# The monitor's operator surface

The wave monitor is what the human watches a run through. It has four parts, and none of them
costs a model token or reaches the coordinator's context
([ADR-0001](adr/0001-coordinator-spends-tokens-only-on-judgment.md)):

| Part | Script | What it does |
| --- | --- | --- |
| the wake-up | [`monitor-wave.sh`][wake] | armed while every child is busy, exits once one is not |
| the dashboard | [`monitor.py dashboard`][monitor] | draws the wave as a table in its own pane |
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
monitor.py dashboard --log <machine log> --wave <N> [--refresh SECONDS]
                     [--toast-state PATH] [--now TIMESTAMP] <worktree>...
```

One row per launched ticket, drawn from the run's machine log
([`docs/machine-log.md`](machine-log.md)) joined with the live agents list. The worktree paths are
the wave's membership — the same arguments the wake-up is given — and a row exists for each one
the log carries a `launch` for.

```text
crew wave 1 — 2026-08-13T09:31:12Z
WAVE  TICKET  CHILD             STATE      LAST EVENT  ELAPSED
1     06      crew-06-dispatch  landable   receipt     00:41:07
1     07      crew-07-log       busy       launch      00:12:31
1     08      crew-08-monitor   waiting    escalation  00:12:29
```

| Column | Where it comes from |
| --- | --- |
| `WAVE` | the `--wave` argument: one dashboard shows one wave |
| `TICKET` | the `ticket` of the worktree's `launch` event |
| `CHILD` | its `child` |
| `STATE` | the settled verdict where the log has one, otherwise the child's live status |
| `LAST EVENT` | the `event` of the ticket's most recent log line |
| `ELAPSED` | `HH:MM:SS` from the `launch` stamp to the settling stamp, or to now while live |

A ticket settles the moment the log carries a `receipt` or an `outcome` for it, and its state is
then that line's `verdict` or `outcome` — a settled row stops following the agents list, and its
elapsed time stops moving. An unsettled row shows what `claude agents --json` says about its
worktree: `busy`, `waiting`, `idle`, `vanished` when the list has no entry for it at all, and
`duplicate` when it has more than one — the wake-up's own word for a worktree with two sessions in
it. When that list cannot be read, every unsettled row reads `unknown` and the pane keeps drawing.

`--refresh` turns the single render into the pane's loop: the same table, redrawn over itself every
so many seconds, so the operator watches the run rather than re-running a command. `--now` fixes
the moment elapsed times are measured from, which is what makes a render reproducible.

## Toasts

Each pass emits a toast for anything that just became true, on `tmux display-message` — the
operator's terminal, which no model reads. Nothing is sent to the coordinator, and nothing is
written where a model would be handed it.

| Toast | Emitted when |
| --- | --- |
| `crew wave <N> complete` | every row of the wave is settled |
| `crew <NN> stuck at a permission prompt` | an unsettled row's live status is `waiting` |
| `crew <NN> vanished` | an unsettled row has no entry in the agents list |
| `crew <NN> escalated` | the log carries an `escalation` for that ticket |

Each of these fires once per run. What has already been said is remembered in the toast-state
file — `--toast-state`, by default `toasts.json` beside the machine log — so a restarted pane
does not replay a run's exceptions, and a `--refresh` loop does not repeat itself every few
seconds.

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

Transcripts are found by the worktree they ran in, compared by realpath: Claude sessions under
`$CLAUDE_CONFIG_DIR` (default `~/.claude`), Codex sessions under `$CODEX_HOME` (default
`~/.codex`) — the same roots the two executors write to, so what is measured is what ran. Every
session of a worktree counts toward its ticket, including a replacement child's and a review's.

A child whose transcript is missing, unreadable, or silent about usage is drawn as the `--` row
above and logged with the diagnosis in place of its figures, so an unmeasured child is visible in
both artifacts. Only a log naming an executor that is neither `claude` nor `codex` stops the pass,
because nothing in it can be billed.

## The pane

```sh
monitor.py pane --session <tmux target> --log <machine log> --wave <N> <worktree>...
```

Splits a dedicated pane in the run's tmux session running the dashboard's own refresh loop, and
prints the pane id. The pane is the operator's window into the run; the coordinator neither reads
it nor is told what it says.
