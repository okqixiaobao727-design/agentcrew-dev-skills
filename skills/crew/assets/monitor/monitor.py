#!/usr/bin/env python3
"""The monitor's operator surface: the dashboard pane, its toasts, and the receipt check.

    dashboard  draw one wave as a table — one row per launched ticket — and toast what just
               became true, refreshing in place when asked to
    pane       split a dedicated tmux pane running that refresh loop
    verify     decide whether a child's `CREW COMPLETE <sha>` holds, and log the receipt it earns

None of this costs a model token and none of it reaches the coordinator (ADR-0001): the table is
drawn in the operator's own pane, toasts go to `tmux display-message`, and the only thing written
anywhere is one `receipt` line in the run's machine log. The wake-up itself stays where it is, in
`monitor-wave.sh`, with the contract it already has — armed while every child is busy, exit
as soon as one needs attention, nonzero on a monitor error. This script is the display beside
it, so a failure it meets is drawn rather than raised; `docs/monitor-dashboard.md` publishes both
surfaces.

The dashboard reads the machine log (`docs/machine-log.md`) joined with the live agents list. The
worktree paths it is given are the wave's membership — the same arguments the wake-up is given.
"""

import argparse
import datetime
import json
import os
import pathlib
import re
import shlex
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

# The two events that settle a ticket: after either, its state is that line's word and its clock
# has stopped, whatever the agents list still says about its worktree.
SETTLING_EVENTS = ("receipt", "outcome")
SETTLED_STATE_KEYS = ("verdict", "outcome")
ESCALATION_EVENT = "escalation"

COLUMNS = ("WAVE", "TICKET", "CHILD", "STATE", "LAST EVENT", "ELAPSED")
COLUMN_GAP = "  "
# What an unsettled row shows when the agents list cannot be read, and when it holds more than one
# session for the same worktree — the wake monitor's own word for that.
UNKNOWN_STATE = "unknown"
VANISHED_STATE = "vanished"
DUPLICATE_STATE = "duplicate"
STUCK_STATE = "waiting"
CLEAR_SCREEN = "\x1b[H\x1b[2J"

DEFAULT_REFRESH_SECONDS = 5.0
TOAST_STATE_NAME = "toasts.json"

FULL_SHA = re.compile(r"^[0-9a-f]{40}$")

LANDABLE = "landable"
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
        return "--"
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
        states[cwd] = DUPLICATE_STATE if cwd in states else str(agent.get("status", UNKNOWN_STATE))
    return states


def settled(events):
    """The record that settled this ticket, or None while it is still live."""
    for record in reversed(events):
        if record.get("event") in SETTLING_EVENTS:
            return record
    return None


def settled_state(record):
    """The word a settling record settles a ticket into: its verdict, or its outcome."""
    for key in SETTLED_STATE_KEYS:
        value = record.get(key)
        if value:
            return str(value)
    return str(record.get("event"))


def build_rows(records, worktrees, wave, moment, states):
    """One row per launched ticket of this wave, in ticket order.

    A worktree the log carries no `launch` for was never launched into and is not a row; a ticket
    launched twice into the same worktree — a replacement child — is the one row its last launch
    describes.
    """
    wanted = {worktree_key(path) for path in worktrees}
    launches = {}
    for record in records:
        if record.get("event") == "launch" and worktree_key(record.get("worktree")) in wanted:
            launches[str(record.get("ticket"))] = record

    rows = []
    for ticket, launch in sorted(launches.items()):
        events = [record for record in records if str(record.get("ticket")) == ticket]
        settling = settled(events)
        if settling is not None:
            state = settled_state(settling)
            end = parse_timestamp(settling.get("ts"))
        elif states is None:
            state = UNKNOWN_STATE
            end = moment
        else:
            state = states.get(worktree_key(launch.get("worktree")), VANISHED_STATE)
            end = moment
        rows.append({
            "wave": str(wave),
            "ticket": ticket,
            "child": str(launch.get("child", "")),
            "state": state,
            "last_event": str(events[-1].get("event", "")) if events else "",
            "elapsed": elapsed(parse_timestamp(launch.get("ts")), end),
            "settled": settling is not None,
            "escalated": any(record.get("event") == ESCALATION_EVENT for record in events),
        })
    return rows


