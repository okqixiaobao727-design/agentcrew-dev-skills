#!/usr/bin/env python3
"""End-to-end tests for claude_review_bridge.py against stub_claude.py.

The bridge is run as a subprocess with a stub `claude` binary, so every
assertion is about external behaviour only: the argv the stub was handed, the
JSON the bridge emits, and the state and log files it leaves behind.
"""

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from hook_fixtures import marker_lines, write_hook_config  # noqa: E402


TESTS_DIR = pathlib.Path(__file__).resolve().parent
BRIDGE_PATH = TESTS_DIR.parent / "scripts" / "claude_review_bridge.py"
STUB_PATH = TESTS_DIR / "stub_claude.py"


class BridgeRun:
    def __init__(self, completed, argv_log, state_dir, cwd):
        self.completed = completed
        self.argv_log = argv_log
        self.state_dir = state_dir
        self.cwd = cwd

    @property
    def returncode(self):
        return self.completed.returncode

    @property
    def stderr(self):
        return self.completed.stderr

    @property
    def output(self):
        return json.loads(self.completed.stdout)

    @property
    def invocations(self):
        if not self.argv_log.exists():
            return []
        return [
            json.loads(line)
            for line in self.argv_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def state(self, lineage_id):
        return json.loads(
            (self.state_dir / f"{lineage_id}.json").read_text(encoding="utf-8")
        )

    def log(self, lineage_id):
        return (self.state_dir / f"{lineage_id}.log").read_text(encoding="utf-8")


class BridgeTestCase(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.work.name)
        self.cwd = root / "worktree"
        self.cwd.mkdir()
        self.state_dir = root / "state"
        self.argv_log = root / "stub-argv.jsonl"
        self.addCleanup(self.work.cleanup)

    def run_bridge(
        self,
        *arguments,
        scenario="ok",
        binary=None,
        path_bin=None,
        env=None,
        watch_env=(),
    ):
        environment = dict(os.environ)
        environment.update(env or {})
        environment["CLAUDE_STUB_SCENARIO"] = scenario
        environment["CLAUDE_STUB_ARGV_LOG"] = str(self.argv_log)
        environment["CLAUDE_STUB_ENV_KEYS"] = ",".join(watch_env)
        if path_bin is not None:
            environment["PATH"] = f"{path_bin}{os.pathsep}{environment['PATH']}"
        command = [
            sys.executable,
            str(BRIDGE_PATH),
            "--cwd",
            str(self.cwd),
            "--state-dir",
            str(self.state_dir),
        ]
        if binary is not False:
            command.extend(["--claude-binary", str(binary or STUB_PATH)])
        command.extend(arguments)
        completed = subprocess.run(
            command,
            env=environment,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        return BridgeRun(completed, self.argv_log, self.state_dir, self.cwd)


class FirstRoundTests(BridgeTestCase):
    def test_first_call_builds_the_headless_argv(self):
        run = self.run_bridge(
            "HEAD~1..HEAD", "--model", "opus", "--effort", "medium"
        )

        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(len(run.invocations), 1)
        argv = run.invocations[0]["argv"]
        self.assertEqual(argv[0], str(STUB_PATH.resolve()))
        self.assertEqual(
            argv[1:-1],
            [
                "-p",
                "--output-format",
                "json",
                "--permission-mode",
                "bypassPermissions",
                "--model",
                "opus",
                "--effort",
                "medium",
            ],
        )
        self.assertIn("HEAD~1..HEAD", argv[-1])
        self.assertNotIn("-r", argv)

    def test_the_cwd_reaches_the_subprocess(self):
        run = self.run_bridge("HEAD")

        self.assertEqual(
            pathlib.Path(run.invocations[0]["cwd"]).resolve(), self.cwd.resolve()
        )

    def test_model_and_effort_are_omitted_when_unset(self):
        run = self.run_bridge("HEAD")

        argv = run.invocations[0]["argv"]
        self.assertNotIn("--model", argv)
        self.assertNotIn("--effort", argv)

    def test_first_call_returns_findings_and_a_session_id(self):
        run = self.run_bridge("HEAD")

        output = run.output
        self.assertEqual(output["status"], "completed")
        self.assertEqual(output["sessionId"], "stub-session-0001")
        self.assertEqual(output["lineageId"], "stub-session-0001")
        self.assertFalse(output["resumed"])
        self.assertEqual(output["round"], 1)
        self.assertIn("Standards:", output["findings"])
        self.assertEqual(output["permissionDenials"], [])

    def test_state_and_log_files_are_written(self):
        run = self.run_bridge("HEAD", "--model", "opus", "--effort", "medium")

        state = run.state("stub-session-0001")
        self.assertEqual(state["version"], 1)
        self.assertEqual(state["sessionId"], "stub-session-0001")
        self.assertEqual(state["target"], "HEAD")
        self.assertEqual(state["model"], "opus")
        self.assertEqual(state["effort"], "medium")
        self.assertEqual(state["permissionMode"], "bypassPermissions")
        self.assertEqual(state["rounds"], 1)
        self.assertEqual(state["lastResult"], run.output["findings"])
        self.assertEqual(state["permissionDenials"], [])
        self.assertEqual(
            pathlib.Path(state["cwd"]).resolve(), self.cwd.resolve()
        )

        state_path = self.state_dir / "stub-session-0001.json"
        self.assertEqual(state_path.stat().st_mode & 0o777, 0o600)

        log = run.log("stub-session-0001")
        self.assertIn("stub claude: starting headless review", log)
        self.assertIn("stub-session-0001", log)
        self.assertEqual(run.output["logFile"], str(state_path.with_suffix(".log")))

    def test_real_binary_is_used_when_path_holds_a_wrapper(self):
        wrapper_dir = pathlib.Path(self.work.name) / "bin"
        wrapper_dir.mkdir()
        (wrapper_dir / "claude").symlink_to(STUB_PATH)

        run = self.run_bridge("HEAD", binary=False, path_bin=wrapper_dir)

        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.invocations[0]["argv"][0], str(STUB_PATH.resolve()))


class LaunchHookTests(BridgeTestCase):
    """The review child is a child launch, so the project's hook covers it too.

    A project that configures no hook gets what it got before the hook existed:
    the reviewer inherits the caller's environment and nothing is called.
    """

    TAG = "CREW_LAUNCH_TAG"

    def setUp(self):
        super().setUp()
        self.marker = pathlib.Path(self.work.name) / "launched.log"

    def marker_lines(self):
        return marker_lines(self.marker)

    def test_an_unconfigured_hook_launches_the_reviewer_untouched(self):
        run = self.run_bridge("HEAD", watch_env=[self.TAG])

        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIsNone(run.invocations[0]["env"][self.TAG])
        self.assertEqual(self.marker_lines(), [])

    def test_the_configured_variables_reach_the_reviewer(self):
        write_hook_config(self.cwd, env={self.TAG: "ticket-133"})

        run = self.run_bridge("HEAD", watch_env=[self.TAG])

        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.invocations[0]["env"][self.TAG], "ticket-133")

    def test_the_command_runs_once_for_every_reviewer_launched(self):
        write_hook_config(self.cwd, command=f"echo launched >> {self.marker}")

        first = self.run_bridge("HEAD")
        self.run_bridge("HEAD", "--resume-session", first.output["lineageId"])

        self.assertEqual(self.marker_lines(), ["launched", "launched"])


