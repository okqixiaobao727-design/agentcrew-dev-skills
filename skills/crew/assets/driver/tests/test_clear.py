#!/usr/bin/env python3
"""Exercise the driver's clear command at its terminal CLI seam.

The fixture writes the run records independently of the command under test. The assertions observe
the terminal inventory, git state, tmux state, hook settings and Codex bridge calls after the
confirmation decision.
"""

import json
import os
import pathlib
import shutil
import subprocess
import sys
import unittest

from test_driver import BASE_BRANCH, DRIVER, Fixture, INTEGRATION_BRANCH, git


MERGED_BRANCH = "recorded/merged"
UNMERGED_BRANCH = "recorded/unmerged"
MERGED_WINDOW = "@11"
UNMERGED_WINDOW = "@12"
DASHBOARD_WINDOW = "@13"
UNRECORDED_BRANCH = "keep/branch"
UNRECORDED_WINDOW = "@99"
UNMERGED_COMMIT_MESSAGE = "unmerged commit"
UNCOMMITTED_FILE = "discard-me.txt"
REPORT_NAME = "report.md"


def read_json_lines(path):
    path = pathlib.Path(path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class ClearTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()
        self.addCleanup(self.fixture.cleanup)
        self.seed_run()

    def run_clear(self, answer):
        return subprocess.run(
            [sys.executable, str(DRIVER), "clear", "--run-dir", str(self.run_dir)],
            input=f"{answer}\n",
            capture_output=True,
            text=True,
            env=self.fixture.environment(),
            cwd=str(self.fixture.repo),
        )

    def seed_run(self):
        self.fixture.ticket("01", "merged ticket")
        self.fixture.ticket("02", "unmerged ticket")
        self.fixture.commit_feature()

        git(self.fixture.repo, "switch", "-c", INTEGRATION_BRANCH)

        self.merged_worktree = self.fixture.repo / ".recorded" / "merged-worktree"
        git(self.fixture.repo, "worktree", "add", "-b", MERGED_BRANCH,
            str(self.merged_worktree), BASE_BRANCH)
        (self.merged_worktree / "merged.txt").write_text("merged\n")
        git(self.merged_worktree, "add", "merged.txt")
        git(self.merged_worktree, "commit", "-m", "merged commit")
        git(self.fixture.repo, "merge", "--no-ff", MERGED_BRANCH, "-m", "merge recorded branch")

        self.unmerged_worktree = self.fixture.repo / ".recorded" / "unmerged-worktree"
        git(self.fixture.repo, "branch", UNMERGED_BRANCH, BASE_BRANCH)
        git(self.fixture.repo, "worktree", "add", str(self.unmerged_worktree), UNMERGED_BRANCH)
        (self.unmerged_worktree / "committed.txt").write_text("committed\n")
        git(self.unmerged_worktree, "add", "committed.txt")
        git(self.unmerged_worktree, "commit", "-m", UNMERGED_COMMIT_MESSAGE)
        (self.unmerged_worktree / UNCOMMITTED_FILE).write_text("uncommitted\n")
        git(self.fixture.repo, "worktree", "lock", str(self.unmerged_worktree),
            "--reason", "clear test")

        git(self.fixture.repo, "branch", UNRECORDED_BRANCH, BASE_BRANCH)

        self.run_dir = self.fixture.feature_dir / ".crew"
        self.codex_dir = self.run_dir / "codex"
        self.run_dir.mkdir()
        self.codex_dir.mkdir()
        self.log_path = self.run_dir / "log.jsonl"
        self.table_path = self.run_dir / "wave-table.json"
        self.report_path = self.fixture.feature_dir / REPORT_NAME
        self.report_path.write_text("durable report\n")

        repo_root = str(self.fixture.repo)
        run = {
            "repo_root": repo_root,
            "spec_path": str(self.fixture.spec_path),
            "integration_branch": INTEGRATION_BRANCH,
            "integration_base_commit": git(
                self.fixture.repo, "rev-parse", BASE_BRANCH
            ).stdout.strip(),
            "coordinator_name": "coordinator",
            "coordinator_pid": 1504,
            "crew_skill_dir": str(DRIVER.parents[1]),
            "tmux_session": "$7:",
            "permission_mode": "acceptEdits",
            "base_branch": BASE_BRANCH,
            "return_branch": BASE_BRANCH,
            "feature_dir": str(self.fixture.feature_dir),
            "codex": {
                "bridge": str(DRIVER.parent / "tests" / "stub_codex_bridge.py"),
                "state_dir": str(self.codex_dir),
            },
        }
        tickets = [
            {
                "id": "01",
                "title": "merged ticket",
                "path": str(self.fixture.feature_dir / "01.md"),
                "workflow": "tdd",
                "executor": "claude",
                "model": "claude-opus-5",
                "effort": "medium",
                "blocked_by": [],
            },
            {
                "id": "02",
                "title": "unmerged ticket",
                "path": str(self.fixture.feature_dir / "02.md"),
                "workflow": "tdd",
                "executor": "codex",
                "model": "gpt-5.6-luna",
                "effort": "max",
                "blocked_by": [],
            },
        ]
        self.table_path.write_text(json.dumps({
            "run": run,
            "waves": [{"wave": 1, "tickets": tickets}],
        }) + "\n")
        self.log_path.write_text("\n".join([
            json.dumps({
                "ts": "2026-08-15T00:00:00Z",
                "event": "launch",
                "ticket": "01",
                "child": "claude-child",
                "workflow": "tdd",
                "executor": "claude",
                "model": "claude-opus-5",
                "effort": "medium",
                "branch": MERGED_BRANCH,
                "worktree": str(self.merged_worktree),
                "window": MERGED_WINDOW,
            }),
            json.dumps({
                "ts": "2026-08-15T00:00:01Z",
                "event": "launch",
                "ticket": "02",
                "child": "codex-thread",
                "workflow": "tdd",
                "executor": "codex",
                "model": "gpt-5.6-luna",
                "effort": "max",
                "branch": UNMERGED_BRANCH,
                "worktree": str(self.unmerged_worktree),
                "window": UNMERGED_WINDOW,
            }),
        ]) + "\n")

        (self.codex_dir / "02.json").write_text(json.dumps({
            "windowId": UNMERGED_WINDOW,
            "paneId": "%12",
            "runtimeDir": str(self.codex_dir / "runtime-02"),
            "name": "02",
        }))
        (self.codex_dir / "runtime-02").mkdir()
        (self.run_dir / "dashboard-window").write_text(DASHBOARD_WINDOW + "\n")
        (self.fixture.stub_dir / "tmux-windows.json").write_text(json.dumps({
            MERGED_WINDOW: {"name": "01", "target": "$7:"},
            UNMERGED_WINDOW: {"name": "02", "target": "$7:"},
            DASHBOARD_WINDOW: {"name": "crew-dashboard", "target": "$7:"},
            UNRECORDED_WINDOW: {"name": "unrelated", "target": "$7:"},
        }))

        settings_path = self.fixture.repo / ".claude" / "settings.local.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps({
            "hooks": {
                "PostToolUse": [{
                    "matcher": "UnrelatedTool",
                    "hooks": [{"type": "command", "command": "unrelated"}],
                }],
            },
        }))
        self.install_coordinator_hook()

    def install_coordinator_hook(self):
        machine_log = DRIVER.parents[1] / "machine_log.py"
        result = subprocess.run(
            [sys.executable, str(machine_log), "--log", str(self.log_path), "install",
             "--settings", str(self.fixture.repo / ".claude" / "settings.local.json"),
             "--role", "coordinator"],
            capture_output=True,
            text=True,
            env=self.fixture.environment(),
            cwd=str(self.fixture.repo),
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_declining_confirmation_removes_nothing(self):
        before_branches = git(
            self.fixture.repo, "branch", "--format=%(refname:short)"
        ).stdout
        before_worktrees = git(self.fixture.repo, "worktree", "list", "--porcelain").stdout
        before_windows = self.fixture.windows()
        before_settings = (self.fixture.repo / ".claude" / "settings.local.json").read_bytes()

        result = self.run_clear("n")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("01", result.stdout)
        self.assertIn("02", result.stdout)
        ask_offset = result.stdout.index("Clear this run?")
        self.assertLess(result.stdout.index(UNCOMMITTED_FILE), ask_offset)
        self.assertLess(result.stdout.index(UNMERGED_COMMIT_MESSAGE), ask_offset)
        self.assertEqual(
            git(self.fixture.repo, "branch", "--format=%(refname:short)").stdout,
            before_branches,
        )
        self.assertEqual(
            git(self.fixture.repo, "worktree", "list", "--porcelain").stdout,
            before_worktrees,
        )
        self.assertEqual(self.fixture.windows(), before_windows)
        self.assertEqual(
            (self.fixture.repo / ".claude" / "settings.local.json").read_bytes(),
            before_settings,
        )
        self.assertTrue((self.codex_dir / "02.json").exists())
        self.assertTrue(self.run_dir.exists())
        self.assertTrue(self.report_path.exists())
        self.assertEqual(read_json_lines(self.fixture.stub_dir / "codex-calls.jsonl"), [])

    def test_confirmation_deletes_merged_branch_when_head_is_elsewhere(self):
        git(self.fixture.repo, "switch", BASE_BRANCH)

        result = self.run_clear("y")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        branches = git(self.fixture.repo, "branch", "--format=%(refname:short)").stdout
        self.assertNotIn(MERGED_BRANCH, branches)
        self.assertEqual(
            git(self.fixture.repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(),
            BASE_BRANCH,
        )

    def test_confirmation_tolerates_a_recorded_worktree_already_gone(self):
        shutil.rmtree(self.merged_worktree)

        result = self.run_clear("y")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("already gone", result.stdout)
        self.assertNotIn(MERGED_BRANCH, git(
            self.fixture.repo, "branch", "--format=%(refname:short)"
        ).stdout)

    def test_confirmation_tolerates_a_tmux_server_already_gone(self):
        (self.fixture.stub_dir / "tmux-server-gone").write_text("yes\n")

        result = self.run_clear("y")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn(UNMERGED_BRANCH, git(
            self.fixture.repo, "branch", "--format=%(refname:short)"
        ).stdout)

    def test_confirmation_lists_discard_and_removes_only_recorded_run_artifacts(self):
        result = self.run_clear("y")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for value in (
            "01",
            "02",
            MERGED_WINDOW,
            UNMERGED_WINDOW,
            DASHBOARD_WINDOW,
            str(self.merged_worktree),
            str(self.unmerged_worktree),
            MERGED_BRANCH,
            UNMERGED_BRANCH,
            UNCOMMITTED_FILE,
            UNMERGED_COMMIT_MESSAGE,
            str(self.codex_dir / "02.json"),
            "delete with -d",
            "force-delete with -D",
        ):
            self.assertIn(value, result.stdout)

        worktree_list = git(self.fixture.repo, "worktree", "list", "--porcelain").stdout
        self.assertNotIn(str(self.merged_worktree), worktree_list)
        self.assertNotIn(str(self.unmerged_worktree), worktree_list)
        branches = git(self.fixture.repo, "branch", "--format=%(refname:short)").stdout
        self.assertNotIn(MERGED_BRANCH, branches)
        self.assertNotIn(UNMERGED_BRANCH, branches)
        self.assertNotIn(INTEGRATION_BRANCH, branches)
        self.assertIn(BASE_BRANCH, branches)
        self.assertEqual(
            git(self.fixture.repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(),
            BASE_BRANCH,
        )
        self.assertIn(UNRECORDED_BRANCH, branches)
        self.assertIn(UNRECORDED_WINDOW, json.dumps(self.fixture.windows()))
        self.assertEqual(self.fixture.windows(), {
            UNRECORDED_WINDOW: {"name": "unrelated", "target": "$7:"},
        })

        self.assertTrue(self.run_dir.exists())
        self.assertTrue(self.table_path.exists())
        self.assertTrue(self.log_path.exists())
        self.assertTrue(self.report_path.exists())
        self.assertFalse(self.codex_dir.exists())

        settings = json.loads(
            (self.fixture.repo / ".claude" / "settings.local.json").read_text()
        )
        commands = [
            hook.get("command", "")
            for event in settings["hooks"].get("PostToolUse", [])
            for hook in event.get("hooks", [])
        ]
        self.assertFalse(any(str(self.log_path) in command for command in commands))
        self.assertIn("unrelated", json.dumps(settings))

        codex_calls = read_json_lines(self.fixture.stub_dir / "codex-calls.jsonl")
        self.assertEqual(len(codex_calls), 1)
        self.assertEqual(codex_calls[0]["argv"][:1], ["stop"])
        self.assertIn(str(self.codex_dir / "02.json"), codex_calls[0]["argv"])
        killed = [
            call["argv"][-1]
            for call in self.fixture.tmux_calls()
            if call["argv"][:1] == ["kill-window"]
        ]
        self.assertEqual(
            sorted(killed), sorted([MERGED_WINDOW, UNMERGED_WINDOW, DASHBOARD_WINDOW])
        )

    def test_repeated_launch_record_does_not_repeat_destructive_operations(self):
        last_launch = self.log_path.read_text().splitlines()[-1]
        self.log_path.write_text(self.log_path.read_text() + last_launch + "\n")

        result = self.run_clear("y")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.unmerged_worktree.exists())
        self.assertNotIn(UNMERGED_BRANCH, git(
            self.fixture.repo, "branch", "--format=%(refname:short)"
        ).stdout)
        codex_calls = read_json_lines(self.fixture.stub_dir / "codex-calls.jsonl")
        self.assertEqual(len(codex_calls), 1)


if __name__ == "__main__":
    unittest.main()
