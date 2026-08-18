#!/usr/bin/env python3
"""Drive the monitor's operator surface from its command line against fixture receipts.

Every fixture is a real git repository with a worktree per ticket, a machine log written by hand
in the schema `docs/machine-log.md` publishes, and a stub PATH carrying `claude` and `tmux`.
Assertions are on external behaviour only — the frame the dashboard window draws, the toasts tmux
was asked to display, the verdict line, the log lines that follow it, and the exit code.
"""

import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


TESTS_DIR = pathlib.Path(__file__).resolve().parent
MONITOR = TESTS_DIR.parent / "monitor.py"
# The review lane the dashboard's annotation is drawn from, written by this bridge and no fixture.
REVIEW_BRIDGE = TESTS_DIR.parents[1] / "review" / "scripts" / "claude_review_bridge.py"

# Stamped in the run's one timestamp format, so every elapsed time below is arithmetic a reader
# can check: 09:00:00 to 09:12:31 is twelve minutes and thirty-one seconds.
LAUNCH_TS = "2026-08-13T09:00:00Z"
NOW_TS = "2026-08-13T09:12:31Z"
LIVE_ELAPSED = "00:12:31"
SETTLED_TS = "2026-08-13T09:41:07Z"
SETTLED_ELAPSED = "00:41:07"
# The moment a merge blew up on a ticket that already had its receipt, and the moment the wave
# re-ran once the coordinator had ruled: both later than the receipt, so a row that keeps
# following its stale receipt is visible in its clock alone.
BLOCKED_TS = "2026-08-13T09:44:19Z"
BLOCKED_ELAPSED = "00:44:19"
RULED_TS = "2026-08-13T09:47:53Z"
RULED_ELAPSED = "00:47:53"
# Why one merge stopped, as the merge driver words it, and what the row says about it underneath.
BLOCKED_DETAIL = "semantic: both sides rewrote the same lines of driver.py"
# The marker the summary line carries while a wave is halted on the coordinator's ruling.
AWAITING_RULING = "⚠ awaiting your ruling"
# The rework instruction the driver sends a child whose merge escalated on a semantic conflict,
# opening with the marker the driver's merge rung is read back out of the log by. A child holding
# this is working, not stuck, which is what the `reworking` state says.
REWORK_INSTRUCTION = (
    "CREW MERGE 06 — your branch worktree-06 conflicts with crew/feature in a way no script can"
    f" resolve: {BLOCKED_DETAIL}. Merge crew/feature into worktree-06, resolve the conflict,"
    " re-run the tests the conflict touched, re-review scoped to the conflict-resolution diff,"
    " commit, and send a new CREW COMPLETE <sha>."
)
# The two widths a frame drawn with the new vocabulary aligns its state column to.
REWORKING_WIDTH = len("reworking")
SETTLING_WIDTH = len("settling")
# A window wide enough to hold that summary line and the blocked row's annotation whole, so a
# halted frame is read rather than measured.
HALTED_COLUMNS = 120

# The refresh the tests that watch the dashboard's loop run it at, and how they wait on it. One
# frame of this fixture costs a quarter of a second or more — every draw spawns the stub `claude`
# and the stub `tmux` — so a fixed nap is no way to count frames: half a second is never long
# enough for a second frame, and on a loaded machine not long enough for the first. These tests
# wait for the frame they are waiting for instead; the deadline is only how long they wait before
# calling the loop broken, and the poll is how often they look.
LOOP_REFRESH_SECONDS = 0.05
FRAME_DEADLINE_SECONDS = 30
FRAME_POLL_SECONDS = 0.05
# How long a frame that must be the run's last is watched for the redraw that would prove it is
# not. At the refresh above a live loop draws several frames a second, so this window is many
# redraws wide — long enough that its silence means the loop really has stopped.
HELD_FRAME_SECONDS = 2.0

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
# The coordinator's own pid, passed in by the coordinator: nothing here ever looks one up.
COORDINATOR_PID = 4242
# The file the dashboard and the pin both dedup toasts through, in the run directory.
TOAST_STATE = "toasts.json"

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
# A review session that ran in a child's own worktree, on the same vendor as the child itself.
REVIEW_SESSION = "9d1f4c2a-0000-4000-8000-000000000003"
# The session driving the run, which works in the repository rather than in any child's worktree.
COORDINATOR_SESSION = "9d1f4c2a-0000-4000-8000-000000000002"
CODEX_SESSION = "019ffe0e-e154-7a93-88c2-3be07fd543cd"

# The guard assets the dispatch renderer installs into every Claude worktree before its child
# starts; the child never commits them, so they are not what makes a tree dirty.
GUARD_ASSETS = ("red-line.sh", "worktree-guard.sh", "settings.local.json")

# The pin registry the statusline discovers the live run through: a directory of pin files under
# the operator's Claude config, which this fixture points at its own root.
PIN_REGISTRY = ("agentcrew", "pins")
# The tmux session the caller of `pin` is sitting in, and one that belongs to another crew.
CALLER_SESSION = "$7"
OTHER_SESSION = "$9"
# A third session, running no crew of its own: the tab a pin must never reach.
BYSTANDER_SESSION = "$3"
# That same session as tmux addresses it as a target, which is the spelling dispatch passes to
# `window` and the pin therefore records.
SESSION_TARGET = f"{CALLER_SESSION}:"
# A second run of the same shape, so a registry can hold two pins at once.
SECOND_RUN_ID = "crew-run-2"

# What each lane's row says when its own source did not answer in the time it was given.
AGENTS_UNREADABLE = "  ↳ anomaly: unknown · the agents list could not be read"
CODEX_UNREADABLE = "  ↳ anomaly: unknown · the codex bridge state could not be read"

ANSI = re.compile(r"\x1b\[[0-9;]*m")
# The colour the state column is painted in for a running row, and the reset that ends it.
RUNNING_COLOUR = "\x1b[36m"
COLOUR_RESET = "\x1b[0m"

