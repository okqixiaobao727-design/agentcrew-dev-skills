import ast
import copy
import dataclasses
import json
import os
import pathlib
import sys
import tempfile
import tomllib
import unittest
from unittest import mock


ASSETS = pathlib.Path(__file__).resolve().parents[1] / "skills" / "crew" / "assets"
PLUGIN_ROOT = ASSETS.parents[2]
sys.path.insert(0, str(ASSETS))

import run_plan  # noqa: E402


WITNESS_MODEL = "claude-sonnet-5"
WITNESS_BUDGET_USD = 2.0


class RunPlanTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        self.feature = self.root / "feature"
        self.feature.mkdir()
        (self.feature / "spec.md").write_text("# Demo\n", encoding="utf-8")
        self.account = self.root / "claude-config"
        self.account.mkdir()
        self.run = {
            "repo_root": str(self.root),
            "spec_path": str(self.feature / "spec.md"),
            "integration_branch": "crew/demo",
            "integration_base_commit": "a" * 40,
            "coordinator_name": "crew-coordinator",
            "coordinator_pid": 1234,
            "coordinator_session": "session-1",
            "coordinator_address": "uds:/tmp/cc-socks/1234.sock",
            "crew_skill_dir": str(ASSETS.parent),
            "tmux_session": "$1:",
            "permission_mode": "acceptEdits",
            "coordinator_config_home": str(self.account),
            "base_branch": "main",
            "return_branch": "main",
            "feature_dir": str(self.feature),
            "repair_model": "claude-sonnet-5",
            "witness_model": WITNESS_MODEL,
            "witness_budget_usd": WITNESS_BUDGET_USD,
            "tracker": "local",
            "declared_accounts": [],
            "codex": {
                "bridge": str(ASSETS / "codex" / "codex_bridge.py"),
                "state_dir": str(self.feature / ".crew" / "codex"),
            },
        }

    def ticket(self, number, title, blocked_by=(), routing=None):
        blockers = "\n".join(f"- #{blocker}" for blocker in blocked_by)
        blocked_section = f"\n## Blocked by\n\n{blockers}\n" if blockers else ""
        routing = routing or (
            "Workflow: direct\n"
            "Executor: claude\n"
            "Model: claude-opus-5\n"
            "Effort: medium\n"
        )
        (self.feature / f"{number}.md").write_text(
            f"# {title}\n"
            f"{blocked_section}\n"
            "## Routing\n\n"
            f"{routing}",
            encoding="utf-8",
        )

    def test_build_returns_immutable_ordered_values_and_assigns_dependency_waves(self):
        self.ticket("01", "Foundation")
        self.ticket("02", "Consumer", blocked_by=("01",))

        plan = run_plan.build(self.feature, self.run)

        self.assertEqual(plan.run.repo_root, str(self.root))
        self.assertEqual([wave.number for wave in plan.waves], [1, 2])
        self.assertEqual([ticket.id for ticket in plan.tickets], ["01", "02"])
        self.assertEqual(plan.ticket("02").blocked_by, ("01",))
        self.assertEqual(
            plan.ticket("01").binding,
            run_plan.accounts.inherited(self.account),
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            plan.ticket("01").title = "changed"

    def test_missing_witness_config_uses_its_own_shipped_defaults_in_the_plan_json(self):
        self.ticket("01", "Foundation")
        metadata = copy.deepcopy(self.run)
        metadata.pop("witness_model")
        metadata.pop("witness_budget_usd")
        independent = self.root / "defaults.toml"
        independent.write_text(
            "[repair]\nmodel = \"claude-opus-5\"\n"
            "[witness]\nmodel = \"claude-sonnet-5\"\nbudget_usd = 2.0\n",
            encoding="utf-8",
        )
        path = self.root / "wave-table.json"

        with mock.patch.object(run_plan, "DEFAULT_CONFIG", independent):
            plan = run_plan.build(self.feature, metadata)
            plan.write(path)

        document = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(document["run"]["witness_model"], WITNESS_MODEL)
        self.assertEqual(document["run"]["witness_budget_usd"], WITNESS_BUDGET_USD)

    def test_an_aliased_witness_model_is_rejected(self):
        self.ticket("01", "Foundation")
        metadata = copy.deepcopy(self.run)
        metadata["witness_model"] = "sonnet"

        with self.assertRaisesRegex(run_plan.RunPlanError, "witness model.*alias"):
            run_plan.build(self.feature, metadata)

    def test_the_shipped_witness_defaults_are_the_advisor_ruled_literals(self):
        with (PLUGIN_ROOT / "config" / "agentcrew.default.toml").open("rb") as handle:
            witness = tomllib.load(handle)["witness"]

        self.assertEqual(witness["model"], WITNESS_MODEL)
        self.assertEqual(witness["budget_usd"], WITNESS_BUDGET_USD)

    def test_witness_routing_names_the_executor_that_launches_the_session(self):
        executor, model, budget_usd = run_plan.witness_routing(
            WITNESS_MODEL, WITNESS_BUDGET_USD
        )

        self.assertEqual(executor, "claude")
        self.assertEqual(model, WITNESS_MODEL)
        self.assertEqual(budget_usd, WITNESS_BUDGET_USD)

    def test_build_resolves_a_named_account_once_and_rejects_duplicate_ticket_ids(self):
        profile = self.root / "second-profile"
        profile.mkdir()
        registry = self.root / "accounts.toml"
        registry.write_text(f'[accounts]\nsecond = "{profile}"\n', encoding="utf-8")
        self.ticket("01", "Named", routing=(
            "Workflow: direct\n"
            "Executor: claude\n"
            "Model: claude-opus-5\n"
            "Effort: medium\n"
            "Account: second\n"
        ))
        named_run = dict(self.run, declared_accounts=["second"])
        with mock.patch.dict(os.environ, {"AGENTCREW_ACCOUNT_REGISTRY": str(registry)}):
            ticket = run_plan.build(self.feature, named_run).ticket("01")
        self.assertEqual(ticket.binding, run_plan.accounts.explicit(profile))

        (self.feature / "01-copy.md").write_text(
            (self.feature / "01.md").read_text(encoding="utf-8"), encoding="utf-8"
        )
        with mock.patch.dict(os.environ, {"AGENTCREW_ACCOUNT_REGISTRY": str(registry)}):
            with self.assertRaisesRegex(run_plan.RunPlanError, "listed twice"):
                run_plan.build(self.feature, named_run)

    def test_write_load_round_trip_and_named_queries_observe_replacements(self):
        self.ticket("01", "Foundation")
        self.ticket("02", "Consumer", blocked_by=("01",))
        table_path = self.root / "wave-table.json"
        built = run_plan.build(self.feature, self.run)

        built.write(table_path)
        loaded = run_plan.load(table_path)

        self.assertEqual(loaded, built)
        self.assertEqual([ticket.id for ticket in loaded.wave("1").tickets], ["01"])
        self.assertEqual(loaded.following_wave(1).number, 2)
        self.assertIsNone(loaded.following_wave(2))
        self.assertEqual(loaded.descendants(("01",)), ("02",))
        with self.assertRaisesRegex(run_plan.RunPlanError, "wave 99"):
            loaded.wave(99)
        with self.assertRaisesRegex(run_plan.RunPlanError, "ticket 99"):
            loaded.descendants(("99",))
        with self.assertRaisesRegex(run_plan.RunPlanError, "ticket 99"):
            loaded.ticket("99")

        replacement = json.loads(table_path.read_text(encoding="utf-8"))
        replacement["waves"][0]["tickets"][0]["title"] = "Replaced"
        table_path.write_text(json.dumps(replacement), encoding="utf-8")
        self.assertEqual(run_plan.load(table_path).ticket("01").title, "Replaced")

    def test_a_table_written_without_the_coordinator_address_still_loads(self):
        """A run already under way when the address shipped resumes; it is not a required key."""
        self.ticket("01", "Foundation")
        table_path = self.root / "wave-table.json"
        run_plan.build(self.feature, self.run).write(table_path)
        written = json.loads(table_path.read_text(encoding="utf-8"))
        del written["run"]["coordinator_address"]
        table_path.write_text(json.dumps(written), encoding="utf-8")

        loaded = run_plan.load(table_path)

        self.assertEqual(loaded.run.coordinator_address, "")

    def test_build_rejects_incomplete_cycles_and_unresolvable_account_bindings(self):
        self.ticket("01", "Missing", blocked_by=("99",))
        with self.assertRaisesRegex(run_plan.RunPlanError, "99.*no ticket"):
            run_plan.build(self.feature, self.run)

        self.ticket("01", "First", blocked_by=("02",))
        self.ticket("02", "Second", blocked_by=("01",))
        with self.assertRaisesRegex(run_plan.RunPlanError, "cycle"):
            run_plan.build(self.feature, self.run)

        self.ticket("01", "Named", routing=(
            "Workflow: direct\n"
            "Executor: claude\n"
            "Model: claude-opus-5\n"
            "Effort: medium\n"
            "Account: second\n"
        ))
        (self.feature / "02.md").unlink()
        registry = self.root / "accounts.toml"
        registry.write_text("[accounts]\n", encoding="utf-8")
        named_run = dict(self.run, declared_accounts=["second"])
        with mock.patch.dict(os.environ, {"AGENTCREW_ACCOUNT_REGISTRY": str(registry)}):
            with self.assertRaisesRegex(run_plan.RunPlanError, "registry.*second"):
                run_plan.build(self.feature, named_run)

    def test_build_rejects_a_review_line_that_is_not_a_complete_lane(self):
        self.ticket("01", "Malformed review", routing=(
            "Workflow: direct\n"
            "Executor: claude\n"
            "Model: claude-opus-5\n"
            "Effort: medium\n"
            "Review: malformed lane\n"
        ))

        with self.assertRaisesRegex(run_plan.RunPlanError, "Review.*vendor.*model.*effort"):
            run_plan.build(self.feature, self.run)

    def test_build_rejects_an_empty_review_line_instead_of_treating_it_as_absent(self):
        self.ticket("01", "Empty review", routing=(
            "Workflow: direct\n"
            "Executor: claude\n"
            "Model: claude-opus-5\n"
            "Effort: medium\n"
            "Review:\n"
        ))

        with self.assertRaisesRegex(run_plan.RunPlanError, "Review.*vendor.*model.*effort"):
            run_plan.build(self.feature, self.run)

    def test_load_applies_the_complete_routing_and_dependency_contract(self):
        self.ticket("01", "Foundation")
        self.ticket("02", "Consumer", blocked_by=("01",))
        path = self.root / "wave-table.json"
        run_plan.build(self.feature, self.run).write(path)
        valid = json.loads(path.read_text(encoding="utf-8"))

        cases = {
            "duplicate ticket": (
                lambda value: value["waves"][1]["tickets"].append(
                    copy.deepcopy(value["waves"][0]["tickets"][0])
                ),
                "listed twice",
            ),
            "noncanonical wave number": (
                lambda value: value["waves"][0].update(wave=2),
                "wave numbers",
            ),
            "unknown workflow": (
                lambda value: value["waves"][0]["tickets"][0].update(workflow="yolo"),
                "Workflow `yolo`",
            ),
            "unknown executor": (
                lambda value: value["waves"][0]["tickets"][0].update(executor="gemini"),
                "Executor `gemini`",
            ),
            "unknown effort": (
                lambda value: value["waves"][0]["tickets"][0].update(effort="heroic"),
                "Effort `heroic`",
            ),
            "model alias": (
                lambda value: value["waves"][0]["tickets"][0].update(model="opus"),
                "full model ID",
            ),
            "review on no-lane workflow": (
                lambda value: value["waves"][0]["tickets"][0].update(review={
                    "vendor": "codex", "model": "gpt-5.6-sol", "effort": "medium",
                }),
                "takes none",
            ),
            "missing review lane": (
                lambda value: value["waves"][0]["tickets"][0].update(workflow="tdd"),
                "lacks Review",
            ),
            "reviewer equals executor": (
                lambda value: value["waves"][0]["tickets"][0].update(
                    workflow="tdd",
                    review={
                        "vendor": "claude", "model": "claude-opus-5", "effort": "medium",
                    },
                ),
                "own Executor",
            ),
            "relative account": (
                lambda value: value["waves"][0]["tickets"][0].update(account="relative"),
                "absolute path",
            ),
            "unknown account mode": (
                lambda value: value["waves"][0]["tickets"][0].update(account_mode="maybe"),
                "Account mode `maybe`",
            ),
            "missing dependency": (
                lambda value: value["waves"][1]["tickets"][0].update(blocked_by=["99"]),
                "99.*no ticket",
            ),
            "wrong dependency frontier": (
                lambda value: value["waves"][1]["tickets"][0].update(blocked_by=[]),
                "dependency frontier",
            ),
            "codex ticket without codex metadata": (
                lambda value: (
                    value["run"].pop("codex"),
                    value["waves"][0]["tickets"][0].update(executor="codex"),
                ),
                "codex bridge",
            ),
        }
        for name, (mutate, message) in cases.items():
            with self.subTest(name):
                document = copy.deepcopy(valid)
                mutate(document)
                path.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaisesRegex(run_plan.RunPlanError, message):
                    run_plan.load(path)

    def test_load_strictly_rejects_malformed_roots_run_metadata_waves_and_tickets(self):
        path = self.root / "wave-table.json"
        cases = (
            ([], "not a JSON object"),
            ({}, "no run section"),
            ({"run": {}}, "no list of waves"),
            ({"run": {}, "waves": []}, "no waves"),
            ({"run": self.run, "waves": [None]}, "not a wave object"),
            ({"run": self.run, "waves": [{"wave": 1, "tickets": [None]}]},
             "not a ticket object"),
        )
        for document, message in cases:
            with self.subTest(message):
                path.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaisesRegex(run_plan.RunPlanError, message):
                    run_plan.load(path)

    def test_load_rejects_malformed_metadata_instead_of_coercing_or_dropping_it(self):
        self.ticket("01", "Foundation")
        path = self.root / "wave-table.json"
        run_plan.build(self.feature, self.run).write(path)
        valid = json.loads(path.read_text(encoding="utf-8"))
        cases = {
            "relative repository": (
                lambda value: value["run"].update(repo_root="relative"),
                "repo_root.*absolute path",
            ),
            "relative spec": (
                lambda value: value["run"].update(spec_path="spec.md"),
                "spec_path.*absolute path",
            ),
            "relative skill directory": (
                lambda value: value["run"].update(crew_skill_dir="skills/crew"),
                "crew_skill_dir.*absolute path",
            ),
            "relative coordinator home": (
                lambda value: value["run"].update(coordinator_config_home=".claude"),
                "coordinator_config_home.*absolute path",
            ),
            "noninteger coordinator pid": (
                lambda value: value["run"].update(coordinator_pid="1504"),
                "coordinator_pid.*positive integer",
            ),
            "nonstring coordinator address": (
                lambda value: value["run"].update(coordinator_address=1504),
                "coordinator_address.*string",
            ),
            "declared accounts object": (
                lambda value: value["run"].update(declared_accounts={"work": True}),
                "declared_accounts.*list",
            ),
            "declared account number": (
                lambda value: value["run"].update(declared_accounts=[7]),
                "declared_accounts.*non-empty strings",
            ),
            "codex string": (
                lambda value: value["run"].update(codex="configured"),
                "codex.*object",
            ),
            "codex missing state directory": (
                lambda value: value["run"].update(codex={"bridge": "/bridge"}),
                "codex.*state_dir",
            ),
            "launch hook environment list": (
                lambda value: value["run"].update(
                    launch_hook={"command": "prepare", "env": []}
                ),
                "launch_hook.env.*object",
            ),
            "missing repair model": (
                lambda value: value["run"].update(repair_model=None),
                "repair model",
            ),
            "unknown tracker": (
                lambda value: value["run"].update(tracker="jira"),
                "tracker.*jira",
            ),
        }
        for name, (mutate, message) in cases.items():
            with self.subTest(name):
                document = copy.deepcopy(valid)
                mutate(document)
                path.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaisesRegex(run_plan.RunPlanError, message):
                    run_plan.load(path)

    def test_load_rejects_malformed_ticket_fields_and_noninteger_wave_numbers(self):
        self.ticket("01", "Foundation")
        path = self.root / "wave-table.json"
        run_plan.build(self.feature, self.run).write(path)
        valid = json.loads(path.read_text(encoding="utf-8"))
        cases = {
            "numeric title": (
                lambda value: value["waves"][0]["tickets"][0].update(title=7),
                "Title.*non-empty string",
            ),
            "numeric review model": (
                lambda value: value["waves"][0]["tickets"][0].update(
                    workflow="tdd",
                    review={"vendor": "codex", "model": 7, "effort": "medium"},
                ),
                "Review model.*non-empty string",
            ),
            "fractional wave": (
                lambda value: value["waves"][0].update(wave=1.5),
                "wave number.*positive integer",
            ),
        }
        for name, (mutate, message) in cases.items():
            with self.subTest(name):
                document = copy.deepcopy(valid)
                mutate(document)
                path.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaisesRegex(run_plan.RunPlanError, message):
                    run_plan.load(path)


class RunPlanSourceGuardTests(unittest.TestCase):
    """Keep Wave-table structure and duplicated queries out of migrated production callers."""

    READERS = {
        ASSETS / "driver" / "driver.py": "run_plan.load",
        ASSETS / "dispatch" / "dispatch.py": "run_plan.load",
        ASSETS / "advance.py": "run_plan.load",
        ASSETS / "merge_driver.py": "run_plan.load",
        ASSETS / "monitor" / "monitor.py": "run_plan.load",
        PLUGIN_ROOT / "skills" / "route" / "assets" / "stage" / "stage.py": "run_plan.build",
    }
    REPLACED_HELPERS = {
        "walk_tickets", "wave_tickets", "every_ticket", "next_wave", "descendants",
        "read_tickets", "routing_problems", "graph_problems", "account_problems",
        "normalise_accounts", "assign_waves", "config_problems",
    }
    TABLE_MARKERS = ("table_path", "wave-table", "table_name", "wave_table_name", "args.table")

    def test_migrated_callers_use_the_run_plan_and_do_not_restore_replaced_helpers_or_reads(self):
        problems = []
        for path, seam in self.READERS.items():
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            if seam not in source:
                problems.append(f"{path}: does not call {seam}")
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if node.name in self.REPLACED_HELPERS:
                        problems.append(f"{path}:{node.lineno}: restores {node.name}()")
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if (
                    node.func.attr not in ("load", "loads")
                    or not isinstance(node.func.value, ast.Name)
                    or node.func.value.id != "json"
                    or not node.args
                ):
                    continue
                argument = ast.unparse(node.args[0]).lower()
                if any(marker in argument for marker in self.TABLE_MARKERS):
                    problems.append(
                        f"{path}:{node.lineno}: reads Wave-table JSON outside run_plan"
                    )
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
