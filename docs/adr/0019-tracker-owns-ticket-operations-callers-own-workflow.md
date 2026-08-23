---
status: accepted
---

# Tracker owns ticket operations; callers own workflow

Tracker knowledge is currently divided between route staging and the Driver. Stage implements
GitHub and local read, edit, mark and comment behaviour, while the Driver separately implements
close behaviour, pickup-label handling, local status rewriting and the close's exact undo. The
same tracker kinds, status syntax and external failure meanings therefore have more than one
owner.

The split also loses a local ticket's identity across staging. Stage reads the original local file
but materialises only its text into `crewtask/<n>/<ticket>.md`; the Driver later treats that copy as
the ticket path and closes it. The file originally selected from the local tracker is not carried
into the run and cannot be the file the Driver closes. This is a source-identity defect, not a new
live-synchronisation requirement.

## Decision

One **Tracker module** owns the complete implementation of the tracker operations `read`, `edit`,
`mark`, `comment` and `close`. Reading includes tracker-native child discovery and resolving the
status of a referenced ticket. Stage decides which reads and writes its staging workflow requires;
the Driver decides when a merged ticket is closed. Neither caller knows how a tracker performs an
operation.

The Tracker interface returns one normalised ticket value containing the common facts callers
need and an opaque, persistable locator. Callers may carry that locator but never parse or construct
it. Stage preserves the original locator when it materialises a ticket, the Run plan carries it as
an uninterpreted ticket fact in the existing Wave-table representation, and the Driver gives it
back to Tracker when closing the ticket. A local close therefore targets the original tracker file,
not the staged copy; a GitHub close targets the original issue.

`close` owns the complete tracker-specific effect and returns the exact undo the Machine log
records. For the local adapter that includes leaving the integration checkout clean after changing
the tracked ticket file. Tracker-specific path validation, status-line and pickup-label semantics,
CLI invocation and external error interpretation remain inside the module.

The module has two internal adapters because two implementations are exercised end to end:
GitHub, through `gh`, and local, through files in the repository. They produce the same observable
operation semantics. The public interface does not expose the adapters or a generic command
runner; tests use private seams inside the module.

Only GitHub and local are supported now. `references/trackers.md` currently claims that GitLab,
Jira, Linear and other trackers work from convention-document commands, while preflight rejects
every kind except GitHub and local. The implementation and documentation will state the actual
support contract. A future tracker is added as another adapter without changing Stage, Driver or
Run plan callers; this decision does not build a plugin registry for hypothetical trackers.

The Run plan continues to own ticket content, dependency and routing meaning. Tracker does not
read the Machine log, decide dependency closure, select workflow actions, advance waves or decide
when a ticket should close. It also introduces no live ticket synchronisation, cache, retry matrix,
generic subprocess framework or second ticket store.

This is a replacement, not a compatibility layer. Once callers use the Tracker interface, the
tracker constants, parsing rules, GitHub/local branches and operation helpers in Stage and Driver
are deleted.

## Consequences

- GitHub and local behaviour can change in one place without coordinated Stage and Driver edits.
- The local ticket selected for staging remains the ticket closed after a successful run.
- Tests exercise observable tracker operations through the same interface callers use; existing
  `gh` stubs and temporary Git repositories remain internal test dependencies.
- Adding a tracker requires one real adapter and its contract tests, not new conditionals in every
  workflow caller.
- Run-plan work must preserve the locator as opaque data; it does not acquire tracker semantics.
