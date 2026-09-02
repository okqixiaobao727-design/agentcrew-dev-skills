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


class TrackerCreateTests(unittest.TestCase):
    """**create** — open one ticket on either adapter and hand back what the Run plan needs."""

    TITLE = "crew: the advance path drops a halted wave"
    BODY = "Queued from #45 — open: cause\n\nskills/crew/assets/advance.py:120"
    ROLE = "ready-for-agent"

    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp()).resolve()
        self.tickets = self.root / "crewtask" / "72"
        self.tickets.mkdir(parents=True)

    @staticmethod
    def completed(argv, stdout="", stderr="", returncode=0):
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    @staticmethod
    def look(search=None, limit="100"):
        """The one issue-listing call create makes per look, recent first and then by search."""
        arguments = ["gh", "issue", "list", "--state", "open", "--limit", limit,
                     "--json", "number,title,body,url"]
        if search is not None:
            arguments += ["--search", search]
        return arguments

    def test_github_create_opens_the_issue_with_its_role_label_and_returns_the_ticket(self):
        locator = "https://github.example.invalid/owner/name/issues/190"
        results = [
            self.completed([], stdout=json.dumps([])),
            self.completed([], stdout=json.dumps([])),
            self.completed([], stdout=locator + "\n"),
        ]

        with mock.patch.object(tracker, "_run", side_effect=results) as run:
            ticket, observed = tracker.create(
                "github", self.TITLE, self.BODY, role_label=self.ROLE, directory=self.root
            )

        self.assertEqual(observed, locator)
        self.assertEqual(ticket, {
            "id": "190",
            "title": self.TITLE,
            "body": self.BODY,
            "path": None,
            "url": locator,
            "repository": str(self.root),
        })
        self.assertEqual(run.call_args_list, [
            mock.call(self.look(), cwd=self.root),
            mock.call(
                self.look(f"{self.TITLE} in:title", limit="1000"), cwd=self.root
            ),
            mock.call(
                ["gh", "issue", "create", "--title", self.TITLE, "--body", self.BODY,
                 "--label", self.ROLE],
                cwd=self.root,
            ),
        ])

    def test_an_identical_open_github_issue_returns_its_locator_without_creating_one(self):
        locator = "https://github.example.invalid/owner/name/issues/190"
        held = [
            {"number": 189, "title": self.TITLE, "body": "another body", "url": "other"},
            {"number": 190, "title": self.TITLE, "body": self.BODY, "url": locator},
        ]

        with mock.patch.object(
            tracker, "_run", return_value=self.completed([], stdout=json.dumps(held))
        ) as run:
            ticket, observed = tracker.create(
                "github", self.TITLE, self.BODY, role_label=self.ROLE, directory=self.root
            )

        self.assertEqual(observed, locator)
        self.assertEqual(ticket["id"], "190")
        run.assert_called_once()

    def test_a_github_create_the_tracker_refuses_names_the_operation(self):
        results = [
            self.completed([], stdout=json.dumps([])),
            self.completed([], stdout=json.dumps([])),
            self.completed([], stderr="could not add label: 'ready-for-agent' not found",
                           returncode=1),
        ]

        with mock.patch.object(tracker, "_run", side_effect=results):
            with self.assertRaisesRegex(tracker.TrackerError, "refused the create"):
                tracker.create(
                    "github", self.TITLE, self.BODY, role_label=self.ROLE, directory=self.root
                )

    def test_the_search_look_is_read_to_githubs_cap_not_the_recent_windows_hundred(self):
        """The bound on the second look is the platform's number, never one of ours."""
        results = [
            self.completed([], stdout=json.dumps([])),
            self.completed([], stdout=json.dumps([])),
            self.completed([], stdout="https://github.example.invalid/owner/name/issues/1\n"),
        ]

        with mock.patch.object(tracker, "_run", side_effect=results) as run:
            tracker.create(
                "github", self.TITLE, self.BODY, role_label=self.ROLE, directory=self.root
            )

        recent, searched = run.call_args_list[0].args[0], run.call_args_list[1].args[0]
        self.assertEqual(recent[recent.index("--limit") + 1], tracker.GITHUB_RECENT_LOOK)
        self.assertEqual(searched[searched.index("--limit") + 1], tracker.GITHUB_SEARCH_CAP)
        self.assertNotEqual(tracker.GITHUB_SEARCH_CAP, tracker.GITHUB_RECENT_LOOK)
        self.assertNotIn("--search", recent)
        self.assertIn(f"{self.TITLE} {tracker.GITHUB_TITLE_SEARCH}", searched)

    def test_an_issue_outside_the_recent_window_is_found_by_the_search_look(self):
        """A ticket old enough to have scrolled out of the list is still not opened twice."""
        locator = "https://github.example.invalid/owner/name/issues/12"
        held = [{"number": 12, "title": self.TITLE, "body": self.BODY, "url": locator}]
        results = [
            self.completed([], stdout=json.dumps([])),
            self.completed([], stdout=json.dumps(held)),
        ]

        with mock.patch.object(tracker, "_run", side_effect=results) as run:
            ticket, observed = tracker.create(
                "github", self.TITLE, self.BODY, role_label=self.ROLE, directory=self.root
            )

        self.assertEqual((ticket["id"], observed), ("12", locator))
        self.assertEqual(len(run.call_args_list), 2)

    def test_a_github_create_that_returns_no_issue_number_is_refused(self):
        results = [
            self.completed([], stdout=json.dumps([])),
            self.completed([], stdout=json.dumps([])),
            self.completed([], stdout="opening in browser\n"),
        ]

        with mock.patch.object(tracker, "_run", side_effect=results):
            with self.assertRaisesRegex(tracker.TrackerError, "no issue number"):
                tracker.create(
                    "github", self.TITLE, self.BODY, role_label=self.ROLE, directory=self.root
                )

    def test_local_create_writes_the_next_numbered_file_with_its_status_role(self):
        (self.tickets / "07.md").write_text("# Seventh\n", encoding="utf-8")
        (self.tickets / "12-slug.md").write_text("# Twelfth\n", encoding="utf-8")
        (self.tickets / "spec.md").write_text("# Spec\n", encoding="utf-8")

        with mock.patch.object(tracker, "_run") as run:
            ticket, locator = tracker.create(
                "local", self.TITLE, self.BODY, role_label=self.ROLE, directory=self.tickets
            )

        run.assert_not_called()
        path = self.tickets / "13.md"
        self.assertEqual(locator, str(path))
        self.assertEqual(ticket, {
            "id": "13",
            "title": self.TITLE,
            "body": self.BODY,
            "path": str(path),
            "url": None,
            "repository": str(self.tickets),
        })
        self.assertEqual(
            path.read_text(encoding="utf-8"),
            f"# {self.TITLE}\n\n{self.BODY}\n\nStatus: {self.ROLE}\n",
        )

    def test_the_first_local_ticket_of_an_empty_directory_is_numbered_one(self):
        ticket, locator = tracker.create(
            "local", self.TITLE, self.BODY, role_label=self.ROLE, directory=self.tickets
        )

        self.assertEqual(ticket["id"], "1")
        self.assertEqual(locator, str(self.tickets / "1.md"))

    def test_an_identical_local_ticket_is_returned_without_rewriting_the_directory(self):
        first, locator = tracker.create(
            "local", self.TITLE, self.BODY, role_label=self.ROLE, directory=self.tickets
        )
        path = pathlib.Path(locator)
        path.write_text(
            path.read_text(encoding="utf-8").replace(self.ROLE, "ready-for-human"),
            encoding="utf-8",
        )
        before = path.read_text(encoding="utf-8")

        again, repeated = tracker.create(
            "local", self.TITLE, self.BODY, role_label=self.ROLE, directory=self.tickets
        )

        self.assertEqual(repeated, locator)
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(path.read_text(encoding="utf-8"), before)
        self.assertEqual(sorted(p.name for p in self.tickets.iterdir()), ["1.md"])

    def test_invalid_create_facts_are_refused_before_any_tracker_call(self):
        cases = (
            ("unknown", self.TITLE, self.BODY, self.ROLE, self.root, "does not support create"),
            ("github", "  ", self.BODY, self.ROLE, self.root, "title is empty"),
            ("github", self.TITLE, "\n", self.ROLE, self.root, "body is empty"),
            ("github", self.TITLE, self.BODY, "triage", self.root, "ready-for-agent"),
            ("local", self.TITLE, self.BODY, self.ROLE, None, "directory"),
        )

        with mock.patch.object(tracker, "_run") as run:
            for kind, title, body, role, directory, detail in cases:
                with self.subTest(kind=kind, detail=detail):
                    with self.assertRaisesRegex(tracker.TrackerError, detail):
                        tracker.create(
                            kind, title, body, role_label=role, directory=directory
                        )
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
