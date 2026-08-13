#!/usr/bin/env python3
"""A Claude CLI stand-in for the monitor tests.

`agents --json` prints the agents list the fixture put in `AGENTCREW_STUB_DIR/agents.json`, and
fails the way the real CLI does when that file is absent. Every invocation is appended to
`claude-calls.jsonl` there, so a test can assert the monitor never asks this CLI for anything but a
snapshot — the monitor must not reach any channel a model reads.
"""

import json
import os
import pathlib
import sys


def state_dir():
    return pathlib.Path(os.environ["AGENTCREW_STUB_DIR"])


def main():
    argv = sys.argv[1:]
    with (state_dir() / "claude-calls.jsonl").open("a") as handle:
        handle.write(json.dumps({"argv": argv}) + "\n")

    if argv == ["agents", "--json"]:
        path = state_dir() / "agents.json"
        if not path.exists():
            print("claude: no agents list", file=sys.stderr)
            return 1
        print(path.read_text())
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
