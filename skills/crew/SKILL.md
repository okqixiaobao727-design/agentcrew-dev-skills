---
name: crew
description: Run every ticket of a spec unattended — parallel Claude and Codex children in tmux worktrees, routed per ticket, wave by wave, onto a throwaway integration branch.
disable-model-invocation: true
---

# Crew

`/crew <path-to-feature-dir>` runs the tickets `/to-spec` and `/to-tickets` produced, unattended,
and hands back an integration branch to review. Typing it is the run's whole sign-off: the wave
table the driver validates from each ticket's `## Routing` section is the sole routing authority
([ADR-0003](../../docs/adr/0003-script-composed-first-turns-wave-table-authority.md)).

Your work is judgment: rule on what a child asks, and on what the driver's rule table has no row
for. The driver runs the rest — preflight, dispatch, receipts, settlement, merges, tracker closes,
advancement, the report — as a background task of this session, costing you no turn while it works
([ADR-0010](../../docs/adr/0010-the-driver-runs-the-run-the-coordinator-rules.md)).

## Contract

**Reversibility is the authority boundary.** You may approve any reversible action, including
production, remote, deployment and external-service changes, and must record an action's exact undo
inside the ruling that approves it. An action with no credible undo is **parked** for the human.
The red-line hook blocks known destructive command shapes; this rule stands where the hook misses.

**You rule from what a message shows you.** The run's files — the wave table, the machine log, a
child's diff, a worktree — stay closed, and nothing puts them in front of you. A message lacking
what a ruling needs is answered by asking its sender for exactly what it lacks.

**Product code belongs to a child**, merge-conflict resolution included: answer with design
direction or pseudocode, and the child writes the edit.

## Start the run

Launch the driver as a background task — this session's one proactive turn:

```bash
python3 <crew-skill-dir>/assets/driver/driver.py start \
  --feature-dir <feature-dir> --coordinator-name <this session's own name> \
  --coordinator-pid "$PPID" --permission-mode <this session's own permission mode>
```

`<crew-skill-dir>` is the absolute directory this `SKILL.md` loaded from. `$PPID` is this session's
own process, the anchor a child authenticates your rulings against, and children launch in your
permission mode because a message crosses only between sessions of one mode.

## Rule when it wakes you

The driver exits with one JSON wake snapshot, and that object is the whole of what you read:

- `judgment-needed` or `driver-error` — rule on it, following
  [`references/triage.md`](references/triage.md), then put the loop back with the snapshot's
  `resume` command, in the background as above.
- `preflight-failed` — tell the operator the count and the surface the snapshot names, and stop;
  they fix and commit in their own session, then type `/crew` again.
- `run-complete` — one sentence pointing at the `report` path. The run is over, and clearing it is
  the operator's own terminal command.

An interrupted run — a crash, a killed driver, a restarted session — resumes by re-typing `/crew
<feature-dir>`: start adopts a run already under way rather than beginning a second one.
