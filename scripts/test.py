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

The gate runs its work in worker interpreters at once, so its total is the slowest piece of work
rather than the sum of them all. That piece is a *shard* of a suite, not a whole suite: one suite
grew into being the whole gate, so its 158 tests are split across interpreters too. `--jobs 1`
runs one work item at a time.
"""

import argparse
import collections
import concurrent.futures
import importlib.util
import os
import pathlib
import sys
import tempfile
import time
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
ASSET_SUITES = "skills/*/assets/**/tests"
ROOT_SUITE = "root"
ROOT_REQUIREMENT = "aiohttp"
TEST_REQUIREMENTS_FILE = "requirements-test.txt"


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


def shards_for(workers):
    """How many pieces to cut each suite into, given how many workers will carry them.

    Doubling the shards halves the longest one, and each doubling costs one more interpreter
    start and one more discovery walk per suite. Past the worker count the halving stops paying:
    the extra shards only queue behind the others, so the critical path is unchanged while the
    overhead is not. So: double while a doubling still fits in the workers, and stop.

    Measured at the two ends this repository actually runs on — a ten-core machine and a
    four-core CI runner — the rule lands on the two counts that measured fastest, 8 and 4, and
    it is why sixteen shards on ten workers measured *slower* than eight. `--jobs 1` falls out
    of the same rule as one shard, which is what makes the escape hatch a real serialisation.
    """
    count = 1
    while count * 2 <= workers:
        count *= 2
    return count


def flatten(tests):
    """Every individual test in a discovered suite, however deeply the loader nested it."""
    for item in tests:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def shard_of(tests, index, shards):
    """This shard's share of a discovered suite: every `shards`th test by id, starting at `index`.

    Round-robin over a sorted list rather than a contiguous block. Sorted, so that the split is
    the same in every worker without any of them agreeing on anything; round-robin, because the
    expensive tests cluster inside a class — `AdoptionTests` averages eight seconds a test — and
    a contiguous block would hand one shard the whole cluster and leave the rest idle.

    One shard is the whole suite, in discovery order rather than sorted: `--jobs 1` is the hatch
    for bisecting a test that only fails beside its neighbours, so it must not quietly reorder
    the neighbours.
    """
    if shards == 1:
        return tests
    ordered = sorted(flatten(tests), key=lambda test: test.id())
    return unittest.TestSuite(ordered[index::shards])


def run_shard(directory, index, shards):
    """Run one shard of one suite.

    Returns whether it passed, how many tests it ran, and how long that took.

    An empty shard is ordinary — a thirty-test suite cut eight ways has some — so it is not
    judged here. Whether a *suite* found any tests at all is decided by the caller, over the sum
    of that suite's shards, because only the caller can see all of them.
    """
    started = time.monotonic()
    tests = shard_of(discover(directory), index, shards)
    count = tests.countTestCases()
    if not count:
        return True, 0, time.monotonic() - started
    result = unittest.TextTestRunner(stream=sys.stderr).run(tests)
    return result.wasSuccessful(), count, time.monotonic() - started


def finish_suite(name, directory, root, parts):
    """Report one suite once all of its shards have landed; return whether it passed and its size.

    The count is the whole suite's, summed, and the time is its *slowest shard* — the figure that
    says what removing the imbalance would buy, where the sum would say nothing at all. The line
    keeps the shape it had before there were shards, because that is what the caller reads.
    """
    count = sum(size for _, size, _ in parts)
    if not count:
        # A directory that loads nothing is a suite that stopped running without saying so.
        report(f"{name}: no tests found in {directory.relative_to(root)}")
        return False, 0
    report(f"{name}: {count} tests in {max(seconds for _, _, seconds in parts):.1f}s")
    return all(passed for passed, _, _ in parts), count


def run_shard_apart(entry):
    """One shard in a worker interpreter; return the suite it belongs to, its result and its output.

    Suites share module names — four asset suites ship a `stub_claude.py` — so the first one
    to import it would leave it in `sys.modules` for the next, which would then be testing
    against its neighbour's copy. One worker per work item is what keeps that from happening,
    and it is also why the split itself happens here rather than in the parent: a parent that
    imported a suite's modules to divide them would be the very first importer of all of them.

    The output is captured at the file descriptors rather than at `sys.stderr`, because a suite's
    own subprocesses inherit the descriptors and would otherwise write straight past the capture,
    into the middle of whatever another worker is writing.
    """
    name, directory, index, shards = entry
    # Replayed rather than decoded strictly: a suite's subprocesses write whatever a branch name
    # or a terminal held, and one undecodable byte must cost that suite's legibility, not the run.
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as held:
        sys.stdout.flush()
        sys.stderr.flush()
        saved = os.dup(1), os.dup(2)
        os.dup2(held.fileno(), 1)
        os.dup2(held.fileno(), 2)
        try:
            outcome = run_shard(directory, index, shards)
        finally:
            sys.stdout.flush()
            sys.stderr.flush()
            os.dup2(saved[0], 1)
            os.dup2(saved[1], 2)
            os.close(saved[0])
            os.close(saved[1])
        held.seek(0)
        return name, outcome, held.read()


def run_together(work, directories, root, workers):
    """Run the work items in worker interpreters; return each suite's (passed, size) pair.

    ADR-0016 measured the suites near-perfectly parallel across processes: their time is spent
    waiting on subprocesses rather than computing. Sharding spends the rest of that headroom, so
    the gate now costs the slowest *shard* rather than the slowest suite. Each shard's output is
    written whole as it lands, and a suite is reported once, when its last shard has.

    The work is submitted in inventory order and never reordered. Putting the slowest suite's
    shards first was measured and is worse: eight heavy shards starting together contend, and
    each of them slows down. The inventory order already interleaves the load.

    `workers` bounds how many run at once, not how many interpreters they run in: one worker
    takes one item and is then replaced, so running them one at a time still isolates them.
    """
    outstanding = collections.Counter(name for name, *_ in work)
    landed = collections.defaultdict(list)
    results = []
    with concurrent.futures.ProcessPoolExecutor(
        max_workers=min(workers, len(work)), max_tasks_per_child=1
    ) as pool:
        pending = [pool.submit(run_shard_apart, item) for item in work]
        for done in concurrent.futures.as_completed(pending):
            name, outcome, output = done.result()
            sys.stderr.write(output)
            landed[name].append(outcome)
            outstanding[name] -= 1
            if not outstanding[name]:
                results.append(finish_suite(name, directories[name], root, landed[name]))
    return results


def die(message):
    print(f"error: {message}", file=sys.stderr)
    return 2


def missing_root_requirement(chosen):
    if not any(name == ROOT_SUITE for name, _ in chosen):
        return None
    if importlib.util.find_spec(ROOT_REQUIREMENT) is not None:
        return None
    return (
        f"the {ROOT_SUITE} suite requires Python package {ROOT_REQUIREMENT!r}; "
        f"install test dependencies with: python3 -m pip install -r {TEST_REQUIREMENTS_FILE}"
    )


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
        help="run this many worker processes at once, up to one per core "
        "(default: one per core; 1 runs the work one item after another)",
    )
    parser.add_argument(
        "--root",
        type=pathlib.Path,
        default=ROOT,
        help="the repository to test (default: the one this script ships in)",
    )
    args = parser.parse_args(argv)
    if args.jobs is not None and args.jobs < 1:
        return die(f"--jobs takes a positive number of worker processes, not {args.jobs}")

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
    missing_requirement = missing_root_requirement(chosen)
    if missing_requirement:
        return die(missing_requirement)

    # Bounded by the cores the machine has, whatever was asked for: these tests measure real
    # processes against absolute wall-clock timeouts, so oversubscribing does not merely slow the
    # gate down, it starts failing tests that would otherwise pass. Going past the core count
    # waits on making those timeouts scale with concurrency, which is its own change (ADR-0016).
    cores = os.cpu_count() or 1
    workers = min(args.jobs, cores) if args.jobs else cores
    if args.jobs and args.jobs > cores:
        # Said out loud rather than applied quietly, so a run that ignores the flag says why.
        report(f"--jobs {args.jobs} is more than this machine's {cores} cores; running {cores}")
    shards = shards_for(workers)
    work = [
        (name, directory, index, shards)
        for name, directory in chosen
        for index in range(shards)
    ]

    started = time.monotonic()
    if len(work) > 1:
        results = run_together(work, dict(chosen), root, workers)
    else:
        name, directory, index, count = work[0]
        results = [finish_suite(name, directory, root, [run_shard(directory, index, count)])]
    if len(results) > 1:
        total = sum(count for _, count in results)
        report(f"total: {total} tests in {time.monotonic() - started:.1f}s")
    return 0 if all(passed for passed, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
