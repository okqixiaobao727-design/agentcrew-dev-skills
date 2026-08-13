# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

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

[Unreleased]: https://github.com/okqixiaobao727-design/agentcrew-dev-skills/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/okqixiaobao727-design/agentcrew-dev-skills/releases/tag/v0.1.0
