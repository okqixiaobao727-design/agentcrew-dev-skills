#!/usr/bin/env python3

"""The machine log: one append-only event log per run, written without model tokens.

Scripts append what they did — a child launched, a receipt verified, a branch merged, a ticket
settled, a wave advanced or halted — and a `PostToolUse` hook on `SendMessage` copies every
outgoing message in verbatim, so escalations and rulings land in the log as the messages are sent
rather than costing a coordinator turn to transcribe (ADR-0001). The Coordinator's matching
`PreToolUse` hook authorizes its address before `SendMessage` can deliver.

The file is JSON Lines: one object per line, appended and never rewritten, every line stamped
`%Y-%m-%dT%H:%M:%SZ` in UTC — the run's one timestamp format, so any two lines subtract to a
duration. The audience is a later auditing agent, not a human; `docs/machine-log.md` publishes the
schema this writes.

    machine_log.py --log <path> launch|launch-failed|receipt|merge|outcome|review|witness|
                                  base-gate|advance|live-source|monitor-error|session-cost|
                                  message ...
                                                              # a script's own event
    machine_log.py --log <path> hook --role coordinator|child  # a hook, on stdin
    machine_log.py --log <path> install|uninstall --settings <file> ...  # register it, or not

The hook never speaks on a channel a model reads: it writes nothing to stdout on the happy path,
writes nothing to stderr ever, and exits 0 even when the log cannot be written, because a send
that already happened must not be reported as a failure and an audit log must not block a run.
"""

import argparse
from collections.abc import Mapping
from dataclasses import dataclass, field
import datetime
import json
import os
import pathlib
import re
import shlex
import sys
import tempfile
from types import MappingProxyType

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# The tool whose outgoing messages the hook copies, and the event it is copied after. Anything
# else the hook is handed is not its business: a matcher is configuration, and configuration can
# be wrong.
MESSAGE_TOOL = "SendMessage"
HOOK_EVENT = "PostToolUse"
# The harness exports every session's own inbox socket into that session's environment, so the
# hook can name the sender without a lookup, a poll or a registry read of any kind. Under the
# `uds:` scheme it is the same literal the receiver sees as the message's `from`, which is what
# lets a ruling addressed to it be correlated back to the ticket that asked (ADR-0023).
SENDER_SOCKET_VARIABLE = "CLAUDE_CODE_MESSAGING_SOCKET"
ADDRESS_SCHEME = "uds:"
PRE_TOOL_EVENT = "PreToolUse"
BOUNDED_TOOLS = "Read|Grep|Glob|Bash"
# What a registered hook runs, and the subcommand that marks a registration as one of ours.
PYTHON = "python3"
HOOK_SUBCOMMAND = "hook"
GUARD_SUBCOMMAND = "guard"
# The name the run's own copy of this script is installed under, beside the log it writes. The
# copy is what keeps a registered command version-independent: the plugin directory a run installs
# from carries the version in its path, so an upgrade would leave the entry naming a file that is
# no longer there, while the run directory outlives every upgrade (#37).
SCRIPT_NAME = "machine_log.py"
BOUNDED_SCRIPT_NAME = "bounded_read.py"
COORDINATOR_CONTROL_SCRIPT_NAME = "coordinator_control.py"
# The directory a settings file lives in when it belongs to a checkout: `<project>/.claude/`.
SETTINGS_DIRECTORY = ".claude"

COORDINATOR = "coordinator"
CHILD = "child"

# The four verbs a child speaks, and the grammar that reads them off a message body. The hook
# classifies on one of them — an escalation announces itself, and it is the only thing a child
# sends that the coordinator must answer — while the driver rules on the other three; both read
# the body through `final_verb` so the log and the rule table can never disagree about what a
# child said. A receipt is deliberately not an event name of its own here — a child's own
# `CREW COMPLETE` is a claim, and only the verifying script's `receipt` event says whether it
# held, so the two must never share an event name in the log.
ESCALATION_VERB = "CREW ASK"
ESCALATION_KINDS = ("design", "scope", "doc-conflict", "stuck", "wrap-up")
COMPLETE_VERB = "CREW COMPLETE"
PARKED_VERB = "CREW PARKED"
FAILED_VERB = "CREW FAILED"

# The instructions the Driver records as rulings. Their markers are Machine-log protocol because
# the projection reads them back to reconstruct persistent episodes across Driver restarts.
RECHECK_MARKER = "CREW RECHECK"
RESEND_MARKER = "CREW RESEND"
NUDGE_MARKER = "CREW NUDGE"
MERGE_MARKER = "CREW MERGE"
ANCHOR_MARKER = "CREW ANCHOR"
HANDED_OVER_MARKER = "CREW RULED"
RECEIPT_WAIT_MARKERS = (RECHECK_MARKER, RESEND_MARKER, NUDGE_MARKER, MERGE_MARKER)
SEMANTIC_PREFIX = "semantic: "

# The optional stamp every message a child sends ends on, which is deduplication for the message
# bus rather than part of what the verb says.
TIMESTAMP_SUFFIX = r"(?: ts=\d+)?"
# Anchored to a whole line, because a child composes its final turn freely: the receipt is as
# often bundled under a summary as sent bare, and only a whole-line match tells the verb apart
# from the same words quoted out of the instructions that taught them. `CREW COMPLETE` carries a
# full 40-character sha, and `CREW ASK` carries one of the closed escalation kinds, so prose about
# either cannot pass for the verb.
VERB_GRAMMAR = (
    (COMPLETE_VERB, re.compile(rf"{COMPLETE_VERB} [0-9a-fA-F]{{40}}{TIMESTAMP_SUFFIX}")),
    (PARKED_VERB, re.compile(rf"{PARKED_VERB} \S.*")),
    (FAILED_VERB, re.compile(rf"{FAILED_VERB} \S.*")),
    (
        ESCALATION_VERB,
        re.compile(
            rf"{ESCALATION_VERB} \d+ (?:{'|'.join(map(re.escape, ESCALATION_KINDS))})"
            rf"(?: — .*?)?{TIMESTAMP_SUFFIX}"
        ),
    ),
)

LIVE = "live"
LANDABLE = "landable"
COMPLETED = "completed"
FAILED = "failed"
PARKED = "parked"
BLOCKED = "blocked"
CLEAN = "clean"
CONFLICT = "conflict"
REPAIRED = "repaired"
RESOLVED = "resolved"
ESCALATED = "escalated"

