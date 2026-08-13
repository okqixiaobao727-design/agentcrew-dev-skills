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
import subprocess
import sys
import tempfile
import unittest


PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = PLUGIN_ROOT / "skills" / "crew" / "assets" / "machine_log.py"

# The run's one timestamp format: `date -u +%Y-%m-%dT%H:%M:%SZ`, as the crew skill reads it.
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

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


def run_cli(*args, log=None):
    command = [sys.executable, str(SCRIPT)]
    if log is not None:
        command += ["--log", str(log)]
    return subprocess.run([*command, *args], capture_output=True, text=True)


def run_hook(payload, log=None, role="child", ticket=None):
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
    )


def send_message_event(message, to="crew-coordinator"):
    """A PostToolUse payload for a SendMessage call that has just been made."""
    return {
        "session_id": "9d1f4c2a-0000-4000-8000-000000000001",
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": "/tmp/worktree",
        "hook_event_name": "PostToolUse",
        "tool_name": "SendMessage",
        "tool_input": {"to": to, "message": message},
        "tool_response": {"status": "delivered"},
    }


class MachineLogTestCase(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.log = pathlib.Path(self.work.name) / "machine.log"

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
        for result in ("clean", "conflict", "repaired", "escalated"):
            self.assertEqual(
                run_cli("merge", "--ticket", "07", "--result", result, log=self.log).returncode,
                0,
            )

        self.assertEqual(len(self.lines()), 11)

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

    def test_a_coordinators_outgoing_ruling_is_appended_verbatim(self):
        result = run_hook(
            send_message_event(RULING, to="agentcrew-dev-skills-07"),
            log=self.log, role="coordinator",
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
        run_hook(send_message_event(ESCALATION), log=self.log, role="coordinator")

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
        run_hook(send_message_event(RULING), log=self.log, role="coordinator")

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

    def install(self, role="child", ticket=None, script=None):
        args = ["install", "--settings", str(self.settings), "--role", role]
        if ticket is not None:
            args += ["--ticket", ticket]
        if script is not None:
            args += ["--hook-script", str(script)]
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

    def test_the_coordinator_side_registers_the_same_hook_without_a_ticket(self):
        self.assertEqual(self.install(role="coordinator").returncode, 0)

        command = self.installed_hooks()[0]["command"]

        self.assertIn("--role coordinator", command)
        self.assertNotIn("--ticket", command)

    def test_the_registered_command_is_the_one_that_writes_the_log(self):
        self.install(role="child", ticket="07")

        command = self.installed_hooks()[0]["command"]
        result = subprocess.run(
            command, shell=True, input=json.dumps(send_message_event(ESCALATION)),
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


class MachineParseabilityTests(MachineLogTestCase):
    """A later agent reconstructs the run from the file alone."""

    def test_a_run_reads_back_as_one_object_per_line_with_arithmetic_stamps(self):
        run_cli("launch", "--ticket", "07", "--child", "c", "--workflow", "tdd",
                "--executor", "claude", "--model", "claude-opus-4-6-20260401",
                "--effort", "medium", log=self.log)
        run_hook(send_message_event(ESCALATION), log=self.log, role="child", ticket="07")
        run_hook(send_message_event(RULING), log=self.log, role="coordinator")
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
