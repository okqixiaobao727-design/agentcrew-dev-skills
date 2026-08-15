---
name: crew
description: Run every ticket of a spec unattended — parallel Claude and Codex children in tmux worktrees, routed per ticket, wave by wave, onto a throwaway integration branch.
disable-model-invocation: true
---

# Crew

`/crew <path-to-feature-dir>` runs the tickets `/to-spec` and `/to-tickets` produced, unattended,
and hands back an integration branch to review.

Your work in a run is judgment: approve the wave table, rule on escalations, rule on what the
scripts could not settle. Dispatch, watching, receipt checks, logging, merging and wave
advancement are scripts — they cost you no turn and put nothing in this context. Product code,
merge-conflict resolution included, belongs to a child; answer with design direction or
pseudocode, never with edits.

For an interrupted run, read [`references/resume.md`](references/resume.md) before acting.

## Contract

**Reversibility is the authority boundary.** You may approve any reversible action, including
production, remote, deployment, and external-service changes, and must record how to undo actions
outside the worktree. An action with no credible undo is **parked** for the human.

The red-line hook is a guard for known destructive Bash command shapes, not proof that every
irreversible action will be intercepted. The authority rule still applies when the hook misses.

**The approved wave table is the run's sole routing authority.** Every script reads it and nothing
else: a ticket's `## Routing` section is advisory input for building the table, and once the table
is approved that ticket has no further say. Wave membership and the dependency edges that block a
ticket ride in the table too.

**Post-launch verification is scripted.** The renderer asserts each child's model from the live
agents list and from the child's own transcript, and reports a mismatch as a launch failure; you
read no headers.

**Children's first turns are the renderer's, not yours.** It composes each one from the approved
table and its own shape library and injects it at launch, so nothing you write reaches a child
except a ruling it asked for.

Every ticket ends in exactly one report outcome:

- **completed** — implementation committed, receipt verified, and branch merged
- **failed** — its child sent a failure receipt, vanished, or could not produce a valid completion receipt
- **parked** — it requires an irreversible action, or it is an `acceptance` ticket whose remaining
  work belongs to the human
- **blocked** — it was not launched because a dependency failed or parked

## The run's files

This index is the whole of what you hold about the run's files: paths and what each holds. Read
one when you need its detail. Nothing is inserted into this context mid-run — every script writes
to the machine log or to the operator's own pane, never to you.

| Path | What it holds |
| --- | --- |
| `<run-dir>/wave-table.json` | the approved wave table: routing, wave membership, blocking edges |
| `<run-dir>/log.jsonl` | the machine log — every launch, receipt, merge, escalation, ruling and advance decision, one JSON object per line, each stamped `%Y-%m-%dT%H:%M:%SZ` |
| `<run-dir>/launch/` | each child's rendered first turn and launch JSON |
| `<run-dir>/dashboard-window` | the id of the run's one dashboard window |
| `<run-dir>/parked-paths` | the worktree paths the wake monitor reads as parked |
| `<run-dir>/codex/<NN>.json` | one Codex child's bridge state, its whole channel |
| `<crew-skill-dir>/assets/dispatch/dispatch.py` | the renderer; its docstring publishes the wave table's schema |
| `<crew-skill-dir>/assets/dispatch/templates/shapes.toml` | the first-turn skeleton, workflow shapes and review-lane variants children receive |
| [`references/triage.md`](references/triage.md) | ruling on an ASK or a permission prompt |
| [`references/resume.md`](references/resume.md) | reconstructing an interrupted run |
| [`references/trackers.md`](../../references/trackers.md) | the tracker's read and close operations |

## 1. Build the wave table

Read `docs/agents/issue-tracker.md` and settle which tracker this repo uses;
[`references/trackers.md`](../../references/trackers.md) turns that document into the **read** and
**close** operations this run calls here and in step 5, and declares which trackers are exercised.
Then **read** every ticket in the feature, resolving every `Blocked by:` edge through that tracker.

