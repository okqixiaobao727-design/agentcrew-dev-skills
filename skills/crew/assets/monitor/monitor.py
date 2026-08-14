#!/usr/bin/env python3
"""The monitor's operator surface: the run dashboard, its toasts, the receipt check, the cost pass.

    dashboard  draw the whole run as a table — one row per ticket of every wave — and toast what
               just became true, refreshing in place when asked to
    window     create, reuse or recreate the run's one dedicated tmux window running that loop
    verify     decide whether a child's `CREW COMPLETE <sha>` holds, and log the receipt it earns
    cost       at run completion, log what each child's sessions spent and roll the run up

None of this costs a model token and none of it reaches the coordinator (ADR-0001): the table is
drawn in the operator's own window, toasts go to `tmux display-message`, and the only things
written anywhere are the run's own machine-log lines — one `receipt` per verified completion, and
one `session-cost` per child when the run is over. The wake-up itself stays where it is, in
`monitor-wave.sh`, with the contract it already has — armed while every child is busy, exit
as soon as one needs attention, nonzero on a monitor error. This script is the display beside
it, so a failure it meets is drawn rather than raised; `docs/monitor-dashboard.md` publishes both
surfaces.

The dashboard takes the run directory, never a wave number or a worktree list: the wave table
there is the whole run's membership, the machine log (`docs/machine-log.md`) is what has happened
to it, and the live agents list is what its children are doing now. A ticket no launch event names
is drawn `pending`, so "not started yet" is never mistaken for "lost".
"""

import argparse
import contextlib
import datetime
import fcntl
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import time

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
# The writer that owns the log's schema: a receipt this script verifies is appended through it
# rather than formatted here, so the closed sets stay in one place.
MACHINE_LOG = pathlib.Path(__file__).resolve().parent.parent / "machine_log.py"

CLAUDE_BIN = "claude"
TMUX_BIN = "tmux"

# The guard assets the dispatch renderer installs into a Claude worktree before its child starts.
# The child never commits them, so they are the one thing a clean tree may still be carrying.
GUARD_ASSET_PATHS = (
    ".claude/red-line.sh",
    ".claude/worktree-guard.sh",
    ".claude/settings.local.json",
)
# The `git status --porcelain` code for a file git has never been told about, which is the only
# state an installed guard asset is ever in.
UNTRACKED_CODE = "??"

CLAUDE = "claude"
CODEX = "codex"

# The run directory's fixed layout: the whole run's membership, what has happened to it, where a
# Codex child says how it is doing, what has already been toasted, and the window it is drawn in.
WAVE_TABLE_NAME = "wave-table.json"
MACHINE_LOG_NAME = "log.jsonl"
CODEX_STATE_DIR = "codex"
CODEX_STATE_GLOB = "*.json"
TOAST_STATE_NAME = "toasts.json"
WINDOW_RECORD_NAME = "dashboard-window"
# Held across the check-create-record that makes a window, so two callers cannot make two.
WINDOW_LOCK_NAME = "dashboard-window.lock"
# One run, one dashboard, one window with this name — so the operator always knows where to look.
DASHBOARD_WINDOW_NAME = "crew-dashboard"

# The Ticket state vocabulary (`docs/glossary.md`): the words the operator reads, and the only
# words this script draws. Every source state — a tmux process status, a settlement verdict, a
# monitor internal — is mapped into one of them before it reaches a frame.
PENDING = "pending"
RUNNING = "running"
WAITING = "waiting"
PARKED = "parked"
LANDABLE = "landable"
MERGED = "merged"
FAILED = "failed"
VANISHED = "vanished"
# The order the summary line counts them in: the way a ticket travels, start to finish.
STATE_ORDER = (PENDING, RUNNING, WAITING, PARKED, LANDABLE, MERGED, FAILED, VANISHED)
# The states that owe the operator an explanation; every other row stays quiet.
ABNORMAL_STATES = (WAITING, FAILED, VANISHED)

# The two anomalies, which are annotations rather than states: no row is ever `duplicate` or
# `unknown`, because both describe what the agents list did, not where the ticket is.
DUPLICATE = "duplicate"
UNKNOWN = "unknown"

# Every settling event and the word it settles a ticket into. A ticket keeps travelling after a
# receipt — a landable branch is merged next — so the last settling line the log carries wins.
SETTLED_STATES = {
    "receipt": ("verdict", {"landable": LANDABLE, "parked": PARKED, "failed": FAILED}),
    "outcome": ("outcome", {
        "completed": MERGED, "failed": FAILED, "parked": PARKED, "blocked": PENDING,
    }),
    "merge": ("result", {"clean": MERGED, "repaired": MERGED}),
}
# Where a live child of each lane says how it is doing, and what its words mean to the operator.
# The two sources are disjoint: a Codex child has no entry in the agents list at all — the bridge
# is the only thing that knows about it — so a run's Codex tickets are read from its state files
# or drawn `vanished` on every frame. `idle` in either lane is a child that has stopped without
# settling anything, which needs the operator as much as a permission prompt does.
LIVE_SOURCES = {
    CLAUDE: "the agents list",
    CODEX: "the codex bridge state",
}
LIVE_STATES = {
    CLAUDE: {"busy": RUNNING, "waiting": WAITING, "idle": WAITING, "parked": PARKED},
    CODEX: {"busy": RUNNING, "idle": WAITING, "stopped": VANISHED},
}
# What a child that has stopped is toasted as. The two situations send the operator to different
# places, so neither is announced in the other's words.
ATTENTION_TOASTS = {
    "waiting": "stuck at a permission prompt",
    "idle": "stopped without finishing",
}
# The qualifier each event carries, in the order an annotation looks for one.
EVENT_QUALIFIERS = ("verdict", "outcome", "result", "decision", "state")
ESCALATION_EVENT = "escalation"
REVIEW_EVENT = "review"
REVIEW_RUNNING = "running"
# The advance decisions after which nothing more happens in this run.
ADVANCE_EVENT = "advance"
FINAL_DECISIONS = ("complete", "escalated", "interrupted")

