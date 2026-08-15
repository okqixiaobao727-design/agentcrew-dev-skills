#!/usr/bin/env bash

set -uo pipefail

if [ "${1:-}" = "agents" ] && [ "${2:-}" = "--json" ]; then
  case "${MONITOR_TEST_SCENARIO:-}" in
    waiting-busy)
      printf '[{"cwd":"/wave/01","status":"waiting"},{"cwd":"/wave/02","status":"busy"}]\n'
      ;;
    busy-to-waiting)
      state_file=${MONITOR_TEST_STATE_FILE:?}
      if [ -f "$state_file" ]; then
        printf '[{"cwd":"/wave/01","status":"waiting"},{"cwd":"/wave/02","status":"busy"}]\n'
      else
        : >"$state_file"
        printf '[{"cwd":"/wave/01","status":"busy"},{"cwd":"/wave/02","status":"busy"}]\n'
      fi
      ;;
    idle-busy)
      printf '[{"cwd":"/wave/01","status":"idle"},{"cwd":"/wave/02","status":"busy"}]\n'
      ;;
    vanished-busy)
      printf '[{"cwd":"/wave/02","status":"busy"}]\n'
      ;;
    parked-busy|all-busy)
      printf '[{"cwd":"/wave/01","status":"busy"},{"cwd":"/wave/02","status":"busy"}]\n'
      ;;
    duplicate)
      printf '[{"cwd":"/wave/01","status":"busy"},{"cwd":"/wave/01","status":"busy"},{"cwd":"/wave/02","status":"busy"}]\n'
      ;;
    aliased-busy)
      # The child is listed under the directory's canonical spelling while the wave was launched
      # with an aliased one — the `/tmp` vs `/private/tmp` shape, made portable with a symlink.
      printf '[{"cwd":"%s","status":"busy"},{"cwd":"/wave/02","status":"busy"}]\n' \
        "${MONITOR_TEST_REAL_PATH:?}"
      ;;
    aliased-duplicate)
      printf '[{"cwd":"%s","status":"busy"},{"cwd":"%s","status":"busy"},%s]\n' \
        "${MONITOR_TEST_REAL_PATH:?}" "${MONITOR_TEST_LINK_PATH:?}" \
        '{"cwd":"/wave/02","status":"busy"}'
      ;;
    unknown)
      printf '[{"cwd":"/wave/01","status":"paused"},{"cwd":"/wave/02","status":"busy"}]\n'
      ;;
    invalid-json)
      printf '{invalid\n'
      ;;
    cli-failure)
      exit 7
      ;;
    *)
      printf 'unknown test scenario: %s\n' "${MONITOR_TEST_SCENARIO:-}" >&2
      exit 2
      ;;
  esac
  exit 0
fi

monitor=${MONITOR_UNDER_TEST:-"$(cd "$(dirname "$0")" && pwd)/monitor-wave.sh"}
test_dir=$(mktemp -d /tmp/crew-monitor-test.XXXXXX)

cleanup() {
  case "$test_dir" in
    /tmp/crew-monitor-test.*) rm -rf -- "$test_dir" ;;
    *) printf 'refusing to clean unexpected path: %s\n' "$test_dir" >&2 ;;
  esac
}
trap cleanup EXIT

run_monitor() {
  local scenario=$1
  local parked_file=$2
  local output_file=$3
  local exit_file=$4
  shift 4
  local state_file="$test_dir/$scenario.state"
  # The wave's membership, as the caller spells it; two fixed paths unless a case says otherwise.
  local -a paths=("$@")
  [ "${#paths[@]}" -gt 0 ] || paths=(/wave/01 /wave/02)
  local -a monitor_args=("$parked_file" "${paths[@]}")
  if [ -n "${MONITOR_TEST_LOG_FILE:-}" ]; then
    monitor_args=(--log "$MONITOR_TEST_LOG_FILE" "${monitor_args[@]}")
  fi

  (
    set +e
    MONITOR_TEST_SCENARIO="$scenario" \
      MONITOR_TEST_STATE_FILE="$state_file" \
      CREW_CLAUDE_BIN="$0" \
      CREW_POLL_SECONDS=0.02 \
      perl -e 'alarm 2; exec @ARGV' \
        "$monitor" "${monitor_args[@]}" >"$output_file" 2>&1
    printf '%s\n' "$?" >"$exit_file"
  ) 2>/dev/null
}

assert_actionable() {
  local scenario=$1
  local expected_state=$2
  local parked_file="$test_dir/$scenario.parked"
  local output_file="$test_dir/$scenario.output"
  local exit_file="$test_dir/$scenario.exit"

  : >"$parked_file"
  if [ "$scenario" = "parked-busy" ]; then
    printf '/wave/01\n' >"$parked_file"
  fi

  run_monitor "$scenario" "$parked_file" "$output_file" "$exit_file"

  if [ "$(cat "$exit_file")" -ne 0 ] \
    || ! grep -q $'/wave/01\t'"$expected_state" "$output_file" \
    || ! grep -q '^MONITOR ACTIONABLE$' "$output_file"; then
    printf 'FAIL actionable scenario: %s\n' "$scenario" >&2
    sed -n '1,20p' "$output_file" >&2
    return 1
  fi
}

