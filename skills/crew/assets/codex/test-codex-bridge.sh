#!/usr/bin/env bash
# End-to-end tests for codex_bridge.py against stub_codex.py.
# Uses a private tmux server (-L codex-bridge-test); needs tmux and python3+aiohttp.

set -uo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
BRIDGE="$SCRIPT_DIR/codex_bridge.py"
STUB="$SCRIPT_DIR/stub_codex.py"
PYTHON=${PYTHON:-python3}
REAL_TMUX=$(command -v tmux) || { echo "tmux not found"; exit 1; }
STUB_SHA="1234567890abcdef1234567890abcdef12345678"
PINNED_MODEL="model-x"
PINNED_EFFORT="effort-y"
CHILD_MESSAGE="ordinary child update"
ESCALATION_MESSAGE="CREW ASK 18 scope — choose option A or option B.
ts=1755060060"
RULING_MESSAGE="Use option A; it is reversible.
ts=1755060070"
# What a child says on a turn the bridge never started: the operator answered in the pane, and the
# receipt that follows is the run's only word about the work.
TYPED_TURN_MESSAGE="Ruling applied and the work is committed.
CREW COMPLETE $STUB_SHA"

WORK=$(mktemp -d -t codex-bridge-test)
TUI_EXIT_RUNTIME_ROOT=$(mktemp -d /tmp/codex-tui-exit.XXXXXX)
RELAUNCH_EARLY_RUNTIME_ROOT=$(mktemp -d /tmp/codex-relaunch-early.XXXXXX)
BIN="$WORK/bin"
PLUGIN_ROOT="$WORK/mattpocock-skills"
SKILL_DIR="$PLUGIN_ROOT/skills/engineering/implement"
SKILL_PATH="$SKILL_DIR/SKILL.md"
CACHE_PLUGIN_ROOT="$WORK/codex-home/plugins/cache/mattpocock/mattpocock-skills/1.2.3"
CACHE_SKILL_DIR="$CACHE_PLUGIN_ROOT/skills/engineering/implement"
CACHE_SKILL_PATH="$CACHE_SKILL_DIR/SKILL.md"
mkdir -p "$BIN"
mkdir -p "$SKILL_DIR"
mkdir -p "$CACHE_SKILL_DIR"
touch "$SKILL_PATH"
touch "$CACHE_SKILL_PATH"
export CODEX_STUB_PLUGIN_ROOT="$PLUGIN_ROOT"
export CODEX_HOME="$WORK/codex-home"

TMUX_LIST_PANES_FAILURES="$WORK/tmux-list-panes-failures"
TMUX_LIST_PANES_FAILED="$WORK/tmux-list-panes-failed"
export TMUX_LIST_PANES_FAILURES TMUX_LIST_PANES_FAILED

printf '#!/bin/sh
if [ "$1" = list-panes ] && [ -f "$TMUX_LIST_PANES_FAILURES" ]; then
  remaining=$(cat "$TMUX_LIST_PANES_FAILURES")
  if [ "$remaining" -gt 0 ]; then
    printf "%%s\\n" "$((remaining - 1))" > "$TMUX_LIST_PANES_FAILURES"
    printf "failed\\n" >> "$TMUX_LIST_PANES_FAILED"
    printf "tmux list-panes test failure\\n" >&2
    exit 71
  fi
fi
if [ "$1" = new-window ] && [ "${CODEX_STUB_DELAY_NEW_WINDOW_RETURN:-}" = 1 ]; then
  output=$("%s" -L codex-bridge-test "$@")
  status=$?
  pane_id=$(printf "%%s\n" "$output" | cut -f 2)
  while "%s" -L codex-bridge-test list-panes -a -F "#{pane_id}" \
      | grep -Fx "$pane_id" >/dev/null; do
    sleep 0.01
  done
  printf "%%s\n" "$output"
  exit "$status"
fi
exec "%s" -L codex-bridge-test "$@"
' "$REAL_TMUX" "$REAL_TMUX" "$REAL_TMUX" > "$BIN/tmux"
printf '#!/bin/sh\nexec "%s" "%s" "$@"\n' "$PYTHON" "$STUB" > "$BIN/codex"
chmod +x "$BIN/tmux" "$BIN/codex"
export PATH="$BIN:$PATH"

cleanup() {
  tmux kill-server 2>/dev/null
  rm -rf "$WORK" "$TUI_EXIT_RUNTIME_ROOT" "$RELAUNCH_EARLY_RUNTIME_ROOT"
}
trap cleanup EXIT

tmux new-session -d -s bt -x 200 -y 50 || { echo "cannot start test tmux server"; exit 1; }

FAILURES=0
fail() { echo "FAIL: $*"; FAILURES=$((FAILURES + 1)); }
ok() { echo "ok: $*"; }

# json_field <file> <key...>  — prints nested value or empty
json_field() {
  "$PYTHON" - "$@" <<'PY'
import json, sys
value = json.load(open(sys.argv[1]))
for key in sys.argv[2:]:
    key = int(key) if key.lstrip("-").isdigit() else key
    try:
        value = value[key]
    except (KeyError, IndexError, TypeError):
        print("")
        sys.exit(0)
print(value if value is not None else "")
PY
}

assert_log_event() { # <log> <index> <event> <role> <ticket> <message>
  "$PYTHON" - "$@" <<'PY'
import json
import re
import sys

path, index, expected_event, expected_role, expected_ticket, expected_message = sys.argv[1:]
with open(path, encoding="utf-8") as stream:
    records = [json.loads(line) for line in stream if line.strip()]

entry = records[int(index)]
assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", entry["ts"]), entry
assert entry["event"] == expected_event, entry
assert entry["role"] == expected_role, entry
assert entry["ticket"] == expected_ticket, entry
assert entry["message"] == expected_message, entry
assert set(entry) <= {"ts", "event", "role", "ticket", "to", "message"}, entry
PY
}

