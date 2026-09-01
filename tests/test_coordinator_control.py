import contextlib
import dataclasses
import io
import json
import os
import pathlib
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


ASSETS = pathlib.Path(__file__).resolve().parents[1] / "skills" / "crew" / "assets"
sys.path.insert(0, str(ASSETS))

import coordinator_control  # noqa: E402


COORDINATOR_A = coordinator_control.CoordinatorContext(
    name="crew-coordinator-a",
    pid=1401,
    harness_session="session-a",
    address="uds:/tmp/crew-a.sock",
    pane="%1",
    permission_mode="acceptEdits",
    display_session="$1:",
)
COORDINATOR_B = coordinator_control.CoordinatorContext(
    name="crew-coordinator-b",
    pid=2402,
    harness_session="session-b",
    address="uds:/tmp/crew-b.sock",
    pane="%8",
    permission_mode="bypassPermissions",
    display_session="$8:",
)


class Liveness:
    def __init__(self):
        self.driver_pid = None
        self.driver_alive = True
        self.waiters = []

    def live_driver(self, _run_dir):
        return self.driver_pid if self.driver_pid is not None and self.driver_alive else None

    def recorded_driver(self, _run_dir):
        return self.driver_pid

    def alive(self, pid):
        return self.driver_alive and pid == self.driver_pid

    def record_waiter(self, _run_dir, pid):
        self.waiters.append(pid)

    def release_waiter(self, _run_dir, pid):
        self.waiters.remove(pid)


class CoordinatorControlAttendanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.run_dir = pathlib.Path(self.temporary.name) / ".crew"
        self.run_dir.mkdir()
        self.liveness = Liveness()
        self.control = coordinator_control.CoordinatorControl(
            self.run_dir, liveness=self.liveness, poll_seconds=0.001
        )

    def wake(self):
        (self.run_dir / "wake.json").write_text('{"reason":"run-complete"}\n')

    def test_context_is_immutable(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            COORDINATOR_A.address = "uds:/tmp/replacement.sock"

    def test_no_live_driver_starts_once_and_same_address_only_attaches(self):
        starts = []

        def start(context):
            starts.append(context)
            self.liveness.driver_pid = 9999
            self.wake()

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(self.control.attend(COORDINATOR_A, start), 0)
        self.assertEqual(starts, [COORDINATOR_A])
        self.assertEqual(output.getvalue(), '{"reason":"run-complete"}\n')
        self.assertEqual(self.liveness.waiters, [])

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(self.control.attend(COORDINATOR_A, start), 0)
        self.assertEqual(starts, [COORDINATOR_A])
        self.assertEqual(output.getvalue(), '{"reason":"run-complete"}\n')
        self.assertEqual(self.liveness.waiters, [])

    def test_same_address_reattendance_keeps_the_current_pane_without_applying(self):
        self.liveness.driver_pid = 9999
        self.control.service(COORDINATOR_A, lambda _context: None)
        self.wake()
        moved = dataclasses.replace(
            COORDINATOR_A,
            pane="%99",
            display_session="$99:",
        )

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.control.attend(moved, self.fail), 0)

        applied = []
        current = self.control.service(COORDINATOR_A, applied.append)

        self.assertEqual(applied, [])
        self.assertEqual(current.pane, COORDINATOR_A.pane)
        self.assertEqual(current.display_session, COORDINATOR_A.display_session)
        recorded = self.control._context(self.control._read()["current"])
        self.assertEqual(recorded.pane, COORDINATOR_A.pane)
        self.assertEqual(recorded.display_session, COORDINATOR_A.display_session)

    def test_different_address_is_serviced_before_attendance_succeeds(self):
        self.liveness.driver_pid = 9999
        self.control.service(COORDINATOR_A, lambda _context: None)
        self.wake()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.control.attend(COORDINATOR_A, self.fail), 0)
        (self.run_dir / "wake.json").unlink()

        output = io.StringIO()
        result = []

        def attend():
            with contextlib.redirect_stdout(output):
                result.append(self.control.attend(COORDINATOR_B, self.fail))

        waiter = threading.Thread(target=attend)
        waiter.start()
        applied = []
        current = COORDINATOR_A
        deadline = time.monotonic() + 1.0
        while current == COORDINATOR_A and time.monotonic() < deadline:
            current = self.control.service(COORDINATOR_A, applied.append)
            if current == COORDINATOR_A:
                time.sleep(0.001)

        self.assertEqual(current, COORDINATOR_B)
        self.assertEqual(applied, [COORDINATOR_B])
        self.assertEqual(self.liveness.driver_pid, 9999)
        self.assertTrue(waiter.is_alive(), "attendance returned before the next wake")

        self.wake()
        waiter.join(1.0)
        self.assertFalse(waiter.is_alive())
        self.assertEqual(result, [0])
        self.assertEqual(output.getvalue(), '{"reason":"run-complete"}\n')
        self.assertEqual(self.liveness.waiters, [])

    def test_a_live_driver_with_no_private_state_remains_authoritative_until_handover(self):
        self.liveness.driver_pid = 9999
        (self.run_dir / "wave-table.json").write_text(json.dumps({"run": {
            "coordinator_name": COORDINATOR_A.name,
            "coordinator_pid": COORDINATOR_A.pid,
            "coordinator_session": COORDINATOR_A.harness_session,
            "coordinator_address": COORDINATOR_A.address,
            "permission_mode": COORDINATOR_A.permission_mode,
        }}))
        result = []

        def attend():
            with contextlib.redirect_stdout(io.StringIO()):
                result.append(self.control.attend(COORDINATOR_B, self.fail))

        waiter = threading.Thread(target=attend, daemon=True)
        waiter.start()
        deadline = time.monotonic() + 1.0
        while not self.liveness.waiters and time.monotonic() < deadline:
            time.sleep(0.001)

        applied = []
        driver_control = coordinator_control.CoordinatorControl(
            self.run_dir, poll_seconds=0.001
        )
        current = driver_control.service(COORDINATOR_A, applied.append)
        deadline = time.monotonic() + 1.0
        while current != COORDINATOR_B and time.monotonic() < deadline:
            current = driver_control.service(current, applied.append)
            time.sleep(0.001)

        self.assertEqual(current, COORDINATOR_B)
        self.assertEqual(applied, [COORDINATOR_B])
        self.wake()
        waiter.join(1.0)
        self.assertFalse(waiter.is_alive())
        self.assertEqual(result, [0])

    def test_a_driver_killed_during_handover_reaches_the_existing_waiter_ending(self):
        self.liveness.driver_pid = 9999
        self.control.service(COORDINATOR_A, lambda _context: None)
        self.wake()
        with contextlib.redirect_stdout(io.StringIO()):
            self.control.attend(COORDINATOR_A, self.fail)
        (self.run_dir / "wake.json").unlink()
        output = io.StringIO()
        result = []

        def attend():
            with contextlib.redirect_stdout(output):
                result.append(self.control.attend(COORDINATOR_B, self.fail))

        waiter = threading.Thread(target=attend, daemon=True)
        waiter.start()
        deadline = time.monotonic() + 1.0
        while not self.liveness.waiters and time.monotonic() < deadline:
            time.sleep(0.001)
        self.liveness.driver_alive = False

        waiter.join(1.0)
        self.assertFalse(waiter.is_alive(), "handover waited forever after its Driver died")
        self.assertEqual(result, [0])
        self.assertIn("was killed; it left no wake snapshot", output.getvalue())


class CoordinatorControlAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.run_dir = pathlib.Path(self.temporary.name) / ".crew"
        self.run_dir.mkdir()
        self.liveness = Liveness()
        self.liveness.driver_pid = 9999
        self.control = coordinator_control.CoordinatorControl(
            self.run_dir, liveness=self.liveness, poll_seconds=0.001
        )
        self.control.service(COORDINATOR_A, lambda _context: None)
        (self.run_dir / "wake.json").write_text('{"reason":"run-complete"}\n')
        with contextlib.redirect_stdout(io.StringIO()):
            self.control.attend(COORDINATOR_A, self.fail)

    def test_a_driver_initializing_private_control_applies_its_recorded_context(self):
        empty_run = pathlib.Path(self.temporary.name) / "old-run"
        applied = []

        current = coordinator_control.CoordinatorControl(empty_run).service(
            COORDINATOR_A, applied.append
        )

        self.assertEqual(current, COORDINATOR_A)
        self.assertEqual(applied, [COORDINATOR_A])

    def test_current_address_encloses_the_action(self):
        effects = []
        with mock.patch.dict(
            os.environ,
            {coordinator_control.SENDER_SOCKET_VARIABLE: "/tmp/crew-a.sock"},
            clear=True,
        ):
            result = self.control.authorized_action(lambda: effects.append("sent") or 17)
        self.assertEqual(result, 17)
        self.assertEqual(effects, ["sent"])

    def test_stale_or_missing_address_is_refused_before_the_action(self):
        effects = []
        with mock.patch.dict(
            os.environ,
            {coordinator_control.SENDER_SOCKET_VARIABLE: "/tmp/crew-b.sock"},
            clear=True,
        ):
            with self.assertRaisesRegex(
                coordinator_control.CoordinatorControlError,
                f"^{coordinator_control.STALE_COORDINATOR}$",
            ):
                self.control.authorized_action(lambda: effects.append("stale"))
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(coordinator_control.CoordinatorControlError) as raised:
                self.control.authorized_action(lambda: effects.append("missing"))
        self.assertEqual(str(raised.exception), coordinator_control.MISSING_COORDINATOR)
        self.assertEqual(effects, [])


if __name__ == "__main__":
    unittest.main()