assert_error() {
  local scenario=$1
  local expected_message=$2
  shift 2
  local parked_file="$test_dir/$scenario.parked"
  local output_file="$test_dir/$scenario.output"
  local exit_file="$test_dir/$scenario.exit"

  : >"$parked_file"
  run_monitor "$scenario" "$parked_file" "$output_file" "$exit_file" "$@"

  if [ "$(cat "$exit_file")" -eq 0 ] \
    || ! grep -q "$expected_message" "$output_file"; then
    printf 'FAIL error scenario: %s\n' "$scenario" >&2
    sed -n '1,20p' "$output_file" >&2
    return 1
  fi
}

assert_error_records_log() {
  local scenario=$1
  local expected_reason=$2
  local log_mode=${3:-explicit}
  local record_dir="$test_dir/recorded-$scenario-$log_mode"
  local parked_file="$record_dir/parked-paths"
  local output_file="$record_dir/output"
  local exit_file="$record_dir/exit"
  local log_file="$record_dir/log.jsonl"

  mkdir -p "$record_dir"
  : >"$parked_file"
  if [ "$log_mode" = "explicit" ]; then
    MONITOR_TEST_LOG_FILE="$log_file" run_monitor \
      "$scenario" "$parked_file" "$output_file" "$exit_file"
  else
    MONITOR_TEST_LOG_FILE= run_monitor \
      "$scenario" "$parked_file" "$output_file" "$exit_file"
  fi

  if [ "$(cat "$exit_file")" -eq 0 ] \
    || ! grep -q "MONITOR ERROR $expected_reason" "$output_file" \
    || ! jq -s -e --arg reason "$expected_reason" '
      length == 1
      and .[0].event == "monitor-error"
      and .[0].monitor == "monitor-wave.sh"
      and .[0].reason == $reason
    ' "$log_file" >/dev/null 2>&1; then
    printf 'FAIL monitor error was not recorded: %s\n' "$scenario" >&2
    [ -f "$output_file" ] && sed -n '1,20p' "$output_file" >&2
    [ -f "$log_file" ] && sed -n '1,20p' "$log_file" >&2
    return 1
  fi
}

set -e

assert_actionable waiting-busy waiting
assert_actionable busy-to-waiting waiting
assert_actionable idle-busy idle
assert_actionable vanished-busy vanished
assert_actionable parked-busy parked
assert_error duplicate 'MONITOR ERROR duplicate session'
assert_error unknown 'MONITOR ERROR unknown status'
assert_error invalid-json 'MONITOR ERROR invalid claude agents JSON'
assert_error cli-failure 'MONITOR ERROR claude agents --json failed'
assert_error_records_log cli-failure 'claude agents --json failed'
assert_error_records_log invalid-json 'invalid claude agents JSON'
assert_error_records_log duplicate 'duplicate session for /wave/01'
assert_error_records_log unknown "unknown status 'paused' for /wave/01"
assert_error_records_log cli-failure 'claude agents --json failed' default

busy_parked="$test_dir/all-busy.parked"
busy_output="$test_dir/all-busy.output"
busy_exit="$test_dir/all-busy.exit"
: >"$busy_parked"
run_monitor all-busy "$busy_parked" "$busy_output" "$busy_exit"
if [ "$(cat "$busy_exit")" -lt 128 ] || grep -q '^MONITOR ACTIONABLE$' "$busy_output"; then
  printf 'FAIL all-busy monitor did not remain armed\n' >&2
  sed -n '1,20p' "$busy_output" >&2
  exit 1
fi

# A worktree reached by two spellings is one worktree: the wave is launched with the symlinked
# name while the session is listed under the canonical one, and the monitor must stay armed rather
# than call a live child vanished.
mkdir -p "$test_dir/real/worktree-01"
ln -s "$test_dir/real" "$test_dir/link"
export MONITOR_TEST_REAL_PATH="$test_dir/real/worktree-01"
export MONITOR_TEST_LINK_PATH="$test_dir/link/worktree-01"

alias_parked="$test_dir/aliased-busy.parked"
alias_output="$test_dir/aliased-busy.output"
alias_exit="$test_dir/aliased-busy.exit"
: >"$alias_parked"
run_monitor aliased-busy "$alias_parked" "$alias_output" "$alias_exit" \
  "$MONITOR_TEST_LINK_PATH" /wave/02
if [ "$(cat "$alias_exit")" -lt 128 ] || grep -q '^MONITOR ACTIONABLE$' "$alias_output"; then
  printf 'FAIL aliased-busy monitor did not remain armed\n' >&2
  sed -n '1,20p' "$alias_output" >&2
  exit 1
fi

assert_error aliased-duplicate 'MONITOR ERROR duplicate session' \
  "$MONITOR_TEST_LINK_PATH" /wave/02

printf 'MONITOR_WAKE_TESTS_OK\n'
