#!/usr/bin/env python3
"""Land a wave's landable branches on the integration branch without waking the coordinator.

    merge_driver.py land --table <wave table> --wave N --log <machine log>
                         --repair-model <full model ID> [--repair-budget-usd 2]

Every branch whose receipt says `landable` is merged `--no-ff` in ticket order. A merge that
conflicts is classified before anything else happens (ADR-0004):

    mechanical  every conflicted path is a content conflict whose every hunk has an empty base
                section — both sides only inserted at the same point, and neither touched a line
                the other touched. Nothing about the two designs disagrees, so the driver
                keeps both sides' insertions itself — ours, then theirs, markers removed.
    semantic    anything else: a hunk whose base section is not empty (both sides rewrote the
                same existing lines), a file both sides created, a file one side deleted and the
                other changed, or a conflict with no readable markers at all. Two children's
                designs disagree, and only a ruling settles which one stands.

A mechanical conflict costs no model anything. The budget-capped headless repair session this
script launches itself is left for the mechanical conflict its own rewrite refuses — a file whose
markers do not open and close in order, or whose text carries a line that reads as one — and is
retried on a second session if the first leaves it unresolved. A semantic conflict skips that rung,
and a repair double failure exhausts it; both abort the merge and record one `escalated` event for
the coordinator, carrying the pointers a ruling starts from — the ticket's path, its branch, and
the conflicted files.

The receipts are read from the machine log, and every step is written back to it, so the log is
both the driver's input and its account of itself. One line per ticket is printed:

    07 clean <sha>
    08 resolved <sha>
    09 repaired <sha>
    10 escalated <why>
    11 skipped failed

Exit 0 when every landable branch landed, 1 when any ticket escalated or the wave could not be
started.
"""

import argparse
import hashlib
import os
import pathlib
import subprocess
import sys

# The branch a ticket's worktree was cut on is the dispatch renderer's naming, not a second
# convention: the driver merges exactly what the renderer created.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "dispatch"))
import dispatch  # noqa: E402
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import accounts  # noqa: E402
import run_plan  # noqa: E402
import machine_log  # noqa: E402

MACHINE_LOG = pathlib.Path(__file__).resolve().parent / "machine_log.py"

# The only verdict that lands. A failed or parked branch is never merged, and a ticket with no
# receipt has not been verified by anything, which is not the same as being landable.
LANDABLE = machine_log.LANDABLE

# ADR-0004: the repair rung is a headless session under a hard budget cap. `--max-budget-usd`
# binds only on a headless (`--print`) session, so the two flags travel together or not at all.
REPAIR_COMMAND = "claude"
HEADLESS_FLAG = "--print"
BUDGET_FLAG = "--max-budget-usd"
DEFAULT_BUDGET_USD = 2.0
DEFAULT_REPAIR_ATTEMPTS = 2
DEFAULT_REPAIR_TIMEOUT_SECONDS = 900.0
REPAIR_PERMISSION_MODE = "acceptEdits"
# The account the repair session spends on. Repairing a ticket's conflict is spend that ticket
# caused, so the session runs on the ticket's own Claude login exactly as its child did. The
# binding is read off the ticket's row, already resolved: the wave table is where a ticket's
# account name stopped being a name (ADR-0014), and this script reads no account registry. Which
# environment that binding is — one variable set, or nothing touched — is the account module's
# answer, not this script's.
CONFIG_HOME_ENV_VAR = accounts.CONFIG_HOME_VARIABLE

# The conflict markers, in the style the merge is run under: `diff3` keeps the base section, and
# whether that section is empty is the whole of the mechanical/semantic test.
CONFLICT_STYLE = "merge.conflictStyle=diff3"
OURS_MARKER = "<<<<<<<"
BASE_MARKER = "|||||||"
SPLIT_MARKER = "======="
THEIRS_MARKER = ">>>>>>>"

MECHANICAL = "mechanical"
SEMANTIC = "semantic"

# The merge results this script writes, in the machine log's closed vocabulary. `resolved` is the
# driver's own deterministic resolution and `repaired` a session's, and they are separate words
# because a log that spells them alike cannot say which merges cost a model anything.
RESOLVED = "resolved"
REPAIRED = "repaired"


