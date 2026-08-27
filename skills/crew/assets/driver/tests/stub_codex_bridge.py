#!/usr/bin/env python3
"""A Codex bridge stand-in for the driver tests.

`launch` answers the way the real bridge does — one JSON object carrying `ok`, `windowId` and
`threadId`, plus the state file it writes — and `watch` answers a snapshot and returns, which is
what an armed Codex wake monitor does the moment a session stops being busy. `send` copies the
prompt into the machine log through the log's own writer, as the real bridge does, so a test reads
the ruling out of the run's log rather than out of the stub. Every call is recorded in
`AGENTCREW_STUB_DIR/codex-calls.jsonl`, so a test reads both the launch and the arming from it.
"""

import json
import os
import pathlib
import subprocess
import sys


MACHINE_LOG = pathlib.Path(__file__).resolve().parents[2] / "machine_log.py"


def flag(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


def main():
    argv = sys.argv[1:]
    state_dir = pathlib.Path(os.environ["AGENTCREW_STUB_DIR"])
    with (state_dir / "codex-calls.jsonl").open("a") as handle:
        handle.write(json.dumps({"argv": argv}) + "\n")

    if argv[:1] == ["watch"]:
        path = state_dir / "codex-statuses.json"
        statuses = json.loads(path.read_text()) if path.exists() else {}
        sessions = []
        for state_file in argv[1:]:
            ticket = pathlib.Path(state_file).stem
            status = statuses.get(ticket)
            if status is not None:
                sessions.append({"stateFile": state_file, "status": status})
                if status == "idle":
                    statuses[ticket] = "busy"
        if path.exists():
            path.write_text(json.dumps(statuses))
        print(json.dumps({"sessions": sessions}))
        return 0

    if argv[:1] == ["send"]:
        machine_log = flag(argv, "--machine-log")
        if machine_log:
            command = [
                sys.executable, str(MACHINE_LOG), "--log", machine_log, "message",
                "--role", "coordinator", "--message", flag(argv, "--prompt"),
            ]
            ticket = flag(argv, "--ticket")
            if ticket is not None:
                command.extend(["--ticket", ticket])
            subprocess.run(command, capture_output=True, text=True, check=False)
        print(json.dumps({"ok": True, "stateFile": flag(argv, "--state-file")}))
        return 0

    if argv[:1] == ["stop"]:
        if (state_dir / "codex-stop-fails").exists():
            print("stop refused: the session would not go", file=sys.stderr)
            return 1
        print(json.dumps({"ok": True, "stateFile": flag(argv, "--state-file")}))
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
