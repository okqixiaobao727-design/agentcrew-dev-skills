#!/usr/bin/env python3
"""A Claude CLI stand-in for the merge driver tests: the repair session the ladder launches.

Every invocation records its argv and working directory in `AGENTCREW_STUB_DIR/repairs.jsonl`,
which is how the tests read the command the driver composed. What it then does to the conflicted
worktree is `AGENTCREW_STUB_REPAIR` :

    resolve  strip the conflict markers and keep both sides — a repair that worked. It stages
             nothing, because the brief tells the session to run no git command at all.
    noop     leave the conflict exactly as it was and exit 0 — a session that spent its budget
             without fixing anything
    fail     exit nonzero without touching the tree
    half     drop only the opening marker, leaving the rest of the hunk standing — a session that
             believes it resolved the file and did not
    stray    resolve the conflict and also write a file it was never handed

`AGENTCREW_STUB_REPAIR_SEQUENCE` overrides that per attempt with a comma-separated list, so a
first attempt that fails and a second that succeeds can be fixtured.
"""

import json
import os
import pathlib
import subprocess
import sys

OURS = "<<<<<<<"
BASE = "|||||||"
SPLIT = "======="
THEIRS = ">>>>>>>"

# The file the `stray` behaviour writes: a change outside the conflict it was handed.
STRAY_FILE = "outside.txt"


def state_dir():
    return pathlib.Path(os.environ["AGENTCREW_STUB_DIR"])


def record(argv, cwd):
    """Append this invocation and return how many have been made, this one included."""
    path = state_dir() / "repairs.jsonl"
    with path.open("a") as handle:
        handle.write(json.dumps({"argv": argv, "cwd": cwd}) + "\n")
    return len(path.read_text().splitlines())


def behaviour(attempt):
    sequence = os.environ.get("AGENTCREW_STUB_REPAIR_SEQUENCE")
    if sequence:
        steps = sequence.split(",")
        return steps[min(attempt, len(steps)) - 1]
    return os.environ.get("AGENTCREW_STUB_REPAIR", "resolve")


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, text=True, check=True
    ).stdout


def conflicted_paths(repo):
    listed = git(repo, "diff", "--name-only", "--diff-filter=U").splitlines()
    return [path for path in listed if path]


def keep_both_sides(text):
    """The file with its conflict markers gone and both sides' lines kept, ours first."""
    kept = []
    in_base = False
    for line in text.splitlines(keepends=True):
        marker = line.split(" ", 1)[0].rstrip("\n")
        if marker == BASE:
            in_base = True
        elif marker in (SPLIT, THEIRS):
            in_base = False
        elif marker == OURS:
            in_base = False
        elif not in_base:
            kept.append(line)
    return "".join(kept)


def main():
    argv = sys.argv[1:]
    repo = os.getcwd()
    attempt = record(argv, repo)
    step = behaviour(attempt)
    if step == "fail":
        return 1
    if step == "noop":
        return 0
    for relative in conflicted_paths(repo):
        path = pathlib.Path(repo) / relative
        text = path.read_text()
        if step == "half":
            path.write_text(
                "".join(
                    line for line in text.splitlines(keepends=True)
                    if line.split(" ", 1)[0].rstrip("\n") != OURS
                )
            )
            continue
        path.write_text(keep_both_sides(text))
    if step == "stray":
        (pathlib.Path(repo) / STRAY_FILE).write_text("work nobody asked for\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
