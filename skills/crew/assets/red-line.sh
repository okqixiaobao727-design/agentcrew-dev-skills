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

# Strip quotes and grouping parens so quoted paths, SQL, and subshells are matched the same as
# bare commands, then split on shell separators. Checking each segment on its own keeps one
# command's arguments from being read as another's, and keeps "git commit -m 'drop table
# support'" out of the SQL patterns below.
normalized_full=$(tr -d "\"'()" <<<"$command")

# The install step substitutes this worktree's absolute path; an unsubstituted or relative value
# disables check 6 only.
worktree="<WORKTREE_ABSOLUTE_PATH>"
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
case "$worktree" in
/*)
  gitdir="$cur"
  if grep -Eq '^[[:space:]]*git[[:space:]]+-C[[:space:]]' <<<"$normalized"; then
    cpath=$(sed -E 's/^[[:space:]]*git[[:space:]]+-C[[:space:]]+([^[:space:]]+).*/\1/' <<<"$normalized")
    case "$cpath" in /*) gitdir="$cpath" ;; *) gitdir="$cur/$cpath" ;; esac
  elif grep -Eq -- '--work-tree(=|[[:space:]])|--git-dir(=|[[:space:]])' <<<"$normalized"; then
    cpath=$(sed -E 's/.*--(work-tree|git-dir)[= ]([^[:space:]]+).*/\2/' <<<"$normalized")
    case "$cpath" in /*) gitdir="$cpath" ;; *) gitdir="$cur/$cpath" ;; esac
  fi
  real_wt=$(cd "$worktree" 2>/dev/null && pwd -P) || real_wt="$worktree"
  real_git=$(cd "$gitdir" 2>/dev/null && pwd -P) || real_git="$gitdir"
  if [[ "$real_git" != "$real_wt" && "$real_git" != "$real_wt"/* ]]; then
    deny "Blocked: git in $real_git, outside this worktree ($real_wt). Git runs against this worktree only; re-run it from inside the worktree, or escalate with CREW ASK if the ticket truly needs another checkout."
  fi
  ;;
esac

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
    case "$path" in
      /*)  [[ "$path" == "$workdir"/* ]] || deny "Blocked: recursive delete of $path, outside this worktree. $REASON_SUFFIX" ;;
      '~'*|'$HOME'*) deny "Blocked: recursive delete of $path, outside this worktree. $REASON_SUFFIX" ;;
      *..*) deny "Blocked: recursive delete of $path escapes this worktree. $REASON_SUFFIX" ;;
    esac
  done <<<"$paths"
fi

done < <(tr ';|&\n' '\n\n\n\n' <<<"$normalized_full")

# No objection. Silence is not approval — the normal permission flow still runs.
exit 0