# The closed sets. A log that accepts an unknown verdict is a log a later agent cannot trust.
VERDICTS = (LANDABLE, PARKED, FAILED)
OUTCOMES = (COMPLETED, FAILED, PARKED, BLOCKED)
MERGE_RESULTS = (CLEAN, CONFLICT, REPAIRED, RESOLVED, ESCALATED)
LANDED_MERGE_RESULTS = (CLEAN, REPAIRED, RESOLVED)
# The two ends of one review. A review that started and never came back is a row the dashboard
# would leave standing, so the vocabulary is closed at "it is running" and "it is not".
REVIEW_STATES = ("running", "returned")
# What the run decided about carrying on after a wave. One of these per decision, and a decision
# is about a wave rather than a ticket, so this is the one event that carries no ticket. Two of
# them end a run — `complete`, every wave landed, and `stopped`, the chain halted on reasons no
# ruling will undo — and every surface that asks whether a run is over asks for exactly those two:
# `escalated` alone cannot say, because it is also the word for a wave awaiting a ruling.
DECISIONS = ("launched", "escalated", "complete", "interrupted", "stopped")
FINAL_DECISIONS = ("complete", "stopped")
HALTED_DECISIONS = ("escalated", "interrupted")
# The two lanes a child runs in. A usage figure is only readable against the executor that wrote
# it, so an executor this log does not know is an executor whose figures nobody can check.
EXECUTORS = ("claude", "codex")
WITNESS_OUTCOMES = ("checked", "partial", "failed")
BASE_GATE_STATUSES = ("passed", "not-configured")
# What a queued ticket's finding still leaves open (ADR-0028). The Run plan decides this
# vocabulary; it is spelled again here because this file is installed as a hook and runs with no
# crew module beside it, and `tests/test_machine_log.py` fails the moment the two disagree.
OPEN_WORDS = ("cause", "approach", "reach")
# Where a lane's live children were read from, when a dashboard had to read them from anywhere but
# its first choice (ADR-0012). The set is closed on the sources a lane actually has, so a line
# saying a dashboard fell back names something a reader can go and look at.
LIVE_SOURCES = ("sessions", "command", "bridge")
# What a session cost is made of: four disjoint counters and the total they must come to. A line
# carrying some of them, or a total that is not their sum, is arithmetic a later agent would trust
# and be wrong to, so it is refused like a value outside a closed set.
COST_COUNTERS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")
COST_TOTAL = "total_tokens"

LOG_FILE_MODE = 0o644


@dataclass(frozen=True)
class TicketFacts:
    """The immutable Machine-log facts about one ticket."""

    ticket: str
    events: tuple = ()
    first_launch: Mapping | None = None
    launch: Mapping | None = None
    launch_verification_failed: bool = False
    receipt: Mapping | None = None
    latest_settling_event: Mapping | None = None
    progress_event: Mapping | None = None
    settlement_state: str = LIVE
    unanswered_child_message: Mapping | None = None
    escalation: Mapping | None = None
    witness: Mapping | None = None
    fact_check_running: bool = False
    awaiting_receipt: bool = False
    awaiting_ruling: bool = False
    outstanding_nudge: bool = False
    merge_result: str | None = None
    merge_landed: bool = False
    semantic_conflict_detail: str | None = None
    merge_rework_requested: bool = False
    _instruction_messages: tuple = field(default=(), repr=False, compare=False)

    def instruction_count(self, marker):
        """Return how many correlated rulings start with `marker`."""
        return sum(message.lstrip().startswith(marker) for message in self._instruction_messages)


@dataclass(frozen=True)
class RunProjection:
    """The immutable current facts derived from one physically ordered Machine log."""

    tickets: Mapping
    latest_landed_merge: Mapping | None = None
    current_wave: int = 1
    ended: bool = False
    halted: bool = False

    def ticket(self, ticket_id):
        """Return the facts for `ticket_id`, or immutable empty facts when it is unknown."""
        ticket = str(ticket_id)
        return self.tickets.get(ticket, TicketFacts(ticket))


def _freeze(value):
    """Return an immutable copy of one JSON-compatible value."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _current_settlement_epoch(records):
    """Return the records that can settle the ticket's current launch epoch."""
    latest_blocked = None
    for index, record in enumerate(records):
        if record.get("event") == "outcome" and record.get("outcome") == BLOCKED:
            latest_blocked = index
    if latest_blocked is None:
        return records
    relaunched = next(
        (
            index
            for index, record in enumerate(records[latest_blocked + 1:], latest_blocked + 1)
            if record.get("event") == "launch"
        ),
        None,
    )
    return records[relaunched:] if relaunched is not None else records


