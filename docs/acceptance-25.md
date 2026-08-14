# Acceptance checklist — #25: Check in the redacted predecessor cost baseline

Ticket: `/Users/simon/Documents/coding/skills/agentcrew-dev-skills/features/crew-first-run-defects/tickets/25.md`
Spec: `/Users/simon/Documents/coding/skills/agentcrew-dev-skills/features/crew-first-run-defects/spec.md`
Branch: `worktree-25-25`
Worktree: `/Users/simon/Documents/coding/skills/agentcrew-dev-skills/.claude/worktrees/25-25`
Deliverable: [`docs/cost-baseline.md`](cost-baseline.md) — currently a **draft skeleton**.

This ticket cannot be closed by an agent: its first acceptance criterion is that the operator
supplies the redacted document and approves its contents for a public repo. Everything an agent
could settle is below under **Verified**; everything left is under **Assumed**, each with the exact
step that settles it.

## Verified

- **The baseline is not in the repo today.** `grep -rn "70.4\|7\.6%" . --include="*.md" -l` from the
  worktree root returns only `docs/adr/0001-coordinator-spends-tokens-only-on-judgment.md`; a
  `grep -rn "baseline\|predecessor"` across `docs/`, `README.md`, `AGENTS.md`, `CONTEXT.md` returns
  no cost baseline — only two unrelated uses of the word "baseline" in `docs/merge-driver.md:97`
  and `docs/design.md:156`. So the ticket's gap is real and no earlier copy is being duplicated.

- **The figures ADR-0001 already publishes.** Read
  `docs/adr/0001-coordinator-spends-tokens-only-on-judgment.md` lines 7–15 and 30–32: 69% of turns
  and 70.4% of the bill mechanical, 7.6% rulings, 127 of 184 turns and ~81% of cost removed by the
  replay, 36.5K-token preamble re-read 183 times for 19.5% of the bill. Every number in the
  "Headline figures" table of `docs/cost-baseline.md` is transcribed from those lines — no figure in
  the draft is invented, and the operator's job on that table is confirmation, not authorship.

- **The ticket/spec live outside the repo, so the baseline must land in tracked `docs/`.**
  `.gitignore` ends with `features/` ("A crew run's own working directory … never part of it"), so
  `features/crew-first-run-defects/` is untracked. The deliverable was therefore placed at
  `docs/cost-baseline.md`, beside the other audience-facing docs.

- **Absolute spend figures cannot be published, and the draft no longer contains any.** A first
  draft carried the first-run dollar amounts from the spec's "Further Notes";
  `python3 scripts/validate_plugin_tree.py` rejected it with
  `docs/cost-baseline.md:81: <amount> — spend figure is not public` (the amount is elided here for
  the same reason). The rule is `SPEND_FIGURE` in
  `scripts/validate_plugin_tree.py:74-85` and it bans currency amounts tree-wide. The draft now
  states shares and ratios only, and the validator passes:
  `python3 scripts/validate_plugin_tree.py` → `plugin tree OK`. This closes what would otherwise
  have been an operator judgment call ("are the dollar figures publishable?") — they are not.

- **The draft does not trip the legacy-name rule.** The predecessor command is named twice, both
  times inside a code span, which `tests/test_validate_plugin_tree.py:397-406` documents as the
  permitted form; the bare spelling is rejected. Confirmed by the passing validator run above.

- **The rest of the suite is unaffected.** `python3 -m pytest tests -q` → 148 passed, 83 subtests
  passed, 2 failed. Both failures are
  `tests/test_validate_plugin_tree.py::ValidatePluginTreeTests::{test_residue_outside_a_git_repo_is_rejected,test_identifier_in_git_internals_is_accepted}`
  and both fail inside `shutil` with
  `NotADirectoryError: … /plugin/.git` — the tests copy the tree including `.git`, which is a *file*
  in a git worktree rather than a directory. They fail for the checkout shape, not for this change.
  To confirm on the human's side, re-run the same two tests from the main checkout
  (`/Users/simon/Documents/coding/skills/agentcrew-dev-skills`), where `.git` is a directory; a
  pass there settles it.

