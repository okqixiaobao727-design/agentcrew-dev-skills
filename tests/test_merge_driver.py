#!/usr/bin/env python3
"""Behaviour of the merge driver, driven from its command line against a real git fixture.

Every test builds a throwaway repository with an integration branch and one ticket branch per
case, seeds the run's machine log with the receipts the monitor would have written, and runs the
driver with a stub `claude` on PATH. What is asserted is external only: the git graph the wave
left behind, the lines the machine log gained, the command the repair session was launched with,
and the exit code.

The three paths down the ladder (ADR-0004) are the three conflict fixtures: a mechanical conflict
repaired without waking anyone, a mechanical conflict the repair session fails twice, and a
semantic conflict that skips the repair rung.
"""

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

from stub_claude_repair import STRAY_FILE


PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[1]
TESTS_DIR = pathlib.Path(__file__).resolve().parent
ASSETS = PLUGIN_ROOT / "skills" / "crew" / "assets"
DRIVER = ASSETS / "merge_driver.py"
MACHINE_LOG = ASSETS / "machine_log.py"

# The repair rung's routing, as the caller supplies it: a full model ID, never an alias
# (ADR-0003), and the hard budget cap ADR-0004 defaults to two US dollars.
REPAIR_MODEL = "claude-sonnet-4-5-20250929"
DEFAULT_BUDGET_USD = "2"

INTEGRATION_BRANCH = "crew/crew-v2"
FEATURE = "features/demo"

# The shared file every conflict fixture is built in, and the line each side writes into it.
SHARED = "notes.md"
SHARED_BASE = "one\ntwo\nthree\n"


def run_git(repo, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=check
    )


def git_out(repo, *args):
    return run_git(repo, *args).stdout.strip()


class Fixture:
    """A temporary run: a repository with an integration branch, a machine log, and a stub PATH."""

    def __init__(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.repo = self.root / "repo"
        self.repo.mkdir()
        run_git(self.repo, "init", "-b", "main")
        run_git(self.repo, "config", "user.email", "crew@example.invalid")
        run_git(self.repo, "config", "user.name", "Crew Test")
        (self.repo / FEATURE).mkdir(parents=True)
        (self.repo / SHARED).write_text(SHARED_BASE)
        run_git(self.repo, "add", "-A")
        run_git(self.repo, "commit", "-m", "base")
        self.base_commit = git_out(self.repo, "rev-parse", "HEAD")
        run_git(self.repo, "checkout", "-b", INTEGRATION_BRANCH)

        self.log = self.root / "machine.log"
        self.stub_dir = self.root / "stub"
        self.stub_dir.mkdir()
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        claude = self.bin_dir / "claude"
        claude.write_text(
            "#!/bin/sh\nexec %s %s \"$@\"\n" % (sys.executable, TESTS_DIR / "stub_claude_repair.py")
        )
        claude.chmod(0o755)
        self.tickets = []

    def ticket(self, number, slug, changes, verdict="landable"):
        """A ticket: its file, its branch carrying `changes`, and its receipt in the machine log."""
        path = self.repo / FEATURE / f"{number}-{slug}.md"
        path.write_text(f"# {number} {slug}\n")
        run_git(self.repo, "add", "--", str(path))
        run_git(self.repo, "commit", "-m", f"ticket {number}")
        branch = f"worktree-{number}-{slug}"
        run_git(self.repo, "checkout", "-b", branch, self.base_commit)
        for name, text in changes.items():
            (self.repo / name).write_text(text)
        run_git(self.repo, "add", "-A")
        run_git(self.repo, "commit", "-m", f"work on {number}")
        head = git_out(self.repo, "rev-parse", "HEAD")
        run_git(self.repo, "checkout", INTEGRATION_BRANCH)

        self.receipt(number, verdict, head)
        self.tickets.append({
            "id": number,
            "title": slug.replace("-", " "),
            "path": str(path),
            "workflow": "tdd",
            "executor": "claude",
            "model": "claude-opus-4-5-20251101",
            "effort": "medium",
        })
        return branch

    def receipt(self, number, verdict, sha):
        subprocess.run(
            [
                sys.executable, str(MACHINE_LOG), "--log", str(self.log),
                "receipt", "--ticket", number, "--verdict", verdict, "--sha", sha,
            ],
            check=True, capture_output=True,
        )

    def commit_on_integration(self, name, text, message):
        (self.repo / name).write_text(text)
        run_git(self.repo, "add", "-A")
        run_git(self.repo, "commit", "-m", message)

    def table_path(self, tickets=None, waves=None):
        table = {
            "run": {
                "repo_root": str(self.repo),
                "integration_branch": INTEGRATION_BRANCH,
                "integration_base_commit": self.base_commit,
            },
            "waves": waves or [
                {"wave": 1, "tickets": tickets if tickets is not None else self.tickets}
            ],
        }
        path = self.root / "wave-table.json"
        path.write_text(json.dumps(table, indent=2))
        return path

    def land(self, *extra, wave=1, tickets=None, waves=None, env=None, repair_model=REPAIR_MODEL):
        environment = dict(os.environ)
        environment["PATH"] = f"{self.bin_dir}{os.pathsep}{environment['PATH']}"
        environment["AGENTCREW_STUB_DIR"] = str(self.stub_dir)
        environment.update(env or {})
        return subprocess.run(
            [
                sys.executable, str(DRIVER), "land",
                "--table", str(self.table_path(tickets, waves)),
                "--wave", str(wave),
                "--log", str(self.log),
                "--repair-model", repair_model,
                *extra,
            ],
            capture_output=True, text=True, env=environment,
        )

    def repairs(self):
        """Every repair session the driver launched, in order."""
        path = self.stub_dir / "repairs.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def events(self, name=None):
        lines = [json.loads(line) for line in self.log.read_text().splitlines()]
        return [line for line in lines if name is None or line["event"] == name]

    def merged(self, branch):
        """Whether `branch` is an ancestor of the integration branch's head."""
        return run_git(
            self.repo, "merge-base", "--is-ancestor", branch, INTEGRATION_BRANCH, check=False
        ).returncode == 0


class MergeDriverTestCase(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.fixture.root)], check=False))

    def assertOnIntegrationBranch(self):
        """The wave ends checked out on the integration branch, with nothing left half-merged."""
        repo = self.fixture.repo
        self.assertEqual(git_out(repo, "rev-parse", "--abbrev-ref", "HEAD"), INTEGRATION_BRANCH)
        self.assertEqual(git_out(repo, "status", "--porcelain"), "")
        self.assertFalse((repo / ".git" / "MERGE_HEAD").exists(), "a merge was left in progress")


