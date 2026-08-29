#!/usr/bin/env python3
"""Render and launch a wave of crew children from the approved wave table.

    render    validate the table and write each child's launch artifacts, launching nothing
    dispatch  render, then prepare and launch the children selected by repeated `--ticket-id`
              arguments from one `--wave`, in their wave-table order, and verify each started on
              the model the table approved — a Claude child from its entry in the live agents
              list and the model its own transcript records, a Codex child from the model the
              bridge pinned into its state file
    roles     print the manual advisor and developer prompts for one spec and ticket, naming
              the coordinator address the developer sends to

Every artifact path is recorded absolute, whatever spelling `--out-dir` was given: the launch line
runs in the child's own worktree, so a relative path recorded here would resolve to nothing there.
Given `--log`, every launched child earns one `launch` event in the run's machine log, written by
dispatch itself — wave advancement and the dashboard read the launched set with no coordinator
turn spent on bookkeeping (ADR-0001) — and every rendered review command carries this run's
Lifecycle Hook commands, so Review-Switch writes the ticket's `review` event pair and per-axis
cost through the log's own CLI. Child windows are created detached: a launching wave never takes
the operator's focus.

Every Claude process of a ticket runs on that ticket's account. The account is set on the child's
window itself, so the whole window belongs to it — a `claude` the operator types in there by hand
included — and post-launch verification reads that same account for both of its surfaces, so a
child alive in its own account is never reported missing. Nothing here checks that an account is
logged in: an unauthenticated profile answers the agents list exactly as an idle authenticated one
does, so it surfaces at the verification timeout, which names the account.

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
        "coordinator_name":        the coordinator session's name, a label a child reads
        "coordinator_pid":         its pid — what the dashboard pins the run to
        "coordinator_address":     its whole `uds:` inbox address — the one thing a child sends
                                   to, on its own account or another (ADR-0023)
        "crew_skill_dir":          absolute path to the installed crew skill
        "tmux_session":            the session every child's window is created in
        "permission_mode":         the mode children launch under
        "coordinator_config_home": the coordinator's own Claude configuration home, which is the
                                   account a ticket naming none runs on
        "declared_accounts":       the account names the project config declares, [] where it
                                   declares none — diagnosis only, never a path
        "launch_hook":             optional {"command": str, "env": {name: value}}
        "codex":                   {"bridge": path, "state_dir": path} — required by a codex ticket
      },
      "waves": [{"wave": 1, "tickets": [{
        "id": "06", "title": str, "path": absolute ticket path,
        "workflow": one of the shapes, "executor": "claude" | "codex",
        "model": full model ID, "effort": str,
        "account": absolute path to the Claude Code profile directory this ticket's processes
                   run under — on every row and never absent, resolved from the ticket's own
                   `Account` name or from the run's coordinator_config_home where it named
                   none (ADR-0014),
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

`dispatch` requires at least one `--ticket-id`, rejects the whole request when any selected id is
outside the named Wave, and never uses argument order as a second routing order. Exit 0 when every
selected child launched, verified, and — under `--log` — was recorded, 1 otherwise: a child the
log missed is one wave advancement cannot see, so the Wave has not activated whatever the child
is doing.
"""

import argparse
import dataclasses
import json
import os
import pathlib
import shlex
import subprocess
import sys
import time
import tomllib

TEMPLATES = pathlib.Path(__file__).resolve().parent / "templates" / "shapes.toml"
SKILL_DOCUMENT = pathlib.Path(__file__).resolve().parents[2] / "SKILL.md"
WITNESS_SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "witness.py"
# The log's own writer: the event shape and its closed sets stay the log's alone.
MACHINE_LOG = pathlib.Path(__file__).resolve().parent.parent / "machine_log.py"

# The account module beside it: the one place a row's account binding becomes an environment, so
# this renderer decides neither what "inherit" means nor how an account is spelled into a process.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import accounts  # noqa: E402
import run_plan  # noqa: E402