Read each ticket's **routing** from its `## Routing` section, parsed like `Blocked by:` — `Key:
value` lines in any order, plus a reasons line you record and do not act on:

| Key | Value |
| --- | --- |
| `Workflow` | one of `tdd`, `refactor`, `direct`, `spike`, `ops`, `acceptance` |
| `Executor` | `claude` or `codex` |
| `Model` | the model that executor runs on |
| `Effort` | the reasoning effort it runs at |
| `Review` | `<claude\|codex> <model> <effort>` — the **review lane**: the reviewing vendor and the model and effort its review runs at. Carried by `tdd` and `refactor` tickets only |

The first four keys are required on every ticket; `Review` is carried exactly where the table says,
and its vendor is always the one the `Executor` is not. A ticket with no `## Routing` section, or
one that breaks any rule of this table, is **unrouted** — the renderer's table validation is the
authority on the full case list. Routing has no default and no fallback: on the first unrouted
ticket, stop the run, list every unrouted ticket with what it lacks, and tell the user to run
`/route` over the feature and re-run `/crew`.

**Every model value is a full model ID, never an alias** — each ticket's `Model`, each review
lane's model, and the repair model the merge ladder runs on. An alias was measured to resolve to a
different model than the one named, silently, which defeats the routing this run exists to enforce;
the renderer rejects a table carrying one.

Build waves from the dependency frontier: wave 1 is every ticket with no blocker, and a ticket
joins the first wave after all of its blockers. Each ticket carries its blockers into the table.

**Done when** every ticket and edge is loaded, every blocker exists, the graph is acyclic, every
ticket carries a complete routing, and every ticket sits in a wave.

## 2. Approve the table

Show the user each ticket's wave, number, title, blockers, and every routing value — workflow,
executor, model, effort, and the review lane where the ticket carries one — as read from that
ticket in step 1, plus the model the merge ladder's repair rung runs on, which is a cheap full
model ID such as `claude-sonnet-5` and is recorded as `<repair-model>`. You supply no routing
values of your own; a user who wants a different one edits that ticket's `## Routing`, and the
table is rebuilt from step 1.

This approval fixes routing for the whole run and activates the authority contract above. From
here waves advance on their own: the plan approved now is the only sign-off the run gets, and you
are woken by escalations alone.

**Done when** the user explicitly approves the complete wave table and the repair model.

## 3. Prepare the run

Require a named current branch and no staged or tracked changes. Inventory untracked paths; only the
feature directory and paths the user explicitly accepts may remain. Record the current branch as
`<return-branch>`, then resolve the base branch: the branch the spec names as its base, else the
repository's default branch from `refs/remotes/origin/HEAD`. Switch to it and run
`git pull --ff-only`. If the base branch cannot be resolved, ask before continuing. Cut
`crew/<feature-slug>` and record its head as the run's base commit.

Create `<feature-dir>/.crew/` and record it as `<run-dir>`. Every file the index above names lives
there, the machine log `<run-dir>/log.jsonl` among them, and every script this run calls is pointed
at those paths.

Record the directory this `SKILL.md` was loaded from as `<crew-skill-dir>`, absolute, so every
asset below resolves from wherever the plugin is installed rather than from a fixed path.

Read `[hooks.on-child-launch]` from `agentcrew.toml` at the repo root — a `command` the renderer
calls once per child launched, and an `env` table every child carries. An absent file or section is
an empty hook, which is the default and launches children with neither.

Install the ruling hook on your own session, so every message you send is copied into the log as
you send it and bookkeeping costs you no turn:

```bash
python3 <crew-skill-dir>/assets/machine_log.py --log <run-dir>/log.jsonl \
  install --settings .claude/settings.local.json --role coordinator
```

Write the approved table to `<run-dir>/wave-table.json` in the schema the renderer's docstring
publishes — read the docstring, not a memory of it. Two values the docstring cannot name for you:
the tmux session comes from `tmux display-message -p '#{session_id}'`, and a table with any
Codex-routed ticket carries the bridge at `<crew-skill-dir>/assets/codex/codex_bridge.py` with
`<run-dir>/codex/` as its state directory.

