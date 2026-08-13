---
status: accepted
---

# Mechanical failures climb a fixed ladder: script → budget-capped headless Sonnet → coordinator

Clean merges and routine outcomes are handled by scripts alone. A mechanical failure a
script cannot resolve (canonical case: a textual merge conflict with no design
disagreement) is handed to a headless, budget-capped, cheap-model repair session that the
*script* launches — the coordinator is not woken and pays nothing. Only a double failure,
or a conflict the rules classify as semantic (two children's designs disagree), escalates
to the coordinator.

## Considered Options

- **Resident cheap "executive" session** doing all mechanics: grows its own long-context
  bill (~13.6% of the measured run's bill, replayed) and adds a relay hop that distorts
  escalations. Rejected.
- **Coordinator-spawned repair subagents**: every exception costs the coordinator at
  least two late-context turns (~0.4–0.6% of the measured run's bill each, in pure
  prefix re-reading) to buy conversation context that a mechanical fix by definition
  does not need. Conflicts that do need that context are semantic and go straight to
  the coordinator anyway. Rejected.
