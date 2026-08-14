#!/usr/bin/env python3
"""Drive the monitor's operator surface from its command line against fixture receipts.

Every fixture is a real git repository with a worktree per ticket, a machine log written by hand
in the schema `docs/machine-log.md` publishes, and a stub PATH carrying `claude` and `tmux`.
Assertions are on external behaviour only — the frame the dashboard window draws, the toasts tmux
was asked to display, the verdict line, the log lines that follow it, and the exit code.
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

# Stamped in the run's one timestamp format, so every elapsed time below is arithmetic a reader
# can check: 09:00:00 to 09:12:31 is twelve minutes and thirty-one seconds.
LAUNCH_TS = "2026-08-13T09:00:00Z"
NOW_TS = "2026-08-13T09:12:31Z"
LIVE_ELAPSED = "00:12:31"
SETTLED_TS = "2026-08-13T09:41:07Z"
SETTLED_ELAPSED = "00:41:07"

CHILDREN = {"06": "crew-06-dispatch", "07": "crew-07-log", "08": "crew-08-skill"}
MODEL = "claude-opus-4-5-20251101"
CODEX_MODEL = "gpt-5.6-luna"

# The run the dashboard draws: three tickets over two waves, so a frame carries both a launched
# wave and a wave nobody has reached yet. The run id is the run directory's own name.
RUN_ID = "crew-run-1"
TITLES = {"06": "Dispatch launch path", "07": "Path handling", "08": "Skill copy"}
EXECUTORS = {"06": "claude", "07": "codex", "08": "claude"}
MODELS = {"06": MODEL, "07": CODEX_MODEL, "08": MODEL}
WAVES = {1: ("06", "07"), 2: ("08",)}
REVIEW_TS = "2026-08-13T09:10:00Z"
REVIEW_ELAPSED = "00:02:31"
REVIEW_LANE = "codex gpt-5.6-sol"

# The dashboard window's fixed name, and the file in the run dir that remembers its id.
WINDOW_NAME = "crew-dashboard"
WINDOW_RECORD = "dashboard-window"

# What the executor column shows for each lane of this run, and what a row with no clock shows.
CLAUDE_LANE = f"claude/{MODEL}"
CODEX_LANE = f"codex/{CODEX_MODEL}"
NO_ELAPSED = "--"

# The table's alignment, as the spec asks for it: every column as wide as its widest cell, two
# spaces between them. These are this run's widths — `WAVE` and `TICKET` are their own headers,
# the title is "Dispatch launch path", the executor is the Claude lane above — and the two that
# move are passed in: the state column when a frame shows a longer state than `running`, and the
# title column when the window is too narrow to give it its content.
COLUMN_GAP = "  "
WAVE_WIDTH = len("WAVE")
TICKET_WIDTH = len("TICKET")
TITLE_WIDTH = len("Dispatch launch path")
EXECUTOR_WIDTH = len(CLAUDE_LANE)
STATE_WIDTH = len("running")

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

        # The run directory the crew skill lays out: its wave table and its machine log, under a
        # directory whose name is the run's id.
        self.run_dir = self.root / RUN_ID
        self.run_dir.mkdir()
        self.log = self.run_dir / "log.jsonl"
        self.table_path = self.run_dir / "wave-table.json"
        self.toast_state = self.run_dir / "toasts.json"

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
        self.columns = 100

    def _link_stub(self, name, script):
        target = self.bin_dir / name
        target.write_text(
            "#!/bin/sh\nexec %s %s \"$@\"\n" % (sys.executable, TESTS_DIR / script)
        )
        target.chmod(0o755)

    def table(self, waves=WAVES):
        """The approved wave table: every ticket of every wave, in the schema dispatch reads."""
        self.table_path.write_text(json.dumps({
            "run": {
                "repo_root": str(self.repo),
                "integration_branch": "crew/feature",
                "integration_base_commit": self.base_commit,
            },
            "waves": [
                {
                    "wave": wave,
                    "tickets": [
                        {
                            "id": ticket,
                            "title": TITLES[ticket],
                            "path": str(self.root / f"{ticket}.md"),
                            "workflow": "tdd",
                            "executor": EXECUTORS[ticket],
                            "model": MODELS[ticket],
                            "effort": "medium",
                        }
                        for ticket in tickets
                    ],
                }
                for wave, tickets in waves.items()
            ],
        }))

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

    def launch(self, ticket, ts=LAUNCH_TS, executor="claude", model=MODEL, worktree=None):
        self.append(
            ts, "launch", ticket=ticket, child=CHILDREN[ticket], workflow="tdd",
            executor=executor, model=model, effort="medium",
            branch=f"worktree-{ticket}",
            worktree=str(worktree if worktree is not None else self.worktrees[ticket]),
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

    def codex_state(self, ticket, status="busy", cwd=None):
        """The bridge state file a Codex child's launch writes: where it runs, and how it is."""
        path = self.run_dir / "codex" / f"{ticket}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "version": 1,
            "name": CHILDREN[ticket],
            "cwd": str(cwd if cwd is not None else self.worktrees[ticket]),
            "model": CODEX_MODEL,
            "status": status,
        }))
        return path

    def live(self, statuses):
        """What each lane's own source says about its children: the agents list for a Claude
        child, its bridge state file for a Codex one. A ticket left out has no live entry at all.
        """
        self.agents({
            ticket: status for ticket, status in statuses.items()
            if EXECUTORS[ticket] == "claude"
        })
        for ticket, status in statuses.items():
            if EXECUTORS[ticket] == "codex":
                self.codex_state(ticket, status)

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
        # The window width the title column absorbs, fixed here so a frame is the same frame
        # whatever terminal the suite runs in.
        environment["COLUMNS"] = str(self.columns)
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

    def dashboard(self, *extra):
        """Draw the whole run once, at the fixed moment the elapsed times are measured to."""
        return self.run_monitor(
            "dashboard", "--run-dir", self.run_dir, "--now", NOW_TS, *extra
        )

    def window(self, *extra):
        """Ask for the run's dashboard window, as a script re-running the command would."""
        return self.run_monitor("window", "--run-dir", self.run_dir, "--session", "$7:", *extra)

    def live_windows(self):
        path = self.stub_dir / "tmux-windows.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def close_window(self, window_id):
        """What the operator's own `tmux kill-window` leaves behind: the id no longer exists."""
        table = self.live_windows()
        del table[window_id]
        (self.stub_dir / "tmux-windows.json").write_text(json.dumps(table))

    def recorded_window(self):
        path = self.run_dir / WINDOW_RECORD
        return path.read_text().strip() if path.exists() else None

    def window_calls(self):
        return [argv for argv in self.calls("tmux") if argv[0] == "new-window"]

    def calls(self, name):
        path = self.stub_dir / f"{name}-calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line)["argv"] for line in path.read_text().splitlines() if line]

    def toasts(self):
        return [argv[-1] for argv in self.calls("tmux") if argv[:1] == ["display-message"]]

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


