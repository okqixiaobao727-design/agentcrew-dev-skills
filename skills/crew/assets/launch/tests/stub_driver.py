#!/usr/bin/env python3
"""A driver stand-in for the launch tests: it lives the life of a driver without being one.

Every call is appended to `AGENTCREW_STUB_DIR/driver-calls.jsonl` with the directory it was made
in, because the composed command line and the directory it runs in are the whole of what the
launch script hands the driver. Beyond that it keeps the two records a real driver keeps, which
are the whole of what the launcher and the dashboard read it by:

- it names itself in `<feature-dir>/.crew/driver.pid` on the way in, as a driver's loop does;
- it takes that record away and leaves one wake snapshot in `<feature-dir>/.crew/wake.json` on the
  way out, as every deliberate exit of a driver does.

`AGENTCREW_STUB_DRIVER_HOLD` holds it that many seconds before it wakes, so a test can catch a run
while its driver is still driving it. `AGENTCREW_STUB_DRIVER_WAKE` is the snapshot it wakes with.
`AGENTCREW_STUB_DRIVER_STOPPED` makes it release the run and end without a wake at all, which is
what an operator's Ctrl-C in the driver's own window leaves behind.
"""

import json
import os
import pathlib
import sys
import time


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import run_plan  # noqa: E402


STDOUT_LINE = "stub driver ran"
DEFAULT_WAKE = {"reason": "run-complete", "ticket": None, "pointer": "report.md"}


def flag(argv, name):
    """Return the value following `name`, or None when that flag is absent."""
    return argv[argv.index(name) + 1] if name in argv else None


def run_dir(argv):
    """Return the Run directory named by this Driver command, or None when it names none."""
    feature = flag(argv, "--feature-dir")
    return (
        pathlib.Path(feature).resolve() / run_plan.CREW_STATE_DIR_NAME
        if feature else None
    )


def waiter(directory):
    """Return what the Run directory said about its Waiter when this Driver started, or None.

    A real driver asks the same question of the same file before every wake it writes, so what a
    driver could have read is recorded here rather than inferred from timing afterwards.
    """
    if directory is None:
        return None
    try:
        return (directory / "waiter.pid").read_text().strip() or None
    except OSError:
        return None


def coordinator_context(argv):
    """Return the exact immutable context the launcher handed this Driver stand-in."""
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
    import coordinator_control

    return coordinator_control, coordinator_control.CoordinatorContext(
        name=flag(argv, "--coordinator-name"),
        pid=int(flag(argv, "--coordinator-pid")),
        harness_session=flag(argv, "--coordinator-session"),
        address=flag(argv, "--coordinator-address"),
        pane=flag(argv, "--coordinator-pane"),
        permission_mode=flag(argv, "--permission-mode"),
        display_session=flag(argv, "--tmux-session"),
    )


def record_handover(state_dir, context):
    """Record one changed context applied by the Driver stand-in; return nothing."""
    with (state_dir / "driver-handover-calls.jsonl").open("a") as handle:
        handle.write(json.dumps({
            "name": context.name,
            "pid": context.pid,
            "harness_session": context.harness_session,
            "address": context.address,
            "pane": context.pane,
            "permission_mode": context.permission_mode,
            "display_session": context.display_session,
        }) + "\n")


def record_self(directory):
    """Name this process the run's driver, by rename, as `monitor.record_driver` does.

    A plain write truncates first, so a launcher polling the record can read it empty while a
    driver that is very much alive is halfway through naming itself — the exact read the real
    writer uses a rename to make impossible. A stand-in that leaves that window open does not
    merely fail to test the race; it manufactures one the product does not have.
    """
    path = directory / "driver.pid"
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(f"{os.getpid()}\n")
    os.replace(temporary, path)


def main():
    """Run the Driver stand-in and return the configured process exit status."""
    argv = sys.argv[1:]
    state_dir = pathlib.Path(os.environ["AGENTCREW_STUB_DIR"])
    directory = run_dir(argv)
    with (state_dir / "driver-calls.jsonl").open("a") as handle:
        handle.write(json.dumps({
            "argv": argv, "cwd": os.getcwd(), "pid": os.getpid(),
            "waiter": waiter(directory),
        }) + "\n")
    print(STDOUT_LINE, flush=True)

    if directory is None or not directory.is_dir():
        return int(os.environ.get("AGENTCREW_STUB_DRIVER_EXIT") or 0)
    record_self(directory)
    hold = float(os.environ.get("AGENTCREW_STUB_DRIVER_HOLD") or 0)
    if os.environ.get("AGENTCREW_STUB_DRIVER_SERVICE"):
        coordinator_control, context = coordinator_context(argv)
        control = coordinator_control.CoordinatorControl(directory)
        deadline = time.monotonic() + hold
        handed_over = False
        while True:
            if (
                os.environ.get("AGENTCREW_STUB_DRIVER_SERVICE_GATE")
                and not (state_dir / "service-enabled").exists()
            ):
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.01)
                continue
            previous = context
            context = control.service(
                context,
                lambda next_context: (
                    record_handover(state_dir, next_context)
                    if next_context.address != previous.address else None
                ),
            )
            handed_over = handed_over or context.address != previous.address
            if handed_over and (state_dir / "wake-after-handover").exists():
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(0.01)
    else:
        time.sleep(hold)
    (directory / "driver.pid").unlink(missing_ok=True)
    if not os.environ.get("AGENTCREW_STUB_DRIVER_STOPPED"):
        wake = os.environ.get("AGENTCREW_STUB_DRIVER_WAKE") or json.dumps(DEFAULT_WAKE)
        (directory / "wake.json").write_text(wake + "\n")
    return int(os.environ.get("AGENTCREW_STUB_DRIVER_EXIT") or 0)


if __name__ == "__main__":
    sys.exit(main())
