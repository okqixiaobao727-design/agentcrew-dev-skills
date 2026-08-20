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
