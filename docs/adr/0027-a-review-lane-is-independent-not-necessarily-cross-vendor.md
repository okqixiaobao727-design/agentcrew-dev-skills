---
status: accepted
---

# A Review lane is independent, not necessarily cross-vendor

Every tdd and refactor ticket still receives a fresh independent Reviewer session through
Review-Switch, but the Reviewer's vendor is independent of the Implementer's vendor. Claude to
Claude, Claude to Codex, Codex to Claude and Codex to Codex are all valid Review lanes. A
same-vendor review never reuses the Implementer's session or turns self-review into an accepted
lane.

AgentCrew owns this routing choice at both of its existing decision surfaces. A project's reviewer
cells may configure any of the four combinations, and the user may change a ticket's lane at the
`/route` confirmation checkpoint. The validated Run plan carries the selected vendor, model and
effort without comparing the Reviewer vendor to the ticket's Executor.

The shipped defaults remain cross-vendor. This is a new configurable capability, not a silent
change to the review policy of existing projects. **Review lane** is the general term;
**cross-vendor review** and **same-vendor review** name whether that lane's vendor differs from the
Implementer's.

## Consequences

- The same-vendor rejection in Run-plan validation is removed; all other Review-lane validation
  remains, including the required independent lane, full model ID and valid effort.
- Review-Switch continues to own review execution and recovery. AgentCrew continues to own only the
  Reviewer selection and run-specific arguments, as ADR-0020 requires.
- The four vendor combinations require explicit coverage at the Run-plan, route/staging, dispatch
  and reporting seams; existing downstream support is not treated as end-to-end proof.
