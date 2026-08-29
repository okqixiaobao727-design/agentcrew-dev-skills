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

TITLES = {
    "60": "The parent ticket",
    "61": "The first ticket",
    "62": "The second ticket",
    "70": "An outside ticket",
}

PARENT_BODY = "## What to build\n\nThe parent's own brief, which is this run's spec.\n"

# The routing the user approved at the checkpoint, in the shape the staging script takes it: one
# entry per ticket number, one key per line of `classify.md`'s template. Its effort differs from
# the effort the ticket body already carries, so a write-back is visible on the tracker.
APPROVED = {
    "workflow": "tdd",
    "executor": "claude",
    "model": CLAUDE_MODEL,
    "effort": "high",
    "review": f"codex {CODEX_MODEL} max",
    "reasons": "the approved routing.",
}

# The `## Routing` section `classify.md` templates, rendered from the entry above.
APPROVED_SECTION = f"""## Routing

Workflow: {APPROVED['workflow']}
Executor: {APPROVED['executor']}
Model: {APPROVED['model']}
Effort: {APPROVED['effort']}
Review: {APPROVED['review']}
Reasons: {APPROVED['reasons']}
"""

# The role strings `classify.md` names: an `acceptance` ticket is for a human, every other for an
# agent. The acceptance entry carries no `Review` line, which is the workflow's own rule.
ACCEPTANCE = {
    "workflow": "acceptance",
    "executor": "claude",
    "model": CLAUDE_MODEL,
    "effort": "medium",
    "reasons": "a human finishes it.",
}
# The account an approved entry may name: the one `## Routing` value `/route` records rather than
# concludes, and the one this machine's registry has to hold before a run may start.
ACCOUNT = "second"

AGENT_ROLE = "ready-for-agent"
HUMAN_ROLE = "ready-for-human"


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
        self.origin = self.root / "origin.git"
        subprocess.run(
            ["git", "init", "--bare", "-b", BASE_BRANCH, str(self.origin)],
            check=True, capture_output=True,
        )
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
        # The machine's account registry, moved off the real home by the override staging reads it
        # through. Nothing is written here until a test registers an account, which is the machine
        # of an operator who has never asked for a second one.
        self.registry = self.root / "accounts.toml"
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self._link_stub("gh", "stub_gh.py")
        (self.stub_dir / "gh-issues.json").write_text("{}")
        self.commit("base")
        git(self.repo, "remote", "add", "origin", str(self.origin))
        git(self.repo, "push", "-u", "origin", BASE_BRANCH)
        git(self.repo, "remote", "set-head", "origin", "-a")

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

    def issue(self, number, title=None, body=None, state="OPEN", blocked_by=(),
              native_blocked_by=(), sub_issues=(), page_size=None):
        """One ticket, written where this fixture's tracker keeps them."""
        title = TITLES.get(str(number), f"Ticket {number}") if title is None else title
        body = ticket_body(blocked_by) if body is None else body
        if self.tracker == "github":
            path = self.stub_dir / "gh-issues.json"
            table = json.loads(path.read_text())
            table[str(number)] = {
                "title": title, "body": body, "state": state,
                "blocked_by": [str(blocker) for blocker in native_blocked_by],
                "sub_issues": [str(child) for child in sub_issues],
                "page_size": page_size,
            }
            path.write_text(json.dumps(table))
            return str(number)
        path = self.tickets_dir / f"{number}.md"
        status = "done" if state == "CLOSED" else AGENT_ROLE
        path.write_text(f"# {title}\n\n{body}\nStatus: {status}\n")
        self.commit(f"ticket {number}")
        return str(path)

    def record(self, number):
        return json.loads((self.stub_dir / "gh-issues.json").read_text())[str(number)]

    def ticket_file(self, number):
        return (self.tickets_dir / f"{number}.md").read_text()

    def tracker_body(self, number):
        """The ticket's text as the tracker now holds it, after whatever staging wrote."""
        if self.tracker == "github":
            return self.record(number)["body"]
        return self.ticket_file(number)

    def tracker_role(self, number):
        """The role the ticket is marked with: its pickup label, or its `Status:` line."""
        if self.tracker == "github":
            marks = [name for name in self.record(number).get("labels", [])
                     if name in (AGENT_ROLE, HUMAN_ROLE)]
            return marks[-1] if marks else None
        for line in self.ticket_file(number).splitlines():
            if line.startswith("Status:"):
                return line.split(":", 1)[1].strip()
        return None

    def tracker_comments(self, number):
        """What has been commented on that ticket: its comments, or its `Crew:` lines."""
        if self.tracker == "github":
            return list(self.record(number).get("comments", []))
        return [line.split(":", 1)[1].strip()
                for line in self.ticket_file(number).splitlines() if line.startswith("Crew:")]

    def routing_file(self, table):
        """The approved routing, written where the script takes it from."""
        path = self.root / "routing.json"
        path.write_text(json.dumps(table))
        return str(path)

    # --- running it -----------------------------------------------------------------------

    def environment(self):
        environment = dict(os.environ)
        environment["PATH"] = f"{self.bin_dir}{os.pathsep}{environment['PATH']}"
        environment["AGENTCREW_STUB_DIR"] = str(self.stub_dir)
        environment["AGENTCREW_ACCOUNT_REGISTRY"] = str(self.registry)
        return environment

    def register(self, **accounts):
        """Write the machine-level registry mapping each account name to a profile directory."""
        lines = ["[accounts]"]
        for name, directory in accounts.items():
            pathlib.Path(directory).mkdir(parents=True, exist_ok=True)
            lines.append(f'{name} = "{directory}"')
        self.registry.write_text("\n".join(lines) + "\n")

    def profile(self, name):
        """A Claude Code profile directory on this fixture's machine."""
        return self.root / "profiles" / name

    def stage(self, *arguments):
        return subprocess.run(
            [sys.executable, str(STAGE), *arguments],
            capture_output=True, text=True, env=self.environment(), cwd=str(self.repo),
        )

    def run_dir(self, number=1):
        return self.repo / RUN_ROOT / str(number)

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


