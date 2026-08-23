---
status: accepted
---

# Machine log owns run facts; Driver owns workflow policy

The run's current facts were being reconstructed independently by the Driver, monitor, advance and
merge driver. Their answers all came from one ordered Machine log, but the ordering rules lived in
the readers, so changing one fact could leave different parts of the same run with different
answers. We will deepen the existing Machine log module without changing any command, output,
state meaning or execution order.

## Considered Options

- **Derive run facts from the Machine log alone. Chosen.** History remains one authority, while the
  Wave table remains the authority for what the run planned.
- **Combine the Machine log and Wave table into one projection.** Rejected because facts about what
  happened and plans for what should happen would change together and recreate the coupling this
  decision removes.
- **Let the projection decide the Driver's next action.** Rejected because ADR-0010 already gives
  the Driver and its rule table that responsibility; moving it would turn a factual reading into a
  second workflow engine.
- **Keep each reader's local helpers.** Rejected because the same event order would remain an
  undeclared interface copied across readers.

## Decision

A **Run projection** is the current factual reading derived solely from one run's ordered Machine
log. The Machine log module owns the shared rules for reading records and deriving those facts.
It does not read the Wave table, choose the next workflow action or translate facts into the
human-facing Ticket state.

The Driver combines the Run projection with the Wave table and its rule table to choose what the
run does next. The monitor retains presentation-only Ticket state mapping. This is a
behaviour-preserving architecture change: existing commands, outputs, state meanings and execution
order are the acceptance contract.

## Consequences

- A shared event-order rule has one owner. A caller may act on a projected fact, but does not
  independently reinterpret the same event sequence.
- Wave-table planning remains a separate module candidate; this decision does not introduce an
  adapter between the two or a second state framework.
- A defect or further architecture opportunity discovered during the change is recorded with its
  evidence, impact and recommended disposition. It is neither silently ignored nor fixed inside
  this behaviour-preserving change. If it makes behaviour preservation impossible, work stops for
  a ruling.