# The statusline the operator already runs, and the one line it prints — the context, cost and
# rate-limit readout the install has to keep, recognisable in the wrapper's output.
PREVIOUS_STATUSLINE = "opus | main | 42% context"
# The tick rate the installer sets when the operator has none, in seconds. Only a fresh install
# ever sees it: a value already there is left alone, however large.
PIN_REFRESH_INTERVAL = 2
# A refresh interval of the operator's own, larger than the one above, so "left alone" and "never
# lowered" are the same assertion.
OPERATOR_REFRESH_INTERVAL = 30
# A settings key that is none of the installer's business, so an edit that loses it is visible.
UNRELATED_SETTING = ("model", "opus")


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

        # The operator's own Claude Code wiring the installer edits: the settings file, the
        # statusline script it already points at, and the wrapper the installer writes. All three
        # are under the fixture root, so no test can reach the machine it runs on.
        self.settings_dir = self.root / "claude-settings"
        self.settings_dir.mkdir()
        self.settings_path = self.settings_dir / "settings.json"
        self.wrapper_path = self.settings_dir / "agentcrew-statusline.sh"
        self.statusline_path = self.settings_dir / "statusline.sh"

        self.stub_dir = self.root / "stub"
        self.stub_dir.mkdir()
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self._link_stub("claude", "stub_claude.py")
        self._link_stub("tmux", "stub_tmux.py")

        # Where a refreshing dashboard's frames and complaints land. Files rather than pipes: the
        # way a test collects a pipe is `communicate()`, which returns at EOF and so not until the
        # loop exits, leaving a test watching it redraw nothing to do but nap and then read. A
        # file is readable while the loop that writes it is still running, so a test can wait for
        # the frame it is waiting for.
        self.frames_path = self.root / "dashboard-frames.txt"
        self.frames_errors = self.root / "dashboard-errors.txt"

        self.worktrees = {}
        self.columns = 100
        self.lines = 24
        # The tmux session the monitor is called from, as tmux itself exports it; None is a
        # caller whose environment cannot answer the question.
        self.tmux_session = CALLER_SESSION
        # Anything else this fixture's environment carries, for the variables one test moves.
        self.extra_environment = {}

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

    def claude_transcript(
        self, ticket, session=CLAUDE_SESSION, turns=CLAUDE_TURNS, model=MODEL, cwd=None
    ):
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
                "cwd": str(cwd if cwd is not None else self.worktrees[ticket]),
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

    def coordinator_transcript(self, session=COORDINATOR_SESSION, turns=CLAUDE_TURNS):
        """The transcript of the session driving the run, written in the repository itself."""
        return self.claude_transcript(None, session=session, turns=turns, cwd=self.repo)

    def codex_rollout(
        self, ticket, session=CODEX_SESSION, usage=CODEX_USAGE, text=None, cwd=None
    ):
        """A Codex rollout for that worktree: its session meta, then its last token count."""
        path = self.codex_home / "sessions" / "2026" / "08" / "13" / f"rollout-{session}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        if text is None:
            text = "\n".join([
                json.dumps({
                    "type": "session_meta",
                    "payload": {
                        "id": session,
                        "cwd": str(cwd if cwd is not None else self.worktrees[ticket]),
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

    def review(self, ticket, model=MODEL):
        """Run the real review bridge over this ticket, pointed at this run's machine log.

        Returns the finished call and the log as the bridge left it *mid-review* — the stub
        reviewer copies the file aside while it is running — so a frame can be drawn from a
        review that is genuinely in flight rather than from a `review` line composed by hand.
        """
        snapshot = self.run_dir / "log-mid-review.jsonl"
        environment = self.environment()
        environment["AGENTCREW_STUB_REVIEW_LOG"] = str(self.log)
        environment["AGENTCREW_STUB_REVIEW_SNAPSHOT"] = str(snapshot)
        completed = subprocess.run(
            [
                sys.executable, str(REVIEW_BRIDGE),
                "--cwd", str(self.worktrees[ticket]),
                "--state-dir", str(self.run_dir / "review-state"),
                "--claude-binary", str(TESTS_DIR / "stub_review_claude.py"),
                "--machine-log", str(self.log),
                "--ticket", ticket,
                "--model", model,
                "the changes in this worktree",
            ],
            capture_output=True, text=True, env=environment,
        )
        return completed, snapshot

    @staticmethod
    def set_claude_cwds(transcript, cwds):
        """Set each assistant record's cwd without making tests know its JSON layout."""
        records = [json.loads(line) for line in transcript.read_text().splitlines()]
        if len(records) != len(cwds):
            raise AssertionError("one cwd is required for each Claude record")
        for record, cwd in zip(records, cwds):
            record["cwd"] = str(cwd)
        transcript.write_text("\n".join(json.dumps(record) for record in records) + "\n")

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

    def aliased_run_dir(self):
        """The run directory addressed through a symlink — the `/tmp` vs `/private/tmp` shape."""
        link = self.root / "run-alias"
        if not link.exists():
            link.symlink_to(self.run_dir, target_is_directory=True)
        return link

    def second_run(self):
        """A second run directory of the same shape, so a registry can carry two live pins."""
        path = self.root / SECOND_RUN_ID
        if not path.exists():
            shutil.copytree(self.run_dir, path)
        return path

    def pin_dir(self):
        """The pin registry's default location, under the Claude config this fixture points at."""
        return self.claude_home.joinpath(*PIN_REGISTRY)

    def pin(self, run_dir=None, pid=None, session=CALLER_SESSION, directory=None,
            renderer=MONITOR, interpreter=sys.executable):
        """A pin naming a live run: its run directory, the coordinator's pid, its tmux session,
        and the renderer and interpreter that draw it.

        `None` for either of the last two leaves that key out, which is how a release older than
        those fields wrote its pins.
        """
        run_dir = self.run_dir if run_dir is None else run_dir
        directory = self.pin_dir() if directory is None else pathlib.Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        # The run names its pin file after the run directory it points at, as dispatch does.
        name = hashlib.sha256(os.path.realpath(str(run_dir)).encode()).hexdigest()[:16]
        path = directory / f"{name}.json"
        pin = {
            "run_dir": str(run_dir),
            "coordinator_pid": os.getpid() if pid is None else pid,
            "tmux_session": session,
        }
        if renderer is not None:
            pin["renderer"] = str(renderer)
        if interpreter is not None:
            pin["interpreter"] = str(interpreter)
        path.write_text(json.dumps(pin))
        return path

    def release_copy(self, name):
        """A second copy of this release's monitor under `name` — the upgrade simulation's X.

        An install run from it records nothing about where it is, so the copy can be taken away
        afterwards and the pin a later release writes still draws.
        """
        directory = self.root / name
        directory.mkdir(parents=True, exist_ok=True)
        copy = directory / MONITOR.name
        shutil.copy2(str(MONITOR), str(copy))
        return copy

    def dead_pid(self):
        """A pid that has certainly gone: a process this fixture started and then reaped."""
        process = subprocess.Popen([sys.executable, "-c", ""])
        process.wait()
        return process.pid

    def tmux_says_session(self, session):
        """The session the stub tmux answers with when it is asked which one this is."""
        (self.stub_dir / "tmux-session").write_text(session)

    def slow_agents(self, seconds):
        """Make the stub Claude CLI take `seconds` to answer, so a read can be timed out."""
        (self.stub_dir / "agents-delay").write_text(str(seconds))

    def slow_toasts(self, seconds):
        """Make the stub tmux take `seconds` to display a toast, so the tick can time it out."""
        (self.stub_dir / "display-delay").write_text(str(seconds))

    def environment(self):
        environment = dict(os.environ)
        environment["PATH"] = f"{self.bin_dir}{os.pathsep}{environment['PATH']}"
        # The window width the title column absorbs, fixed here so a frame is the same frame
        # whatever terminal the suite runs in.
        environment["COLUMNS"] = str(self.columns)
        environment["LINES"] = str(self.lines)
        environment["AGENTCREW_STUB_DIR"] = str(self.stub_dir)
        # An operator who turned colour off everywhere is not what these frames are drawn under:
        # the one case that asks for it puts it back.
        environment.pop("NO_COLOR", None)
        environment["CLAUDE_CONFIG_DIR"] = str(self.claude_home)
        environment["CODEX_HOME"] = str(self.codex_home)
        # tmux exports its socket, its client pid and the session id into every session it runs,
        # which is where the pin reads the caller's own session from. Set here rather than
        # inherited, so a suite run inside tmux draws the fixture's run and not the machine's.
        if self.tmux_session:
            environment["TMUX"] = f"/tmp/tmux-1000/default,1234,{self.tmux_session.lstrip('$')}"
        else:
            environment.pop("TMUX", None)
        environment.update(self.extra_environment)
        return environment

    def run_monitor(self, *args):
        return self.run_release(MONITOR, *args)

    def run_release(self, monitor, *args):
        """The same call against one particular copy of the release, so an install made by the
        copy and a run dispatched by another are two different releases."""
        return subprocess.run(
            [sys.executable, str(monitor), *[str(argument) for argument in args]],
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

    def pin_frame(self, *extra):
        """One statusline tick, at the fixed moment the elapsed times are measured to."""
        return self.run_monitor("pin", "--now", NOW_TS, *extra)

    def refreshing_dashboard(self, *extra):
        """Start the dashboard in its refresh loop; returns the `Popen` still drawing.

        Its frames go to `frames_path` and anything it complains about to `frames_errors`, both
        readable while it runs. Nothing here waits for it or ends it: the caller owns both.
        """
        with self.frames_path.open("wb") as frames, self.frames_errors.open("wb") as errors:
            return subprocess.Popen(
                [sys.executable, str(MONITOR), "dashboard",
                 "--run-dir", str(self.run_dir), "--now", NOW_TS,
                 "--refresh", str(LOOP_REFRESH_SECONDS),
                 *[str(argument) for argument in extra]],
                stdout=frames, stderr=errors, env=self.environment(),
            )

    def pin_frame_over(self, stdin, *extra):
        """One tick with Claude Code's own JSON on its stdin, as the statusline runs it."""
        with pathlib.Path(stdin).open() as handle:
            return subprocess.run(
                [sys.executable, str(MONITOR), "pin", "--now", NOW_TS,
                 *[str(argument) for argument in extra]],
                capture_output=True, text=True, env=self.environment(), stdin=handle,
            )

    def claude_stdin(self):
        """What the stub Claude CLI was able to read off stdin when the monitor called it."""
        path = self.stub_dir / "claude-stdin"
        return path.read_text() if path.exists() else ""

    def window(self, *extra):
        """Ask for the run's dashboard window, as a script re-running the command would."""
        return self.run_monitor(
            "window", "--run-dir", self.run_dir, "--session", SESSION_TARGET, *extra
        )

    def unpin(self, *extra):
        """Take the run's pin out of the registry, as the end of the run does."""
        return self.run_monitor("unpin", "--run-dir", self.run_dir, *extra)

    def config(self, surface):
        """A project config choosing this run's surface, as the setup wizard writes one."""
        path = self.root / "agentcrew.toml"
        path.write_text(f'[dashboard]\nsurface = "{surface}"\n')
        return path

    def pins(self, directory=None):
        """Every pin in the registry, parsed — what a statusline tick has to find the run by."""
        directory = pathlib.Path(directory) if directory else self.pin_dir()
        if not directory.is_dir():
            return []
        return [json.loads(path.read_text()) for path in sorted(directory.glob("*.json"))]

    def statusline(self, output=PREVIOUS_STATUSLINE):
        """The statusline command the operator already runs, printing one recognisable line."""
        self.statusline_path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{output}'\n")
        self.statusline_path.chmod(0o755)
        return self.statusline_path

    def settings(self, text=None, **fields):
        """The operator's settings file, as JSON fields or as the exact text `text` spells."""
        self.settings_path.write_text(text if text is not None else json.dumps(fields, indent=2))
        return self.settings_path

    def settings_json(self):
        return json.loads(self.settings_path.read_text())

    def pin_install(self, *extra, monitor=MONITOR):
        """Wire the pin in, against this fixture's settings file and wrapper path."""
        return self.run_release(
            monitor,
            "pin-install", "--settings", self.settings_path, "--statusline", self.wrapper_path,
            *extra,
        )

    def run_statusline(self, payload='{"session_id":"pin-install-test"}'):
        """Run the installed wrapper as Claude Code runs it: the JSON on its stdin."""
        return subprocess.run(
            ["/bin/sh", str(self.wrapper_path)], input=payload,
            capture_output=True, text=True, env=self.environment(),
        )

    def backups(self, path):
        """Every file the installer left beside `path` — a backup is the only thing that does."""
        return sorted(
            entry for entry in path.parent.iterdir()
            if entry != path and entry.name.startswith(path.name)
        )

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

    def start_loop(self):
        """Start the dashboard's refresh loop; returns the `Popen`, killed and reaped at cleanup.

        Nothing ever ends this loop on its own — a finished run holds its frame rather than
        exiting — so the kill is the only way out, and the wait after it is what keeps a loop the
        suite abandoned from being left a zombie.
        """
        process = self.fixture.refreshing_dashboard()
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        return process

    def drawn(self):
        """Everything the loop has drawn so far."""
        return self.fixture.frames_path.read_text(errors="replace")

    def frames(self):
        """How many whole frames the loop has drawn so far, counted by their one summary line."""
        return self.drawn().count(f"crew {RUN_ID} —")

    def await_frames(self, count):
        """Wait for the loop to draw `count` frames; returns everything it has drawn by then."""
        return self.await_loop(lambda: self.frames() >= count, f"{count} frames")

    def await_text(self, text):
        """Wait for the loop to draw `text`; returns everything it has drawn by then."""
        return self.await_loop(lambda: text in self.drawn(), repr(text))

    def await_loop(self, drawn, wanted):
        """Wait for the loop to satisfy `drawn`, or fail naming what it drew instead."""
        deadline = time.monotonic() + FRAME_DEADLINE_SECONDS
        while not drawn():
            if time.monotonic() >= deadline:
                self.fail(
                    f"the dashboard never drew {wanted} in {FRAME_DEADLINE_SECONDS}s; "
                    f"{self.frames()} frames drawn\n{self.drawn()}"
                    f"{self.fixture.frames_errors.read_text(errors='replace')}"
                )
            time.sleep(FRAME_POLL_SECONDS)
        return self.drawn()

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

    def test_a_review_a_bridge_actually_ran_annotates_its_row_and_then_stops(self):
        """The whole path, end to end: the bridge writes, the dashboard draws, both unaided.

        Ticket 07 is the Codex child, so its review lane is the Claude one, and the elapsed clock
        is left to whatever the frame measures — these stamps are the ones a live run wrote.
        """
        self.launch_wave_one()
        self.fixture.live({"06": "busy", "07": "busy"})

        completed, snapshot = self.fixture.review("07")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        settled = self.fixture.log.read_text()
        self.fixture.log.write_text(snapshot.read_text())
        running = self.fixture.run_monitor("dashboard", "--run-dir", self.fixture.run_dir)
        self.fixture.log.write_text(settled)
        returned = self.fixture.run_monitor("dashboard", "--run-dir", self.fixture.run_dir)

        self.assertEqual(running.returncode, 0, running.stderr)
        self.assertIn(f"↳ review: claude {MODEL} running · ", running.stdout)
        self.assertEqual(returned.returncode, 0, returned.stderr)
        self.assertNotIn("↳ review:", returned.stdout)

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

    def blocked_merge(self):
        """A wave halted on a ruling: 06's receipt landed, its merge did not, and the wave stopped.

        This is the shape the real run took — a receipt, then a `merge` the driver could not
        finish, then the `advance` that handed the wave to the coordinator.
        """
        self.launch_wave_one()
        self.fixture.append(SETTLED_TS, "receipt", ticket="06", verdict="landable",
                            sha=self.fixture.head("06"))
        self.fixture.append(BLOCKED_TS, "merge", ticket="06", result="escalated",
                            branch="worktree-06", into="crew/feature", detail=BLOCKED_DETAIL)
        self.fixture.append(BLOCKED_TS, "advance", wave=1, decision="escalated")

    def test_a_wave_halted_on_a_ruling_says_so_in_the_summary_and_under_the_blocked_row(self):
        self.blocked_merge()
        # Wide enough for the marker and the merge driver's own words: what a narrower window does
        # to either is the cutting the frame already has its own tests for.
        self.fixture.columns = HALTED_COLUMNS
        self.fixture.live({"07": "busy"})

        result = self.fixture.dashboard()

        self.assertEqual(frame(result.stdout), "\n".join([
            f"crew {RUN_ID} — wave 1/2 · pending=1 running=1 waiting=1 · "
            f"{AWAITING_RULING} · elapsed {LIVE_ELAPSED}",
            header(),
            row("1", "06", TITLES["06"], CLAUDE_LANE, "waiting", BLOCKED_ELAPSED),
            f"  ↳ last event: merge escalated — {BLOCKED_DETAIL} · {BLOCKED_TS}",
            row("1", "07", TITLES["07"], CODEX_LANE, "running", LIVE_ELAPSED),
            row("2", "08", TITLES["08"], CLAUDE_LANE, "pending", NO_ELAPSED),
        ]))

    def test_a_merge_that_hit_a_conflict_is_not_drawn_landable(self):
        self.launch_wave_one()
        self.fixture.append(SETTLED_TS, "receipt", ticket="06", verdict="landable",
                            sha=self.fixture.head("06"))
        self.fixture.append(BLOCKED_TS, "merge", ticket="06", result="conflict",
                            branch="worktree-06", into="crew/feature", detail=BLOCKED_DETAIL)
        self.fixture.live({"07": "busy"})

        result = self.fixture.dashboard()

        self.assertIn(
            row("1", "06", TITLES["06"], CLAUDE_LANE, "waiting", BLOCKED_ELAPSED),
            frame(result.stdout),
        )
        self.assertNotIn("landable", result.stdout)

    def test_the_ruling_lands_the_wave_re_runs_and_both_marks_go(self):
        self.blocked_merge()
        self.fixture.append(RULED_TS, "merge", ticket="06", result="clean",
                            branch="worktree-06", into="crew/feature")
        self.fixture.append(RULED_TS, "advance", wave=2, decision="launched")
        self.fixture.live({"07": "busy"})

        result = self.fixture.dashboard()

        self.assertIn(
            row("1", "06", TITLES["06"], CLAUDE_LANE, "merged", RULED_ELAPSED),
            frame(result.stdout),
        )
        self.assertNotIn(AWAITING_RULING, result.stdout)
        self.assertNotIn("↳ last event:", result.stdout)

    def rework_sent(self):
        """The rework path as the run walks it: a receipt, a semantic conflict, the instruction.

        The merge driver escalates the conflict it cannot resolve, and the driver hands the child
        that lost the race the instruction to resolve it — so the child is working again, on a
        ticket whose last settling line is still that escalated merge.
        """
        self.launch_wave_one()
        self.fixture.append(SETTLED_TS, "receipt", ticket="06", verdict="landable",
                            sha=self.fixture.head("06"))
        self.fixture.append(BLOCKED_TS, "merge", ticket="06", result="escalated",
                            branch="worktree-06", into="crew/feature", detail=BLOCKED_DETAIL)
        self.fixture.append(RULED_TS, "ruling", ticket="06", role="coordinator",
                            to=CHILDREN["06"], message=REWORK_INSTRUCTION)

    def test_a_child_reworking_a_semantic_conflict_is_drawn_reworking_rather_than_waiting(self):
        self.rework_sent()
        self.fixture.live({"06": "busy", "07": "busy"})

        result = self.fixture.dashboard()

        self.assertIn(
            row("1", "06", TITLES["06"], CLAUDE_LANE, "reworking", BLOCKED_ELAPSED,
                width=REWORKING_WIDTH),
            frame(result.stdout),
        )
        self.assertNotIn("waiting", result.stdout)
        self.assertIn(f"  ↳ last event: ruling · {RULED_TS}\n", result.stdout)

    def test_a_child_that_stopped_under_its_rework_instruction_is_waiting_not_reworking(self):
        """`reworking` says work is happening, so a child that is not working cannot wear it."""
        self.rework_sent()
        self.fixture.live({"06": "idle", "07": "busy"})

        result = self.fixture.dashboard()

        self.assertIn(
            row("1", "06", TITLES["06"], CLAUDE_LANE, "waiting", BLOCKED_ELAPSED),
            frame(result.stdout),
        )
        self.assertNotIn("reworking", result.stdout)

    def test_a_child_gone_from_its_lane_under_its_rework_instruction_is_not_reworking(self):
        self.rework_sent()
        self.fixture.live({"07": "busy"})

        result = self.fixture.dashboard()

        self.assertIn(
            row("1", "06", TITLES["06"], CLAUDE_LANE, "waiting", BLOCKED_ELAPSED),
            frame(result.stdout),
        )
        self.assertNotIn("reworking", result.stdout)

    def test_a_conflict_bounced_back_a_second_time_is_waiting_again(self):
        """The rung above: the instruction fired once, and the same conflict came back anyway.

        That escalation is the coordinator's, not the child's, so the row owes the operator the
        abnormal word again — the instruction standing in the log is an older one.
        """
        self.rework_sent()
        self.fixture.append(RULED_TS, "receipt", ticket="06", verdict="landable",
                            sha=self.fixture.head("06"))
        self.fixture.append(RULED_TS, "merge", ticket="06", result="escalated",
                            branch="worktree-06", into="crew/feature", detail=BLOCKED_DETAIL)
        self.fixture.live({"06": "busy", "07": "busy"})

        result = self.fixture.dashboard()

        self.assertIn(
            row("1", "06", TITLES["06"], CLAUDE_LANE, "waiting", RULED_ELAPSED),
            frame(result.stdout),
        )
        self.assertNotIn("reworking", result.stdout)

    def test_a_wave_between_its_last_receipt_and_its_merges_is_settling(self):
        self.launch_wave_one()
        self.fixture.append(SETTLED_TS, "receipt", ticket="06", verdict="landable",
                            sha=self.fixture.head("06"))
        self.fixture.append(SETTLED_TS, "receipt", ticket="07", verdict="landable",
                            sha=self.fixture.head("07"))
        self.fixture.live({})

        result = self.fixture.dashboard()

        self.assertEqual(frame(result.stdout), "\n".join([
            f"crew {RUN_ID} — wave 1/2 · pending=1 settling=2 · elapsed {LIVE_ELAPSED}",
            header(width=SETTLING_WIDTH),
            row("1", "06", TITLES["06"], CLAUDE_LANE, "settling", SETTLED_ELAPSED,
                width=SETTLING_WIDTH),
            row("1", "07", TITLES["07"], CODEX_LANE, "settling", SETTLED_ELAPSED,
                width=SETTLING_WIDTH),
            row("2", "08", TITLES["08"], CLAUDE_LANE, "pending", NO_ELAPSED,
                width=SETTLING_WIDTH),
        ]))

    def test_a_merge_takes_its_own_row_out_of_settling_and_leaves_the_rest_in_it(self):
        self.launch_wave_one()
        self.fixture.append(SETTLED_TS, "receipt", ticket="06", verdict="landable",
                            sha=self.fixture.head("06"))
        self.fixture.append(SETTLED_TS, "receipt", ticket="07", verdict="landable",
                            sha=self.fixture.head("07"))
        self.fixture.append(BLOCKED_TS, "merge", ticket="06", result="clean",
                            branch="worktree-06", into="crew/feature")
        self.fixture.live({})

        result = self.fixture.dashboard()

        self.assertIn(
            row("1", "06", TITLES["06"], CLAUDE_LANE, "merged", BLOCKED_ELAPSED,
                width=SETTLING_WIDTH),
            frame(result.stdout),
        )
        self.assertIn(
            row("1", "07", TITLES["07"], CODEX_LANE, "settling", SETTLED_ELAPSED,
                width=SETTLING_WIDTH),
            frame(result.stdout),
        )

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
        self.start_loop()

        self.assertIn("running", self.await_frames(1))
        self.fixture.append(SETTLED_TS, "receipt", ticket="06", verdict="landable",
                            sha=self.fixture.head("06"))
        output = self.await_text("landable")

        self.assertGreater(output.count(f"crew {RUN_ID} —"), 1)

    def test_a_finished_run_stops_refreshing_and_keeps_its_last_frame(self):
        self.launch_wave_one()
        self.fixture.append(SETTLED_TS, "receipt", ticket="06", verdict="landable",
                            sha=self.fixture.head("06"))
        self.fixture.append(SETTLED_TS, "receipt", ticket="07", verdict="failed")
        self.fixture.append(SETTLED_TS, "advance", wave=2, decision="complete")
        self.fixture.live({})
        process = self.start_loop()

        output = self.await_frames(1)
        time.sleep(HELD_FRAME_SECONDS)

        self.assertIsNone(process.poll(), "the renderer left the window without a frame in it")
        self.assertEqual(self.frames(), 1, self.drawn())
        self.assertIn("landable", output)

    def test_a_run_the_driver_stopped_keeps_its_last_frame_and_claims_no_ruling(self):
        """The other ending: the chain stopped on an escalation nobody can rule away.

        The driver writes `stopped` when it ends such a run, and that line is what tells the frame
        the run is over — the `escalated` decision above it never could, because the same word is
        written when a wave is halted and a ruling would carry it on.
        """
        self.launch_wave_one()
        self.fixture.append(SETTLED_TS, "receipt", ticket="06", verdict="landable",
                            sha=self.fixture.head("06"))
        self.fixture.append(SETTLED_TS, "receipt", ticket="07", verdict="failed")
        self.fixture.append(BLOCKED_TS, "advance", wave=1, decision="escalated")
        self.fixture.append(BLOCKED_TS, "advance", wave=1, decision="stopped")
        self.fixture.live({})
        process = self.start_loop()

        output = self.await_frames(1)
        time.sleep(HELD_FRAME_SECONDS)

        self.assertIsNone(process.poll(), "the renderer left the window without a frame in it")
        self.assertEqual(self.frames(), 1, self.drawn())
        self.assertNotIn(AWAITING_RULING, output)

    def test_a_wave_that_escalated_keeps_redrawing_because_the_run_is_not_over(self):
        """The defect this ticket exists for: only `complete` ends a run, and the frame with it."""
        self.launch_wave_one()
        self.fixture.append(BLOCKED_TS, "advance", wave=1, decision="escalated")
        self.fixture.live({"06": "busy", "07": "busy"})
        self.start_loop()

        output = self.await_frames(2)

        self.assertGreater(output.count(f"crew {RUN_ID} —"), 1)

    def test_a_wave_an_interruption_halted_keeps_redrawing_too(self):
        self.launch_wave_one()
        self.fixture.append(BLOCKED_TS, "advance", wave=1, decision="interrupted")
        self.fixture.live({"06": "busy", "07": "busy"})
        self.start_loop()

        output = self.await_frames(2)

        self.assertGreater(output.count(f"crew {RUN_ID} —"), 1)

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

    def test_each_escalation_occurrence_toasts_once(self):
        self.launch_wave_one()
        self.fixture.append(
            NOW_TS, "escalation", ticket="06", role="child",
            message="CREW ASK 06 first escalation — ts=1755060042",
        )
        self.fixture.live({"06": "busy", "07": "busy"})

        self.fixture.dashboard()
        self.fixture.dashboard()

        self.fixture.append(
            NOW_TS, "escalation", ticket="06", role="child",
            message="CREW ASK 06 second escalation — ts=1755060043",
        )
        self.fixture.dashboard()
        self.fixture.dashboard()

        self.assertEqual(
            self.fixture.toasts(),
            ["crew 06 escalated", "crew 06 escalated"],
        )

    def test_pin_toasts_on_the_first_tick_and_not_on_the_second(self):
        self.launch_wave_one()
        self.fixture.live({"06": "waiting", "07": "busy"})
        self.fixture.pin()

        first = self.fixture.pin_frame("--no-color")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(self.fixture.toasts(), ["crew 06 stuck at a permission prompt"])

        second = self.fixture.pin_frame("--no-color")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.fixture.toasts(), ["crew 06 stuck at a permission prompt"])

    def test_pin_no_toast_never_records_a_display_message(self):
        self.launch_wave_one()
        self.fixture.live({"06": "waiting", "07": "busy"})
        self.fixture.pin()

        result = self.fixture.pin_frame("--no-color", "--no-toast")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.fixture.toasts(), [])

    def test_pin_toast_display_is_bounded_by_the_tick_timeout(self):
        self.launch_wave_one()
        self.fixture.live({"06": "waiting", "07": "busy"})
        self.fixture.pin()
        delay = 2
        self.fixture.slow_toasts(delay)

        started = time.monotonic()
        result = self.fixture.pin_frame("--no-color", "--timeout", "0.2")
        elapsed = time.monotonic() - started

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"crew {RUN_ID} —", result.stdout)
        self.assertLess(elapsed, delay)

    def test_pin_and_dashboard_deduplicate_through_the_same_toast_state(self):
        self.launch_wave_one()
        self.fixture.live({"06": "waiting", "07": "busy"})
        self.fixture.pin()
        shared_state = self.fixture.root / "shared-toasts.json"

        dashboard = self.fixture.dashboard("--toast-state", shared_state)
        pin = self.fixture.pin_frame("--no-color", "--toast-state", shared_state)

        self.assertEqual(dashboard.returncode, 0, dashboard.stderr)
        self.assertEqual(pin.returncode, 0, pin.stderr)
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


class PinTests(MonitorTestCase):
    """One statusline tick: find the live run through the pin registry, and draw it or nothing.

    Every dead case is silence — no stdout, no stderr, exit 0 — because a statusline that spews
    diagnostics across the operator's prompt is worse than one that goes quiet.
    """

    def launch_wave_one(self):
        self.fixture.table()
        self.fixture.worktree("06")
        self.fixture.worktree("07")
        self.fixture.launch("06")
        self.fixture.launch("07", executor="codex", model=CODEX_MODEL)

    def live_run(self):
        """A run of two launched, busy children and one wave nobody has reached yet."""
        self.launch_wave_one()
        self.fixture.live({"06": "busy", "07": "busy"})

    def assertNothingDrawn(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_no_pin_at_all_draws_nothing(self):
        self.live_run()

        self.assertNothingDrawn(self.fixture.pin_frame())

    def test_a_pin_naming_a_run_directory_that_is_gone_draws_nothing(self):
        self.live_run()
        self.fixture.pin(run_dir=self.fixture.root / "crew-run-gone")

        self.assertNothingDrawn(self.fixture.pin_frame())

    def test_a_pin_whose_coordinator_is_gone_draws_nothing(self):
        self.live_run()
        self.fixture.pin(pid=self.fixture.dead_pid())

        self.assertNothingDrawn(self.fixture.pin_frame())

    def test_a_run_a_final_advance_decision_ended_draws_nothing(self):
        self.live_run()
        self.fixture.append(SETTLED_TS, "advance", wave=2, decision="complete")
        self.fixture.pin()

        self.assertNothingDrawn(self.fixture.pin_frame())

    def test_a_run_the_driver_stopped_draws_nothing(self):
        self.live_run()
        self.fixture.append(BLOCKED_TS, "advance", wave=1, decision="escalated")
        self.fixture.append(BLOCKED_TS, "advance", wave=1, decision="stopped")
        self.fixture.pin()

        self.assertNothingDrawn(self.fixture.pin_frame())

    def test_a_run_an_escalated_advance_halted_is_still_drawn(self):
        """An escalation is not the end of a run, so the statusline keeps naming it."""
        self.live_run()
        self.fixture.append(BLOCKED_TS, "advance", wave=1, decision="escalated")
        self.fixture.pin()

        result = self.fixture.pin_frame("--no-color")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"crew {RUN_ID} —", result.stdout)

    def test_a_run_an_interruption_halted_is_still_drawn(self):
        self.live_run()
        self.fixture.append(BLOCKED_TS, "advance", wave=1, decision="interrupted")
        self.fixture.pin()

        result = self.fixture.pin_frame("--no-color")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"crew {RUN_ID} —", result.stdout)

    def test_the_pin_says_a_halted_wave_is_awaiting_a_ruling(self):
        self.launch_wave_one()
        self.fixture.append(SETTLED_TS, "receipt", ticket="06", verdict="landable",
                            sha=self.fixture.head("06"))
        self.fixture.append(BLOCKED_TS, "merge", ticket="06", result="escalated",
                            branch="worktree-06", into="crew/feature", detail=BLOCKED_DETAIL)
        self.fixture.append(BLOCKED_TS, "advance", wave=1, decision="escalated")
        self.fixture.columns = HALTED_COLUMNS
        self.fixture.live({"07": "busy"})
        self.fixture.pin()

        result = self.fixture.pin_frame("--no-color")

        self.assertEqual(
            frame(result.stdout).splitlines()[0],
            f"crew {RUN_ID} — wave 1/2 · pending=1 running=1 waiting=1 · "
            f"{AWAITING_RULING} · elapsed {LIVE_ELAPSED}",
        )
        self.assertIn(
            f"  ↳ last event: merge escalated — {BLOCKED_DETAIL} · {BLOCKED_TS}", result.stdout
        )

    def test_a_malformed_wave_table_draws_nothing(self):
        self.live_run()
        self.fixture.table_path.write_text("{ this is not the wave table")
        self.fixture.pin()

        self.assertNothingDrawn(self.fixture.pin_frame())

    def test_a_pin_that_is_not_a_pin_file_draws_nothing(self):
        self.live_run()
        self.fixture.pin_dir().mkdir(parents=True, exist_ok=True)
        (self.fixture.pin_dir() / "broken.json").write_text("{ half a pin")

        self.assertNothingDrawn(self.fixture.pin_frame())

    def test_the_wrapper_is_told_in_one_line_that_a_pin_cannot_be_read(self):
        """ADR-0011's exception, asked for by the one caller that cannot judge it: the wrapper
        reads the registry with `sed`, so whether a file is a pin at all is decided here."""
        self.live_run()
        self.fixture.pin_dir().mkdir(parents=True, exist_ok=True)
        (self.fixture.pin_dir() / "broken.json").write_text("{ half a pin")

        result = self.fixture.pin_frame("--from-wrapper")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 1, result.stdout)
        self.assertIn(str(self.fixture.pin_dir()), result.stdout)

    def test_a_live_run_beside_an_unreadable_pin_is_drawn_and_says_nothing_else(self):
        """The notice never displaces a frame: it is what is printed when nothing was drawn."""
        self.live_run()
        self.fixture.pin()
        (self.fixture.pin_dir() / "broken.json").write_text("{ half a pin")

        result = self.fixture.pin_frame("--from-wrapper", "--no-color")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"crew {RUN_ID} —", result.stdout)
        self.assertNotIn(str(self.fixture.pin_dir()), result.stdout)

    def test_a_registry_of_readable_pins_is_silent_for_the_wrapper_too(self):
        """A run that is simply over is not a wiring fault, so the exception does not fire."""
        self.live_run()
        self.fixture.append(SETTLED_TS, "advance", wave=2, decision="complete")
        self.fixture.pin()

        self.assertNothingDrawn(self.fixture.pin_frame("--from-wrapper"))

    def test_the_pin_matching_the_callers_session_is_the_run_drawn(self):
        self.live_run()
        self.fixture.pin(session=CALLER_SESSION)
        self.fixture.pin(run_dir=self.fixture.second_run(), session=OTHER_SESSION)

        result = self.fixture.pin_frame("--no-color")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"crew {RUN_ID} —", result.stdout)
        self.assertNotIn(SECOND_RUN_ID, result.stdout)

    def test_the_other_pin_is_drawn_from_the_other_session(self):
        """The same two pins, read from the other crew's session, draw the other crew's run."""
        self.live_run()
        self.fixture.pin(session=CALLER_SESSION)
        self.fixture.pin(run_dir=self.fixture.second_run(), session=OTHER_SESSION)
        self.fixture.tmux_session = OTHER_SESSION

        result = self.fixture.pin_frame("--no-color")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"crew {SECOND_RUN_ID} —", result.stdout)
        self.assertNotIn(f"crew {RUN_ID} —", result.stdout)

    def test_a_caller_whose_environment_cannot_answer_asks_tmux_itself(self):
        self.live_run()
        self.fixture.pin(session=CALLER_SESSION)
        self.fixture.pin(run_dir=self.fixture.second_run(), session=OTHER_SESSION)
        self.fixture.tmux_session = None
        self.fixture.tmux_says_session(OTHER_SESSION)

        result = self.fixture.pin_frame("--no-color")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"crew {SECOND_RUN_ID} —", result.stdout)
        self.assertNotIn(f"crew {RUN_ID} —", result.stdout)

    def test_the_environment_answers_without_tmux_being_asked(self):
        self.live_run()
        self.fixture.pin()

        self.fixture.pin_frame("--no-color")

        self.assertEqual(self.fixture.calls("tmux"), [])

    def test_two_pins_and_neither_matching_the_caller_draws_nothing(self):
        self.live_run()
        self.fixture.pin(session=OTHER_SESSION)
        self.fixture.pin(run_dir=self.fixture.second_run(), session=OTHER_SESSION)
        self.fixture.tmux_session = CALLER_SESSION

        self.assertNothingDrawn(self.fixture.pin_frame())

    def test_a_session_running_neither_of_two_crews_draws_nothing(self):
        """The third tab: two runs in two sessions, and a session that launched neither of them."""
        self.live_run()
        self.fixture.pin(session=CALLER_SESSION)
        self.fixture.pin(run_dir=self.fixture.second_run(), session=OTHER_SESSION)
        self.fixture.tmux_session = BYSTANDER_SESSION

        self.assertNothingDrawn(self.fixture.pin_frame())

    def test_the_sole_pin_is_drawn_in_the_session_it_records(self):
        self.live_run()
        self.fixture.pin(session=CALLER_SESSION)
        self.fixture.tmux_session = CALLER_SESSION

        result = self.fixture.pin_frame("--no-color")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"crew {RUN_ID} —", result.stdout)

    def test_the_sole_pin_is_not_drawn_where_it_names_another_session(self):
        """A pin draws in the session that launched its run and nowhere else, however few pins the
        registry holds: the lone pin used to be drawn everywhere, which is the defect."""
        self.live_run()
        self.fixture.pin(session=OTHER_SESSION)
        self.fixture.tmux_session = CALLER_SESSION

        self.assertNothingDrawn(self.fixture.pin_frame())

    def test_a_caller_without_a_tmux_session_draws_nothing(self):
        """Outside tmux there is no session to match, so there is nothing to draw — the accepted
        consequence of scoping the pin to the session that launched the run."""
        self.live_run()
        self.fixture.pin(session=CALLER_SESSION)
        self.fixture.tmux_session = None

        self.assertNothingDrawn(self.fixture.pin_frame())

    def test_a_pin_recording_an_aliased_run_directory_is_resolved_and_drawn(self):
        self.live_run()
        self.fixture.pin(run_dir=self.fixture.aliased_run_dir())

        result = self.fixture.pin_frame("--no-color")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"crew {RUN_ID} —", result.stdout)

    def test_a_registry_given_with_pin_dir_is_the_one_read(self):
        self.live_run()
        elsewhere = self.fixture.root / "elsewhere"
        self.fixture.pin(directory=elsewhere)

        result = self.fixture.pin_frame("--no-color", "--pin-dir", elsewhere)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"crew {RUN_ID} —", result.stdout)

    def test_the_frame_is_the_dashboards_frame_for_the_same_run_at_the_same_moment(self):
        self.live_run()
        self.fixture.pin()

        drawn = self.fixture.pin_frame()
        dashboard = self.fixture.dashboard("--no-color")

        self.assertEqual(drawn.returncode, 0, drawn.stderr)
        self.assertEqual(ANSI.sub("", drawn.stdout), dashboard.stdout)
        self.assertEqual(
            frame(dashboard.stdout).splitlines()[0],
            f"crew {RUN_ID} — wave 1/2 · pending=1 running=2 · elapsed {LIVE_ELAPSED}",
        )

    def test_the_state_column_is_coloured_even_though_stdout_is_a_pipe(self):
        self.live_run()
        self.fixture.pin()

        result = self.fixture.pin_frame()

        self.assertIn(f"{RUNNING_COLOUR}running{COLOUR_RESET}", result.stdout)

    def test_no_color_draws_plain_text(self):
        self.live_run()
        self.fixture.pin()

        result = self.fixture.pin_frame("--no-color")

        self.assertIn("running", result.stdout)
        self.assertNotIn("\x1b[", result.stdout)

    def test_the_NO_COLOR_environment_draws_plain_text(self):
        self.live_run()
        self.fixture.pin()
        self.fixture.extra_environment["NO_COLOR"] = "1"

        result = self.fixture.pin_frame()

        self.assertIn("running", result.stdout)
        self.assertNotIn("\x1b[", result.stdout)

    def test_the_title_column_is_cut_to_the_width_columns_gives_it(self):
        # Ten columns is what this run's other five leave of a 76-column statusline, so the two
        # long titles are cut to it and the short one is not.
        self.fixture.columns = 76
        self.live_run()
        self.fixture.pin()

        result = self.fixture.pin_frame("--no-color")

        for line in frame(result.stdout).splitlines():
            self.assertLessEqual(len(line), 76, line)
        self.assertIn(
            row("1", "06", "Dispatch…", CLAUDE_LANE, "running", LIVE_ELAPSED, title=10),
            result.stdout,
        )

    def test_taller_than_budget_drops_settled_rows_before_live_rows(self):
        self.live_run()
        self.fixture.append(
            REVIEW_TS, "review", ticket="06", state="running", lane=REVIEW_LANE
        )
        self.fixture.append(
            SETTLED_TS, "receipt", ticket="06", verdict="landable", sha=self.fixture.head("06")
        )
        self.fixture.append(SETTLED_TS, "merge", ticket="06", result="clean")
        self.fixture.pin()
        self.fixture.lines = 7

        result = self.fixture.pin_frame("--no-color")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(
            row("1", "06", TITLES["06"], CLAUDE_LANE, "merged", SETTLED_ELAPSED),
            result.stdout,
        )
        self.assertIn(
            row("1", "07", TITLES["07"], CODEX_LANE, "running", LIVE_ELAPSED),
            result.stdout,
        )
        self.assertIn(
            row("2", "08", TITLES["08"], CLAUDE_LANE, "pending", NO_ELAPSED),
            result.stdout,
        )
        self.assertIn("… +1 more", result.stdout)
        self.assertLessEqual(len(frame(result.stdout).splitlines()), 5)

    def test_still_taller_drops_annotations_then_reports_omitted_rows(self):
        self.live_run()
        self.fixture.live({"06": "waiting", "07": "idle"})
        self.fixture.pin()
        self.fixture.lines = 6

        result = self.fixture.pin_frame("--no-color")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("↳", result.stdout)
        self.assertIn("… +2 more", result.stdout)
        self.assertIn(
            row("1", "06", TITLES["06"], CLAUDE_LANE, "waiting", LIVE_ELAPSED),
            result.stdout,
        )
        self.assertLessEqual(len(frame(result.stdout).splitlines()), 4)

    def test_budget_below_header_leaves_summary_alone_without_blank_lines(self):
        self.live_run()
        self.fixture.pin()
        self.fixture.lines = 3

        result = self.fixture.pin_frame("--no-color")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            frame(result.stdout).splitlines(),
            [f"crew {RUN_ID} — wave 1/2 · pending=1 running=2 · elapsed {LIVE_ELAPSED}"],
        )
        self.assertNotIn("\n\n", result.stdout)

    def test_a_full_frame_never_emits_a_blank_line(self):
        self.live_run()
        self.fixture.pin()

        result = self.fixture.pin_frame("--no-color")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(all(line for line in frame(result.stdout).splitlines()))

    def test_max_lines_overrides_lines(self):
        self.live_run()
        self.fixture.pin()
        self.fixture.lines = 3

        result = self.fixture.pin_frame("--no-color", "--max-lines", "4")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(header(), result.stdout)
        self.assertIn("… +2 more", result.stdout)
        self.assertLessEqual(len(frame(result.stdout).splitlines()), 4)

    def test_a_live_source_that_times_out_is_an_unknown_row_and_the_frame_still_draws(self):
        self.live_run()
        self.fixture.pin()
        self.fixture.slow_agents(30)

        result = self.fixture.pin_frame("--no-color", "--timeout", "0.2")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertIn(f"crew {RUN_ID} —", result.stdout)
        self.assertIn(
            row("1", "06", TITLES["06"], CLAUDE_LANE, "running", LIVE_ELAPSED),
            result.stdout,
        )
        self.assertIn(f"{AGENTS_UNREADABLE}\n", result.stdout)
        # The lane read from its own files is unaffected by the one that did not answer.
        self.assertIn(
            row("1", "07", TITLES["07"], CODEX_LANE, "running", LIVE_ELAPSED),
            result.stdout,
        )

    def test_a_tick_with_no_time_left_draws_each_lane_unknown_and_still_prints_the_frame(self):
        """Both lanes are bounded by the one budget, and a spent budget still draws a frame."""
        self.live_run()
        self.fixture.pin()

        result = self.fixture.pin_frame("--no-color", "--timeout", "0")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"crew {RUN_ID} —", result.stdout)
        self.assertIn(f"{AGENTS_UNREADABLE}\n", result.stdout)
        self.assertIn(f"{CODEX_UNREADABLE}\n", result.stdout)

    def test_a_bridge_state_file_nothing_will_ever_write_does_not_hold_up_the_tick(self):
        """A fifo where a state file should be blocks an ordinary read for ever; a tick cannot."""
        self.live_run()
        self.fixture.pin()
        state = self.fixture.codex_state("07")
        state.unlink()
        os.mkfifo(state)

        tick = self.fixture.start_monitor(
            "pin", "--now", NOW_TS, "--no-color", "--timeout", "0.2"
        )
        self.addCleanup(tick.kill)
        output = tick.communicate(timeout=10)[0]

        self.assertEqual(tick.returncode, 0)
        self.assertIn(f"crew {RUN_ID} —", output)

    def test_the_json_claude_code_writes_to_the_tick_is_never_read_by_a_live_source(self):
        self.live_run()
        self.fixture.pin()
        written = self.fixture.root / "statusline-stdin.json"
        written.write_text(json.dumps({"workspace": {"current_dir": str(self.fixture.repo)}}))

        result = self.fixture.pin_frame_over(written, "--no-color")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"crew {RUN_ID} —", result.stdout)
        self.assertEqual(self.fixture.claude_stdin(), "")

    def test_no_failure_ever_reaches_stderr_as_a_monitor_error(self):
        self.live_run()
        gone = self.fixture.root / "crew-run-gone"

        for pin in (None, {"run_dir": gone}, {"pid": self.fixture.dead_pid()}):
            with self.subTest(pin=pin):
                shutil.rmtree(self.fixture.pin_dir(), ignore_errors=True)
                if pin is not None:
                    self.fixture.pin(**pin)

                result = self.fixture.pin_frame()

                self.assertNotIn("MONITOR ERROR", result.stderr)
                self.assertEqual(result.stderr, "")
                self.assertEqual(result.returncode, 0)


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


