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

**Ticket state**:
The single human-facing vocabulary for where a ticket stands, shared by the dashboard,
toasts, and the final report: pending, running, waiting, parked, landable, merged,
failed, vanished — plus duplicate and unknown as anomaly annotations. Source
vocabularies (tmux process states, monitor internals, settlement verdicts) are mapped
into it before anything reaches the human.
_Avoid_: raw source states (busy, idle) in human-facing output

**Dashboard**:
The script-rendered human view of a run: a live table plus milestone and exception
toasts, exactly one per run. The human watches this instead of coordinator prose.

**Pin**:
The small JSON file a live run leaves in the pin registry, written at dispatch and removed
when the run ends. It names the run (directory, coordinator pid, tmux session) and what
draws it — the writing release's own `monitor.py` and interpreter. Those last two are
recorded here, by a release alive at that moment, and never at install time, which is what
keeps the statusline wrapper a permanent stub no upgrade can strand (ADR-0011).
_Avoid_: frame file, liveness file (there is no background process behind it)

**Reference index**:
The static list of file paths (one descriptive line each, no contents) placed in the
coordinator's opening context so a ruling never starts with a hunt. Contents are read on
demand and never inserted into the live context by anything but the coordinator itself.

**Judgment turn**:
Any coordinator turn that produces a ruling or an approval. The design goal is that a
run contains no other kind of coordinator turn.

**Judgment core**:
The judgment material every run consults — the classification tests, checkpoint rules,
and completion criteria. It lives in one reference file that every branch force-reads at
its point of use (ADR-0006): the mandatory Read step is what makes applying it reliable,
where a weakly-worded pointer would make it a coin-flip.
_Avoid_: essential prose, main content

**Frontmatter pin**:
A `model`/`effort` value fixed in a skill's frontmatter, model always as a full ID, so
the session that makes routing decisions never depends on what the environment happens
to resolve (ADR-0005).

**Full model ID**:
The only form a model name takes anywhere in the chain — config cell, `## Routing` line,
launch command — passed verbatim end to end with no alias-resolution layer. Aliases
mis-resolve under plan mode (ADR-0003).
_Avoid_: alias, short name

**Review recovery**:
Re-attaching to the review a child already has running, keyed on the owner tuple the review
bridge stores — tmux server, origin pane, worktree root. The reviewing session outlives the
driver process that launched it, so a lost handle is recovered rather than replaced; starting
a second review of one diff is the failure recovery exists to prevent.
_Avoid_: retry, restart

**Vendored Copy**:
A file this repo ships but does not own — today only the review bridge, pinned to one
Review-Switch commit by `scripts/sync-bridge.sh` and held to that pin by CI. A change to it
is made upstream and arrives by moving the pin, never by editing the copy (ADR-0009).
_Avoid_: fork, local copy (both imply it may be edited here)

**Mode-gated reference**:
A reference file only one mode of a skill loads, via an explicit Read instruction in the
body — e.g. the to-tickets+route handoff that route-only `/route` runs never see. Disclosure
earns its cost only when some branch skips the material.
