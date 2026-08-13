# Cutting overlay

Rules that ride on top of `/mattpocock-skills:to-tickets`. They are **additions**: that skill is the
source of truth for how tickets get drafted, and everything it does on its own it keeps doing. Where
it is installed but not exposed as a slash command, read its `SKILL.md` and follow it directly.

**The convention document names where tickets land.** That skill's local-file branch names a path of
its own; the tracker settled in step 2 is the one that decides.

**Vertical slicing binds the code tickets.** A `tdd` or `refactor` ticket has layers to cut through,
so the vertical-slice rules bind it whole. A `direct`, `spike`, `ops`, or `acceptance` ticket has no
layers: it keeps the sizing rule — one fresh context window — and the bar that a finished ticket is
verifiable on its own, in whatever form its deliverable takes.

**Splits stay vertical-first, and a contract is the only exception.** Routing pressure — wanting a
core design decision to sit in a ticket of its own — buys no horizontal split. The exception is a
**contract** that two or more downstream tickets couple to (a schema, a protocol, a shared
interface): cut it as its own ticket ahead of them, which is what lets those downstream slices route
as non-core in step 4. The wide-refactor exception that skill already carries stays available.

**A contract ticket's acceptance criteria state the contract.** Name what downstream couples to —
the import path, the directory layout, the command that runs the suite. Criteria written as symptoms
of a working deliverable ("the suite exits 0") leave every slice behind it free to answer the same
question differently, and they collide once an edge case reaches them.

**One approval, one publication, both owned by the skill body.** Draft the slices, then stop: the
quiz material — title, blocking edges, what it delivers — becomes columns of step 5's table instead
of a separate checkpoint, and step 6 publishes with routing already in each body.
