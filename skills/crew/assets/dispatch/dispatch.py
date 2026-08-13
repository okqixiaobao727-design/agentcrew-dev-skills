#!/usr/bin/env python3
"""Render and launch a wave of crew children from the approved wave table.

    render    validate the table and write each child's launch artifacts, launching nothing
    dispatch  render, then prepare and launch every child of one wave and verify it started
              on the model the table approved — a Claude child from its entry in the live
              agents list and the model its own transcript records, a Codex child from the
              model the bridge pinned into its state file

The approved wave table is the only routing source this script reads (ADR-0003): a ticket's
`## Routing` section is advisory input used to build that table, never a second authority. The
first-turn skeleton, the workflow shapes and the review-lane variants live beside this script in
`templates/shapes.toml`, so the text a child receives and the code that composes it cannot drift.

The wave table is one JSON object:

    {
      "run": {
        "repo_root":               absolute path to the repository the worktrees are cut in
        "spec_path":               absolute path to the spec every child is pointed at
        "integration_branch":      the branch completed tickets land on
        "integration_base_commit": the commit ticket worktrees and reviews are based on
        "coordinator_name":        the coordinator session's name
        "coordinator_pid":         its pid — the trust anchor a child authenticates against
        "crew_skill_dir":          absolute path to the installed crew skill
        "tmux_session":            the session every child's window is created in
        "permission_mode":         the mode children launch under
        "launch_hook":             optional {"command": str, "env": {name: value}}
        "codex":                   {"bridge": path, "state_dir": path} — required by a codex ticket
      },
      "waves": [{"wave": 1, "tickets": [{
        "id": "06", "title": str, "path": absolute ticket path,
        "workflow": one of the shapes, "executor": "claude" | "codex",
        "model": full model ID, "effort": str,
        "review": {"vendor", "model", "effort"} — on `tdd` and `refactor` only,
        "slug": optional, defaulting to the ticket file's name after its number,
        "base_commit": optional, defaulting to `--base-commit` or the run's base commit,
        "blocked_by": optional list of ticket ids — the dependency edges the advance driver
                      follows; routing does not read them
      }]}]
    }

Each launched child is confirmed on one line, which is the whole of what the coordinator reads:

    06 launched claude <model> <effort> window=@3 name=<agent name> pid=<pid> session=<id>
    06 FAILED <what went wrong>

Exit 0 when every child of the wave launched and verified, 1 otherwise.
"""

import argparse
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
import time
import tomllib

TEMPLATES = pathlib.Path(__file__).resolve().parent / "templates" / "shapes.toml"

EXECUTORS = ("claude", "codex")
REVIEW_VENDORS = ("claude", "codex")
# Guard assets every Claude worktree carries before a child starts in it.
GUARD_ASSETS = ("red-line.sh", "worktree-guard.sh", "settings.local.json")
EXECUTABLE_ASSETS = ("red-line.sh", "worktree-guard.sh")
WORKTREE_PLACEHOLDER = "<WORKTREE_ABSOLUTE_PATH>"
# A bare vendor-less word is an alias whatever the vendor's catalogue holds today.
BARE_WORD = re.compile(r"^[A-Za-z]+$")
# A context-window suffix rides on both forms — `sonnet[1m]` is still that alias, and
# `claude-opus-5[1m]` is still that full ID — so it comes off before either test.
CONTEXT_SUFFIX = re.compile(r"\[[^\]]*\]$")

RUN_KEYS = (
    "repo_root",
    "spec_path",
    "integration_base_commit",
    "coordinator_name",
    "coordinator_pid",
    "crew_skill_dir",
    "tmux_session",
    "permission_mode",
)
# The routing keys every ticket carries, under the names the wave table shows the user.
TICKET_KEYS = {
    "id": "Ticket id",
    "path": "Path",
    "workflow": "Workflow",
    "executor": "Executor",
    "model": "Model",
    "effort": "Effort",
}

DEFAULT_VERIFY_TIMEOUT_SECONDS = 90.0
DEFAULT_HOOK_TIMEOUT_SECONDS = 120.0
VERIFY_POLL_SECONDS = 1.0