class WaveError(Exception):
    """The wave cannot be landed at all: nothing has been merged and nothing will be."""


def git(repo, *args):
    """Run git in `repo` and return the completed process, failed or not."""
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )


def git_or_raise(repo, *args):
    result = git(repo, *args)
    if result.returncode != 0:
        raise WaveError(f"git {' '.join(args)}: {(result.stderr or result.stdout).strip()}")
    return result.stdout.strip()


# --- the machine log ------------------------------------------------------------------------


def log_event(log, *args):
    """Append one event through the log's own writer, so its closed sets stay the only ones."""
    result = subprocess.run(
        [sys.executable, str(MACHINE_LOG), "--log", str(log), *args],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise WaveError(f"machine log: {(result.stderr or result.stdout).strip()}")


def unverified_reason(repo, branch, receipt):
    """The reason this branch is not the commit that was verified, or None when it is.

    A receipt is a sha: it says a script checked *this commit*, and a branch is a ref that can
    move off it. A receipt naming no sha verified no commit whatever its verdict says, and a
    branch that has moved carries work nothing has verified; neither is merged.
    """
    verified = receipt.get("sha")
    if not verified:
        return "its receipt names no sha, so nothing was verified to merge"
    head = git_or_raise(repo, "rev-parse", f"{branch}^{{commit}}")
    if head != verified:
        return f"the branch is at {head}, and its receipt verified {verified}"
    return None


# --- the conflict classifier ------------------------------------------------------------------


def unmerged_stages(repo):
    """{path: {stages present}} for every path the merge left unmerged.

    Stage 1 is the merge base, 2 ours, 3 theirs; a path missing one of them is a conflict about
    the file's existence rather than its contents.
    """
    listed = git_or_raise(repo, "ls-files", "--unmerged", "-z")
    stages = {}
    for record in listed.split("\0"):
        if not record:
            continue
        head, _, path = record.partition("\t")
        stages.setdefault(path, set()).add(int(head.split()[-1]))
    return stages


def marker(line):
    """The conflict marker `line` is, or None — a marker stands alone or labels itself.

    `=======` is also how a line of prose underlines a heading, so a marker is only read as one
    inside a hunk; this says what a line looks like, and the caller says where it was found.
    """
    for candidate in (OURS_MARKER, BASE_MARKER, SPLIT_MARKER, THEIRS_MARKER):
        if line == candidate or line.startswith(candidate + " "):
            return candidate
    return None


def has_conflict_hunk(text):
    return any(marker(line) == OURS_MARKER for line in text.splitlines())


def markers_standing(text):
    """Every conflict marker still in `text`, in the order they were found.

    A resolved file has none of the four. `=======` is also how prose underlines a heading, so a
    file that really contains one cannot be repaired by a script and goes to the coordinator
    instead — the driver refuses to commit a file it cannot tell apart from a half-resolved one.
    """
    found = []
    for line in text.splitlines():
        standing = marker(line)
        if standing and standing not in found:
            found.append(standing)
    return found


def hunks_are_insertions_only(text):
    """Whether every conflict hunk in `text` has an empty base section.

    An empty base is both sides inserting where there was nothing; a base with lines in it is
    both sides rewriting the same existing lines, which is a disagreement about that code.
    """
    saw_hunk = False
    in_hunk = False
    in_base = False
    base_lines = 0
    for line in text.splitlines():
        found = marker(line)
        if found == OURS_MARKER:
            saw_hunk = in_hunk = True
            in_base = False
            continue
        if not in_hunk:
            continue
        if found == BASE_MARKER:
            in_base = True
            base_lines = 0
        elif found == SPLIT_MARKER:
            if in_base and base_lines:
                return False
            in_base = False
        elif found == THEIRS_MARKER:
            in_hunk = in_base = False
        elif in_base:
            base_lines += 1
    # A conflicted file with no hunk in it carries its conflict somewhere this cannot read.
    return saw_hunk


def classify(repo, stages):
    """`(MECHANICAL, reason)` or `(SEMANTIC, reason)` for the conflict standing in `repo`."""
    for path, present in sorted(stages.items()):
        if present != {1, 2, 3}:
            missing = "created by both sides" if 1 not in present else "deleted by one side"
            return SEMANTIC, f"{path} was {missing}"
        try:
            text = (pathlib.Path(repo) / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return SEMANTIC, f"{path} carries no readable conflict markers"
        if not hunks_are_insertions_only(text):
            return SEMANTIC, f"both sides rewrote the same lines of {path}"
    return MECHANICAL, "both sides only inserted at the same point"


# --- the driver's own resolution ---------------------------------------------------------------


def resolve_insertions(text):
    """`text` with every hunk rewritten as ours then theirs, or None where it cannot be.

    This is the resolution a mechanical classification already proved correct: both sides only
    inserted at the same point, so keeping both insertions in a fixed order loses nothing and
    decides nothing. The base section is dropped, which on a mechanical conflict is empty anyway.

    None is a file this cannot rewrite: markers that do not open and close in order, a file with no
    hunk at all, or one whose own text carries a line that reads as a marker — the same ambiguity
    the driver already refuses to commit through, since `=======` is also how prose underlines a
    heading. That is a stricter reading than the classifier's, which is why this scans the file
    again rather than sharing that one: a conflict can be mechanical and still be one this will not
    rewrite, and the rung below exists for exactly that file.
    """
    kept, ours, theirs = [], [], []
    # Which section of a hunk this line is in: None outside a hunk at all, and the base section's
    # lines are the ones nothing keeps.
    section = None
    saw_hunk = False
    # `split` rather than `splitlines`: the final element is whatever followed the last newline,
    # so joining it back restores the file's ending exactly as it was.
    for line in text.split("\n"):
        found = marker(line)
        if section is None:
            if found == OURS_MARKER:
                section, saw_hunk = "ours", True
                ours, theirs = [], []
            else:
                kept.append(line)
            continue
        if found == OURS_MARKER:
            return None
        if found == BASE_MARKER:
            if section != "ours":
                return None
            section = "base"
        elif found == SPLIT_MARKER:
            if section == "theirs":
                return None
            section = "theirs"
        elif found == THEIRS_MARKER:
            if section != "theirs":
                return None
            kept.extend(ours)
            kept.extend(theirs)
            section = None
        elif section == "ours":
            ours.append(line)
        elif section == "theirs":
            theirs.append(line)
    if section is not None or not saw_hunk:
        return None
    settled = "\n".join(kept)
    return None if markers_standing(settled) else settled


def keep_both_insertions(repo, paths):
    """Rewrite and stage every conflicted path as ours then theirs; returns whether it did.

    All or nothing: nothing is written until every path has been rewritten, so a file this cannot
    rewrite leaves the whole conflict standing exactly as git wrote it, which is what the rung
    below has to be handed.
    """
    rewritten = {}
    for path in paths:
        file = pathlib.Path(repo) / path
        try:
            text = file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False
        settled = resolve_insertions(text)
        if settled is None:
            return False
        rewritten[file] = settled
    for file, settled in rewritten.items():
        file.write_text(settled, encoding="utf-8")
    git_or_raise(repo, "add", "--", *paths)
    return True


# --- the repair rung ---------------------------------------------------------------------------


def repair_prompt(repo, branch, into, paths):
    """The brief the repair session is launched on: this conflict, and nothing else to do."""
    listed = "\n".join(f"- {path}" for path in paths)
    markers = ", ".join((OURS_MARKER, BASE_MARKER, SPLIT_MARKER, THEIRS_MARKER))
    return (
        f"Resolve a mechanical git merge conflict in the repository at {repo}.\n"
        f"The merge of {branch} into {into} is in progress and has stopped on these files:\n"
        f"{listed}\n\n"
        "Mechanical means both sides added new lines at the same point, and neither changed a "
        "line the other changed. Keep both sides' additions, ordered so the file reads correctly, "
        "and remove the conflict markers around them.\n\n"
        "Those files are your whole task: edit them and stop there. Staging, committing and the "
        "merge itself belong to the script that launched you, so leave every git command to it.\n\n"
        f"Done means every listed file carries both sides' additions and none of the four markers "
        f"({markers}), and every other file in the repository stands exactly as you found it — "
        "the script checks both before it commits. Where keeping both additions would be the "
        "wrong answer for a file, leave that file as it stands and stop: the script takes it to "
        "the coordinator for a ruling, which is the right ending for a conflict a rule cannot "
        "settle."
    )


def repair_command(model, budget, prompt):
    """The headless, budget-capped session the script launches itself (ADR-0004)."""
    return [
        REPAIR_COMMAND,
        HEADLESS_FLAG, prompt,
        "--model", model,
        BUDGET_FLAG, f"{budget:g}",
        "--permission-mode", REPAIR_PERMISSION_MODE,
    ]


def run_repair(repo, command, timeout, binding):
    """Whether the repair session exited cleanly; a session that overruns its time has not.

    The session is launched in the binding's own environment: the named account's configuration
    home where the ticket named one, and this script's own environment untouched where it did not
    — a default home spelled out explicitly is a login that may fail where the inherited one
    works (#110).
    """
    try:
        result = subprocess.run(
            command, cwd=str(repo), capture_output=True, text=True, timeout=timeout,
            env=accounts.process_environment(binding),
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0


def content_digest(path):
    """A digest of the bytes at `path`, or None where nothing is there to read."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def working_state(repo):
    """{path: (status code, content digest)} for everything git reports as changed.

    This is the before-and-after the repair session is held to: it was given one conflict to
    resolve, and the rest of the wave's repository is not its to touch. The digest is what makes
    that a real comparison — a file rewritten in place keeps the status code it already had, and
    only its contents say it moved.
    """
    listed = git_or_raise(repo, "status", "--porcelain", "-z")
    state = {}
    for record in listed.split("\0"):
        if record:
            path = record[3:]
            state[path] = (record[:2], content_digest(pathlib.Path(repo) / path))
    return state


def strayed_outside(before, after, allowed):
    """Every path the repair session changed that was not the conflict it was handed."""
    touched = set(before) | set(after)
    return sorted(
        path for path in touched
        if path not in allowed and before.get(path) != after.get(path)
    )


def repair_attempt(repo, paths, command, timeout, baseline, binding):
    """Run one repair session and stage what it left; returns None, or why it did not settle it.

    The session edits files and nothing else — staging is done here, so the session needs no
    permission to run git, and what is committed is exactly the paths the conflict named.

    `baseline` is the working state before the *first* attempt, not this one: aborting a merge
    puts tracked files back but leaves a stray untracked file standing, and a second session must
    not inherit the first one's mess as the state it is measured against.
    """
    if not run_repair(repo, command, timeout, binding):
        return "the repair session did not finish"
    strays = strayed_outside(baseline, working_state(repo), set(paths))
    if strays:
        # A session that edited its way outside the conflict has made a change nobody verified,
        # which is the one thing a merge this script commits must never carry.
        return f"the repair session also changed {', '.join(strays)}"
    for path in paths:
        file = pathlib.Path(repo) / path
        if not file.exists():
            continue
        try:
            standing = markers_standing(file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            return f"{path} is no longer readable text"
        if standing:
            return f"{path} still carries {', '.join(standing)}"
    git_or_raise(repo, "add", "--", *paths)
    if unmerged_stages(repo):
        return "the conflict is still unmerged"
    return None


# --- landing one ticket -------------------------------------------------------------------------


def start_merge(repo, branch):
    """Merge `branch` no-ff; returns the conflicted paths, or None when the merge committed."""
    result = git(repo, "-c", CONFLICT_STYLE, "merge", "--no-ff", "--no-edit", branch)
    if result.returncode == 0:
        return None
    stages = unmerged_stages(repo)
    if not stages:
        # Not a conflict at all — a branch that does not exist, a hook that refused the commit.
        git(repo, "merge", "--abort")
        raise WaveError((result.stderr or result.stdout).strip())
    return stages


def escalation_detail(reason, ticket, branch, paths):
    """An escalation carries its own pointers, so a ruling never starts with a hunt."""
    pointers = f"{reason}; ticket {ticket.path}; branch {branch}"
    return f"{pointers}; conflicted {', '.join(paths)}" if paths else pointers


def merge_recorder(run, ticket, branch, options):
    """A one-argument writer of this ticket's `merge` events, so every one of them agrees."""
    def record(result, **fields):
        log_event(
            options["log"], "merge", "--ticket", ticket.id, "--result", result,
            "--branch", branch, "--into", run.integration_branch,
            *[flag for name, value in fields.items() for flag in (f"--{name}", value)],
        )
    return record


def escalate(run, ticket, branch, options, reason, paths):
    """Record the one event that wakes the coordinator; returns `(printed line, True)`."""
    detail = escalation_detail(reason, ticket, branch, paths)
    merge_recorder(run, ticket, branch, options)("escalated", detail=detail)
    return f"{ticket.id} escalated {detail}", True


def climb_the_ladder(repo, into, ticket, branch, options, record):
    """Land one conflicted merge, or return the reason it has to be a ruling.

    Returns `(printed line, None)` when a rung below the coordinator settled it, and
    `(None, reason)` when the ladder is exhausted with the merge still in progress.
    """
    stages = unmerged_stages(repo)
    paths = sorted(stages)
    classification, reason = classify(repo, stages)
    record("conflict", detail=f"{classification}: {reason}; conflicted {', '.join(paths)}")
    if classification == SEMANTIC:
        return None, reason

    # The classification has already proved both sides only inserted, which is the whole of what
    # the repair brief used to ask a model to do. Doing it here spends nothing and cannot wander,
    # so the session below is left for the conflict this cannot rewrite.
    if keep_both_insertions(repo, paths):
        # The commit refuses while any path is still unmerged, so it is also the check that every
        # conflict this claims to have settled really is settled.
        git_or_raise(repo, "commit", "--no-edit")
        sha = git_or_raise(repo, "rev-parse", "HEAD")
        record(RESOLVED, sha=sha, detail=f"kept both sides' insertions in {', '.join(paths)}")
        return f"{ticket.id} {RESOLVED} {sha}", None

    binding = ticket.binding

    command = repair_command(
        options["repair_model"], options["repair_budget"],
        repair_prompt(repo, branch, into, paths),
    )
    attempts = options["repair_attempts"]
    baseline = working_state(repo)
    for attempt in range(1, attempts + 1):
        if attempt > 1:
            # Each attempt starts from the conflict as git left it, not from what the last
            # session made of it.
            git_or_raise(repo, "merge", "--abort")
            start_merge(repo, branch)
        failure = repair_attempt(
            repo, paths, command, options["repair_timeout"], baseline, binding
        )
        if failure is None:
            git_or_raise(repo, "commit", "--no-edit")
            sha = git_or_raise(repo, "rev-parse", "HEAD")
            record(
                REPAIRED, sha=sha,
                detail=f"repair session {attempt} of {attempts} resolved {', '.join(paths)}",
            )
            return f"{ticket.id} {REPAIRED} {sha}", None
    return None, f"{attempts} repair sessions left the conflict standing: {failure}"


def land_ticket(run, ticket, branch, options):
    """Land one branch and record what happened; returns `(printed line, escalated)`.

    Every way this can end short of a merged branch ends the same way: the merge is aborted, so
    the next ticket starts from a repository in one piece, and one `escalated` event carries the
    reason to the coordinator.
    """
    repo = run.repo_root
    into = run.integration_branch
    number = ticket.id
    record = merge_recorder(run, ticket, branch, options)

    paths = []
    try:
        if start_merge(repo, branch) is None:
            sha = git_or_raise(repo, "rev-parse", "HEAD")
            record("clean", sha=sha)
            return f"{number} clean {sha}", False
        paths = sorted(unmerged_stages(repo))
        line, reason = climb_the_ladder(repo, into, ticket, branch, options, record)
        if line:
            return line, False
    except WaveError as error:
        # A git command that failed mid-merge is not a conflict anyone can rule on, but it leaves
        # the same repository behind, so it takes the same way out.
        reason = str(error)

    git(repo, "merge", "--abort")
    return escalate(run, ticket, branch, options, reason, paths)


# --- the wave ------------------------------------------------------------------------------------


def ticket_order(ticket):
    """Ticket order: by number where a ticket is numbered, and by name where it is not."""
    number = ticket.id
    return (0, int(number), "") if number.isdigit() else (1, 0, number)


def merge_tickets(plan, wave):
    """The selected plan wave in merge policy's numeric ticket order."""
    return sorted(plan.wave(wave).tickets, key=ticket_order)


def check_repair_model(model):
    """Refuse an alias before a wave starts: an alias resolves to a model nobody approved."""
    problem = run_plan.model_problem("Repair model", model)
    if problem:
        raise WaveError(problem)


def open_integration_branch(run):
    """Check the integration branch out, refusing a repository with work of its own in the way."""
    repo = run.repo_root
    if git_or_raise(repo, "status", "--porcelain", "--untracked-files=no"):
        raise WaveError(f"{repo} has uncommitted changes; nothing was merged")
    git_or_raise(repo, "checkout", run.integration_branch)


def land_wave(plan, wave, options):
    """Land every landable ticket of `wave`; returns `(printed lines, whether any escalated)`."""
    run = plan.run
    tickets = merge_tickets(plan, wave)
    check_repair_model(options["repair_model"])
    open_integration_branch(run)

    projection = machine_log.project(machine_log.read_records(options["log"]))
    lines = []
    escalated = False
    for ticket in tickets:
        number = ticket.id
        branch = dispatch.branch_name(ticket)
        facts = projection.ticket(number)
        receipt = facts.receipt or {}
        if receipt.get("verdict") != LANDABLE:
            lines.append(f"{number} skipped {receipt.get('verdict') or 'no receipt'}")
            continue
        if facts.merge_landed:
            lines.append(f"{number} skipped already landed")
            continue
        unverified = unverified_reason(run.repo_root, branch, receipt)
        if unverified:
            line, _ = escalate(run, ticket, branch, options, unverified, [])
            lines.append(line)
            escalated = True
            continue
        line, ticket_escalated = land_ticket(run, ticket, branch, options)
        lines.append(line)
        escalated = escalated or ticket_escalated
    return lines, escalated


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("land",))
    parser.add_argument("--table", required=True, help="the approved wave table, as JSON")
    parser.add_argument("--wave", required=True, type=int, help="which wave of it to land")
    parser.add_argument("--log", required=True, help="the run's machine log")
    parser.add_argument(
        "--repair-model", required=True,
        help="the full model ID the repair rung runs on, never an alias",
    )
    parser.add_argument(
        "--repair-budget-usd", type=float, default=DEFAULT_BUDGET_USD,
        help="the hard cap on what one repair session may spend (default: %(default)s)",
    )
    parser.add_argument(
        "--repair-attempts", type=int, default=DEFAULT_REPAIR_ATTEMPTS,
        help="how many repair sessions a mechanical conflict gets (default: %(default)s)",
    )
    parser.add_argument(
        "--repair-timeout", type=float, default=DEFAULT_REPAIR_TIMEOUT_SECONDS,
        help="how long one repair session is given before it is abandoned",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        plan = run_plan.load(args.table)
    except run_plan.RunPlanError as error:
        for problem in error.problems:
            print(problem, file=sys.stderr)
        return 1

    try:
        lines, escalated = land_wave(plan, args.wave, {
            "log": args.log,
            "repair_model": args.repair_model,
            "repair_budget": args.repair_budget_usd,
            "repair_attempts": args.repair_attempts,
            "repair_timeout": args.repair_timeout,
        })
    except (WaveError, run_plan.RunPlanError) as error:
        print(f"run: {error}", file=sys.stderr)
        return 1

    for line in lines:
        print(line)
    return 1 if escalated else 0


if __name__ == "__main__":
    sys.exit(main())
