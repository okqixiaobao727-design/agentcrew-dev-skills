#!/usr/bin/env python3
"""Drive the monitor's operator surface from its command line against fixture receipts.

Every fixture is a real git repository with a worktree per ticket, a machine log written by hand
in the schema `docs/machine-log.md` publishes, and a stub PATH carrying `claude` and `tmux`.
Assertions are on external behaviour only — the table the pane draws, the toasts tmux was asked to
display, the verdict line, the log lines that follow it, and the exit code.
"""

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


TESTS_DIR = pathlib.Path(__file__).resolve().parent
MONITOR = TESTS_DIR.parent / "monitor.py"

# One wave, stamped in the run's one timestamp format, so every elapsed time below is arithmetic
# a reader can check: 09:00:00 to 09:12:31 is twelve minutes and thirty-one seconds.
WAVE = 1
LAUNCH_TS = "2026-08-13T09:00:00Z"
NOW_TS = "2026-08-13T09:12:31Z"
LIVE_ELAPSED = "00:12:31"
SETTLED_TS = "2026-08-13T09:41:07Z"
SETTLED_ELAPSED = "00:41:07"

CHILDREN = {"06": "crew-06-dispatch", "07": "crew-07-log"}
MODEL = "claude-opus-4-5-20251101"
CODEX_MODEL = "gpt-5.6-luna"

# Two assistant turns of a fabricated Claude transcript, and what the four disjoint counters come
# to once both are counted: 11+13 in, 22+24 out, 3300+3500 read from cache, 440+460 written to it,
# and 7770 tokens moved in all.
CLAUDE_TURNS = (
    {"input": 11, "output": 22, "cache_read": 3300, "cache_creation": 440},
    {"input": 13, "output": 24, "cache_read": 3500, "cache_creation": 460},
)
CLAUDE_TOTALS = {
    "input": 24, "output": 46, "cache_read": 6800, "cache_creation": 900, "total": 7770,
}
# What a Codex rollout's last `total_token_usage` reports, and the same four disjoint counters
# read off it: its `input_tokens` counts the cached tokens inside itself, so the uncached input is
# 5000 - 4000, and 1000+700+4000+250 tokens moved in all.
CODEX_USAGE = {
    "input_tokens": 5000, "cached_input_tokens": 4000,
    "cache_write_input_tokens": 250, "output_tokens": 700,
    "reasoning_output_tokens": 120, "total_tokens": 5700,
}
CODEX_TOTALS = {
    "input": 1000, "output": 700, "cache_read": 4000, "cache_creation": 250, "total": 5950,
}
CLAUDE_SESSION = "9d1f4c2a-0000-4000-8000-000000000001"
CODEX_SESSION = "019ffe0e-e154-7a93-88c2-3be07fd543cd"

# The guard assets the dispatch renderer installs into every Claude worktree before its child
# starts; the child never commits them, so they are not what makes a tree dirty.
GUARD_ASSETS = ("red-line.sh", "worktree-guard.sh", "settings.local.json")


def run_git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


