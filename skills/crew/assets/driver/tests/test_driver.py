#!/usr/bin/env python3
"""Drive the crew driver from its command line against stubbed claude, tmux and codex.

Every fixture is built in a temporary root: a real git repository with a real origin to
fast-forward from, a feature directory of tickets carrying their own `## Routing` sections, and a
stub PATH carrying `claude` and `tmux`. Assertions are on external behavior only — the exit code,
the one stdout line, the wave table and machine log the run directory holds, the calls the stubs
recorded, and the repository's own branch state.

The fixture itself lives in `harness.py`, beside this file; here are only the tests.
"""

import fcntl
import json
import os
import pathlib
import re
import shlex
import signal
import subprocess
import sys
import time
import unittest

from harness import (
    BASE_BRANCH,
    CLAUDE_EFFORT,
    CLAUDE_MODEL,
    CODEX_EFFORT,
    CODEX_MODEL,
    CODEX_ROUTING,
    COORDINATOR_ADDRESS,
    COORDINATOR_NAME,
    COORDINATOR_PANE,
    COORDINATOR_PID,
    COORDINATOR_SESSION,
    DASHBOARD_WINDOW,
    DIRECT_ROUTING,
    DRIVER,
    DRIVER_RECORD,
    DriverTestCase,
    EXPLICIT,
    FEATURE_NAME,
    INHERITED,
    INTEGRATION_BRANCH,
    LAUNCH,
    MACHINE_LOG,
    MONITOR_WAVE_NAME,
    PARKED_PATHS,
    PERMISSION_MODE,
    PREFLIGHT_WINDOW,
    QUIET_SECONDS,
    REPAIR_MODEL,
    REPORT_NAME,
    ROUTING,
    TMUX_SESSION,
    TRACKER,
    TRIAGE,
    UNREWRITABLE,
    WAITER_RECORD,
    WAKE_NAME,
    WITNESS_BRIEF,
    WITNESS_BUDGET_USD,
    WITNESS_FAILURE,
    WITNESS_MODEL,
    WITNESS_OVERRUN,
    git,
    routing_naming,
)

sys.path.insert(0, str(DRIVER.parent))
import driver as driver_module  # noqa: E402

# The address a restarted coordinator binds: a second socket in a second directory, so a re-anchor
# that composed one out of the new pid would produce something else and be visible.
RESTARTED_ADDRESS = "uds:/private/tmp/cc-socks-501/2601.sock"


class StubTmuxTests(DriverTestCase):
    """The process-per-command stub preserves one tmux server's serialized state changes."""

    def test_a_second_command_waits_for_the_stub_server_lock(self):
        lock_path = self.fixture.stub_dir / "tmux-command.lock"
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            process = subprocess.Popen(
                [str(self.fixture.bin_dir / "tmux"), "list-windows", "-t", TMUX_SESSION],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self.fixture.environment(),
            )
            try:
                with self.assertRaises(subprocess.TimeoutExpired):
                    process.wait(timeout=1.0)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

        stdout, stderr = process.communicate(timeout=5.0)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(stdout, "")


class StrictLaunchReadTests(DriverTestCase):
    """Post-dispatch launch adoption refuses a damaged log instead of weakening the boundary."""

    def test_malformed_json_fails_the_post_dispatch_launch_read(self):
        log = self.fixture.root / "damaged-log.jsonl"
        log.write_text('{"event":"launch"}\nnot json\n')

        with self.assertRaises(json.JSONDecodeError):
            driver_module.launched_children(log)

    def test_every_other_launch_reader_boundary_remains_strict_or_accepted(self):
        log = self.fixture.root / "boundary-log.jsonl"
        with self.assertRaises(FileNotFoundError):
            driver_module.launched_children(log)

        log.write_text('\n{"event":"unknown"}\n')
        self.assertEqual(driver_module.launched_children(log), [])

        log.write_text("[]\n")
        with self.assertRaises(AttributeError):
            driver_module.launched_children(log)

        log.unlink()
        log.mkdir()
        with self.assertRaises(IsADirectoryError):
            driver_module.launched_children(log)

        log.rmdir()
        log.write_bytes(b"\xff\xfe")
        with self.assertRaises(UnicodeDecodeError):
            driver_module.launched_children(log)


class ReportSelectionTests(unittest.TestCase):
    """Report chronology remains distinct from the projection's non-empty settling fact."""

    def test_received_is_the_last_receipt_or_outcome_even_when_its_value_is_empty(self):
        records = (
            {"event": "outcome", "ticket": "7", "outcome": "completed", "ts": "01"},
            {"event": "receipt", "ticket": "7", "ts": "02"},
        )

        self.assertIs(driver_module.report_received(records, "7"), records[-1])

    def test_a_one_line_wrap_up_body_is_paired_with_its_placement(self):
        leftover = "Unreleased lock at src/lock.py:41"
        records = (
            {
                "event": "escalation", "ticket": "7",
                "message": f"CREW ASK 7 wrap-up — {leftover} ts=1",
            },
            {
                "event": "ruling", "ticket": "7",
                "message": "CREW RULED 7 — handed over",
            },
            {
                "event": "ruling", "ticket": "7",
                "message": f"{leftover} — opened #204",
            },
        )

        self.assertEqual(
            driver_module.report_rulings(records),
            [("7", f"{leftover} — opened #204")],
        )

    def test_an_unmatched_wrap_up_falls_back_verbatim_without_poisoning_later_rulings(self):
        records = (
            {
                "event": "escalation", "ticket": "7",
                "message": "A at a.py:1\nB at b.py:2\nCREW ASK 7 wrap-up ts=1",
            },
            {
                "event": "ruling", "ticket": "7",
                "message": "CREW RULED 7 — handed over",
            },
            {
                "event": "ruling", "ticket": "7",
                "message": "- A at a.py:1 — dropped\nB at b.py:2 — opened #205",
            },
            {
                "event": "ruling", "ticket": "7",
                "message": "CREW RULED 7 — second thing",
            },
        )

        self.assertEqual(
            driver_module.report_rulings(records),
            [
                ("7", "CREW RULED 7 — handed over"),
                ("7", "- A at a.py:1 — dropped\nB at b.py:2 — opened #205"),
                ("7", "CREW RULED 7 — second thing"),
            ],
        )

    def test_an_unanswered_wrap_up_keeps_its_handed_over_ruling(self):
        records = (
            {
                "event": "escalation", "ticket": "7",
                "message": "A at a.py:1\nCREW ASK 7 wrap-up ts=1",
            },
            {
                "event": "ruling", "ticket": "7",
                "message": "CREW RULED 7 — handed over",
            },
        )

        self.assertEqual(
            driver_module.report_rulings(records),
            [("7", "CREW RULED 7 — handed over")],
        )


