#!/usr/bin/env python3
"""A Claude CLI stand-in for the driver tests.

Every invocation is appended to `AGENTCREW_STUB_DIR/claude-calls.jsonl`, so a test can see both
the launches the renderer made and the snapshots an armed wake monitor asked for.

`agents --json` prints the sessions this stub has been asked to start **in the configuration home
it is called under**, from `agents-<home>.json` there — one file per account, because the real CLI
answers this question out of the profile `CLAUDE_CONFIG_DIR` names and two accounts return disjoint
lists. A test rewrites that file to make a child go idle or vanish under the monitor watching it.
Any other invocation is an interactive launch: it records its argv, working directory and
configuration home in `launches.jsonl`, adds itself to that home's agents list as `busy`, and
writes a transcript under `CLAUDE_CONFIG_DIR/projects/` carrying the model it ran on, which is the
surface post-launch verification reads.

`AGENTCREW_STUB_TRANSCRIPT_MODEL` writes a different model into that transcript than the launch
named, which is the silent-downgrade case the renderer has to catch.
"""

import json
import os
import pathlib
import sys
import uuid


# What a call made under no configuration home at all reads its list from.
NO_CONFIG_HOME = "default"


def state_dir():
    return pathlib.Path(os.environ["AGENTCREW_STUB_DIR"])


def config_home():
    """The profile directory this call was made under, which is to say its account."""
    return os.environ.get("CLAUDE_CONFIG_DIR", "")


def agents_path(state=None, home=None):
    """The file holding one account's agents list; the caller's own account by default.

    One file per account, as the real CLI has one list per profile. Taken as a function of the
    home rather than of this process, so the fixture that seeds a list and the stub that answers
    from it agree on the name without either of them restating the rule.
    """
    name = pathlib.Path(home if home is not None else config_home()).name or NO_CONFIG_HOME
    return pathlib.Path(state if state is not None else state_dir()) / f"agents-{name}.json"


def read_agents():
    path = agents_path()
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return []


def flag(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


def write_transcript(session_id, cwd, model):
    root = pathlib.Path(config_home() or pathlib.Path.home() / ".claude") / "projects"
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
        handle.write(json.dumps({
            "argv": argv, "configHome": config_home(), "cwd": os.getcwd(),
        }) + "\n")

    if argv[:2] == ["agents", "--json"]:
        print(json.dumps(read_agents()))
        return 0

    if "--print" in argv:
        if os.environ.get("AGENTCREW_STUB_WITNESS_BEHAVIOUR") in ("fail", "overrun"):
            print(os.environ["AGENTCREW_STUB_WITNESS_FAILURE"], file=sys.stderr)
            return 7
        usage = {
            "input_tokens": 11,
            "output_tokens": 22,
            "cache_read_input_tokens": 33,
            "cache_creation_input_tokens": 44,
        }
        if os.environ.get("AGENTCREW_STUB_WITNESS_BEHAVIOUR") == "partial-usage":
            usage.pop("cache_creation_input_tokens")
        print(json.dumps({
            "is_error": False,
            "result": os.environ["AGENTCREW_STUB_WITNESS_BRIEF"],
            "usage": usage,
        }))
        return 0

    cwd = os.getcwd()
    session_id = str(uuid.uuid4())
    model = flag(argv, "--model")
    with (state_dir() / "launches.jsonl").open("a") as handle:
        handle.write(json.dumps({
            "argv": argv, "cwd": cwd, "sessionId": session_id, "configHome": config_home(),
        }) + "\n")

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
