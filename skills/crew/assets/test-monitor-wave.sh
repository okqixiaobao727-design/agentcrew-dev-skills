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
  local state_file="$test_dir/$scenario.state"

  (
    set +e
    MONITOR_TEST_SCENARIO="$scenario" \
      MONITOR_TEST_STATE_FILE="$state_file" \
      CREW_CLAUDE_BIN="$0" \
      CREW_POLL_SECONDS=0.02 \
      perl -e 'alarm 2; exec @ARGV' \
        "$monitor" "$parked_file" /wave/01 /wave/02 >"$output_file" 2>&1
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
  local parked_file="$test_dir/$scenario.parked"
  local output_file="$test_dir/$scenario.output"
  local exit_file="$test_dir/$scenario.exit"

  : >"$parked_file"
  run_monitor "$scenario" "$parked_file" "$output_file" "$exit_file"

  if [ "$(cat "$exit_file")" -eq 0 ] \
    || ! grep -q "$expected_message" "$output_file"; then
    printf 'FAIL error scenario: %s\n' "$scenario" >&2
    sed -n '1,20p' "$output_file" >&2
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

printf 'MONITOR_WAKE_TESTS_OK\n'
