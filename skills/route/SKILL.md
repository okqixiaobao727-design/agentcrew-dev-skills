---
name: route
description: Cut a spec into tickets and classify each one — workflow, executor, model, effort — or classify the tickets a feature already has, writing the confirmed routing into every ticket.
disable-model-invocation: true
---

# Route

`/route <path-to-feature-dir>` classifies every ticket of a feature on four dimensions —
workflow, executor, model, effort — and writes the confirmed conclusions into each ticket as a
`## Routing` section, together with the review lane the reviewed workflows carry. `/crew` reads
that section to shape each child's first turn and to pick its executor, model, effort, and
reviewer.

Two modes, decided in step 2 by what the feature directory already holds. On a spec with no
tickets, `/route` cuts the tickets too, by invoking `/mattpocock-skills:to-tickets` with the
routing overlay of step 3 — ticket granularity and routing granularity are one sizing decision, so
one skill and one approval settle both. Where tickets already exist, `/route` routes them and cuts
nothing.

The classification logic is fixed: the tests below decide which case a ticket falls in. The outcome
of each case — executor, model, effort — is a cell of the model tables, which you read from config
rather than from this file.

Routing is a **suggestion**: this skill does the analysis and states the reason behind every call;
the user is the one who decides. Steps 1–5 read and propose; step 6 is the first write.

`ask-matt` routes a conversation to a skill before a spec exists; `/route` routes a ticket to an
executor after tickets exist.

## 1. Load the model tables

Read the shipped defaults in [`config/agentcrew.default.toml`](../../config/agentcrew.default.toml),
then `agentcrew.toml` at the target repo root. A cell the project file names replaces the shipped
cell for that case; every case it leaves out keeps its shipped values.

A repo with no `agentcrew.toml` at its root is a first run, and starts at the setup wizard in
[`references/setup.md`](references/setup.md): it settles the repo's issue-tracker convention
document, then writes the config from the shipped defaults. Resume here once it is done. The same
wizard reconfigures a repo whenever the user asks.

**Done when** every cell named in the two tables of step 4 has resolved to an executor, a model, and
an effort, and you can say which file each one came from.

## 2. Load the feature directory

Read `docs/agents/issue-tracker.md` and settle which tracker this repo uses;
[`references/trackers.md`](../../references/trackers.md) turns that document into the **read**,
**publish**, **edit**, and **mark** operations this skill calls in steps 2 and 6, and declares which
trackers are exercised. Then read the feature's spec — two of the tests in step 4 turn on what the
spec pins down and what it leaves to the executor. Then look for the feature's tickets, where that
tracker keeps them.

Tickets found is **route-only** mode: **read** every one of them — body, `Blocked by:` edges
resolved into dependents as well as blockers, and its existing `## Routing` section if it has one —
then go straight to step 4; this mode cuts nothing and publishes nothing. A spec with no tickets is
**spec-only** mode: step 3 cuts them.

**Done when** the spec is read, the tracker and the mode are both settled and stated to the user,
and in route-only mode every ticket is loaded with its body, blockers, dependents, and current
routing.

## 3. Cut the tickets

Spec-only mode. Invoke `/mattpocock-skills:to-tickets` with the spec as its argument, and carry
these rules into it. They are **additions**: everything that skill does on its own it keeps doing,
and none of its process is restated here — the skill is the source of truth for how tickets get
drafted and published. Where it is installed but not exposed as a slash command in this session,
read its `SKILL.md` and follow it directly; the overlay is the same either way.

**The convention document names where tickets land.** That skill's own local-file branch names a
path of its own. This repo's `docs/agents/issue-tracker.md` is the one that decides — the tracker
operations of [`references/trackers.md`](../../references/trackers.md) read it, and step 6 publishes
where it says.

**Vertical slicing binds the code tickets.** The deliverable kinds are the workflow enum of step 4.
A `tdd` or `refactor` ticket has layers to cut through, so the vertical-slice rules bind it whole.
A `direct`, `spike`, `ops`, or `acceptance` ticket has no layers to cut: it keeps the sizing rule —
one fresh context window — and the bar that a finished ticket is verifiable on its own, in whatever
form its deliverable takes.

**Splits stay vertical-first, and a contract is the only exception.** Routing pressure — wanting a
core design decision to sit in a ticket of its own — never buys a horizontal split. The one
exception is a **contract** that two or more downstream tickets couple to (a schema, a protocol, a
shared interface): cut it as its own ticket ahead of them, which is what lets those downstream
slices route as non-core in step 4. The wide-refactor exception the skill already carries is
untouched and stays available.

**A contract ticket's acceptance criteria state the contract.** Name what downstream couples to —
the import path, the directory layout, the command that runs the suite — because criteria written
as symptoms of a working deliverable ("the suite exits 0") leave every slice behind it free to
answer the same question differently, and they collide with each other once an edge case reaches
them.

**One approval and one publication, both owned here.** Draft the slices, then stop: the quiz
material — title, blocking edges, what it delivers — becomes columns of the merged table in step 5
instead of a separate checkpoint, and publication happens in step 6 with routing already in the
body.

**Done when** every ticket is drafted with a title, its blocking edges, and what it delivers, and
nothing has been published or written to the tracker.

## 4. Classify each ticket

Two passes per ticket: workflow first, then implementer. Every call cites the test that decided it.

### Workflow — closed enum, first match wins

