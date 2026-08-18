#!/usr/bin/env python3
"""The repo-scope MCP config launches the graph server directly, not through a wrapper.

`uvx code-review-graph serve` stays resident beside the server it starts, so every session that
loads this config pays two processes per server, and uvx rebuilds a fresh ~440 MB environment on
each dependency release. Naming the installed console script costs one process and no environment
churn; on a machine without the tool the server simply fails to connect and agents fall back to
Grep/Glob/Read as AGENTS.md prescribes. This guards against a convenience revert (#88).
"""

import json
import pathlib
import unittest


REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[1]
MCP_CONFIG = REPOSITORY_ROOT / ".mcp.json"
SERVER = "code-review-graph"
# Launchers that resolve the tool at run time and stay resident beside it.
WRAPPERS = {"uvx", "uv", "pipx", "npx", "pdm", "poetry", "rye", "hatch"}


class GraphServerLaunch(unittest.TestCase):
    def setUp(self):
        config = json.loads(MCP_CONFIG.read_text(encoding="utf-8"))
        self.server = config["mcpServers"][SERVER]

    def test_command_is_the_installed_console_script(self):
        self.assertEqual(self.server["command"], SERVER)

    def test_command_is_not_a_wrapper(self):
        self.assertNotIn(pathlib.PurePath(self.server["command"]).name, WRAPPERS)

    def test_args_are_the_serve_subcommand_alone(self):
        self.assertEqual(self.server["args"], ["serve"])

    def test_command_carries_no_machine_specific_path(self):
        self.assertEqual(pathlib.PurePath(self.server["command"]).name, self.server["command"])


if __name__ == "__main__":
    unittest.main()
