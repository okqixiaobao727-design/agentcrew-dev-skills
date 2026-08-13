# Workflow shapes

One shape per `Workflow` value in a ticket's `## Routing` section. Each supplies the three fills
the first turn in SKILL.md step 4 leaves open — `<opening line>`, `<workflow block>`, and
`<completion condition>` — so a child's first turn carries exactly the process its ticket was
routed to and nothing from another one.

`tdd` and `refactor` take a fourth fill, `<review block>`, from [Cross review](#cross-review) below
— the variant its ticket's `Review` vendor names.

Where a shape says **Claude children only**, include that line when `Executor` is `claude` and
drop it otherwise.

## tdd

Opening line:

```text
/implement <absolute ticket path>
```

Workflow block:

```text
Workflow: tdd. Base commit for the review: <ticket base commit>.
Every expected value in a test derives from the ticket or the spec. A value read off your own
implementation's output restates the implementation and tests nothing. Where the ticket pins no
expected value for something you must assert, escalate for the value.
<review block>
```

Completion condition: `implementation, tests, the review, and commit`

## refactor

Opening line:

```text
Refactor per <absolute ticket path>
```

Workflow block:

```text
Workflow: refactor. Base commit for the review: <ticket base commit>.
Behaviour is frozen; only its expression changes. Work characterization-tests-first:
1. Write characterization tests that pin today's behaviour and pass against the code as it stands
   — the suite is green before you change a line. There is no red phase in this workflow.
2. Refactor in steps, running the suite after each and keeping it green.
3. Those expectations stay exactly as written in step 1. A test that only passes once you edit its
   expected value means the behaviour moved: revert that step, or escalate if the ticket asks for
   the move.
4. Obtain the review below, then commit.
<review block>
```

Completion condition: `characterization tests, refactor, the review, and commit`

## direct

Opening line:

```text
Implement <absolute ticket path>
```

Workflow block:

```text
Workflow: direct. Implement it and commit — no test-first cycle, no test files added to satisfy
one, and no review. Write tests only where the ticket names them as part of the deliverable.
For a prose deliverable — a skill, AGENTS.md or CLAUDE.md, a document an agent reads — invoke
/mattpocock-skills:writing-for-agents before you write any of it, and follow it.
```

Completion condition: `implementation and commit`

## spike

Opening line:

```text
Investigate <absolute ticket path>
```

Workflow block:

```text
Workflow: spike. The deliverable is one Markdown findings document, committed to the repo at the
path the ticket names; ship no production code and obtain no review.
Every claim carries a source — file and line, command and its output, or URL — and a confidence
level of high, medium, or low. Record a claim you could not source as an open question, and keep
low-confidence findings as findings: an honest low beats a guess dressed as high.
Claude children only: the /research and /prototype skills are available as execution tools.
```

Completion condition: `the findings document and commit`

## ops

Opening line:

```text
Implement <absolute ticket path>
```

Workflow block:

```text
Workflow: ops. Verification here is an end-to-end run, not a unit test: exercise the real path in
a temporary root you create for the run, and let that run be the evidence the ticket works. Add
unit tests only where the ticket names them, and obtain no review.
Record in the commit message the commands you ran, the temp root you ran them in, and what they
printed.
```

Completion condition: `the end-to-end run and commit`

## acceptance

Opening line:

```text
Prepare <absolute ticket path>
```

Workflow block:

```text
Workflow: acceptance. This ticket closes with a human, so your job is to leave them the shortest
possible path. Do every part of it an agent can do, then write a checklist at the path the ticket
names, or `acceptance-<NN>.md` beside the ticket's own deliverable when it names none, with two
headed sections:
- Verified — what you ran or read, each item with the evidence that settled it.
- Assumed — what you could not settle yourself, each item with the exact command, click-path, or
  question that would settle it, and what a pass looks like.
The checklist is resumable: a human picks it up cold, without rereading this run. Claiming an
Assumed item as Verified is the one failure this workflow exists to prevent.
```

Receipt: `acceptance` replaces the first turn's completion paragraph with this one:

```text
Commit your preparation and the checklist, then park: send
CREW PARKED <absolute checklist path> ts=<unix time>
and stop — the checklist is the human's to run.
If you cannot reach that point, send:
CREW FAILED <reason> ts=<unix time>
```

## Cross review

A reviewer catches what the implementer's own blind spots hide, so on `tdd` and `refactor` the
reviewer is always the vendor that did not implement: a Claude child is reviewed by Codex, a Codex
child by Claude. The ticket's `Review` line names that vendor and the model and effort the review
runs at; the coordinator fills the `<review block>` from the matching variant below and every
`<...>` in it from the run.

Both variants review the same fixed point — the ticket's base commit — through a bridge this skill
carries in `assets/review/scripts/`, so the review runs at the routed strength as a command-line
flag rather than as a request the child could soften. Close every filled `<review block>` with the
Rounds contract from [`rounds.md`](rounds.md), quoted in full.

### Reviewer `codex` — the child is Claude

```text
Review: codex at model <review model>, effort <review effort>. Once the work is in place and
before you commit it, run:

python3 <crew-skill-dir>/assets/review/scripts/tui_review_bridge.py \
  --cwd <worktree-abs-path> --model <review model> --effort <review effort> \
  -- 'the changes in this worktree since <ticket base commit>'

This command is the ticket's only review; it satisfies every review step any skill you invoke
asks of you.

The bridge prints one JSON object. A `status` of `completed` with a non-empty `finalMessage` is
the report; keep `reviewSessionId` and pass it back as `--resume-session '<reviewSessionId>'` on
the same command for round two, so the follow-up still sees round one's findings and can say which
of them the fixes closed. A `status` of `interrupted` carrying a `reviewSessionId` is a resumable
pane: resume it the same way. Any other result is a failed review — retry once, then escalate with
exactly what the bridge printed.

<rounds contract>
```

### Reviewer `claude` — the child is Codex

```text
Review: claude at model <review model>, effort <review effort>. You have no Claude session of your
own, so a headless one reviews for you. Once the work is in place and before you commit it, run:

python3 <crew-skill-dir>/assets/review/scripts/claude_review_bridge.py \
  --cwd <worktree-abs-path> --model <review model> --effort <review effort> \
  'the changes in this worktree since <ticket base commit>'

This command is the ticket's only review; it satisfies every review step any skill you invoke
asks of you.

The bridge prints one JSON object: `findings` is the report, and `lineageId` is the handle for
round two. Keep that handle and pass it back — the same command plus `--resume-session
'<lineageId>'` — so the follow-up review still sees round one's findings and can say which of them
the fixes closed.

Read `status`, `exitCode`, and `permissionDenials` before you trust the report: a status of
`error`, a nonzero exit, or any denial means the review is partial or absent. Retry once, then
escalate with what the bridge printed and the `logFile` path it names.

<rounds contract>
```
