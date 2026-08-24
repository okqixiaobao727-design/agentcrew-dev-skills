# AgentCrew design

> Hand it a spec at night; find an integration branch to review and a decision log in the morning.
> Short of destroying data, it pushes everything forward on its own.

`/route` turns a spec into routed tickets. `/crew` runs those tickets as waves of child agents,
each in its own tmux window and its own worktree, until the spec is done. Terms are in
[`glossary.md`](glossary.md); the two standing architecture decisions are in [`adr/`](adr/).

## 1. What it stands on

AgentCrew invents no process. It automates the closing line of Matt's `/to-tickets`:

> "Work the frontier one ticket at a time with `/implement`, clearing context between tickets."

with one difference: **the whole frontier runs in parallel, not one ticket at a time.**

Upstream contracts, all from mattpocock-skills and all unmodified (see the Matt-first principle):

| Stage | Produces |
|---|---|
| `/to-spec` | the spec, with the test seam settled in advance |
| `/to-tickets` | vertical-slice tickets carrying `Blocked by:` and `Status:` |
| `/implement` | implement → `/tdd` → typecheck + full test run → code review → commit |
| code review | two-axis findings, each carrying Review-Switch's next permitted action |

**The issue tracker is configurable.** Both skills read the repo's `docs/agents/issue-tracker.md` to
learn where tickets are read from and where status is written back, and hard-code neither. Two
trackers are exercised: GitHub issues, and local markdown files — where every ticket operation is a
file read or write in the working tree, and `/crew` writes each finished ticket's `Status:` line
instead of closing an issue. Every other tracker runs on its convention document alone and is declared
untested; the operations are in [`trackers.md`](../references/trackers.md).

## 2. The run loop

```
/crew <feature-directory>
     │
     ├─ read tracker config → read every ticket → parse Blocked by → build the graph
     ├─ validate: a cycle, or a reference to a ticket that does not exist → stop before launching
     ├─ show the wave table → wait for approval
     └─ cut crew/<slug> from the base branch
     │
┌────┤ each wave ───────────────────────────────────────────────┐
│    ├─ per ticket: create a worktree + a tmux window named for the ticket
│    ├─ write the red-line hook config into that worktree (§5)
│    ├─ launch the child in the window with the first prompt (§3); run the launch hook
│    │
│    └─ children report over the message channel:
│         escalation → the coordinator rules and answers (§4)
│         receipt    → verify the sha, merge into the integration branch, mark the window ✓
│         silence    → the monitor is the safety net (§6)
│                                                                │
└──── wave complete → cut the next wave's worktrees from the merged state ────┘
     │
     └─ report: completed / failed / parked, plus the decision log and durations
```

The base branch is untouched throughout. The final merge is yours.

## 3. The child's first prompt

Three things, none optional:

1. **The ticket path** — what to build.
2. **The spec path** — the basis for judging it correct, and the source of the pre-agreed test seam
   `/tdd` needs, so the child never stops to ask.
3. **The base commit** — code review needs a fixed point. Without one it stops and asks before a
   review lane opens.

Plus the scope note (*a hint, not a block*): work stays inside this worktree and this branch. And the
coordinator's own address on the message channel — the trust anchor must arrive with the first
prompt over the trusted channel, never be sent afterwards as a message.

## 4. Triage: how the coordinator handles what children raise

**One rule: answer everything, keep it moving. Nothing parks except the red line.**

The coordinator rules from the escalation message alone — it does not enter the child's worktree to
read files, because it is serving the whole wave at once. Children escalate in a fixed compact
format: ticket number, category, the question in one paragraph, two or three options with one
recommended. No pasted code or logs.

Every judgement goes into the decision log, and any action reaching outside a worktree gets its own
entry stating how to undo it.

**Why the axis is reversibility.** An earlier design triaged on whether an action left the worktree.
That axis is wrong: creating an empty table in a production database leaves the worktree but is
undone by one `DROP TABLE`, while dropping a table with data in it is equally outside and cannot be
undone. Orders of magnitude apart in risk; not the same category.

**Why parking was cut back to the red line.** With permissions open, a careless child never asks —
it just acts. A careful child that stops to ask would be the one parked for the whole night. That
incentive is backwards.

## 5. The red line

The one class of action that is not left to the model's judgement: **irreversible destruction of
data.**

Blocked: `DROP DATABASE` / `DROP SCHEMA` / `dropdb`; `DROP TABLE` against a non-test table;
`git push --force`; deleting a remote branch; `rm -rf` outside the worktree.

**Why the list stays narrow.** Testing against a real database means create fixture → assert → clean
up, which is `DELETE` and `TRUNCATE` throughout. A broad list kills exactly the tests worth running
first. Test cleanup destroys the data the test just made — reversible by construction, and squarely
inside the exception. **Known cost:** `DELETE FROM <production table> WHERE …` gets through, because
it looks identical to test cleanup.

