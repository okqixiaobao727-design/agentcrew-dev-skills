#!/usr/bin/env python3
"""Materialise a crew run directory from a set of tracker tickets, and say whether it will start.

`/route` ends on the tracker, and `/crew` begins at a directory; this script is the bridge between
them. It takes the ticket set the user approved, writes `crewtask/<n>/` — a `spec.md` plus one
`<ticket-number>.md` per ticket, at the directory's root, which is the layout the driver reads —
and then asks the driver's own code whether that directory would start. A github file is a stub
pointing at the live issue and carrying only the two machine sections; a local file remains the
ticket itself. Only a fully green answer prints `/crew crewtask/<n>`. Anything else names each
blocking item beside its fix and withholds the command, because a printed command that fails is the
defect this script exists to remove.

**The self-check uses the Run plan, not a copy of it.** Ticket parsing, `## Routing` validation,
concrete account binding, the dependency graph and the wave-table build are the Run plan builder's
own work, called here on the directory just written; the environment gates are the Driver's
`config_problems` and `dirty_tree_problems`, plus its default base-branch check. Two modules that
both decide what a valid run directory is would drift; staging therefore asks the same builder a
real run asks. The Driver's base-branch check is
included for the default case: a bare command must not be printed when the repository cannot
resolve the default branch that a later run would use. A later run may still name an explicit
`--base-branch` override at start time.

**A ticket reference is whatever the configured tracker calls one.** Under `[tracker] kind =
"github"` it is the issue number, read with `gh issue view`; under `local` it is the path to the
ticket's markdown file. Both go through the read operation `references/trackers.md` names, and
nothing here reaches for a CLI the config did not name.

**The dependency closure.** An edge to a ticket outside the set is resolved against the tracker: a
closed one is already satisfied, so the edge is stripped as the ticket is written, and the driver's
graph check — which fails on a blocker no ticket of the run carries — passes. An open one is a
decision the operator has to make, so it is a blocking item naming both tickets.

**The directory is the handle.** `n` is the current maximum plus one. A re-run for the same ticket
set — or the same parent — finds its own directory again through the `Tracker:` provenance line in
that directory's `spec.md` and overwrites the markdown files in place, leaving any `.crew` record a
run already started there alone. `crewtask/` is gitignored, so none of this touches the tracked
tree.

**The spec page is a projection.** After the newly written tickets build a valid Run plan, staging
renders its ticket order and Wave membership into `spec.md`. The Reference index beside it uses the
same target-repository judgment-file definition as the bounded-read hook, so navigation and read
permission cannot drift into separate lists.

**The tracker's side.** `--parent <n>` expands to that parent's native sub-issues, so routing a
triaged piece of work needs nothing but one number; its closed sub-issues are finished work and stay
out of the set, where the closure resolution below meets them as satisfied edges. `--routing` is the
table the user approved: each entry is written back as that ticket's `## Routing` section and its
role label, through the **edit** and **mark** operations `references/trackers.md` names, and the
same routing is projected into the staged stub. Without `--routing` nothing is written to the
tracker at all. Finally, a
green self-check **comments** the staged `/crew crewtask/<n>` on the parent, or on every ticket of a
parentless set, so the pickup point lives where work state lives; a failed one comments nothing.
Every write is skipped when the tracker already holds that exact value, so re-staging refreshes
rather than duplicates.

On the local tracker a ticket is a file in the repository, so every write above is a file write.
Where those files are tracked, writing the approved routing into them dirties the tracked tree and
the self-check reports it with the fix the operator has to apply anyway — commit the ticket edits,
and stage again; the `Crew:` line a green staging then leaves is one more of the tracker's own files
to commit before the command is typed.
"""

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys

# Driver supplies environment-derived metadata; Run plan owns the planning this script checks.
# Both are reached from this file's own location, never from an install path.
PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[4]
CREW_ASSETS = PLUGIN_ROOT / "skills" / "crew" / "assets"
DRIVER_DIR = PLUGIN_ROOT / "skills" / "crew" / "assets" / "driver"
sys.path.insert(0, str(CREW_ASSETS))
sys.path.insert(0, str(DRIVER_DIR))