COLUMNS = ("WAVE", "TICKET", "TITLE", "EXECUTOR", "STATE", "ELAPSED")
# The one column that has no natural width — it is given whatever the window has left over — and
# the one that is drawn in colour.
TITLE_COLUMN = COLUMNS.index("TITLE")
STATE_COLUMN = COLUMNS.index("STATE")
COLUMN_GAP = "  "
ANNOTATION_PREFIX = "  ↳ "
NO_ELAPSED = "--"
ELLIPSIS = "…"
FALLBACK_WIDTH = 80
CLEAR_SCREEN = "\x1b[H\x1b[2J"

# One SGR colour per state, and the reset that ends it. Applied to the state cell alone, and only
# when a terminal is watching: `plain` is what a pipe, a redirect and every test sees.
STATE_COLOURS = {
    PENDING: "90", RUNNING: "36", WAITING: "33", PARKED: "35",
    LANDABLE: "32", MERGED: "32", FAILED: "31", VANISHED: "31",
}
COLOUR_RESET = "\x1b[0m"

DEFAULT_REFRESH_SECONDS = 5.0
# How long the renderer sleeps between checks once the run is over: it has stopped drawing, and it
# stays alive only so the window keeps the last frame until the human closes it.
HOLD_SECONDS = 3600.0

FULL_SHA = re.compile(r"^[0-9a-f]{40}$")

# Where each executor keeps the transcripts the cost pass reads, and the environment variable
# that moves that root — the same two the executors themselves honour, so a run measured here is
# the run that happened.
CLAUDE_TRANSCRIPTS = ("CLAUDE_CONFIG_DIR", ".claude", "projects")
CODEX_TRANSCRIPTS = ("CODEX_HOME", ".codex", "sessions")
TRANSCRIPT_GLOB = "**/*.jsonl"

# The four disjoint counters every child's usage is normalised to, in the order the rollup shows
# them. Disjoint is what makes them addable: Codex reports its cached tokens inside its input
# count and Claude reports them beside it, so one of the two is converted rather than compared.
COUNTERS = ("input", "output", "cache_read", "cache_creation")
COST_COLUMNS = (
    "TICKET", "EXECUTOR", "MODEL", "INPUT", "OUTPUT", "CACHE-READ", "CACHE-CREATION", "TOTAL",
)
TOTAL_ROW = "TOTAL"
# The row beneath the total: what the session driving the run spent, against the children's total.
COORDINATOR_ROW = "coordinator"
# What a cell shows when there is no figure behind it: a diagnosed row's counters, and the two
# columns the total row has no single answer for.
NO_FIGURE = "--"
SESSION_SEPARATOR = ","
# A transcript line nobody can read, and a transcript that never said which worktree it ran in —
# the two facts a reader passes back when it cannot answer the question it was asked.
MALFORMED = object()
UNDETERMINED = object()

MONITOR_ERROR_EXIT = 3


class MonitorError(Exception):
    """Something the monitor needs was not there. Reported on stderr, never drawn as a verdict."""


def parse_timestamp(value):
    """The moment `value` names, or None when it is not the run's one timestamp format."""
    try:
        return datetime.datetime.strptime(value, TIMESTAMP_FORMAT).replace(
            tzinfo=datetime.timezone.utc
        )
    except (TypeError, ValueError):
        return None


def now():
    return datetime.datetime.now(datetime.timezone.utc)


def elapsed(start, end):
    """`HH:MM:SS` between two moments, or `--` when either of them was never stamped."""
    if start is None or end is None:
        return NO_ELAPSED
    seconds = max(int((end - start).total_seconds()), 0)
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def worktree_key(path):
    """One worktree's identity: what its path resolves to, never how it was spelled.

    A run reaches the same directory by several names — `/tmp` and `/private/tmp` on macOS, a
    symlinked checkout anywhere — and two of them compared as strings make a live child look
    vanished, which is a false toast and a false wake-up. A path that resolves to nothing keeps
    its own text, which is still a stable key for the row it belongs to.
    """
    return os.path.realpath(str(path))


