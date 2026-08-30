---
status: accepted
---

# A Run owns a dedicated Crew worktree

A fresh `/crew` requires the checkout from which it was invoked to be clean, then fixes the Run's
base at the current committed tip of its local base branch. The Run creates
`<repo>/.claude/worktrees/crew-<run-slug>` from that commit and the Driver performs every later Git
operation there. It neither updates the local base branch from its remote nor changes the invoking
checkout; once the snapshot is fixed, later edits in that checkout do not affect the Run.

The **Crew worktree** is distinct from both the Coordinator and a Ticket worktree. The Coordinator
remains in the checkout that invoked `/crew` and continues to make judgments only. Ticket worktrees
remain siblings of the Crew worktree under `.claude/worktrees/`, rather than being nested inside it.
The original gitignored `crewtask/<n>` remains the durable Run handle and continues to own `.crew/`,
the Machine log and `report.md`; isolating Git operations does not relocate those records.

The Crew worktree and its Integration branch remain after completion, failure or interruption so
the operator can inspect the result or resume the Run. Only the existing explicit, confirmed
`clear` operation removes them; durable Run records and the report remain.

## Considered Options

- Moving the Coordinator session into the Crew worktree was rejected: its role is judgment, while
  the isolation problem belongs to the Driver's Git operations.
- Fast-forwarding the local base branch from its remote was rejected: it would still mutate the
  checkout the operator needs free for other work. A Run deliberately uses the local committed
  snapshot present at invocation.
- Relocating `crewtask/<n>` into the Crew worktree was rejected: it would turn the Run's stable
  recovery handle into an artefact whose lifetime is tied to a disposable checkout.
- Removing the Crew worktree automatically at completion was rejected: the final result must remain
  immediately inspectable until the operator chooses to clear it.

## Consequences

- The Integration branch remains the Run's merge target, but it is checked out only in the Crew
  worktree. The invoking checkout is never switched to it.
- The clean-checkout preflight is a start-time boundary. It does not become a continuing restriction
  on the invoking checkout while the Run is active.
- Run metadata must distinguish the repository and durable feature directory from the Crew
  worktree; one `repo_root` path can no longer stand for all three responsibilities.