import bounded_read  # noqa: E402
import driver  # noqa: E402  (the path above is what makes this importable)
import run_plan  # noqa: E402
import tracker  # noqa: E402

RUN_ROOT = "crewtask"
SPEC_NAME = "spec.md"
PROVENANCE_KEY = "Tracker:"

# The values a run resolves at launch, not at staging: the coordinator's identity and the session
# it draws in. The renderer's validation requires them present, so the self-check fills them with
# a name that could never be mistaken for a resolved one.
LATER = "resolved-at-launch"

BLOCKED_EXIT = 1

GH = "gh"
GH_FIELDS = "number,title,body,state,url,comments"
GH_STATE_CLOSED = "CLOSED"
GH_SUB_ISSUES = "repos/{owner}/{repo}/issues/%s/sub_issues"
GH_BLOCKED_BY = "repos/{owner}/{repo}/issues/%s/dependencies/blocked_by"

TICKET_NUMBER = re.compile(r"#(\d+)")

# The `## Routing` section `skills/route/references/classify.md` templates: these keys, in this
# order, one line each. Two may be left out of an approved entry: `review`, which four of the six
# workflows take none of, and `account`, which is the user's own override rather than anything
# `/route` concludes — a ticket naming none runs on the coordinator's account and is written
# exactly as it was before accounts existed. Every other line is required, because a ticket
# missing one is unrouted and the renderer would refuse it at the self-check anyway.
ROUTING_HEADING = "## Routing"
ROUTING_ORDER = ("workflow", "executor", "model", "effort", "account", "review", "reasons")
ROUTING_OPTIONAL = ("review", "account")

# The role strings classify.md names: an `acceptance` ticket is a human's to pick up, every other
# ticket an agent's.
HUMAN_WORKFLOW = "acceptance"
AGENT_ROLE, HUMAN_ROLE = driver.PICKUP_LABELS

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
    """That local ticket file as the repository holds it; raises Blocked where it is not one.

    A local ticket is a file **in the repository** — that is the whole of what the local tracker is
    — and the write operations below rewrite the file they were given, so a reference resolving
    outside the repository is refused before anything reads or writes it.
    """
    path = pathlib.Path(reference)
    if not path.is_absolute():
        path = repo / path
    path = path.resolve()
    if not path.is_relative_to(repo):
        raise Blocked([
            f"ticket {reference}: resolves to {path}, outside {repo} — under the local tracker a"
            " ticket is a file in the repository, so pass a path inside it"
        ])
    match = run_plan.TICKET_FILE.match(path.name)
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


def section(text, wanted):
    """One Markdown section, including its heading, exactly as the source carries it."""
    lines = text.splitlines(keepends=True)
    start = None
    for index, line in enumerate(lines):
        heading = run_plan.SECTION.match(line.rstrip("\n"))
        if heading and start is not None:
            return "".join(lines[start:index]).rstrip("\n")
        if heading and heading.group(1).lower() == wanted:
            start = index
    return "" if start is None else "".join(lines[start:]).rstrip("\n")


def ticket_pointer(url):
    """The tracker-authority line shared by ticket and parent stubs."""
    return f"Ticket: {url} — the issue body and every comment are this ticket; read all of it."


def staged_text(kind, title, body, url=None):
    """That ticket as the run directory holds it, pointing at its tracker authority."""
    if kind == driver.TRACKER_GITHUB:
        machine_sections = [
            held for name in (run_plan.ROUTING_SECTION, run_plan.BLOCKED_BY_SECTION)
            if (held := section(body, name))
        ]
        suffix = "".join(f"\n\n{held}" for held in machine_sections)
        return f"# {title}\n\n{ticket_pointer(url)}{suffix}\n"
    return body