class CleanMergeTests(MergeDriverTestCase):
    def test_landable_branches_merge_in_ticket_order_with_no_ff_and_no_model(self):
        first = self.fixture.ticket("07", "alpha", {"alpha.txt": "alpha\n"})
        second = self.fixture.ticket("08", "beta", {"beta.txt": "beta\n"})
        # Listed out of order: ticket order is the driver's, not the table's line order.
        listed = list(reversed(self.fixture.tickets))

        result = self.fixture.land(tickets=listed)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.fixture.merged(first))
        self.assertTrue(self.fixture.merged(second))

        repo = self.fixture.repo
        merges = [
            line.split("\t")
            for line in git_out(
                repo, "log", "--first-parent", "--merges", "--format=%P\t%s"
            ).splitlines()
        ]
        self.assertEqual(len(merges), 2, merges)
        # `--no-ff`: each landing is its own merge commit with two parents, newest first.
        for parents, _ in merges:
            self.assertEqual(len(parents.split()), 2, parents)
        self.assertIn(second, merges[0][1])
        self.assertIn(first, merges[1][1])

        logged = self.fixture.events("merge")
        self.assertEqual([entry["ticket"] for entry in logged], ["07", "08"])
        for entry, branch in zip(logged, (first, second)):
            self.assertEqual(entry["result"], "clean")
            self.assertEqual(entry["branch"], branch)
            self.assertEqual(entry["into"], INTEGRATION_BRANCH)
        self.assertEqual(
            [entry["sha"] for entry in logged],
            git_out(repo, "log", "--first-parent", "--merges", "--format=%H").splitlines()[::-1],
        )

        self.assertEqual(self.fixture.repairs(), [], "a clean wave invokes no model")
        self.assertEqual(self.fixture.events("escalation"), [])
        self.assertOnIntegrationBranch()

    def test_failed_and_parked_branches_are_never_merged(self):
        landable = self.fixture.ticket("07", "alpha", {"alpha.txt": "alpha\n"})
        failed = self.fixture.ticket("08", "beta", {"beta.txt": "beta\n"}, verdict="failed")
        parked = self.fixture.ticket("09", "gamma", {"gamma.txt": "gamma\n"}, verdict="parked")

        result = self.fixture.land()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.fixture.merged(landable))
        self.assertFalse(self.fixture.merged(failed))
        self.assertFalse(self.fixture.merged(parked))
        self.assertEqual([entry["ticket"] for entry in self.fixture.events("merge")], ["07"])

        repo = self.fixture.repo
        self.assertOnIntegrationBranch()
        # The wave result is the last merge the wave landed.
        self.assertEqual(
            git_out(repo, "rev-parse", "HEAD"),
            git_out(repo, "log", "--first-parent", "--merges", "--format=%H", "-1"),
        )
        self.assertEqual(self.fixture.repairs(), [])

    def test_a_branch_that_moved_since_its_receipt_is_not_merged(self):
        """A receipt verifies a commit, and a branch is a ref that can move off it."""
        branch = self.fixture.ticket("07", "alpha", {"alpha.txt": "alpha\n"})
        run_git(self.fixture.repo, "checkout", branch)
        (self.fixture.repo / "alpha.txt").write_text("alpha, and more nobody checked\n")
        run_git(self.fixture.repo, "add", "-A")
        run_git(self.fixture.repo, "commit", "-m", "work after the receipt")
        run_git(self.fixture.repo, "checkout", INTEGRATION_BRANCH)

        result = self.fixture.land()

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertFalse(self.fixture.merged(branch))
        escalations = [
            entry for entry in self.fixture.events("merge") if entry["result"] == "escalated"
        ]
        self.assertEqual(len(escalations), 1, escalations)
        self.assertEqual(escalations[0]["ticket"], "07")
        self.assertOnIntegrationBranch()

    def test_a_branch_still_at_the_sha_its_receipt_verified_is_merged(self):
        branch = self.fixture.ticket("07", "alpha", {"alpha.txt": "alpha\n"})

        result = self.fixture.land()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.fixture.merged(branch))

    def test_a_landable_receipt_naming_no_sha_verified_nothing_and_is_not_merged(self):
        branch = self.fixture.ticket("07", "alpha", {"alpha.txt": "alpha\n"})
        self.fixture.log.write_text("")
        subprocess.run(
            [
                sys.executable, str(MACHINE_LOG), "--log", str(self.fixture.log),
                "receipt", "--ticket", "07", "--verdict", "landable",
            ],
            check=True, capture_output=True,
        )

        result = self.fixture.land()

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertFalse(self.fixture.merged(branch))
        self.assertEqual(
            len([e for e in self.fixture.events("merge") if e["result"] == "escalated"]), 1
        )

    def test_a_ticket_with_no_receipt_is_not_merged(self):
        unreported = self.fixture.ticket("07", "alpha", {"alpha.txt": "alpha\n"})
        self.fixture.log.write_text("")

        result = self.fixture.land()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.fixture.merged(unreported))
        self.assertEqual(self.fixture.events("merge"), [])


