#!/usr/bin/env python3

import argparse
import asyncio
import importlib.util
import os
import pathlib
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