def read_ticket(kind, repo, reference):
    """One ticket and the tracker facts staging projects into its run-directory file."""
    if kind == driver.TRACKER_GITHUB:
        issue = gh_read(repo, reference)
        body = issue.get("body") or ""
        title = issue.get("title") or ""
        return {
            "id": str(issue.get("number")),
            "title": title,
            "body": body,
            "text": staged_text(kind, title, body, issue.get("url")),
            "url": issue.get("url"),
            "comment_count": len(issue.get("comments") or []),
            "path": None,
            "repository": repo,
            "closed": (issue.get("state") or "").upper() == GH_STATE_CLOSED,
        }
    number, path = local_read(repo, reference)
    text = path.read_text(encoding="utf-8")
    return {
        "id": number,
        "title": run_plan.ticket_title(text, number),
        "body": text,
        "text": text,
        "path": path,
        "repository": repo,
        "closed": local_status(text) == driver.STATUS_FINISHED,
    }


def json_documents(text):
    """Every entry of a paginated `gh api` answer, however it split the pages up.

    A paginated array comes back as one merged array where `gh` merges the pages, and as one JSON
    document per page where it does not; both are read here, so a parent with more sub-issues than
    a page holds expands the same as one with fewer.
    """
    decoder = json.JSONDecoder()
    entries = []
    index = 0
    text = text or ""
    while True:
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            return entries
        page, index = decoder.raw_decode(text, index)
        entries += page if isinstance(page, list) else [page]


