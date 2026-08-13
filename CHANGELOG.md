# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

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

[Unreleased]: https://github.com/okqixiaobao727-design/agentcrew-dev-skills/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/okqixiaobao727-design/agentcrew-dev-skills/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/okqixiaobao727-design/agentcrew-dev-skills/releases/tag/v0.1.0
