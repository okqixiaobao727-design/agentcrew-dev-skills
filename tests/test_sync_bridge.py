#!/usr/bin/env python3
"""Behaviour of the pinned Review-Switch vendoring command at its CLI seam."""

import os
import pathlib
import stat
import subprocess
import tempfile
import unittest


REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[1]
SYNC_SCRIPT = REPOSITORY_ROOT / "scripts" / "sync-bridge.sh"
VENDORED_BRIDGE = pathlib.Path(
    "skills/crew/assets/review/scripts/tui_review_bridge.py"
)


def pinned(name):
    """One `NAME="value"` assignment, read from the script that owns it.

    The pin is the script's to declare, and a copy of it here would be a second
    place to edit on every upgrade — one that goes stale silently, since a test
    asserting a sha nobody fetches proves nothing. What these tests are for is
    that the sha the script fetches and the sha it writes into the header are the
    same one, whatever it is.
    """
    for line in SYNC_SCRIPT.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key == name:
            return value.strip().strip('"')
    raise AssertionError(f"{SYNC_SCRIPT} declares no {name}")


UPSTREAM_REPOSITORY = pinned("UPSTREAM_REPOSITORY")
UPSTREAM_RAW_BASE = pinned("UPSTREAM_RAW_BASE")
UPSTREAM_COMMIT = pinned("UPSTREAM_COMMIT")
UPSTREAM_PATH = pinned("UPSTREAM_PATH")
EXPECTED_SOURCE_URL = f"{UPSTREAM_RAW_BASE}/{UPSTREAM_COMMIT}/{UPSTREAM_PATH}"
EXPECTED_HEADER = (
    f"# Vendored from {UPSTREAM_REPOSITORY}\n"
    f"# Pinned upstream commit: {UPSTREAM_COMMIT}\n"
    "# Changes belong upstream; update scripts/sync-bridge.sh when upgrading.\n"
)
UPSTREAM_SHEBANG = "#!/usr/bin/env python3\n"


