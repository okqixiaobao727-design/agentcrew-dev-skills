#!/usr/bin/env python3
"""Start a crew run from a run directory alone: resolve the coordinator, then launch the driver.

The driver's `start` needs three facts about the session driving it — the pid a child
authenticates a ruling against, the name a child answers to, and the permission mode every child
launches under. A coordinator session cannot see any of them from inside itself, and the turns it
spends hunting for them are the whole of `/crew`'s start-up cost. This script reads them off the
harness's own on-disk records instead:

- **pid** — the invoking shell's parent, found by walking up the process ancestry to the first
  process the harness has a session registry entry for. The shell in between is why the walk
  exists: the coordinator is never the launcher's own parent.
- **name** — that registry entry's `name`, which is what the harness itself calls the session.
- **permission mode** — the newest entry of that session's transcript that records one. The
  transcript is the only on-disk source that follows a mid-session mode switch, and the mode is
  the one fact here that must be current rather than merely correct.

These are harness-internal formats, so every resolution failure aborts with the flag that supplies
the value by hand. Nothing is defaulted and nothing is guessed: a wrong name or pid strands the
run's rulings, and a wrong mode launches every child of the run in the wrong permission regime.

Starting and adopting are the driver's own distinction, not this script's: it composes `start`,
which adopts a run the directory already holds rather than beginning a second one.

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
the coordinator's pane itself; the dashboard says so until one attaches.

That makes the command idempotent in one more way than before. A run whose driver is already alive
— named in the run directory, and answering to a signal — is attached to rather than started
again, so `/crew` stays safe to type at any moment and no run is ever driven twice.

    python3 launch.py <run-dir> [--coordinator-pid N] [--coordinator-name NAME]
                                [--permission-mode MODE] [--driver PATH]
"""

import argparse
import contextlib
import fcntl
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

# The run's own directory inside the feature, and the three files this end of the run uses: the
# wake snapshot the driver leaves for this waiter, the driver's own output now that no task output
# file collects it, and the lock that makes "is a driver alive, and start one if not" one decision.
# The fourth — the pid record the whole judgment rests on — belongs to the driver, so the renderer
# that owns it is asked rather than read here.
RUN_DIR_NAME = ".crew"
WAKE_NAME = "wake.json"
DRIVER_LOG_NAME = "driver.log"
DRIVER_LOCK_NAME = "driver.lock"
# The window the driver runs in, named as the run's other windows are so an operator reading the
# session's window list can tell what it is. Killing it, or Ctrl-C in it, stops the run's driver
# and nothing else.
DRIVER_WINDOW_NAME = "crew-driver"
TMUX = "tmux"
TMUX_SESSION = ("display-message", "-p", "#{session_id}")

# How long a spawned driver is given to name itself in the run directory before the launch is
# called failed. It is the first thing its loop does, so this is generous rather than tuned; the
# environment moves it in the shape the loop's own poll interval is already moved by one.
HANDSHAKE_SECONDS = float(os.environ.get("CREW_LAUNCH_HANDSHAKE_SECONDS") or 30.0)
# How often this waiter looks for the wake it is waiting on. Nothing here is on a hot path: the
# thing being waited for takes minutes to hours.
POLL_SECONDS = 0.5
# How long a released run is watched before its silence is called a deliberate stop. A driver
# releases the run an instant before it writes its wake, so this only has to outlast one file
# rename — it is seconds rather than milliseconds because nothing is waiting on it.
RELEASE_GRACE_SECONDS = 3.0

# The harness's two records, each as the three parts `monitor.py` spells them in: the variable
# that moves the home, the home's own name under `~`, and the fixed subdirectory inside it.
SESSION_REGISTRY = ("CLAUDE_CONFIG_DIR", ".claude", "sessions")
TRANSCRIPTS = ("CLAUDE_CONFIG_DIR", ".claude", "projects")
REGISTRY_SUFFIX = ".json"
TRANSCRIPT_SUFFIX = ".jsonl"
# The three fields read out of those records, in the harness's own spelling.
REGISTRY_NAME = "name"
REGISTRY_SESSION = "sessionId"
PERMISSION_MODE = "permissionMode"

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


def session_name(entry, pid):
    """The name that session answers to, out of its registry entry."""
    name = entry.get(REGISTRY_NAME)
    if not isinstance(name, str) or not name.strip():
        raise LaunchError(
            f"the session registry entry for {pid} in {harness_directory(SESSION_REGISTRY)}"
            f" records no {REGISTRY_NAME}: pass --coordinator-name explicitly"
        )
    return name


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
    """The mode that session is in now, out of the newest transcript entry that records one."""
    session = entry.get(REGISTRY_SESSION)
    for path in transcripts(session):
        mode = recorded_mode(path)
        if mode is not None:
            return mode
    raise LaunchError(
        f"no transcript entry under {harness_directory(TRANSCRIPTS)} records the permission mode"
        f" of session {session or 'unnamed'} (pid {pid}):"
        f" {pass_explicitly(['--permission-mode'])}, because a guessed one launches every child in"
        f" the wrong permission regime"
    )


def resolve(args):
    """The three values the driver's start requires, each given by hand or read off the harness.

    Every one of the three is resolved or the launch fails: a value the harness cannot supply and
    the operator did not pass is not a value to hand the driver empty.
    """
    pid, name, mode = args.coordinator_pid, args.coordinator_name, args.permission_mode
    if pid is None or name is None or mode is None:
        given = {"--coordinator-pid": pid, "--coordinator-name": name, "--permission-mode": mode}
        pid, entry = coordinator(pid, [flag for flag, value in given.items() if value is None])
        name = name if name is not None else session_name(entry, pid)
        mode = mode if mode is not None else permission_mode(entry, pid)
    return pid, name, mode