class DeSkilledPromptTests(BridgeTestCase):
    """The prompt carries the review itself: no skill to resolve, no config to read.

    A headless reviewer that names a skill resolves that name a second time, and
    a reviewer-config override in its environment answers a lookup it should
    never make. Both are gone, so the prompt states the request and the Rounds
    contract outright.
    """

    SKILL_MARKERS = ("$code-review", "/code-review", "mattpocock-skills")

    def prompt_from(self, run):
        return run.invocations[-1]["argv"][-1]

    def test_first_prompt_names_no_skill(self):
        run = self.run_bridge("HEAD")

        prompt = self.prompt_from(run)
        for marker in self.SKILL_MARKERS:
            self.assertNotIn(marker, prompt)

    def test_first_prompt_states_the_request_and_the_rounds_contract(self):
        run = self.run_bridge("HEAD~1..HEAD")

        prompt = self.prompt_from(run)
        self.assertIn("HEAD~1..HEAD", prompt)
        self.assertIn("review-only task", prompt)
        self.assertIn("Rounds contract", prompt)
        self.assertIn("two axes", prompt)
        self.assertIn("standards", prompt)
        self.assertIn("spec", prompt)
        self.assertIn("one re-review", prompt)
        self.assertIn("both positions", prompt)

    def test_followup_prompt_names_no_skill_and_keeps_the_contract(self):
        first = self.run_bridge("HEAD")

        second = self.run_bridge(
            "HEAD", "--resume-session", first.output["lineageId"]
        )

        prompt = self.prompt_from(second)
        for marker in self.SKILL_MARKERS:
            self.assertNotIn(marker, prompt)
        self.assertIn("Rounds contract", prompt)
        self.assertIn("review-only task", prompt)

    def test_reviewer_config_override_does_not_reach_the_headless_reviewer(self):
        run = self.run_bridge(
            "HEAD", env={"CODE_REVIEWER_FILE": "/tmp/seam-that-must-not-travel"}
        )

        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIsNone(run.invocations[0]["reviewerFileEnv"])