assert_stub_argv() { # <dir> <expected-model> <expected-effort> <start-index>
  "$PYTHON" - "$1/stub-argv.jsonl" "$2" "$3" "$4" <<'PY'
import json
import sys

path, expected_model, expected_effort, start_index = sys.argv[1:]
with open(path, encoding="utf-8") as stream:
    invocations = [json.loads(line) for line in stream if line.strip()]

start = int(start_index)
selected = invocations[start:start + 2]
assert len(selected) == 2, (
    f"expected two Codex invocations from index {start}, got {len(selected)}"
)

for argv in selected:
    expected = []
    if expected_model:
        expected.extend(["-c", f'model="{expected_model}"'])
    if expected_effort:
        expected.extend(["-c", f'model_reasoning_effort="{expected_effort}"'])
    if not expected:
        assert "-c" not in argv, f"unexpected config override in argv {argv!r}"
        continue

    starts = [
        index
        for index in range(len(argv) - len(expected) + 1)
        if argv[index:index + len(expected)] == expected
    ]
    assert len(starts) == 1, f"expected one override block in argv {argv!r}"
    block_end = starts[0] + len(expected)
    if argv[0] == "app-server":
        positional_start = len(argv)
    elif "resume" in argv:
        positional_start = argv.index("resume")
    else:
        positional_start = len(argv) - 1
    assert block_end == positional_start, (
        f"override block must end before positionals in argv {argv!r}"
    )
PY
}

patch_state() { # <state-file> <key=value>...
  "$PYTHON" - "$@" <<'PYPATCH'
import json
import sys

path = sys.argv[1]
with open(path, encoding="utf-8") as stream:
    state = json.load(stream)
for assignment in sys.argv[2:]:
    key, _, value = assignment.partition("=")
    state[key] = None if value == "null" else value
with open(path, "w", encoding="utf-8") as stream:
    json.dump(state, stream, ensure_ascii=False, sort_keys=True)
PYPATCH
}

assert_log_count() { # <log> <expected>
  "$PYTHON" - "$@" <<'PYCOUNT'
import json
import sys

path, expected = sys.argv[1:]
with open(path, encoding="utf-8") as stream:
    records = [json.loads(line) for line in stream if line.strip()]
assert len(records) == int(expected), records
PYCOUNT
}

wait_for_stub_argv() { # <dir> <minimum-count>
  local path="$1/stub-argv.jsonl" minimum="$2" attempt=0
  while [ "$attempt" -lt 100 ]; do
    if [ -f "$path" ] && [ "$(wc -l < "$path")" -ge "$minimum" ]; then
      return 0
    fi
    sleep 0.05
    attempt=$((attempt + 1))
  done
  return 1
}

make_child() { # <name> <scenario> -> prints dir
  local dir="$WORK/$1"
  mkdir -p "$dir"
  printf '%s\n' "$2" > "$dir/.codex-stub-scenario"
  printf '%s' "$dir"
}

launch() { # <dir> <state-file> <window-name> <out-file>
  launch_with_options "$1" "$2" "$3" "$4"
}

launch_with_options() { # <dir> <state-file> <window-name> <out-file> [launch options]
  local dir="$1" state_file="$2" window_name="$3" out_file="$4"
  shift 4
  "$PYTHON" "$BRIDGE" launch --cwd "$dir" --tmux-session 'bt:' \
    --window-name "$window_name" --state-file "$state_file" \
    --startup-timeout 15 --prompt "run the implement skill on ticket $window_name" \
    "$@" > "$out_file" 2>"$out_file.err"
}

watch() { # <out-file> <state-file...>
  local out="$1"; shift
  "$PYTHON" "$BRIDGE" watch --interval 0.3 --timeout 30 "$@" > "$out" 2>"$out.err"
}

fail_next_pane_reads() { # <count>
  printf '%s\n' "$1" > "$TMUX_LIST_PANES_FAILURES"
  : > "$TMUX_LIST_PANES_FAILED"
}

wait_for_failed_pane_reads() { # <count>
  local expected="$1" attempt=0
  while [ "$attempt" -lt 100 ]; do
    if [ -f "$TMUX_LIST_PANES_FAILED" ] \
        && [ "$(wc -l < "$TMUX_LIST_PANES_FAILED")" -ge "$expected" ]; then
      return 0
    fi
    sleep 0.05
    attempt=$((attempt + 1))
  done
  return 1
}

# --- Test 1: receipt turn reaches idle/completed with the receipt message ---
test_receipt() {
  local dir; dir=$(make_child t1 receipt)
  local sf="$WORK/t1.state.json" out="$WORK/t1.launch.json" snap="$WORK/t1.watch.json"
  launch "$dir" "$sf" 01 "$out" || { fail "receipt: launch exited $? ($(cat "$out.err"))"; return; }
  [ "$(json_field "$out" ok)" = "True" ] || { fail "receipt: launch not ok"; return; }
  watch "$snap" "$sf" || { fail "receipt: watch exited $? ($(cat "$snap.err"))"; return; }
  [ "$(json_field "$snap" sessions 0 status)" = "idle" ] || fail "receipt: status not idle"
  [ "$(json_field "$snap" sessions 0 turnStatus)" = "completed" ] || fail "receipt: turn not completed"
  case "$(json_field "$snap" sessions 0 finalMessage)" in
    *"CREW COMPLETE $STUB_SHA"*) ok "receipt: receipt surfaced" ;;
    *) fail "receipt: receipt missing from finalMessage" ;;
  esac
  [ "$(json_field "$sf" status)" = "idle" ] || fail "receipt: state file not updated"
}

# --- Test 16: a Codex CREW ASK read by watch is copied to the machine log ---
test_escalation_logging() {
  local dir; dir=$(make_child t16 escalation)
  local sf="$WORK/t16.state.json" out="$WORK/t16.launch.json" snap="$WORK/t16.watch.json"
  local log="$WORK/t16.log.jsonl"
  launch_with_options "$dir" "$sf" 18 "$out" \
    --machine-log "$log" --ticket 18 \
    || { fail "escalation-log: launch exited $?"; return; }
  watch "$snap" "$sf" \
    || { fail "escalation-log: watch exited $? ($(cat "$snap.err"))"; return; }
  assert_log_event "$log" 0 escalation child 18 "$ESCALATION_MESSAGE" \
    && ok "escalation-log: CREW ASK appended with child fields" \
    || fail "escalation-log: event did not match the machine-log contract"
  watch "$WORK/t16.watch-again.json" "$sf" \
    || { fail "escalation-log: repeated watch exited $?"; return; }
  "$PYTHON" - "$log" <<'PY' \
    && ok "escalation-log: repeated observation did not duplicate the event" \
    || fail "escalation-log: repeated observation duplicated the event"
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    records = [json.loads(line) for line in stream if line.strip()]
assert len(records) == 1, records
PY
}

