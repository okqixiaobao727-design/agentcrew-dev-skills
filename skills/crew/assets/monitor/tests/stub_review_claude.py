#!/usr/bin/env python3
"""Test double for the `claude` CLI a review bridge launches, with one extra job.

The dashboard's review annotation is drawn from the run's machine log *while a review is running*,
and the only moment that log says so is between the bridge's two writes. So this stub copies the
log as it stands the moment the reviewer is running — the real bridge's own `running` line
included — to $AGENTCREW_STUB_REVIEW_SNAPSHOT, and then answers like any headless review.

Nothing here writes a log line: the bridge under test is the writer, and a snapshot of what it
wrote is not a fixture anybody composed.
"""

import json
import os
import pathlib
import shutil
import sys

SESSION_ID = "review-session-0001"
REVIEW_REPORT = "Standards: no findings. Spec: no findings."


def snapshot_the_log():
    source = os.environ.get("AGENTCREW_STUB_REVIEW_LOG")
    destination = os.environ.get("AGENTCREW_STUB_REVIEW_SNAPSHOT")
    if source and destination and pathlib.Path(source).exists():
        shutil.copyfile(source, destination)


def main():
    snapshot_the_log()
    print(json.dumps({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "session_id": SESSION_ID,
        "result": REVIEW_REPORT,
        "permission_denials": [],
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
