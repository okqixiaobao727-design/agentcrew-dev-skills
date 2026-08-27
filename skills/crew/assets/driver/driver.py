#!/usr/bin/env python3
"""The crew driver: the one command a run is started by, and the state machine it runs on.

    start   preflight the run, build and validate its wave table, prepare the branch and the run
            directory, dispatch wave 1, start the dashboard, and run the wave loop to its end —
            or, where the feature already carries an unfinished run, adopt that one instead
    clear   inventory one recorded run, ask the operator, and remove its recorded artefacts
    resume  put that loop back where a ruling stopped it
    answer  deliver and record one coordinator answer to a child, on its own channel

The driver runs as a background task of the coordinator's own session, so it costs that session no
turn while it works and its exit is what wakes it (ADR-0001). Two contracts follow from that and
are the whole of what a woken coordinator reads; the tickets that build the wind-down on top of
this module couple to them rather than to anything inside it.

**Stdout is at most one line per lifecycle event.** A successful launch prints one line naming the
run directory. Every exit that wakes the coordinator prints its **wake snapshot** instead: one JSON
object, the last thing on stdout, carrying

    {"reason": preflight-failed | judgment-needed | driver-error | run-complete,
     "ticket": the ticket it applies to, or null,
     "pointer": where a ruling starts from,
     ...}                                    # whatever that reason names, as below

A snapshot on stdout is the one channel that reaches the woken coordinator without it opening a
run file, which is what keeps the oracle boundary intact; `monitor-wave.sh` and the Codex bridge's
`watch` already exit this way. A launched wave is not a wake reason: the driver prints its one
launch line and goes on working, and only one of the four reasons ends it.

A `judgment-needed` snapshot for a child escalation carries the child's message in `detail` and
the checked text in `brief`. When the witness failed, `brief` is empty and the plain-string
`witness_reason` sits beside it; `witness_reason` is absent on success and the snapshot's existing
`reason` remains the wake reason.

The `clear` subcommand is an operator terminal command rather than a coordinator lifecycle
event: it prints a multi-line inventory, asks for confirmation, and reports errors directly instead
of emitting a wake snapshot.

The `answer` subcommand is also an operator terminal command, but its failures emit a `driver-error`
wake snapshot so the coordinator can see why the child could not be answered.

**A preflight failure never reaches the coordinator as diagnosis.** Its first phase is five
read-only checks — a clean working tree, a base branch that resolves and fast-forwards, a valid
routing on every ticket (the renderer's own validation, which is the authority on the case list),
a complete acyclic dependency graph, and, for a run that reviews at all, the installed
Review-Switch command this repository's review lane is (ADR-0020) — plus the run's two configured
decisions, the repair rung's model and the tracker its merged tickets are closed in, neither of
which has a default. Once those pass, branch preparation checks out and fast-forwards the base and
runs the project's optional `[preflight] gate` from the repository root, immediately before it cuts
the integration branch. A red gate restores the starting ref and creates neither integration
branch nor child worktree. On any failure the driver launches nothing, prints the
`preflight-failed` snapshot naming the problem count and the display surface, and shows the full
problem list to the operator in a detached tmux window named `crew-preflight` in the run's own
session, ending with the reminder that fixes must be committed. That notice is the run's only
diagnosis surface: it is killed by name, in that session alone, at the start of the next run, so a
stale notice can never outlive its fix.

**Starting and resuming are one action.** A feature that already carries a run directory is a run
`start` adopts rather than one it starts beside: no branch is cut, no settled ticket is dispatched
a second time, the children keep the worktrees and windows they have, their hooks are put back
where their worktrees still stand, a coordinator that restarted re-anchors the run and the live
children it answers, the dashboard is drawn again, and the loop picks the run up from its log —
which is where every count it acts on lives, so an adopted run and an uninterrupted one are the
same code path. What ends adoption is the log's own final advance decision, which is the monitor's
predicate asked rather than restated: a run that has completed is not adopted, and the driver says
so and points at the run's report. So re-typing the crew command is the whole of what an
interruption, a driver crash or a coordinator restart costs.

**The wave loop is a rule table, and the rule table is exhaustive.** Between the launch and the
report the driver settles everything a written rule already decides: a `CREW COMPLETE` is verified
and, where it holds, settled in silence; an invalid receipt earns one re-ask and settles failed on
the second; parked and failed receipts are recorded by the driver rather than by hand; an idle
child earns one nudge and settles failed on the second silence, unless it is idle because it is
owed a ruling nothing has answered yet; a vanished child settles failed; a settled wave is
advanced, which lands its branches, resolves a mechanical conflict in the merge driver itself and
launches the next wave; a semantic conflict is answered first by a templated instruction to the
child that has to resolve it; a merged ticket is closed in the run's
tracker with its exact undo written into the log; and each wave's monitors are re-armed without a
coordinator turn.

**A completed run clears its own site.** After the report is written the driver runs a scripted
epilogue over the tickets the log says landed: their worktrees removed, their branches deleted,
their windows and the dashboard's killed, their Codex sessions stopped. Parked and failed tickets
keep worktree, branch and window, and the report lists those paths. The coordinator's window, the
integration branch the run hands over and the durable run directory are never touched. An artefact
that would not go does not withhold the run's ending: the `run-complete` snapshot carries a
`cleanup` field, null where the site was cleared and the failure where it was not. The `clear`
subcommand keeps its confirmation, because it is aimed at a run in any state rather than at one
whose log says what landed.

**Exactly three things end the loop at judgment.** A `CREW ASK` of any kind — an assumption
confirmation included, deliberately not auto-approved; a semantic conflict a child has bounced back
a second time; and any state the table has no row for, a child at a permission prompt and a monitor
that failed among them, so the unexpected reaches judgment instead of hanging. Each exits with the
wake snapshot, which carries the one command that resumes the loop where it left off.

The loop keeps no state of its own. Every count it acts on is read back out of the machine log each
time it is needed, which is what makes `resume` the same code path as carrying on and what lets a
driver that died mid-wave be replaced by another.

Everything the driver does to the repository, the children and the log it does through the existing
scripts — the dispatch renderer, the machine log, the monitor and its wake monitors, the Codex
bridge — at their published command lines; it reimplements none of them. The run directory holds
what today's index names and nothing this module invents: the wave table, the machine log, the
launch directory, the Codex state, and the parked paths — and, since the driver stopped being a
task of the coordinator's session (#103), this process's own two records: the pid it drives under
and the wake snapshot it leaves behind.
"""

import argparse
import contextlib
from dataclasses import dataclass, replace
import datetime
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

CREW_SKILL_DIR = pathlib.Path(__file__).resolve().parents[2]
ASSETS = CREW_SKILL_DIR / "assets"
DISPATCH = ASSETS / "dispatch" / "dispatch.py"
MACHINE_LOG = ASSETS / "machine_log.py"
MONITOR = ASSETS / "monitor" / "monitor.py"
MONITOR_WAVE = ASSETS / "monitor-wave.sh"
CODEX_BRIDGE = ASSETS / "codex" / "codex_bridge.py"
ADVANCE = ASSETS / "advance.py"
LAUNCH = ASSETS / "launch" / "launch.py"
WITNESS = ASSETS / "witness.py"

# The renderer owns what a ticket's branch is called.
sys.path.insert(0, str(DISPATCH.parent))
import dispatch  # noqa: E402
sys.path.insert(0, str(ASSETS))
import machine_log  # noqa: E402
# Account bindings become process environments only through the account module.
import accounts  # noqa: E402
import run_plan  # noqa: E402
import witness as witness_runner  # noqa: E402
# The monitor still owns process liveness, the driver pid record and every operator-facing surface;
# Machine-log interpretation itself comes from the projection above.
sys.path.insert(0, str(MONITOR.parent))
import monitor  # noqa: E402

# The run's own directory, inside the feature it runs: `docs/` publishes what it holds.
RUN_DIR_NAME = ".crew"
LOG_NAME = "log.jsonl"
# The channel a woken coordinator reads. The driver runs detached in its own tmux window now, so
# its stdout belongs to that pane and to the log beside it — the one JSON object the coordinator
# rules on is left here instead, where the waiter `/crew` leaves behind blocks on it (#103).
WAKE_NAME = "wake.json"
TABLE_NAME = "wave-table.json"
LAUNCH_DIR_NAME = "launch"
CODEX_DIR_NAME = "codex"
PARKED_PATHS_NAME = "parked-paths"
REPORT_NAME = "report.md"

# The operator's preflight surface: one detached window, found and cleared by this name.
NOTICE_WINDOW_NAME = "crew-preflight"
NOTICE_HEADING = "crew preflight stopped this run:"
NOTICE_REMINDER = (
    "Fix each of these and commit the fixes — an uncommitted fix is not one — then type /crew"
    " again."
)

# The settings file a session's hooks are registered in, relative to the directory it runs in.
SETTINGS_PATH = pathlib.Path(".claude") / "settings.local.json"
# The project config the dashboard's surface, the launch hook, and the run's configured routing
# decisions are read from.
CONFIG_NAME = "agentcrew.toml"
LAUNCH_HOOK_SECTION = ("hooks", "on-child-launch")
# The repair rung's model and the tracker a merged ticket is closed in. Both are routing decisions
# and both live in the project's committed config rather than in a launch flag: the only caller
# left to pass a flag is the coordinator, and a routing decision composed in a model turn is the
# thing this design deletes. Neither has a default here — a missing one is a preflight failure,
# which committing the config permanently clears.
REPAIR_MODEL_KEYS = ("repair", "model")
TRACKER_KIND_KEYS = ("tracker", "kind")
PREFLIGHT_GATE_KEYS = ("preflight", "gate")
# Unlike those two required project decisions, witness routing inherits the independently shipped
# defaults where the project leaves either cell out.
WITNESS_MODEL_KEYS = ("witness", "model")
WITNESS_BUDGET_KEYS = ("witness", "budget_usd")
# The account *names* this repository expects, declared in its committed config and never a path
# (ADR-0013). Declaring none is the ordinary case and checks nothing; declaring some makes a
# ticket naming an account outside them a problem stated in the config's own terms, which is a
# different fault from a name this machine never registered.
ACCOUNT_NAMES_KEYS = ("accounts", "names")

# The two trackers `references/trackers.md` declares exercised end to end, and the whole of what a
# close operation may be asked for: anything else stops the run rather than guessing a CLI.
TRACKER_GITHUB = "github"
TRACKER_LOCAL = "local"
TRACKERS = run_plan.TRACKERS
# A local ticket's status is a `Status:` line in its own file, and the value a finished one carries
# where the repo's convention document names none.
STATUS_LINE = re.compile(r"^(\s*(?:[-*]\s+)?)(?:\*\*)?Status(?:\*\*)?\s*:\s*(.*?)\s*$")
STATUS_FINISHED = "done"
# The labels a github ticket carries to say who may pick it up; a close takes the one it has off,
# and the undo puts it back (`references/trackers.md`).
PICKUP_LABELS = ("ready-for-agent", "ready-for-human")
GH = "gh"
# The installed Review-Switch command a reviewed ticket's child runs. This repository ships no
# review implementation and calls it across a process boundary (ADR-0020), so on a machine where
# it is not installed the review lane is a command the child discovers missing only once its work
# is already written — which is the failure preflight moves to before the run.
REVIEW_COMMAND = "review-bridge"

# The wake reasons, exhaustively: nothing else ends this driver. `run-complete` stays the
# wind-down's to fill out, and is named here because the snapshot's shape is one.
PREFLIGHT_FAILED = "preflight-failed"
JUDGMENT_NEEDED = "judgment-needed"
DRIVER_ERROR = "driver-error"
RUN_COMPLETE = "run-complete"

# What origin holds for the base branch: the one fact preflight and the preparation share.
UPSTREAM_PRESENT = "present"
UPSTREAM_ABSENT = "absent"
UPSTREAM_UNREACHABLE = "unreachable"

PREFLIGHT_EXIT = 1
DRIVER_ERROR_EXIT = 2
# What a shell reports for a process an interrupt ended, which is what this ends on when the
# operator stops the driver in its own window.
INTERRUPTED_EXIT = 130

CODEX = "codex"
CLAUDE = "claude"

# --- the rule table's own vocabulary ------------------------------------------------------------

# What a child says, in the grammar its first turn gave it. The grammar itself belongs to the
# log's own writer — `machine_log.final_verb` reads a body line by line and answers with the verb
# it ended on — so the rule table and the log can never disagree about what a child said. Only
# the three settling verbs are ruled on here; `CREW ASK` is the fourth the same reader knows, and
# it arrives as its own event rather than as a message this has to recognise.
CHILD_ROLE = "child"
COORDINATOR_ROLE = "coordinator"

COMPLETE_VERB = machine_log.COMPLETE_VERB
PARKED_VERB = machine_log.PARKED_VERB
FAILED_VERB = machine_log.FAILED_VERB
ESCALATION_VERB = machine_log.ESCALATION_VERB
ESCALATION_KINDS = machine_log.ESCALATION_KINDS

# What the driver says back. Each opens with its own marker, because the marker is how the loop
# reads its own history out of the log: a rung that has already fired for a ticket is a ruling of
# that shape standing in the log, which is what makes every count survive a resume.
RECHECK_MARKER = machine_log.RECHECK_MARKER
RESEND_MARKER = machine_log.RESEND_MARKER
NUDGE_MARKER = machine_log.NUDGE_MARKER
MERGE_MARKER = machine_log.MERGE_MARKER
ANCHOR_MARKER = machine_log.ANCHOR_MARKER
HANDED_OVER_MARKER = machine_log.HANDED_OVER_MARKER

# The tmux key names the permission-prompt command accepts, kept narrow so an answer cannot
# accidentally become an unsupported tmux key sequence.
ANSWER_KEYS = tuple("0123456789") + ("Up", "Down", "Left", "Right", "Enter", "S-Enter")

# The socket a Claude child authenticates its coordinator by, spelled as the first turn spells it.
COORDINATOR_SOCKET = "uds:/tmp/cc-socks/{pid}.sock"

HANDED_OVER = (
    "{marker} {ticket} — this escalation was handed to the coordinator, which is where it is"
    " answered. An answer sent as tmux keys passes no hook and so reaches no log; this line is"
    " what the run's report has of it."
)
ASK_SHAPE = f"CREW ASK <NN> <{'|'.join(ESCALATION_KINDS)}> [— <body>] [ts=<unix>]"

RECHECK_TEMPLATE = (
    "{marker} {ticket} {sha} — the receipt you sent did not verify: {problem}. Finish the work in"
    " your worktree and commit it. {receipt_direction} This is the one re-ask — a second receipt"
    " that does not verify settles this ticket failed."
)
RESEND_TEMPLATE = (
    "{marker} {ticket} — the line you ended on reached for one of this run's verbs and missed its"
    " shape, so nothing settled and nobody was woken: {line}. {receipt_direction} This is the one"
    " re-ask — a second line that misses its shape settles this ticket failed."
)
NUDGE_TEMPLATE = (
    "{marker} {ticket} — your session is idle and this run holds no receipt from you."
    " {receipt_direction} This is the one nudge — a second idle silence settles this ticket"
    " failed."
)
ANCHOR_TEMPLATE = (
    "{marker} {ticket} — this run is driven by a coordinator session that has restarted, so the"
    " socket your first turn told you to trust is dead. Your coordinator is the Claude session"
    " `{name}` now, and its messages arrive from `{socket}` — that socket is the identity. Reply"
    " with SendMessage to `{name}`; nothing else about your ticket has changed."
)
# The rework instruction is scoped to the conflict rather than to the whole workflow: the work it
# asks for is a resolution, and re-running everything the ticket already ran green buys nothing the
# conflict put in doubt.
MERGE_TEMPLATE = (
    "{marker} {ticket} — your branch {branch} conflicts with {integration} in a way no script can"
    " resolve: {reason}. Merge {integration} into {branch}, resolve the conflict, re-run the tests"
    " the conflict touched, re-review scoped to the conflict-resolution diff, and commit."
    " {receipt_direction}"
)

# The verdicts and events the loop reads back out of the log. The writer owns their spelling.
LANDABLE = machine_log.LANDABLE
PARKED = machine_log.PARKED
FAILED = machine_log.FAILED
COMPLETED = machine_log.COMPLETED
BLOCKED = machine_log.BLOCKED
LAUNCHED = "launched"
# The advance decision this loop writes itself: the run ended because the chain stopped on reasons
# the rule table had already settled, which no other decision in the log's vocabulary says.
STOPPED = "stopped"
ESCALATED = "escalated"