def github_relation(repo, endpoint, subject, relation):
    """Issue objects from one paginated GitHub relation; raises Blocked where unreadable."""
    result = subprocess.run(
        [GH, "api", "--paginate", endpoint],
        cwd=str(repo), capture_output=True, text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().replace("\n", " ")
        raise Blocked([
            f"{subject}: the tracker could not list its {relation} — {detail}; check the"
            " number, and that this checkout is the repository the issue belongs to"
        ])
    try:
        return json_documents(result.stdout)
    except ValueError as error:
        raise Blocked([
            f"{subject}: the tracker's {relation} list was not readable — {error}"
        ]) from error


def sub_issue_numbers(repo, parent):
    """The parent's native sub-issue numbers, in the tracker's own order."""
    listed = github_relation(
        repo, GH_SUB_ISSUES % parent, f"parent {parent}", "sub-issue",
    )
    return [str(entry["number"]) for entry in listed if entry.get("number") is not None]


def github_blockers(repo, ticket):
    """The ticket's native GitHub blocked-by issues, in tracker order."""
    return github_relation(
        repo,
        GH_BLOCKED_BY % ticket["id"],
        f"ticket {ticket['id']}",
        "blocked-by",
    )


def ticket_blockers(kind, repo, ticket):
    """Every blocker however this tracker records it, once each and in source order."""
    body_blockers = run_plan.ticket_dependencies(ticket["text"])
    if kind != driver.TRACKER_GITHUB:
        return tuple(dict.fromkeys(body_blockers))
    native = (
        str(entry["number"])
        for entry in github_blockers(repo, ticket)
        if entry.get("number") is not None
    )
    return tuple(dict.fromkeys((*body_blockers, *native)))


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
            match = run_plan.TICKET_FILE.match(path.name)
            if match and match.group(1) == number:
                return local_status(path.read_text(encoding="utf-8")) == driver.STATUS_FINISHED
    raise Blocked([
        f"blocker #{number}: no ticket file for it sits beside the ticket set"
        f" ({', '.join(str(source) for source in sources)}) — add it to the set, or point the set"
        " at the directory that holds it"
    ])


# --- the dependency closure ----------------------------------------------------------------------


def resolve_closure(kind, repo, tickets, sources):
    """Which outside-set edges are already satisfied; raises Blocked on the ones that are not.

    Returns the set of ticket numbers whose edges are stripped, which is every outside-set blocker
    the tracker reports closed. An open one is nobody's to decide here: the operator either pulls
    it into the set or waits for it, and until then this run would start on a dependency that is
    not met.
    """
    inside = {ticket["id"] for ticket in tickets}
    dependencies = {
        ticket["id"]: ticket_blockers(kind, repo, ticket)
        for ticket in tickets
    }
    stripped = set()
    problems = []
    for ticket in tickets:
        for number in dependencies[ticket["id"]]:
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
    return dependencies, stripped


def project_edges(text, dependencies, stripped):
    """That staged ticket with the complete dependency set rendered in `Blocked by`.

    Existing body lines keep their explanation. Native-only edges gain the minimal Markdown line
    the Run plan reads. Closed outside-set blockers lose their `#`, so their reason remains visible
    while the Run plan no longer sees a pending edge.
    """
    existing = run_plan.ticket_dependencies(text)
    dependencies = tuple(dict.fromkeys(dependencies))
    if dependencies == existing and not stripped:
        return text

    lines = text.splitlines(keepends=True)
    start = None
    end = len(lines)
    for index, line in enumerate(lines):
        heading = run_plan.SECTION.match(line.rstrip("\n"))
        if not heading:
            continue
        if start is None and heading.group(1).lower() == run_plan.BLOCKED_BY_SECTION:
            start = index + 1
        elif start is not None:
            end = index
            break
    if start is None:
        suffix = "" if text.endswith("\n") else "\n"
        rendered = "\n".join(
            f"- {number} (closed)" if number in stripped else f"- #{number}"
            for number in dependencies
        )
        return f"{text}{suffix}\n## Blocked by\n\n{rendered}\n"

    seen = set()
    section = lines[start:end] if existing else ["\n"]

    def render(match):
        number = match.group(1)
        if number in seen:
            return number
        seen.add(number)
        return f"{number} (closed)" if number in stripped else match.group(0)

    section = [TICKET_NUMBER.sub(render, line) for line in section]
    if section and section[-1].strip():
        section.append("\n")
    for number in dependencies:
        if number in seen:
            continue
        suffix = " (closed)" if number in stripped else ""
        marker = "" if number in stripped else "#"
        section.append(f"- {marker}{number}{suffix}\n")
    return "".join([*lines[:start], *section, *lines[end:]])


# --- the tracker's edit and mark operations ------------------------------------------------------


def gh_write(repo, arguments, reference, operation, body=None):
    """One `gh` write against that issue; raises Blocked where the tracker refused it."""
    result = subprocess.run(
        [GH, "issue", *arguments], cwd=str(repo), input=body,
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().replace("\n", " ")
        raise Blocked([
            f"ticket {reference}: the tracker refused the {operation} — {detail}; check that this"
            " checkout is the repository the issue belongs to, and that `gh` is authenticated"
        ])


def edit_body(kind, repo, ticket, body):
    """**edit** — replace that ticket's body with the new text, as one atomic replacement.

    A body the tracker already holds is not written again: on github that is a call saved, and on
    the local tracker, where the ticket is a file in the repository, it is the difference between a
    re-staging that leaves the tracked tree as it found it and one that dirties it for nothing.
    """
    if body == ticket["body"]:
        return
    if kind == driver.TRACKER_GITHUB:
        gh_write(repo, ["edit", ticket["id"], "--body-file", "-"], ticket["id"], "edit", body=body)
    else:
        ticket["path"].write_text(body, encoding="utf-8")
    ticket["body"] = body
    ticket["text"] = staged_text(kind, ticket["title"], body, ticket.get("url"))


def local_marked(text, role):
    """That ticket file with its `Status:` line carrying the role, added where it carried none."""
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        match = driver.STATUS_LINE.match(line.rstrip("\n"))
        if match:
            lines[index] = f"{match.group(1)}Status: {role}\n"
            return "".join(lines)
    return text.rstrip("\n") + f"\n\nStatus: {role}\n"


def mark(kind, repo, ticket, role):
    """**mark** — declare who may pick that ticket up, and take the other role off it."""
    other = HUMAN_ROLE if role == AGENT_ROLE else AGENT_ROLE
    if kind == driver.TRACKER_GITHUB:
        carried = [
            entry.get("name") for entry in gh_read(repo, ticket["id"], fields="labels")["labels"]
        ]
        arguments = []
        if role not in carried:
            arguments += ["--add-label", role]
        if other in carried:
            arguments += ["--remove-label", other]
        if arguments:
            gh_write(repo, ["edit", ticket["id"], *arguments], ticket["id"], "mark")
        return
    edit_body(kind, repo, ticket, local_marked(ticket["body"], role))


# --- the approved routing --------------------------------------------------------------------


def read_routing(path):
    """The approved routing table, keyed by ticket number; raises Blocked where it is not one."""
    if not path:
        return {}
    try:
        text = pathlib.Path(path).read_text(encoding="utf-8")
    except OSError as error:
        raise Blocked([
            f"approved routing: {path} could not be read — {error}; pass the file the approved"
            " table was written to"
        ]) from error
    try:
        table = json.loads(text)
    except ValueError as error:
        raise Blocked([
            f"approved routing: {path} is not readable JSON — {error}; pass an object keyed by"
            " ticket number, each entry carrying the approved routing's lines"
        ]) from error
    if not isinstance(table, dict) or not all(isinstance(entry, dict) for entry in table.values()):
        raise Blocked([
            f"approved routing: {path} is not an object keyed by ticket number, each entry an"
            " object carrying the approved routing's lines"
        ])
    return {str(key): entry for key, entry in table.items()}


def routing_faults(table, tickets):
    """Everything the approved routing and the ticket set disagree on, one problem each."""
    problems = []
    inside = {ticket["id"] for ticket in tickets}
    for key in sorted(table, key=lambda number: (not number.isdigit(), number)):
        if key not in inside:
            problems.append(
                f"approved routing: it names ticket {key}, which is not in this ticket set — stage"
                " that ticket too, or drop it from the approved routing"
            )
    for ticket in tickets:
        entry = table.get(ticket["id"])
        if entry is None:
            problems.append(
                f"ticket {ticket['id']}: the approved routing carries no entry for it — every"
                " ticket of the set is routed, or none is"
            )
            continue
        missing = [
            key for key in ROUTING_ORDER
            if key not in ROUTING_OPTIONAL and not str(entry.get(key, "")).strip()
        ]
        for key in missing:
            problems.append(
                f"ticket {ticket['id']}: the approved routing carries no {key} — the `## Routing`"
                f" section takes a {key.capitalize()} line, so add it to the approved entry"
            )
    return problems


def routing_section(entry):
    """The `## Routing` section classify.md templates, rendered from that approved entry."""
    lines = [
        f"{key.capitalize()}: {str(entry[key]).strip()}"
        for key in ROUTING_ORDER if str(entry.get(key, "")).strip()
    ]
    return ROUTING_HEADING + "\n\n" + "\n".join(lines) + "\n"


def with_routing(body, section):
    """That body carrying exactly this `## Routing` section, replacing the one it had.

    The section ends the ticket, so what follows a replaced one is whatever the tracker keeps after
    it — the next `##` heading, or the local tracker's `Status:` line — and that is kept.
    """
    lines = body.splitlines(keepends=True)
    written = []
    index = 0
    replaced = False
    while index < len(lines):
        heading = run_plan.SECTION.match(lines[index].rstrip("\n"))
        if heading and heading.group(1).lower() == run_plan.ROUTING_SECTION:
            written.append(section.rstrip("\n") + "\n\n")
            replaced = True
            index += 1
            while index < len(lines):
                line = lines[index].rstrip("\n")
                if run_plan.SECTION.match(line) or driver.STATUS_LINE.match(line):
                    break
                index += 1
            continue
        written.append(lines[index])
        index += 1
    if not replaced:
        written.append("\n" + section)
    return "".join(written).rstrip("\n") + "\n"


def role_of(entry):
    """Which role string that routing's workflow names."""
    workflow = str(entry.get("workflow", "")).strip().lower()
    return HUMAN_ROLE if workflow == HUMAN_WORKFLOW else AGENT_ROLE


def write_routing(kind, repo, tickets, table):
    """Write each approved `## Routing` section and role label back to the tracker.

    The staged text is taken from the same value, so the run directory and the tracker say the same
    thing about how a ticket is routed. What is checked here is only that the approved table and
    the ticket set are about each other, and that every entry carries the lines the section takes:
    whether `tdd` is a workflow and `medium` an effort is the renderer's verdict at the self-check,
    which is this script's one authority on a valid routing everywhere else too.
    """
    if not table:
        return
    problems = routing_faults(table, tickets)
    if problems:
        raise Blocked(problems)
    for ticket in tickets:
        entry = table[ticket["id"]]
        edit_body(kind, repo, ticket, with_routing(ticket["body"], routing_section(entry)))
        mark(kind, repo, ticket, role_of(entry))


# --- the run directory -----------------------------------------------------------------------


def provenance(kind, parent, tickets):
    """The line that says where this directory came from, and so which re-run owns it.

    A parent names the run on its own: its sub-issues are the tracker's to change between two
    routings, and a re-run for that parent has to find the directory the first one wrote whatever
    the set has become. A parentless set is named by the tickets themselves, which are all it is.
    """
    if parent is not None:
        return f"{kind} parent #{parent}"
    numbers = sorted(tickets, key=lambda ticket: int(ticket["id"]))
    return f"{kind} " + " ".join(f"#{ticket['id']}" for ticket in numbers)


def ticket_listing(plan):
    """The Run plan's ticket order and Wave membership, rendered without reinterpreting either."""
    return "\n".join(
        f"- #{ticket.id} — {ticket.title} — Wave {wave.number}"
        for wave in plan.waves for ticket in wave.tickets
    )


def reference_description(path, ticket_titles, adr_root):
    """One indexed file's ticket title, Markdown heading, or non-ADR file name."""
    if path in ticket_titles:
        return ticket_titles[path]
    is_adr = bounded_read.path_is_below(path, adr_root)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        if not is_adr:
            return path.name
        raise Blocked([f"reference index: ADR {path} could not be read — {error}"]) from error
    for line in lines:
        if line.startswith("# ") and line[2:].strip():
            return line[2:].strip()
    if not is_adr:
        return path.name
    raise Blocked([
        f"reference index: ADR {path} has no `# ` heading — add its one-line description as the"
        " document's first level-one heading"
    ])


def reference_listing(plan, directory):
    """The files the shared bounded-read definition places in this run's Reference index."""
    ticket_titles = {
        pathlib.Path(ticket.path).resolve(): ticket.title for ticket in plan.tickets
    }
    adr_root = directory.resolve().parent.parent / "docs" / "adr"
    return "\n".join(
        f"- {path} — {reference_description(path, ticket_titles, adr_root)}"
        for path in bounded_read.reference_index_paths(directory)
    )


def spec_sections(plan, directory):
    """The two generated sections shared by both staging modes."""
    return (
        "## Tickets\n"
        "\n"
        f"{ticket_listing(plan)}\n"
        "\n"
        "## Reference index\n"
        "\n"
        f"{reference_listing(plan, directory)}\n"
    )


def parent_page(line, parent, plan, directory):
    """The `spec.md` of a parent run: tracker provenance and the validated Run plan."""
    return (
        f"# {parent['title']}\n"
        "\n"
        f"{ticket_pointer(parent['url'])}\n"
        "\n"
        f"{PROVENANCE_KEY} {line}\n"
        "\n"
        f"{spec_sections(plan, directory)}"
    )


def cover_page(line, plan, directory):
    """The generated `spec.md` for a ticket set with no parent to take a spec from."""
    return (
        f"# Staged run of {len(plan.tickets)}"
        f" ticket{'s' if len(plan.tickets) != 1 else ''}\n"
        "\n"
        f"{PROVENANCE_KEY} {line}\n"
        "\n"
        "This page is written by the staging script, and there is no parent spec behind it. Each\n"
        "ticket in this run is self-contained: its own brief is the whole context, and nothing\n"
        "here adds to it.\n"
        "\n"
        f"{spec_sections(plan, directory)}"
    )


def provenance_page(line):
    """The allocation marker kept while the newly materialised ticket set is validated."""
    return f"{PROVENANCE_KEY} {line}\n"


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


def materialise(directory, spec, tickets, dependencies, stripped):
    """Write the run directory's `spec.md` and one file per ticket; returns nothing.

    A directory being re-staged keeps its subdirectories: a run already started there holds its
    machine log and wave table under `.crew`, and refreshing the tickets is no reason to destroy
    the record of what was done with them.
    """
    directory.mkdir(parents=True, exist_ok=True)
    for path in directory.glob("*.md"):
        path.unlink()
    (directory / SPEC_NAME).write_text(spec, encoding="utf-8")
    for ticket in tickets:
        (directory / f"{ticket['id']}.md").write_text(
            project_edges(ticket["text"], dependencies[ticket["id"]], stripped), encoding="utf-8"
        )


# --- the self-check --------------------------------------------------------------------------


def candidate_run(repo, directory, config):
    """The run section the driver would build for this directory, for validation only."""
    args = argparse.Namespace(
        spec=None,
        codex_bridge=None,
        coordinator_name=LATER,
        coordinator_pid=os.getpid(),
        coordinator_session=LATER,
        coordinator_address=LATER,
        tmux_session=LATER,
        permission_mode=LATER,
    )
    head = driver.git_output(repo, "rev-parse", "HEAD") or LATER
    return driver.run_section(
        args, repo, directory, run_plan.crew_state_dir(directory),
        base_branch=None, base_commit=head, config=config,
    )


def self_check(repo, directory, config):
    """The checked Run plan and every blocking item, in the real run checks' own words."""
    run = candidate_run(repo, directory, config)
    problems = driver.dirty_tree_problems(repo)
    plan = None
    base_branch = driver.default_base_branch(repo)
    if base_branch is None:
        # Resolving the selected local ref belongs to run start; staging only checks default naming.
        problems += driver.base_branch_problems(repo, base_branch)
    try:
        plan = run_plan.build(directory, run)
    except run_plan.RunPlanError as error:
        problems += list(error.problems)
    return plan, problems


def print_waves(plan):
    """The derived wave layout, one operator-facing stderr line per wave."""
    for wave in plan.waves:
        tickets = ", ".join(f"#{ticket.id}" for ticket in wave.tickets)
        print(f"wave {wave.number}: {tickets}", file=sys.stderr)


def print_stubs(kind, tickets):
    """One informational stderr line per GitHub stub, in ticket-number order."""
    if kind != driver.TRACKER_GITHUB:
        return
    for ticket in sorted(tickets, key=lambda held: int(held["id"])):
        print(
            f"ticket #{ticket['id']}: stubbed {ticket['url']}"
            f" ({ticket['comment_count']} comments)",
            file=sys.stderr,
        )


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
        "witness_model": driver.config_value(config, driver.WITNESS_MODEL_KEYS),
        "witness_budget_usd": driver.config_value(config, driver.WITNESS_BUDGET_KEYS),
        "tracker": driver.config_value(config, driver.TRACKER_KIND_KEYS),
    }
    problems = run_plan.configuration_problems(
        repo,
        run["repair_model"],
        run["witness_model"],
        run["witness_budget_usd"],
        run["tracker"],
    )
    if run["tracker"] not in run_plan.TRACKERS:
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
            "the parent ticket to stage, expanded into its open native sub-issues; the run's"
            " `spec.md` then points at the live parent, and the staged command is commented there"
        ),
    )
    parser.add_argument(
        "--routing", metavar="FILE",
        help=(
            "the routing the user approved, as JSON keyed by ticket number, each entry carrying"
            " the `## Routing` lines (workflow, executor, model, effort, reasons, review where"
            " the workflow takes one, and account where the user named one); it is written back to"
            " the tracker with each ticket's role label, and nothing is written to the tracker"
            " without it"
        ),
    )
    parser.add_argument(
        "--repo-root", help="the repository to stage into; default the one this is run in",
    )
    return parser


