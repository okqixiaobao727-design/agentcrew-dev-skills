# To-tickets+route — the user-typed handoff

A spec with no tickets. `/mattpocock-skills:to-tickets` owns the cutting, the approval quiz, and the
publication; everything here is an **addition** riding along on that run — that skill is the source
of truth for how tickets get drafted, and everything it does on its own it keeps doing (ADR-0006).

## 1. Prompt the user

The skill is reachable only by the user typing it — upstream gates it against model invocation, and
the gate is deliberate. Print one line for the user to send, with both blanks filled in: the spec
path, and the absolute path of `classify.md` resolved from this file's own location:

```
/mattpocock-skills:to-tickets <spec-path> — while drafting, read <absolute-path-to>/classify.md and apply the additions already loaded from to-tickets+route.md
```

Then wait. If the line comes back unexpanded — the harness treats it as plain text because
mattpocock-skills is not installed — stop and tell the user: this mode needs that plugin installed;
until then `/route` can only classify tickets that already exist.

**Done when** the user has sent the line and the `to-tickets` skill body has arrived.

## 2. The additions

Carry these into every step of the `to-tickets` run. They add; where its own text already decides
something, its text stands.

- **The convention document names where tickets land.** Its local-file branch names a path of its
  own; the tracker `docs/agents/issue-tracker.md` settles is the one that decides.
- **Vertical slicing binds the code tickets.** A `tdd` or `refactor` ticket has layers to cut
  through, so the vertical-slice rules bind it whole. A `direct`, `spike`, `ops`, or `acceptance`
  ticket has no layers: it keeps the sizing rule — one fresh context window — and the bar that a
  finished ticket is verifiable on its own, in whatever form its deliverable takes.
- **Splits stay vertical-first, and a contract is the only exception.** Routing pressure — wanting a
  core design decision to sit in a ticket of its own — buys no horizontal split. The exception is a
  **contract** that two or more downstream tickets couple to (a schema, a protocol, a shared
  interface): cut it as its own ticket ahead of them, which is what lets those downstream slices
  classify as non-core. The wide-refactor exception that skill already carries stays available.
- **A contract ticket's acceptance criteria state the contract.** Name what downstream couples to —
  the import path, the directory layout, the command that runs the suite. Criteria written as
  symptoms of a working deliverable ("the suite exits 0") leave every slice behind it free to answer
  the same question differently, and they collide once an edge case reaches them.
- **The quiz presents the routing table.** Classify each drafted ticket by `classify.md`, and show
  the quiz as that file's table plus two columns — blockers, and what the ticket delivers — so the
  quiz's own questions (granularity, edges, merge or split) and the routing land in one approval.
- **Publication carries the routing.** Each published ticket ends with the `## Routing` section
  `classify.md` templates, and is marked with the role string that file names — the skill's own
  "unless instructed otherwise" on labels is this instruction.

## 3. Verify the published tickets

After the `to-tickets` run publishes, **read** every published ticket back through the tracker
operations ([`trackers.md`](../../../references/trackers.md)): each must carry exactly one
`## Routing` section whose lines match the approved table, and the role string that matches its
workflow. **Edit** or **mark** any ticket that misses, and touch nothing the user declined.

**Done when** every published ticket is accounted for against the approved table.

## 4. Link them, then stage

Publication ends on the tracker, and a run starts from a directory: this step carries the published
tickets across, so this entrance ends where the other two do.

**Link** every published ticket to the run's parent ticket as a native sub-issue, where the tracker
is github and the spec has a parent ticket, so that a later `/route #<parent>` expands to exactly
this set:

```bash
gh api repos/<owner>/<repo>/issues/<parent>/sub_issues \
  -F sub_issue_id="$(gh api repos/<owner>/<repo>/issues/<published-ticket> --jq .id)"
```

The sub-issues API takes the child's database `id`, which is why the inner call reads it rather
than passing the issue number.

Then run the staging script exactly as step 4 of [`../SKILL.md`](../SKILL.md) runs it, with the
approved routing table as its JSON: `--parent <n>` where the tickets were just linked to one, the
published ticket set itself otherwise. What the user is handed — the printed `/crew crewtask/<n>`
command, or the blocking items and their fixes — is that step's rule too.

**Done when** the staging script has run and the user holds its printed command, or its blocking
items.
