#!/usr/bin/env bash
# Best-effort guard for known destructive Bash command shapes in a crew run.
#
# PreToolUse hook, matcher Bash. The coordinator's authority contract remains the source of truth;
# this pattern guard cannot prove that every irreversible action has been intercepted.
# Deliberately narrow: DELETE and TRUNCATE stay open, because "create fixture -> assert ->
# clean up fixture" is what real-database testing looks like, and a wide filter kills exactly
# the tests the run most wants to execute. Known cost: DELETE FROM <production table> reads
# identically to a fixture cleanup and passes through.
#
# Installed per worktree at .claude/settings.local.json alongside this script. Never global.

set -uo pipefail

input=$(cat)
command=$(jq -r '.tool_input.command // ""' <<<"$input")
workdir=$(jq -r '.cwd // ""' <<<"$input")
[ -n "$workdir" ] || workdir="${CLAUDE_PROJECT_DIR:-$PWD}"

deny() {
  jq -n --arg reason "$1" '{
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: $reason
    }
  }'
  exit 0
}

REASON_SUFFIX="This is an irreversible deletion. Find a reversible way to do it, or stop and \
explain what you want to delete and why."

# Resolve the deepest ancestor that exists, then re-append the rest, so a path that names a file
# still to be created resolves like the directory it will live in. Two spellings of one directory
# — `/tmp` and `/private/tmp` on macOS, a symlinked checkout anywhere — come back as one string,
# which is what makes the containment checks below comparisons of places rather than of text.
resolve() {
  local p="$1" tail="" dir parent
  while :; do
    if dir=$(cd "$p" 2>/dev/null && pwd -P); then
      printf '%s' "${dir}${tail}"
      return
    fi
    tail="/$(basename "$p")$tail"
    parent=$(dirname "$p")
    [ "$parent" != "$p" ] || { printf '%s' "$p$tail"; return; }
    p="$parent"
  done
}

# Strip quotes and grouping parens so quoted paths, SQL, and subshells are matched the same as
# bare commands, then split on shell separators. Checking each segment on its own keeps one
# command's arguments from being read as another's, and keeps "git commit -m 'drop table
# support'" out of the SQL patterns below.
normalized_full=$(tr -d "\"'()" <<<"$command")