def expand_parent(kind, repo, parent):
    """The parent ticket and the open sub-issues that are this run's set.

    A closed sub-issue is work already finished, so it stays out of the set; an edge to it is met
    by the closure resolution, which reads it from the tracker as satisfied.
    """
    if kind != driver.TRACKER_GITHUB:
        raise Blocked([
            f"parent {parent}: the {kind} tracker has no sub-issues to expand — pass the ticket set"
            " itself, which is what a ticket is on this tracker"
        ])
    ticket = read_ticket(kind, repo, parent)
    numbers = sub_issue_numbers(repo, parent)
    tickets = [read_ticket(kind, repo, number) for number in numbers]
    open_tickets = [child for child in tickets if not child["closed"]]
    if not open_tickets:
        raise Blocked([
            f"parent {parent}: it has no open sub-issues to stage — link the tickets to it as"
            " sub-issues, or pass the ticket set itself"
        ])
    return ticket, open_tickets


def stage(args):
    """Stage that ticket set; returns the `/crew` command for the run directory it wrote."""
    repo = repository_root(args.repo_root)
    config = driver.project_config(repo)
    kind = tracker_kind(repo, config)
    table = read_routing(args.routing)

    if args.parent:
        parent, tickets = expand_parent(kind, repo, args.parent)
        references = [ticket["id"] for ticket in tickets]
    else:
        parent = None
        # A reference repeated in the set names one ticket, not two: the directory holds one file
        # per ticket, and the cover page and the provenance line have to say the same.
        references = list(dict.fromkeys(args.tickets))
        tickets = [read_ticket(kind, repo, reference) for reference in references]

    write_routing(kind, repo, tickets, table)
    dependencies, stripped = resolve_closure(
        kind, repo, tickets, ticket_sources(kind, repo, references)
    )
    line = provenance(kind, args.parent, tickets)
    number, directory = allocate(repo / RUN_ROOT, line)
    materialise(directory, provenance_page(line), tickets, dependencies, stripped)

    plan, problems = self_check(repo, directory, config)
    if problems:
        raise Blocked(problems)
    spec = (
        parent_page(line, parent, plan, directory)
        if parent else cover_page(line, plan, directory)
    )
    (directory / SPEC_NAME).write_text(spec, encoding="utf-8")
    print_stubs(kind, [*tickets, *([parent] if parent else [])])
    print_waves(plan)

    # Only now, with the self-check green, does the pickup point go on the tracker: a command that
    # would not start is one nobody should be able to find later. A staging given no approved
    # routing writes nothing to the tracker at all, and the comment is a tracker write like the
    # others — that entrance stages a directory and says whether it starts, nothing more.
    command = f"/crew {RUN_ROOT}/{number}"
    pickup_prefix = f"/crew {RUN_ROOT}/"
    if table:
        for ticket in [parent] if parent else tickets:
            try:
                tracker.comment(kind, ticket, command, supersedes=pickup_prefix)
            except tracker.TrackerError as error:
                raise Blocked([str(error)]) from error
    return command


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.parent and args.tickets:
        parser.error(
            "--parent and a ticket set are two entrances, not one — pass the parent to stage its"
            " sub-issues, or the tickets to stage exactly those"
        )
    if not args.parent and not args.tickets:
        parser.error("no tickets to stage — pass the ticket set this run is made of, or --parent")
    try:
        command = stage(args)
    except Blocked as blocked:
        print(
            f"staging stopped on {len(blocked.problems)} blocking"
            f" item{'s' if len(blocked.problems) != 1 else ''}:",
            file=sys.stderr,
        )
        for problem in blocked.problems:
            print(f"- {problem}", file=sys.stderr)
        return BLOCKED_EXIT
    print(command)
    return 0


if __name__ == "__main__":
    sys.exit(main())
