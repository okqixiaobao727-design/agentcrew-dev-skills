#!/usr/bin/env python3
"""A tmux stand-in for the monitor tests.

Every call is appended to `AGENTCREW_STUB_DIR/tmux-calls.jsonl`, which is where the tests read
both the toasts the monitor displayed and the command it gave the dashboard window. Windows live
in `tmux-windows.json` there: `new-window` adds one and prints its id, `list-windows` prints the
ids that are still there, and a test makes a window vanish by deleting its entry — which is the
whole of what the dashboard's window lifecycle asks of tmux.
"""

import json
import os
import pathlib
import sys


def state_dir():
    return pathlib.Path(os.environ["AGENTCREW_STUB_DIR"])


def windows_path():
    return state_dir() / "tmux-windows.json"


def windows():
    path = windows_path()
    return json.loads(path.read_text()) if path.exists() else {}


def save_windows(table):
    windows_path().write_text(json.dumps(table))


def flag(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


def main():
    argv = sys.argv[1:]
    with (state_dir() / "tmux-calls.jsonl").open("a") as handle:
        handle.write(json.dumps({"argv": argv}) + "\n")

    if not argv:
        return 1
    command = argv[0]

    if command == "new-window":
        table = windows()
        # tmux hands out a fresh id per window, so the counter is its own file: an id a test
        # made vanish is never handed out again, as tmux never hands one out twice.
        counter = state_dir() / "tmux-window-counter"
        issued = int(counter.read_text()) + 1 if counter.exists() else 1
        counter.write_text(str(issued))
        window_id = f"@{issued}"
        table[window_id] = {
            "name": flag(argv, "-n"),
            "target": flag(argv, "-t"),
            "command": argv[-1],
            "detached": "-d" in argv,
        }
        save_windows(table)
        print(window_id)
        return 0

    if command == "list-windows":
        for window_id in windows():
            print(window_id)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