def frame(output):
    """The one frame a single draw wrote, without the trailing newline."""
    return output.rstrip("\n")


def row(wave, ticket, text, executor, state, elapsed, width=STATE_WIDTH, title=TITLE_WIDTH):
    """One line of the table, aligned as the frame aligns it."""
    return COLUMN_GAP.join([
        wave.ljust(WAVE_WIDTH),
        ticket.ljust(TICKET_WIDTH),
        text.ljust(title),
        executor.ljust(EXECUTOR_WIDTH),
        state.ljust(width),
        elapsed,
    ]).rstrip()


def header(width=STATE_WIDTH, title=TITLE_WIDTH):
    return row("WAVE", "TICKET", "TITLE", "EXECUTOR", "STATE", "ELAPSED", width, title)


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
    """The whole run in one frame: every ticket of every wave, whatever has happened to it."""

    def launch_wave_one(self):
        self.fixture.table()
        self.fixture.worktree("06")
        self.fixture.worktree("07")
        self.fixture.launch("06")
        self.fixture.launch("07", executor="codex", model=CODEX_MODEL)

    def test_the_frame_carries_a_summary_line_a_row_per_ticket_and_pending_for_the_unlaunched(self):
        self.launch_wave_one()
        self.fixture.live({"06": "busy", "07": "busy"})

        result = self.fixture.dashboard()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(frame(result.stdout), "\n".join([
            f"crew {RUN_ID} — wave 1/2 · pending=1 running=2 · elapsed {LIVE_ELAPSED}",
            header(),
            row("1", "06", TITLES["06"], CLAUDE_LANE, "running", LIVE_ELAPSED),
            row("1", "07", TITLES["07"], CODEX_LANE, "running", LIVE_ELAPSED),
            row("2", "08", TITLES["08"], CLAUDE_LANE, "pending", NO_ELAPSED),
        ]))

    def test_an_abnormal_row_explains_its_last_event_and_a_normal_row_stays_quiet(self):
        self.launch_wave_one()
        self.fixture.append(NOW_TS, "escalation", ticket="07", role="child",
                            message="CREW ASK 07 stuck — ts=1755060042")
        self.fixture.live({"06": "busy", "07": "idle"})

        result = self.fixture.dashboard()

        self.assertEqual(frame(result.stdout), "\n".join([
            f"crew {RUN_ID} — wave 1/2 · pending=1 running=1 waiting=1 · elapsed {LIVE_ELAPSED}",
            header(),
            row("1", "06", TITLES["06"], CLAUDE_LANE, "running", LIVE_ELAPSED),
            row("1", "07", TITLES["07"], CODEX_LANE, "waiting", LIVE_ELAPSED),
            f"  ↳ last event: escalation · {NOW_TS}",
            row("2", "08", TITLES["08"], CLAUDE_LANE, "pending", NO_ELAPSED),
        ]))

    def test_a_ticket_under_review_carries_the_review_lane_state_and_elapsed_beneath_it(self):
        self.launch_wave_one()
        self.fixture.append(REVIEW_TS, "review", ticket="06", lane=REVIEW_LANE, state="running")
        self.fixture.live({"06": "busy", "07": "busy"})

        result = self.fixture.dashboard()

        self.assertIn(
            f"\n  ↳ review: {REVIEW_LANE} running · {REVIEW_ELAPSED}\n", result.stdout
        )
        self.assertEqual(
            frame(result.stdout).splitlines()[2],
            row("1", "06", TITLES["06"], CLAUDE_LANE, "running", LIVE_ELAPSED),
        )

    def test_a_review_that_has_returned_no_longer_annotates_its_row(self):
        self.launch_wave_one()
        self.fixture.append(REVIEW_TS, "review", ticket="06", lane=REVIEW_LANE, state="running")
        self.fixture.append(NOW_TS, "review", ticket="06", lane=REVIEW_LANE, state="returned")
        self.fixture.live({"06": "busy", "07": "busy"})

        result = self.fixture.dashboard()

        self.assertNotIn("↳ review:", result.stdout)

    def test_a_settled_ticket_shows_its_state_and_stops_its_clock(self):
        self.launch_wave_one()
        self.fixture.append(SETTLED_TS, "receipt", ticket="06", verdict="landable",
                            sha=self.fixture.head("06"))
        self.fixture.append(SETTLED_TS, "merge", ticket="06", result="clean",
                            branch="worktree-06", into="crew/feature")
        self.fixture.append(SETTLED_TS, "receipt", ticket="07", verdict="failed",
                            detail="the child never finished")
        self.fixture.live({})

        result = self.fixture.dashboard()

        self.assertEqual(frame(result.stdout), "\n".join([
            f"crew {RUN_ID} — wave 1/2 · pending=1 merged=1 failed=1 · elapsed {LIVE_ELAPSED}",
            header(),
            row("1", "06", TITLES["06"], CLAUDE_LANE, "merged", SETTLED_ELAPSED),
            row("1", "07", TITLES["07"], CODEX_LANE, "failed", SETTLED_ELAPSED),
            f"  ↳ last event: receipt failed — the child never finished · {SETTLED_TS}",
            row("2", "08", TITLES["08"], CLAUDE_LANE, "pending", NO_ELAPSED),
        ]))

    def test_a_settled_ticket_stops_following_its_lanes_live_source(self):
        self.launch_wave_one()
        self.fixture.append(SETTLED_TS, "receipt", ticket="07", verdict="landable",
                            sha=self.fixture.head("07"))
        self.fixture.live({"06": "busy", "07": "busy"})

        result = self.fixture.dashboard()

        self.assertIn(
            row("1", "07", TITLES["07"], CODEX_LANE, "landable", SETTLED_ELAPSED, width=8),
            result.stdout,
        )

    def test_a_launched_claude_child_missing_from_the_agents_list_is_vanished(self):
        self.launch_wave_one()
        self.fixture.live({"07": "busy"})

        result = self.fixture.dashboard()

        self.assertIn(
            row("1", "06", TITLES["06"], CLAUDE_LANE, "vanished", LIVE_ELAPSED, width=8),
            result.stdout,
        )
        self.assertIn(f"  ↳ last event: launch · {LAUNCH_TS}\n", result.stdout)

    def test_a_launched_codex_child_with_no_bridge_state_is_vanished(self):
        self.launch_wave_one()
        self.fixture.live({"06": "busy"})

        result = self.fixture.dashboard()

        self.assertIn(
            row("1", "07", TITLES["07"], CODEX_LANE, "vanished", LIVE_ELAPSED, width=8),
            result.stdout,
        )

    def test_a_codex_child_the_bridge_calls_stopped_is_vanished(self):
        self.launch_wave_one()
        self.fixture.live({"06": "busy", "07": "stopped"})

        result = self.fixture.dashboard()

        self.assertIn(
            row("1", "07", TITLES["07"], CODEX_LANE, "vanished", LIVE_ELAPSED, width=8),
            result.stdout,
        )

    def test_an_idle_child_is_waiting_and_says_so(self):
        self.launch_wave_one()
        self.fixture.live({"06": "idle", "07": "busy"})

        result = self.fixture.dashboard()

        self.assertIn(
            row("1", "06", TITLES["06"], CLAUDE_LANE, "waiting", LIVE_ELAPSED),
            result.stdout,
        )

    def test_a_worktree_with_two_sessions_keeps_its_row_and_carries_the_anomaly(self):
        # Wide enough that the temporary worktree path in the annotation is not cut to fit.
        self.fixture.columns = 200
        self.launch_wave_one()
        self.fixture.agents_at([
            ("06", self.fixture.worktrees["06"], "busy"),
            ("06", self.fixture.worktrees["06"], "busy"),
        ])
        self.fixture.codex_state("07")

        result = self.fixture.dashboard()

        self.assertIn(
            row("1", "06", TITLES["06"], CLAUDE_LANE, "running", LIVE_ELAPSED),
            result.stdout,
        )
        self.assertIn(
            f"  ↳ anomaly: duplicate · more than one session in {self.fixture.worktrees['06']}\n",
            result.stdout,
        )

    def test_an_unreadable_agents_list_is_an_anomaly_and_the_frame_still_draws(self):
        self.launch_wave_one()
        self.fixture.codex_state("07")

        result = self.fixture.dashboard()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            row("1", "06", TITLES["06"], CLAUDE_LANE, "running", LIVE_ELAPSED),
            result.stdout,
        )
        self.assertIn("  ↳ anomaly: unknown · the agents list could not be read\n", result.stdout)
        # The other lane is read from its own source, so it is unaffected.
        self.assertIn(
            row("1", "07", TITLES["07"], CODEX_LANE, "running", LIVE_ELAPSED),
            result.stdout,
        )

    def test_the_title_column_absorbs_the_window_width_and_is_cut_when_it_will_not_fit(self):
        # Ten columns is what this run's other five leave of a 76-column window, so the two long
        # titles are cut to it and the short one is not.
        self.fixture.columns = 76
        self.launch_wave_one()
        self.fixture.live({"06": "busy", "07": "busy"})

        result = self.fixture.dashboard()

        for line in frame(result.stdout).splitlines():
            self.assertLessEqual(len(line), 76, line)
        self.assertIn(
            row("1", "06", "Dispatch…", CLAUDE_LANE, "running", LIVE_ELAPSED, title=10),
            result.stdout,
        )
        self.assertIn(
            row("1", "07", "Path hand…", CODEX_LANE, "running", LIVE_ELAPSED, title=10),
            result.stdout,
        )
        self.assertIn(
            row("2", "08", TITLES["08"], CLAUDE_LANE, "pending", NO_ELAPSED, title=10),
            result.stdout,
        )

    def test_the_frame_is_plain_text_when_nothing_is_watching_a_terminal(self):
        self.launch_wave_one()
        self.fixture.live({"06": "waiting", "07": "busy"})

        result = self.fixture.dashboard()

        self.assertNotIn("\x1b[", result.stdout)

    def test_the_frame_redraws_as_the_log_changes(self):
        self.launch_wave_one()
        self.fixture.live({"06": "busy", "07": "busy"})
        process = self.fixture.start_monitor(
            "dashboard", "--run-dir", self.fixture.run_dir, "--now", NOW_TS, "--refresh", "0.05",
        )
        self.addCleanup(process.kill)

        time.sleep(0.5)
        self.fixture.append(SETTLED_TS, "receipt", ticket="06", verdict="landable",
                            sha=self.fixture.head("06"))
        time.sleep(0.5)
        process.terminate()
        output = process.communicate(timeout=10)[0]

        self.assertIn("running", output)
        self.assertIn("landable", output)
        self.assertGreater(output.count(f"crew {RUN_ID} —"), 1)

    def test_a_finished_run_stops_refreshing_and_keeps_its_last_frame(self):
        self.launch_wave_one()
        self.fixture.append(SETTLED_TS, "receipt", ticket="06", verdict="landable",
                            sha=self.fixture.head("06"))
        self.fixture.append(SETTLED_TS, "receipt", ticket="07", verdict="failed")
        self.fixture.append(SETTLED_TS, "advance", wave=2, decision="escalated")
        self.fixture.live({})
        process = self.fixture.start_monitor(
            "dashboard", "--run-dir", self.fixture.run_dir, "--now", NOW_TS, "--refresh", "0.05",
        )
        self.addCleanup(process.kill)

        time.sleep(0.5)
        self.assertIsNone(process.poll(), "the renderer left the window without a frame in it")
        process.terminate()
        output = process.communicate(timeout=10)[0]

        self.assertEqual(output.count(f"crew {RUN_ID} —"), 1)
        self.assertIn("landable", output)

    def test_a_run_dir_with_no_wave_table_is_a_monitor_error_rather_than_an_empty_frame(self):
        result = self.fixture.dashboard()

        self.assertEqual(result.returncode, 3, result.stdout)
        self.assertIn("MONITOR ERROR", result.stderr)


