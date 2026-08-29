# AgentCrew glossary

The one home for the vocabulary `/route` and `/crew` speak: every term is defined here and
nowhere else, with the synonyms it displaces marked *Avoid*. Terms define *what a thing is*; how
the skills use them is in [`design.md`](design.md).

## Pipeline

**Pipeline** — The feature flow AgentCrew slots into: `grilling → to-spec → route → crew`.
The first two stages are mattpocock-skills; AgentCrew replaces the back half of Matt's own
pipeline (`… → to-tickets → implement`).

**Matt-first principle** — The governing rule for every integration decision. AgentCrew is a
derivative and an *aggregating enhancement* of mattpocock-skills: when AgentCrew's needs and
Matt's skills' behaviour are in tension, Matt's experience wins. The overlay adds rules; it
never overrides or degrades what his skills do on their own.

**/route** — The routing skill. It assigns every ticket of a feature a workflow, executor, model,
effort, and — for tdd/refactor tickets — a review line. Where tickets already exist it routes them
directly; from a bare spec it prompts the user to type `/mattpocock-skills:to-tickets`, whose run
cuts, confirms, and publishes with `/route`'s rules riding along as additions (ADR-0006). Ticket
granularity and routing granularity are the same sizing decision, so one approval covers both.

**/crew** — The runner skill. Takes a feature directory of routed tickets and runs them as waves
of child agents in tmux worktrees, unattended, producing an integration branch and a decision log.

## Work items

**spec** — The requirements document `/to-spec` produces. It is the sole basis on which a child
agent judges *did I build the right thing*. Its **Testing Decisions** section matters most: it
settles the test seam in advance, so a child never has to stop and ask a human mid-run.

**ticket** — The vertical slice `/to-tickets` produces: one narrow path through every layer that
can be demonstrated or verified on its own and fits in a fresh context window. Its content lives
at the Tracker: a github ticket is its issue body and comments, while a local ticket is its file.
A staged github file is only a live `Ticket:` link plus the `## Routing` and `## Blocked by` machine
sections; it is not a content copy. A local staged file remains the ticket itself.

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

**stopped** — The fifth `advance` decision, appended to the machine log by the wave loop rather
than by the advance script: a run ends on an escalation the rule table had already settled, so
there is nothing left to launch and nothing left to rule on
([`wave-advance.md`](wave-advance.md)). It says the *run* ended, where `escalated` says a wave is
halted and still awaiting a ruling, and `interrupted` says the operator stopped the run.

**Ticket state** — Where one ticket has got to, in the one vocabulary every part of the run speaks
to a human: the dashboard, the toasts and the final report. Internal words — a tmux process
status, a monitor's own bookkeeping, a settlement verdict — are mapped into it before they reach
anyone, so nobody has to translate.
_Avoid_: raw source states (busy, idle) in human-facing output

| State | The ticket |
| --- | --- |
| `pending` | is in the table and has not been launched yet |
| `running` | has a live child working on it |
| `waiting` | has a child or merge needing a human: a prompt, an idle/shell turn, a blocked merge |
| `reworking` | has a live child resolving the semantic merge conflict the run sent it back to |
| `parked` | needs an action only a human can take, so it is waiting for them |
| `landable` | has a verified completion receipt and has not been merged yet |
| `settling` | is landable in a wave whose last receipt is in, so its merge is what happens next |
| `merged` | is in the integration branch |
| `failed` | did not produce work that could land |
| `vanished` | was launched and its child is no longer there |

Two more words describe what a *reading* did rather than where a ticket got to, so they are drawn
as annotations beneath a row and are never a state of their own: `duplicate`, where two sessions
are running in one worktree, and `unknown`, where the agents list could not be read at all.

## Routing

**Routing** — What `/route` concludes for one ticket: workflow, executor, model, effort, plus a
review line on tdd/refactor tickets naming the reviewing vendor and its model/effort. Written into
the ticket as a `## Routing` section of `key: value` lines, which is advisory input to the wave
table `/crew` dispatches from, never a second live authority beside it. Routing is proposed, not
imposed: `/route` presents the full table with per-ticket reasons and writes nothing until you
approve it. The section also carries the ticket's **account** where the user named one, which is
the one `## Routing` value `/route` never concludes — it records what the user said.

**Wave table** — The routing table (ticket → workflow, executor, model, effort, review lane,
account) the user approves before the run. After approval it is the sole routing authority, and
every key on it is concrete: an optional key absent from a ticket is resolved to a value where the
table is built, never carried onward as an absence
([ADR-0014](adr/0014-optional-routing-keys-are-resolved-at-the-wave-table-boundary.md)).