class ResumeTests(BridgeTestCase):
    def test_followup_resumes_the_stored_lineage(self):
        first = self.run_bridge("HEAD", "--model", "opus", "--effort", "medium")
        lineage_id = first.output["lineageId"]

        second = self.run_bridge("HEAD", "--resume-session", lineage_id)

        self.assertEqual(second.returncode, 0, second.stderr)
        argv = second.invocations[1]["argv"]
        self.assertEqual(argv[argv.index("-r") + 1], lineage_id)
        self.assertIn(
            "say for each earlier finding whether it is now resolved", argv[-1]
        )

        output = second.output
        self.assertTrue(output["resumed"])
        self.assertEqual(output["round"], 2)
        self.assertEqual(output["lineageId"], lineage_id)
        self.assertIn("Round two", output["findings"])

    def test_followup_inherits_the_lineage_model_and_effort(self):
        first = self.run_bridge("HEAD", "--model", "opus", "--effort", "medium")
        lineage_id = first.output["lineageId"]

        second = self.run_bridge("HEAD", "--resume-session", lineage_id)

        argv = second.invocations[1]["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "opus")
        self.assertEqual(argv[argv.index("--effort") + 1], "medium")

    def test_both_rounds_append_to_one_log_and_one_state_file(self):
        first = self.run_bridge("HEAD")
        lineage_id = first.output["lineageId"]

        second = self.run_bridge("HEAD", "--resume-session", lineage_id)

        self.assertEqual(second.state(lineage_id)["rounds"], 2)
        self.assertEqual(second.log(lineage_id).count("===== round"), 2)
        self.assertEqual(
            sorted(path.name for path in self.state_dir.iterdir()),
            [f"{lineage_id}.json", f"{lineage_id}.log"],
        )

    def test_unknown_session_is_reported(self):
        run = self.run_bridge("HEAD", "--resume-session", "stub-session-9999")

        self.assertEqual(run.returncode, 1)
        self.assertIn("Unknown review session", run.stderr)

    def test_session_id_cannot_escape_the_state_directory(self):
        run = self.run_bridge("HEAD", "--resume-session", "../another-task")

        self.assertEqual(run.returncode, 1)
        self.assertIn("Invalid review session id", run.stderr)


class FailureSurfaceTests(BridgeTestCase):
    def test_permission_denials_are_surfaced_not_swallowed(self):
        run = self.run_bridge("HEAD", scenario="denials")

        output = run.output
        self.assertEqual(len(output["permissionDenials"]), 2)
        self.assertEqual(output["permissionDenials"][0]["tool_name"], "Bash")
        self.assertIn("permission denial", run.stderr)
        self.assertEqual(run.returncode, 1)
        self.assertEqual(
            run.state("stub-session-0001")["permissionDenials"],
            output["permissionDenials"],
        )

    def test_error_result_fails_the_call(self):
        run = self.run_bridge("HEAD", scenario="error")

        self.assertEqual(run.returncode, 1)
        self.assertEqual(run.output["status"], "error")

    def test_unparsable_output_is_reported_and_logged(self):
        run = self.run_bridge("HEAD", scenario="bad-json")

        self.assertEqual(run.returncode, 1)
        self.assertIn("not JSON", run.stderr)
        self.assertIn("Not JSON at all", (self.state_dir / "unparsed.log").read_text())

    def test_timeout_is_reported_and_leaves_no_lineage(self):
        run = self.run_bridge("HEAD", "--timeout", "1", scenario="hang")

        self.assertEqual(run.returncode, 1)
        self.assertIn("timed out", run.stderr)
        self.assertEqual(
            [path.name for path in self.state_dir.iterdir()], ["timed-out.log"]
        )

    def test_missing_session_id_is_reported(self):
        run = self.run_bridge("HEAD", scenario="no-session")

        self.assertEqual(run.returncode, 1)
        self.assertIn("no session_id", run.stderr)


if __name__ == "__main__":
    unittest.main()