# --- Test 17: a ruling sent through the bridge is copied to the machine log ---
test_ruling_logging() {
  local dir; dir=$(make_child t17 message)
  local sf="$WORK/t17.state.json" out="$WORK/t17.launch.json" snap="$WORK/t17.watch.json"
  local log="$WORK/t17.log.jsonl" answer="$WORK/t17.answer"
  launch_with_options "$dir" "$sf" 19 "$out" \
    --machine-log "$log" --ticket 19 \
    || { fail "ruling-log: launch exited $?"; return; }
  watch "$snap" "$sf" \
    || { fail "ruling-log: watch exited $? ($(cat "$snap.err"))"; return; }
  printf '%s' "$RULING_MESSAGE" > "$answer"
  "$PYTHON" "$BRIDGE" send --state-file "$sf" --prompt-file "$answer" \
    > "$WORK/t17.send.json" 2> "$WORK/t17.send.err" \
    || { fail "ruling-log: send exited $? ($(cat "$WORK/t17.send.err"))"; return; }
  assert_log_event "$log" 0 message child 19 "$CHILD_MESSAGE" \
    || { fail "ruling-log: child message was not classified"; return; }
  assert_log_event "$log" 1 ruling coordinator 19 "$RULING_MESSAGE" \
    && ok "ruling-log: ruling appended with coordinator fields" \
    || fail "ruling-log: event did not match the machine-log contract"
}

# --- Test 18: a resumed state keeps its logging configuration ---
test_resume_keeps_logging_configuration() {
  local dir; dir=$(make_child t18 escalation)
  local sf="$WORK/t18.state.json" out="$WORK/t18.launch.json"
  local log="$WORK/t18.log.jsonl"
  launch_with_options "$dir" "$sf" 18 "$out" \
    --machine-log "$log" --ticket 18 \
    || { fail "resume-log: initial launch exited $?"; return; }
  local thread_id; thread_id=$(json_field "$sf" threadId)
  local window_id; window_id=$(json_field "$sf" windowId)
  tmux kill-window -t "$window_id" \
    || { fail "resume-log: could not kill initial window"; return; }
  sleep 0.2
  launch_with_options "$dir" "$sf" 20 "$WORK/t18.resume.json" \
    --thread-id "$thread_id" \
    || { fail "resume-log: resumed launch exited $? ($(cat "$WORK/t18.resume.json.err"))"; return; }
  watch "$WORK/t18.watch.json" "$sf" \
    || { fail "resume-log: watch exited $?"; return; }
  assert_log_event "$log" 0 escalation child 18 "$ESCALATION_MESSAGE" \
    && ok "resume-log: logging configuration survived resume" \
    || fail "resume-log: resumed state did not log the escalation"
}

# --- Test 19: a turn the bridge never started is watched and its message logged ---
test_unmarked_turn_is_watched() {
  local dir; dir=$(make_child t19 receipt)
  local sf="$WORK/t19.state.json" out="$WORK/t19.launch.json" snap="$WORK/t19.watch.json"
  local log="$WORK/t19.log.jsonl"
  launch_with_options "$dir" "$sf" 21 "$out" \
    --machine-log "$log" --ticket 21 \
    || { fail "unmarked-turn: launch exited $?"; return; }
  watch "$snap" "$sf" \
    || { fail "unmarked-turn: first watch exited $? ($(cat "$snap.err"))"; return; }
  printf '%s' "$TYPED_TURN_MESSAGE" > "$dir/stub-typed-turn"
  watch "$WORK/t19.watch-typed.json" "$sf" \
    || { fail "unmarked-turn: second watch exited $?"; return; }
  [ "$(json_field "$WORK/t19.watch-typed.json" sessions 0 finalMessage)" = "$TYPED_TURN_MESSAGE" ] \
    && ok "unmarked-turn: the typed turn's message surfaced" \
    || fail "unmarked-turn: watch reported the marked turn instead"
  assert_log_event "$log" 1 message child 21 "$TYPED_TURN_MESSAGE" \
    && ok "unmarked-turn: the typed turn's message reached the log" \
    || fail "unmarked-turn: the typed turn's message was never logged"
}

# --- Test 20: a message whose busy->idle edge was missed is logged once, and only once ---
test_missed_edge_is_still_logged_once() {
  local dir; dir=$(make_child t20 message)
  local sf="$WORK/t20.state.json" out="$WORK/t20.launch.json" snap="$WORK/t20.watch.json"
  local log="$WORK/t20.log.jsonl"
  launch_with_options "$dir" "$sf" 22 "$out" \
    --machine-log "$log" --ticket 22 \
    || { fail "missed-edge: launch exited $?"; return; }
  # The edge a watch that died between polls consumed: the turn has finished, the state already
  # says idle, and the message it went idle carrying was never recorded.
  sleep 1
  patch_state "$sf" status=idle turnStatus=completed finalMessage=null \
    || { fail "missed-edge: could not stage the missed edge"; return; }
  watch "$snap" "$sf" || { fail "missed-edge: watch exited $? ($(cat "$snap.err"))"; return; }
  assert_log_event "$log" 0 message child 22 "$CHILD_MESSAGE" \
    && ok "missed-edge: the message survived the missed edge" \
    || fail "missed-edge: no edge meant no message"
  watch "$WORK/t20.watch-again.json" "$sf" \
    || { fail "missed-edge: repeated watch exited $?"; return; }
  assert_log_count "$log" 1 \
    && ok "missed-edge: an unchanged message was not logged twice" \
    || fail "missed-edge: the message was logged twice"
}

# --- Test 21: a finished turn nobody recorded survives the turn started on top of it ---
test_unrecorded_turn_survives_a_later_turn() {
  local dir; dir=$(make_child t21 message)
  local sf="$WORK/t21.state.json" out="$WORK/t21.launch.json"
  local log="$WORK/t21.log.jsonl"
  launch_with_options "$dir" "$sf" 23 "$out" \
    --machine-log "$log" --ticket 23 \
    || { fail "unrecorded-turn: launch exited $?"; return; }
  # The turn finishes with no watch running, and the operator types the next one before one is:
  # the finished turn is now behind an unfinished one, and only the thread still holds it.
  sleep 1
  printf 'x' > "$dir/stub-typed-turn-held"
  # The session is busy, so watch cannot come back with a snapshot; it is run to its own timeout,
  # and what is asserted is what reached the log while it watched.
  if "$PYTHON" "$BRIDGE" watch --interval 0.3 --timeout 3 "$sf" \
      > "$WORK/t21.watch.json" 2> "$WORK/t21.watch.err"; then
    fail "unrecorded-turn: watch returned while the session was busy"
    return
  fi
  assert_log_event "$log" 0 message child 23 "$CHILD_MESSAGE" \
    && ok "unrecorded-turn: the finished turn's message still reached the log" \
    || fail "unrecorded-turn: a later turn buried the message"
}

