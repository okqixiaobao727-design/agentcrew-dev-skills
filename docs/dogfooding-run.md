# The dogfooding run

The README's demo is one real run of this plugin, and that run is what accepted this release: every
prose-level behaviour of `/route` and `/crew` was exercised against a throwaway repo rather than
argued about. This file records what the run did and what it found.

## What ran

A fresh git repo with a tracker convention document in **local markdown** mode (no remote, no `gh`),
a spec for `textkit` — two independent text helpers and a CLI over them — and this plugin installed
from a local checkout as `agentcrew-dev-skills@agentcrew-dev-skills`.

`/route` started at the setup wizard, wrote `agentcrew.toml` from the shipped defaults, cut the spec
into four tickets, took one revision from the operator — pull the package scaffold out as a contract
ticket — reclassified, and published five tickets carrying `## Routing`.

`/crew` ran them in three waves onto `crew/textkit`. Ticket 03 carried an operator override of its
executor, so wave 2 ran one child per vendor with each reviewed by the other lane.

| NN | Workflow | Executor | Model | Effort | Outcome | Duration |
| --- | --- | --- | --- | --- | --- | --- |
| 01 | direct | claude | opus | medium | completed | 1m 34s |
| 02 | tdd | codex | gpt-5.6-luna | max | completed | 7m 31s |
| 03 | tdd | claude | opus | medium | completed | 12m 03s |
| 04 | tdd | claude | opus | medium | completed | 9m 46s |
| 05 | direct | claude | opus | medium | completed | 3m 01s |

30m 17s wall clock, five merges on the integration branch, five tickets closed in the tracker, and a
`decisions.md` carrying every launch stamp, outcome, coordinator ruling and undo. Three of the five
tickets escalated and waited on a ruling, so the durations price a ticket under this coordinator
rather than measuring vendor speed.

## What it found

Three findings were fixed in `skills/route/SKILL.md` before publishing:

- **`to-tickets` may not be a slash command.** The plugin was installed and the skill present, but
  the session exposed no `/mattpocock-skills:to-tickets` command. Step 3 now says to read that
  skill's `SKILL.md` and follow it directly when that happens.
- **Two documents named the ticket path.** That skill's local-file branch writes to a path of its
  own; this repo's convention document said somewhere else. Step 3 now states which one decides.
- **Contract criteria were written as symptoms.** Four defects in the run traced back to the
  scaffold ticket's acceptance criteria describing a working deliverable rather than the contract
  downstream couples to — including two criteria of one ticket that contradicted each other. Step 3
  now requires a contract ticket's criteria to state the contract.

One finding is open, and it belongs to the bridge rewrite already on the README's roadmap: a
`tui_review_bridge.py` pane process outlives the review it ran, so a run leaves one per reviewed
ticket behind until the shell that owns it exits.

## Reproducing it

The recording is `docs/media/agentcrew-demo.gif`, cut from an `asciinema` capture of the
coordinator's tmux session and rendered with `agg`. Nothing in it is staged: the frames are the
run's own terminal, and the only editing is the choice of which stretches to keep.