class Fixture:
    """A temporary run: a repository, a worktree per ticket, a machine log, and a stub PATH."""

    def __init__(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.repo = self.root / "repo"
        self.repo.mkdir()
        run_git(self.repo, "init", "-b", "main")
        run_git(self.repo, "config", "user.email", "crew@example.invalid")
        run_git(self.repo, "config", "user.name", "Crew Test")
        (self.repo / "README.md").write_text("fixture\n")
        run_git(self.repo, "add", "README.md")
        run_git(self.repo, "commit", "-m", "base")
        self.base_commit = run_git(self.repo, "rev-parse", "HEAD")

        self.log = self.root / "run" / "machine-log.jsonl"
        self.log.parent.mkdir()
        self.toast_state = self.root / "run" / "toasts.json"

        # Where the two executors keep the transcripts the cost pass reads, pointed at this
        # fixture so nothing on the machine running the tests is ever opened.
        self.claude_home = self.root / "claude-config"
        self.codex_home = self.root / "codex-home"

        self.stub_dir = self.root / "stub"
        self.stub_dir.mkdir()
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self._link_stub("claude", "stub_claude.py")
        self._link_stub("tmux", "stub_tmux.py")

        self.worktrees = {}

    def _link_stub(self, name, script):
        target = self.bin_dir / name
        target.write_text(
            "#!/bin/sh\nexec %s %s \"$@\"\n" % (sys.executable, TESTS_DIR / script)
        )
        target.chmod(0o755)

    def worktree(self, ticket, commits=1):
        """The ticket's worktree, cut from the base commit and carrying `commits` of its own."""
        path = self.root / "worktrees" / f"worktree-{ticket}"
        run_git(self.repo, "worktree", "add", "-b", f"worktree-{ticket}", str(path),
                self.base_commit)
        for number in range(commits):
            (path / f"work-{number}.txt").write_text(f"{ticket} {number}\n")
            run_git(path, "add", f"work-{number}.txt")
            run_git(path, "commit", "-m", f"{ticket} work {number}")
        self.worktrees[ticket] = path
        return path

    def unrelated_commit(self):
        """A commit in the same repository that shares no history with any ticket branch."""
        path = self.root / "unrelated"
        run_git(self.repo, "worktree", "add", "--detach", str(path), self.base_commit)
        run_git(path, "checkout", "--orphan", "unrelated")
        run_git(path, "rm", "-rf", ".")
        (path / "elsewhere.txt").write_text("another history\n")
        run_git(path, "add", "elsewhere.txt")
        run_git(path, "commit", "-m", "unrelated")
        return run_git(path, "rev-parse", "HEAD")

    def install_guard_assets(self, ticket):
        target = self.worktrees[ticket] / ".claude"
        target.mkdir(parents=True, exist_ok=True)
        for name in GUARD_ASSETS:
            (target / name).write_text("{}\n")

    def head(self, ticket):
        return run_git(self.worktrees[ticket], "rev-parse", "HEAD")

    def append(self, ts, event, **fields):
        record = {"ts": ts, "event": event}
        record.update(fields)
        with self.log.open("a") as handle:
            handle.write(json.dumps(record) + "\n")

    def launch(self, ticket, ts=LAUNCH_TS, executor="claude", model=MODEL):
        self.append(
            ts, "launch", ticket=ticket, child=CHILDREN[ticket], workflow="tdd",
            executor=executor, model=model, effort="medium",
            branch=f"worktree-{ticket}", worktree=str(self.worktrees[ticket]),
            window=f"@{ticket}",
        )

    def claude_transcript(self, ticket, session=CLAUDE_SESSION, turns=CLAUDE_TURNS, model=MODEL):
        """A Claude transcript for that worktree, one assistant record per turn's usage."""
        path = self.claude_home / "projects" / f"project-{session}" / f"{session}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        for number, turn in enumerate(turns):
            lines.append(json.dumps({
                "type": "assistant",
                "uuid": f"{session}-{number}",
                "requestId": f"req_{session}_{number}",
                "sessionId": session,
                "cwd": str(self.worktrees[ticket]),
                "message": {
                    "id": f"msg_{session}_{number}",
                    "model": model,
                    "usage": {
                        "input_tokens": turn["input"],
                        "output_tokens": turn["output"],
                        "cache_read_input_tokens": turn["cache_read"],
                        "cache_creation_input_tokens": turn["cache_creation"],
                    },
                },
            }))
        path.write_text("\n".join(lines) + "\n")
        return path

    def codex_rollout(self, ticket, session=CODEX_SESSION, usage=CODEX_USAGE, text=None):
        """A Codex rollout for that worktree: its session meta, then its last token count."""
        path = self.codex_home / "sessions" / "2026" / "08" / "13" / f"rollout-{session}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        if text is None:
            text = "\n".join([
                json.dumps({
                    "type": "session_meta",
                    "payload": {
                        "id": session,
                        "cwd": str(self.worktrees[ticket]),
                        "originator": "agentcrew_codex_bridge",
                    },
                }),
                json.dumps({
                    "type": "event_msg",
                    "payload": {"type": "token_count", "info": {"total_token_usage": usage}},
                }),
            ]) + "\n"
        path.write_text(text)
        return path

    def log_lines(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines() if line]

    def agents(self, entries):
        """The agents list `claude agents --json` answers with, keyed by ticket."""
        self.agents_at([
            (ticket, self.worktrees[ticket], status) for ticket, status in entries.items()
        ])

    def agents_at(self, entries):
        """The same list, each session's `cwd` spelled as the caller wants it spelled."""
        (self.stub_dir / "agents.json").write_text(json.dumps([
            {
                "pid": 4000 + index,
                "cwd": str(cwd),
                "kind": "interactive",
                "sessionId": f"session-{ticket}-{index}",
                "name": CHILDREN[ticket],
                "status": status,
            }
            for index, (ticket, cwd, status) in enumerate(entries)
        ]))

    def alias(self, ticket):
        """The ticket's worktree addressed through a symlink — the `/tmp` vs `/private/tmp` shape.

        macOS reaches the same directory by two spellings; a symlink is that aliasing made
        portable, so the comparison under test is the one a real run meets.
        """
        link = self.root / "alias"
        if not link.exists():
            link.symlink_to(self.root / "worktrees", target_is_directory=True)
        return link / f"worktree-{ticket}"

    def environment(self):
        environment = dict(os.environ)
        environment["PATH"] = f"{self.bin_dir}{os.pathsep}{environment['PATH']}"
        environment["AGENTCREW_STUB_DIR"] = str(self.stub_dir)
        environment["CLAUDE_CONFIG_DIR"] = str(self.claude_home)
        environment["CODEX_HOME"] = str(self.codex_home)
        return environment

    def run_monitor(self, *args):
        return subprocess.run(
            [sys.executable, str(MONITOR), *[str(argument) for argument in args]],
            capture_output=True, text=True, env=self.environment(),
        )

    def start_monitor(self, *args):
        return subprocess.Popen(
            [sys.executable, str(MONITOR), *[str(argument) for argument in args]],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=self.environment(),
        )

    def dashboard(self, *extra, tickets=("06", "07"), paths=None):
        """Draw the wave, addressing its worktrees as `paths` spells them when given."""
        return self.run_monitor(
            "dashboard", "--log", self.log, "--wave", WAVE, "--now", NOW_TS,
            "--toast-state", self.toast_state, *extra,
            *(paths if paths is not None else [self.worktrees[ticket] for ticket in tickets]),
        )

    def calls(self, name):
        path = self.stub_dir / f"{name}-calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line)["argv"] for line in path.read_text().splitlines() if line]

    def toasts(self):
        return [argv[-1] for argv in self.calls("tmux") if argv[:1] == ["display-message"]]

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


def rows(output):
    """The dashboard's data rows, each split into its six fields."""
    lines = output.splitlines()
    header = next(index for index, line in enumerate(lines) if line.startswith("WAVE"))
    return [line.split() for line in lines[header + 1:] if line.strip()]


def cost_rows(output):
    """The rollup's header and rows, each split into its fields, down to the first blank line."""
    lines = output.splitlines()
    header = next(index for index, line in enumerate(lines) if line.startswith("TICKET"))
    rows = []
    for line in lines[header:]:
        if not line.strip():
            break
        rows.append(line.split())
    return rows


class MonitorTestCase(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()
        self.addCleanup(self.fixture.cleanup)


class ReceiptVerificationTests(MonitorTestCase):
    def verify(self, ticket, sha, base=None, log=True):
        worktree = self.fixture.worktrees[ticket]
        arguments = [
            "verify", "--ticket", ticket, "--worktree", worktree, "--sha", sha,
            "--base", base or self.fixture.base_commit,
        ]
        if log:
            arguments += ["--log", self.fixture.log]
        return self.fixture.run_monitor(*arguments)

    def test_a_receipt_matching_the_worktree_head_is_landable_and_logged(self):
        self.fixture.worktree("06")
        head = self.fixture.head("06")

        result = self.verify("06", head)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"06 landable {head}")
        self.assertEqual(
            [(line["event"], line.get("ticket"), line.get("verdict"), line.get("sha"))
             for line in self.fixture.log_lines()],
            [("receipt", "06", "landable", head)],
        )

    def test_a_receipt_whose_tail_is_invented_is_invalid(self):
        self.fixture.worktree("06")
        head = self.fixture.head("06")
        invented = head[:7] + ("0" * 33 if head[7] != "0" else "1" * 33)

        result = self.verify("06", invented)

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertTrue(result.stdout.startswith("06 invalid"), result.stdout)
        self.assertIn("head", result.stdout)
        self.assertEqual(self.fixture.log_lines(), [])

    def test_a_receipt_shorter_than_forty_characters_is_invalid(self):
        self.fixture.worktree("06")

        result = self.verify("06", self.fixture.head("06")[:7])

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertTrue(result.stdout.startswith("06 invalid"), result.stdout)
        self.assertEqual(self.fixture.log_lines(), [])

    def test_a_branch_no_commits_ahead_of_its_base_is_invalid(self):
        self.fixture.worktree("06", commits=0)

        result = self.verify("06", self.fixture.head("06"))

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("ahead", result.stdout)
        self.assertEqual(self.fixture.log_lines(), [])

    def test_work_left_uncommitted_in_the_worktree_is_invalid(self):
        worktree = self.fixture.worktree("06")
        (worktree / "unfinished.txt").write_text("half a feature\n")

        result = self.verify("06", self.fixture.head("06"))

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("unfinished.txt", result.stdout)
        self.assertEqual(self.fixture.log_lines(), [])

    def test_the_installed_guard_assets_do_not_make_a_worktree_dirty(self):
        self.fixture.worktree("06")
        self.fixture.install_guard_assets("06")
        head = self.fixture.head("06")

        result = self.verify("06", head)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), f"06 landable {head}")

    def test_work_renamed_onto_a_guard_asset_path_is_still_uncommitted(self):
        worktree = self.fixture.worktree("06")
        (worktree / ".claude").mkdir()
        run_git(worktree, "mv", "work-0.txt", ".claude/red-line.sh")

        result = self.verify("06", self.fixture.head("06"))

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn(".claude/red-line.sh", result.stdout)
        self.assertEqual(self.fixture.log_lines(), [])

    def test_a_base_the_branch_never_grew_from_is_invalid(self):
        self.fixture.worktree("06")

        result = self.verify("06", self.fixture.head("06"), base=self.fixture.unrelated_commit())

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("descend", result.stdout)
        self.assertEqual(self.fixture.log_lines(), [])

    def test_a_worktree_that_is_not_a_repository_is_a_monitor_error(self):
        worktree = self.fixture.root / "not-a-repo"
        worktree.mkdir()
        self.fixture.worktrees["06"] = worktree

        result = self.verify("06", "0" * 40)

        self.assertEqual(result.returncode, 3, result.stdout)
        self.assertIn("MONITOR ERROR", result.stderr)
        self.assertEqual(self.fixture.log_lines(), [])


