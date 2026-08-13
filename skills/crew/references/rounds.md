# Rounds contract

The contract every reviewed child works its findings under. Quote it word for word into the
`<review block>` you fill from [`workflows.md`](workflows.md), whichever reviewer the ticket's
`Review` lane names — the reviewed child is the party that reads it, so the same text closes both
variants:

```text
Rounds. Classify each finding on two axes: standards — style, naming, convention, anything that
leaves behaviour intact — and spec — correctness, security, deviation from the spec or ticket.
Fix the standards findings you accept in one pass; they are done without re-review. Spec findings
that required fixes get one re-review, scoped to exactly those fixes. Most reviews end clean after
the first pass — the re-review is a cap, not a stage to fill.
A spec finding still open after that re-review, or a finding the reviewer reopens after you ruled
on it, ends the review: send your coordinator a CREW ASK carrying both positions, rather
than opening another round.
```

Each bridge in `assets/review/scripts/` states the same contract to the reviewer it opens, so both
ends of a review hold one contract and neither end resolves a skill name to find it.
