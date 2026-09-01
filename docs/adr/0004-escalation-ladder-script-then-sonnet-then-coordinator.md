---
status: accepted
---

# Mechanical failures climb a fixed ladder: script → budget-capped headless Sonnet → coordinator

Clean merges and routine outcomes are handled by scripts alone. A textual merge conflict
with no design disagreement is resolved by the script itself, which keeps both sides'
insertions and costs nothing. A mechanical failure a script cannot resolve — that conflict
where the file's own markers defeat the rewrite — is handed to a headless, budget-capped,
cheap-model repair session that the *script* launches — the coordinator is not woken and
pays nothing. Only a double failure, or a conflict the rules classify as semantic (two
children's designs disagree), escalates to the coordinator.

> **Amended (advisor experiment, 2026-08-26).** The Sonnet rung now also runs *ahead* of the
> coordinator on every escalation, as the **witness brief**: a script launches a fresh,
> budget-capped cheap session that checks each pointer the child cites and marks it held,
> contradicted or missing. It never rules; it makes the "coordinator rules from the message alone"
> contract tenable without the coordinator's own reads, which the amended
> [ADR-0010](0010-the-driver-runs-the-run-the-coordinator-rules.md) measured as pure cost.

> **Implemented (#163, 2026-08-30).** The Witness remains one fresh,
> read-only, budget-capped module behind one CLI seam, with two named operations. `check` replaces
> the unnamed `--escalation` form and verifies one escalation from explicit resolved run values;
> `ask --run <run-dir> --ticket <NN> --question <text>` resolves the active Run plan's frozen
> worktree, account, model and budget before answering a coordinator's factual question. Both
> operations return the `brief`/`checked|partial|failed` envelope, share one execution path and
> tracker-comment trust rule, and never recommend or rule. Claude returns mode-specific structured
> output under a JSON schema; Python validates its pointer semantics and renders the brief, so
> extra model prose cannot become protocol. The old unnamed CLI form receives no compatibility
> alias, and this change adds neither a resident witness nor a second witness module.

> **Amended (#175, 2026-09-01).** An escalation `check` receives Python's normalised expected
> pointer list as numbered prompt input. `checked` carries every finding in a non-empty brief and
> no reason; `partial` carries the usable covered findings in a non-empty brief and a reason naming
> every omitted or structurally rejected pointer. Repeated and out-of-order expected pointers stay
> out of the brief, while extra cited pointers become uncited findings. Only a zero-coverage result
> is `failed` with an empty brief. The Witness result
> and Machine-log event carry required `covered_count` and `uncovered_count` integers, so neither
> the Driver nor the coordinator derives protocol facts from model prose.

## Considered Options

- **Resident cheap "executive" session** doing all mechanics: grows its own long-context
  bill (~13.6% of the measured run's bill, replayed) and adds a relay hop that distorts
  escalations. Rejected.
- **Coordinator-spawned repair subagents**: every exception costs the coordinator at
  least two late-context turns (~0.4–0.6% of the measured run's bill each, in pure
  prefix re-reading) to buy conversation context that a mechanical fix by definition
  does not need. Conflicts that do need that context are semantic and go straight to
  the coordinator anyway. Rejected.