class SurfaceTests(MonitorTestCase):
    """Which surface a run draws itself on, and the pin's lifecycle: written at dispatch, removed
    when the run ends. The window stays the default, so a run that says nothing runs as it does
    today.
    """

    def setUp(self):
        super().setUp()
        self.fixture.table()

    def surfaced(self, surface, *extra):
        """Dispatch's call, on a repo whose config chose that surface."""
        return self.fixture.window(
            "--config", self.fixture.config(surface),
            "--coordinator-pid", COORDINATOR_PID, *extra,
        )

    def test_dispatch_writes_a_pin_naming_the_run_the_coordinator_pid_and_its_session(self):
        """And the renderer and interpreter that draw it: the running release's own monitor and
        the interpreter running it, both necessarily alive at the moment the pin is written."""
        result = self.surfaced("pin")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.fixture.pins(), [{
            "run_dir": str(self.fixture.run_dir.resolve()),
            "coordinator_pid": COORDINATOR_PID,
            "tmux_session": SESSION_TARGET,
            "renderer": str(MONITOR.resolve()),
            "interpreter": sys.executable,
        }])

    def test_the_pin_names_the_runs_realpath_however_the_run_dir_is_spelled(self):
        """The `/tmp` against `/private/tmp` case: one run, one pin, under its resolved path."""
        link = self.fixture.root / "alias-run"
        link.symlink_to(self.fixture.run_dir, target_is_directory=True)

        result = self.fixture.run_monitor(
            "window", "--run-dir", link, "--session", SESSION_TARGET,
            "--config", self.fixture.config("pin"), "--coordinator-pid", COORDINATOR_PID,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            [pin["run_dir"] for pin in self.fixture.pins()],
            [str(self.fixture.run_dir.resolve())],
        )

    def test_dispatching_every_wave_leaves_the_run_one_pin(self):
        """The command is re-run each wave, as the window's is; a run pins itself once."""
        self.surfaced("pin")

        self.surfaced("pin")

        self.assertEqual(len(self.fixture.pins()), 1, self.fixture.pins())

    def test_the_end_of_the_run_removes_the_pin(self):
        self.surfaced("pin")

        result = self.fixture.unpin()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.fixture.pins(), [])

    def test_removing_a_pin_a_run_never_wrote_is_quiet(self):
        """A `surface = "window"` run ends through the same step, and has nothing to remove."""
        result = self.fixture.unpin()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.fixture.pins(), [])

    def test_a_pin_surface_never_launches_the_dashboard_window(self):
        result = self.surfaced("pin")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.fixture.window_calls(), [])
        self.assertEqual(self.fixture.live_windows(), {})
        self.assertIsNone(self.fixture.recorded_window())
        self.assertEqual(len(self.fixture.pins()), 1)

    def test_a_window_surface_writes_no_pin(self):
        result = self.surfaced("window")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.fixture.window_calls()), 1, self.fixture.calls("tmux"))
        self.assertEqual(self.fixture.pins(), [])

    def test_a_run_whose_repo_configures_nothing_keeps_todays_behaviour(self):
        """No config file at all: the window is created and no pin is written, as before."""
        result = self.fixture.window("--coordinator-pid", COORDINATOR_PID)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.fixture.window_calls()), 1, self.fixture.calls("tmux"))
        self.assertEqual(self.fixture.pins(), [])

    def test_a_config_file_that_is_not_there_is_the_default_surface(self):
        result = self.fixture.window(
            "--config", self.fixture.root / "absent.toml",
            "--coordinator-pid", COORDINATOR_PID,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.fixture.window_calls()), 1, self.fixture.calls("tmux"))
        self.assertEqual(self.fixture.pins(), [])

    def test_both_surfaces_run_over_one_toast_state_file(self):
        """Window and pin both run, and both dedup through the run's own toast state — which is
        what stops the two passes announcing the same thing twice.
        """
        result = self.surfaced("both")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.fixture.window_calls()), 1, self.fixture.calls("tmux"))
        command = self.fixture.window_calls()[0][-1]
        self.assertIn(f"--toast-state {self.fixture.run_dir.resolve() / TOAST_STATE}", command)
        # The pin's own pass reads that same file, because it is the one this run directory has.
        self.assertEqual(
            [pin["run_dir"] for pin in self.fixture.pins()],
            [str(self.fixture.run_dir.resolve())],
        )

    def test_the_registry_the_pin_is_written_into_is_overridable(self):
        elsewhere = self.fixture.root / "other-pins"

        result = self.surfaced("pin", "--pin-dir", elsewhere)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.fixture.pins(elsewhere)), 1)
        self.assertEqual(self.fixture.pins(), [])

    def test_a_pin_written_elsewhere_is_removed_from_there(self):
        elsewhere = self.fixture.root / "other-pins"
        self.surfaced("pin", "--pin-dir", elsewhere)

        result = self.fixture.unpin("--pin-dir", elsewhere)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.fixture.pins(elsewhere), [])

    def test_an_unknown_surface_is_reported_and_nothing_is_run(self):
        result = self.surfaced("popup")

        self.assertEqual(result.returncode, 3, result.stdout)
        self.assertIn("MONITOR ERROR", result.stderr)
        self.assertEqual(self.fixture.window_calls(), [])
        self.assertEqual(self.fixture.pins(), [])

    def test_the_pin_dispatch_writes_is_the_one_the_statusline_tick_draws(self):
        """The two halves of the surface meet here: what dispatch writes, `pin` selects and draws.

        The session is the join. Dispatch passes tmux's target spelling, `$7:`, while the tick
        reads its own session as `$7`, so a second crew's pin is present to make the match do the
        selecting rather than the sole-pin fallback.
        """
        self.fixture.worktree("06")
        self.fixture.worktree("07")
        self.fixture.launch("06")
        self.fixture.launch("07", executor="codex", model=CODEX_MODEL)
        self.fixture.live({"06": "busy", "07": "busy"})
        self.fixture.window(
            "--config", self.fixture.config("pin"), "--coordinator-pid", os.getpid(),
        )
        self.fixture.pin(
            run_dir=self.fixture.root / SECOND_RUN_ID, pid=os.getpid(), session=OTHER_SESSION
        )
        self.fixture.tmux_says_session(CALLER_SESSION)

        drawn = self.fixture.pin_frame()

        self.assertEqual(drawn.returncode, 0, drawn.stderr)
        self.assertEqual(ANSI.sub("", drawn.stdout), self.fixture.dashboard("--no-color").stdout)
        self.assertEqual(
            frame(ANSI.sub("", drawn.stdout)).splitlines()[0],
            f"crew {RUN_ID} — wave 1/2 · pending=1 running=2 · elapsed {LIVE_ELAPSED}",
        )

    def test_a_pinned_surface_without_the_coordinators_pid_is_reported(self):
        """The pid is the whole crash story, so a pin may never be written without one."""
        result = self.fixture.window("--config", self.fixture.config("pin"))

        self.assertEqual(result.returncode, 3, result.stdout)
        self.assertIn("MONITOR ERROR", result.stderr)
        self.assertEqual(self.fixture.pins(), [])
        self.assertEqual(self.fixture.window_calls(), [])