# Guard assets every Claude worktree carries before a child starts in it.
GUARD_ASSETS = ("red-line.sh", "worktree-guard.sh", "settings.local.json")
EXECUTABLE_ASSETS = ("red-line.sh", "worktree-guard.sh")
WORKTREE_PLACEHOLDER = "<WORKTREE_ABSOLUTE_PATH>"
# A child's transcripts, as the three parts `monitor.py` and `launch.py` already spell such a
# directory in: the variable that moves the configuration home, the home's own name under `~`, and
# the fixed subdirectory inside it. Reading it through the shared shape rather than inline is what
# lets one account's home be substituted for the environment's (ADR-0014).
CONFIG_HOME_VARIABLE = accounts.CONFIG_HOME_VARIABLE
TRANSCRIPTS = (CONFIG_HOME_VARIABLE, ".claude", "projects")

DEFAULT_VERIFY_TIMEOUT_SECONDS = 90.0
DEFAULT_HOOK_TIMEOUT_SECONDS = 120.0
VERIFY_POLL_SECONDS = 1.0


class LaunchError(Exception):
    """One child could not be launched or could not be verified after launch."""


class RoleRenderError(Exception):
    """The manual role prompts could not be rendered from their source documents."""


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


def render_witness_prompt(subject, templates=None, operation="check"):
    """One operation filled into the library's shared Witness prompt."""
    templates = templates or load_templates()
    witness = templates["witness"]
    return fill(
        f"{block(witness['prompt'])}\n\n{block(witness[operation])}",
        {
            "<ticket comment rule>": block(templates["ticket"]["comment_rule"]),
            f"<{operation} subject>": str(subject).strip(),
        },
    )


def contract_text():
    """The body of the crew skill's `## Contract` section, unchanged."""
    document = SKILL_DOCUMENT
    try:
        lines = document.read_text().splitlines()
    except OSError as error:
        raise RoleRenderError(
            f"cannot read the crew skill document {document}: {error}"
        ) from error
    try:
        start = lines.index("## Contract") + 1
    except ValueError as error:
        raise RoleRenderError(
            f"the crew skill document {document} has no ## Contract section"
        ) from error
    end = next(
        (index for index in range(start, len(lines)) if lines[index].startswith("## ")),
        len(lines),
    )
    return "\n".join(lines[start:end]).strip()


