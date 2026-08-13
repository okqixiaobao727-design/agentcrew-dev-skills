#!/usr/bin/env bash

set -uo pipefail

if [ "$#" -lt 2 ]; then
  echo "usage: monitor-wave.sh <parked-path-file> <worktree-path>..." >&2
  exit 2
fi

parked_file=$1
shift
poll_seconds=${CREW_POLL_SECONDS:-20}
claude_bin=${CREW_CLAUDE_BIN:-claude}

[ -f "$parked_file" ] || : >"$parked_file"
expected=$(printf '%s\n' "$@" | jq -R . | jq -s .) || exit 2
previous=""

while true; do
  if ! snapshot=$("$claude_bin" agents --json 2>/dev/null); then
    echo "MONITOR ERROR claude agents --json failed" >&2
    exit 3
  fi

  if ! current=$(jq -r --argjson expected "$expected" --rawfile parked "$parked_file" '
    ($parked | split("\n") | map(select(length > 0))) as $parked_paths
    | $expected[] as $cwd
    | if ($parked_paths | index($cwd)) != null then
        [$cwd, "parked"]
      else
        ([.[] | select(.cwd == $cwd)]) as $agents
        | if ($agents | length) == 0 then
            [$cwd, "vanished"]
          elif ($agents | length) > 1 then
            [$cwd, "duplicate"]
          else
            [$cwd, $agents[0].status]
          end
      end
    | @tsv
  ' <<<"$snapshot" 2>/dev/null); then
    echo "MONITOR ERROR invalid claude agents JSON" >&2
    exit 3
  fi

  comm -13 <(printf '%s\n' "$previous" | sort) <(printf '%s\n' "$current" | sort)
  previous=$current

  actionable=0
  while IFS=$'\t' read -r cwd status; do
    case "$status" in
      busy) ;;
      waiting|idle|parked|vanished) actionable=1 ;;
      duplicate)
        echo "MONITOR ERROR duplicate session for $cwd" >&2
        exit 4
        ;;
      *)
        echo "MONITOR ERROR unknown status '$status' for $cwd" >&2
        exit 4
        ;;
    esac
  done <<<"$current"

  if [ "$actionable" -eq 1 ]; then
    echo "MONITOR ACTIONABLE"
    exit 0
  fi

  sleep "$poll_seconds"
done
