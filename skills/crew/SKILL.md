---
name: crew
description: Run every ticket of a spec unattended — parallel Claude and Codex children in tmux worktrees, routed per ticket, wave by wave, onto a throwaway integration branch.
disable-model-invocation: true
---

# Crew

`/crew <run-dir>` runs the tickets of the run directory `/route` staged, unattended, and hands back
an integration branch to review. Typing it is the run's whole sign-off: the wave
table the driver validates from each ticket's `## Routing` section is the sole routing authority
([ADR-0003](../../docs/adr/0003-script-composed-first-turns-wave-table-authority.md)).

Your work is judgment: rule on what a child asks, and on what the driver's rule table has no row
for. The driver runs the rest — preflight, dispatch, receipts, settlement, merges, tracker closes,
advancement, the report — in a tmux window of its own, costing you no turn while it works
([ADR-0010](../../docs/adr/0010-the-driver-runs-the-run-the-coordinator-rules.md)).

## Contract

Terms are the glossary's ([`docs/glossary.md`](../../docs/glossary.md)).

**Reversibility is the authority boundary.** You may approve any reversible action —
production, remote, deployment, external-service and acceptance runs included — and must record
the action's exact undo inside the ruling that approves it. Whether a ticket's acceptance runs at
all is your ruling too; its credentials come from the project's own convention, and one the run
lacks is parked. An action with no credible undo is **parked** for the human. The red-line hook
blocks known destructive command shapes; this rule stands where the hook misses.

**Judgment material is read whole; facts are delegated** — the coordinator may use `Read` on the
whole maintainer-authored Markdown that carries decisions: this run's ticket stubs and `spec.md`,
the repository's ADRs, glossary, `CONTEXT.md` and tracker conventions, and the Crew skill and its
references. The hook compares their physical locations. Code, tests and git remain facts for the
witness brief, which checks every pointer the child cites and recommends nothing. Where the child
and the brief state a fact differently, one bounded `Read` settles it: that pointer, an explicit
offset, 80 lines at most. `Grep`, `Glob` and shell file reads are hunts and the hook refuses them;
`gh issue view` stays open because the tracker is the ticket. A message lacking what a ruling needs
is answered by asking its sender for exactly what it lacks.

**A ruling is one decision and its action** — pick an option, redirect, or sketch pseudocode —
and the child writes the edit: product code belongs to a child, merge-conflict resolution included.

**Leftovers are yours to place.** A `wrap-up` lists what a finished ticket leaves behind; rule
each line into this ticket, a new ticket you open seeded from that line, or dropped.

**The human hears from you for a decision that is theirs** — product direction, a material change
of architecture, a widening of product scope, an action with no undo — and asks you for anything
else.

## Start the run

Run the launch script as a background task — this session's one proactive turn:

```bash
python3 <crew-skill-dir>/assets/launch/launch.py <run-dir>
```

`<crew-skill-dir>` is the absolute directory this `SKILL.md` loaded from. The launch script reads
your pid, session name, session ID, and permission mode off the harness's own records, puts the
driver in a tmux window of its own, and stays behind as a waiter that blocks until the run has
something to say — so this step costs one tool call. Where it cannot read one of the four values,
it stops and names the flag that supplies the value by hand: get that value from the user, because a
wrong pid or name strands every ruling, a wrong session ID scopes coordinator-only hooks to the
wrong session, and a wrong mode launches every child outside the mode a message can cross.

The background task you are left holding is only the waiter. The driver is not yours to lose:
whatever happens to that task — an interrupt, a compaction, this session ending — the run carries
on, and typing `/crew <run-dir>` again puts another waiter on it without starting a second driver.

## Rule when it wakes you

The waiter ends when the driver leaves one JSON wake snapshot, and that object is the whole of
what you read:

- `judgment-needed` or `driver-error` — rule on it, following
  [`references/triage.md`](references/triage.md), then put the loop back with the snapshot's
  `resume` command, in the background as above — it puts the driver back in a window of its own,
  exactly as the start did.
- `preflight-failed` — tell the operator the count and the surface the snapshot names, and stop;
  they fix and commit in their own session, then type `/crew` again.
- `run-complete` — one sentence pointing at the `report` path. The run is over: the driver has
  already cleared what landed, and the parked and failed work it left standing is listed in the
  report for the operator's own terminal command to clear. Where the snapshot's `cleanup` field is
  not null, say what it names too: that much of the site is still standing.

Not every ending is a snapshot. Where a plain line comes back instead of JSON, the run's driver
ended without asking you for anything: report that line as it stands and stop.

An interrupted run — a crash, a killed driver, a restarted session — resumes by re-typing `/crew
<run-dir>`: start adopts a run already under way rather than beginning a second one. The operator
sees the same thing on their dashboard, which carries `✖ driver dead — /crew <run-dir> to resume`
whenever the run's driver was killed rather than exiting.
