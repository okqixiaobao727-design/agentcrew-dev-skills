---
status: accepted
---

# The coordinator spends model tokens only on judgment

Forensics on a real `/orchestrate` run (Fable 5 coordinator, bill measured from the
transcript) showed 69% of coordinator turns and 70.4% of its bill were dispatch, polling,
bookkeeping, status narration, and git plumbing; rulings — the only work that needs the
expensive model — were 7.6%. Cost behaves as turns × context-size-at-that-turn, so every
late mechanical turn also pays to re-read the whole prefix. We decided the coordinator's
only model activity during a run is judgment: approving the opening wave table, answering
escalations, and ruling on conflicts. Everything mechanical belongs to deterministic
scripts. Replayed against the measured run, this removes 127 of 184 coordinator turns and
~81% of the coordinator's cost.

## Consequences

- **Zero polling** is a contract term: children message the coordinator directly; scripts
  watch everything else and wake the coordinator only for exceptions.
- **Waves auto-advance** when all receipts are green. The user pre-approved the plan; the
  coordinator is interrupted only by escalations and failures, and the human can always
  interrupt manually.
- **Bookkeeping costs zero turns.** Scripts append every event to the machine log;
  SendMessage hooks on both sides copy child escalations and coordinator rulings in
  verbatim. Nobody writes prose status reports — the human watches a script-rendered
  dashboard pane plus milestone/exception toasts (both invisible to the model).
- **The resident prefix shrinks.** The crew skill body slims to the judgment core
  (contract, ruling loop, escalation grammar, reference index); first-turn templates and
  workflow shapes move into the dispatch renderer, watch/merge rules into the monitor —
  the coordinator never reads them again. (The measured 36.5K-token startup preamble was
  re-read 183 times, 19.5% of the coordinator's bill.)
- **Nothing is inserted into the live coordinator context mid-run** except what the
  coordinator itself chooses to Read. Claude Code writes 1-hour cache entries at 2× base
  price, so a mid-run insertion forces a 2× rewrite of everything after it. The
  coordinator instead gets a static reference index (paths + one-line descriptions, no
  contents) at session start, and escalation messages must carry their own file pointers.
  (Amended by [ADR-0010](0010-the-driver-runs-the-run-the-coordinator-rules.md): what the
  coordinator chooses to Read is itself bounded to one pointer, and the pointers arrive
  pre-checked by a witness brief.)
