# Resume an interrupted run

Reached when the feature already has a `crew/<feature-slug>` branch. Reconstruct the run
before launching or deleting anything.

## Reconstruct

Join these sources by ticket, branch, and exact worktree path:

1. Every ticket's status, **read** through the tracker `docs/agents/issue-tracker.md` names.
2. `<feature-dir>/.crew/` — the previous run's directory: `wave-table.json` for what was approved,
   `log.jsonl` for every launch, receipt, merge, escalation, ruling and advance decision it
   recorded, `launch/` for the turns its children received.
3. `claude agents --json` for exact recorded worktree paths, and `codex_bridge.py watch` over the
   state files in `.crew/codex/` for the Codex children.
4. The integration and `worktree-*` branches, their merge state, and worktree changes.
5. `tmux list-windows -a -F '#{window_id} #{window_name} #{pane_current_path}'` for window ids.

Classify every ticket:

- merged into the integration branch → completed
- live `busy` or `waiting` → adopt and monitor
- live `idle` → check its completion receipt with the verify command in step 5 of `SKILL.md`;
  prompt it to finish when the check fails
- branch or changed worktree without a live session → re-dispatch that ticket's wave from the
  rebuilt table, which launches a replacement in that same worktree on the routing the table
  approved; existing work in the worktree is preserved
- branch without its worktree → recreate the worktree on that branch, then continue it
- no branch, worktree, or session → not started
- a `parked` verdict in the log, or its worktree listed in `.crew/parked-paths` → parked; its
  unlaunched descendants are blocked

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
previously approved table, then write the rebuilt table back to `.crew/wave-table.json`: it is
what every script the resumed run calls reads.

## Restore the dashboard

Run the dashboard window command from step 4 of `SKILL.md` over the adopted run. The window the
interrupted run recorded is usually gone with it; the command recreates it and records the new id,
and where the window survived it prints that id and changes nothing. The resumed run ends with the
one dashboard every run has, without your judgment being spent on which case this was.

**Done when** every ticket and run artifact is accounted for, existing work is preserved, the run
has its one dashboard window, and the user has approved the reconstructed wave table.