# --- Test 11: pinned model and effort reach both Codex argv lists and state ---
test_model_effort_overrides() {
  local dir; dir=$(make_child t11 receipt)
  local sf="$WORK/t11.state.json" out="$WORK/t11.launch.json"
  launch_with_options "$dir" "$sf" 12 "$out" \
    --model "$PINNED_MODEL" --effort "$PINNED_EFFORT" \
    || { fail "pinned: launch exited $?"; return; }
  wait_for_stub_argv "$dir" 2 \
    || { fail "pinned: TUI invocation was not recorded"; return; }
  assert_stub_argv "$dir" "$PINNED_MODEL" "$PINNED_EFFORT" 0 \
    && ok "pinned: both overrides reached app-server and TUI" \
    || fail "pinned: overrides missing from Codex argv"
  [ "$(json_field "$sf" model)" = "$PINNED_MODEL" ] \
    && ok "pinned: model persisted" || fail "pinned: model missing from state"
  [ "$(json_field "$sf" effort)" = "$PINNED_EFFORT" ] \
    && ok "pinned: effort persisted" || fail "pinned: effort missing from state"
}

# --- Test 12: an unpinned launch keeps the pre-existing argv shape ---
test_without_model_effort_overrides() {
  local dir; dir=$(make_child t12 receipt)
  local sf="$WORK/t12.state.json" out="$WORK/t12.launch.json"
  launch "$dir" "$sf" 13 "$out" \
    || { fail "unpinned: launch exited $?"; return; }
  wait_for_stub_argv "$dir" 2 \
    || { fail "unpinned: TUI invocation was not recorded"; return; }
  assert_stub_argv "$dir" "" "" 0 \
    && ok "unpinned: no model or effort override" \
    || fail "unpinned: unexpected Codex config override"
}

# --- Test 13: relaunching a vanished thread inherits its pinned values ---
test_resume_keeps_pinned_model_effort() {
  local dir; dir=$(make_child t13 receipt)
  local sf="$WORK/t13.state.json" out="$WORK/t13.launch.json"
  launch_with_options "$dir" "$sf" 14 "$out" \
    --model "$PINNED_MODEL" --effort "$PINNED_EFFORT" \
    || { fail "resume: initial launch exited $?"; return; }
  local thread_id; thread_id=$(json_field "$sf" threadId)
  local window_id; window_id=$(json_field "$sf" windowId)
  tmux kill-window -t "$window_id" || {
    fail "resume: could not kill initial window"
    return
  }
  sleep 0.2
  launch_with_options "$dir" "$sf" 15 "$WORK/t13.resume.json" \
    --thread-id "$thread_id" \
    || { fail "resume: relaunch exited $? ($(cat "$WORK/t13.resume.json.err"))"; return; }
  wait_for_stub_argv "$dir" 4 \
    || { fail "resume: TUI invocation was not recorded"; return; }
  assert_stub_argv "$dir" "$PINNED_MODEL" "$PINNED_EFFORT" 2 \
    && ok "resume: pinned values restored in both Codex argv lists" \
    || fail "resume: pinned values were not restored"
  [ "$(json_field "$sf" model)" = "$PINNED_MODEL" ] \
    && ok "resume: model remains persisted" || fail "resume: model changed"
  [ "$(json_field "$sf" effort)" = "$PINNED_EFFORT" ] \
    && ok "resume: effort remains persisted" || fail "resume: effort changed"
}

# --- Test 14: model-only pins persist and resume without an effort pin ---
test_model_only_pin_and_resume() {
  local dir; dir=$(make_child t14 receipt)
  local sf="$WORK/t14.state.json" out="$WORK/t14.launch.json"
  launch_with_options "$dir" "$sf" 16 "$out" --model "$PINNED_MODEL" \
    || { fail "model-only: initial launch exited $?"; return; }
  wait_for_stub_argv "$dir" 2 \
    || { fail "model-only: TUI invocation was not recorded"; return; }
  assert_stub_argv "$dir" "$PINNED_MODEL" "" 0 \
    && ok "model-only: model override reached both Codex argv lists" \
    || fail "model-only: argv mismatch"
  [ "$(json_field "$sf" model)" = "$PINNED_MODEL" ] \
    && ok "model-only: model persisted" || fail "model-only: model missing from state"
  [ -z "$(json_field "$sf" effort)" ] \
    && ok "model-only: effort remains unpinned" || fail "model-only: effort was pinned"

  local thread_id; thread_id=$(json_field "$sf" threadId)
  local window_id; window_id=$(json_field "$sf" windowId)
  tmux kill-window -t "$window_id" || {
    fail "model-only: could not kill initial window"
    return
  }
  sleep 0.2
  launch_with_options "$dir" "$sf" 17 "$WORK/t14.resume.json" \
    --thread-id "$thread_id" \
    || { fail "model-only: resume exited $?"; return; }
  wait_for_stub_argv "$dir" 4 \
    || { fail "model-only: resumed TUI invocation was not recorded"; return; }
  assert_stub_argv "$dir" "$PINNED_MODEL" "" 2 \
    && ok "model-only: persisted model restored without effort" \
    || fail "model-only: persisted model was not restored"
}

assert_unusable_resume_state() { # <name> <state-content> <window-name>
  local name="$1" state_content="$2" window_name="$3"
  local dir; dir=$(make_child "$name" receipt)
  local sf="$WORK/$name.state.json" out="$WORK/$name.launch.json"
  local initial_out="$WORK/$name.initial.json"
  launch "$dir" "$sf" "$window_name-initial" "$initial_out" \
    || { fail "$name: could not materialize the thread before relaunch"; return; }
  local thread_id; thread_id=$(json_field "$sf" threadId)
  local window_id; window_id=$(json_field "$sf" windowId)
  tmux kill-window -t "$window_id" || {
    fail "$name: could not stop the initial launch"
    return
  }
  sleep 0.2
  printf '%s' "$state_content" > "$sf"
  launch_with_options "$dir" "$sf" "$window_name" "$out" \
    --thread-id "$thread_id" \
    || { fail "$name: unusable state blocked relaunch"; return; }
  [ "$(json_field "$out" ok)" = "True" ] \
    && ok "$name: relaunch succeeded without inherited pins" \
    || fail "$name: relaunch did not return ok"
}