def project(records):
    """Return an immutable factual projection of physically ordered `records`."""
    frozen_records = tuple(
        _freeze(record) for record in records if isinstance(record, Mapping)
    )
    events_by_ticket = {}
    launches_by_ticket = {}
    # Every address a child of this run has sent from. A coordinator replies to the address the
    # message arrived from — the tool's own contract instructs copying the inbound `from` into
    # `to`, and it is the one form that works whichever account the child runs on — so a ruling
    # naming no ticket is correlated through it. Only a child's own sends are collected: the
    # coordinator answers every ticket, so its address identifies none of them.
    addresses_by_ticket = {}
    for record in frozen_records:
        if record.get("ticket") is None:
            continue
        ticket = str(record["ticket"])
        events_by_ticket.setdefault(ticket, []).append(record)
        if record.get("event") == "launch":
            launches_by_ticket[ticket] = record
        origin = record.get("from")
        if record.get("role") == CHILD and isinstance(origin, str) and origin:
            addresses_by_ticket.setdefault(origin, ticket)

    # A child is indexed by every identity this run knows it under: the name it was launched with,
    # and the addresses above. The two key spaces are disjoint — an address carries its scheme —
    # so a log written before addresses were recorded correlates through the name exactly as it
    # always did.
    ticket_by_child = dict(addresses_by_ticket)
    for ticket, launch in launches_by_ticket.items():
        child = launch.get("child")
        if isinstance(child, str) and child:
            ticket_by_child.setdefault(child, ticket)

    def correlated_ticket(record):
        if record.get("ticket") is not None:
            return str(record["ticket"])
        recipient = record.get("to")
        if not isinstance(recipient, str):
            return None
        return ticket_by_child.get(recipient)

    current_wave = 1
    ended = False
    latest_advance = None
    latest_landed_merge = None
    episodes = {}
    for record in frozen_records:
        if (
            record.get("event") == "merge"
            and record.get("result") in LANDED_MERGE_RESULTS
        ):
            latest_landed_merge = record
        if record.get("event") == "advance":
            latest_advance = record
            decision = record.get("decision")
            if decision in FINAL_DECISIONS:
                ended = True
            if decision == "launched":
                # A Wave launched after a final decision is a Run that has not ended after all:
                # the Run grew, and the latest decision is the one that says where it stands. The
                # walk is in log order, so a later `complete` says it has ended again.
                ended = False
                try:
                    current_wave = int(record.get("wave"))
                except (TypeError, ValueError):
                    pass

        ticket = correlated_ticket(record)
        if ticket is None:
            continue
        episode = episodes.setdefault(
            ticket,
            {
                "unanswered_child_message": None,
                "escalation": None,
                "witness": None,
                "fact_check_running": False,
                "awaiting_receipt": False,
                "awaiting_ruling": False,
                "outstanding_nudge": False,
                "instruction_messages": [],
            },
        )
        event = record.get("event")
        message = record.get("message")
        child_message = event in ("message", "escalation") and record.get("role") == CHILD
        if child_message:
            episode["unanswered_child_message"] = record
            verb, _line = final_verb(message)
            if ended and (
                event == "escalation"
                or verb in (COMPLETE_VERB, PARKED_VERB, FAILED_VERB)
                or malformed_receipt(message) is not None
            ):
                ended = False
            if event == "escalation":
                episode["escalation"] = record
                episode["witness"] = None
                episode["fact_check_running"] = True
            episode["awaiting_ruling"] = False
            episode["outstanding_nudge"] = False
        elif event == "witness":
            pending = episode["unanswered_child_message"]
            if (pending or {}).get("event") == "escalation":
                episode["witness"] = record
        elif event in ("receipt", "ruling", "outcome"):
            pending = episode["unanswered_child_message"]
            pending_verb, pending_line = final_verb((pending or {}).get("message"))
            completion_sha = (
                pending_line.split()[2].lower()
                if pending_verb == COMPLETE_VERB and pending_line is not None
                else None
            )
            receipt_matches = (
                event == "receipt"
                and completion_sha is not None
                and isinstance(record.get("sha"), str)
                and record["sha"].lower() == completion_sha
            )
            ruling_words = message.lstrip().split() if isinstance(message, str) else []
            recheck_matches = (
                event == "ruling"
                and completion_sha is not None
                and len(ruling_words) >= 4
                and " ".join(ruling_words[:2]) == RECHECK_MARKER
                and ruling_words[3].lower() == completion_sha
            )
            if (
                (pending_verb == COMPLETE_VERB and (receipt_matches or recheck_matches))
                or pending_verb != COMPLETE_VERB
            ):
                episode["unanswered_child_message"] = None

        if event == "ruling" and isinstance(message, str):
            episode["instruction_messages"].append(message)
            if any(message.lstrip().startswith(marker) for marker in RECEIPT_WAIT_MARKERS):
                episode["awaiting_receipt"] = True
            episode["awaiting_ruling"] = message.lstrip().startswith(HANDED_OVER_MARKER)
            if episode["awaiting_ruling"]:
                episode["fact_check_running"] = False
            if message.lstrip().startswith(NUDGE_MARKER):
                episode["outstanding_nudge"] = True
            elif not message.lstrip().startswith(HANDED_OVER_MARKER):
                episode["outstanding_nudge"] = False
        elif event == "receipt":
            episode["awaiting_receipt"] = False
        elif event == "review":
            episode["outstanding_nudge"] = False

    tickets = {}
    for ticket, events in events_by_ticket.items():
        launches = [record for record in events if record.get("event") == "launch"]
        launch_verification_failed = False
        launch_seen = False
        for record in events:
            if record.get("event") == "launch":
                launch_seen = True
                launch_verification_failed = False
            elif record.get("event") == "launch-failed" and launch_seen:
                launch_verification_failed = True
        receipts = [record for record in events if record.get("event") == "receipt"]
        latest_settling_event = None
        progress_event = None
        merge_result = None
        classified_conflict = None
        for record in events:
            event = record.get("event")
            if event in ("receipt", "outcome") and (
                record.get("verdict") or record.get("outcome")
            ):
                latest_settling_event = record
            if event == "merge":
                if record.get("result"):
                    merge_result = str(record["result"])
                detail = record.get("detail")
                if record.get("result") == CONFLICT and isinstance(detail, str):
                    classified_conflict = detail

        for record in _current_settlement_epoch(events):
            event = record.get("event")
            if event == "receipt":
                if record.get("verdict") in VERDICTS:
                    progress_event = record
            elif event == "outcome":
                if record.get("outcome") in OUTCOMES:
                    progress_event = record
            elif event == "merge":
                if record.get("result") in MERGE_RESULTS:
                    progress_event = record

        semantic_conflict_detail = None
        if (
            merge_result == ESCALATED
            and isinstance(classified_conflict, str)
            and classified_conflict.startswith(SEMANTIC_PREFIX)
        ):
            semantic_conflict_detail = classified_conflict[len(SEMANTIC_PREFIX):]
        merge_rework_requested = False
        if progress_event is not None and (
            progress_event.get("event") == "merge"
            and progress_event.get("result") == ESCALATED
        ):
            progress_index = next(
                index for index, record in enumerate(events) if record is progress_event
            )
            merge_rework_requested = any(
                record.get("event") == "ruling"
                and isinstance(record.get("message"), str)
                and record["message"].lstrip().startswith(MERGE_MARKER)
                for record in events[progress_index + 1:]
            )

        episode = episodes.get(ticket, {})
        tickets[ticket] = TicketFacts(
            ticket=ticket,
            events=tuple(events),
            first_launch=launches[0] if launches else None,
            launch=launches[-1] if launches else None,
            launch_verification_failed=launch_verification_failed,
            receipt=receipts[-1] if receipts else None,
            latest_settling_event=latest_settling_event,
            progress_event=progress_event,
            settlement_state=settlement_state(events, ticket),
            unanswered_child_message=episode.get("unanswered_child_message"),
            escalation=episode.get("escalation"),
            witness=episode.get("witness"),
            fact_check_running=episode.get("fact_check_running", False),
            awaiting_receipt=episode.get("awaiting_receipt", False),
            awaiting_ruling=episode.get("awaiting_ruling", False),
            outstanding_nudge=episode.get("outstanding_nudge", False),
            merge_result=merge_result,
            merge_landed=merge_result in LANDED_MERGE_RESULTS,
            semantic_conflict_detail=semantic_conflict_detail,
            merge_rework_requested=merge_rework_requested,
            _instruction_messages=tuple(episode.get("instruction_messages", ())),
        )
    return RunProjection(
        tickets=MappingProxyType(tickets),
        latest_landed_merge=latest_landed_merge,
        current_wave=current_wave,
        ended=ended,
        halted=(
            latest_advance is not None
            and latest_advance.get("decision") in HALTED_DECISIONS
        ),
    )


def read_records(path):
    """Return the normal runtime records in physical order, skipping incomplete lines."""
    try:
        text = pathlib.Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return ()
    records = []
    for line in text.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return tuple(records)


def settlement_state(records, ticket):
    """Return one of live, landable, completed, failed, parked, or blocked.

    Precedence, from highest to lowest:

    ================  ==============================================================
    Log evidence      State
    ================  ==============================================================
    latest outcome    that outcome; it refines a receipt and never un-settles it
    latest receipt    its verdict, except landable plus a landed merge is completed
    neither           live
    ================  ==============================================================

    A completed outcome needs no receipt or merge lookup here: the tracker close that writes it
    only happens after a landed merge. A launch after a blocked outcome starts a current epoch, so
    the old derived block remains history without settling the relaunched child. For a landable
    receipt, the latest merge result must be clean or repaired; a conflict or escalation leaves the
    ticket landable rather than done.
    """
    ticket = str(ticket)
    ticket_records = [record for record in records if str(record.get("ticket")) == ticket]
    ticket_records = _current_settlement_epoch(ticket_records)

    latest_outcome = None
    latest_receipt = None
    latest_merge = None
    for record in ticket_records:
        event = record.get("event")
        if event == "outcome" and record.get("outcome") in OUTCOMES:
            latest_outcome = str(record["outcome"])
        elif event == "receipt" and record.get("verdict") in VERDICTS:
            latest_receipt = str(record["verdict"])
        elif event == "merge" and record.get("result") in MERGE_RESULTS:
            latest_merge = str(record["result"])
    if latest_outcome is not None:
        return latest_outcome
    if latest_receipt == LANDABLE and latest_merge in LANDED_MERGE_RESULTS:
        return COMPLETED
    if latest_receipt is not None:
        return latest_receipt
    return LIVE


