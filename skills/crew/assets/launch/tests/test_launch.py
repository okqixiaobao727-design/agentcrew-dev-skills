#!/usr/bin/env python3
"""Drive the launch script from its command line against a stand-in harness home and driver.

The fixture is the harness's own two records and nothing else: a per-pid session registry entry
under `sessions/`, and a session transcript under `projects/`, both inside a temporary directory
the script is pointed at through `CLAUDE_CONFIG_DIR` — the environment override the driver and
monitor suites already inject their harness home with. Assertions are on external behavior only:
the exit code, stdout and stderr, and the command line the stub driver recorded.
"""

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest


TESTS_DIR = pathlib.Path(__file__).resolve().parent
LAUNCH = TESTS_DIR.parent / "launch.py"
STUB_DRIVER = TESTS_DIR / "stub_driver.py"
STUB_STDOUT = "stub driver ran\n"

# What the harness's records hold for the coordinator this fixture stands for.
COORDINATOR_NAME = "crew-coordinator-1f"
SESSION_ID = "2cd60d75-fa21-4d9c-adf2-b4073f60fbb6"
# The slug the harness names a project's transcript directory with: the session's cwd, flattened.
PROJECT_SLUG = "-Users-someone-repo"
PERMISSION_MODE = "acceptEdits"
STARTUP_MODE = "default"
SWITCHED_MODE = "bypassPermissions"

RUN_DIR_NAME = ".crew"
TABLE_NAME = "wave-table.json"


class Fixture:
    """A temporary harness home — one registry entry, one transcript — and a run directory."""

    def __init__(self):
        # Resolved, because the command line the script composes carries resolved paths: a
        # temporary directory reached by one spelling and recorded as another compares unequal
        # for no reason of the launch's.
        self.root = pathlib.Path(tempfile.mkdtemp()).resolve()
        self.config_dir = self.root / "claude-config"
        self.config_dir.mkdir()
        self.stub_dir = self.root / "stub"
        self.stub_dir.mkdir()
        self.run_dir = self.root / "crewtask" / "7"
        self.run_dir.mkdir(parents=True)

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)

    # --- the harness's own records --------------------------------------------------------

    def registry(self, pid, name=COORDINATOR_NAME, session=SESSION_ID, **fields):
        """The per-pid session registry entry the harness writes for a live session."""
        entry = {"pid": pid, "sessionId": session, "cwd": str(self.root), "name": name}
        entry.update(fields)
        for key, value in list(entry.items()):
            if value is None:
                del entry[key]
        directory = self.config_dir / "sessions"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{pid}.json").write_text(json.dumps(entry) + "\n")

    def transcript(self, modes, session=SESSION_ID, trailing=True):
        """A session transcript whose entries record `modes`, oldest first.

        A transcript is a mixture: entries that carry the mode and entries that do not, the newest
        line of all being one that does not. Resolution reads the newest entry that carries one.
        """
        lines = []
        for mode in modes:
            lines.append({"type": "user", "sessionId": session, "message": {"role": "user"}})
            lines.append(
                {"type": "permission-mode", "permissionMode": mode, "sessionId": session}
            )
        if trailing:
            lines.append(
                {"type": "assistant", "sessionId": session, "message": {"role": "assistant"}}
            )
        path = self.config_dir / "projects" / PROJECT_SLUG / f"{session}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(line) + "\n" for line in lines))
        return path

    def wave_table(self):
        """The record that makes this run one already in flight, which the driver's start adopts."""
        table = self.run_dir / RUN_DIR_NAME / TABLE_NAME
        table.parent.mkdir(parents=True, exist_ok=True)
        table.write_text(json.dumps({"run": {}, "waves": []}) + "\n")
        return table

    # --- running it -----------------------------------------------------------------------

    def environment(self, overrides=None):
        environment = dict(os.environ)
        environment["CLAUDE_CONFIG_DIR"] = str(self.config_dir)
        environment["AGENTCREW_STUB_DIR"] = str(self.stub_dir)
        environment.pop("AGENTCREW_STUB_DRIVER_EXIT", None)
        environment.update(overrides or {})
        return environment

    def argv(self, extra=()):
        return [
            sys.executable, str(LAUNCH), str(self.run_dir),
            "--driver", str(STUB_DRIVER), *extra,
        ]

    def launch(self, extra=(), env_overrides=None):
        """Run the launcher as a direct child, so this process is the invoking shell's stand-in."""
        return subprocess.run(
            self.argv(extra), capture_output=True, text=True,
            env=self.environment(env_overrides), cwd=str(self.root),
        )

    def launch_below_a_shell(self, extra=()):
        """Run the launcher one process further down, as a real shell puts it below the session.

        The middle process stands in for the shell the coordinator's command runs in: the registry
        entry belongs to this process, two levels above the launcher, so nothing but a walk up the
        ancestry can find it.
        """
        relay = "import subprocess, sys; sys.exit(subprocess.run(sys.argv[1:]).returncode)"
        return subprocess.run(
            [sys.executable, "-c", relay, *self.argv(extra)], capture_output=True, text=True,
            env=self.environment(), cwd=str(self.root),
        )

    # --- what the driver was launched with ------------------------------------------------

    def driver_calls(self):
        path = self.stub_dir / "driver-calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line]