# --- Test 15: unusable prior state degrades to an unpinned relaunch ---
test_unusable_resume_state() {
  assert_unusable_resume_state t15-corrupt '{not-json' 18
  assert_unusable_resume_state t15-version '{"version":999}' 19
}

# --- Test 2: question turn, answered via send, completes on the follow-up ---
test_question_send() {
  local dir; dir=$(make_child t2 question)
  local sf="$WORK/t2.state.json" out="$WORK/t2.launch.json"
  launch "$dir" "$sf" 02 "$out" || { fail "question: launch exited $?"; return; }
  watch "$WORK/t2.watch1.json" "$sf" || { fail "question: first watch exited $?"; return; }
  case "$(json_field "$WORK/t2.watch1.json" sessions 0 finalMessage)" in
    *"Should I"*) ok "question: question surfaced" ;;
    *) fail "question: question missing"; return ;;
  esac
  "$PYTHON" "$BRIDGE" send --state-file "$sf" \
    --prompt "Extend the existing view." > "$WORK/t2.send.json" 2>&1 \
    || { fail "question: send exited $?"; return; }
  [ "$(json_field "$sf" status)" = "busy" ] || fail "question: state not busy after send"
  watch "$WORK/t2.watch2.json" "$sf" || { fail "question: second watch exited $?"; return; }
  case "$(json_field "$WORK/t2.watch2.json" sessions 0 finalMessage)" in
    *"CREW COMPLETE"*) ok "question: follow-up completed with receipt" ;;
    *) fail "question: follow-up receipt missing" ;;
  esac
}

# --- Test 22: a send opening with a skill passes its installed skill input item ---
test_send_skill_input() {
  local dir; dir=$(make_child t22 question)
  local sf="$WORK/t22.state.json" out="$WORK/t22.launch.json"
  launch "$dir" "$sf" 24 "$out" || { fail "send-skill: launch exited $?"; return; }
  watch "$WORK/t22.watch.json" "$sf" || { fail "send-skill: watch exited $?"; return; }
  "$PYTHON" "$BRIDGE" send --state-file "$sf" \
    --prompt '$implement /tmp/ticket.md' > "$WORK/t22.send.json" 2>&1 \
    || { fail "send-skill: send exited $?"; return; }
  "$PYTHON" - "$dir/stub-requests.jsonl" \
      "$CACHE_SKILL_PATH" <<'PY' \
    && ok "send-skill: installed skill input posted" \
    || fail "send-skill: installed skill input missing"
import json
import pathlib
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    requests = [json.loads(line) for line in stream if line.strip()]
turn = [request for request in requests if request["method"] == "turn/start"][-1]
skill = turn["params"]["input"][1]
assert skill["type"] == "skill", turn
assert skill["name"] == "implement", turn
assert pathlib.Path(skill["path"]).samefile(sys.argv[2]), turn
PY
}

# --- Test 23: a launch opening with a skill passes a linked mention to the TUI ---
test_launch_skill_input() {
  local dir; dir=$(make_child t23 receipt)
  local sf="$WORK/t23.state.json" out="$WORK/t23.launch.json"
  "$PYTHON" "$BRIDGE" launch --cwd "$dir" --tmux-session 'bt:' \
    --window-name 25 --state-file "$sf" --startup-timeout 15 \
    --prompt '$implement /tmp/ticket.md' > "$out" 2> "$out.err" \
    || { fail "launch-skill: launch exited $? ($(cat "$out.err"))"; return; }
  wait_for_stub_argv "$dir" 2 \
    || { fail "launch-skill: TUI invocation was not recorded"; return; }
  "$PYTHON" - "$dir/stub-argv.jsonl" "$CACHE_SKILL_PATH" <<'PY' \
    && ok "launch-skill: linked mention passed in the TUI prompt" \
    || fail "launch-skill: linked mention missing from the TUI prompt"
import json
import pathlib
import re
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    invocations = [json.loads(line) for line in stream if line.strip()]
tui = invocations[-1]
prompt = tui[-1]
marker, message = prompt.split("\n", 1)
assert re.fullmatch(r"\[agentcrew:[^]]+\]", marker), prompt
path = pathlib.Path(sys.argv[2]).resolve()
assert message == f"[$implement]({path}) /tmp/ticket.md", prompt
PY
}

# --- Test 27: an absent versioned cache links the plugin source path in the TUI prompt ---
test_skill_source_fallback() {
  local dir; dir=$(make_child t27 receipt)
  local sf="$WORK/t27.state.json" out="$WORK/t27.launch.json" launch_status
  tmux set-environment -t bt CODEX_STUB_PLUGIN_VERSION 9.9.9
  CODEX_STUB_PLUGIN_VERSION=9.9.9 "$PYTHON" "$BRIDGE" launch \
    --cwd "$dir" --tmux-session 'bt:' --window-name 29 \
    --state-file "$sf" --startup-timeout 15 \
    --prompt '$implement /tmp/ticket.md' > "$out" 2> "$out.err"
  launch_status=$?
  tmux set-environment -u -t bt CODEX_STUB_PLUGIN_VERSION
  [ "$launch_status" -eq 0 ] \
    || { fail "skill-source: launch exited $launch_status ($(cat "$out.err"))"; return; }
  wait_for_stub_argv "$dir" 2 \
    || { fail "skill-source: TUI invocation was not recorded"; return; }
  "$PYTHON" - "$dir/stub-argv.jsonl" "$SKILL_PATH" <<'PY' \
    && ok "skill-source: absent cache linked the source path" \
    || fail "skill-source: source path link missing"
import json
import pathlib
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    invocations = [json.loads(line) for line in stream if line.strip()]
prompt = invocations[-1][-1]
_marker, message = prompt.split("\n", 1)
path = pathlib.Path(sys.argv[2]).resolve()
assert message == f"[$implement]({path}) /tmp/ticket.md", prompt
PY
}

