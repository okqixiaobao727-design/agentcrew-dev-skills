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
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


TESTS_DIR = pathlib.Path(__file__).resolve().parent
DRIVER = TESTS_DIR.parent / "driver.py"

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
RUN_DIR_NAME = ".crew"
FEATURE_NAME = "demo"
INTEGRATION_BRANCH = "crew/demo"
BASE_BRANCH = "main"

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
        git(self.repo, "add", "README.md")
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
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self._link_stub("claude", "stub_claude.py")
        self._link_stub("tmux", "stub_tmux.py")
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
        environment["CREW_POLL_SECONDS"] = "1"
        environment.pop("AGENTCREW_STUB_TRANSCRIPT_MODEL", None)
        environment.update(overrides or {})
        return environment

    def start(self, extra=(), env_overrides=None):
        return subprocess.run(
            [
                sys.executable, str(DRIVER), "start",
                "--feature-dir", str(self.feature_dir),
                "--coordinator-name", COORDINATOR_NAME,
                "--coordinator-pid", str(COORDINATOR_PID),
                "--permission-mode", PERMISSION_MODE,
                "--tmux-session", TMUX_SESSION,
                "--codex-bridge", str(self.codex_bridge),
                *extra,
            ],
            capture_output=True, text=True, env=self.environment(env_overrides),
            cwd=str(self.repo),
        )

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

    def windows(self):
        path = self.stub_dir / "tmux-windows.json"
        return json.loads(path.read_text()) if path.exists() else {}

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

    def assert_preflight_failed(self, result, problems):
        """A preflight failure: nothing launched, one snapshot line, the full list shown."""
        self.assertNotEqual(result.returncode, 0, result.stdout)
        snapshot = self.snapshot(result)
        self.assertEqual(snapshot["reason"], "preflight-failed")
        self.assertEqual(snapshot["surface"], PREFLIGHT_WINDOW)
        self.assertEqual(snapshot["count"], problems)
        self.assert_nothing_launched()
        return self.notice()


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

        result = self.fixture.start()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.fixture.windows_named(PREFLIGHT_WINDOW), {})

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

    def test_a_base_branch_origin_does_not_carry_has_nothing_to_fast_forward(self):
        self.fixture.ticket("01", "first thing")
        self.fixture.commit_feature()
        git(self.fixture.repo, "branch", "local-base")

        result = self.fixture.start(extra=("--base-branch", "local-base"))

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
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

        result = self.fixture.start()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.fixture.windows_named(PREFLIGHT_WINDOW), {})
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

    def test_a_passing_start_clears_the_notice_of_the_start_before_it(self):
        self.fixture.ticket("01", "no routing", routing="")
        self.fixture.commit_feature()
        self.fixture.start()
        self.fixture.ticket("01", "first thing")
        git(self.fixture.repo, "add", "-A", "features")
        git(self.fixture.repo, "commit", "-m", "route it")

        result = self.fixture.start()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.fixture.windows_named(PREFLIGHT_WINDOW), {})


class LaunchTests(DriverTestCase):
    def start_a_run(self, extra=(), env_overrides=None):
        self.fixture.ticket("01", "first thing")
        self.fixture.ticket("02", "second thing", blocked_by=("01",))
        self.fixture.commit_feature()
        return self.fixture.start(extra=extra, env_overrides=env_overrides)

    def test_the_launch_line_names_the_run_directory(self):
        result = self.start_a_run()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, f"stdout was not one line:\n{result.stdout}")
        self.assertIn(str(self.fixture.run_dir), lines[0])

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

    def test_a_relative_path_is_recorded_absolute(self):
        """Every path the table carries is read in a child's worktree, never in this cwd."""
        self.fixture.ticket("01", "first thing")
        self.fixture.commit_feature()

        result = self.fixture.start(extra=("--spec", "features/demo/spec.md"))

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.fixture.table()["run"]["spec_path"], str(self.fixture.spec_path))

    def test_the_integration_branch_is_cut_and_checked_out(self):
        self.start_a_run()

        self.assertIn(INTEGRATION_BRANCH, self.fixture.branches())
        self.assertEqual(self.fixture.current_branch(), INTEGRATION_BRANCH)

    def test_only_wave_one_is_dispatched_and_every_launch_is_logged(self):
        self.start_a_run()

        launched = [
            record for record in self.fixture.log_records() if record["event"] == "launch"
        ]
        self.assertEqual([record["ticket"] for record in launched], ["01"])
        self.assertEqual(len(self.fixture.launches()), 1)
        self.assertEqual(
            launched[0]["worktree"],
            str(self.fixture.repo / ".claude" / "worktrees" / "01-01"),
        )
        self.assertEqual(launched[0]["model"], CLAUDE_MODEL)

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

    def test_the_wake_monitor_is_armed_over_the_wave_s_children(self):
        self.start_a_run()

        self.assertTrue(
            self.fixture.wait_for_snapshot(),
            "no wake monitor asked the agents list what the wave's children are doing",
        )

    def test_a_codex_child_is_launched_and_watched_through_the_bridge(self):
        self.fixture.ticket("01", "first thing", routing=CODEX_ROUTING)
        self.fixture.commit_feature()

        result = self.fixture.start()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(
            self.fixture.wait_for_codex_watch(),
            "no wake monitor watched the wave's Codex child",
        )
        commands = [call["argv"][0] for call in self.fixture.codex_calls()]
        self.assertIn("launch", commands)
        watch = next(call for call in self.fixture.codex_calls() if call["argv"][0] == "watch")
        self.assertIn(str(self.fixture.run_dir / "codex" / "01.json"), watch["argv"])

    def test_a_child_that_will_not_come_up_exits_with_a_driver_error_snapshot(self):
        result = self.start_a_run(
            env_overrides={"AGENTCREW_STUB_TRANSCRIPT_MODEL": "claude-haiku-4-5-20251001"}
        )

        self.assertNotEqual(result.returncode, 0)
        snapshot = self.snapshot(result)
        self.assertEqual(snapshot["reason"], "driver-error")
        self.assertEqual(snapshot["ticket"], "01")
        self.assertIn(str(self.fixture.run_dir), snapshot["pointer"])


if __name__ == "__main__":
    unittest.main()
