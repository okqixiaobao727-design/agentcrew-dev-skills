# AgentCrew glossary

The vocabulary `/route` and `/crew` speak. Terms define *what a thing is*; how the
skills use them is in [`design.md`](design.md).

## Pipeline

**Pipeline** — The feature flow AgentCrew slots into: `grilling → to-spec → route → crew`.
The first two stages are mattpocock-skills; AgentCrew replaces the back half of Matt's own
pipeline (`… → to-tickets → implement`).

**Matt-first principle** — The governing rule for every integration decision. AgentCrew is a
derivative and an *aggregating enhancement* of mattpocock-skills: when AgentCrew's needs and
Matt's skills' behaviour are in tension, Matt's experience wins. The overlay adds rules; it
never overrides or degrades what his skills do on their own.

**/route** — The routing skill. From a spec it both cuts the tickets (invoking
`/mattpocock-skills:to-tickets` with a routing-aware overlay) and assigns each one a workflow,
executor, model, effort, and — for tdd/refactor tickets — a review line. Ticket granularity and
routing granularity are the same sizing decision, so one skill makes both. Where tickets already
exist, `/route` routes them and cuts nothing.

**/crew** — The runner skill. Takes a feature directory of routed tickets and runs them as waves
of child agents in tmux worktrees, unattended, producing an integration branch and a decision log.

## Work items

**spec** — The requirements document `/to-spec` produces. It is the sole basis on which a child
agent judges *did I build the right thing*. Its **Testing Decisions** section matters most: it
settles the test seam in advance, so a child never has to stop and ask a human mid-run.

**ticket** — The vertical slice `/to-tickets` produces: one narrow path through every layer that
can be demonstrated or verified on its own and fits in a fresh context window. Carries
`Blocked by:` and `Status:` fields, plus AgentCrew's `## Routing` section.

**Tracker** — Where tickets live and where their status is written back, named by the repo's
`docs/agents/issue-tracker.md`. Two are exercised: **github**, where a ticket is an issue reached
through the `gh` CLI, and **local**, where a ticket is a markdown file whose `Status:` line carries
what a label carries on github. Any other tracker runs on its convention document alone, untested.

**Blocked by** — Tickets one ticket declares must finish first. Across all tickets these edges
form a directed acyclic graph.

**Frontier** — The tickets whose blocking edges are all satisfied and that nobody has claimed.
`/crew` only ever takes work from the frontier.

**Wave** — The group of frontier tickets launched at the same moment. By definition they do not
depend on each other, so they run in parallel; the next wave is cut only after the whole wave has
landed.

## Routing

**Routing** — What `/route` concludes for one ticket: workflow, executor, model, effort, plus a
review line on tdd/refactor tickets naming the reviewing vendor and its model/effort. Written into
the ticket as a `## Routing` section of `key: value` lines, which is advisory input to the wave
table `/crew` dispatches from, never a second live authority beside it. Routing is proposed, not
imposed: `/route` presents the full table with per-ticket reasons and writes nothing until you
approve it.

**Workflow** — How a ticket gets developed. A closed set of six: `tdd`, `refactor`, `direct`,
`spike`, `ops`, `acceptance`. It shapes the child's first prompt — which stages run and which are
skipped.

**Executor** — The kind of agent that takes a ticket: `claude` (an interactive Claude session in
tmux) or `codex` (a Codex bridge window). Chosen per ticket; a single wave may mix both.

**Core / non-core** — First axis of the tdd/refactor quadrant. Core means downstream tickets couple
to *this ticket's design decisions* — change the internal approach and downstream code must follow
(contracts, schemas, protocols, shared interfaces). A ticket that merely comes first in sequence is
not core.

**Complex / routine** — Second axis of the tdd/refactor quadrant. Complex if any one of three holds:
it crosses modules; the spec leaves the implementation approach open, so the executor picks the
technical route; or the logic is intricate (concurrency, state machines, failure recovery).
Everything else is routine.

