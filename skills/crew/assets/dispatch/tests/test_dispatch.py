#!/usr/bin/env python3
"""Drive the dispatch renderer from its command line against stubbed claude, tmux and codex.

Every fixture is built in a temporary root: a real git repository for the worktrees, a stub PATH
carrying `claude` and `tmux`, and a stub codex bridge. Assertions are on external behavior only —
the rendered launch JSON, the turn files, the composed launch command, the confirmation lines and
the exit code.
"""

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest


TESTS_DIR = pathlib.Path(__file__).resolve().parent
DISPATCH = TESTS_DIR.parent / "dispatch.py"
CREW_SKILL_DIR = TESTS_DIR.parents[2]

CLAUDE_MODEL = "claude-opus-4-5-20251101"
CLAUDE_EFFORT = "medium"
CODEX_MODEL = "gpt-5.6-luna"
CODEX_EFFORT = "max"
COORDINATOR_NAME = "crew-coordinator-1f"
COORDINATOR_PID = 1504
BASE_COMMIT = "b614ec84712aa8c351fe30ec69000e2e12518aeb"
PERMISSION_MODE = "acceptEdits"
TMUX_SESSION = "$7:"


def run_git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


class Fixture:
    """A temporary run: repository, tickets, stub PATH, and a wave table over them."""

    def __init__(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.repo = self.root / "repo"
        self.repo.mkdir()
        run_git(self.repo, "init", "-b", "main")
        run_git(self.repo, "config", "user.email", "crew@example.invalid")
        run_git(self.repo, "config", "user.name", "Crew Test")
        (self.repo / "README.md").write_text("fixture\n")
        run_git(self.repo, "add", "README.md")
        run_git(self.repo, "commit", "-m", "base")
        self.base_commit = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

        self.feature_dir = self.repo / "features" / "demo"
        self.feature_dir.mkdir(parents=True)
        self.spec_path = self.feature_dir / "spec.md"
        self.spec_path.write_text("# spec\n")

        self.stub_dir = self.root / "stub"
        self.stub_dir.mkdir()
        self.config_dir = self.root / "claude-config"
        self.config_dir.mkdir()
        self.out_dir = self.root / "render"
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self._link_stub("claude", "stub_claude.py")
        self._link_stub("tmux", "stub_tmux.py")
        self.codex_bridge = TESTS_DIR / "stub_codex_bridge.py"
        self.hook_marker = self.root / "hook-ran.json"

    def _link_stub(self, name, script):
        target = self.bin_dir / name
        target.write_text(
            "#!/bin/sh\nexec %s %s \"$@\"\n" % (sys.executable, TESTS_DIR / script)
        )
        target.chmod(0o755)

    def ticket(self, number, slug, **overrides):
        path = self.feature_dir / f"{number}-{slug}.md"
        path.write_text(f"# {number} {slug}\n")
        ticket = {
            "id": number,
            "title": slug.replace("-", " "),
            "path": str(path),
            "workflow": "tdd",
            "executor": "claude",
            "model": CLAUDE_MODEL,
            "effort": CLAUDE_EFFORT,
            "review": {"vendor": "codex", "model": CODEX_MODEL, "effort": CODEX_EFFORT},
        }
        ticket.update(overrides)
        return ticket

    def table(self, tickets, **run_overrides):
        run = {
            "repo_root": str(self.repo),
            "spec_path": str(self.spec_path),
            "integration_branch": "crew/demo",
            "integration_base_commit": self.base_commit,
            "coordinator_name": COORDINATOR_NAME,
            "coordinator_pid": COORDINATOR_PID,
            "crew_skill_dir": str(CREW_SKILL_DIR),
            "tmux_session": TMUX_SESSION,
            "permission_mode": PERMISSION_MODE,
            "codex": {
                "bridge": str(self.codex_bridge),
                "state_dir": str(self.feature_dir / ".crew-codex"),
            },
        }
        run.update(run_overrides)
        path = self.root / "wave-table.json"
        path.write_text(json.dumps({
            "run": run,
            "waves": [{"wave": 1, "tickets": tickets}],
        }))
        return path

    def run_dispatch(self, command, table, wave=1, env_overrides=None, extra=()):
        environment = dict(os.environ)
        environment["PATH"] = f"{self.bin_dir}{os.pathsep}{environment['PATH']}"
        environment["AGENTCREW_STUB_DIR"] = str(self.stub_dir)
        environment["CLAUDE_CONFIG_DIR"] = str(self.config_dir)
        environment.pop("AGENTCREW_STUB_TRANSCRIPT_MODEL", None)
        environment.pop("AGENTCREW_STUB_STATE_MODEL", None)
        environment.pop("AGENTCREW_STUB_STATE_CWD", None)
        environment.update(env_overrides or {})
        return subprocess.run(
            [
                sys.executable, str(DISPATCH), command,
                "--table", str(table),
                "--wave", str(wave),
                "--out-dir", str(self.out_dir),
                *extra,
            ],
            capture_output=True, text=True, env=environment,
        )

    def launches(self):
        path = self.stub_dir / "launches.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def codex_launches(self):
        path = self.stub_dir / "codex-launches.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def tmux_calls(self):
        path = self.stub_dir / "tmux-calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def turn(self, number):
        return (self.out_dir / f"{number}.turn.txt").read_text()

    def agent_json(self, number):
        return json.loads((self.out_dir / f"{number}.agents.json").read_text())

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


class DispatchTestCase(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()
        self.addCleanup(self.fixture.cleanup)


class TableValidationTests(DispatchTestCase):
    def test_every_offending_ticket_is_listed_with_what_it_lacks(self):
        tickets = [
            self.fixture.ticket("01", "missing-model", model=None),
            self.fixture.ticket("02", "bad-workflow", workflow="yolo"),
            self.fixture.ticket("03", "same-vendor-review", review={
                "vendor": "claude", "model": CODEX_MODEL, "effort": CODEX_EFFORT,
            }),
            self.fixture.ticket("04", "review-without-lane", workflow="direct"),
            self.fixture.ticket("05", "no-review", workflow="tdd", review=None),
        ]
        table = self.fixture.table(tickets)

        result = self.fixture.run_dispatch("dispatch", table)

        self.assertEqual(result.returncode, 1, result.stderr)
        for number, lacks in [
            ("01", "Model"),
            ("02", "Workflow"),
            ("03", "Review"),
            ("04", "Review"),
            ("05", "Review"),
        ]:
            offence = [line for line in result.stderr.splitlines() if line.startswith(number)]
            self.assertTrue(offence, f"ticket {number} is unlisted:\n{result.stderr}")
            self.assertIn(lacks, offence[0])
        self.assertEqual(self.fixture.launches(), [])
        self.assertEqual(self.fixture.tmux_calls(), [])

    def test_a_model_alias_is_rejected_before_any_launch(self):
        table = self.fixture.table([self.fixture.ticket("06", "aliased", model="opus")])

        result = self.fixture.run_dispatch("dispatch", table)

        self.assertEqual(result.returncode, 1)
        self.assertIn("06", result.stderr)
        self.assertIn("opus", result.stderr)
        self.assertIn("full model ID", result.stderr)
        self.assertEqual(self.fixture.launches(), [])
        self.assertEqual(self.fixture.tmux_calls(), [])

    def test_a_review_lane_alias_is_rejected_too(self):
        table = self.fixture.table([self.fixture.ticket("06", "aliased-lane", review={
            "vendor": "codex", "model": "haiku", "effort": CODEX_EFFORT,
        })])

        result = self.fixture.run_dispatch("dispatch", table)

        self.assertEqual(result.returncode, 1)
        self.assertIn("haiku", result.stderr)
        self.assertEqual(self.fixture.tmux_calls(), [])

    def test_a_context_suffixed_alias_is_rejected_before_any_launch(self):
        """The suffix rides on whatever it is attached to; it does not make `sonnet` a full ID."""
        table = self.fixture.table([self.fixture.ticket("06", "suffixed", model="sonnet[1m]")])

        result = self.fixture.run_dispatch("dispatch", table)

        self.assertEqual(result.returncode, 1)
        self.assertIn("06", result.stderr)
        self.assertIn("sonnet[1m]", result.stderr)
        self.assertIn("full model ID", result.stderr)
        self.assertEqual(self.fixture.launches(), [])
        self.assertEqual(self.fixture.tmux_calls(), [])

    def test_a_context_suffixed_review_lane_alias_is_rejected_too(self):
        table = self.fixture.table([self.fixture.ticket("06", "suffixed-lane", review={
            "vendor": "codex", "model": "opus[1m]", "effort": CODEX_EFFORT,
        })])

        result = self.fixture.run_dispatch("dispatch", table)

        self.assertEqual(result.returncode, 1)
        self.assertIn("opus[1m]", result.stderr)
        self.assertEqual(self.fixture.launches(), [])
        self.assertEqual(self.fixture.tmux_calls(), [])

    def test_a_context_suffixed_full_id_still_launches(self):
        """Stripping the suffix must not cost a full ID its launch: the rule cuts one way only."""
        table = self.fixture.table(
            [self.fixture.ticket("06", "suffixed-id", model=f"{CLAUDE_MODEL}[1m]")],
        )

        result = self.fixture.run_dispatch("dispatch", table)

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue(self.fixture.launches(), result.stderr)

    def test_a_codex_ticket_without_bridge_configuration_names_every_affected_ticket(self):
        codex = dict(executor="codex", model=CODEX_MODEL, effort=CODEX_EFFORT,
                     review={"vendor": "claude", "model": CLAUDE_MODEL, "effort": CLAUDE_EFFORT})
        table = self.fixture.table(
            [self.fixture.ticket("06", "claude-child"),
             self.fixture.ticket("07", "first-codex", **codex),
             self.fixture.ticket("08", "second-codex", **codex)],
            codex=None,
        )

        result = self.fixture.run_dispatch("dispatch", table)

        self.assertEqual(result.returncode, 1)
        for number in ("07", "08"):
            offence = [line for line in result.stderr.splitlines() if line.startswith(number)]
            self.assertTrue(offence, f"ticket {number} is unlisted:\n{result.stderr}")
            self.assertIn("bridge", offence[0])
        self.assertFalse([line for line in result.stderr.splitlines() if line.startswith("06")])

    def test_a_malformed_table_is_rejected_without_a_traceback(self):
        for name, payload in [
            ("not json", "{"),
            ("not utf-8", b"\xff\xfe\x00"),
            ("not an object", "[]"),
            ("waves is not a list", json.dumps({"run": {}, "waves": {"wave": 1}})),
            ("a ticket is not an object", json.dumps({"run": {}, "waves": [
                {"wave": 1, "tickets": ["06"]}]})),
            ("review is not an object", json.dumps({"run": {}, "waves": [
                {"wave": 1, "tickets": [{"id": "06", "review": "codex max"}]}]})),
        ]:
            with self.subTest(name):
                table = self.fixture.root / "malformed.json"
                if isinstance(payload, bytes):
                    table.write_bytes(payload)
                else:
                    table.write_text(payload)

                result = self.fixture.run_dispatch("dispatch", table)

                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertNotIn("Traceback", result.stderr)
                self.assertTrue(result.stderr.strip(), "a rejection says nothing")

    def test_a_wave_the_table_does_not_hold_is_rejected(self):
        table = self.fixture.table([self.fixture.ticket("06", "only-wave-one")])

        result = self.fixture.run_dispatch("dispatch", table, wave=2)

        self.assertEqual(result.returncode, 1)
        self.assertIn("wave 2", result.stderr)
        self.assertEqual(self.fixture.tmux_calls(), [])


class ClaudeRenderTests(DispatchTestCase):
    def setUp(self):
        super().setUp()
        self.table = self.fixture.table([self.fixture.ticket("06", "dispatch-renderer")])
        self.result = self.fixture.run_dispatch("render", self.table)
        self.assertEqual(self.result.returncode, 0, self.result.stderr)
        self.ticket_path = str(self.fixture.feature_dir / "06-dispatch-renderer.md")
        self.worktree = str(self.fixture.repo / ".claude" / "worktrees" / "06-dispatch-renderer")

    def initial_prompt(self):
        agents = self.fixture.agent_json("06")
        self.assertEqual(len(agents), 1, agents)
        return next(iter(agents.values()))["initialPrompt"]

    def test_the_launch_json_defines_one_agent_the_cli_can_register(self):
        agents = self.fixture.agent_json("06")
        definition = next(iter(agents.values()))
        self.assertIn("description", definition)
        self.assertIn("prompt", definition)
        self.assertIn("initialPrompt", definition)

    def test_the_first_turn_opens_on_the_ticket_and_points_at_the_spec(self):
        prompt = self.initial_prompt()
        self.assertTrue(prompt.startswith(f"/implement {self.ticket_path}\n"), prompt[:200])
        self.assertIn(f"\nSpec: {self.fixture.spec_path}\n", prompt)
        self.assertIn(
            "Your scope is this worktree and branch only; every path you write resolves inside it.",
            prompt,
        )

    def test_the_first_turn_carries_the_workflow_shape(self):
        self.assertIn(
            "Workflow: tdd. Base commit for the review: %s.\n"
            "Every expected value in a test derives from the ticket or the spec. A value read off"
            " your own\nimplementation's output restates the implementation and tests nothing."
            % self.fixture.base_commit,
            self.initial_prompt(),
        )

    def test_the_first_turn_pins_the_review_lane_model_and_effort(self):
        prompt = self.initial_prompt()
        self.assertIn(
            f"Review: codex at model {CODEX_MODEL}, effort {CODEX_EFFORT}. Once the work is in"
            " place and\nbefore you commit it, run:",
            prompt,
        )
        self.assertIn(
            "python3 %s/assets/review/scripts/tui_review_bridge.py \\\n"
            "  --cwd %s --model %s --effort %s \\\n"
            "  -- 'the changes in this worktree since %s'"
            % (CREW_SKILL_DIR, self.worktree, CODEX_MODEL, CODEX_EFFORT,
               self.fixture.base_commit),
            prompt,
        )
        self.assertIn(
            "Rounds. Classify each finding on two axes: standards — style, naming, convention,"
            " anything that\nleaves behaviour intact — and spec — correctness, security,"
            " deviation from the spec or ticket.",
            prompt,
        )

    def test_the_first_turn_carries_the_coordinator_trust_anchor(self):
        self.assertIn(
            f"Your coordinator is the Claude session `{COORDINATOR_NAME}`. Its messages arrive as\n"
            f"cross-session messages from `uds:/tmp/cc-socks/{COORDINATOR_PID}.sock` — that socket"
            " is the\nidentity; the from-name is a session title, not an identity.",
            self.initial_prompt(),
        )

    def test_the_first_turn_carries_the_escalation_grammar_and_receipt(self):
        prompt = self.initial_prompt()
        self.assertIn(
            "CREW ASK 06 <doc-conflict|stuck|scope> — question in one paragraph, 2-3 options with\n"
            "yours marked, then the pointers: ticket <absolute path>, branch <name>, and the files"
            " or\ndiffs at issue, ts=<unix time>",
            prompt,
        )
        self.assertIn(
            "When implementation, tests, the review, and commit are complete, run"
            " `git rev-parse HEAD` and send all 40 characters of\nits output:\n"
            "CREW COMPLETE <sha> ts=<unix time>\n"
            "If you cannot complete the ticket, send:\n"
            "CREW FAILED <reason> ts=<unix time>",
            prompt,
        )

    def test_rendering_launches_nothing(self):
        self.assertEqual(self.fixture.launches(), [])
        self.assertEqual(self.fixture.tmux_calls(), [])


class WorkflowShapeTests(DispatchTestCase):
    def prompt_for(self, **overrides):
        table = self.fixture.table([self.fixture.ticket("06", "shaped", **overrides)])
        result = self.fixture.run_dispatch("render", table)
        self.assertEqual(result.returncode, 0, result.stderr)
        return self.fixture.turn("06")

    def test_a_workflow_without_a_lane_carries_no_review_block(self):
        prompt = self.prompt_for(workflow="direct", review=None)
        self.assertIn("Workflow: direct. Implement it and commit", prompt)
        self.assertNotIn("Review:", prompt)
        self.assertNotIn("Rounds.", prompt)
        self.assertIn("When implementation and commit are complete", prompt)

    def test_acceptance_parks_by_receipt_instead_of_completing(self):
        prompt = self.prompt_for(workflow="acceptance", review=None)
        self.assertIn("Workflow: acceptance. This ticket closes with a human", prompt)
        self.assertIn(
            "Commit your preparation and the checklist, then park: send\n"
            "CREW PARKED <absolute checklist path> ts=<unix time>",
            prompt,
        )
        self.assertNotIn("CREW COMPLETE", prompt)

    def test_a_codex_child_reviewed_by_claude_gets_the_headless_bridge_variant(self):
        prompt = self.prompt_for(
            workflow="refactor", executor="codex", model=CODEX_MODEL, effort=CODEX_EFFORT,
            review={"vendor": "claude", "model": CLAUDE_MODEL, "effort": CLAUDE_EFFORT},
        )
        self.assertIn(
            f"Review: claude at model {CLAUDE_MODEL}, effort {CLAUDE_EFFORT}.", prompt
        )
        self.assertIn("claude_review_bridge.py", prompt)


class CodexRenderTests(DispatchTestCase):
    def setUp(self):
        super().setUp()
        self.ticket = self.fixture.ticket(
            "07", "codex-child", executor="codex", model=CODEX_MODEL, effort=CODEX_EFFORT,
            review={"vendor": "claude", "model": CLAUDE_MODEL, "effort": CLAUDE_EFFORT},
        )
        self.table = self.fixture.table([self.ticket])

    def test_a_codex_ticket_renders_a_turn_file_and_no_launch_json(self):
        result = self.fixture.run_dispatch("render", self.table)

        self.assertEqual(result.returncode, 0, result.stderr)
        turn = self.fixture.turn("07")
        self.assertIn(
            "Your coordinator is outside your session and reads the final message of every turn"
            " you end —\nnever anything you print mid-turn.",
            turn,
        )
        self.assertNotIn("cc-socks", turn)
        self.assertIn("CREW COMPLETE <sha> ts=<unix time>", turn)
        self.assertFalse((self.fixture.out_dir / "07.agents.json").exists())

    def test_a_codex_ticket_launches_through_the_bridge_with_its_turn_file(self):
        result = self.fixture.run_dispatch("dispatch", self.table)

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        launches = self.fixture.codex_launches()
        self.assertEqual(len(launches), 1, launches)
        argv = launches[0]["argv"]
        self.assertEqual(argv[0], "launch")
        worktree = str(self.fixture.repo / ".claude" / "worktrees" / "07-codex-child")
        for flag, value in [
            ("--cwd", worktree),
            ("--tmux-session", TMUX_SESSION),
            ("--window-name", "07"),
            ("--model", CODEX_MODEL),
            ("--effort", CODEX_EFFORT),
            ("--prompt-file", str(self.fixture.out_dir / "07.turn.txt")),
            ("--state-file", str(self.fixture.feature_dir / ".crew-codex" / "07.json")),
        ]:
            self.assertIn(flag, argv)
            self.assertEqual(argv[argv.index(flag) + 1], value)
        self.assertEqual(self.fixture.launches(), [])

    def test_a_codex_state_file_naming_no_working_directory_is_a_launch_failure(self):
        result = self.fixture.run_dispatch(
            "dispatch", self.table, env_overrides={"AGENTCREW_STUB_STATE_CWD": ""},
        )

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertTrue(result.stdout.strip().startswith("07 FAILED"), result.stdout)

    def test_a_codex_session_pinned_to_another_model_is_a_launch_failure(self):
        result = self.fixture.run_dispatch(
            "dispatch", self.table,
            env_overrides={"AGENTCREW_STUB_STATE_MODEL": "gpt-5.6-nova"},
        )

        self.assertEqual(result.returncode, 1, result.stdout)
        line = result.stdout.strip()
        self.assertTrue(line.startswith("07 FAILED"), line)
        self.assertIn("gpt-5.6-nova", line)
        self.assertIn(CODEX_MODEL, line)


class DispatchLaunchTests(DispatchTestCase):
    def setUp(self):
        super().setUp()
        self.table = self.fixture.table(
            [self.fixture.ticket("06", "dispatch-renderer")],
            launch_hook={
                "command": "{ echo \"$AGENTCREW_CHILD_CWD\";"
                           " echo \"$AGENTCREW_CHILD_TMUX_TARGET\";"
                           " echo \"$AGENTCREW_HOOK_TOKEN\"; } >> "
                           + str(self.fixture.hook_marker),
                "env": {"AGENTCREW_HOOK_TOKEN": "hook-token"},
            },
        )
        self.worktree = self.fixture.repo / ".claude" / "worktrees" / "06-dispatch-renderer"

    def test_the_child_gets_a_worktree_on_its_own_branch(self):
        result = self.fixture.run_dispatch("dispatch", self.table)

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue(self.worktree.is_dir())
        branch = subprocess.run(
            ["git", "-C", str(self.worktree), "rev-parse", "--abbrev-ref", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        self.assertEqual(branch, "worktree-06-dispatch-renderer")

    def test_base_commit_cuts_the_worktree_and_the_review_from_that_commit(self):
        """A wave launched after an earlier one landed is cut from what that wave left behind."""
        (self.fixture.repo / "landed.md").write_text("what wave one landed\n")
        run_git(self.fixture.repo, "add", "landed.md")
        run_git(self.fixture.repo, "commit", "-m", "wave one")
        landed = subprocess.run(
            ["git", "-C", str(self.fixture.repo), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

        result = self.fixture.run_dispatch(
            "dispatch", self.table, extra=("--base-commit", landed),
        )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue((self.worktree / "landed.md").is_file())
        self.assertIn(f"Base commit for the review: {landed}.", self.fixture.turn("06"))

    def test_the_guard_assets_are_installed_with_the_worktree_path_filled_in(self):
        self.fixture.run_dispatch("dispatch", self.table)

        for name in ("red-line.sh", "worktree-guard.sh", "settings.local.json"):
            installed = self.worktree / ".claude" / name
            self.assertTrue(installed.exists(), name)
            self.assertNotIn("<WORKTREE_ABSOLUTE_PATH>", installed.read_text())
        for name in ("red-line.sh", "worktree-guard.sh"):
            self.assertTrue(os.access(self.worktree / ".claude" / name, os.X_OK), name)
        self.assertIn(str(self.worktree), (self.worktree / ".claude" / "settings.local.json").read_text())

    def test_the_launch_hook_runs_once_for_the_child_window(self):
        self.fixture.run_dispatch("dispatch", self.table)

        printed = self.fixture.hook_marker.read_text().splitlines()
        self.assertEqual(len(printed), 3, printed)
        self.assertEqual(printed[0], str(self.worktree))
        self.assertTrue(printed[1].startswith("@"), printed)
        self.assertEqual(printed[2], "hook-token")

    def test_the_window_is_named_for_the_ticket_in_the_approved_session(self):
        self.fixture.run_dispatch("dispatch", self.table)

        new_windows = [call["argv"] for call in self.fixture.tmux_calls()
                       if call["argv"][0] == "new-window"]
        self.assertEqual(len(new_windows), 1, new_windows)
        argv = new_windows[0]
        self.assertEqual(argv[argv.index("-t") + 1], TMUX_SESSION)
        self.assertEqual(argv[argv.index("-n") + 1], "06")
        self.assertEqual(argv[argv.index("-c") + 1], str(self.worktree))

    def test_the_launch_command_carries_the_routed_model_effort_and_mode(self):
        self.fixture.run_dispatch("dispatch", self.table)

        launches = self.fixture.launches()
        self.assertEqual(len(launches), 1, launches)
        argv = launches[0]["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], CLAUDE_MODEL)
        self.assertEqual(argv[argv.index("--effort") + 1], CLAUDE_EFFORT)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], PERMISSION_MODE)
        self.assertEqual(
            os.path.realpath(launches[0]["cwd"]), os.path.realpath(str(self.worktree))
        )
        agents = json.loads(argv[argv.index("--agents") + 1])
        self.assertEqual(list(agents), [argv[argv.index("--agent") + 1]])
        self.assertEqual(
            next(iter(agents.values()))["initialPrompt"], self.fixture.turn("06")
        )

    def test_each_child_gets_one_confirmation_line(self):
        result = self.fixture.run_dispatch("dispatch", self.table)

        lines = [line for line in result.stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, result.stdout)
        line = lines[0]
        self.assertTrue(line.startswith("06 launched"), line)
        for value in ("claude", CLAUDE_MODEL, CLAUDE_EFFORT, "stub-child-1"):
            self.assertIn(value, line)
        self.assertIn("window=@", line)

    def test_a_failing_launch_hook_is_reported_and_the_child_launches_anyway(self):
        table = self.fixture.table(
            [self.fixture.ticket("06", "dispatch-renderer")],
            launch_hook={"command": "exit 3", "env": {}},
        )

        result = self.fixture.run_dispatch("dispatch", table)

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        line = result.stdout.strip()
        self.assertTrue(line.startswith("06 launched"), line)
        self.assertIn("hook-failed=3", line)
        self.assertEqual(len(self.fixture.launches()), 1)

    def test_a_hook_that_hangs_is_reported_and_the_wave_still_launches(self):
        table = self.fixture.table(
            [self.fixture.ticket("06", "dispatch-renderer")],
            launch_hook={"command": "sleep 30", "env": {}},
        )

        result = self.fixture.run_dispatch(
            "dispatch", table, extra=("--hook-timeout", "1"),
        )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        line = result.stdout.strip()
        self.assertTrue(line.startswith("06 launched"), line)
        self.assertIn("hook-failed=timeout", line)

    def test_a_worktree_that_is_not_this_tickets_branch_is_a_launch_failure(self):
        worktree = self.fixture.repo / ".claude" / "worktrees" / "06-dispatch-renderer"
        worktree.parent.mkdir(parents=True)
        run_git(self.fixture.repo, "worktree", "add", "-b", "someone-elses-branch",
                str(worktree), self.fixture.base_commit)

        result = self.fixture.run_dispatch("dispatch", self.table)

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertTrue(result.stdout.strip().startswith("06 FAILED"), result.stdout)
        self.assertIn("worktree-06-dispatch-renderer", result.stdout)
        self.assertEqual(self.fixture.launches(), [])

    def test_a_transcript_on_another_model_is_a_launch_failure(self):
        result = self.fixture.run_dispatch(
            "dispatch", self.table,
            env_overrides={"AGENTCREW_STUB_TRANSCRIPT_MODEL": "claude-haiku-4-5-20251001"},
        )

        self.assertEqual(result.returncode, 1, result.stdout)
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, result.stdout)
        self.assertTrue(lines[0].startswith("06 FAILED"), lines[0])
        self.assertIn("claude-haiku-4-5-20251001", lines[0])
        self.assertIn(CLAUDE_MODEL, lines[0])

    def test_a_child_absent_from_the_agents_list_is_a_launch_failure(self):
        table = self.fixture.table([self.fixture.ticket(
            "06", "dispatch-renderer", model="claude-opus-4-5-20251101",
        )], permission_mode=PERMISSION_MODE)
        # A launch that never reaches the CLI leaves no agents entry to verify against.
        (self.fixture.bin_dir / "claude").write_text(
            "#!/bin/sh\nif [ \"$1\" = agents ]; then echo '[]'; fi\nexit 0\n"
        )
        (self.fixture.bin_dir / "claude").chmod(0o755)

        result = self.fixture.run_dispatch("dispatch", table, extra=("--verify-timeout", "1"))

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertTrue(result.stdout.strip().startswith("06 FAILED"), result.stdout)


class MixedWaveTests(DispatchTestCase):
    def test_one_line_per_child_in_ticket_order(self):
        tickets = [
            self.fixture.ticket("06", "claude-child"),
            self.fixture.ticket(
                "07", "codex-child", executor="codex", model=CODEX_MODEL, effort=CODEX_EFFORT,
                review={"vendor": "claude", "model": CLAUDE_MODEL, "effort": CLAUDE_EFFORT},
            ),
        ]
        table = self.fixture.table(tickets)

        result = self.fixture.run_dispatch("dispatch", table)

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 2, result.stdout)
        self.assertTrue(lines[0].startswith("06 launched claude"), lines[0])
        self.assertTrue(lines[1].startswith("07 launched codex"), lines[1])


if __name__ == "__main__":
    unittest.main()
