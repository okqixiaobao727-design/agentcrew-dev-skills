#!/usr/bin/env python3

import argparse
import asyncio
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import re
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from hook_fixtures import marker_lines, write_hook_config  # noqa: E402


BRIDGE_PATH = (
    pathlib.Path(__file__).parents[1] / "scripts" / "tui_review_bridge.py"
)


def load_bridge():
    spec = importlib.util.spec_from_file_location("tui_review_bridge", BRIDGE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def base_args(**overrides):
    values = {
        "target": "HEAD",
        "cwd": "/workspace/ticket-50",
        "timeout": 1,
        "startup_timeout": 1,
        "sandbox": "danger-full-access",
        "approval": "never",
        "network": False,
        "tmux_target": None,
        "resume_session": None,
        "recover_session": False,
        "model": None,
        "effort": None,
        "probe": False,
        "browser_probe": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class FakeSubprocess:
    """Stands in for the module's `subprocess` while a launch is exercised.

    Only the bridge's own calls are faked; the hook runs its command for real
    through its own module.
    """

    def __init__(self, stdout=""):
        self.stdout = stdout
        self.calls = []

    def run(self, command, **kwargs):
        self.calls.append(command)
        return argparse.Namespace(returncode=0, stdout=self.stdout, stderr="")


class FakeClient:
    def __init__(self):
        self.requests = []

    async def request(self, method, params):
        self.requests.append((method, params))
        if method == "turn/start":
            return {"turn": {"id": "turn-followup"}}
        raise AssertionError(f"unexpected request: {method}")


class BridgeContractTests(unittest.TestCase):
    def setUp(self):
        self.bridge = load_bridge()

    def test_owner_uses_origin_pane_and_canonical_worktree(self):
        environment = {
            "TMUX": "/private/tmp/tmux-501/default,11028,2",
            "TMUX_PANE": "%24",
        }
        with mock.patch.object(
            self.bridge,
            "canonical_worktree_root",
            return_value="/workspace/ticket-50",
        ):
            owner = self.bridge.resolve_owner(base_args(), environment)

        self.assertEqual(owner.origin_pane, "%24")
        self.assertEqual(
            owner.tmux_server, "/private/tmp/tmux-501/default,11028"
        )
        self.assertEqual(owner.worktree_root, "/workspace/ticket-50")

    def test_parallel_panes_have_different_owner_keys(self):
        with mock.patch.object(
            self.bridge,
            "canonical_worktree_root",
            return_value="/workspace/property",
        ):
            owner_49 = self.bridge.resolve_owner(
                base_args(),
                {
                    "TMUX": "/private/tmp/tmux-501/default,11028,2",
                    "TMUX_PANE": "%23",
                },
            )
            owner_50 = self.bridge.resolve_owner(
                base_args(),
                {
                    "TMUX": "/private/tmp/tmux-501/default,11028,2",
                    "TMUX_PANE": "%24",
                },
            )

        self.assertNotEqual(owner_49.key, owner_50.key)

    def test_session_owner_rejects_another_parallel_pane(self):
        expected = self.bridge.InvocationOwner(
            tmux_server="/private/tmp/tmux-501/default,11028",
            origin_pane="%23",
            worktree_root="/workspace/ticket-49",
        )
        actual = self.bridge.InvocationOwner(
            tmux_server="/private/tmp/tmux-501/default,11028",
            origin_pane="%24",
            worktree_root="/workspace/ticket-50",
        )

        with self.assertRaisesRegex(RuntimeError, "belongs to another"):
            self.bridge.validate_session_owner(
                {"owner": expected.to_dict()}, actual
            )

    def test_launch_pane_requires_an_explicit_origin_target(self):
        with self.assertRaisesRegex(RuntimeError, "originating tmux pane"):
            self.bridge.launch_pane(
                base_args(tmux_target=None),
                pathlib.Path("/tmp/runtime"),
                pathlib.Path("/tmp/prompt"),
            )

    def test_followup_starts_a_turn_on_the_saved_thread(self):
        client = FakeClient()
        state = {"threadId": "thread-ticket-50"}

        result = asyncio.run(
            self.bridge.start_followup_turn(client, state, "review again")
        )

        self.assertEqual(result["turn"]["id"], "turn-followup")
        self.assertEqual(
            client.requests,
            [
                (
                    "turn/start",
                    {
                        "threadId": "thread-ticket-50",
                        "input": [
                            {
                                "type": "text",
                                "text": "review again",
                                "text_elements": [],
                            }
                        ],
                    },
                )
            ],
        )

    def test_interrupted_review_remains_resumable(self):
        self.assertFalse(
            self.bridge.should_cleanup_session(
                {"status": "interrupted"}, final_message=""
            )
        )

    def test_completed_review_without_a_report_is_not_kept(self):
        self.assertTrue(
            self.bridge.should_cleanup_session(
                {"status": "completed"}, final_message=""
            )
        )

    def test_reopened_tui_resumes_the_saved_thread(self):
        command = self.bridge.build_tui_command(
            base_args(thread_id="thread-ticket-50"),
            pathlib.Path("/tmp/app-server.sock"),
            "review again",
        )
        self.assertEqual(
            command[-3:],
            ["resume", "thread-ticket-50", "review again"],
        )

    def test_parser_exposes_explicit_resume_handle(self):
        args = self.bridge.build_parser().parse_args(
            ["--resume-session", "session-ticket-50", "HEAD"]
        )
        self.assertEqual(args.resume_session, "session-ticket-50")

    def test_session_store_is_overridable_and_private(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.dict(
                os.environ,
                {"CODE_REVIEW_TUI_STATE_DIR": temp_dir},
                clear=False,
            ):
                store = self.bridge.SessionStore()
                store.write(
                    "session-ticket-50",
                    {
                        "version": self.bridge.SESSION_STATE_VERSION,
                        "reviewSessionId": "session-ticket-50",
                        "threadId": "thread-ticket-50",
                    },
                )
                state_path = pathlib.Path(temp_dir) / "session-ticket-50.json"
                self.assertEqual(
                    store.read("session-ticket-50")["threadId"],
                    "thread-ticket-50",
                )
                self.assertEqual(state_path.stat().st_mode & 0o777, 0o600)

    def test_session_id_cannot_escape_the_state_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.bridge.SessionStore(temp_dir)
            with self.assertRaisesRegex(RuntimeError, "Invalid review session"):
                store.read("../another-task")

    def test_review_prompts_carry_the_request_and_name_no_skill(self):
        for prompt in (
            self.bridge.build_prompt(base_args(), "bridge-1"),
            self.bridge.build_prompt(base_args(), "bridge-1", followup=True),
        ):
            for marker in ("$code-review", "/code-review", "mattpocock-skills"):
                self.assertNotIn(marker, prompt)
            self.assertIn("HEAD", prompt)
            self.assertIn("Rounds contract", prompt)
            self.assertIn("two axes", prompt)
            self.assertIn("standards", prompt)
            self.assertIn("one re-review", prompt)
            self.assertIn("coordinator", prompt)
            self.assertIn("review-only task", prompt)

    def test_parser_carries_no_environment_specific_probe(self):
        with self.assertRaises(SystemExit):
            self.bridge.build_parser().parse_args(["--network-probe", "HEAD"])

    def test_same_pane_rejects_concurrent_bridge_calls(self):
        owner = self.bridge.InvocationOwner(
            tmux_server="/private/tmp/tmux-501/default,11028",
            origin_pane="%24",
            worktree_root="/workspace/ticket-50",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.bridge.SessionStore(temp_dir)
            with self.bridge.owner_lock(store, owner):
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    with self.bridge.owner_lock(store, owner):
                        pass


MARKER_PATTERN = re.compile(r"\[claude-tui-review-bridge:[^\]]+\]")


class FakeCodexSession:
    """One Codex TUI pane and its app-server, seen through the bridge's calls.

    It answers the three requests the bridge makes and records what it was asked
    to create, so a test can assert that a second pane or a second turn was never
    started.
    """

    def __init__(self):
        self.marker = None
        self.status = "in_progress"
        self.final_message = ""
        self.thread_id = "thread-ticket-13"
        self.turn_id = "turn-round-one"
        self.panes = []
        self.started_turns = []

    def launch_pane(self, args, runtime_dir, prompt_file):
        runtime_dir = pathlib.Path(runtime_dir)
        runtime_dir.mkdir(parents=True, exist_ok=True)
        (runtime_dir / "app-server.sock").touch()
        self.marker = MARKER_PATTERN.search(
            pathlib.Path(prompt_file).read_text(encoding="utf-8")
        ).group(0)
        self.panes.append("%%%d" % (90 + len(self.panes)))
        return self.panes[-1]

    def finish(self, message):
        self.status = "completed"
        self.final_message = message

    def turn(self):
        return {
            "id": self.turn_id,
            "status": self.status,
            "items": [
                {
                    "type": "userMessage",
                    "content": [{"type": "text", "text": self.marker}],
                },
                {
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": self.final_message,
                },
            ],
        }

    async def request(self, method, params):
        if method == "thread/list":
            return {"data": [{"id": self.thread_id, "preview": self.marker}]}
        if method == "thread/read":
            return {"thread": {"id": self.thread_id, "turns": [self.turn()]}}
        if method == "turn/start":
            self.started_turns.append(params)
            self.marker = MARKER_PATTERN.search(
                params["input"][0]["text"]
            ).group(0)
            return {"turn": {"id": self.turn_id}}
        raise AssertionError(f"unexpected request: {method}")

    async def __aexit__(self, *_ignored):
        return None


class RecoveryTests(unittest.TestCase):
    """A driver killed mid-review is recovered, not restarted.

    The whole path runs through `run_bridge` against a stubbed pane: the first
    call is killed the way the harness kills it — the record is written, the
    pane lives on, nothing is printed — and the second call has only its own
    owner identity to work from.
    """

    TMUX = "/private/tmp/tmux-501/default,11028,2"
    ORIGIN_PANE = "%235"

    def setUp(self):
        self.bridge = load_bridge()
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.root = pathlib.Path(self.work.name)
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        self.state_dir = self.root / "state"
        self.codex = FakeCodexSession()

        self.environment = {
            "TMUX": self.TMUX,
            "TMUX_PANE": self.ORIGIN_PANE,
            "CODE_REVIEW_TUI_STATE_DIR": str(self.state_dir),
        }
        self.worktree_root = str(self.worktree)
        self.enter(mock.patch.dict(os.environ, self.environment, clear=False))
        self.enter(mock.patch.object(
            self.bridge, "canonical_worktree_root",
            side_effect=lambda _cwd: self.worktree_root,
        ))
        self.enter(mock.patch.object(
            self.bridge, "launch_pane", self.codex.launch_pane
        ))
        self.enter(mock.patch.object(
            self.bridge, "pane_exists",
            side_effect=lambda pane: pane in self.codex.panes,
        ))
        self.enter(mock.patch.object(
            self.bridge, "connect_when_ready", self.connect
        ))
        self.enter(mock.patch.object(
            self.bridge, "connect_existing_session", self.connect_existing
        ))

    def enter(self, patcher):
        patcher.start()
        self.addCleanup(patcher.stop)

    async def connect(self, *_args, **_kwargs):
        return self.codex

    async def connect_existing(self, state):
        if state["paneId"] not in self.codex.panes:
            return None
        return self.codex

    def args(self, **overrides):
        values = {"cwd": str(self.worktree), "timeout": 5, "startup_timeout": 5}
        values.update(overrides)
        return base_args(**values)

    def run_bridge(self, args):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = asyncio.run(self.bridge.run_bridge(args))
        printed = stdout.getvalue().strip()
        return code, json.loads(printed) if printed else None

    def kill_the_driver(self):
        """The first review, killed the way the harness kills it."""
        with self.assertRaisesRegex(RuntimeError, "Timed out"):
            self.run_bridge(self.args(timeout=0.2))
        return self.stored_session()

    def stored_session(self):
        records = sorted(self.state_dir.glob("*.json"))
        self.assertEqual(len(records), 1, records)
        return json.loads(records[0].read_text(encoding="utf-8"))

    def test_a_killed_driver_leaves_a_recoverable_record_and_a_live_pane(self):
        state = self.kill_the_driver()

        self.assertEqual(self.codex.panes, ["%90"])
        self.assertEqual(state["threadId"], self.codex.thread_id)
        self.assertEqual(state["marker"], self.codex.marker)
        self.assertEqual(state["owner"]["origin_pane"], self.ORIGIN_PANE)

    def test_recovery_returns_the_same_session_without_a_second_pane(self):
        killed = self.kill_the_driver()
        self.codex.finish("two spec findings, one standards finding")

        code, output = self.run_bridge(
            self.args(target=None, recover_session=True)
        )

        self.assertEqual(code, 0)
        self.assertTrue(output["recovered"])
        self.assertEqual(output["reviewSessionId"], killed["reviewSessionId"])
        self.assertEqual(output["threadId"], self.codex.thread_id)
        self.assertEqual(output["paneId"], killed["paneId"])
        self.assertEqual(
            output["finalMessage"], "two spec findings, one standards finding"
        )
        self.assertEqual(self.codex.panes, ["%90"])
        self.assertEqual(self.codex.started_turns, [])
        self.assertEqual(
            self.stored_session()["target"], killed["target"]
        )

    def test_recovery_keeps_the_lineage_resumable_for_round_two(self):
        killed = self.kill_the_driver()
        self.codex.finish("round one findings")
        self.run_bridge(self.args(target=None, recover_session=True))

        code, output = self.run_bridge(
            self.args(resume_session=killed["reviewSessionId"])
        )

        self.assertEqual(code, 0)
        self.assertFalse(output["recovered"])
        self.assertEqual(len(self.codex.started_turns), 1)
        self.assertEqual(self.codex.panes, ["%90"])

    def test_another_origin_pane_recovers_nothing(self):
        self.kill_the_driver()
        os.environ["TMUX_PANE"] = "%777"

        with self.assertRaisesRegex(
            self.bridge.NoLiveSessionError, "No live review session"
        ):
            self.run_bridge(self.args(target=None, recover_session=True))

        self.assertEqual(self.codex.panes, ["%90"])

    def test_another_worktree_recovers_nothing(self):
        self.kill_the_driver()
        self.worktree_root = str(self.root / "another-worktree")

        with self.assertRaises(self.bridge.NoLiveSessionError):
            self.run_bridge(self.args(target=None, recover_session=True))

    def test_a_dead_pane_recovers_nothing(self):
        self.kill_the_driver()
        self.codex.panes.clear()

        with self.assertRaises(self.bridge.NoLiveSessionError):
            self.run_bridge(self.args(target=None, recover_session=True))

    def test_a_record_from_before_recovery_names_no_turn_to_wait_on(self):
        state = self.kill_the_driver()
        del state["marker"]
        (self.state_dir / f"{state['reviewSessionId']}.json").write_text(
            json.dumps(state), encoding="utf-8"
        )

        with self.assertRaises(self.bridge.NoLiveSessionError):
            self.run_bridge(self.args(target=None, recover_session=True))

    def test_nothing_to_recover_exits_distinguishably_from_a_failed_review(self):
        with mock.patch.object(sys, "argv", [
            "tui_review_bridge.py", "--recover-session", "--cwd", str(self.worktree),
        ]):
            code = self.bridge.main()

        self.assertEqual(code, self.bridge.NO_LIVE_SESSION_EXIT)
        self.assertNotEqual(self.bridge.NO_LIVE_SESSION_EXIT, 1)
        self.assertEqual(self.codex.panes, [])


class RecoveryParserTests(unittest.TestCase):
    def setUp(self):
        self.bridge = load_bridge()

    def test_recovery_needs_no_target(self):
        args = self.bridge.parse_args(["--recover-session"])

        self.assertTrue(args.recover_session)
        self.assertIsNone(args.target)

    def test_a_review_still_requires_its_target(self):
        with self.assertRaises(SystemExit):
            self.bridge.parse_args(["--cwd", "/workspace/ticket-13"])

    def test_recovery_and_resume_are_exclusive(self):
        with self.assertRaises(SystemExit):
            self.bridge.parse_args(
                ["--recover-session", "--resume-session", "session-13"]
            )

    def test_recovery_takes_no_target(self):
        with self.assertRaises(SystemExit):
            self.bridge.parse_args(["--recover-session", "HEAD"])


class LaunchHookTests(unittest.TestCase):
    """The review pane is a child launch, so the project's hook covers it too.

    A project that configures no hook gets what it got before the hook existed:
    the pane inherits the caller's environment and nothing is called.
    """

    TAG = "CREW_LAUNCH_TAG"

    def setUp(self):
        self.bridge = load_bridge()
        self.work = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.work.name)
        self.marker = self.root / "launched.log"
        self.addCleanup(self.work.cleanup)

    def marker_lines(self):
        return marker_lines(self.marker)

    def launch(self, pane_id):
        fake = FakeSubprocess(stdout=f"{pane_id}\n")
        with mock.patch.object(self.bridge, "subprocess", fake):
            returned = self.bridge.launch_pane(
                base_args(cwd=str(self.root), tmux_target="%1"),
                self.root / "runtime",
                self.root / "prompt.txt",
            )
        self.assertEqual(returned, pane_id)

    def test_an_unconfigured_hook_launches_the_pane_untouched(self):
        self.launch("%42")

        self.assertEqual(self.marker_lines(), [])

    def test_the_command_runs_once_for_the_pane_it_launched(self):
        write_hook_config(
            self.root,
            command=f'printf "%s\\n" "$AGENTCREW_CHILD_TMUX_TARGET" >> {self.marker}',
        )

        self.launch("%42")

        self.assertEqual(self.marker_lines(), ["%42"])

    def test_a_child_of_an_unconfigured_hook_gets_no_extra_environment(self):
        hook = self.bridge.launch_hook.load_hook(self.root)

        self.assertEqual(self.bridge.child_session_env(hook), dict(os.environ))

    def test_the_configured_variables_reach_the_codex_child(self):
        write_hook_config(self.root, env={self.TAG: "ticket-133"})

        hook = self.bridge.launch_hook.load_hook(self.root)

        self.assertEqual(self.bridge.child_session_env(hook)[self.TAG], "ticket-133")


if __name__ == "__main__":
    unittest.main()
