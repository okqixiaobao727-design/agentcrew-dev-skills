#!/usr/bin/env python3
"""Tracker ticket operations shared by route staging and the Crew Driver."""

import json
from collections.abc import Mapping
import pathlib
import subprocess

import run_plan


GITHUB = "github"
LOCAL = "local"
CREW_KEY = "Crew:"
# The two roles **mark** declares, which is what a created ticket's role label is one of
# (`references/trackers.md`): a github label, and the local tracker's `Status:` line.
ROLE_LABELS = ("ready-for-agent", "ready-for-human")
STATUS_KEY = "Status:"
# A local ticket file, which is the whole of what a local ticket is. The Run plan owns what a
# ticket file is named, and every reader of that rule reads it from there.
TICKET_FILE = run_plan.TICKET_FILE
# **create** takes two looks before it decides the ticket is not already there, because neither
# alone is enough. The first reads the newest open issues from the list API, which is consistent
# the moment an issue is opened but reaches back only this far — it answers a retry seconds after
# a crash. The second goes through the search index, which lags a fresh issue but is bounded only
# by age, and answers a ticket opened long enough ago to have scrolled out of the first.
GITHUB_RECENT_LOOK = "100"
# The search look is scoped to the title, so what comes back is the candidates that share it
# rather than the repository's whole open set, and it is read to GitHub's own search cap: past
# that the bound on this operation would be a number of ours rather than the platform's.
GITHUB_TITLE_SEARCH = "in:title"
GITHUB_SEARCH_CAP = "1000"
UPDATE_COMMENT_MUTATION = (
    "mutation($id: ID!, $body: String!) {"
    " updateIssueComment(input: {id: $id, body: $body}) { issueComment { url } }"
    " }"
)


class TrackerError(Exception):
    """A tracker operation the selected adapter could not complete."""


def _value(fact, name):
    """One uninterpreted ticket fact value, whether loaded or still being staged."""
    if isinstance(fact, Mapping):
        return fact.get(name)
    return getattr(fact, name, None)


def _working_directory(ticket):
    """A directory inside the ticket's repository, with no ambient-cwd fallback."""
    path = _value(ticket, "path")
    if path:
        return pathlib.Path(path).resolve().parent
    repository = _value(ticket, "repository")
    if repository:
        return pathlib.Path(repository).resolve()
    raise TrackerError("tracker ticket carries no repository directory")


def _run(arguments, *, cwd):
    """Run one tracker CLI operation at the private process seam."""
    return subprocess.run(
        arguments, cwd=str(cwd) if cwd is not None else None, capture_output=True, text=True
    )


def _failure(result, reference, operation):
    detail = (result.stderr or result.stdout).strip().replace("\n", " ")
    return TrackerError(
        f"ticket {reference}: the tracker refused the {operation} — {detail or 'no detail'}"
    )


def _github_comment(ticket, body, supersedes):
    reference = str(_value(ticket, "id") or "")
    if not reference.isdigit():
        raise TrackerError(
            f"ticket {reference or '(missing)'}: is not a GitHub issue number"
        )
    cwd = _working_directory(ticket)
    viewed = _run(
        ["gh", "issue", "view", reference, "--json", "comments"],
        cwd=cwd,
    )
    if viewed.returncode != 0:
        raise _failure(viewed, reference, "comment read")
    try:
        held = json.loads(viewed.stdout).get("comments") or []
    except (AttributeError, ValueError) as error:
        raise TrackerError(
            f"ticket {reference}: the tracker's comments were not readable — {error}"
        ) from error
    for entry in held:
        if entry.get("body") != body:
            continue
        locator = str(entry.get("url") or "").strip()
        if not locator:
            raise TrackerError(
                f"ticket {reference}: the identical tracker comment has no locator"
            )
        return locator

    if supersedes is not None:
        replaced = next(
            (
                entry for entry in held
                if str(entry.get("body") or "").startswith(supersedes)
            ),
            None,
        )
        if replaced is not None:
            node = str(replaced.get("id") or "").strip()
            locator = str(replaced.get("url") or "").strip()
            if not node or not locator:
                raise TrackerError(
                    f"ticket {reference}: the superseded tracker comment has no identity"
                )
            written = _run(
                [
                    "gh", "api", "graphql",
                    "-f", f"query={UPDATE_COMMENT_MUTATION}",
                    "-f", f"id={node}",
                    "-f", f"body={body}",
                ],
                cwd=cwd,
            )
            if written.returncode != 0:
                raise _failure(written, reference, "comment replacement")
            return locator

    written = _run(
        ["gh", "issue", "comment", reference, "--body", body],
        cwd=cwd,
    )
    if written.returncode != 0:
        raise _failure(written, reference, "comment")
    lines = [line.strip() for line in written.stdout.splitlines() if line.strip()]
    if not lines:
        raise TrackerError(
            f"ticket {reference}: the tracker wrote the comment but returned no locator"
        )
    return lines[-1]


