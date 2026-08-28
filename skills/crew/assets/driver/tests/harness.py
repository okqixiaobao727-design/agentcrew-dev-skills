#!/usr/bin/env python3
"""The harness the driver's suites are built on: a temporary run, and the test case that owns one.

`Fixture` builds one run's world — an origin, a checkout, a feature of tickets, and a stub PATH
carrying `claude`, `tmux` and the Codex bridge — and reads it back the way a test observes it.
`DriverTestCase` binds one fixture to one test and adds the assertions every suite makes about a
run's stdout, its log and its preflight notice.

This module holds no tests. It lives beside them so that `test_driver.py`, `test_clear.py` and
`test_sweep.py` share the harness through an import of their own: a test file is a collection
of tests, never another test file's interface.
"""

import json
import os
import pathlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


TESTS_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))
# The stub CLI itself, for the one rule the fixture and the stub have to agree on: which file an
# account's agents list lives in. Imported rather than restated, so they cannot drift apart.
import stub_claude  # noqa: E402
DRIVER = TESTS_DIR.parent / "driver.py"
MACHINE_LOG = DRIVER.parent.parent / "machine_log.py"
TRIAGE = DRIVER.parent.parent.parent / "references" / "triage.md"
MONITOR = DRIVER.parent.parent / "monitor" / "monitor.py"
# The script every snapshot's `resume` names, because a driver put back belongs in a window of its
# own exactly as the first one did. Its own suite drives it; here it is only what is named.
LAUNCH = DRIVER.parent.parent / "launch" / "launch.py"

CLAUDE_MODEL = "claude-opus-4-5-20251101"
CLAUDE_EFFORT = "medium"
CODEX_MODEL = "gpt-5.6-luna"
CODEX_EFFORT = "max"
COORDINATOR_NAME = "crew-coordinator-1f"
COORDINATOR_PID = 1504
COORDINATOR_SESSION = "2cd60d75-fa21-4d9c-adf2-b4073f60fbb6"
# The whole address a child of this run sends to, as the launcher reads it off the harness: the
# socket the coordinator bound, under the `uds:` scheme, spelled exactly as the harness spelled it.
COORDINATOR_ADDRESS = "uds:/tmp/cc-socks/1504.sock"
PERMISSION_MODE = "acceptEdits"
TMUX_SESSION = "$7:"
# The pane the coordinator itself is sitting in, as tmux names one and as the launcher reads it
# out of its own environment: a wake that reaches no waiter is re-typed here, and nowhere else.
COORDINATOR_PANE = "%3"

PREFLIGHT_WINDOW = "crew-preflight"
DASHBOARD_WINDOW = "crew-dashboard"
REPORT_NAME = "report.md"
RUN_DIR_NAME = ".crew"
# The two files the run directory gains: the driver's own pid while its loop runs, and the wake
# snapshot the coordinator's waiter reads instead of the driver's stdout.
DRIVER_RECORD = "driver.pid"
# The coordinator waiter's own record beside it: written by the launcher while it blocks on the
# run's wake, and read by the driver to decide whether anyone is left to carry that wake back.
WAITER_RECORD = "waiter.pid"
# The file every armed wake monitor carries the path of, which is how one is told from another
# run's on the process table, and the script's own name beside it.
PARKED_PATHS = "parked-paths"
# The two halves of a row's account binding: a ticket that named an account selects that
# configuration home explicitly, and a ticket that named none inherits the environment the run
# was started in (ADR-0014).
INHERITED = "inherited"
EXPLICIT = "explicit"
MONITOR_WAVE_NAME = "monitor-wave.sh"
WAKE_NAME = "wake.json"
FEATURE_NAME = "demo"
INTEGRATION_BRANCH = "crew/demo"
BASE_BRANCH = "main"
# The configured decisions the driver records into the run.
REPAIR_MODEL = "claude-sonnet-5"
WITNESS_MODEL = "claude-sonnet-5"
WITNESS_BUDGET_USD = 2.0
WITNESS_BRIEF = "README.md:1 — held — the fixture file exists"
WITNESS_FAILURE = "stub witness failed on purpose"
WITNESS_OVERRUN = "witness session timed out"
# A file carrying a line that reads as an opening conflict marker: the merge driver will not
# rewrite a conflict in it, so this is the shape that still climbs to the repair rung.
UNREWRITABLE = "one\n<<<<<<< left over from an earlier merge\nthree\n"
TRACKER = "local"
# The loop's dials, wound down so a test drives a run in seconds rather than in poll intervals.
POLL_SECONDS = "0.2"
LOOP_TIMEOUT = "20"
# Long enough for the loop to have polled a status many times over, which is what makes "it was
# never nudged" an observation rather than a race won.
QUIET_SECONDS = 3.0