class DashboardTests(MonitorTestCase):
    def test_one_row_per_launched_ticket_carries_the_wave_ticket_child_state_and_elapsed(self):
        self.fixture.worktree("06")
        self.fixture.worktree("07")
        self.fixture.launch("06")
        self.fixture.launch("07")
        self.fixture.agents({"06": "busy", "07": "waiting"})

        result = self.fixture.dashboard()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("LAST EVENT", result.stdout)
        self.assertEqual(
            rows(result.stdout),
            [
                ["1", "06", CHILDREN["06"], "busy", "launch", LIVE_ELAPSED],
                ["1", "07", CHILDREN["07"], "waiting", "launch", LIVE_ELAPSED],
            ],
        )

    def test_a_settled_ticket_shows_its_verdict_and_stops_its_clock(self):
        self.fixture.worktree("06")
        self.fixture.launch("06")
        self.fixture.append(SETTLED_TS, "receipt", ticket="06", verdict="landable",
                            sha=self.fixture.head("06"))
        self.fixture.agents({"06": "idle"})

        result = self.fixture.dashboard(tickets=("06",))

        self.assertEqual(
            rows(result.stdout),
            [["1", "06", CHILDREN["06"], "landable", "receipt", SETTLED_ELAPSED]],
        )

    def test_a_child_missing_from_the_agents_list_is_vanished(self):
        self.fixture.worktree("06")
        self.fixture.worktree("07")
        self.fixture.launch("06")
        self.fixture.launch("07")
        self.fixture.agents({"06": "busy"})

        result = self.fixture.dashboard()

        self.assertEqual(
            [row[1:4] for row in rows(result.stdout)],
            [["06", CHILDREN["06"], "busy"], ["07", CHILDREN["07"], "vanished"]],
        )

    def test_an_unreadable_agents_list_draws_unknown_and_keeps_the_pane(self):
        self.fixture.worktree("06")
        self.fixture.launch("06")

        result = self.fixture.dashboard(tickets=("06",))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(rows(result.stdout)[0][3], "unknown")

    def test_a_worktree_with_no_launch_is_not_a_row(self):
        self.fixture.worktree("06")
        self.fixture.worktree("07")
        self.fixture.launch("06")
        self.fixture.agents({"06": "busy", "07": "busy"})

        result = self.fixture.dashboard()

        self.assertEqual([row[1] for row in rows(result.stdout)], ["06"])

    def test_the_pane_redraws_as_the_log_changes(self):
        self.fixture.worktree("06")
        self.fixture.launch("06")
        self.fixture.agents({"06": "busy"})
        process = self.fixture.start_monitor(
            "dashboard", "--log", self.fixture.log, "--wave", WAVE, "--now", NOW_TS,
            "--toast-state", self.fixture.toast_state, "--refresh", "0.05",
            self.fixture.worktrees["06"],
        )
        self.addCleanup(process.kill)

        time.sleep(0.5)
        self.fixture.append(SETTLED_TS, "receipt", ticket="06", verdict="landable",
                            sha=self.fixture.head("06"))
        time.sleep(0.5)
        process.terminate()
        output = process.communicate(timeout=10)[0]

        self.assertIn("busy", output)
        self.assertIn("landable", output)
        self.assertGreater(output.count(CHILDREN["06"]), 1)


