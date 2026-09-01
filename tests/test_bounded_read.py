#!/usr/bin/env python3
"""Drive the coordinator's bounded-read hook through its PreToolUse stdin boundary."""

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "skills" / "crew" / "assets"))

import bounded_read  # noqa: E402


SCRIPT = PLUGIN_ROOT / "skills" / "crew" / "assets" / "bounded_read.py"
CREW_DIR = PLUGIN_ROOT / "skills" / "crew"
RUN_DIR = PLUGIN_ROOT / "docs" / "research"
SESSION_ID = "9d1f4c2a-0000-4000-8000-000000000133"


class BoundedReadHookTests(unittest.TestCase):
    def run_hook(
        self, tool_name, tool_input, session_id=SESSION_ID, configured_session_id=None,
        run_dir=None,
    ):
        command = [sys.executable, str(SCRIPT), "hook", "--crew-dir", str(CREW_DIR)]
        if configured_session_id is not None:
            command += ["--session-id", configured_session_id]
        if run_dir is not None:
            command += ["--run-dir", str(run_dir)]
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

    def test_a_top_level_markdown_file_in_the_current_run_can_be_read_whole(self):
        result = self.run_hook(
            "Read",
            {"file_path": str(RUN_DIR / "cross-account-ask-channel.md"), "limit": 1000},
            run_dir=RUN_DIR,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_maintainer_judgment_markdown_can_be_read_whole(self):
        paths = (
            PLUGIN_ROOT / "docs/adr/0010-the-driver-runs-the-run-the-coordinator-rules.md",
            PLUGIN_ROOT / "docs/glossary.md",
            PLUGIN_ROOT / "CONTEXT.md",
            CREW_DIR / "SKILL.md",
            CREW_DIR / "references/triage.md",
            PLUGIN_ROOT / "references/trackers.md",
        )

        for path in paths:
            with self.subTest(path=path):
                result = self.run_hook(
                    "Read", {"file_path": str(path), "limit": 1000}, run_dir=RUN_DIR
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "")

    def test_a_plugin_document_is_admitted_without_becoming_a_repo_reference_entry(self):
        plugin_document = (CREW_DIR / "SKILL.md").resolve()

        allowed = self.run_hook(
            "Read", {"file_path": str(plugin_document), "limit": 1000}, run_dir=RUN_DIR
        )

        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertEqual(allowed.stdout, "")
        self.assertNotIn(plugin_document, bounded_read.reference_index_paths(RUN_DIR))

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
        self.assertIn("judgment Markdown", decision["permissionDecisionReason"])
        self.assertIn("explicit offset", decision["permissionDecisionReason"])
        self.assertIn("at most 80 lines", decision["permissionDecisionReason"])

    def test_a_read_over_the_eighty_line_limit_is_refused(self):
        result = self.run_hook(
            "Read",
            {"file_path": "/repo/ticket.md", "offset": 12, "limit": 81},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        decision = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")

    def test_source_under_the_crew_skill_directory_cannot_be_read_whole(self):
        path = CREW_DIR / "assets/bounded_read.py"

        result = self.run_hook("Read", {"file_path": str(path), "limit": 1000})

        self.assertEqual(result.returncode, 0, result.stderr)
        decision = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")

        bounded = self.run_hook(
            "Read", {"file_path": str(path), "offset": 12, "limit": 80}
        )

        self.assertEqual(bounded.returncode, 0, bounded.stderr)
        self.assertEqual(bounded.stdout, "")

    def test_unlisted_markdown_cannot_be_read_whole(self):
        paths = (
            PLUGIN_ROOT / "docs/design.md",
            RUN_DIR / "notes" / "decision.md",
            CREW_DIR / "README.md",
            PLUGIN_ROOT / "docs/agents/nested/convention.md",
        )

        for path in paths:
            with self.subTest(path=path):
                result = self.run_hook(
                    "Read", {"file_path": str(path), "limit": 1000}, run_dir=RUN_DIR
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                decision = json.loads(result.stdout)["hookSpecificOutput"]
                self.assertEqual(decision["permissionDecision"], "deny")

    def test_judgment_paths_are_compared_by_realpath(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = pathlib.Path(directory) / "repo"
            run_dir = repo / "crewtask" / "162"
            glossary = repo / "docs" / "glossary.md"
            tracker = repo / "docs" / "agents" / "issue-tracker.md"
            unlisted = repo / "docs" / "design.md"
            run_dir.mkdir(parents=True)
            glossary.parent.mkdir(parents=True)
            tracker.parent.mkdir(parents=True)
            glossary.write_text("# Glossary\n", encoding="utf-8")
            tracker.write_text("# Issue tracker\n", encoding="utf-8")
            unlisted.write_text("# Design\n", encoding="utf-8")
            alias = pathlib.Path(directory) / "repo-alias"
            alias.symlink_to(repo, target_is_directory=True)

            for path in (
                alias / "docs/glossary.md",
                alias / "docs/agents/issue-tracker.md",
            ):
                with self.subTest(path=path):
                    allowed = self.run_hook(
                        "Read",
                        {"file_path": str(path), "limit": 1000},
                        run_dir=alias / "crewtask" / "162",
                    )

                    self.assertEqual(allowed.returncode, 0, allowed.stderr)
                    self.assertEqual(allowed.stdout, "")

            escaped = run_dir / "spec.md"
            escaped.symlink_to(unlisted)
            denied = self.run_hook(
                "Read", {"file_path": str(escaped), "limit": 1000}, run_dir=run_dir
            )

            self.assertEqual(denied.returncode, 0, denied.stderr)
            decision = json.loads(denied.stdout)["hookSpecificOutput"]
            self.assertEqual(decision["permissionDecision"], "deny")

    def test_an_unresolvable_judgment_path_is_denied(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = pathlib.Path(directory) / "repo"
            run_dir = repo / "crewtask" / "162"
            run_dir.mkdir(parents=True)
            first = run_dir / "first.md"
            second = run_dir / "second.md"
            first.symlink_to(second)
            second.symlink_to(first)

            result = self.run_hook(
                "Read", {"file_path": str(first), "limit": 1000}, run_dir=run_dir
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            decision = json.loads(result.stdout)["hookSpecificOutput"]
            self.assertEqual(decision["permissionDecision"], "deny")

    def test_search_and_shell_reads_under_the_crew_skill_directory_are_hunts(self):
        cases = (
            ("Grep", {"pattern": "Contract", "path": str(CREW_DIR)}),
            ("Glob", {"pattern": "**/*.md", "path": str(CREW_DIR)}),
            ("Bash", {"command": f"cat {CREW_DIR / 'SKILL.md'}"}),
        )
        for tool_name, tool_input in cases:
            with self.subTest(tool_name=tool_name):
                result = self.run_hook(tool_name, tool_input)

                self.assertEqual(result.returncode, 0, result.stderr)
                decision = json.loads(result.stdout)["hookSpecificOutput"]
                self.assertEqual(decision["permissionDecision"], "deny")

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
            "gh issue view 162 --json number,title,body,labels,comments",
            "python3 /plugin/driver.py answer --run-dir /run --ticket 133 --text ready",
            "git commit -m 'support cat output'",
            "ls -d /repo",
        ):
            with self.subTest(command=command):
                result = self.run_hook("Bash", {"command": command})

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "")

    def test_prose_arguments_are_never_read_hunts(self):
        for command in (
            "python3 /plugin/driver.py answer --run-dir /run --ticket 44 --text "
            "$'It is settled by evidence, not by the reviewer\\'s note: run scripts/test.py'",
            'python3 /plugin/driver.py answer --run-dir /run --ticket 44 --text '
            '"Behaviour-pinning tests, characterization tests, and grep -R notes"',
            "python3 /plugin/driver.py answer --text 'Run `ls -la` first'",
            "gh issue comment 46 --body-file /tmp/x.md",
            "git commit -m 'support cat output'",
        ):
            with self.subTest(command=command):
                result = self.run_hook("Bash", {"command": command})

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "")

    def test_a_heredoc_fed_read_command_writes_rather_than_reads(self):
        command = (
            "gh issue comment 46 --body \"$(cat <<'EOF'\n"
            "A ruling with \" and ' and # and `ls -la` inside it.\n"
            "It cites sed -n '1,80p' docs/x.md as a pointer.\n"
            "EOF\n"
            ")\""
        )

        result = self.run_hook("Bash", {"command": command})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_a_comment_does_not_hide_a_later_read(self):
        result = self.run_hook("Bash", {"command": "ls -d /tmp # note\ncat /etc/passwd"})

        self.assertEqual(result.returncode, 0, result.stderr)
        decision = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")

    def test_a_hash_inside_a_word_does_not_start_a_comment(self):
        result = self.run_hook("Bash", {"command": "gh issue view '#176' --json body"})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_the_substitution_and_pipeline_read_shapes_are_refused(self):
        for command in (
            "x=$(cat /repo/ticket.md)",
            "cat /repo/ticket.md | grep needle",
            "cat <<EOF\n$(cat /etc/passwd)\nEOF",
        ):
            with self.subTest(command=command):
                result = self.run_hook("Bash", {"command": command})

                self.assertEqual(result.returncode, 0, result.stderr)
                decision = json.loads(result.stdout)["hookSpecificOutput"]
                self.assertEqual(decision["permissionDecision"], "deny")

    def test_a_redirected_or_default_directory_read_is_refused(self):
        for command in (
            "cat</etc/passwd",
            "cat <<EOF < /etc/passwd\nbody\nEOF",
            "ls <<'EOF'\nbody\nEOF",
        ):
            with self.subTest(command=command):
                result = self.run_hook("Bash", {"command": command})

                self.assertEqual(result.returncode, 0, result.stderr)
                decision = json.loads(result.stdout)["hookSpecificOutput"]
                self.assertEqual(decision["permissionDecision"], "deny")

    def test_an_arithmetic_expansion_is_not_a_command(self):
        result = self.run_hook("Bash", {"command": "echo $((head + 1))"})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_a_nested_shell_body_is_decoded_before_it_is_scanned(self):
        for command in (
            "bash -c $'cat\\x20/etc/passwd'",
            "bash -lc 'cat /etc/passwd'",
        ):
            with self.subTest(command=command):
                result = self.run_hook("Bash", {"command": command})

                self.assertEqual(result.returncode, 0, result.stderr)
                decision = json.loads(result.stdout)["hookSpecificOutput"]
                self.assertEqual(decision["permissionDecision"], "deny")

    def test_unbalanced_parentheses_fail_closed(self):
        for command in ("echo (", "echo )"):
            with self.subTest(command=command):
                result = self.run_hook("Bash", {"command": command})

                self.assertEqual(result.returncode, 0, result.stderr)
                decision = json.loads(result.stdout)["hookSpecificOutput"]
                self.assertEqual(decision["permissionDecision"], "deny")
                self.assertIn("could not be parsed", decision["permissionDecisionReason"])

    def test_a_substitution_inside_arithmetic_is_still_a_command(self):
        result = self.run_hook("Bash", {"command": "echo $(( $(cat /dev/null) + 1 ))"})

        self.assertEqual(result.returncode, 0, result.stderr)
        decision = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn("Matched `cat`", decision["permissionDecisionReason"])

    def test_an_escape_outside_unicode_fails_closed_rather_than_crashing(self):
        result = self.run_hook("Bash", {"command": "bash -c $'printf\\x20\\UFFFFFFFF'"})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        decision = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn("could not be parsed", decision["permissionDecisionReason"])

    def test_a_parameter_expansion_is_read_as_data(self):
        for command in ("echo ${x:-)}", 'gh issue view 176 --json body > "${TMPDIR}/x"'):
            with self.subTest(command=command):
                result = self.run_hook("Bash", {"command": command})

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "")

    def test_a_substitution_inside_a_parameter_expansion_is_still_a_command(self):
        result = self.run_hook("Bash", {"command": "echo ${x:-$(cat /etc/passwd)}"})

        self.assertEqual(result.returncode, 0, result.stderr)
        decision = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")

    def test_the_denial_names_the_token_it_matched(self):
        result = self.run_hook(
            "Bash", {"command": "python3 /plugin/driver.py answer --help 2>&1 | head -40"}
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        decision = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn(bounded_read.DENIAL_REASON, decision["permissionDecisionReason"])
        self.assertIn("Matched `head` in `head -40`.", decision["permissionDecisionReason"])

    def test_an_unparseable_command_says_so(self):
        result = self.run_hook("Bash", {"command": "echo 'unterminated"})

        self.assertEqual(result.returncode, 0, result.stderr)
        decision = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn("could not be parsed", decision["permissionDecisionReason"])

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
