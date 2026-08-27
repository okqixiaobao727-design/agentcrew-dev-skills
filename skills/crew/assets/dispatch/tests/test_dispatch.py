#!/usr/bin/env python3
"""Drive the dispatch renderer from its command line against stubbed claude, tmux and codex.

Every fixture is built in a temporary root: a real git repository for the worktrees, a stub PATH
carrying `claude` and `tmux`, and a stub codex bridge. Assertions are on external behavior only —
the rendered launch JSON, the turn files, the composed launch command, the confirmation lines and
the exit code.
"""

import contextlib
import io
import json
import os
import pathlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


TESTS_DIR = pathlib.Path(__file__).resolve().parent
DISPATCH = TESTS_DIR.parent / "dispatch.py"
CREW_SKILL_DIR = TESTS_DIR.parents[2]
sys.path.insert(0, str(DISPATCH.parent))
import dispatch as dispatch_module  # noqa: E402

CLAUDE_MODEL = "claude-opus-4-5-20251101"
CLAUDE_EFFORT = "medium"
CODEX_MODEL = "gpt-5.6-luna"
CODEX_EFFORT = "max"
WITNESS_MODEL = "claude-sonnet-5"
WITNESS_BUDGET_USD = 2.5
COORDINATOR_NAME = "crew-coordinator-1f"
COORDINATOR_PID = 1504
BASE_COMMIT = "b614ec84712aa8c351fe30ec69000e2e12518aeb"
PERMISSION_MODE = "acceptEdits"
TMUX_SESSION = "$7:"
RENDERED_RUN_AGAIN_BUDGET = (
    "A `run again` axis is run again at most once during this ticket's only review; past that "
    "the child sends `CREW ASK 06 stuck` with its reason"
)
# The two halves of a row's account binding, spelled as the wave table spells them: a ticket that
# named an account selects that configuration home explicitly, and a ticket that named none
# inherits the environment the run was started in (ADR-0014).
INHERITED = "inherited"
EXPLICIT = "explicit"
CONFIG_HOME_VARIABLE = "CLAUDE_CONFIG_DIR"


def review_command_argv(prompt):
    """The one installed Review-Switch command rendered into a child's first turn."""
    lines = prompt.splitlines()
    starts = [index for index, line in enumerate(lines) if line == "review-bridge \\"]
    if len(starts) != 1:
        raise AssertionError(f"expected one review-bridge command, found {len(starts)}")
    command = []
    for line in lines[starts[0]:]:
        continued = line.rstrip().endswith("\\")
        command.append(line.rstrip().removesuffix("\\").strip())
        if not continued:
            break
    return shlex.split(" ".join(command))


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
        self.config_path = self.repo / "agentcrew.toml"
        self.config_path.write_text(
            f'[witness]\nmodel = "{WITNESS_MODEL}"\nbudget_usd = {WITNESS_BUDGET_USD}\n'
        )

        self.stub_dir = self.root / "stub"
        self.stub_dir.mkdir()
        # The coordinator's own configuration home, which is what the driver resolves a ticket
        # naming no account to, and a second account's home beside it for the mixed-wave cases.
        self.config_dir = self.root / "claude-config"
        self.config_dir.mkdir()
        self.other_account = self.root / "claude-account-b"
        self.other_account.mkdir()
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
            # Every row of a validated wave table carries the account binding its ticket's
            # processes run under: the resolved configuration home, and whether that home is
            # selected explicitly or inherited from the environment the run was started in
            # (ADR-0014). This fixture's rows are the ordinary case — a ticket that named no
            # account, bound to the coordinator's own home, inherited.
            "account": str(self.config_dir),
            "account_mode": INHERITED,
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
            "coordinator_config_home": str(self.config_dir),
            "repair_model": CLAUDE_MODEL,
            "tracker": "github",
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

    def run_dispatch(self, command, table, wave=1, env_overrides=None, extra=(),
                     out_dir=None, cwd=None):
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
                "--out-dir", str(self.out_dir if out_dir is None else out_dir),
                *extra,
            ],
            capture_output=True, text=True, env=environment,
            cwd=str(cwd) if cwd else None,
        )

    def roles_arguments(self, ticket):
        return [
            "roles",
            "--spec", str(self.spec_path),
            "--ticket", str(ticket),
            "--coordinator-name", COORDINATOR_NAME,
        ]

    def run_roles(self, ticket):
        return subprocess.run(
            [sys.executable, str(DISPATCH), *self.roles_arguments(ticket)],
            capture_output=True, text=True,
        )

    def log_records(self, path):
        path = pathlib.Path(path)
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

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

    def transcripts(self, account):
        """Every session id that account's profile holds a transcript for."""
        return sorted(
            path.stem for path in (pathlib.Path(account) / "projects").glob("*/*.jsonl")
        )

    def agents_listed(self, account):
        """The names in that account's own live agents list, empty where it holds none."""
        path = pathlib.Path(account) / "agents.json"
        if not path.exists():
            return []
        return sorted(entry["name"] for entry in json.loads(path.read_text()))

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


