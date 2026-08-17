#!/usr/bin/env python3
"""Behaviour of this repo's registered knowledge-graph hooks, run from a child worktree.

A crew run works in worktrees, and the incremental-update hook fires there. Deriving the repo
from the working tree makes every child build a private empty graph while the shared one at the
main checkout goes stale; deriving it from the git common directory resolves to the main checkout
from both sides. What is asserted here is external: the command registered in
`.claude/settings.json` is executed from inside a real worktree against a stub `code-review-graph`
on PATH, and the graph it touches is the shared one.
"""

import json
import os
import pathlib
import subprocess
import tempfile
import unittest


REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[1]
SETTINGS = REPOSITORY_ROOT / ".claude" / "settings.json"
GRAPH_DIRECTORY = ".code-review-graph"
GRAPH_FILE = "graph.db"
# What the stub records, one line per invocation: the directory it was pointed at.
INVOCATIONS = "invocations"
# The hook is a PostToolUse command; this is the call it exists to make.
UPDATE_COMMAND = "code-review-graph update"


def run_git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def update_hook_command():
    """The incremental-update hook command as this repo registers it."""
    settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
    commands = [
        hook["command"]
        for block in settings["hooks"]["PostToolUse"]
        for hook in block["hooks"]
        if UPDATE_COMMAND in hook.get("command", "")
    ]
    if len(commands) != 1:
        raise AssertionError(f"expected one {UPDATE_COMMAND!r} hook, found {len(commands)}")
    return commands[0]


class WorktreeGraphHookTests(unittest.TestCase):
    """A main checkout, a worktree cut from it, and a `code-review-graph` that only records."""

    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        # Resolved: git reports real paths, and the temporary directory sits behind a symlink on
        # macOS, so an unresolved path would differ from what the hook derives for that reason
        # alone.
        root = pathlib.Path(self.work.name).resolve()

        self.checkout = root / "checkout"
        self.checkout.mkdir()
        run_git(root, "init", "-b", "main", str(self.checkout))
        run_git(self.checkout, "config", "user.email", "crew@example.invalid")
        run_git(self.checkout, "config", "user.name", "Crew Test")
        (self.checkout / "notes.md").write_text("one\n", encoding="utf-8")
        run_git(self.checkout, "add", "-A")
        run_git(self.checkout, "commit", "-m", "base")

        self.worktree = root / "worktrees" / "74-74"
        run_git(self.checkout, "worktree", "add", "-b", "worktree-74-74", str(self.worktree))

        self.record = root / INVOCATIONS
        self.bin_dir = root / "bin"
        self.bin_dir.mkdir()
        stub = self.bin_dir / "code-review-graph"
        # Stands in for the real CLI: writes the graph into the directory `--repo` names, the way
        # the real one does, and records where that was.
        stub.write_text(
            "#!/bin/sh\n"
            'repo=""\n'
            "while [ $# -gt 0 ]; do\n"
            '  if [ "$1" = "--repo" ]; then repo="$2"; shift; fi\n'
            "  shift\n"
            "done\n"
            f'printf "%s\\n" "$repo" >>"{self.record}"\n'
            f'mkdir -p "$repo/{GRAPH_DIRECTORY}" && : >"$repo/{GRAPH_DIRECTORY}/{GRAPH_FILE}"\n',
            encoding="utf-8",
        )
        stub.chmod(0o755)

    def fire_hook(self, cwd):
        """The registered command, run the way the harness runs it: shell, hook JSON on stdin."""
        environment = dict(os.environ)
        environment["PATH"] = f"{self.bin_dir}{os.pathsep}{environment['PATH']}"
        payload = json.dumps({"tool_name": "Edit", "cwd": str(cwd)})
        result = subprocess.run(
            update_hook_command(), shell=True, cwd=str(cwd), env=environment,
            input=payload, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def repos_updated(self):
        if not self.record.exists():
            return []
        return self.record.read_text(encoding="utf-8").split()

    def test_the_repo_it_derives_from_a_worktree_is_the_main_checkout(self):
        self.fire_hook(self.worktree)

        self.assertEqual(self.repos_updated(), [str(self.checkout)])

    def test_the_repo_it_derives_from_the_main_checkout_is_still_the_main_checkout(self):
        self.fire_hook(self.checkout)

        self.assertEqual(self.repos_updated(), [str(self.checkout)])

    def test_a_worktree_side_update_lands_in_the_shared_graph_not_a_private_one(self):
        self.fire_hook(self.worktree)

        self.assertTrue((self.checkout / GRAPH_DIRECTORY / GRAPH_FILE).exists())
        self.assertFalse((self.worktree / GRAPH_DIRECTORY).exists())


class GraphDirectoryIgnoreTests(unittest.TestCase):
    """Whatever a graph directory lands in, git never carries it — including under a worktree."""

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