def flag(argv, name):
    """The value that follows `name` on that command line, or None where it does not appear."""
    return argv[argv.index(name) + 1] if name in argv else None


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()
        self.addCleanup(self.fixture.close)

    def one_call(self, result):
        """The single driver command line this launch composed, exit code and stdout checked."""
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.fixture.driver_calls()
        self.assertEqual(len(calls), 1, calls)
        return calls[0]

    # --- the three resolved values --------------------------------------------------------

    def test_the_composed_command_line_carries_the_resolved_pid_name_and_mode(self):
        """The whole of the start-up: the harness's records become the driver's command line."""
        self.fixture.registry(os.getpid())
        self.fixture.transcript([PERMISSION_MODE])

        result = self.fixture.launch()

        call = self.one_call(result)
        self.assertEqual(call["argv"][0], "start")
        self.assertEqual(flag(call["argv"], "--feature-dir"), str(self.fixture.run_dir))
        self.assertEqual(flag(call["argv"], "--coordinator-pid"), str(os.getpid()))
        self.assertEqual(flag(call["argv"], "--coordinator-name"), COORDINATOR_NAME)
        self.assertEqual(flag(call["argv"], "--permission-mode"), PERMISSION_MODE)
        # The driver's own output is what the coordinator reads, and this launch adds nothing to it.
        self.assertEqual(result.stdout, STUB_STDOUT)

    def test_the_coordinator_is_found_above_the_shell_the_command_ran_in(self):
        """The pid is the session's, not the shell's: a walk up the ancestry finds the entry."""
        self.fixture.registry(os.getpid())
        self.fixture.transcript([PERMISSION_MODE])

        result = self.fixture.launch_below_a_shell()

        call = self.one_call(result)
        self.assertEqual(flag(call["argv"], "--coordinator-pid"), str(os.getpid()))
        self.assertEqual(flag(call["argv"], "--coordinator-name"), COORDINATOR_NAME)

    def test_a_mode_switched_mid_session_is_the_mode_children_launch_under(self):
        """The newest entry that records a mode wins, not the one the session started on."""
        self.fixture.registry(os.getpid())
        self.fixture.transcript([STARTUP_MODE, STARTUP_MODE, SWITCHED_MODE])

        call = self.one_call(self.fixture.launch())

        self.assertEqual(flag(call["argv"], "--permission-mode"), SWITCHED_MODE)

    def test_the_driver_is_launched_in_the_directory_the_command_was_typed_in(self):
        """A run directory named relatively is the operator's own, so the cwd carries through."""
        self.fixture.registry(os.getpid())
        self.fixture.transcript([PERMISSION_MODE])

        call = self.one_call(self.fixture.launch())

        self.assertEqual(pathlib.Path(call["cwd"]).resolve(), self.fixture.root)

    # --- the values given explicitly ------------------------------------------------------

    def test_the_three_values_given_explicitly_need_no_harness_records_at_all(self):
        """What the failure message instructs: passing them by hand is a complete substitute."""
        result = self.fixture.launch(extra=[
            "--coordinator-pid", "1504", "--coordinator-name", "given-by-hand",
            "--permission-mode", SWITCHED_MODE,
        ])

        call = self.one_call(result)
        self.assertEqual(flag(call["argv"], "--coordinator-pid"), "1504")
        self.assertEqual(flag(call["argv"], "--coordinator-name"), "given-by-hand")
        self.assertEqual(flag(call["argv"], "--permission-mode"), SWITCHED_MODE)

    # --- what an unresolvable value does --------------------------------------------------

    def test_an_unresolvable_permission_mode_aborts_and_names_the_flag_to_pass(self):
        """No mode is guessed: a wrong one launches every child in the wrong permission regime."""
        self.fixture.registry(os.getpid())
        self.fixture.transcript([])

        result = self.fixture.launch()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--permission-mode", result.stderr)
        self.assertEqual(self.fixture.driver_calls(), [])

    def test_a_missing_transcript_aborts_and_names_the_flag_to_pass(self):
        """The registry entry alone resolves no mode, and a session with no transcript is that."""
        self.fixture.registry(os.getpid())

        result = self.fixture.launch()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--permission-mode", result.stderr)
        self.assertEqual(self.fixture.driver_calls(), [])

    def test_a_missing_registry_entry_aborts_and_names_the_flags_to_pass(self):
        """Nothing above this process is a session the harness recorded, so nothing is resolved."""
        self.fixture.transcript([PERMISSION_MODE])

        result = self.fixture.launch()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--coordinator-pid", result.stderr)
        self.assertIn("--coordinator-name", result.stderr)
        self.assertEqual(self.fixture.driver_calls(), [])

    def test_a_registry_entry_carrying_no_name_aborts_and_names_the_flag_to_pass(self):
        """A record found but unreadable is a failed resolution, not a name to invent."""
        self.fixture.registry(os.getpid(), name=None)
        self.fixture.transcript([PERMISSION_MODE])

        result = self.fixture.launch()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--coordinator-name", result.stderr)
        self.assertEqual(self.fixture.driver_calls(), [])

    def test_a_pid_the_harness_does_not_record_aborts_even_when_the_other_two_are_given(self):
        """No value reaches the driver unresolved: an empty pid strands every ruling of the run."""
        result = self.fixture.launch(extra=[
            "--coordinator-name", "given-by-hand", "--permission-mode", PERMISSION_MODE,
        ])

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--coordinator-pid", result.stderr)
        self.assertEqual(self.fixture.driver_calls(), [])

    def test_a_pid_left_to_the_harness_is_resolved_beside_values_given_by_hand(self):
        """The two given by hand are taken as given, and the third is still read off the harness."""
        self.fixture.registry(os.getpid())

        result = self.fixture.launch(extra=[
            "--coordinator-name", "given-by-hand", "--permission-mode", PERMISSION_MODE,
        ])

        call = self.one_call(result)
        self.assertEqual(flag(call["argv"], "--coordinator-pid"), str(os.getpid()))
        self.assertEqual(flag(call["argv"], "--coordinator-name"), "given-by-hand")

    def test_a_harness_home_given_relatively_is_named_absolutely_when_it_fails(self):
        """ADR-0007: the directory a failure names is the one every reader of it can open."""
        result = self.fixture.launch(env_overrides={"CLAUDE_CONFIG_DIR": "claude-config"})

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(str(self.fixture.config_dir / "sessions"), result.stderr)
        self.assertEqual(self.fixture.driver_calls(), [])

    def test_the_instruction_names_only_the_values_still_unresolved(self):
        """A value already given by hand is not a value the failure asks the operator for again."""
        result = self.fixture.launch(extra=[
            "--coordinator-pid", "1504", "--permission-mode", PERMISSION_MODE,
        ])

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--coordinator-name", result.stderr)
        self.assertNotIn("--coordinator-pid", result.stderr)
        self.assertNotIn("--permission-mode", result.stderr)
        self.assertEqual(self.fixture.driver_calls(), [])

    # --- starting, adopting, and what comes back ------------------------------------------

    def test_a_run_already_in_flight_is_adopted_rather_than_doubled(self):
        """One `start` against the same run directory, which is what the driver adopts on."""
        self.fixture.registry(os.getpid())
        self.fixture.transcript([PERMISSION_MODE])
        table = self.fixture.wave_table()
        recorded = table.read_bytes()

        call = self.one_call(self.fixture.launch())

        self.assertEqual(call["argv"][0], "start")
        self.assertEqual(flag(call["argv"], "--feature-dir"), str(self.fixture.run_dir))
        self.assertEqual(table.read_bytes(), recorded)

    def test_the_drivers_exit_code_is_the_launchers(self):
        """The coordinator reads the driver's outcome, so nothing may swallow or rewrite it."""
        self.fixture.registry(os.getpid())
        self.fixture.transcript([PERMISSION_MODE])

        result = self.fixture.launch(env_overrides={"AGENTCREW_STUB_DRIVER_EXIT": "1"})

        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(self.fixture.driver_calls()), 1)


if __name__ == "__main__":
    unittest.main()
