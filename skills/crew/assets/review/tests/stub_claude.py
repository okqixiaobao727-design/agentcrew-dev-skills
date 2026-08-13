#!/usr/bin/env python3
"""Test double for the `claude` CLI in headless (`-p`) mode.

Emulates the single invocation claude_review_bridge.py makes:

    claude -p --output-format json --permission-mode <mode> \
        [--model <m>] [--effort <e>] [-r <session-id>] <prompt>

Every invocation appends one JSON line to $CLAUDE_STUB_ARGV_LOG recording the
argv, the working directory, the value of each variable named in
$CLAUDE_STUB_ENV_KEYS, and the reviewer-config override that must never reach a
headless reviewer, so tests can assert on the launch the bridge makes without
reaching into its internals.

The scenario is read from $CLAUDE_STUB_SCENARIO (default "ok"):

    ok          a completed review report
    denials     a completed report plus two permission_denials entries
    error       is_error true with an error subtype
    bad-json    non-JSON output on stdout
    no-session  valid JSON without a session_id
    hang        never answers, so the bridge's timeout fires
"""

import json
import os
import pathlib
import sys
import time

FIRST_SESSION_ID = "stub-session-0001"
REVIEW_REPORT = "Standards: 1 finding (naming). Spec: no findings."
FOLLOWUP_REPORT = "Round two: the naming finding is resolved. No new findings."


def scenario():
    return os.environ.get("CLAUDE_STUB_SCENARIO", "ok")


def watched_env():
    names = os.environ.get("CLAUDE_STUB_ENV_KEYS", "")
    return {name: os.environ.get(name) for name in names.split(",") if name}


def record(argv):
    log = os.environ.get("CLAUDE_STUB_ARGV_LOG")
    if not log:
        return
    entry = {
        "argv": [sys.argv[0]] + argv,
        "cwd": os.getcwd(),
        "env": watched_env(),
        "reviewerFileEnv": os.environ.get("CODE_REVIEWER_FILE"),
    }
    with pathlib.Path(log).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry) + "\n")


def resumed_session_id(argv):
    for flag in ("-r", "--resume"):
        if flag in argv:
            return argv[argv.index(flag) + 1]
    return None


def main():
    argv = sys.argv[1:]
    record(argv)
    active = scenario()
    resume_id = resumed_session_id(argv)

    print("stub claude: starting headless review", file=sys.stderr)

    if active == "hang":
        time.sleep(300)
        return 0

    if active == "bad-json":
        print("Not JSON at all")
        return 0

    cost_field = "total" + "_cost_usd"
    payload = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "session_id": resume_id or FIRST_SESSION_ID,
        "result": FOLLOWUP_REPORT if resume_id else REVIEW_REPORT,
        "permission_denials": [],
    }
    payload[cost_field] = 0.12

    if active == "denials":
        payload["permission_denials"] = [
            {"tool_name": "Bash", "tool_input": {"command": "gh issue view 116"}},
            {"tool_name": "WebFetch", "tool_input": {"url": "https://example.com"}},
        ]
    elif active == "error":
        payload["is_error"] = True
        payload["subtype"] = "error_during_execution"
        payload["result"] = ""
    elif active == "no-session":
        payload.pop("session_id")

    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