class PreflightTests(DriverTestCase):
    def test_the_shipped_default_config_passes_preflight_without_a_launch_hook(self):
        shipped = DRIVER.parents[4] / "config" / "agentcrew.default.toml"
        (self.fixture.repo / "agentcrew.toml").write_text(
            shipped.read_text(encoding="utf-8"), encoding="utf-8"
        )
        git(self.fixture.repo, "add", "agentcrew.toml")
        git(self.fixture.repo, "commit", "-m", "use the shipped config")
        self.fixture.ticket("01", "first thing")
        self.fixture.commit_feature()

        self.started()

        self.assertNotIn("launch_hook", self.fixture.table()["run"])

    def test_a_foreign_worktree_at_the_crew_path_is_named_and_preserved(self):
        self.fixture.ticket("01", "first thing")
        self.fixture.commit_feature()
        foreign_branch = "operator/foreign-worktree"
        git(self.fixture.repo, "branch", foreign_branch, BASE_BRANCH)
        git(
            self.fixture.repo, "worktree", "add", str(self.fixture.crew_worktree),
            foreign_branch,
        )
        marker = self.fixture.crew_worktree / "foreign.txt"
        marker.write_text("not this Run\n")

        result = self.fixture.start()
        snapshot = self.snapshot(result)

        self.assertEqual(snapshot["reason"], "driver-error")
        self.assertIn(str(self.fixture.crew_worktree), snapshot["detail"])
        self.assertIn(f"branch {foreign_branch}", snapshot["detail"])
        self.assertTrue(marker.exists(), "fresh start removed the foreign worktree")
        self.assertIn(foreign_branch, self.fixture.branches())
        self.assertNotIn(INTEGRATION_BRANCH, self.fixture.branches())
        self.assertFalse(self.fixture.run_dir.exists())

    def test_a_stale_integration_branch_is_named_and_preserved(self):
        self.fixture.ticket("01", "first thing")
        self.fixture.commit_feature()
        base = git(self.fixture.repo, "rev-parse", BASE_BRANCH).stdout.strip()
        git(self.fixture.repo, "branch", INTEGRATION_BRANCH, base)

        result = self.fixture.start()
        snapshot = self.snapshot(result)

        self.assertEqual(snapshot["reason"], "driver-error")
        self.assertIn(INTEGRATION_BRANCH, snapshot["detail"])
        self.assertIn("no registered worktree", snapshot["detail"])
        self.assertEqual(
            git(self.fixture.repo, "rev-parse", INTEGRATION_BRANCH).stdout.strip(), base
        )
        self.assertFalse(self.fixture.crew_worktree.exists())
        self.assertFalse(self.fixture.run_dir.exists())

    def test_a_failing_configured_base_gate_stops_the_run_with_its_diagnosis(self):
        self.fixture.ticket("01", "first thing")
        self.fixture.commit_feature()
        output = "\n".join(f"gate line {number}" for number in range(30)) + "\n"
        self.fixture.configure_gate(exit_code=7, output=output)

        result = self.fixture.start()

        notice = self.assert_preflight_failed(result, 1)
        self.assertIn("base gate", notice)
        self.assertIn("base-gate --full", notice)
        self.assertIn("exit status 7", notice)
        self.assertIn("gate line 29", notice)
        self.assertNotIn("gate line 0\n", notice)
        self.assertEqual(
            self.fixture.gate_calls(),
            [
                {
                    "cwd": str(self.fixture.crew_worktree),
                    "argv": ["--full"],
                    "head": git(self.fixture.repo, "rev-parse", BASE_BRANCH).stdout.strip(),
                }
            ],
        )
        (self.fixture.stub_dir / "base-gate-exit").write_text("0")

        retry = self.started()

        self.assertIsNone(retry.poll())
        self.assertEqual(len(self.fixture.gate_calls()), 2)
        self.assertTrue(self.fixture.crew_worktree.is_dir())
        self.assertIn(INTEGRATION_BRANCH, self.fixture.branches())

    def test_a_passing_configured_base_gate_is_recorded_before_the_run_starts(self):
        self.fixture.ticket("01", "first thing")
        self.fixture.commit_feature()
        self.fixture.configure_gate(output="base is green\n")

        self.started()

        self.assertTrue(
            self.fixture.wait_for(lambda: len(self.events("base-gate")) == 1),
            "the passing base gate was not recorded",
        )
        base_gate = self.events("base-gate")
        self.assertEqual(len(base_gate), 1)
        self.assertEqual(base_gate[0]["status"], "passed")
        self.assertEqual(base_gate[0]["argv"], ["base-gate", "--full"])
        self.assertEqual(len(self.fixture.gate_calls()), 1)

    def test_a_dirty_working_tree_is_reported_without_running_the_base_gate(self):
        self.fixture.ticket("01", "first thing")
        self.fixture.commit_feature()
        self.fixture.configure_gate()
        (self.fixture.repo / "README.md").write_text("edited\n")

        result = self.fixture.start()

        notice = self.assert_preflight_failed(result, 1)
        self.assertIn("README.md", notice)
        self.assertEqual(self.fixture.gate_calls(), [])

    def test_a_remote_base_newer_than_local_does_not_move_the_run_snapshot(self):
        self.fixture.ticket("01", "first thing")
        self.fixture.commit_feature()
        self.fixture.configure_gate()
        local_tip = git(self.fixture.repo, "rev-parse", BASE_BRANCH).stdout.strip()
        other = self.fixture.root / "other-gated"
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

        self.started()

        self.assertEqual(self.fixture.table()["run"]["integration_base_commit"], local_tip)
        self.assertEqual(git(self.fixture.repo, "rev-parse", BASE_BRANCH).stdout.strip(), local_tip)
        self.assertEqual(self.fixture.gate_calls()[0]["head"], local_tip)

    def test_a_cross_branch_start_gates_the_local_base_and_leaves_source_untouched_on_failure(self):
        self.fixture.ticket("01", "first thing")
        self.fixture.commit_feature()
        self.fixture.configure_gate(exit_code=9, output="local base is red\n")
        git(self.fixture.repo, "push", "origin", BASE_BRANCH)
        git(self.fixture.repo, "switch", "-c", "starting-branch")
        (self.fixture.repo / "starting.md").write_text("starting branch\n")
        git(self.fixture.repo, "add", "starting.md")
        git(self.fixture.repo, "commit", "-m", "starting branch")
        starting_head = git(self.fixture.repo, "rev-parse", "HEAD").stdout.strip()

        other = self.fixture.root / "other-ahead"
        subprocess.run(
            ["git", "clone", str(self.fixture.origin), str(other)],
            check=True, capture_output=True,
        )
        git(other, "config", "user.email", "crew@example.invalid")
        git(other, "config", "user.name", "Crew Test")
        (other / "pulled.md").write_text("pulled base\n")
        git(other, "add", "pulled.md")
        git(other, "commit", "-m", "advance base")
        git(other, "push", "origin", BASE_BRANCH)
        local_base_head = git(self.fixture.repo, "rev-parse", BASE_BRANCH).stdout.strip()

        result = self.fixture.start()

        notice = self.assert_preflight_failed(result, 1)
        self.assertIn("exit status 9", notice)
        self.assertEqual(
            git(self.fixture.repo, "branch", "--show-current").stdout.strip(),
            "starting-branch",
        )
        self.assertEqual(git(self.fixture.repo, "rev-parse", "HEAD").stdout.strip(), starting_head)
        self.assertEqual(self.fixture.gate_calls()[0]["head"], local_base_head)

    def test_a_failing_base_gate_leaves_a_detached_starting_commit_unchanged(self):
        self.fixture.ticket("01", "first thing")
        self.fixture.commit_feature()
        self.fixture.configure_gate(exit_code=6, output="detached base is red\n")
        git(self.fixture.repo, "switch", "--detach", "HEAD")
        starting_head = git(self.fixture.repo, "rev-parse", "HEAD").stdout.strip()

        result = self.fixture.start()

        notice = self.assert_preflight_failed(result, 1)
        self.assertIn("exit status 6", notice)
        self.assertEqual(git(self.fixture.repo, "branch", "--show-current").stdout.strip(), "")
        self.assertEqual(git(self.fixture.repo, "rev-parse", "HEAD").stdout.strip(), starting_head)

    def test_a_malformed_base_gate_config_stops_before_any_command_runs(self):
        self.fixture.ticket("01", "first thing")
        self.fixture.commit_feature()
        self.fixture.configure(gate="python3 scripts/test.py")

        result = self.fixture.start()

        notice = self.assert_preflight_failed(result, 1)
        self.assertIn("argv list", notice)
        self.assertEqual(self.fixture.gate_calls(), [])

    def test_a_dirty_working_tree_stops_the_run(self):
        self.fixture.ticket("01", "first thing")
        self.fixture.commit_feature()
        (self.fixture.repo / "README.md").write_text("edited\n")
        branch = self.fixture.current_branch()
        head = git(self.fixture.repo, "rev-parse", "HEAD").stdout.strip()

        result = self.fixture.start()

        notice = self.assert_preflight_failed(result, 1)
        self.assertIn("README.md", notice)
        self.assertIn("committed", notice)
        self.assertEqual(self.fixture.current_branch(), branch)
        self.assertEqual(git(self.fixture.repo, "rev-parse", "HEAD").stdout.strip(), head)
        self.assertEqual((self.fixture.repo / "README.md").read_text(), "edited\n")

    def test_an_untracked_file_is_not_a_preflight_failure(self):
        self.fixture.ticket("01", "first thing")
        self.fixture.commit_feature()
        (self.fixture.repo / "scratch.txt").write_text("not mine\n")

        self.started()

    def test_a_missing_review_command_stops_a_run_that_reviews(self):
        """The review lane is an installed command, so preflight asks for it before the work."""
        self.fixture.ticket("01", "first thing")
        self.fixture.commit_feature()
        self.fixture.uninstall_review_command()

        result = self.fixture.start()

        notice = self.assert_preflight_failed(result, 1)
        self.assertIn("review-bridge", notice)
        self.assertIn("01", notice)
        self.assertIn("Review-Switch", notice)

    def test_a_missing_review_command_is_no_problem_for_a_run_that_reviews_nowhere(self):
        """A machine that reviews nowhere is not misconfigured for lacking the command."""
        self.fixture.ticket("01", "first thing", routing=DIRECT_ROUTING)
        self.fixture.commit_feature()
        self.fixture.uninstall_review_command()

        self.started()

    def test_a_remote_divergence_does_not_replace_the_local_base_tip(self):
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
        local_tip = git(self.fixture.repo, "rev-parse", BASE_BRANCH).stdout.strip()

        self.started()

        self.assertEqual(self.fixture.table()["run"]["integration_base_commit"], local_tip)
        self.assertEqual(git(self.fixture.repo, "rev-parse", BASE_BRANCH).stdout.strip(), local_tip)

    def test_an_origin_that_cannot_be_reached_is_not_consulted(self):
        self.fixture.ticket("01", "first thing")
        self.fixture.commit_feature()
        git(self.fixture.repo, "remote", "set-url", "origin", str(self.fixture.root / "gone.git"))

        local_tip = git(self.fixture.repo, "rev-parse", BASE_BRANCH).stdout.strip()
        self.started()

        self.assertEqual(self.fixture.table()["run"]["integration_base_commit"], local_tip)

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

    def test_a_hand_written_ticket_with_a_same_vendor_review_passes_preflight(self):
        self.fixture.ticket(
            "01", "same vendor", routing=ROUTING.replace(
                f"Review: codex {CODEX_MODEL} {CODEX_EFFORT}",
                f"Review: claude {CLAUDE_MODEL} {CLAUDE_EFFORT}",
            ),
        )
        self.fixture.commit_feature()

        self.started()

        ticket = self.fixture.table()["waves"][0]["tickets"][0]
        self.assertEqual(
            ticket["review"],
            {"vendor": "claude", "model": CLAUDE_MODEL, "effort": CLAUDE_EFFORT},
        )

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

    def test_a_ticket_naming_no_account_is_bound_to_the_coordinators_home_inherited(self):
        """Both halves of the binding: the home it is observed at, and that nothing sets it.

        The directory is still carried — it is what a child's transcript, cost and session files
        are read at — but the mode says the ticket's processes inherit the environment the run was
        started in rather than spelling that home out, which is not the same login (#110).
        """
        self.fixture.ticket("01", "first thing")
        self.fixture.ticket("02", "second thing")
        self.fixture.commit_feature()

        self.started()

        table = self.fixture.table()
        self.assertEqual(
            table["run"]["coordinator_config_home"], str(self.fixture.config_dir)
        )
        for number, row in self.rows().items():
            self.assertEqual(
                (row["account"], row["account_mode"]),
                (str(self.fixture.config_dir), INHERITED),
                number,
            )

    def test_a_ticket_naming_a_registered_account_carries_that_accounts_profile_directory(self):
        profile = self.fixture.profile("second")
        self.fixture.register(second=profile)
        self.fixture.ticket("01", "first thing", routing=routing_naming("second"))
        self.fixture.ticket("02", "second thing")
        self.fixture.commit_feature()

        self.started()

        rows = self.rows()
        self.assertEqual((rows["01"]["account"], rows["01"]["account_mode"]),
                         (str(profile), EXPLICIT))
        self.assertEqual((rows["02"]["account"], rows["02"]["account_mode"]),
                         (str(self.fixture.config_dir), INHERITED))

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
            (self.rows()["01"]["account"], self.rows()["01"]["account_mode"]),
            (str(self.fixture.config_dir), INHERITED),
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


