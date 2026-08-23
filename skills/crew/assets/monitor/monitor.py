#!/usr/bin/env python3
"""The monitor's operator surface: the run dashboard, its toasts, the receipt check, the cost pass.

    dashboard  draw the whole run as a table — one row per ticket of every wave — and toast what
               just became true, refreshing in place when asked to
    window     draw the run on the surfaces its repo chose: create, reuse or recreate the run's one
               dedicated tmux window running that loop, and write the pin that names the live run
    unpin      at the end of the run, take that pin back out of the registry
    pin        draw one frame of the live run into the coordinator's Claude Code statusline, or
               draw nothing at all
    pin-install  wire that same frame into the operator's Claude Code statusline, reversibly
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
import hashlib
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import time
import tomllib

# The account module beside this one: what a row's account binding is, and the one place a binding
# becomes an environment. A dashboard that re-derived that rule would answer for one account with
# another's login.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import accounts  # noqa: E402
import machine_log  # noqa: E402
import run_plan  # noqa: E402

TIMESTAMP_FORMAT = machine_log.TIMESTAMP_FORMAT
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

# A pin is named for the run it names, so a run re-pinning itself every wave still has one pin.
PIN_NAME_LENGTH = 16

# Which surface a run draws itself on: an explicit project value, the machine preference recorded
# by pin-install for a silent project, or the shipped window default.
DASHBOARD_SECTION = "dashboard"
SURFACE_KEY = "surface"
SURFACE_WINDOW = "window"
SURFACE_PIN = "pin"
SURFACE_BOTH = "both"
SURFACES = (SURFACE_WINDOW, SURFACE_PIN, SURFACE_BOTH)
DEFAULT_SURFACE = SURFACE_WINDOW

# The Ticket state vocabulary (`docs/glossary.md`): the words the operator reads, and the only
# words this script draws. Every source state — a tmux process status, a settlement verdict, a
# monitor internal — is mapped into one of them before it reaches a frame.
PENDING = "pending"
RUNNING = "running"
WAITING = "waiting"
# The two intervals the run used to have no word for. A child sent the merge driver's rework
# instruction is working on the conflict, not stuck at anything, so its row says so instead of
# wearing the abnormal `waiting` a blocked merge leaves behind; a wave that has taken its last
# receipt is being merged, so its unmerged rows say that rather than sitting `landable` while the
# operator reads a run that lands in under a second as a hung one.
REWORKING = "reworking"
SETTLING = "settling"
PARKED = "parked"
LANDABLE = "landable"
MERGED = "merged"
FAILED = "failed"
VANISHED = "vanished"
# The order the summary line counts them in: the way a ticket travels, start to finish.
STATE_ORDER = (
    PENDING, RUNNING, WAITING, REWORKING, PARKED, LANDABLE, SETTLING, MERGED, FAILED, VANISHED,
)
# The states that owe the operator an explanation; every other row stays quiet. `reworking` is one
# of them: the word says the child is busy, and the annotation says what it was sent to do.
ABNORMAL_STATES = (WAITING, REWORKING, FAILED, VANISHED)
# The existing Claude Code statusline occupies two rows before the pin's frame is appended.
STATUSLINE_RESERVE_LINES = 2
FINAL_STATES = (MERGED, FAILED)

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
    # A merge that hit a conflict or went to the coordinator has not landed the branch and is not
    # queued to land either, so it leaves `landable` for the abnormal state that owes the operator
    # a line: "waiting its turn to merge" and "the merge blew up" are not the same row.
    "merge": ("result", {
        "clean": MERGED, "repaired": MERGED, "resolved": MERGED, "conflict": WAITING,
        "escalated": WAITING,
    }),
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
    CLAUDE: {
        "busy": RUNNING, "waiting": WAITING, "idle": WAITING, "shell": WAITING, "parked": PARKED,
    },
    CODEX: {"busy": RUNNING, "idle": WAITING, "stopped": VANISHED},
}
# What a child that has stopped is toasted as. The three situations send the operator to three
# different places, so none is announced in another's words. `shell` is the word only the sessions
# files carry (ADR-0012): the fallback command folds it into `busy`, so a tick drawn from the
# fallback draws such a child `running` and never reaches this line.
ATTENTION_TOASTS = {
    "waiting": "stuck at a permission prompt",
    "idle": "stopped without finishing",
    "shell": "sitting at a shell prompt",
}
# The qualifier each event carries, in the order an annotation looks for one.
EVENT_QUALIFIERS = ("verdict", "outcome", "result", "decision", "state")
ESCALATION_EVENT = "escalation"
# What a row is drawn `reworking` from: the merge the ladder ran out of rungs on, and the
# instruction the driver sends the child that lost the race, which the log carries as a ruling
# opening with the driver's merge marker. The pair is the whole signal — an escalated merge with
# no instruction after it is nobody's work yet, and it keeps the abnormal word it always had.
REVIEW_EVENT = "review"
REVIEW_RUNNING = "running"
# What the summary line says while the run is waiting on the operator, so a halted wave is never
# mistaken for a frozen frame.
AWAITING_RULING = "⚠ awaiting your ruling"
# The run directory's record of the driver driving it: the pid its loop wrote on the way in, which
# every deliberate exit takes away again and a kill cannot. A record naming a process that is not
# running is therefore a killed driver by construction — the one thing the operator could not see
# while the run stalled — and the summary line says so in the slot the ruling marker occupies,
# painted red, carrying the command that puts the run back.
DRIVER_RECORD_NAME = "driver.pid"
DRIVER_DEAD = "✖ driver dead — /crew {run} to resume"
DRIVER_DEAD_COLOUR = "31"

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
    PENDING: "90", RUNNING: "36", WAITING: "33", REWORKING: "33", PARKED: "35",
    LANDABLE: "32", SETTLING: "32", MERGED: "32", FAILED: "31", VANISHED: "31",
}
COLOUR_RESET = "\x1b[0m"

# The pin registry (`docs/monitor-dashboard.md`): the directory a live run leaves the file naming
# itself in, under the operator's Claude config, and the five things that file carries.
PIN_REGISTRY = ("CLAUDE_CONFIG_DIR", ".claude", "agentcrew/pins")
SURFACE_PREFERENCE_NAME = "surface"
PIN_SUFFIX = ".json"
PIN_GLOB = f"*{PIN_SUFFIX}"
PIN_RUN_DIR = "run_dir"
PIN_PID = "coordinator_pid"
PIN_SESSION = "tmux_session"
# The renderer and interpreter that draw this run's frame, recorded by the release that wrote the
# pin: paths that expire with a release belong to a file the run takes away with it, never to the
# operator's statusline wrapper, which no upgrade rewrites (ADR-0011).
PIN_RENDERER = "renderer"
PIN_INTERPRETER = "interpreter"
# ADR-0011's one exception to the silence contract, in one line: the registry holds something no
# frame can be drawn from, which is a wiring fault the operator can act on rather than the ordinary
# "nothing is running". The wrapper prints it when it can reach no renderer at all, and the
# renderer the wrapper reached prints it for the pins only a JSON parser can judge, so both halves
# of the statusline say the same sentence.
PIN_NOTICE = (
    "agentcrew: no pin in {registry} names a dashboard this machine still has — "
    "re-dispatch the run to repin it, or clear that directory"
)
# What tmux exports into every session it runs — socket path, client pid, session id — which is
# where the caller's own session is read from before tmux itself is asked.
TMUX_ENVIRONMENT = "TMUX"
TMUX_ENVIRONMENT_FIELDS = 3
# tmux's own spelling of a session id, and the suffix a session is addressed as a target by.
SESSION_PREFIX = "$"
SESSION_TARGET_SUFFIX = ":"

# The Run plan's per-ticket account binding is resolved when the plan is built. Monitor consumes
# that binding directly and never resolves an account name or tolerates a partial table.

# The Claude lane's primary live source (ADR-0012): the CLI's own per-session files, one JSON
# object per live session, under the same configured home the pin registry hangs off. Reading them
# costs a few file reads, where the fallback command below costs a whole CLI start per tick.
CLAUDE_SESSIONS = ("CLAUDE_CONFIG_DIR", ".claude", "sessions")
SESSION_SUFFIX = ".json"
SESSION_CWD = "cwd"
SESSION_STATUS = "status"
# The fallback's one shared answer, beside the pin registry so every pane of the machine reads the
# same file, and how long a fetched answer stands before another tick goes and gets one. The
# window is what bounds the spawn rate to the machine rather than to the pane count; a failed
# fetch is cached too, so a broken CLI cannot be asked seven times a second either.
AGENTS_CACHE_NAME = "agents-cache.json"
AGENTS_CACHE_SECONDS = 10.0
AGENTS_CACHE_FETCHED = "fetched_at"
AGENTS_CACHE_STATES = "states"
# What the run's machine log records when a tick could not read the sessions directory, so a
# relocated directory is diagnosable without the statusline ever saying a word (ADR-0008). One
# line per run: a tick appends it only while the run's own log carries none.
LIVE_SOURCE_EVENT = "live-source"
FALLBACK_SOURCE = "command"
FALLBACK_REASON = "the sessions directory {directory} could not be read"

DEFAULT_REFRESH_SECONDS = 5.0
# How long one statusline tick gives a live source before drawing its row `unknown` instead. The
# tick draws the status line the operator's whole session depends on, so nothing in it may wait
# indefinitely.
DEFAULT_TIMEOUT_SECONDS = 2.0
# The tick the pin's install sets when the operator has none, in seconds. Deliberately its own
# constant rather than the window's: one is a redraw loop the operator watches on purpose, the
# other is a statusline command re-run under every session, and their costs are not the same.
DEFAULT_PIN_REFRESH_SECONDS = 2
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
# The fallback cache has nothing this tick may use — no file, an unreadable one, or one older than
# its freshness window. Distinct from the None a cached failure carries, which is an answer.
NO_CACHE = object()
# The tick's budget was spent before a source finished answering. Distinct from the None that says
# a source is not there: a lane that simply ran out of time is drawn `unknown` and nothing is
# reached for in its place, because the fallback is slower than the source that just timed out.
OUT_OF_TIME = object()

# The operator's Claude Code wiring the pin's install edits, and the environment variable that
# moves it — the same one the executor itself honours, so the settings edited are the ones in use.
CLAUDE_SETTINGS = ("CLAUDE_CONFIG_DIR", ".claude", "settings.json")
# The wrapper the install writes beside those settings: it runs the operator's own statusline
# first and appends the pin's frame beneath, so nothing the operator already reads is lost.
PIN_WRAPPER_NAME = "agentcrew-statusline.sh"
# The line that wrapper carries: what the statusline ran before the install, and whether the
# install is the one that added the refresh interval. It is what makes `--uninstall` exact and a
# second `--apply` a no-op, and it lives in the wrapper so nothing else has to be kept in step.
INSTALL_MARKER = "# agentcrew pin-install "
INSTALL_RECORD_VERSION = 1
# What a backup is called: beside the file it copies, so a bad install is recoverable without git.
BACKUP_SUFFIX = ".agentcrew-backup"
STATUS_LINE_KEY = "statusLine"
COMMAND_KEY = "command"
TYPE_KEY = "type"
COMMAND_TYPE = "command"
REFRESH_INTERVAL_KEY = "refreshInterval"

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
        return machine_log.read_records(path)
    except OSError:
        return ()


def agent_states(claude_bin, timeout=None, binding=None):
    """Each live session's status by its `cwd`, or None when the agents list cannot be read.

    The fallback lane and nothing else (ADR-0012): every call here is a whole CLI start, which is
    why the source a tick reaches for first is `session_states` and why what this answers is
    shared machine-wide rather than fetched again by the next pane.

    A worktree the list carries twice has no single status, so it is reported as the duplicate the
    wake monitor calls an error — the dashboard draws it and leaves the stopping to the wake-up.

    A list that does not answer within `timeout` is one that could not be read: a caller drawing
    into a statusline cannot wait on it, and a row drawn `unknown` says more than no frame at all.
    """
    try:
        result = subprocess.run(
            [claude_bin, "agents", "--json"],
            capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
            # The account module's answer for this binding: the named account's home, or None
            # where the binding is inherited and this process's own environment is the question
            # being asked. Spelling the default home out here would ask a login that is not the
            # one the child was launched on (#110).
            env=accounts.process_environment(binding),
        )
    except subprocess.TimeoutExpired:
        return None
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


def read_without_blocking(path):
    """One file's whole text, without ever waiting on what kind of file it turned out to be.

    A path that is not a regular file — a fifo left in the run directory — makes an ordinary
    read wait for a writer that may never come, and no part of a statusline tick may wait.
    """
    descriptor = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(descriptor, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def codex_states(run_dir, timeout=None):
    """Each live Codex child's status by the worktree its bridge recorded, never by spelling.

    A Codex child is invisible to `claude agents --json`, so its bridge state file is the only
    thing that knows it is alive: read nothing here and every Codex ticket of a run is drawn
    `vanished` from the first frame. A file that cannot be read is not a child that stopped, so it
    is passed over rather than counted as one.

    The whole read is bounded by `timeout`, as the other lane's is: a source that has spent its
    budget answers None — the lane a caller draws `unknown` — rather than holding up the frame.
    """
    deadline = None if timeout is None else time.monotonic() + timeout
    states = {}
    for path in sorted((pathlib.Path(run_dir) / CODEX_STATE_DIR).glob(CODEX_STATE_GLOB)):
        if deadline is not None and time.monotonic() >= deadline:
            return None
        try:
            state = json.loads(read_without_blocking(path))
        except (OSError, ValueError):
            continue
        cwd = state.get("cwd") if isinstance(state, dict) else None
        if not cwd:
            continue
        key = worktree_key(cwd)
        states[key] = DUPLICATE if key in states else str(state.get("status", UNKNOWN))
    return states


def sessions_directory(account=None):
    """The CLI's own per-session files, under one account's configuration home.

    One seam for both (ADR-0012): a test that moves the config directory moves the sessions files,
    the fallback's cache and the registry together, which is how a fixture gets a machine of its
    own without a flag per path. An account named here moves all three the same way, for the one
    ticket that runs under it.
    """
    return transcript_root(CLAUDE_SESSIONS, account)


def session_states(timeout=None, account=None):
    """Each live session's status by its `cwd`, read from the files the CLI keeps itself, or None
    when that directory is not there to be read.

    The Claude lane's primary source (ADR-0012). It answers the same question `agent_states` does
    — which sessions are alive and what each is doing — for the cost of a few file reads instead
    of a whole CLI start, and it carries one word that lane cannot: `shell`, a child sitting at a
    shell prompt, which the command folds into `busy`.

    A half-written file is skipped rather than fatal, as the Codex lane's are: the CLI rewrites
    these on every status change, so a tick that catches one mid-write reads the rest and draws.
    The whole read is bounded by `timeout` like the other lane's, and the two ways it can fail are
    kept apart: a directory that cannot be listed — gone, moved, or not this user's to read —
    answers None, the caller's signal to fall back, while a budget spent answers `OUT_OF_TIME`,
    which is not a reason to go and start a CLI.
    """
    directory = sessions_directory(account)
    try:
        # Listed rather than globbed: a glob answers an unreadable directory with no matches, so a
        # tick that trusted it would draw every live child `vanished` instead of falling back.
        names = os.listdir(directory)
    except OSError:
        return None
    paths = sorted(directory / name for name in names if name.endswith(SESSION_SUFFIX))
    deadline = None if timeout is None else time.monotonic() + timeout
    states = {}
    for path in paths:
        if deadline is not None and time.monotonic() >= deadline:
            return OUT_OF_TIME
        try:
            session = json.loads(read_without_blocking(path))
        except (OSError, ValueError):
            continue
        if not isinstance(session, dict) or not session.get(SESSION_CWD):
            continue
        cwd = worktree_key(session[SESSION_CWD])
        states[cwd] = DUPLICATE if cwd in states else str(session.get(SESSION_STATUS, UNKNOWN))
    return states


def agents_cache_path(account=None):
    """Where the fallback's shared answer lives: beside the pin registry, not inside it.

    Machine-level on purpose: every pane of every session reads this same file, which is what
    stops the fallback from spawning once per pane per tick. One such file per account, because
    what it holds is one account's list of live sessions and the accounts' lists are disjoint —
    a single file would serve one account's children as the answer about another's.
    """
    return transcript_root(PIN_REGISTRY, account).parent / AGENTS_CACHE_NAME


def read_agents_cache(moment, account=None):
    """The fallback answer this machine already has and may still use, or `NO_CACHE`.

    A cached failure is an answer too — it is returned as the None the lane draws `unknown` from —
    so a CLI that cannot answer is asked once per window rather than once per tick.
    """
    try:
        cached = json.loads(read_without_blocking(agents_cache_path(account)))
    except (OSError, ValueError):
        return NO_CACHE
    if not isinstance(cached, dict):
        return NO_CACHE
    fetched = cached.get(AGENTS_CACHE_FETCHED)
    if not isinstance(fetched, (int, float)) or isinstance(fetched, bool):
        return NO_CACHE
    if not 0 <= moment - fetched < AGENTS_CACHE_SECONDS:
        return NO_CACHE
    states = cached.get(AGENTS_CACHE_STATES)
    return states if isinstance(states, dict) else None


def write_agents_cache(states, moment, account=None):
    """Share one fetch with every other pane on the machine; returns nothing.

    Put in place by rename, so no tick ever reads a half-written answer. A cache that could not be
    written is not a failure anybody is told about: the next tick simply fetches again, which is
    exactly what would have happened anyway.
    """
    path = agents_cache_path(account)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps({AGENTS_CACHE_FETCHED: moment, AGENTS_CACHE_STATES: states}) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except OSError:
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)


def announce_fallback(run_dir, records, timeout=None, account=None, announced=None):
    """Record in the run's own log that this lane is reading its fallback; returns nothing.

    ADR-0008's silence holds on screen, so the one place a relocated sessions directory can be
    diagnosed from is here. One line per run — not per tick, and not per account: the run's log is
    read for the frame anyway, so a run that has already said it is in fallback says nothing more.

    `announced` is what carries that across the accounts of one frame, which the log alone cannot:
    every account of a frame is read against the same snapshot of the log, taken before any of
    them had written anything, so a second falling-back account would otherwise repeat the line
    the first had just written. The accounts are read in the wave table's order, so the directory
    the line names is the first one a run could not read.
    """
    if run_dir is None or announced:
        return
    if any(record.get("event") == LIVE_SOURCE_EVENT for record in records):
        return
    if announced is not None:
        announced.append(account)
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            [
                sys.executable, str(MACHINE_LOG),
                "--log", str(pathlib.Path(run_dir) / MACHINE_LOG_NAME), LIVE_SOURCE_EVENT,
                "--lane", CLAUDE, "--source", FALLBACK_SOURCE,
                "--reason", FALLBACK_REASON.format(directory=sessions_directory(account)),
            ],
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=timeout,
        )


def claude_states(claude_bin, run_dir=None, timeout=None, records=(), binding=None,
                  announced=None):
    """What one account's Claude children are doing: its sessions files, or the command behind
    them.

    The primary source is read every time and cached nowhere — it is already as cheap as a cache
    would be, so nothing here can go stale. Only a directory that cannot be read at all sends the
    tick to the command, and there the machine's shared answer stands for a freshness window
    before any pane spawns another (ADR-0012). Both halves are asked of the account's own home, so
    an account more costs a few directory listings on the path a tick actually takes, and a spawn
    only on the exceptional path that already spawns.

    The binding's two halves answer two different questions here, and both are asked of the
    account the child is actually on. Its files — the sessions directory — are at the directory
    the row carries, in either mode: a child writes its session under the home it was launched
    with and goes on running there, whatever login a later tick happens to be started under. The
    command is a login, so it is spawned in the binding's own environment, which for an inherited
    binding is this tick's, untouched: spelling a default home out is how an account-less process
    came to be told it was not logged in (#110), and a fallback answered by a login that is not
    the child's is a live child drawn `vanished`.

    The shared answer is filed under the account whose login gave it (`accounts.login_home`), not
    under the directory being read: caching one account's list in another's file would serve every
    pane on the machine an answer about children it never had.
    """
    home = binding.directory if binding else None
    states = session_states(timeout, home)
    if states is OUT_OF_TIME:
        return None
    if states is not None:
        return states
    announce_fallback(run_dir, records, timeout, home, announced)
    answered_by = accounts.login_home(binding)
    cached = read_agents_cache(time.time(), answered_by)
    if cached is not NO_CACHE:
        return cached
    fetched = agent_states(claude_bin, timeout, binding)
    write_agents_cache(fetched, time.time(), answered_by)
    return fetched


def table_bindings(plan):
    """Every account binding the wave table carries, once each, in the table's own order.

    Two rows on the same directory in different modes are two bindings, because the login one of
    them asks is not the login the other asks.
    """
    named = []
    for ticket in plan.tickets:
        binding = ticket.binding
        if binding not in named:
            named.append(binding)
    return named


def live_sources(claude_bin, run_dir, timeout=None, records=(), bindings=(None,)):
    """What each lane says about the children of each account binding the run touches.

    Keyed by binding and then by lane, because that is the order a row is read in: a ticket knows
    which account it runs under before it knows anything about its lane's source. The Claude lane
    is asked once per binding — its per-session files and its login are the account's — and the
    Codex lane is asked once for the run and shared, because a Codex child's bridge state lives in
    the run directory and no Claude profile has anything to say about it.
    """
    codex = codex_states(run_dir, timeout)
    # The one line a frame may record about falling back, shared by every account it reads.
    announced = []
    return {
        binding: {
            CLAUDE: claude_states(claude_bin, run_dir, timeout, records, binding, announced),
            CODEX: codex,
        }
        for binding in bindings
    }


def read_plan(run_dir):
    """The run's validated plan, preserving every wave and ticket in approved order.

    A dashboard drawn from a table nobody could read would be an empty frame, which is exactly the
    failure this window exists to make impossible — so an unreadable table stops the command
    instead.
    """
    path = pathlib.Path(run_dir) / WAVE_TABLE_NAME
    try:
        return run_plan.load(path)
    except run_plan.RunPlanError as error:
        raise MonitorError(str(error)) from error


def account_homes(run_dir):
    """The Claude profile directory each ticket's wave-table row names, keyed by ticket."""
    return {ticket.id: ticket.binding.directory for ticket in read_plan(run_dir).tickets}


def settling_state(record):
    """The word this record settles a ticket into, or None when it settles nothing."""
    key, words = SETTLED_STATES.get(str(record.get("event")), (None, {}))
    if key is None:
        return None
    return words.get(str(record.get(key)))


def reworking(facts, launch, sources, binding=None):
    """Whether this ticket's child is working on the conflict its escalated merge left behind.

    Three things make it true, and the order between the first two is the whole point: the last
    thing that settled the ticket is a merge the ladder escalated, and the run sent the rework
    instruction *after* it. A conflict that came back a second time stands on an instruction older
    than the merge it is standing on, and that one is the coordinator's to answer — so it keeps
    the abnormal `waiting` the operator is meant to read as "this needs a human".

    The third is the child itself. `reworking` is the one settled word that claims something is
    happening right now, so it is the one that asks its lane: a child that has gone idle, stopped
    at a prompt, or vanished under its instruction is not reworking anything, and drawing it as
    though it were would hide exactly the row the operator has to go and look at.
    """
    return facts.merge_rework_requested and working(launch, sources, binding)


def working(launch, sources, binding=None):
    """Whether this ticket's own lane can see a child of its own busy on it right now.

    Stricter than "the lane did not say `idle`": a reading that produced an anomaly answered about
    itself rather than about the child, and a state read off a source that could not be read is no
    evidence that anyone is working.
    """
    state, anomaly, status = live_state(launch, sources, binding)
    return state == RUNNING and anomaly is None and status is not None


def settling_wave(settlements, projection):
    """Whether this wave is between its last receipt and its merges, which is what `settling` says.

    Every ticket of the wave has settled, so the receipts are all in and the driver's per-wave
    merging is what happens next — measured in seconds, which is exactly why a row idling at
    `landable` through it reads as a merge that hung. A run that is over is not in that interval
    at all: nothing is coming for a branch that never landed, and `landable` is the true word for
    it.
    """
    if not settlements or any(record is None for record in settlements):
        return False
    return not projection.ended


def live_state(launch, sources, binding=None):
    """What a launched, unsettled ticket is doing now: its state, its anomaly, and its raw status.

    Each lane is read from its own source, chosen by the executor its launch names, because the
    two do not overlap: a Claude child is in the agents list and a Codex child is in its bridge
    state file, and asking either about the other's children answers `vanished`. The account
    binding the wave table routed the ticket to chooses which copy of those sources is asked, on
    the same reasoning: two accounts' live sources are disjoint, and asking one about the other's
    children answers `vanished` too.

    An anomaly is not a state: `duplicate` and `unknown` say what a reading did, not where the
    ticket got to, so the row keeps the state its own log line justifies and carries the anomaly
    as an annotation underneath. The raw status rides along because two of them mean the same
    state and different things to the operator — a permission prompt is not a finished turn.
    """
    executor = str(launch.get("executor", ""))
    if executor not in LIVE_STATES:
        return RUNNING, (UNKNOWN, f"the launch names executor {executor or '(none)'}"), None
    states = sources.get(binding, {}).get(executor)
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


def build_rows(plan, projection, moment, sources):
    """One row per ticket of every wave, in the order the approved table lists them.

    A ticket no `launch` event names has not started, which is what `pending` says: the frame
    shows the whole run from its first draw, so "not started yet" is never read as "lost". A
    ticket launched twice — a replacement child — is the one row its last launch describes.
    """
    rows = []
    for wave in plan.waves:
        number = str(wave.number)
        entries = wave.tickets
        # Read once for the whole wave, because two of the words a row can be drawn need the wave
        # rather than the ticket: `settling` is what the wave's last receipt puts its unmerged rows
        # into, and no row can know that from its own events.
        wave_facts = {
            entry.id: projection.ticket(entry.id)
            for entry in entries
        }
        wave_settled = {
            ticket: (
                facts.progress_event,
                settling_state(facts.progress_event) if facts.progress_event is not None else None,
            )
            for ticket, facts in wave_facts.items()
        }
        merging = settling_wave([settling for settling, _ in wave_settled.values()], projection)
        for entry in entries:
            ticket = entry.id
            binding = entry.binding
            facts = wave_facts[ticket]
            events = facts.events
            launch = facts.launch
            settling, settled_into = wave_settled[ticket]
            anomaly = None
            status = None
            if launch is None:
                state, started, end = PENDING, None, None
            else:
                started = parse_timestamp(launch.get("ts"))
                if settling is not None:
                    state, end = settled_into, parse_timestamp(settling.get("ts"))
                    if state == WAITING and reworking(facts, launch, sources, binding):
                        state = REWORKING
                    elif state == LANDABLE and merging:
                        state = SETTLING
                else:
                    state, anomaly, status = live_state(launch, sources, binding)
                    end = moment
            escalation_count = sum(record.get("event") == ESCALATION_EVENT for record in events)
            rows.append({
                "wave": number,
                "ticket": ticket,
                "title": entry.title,
                "executor": "/".join(
                    part for part in (entry.executor, entry.model)
                    if part
                ),
                "state": state,
                "status": status,
                "elapsed": elapsed(started, end),
                "started": started,
                "launched": launch is not None,
                "settled": settling is not None,
                "escalation_count": escalation_count,
                "annotations": annotations(events, state, anomaly, moment),
            })
    return rows


def terminal_width():
    """How wide the window is, as the terminal or `COLUMNS` has it."""
    return shutil.get_terminal_size(fallback=(FALLBACK_WIDTH, 24)).columns


def terminal_height():
    """How tall the terminal is, as the statusline environment or the terminal reports it."""
    return shutil.get_terminal_size(fallback=(FALLBACK_WIDTH, 24)).lines


def cut(text, width):
    """`text` in at most `width` columns, the last one spent saying it was cut."""
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    return text[: width - 1].rstrip() + ELLIPSIS


def summary(rows, run_id, waves, moment, awaiting_ruling=False, dead_driver=None):
    """The one line above the table: which run, how far through its waves, and how it stands.

    A run halted on a ruling says so between its counts and its clock: the frame is still being
    drawn, and what stopped moving is the run rather than the renderer.

    A run whose driver was killed says that instead, in the same slot. The two are mutually
    exclusive by the blanking rule — a driver that halted a wave on purpose released its record on
    the way out — and the slot is written that way so that they can never both be believed.
    """
    counts = {state: 0 for state in STATE_ORDER}
    for row in rows:
        counts[row["state"]] += 1
    started = [row["started"] for row in rows if row["started"] is not None]
    parts = [
        f"wave {len({row['wave'] for row in rows if row['launched']})}/{waves}",
        " ".join(f"{state}={counts[state]}" for state in STATE_ORDER if counts[state]),
        dead_driver or (AWAITING_RULING if awaiting_ruling else ""),
        f"elapsed {elapsed(min(started) if started else None, moment)}",
    ]
    return f"crew {run_id} — " + " · ".join(part for part in parts if part)


def paint(text, state, colour):
    """The state cell in its own colour, or exactly as it was when nothing can show one."""
    code = STATE_COLOURS.get(state) if colour else None
    return f"\x1b[{code}m{text}{COLOUR_RESET}" if code else text


def paint_segment(line, segment, code, colour):
    """That segment of an already-cut line in its own colour, or the line exactly as it was.

    Applied after the cut and never before it: an escape sequence counts toward a line's length,
    so colouring first would let a narrow window slice one in half and leave the operator's
    prompt painted. A banner the window cut away is simply not painted.
    """
    if not colour or segment is None or segment not in line:
        return line
    return line.replace(segment, f"\x1b[{code}m{segment}{COLOUR_RESET}", 1)


def fit_rows(rows, row_blocks, available, width):
    """Spend the rows left below the summary and header in the contract's fixed order.

    Settled ticket rows give up space first, annotations second, and live or pending ticket rows
    last. Once a ticket row is omitted, one of the remaining rows is reserved for the marker.
    """
    selected = list(range(len(rows)))
    omitted = 0
    content_budget = available

    def line_count(indexes, include_annotations):
        return sum(
            1 + (len(rows[index]["annotations"]) if include_annotations else 0)
            for index in indexes
        )

    while line_count(selected, True) > content_budget:
        settled_index = next(
            (index for index in selected if rows[index]["state"] in FINAL_STATES),
            None,
        )
        if settled_index is None:
            break
        selected.remove(settled_index)
        omitted += 1
        # The marker is itself a row, so a dropped ticket consumes one line for it.
        content_budget = max(available - 1, 0)

    include_annotations = line_count(selected, True) <= content_budget

    if line_count(selected, include_annotations) > content_budget:
        if omitted == 0:
            content_budget = max(available - 1, 0)
        row_capacity = content_budget
        omitted += len(selected) - row_capacity
        selected = selected[:row_capacity]

    lines = [
        line
        for index in selected
        for line in (row_blocks[index] if include_annotations else row_blocks[index][:1])
    ]
    if omitted:
        lines.append(cut(f"{ELLIPSIS} +{omitted} more", width))
    return lines


def render(
    rows, run_id, waves, moment, width=None, colour=False, max_lines=None, awaiting_ruling=False,
    dead_driver=None,
):
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

    def block(index, values):
        values = list(values)
        values[TITLE_COLUMN] = cut(values[TITLE_COLUMN], widths[TITLE_COLUMN])
        padded = [value.ljust(size) for value, size in zip(values, widths)]
        if index:
            padded[STATE_COLUMN] = paint(padded[STATE_COLUMN], rows[index - 1]["state"], colour)
        block_lines = [COLUMN_GAP.join(padded).rstrip()]
        if index:
            block_lines += [
                cut(ANNOTATION_PREFIX + note, width) for note in rows[index - 1]["annotations"]
            ]
        return block_lines

    lines = [paint_segment(
        cut(summary(rows, run_id, waves, moment, awaiting_ruling, dead_driver), width),
        dead_driver, DRIVER_DEAD_COLOUR, colour,
    )]
    header_lines = block(0, cells[0])
    row_blocks = [block(index, values) for index, values in enumerate(cells[1:], 1)]
    if max_lines is None:
        return "\n".join(
            lines + header_lines + [
                line for block_lines in row_blocks for line in block_lines
            ]
        )

    budget = max(max_lines, 0)
    if budget <= 1:
        return "\n".join(lines)
    lines += header_lines
    if budget <= len(lines):
        return "\n".join(lines)

    lines += fit_rows(rows, row_blocks, budget - len(lines), width)
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
        for occurrence in range(row["escalation_count"]):
            # Keep the original key for the first occurrence so an existing run's toast state
            # remains valid; later occurrences extend it with their ordinal in the append-only log.
            suffix = "" if occurrence == 0 else f":{occurrence}"
            said.append((f"escalated:{ticket}{suffix}", f"crew {ticket} escalated"))
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


def display(tmux_bin, text, timeout=None):
    """Show one toast in the operator's terminal. A tmux that will not show it is not an error."""
    try:
        subprocess.run(
            [tmux_bin, "display-message", text],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def emit_toasts(rows, state_path, tmux_bin, timeout=None):
    """Display the toasts this pass has grounds for and has not shown; returns their texts."""
    said = read_said(state_path)
    shown = []
    for key, text in toasts(rows):
        if key in said:
            continue
        display(tmux_bin, text, timeout=timeout)
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
    plan = read_plan(run_dir)
    records = read_log(run_dir / MACHINE_LOG_NAME)
    projection = machine_log.project(records)
    rows = build_rows(
        plan, projection, moment,
        live_sources(args.claude_bin, run_dir, records=records, bindings=table_bindings(plan)),
    )
    print(
        render(
            rows, run_dir.name, len(plan.waves), moment,
            colour=colour_wanted(args), awaiting_ruling=projection.halted,
            dead_driver=dead_driver_banner(run_dir),
        ),
        flush=True,
    )
    emit_toasts(rows, toast_state_path(args, run_dir), args.tmux_bin)
    return projection.ended


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
        # Named rather than left to the default, so the file this loop dedups its toasts through
        # is legible in the command itself — it is the file a pinned surface shares with it.
        "--toast-state", str(toast_state_path(args, run_dir)),
    ]
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


def machine_surface_preference():
    """The installed machine preference, or None when it is absent or unusable."""
    try:
        value = surface_preference_path().read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    return value if value in SURFACES else None


def configured_surface(args):
    """Which surface this run draws itself on, read from the project's config file.

    A project's explicit surface wins. A silent project inherits the machine preference recorded by
    pin-install, and a machine with no usable preference keeps the shipped window default.
    """
    machine_surface = machine_surface_preference()
    fallback = machine_surface or DEFAULT_SURFACE
    if not args.config:
        return fallback
    try:
        text = pathlib.Path(args.config).read_text(encoding="utf-8")
    except FileNotFoundError:
        return fallback
    except OSError as error:
        raise MonitorError(f"config {args.config} is unreadable: {error}")
    try:
        config = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise MonitorError(f"config {args.config} is unparsable: {error}")
    if DASHBOARD_SECTION not in config:
        return fallback
    section = config[DASHBOARD_SECTION]
    if not isinstance(section, dict):
        raise MonitorError(f"config {args.config}: [{DASHBOARD_SECTION}] must be a table")
    surface = section.get(SURFACE_KEY, fallback)
    if surface not in SURFACES:
        raise MonitorError(
            f"config {args.config}: unknown {DASHBOARD_SECTION} {SURFACE_KEY} {surface!r}, "
            f"one of {', '.join(SURFACES)}"
        )
    return surface


def pin_path(args, run_dir):
    """This run's one pin, named for the run directory it names.

    The name is a digest of the resolved run directory, so every wave of one run writes the same
    pin and the end of the run removes exactly what dispatch wrote — while two runs at once, whose
    directories differ, never collide. The registry is the one `pin` itself reads.
    """
    digest = hashlib.sha256(str(run_dir).encode("utf-8")).hexdigest()[:PIN_NAME_LENGTH]
    return pin_directory(args) / f"{digest}{PIN_SUFFIX}"


def write_pin(args, run_dir):
    """Name this live run in the pin registry: where it is, whose process it is, where that
    process runs, and what draws its frame.

    The pid is the whole crash story — a statusline tick draws nothing once it is gone — so a pin
    is never written without one. The renderer and interpreter are this release's own, recorded
    here rather than at install time because both are necessarily alive at this moment, while an
    install outlives every release that follows it. The file is put in place by rename, because a
    tick reading it half-written would draw nothing for a run that is very much alive.
    """
    if args.coordinator_pid is None:
        raise MonitorError(
            "a pinned surface needs --coordinator-pid, the pid of the session driving the run"
        )
    path = pin_path(args, run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    pin = {
        PIN_RUN_DIR: str(run_dir),
        PIN_PID: args.coordinator_pid,
        PIN_SESSION: args.session,
        PIN_RENDERER: str(pathlib.Path(__file__).resolve()),
        PIN_INTERPRETER: sys.executable,
    }
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        # Written as the characters themselves, not as escapes: the wrapper that reads these two
        # paths back is a shell script, and a home directory spelled in any script but Latin would
        # reach it as `\uXXXX` and name nothing.
        temporary.write_text(json.dumps(pin, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise MonitorError(f"the run's pin could not be written: {error}")
    return path


def run_window(args):
    """Draw the run on the surfaces its repo chose, printing the window's id where it has one;
    returns 0.

    The window's whole lifecycle is here and idempotent: the recorded window is reused while it is
    alive, a window the operator closed is recreated on the next call — which is what makes this
    safe for a resuming coordinator to re-run — and nothing here ever closes one. The pin is
    idempotent for the same reason: every wave re-writes the one file that names this run.
    """
    run_dir = run_directory(args)
    surface = configured_surface(args)
    if surface in (SURFACE_PIN, SURFACE_BOTH):
        write_pin(args, run_dir)
    if surface == SURFACE_PIN:
        return 0
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


def run_unpin(args):
    """Take this run's pin out of the registry; returns 0.

    The end of the run, after the report is written: the final frame lives in the report and the
    machine log, not on the operator's screen. A run that never wrote a pin ends through this same
    step and has nothing to remove, so a missing pin is success.
    """
    try:
        pin_path(args, run_directory(args)).unlink(missing_ok=True)
    except OSError as error:
        raise MonitorError(f"the run's pin could not be removed: {error}")
    return 0


def session_key(session):
    """One tmux session's identity, however the caller or the pin spelled it.

    tmux writes a session id as `$7`, addresses that session as the target `$7:` and exports the
    bare `7` into the environment, and all three name one session — so the spelling is taken off
    before two of them are ever compared. A session nobody named is None, which matches no pin.
    """
    text = str(session or "").strip().removesuffix(SESSION_TARGET_SUFFIX)
    return text.removeprefix(SESSION_PREFIX) or None


def caller_session(tmux_bin, timeout):
    """The tmux session this tick was called from, or None when nothing can say.

    The environment is asked first because it costs nothing and cannot hang: tmux exports the
    session id into every session it runs. tmux itself is only consulted when that variable is
    absent or is not the three fields it publishes.
    """
    fields = os.environ.get(TMUX_ENVIRONMENT, "").split(",")
    if len(fields) == TMUX_ENVIRONMENT_FIELDS and session_key(fields[-1]):
        return session_key(fields[-1])
    try:
        result = subprocess.run(
            [tmux_bin, "display-message", "-p", "#{session_id}"],
            capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return session_key(result.stdout) if result.returncode == 0 else None


def read_pin(path):
    """One pin as the run wrote it, or None when that file is not a pin at all.

    A pin names its run directory as an absolute realpath (ADR-0007), so it is resolved here and
    an aliased spelling never becomes a second run.
    """
    try:
        pin = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(pin, dict):
        return None
    run_dir = pin.get(PIN_RUN_DIR)
    pid = pin.get(PIN_PID)
    if not run_dir or not isinstance(pid, int) or isinstance(pid, bool):
        return None
    return {
        "path": pathlib.Path(path),
        "run_dir": pathlib.Path(worktree_key(run_dir)),
        "pid": pid,
        "session": session_key(pin.get(PIN_SESSION)),
    }


def read_registry(pin_dir):
    """Every pin the registry carries, in a fixed order, and how many of its files are not pins.

    That second number is the wrapper's case and nobody else's: a file named like a pin that
    cannot be read as one is invisible to a reader that collects only what it could parse, and it
    is exactly what ADR-0011's notice is about.
    """
    try:
        paths = sorted(pathlib.Path(pin_dir).glob(PIN_GLOB))
    except OSError:
        return [], 0
    pins = [pin for pin in (read_pin(path) for path in paths) if pin is not None]
    return pins, len(paths) - len(pins)


def sweep_registry(pins):
    """Take out every pin whose coordinator has gone; returns the pins that are still live.

    A run ends by unpinning itself, and a coordinator abandoned after a judgment-needed or
    driver-error pause never reaches that step — so without this the registry keeps a file naming
    a run nobody will ever resume, and every tick of every pane goes on reading it. Sweeping is
    safe because a pin is not state: every wave writes it again, so a run the operator resumes
    re-pins itself on its next dispatch.

    A pin that cannot be removed is left where it is. This is a statusline tick's housekeeping,
    not its job, and a registry on a read-only disk is nobody's emergency.
    """
    live = []
    for pin in pins:
        if alive(pin["pid"]):
            live.append(pin)
            continue
        with contextlib.suppress(OSError):
            pin["path"].unlink(missing_ok=True)
    return live


def select_pin(pins, session):
    """The one run this tick draws, or None when the registry cannot name a single one.

    The caller's own session decides and nothing else does, so a run's frame is drawn in the tab
    that launched it and in no other — however few pins the registry holds. A tick whose session
    matches no pin, and a tick with no session to match at all, draw nothing. Two pins recording
    one session name a single run no better than two unmatched pins do, so both draw nothing.
    """
    matching = [pin for pin in pins if session is not None and pin["session"] == session]
    return matching[0] if len(matching) == 1 else None


def alive(pid):
    """Whether the coordinator that wrote this pin is still running.

    The whole crash story: a run whose coordinator is gone has no frame to draw, and there is no
    watchdog, no heartbeat and no liveness file behind that.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # A process this user may not signal is still a process, so the pid is not free.
        return True
    except OSError:
        return False
    return True


def driver_record_path(run_dir):
    """The file the run directory names its driver in, absolute (ADR-0007)."""
    return pathlib.Path(run_dir).resolve() / DRIVER_RECORD_NAME


def record_driver(run_dir, pid):
    """Name the process driving this run; returns the file it was named in.

    Written by rename, like the pin, because a launcher reading it half-written would read no pid
    for a driver that is very much alive and start a second one.
    """
    path = driver_record_path(run_dir)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(f"{int(pid)}\n", encoding="utf-8")
        os.replace(temporary, path)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise MonitorError(f"the run's driver could not be recorded: {error}")
    return path


def release_driver(run_dir):
    """Take that record away, which is what makes an exit a deliberate one; returns nothing.

    Every way a driver ends on purpose passes through here — a wake handing judgment over, a
    driver error, the run finishing, an operator's own interrupt in the driver's window — and no
    kill does. A run with no record to take away has already been released.
    """
    with contextlib.suppress(OSError):
        driver_record_path(run_dir).unlink(missing_ok=True)


def recorded_driver(run_dir):
    """The pid the run directory names as its driver, or None where it names none it can read.

    A real pid or nothing: everything else this record could hold — a half-written file, a pid
    with a sign, a zero — is a process id nobody can ask about, and `kill -0` on the last two asks
    about somebody else's process group entirely.
    """
    try:
        text = driver_record_path(run_dir).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    return int(text) if text.isdigit() and int(text) > 0 else None


def live_driver(run_dir):
    """The pid of the driver still driving this run, or None where none is."""
    pid = recorded_driver(run_dir)
    return pid if pid is not None and alive(pid) else None


def dead_driver_banner(run_dir):
    """What the summary line says about a driver that was killed, or None where none was.

    The run directory's parent is what the banner names, because that is the directory the
    operator typed `/crew` with: the run directory itself is the `.crew` inside it.
    """
    pid = recorded_driver(run_dir)
    if pid is None or alive(pid):
        return None
    return DRIVER_DEAD.format(run=pathlib.Path(run_dir).resolve().parent)


def pin_directory(args):
    """The pin registry this tick reads, and the one the run writes its pin into.

    Resolved at this boundary (ADR-0007), so a run that writes its pin at dispatch and removes it
    at the end of the run addresses one registry however the two calls spelled the path or
    whatever directory each ran from.
    """
    given = pathlib.Path(args.pin_dir) if args.pin_dir else transcript_root(PIN_REGISTRY)
    return given.expanduser().resolve()


def surface_preference_path():
    """The machine-level dashboard surface preference beside the pin registry."""
    return transcript_root(PIN_REGISTRY).parent / SURFACE_PREFERENCE_NAME


def write_surface_preference():
    """Record the surface the operator opted into by installing the pin."""
    path = surface_preference_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(SURFACE_PIN + "\n", encoding="utf-8")
    except OSError as error:
        raise MonitorError(f"the machine surface preference could not be written: {error}")


def surface_preference_is_pinned():
    """Whether the machine preference exists and carries the installed surface value."""
    try:
        return surface_preference_path().read_text(encoding="utf-8").strip() == SURFACE_PIN
    except (OSError, UnicodeError):
        return False


def remove_surface_preference():
    """Remove the machine preference; an absent preference is already the desired state."""
    try:
        surface_preference_path().unlink(missing_ok=True)
    except OSError as error:
        raise MonitorError(f"the machine surface preference could not be removed: {error}")


def pin_colour(args):
    """Whether the frame is painted: unlike the dashboard's, this stdout is always a pipe.

    Claude Code passes raw ANSI through to the statusline it draws, so colour is on by default
    here and only the operator's own two ways of refusing it turn it off.
    """
    return not args.no_color and not os.environ.get("NO_COLOR")


def pin_frame_data(args, run_dir, moment):
    """The frame and rows that run has to draw right now, or None when it has none.

    A run the log says is over is a run whose final frame lives in the report and the machine log
    rather than on the operator's screen.
    """
    records = read_log(run_dir / MACHINE_LOG_NAME)
    projection = machine_log.project(records)
    if projection.ended:
        return None
    plan = read_plan(run_dir)
    sources = live_sources(
        args.claude_bin, run_dir, args.timeout, records, table_bindings(plan)
    )
    rows = build_rows(plan, projection, moment, sources)
    frame = render(
        rows, run_dir.name, len(plan.waves), moment,
        colour=pin_colour(args), max_lines=pin_line_budget(args),
        awaiting_ruling=projection.halted, dead_driver=dead_driver_banner(run_dir),
    )
    return frame, rows


def pin_line_budget(args):
    """The number of frame rows available to the pin, with an explicit override for tests/users."""
    if args.max_lines is not None:
        return max(args.max_lines, 0)
    return max(terminal_height() - STATUSLINE_RESERVE_LINES, 0)


def run_pin(args):
    """Draw the live run's one frame for a statusline tick, or draw nothing; returns 0.

    Every failure here is silence. A statusline that spews diagnostics across the operator's
    prompt is worse than one that goes quiet, so this is the one command that neither writes to
    stderr nor returns a `MONITOR ERROR` line — the frame is built whole before a byte of it is
    printed, and anything that goes wrong on the way leaves the statusline as it was.

    Claude Code writes its own JSON to this command's stdin. Nothing here reads it, and no source
    this spawns inherits it, so a tick can neither block on that stream nor eat what is on it.

    A tick is also the registry's only housekeeper: a pin whose coordinator has gone is swept
    before any of them is matched, because nothing but a normal finish ever unpins a run and an
    abandoned one would otherwise be read by every pane for as long as the machine stays up.

    `--from-wrapper` adds ADR-0011's one exception, and only for the caller that cannot judge it
    itself: the wrapper reads the registry with `sed`, so a file that is named like a pin and is
    not one is a judgment only this JSON parser can make. Where nothing was drawn and the registry
    holds such a file, the notice is printed here instead. Render failures stay silent (ADR-0008).
    """
    try:
        directory = pin_directory(args)
        pins, unreadable = read_registry(directory)
        pin = select_pin(sweep_registry(pins), caller_session(args.tmux_bin, args.timeout))
        data = None
        if pin is not None:
            data = pin_frame_data(args, pin["run_dir"], args.now or now())
        if data is None:
            if unreadable and args.from_wrapper:
                print(PIN_NOTICE.format(registry=directory), flush=True)
            return 0
        frame, rows = data
        print(frame, flush=True)
        if not args.no_toast:
            emit_toasts(
                rows,
                toast_state_path(args, pin["run_dir"]),
                args.tmux_bin,
                timeout=args.timeout,
            )
    except Exception:  # noqa: BLE001 — the silence contract: a tick never reports its own failure
        return 0
    return 0


def claude_settings_path():
    """The settings file Claude Code reads on this machine, as this machine has it configured."""
    variable, home, name = CLAUDE_SETTINGS
    configured = os.environ.get(variable)
    return pathlib.Path(configured or pathlib.Path.home() / home) / name


def pin_settings_path(args):
    """The settings file this install edits: the operator's real one unless told otherwise.

    Absolute and resolved (ADR-0007), because the path this writes into `statusLine.command` is
    run from whatever directory the session happens to be in.
    """
    settings = pathlib.Path(args.settings) if args.settings else claude_settings_path()
    return settings.expanduser().resolve()


def pin_wrapper_path(args, settings_file):
    """The wrapper script this install writes, absolute: beside the settings it is wired into."""
    if args.statusline:
        return pathlib.Path(args.statusline).expanduser().resolve()
    return settings_file.parent / PIN_WRAPPER_NAME


def pin_registry_expression():
    """The pin registry, spelled for the shell: the same three parts `transcript_root` resolves."""
    variable, home, subdirectory = PIN_REGISTRY
    return f'"${{{variable}:-$HOME/{home}}}/{subdirectory}"'


def pin_command():
    """The lines the wrapper runs to draw one frame: whatever the live run's pin names.

    Nothing here names a release. The pin carries the renderer and interpreter that draw it,
    recorded by the release that wrote it, so an upgrade that moves both leaves this stub correct
    and no re-install is ever needed (ADR-0011).

    Silence is still the rule — no pin, and this prints nothing — with the one exception ADR-0011
    carves out: pins that are there but name nothing this machine has are a wiring fault the
    operator can act on, so one line says so. Every path exits 0, because Claude Code blanks the
    operator's whole statusline, their own lines included, when this command does not.

    The two paths are read out with `sed` rather than parsed, because a shell has no JSON parser
    and the interpreter that would is the very thing being looked up. That bootstrap is only ever
    trusted forwards: what it finds is checked before it is run, the renderer it reaches is told
    it was reached this way and judges the registry properly itself, and a path spelled in a way
    `sed` cannot read back — an embedded quote or backslash — falls through to the notice rather
    than to a blank statusline.
    """
    return [
        f"registry={pin_registry_expression()}",
        "pinned=",
        f'for pin in "$registry"/{PIN_GLOB}; do',
        '    [ -f "$pin" ] || continue',
        "    pinned=$pin",
        f'    interpreter=$(sed -n \'s/.*"{PIN_INTERPRETER}"[[:space:]]*:[[:space:]]*'
        "\"\\([^\"]*\\)\".*/\\1/p' \"$pin\" 2>/dev/null)",
        f'    renderer=$(sed -n \'s/.*"{PIN_RENDERER}"[[:space:]]*:[[:space:]]*'
        "\"\\([^\"]*\\)\".*/\\1/p' \"$pin\" 2>/dev/null)",
        '    if [ -x "$interpreter" ] && [ -f "$renderer" ]; then',
        # Run rather than exec: a renderer that dies on a signal must not take the exit code, and
        # the frame is the last thing this wrapper has to print anyway.
        '        "$interpreter" "$renderer" pin --from-wrapper </dev/null',
        "        exit 0",
        "    fi",
        "done",
        'if [ -n "$pinned" ]; then',
        "    printf '%s\\n' \"" + PIN_NOTICE.format(registry="$registry") + '"',
        "fi",
        "exit 0",
    ]


def wrapper_text(previous, added_interval):
    """The wrapper's whole text: the record it is undone from, then the commands it runs."""
    record = json.dumps({
        "version": INSTALL_RECORD_VERSION,
        "previous": previous,
        "refresh_interval_added": added_interval,
    })
    lines = [
        "#!/bin/sh",
        "# Written by monitor.py pin-install. Change the settings rather than this file:",
        "# `pin-install --uninstall` reads the record below to put the statusline back exactly.",
        INSTALL_MARKER + record,
        "",
    ]
    if previous:
        lines += [
            # Claude Code writes its JSON to stdin and both commands are entitled to it, so it is
            # read once and handed on. The substitution drops the trailing newlines and exactly one
            # is put back: the frame starts on its own line, and a readout that printed nothing
            # costs no row, because Claude Code drops a blank line rather than drawing it.
            "payload=$(cat)",
            f'previous=$(printf %s "$payload" | {previous})',
            'if [ -n "$previous" ]; then printf \'%s\\n\' "$previous"; fi',
            "",
        ]
    lines += [*pin_command(), ""]
    return "\n".join(lines)


def file_text(path):
    """The file's text, or None when it is not there or cannot be read."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def install_record(wrapper):
    """What this installer recorded in that wrapper, or None if it did not write it."""
    text = file_text(wrapper)
    for line in (text or "").splitlines():
        if line.startswith(INSTALL_MARKER):
            try:
                record = json.loads(line[len(INSTALL_MARKER):])
            except json.JSONDecodeError:
                return None
            return record if isinstance(record, dict) else None
    return None


def read_settings(path):
    """The operator's settings as a dict; a file that cannot be parsed is refused, never guessed."""
    text = file_text(path)
    if text is None or not text.strip():
        return {}
    try:
        settings = json.loads(text)
    except json.JSONDecodeError as error:
        raise MonitorError(f"{path} is not valid JSON ({error}); nothing was written")
    if not isinstance(settings, dict):
        raise MonitorError(f"{path} is not a JSON object; nothing was written")
    return settings


def status_line_of(settings):
    """The settings' `statusLine`, as a dict that can be edited without touching the original."""
    existing = settings.get(STATUS_LINE_KEY)
    return dict(existing) if isinstance(existing, dict) else {}


def wrapped_command(status_line, wrapper):
    """What the statusline ran before this install, and whether the install adds the interval.

    Read from the wrapper's own record once the pin is in place, so a second run wraps what the
    first one wrapped rather than wrapping itself, and from the settings before that.
    """
    record = install_record(wrapper)
    if record is not None and status_line.get(COMMAND_KEY) == str(wrapper):
        return record.get("previous"), bool(record.get("refresh_interval_added"))
    command = status_line.get(COMMAND_KEY)
    previous = command if isinstance(command, str) and command != str(wrapper) else None
    return previous, REFRESH_INTERVAL_KEY not in status_line


def plan_install(settings, settings_file, wrapper):
    """The settings and wrapper an `--apply` would leave, and the lines describing the edits.

    No lines means the install is already in place, which is what makes a second `--apply` a
    no-op rather than a second layer of wrapping.
    """
    status_line = status_line_of(settings)
    previous, adds_interval = wrapped_command(status_line, wrapper)
    wanted = dict(status_line)
    wanted[TYPE_KEY] = COMMAND_TYPE
    wanted[COMMAND_KEY] = str(wrapper)
    if adds_interval:
        wanted[REFRESH_INTERVAL_KEY] = DEFAULT_PIN_REFRESH_SECONDS
    text = wrapper_text(previous, adds_interval)

    lines = []
    if file_text(wrapper) != text:
        lines.append(f"{'rewrite' if wrapper.exists() else 'create'} {wrapper}")
        lines.append(
            f"  runs {previous} first, then draws the pin's frame beneath it" if previous
            else "  runs the pin, and nothing else"
        )
    if settings.get(STATUS_LINE_KEY) != wanted:
        lines.append(f"edit {settings_file}")
        lines.append(
            f"  {STATUS_LINE_KEY}.{COMMAND_KEY}: {previous or '(absent)'} -> {wrapper}"
        )
        if adds_interval:
            lines.append(
                f"  {STATUS_LINE_KEY}.{REFRESH_INTERVAL_KEY}: (absent) -> "
                f"{DEFAULT_PIN_REFRESH_SECONDS}"
            )
        else:
            lines.append(
                f"  {STATUS_LINE_KEY}.{REFRESH_INTERVAL_KEY}: "
                f"{status_line.get(REFRESH_INTERVAL_KEY)}, left as it is"
            )
    after = dict(settings)
    after[STATUS_LINE_KEY] = wanted
    return after, text, lines


def plan_uninstall(settings, settings_file, wrapper):
    """The settings an `--apply --uninstall` would put back, and the lines describing the undo.

    The wrapper's record is the whole of what is restored: the command the operator had, and
    whether the refresh interval beside it is this install's to remove or the operator's to keep.
    """
    record = install_record(wrapper)
    if record is None:
        return None, []
    previous = record.get("previous")
    after = dict(settings)
    edits = []
    if previous:
        restored = status_line_of(settings)
        restored[COMMAND_KEY] = previous
        if record.get("refresh_interval_added"):
            restored.pop(REFRESH_INTERVAL_KEY, None)
            edits.append(
                f"  {STATUS_LINE_KEY}.{REFRESH_INTERVAL_KEY}: removed — this install added it"
            )
        after[STATUS_LINE_KEY] = restored
        edits.insert(0, f"  {STATUS_LINE_KEY}.{COMMAND_KEY}: {wrapper} -> {previous}")
    else:
        after.pop(STATUS_LINE_KEY, None)
        edits.append(f"  {STATUS_LINE_KEY}: removed — this install is what created it")
    lines = []
    if after != settings:
        lines = [f"edit {settings_file}", *edits]
    if wrapper.exists():
        lines.append(f"remove {wrapper}")
    return after, lines


def uninstall_drift(settings, wrapper):
    """What the statusline runs now, when that is no longer this install's wrapper; else None.

    An operator who has rewired the statusline since the install has said what they want it to
    run, and putting the command this wrapper replaced back over the top of that would undo a
    decision this command never saw.
    """
    command = status_line_of(settings).get(COMMAND_KEY)
    return None if command == str(wrapper) else (command or "(nothing)")


def back_up(path):
    """Copy `path` aside before it is written or removed; a file that is not there needs none."""
    if path.exists():
        shutil.copy2(str(path), str(path) + BACKUP_SUFFIX)


def write_settings(path, settings):
    """Write the settings back, having first put a copy of what was there beside it."""
    back_up(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


def write_wrapper(wrapper, text):
    """Write the wrapper and make it runnable, having first backed up whatever it replaces."""
    back_up(wrapper)
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    wrapper.write_text(text, encoding="utf-8")
    wrapper.chmod(0o755)


def run_pin_install(args):
    """Print the edits that wire the pin into the operator's statusline, or make them; returns 0.

    Nothing is written without `--apply`, so the operator authorises the exact edit rather than the
    general permission to edit, and every file is copied aside before it is written.
    """
    settings_file = pin_settings_path(args)
    wrapper = pin_wrapper_path(args, settings_file)
    settings = read_settings(settings_file)
    preference = surface_preference_path()
    if args.uninstall:
        drift = uninstall_drift(settings, wrapper) if install_record(wrapper) else None
        if drift is not None:
            raise MonitorError(
                f"{settings_file} now runs {drift} rather than {wrapper}; "
                "the statusline has moved on since the install and nothing was written"
            )
        after, lines = plan_uninstall(settings, settings_file, wrapper)
        if preference.exists():
            lines.append(f"remove {preference}")
        if not lines:
            print("the pin is not installed here; nothing to undo")
            return 0
    else:
        after, text, lines = plan_install(settings, settings_file, wrapper)
        wiring_needs_change = bool(lines)
        preference_needs_write = not surface_preference_is_pinned()
        if preference_needs_write:
            lines.append(f"write {preference} = {SURFACE_PIN}")
        if not lines:
            print("the pin is already installed here; nothing to change")
            return 0
    for line in lines:
        print(line)
    if not args.apply:
        print("dry run — nothing written. Re-run with --apply to make these changes.")
        return 0
    if args.uninstall:
        if after is not None:
            write_settings(settings_file, after)
        if wrapper.exists():
            back_up(wrapper)
            wrapper.unlink()
        remove_surface_preference()
    else:
        # The wrapper first: the settings must never name a script that is not there yet.
        if wiring_needs_change:
            write_wrapper(wrapper, text)
            write_settings(settings_file, after)
        if preference_needs_write:
            write_surface_preference()
    return 0


def transcript_root(spec, account=None):
    """A directory under an executor's configured home: a transcript root, or the pin registry.

    The same three parts every time — the variable that moves the home, the home's own name under
    `~`, and the fixed subdirectory inside it.

    `account` is an explicitly supplied profile directory, which is what a ticket routed to
    another account carries and what a cost read uses for its ticket. The wave table holds the
    profile directory itself, already resolved, so a read for that ticket goes to that home rather
    than to the one this process was started under.
    """
    variable, home, subdirectory = spec
    account = os.environ.get(variable) if account is None else account
    return pathlib.Path(account or pathlib.Path.home() / home) / subdirectory


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


def review_sessions(records):
    """Every session id the run's reviews already recorded a cost for.

    A review runs in a session of its own and writes its own lane-tagged `session-cost` line, so
    its transcript is spoken for. When the review lane and the child share a vendor that
    transcript also sits in the child's worktree, where the glob below would otherwise bill the
    ticket for it a second time.
    """
    found = set()
    for record in records:
        if record.get("event") != "session-cost" or not record.get("lane"):
            continue
        for session in str(record.get("session") or "").split(SESSION_SEPARATOR):
            if session:
                found.add(session)
    return found


def child_usage(executor, worktree, reviewed=(), config_home=None):
    """What one child spent: its sessions, their counters, or the diagnosis in place of both.

    Every failure on this path is diagnosed rather than raised, and a child with one unbillable
    transcript is diagnosed whole rather than billed for the rest: a total that quietly leaves
    out what could not be read is worse than no total at all. The pass runs after the run is
    over, so what it cannot read it will never be able to read.

    `reviewed` names the sessions the run's reviews are already billed under, which are skipped
    here rather than added to the child that was reviewed.
    """
    spec, usage_of = TRANSCRIPT_READERS[executor]
    root = transcript_root(spec, config_home)
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
        if session in reviewed:
            continue
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


def cost_rows(records, claude_homes=None):
    """One row per launched ticket, in ticket order: what it was routed to, and what it spent.

    A ticket launched twice into the same worktree — a replacement child — is one row, and its
    figures are every session that ran there, because both children spent the ticket's tokens.
    """
    projection = machine_log.project(records)
    launches = {
        ticket: facts.launch for ticket, facts in projection.tickets.items()
        if facts.launch is not None
    }
    reviewed = review_sessions(records)
    claude_homes = claude_homes or {}

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
            config_home = claude_homes.get(ticket) if executor == CLAUDE else None
            usage = child_usage(executor, worktree, reviewed, config_home)
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

    The log's parent holds the Run plan that maps Claude tickets to profile directories. The plan
    is loaded before any cost row is written, so an invalid plan rejects the whole report.

    The coordinator's row is printed and not logged: the log's `session-cost` is a launched
    ticket's line, and the session that drives the run is not one.
    """
    run_dir = pathlib.Path(args.log).parent
    homes = account_homes(run_dir)
    rows = cost_rows(read_log(args.log), homes)
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

    window = run_command("window", "draw the run on the surfaces its repo chose")
    window.set_defaults(handler=run_window)
    window.add_argument("--session", required=True, help="the tmux target the window is created in")
    window.add_argument(
        "--refresh", type=float,
        help=f"the window's redraw interval in seconds (default: {DEFAULT_REFRESH_SECONDS})",
    )
    window.add_argument(
        "--config",
        help=f"the project's config, whose [{DASHBOARD_SECTION}] {SURFACE_KEY} chooses between "
             f"{', '.join(SURFACES)} (default: machine preference or {DEFAULT_SURFACE})",
    )
    window.add_argument(
        "--coordinator-pid", type=int,
        help="the pid of the session driving the run, which a pinned surface is checked against",
    )
    window.add_argument("--pin-dir", help="the pin registry to write this run's pin into")

    unpin = commands.add_parser("unpin", help="take the run's pin out of the registry")
    unpin.set_defaults(handler=run_unpin)
    unpin.add_argument("--run-dir", required=True, help="the run's directory")
    unpin.add_argument("--pin-dir", help="the pin registry this run's pin was written into")

    pin = commands.add_parser(
        "pin", help="draw the live run's one frame into the coordinator's Claude Code statusline"
    )
    pin.set_defaults(handler=run_pin)
    pin.add_argument("--pin-dir", help="the pin registry the live run is discovered through")
    pin.add_argument("--toast-state", help="where this run remembers what it has toasted")
    pin.add_argument(
        "--no-toast", action="store_true", help="draw the frame without displaying toasts"
    )
    pin.add_argument(
        "--from-wrapper", action="store_true",
        help="the statusline wrapper is the caller: say so in one line when the registry holds a"
             " file that cannot be read as a pin (ADR-0011), rather than drawing nothing",
    )
    pin.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS,
        help="how long a live source is given to answer before its row is drawn"
             f" {UNKNOWN} (default: {DEFAULT_TIMEOUT_SECONDS})",
    )
    pin.add_argument(
        "--no-color", action="store_true", help="draw plain text rather than the state's colour"
    )
    pin.add_argument(
        "--now", type=timestamp_argument,
        help="the moment elapsed times are measured to, stamped as the log stamps (default: now)",
    )
    pin.add_argument(
        "--max-lines", type=int,
        help="the maximum number of frame rows (default: LINES minus the statusline reserve)",
    )

    cost = commands.add_parser("cost", help="record what each child spent and roll the run up")
    cost.set_defaults(handler=run_cost)
    cost.add_argument("--log", required=True, help="the run's machine log")
    cost.add_argument(
        "--coordinator-session",
        help="the id of the session driving the run, whose transcript becomes the coordinator row",
    )

    pin_install = commands.add_parser(
        "pin-install", help="wire the pin into the operator's Claude Code statusline"
    )
    pin_install.set_defaults(handler=run_pin_install)
    pin_install.add_argument(
        "--apply", action="store_true", help="make the edits instead of printing them"
    )
    pin_install.add_argument(
        "--uninstall", action="store_true", help="put the statusline back the way it was"
    )
    pin_install.add_argument(
        "--settings", help="the Claude Code settings file to edit (default: the real one)"
    )
    pin_install.add_argument(
        "--statusline", help="the wrapper script to write (default: beside those settings)"
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
