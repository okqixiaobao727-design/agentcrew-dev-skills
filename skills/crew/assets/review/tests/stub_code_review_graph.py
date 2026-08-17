#!/usr/bin/env python3
"""Test double for the `code-review-graph` CLI, in the one mode the bridge calls.

Emulates:

    code-review-graph detect-changes --base <range> --brief --repo <checkout>

Every invocation appends one JSON line to $CRG_STUB_ARGV_LOG recording the argv, the working
directory, and the two graph-redirection variables the spec forbids the bridge from setting, so a
test can assert on the call without reaching into the bridge.

The scenario is read from $CRG_STUB_SCENARIO (default "risk"):

    risk        a brief report with changed functions and a nonzero risk score
    zero        a brief report with zero changed functions — the stale-graph signature
    fail        a nonzero exit with a message on stderr
    empty       exit 0 with nothing on stdout
"""

import json
import os
import pathlib
import sys

RISK_REPORT = (
    "Analyzed 2 changed file(s):\n"
    "  - 3 changed function(s)/class(es)\n"
    "  - 1 affected flow(s)\n"
    "  - 2 test gap(s)\n"
    "  - Overall risk score: 0.62\n"
)
ZERO_REPORT = (
    "Analyzed 2 changed file(s):\n"
    "  - 0 changed function(s)/class(es)\n"
    "  - 0 affected flow(s)\n"
    "  - 0 test gap(s)\n"
    "  - Overall risk score: 0.00\n"
)
FAILURE_MESSAGE = "stub code-review-graph: no graph for this repository"


def record(argv):
    log = os.environ.get("CRG_STUB_ARGV_LOG")
    if not log:
        return
    entry = {
        "argv": argv,
        "cwd": os.getcwd(),
        "dataDirEnv": os.environ.get("CRG_DATA_DIR"),
        "repoRootEnv": os.environ.get("CRG_REPO_ROOT"),
    }
    with pathlib.Path(log).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry) + "\n")


def main():
    argv = sys.argv[1:]
    record(argv)
    active = os.environ.get("CRG_STUB_SCENARIO", "risk")

    if active == "fail":
        print(FAILURE_MESSAGE, file=sys.stderr)
        return 1
    if active == "empty":
        return 0

    sys.stdout.write(ZERO_REPORT if active == "zero" else RISK_REPORT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