class MechanicalConflictTests(MergeDriverTestCase):
    """Both sides inserted at the same point: a textual conflict with no design disagreement."""

    def conflict(self):
        branch = self.fixture.ticket("07", "alpha", {SHARED: SHARED_BASE + "from the ticket\n"})
        self.fixture.commit_on_integration(
            SHARED, SHARED_BASE + "from the integration branch\n", "integration adds a line"
        )
        return branch

    def test_the_repair_session_carries_the_full_model_id_and_the_budget_cap(self):
        self.conflict()

        result = self.fixture.land()

        self.assertEqual(result.returncode, 0, result.stderr)
        repairs = self.fixture.repairs()
        self.assertEqual(len(repairs), 1, repairs)
        argv = repairs[0]["argv"]
        self.assertIn("--model", argv)
        self.assertEqual(argv[argv.index("--model") + 1], REPAIR_MODEL)
        self.assertIn("--max-budget-usd", argv)
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], DEFAULT_BUDGET_USD)
        # `--max-budget-usd` binds only on a headless session, so the cap is only a cap with it.
        self.assertTrue({"--print", "-p"} & set(argv), argv)
        self.assertEqual(repairs[0]["cwd"], os.path.realpath(str(self.fixture.repo)))

    def test_a_stubbed_repair_completes_the_merge_without_waking_the_coordinator(self):
        branch = self.conflict()

        result = self.fixture.land()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.fixture.merged(branch))
        self.assertEqual(
            (self.fixture.repo / SHARED).read_text(),
            SHARED_BASE + "from the integration branch\nfrom the ticket\n",
        )
        results = [entry["result"] for entry in self.fixture.events("merge")]
        self.assertEqual(results, ["conflict", "repaired"])
        self.assertNotIn("escalated", results)
        self.assertOnIntegrationBranch()

    def test_a_second_attempt_follows_a_first_that_failed(self):
        branch = self.conflict()

        result = self.fixture.land(env={"AGENTCREW_STUB_REPAIR_SEQUENCE": "noop,resolve"})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.fixture.repairs()), 2)
        self.assertTrue(self.fixture.merged(branch))
        self.assertEqual(
            [entry["result"] for entry in self.fixture.events("merge")], ["conflict", "repaired"]
        )

    def test_a_repair_double_failure_escalates_once_and_leaves_the_branch_unmerged(self):
        branch = self.conflict()

        result = self.fixture.land(env={"AGENTCREW_STUB_REPAIR": "noop"})

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(len(self.fixture.repairs()), 2, "the ladder's repair rung is tried twice")
        self.assertFalse(self.fixture.merged(branch))

        escalations = [
            entry for entry in self.fixture.events("merge") if entry["result"] == "escalated"
        ]
        self.assertEqual(len(escalations), 1, escalations)
        self.assertEqual(escalations[0]["ticket"], "07")
        self.assertEqual(escalations[0]["branch"], branch)
        # An escalation carries its own pointers, so a ruling never starts with a hunt.
        detail = escalations[0]["detail"]
        self.assertIn(str(self.fixture.repo / FEATURE / "07-alpha.md"), detail)
        self.assertIn(SHARED, detail)
        self.assertOnIntegrationBranch()

    def test_a_repair_session_that_exits_nonzero_is_a_failed_attempt(self):
        branch = self.conflict()

        result = self.fixture.land(env={"AGENTCREW_STUB_REPAIR": "fail"})

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(len(self.fixture.repairs()), 2)
        self.assertFalse(self.fixture.merged(branch))
        self.assertEqual(
            len([e for e in self.fixture.events("merge") if e["result"] == "escalated"]), 1
        )

    def test_a_repair_that_leaves_any_marker_standing_has_not_resolved_anything(self):
        branch = self.conflict()

        result = self.fixture.land(env={"AGENTCREW_STUB_REPAIR": "half"})

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertFalse(self.fixture.merged(branch))
        self.assertEqual(
            len([e for e in self.fixture.events("merge") if e["result"] == "escalated"]), 1
        )
        # The half-resolved file went back where it came from rather than into the branch.
        self.assertOnIntegrationBranch()

    def test_a_repair_that_edits_outside_the_conflict_has_not_resolved_anything(self):
        branch = self.conflict()

        result = self.fixture.land(env={"AGENTCREW_STUB_REPAIR": "stray"})

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertFalse(self.fixture.merged(branch), "unverified work never reaches the branch")
        escalations = [
            entry for entry in self.fixture.events("merge") if entry["result"] == "escalated"
        ]
        self.assertEqual(len(escalations), 1, escalations)
        self.assertIn(STRAY_FILE, escalations[0]["detail"])

    def test_a_repair_that_rewrites_a_file_already_in_the_tree_is_a_stray(self):
        """A file the session rewrites in place keeps its status code; only its contents moved."""
        self.conflict()
        (self.fixture.repo / STRAY_FILE).write_text("what was already there\n")

        result = self.fixture.land(env={"AGENTCREW_STUB_REPAIR": "stray"})

        self.assertEqual(result.returncode, 1, result.stdout)
        escalations = [
            entry for entry in self.fixture.events("merge") if entry["result"] == "escalated"
        ]
        self.assertEqual(len(escalations), 1, escalations)
        self.assertIn(STRAY_FILE, escalations[0]["detail"])

    def test_the_budget_cap_is_the_operators_when_they_give_one(self):
        self.conflict()

        result = self.fixture.land("--repair-budget-usd", "0.5")

        self.assertEqual(result.returncode, 0, result.stderr)
        argv = self.fixture.repairs()[0]["argv"]
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "0.5")

    def test_an_alias_is_refused_as_a_repair_model(self):
        self.conflict()

        result = self.fixture.land(repair_model="sonnet")

        self.assertEqual(result.returncode, 1)
        self.assertIn("alias", result.stderr)
        self.assertEqual(self.fixture.repairs(), [], "nothing is launched on an unresolved name")
        self.assertEqual(self.fixture.events("merge"), [])


