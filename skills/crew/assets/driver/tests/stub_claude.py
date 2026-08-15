#!/usr/bin/env python3
"""A Claude CLI stand-in for the driver tests.

Every invocation is appended to `AGENTCREW_STUB_DIR/claude-calls.jsonl`, so a test can see both
the launches the renderer made and the snapshots an armed wake monitor asked for.

`agents --json` prints the sessions this stub has been asked to start, from `agents.json` there —
a file a test rewrites to make a child go idle or vanish under the monitor watching it. Any other
invocation is an interactive launch: it records its argv and working directory in `launches.jsonl`,
adds itself to the agents list as `busy`, and writes a transcript under
`CLAUDE_CONFIG_DIR/projects/` carrying the model it ran on, which is the surface post-launch
verification reads.

`AGENTCREW_STUB_TRANSCRIPT_MODEL` writes a different model into that transcript than the launch
named, which is the silent-downgrade case the renderer has to catch.
"""

import json
import os
import pathlib
import sys
import uuid


def state_dir():
    return pathlib.Path(os.environ["AGENTCREW_STUB_DIR"])


def agents_path():
    return state_dir() / "agents.json"


def read_agents():
    path = agents_path()
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return []


def flag(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


def write_transcript(session_id, cwd, model):
    root = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects"
    project = root / cwd.replace("/", "-").replace(".", "-")
    project.mkdir(parents=True, exist_ok=True)
    lines = [
        {"type": "user", "message": {"role": "user", "content": "first turn"}},
        {"type": "assistant", "message": {"role": "assistant", "model": model}},
    ]
    (project / f"{session_id}.jsonl").write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n"
    )


def main():
    argv = sys.argv[1:]
    with (state_dir() / "claude-calls.jsonl").open("a") as handle:
        handle.write(json.dumps({"argv": argv}) + "\n")

    if argv[:2] == ["agents", "--json"]:
        print(json.dumps(read_agents()))
        return 0

    cwd = os.getcwd()
    session_id = str(uuid.uuid4())
    model = flag(argv, "--model")
    with (state_dir() / "launches.jsonl").open("a") as handle:
        handle.write(json.dumps({"argv": argv, "cwd": cwd, "sessionId": session_id}) + "\n")

    agents = read_agents()
    agents.append(
        {
            "pid": 4000 + len(agents),
            "cwd": cwd,
            "kind": "interactive",
            "sessionId": session_id,
            "name": f"stub-child-{len(agents) + 1}",
            "status": "busy",
        }
    )
    agents_path().write_text(json.dumps(agents))
    write_transcript(
        session_id, cwd, os.environ.get("AGENTCREW_STUB_TRANSCRIPT_MODEL") or model
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
