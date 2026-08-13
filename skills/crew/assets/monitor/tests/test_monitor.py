#!/usr/bin/env python3
"""Drive the monitor's operator surface from its command line against fixture receipts.

Every fixture is a real git repository with a worktree per ticket, a machine log written by hand
in the schema `docs/machine-log.md` publishes, and a stub PATH carrying `claude` and `tmux`.
Assertions are on external behaviour only — the table the pane draws, the toasts tmux was asked to
display, the verdict line, the log lines that follow it, and the exit code.
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
MONITOR = TESTS_DIR.parent / "monitor.py"

# One wave, stamped in the run's one timestamp format, so every elapsed time below is arithmetic
# a reader can check: 09:00:00 to 09:12:31 is twelve minutes and thirty-one seconds.
WAVE = 1
LAUNCH_TS = "2026-08-13T09:00:00Z"
NOW_TS = "2026-08-13T09:12:31Z"
LIVE_ELAPSED = "00:12:31"
SETTLED_TS = "2026-08-13T09:41:07Z"
SETTLED_ELAPSED = "00:41:07"

CHILDREN = {"06": "crew-06-dispatch", "07": "crew-07-log"}
MODEL = "claude-opus-4-5-20251101"

# The guard assets the dispatch renderer installs into every Claude worktree before its child
# starts; the child never commits them, so they are not what makes a tree dirty.
GUARD_ASSETS = ("red-line.sh", "worktree-guard.sh", "settings.local.json")


def run_git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


class Fixture:
    """A temporary run: a repository, a worktree per ticket, a machine log, and a stub PATH."""

    def __init__(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.repo = self.root / "repo"
        self.repo.mkdir()
        run_git(self.repo, "init", "-b", "main")
        run_git(self.repo, "config", "user.email", "crew@example.invalid")
        run_git(self.repo, "config", "user.name", "Crew Test")
        (self.repo / "README.md").write_text("fixture\n")
        run_git(self.repo, "add", "README.md")
        run_git(self.repo, "commit", "-m", "base")
        self.base_commit = run_git(self.repo, "rev-parse", "HEAD")

        self.log = self.root / "run" / "machine-log.jsonl"
        self.log.parent.mkdir()
        self.toast_state = self.root / "run" / "toasts.json"

        self.stub_dir = self.root / "stub"
        self.stub_dir.mkdir()
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self._link_stub("claude", "stub_claude.py")
        self._link_stub("tmux", "stub_tmux.py")

        self.worktrees = {}

    def _link_stub(self, name, script):
        target = self.bin_dir / name
        target.write_text(
            "#!/bin/sh\nexec %s %s \"$@\"\n" % (sys.executable, TESTS_DIR / script)
        )
        target.chmod(0o755)

    def worktree(self, ticket, commits=1):
        """The ticket's worktree, cut from the base commit and carrying `commits` of its own."""
        path = self.root / "worktrees" / f"worktree-{ticket}"
        run_git(self.repo, "worktree", "add", "-b", f"worktree-{ticket}", str(path),
                self.base_commit)
        for number in range(commits):
            (path / f"work-{number}.txt").write_text(f"{ticket} {number}\n")
            run_git(path, "add", f"work-{number}.txt")
            run_git(path, "commit", "-m", f"{ticket} work {number}")
        self.worktrees[ticket] = path
        return path

    def unrelated_commit(self):
        """A commit in the same repository that shares no history with any ticket branch."""
        path = self.root / "unrelated"
        run_git(self.repo, "worktree", "add", "--detach", str(path), self.base_commit)
        run_git(path, "checkout", "--orphan", "unrelated")
        run_git(path, "rm", "-rf", ".")
        (path / "elsewhere.txt").write_text("another history\n")
        run_git(path, "add", "elsewhere.txt")
        run_git(path, "commit", "-m", "unrelated")
        return run_git(path, "rev-parse", "HEAD")

    def install_guard_assets(self, ticket):
        target = self.worktrees[ticket] / ".claude"
        target.mkdir(parents=True, exist_ok=True)
        for name in GUARD_ASSETS:
            (target / name).write_text("{}\n")

    def head(self, ticket):
        return run_git(self.worktrees[ticket], "rev-parse", "HEAD")

    def append(self, ts, event, **fields):
        record = {"ts": ts, "event": event}
        record.update(fields)
        with self.log.open("a") as handle:
            handle.write(json.dumps(record) + "\n")

    def launch(self, ticket, ts=LAUNCH_TS):
        self.append(
            ts, "launch", ticket=ticket, child=CHILDREN[ticket], workflow="tdd",
            executor="claude", model=MODEL, effort="medium",
            branch=f"worktree-{ticket}", worktree=str(self.worktrees[ticket]),
            window=f"@{ticket}",
        )

    def log_lines(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines() if line]

    def agents(self, entries):
        """The agents list `claude agents --json` answers with, keyed by ticket."""
        (self.stub_dir / "agents.json").write_text(json.dumps([
            {
                "pid": 4000 + index,
                "cwd": str(self.worktrees[ticket]),
                "kind": "interactive",
                "sessionId": f"session-{ticket}",
                "name": CHILDREN[ticket],
                "status": status,
            }
            for index, (ticket, status) in enumerate(entries.items())
        ]))

    def environment(self):
        environment = dict(os.environ)
        environment["PATH"] = f"{self.bin_dir}{os.pathsep}{environment['PATH']}"
        environment["AGENTCREW_STUB_DIR"] = str(self.stub_dir)
        return environment

    def run_monitor(self, *args):
        return subprocess.run(
            [sys.executable, str(MONITOR), *[str(argument) for argument in args]],
            capture_output=True, text=True, env=self.environment(),
        )

    def start_monitor(self, *args):
        return subprocess.Popen(
            [sys.executable, str(MONITOR), *[str(argument) for argument in args]],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=self.environment(),
        )

    def dashboard(self, *extra, tickets=("06", "07")):
        return self.run_monitor(
            "dashboard", "--log", self.log, "--wave", WAVE, "--now", NOW_TS,
            "--toast-state", self.toast_state, *extra,
            *[self.worktrees[ticket] for ticket in tickets],
        )

    def calls(self, name):
        path = self.stub_dir / f"{name}-calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line)["argv"] for line in path.read_text().splitlines() if line]

    def toasts(self):
        return [argv[-1] for argv in self.calls("tmux") if argv[:1] == ["display-message"]]

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


