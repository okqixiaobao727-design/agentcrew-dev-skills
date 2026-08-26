# Contributing

Thanks for looking. This is a small project with a narrow scope, so the most useful thing you can
do before writing code is open an issue and say what you are trying to change.

## What this project accepts

AgentCrew is the orchestration layer described in [`README.md`](README.md) and designed in
[`docs/design.md`](docs/design.md). Changes that fit its shape are welcome; changes that widen its
scope need a conversation first, because the design document names the alternatives that were
rejected and why.

The [Roadmap](README.md#roadmap) lists the work already planned. Bug reports are always welcome, and
a bug report that names the exact command and the exact output is worth more than a patch that
guesses at the cause.

## Requirements

- **Python 3.11 or newer.** The config validator reads TOML with `tomllib`, which arrived in 3.11.
- **No Python package install.** The test gate and validator use the standard library only.

Running the skills themselves needs more than this (Claude Code, tmux, optionally the Codex CLI);
see the README's Requirements section. Contributing to the Python code does not.

## Running the checks

`scripts/test.py` is the whole of the test gate, and it takes one argument:

```sh
python3 scripts/test.py --asset driver   # while you work: the suite that tests what you changed
python3 scripts/test.py                  # every suite at once
python3 scripts/validate_plugin_tree.py  # manifest, skill slots, config, residue lint
```

An asset's suite answers to the name of its directory, because a skill asset's tests live next to
the asset, in a `tests/` directory beside it: the stub PATH and fixture repository they need belong
with the script they stand in for. The repository's own suite, `tests/`, answers to `root`, and a
name the script does not know prints the list. Selection is declared, never inferred: nothing reads
`git diff` to guess what you touched
([ADR-0016](docs/adr/0016-one-validation-entry-point-with-focused-and-full-intents.md)).

**Which of them to run is in [AGENTS.md](AGENTS.md#validating-your-work)** — the rule turns on
whether your change reaches the shared spine, and it is written down in one place so it cannot
drift. In short: the focused suite while you work, the validator every time, and the full gate
when you touched something more than one suite imports.

Each suite reports its size and wall time on stderr, so read the total there rather than expecting
a figure from this file; the gate cuts every suite into shards and runs them at once, so that
total is its slowest shard and not the sum of them, and a suite's own line is its slowest shard
too. Give it a fifteen-minute timeout — a ceiling to allocate, not an estimate.

CI runs the tests and the validator on every push and pull request to `main`, on Python 3.11 and
3.14 — the only place they meet the floor version, Linux, and a clean checkout. A newer run on the
same ref cancels the one it supersedes.

### One thing that will surprise you

The validator's residue lint rejects **personal identifiers**: machine nicknames and account names
that have no business in a public repo. It cannot guess yours, so it reads them from
`.agentcrew-local-identifiers` at the tree root — a gitignored file, so your list stays yours —
or from `AGENTCREW_LOCAL_IDENTIFIERS` as a comma-separated list.

If you create that file, the test suite starts checking the real working tree against it. That is
deliberate: it is what stops your hostname reaching a release. If it reports a hit in a file you
did not touch, that is a real finding, not a broken test.

Configure nothing and the rule stays inert. The other rules — private bridge paths, private
environment tokens, spend figures, the skill's retired name — always run.

### What the checks cannot tell you

Most of this project is prose: the two `SKILL.md` files are instructions an agent follows, and no
unit test can tell you whether an agent actually follows them. Behaviour changes to a skill are
verified by running them, against a throwaway repo, and reporting what happened —
[`docs/dogfooding-run.md`](docs/dogfooding-run.md) records how that was done for the first release.

If your change touches skill prose, say in the pull request what you ran and what you saw. "The
tests pass" is not evidence about a skill's behaviour.

## Pull requests

1. Branch off `main`. Name it for what it does — `fix/...`, `feat/...`, `docs/...`.
2. **One concern per pull request.** Two unrelated fixes are two pull requests; it makes each one
   reviewable and either one revertable without taking the other with it.
3. Run what your change reaches, once, at the end — see
   [AGENTS.md](AGENTS.md#validating-your-work). The validator runs whatever you touched.
4. Open the pull request against `main`. Describe the problem, then the fix, then how you verified
   it. If it closes an issue, write `Closes #<n>` in the body.
5. Say what you changed beyond the ticket, if anything. A widened fix is often the right call — it
   just needs to be visible to the reviewer rather than buried in the diff.

Commit messages use a `type: summary` first line in the imperative, and a body that explains **why**
the change was made, not what the diff already shows.

## Code style

There is no formatter and no linter beyond the tree validator. Match the code that is already there:

- Lines wrap at 100 characters.
- Comments explain **why**, and are worth writing when the reason is not obvious from the code.
  Comments that restate the line above are noise.
- Docstrings say what a function returns, in a sentence.
- **Never hard-code an install path.** A skill reaches its own assets through the `<crew-skill-dir>`
  and `<plugin-dir>` placeholders, which the coordinator resolves at run time. The validator fails
  the build if you write a literal path into a skill or a script, because that path is correct only
  on the machine it was written on.

## Reporting a security issue

Do not open a public issue, because that publishes the vulnerability along with the report. Use
GitHub's private vulnerability reporting on this repository instead — **Security → Report a
vulnerability** — and give the maintainer a few days to respond before escalating.

## License

By contributing you agree that your contributions are licensed under the [MIT License](LICENSE),
the same terms that cover the rest of the project.
