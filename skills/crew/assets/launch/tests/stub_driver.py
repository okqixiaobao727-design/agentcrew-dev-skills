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
    return argv[argv.index(name) + 1] if name in argv else None


def run_dir(argv):
    feature = flag(argv, "--feature-dir")
    return (
        pathlib.Path(feature).resolve() / run_plan.CREW_STATE_DIR_NAME
        if feature else None
    )


def waiter(directory):
    """What the run directory said about its waiter when this driver started, or None.

    A real driver asks the same question of the same file before every wake it writes, so what a
    driver could have read is recorded here rather than inferred from timing afterwards.
    """
    if directory is None:
        return None
    try:
        return (directory / "waiter.pid").read_text().strip() or None
    except OSError:
        return None


def main():
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
    (directory / "driver.pid").write_text(f"{os.getpid()}\n")
    time.sleep(float(os.environ.get("AGENTCREW_STUB_DRIVER_HOLD") or 0))
    (directory / "driver.pid").unlink(missing_ok=True)
    if not os.environ.get("AGENTCREW_STUB_DRIVER_STOPPED"):
        wake = os.environ.get("AGENTCREW_STUB_DRIVER_WAKE") or json.dumps(DEFAULT_WAKE)
        (directory / "wake.json").write_text(wake + "\n")
    return int(os.environ.get("AGENTCREW_STUB_DRIVER_EXIT") or 0)


if __name__ == "__main__":
    sys.exit(main())
