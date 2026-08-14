#!/usr/bin/env bash
# The shell side of the two path invariants: a path that crosses a cwd boundary is absolute before
# it is recorded, and two paths are compared by what they resolve to.
#
# Both subjects here run somewhere other than where they were configured. The machine-log hook is
# registered from the run directory and fires in a child's worktree; the red-line guard is written
# per worktree and judges commands that name paths from anywhere. Modelled on
# `test-monitor-wave.sh`: no framework, one temporary directory, a line per case.
#
#     bash test-path-handling.sh   # prints PATH_HANDLING_TESTS_OK, or the cases that failed

set -uo pipefail

here=$(cd "$(dirname "$0")" && pwd -P)
test_dir=$(mktemp -d /tmp/crew-path-test.XXXXXX)

cleanup() {
  case "$test_dir" in
    /tmp/crew-path-test.*) rm -rf -- "$test_dir" ;;
    *) printf 'refusing to clean unexpected path: %s\n' "$test_dir" >&2 ;;
  esac
}
trap cleanup EXIT

failures=0
fail() {
  printf 'FAIL %s\n' "$1" >&2
  shift
  [ "$#" -eq 0 ] || printf '  %s\n' "$@" >&2
  failures=$((failures + 1))
}

# --- the machine-log hook: registered in one directory, run in another ------------------------

hook_dir="$test_dir/hook"
run_dir="$hook_dir/run"
worktree="$hook_dir/worktree"
settings="$worktree/.claude/settings.local.json"
mkdir -p "$run_dir" "$worktree/.claude"

if ! (cd "$run_dir" && python3 "$here/machine_log.py" --log machine-log.jsonl install \
  --settings "$settings" --role child --ticket 20 >/dev/null); then
  fail "machine-log install with a relative --log"
else
  command=$(jq -r '
    .hooks.PostToolUse[] | select(.matcher == "SendMessage") | .hooks[0].command
  ' "$settings")
  payload=$(jq -n '{
    tool_name: "SendMessage",
    tool_input: {to: "coordinator", message: "CREW ASK 20 stuck — ts=1755060042"}
  }')
  (cd "$worktree" && printf '%s' "$payload" | eval "$command" >/dev/null)

  if ! grep -q 'CREW ASK 20' "$run_dir/machine-log.jsonl" 2>/dev/null; then
    fail "hook fired from another cwd did not write the intended log" \
      "expected: $run_dir/machine-log.jsonl"
  fi
  if [ -e "$worktree/machine-log.jsonl" ]; then
    fail "hook wrote a log into the cwd it happened to run in"
  fi
fi

# --- the red-line guard: its own path checks ---------------------------------------------------

guards=0
guard_for() {
  # A copy of the guard with `worktree` set to the value under test, as the install step sets it.
  guards=$((guards + 1))
  local script="$test_dir/red-line-$guards.sh"
  WORKTREE_VALUE="$1" perl -pe 's/<WORKTREE_ABSOLUTE_PATH>/$ENV{WORKTREE_VALUE}/g' \
    "$here/red-line.sh" >"$script"
  chmod +x "$script"
  printf '%s\n' "$script"
}

decision() {
  # What the guard decided about one command: `deny`, or empty when it raised no objection.
  local script=$1 cwd=$2 shell_command=$3
  jq -n --arg cwd "$cwd" --arg command "$shell_command" \
    '{cwd: $cwd, tool_name: "Bash", tool_input: {command: $command}}' \
    | "$script" \
    | jq -r '.hookSpecificOutput.permissionDecision // ""' 2>/dev/null
}

assert_deny() {
  local name=$1 script=$2 cwd=$3 shell_command=$4
  local verdict
  verdict=$(decision "$script" "$cwd" "$shell_command")
  [ "$verdict" = "deny" ] || fail "$name" "expected deny, got '${verdict:-<silence>}'"
}

assert_allow() {
  local name=$1 script=$2 cwd=$3 shell_command=$4
  local verdict
  verdict=$(decision "$script" "$cwd" "$shell_command")
  [ -z "$verdict" ] || fail "$name" "expected no objection, got '$verdict'"
}

guard_worktree="$test_dir/checkout/worktree-20"
mkdir -p "$guard_worktree/build" "$test_dir/elsewhere"
ln -s "$test_dir/checkout" "$test_dir/checkout-link"
real_worktree=$(cd "$guard_worktree" && pwd -P)

# A guard whose worktree path never got substituted, or got a relative one, cannot judge anything
# — and a check that cannot judge must say so rather than wave the command through.
unsubstituted=$(guard_for '<WORKTREE_ABSOLUTE_PATH>')
assert_deny "unsubstituted worktree path is loud" \
  "$unsubstituted" "$real_worktree" "git status"

relative=$(guard_for 'checkout/worktree-20')
assert_deny "relative worktree path is loud" \
  "$relative" "$real_worktree" "git status"

configured=$(guard_for "$guard_worktree")
assert_allow "git inside the worktree passes" \
  "$configured" "$real_worktree" "git status"
assert_deny "git aimed outside the worktree is denied" \
  "$configured" "$real_worktree" "git -C $test_dir/elsewhere reset --hard"

# The same directory under two spellings is one worktree, so neither check fires on it.
aliased=$(guard_for "$test_dir/checkout-link/worktree-20")
assert_allow "git in an aliased spelling of the worktree passes" \
  "$aliased" "$real_worktree" "git status"

assert_allow "a recursive delete inside the worktree passes" \
  "$configured" "$real_worktree" "rm -rf build"
assert_deny "an absolute recursive delete outside the worktree is denied" \
  "$configured" "$real_worktree" "rm -rf $test_dir/elsewhere"
assert_deny "a relative recursive delete outside the worktree is denied" \
  "$configured" "$real_worktree" "cd $test_dir/elsewhere && rm -rf ."
assert_deny "a relative recursive delete climbing out is denied" \
  "$configured" "$real_worktree" "rm -rf ../worktree-19"

[ "$failures" -eq 0 ] || exit 1
printf 'PATH_HANDLING_TESTS_OK\n'