**Run plan** — The validated meaning of a run's Wave table: its run metadata, ordered waves, tickets,
dependencies and concrete routing. It says what should happen; the Machine log says what did
happen. Every reader uses the same Run plan rather than interpreting the Wave-table JSON itself
([ADR-0018](adr/0018-run-plan-owns-wave-table-meaning-callers-own-execution.md)).
_Avoid_: wave-table dict, routing cache, workflow state

**Workflow** — How a ticket gets developed. A closed set of six: `tdd`, `refactor`, `direct`,
`spike`, `ops`, `acceptance`. It shapes the child's first prompt — which stages run and which are
skipped.

**Executor** — The kind of agent that takes a ticket: `claude` (an interactive Claude session in
tmux) or `codex` (a Codex bridge window). Chosen per ticket; a single wave may mix both.

**Account** — A named Claude Code login, under which a ticket's Claude processes run and against
whose subscription their tokens are billed. Named per ticket, and unlike every other routing value
it is **not** concluded from the ticket's classification: which subscription pays is a fact about
the operator's wallet, not about the kind of work. A ticket that names none runs on the
coordinator's account, so a single-account run never mentions the word. A single wave may mix
accounts.
_Avoid_: profile (the directory an account resolves to, not the account), subscription (the billing
plan behind the login, not the login)

**Account binding** — What a ticket's account is once the wave table carries it: two facts, not
one. The **resolved configuration home**, which identifies the account and is where that ticket's
transcripts, session files and cost are read; and the **execution mode**, `explicit` for a ticket
that named an account and `inherited` for a ticket that named none. Every Claude process of the
ticket gets its environment from the binding through one shared contract — the one variable for an
explicit binding, nothing at all for an inherited one. An account-less ticket therefore runs in
the environment the run was started in rather than under a default home spelled out explicitly,
which is not the same login
([ADR-0014](adr/0014-optional-routing-keys-are-resolved-at-the-wave-table-boundary.md)).
_Avoid_: "the ticket's account path" for the whole binding (a path cannot say whether it is set or
inherited, which was the defect)

**One ticket, one account** — The invariant that every Claude process belonging to a ticket runs
on that ticket's account: the implementer child, the reviewer child, the exception handler that
repairs that ticket's merge conflict, and the wake monitor that watches it. The account is a
property of the ticket, not of the process, so a ticket moved to another account takes all of its
spend — and everything asked about its liveness — with it.

**Account registry** — The machine-level file mapping account names to Claude Code profile
directories. It is deliberately not profile-scoped: a map *between* accounts cannot be stored per
account ([ADR-0013](adr/0013-the-account-registry-is-not-profile-scoped.md)). The repository holds
names only — a ticket names an account, the crew config file may declare which names this repo
expects, and neither ever holds a path. A name the registry does not hold stops the run; it never
falls back to the coordinator's account.

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

**Standards axis / Spec axis** — The two axes a review runs and reports separately. Standards =
style, naming, convention — anything that leaves behaviour intact. Spec = correctness, safety,
divergence from the spec. Review-Switch reports the next permitted action for each axis; AgentCrew
does not carry a second copy of that rule.

**Model table** — The two tables mapping a classification case to vendor/model/effort: one for
implementers, one for reviewers. This is AgentCrew's configurable surface. The classification logic
itself — six workflows, core/non-core, complex/routine — is fixed product opinion.

**Frontmatter pin** — A `model`/`effort` value fixed in a skill's frontmatter, model always as a
full ID, so the session that makes routing decisions never depends on what the environment happens
to resolve (ADR-0005).

**Full model ID** — The only form a model name takes anywhere in the chain — config cell,
`## Routing` line, launch command — passed verbatim end to end with no alias-resolution layer.
Aliases mis-resolve under plan mode (ADR-0003).
_Avoid_: alias, short name

**Crew config file** — The per-project config file at the target repo's root that overrides the model
tables and holds the on-child-launch hook. Generated by the setup wizard from the commented default
shipped in the plugin, so different repos can carry different model choices.

**Setup wizard** — The first-run step that generates the crew config file. It checks for the repo's
issue-tracker convention document first and sends you to `/setup-matt-pocock-skills` when it is
missing, so tracker configuration stays in its one canonical place.

## Roles and messages