# --- Test 29: skills/list may spell the same SKILL.md through path aliases ---
test_skill_path_alias() {
  local dir; dir=$(make_child t29 skill-path-alias)
  local sf="$WORK/t29.state.json" out="$WORK/t29.launch.json"
  "$PYTHON" "$BRIDGE" launch --cwd "$dir" --tmux-session 'bt:' \
    --window-name 32 --state-file "$sf" --startup-timeout 15 \
    --prompt '$implement /tmp/ticket.md' > "$out" 2> "$out.err" \
    && ok "skill-path-alias: equivalent real path accepted" \
    || fail "skill-path-alias: equivalent real path rejected ($(cat "$out.err"))"
}

# --- Test 24: a prompt without a skill mention passes unchanged in the TUI argv ---
test_plain_prompt_has_no_skill_input() {
  local dir; dir=$(make_child t24 question)
  local sf="$WORK/t24.state.json" out="$WORK/t24.launch.json"
  "$PYTHON" "$BRIDGE" launch --cwd "$dir" --tmux-session 'bt:' \
    --window-name 26 --state-file "$sf" --startup-timeout 15 \
    --prompt $'plain prompt\nline two' > "$out" 2> "$out.err" \
    || { fail "plain-input: launch exited $? ($(cat "$out.err"))"; return; }
  wait_for_stub_argv "$dir" 2 \
    || { fail "plain-input: TUI invocation was not recorded"; return; }
  "$PYTHON" - "$dir/stub-argv.jsonl" "$dir/stub-requests.jsonl" <<'PY' \
    && ok "plain-input: prompt passed unchanged without a skill query" \
    || fail "plain-input: prompt or app-server requests changed"
import json
import re
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    invocations = [json.loads(line) for line in stream if line.strip()]
prompt = invocations[-1][-1]
marker, message = prompt.split("\n", 1)
assert re.fullmatch(r"\[agentcrew:[^]]+\]", marker), prompt
assert message == "plain prompt\nline two", prompt
with open(sys.argv[2], encoding="utf-8") as stream:
    requests = [json.loads(line) for line in stream if line.strip()]
assert not [request for request in requests if request["method"] == "skills/list"], requests
PY
}

# --- Test 25: a named skill without an installed SKILL.md is reported ---
test_missing_skill_path_is_reported() {
  local dir; dir=$(make_child t25 question)
  local sf="$WORK/t25.state.json" out="$WORK/t25.launch.json"
  launch "$dir" "$sf" 27 "$out" || { fail "missing-skill: launch exited $?"; return; }
  watch "$WORK/t25.watch.json" "$sf" || { fail "missing-skill: watch exited $?"; return; }
  if "$PYTHON" "$BRIDGE" send --state-file "$sf" \
      --prompt '$missing /tmp/ticket.md' > "$WORK/t25.send.json" 2> "$WORK/t25.send.err"; then
    fail "missing-skill: send unexpectedly succeeded"
    return
  fi
  grep -q "SKILL.md" "$WORK/t25.send.err" \
    && ok "missing-skill: missing path reported" \
    || fail "missing-skill: missing path was silently dropped"
}

# --- Test 26: a launch fails when skills/list cannot resolve its linked mention ---
test_launch_unresolved_skill_is_reported() {
  local dir; dir=$(make_child t26 skill-unresolved)
  local sf="$WORK/t26.state.json" out="$WORK/t26.launch.json"
  if "$PYTHON" "$BRIDGE" launch --cwd "$dir" --tmux-session 'bt:' \
      --window-name 28 --state-file "$sf" --startup-timeout 15 \
      --prompt '$implement /tmp/ticket.md' > "$out" 2> "$out.err"; then
    fail "launch-unresolved-skill: launch unexpectedly succeeded"
    return
  fi
  grep -q "exactly one enabled skill" "$out.err" \
    || { fail "launch-unresolved-skill: clear failure detail missing ($(cat "$out.err"))"; return; }
  tmux list-windows -t bt -F '#{window_name}' | grep -qx 28 \
    && { fail "launch-unresolved-skill: failed window survived"; return; }
  "$PYTHON" - "$dir/stub-requests.jsonl" "$dir" <<'PY' \
    && ok "launch-unresolved-skill: exact child-cwd assertion failed closed" \
    || fail "launch-unresolved-skill: skills/list request was wrong"
import json
import pathlib
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    requests = [json.loads(line) for line in stream if line.strip()]
skills = [request for request in requests if request["method"] == "skills/list"]
assert len(skills) == 1, skills
cwd = str(pathlib.Path(sys.argv[2]).resolve())
assert skills[0]["params"] == {"cwds": [cwd], "forceReload": True}, skills
PY
}

# --- Test 28: a relaunch may return before its opening-skill assertion fails ---
test_relaunch_late_skill_failure_is_reported() {
  local dir; dir=$(make_child t28 receipt)
  local sf="$WORK/t28.state.json" out="$WORK/t28.launch.json" launch_pid
  "$PYTHON" "$BRIDGE" launch --cwd "$dir" --tmux-session 'bt:' \
    --window-name 30 --state-file "$sf" --startup-timeout 15 \
    --prompt '$implement /tmp/ticket.md' > "$out" 2> "$out.err" \
    || { fail "relaunch-unresolved: initial launch exited $? ($(cat "$out.err"))"; return; }
  local thread_id; thread_id=$(json_field "$sf" threadId)
  local window_id; window_id=$(json_field "$sf" windowId)
  tmux kill-window -t "$window_id" \
    || { fail "relaunch-late-failure: could not stop initial window"; return; }
  sleep 0.2
  printf '%s\n' skill-unresolved-after-launch > "$dir/.codex-stub-scenario"
  "$PYTHON" "$BRIDGE" launch --cwd "$dir" --tmux-session 'bt:' \
    --window-name 31 --state-file "$sf" --startup-timeout 15 \
    --thread-id "$thread_id" --prompt '$implement /tmp/ticket.md' \
    > "$WORK/t28.resume.json" 2> "$WORK/t28.resume.json.err" &
  launch_pid=$!
  if ! wait "$launch_pid"; then
    fail "relaunch-late-failure: relaunch did not return ok"
    return
  fi
  touch "$dir/stub-release-skill-check"
  watch "$WORK/t28.watch.json" "$sf" \
    || { fail "relaunch-late-failure: watch exited $?"; return; }
  [ "$(json_field "$WORK/t28.watch.json" sessions 0 status)" = "vanished" ] \
    || { fail "relaunch-late-failure: failed pane was not vanished"; return; }
  local log_path; log_path="$(json_field "$sf" runtimeDir)/app-server.log"
  tail -n 1 "$log_path" | grep -q "^Codex opening skill assertion failed:.*exactly one enabled skill" \
    && ok "relaunch-late-failure: vanished pane kept its final-line reason" \
    || fail "relaunch-late-failure: final-line reason was not preserved"
}