def driver_command(args, session, resolved):
    """The driver command line this run starts on, `start` because start is what adopts."""
    pid, name, mode = resolved
    return [
        sys.executable, str(pathlib.Path(args.driver).resolve()), START_COMMAND,
        "--feature-dir", str(pathlib.Path(args.run_dir).resolve()),
        "--coordinator-name", name,
        "--coordinator-pid", str(pid),
        "--permission-mode", mode,
        "--tmux-session", session,
    ]


# --- the driver's own window ------------------------------------------------------------------


def tmux_session():
    """The tmux session this run's windows belong to: the one this command was typed in.

    Asked of tmux exactly as the driver asks it, because the driver's window and the run's child
    windows have to land in one session for the operator to have one place to watch the run from.
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

    Held under the launch lock, so that the next `/crew` typed at this run sees the driver this one
    started rather than an empty run directory it would start a second driver for. A driver that
    got as far as a wake — a preflight failure is over before its first poll — has answered too.
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


@contextlib.contextmanager
def launch_lock(run_dir):
    """Hold the run for the whole of the check-and-start; released before anything is waited on.

    Two `/crew` commands typed at one run inside a second both find no driver and both start one
    without this, which is the one thing adopt exists to prevent. It is not held across the wait:
    a waiter blocks for as long as the run takes, and nothing may be shut out for that.
    """
    with (run_dir / DRIVER_LOCK_NAME).open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def start_driver(args, run_dir):
    """Start this run's driver unless one is already driving it; returns nothing.

    The record the driver keeps of itself is the whole judgment: a pid that answers to a signal is
    a driver, and this attaches to it. Anything else — no record, or one naming a process that has
    gone — is a run to put a driver back on.

    Whose driver it is, this does not ask. A driver carries the coordinator it was started for for
    its whole life — the pid every child authenticates a ruling against — so one that outlived its
    session is a driver a new session cannot rule through. Detaching the driver is what made that
    state reachable, and closing it means re-anchoring the run's children as well as its driver,
    which is its own piece of work rather than this one's (#112).
    """
    with launch_lock(run_dir):
        if monitor.live_driver(run_dir):
            return
        # Resolved before tmux is asked for anything, so that the three values a run cannot be
        # started without are still what a failed launch reports, whatever else is wrong.
        resolved = resolve(args)
        session = tmux_session()
        command = driver_command(args, session, resolved)
        # The wake of the cycle just ended, taken away before the driver that would write the next
        # one starts: a waiter that found the old one would answer its coordinator with a snapshot
        # that has already been ruled on.
        with contextlib.suppress(OSError):
            wake_path(run_dir).unlink(missing_ok=True)
        open_window(command, session, run_dir / DRIVER_LOG_NAME)
        await_driver(run_dir, time.monotonic() + HANDSHAKE_SECONDS)


def wait_for_wake(run_dir):
    """Block until this run has something to say, print it, and end; returns the exit code.

    The one line printed is the driver's own wake snapshot, unchanged — this waiter composes
    nothing and judges nothing, so what a coordinator reads is what the driver wrote.

    Two endings are not snapshots, and neither is dressed up as one: a coordinator handed an
    invented snapshot would go off ruling on a run nobody is driving. A driver whose record still
    stands over a process that is gone was killed — the same judgment the dashboard's own banner
    makes, said here so this waiter does not block on it forever the way the coordinator's task
    used to. A driver that put the run down and left no wake ended without asking for anything: an
    interrupt in its own window is the ordinary reason, and a wake it could not write is the other,
    so the line says both and points at the log that tells them apart.
    """
    settled = None
    while True:
        try:
            wake = wake_path(run_dir).read_text(encoding="utf-8").strip()
        except OSError:
            wake = None
        if wake:
            print(wake, flush=True)
            return 0
        driver = monitor.recorded_driver(run_dir)
        if driver is not None and not monitor.alive(driver):
            print(
                f"crew: the driver of {run_dir.parent} was killed; it left no wake snapshot."
                f" /crew {run_dir.parent} puts a driver back on the run",
                flush=True,
            )
            return 0
        if driver is not None:
            settled = None
        elif settled is None:
            # A deliberate exit releases the run and writes its wake immediately after, so a
            # released record on its own proves nothing until that instant has passed.
            settled = time.monotonic() + RELEASE_GRACE_SECONDS
        elif time.monotonic() >= settled:
            print(
                f"crew: the driver of {run_dir.parent} ended without leaving a wake snapshot —"
                f" stopped in its own window, or unable to write one; {run_dir / DRIVER_LOG_NAME}"
                f" says which. /crew {run_dir.parent} starts it again",
                flush=True,
            )
            return 0
        time.sleep(POLL_SECONDS)


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
        "--permission-mode",
        help="the mode children launch under, where the session's transcript cannot be read",
    )
    parser.add_argument("--driver", default=str(DRIVER),
                        help="the driver this starts (default: %(default)s)")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        run_dir = run_directory(args)
        start_driver(args, run_dir)
    except LaunchError as error:
        print(f"launch: {error}", file=sys.stderr)
        return LAUNCH_ERROR_EXIT
    # Everything after this point is disposable. The run belongs to the driver's own window now,
    # and this process only carries its wake back — so whatever ends it, ends nothing.
    return wait_for_wake(run_dir)


if __name__ == "__main__":
    sys.exit(main())
