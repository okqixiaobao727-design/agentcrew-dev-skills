#!/usr/bin/env python3
"""A Codex bridge stand-in for the dispatch tests.

It answers `launch` the way the real bridge does — one JSON object carrying `ok`, `windowId` and
`threadId`, plus the state file it writes — and records its argv in
`AGENTCREW_STUB_DIR/codex-launches.jsonl`.

`AGENTCREW_STUB_STATE_MODEL` pins a different model into the state file than the launch named,
which is the silent-downgrade case the renderer has to catch on the Codex side, and
`AGENTCREW_STUB_STATE_CWD` writes a different working directory — empty, for a state file that
names none at all.
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
    with (state_dir / "codex-launches.jsonl").open("a") as handle:
        handle.write(json.dumps({"argv": argv, "env": {
            key: value for key, value in os.environ.items()
            if key.startswith("AGENTCREW_HOOK_")
        }}) + "\n")

    if argv[:1] != ["launch"]:
        print(json.dumps({"ok": False, "error": f"unexpected command: {argv[:1]}"}))
        return 1

    state_file = pathlib.Path(flag(argv, "--state-file"))
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({
        "threadId": "stub-thread",
        "windowId": "@codex-1",
        "cwd": os.environ.get("AGENTCREW_STUB_STATE_CWD", os.path.realpath(flag(argv, "--cwd"))),
        "model": os.environ.get("AGENTCREW_STUB_STATE_MODEL") or flag(argv, "--model"),
        "effort": flag(argv, "--effort"),
    }))
    print(json.dumps({"ok": True, "windowId": "@codex-1", "threadId": "stub-thread"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
