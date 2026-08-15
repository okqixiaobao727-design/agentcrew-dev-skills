#!/usr/bin/env python3
"""The crew driver: the one command a run is started by, and the state machine it runs on.

    start   preflight the run, build and validate its wave table, prepare the branch and the run
            directory, dispatch wave 1, start the dashboard, and arm the wake monitors

The driver runs as a background task of the coordinator's own session, so it costs that session no
turn while it works and its exit is what wakes it (ADR-0001). Two contracts follow from that and
are the whole of what a woken coordinator reads; the tickets that build the wave loop and the
wind-down on top of this module couple to them rather than to anything inside it.

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
launch line and exits 0, and the loop that ends in one of the four reasons belongs to the wave
loop built on this module.

**A preflight failure never reaches the coordinator as diagnosis.** Preflight is exactly four
read-only checks — a clean working tree, a base branch that resolves and fast-forwards, a valid
routing on every ticket (the renderer's own validation, which is the authority on the case list),
and a complete acyclic dependency graph. On any failure the driver launches nothing, prints the
`preflight-failed` snapshot naming the problem count and the display surface, and shows the full
problem list to the operator in a detached tmux window named `crew-preflight` in the run's own
session, ending with the reminder that fixes must be committed. That notice is the run's only
diagnosis surface: it is killed by name, in that session alone, at the start of the next run, so a
stale notice can never outlive its fix.

Everything the driver does to the repository, the children and the log it does through the existing
scripts — the dispatch renderer, the machine log, the monitor and its wake monitors, the Codex
bridge — at their published command lines; it reimplements none of them. The run directory holds
what today's index names and nothing this module invents: the wave table, the machine log, the
launch directory, the Codex state, and the parked paths.
"""

import argparse
import contextlib
import json
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib

CREW_SKILL_DIR = pathlib.Path(__file__).resolve().parents[2]
ASSETS = CREW_SKILL_DIR / "assets"
DISPATCH = ASSETS / "dispatch" / "dispatch.py"
MACHINE_LOG = ASSETS / "machine_log.py"
MONITOR = ASSETS / "monitor" / "monitor.py"
MONITOR_WAVE = ASSETS / "monitor-wave.sh"
CODEX_BRIDGE = ASSETS / "codex" / "codex_bridge.py"

# The run's own directory, inside the feature it runs: `docs/` publishes what it holds.
RUN_DIR_NAME = ".crew"
LOG_NAME = "log.jsonl"
TABLE_NAME = "wave-table.json"
LAUNCH_DIR_NAME = "launch"
CODEX_DIR_NAME = "codex"
PARKED_PATHS_NAME = "parked-paths"

# The operator's preflight surface: one detached window, found and cleared by this name.
NOTICE_WINDOW_NAME = "crew-preflight"
NOTICE_HEADING = "crew preflight stopped this run:"
NOTICE_REMINDER = (
    "Fix each of these and commit the fixes — an uncommitted fix is not one — then type /crew"
    " again."
)

# The settings file a session's hooks are registered in, relative to the directory it runs in.
SETTINGS_PATH = pathlib.Path(".claude") / "settings.local.json"
# The project config the dashboard's surface and the launch hook are read from.
CONFIG_NAME = "agentcrew.toml"
LAUNCH_HOOK_SECTION = ("hooks", "on-child-launch")

# The wake reasons. `preflight-failed` and `driver-error` are this module's; the other two belong
# to the wave loop and the wind-down, and are named here because the snapshot's shape is one.
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

# A ticket file of a feature: its number, as written, and whatever slug follows it.
TICKET_FILE = re.compile(r"^(\d+)(?:-.*)?\.md$")
# A `Key: value` routing line, in any order, as `Blocked by:` edges are parsed.
ROUTING_LINE = re.compile(r"^([A-Za-z][A-Za-z ]*?)\s*:\s*(.+?)\s*$")
BLOCKER = re.compile(r"#(\d+)")
SECTION = re.compile(r"^##\s+(.*?)\s*$")

ROUTING_SECTION = "routing"
BLOCKED_BY_SECTION = "blocked by"
# The routing keys the table carries under their own names, and the review lane's three words.
ROUTING_KEYS = ("workflow", "executor", "model", "effort")
REVIEW_KEY = "review"
REVIEW_FIELDS = ("vendor", "model", "effort")

CODEX = "codex"


class DriverError(Exception):
    """Something outside the rule table: it wakes the coordinator with a snapshot."""

    def __init__(self, message, ticket=None, pointer=None):
        super().__init__(message)
        self.ticket = ticket
        self.pointer = pointer


# --- the wake snapshot ----------------------------------------------------------------------


