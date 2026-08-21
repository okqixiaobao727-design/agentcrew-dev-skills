#!/usr/bin/env python3
"""Behaviour of `scripts/test.py`, asserted at its CLI seam and against the real inventory.

The runner is the one entry point to validation (ADR-0016), so two of its properties are load
bearing and are checked here against the tree this repository actually ships: every suite in the
inventory contributes tests, and walking the asset suites leaves the root walk alone. Everything
about selection, exit codes and reporting is asserted against a throwaway tree instead, because a
test that runs the real suites to observe the runner would cost the very fifteen minutes this
script exists to avoid.
"""

import importlib.util
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest


PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = PLUGIN_ROOT / "scripts" / "test.py"

PASSING_TEST = """
import unittest


class Passes(unittest.TestCase):
    def test_it_passes(self):
        self.assertTrue(True)
"""

FAILING_TEST = """
import unittest


class Fails(unittest.TestCase):
    def test_it_fails(self):
        self.fail("as the fixture intends")
"""


# A pair of suites that only pass if they are running at the same time: each announces itself and
# then waits for the other. Run one after another, the first waits out the whole deadline and
# fails, which is what makes this an observation of concurrency rather than of wall time.
RENDEZVOUS_TEST = """
import pathlib
import time
import unittest

SHARED = pathlib.Path({shared!r})
NAME = {name!r}
PARTNER = {partner!r}


class Rendezvous(unittest.TestCase):
    def test_the_other_suite_is_running_too(self):
        SHARED.mkdir(parents=True, exist_ok=True)
        (SHARED / NAME).write_text("here")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if (SHARED / PARTNER).exists():
                return
            time.sleep(0.02)
        self.fail("%s never started; the suites did not overlap" % PARTNER)
"""

# A pair of test methods inside a *single* suite that only pass if they are running at the same
# time. The suite-level rendezvous above cannot see this: it is satisfied by one worker per suite.
# This one can only pass if that one suite's tests were split across interpreters.
RENDEZVOUS_WITHIN_A_SUITE = """
import pathlib
import time
import unittest

SHARED = pathlib.Path({shared!r})


class Rendezvous(unittest.TestCase):
    def meet(self, name, partner):
        SHARED.mkdir(parents=True, exist_ok=True)
        (SHARED / name).write_text("here")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if (SHARED / partner).exists():
                return
            time.sleep(0.02)
        self.fail("%s never started; the suite's own tests did not overlap" % partner)

    def test_the_other_test_in_this_suite_is_running_too(self):
        self.meet("first", "second")

    def test_this_suite_s_other_test_is_running_too(self):
        self.meet("second", "first")
"""

# Two suites, each with a helper module of the same name — the shape the real tree already has,
# where four asset suites ship a `stub_claude.py`. A suite that imported its neighbour's copy
# would read the wrong constant.
HELPER = """
VALUE = {value!r}
"""

HELPER_TEST = """
import unittest

import helper


class ItsOwnHelper(unittest.TestCase):
    def test_the_helper_is_this_suite_s_own(self):
        assert helper.VALUE == {value!r}, helper.VALUE

    def test_the_helper_is_still_this_suite_s_own_beside_its_neighbour(self):
        assert helper.VALUE == {value!r}, helper.VALUE
"""


# A suite that writes bytes no locale can decode, straight at the descriptor its own subprocesses
# would inherit. It stands in for the real thing: git and the tmux stubs emit whatever a branch
# name or a terminal happened to hold, which is not always valid UTF-8.
UNDECODABLE_TEST = """
import os
import unittest


class Undecodable(unittest.TestCase):
    def test_it_writes_bytes_no_locale_can_decode(self):
        os.write(2, b"\\xff\\xfe raw bytes at the descriptor\\n")
"""


