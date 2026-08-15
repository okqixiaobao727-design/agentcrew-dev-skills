# The machine log

One append-only event log per run, written entirely by scripts and hooks. Its audience is a
future auditing agent, not a human: it exists so a run can be reconstructed without anyone's
memory, and so bookkeeping costs the coordinator zero turns
([ADR-0001](adr/0001-coordinator-spends-tokens-only-on-judgment.md)).

The writer is [`skills/crew/assets/machine_log.py`](../skills/crew/assets/machine_log.py).

## Format

JSON Lines: one JSON object per line, UTF-8, appended and never rewritten. A line is written
with a single `write` on a file opened `O_APPEND`, so concurrent writers — the monitor, the
merge driver, one hook per child — interleave lines, never characters.

Every line carries these two keys first, in this order:

| Key | Value |
| --- | --- |
| `ts` | the moment the event was recorded, `%Y-%m-%dT%H:%M:%SZ` in UTC |
| `event` | one of the events [below](#events), which are the whole set a line can carry |

`ts` is the run's one timestamp format — the same one the crew skill reads with
`date -u +%Y-%m-%dT%H:%M:%SZ` — so any two lines subtract to a duration.

A key whose value was not supplied is omitted from the line rather than written empty, so a
reader can tell "not recorded" from "recorded as nothing".

## Events

### `launch` — a child started on a ticket

`ticket`, `child`, `workflow`, `executor`, `model`, `effort`, `branch`, `worktree`, `window`.
`model` is a full model ID, never an alias.

The dispatch renderer writes this one itself, as each child comes up, given `--log`: the launched
set is what wave advancement and the dashboard read, and it costs the coordinator no turn. `child`
is the agent name a Claude child took in the live agents list, and the thread the bridge pinned for
a Codex child.

### `receipt` — a child's final word, as verified by script

`ticket`, `verdict` (`landable`, `parked`, or `failed`), `sha`, `detail`. The three verdicts are
the ones the crew skill's watch step settles a launched ticket into.

### `merge` — one ticket branch's trip into the integration branch

`ticket`, `branch`, `into`, `result`, `sha`, `detail`. `result` is one of the escalation
ladder's four stops ([ADR-0004](adr/0004-escalation-ladder-script-then-sonnet-then-coordinator.md)):

| `result` | Meaning |
| --- | --- |
| `clean` | the scripted merge succeeded, no model involved |
| `conflict` | the merge conflicted and was handed down the ladder |
| `repaired` | the budget-capped repair session resolved it |
| `escalated` | a semantic conflict or a repair double failure went to the coordinator |

### `outcome` — a ticket's one report outcome

`ticket`, `outcome` (`completed`, `failed`, `parked`, or `blocked`), `detail`. These are the four
outcomes the crew contract gives every ticket.

### `review` — a ticket's trip through its review lane

`ticket`, `lane` (the reviewing vendor and its model, as the wave table approved them), `state`
(`running` or `returned`), `detail`. One line when a review starts and one when it comes back; the
last line for a ticket is the one that holds.

The dashboard reads this event and nothing else does: a ticket whose last `review` line says
`running` carries the review annotation row
([`docs/monitor-dashboard.md`](monitor-dashboard.md)), and a ticket whose review has `returned`
goes quiet again.

The writer is the review bridge — the [Codex
one](../skills/crew/assets/review/scripts/tui_review_bridge.py) or the [Claude
one](../skills/crew/assets/review/scripts/claude_review_bridge.py) — given the run's log and the
ticket, which the dispatch renderer fills into the review command the reviewed child runs. The
bridge writes the event rather than the child because it is the only party that deterministically
knows both ends: a child asked in prose to log it may skip the line, and a child whose session
dies mid-review could never write `returned` at all. So `returned` goes down on every exit path
the bridge controls, a review that failed or was interrupted included. A bridge given no log path
writes nothing, and a log it cannot write changes neither its exit status nor the report the child
reads. Each round of a review is a review, so round two writes its own pair.

### `advance` — what the run decided after a wave settled

`wave`, `decision`, `detail`. The one event that carries no `ticket`: a decision is about a wave.
`decision` is one of the four the advance driver reaches
([`docs/wave-advance.md`](wave-advance.md)):

| `decision` | Meaning |
| --- | --- |
| `launched` | the next wave is running; `wave` is the wave that started |
| `complete` | that was the last wave of the run |
| `escalated` | a ticket failed, parked or did not land, and the chain stopped |
| `interrupted` | the operator stopped the run |

### `session-cost` — what one child's session spent, in tokens

`ticket`, `executor`, `model`, `session`, `input_tokens`, `output_tokens`, `cache_read_tokens`,
`cache_creation_tokens`, `total_tokens`, `detail`. One line per launched child, appended by the
cost pass at run completion ([`docs/monitor-dashboard.md`](monitor-dashboard.md)), so a run's
usage is in its own artifacts rather than in transcripts a later agent would have to dig through.

The four token counters are disjoint and `total_tokens` is their sum, in both lanes: Codex reports
its cached tokens inside its input count and Claude reports them beside it, so the Codex figures
are converted before they are written. `session` names every session that ran in the child's
worktree, comma-separated — a resumed or replaced child spent the ticket's tokens too.

A child whose transcript could not be read carries `detail` and no figures at all: the line says
what went wrong and where to look, which is what makes a missing measurement visible rather than
a gap in the log.

### `escalation`, `ruling`, `message` — an outgoing message, copied verbatim

Claude messages are written by the SendMessage hook below; Codex turn messages are written by
`codex_bridge.py` through the same machine-log writer. Neither path writes by hand: `role`
(`coordinator` or `child`), `to`, `message`, and `ticket` where the sender knows one.

`message` is the argument the sender gave the tool, byte for byte — no truncation, no
reformatting, no summary. A structured message is recorded as the object it was.

## Script entry points

```sh
machine_log.py --log <path> launch  --ticket NN --child NAME --workflow W --executor E \
                                    --model ID --effort E \
                                    [--branch B] [--worktree P] [--window W]
machine_log.py --log <path> receipt --ticket NN --verdict landable|parked|failed \
                                    [--sha SHA] [--detail TEXT]
machine_log.py --log <path> merge   --ticket NN --result clean|conflict|repaired|escalated \
                                    [--branch B] [--into B] [--sha SHA] [--detail TEXT]
machine_log.py --log <path> outcome --ticket NN --outcome completed|failed|parked|blocked \
                                    [--detail TEXT]
machine_log.py --log <path> review  --ticket NN --lane "VENDOR MODEL" --state running|returned \
                                    [--detail TEXT]
machine_log.py --log <path> advance --wave N \
                                    --decision launched|complete|escalated|interrupted \
                                    [--detail TEXT]
machine_log.py --log <path> message --role coordinator|child [--ticket NN] [--to NAME] \
                                    --message TEXT
machine_log.py --log <path> session-cost --ticket NN --executor claude|codex --model ID \
                                    [--session IDS] [--input-tokens N] [--output-tokens N] \
                                    [--cache-read-tokens N] [--cache-creation-tokens N] \
                                    [--total-tokens N] [--detail TEXT]
```

Each call appends exactly one line and exits 0. A value outside a closed set is a usage error:
nothing is appended and the exit code is 2, because a log that accepts an unknown verdict is a
log a later agent cannot trust.

## The SendMessage hook

A `PostToolUse` hook matching `SendMessage` copies the message that was just sent into the log.
It is installed on both sides of the channel: coordinator-side it captures rulings, child-side it
captures escalations. Nothing about it costs a model token.

The Codex bridge records a child's non-empty final turn when `watch` observes it finish, and a
coordinator's prompt when `send` submits it. A launch given `--machine-log` and `--ticket` stores
those values in the state file, so later `watch` and `send` calls use the state for the correct
ticket; callers without a log path remain unlogged.

Installing it is the `install` entry point, run once per side against the settings file that side
starts with — the coordinator's own, and each child's `.claude/settings.local.json` in its
worktree, alongside the guard hooks:

```sh
machine_log.py --log <path> install   --settings <settings.json> --role child --ticket NN
machine_log.py --log <path> install   --settings <settings.json> --role coordinator
machine_log.py --log <path> uninstall --settings <settings.json>
```

`--role` says which side is being installed; `--ticket` is added where that side knows one — the
coordinator's hook serves every child at once and omits it. Installing twice replaces the entry
instead of doubling it — the log an entry writes is what identifies it as this run's, whichever
version of this script it runs — and every other hook already in the file is left where it is.

The command registered runs the run's own copy of this script, written beside the log the first
time a run installs and refreshed on every install after it. The plugin is installed one directory
per version, so an entry naming the plugin's own copy stops working at the next upgrade; the run
directory carries no version and outlives every one of them (#37). A caller that keeps its own
copy names it with `--hook-script`, and that path is registered exactly as it was given.

`uninstall` removes every entry installed for the `--log` it is given, and nothing else — an entry
an older plugin version wrote for that run included, which is what keeps an upgrade from leaving
two writers on one log. It is called when the run ends, once for the coordinator's settings file
and once for each launched child's, and again when the run is cleared, so a finished run leaves no
hook behind. It is idempotent: a settings file carrying none of ours is left byte for byte as it
was found, and a block emptied of its only entry goes with it.

A missing or empty settings file is a fresh document to write — and nothing to uninstall from; a
file with content that does not parse, or whose `hooks` are not the shape this writes through, is
refused and left untouched, because the guard hooks live in that file too.

### Which side writes the line

A child's worktree sits inside the repository the coordinator runs in, and the enclosing
checkout's settings load in the child's session too, so every hook both files register fires on
one send. Each installed entry therefore carries `--scope`: the directory whose sends it copies —
the coordinator's checkout, or the child's worktree — taken from the settings file being written
(`<project>/.claude/settings.local.json` scopes to `<project>`) unless `--scope` names another.

The match is that directory exactly, never a directory beneath it, because a worktree is a
descendant of the checkout above it. A send from anywhere else is another side's, and the hook
writes nothing: one message, one line, under the role of the session that sent it.

The event name comes from the message the sender wrote, so the log reads the way the run ran:

| Message | `event` |
| --- | --- |
| anything sent by the coordinator | `ruling` |
| sent by a child, beginning `CREW ASK` | `escalation` |
| anything else sent by a child | `message` |

Only a child escalates — the coordinator is the top of the ladder — so the verb is read on the
child side alone.

A child's own `CREW COMPLETE` is a claim about a sha, not a verified receipt, so it is copied in
as a `message`. Only the script that checked the sha writes a `receipt`, and the log therefore
carries exactly one per launched ticket.

### Zero-token output

The hook writes nothing to stdout, writes nothing to stderr, and always exits 0 — including
when it cannot write the log. Exit 2 would feed stderr back to the model, and any JSON carrying
`additionalContext` would insert into the live context the coordinator was promised nothing would
be inserted into. A failed append is reported on the one channel the model never sees: a
`systemMessage`, which the transcript shows the human.

A send is never blocked by the log. A hook input that is not a `SendMessage` call, or is not
JSON at all, appends nothing and exits 0.

## Why this one is Python

[ADR-0002](adr/0002-shell-scripts-drive-the-cli-not-workflows-or-the-sdk.md) chose scripts driving
the CLI over dynamic workflows and the Agent SDK; it rejected those two, not a language. The
zero-token layer already speaks stdlib Python where a shell script would need a dependency —
`codex_bridge.py`, the review bridges, `launch_hook.py`. This writer copies arbitrary message
values, including structured ones, into JSON verbatim, which bash cannot do without `jq`, and it
reads a hook payload off stdin. Stdlib Python is what the repo already requires and CI already
runs.