# What a wake monitor says a child is doing. `busy` is the only one the loop lets stand.
STATUS_BUSY = "busy"
STATUS_IDLE = "idle"
STATUS_WAITING = "waiting"
STATUS_VANISHED = "vanished"
STATUS_PARKED = "parked"

# How often the loop asks the log what has happened, and how long it waits for anything at all
# before a run that is going nowhere becomes a wake rather than a hang.
DEFAULT_POLL_SECONDS = 5.0
DEFAULT_TIMEOUT_SECONDS = 7200.0
# A failed gate can print an entire parallel test run. The notice keeps the end, where test runners
# put their summary and failure diagnosis, without turning the operator's diagnosis surface into a
# second copy of the full log.
GATE_OUTPUT_LINE_LIMIT = 20
# How long a monitor asked to stop is given before it is killed.
MONITOR_STOP_SECONDS = 5.0

ADVANCE_ESCALATED_EXIT = 1
ADVANCE_INTERRUPTED_EXIT = 130


class DriverError(Exception):
    """Something outside the rule table: it wakes the coordinator with a snapshot."""

    def __init__(self, message, ticket=None, pointer=None):
        super().__init__(message)
        self.ticket = ticket
        self.pointer = pointer


class Unreachable(DriverError):
    """The child's own channel could not be reached: its window is gone, or it never had one.

    A driver error like any other wherever an instruction has to arrive — but told apart from one,
    because a run taking over children it did not launch has to be able to leave a child it cannot
    talk to for the rule that settles it, and must not do the same to a log write that failed.
    """


class ClearError(Exception):
    """The recorded run cannot be inventoried or cleared safely."""


@dataclass(frozen=True)
class ClearPlan:
    """The destructive inputs prepared by the confirmed terminal inventory."""

    rows: list
    launches: list
    dashboard_window: str | None


class Wake(Exception):
    """A state the rule table settles by handing it to judgment; the loop exits on it.

    Exactly three raise it: a CREW ASK of any kind, a semantic conflict a child has bounced back a
    second time, and a state the table has no row for. Everything else the loop settles itself.
    """

    def __init__(self, reason, ticket=None, pointer=None, **fields):
        super().__init__(reason)
        self.reason = reason
        self.ticket = ticket
        self.pointer = pointer
        self.fields = fields


# --- the run this process is driving ------------------------------------------------------------

# The run directory this driver has in hand, or None while it has none — before a fresh run has
# made its directory, and in the commands that do not drive a run at all. Two things hang off it,
# and both are a driver's word about its own life: the pid record the dashboard reads liveness
# from, and the file the coordinator's waiter blocks on. Module state because it is one fact about
# the process: a driver drives one run, from the moment it takes it up until it puts it down.
_run_in_hand = None


def take_up_run(run_dir):
    """Take up that run: name this process as its driver, and open its wake channel.

    A run directory that is not there yet is not taken up. That is the fresh start before it has
    made one, and the start that fails preflight without ever making one — a run that does not
    begin leaves no run directory behind, and the launcher that made one for its own log has
    already put the directory there for every other case.
    """
    global _run_in_hand
    if run_dir is None or not pathlib.Path(run_dir).is_dir():
        return
    _run_in_hand = pathlib.Path(run_dir)
    try:
        monitor.record_driver(_run_in_hand, os.getpid())
    except monitor.MonitorError as error:
        # A driver nothing can name is a driver nothing can tell from a killed one, and a second
        # `/crew` would start another beside it. That is not a run to carry on quietly.
        _run_in_hand = None
        raise DriverError(f"this run could not be taken up: {error}", pointer=str(run_dir))


def put_down_run():
    """Put this run down: take the pid record away, which is what makes an exit deliberate.

    Every ending this process reaches by running code passes through here, and no kill does — so
    a record still naming a process that has gone is a killed driver, and the dashboard says so.
    """
    global _run_in_hand
    if _run_in_hand is None:
        return
    monitor.release_driver(_run_in_hand)
    _run_in_hand = None


# --- the wake snapshot ----------------------------------------------------------------------


def snapshot(reason, ticket=None, pointer=None, **fields):
    """Print the run's wake snapshot, and leave it where the coordinator's waiter reads it.

    Flushed as it is written, like every other line this driver puts on that channel: stdout is a
    pipe here, and a lifecycle line held in a buffer until the process ends is not one line per
    lifecycle event.

    Every call is this process's last act, so the run is put down first and the wake written
    second: a coordinator that pastes the resume command the instant it is woken must not find a
    pid that was about to stop and attach to a driver already on its way out. The file is put in
    place by rename, because a waiter reading it half-written would read no wake at all.
    """
    record = {"reason": reason, "ticket": ticket, "pointer": pointer}
    record.update(fields)
    line = json.dumps(record, ensure_ascii=False)
    print(line, flush=True)
    run_dir = _run_in_hand
    put_down_run()
    if run_dir is None:
        return
    path = run_dir / WAKE_NAME
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(line + "\n", encoding="utf-8")
        os.replace(temporary, path)
    except OSError as error:
        # The snapshot is on stdout either way, and the driver's own pane and log keep it. A wake
        # channel that could not be written is said where a wake cannot be: on stderr.
        temporary.unlink(missing_ok=True)
        print(f"crew: the wake snapshot could not be left in {path}: {error}",
              file=sys.stderr, flush=True)


# --- git ----------------------------------------------------------------------------------


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )


def git_output(repo, *args):
    """What that git command printed, or None where it refused to answer."""
    result = git(repo, *args)
    return result.stdout.strip() if result.returncode == 0 else None


