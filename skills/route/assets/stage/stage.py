#!/usr/bin/env python3
"""Materialise a crew run directory from a set of tracker tickets, and say whether it will start.

`/route` ends on the tracker, and `/crew` begins at a directory; this script is the bridge between
them. It takes the ticket set the user approved, writes `crewtask/<n>/` — a `spec.md` plus one
`<ticket-number>.md` per ticket, at the directory's root, which is the layout the driver reads —
and then asks the driver's own code whether that directory would start. Only a fully green answer
prints `/crew crewtask/<n>`. Anything else names each blocking item beside its fix and withholds
the command, because a printed command that fails is the defect this script exists to remove.

**The self-check is the driver's, not a copy of it.** Ticket parsing, `## Routing` validation, the
dependency graph and the wave-table build are `driver.py`'s own functions, called here on the
directory just written; the environment gates are the driver's `config_problems` and
`dirty_tree_problems`. Two skills that both decide what a valid run directory is would drift; one
that asks the other cannot. The driver's base-branch check is deliberately not repeated: the branch
a run cuts from is an argument of the run's start (`--base-branch`), not a property of the staged
directory, and staging does not know which branch the later session will name.

**A ticket reference is whatever the configured tracker calls one.** Under `[tracker] kind =
"github"` it is the issue number, read with `gh issue view`; under `local` it is the path to the
ticket's markdown file. Both go through the read operation `references/trackers.md` names, and
nothing here reaches for a CLI the config did not name.

**The dependency closure.** An edge to a ticket outside the set is resolved against the tracker: a
closed one is already satisfied, so the edge is stripped as the ticket is written, and the driver's
graph check — which fails on a blocker no ticket of the run carries — passes. An open one is a
decision the operator has to make, so it is a blocking item naming both tickets.

**The directory is the handle.** `n` is the current maximum plus one. A re-run for the same ticket
set finds its own directory again through the `Tracker:` provenance line in that directory's
`spec.md` and overwrites the markdown files in place, leaving any `.crew` record a run already
started there alone. `crewtask/` is gitignored, so none of this touches the tracked tree.
"""

import argparse
import json
import pathlib
import re
import subprocess
import sys

# The driver owns the parsing and the validation this script self-checks with, so it is imported
# rather than restated. Reached from this file's own location, never from an install path.
PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[4]
DRIVER_DIR = PLUGIN_ROOT / "skills" / "crew" / "assets" / "driver"
sys.path.insert(0, str(DRIVER_DIR))

import driver  # noqa: E402  (the path above is what makes this importable)

RUN_ROOT = "crewtask"
SPEC_NAME = "spec.md"
PROVENANCE_KEY = "Tracker:"

# The values a run resolves at launch, not at staging: the coordinator's identity and the session
# it draws in. The renderer's validation requires them present, so the self-check fills them with
# a name that could never be mistaken for a resolved one.
LATER = "resolved-at-launch"

# `--parent` is part of the invocation shape downstream couples to, so it exists here from the
# first release; the sub-issue expansion behind it belongs to its own ticket.
PARENT_TICKET = "#64"

BLOCKED_EXIT = 1

GH = "gh"
GH_FIELDS = "number,title,body,state"
GH_STATE_CLOSED = "CLOSED"

TICKET_NUMBER = re.compile(r"#(\d+)")


class Blocked(Exception):
    """The blocking items that stopped this staging run, each carrying its own fix."""

    def __init__(self, problems):
        super().__init__("; ".join(problems))
        self.problems = list(problems)


# --- the tracker's read operation ---------------------------------------------------------------