def parsed(directory, environment):
    """What the Run plan builder makes of that staged directory."""
    program = (
        "import json,pathlib,sys;"
        f"sys.path.insert(0, {str(STAGE.parent)!r});"
        f"sys.path.insert(0, {str(DRIVER.parent)!r});"
        f"sys.path.insert(0, {str(DRIVER.parent.parent)!r});"
        "import driver;"
        "import run_plan;"
        f"directory = pathlib.Path({str(directory)!r});"
        "repo = pathlib.Path(driver.git_output(directory, 'rev-parse', '--show-toplevel'));"
        "config = driver.project_config(repo);"
        "plan = run_plan.build("
        "directory, __import__('stage').candidate_run(repo, directory, config));"
        "tickets = [{"
        "'id': t.id, 'title': t.title, 'blocked_by': list(t.blocked_by),"
        "'effort': t.effort, 'account': t.binding.directory,"
        "'account_mode': t.binding.mode"
        "} for t in plan.tickets];"
        "print(json.dumps({"
        "'tickets': tickets,"
        "'problems': [],"
        "'waves': [[t.id for t in w.tickets] for w in plan.waves]"
        "}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, check=True,
        env=environment,
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

    def stage_approved(self, *arguments):
        """Stage an approved routing, committing the tracker's writes where the repo is the tracker.

        On the local tracker a ticket **is** a tracked file, so writing the approved routing into it
        dirties the tracked tree and the self-check says so — which is the designed answer, and the
        operator's move is to commit and stage again. That is what this does, so the tests below can
        state what a green staging leaves behind on either tracker.
        """
        result = self.fixture.stage(*arguments)
        if self.tracker == "local" and result.returncode != 0:
            self.fixture.commit("the tracker's own files, as the self-check asked")
            result = self.fixture.stage(*arguments)
        return result

    def stage_two_approved(self, table=None):
        first, second = self.two_tickets()
        routing = self.fixture.routing_file(
            {"61": dict(APPROVED), "62": dict(APPROVED)} if table is None else table
        )
        return self.stage_approved(first, second, "--routing", routing)

    # --- the contract ---------------------------------------------------------------------

    def test_help_documents_the_ticket_set_the_parent_flag_and_the_routing_input(self):
        """The invocation shape downstream couples to is the script's own help output."""
        result = self.fixture.stage("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--parent", result.stdout)
        self.assertIn("--routing", result.stdout)
        self.assertIn("ticket", result.stdout.lower())

    def test_green_staging_prints_one_crew_command_and_exits_zero(self):
        result = self.stage_two()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"/crew {RUN_ROOT}/1\n")

    def test_green_staging_prints_one_stderr_line_per_derived_wave(self):
        result = self.stage_two()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr.splitlines(), ["wave 1: #61", "wave 2: #62"])

    def test_green_staging_writes_a_spec_and_one_file_per_ticket_and_nothing_else(self):
        self.stage_two()
        held = sorted(path.name for path in self.fixture.run_dir().iterdir())
        self.assertEqual(held, ["61.md", "62.md", SPEC_NAME])

    def test_the_staged_directory_is_what_the_drivers_own_parsing_accepts(self):
        self.stage_two()
        read = parsed(self.fixture.run_dir(), self.fixture.environment())
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

    def test_a_malformed_review_is_a_named_blocking_item_and_withholds_the_command(self):
        malformed = ROUTING.replace(
            f"Review: codex {CODEX_MODEL} max",
            "Review: missing-effort",
        )
        ticket = self.fixture.issue(61, body=ticket_body(routing=malformed))

        result = self.fixture.stage(ticket)

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("/crew ", result.stdout)
        self.assertIn("Review", result.stderr)
        self.assertIn("vendor", result.stderr)
        self.assertIn("model", result.stderr)
        self.assertIn("effort", result.stderr)

    def test_an_empty_review_is_a_named_blocking_item_and_withholds_the_command(self):
        empty = ROUTING.replace(
            f"Review: codex {CODEX_MODEL} max",
            "Review:",
        )
        ticket = self.fixture.issue(61, body=ticket_body(routing=empty))

        result = self.fixture.stage(ticket)

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("/crew ", result.stdout)
        self.assertIn("Review", result.stderr)
        self.assertIn("vendor", result.stderr)
        self.assertIn("model", result.stderr)
        self.assertIn("effort", result.stderr)

    def test_a_missing_config_is_a_named_blocking_item_and_withholds_the_command(self):
        self.two_tickets()
        (self.fixture.repo / "agentcrew.toml").unlink()
        self.fixture.commit("drop the config")
        result = self.stage_two()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("/crew ", result.stdout)
        self.assertIn("agentcrew.toml", result.stderr)

    def test_a_missing_default_base_branch_withholds_the_command_and_names_the_fix(self):
        self.two_tickets()
        git(self.fixture.repo, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")

        result = self.stage_two()

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("/crew ", result.stdout)
        self.assertIn("git remote set-head origin -a", result.stderr)
        self.assertIn("--base-branch <branch>", result.stderr)

    # --- the dependency closure -----------------------------------------------------------

    def test_an_edge_to_a_closed_outside_set_blocker_is_stripped(self):
        self.fixture.issue(70, state="CLOSED")
        result = self.fixture.stage(self.fixture.issue(61, blocked_by=("70",)))
        self.assertEqual(result.returncode, 0, result.stderr)
        read = parsed(self.fixture.run_dir(), self.fixture.environment())
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

    # --- the tracker's side ------------------------------------------------------------------

    def test_the_approved_routing_section_reaches_the_tracker(self):
        result = self.stage_two_approved()
        self.assertEqual(result.returncode, 0, result.stderr)
        for number in ("61", "62"):
            body = self.fixture.tracker_body(number)
            self.assertIn(APPROVED_SECTION, body)
            self.assertEqual(body.count("## Routing"), 1)
            self.assertIn("## What to build", body)

    def test_a_ticket_carrying_no_routing_yet_is_given_the_approved_one(self):
        first = self.fixture.issue(61, body="## What to build\n\nA fixture with no routing.\n")
        routing = self.fixture.routing_file({"61": dict(APPROVED)})
        result = self.stage_approved(first, "--routing", routing)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(APPROVED_SECTION, self.fixture.tracker_body("61"))
        self.assertEqual(
            parsed(self.fixture.run_dir(), self.fixture.environment())["tickets"][0]["effort"],
            APPROVED["effort"],
        )

    def test_an_approved_account_reaches_the_ticket_and_the_drivers_own_parsing(self):
        self.fixture.register(second=self.fixture.profile("second"))
        approved = dict(APPROVED, account=ACCOUNT)
        result = self.stage_two_approved({"61": approved, "62": approved})
        self.assertEqual(result.returncode, 0, result.stderr)
        for number in ("61", "62"):
            self.assertIn(f"Account: {ACCOUNT}", self.fixture.tracker_body(number))
        self.assertEqual(
            [ticket["account"] for ticket in parsed(
                self.fixture.run_dir(), self.fixture.environment()
            )["tickets"]],
            [str(self.fixture.profile("second")), str(self.fixture.profile("second"))],
        )

    def test_an_approved_entry_naming_no_account_builds_an_inherited_binding(self):
        result = self.stage_two_approved()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Account:", self.fixture.tracker_body("61"))
        ticket = parsed(self.fixture.run_dir(), self.fixture.environment())["tickets"][0]
        self.assertEqual(ticket["account_mode"], "inherited")
        self.assertTrue(pathlib.Path(ticket["account"]).is_absolute())

    def test_an_account_this_machine_has_not_registered_is_a_named_blocking_item(self):
        approved = dict(APPROVED, account=ACCOUNT)
        result = self.stage_two_approved({"61": approved, "62": approved})
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(ACCOUNT, result.stderr)
        self.assertIn(str(self.fixture.registry), result.stderr)

    def test_the_role_label_each_workflow_names_reaches_the_tracker(self):
        result = self.stage_two_approved({"61": dict(APPROVED), "62": dict(ACCEPTANCE)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.fixture.tracker_role("61"), AGENT_ROLE)
        self.assertEqual(self.fixture.tracker_role("62"), HUMAN_ROLE)

    def test_the_staged_ticket_carries_the_approved_routing_the_tracker_was_given(self):
        result = self.stage_two_approved()
        self.assertEqual(result.returncode, 0, result.stderr)
        read = parsed(self.fixture.run_dir(), self.fixture.environment())
        self.assertEqual([ticket["effort"] for ticket in read["tickets"]],
                         [APPROVED["effort"], APPROVED["effort"]])

    def test_without_an_approved_routing_nothing_is_written_to_the_tracker(self):
        self.two_tickets()
        before = [self.fixture.tracker_body(number) for number in ("61", "62")]
        result = self.stage_two()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([self.fixture.tracker_body(number) for number in ("61", "62")], before)
        for number in ("61", "62"):
            self.assertEqual(self.fixture.tracker_comments(number), [])

    def test_a_green_staging_comments_the_command_on_every_ticket_of_a_parentless_set(self):
        result = self.stage_two_approved()
        self.assertEqual(result.returncode, 0, result.stderr)
        for number in ("61", "62"):
            self.assertEqual(self.fixture.tracker_comments(number), [f"/crew {RUN_ROOT}/1"])

    def test_re_staging_refreshes_the_commented_command_rather_than_duplicating_it(self):
        self.assertEqual(self.stage_two_approved().returncode, 0)
        second = self.stage_two_approved()
        self.assertEqual(second.returncode, 0, second.stderr)
        for number in ("61", "62"):
            self.assertEqual(self.fixture.tracker_comments(number), [f"/crew {RUN_ROOT}/1"])

    def test_a_failed_self_check_comments_no_command(self):
        self.two_tickets()
        (self.fixture.repo / "README.md").write_text("edited\n")
        routing = self.fixture.routing_file({"61": dict(APPROVED), "62": dict(APPROVED)})
        result = self.fixture.stage(*self.refs, "--routing", routing)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("/crew ", result.stdout)
        for number in ("61", "62"):
            self.assertEqual(self.fixture.tracker_comments(number), [])

    def test_an_approved_routing_naming_no_ticket_of_the_set_is_a_named_blocking_item(self):
        first, second = self.two_tickets()
        routing = self.fixture.routing_file({"61": dict(APPROVED), "99": dict(APPROVED)})
        result = self.fixture.stage(first, second, "--routing", routing)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("/crew ", result.stdout)
        self.assertIn("99", result.stderr)

    def test_an_approved_routing_missing_a_line_of_the_template_is_a_named_blocking_item(self):
        first, second = self.two_tickets()
        short = {key: value for key, value in APPROVED.items() if key != "effort"}
        routing = self.fixture.routing_file({"61": short, "62": dict(APPROVED)})
        result = self.fixture.stage(first, second, "--routing", routing)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("/crew ", result.stdout)
        self.assertIn("61", result.stderr)
        self.assertIn("effort", result.stderr.lower())


class LocalTrackerTests(StagingTests):
    """The same behaviour on the tracker whose tickets are files in the repository."""

    tracker = "local"

    def test_dependency_staging_makes_no_github_api_call(self):
        result = self.stage_two()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.fixture.stub_dir / "gh-calls.jsonl").exists())


