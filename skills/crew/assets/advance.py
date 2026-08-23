#!/usr/bin/env python3
"""Advance a run from one wave to the next without a coordinator turn.

    advance.py advance --table <wave table> --wave N --log <machine log> \
                       --out-dir <launch artifacts> --repair-model <full model ID>

One settled wave in, one decision out. The wave is landed through the merge driver; when every
ticket either came back `landable` and merged or is `parked` with no descendants, the next wave
is launched through the dispatch renderer and the operator is toasted. Nobody is consulted on
the way: the plan the user approved up front is the whole authority this needs (ADR-0001).

The four decisions, each recorded once in the machine log as an `advance` event:

    launched     the next wave is running, and the toast says so
    complete     that was the last wave; the run has nothing left to advance to
    escalated    a ticket failed, a parked ticket has descendants, or did not merge — the chain
                 stops here; the coordinator rules, woken by the child's own message rather than
                 by this script, which writes nothing into anybody's context
    interrupted  the operator stopped the run

A halt also marks every unlaunched descendant of a failed or parked ticket `blocked`, following
the `blocked_by` edges the wave table carries; because the halt launches nothing, a blocked
ticket never starts. The edges are the table's, like everything else about routing (ADR-0003).

A wave the log says has already been advanced past is refused before anything is merged: a second
run would start the same next wave again, in the worktrees the first one is working in. A wave
that escalated or was interrupted is not refused — re-running it is how the run carries on.

An interrupt — SIGINT or SIGTERM, the operator's Ctrl-C in the run's window — is taken at the
next step boundary. The merge or launch already in flight is left to finish, and is shielded from
the signal that reached this script, because a step torn down halfway is exactly the corrupted run
state the operator interrupted to avoid.

One line per step is printed, the merge driver's own lines among them:

    06 clean 4f1c…
    07 clean 9ab2…
    wave 2 launched 08, 09

Exit 0 when the run advanced or finished, 1 when it escalated, 130 when it was interrupted.
"""

import argparse
import pathlib
import signal
import subprocess
import sys

# The branch a ticket's worktree stands on is the dispatch renderer's naming, and the renderer is
# what launches a wave, so both come from the one script that owns them.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "dispatch"))
import dispatch  # noqa: E402
import machine_log  # noqa: E402
import run_plan  # noqa: E402

ASSETS = pathlib.Path(__file__).resolve().parent
MACHINE_LOG = ASSETS / "machine_log.py"
MERGE_DRIVER = ASSETS / "merge_driver.py"
DISPATCH = ASSETS / "dispatch" / "dispatch.py"

TMUX_BIN = "tmux"

LANDABLE = machine_log.LANDABLE
COMPLETED = machine_log.COMPLETED
FAILED = machine_log.FAILED
PARKED = machine_log.PARKED
# A failed ticket always stops the chain; a parked ticket only halts it when it has descendants.
BLOCKED = machine_log.BLOCKED

LAUNCHED = "launched"
ESCALATED = "escalated"
COMPLETE = "complete"
INTERRUPTED = "interrupted"

INTERRUPT_EXIT = 128 + signal.SIGINT
ESCALATED_EXIT = 1


class AdvanceError(Exception):
    """The wave cannot be advanced from, and nothing was decided about it."""


class Interrupt:
    """The operator's stop, taken at the next step boundary rather than where it landed."""

    def __init__(self):
        self.raised = False

    def arm(self):
        for number in (signal.SIGINT, signal.SIGTERM):
            signal.signal(number, self._take)

    def _take(self, signum, frame):
        self.raised = True


def run_shielded(command):
    """Run one step of the chain, shielded from the signal that stops the chain itself.

    A step gets its own session, so the interrupt an operator sends this script's process group
    does not reach a merge or a launch that is already under way: the chain stops between steps,
    with every step it took either finished or never started.
    """
    return subprocess.run(command, capture_output=True, text=True, start_new_session=True)


# --- the machine log --------------------------------------------------------------------------


def log_event(log, *arguments):
    """Append one event through the log's own writer, so its closed sets stay the only ones."""
    result = run_shielded([sys.executable, str(MACHINE_LOG), "--log", str(log), *arguments])
    if result.returncode != 0:
        raise AdvanceError(f"machine log: {(result.stderr or result.stdout).strip()}")