# --- Test 30: a relaunch may observe the opening-skill failure directly ---
test_relaunch_early_skill_failure_is_reported() {
  local dir; dir=$(make_child t30 receipt)
  local sf="$WORK/t30.state.json" out="$WORK/t30.launch.json"
  "$PYTHON" "$BRIDGE" launch --cwd "$dir" --tmux-session 'bt:' \
    --window-name 33 --state-file "$sf" --startup-timeout 15 \
    --prompt '$implement /tmp/ticket.md' > "$out" 2> "$out.err" \
    || { fail "relaunch-early-failure: initial launch exited $?"; return; }
  local thread_id; thread_id=$(json_field "$sf" threadId)
  local window_id; window_id=$(json_field "$sf" windowId)
  tmux kill-window -t "$window_id" \
    || { fail "relaunch-early-failure: could not stop initial window"; return; }
  sleep 0.2
  printf '%s\n' skill-unresolved-before-launch > "$dir/.codex-stub-scenario"
  if CODEX_STUB_DELAY_NEW_WINDOW_RETURN=1 TMPDIR="$RELAUNCH_EARLY_RUNTIME_ROOT" \
      "$PYTHON" "$BRIDGE" launch --cwd "$dir" --tmux-session 'bt:' \
      --window-name 34 --state-file "$sf" --startup-timeout 15 \
      --thread-id "$thread_id" --prompt '$implement /tmp/ticket.md' \
      > "$WORK/t30.resume.json" 2> "$WORK/t30.resume.json.err"; then
    fail "relaunch-early-failure: relaunch unexpectedly returned ok"
    return
  fi
  local log_path reason
  log_path=$(find "$RELAUNCH_EARLY_RUNTIME_ROOT" -name app-server.log -type f -print -quit)
  [ -n "$log_path" ] \
    || { fail "relaunch-early-failure: failed runtime log was not retained"; return; }
  reason=$(tail -n 1 "$log_path")
  printf '%s\n' "$reason" | grep -q \
    "^Codex opening skill assertion failed:.*exactly one enabled skill" \
    || { fail "relaunch-early-failure: final-line reason was not preserved"; return; }
  grep -Fq "$reason" "$WORK/t30.resume.json.err" \
    && ok "relaunch-early-failure: launch and retained log share the reason" \
    || fail "relaunch-early-failure: launch stderr lost the retained reason"
}

# --- Test 3: watch stays armed while all busy, wakes on first idle child ---
test_wave_wakeup() {
  local d1 d2; d1=$(make_child t3a slow); d2=$(make_child t3b slow)
  local s1="$WORK/t3a.state.json" s2="$WORK/t3b.state.json"
  launch "$d1" "$s1" 03 "$WORK/t3a.launch.json" || { fail "wave: launch a exited $?"; return; }
  launch "$d2" "$s2" 04 "$WORK/t3b.launch.json" || { fail "wave: launch b exited $?"; return; }
  watch "$WORK/t3.watch.json" "$s1" "$s2" &
  local watch_pid=$!
  sleep 2
  kill -0 "$watch_pid" 2>/dev/null || { fail "wave: watch exited while all busy"; return; }
  touch "$d1/stub-release"
  wait "$watch_pid" || { fail "wave: watch exited $?"; return; }
  [ "$(json_field "$WORK/t3.watch.json" sessions 0 status)" = "idle" ] || fail "wave: released child not idle"
  [ "$(json_field "$WORK/t3.watch.json" sessions 1 status)" = "busy" ] || fail "wave: held child not busy"
  ok "wave: one-shot wake-up on first idle child"
}

# --- Test 4: a killed window is reported vanished ---
test_vanished() {
  local dir; dir=$(make_child t4 slow)
  local sf="$WORK/t4.state.json"
  launch "$dir" "$sf" 05 "$WORK/t4.launch.json" || { fail "vanished: launch exited $?"; return; }
  tmux kill-window -t "$(json_field "$sf" windowId)"
  watch "$WORK/t4.watch.json" "$sf" || { fail "vanished: watch exited $?"; return; }
  [ "$(json_field "$WORK/t4.watch.json" sessions 0 status)" = "vanished" ] \
    && ok "vanished: killed window detected" || fail "vanished: status wrong"
}

# --- Test 22: failed pane observations are retried without reporting vanished ---
test_transient_pane_read_failures() {
  local dir; dir=$(make_child t22 slow)
  local sf="$WORK/t22.state.json" snap="$WORK/t22.watch.json"
  launch "$dir" "$sf" 22 "$WORK/t22.launch.json" \
    || { fail "pane-retry: launch exited $?"; return; }
  fail_next_pane_reads 2
  watch "$snap" "$sf" &
  local watch_pid=$!
  wait_for_failed_pane_reads 2 \
    || { fail "pane-retry: watch did not retry failed pane reads"
         kill "$watch_pid" 2>/dev/null
         wait "$watch_pid" 2>/dev/null
         return; }
  touch "$dir/stub-release"
  wait "$watch_pid" \
    || { fail "pane-retry: watch exited $? ($(cat "$snap.err"))"; return; }
  [ "$(json_field "$snap" sessions 0 status)" = "idle" ] \
    || fail "pane-retry: recovered child was not reported idle"
  grep -q '"vanished"' "$snap" \
    && fail "pane-retry: a failed observation reported vanished" \
    || ok "pane-retry: failed observations said nothing"
}