def git(worktree, *args):
    """The trimmed stdout of one git command, or a MonitorError carrying why it failed."""
    result = subprocess.run(
        ["git", "-C", str(worktree), *args], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise MonitorError(
            f"git {' '.join(args)} failed: {(result.stderr or result.stdout).strip()}"
        )
    return result.stdout.strip()


def descends_from(worktree, base):
    """Whether the worktree's head has `base` in its history at all.

    Counting commits `base..HEAD` answers a different question: an unrelated commit is behind
    every commit that cannot reach it, so a branch cut from the wrong place — or from no shared
    history at all — counts as ahead of it without ever having built on it.
    """
    result = subprocess.run(
        ["git", "-C", str(worktree), "merge-base", "--is-ancestor", base, "HEAD"],
        capture_output=True, text=True,
    )
    if result.returncode in (0, 1):
        return result.returncode == 0
    raise MonitorError(
        f"git merge-base failed: {(result.stderr or result.stdout).strip()}"
    )


def read_log(path):
    """Every record in the machine log, oldest first; an absent or half-written line is skipped."""
    try:
        text = pathlib.Path(path).read_text(encoding="utf-8")
    except OSError:
        return []
    records = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def agent_states(claude_bin):
    """Each live session's status by its `cwd`, or None when the agents list cannot be read.

    A worktree the list carries twice has no single status, so it is reported as the duplicate the
    wake monitor calls an error — the dashboard draws it and leaves the stopping to the wake-up.
    """
    result = subprocess.run(
        [claude_bin, "agents", "--json"], capture_output=True, text=True
    )
    if result.returncode != 0:
        return None
    try:
        snapshot = json.loads(result.stdout)
    except ValueError:
        return None
    if not isinstance(snapshot, list):
        return None
    states = {}
    for agent in snapshot:
        if not isinstance(agent, dict) or "cwd" not in agent:
            continue
        cwd = worktree_key(agent["cwd"])
        states[cwd] = DUPLICATE if cwd in states else str(agent.get("status", UNKNOWN))
    return states


def codex_states(run_dir):
    """Each live Codex child's status by the worktree its bridge recorded, never by spelling.

    A Codex child is invisible to `claude agents --json`, so its bridge state file is the only
    thing that knows it is alive: read nothing here and every Codex ticket of a run is drawn
    `vanished` from the first frame. A file that cannot be read is not a child that stopped, so it
    is passed over rather than counted as one.
    """
    states = {}
    for path in sorted((pathlib.Path(run_dir) / CODEX_STATE_DIR).glob(CODEX_STATE_GLOB)):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        cwd = state.get("cwd") if isinstance(state, dict) else None
        if not cwd:
            continue
        key = worktree_key(cwd)
        states[key] = DUPLICATE if key in states else str(state.get("status", UNKNOWN))
    return states


def live_sources(claude_bin, run_dir):
    """What each lane says about its own children: the agents list, and the bridge state files."""
    return {CLAUDE: agent_states(claude_bin), CODEX: codex_states(run_dir)}


def read_table(run_dir):
    """The run's approved wave table: every wave, with every ticket of it, in the table's order.

    A dashboard drawn from a table nobody could read would be an empty frame, which is exactly the
    failure this window exists to make impossible — so an unreadable table stops the command
    instead.
    """
    path = pathlib.Path(run_dir) / WAVE_TABLE_NAME
    try:
        table = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise MonitorError(f"the run's wave table could not be read at {path}: {error}")
    except ValueError as error:
        raise MonitorError(f"{path} is not the wave table's JSON: {error}")
    waves = table.get("waves") if isinstance(table, dict) else None
    if not isinstance(waves, list):
        raise MonitorError(f"{path} carries no waves list")
    return waves


def settling_state(record):
    """The word this record settles a ticket into, or None when it settles nothing."""
    key, words = SETTLED_STATES.get(str(record.get("event")), (None, {}))
    if key is None:
        return None
    return words.get(str(record.get(key)))


def settled(events):
    """The last record that settled this ticket and the word it settled it into, or two Nones.

    The last one wins rather than the first: a ticket keeps travelling after its receipt — a
    landable branch is merged next — and where it is now is what the operator is looking for.
    """
    for record in reversed(events):
        state = settling_state(record)
        if state is not None:
            return record, state
    return None, None


def live_state(launch, sources):
    """What a launched, unsettled ticket is doing now: its state, its anomaly, and its raw status.

    Each lane is read from its own source, chosen by the executor its launch names, because the
    two do not overlap: a Claude child is in the agents list and a Codex child is in its bridge
    state file, and asking either about the other's children answers `vanished`.

    An anomaly is not a state: `duplicate` and `unknown` say what a reading did, not where the
    ticket got to, so the row keeps the state its own log line justifies and carries the anomaly
    as an annotation underneath. The raw status rides along because two of them mean the same
    state and different things to the operator — a permission prompt is not a finished turn.
    """
    executor = str(launch.get("executor", ""))
    if executor not in LIVE_STATES:
        return RUNNING, (UNKNOWN, f"the launch names executor {executor or '(none)'}"), None
    states = sources.get(executor)
    if states is None:
        return RUNNING, (UNKNOWN, f"{LIVE_SOURCES[executor]} could not be read"), None
    status = states.get(worktree_key(launch.get("worktree")))
    if status is None:
        return VANISHED, None, None
    if status == DUPLICATE:
        return RUNNING, (DUPLICATE, f"more than one session in {launch.get('worktree')}"), status
    if status not in LIVE_STATES[executor]:
        return RUNNING, (UNKNOWN, f"{LIVE_SOURCES[executor]} calls it {status}"), status
    return LIVE_STATES[executor][status], None, status


def event_note(record):
    """One line of what an event was: its name, the word it carried, and its detail."""
    parts = [str(record.get("event", ""))]
    for key in EVENT_QUALIFIERS:
        value = record.get(key)
        if value:
            parts.append(str(value))
            break
    note = " ".join(parts)
    detail = record.get("detail")
    if detail:
        note += " — " + " ".join(str(detail).split())
    return note


def review_note(events, moment):
    """The review this ticket is under, or None when it is under none.

    The log's last `review` line for the ticket is the one that holds: a review that has returned
    says so, and its row goes quiet again.
    """
    for record in reversed(events):
        if record.get("event") != REVIEW_EVENT:
            continue
        if str(record.get("state")) != REVIEW_RUNNING:
            return None
        lane = " ".join(str(record.get("lane", "")).split())
        clock = elapsed(parse_timestamp(record.get("ts")), moment)
        return f"review: {lane} {REVIEW_RUNNING} · {clock}"
    return None


def annotations(events, state, anomaly, moment):
    """The lines drawn under one row: its review, its anomaly, and what last happened to it."""
    lines = []
    review = review_note(events, moment)
    if review:
        lines.append(review)
    if anomaly is not None:
        lines.append(f"anomaly: {anomaly[0]} · {anomaly[1]}")
    if state in ABNORMAL_STATES and events:
        lines.append(f"last event: {event_note(events[-1])} · {events[-1].get('ts')}")
    return lines


def build_rows(waves, records, moment, sources):
    """One row per ticket of every wave, in the order the approved table lists them.

    A ticket no `launch` event names has not started, which is what `pending` says: the frame
    shows the whole run from its first draw, so "not started yet" is never read as "lost". A
    ticket launched twice — a replacement child — is the one row its last launch describes.
    """
    launches = {}
    for record in records:
        if record.get("event") == "launch":
            launches[str(record.get("ticket"))] = record

    rows = []
    for wave in waves:
        number = str(wave.get("wave", ""))
        for entry in wave.get("tickets") or []:
            ticket = str(entry.get("id", ""))
            events = [record for record in records if str(record.get("ticket")) == ticket]
            launch = launches.get(ticket)
            settling, settled_into = settled(events)
            anomaly = None
            status = None
            if launch is None:
                state, started, end = PENDING, None, None
            else:
                started = parse_timestamp(launch.get("ts"))
                if settling is not None:
                    state, end = settled_into, parse_timestamp(settling.get("ts"))
                else:
                    state, anomaly, status = live_state(launch, sources)
                    end = moment
            rows.append({
                "wave": number,
                "ticket": ticket,
                "title": str(entry.get("title", "")),
                "executor": "/".join(
                    part for part in (str(entry.get("executor", "")), str(entry.get("model", "")))
                    if part
                ),
                "state": state,
                "status": status,
                "elapsed": elapsed(started, end),
                "started": started,
                "launched": launch is not None,
                "settled": settling is not None,
                "escalated": any(record.get("event") == ESCALATION_EVENT for record in events),
                "annotations": annotations(events, state, anomaly, moment),
            })
    return rows


def over(records):
    """Whether nothing more will happen in this run, which is when the frame stops moving."""
    return any(
        record.get("event") == ADVANCE_EVENT and str(record.get("decision")) in FINAL_DECISIONS
        for record in records
    )


def terminal_width():
    """How wide the window is, as the terminal or `COLUMNS` has it."""
    return shutil.get_terminal_size(fallback=(FALLBACK_WIDTH, 24)).columns


def cut(text, width):
    """`text` in at most `width` columns, the last one spent saying it was cut."""
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    return text[: width - 1].rstrip() + ELLIPSIS


def summary(rows, run_id, waves, moment):
    """The one line above the table: which run, how far through its waves, and how it stands."""
    counts = {state: 0 for state in STATE_ORDER}
    for row in rows:
        counts[row["state"]] += 1
    started = [row["started"] for row in rows if row["started"] is not None]
    parts = [
        f"wave {len({row['wave'] for row in rows if row['launched']})}/{waves}",
        " ".join(f"{state}={counts[state]}" for state in STATE_ORDER if counts[state]),
        f"elapsed {elapsed(min(started) if started else None, moment)}",
    ]
    return f"crew {run_id} — " + " · ".join(part for part in parts if part)


def paint(text, state, colour):
    """The state cell in its own colour, or exactly as it was when nothing can show one."""
    code = STATE_COLOURS.get(state) if colour else None
    return f"\x1b[{code}m{text}{COLOUR_RESET}" if code else text


def render(rows, run_id, waves, moment, width=None, colour=False):
    """The whole frame: the summary line, the header, and each row with its annotations."""
    width = width or terminal_width()
    cells = [list(COLUMNS)] + [
        [row["wave"], row["ticket"], row["title"], row["executor"], row["state"], row["elapsed"]]
        for row in rows
    ]
    widths = [max(len(row[index]) for row in cells) for index in range(len(COLUMNS))]
    # Every other column is as wide as its content; the title takes what the window has left.
    fixed = sum(widths) - widths[TITLE_COLUMN] + len(COLUMN_GAP) * (len(COLUMNS) - 1)
    widths[TITLE_COLUMN] = max(min(widths[TITLE_COLUMN], width - fixed), 0)

    lines = [cut(summary(rows, run_id, waves, moment), width)]
    for index, values in enumerate(cells):
        values = list(values)
        values[TITLE_COLUMN] = cut(values[TITLE_COLUMN], widths[TITLE_COLUMN])
        padded = [value.ljust(size) for value, size in zip(values, widths)]
        if index:
            padded[STATE_COLUMN] = paint(padded[STATE_COLUMN], rows[index - 1]["state"], colour)
        lines.append(COLUMN_GAP.join(padded).rstrip())
        if index:
            lines += [
                cut(ANNOTATION_PREFIX + note, width) for note in rows[index - 1]["annotations"]
            ]
    return "\n".join(lines)


def toasts(rows):
    """Every toast this pass has grounds for, each with the key that says it has been said.

    The key is per run, not per pass: an exception is announced when it becomes true and is not
    repeated while it stays true, which is what makes a refreshing window bearable to sit beside.
    """
    said = []
    waves = {}
    for row in rows:
        ticket = row["ticket"]
        if row["launched"] and not row["settled"]:
            if row["state"] == WAITING and row["status"] in ATTENTION_TOASTS:
                # Keyed on the raw status, so a child that waits and later goes idle says both.
                said.append((
                    f"{row['status']}:{ticket}",
                    f"crew {ticket} {ATTENTION_TOASTS[row['status']]}",
                ))
            elif row["state"] == VANISHED:
                said.append((f"vanished:{ticket}", f"crew {ticket} vanished"))
        if row["escalated"]:
            said.append((f"escalated:{ticket}", f"crew {ticket} escalated"))
        waves.setdefault(row["wave"], []).append(row)
    for wave, members in waves.items():
        # A wave nobody has launched into is not a wave that finished.
        if any(row["launched"] for row in members) and all(row["settled"] for row in members):
            said.append((f"wave-complete:{wave}", f"crew wave {wave} complete"))
    return said


def read_said(path):
    try:
        said = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return set(said) if isinstance(said, list) else set()


def write_said(path, said):
    path = pathlib.Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sorted(said)) + "\n", encoding="utf-8")
    except OSError:
        # A toast that cannot be remembered is still worth showing; the cost is showing it twice.
        pass


