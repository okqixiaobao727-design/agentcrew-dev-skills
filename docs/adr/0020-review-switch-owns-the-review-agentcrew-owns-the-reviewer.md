---
status: accepted
supersedes: ADR-0009
---

# Review-Switch owns the review, AgentCrew owns the reviewer

ADR-0009 kept a Vendored Copy of the review bridge honest with a pin, a sync script, and a CI
drift check. It held the copy in place and let the coupling run the other way: upstream's own
source grew `--machine-log` and `--ticket` flags, a `session-cost` line spelled in this log's
vocabulary, and a hook keyed on a file named `agentcrew.toml`. Review-Switch is now called
across a process boundary (its ADR-0002), so this repo ships no review bridge at all.

What stays here is the reviewer: which vendor, which model, which effort, which account — the
wave table's review cell, resolved at the table boundary like every other routing key
(ADR-0014) and rendered into the child's first turn as arguments. With it stays what only a run
knows: the ticket's base commit, the ticket's path, and where in the ticket a review happens.

## Consequences

- `skills/crew/assets/review/` and `scripts/sync-bridge.sh` go, with their suites — 5,816
  lines. There is no drift check to maintain because there is no copy to drift, and CI loses
  its only step that reaches the network.
- The `review` event's writer changes; its shape does not. The bridge wrote it because it alone
  knew both ends of a review deterministically, and that argument survives — so the writer
  becomes a Lifecycle Hook this repo configures: a command in config, not code here.
- The `[review]` rounds paragraph in `shapes.toml` goes. The cap is enforced upstream and the
  next permitted action arrives with each result, which is also how it reaches a Codex child.
  `CREW ASK` stays this repo's word for the escalating act.
- Cross review is unchanged. Choosing a reviewer from the other vendor is a routing decision,
  and routing did not move.
