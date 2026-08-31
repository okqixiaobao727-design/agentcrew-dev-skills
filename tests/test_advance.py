#!/usr/bin/env python3
"""Behaviour of the wave-advance driver, driven from its command line against a stubbed run.

Every fixture is a real git repository holding a three-wave table, a machine log carrying the
receipts the monitor would have written, and a stub PATH. What is asserted is external only: the
git graph the wave left behind, the lines the machine log gained, the next Wave returned to the
Driver, and the exit code.

Advance lands a Wave through the real merge driver. Driver activation owns every launch
(ADR-0024), so this suite proves advance does not cross that boundary.
"""

import json
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import time
import unittest


PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[1]
ASSETS = PLUGIN_ROOT / "skills" / "crew" / "assets"
ADVANCE = ASSETS / "advance.py"
MACHINE_LOG = ASSETS / "machine_log.py"
CREW_SKILL_DIR = ASSETS.parent
DISPATCH_STUBS = ASSETS / "dispatch" / "tests"

# The run's routing, as the approved table carries it: full model IDs, never aliases (ADR-0003).
MODEL = "claude-opus-4-5-20251101"
EFFORT = "medium"
REVIEW = {"vendor": "codex", "model": "gpt-5.6-luna", "effort": "max"}
REPAIR_MODEL = "claude-sonnet-4-5-20250929"

COORDINATOR_NAME = "crew-coordinator-1f"
COORDINATOR_PID = 1504
PERMISSION_MODE = "acceptEdits"
TMUX_SESSION = "$7:"
INTEGRATION_BRANCH = "crew/demo"
FEATURE = "features/demo"
SHARED = "notes.md"
SHARED_BASE = "one\ntwo\nthree\n"

# The dependency graph the table carries: three waves, and one edge per ticket into the next wave.
# 11 is a descendant of 06 only through 08, which is what makes it the transitive case.
WAVES = {
    1: [("06", "dispatch-renderer", []), ("07", "machine-log", [])],
    2: [("08", "monitor-dashboard", ["06"]), ("09", "merge-driver", ["07"])],
    3: [("11", "skill-body", ["08"])],
}

INTERRUPT_EXIT = 130


def run_git(repo, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=check
    )


def git_out(repo, *args):
    return run_git(repo, *args).stdout.strip()