**Where it lands.** The coordinator writes a `PreToolUse` hook config into each worktree as it is
created, so the block applies to the child only: your own sessions are unaffected, global config is
untouched, and deleting the worktree removes the config. The hook splits a command on `;`, `&&`,
`||` and `|` and judges each segment, so `git commit -m "drop table support"` is not caught, and
skips pure-text commands (`echo`, `cat`, …) outright.

**On a hit** the child is not crashed but told, through the permission decision reason, that the
action is irreversible and to find a reversible route or explain what it wants to delete and why. It
stops and asks; the coordinator catches it; if the deletion is genuinely unavoidable, the ticket
**parks** for a human.

## 6. Message channel, and the monitor behind it

Escalations and answers travel over cross-session messaging. A receipt does not: a child records
its own completion, failure or park in the run's machine log, which is where the driver verifies
receipts from, so an event nobody has to decide about never wakes the coordinator. tmux keeps three
jobs:
launching children, answering permission confirmations (messaging cannot approve permissions for the
receiver), and human takeover.

Message trust is deliberately light — enough to prevent crossed wires, not forgery. The anchor is
pid ↔ socket: the coordinator plants its own socket address in the first prompt, and checks incoming
mail against the cwd and pid it recorded at launch. The threat model is misdelivery, not an attacker.
Written any tighter, the rule makes children ask for human confirmation on every instruction.

A polling monitor stays as a safety net for the two cases messages cannot cover: a child stuck on a
permission confirmation (it cannot send) and a process that dies silently. It sets no timeout, and an
unknown state stays fail-closed.

## 7. Decisions and what each one gives up

| Topic | Decision | Given up |
|---|---|---|
| Child contract | follow `/implement` | review reports rather than blocks, so "green before commit" does not hold |
| Parallelism | dependency waves | the `Blocked by` edges must be right; wrong edges start a child on missing code |
| Merge gate | no verification before merging | red code lands, and the next wave cuts from a polluted baseline |
| Merge target | `crew/<slug>` | — |
| Child shape | full interactive TUI | no structured headless JSON with stop reason and cost |
| Triage | the coordinator answers everything | a wrong ruling propagates silently downstream |
| Permissions | fully open | last night's actions can be read afterwards, not prevented |
| Red line | narrow blacklist on irreversible deletion | `DELETE FROM <production table>` gets through |
| Resume | rebuild from ticket `Status:` plus the live session list | inferred rather than read; an ugly death reads as still running |
| Windows | one ticket, one window, renamed ✓ when done | windows and processes accumulate for a human to clear |
| Concurrency | no cap | — |
| Models | per-ticket, from the model tables in the crew config | — |

**Why there is no concurrency cap.** Most tickets are serial anyway — the dependency graph does the
limiting. A second, human-set ceiling is a redundant constraint.

**Why the models are configuration.** A ticket is a thin vertical slice with the test seam already
settled: execution, not design. But which vendor and model suits which quadrant depends on the
subscriptions you hold, so the tables are yours to edit per repo. The classification logic that
picks a table cell is not configurable — that is the product opinion.

## 8. Rejected alternatives

- **Parsing transcript `.jsonl` directly** — the format is internal to Claude Code and changes
  between releases, so any script reading it breaks on an upgrade.
- **Reading child state from screen scrapes** — one UI change and it is dead. Screen capture is used
  only after the official interface has already said a child is waiting, to read the question once.
  The judgement stays with the supported interface.
- **Subagents instead of tmux windows** — a subagent lives and dies with its parent session and
  cannot cross sessions, and cannot be taken over by a human mid-run.
- **Headless (`claude -p`)** — briefly chosen for its clean completion signal (process exit), on the
  assumption that interactive state could only be guessed from the screen. Once the official session
  interface proved otherwise, the assumption failed and interactive won back full visibility and
  takeover.
- **A resident teammate process** — rejected when no message channel existed between sessions.
  Cross-session messaging removed that constraint, and §6 adopts it as the primary channel.
- **Vendoring Matt's `/to-tickets` prompt text into `/route`** — it would drift from upstream and
  reads as appropriating his work. `/route` has the user type his skill's command and layers
  additions on top instead; the skill's own gate against model invocation is honoured, not worked
  around (ADR-0006).
- **A built-in integration for child launches** — see [ADR 0001](adr/0001-on-child-launch-hook.md).
- **Binding to mattpocock-skills through the repo name** — see
  [ADR 0002](adr/0002-description-layer-binding.md).

## 9. The thinnest layer

**Permissions are fully open and, apart from the red line, nothing is blocked.** The only defence is
a model choosing to stop and ask, and smaller models push on where larger ones would pause.

One mitigation, and it is nearly free: **the decision log must be complete.** It is the only trail
back.

A second known gap: `git push` is allowed, so "the base branch is untouched" holds by convention
alone. Nothing stops a child checking out the base branch and pushing. The scope note in the first
prompt is a hint, not a block.

Tightening is one edit away — put "park on any irreversible action" back into the triage rule. The
current stance is to run open and tighten when something burns, matching the decision not to verify
before merging.