class ToastTests(MonitorTestCase):
    def test_a_stuck_child_a_vanished_child_and_an_escalation_each_toast(self):
        self.fixture.worktree("06")
        self.fixture.worktree("07")
        self.fixture.launch("06")
        self.fixture.launch("07")
        self.fixture.append(NOW_TS, "escalation", ticket="06", role="child",
                            message="CREW ASK 06 stuck — ts=1755060042")
        self.fixture.agents({"06": "waiting"})

        self.fixture.dashboard()

        self.assertEqual(
            sorted(self.fixture.toasts()),
            sorted([
                "crew 06 stuck at a permission prompt",
                "crew 06 escalated",
                "crew 07 vanished",
            ]),
        )

    def test_a_wave_whose_every_ticket_is_settled_toasts_once(self):
        self.fixture.worktree("06")
        self.fixture.worktree("07")
        self.fixture.launch("06")
        self.fixture.launch("07")
        self.fixture.append(SETTLED_TS, "receipt", ticket="06", verdict="landable",
                            sha=self.fixture.head("06"))
        self.fixture.append(SETTLED_TS, "receipt", ticket="07", verdict="failed")
        self.fixture.agents({})

        self.fixture.dashboard()
        self.fixture.dashboard()

        self.assertEqual(self.fixture.toasts(), ["crew wave 1 complete"])

    def test_an_unfinished_wave_does_not_toast_complete(self):
        self.fixture.worktree("06")
        self.fixture.worktree("07")
        self.fixture.launch("06")
        self.fixture.launch("07")
        self.fixture.append(SETTLED_TS, "receipt", ticket="06", verdict="landable",
                            sha=self.fixture.head("06"))
        self.fixture.agents({"07": "busy"})

        self.fixture.dashboard()

        self.assertEqual(self.fixture.toasts(), [])

    def test_an_exception_is_not_toasted_twice(self):
        self.fixture.worktree("06")
        self.fixture.launch("06")
        self.fixture.agents({"06": "waiting"})

        self.fixture.dashboard(tickets=("06",))
        self.fixture.dashboard(tickets=("06",))

        self.assertEqual(self.fixture.toasts(), ["crew 06 stuck at a permission prompt"])

    def test_nothing_the_monitor_emits_reaches_the_coordinator(self):
        self.fixture.worktree("06")
        self.fixture.launch("06")
        self.fixture.agents({"06": "waiting"})

        self.fixture.dashboard(tickets=("06",))

        self.assertEqual(self.fixture.calls("claude"), [["agents", "--json"]])
        self.assertTrue(
            all(argv[:1] == ["display-message"] for argv in self.fixture.calls("tmux")),
            self.fixture.calls("tmux"),
        )


