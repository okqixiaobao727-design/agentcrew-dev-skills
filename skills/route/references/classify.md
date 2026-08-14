# Classify

The four-dimension tests, the routing table's shape, and the `## Routing` template — the judgment
core both branches consume, read at the point of use: route-only reads it before classifying, and a
spec-only `to-tickets` run reads it when drafting begins (ADR-0006).

Two passes per ticket: workflow first, then implementer. Every call cites the test that decided it.

## Workflow — closed enum, first match wins

| Workflow | Take it when |
| --- | --- |
| `acceptance` | Finishing needs a human: real credentials, a third-party dashboard, or a person's judgement of the finished product |
| `ops` | The deliverable is an action against an environment — install, migrate, configure, run and record — rather than new logic |
| `spike` | The deliverable is knowledge: a findings document, not changed behaviour |
| `refactor` | Existing code changes shape while its behaviour stays frozen |
| `tdd` | Runtime behaviour is new or changed, and a test can pin it |
| `direct` | Everything else — prose, docs, skill copy, config — where a test would only restate the edit |

## Core vs non-core — the tdd/refactor first axis

Core iff downstream couples to **this ticket's design decisions**: if this ticket's internal
implementation changed, would a downstream ticket's code have to change? Contracts, schemas,
protocols, and shared interfaces are core. A ticket that merely runs first in a serial chain is
non-core. Fan-out of two or more dependents is a supporting signal, never the verdict.

## Complex vs routine — the tdd/refactor second axis

Complex iff any one of: it crosses modules; the spec leaves the implementation approach to the
executor; the logic is convoluted (concurrency, state machines, failure recovery). Otherwise
routine.

## Directed collection vs open exploration — the spike axis

Directed collection iff all three hold: the questions are enumerable up front as fill-in blanks;
each blank names its verification method; the deliverable carries no recommendation. Any one missing
makes it open exploration.

## From case to cell

The cells live in the shipped defaults,
[`config/agentcrew.default.toml`](../../../config/agentcrew.default.toml), merged with
`agentcrew.toml` at the target repo root; that file's own header states how the two merge. The case
the tests land on names its own cell: `implementer.<workflow>.<case>`, the answers hyphenated, with
`tdd` and `refactor` sharing `tdd-refactor` — so core × complex is
`implementer.tdd-refactor.core-complex`. The config file names the case beside every cell, including
the `ops` split, which lives only there. Take that cell's executor, model, and effort as the
ticket's.

`tdd` and `refactor` are the only workflows whose diff a review can catch anything in, so they are
the only ones that carry a `Review` line. The quadrant that chose the implementer chooses
`reviewer.<quadrant>`.

An `ops` ticket that touches production keeps its cell's effort and carries the note *consider
raising the effort* in its reason, so the user decides the effort at confirmation.

## The routing table

One row per ticket in dependency order — number, title, workflow, executor, model, effort, review,
reason — the review cell empty on the four workflows that carry none, and a ticket's existing
`## Routing` values beside the suggested ones so a re-run reads as a diff. Head the table with the
config file the cells resolved from.

## The Routing section

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

Values go in verbatim as the approved table shows them. Nothing downstream resolves a name, so an
alias written here reaches the launch command as an alias, where plan mode mis-resolves it.

The `Review` line goes in on a `tdd` or `refactor` ticket and is left out on the other four, whose
routing is five lines rather than six. It is the ticket's last section, and a ticket carries exactly
one.

The role string follows the workflow: an `acceptance` ticket is marked `ready-for-human`, every
other ticket `ready-for-agent`.
