#!/usr/bin/env python3
"""The witness CLI, exercised against the merge repair rung's Claude stand-in."""

import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest


PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[1]
TESTS_DIR = pathlib.Path(__file__).resolve().parent
ASSETS = PLUGIN_ROOT / "skills" / "crew" / "assets"
WITNESS = ASSETS / "witness.py"
sys.path.insert(0, str(ASSETS))
import witness as witness_module  # noqa: E402

MODEL = "claude-sonnet-5"
BUDGET_USD = "2"
BRIEF = (
    "src/check.py:12 — held — the cited guard is present\n"
    "#130 — contradicted — the ticket says the session is fresh\n"
    "ADR-0004 — missing — the ADR is absent from this fixture"
)
CHECK_OUTPUT = {
    "cited": [
        {
            "pointer": "src/check.py:12",
            "status": "held",
            "reason": "the cited guard is present",
        },
        {
            "pointer": "#130",
            "status": "contradicted",
            "reason": "the ticket says the session is fresh",
        },
        {
            "pointer": "ADR-0004",
            "status": "missing",
            "reason": "the ADR is absent from this fixture",
        },
    ],
    "uncited": [],
}
ASK_OUTPUT = {
    "claims": [
        {
            "claim": "Issue 154 requires the tracker body and authoritative comments",
            "pointers": ["#154"],
        },
    ],
}
ASK_BRIEF = "Issue 154 requires the tracker body and authoritative comments — #154"
STRUCTURED_FROM_BRIEF = object()


def check_output(brief):
    output = {"cited": [], "uncited": []}
    for line in brief.splitlines():
        target = "uncited" if line.startswith("uncited ") else "cited"
        shaped = line.removeprefix("uncited ")
        pointer, status, reason = shaped.split(" — ", 2)
        output[target].append({"pointer": pointer, "status": status, "reason": reason})
    return output


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


class WitnessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        git(self.worktree, "init", "-b", "main")
        git(self.worktree, "config", "user.email", "crew@example.invalid")
        git(self.worktree, "config", "user.name", "Crew Test")
        (self.worktree / "src").mkdir()
        (self.worktree / "src" / "check.py").write_text("guard = True\n", encoding="utf-8")
        git(self.worktree, "add", "-A")
        git(self.worktree, "commit", "-m", "base")

        self.escalation = self.root / "escalation.txt"
        self.escalation.write_text(
            "CREW ASK 132 design — check src/check.py:12, #130 and ADR-0004",
            encoding="utf-8",
        )
        self.stub_dir = self.root / "stub"
        self.stub_dir.mkdir()
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        claude = self.bin_dir / "claude"
        claude.write_text(
            "#!/bin/sh\nexec %s %s \"$@\"\n"
            % (sys.executable, TESTS_DIR / "stub_claude_repair.py"),
            encoding="utf-8",
        )
        claude.chmod(0o755)
        gh = self.bin_dir / "gh"
        gh.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$*\" >> \"$AGENTCREW_STUB_DIR/gh-calls\"\n"
            "printf '%s\\n' \"$AGENTCREW_STUB_GH_ISSUE_154\"\n",
            encoding="utf-8",
        )
        gh.chmod(0o755)

        self.run_dir = self.root / "run"
        self.run_dir.mkdir()
        self.state_dir = self.run_dir / ".crew"
        self.state_dir.mkdir()
        self.ask_account = self.root / "ask-account"
        self.ask_account.mkdir()
        self.ask_worktree = self.root / ".claude" / "worktrees" / "154-witness"
        self.ask_worktree.mkdir(parents=True)
        git(self.ask_worktree, "init", "-b", "main")
        git(self.ask_worktree, "config", "user.email", "crew@example.invalid")
        git(self.ask_worktree, "config", "user.name", "Crew Test")
        (self.ask_worktree / "README.md").write_text("ask fixture\n", encoding="utf-8")
        git(self.ask_worktree, "add", "-A")
        git(self.ask_worktree, "commit", "-m", "base")
        spec = self.root / "spec.md"
        ticket = self.root / "154-witness.md"
        spec.write_text("# Witness\n", encoding="utf-8")
        ticket.write_text("# Ticket 154\n", encoding="utf-8")
        (self.state_dir / "wave-table.json").write_text(
            json.dumps({
                "run": {
                    "repo_root": str(self.root),
                    "crew_worktree": str(self.worktree),
                    "spec_path": str(spec),
                    "integration_branch": "crew/witness",
                    "integration_base_commit": "a" * 40,
                    "coordinator_name": "advisor",
                    "coordinator_pid": 1234,
                    "crew_skill_dir": str(ASSETS.parent),
                    "tmux_session": "$1:",
                    "permission_mode": "acceptEdits",
                    "coordinator_config_home": str(self.root / "coordinator-account"),
                    "repair_model": MODEL,
                    "witness_model": MODEL,
                    "witness_budget_usd": 3.5,
                    "tracker": "local",
                },
                "waves": [{
                    "wave": 1,
                    "tickets": [{
                        "id": "154",
                        "title": "Witness",
                        "path": str(ticket),
                        "workflow": "direct",
                        "executor": "claude",
                        "model": "claude-opus-5",
                        "effort": "medium",
                        "account": str(self.ask_account),
                        "account_mode": "explicit",
                        "blocked_by": [],
                        "slug": "witness",
                    }],
                }],
            }),
            encoding="utf-8",
        )

    def test_pointer_list_normalises_real_ticket_spellings_without_guessing(self):
        run_plan_path = self.worktree / "skills" / "crew" / "assets" / "run_plan.py"
        driver_path = self.worktree / "skills" / "crew" / "assets" / "driver" / "driver.py"
        outside_path = pathlib.Path.home() / ".claude" / "state" / "review.md"
        escalation = (
            f"ticket {self.root / '175.md'}，branch worktree-175-175，事实 #138，"
            f"{run_plan_path}:48，skills/crew/assets/run_plan.py:48，"
            "…run_plan.py:578，…:645，:646；"
            "中文skills/crew/assets/driver/driver.py:811，ADR-0018，"
            f"~/.claude/state/review.md:40，{outside_path}:40，"
            "skills/crew/assets/run_plan.py"
        )

        result = witness_module.pointers(escalation, self.worktree)

        self.assertTrue(all(isinstance(pointer, witness_module.Pointer) for pointer in result))
        self.assertEqual(
            [str(pointer) for pointer in result],
            [
                "#138",
                "skills/crew/assets/run_plan.py:48",
                "skills/crew/assets/run_plan.py:578",
                "skills/crew/assets/run_plan.py:645",
                "skills/crew/assets/run_plan.py:646",
                "skills/crew/assets/driver/driver.py:811",
                "ADR-0018",
                f"{outside_path}:40",
            ],
        )

    def test_an_ambiguous_or_unknown_elided_path_stays_unresolved(self):
        escalation = (
            "a/run_plan.py:1，b/run_plan.py:2，…run_plan.py:3，"
            "…unknown.py:4，…:5"
        )

        result = witness_module.pointers(escalation, self.worktree)

        self.assertEqual(
            [str(pointer) for pointer in result],
            [
                "a/run_plan.py:1",
                "b/run_plan.py:2",
                "run_plan.py:3",
                "unknown.py:4",
                "unknown.py:5",
            ],
        )

    def run_witness(
        self, behaviour="witness", *extra, stdin=None, brief=BRIEF, operation="check",
        structured_output=STRUCTURED_FROM_BRIEF, prose=None, issue=None,
    ):
        environment = dict(os.environ)
        environment["PATH"] = f"{self.bin_dir}{os.pathsep}{environment['PATH']}"
        environment["AGENTCREW_STUB_DIR"] = str(self.stub_dir)
        environment["AGENTCREW_STUB_REPAIR"] = behaviour
        environment["AGENTCREW_STUB_WITNESS_BRIEF"] = brief
        if structured_output is STRUCTURED_FROM_BRIEF:
            structured_output = check_output(brief)
        if structured_output is not None:
            environment["AGENTCREW_STUB_WITNESS_OUTPUT"] = json.dumps(structured_output)
        if prose is not None:
            environment["AGENTCREW_STUB_WITNESS_PROSE"] = prose
        if issue is not None:
            environment["AGENTCREW_STUB_GH_ISSUE_154"] = json.dumps(issue)
        command = [
            sys.executable,
            str(WITNESS),
            *([operation] if operation else []),
            "--escalation",
            "-" if stdin is not None else str(self.escalation),
            "--worktree",
            str(self.worktree),
            "--model",
            MODEL,
            "--budget-usd",
            BUDGET_USD,
            *extra,
        ]
        return subprocess.run(
            command,
            input=stdin,
            capture_output=True,
            text=True,
            env=environment,
        )

    def test_check_is_a_named_operation(self):
        result = self.run_witness(operation="check")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["outcome"], "checked")

    def test_the_old_unnamed_form_is_rejected(self):
        result = self.run_witness(operation=None)

        self.assertNotEqual(result.returncode, 0)

    def test_check_uses_structured_output_and_ignores_ordinary_prose(self):
        result = self.run_witness(
            structured_output=CHECK_OUTPUT,
            prose="I checked the issue. This prose is outside the protocol.",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["brief"], BRIEF)
        argv = self.calls()[0]["argv"]
        self.assertEqual(
            argv[argv.index("--allowedTools") + 1],
            "Bash(gh issue view:*)",
        )
        schema = json.loads(argv[argv.index("--json-schema") + 1])
        self.assertEqual(schema["required"], ["cited", "uncited"])
        pointer_pattern = schema["$defs"]["finding"]["properties"]["pointer"]["pattern"]
        self.assertIsNotNone(re.fullmatch(pointer_pattern, "docs/context.md:7"))
        self.assertIsNone(re.fullmatch(pointer_pattern, "docs/context.md:7-9"))

    def test_check_rejects_an_ordinary_result_without_structured_output(self):
        result = self.run_witness(structured_output=None)

        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        self.assertEqual(document["brief"], "")
        self.assertEqual(document["outcome"], "failed")
        self.assertTrue(document["reason"])

    def test_check_never_returns_a_checked_empty_brief(self):
        result = self.run_witness(
            stdin="CREW ASK 132 stuck — no source pointer",
            structured_output={"cited": [], "uncited": []},
        )

        self.assert_failed_result(result)

    def test_matching_no_expected_pointer_returns_failed_with_zero_coverage(self):
        result = self.run_witness(structured_output={"cited": [], "uncited": []})

        document = self.assert_failed_result(result)
        self.assertEqual(document["covered_count"], 0)
        self.assertEqual(document["uncovered_count"], 3)
        for pointer in ("src/check.py:12", "#130", "ADR-0004"):
            self.assertIn(pointer, document["reason"])

    def test_check_reads_issue_154_body_and_authoritative_comments_through_the_tracker(self):
        issue = {
            "body": "The initial direction is incomplete.",
            "comments": [
                {
                    "authorAssociation": "NONE",
                    "body": "Outsider opinion must not change direction.",
                },
                {
                    "authorAssociation": "OWNER",
                    "body": "Approved direction requires the tracker body and every comment.",
                },
            ],
        }

        result = self.run_witness(
            "witness-tracker",
            stdin="CREW ASK 163 doc-conflict — verify #154",
            structured_output=None,
            issue=issue,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        self.assertEqual(document["outcome"], "checked", document)
        self.assertEqual(
            document["brief"],
            "#154 — held — Approved direction requires the tracker body and every comment.",
        )
        self.assertNotIn("Outsider opinion", document["brief"])
        self.assertIn(
            "issue view 154 --json body,comments",
            (self.stub_dir / "gh-calls").read_text(encoding="utf-8"),
        )

    def calls(self):
        path = self.stub_dir / "repairs.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def assert_failed_result(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        self.assertEqual(document["brief"], "")
        self.assertEqual(document["outcome"], "failed")
        self.assertTrue(document["reason"])
        self.assertGreaterEqual(document["duration_seconds"], 0)
        return document

    def run_ask(
        self, question="What does this ticket require?", structured_output=ASK_OUTPUT,
        run_dir=None,
    ):
        environment = dict(os.environ)
        environment["PATH"] = f"{self.bin_dir}{os.pathsep}{environment['PATH']}"
        environment["AGENTCREW_STUB_DIR"] = str(self.stub_dir)
        environment["AGENTCREW_STUB_REPAIR"] = "witness"
        environment["AGENTCREW_STUB_WITNESS_BRIEF"] = BRIEF
        if structured_output is not None:
            environment["AGENTCREW_STUB_WITNESS_OUTPUT"] = json.dumps(structured_output)
        return subprocess.run(
            [
                sys.executable,
                str(WITNESS),
                "ask",
                "--run",
                str(run_dir or self.run_dir),
                "--ticket",
                "154",
                "--question",
                question,
            ],
            capture_output=True,
            text=True,
            env=environment,
        )

    def test_ask_resolves_its_execution_context_from_the_run_plan(self):
        result = self.run_ask()

        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        self.assertEqual(document["brief"], ASK_BRIEF, document)
        calls = self.calls()
        self.assertEqual(len(calls), 1, calls)
        call = calls[0]
        self.assertEqual(pathlib.Path(call["cwd"]).resolve(), self.ask_worktree.resolve())
        self.assertEqual(call["env"]["CLAUDE_CONFIG_DIR"], str(self.ask_account))
        argv = call["argv"]
        prompt = argv[argv.index("--print") + 1]
        self.assertIn("#154", prompt)
        self.assertIn("What does this ticket require?", prompt)
        self.assertEqual(argv[argv.index("--model") + 1], MODEL)
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "3.5")
        schema = json.loads(argv[argv.index("--json-schema") + 1])
        self.assertEqual(schema["required"], ["claims"])

    def test_ask_keeps_accepting_the_state_directory_form(self):
        result = self.run_ask(run_dir=self.state_dir)

        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        self.assertEqual(document["outcome"], "checked", document)
        self.assertEqual(document["brief"], ASK_BRIEF)

    def test_ask_returns_a_failed_envelope_for_a_wrong_run_directory(self):
        run_dir = self.root / "missing-run"

        result = self.run_ask(run_dir=run_dir)

        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        self.assertEqual(document["outcome"], "failed", document)
        self.assertIn(
            str(run_dir / ".crew" / "wave-table.json"), document["reason"]
        )
        self.assertIn("<feature-dir>/.crew", document["reason"])

    def test_ask_rejects_a_pointer_repeated_across_claims(self):
        result = self.run_ask(structured_output={
            "claims": [
                {"claim": "The issue is open", "pointers": ["#154"]},
                {"claim": "The issue has an owner ruling", "pointers": ["#154"]},
            ],
        })

        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        self.assertEqual(document["brief"], "")
        self.assertEqual(document["outcome"], "failed")
        self.assertIn("repeat", document["reason"])

    def test_ask_rejects_empty_questions_answers_and_uncited_or_malformed_claims(self):
        cases = (
            ("", ASK_OUTPUT),
            ("What changed?", {"claims": []}),
            ("What changed?", {"claims": [{"claim": "", "pointers": ["#154"]}]}),
            ("What changed?", {"claims": [{"claim": "A fact", "pointers": []}]}),
            (
                "What changed?",
                {"claims": [{"claim": "A fact", "pointers": ["not-a-pointer"]}]},
            ),
        )
        for question, structured_output in cases:
            with self.subTest(question=question, structured_output=structured_output):
                self.assert_failed_result(self.run_ask(question, structured_output))

    def test_a_checked_escalation_returns_one_line_per_cited_pointer(self):
        result = self.run_witness()

        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        self.assertEqual(document["brief"], BRIEF)
        self.assertEqual(document["outcome"], "checked")
        self.assertEqual(document["reason"], "")
        self.assertEqual(document["covered_count"], 3)
        self.assertEqual(document["uncovered_count"], 0)
        self.assertGreaterEqual(document["duration_seconds"], 0)
        self.assertEqual(
            document["usage"],
            {
                "input_tokens": 11,
                "output_tokens": 22,
                "cache_read_input_tokens": 33,
                "cache_creation_input_tokens": 44,
            },
        )

    def test_check_renders_the_numbered_normalised_pointer_list_into_the_prompt(self):
        result = self.run_witness()

        self.assertEqual(result.returncode, 0, result.stderr)
        prompt = self.calls()[0]["argv"][1]
        self.assertIn(
            "1. src/check.py:12\n2. #130\n3. ADR-0004",
            prompt,
        )
        self.assertNotIn("<check pointers>", prompt)

    def test_omitting_two_expected_pointers_returns_a_partial_brief_and_coverage(self):
        expected = [f"src/check.py:{line}" for line in range(1, 13)]
        escalation = "CREW ASK 132 design — " + "，".join(expected)
        cited = [
            {"pointer": pointer, "status": "held", "reason": f"fact {number}"}
            for number, pointer in enumerate(expected[:10], 1)
        ]

        result = self.run_witness(
            stdin=escalation,
            structured_output={"cited": cited, "uncited": []},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        self.assertEqual(document["outcome"], "partial")
        self.assertEqual(document["covered_count"], 10)
        self.assertEqual(document["uncovered_count"], 2)
        self.assertEqual(document["brief"].splitlines(), [
            f"{pointer} — held — fact {number}"
            for number, pointer in enumerate(expected[:10], 1)
        ])
        self.assertIn("src/check.py:11", document["reason"])
        self.assertIn("src/check.py:12", document["reason"])

    def test_the_session_is_headless_budget_capped_read_only_and_in_the_worktree(self):
        result = self.run_witness()

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertEqual(len(calls), 1, calls)
        argv = calls[0]["argv"]
        self.assertIn("--print", argv)
        self.assertEqual(argv[argv.index("--model") + 1], MODEL)
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], BUDGET_USD)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "plan")
        self.assertEqual(pathlib.Path(calls[0]["cwd"]).resolve(), self.worktree.resolve())

    def test_stdin_is_the_second_documented_escalation_source(self):
        escalation = "CREW ASK 132 stuck — ADR-0004 is the only pointer"

        result = self.run_witness(stdin=escalation)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            any(escalation in argument for argument in self.calls()[0]["argv"]),
            self.calls()[0]["argv"],
        )

    def test_a_nonzero_session_returns_an_empty_failed_brief_on_zero(self):
        result = self.run_witness("fail")

        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        self.assertEqual(document["brief"], "")
        self.assertEqual(document["outcome"], "failed")
        self.assertTrue(document["reason"])

    def test_permission_denial_outer_json_and_claude_errors_use_the_failed_envelope(self):
        for behaviour in ("permission-denied", "witness-invalid-json", "witness-error"):
            with self.subTest(behaviour=behaviour):
                self.assert_failed_result(self.run_witness(behaviour))

    def test_an_overrun_returns_an_empty_failed_brief_on_zero(self):
        result = self.run_witness("witness-timeout", "--timeout-seconds", "0.05")

        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        self.assertEqual(document["brief"], "")
        self.assertEqual(document["outcome"], "failed")
        self.assertIn("time", document["reason"].lower())

    def test_a_session_that_changes_the_tree_returns_an_empty_failed_brief(self):
        result = self.run_witness("witness-write")

        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        self.assertEqual(document["brief"], "")
        self.assertEqual(document["outcome"], "failed")
        self.assertTrue(document["reason"])

    def test_tree_changes_fail_closed_after_process_failure_and_timeout(self):
        for behaviour, extra in (
            ("witness-fail-write", ()),
            ("witness-timeout-write", ("--timeout-seconds", "0.05")),
        ):
            with self.subTest(behaviour=behaviour):
                document = self.assert_failed_result(self.run_witness(behaviour, *extra))
                self.assertIn("changed the worktree", document["reason"])

    def test_partial_and_absent_usage_do_not_discard_a_checked_result(self):
        partial = self.run_witness("witness-partial-usage")
        absent = self.run_witness("witness-no-usage")

        self.assertEqual(json.loads(partial.stdout)["outcome"], "checked")
        self.assertEqual(
            json.loads(partial.stdout)["usage"],
            {
                "input_tokens": 11,
                "output_tokens": 22,
                "cache_read_input_tokens": 33,
            },
        )
        self.assertEqual(json.loads(absent.stdout)["outcome"], "checked")
        self.assertNotIn("usage", json.loads(absent.stdout))

    def test_rewriting_an_already_dirty_file_is_still_a_tree_change(self):
        (self.worktree / "src" / "check.py").write_text(
            "the child already changed it\n", encoding="utf-8"
        )

        result = self.run_witness("witness-rewrite")

        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        self.assertEqual(document["brief"], "")
        self.assertEqual(document["outcome"], "failed")
        self.assertTrue(document["reason"])

    def test_an_uncited_pointer_uses_the_fixed_uncited_line_shape(self):
        brief = BRIEF + "\nuncited docs/context.md:7 — held — this fact also needs context"

        result = self.run_witness(brief=brief)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["brief"], brief)

    def test_check_degrades_out_of_order_findings_to_the_largest_ordered_partial(self):
        out_of_order = json.loads(json.dumps(CHECK_OUTPUT))
        out_of_order["cited"].reverse()

        result = self.run_witness(structured_output=out_of_order)

        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        self.assertEqual(document["outcome"], "partial")
        self.assertEqual(
            document["brief"],
            "src/check.py:12 — held — the cited guard is present",
        )
        self.assertEqual(document["covered_count"], 1)
        self.assertEqual(document["uncovered_count"], 2)
        self.assertIn("structural rejection (out of order): #130", document["reason"])
        self.assertIn("structural rejection (out of order): ADR-0004", document["reason"])
        self.assertNotIn("uncovered pointers", document["reason"])

    def test_a_duplicate_expected_finding_is_structurally_rejected_from_a_partial(self):
        duplicate = json.loads(json.dumps(CHECK_OUTPUT))
        duplicate["cited"].append(dict(duplicate["cited"][-1]))

        result = self.run_witness(structured_output=duplicate)

        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        self.assertEqual(document["outcome"], "partial")
        self.assertEqual(document["covered_count"], 2)
        self.assertEqual(document["uncovered_count"], 1)
        self.assertNotIn("ADR-0004 —", document["brief"])
        self.assertIn("structural rejection (repeated): ADR-0004", document["reason"])
        self.assertNotIn("uncovered pointers", document["reason"])

    def test_an_expected_pointer_repeated_as_uncited_is_rejected_from_a_partial(self):
        repeated_uncited = json.loads(json.dumps(CHECK_OUTPUT))
        repeated_uncited["uncited"] = [dict(repeated_uncited["cited"][0])]

        result = self.run_witness(structured_output=repeated_uncited)

        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        self.assertEqual(document["outcome"], "partial")
        self.assertEqual(document["covered_count"], 2)
        self.assertEqual(document["uncovered_count"], 1)
        self.assertNotIn("src/check.py:12 —", document["brief"])
        self.assertIn(
            "structural rejection (repeated): src/check.py:12",
            document["reason"],
        )

    def test_an_extra_cited_pointer_becomes_uncited_in_a_structural_partial(self):
        extra = json.loads(json.dumps(CHECK_OUTPUT))
        extra["cited"].append({
            "pointer": "docs/context.md:7",
            "status": "held",
            "reason": "the extra context exists",
        })

        result = self.run_witness(structured_output=extra)

        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        self.assertEqual(document["outcome"], "partial")
        self.assertEqual(document["covered_count"], 3)
        self.assertEqual(document["uncovered_count"], 0)
        self.assertEqual(
            document["brief"],
            BRIEF + "\nuncited docs/context.md:7 — held — the extra context exists",
        )
        self.assertIn(
            "structural rejection (extra cited): docs/context.md:7",
            document["reason"],
        )

    def test_pointer_free_escalation_keeps_an_uncited_finding_as_checked(self):
        structured_output = {
            "cited": [],
            "uncited": [{
                "pointer": "#200",
                "status": "held",
                "reason": "the follow-up ticket exists",
            }],
        }

        result = self.run_witness(
            stdin="CREW ASK 132 wrap-up — place the remaining follow-up ts=1",
            structured_output=structured_output,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        self.assertEqual(document["outcome"], "checked")
        self.assertEqual(
            document["brief"],
            "uncited #200 — held — the follow-up ticket exists",
        )
        self.assertEqual(document["covered_count"], 0)
        self.assertEqual(document["uncovered_count"], 0)

    def test_a_nonpointer_line_cannot_pose_as_an_uncited_pointer(self):
        brief = BRIEF + "\ntotal garbage — held — anything"

        result = self.run_witness(brief=brief)

        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        self.assertEqual(document["brief"], "")
        self.assertEqual(document["outcome"], "failed")

    def test_a_time_is_not_mistaken_for_a_path_and_line_pointer(self):
        escalation = "CREW ASK 132 stuck — at 09:30 check src/check.py:12"
        brief = "src/check.py:12 — held — the cited guard is present"

        result = self.run_witness(stdin=escalation, brief=brief)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["brief"], brief)

    def test_a_numeric_or_version_token_is_not_a_path_and_line_pointer(self):
        brief = "src/check.py:12 — held — the cited guard is present"
        for token in ("2.0:1", "v1.2:34", "4-2:1"):
            with self.subTest(token=token):
                escalation = f"CREW ASK 132 stuck — {token} check src/check.py:12"

                result = self.run_witness(stdin=escalation, brief=brief)

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["brief"], brief)

    def test_each_documented_path_shape_is_a_path_and_line_pointer(self):
        for pointer in (
            "README:12",
            "src/check.py:12",
            ".github/ci.yml:3",
            "_init.py:1",
        ):
            with self.subTest(pointer=pointer):
                escalation = f"CREW ASK 132 stuck — check {pointer}"
                brief = f"{pointer} — held — the cited location is present"

                result = self.run_witness(stdin=escalation, brief=brief)

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["brief"], brief)


if __name__ == "__main__":
    unittest.main()
