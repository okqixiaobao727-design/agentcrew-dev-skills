#!/usr/bin/env python3
"""What a start does about the runs abandoned in the repo before it, and what a killed driver
leaves behind it.

A run that reaches its own end cleans up after itself. A run that never gets there — an unresumed
pause, a killed driver — leaves its hook in the repo's settings and its landed worktrees on disk,
and its wake monitors polling for a reader that is gone. Both are observed here from outside: the
repo's settings file, the repo's worktrees and branches, and the operating system's own answer
about which processes are still running.
"""

import json
import os
import pathlib
import subprocess
import sys
import unittest

from test_driver import BASE_BRANCH, DRIVER, DriverTestCase, git


MACHINE_LOG = DRIVER.parents[1] / "machine_log.py"
SETTINGS_PATH = pathlib.Path(".claude") / "settings.local.json"
LANDED_TICKET = "01"
PARKED_TICKET = "02"


def dead_pid():
    """A pid that has certainly gone: a process started here and then reaped."""
    process = subprocess.Popen([sys.executable, "-c", ""])
    process.wait()
    return process.pid


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class SeededRun:
    """One run recorded in a repo the way a real one records itself, and abandoned there.

    Its feature directory, run directory, integration branch, two children — one landed, one
    parked — and the coordinator hook a start installs in the repo's settings. Nothing here is the
    code under test: the records are written so a later start meets exactly what an abandoned run
    leaves.
    """

    def __init__(self, fixture, name, coordinator_pid):
        self.fixture = fixture
        self.name = name
        self.repo = fixture.repo
        self.feature_dir = fixture.repo / "features" / name
        self.feature_dir.mkdir(parents=True)
        self.run_dir = self.feature_dir / ".crew"
        self.run_dir.mkdir()
        self.log_path = self.run_dir / "log.jsonl"
        self.integration_branch = f"crew/{name}"

        git(self.repo, "branch", self.integration_branch, BASE_BRANCH)
        self.landed_worktree = self.worktree(f"{name}-landed", f"{name}/landed", "landed\n")
        self.merge_into_integration(f"{name}/landed")
        self.parked_worktree = self.worktree(f"{name}-parked", f"{name}/parked", "parked\n")

        (self.run_dir / "wave-table.json").write_text(json.dumps({
            "run": {
                "repo_root": str(self.repo),
                "spec_path": str(self.feature_dir / "spec.md"),
                "integration_branch": self.integration_branch,
                "integration_base_commit": git(
                    self.repo, "rev-parse", BASE_BRANCH
                ).stdout.strip(),
                "coordinator_name": f"coordinator-{name}",
                "coordinator_pid": coordinator_pid,
                "crew_skill_dir": str(DRIVER.parents[1]),
                "tmux_session": "$7:",
                "permission_mode": "acceptEdits",
                "base_branch": BASE_BRANCH,
                "return_branch": BASE_BRANCH,
                "feature_dir": str(self.feature_dir),
                "codex": {"state_dir": str(self.run_dir / "codex")},
            },
            "waves": [{"wave": 1, "tickets": [
                {"id": LANDED_TICKET, "title": "landed", "blocked_by": []},
                {"id": PARKED_TICKET, "title": "parked", "blocked_by": []},
            ]}],
        }) + "\n")
        self.log_path.write_text("".join(
            json.dumps(record) + "\n" for record in [
                {
                    "ts": "2026-08-15T00:00:00Z", "event": "launch", "ticket": LANDED_TICKET,
                    "executor": "claude", "branch": f"{name}/landed",
                    "worktree": str(self.landed_worktree),
                },
                {
                    "ts": "2026-08-15T00:00:01Z", "event": "launch", "ticket": PARKED_TICKET,
                    "executor": "claude", "branch": f"{name}/parked",
                    "worktree": str(self.parked_worktree),
                },
                {
                    "ts": "2026-08-15T00:00:02Z", "event": "merge", "ticket": LANDED_TICKET,
                    "result": "clean",
                },
            ]
        ))
        self.install_hook()

    def worktree(self, directory, branch, text):
        """One child's worktree with one commit on its own branch, outside the repo's own tree."""
        path = self.fixture.root / "worktrees" / directory
        git(self.repo, "worktree", "add", "-b", branch, str(path), BASE_BRANCH)
        (path / f"{branch.replace('/', '-')}.txt").write_text(text)
        git(path, "add", "-A")
        git(path, "commit", "-m", f"{branch} work")
        return path

    def merge_into_integration(self, branch):
        """Land that branch on this run's integration branch, as a merged ticket's is."""
        current = self.fixture.current_branch()
        git(self.repo, "switch", self.integration_branch)
        git(self.repo, "merge", "--no-ff", branch, "-m", f"merge {branch}")
        git(self.repo, "switch", current)

    def install_hook(self):
        """Register this run's coordinator hook in the repo's settings, as its start did."""
        result = subprocess.run(
            [
                sys.executable, str(MACHINE_LOG), "--log", str(self.log_path), "install",
                "--settings", str(self.repo / SETTINGS_PATH), "--role", "coordinator",
            ],
            capture_output=True, text=True, env=self.fixture.environment(),
            cwd=str(self.repo),
        )
        assert result.returncode == 0, result.stderr

    def branches(self):
        return self.fixture.branches()