# --- Test 23: the retry limit makes a failed pane source a bridge error ---
test_pane_read_failure_limit() {
  local dir; dir=$(make_child t23 slow)
  local sf="$WORK/t23.state.json" snap="$WORK/t23.watch.json"
  launch "$dir" "$sf" 23 "$WORK/t23.launch.json" \
    || { fail "pane-limit: launch exited $?"; return; }
  fail_next_pane_reads 3
  if watch "$snap" "$sf"; then
    fail "pane-limit: watch accepted three failed pane reads"
    return
  fi
  grep -q "unreachable but its window is alive" "$snap.err" \
    && ok "pane-limit: watch surfaced a bridge error" \
    || fail "pane-limit: wrong error ($(cat "$snap.err"))"
  grep -q '"vanished"' "$snap" \
    && fail "pane-limit: a failed observation reported vanished"
}

# --- Test 5: a failed turn surfaces as idle with turnStatus failed ---
test_failed_turn() {
  local dir; dir=$(make_child t5 failed-turn)
  local sf="$WORK/t5.state.json"
  launch "$dir" "$sf" 06 "$WORK/t5.launch.json" || { fail "failed-turn: launch exited $?"; return; }
  watch "$WORK/t5.watch.json" "$sf" || { fail "failed-turn: watch exited $?"; return; }
  [ "$(json_field "$WORK/t5.watch.json" sessions 0 status)" = "idle" ] || fail "failed-turn: not idle"
  [ "$(json_field "$WORK/t5.watch.json" sessions 0 turnStatus)" = "failed" ] \
    && ok "failed-turn: failure surfaced" || fail "failed-turn: turnStatus wrong"
}

# --- Test 6: TUI that dies during startup fails the launch ---
test_tui_exit() {
  local dir; dir=$(make_child t6 tui-exit)
  local runtime_root="$TUI_EXIT_RUNTIME_ROOT" out="$WORK/t6.launch.json" log_path
  if TMPDIR="$runtime_root" launch "$dir" "$WORK/t6.state.json" 07 "$out"; then
    fail "tui-exit: launch unexpectedly succeeded"
  else
    ok "tui-exit: launch failed as expected"
  fi
  [ -f "$WORK/t6.state.json" ] && fail "tui-exit: state file written on failure"
  log_path=$(find "$runtime_root" -name app-server.log -type f -print -quit)
  if [ -z "$log_path" ]; then
    fail "tui-exit: app-server.log was removed"
  elif grep -q "Codex TUI exited before creating its thread" "$log_path"; then
    ok "tui-exit: startup failure log was preserved"
  else
    fail "tui-exit: preserved log lost the startup failure detail ($(cat "$log_path"))"
  fi
  grep -q "Codex TUI exited before creating its thread" "$out.err" \
    && ok "tui-exit: launch reported the pane's startup failure" \
    || fail "tui-exit: launch stderr lost the pane's startup failure ($(cat "$out.err"))"
}

# --- Test 7: app-server that never opens its socket fails the launch ---
test_no_server() {
  local dir; dir=$(make_child t7 no-server)
  if "$PYTHON" "$BRIDGE" launch --cwd "$dir" --tmux-session 'bt:' \
      --window-name 08 --state-file "$WORK/t7.state.json" --startup-timeout 3 \
      --prompt x > "$WORK/t7.launch.json" 2>&1; then
    fail "no-server: launch unexpectedly succeeded"
  else
    ok "no-server: launch failed as expected"
  fi
}

# --- Test 8: duplicate state files are rejected ---
test_duplicates() {
  local dir; dir=$(make_child t8 receipt)
  local sf="$WORK/t8.state.json"
  launch "$dir" "$sf" 09 "$WORK/t8.launch.json" || { fail "duplicates: launch exited $?"; return; }
  if watch "$WORK/t8.watch.json" "$sf" "$sf"; then
    fail "duplicates: watch accepted duplicate state files"
  else
    ok "duplicates: rejected"
  fi
}

# --- Test 9: watch times out with every session busy ---
test_watch_timeout() {
  local dir; dir=$(make_child t9 slow)
  local sf="$WORK/t9.state.json"
  launch "$dir" "$sf" 10 "$WORK/t9.launch.json" || { fail "timeout: launch exited $?"; return; }
  if "$PYTHON" "$BRIDGE" watch --interval 0.3 --timeout 2 "$sf" \
      > "$WORK/t9.watch.json" 2>"$WORK/t9.watch.err"; then
    fail "timeout: watch unexpectedly succeeded"
  else
    grep -q "timed out" "$WORK/t9.watch.err" \
      && ok "timeout: reported" || fail "timeout: wrong error"
  fi
}

# --- Test 10: stop kills the window and removes the runtime ---
test_stop() {
  local dir; dir=$(make_child t10 receipt)
  local sf="$WORK/t10.state.json"
  launch "$dir" "$sf" 11 "$WORK/t10.launch.json" || { fail "stop: launch exited $?"; return; }
  local runtime; runtime=$(json_field "$sf" runtimeDir)
  local window; window=$(json_field "$sf" windowId)
  "$PYTHON" "$BRIDGE" stop --state-file "$sf" > "$WORK/t10.stop.json" 2>&1 \
    || { fail "stop: exited $?"; return; }
  sleep 0.5
  tmux list-windows -a -F '#{window_id}' | grep -qx "$window" \
    && fail "stop: window still alive"
  [ -d "$runtime" ] && fail "stop: runtime dir still present"
  [ "$(json_field "$sf" status)" = "stopped" ] \
    && ok "stop: window and runtime cleared" || fail "stop: state not stopped"
}

test_receipt
test_escalation_logging
test_ruling_logging
test_resume_keeps_logging_configuration
test_unmarked_turn_is_watched
test_missed_edge_is_still_logged_once
test_unrecorded_turn_survives_a_later_turn
test_model_effort_overrides
test_without_model_effort_overrides
test_resume_keeps_pinned_model_effort
test_model_only_pin_and_resume
test_unusable_resume_state
test_question_send
test_send_skill_input
test_launch_skill_input
test_plain_prompt_has_no_skill_input
test_missing_skill_path_is_reported
test_launch_unresolved_skill_is_reported
test_relaunch_late_skill_failure_is_reported
test_relaunch_early_skill_failure_is_reported
test_skill_source_fallback
test_skill_path_alias
test_wave_wakeup
test_vanished
test_transient_pane_read_failures
test_pane_read_failure_limit
test_failed_turn
test_tui_exit
test_no_server
test_duplicates
test_watch_timeout
test_stop

if [ "$FAILURES" -gt 0 ]; then
  echo "$FAILURES test(s) failed"
  exit 1
fi
echo "all tests passed"
