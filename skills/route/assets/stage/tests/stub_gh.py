#!/usr/bin/env python3
"""A `gh` stand-in for the staging tests, standing for a tracker whose tickets are issues.

The driver's own `gh` stub answers the **close** operation — labels and closure — and knows nothing
of a ticket's text, which is the whole of what staging reads. This one answers the **read**
operation instead: `issue view <n> --json number,title,body,state` over the issues in
`AGENTCREW_STUB_DIR/gh-issues.json`, keyed by number. An issue the table does not hold exits 1 with
the message `gh` gives for one, because a ticket set naming an issue nobody can read is a case
staging has to report rather than stage.

Every call is appended to `AGENTCREW_STUB_DIR/gh-calls.jsonl` with the directory it was made in:
that directory is how a real `gh` resolves which repository it is talking to.
"""

import json
import os
import pathlib
import sys


def state_dir():
    return pathlib.Path(os.environ["AGENTCREW_STUB_DIR"])


def issues():
    path = state_dir() / "gh-issues.json"
    return json.loads(path.read_text()) if path.exists() else {}


def flag(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


def issue(argv):
    if argv[1] != "view":
        return 1
    number = argv[2] if len(argv) > 2 else None
    record = issues().get(str(number))
    if record is None:
        print(f"could not resolve to an Issue with the number of {number}", file=sys.stderr)
        return 1
    fields = (flag(argv, "--json") or "").split(",")
    known = {
        "number": int(number),
        "title": record.get("title", ""),
        "body": record.get("body", ""),
        "state": record.get("state", "OPEN"),
    }
    missing = [field for field in fields if field not in known]
    if missing:
        print(f"unknown JSON field: {missing[0]}", file=sys.stderr)
        return 1
    print(json.dumps({field: known[field] for field in fields}))
    return 0


def main():
    argv = sys.argv[1:]
    with (state_dir() / "gh-calls.jsonl").open("a") as handle:
        handle.write(json.dumps({"argv": argv, "cwd": os.getcwd()}) + "\n")
    if argv and argv[0] == "issue" and len(argv) > 1:
        return issue(argv)
    return 1


if __name__ == "__main__":
    sys.exit(main())
