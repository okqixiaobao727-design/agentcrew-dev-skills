#!/usr/bin/env python3
"""A tmux stand-in for the launch tests, which is the one suite whose windows have to really run.

The launcher's whole point is that the driver lives in a window of its own, so a stub that only
recorded the command would leave nothing to observe. `new-window` records the call in
`AGENTCREW_STUB_DIR/tmux-calls.jsonl` and then starts the command for real, in its own session —
which is what a tmux window is, a process belonging to the tmux server and to nothing that asked
for it — and returns at once, as tmux does.

`display-message -p` answers with the session in `AGENTCREW_STUB_DIR/tmux-session`, and a
`tmux-no-session` file there makes it refuse, as tmux does outside a session.
"""

import json
import os
import pathlib
import subprocess
import sys


def state_dir():
    return pathlib.Path(os.environ["AGENTCREW_STUB_DIR"])


def flag(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


def new_window(argv):
    if (state_dir() / "tmux-new-window-fails").exists():
        print("create window failed: index in use", file=sys.stderr)
        return 1
    command = argv[-1]
    counter = state_dir() / "tmux-window-counter"
    issued = int(counter.read_text()) + 1 if counter.exists() else 1
    counter.write_text(str(issued))
    # Started in its own session, detached from this process and from whatever started it, which
    # is the property the launcher borrows a tmux window for in the first place.
    subprocess.Popen(
        ["sh", "-c", command], cwd=flag(argv, "-c") or os.getcwd(),
        start_new_session=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    print(f"@{issued}")
    return 0


def main():
    argv = sys.argv[1:]
    with (state_dir() / "tmux-calls.jsonl").open("a") as handle:
        handle.write(json.dumps({"argv": argv, "cwd": os.getcwd()}) + "\n")
    if not argv:
        return 1
    if argv[0] == "new-window":
        return new_window(argv)
    if argv[0] == "display-message" and "-p" in argv:
        if (state_dir() / "tmux-no-session").exists():
            print("no server running on /tmp/tmux-1000/default", file=sys.stderr)
            return 1
        path = state_dir() / "tmux-session"
        print(path.read_text().strip() if path.exists() else "$9")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