**Protocol** — The text that never varies by workflow and is rendered into every child's first
turn and the manual `developer` prompt from shared blocks: coordinator identity, the five
escalation kinds with their message shape, and the receipt.

**Work brief** — The per-workflow delta in a child's first turn: the base commit, whether a review
runs, and which tests the ticket names. Method belongs to the skill the opening line invokes.

**Coordinator** — The session in the main tmux window that runs `/crew`, and the run's **oracle**:
its whole job is judgment. It launches the driver, rules on what children escalate and on what the
rule table has no row for, and points at the report. It writes no product code. It reads the
judgment Markdown the Contract names whole; for code, tests and git facts it rules from the
escalation and its witness brief, with at most one bounded read to settle a fact they state
differently
([ADR-0010](adr/0010-the-driver-runs-the-run-the-coordinator-rules.md)). It speaks to the human
only for a decision that is theirs; the human asks when they want to know.
_Avoid_: lead, orchestrator (legacy term from `/orchestrate`), advisor (the role's name in a
manual run's prompt, never in these documents)

**Judgment turn** — Any coordinator turn that produces a ruling or an approval. The design goal is
that a run contains no other kind of coordinator turn.

**Reference index** — The static list of file paths (one descriptive line each, no contents) that
staging generates per run in `spec.md`, the coordinator's opening context, so a ruling never starts
with a hunt. Contents are read on demand, and nothing but the coordinator itself puts them in the
live context.

**Driver** — The scripted state machine that runs everything else, in a tmux window of its own,
detached from the coordinator's session: preflight, the validated wave table, the branch and run
directory, dispatch,
receipt verification, the rule table's settlements, merges, tracker closes, wave advancement,
monitor re-arming, and the report. It drives the existing scripts at their published command lines
and holds no state of its own — every count it acts on is read back out of the machine log,
which is what makes adopting an unfinished run the same code path as carrying one on. Before any
Wave is polled, the Driver makes it ready through the same activation path for a fresh start,
adoption, resume or advance: a recorded launch is adopted, while an unrecorded ticket is dispatched
only after a successful live-source reading confirms that no child exists
([ADR-0024](adr/0024-driver-activates-every-wave-through-one-path.md)).

**Run adoption** — A new Driver process taking up an unfinished run after its previous Driver
ended. It restores the run around the work already recorded; it is not a Coordinator handover or
a Waiter attachment.

**Waiter** — The coordinator's one background task while a run is live: it blocks until the
driver leaves a wake snapshot in the run directory, prints that snapshot unchanged, and ends. It
holds no state and composes nothing, so a killed waiter loses no run fact — but until another is
attached, no wake reaches the coordinator (#127). Re-typing `/crew <feature-dir>` attaches one.
_Avoid_: wake monitor (the per-child watchers), listener, poller

**Waiter attachment** — A Coordinator session attaching its stateless Waiter to a run whose Driver
continues unchanged. It restores delivery of future wake snapshots; it is neither Run adoption nor
Coordinator handover.

**Wake surface** — The exhaustive list of what reaches the coordinator mid-run: a `CREW ASK` of any
kind, a semantic merge conflict a child has bounced back a second time, and any state the rule table
has no row for (a driver crash, a timeout, an unknown status, a permission prompt, a failed
monitor). Each arrives as one JSON **wake snapshot** the driver prints as it exits, carrying the
reason, the ticket, a pointer for the ruling, and the command that resumes the loop.

**Vanished** — A launched, unsettled child whose lane's live source was read successfully and
does not list it: a tmux pane that is not in the pane list, a session that is not in the sessions
list. Only a successful observation can say it. A read that failed — a tmux call that did not run,
a state file that could not be read, a list fetched from the wrong account — says nothing about the
child and is `unknown`, never `vanished` (#110, #140). The driver settles a vanished child `failed`
on one reading, because the word is only ever used when the source itself answered.
_Avoid_: gone, dead child, missing (the words a failed read tempts)

**Child agent** — An execution session in its own tmux window and its own worktree, one window per
ticket, with model and effort assigned per ticket by the routing. It is a full interactive session:
you can switch into it and take over at any moment.
_Avoid_: worker, teammate

**Escalation** — Handing a question up to the level that can rule on it: child to coordinator,
coordinator to human. A child escalates at any phase — before its first edit, mid-ticket, or at
wrap-up — in exactly five kinds: `design` (a decision point), `scope` (finishing needs a change to
the ticket's declared scope), `doc-conflict` (spec, ticket and code reality disagree in any pair),
`stuck` (the same obstacle surviving two attempts) and `wrap-up` (leftovers at completion). A
review ending on `escalate` goes up as `doc-conflict`; a review that cannot run goes up as `stuck`.
Everything else the child settles itself: code shape, naming, tests, a root-caused bug, a refactor
that changes no behaviour, what a document already answers. An escalation is one message carrying only
what a ruling needs — every fact as a pointer; progress, reasoning and assumptions stay in the
child's commits and receipt. Escalation is not an ending: once ruled on, the ticket keeps running.
_Avoid_: question, ask (as a noun), progress report

**Decision point** — A choice on product goal, acceptance, architecture, module boundary or
public interface that the child cannot make alone, most often before its first edit. All of a
phase's decision points travel as one `design` escalation, the child's own pick marked on each.

**Witness brief** — A pointer-backed, fact-only result from one fresh, read-only, budget-capped
Witness session. An escalation `check` marks each cited pointer held, contradicted or missing and is
launched automatically by the driver, or by the child before sending in a run without a driver. An
`ask` is coordinator-initiated and returns independently stated factual claims, each with at least
one pointer. Both carry no recommendation or ruling.
_Avoid_: research, verification

**Wrap-up** — The `wrap-up` escalation a child sends when its ticket is complete and leftovers
remain: one line per leftover — what it is, its pointer, and whether it touches only this ticket
or another ticket or a public interface — with no recommendation. The coordinator rules each line:
fixed in this ticket, opened as a ticket seeded from that line, or dropped. A completion with no
leftovers is a receipt, not a wrap-up.
_Avoid_: tail, follow-up list

**Bounded read** — The coordinator's file boundary is a kind line. Maintainer-authored judgment
Markdown is read whole: top-level Markdown in the current staged run; repository ADRs, the
glossary, `CONTEXT.md`, `references/trackers.md` and `docs/agents/*.md`; and the Crew `SKILL.md`
and its Markdown references. Physical locations decide membership (ADR-0007). Code, tests and git
are facts: one source pointer may be read with an explicit offset and at most 80 lines to settle
what an escalation and its witness brief state differently, or to see a pointer the brief marks
missing. `Grep`, `Glob` and shell file reads are a **hunt**, and the hook refuses them.

**Ruling** — A judgment the coordinator issues in reply to an escalation: design direction, a
conflict verdict, or a scope decision.
_Avoid_: decision (overloaded), answer

**Escalation ladder** — The fixed order in which a mechanical failure is retried: script →
budget-capped headless Sonnet repair → coordinator. The script rung resolves a mechanical merge
conflict itself, so only what it will not rewrite reaches the repair session; only a double failure
or a semantic conflict reaches the top.

**Exception handler** — The budget-capped headless cheap-model session a script launches to fix a
mechanical failure. It is not resident, and the coordinator learns it ran only when it fails.
_Avoid_: executive, dispatcher agent

**Receipt** — A child's final word on a ticket: complete (with the full commit sha, which the
coordinator verifies independently) or failed (with the reason). A child records it in the run's
machine log rather than sending it, so a receipt reaches the run's record without waking the
coordinator for an event it has nothing to decide.

**Message channel** — Point-to-point cross-session messaging between Claude Code sessions on one
machine. Escalations and answers travel this way; receipts do not. tmux is reserved for three jobs:
launching children, answering permission confirmations (a message cannot approve permissions on the
receiver's behalf), and human takeover.

**Coordinator address** — The whole `uds:` address of the socket the harness bound for the
coordinator's session, and the one thing a Claude child sends to. It is the coordinator's identity
on the message channel; the coordinator's session *name* is a label beside it, never an address.
Read off the harness at start, carried on the run's metadata, and used verbatim — never composed,
never normalised
([ADR-0023](adr/0023-the-coordinator-is-addressed-by-socket-not-by-name.md)). It is what makes a
child on a second account reach the same coordinator as a child on the first.

**Coordinator handover** — An in-place transfer of a live run from its current Coordinator to a
new Coordinator while the existing Driver continues. A manual `/crew` invocation from a different
Coordinator address requests the transfer even if the old process still appears alive; the Driver
atomically supersedes the old Coordinator, records the new identity and re-anchors live children
before Wave activation or polling continues
([ADR-0025](adr/0025-a-live-driver-accepts-coordinator-handover-in-place.md)).

**On-child-launch hook** — An optional config entry — a command plus optional extra environment
variables for the child — run once per child at launch. Empty by default. It is the extension point
for whatever notification or session-tracking system you already run.

## Operator surface

**Operator surface** — What the human watches a run through, and what no model reads: the wake-up,
the dashboard, the window, the pin, the receipt check and the cost pass. What each part does is in
[`monitor-dashboard.md`](monitor-dashboard.md).

**Dashboard** — The script-rendered human view of a run: a live table plus milestone and
exception toasts, exactly one per run. The human watches this instead of coordinator prose.

**The pin** — The surface that draws the run's frame into the coordinator's own Claude Code
statusline, so the frame and the coordinator's prompt are on screen at once and nothing is left to
close when the run ends. Same rows, same states, same annotations as the window; only where the
frame is drawn is different. The window is unchanged and stays the default
([ADR-0008](adr/0008-the-pinned-dashboard-lives-in-claude-codes-statusline.md)).

## Artefacts

**Worktree** — A child's isolated working directory. It isolates *files*, not the world: inside a
worktree a child still reaches the network, remote hosts and production databases. That is why the
red line exists.

**Integration branch** — `crew/<feature-slug>`, cut from the base branch when the run starts. Every
finished ticket merges into it; the base branch is untouched for the whole unattended run and the
final merge is a human's. The integration branch is disposable — if a run goes wrong, delete it and
run again.

**Machine log** — The run's `.crew/log.jsonl`, written entirely by scripts and hooks. Every
launch, receipt, merge, escalation, ruling and wave decision lands there as one JSON line, and any
action reaching outside a worktree (production database, remote, deployment, external service) is
ruled on in a message that says how to undo it, copied in verbatim. With permissions fully open,
this log is the only trail of what happened overnight; the run's `report.md` is the read of it a
human gets at the end.
_Avoid_: decision log (legacy), status report

**Run projection** — The current facts about one run, derived solely from its ordered Machine log.
It says what has happened and remains true; it contains neither the Wave table's plan nor the
Driver's next action
([ADR-0017](adr/0017-machine-log-owns-run-facts-driver-owns-workflow-policy.md)).
_Avoid_: driver state, workflow decision, Ticket state (the human-facing vocabulary)

**Pin** — The file that names a live run: the run directory as an absolute realpath (ADR-0007), the
coordinator's pid, the coordinator's tmux session, and what draws it — the writing release's own
`monitor.py` and interpreter. Those last two are recorded by a release alive at that moment and
never at install time, which is what keeps the statusline wrapper a permanent stub no upgrade can
strand (ADR-0011). Written at dispatch into the pin registry
(`$CLAUDE_CONFIG_DIR/agentcrew/pins/`) and removed when the run ends. It is the run's only trace on
the operator's screen, and the only thing the pin surface reads to find the run: no pin, nothing
drawn ([`monitor-dashboard.md`](monitor-dashboard.md)).
_Avoid_: frame file, liveness file (there is no background process behind it)

**Red line** — The one class of action blocked mechanically: irreversible destruction of data. A
ticket that hits it is parked for a human.

**Parked** — A child stopped, waiting on the human, because going on needs an action only they can
take: an irreversible action, or a credential the run lacks. The red-line hook is a mechanical block on known command shapes; the criterion for parking
is reversibility itself, so a hook that fails to catch something does not change the criterion.

**Judgment core** — The judgment material every run consults: the classification tests, the
checkpoint rules, and the completion criteria. It lives in one reference file that every branch
force-reads at its point of use (ADR-0006) — the mandatory Read step is what makes applying it
reliable, where a weakly-worded pointer would make it a coin-flip.
_Avoid_: essential prose, main content

**Mode-gated reference** — A reference file only one mode of a skill loads, through an explicit
Read instruction in the body — e.g. the to-tickets+route handoff that route-only `/route` runs
never see. Disclosure earns its cost only when some branch skips the material.

**Review recovery** — Re-attaching to the review a child already has running, keyed on the owner
tuple Review-Switch stores: tmux server, origin pane, worktree root. The reviewing session
outlives the driver process that launched it, so a lost handle is recovered rather than replaced;
starting a second review of one diff is the failure this recovery exists to prevent.
_Avoid_: retry, restart

**Codex bridge** — `codex_bridge.py`, the Codex-side transport, written and maintained here rather
than installed as a separate skill.
_Avoid_: vendored codex bridge — the word said this repo may not edit it, and this repo owns it.
