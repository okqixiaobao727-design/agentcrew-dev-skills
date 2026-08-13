# Resume an interrupted run

Reached when the feature already has a `crew/<feature-slug>` branch. Reconstruct the run
before launching or deleting anything.

## Reconstruct

Join these sources by ticket, branch, and exact worktree path:

1. Every ticket's status, **read** through the tracker `docs/agents/issue-tracker.md` names.
2. `claude agents --json` for exact recorded worktree paths, and `<bridge> watch` over the state
   files in `<feature-dir>/.crew-codex/` for the Codex children.
3. The integration and `worktree-*` branches, their merge state, and worktree changes.
4. `decisions.md` for previous answers and outside-worktree effects.
5. `tmux list-windows -a -F '#{window_id} #{window_name} #{pane_current_path}'` for window ids and
   `<NN>✓` / `<NN>?` markers.

Classify every ticket:

- merged into the integration branch → completed
- live `busy` or `waiting` → adopt and monitor
- live `idle` → validate the completion receipt from step 5 of `SKILL.md`; prompt it to finish
  when invalid
- branch or changed worktree without a live session → launch a replacement child in that same
  worktree in that ticket's step 4 launch shape and workflow shape and tell it to continue;
  preserve existing work. A Codex replacement started without its old thread id is a fresh child
  that inherits no routing, so read `--model` and `--effort` from the ticket's `## Routing` again
- branch without its worktree → recreate the worktree on that branch, then continue it
- no branch, worktree, or session → not started
- prior parked marker → parked; its unlaunched descendants are blocked

Do not infer completion from a plausible-looking commit. Use the receipt and Git checks from the
main skill.

## Re-anchor adopted children

The resuming coordinator has a new pid, so every adopted child's trust anchor points at a dead
socket, and its refusal of the new socket's messages is the anchor working. Re-anchor each
adopted child through its tmux pane — the trusted channel — with the new coordinator name and
`uds:/tmp/cc-socks/<new pid>.sock`, and record the child's `name` and `pid` afresh from
`claude agents --json`.

An adopted Codex child needs no re-anchor: its channel is a state file on disk, which the new
coordinator opens as the old one did.

Rebuild the remaining waves and show them for approval because the live run has diverged from the
previously approved table.

**Done when** every ticket and run artifact is accounted for, existing work is preserved, and the
user has approved the reconstructed wave table.
