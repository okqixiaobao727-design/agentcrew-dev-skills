---
status: accepted
---

# A finding is queued into the Run and diagnosed by the child that implements it

Six consecutive Runs (crewtask/66–71) each finished with four or five new tickets the coordinator
had opened while ruling — almost all of them from `doc-conflict` and `scope` rulings, one from a
`wrap-up` — and every one of them needed a fresh `/route` and `/crew` to be worked. A feature
therefore took a chain of Runs, each paying the coordinator's start-up and the operator's
attention again, for work the Run had already discovered and could describe with pointers. The
machine logs show the cause: the placement grammar offered *this ticket*, *opened*, *deferred* and
*dropped* with no order of preference, and *opened* — the one placement that leaves the Run — was
the natural reading of "outside the ticket's scope".

## Decision

**Scope does not place a finding; the kind of work does.** A finding that is an *edit* — cause and
change site already known — stays with the child in front of the coordinator, whatever its scope.
A finding that is a *diagnosis* — cause, approach or reach still open — is *queued*: the
coordinator opens the ticket through one Driver command, and the Driver inserts it into the
current Run as the **next Wave to launch** — one Wave per queued ticket, in queue order, directly
behind the Wave the Run is working. The Waves that were still pending shift behind it and are
renumbered, so they build on the code the diagnosis merged rather than on the shape the finding
says is wrong; a finding queued when there is nothing pending is the trailing Wave this decision
first described. The anchor is the last Wave the machine log says *launched*, never the source
ticket's: Wave numbers are how `advance` records name their Wave and how the advance idempotence
check decides whether a Wave already launched, so anchoring there renumbers only Waves that have
never appeared in the log and no recorded number changes meaning. The Run plan remains the sole
routing authority; it gains a placement operation that takes that anchor — which Wave launched
last is log knowledge, not table knowledge — and is reloaded before each advance
([ADR-0018](0018-run-plan-owns-wave-table-meaning-callers-own-execution.md) made it reloadable
for exactly this kind of change). *Opened* remains for work outside the feature altogether.

**A question is answered; a finding is placed.** Every escalation still receives the ruling it
receives today — an option picked, a redirect, pseudocode — and *queued* is a placement for what
that ruling cannot direct, never an answer to the question itself. The queue command makes this
checkable: it refuses a finding that does not name what is open (`--open cause|approach|reach`),
and the report shows that word beside the placement. A finding that shares a cause or an area
with a queued ticket not yet launched is *deferred* to it through the existing `defer` command, so
one diagnosing child covers them; the queue command prints the Run's pending queued tickets on
every call so the coordinator has them in front of it when it rules.

**The child that diagnoses is the child that implements.** A queued ticket's child receives the
whole protocol skeleton every child receives — coordinator identity, the five escalation kinds,
the receipt — and differs in two places: its opening line is `/triage` on the ticket, and the
skeleton's step 1 is a diagnosis step that is the adapter over `/triage` as shipped, with the
*codebase-design* skill named for the brief's approach. The child reads, verifies, writes the
agent brief to the ticket, then sends one `design` escalation carrying the brief's pointer and the
pick "implement per brief" — the skill's "wait for direction" and "grill if needed" moments, and
every question the triage would put to a maintainer, travel in that one message. Dispatch chooses
this variant from a `queued` fact the Run plan carries on the ticket, never from the machine log
([ADR-0024](0024-driver-activates-every-wave-through-one-path.md)). The witness checks the brief's pointers as it checks any escalation. The coordinator's
ruling is the **workflow's** own opening line (`/implement <ticket>` for a tdd ticket) — the
ticket's own opening line is the triage skill, and the ruling replaces it — delivered through
the Driver's `rule` command, which composes that line from the shapes library and carries the
coordinator's ruling body after it as one message, and the child implements from the context its
diagnosis built. That delivery is the same typed-into-the-composer path #127 already uses to type
`/crew <feature-dir>` into the coordinator's pane, and the Codex bridge's next-turn path with
`$implement`; a slash command typed into a composer is a user-typed invocation, which is what a
`disable-model-invocation` skill accepts. Routing for queued tickets comes from one `[queued]`
cell in the crew config file, overridable on the queue command; no classification session runs.

