#!/usr/bin/env python3
"""Tracker comment behavior shared by route staging and the Crew Driver."""

import json
from collections.abc import Mapping
import pathlib
import subprocess


GITHUB = "github"
LOCAL = "local"
CREW_KEY = "Crew:"
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
    return subprocess.run(arguments, cwd=str(cwd), capture_output=True, text=True)


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


def comment(run_tracker_fact, ticket_fact, body, *, supersedes=None):
    """Comment idempotently, optionally supersede by caller prefix, and return a locator."""
    if not isinstance(body, str) or not body.strip():
        raise TrackerError("tracker comment body is empty")
    if supersedes is not None and not isinstance(supersedes, str):
        raise TrackerError("tracker comment supersedes prefix is not a string")
    if run_tracker_fact == GITHUB:
        return _github_comment(ticket_fact, body, supersedes)
    if run_tracker_fact == LOCAL:
        return _local_comment(ticket_fact, body, supersedes)
    raise TrackerError(f"tracker kind {run_tracker_fact!r} does not support comment")
