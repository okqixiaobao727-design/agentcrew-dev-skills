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
# tmux's own name for the pane a process runs in, and the pane this fixture's coordinator sits in.
# It is what the launcher hands the driver as the one target a wake with no waiter is typed into.
TMUX_PANE = "TMUX_PANE"
COORDINATOR_PANE = "%4"
# The three files this end of the run reads and writes in the run directory.
DRIVER_RECORD = "driver.pid"
WAITER_RECORD = "waiter.pid"
WAKE_NAME = "wake.json"
DRIVER_LOG = "driver.log"
# The wake the stub driver ends on unless a test names another, and how long a launch is given
# before it is called hung.
DEFAULT_WAKE = {"reason": "run-complete", "ticket": None, "pointer": "report.md"}
LAUNCH_TIMEOUT = 60.0

# What the harness's records hold for the coordinator this fixture stands for. The socket is
# spelled the way the harness spells it — a path under the directory it happened to bind in — and
# the address the driver is handed is that path under the `uds:` scheme.
COORDINATOR_NAME = "crew-coordinator-1f"
SESSION_ID = "2cd60d75-fa21-4d9c-adf2-b4073f60fbb6"
MESSAGING_SOCKET = "/private/tmp/cc-socks-501/1504.sock"
COORDINATOR_ADDRESS = f"uds:{MESSAGING_SOCKET}"
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

    def record_waiter(self, pid):
        """Add a process to this run's waiter record, as a second `/crew` adds the one it attaches.

        Appended rather than written over, because the record names every waiter blocking on the
        run: a second one attaching does not unname the first.
        """
        self.crew_dir.mkdir(parents=True, exist_ok=True)
        with (self.crew_dir / WAITER_RECORD).open("a") as handle:
            handle.write(f"{pid}\n")

    def recorded_waiters(self):
        """Every pid the run directory names as a waiter, in the order they attached."""
        path = self.crew_dir / WAITER_RECORD
        if not path.exists():
            return []
        return [line.strip() for line in path.read_text().splitlines() if line.strip()]

    def recorded_waiter(self):
        """The one pid the record names, or None where it names none — the ordinary case.

        A test that means to read a run with two waiters on it reads `recorded_waiters`; this is
        for the runs that have one, and it fails loudly rather than picking a name out of several.
        """
        named = self.recorded_waiters()
        if not named:
            return None
        assert len(named) == 1, f"the run names {len(named)} waiters: {named}"
        return int(named[0]) if named[0].isdigit() else named[0]

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

    def registry(self, pid, name=COORDINATOR_NAME, session=SESSION_ID,
                 socket=MESSAGING_SOCKET, **fields):
        """The per-pid session registry entry the harness writes for a live session."""
        entry = {
            "pid": pid, "sessionId": session, "cwd": str(self.root), "name": name,
            "messagingSocketPath": socket,
        }
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
        environment["CREW_LAUNCH_PERMISSION_MODE_SECONDS"] = "0"
        for name in list(environment):
            if name.startswith("AGENTCREW_STUB_DRIVER_"):
                del environment[name]
        # Taken out rather than inherited: whether this suite runs inside tmux is not what decides
        # which pane the driver is told about. The one case that wants a pane names one.
        environment.pop(TMUX_PANE, None)
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

    def handover_calls(self):
        path = self.stub_dir / "driver-handover-calls.jsonl"
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

    # --- the four resolved values ---------------------------------------------------------

    def test_the_composed_command_line_carries_the_resolved_session_identity_and_mode(self):
        """The whole of the start-up: the harness's records become the driver's command line."""
        self.fixture.registry(os.getpid())
        self.fixture.transcript([PERMISSION_MODE])

        result = self.fixture.launch()

        call = self.one_call(result)
        self.assertEqual(call["argv"][0], "start")
        self.assertEqual(flag(call["argv"], "--feature-dir"), str(self.fixture.run_dir))
        self.assertEqual(flag(call["argv"], "--coordinator-pid"), str(os.getpid()))
        self.assertEqual(flag(call["argv"], "--coordinator-name"), COORDINATOR_NAME)
        self.assertEqual(flag(call["argv"], "--coordinator-session"), SESSION_ID)
        self.assertEqual(flag(call["argv"], "--coordinator-address"), COORDINATOR_ADDRESS)
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

    def test_a_fresh_session_waits_for_its_first_permission_mode(self):
        """A slash-command turn records its mode only after the launcher has started."""
        self.fixture.registry(os.getpid())
        transcript = self.fixture.transcript([])
        os.utime(transcript, ns=(0, transcript.stat().st_mtime_ns))

        launch = self.fixture.start_launch(
            env_overrides={"CREW_LAUNCH_PERMISSION_MODE_SECONDS": "1"}
        )
        self.assertTrue(
            self.fixture.wait_for(lambda: transcript.stat().st_atime_ns > 0),
            "the launcher did not read the transcript before its permission-mode deadline",
        )
        self.fixture.transcript([SWITCHED_MODE])
        stdout, stderr = launch.communicate(timeout=LAUNCH_TIMEOUT)

        result = subprocess.CompletedProcess(launch.args, launch.returncode, stdout, stderr)
        call = self.one_call(result)
        self.assertEqual(flag(call["argv"], "--permission-mode"), SWITCHED_MODE)

    def test_the_driver_is_launched_in_the_directory_the_command_was_typed_in(self):
        """A run directory named relatively is the operator's own, so the cwd carries through."""
        self.fixture.registry(os.getpid())
        self.fixture.transcript([PERMISSION_MODE])

        call = self.one_call(self.fixture.launch())

        self.assertEqual(pathlib.Path(call["cwd"]).resolve(), self.fixture.root)

    def test_the_socket_the_harness_spelled_is_the_address_passed_on_unnormalised(self):
        """The receiver bound that literal; a realpath is an address nobody is listening on."""
        self.fixture.registry(os.getpid(), socket="/tmp/cc-socks/1504.sock")
        self.fixture.transcript([PERMISSION_MODE])

        call = self.one_call(self.fixture.launch())

        self.assertEqual(
            flag(call["argv"], "--coordinator-address"), "uds:/tmp/cc-socks/1504.sock"
        )

    # --- the values given explicitly ------------------------------------------------------

    def test_the_five_values_given_explicitly_need_no_harness_records_at_all(self):
        """What the failure message instructs: passing them by hand is a complete substitute."""
        result = self.fixture.launch(extra=[
            "--coordinator-pid", "1504", "--coordinator-name", "given-by-hand",
            "--coordinator-session", SESSION_ID,
            "--coordinator-address", "uds:/elsewhere/1504.sock",
            "--permission-mode", SWITCHED_MODE,
        ])

        call = self.one_call(result)
        self.assertEqual(flag(call["argv"], "--coordinator-pid"), "1504")
        self.assertEqual(flag(call["argv"], "--coordinator-name"), "given-by-hand")
        self.assertEqual(flag(call["argv"], "--coordinator-session"), SESSION_ID)
        self.assertEqual(flag(call["argv"], "--coordinator-address"), "uds:/elsewhere/1504.sock")
        self.assertEqual(flag(call["argv"], "--permission-mode"), SWITCHED_MODE)

    def test_an_explicit_hook_session_does_not_redirect_the_permission_mode_lookup(self):
        explicit_session = "3cd60d75-fa21-4d9c-adf2-b4073f60fbb6"
        self.fixture.registry(os.getpid())
        self.fixture.transcript([PERMISSION_MODE])

        result = self.fixture.launch(extra=["--coordinator-session", explicit_session])

        call = self.one_call(result)
        self.assertEqual(flag(call["argv"], "--coordinator-session"), explicit_session)
        self.assertEqual(flag(call["argv"], "--permission-mode"), PERMISSION_MODE)

    # --- what an unresolvable value does --------------------------------------------------

    def test_an_unresolvable_permission_mode_aborts_and_names_the_flag_to_pass(self):
        """No mode is guessed: a wrong one launches every child in the wrong permission regime."""
        self.fixture.registry(os.getpid())
        self.fixture.transcript([])

        result = self.fixture.launch()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--permission-mode", result.stderr)
        self.assertEqual(self.fixture.driver_calls(), [])

    def test_a_registry_entry_carrying_no_session_id_aborts_and_names_the_flag_to_pass(self):
        self.fixture.registry(os.getpid(), session=None)
        self.fixture.transcript([PERMISSION_MODE])

        result = self.fixture.launch()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--coordinator-session", result.stderr)
        self.assertEqual(self.fixture.driver_calls(), [])

    def test_a_registry_entry_carrying_no_socket_aborts_and_names_the_flag_to_pass(self):
        """No address is composed from the pid: a harness binding elsewhere would be unreachable."""
        self.fixture.registry(os.getpid(), socket=None)
        self.fixture.transcript([PERMISSION_MODE])

        result = self.fixture.launch()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--coordinator-address", result.stderr)
        self.assertIn("messagingSocketPath", result.stderr)
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

    def test_a_live_driver_does_not_bypass_coordinator_context_resolution(self):
        self.fixture.record_driver(os.getpid())
        self.fixture.wake(DEFAULT_WAKE)

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

    def test_the_driver_is_told_the_pane_this_command_was_typed_in(self):
        """The coordinator's own pane, which only this process can name: it is the one part of the
        run that runs inside it. A wake that reaches no waiter is typed there and nowhere else."""
        result = self.fixture.launch(env_overrides={TMUX_PANE: COORDINATOR_PANE})

        self.assertEqual(result.returncode, 0, result.stderr)
        call, = self.fixture.driver_calls()
        self.assertEqual(flag(call["argv"], "--coordinator-pane"), COORDINATOR_PANE)

    def test_a_launcher_in_no_pane_at_all_names_none(self):
        """Nothing is invented. A pane that cannot be named is a driver that types nothing, and
        the dashboard's own banner is what tells the operator instead."""
        result = self.fixture.launch()

        self.assertEqual(result.returncode, 0, result.stderr)
        call, = self.fixture.driver_calls()
        self.assertNotIn("--coordinator-pane", call["argv"])

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

    def test_a_different_coordinator_hands_over_the_live_driver_without_a_wake_gap(self):
        """The old waiter loses ownership only after the replacement waiter has gained it."""
        self.fixture.registry(os.getpid())
        self.fixture.transcript([PERMISSION_MODE])
        first = self.fixture.start_launch(env_overrides={
            "AGENTCREW_STUB_DRIVER_HOLD": "10",
            "AGENTCREW_STUB_DRIVER_SERVICE": "1",
            TMUX_PANE: COORDINATOR_PANE,
        })
        self.assertTrue(
            self.fixture.wait_for(lambda: self.fixture.recorded_driver() is not None),
            "the original Driver never named itself",
        )
        driver_pid = self.fixture.recorded_driver()
        second_session = "7dc60d75-fa21-4d9c-adf2-b4073f60fbb6"
        second_address = "uds:/private/tmp/cc-socks-501/2601.sock"
        second_pane = "%8"
        second_display = "$8"
        (self.fixture.stub_dir / "tmux-session").write_text(second_display)

        second = self.fixture.start_launch(extra=[
            "--coordinator-pid", "2601",
            "--coordinator-name", "crew-coordinator-2a",
            "--coordinator-session", second_session,
            "--coordinator-address", second_address,
            "--permission-mode", SWITCHED_MODE,
        ], env_overrides={TMUX_PANE: second_pane})

        first_out, first_err = first.communicate(timeout=LAUNCH_TIMEOUT)
        self.assertNotEqual(first.returncode, 0)
        self.assertEqual(first_out.strip(),
                         "crew: this waiter was superseded by a coordinator handover")
        self.assertEqual(first_err, "")
        self.assertTrue(
            self.fixture.wait_for(lambda: len(self.fixture.handover_calls()) == 1),
            "the live Driver never applied the Coordinator handover",
        )
        self.assertIsNone(second.poll(), "the new waiter returned before the Run woke")
        self.assertEqual(self.fixture.recorded_driver(), driver_pid)
        self.assertEqual(len(self.fixture.driver_calls()), 1, "handover started a second Driver")
        self.assertEqual(self.fixture.recorded_waiters(), [str(second.pid)])
        self.assertEqual(self.fixture.handover_calls(), [{
            "name": "crew-coordinator-2a",
            "pid": 2601,
            "harness_session": second_session,
            "address": second_address,
            "pane": second_pane,
            "permission_mode": SWITCHED_MODE,
            "display_session": second_display,
        }])

        (self.fixture.stub_dir / "wake-after-handover").touch()
        self.assertEqual(self.wake(second), DEFAULT_WAKE)
        self.assertEqual(self.fixture.recorded_waiters(), [])

    def test_concurrent_handover_requests_are_serviced_in_arrival_order(self):
        self.fixture.registry(os.getpid())
        self.fixture.transcript([PERMISSION_MODE])
        first = self.fixture.start_launch(env_overrides={
            "AGENTCREW_STUB_DRIVER_HOLD": "10",
            "AGENTCREW_STUB_DRIVER_SERVICE": "1",
            "AGENTCREW_STUB_DRIVER_SERVICE_GATE": "1",
        })
        self.assertTrue(
            self.fixture.wait_for(lambda: self.fixture.recorded_driver() is not None),
            "the original Driver never named itself",
        )

        def handover(pid, name, session, address):
            return self.fixture.start_launch(extra=[
                "--coordinator-pid", str(pid),
                "--coordinator-name", name,
                "--coordinator-session", session,
                "--coordinator-address", address,
                "--permission-mode", SWITCHED_MODE,
            ])

        second_address = "uds:/private/tmp/cc-socks-501/2601.sock"
        third_address = "uds:/private/tmp/cc-socks-501/3601.sock"
        second = handover(2601, "crew-coordinator-2a", "session-2a", second_address)
        self.assertTrue(
            self.fixture.wait_for(lambda: second.pid in map(int, self.fixture.recorded_waiters())),
            "the first handover request never reached attendance",
        )
        third = handover(3601, "crew-coordinator-3a", "session-3a", third_address)
        time.sleep(0.2)
        (self.fixture.stub_dir / "service-enabled").touch()

        self.assertTrue(
            self.fixture.wait_for(lambda: len(self.fixture.handover_calls()) == 2, timeout=3),
            f"the concurrent handovers were not both serviced: {self.fixture.handover_calls()}",
        )
        self.assertEqual(
            [call["address"] for call in self.fixture.handover_calls()],
            [second_address, third_address],
        )
        (self.fixture.stub_dir / "wake-after-handover").touch()
        first.communicate(timeout=LAUNCH_TIMEOUT)
        second.communicate(timeout=LAUNCH_TIMEOUT)
        self.assertEqual(self.wake(third), DEFAULT_WAKE)

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


