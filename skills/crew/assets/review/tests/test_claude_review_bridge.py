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
GRAPH_STUB_PATH = TESTS_DIR / "stub_code_review_graph.py"


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


class MachineLogTests(BridgeTestCase):
    """The pair of `review` lines every review leaves in the run's machine log.

    The bridge is the writer because it is the only party that deterministically knows both that a
    review started and that it ended: the reviewed child may skip a line it was asked for in prose,
    and a child whose session dies mid-review can never write the `returned` line at all.
    """

    TICKET = "26"
    MODEL = "claude-opus-4-6-20260401"

    def setUp(self):
        super().setUp()
        self.machine_log = pathlib.Path(self.work.name) / "run" / "log.jsonl"

    def run_logged(self, *arguments, machine_log=None, **kwargs):
        return self.run_bridge(
            "HEAD",
            "--model", self.MODEL,
            "--machine-log", str(self.machine_log if machine_log is None else machine_log),
            "--ticket", self.TICKET,
            *arguments,
            **kwargs,
        )

    def reviews(self):
        if not self.machine_log.exists():
            return []
        return [
            json.loads(line)
            for line in self.machine_log.read_text(encoding="utf-8").splitlines()
            if line.strip() and json.loads(line).get("event") == "review"
        ]

    def assertPair(self, records):
        """Exactly one `running` line and its `returned` pair, for this ticket, in that order."""
        self.assertEqual([record["state"] for record in records], ["running", "returned"])
        for record in records:
            self.assertEqual(record["ticket"], self.TICKET)
            # Vendor then model, the spelling the dashboard's annotation row prints verbatim.
            self.assertEqual(record["lane"], f"claude {self.MODEL}")

    def test_a_review_leaves_its_running_line_and_its_returned_pair(self):
        run = self.run_logged()

        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertPair(self.reviews())

    def test_a_review_with_no_log_configured_writes_nothing_and_reports_the_same(self):
        logged = self.run_logged()
        self.machine_log.unlink()

        unlogged = self.run_bridge("HEAD", "--model", self.MODEL)

        self.assertEqual(unlogged.returncode, logged.returncode, unlogged.stderr)
        for key in ("status", "lineageId", "sessionId", "round", "findings"):
            self.assertEqual(unlogged.output[key], logged.output[key], key)
        self.assertFalse(self.machine_log.exists())

    def test_a_review_told_a_ticket_but_no_log_writes_nothing(self):
        run = self.run_bridge("HEAD", "--model", self.MODEL, "--ticket", self.TICKET)

        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertFalse(self.machine_log.exists())

    def test_a_log_that_cannot_be_written_leaves_the_report_and_the_exit_alone(self):
        blocked = pathlib.Path(self.work.name) / "not-a-directory"
        blocked.write_text("this is a file, so nothing can be created beneath it\n")

        run = self.run_logged(machine_log=blocked / "log.jsonl")

        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("Standards:", run.output["findings"])

    def test_a_review_that_came_back_an_error_still_writes_its_returned_line(self):
        run = self.run_logged(scenario="error")

        self.assertEqual(run.returncode, 1)
        self.assertPair(self.reviews())

    def test_a_review_the_bridge_could_not_parse_still_writes_its_returned_line(self):
        run = self.run_logged(scenario="bad-json")

        self.assertEqual(run.returncode, 1)
        self.assertPair(self.reviews())

    def test_round_two_writes_its_own_pair_for_the_same_ticket(self):
        first = self.run_logged()

        second = self.run_logged("--resume-session", first.output["lineageId"])

        self.assertEqual(second.returncode, 0, second.stderr)
        records = self.reviews()
        self.assertEqual(
            [record["state"] for record in records],
            ["running", "returned", "running", "returned"],
        )
        self.assertEqual({record["ticket"] for record in records}, {self.TICKET})


