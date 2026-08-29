"""Run the Codex bridge's existing end-to-end contract through the root suite.

The bridge sits in the spine rather than in a named asset test directory, so ADR-0016's single
entry point reaches its private-tmux shell suite through this root adapter. The shell script owns
the end-to-end assertions; focused tests here pin contract cleanup and launch-failure translation.
"""

import argparse
import importlib.util
import os
import pathlib
import re
import signal
import subprocess
import tempfile
import time
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
BRIDGE_SUITE = ROOT / "skills/crew/assets/codex/test-codex-bridge.sh"
BRIDGE = ROOT / "skills/crew/assets/codex/codex_bridge.py"
CONTRACT_CLEANUP_GRACE_SECONDS = 10


def load_bridge():
    spec = importlib.util.spec_from_file_location("agentcrew_codex_bridge", BRIDGE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bridge = load_bridge()


def terminate_contracts(processes, *, timeout):
    for process in processes:
        if process.poll() is not None:
            continue
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    deadline = time.monotonic() + timeout
    for process in processes:
        if process.poll() is not None:
            continue
        remaining = max(0, deadline - time.monotonic())
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()


def format_contract_results(results):
    sections = []
    for name, result in results:
        sections.append(
            f"contract shard={name} returncode={result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return "\n".join(sections)


def run_bridge_contracts(invocations, *, cwd, timeout):
    deadline = time.monotonic() + timeout
    running = []
    try:
        for name, command, environment in invocations:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            running.append((name, command, process))
    except BaseException:
        terminate_contracts(
            [process for _, _, process in running],
            timeout=CONTRACT_CLEANUP_GRACE_SECONDS,
        )
        raise

    completed = []
    try:
        for name, command, process in running:
            remaining = max(0, deadline - time.monotonic())
            stdout, stderr = process.communicate(timeout=remaining)
            completed.append(
                (
                    name,
                    subprocess.CompletedProcess(
                        command, process.returncode, stdout, stderr
                    ),
                )
            )
    except subprocess.TimeoutExpired:
        terminate_contracts(
            [process for _, _, process in running],
            timeout=CONTRACT_CLEANUP_GRACE_SECONDS,
        )
        completed = []
        for name, command, process in running:
            stdout, stderr = process.communicate()
            completed.append(
                (
                    name,
                    subprocess.CompletedProcess(
                        command, process.returncode, stdout, stderr
                    ),
                )
            )
        raise AssertionError(
            f"Codex bridge contract timed out after {timeout} seconds\n"
            f"{format_contract_results(completed)}"
        )
    except BaseException:
        terminate_contracts(
            [process for _, _, process in running],
            timeout=CONTRACT_CLEANUP_GRACE_SECONDS,
        )
        raise
    return completed


def run_bridge_contract(command, *, cwd, timeout, env=None):
    return run_bridge_contracts(
        [("single", command, env)], cwd=cwd, timeout=timeout
    )[0][1]


def bridge_scenario_groups():
    result = subprocess.run(
        [str(BRIDGE_SUITE), "--list-scenario-groups"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            "Could not list Codex bridge scenario groups\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    groups = tuple(line for line in result.stdout.splitlines() if line)
    if not groups:
        raise AssertionError("Codex bridge suite listed no scenario groups")
    return groups


def run_bridge_contract_groups(groups, *, cwd, timeout):
    invocations = []
    for group in groups:
        environment = os.environ.copy()
        environment["CODEX_BRIDGE_TEST_GROUP"] = group
        invocations.append((group, [str(BRIDGE_SUITE)], environment))
    return run_bridge_contracts(invocations, cwd=cwd, timeout=timeout)


class ContractRunnerTests(unittest.TestCase):
    def test_timeout_interrupts_the_contract_and_preserves_progress_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            cleanup_receipt = root / "cleaned"
            contract = root / "contract.sh"
            contract.write_text(
                """#!/usr/bin/env bash
trap 'touch "$1"; echo "contract interrupted last_started=receipt last_completed=none elapsed=1s"; exit 143' TERM
echo "scenario started name=receipt elapsed=0s"
while :; do sleep 1; done
""",
                encoding="utf-8",
            )
            contract.chmod(0o755)

            with self.assertRaises(AssertionError) as raised:
                run_bridge_contract(
                    [str(contract), str(cleanup_receipt)],
                    cwd=root,
                    timeout=2,
                )

            self.assertTrue(cleanup_receipt.is_file())
            self.assertIn("timed out after 2 seconds", str(raised.exception))
            self.assertIn("last_started=receipt", str(raised.exception))
            self.assertIn("last_completed=none", str(raised.exception))

    def test_parallel_timeout_interrupts_and_reports_every_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            contract = root / "contract.sh"
            contract.write_text(
                """#!/usr/bin/env bash
if [ "$1" = alpha ]; then
  touch "$2"
  echo "contract cleanup last_started=alpha last_completed=alpha elapsed=0s"
  exit 0
fi
trap 'touch "$2"; echo "contract cleanup last_started=$1 last_completed=none elapsed=1s"; exit 143' TERM
echo "scenario started name=$1 elapsed=0s"
while :; do sleep 1; done
""",
                encoding="utf-8",
            )
            contract.chmod(0o755)
            receipts = [root / "alpha-cleaned", root / "beta-cleaned"]
            invocations = [
                (name, [str(contract), name, str(receipt)], None)
                for name, receipt in zip(("alpha", "beta"), receipts, strict=True)
            ]

            with self.assertRaises(AssertionError) as raised:
                run_bridge_contracts(invocations, cwd=root, timeout=2)

            failure = str(raised.exception)
            for name, receipt in zip(("alpha", "beta"), receipts, strict=True):
                self.assertTrue(receipt.is_file())
                self.assertIn(f"contract shard={name}", failure)
                self.assertIn(f"last_started={name}", failure)

    def test_waiting_error_interrupts_every_started_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            cleanup_receipt = root / "cleaned"
            ready = root / "ready"
            hanging_contract = root / "hanging.sh"
            hanging_contract.write_text(
                """#!/usr/bin/env bash
trap 'touch "$1"; exit 143' TERM
touch "$2"
sleep 5
touch "$1"
""",
                encoding="utf-8",
            )
            hanging_contract.chmod(0o755)
            invalid_utf8 = [
                os.fsdecode(os.environ.get("PYTHON", "python3")),
                "-c",
                (
                    "import os, pathlib, time\n"
                    f"ready = pathlib.Path({str(ready)!r})\n"
                    "while not ready.exists():\n"
                    "    time.sleep(0.01)\n"
                    "os.write(1, b'\\xff')\n"
                ),
            ]

            with self.assertRaises(UnicodeDecodeError):
                run_bridge_contracts(
                    [
                        ("invalid-output", invalid_utf8, None),
                        (
                            "hanging",
                            [str(hanging_contract), str(cleanup_receipt), str(ready)],
                            None,
                        ),
                    ],
                    cwd=root,
                    timeout=30,
                )

            self.assertTrue(cleanup_receipt.is_file())


class CodexBridgeSuiteTests(unittest.TestCase):
    def assert_contract_passed(self, result):
        self.assertEqual(
            result.returncode,
            0,
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )

    def owned_paths(self, output):
        ownership = re.search(
            r"contract ownership work=(\S+) tmux_socket=(\S+) resource_root=(\S+)",
            output,
        )
        self.assertIsNotNone(ownership, output)
        return tuple(pathlib.Path(path) for path in ownership.groups())

    def assert_owned_resources_are_absent(self, output):
        process_list = subprocess.run(
            ["ps", "-axo", "command="],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        for path in self.owned_paths(output):
            self.assertFalse(path.exists())
            self.assertNotIn(str(path), process_list)

    def test_scenario_groups_are_listed_and_unknown_groups_fail_before_setup(self):
        scenario_groups = bridge_scenario_groups()
        self.assertGreater(len(scenario_groups), 1)
        self.assertEqual(len(scenario_groups), len(set(scenario_groups)))

        environment = os.environ.copy()
        environment["CODEX_BRIDGE_TEST_GROUP"] = "not-a-scenario-group"
        result = run_bridge_contract(
            [str(BRIDGE_SUITE)], cwd=ROOT, timeout=5, env=environment
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown scenario group", result.stderr)
        self.assertNotIn("contract ownership", result.stdout)

    def test_end_to_end_bridge_contract(self):
        scenario_groups = bridge_scenario_groups()
        self.assertGreater(len(scenario_groups), 1)
        self.assertEqual(len(scenario_groups), len(set(scenario_groups)))

        started = time.monotonic()
        results = run_bridge_contract_groups(
            scenario_groups,
            cwd=ROOT,
            timeout=180,
        )
        elapsed = time.monotonic() - started
        print(
            f"[test] Codex bridge contract: {len(scenario_groups)} shards "
            f"in {elapsed:.1f}s"
        )
        failures = [result for _, result in results if result.returncode != 0]
        self.assertFalse(failures, format_contract_results(results))
        for _, result in results:
            self.assert_owned_resources_are_absent(result.stdout)

        with tempfile.TemporaryDirectory() as directory:
            test_root = pathlib.Path(directory)
            temp_root = test_root / "tmp"
            tmux_root = test_root / "tmux"
            temp_root.mkdir()
            tmux_root.mkdir()
            environment = os.environ.copy()
            environment.update(
                {
                    "CODEX_BRIDGE_TEST_FAIL_SCENARIO": "receipt",
                    "CODEX_BRIDGE_TEST_SCENARIO": "receipt",
                    "TMPDIR": str(temp_root),
                    "TMUX_TMPDIR": str(tmux_root),
                }
            )

            failed = run_bridge_contract(
                [str(BRIDGE_SUITE)], cwd=ROOT, timeout=30, env=environment
            )
            self.assertNotEqual(failed.returncode, 0, failed.stdout)
            self.assertIn("forced assertion failure", failed.stdout)
            self.assertIn("last_started=receipt", failed.stdout)
            self.assertIn("last_completed=none", failed.stdout)
            self.assert_owned_resources_are_absent(failed.stdout)

            environment.pop("CODEX_BRIDGE_TEST_FAIL_SCENARIO")
            retry = run_bridge_contract(
                [str(BRIDGE_SUITE)], cwd=ROOT, timeout=30, env=environment
            )
            self.assert_contract_passed(retry)
            self.assert_owned_resources_are_absent(retry.stdout)

            environment["CODEX_BRIDGE_TEST_HOLD_SCENARIO"] = "receipt"
            with self.assertRaises(AssertionError) as raised:
                run_bridge_contract(
                    [str(BRIDGE_SUITE)], cwd=ROOT, timeout=10, env=environment
                )
            timeout_failure = str(raised.exception)
            self.assertIn("timed out after 10 seconds", timeout_failure)
            self.assertIn("last_started=receipt", timeout_failure)
            self.assertIn("last_completed=none", timeout_failure)
            self.assert_owned_resources_are_absent(timeout_failure)

            environment.pop("CODEX_BRIDGE_TEST_HOLD_SCENARIO")

            invocations = [
                (name, [str(BRIDGE_SUITE)], environment.copy())
                for name in ("isolation-a", "isolation-b")
            ]
            results = run_bridge_contracts(invocations, cwd=ROOT, timeout=30)
            for _, concurrent_result in results:
                self.assert_contract_passed(concurrent_result)
                self.assert_owned_resources_are_absent(concurrent_result.stdout)
            sockets = [self.owned_paths(item.stdout)[1] for _, item in results]
            self.assertEqual(len(set(sockets)), 2)


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