class CostTests(MonitorTestCase):
    """The cost pass at run completion: one event per child, and the run's rollup."""

    def cost(self, *extra):
        return self.fixture.run_monitor("cost", "--log", self.fixture.log, *extra)

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

    def test_a_same_vendor_review_transcript_is_not_folded_into_the_childs_row(self):
        """A review is its own session, already costed under its own lane-tagged row.

        Reviewing a Claude child on the Claude lane leaves a second transcript in the child's
        worktree; billing the child for it would charge the ticket twice for tokens the review
        lane already accounts for.
        """
        self.fixture.worktree("06")
        self.fixture.launch("06")
        self.fixture.claude_transcript("06")
        self.fixture.claude_transcript("06", session=REVIEW_SESSION)
        self.fixture.append(
            LAUNCH_TS, "session-cost", ticket="06", executor="claude", model=MODEL,
            lane=f"claude {MODEL}", session=REVIEW_SESSION,
            input_tokens=CLAUDE_TOTALS["input"], output_tokens=CLAUDE_TOTALS["output"],
            cache_read_tokens=CLAUDE_TOTALS["cache_read"],
            cache_creation_tokens=CLAUDE_TOTALS["cache_creation"],
            total_tokens=CLAUDE_TOTALS["total"],
        )

        result = self.cost()

        self.assertEqual(result.returncode, 0, result.stderr)
        child = [entry for entry in self.costs() if "lane" not in entry]
        self.assertEqual(len(child), 1, child)
        self.assertEqual(child[0]["session"], CLAUDE_SESSION)
        self.assertEqual(child[0]["total_tokens"], CLAUDE_TOTALS["total"])

    def test_a_claude_transcript_with_a_subdirectory_after_the_worktree_is_measured(self):
        worktree = self.fixture.worktree("06")
        self.fixture.launch("06")
        transcript = self.fixture.claude_transcript("06")
        subdirectory = worktree / "tests"
        subdirectory.mkdir()
        self.fixture.set_claude_cwds(transcript, [worktree, subdirectory])

        result = self.cost()

        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.costs()[0]
        self.assertEqual(entry["input_tokens"], CLAUDE_TOTALS["input"])
        self.assertEqual(entry["output_tokens"], CLAUDE_TOTALS["output"])
        self.assertEqual(entry["cache_read_tokens"], CLAUDE_TOTALS["cache_read"])
        self.assertEqual(entry["cache_creation_tokens"], CLAUDE_TOTALS["cache_creation"])
        self.assertEqual(entry["total_tokens"], CLAUDE_TOTALS["total"])

    def test_a_claude_transcript_whose_only_cwd_is_a_subdirectory_is_measured_once(self):
        worktree = self.fixture.worktree("06")
        self.fixture.launch("06")
        subdirectory = worktree / "tests"
        subdirectory.mkdir()
        self.fixture.claude_transcript("06", cwd=subdirectory)

        result = self.cost()

        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.costs()[0]
        self.assertEqual(entry["input_tokens"], CLAUDE_TOTALS["input"])
        self.assertEqual(entry["output_tokens"], CLAUDE_TOTALS["output"])
        self.assertEqual(entry["cache_read_tokens"], CLAUDE_TOTALS["cache_read"])
        self.assertEqual(entry["cache_creation_tokens"], CLAUDE_TOTALS["cache_creation"])
        self.assertEqual(entry["total_tokens"], CLAUDE_TOTALS["total"])

    def test_several_subdirectories_in_one_claude_transcript_are_one_identity(self):
        worktree = self.fixture.worktree("06")
        self.fixture.launch("06")
        transcript = self.fixture.claude_transcript("06")
        first_subdirectory = worktree / "first"
        second_subdirectory = worktree / "second"
        first_subdirectory.mkdir()
        second_subdirectory.mkdir()
        self.fixture.set_claude_cwds(transcript, [first_subdirectory, second_subdirectory])

        result = self.cost()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.costs()[0]["total_tokens"], CLAUDE_TOTALS["total"])

    def test_a_symlinked_subdirectory_resolving_inside_the_worktree_is_measured(self):
        worktree = self.fixture.worktree("06")
        self.fixture.launch("06")
        target = worktree / "real-tests"
        target.mkdir()
        symlink = worktree / "tests"
        symlink.symlink_to(target, target_is_directory=True)
        transcript = self.fixture.claude_transcript("06")
        self.fixture.set_claude_cwds(transcript, [worktree, symlink])

        result = self.cost()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.costs()[0]["total_tokens"], CLAUDE_TOTALS["total"])

    def test_a_symlinked_path_resolving_outside_the_worktree_is_diagnosed(self):
        worktree = self.fixture.worktree("06")
        self.fixture.launch("06")
        outside = worktree.parent / "outside"
        outside.mkdir()
        symlink = worktree / "outside-link"
        symlink.symlink_to(outside, target_is_directory=True)
        transcript = self.fixture.claude_transcript("06")
        self.fixture.set_claude_cwds(transcript, [worktree, symlink])

        result = self.cost()

        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.costs()[0]
        self.assertIn(str(symlink), entry["detail"])
        self.assertNotIn("total_tokens", entry)

    def test_a_launch_worktree_and_transcript_with_aliased_spellings_are_one_worktree(self):
        self.fixture.worktree("06")
        self.fixture.launch("06", worktree=self.fixture.alias("06"))
        self.fixture.claude_transcript("06")

        result = self.cost()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.costs()[0]["total_tokens"], CLAUDE_TOTALS["total"])

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

    def test_a_codex_rollout_with_a_subdirectory_of_the_worktree_is_measured(self):
        worktree = self.fixture.worktree("07")
        self.fixture.launch("07", executor="codex", model=CODEX_MODEL)
        subdirectory = worktree / "package"
        subdirectory.mkdir()
        self.fixture.codex_rollout("07", cwd=subdirectory)

        result = self.cost()

        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.costs()[0]
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

    def test_two_transcripts_claiming_one_session_are_diagnosed_even_from_subdirectories(self):
        worktree = self.fixture.worktree("07")
        self.fixture.launch("07", executor="codex", model=CODEX_MODEL)
        first_subdirectory = worktree / "first"
        first_subdirectory.mkdir()
        first = self.fixture.codex_rollout("07", cwd=first_subdirectory)
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

    def test_a_claude_transcript_that_leaves_the_worktree_is_diagnosed(self):
        worktree = self.fixture.worktree("06")
        self.fixture.launch("06")
        transcript = self.fixture.claude_transcript("06")
        outside = worktree.parent / "outside"
        outside.mkdir()
        self.fixture.set_claude_cwds(transcript, [worktree, outside])

        result = self.cost()

        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.costs()[0]
        self.assertIn(str(outside), entry["detail"])
        self.assertNotIn("total_tokens", entry)

    def test_a_sibling_worktree_with_a_shared_prefix_is_outside(self):
        worktree = self.fixture.worktree("06")
        self.fixture.launch("06")
        transcript = self.fixture.claude_transcript("06")
        sibling = worktree.with_name(worktree.name + "-2")
        sibling.mkdir()
        self.fixture.set_claude_cwds(transcript, [worktree, sibling])

        result = self.cost()

        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.costs()[0]
        self.assertIn(str(sibling), entry["detail"])
        self.assertNotIn("total_tokens", entry)

    def test_a_parent_of_the_worktree_is_outside(self):
        worktree = self.fixture.worktree("06")
        self.fixture.launch("06")
        transcript = self.fixture.claude_transcript("06")
        parent = worktree.parent
        self.fixture.set_claude_cwds(transcript, [worktree, parent])

        result = self.cost()

        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.costs()[0]
        self.assertIn(str(parent), entry["detail"])
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

    def coordinator_row(self, output):
        """The rollup's coordinator row, which sits beneath the total."""
        return next(row for row in cost_rows(output) if row[0] == "coordinator")

    def test_the_named_session_is_a_coordinator_row_beneath_the_total(self):
        self.fixture.worktree("06")
        self.fixture.launch("06")
        self.fixture.claude_transcript("06")
        self.fixture.coordinator_transcript()

        result = self.cost("--coordinator-session", COORDINATOR_SESSION)

        self.assertEqual(result.returncode, 0, result.stderr)
        rows = cost_rows(result.stdout)
        self.assertEqual(rows[-2][0], "TOTAL")
        self.assertEqual(
            rows[-1],
            ["coordinator", "claude", "--"] + [
                str(CLAUDE_TOTALS[name])
                for name in ("input", "output", "cache_read", "cache_creation", "total")
            ],
        )

    def test_the_coordinator_row_is_left_out_of_the_runs_total(self):
        self.fixture.worktree("06")
        self.fixture.launch("06")
        self.fixture.claude_transcript("06")
        self.fixture.coordinator_transcript()

        result = self.cost("--coordinator-session", COORDINATOR_SESSION)

        total = next(row for row in cost_rows(result.stdout) if row[0] == "TOTAL")
        self.assertEqual(total[-1], str(CLAUDE_TOTALS["total"]))

    def test_the_coordinators_own_transcript_is_not_billed_to_a_child(self):
        self.fixture.worktree("06")
        self.fixture.launch("06")
        self.fixture.claude_transcript("06")
        self.fixture.coordinator_transcript()

        self.cost("--coordinator-session", COORDINATOR_SESSION)

        self.assertEqual(self.costs()[0]["total_tokens"], CLAUDE_TOTALS["total"])

    def test_the_coordinator_row_is_printed_and_never_logged(self):
        self.fixture.worktree("06")
        self.fixture.launch("06")
        self.fixture.claude_transcript("06")
        self.fixture.coordinator_transcript()

        self.cost("--coordinator-session", COORDINATOR_SESSION)

        self.assertEqual([entry["ticket"] for entry in self.costs()], ["06"])

    def test_a_half_written_last_line_leaves_the_rest_of_the_session_counted(self):
        self.fixture.worktree("06")
        self.fixture.launch("06")
        transcript = self.fixture.coordinator_transcript()
        transcript.write_text(transcript.read_text() + "{ the request in flight\n")

        result = self.cost("--coordinator-session", COORDINATOR_SESSION)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.coordinator_row(result.stdout)[-1], str(CLAUDE_TOTALS["total"]))

    def test_a_line_that_does_not_parse_mid_session_is_a_dashed_row_and_a_reason(self):
        self.fixture.worktree("06")
        self.fixture.launch("06")
        transcript = self.fixture.coordinator_transcript()
        lines = transcript.read_text().splitlines()
        transcript.write_text("\n".join([lines[0], "{ half a line", *lines[1:]]) + "\n")

        result = self.cost("--coordinator-session", COORDINATOR_SESSION)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.coordinator_row(result.stdout)[-1], "--")
        self.assertIn(f"coordinator not measured: {transcript} carries a line", result.stdout)

    def test_a_session_with_no_transcript_is_a_dashed_row_and_a_reason(self):
        self.fixture.worktree("06")
        self.fixture.launch("06")
        self.fixture.claude_transcript("06")

        result = self.cost("--coordinator-session", COORDINATOR_SESSION)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.coordinator_row(result.stdout), ["coordinator", "claude", "--", "--", "--", "--",
                                                  "--", "--"],
        )
        self.assertIn(f"coordinator not measured: no transcript named {COORDINATOR_SESSION}",
                      result.stdout)

    def test_an_empty_session_id_is_a_dashed_row_rather_than_a_failure(self):
        self.fixture.worktree("06")
        self.fixture.launch("06")
        self.fixture.claude_transcript("06")

        result = self.cost("--coordinator-session", "")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.coordinator_row(result.stdout)[-1], "--")
        self.assertIn("coordinator not measured: no session id", result.stdout)

    def test_a_rollup_asked_for_no_coordinator_carries_no_such_row(self):
        self.fixture.worktree("06")
        self.fixture.launch("06")
        self.fixture.claude_transcript("06")

        result = self.cost()

        self.assertEqual([row[0] for row in cost_rows(result.stdout)][-1], "TOTAL")

    def test_the_cost_pass_costs_the_coordinator_nothing(self):
        self.fixture.worktree("06")
        self.fixture.launch("06")
        self.fixture.claude_transcript("06")

        self.cost()

        self.assertEqual(self.fixture.calls("claude"), [])
        self.assertEqual(self.fixture.calls("tmux"), [])