def now():
    """The current moment in the run's one timestamp format."""
    return datetime.datetime.now(datetime.timezone.utc).strftime(TIMESTAMP_FORMAT)


def entry(event, **fields):
    """One log record: the stamp and the event first, then the fields that were supplied.

    A field left unset is left out rather than written empty, so a reader can tell "not recorded"
    from "recorded as nothing".
    """
    record = {"ts": now(), "event": event}
    record.update({name: value for name, value in fields.items() if value is not None})
    return record


def append(path, record):
    """Append one record to the log at `path`, returning nothing; an unwritable log raises OSError.

    The line is handed to a single `write` on a descriptor opened `O_APPEND`, which is what lets
    the monitor, the merge driver and one hook per child share a file: their lines interleave,
    their characters never do.
    """
    line = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, LOG_FILE_MODE)
    try:
        os.write(descriptor, line)
    finally:
        os.close(descriptor)


def final_verb(message):
    """The last verb line of `message` as `(verb, line)`, or `(None, None)` where it speaks none.

    The last one, because a final turn speaks once: a child that withdrew an ask and finished
    anyway has said the thing it ended on, and reading the first verb instead would settle it on
    a word it had already taken back.
    """
    if not isinstance(message, str):
        return None, None
    for line in reversed(message.split("\n")):
        # Trailing padding only. Indentation is the way a quoted example is set apart from the
        # prose around it, so a line that starts anywhere but the margin is being shown rather
        # than spoken; nothing is quoted by padding its end, and refusing a padded line — a
        # stray space, a `\r` off a CRLF sender — would cost a receipt for a difference nobody
        # can see.
        line = line.rstrip()
        for verb, pattern in VERB_GRAMMAR:
            if pattern.fullmatch(line):
                return verb, line
    return None, None


def malformed_receipt(message):
    """The last line of `message` that reached for a verb and missed its shape, or None.

    Two things `final_verb` cannot tell apart both come back from it as `(None, None)`: a message
    that speaks no verb, and a message that tried to speak one and got the shape wrong. This is the
    second of them, and it exists so a refusal can be answered rather than dropped — a receipt with
    prose appended to its verb line once left a finished ticket reading `waiting` for eight minutes
    with nothing anywhere saying why (ADR-0015).

    Deliberately narrow. A line counts as a near miss only where it opens, at the margin, with a
    verb word the grammar knows and then fails that verb's whole-line pattern: prose that names a
    verb mid-line has not reached for one, an indented line is quoting rather than speaking — the
    same exemption `final_verb` grants — and a message carrying any valid verb line has said its
    word and is never a near miss whatever else stands in it. The grammar itself is untouched, so
    nothing this recognises can settle anything.
    """
    if not isinstance(message, str):
        return None
    if final_verb(message)[0] is not None:
        return None
    for line in reversed(message.split("\n")):
        line = line.rstrip()
        for verb, _pattern in VERB_GRAMMAR:
            if line == verb or line.startswith(verb + " "):
                return line
    return None


def message_event(message, role):
    """The event name for an outgoing message: what the role sends, or a child's escalation.

    Only a child escalates — the coordinator is the top of the ladder — so the verb is read on
    the child side alone, and everything the coordinator sends is a ruling whatever it opens with.
    """
    if role == COORDINATOR:
        return "ruling"
    if final_verb(message)[0] == ESCALATION_VERB:
        return "escalation"
    return "message"


def hook_record(payload, role, ticket, sender=None):
    """The record for a hook payload, or None when the payload carries no sent message.

    A payload that is not a `SendMessage` call, or not the shape one has, is not an error: the
    hook's job is to copy what it recognises and stay out of the way of everything else.

    `sender` is the address this side sends from, passed in by the entry point that read it rather
    than read here: this function stays a pure function of its arguments, so the record it builds
    is testable without an environment.
    """
    if not isinstance(payload, dict) or payload.get("tool_name") != MESSAGE_TOOL:
        return None
    arguments = payload.get("tool_input")
    if not isinstance(arguments, dict) or "message" not in arguments:
        return None
    recipient = arguments.get("to")
    return entry(
        message_event(arguments["message"], role),
        ticket=ticket,
        role=role,
        # The identity behind the title: what the receiver of this message sees as its `from`,
        # and so what a reply to it is addressed to.
        **{"from": sender if isinstance(sender, str) and sender.strip() else None},
        to=recipient if isinstance(recipient, str) else None,
        # Verbatim: the argument the sender gave the tool, neither truncated nor reformatted.
        message=arguments["message"],
    )


def sender_address(environment=None):
    """The whole address this session receives at, or None where the harness exported none."""
    socket = (environment if environment is not None else os.environ).get(
        SENDER_SOCKET_VARIABLE
    )
    if not isinstance(socket, str) or not socket.strip():
        return None
    return ADDRESS_SCHEME + socket


def sent_from_scope(payload, scope):
    """Whether this send came from the session whose settings registered this hook.

    A child's worktree sits inside the repository the coordinator runs in, and Claude Code loads
    the enclosing checkout's settings in the child's session too: every hook both files register
    fires on one send. Without this check the coordinator's hook copies the child's message in a
    second time and labels it a ruling, so the log counts two messages where the run had one and
    attributes the child's words to the coordinator (#37).

    The scope is the directory the install was registered for, and the match is that directory
    exactly — a worktree is a descendant of the checkout above it, so containment would let the
    same duplicate through. A hook registered without a scope is one an older install wrote, and
    it copies what it is handed as it always did.
    """
    if scope is None:
        return True
    sender = payload.get("cwd")
    if not isinstance(sender, str):
        return False
    return os.path.realpath(sender) == os.path.realpath(scope)


def run_hook(args):
    """Copy the message that was just sent into the log; returns 0 always, and always will.

    A send has already happened by the time this runs, so there is no failure left to report:
    the exit code is the hook's only channel a model reads, and it stays silent.
    """
    try:
        payload = json.loads(sys.stdin.read())
    except (ValueError, OSError):
        return 0
    if not isinstance(payload, dict) or not sent_from_scope(payload, args.scope):
        return 0
    record = hook_record(payload, args.role, args.ticket, sender_address())
    if record is None:
        return 0

    def record_send():
        try:
            append(args.log, record)
        except OSError as error:
            # The one channel left that no model reads. Reporting it any louder — stderr, a nonzero
            # exit — would feed the model a failure of bookkeeping it is not meant to know about.
            json.dump({"systemMessage": f"machine log: {args.log}: {error}"}, sys.stdout)
        return 0

    if args.role != COORDINATOR:
        return record_send()
    coordinator_control = _coordinator_control()
    try:
        return coordinator_control.CoordinatorControl(
            pathlib.Path(args.log).parent
        ).authorized_action(record_send)
    except coordinator_control.CoordinatorControlError as error:
        json.dump({"systemMessage": str(error)}, sys.stdout)
        return 0


