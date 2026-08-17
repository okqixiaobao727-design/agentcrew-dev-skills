#!/usr/bin/env python3
"""Drive the staging script from its command line against a stubbed tracker.

Every fixture is a real git repository carrying a real `agentcrew.toml`, plus a stub PATH holding
`gh` for the github tracker; the local tracker needs no stub, because its tickets are files in that
same repository. Assertions are on external behaviour only — the exit code, what reached stdout and
stderr, the files the run directory holds, and what the driver's own parsing makes of them.
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
STAGE = TESTS_DIR.parent / "stage.py"
PLUGIN_ROOT = TESTS_DIR.parents[4]
DRIVER = PLUGIN_ROOT / "skills" / "crew" / "assets" / "driver" / "driver.py"

BASE_BRANCH = "main"
REPAIR_MODEL = "claude-sonnet-5"
CLAUDE_MODEL = "claude-opus-4-5-20251101"
CODEX_MODEL = "gpt-5.6-luna"

RUN_ROOT = "crewtask"
SPEC_NAME = "spec.md"

ROUTING = f"""## Routing

Workflow: tdd
Executor: claude
Model: {CLAUDE_MODEL}
Effort: medium
Review: codex {CODEX_MODEL} max
Reasons: a fixture ticket.
"""

TITLES = {"61": "The first ticket", "62": "The second ticket", "70": "An outside ticket"}


def git(repo, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=check, capture_output=True, text=True
    )


def ticket_body(blocked_by=(), routing=ROUTING):
    """A ticket's body in the shape `/route` leaves it: a brief, its edges, then its routing."""
    edges = (
        "\n".join(f"- #{number} — a fixture edge." for number in blocked_by)
        if blocked_by else "None — can start immediately."
    )
    return f"## What to build\n\nA fixture.\n\n## Blocked by\n\n{edges}\n\n{routing}"


class Fixture:
    """A repository whose tickets live in a stubbed tracker, ready for one staging run."""

    def __init__(self, tracker="github"):
        self.root = pathlib.Path(tempfile.mkdtemp()).resolve()
        self.tracker = tracker
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", BASE_BRANCH)
        git(self.repo, "config", "user.email", "crew@example.invalid")
        git(self.repo, "config", "user.name", "Crew Test")
        (self.repo / "README.md").write_text("fixture\n")
        (self.repo / ".gitignore").write_text(f"{RUN_ROOT}/\n")
        self.write_config()
        self.tickets_dir = self.repo / "tickets"
        self.tickets_dir.mkdir()
        (self.tickets_dir / ".keep").write_text("")

        self.stub_dir = self.root / "stub"
        self.stub_dir.mkdir()
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self._link_stub("gh", "stub_gh.py")
        (self.stub_dir / "gh-issues.json").write_text("{}")
        self.commit("base")

    def _link_stub(self, name, script):
        target = self.bin_dir / name
        target.write_text(
            "#!/bin/sh\nexec %s %s \"$@\"\n" % (sys.executable, TESTS_DIR / script)
        )
        target.chmod(0o755)

    def write_config(self, repair_model=REPAIR_MODEL, tracker=None):
        lines = []
        if repair_model is not None:
            lines += ["[repair]", f'model = "{repair_model}"']
        kind = self.tracker if tracker is None else tracker
        if kind is not None:
            lines += ["[tracker]", f'kind = "{kind}"']
        (self.repo / "agentcrew.toml").write_text("\n".join(lines) + "\n")

    def commit(self, message):
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", message)

    # --- the tracker's side ---------------------------------------------------------------

    def issue(self, number, title=None, body=None, state="OPEN", blocked_by=()):
        """One ticket, written where this fixture's tracker keeps them."""
        title = TITLES.get(str(number), f"Ticket {number}") if title is None else title
        body = ticket_body(blocked_by) if body is None else body
        if self.tracker == "github":
            path = self.stub_dir / "gh-issues.json"
            table = json.loads(path.read_text())
            table[str(number)] = {"title": title, "body": body, "state": state}
            path.write_text(json.dumps(table))
            return str(number)
        path = self.tickets_dir / f"{number}.md"
        status = "done" if state == "CLOSED" else "ready-for-agent"
        path.write_text(f"# {title}\n\n{body}\nStatus: {status}\n")
        self.commit(f"ticket {number}")
        return str(path)

    # --- running it -----------------------------------------------------------------------

    def environment(self):
        environment = dict(os.environ)
        environment["PATH"] = f"{self.bin_dir}{os.pathsep}{environment['PATH']}"
        environment["AGENTCREW_STUB_DIR"] = str(self.stub_dir)
        return environment

    def stage(self, *arguments):
        return subprocess.run(
            [sys.executable, str(STAGE), *arguments],
            capture_output=True, text=True, env=self.environment(), cwd=str(self.repo),
        )

    def run_dir(self, number=1):
        return self.repo / RUN_ROOT / str(number)

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


