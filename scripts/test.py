#!/usr/bin/env python3
"""Run this repository's tests: one asset's suite while you work, every suite as the gate.

The suites are not all in one place. `tests/` covers the repository itself; a skill asset keeps
its tests beside it, under `skills/*/assets/<asset>/tests`, because the stub PATH and fixture
repository they need belong with the script they stand in for. This script owns that inventory,
so every caller — CONTRIBUTING.md, CI, `scripts/release.py`, an agent following AGENTS.md —
names an intent instead of repeating an incantation (ADR-0016).

Usage:
    python3 scripts/test.py                  # the full gate: every suite
    python3 scripts/test.py --asset driver   # one suite, while you are working on that asset

Selection is declared, never inferred: nothing here reads `git diff` to guess what changed, because
an inference that guesses wrong skips tests silently. The caller states what it touched. Each
suite's size and wall time go to stderr, so the next slowdown arrives as a number with a name on it.

The gate runs its suites at once, one worker interpreter each, so its total is the slowest
suite's wall time rather than the sum of them all. `--jobs 1` runs them one at a time.
"""

import argparse
import concurrent.futures
import os
import pathlib
import sys
import tempfile
import time
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
ASSET_SUITES = "skills/*/assets/**/tests"
ROOT_SUITE = "root"


def suites(root):
    """Every suite in the inventory, as (name, directory) pairs: the repository's, then the assets'.

    An asset suite answers to the name of the asset it sits beside — `.../driver/tests` is
    `driver` — which is what a caller knows it changed. The repository's own suite is listed
    whether or not it is there: a `tests/` that went missing has to fail the gate, not quietly
    shrink the inventory the gate is measured against.
    """
    found = [(ROOT_SUITE, root / "tests")]
    found.extend(
        (directory.parent.name, directory)
        for directory in sorted(root.glob(ASSET_SUITES))
        if directory.is_dir()
    )
    return found


def discover(directory):
    """That directory's tests, discovered with the directory itself as the import root.

    Deliberately a throwaway loader: `discover` leaves its `top_level_dir` on the loader it runs
    on, and on most Python versions never puts it back. Reusing one loader across the inventory
    would repoint every later walk at whichever directory was walked first.
    """
    return unittest.TestLoader().discover(str(directory), top_level_dir=str(directory))


def select(inventory, asset):
    """The suites to run, and the reason there are none — naming no asset selects them all."""
    if asset is None:
        return inventory, None
    chosen = [entry for entry in inventory if entry[0] == asset]
    if not chosen:
        known = ", ".join(name for name, _ in inventory)
        return [], f"unknown asset {asset!r}; the suites are: {known}"
    if len(chosen) > 1:
        paths = ", ".join(str(directory) for _, directory in chosen)
        return [], f"{asset!r} names more than one suite: {paths}"
    return chosen, None


def report(message):
    """Say something about the run itself, on the stream the test output already uses."""
    print(f"[test] {message}", file=sys.stderr)


def run_suite(name, directory, root):
    """Run one suite; return whether it passed and how many tests it held."""
    tests = discover(directory)
    count = tests.countTestCases()
    if not count:
        # A directory that loads nothing is a suite that stopped running without saying so.
        report(f"{name}: no tests found in {directory.relative_to(root)}")
        return False, 0
    started = time.monotonic()
    result = unittest.TextTestRunner(stream=sys.stderr).run(tests)
    report(f"{name}: {count} tests in {time.monotonic() - started:.1f}s")
    return result.wasSuccessful(), count


def run_suite_apart(entry):
    """One suite in a worker interpreter; return whether it passed, its size, and its output.

    Suites share module names — four asset suites ship a `stub_claude.py` — so the first one
    to import it would leave it in `sys.modules` for the next, which would then be testing
    against its neighbour's copy. One worker per suite is what keeps that from happening.

    The output is captured at the file descriptors rather than at `sys.stderr`, because a suite's
    own subprocesses inherit the descriptors and would otherwise write straight past the capture,
    into the middle of whatever another worker is writing.
    """
    name, directory, root = entry
    # Replayed rather than decoded strictly: a suite's subprocesses write whatever a branch name
    # or a terminal held, and one undecodable byte must cost that suite's legibility, not the run.
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as held:
        sys.stdout.flush()
        sys.stderr.flush()
        saved = os.dup(1), os.dup(2)
        os.dup2(held.fileno(), 1)
        os.dup2(held.fileno(), 2)
        try:
            passed, count = run_suite(name, directory, root)
        finally:
            sys.stdout.flush()
            sys.stderr.flush()
            os.dup2(saved[0], 1)
            os.dup2(saved[1], 2)
            os.close(saved[0])
            os.close(saved[1])
        held.seek(0)
        return passed, count, held.read()


def run_together(chosen, root, jobs):
    """Run the suites in worker interpreters; return their (passed, size) pairs.

    ADR-0016 measured the suites near-perfectly parallel across processes: their time is spent
    waiting on subprocesses rather than computing, so the gate costs its slowest suite rather
    than the sum of them all. Each suite's output is written whole, as that suite lands.

    `jobs` bounds how many run at once, not how many interpreters they run in: one worker takes
    one suite and is then replaced, so running them one at a time still isolates them.
    """
    work = [(name, directory, root) for name, directory in chosen]
    results = []
    with concurrent.futures.ProcessPoolExecutor(
        max_workers=jobs or len(work), max_tasks_per_child=1
    ) as pool:
        pending = [pool.submit(run_suite_apart, item) for item in work]
        for done in concurrent.futures.as_completed(pending):
            passed, count, output = done.result()
            sys.stderr.write(output)
            results.append((passed, count))
    return results


def die(message):
    print(f"error: {message}", file=sys.stderr)
    return 2


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Run every test suite, or the one suite belonging to a named asset.",
    )
    parser.add_argument(
        "--asset",
        help=f"run only this suite: an asset directory's name, or {ROOT_SUITE!r} for tests/",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        help="run this many suites at once (default: all of them; 1 runs them one after another)",
    )
    parser.add_argument(
        "--root",
        type=pathlib.Path,
        default=ROOT,
        help="the repository to test (default: the one this script ships in)",
    )
    args = parser.parse_args(argv)
    if args.jobs is not None and args.jobs < 1:
        return die(f"--jobs takes a positive number of suites at once, not {args.jobs}")

    root = args.root.resolve()
    inventory = suites(root)
    missing = [str(directory) for _, directory in inventory if not directory.is_dir()]
    if missing:
        return die(f"the inventory names a directory that is not there: {', '.join(missing)}")
    if not [name for name, _ in inventory if name != ROOT_SUITE]:
        return die(f"no tests directory under {root / ASSET_SUITES}")

    chosen, unselectable = select(inventory, args.asset)
    if unselectable:
        return die(unselectable)

    started = time.monotonic()
    if len(chosen) > 1:
        results = run_together(chosen, root, args.jobs)
    else:
        results = [run_suite(name, directory, root) for name, directory in chosen]
    if len(results) > 1:
        total = sum(count for _, count in results)
        report(f"total: {total} tests in {time.monotonic() - started:.1f}s")
    return 0 if all(passed for passed, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