def already_advanced(records, wave, following):
    """The decision that already carried the run past `wave`, or None.

    Re-running a wave that escalated or was interrupted is how a run resumes once the coordinator
    has ruled. Re-running one that launched or completed would start the next wave a second time,
    in the worktrees the first one is working in — the duplicate the monitor calls an error.
    """
    for record in records:
        if record.get("event") != "advance":
            continue
        decision = record.get("decision")
        against = str(record.get("wave"))
        if decision == COMPLETE and against == str(wave):
            return record
        if decision == LAUNCHED and following is not None and against == str(following):
            return record
    return None


def pointers(ticket):
    """A ticket's own file and branch, so a ruling on it never starts with a hunt."""
    return f"ticket {ticket.path}; branch {dispatch.branch_name(ticket)}"


# --- the steps ----------------------------------------------------------------------------------


def land(table_path, wave, options):
    """Land the wave through the merge driver, and return the lines it printed."""
    result = run_shielded([
        sys.executable, str(MERGE_DRIVER), "land",
        "--table", str(table_path), "--wave", str(wave), "--log", str(options["log"]),
        "--repair-model", options["repair_model"],
        "--repair-budget-usd", str(options["repair_budget"]),
        "--repair-attempts", str(options["repair_attempts"]),
        "--repair-timeout", str(options["repair_timeout"]),
    ])
    lines = result.stdout.splitlines()
    if result.returncode != 0 and not lines:
        # The wave could not be started at all: no branch was touched, and the reason is the
        # driver's, printed where a caller reads it.
        raise AdvanceError((result.stderr or result.stdout).strip())
    return lines


def launch(table_path, wave, base_commit, options):
    """Launch the wave through the dispatch renderer, and return the lines it printed."""
    result = run_shielded([
        sys.executable, str(DISPATCH), "dispatch",
        "--table", str(table_path), "--wave", str(wave),
        "--out-dir", str(options["out_dir"]),
        "--log", str(options["log"]),
        "--base-commit", base_commit,
    ])
    return result.returncode == 0, (result.stdout + result.stderr).splitlines()


def toast(text):
    """Show one milestone in the operator's terminal, on the channel no model reads."""
    try:
        subprocess.run([TMUX_BIN, "display-message", text], capture_output=True, text=True)
    except OSError:
        # A run watched from outside tmux still advances; the toast is the operator's, not the
        # run's.
        pass