class SemanticConflictTests(MergeDriverTestCase):
    """Both sides rewrote the same existing lines: two designs disagree, and only a ruling settles
    which one stands."""

    def conflict(self):
        branch = self.fixture.ticket("07", "alpha", {SHARED: "one\nthe ticket's two\nthree\n"})
        self.fixture.commit_on_integration(
            SHARED, "one\nthe integration branch's two\nthree\n", "integration rewrites a line"
        )
        return branch

    def test_a_semantic_conflict_skips_the_repair_rung_and_escalates_directly(self):
        branch = self.conflict()

        result = self.fixture.land()

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(self.fixture.repairs(), [], "the repair rung is skipped, not attempted")
        self.assertFalse(self.fixture.merged(branch))

        results = [entry["result"] for entry in self.fixture.events("merge")]
        self.assertEqual(results.count("escalated"), 1)
        self.assertEqual(results.count("repaired"), 0)
        self.assertOnIntegrationBranch()

    def test_a_file_both_sides_created_is_semantic(self):
        """Two children each wrote their own version of the same new file: a design disagreement,
        not a textual accident."""
        branch = self.fixture.ticket("07", "alpha", {"new.txt": "the ticket's version\n"})
        self.fixture.commit_on_integration(
            "new.txt", "the integration branch's version\n", "integration adds the same file"
        )

        result = self.fixture.land()

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(self.fixture.repairs(), [])
        self.assertFalse(self.fixture.merged(branch))
        self.assertEqual(
            len([e for e in self.fixture.events("merge") if e["result"] == "escalated"]), 1
        )

    def test_a_file_one_side_deleted_and_the_other_changed_is_semantic(self):
        branch = self.fixture.ticket("07", "alpha", {SHARED: "one\ntwo\nthree\nfour\n"})
        (self.fixture.repo / SHARED).unlink()
        run_git(self.fixture.repo, "add", "-A")
        run_git(self.fixture.repo, "commit", "-m", "integration deletes the file")

        result = self.fixture.land()

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(self.fixture.repairs(), [])
        self.assertFalse(self.fixture.merged(branch))
        self.assertEqual(
            len([e for e in self.fixture.events("merge") if e["result"] == "escalated"]), 1
        )