def display(tmux_bin, text):
    """Show one toast in the operator's terminal. A tmux that will not show it is not an error."""
    try:
        subprocess.run([tmux_bin, "display-message", text], capture_output=True, text=True)
    except OSError:
        pass


def emit_toasts(rows, state_path, tmux_bin):
    """Display the toasts this pass has grounds for and has not shown; returns their texts."""
    said = read_said(state_path)
    shown = []
    for key, text in toasts(rows):
        if key in said:
            continue
        display(tmux_bin, text)
        said.add(key)
        shown.append(text)
    if shown:
        write_said(state_path, said)
    return shown


def run_directory(args):
    """The run directory, absolute: every path this command reads or writes hangs off it."""
    return pathlib.Path(args.run_dir).resolve()


def toast_state_path(args, run_dir):
    """Where this run remembers what it has already toasted: in the run directory by default."""
    if args.toast_state:
        return pathlib.Path(args.toast_state)
    return run_dir / TOAST_STATE_NAME


def colour_wanted(args):
    """Whether a terminal is watching that can show colour, which is the only time it is drawn."""
    return bool(sys.stdout.isatty()) and not args.no_color and not os.environ.get("NO_COLOR")


def draw(args, run_dir, moment):
    """Draw one frame of the whole run; returns whether the run it drew is over."""
    waves = read_table(run_dir)
    records = read_log(run_dir / MACHINE_LOG_NAME)
    rows = build_rows(waves, records, moment, live_sources(args.claude_bin, run_dir))
    print(
        render(rows, run_dir.name, len(waves), moment, colour=colour_wanted(args)),
        flush=True,
    )
    emit_toasts(rows, toast_state_path(args, run_dir), args.tmux_bin)
    return over(records)