class SweepTestCase(DriverTestCase):
    """A start in a repo that already carries other runs' records."""

    def hook_commands(self):
        settings = self.fixture.settings(self.fixture.repo / SETTINGS_PATH)
        return [
            hook.get("command", "")
            for block in settings.get("hooks", {}).get("PostToolUse", [])
            for hook in block.get("hooks", [])
        ]

    def hook_logs(self):
        """The log path each registered hook writes to, which is the run it belongs to."""
        return [
            command.split("--log", 1)[1].split()[0]
            for command in self.hook_commands() if "--log" in command
        ]

    def start_a_run(self):
        """Start a real run in this repo's own feature and leave its loop running."""
        self.fixture.ticket(LANDED_TICKET, "first thing")
        self.fixture.commit_feature()
        return self.fixture.launch()

    def start_and_end(self):
        """Start a run, let its loop go, and read what the whole start printed.

        The sweep's warnings go to stderr before anything else happens, so what this asserts on is
        the driver's own output rather than a file it happened to touch.
        """
        process = self.start_a_run()
        self.assertTrue(
            self.fixture.wait_for(
                lambda: (self.fixture.run_dir / "wave-table.json").exists()
                or process.poll() is not None
            ),
            "the start neither ran nor ended",
        )
        self.fixture.stop_monitors()
        process.terminate()
        return self.fixture.ended(process)

    def add_hook(self, command):
        """Register one hook in the repo's settings the way any other tool would."""
        settings_path = self.fixture.repo / SETTINGS_PATH
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings = self.fixture.settings(settings_path)
        blocks = settings.setdefault("hooks", {}).setdefault("PostToolUse", [])
        blocks.append({
            "matcher": "SendMessage",
            "hooks": [{"type": "command", "command": command}],
        })
        settings_path.write_text(json.dumps(settings, indent=2) + "\n")


class HookSweepTests(SweepTestCase):
    def test_one_start_removes_a_dead_runs_hook_and_leaves_a_live_runs_alone(self):
        dead = SeededRun(self.fixture, "dead", dead_pid())
        live = SeededRun(self.fixture, "live", os.getpid())

        self.start_a_run()

        self.assertTrue(
            self.fixture.wait_for(lambda: str(dead.log_path) not in self.hook_logs()),
            f"the dead run's hook was not swept: {self.hook_commands()}",
        )
        self.assertIn(str(live.log_path), self.hook_logs())
        self.assertTrue(
            self.fixture.wait_for(
                lambda: str(self.fixture.run_dir / "log.jsonl") in self.hook_logs()
            ),
            f"the new run installed no hook of its own: {self.hook_commands()}",
        )

    def test_a_sweep_problem_warns_and_the_run_still_starts(self):
        dead = SeededRun(self.fixture, "dead", dead_pid())
        dead.log_path.write_text("not json at all\n")

        result = self.start_and_end()

        self.assertIn(str(dead.run_dir), result.stderr)

    def test_a_launch_record_of_the_wrong_shape_warns_and_the_run_still_starts(self):
        """A record no reader here anticipated is still a dead run's record, not this run's
        problem."""
        dead = SeededRun(self.fixture, "dead", dead_pid())
        records = [json.loads(line) for line in dead.log_path.read_text().splitlines()]
        records[0]["worktree"] = {"not": "a path"}
        dead.log_path.write_text("".join(json.dumps(record) + "\n" for record in records))

        result = self.start_and_end()

        self.assertIn(str(dead.run_dir), result.stderr)

    def foreign_hook(self, script, log):
        """One hook of somebody else's, in the shape of ours but run by another script."""
        directory = self.fixture.root / "foreign"
        return (
            f"python3 {directory / script} --log {directory / log}"
            f" hook --role coordinator --scope {self.fixture.repo}"
        )

    def test_a_hook_this_project_did_not_install_is_left_alone(self):
        """The `--log` a command carries does not make it ours; the script it runs does — and a
        `--log` that merely points at a file called `machine_log.py` is still somebody else's."""
        foreign = [
            self.foreign_hook("writer.py", "log.jsonl"),
            self.foreign_hook("writer.py", "machine_log.py"),
        ]
        for command in foreign:
            self.add_hook(command)

        self.start_a_run()

        self.assertTrue(
            self.fixture.wait_for(
                lambda: str(self.fixture.run_dir / "log.jsonl") in self.hook_logs()
            ),
            "the run never installed its own hook, so the sweep cannot be read off this file",
        )
        for command in foreign:
            self.assertIn(command, self.hook_commands())

    def test_an_unreadable_settings_file_warns_rather_than_sweeping_it(self):
        settings_path = self.fixture.repo / SETTINGS_PATH
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text("{ this is not json")

        result = self.start_and_end()

        self.assertIn(str(settings_path), result.stderr)