class ScopedRoundOneTestCase(BridgeTestCase):
    """A real repository, a real worktree on a branch, and a PATH the graph CLI can be kept off.

    The bridge is told only `--cwd` and `--base`: the main checkout and the branch under review
    are its own to derive, because a worktree shares the object database with the checkout that
    holds the graph.
    """

    BRANCH = "ticket-71"
    TOUCHED_FILE = "app.py"
    ADDED_FILE = "notes.md"
    GRAPH_CLI = "code-review-graph"

    def setUp(self):
        super().setUp()
        self.checkout = pathlib.Path(self.work.name) / "checkout"
        self.checkout.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.email", "crew@example.com")
        self.git("config", "user.name", "Crew Test")
        (self.checkout / self.TOUCHED_FILE).write_text("def one():\n    return 1\n")
        self.git("add", ".")
        self.git("commit", "-m", "base")
        self.base = self.git("rev-parse", "HEAD").strip()

        # BridgeTestCase made `self.cwd` a bare directory; the worktree takes its place.
        self.cwd.rmdir()
        self.git("worktree", "add", "-b", self.BRANCH, str(self.cwd))
        (self.cwd / self.TOUCHED_FILE).write_text("def one():\n    return 2\n")
        (self.cwd / self.ADDED_FILE).write_text("what changed and why\n")
        self.git("add", ".", cwd=self.cwd)
        self.git("commit", "-m", "the change under review", cwd=self.cwd)

        self.graph_bin = pathlib.Path(self.work.name) / "graph-bin"
        self.graph_bin.mkdir()
        self.graph_argv_log = pathlib.Path(self.work.name) / "crg-argv.jsonl"

    def git(self, *arguments, cwd=None):
        completed = subprocess.run(
            ["git", *arguments],
            cwd=str(cwd or self.checkout),
            text=True,
            capture_output=True,
            check=True,
        )
        return completed.stdout

    def install_graph_stub(self):
        installed = self.graph_bin / self.GRAPH_CLI
        if not installed.exists():
            installed.symlink_to(GRAPH_STUB_PATH)

    def graphless_path(self):
        """PATH with every directory that already carries a real graph CLI removed."""
        kept = [
            entry
            for entry in os.environ.get("PATH", "").split(os.pathsep)
            if entry and not (pathlib.Path(entry) / self.GRAPH_CLI).exists()
        ]
        return os.pathsep.join(kept)

    def run_scoped(
        self,
        *arguments,
        scenario="ok",
        graph_scenario="risk",
        with_graph=True,
        env_redirection=None,
    ):
        if with_graph:
            self.install_graph_stub()
        environment = {
            "PATH": (
                f"{self.graph_bin}{os.pathsep}{self.graphless_path()}"
                if with_graph
                else self.graphless_path()
            ),
            "CRG_STUB_SCENARIO": graph_scenario,
            "CRG_STUB_ARGV_LOG": str(self.graph_argv_log),
        }
        environment.update(env_redirection or {})
        return self.run_bridge(
            "the changes in this worktree since the base",
            "--base",
            self.base,
            *arguments,
            scenario=scenario,
            env=environment,
        )

    def graph_calls(self):
        if not self.graph_argv_log.exists():
            return []
        return [
            json.loads(line)
            for line in self.graph_argv_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def prompt_from(self, run):
        return run.invocations[-1]["argv"][-1]


class ScopedContextTests(ScopedRoundOneTestCase):
    """Round one opens with an analysis of exactly the range under review.

    The graph CLI answers when it can; when it cannot — missing, failing, or reporting no changed
    functions against a diff that plainly has some — a git summary fills the same slot, and
    nothing else about the review changes.
    """

    def test_the_graph_is_queried_for_the_range_against_the_main_checkout(self):
        run = self.run_scoped()

        self.assertEqual(run.returncode, 0, run.stderr)
        calls = self.graph_calls()
        self.assertEqual(len(calls), 1)
        argv = calls[0]["argv"]
        self.assertEqual(argv[0], "detect-changes")
        self.assertIn("--brief", argv)
        self.assertEqual(argv[argv.index("--base") + 1], f"{self.base}...{self.BRANCH}")
        self.assertEqual(
            pathlib.Path(argv[argv.index("--repo") + 1]).resolve(),
            self.checkout.resolve(),
        )

    def test_the_graph_is_never_pointed_by_environment_variable(self):
        # Measured unreliable: a redirected graph answers with a silent zero-risk score, so even
        # an inherited redirection must not reach the call.
        self.run_scoped(
            env_redirection={
                "CRG_DATA_DIR": str(self.cwd / "private-graph"),
                "CRG_REPO_ROOT": str(self.cwd),
            }
        )

        call = self.graph_calls()[0]
        self.assertIsNone(call["dataDirEnv"])
        self.assertIsNone(call["repoRootEnv"])

    def test_the_risk_analysis_reaches_the_round_one_prompt(self):
        run = self.run_scoped()

        prompt = self.prompt_from(run)
        self.assertIn(f"{self.base}...{self.BRANCH}", prompt)
        self.assertIn("3 changed function(s)/class(es)", prompt)
        self.assertIn("2 test gap(s)", prompt)
        self.assertIn("Overall risk score: 0.62", prompt)

    def test_a_missing_graph_cli_fills_the_slot_with_the_git_summary(self):
        run = self.run_scoped(with_graph=False)

        self.assertEqual(run.returncode, 0, run.stderr)
        prompt = self.prompt_from(run)
        self.assertIn(self.base, prompt)
        self.assertIn("2 files changed", prompt)
        self.assertIn(self.TOUCHED_FILE, prompt)
        self.assertIn(self.ADDED_FILE, prompt)

    def test_a_failing_graph_cli_falls_back_to_the_git_summary(self):
        run = self.run_scoped(graph_scenario="fail")

        self.assertEqual(run.returncode, 0, run.stderr)
        prompt = self.prompt_from(run)
        self.assertIn("2 files changed", prompt)
        self.assertIn(self.ADDED_FILE, prompt)

    def test_a_silent_graph_cli_falls_back_to_the_git_summary(self):
        run = self.run_scoped(graph_scenario="empty")

        self.assertEqual(run.returncode, 0, run.stderr)
        prompt = self.prompt_from(run)
        self.assertIn("2 files changed", prompt)

    def test_zero_changed_functions_on_a_non_empty_diff_is_treated_as_no_graph(self):
        run = self.run_scoped(graph_scenario="zero")

        prompt = self.prompt_from(run)
        self.assertNotIn("Overall risk score", prompt)
        self.assertIn("2 files changed", prompt)
        self.assertIn(self.TOUCHED_FILE, prompt)

    def test_uncommitted_work_is_summarised_rather_than_left_out(self):
        """The review runs before the child commits, so the range must end at the working tree."""
        (self.cwd / "pending.py").write_text("def two():\n    return 2\n")
        (self.cwd / self.TOUCHED_FILE).write_text("def one():\n    return 3\n")

        run = self.run_scoped()

        prompt = self.prompt_from(run)
        self.assertIn("pending.py", prompt)
        self.assertIn(self.TOUCHED_FILE, prompt)
        self.assertIn(self.base, prompt)

    def test_a_dirty_worktree_is_never_answered_by_a_graph_that_cannot_see_it(self):
        (self.cwd / "pending.py").write_text("def two():\n    return 2\n")

        run = self.run_scoped()

        self.assertEqual(self.graph_calls(), [])
        self.assertNotIn("Overall risk score", self.prompt_from(run))

    def test_the_review_behaves_identically_whichever_slot_filler_ran(self):
        with_graph = self.run_scoped()
        self.argv_log.unlink()
        self.graph_argv_log.unlink()
        without_graph = self.run_scoped(with_graph=False)

        self.assertEqual(without_graph.returncode, with_graph.returncode)
        for key in ("status", "sessionId", "round", "findings", "permissionDenials"):
            self.assertEqual(without_graph.output[key], with_graph.output[key], key)

    def test_a_call_without_a_base_reviews_exactly_as_before(self):
        scoped = self.run_scoped()
        self.argv_log.unlink()

        plain = self.run_bridge("the changes in this worktree since the base")

        self.assertEqual(plain.returncode, 0, plain.stderr)
        self.assertEqual(plain.output["status"], scoped.output["status"])
        self.assertNotIn("Change analysis", self.prompt_from(plain))

    def test_the_follow_up_prompt_does_not_repeat_the_scoped_block(self):
        first = self.run_scoped()

        second = self.run_scoped("--resume-session", first.output["lineageId"])

        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertNotIn("Change analysis", self.prompt_from(second))


class VerificationReuseTests(ScopedRoundOneTestCase):
    """The reviewer is handed what the author already ran, and told not to run it again."""

    VERIFICATION = "python3 -m unittest discover -s tests — passed; validator — passed"

    def test_the_recorded_verification_reaches_the_round_one_prompt(self):
        run = self.run_scoped("--verification", self.VERIFICATION)

        prompt = self.prompt_from(run)
        self.assertIn(self.VERIFICATION, prompt)

    def test_the_prompt_asks_for_the_touched_tests_only(self):
        run = self.run_scoped("--verification", self.VERIFICATION)

        prompt = self.prompt_from(run)
        self.assertIn("only the tests the diff touches", prompt)
        self.assertIn("full suite", prompt)

    def test_an_unrecorded_verification_says_so_rather_than_claiming_one(self):
        run = self.run_scoped()

        prompt = self.prompt_from(run)
        self.assertIn("recorded no verification", prompt)
        self.assertIn("only the tests the diff touches", prompt)


class ReReviewGateTests(ScopedRoundOneTestCase):
    """The cap is code now: a second pass needs a spec finding from the first.

    The round-one prompt asks for a machine-readable verdict line, and the gate reads it. A round
    one that never printed one is not evidence of a clean review, so the gate lets that through
    rather than refusing a review the caller may genuinely need.
    """

    def resume(self, first, **kwargs):
        return self.run_scoped("--resume-session", first.output["lineageId"], **kwargs)

    def test_the_round_one_prompt_asks_for_the_verdict_line(self):
        run = self.run_scoped()

        prompt = self.prompt_from(run)
        self.assertIn("REVIEW VERDICT: spec-findings-requiring-fix=", prompt)

    def test_a_spec_finding_that_required_a_fix_earns_the_second_pass(self):
        first = self.run_scoped()

        second = self.resume(first)

        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(second.output["status"], "completed")
        self.assertEqual(second.output["round"], 2)
        self.assertEqual(len(second.invocations), 2)

    def test_a_standards_only_round_one_is_refused_a_second_pass(self):
        first = self.run_scoped(scenario="verdict-clean")

        second = self.resume(first, scenario="verdict-clean")

        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(second.output["status"], "refused")
        self.assertEqual(second.output["specFindingsRequiringFix"], 0)
        self.assertEqual(len(second.invocations), 1)
        self.assertEqual(second.state(first.output["lineageId"])["rounds"], 1)

    def test_a_refusal_says_why_without_pretending_to_be_a_review(self):
        first = self.run_scoped(scenario="verdict-clean")

        second = self.resume(first, scenario="verdict-clean")

        self.assertIn("spec finding", second.output["reason"])
        self.assertEqual(second.output["findings"], "")
        self.assertEqual(second.output["lineageId"], first.output["lineageId"])

    def test_a_round_one_without_a_verdict_line_earns_no_second_pass(self):
        first = self.run_scoped(scenario="no-verdict")

        second = self.resume(first, scenario="no-verdict")

        self.assertEqual(second.output["status"], "refused")
        self.assertIsNone(second.output["specFindingsRequiringFix"])
        self.assertIn("REVIEW VERDICT", second.output["reason"])
        self.assertEqual(len(second.invocations), 1)

    def test_half_a_verdict_line_is_not_a_verdict(self):
        """A fragment of the line, or the phrase inside prose, is not the report saying so."""
        first = self.run_scoped(scenario="part-verdict")

        second = self.resume(first, scenario="part-verdict")

        self.assertEqual(second.output["status"], "refused")
        self.assertIsNone(second.output["specFindingsRequiringFix"])
        self.assertEqual(len(second.invocations), 1)

    def test_the_one_re_review_is_a_cap_so_a_third_pass_is_refused(self):
        first = self.run_scoped()
        second = self.resume(first)

        third = self.resume(first)

        self.assertEqual(second.output["round"], 2)
        self.assertEqual(third.output["status"], "refused")
        self.assertIn("one re-review", third.output["reason"])
        self.assertEqual(len(third.invocations), 2)


if __name__ == "__main__":
    unittest.main()
