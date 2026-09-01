#!/usr/bin/env python3
"""Own Coordinator attendance, Driver handover and Coordinator action ordering.

The run has one Coordinator identity but four callers used to make decisions about it: the
launcher, the live Driver, the answer command and the message hooks.  This Module keeps those
decisions behind three role-shaped operations.  Its run-local files are private implementation;
the Wave table remains the public record of the five Coordinator-derived Run facts.
"""

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import fcntl
import json
import os
import pathlib
import sys
import time
import uuid


SUPERSEDED = "crew: this waiter was superseded by a coordinator handover"
STALE_COORDINATOR = "crew: this Coordinator no longer owns the run"
MISSING_COORDINATOR = (
    "crew: no coordinator address in this environment"
    " (CLAUDE_CODE_MESSAGING_SOCKET unset)"
)
SENDER_SOCKET_VARIABLE = "CLAUDE_CODE_MESSAGING_SOCKET"
_ADDRESS_SCHEME = "uds:"

_STATE_NAME = "coordinator-control.json"
_LOCK_NAME = "coordinator-control.lock"
_TABLE_NAME = "wave-table.json"
_WAKE_NAME = "wake.json"
_DRIVER_LOG_NAME = "driver.log"
_SUPERSEDED_EXIT = 1
_RELEASE_GRACE_SECONDS = 3.0


class CoordinatorControlError(Exception):
    """Coordinator control could not complete the requested role operation."""


@dataclass(frozen=True)
class CoordinatorContext:
    """Every fact resolved once for the Coordinator invoking `/crew`."""

    name: str
    pid: int
    harness_session: str
    address: str
    pane: str | None
    permission_mode: str
    display_session: str


