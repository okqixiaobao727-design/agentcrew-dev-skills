#!/usr/bin/env python3
"""Start a crew run from a run directory alone: resolve the coordinator, then launch the driver.

The Driver needs one immutable context for the session driving it: the five recorded identity
facts below, plus the invoking pane used for no-Waiter recovery and the tmux display session used
for the dashboard. A Coordinator session cannot see the five identity facts from inside itself,
and the turns it spends hunting for them are the whole of `/crew`'s start-up cost. This script
reads them off the harness's own on-disk records instead:

- **pid** — the invoking shell's parent, found by walking up the process ancestry to the first
  process the harness has a session registry entry for. The shell in between is why the walk
  exists: the coordinator is never the launcher's own parent.
- **name** — that registry entry's `name`, which is what the harness itself calls the session.
- **address** — that registry entry's `messagingSocketPath` under the `uds:` scheme, which is the
  one address a child can reach this coordinator at whatever account the child runs on
  (ADR-0023). Composing it out of the pid instead would name the default socket directory, and
  the harness does not always bind there.
- **session ID** — that registry entry's `sessionId`, which scopes hooks to this coordinator.
- **permission mode** — the newest registry-session transcript entry that records one. The
  transcript is the only on-disk source that follows a mid-session mode switch, and the mode is
  the one fact here that must be current rather than merely correct.

These are harness-internal formats, so every resolution failure aborts with the flag that supplies
the value by hand. Nothing is defaulted and nothing is guessed: a wrong name or pid strands the
run's rulings, and a wrong mode launches every child of the run in the wrong permission regime.

Attendance owns the Run-control distinction here. With no live Driver it composes `start`; with a
live Driver the same Coordinator address attaches another Waiter, while a different address waits
for that Driver to complete an in-place Coordinator handover.

What this script does *not* do any more is become that driver. A driver held as a background task
of the coordinator's session is a driver the harness may end at any moment — it did, silently,
45 minutes into a live run, and the dashboard went on drawing `waiting` for forty minutes after
(#103). So the driver is put in its own tmux window, through the same windowing machinery every
child of the run is launched through, where no session-level task management can reach it, and
this process stays behind as a waiter: it blocks until the run's wake snapshot appears in the run
directory, prints it, and ends. A killed waiter loses no run fact — the driver is untouched — but
the harness reaps a main session's background shells under memory pressure, and until another
waiter is attached no wake reaches the coordinator (#127). So the waiter records its liveness as
the driver does, and a driver that wakes with no live waiter re-types `/crew <feature-dir>` into
the coordinator's pane itself; the dashboard says so until one attaches. Which pane that is, this
process is the only one that can say — it is the only part of the run that runs inside it — so it
reads `$TMUX_PANE` out of its own environment and hands it to the driver.

That keeps the command idempotent: a Run whose Driver is already alive is never driven twice, even
when the invoking Coordinator must first take ownership from a different address.

    python3 launch.py <run-dir> [--coordinator-pid N] [--coordinator-name NAME]
                                [--coordinator-session ID] [--coordinator-address uds:PATH]
                                [--permission-mode MODE] [--driver PATH]
"""

import argparse
import contextlib
import json
import os
import pathlib
import shlex
import subprocess
import sys
import time


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
# The driver this launches, beside its own skill assets. Overridable in the shape the driver's own
# `--codex-bridge` already sets — a default pointing at the neighbour, and a flag naming another —
# so a test drives this from its command line against a recorder rather than assembling a whole
# run for the real driver to refuse.
DRIVER = SCRIPT_DIR.parent / "driver" / "driver.py"
START_COMMAND = "start"
# The renderer owns the run's liveness record — the pid its driver writes on the way into its loop
# and takes away on the way out — so this asks it rather than keeping a second reader of the same
# file. It is the one thing that decides whether `/crew` starts a driver or attaches to one.
MONITOR = SCRIPT_DIR.parent / "monitor" / "monitor.py"
sys.path.insert(0, str(MONITOR.parent))
import monitor  # noqa: E402
import coordinator_control  # noqa: E402

