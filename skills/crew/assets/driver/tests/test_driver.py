#!/usr/bin/env python3
"""Drive the crew driver from its command line against stubbed claude, tmux and codex.

Every fixture is built in a temporary root: a real git repository with a real origin to
fast-forward from, a feature directory of tickets carrying their own `## Routing` sections, and a
stub PATH carrying `claude` and `tmux`. Assertions are on external behavior only — the exit code,
the one stdout line, the wave table and machine log the run directory holds, the calls the stubs
recorded, and the repository's own branch state.
"""

import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


TESTS_DIR = pathlib.Path(__file__).resolve().parent
DRIVER = TESTS_DIR.parent / "driver.py"
MACHINE_LOG = DRIVER.parent.parent / "machine_log.py"
TRIAGE = DRIVER.parent.parent.parent / "references" / "triage.md"
MONITOR = DRIVER.parent.parent / "monitor" / "monitor.py"

CLAUDE_MODEL = "claude-opus-4-5-20251101"
CLAUDE_EFFORT = "medium"
CODEX_MODEL = "gpt-5.6-luna"
CODEX_EFFORT = "max"
COORDINATOR_NAME = "crew-coordinator-1f"
COORDINATOR_PID = 1504
PERMISSION_MODE = "acceptEdits"
TMUX_SESSION = "$7:"

