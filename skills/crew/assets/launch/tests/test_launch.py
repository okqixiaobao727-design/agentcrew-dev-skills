#!/usr/bin/env python3
"""Drive the launch script from its command line against a stand-in harness home, tmux and driver.

The fixture is the harness's own two records and nothing else: a per-pid session registry entry
under `sessions/`, and a session transcript under `projects/`, both inside a temporary directory
the script is pointed at through `CLAUDE_CONFIG_DIR` — the environment override the driver and
monitor suites already inject their harness home with. The stub tmux really runs the window it is
asked for, in a session of its own, because a driver that outlives the process that launched it is
the whole of what this script now does. Assertions are on external behavior only: the exit code,
stdout and stderr, the command line the stub driver recorded, and the files the run directory
holds.
"""

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


TESTS_DIR = pathlib.Path(__file__).resolve().parent
LAUNCH = TESTS_DIR.parent / "launch.py"
STUB_DRIVER = TESTS_DIR / "stub_driver.py"
STUB_TMUX = TESTS_DIR / "stub_tmux.py"
STUB_STDOUT = "stub driver ran\n"
# The window the driver is put in, and the tmux session the stub server answers with.
DRIVER_WINDOW = "crew-driver"
TMUX_SESSION = "$9"
# The three files this end of the run reads and writes in the run directory.
DRIVER_RECORD = "driver.pid"
WAKE_NAME = "wake.json"
DRIVER_LOG = "driver.log"
# The wake the stub driver ends on unless a test names another, and how long a launch is given
# before it is called hung.
DEFAULT_WAKE = {"reason": "run-complete", "ticket": None, "pointer": "report.md"}
LAUNCH_TIMEOUT = 60.0

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
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self._link_stub("tmux", STUB_TMUX)
        (self.stub_dir / "tmux-session").write_text(TMUX_SESSION)
        self.started = []

    def _link_stub(self, name, script):
        target = self.bin_dir / name
        target.write_text("#!/bin/sh\nexec %s %s \"$@\"\n" % (sys.executable, script))
        target.chmod(0o755)

    def close(self):
        for process in self.started:
            if process.poll() is None:
                process.kill()
                process.communicate()
        shutil.rmtree(self.root, ignore_errors=True)

    # --- what the run directory holds -----------------------------------------------------

    @property
    def crew_dir(self):
        """The run's own directory inside the feature, which the launcher makes."""
        return self.run_dir / ".crew"

    def recorded_driver(self):
        """The pid the run directory names as its driver, or None where it names none."""
        path = self.crew_dir / DRIVER_RECORD
        return int(path.read_text().strip()) if path.exists() else None

    def wake(self, snapshot):
        """Leave a wake snapshot in the run directory, as a driver's deliberate exit does.

        Put in place by rename, as the driver puts it, so a waiter polling for it never reads
        half of one.
        """
        self.crew_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.crew_dir / f"{WAKE_NAME}.tmp"
        temporary.write_text(json.dumps(snapshot) + "\n")
        os.replace(temporary, self.crew_dir / WAKE_NAME)

    def record_driver(self, pid):
        """Name a process as this run's driver, as a driver's loop names itself."""
        self.crew_dir.mkdir(parents=True, exist_ok=True)
        (self.crew_dir / DRIVER_RECORD).write_text(f"{pid}\n")

    def dead_pid(self):
        """A pid that has certainly gone: a process this fixture started and then reaped."""
        process = subprocess.Popen([sys.executable, "-c", ""])
        process.wait()
        return process.pid

    def wait_for(self, condition, timeout=LAUNCH_TIMEOUT):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return True
            time.sleep(0.1)
        return False

    def tmux_calls(self):
        path = self.stub_dir / "tmux-calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line]

    def windows(self):
        """Every window the stub tmux server was asked to open."""
        return [call for call in self.tmux_calls() if call["argv"][:1] == ["new-window"]]

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
        environment["PATH"] = f"{self.bin_dir}{os.pathsep}{environment['PATH']}"
        environment["CLAUDE_CONFIG_DIR"] = str(self.config_dir)
        environment["AGENTCREW_STUB_DIR"] = str(self.stub_dir)
        for name in list(environment):
            if name.startswith("AGENTCREW_STUB_DRIVER_"):
                del environment[name]
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
            self.argv(extra), capture_output=True, text=True, timeout=LAUNCH_TIMEOUT,
            env=self.environment(env_overrides), cwd=str(self.root),
        )

    def start_launch(self, extra=(), env_overrides=None):
        """Start the launcher and leave it waiting; returns the waiter still blocked on its run."""
        process = subprocess.Popen(
            self.argv(extra), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=self.environment(env_overrides), cwd=str(self.root),
        )
        self.started.append(process)
        return process

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

    def one_window(self):
        """The single tmux window this launch opened, which is the driver's own."""
        windows = self.fixture.windows()
        self.assertEqual(len(windows), 1, windows)
        return windows[0]["argv"]

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
        # The driver's own wake is what the coordinator reads, and this waiter adds nothing to it.
        self.assertEqual(json.loads(result.stdout), DEFAULT_WAKE)

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

    def test_a_driver_that_failed_still_wakes_the_coordinator_with_its_own_snapshot(self):
        """The outcome is the snapshot's own, not an exit code: the driver's exit is its window's.

        A waiter that reported the driver's status instead would be reporting the status of a
        process it does not have and cannot wait on.
        """
        self.fixture.registry(os.getpid())
        self.fixture.transcript([PERMISSION_MODE])
        failed = {"reason": "driver-error", "ticket": "01", "detail": "something outside the table"}

        result = self.fixture.launch(env_overrides={
            "AGENTCREW_STUB_DRIVER_EXIT": "2",
            "AGENTCREW_STUB_DRIVER_WAKE": json.dumps(failed),
        })

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), failed)