def snapshot(reason, ticket=None, pointer=None, **fields):
    """Print the run's wake snapshot: the one JSON object a woken coordinator reads."""
    record = {"reason": reason, "ticket": ticket, "pointer": pointer}
    record.update(fields)
    print(json.dumps(record, ensure_ascii=False))


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


# --- the tickets ----------------------------------------------------------------------------


def sections(text):
    """A ticket's `##` sections, keyed by their lowercased headings."""
    found = {}
    heading = None
    for line in text.splitlines():
        match = SECTION.match(line)
        if match:
            heading = match.group(1).lower()
            found[heading] = []
            continue
        if heading is not None:
            found[heading].append(line)
    return {name: "\n".join(lines) for name, lines in found.items()}


def title_of(text, number):
    """The ticket's own first heading, which is the title the dashboard draws."""
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return number


def routing_of(section):
    """The routing that section declares, under the table's own key names.

    A key the section does not carry is left out rather than defaulted: routing has no default and
    no fallback, and a ticket missing one is unrouted — which the renderer's validation is the
    authority on, not this parser.
    """
    values = {}
    for line in section.splitlines():
        match = ROUTING_LINE.match(line)
        if not match:
            continue
        key = match.group(1).strip().lower()
        value = match.group(2).strip()
        if key in ROUTING_KEYS:
            values[key] = value
        elif key == REVIEW_KEY:
            words = value.split()
            values[REVIEW_KEY] = dict(zip(REVIEW_FIELDS, words)) if len(words) == 3 else value
    return values


def blockers_of(section):
    """The ticket ids this ticket is blocked by, as its `Blocked by` section writes them."""
    return BLOCKER.findall(section)


def read_tickets(feature_dir):
    """Every ticket of the feature, in number order, carrying the routing it declares."""
    tickets = []
    for path in sorted(feature_dir.glob("*.md")):
        match = TICKET_FILE.match(path.name)
        if not match:
            continue
        text = path.read_text(encoding="utf-8")
        parts = sections(text)
        ticket = {
            "id": match.group(1),
            "title": title_of(text, match.group(1)),
            "path": str(path),
            "blocked_by": blockers_of(parts.get(BLOCKED_BY_SECTION, "")),
        }
        ticket.update(routing_of(parts.get(ROUTING_SECTION, "")))
        tickets.append(ticket)
    return tickets


# --- the four preflight checks ----------------------------------------------------------------


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
            " refs/remotes/origin/HEAD, so give the run one with --base-branch"
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


