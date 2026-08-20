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

## Amendment (#110): concrete means identity *and* execution mode

The first implementation of this decision resolved an account-less ticket to the coordinator's
configuration home and stopped there — a path, and nothing else. That reading of "concrete" was
wrong, and it broke two things at once.

`CLAUDE_CONFIG_DIR` set to the default home and `CLAUDE_CONFIG_DIR` left unset name the same
directory and are **not** the same login: the explicit spelling failed the credential lookup the
inherited one succeeded at, so account-less reviewers and merge-repair sessions were told `Not
logged in` on a machine whose operator was signed in. Meanwhile the Claude wake monitor, which
never received the account dimension at all, polled one live-agents list for a whole wave and
called a child on a second account `vanished` ten seconds after launching it, settling a live
ticket `failed`.

Both are the same missing distinction, so the resolved value is now a **binding** of two facts:

- the **resolved configuration home** — what identifies the account, what a child's transcript,
  cost and session files are read at, and what the machine log records for attribution;
- the **execution mode** — `explicit` for a ticket that named an account, `inherited` for a ticket
  that named none.

A ticket that named no account still carries a directory, because observation and attribution need
one; what it no longer carries is an instruction to set that directory as an environment. Every
Claude process of the ticket — implementer, reviewer, merge-repair session and wake monitor —
takes its environment from one shared contract (`accounts.environment_delta`), which answers with
the one variable for an explicit binding and with nothing at all for an inherited one. No consumer
re-derives the distinction from a path, a name, the coordinator's home or its own environment.

The rest of the decision stands. Optionality still dies at the wave table, the table still carries
a value on every row, and "carry the absence downstream" is still a recorded rejection — what the
row carries is simply the whole of the resolved value rather than half of it.

One consequence is accepted knowingly. An account-less child's tmux window is now created with no
`-e` pair, so its login is whatever the multiplexer server's environment holds rather than the
home the run recorded. On a machine whose operator runs no `CLAUDE_CONFIG_DIR` — every
single-account machine — the two are the same directory and the inherited one is the only one that
authenticates. On a machine whose coordinator runs under a non-default home *and* whose tmux server
does not carry it, an account-less child could start on the default login instead: the answer there
is to name the account on the ticket, which is what an explicit binding is for. Restoring the
window variable "just in case" would put every account-less run back on the spelling that fails.
