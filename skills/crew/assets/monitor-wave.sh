#!/usr/bin/env bash

set -uo pipefail

usage() {
  echo "usage: monitor-wave.sh [--log <log-path>] <parked-path-file> <worktree-path>..." >&2
}

log_file=""
log_option_given=0
case "${1:-}" in
  --log)
    if [ "$#" -lt 3 ] || [ -z "$2" ]; then
      usage
      exit 2
    fi
    log_file=$2
    log_option_given=1
    shift 2
    ;;
  --log=*)
    log_file=${1#--log=}
    if [ -z "$log_file" ]; then
      usage
      exit 2
    fi
    log_option_given=1
    shift
    ;;
esac

if [ "$#" -lt 2 ]; then
  usage
  exit 2
fi

parked_file=$1
shift
script_path=${BASH_SOURCE[0]}
script_dir=$(cd "$(dirname "$script_path")" && pwd -P)
monitor_name=$(basename "$script_path")
if [ "$log_option_given" -eq 0 ]; then
  log_file="$(dirname "$parked_file")/log.jsonl"
fi
poll_seconds=${CREW_POLL_SECONDS:-20}
claude_bin=${CREW_CLAUDE_BIN:-claude}

record_error() {
  local reason=$1
  # The logger is best effort: its failure must not replace the monitor's original nonzero exit.
  python3 "$script_dir/machine_log.py" --log "$log_file" monitor-error \
    --monitor "$monitor_name" --reason "$reason" >/dev/null 2>&1 || true
  echo "MONITOR ERROR $reason" >&2
}

fail() {
  local exit_code=$1
  local reason=$2
  record_error "$reason"
  exit "$exit_code"
}

# A worktree's identity is what its path resolves to, never how it was spelled: the same directory
# is reached as `/tmp/...` and `/private/tmp/...` on macOS, and through a symlinked checkout
# anywhere, and two spellings compared as strings report a live child as vanished. A path that
# resolves to nothing keeps its own text, which still compares equal to itself.
resolve() {
  local path=$1 resolved
  resolved=$(cd "$path" 2>/dev/null && pwd -P) || resolved=$path
  printf '%s\n' "$resolved"
}

[ -f "$parked_file" ] || : >"$parked_file"
expected=$(for path in "$@"; do resolve "$path"; done | jq -R . | jq -s .) \
  || fail 2 "invalid worktree paths"
previous=""

while true; do
  if ! snapshot=$("$claude_bin" agents --json 2>/dev/null); then
    fail 3 "claude agents --json failed"
  fi

  # Every spelling in play — the sessions' own cwds and the parked list — resolved before any of
  # it is compared, because jq has no way to reach the filesystem and ask.
  if ! listed=$(jq -r '.[].cwd // empty' <<<"$snapshot" 2>/dev/null); then
    fail 3 "invalid claude agents JSON"
  fi
  real=$(while IFS= read -r cwd; do
    [ -n "$cwd" ] || continue
    printf '%s\t%s\n' "$cwd" "$(resolve "$cwd")"
  done <<<"$listed" | jq -R 'select(length > 0) | split("\t") | {(.[0]): .[1]}' | jq -s 'add // {}')
  parked_resolved=$(while IFS= read -r path; do
    [ -n "$path" ] || continue
    resolve "$path"
  done <"$parked_file" | jq -R 'select(length > 0)' | jq -s .)

  if ! current=$(jq -r --argjson expected "$expected" --argjson real "$real" \
    --argjson parked_paths "$parked_resolved" '
    $expected[] as $cwd
    | if ($parked_paths | index($cwd)) != null then
        [$cwd, "parked"]
      else
        ([.[] | select((.cwd // "") as $listed | ($real[$listed] // $listed) == $cwd)]) as $agents
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
    fail 3 "invalid claude agents JSON"
  fi

  comm -13 <(printf '%s\n' "$previous" | sort) <(printf '%s\n' "$current" | sort)
  previous=$current

  actionable=0
  while IFS=$'\t' read -r cwd status; do
    case "$status" in
      busy) ;;
      waiting|idle|parked|vanished) actionable=1 ;;
      duplicate)
        fail 4 "duplicate session for $cwd"
        ;;
      *)
        fail 4 "unknown status '$status' for $cwd"
        ;;
    esac
  done <<<"$current"

  if [ "$actionable" -eq 1 ]; then
    echo "MONITOR ACTIONABLE"
    exit 0
  fi

  sleep "$poll_seconds"
done