def _local_block(body):
    suffix = "" if body.endswith("\n") else "\n"
    return f"{CREW_KEY} {body}{suffix}"


def _block_line(text, block):
    """The one-based line where an exact Crew comment block starts, or None."""
    start = 0
    while True:
        position = text.find(block, start)
        if position < 0:
            return None
        if position == 0 or text[position - 1] == "\n":
            return text.count("\n", 0, position) + 1
        start = position + 1


def _without_superseded_comment(text, prefix):
    """Remove the first local Crew comment whose body starts with the caller's prefix."""
    needle = f"{CREW_KEY} {prefix}"
    start = 0
    while True:
        position = text.find(needle, start)
        if position < 0:
            return text
        if position == 0 or text[position - 1] == "\n":
            break
        start = position + 1
    following = text.find(f"\n\n{CREW_KEY} ", position + len(needle))
    end = len(text) if following < 0 else following + 2
    return text[:position] + text[end:]


def _local_comment(ticket, body, supersedes):
    path_value = _value(ticket, "path")
    if not path_value:
        raise TrackerError("local tracker ticket: carries no staged path")
    path = pathlib.Path(path_value).resolve()
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise TrackerError(f"local tracker ticket {path}: is not readable — {error}") from error

    block = _local_block(body)
    line = _block_line(text, block)
    if line is not None:
        return f"{path}:{line}"

    if supersedes is not None:
        text = _without_superseded_comment(text, supersedes)

    updated = text.rstrip("\n") + f"\n\n{block}"
    line = updated.count("\n", 0, len(text.rstrip("\n")) + 2) + 1
    try:
        path.write_text(updated, encoding="utf-8")
    except OSError as error:
        raise TrackerError(
            f"local tracker ticket {path}: could not be commented — {error}"
        ) from error
    return f"{path}:{line}"


def _created_ticket(identifier, title, body, path, url, directory):
    """The one shape both adapters answer **create** with, whatever the tracker underneath is."""
    return {
        "id": identifier,
        "title": title,
        "body": body,
        "path": str(path) if path is not None else None,
        "url": url,
        "repository": str(directory) if directory is not None else None,
    }


def _github_held(title, body, directory, search):
    """The open issue this title and body already are, or None where the tracker holds none."""
    arguments = [
        "gh", "issue", "list", "--state", "open",
        "--limit", GITHUB_SEARCH_CAP if search else GITHUB_RECENT_LOOK,
        "--json", "number,title,body,url",
    ]
    if search:
        arguments += ["--search", f"{title} {GITHUB_TITLE_SEARCH}"]
    listed = _run(arguments, cwd=directory)
    if listed.returncode != 0:
        raise _failure(listed, repr(title), "ticket read")
    try:
        held = json.loads(listed.stdout)
    except ValueError as error:
        raise TrackerError(
            f"ticket {title!r}: the tracker's open issues were not readable — {error}"
        ) from error
    if not isinstance(held, list):
        raise TrackerError(f"ticket {title!r}: the tracker's open issues were not a list")
    for entry in held:
        if not isinstance(entry, Mapping):
            continue
        if entry.get("title") != title or entry.get("body") != body:
            continue
        locator = str(entry.get("url") or "").strip()
        if not locator:
            raise TrackerError(f"ticket {title!r}: the identical open issue has no locator")
        return _created_ticket(
            str(entry.get("number")), title, body, None, locator, directory
        ), locator
    return None