class WakeMonitorAccountTests(DriverTestCase):
    """Every Claude child's liveness is asked of the account that child actually runs under.

    `claude agents --json` answers for the profile it is invoked under and for no other, so one
    monitor over a mixed wave asks a list that could not contain half of it. That is how ticket
    109 of a real run was settled `failed` — "the child's session vanished with no receipt sent" —
    ten seconds after launch, while its child was working and about to escalate (#110). The wake
    monitor is therefore armed one per account binding, and the stub CLI here answers each account
    out of its own list exactly as two logged-in profiles do.
    """

    def routed(self, name="second"):
        """A wave over two accounts: ticket 01 names one, 02 names none; returns 01's profile."""
        profile = self.fixture.profile(name)
        self.fixture.register(**{name: profile})
        self.fixture.ticket("01", "first thing", routing=routing_naming(name))
        self.fixture.ticket("02", "second thing")
        self.fixture.commit_feature()
        return profile

    def single_account(self):
        """A wave nobody named an account on: two Claude children on the run's own login."""
        self.fixture.ticket("01", "first thing")
        self.fixture.ticket("02", "second thing")
        self.fixture.commit_feature()

    def launched(self, *tickets):
        """Start the run and wait until every one of those tickets has a child of its own."""
        self.fixture.launch()
        for ticket in tickets:
            self.assertTrue(
                self.fixture.wait_for(
                    lambda ticket=ticket: self.fixture.verified_launch(ticket) is not None
                ),
                f"{ticket} never launched",
            )

    def monitors(self):
        """Every wake monitor over this run, as the paths each one was armed to watch.

        Read off the process table because the grouping is the thing under test: which worktrees
        one monitor stands over is written down nowhere else. `-A`, not `-e`: a monitor is started
        in a session of its own, and BSD `ps` leaves a process with no controlling terminal out of
        its default listing.
        """
        listed = subprocess.run(
            ["ps", "-A", "-o", "args="], capture_output=True, text=True
        ).stdout
        marker = str(self.fixture.run_dir / PARKED_PATHS)
        watched = []
        for line in listed.splitlines():
            if MONITOR_WAVE_NAME not in line or marker not in line:
                continue
            arguments = shlex.split(line)
            watched.append(sorted(
                os.path.realpath(path) for path in arguments[arguments.index(marker) + 1:]
            ))
        return sorted(watched)

    def wait_for_monitors(self, count):
        """The worktrees each armed monitor stands over, once there are `count` of them.

        A monitor forks a subshell of its own around each poll, so the same command line can
        appear twice for one monitor for as long as that poll takes; the wait is for the count
        the arming produced, which is the state the fork passes through and returns to. The
        reading that count was taken from is the reading returned: asking the process table
        again is another chance to land inside a fork, and a wait that passed on one reading
        has no business answering from another.
        """
        reading = []

        def armed():
            reading[:] = self.monitors()
            return len(reading) == count

        self.assertTrue(
            self.fixture.wait_for(armed),
            f"the run armed {len(reading)} wake monitors, not {count}",
        )
        return list(reading)

    def snapshot_homes(self):
        """The configuration home every agents-list read of this run was made under."""
        return [
            call["configHome"] for call in self.fixture.claude_calls()
            if call["argv"][:2] == ["agents", "--json"]
        ]

    def worktrees(self, *tickets):
        return sorted(os.path.realpath(self.fixture.worktree(ticket)) for ticket in tickets)

    def rows(self):
        """Every ticket of the built table, by its number."""
        return {
            ticket["id"]: ticket
            for wave in self.fixture.table()["waves"] for ticket in wave["tickets"]
        }

    def test_a_mixed_wave_arms_one_monitor_per_account_over_its_own_children(self):
        profile = self.routed()

        self.launched("01", "02")

        self.assertEqual(
            self.wait_for_monitors(2), sorted([self.worktrees("01"), self.worktrees("02")])
        )
        self.assertTrue(
            self.fixture.wait_for(
                lambda: {str(profile), str(self.fixture.config_dir)} <= set(self.snapshot_homes())
            ),
            f"the two accounts were not both asked: {self.snapshot_homes()}",
        )

    def test_a_child_alive_on_its_own_account_is_not_settled_failed(self):
        """The crewtask/65 shape, replayed: the child is listed, under its own account alone."""
        self.routed()

        self.launched("01", "02")
        # Long enough for the monitors to have polled many times over, so a child still unsettled
        # is an observation rather than a race won.
        time.sleep(QUIET_SECONDS)

        self.assertEqual([self.verdict("01"), self.verdict("02")], [None, None])
        self.assertEqual(self.events("receipt"), [])

    def test_a_single_account_wave_arms_one_monitor_in_the_environment_it_inherited(self):
        """The default path, unmoved: one monitor over the wave, on the run's own login."""
        self.single_account()

        self.launched("01", "02")

        self.assertEqual(self.wait_for_monitors(1), [self.worktrees("01", "02")])
        self.assertTrue(
            self.fixture.wait_for(lambda: self.snapshot_homes()),
            "no snapshot of the agents list was ever taken",
        )
        self.assertEqual(set(self.snapshot_homes()), {str(self.fixture.config_dir)})

    def test_an_inherited_lane_is_armed_with_no_configuration_home_of_its_own(self):
        """Nothing is injected: the monitor polls whatever login its driver was started on.

        The table's rows and the driver's environment name the same home on a first start, so
        "inherited" and "set to that same directory" are indistinguishable there. A resume pulls
        them apart — the table already carries the home the run began on, and this driver is
        started on another — and the account the monitor then asks is the whole assertion. An
        arming that spelled the row's directory into the environment would ask the old one.
        """
        self.single_account()
        self.launched("01", "02")
        elsewhere = self.fixture.profile("operators-own-login")
        first = self.fixture.running[0]
        first.kill()
        first.communicate()
        before = len(self.snapshot_homes())

        self.fixture.resume(env_overrides={"CLAUDE_CONFIG_DIR": str(elsewhere)})

        self.assertTrue(
            self.fixture.wait_for(lambda: len(self.snapshot_homes()) > before),
            "the resumed run never re-armed a monitor",
        )
        self.assertEqual(set(self.snapshot_homes()[before:]), {str(elsewhere)})
        self.assertEqual(
            self.rows()["01"]["account"], str(self.fixture.config_dir),
            "the table still carries the home the run began on",
        )

    def test_a_child_that_has_gone_from_its_own_account_still_settles_failed(self):
        """The word keeps meaning what it means: a genuinely vanished child is a failed ticket."""
        self.routed()
        self.launched("01", "02")

        self.fixture.vanishes("01")

        self.wait_for_verdict("01", "failed")
        self.assertIn("vanished", self.events("receipt", ticket="01")[-1]["detail"])
        self.assertEqual(self.verdict("02"), None)


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
                lambda: self.fixture.verified_launch("01") is not None
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
        self.assertEqual(run["crew_worktree"], str(self.fixture.crew_worktree))
        self.assertEqual(run["spec_path"], str(self.fixture.spec_path))
        self.assertEqual(run["integration_branch"], INTEGRATION_BRANCH)
        self.assertEqual(run["integration_base_commit"], head)
        self.assertEqual(run["base_branch"], BASE_BRANCH)
        self.assertEqual(run["coordinator_name"], COORDINATOR_NAME)
        self.assertEqual(run["coordinator_pid"], COORDINATOR_PID)
        self.assertEqual(run["coordinator_session"], COORDINATOR_SESSION)
        self.assertEqual(run["coordinator_address"], COORDINATOR_ADDRESS)
        self.assertEqual(run["tmux_session"], TMUX_SESSION)
        self.assertEqual(run["permission_mode"], PERMISSION_MODE)
        self.assertEqual(run["repair_model"], REPAIR_MODEL)
        self.assertEqual(run["witness_model"], WITNESS_MODEL)
        self.assertEqual(run["witness_budget_usd"], WITNESS_BUDGET_USD)
        self.assertEqual(run["tracker"], TRACKER)
        self.assertEqual(
            len({run["repo_root"], run["feature_dir"], run["crew_worktree"]}),
            3,
        )

    def test_a_relative_path_is_recorded_absolute(self):
        """Every path the table carries is read in a child's worktree, never in this cwd."""
        self.fixture.ticket("01", "first thing")
        self.fixture.commit_feature()

        self.launched(extra=("--spec", "features/demo/spec.md"))

        self.assertEqual(self.fixture.table()["run"]["spec_path"], str(self.fixture.spec_path))

    def test_the_source_checkout_stays_on_its_branch_and_the_crew_worktree_holds_integration(self):
        self.start_a_run()

        self.assertIn(INTEGRATION_BRANCH, self.fixture.branches())
        self.assertEqual(self.fixture.current_branch(), BASE_BRANCH)
        self.assertTrue(self.fixture.crew_worktree.is_dir())
        self.assertEqual(
            git(
                self.fixture.crew_worktree, "rev-parse", "--abbrev-ref", "HEAD"
            ).stdout.strip(),
            INTEGRATION_BRANCH,
        )
        ticket_worktree = self.fixture.worktree("01")
        self.assertEqual(ticket_worktree.parent, self.fixture.crew_worktree.parent)
        self.assertNotIn(self.fixture.crew_worktree, ticket_worktree.parents)

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
        """What a run in flight holds. Two more files belong to its ending rather than its life —
        the wake snapshot, written as the driver exits — and one to the launcher, which opens the
        driver's log before this driver runs at all."""
        self.start_a_run()

        self.assertEqual(
            sorted(path.name for path in self.fixture.run_dir.iterdir()),
            sorted([
                "wave-table.json", "log.jsonl", "launch", "parked-paths",
                "bounded_read.py", "dashboard-window", "dashboard-window.lock", "machine_log.py",
                DRIVER_RECORD,
            ]),
        )

    def test_the_coordinator_and_the_child_carry_this_run_s_hooks(self):
        session = "fixture-coordinator-session"
        self.start_a_run(extra=("--coordinator-session", session))

        log = str(self.fixture.run_dir / "log.jsonl")
        coordinator_settings = self.fixture.settings(
            self.fixture.repo / ".claude" / "settings.local.json"
        )
        coordinator = json.dumps(coordinator_settings)
        child = json.dumps(self.fixture.settings(
            self.fixture.repo / ".claude" / "worktrees" / "01-01"
            / ".claude" / "settings.local.json"
        ))
        self.assertIn(log, coordinator)
        self.assertIn("--role coordinator", coordinator)
        self.assertIn("bounded_read.py", coordinator)
        bounded_command, = [
            hook["command"]
            for block in coordinator_settings["hooks"]["PreToolUse"]
            if block["matcher"] == "Read|Grep|Glob|Bash"
            for hook in block["hooks"]
        ]
        bounded_words = shlex.split(bounded_command)
        self.assertEqual(
            bounded_words[bounded_words.index("--run-dir") + 1],
            str(self.fixture.feature_dir),
        )
        self.assertIn(f"--session-id {session}", coordinator)
        self.assertIn(log, child)
        self.assertIn("--role child", child)
        self.assertIn("--ticket 01", child)
        self.assertNotIn("bounded_read.py", child)

    def test_an_empty_coordinator_session_stops_before_installing_hooks(self):
        self.fixture.ticket("01", "first thing")
        self.fixture.commit_feature()

        result = self.fixture.start(extra=("--coordinator-session", ""))

        self.assertNotEqual(result.returncode, 0)
        self.assert_nothing_launched()

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

    def start(self, *tickets, shared=None, extra=(), env_overrides=None, routing=ROUTING):
        """A run with its first wave up and its loop running."""
        self.feature(*tickets, shared=shared, routing=routing)
        process = self.fixture.launch(extra=extra, env_overrides=env_overrides)
        for number, _ in tickets:
            if not _:
                self.assertTrue(
                    self.fixture.wait_for(
                        lambda number=number: self.fixture.verified_launch(number) is not None
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

    def assert_claude_receipt_command(self, instruction):
        run_machine_log = self.fixture.run_dir / MACHINE_LOG.name
        self.assertIn(f"python3 {run_machine_log}", instruction)
        self.assertIn(f"--log {self.fixture.run_dir / 'log.jsonl'}", instruction)
        self.assertNotIn("SendMessage", instruction)
        self.assertNotRegex(instruction, r"(?i)\bsend\b")

    # --- a whole run, with nothing outside the table in it ------------------------------------

    def test_a_clean_run_settles_every_wave_and_ends_without_one_wake(self):
        process = self.start(("01", ()), ("02", ("01",)))

        first_head = self.fixture.commit_work("01")
        self.fixture.says("01", f"CREW COMPLETE {first_head}")
        self.assertTrue(
            self.fixture.wait_for(lambda: self.fixture.verified_launch("02") is not None),
            "the run never advanced to wave 2",
        )
        self.assertEqual(
            git(
                self.fixture.worktree("02"),
                "merge-base", "--is-ancestor", first_head, "HEAD",
            ).returncode,
            0,
            "wave 2 was not cut from the integration state wave 1 landed",
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
        settings = self.fixture.settings(self.fixture.repo / ".claude" / "settings.local.json")
        self.assertNotIn("bounded_read.py", json.dumps(settings))
        self.assertTrue(self.fixture.crew_worktree.is_dir())
        self.assertIn(INTEGRATION_BRANCH, self.fixture.branches())

    def test_source_edits_and_a_branch_switch_after_launch_do_not_affect_later_waves(self):
        process = self.start(("01", ()), ("02", ("01",)))
        (self.fixture.repo / "README.md").write_text("operator edit\n")
        git(self.fixture.repo, "switch", "-c", "operator-work")

        first_head = self.fixture.commit_work("01")
        self.fixture.says("01", f"CREW COMPLETE {first_head}")
        self.assertTrue(
            self.fixture.wait_for(lambda: self.fixture.verified_launch("02") is not None),
            "the dirty source checkout interrupted wave 2",
        )

        self.assertEqual(self.fixture.current_branch(), "operator-work")
        self.assertEqual((self.fixture.repo / "README.md").read_text(), "operator edit\n")
        self.assertEqual(
            (self.fixture.crew_worktree / "README.md").read_text(), "fixture\n"
        )
        self.assertEqual(
            git(
                self.fixture.worktree("02"), "merge-base", "--is-ancestor", first_head, "HEAD"
            ).returncode,
            0,
        )
        self.fixture.completes("02")
        self.woken(process, "run-complete")

    def test_next_wave_precedes_the_tracker_close_commit_and_outcome(self):
        process = self.start(("01", ()), ("02", ("01",)))
        first_head = self.fixture.commit_work("01")
        self.fixture.says("01", f"CREW COMPLETE {first_head}")
        self.assertTrue(
            self.fixture.wait_for(
                lambda: (
                    self.fixture.verified_launch("02") is not None
                    and self.events("outcome", ticket="01", outcome="completed")
                )
            ),
            "wave 2 activation and the later tracker close never completed",
        )

        integration_branch = self.fixture.table()["run"]["integration_branch"]
        tracker_close = git(
            self.fixture.repo, "rev-parse", integration_branch
        ).stdout.strip()
        wave_head = git(self.fixture.worktree("02"), "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(
            git(
                self.fixture.worktree("02"),
                "merge-base", "--is-ancestor", first_head, wave_head,
            ).returncode,
            0,
            "wave 2 does not contain wave 1's landed code",
        )
        self.assertNotEqual(wave_head, tracker_close)
        self.assertNotEqual(
            git(
                self.fixture.worktree("02"),
                "merge-base", "--is-ancestor", tracker_close, wave_head,
                check=False,
            ).returncode,
            0,
            "wave 2 contains the later tracker-close commit",
        )

        records = self.fixture.log_records()
        launch = max(
            index for index, record in enumerate(records)
            if record.get("event") == "launch" and record.get("ticket") == "02"
        )
        launched = next(
            index for index, record in enumerate(records)
            if record.get("event") == "advance"
            and record.get("decision") == "launched"
            and str(record.get("wave")) == "2"
        )
        completed = next(
            index for index, record in enumerate(records)
            if record.get("event") == "outcome"
            and record.get("ticket") == "01"
            and record.get("outcome") == "completed"
        )
        self.assertLess(launch, launched)
        self.assertLess(launched, completed)

        self.fixture.completes("02")
        self.woken(process, "run-complete")

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
        self.assertIn(str(self.fixture.crew_worktree), report)
        self.assertIn("human", report.lower())
        self.assertIn("## Base gate", report)
        self.assertIn("- Base gate: none configured", report)
        base_gate = self.events("base-gate")
        self.assertEqual(len(base_gate), 1)
        self.assertEqual(base_gate[0]["status"], "not-configured")
        self.assertNotIn("command", base_gate[0])
        self.assertIn("TOTAL", report)
        cost = report.split("## Cost", 1)[1]
        self.assertRegex(cost, r"(?m)^coordinator\s+claude(?:\s+--){7}\s*$")
        self.assertIn("coordinator not measured:", cost)
        self.assertIn("session-wide upper bound", report)
        self.assertEqual(
            [record["ticket"] for record in self.events("session-cost")], ["01"]
        )
        self.assertEqual(snapshot["reason"], "run-complete")
        self.assertEqual(snapshot["integration_branch"], INTEGRATION_BRANCH)
        self.assertEqual(snapshot["crew_worktree"], str(self.fixture.crew_worktree))

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

        def corrupt_the_first_timestamp(records):
            next(record for record in records if record.get("event") == "launch")[
                "ts"
            ] = "not-a-machine-log-timestamp"
            return records

        self.fixture.edit_log(corrupt_the_first_timestamp)
        self.fixture.completes("01")

        self.woken(process, "driver-error")
        self.assertTrue(self.fixture.crew_worktree.is_dir())
        self.assertIn(INTEGRATION_BRANCH, self.fixture.branches())
        self.assertEqual(list(pin_dir.glob("*.json")), [])
        log = str(self.fixture.run_dir / "log.jsonl")
        coordinator_settings = self.fixture.settings(
            self.fixture.repo / ".claude" / "settings.local.json"
        )
        self.assertNotIn(log, json.dumps(coordinator_settings))
        self.assertNotIn("bounded_read.py", json.dumps(coordinator_settings))
        child_settings = self.fixture.worktree("01") / ".claude" / "settings.local.json"
        self.assertNotIn(log, json.dumps(self.fixture.settings(child_settings)))

    def test_the_cost_pass_reads_the_coordinator_transcript_into_its_own_row(self):
        session = "fixture-coordinator-session"
        self.fixture.coordinator_transcript(session)
        process = self.start(
            ("01", ()), extra=("--coordinator-session", session),
            env_overrides={"CLAUDE_CODE_SESSION_ID": "unrelated-detached-window-session"},
        )

        self.fixture.completes("01")
        self.woken(process, "run-complete")

        report = (self.fixture.feature_dir / "report.md").read_text()
        self.assertEqual(self.fixture.table()["run"]["coordinator_session"], session)
        coordinator_line = next(
            line for line in report.splitlines() if line.startswith("coordinator")
        )
        self.assertRegex(coordinator_line, r"\b\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+--$")

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
        self.assertRegex(cost_section, r"(?m)^coordinator\s+claude(?:\s+--){7}\s*$")
        self.assertIn("coordinator not measured:", cost_section)
        self.assertEqual(
            sorted(record["ticket"] for record in self.events("session-cost")),
            ["01", "02", "03"],
        )

    def test_the_report_costs_a_witness_and_keeps_a_design_ruling_rendered_as_before(self):
        process = self.start(("01", ()), env_overrides={
            "CLAUDE_CODE_SESSION_ID": "",
            "AGENTCREW_STUB_WITNESS_BRIEF": WITNESS_BRIEF,
        })
        self.fixture.says("01", "CREW ASK 01 design — use which seam? ts=1")
        self.woken(process, "judgment-needed")
        resumed = self.fixture.resume()
        self.assertIn("resumed", resumed.stdout.readline())
        ruling = "Use the existing public CLI seam"
        self.fixture.answers("01", ruling)
        self.fixture.completes("01")
        self.woken(resumed, "run-complete")

        report = (self.fixture.feature_dir / "report.md").read_text()
        rulings = report.split("## Rulings", 1)[1].split("## Outside-worktree effects", 1)[0]
        self.assertIn(f"- 01: {ruling}", rulings)
        cost = report.split("## Cost", 1)[1]
        self.assertRegex(
            cost,
            r"(?m)^witness-01\s+claude\s+claude-sonnet-5\s+11\s+22\s+33\s+44\s+110\s+\d+(?:\.\d+)?s$",
        )
        self.assertRegex(cost, r"(?m)^TOTAL(?:\s+--){2}\s+11\s+22\s+33\s+44\s+110\s+--$")

    def test_the_report_lists_each_wrap_up_leftover_beside_its_placement(self):
        process = self.start(("01", ()), env_overrides={
            "CLAUDE_CODE_SESSION_ID": "",
            "AGENTCREW_STUB_WITNESS_BRIEF": WITNESS_BRIEF,
        })
        first = "Unreleased lock at src/lock.py:41"
        second = "Duplicate cleanup in src/cleanup.py:9"
        third = "Missing assertion in tests/test_lock.py:88"
        self.fixture.says(
            "01", f"{first}\n{second}\n{third}\nCREW ASK 01 wrap-up ts=1"
        )
        self.woken(process, "judgment-needed")
        resumed = self.fixture.resume()
        self.assertIn("resumed", resumed.stdout.readline())
        self.fixture.answers(
            "01", f"{first} — opened #204\n{second} — dropped\n{third} — this ticket"
        )
        self.fixture.completes("01")
        self.woken(resumed, "run-complete")

        report = (self.fixture.feature_dir / "report.md").read_text()
        rulings = report.split("## Rulings", 1)[1].split("## Outside-worktree effects", 1)[0]
        self.assertIn(f"- 01: {first} — opened #204", rulings.splitlines())
        self.assertIn(f"- 01: {second} — dropped", rulings.splitlines())
        self.assertIn(f"- 01: {third} — this ticket", rulings.splitlines())

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
            self.fixture.wait_for(lambda: self.fixture.verified_launch("01") is not None),
            "01 never finished launch verification",
        )
        self.fixture.edit_log(lambda records: [
            record for record in records if record.get("event") != "launch"
        ])
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
        self.assertIn("0" * 40, instruction)
        self.assertIn("CREW COMPLETE", instruction)
        self.assert_claude_receipt_command(instruction)
        self.assertNotIn("send a new CREW COMPLETE", instruction)
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
        self.assertEqual(
            self.events("receipt", ticket="01", verdict="failed")[-1]["sha"], "1" * 40
        )

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
        """An ask bounce names every kind, its body separator and the protocol timestamp."""
        process = self.start(("01", ()))

        self.fixture.says("01", "CREW ASK 01 progress")
        instruction = self.wait_for_instruction("01", "CREW RESEND")
        self.fixture.completes("01")
        self.woken(process, "run-complete")

        self.assertIn(
            "CREW ASK <NN> <design|scope|doc-conflict|stuck|wrap-up>"
            " [— <body>] [ts=<unix>]",
            instruction,
        )
        self.assert_claude_receipt_command(instruction)
        self.assertNotIn("Send it in exactly the shape shown", instruction)
        self.assertEqual(self.events("escalation", ticket="01"), [])
        self.assertEqual(self.verdict("01"), "completed")

    def test_a_second_unknown_ask_kind_settles_the_ticket_failed(self):
        """The bounce is the unknown kind's only retry before the written failed outcome."""
        process = self.start(("01", ()))

        malformed = "CREW ASK 01 progress"
        self.fixture.says("01", malformed)
        self.wait_for_instruction("01", "CREW RESEND")
        self.fixture.says("01", malformed)
        self.wait_for_verdict("01", "failed")
        self.woken(process, "run-complete")

        self.assertEqual(len(self.instructions("01", "CREW RESEND")), 1)
        self.assertEqual(self.events("escalation", ticket="01"), [])
        failed = self.events("receipt", ticket="01", verdict="failed")
        self.assertIn(malformed, failed[-1]["detail"])

    def test_a_codex_child_is_still_told_to_send_a_bounced_receipt(self):
        process = self.start(("01", ()), routing=CODEX_ROUTING)

        self.fixture.says("01", "CREW ASK 01 progress")
        instruction = self.wait_for_instruction("01", "CREW RESEND")
        self.fixture.completes("01")
        self.woken(process, "run-complete")

        self.assertRegex(instruction, r"(?i)\bsend\b")
        self.assertEqual(self.verdict("01"), "completed")

    def test_a_codex_child_is_still_told_to_send_a_rechecked_receipt(self):
        process = self.start(("01", ()), routing=CODEX_ROUTING)

        self.fixture.says("01", "CREW COMPLETE " + "0" * 40)
        instruction = self.wait_for_instruction("01", "CREW RECHECK")
        self.fixture.completes("01")
        self.woken(process, "run-complete")

        self.assertRegex(instruction, r"(?i)\bsend\b")
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

    def test_resume_rejects_a_rewritten_table_whose_waves_no_longer_match_dependencies(self):
        process = self.start(("01", ()), ("02", ("01",)))
        process.kill()
        process.communicate()
        table_path = self.fixture.run_dir / "wave-table.json"
        table = json.loads(table_path.read_text())
        table["waves"][1]["tickets"][0]["blocked_by"] = []
        table_path.write_text(json.dumps(table))

        resumed = self.fixture.resume()
        snapshot = self.woken(resumed, "driver-error")

        self.assertIn("do not follow the dependency frontier", snapshot["detail"])
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
        self.assert_claude_receipt_command(instruction)
        self.assertNotIn("Send CREW COMPLETE", instruction)
        self.assertEqual(len(self.instructions("01", "CREW NUDGE")), 1)

    def test_a_codex_child_is_still_told_to_send_a_nudged_receipt(self):
        self.fixture.codex_goes("01", "idle")
        process = self.start(("01", ()), routing=CODEX_ROUTING)

        instruction = self.wait_for_instruction("01", "CREW NUDGE")
        self.fixture.completes("01")
        self.woken(process, "run-complete")

        self.assertRegex(instruction, r"(?i)\bsend\b")
        self.assertEqual(self.verdict("01"), "completed")

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

    def test_a_reviewed_child_is_nudged_afresh_rather_than_failed_on_the_stale_nudge(self):
        """A nudge addresses one silence, and a review lane's own lines are that silence breaking.

        The child said nothing itself, but its review ran and came back — which the log carries,
        and which is the ticket moving. The silence the nudge was sent into is over, so the next
        one is a new silence and is owed its own nudge rather than the failure the first earned.
        """
        process = self.start(("01", ()))

        self.fixture.goes("01", "idle")
        self.wait_for_instruction("01", "CREW NUDGE")
        self.fixture.goes("01", "busy")
        self.fixture.reviews("01", "running")
        self.fixture.reviews("01", "returned")
        self.fixture.goes("01", "idle")
        self.assertTrue(
            self.fixture.wait_for(lambda: len(self.instructions("01", "CREW NUDGE")) == 2),
            "the later silence was never nudged afresh",
        )
        self.fixture.goes("01", "busy")
        self.fixture.completes("01")
        self.woken(process, "run-complete")

        self.assertEqual(self.verdict("01"), "completed")

    def test_a_ruling_that_resumed_the_work_leaves_no_nudge_for_a_later_silence_to_inherit(self):
        """Ticket 104's own timeline, replayed: nudged, reviewed, asked, answered — then idle.

        The observed run failed that ticket four seconds after the coordinator's answer put it
        back to work, on a nudge sent before the question it had since asked. No review lane runs
        here, so the ask and the answer are the whole of what closes the nudge — the review's own
        line is the neighbouring test's subject. Everything between the two silences is in the
        log, and a driver that adopted the run part-way through must read it there, so the
        escalation's wake and the resume that carries the run on sit in the middle on purpose.
        """
        process = self.start(("01", ()))

        self.fixture.goes("01", "idle")
        self.wait_for_instruction("01", "CREW NUDGE")
        self.fixture.goes("01", "busy")
        self.fixture.says("01", "CREW ASK 01 scope — which table? ts=1")
        self.woken(process, "judgment-needed")

        resumed = self.fixture.resume()
        self.assertIn("resumed", resumed.stdout.readline())
        self.fixture.answers("01", "Use the existing retention_audit table")
        self.fixture.goes("01", "idle")
        self.assertTrue(
            self.fixture.wait_for(lambda: len(self.instructions("01", "CREW NUDGE")) == 2),
            "the silence after the ruling was never nudged afresh",
        )
        self.fixture.goes("01", "busy")
        self.fixture.completes("01")
        self.woken(resumed, "run-complete")

        self.assertEqual(self.verdict("01"), "completed")

    def test_an_unanswered_nudge_still_settles_the_ticket_failed_after_an_adoption(self):
        """The other half of reading the nudge off the log: a silence that never broke.

        The driver that sent the nudge is gone, and the one that adopts the run has nothing but
        the log to tell it whether the nudge was ever answered. Nothing followed it there, so the
        terminal rung is still the terminal rung, and it fires once rather than nudging again.
        """
        process = self.start(("01", ()))

        self.fixture.goes("01", "idle")
        self.wait_for_instruction("01", "CREW NUDGE")
        process.kill()
        process.communicate()

        resumed = self.fixture.resume()
        self.assertIn("resumed", resumed.stdout.readline())
        self.wait_for_verdict("01", "failed")
        self.woken(resumed, "run-complete")

        self.assertEqual(len(self.instructions("01", "CREW NUDGE")), 1)

    def test_a_vanished_child_settles_the_ticket_failed(self):
        process = self.start(("01", ()))

        self.fixture.vanishes("01")
        self.wait_for_verdict("01", "failed")
        self.woken(process, "run-complete")

        self.assertIn("vanished", self.events("receipt", ticket="01")[-1]["detail"])
        self.assertEqual(self.events("ruling", ticket="01"), [])

    def test_a_failed_codex_pane_read_is_a_driver_error_that_settles_no_ticket(self):
        (self.fixture.stub_dir / "codex-watch-fails").write_text("yes\n")

        process = self.start(("01", ()), routing=CODEX_ROUTING)
        snapshot = self.woken(process, "driver-error")

        self.assertIn("could not read tmux's pane list", snapshot["detail"])
        self.assertIsNone(self.verdict("01"))
        self.assertEqual(self.events("receipt", ticket="01"), [])

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

    def test_the_command_a_snapshot_names_puts_the_driver_back_in_a_window_of_its_own(self):
        """The launcher, never this driver's own `resume`: a coordinator that put the loop back
        as a task of its own session would be handing the harness the one process a run cannot
        lose. What that command does with the run is its own suite's; what it is, is this."""
        self.feature(("01", ()))
        stopped = self.fixture.ended(self.fixture.launch(extra=("--timeout", "3")))
        snapshot = json.loads(
            [line for line in stopped.stdout.splitlines() if line.strip()][-1]
        )

        self.assertIn(str(LAUNCH), snapshot["resume"])
        self.assertIn(str(self.fixture.feature_dir), snapshot["resume"])
        self.assertIn(f"--coordinator-pid {COORDINATOR_PID}", snapshot["resume"])

    def test_the_run_a_driver_error_stopped_is_carried_on_from_where_it_stopped(self):
        """A driver error is recovered exactly as a ruling is: the same seam, the same run."""
        self.feature(("01", ()))
        self.fixture.ended(self.fixture.launch(extra=("--timeout", "3")))

        resumed = self.fixture.resume()
        self.assertTrue(
            self.fixture.wait_for(lambda: self.fixture.verified_launch("01") is not None),
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

    def test_a_wrap_up_crew_ask_wakes_the_coordinator_carrying_the_ticket_and_ask(self):
        """Wrap-up is an ordinary escalation rather than a settling receipt."""
        process = self.start(("01", ()))

        message = "CREW ASK 01 wrap-up — should this leftover become a ticket? ts=1"
        self.fixture.says("01", message)
        snapshot = self.woken(process, "judgment-needed")

        self.assertEqual(snapshot["ticket"], "01")
        self.assertIn(message, snapshot["detail"])
        self.assertIsNone(self.verdict("01"), "a wrap-up ASK is not an outcome")

    def test_a_wrap_up_crew_ask_wakes_with_its_witness_brief_beside_the_detail(self):
        process = self.start(("01", ()), env_overrides={
            "AGENTCREW_STUB_WITNESS_BRIEF": WITNESS_BRIEF,
        })

        message = "CREW ASK 01 wrap-up — check README.md:1 ts=1"
        self.fixture.says("01", message)
        snapshot = self.woken(process, "judgment-needed")

        self.assertEqual(snapshot["detail"], message)
        self.assertEqual(snapshot["brief"], WITNESS_BRIEF)
        self.assertNotIn("witness_reason", snapshot)

    def test_a_failed_witness_still_wakes_with_an_empty_brief_and_its_reason(self):
        process = self.start(("01", ()), env_overrides={
            "AGENTCREW_STUB_WITNESS_BEHAVIOUR": "fail",
            "AGENTCREW_STUB_WITNESS_FAILURE": WITNESS_FAILURE,
        })

        message = "CREW ASK 01 scope — check README.md:1 ts=1"
        self.fixture.says("01", message)
        snapshot = self.woken(process, "judgment-needed")

        self.assertEqual(snapshot["reason"], "judgment-needed")
        self.assertEqual(snapshot["detail"], message)
        self.assertEqual(snapshot["brief"], "")
        self.assertEqual(snapshot["witness_reason"], WITNESS_FAILURE)
        witness = self.events("witness", ticket="01")
        self.assertEqual(len(witness), 1, witness)
        self.assertEqual(witness[0]["outcome"], "failed")
        self.assertEqual(witness[0]["reason"], WITNESS_FAILURE)

    def test_an_overrun_witness_still_wakes_with_the_timeout_reason(self):
        process = self.start(("01", ()), env_overrides={
            "AGENTCREW_STUB_WITNESS_BEHAVIOUR": "overrun",
            "AGENTCREW_STUB_WITNESS_FAILURE": WITNESS_OVERRUN,
        })

        self.fixture.says("01", "CREW ASK 01 stuck — check README.md:1 ts=1")
        snapshot = self.woken(process, "judgment-needed")

        self.assertEqual(snapshot["brief"], "")
        self.assertEqual(snapshot["witness_reason"], WITNESS_OVERRUN)
        self.assertEqual(
            self.events("witness", ticket="01")[0]["reason"], WITNESS_OVERRUN
        )

    def test_a_checked_brief_survives_an_incomplete_usage_block_without_cost(self):
        process = self.start(("01", ()), env_overrides={
            "AGENTCREW_STUB_WITNESS_BEHAVIOUR": "partial-usage",
            "AGENTCREW_STUB_WITNESS_BRIEF": WITNESS_BRIEF,
        })

        self.fixture.says("01", "CREW ASK 01 scope — check README.md:1 ts=1")
        snapshot = self.woken(process, "judgment-needed")

        self.assertEqual(snapshot["brief"], WITNESS_BRIEF)
        self.assertNotIn("witness_reason", snapshot)
        witness = self.events("witness", ticket="01")[0]
        self.assertEqual(witness["outcome"], "checked")
        self.assertEqual(witness["reason"], "")
        self.assertGreaterEqual(witness["duration_seconds"], 0)
        for field in (
            "input_tokens", "output_tokens", "cache_read_tokens",
            "cache_creation_tokens", "total_tokens",
        ):
            self.assertNotIn(field, witness)

    def test_the_witness_uses_its_configured_route_and_records_its_run(self):
        model = "claude-haiku-4-5-20251001"
        budget_usd = 1.25
        profile = self.fixture.profile("paid")
        self.fixture.register(paid=profile)
        self.fixture.configure(
            accounts=["paid"], witness_model=model, witness_budget_usd=budget_usd,
        )
        process = self.start(
            ("01", ()), routing=routing_naming("paid"), env_overrides={
                "AGENTCREW_STUB_WITNESS_BRIEF": WITNESS_BRIEF,
            },
        )

        self.fixture.says("01", "CREW ASK 01 design — check README.md:1 ts=1")
        snapshot = self.woken(process, "judgment-needed")

        self.assertEqual(snapshot["brief"], WITNESS_BRIEF)
        calls = [call for call in self.fixture.claude_calls() if "--print" in call["argv"]]
        self.assertEqual(len(calls), 1, calls)
        call = calls[0]
        self.assertEqual(call["argv"][call["argv"].index("--model") + 1], model)
        self.assertEqual(
            call["argv"][call["argv"].index("--max-budget-usd") + 1], str(budget_usd)
        )
        self.assertEqual(pathlib.Path(call["cwd"]).resolve(), self.fixture.worktree("01"))
        self.assertEqual(call["configHome"], str(profile))
        witness = self.events("witness", ticket="01")
        self.assertEqual(len(witness), 1, witness)
        self.assertEqual(witness[0]["executor"], "claude")
        self.assertEqual(witness[0]["model"], model)
        self.assertEqual(witness[0]["outcome"], "checked")
        self.assertEqual(witness[0]["reason"], "")
        self.assertGreaterEqual(witness[0]["duration_seconds"], 0)
        self.assertEqual(witness[0]["input_tokens"], 11)
        self.assertEqual(witness[0]["output_tokens"], 22)
        self.assertEqual(witness[0]["cache_read_tokens"], 33)
        self.assertEqual(witness[0]["cache_creation_tokens"], 44)
        self.assertEqual(witness[0]["total_tokens"], 110)

    def test_a_codex_childs_escalation_carries_the_same_witness_brief(self):
        process = self.start(
            ("01", ()), routing=CODEX_ROUTING, env_overrides={
                "AGENTCREW_STUB_WITNESS_BRIEF": WITNESS_BRIEF,
            },
        )

        message = "CREW ASK 01 scope — check README.md:1 ts=1"
        self.fixture.says("01", message)
        snapshot = self.woken(process, "judgment-needed")

        self.assertEqual(snapshot["detail"], message)
        self.assertEqual(snapshot["brief"], WITNESS_BRIEF)
        self.assertNotIn("witness_reason", snapshot)
        self.assertEqual(self.events("witness", ticket="01")[0]["outcome"], "checked")

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
        self.assert_claude_receipt_command(instruction)
        self.assertNotIn("send a new CREW COMPLETE", instruction)
        self.assertIn(
            "semantic", self.events("merge", ticket="02", result="conflict")[-1]["detail"]
        )

    def test_a_codex_child_is_still_told_to_send_a_merge_receipt(self):
        self.fixture.codex_goes("01", "busy")
        self.fixture.codex_goes("02", "busy")
        process = self.start(
            ("01", ()), ("02", ()), shared="one\n", routing=CODEX_ROUTING
        )

        self.fixture.completes("01", "01 rewrote\n", name="shared.txt")
        self.fixture.completes("02", "02 rewrote\n", name="shared.txt")
        instruction = self.wait_for_instruction("02", "CREW MERGE")

        self.assertRegex(instruction, r"(?i)\bsend\b")

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

    def test_a_late_completion_survives_an_unrelated_ruling_after_a_failed_nudge(self):
        """The whole issue-126 timeline is reconciled from the child's newer completion fact."""
        process = self.start(("01", ()), ("02", ()), shared="one\n")
        self.fixture.completes("01", "01 rewrote\n", name="shared.txt")
        self.fixture.completes("02", "02 rewrote\n", name="shared.txt")
        self.wait_for_instruction("02", "CREW MERGE")

        self.fixture.goes("02", "idle")
        self.wait_for_verdict("02", "failed")
        self.woken(process, "judgment-needed")

        merge = git(self.fixture.worktree("02"), "merge", INTEGRATION_BRANCH, check=False)
        self.assertNotEqual(merge.returncode, 0, "the semantic conflict was not reproduced")
        sha = self.fixture.commit_work("02", "resolved\n", name="shared.txt")
        self.fixture.says("02", f"CREW COMPLETE {sha}")
        self.fixture.answers("02", "Continue with the verified completion")

        resumed = self.fixture.resume()
        self.woken(resumed, "run-complete")

        self.assertEqual(
            [record["verdict"] for record in self.events("receipt", ticket="02")],
            ["landable", "failed", "landable"],
        )
        self.assertEqual(self.verdict("02"), "completed")
        self.assertEqual(len(self.instructions("02", "CREW MERGE")), 1)

    def test_a_descendant_launch_supersedes_the_blocked_outcome_left_by_a_repaired_root(self):
        """A stale derived block remains auditable but cannot settle the newly launched child."""
        process = self.start(
            ("01", ()), ("02", ()), ("03", ("02",)), shared="one\n"
        )
        self.fixture.completes("01", "01 rewrote\n", name="shared.txt")
        self.fixture.completes("02", "02 rewrote\n", name="shared.txt")
        self.wait_for_instruction("02", "CREW MERGE")

        self.fixture.goes("02", "idle")
        self.wait_for_verdict("02", "failed")
        self.woken(process, "judgment-needed")
        self.assertEqual(self.verdict("03"), "blocked")
        self.assertIsNone(self.fixture.launch_record("03"))

        merge = git(self.fixture.worktree("02"), "merge", INTEGRATION_BRANCH, check=False)
        self.assertNotEqual(merge.returncode, 0, "the semantic conflict was not reproduced")
        self.fixture.completes("02", "resolved\n", name="shared.txt")

        resumed = self.fixture.resume()
        self.assertTrue(
            self.fixture.wait_for(lambda: self.fixture.verified_launch("03") is not None),
            "the repaired root never released its descendant",
        )
        self.fixture.goes("03", "idle")
        self.wait_for_instruction("03", "CREW NUDGE")
        self.assertIsNone(resumed.poll(), "the stale blocked outcome stopped the live child's rule")
        self.fixture.goes("03", "busy")
        self.fixture.completes("03")
        self.woken(resumed, "run-complete")

        self.assertEqual(self.verdict("03"), "completed")
        self.assertEqual(len(self.events("outcome", ticket="03", outcome="blocked")), 1)
        self.assertEqual(
            [record["verdict"] for record in self.events("receipt", ticket="02")],
            ["landable", "failed", "landable"],
        )

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
        close_sha = git(self.fixture.crew_worktree, "rev-parse", "HEAD").stdout.strip()
        self.assertIn(f"(1) take the `Status: done` line off", closed[0]["detail"])
        self.assertIn(f"(2) run `git revert {close_sha}`", closed[0]["detail"])
        self.assertIn(str(self.fixture.crew_worktree), closed[0]["detail"])
        self.assertEqual(len(close_sha), 40)
        report = (self.fixture.feature_dir / REPORT_NAME).read_text()
        self.assertIn(f"(1) take the `Status: done` line off", report)
        self.assertIn(f"(2) run `git revert {close_sha}`", report)
        self.assertIn(str(self.fixture.crew_worktree), report)
        crew_ticket = self.fixture.crew_worktree / "features" / FEATURE_NAME / "01.md"
        self.assertIn("Status: done", crew_ticket.read_text())
        self.assertIn("Status: done", (self.fixture.feature_dir / "01.md").read_text())

    def test_a_gitignored_durable_ticket_is_closed_without_a_crew_copy(self):
        (self.fixture.repo / ".gitignore").write_text("features/\n")
        git(self.fixture.repo, "add", ".gitignore")
        git(self.fixture.repo, "commit", "-m", "ignore durable run records")
        self.fixture.ticket("01", "first thing")

        process = self.started()
        self.assertTrue(
            self.fixture.wait_for(lambda: self.fixture.verified_launch("01") is not None),
            "the ignored durable ticket's child never launched",
        )
        self.fixture.completes("01")
        self.woken(process, "run-complete")

        durable_ticket = self.fixture.feature_dir / "01.md"
        crew_ticket = self.fixture.crew_worktree / "features" / FEATURE_NAME / "01.md"
        self.assertIn("Status: done", durable_ticket.read_text())
        self.assertFalse(crew_ticket.exists())
        undo = self.events("outcome", ticket="01", outcome="completed")[0]["detail"]
        self.assertIn("undo: take the `Status: done` line off", undo)
        self.assertNotIn("git revert", undo)
        self.assertEqual(
            git(
                self.fixture.crew_worktree, "status", "--porcelain",
                "--untracked-files=no",
            ).stdout.strip(),
            "",
        )

    def test_a_local_close_leaves_the_working_tree_clean_for_the_next_wave(self):
        """A close is a write inside the repo, and an uncommitted one stops the merge after it."""
        process = self.start(("01", ()), ("02", ("01",)))

        self.fixture.completes("01")
        self.assertTrue(
            self.fixture.wait_for(lambda: self.fixture.verified_launch("02") is not None),
            "the run never advanced past the wave it closed a ticket in",
        )
        self.fixture.completes("02")
        self.woken(process, "run-complete")

        # Untracked paths are the run's own directory and the guard assets, which the run's own
        # clean-tree rule allows; what a merge refuses is a tracked file left uncommitted.
        left = git(
            self.fixture.crew_worktree, "status", "--porcelain", "--untracked-files=no"
        ).stdout
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
            self.assertEqual(
                os.path.realpath(call["cwd"]), os.path.realpath(self.fixture.crew_worktree)
            )


class DriverLifecycleTests(DriverTestCase):
    """What the run directory says about the process driving it, and when it stops saying it.

    The driver no longer runs as a background task of the coordinator's session, so nothing the
    coordinator holds can report that it ended. The run directory does: its loop names its own pid
    on the way in, and every exit it takes on purpose takes that name away again. A kill cannot,
    which is the whole of how a killed driver is told from one that handed judgment over.
    """

    def feature(self, *tickets, routing=ROUTING):
        for number, blockers in tickets:
            self.fixture.ticket(number, f"thing {number}", routing=routing, blocked_by=blockers)
        self.fixture.commit_feature()

    def start(self, *tickets, routing=ROUTING):
        """A run with its first wave up and its loop running."""
        self.feature(*tickets, routing=routing)
        process = self.fixture.launch()
        for number, blockers in tickets:
            if blockers:
                continue
            self.assertTrue(
                self.fixture.wait_for(
                    lambda number=number: self.fixture.verified_launch(number) is not None
                ),
                f"{number} never launched",
            )
        return process

    def record(self):
        """The pid the run directory names as its driver, or None where it names none."""
        path = self.fixture.run_dir / DRIVER_RECORD
        if not path.exists():
            return None
        text = path.read_text().strip()
        return int(text) if text.isdigit() else text

    def wake_file(self):
        """The wake snapshot the run directory holds, or None before one is written."""
        path = self.fixture.run_dir / WAKE_NAME
        return json.loads(path.read_text()) if path.exists() else None

    def assertReleased(self):
        self.assertIsNone(self.record(), "a deliberate exit left the driver's pid standing")

    # --- the record while the loop runs -----------------------------------------------------

    def test_a_running_loop_names_its_own_process_in_the_run_directory(self):
        process = self.start(("01", ()))

        self.assertTrue(
            self.fixture.wait_for(lambda: self.record() == process.pid),
            f"the run directory names {self.record()}, not the driver {process.pid}",
        )

        self.fixture.completes("01")
        self.woken(process, "run-complete")

    def test_a_resumed_run_is_named_after_the_driver_now_driving_it(self):
        """A run is driven by whichever process holds it, and the record says which."""
        process = self.start(("01", ()))
        self.fixture.says("01", "CREW ASK 01 scope — which table? ts=1")
        self.woken(process, "judgment-needed")

        resumed = self.fixture.resume()
        self.assertIn("resumed", resumed.stdout.readline())

        self.assertTrue(
            self.fixture.wait_for(lambda: self.record() == resumed.pid),
            f"the run directory names {self.record()}, not the driver {resumed.pid}",
        )
        self.fixture.completes("01")
        self.woken(resumed, "run-complete")

    # --- the three deliberate exits ---------------------------------------------------------

    def test_a_wake_handing_judgment_over_releases_the_run(self):
        process = self.start(("01", ()))

        self.fixture.says("01", "CREW ASK 01 scope — which table? ts=1")
        self.woken(process, "judgment-needed")

        self.assertReleased()

    def test_a_driver_error_releases_the_run(self):
        """A state the rule table has no row for still ends the driver on its own terms."""
        process = self.start(("01", ()))
        # A receipt whose child the log has no launch record for, which is a driver error rather
        # than anything a rule settles: the launch lines are taken back out from under it.
        self.fixture.edit_log(lambda records: [
            record for record in records if record.get("event") != "launch"
        ])
        self.fixture.says("01", "CREW COMPLETE " + "0" * 40)
        self.woken(process, "driver-error")

        self.assertReleased()

    def test_a_finished_run_releases_the_run(self):
        process = self.start(("01", ()))

        self.fixture.completes("01")
        self.woken(process, "run-complete")

        self.assertReleased()

    def test_an_interrupt_in_the_drivers_own_window_releases_the_run(self):
        """The operator's own Ctrl-C is deliberate, so it must not raise the dead-driver flag."""
        process = self.start(("01", ()))
        self.assertTrue(self.fixture.wait_for(lambda: self.record() == process.pid))

        process.send_signal(signal.SIGINT)
        process.communicate(timeout=30)

        self.assertReleased()

    # --- the one exit that is not deliberate -------------------------------------------------

    def test_a_killed_driver_leaves_its_pid_standing(self):
        """The stall this ticket is about: nothing runs on the way out of a SIGKILL, so the
        record is the only thing left that can say the run was orphaned."""
        process = self.start(("01", ()))
        self.assertTrue(self.fixture.wait_for(lambda: self.record() == process.pid))

        process.kill()
        process.communicate()

        self.assertEqual(self.record(), process.pid)

    # --- the wake channel -------------------------------------------------------------------

    def test_the_wake_snapshot_is_left_in_the_run_directory_as_well_as_printed(self):
        """The coordinator's waiter reads a file now: the driver's stdout is its own pane's."""
        process = self.start(("01", ()))

        self.fixture.says("01", "CREW ASK 01 scope — which table? ts=1")
        snapshot = self.woken(process, "judgment-needed")

        self.assertEqual(self.wake_file(), snapshot)

    def test_a_run_that_finishes_leaves_its_final_snapshot_in_the_run_directory(self):
        process = self.start(("01", ()))

        self.fixture.completes("01")
        snapshot = self.woken(process, "run-complete")

        self.assertEqual(self.wake_file(), snapshot)

    def test_the_record_is_released_before_the_wake_is_written(self):
        """Ordering, so a coordinator that pastes the resume command the instant it is woken
        cannot find a pid that is about to stop and attach to a driver that is already leaving."""
        process = self.start(("01", ()))

        self.fixture.says("01", "CREW ASK 01 scope — which table? ts=1")
        self.assertTrue(
            self.fixture.wait_for(lambda: self.wake_file() is not None),
            "the driver never wrote its wake snapshot",
        )
        self.assertReleased()

        self.woken(process, "judgment-needed")


class WakeWithNoWaiterTests(DriverTestCase):
    """A wake nobody is left to carry back is typed into the coordinator's own pane instead.

    The harness reaps the coordinator's waiter under memory pressure. The run is untouched, but
    the snapshot sits unread until a human re-types `/crew` — which is what this does for them,
    once per wake, on the same tmux channel a child is reached through (#127).
    """

    def start(self, ticket="01"):
        """A run with its first wave up and its loop running."""
        self.fixture.ticket(ticket, f"thing {ticket}")
        self.fixture.commit_feature()
        process = self.fixture.launch()
        self.assertTrue(
            self.fixture.wait_for(
                lambda: self.fixture.verified_launch(ticket) is not None
            ),
            f"{ticket} never launched",
        )
        return process

    def waiter_record(self, pid):
        """Name a process as this run's waiter, as the launcher names itself while it blocks."""
        path = self.fixture.run_dir / WAITER_RECORD
        path.write_text(f"{pid}\n")
        return path

    def dead_pid(self):
        """A pid that has certainly gone: a process this test started and then reaped."""
        process = subprocess.Popen([sys.executable, "-c", ""])
        process.wait()
        return process.pid

    def re_typed(self):
        """Every literal line the driver typed into the coordinator's pane, as tmux recorded it."""
        typed = []
        for call in self.fixture.tmux_calls():
            argv = call["argv"]
            if argv[:1] != ["send-keys"] or "-l" not in argv:
                continue
            if argv[argv.index("-t") + 1] != COORDINATOR_PANE:
                continue
            typed.append(argv[-1])
        return typed

    def resume_line(self):
        """The command a human would have re-typed, which is the one line this replaces."""
        return f"/crew {self.fixture.feature_dir}"

    # --- the wake with nobody waiting on it -----------------------------------------------------

    def test_a_wake_with_no_waiter_re_types_crew_into_the_coordinators_pane(self):
        """No record at all is no waiter: nothing was ever attached, or one was killed early."""
        process = self.start()

        self.fixture.says("01", "CREW ASK 01 scope — which table? ts=1")
        self.woken(process, "judgment-needed")

        self.assertEqual(self.re_typed(), [self.resume_line()])

    def test_a_waiter_record_naming_a_process_that_is_gone_is_no_waiter(self):
        """A reaped waiter cannot remove its own record, so the file is not the judgment."""
        process = self.start()
        self.waiter_record(self.dead_pid())

        self.fixture.says("01", "CREW ASK 01 scope — which table? ts=1")
        self.woken(process, "judgment-needed")

        self.assertEqual(self.re_typed(), [self.resume_line()])

    def test_a_run_that_finishes_with_no_waiter_is_re_typed_too(self):
        """Every wake reaches the coordinator, and a finished run's report is one of them."""
        process = self.start()

        self.fixture.completes("01")
        self.woken(process, "run-complete")

        self.assertEqual(self.re_typed(), [self.resume_line()])

    def test_the_line_goes_to_the_coordinators_pane_and_never_to_the_runs_session(self):
        """A session target is the active pane of whichever window is current there, so an
        operator watching a child or the dashboard would have the run's own recovery typed into
        it. The pane the launcher named is the only target that survives switching windows."""
        process = self.start()

        self.fixture.says("01", "CREW ASK 01 scope — which table? ts=1")
        self.woken(process, "judgment-needed")

        typed = [
            call["argv"] for call in self.fixture.tmux_calls()
            if call["argv"][:1] == ["send-keys"] and "-l" in call["argv"]
            and call["argv"][-1] == self.resume_line()
        ]
        self.assertEqual(len(typed), 1, typed)
        self.assertEqual(typed[0][typed[0].index("-t") + 1], COORDINATOR_PANE)

    def test_a_driver_told_no_pane_types_nothing_and_still_wakes(self):
        """Nothing is guessed. Where the launcher could not name the coordinator's pane there is
        no pane to type into, and the dashboard's own banner is what says so."""
        self.fixture.ticket("01", "thing 01")
        self.fixture.commit_feature()
        process = self.fixture.launch(extra=["--coordinator-pane", ""])
        self.assertTrue(
            self.fixture.wait_for(lambda: self.fixture.verified_launch("01") is not None),
            "01 never launched",
        )

        self.fixture.says("01", "CREW ASK 01 scope — which table? ts=1")
        self.woken(process, "judgment-needed")

        self.assertEqual(self.re_typed(), [])

    # --- the wake somebody is waiting on --------------------------------------------------------

    def test_a_wake_with_a_live_waiter_types_nothing_at_all(self):
        """The ordinary case: a waiter is blocked on this run, and it will print the snapshot."""
        process = self.start()
        self.waiter_record(os.getpid())

        self.fixture.says("01", "CREW ASK 01 scope — which table? ts=1")
        self.woken(process, "judgment-needed")

        self.assertEqual(self.re_typed(), [])

    # --- once per wake, never more ---------------------------------------------------------------

    def test_a_second_wake_of_the_same_run_re_types_once_again_and_no_more(self):
        """Once per wake is the whole rule: two wakes are two lines, never three."""
        process = self.start()
        self.fixture.says("01", "CREW ASK 01 scope — which table? ts=1")
        self.woken(process, "judgment-needed")
        self.assertEqual(self.re_typed(), [self.resume_line()])

        resumed = self.fixture.resume()
        self.fixture.completes("01")
        self.woken(resumed, "run-complete")

        self.assertEqual(self.re_typed(), [self.resume_line(), self.resume_line()])


class AnswerTests(DriverTestCase):
    def start(self, routing=ROUTING):
        self.fixture.ticket("01", "first thing", routing=routing)
        self.fixture.commit_feature()
        process = self.fixture.launch()
        self.assertTrue(
            self.fixture.wait_for(lambda: self.fixture.verified_launch("01") is not None),
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

    def test_text_left_in_the_composer_is_not_recorded_as_delivered(self):
        self.start()
        text = "Continue with the verified completion"
        window = self.fixture.launch_record("01")["window"]
        (self.fixture.stub_dir / "tmux-ignore-enter").touch()

        result = self.answer("--text", text)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.events("ruling", ticket="01"), [])
        enters = [
            call["argv"] for call in self.fixture.tmux_calls()
            if call["argv"] == ["send-keys", "-t", window, "Enter"]
        ]
        self.assertEqual(len(enters), 2)

    def assert_blank_text_is_not_recorded(self, text):
        self.start()
        window = self.fixture.launch_record("01")["window"]
        (self.fixture.stub_dir / "tmux-ignore-enter").touch()

        result = self.answer("--text", text)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.events("ruling", ticket="01"), [])
        enters = [
            call["argv"] for call in self.fixture.tmux_calls()
            if call["argv"] == ["send-keys", "-t", window, "Enter"]
        ]
        self.assertEqual(len(enters), 2)

    def test_empty_text_cannot_be_recorded_when_submission_is_not_observable(self):
        self.assert_blank_text_is_not_recorded("")

    def test_whitespace_text_cannot_be_recorded_when_submission_is_not_observable(self):
        self.assert_blank_text_is_not_recorded("   ")

    def test_text_delivery_retries_one_dropped_enter_before_recording_the_ruling(self):
        self.start()
        text = "Continue with the verified completion"
        window = self.fixture.launch_record("01")["window"]
        (self.fixture.stub_dir / "tmux-drop-enter-once").touch()

        result = self.answer("--text", text)

        self.assertEqual(result.returncode, 0, result.stderr)
        enters = [
            call["argv"] for call in self.fixture.tmux_calls()
            if call["argv"] == ["send-keys", "-t", window, "Enter"]
        ]
        self.assertEqual(len(enters), 2)
        self.assertEqual(self.events("ruling", ticket="01")[-1]["message"], text)

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

        def forget_where_that_child_is(records):
            launch = next(
                record
                for record in reversed(records)
                if record.get("event") == "launch" and record.get("ticket") == "01"
            )
            launch["window"] = None
            return records

        self.fixture.edit_log(forget_where_that_child_is)

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
                    lambda number=number: self.fixture.verified_launch(number) is not None
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

    def test_a_true_pre_launch_failure_is_dispatched_by_the_next_start(self):
        self.feature(("01", ()))
        failure = self.fixture.stub_dir / "tmux-new-window-fails"
        failure.write_text("yes\n")

        first = self.fixture.start()

        first_snapshot = self.snapshot(first)
        self.assertEqual(first_snapshot["reason"], "driver-error")
        self.assertIn("resume", first_snapshot)
        self.assertEqual(self.events("launch", ticket="01"), [])

        failure.unlink()
        recovered = self.fixture.launch()
        self.assertTrue(
            self.fixture.wait_for(lambda: self.fixture.verified_launch("01") is not None),
            "the next start never dispatched the ticket whose first launch never began",
        )
        self.fixture.completes("01")
        self.woken(recovered, "run-complete")

        self.assertEqual(len(self.events("launch", ticket="01")), 2)

    def test_a_partial_next_wave_commits_only_after_missing_work_is_recovered(self):
        self.feature(("01", ()), ("02", ("01",)), ("03", ("01",)))
        process = self.fixture.launch()
        self.assertTrue(
            self.fixture.wait_for(lambda: self.fixture.verified_launch("01") is not None),
            "wave 1 never launched",
        )
        failure = self.fixture.stub_dir / "tmux-new-window-fails-for"
        failure.write_text("03\n")

        self.fixture.completes("01")
        first_snapshot = self.woken(process, "driver-error")

        self.assertIn("resume", first_snapshot)
        attempts = self.events("advance", wave="2")
        self.assertEqual([event["decision"] for event in attempts], ["escalated"])
        self.assertEqual(
            driver_module.machine_log.project(self.fixture.log_records()).current_wave,
            1,
        )
        self.assertIsNotNone(self.fixture.verified_launch("02"))
        self.assertIsNone(self.fixture.launch_record("03"))
        pure_code_base = git(self.fixture.worktree("02"), "rev-parse", "HEAD").stdout.strip()
        tracker_close = git(
            self.fixture.repo,
            "rev-parse",
            self.fixture.table()["run"]["integration_branch"],
        ).stdout.strip()
        self.assertNotEqual(pure_code_base, tracker_close)

        failure.unlink()
        recovered = self.fixture.launch()
        self.assertTrue(
            self.fixture.wait_for(lambda: self.fixture.verified_launch("03") is not None),
            "the recovered activation never dispatched only the missing ticket",
        )
        recovered_base = git(self.fixture.worktree("03"), "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(recovered_base, pure_code_base)
        self.assertNotEqual(recovered_base, tracker_close)
        self.fixture.completes("02")
        self.fixture.completes("03")
        self.woken(recovered, "run-complete")

        self.assertEqual(len(self.events("launch", ticket="02")), 2)
        self.assertEqual(len(self.events("launch", ticket="03")), 2)
        attempts = self.events("advance", wave="2")
        self.assertEqual(
            [event["decision"] for event in attempts], ["escalated", "launched", "complete"]
        )
        self.assertEqual(
            driver_module.machine_log.project(self.fixture.log_records()).current_wave,
            2,
        )

    def test_a_partial_dispatch_restores_one_current_hook_for_the_started_sibling(self):
        self.feature(("01", ()), ("02", ("01",)), ("03", ("01",)))
        process = self.fixture.launch()
        self.assertTrue(
            self.fixture.wait_for(lambda: self.fixture.verified_launch("01") is not None),
            "wave 1 never launched",
        )
        (self.fixture.stub_dir / "tmux-new-window-fails-for").write_text("03\n")

        self.fixture.completes("01")
        snapshot = self.woken(process, "driver-error")

        self.assertIn("resume", snapshot)
        settings = self.fixture.settings(
            self.fixture.worktree("02") / ".claude" / "settings.local.json"
        )
        current_hooks = [
            hook["command"]
            for block in settings.get("hooks", {}).get("PostToolUse", [])
            for hook in block.get("hooks", [])
            if str(self.fixture.run_dir / "log.jsonl") in hook.get("command", "")
            and "--role child" in hook.get("command", "")
            and "--ticket 02" in hook.get("command", "")
        ]
        self.assertEqual(len(current_hooks), 1, settings)

    def test_an_unrecorded_observable_child_is_adopted_without_a_second_launch(self):
        self.interrupted(("01", ()))
        launches = list(self.fixture.launches())

        self.fixture.edit_log(lambda records: [
            record for record in records
            if not (record.get("event") == "launch" and record.get("ticket") == "01")
        ])
        self.assertIsNone(self.fixture.launch_record("01"))

        adopted = self.fixture.launch()
        self.assertTrue(
            self.fixture.wait_for(lambda: self.fixture.launch_record("01") is not None),
            "activation never adopted the observable child into the Machine log",
        )
        self.fixture.completes("01")
        self.woken(adopted, "run-complete")

        self.assertEqual(self.fixture.launches(), launches, "the observed child was launched twice")
        self.assertEqual(len(self.events("launch", ticket="01")), 1)

    def test_a_launch_failed_child_is_reverified_and_adopted_without_redispatch(self):
        self.feature(("01", ()))

        first = self.fixture.start(env_overrides={
            "AGENTCREW_STUB_TRANSCRIPT_MODEL": "claude-haiku-4-5-20251001",
        })

        first_snapshot = self.snapshot(first)
        self.assertEqual(first_snapshot["reason"], "driver-error")
        self.assertIn("resume", first_snapshot)
        self.assertEqual(len(self.events("launch", ticket="01")), 1)
        self.assertEqual(len(self.events("launch-failed", ticket="01")), 1)
        launches = list(self.fixture.launches())
        agent = json.loads(next(self.fixture.stub_dir.glob("agents-*.json")).read_text())[0]
        transcript = next(
            (self.fixture.config_dir / "projects").glob(f"*/{agent['sessionId']}.jsonl")
        )
        transcript.write_text(
            transcript.read_text().replace("claude-haiku-4-5-20251001", CLAUDE_MODEL)
        )

        adopted = self.fixture.launch()
        self.assertTrue(
            self.fixture.wait_for(lambda: len(self.events("launch", ticket="01")) == 2),
            "the failed launch's live child was not adopted after re-verification",
        )
        self.fixture.completes("01")
        self.woken(adopted, "run-complete")

        self.assertEqual(self.fixture.launches(), launches, "launch-failed was redispatched")
        ticket_events = [
            event["event"] for event in self.fixture.log_records()
            if event.get("ticket") == "01" and event.get("event") in ("launch", "launch-failed")
        ]
        self.assertEqual(ticket_events, ["launch", "launch-failed", "launch"])

    def test_a_launch_failed_child_that_still_fails_verification_is_reported(self):
        self.feature(("01", ()))
        first = self.fixture.start(env_overrides={
            "AGENTCREW_STUB_TRANSCRIPT_MODEL": "claude-haiku-4-5-20251001",
        })
        self.assertEqual(self.snapshot(first)["reason"], "driver-error")
        launches = list(self.fixture.launches())

        retried = self.fixture.start()

        retried_snapshot = json.loads([
            line for line in retried.stdout.splitlines() if line.strip()
        ][-1])
        self.assertEqual(retried_snapshot["reason"], "driver-error")
        self.assertIn("resume", retried_snapshot)
        self.assertIn("failed re-verification", retried_snapshot["detail"])
        self.assertIn("model mismatch", retried_snapshot["detail"])
        self.assertEqual(self.fixture.launches(), launches, "launch-failed was redispatched")
        self.assertEqual(len(self.events("launch", ticket="01")), 1)

    def test_an_unknown_live_source_dispatches_nothing_and_reports_resume(self):
        self.feature(("01", ()))
        failure = self.fixture.stub_dir / "tmux-new-window-fails"
        failure.write_text("yes\n")
        first = self.fixture.start()
        self.assertEqual(self.snapshot(first)["reason"], "driver-error")
        failure.unlink()

        cache = self.fixture.config_dir / "agentcrew" / "agents-cache.json"
        cache.unlink(missing_ok=True)
        claude = self.fixture.bin_dir / "claude"
        claude.write_text("#!/bin/sh\nexit 7\n")
        claude.chmod(0o755)

        retried = self.fixture.start()

        snapshot = json.loads([
            line for line in retried.stdout.splitlines() if line.strip()
        ][-1])
        self.assertEqual(snapshot["reason"], "driver-error")
        self.assertIn("resume", snapshot)
        self.assertIn("unknown", snapshot["detail"])
        self.assertEqual(self.events("launch", ticket="01"), [])
        self.assertEqual(self.fixture.launches(), [])

    def test_re_invoking_the_driver_over_an_unfinished_run_adopts_it(self):
        self.interrupted(("01", ()), ("02", ("01",)))
        run = self.fixture.table()["run"]

        adopted = self.fixture.launch()
        line = adopted.stdout.readline()
        self.fixture.completes("01")
        self.assertTrue(
            self.fixture.wait_for(lambda: self.fixture.verified_launch("02") is not None),
            "the adopted run never advanced to wave 2",
        )
        self.fixture.completes("02")
        self.woken(adopted, "run-complete")

        self.assertIn(str(self.fixture.run_dir), line)
        # A start that began again would have cut its integration branch afresh and recorded the
        # commit it cut it from; the run the adoption carried on is the one already on the ground.
        self.assertEqual(self.fixture.table()["run"], run, "the adopted run was started afresh")
        worktrees = git(self.fixture.repo, "worktree", "list", "--porcelain").stdout
        self.assertEqual(worktrees.count(f"worktree {self.fixture.crew_worktree}\n"), 1)
        self.assertEqual(len(self.events("launch", ticket="01")), 2, "01 was dispatched twice")
        self.assertEqual([self.verdict("01"), self.verdict("02")], ["completed", "completed"])

    def test_explicit_resume_reuses_the_recorded_crew_worktree(self):
        self.interrupted(("01", ()))
        recorded = self.fixture.table()["run"]["crew_worktree"]

        resumed = self.fixture.resume()
        self.assertIn("resumed", resumed.stdout.readline())
        self.fixture.completes("01")
        self.woken(resumed, "run-complete")

        self.assertEqual(self.fixture.table()["run"]["crew_worktree"], recorded)
        worktrees = git(self.fixture.repo, "worktree", "list", "--porcelain").stdout
        self.assertEqual(worktrees.count(f"worktree {recorded}\n"), 1)
        self.assertEqual(
            git(recorded, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(),
            INTEGRATION_BRANCH,
        )

    def test_adoption_refuses_a_foreign_directory_at_the_recorded_crew_worktree(self):
        self.interrupted(("01", ()))
        worktree = self.fixture.crew_worktree
        git(self.fixture.repo, "worktree", "remove", "--force", "--", str(worktree))
        worktree.mkdir(parents=True)
        marker = worktree / "foreign.txt"
        marker.write_text("not this Run\n")

        adopted = self.fixture.launch()
        result = self.fixture.ended(adopted)
        snapshot = self.snapshot(result)

        self.assertEqual(snapshot["reason"], "driver-error")
        self.assertIn(str(worktree), snapshot["detail"])
        self.assertIn("not a readable Git worktree", snapshot["detail"])
        self.assertTrue(marker.exists(), "adoption removed the foreign directory")

    def test_adopting_an_existing_run_does_not_run_its_configured_base_gate_again(self):
        self.fixture.configure_gate()
        self.interrupted(("01", ()))
        calls_before_adoption = self.fixture.gate_calls()
        (self.fixture.stub_dir / "base-gate-exit").write_text("8")

        adopted = self.fixture.launch()
        adopted.stdout.readline()
        self.fixture.completes("01")
        self.woken(adopted, "run-complete")

        self.assertEqual(self.fixture.gate_calls(), calls_before_adoption)

    def test_adopting_an_interrupted_run_leaves_one_bounded_read_hook(self):
        self.interrupted(("01", ()))

        adopted = self.fixture.launch()
        adopted.stdout.readline()
        settings_path = self.fixture.repo / ".claude" / "settings.local.json"
        installed = json.dumps(self.fixture.settings(settings_path))

        self.assertEqual(installed.count("bounded_read.py"), 1)
        self.fixture.completes("01")
        self.woken(adopted, "run-complete")

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
            self.fixture.wait_for(lambda: self.fixture.verified_launch("02") is not None),
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
        """A restarted coordinator binds a new socket, and a child trusts the old address."""
        self.interrupted(("01", ()), ("02", ("01",)))
        restarted_session = "3ed70d86-fa21-4d9c-adf2-b4073f60fbb6"
        restarted = (
            "--coordinator-name", "crew-coordinator-2a",
            "--coordinator-pid", "2601",
            "--coordinator-session", restarted_session,
            "--coordinator-address", RESTARTED_ADDRESS,
        )

        adopted = self.fixture.launch(extra=restarted)
        self.assertTrue(
            self.fixture.wait_for(lambda: self.events("ruling", ticket="01")),
            "the live child was never re-anchored",
        )
        anchor = self.events("ruling", ticket="01")
        installed = json.dumps(self.fixture.settings(
            self.fixture.repo / ".claude" / "settings.local.json"
        ))
        self.assertIn(f"--session-id {restarted_session}", installed)
        self.fixture.completes("01")
        self.assertTrue(
            self.fixture.wait_for(lambda: self.fixture.verified_launch("02") is not None),
            "the adopted run never advanced to wave 2",
        )
        self.fixture.completes("02")
        self.woken(adopted, "run-complete")

        self.assertEqual(
            self.fixture.table()["run"]["coordinator_name"], "crew-coordinator-2a"
        )
        self.assertEqual(
            self.fixture.table()["run"]["coordinator_session"], restarted_session
        )
        self.assertEqual(
            self.fixture.table()["run"]["coordinator_address"], RESTARTED_ADDRESS
        )
        self.assertEqual(len(anchor), 1, f"01 was not re-anchored once: {anchor}")
        self.assertIn(RESTARTED_ADDRESS, anchor[0]["message"])
        self.assertIn("crew-coordinator-2a", anchor[0]["message"])
        launched = [
            call for call in self.fixture.launches()
            if str(self.fixture.repo / ".claude" / "worktrees" / "02-02") in json.dumps(call)
        ]
        self.assertEqual(len(launched), 1, "02 was not launched once")
        self.assertIn(RESTARTED_ADDRESS, json.dumps(launched[0]))

    def test_a_same_address_new_session_adoption_updates_the_hook_without_reanchoring(self):
        """The condition is the address: what a child was told to trust has not moved."""
        self.interrupted(("01", ()))
        restarted_session = "4fd70d86-fa21-4d9c-adf2-b4073f60fbb6"

        adopted = self.fixture.launch(extra=("--coordinator-session", restarted_session))
        self.assertTrue(
            self.fixture.wait_for(
                lambda: self.fixture.table()["run"]["coordinator_session"] == restarted_session
            ),
            "the adopted run kept the old coordinator session",
        )
        self.fixture.completes("01")
        self.woken(adopted, "run-complete")

        self.assertEqual(self.fixture.table()["run"]["coordinator_pid"], COORDINATOR_PID)
        self.assertEqual(
            self.fixture.table()["run"]["coordinator_session"], restarted_session
        )
        self.assertEqual(self.events("ruling", ticket="01"), [])

    def test_a_coordinator_that_rebound_its_socket_re_anchors_on_the_same_pid(self):
        """The condition names the address, not a proxy for it: only the address moved here."""
        self.interrupted(("01", ()))
        rebound = "uds:/private/tmp/cc-socks-501/1504.sock"

        adopted = self.fixture.launch(extra=("--coordinator-address", rebound))
        self.assertTrue(
            self.fixture.wait_for(lambda: self.events("ruling", ticket="01")),
            "the live child was never re-anchored",
        )
        anchor = self.events("ruling", ticket="01")
        self.fixture.completes("01")
        self.woken(adopted, "run-complete")

        self.assertEqual(self.fixture.table()["run"]["coordinator_pid"], COORDINATOR_PID)
        self.assertEqual(self.fixture.table()["run"]["coordinator_address"], rebound)
        self.assertEqual(len(anchor), 1, f"01 was not re-anchored once: {anchor}")
        self.assertIn(rebound, anchor[0]["message"])

    def test_a_codex_child_is_not_re_anchored_because_its_channel_is_a_file(self):
        self.interrupted(("01", ()), routing=CODEX_ROUTING)

        adopted = self.fixture.launch(extra=(
            "--coordinator-name", "crew-coordinator-2a", "--coordinator-pid", "2601",
            "--coordinator-address", RESTARTED_ADDRESS,
        ))
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

        adopted = self.fixture.launch(extra=(
            "--coordinator-name", "crew-coordinator-2a", "--coordinator-pid", "2601",
            "--coordinator-address", RESTARTED_ADDRESS,
        ))
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
        self.assertIn(RESTARTED_ADDRESS, anchor[0]["message"])

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
