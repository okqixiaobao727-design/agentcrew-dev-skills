#!/usr/bin/env python3
"""A Claude CLI stand-in for the monitor tests.

`agents --json` prints the agents list the fixture put in `AGENTCREW_STUB_DIR/agents.json`, and
fails the way the real CLI does when that file is absent. Every invocation is appended to
`claude-calls.jsonl` there, so a test can assert the monitor never asks this CLI for anything but a
snapshot — the monitor must not reach any channel a model reads.

A fixture that wrote `agents-delay` there gets an answer that many seconds late, which is how a
caller that bounds this read is tested against a source that does not answer in time.
"""

import json
import os
import pathlib
import sys
import time


def state_dir():
    return pathlib.Path(os.environ["AGENTCREW_STUB_DIR"])


def record_stdin():
    """Whatever this call could read off stdin, which a statusline's command must never consume.

    Claude Code writes its own JSON to the statusline command's stdin, so a source spawned from
    that command reading it takes it from whoever it was meant for. The file left here is empty
    when nothing was inherited, which is the whole assertion.
    """
    try:
        data = os.read(sys.stdin.fileno(), 4096)
    except OSError:
        data = b""
    (state_dir() / "claude-stdin").write_bytes(data)


def delay():
    path = state_dir() / "agents-delay"
    if path.exists():
        time.sleep(float(path.read_text().strip()))


def main():
    argv = sys.argv[1:]
    with (state_dir() / "claude-calls.jsonl").open("a") as handle:
        handle.write(json.dumps({"argv": argv}) + "\n")

    if argv == ["agents", "--json"]:
        record_stdin()
        delay()
        path = state_dir() / "agents.json"
        if not path.exists():
            print("claude: no agents list", file=sys.stderr)
            return 1
        print(path.read_text())
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