def rows(output):
    """The dashboard's data rows, each split into its six fields."""
    lines = output.splitlines()
    header = next(index for index, line in enumerate(lines) if line.startswith("WAVE"))
    return [line.split() for line in lines[header + 1:] if line.strip()]


class MonitorTestCase(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()
        self.addCleanup(self.fixture.cleanup)


class ReceiptVerificationTests(MonitorTestCase):
    def verify(self, ticket, sha, base=None, log=True):
        worktree = self.fixture.worktrees[ticket]
        arguments = [
            "verify", "--ticket", ticket, "--worktree", worktree, "--sha", sha,
            "--base", base or self.fixture.base_commit,
        ]
        if log:
            arguments += ["--log", self.fixture.log]
        return self.fixture.run_monitor(*arguments)

    def test_a_receipt_matching_the_worktree_head_is_landable_and_logged(self):
        self.fixture.worktree("06")
        head = self.fixture.head("06")

        result = self.verify("06", head)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"06 landable {head}")
        self.assertEqual(
            [(line["event"], line.get("ticket"), line.get("verdict"), line.get("sha"))
             for line in self.fixture.log_lines()],
            [("receipt", "06", "landable", head)],
        )

    def test_a_receipt_whose_tail_is_invented_is_invalid(self):
        self.fixture.worktree("06")
        head = self.fixture.head("06")
        invented = head[:7] + ("0" * 33 if head[7] != "0" else "1" * 33)

        result = self.verify("06", invented)

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertTrue(result.stdout.startswith("06 invalid"), result.stdout)
        self.assertIn("head", result.stdout)
        self.assertEqual(self.fixture.log_lines(), [])

    def test_a_receipt_shorter_than_forty_characters_is_invalid(self):
        self.fixture.worktree("06")

        result = self.verify("06", self.fixture.head("06")[:7])

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertTrue(result.stdout.startswith("06 invalid"), result.stdout)
        self.assertEqual(self.fixture.log_lines(), [])

    def test_a_branch_no_commits_ahead_of_its_base_is_invalid(self):
        self.fixture.worktree("06", commits=0)

        result = self.verify("06", self.fixture.head("06"))

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("ahead", result.stdout)
        self.assertEqual(self.fixture.log_lines(), [])

    def test_work_left_uncommitted_in_the_worktree_is_invalid(self):
        worktree = self.fixture.worktree("06")
        (worktree / "unfinished.txt").write_text("half a feature\n")

        result = self.verify("06", self.fixture.head("06"))

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("unfinished.txt", result.stdout)
        self.assertEqual(self.fixture.log_lines(), [])

    def test_the_installed_guard_assets_do_not_make_a_worktree_dirty(self):
        self.fixture.worktree("06")
        self.fixture.install_guard_assets("06")
        head = self.fixture.head("06")

        result = self.verify("06", head)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), f"06 landable {head}")

    def test_work_renamed_onto_a_guard_asset_path_is_still_uncommitted(self):
        worktree = self.fixture.worktree("06")
        (worktree / ".claude").mkdir()
        run_git(worktree, "mv", "work-0.txt", ".claude/red-line.sh")

        result = self.verify("06", self.fixture.head("06"))

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn(".claude/red-line.sh", result.stdout)
        self.assertEqual(self.fixture.log_lines(), [])

    def test_a_base_the_branch_never_grew_from_is_invalid(self):
        self.fixture.worktree("06")

        result = self.verify("06", self.fixture.head("06"), base=self.fixture.unrelated_commit())

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("descend", result.stdout)
        self.assertEqual(self.fixture.log_lines(), [])

    def test_a_worktree_that_is_not_a_repository_is_a_monitor_error(self):
        worktree = self.fixture.root / "not-a-repo"
        worktree.mkdir()
        self.fixture.worktrees["06"] = worktree

        result = self.verify("06", "0" * 40)

        self.assertEqual(result.returncode, 3, result.stdout)
        self.assertIn("MONITOR ERROR", result.stderr)
        self.assertEqual(self.fixture.log_lines(), [])