class GithubDependencyTests(unittest.TestCase):
    """Dependency edges that exist only in GitHub's native relationship."""

    def setUp(self):
        self.fixture = Fixture("github")
        self.addCleanup(self.fixture.cleanup)

    def test_a_native_github_edge_reaches_the_staged_copy_and_wave_graph(self):
        first = self.fixture.issue(61)
        second = self.fixture.issue(62, native_blocked_by=("61",))

        result = self.fixture.stage(first, second)

        self.assertEqual(result.returncode, 0, result.stderr)
        read = parsed(self.fixture.run_dir(), self.fixture.environment())
        self.assertEqual(read["tickets"][1]["blocked_by"], ["61"])
        self.assertEqual(read["waves"], [["61"], ["62"]])

    def test_body_and_paginated_native_edges_are_unioned_and_deduplicated(self):
        first = self.fixture.issue(61)
        outside = self.fixture.issue(70)
        second = self.fixture.issue(
            62,
            blocked_by=("61",),
            native_blocked_by=("61", "70"),
            page_size=1,
        )

        result = self.fixture.stage(first, outside, second)

        self.assertEqual(result.returncode, 0, result.stderr)
        read = parsed(self.fixture.run_dir(), self.fixture.environment())
        ticket = next(ticket for ticket in read["tickets"] if ticket["id"] == "62")
        self.assertEqual(ticket["blocked_by"], ["61", "70"])
        staged = (self.fixture.run_dir() / "62.md").read_text()
        self.assertEqual(staged.count("#61"), 1)
        self.assertEqual(staged.count("#70"), 1)
        self.assertEqual(read["waves"], [["61", "70"], ["62"]])

    def test_a_body_only_edge_is_staged_byte_for_byte_as_before(self):
        first = self.fixture.issue(61)
        body = ticket_body(blocked_by=("61",))
        second = self.fixture.issue(62, body=body)

        result = self.fixture.stage(first, second)

        self.assertEqual(result.returncode, 0, result.stderr)
        expected = f"# {TITLES['62']}\n\n{body.rstrip()}\n"
        self.assertEqual((self.fixture.run_dir() / "62.md").read_text(), expected)

    def test_a_closed_native_outside_set_edge_is_spent_in_the_staged_copy(self):
        self.fixture.issue(70, state="CLOSED")
        ticket = self.fixture.issue(61, native_blocked_by=("70",))

        result = self.fixture.stage(ticket)

        self.assertEqual(result.returncode, 0, result.stderr)
        read = parsed(self.fixture.run_dir(), self.fixture.environment())
        self.assertEqual(read["tickets"][0]["blocked_by"], [])
        staged = (self.fixture.run_dir() / "61.md").read_text()
        self.assertIn("70 (closed)", staged)
        self.assertNotIn("#70", staged)

    def test_an_open_native_outside_set_edge_is_a_named_blocking_item(self):
        self.fixture.issue(70, state="OPEN")
        ticket = self.fixture.issue(61, native_blocked_by=("70",))

        result = self.fixture.stage(ticket)

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("/crew ", result.stdout)
        self.assertIn("70", result.stderr)
        self.assertIn("61", result.stderr)

    def test_native_dependency_projection_does_not_edit_the_tracker_body(self):
        first = self.fixture.issue(61)
        second = self.fixture.issue(62, native_blocked_by=("61",))
        before = self.fixture.tracker_body("62")

        result = self.fixture.stage(first, second)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.fixture.tracker_body("62"), before)
        self.assertIn("#61", (self.fixture.run_dir() / "62.md").read_text())


