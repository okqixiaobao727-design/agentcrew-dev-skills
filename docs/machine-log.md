# The machine log

One append-only event log per run, written entirely by scripts and hooks. Its audience is a
future auditing agent, not a human: it exists so a run can be reconstructed without anyone's
memory, and so bookkeeping costs the coordinator zero turns
([ADR-0001](adr/0001-coordinator-spends-tokens-only-on-judgment.md)).

The writer is [`skills/crew/assets/machine_log.py`](../skills/crew/assets/machine_log.py).

## Accepted Run projection design

[ADR-0017](adr/0017-machine-log-owns-run-facts-driver-owns-workflow-policy.md) makes the Machine
log module the one owner of facts derived from the ordered log. The design is deliberately a
snapshot, not a second workflow engine: it says what the log establishes now, while the Driver
still combines those facts with the Wave table and rule table to choose the next action. The
monitor still translates facts and live child status into the human-facing Ticket state.

### Interface

The public seam has three operations:

```python
records = machine_log.read_records(path)
projection = machine_log.project(records)
ticket = projection.ticket(ticket_id)
```

`read_records(path)` owns the normal runtime JSONL read. `project(records)` is an in-process,
side-effect-free reduction over an already ordered iterable and returns an immutable
`RunProjection`. `ticket(ticket_id)` normalises the id with `str()` and returns empty/live facts
for a ticket the log has never mentioned. There is no generic query language, reducer registry,
storage adapter, diagnostics surface, incremental reader or cache.

The intended result shape is:

```python
@dataclass(frozen=True)
class RunProjection:
    tickets: Mapping[str, TicketFacts]
    latest_landed_merge: Mapping[str, object] | None
    current_wave: int
    ended: bool
    halted: bool

    def ticket(self, ticket: object) -> TicketFacts: ...


@dataclass(frozen=True)
class TicketFacts:
    ticket: str
    events: tuple[Mapping[str, object], ...]
    first_launch: Mapping[str, object] | None
    launch: Mapping[str, object] | None
    launch_verification_failed: bool
    receipt: Mapping[str, object] | None
    latest_settling_event: Mapping[str, object] | None
    progress_event: Mapping[str, object] | None
    settlement_state: str
    unanswered_child_message: Mapping[str, object] | None
    escalation: Mapping[str, object] | None
    witness: Mapping[str, object] | None
    awaiting_receipt: bool
    awaiting_ruling: bool
    outstanding_nudge: bool
    merge_result: str | None
    merge_landed: bool
    semantic_conflict_detail: str | None
    merge_rework_requested: bool

    def instruction_count(self, marker: str) -> int: ...
```

This sketch fixes the interface and vocabulary, not the implementation layout. `events` contains
only records that explicitly name the ticket, in accepted-record order. The projection may
correlate a ticketless ruling internally through its `to` child, but must not add that inferred
record to `events`: the monitor's visible annotations currently use explicit ticket records only.
Callers that render audit, cost or report detail retain the original `records`; they must not
re-derive a named projection fact from them.

### Ordering and state contracts

- Physical JSONL order is authoritative. A timestamp is data, never a sort key.
- A snapshot never changes after construction. A caller explicitly reads again after an external
  action; in particular, advance keeps its read before landing and its second read afterwards.
- `latest_landed_merge` is the last physical `merge` record whose result is `clean`, `resolved`,
  or `repaired`, across the whole run. Its `sha` is the integration branch's code landing point.
- First and latest launch remain distinct. The latest launch includes the verified amendment that
  follows a provisional launch; the first launch starts report duration.
- `launch_verification_failed` is true only when `launch-failed` follows the latest `launch`.
  It is false without a launch, and a newer launch clears it.
- `escalation` and `witness` are the latest checked pair. A later escalation replaces the first
  and clears the second until its own witness event arrives, so an older fact-check cannot be
  paired with a newer escalation. The handed-over ruling consumes the pending message but retains
  this factual pair for audit and report readers.
- `latest_settling_event` means the last receipt or outcome with a non-empty value. It answers
  whether the ticket has received a settling event.