class WaiterLivenessTests(unittest.TestCase):
    """The waiter's own record of itself, on the protocol the driver already keeps (#127).

    A waiter is a background task of the coordinator's main session, and the harness reaps those
    under memory pressure: it died three times in one run, and the `CREW ASK` it would have
    carried sat unanswered until a human re-typed `/crew`. Nothing on disk said a waiter was
    missing. Now one file does — written while it blocks, taken away on each of its three
    endings, and left standing by a kill, because nothing runs on the way out of one.
    """

    def setUp(self):
        self.fixture = Fixture()
        self.addCleanup(self.fixture.close)
        self.fixture.registry(os.getpid())
        self.fixture.transcript([PERMISSION_MODE])

    def blocking_waiter(self):
        """A waiter attached to a live run and blocked on its wake; returns the process."""
        # A process that does nothing but exist, named as this run's driver: the launch attaches
        # to it rather than starting one, so the waiter is the only thing this fixture is running.
        standing = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        self.addCleanup(standing.wait)
        self.addCleanup(standing.kill)
        self.fixture.record_driver(standing.pid)
        self.driver = standing
        waiter = self.fixture.start_launch()
        self.assertTrue(
            self.fixture.wait_for(lambda: self.fixture.recorded_waiter() is not None),
            "the waiter never named itself in the run directory",
        )
        return waiter

    def ended(self, waiter):
        """What that waiter printed, once it has ended of its own accord."""
        out, err = waiter.communicate(timeout=LAUNCH_TIMEOUT)
        self.assertEqual(waiter.returncode, 0, err)
        return out

    def assertReleased(self):
        self.assertIsNone(
            self.fixture.recorded_waiter(), "a deliberate ending left the waiter's pid standing"
        )

    # --- while it blocks -----------------------------------------------------------------------

    def test_a_blocking_waiter_names_its_own_process_in_the_run_directory(self):
        waiter = self.blocking_waiter()

        self.assertEqual(self.fixture.recorded_waiter(), waiter.pid)

    def test_the_waiter_is_named_before_the_driver_it_starts_can_read_the_record(self):
        """Ordering, because the driver acts on this record. A start that failed preflight is over
        well inside the handshake, and a driver that read an empty record would type `/crew` at a
        coordinator whose waiter was only a moment away — and start the failure over again."""
        result = self.fixture.launch()

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.fixture.driver_calls()
        self.assertEqual(len(calls), 1, calls)
        self.assertIsNotNone(
            calls[0]["waiter"], "the driver started before a waiter had named itself"
        )

    def test_a_waiter_record_failure_is_said_but_does_not_withhold_the_run_wake(self):
        record = self.fixture.crew_dir / WAITER_RECORD
        record.mkdir(parents=True)

        result = self.fixture.launch()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), DEFAULT_WAKE)
        self.assertIn("this waiter could not name itself", result.stderr)
        self.assertEqual(len(self.fixture.driver_calls()), 1)

    # --- the three deliberate endings ----------------------------------------------------------

    def test_the_snapshot_ending_takes_the_record_away(self):
        waiter = self.blocking_waiter()

        self.fixture.wake(DEFAULT_WAKE)

        self.assertEqual(json.loads(self.ended(waiter)), DEFAULT_WAKE)
        self.assertReleased()

    def test_the_killed_driver_ending_takes_the_record_away(self):
        """No snapshot was left, so the waiter says so and lets the run go all the same."""
        waiter = self.blocking_waiter()

        self.driver.kill()
        # Reaped, because a killed process this fixture is the parent of stays in the process
        # table until it is, and `kill -0` cannot tell a zombie from a running driver.
        self.driver.wait()

        self.assertIn("was killed", self.ended(waiter))
        self.assertReleased()

    def test_the_no_wake_ending_takes_the_record_away(self):
        """The driver put the run down and asked for nothing: still an ending the waiter ran."""
        waiter = self.fixture.start_launch(env_overrides={
            "AGENTCREW_STUB_DRIVER_STOPPED": "1", "AGENTCREW_STUB_DRIVER_HOLD": "1",
        })

        self.assertIn("without leaving a wake snapshot", self.ended(waiter))
        self.assertReleased()

    # --- two waiters on one run -------------------------------------------------------------------

    def test_a_waiter_that_ends_leaves_the_record_of_the_one_that_replaced_it(self):
        """`/crew` typed twice puts a second waiter on one run — nothing but the launch lock stops
        it, and the lock only guards the driver. The newer waiter owns the record, so the older one
        ending must not take the name of a waiter that is still blocking away: a run read as having
        none would have `/crew` typed at a coordinator whose wake is already on its way.

        The replacement here is this test process, named in the record as a second waiter would
        name itself — a process that is certainly alive when the first one ends.
        """
        first = self.blocking_waiter()
        self.fixture.record_waiter(os.getpid())

        self.fixture.wake(DEFAULT_WAKE)

        self.assertEqual(json.loads(self.ended(first)), DEFAULT_WAKE)
        self.assertEqual(self.fixture.recorded_waiter(), os.getpid())

    def test_a_waiter_reaped_after_a_later_one_attached_hides_neither(self):
        """One name could not hold two waiters. A second `/crew` attaching and then being reaped
        left the record naming a process that was gone, while the first was still blocking — and a
        driver reading that record typed `/crew` at a coordinator whose wake was already coming.
        Every waiter is named, so a dead name never speaks for a live one."""
        first = self.blocking_waiter()
        reaped = self.fixture.dead_pid()
        self.fixture.record_waiter(reaped)

        self.assertEqual(
            self.fixture.recorded_waiters(), [str(first.pid), str(reaped)]
        )

        self.fixture.wake(DEFAULT_WAKE)
        self.assertEqual(json.loads(self.ended(first)), DEFAULT_WAKE)
        self.assertEqual(self.fixture.recorded_waiters(), [str(reaped)])

    # --- the one ending that is not deliberate --------------------------------------------------

    def test_a_killed_waiter_leaves_its_record_standing(self):
        """The stall this exists for: the harness reaps the waiter, nothing runs on its way out,
        and the file naming a process that is gone is all that is left to say so."""
        waiter = self.blocking_waiter()

        waiter.kill()
        waiter.communicate()

        self.assertEqual(self.fixture.recorded_waiter(), waiter.pid)


if __name__ == "__main__":
    unittest.main()
