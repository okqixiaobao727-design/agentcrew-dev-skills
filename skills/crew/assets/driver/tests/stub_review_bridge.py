#!/usr/bin/env python3
"""The Review-Switch command, present on the fixture's PATH and never run.

Preflight asks whether this command is installed, so the fixture installs one. Nothing in a driver
test reviews for real — a stub child writes its ticket's `review` events through the machine log
directly — so being executed means a test reached the process boundary this repository stopped at
(ADR-0020), and saying so is more use than any report this could invent.
"""
import sys

print(
    "stub review-bridge: a driver test ran a real review; the fixture installs this command for"
    f" preflight to find, not to call ({' '.join(sys.argv[1:])})",
    file=sys.stderr,
)
sys.exit(1)