class ToastTests(MonitorTestCase):
    def launch_wave_one(self):
        self.fixture.table()
        self.fixture.worktree("06")
        self.fixture.worktree("07")
        self.fixture.launch("06")
        self.fixture.launch("07", executor="codex", model=CODEX_MODEL)

    def test_a_stuck_child_a_vanished_child_and_an_escalation_each_toast(self):
        self.launch_wave_one()
        self.fixture.append(NOW_TS, "escalation", ticket="06", role="child",
                            message="CREW ASK 06 stuck — ts=1755060042")
        self.fixture.live({"06": "waiting"})

        self.fixture.dashboard()

        self.assertEqual(
            sorted(self.fixture.toasts()),
            sorted([
                "crew 06 stuck at a permission prompt",
                "crew 06 escalated",
                "crew 07 vanished",
            ]),
        )

    def test_a_child_that_stopped_without_finishing_is_not_toasted_as_a_permission_prompt(self):
        self.launch_wave_one()
        self.fixture.live({"06": "idle", "07": "idle"})

        self.fixture.dashboard()

        self.assertEqual(
            sorted(self.fixture.toasts()),
            sorted([
                "crew 06 stopped without finishing",
                "crew 07 stopped without finishing",
            ]),
        )

    def test_a_wave_whose_every_ticket_is_settled_toasts_once(self):
        self.launch_wave_one()
        self.fixture.append(SETTLED_TS, "receipt", ticket="06", verdict="landable",
                            sha=self.fixture.head("06"))
        self.fixture.append(SETTLED_TS, "receipt", ticket="07", verdict="failed")
        self.fixture.live({})

        self.fixture.dashboard()
        self.fixture.dashboard()

        self.assertEqual(self.fixture.toasts(), ["crew wave 1 complete"])

    def test_an_unfinished_wave_does_not_toast_complete(self):
        self.launch_wave_one()
        self.fixture.append(SETTLED_TS, "receipt", ticket="06", verdict="landable",
                            sha=self.fixture.head("06"))
        self.fixture.live({"07": "busy"})

        self.fixture.dashboard()

        self.assertEqual(self.fixture.toasts(), [])

    def test_a_wave_nobody_has_launched_does_not_toast_complete(self):
        self.fixture.table()
        self.fixture.live({})

        self.fixture.dashboard()

        self.assertEqual(self.fixture.toasts(), [])

    def test_an_exception_is_not_toasted_twice(self):
        self.launch_wave_one()
        self.fixture.live({"06": "waiting", "07": "busy"})

        self.fixture.dashboard()
        self.fixture.dashboard()

        self.assertEqual(self.fixture.toasts(), ["crew 06 stuck at a permission prompt"])

    def test_nothing_the_monitor_emits_reaches_the_coordinator(self):
        self.launch_wave_one()
        self.fixture.live({"06": "waiting", "07": "busy"})

        self.fixture.dashboard()

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

    def test_a_session_listed_under_an_aliased_cwd_is_not_vanished(self):
        self.fixture.table(waves={1: ("06",)})
        self.fixture.worktree("06")
        self.fixture.launch("06")
        self.fixture.agents_at([("06", self.fixture.alias("06"), "busy")])

        result = self.fixture.dashboard()

        self.assertIn("running", result.stdout)
        self.assertNotIn("vanished", result.stdout)
        self.assertEqual(self.fixture.toasts(), [])

    def test_a_codex_bridge_state_under_an_aliased_cwd_is_not_vanished(self):
        self.fixture.table(waves={1: ("07",)})
        self.fixture.worktree("07")
        self.fixture.launch("07", executor="codex", model=CODEX_MODEL)
        self.fixture.codex_state("07", cwd=self.fixture.alias("07"))

        result = self.fixture.dashboard()

        self.assertIn("running", result.stdout)
        self.assertNotIn("vanished", result.stdout)
        self.assertEqual(self.fixture.toasts(), [])

    def test_a_launch_recorded_under_an_aliased_spelling_finds_its_session(self):
        self.fixture.table(waves={1: ("06",)})
        self.fixture.worktree("06")
        self.fixture.launch("06", worktree=self.fixture.alias("06"))
        self.fixture.agents({"06": "busy"})

        result = self.fixture.dashboard()

        self.assertIn("running", result.stdout)
        self.assertNotIn("vanished", result.stdout)

    def test_two_spellings_of_one_worktree_are_one_duplicated_session(self):
        self.fixture.table(waves={1: ("06",)})
        self.fixture.worktree("06")
        self.fixture.launch("06")
        self.fixture.agents_at([
            ("06", self.fixture.worktrees["06"], "busy"),
            ("06", self.fixture.alias("06"), "busy"),
        ])

        result = self.fixture.dashboard()

        self.assertIn("↳ anomaly: duplicate", result.stdout)