**Targeted collection / open exploration** — The two kinds of spike. Targeted collection means the
acceptance criteria can be written as a fact table waiting to be filled in: the questions can be
enumerated up front, the verification method is specified, and the deliverable recommends no
approach. Missing any one of the three makes it open exploration.

**Cross review** — Only tdd and refactor tickets get code review, and the reviewer always comes from
a different vendor than the implementer (Claude implements, Codex reviews, and vice versa). Review
intensity rises with the quadrant's stakes.

**Standards axis / Spec axis** — The two buckets every review finding falls into. Standards = style,
naming, convention — anything that does not affect correctness. Spec = correctness, safety,
divergence from the spec. Whichever vendor reviews, findings are bucketed on these two axes and
share one round budget.

**Model table** — The two tables mapping a classification case to vendor/model/effort: one for
implementers, one for reviewers. This is AgentCrew's configurable surface. The classification logic
itself — six workflows, core/non-core, complex/routine — is fixed product opinion.

**Crew config file** — The per-project config file at the target repo's root that overrides the model
tables and holds the on-child-launch hook. Generated by the setup wizard from the commented default
shipped in the plugin, so different repos can carry different model choices.

**Setup wizard** — The first-run step that generates the crew config file. It checks for the repo's
issue-tracker convention document first and sends you to `/setup-matt-pocock-skills` when it is
missing, so tracker configuration stays in its one canonical place.

## Roles and messages

**Coordinator** — The scheduling session in the main tmux window that runs `/crew`. It writes no
product code, but it may give design and debugging direction on what children escalate. Its job:
build the dependency graph, open windows and worktrees, watch children, rule on escalations, merge
finished branches, and report the outcome.

**Child agent** — An execution session in its own tmux window and its own worktree, one window per
ticket, with model and effort assigned per ticket by the routing. It is a full interactive session:
you can switch into it and take over at any moment.

**Escalation** — Handing a question up to the level that can rule on it: child to coordinator,
coordinator to human. A child must escalate in exactly three cases — a document conflict (spec,
ticket and code reality disagree in any pair), the same obstacle surviving two attempts, and
finishing the ticket requiring a change to its declared scope. A blocking review finding still open
when the round budget runs out escalates too. Escalation is not an ending: once ruled on, the ticket
keeps running.

**Receipt** — A child's final word on a ticket: complete (with the full commit sha, which the
coordinator verifies independently) or failed (with the reason).

**Message channel** — Point-to-point cross-session messaging between Claude Code sessions on one
machine. Escalations, answers and receipts travel this way. tmux is reserved for three jobs:
launching children, answering permission confirmations (a message cannot approve permissions on the
receiver's behalf), and human takeover.

**On-child-launch hook** — An optional config entry — a command plus optional extra environment
variables for the child — run once per child at launch. Empty by default. It is the extension point
for whatever notification or session-tracking system you already run.

## Artefacts

**Worktree** — A child's isolated working directory. It isolates *files*, not the world: inside a
worktree a child still reaches the network, remote hosts and production databases. That is why the
red line exists.

**Integration branch** — `crew/<feature-slug>`, cut from the base branch when the run starts. Every
finished ticket merges into it; the base branch is untouched for the whole unattended run and the
final merge is a human's. The integration branch is disposable — if a run goes wrong, delete it and
run again.

**Decision log** — The feature's `decisions.md`. Every judgement the coordinator makes on your
behalf is recorded here, and any action reaching outside a worktree (production database, remote,
deployment, external service) gets its own entry stating how to undo it. With permissions fully
open, this log is the only trail of what happened overnight.

**Red line** — The one class of action blocked mechanically: irreversible destruction of data. A
ticket that hits it is parked for a human.

**Parked** — A child stopped, waiting for a human decision, because going on needs an irreversible
action. The red-line hook is a mechanical block on known command shapes; the criterion for parking
is reversibility itself, so a hook that fails to catch something does not change the criterion.

**Vendored codex bridge** — `codex_bridge.py`, the Codex-side transport, maintained inside this repo
rather than installed as a separate skill.
