---
name: route
description: Classify a run's tickets — workflow, executor, model, effort, review lane — and stage the run directory `/crew` starts from; takes a parent ticket number, an explicit ticket list, or a spec whose tickets are not cut yet.
disable-model-invocation: true
model: claude-opus-5
effort: medium
---

# Route

`/route` classifies every ticket of a run on four dimensions — workflow, executor, model, effort —
plus the review lane the reviewed workflows carry, writes each conclusion into its ticket as a
`## Routing` section, and ends by staging the run directory whose exact `/crew` command it prints.
That section is **advisory input**: `/crew`'s driver builds and validates its wave table from it,
and from that validation onward the wave table is the sole routing authority.

Three entrances, one exit — whichever was typed, the run ends at the staging script:

| Typed | The run's ticket set |
| --- | --- |
| `/route #<parent>` | the parent ticket's open sub-issues |
| `/route #<a> #<b> …` | exactly those tickets, whether or not they share a parent |
| `/route <spec-or-feature-dir>` | cut from that spec first — **to-tickets+route** |

Routing is a **suggestion**: steps 1–3 read and propose, step 4 is the first write, and the user
decides at the one checkpoint between them.

## 1. Settle the mode

A repo with no `agentcrew.toml` at its root is a first run: go to the setup wizard in
[`references/setup.md`](references/setup.md), and resume here once it is done. The same wizard
reconfigures a repo whenever the user asks, and fills a config an upgrade left a key short.

Settle the tracker: [`references/trackers.md`](../../references/trackers.md) turns this repo's
`docs/agents/issue-tracker.md` into the **read**, **edit**, and **mark** operations the steps below
call.

Then settle the entrance from what was typed, per the table above. A spec whose tickets are not cut
yet is **to-tickets+route**: hand over to
[`references/to-tickets+route.md`](references/to-tickets+route.md) — `/mattpocock-skills:to-tickets` cuts,
confirms, publishes and stages there with the routing rules riding along, and steps 2–4 below never
run. The other two entrances are **route-only**: assemble the ticket set — a parent's open
sub-issues, or the tickets named — and **read** every ticket of it: body, `Blocked by:` edges
resolved into dependents as well as blockers, and its existing `## Routing` section. Read the
parent ticket's body too where the set came from one; a set given as a list may have no parent and
no spec, and then each ticket's own body is the whole context.

**Done when** the tracker and the entrance are both settled and stated to the user, and in
route-only mode every ticket of the set is loaded with its body, blockers, dependents, and current
routing.

## 2. Classify each ticket

Read [`references/classify.md`](references/classify.md) and run every ticket through its tests.

**Done when** every ticket has a workflow, executor, model, effort, and a one-sentence reason naming
the test that decided it and the answer that test gave, and every `tdd` or `refactor` ticket also
has its review lane.

## 3. Present the suggestion

One table, one checkpoint — the shape `classify.md` gives it. Ask the user to confirm. Apply any
revision they give — re-cutting a ticket means re-classifying it — redisplay the full table, and ask
again.

**Done when** the user explicitly approves the complete table as displayed.

## 4. Stage the run

Write the approved table to a JSON file — one object keyed by ticket number, each entry carrying
that ticket's approved `workflow`, `executor`, `model`, `effort`, `reasons`, and `review` where the
workflow takes one — and run the staging script:

```bash
python3 <route-skill-dir>/assets/stage/stage.py --routing <approved-table.json> \
  --parent <n>            # the parent entrance
python3 <route-skill-dir>/assets/stage/stage.py --routing <approved-table.json> \
  <ticket> <ticket> …     # the ticket-list entrance
```

`<route-skill-dir>` is the absolute directory this `SKILL.md` loaded from, and a `<ticket>` is what
the settled tracker calls one — an issue number on github, a path to the ticket's file on local.
The script owns every write from here: each approved `## Routing` section and role label onto the
tracker, the `crewtask/<n>/` run directory, the self-check of that directory against the driver's
own preflight, and — on a green self-check only — the `/crew crewtask/<n>` command, commented on
the tracker and printed.

Exit 0: hand the user the printed command, which is the whole of what a later session needs.
Anything else: hand them the blocking items the script named, each beside its fix, and nothing more
— the only `/crew` command that leaves this step is one the script itself printed. Re-run the
script once the blocking items are fixed; it refreshes the same run directory in place.

**Done when** the script has exited 0 and the user holds its printed `/crew crewtask/<n>` command,
or it has exited non-zero and the user holds its blocking items and their fixes.