- `settlement_state` retains the closed Machine-log vocabulary and its category precedence: a valid
  outcome wins over receipt and merge evidence; otherwise the latest valid receipt wins, with a
  landable receipt completed by a landed merge. One later fact opens a new settlement epoch: a
  launch after `outcome=blocked` means the old derived block no longer controls that child, so its
  current state is derived from the launch and the evidence after it. The blocked record remains in
  `latest_settling_event` and in the append-only log. The two facts must not be collapsed.
- `progress_event` is the latest recognised receipt, outcome or merge used by the monitor as input
  to its own Ticket state mapping. The projection does not produce Ticket state.
- A ticketless ruling is correlated through `to` and the child mapping, exactly as the Driver does
  today. Explicit `ticket` always wins. The mapping holds every identity the run knows a child
  under: the name of its final latest launch, and every address a child record of that ticket has
  a `from` on. A launch name never loses to an address, so a log written before addresses were
  recorded correlates unchanged.
- `unanswered_child_message`, receipt/ruling waits, outstanding nudge, instruction counts,
  semantic-conflict detail and merge-rework request follow their ordered-event rules. A valid
  receipt claim remains unanswered until a receipt with the claimed SHA or a `CREW RECHECK` naming
  that SHA consumes it; an unrelated receipt, ruling or outcome cannot hide it. Record position
  stays private; callers receive the selected record, not an index.
- `current_wave` starts at 1 and follows the last integer-like `advance=launched` record.
- `ended` is an ordered current fact. `advance=complete|stopped` makes it true; a later protocol
  message that the existing Driver rule table can act on makes it false; a subsequent
  final advance makes it true again. Plain child conversation does not reopen a Run. `halted` is a
  latest rule: the last advance is `escalated|interrupted`. They remain separate facts, not one
  run-state enum, and no `reopened` event is persisted.

### Reading and error contracts

The normal runtime reader preserves the behavior shared by the Driver, advance and merge driver:

- a missing path returns an empty ordered record tuple;
- blank lines, invalid JSON lines and valid non-object JSON values are skipped;
- valid objects with unknown events or fields remain in the returned records but contribute no
  unknown fact;
- other `OSError` and UTF-8 decoding failures propagate unchanged;
- no skipped-line diagnostics are printed or added to command output.

The monitor retains its existing presentation policy by catching `OSError` around
`read_records()` and projecting an empty tuple. The error policy is not a parameter on the Machine
log interface: making it one would leak a caller decision into the module.

Two strict readers are explicitly outside this interface. The post-dispatch `launched_children`
read fails on malformed JSON because launch adoption must not proceed from a damaged record. The
clear path refuses unreadable, malformed and non-object records with line-specific errors before
deleting anything. Neither may be replaced by the tolerant runtime reader.

### What moves and what stays

The implementation behind the projection seam owns normal JSONL parsing; first/latest selection;
ticket/child correlation; settlement precedence; message, receipt, ruling and nudge episodes;
merge outcome and semantic-conflict standing; current wave; and run finality. Protocol markers the
projection interprets are defined once in the Machine log module and imported by the Driver.

The following remain with their current owners:

- Wave membership, dependencies and next-wave selection stay with the Wave table and advance.
- The Driver keeps the rule table and every choice to deliver, settle, merge, advance or stop.
- Ticket state, live-source combination, annotations, elapsed time and all rendering stay with the
  monitor.
- Report wording, cost aggregation, undo rendering and strict clear validation stay with the
  Driver or monitor. They may inspect retained records for presentation, but not redefine a named
  Run projection fact.
- Git receipt verification and branch movement stay with the merge driver.

The remaining raw Machine-log scans are an explicit allowlist rather than alternate fact readers:

- `advance.already_advanced` keeps the exact advance record used by the Wave-table duplicate-launch
  safety check and its error text; the decision combines a log event with the table's next wave.
- Driver's post-dispatch `launched_children` and all `clear_*` reads stay strict. Clear also walks
  every historical launch because destructive cleanup must inventory every recorded artefact, not
  only the latest child.
- Driver report helpers retain receipt/outcome chronology, ruling text, tracker undo text and
  terminal detail for presentation. Hook uninstall likewise walks every historical launch path.