class ManualRolesTests(DispatchTestCase):
    def test_the_advisor_prompt_heads_the_skill_contract_with_the_spec_path(self):
        ticket = self.fixture.ticket("136", "manual-roles")
        skill = (CREW_SKILL_DIR / "SKILL.md").read_text()
        contract = skill.split("## Contract\n", 1)[1].split("\n## ", 1)[0].strip()

        result = self.fixture.run_roles(ticket["path"])

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"Spec: {self.fixture.spec_path}.", result.stdout)
        self.assertIn(contract, result.stdout)

    def test_the_developer_prompt_reuses_the_first_turn_escalation_clause(self):
        ticket = self.fixture.ticket("136", "manual-roles")
        escalation = dispatch_module.block(
            dispatch_module.load_templates()["turn"]["escalate"]
        )

        result = self.fixture.run_roles(ticket["path"])

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(escalation, result.stdout)
        self.assertIn(COORDINATOR_NAME, result.stdout)
        self.assertIn(str(self.fixture.spec_path), result.stdout)
        self.assertIn(ticket["path"], result.stdout)

    def test_manual_and_first_turn_render_the_same_clause_for_their_known_values(self):
        ticket = self.fixture.ticket("136", "manual-roles")
        shared = dispatch_module.block(
            dispatch_module.load_templates()["turn"]["escalate"]
        )

        manual = self.fixture.run_roles(ticket["path"])
        table = self.fixture.table([ticket])
        first_turn = self.fixture.run_dispatch("render", table)

        self.assertEqual(manual.returncode, 0, manual.stderr)
        self.assertEqual(first_turn.returncode, 0, first_turn.stderr)
        self.assertIn(shared, manual.stdout)
        self.assertIn(shared.replace("<NN>", "136"), self.fixture.turn("136"))

    def test_the_developer_prompt_fills_the_configured_witness_command(self):
        ticket = self.fixture.ticket("136", "manual-roles")
        expected = shlex.join([
            "python3", str(CREW_SKILL_DIR / "assets" / "witness.py"),
            "--escalation", "-", "--worktree", ".",
            "--model", WITNESS_MODEL,
            "--budget-usd", f"{WITNESS_BUDGET_USD:g}",
        ])

        result = self.fixture.run_roles(ticket["path"])

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(expected, result.stdout)

    def test_an_aliased_configured_witness_model_is_an_error(self):
        ticket = self.fixture.ticket("136", "manual-roles")
        self.fixture.config_path.write_text(
            f'[witness]\nmodel = "sonnet"\nbudget_usd = {WITNESS_BUDGET_USD}\n'
        )

        result = self.fixture.run_roles(ticket["path"])

        self.assertEqual(result.returncode, 1)
        self.assertIn(str(self.fixture.config_path), result.stderr)
        self.assertIn("alias", result.stderr)

    def test_invalid_shipped_fallbacks_name_both_sources_and_the_config_key(self):
        ticket = self.fixture.ticket("136", "manual-roles")
        self.fixture.config_path.unlink()
        cases = (
            ('model = "sonnet"\nbudget_usd = 2.0', "`[witness] model`", "alias"),
            ('model = "claude-sonnet-5"\nbudget_usd = 0', "[witness] budget_usd", "positive"),
        )

        for witness, key, reason in cases:
            with self.subTest(key=key):
                defaults = self.fixture.root / "agentcrew.default.toml"
                defaults.write_text(f"[witness]\n{witness}\n")
                errors = io.StringIO()
                with mock.patch.object(dispatch_module.run_plan, "DEFAULT_CONFIG", defaults):
                    with contextlib.redirect_stderr(errors):
                        status = dispatch_module.main(
                            self.fixture.roles_arguments(ticket["path"])
                        )

                self.assertEqual(status, 1)
                self.assertIn("project config and shipped defaults", errors.getvalue())
                self.assertIn(key, errors.getvalue())
                self.assertIn(reason, errors.getvalue())

    def test_a_skill_without_a_contract_names_that_document_as_an_error(self):
        ticket = self.fixture.ticket("136", "manual-roles")
        document = self.fixture.root / "crew-without-contract.md"
        document.write_text("# AgentCrew\n")
        errors = io.StringIO()
        arguments = self.fixture.roles_arguments(ticket["path"])

        with mock.patch.object(dispatch_module, "SKILL_DOCUMENT", document):
            with contextlib.redirect_stderr(errors):
                status = dispatch_module.main(arguments)

        self.assertEqual(status, 1)
        self.assertIn(str(document), errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())

    def test_renderer_slots_are_filled_and_child_literal_tokens_survive(self):
        ticket = self.fixture.ticket("136", "manual-roles")

        result = self.fixture.run_roles(ticket["path"])

        self.assertEqual(result.returncode, 0, result.stderr)
        for filled in (
            "<contract>", "<absolute spec path>", "<absolute ticket path>",
            "<coordinator name>", "<escalation paragraph>", "<witness command>",
        ):
            self.assertNotIn(filled, result.stdout)
        for literal in (
            "<NN>", "<unix time>", "<path:line>", "#<ticket>", "ADR-<nnnn>",
            "<design|scope|doc-conflict|stuck|wrap-up>",
        ):
            self.assertIn(literal, result.stdout)

    def test_usage_lists_roles_and_each_of_its_arguments(self):
        result = subprocess.run(
            [sys.executable, str(DISPATCH), "--help"],
            capture_output=True, text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("roles", result.stdout)
        for argument in (
            "--spec", "--ticket", "--coordinator-name",
        ):
            self.assertIn(argument, result.stdout)


class WitnessPromptTests(unittest.TestCase):
    def test_the_ruled_witness_prompt_renders_from_witness_dot_prompt(self):
        escalation = "CREW ASK 132 design — check src/check.py:12 and ADR-0004"

        templates = dispatch_module.load_templates()
        prompt = dispatch_module.render_witness_prompt(escalation, templates)

        self.assertIn("witness", templates)
        self.assertIn("prompt", templates["witness"])
        self.assertIn(escalation, prompt)
        self.assertNotIn("<", prompt)


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

    def review_block(self):
        prompt = self.initial_prompt()
        return prompt.split("Review: ", 1)[1].split("\n\nYour coordinator is", 1)[0]

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

    def test_the_first_turn_calls_review_switch_with_the_approved_review_and_scope(self):
        prompt = self.initial_prompt()
        self.assertEqual(
            review_command_argv(prompt),
            [
                "review-bridge",
                "--reviewer", "codex",
                "--cwd", self.worktree,
                "--model", CODEX_MODEL,
                "--effort", CODEX_EFFORT,
                "--base", self.fixture.base_commit,
                "--spec", self.ticket_path,
                "--axis", "both",
            ],
        )
        self.assertNotIn("tui_review_bridge.py", prompt)
        self.assertNotIn("claude_review_bridge.py", prompt)

    def test_the_first_turn_follows_next_and_sends_a_typed_doc_conflict(self):
        prompt = self.initial_prompt()
        flattened = " ".join(prompt.split())
        self.assertIn(
            "For each axis do exactly what its `next` permits and nothing else", flattened
        )
        self.assertIn("`reportFile` and `preparation.responseFile`", flattened)
        self.assertIn(
            "send `CREW ASK 06 doc-conflict` carrying both positions to your coordinator",
            flattened,
        )
        self.assertNotIn("Rounds.", prompt)

    def test_the_first_turn_keeps_long_reviews_out_of_the_foreground_bash_limit(self):
        prompt = " ".join(self.initial_prompt().split())
        self.assertIn("background or long-running command", prompt)
        self.assertIn("`run_in_background: true`", prompt)
        self.assertIn("foreground Bash call is killed at ten minutes", prompt)

    def test_the_first_turn_follows_the_next_call_without_translating_it(self):
        prompt = " ".join(self.initial_prompt().split())
        self.assertIn("Where `nextCall.responseFile` is non-null", prompt)
        self.assertIn("write the Response to `nextCall.responseFile`", prompt)
        self.assertIn("in the shape `nextCall.responseFormat` shows", prompt)
        self.assertIn("one line per finding, in report order", prompt)
        self.assertIn("Run every `nextCall.argv` exactly as given", prompt)

    def test_the_first_turn_caps_the_callers_run_again_budget(self):
        prompt = " ".join(self.initial_prompt().split())
        self.assertIn(RENDERED_RUN_AGAIN_BUDGET, prompt)

    def test_the_first_turn_recovers_a_lost_result_before_starting_another_review(self):
        prompt = " ".join(self.initial_prompt().split())
        self.assertIn(
            "run the same command with `--recover-session` before starting another review",
            prompt,
        )
        self.assertIn("`review-bridge --help` is the rule for what its exit codes permit", prompt)

    def test_the_review_template_contains_no_copy_of_the_bridge_protocol(self):
        review_template = dispatch_module.load_templates()["review"]["block"]
        for copied_constant in (
            "--resume-session", "--response", "/tmp/", "two hours", "finalMessage",
        ):
            with self.subTest(copied_constant=copied_constant):
                self.assertNotIn(copied_constant, review_template)

    def test_the_first_turn_sends_a_typed_stuck_ask_for_a_refusal(self):
        prompt = " ".join(self.initial_prompt().split())
        self.assertIn("A `refused` result is not a report", prompt)
        self.assertIn("send `CREW ASK 06 stuck` carrying its `next` and reason", prompt)
        self.assertIn("start or resume nothing", prompt)

    def test_the_review_block_fills_every_value_the_dispatcher_owns(self):
        prompt = self.review_block()
        for placeholder in (
            "<review vendor>", "<review model>", "<review effort>", "<review account>",
            "<review cwd>", "<review base>", "<review spec>", "<review hooks>",
        ):
            with self.subTest(placeholder=placeholder):
                self.assertNotIn(placeholder, prompt)

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
            "CREW ASK 06 <design|scope|doc-conflict|stuck|wrap-up> — the body above, what the ruling\n"
            "touches, then the pointers: ticket <absolute path>, branch <name>, and every fact as a"
            " pointer\n— <path:line>, #<ticket>, ADR-<nnnn> — re-read as you write it, ts=<unix time>",
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


class ReviewEventRenderTests(DispatchTestCase):
    """The rendered Lifecycle Hook commands keep the run's existing review observations.

    Review-Switch owns when each point fires and AgentCrew owns the commands that write its log.
    """

    def setUp(self):
        super().setUp()
        self.machine_log = self.fixture.root / "run" / "log.jsonl"
        self.worktree = str(self.fixture.repo / ".claude" / "worktrees" / "06-reviewed")

    def prompt_for(self, *extra, **overrides):
        table = self.fixture.table([self.fixture.ticket("06", "reviewed", **overrides)])
        result = self.fixture.run_dispatch("render", table, extra=extra)
        self.assertEqual(result.returncode, 0, result.stderr)
        return self.fixture.turn("06")

    def hook_command(self, prompt, flag):
        argv = review_command_argv(prompt)
        return argv[argv.index(flag) + 1]

    def expected_review_hook(self, state, vendor="codex", model=CODEX_MODEL):
        return [
            "python3", str(self.machine_log.parent / "machine_log.py"),
            "--log", str(self.machine_log), "review", "--ticket", "06",
            "--lane", f"{vendor} {model}", "--state", state,
        ]

    def run_axis_end_hook(self, **facts):
        """Run the rendered hook and return the one session-cost event it appends."""
        self.machine_log.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(
            DISPATCH.parents[1] / "machine_log.py",
            self.machine_log.parent / "machine_log.py",
        )
        command = self.hook_command(
            self.prompt_for("--log", str(self.machine_log)), "--on-axis-end"
        )
        environment = dict(os.environ)
        for name in (
            "REVIEW_COST_DETAIL", "REVIEW_INPUT_TOKENS", "REVIEW_OUTPUT_TOKENS",
            "REVIEW_CACHE_READ_TOKENS", "REVIEW_CACHE_CREATION_TOKENS",
        ):
            environment.pop(name, None)
        environment.update(facts)
        result = subprocess.run(
            ["sh", "-c", command], capture_output=True, text=True, env=environment
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = self.machine_log.read_text().splitlines()
        self.assertEqual(len(lines), 1, lines)
        return json.loads(lines[0])

    def test_the_review_hooks_write_the_same_running_and_returned_pair(self):
        prompt = self.prompt_for("--log", str(self.machine_log))

        self.assertEqual(
            shlex.split(self.hook_command(prompt, "--on-review-start")),
            self.expected_review_hook("running"),
        )
        self.assertEqual(
            shlex.split(self.hook_command(prompt, "--on-review-end")),
            self.expected_review_hook("returned"),
        )

    def test_the_axis_end_hook_writes_the_review_figures_and_their_sum(self):
        counters = {
            "REVIEW_INPUT_TOKENS": "10",
            "REVIEW_OUTPUT_TOKENS": "20",
            "REVIEW_CACHE_READ_TOKENS": "30",
            "REVIEW_CACHE_CREATION_TOKENS": "40",
        }

        event = self.run_axis_end_hook(
            REVIEW_MODEL=CODEX_MODEL, REVIEW_SESSION="review-session-06", **counters
        )

        self.assertEqual(event["event"], "session-cost")
        self.assertEqual(event["ticket"], "06")
        self.assertEqual(event["executor"], "codex")
        self.assertEqual(event["model"], CODEX_MODEL)
        self.assertEqual(event["lane"], f"codex {CODEX_MODEL}")
        self.assertEqual(event["session"], "review-session-06")
        self.assertEqual(event["input_tokens"], 10)
        self.assertEqual(event["output_tokens"], 20)
        self.assertEqual(event["cache_read_tokens"], 30)
        self.assertEqual(event["cache_creation_tokens"], 40)
        self.assertEqual(event["total_tokens"], sum(map(int, counters.values())))
        self.assertNotIn("detail", event)

    def test_the_axis_end_hook_writes_a_diagnosis_instead_of_figures(self):
        detail = "review result carried no counters"

        event = self.run_axis_end_hook(
            REVIEW_MODEL="", REVIEW_SESSION="", REVIEW_COST_DETAIL=detail
        )

        self.assertEqual(event["event"], "session-cost")
        self.assertEqual(event["detail"], detail)
        for key in (
            "session", "input_tokens", "output_tokens", "cache_read_tokens",
            "cache_creation_tokens", "total_tokens",
        ):
            self.assertNotIn(key, event)

    def claude_lane(self, account=None, **overrides):
        """A ticket whose reviewer is the Claude lane, on the account the caller names.

        An account named here is a ticket that named one, so its binding is explicit; naming none
        is the ordinary row, inherited on the coordinator's own home.
        """
        return dict(
            workflow="refactor", executor="codex", model=CODEX_MODEL, effort=CODEX_EFFORT,
            review={"vendor": "claude", "model": CLAUDE_MODEL, "effort": CLAUDE_EFFORT},
            account=str(account or self.fixture.config_dir),
            account_mode=EXPLICIT if account else INHERITED,
            **overrides,
        )

    def test_both_reviewing_vendors_use_the_same_installed_command_and_hook_shape(self):
        prompt = self.prompt_for("--log", str(self.machine_log), **self.claude_lane())
        argv = review_command_argv(prompt)

        self.assertEqual(argv[0], "review-bridge")
        self.assertEqual(argv[argv.index("--reviewer") + 1], "claude")
        self.assertEqual(argv[argv.index("--model") + 1], CLAUDE_MODEL)
        self.assertEqual(argv[argv.index("--effort") + 1], CLAUDE_EFFORT)
        self.assertEqual(
            shlex.split(self.hook_command(prompt, "--on-review-start")),
            self.expected_review_hook("running", "claude", CLAUDE_MODEL),
        )

    def test_a_relative_log_reaches_the_child_as_an_absolute_path(self):
        """The review runs in the child's worktree, where a relative path names nothing."""
        table = self.fixture.table([self.fixture.ticket("06", "reviewed")])
        here = os.path.realpath(self.fixture.root)

        result = self.fixture.run_dispatch(
            "render", table, extra=("--log", "run/log.jsonl"), cwd=here
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        argv = review_command_argv(self.fixture.turn("06"))
        command = argv[argv.index("--on-review-start") + 1]
        self.assertIn(f"--log {here}/run/log.jsonl", command)

    def test_a_run_with_no_machine_log_still_renders_a_valid_review_command(self):
        prompt = self.prompt_for()
        argv = review_command_argv(prompt)

        for flag in ("--on-review-start", "--on-axis-end", "--on-review-end"):
            self.assertNotIn(flag, argv)


class ReviewAccountTests(ReviewEventRenderTests):
    """The reviewer of a ticket runs on that ticket's account, whichever account that is.

    The renderer hands the bridge the profile directory the wave table resolved for the row, so
    the review lane spends where the ticket spends. The Codex lane is a different vendor with its
    own credentials and takes none of this.
    """

    def test_the_claude_reviewer_is_launched_on_the_tickets_own_account(self):
        second = self.fixture.root / "claude-config-b"
        second.mkdir()

        prompt = self.prompt_for(**self.claude_lane(account=second))

        argv = review_command_argv(prompt)
        self.assertEqual(argv[argv.index("--account") + 1], str(second))

    def test_a_ticket_that_named_no_account_hands_the_bridge_none(self):
        """An inherited binding reviews on the login the operator is signed into.

        The bridge sets `CLAUDE_CONFIG_DIR` for an account it is given and touches the environment
        for no other, so handing it the default home explicitly is how an account-less reviewer
        came to be told it was not logged in (#110). It is handed nothing instead.
        """
        prompt = self.prompt_for(**self.claude_lane())

        argv = review_command_argv(prompt)
        self.assertEqual(argv[argv.index("--reviewer") + 1], "claude")
        self.assertNotIn("--account", argv)

    def test_the_codex_review_lane_is_handed_no_account(self):
        prompt = self.prompt_for(account=str(self.fixture.config_dir))

        argv = review_command_argv(prompt)
        self.assertEqual(argv[argv.index("--reviewer") + 1], "codex")
        self.assertNotIn("--account", argv)

    def test_a_profile_directory_with_a_space_reaches_the_bridge_as_one_argument(self):
        """A profile directory is the operator's path, and an operator's path can carry a space."""
        second = self.fixture.root / "claude config b"
        second.mkdir()

        prompt = self.prompt_for(**self.claude_lane(account=second))

        argv = review_command_argv(prompt)
        self.assertEqual(argv[argv.index("--account") + 1], str(second))


class ReceiptChannelTests(DispatchTestCase):
    """Where a child's receipts go: a Claude child writes them, only CREW ASK is sent.

    A receipt carries no decision, so waking the coordinator with one buys nothing. The Claude
    lane records `CREW COMPLETE` / `CREW FAILED` / `CREW PARKED` through the machine log's own
    CLI — the same log the Codex bridge already writes — and keeps the cross-session channel for
    the one line the coordinator must answer.
    """

    def setUp(self):
        super().setUp()
        self.machine_log = self.fixture.root / "run" / "log.jsonl"
        # The run's own copy of the log's writer: installed beside the log, so a plugin upgrade
        # mid-run cannot leave the receipt command naming a file that is no longer there (#37).
        self.log_script = self.machine_log.parent / "machine_log.py"

    def prompt_for(self, *extra, **overrides):
        table = self.fixture.table([self.fixture.ticket("06", "receipted", **overrides)])
        result = self.fixture.run_dispatch("render", table, extra=extra)
        self.assertEqual(result.returncode, 0, result.stderr)
        return self.fixture.turn("06")

    def logged_prompt(self):
        return self.prompt_for("--log", str(self.machine_log))

    def test_a_claude_child_records_its_completion_in_the_runs_machine_log(self):
        self.assertIn(
            "When implementation, tests, the review, and commit are complete, run"
            " `git rev-parse HEAD` and record the receipt in the\nrun's machine log yourself —"
            " this receipt is not a message to your coordinator:\n"
            "\n"
            "python3 %s --log %s message \\\n"
            "  --role child --ticket 06 --message 'CREW COMPLETE <sha> ts=<unix time>'"
            % (self.log_script, self.machine_log),
            self.logged_prompt(),
        )

    def test_a_claude_child_records_its_failure_the_same_way(self):
        self.assertIn(
            "If you cannot complete the ticket, record the same way with:\n"
            "CREW FAILED <reason> ts=<unix time>",
            self.logged_prompt(),
        )

    def test_a_claude_child_is_told_not_to_send_its_receipts(self):
        self.assertIn(
            "Send neither receipt with SendMessage: CREW ASK is the only line the coordinator is"
            " woken for.",
            self.logged_prompt(),
        )

    def test_a_claude_child_records_a_parked_receipt_too(self):
        prompt = self.prompt_for(
            "--log", str(self.machine_log), workflow="acceptance", review=None,
        )
        self.assertIn(
            "Commit your preparation and the checklist, then park: record the receipt in the run's"
            " machine log\nyourself — this receipt is not a message to your coordinator:\n"
            "\n"
            "python3 %s --log %s message \\\n"
            "  --role child --ticket 06 --message"
            " 'CREW PARKED <absolute checklist path> ts=<unix time>'\n"
            "\n"
            "and stop — the checklist is the human's to run."
            % (self.log_script, self.machine_log),
            prompt,
        )

    def test_a_claude_child_keeps_the_coordinator_channel_for_crew_ask(self):
        prompt = self.logged_prompt()
        self.assertIn(
            f"Reply with SendMessage to\n`{COORDINATOR_NAME}`; ListAgents shows the ref to attach"
            " on first send.",
            prompt,
        )
        self.assertIn("CREW ASK 06 <design|scope|doc-conflict|stuck|wrap-up>", prompt)

    def test_a_codex_child_keeps_the_sendable_receipt_its_bridge_reads(self):
        """Its review hooks use the log writer, but its completion stays a bridge receipt."""
        prompt = self.prompt_for(
            "--log", str(self.machine_log),
            workflow="refactor", executor="codex", model=CODEX_MODEL, effort=CODEX_EFFORT,
            review={"vendor": "claude", "model": CLAUDE_MODEL, "effort": CLAUDE_EFFORT},
        )
        self.assertIn(
            "When characterization tests, refactor, the review, and commit are complete, run"
            " `git rev-parse HEAD` and send all 40 characters of\nits output:\n"
            "CREW COMPLETE <sha> ts=<unix time>\n"
            "If you cannot complete the ticket, send:\n"
            "CREW FAILED <reason> ts=<unix time>",
            prompt,
        )
        self.assertNotIn("--role child", prompt)
        self.assertIn("CREW ASK 06 <design|scope|doc-conflict|stuck|wrap-up>", prompt)

    def test_a_run_with_no_machine_log_leaves_the_receipt_a_message(self):
        """There is no log to name, so the sendable receipt is what a child can still do."""
        prompt = self.prompt_for()
        self.assertNotIn("machine_log.py", prompt)
        self.assertIn(
            "When implementation, tests, the review, and commit are complete, run"
            " `git rev-parse HEAD` and send all 40 characters of\nits output:\n"
            "CREW COMPLETE <sha> ts=<unix time>",
            prompt,
        )



class BareVerbLineTests(DispatchTestCase):
    """The rule that decides whether a receipt parses is stated where the verbs are taught.

    It used to live only in the machine log's own regex comment, so a child following its
    instructions to the letter could still send a receipt the grammar refused (#105).
    """

    SENTENCE = (
        "The verb line stands alone: it is the whole of that message's final line, and any prose"
        "\nyou add goes on the lines above it, never on the line itself."
    )

    def setUp(self):
        super().setUp()
        self.machine_log = self.fixture.root / "run" / "log.jsonl"

    def prompt_for(self, *extra, **overrides):
        table = self.fixture.table([self.fixture.ticket("06", "receipted", **overrides)])
        result = self.fixture.run_dispatch("render", table, extra=extra)
        self.assertEqual(result.returncode, 0, result.stderr)
        return self.fixture.turn("06")

    def test_a_sendable_receipt_is_taught_the_bare_line(self):
        self.assertIn(self.SENTENCE, self.prompt_for())

    def test_a_recorded_receipt_is_taught_the_bare_line(self):
        self.assertIn(self.SENTENCE, self.prompt_for("--log", str(self.machine_log)))

    def test_a_parking_workflow_is_taught_the_bare_line_on_both_channels(self):
        self.assertIn(
            self.SENTENCE, self.prompt_for(workflow="acceptance", review=None)
        )
        self.assertIn(
            self.SENTENCE,
            self.prompt_for("--log", str(self.machine_log), workflow="acceptance", review=None),
        )

class ReviewCallerBudgetTests(DispatchTestCase):
    """Both review lanes receive the caller budget AgentCrew owns."""

    def prompt_for(self, **overrides):
        table = self.fixture.table([self.fixture.ticket("06", "reviewed", **overrides)])
        result = self.fixture.run_dispatch("render", table)
        self.assertEqual(result.returncode, 0, result.stderr)
        return self.fixture.turn("06")

    def test_the_codex_review_lane_carries_the_callers_budget(self):
        prompt = self.prompt_for()
        self.assertNotIn("Rounds.", prompt)
        self.assertIn(RENDERED_RUN_AGAIN_BUDGET, " ".join(prompt.split()))

    def test_the_claude_review_lane_carries_the_callers_budget(self):
        prompt = self.prompt_for(
            workflow="refactor", executor="codex", model=CODEX_MODEL, effort=CODEX_EFFORT,
            review={"vendor": "claude", "model": CLAUDE_MODEL, "effort": CLAUDE_EFFORT},
        )
        self.assertNotIn("Rounds.", prompt)
        self.assertIn(RENDERED_RUN_AGAIN_BUDGET, " ".join(prompt.split()))


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

    def test_a_codex_child_reviewed_by_claude_gets_the_same_installed_command(self):
        prompt = self.prompt_for(
            workflow="refactor", executor="codex", model=CODEX_MODEL, effort=CODEX_EFFORT,
            review={"vendor": "claude", "model": CLAUDE_MODEL, "effort": CLAUDE_EFFORT},
        )
        self.assertIn(
            f"Review: claude at model {CLAUDE_MODEL}, effort {CLAUDE_EFFORT}.", prompt
        )
        self.assertEqual(
            review_command_argv(prompt)[0:3], ["review-bridge", "--reviewer", "claude"]
        )


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

    def test_a_claude_review_lane_uses_only_the_installed_review_switch_command(self):
        result = self.fixture.run_dispatch("render", self.table)

        self.assertEqual(result.returncode, 0, result.stderr)
        prompt = self.fixture.turn("07")
        self.assertEqual(
            review_command_argv(prompt),
            [
                "review-bridge",
                "--reviewer", "claude",
                "--cwd", str(self.fixture.repo / ".claude" / "worktrees" / "07-codex-child"),
                "--model", CLAUDE_MODEL,
                "--effort", CLAUDE_EFFORT,
                "--base", self.fixture.base_commit,
                "--spec", self.ticket["path"],
                "--axis", "both",
            ],
        )
        self.assertNotIn("tui_review_bridge.py", prompt)
        self.assertNotIn("claude_review_bridge.py", prompt)

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

    def test_a_logged_codex_launch_carries_the_log_and_ticket_to_the_bridge(self):
        log = self.fixture.root / "run" / "log.jsonl"

        result = self.fixture.run_dispatch(
            "dispatch", self.table, extra=("--log", str(log)),
        )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        argv = self.fixture.codex_launches()[0]["argv"]
        self.assertIn("--machine-log", argv)
        self.assertEqual(
            argv[argv.index("--machine-log") + 1],
            os.path.abspath(log),
        )
        self.assertIn("--ticket", argv)
        self.assertEqual(argv[argv.index("--ticket") + 1], "07")

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


class RelativeOutDirTests(DispatchTestCase):
    """The artifact list is absolute whatever the caller spelled, because the child never shares
    this process's working directory: the launch line runs in the child's own worktree."""

    def test_render_records_every_artifact_path_absolute(self):
        table = self.fixture.table([self.fixture.ticket("06", "relative-render")])

        result = self.fixture.run_dispatch(
            "render", table, out_dir="render", cwd=self.fixture.root,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        child = json.loads(result.stdout)["children"][0]
        self.assertTrue(os.path.isabs(child["turnFile"]), child)
        self.assertTrue(os.path.isabs(child["launchJson"]), child)
        self.assertEqual(
            os.path.realpath(child["turnFile"]),
            os.path.realpath(self.fixture.out_dir / "06.turn.txt"),
        )
        self.assertEqual(
            os.path.realpath(child["launchJson"]),
            os.path.realpath(self.fixture.out_dir / "06.agents.json"),
        )

    def test_a_relative_out_dir_still_launches_the_child_with_its_first_turn(self):
        table = self.fixture.table([self.fixture.ticket("06", "relative-launch")])

        result = self.fixture.run_dispatch(
            "dispatch", table, out_dir="render", cwd=self.fixture.root,
        )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        launches = self.fixture.launches()
        self.assertEqual(len(launches), 1, launches)
        argv = launches[0]["argv"]
        agents = json.loads(argv[argv.index("--agents") + 1])
        self.assertEqual(
            next(iter(agents.values()))["initialPrompt"], self.fixture.turn("06")
        )

    def test_a_relative_out_dir_hands_the_codex_bridge_an_absolute_turn_file(self):
        ticket = self.fixture.ticket(
            "07", "relative-codex", executor="codex", model=CODEX_MODEL, effort=CODEX_EFFORT,
            review={"vendor": "claude", "model": CLAUDE_MODEL, "effort": CLAUDE_EFFORT},
        )
        table = self.fixture.table([ticket])

        result = self.fixture.run_dispatch(
            "dispatch", table, out_dir="render", cwd=self.fixture.root,
        )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        argv = self.fixture.codex_launches()[0]["argv"]
        prompt_file = argv[argv.index("--prompt-file") + 1]
        self.assertTrue(os.path.isabs(prompt_file), prompt_file)
        self.assertEqual(
            os.path.realpath(prompt_file),
            os.path.realpath(self.fixture.out_dir / "07.turn.txt"),
        )


class DetachedWindowTests(DispatchTestCase):
    def test_a_child_window_is_created_without_taking_the_focus(self):
        table = self.fixture.table([self.fixture.ticket("06", "detached")])

        result = self.fixture.run_dispatch("dispatch", table)

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        new_windows = [call["argv"] for call in self.fixture.tmux_calls()
                       if call["argv"][0] == "new-window"]
        self.assertEqual(len(new_windows), 1, new_windows)
        self.assertIn("-d", new_windows[0])


class LaunchEventTests(DispatchTestCase):
    def setUp(self):
        super().setUp()
        self.log = self.fixture.root / "log.jsonl"

    def launch_events(self):
        return [record for record in self.fixture.log_records(self.log)
                if record["event"] == "launch"]

    def test_a_claude_launch_is_recorded_before_and_after_verification(self):
        tickets = [
            self.fixture.ticket("06", "claude-child"),
            self.fixture.ticket(
                "07", "codex-child", executor="codex", model=CODEX_MODEL, effort=CODEX_EFFORT,
                review={"vendor": "claude", "model": CLAUDE_MODEL, "effort": CLAUDE_EFFORT},
            ),
        ]
        table = self.fixture.table(tickets)

        result = self.fixture.run_dispatch(
            "dispatch", table, extra=("--log", str(self.log)),
        )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        events = self.launch_events()
        self.assertEqual([event["ticket"] for event in events], ["06", "06", "07"], events)
        started, verified, codex = events
        self.assertEqual(started["child"], "")
        self.assertEqual(verified["executor"], "claude")
        self.assertEqual(verified["model"], CLAUDE_MODEL)
        self.assertEqual(verified["effort"], CLAUDE_EFFORT)
        self.assertEqual(verified["workflow"], "tdd")
        self.assertEqual(verified["child"], "stub-child-1")
        self.assertEqual(verified["branch"], "worktree-06-claude-child")
        self.assertEqual(
            verified["worktree"],
            str(self.fixture.repo / ".claude" / "worktrees" / "06-claude-child"),
        )
        self.assertEqual(started["window"], verified["window"])
        self.assertTrue(verified["window"].startswith("@"), verified)
        self.assertEqual(codex["executor"], "codex")
        self.assertEqual(codex["model"], CODEX_MODEL)
        self.assertEqual(codex["effort"], CODEX_EFFORT)
        self.assertEqual(codex["branch"], "worktree-07-codex-child")

    def test_a_child_that_never_started_earns_no_launch_event(self):
        worktree = self.fixture.repo / ".claude" / "worktrees" / "06-dispatch-renderer"
        worktree.parent.mkdir(parents=True)
        run_git(self.fixture.repo, "worktree", "add", "-b", "someone-elses-branch",
                str(worktree), self.fixture.base_commit)
        table = self.fixture.table([self.fixture.ticket("06", "dispatch-renderer")])

        result = self.fixture.run_dispatch(
            "dispatch", table, extra=("--log", str(self.log)),
        )

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(self.launch_events(), [])

    def test_a_live_child_that_fails_verification_is_recorded_for_adoption(self):
        table = self.fixture.table([self.fixture.ticket("06", "dispatch-renderer")])
        (self.fixture.bin_dir / "claude").write_text(
            "#!/bin/sh\nif [ \"$1\" = agents ]; then echo '[]'; fi\nexit 0\n"
        )
        (self.fixture.bin_dir / "claude").chmod(0o755)

        result = self.fixture.run_dispatch(
            "dispatch", table,
            extra=("--log", str(self.log), "--verify-timeout", "1"),
        )

        self.assertEqual(result.returncode, 1, result.stdout)
        records = self.fixture.log_records(self.log)
        self.assertEqual(records[0]["event"], "launch", records)
        launch = self.launch_events()[0]
        self.assertEqual(launch["ticket"], "06")
        self.assertEqual(launch["branch"], "worktree-06-dispatch-renderer")
        self.assertEqual(launch["worktree"], str(
            self.fixture.repo / ".claude" / "worktrees" / "06-dispatch-renderer"
        ))
        self.assertTrue(launch["window"].startswith("@"), launch)
        failures = [
            record for record in records
            if record.get("event") != "launch" and record.get("ticket") == "06"
        ]
        self.assertEqual(len(failures), 1, records)
        self.assertEqual(failures[0]["event"], "launch-failed")
        self.assertIn("no entry for this child", failures[0].get("detail", ""))

    def test_a_launch_the_log_could_not_record_fails_the_dispatch(self):
        """A child the log never heard of is a child wave advancement cannot see, so a wave that
        lost one is not a wave that advanced: the line says the child is up, the exit code does
        not say the run may carry on."""
        unwritable = self.fixture.root / "unwritable-log"
        unwritable.mkdir()
        table = self.fixture.table([self.fixture.ticket("06", "dispatch-renderer")])

        result = self.fixture.run_dispatch(
            "dispatch", table, extra=("--log", str(unwritable)),
        )

        self.assertEqual(result.returncode, 1, result.stdout)
        line = result.stdout.strip()
        self.assertTrue(line.startswith("06 launched"), line)
        self.assertIn("log-failed=", line)
        self.assertEqual(len(self.fixture.launches()), 1)

    def test_rendering_writes_no_launch_event(self):
        table = self.fixture.table([self.fixture.ticket("06", "dispatch-renderer")])

        result = self.fixture.run_dispatch("render", table, extra=("--log", str(self.log)))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.fixture.log_records(self.log), [])

    def test_a_wave_dispatched_without_a_log_still_launches(self):
        table = self.fixture.table([self.fixture.ticket("06", "dispatch-renderer")])

        result = self.fixture.run_dispatch("dispatch", table)

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(len(self.fixture.launches()), 1)


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


class AccountRoutingTests(DispatchTestCase):
    """A child launches on its ticket's account and is verified there (#98).

    The wave table carries a concrete account on every row — the profile directory the driver
    resolved the ticket's name to, or the coordinator's own where the ticket named none. These
    cases drive the whole launch: the stub multiplexer carries the window's environment into the
    keys it runs, so the stub CLI writes its transcript into whichever profile the window was put
    on, and a transcript in the routed profile is what proves the launch rather than the arguments
    the launch was recorded with.
    """

    def setUp(self):
        super().setUp()
        self.other = str(self.fixture.other_account)
        self.coordinator_account = str(self.fixture.config_dir)

    def routed(self, number, slug):
        """A ticket that named an account: bound to the second profile, explicitly."""
        return self.fixture.ticket(number, slug, account=self.other, account_mode=EXPLICIT)

    def window_environment(self, name):
        """The `NAME=VALUE` pairs the window for that ticket was created with."""
        for call in self.fixture.tmux_calls():
            argv = call["argv"]
            if argv[0] != "new-window" or argv[argv.index("-n") + 1] != name:
                continue
            return dict(
                argv[index + 1].split("=", 1)
                for index, value in enumerate(argv) if value == "-e"
            )
        return None

    def test_a_ticket_lacking_an_account_is_refused_before_any_launch(self):
        """Ambiguity ended at the wave table (ADR-0014): every row carries a concrete account."""
        table = self.fixture.table([self.fixture.ticket("06", "unrouted", account=None)])

        result = self.fixture.run_dispatch("dispatch", table)

        self.assertEqual(result.returncode, 1, result.stdout)
        offence = [line for line in result.stderr.splitlines() if line.startswith("06")]
        self.assertTrue(offence, result.stderr)
        self.assertIn("Account", offence[0])
        self.assertEqual(self.fixture.launches(), [])
        self.assertEqual(self.fixture.tmux_calls(), [])

    def test_a_child_runs_under_the_account_its_ticket_names(self):
        table = self.fixture.table([self.routed("06", "routed")])

        result = self.fixture.run_dispatch("dispatch", table)

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        launches = self.fixture.launches()
        self.assertEqual(len(launches), 1, launches)
        self.assertEqual(launches[0]["configHome"], self.other)
        self.assertEqual(
            self.fixture.transcripts(self.other), [launches[0]["sessionId"]]
        )
        self.assertEqual(self.fixture.transcripts(self.coordinator_account), [])

    def test_a_shell_in_that_window_sees_the_account_too(self):
        """The window itself is put on the account, so a `claude` typed by hand stays on it."""
        table = self.fixture.table([self.routed("06", "routed")])

        result = self.fixture.run_dispatch("dispatch", table)

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(
            self.window_environment("06"), {"CLAUDE_CONFIG_DIR": self.other}
        )

    def test_a_ticket_naming_no_account_runs_on_the_login_the_run_was_started_from(self):
        """An inherited binding sets nothing: the window is given no configuration home at all.

        The child still lands in the coordinator's profile, because that is the login the run is
        already on — but it lands there by inheriting it, not by having the default home spelled
        out for it, which is a login that can fail where the inherited one works (#110).
        """
        table = self.fixture.table([self.fixture.ticket("06", "unnamed")])

        result = self.fixture.run_dispatch("dispatch", table)

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        launches = self.fixture.launches()
        self.assertEqual(launches[0]["configHome"], self.coordinator_account)
        self.assertEqual(
            self.fixture.transcripts(self.coordinator_account), [launches[0]["sessionId"]]
        )
        self.assertEqual(self.fixture.transcripts(self.other), [])
        self.assertEqual(self.window_environment("06"), {})

    def test_an_account_mode_the_table_cannot_mean_is_refused_before_any_launch(self):
        """The half of the binding that says whether the account is set or inherited."""
        table = self.fixture.table(
            [self.fixture.ticket("06", "routed", account=self.other, account_mode="maybe")]
        )

        result = self.fixture.run_dispatch("dispatch", table)

        self.assertEqual(result.returncode, 1, result.stdout)
        offence = [line for line in result.stderr.splitlines() if line.startswith("06")]
        self.assertTrue(offence, result.stderr)
        self.assertIn("Account mode `maybe`", offence[0])
        self.assertEqual(self.fixture.launches(), [])

    def test_a_wave_mixing_two_accounts_launches_every_child_under_its_own(self):
        table = self.fixture.table([
            self.fixture.ticket("06", "on-the-coordinators"),
            self.routed("07", "on-the-other"),
        ])

        result = self.fixture.run_dispatch("dispatch", table)

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 2, result.stdout)
        launched = {entry["argv"][entry["argv"].index("--agent") + 1]: entry
                    for entry in self.fixture.launches()}
        self.assertEqual(launched["crew-06"]["configHome"], self.coordinator_account)
        self.assertEqual(launched["crew-07"]["configHome"], self.other)
        self.assertEqual(
            self.fixture.transcripts(self.coordinator_account),
            [launched["crew-06"]["sessionId"]],
        )
        self.assertEqual(
            self.fixture.transcripts(self.other), [launched["crew-07"]["sessionId"]]
        )

    def test_a_child_present_only_in_its_own_accounts_list_is_still_found(self):
        """Two profiles answer `agents --json` with two disjoint lists; the routed one is read."""
        table = self.fixture.table([self.routed("06", "routed")])

        result = self.fixture.run_dispatch("dispatch", table)

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(self.fixture.agents_listed(self.other), ["stub-child-1"])
        self.assertEqual(self.fixture.agents_listed(self.coordinator_account), [])

    def test_the_transcript_read_that_asserts_the_model_reads_the_childs_account(self):
        """A downgrade written into the routed profile is still caught, so that profile was read."""
        table = self.fixture.table([self.routed("06", "routed")])

        result = self.fixture.run_dispatch(
            "dispatch", table, extra=("--verify-timeout", "5"),
            env_overrides={"AGENTCREW_STUB_TRANSCRIPT_MODEL": "claude-haiku-4-5-20251001"},
        )

        self.assertEqual(result.returncode, 1, result.stdout)
        line = result.stdout.strip()
        self.assertTrue(line.startswith("06 FAILED"), line)
        self.assertIn("claude-haiku-4-5-20251001", line)
        self.assertIn(CLAUDE_MODEL, line)

    def test_the_launch_event_records_the_account_the_child_launched_under(self):
        log = self.fixture.root / "log.jsonl"
        table = self.fixture.table([
            self.fixture.ticket("06", "on-the-coordinators"),
            self.fixture.ticket("07", "on-the-other", account=self.other, account_mode=EXPLICIT),
        ])

        result = self.fixture.run_dispatch(
            "dispatch", table, extra=("--log", str(log)),
        )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        events = [record for record in self.fixture.log_records(log)
                  if record["event"] == "launch"]
        self.assertEqual(
            [(event["ticket"], event["account"]) for event in events],
            [
                ("06", self.coordinator_account),
                ("06", self.coordinator_account),
                ("07", self.other),
                ("07", self.other),
            ],
            events,
        )

    def test_the_verification_timeout_names_the_tickets_account(self):
        """With no login check anywhere, this timeout is where an unauthenticated profile shows."""
        table = self.fixture.table(
            [self.fixture.ticket("06", "routed", account=self.other, account_mode=EXPLICIT)]
        )
        # A CLI that starts nothing: the routed account's list stays empty, as an unauthenticated
        # profile's does.
        (self.fixture.bin_dir / "claude").write_text(
            "#!/bin/sh\nif [ \"$1\" = agents ]; then echo '[]'; fi\nexit 0\n"
        )
        (self.fixture.bin_dir / "claude").chmod(0o755)

        result = self.fixture.run_dispatch("dispatch", table, extra=("--verify-timeout", "1"))

        self.assertEqual(result.returncode, 1, result.stdout)
        line = result.stdout.strip()
        self.assertTrue(line.startswith("06 FAILED"), line)
        self.assertIn(self.other, line)

    def test_a_hook_variable_cannot_move_a_child_off_its_tickets_account(self):
        """The window carries the account; the launch line's hook variables never overrule it."""
        table = self.fixture.table(
            [self.fixture.ticket("06", "routed", account=self.other, account_mode=EXPLICIT)],
            launch_hook={
                "command": "true",
                "env": {"CLAUDE_CONFIG_DIR": self.coordinator_account},
            },
        )

        result = self.fixture.run_dispatch("dispatch", table)

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        launches = self.fixture.launches()
        self.assertEqual(launches[0]["configHome"], self.other)
        self.assertEqual(
            self.fixture.transcripts(self.other), [launches[0]["sessionId"]]
        )
        self.assertEqual(self.fixture.transcripts(self.coordinator_account), [])

    def test_an_account_that_is_not_an_absolute_path_is_refused_before_any_launch(self):
        """ADR-0007: the child reads this path in its own worktree, not in the dispatcher's."""
        table = self.fixture.table(
            [self.fixture.ticket("06", "relative-account", account="claude-config")]
        )

        result = self.fixture.run_dispatch("dispatch", table)

        self.assertEqual(result.returncode, 1, result.stdout)
        offence = [line for line in result.stderr.splitlines() if line.startswith("06")]
        self.assertTrue(offence, result.stderr)
        self.assertIn("claude-config", offence[0])
        self.assertIn("absolute path", offence[0])
        self.assertEqual(self.fixture.launches(), [])
        self.assertEqual(self.fixture.tmux_calls(), [])

    def test_a_codex_childs_launch_event_records_no_claude_account(self):
        """A Codex child launches on its own vendor's credentials, not on a Claude profile."""
        log = self.fixture.root / "log.jsonl"
        table = self.fixture.table([
            self.fixture.ticket("06", "claude-child", account=self.other, account_mode=EXPLICIT),
            self.fixture.ticket(
                "07", "codex-child", account=self.other, account_mode=EXPLICIT,
                executor="codex", model=CODEX_MODEL, effort=CODEX_EFFORT,
                review={"vendor": "claude", "model": CLAUDE_MODEL, "effort": CLAUDE_EFFORT},
            ),
        ])

        result = self.fixture.run_dispatch(
            "dispatch", table, extra=("--log", str(log)),
        )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        events = {record["ticket"]: record for record in self.fixture.log_records(log)
                  if record["event"] == "launch"}
        self.assertEqual(events["06"]["account"], self.other)
        self.assertNotIn("account", events["07"])

    def test_a_codex_ticket_in_the_same_wave_is_unaffected(self):
        table = self.fixture.table([
            self.fixture.ticket("06", "claude-child", account=self.other, account_mode=EXPLICIT),
            self.fixture.ticket(
                "07", "codex-child", account=self.other, account_mode=EXPLICIT,
                executor="codex", model=CODEX_MODEL, effort=CODEX_EFFORT,
                review={"vendor": "claude", "model": CLAUDE_MODEL, "effort": CLAUDE_EFFORT},
            ),
        ])

        result = self.fixture.run_dispatch("dispatch", table)

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        self.assertTrue(lines[1].startswith("07 launched codex"), lines)
        argv = self.fixture.codex_launches()[0]["argv"]
        self.assertNotIn("--account", argv)
        self.assertNotIn("CLAUDE_CONFIG_DIR", " ".join(argv))
        self.assertEqual(
            [call["argv"] for call in self.fixture.tmux_calls()
             if call["argv"][0] == "new-window" and call["argv"][
                 call["argv"].index("-n") + 1] == "07"],
            [],
        )


if __name__ == "__main__":
    unittest.main()
