---
name: route
description: Classify every ticket of a feature — workflow, executor, model, effort, review lane — and write the confirmed routing into each one; a spec with no tickets is cut, confirmed, and published by a user-typed /mattpocock-skills:to-tickets run carrying the routing rules along.
disable-model-invocation: true
model: claude-opus-5
effort: medium
---

# Route

`/route <path-to-feature-dir>` classifies every ticket of a feature on four dimensions — workflow,
executor, model, effort — plus the review lane the reviewed workflows carry, and writes each
conclusion into its ticket as a `## Routing` section. That section is **advisory input**: `/crew`
builds its wave table from it, and from approval onward the wave table is the sole routing
authority.

Routing is a **suggestion**: steps 1–3 read and propose, step 4 is the first write, and the user
decides at the one checkpoint between them.

## 1. Settle the mode

A repo with no `agentcrew.toml` at its root is a first run: go to the setup wizard in
[`references/setup.md`](references/setup.md), and resume here once it is done. The same wizard
reconfigures a repo whenever the user asks.

Settle the tracker: [`references/trackers.md`](../../references/trackers.md) turns this repo's
`docs/agents/issue-tracker.md` into the **read**, **edit**, and **mark** operations the steps below
call. Then look for the feature's tickets, where that tracker keeps them.

A spec with no tickets is **spec-only**: hand over to
[`references/spec-only.md`](references/spec-only.md) — `/mattpocock-skills:to-tickets` cuts,
confirms, and publishes there with the routing rules riding along, and steps 2–4 below never run.
Tickets found is **route-only**: read the feature's spec, then **read** every ticket — body,
`Blocked by:` edges resolved into dependents as well as blockers, and its existing `## Routing`
section.

**Done when** the tracker and the mode are both settled and stated to the user, and in route-only
mode the spec and every ticket are loaded — each ticket with its body, blockers, dependents, and
current routing.

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

## 4. Write the Routing section

**Edit** each approved ticket, replacing its whole body with the approved text, ending in the
`## Routing` section `classify.md` templates; a ticket that already carries one gets it replaced in
place. **Mark** each ticket with the role string its workflow names.

**Done when** every approved ticket carries exactly one `## Routing` section whose lines match the
approved table, is marked with the role string that matches its workflow, and no ticket the user
declined was touched.