class CoordinatorControl:
    """The single Interface for launcher, Driver and Coordinator-originated actions."""

    def __init__(self, run_dir, *, liveness=None, poll_seconds=0.5):
        """Bind control to one Run directory; return a role-operation boundary."""
        self.run_dir = pathlib.Path(run_dir).resolve()
        self.liveness = liveness
        self.poll_seconds = poll_seconds
        self._state_path = self.run_dir / _STATE_NAME
        self._lock_path = self.run_dir / _LOCK_NAME
        self._serviced = False

    @contextmanager
    def _held(self):
        """Hold the Run's Coordinator ordering lock; yield no value."""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        with self._lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _read(self):
        """Return the private Coordinator-control document with its containers normalized."""
        try:
            value = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"current": None, "waiters": {}}
        if not isinstance(value, dict):
            return {"current": None, "waiters": {}}
        waiters = value.get("waiters")
        value["waiters"] = waiters if isinstance(waiters, dict) else {}
        responses = value.get("responses")
        value["responses"] = responses if isinstance(responses, dict) else {}
        return value

    def _write(self, state):
        """Atomically replace the private Coordinator-control document; return nothing."""
        temporary = self._state_path.with_name(
            f".{self._state_path.name}.{os.getpid()}.tmp"
        )
        try:
            temporary.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(temporary, self._state_path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    @staticmethod
    def _context(value):
        """Return the immutable context represented by `value`, or None when it is invalid."""
        try:
            return CoordinatorContext(**value)
        except (TypeError, ValueError):
            return None

    def _recorded_context(self, fallback):
        """Return the Wave table's five recorded identity facts, or None when unavailable."""
        try:
            run = json.loads(
                (self.run_dir / _TABLE_NAME).read_text(encoding="utf-8")
            )["run"]
        except (OSError, ValueError, TypeError, KeyError):
            return None
        return self._context({
            "name": run.get("coordinator_name"),
            "pid": run.get("coordinator_pid"),
            "harness_session": run.get("coordinator_session"),
            "address": run.get("coordinator_address"),
            "pane": fallback.pane,
            "permission_mode": run.get("permission_mode"),
            "display_session": fallback.display_session,
        })

    def _release_waiter(self, pid):
        """Remove one Waiter from private ownership and the public pid record; return nothing."""
        with self._held():
            state = self._read()
            state["waiters"].pop(str(pid), None)
            self._write(state)
            self.liveness.release_waiter(self.run_dir, pid)

    def _waiter_is_current(self, pid, address):
        """Return whether this Waiter still belongs to the current Coordinator address."""
        with self._held():
            state = self._read()
            current = self._context(state.get("current"))
            return (
                current is not None
                and current.address == address
                and state["waiters"].get(str(pid)) == address
            )

    def _wait_for_wake(self, pid, context, *, require_ownership=True):
        """Carry one Run ending to this Waiter and return the launcher exit code it earns."""
        settled = None
        while True:
            if require_ownership and not self._waiter_is_current(pid, context.address):
                print(SUPERSEDED, flush=True)
                return _SUPERSEDED_EXIT
            try:
                wake = (self.run_dir / _WAKE_NAME).read_text(encoding="utf-8").strip()
            except OSError:
                wake = None
            if wake:
                print(wake, flush=True)
                return 0
            driver = self.liveness.recorded_driver(self.run_dir)
            if driver is not None and not self.liveness.alive(driver):
                print(
                    f"crew: the driver of {self.run_dir.parent} was killed; it left no wake"
                    f" snapshot. /crew {self.run_dir.parent} puts a driver back on the run",
                    flush=True,
                )
                return 0
            if driver is not None:
                settled = None
            elif settled is None:
                settled = time.monotonic() + _RELEASE_GRACE_SECONDS
            elif time.monotonic() >= settled:
                print(
                    f"crew: the driver of {self.run_dir.parent} ended without leaving a wake"
                    " snapshot — stopped in its own window, or unable to write one;"
                    f" {self.run_dir / _DRIVER_LOG_NAME} says which."
                    f" /crew {self.run_dir.parent} starts it again",
                    flush=True,
                )
                return 0
            time.sleep(self.poll_seconds)

    def _await_handover(self, request_id):
        """Return the correlated Driver response, or None when that Driver is no longer live."""
        while True:
            with self._held():
                state = self._read()
                response = state["responses"].pop(request_id, None)
                if response is not None:
                    self._write(state)
                    return response
            if self.liveness.live_driver(self.run_dir) is None:
                return None
            time.sleep(self.poll_seconds)

    def _record_waiter(self, pid):
        """Best-effort record one public Waiter pid and return whether it was recorded."""
        try:
            self.liveness.record_waiter(self.run_dir, pid)
        except Exception as error:
            print(
                f"crew: this waiter could not name itself in {self.run_dir}: {error}",
                file=sys.stderr,
                flush=True,
            )
            return False
        return True

    def attend(self, context, start_driver):
        """Attend as `context`, starting, attaching or handing over, then carry one wake."""
        if self.liveness is None:
            raise CoordinatorControlError("launcher attendance requires process liveness")
        pid = os.getpid()
        request_id = None
        try:
            while True:
                pending = False
                with self._held():
                    state = self._read()
                    live = self.liveness.live_driver(self.run_dir)
                    current = self._context(state.get("current"))
                    if live is not None and current is None:
                        current = self._recorded_context(context) or context
                        state["current"] = asdict(current)
                    pending = live is not None and isinstance(state.get("request"), dict)
                    if not pending:
                        if live is not None and (
                            current is None or current.address != context.address
                        ):
                            request_id = uuid.uuid4().hex
                            state["request"] = {
                                "id": request_id,
                                "context": asdict(context),
                                "waiter_pid": pid,
                            }
                        else:
                            state["waiters"][str(pid)] = context.address
                            if live is None:
                                state["current"] = asdict(context)
                                state["request"] = None
                        self._write(state)
                        self._record_waiter(pid)
                        if live is None:
                            start_driver(context)
                if not pending:
                    break
                time.sleep(self.poll_seconds)
            if request_id is not None:
                response = self._await_handover(request_id)
                if response is None:
                    return self._wait_for_wake(pid, context, require_ownership=False)
                if not isinstance(response, dict) or response.get("outcome") not in (
                    "success", "error"
                ):
                    raise CoordinatorControlError(
                        "the Coordinator handover response is unreadable"
                    )
                # An apply error is carried by the Driver's existing error snapshot. Waiting for
                # that wake preserves its structured diagnosis and forward recovery command.
                if response["outcome"] == "error":
                    return self._wait_for_wake(pid, context)
            return self._wait_for_wake(pid, context)
        finally:
            self._release_waiter(pid)

    def service(self, context, apply):
        """Apply pending Coordinator control before the Driver activates or polls."""
        with self._held():
            state = self._read()
            current = self._context(state.get("current"))
            if current is None:
                state["current"] = asdict(context)
                self._write(state)
                apply(context)
                self._serviced = True
                return context

            request = state.get("request")
            if not isinstance(request, dict):
                if current != context or not self._serviced:
                    state["current"] = asdict(context)
                    self._write(state)
                    apply(context)
                self._serviced = True
                return context

            request_id = request.get("id")
            next_context = self._context(request.get("context"))
            waiter_pid = request.get("waiter_pid")
            if (
                not isinstance(request_id, str)
                or next_context is None
                or not isinstance(waiter_pid, int)
            ):
                raise CoordinatorControlError("the pending coordinator handover is unreadable")

            state["current"] = asdict(next_context)
            state["waiters"][str(waiter_pid)] = next_context.address
            state["request"] = None
            self._write(state)
            try:
                apply(next_context)
            except BaseException as error:
                state["responses"][request_id] = {
                    "outcome": "error",
                    "detail": str(error),
                }
                self._write(state)
                raise
            state["responses"][request_id] = {"outcome": "success"}
            self._write(state)
            self._serviced = True
            return next_context

    def authorized_action(self, action):
        """Run one Coordinator side effect only while its caller owns the Run."""
        socket = os.environ.get(SENDER_SOCKET_VARIABLE)
        if not isinstance(socket, str) or not socket.strip():
            raise CoordinatorControlError(MISSING_COORDINATOR)
        address = _ADDRESS_SCHEME + socket
        with self._held():
            current = self._context(self._read().get("current"))
            if current is None or current.address != address:
                raise CoordinatorControlError(STALE_COORDINATOR)
            return action()
