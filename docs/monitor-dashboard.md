# The monitor's operator surface

The wave monitor is what the human watches a run through. It has four parts, and none of them
costs a model token or reaches the coordinator's context
([ADR-0001](adr/0001-coordinator-spends-tokens-only-on-judgment.md)):

| Part | Script | What it does |
| --- | --- | --- |
| the wake-up | [`monitor-wave.sh`][wake] | armed while every child is busy, exits once one is not |
| the dashboard | [`monitor.py dashboard`][monitor] | draws the whole run as a table |
| the window | [`monitor.py window`][monitor] | gives the run its one dashboard window |
| the receipt check | [`monitor.py verify`][monitor] | decides whether a completion receipt holds |
| the cost pass | [`monitor.py cost`][monitor] | records what each child spent, and the run total |

[wake]: ../skills/crew/assets/monitor-wave.sh
[monitor]: ../skills/crew/assets/monitor/monitor.py

The wake-up is unchanged and keeps its contract: armed while every supplied child is `busy`, exit 0
the moment any child is `waiting`, `idle`, `parked` or `vanished`, nonzero on a monitor error. The
dashboard is a display, so a failure it meets is drawn rather than raised — it does not compete
with the wake-up for the job of stopping a run.

## The dashboard

```sh
monitor.py dashboard --run-dir <run dir> [--refresh SECONDS] [--toast-state PATH]
                     [--now TIMESTAMP] [--no-color]
```

One row per ticket of every wave, drawn from the run directory: the approved wave table
(`<run-dir>/wave-table.json`), the machine log (`<run-dir>/log.jsonl`,
[`docs/machine-log.md`](machine-log.md)), and one live-state source per lane — `claude agents
--json` for Claude children and the bridge state files `<run-dir>/codex/<NN>.json` for Codex
ones. It takes the run directory and nothing else — no wave number, no worktree list — so the
frame is the whole run from the first draw, and a ticket nothing has launched yet is drawn
`pending` rather than missing.

The spec's source list for this dashboard names three sources and stops at the agents list. A
Codex child never appears there — the bridge is the only thing that knows it is alive — so
reading the agents list alone draws every Codex ticket of a mixed run `vanished` and toasts it
lost, which is the very failure the run's realpath work exists to end. The fourth source is that
deviation, taken deliberately.

```text
crew crew-run-1 — wave 2/3 · pending=1 running=1 waiting=1 merged=1 · elapsed 00:41:07
WAVE  TICKET  TITLE                       EXECUTOR                         STATE    ELAPSED
1     06      Dispatch launch path        claude/claude-opus-4-5-20251101  merged   00:41:07
2     07      Path handling hardened      codex/gpt-5.6-luna               running  00:12:31
  ↳ review: codex gpt-5.6-luna running · 00:02:31
2     08      The run dashboard           claude/claude-opus-4-5-20251101  waiting  00:12:29
  ↳ last event: escalation · 2026-08-13T09:31:00Z
3     09      Skill copy and ADR          claude/claude-opus-4-5-20251101  pending  --
```

| Column | Where it comes from |
| --- | --- |
| `WAVE` | the wave the table lists that ticket in |
| `TICKET` | its `id` in the table |
| `TITLE` | its `title`, given the width the window has left over and cut with `…` to fit |
| `EXECUTOR` | its `executor` and `model`, as the table approved them |
| `STATE` | one word of the Ticket state vocabulary ([`docs/glossary.md`](glossary.md)) |
| `ELAPSED` | `HH:MM:SS` from the `launch` stamp to the settling stamp, or to now while live |

The summary line above the table is the run at a glance: the run id (the run directory's own
name), how many of its waves have been launched, a count per state, and the run's total elapsed
time from its first launch.

### States

Every source state is mapped into the Ticket state vocabulary before it is drawn, so the operator
never reads an internal word:

| Drawn | What it is mapped from |
| --- | --- |
| `pending` | the log carries no `launch` for that ticket, or an `outcome` of `blocked` |
| `running` | its lane's source says `busy` |
| `waiting` | its lane's source says `waiting` or `idle`: a child that stopped short |
| `parked` | a `receipt` verdict or an `outcome` of `parked`, or the agents list says so |
| `landable` | a `receipt` verdict of `landable` |
| `merged` | a `merge` result of `clean` or `repaired`, or an `outcome` of `completed` |
| `failed` | a `receipt` verdict or an `outcome` of `failed` |
| `vanished` | it was launched, nothing settled it, and its lane has no live entry for it |

The last settling line the log carries wins, not the first: a landable branch is merged next, and
where the ticket is *now* is what the operator is looking for. A settled row stops following its
lane's live source and its elapsed time stops moving. Worktrees are compared by realpath, never as
strings, so a `/tmp` spelling and a `/private/tmp` one are one worktree.

### Annotation rows

Normal rows stay quiet; a row that owes an explanation carries it underneath, so the table never
has to grow a column:

| Annotation | Drawn when |
| --- | --- |
| `↳ review: <lane> <state> · <elapsed>` | the log's last `review` line says a review is `running` |
| `↳ anomaly: duplicate · more than one session in <worktree>` | one worktree, two sessions |
| `↳ anomaly: unknown · <source> could not be read` | the lane's source failed or is unknown |
| `↳ last event: <event> <word> — <detail> · <stamp>` | the row is `waiting`, `failed`, `vanished` |

`duplicate` and `unknown` are annotations rather than states: both say what a reading did, not
where the ticket got to, so the row keeps the state its own log lines justify.