class TableError(Exception):
    """The table cannot be dispatched. Carries one line per offending ticket."""

    def __init__(self, problems):
        super().__init__("\n".join(problems))
        self.problems = problems


class LaunchError(Exception):
    """One child could not be launched or could not be verified after launch."""


def load_templates():
    with TEMPLATES.open("rb") as handle:
        return tomllib.load(handle)


def fill(text, values):
    for token, value in values.items():
        text = text.replace(token, str(value))
    return text


def block(text):
    """A template block, without the newlines its TOML delimiters add."""
    return text.strip("\n")


# --- the table ----------------------------------------------------------------------------


def alias_problem(label, value, aliases):
    if not isinstance(value, str):
        return f"{label} `{value}` is not a model name"
    bare = CONTEXT_SUFFIX.sub("", value.strip())
    if bare.lower() in aliases or BARE_WORD.match(bare):
        return f"{label} `{value}` is an alias, not a full model ID"
    return None


def walk_tickets(table):
    """Every ticket the table holds, whatever shape the caller wrote it in."""
    if not isinstance(table, dict):
        return
    waves = table.get("waves")
    for wave in waves if isinstance(waves, list) else []:
        tickets = wave.get("tickets") if isinstance(wave, dict) else None
        for ticket in tickets if isinstance(tickets, list) else []:
            yield ticket


def validate(table, templates):
    """Raise a TableError carrying one line per offending ticket, or return having found none."""
    problems = []
    if not isinstance(table, dict):
        raise TableError(["run: the table is not a JSON object"])
    run = table.get("run")
    if not isinstance(run, dict):
        raise TableError(["run: the table carries no run section"])
    if not isinstance(table.get("waves"), list):
        raise TableError(["run: the table carries no list of waves"])
    missing_run = [key for key in RUN_KEYS if not run.get(key)]
    if missing_run:
        problems.append("run: lacks " + ", ".join(missing_run))

    codex = run.get("codex") if isinstance(run.get("codex"), dict) else {}
    missing_codex = [key for key in ("bridge", "state_dir") if not codex.get(key)]

    aliases = {alias.lower() for alias in templates["models"]["aliases"]}
    shapes = templates["workflows"]
    seen = set()
    for ticket in walk_tickets(table):
        if not isinstance(ticket, dict):
            problems.append(f"{ticket}: is not a ticket object")
            continue
        number = ticket.get("id") or "(no id)"
        faults = []
        for key, label in TICKET_KEYS.items():
            if not ticket.get(key):
                faults.append(f"lacks {label}")
        workflow = ticket.get("workflow")
        if not isinstance(workflow, str):
            workflow = None
        if workflow and workflow not in shapes:
            faults.append(
                f"Workflow `{workflow}` is outside {', '.join(sorted(shapes))}"
            )
        executor = ticket.get("executor")
        if executor and executor not in EXECUTORS:
            faults.append(f"Executor `{executor}` is outside {', '.join(EXECUTORS)}")
        if ticket.get("model"):
            fault = alias_problem("Model", ticket["model"], aliases)
            if fault:
                faults.append(fault)
        if str(ticket.get("id")) in seen:
            faults.append("is listed twice")
        seen.add(str(ticket.get("id")))

        if ticket.get("executor") == "codex" and missing_codex:
            faults.append(
                "needs the run's codex " + ", ".join(missing_codex)
                + ", which the table does not carry"
            )

        review = ticket.get("review")
        if review is not None and not isinstance(review, dict):
            faults.append(f"Review `{review}` is not a review lane")
            review = None
        wants_lane = bool(workflow in shapes and shapes[workflow]["review_lane"])
        if wants_lane and not review:
            faults.append("lacks Review, which its workflow requires")
        elif review and not wants_lane:
            faults.append(f"carries a Review, which workflow `{workflow}` takes none of")
        elif review:
            vendor = review.get("vendor")
            if vendor not in REVIEW_VENDORS:
                faults.append(f"Review vendor `{vendor}` is outside {', '.join(REVIEW_VENDORS)}")
            elif vendor == executor:
                faults.append(f"Review vendor `{vendor}` is its own Executor, not a lane")
            for key in ("model", "effort"):
                if not review.get(key):
                    faults.append(f"Review lacks {key}")
            if review.get("model"):
                fault = alias_problem("Review model", review["model"], aliases)
                if fault:
                    faults.append(fault)

        if faults:
            problems.append(f"{number} {ticket.get('path', '')}: " + "; ".join(faults))

    if problems:
        raise TableError(problems)


