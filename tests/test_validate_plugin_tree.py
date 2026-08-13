#!/usr/bin/env python3
"""Behaviour of the plugin-tree validation script, asserted at its CLI seam."""

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest


PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = PLUGIN_ROOT / "scripts" / "validate_plugin_tree.py"
CONFIG = "config/agentcrew.default.toml"


IDENTIFIERS_FILE = ".agentcrew-local-identifiers"
IDENTIFIERS_ENV = "AGENTCREW_LOCAL_IDENTIFIERS"


def run_validator(root, env=None):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root)],
        capture_output=True,
        text=True,
        env={**os.environ, **env} if env else None,
    )


class TreeFixture:
    """A writable copy of the shipped plugin tree, seeded with one defect."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name) / "plugin"
        shutil.copytree(PLUGIN_ROOT, self.root)
        # A maintainer's own identifier list must not decide what these tests reject.
        self.path(IDENTIFIERS_FILE).unlink(missing_ok=True)

    def close(self):
        self._tmp.cleanup()

    def path(self, relative):
        return self.root / relative

    def read(self, relative):
        return self.path(relative).read_text()

    def write(self, relative, text):
        self.path(relative).write_text(text)

    def manifest(self):
        return json.loads(self.read(".claude-plugin/plugin.json"))

    def write_manifest(self, data):
        self.write(".claude-plugin/plugin.json", json.dumps(data, indent=2))

    def drop_config_block(self, header):
        """Remove one `[header]` table and its body from the default config."""
        kept, dropping = [], False
        for line in self.read(CONFIG).splitlines(keepends=True):
            stripped = line.strip()
            if stripped == f"[{header}]":
                dropping = True
                continue
            if dropping and stripped.startswith("["):
                dropping = False
            if not dropping:
                kept.append(line)
        self.write(CONFIG, "".join(kept))

    def set_local_identifiers(self, *tokens, comment="# personal identifiers"):
        """Configure the identifiers this tree's residue lint rejects."""
        self.write(IDENTIFIERS_FILE, comment + "\n" + "\n".join(tokens) + "\n")

    def edit_config(self, old, new):
        text = self.read(CONFIG)
        assert old in text, f"config fixture has no {old!r} to edit"
        self.write(CONFIG, text.replace(old, new, 1))