class WindowTests(MonitorTestCase):
    """The run's one dashboard window: created once, reused, recreated, never closed."""

    def setUp(self):
        super().setUp()
        self.fixture.table()

    def test_the_window_is_created_detached_under_the_runs_fixed_name_and_recorded(self):
        result = self.fixture.window()

        self.assertEqual(result.returncode, 0, result.stderr)
        window_id = result.stdout.strip()
        self.assertEqual(self.fixture.recorded_window(), window_id)
        self.assertEqual(list(self.fixture.live_windows()), [window_id])
        created = self.fixture.window_calls()
        self.assertEqual(len(created), 1, self.fixture.calls("tmux"))
        self.assertIn("-d", created[0])
        self.assertIn(WINDOW_NAME, created[0])
        self.assertIn("$7:", created[0])
        command = created[0][-1]
        self.assertIn("dashboard", command)
        self.assertIn("--refresh", command)
        self.assertIn(str(self.fixture.run_dir), command)

    def test_calling_it_again_reuses_the_recorded_window(self):
        first = self.fixture.window()

        second = self.fixture.window()

        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(second.stdout.strip(), first.stdout.strip())
        self.assertEqual(len(self.fixture.window_calls()), 1)
        self.assertEqual(len(self.fixture.live_windows()), 1)

    def test_overlapping_calls_still_produce_one_window(self):
        processes = [
            self.fixture.start_monitor(
                "window", "--run-dir", self.fixture.run_dir, "--session", "$7:"
            )
            for _ in range(4)
        ]
        printed = {process.communicate(timeout=30)[0].strip() for process in processes}

        self.assertEqual(len(self.fixture.window_calls()), 1, self.fixture.calls("tmux"))
        self.assertEqual(len(self.fixture.live_windows()), 1)
        self.assertEqual(printed, {self.fixture.recorded_window()})

    def test_a_window_that_vanished_is_recreated_and_re_recorded(self):
        first = self.fixture.window().stdout.strip()
        self.fixture.close_window(first)

        second = self.fixture.window()

        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertNotEqual(second.stdout.strip(), first)
        self.assertEqual(self.fixture.recorded_window(), second.stdout.strip())
        self.assertEqual(list(self.fixture.live_windows()), [second.stdout.strip()])

    def test_the_tool_never_closes_the_window(self):
        window_id = self.fixture.window().stdout.strip()
        self.fixture.window()

        self.assertEqual(
            [argv[0] for argv in self.fixture.calls("tmux") if argv[0].startswith("kill")], []
        )
        self.assertEqual(list(self.fixture.live_windows()), [window_id])


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