class PinInstallTests(MonitorTestCase):
    """Wiring the pin into the operator's statusline: authorised, reversible, safe to repeat."""

    def setUp(self):
        super().setUp()
        self.statusline = self.fixture.statusline()

    def wired(self, **extra):
        """Settings whose statusline is the operator's own script, as they are before an install."""
        fields = {UNRELATED_SETTING[0]: UNRELATED_SETTING[1]}
        fields["statusLine"] = {"type": "command", "command": str(self.statusline), **extra}
        return self.fixture.settings(**fields)

    def status_line(self):
        return self.fixture.settings_json()["statusLine"]

    @staticmethod
    def spelled(path):
        """How the installer spells a path it was given: absolute and resolved (ADR-0007), because
        `statusLine.command` is run from whatever directory the session happens to be in."""
        return str(path.resolve())

    def test_a_dry_run_prints_the_change_and_writes_nothing(self):
        self.wired()
        before = self.fixture.settings_path.read_text()

        result = self.fixture.pin_install()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(self.spelled(self.fixture.settings_path), result.stdout)
        self.assertIn(self.spelled(self.fixture.wrapper_path), result.stdout)
        self.assertIn(str(self.statusline), result.stdout)
        self.assertIn("--apply", result.stdout)
        self.assertEqual(self.fixture.settings_path.read_text(), before)
        self.assertFalse(self.fixture.wrapper_path.exists())
        self.assertEqual(self.fixture.backups(self.fixture.settings_path), [])

    def test_applying_over_an_existing_statusline_keeps_it_and_prints_it_first(self):
        self.wired()
        before = self.fixture.settings_path.read_text()

        result = self.fixture.pin_install("--apply")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.status_line()["command"], self.spelled(self.fixture.wrapper_path))
        self.assertEqual(
            self.fixture.settings_json()[UNRELATED_SETTING[0]], UNRELATED_SETTING[1]
        )
        drawn = self.fixture.run_statusline()
        self.assertEqual(drawn.stdout.splitlines()[0], PREVIOUS_STATUSLINE)
        backups = self.fixture.backups(self.fixture.settings_path)
        self.assertEqual([backup.read_text() for backup in backups], [before])

    def test_the_wrapper_carries_no_path_that_a_release_can_expire(self):
        """What the installer writes is a permanent stub: neither the release that wrote it nor
        the interpreter that ran it is recorded, so no upgrade can strand it."""
        self.wired()

        self.fixture.pin_install("--apply")

        wrapper = self.fixture.wrapper_path.read_text()
        self.assertNotIn(str(MONITOR), wrapper)
        self.assertNotIn(sys.executable, wrapper)
        self.assertLess(
            wrapper.index(str(self.statusline)), wrapper.index("/".join(PIN_REGISTRY)),
            "the operator's own statusline still runs before the pin's frame",
        )

    def test_a_second_apply_changes_nothing(self):
        self.wired()
        self.fixture.pin_install("--apply")
        settings = self.fixture.settings_path.read_text()
        wrapper = self.fixture.wrapper_path.read_text()
        backups = self.fixture.backups(self.fixture.settings_path)

        result = self.fixture.pin_install("--apply")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.fixture.settings_path.read_text(), settings)
        self.assertEqual(self.fixture.wrapper_path.read_text(), wrapper)
        self.assertEqual(self.fixture.backups(self.fixture.settings_path), backups)

    def test_with_no_statusline_at_all_one_is_created_that_is_just_the_pin(self):
        self.fixture.settings(**{UNRELATED_SETTING[0]: UNRELATED_SETTING[1]})

        result = self.fixture.pin_install("--apply")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.status_line()["command"], self.spelled(self.fixture.wrapper_path))
        # The pin is the whole of what the statusline runs: the operator's script — which this
        # settings file never named — is not summoned, and with no run pinned nothing is printed.
        wrapper = self.fixture.wrapper_path.read_text()
        self.assertNotIn(str(self.statusline), wrapper)
        drawn = self.fixture.run_statusline()
        self.assertEqual(drawn.returncode, 0, drawn.stderr)
        self.assertEqual(drawn.stdout, "")

    def test_an_install_onto_a_fresh_machine_sets_the_pins_refresh_interval(self):
        self.fixture.settings(**{UNRELATED_SETTING[0]: UNRELATED_SETTING[1]})

        self.fixture.pin_install("--apply")

        self.assertEqual(self.status_line()["refreshInterval"], PIN_REFRESH_INTERVAL)

    def test_a_refresh_interval_the_operator_set_is_left_alone(self):
        self.wired(refreshInterval=OPERATOR_REFRESH_INTERVAL)

        self.fixture.pin_install("--apply")

        self.assertEqual(self.status_line()["refreshInterval"], OPERATOR_REFRESH_INTERVAL)

    def test_unparseable_settings_are_refused_without_writing_anything(self):
        self.fixture.settings(text='{"statusLine": {"command": "mine.sh"')
        before = self.fixture.settings_path.read_text()

        result = self.fixture.pin_install("--apply")

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.fixture.settings_path.read_text(), before)
        self.assertFalse(self.fixture.wrapper_path.exists())
        self.assertEqual(self.fixture.backups(self.fixture.settings_path), [])

    def test_uninstalling_restores_the_previous_command_exactly(self):
        self.wired()
        before = self.fixture.settings_json()
        self.fixture.pin_install("--apply")

        result = self.fixture.pin_install("--uninstall", "--apply")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.fixture.settings_json(), before)

    def test_uninstalling_leaves_the_refresh_interval_it_did_not_add(self):
        self.wired(refreshInterval=OPERATOR_REFRESH_INTERVAL)
        before = self.fixture.settings_json()
        self.fixture.pin_install("--apply")

        self.fixture.pin_install("--uninstall", "--apply")

        self.assertEqual(self.fixture.settings_json(), before)

    def test_uninstalling_a_statusline_the_installer_created_takes_it_away(self):
        self.fixture.settings(**{UNRELATED_SETTING[0]: UNRELATED_SETTING[1]})
        self.fixture.pin_install("--apply")

        self.fixture.pin_install("--uninstall", "--apply")

        self.assertEqual(
            self.fixture.settings_json(), {UNRELATED_SETTING[0]: UNRELATED_SETTING[1]}
        )

    def test_uninstalling_refuses_when_the_statusline_has_moved_on(self):
        self.wired()
        self.fixture.pin_install("--apply")
        moved = self.fixture.settings_json()
        chosen = str(self.fixture.settings_dir / "chosen-since.sh")
        moved["statusLine"] = {"type": "command", "command": chosen}
        self.fixture.settings(**moved)
        before = self.fixture.settings_path.read_text()

        result = self.fixture.pin_install("--uninstall", "--apply")

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.fixture.settings_path.read_text(), before)
        self.assertTrue(self.fixture.wrapper_path.exists())

    def test_uninstalling_is_a_dry_run_until_it_is_applied(self):
        self.wired()
        self.fixture.pin_install("--apply")
        installed = self.fixture.settings_path.read_text()

        result = self.fixture.pin_install("--uninstall")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(self.statusline), result.stdout)
        self.assertEqual(self.fixture.settings_path.read_text(), installed)
        self.assertTrue(self.fixture.wrapper_path.exists())

    def test_an_installed_wrapper_that_differs_is_reported_and_rewritten(self):
        """A wrapper an older release wrote is not what this one would write, so the difference is
        the whole point of re-running the install: it is reported, and `--apply` puts it right."""
        self.wired()
        self.fixture.pin_install("--apply")
        wanted = self.fixture.wrapper_path.read_text()
        self.fixture.wrapper_path.write_text(wanted + "# a wrapper an older release wrote\n")

        reported = self.fixture.pin_install()

        self.assertEqual(reported.returncode, 0, reported.stderr)
        self.assertIn(f"rewrite {self.spelled(self.fixture.wrapper_path)}", reported.stdout)
        self.assertNotIn("nothing to change", reported.stdout)
        self.assertNotEqual(self.fixture.wrapper_path.read_text(), wanted)

        applied = self.fixture.pin_install("--apply")

        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertEqual(self.fixture.wrapper_path.read_text(), wanted)

    def test_the_install_costs_the_coordinator_nothing(self):
        self.wired()

        self.fixture.pin_install("--apply")

        self.assertEqual(self.fixture.calls("claude"), [])
        self.assertEqual(self.fixture.calls("tmux"), [])