class ParentExpansionTests(unittest.TestCase):
    """`--parent <n>` on the tracker whose parent-child links are native sub-issues."""

    def setUp(self):
        self.fixture = Fixture("github")
        self.addCleanup(self.fixture.cleanup)

    def parent(self, sub_issues=("61", "62")):
        self.fixture.issue(61)
        self.fixture.issue(62, blocked_by=("61",))
        self.fixture.issue(60, body=PARENT_BODY, sub_issues=sub_issues)
        return "60"

    def test_the_parent_expands_to_its_sub_issues(self):
        result = self.fixture.stage("--parent", self.parent())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"/crew {RUN_ROOT}/1\n")
        held = sorted(path.name for path in self.fixture.run_dir().iterdir())
        self.assertEqual(held, ["61.md", "62.md", SPEC_NAME])

    def test_the_spec_is_the_parent_ticket_body_and_carries_the_provenance(self):
        result = self.fixture.stage("--parent", self.parent())
        self.assertEqual(result.returncode, 0, result.stderr)
        spec = (self.fixture.run_dir() / SPEC_NAME).read_text()
        self.assertIn(TITLES["60"], spec)
        self.assertIn("The parent's own brief, which is this run's spec.", spec)
        self.assertTrue(
            any(line.startswith("Tracker:") and "60" in line for line in spec.splitlines()),
            f"no Tracker: provenance line naming the parent in {spec!r}",
        )

    def test_a_sub_issue_list_the_tracker_answers_a_page_at_a_time_expands_whole(self):
        """A parent with more sub-issues than one page holds expands the same as a smaller one."""
        self.fixture.issue(61)
        self.fixture.issue(62, blocked_by=("61",))
        self.fixture.issue(60, body=PARENT_BODY, sub_issues=("61", "62"), page_size=1)
        result = self.fixture.stage("--parent", "60")
        self.assertEqual(result.returncode, 0, result.stderr)
        held = sorted(path.name for path in self.fixture.run_dir().iterdir())
        self.assertEqual(held, ["61.md", "62.md", SPEC_NAME])

    def test_a_closed_sub_issue_is_left_out_of_the_set(self):
        self.fixture.issue(61)
        self.fixture.issue(70, state="CLOSED")
        self.fixture.issue(60, body=PARENT_BODY, sub_issues=("61", "70"))
        result = self.fixture.stage("--parent", "60")
        self.assertEqual(result.returncode, 0, result.stderr)
        held = sorted(path.name for path in self.fixture.run_dir().iterdir())
        self.assertEqual(held, ["61.md", SPEC_NAME])

    def test_a_parent_with_no_open_sub_issues_is_a_named_blocking_item(self):
        self.fixture.issue(70, state="CLOSED")
        self.fixture.issue(60, body=PARENT_BODY, sub_issues=("70",))
        result = self.fixture.stage("--parent", "60")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("/crew ", result.stdout)
        self.assertIn("60", result.stderr)

    def test_the_command_is_commented_on_the_parent_and_not_on_its_sub_issues(self):
        routing = self.fixture.routing_file({"61": dict(APPROVED), "62": dict(APPROVED)})
        result = self.fixture.stage("--parent", self.parent(), "--routing", routing)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.fixture.tracker_comments("60"), [f"/crew {RUN_ROOT}/1"])
        for number in ("61", "62"):
            self.assertEqual(self.fixture.tracker_comments(number), [])

    def test_re_running_for_the_same_parent_overwrites_the_same_directory(self):
        first = self.fixture.stage("--parent", self.parent())
        self.assertEqual(first.returncode, 0, first.stderr)
        self.fixture.issue(60, body=PARENT_BODY, sub_issues=("61",))

        second = self.fixture.stage("--parent", "60")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(second.stdout, first.stdout)
        self.assertEqual(
            sorted(path.name for path in (self.fixture.repo / RUN_ROOT).iterdir()), ["1"]
        )

    def test_a_parent_and_an_explicit_ticket_set_are_two_entrances_not_one(self):
        result = self.fixture.stage("--parent", self.parent(), "61")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("/crew ", result.stdout)


