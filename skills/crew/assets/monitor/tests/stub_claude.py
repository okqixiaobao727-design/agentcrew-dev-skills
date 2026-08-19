#!/usr/bin/env python3
"""A Claude CLI stand-in for the monitor tests.

`agents --json` prints the agents list the fixture put in `AGENTCREW_STUB_DIR/agents-<home>.json`
— one file per configuration home, because the real CLI answers this question out of the profile
`CLAUDE_CONFIG_DIR` names and two accounts return disjoint lists. A home the fixture wrote no list
for fails the way the real CLI does when its list cannot be read. Every invocation is appended to
`claude-calls.jsonl` there with the home it was made under, so a test can assert both that the
monitor never asks this CLI for anything but a snapshot — the monitor must not reach any channel a
model reads — and which accounts it asked at all.

A fixture that wrote `agents-delay` there gets an answer that many seconds late, which is how a
caller that bounds this read is tested against a source that does not answer in time.
"""

import json
import os
import pathlib
import sys
import time


# What the fixture names each home's list after, and what a call made under no home reads.
AGENTS_PREFIX = "agents-"
AGENTS_SUFFIX = ".json"
NO_CONFIG_HOME = "default"


def state_dir():
    return pathlib.Path(os.environ["AGENTCREW_STUB_DIR"])


def config_home():
    """The profile directory this call was made under, which is to say its account."""
    return os.environ.get("CLAUDE_CONFIG_DIR", "")


def agents_path():
    """The file holding this call's own home's agents list.

    One file per account, as the real CLI has one list per profile: which one this call answers
    from is decided by the home it was invoked under.
    """
    name = pathlib.Path(config_home()).name or NO_CONFIG_HOME
    return state_dir() / f"{AGENTS_PREFIX}{name}{AGENTS_SUFFIX}"


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
        handle.write(json.dumps({"argv": argv, "config_home": config_home()}) + "\n")

    if argv == ["agents", "--json"]:
        record_stdin()
        delay()
        path = agents_path()
        if not path.exists():
            print("claude: no agents list", file=sys.stderr)
            return 1
        print(path.read_text())
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
