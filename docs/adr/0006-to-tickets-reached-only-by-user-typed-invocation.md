---
status: accepted
---

# `/to-tickets` is reached only by user-typed invocation; the overlay only adds, and the judgment core moves to a point-of-use reference

`/route`'s spec-only mode needs `/mattpocock-skills:to-tickets`, but upstream gates that skill
deliberately: `disable-model-invocation: true` blocks the `Skill` tool, the skill's description
never enters model context, and the same intent is mirrored into its Codex policy file. The gate
exists because the skill publishes to a real tracker behind a mandatory approval round. Meanwhile
the old design had `/route` suppress that skill's own approval and publication steps and run its
own — a direct violation of the Matt-first principle ("the overlay adds rules; it never overrides")
— and told the agent to read the gated skill's `SKILL.md` and follow it when invocation failed.

## Considered Options

- **Read the gated skill's file and follow it** (the de-facto behaviour) — works, but a published
  plugin teaching Claude "when a skill blocks you, read its file instead" normalises working around
  a deliberate upstream gate. Rejected.
- **A child session opened with the slash command** (`claude -p` expands a leading slash command,
  bypassing the gate) — loses the parent's context and adds dispatch machinery, and is still a
  bypass. Rejected.
- **Prompt the user to type the command** — one line of typing, no context loss, and the gate is
  honoured as designed. Chosen.

The shape that follows: `/route` in spec-only mode is a pre-loader. It settles the environment,
prints the exact command line for the user to send — with a tail note pointing at the routing
rules, so the note and the skill body share one user message — and the `to-tickets` run itself
cuts, quizzes, and publishes, with `/route`'s rules as **additions** carried into each of its
steps. One approval and one publication remain, both owned by `to-tickets`. Its own "apply
`ready-for-agent` unless instructed otherwise" is the seam the role-string addition slots into.

**The judgment core moves to `references/classify.md`, read at the point of use** — superseding
ADR-0005's body-structure bullet ("a pointer would make applying a judgment test a coin-flip").
Two facts that bullet did not weigh:

- In spec-only, the `to-tickets` body arrives as a user message *after* the resident skill body, so
  resident judgment material sits at a recency and weight disadvantage exactly when it must bind.
  A pointer fired at drafting time lands the material newest, at the step that consumes it.
- The coin-flip risk belongs to weakly-worded pointers. Both branches reach `classify.md` through a
  mandatory Read written into the step that needs it, which is deterministic.

## Consequences

- `/route`'s resident body shrinks to a thin router; `classify.md` is the single source for the
  tests, table shape, and `## Routing` template, and `spec-only.md` (absorbing the old cutting
  overlay) is the single mode-gated branch file.
- `trackers.md` loses its **publish** operation — publication is `to-tickets`' alone; `/route`
  reads, edits, and marks.
- A repo without mattpocock-skills installed has no spec-only mode: `/route` says so and classifies
  only existing tickets.
- The spec-only branch ends with a verification pass over the published tickets, so its completion
  criterion is checkable by `/route` rather than assumed from the upstream run.
- ADR-0005 stands except its body-structure bullet, superseded here.