def routing_problems(tickets, run, launch_dir):
    """The renderer's own verdict on this table's routing, one line per offending ticket.

    The renderer's validation is the authority on the case list — the alias rule, the review
    lane's rules, every required key — so the driver asks it rather than restating it. The
    candidate table
    is one wave of every ticket, because a table that cannot be routed is never dispatched and the
    wave a ticket would sit in has no bearing on whether its routing is valid.
    """
    if not tickets:
        return ["run: the feature carries no tickets to route"]
    candidate = launch_dir / "candidate-table.json"
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text(
        json.dumps({"run": run, "waves": [{"wave": 1, "tickets": tickets}]}), encoding="utf-8"
    )
    result = subprocess.run(
        [
            sys.executable, str(DISPATCH), "render",
            "--table", str(candidate),
            "--wave", "1",
            "--out-dir", str(candidate.parent / "candidate"),
        ],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        return []
    return [line for line in result.stderr.splitlines() if line.strip()]


def graph_problems(tickets):
    """Whether every blocker exists in the feature and whether the graph is acyclic."""
    problems = []
    edges = {ticket["id"]: list(ticket["blocked_by"]) for ticket in tickets}
    for ticket in tickets:
        for blocker in ticket["blocked_by"]:
            if blocker not in edges:
                problems.append(
                    f"{ticket['id']} {ticket['path']}: is blocked by #{blocker}, which no ticket"
                    " of this feature carries"
                )
    cycle = find_cycle({
        identifier: [blocker for blocker in blockers if blocker in edges]
        for identifier, blockers in edges.items()
    })
    if cycle:
        problems.append(
            "dependency graph: " + " → ".join(cycle) + " is a cycle, which no wave can order"
        )
    return problems


def find_cycle(edges):
    """One cycle of that graph, as the ids around it, or None where it is acyclic."""
    WALKING, DONE = 1, 2
    marks = {}
    stack = []

    def walk(identifier):
        marks[identifier] = WALKING
        stack.append(identifier)
        for blocker in edges[identifier]:
            state = marks.get(blocker)
            if state == WALKING:
                return stack[stack.index(blocker):] + [blocker]
            if state is None:
                found = walk(blocker)
                if found:
                    return found
        stack.pop()
        marks[identifier] = DONE
        return None

    for identifier in edges:
        if identifier not in marks:
            found = walk(identifier)
            if found:
                return found
    return None


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


def launch_hook(repo):
    """The project's `[hooks.on-child-launch]`, or nothing where it declares none."""
    config = repo / CONFIG_NAME
    if not config.exists():
        return None
    try:
        parsed = tomllib.loads(config.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise DriverError(f"{config} is unreadable: {error}") from error
    section = parsed
    for key in LAUNCH_HOOK_SECTION:
        section = section.get(key) if isinstance(section, dict) else None
    return section if isinstance(section, dict) and section else None


def run_section(args, repo, feature_dir, run_dir, base_branch, return_branch, base_commit):
    """The table's `run` section: everything about this run that is not a ticket."""
    run = {
        "repo_root": str(repo),
        "spec_path": str(args.spec or feature_dir / "spec.md"),
        "integration_branch": f"crew/{feature_dir.name}",
        "integration_base_commit": base_commit,
        "coordinator_name": args.coordinator_name,
        "coordinator_pid": args.coordinator_pid,
        "crew_skill_dir": str(CREW_SKILL_DIR),
        "tmux_session": args.tmux_session,
        "permission_mode": args.permission_mode,
        # Recorded for the run's own wind-down: where the run came from, and where it goes back to.
        "base_branch": base_branch,
        "return_branch": return_branch,
        "feature_dir": str(feature_dir),
        "codex": {
            "bridge": str(args.codex_bridge or CODEX_BRIDGE),
            "state_dir": str(run_dir / CODEX_DIR_NAME),
        },
    }
    hook = launch_hook(repo)
    if hook:
        run["launch_hook"] = hook
    return run


def assign_waves(tickets):
    """Every ticket in the first wave its blockers allow, built from the dependency frontier.

    Wave 1 is every ticket with no blocker, and a ticket joins the first wave after all of its
    blockers. The graph is known complete and acyclic by the time this runs — preflight is what
    says so — so every ticket is placed.
    """
    waves = []
    remaining = list(tickets)
    placed = {}
    while remaining:
        frontier = [
            ticket for ticket in remaining
            if all(blocker in placed for blocker in ticket["blocked_by"])
        ]
        if not frontier:
            raise DriverError(
                "the wave table cannot be ordered: "
                + ", ".join(ticket["id"] for ticket in remaining)
                + " block each other"
            )
        number = len(waves) + 1
        waves.append({"wave": number, "tickets": frontier})
        for ticket in frontier:
            placed[ticket["id"]] = number
        remaining = [ticket for ticket in remaining if ticket["id"] not in placed]
    return waves


def prepare_branches(repo, base_branch, integration_branch, pull):
    """Fast-forward the base branch and cut this run's integration branch from it; returns the
    branch the run returns to and the commit it is based on."""
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
    if git_output(repo, "rev-parse", "--verify", f"refs/heads/{integration_branch}"):
        raise DriverError(
            f"the integration branch {integration_branch} already exists: this run has been"
            " started before, and adopting an unfinished run is not this command's to do"
        )
    result = git(repo, "switch", "-c", integration_branch)
    if result.returncode != 0:
        raise DriverError(
            f"the integration branch {integration_branch} could not be cut:"
            f" {result.stderr.strip()}"
        )
    return return_branch, git_output(repo, "rev-parse", "HEAD")


def install_hook(log, settings, role, ticket=None):
    """Register this run's log hook in that settings file, through the log's own operation."""
    arguments = [
        sys.executable, MACHINE_LOG, "--log", log, "install",
        "--settings", settings, "--role", role,
    ]
    if ticket:
        arguments += ["--ticket", ticket]
    run_command(arguments, f"the {role} hook could not be installed in {settings}")


def launched_children(log):
    """Every child this run's log records as launched: its ticket, worktree and executor."""
    children = []
    for line in pathlib.Path(log).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("event") == "launch":
            children.append(record)
    return children


def arm_monitors(run_dir, log, children, bridge):
    """Arm the wake monitors over this wave's live children, Claude and Codex each under theirs.

    Each is a one-shot wake-up that outlives this process: armed while every child under it is
    busy, exiting with its snapshot as soon as one needs attention.
    """
    parked_paths = run_dir / PARKED_PATHS_NAME
    parked_paths.touch()
    claude_worktrees = [
        child["worktree"] for child in children
        if child.get("executor") != CODEX and child.get("worktree")
    ]
    codex_states = [
        str(run_dir / CODEX_DIR_NAME / f"{child['ticket']}.json")
        for child in children if child.get("executor") == CODEX
    ]
    if claude_worktrees:
        spawn([str(MONITOR_WAVE), "--log", str(log), str(parked_paths), *claude_worktrees])
    if codex_states:
        spawn([sys.executable, str(bridge), "watch", *codex_states])


def spawn(arguments):
    """Start a wake monitor and leave it running; returns nothing.

    Its exit is a wake, not this process's business: the monitor outlives the driver.
    """
    subprocess.Popen(
        arguments,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


# --- start ------------------------------------------------------------------------------------


@contextlib.contextmanager
def scratch():
    """A working directory for preflight's own artifacts, removed whatever preflight decides.

    Preflight is read-only about the run: the candidate table it hands the renderer, and what the
    renderer renders from it, are this process's scratch and never the run directory's — a run
    that does not start leaves no run directory behind.
    """
    directory = tempfile.mkdtemp(prefix="crew-preflight-")
    try:
        yield pathlib.Path(directory)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def preflight(repo, tickets, base_branch, upstream, run):
    """The four read-only checks, every problem of every one of them."""
    problems = dirty_tree_problems(repo)
    problems += base_branch_problems(repo, base_branch, upstream)
    with scratch() as directory:
        problems += routing_problems(tickets, run, directory)
    problems += graph_problems(tickets)
    return problems


def resolved(path):
    """That path, absolute, or None where none was given."""
    return pathlib.Path(path).resolve() if path else None


def run_start(args):
    feature_dir = pathlib.Path(args.feature_dir).resolve()
    if not feature_dir.is_dir():
        raise DriverError(f"{feature_dir} is not a feature directory", pointer=str(feature_dir))
    # Absolute at the boundary, whatever spelling the caller used: every path recorded here is read
    # again in a child's own worktree, where a relative one names another file or none.
    args.spec = resolved(args.spec)
    args.codex_bridge = resolved(args.codex_bridge)
    repo = repository_root(feature_dir, args.repo_root)
    args.tmux_session = tmux_session(args.tmux_session)
    clear_notice(args.tmux_session)

    run_dir = feature_dir / RUN_DIR_NAME
    base_branch = args.base_branch or default_base_branch(repo)
    tickets = read_tickets(feature_dir)
    # The table preflight validates: everything the run section carries but the commit the run has
    # not cut yet, which no routing rule reads.
    head = git_output(repo, "rev-parse", "HEAD")
    candidate = run_section(args, repo, feature_dir, run_dir, base_branch, head, head)
    upstream = upstream_state(repo, base_branch) if base_branch else (UPSTREAM_ABSENT, "")
    problems = preflight(repo, tickets, base_branch, upstream, candidate)

    if problems:
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

    integration_branch = candidate["integration_branch"]
    return_branch, base_commit = prepare_branches(
        repo, base_branch, integration_branch, pull=upstream[0] == UPSTREAM_PRESENT
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    launch_dir = run_dir / LAUNCH_DIR_NAME
    log = run_dir / LOG_NAME
    run = run_section(
        args, repo, feature_dir, run_dir, base_branch, return_branch, base_commit
    )
    table = run_dir / TABLE_NAME
    table.write_text(
        json.dumps({"run": run, "waves": assign_waves(tickets)}, indent=2) + "\n",
        encoding="utf-8",
    )

    install_hook(log, repo / SETTINGS_PATH, "coordinator")
    dispatch_wave(table, log, launch_dir, run_dir)
    children = launched_children(log)
    for child in children:
        install_hook(
            log, pathlib.Path(child["worktree"]) / SETTINGS_PATH, "child", child["ticket"]
        )
    start_dashboard(args, repo, run_dir)
    arm_monitors(run_dir, log, children, run["codex"]["bridge"])
    print(f"crew wave 1 launched, run directory {run_dir}")
    return 0


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
    """Point the operator's dashboard at the run, on whichever surface the repo chose."""
    arguments = [
        sys.executable, MONITOR, "window",
        "--run-dir", run_dir, "--session", args.tmux_session,
        "--coordinator-pid", args.coordinator_pid,
    ]
    config = repo / CONFIG_NAME
    if config.exists():
        arguments += ["--config", config]
    run_command(arguments, "the dashboard could not be started", pointer=str(run_dir))


# --- entry point ------------------------------------------------------------------------------


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
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except DriverError as error:
        snapshot(DRIVER_ERROR, ticket=error.ticket, pointer=error.pointer, detail=str(error))
        return DRIVER_ERROR_EXIT


if __name__ == "__main__":
    sys.exit(main())
