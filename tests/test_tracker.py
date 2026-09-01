import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock


ASSETS = pathlib.Path(__file__).resolve().parents[1] / "skills" / "crew" / "assets"
sys.path.insert(0, str(ASSETS))

import tracker  # noqa: E402


class TrackerCommentTests(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp()).resolve()
        self.ticket_path = self.root / "crewtask" / "71" / "46.md"
        self.ticket_path.parent.mkdir(parents=True)
        self.ticket_path.write_text("# Later ticket\n", encoding="utf-8")
        self.ticket = SimpleNamespace(id="46", path=str(self.ticket_path))

    @staticmethod
    def completed(argv, stdout="", stderr="", returncode=0):
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    def test_github_comment_returns_the_gh_locator_and_carries_the_body_unchanged(self):
        body = "Deferred from #45:\nFinding text — skills/example.py:12"
        locator = "https://github.example.invalid/issues/46#issuecomment-7001"
        results = [
            self.completed([], stdout=json.dumps({"comments": []})),
            self.completed([], stdout=locator + "\n"),
        ]

        with mock.patch.object(tracker, "_run", side_effect=results) as run:
            observed = tracker.comment("github", self.ticket, body)

        self.assertEqual(observed, locator)
        self.assertEqual(run.call_args_list, [
            mock.call(
                ["gh", "issue", "view", "46", "--json", "comments"],
                cwd=self.ticket_path.parent,
            ),
            mock.call(
                ["gh", "issue", "comment", "46", "--body", body],
                cwd=self.ticket_path.parent,
            ),
        ])

    def test_an_existing_identical_github_comment_returns_its_locator_without_writing(self):
        body = "Deferred from #45: keep the cited pointer — #46"
        locator = "https://github.example.invalid/issues/46#issuecomment-7001"
        held = {"comments": [{"body": body, "url": locator}]}

        with mock.patch.object(
            tracker,
            "_run",
            return_value=self.completed([], stdout=json.dumps(held)),
        ) as run:
            observed = tracker.comment("github", self.ticket, body)

        self.assertEqual(observed, locator)
        run.assert_called_once_with(
            ["gh", "issue", "view", "46", "--json", "comments"],
            cwd=self.ticket_path.parent,
        )

    def test_github_supersedes_the_matching_comment_without_knowing_its_workflow(self):
        old = "pickup/v1"
        new = "pickup/v2"
        locator = "https://github.example.invalid/issues/46#issuecomment-7001"
        held = {
            "comments": [
                {"id": "IC_node_7001", "body": old, "url": locator},
                {"id": "IC_node_7002", "body": "another note", "url": "other"},
            ]
        }
        results = [
            self.completed([], stdout=json.dumps(held)),
            self.completed([], stdout="{}\n"),
        ]

        with mock.patch.object(tracker, "_run", side_effect=results) as run:
            observed = tracker.comment(
                "github", self.ticket, new, supersedes="pickup/"
            )

        self.assertEqual(observed, locator)
        self.assertEqual(run.call_args_list[0], mock.call(
            ["gh", "issue", "view", "46", "--json", "comments"],
            cwd=self.ticket_path.parent,
        ))
        mutation = run.call_args_list[1]
        self.assertEqual(mutation.kwargs, {"cwd": self.ticket_path.parent})
        self.assertEqual(mutation.args[0][:3], ["gh", "api", "graphql"])
        self.assertIn("id=IC_node_7001", mutation.args[0])
        self.assertIn(f"body={new}", mutation.args[0])

    def test_local_comments_keep_distinct_findings_and_deduplicate_an_identical_one(self):
        first = "Deferred from #45: first finding — alpha.py:10"
        second = "Deferred from #45:\nsecond finding — beta.py:20"

        first_locator = tracker.comment("local", self.ticket, first)
        second_locator = tracker.comment("local", self.ticket, second)
        repeated_locator = tracker.comment("local", self.ticket, first)

        self.assertEqual(first_locator, f"{self.ticket_path}:3")
        self.assertEqual(second_locator, f"{self.ticket_path}:5")
        self.assertEqual(repeated_locator, first_locator)
        self.assertEqual(
            self.ticket_path.read_text(encoding="utf-8"),
            "# Later ticket\n\n"
            "Crew: Deferred from #45: first finding — alpha.py:10\n\n"
            "Crew: Deferred from #45:\nsecond finding — beta.py:20\n",
        )

    def test_local_supersedes_only_the_matching_comment_with_an_arbitrary_prefix(self):
        self.ticket_path.write_text(
            "# Later ticket\n\n"
            "Crew: pickup/v1\n\n"
            "Crew: another note\n",
            encoding="utf-8",
        )

        locator = tracker.comment(
            "local", self.ticket, "pickup/v2", supersedes="pickup/"
        )

        self.assertEqual(locator, f"{self.ticket_path}:5")
        self.assertEqual(
            self.ticket_path.read_text(encoding="utf-8"),
            "# Later ticket\n\nCrew: another note\n\nCrew: pickup/v2\n",
        )

    def test_invalid_comment_facts_are_refused_before_any_tracker_call(self):
        cases = (
            ("unknown", self.ticket, "body", "does not support comment"),
            ("github", self.ticket, "  ", "body is empty"),
            (
                "github",
                SimpleNamespace(id="not-a-number", path=self.ticket_path),
                "body",
                "not a GitHub issue number",
            ),
            ("github", {"id": "46"}, "body", "carries no repository directory"),
        )

        with mock.patch.object(tracker, "_run") as run:
            for kind, ticket, body, detail in cases:
                with self.subTest(kind=kind, detail=detail):
                    with self.assertRaisesRegex(tracker.TrackerError, detail):
                        tracker.comment(kind, ticket, body)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
