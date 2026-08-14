---
status: accepted
---

# The pinned dashboard is drawn into Claude Code's statusline, not into tmux's status lines

Watching a crew run means leaving the run. The dashboard lives in its own tmux window
([`docs/monitor-dashboard.md`](../monitor-dashboard.md)), so the operator sitting in the
coordinator's Claude Code session switches windows to see where the run is and switches back to
answer an escalation — and the two things they need at once, the frame and the coordinator's
prompt, can never be on screen together. A window is also a thing a human closes: a run that ended
two hours ago should not leave one sitting there waiting to be tidied up.

Three requirements decided the surface, and every candidate was measured against all three:
no window, pane or popup of its own; the coordinator's pane keeps its full size; and the lifecycle
is automatic, so the surface appears when a run starts, disappears when it ends, and leaves no
residue when a run is killed. The measurements are in
[`docs/dashboard-pinning-research.md`](../dashboard-pinning-research.md), taken against Claude Code
2.1.232 and tmux 3.6a.

## Considered Options

- **Claude Code's `statusLine`.** A region that already exists: `statusLine` runs a shell command
  and draws what it prints, and `refreshInterval` re-runs that command every N seconds whether or
  not anyone is typing. Measured, it never resizes the coordinator's pane (190×45 before, during
  and after); it has no line cap that could be found (12 printed lines drew as 12 rows); it takes
  raw ANSI, so the renderer's existing colour works unchanged; and it draws its input as bytes with
  no expansion, so a ticket title needs no escaping. Its honest costs: the region is
  bottom-anchored, and the frame spends rows of visible transcript while a run is live. Chosen.
- **tmux's extra status lines** (`status 2..5`, `status-format[N]`, `status-position top`). The
  expected front-runner, and it does give top placement and a self-healing teardown. It loses on
  three measured counts: a hard six-row ceiling (`status 6` answers `unknown value: 6`) against a
  table that grows a row per ticket and per annotation; it shrinks the coordinator's pane by five
  rows (190×49 → 190×44), which redraws Claude Code twice per run; and `#()` re-expands its own
  output, so a printed `#(id -un)` was *executed* — ticket titles come from GitHub issues, which
  makes that a shell-injection hazard carried on attacker-influenced text. Rejected.
- **`display-popup` and tmux 3.7 floating panes.** Both draw over a pane without costing it rows,
  and both are things a human closes — requirement 1 and requirement 3. The popup is modal as well:
  measured, keystrokes landed inside it and the pane behind stopped repainting, so the operator
  could not type to the coordinator. Rejected.
- **Terminal-emulator status bars** (iTerm2, WezTerm). One row each against an unbounded table, and
  they would tie the dashboard to one terminal. Rejected.
- **`subagentStatusLine`.** A near-miss with a perfect lifecycle — Claude Code creates and destroys
  one row per subagent — but crew children are separate sessions rather than the coordinator's
  subagents, so the agent panel is empty and there are no rows to override. Rejected; worth
  revisiting only if the dispatch model changes.

## Decision

The pinned dashboard is drawn into Claude Code's `statusLine`, and the statusline's own tick is the
refresh loop: each tick renders one frame on demand. There is no background process and no frame
file.

The run's only trace is a **pin** — a small JSON file naming the live run, written at dispatch and
removed when the run ends. The frame is a function of the pin, and the pin is checked against the
coordinator's own process, so a crashed or killed run takes the frame down with it. That is the
whole crash story: no watchdog, no heartbeat, no separate liveness file. The registry the pin lives
in, what the pin file carries, how one is selected, and every condition under which nothing is
drawn are the pin's contract, stated in
[`docs/monitor-dashboard.md`](../monitor-dashboard.md#the-pin).

[ADR-0001](0001-coordinator-spends-tokens-only-on-judgment.md) holds unchanged: the frame is a
rendered display region. It is never added to the coordinator's context, costs no model tokens, and
makes no API call.

## Consequences

- The operator reads the run and answers an escalation without choosing between them, and has
  nothing to close when the run is done.
- The frame is anchored at the bottom of the session. Claude Code's statusline region exposes no
  position setting, so the original ask — pinned at the top — is not available and is not pursued.
- Turning the pin on costs `refreshInterval` in `settings.json`, set once at install. The
  operator's existing statusline script measured ~80 ms per run, dominated by `git status
  --porcelain`; at a two-second interval that is roughly 4% of one core, continuously, per session.
  The interval is the dial.
- The renderer caps itself against `LINES`, because rows past the bottom of the terminal are lost
  silently rather than scrolled to.
- A statusline that spews diagnostics across the operator's prompt is worse than one that goes
  quiet, so the pin prints nothing and exits 0 where every other subcommand would report an error.
- `monitor.py window` is unchanged, undeprecated, and stays the default surface. An operator who is
  not in Claude Code loses nothing, and upgrading agentcrew changes nobody's run.
- The pin registry is local to the machine the coordinator runs on. Watching a run on another
  machine is not addressed here.
