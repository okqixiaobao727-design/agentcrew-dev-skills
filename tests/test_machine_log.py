#!/usr/bin/env python3
"""Behaviour of the machine log, asserted at its two seams: the CLI and the hook's stdin.

The log is the schema every other crew script and every future auditing agent reads, so what
these tests pin is the file on disk — one JSON object per line, a uniform UTC timestamp on every
one of them, and outgoing messages copied in byte for byte. The shape they assert is the one
`docs/machine-log.md` publishes.
"""

import datetime
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest


PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = PLUGIN_ROOT / "skills" / "crew" / "assets" / "machine_log.py"
BOUNDED_SCRIPT = SCRIPT.with_name("bounded_read.py")
CONTROL_SCRIPT = SCRIPT.with_name("coordinator_control.py")
CREW_DIR = SCRIPT.parent.parent
GLOSSARY = PLUGIN_ROOT / "docs" / "glossary.md"
SHAPES = PLUGIN_ROOT / "skills" / "crew" / "assets" / "dispatch" / "templates" / "shapes.toml"
COORDINATOR_SESSION = "9d1f4c2a-0000-4000-8000-000000000133"
SESSION_ENV = "CLAUDE_CODE_SESSION_ID"
COORDINATOR_SOCKET = "/private/tmp/cc-socks-501/1504.sock"
sys.path.insert(0, str(SCRIPT.parent))
import coordinator_control  # noqa: E402
import machine_log  # noqa: E402
import run_plan  # noqa: E402

# The run's one timestamp format: `date -u +%Y-%m-%dT%H:%M:%SZ`, as the crew skill reads it.
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

# A review lane as the wave table approves it: the reviewing vendor, then its full model ID.
REVIEW_LANE = "codex gpt-5.6-luna"

# A ruling and an escalation as they travel the message channel, in the run's own grammar.
RULING = (
    "Take option B: keep the existing column and add the new one beside it.\n"
    "Reasons: the migration is reversible, and ticket 04 already reads the old name.\n"
    "ts=1755060000"
)
ESCALATION = (
    "CREW ASK 07 doc-conflict — the spec says the log is JSON Lines and the ticket says\n"
    "one line per event; option A treats them as the same claim (mine), option B splits\n"
    "multi-line messages across lines. ts=1755060042"
)
# The same escalation as a child that talks before it asks sends it: a summary first, the verb
# on its own line after it. A final turn is composed freely, so this shape is as ordinary as the
# bare one and the log must classify the two alike.
BUNDLED_ESCALATION = (
    "I read both documents through and they do not agree about the log's shape.\n"
    "\n" + ESCALATION
)
SHA = "b614ec84712aa8c351fe30ec69000e2e12518aeb"


def run_cli(*args, log=None):
    command = [sys.executable, str(SCRIPT)]
    if log is not None:
        command += ["--log", str(log)]
    return subprocess.run([*command, *args], capture_output=True, text=True)


def hook_environment(sender=None):
    """The environment a hook fires in: this one's own, with the sender's inbox socket settled.

    Settled rather than inherited, because these tests run inside a Claude Code session of their
    own and would otherwise record the socket of whichever session happened to run them.
    """
    environment = dict(os.environ)
    environment.pop(machine_log.SENDER_SOCKET_VARIABLE, None)
    if sender is not None:
        environment[machine_log.SENDER_SOCKET_VARIABLE] = sender
    return environment


def run_hook(payload, log=None, role="child", ticket=None, sender=None):
    args = ["hook", "--role", role]
    if ticket is not None:
        args += ["--ticket", ticket]
    command = [sys.executable, str(SCRIPT)]
    if log is not None:
        command += ["--log", str(log)]
    return subprocess.run(
        [*command, *args],
        input=payload if isinstance(payload, str) else json.dumps(payload),
        capture_output=True,
        text=True,
        env=hook_environment(sender),
    )


def seed_coordinator(log, socket=COORDINATOR_SOCKET):
    context = coordinator_control.CoordinatorContext(
        name="crew-coordinator",
        pid=1504,
        harness_session=COORDINATOR_SESSION,
        address=f"uds:{socket}",
        pane="%1",
        permission_mode="acceptEdits",
        display_session="$1:",
    )
    return coordinator_control.CoordinatorControl(pathlib.Path(log).parent).service(
        context, lambda _context: None
    )


def run_guard(payload, log, sender=None):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--log", str(log), "guard"],
        input=payload if isinstance(payload, str) else json.dumps(payload),
        capture_output=True,
        text=True,
        env=hook_environment(sender),
    )


class EscalationGrammarTests(unittest.TestCase):
    """An ask is a verb only when it names one of the protocol's five kinds."""

    def test_the_escalation_grammar_exports_and_accepts_exactly_five_kinds(self):
        expected_kinds = ("design", "scope", "doc-conflict", "stuck", "wrap-up")

        self.assertEqual(machine_log.ESCALATION_KINDS, expected_kinds)
        for kind in expected_kinds:
            line = f"CREW ASK 07 {kind}"
            with self.subTest(kind=kind):
                self.assertEqual(
                    machine_log.final_verb(line),
                    (machine_log.ESCALATION_VERB, line),
                )

        malformed = (
            "CREW ASK 07 progress",
            "CREW ASK 07",
            "CREW ASK 07 scope extra",
        )
        for line in malformed:
            with self.subTest(line=line):
                self.assertEqual(machine_log.final_verb(line), (None, None))
                self.assertEqual(machine_log.malformed_receipt(line), line)

    def test_the_glossary_defines_protocol_and_work_brief(self):
        glossary = GLOSSARY.read_text()

        self.assertIn(
            "**Protocol** — The text that never varies by workflow and is rendered into every "
            "child's first turn",
            " ".join(glossary.split()),
        )
        self.assertIn(
            "**Work brief** — The per-workflow delta in a child's first turn",
            " ".join(glossary.split()),
        )

    def test_the_first_turn_escalation_block_names_the_machine_logs_five_kinds(self):
        with SHAPES.open("rb") as handle:
            escalation = tomllib.load(handle)["turn"]["escalate"]

        rendered_kinds = tuple(re.findall(r"^- ([a-z-]+):", escalation, flags=re.MULTILINE))

        self.assertEqual(rendered_kinds, machine_log.ESCALATION_KINDS)

    def test_an_ask_allows_the_protocol_timestamp_after_its_kind_or_body(self):
        messages = (
            "CREW ASK 07 stuck ts=1755060042",
            "CREW ASK 07 wrap-up — place this leftover ts=1755060042",
        )

        for message in messages:
            with self.subTest(message=message):
                self.assertEqual(
                    machine_log.final_verb(message),
                    (machine_log.ESCALATION_VERB, message),
                )

    def test_an_ask_refuses_a_non_numeric_ticket_and_other_body_separators(self):
        malformed = (
            "CREW ASK nonsense stuck",
            "CREW ASK 07 design: which option?",
            "CREW ASK 07 stuck - the fixture never came up",
        )

        for line in malformed:
            with self.subTest(line=line):
                self.assertEqual(machine_log.final_verb(line), (None, None))
                self.assertEqual(machine_log.malformed_receipt(line), line)


def send_message_event(message, to="crew-coordinator", cwd="/tmp/worktree"):
    """A PostToolUse payload for a SendMessage call that has just been made.

    `cwd` is the directory the sending session runs in — the coordinator's checkout or a child's
    worktree — which is what tells one side of the channel's hooks from the other's.
    """
    return {
        "session_id": "9d1f4c2a-0000-4000-8000-000000000001",
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": cwd,
        "hook_event_name": "PostToolUse",
        "tool_name": "SendMessage",
        "tool_input": {"to": to, "message": message},
        "tool_response": {"status": "delivered"},
    }


def registered_commands(settings):
    """Every SendMessage hook command a settings file registers, in the order it lists them."""
    document = json.loads(pathlib.Path(settings).read_text(encoding="utf-8"))
    return [
        hook["command"]
        for block in document.get("hooks", {}).get("PostToolUse", [])
        if block["matcher"] == "SendMessage"
        for hook in block["hooks"]
    ]


def registered_bounded_commands(settings):
    """Every bounded-read hook command a settings file registers, in listed order."""
    document = json.loads(pathlib.Path(settings).read_text(encoding="utf-8"))
    return [
        hook["command"]
        for block in document.get("hooks", {}).get("PreToolUse", [])
        if block["matcher"] == "Read|Grep|Glob|Bash"
        for hook in block["hooks"]
    ]