class AliasedPathTests(MonitorTestCase):
    """A worktree addressed by a second spelling of the same directory is the same worktree.

    Path equality here decides whether a live child is drawn as `vanished`, toasted as lost, and
    woken over — so it is decided by what the paths resolve to, never by how they were spelled.
    """

    def test_a_wave_addressed_by_an_aliased_spelling_draws_what_the_canonical_one_draws(self):
        self.fixture.worktree("06")
        self.fixture.worktree("07")
        self.fixture.launch("06")
        self.fixture.launch("07")
        self.fixture.agents({"06": "busy", "07": "waiting"})

        canonical = self.fixture.dashboard()
        aliased = self.fixture.dashboard(
            paths=[self.fixture.alias("06"), self.fixture.alias("07")]
        )

        self.assertEqual(aliased.returncode, 0, aliased.stderr)
        self.assertEqual(
            rows(aliased.stdout),
            [
                ["1", "06", CHILDREN["06"], "busy", "launch", LIVE_ELAPSED],
                ["1", "07", CHILDREN["07"], "waiting", "launch", LIVE_ELAPSED],
            ],
        )
        self.assertEqual(rows(aliased.stdout), rows(canonical.stdout))

    def test_a_session_listed_under_an_aliased_cwd_is_not_vanished(self):
        self.fixture.worktree("06")
        self.fixture.launch("06")
        self.fixture.agents_at([("06", self.fixture.alias("06"), "busy")])

        result = self.fixture.dashboard(tickets=("06",))

        self.assertEqual(rows(result.stdout)[0][3], "busy")
        self.assertEqual(self.fixture.toasts(), [])

    def test_two_spellings_of_one_worktree_are_one_duplicated_session(self):
        self.fixture.worktree("06")
        self.fixture.launch("06")
        self.fixture.agents_at([
            ("06", self.fixture.worktrees["06"], "busy"),
            ("06", self.fixture.alias("06"), "busy"),
        ])

        result = self.fixture.dashboard(tickets=("06",))

        self.assertEqual(rows(result.stdout)[0][3], "duplicate")


