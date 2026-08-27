# The monitor's operator surface

The wave monitor is what the human watches a run through. It has six parts, and none of them
costs a model token or reaches the coordinator's context
([ADR-0001](adr/0001-coordinator-spends-tokens-only-on-judgment.md)):

| Part | Script | What it does |
| --- | --- | --- |
| the wake-up | [`monitor-wave.sh`][wake] | armed while every child is busy, exits once one is not |
| the dashboard | [`monitor.py dashboard`][monitor] | draws the whole run as a table |
| the window | [`monitor.py window`][monitor] | gives the run its one dashboard window |
| the pin | [`monitor.py pin`][monitor] | draws one frame into the coordinator's Claude Code statusline |
| the receipt check | [`monitor.py verify`][monitor] | decides whether a completion receipt holds |
| the cost pass | [`monitor.py cost`][monitor] | records what each child spent, the run total, and the coordinator's own |

[wake]: ../skills/crew/assets/monitor-wave.sh
[monitor]: ../skills/crew/assets/monitor/monitor.py

The wake-up is started with the run's machine log:

```sh
monitor-wave.sh --log <run-dir>/log.jsonl [--driver-pid <pid>] \
                <run-dir>/parked-paths <worktree-path>...
```

It remains armed while every supplied child is `busy`, exits 0 the moment any child is `waiting`,
`idle`, `parked` or `vanished`, and exits nonzero on a monitor error. Before a nonzero exit it
appends a `monitor-error` event naming the script and reason.

`--driver-pid` is the pid of the driver that armed it, and the driver passes its own. A wake-up is
read by that process and no other, so each poll begins by asking `kill -0` whether it is still
there and the loop ends quietly — exit 0, nothing printed — the first poll after it is gone. Every
ordinary way out of a driver disarms its monitors; this covers the one that cannot, a driver killed
outright, which otherwise left the monitor polling for a reader that would never come back.

The dashboard is a display, so a failure it meets is drawn rather than raised — it does not compete
with the wake-up for the job of stopping a run.

## The dashboard

```sh
monitor.py dashboard --run-dir <run dir> [--refresh SECONDS] [--toast-state PATH]
                     [--now TIMESTAMP] [--no-color]
```

