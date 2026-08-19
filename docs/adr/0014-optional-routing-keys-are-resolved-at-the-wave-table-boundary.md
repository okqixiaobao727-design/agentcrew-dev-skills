---
status: accepted
---

# An optional routing key is resolved to a concrete value where the wave table is built

A ticket's `## Routing` section is advisory input; the validated wave table is the run's sole
routing authority ([ADR-0003](0003-script-composed-first-turns-wave-table-authority.md), as amended
by [ADR-0010](0010-the-driver-runs-the-run-the-coordinator-rules.md)). Until now that rule only had
to answer *which* copy wins. Every routing key except the review line was required, and the review
line's absence means a workflow that takes no review — nothing downstream has to turn "absent" into
a value.

The **account** key (#95) is the first optional routing key whose absence *means* something
concrete: "the coordinator's account". Three consumers need that meaning as an actual value rather
than as a rule they each remember — the dispatch renderer, which must set the child's window
environment; the dashboard, which must know which profiles to read a child's liveness and cost
from; and the machine log, whose launch line is the run's only record of where the money went.

So the question is not which copy wins, but **where "unspecified" becomes "specific"**.

## Considered Options

- **Resolve at launch: carry the key as absent all the way down, and fall back to the coordinator's
  environment in the launch path.** Smallest change, and it puts the fallback at precisely the line
  that produced #95 — a child inheriting the dispatcher's account because nobody said otherwise.
  The correct implicit fallback and the incorrect one would then be the same code shape, so no
  future reader can tell by looking which they are dealing with. It also obliges every consumer to
  repeat the rule, and each one is free to get it wrong on its own. Rejected.
- **Resolve when writing the machine log**, leaving the table with the key absent. The table and
  the log then answer differently for the same ticket, which is a second live authority beside the
  wave table by another route — the thing ADR-0003 exists to prevent. Rejected.
- **Normalise at the wave table build. Chosen.** One site turns "unspecified" into "specific", so
  there is one place to read, one place to test, and one place that can be wrong.

## Decision

The driver resolves every optional routing key to a concrete value as it builds the wave table.
After validation the table carries no absent routing key and no sentinel meaning "use the default";
every consumer downstream reads a value.

For the account key specifically, the value an unnamed ticket resolves to is the coordinator's own
account, and the run section carries the coordinator's configuration home to make that resolvable —
the same class of fact as the coordinator's name and pid it already holds, captured once at start.

Optionality therefore lives in exactly one place: the ticket, and the staging-time validation that
reads it. It does not survive the table.

## Consequences

- The dispatch renderer's validation, which requires every routing key on every ticket, is
  unchanged. No optional-key handling is added downstream, and none should be.
- The rule generalises: a future optional routing key inherits this treatment rather than
  re-litigating it, and "carry the absence downstream" is now a recorded rejection rather than an
  open option.
- A resumed run reads its account assignment out of the table it already has, so a ticket cannot
  change account across a restart. Nothing needs to remember how the resolution was originally
  made, because the answer, not the rule, is what was written down.
- **Normalising the data does not force rendering it.** The intent is that the table's account
  column is drawn only where a run spans more than one account, so a single-account run's rendered
  table is unchanged even though every row now carries a concrete account. That rendering is not
  built: the approval table `/route` presents draws no account column yet, and doing so is
  follow-up work under this feature's parent. Uniform values are a display question; the stored
  value is not.
- The staging script inherits this for free: it does not reimplement routing validation or the
  wave-table build, it calls the driver's own functions, so a ticket staged by `/route` is
  normalised by the same code that will normalise it at `/crew`.