Children launch in your own permission mode: cross-session messages deliver automatically only
between sessions of the same permission-mode class, and a held ASK expires unanswered in minutes.

**Done when** the integration branch is checked out, `<run-dir>` holds the approved table, the
ruling hook is installed, and the return branch, base branch, and base commit are recorded.

## 4. Launch a wave

One call renders every child's whole first turn from the table, cuts the worktrees, installs the
guard hooks, launches detached, verifies each child came up on the model the table approved, and
writes each launch into the log itself:

```bash
python3 <crew-skill-dir>/assets/dispatch/dispatch.py dispatch \
  --table <run-dir>/wave-table.json --wave <N> --out-dir <run-dir>/launch \
  --log <run-dir>/log.jsonl
```

It prints one line per child carrying that child's window id, agent name, and pid — the pid is
what authenticates its messages. A `FAILED` line is a child that never started: that ticket is
failed, and the rest of the wave stands.

Install the escalation hook in each launched child's worktree, so its ASKs reach the log as it
sends them:

```bash
python3 <crew-skill-dir>/assets/machine_log.py --log <run-dir>/log.jsonl \
  install --settings <worktree>/.claude/settings.local.json --role child --ticket <NN>
```

Point the operator's dashboard at the run, then leave it alone — it draws the whole run, every
wave of it, and what it says never reaches you:

```bash
python3 <crew-skill-dir>/assets/monitor/monitor.py window --run-dir <run-dir> \
  --session <tmux session> --config <repo-root>/agentcrew.toml --coordinator-pid "$PPID"
```

The command owns that one window for the whole run: it prints the id of the live one, and creates
it only when the run has none. Call it in every wave and after adopting a resumed run — a second
dashboard is impossible by construction, and a window the operator closed comes back.