One row per ticket of every wave, drawn from the run directory: the approved wave table
(`<run-dir>/wave-table.json`), the machine log (`<run-dir>/log.jsonl`,
[`docs/machine-log.md`](machine-log.md)), and one live-state source per lane — the CLI's own
per-session files for Claude children ([the live sources](#the-live-sources) below) and the bridge
state files `<run-dir>/codex/<NN>.json` for Codex ones. It takes the run directory and nothing
else — no wave number, no worktree list — so the frame is the whole run from the first draw, and a
ticket nothing has launched yet is drawn `pending` rather than missing.

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
time from its first launch. While the run is halted on a ruling it carries one more field
between the counts and the clock — `⚠ awaiting your ruling` — so a run waiting on the operator
is never read as a frozen frame:

```
crew crew-run-3 — wave 2/5 · pending=6 merged=4 · ⚠ awaiting your ruling · elapsed 01:12:44
```

The log's newest `advance` decides it: `escalated` or `interrupted` puts the marker up, and the
next `advance` — the wave carrying on once the coordinator has ruled — takes it away.

### The driver's own liveness

The same slot carries one other thing, in red, and the two never appear together:

```
crew crew-run-3 — wave 2/5 · pending=6 · ✖ driver dead — /crew feat/x to resume · elapsed 01:12:44
```

The driver runs detached from the coordinator's session now, in a tmux window of its own, so
nothing the coordinator holds can report that it ended. The run directory does. Its loop writes
its own pid into `<run-dir>/driver.pid` on the way in, and every exit it takes on purpose — a wake
handing judgment to the coordinator, a driver error, the run finishing, an operator's Ctrl-C in
the driver's own window — removes that file on the way out. A kill cannot, so a file naming a
process that is not running is a killed driver by construction, and that is the whole protocol:
one `kill -0`, the same judgment the pin sweep already makes, and no watchdog or heartbeat behind
it. Because a deliberate exit blanks the record before it writes its wake, a run awaiting a ruling
can never also be flagged as orphaned.

The banner names the directory the operator typed `/crew` with — the run directory's parent — so
recovery is one paste rather than a forensic session. Nothing acts on it: the render path is a
reader, and it neither respawns the driver nor removes the record (#87).

The same file is what makes `/crew` idempotent at the other end — a run whose driver is alive is
attached to rather than started again, so no run is ever driven twice.

Whose driver it is, nothing asks. A driver carries the coordinator it was started for for its
whole life, which is the pid every child authenticates a ruling against, so a driver that outlived
its session goes on answering to a session that has gone: the run keeps advancing, and rulings
made from a new session are refused by children launched under the old one. Detaching the driver
is what made that state reachable, and closing it means re-anchoring the run's children as well as
its driver — tracked in #112. Until then a run whose coordinator has exited is cleared and
restarted rather than adopted.

### States

Every source state is mapped into the Ticket state vocabulary before it is drawn, so the operator
never reads an internal word:

| Drawn | What it is mapped from |
| --- | --- |
| `pending` | the log carries no `launch` for that ticket, or an `outcome` of `blocked` |
| `running` | its lane's source says `busy` |
| `waiting` | its lane says `waiting`, `idle` or `shell`; a `merge` of `conflict`/`escalated` |
| `reworking` | an `escalated` merge, the rework instruction after it, and a `busy` lane |
| `parked` | a `receipt` verdict or an `outcome` of `parked`, or its lane says so |
| `landable` | a `receipt` verdict of `landable` |
| `settling` | `landable`, in a wave every ticket of which has settled, in a run that is not over |
| `merged` | a `merge` result of `clean`, `resolved` or `repaired`, or an `outcome` of `completed` |
| `failed` | a `receipt` verdict or an `outcome` of `failed` |
| `vanished` | it was launched, nothing settled it, and its lane has no live entry for it |

Two of those words are the intervals the run used to have no vocabulary for, and both exist
because a frozen-looking row is read as a hung run. A child sent the merge driver's rework
instruction — the `ruling` the log carries opening `CREW MERGE`, after the `merge` that escalated
— is working on the conflict rather than stuck at anything, so its row says `reworking` and
carries the abnormal row's annotation underneath. Order is the whole signal there: a conflict that
bounced back a second time stands on an instruction older than the merge it is standing on, and
that one is the coordinator's to answer, so it keeps `waiting`. `reworking` is also the one
settled word that asks the lane whether its child is `busy`, because it is the one that claims
something is happening now: a child that went idle, stopped at a prompt or vanished under its
instruction keeps `waiting`, which is the row the operator has to go and look at. A wave whose
last receipt is in is being merged next — seconds of work — so its unmerged rows say `settling`
until each one's own merge takes it to `merged`. A run that is over is in no such interval:
nothing is coming for a branch that never landed, and it stays `landable`.

The last settling line the log carries wins, not the first: a landable branch is merged next, and
where the ticket is *now* is what the operator is looking for. A settled row stops following its
lane's live source — `reworking` above is the one word that still asks it, because it is the one
that claims a child is working — and its elapsed time stops moving in every case. Worktrees are
compared by realpath, never as strings, so a `/tmp` spelling and a `/private/tmp` one are one
worktree.

### Annotation rows

Normal rows stay quiet; a row that owes an explanation carries it underneath, so the table never
has to grow a column:

| Annotation | Drawn when |
| --- | --- |
| `↳ review: <lane> <state> · <elapsed>` | the log's last `review` line says a review is `running` |
| `↳ anomaly: duplicate · more than one session in <worktree>` | one worktree, two sessions |
| `↳ anomaly: unknown · <source> could not be read` | the lane's source failed or is unknown |
| `↳ last event: <event> <word> — <detail> · <stamp>` | the row's state is an abnormal one |

The abnormal states are `waiting`, `reworking`, `failed` and `vanished`: a row in any of them owes
the operator that last line, and every other row stays quiet.

`duplicate` and `unknown` are annotations rather than states: both say what a reading did, not
where the ticket got to, so the row keeps the state its own log lines justify.

The `review` event is written by the Lifecycle Hook commands the reviewed child passes to
Review-Switch — `running` at review start and `returned` on every exit path it controls — so the
review annotation appears and disappears on its own, with no operator action and no model token
spent ([`docs/machine-log.md`](machine-log.md)). A run dispatched without a machine log passes no
hooks, reviews normally and draws no annotation.

### Colour and width

States are drawn in colour when a terminal is watching and plain everywhere else — a pipe, a
redirect, `--no-color`, or `NO_COLOR` in the environment all give plain text, which is what the
tests read. The title column absorbs whatever width the window has left after the other five, so
titles are as readable as the terminal allows.

`--refresh` turns the single render into the window's loop: the same frame, redrawn over itself
every so many seconds. Once the log carries an `advance` decision of `complete` or `stopped`, the
run is over: the renderer draws its last frame, stops refreshing, and stays alive holding it —
nothing here ever closes the window. Those two are the only decisions that end a run, here and in
the driver, which imports this predicate rather than keeping one of its own: a wave that escalated
or was interrupted is carried on by the ruling or by the run that adopts it, so every surface
keeps drawing it and says `⚠ awaiting your ruling` while it waits. `--now` fixes the moment
elapsed times are measured to, which is what makes a render reproducible.

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
| `crew <NN> escalated` | the log carries an escalation occurrence for that ticket |

Both rows a `waiting` state can come from are announced in their own words: a child at a
permission prompt and a child that finished a turn without settling anything need different things
from the operator. A wave nobody has launched into has not completed, so a run's later waves never
toast on the first frame. Each toast occurrence fires once per run: what has already been said is
remembered in the toast-state file — `--toast-state`, by default `<run-dir>/toasts.json` — so a
restarted window does not replay a run's exceptions, and a `--refresh` loop does not repeat itself
every few seconds. Escalations use one key per occurrence in log order, so a later escalation gets
a fresh toast while re-observing the same one does not.

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
monitor.py cost --log <machine log> [--coordinator-session ID]
```

Run once at run completion. For every ticket the log carries a `launch` for, it reads the usage
out of the transcripts that ran in that child's worktree, appends one `session-cost` line per
child ([`docs/machine-log.md`](machine-log.md)), and prints the run's rollup for the report:

```text
TICKET       EXECUTOR  MODEL                     INPUT  OUTPUT  CACHE-READ  CACHE-CREATION  TOTAL    DURATION
06           claude    claude-opus-4-5-20251101  24     46      6800        900             7770     --
07           codex     gpt-5.6-luna              1000   700     4000        250             5950     --
08           codex     gpt-5.6-luna              --     --      --          --              --       --
witness-06   claude    claude-sonnet-5           11     22      33          44              110      1.25s
TOTAL        --        --                        1035   768     10833       1194            13830    --
coordinator  claude    --                        169    44772   5132660     197257          5374858  --

08 not measured: no transcript under /Users/me/.codex/sessions was recorded in /repo/wt/08
```

`--coordinator-session` adds that last row: the session driving the run, read from the transcript
named `<id>.jsonl` under the Claude root. The coordinator can take its own figure rather than ask
the operator for it, because Claude Code writes the id it needs into `$CLAUDE_CODE_SESSION_ID`.
The row sits beneath the total and stays out of it — the judgment the run cost, measured against
the children's work rather than added to it
([ADR-0001](adr/0001-coordinator-spends-tokens-only-on-judgment.md)) — and is printed only, never
logged, because a `session-cost` line belongs to a launched ticket. Two things follow from the
session measuring itself: the figure is the whole session's, not the run's alone, so it is honest
as it stands only where the session did nothing else; and the transcript is still open as it is
read, so its last line alone is forgiven for not parsing — that one is the request in flight, and
unbilled either way. A line that does not parse with more of the session written after it is a
hole in the history, and takes the row to `--` like any other.
Without the flag the rollup ends at `TOTAL`, as it always did.

Every `witness` event contributes its own `witness-<NN>` row using the executor, model, token
counters and duration the event records. Its tokens are included in `TOTAL`; its duration is shown
as `<duration_seconds>s`. Child, total and coordinator rows show `--` for duration. A run with no
`witness` event has no witness row.

Transcripts are found by the worktree they ran in, compared by realpath and path-component
containment: a cwd at or below the launch event's worktree is that worktree, while a parent,
sibling, or other outside path is not. For Claude children, the pass reads the profile directory
named by that ticket's `account` in `<run-dir>/wave-table.json`, where `<run-dir>` is the parent of
the `--log` path; this is how a mixed-account run reads every account it touches. A row without an
`account` (an older table) falls back to the current `$CLAUDE_CONFIG_DIR` (default `~/.claude`).
Codex sessions remain under `$CODEX_HOME` (default `~/.codex`) — the same roots the two executors
write to, so what is measured is what ran. Every session of a worktree counts toward its ticket,
including a replacement child's — except a review's, which the axis-end Lifecycle Hook has already
costed under its own lane-tagged `session-cost` line and which is skipped here by the session id
that line names. Without that, a Claude child reviewed on the Claude lane would be billed for its
own review as well.

If the wave table cannot be read, Claude rows are shown as `--` with the table-reading diagnosis
instead of being charged from an unknown profile or silently omitted; Codex rows can still be
measured from their own root.

A child whose transcript is missing, unreadable, or silent about usage is drawn as the `--` row
above and logged with the diagnosis in place of its figures, so an unmeasured child is visible in
both artifacts. Only a log naming an executor that is neither `claude` nor `codex` stops the pass,
because nothing in it can be billed.

## The window

```sh
monitor.py window --run-dir <run dir> --session <tmux target> [--refresh SECONDS]
                  [--config <agentcrew.toml>] [--coordinator-pid PID] [--pin-dir <dir>]
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

### Which surface the run draws itself on

This is also the command that chooses the surface, because it is the one dispatch already calls in
every wave. `--config` names the project's `agentcrew.toml`, whose `[dashboard] surface` is
`window`, `pin`, or `both`. Resolution is, in order:

1. the project's explicit `[dashboard] surface`;
2. the machine preference at `$CLAUDE_CONFIG_DIR/agentcrew/surface`, falling back to
   `~/.claude/agentcrew/surface`, when the project is silent and the file contains a valid surface
   value; or
3. the shipped `window` default.

The machine preference is written as `pin` by `pin-install --apply` and removed by
`pin-install --uninstall --apply`. A missing, unreadable or invalid preference is treated as absent.

| `surface` | The window | The pin |
| --- | --- | --- |
| `window` | created, reused, recreated as above | never written |
| `pin` | not launched at all | written |
| `both` | as above | written |

A run whose config names no surface, whose config has no `[dashboard]` section, and a run given no
`--config` at all use the machine preference when one is installed, and otherwise get `window` —
so upgrading agentcrew changes nobody's run. On `both`, the two passes dedup their toasts through
the one file the run directory holds, which is what stops the same thing being announced twice.

Writing the pin needs `--coordinator-pid`, the pid of the session driving the run: a pin is the
handle on a live coordinator, and one written without a pid to check would be a frame nothing
could take down. The pin is written into the registry — `--pin-dir` overrides where — under a name
derived from the run directory, so the whole run has one pin however many waves re-write it.

## The pin

```sh
monitor.py pin [--pin-dir <dir>]
```

The same frame as the dashboard, drawn into the coordinator's own Claude Code statusline instead of
a tmux window, so the run and the coordinator's prompt are on screen at once and there is nothing
for the operator to close afterwards. Claude Code's `statusLine` runs a shell command and draws
whatever it prints, and its `refreshInterval` re-runs that command every N seconds whether or not
anyone is typing — so the statusline's own tick is the refresh loop, and each tick renders one
frame on demand. There is no background process and no frame file. The rows, states, annotations
and summary line are the window's, unchanged; only where the frame is drawn is new.

Like every other part, the pin costs no model token. The frame is a rendered display region: it is
never added to the coordinator's context and it makes no API call
([ADR-0001](adr/0001-coordinator-spends-tokens-only-on-judgment.md)). Why the statusline rather
than tmux's status lines is
[ADR-0008](adr/0008-the-pinned-dashboard-lives-in-claude-codes-statusline.md), and the measurements
behind it are in [`docs/dashboard-pinning-research.md`](dashboard-pinning-research.md). The window
is not replaced, deprecated or changed by any of this, and it stays the default surface.

### The live sources

Each lane is read from its own source, and the Claude lane has two of them
([ADR-0012](adr/0012-the-statusline-tick-reads-the-sessions-files.md)):

1. **The sessions files**, always tried first: one JSON object per live session at
   `$CLAUDE_CONFIG_DIR/sessions/<pid>.json`, falling back to `~/.claude/sessions/`. Each carries
   the session's `cwd` and its `status`, which is all a frame needs. A frame costs a few file
   reads, so a pane's tick is affordable at any refresh interval and a tenth pane adds a tenth
   read rather than a tenth CLI start.
2. **`claude agents --json`**, only when that directory cannot be read at all. Each call is a
   complete CLI start, so its parsed result is written to a machine-level cache at
   `$CLAUDE_CONFIG_DIR/agentcrew/agents-cache.json` and every pane on the machine reads that one
   file for a ten-second freshness window before any of them fetches again. A fetch that fails is
   cached too, so a CLI that cannot answer is asked once a window rather than once a tick.

Both are read per **account**. Claude Code scopes a login — and with it the sessions files, the
fallback's answer and everything else under a configuration home — to one profile directory, so
two accounts' live sources are disjoint: a child of a second account is absent from the
coordinator's sessions files and from the coordinator's `claude agents --json`. The wave table
names the profile directory every ticket runs under, and each ticket is read from its own
account's home: its sessions files at `<account>/sessions/`, where `<account>` is the profile
directory its row's account binding carries — the home that child was launched with, in either
mode. Where the two modes differ is the fallback, because that one is a *login* rather than a
directory: the CLI is spawned with `CLAUDE_CONFIG_DIR` set to the profile for a ticket that named
an account, and in the tick's own environment untouched for a ticket that named none, which runs
on the login the operator is signed into rather than under a default home spelled out explicitly.
Its shared answer is filed under the account whose login gave it, at
`<login>/agentcrew/agents-cache.json`, so no pane is ever served one account's list as the answer
about another's. Without any of this a healthy child on another account is drawn `vanished` and
toasts the operator about it. The primary path spawns nothing for the extra account — an account
more is a directory listing more — and only an account whose sessions directory cannot be read
falls back, so a run naming one account costs exactly what it cost before. A wave table with no
`account` on its rows was written before accounts existed, and is read from the configuration home
the tick itself was started under. The Codex lane has no account: a Codex child's bridge state
lives in the run directory, and it is read once and shared by every account of the run.

Nothing is cached on the first path — reading the files is already as cheap as reading a cache
would be, so there is nothing there to go stale. A tick whose sources have both failed draws the
row `unknown` and says nothing at all, exactly as
[ADR-0008](adr/0008-the-pinned-dashboard-lives-in-claude-codes-statusline.md) requires; the one
record that it happened is a `live-source` line in the run's machine log, appended once per run.

The two sources differ in one word. The files carry `shell` — a child sitting at a shell prompt —
which the command folds into `busy`. The lane maps `shell` to `waiting` and toasts it in its own
words (`sitting at a shell prompt`, not `stopped without finishing`); in fallback mode such a child
is necessarily drawn `running`, and that asymmetry is accepted.

The sessions directory is undocumented, which is exactly why the fallback exists: an upgrade that
moves or removes it degrades what the dashboard costs, never what it draws.

### The pin registry

`pin` takes no `--run-dir`. It discovers the live run from the **pin registry**: a directory of pin
files at `$CLAUDE_CONFIG_DIR/agentcrew/pins/`, falling back to `~/.claude/agentcrew/pins/`,
overridable with `--pin-dir`. A pin file is JSON naming the run — the run directory as an absolute
realpath ([ADR-0007](adr/0007-paths-are-absolute-at-the-boundary-and-compared-by-realpath.md)), the
coordinator's pid, and the coordinator's tmux session — and naming what draws it: the `monitor.py`
of the release that wrote the pin, and the interpreter running it. Five keys:

```json
{"run_dir": "/abs/realpath/of/the/run", "coordinator_pid": 4242, "tmux_session": "$7",
 "renderer": "/abs/path/of/the/running/release/monitor.py", "interpreter": "/abs/path/of/python3"}
```

The last two are why an upgrade never strands the pin. They are recorded at dispatch, by a release
that is by definition alive at that moment, rather than at install time by a release that will not
outlive the wiring it writes
([ADR-0011](adr/0011-the-pin-names-its-renderer-the-wrapper-is-a-permanent-stub.md)).

A tick is also the registry's only housekeeper. Before any pin is matched, every pin whose
recorded `coordinator_pid` is no longer alive has its file removed — because nothing but a normal
finish ever unpins a run, and a coordinator abandoned after a judgment-needed or driver-error pause
would otherwise leave a file that every pane reads for as long as the machine stays up. The sweep
is safe because a pin is not state: every wave writes it again, so a run the operator resumes
re-pins itself on its next dispatch. A pin whose coordinator is alive is never touched, which is
what keeps the `⚠ awaiting your ruling` frame on screen through a pause.

The run writes its pin at dispatch — `monitor.py window`, on a `pin` or `both` surface — and
removes it when the run ends, after the report is written:

```sh
monitor.py unpin --run-dir <run dir> [--pin-dir <dir>]
```

The final frame lives in the report and the machine log, not on the screen. `unpin` is how a run
ends whatever surface it ran on: a run that wrote no pin has nothing to remove, and that is a
success rather than a complaint.

One pin is selected per tick, by the caller's own tmux session and by nothing else:

1. the pin whose recorded tmux session matches the caller's own is the run drawn;
2. any other case draws nothing — no pin matches, several pins match, or the tick has no
   resolvable session to match with — however many pins the registry holds.

The pin is therefore scoped to the tmux session that launched the run: a run's frame is drawn in
the tab that launched it and in no other, which is what keeps two crews at once from crossing
frames. There is no fallback for the single-pin case; a lone pin used to be drawn whatever session
the tick came from, which put one run's frame in every window on the machine. Two accepted
consequences follow:

- A Claude Code session outside tmux can see no pin, ever — there is no session to match. The
  workflow this serves is tmux-resident.
- Other tabs on the same project stay clean while a run is in flight. That is the chosen
  behaviour, not a gap: the boundary is the tab, not the project, because one project may run
  several crews at once in different tabs.

### The wrapper the install writes

```sh
monitor.py pin-install [--apply] [--uninstall] [--settings <file>] [--statusline <file>]
```

`pin-install` wires the pin into the operator's own Claude Code statusline: it writes a wrapper
script beside their settings, points `statusLine.command` at it, and records the machine preference
as `pin` at `$CLAUDE_CONFIG_DIR/agentcrew/surface`, falling back to
`~/.claude/agentcrew/surface`. The wrapper runs whatever the statusline ran before, prints that
first, and draws the pin's frame beneath it. Nothing is written without
`--apply`, every settings/wrapper file is copied aside first, and `--uninstall` reads the record the
wrapper carries to put the statusline back exactly while removing the machine preference.

That wrapper names no release. It reads the registry above, and runs the renderer and interpreter
the live pin names — so the release that draws the frame is the release that dispatched the run,
and an upgrade needs no re-install. It ends in `exit 0` on every path, including the one where the
renderer fails, because Claude Code blanks the whole statusline — the operator's own lines with it
— when the statusline command exits non-zero.

The install is idempotent, and "idempotent" is measured against the wrapper this release would
write: a wrapper that is there but differs is reported as a `rewrite` line and rewritten under
`--apply`. Existing installs are never rewritten behind the operator's back; an operator carrying
one from an older release re-runs `pin-install --apply` once.

There is one exception to the silence contract, and this is it: when pins are present but none of
them names a renderer and interpreter this machine has — the paths are gone, the pin cannot be read,
or it was written by a release older than those fields — a single line says so, instead of nothing.
The wrapper prints it when it can reach no renderer at all; the renderer it does reach is told so
(`pin --from-wrapper`) and prints the same line for the registry files only a JSON parser can
judge. `monitor.py pin` on its own is silent as it has always been.

A statusline that has quietly stopped working is indistinguishable from a machine with no run on
it, and this case is one the operator can act on
([ADR-0011](adr/0011-the-pin-names-its-renderer-the-wrapper-is-a-permanent-stub.md)). Failures
*inside* the renderer stay silent, as
[ADR-0008](adr/0008-the-pinned-dashboard-lives-in-claude-codes-statusline.md) decided.

### When nothing is drawn

Nothing is drawn, and the exit status is still 0, when there is no pin, when the run directory is
gone or unreadable, when the coordinator's pid is not alive, or when the machine log carries an
`advance` decision of `complete` or `stopped`. The last two are the whole liveness story for the
*coordinator*: a dead pid is a crashed or killed session, either of those decisions is a run that
is over, and there is no watchdog or heartbeat behind either. A halted wave is none of these — the
pin keeps drawing it, with the `⚠ awaiting your ruling` marker on its summary line — and neither
is a run whose *driver* died, which is drawn with [the dead-driver
banner](#the-drivers-own-liveness) rather than not at all.
