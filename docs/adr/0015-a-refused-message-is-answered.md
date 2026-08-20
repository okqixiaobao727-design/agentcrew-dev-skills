---
status: accepted
---

# A message the protocol refuses is answered, never silently dropped

The verb grammar is strict on purpose: a receipt is a whole line, matched whole, so prose *about*
a receipt can never settle a ticket. Strictness is not the defect. Silence is.

A finished ticket once stalled `waiting` for eight and a half minutes with a live, polling driver,
because its child appended a sentence to its `CREW COMPLETE` line. The line failed the pattern,
`final_verb` answered `(None, None)` — the same answer it gives a message that speaks no verb at
all — and the driver classified a receipt attempt as conversation. Nothing in the log, the
dashboard, or the child's session said a receipt had been refused. The run resumed only when a
human noticed and a coordinator spent a turn asking for the line again.

## Considered Options

- **Loosen the grammar** so a receipt with prose on its line parses. It would have settled this
  one message and opened the door to quoted and narrated receipts settling tickets, which is the
  failure the whole-line anchor exists to prevent. Rejected.
- **Leave it to the idle rung.** The nudge would eventually have prodded the child, so the run was
  never permanently stuck. But the wait is measured in the monitor's idle threshold rather than in
  the fault, and the operator reading `waiting` still learns nothing about why. Rejected as
  sufficient; the nudge stays as the rung for a child that said nothing at all.
- **Wake the coordinator on an unparseable message.** Judgment spent on a typo, against ADR-0001.
  Rejected.
- **Answer the refusal on the scripted rung. Chosen.**

## Decision

Every message the protocol refuses to act on, and that plausibly attempted a verb, produces a
scripted response to its sender. A refusal a sender cannot observe is a defect, not a design.

Concretely, the machine log distinguishes the two cases its grammar used to collapse: `final_verb`
still answers only for a line that parses, and `malformed_receipt` names the line that reached for
a verb and missed. The driver answers a near miss on ADR-0004's scripted rung — one bounce quoting
the offending line and stating the required shape, then `failed` — so the answer costs no
coordinator turn, and ADR-0010's rule stands: the driver settles what the written rules decide.

The obligation is on the protocol rather than on this one verb: a verb or a channel added later
inherits it, and adding a refusing rule without its answering rung is an incomplete change.

## Consequences

- ADR-0010's wake audit noted that "the invalid-receipt rung never fired" across three measured
  runs. It fires now, on the case that was silently swallowed before.
- The two ways a run can read `waiting` forever are now distinguishable: a dead driver (#103) and
  a live driver holding a refused message, which no longer stays quiet.
- The rule that decides whether a receipt parses is stated in the first turn that teaches the
  verbs, so following the instructions cannot produce an unparseable receipt. A coordinator ruling
  that asks a child to add information to its receipt puts that information above the verb line.
- The near-miss detector is deliberately narrow — a known verb word at the margin, failing its own
  pattern. Widening it would smuggle back the looseness the grammar refuses.