# The run's own directory inside the feature, and the two public files attendance consumes: the
# wake snapshot the Driver leaves for its Waiter, and the Driver's output now that no task output
# file collects it. Coordinator control owns its private run-local state; the pid record the whole
# liveness judgment rests on belongs to the Driver, so the renderer that owns it is asked rather
# than read here.
RUN_DIR_NAME = ".crew"
WAKE_NAME = "wake.json"
DRIVER_LOG_NAME = "driver.log"
# The window the driver runs in, named as the run's other windows are so an operator reading the
# session's window list can tell what it is. Killing it, or Ctrl-C in it, stops the run's driver
# and nothing else.
DRIVER_WINDOW_NAME = "crew-driver"
TMUX = "tmux"
TMUX_SESSION = ("display-message", "-p", "#{session_id}")
# tmux's own name for the pane a process is running in, exported into every pane it opens. It is
# how this script names the coordinator's pane for the driver, because this script runs in it.
TMUX_PANE_VARIABLE = "TMUX_PANE"

# How long a spawned driver is given to name itself in the run directory before the launch is
# called failed. It is the first thing its loop does, so this is generous rather than tuned; the
# environment moves it in the shape the loop's own poll interval is already moved by one.
HANDSHAKE_SECONDS = float(os.environ.get("CREW_LAUNCH_HANDSHAKE_SECONDS") or 30.0)
# How long a fresh coordinator session is given to record its permission mode. A slash-command
# turn can start this launcher before its first mode-bearing transcript entry is written, so the
# transcript remains authoritative while the launcher gives that entry a bounded time to arrive.
PERMISSION_MODE_SECONDS = float(
    os.environ.get("CREW_LAUNCH_PERMISSION_MODE_SECONDS") or 10.0
)
# How often the launcher rereads state it is waiting on. Neither the first permission-mode record
# nor a run's wake is a hot path, so polling keeps both waits simple and bounded.
POLL_SECONDS = 0.5
# The harness's two records, each as the three parts `monitor.py` spells them in: the variable
# that moves the home, the home's own name under `~`, and the fixed subdirectory inside it.
SESSION_REGISTRY = ("CLAUDE_CONFIG_DIR", ".claude", "sessions")
TRANSCRIPTS = ("CLAUDE_CONFIG_DIR", ".claude", "projects")
REGISTRY_SUFFIX = ".json"
TRANSCRIPT_SUFFIX = ".jsonl"
# The four fields read out of those records, in the harness's own spelling.
REGISTRY_NAME = "name"
REGISTRY_SESSION = "sessionId"
REGISTRY_SOCKET = "messagingSocketPath"
PERMISSION_MODE = "permissionMode"
# The scheme a socket path is an address under. It is prefixed exactly once, here, so that every
# consumer downstream uses the whole address verbatim rather than assembling one of its own.
ADDRESS_SCHEME = "uds:"

# How far up the ancestry the coordinator is looked for. A shell, and a shell's own wrapper, are
# what stand between this process and the session; anything further up is init, and a walk that
# reached it would be reading somebody else's pid.
ANCESTRY_LIMIT = 10
PROCESS_PARENT = ("ps", "-o", "ppid=", "-p")

LAUNCH_ERROR_EXIT = 2


class LaunchError(Exception):
    """A value the driver requires could not be resolved. Reported on stderr; never defaulted."""


def harness_directory(spec):
    """A directory under the harness's configured home: the session registry, or the transcripts.

    Absolute at the boundary, whatever spelling the environment used (ADR-0007): the failures
    here name the directory they searched, and a relative one names a different directory to
    every reader of the message.
    """
    variable, home, subdirectory = spec
    configured = os.environ.get(variable)
    return (pathlib.Path(configured or pathlib.Path.home() / home) / subdirectory).resolve()


def read_json(path):
    """That file's JSON object, or None where it is absent or is not one."""
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def parent_process(pid):
    """The pid that started `pid`, or None where no process table answers for it."""
    try:
        result = subprocess.run(
            [*PROCESS_PARENT, str(pid)], capture_output=True, text=True, check=False
        )
    except OSError:
        return None
    parent = result.stdout.strip()
    return int(parent) if result.returncode == 0 and parent.isdigit() else None


