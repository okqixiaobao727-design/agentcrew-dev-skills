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
