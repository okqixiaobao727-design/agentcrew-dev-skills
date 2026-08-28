---
status: accepted
---

# One process creates the Codex thread

Codex materialises a thread on disk only after its first user message, and that write is
asynchronous. A thread created through one app-server process therefore has no readiness signal
that says a second process can resume it from disk. v0.9.7 nevertheless moved the opening turn
from the TUI command line to an app-server client so it could carry a structured skill input, then
asked the TUI to resume that thread. Every launch failed before the first turn was durable.

v0.9.8 moved the turn earlier and polled `thread/read`, but a git working directory makes Codex
write an empty rollout before it writes `session_meta`. The poll treated that empty rollout as a
failure. Every AgentCrew child runs in a git worktree, so the second ordering could not launch a
child either. No Codex notification reports rollout durability, and matching its persistence
error text would make an internal race part of this bridge's protocol.

The handoff is unnecessary. Codex resolves a linked skill mention in the TUI's positional prompt:
`[$implement](/absolute/path/to/SKILL.md)`. The TUI turns it into the same first-class skill input
and injected `<skill>` block as an app-server `skill` item, even when the AgentCrew marker is the
line before it (`docs/research/codex-opening-skill.md`).

## Decision

One TUI process creates and owns the Codex thread. The pane starts its private app-server, then
starts the TUI with the marked prompt as its positional argument. A relaunch uses
`resume <thread-id> <prompt>`. The bridge never creates a launch thread, posts its first turn,
calls `thread/read`, reads a rollout, or matches Codex persistence errors.

When the message opens with `$<skill>`, the bridge resolves the installed `SKILL.md` exactly as it
does for `send`, replaces only that opening token with a linked mention, and leaves the rest of
the message byte-for-byte unchanged. Before starting the TUI, the pane calls `skills/list` with
the child's cwd and asserts that exactly one enabled skill has that linked path. A failed check is
written as one line in `app-server.log`; the TUI is not started and the failed runtime is retained.
Prompts without an opening skill do not call `skills/list`. Later `send` turns keep their
structured skill input because the bridge is already their app-server client.

The outer fresh `launch` connects to the pane's app-server and finds the new thread by its marker
in `thread/list` preview, tolerating absence until the startup timeout. Pane exit before discovery
fails the launch with the retained log tail. A relaunch already knows its thread id, so it keeps
the v0.9.6 contract: once the app-server accepts a connection, the outer process writes state and
returns that id without another pane-to-outer handshake.

The relaunch skill assertion is consequently asynchronous. It may fail after outer `launch` has
returned; the next `watch` then observes a successfully read pane list without that pane and
reports `vanished`, while the same reason remains in `app-server.log`. This is deliberate. A
ready-file receipt would recreate the bootstrap-result handoff in smaller form. Path or name
mismatches are deterministic and the run's fresh launch has already failed closed on them;
mid-run environment degradation can be reported by the existing liveness path.

## Consequences

- Thread identity comes from the TUI's own public preview, not Codex's private rollout state.
- Opening skills remain first-class Codex inputs without transferring a thread between clients.
- Fresh launches fail closed on silent skill-resolution failures; relaunches preserve the public
  state, `send`, and `watch` schemas and accept asynchronous assertion failure.
- The failed-launch log retention from #155 remains, as does #140's rule that only a successful
  pane-list observation can report `vanished`; the bootstrap result and its atomic write do not.
