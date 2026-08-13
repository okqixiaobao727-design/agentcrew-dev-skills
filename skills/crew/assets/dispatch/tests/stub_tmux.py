#!/usr/bin/env python3
"""A tmux stand-in for the dispatch tests.

Windows are numbered from a counter in `AGENTCREW_STUB_DIR`; every call is appended to
`tmux-calls.jsonl` there. `send-keys` runs the keys it was given through `sh -c` in the window's
own directory, so a stubbed launch reaches the stub `claude` exactly as a real one reaches the
real CLI.
"""

import json
import os
import pathlib
import subprocess
import sys


def state_dir():
    return pathlib.Path(os.environ["AGENTCREW_STUB_DIR"])


def record(entry):
    with (state_dir() / "tmux-calls.jsonl").open("a") as handle:
        handle.write(json.dumps(entry) + "\n")


def windows():
    path = state_dir() / "tmux-windows.json"
    return json.loads(path.read_text()) if path.exists() else {}


def save_windows(table):
    (state_dir() / "tmux-windows.json").write_text(json.dumps(table))


def flag(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


def main():
    argv = sys.argv[1:]
    record({"argv": argv})
    if not argv:
        return 1
    command = argv[0]

    if command == "new-window":
        table = windows()
        window_id = "@" + str(len(table) + 1)
        table[window_id] = {
            "cwd": flag(argv, "-c") or os.getcwd(),
            "name": flag(argv, "-n"),
            "target": flag(argv, "-t"),
        }
        save_windows(table)
        print(window_id)
        return 0

    if command == "send-keys":
        target = flag(argv, "-t")
        keys = [value for value in argv[argv.index(target) + 1:] if value != "Enter"]
        window = windows().get(target, {})
        result = subprocess.run(
            ["sh", "-c", " ".join(keys)], cwd=window.get("cwd") or os.getcwd()
        )
        return result.returncode

    return 0


if __name__ == "__main__":
    sys.exit(main())