class WaveTests(MergeDriverTestCase):
    def test_an_escalation_does_not_stop_the_tickets_behind_it(self):
        """One branch the coordinator must rule on does not hold up the ones that merge cleanly."""
        conflicting = self.fixture.ticket("07", "alpha", {SHARED: "one\nthe ticket's two\nthree\n"})
        self.fixture.commit_on_integration(
            SHARED, "one\nthe integration branch's two\nthree\n", "integration rewrites a line"
        )
        clean = self.fixture.ticket("08", "beta", {"beta.txt": "beta\n"})

        result = self.fixture.land()

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertFalse(self.fixture.merged(conflicting))
        self.assertTrue(self.fixture.merged(clean))
        self.assertOnIntegrationBranch()

    def test_only_the_named_waves_tickets_are_landed(self):
        first = self.fixture.ticket("07", "alpha", {"alpha.txt": "alpha\n"})
        second = self.fixture.ticket("08", "beta", {"beta.txt": "beta\n"})

        result = self.fixture.land(wave=1, waves=[
            {"wave": 1, "tickets": [self.fixture.tickets[0]]},
            {"wave": 2, "tickets": [self.fixture.tickets[1]]},
        ])

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.fixture.merged(first))
        self.assertFalse(self.fixture.merged(second))

    def test_a_dirty_repository_is_refused_before_anything_is_merged(self):
        branch = self.fixture.ticket("07", "alpha", {"alpha.txt": "alpha\n"})
        (self.fixture.repo / SHARED).write_text("an uncommitted edit\n")

        result = self.fixture.land()

        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.fixture.merged(branch))
        self.assertEqual(self.fixture.events("merge"), [])


if __name__ == "__main__":
    unittest.main()
