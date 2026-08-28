"""Run the Codex bridge's existing end-to-end contract through the root suite.

The bridge sits in the spine rather than in a named asset test directory, so ADR-0016's single
entry point reaches its private-tmux shell suite through this root adapter. The shell script owns
the end-to-end assertions; focused unit tests here pin launch-failure translation without timing.
"""

import argparse
import importlib.util
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
BRIDGE_SUITE = ROOT / "skills/crew/assets/codex/test-codex-bridge.sh"
BRIDGE = ROOT / "skills/crew/assets/codex/codex_bridge.py"


def load_bridge():
    spec = importlib.util.spec_from_file_location("agentcrew_codex_bridge", BRIDGE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bridge = load_bridge()


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


class LaunchFailureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = pathlib.Path(self.temporary.name)

    def args(self):
        return argparse.Namespace(
            approval="never",
            cwd=str(self.directory),
            effort=None,
            machine_log=None,
            model=None,
            prompt="test prompt",
            prompt_file=None,
            sandbox="danger-full-access",
            startup_timeout=1,
            state_file=str(self.directory / "state.json"),
            thread_id=None,
            ticket=None,
            tmux_session="test:",
            window_name="test",
        )

    async def launch_with_pane_observations(self, observations, log_text="app-server noise\n"):
        client = mock.AsyncMock()
        client.request.side_effect = OSError("transport disconnected")
        runtime_dir = self.directory / "runtime"
        runtime_dir.mkdir()

        def launch_window(_args, runtime_dir, _prompt_file, _skill_path):
            (runtime_dir / "app-server.log").write_text(log_text, encoding="utf-8")
            return "@1", "%1"

        with (
            mock.patch.object(bridge, "launch_window", side_effect=launch_window),
            mock.patch.object(bridge, "connect_when_ready", return_value=client),
            mock.patch.object(bridge, "pane_exists", side_effect=observations),
            mock.patch.object(bridge, "kill_window"),
            mock.patch.object(bridge.tempfile, "mkdtemp", return_value=str(runtime_dir)),
        ):
            return await bridge.cmd_launch(self.args())

    async def test_a_transport_error_after_the_pane_exits_reports_its_retained_log(self):
        with self.assertRaises(bridge.BridgeError) as raised:
            await self.launch_with_pane_observations([True, False])

        self.assertIn("Codex TUI window exited before creating its thread", str(raised.exception))
        self.assertIn("app-server noise", str(raised.exception))

    async def test_a_transport_error_while_the_pane_is_alive_is_not_relabelled(self):
        with self.assertRaisesRegex(OSError, "transport disconnected"):
            await self.launch_with_pane_observations([True, True])

    async def test_an_unreadable_pane_list_does_not_turn_unknown_into_vanished(self):
        with self.assertRaisesRegex(OSError, "transport disconnected"):
            await self.launch_with_pane_observations(
                [True, OSError("tmux list-panes unavailable")]
            )

    async def test_an_opening_skill_terminal_log_outranks_a_still_live_pane(self):
        detail = "Codex opening skill assertion failed: expected exactly one enabled skill\n"

        with self.assertRaises(bridge.BridgeError) as raised:
            await self.launch_with_pane_observations([True, True], detail)

        self.assertEqual(str(raised.exception), detail.strip())

    async def test_a_tui_exit_terminal_log_outranks_a_still_live_pane(self):
        detail = "Codex TUI exited before creating its thread (exit code 1)\n"

        with self.assertRaises(bridge.BridgeError) as raised:
            await self.launch_with_pane_observations([True, True], detail)

        self.assertIn("Codex TUI window exited before creating its thread", str(raised.exception))
        self.assertIn(detail.strip(), str(raised.exception))

    async def test_only_the_final_log_line_can_be_a_terminal_ruling(self):
        detail = (
            "Codex TUI exited before creating its thread (exit code 1)\n"
            "ordinary app-server shutdown noise\n"
        )

        with self.assertRaisesRegex(OSError, "transport disconnected"):
            await self.launch_with_pane_observations([True, True], detail)


if __name__ == "__main__":
    unittest.main()
