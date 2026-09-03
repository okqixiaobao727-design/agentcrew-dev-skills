---
status: accepted
---

# A scripted driver runs the whole run; the coordinator is a pure oracle woken by three things

[ADR-0001](0001-coordinator-spends-tokens-only-on-judgment.md) said the coordinator spends tokens
only on judgment. Measured, it did not: the coordinator still built the wave table, asked for its
approval, verified receipts, recorded bookkeeping, advanced waves and wrote the wind-down as prose.
Three measurements decided this design.

- **The cost baseline** ([`cost-baseline.md`](../cost-baseline.md)): 70.4% of the predecessor
  coordinator's bill was mechanical turns, and 19.5% of it was those turns re-reading the cached
  preamble. Every turn re-reads the whole prefix, so removing mechanical turns is superlinear —
  a late bookkeeping turn costs what the session's early turns cost together.
- **The three-run wake audit** (the machine logs and reports of the `crew-first-run-defects`,
  `crew-run-defects-2` and `dashboard-pin` runs): 31 in-run wakes, of which 5 (16%) needed model
  judgment. The other 26 were receipt verifications — 18 of them, every one exiting 0 — bookkeeping,
  or halts a written rule already covered. Across all three runs the approval phase raised zero
  objections, and there were no permission prompts, no invalid receipts and no vanished children;
  the invalid-receipt rung never fired. Assumption-confirmation ASKs were 2 occurrences.
- **The coordinator-as-oracle research note**: a script plus bounded cheap calls beats a second
  resident agent for this work; a hook cannot rewrite a `SendMessage`, so messages cannot be
  intercepted and answered mechanically; and a file-polling channel violates the zero-polling
  contract the wake monitors are built on.

## Decision

**One command is the run's sign-off.** Typing `/crew <feature-dir>` starts the run. The driver
builds and validates the wave table from each ticket's `## Routing` section, and that table is the
run's routing authority ([ADR-0003](0003-script-composed-first-turns-wave-table-authority.md), as
amended). The interactive approval step is removed: it cost the run's most expensive model a
table-construction pass and a round-trip, and produced no objection in three runs. A routing
validation failure is a preflight failure fixed by re-running `/route`, never interpreted by a
model.

**The wake channel is a background task's exit.** The coordinator launches the driver as a
background task of its own session — the mechanism the wake monitors already use — and the turn
ends. Silent running costs the coordinator nothing, and the task's exit is the one channel that
carries a driver-detected judgment event or a driver failure back into the session. The driver's
last line before every exit is one JSON wake snapshot, which is the whole of what a woken
coordinator reads: it never opens a run file, so its rulings rest only on what a message shows it.
Children's ASKs keep arriving by cross-session messaging, authenticated as before.