def run_guard(args):
    """Deny a stale Coordinator before SendMessage can deliver; returns zero for the hook."""
    coordinator_control = _coordinator_control()
    try:
        payload = json.loads(sys.stdin.read())
    except (ValueError, OSError):
        return 0
    if (
        not isinstance(payload, dict)
        or payload.get("tool_name") != MESSAGE_TOOL
        or not sent_from_scope(payload, args.scope)
    ):
        return 0
    try:
        coordinator_control.CoordinatorControl(
            pathlib.Path(args.log).parent
        ).authorized_action(lambda: None)
    except coordinator_control.CoordinatorControlError as error:
        json.dump({
            "hookSpecificOutput": {
                "hookEventName": PRE_TOOL_EVENT,
                "permissionDecision": "deny",
                "permissionDecisionReason": str(error),
            }
        }, sys.stdout)
    return 0


def _coordinator_control():
    """Import and return the control module beside the run-local hook copy.

    Child hooks do not need the module, and old run-local copies do not carry it. Delaying this
    import until a Coordinator hook executes keeps those version-independent child hooks usable.
    """
    import coordinator_control
    return coordinator_control


def absolute(path):
    """`path` as an absolute path, without following the symlinks along the way.

    A hook runs in the cwd of the session that fired it — a child's worktree — while the paths
    it was registered with were spelled wherever the install ran. A relative one recorded here
    would resolve against the wrong directory at fire time and the escalation would land in a
    file nobody reads, so the spelling is settled at the boundary. Symlinks are left alone: the
    operator's own name for the run directory is the one the log should be found under.
    """
    return os.path.abspath(str(path))


def hook_command(script, log, role, ticket, scope):
    """The shell command a registered hook runs: this script, in hook mode, for that side.

    Every path in it is absolute, because the cwd it will run in is not the cwd it was written
    in — and the scope is the one it may write for.
    """
    command = (
        f"{shlex.quote(PYTHON)} {shlex.quote(absolute(script))}"
        f" --log {shlex.quote(absolute(log))}"
    )
    command += f" {HOOK_SUBCOMMAND} --role {shlex.quote(role)}"
    if ticket is not None:
        command += f" --ticket {shlex.quote(ticket)}"
    command += f" --scope {shlex.quote(absolute(scope))}"
    return command


def guard_command(script, log, scope):
    """The Coordinator's PreToolUse SendMessage authorization command."""
    return (
        f"{shlex.quote(PYTHON)} {shlex.quote(absolute(script))}"
        f" --log {shlex.quote(absolute(log))} {GUARD_SUBCOMMAND}"
        f" --scope {shlex.quote(absolute(scope))}"
    )


def bounded_hook_command(script, log, crew_dir, run_dir, session_id=None):
    """The coordinator's bounded-read command, owned by the same run log as its message hook."""
    command = (
        f"{shlex.quote(PYTHON)} {shlex.quote(absolute(script))} hook"
        f" --crew-dir {shlex.quote(absolute(crew_dir))}"
        f" --run-dir {shlex.quote(absolute(run_dir))}"
        f" --owner-log {shlex.quote(absolute(log))}"
    )
    if session_id is not None:
        command += f" --session-id {shlex.quote(session_id)}"
    return command


def settings_scope(settings):
    """The directory whose sessions a settings file governs: the checkout it sits in.

    A settings file lives at `<project>/.claude/settings.local.json`, so the project above it is
    the cwd its sessions send from. Anywhere else, the file's own directory is the best answer
    there is, and `--scope` is there for a caller who knows better.
    """
    directory = pathlib.Path(absolute(pathlib.Path(settings).parent))
    if directory.name == SETTINGS_DIRECTORY:
        return directory.parent
    return directory


def run_script(log):
    """Where a run keeps the copy of this script its hooks run: beside its own log.

    The run directory carries no plugin version, which is the whole point — an entry pointing into
    the versioned plugin tree stops working the moment the plugin is upgraded (#37).
    """
    return pathlib.Path(absolute(pathlib.Path(log).parent / SCRIPT_NAME))


def bounded_run_script(log):
    """Where a run keeps the version-independent copy of its bounded-read runtime."""
    return pathlib.Path(absolute(pathlib.Path(log).parent / BOUNDED_SCRIPT_NAME))


