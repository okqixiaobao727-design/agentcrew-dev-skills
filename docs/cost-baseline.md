# The predecessor cost baseline

> **DRAFT — not yet approved for publication.** Every `TODO(operator)` below marks a line that
> can only come from the operator-private forensics document. Until they are all resolved and the
> operator has approved this file's contents for a public repo, this file is a skeleton, not a
> citable baseline. See `docs/acceptance-25.md` for the checklist that closes it.

[ADR-0001](adr/0001-coordinator-spends-tokens-only-on-judgment.md) decided that the coordinator
spends model tokens only on judgment, and it decided that on measured numbers: a forensic audit of
one real `/orchestrate` run under the predecessor design. That audit lived only in an
operator-private document, so the numbers the ADR cites had no checkable source inside the repo,
and a future run's cost record ([`docs/machine-log.md`](machine-log.md) `session-cost` events plus
the run report's rollup) had nothing in-repo to be graded against.

This file is that source, redacted: the figures, what was measured, and how — with the parts that
identify a private repository, a person, or an account removed.

## What was measured

| Field | Value |
| --- | --- |
| Predecessor system | `/orchestrate` (the workflow AgentCrew replaced) |
| Coordinator model | Fable 5 |
| Run scope | TODO(operator) — tickets, waves, wall clock |
| Date measured | TODO(operator) |
| Source of the bill | the coordinator's own session transcript |
| Repository under work | redacted (private) |

## Headline figures

These are the figures ADR-0001 already cites; this table is their record, not a new claim.

| Figure | Value |
| --- | --- |
| Coordinator turns that were mechanical (dispatch, polling, bookkeeping, status narration, git plumbing) | 69% |
| Coordinator **bill** that was mechanical | **70.4%** |
| Coordinator bill spent on rulings — the only work needing the expensive model | **7.6%** |
| Total coordinator turns in the run | 184 |
| Turns removed when the run is replayed under the judgment-only design | 127 |
| Coordinator cost removed in that replay | ~81% |
| Startup preamble size | 36.5K tokens |
| Times the preamble was re-read | 183 |
| Share of the coordinator's bill spent re-reading the preamble | 19.5% |

Remainder of the bill (100% − 70.4% − 7.6% = 22%): TODO(operator) — what the remaining categories
were, or state that the audit did not categorise them.

## How it was measured

The cost model behind the figures is stated in ADR-0001: cost behaves as turns ×
context-size-at-that-turn, so a late mechanical turn also pays to re-read the whole prefix.

The steps that produced the numbers:

1. TODO(operator) — how the transcript was obtained and which fields were read.
2. TODO(operator) — how each turn was classified as mechanical vs ruling vs other, and by whom
   (hand classification, script, or model-assisted).
3. TODO(operator) — how a turn's bill was computed from its usage figures and the prices used.
4. TODO(operator) — how the replay ("127 of 184 turns removed, ~81% of cost") was simulated.

Anything in this section that cannot be restated without exposing private material should be
recorded as "redacted" rather than dropped, so a future audit knows a step existed.

## Grading a future run against this

A run under the current design leaves its own cost record in its artifacts: one `session-cost`
event per child in the machine log, and a coordinator/run rollup in the run report. To grade:

1. Take the coordinator's own token/cost figures from the run report.
2. Split them the way this baseline splits them — mechanical vs ruling — using the same
   classification rule recorded above.
3. The judgment-only claim holds when the mechanical share is far below this baseline's 70.4% and
   the run needed no coordinator turns for bookkeeping.

## First `/crew` run on v0.2.1 — context, not baseline

Recorded here because it is the first datapoint on the new design, measured the same way (by hand,
from transcripts) and therefore comparable to the baseline in method only, not in scope.

- Coordinator finished 6 tickets across 3 waves at 130.5K context — 13% of the window.
- The coordinator's bill was roughly two thirds of what the six children cost between them.
- Qualitatively consistent with ADR-0001's goal, but not provable from run artifacts: that run
  predates the cost instrumentation.

Absolute spend figures are deliberately absent. The repo's tree validator
([`scripts/validate_plugin_tree.py`](../scripts/validate_plugin_tree.py), rule `SPEND_FIGURE`)
rejects currency amounts anywhere in the tree, so this baseline is expressed as shares and ratios
throughout — which is also what a future run needs in order to be compared against it.

## Redaction record

Two redactions are already settled and need no operator judgment: the repository under work is
named nowhere here, and no currency amount appears anywhere (the tree validator enforces the
second — run `python3 scripts/validate_plugin_tree.py` after any edit to this file).

TODO(operator) — list what else was removed from the private document and why, at the granularity of
"repository name", "account identifiers", "unrelated project material". A reader should be able to
tell that nothing load-bearing for the figures was removed.