- Driver's tracker-close idempotence scan distinguishes a recorded completed outcome from a landed
  merge, while `halt_detail` retains the exact escalated explanation used in an error. Neither
  distinction is a named projection fact.
- Monitor's fallback announcement checks whether the audit event was ever written, and its cost
  pass reads review-session audit rows. Transcript scans are executor logs, not Machine log.

`merge_driver.py` has no remaining raw Machine-log scan. Normal reads in all four consumers enter
through `read_records()`; the allowlisted scans above operate on that accepted record tuple except
for the two deliberately strict Driver paths.

The projection reduction is `O(records)` and a ticket lookup is `O(1)`. Two linear passes are
allowed because a ticketless ruling is interpreted through the child mapping, which the first pass
has to finish building before the second can read it.
There is no cross-read cache. The existing re-evaluation point remains one run log around 5 MB;
below that measured threshold an incremental reader has not earned its complexity.

### Migration and verification

Migration replaces existing logic rather than layering a permanent second path:

1. Add characterization tests at the new interface using fixed expected facts, never old helper
   output as the oracle.
2. Add `read_records`, `project` and the immutable projection without changing a caller.
3. Migrate merge driver's tolerant receipt read, then delete `receipts()`.
4. Migrate advance while preserving the distinct snapshots before and after landing; delete only
   the helpers the projection replaces.
5. Migrate monitor's tolerant read, ticket grouping and run finality. Keep presentation mapping and
   its empty-on-`OSError` behavior.
6. Migrate the Driver's shared helper cluster and remove its dependency on `monitor.over`. Delete
   each replaced helper in the same change that migrates its last caller.
7. Migrate report selections only where the projection already names that fact; keep report-only
   chronology and formatting local.
8. Search the four callers for remaining raw event scans. Every survivor must be one of the
   documented Wave-table, presentation, report, cost, audit or strict-safety cases above.

The tests pin malformed and missing input; physical order against misleading timestamps;
provisional and verified launches; receipt/outcome/merge precedence; repeated child messages;
ticketless rulings; receipt/ruling/nudge episodes; semantic-conflict rework; run ended versus
halted; the two advance snapshots; and byte-for-byte parity of existing command output and monitor
frames. Because the implementation touches the spine, its final gate is the focused root, driver
and monitor suites, the validator, then `python3 scripts/test.py` in full.

### Recorded findings

These findings were exposed by the design work. None is silently fixed by this behavior-preserving
change.

1. **Documentation and test gap:** `LANDED_MERGE_RESULTS` includes `resolved`, while
   `settlement_state()` says only `clean` or `repaired` in its docstring and its direct tests omit
   `resolved`. Add a characterization test during migration; correct the prose only in a separately
   traceable fix.
2. **Compatibility constraint:** normal runtime readers, the monitor, post-dispatch launch reading
   and clear deliberately have different failure strength. The migration preserves all four rather
   than weakening the strict paths or making every display error fatal.
3. **Naming hazard covered by this design:** `settled_states()` answers whether a receipt/outcome
   exists, while `settlement_state()` judges settlement quality. The new names keep those facts
   distinct.
4. **Association behavior requiring characterization:** the Driver correlates a ticketless ruling
   using the final latest-launch child mapping. A historical ruling to a replaced child can
   therefore become unassociated. Preserve it first; any correction needs its own evidence and
   ruling.
5. **Possible stale conflict detail:** `semantic_conflicts()` can pair the latest `escalated` merge
   with an older semantic-conflict detail when no newer conflict record replaced it. Add a focused
   reproduction before deciding whether this is a defect.
6. **Out-of-order compatibility:** `settlement_state()` selects the latest valid receipt and latest
   valid merge by category; it does not require the landed merge to follow the landable receipt.
   Characterize the existing answer for a damaged or hand-written out-of-order log before moving
   the rule.
7. **Snapshot constraint:** advance's two reads surround a subprocess that appends merge and outcome
   events. Combining them into one cached snapshot would change behavior and is forbidden.

## Format