# The lane the fixture routing sends a review to, spelled the way the review bridge spells it.
REVIEW_LANE = f"codex {CODEX_MODEL}"

ROUTING = f"""## Routing

Workflow: tdd
Executor: claude
Model: {CLAUDE_MODEL}
Effort: {CLAUDE_EFFORT}
Review: codex {CODEX_MODEL} {CODEX_EFFORT}
Reasons: a fixture ticket.
"""

CODEX_ROUTING = f"""## Routing

Workflow: tdd
Executor: codex
Model: {CODEX_MODEL}
Effort: {CODEX_EFFORT}
Review: claude {CLAUDE_MODEL} {CLAUDE_EFFORT}
Reasons: a fixture ticket.
"""


# The one fixture routing no review lane reaches: `direct` is a workflow whose shape carries none,
# so a feature of these tickets is a run that reviews nowhere.
DIRECT_ROUTING = f"""## Routing

Workflow: direct
Executor: claude
Model: {CLAUDE_MODEL}
Effort: {CLAUDE_EFFORT}
Reasons: a fixture ticket with no review lane.
"""


def routing_naming(account):
    """The fixture routing carrying the optional `Account` line an operator may name."""
    return ROUTING.replace(
        f"Effort: {CLAUDE_EFFORT}\n", f"Effort: {CLAUDE_EFFORT}\nAccount: {account}\n"
    )


def git(repo, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=check, capture_output=True, text=True
    )


