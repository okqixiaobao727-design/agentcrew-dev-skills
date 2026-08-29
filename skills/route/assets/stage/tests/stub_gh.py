#!/usr/bin/env python3
"""A `gh` stand-in for the staging tests, standing for a tracker whose tickets are issues.

The driver's own `gh` stub answers the **close** operation — labels and closure — and knows nothing
of a ticket's text, which is the whole of what staging reads. This one answers the operations
staging calls, over the issues in `AGENTCREW_STUB_DIR/gh-issues.json`, keyed by number:

- **read** — `issue view <n> --json number,title,body,state`, and `--json labels` / `--json
  comments` for the two the write operations have to read before they write.
- **edit** — `issue edit <n> --body-file -`, replacing the body with what stdin carries.
- **mark** — `issue edit <n> --add-label/--remove-label`, the pickup labels.
- **comment** — `issue comment <n> --body <text>`, appended to the issue's comments.
- **sub-issue expansion** — `api repos/{owner}/{repo}/issues/<n>/sub_issues`, the native list, in
  the shape the REST endpoint answers with: an array of issues carrying at least `number`. An issue
  carrying a `page_size` has that list answered one JSON document per page, which is the other shape
  a paginated `gh api` call comes back in.
- **dependency read** — `api repos/{owner}/{repo}/issues/<n>/dependencies/blocked_by`, answered
  from the issue record's `blocked_by` list in the same issue-array and pagination shapes.

An issue the table does not hold exits 1 with the message `gh` gives for one, because a ticket set
naming an issue nobody can read is a case staging has to report rather than stage.

Every call is appended to `AGENTCREW_STUB_DIR/gh-calls.jsonl` with the directory it was made in:
that directory is how a real `gh` resolves which repository it is talking to.
"""

import json
import os
import pathlib
import re
import sys


SUB_ISSUES = re.compile(r"issues/(\d+)/sub_issues$")
BLOCKED_BY = re.compile(r"issues/(\d+)/dependencies/blocked_by$")


def state_dir():
    return pathlib.Path(os.environ["AGENTCREW_STUB_DIR"])


def issues_path():
    return state_dir() / "gh-issues.json"


def issues():
    path = issues_path()
    return json.loads(path.read_text()) if path.exists() else {}


def save(table):
    issues_path().write_text(json.dumps(table))


def flag(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


def known_fields(number, record):
    return {
        "number": int(number),
        "title": record.get("title", ""),
        "body": record.get("body", ""),
        "state": record.get("state", "OPEN"),
        "labels": [{"name": name} for name in record.get("labels", [])],
        "comments": [{"body": body} for body in record.get("comments", [])],
    }


def view(argv, number, record):
    fields = (flag(argv, "--json") or "").split(",")
    known = known_fields(number, record)
    missing = [field for field in fields if field not in known]
    if missing:
        print(f"unknown JSON field: {missing[0]}", file=sys.stderr)
        return 1
    print(json.dumps({field: known[field] for field in fields}))
    return 0


def edit(argv, table, number, record):
    if flag(argv, "--body-file") == "-":
        record["body"] = sys.stdin.read()
    added = flag(argv, "--add-label")
    removed = flag(argv, "--remove-label")
    labels = list(record.get("labels", []))
    if removed is not None:
        labels = [name for name in labels if name != removed]
    if added is not None:
        labels += [name for name in added.split(",") if name and name not in labels]
    record["labels"] = labels
    table[str(number)] = record
    save(table)
    return 0


def comment(argv, table, number, record):
    body = flag(argv, "--body")
    if body is None:
        return 1
    record["comments"] = list(record.get("comments", [])) + [body]
    table[str(number)] = record
    save(table)
    return 0


def issue(argv):
    action = argv[1]
    if action not in ("view", "edit", "comment"):
        return 1
    number = argv[2] if len(argv) > 2 else None
    table = issues()
    record = table.get(str(number))
    if record is None:
        print(f"could not resolve to an Issue with the number of {number}", file=sys.stderr)
        return 1
    if action == "view":
        return view(argv, number, record)
    if action == "edit":
        return edit(argv, table, number, record)
    return comment(argv, table, number, record)


def api(argv):
    path = argv[-1]
    match = SUB_ISSUES.search(path) or BLOCKED_BY.search(path)
    if not match:
        print(f"no stub answer for {path}", file=sys.stderr)
        return 1
    table = issues()
    record = table.get(match.group(1))
    if record is None:
        print(f"could not resolve to an Issue with the number of {match.group(1)}", file=sys.stderr)
        return 1
    relation = "sub_issues" if SUB_ISSUES.search(path) else "blocked_by"
    listed = [
        # The REST endpoint answers with whole issues, whose `state` is lowercase there.
        {"number": int(number), "state": table.get(str(number), {}).get("state", "OPEN").lower()}
        for number in record.get(relation, [])
    ]
    # `gh --paginate` merges an array response's pages into one array; where it does not, each page
    # is its own JSON document on the same stream. `page_size` asks for the second shape, so a
    # caller can be held to reading both.
    size = record.get("page_size")
    if not size:
        print(json.dumps(listed))
        return 0
    for start in range(0, len(listed), size) or [0]:
        print(json.dumps(listed[start:start + size]))
    return 0


def main():
    argv = sys.argv[1:]
    with (state_dir() / "gh-calls.jsonl").open("a") as handle:
        handle.write(json.dumps({"argv": argv, "cwd": os.getcwd()}) + "\n")
    if argv and argv[0] == "issue" and len(argv) > 1:
        return issue(argv)
    if argv and argv[0] == "api" and len(argv) > 1:
        return api(argv)
    return 1


if __name__ == "__main__":
    sys.exit(main())