class SyncBridgeTests(unittest.TestCase):
    def run_sync(self, root, path):
        return subprocess.run(
            [str(path)],
            cwd=root,
            env={"PATH": str(path.parent / "bin") + os.pathsep + os.environ["PATH"]},
            capture_output=True,
            text=True,
        )

    def test_sync_script_is_executable_for_the_ci_command(self):
        self.assertTrue(os.access(SYNC_SCRIPT, os.X_OK))

    def test_sync_fetches_the_pinned_file_and_writes_one_stable_header(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            script = root / "scripts" / "sync-bridge.sh"
            destination = root / VENDORED_BRIDGE
            fake_bin = root / "scripts" / "bin"
            capture = root / "curl-args"
            fake_curl = fake_bin / "curl"

            script.parent.mkdir()
            destination.parent.mkdir(parents=True)
            fake_bin.mkdir()
            script.write_bytes(SYNC_SCRIPT.read_bytes())
            script.chmod(script.stat().st_mode | stat.S_IXUSR)
            destination.write_text("stale vendored copy\n", encoding="utf-8")
            fake_curl.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$@\" > {capture}\n"
                "output=''\n"
                "previous=''\n"
                "for argument in \"$@\"; do\n"
                "    if [ \"$previous\" = '--output' ]; then output=\"$argument\"; fi\n"
                "    previous=\"$argument\"\n"
                "done\n"
                "printf '#!/usr/bin/env python3\\nindependent upstream payload\\n' > \"$output\"\n",
                encoding="utf-8",
            )
            fake_curl.chmod(fake_curl.stat().st_mode | stat.S_IXUSR)

            first = self.run_sync(root, script)

            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertEqual(
                capture.read_text(encoding="utf-8").splitlines()[-1],
                EXPECTED_SOURCE_URL,
            )
            self.assertEqual(
                destination.read_text(encoding="utf-8"),
                UPSTREAM_SHEBANG + EXPECTED_HEADER + "independent upstream payload\n",
            )

            second = self.run_sync(root, script)

            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertEqual(
                destination.read_text(encoding="utf-8"),
                UPSTREAM_SHEBANG + EXPECTED_HEADER + "independent upstream payload\n",
            )

    def test_ci_diff_fails_on_drift_and_passes_after_the_synced_copy_is_committed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            script = root / "scripts" / "sync-bridge.sh"
            destination = root / VENDORED_BRIDGE
            fake_bin = root / "scripts" / "bin"
            fake_curl = fake_bin / "curl"

            script.parent.mkdir()
            destination.parent.mkdir(parents=True)
            fake_bin.mkdir()
            script.write_bytes(SYNC_SCRIPT.read_bytes())
            script.chmod(script.stat().st_mode | stat.S_IXUSR)
            destination.write_text("committed stale copy\n", encoding="utf-8")
            fake_curl.write_text(
                "#!/bin/sh\n"
                "output=''\n"
                "previous=''\n"
                "for argument in \"$@\"; do\n"
                "    if [ \"$previous\" = '--output' ]; then output=\"$argument\"; fi\n"
                "    previous=\"$argument\"\n"
                "done\n"
                "printf '#!/usr/bin/env python3\\nindependent upstream payload\\n' > \"$output\"\n",
                encoding="utf-8",
            )
            fake_curl.chmod(fake_curl.stat().st_mode | stat.S_IXUSR)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Test Runner"],
                cwd=root,
                check=True,
            )
            subprocess.run(["git", "add", str(VENDORED_BRIDGE)], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "stale copy"], cwd=root, check=True)

            synced = self.run_sync(root, script)
            self.assertEqual(synced.returncode, 0, synced.stdout + synced.stderr)
            drift = subprocess.run(
                ["git", "diff", "--exit-code", "--", str(VENDORED_BRIDGE)],
                cwd=root,
                capture_output=True,
                text=True,
            )
            self.assertEqual(drift.returncode, 1, drift.stdout + drift.stderr)

            subprocess.run(["git", "add", str(VENDORED_BRIDGE)], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "synced copy"], cwd=root, check=True)
            synced_again = self.run_sync(root, script)
            self.assertEqual(
                synced_again.returncode,
                0,
                synced_again.stdout + synced_again.stderr,
            )
            self.assertEqual(
                subprocess.run(
                    ["git", "diff", "--exit-code", "--", str(VENDORED_BRIDGE)],
                    cwd=root,
                    capture_output=True,
                    text=True,
                ).returncode,
                0,
            )

    def test_sync_rejects_an_upstream_payload_without_a_shebang(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            script = root / "scripts" / "sync-bridge.sh"
            destination = root / VENDORED_BRIDGE
            fake_bin = root / "scripts" / "bin"
            fake_curl = fake_bin / "curl"

            script.parent.mkdir()
            destination.parent.mkdir(parents=True)
            fake_bin.mkdir()
            script.write_bytes(SYNC_SCRIPT.read_bytes())
            script.chmod(script.stat().st_mode | stat.S_IXUSR)
            destination.write_text("existing vendored copy\n", encoding="utf-8")
            fake_curl.write_text(
                "#!/bin/sh\n"
                "output=''\n"
                "previous=''\n"
                "for argument in \"$@\"; do\n"
                "    if [ \"$previous\" = '--output' ]; then output=\"$argument\"; fi\n"
                "    previous=\"$argument\"\n"
                "done\n"
                "printf 'not a shebang\\n' > \"$output\"\n",
                encoding="utf-8",
            )
            fake_curl.chmod(fake_curl.stat().st_mode | stat.S_IXUSR)

            result = self.run_sync(root, script)

            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(
                destination.read_text(encoding="utf-8"),
                "existing vendored copy\n",
            )

    def test_ci_syncs_before_failing_on_a_vendored_diff(self):
        workflow = (REPOSITORY_ROOT / ".github/workflows/ci.yml").read_text(
            encoding="utf-8"
        )
        sync_command = "scripts/sync-bridge.sh"
        diff_command = (
            "git diff --exit-code -- "
            "skills/crew/assets/review/scripts/tui_review_bridge.py"
        )

        self.assertIn(sync_command, workflow)
        self.assertIn(diff_command, workflow)
        self.assertLess(workflow.index(sync_command), workflow.index(diff_command))
