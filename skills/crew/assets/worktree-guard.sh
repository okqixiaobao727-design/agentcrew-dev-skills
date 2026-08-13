#!/usr/bin/env bash
# Confines file edits to this worktree.
#
# PreToolUse hook, matchers Edit|Write|NotebookEdit. Reads stay open: the coordinator hands each
# child absolute paths to the spec and its ticket, which live outside the worktree.
#
# A parallel run shares one checkout between the coordinator and every worktree, so an absolute
# path aimed at the shared checkout lands in another session's working tree. Session settings apply
# to spawned sub-agents too, which is where prompt wording alone has been observed to fail.
#
# Installed per worktree at .claude/settings.local.json alongside this script. Never global.

set -uo pipefail

input=$(cat)
worktree="<WORKTREE_ABSOLUTE_PATH>"

path=$(jq -r '.tool_input.file_path // .tool_input.notebook_path // ""' <<<"$input")
[ -n "$path" ] || exit 0

case "$path" in
  /*) abs="$path" ;;
  *)  abs="$worktree/$path" ;;
esac

# Resolve the deepest ancestor that exists, then re-append the rest. A brand-new file in a
# brand-new directory is ordinary work, so existence is never required.
resolve() {
  local p="$1" tail="" dir
  while :; do
    if dir=$(cd "$p" 2>/dev/null && pwd -P); then
      printf '%s' "${dir}${tail}"
      return
    fi
    tail="/$(basename "$p")$tail"
    local parent
    parent=$(dirname "$p")
    [ "$parent" != "$p" ] || { printf '%s' "$p$tail"; return; }
    p="$parent"
  done
}

target=$(resolve "$abs")
real_worktree=$(cd "$worktree" 2>/dev/null && pwd -P) || real_worktree="$worktree"

for root in "$real_worktree" "$worktree"; do
  if [[ "$target" == "$root" || "$target" == "$root"/* ]]; then
    exit 0
  fi
done

jq -n --arg p "$target" --arg w "$real_worktree" '{
  hookSpecificOutput: {
    hookEventName: "PreToolUse",
    permissionDecision: "deny",
    permissionDecisionReason: ("Blocked: \($p) is outside this worktree (\($w)). Another session owns that file. Write the equivalent path inside this worktree instead.")
  }
}'
exit 0