def load_runner():
    """`scripts/test.py` as a module, under a name that cannot shadow the stdlib's `test`."""
    spec = importlib.util.spec_from_file_location("agentcrew_test_runner", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = load_runner()


class TreeFixture:
    """A throwaway repository with the suite layout the runner inventories, and nothing else."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)

    def close(self):
        self._tmp.cleanup()

    def suite(self, relative, **modules):
        """Create a tests directory holding the named modules; no modules means an empty suite."""
        directory = self.root / relative
        directory.mkdir(parents=True)
        for name, body in modules.items():
            (directory / f"{name}.py").write_text(body)

    def run(self, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--root", str(self.root), *args],
            capture_output=True,
            text=True,
        )


class RunnerCLITests(unittest.TestCase):
    """What the caller gets back for each way of naming — or not naming — what to run."""

    def setUp(self):
        self.tree = TreeFixture()
        self.addCleanup(self.tree.close)
        self.tree.suite("tests", test_root=PASSING_TEST)
        self.tree.suite("skills/crew/assets/alpha/tests", test_alpha=PASSING_TEST)
        self.tree.suite("skills/route/assets/beta/tests", test_beta=PASSING_TEST)

    def test_naming_no_asset_runs_every_suite(self):
        run = self.tree.run()

        self.assertEqual(run.returncode, 0, run.stderr)
        for name in ("root", "alpha", "beta"):
            self.assertIn(f"{name}: 1 tests in", run.stderr)
        self.assertIn("total: 3 tests in", run.stderr)

    def test_naming_an_asset_runs_that_suite_and_no_other(self):
        run = self.tree.run("--asset", "alpha")

        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("alpha: 1 tests in", run.stderr)
        self.assertNotIn("beta:", run.stderr)
        self.assertNotIn("root:", run.stderr)

    def test_the_repository_s_own_suite_is_selectable_too(self):
        run = self.tree.run("--asset", "root")

        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("root: 1 tests in", run.stderr)
        self.assertNotIn("alpha:", run.stderr)

    def test_an_unknown_asset_names_the_suites_it_could_have_been(self):
        run = self.tree.run("--asset", "gamma")

        self.assertEqual(run.returncode, 2)
        self.assertIn("gamma", run.stderr)
        self.assertIn("alpha", run.stderr)
        self.assertIn("beta", run.stderr)
        self.assertNotIn("tests in", run.stderr)

    def test_a_failing_test_fails_the_run(self):
        self.tree.suite("skills/crew/assets/broken/tests", test_broken=FAILING_TEST)

        run = self.tree.run("--asset", "broken")

        self.assertEqual(run.returncode, 1)
        self.assertIn("as the fixture intends", run.stderr)

    def test_a_suite_that_loads_nothing_fails_the_run(self):
        """The guard that matters most: a suite stopping silently is what nobody notices."""
        self.tree.suite("skills/crew/assets/silent/tests")

        run = self.tree.run()

        self.assertEqual(run.returncode, 1)
        self.assertIn("silent", run.stderr)
        self.assertIn("no tests", run.stderr)

    def test_a_module_that_cannot_be_imported_fails_the_run(self):
        """An import error is not an empty suite: unittest reports it as a test that failed."""
        self.tree.suite("skills/crew/assets/unimportable/tests", test_broken="import nope_missing")

        run = self.tree.run("--asset", "unimportable")

        self.assertEqual(run.returncode, 1)
        self.assertIn("nope_missing", run.stderr)

    def test_one_name_answering_to_two_suites_is_refused_rather_than_guessed(self):
        self.tree.suite("skills/route/assets/alpha/tests", test_other_alpha=PASSING_TEST)

        run = self.tree.run("--asset", "alpha")

        self.assertEqual(run.returncode, 2)
        self.assertIn("skills/crew/assets/alpha/tests", run.stderr)
        self.assertIn("skills/route/assets/alpha/tests", run.stderr)

    def test_a_tree_without_the_repository_s_own_suite_is_an_error_not_a_pass(self):
        """`tests/` going missing must fail the gate, not quietly shrink the inventory."""
        headless = TreeFixture()
        self.addCleanup(headless.close)
        headless.suite("skills/crew/assets/alpha/tests", test_alpha=PASSING_TEST)

        run = headless.run()

        self.assertEqual(run.returncode, 2)
        self.assertIn("tests", run.stderr)
        self.assertNotIn("alpha: 1 tests in", run.stderr)

    def test_a_tree_with_no_asset_suites_is_an_error_not_a_pass(self):
        bare = TreeFixture()
        self.addCleanup(bare.close)
        bare.suite("tests", test_root=PASSING_TEST)

        run = bare.run()

        self.assertEqual(run.returncode, 2)
        self.assertIn("skills/*/assets/**/tests", run.stderr)


class ParallelGateTests(unittest.TestCase):
    """The gate runs its suites at once, and keeps each one's process and output to itself."""

    def setUp(self):
        self.tree = TreeFixture()
        self.addCleanup(self.tree.close)
        self.tree.suite("tests", test_root=PASSING_TEST)

    def rendezvous_pair(self):
        shared = str(self.tree.root / "rendezvous")
        for name, partner in (("first", "second"), ("second", "first")):
            self.tree.suite(
                f"skills/crew/assets/{name}/tests",
                **{
                    f"test_{name}": RENDEZVOUS_TEST.format(
                        shared=shared, name=name, partner=partner
                    )
                },
            )

    def test_the_gate_runs_its_suites_at_the_same_time(self):
        self.rendezvous_pair()

        run = self.tree.run()

        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("total: 3 tests in", run.stderr)

    def test_jobs_one_runs_them_one_after_another(self):
        """The escape hatch: the same inventory, serially, for bisecting a suite that only fails
        beside its neighbours."""
        self.rendezvous_pair()

        run = self.tree.run("--jobs", "1")

        self.assertEqual(run.returncode, 1)
        self.assertIn("did not overlap", run.stderr)

    def test_each_suite_imports_its_own_helper_of_a_shared_name(self):
        """Suites share module names, so they cannot share an interpreter.

        Asserted at one suite at a time as well as at all of them, because that is the arrangement
        that would reuse a worker: with a worker per suite, a pool that never replaced its workers
        would pass this anyway. One at a time, the same worker takes both suites or the run fails.

        Each suite holds two tests, so this also covers the shards: two tests of one suite land in
        different interpreters, and each must still see its own suite's helper rather than the
        one whichever shard imported first.
        """
        for name, value in (("alpha", "alpha's own"), ("beta", "beta's own")):
            self.tree.suite(
                f"skills/crew/assets/{name}/tests",
                helper=HELPER.format(value=value),
                **{f"test_{name}": HELPER_TEST.format(value=value)},
            )

        for jobs in ((), ("--jobs", "1")):
            with self.subTest(jobs=jobs or "all"):
                run = self.tree.run(*jobs)

                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertIn("total: 5 tests in", run.stderr)

    def test_asking_for_more_workers_than_the_machine_has_cores_is_bounded_and_said_out_loud(self):
        """These tests wait on absolute timeouts, so oversubscribing fails tests rather than
        slowing them; a flag that will not be obeyed as asked has to say so."""
        cores = os.cpu_count() or 1
        self.tree.suite("skills/crew/assets/alpha/tests", test_alpha=PASSING_TEST)

        run = self.tree.run("--jobs", str(cores + 1))

        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn(f"--jobs {cores + 1}", run.stderr)
        self.assertIn(f"{cores} cores", run.stderr)

    def test_asking_for_no_jobs_at_all_is_refused_rather_than_run(self):
        run = self.tree.run("--jobs", "0")

        self.assertEqual(run.returncode, 2)
        self.assertIn("--jobs", run.stderr)
        self.assertNotIn("tests in", run.stderr)

    def test_a_suite_writing_undecodable_bytes_is_still_reported(self):
        """Captured output is replayed, not decoded strictly: a suite that emits bytes the locale
        cannot read should cost its own output's legibility, never the whole gate's exit."""
        self.tree.suite("skills/crew/assets/alpha/tests", test_alpha=PASSING_TEST)
        self.tree.suite("skills/crew/assets/noisy/tests", test_noisy=UNDECODABLE_TEST)

        run = self.tree.run()

        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("noisy: 1 tests in", run.stderr)
        self.assertIn("total: 3 tests in", run.stderr)

    def test_one_suite_failing_beside_the_others_fails_the_run(self):
        self.tree.suite("skills/crew/assets/alpha/tests", test_alpha=PASSING_TEST)
        self.tree.suite("skills/crew/assets/broken/tests", test_broken=FAILING_TEST)

        run = self.tree.run()

        self.assertEqual(run.returncode, 1)
        self.assertIn("as the fixture intends", run.stderr)
        self.assertIn("alpha: 1 tests in", run.stderr)


class ShardedSuiteTests(unittest.TestCase):
    """One suite is more than one work item: its own tests run in interpreters of their own."""

    def setUp(self):
        self.tree = TreeFixture()
        self.addCleanup(self.tree.close)
        self.tree.suite("tests", test_root=PASSING_TEST)

    def rendezvous_within_one_suite(self):
        self.tree.suite(
            "skills/crew/assets/paired/tests",
            test_paired=RENDEZVOUS_WITHIN_A_SUITE.format(
                shared=str(self.tree.root / "rendezvous")
            ),
        )

    def test_one_suite_s_own_tests_run_at_the_same_time(self):
        """The heart of the change: a suite is no longer bounded by one interpreter."""
        if (os.cpu_count() or 1) < 2:
            self.skipTest("a machine with one core has nothing to shard across")
        self.rendezvous_within_one_suite()

        run = self.tree.run()

        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("paired: 2 tests in", run.stderr)

    def test_jobs_one_still_runs_one_work_item_at_a_time(self):
        """The escape hatch survives sharding: the pair that needs two interpreters now fails."""
        self.rendezvous_within_one_suite()

        run = self.tree.run("--jobs", "1")

        self.assertEqual(run.returncode, 1)
        self.assertIn("did not overlap", run.stderr)

    def test_a_suite_with_fewer_tests_than_shards_still_reports_all_of_them(self):
        """A shard that legitimately receives nothing is ordinary, not the empty-suite failure."""
        self.tree.suite("skills/crew/assets/small/tests", test_small=PASSING_TEST)

        run = self.tree.run("--asset", "small")

        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("small: 1 tests in", run.stderr)
        self.assertNotIn("no tests", run.stderr)

    def test_a_failing_test_in_one_shard_fails_the_whole_run(self):
        self.tree.suite(
            "skills/crew/assets/mixed/tests",
            test_passes=PASSING_TEST,
            test_fails=FAILING_TEST,
        )

        run = self.tree.run("--asset", "mixed")

        self.assertEqual(run.returncode, 1)
        self.assertIn("as the fixture intends", run.stderr)
        self.assertIn("mixed: 2 tests in", run.stderr)

    def test_every_suite_is_still_reported_by_name_with_its_whole_count(self):
        """A sharded suite reports one line, summed — not one line per shard."""
        for name in ("alpha", "beta"):
            self.tree.suite(
                f"skills/crew/assets/{name}/tests",
                test_one=PASSING_TEST,
                test_two=PASSING_TEST.replace("Passes", "AlsoPasses"),
            )

        run = self.tree.run()

        self.assertEqual(run.returncode, 0, run.stderr)
        for name in ("alpha", "beta"):
            self.assertEqual(run.stderr.count(f"{name}: "), 1, run.stderr)
            self.assertIn(f"{name}: 2 tests in", run.stderr)
        self.assertIn("total: 5 tests in", run.stderr)


class InventoryTests(unittest.TestCase):
    """The two properties the shipped tree has to keep, checked against the shipped tree."""

    def test_every_suite_in_the_inventory_contributes_tests(self):
        """A directory that loads nothing is a suite that stopped running without saying so."""
        inventory = runner.suites(PLUGIN_ROOT)
        self.assertTrue(inventory, f"no suites under {PLUGIN_ROOT}")
        for name, directory in inventory:
            with self.subTest(suite=name):
                self.assertTrue(runner.discover(directory).countTestCases())

    def test_the_inventory_names_the_asset_directory_and_the_repository_suite(self):
        names = [name for name, _ in runner.suites(PLUGIN_ROOT)]

        self.assertEqual(names[0], runner.ROOT_SUITE)
        self.assertIn("driver", names)
        self.assertEqual(len(names), len(set(names)), f"two suites answer to one name: {names}")

    def test_walking_the_asset_suites_leaves_the_root_walk_alone(self):
        """Discovery leaves its top-level directory on the loader it ran on, so every suite gets
        a throwaway one. Sharing a loader would repoint the root walk at an asset directory, and
        every suite discovered after it would fail the loader's "path is within the project" check.
        """
        root_tests = PLUGIN_ROOT / "tests"
        before = runner.discover(root_tests).countTestCases()

        for _, directory in runner.suites(PLUGIN_ROOT):
            runner.discover(directory)

        self.assertEqual(runner.discover(root_tests).countTestCases(), before)


if __name__ == "__main__":
    unittest.main()
