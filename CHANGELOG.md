# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

The two defects the first `/crew` run of the fixed code exposed in its own
records (#26, #27): a run now shows a review while it is running, and bills a
child that worked in a subdirectory of its own worktree.

### Added
- A `review` subcommand on the machine-log CLI — `--ticket`, `--lane` and
  `--state` required, `--detail` optional — validating `--state` against the
  closed set `running`/`returned` the way the other event subcommands validate
  theirs (#26).
- Both review bridges write the run's `review` event themselves, one `running`
  line on entry and one `returned` on every exit path they control, including
  an interrupted or errored review and a resumed round two. The bridge writes
  it because it is the only party that deterministically knows a review both
  started and ended, which keeps the dashboard's inputs script-written and
  zero-token (ADR-0001). A run with no log configured writes nothing and
  reviews normally, and a logging failure never changes a review's exit status
  or the JSON its child reads (#26).

### Fixed
- The cost pass compared a transcript's working directory to the ticket's
  worktree by equality, so a child that changed directory inside its own
  worktree failed an identity check against itself and lost its whole cost row
  — three tickets of the #18 run were reported `not measured` for this reason.
  A cwd at or below the worktree is now the same identity, compared by path
  component after resolving both sides, so a sibling sharing a prefix and a
  parent directory both stay outside and stay diagnosed. Both lanes take the
  new rule: the Claude reader's per-record check and the Codex reader's
  `session_meta` check (#27).
- The `review` event was documented and rendered but nothing emitted it, so a
  ticket sitting in a multi-minute review was drawn as a plain `running` row,
  indistinguishable from one still writing code. The "nothing writes it yet"
  notes in `docs/machine-log.md` and `docs/monitor-dashboard.md` are retired
  (#26).

## [0.3.5] - 2026-08-14

The defects the first real `/crew` run exposed, fixed together (#18): a run now
records what it cost, shows itself in one place, and refuses to trust a path it
cannot resolve.

### Added
- A `session-cost` event and a run cost rollup: at completion the monitor reads
  each launched child's transcripts, appends one event per child — ticket,
  executor, model, session, four disjoint token counters and their total — and
  prints the rollup the report quotes. Codex figures are converted to the same
  four counters as Claude's so the totals add across lanes. Nothing unreadable
  is billed: a missing, ambiguous or usage-silent transcript leaves a diagnosed
  event and a `--` row, and the log refuses a record carrying both figures and
  a diagnosis (#23).
- One `crew-dashboard` tmux window per run, owned by the monitor's new `window`
  subcommand: it records the window id, recreates it when the operator closed
  it or the run is resumed, and never closes it. Check, creation and record are
  one critical section under a lock, so overlapping callers still leave one
  window (#21).
- Each child's `launch` event, written by dispatch through the log's own
  writer. The launched set is what wave advancement and the dashboard read, and
  nothing wrote it before — the dashboard was permanently empty (#19).
- The Ticket state vocabulary, in the glossary, and the machine log's `review`
  event shape (#21).
- [ADR-0007](docs/adr/0007-worktree-paths-are-absolute-at-the-boundary-and-compared-by-realpath.md),
  recording the two path invariants — absolute at the boundary, compared by
  realpath — with the first-run defects as their evidence (#24).
- [`docs/cost-baseline.md`](docs/cost-baseline.md): the redacted forensic audit
  of one predecessor `/orchestrate` run, so the figures ADR-0001 decided on
  have a checkable source in the repo and a future run's cost record has
  something to be graded against (#25).

### Changed
- `dashboard` takes the run directory instead of a wave and a worktree list,
  and draws every ticket of every wave from the approved wave table joined with
  the machine log — unlaunched ones as `pending`. Colour is drawn only when a
  terminal is watching, and at end of run it draws its last frame and stops
  (#21).
- Child windows are created detached, so a launching wave leaves the operator's
  focus where it was rather than dragging it through every new child (#19).
- The crew skill's report step runs the cost pass and carries the coordinator's
  own token row beside the run rollup, so the judgment-only design (ADR-0001)
  is checkable from a run's artifacts alone; the resume reference gains a step
  that re-runs the idempotent dashboard window command (#24).
- `/route`'s spec-only overlay is renamed `references/to-tickets+route.md` for
  what it actually is: the rules that ride along on a user-typed `to-tickets`
  run.

### Fixed
- Worktree identity is what a path resolves to, in the monitor, the wake
  monitor and the red-line guard alike. macOS reaches the same directory as
  `/tmp` and `/private/tmp`, so comparing two spellings as strings called a
  live child `vanished` — a false toast, a false wake, and a row nobody could
  act on (#20).
- The machine-log hook embeds absolute paths for both the script and the log. A
  relative spelling resolved against the child's worktree at fire time, so the
  escalation landed in a file nobody reads (#20).
- The red-line guard denies and explains itself when it cannot say where its
  worktree is, instead of standing its own checks down. An unsubstituted
  worktree placeholder disabled the "git runs against this worktree only" check
  outright, and a relative `rm -rf` target was skipped for want of a leading
  slash — a safety check that quietly disables itself is worse than none (#20).
- Dispatch resolves `--out-dir` at the boundary, making "the artifact list is
  absolute" an invariant rather than an error case. The first crew run lost its
  whole first wave to a relative one, and the operator saw only a verification
  timeout (#19).
- Live state comes from each lane's own source: the agents list for a Claude
  child, the bridge state file for a Codex one, which appears in no agents list
  — reading only the agents list drew every Codex ticket of a mixed run
  `vanished` (#21).
- The review thread carries the approval and sandbox policy into the app-server
  thread (#22).

## [0.3.0] - 2026-08-14

### Changed
- Spec-only `/route` no longer drives `/mattpocock-skills:to-tickets` itself: it
  prints the command for the user to type, and that user-typed run cuts,
  confirms, and publishes with `/route`'s rules riding along as additions — the
  skill's upstream gate against model invocation is honoured, not worked around
  (ADR-0006).
- `/route`'s body slims to a thin router. The judgment core — classification
  tests, table shape, `## Routing` template — moves to `references/classify.md`,
  force-read at the point of use, superseding ADR-0005's body-structure bullet;
  the cutting overlay is absorbed into `references/spec-only.md`, which ends
  with a verification pass over the published tickets.
- The plugin-tree validator checks relative links in every skill's
  `references/*.md`, not just its `SKILL.md`, so the moved judgment core keeps
  its broken-link protection.

### Removed
- `trackers.md`'s **publish** operation: publication belongs to `to-tickets`,
  and `/route` reads, edits, and marks.

### Fixed
- `docs/dogfooding-run.md` recorded a wrong root cause ("`to-tickets` may not
  be a slash command"); the real cause is the skill's deliberate
  `disable-model-invocation` gate, which keeps its description out of model
  context entirely.

## [0.2.1] - 2026-08-14

### Fixed
- Test collection died before running anything on every Python that does not
  restore `loader.top_level_dir` after a nested `discover`: loading the asset
  suites repointed the walk that loaded them. Each asset suite now loads on its
  own loader.
- CI installs `aiohttp`, without which the review bridge's 44 tests error on
  import rather than run.

## [0.2.0] - 2026-08-14

Crew v2: the coordinator is reduced to judgment, and everything mechanical
moves into scripts, hooks and a machine-readable log (#4).

### Added
- A machine log, written by scripts and hooks, so the coordinator reads a run's
  state instead of scraping tmux panes (#7).
- A dispatch renderer that composes and launches each child agent's first turn
  from its ticket, model and worktree (#6).
- The monitor's operator dashboard, desktop toasts and receipt check (#8).
- A merge driver that lands a finished wave's branches without waking the
  coordinator (#9).
- Wave auto-advance: a run moves itself from one wave to the next (#10).
- The `code-review-graph` MCP server, registered for both Claude Code and
  Codex.
- ADRs 0001–0005 and the Crew v2 and `/route` glossary entries.

### Changed
- `/route` applies the coordinator-cost lessons learned from `/crew` (#5).
- The crew skill body is slimmed to the judgment core, pointing at its schema
  and grammar sources rather than restating them (#11).
- The coordinator-cost evidence is stated as proportions rather than absolute
  amounts, so it stays true as model pricing moves.

### Fixed
- A review whose bridge driver was killed is now recovered rather than
  restarted; a killed driver used to orphan a live session into a duplicate
  Codex review pane (#13).
- The first review turn is held until MCP has started, instead of being
  injected early and tripping the startup-interrupt prompt (#14).
- Dispatch strips a context suffix before judging a model name, so
  context-suffixed and versioned aliases pass the alias check (#15).
- `gpt-5.6` is registered as an alias, and the glossary is aligned with
  ADR-0003.
- The MCP registration no longer carries the maintainer's absolute path.
- The residue lint reads quoted names and ignored files correctly.

## [0.1.0] - 2026-08-13

Initial public release.

### Added
- AgentCrew as a Claude Code plugin: `/route` classifies and splits a spec's
  tickets across Claude and Codex subscriptions, `/crew` runs them as
  unattended waves of tmux child agents.
- CI workflow that runs the test suite and the plugin-tree validator on every
  push and pull request to `main`, across Python 3.11–3.14.
- `CONTRIBUTING.md` documenting the test/validator commands, the
  one-concern-per-PR rule, and the local-identifier residue check.

### Fixed
- The residue lint walked `.git/`, so a maintainer's own git identity could
  produce permanent false positives; shipped-file enumeration now skips any
  path with a `.git` component (#1, #2).

[Unreleased]: https://github.com/okqixiaobao727-design/agentcrew-dev-skills/compare/v0.3.5...HEAD
[0.3.5]: https://github.com/okqixiaobao727-design/agentcrew-dev-skills/compare/v0.3.0...v0.3.5
[0.3.0]: https://github.com/okqixiaobao727-design/agentcrew-dev-skills/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/okqixiaobao727-design/agentcrew-dev-skills/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/okqixiaobao727-design/agentcrew-dev-skills/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/okqixiaobao727-design/agentcrew-dev-skills/releases/tag/v0.1.0