The `review` event has no writer yet — the dashboard reads it, and the review-running workflow
will write it. Until then the review annotation is drawn only for a log a future writer fills in;
nothing else about the frame depends on it.

### Colour and width

States are drawn in colour when a terminal is watching and plain everywhere else — a pipe, a
redirect, `--no-color`, or `NO_COLOR` in the environment all give plain text, which is what the
tests read. The title column absorbs whatever width the window has left after the other five, so
titles are as readable as the terminal allows.

`--refresh` turns the single render into the window's loop: the same frame, redrawn over itself
every so many seconds. Once the log carries an `advance` decision of `complete`, `escalated` or
`interrupted`, the run is over: the renderer draws its last frame, stops refreshing, and stays
alive holding it — nothing here ever closes the window. `--now` fixes the moment elapsed times are
measured to, which is what makes a render reproducible.

## Toasts

Each pass emits a toast for anything that just became true, on `tmux display-message` — the
operator's terminal, which no model reads. Nothing is sent to the coordinator, and nothing is
written where a model would be handed it.

| Toast | Emitted when |
| --- | --- |
| `crew wave <N> complete` | every ticket of a launched wave is settled |
| `crew <NN> stuck at a permission prompt` | its lane's source says `waiting` |
| `crew <NN> stopped without finishing` | its lane's source says `idle` |
| `crew <NN> vanished` | a launched, unsettled row has no live entry at all |
| `crew <NN> escalated` | the log carries an `escalation` for that ticket |

Both rows a `waiting` state can come from are announced in their own words: a child at a
permission prompt and a child that finished a turn without settling anything need different things
from the operator. A wave nobody has launched into has not completed, so a run's later waves never
toast on the first frame. Each of these fires once per run: what has already been said is
remembered in the toast-state file — `--toast-state`, by default `<run-dir>/toasts.json` — so a
restarted window does not replay a run's exceptions, and a `--refresh` loop does not repeat itself
every few seconds.

## The receipt check

```sh
monitor.py verify --ticket <NN> --worktree <path> --sha <sha> --base <commit> [--log <path>]
```

The three things that make a child's completion receipt true, checked mechanically:

1. `<sha>` equals `git -C <worktree> rev-parse HEAD` over all 40 characters — children compose a
   receipt whose short prefix matches and whose tail is invented, so a prefix comparison is not a
   check.
2. the branch is ahead of `<base>`, so a receipt cannot be earned by committing nothing.
3. the worktree is clean apart from the guard assets the dispatch renderer installed into
   `.claude/` — those are the run's own residue, and the child never commits them.

```text
06 landable 4f1c…         # exit 0
06 invalid sha does not match worktree head <head>   # exit 1
MONITOR ERROR git rev-parse failed: …                # exit 3, on stderr
```

A landable verdict appends one `receipt` line to `--log`, which is what puts the ticket's state on
the dashboard. An invalid receipt appends nothing: the ticket is not settled by it — the crew
contract gives that child one more chance to finish or fail — and the log carries exactly one
receipt per launched ticket.

## The cost pass

```sh
monitor.py cost --log <machine log>
```

Run once at run completion. For every ticket the log carries a `launch` for, it reads the usage
out of the transcripts that ran in that child's worktree, appends one `session-cost` line per
child ([`docs/machine-log.md`](machine-log.md)), and prints the run's rollup for the report:

```text
TICKET  EXECUTOR  MODEL                     INPUT  OUTPUT  CACHE-READ  CACHE-CREATION  TOTAL
06      claude    claude-opus-4-5-20251101  24     46      6800        900             7770
07      codex     gpt-5.6-luna              1000   700     4000        250             5950
08      codex     gpt-5.6-luna              --     --      --          --              --
TOTAL   --        --                        1024   746     10800       1150            13720

08 not measured: no transcript under /Users/me/.codex/sessions was recorded in /repo/wt/08
```

Transcripts are found by the worktree they ran in, compared by realpath and path-component
containment: a cwd at or below the launch event's worktree is that worktree, while a parent,
sibling, or other outside path is not. Claude sessions are under `$CLAUDE_CONFIG_DIR` (default
`~/.claude`), Codex sessions under `$CODEX_HOME` (default `~/.codex`) — the same roots the two
executors write to, so what is measured is what ran. Every session of a worktree counts toward its
ticket, including a replacement child's and a review's.

A child whose transcript is missing, unreadable, or silent about usage is drawn as the `--` row
above and logged with the diagnosis in place of its figures, so an unmeasured child is visible in
both artifacts. Only a log naming an executor that is neither `claude` nor `codex` stops the pass,
because nothing in it can be billed.

## The window

```sh
monitor.py window --run-dir <run dir> --session <tmux target> [--refresh SECONDS]
```

One run, one dashboard, one tmux window named `crew-dashboard` running the dashboard's refresh
loop. This subcommand owns that window's whole lifecycle and is idempotent, so the operator — or
any script, at any point in the run — can call it again without ever producing a second dashboard:

1. the window's id is recorded in `<run-dir>/dashboard-window`;
2. if the recorded window is alive, the command prints its id and does nothing else;
3. if it is gone — the operator closed it, or the run is being resumed after a crash — a new one
   is created and re-recorded.

Reading the record, asking tmux, creating the window and writing the id back is one decision, so
it is made under one lock in the run directory: two callers at once still leave one window.

The window is created detached, so a launching or resuming run never takes the operator's focus.
Nothing here ever closes the window: at end of run the renderer inside it stops refreshing and
keeps its last frame, and only the human closes what they are reading.

The window is the operator's view of the run; the coordinator neither reads it nor is told what
it says.
