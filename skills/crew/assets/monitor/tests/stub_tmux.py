#!/usr/bin/env python3
"""A tmux stand-in for the monitor tests.

Every call is appended to `AGENTCREW_STUB_DIR/tmux-calls.jsonl`, which is where the tests read
both the toasts the monitor displayed and the command it gave the dashboard pane. `split-window`
answers with a pane id, as tmux does when asked to print one.
"""

import json
import os
import pathlib
import sys


def state_dir():
    return pathlib.Path(os.environ["AGENTCREW_STUB_DIR"])


def main():
    argv = sys.argv[1:]
    with (state_dir() / "tmux-calls.jsonl").open("a") as handle:
        handle.write(json.dumps({"argv": argv}) + "\n")

    if argv and argv[0] == "split-window":
        print("%9")
    return 0


if __name__ == "__main__":
    sys.exit(main())