def hold():
    """Keep the finished frame on screen: no more drawing, and nothing closes the window."""
    while True:
        time.sleep(HOLD_SECONDS)


def run_dashboard(args):
    """Draw the run once, or keep drawing it over itself until the run is over; returns 0."""
    run_dir = run_directory(args)
    if args.refresh is None:
        draw(args, run_dir, args.now or now())
        return 0
    while True:
        print(CLEAR_SCREEN, end="")
        if draw(args, run_dir, args.now or now()):
            return hold()
        time.sleep(args.refresh)


def dashboard_command(args, run_dir):
    """The command line the window runs: this script's own dashboard, in its refresh loop."""
    command = [
        sys.executable, str(pathlib.Path(__file__).resolve()), "dashboard",
        "--run-dir", str(run_dir),
        "--refresh", str(args.refresh if args.refresh is not None else DEFAULT_REFRESH_SECONDS),
    ]
    if args.toast_state:
        command += ["--toast-state", str(args.toast_state)]
    return shlex.join(command)


def recorded_window(run_dir):
    """The dashboard window this run recorded, or None if it has never had one."""
    try:
        return (run_dir / WINDOW_RECORD_NAME).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def live_windows(tmux_bin):
    """Every window id tmux currently has, across every session."""
    result = subprocess.run(
        [tmux_bin, "list-windows", "-a", "-F", "#{window_id}"], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise MonitorError(f"tmux list-windows failed: {(result.stderr or '').strip()}")
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


@contextlib.contextmanager
def window_lock(run_dir):
    """Hold the run's dashboard window for the whole check-create-record.

    Reading the record, asking tmux, creating a window and writing the id back is one decision:
    two callers interleaving inside it both find nothing recorded and both create a window, which
    is the one thing this command exists to prevent.
    """
    with (run_dir / WINDOW_LOCK_NAME).open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def run_window(args):
    """Give the run its one dashboard window and print its id; returns 0.

    The whole lifecycle is here and idempotent: the recorded window is reused while it is alive,
    a window the operator closed is recreated on the next call — which is what makes this safe for
    a resuming coordinator to re-run — and nothing here ever closes one.
    """
    run_dir = run_directory(args)
    with window_lock(run_dir):
        return make_window(args, run_dir)


def make_window(args, run_dir):
    """Reuse the run's live dashboard window, or create and record one; returns 0."""
    recorded = recorded_window(run_dir)
    if recorded and recorded in live_windows(args.tmux_bin):
        print(recorded)
        return 0
    result = subprocess.run(
        [
            args.tmux_bin, "new-window", "-d", "-P", "-F", "#{window_id}",
            "-n", DASHBOARD_WINDOW_NAME, "-t", args.session,
            dashboard_command(args, run_dir),
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise MonitorError(f"tmux new-window failed: {(result.stderr or '').strip()}")
    window_id = result.stdout.strip()
    if not window_id:
        raise MonitorError("tmux new-window printed no window id")
    (run_dir / WINDOW_RECORD_NAME).write_text(window_id + "\n", encoding="utf-8")
    print(window_id)
    return 0


def transcript_root(spec):
    """The directory one executor keeps its transcripts in, as this machine has it configured."""
    variable, home, subdirectory = spec
    configured = os.environ.get(variable)
    return pathlib.Path(configured or pathlib.Path.home() / home) / subdirectory


def within_path(path, root):
    """Whether `path` is `root` or below it after aliases and symlinks are resolved."""
    resolved_path = os.path.realpath(str(path))
    resolved_root = os.path.realpath(str(root))
    try:
        return os.path.commonpath((resolved_path, resolved_root)) == resolved_root
    except ValueError:
        return False


def transcript_records(path):
    """Every line of one transcript, oldest first, as its object or `MALFORMED`.

    Yielded rather than collected, so a reader that recognises the first record as another
    worktree's stops the read there instead of pulling a session's whole history into memory.

    A line that does not parse is passed on as `MALFORMED` rather than dropped: the pass runs
    after the run is over, so no transcript is still being written, and a line nobody can read
    is a hole in the figures the reader must be able to refuse to bill. A file that cannot be
    opened at all raises OSError, which the caller diagnoses the same way.
    """
    with pathlib.Path(path).open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                yield MALFORMED
                continue
            yield record if isinstance(record, dict) else MALFORMED


def integer(value):
    """`value` as a token count: what a counter it was not written into contributes is nothing."""
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def zero_counters():
    """The four counters at nothing spent, which is what a session with no usage in it cost."""
    return dict.fromkeys(COUNTERS, 0)


def add_counters(into, counters):
    """Add one session's counters into a running set, and return the set they were added to."""
    for name in COUNTERS:
        into[name] += counters[name]
    return into


def counted(counters):
    """Every token the session moved: the four counters are disjoint, so this is their sum."""
    return sum(counters[name] for name in COUNTERS)


def billed(session, counters):
    """A transcript this worktree's child wrote, and what it says it spent."""
    return {"session": session, "counters": counters, "problem": None}


def unusable(problem):
    """A transcript this worktree's child wrote that cannot be billed, and why not."""
    return {"session": None, "counters": None, "problem": problem}


def request_counters(record, already):
    """One Claude record's four counters, or None where it has no usage or repeats a counted one.

    Usage is counted once per request: a transcript forked from another repeats the records it
    was forked from, and a record repeated is the same tokens spent once. `already` is the set of
    requests counted so far, which this adds to.
    """
    message = record.get("message")
    usage = message.get("usage") if isinstance(message, dict) else None
    if not isinstance(usage, dict):
        return None
    request = record.get("requestId") or record.get("uuid")
    if request is not None:
        if request in already:
            return None
        already.add(request)
    return {
        "input": integer(usage.get("input_tokens")),
        "output": integer(usage.get("output_tokens")),
        "cache_read": integer(usage.get("cache_read_input_tokens")),
        "cache_creation": integer(usage.get("cache_creation_input_tokens")),
    }


def claude_usage(records, worktree):
    """One Claude transcript read against `worktree`: `billed`, `unusable`, `UNDETERMINED`, or
    None where nothing in it says it ran there.
    """
    counters = zero_counters()
    session = None
    first_cwd = None
    damaged = False
    already = set()
    for record in records:
        if record is MALFORMED:
            damaged = True
            continue
        cwd = record.get("cwd")
        if cwd is not None:
            if first_cwd is None:
                if not within_path(cwd, worktree):
                    return None
                first_cwd = cwd
            elif not within_path(cwd, worktree):
                return unusable(f"it names both {first_cwd} and {cwd}")
        session = record.get("sessionId") or session
        found = request_counters(record, already)
        if found is not None:
            add_counters(counters, found)
    if first_cwd is None:
        # Nothing in it said where it ran, so whose tokens these are was never established.
        return UNDETERMINED if damaged else None
    if damaged:
        return unusable("it carries a line that does not parse")
    return billed(session, counters)


def codex_usage(records, worktree):
    """One Codex rollout read against `worktree`: `read`, `unusable`, `UNDETERMINED`, or None.

    The rollout carries a running total after every turn, so the last one it wrote is the
    session's whole usage. Its `input_tokens` counts the cached tokens inside itself, which the
    other three counters do not, so the cached ones are taken back out here.
    """
    session = None
    home = None
    damaged = False
    reported = None
    for record in records:
        if record is MALFORMED:
            damaged = True
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if record.get("type") == "session_meta":
            cwd = payload.get("cwd")
            if home is None:
                if cwd is None or not within_path(cwd, worktree):
                    return None
                home = cwd
                session = payload.get("id") or session
            else:
                return unusable(f"it opens more than one session, in {home} and in {cwd}")
        info = payload.get("info")
        if payload.get("type") == "token_count" and isinstance(info, dict):
            usage = info.get("total_token_usage")
            if isinstance(usage, dict):
                reported = usage
    if home is None:
        return UNDETERMINED if damaged else None
    if damaged:
        return unusable("it carries a line that does not parse")
    if reported is None:
        return unusable("it reports no token count")
    cached = integer(reported.get("cached_input_tokens"))
    return billed(session, {
        "input": max(integer(reported.get("input_tokens")) - cached, 0),
        "output": integer(reported.get("output_tokens")),
        "cache_read": cached,
        "cache_creation": integer(reported.get("cache_write_input_tokens")),
    })


# Each executor's transcript root and the reader that understands what it writes there.
TRANSCRIPT_READERS = {
    CLAUDE: (CLAUDE_TRANSCRIPTS, claude_usage),
    CODEX: (CODEX_TRANSCRIPTS, codex_usage),
}


def diagnosed(detail):
    """A child whose figures could not be read: the reason, in place of the numbers."""
    return {"sessions": [], "counters": None, "detail": detail}


def no_figures(root, worktree, undetermined):
    """Why a child that ran left no figures behind, named precisely enough to go and look."""
    detail = f"no transcript under {root} was recorded in {worktree}"
    if undetermined:
        return detail + f" ({undetermined} could not be read at all)"
    return detail


def child_usage(executor, worktree):
    """What one child spent: its sessions, their counters, or the diagnosis in place of both.

    Every failure on this path is diagnosed rather than raised, and a child with one unbillable
    transcript is diagnosed whole rather than billed for the rest: a total that quietly leaves
    out what could not be read is worse than no total at all. The pass runs after the run is
    over, so what it cannot read it will never be able to read.
    """
    spec, usage_of = TRANSCRIPT_READERS[executor]
    root = transcript_root(spec)
    sessions = {}
    counters = zero_counters()
    undetermined = 0
    problems = []
    for path in sorted(root.glob(TRANSCRIPT_GLOB)):
        try:
            found = usage_of(transcript_records(path), worktree)
        except OSError:
            undetermined += 1
            continue
        if found is None:
            continue
        if found is UNDETERMINED:
            undetermined += 1
            continue
        if found["problem"]:
            problems.append(f"{path}: {found['problem']}")
            continue
        session = str(found["session"]) if found["session"] else path.stem
        if session in sessions:
            # Two files claiming one session are two answers to what it spent, and adding them up
            # would bill the same tokens twice.
            problems.append(f"{path}: session {session} is also claimed by {sessions[session]}")
            continue
        sessions[session] = path
        add_counters(counters, found["counters"])
    if problems:
        return diagnosed("; ".join(problems))
    if not sessions:
        return diagnosed(no_figures(root, worktree, undetermined))
    return {"sessions": sorted(sessions), "counters": counters, "detail": None}


def coordinator_transcript(session):
    """The one file that session's transcript is, or the reason there is no one file to read."""
    root = transcript_root(CLAUDE_TRANSCRIPTS)
    found = sorted(path for path in root.glob(TRANSCRIPT_GLOB) if path.stem == session)
    if not found:
        return None, f"no transcript named {session}.jsonl under {root}"
    if len(found) > 1:
        return None, f"{root} holds more than one {session}.jsonl: {', '.join(map(str, found))}"
    return found[0], None


def coordinator_usage(session):
    """What the session driving the run spent, as a row of the same shape as a child's.

    Named by its session id rather than found by worktree: the coordinator works in the repository
    the children's worktrees were cut from, so a cwd match would claim their transcripts too.

    This is the one transcript read while it is still being written, because the session runs the
    pass on itself. Only its last line is forgiven for not parsing: that one is the request in
    flight, half-written and unbilled either way. A line that does not parse with more of the
    session written after it is a hole in the history like any other, and is diagnosed rather
    than quietly left out of the figure.
    """
    row = {"ticket": COORDINATOR_ROW, "sessions": [], "counters": None, "detail": None}
    if not session:
        return {**row, "detail": "no session id was given to read a transcript for"}
    path, problem = coordinator_transcript(session)
    if problem:
        return {**row, "detail": problem}
    counters = zero_counters()
    already = set()
    damaged = False
    unparsed_last = False
    try:
        for record in transcript_records(path):
            if unparsed_last:
                # The line before this one did not parse and was not the file's last after all.
                damaged = True
                unparsed_last = False
            if record is MALFORMED:
                unparsed_last = True
                continue
            found = request_counters(record, already)
            if found is not None:
                add_counters(counters, found)
    except OSError as error:
        return {**row, "detail": f"{path} could not be read: {error.strerror}"}
    if damaged:
        return {**row, "detail": f"{path} carries a line that does not parse"}
    if not counted(counters):
        # A session that ran this pass spent tokens, so nothing counted means the file is not it.
        return {**row, "detail": f"{path} reports no usage"}
    return {**row, "sessions": [session], "counters": counters}


def cost_rows(records):
    """One row per launched ticket, in ticket order: what it was routed to, and what it spent.

    A ticket launched twice into the same worktree — a replacement child — is one row, and its
    figures are every session that ran there, because both children spent the ticket's tokens.
    """
    launches = {}
    for record in records:
        if record.get("event") == "launch":
            launches[str(record.get("ticket"))] = record

    rows = []
    for ticket, launch in sorted(launches.items()):
        executor = str(launch.get("executor", ""))
        worktree = launch.get("worktree")
        if executor not in TRANSCRIPT_READERS:
            # Not a missing transcript but a log that cannot be billed at all, and the run's own
            # writer would refuse the executor too: loud, rather than a line nobody can check.
            raise MonitorError(
                f"the launch for {ticket} names executor {executor or '(none)'},"
                f" which is neither {CLAUDE} nor {CODEX}"
            )
        if not worktree:
            usage = diagnosed("the launch event names no worktree to read a transcript in")
        else:
            usage = child_usage(executor, worktree)
        rows.append({
            "ticket": ticket,
            "executor": executor,
            "model": str(launch.get("model", "")),
            **usage,
        })
    return rows


def figures(counters):
    """The five number cells of one row, or the same five saying there was nothing to count."""
    if counters is None:
        return [NO_FIGURE] * (len(COUNTERS) + 1)
    return [str(counters[name]) for name in COUNTERS] + [str(counted(counters))]


def run_total(rows):
    """What the whole run spent: every row that has figures, added up."""
    counters = zero_counters()
    for row in rows:
        if row["counters"] is not None:
            add_counters(counters, row["counters"])
    return counters


def render_cost(rows, coordinator=None):
    """The rollup the report carries: a row per child, the run's total, then any diagnosis.

    The coordinator's row sits beneath the total rather than inside it: it is what the run's
    judgment cost, read against the children's work, not another share of that work.
    """
    cells = [list(COST_COLUMNS)]
    cells += [
        [row["ticket"], row["executor"] or NO_FIGURE, row["model"] or NO_FIGURE]
        + figures(row["counters"])
        for row in rows
    ]
    cells.append([TOTAL_ROW, NO_FIGURE, NO_FIGURE] + figures(run_total(rows)))
    if coordinator is not None:
        cells.append([COORDINATOR_ROW, CLAUDE, NO_FIGURE] + figures(coordinator["counters"]))
        rows = rows + [coordinator]
    widths = [max(len(row[column]) for row in cells) for column in range(len(COST_COLUMNS))]
    lines = [
        COLUMN_GAP.join(value.ljust(width) for value, width in zip(row, widths)).rstrip()
        for row in cells
    ]
    notes = [f"{row['ticket']} not measured: {row['detail']}" for row in rows if row["detail"]]
    if notes:
        lines += [""] + notes
    return "\n".join(lines)


def log_session_cost(log, row):
    """Append this child's one `session-cost` line, through the log's own writer."""
    command = [
        sys.executable, str(MACHINE_LOG), "--log", str(log), "session-cost",
        "--ticket", row["ticket"], "--executor", row["executor"], "--model", row["model"],
    ]
    if row["sessions"]:
        command += ["--session", SESSION_SEPARATOR.join(row["sessions"])]
    if row["counters"] is not None:
        for name in COUNTERS:
            command += [f"--{name.replace('_', '-')}-tokens", str(row["counters"][name])]
        command += ["--total-tokens", str(counted(row["counters"]))]
    if row["detail"]:
        command += ["--detail", row["detail"]]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise MonitorError(f"machine log append failed: {result.stderr.strip()}")


def run_cost(args):
    """Append one `session-cost` per launched child and print the run's rollup; returns 0.

    The coordinator's row is printed and not logged: the log's `session-cost` is a launched
    ticket's line, and the session that drives the run is not one.
    """
    rows = cost_rows(read_log(args.log))
    for row in rows:
        log_session_cost(args.log, row)
    coordinator = None
    if args.coordinator_session is not None:
        coordinator = coordinator_usage(args.coordinator_session)
    print(render_cost(rows, coordinator), flush=True)
    return 0


def uncommitted(worktree):
    """The paths left uncommitted in `worktree`, beside the guard assets that were installed.

    An asset is only forgiven where it is what the renderer left behind: an untracked file at one
    of those paths. A guard path that is staged, modified or renamed into is the child's work under
    a name the check would otherwise wave through, so it counts like any other unfinished change.
    """
    status = git(worktree, "status", "--porcelain", "--untracked-files=all")
    paths = []
    for line in status.splitlines():
        code, path = line[:2], line[3:].strip()
        # A rename is reported as `old -> new`; the new name is the one that is standing there.
        path = path.split(" -> ")[-1].strip('"')
        if not path or (code == UNTRACKED_CODE and path in GUARD_ASSET_PATHS):
            continue
        paths.append(path)
    return paths


def receipt_problem(worktree, sha, base):
    """Why this receipt does not hold, or None when it does.

    The three checks are the ones the crew contract has always made on a completion receipt, made
    here without a model: the sha is the worktree's head over all forty characters — children
    compose a receipt whose short prefix matches and whose tail is invented — the branch grew from
    the base it was cut from and carries work that base did not, and nothing is left uncommitted
    behind it.
    """
    if not FULL_SHA.match(sha):
        return f"{sha} is not a 40-character sha"
    head = git(worktree, "rev-parse", "HEAD")
    if head != sha:
        return f"sha does not match worktree head {head}"
    if not descends_from(worktree, base):
        return f"branch does not descend from {base}"
    if git(worktree, "rev-list", "--count", f"{base}..HEAD") == "0":
        return f"branch is not ahead of {base}"
    left = uncommitted(worktree)
    if left:
        return "uncommitted changes: " + " ".join(left)
    return None


def log_receipt(log, ticket, sha):
    """Append the one `receipt` line a landable verdict earns, through the log's own writer."""
    result = subprocess.run(
        [
            sys.executable, str(MACHINE_LOG), "--log", str(log), "receipt",
            "--ticket", ticket, "--verdict", LANDABLE, "--sha", sha,
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise MonitorError(f"machine log append failed: {result.stderr.strip()}")


def run_verify(args):
    """Print this receipt's verdict; returns 0 when it holds, 1 when it does not."""
    problem = receipt_problem(args.worktree, args.sha, args.base)
    if problem is not None:
        print(f"{args.ticket} invalid {problem}")
        return 1
    if args.log:
        log_receipt(args.log, args.ticket, args.sha)
    print(f"{args.ticket} {LANDABLE} {args.sha}")
    return 0


def timestamp_argument(value):
    moment = parse_timestamp(value)
    if moment is None:
        raise argparse.ArgumentTypeError(f"{value} is not {TIMESTAMP_FORMAT}")
    return moment


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--claude-bin", default=CLAUDE_BIN, help="the Claude CLI to snapshot with")
    parser.add_argument("--tmux-bin", default=TMUX_BIN, help="the tmux to toast and split through")
    commands = parser.add_subparsers(dest="command", required=True)

    def run_command(name, help_text):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--run-dir", required=True, help="the run's directory")
        command.add_argument("--toast-state", help="where this run remembers what it has toasted")
        return command

    dashboard = run_command("dashboard", "draw the whole run as a table and toast what changed")
    dashboard.set_defaults(handler=run_dashboard)
    dashboard.add_argument(
        "--refresh", type=float, help="redraw every this many seconds instead of drawing once"
    )
    dashboard.add_argument(
        "--now", type=timestamp_argument,
        help="the moment elapsed times are measured to, stamped as the log stamps (default: now)",
    )
    dashboard.add_argument(
        "--no-color", action="store_true", help="draw plain text even where colour would show"
    )

    window = run_command("window", "create, reuse or recreate the run's one dashboard window")
    window.set_defaults(handler=run_window)
    window.add_argument("--session", required=True, help="the tmux target the window is created in")
    window.add_argument(
        "--refresh", type=float,
        help=f"the window's redraw interval in seconds (default: {DEFAULT_REFRESH_SECONDS})",
    )

    cost = commands.add_parser("cost", help="record what each child spent and roll the run up")
    cost.set_defaults(handler=run_cost)
    cost.add_argument("--log", required=True, help="the run's machine log")
    cost.add_argument(
        "--coordinator-session",
        help="the id of the session driving the run, whose transcript becomes the coordinator row",
    )

    verify = commands.add_parser("verify", help="check a child's completion receipt")
    verify.set_defaults(handler=run_verify)
    verify.add_argument("--ticket", required=True, help="the ticket the receipt is for")
    verify.add_argument("--worktree", required=True, help="the worktree the child worked in")
    verify.add_argument("--sha", required=True, help="the sha the child's receipt claimed")
    verify.add_argument("--base", required=True, help="the commit that worktree was cut from")
    verify.add_argument("--log", help="the machine log a landable receipt is appended to")

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except MonitorError as error:
        print(f"MONITOR ERROR {error}", file=sys.stderr)
        return MONITOR_ERROR_EXIT
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
