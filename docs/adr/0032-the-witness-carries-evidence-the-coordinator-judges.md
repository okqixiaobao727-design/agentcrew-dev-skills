---
status: accepted
---

# The Witness carries evidence; the Coordinator judges

One run — `crewtask/21`, plugin 0.9.20 then 0.9.21 — produced three separate defect reports that
turn out to be one defect. On ticket #302 the brief marked a cited pointer **contradicted** on
reasoning it had inferred from naming: it matched the ticket's phrase "Call Agent" against the
first renderer file it found instead of reading the `Audience` enum that defines the term, and the
coordinator, told to treat the brief as the fact-check, ruled the wrong way on the largest ticket
of the run (#202). On four wrap-up checks the same Witness reported that no test or review record
existed anywhere it could read, in four different wordings, because the run keeps none (#203).
Under 0.9.20, seven of nine `check` calls returned `failed` with an empty brief, and the
coordinator ruled straight from the child's word on every one of them, calling `ask` not once in
the whole run (#200).

The common cause is the job the assignment gives a Witness. It is asked to *check cited facts*, to
*actively look for important omissions*, to *substantiate test claims from existing records*, and
to return `held`/`contradicted`/`missing` per pointer with a reason. Every one of those is a
judgment, and the session making it is a fresh, budget-capped Sonnet with no ruling context. A
judgment rendered there arrives at the coordinator wearing the same clothes as a quoted line —
there is no field in which the difference could be written — so the expensive model, whose only
job is judgment ([ADR-0001](0001-coordinator-spends-tokens-only-on-judgment.md)), inherits a
cheap model's conclusion and has no signal to look closer.

## Decision

**A brief states facts and never verdicts.** The Witness returns one list of entries, each a
`pointer` and what that pointer `says` — the source text itself. There is no status enum, no
`reason` prose, and no `cited`/`uncited` split: a pointer that resolves to nothing has an empty
`says`, which is the whole of what `missing` used to mean, and which of the entries the child
cited is something the coordinator already knows from the escalation in front of it. `check` and
`ask` return the same shape, so the coordinator learns one thing rather than two and the code
holds one schema rather than two. The point is not that a verdict would be unwelcome; it is that
after this change **there is nowhere to put one**. #202's ruling cannot recur because no field
can carry it, and #203's four findings cannot recur because a report that a record does not exist
has no pointer to name and no text to quote.

**What it gathers is the child's pointers plus one hop.** Each pointer the escalation cites, and
from each of those, the material directly attached to it: where a cited symbol is defined, how a
concept named in the ticket is defined in the project's own authority — glossary, `CONTEXT.md`, an
enum's docstring, an ADR — and the rest of the definition the cited line sits in. No survey of the
repository, and no search for what the child should have mentioned. Deciding *what to fetch* is a
cheap judgment the Witness cannot avoid and is trusted with; deciding *what it means* is the one
it is now structurally unable to make. One hop is also the exact step #202 skipped: had the
`Audience` enum been fetched because `delegated.py` cited it, the brief would have carried the
definition that settles the question and no verdict would have been needed from anyone but the
coordinator.

**A quotation is the innermost enclosing definition, capped at the coordinator's own read.** The
unit is the definition the pointer falls inside — the method, not the class that holds it — quoted
whole, and where that runs past **80 lines** it is cut to 80 around the pointer. The number is not
a new one: 80 lines is what the coordinator's own bounded read has always been, so the invariant
is that **no single pointer ever hands the coordinator more than it could have fetched itself**.
The cap needs no truncation marker, because under a rule that quotes whole below the limit, a
quotation of exactly the limit *is* the signal that there is more; and it needs no prepared
command, because the drill-down below is what a coordinator reaches for when it wants it.

Measurement decided both halves. Across the 39 briefs in `crewtask/69`–`73`, the ones that
returned content carried 2–14 pointers. Sampling 1,194 pointer positions across this repository's
Python, the innermost enclosing definition has a median of 18 lines but a p90 of 83 and a maximum
of 1,399 — a tail made entirely of large classes, which is why the unit is the innermost and not
the enclosing definition. Uncapped, a 28-pointer brief of p90 units projects to roughly 30k tokens
against a resident coordinator that sees about eight briefs a run; capped at 80, 88.9% of
quotations are unaffected, a typical brief costs under 3k tokens and a whole run's briefs about
23k, with a hard ceiling that can be computed from the pointer count rather than discovered.

**The coordinator drills within the table, and an empty table is laid again rather than ruled
around.** The old bounded read was one pointer per ruling, triggered by "what an escalation and its
witness brief state differently" — a condition that cannot arise once the brief states nothing to
differ with. It is replaced by reach rather than by count: any pointer already on the table — in
the brief, or cited by the escalation — may be read with an offset and at most 80 lines, as often
as the ruling needs, and nothing off the table may be read at all. `Grep` and `Glob` stay refused,
so the coordinator still cannot hunt; what bounds it is the table, and what bounds the table is
the one-hop rule. Where the table lacks what a ruling turns on, the answer is one `ask`, not a
private search — and where `check` returns `failed` or `partial`, that is an empty table, so one
`ask` is dispatched before ruling. Ruling with no brief survives as the last resort it always was,
after that attempt rather than in place of it, on the same one-retry rhythm the run already uses
for a bounced receipt and an idle child.

## Considered Options

- **Keep the verdict, add a basis field** (`quoted` vs `inferred`, as #202 proposed). Rejected: it
  asks the coordinator to discount a judgment rather than not receive one, and it grows the
  interface to describe a failure instead of removing it. The Witness that inferred wrongly is the
  same one that would grade its own basis.
- **A `from` field naming the pointer each one-hop entry came from**, making the hop auditable.
  Rejected: its consumer is a post-hoc auditor, not the caller, so it is interface growth that
  pays the coordinator nothing — and a required field on a strict schema is the shape that cost
  #175 an entire run of fact-checks, 5.2M tokens for zero briefs.
- **A fixed window of N lines around the pointer**, with a truncation marker and a prepared
  read command. Rejected: the window is a guess, and both additions exist only to compensate for
  guessing wrong. The definition unit removes the guess and both compensations with it.
- **No cap at all.** Rejected on the measurement above: the tail is real and lands on the one
  session whose context is worth protecting.
- **Requiring a ruling to declare which facts it verified** (#200's third proposal). Rejected as
  already recorded: a `witness` event carries its own `outcome`, and rulings are logged beside it,
  so a ruling that followed an empty brief is a join over facts the log already holds rather than
  a new obligation on the expensive session.

## Consequences

- The Witness assignment loses three instructions — check cited facts, hunt omissions, substantiate
  test claims — and gains the one-hop rule and the quotation unit. What it keeps is navigation,
  reference resolution, budget discipline, incremental submission and timeout retention: the
  interface shrank, the implementation did not.
- `check` and `ask` collapse onto one schema and one rendered shape, which removes a class of
  structural rejection along with the second schema.
- The coordinator's Contract changes in two places: the bounded read is bounded by reach rather
  than by count, and "rule without a brief" moves from a permission at the front to a last resort
  behind one `ask`.
- The brief no longer tells the coordinator where to look, so it must read the quotations it is
  given. This is the accepted cost, and it is measured against the status quo rather than against
  perfection: a coordinator that skims a page of true source text is wrong less often than one
  that follows a confident false verdict, which is what #202 cost.
- Nothing here records what a review found or what a child's tests did; the Witness no longer
  looks for either. Whether the run keeps a review's own report is a separate question, settled on
  the coordinator's needs rather than the Witness's (#203).
