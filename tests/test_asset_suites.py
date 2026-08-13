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


def discover(loader, directory):
    """That directory's tests, discovered with the directory itself as the import root."""
    return loader.discover(str(directory), top_level_dir=str(directory))


def load_tests(loader, tests, pattern):
    """The root suite, plus every asset suite; the protocol `unittest discover` calls."""
    for directory in asset_suites():
        tests.addTests(discover(loader, directory))
    return tests


class AssetSuiteTests(unittest.TestCase):
    def test_every_asset_suite_contributes_tests(self):
        """A directory that loads nothing is a suite that stopped running without saying so."""
        loader = unittest.TestLoader()
        self.assertTrue(asset_suites(), f"no tests directory under {ASSET_SUITES}")
        for directory in asset_suites():
            with self.subTest(suite=directory.relative_to(PLUGIN_ROOT)):
                self.assertTrue(discover(loader, directory).countTestCases())


if __name__ == "__main__":
    unittest.main()
