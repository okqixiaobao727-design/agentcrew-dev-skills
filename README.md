# AgentCrew

**English** · [简体中文](README.zh-CN.md)

**Two skills that turn a spec into finished branches: `/route` classifies and splits its tickets
across your Claude and Codex subscriptions, `/crew` runs them as unattended waves of tmux child
agents.**

```text
mattpocock-skills   grilling → to-spec → to-tickets → implement    one ticket at a time
AgentCrew           grilling → to-spec →   route    →   crew       the whole frontier at once
```

AgentCrew is an aggregating enhancement of [mattpocock-skills](https://github.com/mattpocock/skills):
it keeps `grilling` and `to-spec` exactly as they are and replaces the back half of the pipeline.
`/route` invokes `/mattpocock-skills:to-tickets` under a routing overlay, so ticket granularity and
routing are one approval; `/crew` runs the routed tickets. The **Matt-first principle** governs every
integration question — the overlay only adds rules, and where AgentCrew's needs and Matt's skills
tension, Matt's experience wins.

![AgentCrew running a real feature: /route's table, the wave table, mixed Claude and Codex children
in tmux, and the closing duration table](docs/media/agentcrew-demo.gif)

> **One real run, cut to 30 seconds.** `/route` proposing a five-ticket split and taking a revision;
> the wave table with one child per vendor and each reviewed by the other lane; wave 2 running with
> both monitors armed; and the report — five tickets completed on `crew/textkit` in 30m 17s across
> three waves. AgentCrew built by AgentCrew: this is the run that accepted this release.

Hand it a spec at night; find an integration branch to review and a decision log in the morning.
Short of destroying data, it pushes everything forward on its own.

## The two skills

| Skill | Takes | Gives back |
| --- | --- | --- |
| `/route <feature-dir>` | a spec, with or without tickets | every ticket carrying a `## Routing` section — workflow, executor, model, effort, and a review lane on the reviewed workflows |
| `/crew <feature-dir>` | routed tickets | an integration branch `crew/<slug>`, a `report.md` with a per-ticket duration table, and a machine log of everything the run did |

`/route` has two modes, decided by what the feature directory holds. A spec with no tickets is cut
and routed in one pass; a feature that already has tickets is routed and nothing is cut, so you can
adopt AgentCrew on work already in flight. Routing is a suggestion: one table, one checkpoint, and
nothing is written until you approve it.

`/crew` gives each ticket its own git worktree and its own tmux window, so unattended never means
invisible — you can attach to any child and take over mid-run. Children escalate over the
cross-session message channel and report a full 40-character commit sha, which the coordinator
verifies before merging. The base branch is untouched for the whole run; the final merge is yours.

## Requirements

- **Claude Code**, with the [mattpocock-skills](https://github.com/mattpocock/skills) plugin
  installed — `/route` invokes `/to-tickets`, and the setup wizard sends you to
  `/setup-matt-pocock-skills` when your repo has no issue-tracker convention document.
- **tmux** — children run as windows of the coordinator's session.
- **Python 3.11+** — the config validator and the Codex bridge.
- For Codex tickets: the **Codex CLI**, and the `aiohttp` package installed for the Python
  interpreter Claude Code runs.
- For reviewed tickets — every `tdd` and `refactor` ticket:
  **[Review-Switch](https://github.com/okqixiaobao727-design/review-switch)**, installed so that
  its `review-bridge` command is on your `PATH`. AgentCrew ships no review implementation of its
  own and calls that command across a process boundary
  (`docs/adr/0020-review-switch-owns-the-review-agentcrew-owns-the-reviewer.md`), so a run whose
  wave table carries a review lane stops in preflight until it is installed.

Every repo you use AgentCrew in also needs `docs/agents/issue-tracker.md`: both skills read it to
learn where tickets live and where status is written back, and neither has a fallback.

## Install

```text
/plugin marketplace add okqixiaobao727-design/agentcrew-dev-skills
/plugin install agentcrew-dev-skills@agentcrew-dev-skills
```

To run from a local checkout instead — to read the skills, or to edit them — clone the repo and add
the clone as the marketplace:

```bash
git clone https://github.com/okqixiaobao727-design/agentcrew-dev-skills.git
```

```text
/plugin marketplace add ./agentcrew-dev-skills
/plugin install agentcrew-dev-skills@agentcrew-dev-skills
```

## First run

1. **`/route <feature-dir>`** in your project. A repo with no `agentcrew.toml` at its root starts at
   the setup wizard, which settles the issue-tracker convention document and then copies the shipped
   defaults — comments and all — to `agentcrew.toml` at your repo root. Ask for the wizard any time
   to reconfigure.
2. **Read the table `/route` prints.** One row per ticket — workflow, executor, model, effort,
   review lane, and the test that decided each — headed by the config file the cells resolved from.
   Revise until it is right; it writes only once you approve.
3. **`/crew <feature-dir>`.** It rebuilds the wave table from what the tickets now carry, asks once,
   then runs: a worktree and a tmux window per ticket, waves cut from the dependency frontier, each
   landed branch merged into `crew/<slug>` before the next wave is cut.
4. **Review the integration branch and `report.md`,** then merge it yourself.

To check a config file you have edited by hand:

```bash
python3 scripts/validate_plugin_tree.py --config agentcrew.toml
```

It prints one line per problem and exits non-zero. A case your file leaves out is not a problem: the
shipped defaults answer every case, so a project file carries only the cells it overrides.

## Configuration reference

The configurable surface is the two model tables, one hook, and the dashboard's surface, in
`agentcrew.toml` at your repo root. The classification logic — six workflows, core vs non-core,
complex vs routine — is fixed product opinion: you configure the outcomes, not the decision
procedure. The shipped, commented defaults are
[`config/agentcrew.default.toml`](config/agentcrew.default.toml).

Every cell carries the same three fields:

| Field | Value |
| --- | --- |
| `executor` | `claude` or `codex` — the vendor that runs the ticket |
| `model` | passed verbatim to that vendor's launch command, so a Codex model is its full slug |
| `effort` | passed verbatim too — the reasoning effort the vendor is launched at |

### Implementer table — who writes the ticket's code

| Cell | The case it answers | Shipped default |
| --- | --- | --- |
| `implementer.tdd-refactor.core-complex` | reviewed code whose design decisions downstream couples to, crossing modules or leaving the approach open | `claude` / `claude-opus-5` / `medium` |
| `implementer.tdd-refactor.core-routine` | the same coupling, contained and specified | `claude` / `claude-opus-5` / `medium` |
| `implementer.tdd-refactor.non-core-complex` | nothing couples to its design decisions, but the work is intricate | `claude` / `claude-opus-5` / `medium` |
| `implementer.tdd-refactor.non-core-routine` | contained, specified, and nothing downstream depends on how it is built | `codex` / `gpt-5.6-luna` / `max` |
| `implementer.direct.any` | prose, docs, skill copy, config — every difficulty | `claude` / `claude-opus-5` / `medium` |
| `implementer.spike.directed-collection` | the questions are enumerable up front, each naming its verification method, with no recommendation in the deliverable | `codex` / `gpt-5.6-luna` / `max` |
| `implementer.spike.open-exploration` | any one of those three missing | `claude` / `claude-opus-5` / `medium` |
| `implementer.ops.mechanical` | an action against an environment, run and recorded | `codex` / `gpt-5.6-luna` / `max` |
| `implementer.ops.acceptance-judgement` | the same run, ending in a judgement of the result | `claude` / `claude-opus-5` / `medium` |
| `implementer.acceptance.any` | finishing needs a human, so the agent prepares and hands over | `claude` / `claude-opus-5` / `medium` |

The three `tdd-refactor` cells that share a default are listed separately so any one of them can be
retargeted on its own.

### Reviewer table — who reviews it

`tdd` and `refactor` are the only workflows whose diff a review can catch anything in, so they are
the only ones that carry a reviewer. The quadrant that chose the implementer chooses the reviewer,
and the reviewing vendor is always the one that did not implement.

| Cell | Shipped default |
| --- | --- |
| `reviewer.core-complex` | `codex` / `gpt-5.6-sol` / `medium` |
| `reviewer.core-routine` | `codex` / `gpt-5.6-luna` / `max` |
| `reviewer.non-core-complex` | `codex` / `gpt-5.6-luna` / `max` |
| `reviewer.non-core-routine` | `claude` / `claude-opus-5` / `medium` |

### `[hooks.on-child-launch]` — the one extension point

Both fields are empty by default, and both empty means a child launches exactly as it would with no
hook at all.

| Field | Value |
| --- | --- |
| `command` | a shell command run once per child at launch, in that child's working directory — wire it to whatever notification or session-tracking system you already run |
| `env` | a table of string environment variables added to every child's environment, and to the hook command's |

The command also receives two variables naming the child that launched:

| Variable | Value |
| --- | --- |
| `AGENTCREW_CHILD_CWD` | the child's working directory |
| `AGENTCREW_CHILD_TMUX_TARGET` | the tmux window or pane the child runs in, empty for a child that has none |

The command is a courtesy to what you already run: one that fails or hangs leaves the launch itself
standing, and the run records what it printed.

```toml
[hooks.on-child-launch]
command = "notify-send 'AgentCrew child launched' \"$AGENTCREW_CHILD_CWD\""

[hooks.on-child-launch.env]
MY_PROJECT_MODE = "unattended"
```

### `[dashboard]` — which surface a run draws itself on

| `surface` | What the run does |
| --- | --- |
| `window` | gives the run its own tmux window, as it always has — the default |
| `pin` | skips that window and draws the same frame into the coordinator's Claude Code statusline, so there is nothing to close when the run ends |
| `both` | runs each, deduping toasts through the run's one toast state |

```toml
[dashboard]
surface = "window"
```

What the pinned dashboard is and how it is wired into Claude Code is in
[`docs/monitor-dashboard.md`](docs/monitor-dashboard.md).

### `[repair]` — the model the merge ladder repairs on

A mechanical merge conflict — two children inserting at the same point, neither rewriting the
other's work — is resolved by the merge driver itself, at no cost. The one it will not rewrite,
where the file's own text carries a line that reads as a conflict marker, goes to a headless
session under a hard budget cap before anything reaches the coordinator. Pick a cheap model: the
rung exists to keep mechanical work off the expensive one. A full model ID, never an alias, and no
default — a run whose config names none stops in preflight.

```toml
[repair]
model = "claude-sonnet-5"
```

### `[tracker]` — where a run closes its merged tickets

Which of the two exercised trackers a run's close operation and its recorded undo are those of:
`github`, where a ticket is an issue reached through `gh`, or `local`, where a ticket is a markdown
file whose `Status:` line carries what a label carries on github. No default, and any other value
stops the run in preflight rather than reaching a CLI nobody named.

```toml
[tracker]
kind = "local"
```

### `[accounts]` — which Claude logins this repo's tickets may name

A ticket may name the **account** it runs on: a named Claude Code login whose subscription that
ticket's children spend. This section declares the names this repository expects, never a path —
the name-to-profile-directory map is a machine-level file. Both are optional, and a project on one
subscription leaves this section out and creates no file.

```toml
[accounts]
names = ["work", "side"]
```

What an account is, where the registry lives, what a ticket naming an unregistered one does, and
why an unauthenticated profile surfaces at the verification timeout are in
[`docs/accounts.md`](docs/accounts.md).

## Tracker support

Both skills read your repo's `docs/agents/issue-tracker.md` and hard-code no tracker.

| Tracker | Status |
| --- | --- |
| GitHub Issues | Supported. Tickets are issues; `/crew` closes each completed one and clears the label that marks it available for pickup |
| Local markdown files | Supported. `/route` writes ticket files only, and `/crew` flips each ticket's `Status:` line instead of closing an issue — no remote required |
| GitLab, Jira, Linear, anything else | **Untested.** Both skills follow whatever your convention document describes, and this release has never been run against them. That is the whole of the support statement |

## Checking the tree

`python3 scripts/validate_plugin_tree.py` validates a plugin tree — manifest, skill slots, config
defaults, self-referential paths — and lints it for residue that has no business in a public repo:
private bridge paths, private environment tokens, spend figures, and the skill's retired name.

That lint also rejects personal identifiers, which it cannot guess: your machine nicknames, your
account names. List yours one per line in `.agentcrew-local-identifiers` at the tree root — the file
is gitignored, so your list stays yours — or set `AGENTCREW_LOCAL_IDENTIFIERS` to a comma-separated
list, which covers every checkout at once and wins over the file. Configure none and that one rule
stays inert; the other four still run.

## Roadmap

- **The Codex bridge** — `codex_bridge.py` is copied in as it stands; a rewrite is planned.
- **The mattpocock-skills dependency** — required today, optional later.

## Docs

- [`docs/design.md`](docs/design.md) — the architecture, the red line, and the rejected alternatives.
- [`docs/glossary.md`](docs/glossary.md) — the vocabulary both skills speak.
- [`docs/accounts.md`](docs/accounts.md) — running a wave's tickets on more than one Claude
  subscription: the registry, what a ticket names, and what each failure path says.
- [`docs/dogfooding-run.md`](docs/dogfooding-run.md) — the run in the demo above: what it did, what
  it found, and what it left open.
- [`docs/cost-baseline.md`](docs/cost-baseline.md) — the measured predecessor run ADR-0001 was
  decided on: where a coordinator's money actually goes, and what a future run is graded against.
- [`config/agentcrew.default.toml`](config/agentcrew.default.toml) — the shipped defaults, commented.

## License

MIT — see [LICENSE](LICENSE).
