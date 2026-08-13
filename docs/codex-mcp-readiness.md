# Codex app-server: when are a thread's MCP servers ready?

The Codex review bridge used to submit its first turn as the TUI's positional
`[PROMPT]` argument, so the turn began at TUI startup and raced the session's MCP
servers. Codex printed `MCP startup interrupted. The following servers were not
initialized: ...` and the review ran with tools that were still coming up
(issue #14).

Fixing that needed two facts the codebase could not supply: whether the
app-server announces MCP readiness at all, and whether the bridge can hold the
first turn until it does. Both were settled by probing a real
`codex app-server`, against **codex-cli 0.147.0**, on 2026-08-14. The protocol
itself is self-describing — `codex app-server generate-json-schema --out <dir>`
writes the full request, response, and notification schemas, and is the first
place to look when this behaviour changes.

## What the probes established

**MCP servers start per thread, not per app-server.** An app-server with no
thread on it starts nothing: 40 seconds of an idle connection produced no
startup traffic whatsoever. The socket appearing — the bridge's only startup
gate before this change — therefore said nothing about MCP at all. It could not
have: at that moment no MCP server had even been asked to start.

**Readiness is announced.** `mcpServer/startupStatus/updated` fires once per
server per transition, carrying `name`, `threadId`, and a `status` of
`starting`, `ready`, `failed`, or `cancelled`. A `thread/start` produced four
`starting` notifications in the same millisecond as the thread, and all four
reached `ready` about 1.1 s later. That stream is the readiness signal.

**Only the connection that started the thread hears it.** Notifications for a
thread the TUI created never reach the bridge's own connection. Neither
`thread/read` nor `thread/resume` subscribes it — `thread/resume` on a fresh
TUI thread fails outright with `no rollout found for thread id ...`. So the
bridge cannot watch a thread it did not start.

**`mcpServerStatus/list` is not a gate.** It looked like one: the first call
returned only after ~1.6 s, which reads exactly like blocking until ready. It
is not. A second identical call on an already-warm connection took the same
~1.55 s, so the delay is the call's own cost, not a wait for readiness. It is
still useful for one thing — it returns the configured inventory, which is how
the bridge distinguishes "no MCP servers configured" from "servers have not
announced yet".

**The inventory is not the announcing set.** The list returns five servers on
this machine; only four ever announce. `computer-use` never emits a startup
notification and never exposes a tool. Waiting for every configured server to
report ready would hang forever.

## The shape that works

Because only the thread's starter hears the announcements, the bridge has to own
the thread. Because a TUI cannot `resume` a thread with no rollout, it cannot own
it *and* hand it over — until the first turn gives that thread a rollout. That
ordering is the whole fix:

1. `thread/start` — the bridge owns the `threadId`, and this is what boots the
   thread's MCP servers.
2. Wait on `mcpServer/startupStatus/updated` until every server that has
   announced has left `starting`.
3. `turn/start` — the first turn, submitted through the same path follow-up
   turns already used.
4. The TUI attaches with `resume <threadId>`, which now succeeds because the
   turn created a rollout.

Verified end to end: the gate opened at 1.708 s, the turn ran, and the attached
pane rendered the whole exchange — prompt and reply both visible, pane alive.

Two things fall out of this. The bridge no longer hunts for its thread by
matching a marker against `thread/list` previews, because `thread/start` hands
it the id. And the human-visible pane now attaches to a review that is already
under way rather than starting one.

## Why the gate is not just "everything announced is ready"

The obvious rule — open once every server that has announced has left
`starting` — is not enough, and the follow-up path is what showed it. Reopening
a thread produced this:

```
[  0.140] node_repl, codex_apps, openaiDeveloperDocs -> starting
[  0.241] node_repl -> ready
[  0.309] code-review-graph -> starting      <- 169 ms late
```

`code-review-graph` announced itself *after* another server had already reported
`ready`. Had the first three settled before 0.309 s, the naive rule would have
opened the gate on a set that did not yet include the server the review most
depends on — the original bug, reintroduced as an intermittent one.

Waiting for the whole configured inventory instead is not available either,
because of `computer-use`: configured, never announces, never ready.

So the gate opens on a settled set only when one of two things is true:

- every configured server has been heard from — nothing can still be coming; or
- nothing new has arrived for `MCP_STARTUP_QUIET_SECONDS` (0.5 s, comfortably
  clear of the 169 ms stagger measured above).

The announcements remain the signal. The quiet period only decides when the set
of announcements is believed to be complete, and it costs half a second on
sessions where some configured server stays silent.

If this bug ever returns intermittently, the stagger is the first thing to
re-measure, and that constant is the thing to raise.
