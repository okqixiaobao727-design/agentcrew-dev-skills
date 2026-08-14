---
status: accepted — the body-structure bullet is superseded by ADR-0006
---

# /route stays a resident interactive skill; its cost is governed by frontmatter pins and body structure, not forking

Applying the ADR-0001 coordinator-cost lessons to `/route` (issue #5) forced the question
of whether its skill body should leave the resident context the way `/crew`'s mechanical
sections did. Verified against the Claude Code docs: a `context: fork` skill runs as a
regular subagent, and the subagent tool filter strips `AskUserQuestion` from every regular
agent type — a forked skill cannot run a multi-round approval loop. `/route`'s single
checkpoint (the user approving the routing table before the first write) is the skill's
contract, so forking is structurally unavailable. `/route` also lacks `/crew`'s cost
shape: it is one short interactive session, not a hundred-turn run re-reading its prefix,
so the full scripted-mechanical-layer treatment is over-engineering — as issue #5 itself
anticipated.

We decided `/route` stays resident and interactive, and its cost is governed three ways:

- **Frontmatter pins.** The skill carries `model` and `effort` in frontmatter, with the
  model as a full ID. The ADR-0003 lesson — routing must not depend on what model the
  environment happens to resolve — applies to the session that *makes* routing decisions,
  not only to the sessions it routes.
- **Full model IDs at the config source.** The shipped defaults and every per-repo
  `agentcrew.toml` carry full model IDs in their cells. The whole chain — config cell →
  `## Routing` line → launch command — stays verbatim, with no alias-resolution table to
  maintain and no way for plan mode to mis-resolve an alias (ADR-0003 consequence).
- **Body structure.** Judgment material every run needs — the workflow enum tests, the
  core/complex/spike axis definitions, the checkpoint rules, each step's completion
  criterion — stays in the resident body; a pointer would make applying a judgment test a
  coin-flip. Content only one mode reaches (the spec-only cutting overlay) is disclosed
  behind a reference loaded by that mode. The case→cell mapping lives as comments beside
  the cells in the config file, which step 1 reads every run anyway, so the mapping and
  its cells cannot drift apart. Rationale prose and restatements of what the config file
  already documents are pruned rather than kept resident.

## Consequences

- Any future skill with a user-approval checkpoint has the same constraint: `context:
  fork` removes the ability to ask, so interactivity and forking are an either/or at the
  skill level.
- Tickets #6–#11, published with alias model values before this decision, are left as-is
  deliberately: their runs were already in flight, and editing a running ticket's routing
  would change a live contract.
- The resident body of `/route` shrinks to roughly its judgment core (~120 lines), with
  the cutting overlay in a mode-gated reference.
