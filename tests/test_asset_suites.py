#!/usr/bin/env python3
"""Pull the test suites that live beside the assets they test into the one suite CI runs.

A script shipped as a skill asset keeps its tests next to it — `dispatch/tests`, `monitor/tests`,
`review/tests` — because the stub PATH and fixture repository they need belong with the script
they stand in for. `unittest discover -s tests` would never look there, so each of those
directories is loaded here as its own discovery root: a suite nobody runs is a suite that is
already broken.
"""

import pathlib
import unittest


PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[1]
ASSET_SUITES = "skills/*/assets/**/tests"


def asset_suites():
    """Every tests directory shipped beside a skill asset, as a discovery root."""
    return sorted(path for path in PLUGIN_ROOT.glob(ASSET_SUITES) if path.is_dir())


def discover(directory):
    """That directory's tests, discovered with the directory itself as the import root.

    Deliberately a throwaway loader: `discover` leaves its `top_level_dir` on the loader it
    runs on, and on most Python versions never puts it back. Handing it the loader that is
    walking `tests/` would repoint that walk at an asset directory, and every test file
    sorting after this one would then fail the loader's "path must be within the project"
    assertion.
    """
    return unittest.TestLoader().discover(str(directory), top_level_dir=str(directory))


def load_tests(loader, tests, pattern):
    """The root suite, plus every asset suite; the protocol `unittest discover` calls."""
    for directory in asset_suites():
        tests.addTests(discover(directory))
    return tests


class AssetSuiteTests(unittest.TestCase):
    def test_every_asset_suite_contributes_tests(self):
        """A directory that loads nothing is a suite that stopped running without saying so."""
        self.assertTrue(asset_suites(), f"no tests directory under {ASSET_SUITES}")
        for directory in asset_suites():
            with self.subTest(suite=directory.relative_to(PLUGIN_ROOT)):
                self.assertTrue(discover(directory).countTestCases())

    def test_loading_asset_suites_leaves_the_root_walk_alone(self):
        """The loader walking `tests/` must come back out still rooted at `tests/`."""
        root = str(PLUGIN_ROOT / "tests")
        loader = unittest.TestLoader()
        loader._top_level_dir = root
        load_tests(loader, unittest.TestSuite(), None)
        self.assertEqual(loader._top_level_dir, root)


if __name__ == "__main__":
    unittest.main()