# The install step substitutes this worktree's absolute path. A value that never got substituted,
# or one that is relative, cannot say where this worktree is — and a guard that cannot answer that
# question has no way to tell an ordinary command from one aimed at another session's checkout.
# It says so instead of standing down: a safety check that quietly disables itself is worse than
# no safety check, because the run goes on believing it is guarded.
worktree="<WORKTREE_ABSOLUTE_PATH>"
case "$worktree" in
  /*) ;;
  *) deny "Blocked: the red-line guard's worktree path is '$worktree', which is not an \
absolute path — this guard was installed without one and cannot tell which checkout you are \
in. Re-install the worktree's guard assets, or escalate with CREW ASK." ;;
esac

# Where this command runs, resolved once: every containment check below compares against it.
real_workdir=$(resolve "$workdir")
case "$real_workdir" in
  /*) ;;
  *) deny "Blocked: the red-line guard was handed cwd '$workdir', which is not an absolute path, \
so it cannot tell whether a command reaches outside this worktree. Escalate with CREW ASK." ;;
esac
real_worktree=$(resolve "$worktree")
cur="$workdir"

while IFS= read -r normalized; do
  [ -n "${normalized// }" ] || continue

# Track the working directory across cd segments, so a later git segment is judged from where it
# will actually run. Best effort, like every check here.
if grep -Eq '^[[:space:]]*cd([[:space:]]|$)' <<<"$normalized"; then
  dest=$(sed -E 's/^[[:space:]]*cd[[:space:]]*//' <<<"$normalized" | awk '{print $1}')
  case "$dest" in
    ""|'~') cur="$HOME" ;;
    '~/'*)  cur="$HOME/${dest#\~/}" ;;
    -)      : ;;
    /*)     cur="$dest" ;;
    *)      cur="$cur/$dest" ;;
  esac
  continue
fi

# Verbs that only ever move text around — their arguments are data, not actions.
if grep -Eq '^[[:space:]]*(echo|printf|cat|grep|sed|awk|comm|jq)([[:space:]]|$)' <<<"$normalized"; then
  continue
fi

# git cannot execute SQL, however its arguments read; its own red lines are checks 3, 4, and 6.
if grep -Eq '^[[:space:]]*git([[:space:]]|$)' <<<"$normalized"; then

# 6. Git runs against this worktree only — the shared checkout and every other worktree belong to
# other sessions, and a git command aimed there moves another session's HEAD or working tree.
gitdir="$cur"
if grep -Eq '^[[:space:]]*git[[:space:]]+-C[[:space:]]' <<<"$normalized"; then
  cpath=$(sed -E 's/^[[:space:]]*git[[:space:]]+-C[[:space:]]+([^[:space:]]+).*/\1/' <<<"$normalized")
  case "$cpath" in /*) gitdir="$cpath" ;; *) gitdir="$cur/$cpath" ;; esac
elif grep -Eq -- '--work-tree(=|[[:space:]])|--git-dir(=|[[:space:]])' <<<"$normalized"; then
  cpath=$(sed -E 's/.*--(work-tree|git-dir)[= ]([^[:space:]]+).*/\2/' <<<"$normalized")
  case "$cpath" in /*) gitdir="$cpath" ;; *) gitdir="$cur/$cpath" ;; esac
fi
real_git=$(resolve "$gitdir")
if [[ "$real_git" != "$real_worktree" && "$real_git" != "$real_worktree"/* ]]; then
  deny "Blocked: git in $real_git, outside this worktree ($real_worktree). Git runs against this worktree only; re-run it from inside the worktree, or escalate with CREW ASK if the ticket truly needs another checkout."
fi

else

# 1. Dropping a whole database or schema.
if grep -Eiq '(^|[^[:alnum:]_])drop[[:space:]]+(database|schema)([^[:alnum:]_]|$)' <<<"$normalized" \
  || grep -Eq '(^|[^[:alnum:]_])dropdb([^[:alnum:]_]|$)' <<<"$normalized"; then
  deny "Blocked: dropping a database or schema. $REASON_SUFFIX"
fi

# 2. Dropping a table that does not look like a test fixture.
if grep -Eiq '(^|[^[:alnum:]_])drop[[:space:]]+table' <<<"$normalized"; then
  targets=$(grep -Eio 'drop[[:space:]]+table([[:space:]]+if[[:space:]]+exists)?[[:space:]]+[^;[:space:]]+' <<<"$normalized" \
    | sed -E 's/.*[[:space:]]//')
  while IFS= read -r target; do
    [ -n "$target" ] || continue
    # Fixture-shaped names only: test_x, x_test, tmp_x, x_fixture, and schema-qualified forms.
    if ! grep -Eiq '(^|[._])(test|tmp|temp|fixture|scratch)([._]|$)|(^|[._])[[:alnum:]_]*_(test|tmp|temp|fixture)([._]|$)' <<<"$target"; then
      deny "Blocked: DROP TABLE $target does not look like a test fixture. $REASON_SUFFIX"
    fi
  done <<<"$targets"
fi

fi  # end non-SQL-verb guard

# 3. Force-pushing — overwrites history that cannot be recovered from the remote.
if grep -Eq '(^|[^[:alnum:]_])git[[:space:]]+push' <<<"$normalized" \
  && grep -Eq -- '(--force([^-]|$)|--force-with-lease|[[:space:]]-[[:alnum:]]*f([[:space:]]|$)|[[:space:]]\+[^[:space:]]*:)' <<<"$normalized"; then
  deny "Blocked: force-push. $REASON_SUFFIX"
fi

# 4. Deleting a remote branch.
if grep -Eq '(^|[^[:alnum:]_])git[[:space:]]+push' <<<"$normalized" \
  && grep -Eq -- '(--delete([^-]|$)|[[:space:]]-d([[:space:]]|$)|[[:space:]]:[^[:space:]]+)' <<<"$normalized"; then
  deny "Blocked: deleting a remote branch. $REASON_SUFFIX"
fi

# 5. Recursive delete reaching outside this worktree.
if grep -Eq '(^|[^[:alnum:]_])rm[[:space:]]+(-[[:alnum:]]*[rR][[:alnum:]]*[[:space:]]+)*-?[[:alnum:]]*[rR]' <<<"$normalized" \
  || grep -Eq '(^|[^[:alnum:]_])rm[[:space:]]+--recursive' <<<"$normalized"; then
  paths=$(sed -E 's/^.*[^[:alnum:]_]rm[[:space:]]+//' <<<"$normalized" | tr ' ' '\n' | grep -Ev '^-' | grep -v '^$')
  while IFS= read -r path; do
    [ -n "$path" ] || continue
    # A relative target is a target like any other: it is resolved against the directory the
    # segment will run in, so `rm -rf .` after a `cd` out of the worktree is read as the delete
    # it is rather than skipped for being spelled without a leading slash.
    case "$path" in
      '~'*|'$HOME'*)
        deny "Blocked: recursive delete of $path, outside this worktree. $REASON_SUFFIX" ;;
      /*) target=$(resolve "$path") ;;
      *)  target=$(resolve "$cur/$path") ;;
    esac
    [[ "$target" == "$real_workdir"/* ]] \
      || deny "Blocked: recursive delete of $path ($target), outside this worktree. $REASON_SUFFIX"
  done <<<"$paths"
fi

done < <(tr ';|&\n' '\n\n\n\n' <<<"$normalized_full")

# No objection. Silence is not approval — the normal permission flow still runs.
exit 0