def project_witness_routing(ticket):
    """Return the ticket repository's configured witness model and budget."""
    ticket = pathlib.Path(ticket).resolve()
    result = subprocess.run(
        ["git", "-C", str(ticket.parent), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise RoleRenderError(
            f"the ticket {ticket} is not inside a Git repository: {result.stderr.strip()}"
        )
    config_path = pathlib.Path(result.stdout.strip()) / run_plan.PROJECT_CONFIG_NAME
    config = {}
    if config_path.exists():
        try:
            config = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise RoleRenderError(
                f"the project config {config_path} is unreadable: {error}"
            ) from error
    witness = config.get("witness") if isinstance(config, dict) else None
    witness = witness if isinstance(witness, dict) else {}
    try:
        _, model, budget = run_plan.witness_routing(
            witness.get("model"), witness.get("budget_usd")
        )
    except run_plan.RunPlanError as error:
        raise RoleRenderError("; ".join(error.problems)) from error
    sources = (
        f"project config and shipped defaults ({config_path}, {run_plan.DEFAULT_CONFIG})"
    )
    fault = run_plan.model_problem("`[witness] model`", model)
    if fault:
        raise RoleRenderError(f"{sources}: {fault}")
    if isinstance(budget, bool) or not isinstance(budget, (int, float)) or budget <= 0:
        raise RoleRenderError(
            f"{sources} name no positive [witness] budget_usd"
        )
    return model, budget


def render_roles(spec, ticket, coordinator_name, coordinator_address, templates):
    """Return the manual advisor and developer prompts rendered from their shared blocks."""
    witness_model, witness_budget = project_witness_routing(ticket)
    caller_budget = f"\n  {block(templates['review']['caller_budget'])}"
    advisor = block(templates["manual"]["advisor"])
    advisor = fill(
        advisor,
        {
            "<absolute spec path>": spec,
            "<contract>": contract_text(),
        },
    )
    developer = fill(
        block(templates["manual"]["developer"]),
        {
            "<coordinator paragraph>": fill(
                block(templates["turn"]["coordinator_claude"]),
                {
                    "<coordinator name>": coordinator_name,
                    "<coordinator address>": coordinator_address,
                },
            ),
            "<coordinator name>": coordinator_name,
            "<coordinator address>": coordinator_address,
            "<absolute spec path>": spec,
            "<absolute ticket path>": ticket,
            "<escalation paragraph>": fill(
                block(templates["turn"]["escalate"]),
                {"<review caller budget>": caller_budget},
            ),
            "<witness command>": shlex.join([
                "python3", str(WITNESS_SCRIPT),
                "check", "--escalation", "-", "--worktree", ".",
                "--model", witness_model,
                "--budget-usd", f"{witness_budget:g}",
            ]),
        },
    )
    return f"{advisor}\n\n{developer}"


def ticket_slug(ticket):
    if ticket.slug:
        return ticket.slug
    stem = pathlib.Path(ticket.path).stem
    prefix = f"{ticket.id}-"
    return stem[len(prefix):] if stem.startswith(prefix) else stem


def worktree_path(run, ticket):
    name = f"{ticket.id}-{ticket_slug(ticket)}"
    return pathlib.Path(run.repo_root) / ".claude" / "worktrees" / name


def branch_name(ticket):
    return f"worktree-{ticket.id}-{ticket_slug(ticket)}"


def base_commit(run, ticket):
    return ticket.base_commit or run.integration_base_commit


# --- the first turn -----------------------------------------------------------------------


def review_account_flag(ticket, vendor):
    """The flag that puts the Claude reviewer on this ticket's account, or nothing.

    A Claude reviewer is a Claude process belonging to this ticket, so it spends on the ticket's
    own account rather than on whichever login the child it reviews for happens to have been
    started from. The value is the profile directory the row already carries: the wave table is
    where a ticket's account name stopped being a name (ADR-0014), and nothing here reads the
    account registry.

    An inherited binding leaves the environment untouched, so a ticket that named no account
    reviews on the login the operator is signed into rather than under an explicitly spelled
    default that fails to authenticate (#110).

    A Codex reviewer uses its own vendor credentials and receives no Claude account argument.
    """
    account = accounts.environment_delta(ticket.binding).get(CONFIG_HOME_VARIABLE)
    if vendor != "claude" or not account:
        return ""
    return f" --account {shlex.quote(str(account))}"


def run_log_script(log):
    """The machine log's own writer, as the run keeps it: the copy beside the log itself.

    The plugin is installed one directory per version, so a command naming this script inside the
    plugin tree stops working at the next upgrade, while the run directory carries no version and
    outlives every one of them (#37). The copy is written there when the run installs its hooks,
    which happens before any child is launched.
    """
    if not log:
        return ""
    return str(pathlib.Path(log).parent / MACHINE_LOG.name)


def review_hook_flags(templates, ticket, log):
    """Return this run's lifecycle-hook arguments for Review-Switch.

    Each command is first filled and quoted for the shell Review-Switch starts, then the whole
    command is quoted again for the child's shell. The writer comes from the run directory so an
    in-flight review survives a plugin upgrade.
    """
    if not log:
        return ""
    review = ticket.review
    values = {
        "<machine log script>": shlex.quote(run_log_script(log)),
        "<machine log path>": shlex.quote(str(log)),
        "<NN>": shlex.quote(ticket.id),
        "<review vendor>": shlex.quote(review.vendor),
        "<review lane>": shlex.quote(f"{review.vendor} {review.model}"),
    }
    flags = []
    for flag, template in templates["review"]["hooks"].items():
        command = " ".join(
            line.strip() for line in fill(template, values).splitlines() if line.strip()
        )
        flags.append(f" \\\n  --{flag} {shlex.quote(command)}")
    return "".join(flags)


def completion_template(shape, turn, ticket, log):
    """The receipt paragraph this child closes with: recorded by a Claude child, sent otherwise.

    A receipt carries no decision, so a Claude child writes it straight to the run's machine log —
    the transport-agnostic surface the driver already verifies receipts from — instead of waking
    the coordinator with a cross-session message. `CREW ASK` is untouched: it is the one message
    that genuinely needs the coordinator. A Codex child keeps sending, because its bridge is what
    reads the final message of a turn and writes the log from it, and so does either lane on a run
    dispatched without a machine log: there is no log path to name.
    """
    if ticket.executor == "claude" and log:
        return shape.get("completion_claude") or turn["completion_claude"]
    return shape.get("completion") or turn["completion"]


def render_review_script(run, ticket, templates, log=None):
    """The complete installed Review-Switch command this ticket runs before committing."""
    review = ticket.review
    return fill(
        block(templates["review"]["command"]),
        {
            "<review vendor>": shlex.quote(review.vendor),
            "<review model>": shlex.quote(review.model),
            "<review effort>": shlex.quote(review.effort),
            "<review cwd>": shlex.quote(str(worktree_path(run, ticket))),
            "<review base>": shlex.quote(base_commit(run, ticket)),
            "<review spec>": shlex.quote(ticket.path),
            "<review account>": review_account_flag(ticket, review.vendor),
            "<review hooks>": review_hook_flags(templates, ticket, log),
        },
    ) + "\n"


def render_turn(run, ticket, templates, review_script, log=None):
    shape = templates["workflows"][ticket.workflow]
    turn = templates["turn"]
    has_review = review_script is not None
    caller_budget = block(templates["review"]["caller_budget"])
    writing_skill = (
        "$mattpocock-skills:writing-for-agents"
        if ticket.executor == "codex"
        else "/mattpocock-skills:writing-for-agents"
    )
    values = {
        "<absolute ticket path>": ticket.path,
        "<absolute spec path>": run.spec_path,
        "<ticket base commit>": base_commit(run, ticket),
        "<worktree-abs-path>": worktree_path(run, ticket),
        "<crew-skill-dir>": run.crew_skill_dir,
        "<NN>": ticket.id,
        "<coordinator name>": run.coordinator_name,
        "<coordinator address>": run.coordinator_address,
        "<machine log path>": log or "",
        "<machine log script>": run_log_script(log),
        "<design bridge>": shape.get("design_bridge", ""),
        "<ticket comment rule>": block(templates["ticket"]["comment_rule"]),
        "<review caller budget>": f"\n  {caller_budget}" if has_review else "",
        "<writing skill>": writing_skill,
        "<commit step>": f"{4 if has_review else 3}. Commit.",
        "<completion step>": 5 if has_review else 4,
    }

    workflow_block = block(shape["block"])
    review_block = ""
    if has_review:
        review_block = "\n\n" + fill(
            block(templates["review"]["block"]),
            {
                "<review script>": shlex.quote(str(review_script)),
                "<review background>": (
                    "\nIn Claude Bash, set `run_in_background: true`."
                    if ticket.executor == "claude" else ""
                ),
            },
        )

    completion = block(completion_template(shape, turn, ticket, log))
    if ticket.executor == "claude":
        coordinator = "\n".join((
            block(turn["coordinator_claude"]),
            block(turn["coordinator_claude_child"]),
        ))
    else:
        coordinator = block(turn["coordinator_codex"])

    opening_line = (
        shape.get("opening_line_codex", shape["opening_line"])
        if ticket.executor == "codex"
        else shape["opening_line"]
    )
    text = block(turn["base"])
    text = text.replace("<opening line>", block(opening_line))
    text = text.replace("<workflow block>", workflow_block)
    text = text.replace("<review step>", review_block)
    text = text.replace("<coordinator paragraph>", coordinator)
    text = text.replace("<escalation paragraph>", block(turn["escalate"]))
    text = text.replace("<completion paragraph>", completion)
    return fill(text, values) + "\n"


def agents_definition(ticket, templates, turn):
    """The `--agents` object whose initialPrompt is the child's whole first turn (ADR-0003)."""
    definition = dict(templates["agent"])
    definition["initialPrompt"] = turn
    definition["keep-coding-instructions"] = True
    return {agent_name(ticket): definition}


def agent_name(ticket):
    return f"crew-{ticket.id}"


def render_wave(run, tickets, templates, out_dir, log=None):
    out_dir.mkdir(parents=True, exist_ok=True)
    rendered = []
    for ticket in tickets:
        shape = templates["workflows"][ticket.workflow]
        review_script = None
        if shape["review_lane"]:
            review_script = out_dir / f"{ticket.id}.review.sh"
            review_script.write_text(render_review_script(run, ticket, templates, log))
        turn = render_turn(run, ticket, templates, review_script, log)
        turn_file = out_dir / f"{ticket.id}.turn.txt"
        turn_file.write_text(turn)
        launch_json = None
        if ticket.executor == "claude":
            launch_json = out_dir / f"{ticket.id}.agents.json"
            launch_json.write_text(
                json.dumps(agents_definition(ticket, templates, turn), indent=2) + "\n"
            )
        rendered.append({
            "ticket": ticket.id,
            "executor": ticket.executor,
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
    repo = run.repo_root
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
    source = pathlib.Path(run.crew_skill_dir) / "assets"
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
    return dict(run.launch_hook.env) if run.launch_hook else {}


def run_launch_hook(run, worktree, window_id, timeout):
    """Call the project's own on-child-launch hook once for this window, and return what it said.

    The hook belongs to the project, not to this run, so it is held to a timeout: a hook that
    hangs must not hold up a wave that is otherwise ready to work.
    """
    hook = run.launch_hook
    command = hook.command if hook else None
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
    """The child's own window, detached and carrying its ticket's account binding.

    Detached: a wave leaves the operator's focus where it was. On the account: a new window's
    environment otherwise comes from the multiplexer server as it was when that server started,
    and the launch line deliberately bypasses the window's interactive shell, so the window itself
    is the only place a named account can be set — which is also what keeps a `claude` the
    operator types into this window by hand on the ticket's account.

    A ticket that named no account is bound to the login the run is already on, and its window is
    given no `-e` pair at all: setting the default configuration home explicitly is not the same
    as leaving it unset, and the explicit spelling fails the login the inherited one succeeds at
    (#110). Which pairs those are is the account module's to decide, never this renderer's.
    """
    delta = accounts.environment_delta(ticket.binding)
    pairs = [argument for name, value in delta.items() for argument in ("-e", f"{name}={value}")]
    return tmux(
        "new-window", "-d", "-t", run.tmux_session, "-n", ticket.id,
        "-c", str(worktree), *pairs,
        "-P", "-F", "#{window_id}",
    )


def launch_command(run, ticket, launch_json):
    """The shell line the child's window runs.

    `command claude` bypasses any `claude` wrapper the window's interactive shell defines, and the
    launch JSON is read from its file so the line stays short enough for one send-keys.

    The project's launch-hook variables ride on this line, but the account is not among them
    whatever the hook declares: the ticket's binding is what decides this child's login — the
    window's own environment where it names an account, and the environment the run was started
    in where it names none — and a project variable that happens to be spelled the same must not
    be able to move a child off either. The child would then write its transcript into one
    profile while verification read another.
    """
    prefix = "".join(
        f"{name}={shlex.quote(value)} "
        for name, value in sorted(hook_environment(run).items())
        if name != CONFIG_HOME_VARIABLE
    )
    return (
        f"{prefix}command claude"
        f" --agents \"$(cat {shlex.quote(str(launch_json))})\""
        f" --agent {shlex.quote(agent_name(ticket))}"
        f" --model {shlex.quote(ticket.model)}"
        f" --effort {shlex.quote(ticket.effort)}"
        f" --permission-mode {shlex.quote(run.permission_mode)}"
    )


# --- post-launch verification ---------------------------------------------------------------


def harness_directory(spec, home=None):
    """A directory under a Claude configuration home: here, the root a child's transcript is in.

    The same three parts `monitor.py` and `launch.py` read theirs through, with one addition this
    script needs and they do not: `home` is the configuration home to read *instead of* the
    environment's, which is how a child is looked for in its ticket's account rather than in the
    coordinator's.
    """
    variable, default_home, subdirectory = spec
    configured = home or os.environ.get(variable)
    return pathlib.Path(configured or pathlib.Path.home() / default_home) / subdirectory


def agents_list(binding):
    """Every live session of that binding's account, as its own login answers for them.

    Asked in the binding's environment, which for an inherited binding is this process's own,
    untouched: a list asked for under an explicitly spelled default home is answered by a login
    that may not be the one the child was launched on (#110).
    """
    result = subprocess.run(
        ["claude", "agents", "--json"], capture_output=True, text=True,
        env=accounts.process_environment(binding),
    )
    if result.returncode != 0:
        raise LaunchError(f"claude agents --json failed: {result.stderr.strip()}")
    try:
        return json.loads(result.stdout or "[]")
    except json.JSONDecodeError as error:
        raise LaunchError(f"claude agents --json returned no JSON: {error}") from error


def find_agent(worktree, binding):
    wanted = os.path.realpath(str(worktree))
    for entry in agents_list(binding):
        if os.path.realpath(entry.get("cwd", "")) == wanted:
            return entry
    return None


def transcript_model(session_id, binding):
    """The model that transcript says the child ran on, read at the binding's own directory.

    The directory in either mode, because that is where the child wrote it: an inherited binding
    carries the home this process is itself on, which is the home it just handed the child.
    """
    root = harness_directory(TRANSCRIPTS, binding.directory if binding else None)
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
    """Assert the child is live on the routed model, from the agents list and its own transcript.

    Both reads are performed in the ticket's own account, which is where the child was launched:
    two profiles answer with two disjoint agents lists and keep two disjoint transcript roots, so
    a child looked for in the coordinator's account is a correctly launched child reported missing.
    Both reads are asked of the account the child was launched on, through the two halves of its
    binding: the agents list in the binding's own environment — untouched for an inherited
    binding, which is the environment the child itself was handed — and the transcript at the
    directory the binding carries, which is where that environment put it.

    No account is checked for a login anywhere in this feature — an unauthenticated profile
    answers the agents list with an empty list and exit code 0, which an authenticated profile
    with nothing running does too. This timeout is therefore the one surface such a profile
    appears on, so it names the account rather than the worktree.
    """
    binding = ticket.binding
    account = binding.directory
    deadline = time.monotonic() + timeout
    entry = None
    while True:
        entry = find_agent(worktree, binding)
        if entry:
            break
        if time.monotonic() >= deadline:
            raise LaunchError(
                f"no entry for this child in the live agents list of the account {account}"
                f" after {timeout:g}s — that account may not be logged in"
            )
        time.sleep(VERIFY_POLL_SECONDS)

    listed = entry.get("model")
    if listed and listed != ticket.model:
        raise LaunchError(
            f"model mismatch: the agents list reports {listed},"
            f" the table approved {ticket.model}"
        )

    model = None
    while True:
        model = transcript_model(entry["sessionId"], binding)
        if model:
            break
        if time.monotonic() >= deadline:
            raise LaunchError(
                f"transcript {entry['sessionId']} under the account {account} names no model"
                f" after {timeout:g}s"
            )
        time.sleep(VERIFY_POLL_SECONDS)

    if model != ticket.model:
        raise LaunchError(
            f"model mismatch: transcript reports {model},"
            f" the table approved {ticket.model}"
        )
    return entry


# --- per-executor launches ---------------------------------------------------------------


def launch_claude_child(run, ticket, artifacts, timeouts, log=None):
    worktree = prepare_worktree(run, ticket)
    install_guard_assets(run, worktree)
    window_id = new_window(run, ticket, worktree)
    hook = run_launch_hook(run, worktree, window_id, timeouts["hook"])
    tmux("send-keys", "-t", window_id, launch_command(run, ticket, artifacts["launchJson"]), "Enter")
    details = {
        "child": "",
        "window": window_id,
        "worktree": str(worktree),
        "account": ticket.binding.directory,
    }
    started_note = log_note(log, ticket, details)
    try:
        entry = verify_child(ticket, worktree, timeouts["verify"])
    except LaunchError as error:
        failure_note = log_launch_failure_note(log, ticket, error)
        if failure_note:
            raise LaunchError(f"{error}; {failure_note}") from error
        raise
    details["child"] = entry["name"]
    verified_note = log_note(log, ticket, details)
    note = started_note or verified_note
    line = (
        f"{ticket.id} launched {ticket.executor} {ticket.model} {ticket.effort}"
        f" window={window_id} name={entry['name']} pid={entry['pid']}"
        f" session={entry['sessionId']}{hook_note(hook)}"
    )
    return line, note


def verify_codex_child(ticket, worktree, state_file):
    """Assert the bridge pinned this session to the routed model, from the state file it wrote.

    A Codex child has no agents list and no transcript of its own; the state file is where the
    bridge records the model and effort it launched under, so it is the surface to read.
    """
    try:
        state = json.loads(state_file.read_text())
    except (OSError, ValueError) as error:
        raise LaunchError(f"the codex state file {state_file} is unreadable: {error}") from error
    if state.get("model") != ticket.model:
        raise LaunchError(
            f"model mismatch: the codex session runs {state.get('model')},"
            f" the table approved {ticket.model}"
        )
    if state.get("effort") != ticket.effort:
        raise LaunchError(
            f"effort mismatch: the codex session runs {state.get('effort')},"
            f" the table approved {ticket.effort}"
        )
    recorded = state.get("cwd")
    if not recorded:
        raise LaunchError(f"the codex state file {state_file} names no working directory")
    if os.path.realpath(recorded) != os.path.realpath(str(worktree)):
        raise LaunchError(f"the codex session runs in {recorded}, not {worktree}")
    return state


def launch_codex_child(run, ticket, artifacts, timeouts, log=None):
    worktree = prepare_worktree(run, ticket)
    codex = run.codex
    state_file = pathlib.Path(codex.state_dir) / f"{ticket.id}.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    environment.update(hook_environment(run))
    command = [
        sys.executable, str(codex.bridge), "launch",
        "--cwd", str(worktree),
        "--tmux-session", run.tmux_session,
        "--window-name", ticket.id,
        "--state-file", str(state_file),
        "--model", ticket.model,
        "--effort", ticket.effort,
        "--prompt-file", artifacts["turnFile"],
    ]
    if log:
        command.extend(
            ["--machine-log", str(log), "--ticket", ticket.id]
        )
    result = subprocess.run(
        command,
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
    state = verify_codex_child(ticket, worktree, state_file)
    hook = run_launch_hook(run, worktree, window_id, timeouts["hook"])
    line = (
        f"{ticket.id} launched {ticket.executor} {ticket.model} {ticket.effort}"
        f" window={window_id} state={state_file}{hook_note(hook)}"
    )
    # A Codex child has no agents-list name; the thread the bridge pinned is what identifies it.
    details = {
        "child": state.get("threadId"), "window": window_id, "worktree": str(worktree),
    }
    return line, log_note(log, ticket, details)


# --- the launch event ------------------------------------------------------------------------


def log_launch(log, ticket, details):
    """Append this child's `launch` event through the log's own writer, so its shape stays one.

    Dispatch is what knows a child came up, so dispatch is what records it: wave advancement and
    the dashboard read the launched set without a coordinator turn spent on bookkeeping
    (ADR-0001).
    """
    arguments = [
        sys.executable, str(MACHINE_LOG), "--log", str(log), "launch",
        "--ticket", ticket.id,
        "--child", str(details.get("child") or ""),
        "--workflow", ticket.workflow,
        "--executor", ticket.executor,
        "--model", ticket.model,
        "--effort", ticket.effort,
        "--branch", branch_name(ticket),
        "--worktree", str(details["worktree"]),
    ]
    # What makes a run's spend attributable after the fact: which account paid for this child.
    # Only the Claude lane has one — a Codex child launches under its own vendor's credentials,
    # and recording a Claude profile against it would record an account it never ran on.
    if details.get("account"):
        arguments += ["--account", str(details["account"])]
    if details.get("window"):
        arguments += ["--window", str(details["window"])]
    result = subprocess.run(arguments, capture_output=True, text=True)
    if result.returncode != 0:
        raise LaunchError(
            f"machine log append failed: {(result.stderr or result.stdout).strip()}"
        )


def log_note(log, ticket, details):
    """Record the launch; returns the note the child's line carries when the log refused it.

    The child is already up by now, so the line still reports it launched — but a launch the log
    missed is a child wave advancement and the dashboard cannot see, so the note is not the whole
    report: the caller fails the dispatch on it rather than declaring a wave that advanced.
    """
    if not log:
        return ""
    try:
        log_launch(log, ticket, details)
    except LaunchError as error:
        return f" log-failed={str(error).replace(chr(10), ' ')}"
    return ""


def log_launch_failure_note(log, ticket, error):
    """Record why a live child failed verification; return any logging failure as a note."""
    if not log:
        return ""
    arguments = [
        sys.executable, str(MACHINE_LOG), "--log", str(log), "launch-failed",
        "--ticket", ticket.id, "--detail", str(error),
    ]
    result = subprocess.run(arguments, capture_output=True, text=True)
    if result.returncode != 0:
        return f"log-failed={str(result.stderr or result.stdout).strip()}"
    return ""


def dispatch_wave(run, tickets, rendered, timeouts, log=None):
    lines = []
    failed = False
    for ticket, artifacts in zip(tickets, rendered):
        try:
            if ticket.executor == "claude":
                line, note = launch_claude_child(
                    run, ticket, artifacts, timeouts, log=log
                )
            else:
                line, note = launch_codex_child(
                    run, ticket, artifacts, timeouts, log=log
                )
        except LaunchError as error:
            failed = True
            lines.append(f"{ticket.id} FAILED {error}".replace("\n", " "))
            continue
        failed = failed or bool(note)
        lines.append(line + note)
    return lines, failed


# --- entry point ---------------------------------------------------------------------------


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("render", "dispatch", "roles"))
    parser.add_argument("--table", help="the approved wave table, as JSON")
    parser.add_argument("--wave", type=int, help="which wave of it to render")
    parser.add_argument(
        "--ticket-id", action="append", dest="ticket_ids",
        help="a ticket the dispatch command must launch; repeat for each selected ticket",
    )
    parser.add_argument("--out-dir", help="where launch artifacts are written")
    parser.add_argument("--spec", help="the spec path for manual role prompts")
    parser.add_argument("--ticket", help="the ticket path for the manual developer prompt")
    parser.add_argument("--coordinator-name", help="the session name the manual developer answers")
    parser.add_argument(
        "--coordinator-address",
        help="the whole `uds:` inbox address the manual developer sends to",
    )
    parser.add_argument(
        "--log", help="the run's machine log, where each launched child's `launch` event is"
                      " appended and which every rendered review command is pointed at; without"
                      " it a dispatch launches and records nothing",
    )
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
    args = parser.parse_args(argv)
    required = {
        "render": ("table", "wave", "out_dir"),
        "dispatch": ("table", "wave", "out_dir"),
        "roles": ("spec", "ticket", "coordinator_name", "coordinator_address"),
    }[args.command]
    missing = [f"--{name.replace('_', '-')}" for name in required if getattr(args, name) is None]
    if missing:
        parser.error(f"{args.command} requires {' '.join(missing)}")
    if args.command == "dispatch" and not args.ticket_ids:
        parser.error("dispatch requires --ticket-id")
    if args.command != "dispatch" and args.ticket_ids:
        parser.error(f"{args.command} does not accept --ticket-id")
    return args


def main(argv=None):
    args = parse_args(argv)
    templates = load_templates()
    if args.command == "roles":
        try:
            rendered = render_roles(
                args.spec, args.ticket, args.coordinator_name, args.coordinator_address,
                templates,
            )
        except RoleRenderError as error:
            print(error, file=sys.stderr)
            return 1
        print(rendered)
        return 0
    try:
        plan = run_plan.load(args.table)
        if args.base_commit:
            # One value, read by the worktree and by the first turn's review base alike: a wave cut
            # from a later commit is also reviewed against it.
            plan = dataclasses.replace(
                plan,
                run=dataclasses.replace(
                    plan.run, integration_base_commit=args.base_commit,
                ),
            )
        tickets = plan.wave(args.wave).tickets
        if args.command == "dispatch":
            selected = set(args.ticket_ids)
            unknown = [ticket for ticket in dict.fromkeys(args.ticket_ids)
                       if ticket not in {item.id for item in tickets}]
            if unknown:
                print(
                    f"wave {args.wave} does not contain ticket ids: {', '.join(unknown)}",
                    file=sys.stderr,
                )
                return 1
            tickets = tuple(ticket for ticket in tickets if ticket.id in selected)
    except run_plan.RunPlanError as error:
        for problem in error.problems:
            print(problem, file=sys.stderr)
        return 1

    run = plan.run
    # Absolute before anything is recorded: the launch line runs in the child's own worktree, not
    # in this process's working directory, so a relative artifact path there resolves to nothing.
    out_dir = pathlib.Path(os.path.abspath(args.out_dir))
    # Absolute for the same reason every artifact path is: the review this path is rendered into
    # runs in the child's own worktree, where a relative spelling names another file or none.
    log = os.path.abspath(args.log) if args.log else None
    rendered = render_wave(run, tickets, templates, out_dir, log)
    if args.command == "render":
        print(json.dumps({"ok": True, "wave": args.wave, "children": rendered}, indent=2))
        return 0

    lines, failed = dispatch_wave(run, tickets, rendered, {
        "verify": args.verify_timeout, "hook": args.hook_timeout,
    }, log=log)
    for line in lines:
        print(line)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