class ValidatePluginTreeTests(unittest.TestCase):
    def setUp(self):
        self.tree = TreeFixture()
        self.addCleanup(self.tree.close)

    def assert_rejects(self, mentioning):
        result = run_validator(self.tree.root)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(mentioning, result.stdout + result.stderr)

    def test_shipped_skeleton_passes(self):
        result = run_validator(PLUGIN_ROOT)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_copy_of_the_skeleton_passes(self):
        result = run_validator(self.tree.root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    # --- broken tree ---------------------------------------------------

    def test_missing_plugin_manifest_is_rejected(self):
        self.tree.path(".claude-plugin/plugin.json").unlink()
        self.assert_rejects(".claude-plugin/plugin.json")

    def test_unparsable_plugin_manifest_is_rejected(self):
        self.tree.write(".claude-plugin/plugin.json", "{ not json")
        self.assert_rejects(".claude-plugin/plugin.json")

    def test_manifest_listing_an_absent_skill_is_rejected(self):
        data = self.tree.manifest()
        data["skills"].append("./skills/ghost")
        self.tree.write_manifest(data)
        self.assert_rejects("skills/ghost")

    def test_skill_without_a_skill_md_is_rejected(self):
        self.tree.path("skills/crew/SKILL.md").unlink()
        self.assert_rejects("skills/crew")

    def test_skill_md_without_frontmatter_is_rejected(self):
        self.tree.write("skills/crew/SKILL.md", "# Crew\n")
        self.assert_rejects("skills/crew")

    def test_skill_name_disagreeing_with_its_directory_is_rejected(self):
        text = self.tree.read("skills/crew/SKILL.md")
        self.tree.write("skills/crew/SKILL.md", text.replace("name: crew", "name: krew", 1))
        self.assert_rejects("krew")

    def test_skill_present_on_disk_but_absent_from_the_manifest_is_rejected(self):
        data = self.tree.manifest()
        data["skills"] = [entry for entry in data["skills"] if not entry.endswith("/route")]
        self.tree.write_manifest(data)
        self.assert_rejects("skills/route")

    def test_a_third_skill_is_rejected(self):
        extra = self.tree.path("skills/extra")
        extra.mkdir()
        (extra / "SKILL.md").write_text("---\nname: extra\ndescription: A third skill.\n---\n")
        data = self.tree.manifest()
        data["skills"].append("./skills/extra")
        self.tree.write_manifest(data)
        self.assert_rejects("extra")

    def test_skill_slot_listed_twice_is_rejected(self):
        data = self.tree.manifest()
        data["skills"].append("./skills/route")
        self.tree.write_manifest(data)
        self.assert_rejects("skills/route")

    def test_legacy_skill_name_in_any_shipped_file_is_rejected(self):
        self.tree.write("skills/crew/NOTES.txt", "Children report ORCHESTRATE COMPLETE.\n")
        self.assert_rejects("ORCHESTRATE")

    def test_legacy_skill_name_in_a_skill_md_is_rejected(self):
        self.tree.write(
            "skills/crew/SKILL.md",
            self.tree.read("skills/crew/SKILL.md") + "\nChildren report ORCHESTRATE COMPLETE.\n",
        )
        self.assert_rejects("ORCHESTRATE")

    def test_skill_link_to_a_missing_file_is_rejected(self):
        self.tree.write(
            "skills/route/SKILL.md",
            self.tree.read("skills/route/SKILL.md").replace(
                "agentcrew.default.toml", "agentcrew.missing.toml"
            ),
        )
        self.assert_rejects("agentcrew.missing.toml")

    def test_skill_link_in_angle_brackets_to_a_missing_file_is_rejected(self):
        self.tree.write(
            "skills/route/SKILL.md",
            self.tree.read("skills/route/SKILL.md") + "\nSee [gone](<missing file.md>).\n",
        )
        self.assert_rejects("missing file.md")

    def test_skill_link_with_a_title_to_a_missing_file_is_rejected(self):
        self.tree.write(
            "skills/route/SKILL.md",
            self.tree.read("skills/route/SKILL.md") + '\nSee [gone](missing.md "title").\n',
        )
        self.assert_rejects("missing.md")

    # --- self-referential asset paths -----------------------------------

    def test_skill_dir_path_to_a_missing_asset_is_rejected(self):
        self.tree.write(
            "skills/crew/SKILL.md",
            self.tree.read("skills/crew/SKILL.md") + "\nRun <crew-skill-dir>/assets/ghost.sh.\n",
        )
        self.assert_rejects("assets/ghost.sh")

    def test_plugin_dir_path_to_a_missing_asset_is_rejected(self):
        self.tree.write(
            "skills/crew/SKILL.md",
            self.tree.read("skills/crew/SKILL.md") + "\nRun <plugin-dir>/scripts/ghost.py.\n",
        )
        self.assert_rejects("scripts/ghost.py")

    def test_skill_dir_path_naming_an_unknown_skill_is_rejected(self):
        self.tree.write(
            "skills/crew/SKILL.md",
            self.tree.read("skills/crew/SKILL.md") + "\nRun <ghost-skill-dir>/assets/x.sh.\n",
        )
        self.assert_rejects("<ghost-skill-dir>")

    def test_hard_coded_install_path_to_a_shipped_asset_is_rejected(self):
        self.tree.write(
            "skills/crew/SKILL.md",
            self.tree.read("skills/crew/SKILL.md")
            + "\nRun ~/.claude/skills/crew/assets/monitor-wave.sh.\n",
        )
        self.assert_rejects("monitor-wave.sh")

    def test_install_path_to_a_file_this_plugin_does_not_ship_is_accepted(self):
        self.tree.write(
            "skills/crew/SKILL.md",
            self.tree.read("skills/crew/SKILL.md")
            + "\nRun ~/.claude/skills/elsewhere/assets/outside_bridge.py.\n",
        )
        result = run_validator(self.tree.root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_private_bridge_path_in_a_shipped_file_is_rejected(self):
        self.tree.write(
            "skills/route/SKILL.md",
            self.tree.read("skills/route/SKILL.md")
            + "\nRun $HOME/Library/Application Support/ChatGPT Hands-Free/runtime/bridgectl.\n",
        )
        self.assert_rejects("skills/route/SKILL.md")

    def test_hands_free_environment_token_in_a_shipped_file_is_rejected(self):
        self.tree.write(
            "skills/route/SKILL.md",
            self.tree.read("skills/route/SKILL.md")
            + "\nSet HANDS_FREE_CHILD_SESSION=1 before launch.\n",
        )
        self.assert_rejects("skills/route/SKILL.md")

    def test_configured_hostname_in_a_shipped_file_is_rejected(self):
        self.tree.set_local_identifiers("example-host")
        self.tree.write(
            "skills/route/SKILL.md",
            self.tree.read("skills/route/SKILL.md") + "\nRun ssh example-host hostname.\n",
        )
        self.assert_rejects("skills/route/SKILL.md")

    def test_configured_account_name_in_a_shipped_file_is_rejected(self):
        self.tree.set_local_identifiers("example-host", "sample-user")
        self.tree.write(
            "skills/route/SKILL.md",
            self.tree.read("skills/route/SKILL.md") + "\nAsk sample-user before launch.\n",
        )
        self.assert_rejects("skills/route/SKILL.md")

    def test_identifier_configured_in_the_environment_is_rejected(self):
        self.tree.write(
            "skills/route/SKILL.md",
            self.tree.read("skills/route/SKILL.md") + "\nRun ssh example-host hostname.\n",
        )
        result = run_validator(self.tree.root, env={IDENTIFIERS_ENV: "example-host"})
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("skills/route/SKILL.md", result.stdout + result.stderr)

    def test_environment_identifiers_override_the_file(self):
        self.tree.set_local_identifiers("example-host")
        self.tree.write(
            "skills/route/SKILL.md",
            self.tree.read("skills/route/SKILL.md") + "\nRun ssh example-host hostname.\n",
        )
        result = run_validator(self.tree.root, env={IDENTIFIERS_ENV: "other-host"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_unconfigured_identifier_is_accepted(self):
        self.tree.write(
            "skills/route/SKILL.md",
            self.tree.read("skills/route/SKILL.md") + "\nRun ssh example-host hostname.\n",
        )
        result = run_validator(self.tree.root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_the_identifier_list_itself_is_not_residue(self):
        self.tree.set_local_identifiers("example-host", "sample-user")
        result = run_validator(self.tree.root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_commented_and_blank_lines_in_the_identifier_list_are_ignored(self):
        self.tree.write(
            IDENTIFIERS_FILE,
            "# one per line\n\nexample-host  # trailing note\n\n",
        )
        self.tree.write(
            "skills/route/SKILL.md",
            self.tree.read("skills/route/SKILL.md")
            + "\nRun ssh example-host, never ssh note.\n",
        )
        result = run_validator(self.tree.root)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("example-host", result.stdout + result.stderr)
        self.assertNotIn("note — personal", result.stdout + result.stderr)

    def test_spend_figure_with_currency_in_a_shipped_file_is_rejected(self):
        self.tree.write(
            "skills/route/SKILL.md",
            self.tree.read("skills/route/SKILL.md") + "\nThe plan costs $20/month.\n",
        )
        self.assert_rejects("skills/route/SKILL.md")

    def test_structured_spend_figure_in_a_shipped_file_is_rejected(self):
        self.tree.write(
            "skills/route/SKILL.md",
            self.tree.read("skills/route/SKILL.md") + "\ntotal_cost_usd: 0.12\n",
        )
        self.assert_rejects("skills/route/SKILL.md")

    def test_plain_currency_amount_in_a_shipped_file_is_rejected(self):
        self.tree.write(
            "skills/route/SKILL.md",
            self.tree.read("skills/route/SKILL.md") + "\nThe fee is $20.\n",
        )
        self.assert_rejects("skills/route/SKILL.md")

    def test_public_vendored_bridge_mention_is_accepted(self):
        self.tree.write(
            "skills/route/SKILL.md",
            self.tree.read("skills/route/SKILL.md")
            + "\nSee ~/.agentcrew/logs and assets/codex/codex_bridge.py.\n",
        )
        result = run_validator(self.tree.root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_non_monetary_numbers_near_cost_words_are_accepted(self):
        self.tree.write(
            "skills/route/SKILL.md",
            self.tree.read("skills/route/SKILL.md")
            + "\n"
            + "Retry until the amount of pending waves reaches 0.\n"
            + "The reviewer costs about 3 rounds of context.\n"
            + "Set the price of admission: 2 approvals.\n",
        )
        result = run_validator(self.tree.root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_legacy_skill_name_in_docs_is_rejected(self):
        self.tree.write(
            "docs/design.md",
            self.tree.read("docs/design.md") + "\nLegacy ORCHESTRATE wording.\n",
        )
        self.assert_rejects("docs/design.md")

    def test_residue_fixture_under_a_nested_tests_directory_is_rejected(self):
        private_app = "ChatGPT " + "Hands" + "-Free"
        private_env = "HANDS" + "_FREE_CHILD_SESSION"
        personal_hostname = "example-host"
        personal_account = "sample-user"
        legacy_name = "ORCHESTR" + "ATE"
        self.tree.set_local_identifiers(personal_hostname, personal_account)
        self.tree.write(
            "skills/crew/assets/review/tests/residue-fixture.txt",
            "\n".join(
                (
                    f"$HOME/Library/Application Support/{private_app}/runtime/bridgectl",
                    private_env,
                    f"ssh {personal_hostname} hostname",
                    f"Ask {personal_account} before launch",
                    "The plan costs $" + "20/month",
                    legacy_name + " COMPLETE",
                )
            )
            + "\n",
        )
        self.assert_rejects("skills/crew/assets/review/tests/residue-fixture.txt")

    def test_shell_positional_parameters_are_not_spend_figures(self):
        self.tree.write(
            "skills/route/SKILL.md",
            self.tree.read("skills/route/SKILL.md")
            + "\nUse $1 and '$3:' as shell parameters.\n",
        )
        result = run_validator(self.tree.root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    # --- malformed config ----------------------------------------------

    def test_missing_config_is_rejected(self):
        self.tree.path(CONFIG).unlink()
        self.assert_rejects(CONFIG)

    def test_unparsable_config_is_rejected(self):
        self.tree.write(CONFIG, "[implementer\nexecutor = ")
        self.assert_rejects(CONFIG)

    def test_missing_implementer_cell_is_rejected(self):
        self.tree.drop_config_block("implementer.ops.mechanical")
        self.assert_rejects("implementer.ops.mechanical")

    def test_missing_reviewer_cell_is_rejected(self):
        self.tree.drop_config_block("reviewer.non-core-routine")
        self.assert_rejects("reviewer.non-core-routine")

    def test_missing_hook_entry_is_rejected(self):
        self.tree.drop_config_block("hooks.on-child-launch")
        self.assert_rejects("hooks.on-child-launch")

    def test_hook_entry_without_a_command_is_rejected(self):
        self.tree.edit_config('command = ""', "")
        self.assert_rejects("hooks.on-child-launch")

    def test_hook_env_holding_a_non_string_value_is_rejected(self):
        self.tree.write(CONFIG, self.tree.read(CONFIG) + "PORT = 123\n")
        self.assert_rejects("PORT")

    def test_cell_missing_a_field_is_rejected(self):
        self.tree.drop_config_block("implementer.acceptance.any")
        self.tree.write(
            CONFIG,
            self.tree.read(CONFIG)
            + '\n[implementer.acceptance.any]\nexecutor = "claude"\nmodel = "opus"\n',
        )
        self.assert_rejects("effort")

    def test_cell_with_a_blank_model_is_rejected(self):
        self.tree.edit_config('model = "opus"', 'model = ""')
        self.assert_rejects("model")

    def test_cell_with_an_unknown_executor_is_rejected(self):
        self.tree.edit_config('executor = "claude"', 'executor = "gemini"')
        self.assert_rejects("gemini")

    def test_unknown_config_key_is_rejected(self):
        self.tree.write(CONFIG, self.tree.read(CONFIG) + '\n[implementer.tdd-refactor.core-tricky]\n')
        self.assert_rejects("core-tricky")


class ProjectConfigTests(unittest.TestCase):
    """`--config`: the file the setup wizard writes at a project root."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = pathlib.Path(self._tmp.name) / "agentcrew.toml"

    def check(self, text):
        self.path.write_text(text)
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--config", str(self.path)],
            capture_output=True,
            text=True,
        )

    def assert_accepts(self, text):
        result = self.check(text)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def assert_rejects(self, text, mentioning):
        result = self.check(text)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(mentioning, result.stdout + result.stderr)

    def test_the_shipped_defaults_are_accepted_verbatim(self):
        self.assert_accepts((PLUGIN_ROOT / CONFIG).read_text())

    def test_a_single_overridden_cell_is_accepted(self):
        self.assert_accepts(
            '[implementer.direct.any]\nexecutor = "codex"\nmodel = "gpt-5.6-sol"\neffort = "high"\n'
        )

    def test_an_empty_file_is_accepted(self):
        self.assert_accepts("")

    def test_an_overridden_cell_missing_a_field_is_rejected(self):
        self.assert_rejects(
            '[implementer.direct.any]\nexecutor = "claude"\nmodel = "opus"\n', "effort"
        )

    def test_an_unknown_executor_is_rejected(self):
        self.assert_rejects('[reviewer.core-complex]\nexecutor = "gemini"\n', "gemini")

    def test_an_unknown_case_is_rejected(self):
        self.assert_rejects("[reviewer.core-tricky]\n", "core-tricky")

    def test_a_hook_env_holding_a_non_string_value_is_rejected(self):
        self.assert_rejects("[hooks.on-child-launch.env]\nPORT = 123\n", "PORT")

    def test_unparsable_toml_is_rejected(self):
        self.assert_rejects("[implementer.direct.any\n", "unparsable")

    def test_a_missing_file_is_rejected(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--config", str(self.path.parent / "absent.toml")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("missing", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
