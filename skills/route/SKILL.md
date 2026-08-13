---
name: route
description: Cut a spec into tickets and classify each one — workflow, executor, model, effort — or classify the tickets a feature already has, writing the confirmed routing into every ticket.
disable-model-invocation: true
model: claude-opus-5
effort: medium
---

# Route

`/route <path-to-feature-dir>` classifies every ticket of a feature on four dimensions — workflow,
executor, model, effort — plus the review lane the reviewed workflows carry, and writes each
conclusion into its ticket as a `## Routing` section. That section is **advisory input**: `/crew`
builds its wave table from it, and from approval onward the wave table is the sole routing
authority.

Routing is a **suggestion**. Steps 1–5 read and propose, step 6 is the first write, and the user
decides at the one checkpoint between them.

## 1. Load the model tables

Read the shipped defaults in [`config/agentcrew.default.toml`](../../config/agentcrew.default.toml),
then `agentcrew.toml` at the target repo root; that file's own header states how the two merge.

A repo with no `agentcrew.toml` at its root is a first run: go to the setup wizard in
[`references/setup.md`](references/setup.md), and resume here once it is done. The same wizard
reconfigures a repo whenever the user asks.

**Done when** every cell of the implementer and reviewer tables has resolved to an executor, a
model, and an effort, and you can say which file each one came from.

## 2. Load the feature directory

Settle the tracker: [`references/trackers.md`](../../references/trackers.md) turns this repo's
`docs/agents/issue-tracker.md` into the **read**, **publish**, **edit**, and **mark** operations
steps 2 and 6 call. Read the feature's spec — two of the step 4 tests turn on what it pins down and
what it leaves to the executor. Then look for the feature's tickets, where that tracker keeps them.

Tickets found is **route-only** mode: **read** every one of them — body, `Blocked by:` edges
resolved into dependents as well as blockers, and its existing `## Routing` section — then go
straight to step 4, cutting nothing. A spec with no tickets is **spec-only** mode: step 3 cuts them.

**Done when** the spec is read, the tracker and the mode are both settled and stated to the user,
and in route-only mode every ticket is loaded with its body, blockers, dependents, and current
routing.

## 3. Cut the tickets

Spec-only mode. Read [`references/cutting.md`](references/cutting.md) — the routing overlay that
rides on top of `/mattpocock-skills:to-tickets` — then invoke that skill with the spec as its
argument, carrying the overlay into it.

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
each blank names its verification method; the deliverable carries no recommendation. Any one missing
makes it open exploration.

### From case to cell

The case the tests land on names its own cell: `implementer.<workflow>.<case>`, the answers
hyphenated, with `tdd` and `refactor` sharing `tdd-refactor` — so core × complex is
`implementer.tdd-refactor.core-complex`. The config file names the case beside every cell, including
the `ops` split, which lives only there. Take that cell's executor, model, and effort as resolved in
step 1.

`tdd` and `refactor` are the only workflows whose diff a review can catch anything in, so they are
the only ones that carry a `Review` line. The quadrant that chose the implementer chooses
`reviewer.<quadrant>`.

An `ops` ticket that touches production keeps its cell's effort and carries the note *consider
raising the effort* in its reason, so the user decides the effort at confirmation.

**Done when** every ticket has a workflow, executor, model, effort, and a one-sentence reason naming
the test that decided it and the answer that test gave, and every `tdd` or `refactor` ticket also
has its review lane.

## 5. Present the suggestion

One table, one checkpoint — the only one either mode has. One row per ticket in dependency order —
number, title, workflow, executor, model, effort, review, reason — the review cell empty on the four
workflows that carry none, and a ticket's existing `## Routing` values beside the suggested ones so
a re-run reads as a diff. Head the table with the config file the cells resolved from.

Spec-only mode adds two columns, blockers and what the ticket delivers, and the questions that come
with them: is the granularity right, are the blocking edges right, should any ticket be merged or
split further.

Ask the user to confirm. Apply any revision they give — re-cutting a ticket means re-classifying it
— redisplay the full table, and ask again.

**Done when** the user explicitly approves the complete table as displayed.

## 6. Write the Routing section

Every approved ticket ends with this section:

```markdown
## Routing

Workflow: <tdd|refactor|direct|spike|ops|acceptance>
Executor: <the implementer cell's executor>
Model: <the implementer cell's model, a full model ID>
Effort: <the implementer cell's effort>
Review: <the reviewer cell's executor, model, and effort, space-separated>
Reasons: <the one-sentence reason from the approved table>
```

Values go in verbatim as the approved table shows them. Nothing downstream resolves a name — the
wave table `/crew` builds from this section carries each value through to the launch command — so an
alias written here reaches the CLI as an alias, where plan mode mis-resolves it.

The `Review` line goes in on a `tdd` or `refactor` ticket and is left out on the other four, whose
routing is five lines rather than six. It is the ticket's last section, and a ticket carries exactly
one.

**Spec-only mode publishes.** Hand the approved tickets back to `/mattpocock-skills:to-tickets` for
its publication step, with the routing block already in each body. That step **publishes** and
**marks** through this repo's tracker; the one addition is the role string: an `acceptance` ticket
is marked `ready-for-human`, every other ticket keeps the `ready-for-agent` that skill applies.

**Route-only mode edits.** **Edit** each ticket, replacing its whole body with the approved text; a
ticket that already carries a `## Routing` section gets that section replaced in place.

**Done when** every approved ticket exists on the tracker carrying exactly one `## Routing` section
whose lines match the approved table, is marked with the role string that matches its workflow, and
no ticket the user declined was touched.