PREFLIGHT_WINDOW = "crew-preflight"
DASHBOARD_WINDOW = "crew-dashboard"
REPORT_NAME = "report.md"
RUN_DIR_NAME = ".crew"
FEATURE_NAME = "demo"
INTEGRATION_BRANCH = "crew/demo"
BASE_BRANCH = "main"
# The two decisions the project's config carries, which the driver records into the run.
REPAIR_MODEL = "claude-sonnet-5"
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

    # --- the project's config -------------------------------------------------------------

    def write_config(self, repair_model=REPAIR_MODEL, tracker=TRACKER, surface=None,
                     accounts=None):
        """The project config the run reads its repair model and its tracker out of."""
        lines = []
        if repair_model is not None:
            lines += ["[repair]", f'model = "{repair_model}"']
        if tracker is not None:
            lines += ["[tracker]", f'kind = "{tracker}"']
        if surface is not None:
            lines += ["[dashboard]", f'surface = "{surface}"']
        if accounts is not None:
            names = ", ".join(f'"{name}"' for name in accounts)
            lines += ["[accounts]", f"names = [{names}]"]
        (self.repo / "agentcrew.toml").write_text("\n".join(lines) + "\n")

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
        environment["PATH"] = f"{self.bin_dir}{os.pathsep}{environment['PATH']}"
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
            "--permission-mode", PERMISSION_MODE,
            "--tmux-session", TMUX_SESSION,
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

    def resume(self, extra=()):
        """Put the loop back where a ruling stopped it, and leave it running."""
        process = subprocess.Popen(
            [
                sys.executable, str(DRIVER), "resume",
                "--feature-dir", str(self.feature_dir),
                "--coordinator-pid", str(COORDINATOR_PID),
                "--tmux-session", TMUX_SESSION,
                "--poll-seconds", POLL_SECONDS,
                "--timeout", LOOP_TIMEOUT,
                *extra,
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=self.environment(), cwd=str(self.repo),
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

    def agents(self):
        path = self.stub_dir / "agents.json"
        return json.loads(path.read_text()) if path.exists() else []

    def set_agents(self, agents):
        (self.stub_dir / "agents.json").write_text(json.dumps(agents))

    def goes(self, ticket, status):
        """Put that child into the status the agents list reports it in."""
        worktree = os.path.realpath(self.worktree(ticket))
        agents = self.agents()
        for agent in agents:
            if os.path.realpath(agent.get("cwd", "")) == worktree:
                agent["status"] = status
        self.set_agents(agents)

    def vanishes(self, ticket):
        """Take that child's session off the agents list, as a session that died leaves it."""
        worktree = os.path.realpath(self.worktree(ticket))
        self.set_agents([
            agent for agent in self.agents()
            if os.path.realpath(agent.get("cwd", "")) != worktree
        ])

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
        """Let every armed wake monitor exit: a run with no live children is one they leave."""
        (self.stub_dir / "agents.json").write_text("[]")

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
        self.assertTrue(
            self.fixture.wait_for(lambda: (self.fixture.run_dir / "wave-table.json").exists()),
            f"the run never started:\n{self.fixture.ended(process, timeout=60).stdout}",
        )
        self.assertEqual(self.fixture.windows_named(PREFLIGHT_WINDOW), {})
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


class PreflightTests(DriverTestCase):
    def test_a_dirty_working_tree_stops_the_run(self):
        self.fixture.ticket("01", "first thing")
        self.fixture.commit_feature()
        (self.fixture.repo / "README.md").write_text("edited\n")

        result = self.fixture.start()

        notice = self.assert_preflight_failed(result, 1)
        self.assertIn("README.md", notice)
        self.assertIn("committed", notice)

    def test_an_untracked_file_is_not_a_preflight_failure(self):
        self.fixture.ticket("01", "first thing")
        self.fixture.commit_feature()
        (self.fixture.repo / "scratch.txt").write_text("not mine\n")

        self.started()

    def test_a_base_branch_that_cannot_fast_forward_stops_the_run(self):
        """The check reads what origin holds now: this checkout has never fetched the divergence."""
        self.fixture.ticket("01", "first thing")
        self.fixture.commit_feature()
        other = self.fixture.root / "other"
        subprocess.run(
            ["git", "clone", str(self.fixture.origin), str(other)],
            check=True, capture_output=True,
        )
        git(other, "config", "user.email", "crew@example.invalid")
        git(other, "config", "user.name", "Crew Test")
        (other / "elsewhere.md").write_text("theirs\n")
        git(other, "add", "elsewhere.md")
        git(other, "commit", "-m", "theirs")
        git(other, "push", "origin", BASE_BRANCH)
        (self.fixture.repo / "mine.md").write_text("mine\n")
        git(self.fixture.repo, "add", "mine.md")
        git(self.fixture.repo, "commit", "-m", "mine")

        result = self.fixture.start()

        notice = self.assert_preflight_failed(result, 1)
        self.assertIn(BASE_BRANCH, notice)
        self.assertIn("fast-forward", notice)

    def test_an_origin_that_cannot_be_reached_stops_the_run(self):
        self.fixture.ticket("01", "first thing")
        self.fixture.commit_feature()
        git(self.fixture.repo, "remote", "set-url", "origin", str(self.fixture.root / "gone.git"))

        result = self.fixture.start()

        notice = self.assert_preflight_failed(result, 1)
        self.assertIn("origin", notice)
        self.assertIn(BASE_BRANCH, notice)

    def test_a_missing_default_base_branch_names_the_repository_fix(self):
        self.fixture.ticket("01", "first thing")
        self.fixture.commit_feature()
        git(self.fixture.repo, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")

        result = self.fixture.start()

        notice = self.assert_preflight_failed(result, 1)
        self.assertIn("git remote set-head origin -a", notice)
        self.assertIn("--base-branch <branch>", notice)

    def test_a_base_branch_origin_does_not_carry_has_nothing_to_fast_forward(self):
        self.fixture.ticket("01", "first thing")
        self.fixture.commit_feature()
        git(self.fixture.repo, "branch", "local-base")

        self.started(extra=("--base-branch", "local-base"))

        self.assertEqual(self.fixture.table()["run"]["base_branch"], "local-base")

    def test_a_base_branch_origin_has_deleted_starts_from_what_is_local(self):
        """The stale remote-tracking ref left behind is not something to fast-forward onto."""
        self.fixture.ticket("01", "first thing")
        self.fixture.commit_feature()
        subprocess.run(
            ["git", "-C", str(self.fixture.origin), "update-ref", "-d",
             f"refs/heads/{BASE_BRANCH}"],
            check=True, capture_output=True,
        )

        self.started()

        self.assertIn(INTEGRATION_BRANCH, self.fixture.branches())

    def test_a_base_branch_that_does_not_resolve_stops_the_run(self):
        self.fixture.ticket("01", "first thing")
        self.fixture.commit_feature()

        result = self.fixture.start(extra=("--base-branch", "no-such-branch"))

        notice = self.assert_preflight_failed(result, 1)
        self.assertIn("no-such-branch", notice)

    def test_an_unrouted_ticket_and_an_alias_model_are_rejected_as_the_renderer_rejects_them(self):
        self.fixture.ticket("01", "no routing", routing="")
        self.fixture.ticket("02", "aliased", routing=ROUTING.replace(CLAUDE_MODEL, "opus"))
        self.fixture.commit_feature()

        result = self.fixture.start()

        notice = self.assert_preflight_failed(result, 2)
        self.assertIn("01", notice)
        self.assertIn("Workflow", notice)
        self.assertIn("02", notice)
        self.assertIn("full model ID", notice)

    def test_a_review_lane_on_its_own_executor_is_rejected(self):
        self.fixture.ticket(
            "01", "same vendor", routing=ROUTING.replace(
                f"Review: codex {CODEX_MODEL} {CODEX_EFFORT}",
                f"Review: claude {CLAUDE_MODEL} {CLAUDE_EFFORT}",
            ),
        )
        self.fixture.commit_feature()

        result = self.fixture.start()

        notice = self.assert_preflight_failed(result, 1)
        self.assertIn("Review", notice)

    def test_a_feature_carrying_no_tickets_names_the_path_the_pattern_and_the_archive_trap(self):
        """The layout mistake is diagnosable from the error alone.

        A run directory assembled by hand puts its tickets where a finished run puts its archive
        often enough that the error names both: where the driver looked, the filename it wanted
        there, and what `tickets/` actually is.
        """
        archived = self.fixture.feature_dir / "tickets"
        archived.mkdir()
        (archived / "01.md").write_text("# a ticket in the archive layout\n")
        self.fixture.commit_feature()

        result = self.fixture.start()

        notice = self.assert_preflight_failed(result, 1)
        self.assertIn(str(self.fixture.feature_dir), notice)
        self.assertIn("<number>.md", notice)
        self.assertIn("tickets/", notice)
        self.assertIn("archive", notice)

    def test_a_blocker_no_ticket_carries_stops_the_run(self):
        self.fixture.ticket("01", "first thing", blocked_by=("09",))
        self.fixture.commit_feature()

        result = self.fixture.start()

        notice = self.assert_preflight_failed(result, 1)
        self.assertIn("09", notice)

    def test_a_dependency_cycle_stops_the_run(self):
        self.fixture.ticket("01", "first thing", blocked_by=("02",))
        self.fixture.ticket("02", "second thing", blocked_by=("01",))
        self.fixture.commit_feature()

        result = self.fixture.start()

        notice = self.assert_preflight_failed(result, 1)
        self.assertIn("cycle", notice)
        self.assertIn("01", notice)
        self.assertIn("02", notice)

    def test_every_problem_of_a_failed_start_is_listed_at_once(self):
        self.fixture.ticket("01", "no routing", routing="")
        self.fixture.ticket("02", "second thing", blocked_by=("09",))
        self.fixture.commit_feature()
        (self.fixture.repo / "README.md").write_text("edited\n")

        result = self.fixture.start()

        notice = self.assert_preflight_failed(result, 3)
        self.assertIn("README.md", notice)
        self.assertIn("01", notice)
        self.assertIn("09", notice)

    def test_the_notice_of_a_previous_failed_start_is_cleared_by_the_next_start(self):
        self.fixture.ticket("01", "no routing", routing="")
        self.fixture.commit_feature()

        first = self.fixture.start()
        stale = self.notice()
        second = self.fixture.start()

        self.assertNotEqual(first.returncode, 0)
        self.assertNotEqual(second.returncode, 0)
        windows = self.fixture.windows_named(PREFLIGHT_WINDOW)
        self.assertEqual(len(windows), 1, f"a stale notice outlived its start: {windows}")
        self.assertEqual(next(iter(windows.values()))["command"], stale)
        kills = [call for call in self.fixture.tmux_calls() if call["argv"][0] == "kill-window"]
        self.assertEqual(len(kills), 1, f"the stale notice was not killed: {kills}")
        listings = [
            call for call in self.fixture.tmux_calls() if call["argv"][0] == "list-windows"
        ]
        self.assertTrue(listings, "the driver never asked which windows the session holds")
        for listing in listings:
            self.assertIn("-t", listing["argv"], "a notice was searched for outside the session")
            self.assertIn(TMUX_SESSION, listing["argv"])

    def test_a_session_tmux_does_not_hold_is_a_driver_error(self):
        """A surface that cannot be read is never reported as one the problem list was drawn on."""
        self.fixture.ticket("01", "no routing", routing="")
        self.fixture.commit_feature()

        result = self.fixture.start(extra=("--tmux-session", "$99:"))

        self.assertNotEqual(result.returncode, 0)
        snapshot = self.snapshot(result)
        self.assertEqual(snapshot["reason"], "driver-error")
        self.assertEqual(self.fixture.windows_named(PREFLIGHT_WINDOW), {})

    def test_a_notice_that_cannot_be_drawn_is_a_driver_error_naming_the_count(self):
        self.fixture.ticket("01", "no routing", routing="")
        self.fixture.ticket("02", "aliased", routing=ROUTING.replace(CLAUDE_MODEL, "opus"))
        self.fixture.commit_feature()
        (self.fixture.stub_dir / "tmux-new-window-fails").write_text("yes\n")

        result = self.fixture.start()

        self.assertNotEqual(result.returncode, 0)
        snapshot = self.snapshot(result)
        self.assertEqual(snapshot["reason"], "driver-error")
        self.assertIn("2 problems", snapshot["detail"])
        self.assert_nothing_launched()

    def test_a_config_naming_no_repair_model_stops_the_run(self):
        self.fixture.ticket("01", "first thing")
        self.fixture.commit_feature()
        self.fixture.configure(repair_model=None)

        result = self.fixture.start()

        notice = self.assert_preflight_failed(result, 1)
        self.assertIn("repair", notice)

    def test_an_aliased_repair_model_is_rejected_as_a_routed_one_is(self):
        self.fixture.ticket("01", "first thing")
        self.fixture.commit_feature()
        self.fixture.configure(repair_model="sonnet")

        result = self.fixture.start()

        notice = self.assert_preflight_failed(result, 1)
        self.assertIn("full model ID", notice)

    def test_a_config_naming_no_tracker_stops_the_run(self):
        self.fixture.ticket("01", "first thing")
        self.fixture.commit_feature()
        self.fixture.configure(tracker=None)

        result = self.fixture.start()

        notice = self.assert_preflight_failed(result, 1)
        self.assertIn("tracker", notice)

    def test_a_tracker_no_run_closes_tickets_in_stops_the_run(self):
        self.fixture.ticket("01", "first thing")
        self.fixture.commit_feature()
        self.fixture.configure(tracker="jira")

        result = self.fixture.start()

        notice = self.assert_preflight_failed(result, 1)
        self.assertIn("jira", notice)
        self.assertIn("github", notice)

    def test_a_passing_start_clears_the_notice_of_the_start_before_it(self):
        self.fixture.ticket("01", "no routing", routing="")
        self.fixture.commit_feature()
        self.fixture.start()
        self.fixture.ticket("01", "first thing")
        git(self.fixture.repo, "add", "-A", "features")
        git(self.fixture.repo, "commit", "-m", "route it")

        self.started()


class AccountTests(DriverTestCase):
    """Which account each ticket of a run spends on, from the ticket line to the wave table.

    An account is a name in the ticket and a profile directory on the machine, and the two are
    joined by the machine-level registry the override here points at. Every assertion is on what
    the run wrote or refused: the table it built, and the preflight notice it stopped on.
    """

    def rows(self):
        """Every ticket of the built table, by its number."""
        return {
            ticket["id"]: ticket
            for wave in self.fixture.table()["waves"] for ticket in wave["tickets"]
        }

    def test_a_ticket_naming_no_account_carries_the_coordinators_own_configuration_home(self):
        self.fixture.ticket("01", "first thing")
        self.fixture.ticket("02", "second thing")
        self.fixture.commit_feature()

        self.started()

        table = self.fixture.table()
        self.assertEqual(
            table["run"]["coordinator_config_home"], str(self.fixture.config_dir)
        )
        for number, row in self.rows().items():
            self.assertEqual(row["account"], str(self.fixture.config_dir), number)

    def test_a_ticket_naming_a_registered_account_carries_that_accounts_profile_directory(self):
        profile = self.fixture.profile("second")
        self.fixture.register(second=profile)
        self.fixture.ticket("01", "first thing", routing=routing_naming("second"))
        self.fixture.ticket("02", "second thing")
        self.fixture.commit_feature()

        self.started()

        rows = self.rows()
        self.assertEqual(rows["01"]["account"], str(profile))
        self.assertEqual(rows["02"]["account"], str(self.fixture.config_dir))

    def test_a_registry_nothing_asks_for_is_never_read(self):
        """No ticket names an account, so no registry is opened — broken or otherwise.

        The file is only needed once a ticket asks for an account, which is what keeps this
        feature free for the operator who never uses it.
        """
        self.fixture.registry.write_text("this is not = = toml\n")
        self.fixture.ticket("01", "first thing")
        self.fixture.commit_feature()

        self.started()

        self.assertEqual(
            self.rows()["01"]["account"], str(self.fixture.config_dir)
        )

    def test_a_registry_override_that_is_not_an_absolute_path_stops_the_run(self):
        """One override, one registry: a relative path would name a different file per process."""
        self.fixture.ticket("01", "first thing", routing=routing_naming("second"))
        self.fixture.commit_feature()

        result = self.fixture.start(env_overrides={"AGENTCREW_ACCOUNT_REGISTRY": "accounts.toml"})

        notice = self.assert_preflight_failed(result, 1)
        self.assertIn("AGENTCREW_ACCOUNT_REGISTRY", notice)
        self.assertIn("accounts.toml", notice)

    def test_a_ticket_naming_an_unregistered_account_stops_the_run(self):
        self.fixture.register(second=self.fixture.profile("second"))
        self.fixture.ticket("01", "first thing", routing=routing_naming("third"))
        self.fixture.commit_feature()

        result = self.fixture.start()

        notice = self.assert_preflight_failed(result, 1)
        self.assertIn("third", notice)
        self.assertIn(str(self.fixture.registry), notice)

    def test_a_machine_with_no_registry_file_stops_a_run_whose_ticket_names_an_account(self):
        self.fixture.ticket("01", "first thing", routing=routing_naming("second"))
        self.fixture.commit_feature()

        result = self.fixture.start()

        notice = self.assert_preflight_failed(result, 1)
        self.assertIn("second", notice)
        self.assertIn(str(self.fixture.registry), notice)

    def test_an_account_the_config_never_declared_is_told_apart_from_an_unregistered_one(self):
        self.fixture.register(second=self.fixture.profile("second"))
        self.fixture.ticket("01", "first thing", routing=routing_naming("second"))
        self.fixture.commit_feature()
        self.fixture.configure(accounts=["first"])

        result = self.fixture.start()

        notice = self.assert_preflight_failed(result, 1)
        self.assertIn("second", notice)
        self.assertIn("agentcrew.toml", notice)
        self.assertNotIn(str(self.fixture.registry), notice)

    def test_the_default_registry_is_not_the_one_the_coordinators_own_profile_holds(self):
        """The registry is machine-level: a copy under CLAUDE_CONFIG_DIR is not one (ADR-0013).

        Driven with the override emptied, so the run resolves the default location itself — under
        the home directory, and never under the configuration home the coordinator's own account
        moves.
        """
        home = self.fixture.root / "home"
        (home / ".claude").mkdir(parents=True)
        shadow = self.fixture.config_dir / "agentcrew" / "accounts.toml"
        shadow.parent.mkdir(parents=True)
        shadow.write_text(f'[accounts]\nsecond = "{self.fixture.profile("second")}"\n')
        self.fixture.ticket("01", "first thing", routing=routing_naming("second"))
        self.fixture.commit_feature()

        result = self.fixture.start(
            env_overrides={"HOME": str(home), "AGENTCREW_ACCOUNT_REGISTRY": ""}
        )

        notice = self.assert_preflight_failed(result, 1)
        self.assertIn(str(home / ".claude" / "agentcrew" / "accounts.toml"), notice)

    def test_an_account_whose_profile_directory_is_not_there_stops_the_run(self):
        missing = self.fixture.root / "profiles" / "gone"
        self.fixture.register(second=missing)
        self.fixture.ticket("01", "first thing", routing=routing_naming("second"))
        self.fixture.commit_feature()

        result = self.fixture.start()

        notice = self.assert_preflight_failed(result, 1)
        self.assertIn("second", notice)
        self.assertIn(str(missing), notice)


class LaunchTests(DriverTestCase):
    """What one start puts on the ground before its wave loop settles anything.

    The driver goes on running after the launch — the loop is the same process — so each of these
    watches a live run rather than reading what a finished one left behind.
    """

    def start_a_run(self, extra=(), env_overrides=None):
        self.fixture.ticket("01", "first thing")
        self.fixture.ticket("02", "second thing", blocked_by=("01",))
        self.fixture.commit_feature()
        return self.launched(extra=extra, env_overrides=env_overrides)

    def launched(self, extra=(), env_overrides=None):
        """A run whose first wave is fully up, still running its loop.

        Waited for by the last thing the launch writes rather than the first, so a test that reads
        a hook, a window or the run directory is never racing the launch that puts them there.
        """
        process = self.fixture.launch(extra=extra, env_overrides=env_overrides)
        self.assertTrue(
            self.fixture.wait_for(
                lambda: self.fixture.launch_record("01") is not None
                and (self.fixture.run_dir / "parked-paths").exists()
            ),
            "wave 1 never launched",
        )
        return process

    def test_the_launch_line_names_the_run_directory(self):
        process = self.start_a_run()

        line = process.stdout.readline().strip()
        self.assertIn(str(self.fixture.run_dir), line)

    def test_the_table_carries_every_ticket_in_its_wave_with_the_routing_it_declared(self):
        self.start_a_run()

        table = self.fixture.table()
        waves = {wave["wave"]: wave["tickets"] for wave in table["waves"]}
        self.assertEqual(sorted(waves), [1, 2])
        self.assertEqual([ticket["id"] for ticket in waves[1]], ["01"])
        self.assertEqual([ticket["id"] for ticket in waves[2]], ["02"])
        first = waves[1][0]
        self.assertEqual(first["title"], "first thing")
        self.assertEqual(first["path"], str(self.fixture.feature_dir / "01.md"))
        self.assertEqual(first["workflow"], "tdd")
        self.assertEqual(first["executor"], "claude")
        self.assertEqual(first["model"], CLAUDE_MODEL)
        self.assertEqual(first["effort"], CLAUDE_EFFORT)
        self.assertEqual(
            first["review"],
            {"vendor": "codex", "model": CODEX_MODEL, "effort": CODEX_EFFORT},
        )
        self.assertEqual(first["blocked_by"], [])
        self.assertEqual(waves[2][0]["blocked_by"], ["01"])

    def test_the_table_records_the_run_the_branches_were_prepared_for(self):
        self.start_a_run()

        run = self.fixture.table()["run"]
        head = git(self.fixture.repo, "rev-parse", INTEGRATION_BRANCH).stdout.strip()
        self.assertEqual(run["repo_root"], str(self.fixture.repo))
        self.assertEqual(run["spec_path"], str(self.fixture.spec_path))
        self.assertEqual(run["integration_branch"], INTEGRATION_BRANCH)
        self.assertEqual(run["integration_base_commit"], head)
        self.assertEqual(run["base_branch"], BASE_BRANCH)
        self.assertEqual(run["return_branch"], BASE_BRANCH)
        self.assertEqual(run["coordinator_name"], COORDINATOR_NAME)
        self.assertEqual(run["coordinator_pid"], COORDINATOR_PID)
        self.assertEqual(run["tmux_session"], TMUX_SESSION)
        self.assertEqual(run["permission_mode"], PERMISSION_MODE)
        self.assertEqual(run["repair_model"], REPAIR_MODEL)
        self.assertEqual(run["tracker"], TRACKER)

    def test_a_relative_path_is_recorded_absolute(self):
        """Every path the table carries is read in a child's worktree, never in this cwd."""
        self.fixture.ticket("01", "first thing")
        self.fixture.commit_feature()

        self.launched(extra=("--spec", "features/demo/spec.md"))

        self.assertEqual(self.fixture.table()["run"]["spec_path"], str(self.fixture.spec_path))

    def test_the_integration_branch_is_cut_and_checked_out(self):
        self.start_a_run()

        self.assertIn(INTEGRATION_BRANCH, self.fixture.branches())
        self.assertEqual(self.fixture.current_branch(), INTEGRATION_BRANCH)

    def test_only_wave_one_is_dispatched_and_its_launch_amendment_is_logged(self):
        self.start_a_run()

        launched = [
            record for record in self.fixture.log_records() if record["event"] == "launch"
        ]
        self.assertEqual([record["ticket"] for record in launched], ["01", "01"])
        self.assertEqual(launched[0]["child"], "")
        self.assertEqual(launched[1]["child"], "stub-child-1")
        self.assertEqual(len(self.fixture.launches()), 1)
        self.assertEqual(
            launched[1]["worktree"],
            str(self.fixture.repo / ".claude" / "worktrees" / "01-01"),
        )
        self.assertEqual(launched[1]["model"], CLAUDE_MODEL)

    def test_the_run_directory_holds_the_layout_the_index_names(self):
        self.start_a_run()

        self.assertEqual(
            sorted(path.name for path in self.fixture.run_dir.iterdir()),
            sorted([
                "wave-table.json", "log.jsonl", "launch", "parked-paths",
                "dashboard-window", "dashboard-window.lock", "machine_log.py",
            ]),
        )

    def test_the_coordinator_and_the_child_carry_this_run_s_hooks(self):
        self.start_a_run()

        log = str(self.fixture.run_dir / "log.jsonl")
        coordinator = json.dumps(
            self.fixture.settings(self.fixture.repo / ".claude" / "settings.local.json")
        )
        child = json.dumps(self.fixture.settings(
            self.fixture.repo / ".claude" / "worktrees" / "01-01"
            / ".claude" / "settings.local.json"
        ))
        self.assertIn(log, coordinator)
        self.assertIn("--role coordinator", coordinator)
        self.assertIn(log, child)
        self.assertIn("--role child", child)
        self.assertIn("--ticket 01", child)

    def test_the_dashboard_is_started_on_the_run(self):
        self.start_a_run()

        windows = self.fixture.windows_named(DASHBOARD_WINDOW)
        self.assertEqual(len(windows), 1, f"dashboard windows: {windows}")
        command = next(iter(windows.values()))["command"]
        self.assertIn("monitor.py", command)
        self.assertIn(str(self.fixture.run_dir), command)
        recorded = (self.fixture.run_dir / "dashboard-window").read_text().strip()
        self.assertEqual(recorded, next(iter(windows)))

    def test_a_machine_pin_preference_starts_the_silent_project_without_a_dashboard_window(self):
        installed = self.fixture.pin_install()
        self.assertEqual(installed.returncode, 0, installed.stderr)

        self.start_a_run()

        self.assertEqual(self.fixture.windows_named(DASHBOARD_WINDOW), {})
        pins = list((self.fixture.config_dir / "agentcrew" / "pins").glob("*.json"))
        self.assertEqual(len(pins), 1)

    def test_the_wake_monitor_is_armed_over_the_wave_s_children(self):
        self.start_a_run()

        self.assertTrue(
            self.fixture.wait_for_snapshot(),
            "no wake monitor asked the agents list what the wave's children are doing",
        )

    def test_a_codex_child_is_launched_and_watched_through_the_bridge(self):
        self.fixture.ticket("01", "first thing", routing=CODEX_ROUTING)
        self.fixture.commit_feature()

        self.launched()

        self.assertTrue(
            self.fixture.wait_for_codex_watch(),
            "no wake monitor watched the wave's Codex child",
        )
        commands = [call["argv"][0] for call in self.fixture.codex_calls()]
        self.assertIn("launch", commands)
        watch = next(call for call in self.fixture.codex_calls() if call["argv"][0] == "watch")
        self.assertIn(str(self.fixture.run_dir / "codex" / "01.json"), watch["argv"])

    def test_a_child_that_will_not_come_up_exits_with_a_driver_error_snapshot(self):
        self.fixture.ticket("01", "first thing")
        self.fixture.commit_feature()

        result = self.fixture.start(
            env_overrides={"AGENTCREW_STUB_TRANSCRIPT_MODEL": "claude-haiku-4-5-20251001"}
        )

        self.assertNotEqual(result.returncode, 0)
        snapshot = self.snapshot(result)
        self.assertEqual(snapshot["reason"], "driver-error")
        self.assertEqual(snapshot["ticket"], "01")
        self.assertIn(str(self.fixture.run_dir), snapshot["pointer"])


class LoopTests(DriverTestCase):
    """The rule table, row by row, driven at the driver's own command line.

    Every child is stubbed, so the run is driven the way the run itself observes one: work is
    committed in a real worktree, the child's word is written into the machine log by the same
    writer its hook uses, and the agents list is rewritten to make a session idle or vanish.
    """

    def feature(self, *tickets, shared=None, routing=ROUTING):
        """A feature of tickets, and a file in the base commit two children can collide over."""
        for number, blockers in tickets:
            self.fixture.ticket(number, f"thing {number}", routing=routing, blocked_by=blockers)
        if shared is not None:
            (self.fixture.repo / "shared.txt").write_text(shared)
            git(self.fixture.repo, "add", "shared.txt")
        self.fixture.commit_feature()

    def start(self, *tickets, shared=None, env_overrides=None, routing=ROUTING):
        """A run with its first wave up and its loop running."""
        self.feature(*tickets, shared=shared, routing=routing)
        process = self.fixture.launch(env_overrides=env_overrides)
        for number, _ in tickets:
            if not _:
                self.assertTrue(
                    self.fixture.wait_for(
                        lambda number=number: self.fixture.launch_record(number) is not None
                    ),
                    f"{number} never launched",
                )
        return process

    # --- what the log says ------------------------------------------------------------------

    def instructions(self, ticket, marker):
        return [
            record for record in self.events("ruling", ticket=ticket)
            if str(record.get("message", "")).startswith(marker)
        ]

    def wait_for_instruction(self, ticket, marker):
        self.assertTrue(
            self.fixture.wait_for(lambda: self.instructions(ticket, marker)),
            f"{ticket} was never sent a {marker}",
        )
        return self.instructions(ticket, marker)[-1]["message"]

    # --- a whole run, with nothing outside the table in it ------------------------------------

    def test_a_clean_run_settles_every_wave_and_ends_without_one_wake(self):
        process = self.start(("01", ()), ("02", ("01",)))

        self.fixture.completes("01")
        self.assertTrue(
            self.fixture.wait_for(lambda: self.fixture.launch_record("02") is not None),
            "the run never advanced to wave 2",
        )
        self.fixture.completes("02")
        snapshot = self.woken(process, "run-complete")

        self.assertEqual(snapshot["ticket"], None)
        self.assertEqual([self.verdict("01"), self.verdict("02")], ["completed", "completed"])
        self.assertEqual(
            [record["result"] for record in self.events("merge")], ["clean", "clean"]
        )
        self.assertEqual(
            [record["decision"] for record in self.events("advance")], ["launched", "complete"]
        )
        self.assertEqual(self.events("ruling"), [], "a clean run instructed a child")

    def test_a_completed_run_writes_the_report_and_names_it_in_the_final_snapshot(self):
        process = self.start(("01", ()), env_overrides={"CLAUDE_CODE_SESSION_ID": ""})

        self.fixture.completes("01")
        snapshot = self.woken(process, "run-complete")

        report_path = self.fixture.feature_dir / "report.md"
        self.assertTrue(report_path.exists())
        self.assertEqual(snapshot["report"], str(report_path))
        report = report_path.read_text()
        self.assertIn(
            "| NN | Workflow | Executor | Model | Effort | Outcome | Launched | Received | Duration |",
            report,
        )
        self.assertRegex(
            report,
            rf"\| 01 \| tdd \| claude \| {re.escape(CLAUDE_MODEL)} \| {CLAUDE_EFFORT} \|"
            r" completed \|",
        )
        self.assertIn(INTEGRATION_BRANCH, report)
        self.assertIn("human", report.lower())
        self.assertIn("TOTAL", report)
        cost = report.split("## Cost", 1)[1]
        self.assertRegex(cost, r"(?m)^coordinator\s+claude(?:\s+--){6}\s*$")
        self.assertIn("coordinator not measured:", cost)
        self.assertIn("session-wide upper bound", report)
        self.assertEqual(
            [record["ticket"] for record in self.events("session-cost")], ["01"]
        )
        self.assertEqual(snapshot["reason"], "run-complete")

    def test_a_pin_surface_removes_its_pin_and_a_window_surface_has_none_to_remove(self):
        self.fixture.configure(surface="pin")
        process = self.start(("01", ()), env_overrides={"CLAUDE_CODE_SESSION_ID": ""})
        pin_dir = self.fixture.config_dir / "agentcrew" / "pins"
        self.assertTrue(
            self.fixture.wait_for(lambda: list(pin_dir.glob("*.json"))),
            "the pin surface did not write its run pin",
        )

        self.fixture.completes("01")
        self.woken(process, "run-complete")

        self.assertEqual(list(pin_dir.glob("*.json")), [])
        self.assertEqual(self.fixture.windows_named(DASHBOARD_WINDOW), {})

    def test_a_report_render_error_still_removes_the_pin_and_run_hooks(self):
        self.fixture.configure(surface="pin")
        process = self.start(("01", ()), env_overrides={"CLAUDE_CODE_SESSION_ID": ""})
        pin_dir = self.fixture.config_dir / "agentcrew" / "pins"
        self.assertTrue(self.fixture.wait_for(lambda: list(pin_dir.glob("*.json"))))

        records = self.fixture.log_records()
        records[0]["ts"] = "not-a-machine-log-timestamp"
        (self.fixture.run_dir / "log.jsonl").write_text(
            "\n".join(json.dumps(record) for record in records) + "\n"
        )
        self.fixture.completes("01")

        self.woken(process, "driver-error")
        self.assertEqual(list(pin_dir.glob("*.json")), [])
        log = str(self.fixture.run_dir / "log.jsonl")
        coordinator_settings = self.fixture.settings(
            self.fixture.repo / ".claude" / "settings.local.json"
        )
        self.assertNotIn(log, json.dumps(coordinator_settings))
        child_settings = self.fixture.worktree("01") / ".claude" / "settings.local.json"
        self.assertNotIn(log, json.dumps(self.fixture.settings(child_settings)))

    def test_the_cost_pass_reads_the_coordinator_transcript_into_its_own_row(self):
        session = "fixture-coordinator-session"
        self.fixture.coordinator_transcript(session)
        process = self.start(("01", ()), env_overrides={"CLAUDE_CODE_SESSION_ID": session})

        self.fixture.completes("01")
        self.woken(process, "run-complete")

        report = (self.fixture.feature_dir / "report.md").read_text()
        self.assertEqual(self.fixture.table()["run"]["coordinator_session"], session)
        coordinator_line = next(
            line for line in report.splitlines() if line.startswith("coordinator")
        )
        self.assertRegex(coordinator_line, r"\b\d+\s+\d+\s+\d+\s+\d+\s+\d+$")

    def test_the_report_accounts_for_each_outcome_ruling_undo_and_duration_row(self):
        process = self.start(
            ("01", ()), ("02", ()), ("03", ()), ("04", ("02",)),
            env_overrides={"CLAUDE_CODE_SESSION_ID": ""},
        )

        self.fixture.completes("01")
        self.fixture.says("02", "CREW PARKED features/demo/checklist-02.md")
        self.fixture.says("03", "CREW COMPLETE " + "0" * 40)
        self.wait_for_instruction("03", "CREW RECHECK")
        self.fixture.says("03", "CREW COMPLETE " + "1" * 40)
        self.woken(process, "run-complete")

        report = (self.fixture.feature_dir / "report.md").read_text()
        self.assertIn("## Outcomes", report)
        self.assertIn("| 01 | thing 01 | completed |", report)
        self.assertIn("| 02 | thing 02 | parked |", report)
        self.assertIn("| 03 | thing 03 | failed |", report)
        self.assertIn("| 04 | thing 04 | blocked |", report)
        self.assertIn("features/demo/checklist-02.md", report)
        self.assertIn("## Failed receipts and sessions", report)
        failed_section = report.split("## Failed receipts and sessions", 1)[1].split(
            "## Rulings", 1
        )[0]
        self.assertIn("- 03:", failed_section)
        self.assertIn("second receipt", failed_section)
        self.assertIn("## Rulings", report)
        self.assertIn("CREW RECHECK 03", report)
        self.assertIn("## Outside-worktree effects", report)
        self.assertIn("undo:", report)
        self.assertIn("## Integration branch", report)
        self.assertIn("## Durations", report)
        self.assertIn("| 01 | tdd | claude |", report)
        self.assertIn("| 03 | tdd | claude |", report)
        self.assertNotIn("| 04 | tdd |", report)
        self.assertIn("## Cost", report)
        cost_section = report.split("## Cost", 1)[1]
        self.assertIn("TOTAL", cost_section)
        self.assertRegex(cost_section, r"(?m)^coordinator\s+claude(?:\s+--){6}\s*$")
        self.assertIn("coordinator not measured:", cost_section)
        self.assertEqual(
            sorted(record["ticket"] for record in self.events("session-cost")),
            ["01", "02", "03"],
        )

    def test_a_receipt_that_verifies_settles_the_ticket_without_a_word_to_anyone(self):
        process = self.start(("01", ()))

        self.fixture.completes("01")
        self.woken(process, "run-complete")

        landable = self.events("receipt", ticket="01", verdict="landable")
        self.assertEqual(len(landable), 1, self.fixture.log_records())
        self.assertEqual(self.events("ruling", ticket="01"), [])

    def missing_launch_snapshot(self, message):
        self.feature(("01", ()))
        process = self.fixture.launch(extra=("--timeout", "5"))
        self.assertTrue(
            self.fixture.wait_for(
                lambda: (self.fixture.launch_record("01") or {}).get("child")
            ),
            "01 never finished launch verification",
        )
        records = [
            record for record in self.fixture.log_records()
            if record.get("event") != "launch"
        ]
        (self.fixture.run_dir / "log.jsonl").write_text(
            "\n".join(json.dumps(record) for record in records) + "\n"
        )
        self.fixture.says("01", message)
        return self.woken(process, "driver-error")

    def test_a_completion_without_a_launch_record_is_a_driver_error(self):
        snapshot = self.missing_launch_snapshot("CREW COMPLETE " + "0" * 40)

        self.assertEqual(snapshot["ticket"], "01")
        self.assertIn("CREW COMPLETE", snapshot["detail"])
        self.assertIn("no launch record", snapshot["detail"])

    def test_a_parked_receipt_without_a_launch_record_is_a_driver_error(self):
        snapshot = self.missing_launch_snapshot("CREW PARKED features/demo/checklist.md")

        self.assertEqual(snapshot["ticket"], "01")
        self.assertIn("CREW PARKED", snapshot["detail"])
        self.assertIn("no launch record", snapshot["detail"])

    def test_a_failure_receipt_without_a_launch_record_is_a_driver_error(self):
        snapshot = self.missing_launch_snapshot("CREW FAILED the approach does not work")

        self.assertEqual(snapshot["ticket"], "01")
        self.assertIn("CREW FAILED", snapshot["detail"])
        self.assertIn("no launch record", snapshot["detail"])

    def test_an_escalation_without_a_launch_record_is_a_driver_error(self):
        snapshot = self.missing_launch_snapshot("CREW ASK 01 scope — which table? ts=1")

        self.assertEqual(snapshot["ticket"], "01")
        self.assertIn("CREW ASK", snapshot["detail"])
        self.assertIn("no launch record", snapshot["detail"])

    def test_no_row_of_the_rule_table_wakes_the_coordinator(self):
        """The wake surface, stated as what is not on it.

        One run carrying every settlement the table owns at once — a verified receipt, a receipt
        re-asked and settled failed, a nudged child that went silent, a vanished child, and a
        parked one — ends without a single judgment wake, and the four tickets it stopped on are
        settled in the log rather than handed up.
        """
        process = self.start(("01", ()), ("02", ()), ("03", ()), ("04", ()), ("05", ()))

        self.fixture.completes("01")
        self.fixture.says("02", "CREW COMPLETE " + "0" * 40)
        self.wait_for_instruction("02", "CREW RECHECK")
        self.fixture.says("02", "CREW COMPLETE " + "1" * 40)
        self.fixture.goes("03", "idle")
        self.wait_for_instruction("03", "CREW NUDGE")
        self.fixture.vanishes("04")
        self.fixture.says("05", "CREW PARKED features/demo/checklist.md")
        self.woken(process, "run-complete")

        self.assertEqual(
            [self.verdict(number) for number in ("01", "02", "03", "04", "05")],
            ["completed", "failed", "failed", "failed", "parked"],
        )

    # --- the receipt rungs ----------------------------------------------------------------

    def test_a_receipt_bundled_under_a_summary_is_read_as_the_childs_final_word(self):
        """A child composes its last turn freely, and the receipt arrives under the summary."""
        process = self.start(("01", ()))

        sha = self.fixture.commit_work("01")
        self.fixture.says(
            "01",
            "Implemented the change, the tests pass and the review came back clean.\n"
            f"CREW COMPLETE {sha} ts=1",
        )
        self.woken(process, "run-complete")

        self.assertEqual(self.verdict("01"), "completed")
        self.assertEqual(self.instructions("01", "CREW RECHECK"), [])

    def test_an_ask_bundled_under_a_summary_reaches_the_coordinator_as_an_escalation(self):
        """The same strictness that ate receipts ate asks, which is a child invisibly stuck."""
        process = self.start(("01", ()))

        self.fixture.says(
            "01",
            "I read the spec and the ticket through and they disagree about the table.\n"
            "CREW ASK 01 doc-conflict — which table? ts=1",
        )
        snapshot = self.woken(process, "judgment-needed")

        self.assertEqual(snapshot["ticket"], "01")
        self.assertIn("which table?", snapshot["detail"])
        self.assertEqual(len(self.events("escalation", ticket="01")), 1)

    def test_an_invalid_receipt_is_re_asked_once_and_the_valid_one_after_it_settles(self):
        process = self.start(("01", ()))

        self.fixture.says("01", "CREW COMPLETE " + "0" * 40)
        instruction = self.wait_for_instruction("01", "CREW RECHECK")
        self.fixture.completes("01")
        self.woken(process, "run-complete")

        self.assertIn("01", instruction)
        self.assertIn("CREW COMPLETE", instruction)
        self.assertEqual(len(self.instructions("01", "CREW RECHECK")), 1)
        self.assertEqual(self.verdict("01"), "completed")

    def test_a_second_invalid_receipt_settles_the_ticket_failed(self):
        process = self.start(("01", ()))

        self.fixture.says("01", "CREW COMPLETE " + "0" * 40)
        self.wait_for_instruction("01", "CREW RECHECK")
        self.fixture.says("01", "CREW COMPLETE " + "1" * 40)
        self.wait_for_verdict("01", "failed")
        self.woken(process, "run-complete")

        self.assertEqual(len(self.instructions("01", "CREW RECHECK")), 1)

    def test_a_malformed_receipt_is_bounced_once_and_the_bare_resend_settles(self):
        """#105, verbatim: prose on the verb line left a finished ticket reading `waiting`."""
        process = self.start(("01", ()))

        # The incident's message, with the one substitution this fixture forces: the sha has to be
        # a commit the run can verify, so the resend settles the way the real one did.
        sha = self.fixture.commit_work("01")
        incident = (
            f"CREW COMPLETE {sha} — deferred gap carried forward: the parked checklist is"
            " unwritten ts=1755594000"
        )
        self.fixture.says("01", incident)
        instruction = self.wait_for_instruction("01", "CREW RESEND")
        self.fixture.says("01", f"CREW COMPLETE {sha} ts=1755594600")
        self.woken(process, "run-complete")

        self.assertIn("01", instruction)
        self.assertIn(incident, instruction)
        self.assertEqual(len(self.instructions("01", "CREW RESEND")), 1)
        self.assertEqual(self.verdict("01"), "completed")

    def test_a_second_malformed_receipt_settles_the_ticket_failed(self):
        process = self.start(("01", ()))

        self.fixture.says("01", "CREW COMPLETE not-a-sha")
        self.wait_for_instruction("01", "CREW RESEND")
        self.fixture.says("01", "CREW COMPLETE still-not-a-sha")
        self.wait_for_verdict("01", "failed")
        self.woken(process, "run-complete")

        self.assertEqual(len(self.instructions("01", "CREW RESEND")), 1)
        failed = self.events("receipt", ticket="01", verdict="failed")
        self.assertIn("still-not-a-sha", failed[-1]["detail"])

    def test_a_bounced_child_that_then_goes_idle_settles_failed_without_a_second_re_ask(self):
        """The bounce is the ticket's one re-ask; a nudge after it would ask twice."""
        process = self.start(("01", ()))

        self.fixture.says("01", "CREW COMPLETE not-a-sha")
        self.wait_for_instruction("01", "CREW RESEND")
        self.fixture.goes("01", "idle")
        self.wait_for_verdict("01", "failed")
        self.woken(process, "run-complete")

        self.assertEqual(self.instructions("01", "CREW NUDGE"), [])
        failed = self.events("receipt", ticket="01", verdict="failed")
        self.assertIn("never resent", failed[-1]["detail"])

    def test_a_malformed_ask_is_bounced_with_the_shape_an_ask_takes(self):
        """A near miss is not always a receipt: the answer names every verb's shape, not three."""
        process = self.start(("01", ()))

        self.fixture.says("01", "CREW ASK")
        instruction = self.wait_for_instruction("01", "CREW RESEND")
        self.fixture.completes("01")
        self.woken(process, "run-complete")

        self.assertIn("CREW ASK", instruction)
        self.assertEqual(self.events("escalation", ticket="01"), [])
        self.assertEqual(self.verdict("01"), "completed")

    def test_a_message_that_speaks_no_verb_at_all_is_not_bounced(self):
        """Conversation is still conversation; only a near miss is answered."""
        process = self.start(("01", ()))

        self.fixture.says("01", "The tests are green; the review is still running.")
        self.fixture.completes("01")
        self.woken(process, "run-complete")

        self.assertEqual(self.instructions("01", "CREW RESEND"), [])
        self.assertEqual(self.verdict("01"), "completed")

    def test_a_parked_receipt_is_recorded_by_the_driver_and_its_worktree_listed(self):
        process = self.start(("01", ()))

        self.fixture.says("01", "CREW PARKED features/demo/checklist.md")
        self.wait_for_verdict("01", "parked")
        self.woken(process, "run-complete")

        parked = (self.fixture.run_dir / "parked-paths").read_text()
        self.assertIn(str(self.fixture.worktree("01")), parked)
        self.assertIn("checklist.md", self.events("receipt", ticket="01")[-1]["detail"])

    def test_a_parked_ticket_that_finishes_after_all_supersedes_its_parked_receipt(self):
        """A park waits on a human, and a child that finished has left that human nothing to do."""
        process = self.start(("01", ()), ("02", ()))

        self.fixture.says("01", "CREW PARKED features/demo/checklist.md")
        self.wait_for_verdict("01", "parked")
        self.fixture.completes("01")
        self.fixture.completes("02")
        self.woken(process, "run-complete")

        self.assertEqual(
            [record["verdict"] for record in self.events("receipt", ticket="01")],
            ["parked", "landable"],
        )
        self.assertEqual(self.verdict("01"), "completed")

    def test_a_parked_ticket_whose_late_receipt_does_not_verify_stays_parked(self):
        """The late receipt takes the normal verify path, which a bad claim does not survive.

        02 is in the wave to keep it open while 01's claim is ruled on; the run is left running,
        because what is pinned here is what the failed verify did to the parked receipt and not
        how the wave ends afterwards.
        """
        self.start(("01", ()), ("02", ()))

        self.fixture.says("01", "CREW PARKED features/demo/checklist.md")
        self.wait_for_verdict("01", "parked")
        self.fixture.says("01", "CREW COMPLETE " + "0" * 40)
        self.wait_for_instruction("01", "CREW RECHECK")

        self.assertEqual(self.verdict("01"), "parked")
        self.assertEqual(self.events("receipt", ticket="01", verdict="landable"), [])

    def test_a_parked_ticket_with_descendants_blocks_them_and_ends_the_run(self):
        """The parked verdict and the blocked descendants are both rules; neither is judgment."""
        process = self.start(("01", ()), ("02", ("01",)))

        self.fixture.says("01", "CREW PARKED features/demo/checklist.md")
        self.woken(process, "run-complete")

        self.assertEqual(self.verdict("02"), "blocked")
        self.assertIsNone(self.fixture.launch_record("02"))

    def test_a_chain_that_stopped_for_good_records_that_the_run_stopped(self):
        """The ending the `escalated` decision cannot describe on its own.

        A wave that escalated is halted awaiting a ruling — until the reasons are all ones the
        rule table has already settled, and then the same word means the run has ended. The driver
        appends `stopped` at that point so the operator's surfaces can tell the two apart: nothing
        else in the log distinguishes a run that is over from one waiting on the coordinator.
        """
        process = self.start(("01", ()))

        self.fixture.says("01", "CREW FAILED the approach does not work")
        self.woken(process, "run-complete")

        self.assertEqual(
            [record["decision"] for record in self.events("advance")], ["escalated", "stopped"]
        )
        self.assertEqual(self.events("advance", decision="stopped")[0]["wave"], "1")

    def test_stopped_refuses_an_unlaunched_ticket_the_halt_did_not_block(self):
        process = self.start(("01", ()), ("02", ("01",)))
        process.kill()
        process.communicate()
        table_path = self.fixture.run_dir / "wave-table.json"
        table = json.loads(table_path.read_text())
        table["waves"][1]["tickets"][0]["blocked_by"] = []
        table_path.write_text(json.dumps(table))

        resumed = self.fixture.resume()
        self.assertIn("resumed", resumed.stdout.readline())
        self.fixture.says("01", "CREW FAILED the approach does not work")
        snapshot = self.woken(resumed, "driver-error")

        self.assertIn("stopped refused: ticket 02 is still launchable", snapshot["detail"])
        self.assertEqual(self.events("advance", decision="stopped"), [])

    def test_a_failure_receipt_is_recorded_by_the_driver(self):
        process = self.start(("01", ()))

        self.fixture.says("01", "CREW FAILED the approach does not work")
        self.wait_for_verdict("01", "failed")
        self.woken(process, "run-complete")

        self.assertIn(
            "does not work", self.events("receipt", ticket="01")[-1]["detail"]
        )

    # --- the wake monitor's rungs -----------------------------------------------------------

    def test_an_idle_child_with_no_receipt_is_nudged_once_and_settles_on_the_receipt(self):
        process = self.start(("01", ()))

        self.fixture.goes("01", "idle")
        instruction = self.wait_for_instruction("01", "CREW NUDGE")
        self.fixture.goes("01", "busy")
        self.fixture.completes("01")
        self.woken(process, "run-complete")

        self.assertIn("CREW COMPLETE", instruction)
        self.assertEqual(len(self.instructions("01", "CREW NUDGE")), 1)

    def test_a_child_awaiting_a_handed_over_ruling_is_not_nudged_until_it_is_answered(self):
        """A nudge to a child waiting on an answer races an answered ask into a parked receipt.

        The child asked, the run handed the ask up, and the answer travels by a channel that
        reaches no log — so the one thing the loop knows is that the child is owed a reply. Idle
        is what waiting for one looks like, and nudging it there asks a child that has nothing to
        report to report something. What it honestly reports is `CREW PARKED`, which settles a
        ticket whose question had already been answered.
        """
        process = self.start(("01", ()))

        self.fixture.says("01", "CREW ASK 01 scope — which table? ts=1")
        self.woken(process, "judgment-needed")
        self.fixture.goes("01", "idle")
        resumed = self.fixture.resume()
        self.assertIn("resumed", resumed.stdout.readline())
        time.sleep(QUIET_SECONDS)

        self.assertEqual(self.instructions("01", "CREW NUDGE"), [])

        # The coordinator answers, and the run is owed nothing: an idle child is one the ordinary
        # rung nudges again.
        self.fixture.answers("01", "Use the existing retention_audit table")
        self.wait_for_instruction("01", "CREW NUDGE")
        self.fixture.goes("01", "busy")
        self.fixture.completes("01")
        self.woken(resumed, "run-complete")

        self.assertEqual(len(self.instructions("01", "CREW NUDGE")), 1)
        self.assertEqual(self.verdict("01"), "completed")

    def test_a_second_idle_silence_settles_the_ticket_failed(self):
        process = self.start(("01", ()))

        self.fixture.goes("01", "idle")
        self.wait_for_instruction("01", "CREW NUDGE")
        self.wait_for_verdict("01", "failed")
        self.woken(process, "run-complete")

        self.assertEqual(len(self.instructions("01", "CREW NUDGE")), 1)

    def test_a_vanished_child_settles_the_ticket_failed(self):
        process = self.start(("01", ()))

        self.fixture.vanishes("01")
        self.wait_for_verdict("01", "failed")
        self.woken(process, "run-complete")

        self.assertIn("vanished", self.events("receipt", ticket="01")[-1]["detail"])
        self.assertEqual(self.events("ruling", ticket="01"), [])

    def test_a_child_waiting_at_a_permission_prompt_wakes_the_coordinator(self):
        process = self.start(("01", ()))

        self.fixture.goes("01", "waiting")
        snapshot = self.woken(process, "judgment-needed")

        self.assertEqual(snapshot["ticket"], "01")
        self.assertIn("waiting", snapshot["detail"])

    def test_a_run_that_does_nothing_at_all_wakes_the_coordinator_rather_than_hanging(self):
        self.feature(("01", ()))

        process = self.fixture.launch(extra=("--timeout", "3"))
        result = self.fixture.ended(process)

        snapshot = json.loads(
            [line for line in result.stdout.splitlines() if line.strip()][-1]
        )
        self.assertEqual(snapshot["reason"], "driver-error")
        self.assertIn("nothing", snapshot["detail"])
        self.assertIn("resume", snapshot, "a stopped run's snapshot did not say how it goes on")

    def test_the_command_a_driver_error_names_puts_the_same_run_back_on_its_feet(self):
        """A driver error is recovered exactly as a ruling is: by the command the snapshot names."""
        self.feature(("01", ()))
        stopped = self.fixture.ended(self.fixture.launch(extra=("--timeout", "3")))
        snapshot = json.loads(
            [line for line in stopped.stdout.splitlines() if line.strip()][-1]
        )

        resumed = self.fixture.run_command(snapshot["resume"])
        self.assertTrue(
            self.fixture.wait_for(lambda: self.fixture.launch_record("01") is not None),
            "the resumed run lost the wave it was carrying on",
        )
        self.fixture.completes("01")
        self.woken(resumed, "run-complete")

        self.assertEqual(self.verdict("01"), "completed")

    # --- the escalation, which is never anything but a wake ---------------------------------

    def test_a_crew_ask_wakes_the_coordinator_carrying_the_ticket_and_the_ask_itself(self):
        process = self.start(("01", ()))

        self.fixture.says("01", "CREW ASK 01 scope — which table? ts=1")
        snapshot = self.woken(process, "judgment-needed")

        self.assertEqual(snapshot["ticket"], "01")
        self.assertIn("which table?", snapshot["detail"])
        self.assertIn("resume", snapshot)
        self.assertIsNone(self.verdict("01"), "an answered ASK is not an outcome")

    def test_a_second_escalation_after_a_ruling_wakes_the_coordinator_again(self):
        """A resume steps past the ASK it was run for, and past nothing else that ticket says."""
        process = self.start(("01", ()))

        self.fixture.says("01", "CREW ASK 01 scope — which table? ts=1")
        self.woken(process, "judgment-needed")
        resumed = self.fixture.resume()
        self.assertIn("resumed", resumed.stdout.readline())
        self.fixture.says("01", "CREW ASK 01 scope — and which column? ts=2")
        snapshot = self.woken(resumed, "judgment-needed")

        self.assertEqual(snapshot["ticket"], "01")
        self.assertIn("which column?", snapshot["detail"])

    def test_an_escalation_the_coordinator_was_never_shown_wakes_it_on_the_resume(self):
        """Two children asking at once is one snapshot, so the second ASK is the next wake.

        The escalation the driver exits on is the only one it can say was handed over; an ASK that
        was standing at the same moment and never reached a snapshot is unread, and settling it
        because the run came back would lose a child's question in silence.
        """
        process = self.start(("01", ()), ("02", ()))

        self.fixture.says("01", "CREW ASK 01 scope — which table? ts=1")
        self.fixture.says("02", "CREW ASK 02 scope — which column? ts=1")
        first = self.woken(process, "judgment-needed")
        second = self.woken(self.fixture.resume(), "judgment-needed")

        self.assertEqual(first["ticket"], "01")
        self.assertEqual(second["ticket"], "02")
        self.assertIn("which column?", second["detail"])

    def test_resuming_after_a_ruling_carries_the_same_wave_on(self):
        process = self.start(("01", ()), ("02", ()))

        self.fixture.says("01", "CREW ASK 01 scope — which table? ts=1")
        self.woken(process, "judgment-needed")
        dispatched = len(self.fixture.launches())
        resumed = self.fixture.resume()
        self.assertIn("resumed", resumed.stdout.readline())
        self.fixture.completes("01")
        self.fixture.completes("02")
        self.woken(resumed, "run-complete")

        self.assertEqual(
            len(self.fixture.launches()), dispatched, "a resumed run re-dispatched a live ticket"
        )
        self.assertEqual([self.verdict("01"), self.verdict("02")], ["completed", "completed"])

    # --- the merge ladder --------------------------------------------------------------------

    def test_a_semantic_conflict_is_answered_by_the_template_before_any_coordinator_turn(self):
        process = self.start(("01", ()), ("02", ()), shared="one\n")

        self.fixture.completes("01", "01 rewrote\n", name="shared.txt")
        self.fixture.completes("02", "02 rewrote\n", name="shared.txt")
        instruction = self.wait_for_instruction("02", "CREW MERGE")

        self.assertIn(INTEGRATION_BRANCH, instruction)
        self.assertIn("CREW COMPLETE", instruction)
        self.assertIn(
            "semantic", self.events("merge", ticket="02", result="conflict")[-1]["detail"]
        )

    def test_the_merge_rework_instruction_scopes_re_verification_to_the_conflict(self):
        """The rework instruction has never fired in a run, so its text is pinned before it does.

        The words are the ticket's: merge the integration branch, resolve, re-run the tests the
        conflict touched, re-review scoped to the conflict-resolution diff, commit, send a new
        receipt. The full-workflow re-run language it replaces is asserted gone.
        """
        process = self.start(("01", ()), ("02", ()), shared="one\n")

        self.fixture.completes("01", "01 rewrote\n", name="shared.txt")
        self.fixture.completes("02", "02 rewrote\n", name="shared.txt")
        instruction = self.wait_for_instruction("02", "CREW MERGE")

        self.assertIn("re-run the tests the conflict touched", instruction)
        self.assertIn("re-review scoped to the conflict-resolution diff", instruction)
        self.assertNotIn("the checks your workflow asked of you", instruction)

    def test_a_semantic_conflict_bounced_back_a_second_time_wakes_the_coordinator(self):
        process = self.start(("01", ()), ("02", ()), shared="one\n")

        self.fixture.completes("01", "01 rewrote\n", name="shared.txt")
        self.fixture.completes("02", "02 rewrote\n", name="shared.txt")
        self.wait_for_instruction("02", "CREW MERGE")
        self.fixture.completes("02", "02 rewrote again\n", name="shared.txt")
        snapshot = self.woken(process, "judgment-needed")

        self.assertEqual(snapshot["ticket"], "02")
        self.assertIn("second time", snapshot["detail"])
        self.assertEqual(len(self.instructions("02", "CREW MERGE")), 1)

    def test_a_mechanical_conflict_is_resolved_by_the_driver_without_a_repair_session(self):
        """Both children only inserted, so the run lands the wave itself and ends on its own."""
        process = self.start(("01", ()), ("02", ()), shared="one\n")

        self.fixture.completes("01", "one\nfrom 01\n", name="shared.txt")
        self.fixture.completes("02", "one\nfrom 02\n", name="shared.txt")
        self.woken(process, "run-complete")

        self.assertEqual(
            [call for call in self.fixture.claude_calls() if "--print" in call["argv"]], [],
            "a proven-mechanical conflict wakes neither the repair rung nor the coordinator",
        )
        self.assertIn(
            "mechanical", self.events("merge", ticket="02", result="conflict")[-1]["detail"]
        )
        self.assertEqual(len(self.events("merge", ticket="02", result="resolved")), 1)
        self.assertEqual(self.instructions("02", "CREW MERGE"), [])

    def test_the_conflict_the_driver_will_not_rewrite_reaches_the_configured_repair_model(self):
        """A file whose own text reads as an opening conflict marker is still the rung's work."""
        process = self.start(("01", ()), ("02", ()), shared=UNREWRITABLE)

        self.fixture.completes("01", UNREWRITABLE + "from 01\n", name="shared.txt")
        self.fixture.completes("02", UNREWRITABLE + "from 02\n", name="shared.txt")
        self.woken(process, "judgment-needed")

        repairs = [
            call for call in self.fixture.claude_calls()
            if "--print" in call["argv"] and REPAIR_MODEL in call["argv"]
        ]
        self.assertTrue(
            repairs, f"the repair rung was never reached: {self.fixture.claude_calls()}"
        )
        self.assertIn(
            "mechanical", self.events("merge", ticket="02", result="conflict")[-1]["detail"]
        )
        self.assertEqual(self.instructions("02", "CREW MERGE"), [])

    # --- the run-end epilogue ------------------------------------------------------------------

    def artefacts(self, ticket):
        """The worktree, branch and window the run recorded for that ticket's child."""
        record = self.fixture.launch_record(ticket)
        return pathlib.Path(record["worktree"]), record["branch"], record["window"]

    def dashboard_window(self):
        """The window id the dashboard recorded, once the surface has drawn itself."""
        path = self.fixture.run_dir / "dashboard-window"
        self.assertTrue(
            self.fixture.wait_for(lambda: path.exists() and path.read_text().strip()),
            "the dashboard never recorded its window",
        )
        return path.read_text().strip()

    def test_the_epilogue_clears_landed_work_and_leaves_parked_work_standing(self):
        process = self.start(("01", ()), ("02", ()))
        landed_worktree, landed_branch, landed_window = self.artefacts("01")
        parked_worktree, parked_branch, parked_window = self.artefacts("02")
        coordinator_window = self.fixture.add_window("@coordinator", "coordinator")
        dashboard_window = self.dashboard_window()

        self.fixture.completes("01")
        self.fixture.says("02", "CREW PARKED features/demo/checklist-02.md")
        snapshot = self.woken(process, "run-complete")

        self.assertIsNone(snapshot["cleanup"], "a cleared site reported a cleanup failure")
        self.assertFalse(landed_worktree.exists(), "a landed worktree survived the epilogue")
        self.assertNotIn(landed_branch, self.fixture.branches())
        self.assertTrue(parked_worktree.exists(), "a parked worktree was destroyed")
        self.assertIn(parked_branch, self.fixture.branches())
        self.assertIn(INTEGRATION_BRANCH, self.fixture.branches())

        windows = self.fixture.windows()
        self.assertNotIn(landed_window, windows)
        self.assertNotIn(dashboard_window, windows)
        self.assertIn(parked_window, windows)
        self.assertIn(coordinator_window, windows)

    def test_the_report_lists_the_preserved_paths_of_parked_and_failed_tickets(self):
        process = self.start(
            ("01", ()), ("02", ()), ("03", ()),
            env_overrides={"CLAUDE_CODE_SESSION_ID": ""},
        )
        landed_worktree, _, _ = self.artefacts("01")
        parked_worktree, parked_branch, _ = self.artefacts("02")
        failed_worktree, failed_branch, _ = self.artefacts("03")

        self.fixture.completes("01")
        self.fixture.says("02", "CREW PARKED features/demo/checklist-02.md")
        self.fixture.says("03", "CREW COMPLETE " + "0" * 40)
        self.wait_for_instruction("03", "CREW RECHECK")
        self.fixture.says("03", "CREW COMPLETE " + "1" * 40)
        self.woken(process, "run-complete")

        self.assertTrue(failed_worktree.exists(), "a failed worktree was destroyed")
        self.assertIn(failed_branch, self.fixture.branches())
        # The cost rollup names every child's worktree, landed ones included, so what the report
        # preserves is read from the report the run itself writes, ahead of that appended block.
        report = (self.fixture.feature_dir / "report.md").read_text().split("## Cost", 1)[0]
        for value in (str(parked_worktree), parked_branch, str(failed_worktree), failed_branch):
            self.assertIn(value, report)
        self.assertNotIn(str(landed_worktree), report)

    def test_the_epilogue_stops_only_a_landed_ticket_s_codex_session(self):
        process = self.start(("01", ()), ("02", ()), routing=CODEX_ROUTING)
        landed_state = self.fixture.run_dir / "codex" / "01.json"
        parked_state = self.fixture.run_dir / "codex" / "02.json"

        self.fixture.completes("01")
        self.fixture.says("02", "CREW PARKED features/demo/checklist-02.md")
        self.woken(process, "run-complete")

        stopped = [
            call["argv"] for call in self.fixture.codex_calls() if call["argv"][:1] == ["stop"]
        ]
        self.assertEqual(len(stopped), 1, stopped)
        self.assertIn(str(landed_state), stopped[0])
        self.assertTrue(parked_state.exists(), "a parked ticket's Codex state was removed")

    def test_an_epilogue_that_could_not_clear_says_so_in_the_run_complete_snapshot(self):
        process = self.start(("01", ()), routing=CODEX_ROUTING)
        (self.fixture.stub_dir / "codex-stop-fails").write_text("yes\n")

        self.fixture.completes("01")
        snapshot = self.woken(process, "run-complete")

        self.assertIn(str(self.fixture.run_dir / "codex" / "01.json"), snapshot["cleanup"])
        self.assertEqual(snapshot["report"], str(self.fixture.feature_dir / REPORT_NAME))

    # --- closing what landed -----------------------------------------------------------------

    def test_a_merged_ticket_is_closed_in_the_tracker_with_its_undo_recorded(self):
        process = self.start(("01", ()))

        self.fixture.completes("01")
        self.woken(process, "run-complete")

        closed = self.events("outcome", ticket="01", outcome="completed")
        self.assertEqual(len(closed), 1, self.fixture.log_records())
        self.assertIn(TRACKER, closed[0]["detail"])
        self.assertIn("undo:", closed[0]["detail"])
        self.assertIn("Status: done", (self.fixture.feature_dir / "01.md").read_text())

    def test_a_local_close_leaves_the_working_tree_clean_for_the_next_wave(self):
        """A close is a write inside the repo, and an uncommitted one stops the merge after it."""
        process = self.start(("01", ()), ("02", ("01",)))

        self.fixture.completes("01")
        self.assertTrue(
            self.fixture.wait_for(lambda: self.fixture.launch_record("02") is not None),
            "the run never advanced past the wave it closed a ticket in",
        )
        self.fixture.completes("02")
        self.woken(process, "run-complete")

        # Untracked paths are the run's own directory and the guard assets, which the run's own
        # clean-tree rule allows; what a merge refuses is a tracked file left uncommitted.
        left = git(self.fixture.repo, "status", "--porcelain", "--untracked-files=no").stdout
        self.assertEqual(left.strip(), "")

    def test_a_github_run_closes_the_issue_and_records_reopening_it_as_the_undo(self):
        self.fixture.configure(tracker="github")
        self.fixture.issues({"01": {"labels": ["ready-for-agent", "area/driver"], "closed": False}})
        process = self.start(("01", ()))

        self.fixture.completes("01")
        self.woken(process, "run-complete")

        issue = self.fixture.issues()["01"]
        self.assertTrue(issue["closed"])
        self.assertEqual(issue["labels"], ["area/driver"], "a label that is not a pickup was taken")
        detail = self.events("outcome", ticket="01", outcome="completed")[0]["detail"]
        self.assertIn("github", detail)
        self.assertIn("gh issue reopen 01", detail)
        self.assertIn("--add-label ready-for-agent", detail)

    def test_a_github_close_names_no_repository_and_is_made_in_the_run_s_own_checkout(self):
        """`gh` takes an OWNER/REPO slug, never a path: the checkout it runs in is what names it."""
        self.fixture.configure(tracker="github")
        process = self.start(("01", ()))

        self.fixture.completes("01")
        self.woken(process, "run-complete")

        calls = self.fixture.gh_calls()
        self.assertTrue(calls, "the tracker was never reached")
        for call in calls:
            self.assertNotIn("--repo", call["argv"], f"gh was handed a repository: {call['argv']}")
            self.assertEqual(os.path.realpath(call["cwd"]), os.path.realpath(self.fixture.repo))


class AnswerTests(DriverTestCase):
    def start(self, routing=ROUTING):
        self.fixture.ticket("01", "first thing", routing=routing)
        self.fixture.commit_feature()
        process = self.fixture.launch()
        self.assertTrue(
            self.fixture.wait_for(lambda: self.fixture.launch_record("01") is not None),
            "01 never launched",
        )
        return process

    def answer(self, *arguments):
        return subprocess.run(
            [
                sys.executable, str(DRIVER), "answer",
                "--run-dir", str(self.fixture.run_dir), "--ticket", "01", *arguments,
            ],
            capture_output=True, text=True,
            env=self.fixture.environment(), cwd=str(self.fixture.repo),
        )

    def kill_window(self, window):
        subprocess.run(
            [str(self.fixture.bin_dir / "tmux"), "kill-window", "-t", window],
            check=True, capture_output=True, env=self.fixture.environment(),
        )

    def test_text_answer_sends_literal_text_then_enter_and_records_the_ruling(self):
        self.start()
        text = "Use the existing retention_audit table"
        window = self.fixture.launch_record("01")["window"]

        result = self.answer("--text", text)

        self.assertEqual(result.returncode, 0, result.stderr)
        sent = [
            call["argv"] for call in self.fixture.tmux_calls()
            if call["argv"][:1] == ["send-keys"]
        ]
        self.assertEqual(sent[-2:], [
            ["send-keys", "-t", window, "-l", "--", text],
            ["send-keys", "-t", window, "Enter"],
        ])
        ruling = self.events("ruling", ticket="01")[-1]
        self.assertEqual(ruling["role"], "coordinator")
        self.assertEqual(ruling["to"], "stub-child-1")
        self.assertEqual(ruling["message"], text)

    def test_key_answer_sends_named_keys_without_literal_mode_and_records_them(self):
        self.start()
        window = self.fixture.launch_record("01")["window"]

        result = self.answer("--key", "Down", "--key", "Enter")

        self.assertEqual(result.returncode, 0, result.stderr)
        sent = [
            call["argv"] for call in self.fixture.tmux_calls()
            if call["argv"][:1] == ["send-keys"]
        ]
        self.assertEqual(sent[-2:], [
            ["send-keys", "-t", window, "Down"],
            ["send-keys", "-t", window, "Enter"],
        ])
        ruling = self.events("ruling", ticket="01")[-1]
        self.assertEqual(ruling["message"], "Down Enter")

    def test_digit_answer_sends_the_digit_and_records_it(self):
        self.start()
        window = self.fixture.launch_record("01")["window"]

        result = self.answer("--key", "4")

        self.assertEqual(result.returncode, 0, result.stderr)
        sent = [
            call["argv"] for call in self.fixture.tmux_calls()
            if call["argv"][:1] == ["send-keys"]
        ]
        self.assertEqual(sent[-1], ["send-keys", "-t", window, "4"])
        self.assertEqual(self.events("ruling", ticket="01")[-1]["message"], "4")

    def test_delivery_failure_reports_unreachable_and_writes_no_ruling(self):
        self.start()
        window = self.fixture.launch_record("01")["window"]
        self.kill_window(window)

        result = self.answer("--text", "Use the existing retention_audit table")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not be reached", result.stdout)
        self.assertEqual(self.events("ruling", ticket="01"), [])

    def test_missing_recorded_window_reports_unreachable_and_writes_no_ruling(self):
        self.start()
        records = self.fixture.log_records()
        launch = next(
            record
            for record in reversed(records)
            if record.get("event") == "launch" and record.get("ticket") == "01"
        )
        launch["window"] = None
        (self.fixture.run_dir / "log.jsonl").write_text(
            "\n".join(json.dumps(record) for record in records) + "\n"
        )

        result = self.answer("--text", "Use the existing retention_audit table")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no window", result.stdout)
        self.assertEqual(self.events("ruling", ticket="01"), [])

    def test_unsupported_key_is_rejected_without_delivery_or_ruling(self):
        self.start()
        before = len([
            call for call in self.fixture.tmux_calls() if call["argv"][:1] == ["send-keys"]
        ])

        result = self.answer("--key", "PageDown")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsupported answer key", result.stdout)
        after = len([
            call for call in self.fixture.tmux_calls() if call["argv"][:1] == ["send-keys"]
        ])
        self.assertEqual(after, before)
        self.assertEqual(self.events("ruling", ticket="01"), [])

    def test_text_answer_can_select_a_free_text_option_before_typing(self):
        self.start()
        text = "Use the existing retention_audit table"
        window = self.fixture.launch_record("01")["window"]

        result = self.answer("--key", "4", "--text", text)

        self.assertEqual(result.returncode, 0, result.stderr)
        sent = [
            call["argv"] for call in self.fixture.tmux_calls()
            if call["argv"][:1] == ["send-keys"]
        ]
        self.assertEqual(sent[-3:], [
            ["send-keys", "-t", window, "4"],
            ["send-keys", "-t", window, "-l", "--", text],
            ["send-keys", "-t", window, "Enter"],
        ])
        self.assertEqual(self.events("ruling", ticket="01")[-1]["message"], f"4 {text}")

    def test_dash_prefixed_text_is_sent_as_literal_text(self):
        self.start()
        text = "- Use the existing retention_audit table"
        window = self.fixture.launch_record("01")["window"]

        result = self.answer("--key", "4", "--text", text)

        self.assertEqual(result.returncode, 0, result.stderr)
        sent = [
            call["argv"] for call in self.fixture.tmux_calls()
            if call["argv"][:1] == ["send-keys"]
        ]
        self.assertEqual(sent[-3:], [
            ["send-keys", "-t", window, "4"],
            ["send-keys", "-t", window, "-l", "--", text],
            ["send-keys", "-t", window, "Enter"],
        ])
        self.assertEqual(self.events("ruling", ticket="01")[-1]["message"], f"4 {text}")

    def test_triage_routes_permission_answers_through_the_driver_without_double_send(self):
        triage = TRIAGE.read_text(encoding="utf-8")

        self.assertIn("driver.py answer", triage)
        self.assertNotIn("tmux send-keys", triage)
        self.assertNotIn("send it to that child as a message too", triage)
        self.assertIn("Reply to a Claude child by SendMessage", triage)

    def test_a_codex_child_is_answered_through_the_bridge_send_and_the_ruling_is_recorded(self):
        """A ruling for a Codex child rides the bridge, which is the channel it already has."""
        self.start(routing=CODEX_ROUTING)
        text = "Use the existing retention_audit table"
        before = [
            call["argv"] for call in self.fixture.tmux_calls() if call["argv"][:1] == ["send-keys"]
        ]

        result = self.answer("--text", text)

        self.assertEqual(result.returncode, 0, result.stderr)
        sends = [
            call["argv"] for call in self.fixture.codex_calls() if call["argv"][:1] == ["send"]
        ]
        self.assertEqual(sends, [[
            "send",
            "--state-file", str(self.fixture.run_dir / "codex" / "01.json"),
            "--machine-log", str(self.fixture.run_dir / "log.jsonl"),
            "--ticket", "01",
            "--prompt", text,
        ]])
        after = [
            call["argv"] for call in self.fixture.tmux_calls() if call["argv"][:1] == ["send-keys"]
        ]
        self.assertEqual(after, before, "a Codex child was answered by tmux keys")
        ruling = self.events("ruling", ticket="01")[-1]
        self.assertEqual(ruling["role"], "coordinator")
        self.assertEqual(ruling["message"], text)

    def test_a_key_answer_to_a_codex_child_is_refused_as_text_only(self):
        self.start(routing=CODEX_ROUTING)

        result = self.answer("--key", "4")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("take text answers only", result.stdout)
        self.assertEqual(
            [call for call in self.fixture.codex_calls() if call["argv"][:1] == ["send"]], []
        )
        self.assertEqual(self.events("ruling", ticket="01"), [])

    def test_triage_answers_a_codex_ask_through_the_driver_rather_than_the_bridge(self):
        triage = TRIAGE.read_text(encoding="utf-8")

        self.assertIn("driver.py answer", triage)
        self.assertNotIn("codex_bridge.py send", triage)

    def test_multiline_text_uses_literal_lines_and_shift_enter_before_submit(self):
        self.start()
        text = "line one\nline two"
        window = self.fixture.launch_record("01")["window"]

        result = self.answer("--key", "4", "--text", text)

        self.assertEqual(result.returncode, 0, result.stderr)
        sent = [
            call["argv"] for call in self.fixture.tmux_calls()
            if call["argv"][:1] == ["send-keys"]
        ]
        self.assertEqual(sent[-5:], [
            ["send-keys", "-t", window, "4"],
            ["send-keys", "-t", window, "-l", "--", "line one"],
            ["send-keys", "-t", window, "S-Enter"],
            ["send-keys", "-t", window, "-l", "--", "line two"],
            ["send-keys", "-t", window, "Enter"],
        ])


class AdoptionTests(DriverTestCase):
    """Starting and resuming as one action: the same command over a run already on the ground.

    Each scenario drives the driver's own `start` seam a second time over a run directory an
    earlier driver left behind — its children stubbed, its branches cut, its log intact — which is
    the whole of what re-typing the crew command after an interruption does.
    """

    def feature(self, *tickets, routing=ROUTING):
        for number, blockers in tickets:
            self.fixture.ticket(number, f"thing {number}", routing=routing, blocked_by=blockers)
        self.fixture.commit_feature()

    def running(self, *tickets, routing=ROUTING):
        """A run of those tickets with its first wave up and its loop running."""
        self.feature(*tickets, routing=routing)
        process = self.fixture.launch()
        for number, blockers in tickets:
            if blockers:
                continue
            self.assertTrue(
                self.fixture.wait_for(
                    lambda number=number: self.fixture.launch_record(number) is not None
                ),
                f"{number} never launched",
            )
        return process

    def interrupted(self, *tickets, routing=ROUTING):
        """The same run, handed back with the driver that started it killed where it stood.

        The kill is the interruption every one of these is about: a driver that died mid-wave, a
        coordinator restarted under it, an operator who stopped the run — from the run directory
        they are one state, which is a run whose log records no final advance decision.
        """
        self.crash(self.running(*tickets, routing=routing))

    def crash(self, process):
        process.kill()
        process.communicate()

    def append_advance(self, wave, decision):
        """The advance line the run's own driver wrote before it stopped, in the log's schema."""
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "event": "advance", "wave": wave, "decision": decision,
        }
        with (self.fixture.run_dir / "log.jsonl").open("a") as handle:
            handle.write(json.dumps(record) + "\n")

    def test_re_invoking_the_driver_over_an_unfinished_run_adopts_it(self):
        self.interrupted(("01", ()), ("02", ("01",)))
        run = self.fixture.table()["run"]

        adopted = self.fixture.launch()
        line = adopted.stdout.readline()
        self.fixture.completes("01")
        self.assertTrue(
            self.fixture.wait_for(lambda: self.fixture.launch_record("02") is not None),
            "the adopted run never advanced to wave 2",
        )
        self.fixture.completes("02")
        self.woken(adopted, "run-complete")

        self.assertIn(str(self.fixture.run_dir), line)
        # A start that began again would have cut its integration branch afresh and recorded the
        # commit it cut it from; the run the adoption carried on is the one already on the ground.
        self.assertEqual(self.fixture.table()["run"], run, "the adopted run was started afresh")
        self.assertEqual(len(self.events("launch", ticket="01")), 2, "01 was dispatched twice")
        self.assertEqual([self.verdict("01"), self.verdict("02")], ["completed", "completed"])

    def test_a_settled_ticket_is_not_dispatched_again_by_the_run_that_adopts_it(self):
        self.interrupted(("01", ()), ("02", ()))
        self.fixture.completes("01")

        adopted = self.fixture.launch()
        self.wait_for_verdict("01", "landable")
        self.fixture.completes("02")
        self.woken(adopted, "run-complete")

        self.assertEqual(len(self.events("launch", ticket="01")), 2, "01 was dispatched twice")
        self.assertEqual(len(self.events("launch", ticket="02")), 2, "02 was dispatched twice")
        self.assertEqual([self.verdict("01"), self.verdict("02")], ["completed", "completed"])

    def test_a_run_whose_wave_escalated_is_unfinished_and_is_adopted(self):
        """`escalated` is not final: the wave the coordinator ruled on is the run's to carry on.

        The predicate the driver asks here is the monitor's own, so this is also what keeps the
        operator's surfaces drawing such a run rather than freezing at its first escalation.
        """
        self.interrupted(("01", ()))
        self.append_advance("1", "escalated")

        adopted = self.fixture.launch()
        self.fixture.completes("01")
        self.woken(adopted, "run-complete")

        self.assertEqual(len(self.events("launch", ticket="01")), 2, "01 was dispatched twice")
        self.assertEqual(self.verdict("01"), "completed")
        self.assertEqual(
            [record["decision"] for record in self.events("advance")], ["escalated", "complete"]
        )

    def test_a_run_the_driver_stopped_is_not_adopted(self):
        """`stopped` is final too: the chain ended on reasons no ruling of the run can undo."""
        self.interrupted(("01", ()))
        self.append_advance("1", "escalated")
        self.append_advance("1", "stopped")
        launches = len(self.fixture.launches())

        result = self.fixture.start()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.snapshot(result)["reason"], "run-complete")
        self.assertEqual(len(self.fixture.launches()), launches, "a stopped run was re-dispatched")

    def test_a_run_whose_log_holds_a_final_advance_decision_is_not_adopted(self):
        finished = self.running(("01", ()))
        self.fixture.completes("01")
        self.woken(finished, "run-complete")
        self.assertEqual(
            [record["decision"] for record in self.events("advance")], ["complete"]
        )
        launches = len(self.fixture.launches())
        branches = self.fixture.branches()

        result = self.fixture.start()

        snapshot = self.snapshot(result)
        report = self.fixture.feature_dir / REPORT_NAME
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(snapshot["reason"], "run-complete")
        # The snapshot the run's own ending emitted, re-emitted: a coordinator woken by either
        # reads the one report the run wrote out of the same two fields.
        self.assertEqual(snapshot["pointer"], str(report))
        self.assertEqual(snapshot["report"], str(report))
        self.assertTrue(report.exists(), "the finished run was pointed at a report it never wrote")
        self.assertEqual(len(self.fixture.launches()), launches, "a finished run was re-dispatched")
        self.assertEqual(self.fixture.branches(), branches)

    def test_adoption_after_a_crash_mid_wave_settles_the_receipt_it_missed(self):
        """The receipt arrives with no driver alive to read it; the run that adopts settles it."""
        self.interrupted(("01", ()), ("02", ("01",)))
        self.fixture.completes("01")

        adopted = self.fixture.launch()
        self.assertTrue(
            self.fixture.wait_for(lambda: self.fixture.launch_record("02") is not None),
            "the adopted run never carried the wave on",
        )
        self.fixture.completes("02")
        self.woken(adopted, "run-complete")

        self.assertEqual([self.verdict("01"), self.verdict("02")], ["completed", "completed"])

    def test_a_child_that_vanished_while_nothing_watched_settles_by_the_loop_s_rule(self):
        self.interrupted(("01", ()))
        self.fixture.vanishes("01")

        adopted = self.fixture.launch()
        self.woken(adopted, "run-complete")

        # `run-complete` means every ticket is settled, so this assertion needs no clock.
        self.assertEqual(self.verdict("01"), "failed")
        self.assertIn("vanished", self.events("receipt", ticket="01")[-1]["detail"])
        self.assertEqual(self.events("ruling", ticket="01"), [])

    def test_the_adopted_run_arms_its_monitors_over_the_live_children_alone(self):
        """A settled ticket is watched by nothing: the lane a snapshot names is the live one."""
        running = self.running(("01", ()), ("02", ()), routing=CODEX_ROUTING)
        self.fixture.says("01", "CREW FAILED the approach does not work")
        self.wait_for_verdict("01", "failed")
        self.crash(running)
        already = len(self.fixture.codex_calls())

        adopted = self.fixture.launch()
        self.assertTrue(
            self.fixture.wait_for(
                lambda: any(
                    call["argv"][:1] == ["watch"]
                    for call in self.fixture.codex_calls()[already:]
                )
            ),
            "the adopted run armed no monitor over its live child",
        )
        watches = [
            call for call in self.fixture.codex_calls()[already:] if call["argv"][:1] == ["watch"]
        ]
        self.fixture.completes("02")
        self.woken(adopted, "run-complete")

        for watch in watches:
            self.assertIn(str(self.fixture.run_dir / "codex" / "02.json"), watch["argv"])
            self.assertNotIn(str(self.fixture.run_dir / "codex" / "01.json"), watch["argv"])

    def test_a_coordinator_that_restarted_re_anchors_the_run_and_its_live_children(self):
        """A restarted coordinator has a new pid, and the socket a child trusts is the old one."""
        self.interrupted(("01", ()), ("02", ("01",)))
        restarted = ("--coordinator-name", "crew-coordinator-2a", "--coordinator-pid", "2601")

        adopted = self.fixture.launch(extra=restarted)
        self.assertTrue(
            self.fixture.wait_for(lambda: self.events("ruling", ticket="01")),
            "the live child was never re-anchored",
        )
        anchor = self.events("ruling", ticket="01")
        self.fixture.completes("01")
        self.assertTrue(
            self.fixture.wait_for(lambda: self.fixture.launch_record("02") is not None),
            "the adopted run never advanced to wave 2",
        )
        self.fixture.completes("02")
        self.woken(adopted, "run-complete")

        self.assertEqual(
            self.fixture.table()["run"]["coordinator_name"], "crew-coordinator-2a"
        )
        self.assertEqual(len(anchor), 1, f"01 was not re-anchored once: {anchor}")
        self.assertIn("uds:/tmp/cc-socks/2601.sock", anchor[0]["message"])
        self.assertIn("crew-coordinator-2a", anchor[0]["message"])
        launched = [
            call for call in self.fixture.launches()
            if str(self.fixture.repo / ".claude" / "worktrees" / "02-02") in json.dumps(call)
        ]
        self.assertEqual(len(launched), 1, "02 was not launched once")
        self.assertIn("uds:/tmp/cc-socks/2601.sock", json.dumps(launched[0]))

    def test_a_run_adopted_by_the_coordinator_that_started_it_re_anchors_nobody(self):
        self.interrupted(("01", ()))

        adopted = self.fixture.launch()
        self.fixture.completes("01")
        self.woken(adopted, "run-complete")

        self.assertEqual(self.fixture.table()["run"]["coordinator_pid"], COORDINATOR_PID)
        self.assertEqual(self.events("ruling", ticket="01"), [])

    def test_a_codex_child_is_not_re_anchored_because_its_channel_is_a_file(self):
        self.interrupted(("01", ()), routing=CODEX_ROUTING)

        adopted = self.fixture.launch(
            extra=("--coordinator-name", "crew-coordinator-2a", "--coordinator-pid", "2601")
        )
        self.assertTrue(
            self.fixture.wait_for(
                lambda: self.fixture.table()["run"]["coordinator_pid"] == 2601
            ),
            "the adopted run kept the coordinator that started it",
        )
        self.fixture.completes("01")
        self.woken(adopted, "run-complete")

        self.assertEqual(self.events("ruling", ticket="01"), [])
        self.assertEqual(
            [call for call in self.fixture.codex_calls() if call["argv"][:1] == ["send"]], []
        )

    def test_a_child_that_cannot_be_reached_is_left_to_the_rule_that_settles_it(self):
        """A window that went with its session is not a driver error: it is a vanished child."""
        self.interrupted(("01", ()), ("02", ()))
        self.kill_window(self.fixture.launch_record("01")["window"])
        self.fixture.vanishes("01")

        adopted = self.fixture.launch(
            extra=("--coordinator-name", "crew-coordinator-2a", "--coordinator-pid", "2601")
        )
        self.assertTrue(
            self.fixture.wait_for(lambda: self.events("ruling", ticket="02")),
            "the adopted run never re-anchored its live child",
        )
        self.fixture.completes("02")
        self.woken(adopted, "run-complete")

        # `run-complete` means every ticket is settled, so this assertion needs no clock.
        self.assertEqual(self.verdict("01"), "failed")
        self.assertEqual(self.events("ruling", ticket="01"), [])
        anchor = self.events("ruling", ticket="02")
        self.assertEqual(len(anchor), 1, f"02 was not re-anchored once: {anchor}")
        self.assertIn("uds:/tmp/cc-socks/2601.sock", anchor[0]["message"])

    def kill_window(self, window):
        subprocess.run(
            [str(self.fixture.bin_dir / "tmux"), "kill-window", "-t", window],
            check=True, capture_output=True, env=self.fixture.environment(),
        )

    def test_the_adopted_run_draws_the_dashboard_the_interrupted_one_left(self):
        """The window an interrupted run recorded goes with it; the run that adopts has one."""
        self.interrupted(("01", ()))
        for window in self.fixture.windows_named(DASHBOARD_WINDOW):
            self.kill_window(window)

        adopted = self.fixture.launch()
        record = self.fixture.run_dir / "dashboard-window"
        self.assertTrue(
            self.fixture.wait_for(
                lambda: self.fixture.windows_named(DASHBOARD_WINDOW)
                and record.exists() and record.read_text().strip()
            ),
            "the adopted run drew no dashboard",
        )

        windows = self.fixture.windows_named(DASHBOARD_WINDOW)
        self.assertEqual(len(windows), 1, f"dashboard windows: {windows}")
        self.assertEqual(record.read_text().strip(), next(iter(windows)))

        self.fixture.completes("01")
        self.woken(adopted, "run-complete")

        # The window is the run's, not the operator's: the epilogue closes it with the run.
        self.assertEqual(self.fixture.windows_named(DASHBOARD_WINDOW), {})


if __name__ == "__main__":
    unittest.main()