def parsed(directory):
    """What the driver's own parsing makes of that directory: its tickets and its wave table."""
    program = (
        "import json,pathlib,sys;"
        f"sys.path.insert(0, {str(DRIVER.parent)!r});"
        "import driver;"
        f"tickets = driver.read_tickets(pathlib.Path({str(directory)!r}));"
        "print(json.dumps({"
        "'tickets': tickets,"
        "'problems': driver.graph_problems(tickets),"
        "'waves': [[t['id'] for t in w['tickets']] for w in driver.assign_waves(tickets)]"
        "}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, check=True
    )
    return json.loads(result.stdout)


class StagingTests(unittest.TestCase):
    tracker = "github"

    def setUp(self):
        self.fixture = Fixture(self.tracker)
        self.addCleanup(self.fixture.cleanup)
        self.refs = []

    def two_tickets(self):
        """A set of two where the second is blocked by the first, written to the tracker once.

        Written once, because on the local tracker writing a ticket commits it: a second write
        inside one test would sweep up whatever else that test had deliberately left uncommitted.
        """
        if not self.refs:
            self.refs = [self.fixture.issue(61), self.fixture.issue(62, blocked_by=("61",))]
        return self.refs

    def stage_two(self):
        return self.fixture.stage(*self.two_tickets())

    # --- the contract ---------------------------------------------------------------------

    def test_help_documents_the_ticket_set_and_the_parent_flag(self):
        """The invocation shape downstream couples to is the script's own help output."""
        result = self.fixture.stage("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--parent", result.stdout)
        self.assertIn("ticket", result.stdout.lower())

    def test_green_staging_prints_one_crew_command_and_exits_zero(self):
        result = self.stage_two()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"/crew {RUN_ROOT}/1\n")

    def test_green_staging_writes_a_spec_and_one_file_per_ticket_and_nothing_else(self):
        self.stage_two()
        held = sorted(path.name for path in self.fixture.run_dir().iterdir())
        self.assertEqual(held, ["61.md", "62.md", SPEC_NAME])

    def test_the_staged_directory_is_what_the_drivers_own_parsing_accepts(self):
        self.stage_two()
        read = parsed(self.fixture.run_dir())
        self.assertEqual([ticket["id"] for ticket in read["tickets"]], ["61", "62"])
        self.assertEqual([ticket["title"] for ticket in read["tickets"]],
                         [TITLES["61"], TITLES["62"]])
        self.assertEqual(read["problems"], [])
        self.assertEqual(read["waves"], [["61"], ["62"]])

    def test_the_cover_page_names_every_ticket_its_self_containment_and_its_provenance(self):
        self.stage_two()
        spec = (self.fixture.run_dir() / SPEC_NAME).read_text()
        for number in ("61", "62"):
            self.assertIn(number, spec)
            self.assertIn(TITLES[number], spec)
        self.assertIn("self-contained", spec)
        self.assertTrue(
            any(line.startswith("Tracker:") for line in spec.splitlines()),
            f"no Tracker: provenance line in {spec!r}",
        )

    # --- the self-check -------------------------------------------------------------------

    def test_a_dirty_tracked_tree_is_a_named_blocking_item_and_withholds_the_command(self):
        self.two_tickets()
        (self.fixture.repo / "README.md").write_text("edited\n")
        result = self.stage_two()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("/crew ", result.stdout)
        self.assertIn("README.md", result.stderr)
        self.assertIn("commit it", result.stderr)

    def test_an_unparseable_ticket_is_a_named_blocking_item_and_withholds_the_command(self):
        first = self.fixture.issue(61)
        second = self.fixture.issue(62, body="## What to build\n\nA fixture with no routing.\n")
        result = self.fixture.stage(first, second)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("/crew ", result.stdout)
        self.assertIn("62", result.stderr)
        self.assertIn("Workflow", result.stderr)

    def test_a_missing_config_is_a_named_blocking_item_and_withholds_the_command(self):
        self.two_tickets()
        (self.fixture.repo / "agentcrew.toml").unlink()
        self.fixture.commit("drop the config")
        result = self.stage_two()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("/crew ", result.stdout)
        self.assertIn("agentcrew.toml", result.stderr)

    # --- the dependency closure -----------------------------------------------------------

    def test_an_edge_to_a_closed_outside_set_blocker_is_stripped(self):
        self.fixture.issue(70, state="CLOSED")
        result = self.fixture.stage(self.fixture.issue(61, blocked_by=("70",)))
        self.assertEqual(result.returncode, 0, result.stderr)
        read = parsed(self.fixture.run_dir())
        self.assertEqual(read["tickets"][0]["blocked_by"], [])
        self.assertEqual(read["problems"], [])

    def test_an_open_outside_set_blocker_is_a_named_blocking_item(self):
        self.fixture.issue(70, state="OPEN")
        result = self.fixture.stage(self.fixture.issue(61, blocked_by=("70",)))
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("/crew ", result.stdout)
        self.assertIn("70", result.stderr)
        self.assertIn("61", result.stderr)

    # --- the directory ---------------------------------------------------------------------

    def test_the_directory_number_is_the_current_maximum_plus_one(self):
        (self.fixture.repo / RUN_ROOT / "5").mkdir(parents=True)
        result = self.stage_two()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"/crew {RUN_ROOT}/6\n")

    def test_re_running_the_same_set_overwrites_the_same_directory(self):
        first = self.stage_two()
        self.assertEqual(first.returncode, 0, first.stderr)
        stale = self.fixture.run_dir() / "99.md"
        stale.write_text("# a file a later staging must not keep\n")
        self.fixture.issue(61, body=ticket_body().replace("A fixture.", "Edited on the tracker."))

        second = self.stage_two()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(second.stdout, first.stdout)
        self.assertEqual(
            sorted(path.name for path in (self.fixture.repo / RUN_ROOT).iterdir()), ["1"]
        )
        self.assertFalse(stale.exists())
        self.assertIn("Edited on the tracker.", (self.fixture.run_dir() / "61.md").read_text())

    def test_staging_leaves_the_tracked_tree_untouched(self):
        result = self.stage_two()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(git(self.fixture.repo, "status", "--porcelain").stdout, "")

    def test_the_parent_flag_is_refused_by_the_ticket_that_owns_its_expansion(self):
        result = self.fixture.stage("--parent", "60")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("/crew ", result.stdout)
        self.assertIn("#64", result.stderr)


class LocalTrackerTests(StagingTests):
    """The same behaviour on the tracker whose tickets are files in the repository."""

    tracker = "local"


class ShippedTreeTests(unittest.TestCase):
    def test_the_run_root_is_gitignored(self):
        """Staged directories never touch the tracked tree, by the rule `features/` already uses."""
        ignored = (PLUGIN_ROOT / ".gitignore").read_text().splitlines()
        self.assertIn(f"{RUN_ROOT}/", ignored)


if __name__ == "__main__":
    unittest.main()