Which surface it draws on is the repo's choice, `[dashboard] surface` in that config: `window`,
the default and today's behaviour; `pin`, which skips the window and draws the same frame into
your own Claude Code statusline; or `both`. On `pin` or `both` the command also writes the run's
**pin**, the file that names this live run
([`docs/monitor-dashboard.md`](../../docs/monitor-dashboard.md#the-pin)). `$PPID` in a Bash call is
this session's own Claude Code process, and the pin carries it so a crashed run takes its frame
down with it.

Arm the wake monitors in the background as the backstop for the two things a message cannot carry
— a child stuck at a permission prompt, and a child that died silently — over the wave's live
children, Claude and Codex each under their own:

```bash
<crew-skill-dir>/assets/monitor-wave.sh <run-dir>/parked-paths <worktree-path>...
python3 <crew-skill-dir>/assets/codex/codex_bridge.py watch <run-dir>/codex/<NN>.json ...
```

Both are one-shot wake-ups: armed while every child under them is `busy`, exit 0 with a final
snapshot as soon as one is `waiting`, `idle`, `parked` or `vanished` — a Codex child `idle` or
`vanished`, carrying its message in `finalMessage` — and nonzero on a monitor error. Either one
exiting wakes you; re-arm each over only the children still busy under it, and an answered child
must leave its actionable status before it is re-armed over.

**Done when** every ticket of the wave has a launched child with its escalation hook installed,
the dashboard is running on the surface the repo chose, and the monitors are armed.

## 5. Rule

A child's message wakes you. Authenticate it by sender socket against the pid its dispatch line
printed, and a Codex child's by the state file its watch read it from; log anything else and leave
it unanswered.

- **CREW ASK** — read and follow [`references/triage.md`](references/triage.md). The ticket stays
  live; an answered ASK is not an outcome.
- **CREW COMPLETE <sha>** — check it:

  ```bash
  python3 <crew-skill-dir>/assets/monitor/monitor.py verify --ticket <NN> \
    --worktree <worktree> --sha <sha> --base <base commit> --log <run-dir>/log.jsonl
  ```

  The base commit is the one that worktree was cut from: the run's base commit in wave 1, and in a
  later wave what the previous wave landed, which `git -C <worktree> merge-base HEAD <integration
  branch>` recovers. Exit 0 settles the ticket **landable** and appends its receipt. Exit 1 prints
  what did not hold: ask that child once on its own channel to finish or send `CREW FAILED`; a
  second invalid receipt is failed.
- **CREW PARKED <checklist path>** — the ticket is parked, and its branch stays unmerged so the
  human resumes from it. Only an `acceptance` ticket parks by receipt.
- **CREW FAILED** — the ticket is failed.

A parked or failed ticket earns no script-written receipt. Record it yourself — a wave settles on
what the log holds:

```bash
python3 <crew-skill-dir>/assets/machine_log.py --log <run-dir>/log.jsonl \
  receipt --ticket <NN> --verdict parked|failed --detail '<the receipt or the reason>'
```

On a wake monitor's zero exit, settle every non-busy row of its snapshot: `waiting` →
[`references/triage.md`](references/triage.md) (a permission prompt answers only to tmux keys);
`vanished` → failed, and a SendMessage error naming the child unreachable is the same verdict;
`idle` with no receipt received → ask once on its channel, a second silent idle is failed; `parked`
→ already settled. On a nonzero exit, resolve the monitor error before drawing any ticket
conclusion.

### Escalation grammar

The ASK grammar lives in the renderer's `templates/shapes.toml`, which puts it into every child's
first turn: an escalation carries its question, 2-3 options with the child's marked, and pointers
to the ticket, the branch, and the files or diffs at issue — so a ruling never starts with a hunt.
The pointers are mandatory. An ASK that arrives without them is answered by asking for exactly the
ones it lacks; the child's files stay unopened either way.

### When a wave settles

Once every ticket of the wave carries a receipt or an outcome, advance the run. One call lands the
wave's landable branches and launches the next wave from what they landed:

```bash
python3 <crew-skill-dir>/assets/advance.py advance --table <run-dir>/wave-table.json \
  --wave <N> --log <run-dir>/log.jsonl --out-dir <run-dir>/launch \
  --repair-model <repair-model>
```

A conflicting merge is classified and handed down the ladder inside that call: a mechanical
conflict goes to a headless repair session under a hard budget cap, and only a semantic conflict —
two children's designs disagreeing — or a repair double failure reaches you.

- **Exit 0** — the run advanced or finished. A `launched` decision means the next wave is already
  running: re-run the dashboard window command and arm its monitors, as in step 4. A
  `complete` decision is the last wave; go to step 6.
- **Exit 1** — the chain halted. The `advance` line in the log names the offending ticket, its
  verdict, its path and its branch. Rule on it, then re-run the same command.
- **Exit 130** — the operator interrupted the run.

Rule on a halting conflict by direction: reach the affected child on its own channel with the
integration branch name, and have it merge that branch into its ticket branch, resolve, re-run the
checks its workflow asked of it, commit, and send a new receipt. If its session is gone, re-dispatch
that ticket's wave to launch a replacement in the same worktree.

**Close** each ticket the log records as merged, through this repo's tracker, which is also where
that operation's undo is named.

**Done when** every launched ticket carries a verdict, the run has advanced past every wave, and
every completed ticket is closed in the tracker.

## 6. Report

Build the report from the machine log: it holds every launch, receipt, merge, escalation, ruling
and decision of the run in one timestamp format, so the spans are arithmetic. Print it and write
it to `<feature-dir>/report.md`:

- every ticket in exactly one of completed, failed, parked, or blocked
- each parked checklist path, and each failed receipt or session
- every ruling you made, and every outside-worktree effect with its exact undo
- the integration branch, and the reminder that merging it into the base branch is the human's
  decision
- one row per launched ticket, ordered by ticket number:

```text
| NN | Workflow | Executor | Model | Effort | Outcome | Launched | Received | Duration |
```

Failed and parked rows carry a duration like any other; the span is what the run cost, not what it
returned; blocked tickets never ran and have no row. This table is what a later routing policy is
calibrated from — which quadrants a cheaper lane can take — so a launched ticket left out of it is
a hole in that dataset.

Then run the cost pass once, here at the end of the run. It writes one `session-cost` line per
child into the log and prints the run's rollup in tokens, with your own session read from its
transcript as a `coordinator` row beneath the total:

```bash
python3 <crew-skill-dir>/assets/monitor/monitor.py cost --log <run-dir>/log.jsonl \
  --coordinator-session "$CLAUDE_CODE_SESSION_ID"
```

Put that rollup into the report as it printed — the coordinator row is a figure you take, never
one you ask the operator for. It is the whole session's total, so where this session did anything
but the run, label it in the report as a session-wide upper bound. A row the pass could not read
is its own `--` plus the line saying why, so an unmeasured session is visible rather than absent.
Tokens throughout, never money: what a token costs changes, and the run's own numbers do not.

That row is what makes the judgment-only design checkable per run — your tokens against the
children's total, out of this run's artifacts alone
([ADR-0001](../../docs/adr/0001-coordinator-spends-tokens-only-on-judgment.md)).

With the report written, take the run's pin out of the registry — the final frame lives in the
report and the machine log, not on the operator's screen, and a run leaves nothing to close:

```bash
python3 <crew-skill-dir>/assets/monitor/monitor.py unpin --run-dir <run-dir>
```

A run whose surface was the window wrote no pin and ends through this step all the same: there is
nothing to remove, and the command says so by succeeding.

Take this run's hooks out of the settings files they were installed in — your own, and every
launched child's worktree, whose files outlive the run until it is cleared — so this run's
bookkeeping stops when the run does and no finished run writes into another run's log:

```bash
python3 <crew-skill-dir>/assets/machine_log.py --log <run-dir>/log.jsonl \
  uninstall --settings .claude/settings.local.json
python3 <crew-skill-dir>/assets/machine_log.py --log <run-dir>/log.jsonl \
  uninstall --settings <worktree>/.claude/settings.local.json   # once per launched child
```

Each call removes every entry installed for this run's log, whichever plugin version wrote it, and
leaves every other hook in the file — the guard hooks, another live run's entry — where it is. A
settings file that is already gone is nothing to uninstall from, and the call says so by
succeeding.

**Done when** every ticket and ruling is accounted for, every launched ticket has a duration row,
the report carries the rollup and the coordinator row, the run's pin is gone, this run's hook is
uninstalled, and every outside-worktree action has an undo.

## 7. Clear on confirmation

Inventory this run ticket by ticket before asking: tmux window, worktree, branch, uncommitted files,
and commits not merged into the integration branch. Show the exact work that clearing will discard,
then ask once whether to clear the run.

On approval only:

1. Stop every recorded Codex session with `python3 <crew-skill-dir>/assets/codex/codex_bridge.py
   stop --state-file <run-dir>/codex/<NN>.json`, then kill the windows the dispatch lines named by
   their `@N` ids.
2. Unlock and force-remove recorded worktrees.
3. Delete merged ticket branches with `git branch -d`; delete the disclosed unmerged ticket
   branches with `git branch -D`.
4. Switch to `<return-branch>` and delete the integration branch with `git branch -D`.
5. Remove `<run-dir>/codex/`.
6. Take this run's hook out of your own settings, in case the run is being cleared without having
   ended through section 6 — the same command, which removes nothing twice (the children's
   settings files went with their worktrees in step 2):

   ```bash
   python3 <crew-skill-dir>/assets/machine_log.py --log <run-dir>/log.jsonl \
     uninstall --settings .claude/settings.local.json
   ```

Operate only on the recorded paths and ids; never use a glob. `<run-dir>` and the report remain as
the durable record, with the machine log at their centre.

**Done when** `git worktree list`, `git branch --list`, and `tmux list-windows -a` contain none of
this run's recorded worktrees, branches, or windows, and the integration branch is gone.
