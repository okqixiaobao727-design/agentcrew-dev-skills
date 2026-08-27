---
status: accepted
---

# The Codex pane reports one bootstrap result

Codex does not materialise a thread when `thread/start` returns.  The first user message creates
the rollout on disk; before that, `thread/resume` fails with `no rollout found for thread id` and
`thread/read` reports that the thread is not materialised.  There is no upstream switch that
persists an empty thread: `thread/start`'s `ephemeral` option points in the opposite direction,
while `thread/resume`'s experimental `path` has no rollout file to name before the first turn.

v0.9.7 moved the structured first turn out of the TUI command line so it could carry a first-class
skill input item (#150).  Its bootstrap order became `thread/start`, TUI `thread/resume`, then
`turn/start`.  Every Codex launch therefore failed before wave 1: the TUI could not resume the
turn-less thread, its exit killed the pane's app-server, and the outer launch saw a closed
WebSocket instead of an explanation (#155).

Reordering the turn alone exposed a second problem.  The outer `launch` process used a marker in
`thread/list` preview to discover the thread and treated a still-existing pane as confirmation.
Once the turn precedes the TUI, that marker is visible before the TUI has started or survived its
startup check.  The outer process could report success while the pane was still proving that the
TUI could attach.  Marker visibility, pane liveness, thread identity and bootstrap failure were
separate observations of one fact with no owner.

## Decision

The pane owns the whole Codex bootstrap:

1. start the app-server;
2. `thread/start`, or `thread/resume` for an explicit thread id;
3. `turn/start` with the text and any resolved skill input;
4. poll `thread/read` until that turn's marker is materialised;
5. start the TUI with `resume <threadId>` and perform a bounded startup liveness check; and
6. atomically write `<runtime-dir>/bootstrap-result.json`.

The result is the pane's only completion receipt to the outer `launch`.  Success carries
`{"ok": true, "threadId": "..."}`.  Failure carries `{"ok": false, "error": "...",
"logPath": "..."}`.  The outer process waits within the existing startup timeout, reads that
result, and only then writes the existing state file and prints the existing public JSON.  If the
pane exits without a result, the outer process reports the preserved app-server log instead.

The launch path no longer calls `thread/list` or discovers a thread from its preview.  Its marker
has one local purpose: prove that the prepared thread contains the first turn the pane submitted.
The state schema and the markers carried by later turns remain compatible with existing callers.

The Codex stub models the app-server, not the bridge.  A turn-less thread rejects
`thread/resume` and `thread/read`, and the stub TUI performs its real protocol request so tests can
prove that `thread/start`, `turn/start`, and TUI `thread/resume` occur in that order before launch
reports success.

## Consequences

- Thread identity, bootstrap completion and bootstrap failure have one owner and one atomic
  handoff instead of independent marker, liveness and log-tail guesses.
- A TUI that dies during startup cannot race a successful launch response.  Its runtime directory
  and app-server log remain available, while successful sessions remove their runtime on stop as
  before.
- Relaunch still inherits model, effort and machine-log pins before the pane starts, and the pane
  resumes the requested thread before posting its new launch turn.
- Driver, dispatch, machine-log, state-file and public bridge command contracts do not change.
