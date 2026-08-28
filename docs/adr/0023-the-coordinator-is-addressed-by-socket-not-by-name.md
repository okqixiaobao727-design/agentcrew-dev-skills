---
status: accepted
---

# A child addresses its coordinator by socket, not by name

A run whose ticket names a second Claude account launches its child under a second
`CLAUDE_CONFIG_DIR`, and the `CREW ASK` channel between that child and the coordinator is dead in
both directions (#146). The cause is narrow: cross-session name resolution reads
`<config-dir>/sessions/<pid>.json`, and two accounts are two configuration homes holding disjoint
registries. The transport underneath — a Unix socket the harness binds per session — is
machine-wide and was never partitioned. Only the lookup was.

The harness accepts a second address form, `uds:<socket>`, that skips name resolution and takes the
identical send path. This repo already depends on it on the receiving side: the child's first turn
has always told a child that its coordinator's messages "arrive from `uds:…` — that socket is the
identity; the from-name is a session title, not an identity". The send side simply never followed
the identity the receive side already used.

## Considered Options

- **Address by name, and fall back to the socket when the ticket's account differs from the
  coordinator's.** Keeps today's spelling for the common run. Rejected: it makes the rare path the
  only one that exercises the code that has to work, and it obliges a child to work out its own
  situation before it can send. A fallback that is never run on the ordinary run is a fallback that
  is broken when it is finally needed.
- **Warn in preflight when the accounts differ**, and leave the channel as it is. Rejected: it is a
  mitigation for a broken channel, and once the channel works, cross-account is a supported
  configuration that nothing should warn about.
- **Merge or share the session registries** between accounts, by symlink or bind mount. Rejected:
  the registry is a harness-private index keyed by pid, and two accounts writing one directory is a
  collision waiting for a pid reuse. It also solves a lookup problem the socket address does not
  have.
- **Launch children at a deterministic socket path** with `--messaging-socket-path`. Rejected: the
  flag is in the binary's argument list but in neither `--help` nor the documentation, and nothing
  here needs a path chosen in advance.
- **Address by socket, uniformly, on every run. Chosen.** One rule, exercised by every child of
  every run, with no branch on whether the accounts differ. A ticket on another account then gets
  no dedicated code at all: it inherits the coverage of the path every run already takes.

## Decision

**The coordinator's address is the whole `uds:` address of the socket the harness bound for it, and
it is the only thing a Claude child is told to send to.** The coordinator's session name is carried
beside it as a human-readable label, and never as a thing to address.

Four properties make that one value rather than a rule each caller applies:

- **It is one string, scheme prefix included.** Consumers use it verbatim. A value assembled at
  each call site is the defect being removed, and half-assembling it is the same defect made
  smaller.
- **It is read, not composed.** The source is the `messagingSocketPath` field of the coordinator's
  own session registry entry, which the launch script already opens to resolve the coordinator's
  name and session. Composing `/tmp/cc-socks/<pid>.sock` instead — which two modules did — is wrong
  the moment the harness binds anywhere else, and it binds elsewhere under several ordinary
  conditions.
- **It is recorded exactly as the harness spelled it.**
  [ADR-0007](0007-paths-are-absolute-at-the-boundary-and-compared-by-realpath.md) makes paths
  absolute at the boundary and compares them by realpath; **this value is exempt from both.** It is
  an address, not a path to a file this repo opens: on macOS the default socket directory is
  reachable under two spellings, the harness's own reply-address handling treats them as distinct
  literals, and the spelling in the registry is the one the receiver bound.
- **It is resolved once, at the wave table boundary**, and carried on the run's metadata the way
  every other resolved routing fact is
  ([ADR-0014](0014-optional-routing-keys-are-resolved-at-the-wave-table-boundary.md)). A resumed
  run therefore cannot address a coordinator other than the one its start resolved.

The coordinator answers by replying to the address the message arrived from, which the harness
delivers inside the envelope. Neither side consults a registry, so neither side has a lookup that
can fail.

The coordinator's pid stays. It has uses unrelated to addressing — the dashboard pin, and the
driver's detection of a restart — so it is relieved of being a socket ingredient rather than
deleted.

## Consequences

- **[ADR-0003](0003-script-composed-first-turns-wave-table-authority.md)'s naming of the pid as the
  trust anchor is superseded.** The anchor is the address; the pid is what the dashboard pins. That
  ADR's substantive claim — that the anchor is known before launch and needs no post-launch
  injection channel — holds unchanged, because the address is known before launch too.
- **The re-anchor condition becomes the address.** A restarted coordinator binds a new socket, and
  the address is what a child was told to trust, so it is what decides whether live children need
  re-pointing. Comparing the pid was a proxy for it.
- **The machine log records every copied message's sender.** The harness exports each session's own
  inbox socket into that session's environment, so the `SendMessage` hook names the sender without
  a lookup. Ticket correlation then indexes a child by both identities the run knows it under — the
  name it was launched with and the address it has sent from — which also fixes a latent defect: a
  coordinator following the tool's documented "copy the `from`" instruction already broke
  correlation today.
- **A run already under way survives the upgrade.** The address is an optional, defaulted field on
  the run metadata and is refreshed from the launcher's arguments at every start, exactly as the
  coordinator's session ID is, so an old wave table heals itself on its first resume rather than
  needing a migration.
- **The Codex lane is untouched.** A Codex child has no message channel of its own — its bridge
  reads the final message of its turn — and renders a different protocol block.

## Known risks

- **Native Windows is out of scope, and this decision would not survive there.** The socket auth
  key is optional on macOS and Linux and required on Windows, and it lives in the sender's own
  configuration home — so on Windows the key is a genuine second blocker. It was never in scope:
  children run as tmux windows, and tmux does not run on native Windows.
- **The tool's own schema is inconsistent about the address form.** `SendMessage`'s `to`
  description names sessions only, while its validator and its call path handle the `uds:` scheme
  explicitly and a sibling tool's schema advertises it. This repo already depends on an
  undocumented harness surface under exactly this reasoning
  ([ADR-0012](0012-the-statusline-tick-reads-the-sessions-files.md)); the inconsistency is named
  here rather than papered over.
- **Cross-session messaging sits behind a remote feature flag that defaults on.** Forcing it on
  through an undocumented environment variable was rejected: it trades certain complexity for a
  risk never observed. This is the risk, recorded rather than defended against.
- **A coordinator in a prompting permission mode may hold an inbound peer message for approval,
  and that approval expires.** This is independent of accounts and cannot be fixed in code here:
  the settings file the crew installs belongs to the child's worktree, not to the coordinator's
  session. It is documented as an operator prerequisite in
  [`accounts.md`](../accounts.md) instead.
