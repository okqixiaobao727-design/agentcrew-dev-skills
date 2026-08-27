#!/usr/bin/env python3
"""The witness CLI, exercised against the merge repair rung's Claude stand-in."""

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest


PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[1]
TESTS_DIR = pathlib.Path(__file__).resolve().parent
ASSETS = PLUGIN_ROOT / "skills" / "crew" / "assets"
WITNESS = ASSETS / "witness.py"
MODEL = "claude-sonnet-5"
BUDGET_USD = "2"
BRIEF = (
    "src/check.py:12 — held — the cited guard is present\n"
    "#130 — contradicted — the ticket says the session is fresh\n"
    "ADR-0004 — missing — the ADR is absent from this fixture"
)


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

    def run_witness(self, behaviour="witness", *extra, stdin=None, brief=BRIEF):
        environment = dict(os.environ)
        environment["PATH"] = f"{self.bin_dir}{os.pathsep}{environment['PATH']}"
        environment["AGENTCREW_STUB_DIR"] = str(self.stub_dir)
        environment["AGENTCREW_STUB_REPAIR"] = behaviour
        environment["AGENTCREW_STUB_WITNESS_BRIEF"] = brief
        command = [
            sys.executable,
            str(WITNESS),
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

    def calls(self):
        path = self.stub_dir / "repairs.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_a_checked_escalation_returns_one_line_per_cited_pointer(self):
        result = self.run_witness()

        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        self.assertEqual(document["brief"], BRIEF)
        self.assertEqual(document["outcome"], "checked")
        self.assertEqual(document["reason"], "")
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
