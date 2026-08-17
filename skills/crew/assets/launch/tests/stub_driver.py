#!/usr/bin/env python3
"""A driver stand-in for the launch tests: it records the command line it was launched with.

Every call is appended to `AGENTCREW_STUB_DIR/driver-calls.jsonl` with the directory it was made
in, because the composed command line and the directory it runs in are the whole of what the
launch script hands the driver. It prints one line on stdout, so a test can assert that the
launcher passes the driver's own output through untouched, and exits with
`AGENTCREW_STUB_DRIVER_EXIT` (default 0), so a test can assert the driver's exit code is the
launcher's.
"""

import json
import os
import pathlib
import sys


STDOUT_LINE = "stub driver ran"


def main():
    argv = sys.argv[1:]
    state_dir = pathlib.Path(os.environ["AGENTCREW_STUB_DIR"])
    with (state_dir / "driver-calls.jsonl").open("a") as handle:
        handle.write(json.dumps({"argv": argv, "cwd": os.getcwd()}) + "\n")
    print(STDOUT_LINE)
    return int(os.environ.get("AGENTCREW_STUB_DRIVER_EXIT") or 0)


if __name__ == "__main__":
    sys.exit(main())
