#!/usr/bin/env python3
"""A Codex bridge stand-in for the driver tests.

`launch` answers the way the real bridge does — one JSON object carrying `ok`, `windowId` and
`threadId`, plus the state file it writes — and `watch` answers a snapshot and returns, which is
what an armed Codex wake monitor does the moment a session stops being busy. Every call is recorded
in `AGENTCREW_STUB_DIR/codex-calls.jsonl`, so a test reads both the launch and the arming from it.
"""

import json
import os
import pathlib
import sys


def flag(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


def main():
    argv = sys.argv[1:]
    state_dir = pathlib.Path(os.environ["AGENTCREW_STUB_DIR"])
    with (state_dir / "codex-calls.jsonl").open("a") as handle:
        handle.write(json.dumps({"argv": argv}) + "\n")

    if argv[:1] == ["watch"]:
        print(json.dumps({"sessions": []}))
        return 0

    if argv[:1] != ["launch"]:
        print(json.dumps({"ok": False, "error": f"unexpected command: {argv[:1]}"}))
        return 1

    state_file = pathlib.Path(flag(argv, "--state-file"))
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({
        "threadId": "stub-thread",
        "windowId": "@codex-1",
        "cwd": os.path.realpath(flag(argv, "--cwd")),
        "model": flag(argv, "--model"),
        "effort": flag(argv, "--effort"),
    }))
    print(json.dumps({"ok": True, "windowId": "@codex-1", "threadId": "stub-thread"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