class WorktreeSweepTests(SweepTestCase):
    def test_the_sweep_clears_a_dead_runs_landed_worktree_and_keeps_its_parked_one(self):
        dead = SeededRun(self.fixture, "dead", dead_pid())

        self.start_a_run()

        # Waited for on the branch, which the clearing code deletes after the worktree it stood
        # in: a wait that ended at the directory would read the branches mid-sweep.
        self.assertTrue(
            self.fixture.wait_for(lambda: "dead/landed" not in self.fixture.branches()),
            f"the landed branch was kept: {self.fixture.branches()}",
        )
        self.assertFalse(dead.landed_worktree.exists())
        self.assertTrue(dead.parked_worktree.exists())
        self.assertIn("dead/parked", self.fixture.branches())
        self.assertIn(dead.integration_branch, self.fixture.branches())

    def test_a_dead_run_recording_another_repository_keeps_its_worktrees(self):
        """This repo's settings are this driver's to edit; another repository's git is not."""
        dead = SeededRun(self.fixture, "dead", dead_pid())
        table_path = dead.run_dir / "wave-table.json"
        table = json.loads(table_path.read_text())
        table["run"]["repo_root"] = str(self.fixture.root / "somebody-elses-repo")
        table_path.write_text(json.dumps(table) + "\n")

        self.start_a_run()

        self.assertTrue(
            self.fixture.wait_for(lambda: str(dead.log_path) not in self.hook_logs()),
            f"the dead run's hook was not swept: {self.hook_commands()}",
        )
        self.assertTrue(dead.landed_worktree.exists())
        self.assertIn("dead/landed", self.fixture.branches())

    def test_a_run_whose_driver_outlived_its_coordinator_is_not_swept(self):
        """Detachment's own consequence (#103): a driver goes on running after the session that
        started it has gone, and a run it is still merging is as live as any other."""
        orphaned = SeededRun(self.fixture, "orphaned", dead_pid())
        (orphaned.run_dir / "driver.pid").write_text(f"{os.getpid()}\n")

        result = self.start_and_end()

        self.assertIn(str(orphaned.log_path), self.hook_logs())
        self.assertTrue(orphaned.landed_worktree.exists())
        self.assertIn("orphaned/landed", self.fixture.branches())
        self.assertNotIn(str(orphaned.run_dir), result.stderr)

    def test_a_run_whose_driver_is_gone_too_is_swept(self):
        """The record is not a reprieve of its own: it names a process that is not running."""
        abandoned = SeededRun(self.fixture, "abandoned", dead_pid())
        (abandoned.run_dir / "driver.pid").write_text(f"{dead_pid()}\n")

        self.start_a_run()

        self.assertTrue(
            self.fixture.wait_for(lambda: str(abandoned.log_path) not in self.hook_logs()),
            f"the abandoned run's hook was not swept: {self.hook_commands()}",
        )

    def test_a_live_runs_landed_worktree_is_left_where_it_is(self):
        live = SeededRun(self.fixture, "live", os.getpid())

        self.start_a_run()

        self.assertTrue(
            self.fixture.wait_for(
                lambda: (self.fixture.run_dir / "wave-table.json").exists()
            ),
            "the run never started",
        )
        self.assertTrue(live.landed_worktree.exists())
        self.assertIn("live/landed", self.fixture.branches())


class MonitorLifetimeTests(DriverTestCase):
    """A wake monitor's life is its driver's: `kill -9` leaves no `disarm` behind."""

    def monitor_pids(self):
        """Every wake monitor watching this run, found by the parked-path file it carries."""
        listed = subprocess.run(
            ["ps", "-eo", "pid=,args="], capture_output=True, text=True
        ).stdout
        marker = str(self.fixture.run_dir / "parked-paths")
        return [
            int(line.split(None, 1)[0]) for line in listed.splitlines()
            if "monitor-wave.sh" in line and marker in line
        ]

    def test_a_killed_driver_takes_its_wake_monitors_with_it(self):
        self.fixture.ticket(LANDED_TICKET, "first thing")
        self.fixture.commit_feature()
        process = self.fixture.launch()
        self.assertTrue(self.fixture.wait_for_snapshot(), "no monitor was ever armed")
        self.assertTrue(
            self.fixture.wait_for(lambda: bool(self.monitor_pids())),
            "no wake monitor process was found",
        )
        armed = self.monitor_pids()

        process.kill()
        process.wait()

        self.assertTrue(
            self.fixture.wait_for(lambda: not any(alive(pid) for pid in armed)),
            f"a wake monitor outlived its killed driver: {armed}",
        )


if __name__ == "__main__":
    unittest.main()
