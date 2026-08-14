# Acceptance checklist — #25: Check in the redacted predecessor cost baseline

Ticket: `/Users/simon/Documents/coding/skills/agentcrew-dev-skills/features/crew-first-run-defects/tickets/25.md`
Spec: `/Users/simon/Documents/coding/skills/agentcrew-dev-skills/features/crew-first-run-defects/spec.md`
Branch: `worktree-25-25`
Worktree: `/Users/simon/Documents/coding/skills/agentcrew-dev-skills/.claude/worktrees/25-25`
Deliverable: [`docs/cost-baseline.md`](cost-baseline.md) — **content complete, awaiting sign-off**.
Private source: `/Users/simon/Documents/coding/brainstorming/lead-cost-research.md` (1037 lines;
Part 1, lines 88–504, is the forensics this file redacts).

The ticket's first criterion is that the operator supplies the redacted document and approves it
for the public repo. The operator did not know where the source lived; it was located and the
redaction drafted from it, so what is left is the read-through and sign-off — not authorship.

## Verified

- **The private source was located and is the right document.**
  `rg -n '\b184\b|69%|7\.6|70\.4' ~/Documents/coding/brainstorming --glob '*.md'` returns
  `lead-cost-research.md` and nothing else. Its line 306 reads "127 turns (69%), <amount> — 70.4%
  of the bill", and line 299 gives the judgment row at 7.6% — the two figures ADR-0001 cites. Its
  subject line (line 7) names the `/orchestrate` run under audit. An earlier repo-wide search
  (`rg -l '70\.4' ~/Documents`) had found the figure only inside this repo, which is why the
  first draft was a skeleton.

- **Every figure in `docs/cost-baseline.md` is transcribed from that source, not derived.**
  Spot-check map: 184 requests / token totals / 72.0% cache-read share → source §0 (lines 99–118)
  and §2.3 (lines 226–234); the ten-category table → source §3.2 (lines 291–306); context curve,
  ~1,020 tokens/turn, the 1.85× half-vs-half figure, the 36,546-token preamble re-read 183 times
  → source §3.3 and §4 (lines 308–405); the replay (127 removed, 57 retained, 82,360 final
  context, ~81%) → source §5.1 and §5.4 (lines 416–475); the tool-result negative result → source
  §3.4 (lines 332–361); all four caveats → source §7 (lines 491–502).

- **The 22% ADR-0001 never explains is now accounted for.** Source §3.2's full table shows the
  remainder is planning 10.7% + reading child output 6.7% + startup 3.1% + verification 1.5% =
  22.0%, and 70.4 + 7.6 + 22.0 = 100. The categories are exhaustive, so the baseline is complete
  rather than a headline plus an unexplained gap.

- **Percentages carry over unchanged despite the pricing ambiguity.** Source §2.4 and §7 record
  that the model's long-context rate card is undocumented and the absolute bill was reconstructed
  to ~1%; all shares are computed on the flat rate card and are insensitive to it. The redaction
  publishes only shares, so it inherits none of that uncertainty.

- **No currency amount survives the redaction, and this is enforced.** A first draft carried the
  spec's dollar figures and `python3 scripts/validate_plugin_tree.py` rejected it
  (`docs/cost-baseline.md:81: <amount> — spend figure is not public`; rule `SPEND_FIGURE`,
  `scripts/validate_plugin_tree.py:74-85`). The file now states shares and ratios only and the
  validator passes: `python3 scripts/validate_plugin_tree.py` → `plugin tree OK`. This is why the
  source's dollar columns were dropped rather than redacted line by line.

- **The other redactions are listed in the file itself.** `docs/cost-baseline.md` ends with a
  "Redaction record" naming what was removed: currency amounts, the transcript path/filename and
  session id, the private project directory, the repository and its ticket numbers, an adjacent
  session already excluded from the source's figures, and the source's Part 2 mechanism map (design
  input, not measurement, and already reflected in the ADRs). Nothing removed is load-bearing for
  any published figure.

- **No `TODO(operator)` markers remain.** `grep -c "TODO(operator)" docs/cost-baseline.md` → 0.

- **The legacy-name rule is respected.** The predecessor command appears only inside code spans,
  the form `tests/test_validate_plugin_tree.py:397-406` documents as permitted; the validator run
  above confirms it.

- **The rest of the suite is unaffected.** `python3 -m pytest tests -q` → 148 passed, 83 subtests
  passed, 2 failed. Both failures are
  `tests/test_validate_plugin_tree.py::ValidatePluginTreeTests::{test_residue_outside_a_git_repo_is_rejected,test_identifier_in_git_internals_is_accepted}`,
  failing inside `shutil` with `NotADirectoryError: … /plugin/.git`: the tests copy the tree
  including `.git`, which is a *file* in a git worktree, not a directory. They fail for the
  checkout shape, not for this change — item 5 below re-runs them where `.git` is a directory.

## Assumed

Each item names what to do and what a pass looks like. **Do not tick one of these as Verified from
reading alone.**

1. **The operator reads the redaction against the private source and confirms it is faithful.**
   Do: open `docs/cost-baseline.md` beside
   `/Users/simon/Documents/coding/brainstorming/lead-cost-research.md` Part 1 (lines 88–504) and
   check the spot-check map under Verified above, line range by line range. The transcription was
   done by an agent; a misread number here becomes the baseline every future run is graded on.
   Pass: every figure matches, or the mismatch is corrected. If a figure disagrees with ADR-0001
   rather than with this file, stop and raise it — that is a finding about an accepted ADR, not a
   typo.

2. **The operator confirms the redaction is safe to publish.**
   Do: read `docs/cost-baseline.md` as an outsider would, with the "Redaction record" section in
   hand. The specific judgment only you can make: whether the run's shape — 13 tickets, waves up to
   6, a four-hour window on 2026-08-12/13 — is itself something you are willing to publish, and
   whether dropping the source's Part 2 is right (it is design input, not measurement).
   Pass: you are content that nothing identifies a private repository, a person, or an account.

3. **Approve: remove the pending-approval banner.**
   Do: delete the blockquote at the top of `docs/cost-baseline.md` (the three lines beginning
   "**Awaiting operator approval for publication.**"), then re-run
   `python3 scripts/validate_plugin_tree.py`.
   Pass: no banner, and `plugin tree OK`.

4. **Make it reachable, so a future audit can cite it.**
   Do: add a line to the README docs index (`README.md`, the `## Docs` list at lines 208–212):
   `- [\`docs/cost-baseline.md\`](docs/cost-baseline.md) — the measured predecessor baseline
   ADR-0001 was decided on.` Decide separately whether ADR-0001 should gain a pointer back; the ADR
   is accepted, so that edit is yours to make.
   Pass: `grep -n "cost-baseline" README.md` shows the new line and
   `python3 scripts/validate_plugin_tree.py` still passes (it checks that markdown link targets
   resolve).

5. **Land it.**
   Do: `git rm docs/acceptance-25.md` in the landing commit — `docs/` ships to users and a
   verification checklist does not belong there. Then, from the main checkout
   `/Users/simon/Documents/coding/skills/agentcrew-dev-skills`, run `python3 -m pytest tests -q`.
   Pass: the merged tree has `docs/cost-baseline.md` and no `docs/acceptance-25.md`, and the suite
   reports 0 failures — in particular the two `.git`-shape failures noted above do not appear
   outside a worktree.