def wave_tickets(table, number):
    for wave in table["waves"]:
        if isinstance(wave, dict) and wave.get("wave") == number:
            return list(wave.get("tickets") or [])
    raise TableError([f"run: the table holds no wave {number}"])


def ticket_slug(ticket):
    if ticket.get("slug"):
        return ticket["slug"]
    stem = pathlib.Path(ticket["path"]).stem
    prefix = f"{ticket['id']}-"
    return stem[len(prefix):] if stem.startswith(prefix) else stem


def worktree_path(run, ticket):
    name = f"{ticket['id']}-{ticket_slug(ticket)}"
    return pathlib.Path(run["repo_root"]) / ".claude" / "worktrees" / name


def branch_name(ticket):
    return f"worktree-{ticket['id']}-{ticket_slug(ticket)}"


def base_commit(run, ticket):
    return ticket.get("base_commit") or run["integration_base_commit"]


# --- the first turn -----------------------------------------------------------------------


def render_turn(run, ticket, templates):
    shape = templates["workflows"][ticket["workflow"]]
    turn = templates["turn"]
    values = {
        "<absolute ticket path>": ticket["path"],
        "<absolute spec path>": run["spec_path"],
        "<ticket base commit>": base_commit(run, ticket),
        "<worktree-abs-path>": worktree_path(run, ticket),
        "<crew-skill-dir>": run["crew_skill_dir"],
        "<NN>": ticket["id"],
        "<coordinator name>": run["coordinator_name"],
        "<coordinator pid>": run["coordinator_pid"],
    }

    workflow_block = block(shape["block"])
    if shape["review_lane"]:
        review = ticket["review"]
        review_block = fill(
            block(templates["review"][review["vendor"]]["block"]),
            {
                "<review model>": review["model"],
                "<review effort>": review["effort"],
                "<rounds contract>": block(templates["review"]["rounds"]),
            },
        )
        workflow_block = workflow_block.replace("<review block>", review_block)

    completion = block(shape.get("completion") or turn["completion"])
    completion = completion.replace(
        "<completion condition>", shape.get("completion_condition", "")
    )
    coordinator = block(
        turn["coordinator_claude"] if ticket["executor"] == "claude"
        else turn["coordinator_codex"]
    )

    text = block(turn["base"])
    text = text.replace("<opening line>", block(shape["opening_line"]))
    text = text.replace("<workflow block>", workflow_block)
    text = text.replace("<coordinator paragraph>", coordinator)
    text = text.replace("<completion paragraph>", completion)
    return fill(text, values) + "\n"


def agents_definition(ticket, templates, turn):
    """The `--agents` object whose initialPrompt is the child's whole first turn (ADR-0003)."""
    definition = dict(templates["agent"])
    definition["initialPrompt"] = turn
    definition["keep-coding-instructions"] = True
    return {agent_name(ticket): definition}


def agent_name(ticket):
    return f"crew-{ticket['id']}"