**The mention is the first characters of the message, and each executor fails silently in its own
shape past that edge.** #182 ran both live
([`docs/research/second-turn-slash-command-expansion.md`](../research/second-turn-slash-command-expansion.md)):
on Claude a preamble ahead of the slash downgrades the whole ruling to prose, with no error and
nothing the delivery check can see; on Codex position is irrelevant but the name form is not — a
bare `$implement` after a preamble injects nothing while the message still arrives, and the
canonical `$mattpocock-skills:implement` is refused at the bridge before it is sent. So a ruling
that starts a diagnosing child carries the opening line and nothing before it — and the
correction travels **inside** that same message, after the line, which #182 also showed is
carried into the skill's own `<command-args>`.

*Amended by #195.* The correction was first sent as an ordinary answer ahead of the opening line,
which put the two on channels with different arrival semantics: an injected cross-session message
reaches a Claude child mid-turn, a typed composer line only at a turn boundary. Sending the
mid-turn one first is what made the child busy, so the line behind it sat in Claude Code's queue —
five minutes on `crewtask/17` #218, thirty-eight on #217, both recorded as delivered because the
line had left the composer. One message removes that failure rather than guarding against it, and
removes the silent downgrade with it: `rule` puts the opening line first by construction, so
nothing the coordinator writes can get ahead of the slash. The coordinator supplies the ruling
body alone; the line, its executor spelling and the ticket path are the Driver's.

**Three earlier decisions are amended, not replaced.** Tracker gains a `create` operation beside
the read, edit, mark, comment and close that
[ADR-0019](0019-tracker-owns-ticket-operations-callers-own-workflow.md) lists. A queued ticket's
routing is approved by the coordinator in the operator's stead — the `[queued]` cell is the
operator's standing approval, the command-line overrides are the coordinator's — as
[ADR-0010](0010-the-driver-runs-the-run-the-coordinator-rules.md) already removed the opening
table's checkpoint; the glossary's "routing is proposed, not imposed" is met by configuration
rather than by a mid-run prompt. Publishing a queued ticket is the Driver's, an addition beside
[ADR-0006](0006-to-tickets-reached-only-by-user-typed-invocation.md)'s to-tickets path, which
stays the only way a spec is cut. On a workflow that writes tests only where the ticket names them,
the diagnosis step is that naming, so the already-fixed rule below holds on every workflow.

**Queued tickets are serial** because they often share a root cause: a later queued child starts
from the code the earlier one merged, and one whose cause is already fixed lands the test that
proves it and completes. Serial costs wall-clock time and buys zero wave-composition logic and a
diagnosis that is always made against the current integration branch.

## Considered Options

- **A headless brief session at queue time** (a `brief.py` beside `witness.py`: opus at medium
  effort, budget-capped, producing root cause, brief and routing under a JSON schema, then a plain
  child implements). Rejected: it is a new module with one caller and a new seam with one adapter,
  the diagnosis context is thrown away and rebuilt by the implementing child, and the diagnosis is
  made against the code at queue time rather than after the preceding Waves merged. Roughly twice
  the code of the chosen design, most of it the hardest kind to test.
- **The Driver auto-placing findings the child marks "only this ticket"** without waking the
  coordinator. Rejected: it lets the child grade its own finding, and wrap-up escalations were 1
  in 62 across the measured Runs, so it saves nothing.
- **Automating the loop** — the Driver re-routing and re-running the opened tickets at
  `run-complete`. Rejected: it keeps every start-up cost the loop has today and still needs an
  interactive `/route` checkpoint the Run cannot supply.
- **A `/triage` sub-agent.** Structurally impossible: the skill is `disable-model-invocation` and
  refuses to be replicated. A child's typed first turn is a user-typed invocation, which is how
  `/implement` already reaches tdd children ([ADR-0006](0006-to-tickets-reached-only-by-user-typed-invocation.md)).
- **A queued-ticket cap or depth limit.** Not adopted: the serial order and the diagnosis-first
  child already make a queued ticket that finds nothing to do cheap, and a cap would be one more
  number to tune.

## Consequences

- The coordinator's Contract gains an order of preference over placements and the edit/diagnosis
  test; `queued` joins the placement grammar and the report's placement rendering.
- Receipt verification is unchanged: a branch must be ahead of its base, so a queued child whose
  cause is already fixed commits the regression test rather than an empty completion.
- A Run's duration is no longer bounded by its approved Wave table; the dashboard and report show
  queued Waves as they are inserted, and the Wave numbers of the Waves still pending shift as the
  Run grows. Only Waves that have never launched are renumbered, so every `advance` record already
  written still names the Wave it named.
- A queued ticket names no account unless the queue command names one, so it runs on the
  coordinator's account exactly as any account-less ticket does; the `[queued]` cell never
  concludes an account, because which subscription pays is not a fact about the kind of work.