def ancestry():
    """This process's ancestors, nearest first: the invoking shell, then what launched it."""
    line = []
    pid = os.getppid()
    while pid and pid > 1 and len(line) < ANCESTRY_LIMIT:
        line.append(pid)
        pid = parent_process(pid)
    return line


def registered_session(pid):
    """The session registry entry the harness holds for `pid`, or None where it holds none."""
    return read_json(harness_directory(SESSION_REGISTRY) / f"{pid}{REGISTRY_SUFFIX}")


def pass_explicitly(flags):
    """The instruction a failed resolution ends on: exactly the flags that would supply it."""
    named = ", ".join(flags[:-1])
    both = f"{named} and {flags[-1]}" if named else flags[-1]
    return f"pass {both} explicitly"


def coordinator(given_pid, unresolved):
    """The coordinator's pid and its registry entry; raises where the harness records neither.

    A pid given by hand is taken as given and its entry looked up directly, so that a pid passed
    explicitly still resolves the name and mode of a session this process never descended from.
    """
    candidates = [given_pid] if given_pid is not None else ancestry()
    for pid in candidates:
        entry = registered_session(pid)
        if entry is not None:
            return pid, entry
    searched = ", ".join(str(pid) for pid in candidates) or "no process at all"
    raise LaunchError(
        f"no session of this harness is recorded for {searched} in"
        f" {harness_directory(SESSION_REGISTRY)}: {pass_explicitly(unresolved)}"
    )


def registry_string(entry, pid, key, flag):
    """Return one required non-empty string from a session registry entry, or raise."""
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise LaunchError(
            f"the session registry entry for {pid} in {harness_directory(SESSION_REGISTRY)}"
            f" records no {key}: pass {flag} explicitly"
        )
    return value


def transcripts(session):
    """Every transcript file the harness holds for that session, newest written first."""
    if not isinstance(session, str) or not session.strip():
        return []
    root = harness_directory(TRANSCRIPTS)
    found = [path for path in root.rglob(f"{session}{TRANSCRIPT_SUFFIX}") if path.is_file()]
    return sorted(found, key=lambda path: path.stat().st_mtime, reverse=True)


def recorded_mode(path):
    """The newest entry of that transcript that records a permission mode, or None where none does.

    Read backwards, because the answer is the last line to carry one and a transcript grows for
    the whole of a session's life.
    """
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        mode = entry.get(PERMISSION_MODE) if isinstance(entry, dict) else None
        if isinstance(mode, str) and mode.strip():
            return mode
    return None


def permission_mode(entry, pid):
    """The current mode from the transcript, after a bounded wait for its first such entry."""
    session = entry.get(REGISTRY_SESSION)
    deadline = time.monotonic() + PERMISSION_MODE_SECONDS
    while True:
        for path in transcripts(session):
            mode = recorded_mode(path)
            if mode is not None:
                return mode
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(POLL_SECONDS, remaining))
    raise LaunchError(
        f"no transcript entry under {harness_directory(TRANSCRIPTS)} records the permission mode"
        f" of session {session or 'unnamed'} (pid {pid}):"
        f" {pass_explicitly(['--permission-mode'])}, because a guessed one launches every child in"
        f" the wrong permission regime"
    )


def registry_address(entry, pid):
    """The coordinator's whole inbox address, out of the socket path the harness bound at.

    Composed rather than read whole because the registry records a path, and prefixing it is the
    one assembly this value ever undergoes. The path itself is taken exactly as spelled — no
    realpath, no normalisation (ADR-0023): the harness's own reply-address checks compare address
    literals, and on macOS the default socket directory reaches the same place under two
    spellings, so the receiver's own spelling is the only one certain to match.
    """
    return ADDRESS_SCHEME + registry_string(
        entry, pid, REGISTRY_SOCKET, "--coordinator-address"
    )