def render_wave(run, tickets, templates, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    rendered = []
    for ticket in tickets:
        turn = render_turn(run, ticket, templates)
        turn_file = out_dir / f"{ticket['id']}.turn.txt"
        turn_file.write_text(turn)
        launch_json = None
        if ticket["executor"] == "claude":
            launch_json = out_dir / f"{ticket['id']}.agents.json"
            launch_json.write_text(
                json.dumps(agents_definition(ticket, templates, turn), indent=2) + "\n"
            )
        rendered.append({
            "ticket": ticket["id"],
            "executor": ticket["executor"],
            "turnFile": str(turn_file),
            "launchJson": str(launch_json) if launch_json else None,
            "worktree": str(worktree_path(run, ticket)),
            "branch": branch_name(ticket),
        })
    return rendered


# --- preparing and launching ---------------------------------------------------------------


def git(repo, *args):
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise LaunchError(
            f"git {' '.join(args)} failed: {(result.stderr or result.stdout).strip()}"
        )
    return result.stdout.strip()


def prepare_worktree(run, ticket):
    """The ticket's worktree, created if absent and identified if not.

    A path already standing here is only this ticket's worktree if it is checked out on this
    ticket's branch; anything else at that path belongs to someone, and a child launched into it
    would work on the wrong branch.
    """
    path = worktree_path(run, ticket)
    branch = branch_name(ticket)
    repo = run["repo_root"]
    if path.is_dir():
        standing = git(path, "rev-parse", "--abbrev-ref", "HEAD")
        if standing != branch:
            raise LaunchError(
                f"{path} is on branch {standing}, not {branch}"
            )
        return path
    existing = git(repo, "branch", "--list", branch)
    path.parent.mkdir(parents=True, exist_ok=True)
    if existing:
        git(repo, "worktree", "add", str(path), branch)
    else:
        git(repo, "worktree", "add", "-b", branch, str(path), base_commit(run, ticket))
    return path


def install_guard_assets(run, worktree):
    """Copy the guard hooks in before any Claude starts in the worktree."""
    source = pathlib.Path(run["crew_skill_dir"]) / "assets"
    target = worktree / ".claude"
    target.mkdir(parents=True, exist_ok=True)
    for name in GUARD_ASSETS:
        origin = source / name
        if not origin.exists():
            raise LaunchError(f"guard asset {name} is absent from {source}")
        installed = target / name
        installed.write_text(
            origin.read_text().replace(WORKTREE_PLACEHOLDER, str(worktree))
        )
        if name in EXECUTABLE_ASSETS:
            installed.chmod(0o755)


def hook_environment(run):
    hook = run.get("launch_hook") or {}
    return {str(key): str(value) for key, value in (hook.get("env") or {}).items()}


def run_launch_hook(run, worktree, window_id, timeout):
    """Call the project's own on-child-launch hook once for this window, and return what it said.

    The hook belongs to the project, not to this run, so it is held to a timeout: a hook that
    hangs must not hold up a wave that is otherwise ready to work.
    """
    hook = run.get("launch_hook") or {}
    command = hook.get("command")
    if not command:
        return None
    environment = dict(os.environ)
    environment.update(hook_environment(run))
    environment["AGENTCREW_CHILD_CWD"] = str(worktree)
    environment["AGENTCREW_CHILD_TMUX_TARGET"] = str(window_id)
    try:
        result = subprocess.run(
            ["sh", "-c", command], cwd=str(worktree), env=environment,
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"exitCode": "timeout", "stdout": "", "stderr": f"no exit within {timeout:g}s"}
    return {
        "exitCode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def hook_note(hook):
    """A launch hook is a courtesy to the project's own tooling: its failure is reported, not fatal."""
    if hook and hook["exitCode"] != 0:
        return f" hook-failed={hook['exitCode']}"
    return ""


def tmux(*args):
    result = subprocess.run(["tmux", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise LaunchError(f"tmux {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def new_window(run, ticket, worktree):
    return tmux(
        "new-window", "-t", run["tmux_session"], "-n", ticket["id"],
        "-c", str(worktree), "-P", "-F", "#{window_id}",
    )


def launch_command(run, ticket, launch_json):
    """The shell line the child's window runs.

    `command claude` bypasses any `claude` wrapper the window's interactive shell defines, and the
    launch JSON is read from its file so the line stays short enough for one send-keys.
    """
    prefix = "".join(
        f"{name}={shlex.quote(value)} " for name, value in sorted(hook_environment(run).items())
    )
    return (
        f"{prefix}command claude"
        f" --agents \"$(cat {shlex.quote(str(launch_json))})\""
        f" --agent {shlex.quote(agent_name(ticket))}"
        f" --model {shlex.quote(ticket['model'])}"
        f" --effort {shlex.quote(ticket['effort'])}"
        f" --permission-mode {shlex.quote(run['permission_mode'])}"
    )


# --- post-launch verification ---------------------------------------------------------------


def agents_list():
    result = subprocess.run(
        ["claude", "agents", "--json"], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise LaunchError(f"claude agents --json failed: {result.stderr.strip()}")
    try:
        return json.loads(result.stdout or "[]")
    except json.JSONDecodeError as error:
        raise LaunchError(f"claude agents --json returned no JSON: {error}") from error


def find_agent(worktree):
    wanted = os.path.realpath(str(worktree))
    for entry in agents_list():
        if os.path.realpath(entry.get("cwd", "")) == wanted:
            return entry
    return None


def transcript_model(session_id):
    root = pathlib.Path(
        os.environ.get("CLAUDE_CONFIG_DIR") or pathlib.Path.home() / ".claude"
    ) / "projects"
    for transcript in root.glob(f"*/{session_id}.jsonl"):
        for line in transcript.read_text().splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = record.get("message")
            model = message.get("model") if isinstance(message, dict) else None
            if model:
                return model
    return None


def verify_child(ticket, worktree, timeout):
    """Assert the child is live on the routed model, from the agents list and its own transcript."""
    deadline = time.monotonic() + timeout
    entry = None
    while True:
        entry = find_agent(worktree)
        if entry:
            break
        if time.monotonic() >= deadline:
            raise LaunchError(
                f"no entry with cwd {worktree} in the live agents list after {timeout:g}s"
            )
        time.sleep(VERIFY_POLL_SECONDS)

    listed = entry.get("model")
    if listed and listed != ticket["model"]:
        raise LaunchError(
            f"model mismatch: the agents list reports {listed},"
            f" the table approved {ticket['model']}"
        )

    model = None
    while True:
        model = transcript_model(entry["sessionId"])
        if model:
            break
        if time.monotonic() >= deadline:
            raise LaunchError(
                f"transcript {entry['sessionId']} names no model after {timeout:g}s"
            )
        time.sleep(VERIFY_POLL_SECONDS)

    if model != ticket["model"]:
        raise LaunchError(
            f"model mismatch: transcript reports {model},"
            f" the table approved {ticket['model']}"
        )
    return entry


# --- per-executor launches ---------------------------------------------------------------


def launch_claude_child(run, ticket, artifacts, timeouts):
    worktree = prepare_worktree(run, ticket)
    install_guard_assets(run, worktree)
    window_id = new_window(run, ticket, worktree)
    hook = run_launch_hook(run, worktree, window_id, timeouts["hook"])
    tmux("send-keys", "-t", window_id, launch_command(run, ticket, artifacts["launchJson"]), "Enter")
    entry = verify_child(ticket, worktree, timeouts["verify"])
    return (
        f"{ticket['id']} launched {ticket['executor']} {ticket['model']} {ticket['effort']}"
        f" window={window_id} name={entry['name']} pid={entry['pid']}"
        f" session={entry['sessionId']}{hook_note(hook)}"
    )


def verify_codex_child(ticket, worktree, state_file):
    """Assert the bridge pinned this session to the routed model, from the state file it wrote.

    A Codex child has no agents list and no transcript of its own; the state file is where the
    bridge records the model and effort it launched under, so it is the surface to read.
    """
    try:
        state = json.loads(state_file.read_text())
    except (OSError, ValueError) as error:
        raise LaunchError(f"the codex state file {state_file} is unreadable: {error}") from error
    if state.get("model") != ticket["model"]:
        raise LaunchError(
            f"model mismatch: the codex session runs {state.get('model')},"
            f" the table approved {ticket['model']}"
        )
    if state.get("effort") != ticket["effort"]:
        raise LaunchError(
            f"effort mismatch: the codex session runs {state.get('effort')},"
            f" the table approved {ticket['effort']}"
        )
    recorded = state.get("cwd")
    if not recorded:
        raise LaunchError(f"the codex state file {state_file} names no working directory")
    if os.path.realpath(recorded) != os.path.realpath(str(worktree)):
        raise LaunchError(f"the codex session runs in {recorded}, not {worktree}")
    return state


def launch_codex_child(run, ticket, artifacts, timeouts):
    worktree = prepare_worktree(run, ticket)
    codex = run["codex"]
    state_file = pathlib.Path(codex["state_dir"]) / f"{ticket['id']}.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    environment.update(hook_environment(run))
    result = subprocess.run(
        [
            sys.executable, str(codex["bridge"]), "launch",
            "--cwd", str(worktree),
            "--tmux-session", run["tmux_session"],
            "--window-name", ticket["id"],
            "--state-file", str(state_file),
            "--model", ticket["model"],
            "--effort", ticket["effort"],
            "--prompt-file", artifacts["turnFile"],
        ],
        capture_output=True, text=True, env=environment,
    )
    try:
        answer = json.loads(result.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        raise LaunchError(
            f"the codex bridge printed no JSON: {(result.stderr or result.stdout).strip()}"
        ) from None
    if not answer.get("ok"):
        raise LaunchError(f"the codex bridge refused the launch: {answer}")
    window_id = answer.get("windowId")
    verify_codex_child(ticket, worktree, state_file)
    hook = run_launch_hook(run, worktree, window_id, timeouts["hook"])
    return (
        f"{ticket['id']} launched {ticket['executor']} {ticket['model']} {ticket['effort']}"
        f" window={window_id} state={state_file}{hook_note(hook)}"
    )


def dispatch_wave(run, tickets, rendered, timeouts):
    lines = []
    failed = False
    for ticket, artifacts in zip(tickets, rendered):
        try:
            if ticket["executor"] == "claude":
                lines.append(launch_claude_child(run, ticket, artifacts, timeouts))
            else:
                lines.append(launch_codex_child(run, ticket, artifacts, timeouts))
        except LaunchError as error:
            failed = True
            lines.append(f"{ticket['id']} FAILED {error}".replace("\n", " "))
    return lines, failed


# --- entry point ---------------------------------------------------------------------------


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("render", "dispatch"))
    parser.add_argument("--table", required=True, help="the approved wave table, as JSON")
    parser.add_argument("--wave", required=True, type=int, help="which wave of it to render")
    parser.add_argument("--out-dir", required=True, help="where launch artifacts are written")
    parser.add_argument(
        "--base-commit",
        help="the commit this wave's worktrees and reviews are based on, in place of the table's"
             " integration base commit — what an earlier wave left on the integration branch",
    )
    parser.add_argument(
        "--verify-timeout", type=float, default=DEFAULT_VERIFY_TIMEOUT_SECONDS,
        help="how long post-launch verification waits for a child to report its model",
    )
    parser.add_argument(
        "--hook-timeout", type=float, default=DEFAULT_HOOK_TIMEOUT_SECONDS,
        help="how long the project's on-child-launch hook is given before it is abandoned",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    templates = load_templates()
    try:
        table = json.loads(pathlib.Path(args.table).read_text())
    except (OSError, ValueError) as error:
        # ValueError covers both a table that is not JSON and one that is not even text.
        print(f"run: {args.table} is not a readable wave table: {error}", file=sys.stderr)
        return 1

    if args.base_commit and isinstance(table.get("run"), dict):
        # One value, read by the worktree and by the first turn's review base alike: a wave cut
        # from a later commit is also reviewed against it.
        table["run"]["integration_base_commit"] = args.base_commit

    try:
        validate(table, templates)
        tickets = wave_tickets(table, args.wave)
    except TableError as error:
        for problem in error.problems:
            print(problem, file=sys.stderr)
        return 1

    run = table["run"]
    rendered = render_wave(run, tickets, templates, pathlib.Path(args.out_dir))
    if args.command == "render":
        print(json.dumps({"ok": True, "wave": args.wave, "children": rendered}, indent=2))
        return 0

    lines, failed = dispatch_wave(run, tickets, rendered, {
        "verify": args.verify_timeout, "hook": args.hook_timeout,
    })
    for line in lines:
        print(line)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