class DashboardTests(MonitorTestCase):
    def test_one_row_per_launched_ticket_carries_the_wave_ticket_child_state_and_elapsed(self):
        self.fixture.worktree("06")
        self.fixture.worktree("07")
        self.fixture.launch("06")
        self.fixture.launch("07")
        self.fixture.agents({"06": "busy", "07": "waiting"})

        result = self.fixture.dashboard()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("LAST EVENT", result.stdout)
        self.assertEqual(
            rows(result.stdout),
            [
                ["1", "06", CHILDREN["06"], "busy", "launch", LIVE_ELAPSED],
                ["1", "07", CHILDREN["07"], "waiting", "launch", LIVE_ELAPSED],
            ],
        )

    def test_a_settled_ticket_shows_its_verdict_and_stops_its_clock(self):
        self.fixture.worktree("06")
        self.fixture.launch("06")
        self.fixture.append(SETTLED_TS, "receipt", ticket="06", verdict="landable",
                            sha=self.fixture.head("06"))
        self.fixture.agents({"06": "idle"})

        result = self.fixture.dashboard(tickets=("06",))

        self.assertEqual(
            rows(result.stdout),
            [["1", "06", CHILDREN["06"], "landable", "receipt", SETTLED_ELAPSED]],
        )

    def test_a_child_missing_from_the_agents_list_is_vanished(self):
        self.fixture.worktree("06")
        self.fixture.worktree("07")
        self.fixture.launch("06")
        self.fixture.launch("07")
        self.fixture.agents({"06": "busy"})

        result = self.fixture.dashboard()

        self.assertEqual(
            [row[1:4] for row in rows(result.stdout)],
            [["06", CHILDREN["06"], "busy"], ["07", CHILDREN["07"], "vanished"]],
        )

    def test_an_unreadable_agents_list_draws_unknown_and_keeps_the_pane(self):
        self.fixture.worktree("06")
        self.fixture.launch("06")

        result = self.fixture.dashboard(tickets=("06",))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(rows(result.stdout)[0][3], "unknown")

    def test_a_worktree_with_no_launch_is_not_a_row(self):
        self.fixture.worktree("06")
        self.fixture.worktree("07")
        self.fixture.launch("06")
        self.fixture.agents({"06": "busy", "07": "busy"})

        result = self.fixture.dashboard()

        self.assertEqual([row[1] for row in rows(result.stdout)], ["06"])

    def test_the_pane_redraws_as_the_log_changes(self):
        self.fixture.worktree("06")
        self.fixture.launch("06")
        self.fixture.agents({"06": "busy"})
        process = self.fixture.start_monitor(
            "dashboard", "--log", self.fixture.log, "--wave", WAVE, "--now", NOW_TS,
            "--toast-state", self.fixture.toast_state, "--refresh", "0.05",
            self.fixture.worktrees["06"],
        )
        self.addCleanup(process.kill)

        time.sleep(0.5)
        self.fixture.append(SETTLED_TS, "receipt", ticket="06", verdict="landable",
                            sha=self.fixture.head("06"))
        time.sleep(0.5)
        process.terminate()
        output = process.communicate(timeout=10)[0]

        self.assertIn("busy", output)
        self.assertIn("landable", output)
        self.assertGreater(output.count(CHILDREN["06"]), 1)


