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

A machine may hand the run to a command of its own: a `[test] runner` in `agentcrew.toml`, or in
the uncommitted `agentcrew.local.toml` laid over it (ADR-0029), is started with this script's
arguments verbatim and its exit status is this script's. Every caller keeps naming this script —
the gate, CI, the agent following AGENTS.md — and the machine decides what runs it. The runner
gets back here with `--no-delegate`, which runs the suites where it is invoked; a local runner
that forgets it is stopped after one hand-over rather than left to fork forever.

The overlay is read from the repository's *main* working tree rather than from beside the
checkout under test, because it is a fact about the machine and untracked: the worktrees a Crew
run gates its base in and works its tickets in never receive it from `git worktree add`, and it
is precisely those runs that a machine configures a runner for.
"""

import argparse
import collections
import concurrent.futures
import importlib.util
import os
import pathlib
import shlex
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
ASSET_SUITES = "skills/*/assets/**/tests"
ROOT_SUITE = "root"
ROOT_REQUIREMENT = "aiohttp"
TEST_REQUIREMENTS_FILE = "requirements-test.txt"
CONFIG_NAME = "agentcrew.toml"
LOCAL_CONFIG_NAME = "agentcrew.local.toml"
TEST_SECTION = "test"
RUNNER_KEY = "runner"
DELEGATED_ENV = "AGENTCREW_TEST_DELEGATED"


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


def config_document(path):
    """One TOML document, or an empty one where the file is not there."""
    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"{path} is unreadable: {error}") from error


def merged_config(base, overlay):
    """`overlay` laid over `base`: tables merge recursively, any other value replaces."""
    merged = dict(base)
    for key, value in overlay.items():
        below = merged.get(key)
        if isinstance(value, dict) and isinstance(below, dict):
            merged[key] = merged_config(below, value)
        else:
            merged[key] = value
    return merged


def overlay_root(root):
    """The working tree this machine's overlay sits in: for a worktree, the repository's main one.

    `agentcrew.local.toml` states a fact about the *machine*, and it is untracked — so `git
    worktree add` never carries it into the worktree a Crew run gates its base in, or the one a
    child works its ticket in. Read from beside the checkout, this machine's runner would reach
    the run started at the repository root and none of the runs that matter. Read from the
    repository's main working tree, it is one file per clone that every checkout of that clone on
    this machine sees (ADR-0029).

    A directory git knows nothing about — a throwaway tree, an unpacked release — keeps its own.
    """
    try:
        found = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return root
    if not found:
        return root
    # Relative in the main working tree, absolute in a linked one; the tree is its parent either
    # way. `.git` beside the checkout is what makes the main tree resolve back to itself.
    common = pathlib.Path(found)
    return (common if common.is_absolute() else root / common).parent


def configured_runner(root):
    """The `[test] runner` argv this machine hands the run to, or None where none is configured.

    Read the way the Driver reads every other project decision: the committed `agentcrew.toml`
    with the machine's `agentcrew.local.toml` merged over it. The committed file is read from the
    tree under test, because it is that tree's own content and moves with its branch; the overlay
    from `overlay_root`, because it is the machine's and one worktree of a repository must not
    have to be told what another already knows.

    Raises ValueError for a document that cannot be read, a `[test]` that is not a table, or a
    runner that is not a non-empty list of non-empty strings — a runner half-configured must stop
    the run, not silently run the suites here instead.
    """
    config = merged_config(
        config_document(root / CONFIG_NAME),
        config_document(overlay_root(root) / LOCAL_CONFIG_NAME),
    )
    section = config.get(TEST_SECTION)
    if section is None:
        return None
    if not isinstance(section, dict):
        # Not read past: `test = "..."` is a machine asking for its runner in a shape this script
        # cannot honour, and running the suites here is the one answer the key exists to prevent.
        raise ValueError(
            f"[{TEST_SECTION}] is not a table — write the runner as a `[{TEST_SECTION}]` section"
            f" with a `{RUNNER_KEY}` argv list under it, or remove the key to run the suites here"
        )
    runner = section.get(RUNNER_KEY)
    if runner is None:
        return None
    if (
        not isinstance(runner, list)
        or not runner
        or any(not isinstance(item, str) or not item.strip() for item in runner)
    ):
        raise ValueError(
            "[test] runner is not a non-empty list of non-empty strings — configure each command"
            " argument as one string, or remove the key to run the suites here"
        )
    return list(runner)


def delegate(runner, root, argv):
    """Start the configured runner with this script's arguments; return its exit status.

    The runner is marked as having been handed the run, in its environment, so that a runner that
    comes back to this script without `--no-delegate` runs the suites rather than handing them
    over again — see `main`. The mark travels as far as the runner's own environment does, which
    is the local process tree; a runner that crosses to another machine takes `--no-delegate`
    with it, exactly as ADR-0029 says, because nothing else can travel that far.
    """
    rendered = shlex.join(runner)
    report(f"handing the run to `{rendered}` ([test] runner); --no-delegate runs it here")
    try:
        return subprocess.run(
            [*runner, *argv], cwd=str(root), env={**os.environ, DELEGATED_ENV: "1"}
        ).returncode
    except OSError as error:
        return die(f"[test] runner `{rendered}` could not be started — {error}")


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
    parser.add_argument(
        "--no-delegate",
        action="store_true",
        help="run the suites here even where a [test] runner is configured "
        "(what that runner passes when it comes back to this script)",
    )
    args = parser.parse_args(argv)
    if args.jobs is not None and args.jobs < 1:
        return die(f"--jobs takes a positive number of worker processes, not {args.jobs}")

    root = args.root.resolve()
    if args.no_delegate:
        pass
    elif os.environ.get(DELEGATED_ENV):
        # The runner came back here without saying so. Handing the run over again would hand it to
        # the same command forever, so this is where the hand-over stops: the run is already the
        # runner's own, and running the suites here is running them where it put them.
        report(
            f"the [test] runner came back without --no-delegate ({DELEGATED_ENV} is set);"
            " running the suites here rather than handing them over again"
        )
    else:
        try:
            runner = configured_runner(root)
        except ValueError as error:
            return die(str(error))
        if runner is not None:
            return delegate(runner, root, sys.argv[1:] if argv is None else list(argv))
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
