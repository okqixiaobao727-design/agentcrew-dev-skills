"""Run the Codex bridge's existing end-to-end contract through the root suite.

The bridge sits in the spine rather than in a named asset test directory, so ADR-0016's single
entry point reaches its private-tmux shell suite through this root adapter.  The shell script owns
all behavioural assertions; this test only makes its exit status part of ``scripts/test.py``.
"""

import pathlib
import subprocess
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
BRIDGE_SUITE = ROOT / "skills/crew/assets/codex/test-codex-bridge.sh"


class CodexBridgeSuiteTests(unittest.TestCase):
    def test_end_to_end_bridge_contract(self):
        result = subprocess.run(
            [str(BRIDGE_SUITE)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )


if __name__ == "__main__":
    unittest.main()
