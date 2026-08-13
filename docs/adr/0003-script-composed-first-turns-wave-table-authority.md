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

The coordinator's pid (the trust anchor) is known before launch, so no post-launch
injection channel is needed. tmux `paste-buffer` was measured to submit one turn per
pasted line — it shreds a multi-line brief — and is rejected as an injection route.

## Consequences

- **Model names are always full IDs, never aliases.** Measured: `--model haiku` under
  `--permission-mode plan` silently resolves to Sonnet; the full ID resolves correctly.
  A silent downgrade — or a silent *upgrade* onto an expensive model — defeats the
  routing this whole design exists to enforce.
