#!/usr/bin/env python3
"""Drive the coordinator's bounded-read hook through its PreToolUse stdin boundary."""

import json
import pathlib
import subprocess
import sys
import unittest


PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = PLUGIN_ROOT / "skills" / "crew" / "assets" / "bounded_read.py"
CREW_DIR = PLUGIN_ROOT / "skills" / "crew"
SESSION_ID = "9d1f4c2a-0000-4000-8000-000000000133"


class BoundedReadHookTests(unittest.TestCase):
    def run_hook(self, tool_name, tool_input, session_id=SESSION_ID, configured_session_id=None):
        command = [sys.executable, str(SCRIPT), "hook", "--crew-dir", str(CREW_DIR)]
        if configured_session_id is not None:
            command += ["--session-id", configured_session_id]
        return subprocess.run(
            command,
            input=json.dumps({
                "session_id": session_id,
                "tool_name": tool_name,
                "tool_input": tool_input,
            }),
            capture_output=True,
            text=True,
        )

    def test_a_read_with_an_explicit_offset_and_eighty_line_limit_is_allowed(self):
        result = self.run_hook(
            "Read",
            {"file_path": "/repo/ticket.md", "offset": 12, "limit": 80},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_a_read_without_an_offset_is_refused_with_the_contract_pointer(self):
        result = self.run_hook("Read", {"file_path": "/repo/ticket.md", "limit": 80})

        self.assertEqual(result.returncode, 0, result.stderr)
        decision = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn("escalation", decision["permissionDecisionReason"])
        self.assertIn("witness brief", decision["permissionDecisionReason"])

    def test_a_read_over_the_eighty_line_limit_is_refused(self):
        result = self.run_hook(
            "Read",
            {"file_path": "/repo/ticket.md", "offset": 12, "limit": 81},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        decision = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")

    def test_a_read_under_the_crew_skill_directory_is_allowed_at_any_size(self):
        result = self.run_hook(
            "Read",
            {"file_path": str(CREW_DIR / "SKILL.md"), "limit": 1000},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_search_and_shell_reads_under_the_crew_skill_directory_are_allowed(self):
        cases = (
            ("Grep", {"pattern": "Contract", "path": str(CREW_DIR)}),
            ("Glob", {"pattern": "**/*.md", "path": str(CREW_DIR)}),
            ("Bash", {"command": f"cat {CREW_DIR / 'SKILL.md'}"}),
        )
        for tool_name, tool_input in cases:
            with self.subTest(tool_name=tool_name):
                result = self.run_hook(tool_name, tool_input)

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "")

    def test_grep_and_glob_are_refused_as_hunts(self):
        for tool_name, tool_input in (
            ("Grep", {"pattern": "needle", "path": "/repo"}),
            ("Glob", {"pattern": "**/*.py", "path": "/repo"}),
        ):
            with self.subTest(tool_name=tool_name):
                result = self.run_hook(tool_name, tool_input)

                self.assertEqual(result.returncode, 0, result.stderr)
                decision = json.loads(result.stdout)["hookSpecificOutput"]
                self.assertEqual(decision["permissionDecision"], "deny")

    def test_shell_cat_sed_and_grep_reads_are_refused(self):
        for command in (
            "cat /repo/ticket.md",
            "sed -n '1,80p' /repo/ticket.md",
            "grep needle /repo/ticket.md",
            "git status\ncat /repo/ticket.md",
        ):
            with self.subTest(command=command):
                result = self.run_hook("Bash", {"command": command})

                self.assertEqual(result.returncode, 0, result.stderr)
                decision = json.loads(result.stdout)["hookSpecificOutput"]
                self.assertEqual(decision["permissionDecision"], "deny")

    def test_the_other_named_shell_read_shapes_are_refused(self):
        for command in (
            "head -80 /repo/ticket.md",
            "tail -80 /repo/ticket.md",
            "env LC_ALL=C rg needle /repo",
            "/usr/bin/find /repo -name '*.md'",
            "git status && /bin/ls /repo",
        ):
            with self.subTest(command=command):
                result = self.run_hook("Bash", {"command": command})

                self.assertEqual(result.returncode, 0, result.stderr)
                decision = json.loads(result.stdout)["hookSpecificOutput"]
                self.assertEqual(decision["permissionDecision"], "deny")

    def test_nested_shell_and_control_flow_read_shapes_are_refused(self):
        for command in (
            "echo `cat /repo/ticket.md`",
            'bash -c "cat /repo/ticket.md"',
            "for f in /repo/*; do cat $f; done",
            "if true; then cat /repo/ticket.md; fi",
        ):
            with self.subTest(command=command):
                result = self.run_hook("Bash", {"command": command})

                self.assertEqual(result.returncode, 0, result.stderr)
                decision = json.loads(result.stdout)["hookSpecificOutput"]
                self.assertEqual(decision["permissionDecision"], "deny")

    def test_non_reading_bash_commands_are_allowed(self):
        for command in (
            "git status",
            "python3 /plugin/driver.py answer --run-dir /run --ticket 133 --text ready",
            "git commit -m 'support cat output'",
            "ls -d /repo",
        ):
            with self.subTest(command=command):
                result = self.run_hook("Bash", {"command": command})

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "")

    def test_tools_that_do_not_read_files_pass_through(self):
        result = self.run_hook("SendMessage", {"to": "child", "message": "continue"})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_another_session_using_the_same_settings_is_not_bounded(self):
        result = self.run_hook(
            "Grep",
            {"pattern": "needle", "path": "/repo"},
            session_id="9d1f4c2a-0000-4000-8000-000000000999",
            configured_session_id=SESSION_ID,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
