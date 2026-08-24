---
status: accepted
supersedes: ADR-0009
---

# Review-Switch owns the review, AgentCrew owns the reviewer

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
- The `[review]` rounds paragraph in `shapes.toml` goes. The cap is enforced upstream and the
  next permitted action arrives with each result, which is also how it reaches a Codex child.
  `CREW ASK` stays this repo's word for the escalating act.
- Cross review is unchanged. Choosing a reviewer from the other vendor is a routing decision,
  and routing did not move.