- **Draft written and committed.** `docs/cost-baseline.md` exists on branch `worktree-25-25` with a
  DRAFT banner, a "What was measured" table, the headline-figures table, the "How it was measured"
  step list, a grading procedure against #23's `session-cost` events, the first-run context block,
  and a redaction record — with every operator-only line marked `TODO(operator)`.

## Assumed

Each item names what to do and what a pass looks like. **Do not tick one of these as Verified from
reading alone** — that is the single failure this checklist exists to prevent.

1. **The operator supplies the redacted predecessor document.**
   Do: open the operator-private forensics document for the predecessor `/orchestrate` run and work
   through `docs/cost-baseline.md` top to bottom, replacing each `TODO(operator)` marker.
   `grep -n "TODO(operator)" docs/cost-baseline.md` currently matches 9 lines: line 3 is the DRAFT
   banner naming the convention, the other 8 (lines 24, 25, 45, 55, 56, 58, 59, 96) are the markers
   to fill.
   Pass: after item 6 removes the banner too, that grep returns nothing.

2. **The headline figures match the private source exactly.**
   Do: compare the "Headline figures" table line by line against the source document. They were
   transcribed from ADR-0001, not from the source, so a transcription error in the ADR would
   propagate.
   Pass: every row matches the source, or the row is corrected and the mismatch noted in the
   redaction record (a figure that disagrees with an accepted ADR is a finding, not a typo —
   raise it before changing either file).

3. **The unaccounted 22% of the bill is explained or declared unexplained.**
   Do: 100% − 70.4% mechanical − 7.6% rulings leaves 22% the ADR never categorises. Fill the line
   under the headline table with what that remainder was, or state that the audit did not
   categorise it.
   Pass: the line no longer says `TODO(operator)`.

4. **The measurement method is restated without private material.**
   Do: fill the four numbered steps under "How it was measured" — transcript source, turn
   classification rule, per-turn bill computation, replay simulation. Where a step cannot be
   restated publicly, write "redacted" and why, rather than deleting the step.
   Pass: a reader who has never seen the private document could, given a transcript, reproduce the
   70.4% / 7.6% split by following the steps.

5. **The claim in the first-run context block is checked.**
   Do: the line "the coordinator's bill was roughly two thirds of what the six children cost
   between them" is a ratio derived from the spec's own figures. Confirm it against the operator's
   record of that run.
   Pass: the ratio is confirmed, or corrected to the true one — still as a ratio, never an amount.

6. **The operator approves the file for a public repo.**
   Do: read the finished `docs/cost-baseline.md` as an outsider would, then delete the DRAFT
   banner block at the top (the blockquote ending "…see `docs/acceptance-25.md`").
   Pass: no DRAFT banner, and `python3 scripts/validate_plugin_tree.py` prints `plugin tree OK`
   (re-run it — the residue rules only ever saw the draft's wording).

7. **The doc is reachable, so a future audit can cite it.**
   Do: add it to the README docs index (`README.md`, the `## Docs` list at lines 208–212), e.g.
   `- [`docs/cost-baseline.md`](docs/cost-baseline.md) — the measured predecessor baseline ADR-0001
   was decided on.` Decide separately whether ADR-0001 should gain a pointer to it; the ADR is
   accepted, so that edit is the operator's call.
   Pass: `grep -n "cost-baseline" README.md` returns the new line, and
   `python3 scripts/validate_plugin_tree.py` still passes (it checks markdown link targets resolve).

8. **This checklist is removed before the branch lands.**
   Do: `docs/` ships to users; an acceptance checklist does not belong there. Once items 1–7 are
   done, `git rm docs/acceptance-25.md` in the landing commit.
   Pass: the merged tree contains `docs/cost-baseline.md` and no `docs/acceptance-25.md`.

9. **The full suite passes on the main checkout.**
   Do: from `/Users/simon/Documents/coding/skills/agentcrew-dev-skills`, after merging, run
   `python3 -m pytest tests -q`.
   Pass: 0 failures — in particular the two `.git`-shape failures noted under Verified do not
   appear outside a worktree.