class LocalTicketReferenceTests(unittest.TestCase):
    """What the tracker whose tickets are files accepts as a reference to one."""

    def setUp(self):
        self.fixture = Fixture("local")
        self.addCleanup(self.fixture.cleanup)

    def test_the_parent_flag_is_a_named_blocking_item_on_the_local_tracker(self):
        result = self.fixture.stage("--parent", "60")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("/crew ", result.stdout)
        self.assertIn("sub-issues", result.stderr)

    def test_a_ticket_path_outside_the_repository_is_a_named_blocking_item(self):
        """A local ticket is a file in the repository, and staging writes the file it was given."""
        outside = self.fixture.root / "61.md"
        outside.write_text(f"# Outside\n\n{ticket_body()}\nStatus: {AGENT_ROLE}\n")
        routing = self.fixture.routing_file({"61": dict(APPROVED)})
        result = self.fixture.stage(str(outside), "--routing", routing)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("/crew ", result.stdout)
        self.assertIn(str(self.fixture.repo), result.stderr)
        self.assertNotIn(APPROVED["effort"], outside.read_text())


class ShippedTreeTests(unittest.TestCase):
    def test_the_run_root_is_gitignored(self):
        """Staged directories never touch the tracked tree, by the rule `features/` already uses."""
        ignored = (PLUGIN_ROOT / ".gitignore").read_text().splitlines()
        self.assertIn(f"{RUN_ROOT}/", ignored)


if __name__ == "__main__":
    unittest.main()