class Fixture:
    """A temporary run: a repository, a three-wave table over it, a machine log, and a stub PATH."""

    def __init__(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.repo = self.root / "repo"
        self.repo.mkdir()
        run_git(self.repo, "init", "-b", "main")
        run_git(self.repo, "config", "user.email", "crew@example.invalid")
        run_git(self.repo, "config", "user.name", "Crew Test")
        self.feature_dir = self.repo / FEATURE
        self.feature_dir.mkdir(parents=True)
        self.spec_path = self.feature_dir / "spec.md"
        self.spec_path.write_text("# spec\n")
        for tickets in WAVES.values():
            for number, slug, _ in tickets:
                (self.feature_dir / f"{number}-{slug}.md").write_text(f"# {number} {slug}\n")
        (self.repo / SHARED).write_text(SHARED_BASE)
        run_git(self.repo, "add", "-A")
        run_git(self.repo, "commit", "-m", "base")
        self.base_commit = git_out(self.repo, "rev-parse", "HEAD")
        run_git(self.repo, "checkout", "-b", INTEGRATION_BRANCH)

        self.log = self.root / "run" / "machine-log.jsonl"
        self.log.parent.mkdir()
        self.out_dir = self.root / "render"
        self.stub_dir = self.root / "stub"
        self.stub_dir.mkdir()
        self.config_dir = self.root / "claude-config"
        self.config_dir.mkdir()
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self._link_stub("claude", "stub_claude.py")
        self._link_stub("tmux", "stub_tmux.py")

    def _link_stub(self, name, script):
        target = self.bin_dir / name
        target.write_text(
            "#!/bin/sh\nexec %s %s \"$@\"\n" % (sys.executable, DISPATCH_STUBS / script)
        )
        target.chmod(0o755)

    # --- the run's state -------------------------------------------------------------------

    def work(self, number, slug, text=None):
        """One commit on a ticket branch, cut from the integration branch as dispatch cuts it."""
        branch = f"worktree-{number}-{slug}"
        run_git(self.repo, "checkout", "-b", branch, INTEGRATION_BRANCH)
        (self.repo / f"{number}.md").write_text(text or f"work on {number}\n")
        run_git(self.repo, "add", "-A")
        run_git(self.repo, "commit", "-m", f"work on {number}")
        head = git_out(self.repo, "rev-parse", "HEAD")
        run_git(self.repo, "checkout", INTEGRATION_BRANCH)
        return head

    def conflicting_work(self, number, slug):
        """A ticket branch that rewrites a line the integration branch also rewrote."""
        branch = f"worktree-{number}-{slug}"
        run_git(self.repo, "checkout", "-b", branch, INTEGRATION_BRANCH)
        (self.repo / SHARED).write_text("one\nTHEIRS\nthree\n")
        run_git(self.repo, "add", "-A")
        run_git(self.repo, "commit", "-m", f"work on {number}")
        head = git_out(self.repo, "rev-parse", "HEAD")
        run_git(self.repo, "checkout", INTEGRATION_BRANCH)
        (self.repo / SHARED).write_text("one\nOURS\nthree\n")
        run_git(self.repo, "add", "-A")
        run_git(self.repo, "commit", "-m", "integration moves too")
        return head

    def launched(self, number, slug):
        """The `launch` event the dispatch renderer writes when a child starts on a ticket."""
        self.log_event(
            "launch", "--ticket", number, "--child", f"crew-{number}",
            "--workflow", "tdd", "--executor", "claude",
            "--model", MODEL, "--effort", EFFORT,
            "--worktree", str(self.worktree(number, slug)),
        )

    def receipt(self, number, verdict, sha=None):
        arguments = ["receipt", "--ticket", number, "--verdict", verdict]
        if sha:
            arguments += ["--sha", sha]
        self.log_event(*arguments)

    def log_event(self, *arguments):
        subprocess.run(
            [sys.executable, str(MACHINE_LOG), "--log", str(self.log), *arguments],
            check=True, capture_output=True,
        )

    def settled_ticket(self, number, slug, verdict="landable", text=None):
        """A launched ticket with a branch and the receipt its verdict earns."""
        self.launched(number, slug)
        head = self.work(number, slug, text) if verdict == "landable" else None
        self.receipt(number, verdict, head)
        return head

    # --- what the run is driven with -------------------------------------------------------

    def worktree(self, number, slug):
        return self.repo / ".claude" / "worktrees" / f"{number}-{slug}"

    def table(self):
        run = {
            "repo_root": str(self.repo),
            "crew_worktree": str(self.repo),
            "spec_path": str(self.spec_path),
            "integration_branch": INTEGRATION_BRANCH,
            "integration_base_commit": self.base_commit,
            "coordinator_name": COORDINATOR_NAME,
            "coordinator_pid": COORDINATOR_PID,
            "crew_skill_dir": str(CREW_SKILL_DIR),
            "tmux_session": TMUX_SESSION,
            "permission_mode": PERMISSION_MODE,
            "coordinator_config_home": str(self.config_dir),
            "repair_model": REPAIR_MODEL,
            "tracker": "github",
        }
        waves = [
            {
                "wave": wave,
                "tickets": [
                    {
                        "id": number,
                        "title": slug.replace("-", " "),
                        "path": str(self.feature_dir / f"{number}-{slug}.md"),
                        "workflow": "tdd",
                        "executor": "claude",
                        "model": MODEL,
                        "effort": EFFORT,
                        # Concrete on every row, as the driver leaves it: this run names no
                        # account, so every ticket takes the coordinator's own home (ADR-0014).
                        "account": str(self.config_dir),
                        "account_mode": "inherited",
                        "review": dict(REVIEW),
                        "blocked_by": list(blocked_by),
                    }
                    for number, slug, blocked_by in tickets
                ],
            }
            for wave, tickets in sorted(WAVES.items())
        ]
        path = self.root / "wave-table.json"
        path.write_text(json.dumps({"run": run, "waves": waves}))
        return path

    def command(self, wave):
        return [
            sys.executable, str(ADVANCE), "advance",
            "--table", str(self.table()),
            "--wave", str(wave),
            "--log", str(self.log),
            "--out-dir", str(self.out_dir),
            "--repair-model", REPAIR_MODEL,
        ]

    def environment(self):
        environment = dict(os.environ)
        environment["PATH"] = f"{self.bin_dir}{os.pathsep}{environment['PATH']}"
        environment["AGENTCREW_STUB_DIR"] = str(self.stub_dir)
        environment["CLAUDE_CONFIG_DIR"] = str(self.config_dir)
        return environment

    def advance(self, wave):
        return subprocess.run(
            self.command(wave), capture_output=True, text=True, env=self.environment()
        )

    # --- what the run left behind ----------------------------------------------------------

    def records(self, event=None):
        if not self.log.exists():
            return []
        records = [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]
        return [record for record in records if event is None or record.get("event") == event]

    def launches(self):
        path = self.stub_dir / "launches.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def tmux_calls(self):
        path = self.stub_dir / "tmux-calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def toasts(self):
        return [
            call["argv"][-1] for call in self.tmux_calls()
            if call["argv"][:1] == ["display-message"]
        ]

    def merged(self, sha):
        return sha in git_out(
            self.repo, "rev-list", INTEGRATION_BRANCH
        ).splitlines()

    def cleanup(self):
        subprocess.run(
            ["git", "-C", str(self.repo), "worktree", "prune"], capture_output=True
        )
        subprocess.run(["rm", "-rf", str(self.root)], capture_output=True)


class AdvanceTestCase(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()
        self.addCleanup(self.fixture.cleanup)

    def advance_events(self):
        return self.fixture.records("advance")

    def assertOneDecision(self, wave, decision):
        """Exactly one advancement decision was recorded, and it is this one."""
        events = self.advance_events()
        self.assertEqual(len(events), 1, events)
        self.assertEqual(events[0].get("wave"), str(wave), events[0])
        self.assertEqual(events[0].get("decision"), decision, events[0])
        return events[0]


class GreenWaveTests(AdvanceTestCase):
    """A green Wave lands and returns the following Wave to Driver activation."""

    def green_wave_one(self):
        return {
            number: self.fixture.settled_ticket(number, slug)
            for number, slug, _ in WAVES[1]
        }

    def test_a_green_wave_lands_and_returns_the_next_wave(self):
        heads = self.green_wave_one()

        result = self.fixture.advance(1)

        self.assertEqual(result.returncode, 0, result.stderr)
        for number, head in heads.items():
            self.assertTrue(self.fixture.merged(head), f"{number} did not land:\n{result.stdout}")
        self.assertIn("wave 2 ready 08, 09", result.stdout)
        self.assertEqual(self.fixture.launches(), [])

    def test_the_integration_branch_contains_every_landed_ticket_before_control_returns(self):
        heads = self.green_wave_one()

        self.fixture.advance(1)

        for landed in heads.values():
            self.assertTrue(self.fixture.merged(landed))
        for number, slug, _ in WAVES[2]:
            self.assertFalse(self.fixture.worktree(number, slug).exists())

    def test_advance_writes_no_launch_fact_for_the_wave_it_only_identified(self):
        self.green_wave_one()

        self.fixture.advance(1)

        started = [record for record in self.fixture.records("launch")
                   if record["ticket"] in ("08", "09")]
        self.assertEqual(started, [])

    def test_advance_does_not_toast_a_wave_the_driver_has_not_activated(self):
        self.green_wave_one()

        self.fixture.advance(1)

        self.assertEqual(self.fixture.toasts(), [])

    def test_advance_does_not_commit_the_wave_before_driver_activation(self):
        self.green_wave_one()

        self.fixture.advance(1)

        self.assertEqual(self.advance_events(), [])

    def test_advancing_costs_the_coordinator_nothing(self):
        """No model was run, and nothing was sent to or from the coordinator (ADR-0001)."""
        self.green_wave_one()

        self.fixture.advance(1)

        headless = [
            launch for launch in self.fixture.launches() if "--print" in launch["argv"]
        ]
        self.assertEqual(headless, [])
        for event in ("escalation", "ruling", "message"):
            self.assertEqual(self.fixture.records(event), [])

    def test_a_wave_already_advanced_past_is_not_advanced_past_twice(self):
        """A second run would start the next wave in the worktrees the first one is working in."""
        self.green_wave_one()
        self.fixture.advance(1)
        self.fixture.log_event(
            "advance", "--wave", "2", "--decision", "launched",
            "--detail", "Driver activation committed wave 2",
        )

        again = self.fixture.advance(1)

        self.assertNotEqual(again.returncode, 0)
        self.assertIn("wave 1", again.stderr)
        self.assertOneDecision(2, "launched")
        self.assertEqual(self.fixture.launches(), [])

    def test_a_run_that_escalated_may_be_advanced_again_once_it_is_settled(self):
        """A halt is where the coordinator rules; the run carries on from the same command."""
        self.fixture.settled_ticket("06", "dispatch-renderer", verdict="failed")
        self.fixture.settled_ticket("07", "machine-log")
        self.fixture.advance(1)
        self.fixture.receipt("06", "landable", self.fixture.work("06", "dispatch-renderer"))

        again = self.fixture.advance(1)

        self.assertEqual(again.returncode, 0, again.stderr + again.stdout)
        self.assertIn("wave 2 ready 08, 09", again.stdout)
        self.assertEqual(self.fixture.launches(), [])

    def test_a_resumed_wave_advances_after_its_tracker_outcomes_completed(self):
        """The ticket-104 incident: tracker close outcomes must not un-settle landed work."""
        for number, slug, _ in WAVES[2]:
            head = self.fixture.settled_ticket(number, slug)
            run_git(self.fixture.repo, "merge", "--no-ff", "--no-edit", head)
            self.fixture.log_event(
                "merge", "--ticket", number, "--result", "clean", "--sha", head,
            )
            self.fixture.log_event(
                "outcome", "--ticket", number, "--outcome", "completed",
            )

        result = self.fixture.advance(2)

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(self.advance_events(), [])
        for number in ("08", "09"):
            self.assertIn(
                f"{number} completed passed over as already landed",
                result.stdout,
            )
        self.assertNotIn("settled completed", result.stdout + result.stderr)
        self.assertIn("wave 3 ready 11", result.stdout)
        self.assertEqual(self.fixture.launches(), [])

    def test_the_last_wave_ends_the_run_instead_of_launching(self):
        self.fixture.settled_ticket("11", "skill-body")

        result = self.fixture.advance(3)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertOneDecision(3, "complete")
        self.assertEqual(self.fixture.launches(), [])

    def test_a_late_actionable_message_supersedes_the_old_final_wave_decision(self):
        self.fixture.settled_ticket("11", "skill-body", verdict="parked")
        first = self.fixture.advance(3)
        self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
        head = self.fixture.work("11", "skill-body")
        self.fixture.log_event(
            "message", "--role", "child", "--ticket", "11",
            "--message", f"CREW COMPLETE {head}",
        )
        self.fixture.receipt("11", "landable", head)

        resumed = self.fixture.advance(3)

        self.assertEqual(resumed.returncode, 0, resumed.stderr + resumed.stdout)
        self.assertTrue(self.fixture.merged(head))
        decisions = self.advance_events()
        self.assertEqual([event.get("decision") for event in decisions], ["complete", "complete"])


class ParkedAdvanceTests(AdvanceTestCase):
    """A parked ticket only stops the chain when the table has descendants to block."""

    def test_a_parked_ticket_with_no_descendants_is_settled_for_advancement(self):
        self.fixture.settled_ticket("08", "monitor-dashboard")
        self.fixture.settled_ticket("09", "merge-driver", verdict="parked")

        result = self.fixture.advance(2)

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(self.advance_events(), [])
        self.assertIn("09", result.stdout)
        self.assertIn("parked", result.stdout)
        self.assertIn("settled", result.stdout)
        self.assertIn("passed over as settled", result.stdout)
        self.assertIn("wave 3 ready 11", result.stdout)
        self.assertEqual(self.fixture.launches(), [])

    def test_a_last_wave_with_only_a_parked_ticket_records_completion(self):
        self.fixture.settled_ticket("11", "skill-body", verdict="parked")

        result = self.fixture.advance(3)

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        event = self.assertOneDecision(3, "complete")
        detail = event.get("detail", "")
        self.assertIn("11", detail)
        self.assertIn("parked", detail)
        self.assertIn("settled", detail)
        self.assertEqual(self.fixture.launches(), [])

    def test_a_parked_ticket_without_descendants_is_not_an_escalation_root(self):
        self.fixture.settled_ticket("08", "monitor-dashboard", verdict="failed")
        self.fixture.settled_ticket("09", "merge-driver", verdict="parked")

        result = self.fixture.advance(2)

        self.assertEqual(result.returncode, 1, result.stdout)
        event = self.assertOneDecision(2, "escalated")
        detail = event.get("detail", "")
        self.assertIn("08", detail)
        self.assertIn("failed", detail)
        self.assertIn("09", detail)
        self.assertIn("passed over as settled", detail)
        blocked = sorted(record["ticket"] for record in self.fixture.records("outcome"))
        self.assertEqual(blocked, ["11"])


class HaltingTests(AdvanceTestCase):
    """A failure or an escalation stops the chain and wakes nobody twice."""

    def test_a_failed_ticket_halts_advancement_with_one_escalation(self):
        self.fixture.settled_ticket("06", "dispatch-renderer", verdict="failed")
        self.fixture.settled_ticket("07", "machine-log")

        result = self.fixture.advance(1)

        self.assertEqual(result.returncode, 1, result.stdout)
        event = self.assertOneDecision(1, "escalated")
        self.assertIn("06", event.get("detail", ""))
        self.assertEqual(self.fixture.launches(), [])

    def test_an_escalated_merge_halts_advancement_with_one_escalation(self):
        self.fixture.launched("06", "dispatch-renderer")
        head = self.fixture.conflicting_work("06", "dispatch-renderer")
        self.fixture.receipt("06", "landable", head)
        self.fixture.settled_ticket("07", "machine-log")

        result = self.fixture.advance(1)

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertOneDecision(1, "escalated")
        self.assertEqual(self.fixture.launches(), [])

    def test_the_escalation_carries_the_pointers_a_ruling_starts_from(self):
        self.fixture.settled_ticket("06", "dispatch-renderer", verdict="failed")
        self.fixture.settled_ticket("07", "machine-log")

        self.fixture.advance(1)

        detail = self.advance_events()[0].get("detail", "")
        self.assertIn(str(self.fixture.feature_dir / "06-dispatch-renderer.md"), detail)
        self.assertIn("worktree-06-dispatch-renderer", detail)

    def test_a_wave_that_has_not_settled_is_not_a_decision(self):
        self.fixture.settled_ticket("06", "dispatch-renderer")
        self.fixture.launched("07", "machine-log")

        result = self.fixture.advance(1)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("07", result.stderr)
        self.assertEqual(self.advance_events(), [])
        self.assertEqual(self.fixture.records("merge"), [])


class BlockedDescendantTests(AdvanceTestCase):
    """A ticket whose blocker failed or parked is blocked before it can be launched."""

    def test_unlaunched_descendants_of_a_failed_ticket_are_blocked_and_never_launch(self):
        self.fixture.settled_ticket("06", "dispatch-renderer", verdict="failed")
        self.fixture.settled_ticket("07", "machine-log")

        self.fixture.advance(1)

        blocked = sorted(record["ticket"] for record in self.fixture.records("outcome"))
        self.assertEqual(blocked, ["08", "11"])
        self.assertTrue(
            all(record["outcome"] == "blocked" for record in self.fixture.records("outcome"))
        )
        self.assertEqual(self.fixture.launches(), [])

    def test_a_parked_ticket_blocks_its_descendants_too(self):
        self.fixture.settled_ticket("06", "dispatch-renderer", verdict="parked")
        self.fixture.settled_ticket("07", "machine-log")

        result = self.fixture.advance(1)

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertOneDecision(1, "escalated")
        blocked = sorted(record["ticket"] for record in self.fixture.records("outcome"))
        self.assertEqual(blocked, ["08", "11"])

    def test_a_blocked_ticket_names_only_the_ticket_it_descends_from(self):
        self.fixture.settled_ticket("06", "dispatch-renderer", verdict="failed")
        self.fixture.settled_ticket("07", "machine-log", verdict="parked")

        self.fixture.advance(1)

        why = {record["ticket"]: record["detail"] for record in self.fixture.records("outcome")}
        self.assertEqual(why["08"], "descendant of 06 failed")
        self.assertEqual(why["09"], "descendant of 07 parked")
        self.assertEqual(why["11"], "descendant of 06 failed")

    def test_a_ticket_the_failure_cannot_reach_is_left_alone(self):
        self.fixture.settled_ticket("06", "dispatch-renderer", verdict="failed")
        self.fixture.settled_ticket("07", "machine-log")

        self.fixture.advance(1)

        blocked = [record["ticket"] for record in self.fixture.records("outcome")]
        self.assertNotIn("09", blocked)


class InterruptTests(AdvanceTestCase):
    """The operator's interrupt stops the chain where it stands, with the run in one piece."""

    def hold_the_merge(self):
        """A hook that holds the merge open until this test releases it, so the interrupt lands."""
        marker = self.fixture.root / "merging"
        release = self.fixture.root / "release"
        hook = self.fixture.repo / ".git" / "hooks" / "pre-merge-commit"
        hook.write_text(
            "#!/bin/sh\n"
            f"touch {marker}\n"
            f"while [ ! -e {release} ]; do sleep 0.05; done\n"
        )
        hook.chmod(0o755)
        return marker, release

    def test_an_interrupt_stops_before_the_next_wave_and_leaves_the_run_intact(self):
        heads = {
            number: self.fixture.settled_ticket(number, slug)
            for number, slug, _ in WAVES[1]
        }
        marker, release = self.hold_the_merge()

        advance = subprocess.Popen(
            self.fixture.command(1), env=self.fixture.environment(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
        )
        deadline = time.monotonic() + 30
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(marker.exists(), "the merge never started")
        # The operator's Ctrl-C reaches the whole foreground group, not one process.
        os.killpg(os.getpgid(advance.pid), signal.SIGINT)
        release.touch()
        advance.communicate(timeout=60)

        self.assertEqual(advance.returncode, INTERRUPT_EXIT)
        self.assertOneDecision(1, "interrupted")
        self.assertEqual(self.fixture.launches(), [])
        # The merge that was in flight finished rather than being torn down.
        self.assertTrue(self.fixture.merged(heads["06"]))
        self.assertFalse((self.fixture.repo / ".git" / "MERGE_HEAD").exists())


if __name__ == "__main__":
    unittest.main()