class MachineLogTestCase(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.log = pathlib.Path(self.work.name) / "machine.log"
        # An install scopes its hook to the session it ran in, read from the ambient environment.
        # A test runner started inside a Claude Code session carries one, which would silently
        # pin a session these tests never send, so a test that wants a session sets it itself.
        ambient = os.environ.pop(SESSION_ENV, None)
        if ambient is not None:
            self.addCleanup(os.environ.__setitem__, SESSION_ENV, ambient)

    def lines(self):
        """Every event recorded so far, parsed."""
        text = self.log.read_text(encoding="utf-8")
        self.assertTrue(text.endswith("\n"), "every record ends its own line")
        return [json.loads(line) for line in text.splitlines()]

    def only_line(self):
        recorded = self.lines()
        self.assertEqual(len(recorded), 1)
        return recorded[0]

    def assertUniformTimestamp(self, entry):
        """The stamp is the run's one format, in UTC, and reads as the moment it was written."""
        stamp = entry["ts"]
        self.assertRegex(stamp, TIMESTAMP)
        recorded = datetime.datetime.strptime(stamp, TIMESTAMP_FORMAT).replace(
            tzinfo=datetime.timezone.utc
        )
        drift = abs((datetime.datetime.now(datetime.timezone.utc) - recorded).total_seconds())
        self.assertLess(drift, 120, "the stamp is UTC, not local time")


class ReadRecordsTests(MachineLogTestCase):
    """The normal runtime reader keeps usable objects and tolerates incomplete lines."""

    def test_missing_and_mixed_logs_preserve_only_objects_in_physical_order(self):
        self.assertEqual(machine_log.read_records(self.log), ())
        self.log.write_text(
            "\n"
            "not json\n"
            "[]\n"
            '{"event":"launch","ticket":"07","ts":"later"}\n'
            '{"event":"unknown","ticket":"07","ts":"earlier"}\n',
            encoding="utf-8",
        )

        self.assertEqual(
            machine_log.read_records(self.log),
            (
                {"event": "launch", "ticket": "07", "ts": "later"},
                {"event": "unknown", "ticket": "07", "ts": "earlier"},
            ),
        )

    def test_non_missing_io_and_unicode_failures_propagate(self):
        with self.assertRaises(IsADirectoryError):
            machine_log.read_records(pathlib.Path(self.work.name))
        self.log.write_bytes(b"\xff\xfe")
        with self.assertRaises(UnicodeDecodeError):
            machine_log.read_records(self.log)


class RunProjectionTests(unittest.TestCase):
    """The immutable run snapshot derives facts from accepted-record order."""

    def test_launch_selection_uses_physical_order_and_the_snapshot_is_immutable(self):
        projection = machine_log.project([
            {"event": "launch", "ticket": 7, "child": "first", "ts": "later"},
            {"event": "unknown", "ticket": "7", "detail": "kept"},
            {"event": "launch", "ticket": "7", "child": "latest", "ts": "earlier"},
        ])
        facts = projection.ticket(7)

        self.assertEqual(tuple(projection.tickets), ("7",))
        self.assertEqual(facts.ticket, "7")
        self.assertEqual(facts.first_launch["child"], "first")
        self.assertEqual(facts.launch["child"], "latest")
        self.assertEqual([event["event"] for event in facts.events], [
            "launch", "unknown", "launch",
        ])
        with self.assertRaises(TypeError):
            projection.tickets["8"] = facts
        with self.assertRaises(TypeError):
            facts.events[0]["child"] = "changed"
        with self.assertRaises(AttributeError):
            facts.ticket = "changed"

        missing = projection.ticket("8")
        self.assertEqual(missing.ticket, "8")
        self.assertEqual(missing.events, ())
        self.assertIsNone(missing.launch)

    def test_launch_verification_failure_follows_the_latest_launch_epoch(self):
        cases = (
            ("no launch", [], False),
            ("only launch", [{"event": "launch", "ticket": "7"}], False),
            ("failed after launch", [
                {"event": "launch", "ticket": "7"},
                {"event": "launch-failed", "ticket": "7"},
            ], True),
            ("new launch clears failure", [
                {"event": "launch", "ticket": "7"},
                {"event": "launch-failed", "ticket": "7"},
                {"event": "launch", "ticket": "7"},
            ], False),
        )

        for name, records, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(
                    machine_log.project(records).ticket("7").launch_verification_failed,
                    expected,
                )

    def test_latest_landed_merge_uses_physical_order_across_the_run(self):
        projection = machine_log.project([
            {"event": "merge", "ticket": "7", "result": "clean", "sha": "a" * 40},
            {"event": "merge", "ticket": "8", "result": "conflict"},
            {"event": "merge", "ticket": "8", "result": "resolved", "sha": "b" * 40},
            {"event": "merge", "ticket": "9", "result": "escalated"},
            {"event": "merge", "ticket": "10", "result": "repaired", "sha": "c" * 40},
        ])

        self.assertEqual(projection.latest_landed_merge["ticket"], "10")
        self.assertEqual(projection.latest_landed_merge["sha"], "c" * 40)
        self.assertIsNone(machine_log.project([]).latest_landed_merge)

    def test_settlement_facts_keep_event_presence_quality_and_progress_distinct(self):
        projection = machine_log.project([
            {"event": "receipt", "ticket": "7", "verdict": "landable", "ts": "03"},
            {"event": "merge", "ticket": "7", "result": "resolved", "ts": "02"},
            {"event": "outcome", "ticket": "7", "outcome": "future-word", "ts": "01"},
            {"event": "merge", "ticket": "8", "result": "clean", "ts": "later"},
            {"event": "receipt", "ticket": "8", "verdict": "landable", "ts": "earlier"},
            {"event": "receipt", "ticket": "9", "outcome": "failed"},
        ])

        seven = projection.ticket("7")
        self.assertEqual(seven.receipt["verdict"], "landable")
        self.assertEqual(seven.latest_settling_event["outcome"], "future-word")
        self.assertEqual(seven.progress_event["result"], "resolved")
        self.assertEqual(seven.settlement_state, "completed")
        self.assertEqual(seven.merge_result, "resolved")
        self.assertTrue(seven.merge_landed)
        self.assertEqual(projection.ticket("8").settlement_state, "completed")
        nine = projection.ticket("9")
        self.assertEqual(nine.latest_settling_event["outcome"], "failed")
        self.assertEqual(nine.settlement_state, "live")

    def test_message_episodes_correlate_ticketless_rulings_through_the_final_child(self):
        projection = machine_log.project([
            {"event": "launch", "ticket": "7", "child": "old-child"},
            {"event": "message", "ticket": "7", "role": "child", "message": "first"},
            {"event": "receipt", "ticket": "7", "verdict": "failed"},
            {"event": "launch", "ticket": "7", "child": "new-child"},
            {"event": "ruling", "to": "old-child", "message": "CREW NUDGE 7 old"},
            {"event": "ruling", "to": "new-child", "message": "CREW NUDGE 7 new"},
            {"event": "review", "ticket": "7", "state": "running"},
            {"event": "message", "ticket": "7", "role": "child", "message": "latest"},
            {"event": "launch", "ticket": "8", "child": "eight"},
            {"event": "ruling", "ticket": "8", "message": "CREW RULED 8 handed over"},
        ])

        seven = projection.ticket("7")
        self.assertEqual(seven.unanswered_child_message["message"], "latest")
        self.assertTrue(seven.awaiting_receipt)
        self.assertFalse(seven.awaiting_ruling)
        self.assertFalse(seven.outstanding_nudge)
        self.assertEqual(seven.instruction_count("CREW NUDGE"), 1)
        self.assertNotIn("ruling", [record["event"] for record in seven.events])

        eight = projection.ticket("8")
        self.assertTrue(eight.awaiting_ruling)
        self.assertEqual(eight.instruction_count("CREW RULED"), 1)

    def test_a_ruling_addressed_by_socket_is_filed_against_the_ticket_that_asked(self):
        """The coordinator replies to the address the ask arrived from, so that is the index."""
        child = "uds:/private/tmp/cc-socks-501/2277.sock"
        projection = machine_log.project([
            {"event": "launch", "ticket": "7", "child": "seven-1f"},
            {
                "event": "escalation", "ticket": "7", "role": "child", "from": child,
                "to": "uds:/private/tmp/cc-socks-501/1504.sock",
                "message": "CREW ASK 7 design — which column",
            },
            {
                "event": "ruling", "role": "coordinator", "to": child,
                "message": "CREW NUDGE 7 keep the existing column",
            },
        ])

        seven = projection.ticket("7")
        self.assertEqual(seven.instruction_count("CREW NUDGE"), 1)
        self.assertFalse(seven.awaiting_ruling)

    def test_a_ruling_addressed_by_the_older_name_form_is_still_filed(self):
        """A log written before addresses were recorded correlates after a resume, unchanged."""
        projection = machine_log.project([
            {"event": "launch", "ticket": "7", "child": "seven-1f"},
            {
                "event": "escalation", "ticket": "7", "role": "child",
                "message": "CREW ASK 7 design — which column",
            },
            {
                "event": "ruling", "role": "coordinator", "to": "seven-1f",
                "message": "CREW NUDGE 7 keep the existing column",
            },
        ])

        seven = projection.ticket("7")
        self.assertEqual(seven.instruction_count("CREW NUDGE"), 1)
        self.assertFalse(seven.awaiting_ruling)

    def test_a_coordinators_own_address_is_not_read_as_a_childs_identity(self):
        """Only a child's sending address indexes a ticket; the coordinator answers many."""
        coordinator = "uds:/private/tmp/cc-socks-501/1504.sock"
        projection = machine_log.project([
            {"event": "launch", "ticket": "7", "child": "seven-1f"},
            {
                "event": "ruling", "ticket": "7", "role": "coordinator", "from": coordinator,
                "to": "uds:/private/tmp/cc-socks-501/2277.sock",
                "message": "CREW NUDGE 7 first",
            },
            {
                "event": "ruling", "role": "coordinator", "to": coordinator,
                "message": "CREW NUDGE 7 second",
            },
        ])

        self.assertEqual(projection.ticket("7").instruction_count("CREW NUDGE"), 1)

    def test_a_witness_is_attached_only_to_the_escalation_it_checked(self):
        first = {
            "event": "escalation", "ticket": "7", "role": "child",
            "message": "CREW ASK 7 design — first",
        }
        first_witness = {
            "event": "witness", "ticket": "7", "outcome": "checked",
            "reason": "", "duration_seconds": 1,
        }
        ruling = {
            "event": "ruling", "ticket": "7", "role": "coordinator",
            "message": "CREW RULED 7 handed over",
        }
        second = {
            "event": "escalation", "ticket": "7", "role": "child",
            "message": "CREW ASK 7 scope — second",
        }

        awaiting_second_witness = machine_log.project([
            first, first_witness, ruling, second,
        ])
        self.assertEqual(awaiting_second_witness.ticket("7").escalation, second)
        self.assertIsNone(awaiting_second_witness.ticket("7").witness)

        second_witness = {
            "event": "witness", "ticket": "7", "outcome": "failed",
            "reason": "session timed out", "duration_seconds": 2,
        }
        checked_second = machine_log.project([
            first, first_witness, ruling, second, second_witness,
        ])
        self.assertEqual(checked_second.ticket("7").escalation, second)
        self.assertEqual(checked_second.ticket("7").witness, second_witness)

    def test_an_ask_never_displaces_the_fact_check_a_standing_escalation_waits_on(self):
        escalation = {
            "event": "escalation", "ticket": "7", "role": "child",
            "message": "CREW ASK 7 design — check src/check.py:12",
        }
        check = {
            "event": "witness", "ticket": "7", "operation": "check", "outcome": "checked",
            "reason": "", "brief": "src/check.py:12 — held — the guard is present",
            "duration_seconds": 1,
        }
        ask = {
            "event": "witness", "ticket": "7", "operation": "ask", "outcome": "checked",
            "reason": "", "brief": "the ticket is open — #7", "duration_seconds": 2,
        }

        projection = machine_log.project([escalation, check, ask])

        self.assertEqual(projection.ticket("7").witness, check)

    def test_an_operation_this_projection_has_not_heard_of_is_a_fact_check(self):
        # Written as "not `ask`", so an operation added later reaches the escalation's slot
        # without an edit here; an event from before the field existed does too.
        escalation = {
            "event": "escalation", "ticket": "7", "role": "child",
            "message": "CREW ASK 7 design — check src/check.py:12",
        }
        later = {
            "event": "witness", "ticket": "7", "operation": "brief", "outcome": "checked",
            "reason": "", "brief": "src/check.py:12 — held — the guard is present",
            "duration_seconds": 1,
        }

        projection = machine_log.project([escalation, later])

        self.assertEqual(projection.ticket("7").witness, later)

    def test_a_delivered_escalation_awaits_a_ruling_from_the_moment_it_was_sent(self):
        escalation = {
            "event": "escalation", "ticket": "7", "role": "child",
            "from": "uds:/tmp/crew-7.sock",
            "message": "CREW ASK 7 design — choose the projection fact",
        }
        ruling = {
            "event": "ruling", "ticket": "7", "role": "coordinator",
            "message": "Choose option A.",
        }

        self.assertTrue(machine_log.project([escalation]).ticket("7").awaiting_ruling)
        for outcome in ("checked", "partial", "failed"):
            with self.subTest(outcome=outcome):
                # The coordinator runs the fact-check as part of its ruling turn, so the event it
                # writes says nothing about whether the ruling has been made (#194).
                witness = {"event": "witness", "ticket": "7", "outcome": outcome}
                checked = machine_log.project([escalation, witness]).ticket("7")
                self.assertTrue(checked.awaiting_ruling)

                answered = machine_log.project([escalation, witness, ruling]).ticket("7")
                self.assertFalse(answered.awaiting_ruling)

    def test_an_undelivered_escalation_awaits_a_ruling_only_once_handed_over(self):
        escalation = {
            "event": "escalation", "ticket": "7", "role": "child",
            "message": "CREW ASK 7 design — a Codex child, transcribed by the bridge",
        }
        hand_over = {
            "event": "ruling", "ticket": "7", "role": "coordinator",
            "message": (
                "CREW RULED 7 — this escalation was handed to the coordinator, which is where "
                "it is answered."
            ),
        }

        standing = machine_log.project([escalation]).ticket("7")
        self.assertFalse(standing.awaiting_ruling)
        self.assertTrue(machine_log.project([escalation, hand_over]).ticket("7").awaiting_ruling)

    def test_the_hand_over_line_leaves_the_escalation_standing_and_a_ruling_ends_it(self):
        """The line that puts a Codex escalation in front of the coordinator does not answer it.

        It consumes the pending child message, which is what stops the Driver handing the same
        escalation over on every poll. The fact-check the coordinator then runs is owed for
        exactly that escalation, so the escalation has to outlive the line announcing it (#194).
        """
        escalation = {
            "event": "escalation", "ticket": "7", "role": "child",
            "message": "CREW ASK 7 design — a Codex child, transcribed by the bridge",
        }
        hand_over = {
            "event": "ruling", "ticket": "7", "role": "coordinator",
            "message": (
                "CREW RULED 7 — this escalation was handed to the coordinator, which is where "
                "it is answered."
            ),
        }
        witness = {"event": "witness", "ticket": "7", "operation": "check", "outcome": "checked"}
        ruling = {
            "event": "ruling", "ticket": "7", "role": "coordinator", "message": "Choose option A.",
        }

        self.assertEqual(machine_log.project([escalation]).ticket("7").standing_escalation,
                         escalation)
        handed_over = machine_log.project([escalation, hand_over]).ticket("7")
        self.assertEqual(handed_over.standing_escalation, escalation)
        self.assertIsNone(handed_over.unanswered_child_message)

        # And the fact-check written after that line pairs with it, so a second run replays.
        checked = machine_log.project([escalation, hand_over, witness]).ticket("7")
        self.assertEqual(checked.witness, witness)

        answered = machine_log.project([escalation, hand_over, witness, ruling]).ticket("7")
        self.assertIsNone(answered.standing_escalation)
        self.assertEqual(answered.escalation, escalation, "the audit pair is still retained")

    def test_a_settling_event_or_another_word_from_the_child_ends_the_standing_escalation(self):
        escalation = {
            "event": "escalation", "ticket": "7", "role": "child",
            "from": "uds:/tmp/a.sock", "message": "CREW ASK 7 design — which table?",
        }
        for name, closing in {
            "a word from the child": {
                "event": "message", "ticket": "7", "role": "child",
                "message": "CREW PARKED 7 — nothing to do",
            },
            "a receipt": {"event": "receipt", "ticket": "7", "verdict": "landable"},
            "an outcome": {"event": "outcome", "ticket": "7", "verdict": "failed"},
        }.items():
            with self.subTest(case=name):
                facts = machine_log.project([escalation, closing]).ticket("7")

                self.assertIsNone(facts.standing_escalation)

    def test_delivery_is_decided_by_the_sender_address_alone(self):
        def escalation(**extra):
            return {
                "event": "escalation", "ticket": "7", "role": "child",
                "message": "CREW ASK 7 design — anything", **extra,
            }

        self.assertTrue(machine_log.delivered(escalation(**{"from": "uds:/tmp/a.sock"})))
        self.assertFalse(machine_log.delivered(escalation()))
        self.assertFalse(machine_log.delivered(escalation(**{"from": None})))
        self.assertFalse(machine_log.delivered(escalation(**{"from": "   "})))
        self.assertFalse(machine_log.delivered(escalation(**{"from": 7})))
        self.assertFalse(machine_log.delivered(None))

    def test_the_newest_escalation_governs_the_ruling_episode(self):
        first = {
            "event": "escalation", "ticket": "7", "role": "child",
            "message": "CREW ASK 7 design — first",
        }
        hand_over = {
            "event": "ruling", "ticket": "7", "role": "coordinator",
            "message": "CREW RULED 7 — this escalation was handed to the coordinator",
        }
        coordinator_ruling = {
            "event": "ruling", "ticket": "7", "role": "coordinator",
            "message": "Choose option A.",
        }
        second = {
            "event": "escalation", "ticket": "7", "role": "child",
            "message": "CREW ASK 7 scope — second",
        }

        ruled = machine_log.project([first, hand_over, coordinator_ruling]).ticket("7")
        self.assertFalse(ruled.awaiting_ruling)

        newest = machine_log.project([
            first, hand_over, coordinator_ruling, second,
        ]).ticket("7")
        self.assertEqual(newest.escalation, second)
        # Undelivered, like the first: this one waits for its own hand-over line.
        self.assertFalse(newest.awaiting_ruling)
        self.assertTrue(machine_log.project([
            first, hand_over, coordinator_ruling, second, hand_over,
        ]).ticket("7").awaiting_ruling)

    def test_only_receipt_evidence_consumes_a_valid_completion_claim(self):
        claim = {
            "event": "message", "ticket": "7", "role": "child",
            "message": f"CREW COMPLETE {'a' * 40}",
        }
        still_pending = machine_log.project([
            claim,
            {"event": "ruling", "ticket": "7", "message": "continue"},
            {"event": "outcome", "ticket": "7", "outcome": "blocked"},
        ])
        self.assertEqual(still_pending.ticket("7").unanswered_child_message, claim)

        unrelated = machine_log.project([
            claim,
            {"event": "receipt", "ticket": "7", "verdict": "failed"},
            {"event": "receipt", "ticket": "7", "verdict": "landable", "sha": "b" * 40},
            {"event": "ruling", "ticket": "7",
             "message": f"CREW RECHECK 7 {'b' * 40} retry"},
        ])
        self.assertEqual(unrelated.ticket("7").unanswered_child_message, claim)

        for evidence in (
            {"event": "receipt", "ticket": "7", "verdict": "landable", "sha": "A" * 40},
            {"event": "ruling", "ticket": "7",
             "message": f"CREW RECHECK 7 {'A' * 40} retry"},
        ):
            with self.subTest(evidence=evidence["event"]):
                consumed = machine_log.project([claim, evidence])
                self.assertIsNone(consumed.ticket("7").unanswered_child_message)

    def test_semantic_conflict_and_rework_preserve_the_existing_order_rules(self):
        projection = machine_log.project([
            {"event": "merge", "ticket": "7", "result": "conflict",
             "detail": "semantic: both tickets own the same name"},
            {"event": "merge", "ticket": "7", "result": "escalated"},
            {"event": "ruling", "ticket": "7", "message": "CREW MERGE 7 resolve it"},
            {"event": "merge", "ticket": "8", "result": "conflict",
             "detail": "semantic: old detail is retained"},
            {"event": "ruling", "ticket": "8", "message": "CREW MERGE 8 first attempt"},
            {"event": "merge", "ticket": "8", "result": "escalated"},
        ])

        seven = projection.ticket("7")
        self.assertEqual(seven.semantic_conflict_detail, "both tickets own the same name")
        self.assertTrue(seven.merge_rework_requested)

        eight = projection.ticket("8")
        self.assertEqual(eight.semantic_conflict_detail, "old detail is retained")
        self.assertFalse(eight.merge_rework_requested)

    def test_run_facts_distinguish_ever_ended_from_the_latest_halt(self):
        projection = machine_log.project([
            {"event": "advance", "decision": "launched", "wave": "2"},
            {"event": "advance", "decision": "launched", "wave": "not-a-wave"},
            {"event": "advance", "decision": "complete", "wave": 2},
            {"event": "advance", "decision": "interrupted", "wave": 2},
        ])

        self.assertEqual(projection.current_wave, 2)
        self.assertTrue(projection.ended)
        self.assertTrue(projection.halted)
        resumed = machine_log.project([
            {"event": "advance", "decision": "escalated", "wave": 1},
            {"event": "advance", "decision": "launched", "wave": 2},
        ])
        self.assertFalse(resumed.ended)
        self.assertFalse(resumed.halted)

    def test_a_wave_launched_after_a_final_decision_reopens_the_run_until_it_ends_again(self):
        """A Wave queued into a Run after it ended is a Run that has not ended after all."""
        complete = {"event": "advance", "decision": "complete", "wave": 1}
        launched = {"event": "advance", "decision": "launched", "wave": 2}

        self.assertTrue(machine_log.project([complete]).ended)
        reopened = machine_log.project([complete, launched])
        self.assertFalse(reopened.ended)
        self.assertEqual(reopened.current_wave, 2)
        self.assertTrue(machine_log.project([
            complete,
            launched,
            {"event": "advance", "decision": "complete", "wave": 2},
        ]).ended)

    def test_a_late_actionable_message_reopens_a_stopped_run_until_it_ends_again(self):
        stopped = {"event": "advance", "decision": "stopped", "wave": 1}
        completion = {
            "event": "message", "ticket": "7", "role": "child",
            "message": f"CREW COMPLETE {'a' * 40}",
        }

        self.assertTrue(machine_log.project([stopped]).ended)
        self.assertFalse(machine_log.project([stopped, completion]).ended)
        self.assertTrue(machine_log.project([
            stopped,
            completion,
            {"event": "advance", "decision": "complete", "wave": 1},
        ]).ended)

    def test_a_late_actionable_message_reopens_a_run_completed_with_a_parked_leaf(self):
        parked = {
            "event": "message", "ticket": "7", "role": "child",
            "message": "CREW PARKED /tmp/parked-7.md",
        }
        completion = {
            "event": "message", "ticket": "7", "role": "child",
            "message": f"CREW COMPLETE {'b' * 40}",
        }
        projection = machine_log.project([
            {"event": "launch", "ticket": "7", "child": "seven"},
            parked,
            {"event": "receipt", "ticket": "7", "verdict": "parked"},
            {"event": "advance", "decision": "complete", "wave": 1},
            completion,
        ])

        self.assertFalse(projection.ended)
        self.assertEqual(projection.ticket("7").unanswered_child_message, completion)

    def test_a_late_non_protocol_message_does_not_reopen_a_finished_run(self):
        projection = machine_log.project([
            {"event": "advance", "decision": "complete", "wave": 1},
            {
                "event": "message", "ticket": "7", "role": "child",
                "message": "I am still thinking about the ticket",
            },
        ])

        self.assertTrue(projection.ended)


class SettlementStateTests(unittest.TestCase):
    """The one public predicate every settlement-quality reader shares."""

    def test_a_ticket_with_no_settling_event_is_live(self):
        records = [{"event": "launch", "ticket": "104"}]

        self.assertEqual(machine_log.settlement_state(records, "104"), "live")

    def test_the_latest_receipt_maps_through_the_merge_result(self):
        cases = (
            ("landable without a merge", [{"event": "receipt", "ticket": "104",
                                            "verdict": "landable"}], "landable"),
            ("landable merged clean", [
                {"event": "receipt", "ticket": "104", "verdict": "landable"},
                {"event": "merge", "ticket": "104", "result": "clean"},
            ], "completed"),
            ("landable merged after repair", [
                {"event": "receipt", "ticket": "104", "verdict": "landable"},
                {"event": "merge", "ticket": "104", "result": "repaired"},
            ], "completed"),
            ("failed", [{"event": "receipt", "ticket": "104",
                          "verdict": "failed"}], "failed"),
            ("parked", [{"event": "receipt", "ticket": "104",
                          "verdict": "parked"}], "parked"),
        )
        for label, records, expected in cases:
            with self.subTest(label):
                self.assertEqual(machine_log.settlement_state(records, "104"), expected)

    def test_the_latest_outcome_wins_even_without_or_after_a_receipt(self):
        self.assertEqual(
            machine_log.settlement_state(
                [{"event": "outcome", "ticket": "104", "outcome": "completed"}], "104"
            ),
            "completed",
        )
        for outcome in ("completed", "failed", "parked", "blocked"):
            records = [
                {"event": "outcome", "ticket": "104", "outcome": "failed"},
                {"event": "outcome", "ticket": "104", "outcome": outcome},
                {"event": "receipt", "ticket": "104", "verdict": "landable"},
                {"event": "merge", "ticket": "104", "result": "clean"},
            ]
            with self.subTest(outcome):
                self.assertEqual(machine_log.settlement_state(records, "104"), outcome)

    def test_a_launch_after_blocked_starts_a_current_settlement_epoch(self):
        records = [
            {"event": "outcome", "ticket": "104", "outcome": "blocked"},
            {"event": "launch", "ticket": "104", "child": "child-104"},
        ]

        facts = machine_log.project(records).ticket("104")

        self.assertEqual(facts.latest_settling_event["outcome"], "blocked")
        self.assertIsNone(facts.progress_event)
        self.assertEqual(facts.settlement_state, "live")

        records += [
            {"event": "receipt", "ticket": "104", "verdict": "landable"},
            {"event": "merge", "ticket": "104", "result": "clean"},
        ]
        self.assertEqual(machine_log.project(records).ticket("104").settlement_state, "completed")


class MalformedReceiptTests(unittest.TestCase):
    """A near-miss is told from a silence, so a refusal can be answered rather than dropped.

    The incident (#105): a child appended prose to its receipt line, the grammar refused the line,
    and nothing anywhere said so — the run read a finished ticket as `waiting` for eight minutes.
    The grammar is unchanged; what these pin is that the refusal is now legible.
    """

    # The message that stalled run `crewtask/64`, as its child sent it.
    INCIDENT = (
        f"CREW COMPLETE {SHA} — deferred gap carried forward: the parked checklist is unwritten"
        " ts=1755594000"
    )

    def test_the_incident_s_receipt_speaks_no_verb_and_is_a_near_miss(self):
        self.assertEqual(machine_log.final_verb(self.INCIDENT), (None, None))
        self.assertEqual(machine_log.malformed_receipt(self.INCIDENT), self.INCIDENT)

    def test_a_message_that_reaches_for_no_verb_at_all_is_silence_not_a_near_miss(self):
        for message in (
            "The tests are green and the review is under way.",
            "",
            None,
            {"note": "a structured message carries no lines"},
        ):
            with self.subTest(message=message):
                self.assertIsNone(machine_log.malformed_receipt(message))

    def test_a_message_carrying_a_valid_verb_line_is_never_a_near_miss(self):
        for message in (
            f"CREW COMPLETE {SHA}",
            f"CREW COMPLETE {SHA} ts=1755594000",
            f"The work is committed and reviewed clean.\nCREW COMPLETE {SHA} ts=1755594000",
            "CREW PARKED features/demo/checklist.md ts=1755594000",
            "CREW FAILED the fixture never came up ts=1755594000",
            "CREW ASK 07 stuck — which table? ts=1755594000",
        ):
            with self.subTest(message=message):
                self.assertIsNone(machine_log.malformed_receipt(message))

    def test_a_valid_verb_line_covers_a_botched_one_beside_it(self):
        """The message settled on a verb it spoke properly; there is nothing to bounce."""
        message = (
            f"CREW COMPLETE {SHA[:8]} — first attempt, wrong length\n"
            f"CREW COMPLETE {SHA} ts=1755594000"
        )

        self.assertIsNone(machine_log.malformed_receipt(message))

    def test_prose_naming_a_verb_mid_line_is_neither_verb_nor_near_miss(self):
        message = "My first turn says to send CREW COMPLETE <sha> when the work is committed."

        self.assertEqual(machine_log.final_verb(message), (None, None))
        self.assertIsNone(machine_log.malformed_receipt(message))

    def test_an_indented_line_is_quoting_rather_than_reaching_for_a_verb(self):
        """The same exemption the grammar grants a quoted example, granted to a botched one."""
        message = (
            "My first turn tells me to end on this line:\n"
            "\n"
            f"    CREW COMPLETE {SHA[:8]}\n"
            "\n"
            "I have not finished, so I have not sent it."
        )

        self.assertIsNone(machine_log.malformed_receipt(message))

    def test_every_verb_is_recognised_when_its_own_shape_is_missed(self):
        cases = (
            f"CREW COMPLETE {SHA[:8]}",
            "CREW COMPLETE",
            f"CREW COMPLETE {SHA} — the review is still out",
            "CREW PARKED",
            "CREW FAILED",
            "CREW ASK",
        )
        for line in cases:
            with self.subTest(line=line):
                self.assertEqual(machine_log.final_verb(line), (None, None))
                self.assertEqual(machine_log.malformed_receipt(line), line)

    def test_the_last_near_miss_is_the_one_reported(self):
        """A final turn speaks once, so the line it ended on is the line to quote back."""
        message = (
            "CREW COMPLETE deadbeef\n"
            "That was the short sha; the full one is\n"
            f"CREW COMPLETE {SHA} — and the review is clean"
        )

        self.assertEqual(
            machine_log.malformed_receipt(message),
            f"CREW COMPLETE {SHA} — and the review is clean",
        )

    def test_trailing_padding_is_not_what_makes_a_line_a_near_miss(self):
        """A stray space cost a receipt nowhere else, and it does not manufacture one here."""
        self.assertIsNone(machine_log.malformed_receipt(f"CREW COMPLETE {SHA} \r"))
        self.assertEqual(
            machine_log.malformed_receipt("CREW COMPLETE  \r"), "CREW COMPLETE"
        )


class EventTests(MachineLogTestCase):
    """The four events a script appends."""

    def test_a_launch_records_the_child_and_its_routing(self):
        result = run_cli(
            "launch",
            "--ticket", "07",
            "--child", "agentcrew-machine-log",
            "--workflow", "tdd",
            "--executor", "claude",
            "--model", "claude-opus-4-6-20260401",
            "--effort", "medium",
            "--branch", "worktree-07-machine-log",
            "--worktree", "/repo/.claude/worktrees/07-machine-log",
            "--window", "crew:07",
            log=self.log,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.only_line()
        self.assertUniformTimestamp(entry)
        self.assertEqual(entry["event"], "launch")
        self.assertEqual(entry["ticket"], "07")
        self.assertEqual(entry["child"], "agentcrew-machine-log")
        self.assertEqual(entry["workflow"], "tdd")
        self.assertEqual(entry["executor"], "claude")
        self.assertEqual(entry["model"], "claude-opus-4-6-20260401")
        self.assertEqual(entry["effort"], "medium")
        self.assertEqual(entry["branch"], "worktree-07-machine-log")
        self.assertEqual(entry["worktree"], "/repo/.claude/worktrees/07-machine-log")
        self.assertEqual(entry["window"], "crew:07")

    def test_a_launch_failure_records_why_verification_failed(self):
        result = run_cli(
            "launch-failed",
            "--ticket", "07",
            "--detail", "no entry for this child in the live agents list",
            log=self.log,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.only_line()
        self.assertUniformTimestamp(entry)
        self.assertEqual(entry["event"], "launch-failed")
        self.assertEqual(entry["ticket"], "07")
        self.assertEqual(entry["detail"], "no entry for this child in the live agents list")

    def test_a_receipt_records_the_verified_verdict(self):
        result = run_cli(
            "receipt",
            "--ticket", "07",
            "--verdict", "landable",
            "--sha", "b614ec84712aa8c351fe30ec69000e2e12518aeb",
            log=self.log,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.only_line()
        self.assertUniformTimestamp(entry)
        self.assertEqual(entry["event"], "receipt")
        self.assertEqual(entry["ticket"], "07")
        self.assertEqual(entry["verdict"], "landable")
        self.assertEqual(entry["sha"], "b614ec84712aa8c351fe30ec69000e2e12518aeb")

    def test_a_merge_records_which_stop_of_the_ladder_settled_it(self):
        result = run_cli(
            "merge",
            "--ticket", "09",
            "--result", "repaired",
            "--branch", "worktree-09-merge-driver",
            "--into", "crew/crew-v2",
            "--detail", "repair session resolved 2 hunks",
            log=self.log,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.only_line()
        self.assertUniformTimestamp(entry)
        self.assertEqual(entry["event"], "merge")
        self.assertEqual(entry["ticket"], "09")
        self.assertEqual(entry["result"], "repaired")
        self.assertEqual(entry["branch"], "worktree-09-merge-driver")
        self.assertEqual(entry["into"], "crew/crew-v2")
        self.assertEqual(entry["detail"], "repair session resolved 2 hunks")

    def test_an_outcome_records_one_of_the_four_report_outcomes(self):
        result = run_cli(
            "outcome", "--ticket", "11", "--outcome", "blocked",
            "--detail", "blocked by 07", log=self.log,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.only_line()
        self.assertUniformTimestamp(entry)
        self.assertEqual(entry["event"], "outcome")
        self.assertEqual(entry["ticket"], "11")
        self.assertEqual(entry["outcome"], "blocked")
        self.assertEqual(entry["detail"], "blocked by 07")

    def test_the_open_words_this_log_accepts_are_the_run_plans_own(self):
        self.assertEqual(machine_log.OPEN_WORDS, run_plan.OPEN_WORDS)

    def test_a_queued_event_records_the_ticket_a_finding_opened_and_what_it_leaves_open(self):
        result = run_cli(
            "queued", "--ticket", "42", "--source", "07", "--open", "cause",
            "--locator", "https://github.example.invalid/issues/42",
            "--finding", "The cause is upstream — skills/example.py:12", log=self.log,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.only_line()
        self.assertUniformTimestamp(entry)
        self.assertEqual(entry["event"], "queued")
        self.assertEqual(entry["finding"], "The cause is upstream — skills/example.py:12")
        self.assertEqual(entry["ticket"], "42")
        self.assertEqual(entry["source"], "07")
        self.assertEqual(entry["open"], "cause")
        self.assertEqual(entry["locator"], "https://github.example.invalid/issues/42")

    def test_a_queued_event_takes_only_the_three_words_a_finding_can_leave_open(self):
        result = run_cli(
            "queued", "--ticket", "42", "--source", "07", "--open", "scope",
            "--locator", "https://github.example.invalid/issues/42",
            "--finding", "The cause is upstream — skills/example.py:12", log=self.log,
        )

        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertFalse(self.log.exists(), self.log.read_text() if self.log.exists() else "")
        for word in ("cause", "approach", "reach"):
            self.assertIn(word, result.stderr)

    def test_a_review_records_the_lane_it_ran_in_and_which_end_of_it_this_is(self):
        result = run_cli(
            "review", "--ticket", "26", "--lane", REVIEW_LANE, "--state", "running",
            log=self.log,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.only_line()
        self.assertUniformTimestamp(entry)
        self.assertEqual(entry["event"], "review")
        self.assertEqual(entry["ticket"], "26")
        self.assertEqual(entry["lane"], REVIEW_LANE)
        self.assertEqual(entry["state"], "running")

    def test_a_review_that_came_back_is_the_pair_of_the_one_that_started(self):
        run_cli("review", "--ticket", "26", "--lane", REVIEW_LANE, "--state", "running",
                log=self.log)

        result = run_cli(
            "review", "--ticket", "26", "--lane", REVIEW_LANE, "--state", "returned",
            "--detail", "round one", log=self.log,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        recorded = self.lines()
        self.assertEqual([entry["state"] for entry in recorded], ["running", "returned"])
        self.assertEqual(recorded[1]["ticket"], "26")
        self.assertEqual(recorded[1]["lane"], REVIEW_LANE)
        self.assertEqual(recorded[1]["detail"], "round one")

    def test_a_witness_records_the_fact_check_and_its_session_cost(self):
        result = run_cli(
            "witness", "--ticket", "07", "--operation", "check", "--executor", "claude",
            "--model", "claude-sonnet-5",
            "--outcome", "failed", "--reason", "witness session timed out",
            "--brief", "", "--duration-seconds", "900.125",
            "--covered-count", "0", "--uncovered-count", "3",
            "--input-tokens", "11", "--output-tokens", "22",
            "--cache-read-tokens", "33", "--cache-creation-tokens", "44",
            "--total-tokens", "110", log=self.log,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.only_line()
        self.assertUniformTimestamp(entry)
        self.assertEqual(entry["event"], "witness")
        self.assertEqual(entry["ticket"], "07")
        self.assertEqual(entry["executor"], "claude")
        self.assertEqual(entry["model"], "claude-sonnet-5")
        self.assertEqual(entry["outcome"], "failed")
        self.assertEqual(entry["reason"], "witness session timed out")
        self.assertEqual(entry["operation"], "check")
        self.assertEqual(entry["brief"], "")
        self.assertEqual(entry["duration_seconds"], 900.125)
        self.assertEqual(entry["covered_count"], 0)
        self.assertEqual(entry["uncovered_count"], 3)
        self.assertEqual(entry["input_tokens"], 11)
        self.assertEqual(entry["output_tokens"], 22)
        self.assertEqual(entry["cache_read_tokens"], 33)
        self.assertEqual(entry["cache_creation_tokens"], 44)
        self.assertEqual(entry["total_tokens"], 110)

    def test_a_partial_witness_records_its_required_coverage_counts(self):
        result = run_cli(
            "witness", "--ticket", "07", "--operation", "check", "--executor", "claude",
            "--model", "claude-sonnet-5",
            "--outcome", "partial", "--reason", "uncovered pointers: c.py:11, c.py:12",
            "--brief", "c.py:10 — held — the cited guard is present",
            "--duration-seconds", "12.5",
            "--covered-count", "10", "--uncovered-count", "2", log=self.log,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.only_line()
        self.assertEqual(entry["outcome"], "partial")
        self.assertEqual(entry["covered_count"], 10)
        self.assertEqual(entry["uncovered_count"], 2)

    def test_a_structural_partial_can_cover_every_expected_pointer(self):
        result = run_cli(
            "witness", "--ticket", "07", "--operation", "check", "--executor", "claude",
            "--model", "claude-sonnet-5",
            "--outcome", "partial",
            "--reason", "structural rejection (extra cited): docs/context.md:7",
            "--brief", "c.py:10 — held — the cited guard is present",
            "--duration-seconds", "12.5",
            "--covered-count", "3", "--uncovered-count", "0", log=self.log,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.only_line()
        self.assertEqual(entry["outcome"], "partial")
        self.assertEqual(entry["covered_count"], 3)
        self.assertEqual(entry["uncovered_count"], 0)

    def test_a_pointer_free_checked_witness_records_zero_coverage(self):
        result = run_cli(
            "witness", "--ticket", "07", "--operation", "check", "--executor", "claude",
            "--model", "claude-sonnet-5",
            "--outcome", "checked", "--reason", "",
            "--brief", "the escalation cited no pointer",
            "--duration-seconds", "1",
            "--covered-count", "0", "--uncovered-count", "0", log=self.log,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.only_line()
        self.assertEqual(entry["outcome"], "checked")
        self.assertEqual(entry["covered_count"], 0)
        self.assertEqual(entry["uncovered_count"], 0)

    def test_an_ask_records_the_answer_it_found_with_no_pointers_to_cover(self):
        result = run_cli(
            "witness", "--ticket", "07", "--operation", "ask", "--executor", "claude",
            "--model", "claude-sonnet-5",
            "--outcome", "checked", "--reason", "",
            "--brief", "Issue 154 requires the tracker body — #154",
            "--duration-seconds", "1",
            "--covered-count", "0", "--uncovered-count", "0", log=self.log,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.only_line()
        self.assertEqual(entry["event"], "witness")
        self.assertEqual(entry["operation"], "ask")
        self.assertEqual(entry["brief"], "Issue 154 requires the tracker body — #154")
        self.assertEqual(entry["covered_count"], 0)
        self.assertEqual(entry["uncovered_count"], 0)

    def test_a_witness_operation_outside_the_closed_grammar_is_refused(self):
        result = run_cli(
            "witness", "--ticket", "07", "--operation", "guess", "--executor", "claude",
            "--model", "claude-sonnet-5",
            "--outcome", "checked", "--reason", "", "--brief", "a fact — #154",
            "--duration-seconds", "1",
            "--covered-count", "1", "--uncovered-count", "0", log=self.log,
        )

        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.log.exists())

    def test_a_passing_base_gate_records_its_argv_without_a_ticket(self):
        result = run_cli(
            "base-gate", "--status", "passed",
            "--argument=python3", "--argument=scripts/test.py", "--argument=--full",
            log=self.log,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.only_line()
        self.assertUniformTimestamp(entry)
        self.assertEqual(entry["event"], "base-gate")
        self.assertEqual(entry["status"], "passed")
        self.assertEqual(entry["argv"], ["python3", "scripts/test.py", "--full"])
        self.assertNotIn("ticket", entry)

    def test_an_unconfigured_base_gate_records_no_argv(self):
        result = run_cli("base-gate", "--status", "not-configured", log=self.log)

        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.only_line()
        self.assertEqual(entry["event"], "base-gate")
        self.assertEqual(entry["status"], "not-configured")
        self.assertNotIn("argv", entry)

    def test_a_base_gate_refuses_a_status_that_contradicts_its_argv(self):
        cases = (
            ("passed without argv", ["--status", "passed"]),
            (
                "not configured with argv",
                ["--status", "not-configured", "--argument=python3"],
            ),
        )
        for label, fields in cases:
            with self.subTest(label=label):
                result = run_cli("base-gate", *fields, log=self.log)

                self.assertEqual(result.returncode, 2)
                self.assertFalse(self.log.exists())

    def test_a_witness_outcome_outside_the_closed_grammar_is_refused(self):
        result = run_cli(
            "witness", "--ticket", "07", "--operation", "check", "--executor", "claude",
            "--model", "claude-sonnet-5",
            "--outcome", "unknown", "--reason", "why", "--brief", "", "--duration-seconds", "1",
            "--covered-count", "0", "--uncovered-count", "0",
            log=self.log,
        )

        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.log.exists())

    def test_a_witness_without_an_executor_is_refused_and_appends_nothing(self):
        result = run_cli(
            "witness", "--ticket", "07", "--operation", "check", "--model", "claude-sonnet-5",
            "--outcome", "checked", "--reason", "", "--brief", "a fact — #154",
            "--duration-seconds", "1",
            "--covered-count", "1", "--uncovered-count", "0",
            log=self.log,
        )

        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.log.exists())

    def test_a_witness_without_both_coverage_counts_is_refused(self):
        result = run_cli(
            "witness", "--ticket", "07", "--operation", "check", "--executor", "claude",
            "--model", "claude-sonnet-5",
            "--outcome", "checked", "--reason", "", "--brief", "a fact — #154",
            "--duration-seconds", "1",
            "--covered-count", "1", log=self.log,
        )

        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.log.exists())

    def test_a_witness_refuses_a_contradictory_result_or_cost(self):
        cases = (
            ("checked with a failure reason", [
                "--outcome", "checked", "--reason", "failed", "--brief", "a fact — #154",
                "--duration-seconds", "1",
                "--covered-count", "1", "--uncovered-count", "0",
            ]),
            ("failed without a reason", [
                "--outcome", "failed", "--reason", "", "--brief", "",
                "--duration-seconds", "1",
                "--covered-count", "0", "--uncovered-count", "1",
            ]),
            ("partial without a reason", [
                "--outcome", "partial", "--reason", "", "--brief", "a fact — #154",
                "--duration-seconds", "1",
                "--covered-count", "10", "--uncovered-count", "2",
            ]),
            ("partial without covered pointers", [
                "--outcome", "partial", "--reason", "uncovered", "--brief", "a fact — #154",
                "--duration-seconds", "1",
                "--covered-count", "0", "--uncovered-count", "2",
            ]),
            ("failed with a covered pointer", [
                "--outcome", "failed", "--reason", "failed", "--brief", "",
                "--duration-seconds", "1",
                "--covered-count", "1", "--uncovered-count", "0",
            ]),
            ("checked without the brief it found", [
                "--outcome", "checked", "--reason", "", "--brief", "   ",
                "--duration-seconds", "1",
                "--covered-count", "1", "--uncovered-count", "0",
            ]),
            ("failed carrying a brief", [
                "--outcome", "failed", "--reason", "timed out", "--brief", "a fact — #154",
                "--duration-seconds", "1",
                "--covered-count", "0", "--uncovered-count", "1",
            ]),
            ("negative duration", [
                "--outcome", "checked", "--reason", "", "--brief", "a fact — #154",
                "--duration-seconds", "-1",
                "--covered-count", "1", "--uncovered-count", "0",
            ]),
            ("negative coverage", [
                "--outcome", "checked", "--reason", "", "--brief", "a fact — #154",
                "--duration-seconds", "1",
                "--covered-count", "-1", "--uncovered-count", "0",
            ]),
            ("partial cost", [
                "--outcome", "checked", "--reason", "", "--brief", "a fact — #154",
                "--duration-seconds", "1",
                "--covered-count", "1", "--uncovered-count", "0",
                "--input-tokens", "11",
            ]),
            ("wrong total", [
                "--outcome", "checked", "--reason", "", "--brief", "a fact — #154",
                "--duration-seconds", "1",
                "--covered-count", "1", "--uncovered-count", "0",
                "--input-tokens", "11", "--output-tokens", "22",
                "--cache-read-tokens", "33", "--cache-creation-tokens", "44",
                "--total-tokens", "111",
            ]),
        )
        for label, fields in cases:
            with self.subTest(label=label):
                result = run_cli(
                    "witness", "--ticket", "07", "--operation", "check",
                    "--executor", "claude",
                    "--model", "claude-sonnet-5",
                    *fields, log=self.log,
                )

                self.assertEqual(result.returncode, 2)
                self.assertFalse(self.log.exists())

    def test_a_review_state_outside_the_two_is_refused_and_appends_nothing(self):
        result = run_cli(
            "review", "--ticket", "26", "--lane", REVIEW_LANE, "--state", "finished",
            log=self.log,
        )

        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.log.exists())

    def test_an_advance_records_what_the_run_decided_about_a_wave(self):
        result = run_cli(
            "advance", "--wave", "2", "--decision", "launched",
            "--detail", "advanced from wave 1: 08, 09", log=self.log,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.only_line()
        self.assertUniformTimestamp(entry)
        self.assertEqual(entry["event"], "advance")
        self.assertEqual(entry["wave"], "2")
        self.assertEqual(entry["decision"], "launched")
        self.assertEqual(entry["detail"], "advanced from wave 1: 08, 09")
        # A decision is about a wave, so it carries no ticket at all.
        self.assertNotIn("ticket", entry)

    def test_a_live_source_records_which_source_a_lane_was_read_from_and_why(self):
        result = run_cli(
            "live-source", "--lane", "claude", "--source", "command",
            "--reason", "the sessions directory /home/x/.claude/sessions could not be read",
            log=self.log,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.only_line()
        self.assertUniformTimestamp(entry)
        self.assertEqual(entry["event"], "live-source")
        self.assertEqual(entry["lane"], "claude")
        self.assertEqual(entry["source"], "command")
        self.assertIn("sessions", entry["reason"])

    def test_every_live_source_is_accepted_and_an_unknown_one_is_refused(self):
        for source in ("sessions", "command", "bridge"):
            self.assertEqual(
                run_cli(
                    "live-source", "--lane", "claude", "--source", source,
                    "--reason", "why", log=self.log,
                ).returncode,
                0,
            )

        refused = run_cli(
            "live-source", "--lane", "claude", "--source", "guesswork",
            "--reason", "why", log=self.log,
        )

        self.assertEqual(refused.returncode, 2)
        self.assertEqual(len(self.lines()), 3)

    def test_a_monitor_error_records_the_monitor_and_its_reason(self):
        result = run_cli(
            "monitor-error", "--monitor", "monitor-wave.sh",
            "--reason", "claude agents --json failed", log=self.log,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.only_line()
        self.assertUniformTimestamp(entry)
        self.assertEqual(entry["event"], "monitor-error")
        self.assertEqual(entry["monitor"], "monitor-wave.sh")
        self.assertEqual(entry["reason"], "claude agents --json failed")

    def test_a_session_cost_records_the_usage_one_child_spent(self):
        result = run_cli(
            "session-cost",
            "--ticket", "07",
            "--executor", "claude",
            "--model", "claude-opus-4-6-20260401",
            "--session", "9d1f4c2a-0000-4000-8000-000000000001",
            "--input-tokens", "1200",
            "--output-tokens", "3400",
            "--cache-read-tokens", "560000",
            "--cache-creation-tokens", "78000",
            "--total-tokens", "642600",
            log=self.log,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.only_line()
        self.assertUniformTimestamp(entry)
        self.assertEqual(entry["event"], "session-cost")
        self.assertEqual(entry["ticket"], "07")
        self.assertEqual(entry["executor"], "claude")
        self.assertEqual(entry["model"], "claude-opus-4-6-20260401")
        self.assertEqual(entry["session"], "9d1f4c2a-0000-4000-8000-000000000001")
        self.assertEqual(entry["input_tokens"], 1200)
        self.assertEqual(entry["output_tokens"], 3400)
        self.assertEqual(entry["cache_read_tokens"], 560000)
        self.assertEqual(entry["cache_creation_tokens"], 78000)
        self.assertEqual(entry["total_tokens"], 642600)

    def test_a_session_cost_that_could_not_be_read_carries_the_diagnosis_alone(self):
        result = run_cli(
            "session-cost", "--ticket", "07", "--executor", "codex",
            "--model", "gpt-5.6-luna",
            "--detail", "no rollout under /run/codex names worktree /repo/worktrees/07",
            log=self.log,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.only_line()
        self.assertEqual(entry["event"], "session-cost")
        self.assertEqual(
            entry["detail"], "no rollout under /run/codex names worktree /repo/worktrees/07"
        )
        for field in (
            "input_tokens", "output_tokens", "cache_read_tokens",
            "cache_creation_tokens", "total_tokens", "session",
        ):
            self.assertNotIn(field, entry)

    def test_a_session_cost_for_a_review_carries_the_lane_it_was_reviewed_in(self):
        result = run_cli(
            "session-cost",
            "--ticket", "07",
            "--executor", "codex",
            "--model", "gpt-5.6-sol",
            "--lane", "codex gpt-5.6-sol",
            "--session", "019fffeb-8a95-7543-b9f1-68e4e6c854f3",
            "--input-tokens", "256833",
            "--output-tokens", "37664",
            "--cache-read-tokens", "11410432",
            "--cache-creation-tokens", "0",
            "--total-tokens", "11704929",
            log=self.log,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.only_line()
        self.assertEqual(entry["event"], "session-cost")
        self.assertEqual(entry["lane"], "codex gpt-5.6-sol")
        self.assertEqual(entry["session"], "019fffeb-8a95-7543-b9f1-68e4e6c854f3")
        self.assertEqual(entry["total_tokens"], 11704929)

    def test_a_session_cost_without_a_lane_is_still_the_implementing_child_it_was(self):
        """The lane is optional, so every row written before it existed keeps its meaning."""
        result = run_cli(
            "session-cost", "--ticket", "07", "--executor", "claude",
            "--model", "claude-opus-4-6-20260401",
            "--input-tokens", "1", "--output-tokens", "2", "--cache-read-tokens", "3",
            "--cache-creation-tokens", "4", "--total-tokens", "10",
            log=self.log,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("lane", self.only_line())

    def test_a_review_whose_rollout_could_not_be_read_carries_lane_and_diagnosis(self):
        result = run_cli(
            "session-cost", "--ticket", "07", "--executor", "codex",
            "--model", "gpt-5.6-sol", "--lane", "codex gpt-5.6-sol",
            "--detail", "no rollout under /run/codex/sessions ends in thread-7",
            log=self.log,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.only_line()
        self.assertEqual(entry["lane"], "codex gpt-5.6-sol")
        self.assertEqual(
            entry["detail"], "no rollout under /run/codex/sessions ends in thread-7"
        )
        self.assertNotIn("total_tokens", entry)

    def session_cost(self, *args):
        return run_cli(
            "session-cost", "--ticket", "07", "--executor", "claude",
            "--model", "claude-opus-4-6-20260401", *args, log=self.log,
        )

    def test_a_session_cost_carrying_both_figures_and_a_diagnosis_is_refused(self):
        result = self.session_cost(
            "--input-tokens", "1", "--output-tokens", "2", "--cache-read-tokens", "3",
            "--cache-creation-tokens", "4", "--total-tokens", "10",
            "--detail", "the transcript was unreadable",
        )

        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.log.exists())

    def test_a_session_cost_that_neither_counts_nor_diagnoses_is_refused(self):
        result = self.session_cost("--session", "9d1f4c2a-0000-4000-8000-000000000001")

        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.log.exists())

    def test_a_session_cost_missing_one_of_its_five_figures_is_refused(self):
        result = self.session_cost("--input-tokens", "1", "--total-tokens", "1")

        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.log.exists())

    def test_a_session_cost_whose_total_is_not_its_parts_is_refused(self):
        result = self.session_cost(
            "--input-tokens", "1", "--output-tokens", "2", "--cache-read-tokens", "3",
            "--cache-creation-tokens", "4", "--total-tokens", "11",
        )

        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.log.exists())

    def test_a_session_cost_for_an_executor_outside_the_closed_set_is_refused(self):
        result = run_cli(
            "session-cost", "--ticket", "07", "--executor", "gemini",
            "--model", "gemini-3-pro", log=self.log,
        )

        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.log.exists())

    def test_every_advance_decision_is_accepted_and_an_unknown_one_is_refused(self):
        for decision in ("launched", "escalated", "complete", "interrupted", "stopped"):
            self.assertEqual(
                run_cli("advance", "--wave", "1", "--decision", decision, log=self.log).returncode,
                0,
            )

        refused = run_cli("advance", "--wave", "1", "--decision", "advanced", log=self.log)

        self.assertEqual(refused.returncode, 2)
        self.assertEqual(len(self.lines()), 5)

    def test_every_verdict_outcome_and_merge_result_is_accepted(self):
        for verdict in ("landable", "parked", "failed"):
            self.assertEqual(
                run_cli("receipt", "--ticket", "07", "--verdict", verdict, log=self.log).returncode,
                0,
            )
        for outcome in ("completed", "failed", "parked", "blocked"):
            self.assertEqual(
                run_cli("outcome", "--ticket", "07", "--outcome", outcome, log=self.log).returncode,
                0,
            )
        for result in ("clean", "conflict", "repaired", "resolved", "escalated"):
            self.assertEqual(
                run_cli("merge", "--ticket", "07", "--result", result, log=self.log).returncode,
                0,
            )

        self.assertEqual(len(self.lines()), 12)

    def test_a_value_outside_a_closed_set_is_refused_and_appends_nothing(self):
        result = run_cli("receipt", "--ticket", "07", "--verdict", "landed", log=self.log)

        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.log.exists())

    def test_a_field_that_was_not_supplied_is_left_out_rather_than_written_empty(self):
        run_cli("outcome", "--ticket", "07", "--outcome", "completed", log=self.log)

        self.assertNotIn("detail", self.only_line())

    def test_the_log_and_its_directory_are_created_on_first_append(self):
        self.log = pathlib.Path(self.work.name) / "run" / "machine.log"

        result = run_cli("outcome", "--ticket", "07", "--outcome", "completed", log=self.log)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.lines()), 1)

    def test_a_direct_message_uses_the_hook_classification_for_both_roles(self):
        cases = (
            ("child", ESCALATION, "escalation"),
            ("coordinator", RULING, "ruling"),
        )

        for role, message, event in cases:
            result = run_cli(
                "message",
                "--role", role,
                "--ticket", "07",
                "--message", message,
                log=self.log,
            )

            self.assertEqual(result.returncode, 0, result.stderr)

        recorded = self.lines()
        self.assertEqual(
            [entry["event"] for entry in recorded],
            [event for _role, _message, event in cases],
        )
        for entry, (role, message, _event) in zip(recorded, cases):
            self.assertUniformTimestamp(entry)
            self.assertEqual(entry["role"], role)
            self.assertEqual(entry["ticket"], "07")
            self.assertEqual(entry["message"], message)


class AppendOnlyTests(MachineLogTestCase):
    def test_events_accumulate_in_the_order_they_were_recorded(self):
        run_cli("launch", "--ticket", "07", "--child", "c", "--workflow", "tdd",
                "--executor", "claude", "--model", "claude-opus-4-6-20260401",
                "--effort", "medium", log=self.log)
        run_cli("receipt", "--ticket", "07", "--verdict", "landable", log=self.log)
        run_cli("merge", "--ticket", "07", "--result", "clean", log=self.log)
        run_cli("outcome", "--ticket", "07", "--outcome", "completed", log=self.log)

        recorded = self.lines()

        self.assertEqual(
            [entry["event"] for entry in recorded],
            ["launch", "receipt", "merge", "outcome"],
        )
        for entry in recorded:
            self.assertUniformTimestamp(entry)

    def test_an_existing_log_is_appended_to_never_rewritten(self):
        self.log.write_text('{"ts": "2026-08-13T09:00:00Z", "event": "launch"}\n', encoding="utf-8")

        run_cli("outcome", "--ticket", "07", "--outcome", "completed", log=self.log)

        recorded = self.lines()
        self.assertEqual(len(recorded), 2)
        self.assertEqual(recorded[0], {"ts": "2026-08-13T09:00:00Z", "event": "launch"})

    def test_concurrent_writers_interleave_lines_never_characters(self):
        writers = [
            subprocess.Popen(
                [sys.executable, str(SCRIPT), "--log", str(self.log),
                 "outcome", "--ticket", f"{number:02d}", "--outcome", "completed"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            for number in range(1, 21)
        ]
        for writer in writers:
            self.assertEqual(writer.wait(), 0)

        recorded = self.lines()

        self.assertEqual(len(recorded), 20)
        self.assertEqual(
            sorted(entry["ticket"] for entry in recorded),
            [f"{number:02d}" for number in range(1, 21)],
        )


class HookTests(MachineLogTestCase):
    """The PostToolUse hook on SendMessage, on both sides of the channel."""

    def test_the_current_coordinator_passes_the_send_guard(self):
        seed_coordinator(self.log)

        result = run_guard(send_message_event(RULING), self.log, sender=COORDINATOR_SOCKET)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_a_stale_coordinator_is_denied_before_send_and_post_hook_mutation(self):
        seed_coordinator(self.log)
        stale = "/private/tmp/cc-socks-501/2601.sock"

        guarded = run_guard(send_message_event(RULING), self.log, sender=stale)
        posted = run_hook(
            send_message_event(RULING), self.log, role="coordinator", sender=stale
        )

        decision = json.loads(guarded.stdout)["hookSpecificOutput"]
        self.assertEqual(decision["hookEventName"], "PreToolUse")
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertEqual(
            decision["permissionDecisionReason"],
            "crew: this Coordinator no longer owns the run",
        )
        self.assertEqual(
            json.loads(posted.stdout)["systemMessage"],
            "crew: this Coordinator no longer owns the run",
        )
        self.assertFalse(self.log.exists())

    def test_a_coordinator_hook_without_a_socket_refuses_instead_of_guessing(self):
        seed_coordinator(self.log)

        guarded = run_guard(send_message_event(RULING), self.log)
        posted = run_hook(send_message_event(RULING), self.log, role="coordinator")

        reason = (
            "crew: no coordinator address in this environment"
            " (CLAUDE_CODE_MESSAGING_SOCKET unset)"
        )
        self.assertEqual(
            json.loads(guarded.stdout)["hookSpecificOutput"]["permissionDecisionReason"],
            reason,
        )
        self.assertEqual(json.loads(posted.stdout)["systemMessage"], reason)
        self.assertFalse(self.log.exists())

    def test_a_coordinators_outgoing_ruling_is_appended_verbatim(self):
        seed_coordinator(self.log)
        result = run_hook(
            send_message_event(RULING, to="agentcrew-dev-skills-07"),
            log=self.log, role="coordinator", sender=COORDINATOR_SOCKET,
        )

        self.assertEqual(result.returncode, 0)
        entry = self.only_line()
        self.assertUniformTimestamp(entry)
        self.assertEqual(entry["event"], "ruling")
        self.assertEqual(entry["role"], "coordinator")
        self.assertEqual(entry["to"], "agentcrew-dev-skills-07")
        self.assertEqual(entry["message"], RULING)

    def test_a_childs_outgoing_escalation_is_appended_verbatim(self):
        result = run_hook(
            send_message_event(ESCALATION, to="agentcrew-dev-skills-1f"),
            log=self.log, role="child", ticket="07",
        )

        self.assertEqual(result.returncode, 0)
        entry = self.only_line()
        self.assertUniformTimestamp(entry)
        self.assertEqual(entry["event"], "escalation")
        self.assertEqual(entry["role"], "child")
        self.assertEqual(entry["ticket"], "07")
        self.assertEqual(entry["to"], "agentcrew-dev-skills-1f")
        self.assertEqual(entry["message"], ESCALATION)

    def test_a_copied_message_names_the_address_it_was_sent_from(self):
        """The log reads as a conversation between identities, not between session titles."""
        socket = "/private/tmp/cc-socks-501/2277.sock"

        result = run_hook(
            send_message_event(ESCALATION, to="uds:/private/tmp/cc-socks-501/1504.sock"),
            log=self.log, role="child", ticket="07", sender=socket,
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.only_line()["from"], f"uds:{socket}")

    def test_a_session_the_harness_exported_no_socket_for_records_no_sender(self):
        """Not recorded and recorded empty are different facts, and this one is not recorded."""
        result = run_hook(
            send_message_event(ESCALATION, to="agentcrew-dev-skills-1f"),
            log=self.log, role="child", ticket="07",
        )

        self.assertEqual(result.returncode, 0)
        self.assertNotIn("from", self.only_line())

    def test_an_escalation_bundled_after_a_summary_is_still_an_escalation(self):
        """A child that explains itself first has still asked, and the log must say so."""
        result = run_hook(
            send_message_event(BUNDLED_ESCALATION, to="agentcrew-dev-skills-1f"),
            log=self.log, role="child", ticket="07",
        )

        self.assertEqual(result.returncode, 0)
        entry = self.only_line()
        self.assertEqual(entry["event"], "escalation")
        self.assertEqual(entry["message"], BUNDLED_ESCALATION)

    def test_wrap_up_is_classified_as_the_same_escalation_event_as_design(self):
        for kind in ("design", "wrap-up"):
            message = f"CREW ASK 07 {kind} — which option should I take? ts=1755060042"
            result = run_hook(
                send_message_event(message, to="agentcrew-dev-skills-1f"),
                log=self.log,
                role="child",
                ticket="07",
            )
            with self.subTest(kind=kind):
                self.assertEqual(result.returncode, 0)

        self.assertEqual(
            [entry["event"] for entry in self.lines()],
            ["escalation", "escalation"],
        )

    def test_a_verb_quoted_inside_a_line_is_not_read_as_one(self):
        """The verbs are anchored to a whole line, so prose that names one has not spoken it."""
        quoted = (
            "My first turn says to send CREW ASK 07 stuck when I am blocked. I am not blocked."
        )

        run_hook(send_message_event(quoted), log=self.log, role="child", ticket="07")

        self.assertEqual(self.only_line()["event"], "message")

    def test_an_indented_verb_line_is_quoting_rather_than_speaking(self):
        """A whole line means the whole line: a quoted example is set in from the margin."""
        quoted = (
            "My first turn tells me to send this when I am blocked:\n"
            "\n"
            "    CREW ASK 07 stuck — question, options, pointers ts=1755060042\n"
            "\n"
            "I am not blocked, so I have not sent it."
        )

        run_hook(send_message_event(quoted), log=self.log, role="child", ticket="07")

        self.assertEqual(self.only_line()["event"], "message")

    def test_the_last_verb_line_of_a_message_is_the_one_it_is_classified_by(self):
        """A final turn speaks once: whichever verb it ends on is the word it sent."""
        cases = (
            (f"CREW ASK 07 stuck — the fixture never came up.\nIt came up.\n"
             f"CREW COMPLETE {SHA}", "message"),
            (f"CREW COMPLETE {SHA}\nThat receipt was premature, the review is not back.\n"
             "CREW ASK 07 stuck — should I hold the receipt back? ts=1755060042", "escalation"),
        )

        for message, _event in cases:
            run_hook(send_message_event(message), log=self.log, role="child", ticket="07")

        self.assertEqual(
            [entry["event"] for entry in self.lines()], [event for _message, event in cases]
        )

    def test_a_childs_own_receipt_claim_never_takes_the_verified_receipts_name(self):
        """`receipt` belongs to the script that checked the sha; a claim is only a message."""
        claims = (
            "CREW COMPLETE b614ec84712aa8c351fe30ec69000e2e12518aeb ts=1755060100",
            "CREW FAILED the stub CLI never answered ts=1755060200",
            "CREW PARKED /repo/features/crew-v2/parked-07.md ts=1755060300",
        )
        for message in claims:
            run_hook(send_message_event(message), log=self.log, role="child", ticket="07")

        recorded = self.lines()

        self.assertEqual([entry["event"] for entry in recorded], ["message"] * 3)
        self.assertEqual([entry["message"] for entry in recorded], list(claims))

    def test_anything_else_a_child_sends_is_recorded_as_a_message(self):
        run_hook(send_message_event("acknowledged, resuming ts=1755060400"), log=self.log)

        self.assertEqual(self.only_line()["event"], "message")

    def test_everything_the_coordinator_sends_is_a_ruling_whatever_it_opens_with(self):
        """The coordinator is the top of the ladder: it answers escalations, it never sends one."""
        seed_coordinator(self.log)
        run_hook(
            send_message_event(ESCALATION), log=self.log, role="coordinator",
            sender=COORDINATOR_SOCKET,
        )

        self.assertEqual(self.only_line()["event"], "ruling")

    def test_verbatim_means_every_byte_including_newlines_and_padding(self):
        awkward = "CREW ASK 07 stuck —  two   spaces\n\ttab-indented\n\ntrailing space \n"

        run_hook(send_message_event(awkward), log=self.log)

        self.assertEqual(self.only_line()["message"], awkward)

    def test_a_long_message_is_copied_whole_and_never_truncated(self):
        long_message = "CREW ASK 07 scope — " + ("detail " * 4000) + "ts=1755060500"

        run_hook(send_message_event(long_message), log=self.log)

        self.assertEqual(self.only_line()["message"], long_message)

    def test_a_structured_message_is_recorded_as_the_object_it_was(self):
        structured = {"type": "shutdown_response", "request_id": "r-7", "approve": True}

        run_hook(send_message_event(structured), log=self.log)

        self.assertEqual(self.only_line()["message"], structured)

    def test_the_ticket_is_left_out_when_the_installing_side_knew_none(self):
        seed_coordinator(self.log)
        run_hook(
            send_message_event(RULING), log=self.log, role="coordinator",
            sender=COORDINATOR_SOCKET,
        )

        self.assertNotIn("ticket", self.only_line())


class ZeroTokenTests(MachineLogTestCase):
    """Nothing the hook emits may reach a model context."""

    def test_the_hook_says_nothing_on_any_channel_the_model_reads(self):
        result = run_hook(send_message_event(ESCALATION), log=self.log)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_a_log_that_cannot_be_written_still_leaves_the_send_standing(self):
        unwritable = pathlib.Path(self.work.name) / "closed"
        unwritable.mkdir(mode=0o500)
        self.addCleanup(unwritable.chmod, 0o700)

        result = run_hook(send_message_event(ESCALATION), log=unwritable / "machine.log")

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        # The one channel left is the human's: a systemMessage, which no model reads.
        if result.stdout:
            emitted = json.loads(result.stdout)
            self.assertEqual(set(emitted), {"systemMessage"})

    def test_a_call_that_is_not_a_sendmessage_appends_nothing(self):
        payload = send_message_event(ESCALATION)
        payload["tool_name"] = "Bash"

        result = run_hook(payload, log=self.log)

        self.assertEqual(result.returncode, 0)
        self.assertFalse(self.log.exists())

    def test_input_that_is_not_hook_json_appends_nothing_and_blocks_nothing(self):
        for payload in ("", "not json at all", "[]", '{"tool_name": "SendMessage"}'):
            result = run_hook(payload, log=self.log)

            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stderr, "")
        self.assertFalse(self.log.exists())


class InstallTests(MachineLogTestCase):
    """Registering the hook, which is what makes it fire on either side of the channel."""

    def setUp(self):
        super().setUp()
        self.settings = pathlib.Path(self.work.name) / ".claude" / "settings.local.json"
        self.settings.parent.mkdir(parents=True)

    def installed_hooks(self):
        settings = json.loads(self.settings.read_text(encoding="utf-8"))
        blocks = [
            block for block in settings["hooks"]["PostToolUse"]
            if block["matcher"] == "SendMessage"
        ]
        self.assertEqual(len(blocks), 1, "one block claims the SendMessage matcher")
        return blocks[0]["hooks"]

    def installed_bounded_read_hooks(self):
        settings = json.loads(self.settings.read_text(encoding="utf-8"))
        blocks = [
            block for block in settings["hooks"]["PreToolUse"]
            if block["matcher"] == "Read|Grep|Glob|Bash"
        ]
        self.assertEqual(len(blocks), 1, "one block claims the bounded-read matchers")
        return blocks[0]["hooks"]

    def installed_send_guard_hooks(self):
        settings = json.loads(self.settings.read_text(encoding="utf-8"))
        blocks = [
            block for block in settings["hooks"]["PreToolUse"]
            if block["matcher"] == "SendMessage"
        ]
        self.assertEqual(len(blocks), 1, "one pre-tool block claims SendMessage")
        return blocks[0]["hooks"]

    def install(self, role="child", ticket=None, script=None, session_id=None, crew_dir=None):
        args = ["install", "--settings", str(self.settings), "--role", role]
        if ticket is not None:
            args += ["--ticket", ticket]
        if script is not None:
            args += ["--hook-script", str(script)]
        if session_id is not None:
            args += ["--session-id", session_id]
        if crew_dir is not None:
            args += ["--crew-dir", str(crew_dir)]
        if role == "coordinator":
            args += ["--run-dir", str(self.log.parent)]
        return run_cli(*args, log=self.log)

    def test_the_child_side_registers_a_posttooluse_hook_on_sendmessage(self):
        result = self.install(role="child", ticket="07")

        self.assertEqual(result.returncode, 0, result.stderr)
        hooks = self.installed_hooks()
        self.assertEqual(len(hooks), 1)
        self.assertEqual(hooks[0]["type"], "command")
        self.assertIn("--role child", hooks[0]["command"])
        self.assertIn("--ticket 07", hooks[0]["command"])
        self.assertIn(str(self.log), hooks[0]["command"])

    def test_the_child_side_carries_no_bounded_read_hook(self):
        result = self.install(role="child", ticket="07")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(registered_bounded_commands(self.settings), [])

    def test_the_coordinator_side_registers_the_same_hook_without_a_ticket(self):
        self.assertEqual(self.install(role="coordinator").returncode, 0)

        command = self.installed_hooks()[0]["command"]

        self.assertIn("--role coordinator", command)
        self.assertNotIn("--ticket", command)

    def test_the_coordinator_registers_one_pretool_send_guard(self):
        result = self.install(role="coordinator")

        self.assertEqual(result.returncode, 0, result.stderr)
        hooks = self.installed_send_guard_hooks()
        self.assertEqual(len(hooks), 1)
        self.assertIn(" guard", hooks[0]["command"])

    def test_the_coordinator_install_also_registers_the_bounded_read_hook(self):
        result = self.install(
            role="coordinator", session_id=COORDINATOR_SESSION, crew_dir=CREW_DIR
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        hook, = self.installed_bounded_read_hooks()
        self.assertEqual(hook["type"], "command")
        self.assertIn("bounded_read.py", hook["command"])
        self.assertIn(" hook --crew-dir ", hook["command"])
        self.assertIn(f"--crew-dir {CREW_DIR}", hook["command"])
        self.assertIn(f"--run-dir {self.log.parent}", hook["command"])
        self.assertIn(f"--session-id {COORDINATOR_SESSION}", hook["command"])

    def test_a_coordinator_install_without_a_staged_run_directory_is_refused(self):
        result = run_cli(
            "install", "--settings", str(self.settings), "--role", "coordinator",
            log=self.log,
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("--run-dir", result.stderr)
        self.assertFalse(self.settings.exists())

    def test_installing_the_coordinator_twice_leaves_one_bounded_read_hook(self):
        self.install(role="coordinator")
        result = self.install(role="coordinator")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(registered_bounded_commands(self.settings)), 1)

    def test_a_run_copy_uses_the_explicit_crew_directory_for_a_manual_install(self):
        run_dir = pathlib.Path(self.work.name) / "run"
        run_dir.mkdir()
        copied_log = run_dir / "log.jsonl"
        copied_script = run_dir / "machine_log.py"
        copied_script.write_bytes(SCRIPT.read_bytes())
        copied_script.with_name("bounded_read.py").write_bytes(BOUNDED_SCRIPT.read_bytes())

        result = subprocess.run(
            [
                sys.executable, str(copied_script), "--log", str(copied_log),
                "install", "--settings", str(self.settings), "--role", "coordinator",
                "--crew-dir", str(CREW_DIR), "--run-dir", str(run_dir),
                "--session-id", COORDINATOR_SESSION,
            ],
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        command, = registered_bounded_commands(self.settings)
        self.assertIn(f"--crew-dir {CREW_DIR}", command)
        self.assertIn(f"--run-dir {run_dir}", command)

    def test_the_registered_command_is_the_one_that_writes_the_log(self):
        self.install(role="child", ticket="07")

        command = self.installed_hooks()[0]["command"]
        sent_here = send_message_event(ESCALATION, cwd=str(self.settings.parent.parent))
        result = subprocess.run(
            command, shell=True, input=json.dumps(sent_here),
            capture_output=True, text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.only_line()
        self.assertEqual(entry["event"], "escalation")
        self.assertEqual(entry["ticket"], "07")
        self.assertEqual(entry["message"], ESCALATION)

    def test_installing_twice_leaves_one_hook_not_two(self):
        self.install(role="child", ticket="07")
        self.install(role="child", ticket="07")

        self.assertEqual(len(self.installed_hooks()), 1)

    def test_the_guard_hooks_already_in_the_settings_file_survive(self):
        self.settings.write_text(json.dumps({
            "permissions": {"allow": ["SendMessage", "ListAgents"]},
            "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
                {"type": "command", "command": "/worktree/.claude/red-line.sh"},
            ]}]},
        }), encoding="utf-8")

        self.install(role="child", ticket="07")

        settings = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(settings["permissions"]["allow"], ["SendMessage", "ListAgents"])
        self.assertEqual(
            settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"],
            "/worktree/.claude/red-line.sh",
        )
        self.assertEqual(len(self.installed_hooks()), 1)

    def test_an_empty_settings_file_is_a_fresh_document_not_content_to_protect(self):
        """Deliberate: a `touch`ed settings file has nothing to lose, so it is written, not refused.

        The refusal exists to protect the guard hooks that live in this file. An empty file holds
        none, so treating it as `{}` costs nothing and keeps a plausible starting state working.
        """
        for body in ("", "   \n"):
            self.settings.write_text(body, encoding="utf-8")

            result = self.install(role="child", ticket="07")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(self.installed_hooks()), 1, body)

    def test_a_copy_of_the_script_installs_beside_the_original_not_over_it(self):
        copy = pathlib.Path(self.work.name) / "worktree" / "machine_log.py"
        copy.parent.mkdir()
        copy.write_bytes(SCRIPT.read_bytes())

        self.install(role="child", ticket="07", script=copy)
        self.install(role="child", ticket="07", script=copy)
        self.install(role="child", ticket="08", script=copy)

        hooks = self.installed_hooks()

        self.assertEqual(len(hooks), 1, "the same script re-registers, it does not accumulate")
        self.assertIn("--ticket 08", hooks[0]["command"])
        self.assertIn(str(copy), hooks[0]["command"])

    def test_a_hook_that_is_not_this_scripts_is_left_alone(self):
        self.settings.write_text(json.dumps({"hooks": {"PostToolUse": [
            {"matcher": "SendMessage", "hooks": [
                {"type": "command", "command": "/notify/machine_log.py-watcher --tag sent"},
            ]},
        ]}}), encoding="utf-8")

        self.install(role="child", ticket="07")

        commands = [hook["command"] for hook in self.installed_hooks()]

        self.assertIn("/notify/machine_log.py-watcher --tag sent", commands)
        self.assertEqual(len(commands), 2)

    def test_a_settings_file_of_an_unexpected_shape_is_refused_never_overwritten(self):
        shapes = (
            '["not a settings document"]',
            '{"hooks": []}',
            '{"hooks": {"PostToolUse": 1}}',
            '{"hooks": {"PostToolUse": [{"matcher": "SendMessage", "hooks": {"a": "b"}}]}}',
        )
        for body in shapes:
            self.settings.write_text(body, encoding="utf-8")

            result = self.install(role="child", ticket="07")

            self.assertEqual(result.returncode, 1, body)
            self.assertEqual(self.settings.read_text(encoding="utf-8"), body)

    def test_a_settings_file_that_cannot_be_parsed_is_refused_never_overwritten(self):
        self.settings.write_text('{"hooks": ', encoding="utf-8")

        result = self.install(role="child", ticket="07")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.settings.read_text(encoding="utf-8"), '{"hooks": ')


class InheritedSettingsTests(MachineLogTestCase):
    """Both sides' hooks fire on a child's send, because the worktree inherits the repo's settings.

    A child's worktree sits under the repository the coordinator runs in, so the coordinator's
    hook is loaded in the child's session too and every hook the two files register runs on one
    send. What decides which of them writes is the directory each was installed for: a message is
    logged by the side whose session sent it, once, under that side's own role.
    """

    def setUp(self):
        super().setUp()
        self.repo = pathlib.Path(self.work.name) / "repo"
        self.worktree = self.repo / ".claude" / "worktrees" / "07"
        self.log = self.repo / "features" / "crew-v3" / ".crew" / "log.jsonl"
        self.coordinator_settings = self.repo / ".claude" / "settings.local.json"
        self.child_settings = self.worktree / ".claude" / "settings.local.json"
        self.child_settings.parent.mkdir(parents=True)
        self.assertEqual(
            run_cli("install", "--settings", str(self.coordinator_settings),
                    "--role", "coordinator", "--run-dir", str(self.log.parent.parent),
                    log=self.log).returncode,
            0,
        )
        self.assertEqual(
            run_cli("install", "--settings", str(self.child_settings),
                    "--role", "child", "--ticket", "07", log=self.log).returncode,
            0,
        )

    def send_from(self, cwd, message, sender=None):
        """Fire every hook an inheriting session sees: the repo root's and the worktree's."""
        payload = json.dumps(send_message_event(message, cwd=str(cwd)))
        for settings in (self.coordinator_settings, self.child_settings):
            for command in registered_commands(settings):
                result = subprocess.run(
                    command, shell=True, input=payload, capture_output=True, text=True,
                    env=hook_environment(sender),
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_childs_message_is_logged_once_as_the_childs(self):
        self.send_from(self.worktree, ESCALATION)

        entry = self.only_line()

        self.assertEqual(entry["event"], "escalation")
        self.assertEqual(entry["role"], "child")
        self.assertEqual(entry["ticket"], "07")
        self.assertEqual(entry["message"], ESCALATION)

    def test_a_childs_message_is_never_logged_as_a_coordinator_ruling(self):
        self.send_from(self.worktree, "CREW COMPLETE b614ec84712aa8c351fe30ec69000e2e12518aeb")

        self.assertEqual([entry["role"] for entry in self.lines()], ["child"])

    def test_the_coordinators_own_message_is_logged_once_as_a_ruling(self):
        seed_coordinator(self.log)

        self.send_from(self.repo, RULING, sender=COORDINATOR_SOCKET)

        entry = self.only_line()

        self.assertEqual(entry["event"], "ruling")
        self.assertEqual(entry["role"], "coordinator")
        self.assertNotIn("ticket", entry)


class VersionIndependentPathTests(MachineLogTestCase):
    """The registered command outlives the plugin version that installed it (#37)."""

    VERSION = "0.3.8"

    def setUp(self):
        super().setUp()
        self.project = pathlib.Path(self.work.name) / "repo"
        self.settings = self.project / ".claude" / "settings.local.json"
        self.settings.parent.mkdir(parents=True)
        self.log = self.project / "features" / "crew-v3" / ".crew" / "log.jsonl"
        # The plugin as it is installed on a machine: one directory per released version.
        self.plugin = (
            pathlib.Path(self.work.name) / "plugins" / self.VERSION
            / "skills" / "crew" / "assets" / "machine_log.py"
        )
        self.plugin.parent.mkdir(parents=True)
        self.plugin.write_bytes(SCRIPT.read_bytes())
        self.plugin.with_name("bounded_read.py").write_bytes(BOUNDED_SCRIPT.read_bytes())
        self.plugin.with_name("coordinator_control.py").write_bytes(
            CONTROL_SCRIPT.read_bytes()
        )

    def install_from_the_plugin(self):
        """Install the way a run does: the plugin's own copy, naming no script but itself."""
        return subprocess.run(
            [sys.executable, str(self.plugin), "--log", str(self.log),
             "install", "--settings", str(self.settings), "--role", "coordinator",
             "--run-dir", str(self.log.parent.parent)],
            capture_output=True, text=True,
        )

    def test_the_installed_command_carries_no_plugin_version_in_its_path(self):
        result = self.install_from_the_plugin()

        self.assertEqual(result.returncode, 0, result.stderr)
        command, = registered_commands(self.settings)
        self.assertNotIn(self.VERSION, command)

    def test_the_hook_still_writes_the_log_after_the_installing_version_is_gone(self):
        self.install_from_the_plugin()
        command, = registered_commands(self.settings)
        seed_coordinator(self.log)
        # The upgrade: the version that installed the hook is no longer on this machine.
        shutil.rmtree(pathlib.Path(self.work.name) / "plugins" / self.VERSION)

        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            input=json.dumps(send_message_event(RULING, cwd=str(self.project))),
            env=hook_environment(COORDINATOR_SOCKET),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.only_line()["event"], "ruling")

    def test_the_bounded_hook_still_runs_after_the_installing_version_is_gone(self):
        self.install_from_the_plugin()
        command, = registered_bounded_commands(self.settings)
        shutil.rmtree(pathlib.Path(self.work.name) / "plugins" / self.VERSION)

        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            input=json.dumps({"tool_name": "Grep", "tool_input": {"pattern": "needle"}}),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        decision = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")


class UninstallTests(MachineLogTestCase):
    """Taking the hook back out: what the matching install wrote, and nothing else."""

    GUARD_COMMAND = "/worktree/.claude/red-line.sh"
    FOREIGN_COMMAND = "/notify/machine_log.py-watcher --tag sent"
    # An entry from a finished run, pinned to a plugin version that is no longer on the machine.
    STALE_SCRIPT = "/cache/agentcrew-dev-skills/0.3.0/skills/crew/assets/machine_log.py"
    STALE_LOG = "/repo/features/crew-first-run-defects/.crew/log.jsonl"

    def setUp(self):
        super().setUp()
        self.project = pathlib.Path(self.work.name) / "repo"
        self.settings = self.project / ".claude" / "settings.local.json"
        self.settings.parent.mkdir(parents=True)
        self.log = self.project / "features" / "crew-v3" / ".crew" / "log.jsonl"

    def install(self, log=None, role="child"):
        chosen_log = log if log is not None else self.log
        ticket = ("--ticket", "07") if role == "child" else ()
        run_dir = ("--run-dir", str(chosen_log.parent.parent)) if role == "coordinator" else ()
        return run_cli(
            "install", "--settings", str(self.settings), "--role", role, *ticket, *run_dir,
            log=chosen_log,
        )

    def uninstall(self, log=None):
        return run_cli(
            "uninstall", "--settings", str(self.settings),
            log=log if log is not None else self.log,
        )

    def stale_entry(self, log):
        """An entry a previous plugin version wrote for `log`, at its own versioned path."""
        return {"type": "command", "command":
                f"python3 {self.STALE_SCRIPT} --log {log} hook --role coordinator"}

    def test_uninstalling_removes_the_entry_the_matching_install_wrote(self):
        self.install()

        result = self.uninstall()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(registered_commands(self.settings), [])

    def test_uninstalling_the_coordinator_removes_both_entries(self):
        self.install(role="coordinator")

        result = self.uninstall()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(registered_commands(self.settings), [])
        self.assertEqual(registered_bounded_commands(self.settings), [])

    def test_uninstalling_removes_a_bounded_entry_when_the_message_entry_is_already_absent(self):
        self.install(role="coordinator")
        settings = json.loads(self.settings.read_text(encoding="utf-8"))
        settings["hooks"].pop("PostToolUse")
        self.settings.write_text(json.dumps(settings), encoding="utf-8")

        result = self.uninstall()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(registered_bounded_commands(self.settings), [])

    def test_uninstalling_removes_a_message_entry_when_the_bounded_entry_is_already_absent(self):
        self.install(role="coordinator")
        settings = json.loads(self.settings.read_text(encoding="utf-8"))
        settings["hooks"].pop("PreToolUse")
        self.settings.write_text(json.dumps(settings), encoding="utf-8")

        result = self.uninstall()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(registered_commands(self.settings), [])

    def test_uninstalling_twice_changes_nothing_the_second_time(self):
        self.install()
        self.uninstall()
        settled = self.settings.read_text(encoding="utf-8")

        result = self.uninstall()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.settings.read_text(encoding="utf-8"), settled)

    def test_uninstalling_from_a_file_that_never_carried_the_hook_leaves_it_untouched(self):
        body = json.dumps({"permissions": {"allow": ["SendMessage"]}})
        self.settings.write_text(body, encoding="utf-8")

        result = self.uninstall()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.settings.read_text(encoding="utf-8"), body)

    def test_every_other_hook_in_the_file_is_left_where_it_is(self):
        self.settings.write_text(json.dumps({
            "permissions": {"allow": ["SendMessage", "ListAgents"]},
            "hooks": {
                "PreToolUse": [{"matcher": "Bash", "hooks": [
                    {"type": "command", "command": self.GUARD_COMMAND},
                ]}],
                "PostToolUse": [{"matcher": "SendMessage", "hooks": [
                    {"type": "command", "command": self.FOREIGN_COMMAND},
                ]}],
            },
        }), encoding="utf-8")
        self.install(role="coordinator")

        self.uninstall()

        settings = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(settings["permissions"]["allow"], ["SendMessage", "ListAgents"])
        self.assertEqual(
            settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"], self.GUARD_COMMAND
        )
        self.assertEqual(registered_bounded_commands(self.settings), [])
        self.assertEqual(registered_commands(self.settings), [self.FOREIGN_COMMAND])

    def test_another_runs_entry_in_the_same_file_survives(self):
        other_log = self.project / "features" / "dashboard-pin" / ".crew" / "log.jsonl"
        self.install()
        self.install(log=other_log)

        self.uninstall()

        remaining, = registered_commands(self.settings)
        self.assertIn(str(other_log), remaining)
        self.assertNotIn(str(self.log), remaining)

    def test_a_stale_version_pinned_entry_is_removed_by_the_log_it_was_writing(self):
        self.settings.write_text(json.dumps({"hooks": {"PostToolUse": [
            {"matcher": "SendMessage", "hooks": [
                self.stale_entry(self.STALE_LOG),
                {"type": "command", "command": self.FOREIGN_COMMAND},
            ]},
        ]}}), encoding="utf-8")

        result = self.uninstall(log=self.STALE_LOG)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(registered_commands(self.settings), [self.FOREIGN_COMMAND])

    def test_an_entry_a_previous_plugin_version_wrote_for_this_run_is_replaced_not_doubled(self):
        """The upgrade case: the installed path changes with the version, the run does not."""
        self.settings.write_text(json.dumps({"hooks": {"PostToolUse": [
            {"matcher": "SendMessage", "hooks": [self.stale_entry(self.log)]},
        ]}}), encoding="utf-8")

        self.install()

        command, = registered_commands(self.settings)
        self.assertNotIn(self.STALE_SCRIPT, command)
        self.assertIn(str(self.log), command)

    def test_an_entry_a_previous_plugin_version_wrote_for_this_run_is_uninstalled_too(self):
        self.settings.write_text(json.dumps({"hooks": {"PostToolUse": [
            {"matcher": "SendMessage", "hooks": [self.stale_entry(self.log)]},
        ]}}), encoding="utf-8")

        result = self.uninstall()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(registered_commands(self.settings), [])

    def test_a_hook_whose_log_merely_begins_with_ours_is_left_alone(self):
        """One run directory's path is a prefix of another's the moment it is named after it."""
        neighbour = (
            f"python3 /elsewhere/watcher.py --log {self.log}-other hook --role coordinator"
        )
        self.settings.write_text(json.dumps({"hooks": {"PostToolUse": [
            {"matcher": "SendMessage", "hooks": [{"type": "command", "command": neighbour}]},
        ]}}), encoding="utf-8")

        result = self.uninstall()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(registered_commands(self.settings), [neighbour])

    def test_a_foreign_command_using_the_word_guard_is_not_claimed_by_this_run(self):
        foreign = f"python3 /elsewhere/policy.py --log {self.log} guard"
        self.settings.write_text(json.dumps({"hooks": {"PreToolUse": [
            {"matcher": "SendMessage", "hooks": [
                {"type": "command", "command": foreign},
            ]},
        ]}}), encoding="utf-8")

        result = self.uninstall()

        self.assertEqual(result.returncode, 0, result.stderr)
        settings = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"], foreign)

    def test_a_missing_settings_file_is_nothing_to_uninstall(self):
        result = self.uninstall()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.settings.exists())

    def test_a_settings_file_that_cannot_be_parsed_is_refused_never_overwritten(self):
        self.settings.write_text('{"hooks": ', encoding="utf-8")

        result = self.uninstall()

        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.settings.read_text(encoding="utf-8"), '{"hooks": ')


class PauseTests(MachineLogTestCase):
    """The two ends of a vendor usage-limit wait, written by the child's own lifecycle hooks."""

    def pause(self, ticket="07", log=None):
        return run_cli("pause", "--ticket", ticket, log=log if log is not None else self.log)

    def resume(self, ticket="07", log=None):
        return run_cli("resume", "--ticket", ticket, log=log if log is not None else self.log)

    def test_the_pause_command_appends_one_child_side_record_for_its_ticket(self):
        result = self.pause()

        self.assertEqual(result.returncode, 0, result.stderr)
        record = self.only_line()
        self.assertEqual(record["event"], "paused")
        self.assertEqual(record["ticket"], "07")
        self.assertEqual(record["role"], "child")
        self.assertUniformTimestamp(record)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_the_projection_reports_the_ticket_paused_once_the_record_is_in(self):
        self.pause()

        projection = machine_log.project(machine_log.read_records(self.log))

        self.assertTrue(projection.ticket("07").paused)
        self.assertFalse(projection.ticket("08").paused)

    def test_the_resume_command_writes_only_while_the_ticket_is_paused(self):
        self.assertEqual(self.resume().returncode, 0)
        self.assertFalse(self.log.exists(), "a resume with no pause open writes nothing")

        self.pause()
        self.assertEqual(self.resume().returncode, 0)
        self.assertEqual([record["event"] for record in self.lines()], ["paused", "resumed"])
        self.assertFalse(machine_log.project(self.lines()).ticket("07").paused)

        self.assertEqual(self.resume().returncode, 0)
        self.assertEqual(len(self.lines()), 2, "a second resume has no pause left to end")

    def test_a_pause_is_per_ticket(self):
        self.pause(ticket="07")
        self.resume(ticket="08")

        self.assertEqual([record["event"] for record in self.lines()], ["paused"])
        projection = machine_log.project(self.lines())
        self.assertTrue(projection.ticket("07").paused)
        self.assertFalse(projection.ticket("08").paused)

    def test_both_commands_stay_silent_and_write_nothing_when_the_log_cannot_be_written(self):
        blocked = pathlib.Path(self.work.name) / "blocked"
        blocked.write_text("not a directory\n", encoding="utf-8")
        unwritable = blocked / "log.jsonl"

        for result in (self.pause(log=unwritable), self.resume(log=unwritable)):
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.stderr, "")
        self.assertEqual(blocked.read_text(encoding="utf-8"), "not a directory\n")

    def test_a_paused_ticket_is_re_projected_the_same_way_from_scratch(self):
        records = [
            {"event": "launch", "ticket": "07", "child": "crew-07"},
            {"event": "paused", "ticket": "07", "role": "child"},
        ]

        self.assertTrue(machine_log.project(records).ticket("07").paused)
        self.assertTrue(machine_log.project(list(records)).ticket("07").paused)

    def test_the_child_speaking_again_ends_the_pause_as_surely_as_a_resume_record(self):
        cases = (
            ("nothing", [], False),
            ("paused", [{"event": "paused", "ticket": "07", "role": "child"}], True),
            ("paused then resumed", [
                {"event": "paused", "ticket": "07", "role": "child"},
                {"event": "resumed", "ticket": "07", "role": "child"},
            ], False),
            ("paused then a receipt claim", [
                {"event": "paused", "ticket": "07", "role": "child"},
                {"event": "message", "ticket": "07", "role": "child",
                 "message": f"CREW COMPLETE {SHA}"},
            ], False),
            ("paused then an escalation", [
                {"event": "paused", "ticket": "07", "role": "child"},
                {"event": "escalation", "ticket": "07", "role": "child", "message": ESCALATION},
            ], False),
            ("a ruling does not end it", [
                {"event": "paused", "ticket": "07", "role": "child"},
                {"event": "ruling", "ticket": "07", "role": "coordinator", "message": RULING},
            ], True),
            ("a driver event does not end it", [
                {"event": "paused", "ticket": "07", "role": "child"},
                {"event": "review", "ticket": "07", "lane": REVIEW_LANE, "state": "running"},
            ], True),
            ("paused again after resuming", [
                {"event": "paused", "ticket": "07", "role": "child"},
                {"event": "resumed", "ticket": "07", "role": "child"},
                {"event": "paused", "ticket": "07", "role": "child"},
            ], True),
            ("a relaunch ends a pause its own child never did", [
                {"event": "paused", "ticket": "07", "role": "child"},
                {"event": "launch", "ticket": "07", "child": "crew-07-again"},
            ], False),
        )
        for name, records, expected in cases:
            with self.subTest(name):
                self.assertEqual(machine_log.project(records).ticket("07").paused, expected)

    def test_a_pause_leaves_no_standing_nudge_for_the_silence_after_it_to_inherit(self):
        """The nudge's own turn ended on the limit, so the silence it addressed is over.

        A nudge left standing across the wait would settle the resumed child `failed` on the first
        idle it reported, which is the failure this ticket exists to end one rung further down.
        """
        nudged = [
            {"event": "launch", "ticket": "07", "child": "crew-07"},
            {"event": "ruling", "ticket": "07", "role": "coordinator",
             "message": f"{machine_log.NUDGE_MARKER} 07 — send your receipt"},
        ]

        self.assertTrue(machine_log.project(nudged).ticket("07").outstanding_nudge)

        paused = machine_log.project(
            nudged + [{"event": "paused", "ticket": "07", "role": "child"}]
        ).ticket("07")
        self.assertFalse(paused.outstanding_nudge)
        self.assertTrue(paused.paused)

        resumed = machine_log.project(
            nudged
            + [{"event": "paused", "ticket": "07", "role": "child"}]
            + [{"event": "resumed", "ticket": "07", "role": "child"}]
        ).ticket("07")
        self.assertFalse(resumed.outstanding_nudge)
        self.assertFalse(resumed.paused)

    def test_a_strangers_command_carrying_the_word_pause_is_not_this_runs_hook(self):
        """A claimed entry is a deleted entry: `uninstall` takes out everything it recognises."""
        log = str(self.log)
        cases = (
            ("ours", f"python3 /run/machine_log.py --log {log} pause --ticket 07", True),
            ("ours, joined", f"python3 /run/machine_log.py --log={log} resume --ticket 07", True),
            ("a stranger in the subcommand slot",
             f"python3 /foreign/tool.py --log {log} pause --ticket 07", False),
            ("a stranger that pauses something else",
             f"/foreign/tool.py --log {log} watch --mode pause --ticket 07", False),
            ("a stranger named nearly ours",
             f"python3 /notify/machine_log.py-watcher --log {log} pause --ticket 07", False),
            ("a stranger with no ticket", f"python3 /run/machine_log.py --log {log} pause", False),
            ("a stranger's own log", "python3 /run/machine_log.py --log /other pause --ticket 07",
             False),
        )
        for name, command, ours in cases:
            with self.subTest(name):
                entry = {"type": "command", "command": command}
                self.assertEqual(machine_log.registered_for(entry, self.log), ours)


class PauseHookInstallTests(MachineLogTestCase):
    """Where the two lifecycle hooks are registered, and that they leave with the message hook."""

    def setUp(self):
        super().setUp()
        self.settings = pathlib.Path(self.work.name) / ".claude" / "settings.local.json"
        self.settings.parent.mkdir(parents=True)

    def install(self, role="child", ticket="07"):
        arguments = ["install", "--settings", str(self.settings), "--role", role]
        if ticket is not None:
            arguments += ["--ticket", ticket]
        if role == "coordinator":
            arguments += ["--run-dir", str(self.log.parent)]
        return run_cli(*arguments, log=self.log)

    def entries(self, event, matcher):
        document = json.loads(self.settings.read_text(encoding="utf-8"))
        blocks = [
            block for block in document.get("hooks", {}).get(event, [])
            if block.get("matcher") == matcher
        ]
        self.assertLessEqual(len(blocks), 1, f"one block claims {event}/{matcher}")
        return blocks[0]["hooks"] if blocks else []

    def paused_entries(self):
        return self.entries("StopFailure", "rate_limit")

    def resumed_entries(self):
        return self.entries("Stop", None)

    def test_a_child_install_registers_the_pause_and_resume_hooks(self):
        result = self.install()

        self.assertEqual(result.returncode, 0, result.stderr)
        for entries, subcommand in (
            (self.paused_entries(), "pause"), (self.resumed_entries(), "resume")
        ):
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["type"], "command")
            self.assertIn(f" {subcommand} --ticket 07", entries[0]["command"])
            self.assertIn(str(self.log), entries[0]["command"])
            self.assertEqual(entries[0]["timeout"], machine_log.LIFECYCLE_HOOK_TIMEOUT_SECONDS)

    def test_the_registered_commands_run_and_do_what_they_say(self):
        self.install()

        for entries in (self.paused_entries(), self.resumed_entries()):
            result = subprocess.run(
                entries[0]["command"], shell=True, capture_output=True, text=True, input="{}"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([record["event"] for record in self.lines()], ["paused", "resumed"])

    def test_installing_twice_registers_each_hook_once(self):
        self.install()
        self.install()

        self.assertEqual(len(self.paused_entries()), 1)
        self.assertEqual(len(self.resumed_entries()), 1)

    def test_the_coordinator_side_registers_neither(self):
        self.install(role="coordinator", ticket=None)

        self.assertEqual(self.paused_entries(), [])
        self.assertEqual(self.resumed_entries(), [])

    def test_uninstalling_takes_both_out_and_leaves_a_stranger_where_it_is(self):
        stranger = {"type": "command", "command": "/worktree/.claude/announce.sh"}
        self.settings.write_text(
            json.dumps({"hooks": {"Stop": [{"hooks": [stranger]}]}}), encoding="utf-8"
        )
        self.install()

        result = run_cli("uninstall", "--settings", str(self.settings), log=self.log)

        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertNotIn("StopFailure", document["hooks"])
        self.assertEqual(document["hooks"]["Stop"], [{"hooks": [stranger]}])


class MachineParseabilityTests(MachineLogTestCase):
    """A later agent reconstructs the run from the file alone."""

    def test_a_run_reads_back_as_one_object_per_line_with_arithmetic_stamps(self):
        seed_coordinator(self.log)
        run_cli("launch", "--ticket", "07", "--child", "c", "--workflow", "tdd",
                "--executor", "claude", "--model", "claude-opus-4-6-20260401",
                "--effort", "medium", log=self.log)
        run_hook(send_message_event(ESCALATION), log=self.log, role="child", ticket="07")
        run_hook(
            send_message_event(RULING), log=self.log, role="coordinator",
            sender=COORDINATOR_SOCKET,
        )
        run_cli("receipt", "--ticket", "07", "--verdict", "landable", log=self.log)
        run_cli("outcome", "--ticket", "07", "--outcome", "completed", log=self.log)

        raw = self.log.read_text(encoding="utf-8").splitlines()
        recorded = [json.loads(line) for line in raw]

        self.assertEqual(
            [entry["event"] for entry in recorded],
            ["launch", "escalation", "ruling", "receipt", "outcome"],
        )
        for line in raw:
            self.assertNotIn("\n", line)
        stamps = [
            datetime.datetime.strptime(entry["ts"], TIMESTAMP_FORMAT) for entry in recorded
        ]
        self.assertEqual(stamps, sorted(stamps), "stamps subtract to a duration in order")


if __name__ == "__main__":
    unittest.main()
