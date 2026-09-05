---
status: accepted
---

# The Machine log has one write seam with two adapters

[ADR-0017](0017-machine-log-owns-run-facts-driver-owns-workflow-policy.md) made the Machine log
module the in-process owner of run facts, and the reading half of that has been true since: the
Driver, the monitor and advance all call `read_records` and `project` in the interpreter they are
already running in. The writing half never followed. Every writer in the same process tree — the
Driver, dispatch, advance, the merge driver, the monitor — appended by starting
`machine_log.py <event> …` as a subprocess, and the docstring at each of those call sites said
"through the Machine-log boundary" or "through the log's own writer" as if the executable were the
boundary.

It was not. The boundary is the module, and the executable was one way of reaching it. The cost of
confusing the two was measured in #192: one CLI write costs about 40ms of interpreter start, while
the `append(entry(...))` that write eventually performs costs 0.025ms. A driver loop test makes
about forty of them; a production poll makes several. Nothing in the repository ever decided that a
write had to be a command — no ADR recorded it, and the module already had one in-process per-event
writer, `record_witness`, whose CLI subcommand was a wrapper over it (#198).

The reason the fix is not "call `append` from the caller" is that the field table of each event
lived only in the CLI's argparse definitions: which fields a `launch` requires, that a
`session-cost` carries all five figures or a diagnosis and never both, that a passed `base-gate`
carries its argv. A caller building a record itself would carry that table, and the module would
get shallower rather than deeper.

## Decision

**The Machine log module offers one in-process writer function per event, and those functions own
the event's field table.** `record_launch`, `record_receipt`, `record_merge`, `record_outcome`,
`record_queued`, `record_review`, `record_advance`, `record_session_cost`, `record_base_gate`,
`record_live_source`, `record_monitor_error`, `record_message`, `record_launch_failed`,
`record_pause`, `record_resume` and the existing `record_witness`. Fields are keyword-only; a
required field is a parameter with no default, an optional one defaults to `None` and is left out
of the record rather than written empty. The order the parameters are assembled in is the order the
keys appear on the line. Each raises `ValueError` where the values contradict each other and
`OSError` where the log could not be written, so a caller can tell its own document being wrong
from the record failing around a document that stands.

**The CLI is an adapter over those functions, and so is the Driver.** One handler, `run_event`,
carries every appending subcommand: argparse checks what argparse can check — that a required flag
was given, that a value is inside a closed set — and the writer owns the rest. A contradiction is
exit 2 and an unwritable log is exit 1, on the same stderr sentences as before. The registration
seam is split the same way: `install_settings` and `uninstall_settings` do the work and raise
`SettingsError`, while `run_install` and `run_uninstall` catch it, print and return an exit code.

**Which adapter a caller uses is decided by what that caller can do, not by preference.** The
Driver, dispatch, advance, the merge driver and the monitor import this module and call the
functions. A hook, a child, and a shell script can only run a command, so the receipt command typed
into a child, the `hook`, `guard`, `pause` and `resume` commands registered in settings, the
lifecycle hook commands the dispatch renderer hands Review-Switch, `monitor-wave.sh` and
`codex_bridge.py` all go on running the CLI. `driver.py clear` keeps its check that the run carries
a durable copy of the writer beside its log, because that copy is what a registered hook command
names across a plugin upgrade (#37) — it just no longer starts it to uninstall.

**The record is the contract, and it does not move.** A line written by a function is byte for byte
the line the subcommand writes for the same inputs; `docs/machine-log.md` keeps publishing the
schema, and this decision changes nothing in it. The snapshot constraint stands unchanged: advance
still takes two reads around the merge driver's writes, and no read snapshot is cached anywhere.

## Consequences

The module got deeper: the event's field table has one home, and the four handlers that each turned
a namespace into a record collapsed into one. The suites see the saving directly — the driver
suite's slowest shard went from 154.5s to 115.5s on the same machine, about 25% — and a
production run stops paying several interpreter starts per poll.

The price is that a caller's error text changed where it used to be the CLI's stderr relayed
through an exit status. A `DriverError` detail that read

    …could not be recorded: machine log: /path: [Errno 21] Is a directory: '/path'

now reads

    …could not be recorded: [Errno 21] Is a directory: '/path'

The path is still in the sentence, because the raised `OSError` carries its own filename; what is
gone is a prefix that only ever said which executable had failed. The exit codes and messages a
hook or a child sees are unchanged, which is the boundary that had callers outside this
repository's control.

Adding an event is now two edits in one file that cannot disagree: a writer function, and a
subparser naming it. Adding one to the CLI alone is no longer possible, which is the point.
