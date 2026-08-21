---
status: accepted
---

# One validation entry point, with focused and full intents

The automated gate is two commands, and the second one is the problem:
`python3 -m unittest discover -s tests -v`. The root `tests/test_asset_suites.py` uses a
`load_tests` hook to pull every asset suite under `skills/*/assets/**/tests` into that one
discovery, so any invocation of the documented entry point runs everything, serially. There is no
documented way to run less. Three measurements decided this design.

- **The suite outgrew its entry point.** v0.1.0: 59 tests in 4.6 seconds. v0.4.1: 566 tests in
  397 seconds. v0.8.3: 869 tests in 617 seconds. The crew/65 snapshot: 940 tests in 872 seconds.
  CONTRIBUTING.md still said the gate "takes a few seconds"; the entry point's cost grew two
  hundredfold while its interface — all or nothing — never changed. Every ticket pays the full
  price for every change, however local.
- **The time is waiting, not computing.** The driver suite alone — 154 tests — measured 641
  seconds at 36% CPU: real subprocesses sleeping toward timeouts, not assertions running. One
  eager-evaluation bug (`test_driver.py` passing `ended(...)` as an assertion *message*, so the
  drain ran on passing paths too) accounted for roughly 220 seconds across 11 call sites. Waiting
  dominates, so the suites are near-perfectly parallel across processes — and near-immune to
  Python-version differences.
- **CI multiplied the cost by four.** The workflow ran the identical full discovery on Python
  3.11, 3.12, 3.13 and 3.14. These tests exercise `git`, `tmux` stubs and process lifecycles, not
  version-sensitive language surface; four identical runs bought almost nothing over two.

## Decision

**One script is the whole of validation.** A stdlib-only `scripts/test.py` owns the suite
inventory (the same `skills/*/assets/**/tests` glob), the selection semantics, and per-suite
timing output. CONTRIBUTING.md, `.github/workflows/ci.yml` and `scripts/release.py` all invoke
it and nothing else; the raw `unittest discover` incantation stops being a documented interface.
The guard tests keep their jobs: every discovered suite must contribute tests, and a suite that
loads nothing fails the run.

**Selection is declared, never inferred.** `scripts/test.py --asset driver` runs one asset's
suite; naming no asset runs everything. The script never derives scope from `git diff`: an
inference that guesses wrong skips tests silently, and nobody can later explain why a regression
was never run. The caller — a person or an agent's contract — states what it touched.

**The full gate does not degrade.** CI, release, and the end of every ticket still run the entire
inventory. Focused runs exist to make the development loop cheap, not to make the gate porous.

**The CI matrix keeps its endpoints.** Full discovery runs on 3.11 and 3.14 only. A failure
unique to an interior version, with both endpoints green, is not a risk these process-lifecycle
tests can meaningfully surface; if one ever appears, the matrix row is one line to restore.

**The contract names the commands.** CONTRIBUTING.md and AGENTS.md state the rule in the
imperative with the exact invocations: while working, run the suite of the asset you changed;
run the full gate exactly once, at the end. Abstract advice ("prefer focused tests") measurably
failed — agents fell back to full discovery repeatedly within a single ticket; a contract that
names one command per situation leaves nothing to interpret.

## Consequences

A change to one asset costs that asset's suite during development — seconds to a few minutes —
instead of ~15 minutes per iteration. The gate's cost still grows linearly with the whole suite,
but it is paid once per ticket and twice per PR instead of four times. Suite-level timing output
turns the next slowdown into a number with a name on it, instead of a feeling. The deferred
follow-ups this decision unblocks are tracked in the issues that cite it: the shared
process-harness consolidation is re-evaluated only after the eager-wait fix resets the timing
baseline, and per-test fixture setup cost is optimized only as its own measured change.

## Amendment: the unit of parallel work is a shard, not a suite

The decision above made the gate cost its slowest suite instead of the sum of them all. Three
tickets later — #114's eager `ended(...)` evaluation, #115's one validation entry point, #116's
harness module — that succeeded so completely that it became the next problem: at 984 tests, the
gate measured 417.7s and the driver suite alone measured 417.5s. `total == driver`, exactly. The
driver suite *was* the gate.

Nothing was left to delete inside it. No test took twenty seconds; what remained was a flat floor
of about two seconds per test, 158 times over, and that floor is real work — building the
fixture's git origin, waiting for a driver to reach a wave table, waiting for dispatch to verify a
child. Meanwhile the suite used 0.65 of the machine's ten cores. "The time is waiting, not
computing" was still true; the decision had just spent it one level too high up.

**A work item is now one shard of one suite.** Everything the decision above gives as the reason
for one worker per item is kept exactly: a worker interpreter each, replaced after one item, with
output captured at the file descriptors. What changed is only how finely the work is cut.

**Neither the shard count nor the worker count is a flag.** Both derive from `os.cpu_count()`:
workers is the core count, and the shard count is the largest power of two that does not exceed
it — eight on a ten-core machine, four on a four-core CI runner. A `--shards` knob would have
contradicted this decision's minimal interface and needed documenting in two places, and no
measured setting beat the derived one. `--jobs` keeps its name and its role as the escape hatch,
but its meaning widens from "how many suites at once" to "how many worker processes at once";
`--jobs 1` derives one shard, so it still runs the whole inventory one item at a time. It bounds
the workers downward only: asking for more than the machine has cores is capped, and said out
loud, because these tests wait on absolute wall-clock timeouts and oversubscribing them does not
slow the gate down so much as start failing tests that would otherwise pass. Going past the core
count waits on making those timeouts scale with concurrency, which is its own change.

**The split happens in the child, by test id.** The parent must never import a suite's modules to
divide them: four asset suites ship a `stub_claude.py`, and a parent that imported one would be
the first importer of all of them — the exact collision one worker per item exists to prevent. So
each child discovers its own suite, sorts by test id and takes `[index::count]`. Round robin over
a sorted order, not a contiguous block, because the expensive tests cluster inside a class.

**The empty-suite guard moved up a level.** An empty *shard* is ordinary — a thirty-test suite cut
eight ways has some — so the guard is now evaluated per suite, over the sum of its shards. Zero
across all of a suite's shards is still a failure. Reporting keeps one line per suite: the count
is the suite's whole count, and the time is its slowest shard, which is the number that says what
removing the imbalance would buy.

Two things were measured and rejected. Scheduling the slowest suite's shards first made the gate
worse (148.3s against 131.0s): eight heavy shards starting together contend. Raising the count to
sixteen shards halved the driver's slowest shard but pushed the gate back to 142.6s on 112 work
items' overhead, and produced a flake — the harness's absolute wall-clock timeouts, not the
sharding, are what bound concurrency from here.

**The deferred per-test fixture setup follow-up is closed, not done.** This is its measurement:
`Fixture()` costs 0.152s, 24s across the driver suite, 6% of it — about five seconds once sharded.
That is not worth a ticket.

Measured on a ten-core Darwin machine, every run green:

| what             | before          | after           |
| ---------------- | --------------- | --------------- |
| `--asset driver` | 158 tests, 402s | 158 tests, 71.1s |
| the full gate    | 984 tests, 417.7s | 989 tests, 135.2s |