JSON Lines: one JSON object per line, UTF-8, appended and never rewritten. A line is written
with a single `write` on a file opened `O_APPEND`, so concurrent writers — the monitor, the
merge driver, one hook per child — interleave lines, never characters.

Every line carries these two keys first, in this order:

| Key | Value |
| --- | --- |
| `ts` | the moment the event was recorded, `%Y-%m-%dT%H:%M:%SZ` in UTC |
| `event` | one of the events [below](#events), which are the whole set a line can carry |

`ts` is the run's one timestamp format — the same one the crew skill reads with
`date -u +%Y-%m-%dT%H:%M:%SZ` — so any two lines subtract to a duration.

A key whose value was not supplied is omitted from the line rather than written empty, so a
reader can tell "not recorded" from "recorded as nothing".

## Events

### `launch` — a child started on a ticket

`ticket`, `child`, `workflow`, `executor`, `model`, `effort`, `branch`, `worktree`, `window`,
`account`. `model` is a full model ID, never an alias; `account` is the Claude Code profile
directory the child launched under, which is what makes a run's spend attributable after the
fact, and a Claude child's alone — a Codex child launches on its own vendor's credentials and
records no account.

The dispatch renderer writes this one itself, as each child comes up, given `--log`: the launched
set is what wave advancement and the dashboard read, and it costs the coordinator no turn. A
Claude launch first records the known window, worktree and branch with an empty `child`, before
post-launch verification can fail; successful verification appends the amended launch with the
agent name. Consumers take the last launch per ticket. A Codex launch records the thread the
bridge pinned.

### `launch-failed` — a live child failed post-launch verification

`ticket`, `detail`. Dispatch writes this after the corresponding `launch` when the child window
has started but the agents list or transcript verification fails. The `launch` remains the
adoption record; `detail` preserves the verification failure instead of leaving it only on the
driver's transient stderr surface.

### `receipt` — a child's final word, as verified by script

`ticket`, `verdict` (`landable`, `parked`, or `failed`), `sha`, `detail`. The three verdicts are
the ones the crew skill's watch step settles a launched ticket into.

### `merge` — one ticket branch's trip into the integration branch

`ticket`, `branch`, `into`, `result`, `sha`, `detail`. `result` is one of the escalation
ladder's stops ([ADR-0004](adr/0004-escalation-ladder-script-then-sonnet-then-coordinator.md)):

| `result` | Meaning |
| --- | --- |
| `clean` | the scripted merge succeeded, no model involved |
| `conflict` | the merge conflicted and was handed down the ladder |
| `resolved` | the driver kept both sides' insertions itself, no model involved |
| `repaired` | the budget-capped repair session resolved it |
| `escalated` | a semantic conflict or a repair double failure went to the coordinator |

`resolved` and `repaired` are two words rather than one because a log that spells them alike
cannot say which merges cost a model anything.

### `outcome` — a ticket's one report outcome

`ticket`, `outcome` (`completed`, `failed`, `parked`, or `blocked`), `detail`. These are the four
outcomes the crew contract gives every ticket.

### `review` — a ticket's trip through its review lane

`ticket`, `lane` (the reviewing vendor and its model, as the wave table approved them), `state`
(`running` or `returned`), `detail`. One line when a review starts and one when it comes back; the
last line for a ticket is the one that holds.

The dashboard reads this event and nothing else does: a ticket whose last `review` line says
`running` carries the review annotation row
([`docs/monitor-dashboard.md`](monitor-dashboard.md)), and a ticket whose review has `returned`
goes quiet again.

The writers are the Lifecycle Hook commands the dispatch renderer passes to Review-Switch:
`review-start` writes `running`, and `review-end` writes `returned`. The child does not write
either line because Review-Switch is the party that deterministically knows both ends: a child
asked in prose to log them may skip a line, and a child whose session dies mid-review could never
write `returned` at all. So the end hook runs on every exit path Review-Switch controls, a review
that failed or was interrupted included. A run with no log passes no commands, and a command that
cannot write changes neither Review-Switch's exit status nor the result the child reads. Each call
that reaches `review-start` writes one pair; a preparation failure opens no review and writes only
`returned`. Following a result's `nextCall.argv` starts another Bridge invocation, so it writes
another pair for the same ticket. The caller's run-again budget changes only how many pairs may be
written; the event shape and the rule that the last line holds are unchanged.

### `witness` — one fact-check of a child escalation

`ticket`, `executor`, `model`, `outcome` (`checked` or `failed`), `reason`, `duration_seconds`,
`input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens`, `total_tokens`.
The Driver writes one line after the child escalation and before its handed-over ruling. `model`
and the budget that bounded the run come from the run's `[witness]` configuration; `executor` and
`model` are the resolved witness route, while the hard budget remains in the Wave table. `reason`
is empty for `checked` and is the witness failure reason for `failed`.

The four token counters and `total_tokens` use the same meanings as `session-cost`, and total is
their sum. They are absent together when the failed witness returned no usage; the outcome,
reason, and duration still record the attempted fact-check. The Driver writes this event for both
Claude and Codex children: the witness itself is driver-side, runs in the escalating child's
worktree, and uses that ticket's named Claude account where it has one.

### `base-gate` — whether a fresh run checked its integration base

`status` (`passed` or `not-configured`), `argv`. This event carries no ticket because it describes
the run's base. A configured gate records `passed` with its non-empty argv after the Driver creates
the Crew worktree at the selected local base tip and before it launches any child. An omitted gate
records `not-configured` with no `argv`; omission is never spelled as a pass. A failing gate removes
the fresh Crew worktree and Integration branch and creates no run directory or machine log, so its
command, status and output tail live only on the `preflight-failed` notice. Adoption records nothing
new and does not rerun the gate; older runs without this event remain readable and their report
says `not recorded`.

### `advance` — what the run decided after a wave settled

`wave`, `decision`, `detail`. The one event that carries no `ticket`: a decision is about a wave.
`decision` is in the existing closed set shared by the advance command and Driver
([`docs/wave-advance.md`](wave-advance.md)). The command records landing outcomes; the Driver
records `launched` only after activation succeeds, and records `escalated` when activation fails:

| `decision` | Meaning |
| --- | --- |
| `launched` | the next wave is running; `wave` is the wave that started |
| `complete` | that was the last wave of the run |
| `escalated` | landing or activation could not continue without recovery |
| `interrupted` | the operator stopped the run |
| `stopped` | the run ended on an escalation the rule table had already settled |

Two of them end a run: `complete` and `stopped`. `escalated` does not — a wave that escalated is
halted until the coordinator rules on it, and the run carries on or is adopted — so the driver
appends `stopped` when the escalation it read leaves nothing to launch and nothing to rule on.
That word is what every surface reads the end of a run from ([`monitor.py`][monitor] `over`), and
the driver asks it there rather than keeping a rule of its own.

[monitor]: ../skills/crew/assets/monitor/monitor.py

### `live-source` — which source a lane's live children were read from

`lane`, `source`, `reason`. Appended by the dashboard when it could not read a lane's first-choice
source and read a fallback instead
([ADR-0012](adr/0012-the-statusline-tick-reads-the-sessions-files.md)).
`lane` is `claude` or `codex`, `source` is `sessions`, `command` or `bridge`, and `reason` says
what could not be read. One line per run: a statusline tick draws in silence
([ADR-0008](adr/0008-the-pinned-dashboard-lives-in-claude-codes-statusline.md)), so this line is
the only place a relocated sessions directory shows up, and appending it once keeps a surface that
redraws every two seconds from filling the run's own record.

### `monitor-error` — a monitor that exited with an error

`monitor`, `reason`. The wake monitor writes this line before it exits nonzero, so a failure is
visible in the run's record as well as on stderr. `monitor` names the monitor script and `reason`
is the same explanation printed after `MONITOR ERROR`.

### `session-cost` — what one session spent, in tokens

`ticket`, `executor`, `model`, `lane`, `session`, `input_tokens`, `output_tokens`,
`cache_read_tokens`, `cache_creation_tokens`, `total_tokens`, `detail`. One line per launched
child, appended by the cost pass at run completion
([`docs/monitor-dashboard.md`](monitor-dashboard.md)), plus one per started review axis, appended
by the `axis-end` Lifecycle Hook command — so a run's usage is in its own artifacts rather than
in transcripts a later agent would have to dig through, and the review lane can be graded from
the run's own log.

`lane` is what tells the two apart. A row with no `lane` is an implementing child's, which is
what every row written before reviews were costed is. A row carrying one is a review's, spelled
the way the `review` event spells it — the reviewing vendor and its model, `codex gpt-5.6-sol` —
and its `executor` is the reviewing vendor rather than the child's. `model` is the model the
session actually resolved to, read off the session's own record rather than the alias the caller
asked for, so `opus` is recorded as the id behind it — and empty where that record named no single
model, because the alias is already on the row, in `lane`, and the model field is a measurement.
An empty `model` beside real figures is that measurement missing, not a corrupt row: the counters
were read, the model the session resolved to was not.

The four token counters are disjoint and `total_tokens` is their sum, in both lanes: Codex reports
its cached tokens inside its input count and Claude reports them beside it, so the Codex figures
are converted before they are written. Reasoning tokens stay inside the output count, where their
vendor already counts them. `session` names every session the figures were read off,
comma-separated — a resumed or replaced child spent the ticket's tokens too. A review names its
own session, which is also what keeps it out of the child's row when the two share a vendor.

A review's figures are cumulative over its lineage, so a resumed invocation writes another line
that supersedes the first rather than a share to be added to it. **A consumer MUST take the last
line per review session id as what that review spent, and MUST NOT sum a session's lines** —
summing them bills the earlier invocation twice. The log is append-only and a bridge cannot know
whether a lineage will resume, so superseding is what "one figure per review" means here:
withholding an invocation's line until the lineage is provably over would leave no figure when a
permitted resume never happens.

A session whose usage could not be read carries `detail` and no figures at all: the line says
what went wrong and where to look, which is what makes a missing measurement visible rather than
a gap in the log. That covers a transcript the cost pass could not read, a review whose rollout or
result carried no counters — a rollout whose figures contradict each other counts as none — and a
review that never returned a result to read one from at all.

### `escalation`, `ruling`, `message` — an outgoing message, copied verbatim

Claude messages are written by the SendMessage hook below; Codex turn messages are written by
`codex_bridge.py` through the same machine-log writer. Neither path writes by hand: `role`
(`coordinator` or `child`), `from` and `to`, `message`, and `ticket` where the sender knows one.

`from` is the sender's own address on the message channel — the socket the harness bound for that
session, under the `uds:` scheme, read out of the sending session's environment by the hook. It is
what the receiver sees as the message's `from` and so what a reply to it is addressed to
([ADR-0023](adr/0023-the-coordinator-is-addressed-by-socket-not-by-name.md)). It is absent on a
record the hook did not write, and on one written by a session the harness exported no socket for.

`message` is the argument the sender gave the tool, byte for byte — no truncation, no
reformatting, no summary. A structured message is recorded as the object it was.

## Script entry points

```sh
machine_log.py --log <path> launch  --ticket NN --child NAME --workflow W --executor E \
                                    --model ID --effort E \
                                    [--branch B] [--worktree P] [--window W] [--account DIR]
machine_log.py --log <path> launch-failed --ticket NN --detail TEXT
machine_log.py --log <path> receipt --ticket NN --verdict landable|parked|failed \
                                    [--sha SHA] [--detail TEXT]
machine_log.py --log <path> merge   --ticket NN \
                                    --result clean|conflict|resolved|repaired|escalated \
                                    [--branch B] [--into B] [--sha SHA] [--detail TEXT]
machine_log.py --log <path> outcome --ticket NN --outcome completed|failed|parked|blocked \
                                    [--detail TEXT]
machine_log.py --log <path> review  --ticket NN --lane "VENDOR MODEL" --state running|returned \
                                    [--detail TEXT]
machine_log.py --log <path> witness --ticket NN --model ID --outcome checked|failed \
                                    --executor claude|codex \
                                    --reason TEXT --duration-seconds N \
                                    [--input-tokens N] [--output-tokens N] \
                                    [--cache-read-tokens N] [--cache-creation-tokens N] \
                                    [--total-tokens N]
machine_log.py --log <path> base-gate --status passed \
                                    --argument=COMMAND [--argument=ARG ...]
machine_log.py --log <path> base-gate --status not-configured
machine_log.py --log <path> advance --wave N \
                                    --decision launched|complete|escalated|interrupted|stopped \
                                    [--detail TEXT]
machine_log.py --log <path> live-source --lane claude|codex \
                                    --source sessions|command|bridge --reason TEXT
machine_log.py --log <path> monitor-error --monitor NAME --reason TEXT
machine_log.py --log <path> message --role coordinator|child [--ticket NN] [--to NAME] \
                                    --message TEXT
machine_log.py --log <path> session-cost --ticket NN --executor claude|codex --model ID \
                                    [--lane "VENDOR MODEL"] [--session IDS] \
                                    [--input-tokens N] [--output-tokens N] \
                                    [--cache-read-tokens N] [--cache-creation-tokens N] \
                                    [--total-tokens N] [--detail TEXT]
```

Each call appends exactly one line and exits 0. A value outside a closed set is a usage error:
nothing is appended and the exit code is 2, because a log that accepts an unknown verdict is a
log a later agent cannot trust.

## The SendMessage hook

A `PostToolUse` hook matching `SendMessage` copies the message that was just sent into the log.
It is installed on both sides of the channel: coordinator-side it captures rulings, child-side it
captures escalations. Nothing about it costs a model token.

The Codex bridge records a child's non-empty final turn when `watch` observes it, and a
coordinator's prompt when `send` submits it. `watch --once` performs exactly one evaluation round,
including a latest finished turn behind a currently busy turn. A launch given `--machine-log` and
`--ticket` stores those values in the state file, so later `watch` and `send` calls use the state
for the correct ticket; callers without a log path remain unlogged. A new observed message is
appended successfully before the state file's `finalMessage` cursor advances. If append fails, the
cursor stays unchanged and the next observation retries that message.

Installing it is the `install` entry point, run once per side against the settings file that side
starts with — the coordinator's own, and each child's `.claude/settings.local.json` in its
worktree, alongside the guard hooks:

```sh
machine_log.py --log <path> install   --settings <settings.json> --role child --ticket NN
machine_log.py --log <path> install   --settings <settings.json> --role coordinator \
    --run-dir <staged-run-dir>
machine_log.py --log <path> uninstall --settings <settings.json>
# Manual advisor: m=machine-log script, c=crew dir, r=staged run, s=settings, l=log, i=session ID.
$m --log $l install --settings $s --role coordinator --crew-dir $c --run-dir $r --session-id $i
$m --log $l uninstall --settings $s
```

`--role` says which side is being installed; `--ticket` is added where that side knows one — the
coordinator's hook serves every child at once and omits it. Installing twice replaces the entry
instead of doubling it — the log an entry writes is what identifies it as this run's, whichever
version of this script it runs — and every other hook already in the file is left where it is. A
coordinator install atomically adds the bounded-read `PreToolUse` hook in the same settings write;
a child install does not. `--session-id` confines that hook to one coordinator or advisor session.
`--run-dir` names the staged directory holding the ticket stubs and `spec.md`; its top-level
Markdown is judgment material the hook permits a whole-file `Read`. The two manual-advisor lines
use that same coordinator path; their explicit `--crew-dir` and `--run-dir` remain correct even if
the command is later run from the version-independent copy beside the log.

The commands registered run the run's own copies of this script and the bounded-read script,
written beside the log the first time a run installs and refreshed on every install after it. The
plugin is installed one directory per version, so an entry naming the plugin's own copy stops
working at the next upgrade; the run directory carries no version and outlives every one of them
(#37). A caller that keeps its own machine-log copy names it with `--hook-script`, and that path is
registered exactly as it was given.

`uninstall` removes every message or bounded-read entry installed for the `--log` it is given, and
nothing else — including an entry an older plugin version wrote for that run. That keeps an
upgrade from leaving stale hooks behind. It is called when the run ends, once for the coordinator's
settings file and once for each launched child's, and again when the run is cleared, so a finished
run leaves no hook behind. It is idempotent: a settings file carrying none of ours is left byte for
byte as it was found, and a block emptied of its only entry goes with it.

A missing or empty settings file is a fresh document to write — and nothing to uninstall from; a
file with content that does not parse, or whose `hooks` are not the shape this writes through, is
refused and left untouched, because the guard hooks live in that file too.

### Which side writes the line

A child's worktree sits inside the repository the coordinator runs in, and the enclosing
checkout's settings load in the child's session too, so every hook both files register fires on
one send. Each installed entry therefore carries `--scope`: the directory whose sends it copies —
the coordinator's checkout, or the child's worktree — taken from the settings file being written
(`<project>/.claude/settings.local.json` scopes to `<project>`) unless `--scope` names another.

The match is that directory exactly, never a directory beneath it, because a worktree is a
descendant of the checkout above it. A send from anywhere else is another side's, and the hook
writes nothing: one message, one line, under the role of the session that sent it.

The event name comes from the message the sender wrote, so the log reads the way the run ran:

| Message | `event` |
| --- | --- |
| anything sent by the coordinator | `ruling` |
| sent by a child whose last verb line is a valid `CREW ASK` | `escalation` |
| anything else sent by a child | `message` |

Only a child escalates — the coordinator is the top of the ladder — so the verb is read on the
child side alone.

The verbs are read line by line rather than off the opening of the message, because a child
composes its final turn freely and bundles the verb under the summary it wrote first as readily
as it sends the line bare. An escalation has this shape:
`CREW ASK <NN> <design|scope|doc-conflict|stuck|wrap-up> [— <body>] [ts=<unix>]`. `NN` is a numeric
ticket; the body, when present, is introduced by ` — `, and the protocol timestamp follows either
the kind or the body. Those five kinds are a closed set; a non-numeric ticket, an unknown kind, no
kind, or any other trailing word or body separator is malformed.
`CREW PARKED` and `CREW FAILED` carry their argument, and `CREW COMPLETE` carries a full
40-character sha and an optional `ts=` stamp. A verb counts only when it is a whole line of its
own, so the same words quoted inside a sentence are prose, as they are in the instructions that
taught them. Where a message carries more than one, the last one is the word it sent: a final turn
speaks once, and a child that withdrew an ask and finished anyway has said the thing it ended on.
The driver's rule table reads a child's word through the same judgment, so the log and the run can
never disagree about what was said.

A line that opens at the margin with one of those verb words and then fails its own whole-line
shape is a near miss rather than a silence, and the module names it separately
(`malformed_receipt`) so the driver can answer it instead of dropping it — one scripted bounce
quoting the line and naming all five ask kinds, then `failed` on a second miss (ADR-0015). A near
miss settles nothing, and prose that names a verb mid-line or quotes one indented is neither verb
nor near miss.

A child's own `CREW COMPLETE` is a claim about a sha, not a verified receipt, so it is copied in
as a `message`. Only the script that checked the sha writes a `receipt`, and the log therefore
carries exactly one per launched ticket.

### Zero-token output

The hook writes nothing to stdout, writes nothing to stderr, and always exits 0 — including
when it cannot write the log. Exit 2 would feed stderr back to the model, and any JSON carrying
`additionalContext` would insert into the live context the coordinator was promised nothing would
be inserted into. A failed append is reported on the one channel the model never sees: a
`systemMessage`, which the transcript shows the human.

A send is never blocked by the log. A hook input that is not a `SendMessage` call, or is not
JSON at all, appends nothing and exits 0.

## Why this one is Python

[ADR-0002](adr/0002-shell-scripts-drive-the-cli-not-workflows-or-the-sdk.md) chose scripts driving
the CLI over dynamic workflows and the Agent SDK; it rejected those two, not a language. The
zero-token layer already speaks stdlib Python where a shell script would need a dependency —
`codex_bridge.py`, `dispatch.py`, and this writer. This writer copies arbitrary message values,
including structured ones, into JSON verbatim, which bash cannot do without `jq`, and it reads a
hook payload off stdin. Stdlib Python is what the repo already requires and CI already runs.