> **Amended (#103).** The driver is no longer a background task of the coordinator's session: the
> harness killed one silently 45 minutes into a live run, and the run stalled for forty minutes.
> The driver now runs in a tmux window of its own, writes its wake snapshot into the run directory,
> and the coordinator's background task is a stateless waiter that blocks on that file and prints
> it. Everything above about the snapshot itself stands — one JSON object, read without opening a
> run file. What changed is only which process carries it back, and that a killed waiter now costs
> the run nothing. See
> [`docs/monitor-dashboard.md`](../monitor-dashboard.md#the-drivers-own-liveness).

> **Amended (#127).** "A killed waiter costs the run nothing" was half right. The harness reaps
> a main session's background shells on OS memory pressure, so the waiter died three times in one
> run while the driver and its children went on working; the run's facts were intact, but a
> `CREW ASK` sat unanswered until a human saw it and re-typed `/crew`. The remedy is that one
> human action, done by the driver: the waiter records its own liveness the way the driver does
> (`waiter.pid`, written on the way in and removed on every deliberate exit), and a driver that
> writes a wake snapshot with no live waiter types `/crew <feature-dir>` once into the
> coordinator's own tmux pane — the channel it already uses to reach a child — after which the
> dashboard carries `✖ no waiter — /crew <feature-dir> to re-attach` in the driver-dead slot until
> one attaches. Rejected: a waiter-side acknowledgement file (a second liveness protocol beside the
> one that exists), a cheap Claude session that messages the coordinator (tokens on every wake, and
> no session exists for a driver error or run completion), and depending on the harness's
> undocumented `CLAUDE_CODE_DISABLE_BG_SHELL_PRESSURE_REAP` switch — which an operator may still set
> on the coordinator's own launch command as a version-noted mitigation, never as the design.

**The rule table settles everything a written rule already decides.** It is a transcription of the
predecessor skill document's settlement prose, not a new invention: verify on `CREW COMPLETE` and
settle in silence; one re-ask on an invalid receipt and failed on the second; parked and failed
receipts recorded by the driver; a parked ticket with no descendants does not gate its wave; one
nudge for an idle child and failed on the second silence, except while the Machine log says that
child is paused on its vendor's usage limit — a wait the harness ends by itself, which the idle
rung neither nudges nor settles and which holds the run's inactivity deadline for as long as it
lasts (#190); vanished settles failed; a mechanical
conflict resolved in the merge driver itself, and only what it will not rewrite to the
budget-capped repair rung; a templated first answer to a semantic conflict; tracker
closes for merged tickets; wave advancement, monitor re-arming and the dashboard. The report — the
outcome table, the duration rows, the rulings, the undo list and the cost rollup with the
coordinator's own row — is rendered by the driver from the machine log. The cost rollup gives each
witness event its own `witness-<NN>` row, includes its tokens in the total and shows its recorded
duration. The Rulings section renders any escalation item beside a listed placement: this ticket,
queued into this Run with the word it leaves open, deferred to an existing pending ticket, dropped,
or opened outside this Run
([ADR-0028](0028-a-finding-is-queued-into-the-run-and-diagnosed-by-the-child-that-implements-it.md)
added the queued form). A ruling that lists no placement keeps its verbatim rendering.

**The wake surface is exactly three items.** A `CREW ASK` of any kind; a semantic merge conflict a
child has bounced back a second time; and any state the rule table has no row for, which includes a
driver crash, a timeout, an unknown status, a child at a permission prompt and a monitor that
failed. Nothing else reaches the coordinator, and the coordinator narrates no status to the
operator. A clean run costs it one turn to launch, one per ASK, and one to point at the report.

> **Amended (advisor experiment, 2026-08-26).** Two days of running the same roles by hand — one
> resident Fable advisor, one Opus developer per ticket, 22 developer sessions, 70 messages up —
> measured what the three-kind surface leaves out and what the "message alone" rule costs.
> *Kinds.* Children asked about two decisions per ticket, against 0.7 under `/crew`, and the extra
> ones were decision points raised **before the first edit** — interface shape, architecture
> placement, acceptance criteria — a phase the three kinds cannot name. Five categories of question
> were used evenly and only two of seventy should have been settled by the child. So a `CREW ASK`
> now comes in five kinds — `design`, `scope`, `doc-conflict`, `stuck`, `wrap-up` — and may arrive
> at any phase; `wrap-up` carries a finished ticket's leftovers for the coordinator to place, which
> is where the run's follow-up tickets now come from. *Evidence.* The advisor read files itself
> 60–95 times a session, 69–141k characters of tool output that changed no ruling: every reversal
> came from evidence the child sent. Its one prompted lever — delegate research to a cheap
> sub-agent — fired once in four sessions. Prompted discipline does not hold, so the mechanism
> changed: a script attaches a **witness brief** to every ASK (a fresh budget-capped cheap session
> checks each pointer, no recommendation), and the coordinator's own reads are bounded by a hook to
> one pointer of at most 80 lines — enough to settle a fact the child and the brief state
> differently, never enough to hunt. *Cost.* Messages up averaged 475 words, 37% of them progress
> or FYI with no decision in them; the advisor narrated 96 times to the operator for 4 decisions
> asked. An escalation now carries a question or a wrap-up and nothing else, and the coordinator
> speaks to the operator only for a decision that is theirs.

> **Amended (#162, 2026-08-30).** The advisor experiment's zero reversals measured whether later
> evidence changed a ruling, not whether an internally consistent ruling was correct. #154 showed
> the missing case: three rulings agreed with a stale brief and were wrong against the maintainer's
> decisions. The read boundary is therefore a kind line, not a quantity line. Maintainer-authored
> judgment Markdown may be read whole: the current staged run's top-level Markdown; repository
> ADRs, glossary, `CONTEXT.md` and tracker conventions; and the Crew skill and its Markdown
> references. Code, tests and git remain behind the witness, with the one-pointer, explicit-offset,
> at-most-80-line read retained only for settling a disputed source fact. Searches and shell file
> reads remain hunts. The former whole-Crew-directory exemption is narrowed to its skill document
> and Markdown references.

> **Amended (#194, 2026-09-03).** The witness brief was attached to an escalation *after* it was
> sent, and the coordinator was woken twice for it. A child sends its `CREW ASK` straight to the
> coordinator, which rules at once; the Driver then reads the same escalation out of the Machine
> log, runs the witness, and wakes the coordinator with a snapshot whose `detail` is byte-identical
> to the message it has already answered. Measured in one run: 8 of 8 escalations, 8 wasted
> coordinator turns, and in 6 of them an empty brief — so the second wake carried nothing new. The
> one wake that did carry value carried it *too late*: the brief contradicted a ruling already
> made, because a directly-sent escalation gives the coordinator no way to know a brief is coming.
> **A fact-check that arrives after the ruling is not a fact-check.** So the witness moves ahead of
> the send: a child states its question and its kind to a Crew command, that command owns the
> `CREW ASK` grammar, the stamp, the pointers and the witness run, and prints one message the child
> sends unchanged. The child is not told the witness exists. Delivery stays with the child's own
> message tool — composing and delivering are separate, and the command only composes — so the
> authorization and message-copying hooks, and the Machine log's content-based classification of an
> escalation, are untouched. A witness that fails or times out never blocks the send; the message
> goes without a brief and says so. *The Driver's wake becomes a backstop.* Seeing a standing
> escalation it now waits, and carries on the moment a ruling is recorded — the Run projection
> already derives that fact and the Driver simply never consulted it. It raises today's wake only
> after a grace period with no ruling recorded, measured from the escalation's own Machine-log
> record so an adopted run does not restart the clock, and still records the hand-over line so the
> backstop cannot repeat. The grace period is not optional: the escalation channel is fallible by
> [ADR-0023](0023-the-coordinator-is-addressed-by-socket-not-by-name.md)'s own known risk — an
> inbound peer message held for permission approval, where the approval expires — and today's
> unconditional wake was silently the only thing covering it. Nothing else would find a lost ruling:
> the whole-run stall timer resets on any Machine-log line anywhere in the run, and the idle rung
> explicitly skips a child whose last word is still unsettled. Rejected: letting the command deliver
> the message as well as compose it — there is no command-line way to send a cross-session message,
> so it would mean reimplementing an undocumented socket protocol or typing into the coordinator's
> pane, both of which bypass the two hooks and force a second, parallel way for escalations to enter
> the Machine log, to save the child one tool call.

## Consequences

- The crew skill document is the oracle's resident prefix, so it holds the reversibility contract,
  the pure-oracle boundary, the start command, the triage pointer and the resume line — 40–60 lines
  where it was 400. Every procedural step it used to carry lives in the driver.
- Starting and resuming are one action: `start` adopts a feature's unfinished run instead of
  beginning a second one, so an interruption, a driver crash or a coordinator restart costs exactly
  one re-typed command. The resume reference document is retired into that code path.
- A preflight failure reaches the model as one line. The full problem list goes to the operator
  through the dashboard channel, cleared at the next start so a stale notice cannot outlive its fix,
  and the repair happens in a separate cheap session. An aborted start costs the oracle nothing.
  **Amended 2026-08-27 by #141:** a project may configure a `[preflight] gate` argv. After the cheap
  read-only checks, the Driver checks out and fast-forwards the base, runs that command from the
  repository root, and only then cuts the integration branch. A red gate restores the starting
  branch or detached commit and reaches the same `preflight-failed` surface without creating an
  integration branch or child worktree. A fresh run with no gate records `base gate: none
  configured` in its machine log and final report; omission is never recorded as a pass.
- Clearing a finished run is a standalone command the operator runs in a terminal, with its own
  inventory and its own confirmation. The coordinator never holds cleanup context.
- Assumption-confirmation ASKs are deliberately not auto-approved. They were 2 occurrences in three
  runs and the cost of misapproving one is asymmetric, so judgment stays with the role that owns it.
- Rejected on the evidence above: a second resident scheduling agent or the Agent SDK
  ([ADR-0002](0002-shell-scripts-drive-the-cli-not-workflows-or-the-sdk.md) stands), a crew-state
  MCP server, and hook-injected `additionalContext` as a context channel — the last two unmeasured,
  with the 2× cache-rewrite cost cutting against them.
- The driver is one thin module driving the dispatch renderer, advance, the monitor, the
  machine log and the Codex bridge at their published command lines. Their responsibilities do not
  move; this ADR adds a state machine, not a rewrite.