def resolve(args):
    """The five values the driver's start requires, each given by hand or read off the harness.

    Every one of the five is resolved or the launch fails: a value the harness cannot supply and
    the operator did not pass is not a value to hand the driver empty.
    """
    pid = args.coordinator_pid
    name = args.coordinator_name
    session = args.coordinator_session
    address = args.coordinator_address
    mode = args.permission_mode
    if pid is None or name is None or session is None or address is None or mode is None:
        given = {
            "--coordinator-pid": pid,
            "--coordinator-name": name,
            "--coordinator-session": session,
            "--coordinator-address": address,
            "--permission-mode": mode,
        }
        pid, entry = coordinator(pid, [flag for flag, value in given.items() if value is None])
        name = name if name is not None else registry_string(
            entry, pid, REGISTRY_NAME, "--coordinator-name"
        )
        session = session if session is not None else registry_string(
            entry, pid, REGISTRY_SESSION, "--coordinator-session"
        )
        address = address if address is not None else registry_address(entry, pid)
        mode = mode if mode is not None else permission_mode(entry, pid)
    return pid, name, session, address, mode


def coordinator_pane():
    """The tmux pane this process is running in, or None where it is running in none.

    That pane is the coordinator's own: this script is a background shell of the coordinator's
    session, so it inherits the environment of the pane the operator typed `/crew` in. Nothing
    else in the run can name it — asked of tmux instead, a pane id would be whichever pane is
    current when the question is put, which is the driver's own window as often as not. So it is
    read here, at the one boundary that has it, and handed down; a driver given none types nothing.
    """
    return os.environ.get(TMUX_PANE_VARIABLE) or None


def coordinator_context(args):
    """Resolve the invoking Coordinator once, before any Run-control decision."""
    pid, name, harness_session, address, mode = resolve(args)
    return coordinator_control.CoordinatorContext(
        name=name,
        pid=pid,
        harness_session=harness_session,
        address=address,
        pane=coordinator_pane(),
        permission_mode=mode,
        display_session=tmux_session(),
    )


def driver_command(args, context):
    """The driver command line this run starts on, `start` because start is what adopts."""
    command = [
        sys.executable, str(pathlib.Path(args.driver).resolve()), START_COMMAND,
        "--feature-dir", str(pathlib.Path(args.run_dir).resolve()),
        "--coordinator-name", context.name,
        "--coordinator-pid", str(context.pid),
        "--coordinator-session", context.harness_session,
        "--coordinator-address", context.address,
        "--permission-mode", context.permission_mode,
        "--tmux-session", context.display_session,
    ]
    return command + ["--coordinator-pane", context.pane] if context.pane else command


# --- the driver's own window ------------------------------------------------------------------


def tmux_session():
    """The invoking Coordinator's tmux display session: the one this command was typed in.

    A new Run starts its Driver and execution windows there. A live handover uses it only to re-pin
    the dashboard; the existing Driver and child windows stay in the Run's recorded session.
    """
    try:
        result = subprocess.run(
            [TMUX, *TMUX_SESSION], capture_output=True, text=True, check=False
        )
    except OSError as error:
        raise LaunchError(
            f"the driver needs a tmux session to run in and tmux is not there: {error}"
        )
    if result.returncode != 0 or not result.stdout.strip():
        raise LaunchError(
            "this run has no tmux session to put its driver in:"
            f" {(result.stderr or result.stdout).strip() or 'tmux named none'}"
        )
    return result.stdout.strip()


def window_command(command, log):
    """The shell line the driver's window runs: the driver, with its output kept.

    Nothing collects a detached driver's stdout any more, so it is teed rather than redirected —
    the pane shows the run as it happens, and the file outlives both the pane and the process, so
    a driver that dies still leaves a trail.
    """
    return f"{shlex.join(command)} 2>&1 | tee -a {shlex.quote(str(log))}"


