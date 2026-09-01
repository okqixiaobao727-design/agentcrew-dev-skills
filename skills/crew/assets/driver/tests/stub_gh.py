#!/usr/bin/env python3
"""A `gh` stand-in for the driver tests, standing for a repository whose tickets are issues.

Every call is appended to `AGENTCREW_STUB_DIR/gh-calls.jsonl` with the directory it was made in,
because that directory is the whole of how a real `gh` knows which repository it is talking to: a
call carrying a filesystem path where the CLI wants an `OWNER/REPO` slug is the failure this stub
has to be able to show.

Issues live in `gh-issues.json` there, keyed by number, each carrying its `labels`, `comments` and
whether it is `closed`. `issue view` prints labels or comments, `issue comment` appends one and
prints its locator, `issue edit --remove-label` takes one off, and `issue close` closes it. Anything
else exits 1, as `gh` does on a subcommand it has no answer for.
"""

import json
import os
import pathlib
import sys


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


def issue(argv):
    table = issues()
    number = argv[2] if len(argv) > 2 else None
    record = table.setdefault(str(number), {"labels": [], "closed": False})
    action = argv[1]

    if action == "view":
        field = flag(argv, "--json")
        if field == "labels":
            print(json.dumps({"labels": [{"name": name} for name in record["labels"]]}))
            return 0
        if field == "comments":
            issue_url = record.get("url", f"https://github.example.invalid/issues/{number}")
            print(json.dumps({"comments": [
                {
                    "id": f"IC_{number}_{index}",
                    "body": body,
                    "url": f"{issue_url}#issuecomment-{index}",
                }
                for index, body in enumerate(record.get("comments", []), 1)
            ]}))
            return 0
        return 1
    if action == "comment":
        if (state_dir() / "gh-comment-fails").exists():
            print("comment refused by tracker", file=sys.stderr)
            return 1
        body = flag(argv, "--body")
        if body is None:
            return 1
        record["comments"] = list(record.get("comments", [])) + [body]
        save(table)
        issue_url = record.get("url", f"https://github.example.invalid/issues/{number}")
        print(f"{issue_url}#issuecomment-{len(record['comments'])}")
        return 0
    if action == "edit":
        removed = flag(argv, "--remove-label")
        added = flag(argv, "--add-label")
        if removed is not None:
            record["labels"] = [name for name in record["labels"] if name != removed]
        if added is not None:
            record["labels"] += [name for name in added.split(",") if name]
        save(table)
        return 0
    if action == "close":
        if (state_dir() / "gh-close-fails").exists():
            print("stub issue close failed", file=sys.stderr)
            return 1
        record["closed"] = True
        save(table)
        return 0
    return 1


def main():
    argv = sys.argv[1:]
    with (state_dir() / "gh-calls.jsonl").open("a") as handle:
        handle.write(json.dumps({"argv": argv, "cwd": os.getcwd()}) + "\n")
    if argv and argv[0] == "issue" and len(argv) > 1:
        return issue(argv)
    return 1


if __name__ == "__main__":
    sys.exit(main())