| Workflow | Take it when |
| --- | --- |
| `acceptance` | Finishing needs a human: real credentials, a third-party dashboard, or a person's judgement of the finished product |
| `ops` | The deliverable is an action against an environment — install, migrate, configure, run and record — rather than new logic |
| `spike` | The deliverable is knowledge: a findings document, not changed behaviour |
| `refactor` | Existing code changes shape while its behaviour stays frozen |
| `tdd` | Runtime behaviour is new or changed, and a test can pin it |
| `direct` | Everything else — prose, docs, skill copy, config — where a test would only restate the edit |

### Core vs non-core — the tdd/refactor first axis

Core iff downstream couples to **this ticket's design decisions**: if this ticket's internal
implementation changed, would a downstream ticket's code have to change? Contracts, schemas,
protocols, and shared interfaces are core. A ticket that merely runs first in a serial chain is
non-core. Fan-out of two or more dependents is a supporting signal, never the verdict.

### Complex vs routine — the tdd/refactor second axis

Complex iff any one of: it crosses modules; the spec leaves the implementation approach to the
executor; the logic is convoluted (concurrency, state machines, failure recovery). Otherwise
routine.

### Directed collection vs open exploration — the spike axis

Directed collection iff all three hold: the questions are enumerable up front as fill-in blanks;
each blank names its verification method; the deliverable carries no recommendation. Any one
missing makes it open exploration.

### Implementer

The case the tests above land on names one cell of the implementer table. Take that cell's
executor, model, and effort as resolved in step 1.

| Workflow | Case | Cell |
| --- | --- | --- |
| `tdd` / `refactor` | core × complex | `implementer.tdd-refactor.core-complex` |
| `tdd` / `refactor` | core × routine | `implementer.tdd-refactor.core-routine` |
| `tdd` / `refactor` | non-core × complex | `implementer.tdd-refactor.non-core-complex` |
| `tdd` / `refactor` | non-core × routine | `implementer.tdd-refactor.non-core-routine` |
| `direct` | any difficulty | `implementer.direct.any` |
| `spike` | directed collection | `implementer.spike.directed-collection` |
| `spike` | open exploration | `implementer.spike.open-exploration` |
| `ops` | mechanical run-and-record | `implementer.ops.mechanical` |
| `ops` | acceptance judgement | `implementer.ops.acceptance-judgement` |
| `acceptance` | — | `implementer.acceptance.any` |

### Reviewer — tdd and refactor only

Those two workflows are the only ones whose diff a review can catch anything in, so they are the
only ones that carry a `Review` line. The quadrant that chose the implementer chooses the reviewer:

| Case | Cell |
| --- | --- |
| core × complex | `reviewer.core-complex` |
| core × routine | `reviewer.core-routine` |
| non-core × complex | `reviewer.non-core-complex` |
| non-core × routine | `reviewer.non-core-routine` |

The quadrant is decided once, here — `/crew` reads the conclusion off this line and never
re-derives it.

An `ops` ticket that touches production keeps its cell's effort in the table and carries the note
*consider raising the effort* in its reason, so the user decides the effort at confirmation.

**Done when** every ticket has a workflow, executor, model, effort, and a one-sentence reason
naming the test that decided it and the answer that test gave, and every `tdd` or `refactor` ticket
also has its review lane.

## 5. Present the suggestion

One table, one checkpoint — the only one either mode has. Show one row per ticket — number, title,
workflow, executor, model, effort, review, reason — in dependency order; the review cell is empty
on the four workflows that carry no review. A ticket that already carries a `## Routing` section
shows its current values beside the suggested ones, so a re-run reads as a diff. Head the table
with the config file the cells resolved from, so the user sees at a glance whether this repo is
routing on its own overrides or on the shipped defaults.

In spec-only mode the rows carry two more columns, blockers and what the ticket delivers, and the
questions the quiz would have asked come with them: is the granularity right, are the blocking
edges right, should any ticket be merged or split further.

Ask the user to confirm. Apply any revision they give — re-cutting a ticket means re-classifying it
— redisplay the full table, and ask again. This step reads and prints only; the tracker stays
untouched until step 6.

**Done when** the user explicitly approves the complete table as displayed.

## 6. Write the Routing section

Every approved ticket ends with this section, whether it is being published for the first time or
edited in place:

```markdown
## Routing

Workflow: <tdd|refactor|direct|spike|ops|acceptance>
Executor: <the implementer cell's executor>
Model: <the implementer cell's model>
Effort: <the implementer cell's effort>
Review: <the reviewer cell's executor, model, and effort, space-separated>
Reasons: <the one-sentence reason from the approved table>
```

Values go in as the approved table shows them, verbatim — `/crew` passes `Model` and `Effort`
straight to the executor's launch command.

The `Review` line goes in on a `tdd` or `refactor` ticket and is left out on the other four, whose
routing is five lines rather than six. It is the ticket's last section, and a ticket carries exactly
one.

**Spec-only mode publishes.** Hand the approved tickets back to `/mattpocock-skills:to-tickets` for
its publication step — its templates, its dependency order, its shape of blocking edges — with the
routing block already in each body, so nothing has to come back and edit what was just written. That
step **publishes** and **marks** through this repo's tracker; the one addition is which role string
each ticket is marked with: an `acceptance` ticket is `ready-for-human`, since finishing it needs a
person, and every other ticket keeps the `ready-for-agent` the skill applies.

**Route-only mode edits.** **Edit** each ticket, replacing its whole body with the approved text; a
ticket that already carries a `## Routing` section gets that section replaced in place.

**Done when** every approved ticket exists on the tracker carrying exactly one `## Routing` section
whose lines match the approved table, is marked with the role string that matches its workflow, and
no ticket the user declined was touched.