def gh_read(repo, reference, fields=GH_FIELDS):
    """That issue as the tracker holds it; raises Blocked where `gh` could not read it."""
    if not str(reference).isdigit():
        raise Blocked([
            f"ticket {reference}: is not an issue number — under the github tracker a ticket"
            " reference is the issue's own number, so pass that"
        ])
    result = subprocess.run(
        [GH, "issue", "view", str(reference), "--json", fields],
        cwd=str(repo), capture_output=True, text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().replace("\n", " ")
        raise Blocked([
            f"ticket {reference}: the tracker could not read it — {detail}; check the number, and"
            " that this checkout is the repository the issue belongs to"
        ])
    try:
        return json.loads(result.stdout)
    except ValueError as error:
        raise Blocked([
            f"ticket {reference}: the tracker's answer was not readable — {error}"
        ]) from error


def local_read(repo, reference):
    """That local ticket file as the repository holds it; raises Blocked where it is not one."""
    path = pathlib.Path(reference)
    if not path.is_absolute():
        path = repo / path
    match = driver.TICKET_FILE.match(path.name)
    if not match or not path.is_file():
        raise Blocked([
            f"ticket {reference}: is not a ticket file — under the local tracker a ticket"
            " reference is the path to its `<number>.md` file, so pass that"
        ])
    return match.group(1), path


def local_status(text):
    """What that ticket file's `Status:` line says, or None where it carries none."""
    for line in text.splitlines():
        match = driver.STATUS_LINE.match(line)
        if match:
            return match.group(2).strip()
    return None


def read_ticket(kind, repo, reference):
    """One ticket of the set: its number, its title, the text to stage, and whether it is closed."""
    if kind == driver.TRACKER_GITHUB:
        issue = gh_read(repo, reference)
        body = issue.get("body") or ""
        title = issue.get("title") or ""
        # The tracker keeps the title out of the body; the staged file is what the driver reads a
        # title from, so the two are put back together as the run-directory layout wants them.
        text = f"# {title}\n\n{body.rstrip()}\n"
        return {
            "id": str(issue.get("number")),
            "title": title,
            "text": text,
            "closed": (issue.get("state") or "").upper() == GH_STATE_CLOSED,
        }
    number, path = local_read(repo, reference)
    text = path.read_text(encoding="utf-8")
    return {
        "id": number,
        "title": driver.title_of(text, number),
        "text": text,
        "closed": local_status(text) == driver.STATUS_FINISHED,
    }


def outside_closed(kind, repo, number, sources):
    """Whether that outside-set ticket is closed; raises Blocked where the tracker cannot say.

    `sources` are the directories the local tracker's tickets were read from, which is where a
    blocker outside the set is looked for — the same convention that found the set itself.
    """
    if kind == driver.TRACKER_GITHUB:
        issue = gh_read(repo, number, fields="number,state")
        return (issue.get("state") or "").upper() == GH_STATE_CLOSED
    for directory in sources:
        for path in sorted(directory.glob("*.md")):
            match = driver.TICKET_FILE.match(path.name)
            if match and match.group(1) == number:
                return local_status(path.read_text(encoding="utf-8")) == driver.STATUS_FINISHED
    raise Blocked([
        f"blocker #{number}: no ticket file for it sits beside the ticket set"
        f" ({', '.join(str(source) for source in sources)}) — add it to the set, or point the set"
        " at the directory that holds it"
    ])


# --- the dependency closure ----------------------------------------------------------------------


def blockers_of(text):
    """The tickets that text declares itself blocked by, read by the driver's own parser."""
    return driver.blockers_of(driver.sections(text).get(driver.BLOCKED_BY_SECTION, ""))


def resolve_closure(kind, repo, tickets, sources):
    """Which outside-set edges are already satisfied; raises Blocked on the ones that are not.

    Returns the set of ticket numbers whose edges are stripped, which is every outside-set blocker
    the tracker reports closed. An open one is nobody's to decide here: the operator either pulls
    it into the set or waits for it, and until then this run would start on a dependency that is
    not met.
    """
    inside = {ticket["id"] for ticket in tickets}
    stripped = set()
    problems = []
    for ticket in tickets:
        for number in blockers_of(ticket["text"]):
            if number in inside or number in stripped:
                continue
            if outside_closed(kind, repo, number, sources):
                stripped.add(number)
                continue
            problems.append(
                f"{ticket['id']} → #{number}: {ticket['id']} is blocked by #{number}, which is"
                f" open and outside this ticket set — add #{number} to the set, or wait for it to"
                " close and stage again"
            )
    if problems:
        raise Blocked(problems)
    return stripped


def strip_edges(text, stripped):
    """That ticket's text with its edges to `stripped` blockers spent rather than pending.

    Only the `Blocked by` section is touched, and the line keeps whatever the ticket said about the
    edge: the child still reads why the dependency was there, and the driver's graph — which reads
    `#<number>` tokens — no longer sees an edge to a ticket this run does not carry.
    """
    if not stripped:
        return text
    inside = False
    written = []
    for line in text.splitlines(keepends=True):
        heading = driver.SECTION.match(line.rstrip("\n"))
        if heading:
            inside = heading.group(1).lower() == driver.BLOCKED_BY_SECTION
        elif inside:
            line = TICKET_NUMBER.sub(
                lambda match: (
                    f"{match.group(1)} (closed)" if match.group(1) in stripped else match.group(0)
                ),
                line,
            )
        written.append(line)
    return "".join(written)


# --- the run directory -----------------------------------------------------------------------


def provenance(kind, tickets):
    """The line that says where this directory came from, and so which re-run owns it."""
    numbers = sorted(tickets, key=lambda ticket: int(ticket["id"]))
    return f"{kind} " + " ".join(f"#{ticket['id']}" for ticket in numbers)


def cover_page(kind, tickets):
    """The generated `spec.md` for a ticket set with no parent to take a spec from."""
    listed = "\n".join(f"- #{ticket['id']} — {ticket['title']}" for ticket in tickets)
    return (
        f"# Staged run of {len(tickets)} ticket{'s' if len(tickets) != 1 else ''}\n"
        "\n"
        f"{PROVENANCE_KEY} {provenance(kind, tickets)}\n"
        "\n"
        "This page is written by the staging script, and there is no parent spec behind it. Each\n"
        "ticket in this run is self-contained: its own brief is the whole context, and nothing\n"
        "here adds to it.\n"
        "\n"
        f"{listed}\n"
    )


def numbered_runs(run_root):
    """Every `crewtask/<n>` the repository already holds, keyed by its number."""
    if not run_root.is_dir():
        return {}
    return {
        int(path.name): path for path in run_root.iterdir()
        if path.is_dir() and path.name.isdigit()
    }


def allocate(run_root, line):
    """The directory this staging owns: the one carrying that provenance, or the next number."""
    existing = numbered_runs(run_root)
    wanted = f"{PROVENANCE_KEY} {line}"
    for number in sorted(existing):
        spec = existing[number] / SPEC_NAME
        if not spec.is_file():
            continue
        if any(text.strip() == wanted for text in spec.read_text(encoding="utf-8").splitlines()):
            return number, existing[number]
    number = max(existing, default=0) + 1
    return number, run_root / str(number)


def materialise(directory, kind, tickets, stripped):
    """Write the run directory's `spec.md` and one file per ticket; returns nothing.

    A directory being re-staged keeps its subdirectories: a run already started there holds its
    machine log and wave table under `.crew`, and refreshing the tickets is no reason to destroy
    the record of what was done with them.
    """
    directory.mkdir(parents=True, exist_ok=True)
    for path in directory.glob("*.md"):
        path.unlink()
    (directory / SPEC_NAME).write_text(cover_page(kind, tickets), encoding="utf-8")
    for ticket in tickets:
        (directory / f"{ticket['id']}.md").write_text(
            strip_edges(ticket["text"], stripped), encoding="utf-8"
        )


# --- the self-check --------------------------------------------------------------------------


def candidate_run(repo, directory, config):
    """The run section the driver would build for this directory, for validation only."""
    args = argparse.Namespace(
        spec=None,
        codex_bridge=None,
        coordinator_name=LATER,
        coordinator_pid=LATER,
        tmux_session=LATER,
        permission_mode=LATER,
    )
    head = driver.git_output(repo, "rev-parse", "HEAD") or LATER
    return driver.run_section(
        args, repo, directory, directory / driver.RUN_DIR_NAME,
        base_branch=None, return_branch=head, base_commit=head, config=config,
    )


def self_check(repo, directory, config):
    """Every blocking item the driver's own checks find in that directory, in the driver's words."""
    tickets = driver.read_tickets(directory)
    run = candidate_run(repo, directory, config)
    problems = driver.dirty_tree_problems(repo)
    with driver.scratch() as scratch:
        problems += driver.routing_problems(tickets, run, scratch)
    problems += driver.graph_problems(tickets)
    problems += driver.config_problems(repo, run)
    if not problems:
        # The wave table is the last thing the driver builds before it dispatches, so a directory
        # that cannot be ordered into waves is not one a run starts from, whatever else passed.
        try:
            driver.assign_waves(tickets)
        except driver.DriverError as error:
            problems.append(f"wave table: {error}")
    return problems


# --- the command line ------------------------------------------------------------------------


def repository_root(given):
    """The repository staging writes into: the one named, or the one this was run in."""
    if given:
        return pathlib.Path(given).resolve()
    root = driver.git_output(pathlib.Path.cwd(), "rev-parse", "--show-toplevel")
    if not root:
        raise Blocked([
            f"repository: {pathlib.Path.cwd()} is not inside a git repository — run staging from"
            " the repository the run will build in, or name it with --repo-root"
        ])
    return pathlib.Path(root).resolve()


def tracker_kind(repo, config):
    """The tracker this repository's config names; raises Blocked where it names none usable."""
    run = {
        "repair_model": driver.config_value(config, driver.REPAIR_MODEL_KEYS),
        "tracker": driver.config_value(config, driver.TRACKER_KIND_KEYS),
    }
    problems = driver.config_problems(repo, run)
    if run["tracker"] not in driver.TRACKERS:
        # Without a tracker there is no reading of tickets at all, so this one stops staging here
        # rather than at the self-check, and carries whatever else the config was missing with it.
        raise Blocked(problems)
    return run["tracker"]


def ticket_sources(kind, repo, references):
    """The directories a local ticket set was read from, which its outside blockers share."""
    if kind != driver.TRACKER_LOCAL:
        return []
    directories = []
    for reference in references:
        path = pathlib.Path(reference)
        path = path if path.is_absolute() else repo / path
        if path.parent not in directories:
            directories.append(path.parent)
    return directories


def build_parser():
    parser = argparse.ArgumentParser(
        prog="stage.py",
        description=(
            "Stage a crew run directory from a set of tracker tickets and self-check it against"
            " the driver's own preflight. Prints `/crew crewtask/<n>` and exits 0 only when the"
            " self-check is fully green; otherwise names each blocking item and exits non-zero."
        ),
    )
    parser.add_argument(
        "tickets", nargs="*", metavar="TICKET",
        help=(
            "the ticket set to stage, as the configured tracker names a ticket: an issue number"
            " under `[tracker] kind = \"github\"`, a path to the ticket's `<number>.md` file under"
            " `local`"
        ),
    )
    parser.add_argument(
        "--parent", metavar="N",
        help=(
            f"the parent ticket to expand into its sub-issues — accepted by {PARENT_TICKET}, which"
            " owns the expansion; until it lands, pass the ticket set itself"
        ),
    )
    parser.add_argument(
        "--repo-root", help="the repository to stage into; default the one this is run in",
    )
    return parser


def stage(args):
    """Stage that ticket set; returns the number of the run directory it wrote."""
    repo = repository_root(args.repo_root)
    config = driver.project_config(repo)
    kind = tracker_kind(repo, config)

    # A reference repeated in the set names one ticket, not two: the directory holds one file per
    # ticket, and the cover page and the provenance line have to say the same.
    references = list(dict.fromkeys(args.tickets))
    tickets = [read_ticket(kind, repo, reference) for reference in references]
    stripped = resolve_closure(kind, repo, tickets, ticket_sources(kind, repo, references))
    number, directory = allocate(repo / RUN_ROOT, provenance(kind, tickets))
    materialise(directory, kind, tickets, stripped)

    problems = self_check(repo, directory, config)
    if problems:
        raise Blocked(problems)
    return number


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.parent:
        parser.error(
            f"--parent expands to the parent's sub-issues in {PARENT_TICKET}, which is not landed"
            " yet — pass the ticket set itself for now"
        )
    if not args.tickets:
        parser.error("no tickets to stage — pass the ticket set this run is made of")
    try:
        number = stage(args)
    except Blocked as blocked:
        print(
            f"staging stopped on {len(blocked.problems)} blocking"
            f" item{'s' if len(blocked.problems) != 1 else ''}:",
            file=sys.stderr,
        )
        for problem in blocked.problems:
            print(f"- {problem}", file=sys.stderr)
        return BLOCKED_EXIT
    print(f"/crew {RUN_ROOT}/{number}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