def landed_head(run):
    """The commit the integration branch stands at, which is what the next wave is cut from."""
    result = subprocess.run(
        ["git", "-C", run.repo_root, "rev-parse", run.integration_branch],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise AdvanceError(f"git rev-parse: {(result.stderr or result.stdout).strip()}")
    return result.stdout.strip()


# --- the decisions --------------------------------------------------------------------------------


def record(options, wave, decision, detail=None):
    arguments = ["advance", "--wave", str(wave), "--decision", decision]
    if detail:
        arguments += ["--detail", detail]
    log_event(options["log"], *arguments)


def block_descendants(plan, projection, roots, options):
    """Record every unlaunched descendant of a stopped ticket blocked; returns the lines printed.

    A ticket that already ran has an outcome of its own, and a ticket the halt leaves untouched is
    not this decision's business — only what can no longer start is blocked.
    """
    started = {
        ticket for ticket, facts in projection.tickets.items()
        if facts.launch is not None or facts.latest_settling_event is not None
    }
    blocked = {}
    for root, verdict in sorted(roots.items()):
        for number in plan.descendants((root,)):
            blocked.setdefault(number, []).append(f"{root} {verdict}")
    lines = []
    for number, why in sorted(blocked.items()):
        if number in started:
            continue
        detail = "descendant of " + ", ".join(why)
        log_event(
            options["log"], "outcome", "--ticket", number,
            "--outcome", BLOCKED, "--detail", detail,
        )
        lines.append(f"{number} {BLOCKED} {detail}")
    return lines


def decision_detail(detail, passed_over=()):
    """Add parked tickets passed over as settled without presenting them as halt reasons."""
    if not passed_over:
        return detail
    return f"{detail}; passed over as settled: " + "; ".join(passed_over)


def halt(plan, wave, projection, reasons, roots, options, passed_over=()):
    """Stop the chain: block what can no longer start, and record the one escalation."""
    lines = block_descendants(plan, projection, roots, options)
    detail = decision_detail("; ".join(reasons), passed_over)
    record(options, wave, ESCALATED, detail)
    return lines + [f"wave {wave} {ESCALATED} {detail}"]


def stop_short(wave, options, passed_over=()):
    detail = decision_detail("the operator interrupted the run", passed_over)
    record(options, wave, INTERRUPTED, detail)
    return list(passed_over) + [f"wave {wave} {INTERRUPTED}"]


def advance_wave(plan, table_path, wave, options, interrupt):
    """Advance the run past `wave`; returns `(the lines to print, the exit code)`."""
    run = plan.run
    tickets = plan.wave(wave).tickets

    records = machine_log.read_records(options["log"])
    projection = machine_log.project(records)
    following_wave = plan.following_wave(wave)
    following = following_wave.number if following_wave else None
    live = [
        ticket.id for ticket in tickets
        if projection.ticket(ticket.id).latest_settling_event is None
    ]
    if live:
        # Nothing has been decided and nothing should be: the wave is still being worked.
        raise AdvanceError(f"wave {wave} has not settled: " + ", ".join(live))
    settled_by = already_advanced(records, wave, following)
    if settled_by:
        raise AdvanceError(
            f"wave {wave} was already advanced past: {settled_by.get('decision')}"
            f" wave {settled_by.get('wave')} at {settled_by.get('ts')}"
        )

    if interrupt.raised:
        return stop_short(wave, options), INTERRUPT_EXIT
    lines = land(table_path, wave, options)

    records = machine_log.read_records(options["log"])
    projection = machine_log.project(records)
    reasons = []
    roots = {}
    passed_over = []
    for ticket in tickets:
        number = ticket.id
        state = projection.ticket(number).settlement_state
        if state == PARKED:
            if not plan.descendants((number,)):
                passed_over.append(f"{number} parked passed over as settled; no descendants")
                continue
            roots[number] = state
            reasons.append(f"{number} {state}; {pointers(ticket)}")
        elif state == FAILED:
            roots[number] = state
            reasons.append(f"{number} {state}; {pointers(ticket)}")
        elif state == COMPLETED:
            passed_over.append(f"{number} completed passed over as already landed")
            continue
        elif state != LANDABLE:
            reasons.append(f"{number} settled {state}; {pointers(ticket)}")
        else:
            reasons.append(f"{number} did not land; {pointers(ticket)}")
    if reasons:
        return (
            lines + halt(plan, wave, projection, reasons, roots, options, passed_over),
            ESCALATED_EXIT,
        )

    if interrupt.raised:
        return lines + stop_short(wave, options, passed_over), INTERRUPT_EXIT

    if following is None:
        detail = decision_detail("every wave of the run has landed", passed_over)
        record(options, wave, COMPLETE, detail)
        return lines + list(passed_over) + [f"wave {wave} {COMPLETE}"], 0

    launched, launch_lines = launch(table_path, following, landed_head(run), options)
    if not launched:
        detail = decision_detail(
            f"wave {following} did not launch: " + "; ".join(launch_lines), passed_over
        )
        record(options, following, ESCALATED, detail)
        return (
            lines + launch_lines + list(passed_over)
            + [f"wave {following} {ESCALATED} {detail}"], ESCALATED_EXIT
        )

    children = ", ".join(ticket.id for ticket in plan.wave(following).tickets)
    detail = decision_detail(f"advanced from wave {wave}: {children}", passed_over)
    record(options, following, LAUNCHED, detail)
    toast(f"crew wave {following} {LAUNCHED}")
    return lines + launch_lines + list(passed_over) + [f"wave {following} {LAUNCHED} {children}"], 0


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("advance",))
    parser.add_argument("--table", required=True, help="the approved wave table, as JSON")
    parser.add_argument("--wave", required=True, type=int, help="the wave that has settled")
    parser.add_argument("--log", required=True, help="the run's machine log")
    parser.add_argument("--out-dir", required=True, help="where the next wave's artifacts go")
    parser.add_argument(
        "--repair-model", required=True,
        help="the full model ID the merge driver's repair rung runs on, never an alias",
    )
    parser.add_argument("--repair-budget-usd", type=float, default=2.0)
    parser.add_argument("--repair-attempts", type=int, default=2)
    parser.add_argument("--repair-timeout", type=float, default=900.0)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    interrupt = Interrupt()
    interrupt.arm()
    try:
        plan = run_plan.load(args.table)
    except run_plan.RunPlanError as error:
        for problem in error.problems:
            print(problem, file=sys.stderr)
        return ESCALATED_EXIT

    options = {
        "log": args.log,
        "out_dir": args.out_dir,
        "repair_model": args.repair_model,
        "repair_budget": args.repair_budget_usd,
        "repair_attempts": args.repair_attempts,
        "repair_timeout": args.repair_timeout,
    }
    try:
        lines, code = advance_wave(plan, args.table, args.wave, options, interrupt)
    except (AdvanceError, run_plan.RunPlanError) as error:
        print(f"run: {error}", file=sys.stderr)
        return ESCALATED_EXIT

    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
