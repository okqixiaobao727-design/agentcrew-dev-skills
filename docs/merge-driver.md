# The merge driver

Landing a wave without waking the coordinator. Every branch whose receipt says `landable` is
merged into the integration branch, in ticket order, `--no-ff`, by script alone. A conflict
climbs the [escalation ladder](adr/0004-escalation-ladder-script-then-sonnet-then-coordinator.md)
instead of interrupting a judgment session.

The driver is [`skills/crew/assets/merge_driver.py`](../skills/crew/assets/merge_driver.py).

```sh
merge_driver.py land --table <wave table> --wave N --log <machine log> \
                     --repair-model <full model ID> \
                     [--repair-budget-usd 2] [--repair-attempts 2] [--repair-timeout 900]
```

The wave table is the dispatch renderer's — the same file, the same authority (ADR-0003) — and
the driver reads two things from its `run` section, `repo_root` and `integration_branch`, plus
the tickets of the named wave. A ticket's branch is not table data: it is the name the renderer gave
it, derived by the same code, so the driver merges exactly what dispatch created.

`--repair-model` has no default on purpose. The repair rung's model is a routing decision, and a
model ID compiled into a script is a routing decision nobody approved; the caller passes the full
ID it approved. An alias is refused before anything is merged, on the same rule the renderer
applies (ADR-0003).

## What is landed

Verdicts come from the run's machine log — the `receipt` events the monitor wrote — and only
`landable` merges:

| Receipt | What happens |
| --- | --- |
| `landable` | merged `--no-ff` into the integration branch |
| `failed`, `parked` | never merged; the ticket is reported `skipped` |
| none | never merged; nothing verified this branch, which is not the same as it being landable |

A receipt is a sha: it says a script checked *this commit*, and a branch is a ref that can move
off it. A `landable` branch is merged only while it still points at the commit its receipt names.
A branch that has moved, and a receipt that names no sha at all, both carry work nothing has
verified, and both escalate rather than merge.

Tickets are landed in ticket order — by number, ascending — whatever order the table lists them
in. A ticket that escalates does not hold up the ones behind it: the wave lands everything it can,
and the coordinator rules on what is left. The wave ends with the integration branch checked out
at the last merge it landed, with no merge left in progress.

A repository with uncommitted changes to tracked files is refused before the first merge, because
a merge that starts on top of somebody's work cannot be aborted back to where it began.

## The conflict classifier

A conflicted merge is classified before anything is rewritten or repaired. The merge runs under
`merge.conflictStyle=diff3`, which keeps the merge base in each conflict hunk, and whether that
base section is empty is the whole test:

**`mechanical`** — every conflicted path is a content conflict (base, ours and theirs all
present) and every hunk in it has an empty base section. Both sides only inserted at the same
point; neither touched a line the other touched.

**`semantic`** — anything else:

- a hunk whose base section carries lines: both sides rewrote the same existing code;
- a file both sides created: two children each wrote their own version of it;
- a file one side deleted and the other changed;
- a conflicted file with no readable conflict markers at all.

The line the classification draws is the one ADR-0004 draws: a mechanical conflict is textual and
nothing about the two designs disagrees, so anything that resolves it correctly resolves it. A
semantic conflict is two children's designs meeting, and which one stands is a ruling.

## The ladder

1. **Script.** The merge itself, and the resolution of a conflict the classifier called mechanical.
   A clean merge is logged and nothing else happens. A mechanical conflict is rewritten by the
   driver: in every conflicted file, each hunk becomes ours' insertion followed by theirs', the
   markers and the empty base section removed. The classification has already proved that keeps
   everything both sides wrote and decides nothing, so no model is asked to do it — the merge is
   staged, committed and logged `resolved`, and the wave carries on.

   The driver refuses to rewrite a file whose markers do not open and close in order, or whose own
   text carries a line that reads as a marker — `=======` is also how prose underlines a heading.
   Nothing is written until every conflicted file has been rewritten, so a file it refuses leaves
   the whole conflict standing exactly as git wrote it, and that conflict goes to rung 2.
2. **Repair session.** A mechanical conflict the driver's own rewrite refused is handed to a
   headless, budget-capped session the driver launches itself, in the repository, on the conflict
   standing in it:

   ```sh
   claude --print <the conflict brief> --model <full model ID> \
          --max-budget-usd <cap> --permission-mode acceptEdits
   ```

   `--max-budget-usd` binds only on a headless (`--print`) session, so the two flags travel
   together; the cap defaults to two US dollars. The session is asked to keep both sides'
   additions and to run no git command at all: staging, committing and the merge itself belong
   to the driver, so the session needs no permission to touch the repository's history.

   An attempt counts as failed if the session exits nonzero, overruns `--repair-timeout`, changes
   anything outside the conflicted paths it was handed, leaves any of the four conflict markers
   standing in one of them, or leaves any path unmerged. What counts as a change outside is
   compared by content digest, so a file rewritten in place is caught along with a file newly
   created. `=======` is also how prose underlines a heading, so a file that really contains that
   line cannot be repaired by a script and goes to the coordinator: the driver refuses to commit a
   file it cannot tell apart from a half-resolved one.

   A second attempt starts from the conflict as git left it, not from what the first session made
   of it — and it is measured against the working tree as it stood before the first session ran,
   so one session's stray file cannot become the next one's baseline.
3. **Coordinator.** A semantic conflict skips rung 2 entirely; a mechanical conflict that
   `--repair-attempts` sessions could not resolve exhausts it. Either way the merge is aborted,
   the branch is left unmerged, and one `escalated` event is written carrying the pointers a
   ruling starts from: the reason, the ticket's path, its branch, and the conflicted files.

No model runs on rung 1, and the coordinator is not consulted on rung 1 or 2.

## What lands in the machine log

One `merge` event per step, through the log's own writer, in the schema
[`docs/machine-log.md`](machine-log.md) publishes:

| Path through the ladder | Events |
| --- | --- |
| clean merge | `clean` |
| mechanical conflict, resolved by the driver | `conflict`, `resolved` |
| mechanical conflict the driver would not rewrite, repaired | `conflict`, `repaired` |
| mechanical conflict, repair exhausted | `conflict`, `escalated` |
| semantic conflict | `conflict`, `escalated` |

`resolved` and `repaired` are separate words on purpose: a merge the driver settled itself cost no
model anything, and a log that spelled them alike could not say so.

A skipped ticket writes no `merge` event: nothing was merged, and a log that records a merge that
did not happen is a log a later agent cannot trust.

## Output and exit code

One line per ticket, which is the whole of what a caller reads:

```
07 clean <sha>
08 resolved <sha>
09 repaired <sha>
10 escalated <reason>; ticket <path>; branch <branch>; conflicted <paths>
11 skipped failed
```

Exit 0 when every landable branch landed, 1 when any ticket escalated or the wave could not be
started at all.
