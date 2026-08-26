#!/usr/bin/env python3
"""A code graph built in this tree is never carried by git, wherever it lands.

Nothing here runs the graph tool any more (#128), but a review does: the Review-Switch Bridge
builds the graph in the checkout under review, and for a crew ticket that checkout is a linked
worktree under `.claude/worktrees/` (review-switch#31, ADR-0005). So a review leaves a
multi-megabyte sqlite file inside this repository's tree, and the `.gitignore` entry that keeps
it out of a commit stopped being residue of the removed hooks and became the thing standing
between a crew merge and a committed database.

This is what remains of `tests/test_graph_hooks.py`, which tested the hooks and this rule
together: the hooks went, the rule outlived them.
"""

import pathlib
import subprocess
import unittest


REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[1]
GRAPH_DIRECTORY = ".code-review-graph"
GRAPH_FILE = "graph.db"


class GraphDirectoryIgnoreTests(unittest.TestCase):
    def check_ignore(self, relative_path):
        return subprocess.run(
            ["git", "-C", str(REPOSITORY_ROOT), "check-ignore", "-q", relative_path],
            capture_output=True, text=True,
        ).returncode

    def test_a_graph_directory_is_ignored_at_the_root_and_under_a_worktree(self):
        for relative_path in (
            f"{GRAPH_DIRECTORY}/{GRAPH_FILE}",
            f".claude/worktrees/74-74/{GRAPH_DIRECTORY}/{GRAPH_FILE}",
        ):
            with self.subTest(path=relative_path):
                self.assertEqual(self.check_ignore(relative_path), 0)


if __name__ == "__main__":
    unittest.main()