class PaneTests(MonitorTestCase):
    def test_the_pane_runs_the_dashboard_refresh_loop_in_the_run_session(self):
        self.fixture.worktree("06")
        self.fixture.launch("06")

        result = self.fixture.run_monitor(
            "pane", "--session", "$7:", "--log", self.fixture.log, "--wave", WAVE,
            self.fixture.worktrees["06"],
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "%9")
        argv = self.fixture.calls("tmux")[0]
        self.assertEqual(argv[0], "split-window")
        self.assertIn("$7:", argv)
        command = argv[-1]
        self.assertIn("dashboard", command)
        self.assertIn("--refresh", command)
        self.assertIn(str(self.fixture.log), command)
        self.assertIn(str(self.fixture.worktrees["06"]), command)


class CostTests(MonitorTestCase):
    """The cost pass at run completion: one event per child, and the run's rollup."""

    def cost(self):
        return self.fixture.run_monitor("cost", "--log", self.fixture.log)

    def costs(self):
        """The session-cost events in the log, in the order they were appended."""
        return [line for line in self.fixture.log_lines() if line["event"] == "session-cost"]

    def test_a_claude_child_costs_what_its_transcripts_usage_adds_up_to(self):
        self.fixture.worktree("06")
        self.fixture.launch("06")
        self.fixture.claude_transcript("06")

        result = self.cost()

        self.assertEqual(result.returncode, 0, result.stderr)
        entries = self.costs()
        self.assertEqual(len(entries), 1)
        self.assertRegex(entries[0].pop("ts"), r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual(
            entries,
            [{
                "event": "session-cost",
                "ticket": "06",
                "executor": "claude",
                "model": MODEL,
                "session": CLAUDE_SESSION,
                "input_tokens": CLAUDE_TOTALS["input"],
                "output_tokens": CLAUDE_TOTALS["output"],
                "cache_read_tokens": CLAUDE_TOTALS["cache_read"],
                "cache_creation_tokens": CLAUDE_TOTALS["cache_creation"],
                "total_tokens": CLAUDE_TOTALS["total"],
            }],
        )

    def test_a_codex_child_costs_what_its_rollouts_last_token_count_reports(self):
        self.fixture.worktree("07")
        self.fixture.launch("07", executor="codex", model=CODEX_MODEL)
        self.fixture.codex_rollout("07")

        result = self.cost()

        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.costs()[0]
        self.assertEqual(entry["ticket"], "07")
        self.assertEqual(entry["executor"], "codex")
        self.assertEqual(entry["model"], CODEX_MODEL)
        self.assertEqual(entry["session"], CODEX_SESSION)
        self.assertEqual(entry["input_tokens"], CODEX_TOTALS["input"])
        self.assertEqual(entry["output_tokens"], CODEX_TOTALS["output"])
        self.assertEqual(entry["cache_read_tokens"], CODEX_TOTALS["cache_read"])
        self.assertEqual(entry["cache_creation_tokens"], CODEX_TOTALS["cache_creation"])
        self.assertEqual(entry["total_tokens"], CODEX_TOTALS["total"])

    def test_the_rollup_carries_a_row_per_child_and_a_total_that_adds_them_up(self):
        self.fixture.worktree("06")
        self.fixture.worktree("07")
        self.fixture.launch("06")
        self.fixture.launch("07", executor="codex", model=CODEX_MODEL)
        self.fixture.claude_transcript("06")
        self.fixture.codex_rollout("07")

        result = self.cost()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            cost_rows(result.stdout),
            [
                ["TICKET", "EXECUTOR", "MODEL", "INPUT", "OUTPUT",
                 "CACHE-READ", "CACHE-CREATION", "TOTAL"],
                ["06", "claude", MODEL, "24", "46", "6800", "900", "7770"],
                ["07", "codex", CODEX_MODEL, "1000", "700", "4000", "250", "5950"],
                ["TOTAL", "--", "--", "1024", "746", "10800", "1150", "13720"],
            ],
        )

    def test_one_session_cost_event_is_appended_per_launched_child(self):
        self.fixture.worktree("06")
        self.fixture.worktree("07")
        self.fixture.launch("06")
        self.fixture.launch("07", executor="codex", model=CODEX_MODEL)
        self.fixture.claude_transcript("06")
        self.fixture.codex_rollout("07")

        self.cost()

        self.assertEqual([entry["ticket"] for entry in self.costs()], ["06", "07"])

    def test_every_session_that_ran_in_the_worktree_counts_toward_its_child(self):
        self.fixture.worktree("06")
        self.fixture.launch("06")
        self.fixture.claude_transcript("06")
        self.fixture.claude_transcript("06", session="resumed-" + CLAUDE_SESSION)

        self.cost()

        entry = self.costs()[0]
        self.assertEqual(entry["total_tokens"], CLAUDE_TOTALS["total"] * 2)
        self.assertEqual(entry["session"], f"{CLAUDE_SESSION},resumed-{CLAUDE_SESSION}")

    def test_a_child_with_no_transcript_is_diagnosed_rather_than_left_out(self):
        self.fixture.worktree("06")
        self.fixture.launch("06")

        result = self.cost()

        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.costs()[0]
        self.assertEqual(entry["ticket"], "06")
        self.assertEqual(entry["executor"], "claude")
        self.assertIn(str(self.fixture.worktrees["06"]), entry["detail"])
        for field in ("session", "input_tokens", "output_tokens", "total_tokens"):
            self.assertNotIn(field, entry)
        self.assertEqual(
            cost_rows(result.stdout)[1],
            ["06", "claude", MODEL, "--", "--", "--", "--", "--"],
        )
        self.assertIn(entry["detail"], result.stdout)

    def test_a_transcript_that_does_not_parse_is_diagnosed_rather_than_fatal(self):
        self.fixture.worktree("07")
        self.fixture.launch("07", executor="codex", model=CODEX_MODEL)
        self.fixture.codex_rollout("07", text="{ this was never JSON\n")

        result = self.cost()

        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.costs()[0]
        self.assertIn("detail", entry)
        self.assertNotIn("total_tokens", entry)

    def test_a_session_that_reports_no_usage_is_named_in_its_diagnosis(self):
        self.fixture.worktree("07")
        self.fixture.launch("07", executor="codex", model=CODEX_MODEL)
        rollout = self.fixture.codex_rollout("07", text=json.dumps({
            "type": "session_meta",
            "payload": {"id": CODEX_SESSION, "cwd": str(self.fixture.worktrees["07"])},
        }) + "\n")

        result = self.cost()

        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.costs()[0]
        self.assertIn(str(rollout), entry["detail"])
        self.assertNotIn("total_tokens", entry)

    def test_a_transcript_with_a_line_that_does_not_parse_is_diagnosed(self):
        self.fixture.worktree("07")
        self.fixture.launch("07", executor="codex", model=CODEX_MODEL)
        rollout = self.fixture.codex_rollout("07")
        rollout.write_text(rollout.read_text() + "{ half a line\n")

        result = self.cost()

        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.costs()[0]
        self.assertIn(str(rollout), entry["detail"])
        self.assertNotIn("total_tokens", entry)

    def test_two_transcripts_claiming_one_session_are_diagnosed_not_counted(self):
        self.fixture.worktree("07")
        self.fixture.launch("07", executor="codex", model=CODEX_MODEL)
        first = self.fixture.codex_rollout("07")
        second = first.with_name(f"rollout-copy-{CODEX_SESSION}.jsonl")
        second.write_text(first.read_text())

        result = self.cost()

        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.costs()[0]
        self.assertIn(CODEX_SESSION, entry["detail"])
        self.assertNotIn("total_tokens", entry)

    def test_a_transcript_that_names_two_worktrees_is_diagnosed(self):
        self.fixture.worktree("06")
        self.fixture.worktree("07")
        self.fixture.launch("07", executor="codex", model=CODEX_MODEL)
        rollout = self.fixture.codex_rollout("07")
        rollout.write_text(rollout.read_text() + json.dumps({
            "type": "session_meta",
            "payload": {"id": "another", "cwd": str(self.fixture.worktrees["06"])},
        }) + "\n")

        result = self.cost()

        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.costs()[0]
        self.assertIn(str(rollout), entry["detail"])
        self.assertNotIn("total_tokens", entry)

    def test_a_session_from_another_worktree_is_not_this_childs_cost(self):
        self.fixture.worktree("06")
        self.fixture.worktree("07")
        self.fixture.launch("06")
        self.fixture.claude_transcript("07")

        self.cost()

        entry = self.costs()[0]
        self.assertEqual(entry["ticket"], "06")
        self.assertNotIn("total_tokens", entry)

    def test_the_cost_pass_costs_the_coordinator_nothing(self):
        self.fixture.worktree("06")
        self.fixture.launch("06")
        self.fixture.claude_transcript("06")

        self.cost()

        self.assertEqual(self.fixture.calls("claude"), [])
        self.assertEqual(self.fixture.calls("tmux"), [])


if __name__ == "__main__":
    unittest.main()
