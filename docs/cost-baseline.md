# The predecessor cost baseline

[ADR-0001](adr/0001-coordinator-spends-tokens-only-on-judgment.md) decided that the coordinator
spends model tokens only on judgment, and it decided that on measured numbers: a forensic audit of
one real `/orchestrate` run under the predecessor design. That audit lived only in an
operator-private document, so the numbers the ADR cites had no checkable source inside the repo,
and a future run's cost record ([`docs/machine-log.md`](machine-log.md) `session-cost` events plus
the run report's rollup) had nothing in-repo to be graded against.

This file is that source, redacted: the figures, what was measured, and how — with the parts that
identify a private repository, a person, or an account removed. Every figure here is a direct
measurement from the coordinator's transcript unless marked *(estimate)*.

## What was measured

| Field | Value |
| --- | --- |
| Predecessor system | `/orchestrate` (the workflow AgentCrew replaced) |
| Coordinator model | Fable 5, effort `high`, on all 184 requests |
| Run scope | 13 tickets; the dispatch turns name waves up to 6 |
| Wall clock | 2026-08-12T23:24Z → 2026-08-13T03:24Z — 4h 00m |
| Measured | 2026-08-13, from the transcript, after the run finished |
| Source of the bill | the coordinator session's own Claude Code transcript (JSONL) |
| Scope boundary | coordinator session only — children ran as separate CLI sessions in worktrees, so none of their spend appears in these figures |
| Repository under work | redacted (private) |
| Transcript location | redacted (operator's local project directory) |

## Headline figures

### Where the money went

| Figure | Value |
| --- | --- |
| Real API requests (deduplicated — see method step 1) | 184 |
| Coordinator turns that were mechanical (dispatch, bookkeeping, status narration, polling, git/gh) | 127 — 69% |
| Coordinator **bill** that was mechanical | **70.4%** |
| Coordinator bill spent on rulings and answering children — the only work needing the expensive model | **7.6%** (14 turns) |
| Share of the bill that was cache reads — the coordinator re-reading its own context | 72.0% |
| Cache-read tokens | 24,677,948 |
| Cache-write tokens (100% at the 1-hour TTL, billed at 2× base input) | 217,440 |
| Uncached input tokens | 365 |
| Output tokens | 104,735 (30,130 of them thinking) |
| Final context | 224,094 tokens |

### The full activity breakdown

Every turn falls in exactly one category, so the three shares above are complete: 70.4% mechanical
+ 7.6% rulings + 22.0% below.

| Activity | Turns | % turns | % of bill |
| --- | ---: | ---: | ---: |
| Dispatch (worktrees, prompt files, tmux launch, Codex bridge) | 31 | 16.8% | 19.7% |
| Decision-log bookkeeping | 28 | 15.2% | 15.0% |
| Status narration to the user (text-only turns) | 29 | 15.8% | 14.5% |
| Polling / monitoring (monitor script, capture-pane, sleeps) | 23 | 12.5% | 11.1% |
| git / gh operations, merges, issue and label CRUD | 16 | 8.7% | 10.2% |
| **— the five above are the mechanical bundle —** | **127** | **69.0%** | **70.4%** |
| Planning, repo and ticket recon | 22 | 12.0% | 10.7% |
| Reading child output (receipts, diffs) | 15 | 8.2% | 6.7% |
| Startup: skill + plugin + CLAUDE.md load | 4 | 2.2% | 3.1% |
| Verification / lint runs | 2 | 1.1% | 1.5% |
| **Judgment calls and child questions** | **14** | **7.6%** | **7.6%** |

The 22.0% that is neither mechanical nor ruling is the four middle rows: planning, reading child
output, startup, and verification.

### Why it compounds

Cost behaves as turns × context-size-at-that-turn, because every request re-reads the whole
conversation from cache.

| Figure | Value |
| --- | --- |
| Context growth after startup | ~1,020 tokens per turn, near-linear |
| First half (turns 1–92) — average context | 94,027 tokens |
| Second half (turns 93–184) — average context | 174,210 tokens |
| Second half's cache-read cost vs the first half's, at identical turn counts | 1.85× |
| Startup preamble | 36,546 tokens — 16% of the final context, loaded in turn 1 |
| Times the preamble was re-read | 183 |
| Share of the bill spent solely re-reading the preamble | 19.5% |
| Largest single context event after startup | +6,021 tokens — the preamble is 6× that, and larger than the next nine events combined |

A late poll is not a small turn. By turn 160 the context was ~199K tokens, so every turn paid to
re-read all of it before doing anything at all — one `capture-pane` poll at that point cost about
as much as the session's entire first four turns.

**The negative result that matters:** tool-result bloat was *not* the lever. All 166 tool results
together came to ~28.8K tokens *(estimate, chars/4)* — about 13% of the final context. The
coordinator was disciplined with `head`/`tail`/`--jq` filters. Capping every tool result would have
saved little and risked truncating the ticket bodies the plan depended on. The context was
dominated instead by the startup preamble, the coordinator's own accumulated output, and ~56.6K
tokens of attachment records *(estimate)*.

### What the replay showed

Removing the five mechanical categories and replaying the remaining turns against a context that
only grows from the turns that are kept:

| Figure | Value |
| --- | --- |
| Turns removed | 127 of 184 |
| Turns retained | 57 |
| Final context after removal | 82,360 tokens, down from 224,094 — −63% |
| Coordinator cost removed | ~81% |
| If the mechanical turns are re-priced onto a Sonnet-class worker instead of scripted away | ~68% removed instead of ~81% |
| Halving the startup preamble, on its own | ~10% removed |

The saving is superlinear: removing a turn removes both its own cost *and* its contribution to the
context that every later turn re-reads. This is the arithmetic behind ADR-0001's decision to move
mechanical work into deterministic scripts rather than onto a cheaper model — scripts remove the
turn, a cheap model only re-prices it.

## How it was measured

1. **Transcript, deduplicated by request.** The coordinator session was identified in the
   operator's local Claude Code project directory by the ticket numbers and wave vocabulary it
   contained, and confirmed three ways: its first user message is the `/orchestrate` invocation
   itself; its final turn's context matches the status line the operator saw; and `isSidechain` is
   false on every assistant line, so no subagent cost hides inside it.
   **The correction that changes every number:** the file has 371 `type:"assistant"` lines but only
   184 distinct `requestId` values. Claude Code writes one line per *content block* and stamps the
   identical `message.usage` object onto every one of them. Summing usage over raw lines
   double-counts by roughly 2×. Every figure here is deduplicated by `requestId`; `usage.iterations`
   has length 1 on all 184 requests, so the top-level usage object is not itself an aggregate.

2. **Classification by the tool calls a turn issued.** The 184 requests were classified by an `awk`
   rule set, applied in order, into the ten categories tabled above: startup = turn 1 plus reads of
   the skill's reference files; judgment = `AskUserQuestion` / `SendMessage` / text turns opening
   with a ruling; status narration = text-only turns with no tool call; decision-log = any command
   touching the decisions file; dispatch = worktree/agent-launch/bridge/tmux/prompt-file writes;
   polling = the monitor script, `tmux capture-pane|list|has-session`, sleeps; child output = reads
   of receipts or `git diff|log`; git/gh = merges, pushes, commits, branch and issue CRUD;
   verification = the tree validator, lint, tests.

3. **Per-turn bill from the exact usage figures.** Each turn's cost is
   `cache_read × read_rate + cache_creation × write_rate_1h + output × output_rate`, where the read
   rate is 0.1× base input, the 1-hour write rate is 2× base input, and the rates come from the
   published model table for the coordinator's model. The flat rate card is used throughout, so
   every share in this document is unaffected by the one pricing ambiguity below.

4. **The replay.** Each counterfactual replays the real per-turn `cache_creation` and
   `output_tokens` but recomputes the cache-read term against a context that grows only from
   retained turns. Removing the five mechanical categories yields 57 retained turns and a final
   context of 82,360 tokens; re-pricing the removed 127 turns onto a cheaper model instead of
   deleting them yields the smaller saving in the replay table.

### Caveats, as recorded in the source

- **The coordinator model's long-context pricing is not documented** in the available rate table.
  The absolute bill was reconstructed, and the reconstruction matched the observed total to ~1%,
  by assuming a 2× input-side premium on the 24 requests whose context exceeded 200K tokens. Every
  share-of-cost figure in this document is computed on the flat model and is insensitive to that
  ambiguity.
- **Token counts from `message.usage` are exact; tool-result and attachment token counts are
  chars/4 estimates** and are marked as such.
- **Category boundaries are heuristic.** In particular, status narration and decision-log
  bookkeeping are arguably one activity — "telling somebody what just happened" — which would make
  that the single largest line item at 29.5% of the bill rather than two items of 14.5% and 15.0%.
  Either way both sit inside the mechanical bundle.
- **Child-session cost was out of scope** and is unmeasured here. That gap is exactly what the cost
  instrumentation in this repo now closes.

## Grading a future run against this

A run under the current design leaves its own cost record in its artifacts: one `session-cost`
event per child in the machine log, and a coordinator/run rollup in the run report. To grade:

1. Take the coordinator's own token/cost figures from the run report.
2. Split them the way this baseline splits them — mechanical vs ruling — using the category rules
   in method step 2.
3. The judgment-only claim holds when the mechanical share is far below this baseline's 70.4%, and
   when bookkeeping, dispatch and polling cost the coordinator no turns at all rather than cheaper
   ones. Watch the turn count and the final context, not just the total: this baseline's lesson is
   that a turn's price is set by when it happens, not by how much it says.

## First `/crew` run on v0.2.1 — context, not baseline

Recorded here because it is the first datapoint on the new design, measured the same way (by hand,
from transcripts) and therefore comparable to the baseline in method only, not in scope.

- Coordinator finished 6 tickets across 3 waves at 130.5K context — 13% of the window, against this
  baseline's 224,094 tokens for 13 tickets.
- The coordinator's bill was roughly two thirds of what the six children cost between them.
- Qualitatively consistent with ADR-0001's goal, but not provable from run artifacts: that run
  predates the cost instrumentation.

Absolute spend figures are deliberately absent from this document. The repo's tree validator
([`scripts/validate_plugin_tree.py`](../scripts/validate_plugin_tree.py), rule `SPEND_FIGURE`)
rejects currency amounts anywhere in the tree, so this baseline is expressed as shares and ratios
throughout — which is also what a future run needs in order to be compared against it.

## Redaction record

Removed from the source document, none of it load-bearing for any figure above:

- **Every currency amount**, replaced by its share of the bill. The source states each category's
  cost in dollars; the shares reproduced here are the source's own, not recomputed.
- **The transcript's path, filename and session id**, and the private project directory containing
  it — replaced by "the operator's local project directory".
- **The repository under work and its ticket numbers** — replaced by the ticket count.
- **An adjacent session** in the same directory, excluded from every figure in the source and
  mentioned here only so a reader knows the scope boundary was deliberate.
- **Part 2 of the source document**, a mechanism map of cheap-orchestration options with citations.
  It is design input, not measurement, and its conclusions are already recorded in the ADRs.

Two redactions need no judgment and are enforced rather than remembered: the repository is named
nowhere here, and no currency amount appears anywhere — run
`python3 scripts/validate_plugin_tree.py` after any edit to this file.
