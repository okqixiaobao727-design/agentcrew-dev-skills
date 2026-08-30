---
status: accepted
supersedes: ADR-0009
---

# Review-Switch owns the review, AgentCrew owns the reviewer

_Amended 2026-08-27 by #139 after review-switch#39 and its ADR-0009 made the caller's
run-again budget explicit._

Review-Switch is called across a process boundary (its ADR-0002), so this repo ships no review
implementation. Its installed command owns the protocol, recovery and round policy; AgentCrew
supplies only the run-specific arguments and Lifecycle Hook commands that the process boundary
accepts.

What stays here is the reviewer: which vendor, which model, which effort, which account — the
wave table's review cell, resolved at the table boundary like every other routing key
(ADR-0014) and rendered into the child's first turn as arguments. With it stays what only a run
knows: the ticket's base commit, the ticket's path, and where in the ticket a review happens.

## Consequences

- CI validates only artifacts this repository owns and installs no review-only dependencies.
- The `review` event's writer changes; its shape does not. The bridge wrote it because it alone
  knew both ends of a review deterministically, and that argument survives — so the writer
  becomes a Lifecycle Hook this repo configures: a command in config, not code here.
- The `[review]` hand-written rounds paragraph in `shapes.toml` goes. Review-Switch owns the
  protocol and names the next permitted action with each result, which is also how it reaches a
  Codex child. What stays is the caller's budget, stated once in `[review]`: A `run again` axis is
  run again at most once during this ticket's only review; past that the child sends `CREW ASK
  <NN> stuck` with its reason. `CREW ASK` stays this repo's word for the escalating act.
- Choosing a reviewer is a routing decision, and routing did not move. This ADR originally required
  the reviewer to come from the other vendor; [ADR-0027](0027-a-review-lane-is-independent-not-necessarily-cross-vendor.md)
  later removed that constraint while preserving Reviewer independence.