class Fixture:
    """A temporary run: an origin, a checkout, a feature of tickets, and a stub PATH."""

    def __init__(self):
        # Resolved, because every path the run records is: a temporary directory reached by one
        # spelling and recorded as another is a comparison that fails for no reason of the run's.
        self.root = pathlib.Path(tempfile.mkdtemp()).resolve()
        self.origin = self.root / "origin.git"
        subprocess.run(
            ["git", "init", "--bare", "-b", BASE_BRANCH, str(self.origin)],
            check=True, capture_output=True,
        )
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", BASE_BRANCH)
        git(self.repo, "config", "user.email", "crew@example.invalid")
        git(self.repo, "config", "user.name", "Crew Test")
        (self.repo / "README.md").write_text("fixture\n")
        self.write_config()
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", "base")
        git(self.repo, "remote", "add", "origin", str(self.origin))
        git(self.repo, "push", "-u", "origin", BASE_BRANCH)
        git(self.repo, "remote", "set-head", "origin", "-a")

        self.feature_dir = self.repo / "features" / FEATURE_NAME
        self.feature_dir.mkdir(parents=True)
        self.spec_path = self.feature_dir / "spec.md"
        self.spec_path.write_text("# spec\n")

        self.stub_dir = self.root / "stub"
        self.stub_dir.mkdir()
        self.config_dir = self.root / "claude-config"
        self.config_dir.mkdir()
        # The machine's account registry, moved off the real home by the override every run reads
        # it through. No file is written here until a test writes one, which is the machine that
        # has never registered an account.
        self.registry = self.root / "accounts.toml"
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self._link_stub("claude", "stub_claude.py")
        self._link_stub("tmux", "stub_tmux.py")
        self._link_stub("gh", "stub_gh.py")
        self._link_stub("review-bridge", "stub_review_bridge.py")
        # Whether this fixture's machine has Review-Switch installed. The default is the machine
        # a reviewed run needs, so every test that is not about the check sees the command.
        self.review_command_installed = True
        self.running = []
        # The one session the stub server holds: a window asked for in any other is refused, as a
        # real tmux refuses a session it does not have.
        (self.stub_dir / "tmux-session").write_text(TMUX_SESSION)
        self.codex_bridge = TESTS_DIR / "stub_codex_bridge.py"

    def _link_stub(self, name, script):
        target = self.bin_dir / name
        target.write_text(
            "#!/bin/sh\nexec %s %s \"$@\"\n" % (sys.executable, TESTS_DIR / script)
        )
        target.chmod(0o755)

    def uninstall_review_command(self):
        """Take Review-Switch off this fixture's machine — the stub, and any real installation.

        This fixture's PATH ends in the developer's own, so removing only the stub would leave a
        machine that has Review-Switch installed passing a check the test is about failing.
        """
        (self.bin_dir / "review-bridge").unlink()
        self.review_command_installed = False

    # --- the project's config -------------------------------------------------------------

    def write_config(self, repair_model=REPAIR_MODEL, tracker=TRACKER, surface=None,
                     accounts=None, witness_model=None, witness_budget_usd=None, gate=None):
        """The project config the run reads its repair model and its tracker out of."""
        lines = []
        if repair_model is not None:
            lines += ["[repair]", f'model = "{repair_model}"']
        if tracker is not None:
            lines += ["[tracker]", f'kind = "{tracker}"']
        if gate is not None:
            lines += ["[preflight]", f"gate = {json.dumps(gate)}"]
        if surface is not None:
            lines += ["[dashboard]", f'surface = "{surface}"']
        if accounts is not None:
            names = ", ".join(f'"{name}"' for name in accounts)
            lines += ["[accounts]", f"names = [{names}]"]
        if witness_model is not None or witness_budget_usd is not None:
            lines += ["[witness]"]
            if witness_model is not None:
                lines += [f'model = "{witness_model}"']
            if witness_budget_usd is not None:
                lines += [f"budget_usd = {witness_budget_usd}"]
        (self.repo / "agentcrew.toml").write_text("\n".join(lines) + "\n")

    def configure_gate(self, exit_code=0, output=""):
        """Install and configure the gate stub used only by tests that exercise base gating."""
        target = self.bin_dir / "base-gate"
        target.write_text(
            "#!" + sys.executable + "\n"
            "import json\n"
            "import os\n"
            "import pathlib\n"
            "import subprocess\n"
            "import sys\n"
            "root = pathlib.Path(os.environ['AGENTCREW_STUB_DIR'])\n"
            "head = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True)\n"
            "with (root / 'base-gate-calls').open('a') as handle:\n"
            "    handle.write(json.dumps({'cwd': os.getcwd(), 'argv': sys.argv[1:], "
            "'head': head.stdout.strip()}) + '\\n')\n"
            "sys.stdout.write((root / 'base-gate-output').read_text())\n"
            "raise SystemExit(int((root / 'base-gate-exit').read_text()))\n"
        )
        target.chmod(0o755)
        (self.stub_dir / "base-gate-output").write_text(output)
        (self.stub_dir / "base-gate-exit").write_text(str(exit_code))
        self.configure(gate=["base-gate", "--full"])

    def gate_calls(self):
        """Every configured base-gate invocation, in physical order."""
        path = self.stub_dir / "base-gate-calls"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    # --- the machine's account registry -----------------------------------------------------

    def profile(self, name):
        """A Claude Code profile directory on this fixture's machine."""
        path = self.root / "profiles" / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def register(self, **accounts):
        """Write the machine-level registry mapping each account name to a profile directory."""
        lines = ["[accounts]"]
        lines += [f'{name} = "{directory}"' for name, directory in accounts.items()]
        self.registry.write_text("\n".join(lines) + "\n")

    def configure(self, **values):
        """Rewrite and commit the project config, because an uncommitted fix is not one."""
        self.write_config(**values)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", "config")

    def coordinator_transcript(self, session, usage=None):
        """Write a coordinator transcript where the monitor's cost reader looks for it."""
        usage = usage or {
            "input_tokens": 1,
            "output_tokens": 1,
            "cache_read_input_tokens": 1,
            "cache_creation_input_tokens": 1,
        }
        path = self.config_dir / "projects" / f"{session}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "type": "assistant",
            "requestId": f"request-{session}",
            "cwd": str(self.repo),
            "message": {"usage": usage},
        }) + "\n")
        return path

    # --- the feature ----------------------------------------------------------------------

    def ticket(self, number, title, routing=ROUTING, blocked_by=(), heading=None):
        """One ticket file, in the shape `/route` writes and the driver parses."""
        blockers = (
            "\n".join(f"- #{blocker}" for blocker in blocked_by)
            if blocked_by else "None — can start immediately."
        )
        text = f"# {heading if heading is not None else title}\n\n"
        text += "## What to build\n\nA fixture.\n\n"
        text += f"## Blocked by\n\n{blockers}\n\n"
        text += routing
        (self.feature_dir / f"{number}.md").write_text(text)

    def commit_feature(self):
        git(self.repo, "add", "-A", "features")
        git(self.repo, "commit", "-m", "tickets")
        git(self.repo, "push", "origin", BASE_BRANCH)

    # --- running it -----------------------------------------------------------------------

    def environment(self, overrides=None):
        environment = dict(os.environ)
        inherited = environment["PATH"]
        if not self.review_command_installed:
            inherited = os.pathsep.join(
                entry for entry in inherited.split(os.pathsep)
                if entry and not os.access(os.path.join(entry, "review-bridge"), os.X_OK)
            )
        environment["PATH"] = f"{self.bin_dir}{os.pathsep}{inherited}"
        environment["AGENTCREW_STUB_DIR"] = str(self.stub_dir)
        environment["CLAUDE_CONFIG_DIR"] = str(self.config_dir)
        environment["AGENTCREW_ACCOUNT_REGISTRY"] = str(self.registry)
        environment["CREW_POLL_SECONDS"] = "1"
        environment.pop("AGENTCREW_STUB_TRANSCRIPT_MODEL", None)
        environment.update(overrides or {})
        return environment

    def pin_install(self):
        """Record the machine preference through the monitor's public installer command."""
        return subprocess.run(
            [
                sys.executable, str(MONITOR), "pin-install",
                "--settings", self.config_dir / "settings.json", "--apply",
            ],
            capture_output=True, text=True, env=self.environment(), cwd=str(self.repo),
        )

    def start_argv(self, extra=()):
        return [
            sys.executable, str(DRIVER), "start",
            "--feature-dir", str(self.feature_dir),
            "--coordinator-name", COORDINATOR_NAME,
            "--coordinator-pid", str(COORDINATOR_PID),
            "--coordinator-session", COORDINATOR_SESSION,
            "--coordinator-address", COORDINATOR_ADDRESS,
            "--permission-mode", PERMISSION_MODE,
            "--tmux-session", TMUX_SESSION,
            "--coordinator-pane", COORDINATOR_PANE,
            "--codex-bridge", str(self.codex_bridge),
            "--poll-seconds", POLL_SECONDS,
            "--timeout", LOOP_TIMEOUT,
            *extra,
        ]

    def start(self, extra=(), env_overrides=None):
        """Start a run and wait for it to end, which a run nobody drives does on its timeout."""
        return subprocess.run(
            self.start_argv(extra),
            capture_output=True, text=True, env=self.environment(env_overrides),
            cwd=str(self.repo),
        )

    def launch(self, extra=(), env_overrides=None):
        """Start a run and leave its loop running; returns the driver process.

        The loop is the driver now, so a test that watches a run drives it while it runs rather
        than reading what it left behind.
        """
        process = subprocess.Popen(
            self.start_argv(extra),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=self.environment(env_overrides), cwd=str(self.repo),
        )
        self.running.append(process)
        return process

    def resume(self, extra=(), env_overrides=None):
        """Put the loop back where a ruling stopped it, and leave it running."""
        process = subprocess.Popen(
            [
                sys.executable, str(DRIVER), "resume",
                "--feature-dir", str(self.feature_dir),
                "--coordinator-pid", str(COORDINATOR_PID),
                "--tmux-session", TMUX_SESSION,
                "--coordinator-pane", COORDINATOR_PANE,
                "--poll-seconds", POLL_SECONDS,
                "--timeout", LOOP_TIMEOUT,
                *extra,
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=self.environment(env_overrides), cwd=str(self.repo),
        )
        self.running.append(process)
        return process

    def run_command(self, command, extra=()):
        """Run a command the driver itself printed, as the coordinator would type it back."""
        process = subprocess.Popen(
            [*shlex.split(command), *extra],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=self.environment(), cwd=str(self.repo),
        )
        self.running.append(process)
        return process

    def ended(self, process, timeout=90.0):
        """What that driver printed and exited with, once it has ended of its own accord."""
        out, err = process.communicate(timeout=timeout)
        return subprocess.CompletedProcess(process.args, process.returncode, out, err)

    # --- driving the run's children -----------------------------------------------------------

    def launch_record(self, ticket):
        """What the log says about the child launched on that ticket."""
        for record in reversed(self.log_records()):
            if record.get("event") == "launch" and record.get("ticket") == ticket:
                return record
        return None

    def verified_launch(self, ticket):
        """That child's launch record, but only once dispatch has verified the child is up.

        Dispatch records a launch twice: a provisional record with an empty `child` as soon as the
        child is sent to its window, then a verified one carrying the real thread id once it has
        found the child in its account's agents list. Between the two, dispatch is still polling
        that list — so a test that returns on the provisional record and then mutates the list
        (`vanishes`, `goes`) takes away the very entry verification is waiting for, and the
        verified record never lands: the wave never advances and the ticket can never settle.

        Every wait for a launched child waits on this rather than on `launch_record` alone.
        """
        record = self.launch_record(ticket)
        return record if (record or {}).get("child") else None

    def worktree(self, ticket):
        return pathlib.Path(self.launch_record(ticket)["worktree"])

    def commit_work(self, ticket, text="work\n", name=None):
        """Commit something in that child's worktree, and return the sha its receipt would claim."""
        worktree = self.worktree(ticket)
        name = name if name is not None else f"{ticket}.txt"
        (worktree / name).write_text(text)
        git(worktree, "add", name)
        git(worktree, "commit", "-m", f"{ticket} work")
        return git(worktree, "rev-parse", "HEAD").stdout.strip()

    def says(self, ticket, message):
        """Write into the log what a child's own hook writes when it sends that message."""
        subprocess.run(
            [
                sys.executable, str(MACHINE_LOG), "--log", str(self.run_dir / "log.jsonl"),
                "message", "--role", "child", "--ticket", ticket,
                "--to", COORDINATOR_NAME, "--message", message,
            ],
            check=True, capture_output=True,
        )

    def reviews(self, ticket, state, lane=REVIEW_LANE):
        """Write the line that ticket's review bridge writes at one end of a lane's trip."""
        subprocess.run(
            [
                sys.executable, str(MACHINE_LOG), "--log", str(self.run_dir / "log.jsonl"),
                "review", "--ticket", ticket, "--lane", lane, "--state", state,
            ],
            check=True, capture_output=True,
        )

    def completes(self, ticket, text="work\n", name=None):
        """The whole of what a child that finished does: commit the work, send the receipt."""
        self.says(ticket, f"CREW COMPLETE {self.commit_work(ticket, text, name)}")

    def answers(self, ticket, text):
        """Deliver the coordinator's ruling the way the coordinator delivers one."""
        subprocess.run(
            [
                sys.executable, str(DRIVER), "answer", "--run-dir", str(self.run_dir),
                "--ticket", ticket, "--text", text,
            ],
            check=True, capture_output=True, env=self.environment(), cwd=str(self.repo),
        )

    def agents_path(self, home=None):
        """The file the stub CLI answers one account's agents list out of.

        One per account, as two logged-in profiles keep two disjoint lists. The coordinator's own
        home unless a caller names another, which is what every child of a single-account run is
        launched and looked for under.
        """
        return stub_claude.agents_path(self.stub_dir, home or self.config_dir)

    def agents(self, home=None):
        path = self.agents_path(home)
        return json.loads(path.read_text()) if path.exists() else []

    def set_agents(self, agents, home=None):
        self.agents_path(home).write_text(json.dumps(agents))

    def account_of(self, ticket):
        """The profile directory that child launched under, as its own launch line records it."""
        return (self.launch_record(ticket) or {}).get("account")

    def goes(self, ticket, status):
        """Put that child into the status its own account's agents list reports it in.

        Safe only once that child's launch is verified — see `verified_launch`.
        """
        worktree = os.path.realpath(self.worktree(ticket))
        home = self.account_of(ticket)
        agents = self.agents(home)
        for agent in agents:
            if os.path.realpath(agent.get("cwd", "")) == worktree:
                agent["status"] = status
        self.set_agents(agents, home)

    def codex_goes(self, ticket, status):
        """Set the status the next Codex watch reports for one ticket."""
        path = self.stub_dir / "codex-statuses.json"
        statuses = json.loads(path.read_text()) if path.exists() else {}
        statuses[ticket] = status
        path.write_text(json.dumps(statuses))

    def vanishes(self, ticket):
        """Take that child's session off its account's list, as a session that died leaves it.

        Safe only once that child's launch is verified — see `verified_launch`.
        """
        worktree = os.path.realpath(self.worktree(ticket))
        home = self.account_of(ticket)
        self.set_agents([
            agent for agent in self.agents(home)
            if os.path.realpath(agent.get("cwd", "")) != worktree
        ], home)

    # --- what the run left behind -----------------------------------------------------------

    @property
    def run_dir(self):
        return self.feature_dir / RUN_DIR_NAME

    def table(self):
        return json.loads((self.run_dir / "wave-table.json").read_text())

    def log_records(self):
        path = self.run_dir / "log.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def edit_log(self, edit):
        """Rewrite the run log through `edit(records) -> records`, next to the read it edits.

        The log is append-only and its writers share it through a single `O_APPEND` write each,
        which no rewrite can join: a test that reads every record, does something else, and only
        then writes the file back drops whatever a live driver appended in between — the run's
        own record of a launch or a receipt, gone, and the test then asserting against a log the
        driver no longer agrees with (#113, #117).

        So the read, the edit and the write happen here with no test logic between them, and
        whatever landed after the read is carried onto the end of what is written rather than
        dropped. What is left is the truncate-and-write itself, two adjacent calls wide; a test
        that cannot afford even that must doctor the log with no driver running.
        """
        path = self.run_dir / "log.jsonl"
        before = path.read_bytes()
        records = [json.loads(line) for line in before.decode().splitlines() if line.strip()]
        kept = "".join(json.dumps(record) + "\n" for record in edit(records)).encode("utf-8")
        path.write_bytes(kept + path.read_bytes()[len(before):])

    def records(self, path):
        path = pathlib.Path(path)
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def launches(self):
        return self.records(self.stub_dir / "launches.jsonl")

    def claude_calls(self):
        return self.records(self.stub_dir / "claude-calls.jsonl")

    def codex_calls(self):
        return self.records(self.stub_dir / "codex-calls.jsonl")

    def tmux_calls(self):
        return self.records(self.stub_dir / "tmux-calls.jsonl")

    def gh_calls(self):
        return self.records(self.stub_dir / "gh-calls.jsonl")

    def issues(self, table=None):
        """The issues the stubbed `gh` answers for, written or read."""
        path = self.stub_dir / "gh-issues.json"
        if table is not None:
            path.write_text(json.dumps(table))
            return table
        return json.loads(path.read_text()) if path.exists() else {}

    def windows(self):
        path = self.stub_dir / "tmux-windows.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def add_window(self, window_id, name):
        """Put a window nobody in the run recorded on the stub server, as a session's own is."""
        path = self.stub_dir / "tmux-windows.json"
        table = self.windows()
        table[window_id] = {"name": name, "target": TMUX_SESSION}
        path.write_text(json.dumps(table))
        return window_id

    def windows_named(self, name):
        return {
            window_id: window for window_id, window in self.windows().items()
            if window.get("name") == name
        }

    def branches(self):
        listed = git(self.repo, "branch", "--format=%(refname:short)").stdout
        return [line.strip() for line in listed.splitlines() if line.strip()]

    def current_branch(self):
        return git(self.repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()

    def settings(self, path):
        path = pathlib.Path(path)
        return json.loads(path.read_text()) if path.exists() else {}

    def stop_monitors(self):
        """Let every armed wake monitor exit: a run with no live children is one they leave.

        Every account's list, because a mixed wave's monitors each poll their own.
        """
        for path in self.stub_dir.glob("agents-*.json"):
            path.write_text("[]")

    def wait_for(self, condition, timeout=30.0):
        """Wait for a monitor armed to outlive the driver to do the thing it was armed to do."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return True
            time.sleep(0.2)
        return False

    def wait_for_snapshot(self, timeout=30.0):
        """Wait until an armed monitor has asked the agents list what the children are doing."""
        return self.wait_for(
            lambda: any(
                call["argv"][:2] == ["agents", "--json"] for call in self.claude_calls()
            ),
            timeout,
        )

    def wait_for_codex_watch(self, timeout=30.0):
        """Wait until the armed Codex monitor has read the sessions it watches."""
        return self.wait_for(
            lambda: any(call["argv"][:1] == ["watch"] for call in self.codex_calls()), timeout
        )

    def cleanup(self):
        for process in self.running:
            if process.poll() is None:
                process.kill()
            process.communicate()
        self.stop_monitors()
        shutil.rmtree(self.root, ignore_errors=True)


class DriverTestCase(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()
        self.addCleanup(self.fixture.cleanup)

    def snapshot(self, result):
        """The one JSON line the driver's exit carries, asserted to be the whole of its stdout."""
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, f"stdout was not one line:\n{result.stdout}")
        return json.loads(lines[0])

    def notice(self):
        """The text of the preflight notice window, as the operator would read it."""
        windows = self.fixture.windows_named(PREFLIGHT_WINDOW)
        self.assertEqual(len(windows), 1, f"preflight windows: {windows}")
        return next(iter(windows.values()))["command"]

    def assert_nothing_launched(self):
        self.assertEqual(self.fixture.launches(), [])
        self.assertNotIn(INTEGRATION_BRANCH, self.fixture.branches())
        self.assertFalse(self.fixture.run_dir.exists(), "a failed start left a run directory")

    def started(self, extra=()):
        """A run that passed preflight, caught while its loop is still running.

        A start no longer ends at the launch — the loop is the same process — so what a passing
        preflight looks like is a run directory with a table in it, not an exit code.
        """
        process = self.fixture.launch(extra=extra)
        # The diagnosis waits for the driver to end, so it is composed only where it is needed:
        # as an `assertTrue` message it was evaluated on every call, and every passing test that
        # starts a run this way waited out the loop's whole idle timeout to be told nothing.
        if not self.fixture.wait_for(
            lambda: (self.fixture.run_dir / "wave-table.json").exists()
        ):
            self.fail(f"the run never started:\n{self.fixture.ended(process, timeout=60).stdout}")
        self.assertEqual(self.fixture.windows_named(PREFLIGHT_WINDOW), {})
        self.assertIsNone(process.poll(), "the run was caught after it had already ended")
        return process

    def assert_preflight_failed(self, result, problems):
        """A preflight failure: nothing launched, one snapshot line, the full list shown."""
        self.assertNotEqual(result.returncode, 0, result.stdout)
        snapshot = self.snapshot(result)
        self.assertEqual(snapshot["reason"], "preflight-failed")
        self.assertEqual(snapshot["surface"], PREFLIGHT_WINDOW)
        self.assertEqual(snapshot["count"], problems)
        self.assert_nothing_launched()
        return self.notice()

    # --- what the run's log says --------------------------------------------------------------

    def events(self, event, **fields):
        return [
            record for record in self.fixture.log_records()
            if record.get("event") == event
            and all(record.get(key) == value for key, value in fields.items())
        ]

    def verdict(self, ticket):
        """The word the run's last settling event settled that ticket into."""
        settled = [
            record for record in self.fixture.log_records()
            if record.get("ticket") == ticket and record.get("event") in ("receipt", "outcome")
        ]
        last = settled[-1] if settled else {}
        return last.get("verdict") or last.get("outcome")

    def wait_for_verdict(self, ticket, verdict):
        self.assertTrue(
            self.fixture.wait_for(lambda: self.verdict(ticket) == verdict),
            f"{ticket} never settled {verdict}; the log says {self.verdict(ticket)}",
        )

    def woken(self, process, reason):
        """The wake snapshot the driver exited on, asserted to be the reason expected."""
        result = self.fixture.ended(process)
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        self.assertTrue(lines, f"the driver printed nothing:\n{result.stderr}")
        snapshot = json.loads(lines[-1])
        self.assertEqual(snapshot["reason"], reason, f"{snapshot}\n{result.stderr}")
        return snapshot
