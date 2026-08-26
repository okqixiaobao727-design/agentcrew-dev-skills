#!/usr/bin/env python3
"""This repository registers no knowledge-graph MCP server, for any session it opens.

The code graph reaches a review through the Review-Switch Bridge's CLI call alone: the Bridge
builds the graph in the checkout under review and reads it there, so no session — authoring or
reviewing, claude or codex — needs a `code-review-graph serve` process, and every one that
carried one paid for a dependency with no consumer (review-switch ADR-0005, #128). Agents
explore this repository with Grep/Glob/Read.

The registration is one JSON object to add back, which is why this guard exists: it fails
whether the server returns under its own name or under an alias whose command is the graph
tool, and it holds whether or not a `.mcp.json` exists at all.
"""

import json
import pathlib
import unittest


REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[1]
MCP_CONFIG = REPOSITORY_ROOT / ".mcp.json"
GRAPH_COMMAND = "code-review-graph"


def registered_servers():
    """The repo-scope MCP servers, as (name, entry) pairs. No config file means none."""
    if not MCP_CONFIG.exists():
        return []
    config = json.loads(MCP_CONFIG.read_text(encoding="utf-8"))
    return sorted(config.get("mcpServers", {}).items())


class NoGraphServerRegistered(unittest.TestCase):
    def test_no_server_is_named_for_the_graph(self):
        for name, _ in registered_servers():
            with self.subTest(server=name):
                self.assertNotIn(GRAPH_COMMAND, name)

    def test_no_server_launches_the_graph_command(self):
        for name, entry in registered_servers():
            with self.subTest(server=name):
                launch = [entry.get("command", ""), *entry.get("args", [])]
                self.assertFalse(
                    any(GRAPH_COMMAND in str(part) for part in launch),
                    f"{name} launches the graph tool: {launch}",
                )


if __name__ == "__main__":
    unittest.main()