def render(rows, wave, moment):
    """The dashboard as the pane shows it: a title, a header, and one line per launched ticket."""
    cells = [list(COLUMNS)] + [
        [row["wave"], row["ticket"], row["child"], row["state"], row["last_event"], row["elapsed"]]
        for row in rows
    ]
    widths = [max(len(row[column]) for row in cells) for column in range(len(COLUMNS))]
    lines = [f"crew wave {wave} — {moment.strftime(TIMESTAMP_FORMAT)}"]
    lines += [
        COLUMN_GAP.join(value.ljust(width) for value, width in zip(row, widths)).rstrip()
        for row in cells
    ]
    return "\n".join(lines)


def toasts(rows, wave):
    """Every toast this pass has grounds for, each with the key that says it has been said.

    The key is per run, not per pass: an exception is announced when it becomes true and is not
    repeated while it stays true, which is what makes a refreshing pane bearable to sit beside.
    """
    said = []
    for row in rows:
        ticket = row["ticket"]
        if not row["settled"]:
            if row["state"] == STUCK_STATE:
                said.append((f"stuck:{ticket}", f"crew {ticket} stuck at a permission prompt"))
            elif row["state"] == VANISHED_STATE:
                said.append((f"vanished:{ticket}", f"crew {ticket} vanished"))
        if row["escalated"]:
            said.append((f"escalated:{ticket}", f"crew {ticket} escalated"))
    if rows and all(row["settled"] for row in rows):
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


def emit_toasts(rows, wave, state_path, tmux_bin):
    """Display the toasts this pass has grounds for and has not shown; returns their texts."""
    said = read_said(state_path)
    shown = []
    for key, text in toasts(rows, wave):
        if key in said:
            continue
        display(tmux_bin, text)
        said.add(key)
        shown.append(text)
    if shown:
        write_said(state_path, said)
    return shown


def toast_state_path(args):
    """Where this run remembers what it has already toasted: beside its machine log by default."""
    if args.toast_state:
        return pathlib.Path(args.toast_state)
    return pathlib.Path(args.log).resolve().parent / TOAST_STATE_NAME


def draw(args, moment):
    records = read_log(args.log)
    rows = build_rows(records, args.worktrees, args.wave, moment, agent_states(args.claude_bin))
    print(render(rows, args.wave, moment), flush=True)
    emit_toasts(rows, args.wave, toast_state_path(args), args.tmux_bin)


def run_dashboard(args):
    """Draw the wave once, or keep drawing it over itself; returns 0."""
    if args.refresh is None:
        draw(args, args.now or now())
        return 0
    while True:
        print(CLEAR_SCREEN, end="")
        draw(args, args.now or now())
        time.sleep(args.refresh)


def dashboard_command(args):
    """The command line the pane runs: this script's own dashboard, in its refresh loop."""
    command = [
        sys.executable, str(pathlib.Path(__file__).resolve()), "dashboard",
        "--log", str(args.log), "--wave", str(args.wave),
        "--refresh", str(args.refresh if args.refresh is not None else DEFAULT_REFRESH_SECONDS),
    ]
    if args.toast_state:
        command += ["--toast-state", str(args.toast_state)]
    command += [str(worktree) for worktree in args.worktrees]
    return shlex.join(command)


def run_pane(args):
    """Split the run's dashboard pane and print its id; returns 0, or 3 if tmux refused."""
    result = subprocess.run(
        [args.tmux_bin, "split-window", "-d", "-P", "-t", args.session, dashboard_command(args)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"MONITOR ERROR tmux split-window failed: {result.stderr.strip()}", file=sys.stderr)
        return MONITOR_ERROR_EXIT
    print(result.stdout.strip())
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

    def wave_command(name, help_text):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--log", required=True, help="the run's machine log")
        command.add_argument("--wave", required=True, help="the wave these worktrees belong to")
        command.add_argument("--toast-state", help="where this run remembers what it has toasted")
        command.add_argument("worktrees", nargs="+", help="the wave's worktrees")
        return command

    dashboard = wave_command("dashboard", "draw one wave as a table and toast what changed")
    dashboard.set_defaults(handler=run_dashboard)
    dashboard.add_argument(
        "--refresh", type=float, help="redraw every this many seconds instead of drawing once"
    )
    dashboard.add_argument(
        "--now", type=timestamp_argument,
        help="the moment elapsed times are measured to, stamped as the log stamps (default: now)",
    )

    pane = wave_command("pane", "split a dedicated tmux pane running the dashboard")
    pane.set_defaults(handler=run_pane)
    pane.add_argument("--session", required=True, help="the tmux target the pane is split in")
    pane.add_argument(
        "--refresh", type=float,
        help=f"the pane's redraw interval in seconds (default: {DEFAULT_REFRESH_SECONDS})",
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