def open_window(command, session, log):
    """Put the driver in its own tmux window, detached; returns the window's id.

    This is the whole of the detachment. A window's process belongs to the tmux server and to no
    session of the harness, so nothing that ends this process — an interrupt, a compaction, the
    session exiting, the harness reaping its own background tasks — can reach the run's driver.
    """
    result = subprocess.run(
        [
            TMUX, "new-window", "-d", "-P", "-F", "#{window_id}",
            "-n", DRIVER_WINDOW_NAME, "-t", session, "-c", os.getcwd(),
            window_command(command, log),
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise LaunchError(
            f"the driver's window could not be opened: {(result.stderr or result.stdout).strip()}"
        )
    return result.stdout.strip()


def await_driver(run_dir, deadline):
    """Wait for the spawned driver to name itself in the run directory; returns nothing.

    Held inside Coordinator attendance, so the next `/crew` typed at this Run sees the Driver this
    one started rather than an empty run directory it would start a second Driver for. A Driver
    that got as far as a wake — a preflight failure is over before its first poll — has answered
    too.
    """
    while time.monotonic() < deadline:
        if monitor.live_driver(run_dir) or wake_path(run_dir).exists():
            return
        time.sleep(POLL_SECONDS)
    raise LaunchError(
        f"the driver never started: nothing named itself in {run_dir} within"
        f" {HANDSHAKE_SECONDS:g} seconds — read {run_dir / DRIVER_LOG_NAME}"
    )


# --- the run directory this end of the run uses -------------------------------------------------


def run_directory(args):
    """The run's own directory inside the feature, made if the run has not made it yet.

    Made here rather than by the driver, because the driver's window writes its log into it before
    the driver has decided whether this run starts at all.
    """
    feature_dir = pathlib.Path(args.run_dir).resolve()
    if not feature_dir.is_dir():
        raise LaunchError(f"{feature_dir} is not a directory of tickets to run")
    run_dir = feature_dir / RUN_DIR_NAME
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise LaunchError(f"the run directory {run_dir} could not be made: {error}")
    return run_dir


def wake_path(run_dir):
    """The file the driver leaves its one wake snapshot in, which is what this waits on."""
    return run_dir / WAKE_NAME


def start_driver(args, run_dir, context):
    """Start the missing Driver selected by Coordinator attendance; returns nothing."""
    command = driver_command(args, context)
    with contextlib.suppress(OSError):
        wake_path(run_dir).unlink(missing_ok=True)
    open_window(command, context.display_session, run_dir / DRIVER_LOG_NAME)
    await_driver(run_dir, time.monotonic() + HANDSHAKE_SECONDS)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run_dir", help="the run directory whose tickets this run works through")
    parser.add_argument(
        "--coordinator-pid", type=int,
        help="the coordinator's pid, where the harness's session registry cannot be read",
    )
    parser.add_argument(
        "--coordinator-name",
        help="the coordinator's session name, where the registry entry cannot be read",
    )
    parser.add_argument(
        "--coordinator-session",
        help="the coordinator's session ID, where the registry entry cannot be read",
    )
    parser.add_argument(
        "--coordinator-address",
        help="the coordinator's whole `uds:` inbox address — the one thing a child sends to —"
             " where the registry entry cannot be read",
    )
    parser.add_argument(
        "--permission-mode",
        help="the mode children launch under, where the session's transcript cannot be read",
    )
    parser.add_argument("--driver", default=str(DRIVER),
                        help="the driver this starts (default: %(default)s)")
    return parser


def carry_the_wake(args, run_dir):
    """Resolve one Coordinator and attend the Run through Coordinator control."""
    try:
        context = coordinator_context(args)
        control = coordinator_control.CoordinatorControl(
            run_dir, liveness=monitor, poll_seconds=POLL_SECONDS
        )
        return control.attend(context, lambda current: start_driver(args, run_dir, current))
    except (LaunchError, coordinator_control.CoordinatorControlError) as error:
        print(f"launch: {error}", file=sys.stderr)
        return LAUNCH_ERROR_EXIT


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        run_dir = run_directory(args)
    except LaunchError as error:
        print(f"launch: {error}", file=sys.stderr)
        return LAUNCH_ERROR_EXIT
    return carry_the_wake(args, run_dir)


if __name__ == "__main__":
    sys.exit(main())
