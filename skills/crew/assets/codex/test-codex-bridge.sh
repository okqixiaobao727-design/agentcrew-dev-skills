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

WORK=$(mktemp -d -t codex-bridge-test)
BIN="$WORK/bin"
mkdir -p "$BIN"

printf '#!/bin/sh\nexec "%s" -L codex-bridge-test "$@"\n' "$REAL_TMUX" > "$BIN/tmux"
printf '#!/bin/sh\nexec "%s" "%s" "$@"\n' "$PYTHON" "$STUB" > "$BIN/codex"
chmod +x "$BIN/tmux" "$BIN/codex"
export PATH="$BIN:$PATH"

cleanup() {
  tmux kill-server 2>/dev/null
  rm -rf "$WORK"
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

# --- Test 11: pinned model and effort reach both Codex argv lists and state ---
test_model_effort_overrides() {
  local dir; dir=$(make_child t11 receipt)
  local sf="$WORK/t11.state.json" out="$WORK/t11.launch.json"
  launch_with_options "$dir" "$sf" 12 "$out" \
    --model "$PINNED_MODEL" --effort "$PINNED_EFFORT" \
    || { fail "pinned: launch exited $?"; return; }
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
    || { fail "resume: relaunch exited $?"; return; }
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
  printf '%s' "$state_content" > "$sf"
  launch_with_options "$dir" "$sf" "$window_name" "$out" \
    --thread-id stub-thread-1 \
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
  if launch "$dir" "$WORK/t6.state.json" 07 "$WORK/t6.launch.json"; then
    fail "tui-exit: launch unexpectedly succeeded"
  else
    ok "tui-exit: launch failed as expected"
  fi
  [ -f "$WORK/t6.state.json" ] && fail "tui-exit: state file written on failure"
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
test_model_effort_overrides
test_without_model_effort_overrides
test_resume_keeps_pinned_model_effort
test_model_only_pin_and_resume
test_unusable_resume_state
test_question_send
test_wave_wakeup
test_vanished
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