class ToastTests(MonitorTestCase):
    def test_a_stuck_child_a_vanished_child_and_an_escalation_each_toast(self):
        self.fixture.worktree("06")
        self.fixture.worktree("07")
        self.fixture.launch("06")
        self.fixture.launch("07")
        self.fixture.append(NOW_TS, "escalation", ticket="06", role="child",
                            message="CREW ASK 06 stuck — ts=1755060042")
        self.fixture.agents({"06": "waiting"})

        self.fixture.dashboard()

        self.assertEqual(
            sorted(self.fixture.toasts()),
            sorted([
                "crew 06 stuck at a permission prompt",
                "crew 06 escalated",
                "crew 07 vanished",
            ]),
        )

    def test_a_wave_whose_every_ticket_is_settled_toasts_once(self):
        self.fixture.worktree("06")
        self.fixture.worktree("07")
        self.fixture.launch("06")
        self.fixture.launch("07")
        self.fixture.append(SETTLED_TS, "receipt", ticket="06", verdict="landable",
                            sha=self.fixture.head("06"))
        self.fixture.append(SETTLED_TS, "receipt", ticket="07", verdict="failed")
        self.fixture.agents({})

        self.fixture.dashboard()
        self.fixture.dashboard()

        self.assertEqual(self.fixture.toasts(), ["crew wave 1 complete"])

    def test_an_unfinished_wave_does_not_toast_complete(self):
        self.fixture.worktree("06")
        self.fixture.worktree("07")
        self.fixture.launch("06")
        self.fixture.launch("07")
        self.fixture.append(SETTLED_TS, "receipt", ticket="06", verdict="landable",
                            sha=self.fixture.head("06"))
        self.fixture.agents({"07": "busy"})

        self.fixture.dashboard()

        self.assertEqual(self.fixture.toasts(), [])

    def test_an_exception_is_not_toasted_twice(self):
        self.fixture.worktree("06")
        self.fixture.launch("06")
        self.fixture.agents({"06": "waiting"})

        self.fixture.dashboard(tickets=("06",))
        self.fixture.dashboard(tickets=("06",))

        self.assertEqual(self.fixture.toasts(), ["crew 06 stuck at a permission prompt"])

    def test_nothing_the_monitor_emits_reaches_the_coordinator(self):
        self.fixture.worktree("06")
        self.fixture.launch("06")
        self.fixture.agents({"06": "waiting"})

        self.fixture.dashboard(tickets=("06",))

        self.assertEqual(self.fixture.calls("claude"), [["agents", "--json"]])
        self.assertTrue(
            all(argv[:1] == ["display-message"] for argv in self.fixture.calls("tmux")),
            self.fixture.calls("tmux"),
        )


class PaneTests(MonitorTestCase):
    def test_the_pane_runs_the_dashboard_refresh_loop_in_the_run_session(self):
        self.fixture.worktree("06")
        self.fixture.launch("06")

        result = self.fixture.run_monitor(
            "pane", "--session", "$7:", "--log", self.fixture.log, "--wave", WAVE,
            self.fixture.worktrees["06"],
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "%9")
        argv = self.fixture.calls("tmux")[0]
        self.assertEqual(argv[0], "split-window")
        self.assertIn("$7:", argv)
        command = argv[-1]
        self.assertIn("dashboard", command)
        self.assertIn("--refresh", command)
        self.assertIn(str(self.fixture.log), command)
        self.assertIn(str(self.fixture.worktrees["06"]), command)


if __name__ == "__main__":
    unittest.main()