class PinWrapperTests(MonitorTestCase):
    """The wrapper the install leaves behind: a permanent stub that runs whatever the live pin
    names.

    Nothing in it expires, so a release that replaces the one that installed it draws the frame
    with no re-install. Every path through it exits 0, because Claude Code blanks the operator's
    whole statusline when the statusline command does not.
    """

    def live_run(self):
        """A run of two launched, busy children and one wave nobody has reached yet."""
        self.fixture.table()
        self.fixture.worktree("06")
        self.fixture.worktree("07")
        self.fixture.launch("06")
        self.fixture.launch("07", executor="codex", model=CODEX_MODEL)
        self.fixture.live({"06": "busy", "07": "busy"})

    def install(self, previous=True, monitor=MONITOR):
        """The wrapper in place, over the operator's own statusline or over nothing at all."""
        if previous:
            statusline = self.fixture.statusline()
            self.fixture.settings(**{
                "statusLine": {"type": "command", "command": str(statusline)},
            })
        else:
            self.fixture.settings(**{UNRELATED_SETTING[0]: UNRELATED_SETTING[1]})
        result = self.fixture.pin_install("--apply", monitor=monitor)
        self.assertEqual(result.returncode, 0, result.stderr)

    def assertOneLine(self, drawn):
        """The one exception to the silence contract: a single line, and still exit 0."""
        self.assertEqual(drawn.returncode, 0, drawn.stderr)
        self.assertEqual(len(drawn.stdout.splitlines()), 1, drawn.stdout)

    def test_a_release_that_replaced_the_installer_draws_the_frame(self):
        """The upgrade: release X installs the wrapper and is then gone; release Y dispatches the
        run and its pin is what the same wrapper draws, with no second install."""
        self.live_run()
        release_x = self.fixture.release_copy("release-x")
        self.install(monitor=release_x)
        shutil.rmtree(release_x.parent)
        self.fixture.window(
            "--config", self.fixture.config("pin"), "--coordinator-pid", os.getpid(),
        )

        drawn = self.fixture.run_statusline()

        self.assertEqual(drawn.returncode, 0, drawn.stderr)
        self.assertEqual(drawn.stdout.splitlines()[0], PREVIOUS_STATUSLINE)
        self.assertIn(f"crew {RUN_ID} —", ANSI.sub("", drawn.stdout))

    def test_no_live_pin_leaves_the_operators_own_statusline_alone(self):
        self.live_run()
        self.install()

        drawn = self.fixture.run_statusline()

        self.assertEqual(drawn.returncode, 0, drawn.stderr)
        self.assertEqual(drawn.stdout.splitlines(), [PREVIOUS_STATUSLINE])

    def test_a_live_pin_is_drawn_beneath_the_operators_own_statusline(self):
        self.live_run()
        self.install()
        self.fixture.pin()

        drawn = self.fixture.run_statusline()

        self.assertEqual(drawn.returncode, 0, drawn.stderr)
        self.assertEqual(drawn.stdout.splitlines()[0], PREVIOUS_STATUSLINE)
        self.assertIn(f"crew {RUN_ID} —", ANSI.sub("", drawn.stdout))

    def test_a_pin_whose_renderer_is_gone_prints_one_line_and_exits_zero(self):
        self.live_run()
        self.install(previous=False)
        self.fixture.pin(renderer=self.fixture.root / "release-gone" / MONITOR.name)

        self.assertOneLine(self.fixture.run_statusline())

    def test_a_pin_whose_interpreter_is_gone_prints_one_line_and_exits_zero(self):
        self.live_run()
        self.install(previous=False)
        self.fixture.pin(interpreter=self.fixture.root / "python-gone")

        self.assertOneLine(self.fixture.run_statusline())

    def test_a_pin_that_cannot_be_read_prints_one_line_and_exits_zero(self):
        self.live_run()
        self.install(previous=False)
        self.fixture.pin_dir().mkdir(parents=True, exist_ok=True)
        (self.fixture.pin_dir() / "broken.json").write_text("{ half a pin")

        self.assertOneLine(self.fixture.run_statusline())

    def test_a_pin_that_cannot_be_read_but_spells_a_renderer_prints_one_line(self):
        """The wrapper reads the two paths out with `sed`, which is not a JSON parser: a file that
        is not a pin at all can still spell them. What the wrapper reaches judges the registry
        properly, so this is still one line rather than the silence a bad read would leave."""
        self.live_run()
        self.install(previous=False)
        self.fixture.pin_dir().mkdir(parents=True, exist_ok=True)
        (self.fixture.pin_dir() / "broken.json").write_text(
            f'{{"renderer": "{MONITOR}", "interpreter": "{sys.executable}", half a pin'
        )

        self.assertOneLine(self.fixture.run_statusline())

    def test_a_renderer_path_the_wrapper_cannot_read_back_prints_one_line(self):
        """A path carrying a quote is escaped in the pin's JSON and `sed` reads it back short, so
        the wrapper reaches no renderer. The bound on that is the point: it is the one actionable
        line and exit 0, never a blank statusline."""
        self.live_run()
        self.install(previous=False)
        self.fixture.pin(renderer=self.fixture.release_copy('rel"ease'))

        self.assertOneLine(self.fixture.run_statusline())

    def test_a_pin_written_before_the_fields_existed_prints_one_line_and_exits_zero(self):
        """A run launched by a release older than this fix: its pin names no renderer, so the
        wrapper says so rather than drawing nothing the operator cannot explain."""
        self.live_run()
        self.install(previous=False)
        self.fixture.pin(renderer=None, interpreter=None)

        self.assertOneLine(self.fixture.run_statusline())

    def test_a_dead_reference_leaves_the_operators_own_statusline_standing(self):
        """The exit code is the whole point: a non-zero one blanks the operator's lines too."""
        self.live_run()
        self.install()
        self.fixture.pin(renderer=self.fixture.root / "release-gone" / MONITOR.name)

        drawn = self.fixture.run_statusline()

        self.assertEqual(drawn.returncode, 0, drawn.stderr)
        self.assertEqual(drawn.stdout.splitlines()[0], PREVIOUS_STATUSLINE)
        self.assertEqual(len(drawn.stdout.splitlines()), 2, drawn.stdout)


if __name__ == "__main__":
    unittest.main()