def run_command(arguments, message, ticket=None, pointer=None):
    """Run one of the run's own scripts; returns what it printed on stdout, and raises a
    DriverError on anything but success.

    Its output is captured rather than passed through: the driver's stdout is the coordinator's
    channel, and one line per lifecycle event is the whole of what may go on it.
    """
    result = subprocess.run(
        [str(argument) for argument in arguments], capture_output=True, text=True
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().replace("\n", " ")
        raise DriverError(f"{message}: {detail}", ticket=ticket, pointer=pointer)
    return result.stdout


# --- the five preflight checks ----------------------------------------------------------------


def dirty_tree_problems(repo):
    """One problem per tracked path the working tree has not committed.

    Untracked paths are deliberately not inventoried: the run directory, a child's guard assets and
    the operator's scratch files are all untracked, and none of them is what a run must not start
    over.
    """
    status = git_output(repo, "status", "--porcelain")
    if status is None:
        return [f"working tree: {repo} is not a git repository this run can read"]
    problems = []
    for line in status.splitlines():
        if not line.strip() or line.startswith("??"):
            continue
        problems.append(
            f"working tree: {line[2:].strip()} is {line[:2].strip()} and uncommitted —"
            " commit it or put it aside"
        )
    return problems


def default_base_branch(repo):
    """The repository's own default branch, as `refs/remotes/origin/HEAD` names it."""
    head = git_output(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if head:
        return head.split("/", 1)[1] if "/" in head else head
    return None


def upstream_state(repo, branch):
    """Whether origin carries that branch now; returns one of the three states and its detail.

    Asked of origin itself rather than of this checkout's remote-tracking refs, which go stale in
    both directions: a ref that has moved on, and a ref for a branch upstream has since deleted.
    One answer serves the whole run — preflight compares against it, and the preparation pulls
    only where it says there is something to pull, so the two never disagree about upstream.
    """
    if not git_output(repo, "remote"):
        return UPSTREAM_ABSENT, ""
    listed = git(repo, "ls-remote", "--heads", "origin", branch)
    if listed.returncode != 0:
        return UPSTREAM_UNREACHABLE, listed.stderr.strip()
    return (UPSTREAM_PRESENT if listed.stdout.strip() else UPSTREAM_ABSENT), ""


def base_branch_problems(repo, branch, upstream):
    """Whether the base branch resolves and whether `git pull --ff-only` would carry it forward.

    Upstream is asked what it holds now, not what this checkout last heard: the comparison is
    against a freshly fetched remote-tracking ref, because a stale one turns a diverged branch into
    a preflight the run passes and a pull it then fails on, which is the failure this check exists
    to spare the coordinator. Fetching moves no local branch and touches no working tree, so the
    check stays read-only about the run.

    A base branch upstream does not carry has nothing to fast-forward onto, and passes: the check
    is that the branch this run cuts from is not diverged from upstream, not that a remote exists.
    A branch ahead of its counterpart is not diverged either — `git pull --ff-only` carries it as
    it stands — so only two histories that have parted ways fail here.
    """
    if not branch:
        return [
            "base branch: this run has no base branch — the repository names no"
            " refs/remotes/origin/HEAD, so run `git remote set-head origin -a` or give the run"
            " one with --base-branch <branch>"
        ]
    if git_output(repo, "rev-parse", "--verify", f"refs/heads/{branch}") is None:
        return [f"base branch: `{branch}` does not resolve to a branch in this repository"]
    state, detail = upstream
    if state == UPSTREAM_UNREACHABLE:
        return [
            f"base branch: origin could not be reached, so whether `{branch}` fast-forwards is"
            f" unknown — {detail}"
        ]
    if state == UPSTREAM_ABSENT:
        return []
    fetched = git(repo, "fetch", "origin", branch)
    if fetched.returncode != 0:
        return [
            f"base branch: origin/{branch} could not be fetched, so whether `{branch}`"
            f" fast-forwards is unknown — {fetched.stderr.strip()}"
        ]
    upstream = "FETCH_HEAD"
    behind = git(repo, "merge-base", "--is-ancestor", branch, upstream).returncode == 0
    ahead = git(repo, "merge-base", "--is-ancestor", upstream, branch).returncode == 0
    if not behind and not ahead:
        return [
            f"base branch: `{branch}` cannot fast-forward onto origin/{branch} — the two have"
            " diverged, so reconcile them before the run cuts from it"
        ]
    return []


def review_command_problems(plan):
    """Whether the Review-Switch command is installed, asked only of a run that reviews.

    A run whose wave table carries no review lane calls the command never, and a machine that
    reviews nowhere is not misconfigured for lacking it — so the check is the plan's own question,
    not the machine's. The lookup is this driver's `PATH`, which is the environment a child
    inherits through the window the driver opens for it.
    """
    reviewed = [ticket.id for ticket in plan.tickets if ticket.review is not None]
    if not reviewed or shutil.which(REVIEW_COMMAND) is not None:
        return []
    return [
        f"review lane: `{REVIEW_COMMAND}` is not on this machine's PATH, and ticket"
        f"{'s' if len(reviewed) > 1 else ''} {', '.join(reviewed)} carry a review this run has no"
        " way to run — install Review-Switch, which owns the review this repository only routes"
        " (ADR-0020), and put its command on your PATH"
    ]


def configured_base_gate(config):
    """The optional base-gate argv, plus any problem that makes it unsafe to execute."""
    value = config_value(config, PREFLIGHT_GATE_KEYS)
    if value is None:
        return None, []
    if not isinstance(value, list) or not value:
        return None, [
            "base gate: [preflight] gate is not a non-empty argv list — configure each command"
            " argument as one string, or remove the key to leave the base ungated"
        ]
    invalid = [
        argument
        for argument in value
        if not isinstance(argument, str) or not argument.strip()
    ]
    if invalid:
        return None, [
            "base gate: [preflight] gate carries an empty or non-string argument — configure each"
            " command argument as one non-empty string"
        ]
    return tuple(value), []


def base_gate_problem(repo, command):
    """Run the configured gate on the checked-out base; return its problem or None on success."""
    if command is None:
        return None
    rendered = shlex.join(command)
    try:
        result = subprocess.run(
            command,
            cwd=str(repo),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except OSError as error:
        return f"base gate: `{rendered}` could not be started — {error}"
    if result.returncode == 0:
        return None
    lines = (result.stdout or "").splitlines()
    tail = "\n".join(lines[-GATE_OUTPUT_LINE_LIMIT:]) or "(no output)"
    return (
        f"base gate: `{rendered}` returned exit status {result.returncode} — last output:\n{tail}"
    )


# --- the preflight notice ----------------------------------------------------------------------


def tmux_session(given):
    """The tmux session this run's windows belong to: the one the driver was launched in."""
    if given:
        return given
    session = subprocess.run(
        ["tmux", "display-message", "-p", "#{session_id}"], capture_output=True, text=True
    )
    if session.returncode != 0 or not session.stdout.strip():
        raise DriverError(
            "this run has no tmux session to draw in: name one with --tmux-session"
        )
    return session.stdout.strip()


def tmux(arguments, message):
    """Run one tmux command, raising a DriverError on refusal; returns what it printed.

    A refusal is never swallowed: the notice window is the operator's whole diagnosis surface, and
    a driver that reported a surface it could not draw would leave a failed run with nowhere to
    read why.
    """
    result = subprocess.run(["tmux", *arguments], capture_output=True, text=True)
    if result.returncode != 0:
        raise DriverError(f"{message}: {(result.stderr or result.stdout).strip()}")
    return result.stdout


def composer_holds(window, text):
    """Whether the pane's cursor line still holds the final line just typed."""
    cursor = tmux(
        ["display-message", "-p", "-t", window, "#{cursor_y}"],
        f"the cursor in {window} could not be read",
    ).strip()
    try:
        cursor_y = str(int(cursor))
    except ValueError as error:
        raise DriverError(f"the cursor in {window} was not a row number: {cursor}") from error
    line = tmux(
        ["capture-pane", "-p", "-J", "-t", window, "-S", "0", "-E", cursor_y],
        f"the composer in {window} could not be read",
    )
    typed_lines = [typed.rstrip() for typed in text.splitlines() if typed.strip()]
    if not typed_lines:
        return True
    cursor_line = line.splitlines()[-1] if line.splitlines() else ""
    return typed_lines[-1] in cursor_line


def notice_windows(session):
    """Every preflight notice window this session holds, by id."""
    listed = tmux(
        ["list-windows", "-t", session, "-F", "#{window_id} #{window_name}"],
        f"the run's tmux session {session} could not be read",
    )
    found = []
    for line in listed.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[1].strip() == NOTICE_WINDOW_NAME:
            found.append(parts[0])
    return found


def clear_notice(session):
    """Take down the notice of a previous failed start, in this session alone; returns nothing.

    Scoped to the session the driver runs in, never the whole tmux server: two repositories
    preflighting at once must not clear each other's diagnosis.
    """
    for window_id in notice_windows(session):
        tmux(
            ["kill-window", "-t", window_id],
            f"the stale preflight notice {window_id} could not be cleared",
        )


def notice_text(problems):
    return "\n".join([NOTICE_HEADING, ""] + list(problems) + ["", NOTICE_REMINDER])


def show_notice(session, problems):
    """Draw the full problem list where the operator reads it, and leave it standing; returns
    nothing.

    The window holds after printing, so the list can be read at leisure and re-read.
    """
    text = notice_text(problems)
    command = f"printf '%s\\n' {shlex.quote(text)}; while :; do sleep 3600; done"
    tmux(
        ["new-window", "-d", "-n", NOTICE_WINDOW_NAME, "-t", session, command],
        f"the preflight notice could not be drawn in {session}",
    )


# --- the run's own preparation -------------------------------------------------------------


def repository_root(feature_dir, given):
    if given:
        return pathlib.Path(given).resolve()
    root = git_output(feature_dir, "rev-parse", "--show-toplevel")
    if not root:
        raise DriverError(f"{feature_dir} is not inside a git repository")
    return pathlib.Path(root).resolve()


def project_config(repo):
    """The project's `agentcrew.toml`, or an empty document where the repo carries none."""
    config = repo / CONFIG_NAME
    if not config.exists():
        return {}
    try:
        return tomllib.loads(config.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise DriverError(f"{config} is unreadable: {error}") from error


def config_value(config, keys):
    """The value that path of keys reaches in the config, or None where it reaches nothing."""
    section = config
    for key in keys:
        section = section.get(key) if isinstance(section, dict) else None
    return section


def launch_hook(config):
    """The project's `[hooks.on-child-launch]`, or nothing where it declares none."""
    section = config_value(config, LAUNCH_HOOK_SECTION)
    return section if isinstance(section, dict) and section else None


def declared_accounts(config):
    """The account names this repository's config declares, or none where it declares none."""
    names = config_value(config, ACCOUNT_NAMES_KEYS)
    if not isinstance(names, list):
        return []
    return [str(name).strip() for name in names if str(name).strip()]


def coordinator_config_home():
    """The Claude configuration home this coordinator itself runs under.

    Which is the account a ticket naming none runs on: the run inherits the operator's own login
    exactly as every run did before accounts existed.
    """
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    return str(pathlib.Path(configured).expanduser() if configured
               else pathlib.Path.home() / accounts.CONFIG_HOME)


def run_section(args, repo, feature_dir, run_dir, base_branch, return_branch, base_commit,
                config):
    """The table's `run` section: everything about this run that is not a ticket."""
    run = {
        "repo_root": str(repo),
        "spec_path": str(args.spec or feature_dir / "spec.md"),
        "integration_branch": f"crew/{feature_dir.name}",
        "integration_base_commit": base_commit,
        "coordinator_name": args.coordinator_name,
        "coordinator_pid": args.coordinator_pid,
        "coordinator_session": args.coordinator_session,
        "crew_skill_dir": str(CREW_SKILL_DIR),
        "tmux_session": args.tmux_session,
        "permission_mode": args.permission_mode,
        # Recorded for the run's own wind-down: where the run came from, and where it goes back to.
        "base_branch": base_branch,
        "return_branch": return_branch,
        "feature_dir": str(feature_dir),
        # The two configured decisions, recorded as this start resolved them. The loop and every
        # resume of it read the run's own record rather than the config file, so editing
        # `agentcrew.toml` mid-run cannot silently retarget a run already under way.
        "repair_model": config_value(config, REPAIR_MODEL_KEYS),
        # Missing witness values are deliberately left for RunPlan to resolve from the shipped
        # `[witness]` defaults. They never fall through to `[repair]`, even while the literals
        # shipped for the two rungs happen to match.
        "witness_model": config_value(config, WITNESS_MODEL_KEYS),
        "witness_budget_usd": config_value(config, WITNESS_BUDGET_KEYS),
        "tracker": config_value(config, TRACKER_KIND_KEYS),
        # The account names this repository declares, and the coordinator's own configuration
        # home — which is the account a ticket naming none runs on, written down here so the
        # value, and not the rule that produced it, is what every consumer reads (ADR-0014).
        "declared_accounts": declared_accounts(config),
        "coordinator_config_home": coordinator_config_home(),
        "codex": {
            "bridge": str(args.codex_bridge or CODEX_BRIDGE),
            "state_dir": str(run_dir / CODEX_DIR_NAME),
        },
    }
    hook = launch_hook(config)
    if hook:
        run["launch_hook"] = hook
    return run


def restore_return_branch(repo, return_branch):
    """Put a failed preparation back on the branch or detached commit it started from."""
    if git(repo, "show-ref", "--verify", f"refs/heads/{return_branch}").returncode == 0:
        result = git(repo, "switch", "--", return_branch)
    else:
        result = git(repo, "switch", "--detach", "--", return_branch)
    if result.returncode != 0:
        raise DriverError(
            f"the base gate failed and the starting ref {return_branch} could not be restored:"
            f" {result.stderr.strip()}"
        )


def prepare_branches(repo, base_branch, integration_branch, pull, gate):
    """Fast-forward the base branch and cut this run's integration branch from it; returns the
    branch the run returns to, the commit it is based on, and any base-gate problem."""
    current = git_output(repo, "rev-parse", "--abbrev-ref", "HEAD")
    return_branch = current if current and current != "HEAD" else git_output(
        repo, "rev-parse", "HEAD"
    )
    if current != base_branch:
        result = git(repo, "switch", base_branch)
        if result.returncode != 0:
            raise DriverError(f"the base branch {base_branch} could not be checked out")
    if pull:
        result = git(repo, "pull", "--ff-only")
        if result.returncode != 0:
            raise DriverError(
                f"{base_branch} could not be fast-forwarded: {result.stderr.strip()}"
            )
    gate_problem = base_gate_problem(repo, gate)
    if gate_problem is not None:
        restore_return_branch(repo, return_branch)
        return return_branch, None, gate_problem
    if git_output(repo, "rev-parse", "--verify", f"refs/heads/{integration_branch}"):
        raise DriverError(
            f"the integration branch {integration_branch} already exists, and this feature holds no"
            " wave table to adopt the run that cut it from: a run was started here and its record"
            " is gone, which no fresh start may cut a branch over"
        )
    result = git(repo, "switch", "-c", integration_branch)
    if result.returncode != 0:
        raise DriverError(
            f"the integration branch {integration_branch} could not be cut:"
            f" {result.stderr.strip()}"
        )
    return return_branch, git_output(repo, "rev-parse", "HEAD"), None


def install_hook(log, settings, role, ticket=None, session_id=None):
    """Register one side's log hook; session_id scopes the coordinator's bounded-read hook."""
    arguments = [
        sys.executable, MACHINE_LOG, "--log", log, "install",
        "--settings", settings, "--role", role,
    ]
    if ticket:
        arguments += ["--ticket", ticket]
    if session_id is not None:
        arguments += ["--session-id", session_id]
    run_command(arguments, f"the {role} hook could not be installed in {settings}")


def record_base_gate(log, gate):
    """Record whether this fresh run checked its base, through the Machine-log CLI."""
    status = "not-configured" if gate is None else "passed"
    arguments = [
        sys.executable, MACHINE_LOG, "--log", log, "base-gate", "--status", status,
    ]
    for value in gate or ():
        arguments.append(f"--argument={value}")
    run_command(arguments, "the base-gate result could not be recorded", pointer=str(log))


def launched_children(log):
    """The last launch record per child: its ticket, worktree and executor."""
    children = {}
    for line in pathlib.Path(log).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("event") == "launch" and record.get("ticket") is not None:
            children[str(record["ticket"])] = record
    return list(children.values())


def arm_monitors(run_dir, log, children, bridge, bindings):
    """Arm the wake monitors over this wave's live children, one per watch lane.

    Each is a one-shot wake-up: armed while every child under it is busy, exiting with its snapshot
    as soon as one needs attention. They are this process's own children now rather than detached
    watchers — the driver is the loop their exit reports to, so a monitor outliving the driver would
    be a wake-up nobody is left to read.

    The Claude lane is one monitor **per account binding**, not one per wave. Its whole reading is
    `claude agents --json`, which lists the account the command runs under and no other, so a child
    on a second account is missing from a list that could never have held it — and a monitor that
    read one list for a mixed wave called that child `vanished` ten seconds after launching it,
    while it was working (#110). Each group is therefore polled in its own binding's environment,
    which for an inherited binding is this process's own, untouched.

    Returns one armed monitor per lane, each carrying the process, the lane's account binding and
    the paths or tickets it stands over, so the lane a snapshot came from is never guessed from
    its shape, and a lane re-armed after firing is re-armed for its own group alone.
    """
    parked_paths = run_dir / PARKED_PATHS_NAME
    parked_paths.touch()
    claude_groups = {}
    codex_states = {}
    for child in children:
        ticket = str(child.get("ticket"))
        if child.get("executor") == CODEX:
            codex_states[str(run_dir / CODEX_DIR_NAME / f"{ticket}.json")] = ticket
        elif child.get("worktree"):
            claude_groups.setdefault(bindings.get(ticket), []).append(child["worktree"])
    armed = []
    for binding, worktrees in claude_groups.items():
        armed.append({
            "lane": CLAUDE,
            "account": binding,
            "process": spawn([
                str(MONITOR_WAVE), "--log", str(log),
                # Its own pid, because the wake-up it holds is readable by this process and no
                # other: a driver killed outright runs no `disarm`, and a monitor that outlived
                # one polled for ever, spawning a CLI every poll for nobody.
                "--driver-pid", str(os.getpid()),
                str(parked_paths), *worktrees,
            ], environment=accounts.process_environment(binding)),
        })
    if codex_states:
        armed.append({
            "lane": CODEX,
            # A Codex child's liveness is a bridge state file in the run directory, which no Claude
            # profile has anything to say about: this lane is account-agnostic and stays one watch.
            "account": None,
            "tickets": codex_states,
            "process": spawn([sys.executable, str(bridge), "watch", *codex_states]),
        })
    return armed


def lane_of(child):
    """The wake monitor lane a child is watched under, which is the executor it runs on."""
    return CODEX if child.get("executor") == CODEX else CLAUDE


def watch_lane(child, binding):
    """The identity of the monitor that watches this child: its lane, and the account behind it.

    Two accounts' live sources are disjoint, so two Claude children on different bindings are
    watched by different monitors — collapsing them to the lane alone would leave one group
    unwatched every time the other was re-armed. The Codex lane carries no binding.
    """
    return (CODEX, None) if child.get("executor") == CODEX else (CLAUDE, binding)


def spawn(arguments, environment=None):
    """Start a wake monitor with its snapshot on a pipe; returns the process.

    `environment` is what `accounts.process_environment` answered for the lane's binding: None
    where the lane inherits this process's environment as it stands, which is every lane of a
    single-account run.
    """
    return subprocess.Popen(
        [str(argument) for argument in arguments],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True, env=environment,
    )


# --- clear ------------------------------------------------------------------------------------


def clear_records(log):
    """Read the append-only log as the source of every child path and window id."""
    try:
        lines = pathlib.Path(log).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise ClearError(f"the machine log {log} is unreadable: {error}") from error
    records = []
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError as error:
            raise ClearError(
                f"the machine log {log} line {line_number} is not JSON: {error}"
            ) from error
        if not isinstance(record, dict):
            raise ClearError(f"the machine log {log} line {line_number} is not an object")
        records.append(record)
    return records


def clear_run_data(run_dir):
    """Load the run table and log, keeping all paths at the recorded boundary."""
    table_path = run_dir / TABLE_NAME
    log_path = run_dir / LOG_NAME
    try:
        plan = run_plan.load(table_path)
    except run_plan.RunPlanError as error:
        raise ClearError(str(error)) from error
    if not plan.run.return_branch:
        raise ClearError(f"the wave table {table_path} carries no run.return_branch")
    return plan, plan.run, clear_records(log_path), log_path


def clear_tickets(plan, records):
    """Return ticket ids in table order, followed by any recorded launch-only ids."""
    identifiers = [ticket.id for ticket in plan.tickets]
    for record in records:
        if record.get("event") != "launch" or record.get("ticket") is None:
            continue
        identifier = str(record["ticket"])
        if identifier not in identifiers:
            identifiers.append(identifier)
    return identifiers


def clear_launches(records):
    """The last launch per ticket whose paths and ids are authorized for this clear."""
    launches = {}
    for record in records:
        if record.get("event") != "launch":
            continue
        missing = [key for key in ("ticket", "branch", "worktree") if not record.get(key)]
        if missing:
            raise ClearError(
                "a launch record lacks " + ", ".join(missing) + "; refusing to guess an artefact"
            )
        launches[str(record["ticket"])] = record
    return list(launches.values())


def clear_codex_state_files(run, launches):
    """The state files named by recorded Codex ticket ids, never a directory glob."""
    state_dir = pathlib.Path(run.codex.state_dir) if run.codex else None
    if state_dir is None:
        return []
    state_files = []
    for launch in launches:
        if launch.get("executor") != CODEX:
            continue
        state_file = state_dir / f"{launch['ticket']}.json"
        if state_file not in state_files:
            state_files.append(state_file)
    return state_files


def clear_git(repo, *arguments):
    """Run one git operation and return stdout, or stop before the next destructive operation."""
    result = git(repo, *arguments)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ClearError(f"git {' '.join(arguments)} failed: {detail}")
    return result.stdout.strip()


def clear_command(arguments, label):
    """Run an existing cleanup interface without exposing its output to the operator's inventory."""
    result = subprocess.run(arguments, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ClearError(f"{label} failed: {detail}")
    return result.stdout


def clear_status(worktree):
    if not worktree.exists():
        return None
    result = git(worktree, "status", "--short")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ClearError(f"the worktree {worktree} could not be inventoried: {detail}")
    return result.stdout.splitlines()


def clear_unmerged(repo, integration_branch, branch):
    branch_ref = f"refs/heads/{branch}"
    if git(repo, "show-ref", "--verify", "--quiet", branch_ref).returncode != 0:
        return None
    integration_ref = f"refs/heads/{integration_branch}"
    if git(repo, "show-ref", "--verify", "--quiet", integration_ref).returncode != 0:
        raise ClearError(
            f"the integration branch {integration_branch} could not be inventoried: it is gone"
        )
    result = git(repo, "rev-list", "--oneline", f"{integration_branch}..{branch}")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ClearError(f"the branch {branch} could not be inventoried: {detail}")
    return result.stdout.splitlines()


def clear_dashboard_window(path):
    """Read the recorded dashboard id once, treating an absent record as already clear."""
    if not path.exists():
        return None
    try:
        window = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as error:
        raise ClearError(f"the dashboard window record is unreadable: {error}") from error
    return window or None


def clear_inventory(run_dir, plan, run, records, log_path):
    """Render every recorded ticket artefact and the exact uncommitted/unmerged work."""
    repo = pathlib.Path(run.repo_root)
    launches = clear_launches(records)
    by_ticket = {}
    ticket_paths = {}
    for ticket in plan.tickets:
        ticket_paths[ticket.id] = ticket.path
    for launch in launches:
        by_ticket.setdefault(str(launch["ticket"]), []).append(launch)
    rows = []
    lines = [
        f"run: {run_dir}",
        f"integration branch: {run.integration_branch}",
        f"return branch: {run.return_branch}",
        f"machine log: {log_path}",
    ]
    for ticket in clear_tickets(plan, records):
        path = ticket_paths.get(ticket)
        lines.append(f"ticket {ticket}" + (f" ({path})" if path else "") + ":")
        ticket_launches = by_ticket.get(ticket, [])
        if not ticket_launches:
            lines.append("  no recorded launch artefacts")
            continue
        for launch in ticket_launches:
            worktree = pathlib.Path(launch["worktree"])
            branch = str(launch["branch"])
            status = clear_status(worktree)
            unmerged = clear_unmerged(repo, run.integration_branch, branch)
            row = {
                "ticket": ticket,
                "executor": launch.get("executor"),
                "worktree": worktree,
                "branch": branch,
                "window": launch.get("window"),
                "status": status,
                "unmerged": unmerged,
            }
            rows.append(row)
            lines.append(f"  window: {launch.get('window') or 'none'}")
            lines.append(
                f"  worktree: {worktree}" + (" (already gone)" if status is None else "")
            )
            if unmerged is None:
                lines.append(f"  branch: {branch} (already gone)")
            else:
                disposition = (
                    "unmerged; force-delete with -D"
                    if unmerged else "merged; delete with -d"
                )
                lines.append(f"  branch: {branch} ({disposition})")
            if status is None:
                lines.append("  uncommitted files: already gone")
            elif status:
                lines.append("  uncommitted files:")
                lines.extend(f"    {item}" for item in status)
            else:
                lines.append("  uncommitted files: none")
            if unmerged is None:
                lines.append("  unmerged commits: already gone")
            elif unmerged:
                lines.append("  unmerged commits:")
                lines.extend(f"    {item}" for item in unmerged)
            else:
                lines.append("  unmerged commits: none")

    for state_file in clear_codex_state_files(run, launches):
        lines.append(
            f"Codex state: {state_file}"
            + (" (already gone)" if not state_file.exists() else "")
        )
    dashboard_path = run_dir / "dashboard-window"
    dashboard_window = clear_dashboard_window(dashboard_path)
    if dashboard_window:
        lines.append(f"dashboard window: {dashboard_window}")
    lines.append(f"report: {report_path(run_dir, run)} (kept)")
    return lines, ClearPlan(rows, launches, dashboard_window)


def clear_kill_window(window):
    """Kill one recorded tmux id; an already-stopped recorded window is already clear."""
    result = subprocess.run(
        ["tmux", "kill-window", "-t", str(window)], capture_output=True, text=True
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        lower_detail = detail.lower()
        already_gone = (
            "can't find window" in lower_detail
            or "no server running" in lower_detail
            or "error connecting to" in lower_detail
            or "failed to connect" in lower_detail
        )
        if not already_gone:
            raise ClearError(f"tmux window {window} could not be killed: {detail}")


def clear_unlock_worktree(repo, worktree):
    """Unlock one recorded worktree, tolerating the normal already-unlocked state."""
    if not worktree.exists():
        return
    result = git(repo, "worktree", "unlock", "--", str(worktree))
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        if "not locked" not in detail.lower():
            raise ClearError(f"the worktree {worktree} could not be unlocked: {detail}")


def clear_remove_worktree(repo, worktree):
    """Remove one recorded worktree, tolerating an artefact already removed by an earlier try."""
    result = git(repo, "worktree", "remove", "--force", "--", str(worktree))
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        lower_detail = detail.lower()
        already_gone = (
            "is not a working tree" in lower_detail
            or "no such file or directory" in lower_detail
        )
        if not already_gone:
            raise ClearError(f"the worktree {worktree} could not be removed: {detail}")


def clear_stop_codex_sessions(run, launches):
    """Stop the Codex session behind each recorded launch through the bridge that started it."""
    bridge = run.codex.bridge if run.codex else None
    for state_file in clear_codex_state_files(run, launches):
        if not state_file.exists():
            continue
        if not bridge:
            raise ClearError(f"the Codex launch for {state_file} carries no bridge")
        clear_command(
            [sys.executable, str(bridge), "stop", "--state-file", str(state_file)],
            f"Codex session {state_file}",
        )


def clear_kill_windows(plan):
    """Kill each window the plan holds once, the dashboard's among them."""
    windows = []
    for launch in plan.launches:
        window = launch.get("window")
        if window and str(window) not in windows:
            windows.append(str(window))
    if plan.dashboard_window and plan.dashboard_window not in windows:
        windows.append(plan.dashboard_window)
    for window in windows:
        clear_kill_window(window)


def clear_worktrees_and_branches(repo, rows, trust_inventory=False):
    """Remove each planned worktree and delete its branch, once per recorded artefact.

    `git branch -d` asks whether the branch is merged into HEAD, and every caller here has already
    asked the better question: the inventory compared it against the run's own integration branch.
    The two agree while HEAD is that branch, which is where a run's clear and its epilogue both
    stand. A caller standing anywhere else — the start that sweeps a dead run, whose HEAD is its
    own repo's — sets `trust_inventory`, and a row the inventory found merged is deleted on that
    answer instead of on HEAD's, which is about somebody else's run entirely.
    """
    unique_rows = []
    seen_rows = set()
    for row in rows:
        identity = (str(row["worktree"]), row["branch"])
        if identity not in seen_rows:
            seen_rows.add(identity)
            unique_rows.append(row)
    for row in unique_rows:
        clear_unlock_worktree(repo, row["worktree"])
        clear_remove_worktree(repo, row["worktree"])

    for row in unique_rows:
        if row["unmerged"] is None:
            continue
        flag = "-D" if row["unmerged"] or trust_inventory else "-d"
        clear_git(repo, "branch", flag, "--", row["branch"])


def clear_actions(run_dir, run, log_path, plan):
    """Apply the clearing steps using only the paths and ids in the inventory."""
    repo = pathlib.Path(run.repo_root)
    integration_branch = run.integration_branch
    if git(repo, "show-ref", "--verify", f"refs/heads/{integration_branch}").returncode == 0:
        clear_git(repo, "switch", "--", integration_branch)

    state_dir = pathlib.Path(run.codex.state_dir) if run.codex else None
    clear_stop_codex_sessions(run, plan.launches)
    clear_kill_windows(plan)
    clear_worktrees_and_branches(repo, plan.rows)

    return_branch = run.return_branch
    if git(repo, "show-ref", "--verify", f"refs/heads/{return_branch}").returncode == 0:
        clear_git(repo, "switch", "--", return_branch)
    else:
        clear_git(repo, "switch", "--detach", "--", return_branch)
    if git(repo, "show-ref", "--verify", f"refs/heads/{integration_branch}").returncode == 0:
        clear_git(repo, "branch", "-D", "--", integration_branch)

    if state_dir is not None and state_dir.exists():
        if state_dir.is_symlink():
            state_dir.unlink()
        else:
            shutil.rmtree(state_dir)

    machine_log = run_dir / MACHINE_LOG.name
    if not machine_log.exists():
        raise ClearError(f"the run carries no durable machine log at {machine_log}")
    clear_command(
        [
            sys.executable, str(machine_log), "--log", str(log_path), "uninstall",
            "--settings", str(repo / SETTINGS_PATH),
        ],
        "machine-log uninstall",
    )


def run_clear(args):
    """Inventory a run, ask in the terminal, and clear only after an affirmative answer."""
    run_dir = pathlib.Path(args.run_dir).resolve()
    run_plan_value, run, records, log_path = clear_run_data(run_dir)
    lines, plan = clear_inventory(run_dir, run_plan_value, run, records, log_path)
    print("\n".join(lines))
    try:
        answer = input("Clear this run? [y/N] ")
    except EOFError:
        answer = ""
    if answer.strip().lower() not in ("y", "yes"):
        print("clear cancelled")
        return 0
    clear_actions(run_dir, run, log_path, plan)
    print(f"run cleared; durable record kept at {run_dir}")
    return 0


# --- the run-end epilogue ----------------------------------------------------------------------


def epilogue_plan(plan, landed):
    """The clear plan narrowed to the tickets whose branches reached the integration branch.

    Everything else a run recorded — a parked child's worktree, a failed child's branch, the window
    either is still sitting in — is work nobody has merged, and the epilogue is not the place a run
    destroys work. The dashboard belongs to the run rather than to a ticket, so it is carried
    through whether or not anything landed.
    """
    return ClearPlan(
        [row for row in plan.rows if str(row["ticket"]) in landed],
        [launch for launch in plan.launches if str(launch["ticket"]) in landed],
        plan.dashboard_window,
    )


def epilogue(run_dir, run, run_plan_value, records, log_path):
    """Clear a completed run's landed artefacts, without a question and without a token.

    The operator's `clear` asks first because it is aimed at a run in any state; this path runs
    itself, over exactly the tickets the log says landed. The coordinator's own window, the
    integration branch the run exists to hand over, and the durable run directory are not the
    plan's to touch, so none of them is in it.
    """
    _, plan = clear_inventory(run_dir, run_plan_value, run, records, log_path)
    projection = machine_log.project(records)
    landed = {ticket for ticket, facts in projection.tickets.items() if facts.merge_landed}
    plan = epilogue_plan(plan, landed)
    clear_stop_codex_sessions(run, plan.launches)
    clear_kill_windows(plan)
    clear_worktrees_and_branches(pathlib.Path(run.repo_root), plan.rows)


def disarm(monitors):
    """Take down every armed monitor; returns nothing.

    Called on every way out of the loop. A monitor left polling after the driver has exited holds
    the run's log open, re-reads an agents list nobody is waiting on, and would report the next
    driver's children to a pipe that has gone.
    """
    for monitor in monitors:
        process = monitor["process"]
        if process.poll() is not None:
            continue
        with contextlib.suppress(OSError):
            process.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=MONITOR_STOP_SECONDS)
        if process.poll() is None:
            with contextlib.suppress(OSError):
                process.kill()


# --- the residue an abandoned run leaves in its repo --------------------------------------------

# A run that reaches `finish()` uninstalls its hooks, removes its pin and clears what landed. A run
# that never gets there — an unresumed judgment-needed or driver-error pause, a driver killed
# outright — leaves all three behind, and nothing in the design was left to notice. The pin is
# swept by the statusline tick (#87); the hook and the landed worktrees are swept here, by the next
# driver that comes to work in the same repo, because a hook lives in that repo's own settings and
# only a driver about to work there has business touching it.


def crew_hook_log(command):
    """The run log this registered command writes for, or None where it is not a crew hook at all.

    The log alone does not identify one. `machine_log` reads a command for the `--log` it carries
    because every command it is ever asked about is one it installed itself; this sweep reads
    commands nobody here installed, and an unrelated hook that happens to take a `--log` and a
    `--role` would be uninstalled as a dead run's. So the script the interpreter runs — the word
    after it, which is where every command `machine_log` writes carries it — must be the machine
    log's own, and that is what makes the command this project's to remove. The position matters:
    a foreign command whose `--log` merely points at a file called `machine_log.py` is somebody
    else's hook, and reading the name anywhere in the words would have claimed it.
    """
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    if len(words) < 2 or pathlib.PurePath(words[1]).name != machine_log.SCRIPT_NAME:
        return None
    return machine_log.command_log(command)


def sweep_hook_logs(settings_path):
    """Every run log this repo's settings registers a crew hook for, and what could not be read.

    Returns the logs in registration order and the problems to warn about. A settings file this
    driver cannot read as one is a file it must not rewrite, so nothing is swept out of it — but
    that is said out loud rather than passed over in silence, because a dead run's hook living on
    in an unreadable settings file is exactly what this sweep exists to catch.
    """
    try:
        text = settings_path.read_text(encoding="utf-8") if settings_path.exists() else ""
        settings = json.loads(text.strip() or "{}")
    except (OSError, UnicodeDecodeError, ValueError) as error:
        return [], [f"{settings_path}: no hook could be read from it: {error}"]
    if not machine_log.settings_shape_is_sound(settings):
        return [], [
            f"{settings_path}: no hook could be read from it: it is not a settings document"
        ]
    logs = []
    for block in machine_log.message_blocks(settings):
        for hook in block.get("hooks", []) or []:
            if not isinstance(hook, dict):
                continue
            log = crew_hook_log(str(hook.get("command", "")))
            if log and log not in logs:
                logs.append(log)
    return logs, []


def uninstall_hook(log, settings):
    """Take every hook writing `log` out of that settings file, through the log's own operation."""
    run_command(
        [sys.executable, MACHINE_LOG, "--log", log, "uninstall", "--settings", settings],
        f"the hook writing {log} could not be uninstalled from {settings}",
    )


def sweep_landed(run_dir):
    """Clear the landed worktrees and branches of the run at `run_dir`; raises ClearError.

    The dead run's own epilogue, run late by somebody else: the same inventory, narrowed by the
    same `epilogue_plan` to the tickets its log says reached its integration branch. A parked or
    failed child's worktree is work nobody merged, and no automatic path deletes that. Windows and
    Codex sessions are the epilogue's and not this sweep's: both belong to a session that died with
    the run, while a worktree is disk that outlives every session there ever was.
    """
    run_plan_value, run, records, log_path = clear_run_data(run_dir)
    _, plan = clear_inventory(run_dir, run_plan_value, run, records, log_path)
    projection = machine_log.project(records)
    landed = {ticket for ticket, facts in projection.tickets.items() if facts.merge_landed}
    plan = epilogue_plan(plan, landed)
    clear_worktrees_and_branches(pathlib.Path(run.repo_root), plan.rows, trust_inventory=True)


def sweep_dead_runs(repo, keep_run_dir):
    """Finish the cleanup every run abandoned in this repo never reached; returns its warnings.

    A run is dead when neither of the two processes it can be alive through is: the coordinator it
    recorded, and the driver it named in its own run directory. The second is why this asks twice
    — a driver detached from its coordinator's session outlives it by design (#103), and sweeping
    a run whose driver is still merging its waves would take that run's worktrees out from under
    it. Every problem either step meets is returned as a warning rather than raised: the run about
    to start is not the place to fail over a run that ended weeks ago.

    For a valid dead plan the landed artefacts go first, and the hook goes whether or not they went:
    a hook nobody reads costs a python interpreter on every message sent in this repo, which is the
    burn this sweep exists to end. A rejected plan is touched nowhere, because without its trusted
    coordinator and repository facts the sweep cannot prove the run is dead or belongs here. Only
    a dead run that recorded this repository has its artefacts cleared — the settings file is this
    repo's to edit, while another repository's worktrees and branches are not this driver's to
    delete, however dead the run that made them.

    The run this start is about is passed as `keep_run_dir` and is never swept. A driver adopting
    an interrupted run meets its own hook here, recorded under the coordinator that abandoned it —
    which is exactly the pid this judgment would call dead.

    Every failure becomes a warning rather than stopping the new run. A plan defect is still a
    strict rejection: the warning carries the Run plan error, and no fallback interpretation is
    used to choose cleanup actions.
    """
    settings_path = repo / SETTINGS_PATH
    keep = pathlib.Path(keep_run_dir).resolve()
    repo_key = pathlib.Path(repo).resolve()
    logs, warnings = sweep_hook_logs(settings_path)
    for log in logs:
        run_dir = pathlib.Path(log).parent
        if run_dir.resolve() == keep:
            continue
        if monitor.live_driver(run_dir):
            continue
        try:
            old_plan = run_plan.load(run_dir / TABLE_NAME)
        except run_plan.RunPlanError as error:
            warnings.append(
                f"{run_dir}: its plan was rejected, so none of its artefacts or hook were"
                f" cleared: {error}"
            )
            continue
        pid = old_plan.run.coordinator_pid
        if isinstance(pid, int) and not isinstance(pid, bool) and monitor.alive(pid):
            continue
        repo_root = pathlib.Path(old_plan.run.repo_root).resolve()
        if repo_root != repo_key:
            warnings.append(
                f"{run_dir}: its landed artefacts were left alone: it records the repository"
                f" {repo_root}, which is not the one being started in"
            )
        else:
            try:
                sweep_landed(run_dir)
            except Exception as error:  # noqa: BLE001 - housekeeping never stops a run
                warnings.append(f"{run_dir}: its landed artefacts were not cleared: {error}")
        try:
            uninstall_hook(log, settings_path)
        except Exception as error:  # noqa: BLE001 - housekeeping never stops a run
            warnings.append(f"{run_dir}: its hook was not uninstalled: {error}")
    return warnings


# --- start ------------------------------------------------------------------------------------


def preflight(repo, feature_dir, base_branch, upstream, run):
    """The five read-only checks and the run's two configured values, every problem of every one."""
    problems = dirty_tree_problems(repo)
    problems += base_branch_problems(repo, base_branch, upstream)
    try:
        plan = run_plan.build(feature_dir, run)
    except run_plan.RunPlanError as error:
        # A plan that does not build answers no question about what this run reviews, so the last
        # check is not asked here rather than guessed at.
        return problems + list(error.problems)
    problems += review_command_problems(plan)
    return problems


def resolved(path):
    """That path, absolute, or None where none was given."""
    return pathlib.Path(path).resolve() if path else None


def stop_for_preflight(args, feature_dir, problems):
    """Show and emit one preflight failure; return the exit code a stopped start earns."""
    try:
        show_notice(args.tmux_session, problems)
    except DriverError as error:
        raise DriverError(
            f"preflight stopped this run on {len(problems)} problems and none of them could"
            f" be shown to the operator: {error}",
            pointer=str(feature_dir),
        ) from error
    snapshot(
        PREFLIGHT_FAILED, pointer=str(feature_dir),
        count=len(problems), surface=NOTICE_WINDOW_NAME,
    )
    return PREFLIGHT_EXIT


def run_start(args):
    feature_dir = pathlib.Path(args.feature_dir).resolve()
    if not feature_dir.is_dir():
        raise DriverError(f"{feature_dir} is not a feature directory", pointer=str(feature_dir))
    run_dir = feature_dir / RUN_DIR_NAME
    # Taken up before the first step that can fail, so that every way this start can end reaches
    # the coordinator's waiter: a wake is only written into a run this process has in hand, and a
    # repository root or a tmux session that cannot be resolved is as much a wake as any other.
    take_up_run(run_dir)
    # Absolute at the boundary, whatever spelling the caller used: every path recorded here is read
    # again in a child's own worktree, where a relative one names another file or none.
    args.spec = resolved(args.spec)
    args.codex_bridge = resolved(args.codex_bridge)
    repo = repository_root(feature_dir, args.repo_root)
    args.tmux_session = tmux_session(args.tmux_session)
    clear_notice(args.tmux_session)

    # Before anything else this driver does in this repo, and before it can matter whether the run
    # is a fresh one or an adoption: what the runs abandoned here never cleaned up.
    for warning in sweep_dead_runs(repo, run_dir):
        print(f"crew sweep: {warning}", file=sys.stderr, flush=True)
    table_path = run_dir / TABLE_NAME
    if table_path.exists():
        return adopt(args, repo, run_dir, table_path)

    base_branch = args.base_branch or default_base_branch(repo)
    # The table preflight validates: everything the run section carries but the commit the run has
    # not cut yet, which no routing rule reads.
    head = git_output(repo, "rev-parse", "HEAD")
    config = project_config(repo)
    candidate = run_section(args, repo, feature_dir, run_dir, base_branch, head, head, config)
    upstream = upstream_state(repo, base_branch) if base_branch else (UPSTREAM_ABSENT, "")
    problems = preflight(repo, feature_dir, base_branch, upstream, candidate)
    gate = None
    if not problems:
        gate, problems = configured_base_gate(config)

    if problems:
        return stop_for_preflight(args, feature_dir, problems)

    integration_branch = candidate["integration_branch"]
    return_branch, base_commit, gate_problem = prepare_branches(
        repo, base_branch, integration_branch,
        pull=upstream[0] == UPSTREAM_PRESENT,
        gate=gate,
    )
    if gate_problem is not None:
        return stop_for_preflight(args, feature_dir, [gate_problem])
    run_dir.mkdir(parents=True, exist_ok=True)
    take_up_run(run_dir)
    launch_dir = run_dir / LAUNCH_DIR_NAME
    log = run_dir / LOG_NAME
    run = run_section(
        args, repo, feature_dir, run_dir, base_branch, return_branch, base_commit, config
    )
    try:
        plan = run_plan.build(feature_dir, run)
        plan.write(table_path)
    except run_plan.RunPlanError as error:
        raise DriverError(str(error), pointer=str(feature_dir)) from error

    record_base_gate(log, gate)
    install_hook(
        log, repo / SETTINGS_PATH, "coordinator", session_id=run["coordinator_session"]
    )
    dispatch_wave(table_path, log, launch_dir, run_dir)
    children = launched_children(log)
    for child in children:
        install_hook(
            log, pathlib.Path(child["worktree"]) / SETTINGS_PATH, "child", child["ticket"]
        )
    print(f"crew wave 1 launched, run directory {run_dir}", flush=True)
    return wave_loop(args, repo, run_dir, table_path)


def adopt(args, repo, run_dir, table_path):
    """Take over the unfinished run this feature already carries; returns the exit code it earns.

    Starting and resuming are the same action, so this is what `start` does whenever the feature
    holds a run directory: nothing is cut, dispatched or approved again. The children keep the
    worktrees, branches and windows the interrupted run left them, and what they lost — the process
    watching them — is what this puts back.

    One thing stands between the log and the loop: a run the monitor's `over` calls finished has
    nothing to adopt — an `advance` decision of `complete`, or the `stopped` this loop appends
    when a chain halted for good. A wave that escalated or was interrupted is neither: it is
    re-run by the run that adopts it, which is how such a run carries on at all. The question is
    asked of the monitor so the loop and the operator's surfaces can never disagree about it. A
    finished run earns saying so and pointing at its report, and nothing else a re-typed command
    may do to it. Everything an unfinished one needs is the loop's own — what it puts back before
    it starts polling is `Loop.adopt`, and what it reads from the log is what it would have read
    had it never stopped.
    """
    log = run_dir / LOG_NAME
    records = machine_log.read_records(log)
    projection = machine_log.project(records)
    if projection.ended:
        try:
            plan = run_plan.load(table_path)
        except run_plan.RunPlanError as error:
            raise DriverError(str(error), pointer=str(table_path)) from error
        # The same snapshot the run's own ending emitted, because a coordinator reading it has no
        # way to tell — and no reason to care — whether this run finished a moment ago or last week.
        report = report_path(run_dir, plan.run)
        snapshot(RUN_COMPLETE, pointer=str(report), report=str(report))
        return 0
    print(f"crew adopted wave {projection.current_wave}, run directory {run_dir}", flush=True)
    return wave_loop(args, repo, run_dir, table_path, adopting=True)


def dispatch_wave(table, log, launch_dir, run_dir):
    """Dispatch wave 1 through the renderer, which launches, verifies and logs every child."""
    result = subprocess.run(
        [
            sys.executable, str(DISPATCH), "dispatch",
            "--table", str(table), "--wave", "1",
            "--out-dir", str(launch_dir), "--log", str(log),
        ],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        return
    failures = [
        line for line in result.stdout.splitlines() if " FAILED " in line
    ] or [(result.stderr or result.stdout).strip()]
    ticket = failures[0].split(None, 1)[0] if failures[0] else None
    raise DriverError(
        "wave 1 did not launch: " + "; ".join(failures),
        ticket=ticket if ticket and ticket.isdigit() else None,
        pointer=str(run_dir / LOG_NAME),
    )


def start_dashboard(args, repo, run_dir):
    """Point the operator's dashboard at the run, on whichever surface the repo chose.

    Idempotent, and run by every driver that takes the run up rather than only by the one that
    started it: an adopted or resumed run has a live dashboard window and a pin naming it again,
    which is what makes a recovered run indistinguishable from one that was never interrupted.
    """
    arguments = [
        sys.executable, MONITOR, "window",
        "--run-dir", run_dir, "--session", args.tmux_session,
        "--coordinator-pid", args.coordinator_pid,
    ]
    config = repo / CONFIG_NAME
    if config.exists():
        arguments += ["--config", config]
    run_command(arguments, "the dashboard could not be started", pointer=str(run_dir))


# --- what the run's log says ---------------------------------------------------------------

# The loop keeps no state of its own between polls, and none at all between runs of this process:
# every count it acts on — how many times a ticket has been re-asked, whether a nudge has gone out,
# how often a conflict has been bounced back — is read out of the machine log each time it is
# needed. That is what makes resuming after a ruling the same code path as carrying on, and what
# makes a driver that died mid-wave recoverable by starting another one.
#
# Re-reading the whole log on every poll — here, in the dashboard and in the statusline tick — is
# accepted rather than fixed (#89), so that it is not re-litigated on suspicion: a run's log
# measured 122 KB at millisecond-scale parse times, and it is sealed the moment the run ends. The
# revisit threshold is one number: a single run's log past ~5 MB, where the parse stops being free
# and an incremental reader would start to earn its own complexity.


# --- the report and wind-down -----------------------------------------------------------------


REPORT_TIMESTAMP_FORMAT = machine_log.TIMESTAMP_FORMAT
REPORT_OUTCOMES = (COMPLETED, FAILED, PARKED, BLOCKED)


def report_ticket_sort_key(ticket):
    """Sort numeric ticket ids by number, retaining a stable fallback for non-numeric ids."""
    value = str(ticket)
    return (0, int(value)) if value.isdigit() else (1, value)


def report_tickets(plan):
    """Every ticket in the approved table, keyed by the id the log records."""
    return {ticket.id: ticket for ticket in plan.tickets}


def report_settlements(records, ticket):
    """The ticket's receipt/outcome events, in log order."""
    return [
        record for record in records
        if str(record.get("ticket")) == ticket
        and record.get("event") in ("receipt", "outcome")
    ]


def report_outcome(projection, ticket, launched):
    """The one report outcome a ticket settled into, or a clear error if the log has a hole."""
    state = projection.ticket(ticket).settlement_state
    if state in REPORT_OUTCOMES:
        return state
    state = "launched" if launched else "not launched"
    raise DriverError(f"ticket {ticket} has no report outcome in the machine log ({state})")


def report_received(records, ticket):
    """The last receipt or outcome, which closes the ticket's full duration span."""
    settlements = report_settlements(records, ticket)
    return settlements[-1] if settlements else None


def report_moment(value, label):
    """Parse the machine log's UTC timestamp, refusing arithmetic on an invented value."""
    try:
        return datetime.datetime.strptime(value, REPORT_TIMESTAMP_FORMAT)
    except (TypeError, ValueError) as error:
        raise DriverError(f"{label} is not {REPORT_TIMESTAMP_FORMAT}: {value!r}") from error


def report_duration(launched, received, ticket):
    """The elapsed wall time between a ticket's launch and the event the run received."""
    start = report_moment(launched.get("ts"), f"ticket {ticket} launch timestamp")
    end = report_moment(received.get("ts"), f"ticket {ticket} received timestamp")
    seconds = int((end - start).total_seconds())
    if seconds < 0:
        raise DriverError(f"ticket {ticket} was received before it was launched")
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def report_terminal_details(records, ticket, outcome):
    """The receipt or outcome detail belonging to a parked or failed report row."""
    for record in reversed(report_settlements(records, ticket)):
        if record.get("event") == "outcome" and record.get("outcome") == outcome:
            return str(record.get("detail") or "")
        if record.get("event") == "receipt" and record.get("verdict") == outcome:
            return str(record.get("detail") or "")
    return ""


def report_rulings(records):
    """Every ruling for display, with wrap-up leftovers paired to their placements."""
    wrap_ups = {}
    rendered = []
    for position, record in enumerate(records):
        ticket = str(record.get("ticket") or "run")
        if record.get("event") == "escalation":
            previous = wrap_ups.pop(ticket, None)
            if previous and previous["handed_over"] is not None:
                rendered.append(previous["handed_over"])
            message = str(record.get("message") or "")
            verb, verb_line = machine_log.final_verb(message)
            words = verb_line.split() if verb == machine_log.ESCALATION_VERB else []
            if len(words) >= 4 and words[3] == "wrap-up":
                leftovers = [
                    line for line in message.splitlines()
                    if line.strip() and line != verb_line
                ]
                if not leftovers and " — " in verb_line:
                    body = re.sub(r" ts=\d+$", "", verb_line.split(" — ", 1)[1]).strip()
                    if body:
                        leftovers.append(body)
                wrap_ups[ticket] = {"leftovers": leftovers, "handed_over": None}
            continue
        if record.get("event") != "ruling":
            continue
        message = str(record.get("message") or "")
        wrap_up = wrap_ups.get(ticket)
        if wrap_up:
            leftovers = wrap_up["leftovers"]
            lines = message.splitlines()
            placements = []
            for leftover in leftovers:
                closed = {
                    f"{leftover} — this ticket",
                    f"{leftover} — dropped",
                }
                placement = next(
                    (
                        line for line in lines
                        if line in closed or line.startswith(f"{leftover} — opened ")
                    ),
                    None,
                )
                if placement is not None:
                    placements.append(placement)
            if len(placements) == len(leftovers):
                rendered.extend(
                    (position, order, ticket, placement)
                    for order, placement in enumerate(placements)
                )
                wrap_ups.pop(ticket, None)
                continue
            if message.startswith(f"{HANDED_OVER_MARKER} "):
                wrap_up["handed_over"] = (position, 0, ticket, message)
                continue
            if wrap_up["handed_over"] is not None:
                rendered.append(wrap_up["handed_over"])
            wrap_ups.pop(ticket, None)
        rendered.append((position, 0, ticket, message))
    rendered.extend(
        wrap_up["handed_over"]
        for wrap_up in wrap_ups.values()
        if wrap_up["handed_over"] is not None
    )
    return [
        (ticket, message)
        for _position, _order, ticket, message in sorted(rendered)
    ]


def report_undo_effects(records):
    """Every completed tracker outcome, whose detail carries the exact undo."""
    return [
        record for record in records
        if record.get("event") == "outcome"
        and record.get("outcome") == COMPLETED
        and isinstance(record.get("detail"), str)
    ]


def report_base_gate(records):
    """Render the last recorded base-gate decision, retaining compatibility with older runs."""
    gates = [record for record in records if record.get("event") == "base-gate"]
    if not gates:
        return "not recorded"
    gate = gates[-1]
    if gate.get("status") == "not-configured" and "argv" not in gate:
        return "none configured"
    argv = gate.get("argv")
    if (
        gate.get("status") == "passed"
        and isinstance(argv, list)
        and argv
        and all(isinstance(argument, str) and argument for argument in argv)
    ):
        return f"passed — `{shlex.join(argv)}`"
    raise DriverError("the machine log carries a contradictory base-gate record")


def render_report(run, tickets, records, cost_output):
    """Render the complete human report from the table, machine log and cost-pass output."""
    ticket_ids = sorted(tickets, key=report_ticket_sort_key)
    projection = machine_log.project(records)
    launches = {
        ticket: facts.launch for ticket, facts in projection.tickets.items()
        if facts.launch is not None
    }
    starts = {
        ticket: facts.first_launch for ticket, facts in projection.tickets.items()
        if facts.first_launch is not None
    }
    outcomes = {
        ticket: report_outcome(projection, ticket, ticket in launches)
        for ticket in ticket_ids
    }
    lines = ["# Crew run report", "", "## Outcomes", "", "| Ticket | Title | Outcome |"]
    lines.append("| --- | --- | --- |")
    for ticket in ticket_ids:
        lines.append(
            f"| {ticket} | {tickets[ticket].title} | {outcomes[ticket]} |"
        )

    lines += ["", "## Base gate", "", f"- Base gate: {report_base_gate(records)}"]

    lines += ["", "## Parked checklists", ""]
    parked = [ticket for ticket in ticket_ids if outcomes[ticket] == PARKED]
    if parked:
        lines.extend(
            f"- {ticket}: {report_terminal_details(records, ticket, PARKED)}"
            for ticket in parked
        )
    else:
        lines.append("- none recorded")

    lines += ["", "## Failed receipts and sessions", ""]
    failed = [ticket for ticket in ticket_ids if outcomes[ticket] == FAILED]
    if failed:
        lines.extend(
            f"- {ticket}: {report_terminal_details(records, ticket, FAILED)}"
            for ticket in failed
        )
    else:
        lines.append("- none recorded")

    lines += ["", "## Preserved work", ""]
    preserved = [ticket for ticket in ticket_ids if outcomes[ticket] in (PARKED, FAILED)]
    preserved_lines = [
        f"- {ticket}: worktree `{launches[ticket]['worktree']}`,"
        f" branch `{launches[ticket]['branch']}`"
        for ticket in preserved
        if ticket in launches and launches[ticket].get("worktree")
        and launches[ticket].get("branch")
    ]
    lines.extend(preserved_lines or ["- none recorded"])

    lines += ["", "## Rulings", ""]
    rulings = report_rulings(records)
    if rulings:
        for ticket, message in rulings:
            lines.append(f"- {ticket}: {message}")
    else:
        lines.append("- none recorded")

    lines += ["", "## Outside-worktree effects", ""]
    effects = report_undo_effects(records)
    if effects:
        for record in effects:
            ticket = record.get("ticket") or "run"
            value = record.get("detail") or record.get("message") or ""
            lines.append(f"- {ticket}: {value}")
    else:
        lines.append("- none recorded")

    integration = run.integration_branch
    base = run.base_branch or ""
    lines += [
        "", "## Integration branch", "",
        f"- Integration branch: `{integration}`",
        f"- Base branch: `{base}`",
        f"- Merging `{integration}` into `{base}` is the human's decision.",
    ]

    lines += [
        "", "## Durations", "",
        "| NN | Workflow | Executor | Model | Effort | Outcome | Launched | Received | Duration |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for ticket in sorted(launches, key=report_ticket_sort_key):
        launched = launches[ticket]
        started = starts.get(ticket, launched)
        received = report_received(records, ticket)
        if received is None:
            raise DriverError(f"ticket {ticket} has no received event in the machine log")
        lines.append(
            f"| {ticket} | {launched.get('workflow', '--')} | {launched.get('executor', '--')} |"
            f" {launched.get('model', '--')} | {launched.get('effort', '--')} |"
            f" {outcomes[ticket]} | {started.get('ts', '--')} | {received.get('ts', '--')} |"
            f" {report_duration(started, received, ticket)} |"
        )

    lines += [
        "", "## Cost", "", "```text", cost_output.rstrip("\n"), "```",
        "",
        "The coordinator row is a session-wide upper bound if this session did anything outside"
        " this run.",
        "",
    ]
    return "\n".join(lines)


def report_path(run_dir, run):
    """The report's feature-level path, outside the durable run directory."""
    return pathlib.Path(run.feature_dir or run_dir.parent) / REPORT_NAME


def run_cost_pass(log, run):
    """Run the existing cost CLI and retain its exact rollup for the report."""
    session = run.coordinator_session
    if session is None:
        session = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    return run_command(
        [
            sys.executable, MONITOR, "cost", "--log", log,
            "--coordinator-session", session,
        ],
        "the run cost pass could not be completed",
        pointer=str(log),
    )


def remove_run_pin(run_dir):
    """Remove the run's pin through the monitor CLI; an absent pin is already success."""
    run_command(
        [sys.executable, MONITOR, "unpin", "--run-dir", run_dir],
        "the run pin could not be removed",
        pointer=str(run_dir),
    )


def uninstall_run_hooks(run_dir, run, records):
    """Remove this run's log hook from the coordinator and every launched child worktree."""
    settings = [pathlib.Path(run.repo_root) / SETTINGS_PATH]
    for record in records:
        worktree = record.get("worktree") if record.get("event") == "launch" else None
        if worktree:
            settings.append(pathlib.Path(worktree) / SETTINGS_PATH)
    seen = set()
    for path in settings:
        path = path.resolve()
        if path in seen:
            continue
        seen.add(path)
        run_command(
            [sys.executable, MACHINE_LOG, "--log", run_dir / LOG_NAME,
             "uninstall", "--settings", path],
            f"the run hook could not be uninstalled from {path}",
            pointer=str(path),
        )


def write_report(run_dir, run, plan, records, cost_output):
    """Write the complete report after the cost pass has appended its child rows."""
    path = report_path(run_dir, run)
    path.write_text(
        render_report(run, report_tickets(plan), records, cost_output),
        encoding="utf-8",
    )
    return path


# --- the loop's own context --------------------------------------------------------------------


class Loop:
    """One run's wave loop: the rule table, the run it applies to, and the monitors it waits on."""

    def __init__(self, args, repo, run_dir, table_path):
        self.args = args
        self.repo = repo
        self.run_dir = run_dir
        self.log = run_dir / LOG_NAME
        self.table_path = table_path
        try:
            self.plan = run_plan.load(table_path)
        except run_plan.RunPlanError as error:
            raise DriverError(str(error), pointer=str(table_path)) from error
        self.run = self.plan.run
        self.monitors = []

    # --- what it reads --------------------------------------------------------------------

    def records(self):
        return machine_log.read_records(self.log)

    def tickets_of(self, wave):
        try:
            return self.plan.wave(wave).tickets
        except run_plan.RunPlanError as error:
            raise DriverError(str(error), pointer=str(self.table_path)) from error

    def live(self, wave, projection):
        """The tickets of that wave the run is still waiting on."""
        return [
            ticket.id for ticket in self.tickets_of(wave)
            if (
                projection.ticket(ticket.id).settlement_state == machine_log.LIVE
                or projection.ticket(ticket.id).awaiting_receipt
            )
        ]

    # --- what it says ---------------------------------------------------------------------

    def receipt_log_command(self, ticket):
        """Return the command a Claude child uses to record one receipt in this run's own log."""
        return shlex.join(str(argument) for argument in (
            "python3", self.run_dir / MACHINE_LOG.name, "--log", self.log, "message",
            "--role", CHILD_ROLE, "--ticket", ticket, "--message", "<receipt>",
        ))

    def receipt_direction(self, ticket, launch, codex, claude):
        """Return one receipt direction rendered for the child's executor lane."""
        if lane_of(launch) == CODEX:
            return codex
        return (
            "Record the outcome in the run's machine log with:\n\n"
            f"{self.receipt_log_command(ticket)}\n\n{claude}"
        )

    def settle(self, ticket, verdict, detail, sha=None):
        """Record a ticket's verdict through the log's own writer; returns nothing.

        The driver writes every parked and failed receipt the run earns. They used to be the
        coordinator's to type, and a wave settles on what the log holds.
        """
        command = [
            sys.executable, MACHINE_LOG, "--log", self.log, "receipt",
            "--ticket", ticket, "--verdict", verdict, "--detail", detail,
        ]
        if sha is not None:
            command.extend(("--sha", sha))
        run_command(
            command,
            f"the {verdict} receipt for {ticket} could not be recorded",
            ticket=ticket, pointer=str(self.log),
        )

    def deliver(self, ticket, launch, text=None, keys=None):
        """Say one thing to a child on its own channel, and record it; returns nothing.

        A Codex child is reached through the bridge, which logs the prompt as it sends it. A Claude
        child is reached through its tmux pane, which is the only channel a script has to it — the
        cross-session message tool belongs to a model — and keys pass no hook, so the instruction
        is written into the log here rather than by being sent. Claude keys are sent one at a time;
        text is typed literally line by line, with S-Enter between lines and Enter at the end. A
        text instruction is recorded only after the cursor line confirms it left the composer; one
        Enter is retried when it did not. The ruling records the keys and text joined by a space.
        """
        if launch.get("executor") == CODEX:
            if keys:
                raise Unreachable(
                    f"{ticket} is a Codex child and cannot receive tmux key answers",
                    ticket=ticket, pointer=str(self.log),
                )
            if text is None:
                raise DriverError(
                    f"{ticket} needs text to receive an answer through the Codex bridge",
                    ticket=ticket, pointer=str(self.log),
                )
            if self.run.codex is None:
                raise DriverError(
                    f"{ticket} is a Codex child but the Run plan carries no Codex bridge",
                    ticket=ticket, pointer=str(self.table_path),
                )
            run_command(
                [
                    sys.executable, self.run.codex.bridge, "send",
                    "--state-file", self.run_dir / CODEX_DIR_NAME / f"{ticket}.json",
                    "--machine-log", self.log, "--ticket", ticket, "--prompt", text,
                ],
                f"the instruction for {ticket} could not be sent through the bridge",
                ticket=ticket, pointer=str(self.log),
            )
            return
        window = launch.get("window")
        if not window:
            raise Unreachable(
                f"{ticket} has no window to reach its child through, so nothing can be asked of it",
                ticket=ticket, pointer=str(self.log),
            )
        # Only the channel to the child is an Unreachable: what happens after the keys land — the
        # log this writes the instruction into — is the run's own record, and a failure there is a
        # driver error for whoever asked for the instruction.
        try:
            for key in keys or ():
                tmux(
                    ["send-keys", "-t", window, key],
                    f"{ticket} could not be reached at {window}",
                )
            if text is not None:
                lines = text.split("\n")
                for index, line in enumerate(lines):
                    tmux(
                        ["send-keys", "-t", window, "-l", "--", line],
                        f"{ticket} could not be reached at {window}",
                    )
                    if index < len(lines) - 1:
                        tmux(
                            ["send-keys", "-t", window, "S-Enter"],
                            f"{ticket} could not be reached at {window}",
                        )
                for _attempt in range(2):
                    tmux(
                        ["send-keys", "-t", window, "Enter"],
                        f"{ticket} could not be reached at {window}",
                    )
                    if not composer_holds(window, text):
                        break
                else:
                    raise DriverError(
                        f"{ticket}'s instruction remained in the composer at {window}"
                    )
        except DriverError as error:
            raise Unreachable(str(error), ticket=ticket, pointer=str(self.log)) from error
        recorded = " ".join(keys or ())
        if text is not None:
            recorded = f"{recorded} {text}".strip()
        self.record_ruling(ticket, launch, recorded)

    def record_ruling(self, ticket, launch, text):
        """Put one thing the run said to a child into the log; returns nothing."""
        run_command(
            [
                sys.executable, MACHINE_LOG, "--log", self.log, "message",
                "--role", COORDINATOR_ROLE, "--ticket", ticket,
                "--to", launch.get("child") or ticket, "--message", text,
            ],
            f"what was said to {ticket} could not be recorded",
            ticket=ticket, pointer=str(self.log),
        )

    # --- the rule table, row by row ---------------------------------------------------------

    def run_witness(self, ticket, launch, message):
        """Run and record one escalation witness; returns its checked or failed document."""
        started = time.monotonic()
        witness_executor, witness_model, witness_budget_usd = run_plan.witness_routing(
            self.run.witness_model, self.run.witness_budget_usd
        )

        def failed(reason):
            return {
                "brief": "",
                "outcome": "failed",
                "reason": str(reason).strip() or "witness process failed",
                "duration_seconds": round(time.monotonic() - started, 3),
            }

        try:
            result = subprocess.run(
                [
                    sys.executable, WITNESS, "--escalation", "-",
                    "--worktree", launch["worktree"],
                    "--model", witness_model,
                    "--budget-usd", f"{witness_budget_usd:g}",
                ],
                input=message,
                capture_output=True,
                text=True,
                env=accounts.process_environment(self.plan.ticket(ticket).binding),
                # The witness owns the session timeout. One poll interval lets it shape and print
                # that failure before this outer guard treats the whole process as the overrun.
                timeout=witness_runner.DEFAULT_TIMEOUT_SECONDS + self.args.poll_seconds,
            )
        except subprocess.TimeoutExpired:
            document = failed("witness process timed out")
        except (OSError, KeyError, TypeError) as error:
            document = failed(error)
        else:
            if result.returncode:
                document = failed(
                    result.stderr.strip()
                    or result.stdout.strip()
                    or f"witness process exited {result.returncode}"
                )
            else:
                try:
                    document = json.loads(result.stdout)
                except (TypeError, json.JSONDecodeError) as error:
                    document = failed(f"witness process returned invalid JSON: {error}")
                if not isinstance(document, dict):
                    document = failed("witness process returned no result object")

        outcome = document.get("outcome")
        brief = document.get("brief")
        reason = document.get("reason")
        duration = document.get("duration_seconds")
        sound = (
            outcome in machine_log.WITNESS_OUTCOMES
            and isinstance(brief, str)
            and isinstance(reason, str)
            and isinstance(duration, (int, float))
            and not isinstance(duration, bool)
            and duration >= 0
            and (
                (outcome == "checked" and bool(brief) and not reason)
                or (outcome == "failed" and not brief and bool(reason.strip()))
            )
        )
        if not sound:
            document = failed("witness process returned a contradictory result")

        usage = document.get("usage")
        if not isinstance(usage, dict):
            usage = {}
        witness_event = [
            sys.executable, MACHINE_LOG, "--log", self.log, "witness",
            "--ticket", ticket,
            "--executor", witness_executor,
            "--model", witness_model,
            "--outcome", document["outcome"],
            "--reason", document["reason"],
            "--duration-seconds", str(document["duration_seconds"]),
        ]
        counters = {
            "input": usage.get("input_tokens"),
            "output": usage.get("output_tokens"),
            "cache-read": usage.get("cache_read_input_tokens"),
            "cache-creation": usage.get("cache_creation_input_tokens"),
        }
        if all(
            isinstance(value, int) and not isinstance(value, bool) and value >= 0
            for value in counters.values()
        ):
            for name, value in counters.items():
                witness_event.extend([f"--{name}-tokens", str(value)])
            witness_event.extend(["--total-tokens", str(sum(counters.values()))])
        run_command(
            witness_event,
            f"the witness run for {ticket} could not be recorded",
            ticket=ticket, pointer=str(self.log),
        )
        return document

    def hand_over(self, ticket, launch, message):
        """Record that this escalation is the coordinator's now, and wake it; never returns.

        Written as the escalation is handed over rather than when the run comes back, because the
        one thing the driver knows for certain is which escalation it is exiting on. Acknowledging
        at resume instead would take in every escalation standing at that moment: two children
        asking at once means one snapshot and two acknowledgements, and the ASK nobody was shown
        would be settled unread.

        The line is what the log has of the answer, too. A ruling sent through a child's tmux pane
        — the channel a permission prompt answers on — passes no hook and reaches no log, so
        without this the escalation would still be standing on the next poll and the run could
        never go on.
        """
        witness_result = self.run_witness(ticket, launch, message)
        self.record_ruling(ticket, launch, HANDED_OVER.format(
            marker=HANDED_OVER_MARKER, ticket=ticket
        ))
        raise Wake(
            JUDGMENT_NEEDED, ticket=ticket, pointer=str(self.log),
            detail=message, brief=witness_result["brief"],
            child=launch.get("child"), window=launch.get("window"),
            **(
                {"witness_reason": witness_result["reason"]}
                if witness_result["outcome"] == "failed" else {}
            ),
        )

    def rule_on_messages(self, projection):
        """Settle everything the wave's children have said and nothing has answered.

        Returns whether anything was settled, so a poll that changed the run is followed by another
        read rather than by a wait.
        """
        acted = False
        pending = [
            (ticket, facts) for ticket, facts in projection.tickets.items()
            if facts.unanswered_child_message is not None
        ]
        for ticket, facts in sorted(pending):
            record = facts.unanswered_child_message
            launch = facts.launch
            message = record.get("message") or ""
            if launch is None:
                verb, _ = machine_log.final_verb(message)
                if verb in (COMPLETE_VERB, PARKED_VERB, FAILED_VERB, ESCALATION_VERB):
                    raise DriverError(
                        f"ticket {ticket} sent {verb} with no launch record",
                        ticket=ticket, pointer=str(self.log),
                    )
                continue
            if record.get("event") == "escalation":
                self.hand_over(ticket, launch, message)
            acted = self.rule_on_receipt(ticket, launch, message, projection) or acted
        return acted

    def rule_on_receipt(self, ticket, launch, message, projection):
        """Settle one child's final word; returns whether it settled anything.

        The word is the last verb line of the body rather than its opening: a child bundles its
        receipt under the summary it wrote first as readily as it sends the line bare, and a run
        that read only the opening left a finished ticket stalling its wave.
        """
        verb, line = machine_log.final_verb(message)
        if verb == PARKED_VERB:
            self.park(ticket, launch, line)
            return True
        if verb == FAILED_VERB:
            self.settle(ticket, FAILED, line)
            return True
        if verb == COMPLETE_VERB:
            self.rule_on_completion(ticket, launch, line, projection)
            return True
        offending = machine_log.malformed_receipt(message)
        if offending is not None:
            self.rule_on_malformed(ticket, launch, offending, projection)
            return True
        # Anything else a child says is conversation, not a verdict: the run reads the grammar its
        # first turn gave it and leaves everything outside it alone. A message that reached for no
        # verb at all is conversation, and nothing is owed back for it.
        return False

    def rule_on_malformed(self, ticket, launch, line, projection):
        """Answer a near-miss receipt once; a second one settles the ticket failed. Returns nothing.

        The grammar refuses the line either way — a receipt with prose on it settles nothing, and
        that stays true. What changes is that the refusal is spoken: the child is told what it sent
        and what shape the line has to take, on the scripted rung, so the coordinator is not woken
        for mechanics (ADR-0004, ADR-0015).
        """
        if projection.ticket(ticket).instruction_count(RESEND_MARKER):
            self.settle(ticket, FAILED, f"a second receipt missed the verb grammar: {line}")
            return
        self.deliver(ticket, launch, RESEND_TEMPLATE.format(
            marker=RESEND_MARKER, ticket=ticket, line=line,
            receipt_direction=self.receipt_direction(
                ticket, launch,
                codex=(
                    "Send it in exactly the shape shown: `CREW COMPLETE <40-character sha>"
                    " ts=<unix>`, CREW PARKED <checklist path>, CREW FAILED <reason>, or"
                    f" {ASK_SHAPE}. Anything you want to say alongside it goes on the lines above."
                ),
                claude=(
                    "Replace `<receipt>` with exactly one of `CREW COMPLETE <40-character sha>"
                    " ts=<unix>`, CREW PARKED <checklist path>, or CREW FAILED <reason>. For"
                    f" {ASK_SHAPE}, use the escalation method from your first turn."
                ),
            ),
        ))

    def park(self, ticket, launch, message):
        """Record a parked receipt and put the child's worktree where the monitor reads it."""
        worktree = launch.get("worktree")
        if worktree:
            with (self.run_dir / PARKED_PATHS_NAME).open("a", encoding="utf-8") as parked:
                parked.write(f"{worktree}\n")
        self.settle(ticket, PARKED, message)

    def rule_on_completion(self, ticket, launch, message, projection):
        """Verify a claimed receipt, and settle what a second failure of it means."""
        words = message.split()
        sha = words[2] if len(words) > 2 else ""
        result = subprocess.run(
            [
                sys.executable, str(MONITOR), "verify",
                "--ticket", ticket, "--worktree", str(launch.get("worktree") or ""),
                "--sha", sha, "--base", self.base_commit(launch), "--log", str(self.log),
            ],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            # Silent success: the verifying script appended the landable receipt itself, and a
            # receipt that held is not something judgment is spent on.
            return
        problem = (result.stdout or result.stderr).strip().replace("\n", " ")
        if projection.ticket(ticket).instruction_count(RECHECK_MARKER):
            self.settle(
                ticket, FAILED, f"a second receipt did not verify: {problem}", sha=sha
            )
            return
        self.deliver(ticket, launch, RECHECK_TEMPLATE.format(
            marker=RECHECK_MARKER, ticket=ticket, sha=sha, problem=problem,
            receipt_direction=self.receipt_direction(
                ticket, launch,
                codex=(
                    "Send a new CREW COMPLETE <sha>; if it cannot be finished, send"
                    " CREW FAILED <reason>."
                ),
                claude=(
                    "Replace `<receipt>` with a new CREW COMPLETE <sha>, or CREW FAILED <reason>"
                    " if the work cannot be finished."
                ),
            ),
        ))

    def base_commit(self, launch):
        """The commit that worktree was cut from, which is what a receipt is measured against.

        Recovered from the worktree itself rather than from the wave it belongs to: a later wave is
        cut from what the wave before it landed, and the fork point of the child's branch and the
        integration branch is that commit whichever wave the ticket sits in.
        """
        worktree = launch.get("worktree")
        forked = git_output(
            worktree, "merge-base", "HEAD", self.run.integration_branch
        ) if worktree else None
        return forked or self.run.integration_base_commit

    def rule_on_statuses(self, statuses, projection):
        """Settle every non-busy child a wake monitor reported; returns whether anything changed."""
        acted = False
        for ticket, status in sorted(statuses.items()):
            facts = projection.ticket(ticket)
            launch = facts.launch
            if launch is None or status in (STATUS_BUSY, STATUS_PARKED):
                continue
            if facts.settlement_state != machine_log.LIVE and not facts.awaiting_receipt:
                continue
            if facts.unanswered_child_message is not None:
                # Its own last word is still to be settled, and that word decides the ticket; a
                # status is only ever read about a child that has said nothing.
                continue
            if status == STATUS_VANISHED:
                self.settle(ticket, FAILED, "the child's session vanished with no receipt sent")
                acted = True
            elif status == STATUS_IDLE:
                acted = self.rule_on_idle(ticket, launch, facts) or acted
            else:
                raise Wake(
                    JUDGMENT_NEEDED, ticket=ticket, pointer=str(self.log),
                    detail=f"the child is {status}, which the rule table does not settle",
                    child=launch.get("child"), window=launch.get("window"),
                )
        return acted

    def rule_on_idle(self, ticket, launch, facts):
        """One nudge per silence for an idle child; returns whether the rung acted.

        A silence the nudge never broke settles the ticket failed — per silence rather than per
        ticket, because the nudge stands only until the log shows the ticket moving again. A
        child that spoke, was reviewed, or was ruled on and went quiet later is at a new silence
        rather than at the one the old nudge failed to end.

        Nothing is asked of a child still owed a ruling. It is idle because it asked a question
        and is waiting for the answer, and a nudge there asks a child with nothing to report to
        report something — which it honestly answers `CREW PARKED`, settling a ticket whose
        question the coordinator had already answered.
        """
        if facts.awaiting_ruling:
            return False
        if facts.instruction_count(RESEND_MARKER):
            # The bounce was this ticket's one re-ask, and it named the shape of every verb a
            # child can send back. Nudging now would ask the same question a second time under
            # another marker; the ladder is one rung deep whichever way the line went wrong.
            self.settle(
                ticket, FAILED, "a bounced receipt was never resent and the child went idle"
            )
            return True
        if facts.outstanding_nudge:
            self.settle(ticket, FAILED, "a nudged child went idle again with no receipt sent")
            return True
        self.deliver(ticket, launch, NUDGE_TEMPLATE.format(
            marker=NUDGE_MARKER, ticket=ticket,
            receipt_direction=self.receipt_direction(
                ticket, launch,
                codex=(
                    "Send CREW COMPLETE <sha> if the work is committed, CREW PARKED <checklist"
                    " path> if finishing it needs a human, or CREW FAILED <reason>."
                ),
                claude=(
                    "Replace `<receipt>` with CREW COMPLETE <sha> if the work is committed,"
                    " CREW PARKED <checklist path> if finishing it needs a human, or CREW FAILED"
                    " <reason>."
                ),
            ),
        ))
        return True

    # --- the wave boundary ------------------------------------------------------------------

    def advance(self, wave):
        """Land the settled wave and launch the next; returns the wave to work, or None when done.

        Every rung below the coordinator lives inside this one call — the merge driver classifies a
        conflict and resolves a mechanical one itself, and advance launches the next wave and
        blocks what a stopped ticket stopped. What comes back is a decision, and only two of them
        are this loop's to act on.
        """
        result = subprocess.run(
            [
                sys.executable, str(ADVANCE), "advance",
                "--table", str(self.table_path), "--wave", str(wave),
                "--log", str(self.log), "--out-dir", str(self.run_dir / LAUNCH_DIR_NAME),
                "--repair-model", str(self.run.repair_model),
            ],
            capture_output=True, text=True,
        )
        if result.returncode == ADVANCE_ESCALATED_EXIT:
            return self.rule_on_halt(wave, result)
        if result.returncode == ADVANCE_INTERRUPTED_EXIT:
            raise DriverError(
                f"wave {wave} was interrupted before it advanced", pointer=str(self.log)
            )
        if result.returncode != 0:
            raise DriverError(
                f"wave {wave} could not be advanced past:"
                f" {(result.stderr or result.stdout).strip()}",
                pointer=str(self.log),
            )
        self.close_merged()
        records = self.records()
        projection = machine_log.project(records)
        following = projection.current_wave
        if following == wave:
            return None
        self.open_wave(projection)
        return following

    def rule_on_halt(self, wave, result):
        """The chain stopped: answer a semantic conflict once, and read what the rest of it means.

        Three things a wave can stop on, and only one of them is judgment. A conflict the merge
        driver called semantic is two children's designs disagreeing, and the child that lost the
        race is the one who can settle it — so it gets the instruction first, and only a second
        bounce is worth a coordinator's turn. A ticket the table already settled failed, or parked
        with descendants below it, stopped the chain by a rule that has run its course: its
        descendants are blocked, nothing downstream can start, and the run is over — which is a
        report, not a ruling. Anything else — a repair rung that failed twice, a branch that moved
        off its receipt — is the ladder exhausted (ADR-0004), and that reaches judgment.
        """
        self.close_merged()
        records = self.records()
        projection = machine_log.project(records)
        halted = [
            ticket.id for ticket in self.tickets_of(wave)
            if (
                projection.ticket(ticket.id).semantic_conflict_detail is not None
                and projection.ticket(ticket.id).launch is not None
            )
        ]
        for ticket in halted:
            facts = projection.ticket(ticket)
            launch = facts.launch
            if facts.instruction_count(MERGE_MARKER):
                raise Wake(
                    JUDGMENT_NEEDED, ticket=ticket, pointer=str(self.log),
                    detail=f"the semantic conflict on {ticket} came back a second time:"
                           f" {facts.semantic_conflict_detail}",
                    child=launch.get("child"), window=launch.get("window"),
                )
            self.deliver(ticket, launch, MERGE_TEMPLATE.format(
                marker=MERGE_MARKER, ticket=ticket, branch=self.branch_of(ticket, launch),
                integration=self.run.integration_branch,
                reason=facts.semantic_conflict_detail,
                receipt_direction=self.receipt_direction(
                    ticket, launch,
                    codex=(
                        "Send a new CREW COMPLETE <sha>. If this is a design disagreement you"
                        " cannot settle alone, send CREW ASK instead."
                    ),
                    claude=(
                        "Replace `<receipt>` with the new CREW COMPLETE <sha>. If this is a design"
                        " disagreement you cannot settle alone, use CREW ASK instead through the"
                        " escalation method from your first turn."
                    ),
                ),
            ))
        if halted:
            return wave
        unsettled = self.unsettled_halt(wave, projection)
        if unsettled:
            raise Wake(
                JUDGMENT_NEEDED, ticket=unsettled[0], pointer=str(self.log),
                detail=self.halt_detail(records, result),
            )
        # Every reason the chain stopped on is one the rule table already settled, so there is
        # nothing left for this run to launch and nothing for anyone to rule on.
        self.record_stopped(wave, projection)
        return None

    def record_stopped(self, wave, projection):
        """Put this run's ending into the log, where every surface reads it from; returns nothing.

        The `escalated` decision above it cannot carry that meaning: the same word is written when
        a wave is halted awaiting a ruling that will carry it on, and a run left with only that
        line reads as still going — a dashboard redrawing it forever, saying a ruling is owed that
        nobody is waiting for.
        """
        tickets = tuple(ticket.id for ticket in self.plan.tickets)
        launches = {
            number for number in tickets if projection.ticket(number).launch is not None
        }
        states = {number: projection.ticket(number).settlement_state for number in tickets}
        stopping_roots = {
            number for number, state in states.items() if state in (FAILED, PARKED)
        }
        stopped_descendants = set(self.plan.descendants(stopping_roots))
        for number in tickets:
            if number in launches or states[number] != machine_log.LIVE:
                continue
            if number in stopped_descendants:
                continue
            raise DriverError(
                f"stopped refused: ticket {number} is still launchable",
                ticket=number,
                pointer=str(self.log),
            )

        run_command(
            [
                sys.executable, MACHINE_LOG, "--log", self.log, "advance",
                "--wave", str(wave), "--decision", STOPPED,
                "--detail", "the chain stopped on reasons the rule table had already settled",
            ],
            f"the end of the run at wave {wave} could not be recorded",
            pointer=str(self.log),
        )

    def unsettled_halt(self, wave, projection):
        """The wave's tickets whose halt no rule of the table has already accounted for.

        A ticket settled `failed` or `parked` stopped the chain by its own recorded verdict. One
        the log says is `landable` and never landed is a merge the ladder could not finish, and
        that is nobody's but the coordinator's.
        """
        return [
            ticket.id for ticket in self.tickets_of(wave)
            if projection.ticket(ticket.id).settlement_state
            not in (COMPLETED, FAILED, PARKED)
        ]

    def halt_detail(self, records, result):
        """Why the chain stopped, as the advance decision the log holds recorded it."""
        for record in reversed(records):
            if record.get("event") == "advance" and record.get("decision") == ESCALATED:
                return str(record.get("detail") or "")
        return (result.stdout or result.stderr).strip().replace("\n", "; ")

    def branch_of(self, ticket, launch):
        """The branch that ticket's child stands on, as its launch or the renderer's naming says."""
        if launch.get("branch"):
            return launch["branch"]
        try:
            return dispatch.branch_name(self.plan.ticket(ticket))
        except run_plan.RunPlanError:
            return ticket

    def open_wave(self, projection):
        """Give the wave advance just launched everything a launched wave has; returns nothing.

        Its children get this run's escalation hook and the dashboard is pointed at the run again,
        exactly as the first wave's launch did — the command owns the run's one window, so calling
        it every wave is what brings a window the operator closed back.
        """
        for ticket, facts in sorted(projection.tickets.items()):
            launch = facts.launch
            if launch is None:
                continue
            worktree = launch.get("worktree")
            if worktree:
                install_hook(self.log, pathlib.Path(worktree) / SETTINGS_PATH, CHILD_ROLE, ticket)
        start_dashboard(self.args, self.repo, self.run_dir)

    def close_merged(self):
        """Close every merged ticket in the run's tracker, recording each undo; returns nothing."""
        records = self.records()
        projection = machine_log.project(records)
        already = {
            str(record.get("ticket")) for record in records
            if record.get("event") == "outcome" and record.get("outcome") == COMPLETED
        }
        tickets = {ticket.id: ticket for ticket in self.plan.tickets}
        for number, facts in sorted(projection.tickets.items()):
            if not facts.merge_landed:
                continue
            if number in already or number not in tickets:
                continue
            undo = close_ticket(self.run, tickets[number], number)
            run_command(
                [
                    sys.executable, MACHINE_LOG, "--log", self.log, "outcome",
                    "--ticket", number, "--outcome", COMPLETED,
                    "--detail", f"closed in the {self.run.tracker} tracker; undo: {undo}",
                ],
                f"the close of {number} could not be recorded",
                ticket=number, pointer=str(self.log),
            )

    # --- taking over a run already on the ground -----------------------------------------------

    def adopt(self):
        """Put back what the run's children lost when its driver stopped; returns nothing.

        They keep the worktrees, branches and windows they were launched with; what an interruption
        takes from them is the process that was watching, the hook that carries their word into the
        log where a worktree was prepared but never reached one, the dashboard the stopped run's
        window went with, and — where the coordinator itself restarted — the identity they answer.

        Nothing here reads a receipt or a status: what has already been settled, what is still live
        and which monitors that leaves to arm are the loop's own reading of the log, and doing any
        of it twice is what an adoption that kept its own state would risk.
        """
        records = self.records()
        projection = machine_log.project(records)
        install_hook(
            self.log, self.repo / SETTINGS_PATH, COORDINATOR_ROLE,
            session_id=self.args.coordinator_session,
        )
        for ticket, facts in sorted(projection.tickets.items()):
            launch = facts.launch
            if launch is None:
                continue
            worktree = launch.get("worktree")
            if worktree and pathlib.Path(worktree).is_dir():
                install_hook(self.log, pathlib.Path(worktree) / SETTINGS_PATH, CHILD_ROLE, ticket)
        self.reanchor(projection)
        start_dashboard(self.args, self.repo, self.run_dir)

    def reanchor(self, projection):
        """Point the run and its live children at the coordinator driving it now; returns nothing.

        A coordinator that restarted has a new pid, so every Claude child of the run is holding a
        trust anchor on a dead socket — and its refusal of the new socket's messages is that anchor
        working. The run's own record is rewritten first, including a changed session ID that
        scopes its coordinator hook. Children are re-anchored only when name or pid changes,
        because their channel is the pid-keyed socket rather than the hook's session scope. The
        identity the run record carries is the one every ticket dispatched from here on is handed.

        A Codex child is not re-anchored: its channel is a state file on disk, which the new
        coordinator opens exactly as the old one did. Neither is a child that cannot be reached —
        its window went with the session, and the loop's own rules settle it on the next poll,
        which is where a child nobody can talk to belongs. Nothing else is passed over: an
        instruction that landed and could not be recorded is a driver error, and it wakes the
        coordinator here as it does everywhere else.
        """
        name = self.args.coordinator_name
        pid = self.args.coordinator_pid
        session = self.args.coordinator_session
        if (
            self.run.coordinator_name,
            self.run.coordinator_pid,
            self.run.coordinator_session,
        ) == (name, pid, session):
            return
        channel_changed = (self.run.coordinator_name, self.run.coordinator_pid) != (name, pid)
        self.run = replace(
            self.run, coordinator_name=name, coordinator_pid=pid,
            coordinator_session=session,
        )
        self.plan = replace(self.plan, run=self.run)
        self.plan.write(self.table_path)
        # A session-only restart moves the hook scope, while the pid-keyed child socket stays put.
        if not channel_changed:
            return
        for ticket, facts in sorted(projection.tickets.items()):
            launch = facts.launch
            if launch is None:
                continue
            if facts.settlement_state != machine_log.LIVE or lane_of(launch) == CODEX:
                continue
            with contextlib.suppress(Unreachable):
                self.deliver(ticket, launch, ANCHOR_TEMPLATE.format(
                    marker=ANCHOR_MARKER, ticket=ticket, name=name,
                    socket=COORDINATOR_SOCKET.format(pid=pid),
                ))

    # --- the loop itself ----------------------------------------------------------------------

    def arm(self, wave, projection):
        """Arm a monitor over the wave's live children in every lane that has none; returns nothing.

        Lane by lane, because they exit independently: a Claude monitor that has fired and been
        settled must be re-armed while the Codex watch beside it is still standing, and re-arming
        both would leave two watching the same session. A lane is its executor *and*, on the
        Claude side, the account binding it polls under — one wave's two accounts are two lanes,
        and one settling must not re-arm a second monitor over the other's children.
        """
        bindings = self.bindings()
        armed = {(monitor["lane"], monitor["account"]) for monitor in self.monitors}
        children = [
            projection.ticket(ticket).launch for ticket in self.live(wave, projection)
            if projection.ticket(ticket).launch is not None
            and projection.ticket(ticket).launch.get("worktree")
            and watch_lane(projection.ticket(ticket).launch, bindings.get(ticket)) not in armed
        ]
        if children:
            bridge = self.run.codex.bridge if self.run.codex else ""
            self.monitors += arm_monitors(
                self.run_dir, self.log, children, bridge, bindings
            )

    def bindings(self):
        """The account binding of every ticket of the run, by ticket, from the wave table.

        Read from the table rather than from the launch record: the table is the run's sole
        routing authority (ADR-0003), and the log's `launch` line records which account paid for
        a child — an attribution, not the execution semantics a monitor has to reproduce.
        """
        return {
            ticket.id: ticket.binding
            for ticket in self.plan.tickets
        }

    def harvest(self):
        """What every monitor that has exited since the last poll reports the children doing.

        Returns `{ticket: status}`. A monitor that exited nonzero is a wake-up that failed rather
        than one that fired, and nothing about a ticket may be concluded from it.
        """
        statuses = {}
        still_armed = []
        for monitor in self.monitors:
            process = monitor["process"]
            if process.poll() is None:
                still_armed.append(monitor)
                continue
            output, errors = process.communicate()
            if process.returncode != 0:
                raise DriverError(
                    f"the {monitor['lane']} wake monitor exited {process.returncode}:"
                    f" {(errors or output).strip()}",
                    pointer=str(self.log),
                )
            statuses.update(
                codex_statuses(output, monitor["tickets"]) if monitor["lane"] == CODEX
                else claude_statuses(output, machine_log.project(self.records()))
            )
        self.monitors = still_armed
        return statuses

    def poll(self, wave):
        """One turn of the loop over one wave; returns the wave to work next, or None when done."""
        records = self.records()
        projection = machine_log.project(records)
        if self.rule_on_messages(projection):
            return wave
        statuses = self.harvest()
        if statuses:
            projection = machine_log.project(self.records())
            if self.rule_on_statuses(statuses, projection):
                return wave
        records = self.records()
        projection = machine_log.project(records)
        if not self.live(wave, projection):
            disarm(self.monitors)
            self.monitors = []
            return self.advance(wave)
        self.arm(wave, projection)
        return wave

    def run_until_woken(self):
        """Apply the rule table until it is done or something outside it needs judgment."""
        projection = machine_log.project(self.records())
        wave = projection.current_wave
        deadline = time.monotonic() + self.args.timeout
        seen = None
        while True:
            following = self.poll(wave)
            if following is None:
                return self.finish()
            here = (following, len(self.records()), len(self.monitors))
            if here != seen:
                seen, deadline = here, time.monotonic() + self.args.timeout
            elif time.monotonic() >= deadline:
                raise DriverError(
                    f"wave {wave} has done nothing for {self.args.timeout:g} seconds, which the"
                    " rule table has no row for",
                    pointer=str(self.log),
                )
            wave = following
            time.sleep(self.args.poll_seconds)

    def finish(self):
        """Render and close the run, clear what landed, then emit its final wake snapshot."""
        try:
            cost_output = run_cost_pass(self.log, self.run)
            records = self.records()
            report = write_report(self.run_dir, self.run, self.plan, records, cost_output)
        finally:
            try:
                remove_run_pin(self.run_dir)
            finally:
                uninstall_run_hooks(self.run_dir, self.run, self.records())
        # The run is over and its report is written, so an artefact that would not go cannot
        # withhold the ending the run has earned — but the snapshot is the only channel a woken
        # coordinator reads, so a half-cleared site says so there rather than nowhere.
        cleanup = None
        try:
            epilogue(self.run_dir, self.run, self.plan, records, self.log)
        except ClearError as error:
            cleanup = str(error)
        snapshot(RUN_COMPLETE, pointer=str(report), report=str(report), cleanup=cleanup)
        return 0


def claude_statuses(output, projection):
    """{ticket: status} from a wake monitor's TSV lines, joined to tickets by worktree path.

    The monitor prints a line per status *change*, so the picture is the last word about each
    worktree across everything it printed rather than its final block alone. Paths are compared
    resolved, because the same directory is reached by two spellings on macOS (ADR-0007).
    """
    by_worktree = {}
    for ticket, facts in projection.tickets.items():
        launch = facts.launch
        if launch is None:
            continue
        worktree = launch.get("worktree")
        if worktree:
            by_worktree[os.path.realpath(worktree)] = ticket
    statuses = {}
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        ticket = by_worktree.get(os.path.realpath(parts[0]))
        if ticket is not None:
            statuses[ticket] = parts[1].strip()
    return statuses


def codex_statuses(output, tickets):
    """{ticket: status} from the Codex bridge's watch snapshot, by the state file it watched."""
    statuses = {}
    for line in output.splitlines():
        try:
            snapshot_object = json.loads(line)
        except ValueError:
            continue
        for session in (snapshot_object or {}).get("sessions") or []:
            ticket = tickets.get(str(session.get("stateFile")))
            if ticket is not None:
                statuses[ticket] = str(session.get("status"))
    return statuses


# --- closing a merged ticket in the run's tracker ------------------------------------------


def close_ticket(run, ticket, number):
    """Close that ticket where this repo keeps its tickets; returns the exact undo.

    Only the two trackers `references/trackers.md` declares exercised are reachable here — anything
    else stopped the run in preflight rather than arriving at a CLI nobody named.
    """
    if run.tracker == TRACKER_GITHUB:
        return close_github_issue(run, number)
    return close_local_ticket(run, ticket, number)


def close_local_ticket(run, ticket, number):
    """Set a local ticket's `Status:` to the finished value; returns how to put it back.

    A local tracker's close is a write inside the repository, so the edit is committed on the
    integration branch as it is made: the merge driver refuses to land a wave into a working tree
    that carries uncommitted changes, and a close left loose would stop the run's next wave on
    bookkeeping the run itself wrote.
    """
    path = pathlib.Path(ticket.path)
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError as error:
        raise DriverError(f"{number} could not be closed: {error}", ticket=number) from error
    for index, line in enumerate(lines):
        match = STATUS_LINE.match(line.rstrip("\n"))
        if not match:
            continue
        held = match.group(2)
        lines[index] = f"{match.group(1)}Status: {STATUS_FINISHED}\n"
        path.write_text("".join(lines), encoding="utf-8")
        undo = f"set `Status:` in {path} back to `{held}`"
        break
    else:
        # A ticket with no status line had none to finish; adding one and saying so keeps the undo
        # exact, which is the whole point of recording it.
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"\nStatus: {STATUS_FINISHED}\n")
        undo = f"take the `Status: {STATUS_FINISHED}` line off the end of {path}"
    commit_close(run.repo_root, path, number)
    return undo


def commit_close(repo, path, number):
    """Commit that close on the integration branch, and nothing else with it."""
    result = git(
        repo, "commit", "-m", f"chore: close {number} in the local tracker", "--", str(path)
    )
    if result.returncode != 0 and git_output(repo, "status", "--porcelain", "--", str(path)):
        detail = (result.stderr or result.stdout).strip()
        raise DriverError(
            f"the close of {number} could not be committed: {detail}", ticket=number
        )


def close_github_issue(run, number):
    """Close that issue with its pickup label off; returns how to reopen and re-label it.

    Every call is made from the run's own checkout and names no repository: `gh` takes an
    `OWNER/REPO` slug there, not a path, and the checkout it is run in is what it resolves the
    slug from — which is also the one repository this run is allowed to touch.
    """
    repo = run.repo_root
    listed = gh(repo, "issue", "view", number, "--json", "labels")
    labels = []
    if listed.returncode == 0:
        with contextlib.suppress(ValueError):
            labels = [
                label.get("name") for label in (json.loads(listed.stdout) or {}).get("labels") or []
                if label.get("name") in PICKUP_LABELS
            ]
    for label in labels:
        gh_or_raise(
            repo, number, f"the pickup label {label} could not be taken off {number}",
            "issue", "edit", number, "--remove-label", label,
        )
    gh_or_raise(repo, number, f"{number} could not be closed", "issue", "close", number)
    undo = f"gh issue reopen {number}"
    if labels:
        undo += f", then gh issue edit {number} --add-label {','.join(labels)}"
    return f"{undo} (in {repo})"


def gh(repo, *arguments):
    """One `gh` call, made in the run's own checkout so it resolves that repository."""
    return subprocess.run([GH, *arguments], cwd=str(repo), capture_output=True, text=True)


def gh_or_raise(repo, number, message, *arguments):
    """The same call, where anything but success stops the close; returns nothing."""
    result = gh(repo, *arguments)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().replace("\n", " ")
        raise DriverError(f"{message}: {detail}", ticket=number)


# --- the loop's two entry points ---------------------------------------------------------------


def wave_loop(args, repo, run_dir, table_path, adopting=False):
    """Run the wave loop over a prepared run; returns the exit code its ending earns.

    Every way out of the loop carries the command that puts it back: a run stopped by a driver
    error is one the operator fixes and the coordinator carries on, exactly as a run stopped by a
    ruling is, so the snapshot that says it stopped has to say how it goes on.

    A loop taking over a run its own process did not launch reconciles it first, inside this
    handling: a run that cannot be adopted wakes the coordinator with a snapshot exactly as one
    that cannot be carried on does.
    """
    start_dashboard(args, repo, run_dir)
    loop = Loop(args, repo, run_dir, table_path)
    resume = resume_command(args)
    try:
        if adopting:
            loop.adopt()
        return loop.run_until_woken()
    except Wake as wake:
        snapshot(
            wake.reason, ticket=wake.ticket, pointer=wake.pointer, resume=resume, **wake.fields
        )
        return 0
    except DriverError as error:
        snapshot(
            DRIVER_ERROR, ticket=error.ticket, pointer=error.pointer,
            detail=str(error), resume=resume,
        )
        return DRIVER_ERROR_EXIT
    finally:
        disarm(loop.monitors)


def resume_command(args):
    """The one command that puts the loop back where it left off, for the snapshot to carry.

    The launcher rather than this driver's own `resume`, because every driver of a run belongs in
    a window of its own and not only the first: a coordinator that put the loop back as a task of
    its own session would be handing the harness the very process this run must not lose (#103).
    The pid is passed because it is already known for certain; the name and the mode are left to
    be read off the harness again, because a mode switched mid-run has to be the current one.
    """
    return shlex.join([
        sys.executable, str(LAUNCH), str(args.feature_dir),
        "--coordinator-pid", str(args.coordinator_pid),
    ])


def run_resume(args):
    """Carry on the loop of a run already under way, once the coordinator has ruled."""
    feature_dir = pathlib.Path(args.feature_dir).resolve()
    run_dir = feature_dir / RUN_DIR_NAME
    table = run_dir / TABLE_NAME
    if not table.exists():
        raise DriverError(
            f"{feature_dir} holds no run to resume: {table} is not there", pointer=str(feature_dir)
        )
    repo = repository_root(feature_dir, args.repo_root)
    args.tmux_session = tmux_session(args.tmux_session)
    take_up_run(run_dir)
    print(f"crew resumed, run directory {run_dir}", flush=True)
    return wave_loop(args, repo, run_dir, table)


def run_answer(args):
    """Deliver one coordinator answer on the recorded child's own channel; returns 0."""
    run_dir = pathlib.Path(args.run_dir).resolve()
    table_path = run_dir / TABLE_NAME
    if not table_path.exists():
        raise DriverError(
            f"{run_dir} holds no run to answer: {table_path} is not there",
            pointer=str(run_dir),
        )
    loop = Loop(args, run_dir.parent, run_dir, table_path)
    ticket = str(args.ticket)
    launch = machine_log.project(loop.records()).ticket(ticket).launch
    if launch is None:
        raise DriverError(
            f"{ticket} has no recorded child in {loop.log}", ticket=ticket, pointer=str(loop.log)
        )
    # Text reaches either executor — `deliver` already carries it to a Codex child over the
    # bridge — so only the keys are guarded here: they answer a tmux permission prompt, and a
    # Codex child runs with approvals off and has no pane to type into.
    if args.keys and launch.get("executor") != CLAUDE:
        raise DriverError(
            f"{ticket} is not a Claude child with a tmux permission prompt: Codex children"
            " take text answers only, so answer it with --text",
            ticket=ticket, pointer=str(loop.log),
        )
    if args.text is None and not args.keys:
        raise DriverError(
            "an answer needs --text or at least one --key", ticket=ticket, pointer=str(loop.log)
        )
    unsupported = [key for key in args.keys if key not in ANSWER_KEYS]
    if unsupported:
        raise DriverError(
            f"unsupported answer key(s): {', '.join(unsupported)}",
            ticket=ticket, pointer=str(loop.log),
        )
    loop.deliver(ticket, launch, args.text, args.keys)
    return 0


# --- entry point ------------------------------------------------------------------------------


def non_empty_session_id(value):
    """Return a non-empty coordinator session ID for the hook boundary, or raise."""
    if not value.strip():
        raise argparse.ArgumentTypeError("coordinator session ID is empty")
    return value


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    start = commands.add_parser("start", help="preflight, prepare and launch a run's first wave")
    start.set_defaults(handler=run_start)
    start.add_argument("--feature-dir", required=True, help="the feature whose tickets this runs")
    start.add_argument(
        "--coordinator-name", required=True, help="the coordinator session children answer to"
    )
    start.add_argument(
        "--coordinator-pid", required=True, type=int,
        help="its pid — the trust anchor a child authenticates a ruling against",
    )
    start.add_argument(
        "--coordinator-session", required=True, type=non_empty_session_id,
        help="its session ID — the scope of coordinator-only hooks",
    )
    start.add_argument(
        "--permission-mode", required=True, help="the mode children launch under, which is its own"
    )
    start.add_argument(
        "--base-branch",
        help="the branch this run cuts from (default: the repository's own default branch)",
    )
    start.add_argument("--repo-root", help="the repository (default: the feature's own checkout)")
    start.add_argument("--spec", help="the spec every child is pointed at (default: the feature's)")
    start.add_argument(
        "--tmux-session", help="the session this run's windows live in (default: the driver's own)"
    )
    start.add_argument(
        "--codex-bridge", help=f"the bridge Codex children are launched and watched through"
                               f" (default: {CODEX_BRIDGE})",
    )
    clear = commands.add_parser(
        "clear", help="inventory a run, confirm in the terminal, and remove its artefacts"
    )
    clear.set_defaults(handler=run_clear)
    clear.add_argument("--run-dir", required=True, help="the recorded run directory to clear")
    add_loop_arguments(start)

    resume = commands.add_parser(
        "resume", help="carry a run's wave loop on from where a ruling stopped it"
    )
    resume.set_defaults(handler=run_resume)
    resume.add_argument("--feature-dir", required=True, help="the feature whose run this resumes")
    resume.add_argument(
        "--coordinator-pid", required=True, type=int,
        help="the pid of the session this run is driven from, which the dashboard is checked"
             " against",
    )
    resume.add_argument("--repo-root", help="the repository (default: the feature's own checkout)")
    resume.add_argument(
        "--tmux-session", help="the session this run's windows live in (default: the driver's own)"
    )
    add_loop_arguments(resume)

    answer = commands.add_parser(
        "answer", help="deliver and record a coordinator answer to a child on its own channel"
    )
    answer.set_defaults(handler=run_answer)
    answer.add_argument("--run-dir", required=True, help="the recorded run directory")
    answer.add_argument("--ticket", required=True, help="the ticket whose child is being answered")
    answer.add_argument(
        "--text", help="literal text to deliver: typed into a Claude child's pane, sent to a"
                       " Codex child as its next turn through the bridge",
    )
    answer.add_argument(
        "--key", dest="keys", action="append", default=[], metavar="KEY",
        help="a permission-prompt key name; repeat for a sequence (Claude children only)",
    )
    return parser


def add_loop_arguments(command):
    """The two dials the wave loop turns on, which every entry point into it carries."""
    command.add_argument(
        "--poll-seconds", type=float, default=float(
            os.environ.get("CREW_POLL_SECONDS") or DEFAULT_POLL_SECONDS
        ),
        help="how often the loop asks the log what has happened (default: %(default)s)",
    )
    command.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS,
        help="how long a run may do nothing at all before that becomes a wake (default:"
             " %(default)s)",
    )


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except ClearError as error:
        print(f"clear: {error}", file=sys.stderr)
        return 1
    except DriverError as error:
        snapshot(DRIVER_ERROR, ticket=error.ticket, pointer=error.pointer, detail=str(error))
        return DRIVER_ERROR_EXIT
    except KeyboardInterrupt:
        # The operator's own Ctrl-C in the driver's window: as deliberate an ending as any wake,
        # so the run is put down and nothing on the dashboard flags it as a driver that was
        # killed. No wake is written, because nobody asked the coordinator for a ruling.
        put_down_run()
        print("crew: the driver was stopped", file=sys.stderr, flush=True)
        return INTERRUPTED_EXIT


if __name__ == "__main__":
    sys.exit(main())
