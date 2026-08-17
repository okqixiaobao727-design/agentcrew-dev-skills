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
which adopts a run the directory already holds rather than beginning a second one, and then
becomes the driver — same stdout, same exit code, so what the coordinator reads is unchanged.

    python3 launch.py <run-dir> [--coordinator-pid N] [--coordinator-name NAME]
                                [--permission-mode MODE] [--driver PATH]
"""

import argparse
import json
import os
import pathlib
import subprocess
import sys


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
# The driver this launches, beside its own skill assets. Overridable in the shape the driver's own
# `--codex-bridge` already sets — a default pointing at the neighbour, and a flag naming another —
# so a test drives this from its command line against a recorder rather than assembling a whole
# run for the real driver to refuse.
DRIVER = SCRIPT_DIR.parent / "driver" / "driver.py"
START_COMMAND = "start"

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


def driver_command(args):
    """The driver command line this run starts on, `start` because start is what adopts."""
    pid, name, mode = resolve(args)
    return [
        sys.executable, str(pathlib.Path(args.driver).resolve()), START_COMMAND,
        "--feature-dir", str(pathlib.Path(args.run_dir).resolve()),
        "--coordinator-name", name,
        "--coordinator-pid", str(pid),
        "--permission-mode", mode,
    ]


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
        command = driver_command(args)
    except LaunchError as error:
        print(f"launch: {error}", file=sys.stderr)
        return LAUNCH_ERROR_EXIT
    # Replaced by the driver rather than wrapping it: its stdout is the coordinator's one wake
    # snapshot and its exit code is the run's outcome, and a wrapper is one more thing that can
    # lose either.
    sys.stdout.flush()
    os.execv(command[0], command)


if __name__ == "__main__":
    sys.exit(main())