def materialise_script(source, destination):
    """Put a copy of `source` at `destination`, replacing whatever was there; raises OSError.

    Written to a neighbouring temporary file and moved into place, so a hook firing during an
    install runs either the old copy or the new one and never half of either.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    if absolute(source) == str(destination):
        return destination
    handle, temporary = tempfile.mkstemp(
        dir=str(destination.parent), prefix=f".{destination.name}."
    )
    try:
        with open(handle, "wb") as copy:
            copy.write(pathlib.Path(source).read_bytes())
        os.replace(temporary, destination)
    except BaseException:
        pathlib.Path(temporary).unlink(missing_ok=True)
        raise
    return destination


def settings_shape_is_sound(settings):
    """Whether the two containers this install writes through are the shape they must be.

    Anything else is a file this script did not write and does not understand, and a settings
    file it does not understand is one it must not rewrite.
    """
    if not isinstance(settings, dict):
        return False
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        return False
    for event, matcher in (
        (HOOK_EVENT, MESSAGE_TOOL),
        (PRE_TOOL_EVENT, BOUNDED_TOOLS),
        (PRE_TOOL_EVENT, MESSAGE_TOOL),
    ):
        events = hooks.get(event, [])
        if not isinstance(events, list):
            return False
        if not all(
            isinstance(block.get("hooks", []), list)
            for block in events
            if isinstance(block, dict) and block.get("matcher") == matcher
        ):
            return False
    return True


def command_log(command):
    """The log a registered command writes in hook mode, or None when it is not one of ours.

    The command is read as the shell will read it — split into its words, the `--log` argument
    taken whole — rather than searched for a substring, because one log's path is a prefix of
    another's the moment a run directory is named after it and a hook nobody installed here must
    never be mistaken for one this script owns.
    """
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    if not (
        (HOOK_SUBCOMMAND in words and "--role" in words)
        or (GUARD_SUBCOMMAND in words and "--scope" in words)
    ):
        return None
    for index, word in enumerate(words):
        if word == "--log" and index + 1 < len(words):
            return absolute(words[index + 1])
        if word.startswith("--log="):
            return absolute(word[len("--log="):])
    return None


def registered_for(hook, log):
    """Whether this registered hook is an entry an install for `log` wrote.

    The log it writes is what identifies it, not the script that runs it: an upgrade changes
    the registered path — the plugin's copy carries its version — and an entry the new install
    failed to recognise would be left beside the new one, firing an old writer at the same log
    (#37). One run owns one entry per settings file, whichever version wrote it; another run's
    entry, and a hook that is not this script's at all, are nobody's business but their owners'.
    """
    if not isinstance(hook, dict):
        return False
    return command_log(str(hook.get("command", ""))) == absolute(log)


def message_blocks(settings):
    """Every block of the settings document that claims the outgoing-message matcher."""
    events = settings.get("hooks", {}).get(HOOK_EVENT, [])
    return [
        block for block in events
        if isinstance(block, dict) and block.get("matcher") == MESSAGE_TOOL
    ]


def bounded_blocks(settings):
    """Every block of the settings document that claims the bounded-read matchers."""
    events = settings.get("hooks", {}).get(PRE_TOOL_EVENT, [])
    return [
        block for block in events
        if isinstance(block, dict) and block.get("matcher") == BOUNDED_TOOLS
    ]


def guard_blocks(settings):
    """Every PreToolUse block that claims the outgoing-message matcher."""
    events = settings.get("hooks", {}).get(PRE_TOOL_EVENT, [])
    return [
        block for block in events
        if isinstance(block, dict) and block.get("matcher") == MESSAGE_TOOL
    ]


def bounded_command_log(command):
    """The run log owning a bounded-read registration, or None for another command."""
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    if "hook" not in words or "--crew-dir" not in words or "--owner-log" not in words:
        return None
    for index, word in enumerate(words):
        if word == "--owner-log" and index + 1 < len(words):
            return absolute(words[index + 1])
        if word.startswith("--owner-log="):
            return absolute(word[len("--owner-log="):])
    return None


def bounded_registered_for(hook, log):
    """Whether this bounded-read entry belongs to the install for `log`."""
    return (
        isinstance(hook, dict)
        and bounded_command_log(str(hook.get("command", ""))) == absolute(log)
    )


def install_hook(settings, command, log):
    """The settings document with this hook registered in it, and nothing else disturbed.

    An entry already writing `log` is replaced rather than added to, so installing twice — a
    resumed run, a re-prepared worktree, a run that outlived a plugin upgrade — leaves one hook
    and not two.
    """
    hooks = settings.setdefault("hooks", {})
    events = hooks.setdefault(HOOK_EVENT, [])
    blocks = message_blocks(settings)
    if blocks:
        block = blocks[0]
    else:
        block = {"matcher": MESSAGE_TOOL, "hooks": []}
        events.append(block)
    block["hooks"] = [
        hook for hook in block.get("hooks", []) if not registered_for(hook, log)
    ]
    block["hooks"].append({"type": "command", "command": command})
    return settings


def install_bounded_hook(settings, command, log):
    """Return settings with one bounded-read entry for `log` and other hooks preserved."""
    hooks = settings.setdefault("hooks", {})
    events = hooks.setdefault(PRE_TOOL_EVENT, [])
    blocks = bounded_blocks(settings)
    if blocks:
        block = blocks[0]
    else:
        block = {"matcher": BOUNDED_TOOLS, "hooks": []}
        events.append(block)
    block["hooks"] = [
        hook for hook in block.get("hooks", []) if not bounded_registered_for(hook, log)
    ]
    block["hooks"].append({"type": "command", "command": command})
    return settings


def install_guard_hook(settings, command, log):
    """Return settings with this Run's Coordinator SendMessage guard installed once."""
    hooks = settings.setdefault("hooks", {})
    events = hooks.setdefault(PRE_TOOL_EVENT, [])
    blocks = guard_blocks(settings)
    if blocks:
        block = blocks[0]
    else:
        block = {"matcher": MESSAGE_TOOL, "hooks": []}
        events.append(block)
    block["hooks"] = [
        hook for hook in block.get("hooks", []) if not registered_for(hook, log)
    ]
    block["hooks"].append({"type": "command", "command": command})
    return settings


def uninstall_hook(settings, log):
    """The settings document with every entry installed for `log` taken out of it.

    Every unrelated hook stays exactly where it is, and a block this leaves empty goes with the
    entry that was its only occupant — an empty matcher block registers nothing and was not there
    before the install. Returns whether anything was removed, so a file with nothing of ours in it
    is left byte for byte as it was found.
    """
    removed = False
    events = settings.get("hooks", {}).get(HOOK_EVENT, [])
    for block in message_blocks(settings):
        registered = block.get("hooks", [])
        kept = [hook for hook in registered if not registered_for(hook, log)]
        if len(kept) == len(registered):
            continue
        removed = True
        block["hooks"] = kept
        if not kept:
            events.remove(block)
    return removed


def uninstall_bounded_hook(settings, log):
    """Return whether this run's bounded-read entry was removed from settings."""
    removed = False
    events = settings.get("hooks", {}).get(PRE_TOOL_EVENT, [])
    for block in bounded_blocks(settings):
        registered = block.get("hooks", [])
        kept = [hook for hook in registered if not bounded_registered_for(hook, log)]
        if len(kept) == len(registered):
            continue
        removed = True
        block["hooks"] = kept
        if not kept:
            events.remove(block)
    return removed


def uninstall_guard_hook(settings, log):
    """Return whether this Run's Coordinator SendMessage guard was removed."""
    removed = False
    events = settings.get("hooks", {}).get(PRE_TOOL_EVENT, [])
    for block in guard_blocks(settings):
        registered = block.get("hooks", [])
        kept = [hook for hook in registered if not registered_for(hook, log)]
        if len(kept) == len(registered):
            continue
        removed = True
        block["hooks"] = kept
        if not kept:
            events.remove(block)
    return removed


def read_settings(path):
    """The settings document at `path`, or the reason it must not be rewritten.

    Returns the document and None, or None and the message to print: a missing or empty file is a
    fresh document to write, and anything this script did not write and does not understand is a
    file it refuses to touch, because the guard hooks live there too.
    """
    try:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        settings = json.loads(text.strip() or "{}")
    except (OSError, UnicodeDecodeError, ValueError) as error:
        return None, f"machine log: {path}: {error}"
    if not settings_shape_is_sound(settings):
        return None, f"machine log: {path}: not a settings document"
    return settings, None


def write_settings(path, settings):
    """Write the settings document back; returns 0, or 1 when it cannot be written."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    except OSError as error:
        print(f"machine log: {path}: {error}", file=sys.stderr)
        return 1
    print(path)
    return 0


def run_install(args):
    """Register the hook in a settings file; returns 0, or 1 when that file cannot be read.

    By default the command registered runs the run's own copy of this script rather than the one
    installing, because the plugin is installed one directory per version: an entry naming the
    plugin's copy stops working at the next upgrade, while the run directory outlives every one of
    them (#37). The copy is refreshed from the script that is installing, so an upgraded plugin's
    log writer is the one a resumed run's hooks go on running. A caller that keeps its own copy
    names it with `--hook-script` and that path is registered as it was given.
    """
    if args.role == COORDINATOR and args.run_dir is None:
        print("machine log: coordinator install requires --run-dir", file=sys.stderr)
        return 1
    path = pathlib.Path(args.settings)
    settings, problem = read_settings(path)
    if problem is not None:
        print(problem, file=sys.stderr)
        return 1
    try:
        source = pathlib.Path(__file__).resolve()
        script = (
            absolute(args.hook_script) if args.hook_script is not None
            else str(materialise_script(source, run_script(args.log)))
        )
        bounded_script = None
        if args.role == COORDINATOR:
            control_source = source.with_name(COORDINATOR_CONTROL_SCRIPT_NAME)
            if not control_source.exists() and args.crew_dir is not None:
                control_source = (
                    pathlib.Path(args.crew_dir) / "assets" / COORDINATOR_CONTROL_SCRIPT_NAME
                )
            materialise_script(
                control_source,
                pathlib.Path(args.log).parent / COORDINATOR_CONTROL_SCRIPT_NAME,
            )
            bounded_script = materialise_script(
                source.with_name(BOUNDED_SCRIPT_NAME),
                bounded_run_script(args.log),
            )
    except OSError as error:
        print(f"machine log: {error}", file=sys.stderr)
        return 1
    scope = args.scope if args.scope is not None else settings_scope(path)
    command = hook_command(script, args.log, args.role, args.ticket, scope)
    install_hook(settings, command, args.log)
    if args.role == COORDINATOR:
        install_guard_hook(settings, guard_command(script, args.log, scope), args.log)
        crew_dir = args.crew_dir
        if crew_dir is None and source.parent.name == "assets":
            crew_dir = source.parent.parent
        if crew_dir is None:
            print(
                "machine log: coordinator install from a copied script requires --crew-dir",
                file=sys.stderr,
            )
            return 1
        session_id = args.session_id
        if session_id is None:
            session_id = os.environ.get("CLAUDE_CODE_SESSION_ID")
        bounded = bounded_hook_command(
            bounded_script, args.log, crew_dir, args.run_dir, session_id
        )
        install_bounded_hook(settings, bounded, args.log)
    return write_settings(path, settings)


def run_uninstall(args):
    """Take this run's hooks out of a settings file; returns 0, or 1 on a file it must not touch.

    What it removes is every message, authorization and bounded-read entry owned by this Run's
    log, whichever version of this script installed it, and nothing else. Idempotent by
    construction: a file that carries none of ours is left exactly as it was found and a second
    call has nothing left to do. Another Run's entry and unrelated hooks stay where they are.
    """
    path = pathlib.Path(args.settings)
    if not path.exists():
        return 0
    settings, problem = read_settings(path)
    if problem is not None:
        print(problem, file=sys.stderr)
        return 1
    removed = uninstall_hook(settings, args.log)
    removed = uninstall_bounded_hook(settings, args.log) or removed
    removed = uninstall_guard_hook(settings, args.log) or removed
    if not removed:
        return 0
    return write_settings(path, settings)


def run_event(args):
    """Append one script event, named by the subcommand called; returns 0, or 1 on an OSError."""
    fields = {
        name: value
        for name, value in vars(args).items()
        if name not in ("log", "event", "handler")
    }
    try:
        append(args.log, entry(args.event, **fields))
    except OSError as error:
        print(f"machine log: {args.log}: {error}", file=sys.stderr)
        return 1
    return 0


def run_message(args):
    """Append one outgoing message; returns 0, or 1 on an OSError."""
    record = entry(
        message_event(args.message, args.role),
        ticket=args.ticket,
        role=args.role,
        to=args.to,
        message=args.message,
    )
    try:
        append(args.log, record)
    except OSError as error:
        print(f"machine log: {args.log}: {error}", file=sys.stderr)
        return 1
    return 0


def cost_problem(args):
    """Why this session cost contradicts itself, or None when it holds together.

    A session cost answers one of two questions and never both: what the child spent, in all five
    figures, or why nobody could tell. Anything between the two is a line whose reader cannot know
    which of the two it is holding.
    """
    counters = [getattr(args, name) for name in COST_COUNTERS]
    total = getattr(args, COST_TOTAL)
    figures = [value for value in counters + [total] if value is not None]
    if not figures:
        if args.detail is None:
            return "a session cost with no figures carries the diagnosis that says why"
        return None
    if len(figures) < len(COST_COUNTERS) + 1:
        return f"a session cost carries all of {', '.join(COST_COUNTERS)} and {COST_TOTAL}, or none"
    if args.detail is not None:
        return "a session cost carries its figures or a diagnosis, never both"
    if any(value < 0 for value in figures):
        return "a token count is never negative"
    if total != sum(counters):
        return f"{COST_TOTAL} {total} is not the sum of the four counters"
    return None


def run_session_cost(args):
    """Append one session cost; returns 0, or 2 for a record that contradicts itself."""
    problem = cost_problem(args)
    if problem is not None:
        print(f"machine log: session-cost: {problem}", file=sys.stderr)
        return 2
    return run_event(args)


def witness_problem(args):
    """Why a witness event contradicts itself, or None when its shape is complete."""
    if args.duration_seconds < 0:
        return "duration_seconds is never negative"
    if args.covered_count < 0 or args.uncovered_count < 0:
        return "witness coverage counts are never negative"
    if args.outcome == "checked" and args.reason:
        return "a checked witness carries an empty reason"
    if args.outcome in ("partial", "failed") and not args.reason.strip():
        return f"a {args.outcome} witness carries its reason"
    if args.outcome == "checked" and args.uncovered_count:
        return "a checked witness leaves no pointers uncovered"
    if args.outcome == "partial" and not args.covered_count:
        return "a partial witness has one or more covered pointers"
    if args.outcome == "failed" and args.covered_count:
        return "a failed witness covers no pointers"
    counters = [getattr(args, name) for name in COST_COUNTERS]
    total = getattr(args, COST_TOTAL)
    figures = [value for value in counters + [total] if value is not None]
    if not figures:
        return None
    if len(figures) < len(COST_COUNTERS) + 1:
        return f"witness cost carries all of {', '.join(COST_COUNTERS)} and {COST_TOTAL}, or none"
    if any(value < 0 for value in figures):
        return "a witness cost token count is never negative"
    if total != sum(counters):
        return f"{COST_TOTAL} {total} is not the sum of the four witness cost counters"
    return None


def run_witness(args):
    """Append one witness event; returns 0, or 2 for a contradictory result."""
    problem = witness_problem(args)
    if problem is not None:
        print(f"machine log: witness: {problem}", file=sys.stderr)
        return 2
    return run_event(args)


def run_base_gate(args):
    """Append one base-gate decision, or refuse a status that contradicts its argv."""
    if args.status == "passed" and not args.argv:
        print("machine log: base-gate: a passed gate carries its argv", file=sys.stderr)
        return 2
    if args.status == "not-configured" and args.argv:
        print("machine log: base-gate: an unconfigured gate carries no argv", file=sys.stderr)
        return 2
    return run_event(args)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--log", required=True, help="the run's machine log")
    subcommands = parser.add_subparsers(dest="event", required=True)

    def event_command(name, help_text):
        command = subcommands.add_parser(name, help=help_text)
        command.set_defaults(handler=run_event)
        command.add_argument("--ticket", required=True, help="the ticket number, as written")
        return command

    launch = event_command("launch", "a child started on a ticket")
    launch.add_argument("--child", required=True)
    launch.add_argument("--workflow", required=True)
    launch.add_argument("--executor", required=True)
    launch.add_argument("--model", required=True, help="the full model ID, never an alias")
    launch.add_argument("--effort", required=True)
    launch.add_argument("--branch")
    launch.add_argument("--worktree")
    launch.add_argument("--window")
    launch.add_argument(
        "--account", help="the Claude Code profile directory this child launched under, which"
                          " is what makes a run's spend attributable after the fact; a Claude"
                          " child's alone, a Codex child running on its own vendor's credentials",
    )

    launch_failed = event_command(
        "launch-failed", "a live child failed post-launch verification"
    )
    launch_failed.add_argument("--detail", required=True)

    receipt = event_command("receipt", "a child's final word, as verified by script")
    receipt.add_argument("--verdict", required=True, choices=VERDICTS)
    receipt.add_argument("--sha")
    receipt.add_argument("--detail")

    merge = event_command("merge", "one ticket branch's trip into the integration branch")
    merge.add_argument("--result", required=True, choices=MERGE_RESULTS)
    merge.add_argument("--branch")
    merge.add_argument("--into")
    merge.add_argument("--sha")
    merge.add_argument("--detail")

    outcome = event_command("outcome", "a ticket's one report outcome")
    outcome.add_argument("--outcome", required=True, choices=OUTCOMES)
    outcome.add_argument("--detail")

    queued = event_command(
        "queued", "a finding this run opened a ticket for and queued into itself"
    )
    queued.add_argument(
        "--source", required=True, help="the ticket whose child stated the finding"
    )
    queued.add_argument(
        "--open", required=True, choices=OPEN_WORDS,
        help="what the finding leaves open, which is what the queued child diagnoses first",
    )
    queued.add_argument(
        "--locator", required=True,
        help="the tracker's own opaque locator for the ticket that was opened",
    )
    queued.add_argument(
        "--finding", required=True,
        help="the finding line exactly as the source child stated it, which is what makes a"
             " repeated queue of the same finding find its own line here",
    )

    review = event_command("review", "one end of a ticket's trip through its review lane")
    review.add_argument(
        "--lane", required=True,
        help="the reviewing vendor and its model, as the wave table approved them",
    )
    review.add_argument("--state", required=True, choices=REVIEW_STATES)
    review.add_argument("--detail")

    witness = event_command("witness", "one witness fact-check of an escalation")
    witness.set_defaults(handler=run_witness)
    witness.add_argument("--executor", required=True, choices=EXECUTORS)
    witness.add_argument("--model", required=True, help="the full model ID, never an alias")
    witness.add_argument("--outcome", required=True, choices=WITNESS_OUTCOMES)
    witness.add_argument("--reason", required=True)
    witness.add_argument("--duration-seconds", required=True, type=float)
    witness.add_argument("--covered-count", required=True, type=int)
    witness.add_argument("--uncovered-count", required=True, type=int)
    for tokens in ("input", "output", "cache-read", "cache-creation", "total"):
        witness.add_argument(f"--{tokens}-tokens", type=int, help=f"{tokens} tokens, as counted")

    base_gate = subcommands.add_parser(
        "base-gate", help="whether the integration base passed its configured project gate"
    )
    base_gate.set_defaults(handler=run_base_gate)
    base_gate.add_argument("--status", required=True, choices=BASE_GATE_STATUSES)
    base_gate.add_argument(
        "--argument", action="append", dest="argv",
        help="one argv element, repeated in order; absent when no gate was configured",
    )

    cost = event_command("session-cost", "what one session spent, in tokens")
    cost.set_defaults(handler=run_session_cost)
    cost.add_argument("--executor", required=True, choices=EXECUTORS)
    # The same spelling the `review` event's lane carries, so a review's spend is filterable by
    # the lane that spent it. Left unset by the cost pass, whose rows are implementing children:
    # an absent lane is what says the session was the ticket's own work.
    cost.add_argument(
        "--lane",
        help="the reviewing vendor and its model, when these figures are a review's;"
             " absent means an implementing child",
    )
    cost.add_argument("--model", required=True, help="the full model ID, never an alias")
    cost.add_argument("--session", help="the session whose transcript these figures were read off")
    for tokens in ("input", "output", "cache-read", "cache-creation", "total"):
        cost.add_argument(f"--{tokens}-tokens", type=int, help=f"{tokens} tokens, as counted")
    # Left unset when the transcript could not be read, which is what makes `detail` the diagnosis
    # rather than a note: a line with no figures and no detail would be a silent gap.
    cost.add_argument("--detail", help="why the figures are missing, when they are")

    advance = subcommands.add_parser("advance", help="what the run decided after a wave settled")
    advance.set_defaults(handler=run_event)
    advance.add_argument("--wave", required=True, help="the wave the decision is about")
    advance.add_argument("--decision", required=True, choices=DECISIONS)
    advance.add_argument("--detail")

    live_source = subcommands.add_parser(
        "live-source", help="record which source a lane's live children were read from"
    )
    live_source.set_defaults(handler=run_event)
    live_source.add_argument("--lane", required=True, choices=EXECUTORS)
    live_source.add_argument("--source", required=True, choices=LIVE_SOURCES)
    live_source.add_argument("--reason", required=True, help="why that source and not the first")

    monitor_error = subcommands.add_parser(
        "monitor-error", help="record a monitor that exited with an error"
    )
    monitor_error.set_defaults(handler=run_event)
    monitor_error.add_argument("--monitor", required=True, help="the monitor that failed")
    monitor_error.add_argument("--reason", required=True, help="why the monitor failed")

    message = subcommands.add_parser("message", help="record an outgoing message")
    message.set_defaults(handler=run_message)
    message.add_argument("--role", required=True, choices=(COORDINATOR, CHILD))
    message.add_argument("--ticket")
    message.add_argument("--to")
    message.add_argument("--message", required=True)

    hook = subcommands.add_parser("hook", help="copy an outgoing SendMessage into the log")
    hook.set_defaults(handler=run_hook)
    hook.add_argument("--role", required=True, choices=(COORDINATOR, CHILD))
    hook.add_argument("--ticket", help="the ticket this side of the channel serves, where known")
    hook.add_argument(
        "--scope",
        help="the directory whose sends this hook copies; anything else is another side's",
    )

    guard = subcommands.add_parser(
        "guard", help="deny a stale Coordinator before SendMessage delivery"
    )
    guard.set_defaults(handler=run_guard)
    guard.add_argument(
        "--scope",
        help="the directory whose sends this Coordinator guard authorizes",
    )

    install = subcommands.add_parser("install", help="register that hook in a settings file")
    install.set_defaults(handler=run_install)
    install.add_argument("--settings", required=True, help="the settings file to register in")
    install.add_argument("--role", required=True, choices=(COORDINATOR, CHILD))
    install.add_argument("--ticket", help="the ticket this side of the channel serves, where known")
    install.add_argument(
        "--hook-script",
        help="a copy of this script to register as given (default: the run's own, beside the log)",
    )
    install.add_argument(
        "--scope",
        help="the directory this settings file's sessions run in (default: the checkout above it)",
    )
    install.add_argument(
        "--crew-dir",
        help="the crew skill directory whose judgment references the coordinator may read whole",
    )
    install.add_argument(
        "--run-dir",
        help="the staged run directory whose top-level Markdown the coordinator may read whole",
    )
    install.add_argument(
        "--session-id",
        help="the one coordinator or manual-advisor session the bounded-read hook applies to",
    )

    uninstall = subcommands.add_parser(
        "uninstall", help="remove every entry installed for this log from a settings file"
    )
    uninstall.set_defaults(handler=run_uninstall)
    uninstall.add_argument("--settings", required=True, help="the settings file to remove from")

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