def _github_create(title, body, role_label, directory):
    for search in (False, True):
        found = _github_held(title, body, directory, search)
        if found is not None:
            return found

    written = _run(
        ["gh", "issue", "create", "--title", title, "--body", body, "--label", role_label],
        cwd=directory,
    )
    if written.returncode != 0:
        raise _failure(written, repr(title), "create")
    lines = [line.strip() for line in written.stdout.splitlines() if line.strip()]
    locator = lines[-1] if lines else ""
    identifier = locator.rstrip("/").rsplit("/", 1)[-1]
    if not identifier.isdigit():
        raise TrackerError(
            f"ticket {title!r}: the tracker opened the issue but returned no issue number —"
            f" {locator or 'no locator'}"
        )
    return _created_ticket(identifier, title, body, None, locator, directory), locator


def _local_head(title, body):
    """A local ticket down to its role: the title heading and the body, and nothing after them."""
    return f"# {title}\n\n{body.strip()}\n"


def _local_create(title, body, role_label, directory):
    if directory is None:
        raise TrackerError(
            "local tracker: create needs the directory the tracker's ticket files live in"
        )
    directory = pathlib.Path(directory).resolve()
    if not directory.is_dir():
        raise TrackerError(f"local tracker directory {directory}: is not there")
    head = _local_head(title, body)
    numbers = []
    for path in sorted(directory.glob("*.md")):
        match = TICKET_FILE.match(path.name)
        if not match:
            continue
        numbers.append((int(match.group(1)), len(match.group(1))))
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise TrackerError(
                f"local tracker ticket {path}: is not readable — {error}"
            ) from error
        if text.startswith(head):
            return _created_ticket(match.group(1), title, body, path, None, directory), str(path)
    # The next number, kept at the width the directory already numbers in, so the files a
    # directory listing walks stay in the order the tickets were opened in.
    width = max((digits for _, digits in numbers), default=1)
    identifier = f"{max((number for number, _ in numbers), default=0) + 1:0{width}d}"
    path = directory / f"{identifier}.md"
    try:
        path.write_text(f"{head}\n{STATUS_KEY} {role_label}\n", encoding="utf-8")
    except OSError as error:
        raise TrackerError(
            f"local tracker ticket {path}: could not be written — {error}"
        ) from error
    return _created_ticket(identifier, title, body, path, None, directory), str(path)


def _adapter(run_tracker_fact, operation, adapters):
    """The adapter that runs one operation on this run's tracker, or the refusal naming it."""
    adapter = adapters.get(run_tracker_fact)
    if adapter is None:
        raise TrackerError(
            f"tracker kind {run_tracker_fact!r} does not support {operation}"
        )
    return adapter


def create(run_tracker_fact, title, body, *, role_label, directory=None):
    """**create** — open one ticket, mark who may pick it up, and return it with its locator.

    The value handed back is the normalised ticket every other operation here accepts, and the
    locator is opaque in the same way **comment**'s is: the issue URL on github, the ticket file's
    path on local. Repeating an identical title and body opens nothing a second time — it returns
    the ticket already there — so a caller that retries after a crash cannot double-open.

    `directory` is where the ticket goes: the local tracker's ticket directory, which it requires,
    and on github a directory inside the repository the issue belongs to.
    """
    if not isinstance(title, str) or not title.strip():
        raise TrackerError("tracker ticket title is empty")
    if not isinstance(body, str) or not body.strip():
        raise TrackerError("tracker ticket body is empty")
    if role_label not in ROLE_LABELS:
        raise TrackerError(
            f"tracker ticket role {role_label!r} is not one of {', '.join(ROLE_LABELS)}"
        )
    adapter = _adapter(
        run_tracker_fact, "create", {GITHUB: _github_create, LOCAL: _local_create}
    )
    return adapter(title, body, role_label, directory)


def comment(run_tracker_fact, ticket_fact, body, *, supersedes=None):
    """Comment idempotently, optionally supersede by caller prefix, and return a locator."""
    if not isinstance(body, str) or not body.strip():
        raise TrackerError("tracker comment body is empty")
    if supersedes is not None and not isinstance(supersedes, str):
        raise TrackerError("tracker comment supersedes prefix is not a string")
    adapter = _adapter(
        run_tracker_fact, "comment", {GITHUB: _github_comment, LOCAL: _local_comment}
    )
    return adapter(ticket_fact, body, supersedes)
