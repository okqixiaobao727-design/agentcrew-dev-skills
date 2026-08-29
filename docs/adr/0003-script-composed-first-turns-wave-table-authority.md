---
status: accepted
---

# Child first turns are script-rendered and injected at launch; the wave table is the sole routing authority

The dispatch script renders each child's complete first turn (opening line, workflow
shape, review lane, coordinator trust anchor, ticket references) and injects it at launch
via `initialPrompt` in the `--agents` launch JSON — verified on this machine to
auto-submit in an interactive session. The coordinator no longer composes or reads child
briefs; it sees only the dispatch script's one-line-per-child confirmation.

Three contract promises change deliberately:

- **Single routing authority.** The approved wave table is the only source the renderer
  reads; a ticket's `## Routing` section becomes advisory input used to *build* that
  table, never a second live authority to reconcile.
- **Scripted verification replaces the visual check.** "Verify each child's header" is
  replaced by post-launch assertions: the child's entry in `claude agents --json` and the
  model field in its own transcript.
- **Accepted impersonation surface.** Any process that can write the renderer's inputs
  can author a child's first turn, trust anchor included. On a single-user machine that
  is equivalent to filesystem write access, which already wins; we accept it.

The coordinator's identity (the trust anchor) is known before launch, so no post-launch
injection channel is needed. It is the coordinator's socket address rather than its pid
([ADR-0023](0023-the-coordinator-is-addressed-by-socket-not-by-name.md)); what matters here
is only that it is known in time, which both forms are. tmux `paste-buffer` was measured to
submit one turn per pasted line — it shreds a multi-line brief — and is rejected as an
injection route.

## Amendment (ADR-0010)

The wave table is now built and validated by the driver rather than by the coordinator, and the
**validated** table is the run's routing authority: user invocation of `/crew` is the run's
sign-off, and the interactive approval step is gone
([ADR-0010](0010-the-driver-runs-the-run-the-coordinator-rules.md)). Everything above holds
unchanged — one routing authority, scripted verification, the accepted impersonation surface — with
"approved table" read as "validated table" throughout.

## Amendment (ADR-0014)

"Advisory input used to *build* that table" is sharpened: building it **resolves** every optional
routing key to a concrete value, so the validated table carries no absent key and no sentinel
meaning "use the default"
([ADR-0014](0014-optional-routing-keys-are-resolved-at-the-wave-table-boundary.md)). Optionality
lives in the ticket and in the staging-time validation that reads it; it does not survive into the
table. The single-routing-authority promise above is what forces this: a key left absent would
oblige each consumer to re-derive its meaning, which is a second authority reassembled downstream.

## Amendment (#154)

For github, the tracker is the ticket's only content authority. Staging writes a stub carrying the
title, live issue URL, `## Routing` and `## Blocked by`; the issue body and every comment remain at
the URL for each child, coordinator, witness and reviewer to read. Comments whose
`authorAssociation` is `OWNER`, `MEMBER` or `COLLABORATOR` are ticket direction; all others are
opinion. Local tickets remain files and are staged unchanged.

Two costs are accepted. In this public repository an authorised comment posted mid-run can change
direction without a separate human gate, with the author-association rule as the trust boundary.
There is also no per-run content snapshot, so two readers at different times can see different
ticket versions. A stale local copy was rejected because it silently discards later tracker
decisions, the failure #154 records.

## Consequences

- **Model names are always full IDs, never aliases.** Measured: `--model haiku` under
  `--permission-mode plan` silently resolves to Sonnet; the full ID resolves correctly.
  A silent downgrade — or a silent *upgrade* onto an expensive model — defeats the
  routing this whole design exists to enforce.