class DetachmentTests(unittest.TestCase):
    """The driver belongs to its own window, and this process is a waiter that costs nothing.

    A driver held as a background task of the coordinator's session was killed by the harness's
    own task management 45 minutes into a live run, with no user input and no model turn, and the
    run stalled for forty minutes (#103). Nothing here may depend on the launching process, or on
    the session it was launched from, living any longer than it happens to.
    """

    def setUp(self):
        self.fixture = Fixture()
        self.addCleanup(self.fixture.close)
        self.fixture.registry(os.getpid())
        self.fixture.transcript([PERMISSION_MODE])

    def wake(self, process, timeout=LAUNCH_TIMEOUT):
        """The snapshot that waiter printed, once it has ended of its own accord."""
        out, err = process.communicate(timeout=timeout)
        self.assertEqual(process.returncode, 0, err)
        return json.loads(out)

    # --- the window the driver runs in ----------------------------------------------------

    def test_the_driver_runs_in_a_detached_window_of_the_callers_own_session(self):
        result = self.fixture.launch()

        self.assertEqual(result.returncode, 0, result.stderr)
        windows = self.fixture.windows()
        self.assertEqual(len(windows), 1, windows)
        argv = windows[0]["argv"]
        self.assertIn("-d", argv)
        self.assertEqual(flag(argv, "-n"), DRIVER_WINDOW)
        self.assertEqual(flag(argv, "-t"), TMUX_SESSION)

    def test_the_driver_is_told_the_session_its_own_window_is_in(self):
        """One session holds the driver's window, the dashboard's, and every child's."""
        result = self.fixture.launch()

        call = self.fixture.driver_calls()[0]
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(flag(call["argv"], "--tmux-session"), TMUX_SESSION)

    def test_the_drivers_output_is_kept_in_the_run_directory(self):
        """No task output file collects it any more, so a driver that dies leaves a trail."""
        result = self.fixture.launch()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            self.fixture.wait_for(lambda: (self.fixture.crew_dir / DRIVER_LOG).exists()),
            "the driver's window kept no log",
        )
        self.assertIn(STUB_STDOUT.strip(), (self.fixture.crew_dir / DRIVER_LOG).read_text())

    def test_a_session_tmux_cannot_name_is_a_launch_failure_and_nothing_is_started(self):
        (self.fixture.stub_dir / "tmux-no-session").write_text("")

        result = self.fixture.launch()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("tmux session", result.stderr)
        self.assertEqual(self.fixture.driver_calls(), [])

    def test_a_window_tmux_refuses_is_a_launch_failure(self):
        (self.fixture.stub_dir / "tmux-new-window-fails").write_text("")

        result = self.fixture.launch()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("window", result.stderr)

    def test_a_driver_that_never_names_itself_is_a_failure_pointing_at_its_log(self):
        """A launch is only over when the run has a driver, and the log is where to read why."""
        inert = self.fixture.root / "inert.py"
        inert.write_text("raise SystemExit(1)\n")

        result = self.fixture.launch(
            extra=["--driver", str(inert)],
            env_overrides={"CREW_LAUNCH_HANDSHAKE_SECONDS": "1"},
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("the driver never started", result.stderr)
        self.assertIn(DRIVER_LOG, result.stderr)

    # --- the driver outliving what launched it ---------------------------------------------

    def test_the_driver_survives_the_process_that_launched_it(self):
        """The whole ticket in one test: kill the launcher, and the run carries on to its end."""
        waiter = self.fixture.start_launch(env_overrides={"AGENTCREW_STUB_DRIVER_HOLD": "3"})
        self.assertTrue(
            self.fixture.wait_for(lambda: self.fixture.recorded_driver() is not None),
            "the driver never named itself",
        )

        waiter.kill()
        waiter.communicate()

        self.assertTrue(
            self.fixture.wait_for(lambda: (self.fixture.crew_dir / WAKE_NAME).exists()),
            "the driver died with the process that launched it",
        )

    def test_a_waiter_killed_and_re_created_loses_nothing(self):
        """A waiter is stateless, so re-typing the command is the whole of the recovery."""
        first = self.fixture.start_launch(env_overrides={"AGENTCREW_STUB_DRIVER_HOLD": "3"})
        self.assertTrue(
            self.fixture.wait_for(lambda: self.fixture.recorded_driver() is not None),
            "the driver never named itself",
        )
        first.kill()
        first.communicate()

        second = self.fixture.start_launch()

        self.assertEqual(self.wake(second), DEFAULT_WAKE)
        self.assertEqual(len(self.fixture.driver_calls()), 1, "a second driver was started")

    def test_a_waiter_surfaces_a_snapshot_written_after_it_started(self):
        self.fixture.record_driver(os.getpid())

        waiter = self.fixture.start_launch()
        self.assertFalse(
            self.fixture.wait_for(lambda: waiter.poll() is not None, timeout=2.0),
            "the waiter ended before the run had anything to say",
        )
        self.fixture.wake({"reason": "judgment-needed", "ticket": "04"})

        self.assertEqual(self.wake(waiter)["ticket"], "04")

    # --- adopt: one run, one driver ---------------------------------------------------------

    def test_a_second_command_on_a_live_run_attaches_without_starting_a_second_driver(self):
        """`/crew` stays safe to type at any moment, which is what adopt has always meant."""
        self.fixture.record_driver(os.getpid())

        waiter = self.fixture.start_launch()
        self.fixture.wake(DEFAULT_WAKE)

        self.assertEqual(self.wake(waiter), DEFAULT_WAKE)
        self.assertEqual(self.fixture.driver_calls(), [], "a live run was driven twice")
        self.assertEqual(self.fixture.windows(), [])

    def test_a_run_whose_driver_has_gone_is_driven_again(self):
        """The record names a process that is not running, which is a run with no driver."""
        self.fixture.record_driver(self.fixture.dead_pid())

        result = self.fixture.launch()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.fixture.driver_calls()), 1)

    def test_the_wake_of_the_cycle_just_ended_is_not_answered_as_this_ones(self):
        """A snapshot already ruled on would send the coordinator round the same loop again."""
        self.fixture.wake({"reason": "judgment-needed", "ticket": "99"})

        result = self.fixture.launch()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), DEFAULT_WAKE)

    def test_a_wake_already_standing_on_a_live_run_is_answered_rather_than_thrown_away(self):
        """Nothing is cleared on the attach path: the driver alive there is still writing it."""
        self.fixture.record_driver(os.getpid())
        self.fixture.wake({"reason": "judgment-needed", "ticket": "04"})

        result = self.fixture.launch()

        self.assertEqual(json.loads(result.stdout)["ticket"], "04")

    # --- the two endings that are not snapshots ---------------------------------------------

    def test_a_driver_killed_under_the_waiter_is_said_rather_than_waited_on_forever(self):
        """The stall this ticket is about, seen from the coordinator's end: a driver that was
        killed left no wake, and a waiter that blocked on one would be the old forty minutes.

        The driver here is a process that does nothing but exist, named as this run's — attached
        to, because it is alive, and then killed under the waiter with nothing run on its way out.
        """
        standing = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        self.addCleanup(standing.wait)
        self.addCleanup(standing.kill)
        self.fixture.record_driver(standing.pid)
        waiter = self.fixture.start_launch()
        self.assertFalse(
            self.fixture.wait_for(lambda: waiter.poll() is not None, timeout=2.0),
            "the waiter ended while its driver was still running",
        )

        standing.kill()
        # Reaped, because a killed process this fixture is the parent of stays in the process
        # table until it is — and a `kill -0` cannot tell a zombie from a running driver.
        standing.wait()
        out, err = waiter.communicate(timeout=LAUNCH_TIMEOUT)

        self.assertEqual(waiter.returncode, 0, err)
        self.assertIn("was killed", out)
        self.assertIn(f"/crew {self.fixture.run_dir}", out)
        self.assertEqual(self.fixture.driver_calls(), [], "a live run was driven twice")

    # --- the driver stopped by hand ---------------------------------------------------------

    def test_a_driver_that_left_no_wake_is_said_in_one_line(self):
        """The operator's own Ctrl-C, or a wake that could not be written: either way no ruling
        was asked for, so no snapshot is invented and the log is named as what tells them apart."""
        result = self.fixture.launch(env_overrides={
            "AGENTCREW_STUB_DRIVER_STOPPED": "1", "AGENTCREW_STUB_DRIVER_HOLD": "1",
        })

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("without leaving a wake snapshot", result.stdout)
        self.assertIn(DRIVER_LOG, result.stdout)
        self.assertIn(f"/crew {self.fixture.run_dir}", result.stdout)


if __name__ == "__main__":
    unittest.main()
