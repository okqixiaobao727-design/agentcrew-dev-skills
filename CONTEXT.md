# AgentCrew

A Claude Code plugin that routes spec tickets (`/route`) and runs them unattended
(`/crew`) as parallel child agent sessions, coordinated by one expensive-model session
whose only job is judgment.

## Language

**Coordinator**:
The single expensive-model session that rules on escalations, approves the opening plan,
and lands nothing by hand. It spends model tokens only on judgment.
_Avoid_: lead, orchestrator (legacy term from `/orchestrate`)

**Child**:
An agent session (Claude or Codex) that implements exactly one ticket in its own worktree.
_Avoid_: worker, teammate

**Ruling**:
A judgment the coordinator issues in reply to an escalation — design direction, a conflict
verdict, or a scope decision.
_Avoid_: decision (overloaded), answer

**Escalation**:
A child's or script's request for a ruling. The only event that is allowed to cost the
coordinator a turn mid-wave.
_Avoid_: question, ask (as a noun)

**Escalation ladder**:
The fixed order in which a mechanical failure is retried: script → budget-capped headless
Sonnet repair → coordinator. Only a double failure or a semantic conflict reaches the top.

**Exception handler**:
The budget-capped headless cheap-model session a script launches to fix a mechanical
failure. It is not resident and the coordinator does not know it ran unless it fails.
_Avoid_: executive, dispatcher agent

**Wave**:
The set of tickets whose blockers are all resolved, launched together.

**Wave table**:
The routing table (ticket → workflow, executor, model, effort, review lane) the user
approves before the run. After approval it is the sole routing authority; a ticket's
`## Routing` section is advisory input to it.

**Receipt**:
A child's structured completion/failure/parked message, verified by script rules, never
by coordinator judgment.

**Machine log**:
The append-only event log written entirely by scripts and hooks — launches, receipts,
merges, escalations, and rulings copied in verbatim. Its audience is future agents, not
the human.
_Avoid_: decision log (legacy), status report

**Dashboard**:
The script-rendered human view of a run: a live table pane plus milestone and exception
toasts. The human watches this instead of coordinator prose.

**Reference index**:
The static list of file paths (one descriptive line each, no contents) placed in the
coordinator's opening context so a ruling never starts with a hunt. Contents are read on
demand and never inserted into the live context by anything but the coordinator itself.

**Judgment turn**:
Any coordinator turn that produces a ruling or an approval. The design goal is that a
run contains no other kind of coordinator turn.
