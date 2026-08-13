#!/usr/bin/env python3
"""Cut a release: bump the version in the plugin manifests, promote the
Unreleased changelog section, tag it, and publish a GitHub release.

Usage: scripts/release.py X.Y.Z [--dry-run]

Refuses to run unless the working tree is clean, on `main`, and up to date
with origin/main, and re-runs the same checks CI runs before doing anything
irreversible. Requires the `gh` CLI to be authenticated.
"""

import argparse
import json
import pathlib
import re
import subprocess
import sys
from datetime import date

ROOT = pathlib.Path(__file__).resolve().parent.parent
PLUGIN_MANIFEST = ROOT / ".claude-plugin" / "plugin.json"
MARKETPLACE = ROOT / ".claude-plugin" / "marketplace.json"
CHANGELOG = ROOT / "CHANGELOG.md"

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def run(*args, capture=False):
    return subprocess.run(
        args, cwd=ROOT, check=True, capture_output=capture, text=True
    )


def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def preflight(version):
    if not VERSION_RE.match(version):
        die(f"version must look like X.Y.Z, got {version!r}")

    branch = run("git", "rev-parse", "--abbrev-ref", "HEAD", capture=True).stdout.strip()
    if branch != "main":
        die(f"release from main, not {branch!r}")

    status = run("git", "status", "--porcelain", capture=True).stdout
    if status.strip():
        die("working tree is not clean; commit or stash first")

    run("git", "fetch", "origin", "main")
    local = run("git", "rev-parse", "HEAD", capture=True).stdout.strip()
    remote = run("git", "rev-parse", "origin/main", capture=True).stdout.strip()
    if local != remote:
        die("local main has diverged from origin/main; pull or push first")

    tag = f"v{version}"
    if run("git", "tag", "-l", tag, capture=True).stdout.strip():
        die(f"tag {tag} already exists")


def bump_json_version(path, version):
    """Set every "version" string field in the JSON tree, however deep."""
    data = json.loads(path.read_text())

    def walk(obj):
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key == "version" and isinstance(value, str):
                    obj[key] = version
                else:
                    walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(data)
    path.write_text(json.dumps(data, indent=2) + "\n")


def promote_changelog(version):
    text = CHANGELOG.read_text()
    if "## [Unreleased]" not in text:
        die("CHANGELOG.md has no [Unreleased] section")
    today = date.today().isoformat()
    text = text.replace(
        "## [Unreleased]",
        f"## [Unreleased]\n\n## [{version}] - {today}",
        1,
    )
    CHANGELOG.write_text(text)


def changelog_section(version):
    """Return the body written under the new version's heading, for release notes."""
    text = CHANGELOG.read_text()
    pattern = re.compile(
        rf"## \[{re.escape(version)}\].*?\n(.*?)(?=\n## \[|\Z)", re.S
    )
    match = pattern.search(text)
    return match.group(1).strip() if match else ""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="new version, e.g. 0.2.0 (no leading v)")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="run the preflight checks and print what would happen, change nothing",
    )
    args = parser.parse_args()

    preflight(args.version)

    if args.dry_run:
        print(f"[dry-run] would release v{args.version}: bump manifests, "
              "update CHANGELOG.md, tag, push, and publish a GitHub release.")
        return

    print("Running the test suite and plugin-tree validator (same as CI)...")
    run("python3", "scripts/validate_plugin_tree.py")
    run("python3", "-m", "unittest", "discover", "-s", "tests")

    bump_json_version(PLUGIN_MANIFEST, args.version)
    bump_json_version(MARKETPLACE, args.version)
    promote_changelog(args.version)

    run("git", "add", str(PLUGIN_MANIFEST), str(MARKETPLACE), str(CHANGELOG))
    run("git", "commit", "-m", f"chore: release v{args.version}")

    tag = f"v{args.version}"
    run("git", "tag", tag)
    run("git", "push", "origin", "main")
    run("git", "push", "origin", tag)

    notes = changelog_section(args.version)
    notes_file = ROOT / f".release-notes-{args.version}.txt"
    notes_file.write_text(notes + "\n")
    try:
        run("gh", "release", "create", tag, "--title", tag, "--notes-file", str(notes_file))
    finally:
        notes_file.unlink(missing_ok=True)

    print(f"Released {tag}.")


if __name__ == "__main__":
    main()
