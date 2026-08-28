# A two-way ASK channel between a coordinator and a child on another account

#146 reports that when a ticket routes a child to a different Claude Code account — a
different `CLAUDE_CONFIG_DIR` — the `CREW ASK` channel is dead in both directions:
`SendMessage` cannot resolve the peer's name, because cross-session name resolution reads
`<config-dir>/sessions/<pid>.json` and the two config dirs hold disjoint registries. The
operator's goal is concrete: run the coordinator on account A, children on account B, and
still have a working two-way ASK/answer channel. This note establishes what the harness
actually does, corrects one premise of the issue, and recommends a route.

**The short version.** The registry is per-config-dir and there is no way to widen it. But
the registry is only a *phone book*. The transport underneath it — the Unix socket in
`/tmp/cc-socks/` — is machine-wide and shared by every account, and `SendMessage` accepts an
explicit `uds:<socket>` address that skips name resolution entirely. On macOS and Linux the
socket auth key is **optional**, not a second gate, so an address alone is enough. The
channel is not missing; the crew's first turn is simply telling the child to use the one
address form that cannot work.

## How this was verified

Everything below is either read out of the installed Claude Code binary on this machine,
read out of this repo, run as a command here, or fetched from
<https://code.claude.com/docs>. No claim rests on recall.

The binary is `~/.local/share/claude/versions/2.1.250` (a bun-compiled Mach-O
that `~/.local/bin/claude` symlinks to; `claude --version` reports `2.1.250 (Claude Code)`).
Its JavaScript is embedded as readable minified source, so the citations below take the form
of a line number in

```
strings -n 6 ~/.local/share/claude/versions/2.1.250 > /tmp/cc_strings.txt
```

together with the identifier or literal to grep for. Line numbers are stable for this
build only; the identifiers (minified, so also build-specific) are quoted so a later reader
can re-find the same code by its surrounding string literals, which are stable.

Live artefacts were read with `ls -la ~/.claude/sessions ~/.claude-b/sessions /tmp/cc-socks`
and `cat` of one `<pid>.json` from each config dir. No key file was opened.

## 0. End-to-end verification (run 2026-08-28, after this note was first written)

The note originally recommended gating everything on one two-terminal test. That test has
now been run, from a session under `CLAUDE_CONFIG_DIR=~/.claude-b` (account B)
against a live session under the default `~/.claude` (account A). **All four checks passed.**

**Check 1 — the `uds:` scheme is parsed as an address.** Sending to this session's own socket
returned the *self-addressing* refusal, not a name-lookup failure:

```
SendMessage({to: "uds:/tmp/cc-socks/55533.sock", …})
→ 'uds:/tmp/cc-socks/55533.sock' is this session itself — "agentcrew-dev-skills-6b" is the
  name other sessions use to message YOU; there is no one else by that name to send to.
```

The validator resolved the socket path all the way to a session identity before refusing.
This matters because `SendMessage`'s own tool schema states the opposite in prose — "Every
row leads with the agent's `name [ref]` — **the name IS the address; there is no separate
address syntax**". §1.4's reading of the code is correct and the schema prose is not.

**Check 2 — the control: the two accounts really cannot see each other.** `ListAgents` from
account B listed three peer sessions; neither live account-A session appeared
(`agentcrew-dev-skills-e4` pid 32548 and `gpt-voicecoding-40` pid 94520, both `alive=True`,
`status=idle`, read from `~/.claude/sessions/*.json`). So any delivery below is attributable
to the address, not to name resolution quietly working.

**Check 3 — outbound, account B → account A, by socket address.** Accepted and delivered:

```
SendMessage({to: "uds:/tmp/cc-socks/32548.sock", …})
→ {"success":true,"message":"… → uds:/tmp/cc-socks/32548.sock","msg_id":"b7ba9777-…"}
```

**Check 4 — the reply leg, account A → account B, by copying `from`.** The account-A session
replied without ever consulting a registry, and the envelope arrived exactly as §1.4
predicted:

```
<cross-session-message from="uds:/tmp/cc-socks/32548.sock"
                       from-name="agentcrew-dev-skills-e4" from-mode="bypass">
ACK from account A, uds reply leg works
</cross-session-message>
```

The `from` attribute is the sender's `uds:` address, so the channel is two-way from the first
message with no shared state. `from-mode="bypass"` is also visible, which is the input to the
`crossSessionInbound` behaviour discussed in §3 — see the caveat below.

**What the test did not cover.** Both sessions were in a bypass permission mode and both
`settings.local.json` files already allow `SendMessage`, so the test does **not** exercise
the case a prompting-mode coordinator hits; recommendation item 4 (`crossSessionInbound:
"accept"`) is still unverified and still worth doing. A scratch account-A session could not
be used — starting `claude` with `CLAUDE_CONFIG_DIR=~/.claude` in a fresh
directory presented the first-run login menu, and driving an OAuth flow was out of scope —
so check 3 was run against one of the operator's own live sessions, with their authorisation.
Nothing about the mechanism depends on which account-A session was used.

## 1. How the channel actually works today

### 1.1 Three pieces, and only one of them is partitioned by account

**The registry — per config dir.** The configuration home is
`process.env.CLAUDE_CONFIG_DIR ?? homedir()/.claude` (`cc_strings.txt:289848`, the memoised
`ge()`), and the session registry is exactly `join(ge(), "sessions")`
(`cc_strings.txt:291171`, `function k4(){return Mr(ge(),"sessions")}`). Every read site
goes through `k4()`: registration (`EZ`), the heartbeat, the sweep, the peer listing
`D()` (`cc_strings.txt:291716`, `readdir` filtered by `/^\d+\.json$/`), and
`registeredInboxesOfPids` (`Lan`). There is no second root and no merge. Observed live:

```
~/.claude/sessions/     32548.json  94520.json  (+ one .key each)
~/.claude-b/sessions/   55533.json  78472.json  (+ one .key each)
```

A `<pid>.json` carries, among other fields, `pid`, `sessionId`, `cwd`, `startedAt`,
`procStart`, `version`, `peerProtocol`, `peerFeatures`, `kind`, `entrypoint`, `pidDomain`,
`tmux`, **`messagingSocketPath`**, `name`, `nameSource`, `status`. That
`messagingSocketPath` field matters later.

**The socket — machine-wide.** The socket path is derived from the temp directory, never
from the config dir: `join(XDG_RUNTIME_DIR ?? tmpdir(), "cc-socks", "<pid>.sock")`, with a
per-uid fallback `"/tmp/cc-socks-<uid>/<pid>.sock"` when the primary directory is refused
(`cc_strings.txt:314202`, `function Xnr()` / `_In()`). Both accounts' sessions therefore
land in the same directory. Observed live — `/tmp/cc-socks/` (mode `0700`, owned by the operator)
holds `32548.sock` and `94520.sock` (account A, `~/.claude`) alongside `55533.sock` and
`78472.sock` (account B, `~/.claude-b`).

Verified rather than assumed: a Python process running with
`CLAUDE_CONFIG_DIR=~/.claude-b` opened and immediately closed an `AF_UNIX`
connection to `/tmp/cc-socks/32548.sock` — an account-A session — and to `94520.sock`. Both
returned `CONNECT OK`. No bytes were sent. This is the same connect-and-drop probe the
harness itself uses for liveness (`cc_strings.txt:314202`, `function ge(e)` returning
`"live"`/`"dead"`).

**The teams directory — per config dir, and per session.** `~/.claude/teams/` and
`~/.claude-b/teams/` both exist, but this is not a second discovery path.
`kde(){return join(ge(),"teams")}` (`cc_strings.txt:289848`) puts it under the same
configuration home, and each entry is `session-<first 8 chars of session id>/config.json`
holding a `members` array of that one session's in-process teammates
(`~/.claude-b/teams/session-608b2adf/config.json`, read here, holds exactly one member,
`team-lead@session-608b2adf`, `backendType: "in-process"`). The official docs say the same
and add the limitation explicitly: "One team per session: a session has exactly one team,
scoped to that session. You can't create additional named teams or share a team across
sessions." Teams are a dead end for this problem.

### 1.2 The exact reason a cross-config-dir send by name fails

`SendMessage`'s resolver (`cc_strings.txt:309001`, `async function UBe`) searches three
domains in order: in-session agents and teammates; **local sessions**, from `k4()`; and
"your account's other sessions (Remote Control and cloud)" via the bridge. The failure
message enumerates them — `No agent named '<to>' is reachable.` plus, conditionally,
`The sessions on this machine could not be listed just now…` and `Your account's other
sessions (Remote Control and cloud) could not be checked just now…`.

For our case, all three miss:

- **Local**: the peer's `<pid>.json` is in the *other* config dir, so `D()` never sees it.
- **Bridge/cloud**: partitioned by *Anthropic account*, and A and B are different accounts.
- **Teammates**: partitioned by session.

So the local partition is by config dir, the remote partition is by login, and the two
partitions do not overlap in a way that helps. This is the documented model, stated
generally: "Each session registers itself in files on disk. When Claude lists or messages
your local sessions, Claude Code reads those files to find the sessions, so two sessions can
reach each other only when they can see the same files." The docs illustrate it with WSL 2
versus native Windows — "they register under different home directories" — which is the same
mechanism `CLAUDE_CONFIG_DIR` moves. Two config dirs are two home directories as far as the
registry is concerned.

### 1.3 Is the `.key` a second, independent blocker? **No — not on macOS or Linux.**

This is where #146's cause section is wrong. The issue says: "Each registry entry is paired
with a per-session `.key` file, so a hand-typed socket path would not authenticate either."
Three pieces of code say otherwise, and the official docs say so in plain words.

**The key really is per-config-dir.** `JKn(socketPath, storage, opts)`
(`cc_strings.txt:291171`) resolves a peer's token by listing `k4()` — the sender's *own*
sessions dir — and selecting files matching `<pid>.<sha256(canonical socket path)>.key`
(regex `Lo = /^(\d+)\.[0-9a-f]{64}\.key$/`). For a peer in another config dir there is no
such file, so it returns `{kind:"no-key"}`. That much of the issue is right.

**But a missing key is not fatal on this platform.** The sender is `be(target, frame, …)`
(`cc_strings.txt:291715`):

```js
let u = vgt();                                   // vgt() === (platform() === "windows")
let c = await JKn(e, r, {requireLiveOwner: u});
let P = c.kind === "token" ? c.token : void 0;
if (u && c.kind !== "token") { … throw … }       // Windows only
let R = P !== void 0 ? vmn(P) : "";              // no token ⇒ no auth line at all
```

On macOS `u` is false, so the refusal branch is skipped, `P` stays undefined, and the frame
is simply sent with no auth line prepended.

**And the receiver does not require one.** The listener sets
`o().authRequired = i.requireAuth ?? vgt()` (`cc_strings.txt:314203`) — again false on
macOS — and the connection handler `Je(socket)` (`cc_strings.txt:314202`) only drops
unauthenticated lines under `if (o().authRequired && p === void 0)`. The plain user-message
path `je()` (`cc_strings.txt:314202`) performs no registry lookup and no sender
allow-listing; it enqueues the message with `origin.kind === "peer"` and
`from = e.from ?? "unknown"`.

The harness says this itself, in a debug string shipped in the same binary
(`cc_strings.txt:163055`):

> `[uds-messaging] Failed to publish the inbox auth key; peers will send unauthenticated (accepted: auth is optional on this platform)`

And the official documentation states it as a supported property, not an accident:

> **macOS and Linux, including WSL 2**: the line is optional. Claude Code accepts a
> connection with or without it.
> **Native Windows**: the line is required.

What protects the socket on macOS/Linux is filesystem permission, not the token: the socket
is `chmod 0600` in a `0700` directory, both owned by the OS user (`srw-------` inside
`drwx------`, confirmed with `stat`). Both accounts here are the same OS user, which is
exactly why the connect probe in §1.1 succeeded.

**So the answer to the question as posed is yes.** Hand a process the peer's socket path on
macOS or Linux and it will connect and deliver. On native Windows it would not — the key
would be a genuine second blocker there. Anything built on this is macOS/Linux-only.

### 1.4 `uds:<socket>` is a first-class address, not a hand-typed path

`SendMessage` does not only take names. `Np(to)` (`cc_strings.txt:291171`) parses the `to`
field into a scheme:

```js
function Np(e){
  if (e.startsWith("uds:"))    return {scheme:"uds",    target: Vd(e.slice(4))};
  if (e.startsWith("bridge:")) return {scheme:"bridge", target: Vd(e.slice(7))};
  if (e.startsWith("did:"))    return {scheme:"did",    target: e};
  if (x8.test(e) || L8.test(e)) return {scheme:"uds",   target: e};   // x8 = /^\/\S*\.sock$/
  return {scheme:"other", target: e};
}
```

`validateInput` handles the scheme explicitly and accepts it
(`cc_strings.txt:309001`): a `uds` target passes `_gt()`'s "is this a local socket address"
check, self-addressing is refused via `UX()`, structured protocol frames are rejected for
`uds` while plain text is accepted (`if (Np(e.to).scheme === "uds" && typeof e.message === "string") return {result:!0}`),
and `notify_when_idle` is allowed on `uds` (only `bridge` and `did` are refused for it).

The `call` implementation then takes the `uds` branch (`cc_strings.txt:309002`) and invokes
the *same* `sendToUdsSocket` the name-resolved `local-session` branch uses. That function,
`j9e` (`cc_strings.txt:291715`), reads **no registry at all**:

```js
let P = rN();                          // process.env.CLAUDE_CODE_MESSAGING_SOCKET — our own inbox
let R = P ? zN(P) : void 0;            // zN(x) = `uds:${percent-encode(x)}`
let _ = JHe(R, i, t, void 0, …);       // the envelope, with from="uds:…"
… await be(e, b, r, {noFollowSymlink:true, …});
```

`JHe` (`cc_strings.txt:291171`) builds the wire envelope the receiving model sees:

```
<cross-session-message from="uds:/tmp/cc-socks/<sender pid>.sock" from-name="…" from-mode="bypass|prompting">
…body…
</cross-session-message>
```

**This is what makes the channel two-way for free.** The receiver is handed the sender's
`uds:` address in the message itself, so it can reply with
`SendMessage({to: "uds:/tmp/cc-socks/<sender pid>.sock", …})` without ever consulting a
registry. This repo already knows that address form is what arrives — `shapes.toml:71-75`
tells a Claude child "Messages arrive as cross-session messages from
`uds:/tmp/cc-socks/<coordinator pid>.sock` — that socket is the identity". What it does not
do is tell the child to *send* that way.

Two gates apply to the `uds` scheme and both are open by default:

- `Ho()` (`cc_strings.txt:291675`) — `CLAUDE_CODE_HARBOR_KITE` if set, otherwise the
  GrowthBook flag `tengu_harbor_kite`, **default `true`**. When false, both `uds:` and
  `bridge:` addresses are refused with "Cross-session messaging is not available in this
  session."
- `crossSessionInbound` on the receiver. See §3's failure modes; this one bites
  independently of accounts.

`ListAgents` is unaffected and stays broken: its description
(`cc_strings.txt:292064`) says "Names are the address", it enumerates in-session agents,
teammates, the local registry, cloud and Remote Control, and a peer in another config dir
appears in none of them. Addressing by socket is what routes around it.

### 1.5 What run 68's own log proves about the fallback route

`crewtask/68/.crew/log.jsonl` — the run #146 was filed from — contains exactly one record
with a recipient:

```
event=ruling  role=coordinator  ticket=None  to='127-127-e4'
  message="CREW RULING #127 pane source: option 1 — …"
```

That is the coordinator's `SendMessage` that #146 reports as having *failed* with
`No agent named '127-127-e4' is reachable`. It is in the log anyway. This is not luck:
`machine_log.hook_record` (`skills/crew/assets/machine_log.py:587-606`) reads only
`payload["tool_name"]` and `payload["tool_input"]` and never looks at the tool response, so
the `PostToolUse` hook copies a refused send in verbatim exactly as it copies a delivered
one. **The machine log already carries messages the harness would not deliver.** That is the
load-bearing fact behind option 2 below.

The same log contains no `escalation` record, consistent with the issue's account that the
child fell back to `AskUserQuestion` rather than calling `SendMessage` at all.

## 2. Is there any supported cross-account mechanism?

**For the registry: no.** What was searched, and found empty:

- **Every `CLAUDE_*`/`ANTHROPIC_*` environment variable the binary knows.** Extracted from
  the bundle's own export map (`cc_strings.txt:289965`) — 614 names. Nothing names a
  registry root, a second sessions directory, or a peer search path. The only variables
  that touch this area are `CLAUDE_CONFIG_DIR` itself (which moves the one root),
  `CLAUDE_CODE_TMPDIR` / `CLAUDE_TMPDIR` / `XDG_RUNTIME_DIR` (which move the *socket*
  directory, already shared), `CLAUDE_CODE_MESSAGING_SOCKET` and
  `CLAUDE_CODE_MESSAGING_TOKEN` (both *exported by* the harness, not read as input for
  discovery), and `CLAUDE_CODE_HARBOR_KITE` (the on/off gate).
- **Settings keys.** The official settings and settings-reference pages carry exactly three
  cross-session keys — `crossSessionInbound`, `isolatePeerMachines`, `dialogExpiry` — and
  none widens discovery. `isolatePeerMachines` only *narrows*, and only for cross-machine
  sends: "Claude Code doesn't prompt for messages between sessions on the same machine."
- **Multi-root behaviour elsewhere in the binary, as a precedent.** There is one, and it is
  pointedly not applied here. IDE lock-file discovery deliberately widens its search when a
  custom config dir is set (`cc_strings.txt:293047`):
  `let i=[join(ge(),"ide")]; if (CLAUDE_CONFIG_DIR) i.push(join(homedir(),".claude","ide"));`
  Nothing equivalent exists for `sessions`. **Inference:** the single-root behaviour of the
  registry is deliberate rather than an oversight, since the codebase demonstrably knows the
  pattern.
- **The `reply_across_default_dirs` peer feature**, which the live `<pid>.json` files
  advertise and which looked promising. It is not about config dirs. `$d =
  "reply_across_default_dirs"` (`cc_strings.txt:291172`) gates `zKn()`, which decides whether
  a *reply address* may point into a different **socket** default directory — the four
  regexes beside it are `/^\/tmp\/cc-socks…/`, `/^\/private\/tmp\/cc-socks…/`,
  `/^\/run\/user\/\d+\/cc-socks$/`, `/^\/data\/data\/com\.termux\/…\/cc-socks…/`. It is
  offered only when `Bun.ant.getPeerPid` exists (`mZ()`, `Vur()`, `cc_strings.txt:291172`),
  i.e. when peer-credential verification is available. A false lead, but worth recording so
  nobody else chases it.
- **The `teams/` directory and MCP.** Covered in §1.1; per-session, per-config-dir, and the
  docs rule out sharing a team across sessions. No MCP surface for the session registry
  exists in the binary.

**For addressing: yes, and it is documented.** The socket address is a published surface.
The cross-session-messaging page says the path appears in `/status` as the `Peer address`
row, "prefixed with `uds:`", and is exported as `CLAUDE_CODE_MESSAGING_SOCKET`; a whole
subsection ("The session's inbox socket") is written for the case "when you want a script or
hook to post into a session". `SendFile`'s `to` parameter advertises it in so many words:
"a peer session name from `ListAgents`, or an explicit `uds:<socket>` / `bridge:<session id>`
address" (`cc_strings.txt:309002`). `SendMessage`'s own `to` description does **not** mention
it (`cc_strings.txt:309001` — "a name from `ListAgents` …, a teammate name, `main`, or a
background agent's `agentId``"), even though its validator and its call path handle the
scheme explicitly. So: supported and validated in code, documented at the page level,
under-advertised in one tool's schema.

## 3. Options

### Option 1 — Preflight refusal or warning when the ticket's account ≠ the coordinator's

**Mechanism.** `driver.py` already records both facts: `declared_accounts(config)` and
`coordinator_config_home()` (`skills/crew/assets/driver/driver.py:868-883`), written into the
run section at `:918-919`, and every wave-table row carries a resolved `accounts.Binding`
(ADR-0014). Comparing them at preflight is a few lines.

**Cost.** Very small. One comparison, one message, one test.

**Depends on.** Nothing in the harness. Pure repo logic.

**Failure modes.** It does not deliver the operator's goal — it makes the breakage visible
and then refuses to do the thing they asked for. As a *warning* it is honest; as a *refusal*
it removes the account feature's main use for any ticket that might escalate, which is all of
them. Worth keeping as a guard rail behind a real fix, not as the fix.

### Option 2 — A registry-independent ASK channel through the machine log

**Mechanism.** The child records `CREW ASK` in the run's machine log instead of sending it;
the driver's wave loop already wakes the coordinator from an `escalation` record
(`driver.py:2528-2529`, `rule_on_messages` → `hand_over`), and the answer goes back through
`driver.py answer` (`driver.py:3340-3376` → `Loop.deliver`, `:2297-2360`), which for a Claude
child types into its tmux pane and for a Codex child goes over the bridge.

**Cost.** Moderate but well-bounded, and much of it is already built:

- The ask leg is nearly free. §1.5 shows the `PostToolUse` hook already copies even a refused
  `SendMessage` into the log as an `escalation`, so today's prompt would land the ASK in the
  log even when the send fails. Making it deliberate means giving the Claude lane a
  `machine_log.py … message --role child --ticket NN --message 'CREW ASK …'` form, exactly
  as `completion_claude` (`shapes.toml`) already does for receipts.
- The answer leg is fully built: `Loop.deliver`'s tmux `send-keys` / `type_into_pane` path is
  transport-agnostic and account-blind. `driver.py:2478-2492` already records that a ruling
  delivered through a pane "passes no hook and so reaches no log", and writes it in itself.

**Depends on.** Nothing in the harness for the ask leg. The answer leg depends on tmux, and
on the child sitting where typed text is read as a prompt.

**Failure modes.** This is the route the run already limps along on, and the issue names its
limits: escalation only works "when the child happens to block on a prompt". Typing into a
pane is a lossy channel — `deliver()` retries a single Enter and only records the instruction
after the cursor line confirms the composer emptied (`driver.py:2304-2306`), which is a
tell that this is fragile. It also inverts the design: `completion_claude`'s comment says
"`CREW ASK` is untouched: it is the one message that genuinely needs the coordinator", and
this option removes the only real conversation the run has.

### Option 3 — A shared or merged registry (symlink, bind mount, merged `sessions/`)

**Mechanism.** Make `~/.claude-b/sessions` and `~/.claude/sessions` the same directory.

**Cost.** Trivial to try, expensive to own.

**Depends on.** Undocumented harness internals, deeply. The sessions directory is not only
the peer registry: it also holds `.fleetview-heartbeat` (`cc_strings.txt:291172`), the
per-session `.key` files, and the sweep that reaps records of dead pids
(`$Ge`, `cc_strings.txt:291172`). The sweep is guarded by `isRegistrySweepPermitted()` and a
`pidDomain` check, but it deletes files; pointing two logins at one directory means each
account's sweeper walks the other's records. `countConcurrentSessions` and the FleetView
heartbeat would also merge, silently changing behaviour the plugin does not control.

**Failure modes.** Name collisions across accounts (the harness renames a colliding session
and updates "the shared record your other sessions use to look up the session's name"), cost
and liveness attribution mixing between accounts — which is the exact defect ADR-0013 and the
account feature exist to prevent — and the whole thing is one release away from breaking.
Reject.

### Option 4 — Address the peer by `uds:<socket>` (the issue's author did not know this exists)

**Mechanism.** The child's first turn tells it to reply to
`uds:<coordinator's messagingSocketPath>` instead of to the coordinator's *name*. The
coordinator's ASK arrives carrying `from="uds:/tmp/cc-socks/<child pid>.sock"` (§1.4) and it
replies to that address. Neither side reads a registry; both go down the identical
`sendToUdsSocket` path a same-account send uses.

**Cost.** Small, and mostly in one template. See §4.

**Depends on.** Three harness behaviours, in decreasing order of stability:

1. *The socket transport is shared across config dirs.* Structural — the socket directory is
   derived from `TMPDIR`, and the docs describe delivery as "over a per-session socket on
   macOS and Linux". Very unlikely to change.
2. *`uds:` is an accepted `SendMessage` address.* Handled explicitly by `validateInput` and
   `call`, and advertised in `SendFile`'s schema and on the docs page, but **not** in
   `SendMessage`'s own `to` description. Medium stability. A model reading only the tool
   schema would not know to use it, which is why it has to be in the prompt.
3. *Auth is optional on macOS/Linux.* Documented on the cross-session-messaging page as a
   platform property. Medium-high stability, but it is explicitly platform-conditional and
   would not hold on native Windows.

**Failure modes.**

- **Windows.** Would need the auth line, and the key is in the other config dir. Out of
  scope for this project today; worth a note.
- **A non-default socket directory.** The template hard-codes `/tmp/cc-socks/<pid>.sock`
  (`shapes.toml:72`). The harness may bind at `/tmp/cc-socks-<uid>/`, `$XDG_RUNTIME_DIR/cc-socks/`,
  or under `CLAUDE_CODE_TMPDIR` (`cc_strings.txt:314202`), and falls back to the per-uid
  directory when the primary is refused. The fix is to read the path instead of composing it
  — see §4.
- **`crossSessionInbound` and the permission-mode default.** This is the real one, and it is
  *independent of accounts*. With no `crossSessionInbound` value set, the docs say: "The
  receiving session prompts for permissions: … It holds one for your approval only when the
  sending session identifies itself as bypassing permission prompts." Crew children run
  `--dangerously-skip-permissions`, so their `from-mode="bypass"` (the envelope carries it —
  `ZP = ["bypass","prompting"]`, `cc_strings.txt:291171`). **A coordinator that is not itself
  in a bypass mode will hold every child ASK behind an approval dialog that expires after
  `dialogExpiry`, five minutes by default.** A crew that depends on this channel should set
  `crossSessionInbound: "accept"` for the coordinator, or document that the coordinator runs
  bypass.
- **Duplicate suppression and rate limits.** The receiver drops identical bodies inside a
  30-second window and rate-limits per sender (`cc_strings.txt:291675`, `Aan`, `dedupWindowMs:
  30000`, `bucketCapacity: 30`). `shapes.toml:74` already warns the child about this.
- **A stale pid.** A socket path names a pid. If the coordinator restarts, the address is
  dead. Today's name-based addressing has the same problem in a different shape (#112
  re-anchors a run to a new coordinator); the socket form makes it explicit rather than worse.

### Option 5 — Launch children at a deterministic socket path (`--messaging-socket-path`)

**Mechanism.** The binary parses `--messaging-socket-path <path>`. Its argv allow-list
contains it (`cc_strings.txt:232721`, alongside `--agent-id`, `--team-name`,
`--teammate-mode`) and the bundle carries its help text
(`cc_strings.txt:246382`): "Cross-session messaging server path: a Unix domain socket on
Mac/Linux, a `\\.\pipe\` name on Windows (defaults to an auto-generated path)". The listener
honours an explicit path (`isExplicit`, `cc_strings.txt:314203`, with dedicated errors —
"`--messaging-socket-path` points to a live socket", "Choose a different
`--messaging-socket-path` whose directory you own and can make private (0700)"). The crew
could give every child a run-scoped path, e.g. `<run-dir>/socks/<ticket>.sock`, and hand the
same string to the coordinator.

**Cost.** Small — one flag in the child launch, one value in the launch record.

**Depends on.** A **hidden** flag. `claude --help` on 2.1.250 does not print it (`claude
--help | grep -ic messaging` → `0`), and it appears in none of the docs pages fetched. I
could not confirm it is accepted at runtime without starting a session, which I did not do.
That is a materially weaker footing than option 4, which uses only surfaces the docs
describe.

**Failure modes.** Socket paths are capped near 104 bytes ("Socket path too long (… max
~104)"), so a run directory deep in `~/Documents/…` could overflow it — the harness's own
advice is to shorten `CLAUDE_CODE_TMPDIR`. A hidden flag can be renamed or removed in any
release with no changelog entry.

### Option 6 — Post to the child's socket from a script, not from the model

**Mechanism.** The docs' "The session's inbox socket" section is written for this: connect
to the socket and write one JSON line. The harness even ships the recipe as a debug string
(`cc_strings.txt:163055`):

```
{ echo '{"type":"auth","token":"'"$CLAUDE_CODE_MESSAGING_TOKEN"'"}'; \
  echo '{"type":"user","message":{"role":"user","content":"hello"}}'; } | socat - UNIX-CONNECT:<sock>
```

This would let `driver.py answer` reach a Claude child over its inbox instead of by typing
into a tmux pane — a strictly better version of option 2's answer leg.

**Cost.** Moderate: a small socket client in `driver.py`, plus knowing the child's socket
path.

**Depends on.** The wire frame shape (`{"type":"user","message":{"role":"user","content":…}}`),
which is undocumented beyond that one debug string, and on auth being optional (a *script*
is not the child's own child process, so the "own-child" verification the docs describe does
not apply — it would arrive as an ordinary peer message and be subject to
`crossSessionInbound`). Higher internals exposure than option 4.

**Failure modes.** A message posted this way carries no `from` address unless the script
supplies one, so the child cannot reply to it. It also bypasses the `PostToolUse` hook
entirely, so the driver must write the ruling into the log itself — which `deliver()` already
does for the tmux path (`driver.py:2357-2360`), so that part is free.

## 4. Recommendation

**Take option 4: address the peer by its `uds:` socket, both ways. Keep option 1 as a
warning, not a refusal. Do not build option 3.**

The reasoning is that option 4 is not a workaround around the harness — it is the harness's
own second addressing mode, used the way its own `SendFile` schema and its own `/status`
output describe. It needs no new channel, no new file format, no daemon and no shared state:
the send path is byte-identical to the one a same-account send already takes
(`sendToUdsSocket`, §1.4), and the reply address arrives inside the message for free, so the
channel is two-way the moment the first message lands. Option 2 is the honest fallback but it
trades a real conversation for a tmux keystroke channel that the driver's own comments admit
is lossy, and it would leave `CREW ASK` — by the repo's own words the one message that needs
the coordinator — travelling on the least reliable transport in the system. Option 5 is
cleaner in principle but rests on a flag that `--help` does not admit exists.

**The specific first change.** `skills/crew/assets/dispatch/templates/shapes.toml:66-69`,
the `coordinator_claude` block, currently reads:

> Your coordinator is the Claude session `<coordinator name>`; reply with SendMessage to it,
> ending every message with `ts=<unix time>`.

It should name the address instead of the name — `SendMessage({to: "uds:<coordinator socket>", …})`
— keeping the name only as a human-readable label. Four consequences follow, and each is
small:

1. **Do not compose the socket path; read it.** `shapes.toml:72` already hard-codes
   `/tmp/cc-socks/<coordinator pid>.sock`, and §3's failure modes say why that is a latent
   bug. The authoritative value is the `messagingSocketPath` field of the coordinator's own
   registry entry — and `launch.py` **already reads that exact file**:
   `coordinator()` returns `(pid, entry)` from
   `harness_directory(SESSION_REGISTRY)/f"{pid}.json"` (`skills/crew/assets/launch/launch.py:174,
   194-209`) and pulls fields out with `registry_string()` (`:203-211`). Adding
   `MESSAGING_SOCKET = "messagingSocketPath"` beside `REGISTRY_NAME` and `REGISTRY_SESSION`
   at `launch.py:112-114` is a one-line extension of a read that already happens, and it
   costs nothing at run time. Thread it to the driver as `--coordinator-socket` beside the
   existing `--coordinator-pid`, into `run_section` (`driver.py:889-897`) beside
   `coordinator_name` and `coordinator_pid`, and into `render_turn`'s values
   (`dispatch.py:385-386`) as `<coordinator socket>`.
2. **Thread the value through the manual path too.** `render_roles`
   (`dispatch.py:214, 229-232`) fills `coordinator_claude` with `<coordinator name>` alone.
   If the block gains a socket placeholder, that call site and the `"roles"` argument tuple
   at `dispatch.py:980` both need the new value, or the manual developer prompt will render
   an unfilled placeholder.
3. **Fix the ruling→ticket correlation before it silently breaks.** `machine_log.project`
   builds `ticket_by_child` from each launch record's `child` (the session *name*) and
   correlates a ticketless coordinator record through its `to` field
   (`machine_log.py:242-254`). Run 68's log shows exactly this working: `to='127-127-e4'`.
   Once the coordinator answers to `uds:/tmp/cc-socks/<pid>.sock`, that lookup misses and the
   ruling loses its ticket. Either record the child's socket address in the launch record
   and index `ticket_by_child` by both, or have the coordinator's hook carry `--ticket`.
   This is the one place the change is not local, and it should land in the same commit.
4. **Set the receiver's inbound policy explicitly.** Add `"crossSessionInbound": "accept"` to
   `skills/crew/assets/settings.local.json` — which already carries the `SendMessage` and
   `ListAgents` allowances — so a child's bypass-mode ASK is not held behind an approval
   dialog in a prompting coordinator, and vice versa. This failure has nothing to do with
   accounts and is presumably latent today.

Options 1 and 2 stay in the picture as defence in depth: preflight should *warn* when a
ticket's account differs from the coordinator's, naming the socket the child will answer to,
and the `escalation` record in the machine log remains the audit trail whether or not the
send lands. Neither needs to block this change.

## 5. Open questions and what I could not verify

- **~~No end-to-end delivery test was run.~~ Superseded — see §0.** The two-terminal test
  this note asked for has been run and both legs pass. What remains unverified from that
  gate is only the prompting-mode / `crossSessionInbound` case.
- **`--messaging-socket-path` acceptance at run time is unconfirmed.** It is in the binary's
  argv allow-list and has help text, but `claude --help` does not show it, and `claude
  --messaging-socket-path … --version` proves nothing because `--version` short-circuits
  before option validation. Only relevant if option 5 is revisited.
- **The GrowthBook flag `tengu_harbor_kite` defaults to `true` in code**, but it is a remote
  flag and could in principle be served `false` to some accounts. `CLAUDE_CODE_HARBOR_KITE=1`
  forces it on (`cc_strings.txt:291675`); whether the crew should set it is a judgement call
  I have not made.
- **Whether a `uds:`-addressed send triggers any permission prompt** in modes other than
  bypass. `SendMessage`'s `checkPermissions` has an `isolatePeerMachines` branch for
  cross-*machine* targets and the docs say same-machine sends are not prompted, but I did not
  trace every branch of the `uds` path for an `ask` outcome under `plan` or `auto` mode. The
  code shows `plan` mode asking for `SendFile`; the `SendMessage` equivalent was not read.
- **The receiver's `senderMode` handling for an unauthenticated peer.** `je()` accepts the
  message, but the classification passed downstream (`p` in `Je()`, `undefined` when no auth
  line was sent) also feeds `le(t,s,u)`. I established that delivery is not gated on it; I
  did not establish whether it changes how the message is labelled to the model.
- **Windows.** Everything in §1.3 is explicitly platform-conditional. If this project ever
  targets native Windows, the `.key` becomes a real second blocker and option 4 does not
  survive.
- **#146's own run.** The child never emitted a `SendMessage` at all (no `escalation` record
  in `crewtask/68/.crew/log.jsonl`), so the child-side failure message quoted in the issue
  came from the child's transcript rather than the log. I did not read the transcript to
  confirm the exact refusal text.

## 6. Sources

**The installed harness** — `~/.local/share/claude/versions/2.1.250`, read via
`strings -n 6 … > /tmp/cc_strings.txt`; line numbers are into that dump.

| Where | What it established |
|---|---|
| `:289848` (`ge()`, `kde()`) | Config home = `CLAUDE_CONFIG_DIR ?? ~/.claude`; teams dir = `join(configHome,"teams")` |
| `:291171` (`k4()`) | Session registry = `join(configHome,"sessions")`, single-rooted |
| `:291171` (`JKn`) | Peer auth key is looked up in the *sender's own* `sessions/`; a cross-config-dir peer yields `no-key` |
| `:291171` (`Np`, `_gt`, `JHe`, `zN`) | `uds:`/`bridge:`/`did:` address parsing; the `<cross-session-message from="uds:…">` envelope |
| `:291172` (`$d`, `zKn`, `Vur`) | `reply_across_default_dirs` is about socket directories, not config dirs |
| `:291675` (`Ho`, `Ypt`, `Aan`) | `CLAUDE_CODE_HARBOR_KITE` / `tengu_harbor_kite` gate (default on); dedup and rate limits |
| `:291715` (`be`, `j9e`) | Sender omits the auth line when it has no key (non-Windows); `sendToUdsSocket` reads no registry |
| `:291716` (`D`, `Pe`) | The peer listing is a `readdir` of `k4()` only |
| `:292064` (`ListAgents` description) | Discovery domains: in-session, teammates, local registry, cloud, Remote Control |
| `:309001`–`:309002` (`UBe`, `validateInput`, `call`) | Name resolution's three domains and its failure text; `uds` accepted and routed to `sendToUdsSocket` |
| `:314202` (`Xnr`, `_In`, `Je`, `je`) | Socket path derives from `TMPDIR`, not the config dir; unauthenticated lines accepted when `authRequired` is false; the user-message handler does no sender allow-listing |
| `:314203` (`authRequired = i.requireAuth ?? vgt()`) | Auth is required only on Windows |
| `:163055` | `"peers will send unauthenticated (accepted: auth is optional on this platform)"`; the `socat` inject recipe |
| `:232721`, `:246382` | `--messaging-socket-path` is in the argv allow-list and has help text, but is hidden from `claude --help` |
| `:293047` | IDE lock-file discovery *does* search a second config root — the precedent the registry deliberately lacks |

**Live artefacts on this machine**

| Where | What it established |
|---|---|
| `~/.claude/sessions/`, `~/.claude-b/sessions/` (`ls -la`, `cat <pid>.json`) | Disjoint registries; record shape, including `messagingSocketPath`, `peerFeatures`, `name`, `status` |
| `/tmp/cc-socks/` (`ls -la`, `stat`) | Both accounts' sockets in one `0700` directory, each socket `0600`, same owner |
| `AF_UNIX` connect probe from a `CLAUDE_CONFIG_DIR=~/.claude-b` process | An account-B process can open a connection to an account-A session's socket |
| `~/.claude-b/teams/session-*/config.json` | Team config is per-session and per-config-dir; one `members` array of in-process agents |
| `claude --version`, `claude --help`, `ps aux`, `lsof -p` | Version 2.1.250; the bundle path; `--messaging-socket-path` absent from `--help` |

**Official documentation** (fetched 2026-08-28)

| URL | What it established |
|---|---|
| <https://code.claude.com/docs/en/cross-session-messaging> | Registration is by files on disk, "two sessions can reach each other only when they can see the same files"; the auth line is optional on macOS/Linux and required on native Windows; the socket path is the `uds:`-prefixed `Peer address` and is exported as `CLAUDE_CODE_MESSAGING_SOCKET`; `crossSessionInbound` semantics and the bypass/prompting default; `isolatePeerMachines` does not apply same-machine; size, burst and loop limits |
| <https://code.claude.com/docs/en/agent-teams> | Mailboxes live at `~/.claude/teams/{team}/inboxes/{agent}.json`; "One team per session … can't share a team across sessions" |
| <https://code.claude.com/docs/en/settings> | `CLAUDE_CONFIG_DIR` "stores your settings, session history, and plugins there instead"; precedence rules for `crossSessionInbound` and `isolatePeerMachines` |
| `anthropics/claude-code` `CHANGELOG.md` | Cross-session messaging entries 2.1.221–2.1.250; 2.1.250 lists only "Bug fixes and reliability improvements"; no entry mentions the `uds:` address form |

**This repository**

| Where | What it established |
|---|---|
| `skills/crew/assets/launch/launch.py:107-114, 129-141, 174, 194-211` | `SESSION_REGISTRY = ("CLAUDE_CONFIG_DIR", ".claude", "sessions")`; `harness_directory`; the coordinator's registry entry is already read and fields pulled from it |
| `skills/crew/assets/driver/driver.py:868-883, 918-919` | `declared_accounts`, `coordinator_config_home`, both written into the run section |
| `skills/crew/assets/driver/driver.py:2297-2360, 2478-2492, 3340-3376` | `Loop.deliver` (tmux keys for a Claude child, bridge for Codex); `hand_over`; `run_answer` |
| `skills/crew/assets/driver/driver.py:2506-2530` | `rule_on_messages`: an `escalation` record is what hands a ticket to the coordinator |
| `skills/crew/assets/accounts.py` | `environment_delta` / `Binding` — an account is a config dir plus inherited-vs-explicit |
| `skills/crew/assets/machine_log.py:5-8, 242-254, 587-628` | The `PostToolUse` hook copies `tool_input` only, never the response; ruling↔ticket correlation via `to` and `ticket_by_child` |
| `skills/crew/assets/monitor/monitor.py:518-527, 650-690` | Per-account session reading already exists: `sessions_directory(account)`, `claude_states(binding=…)` |
| `skills/crew/assets/dispatch/templates/shapes.toml:66-105` | `coordinator_claude` (name-based send — the break), `coordinator_claude_child` (already names the `uds:` form for *incoming* messages), `completion_claude` (receipts already bypass the channel) |
| `skills/crew/assets/dispatch/dispatch.py:214, 229-232, 385-386, 405-428, 980` | Where the coordinator paragraph is assembled, and the two call sites a new placeholder must reach |
| `crewtask/68/.crew/log.jsonl` | The failed coordinator send was recorded anyway (`event=ruling, to='127-127-e4'`); no `escalation` record exists for the child |
| `docs/adr/0010`, `docs/adr/0013`, `docs/adr/0014`, `docs/accounts.md` | The driver runs the run; the account registry is machine-level; optional routing keys resolve at the wave-table boundary |
| `gh issue view 146 / 127 / 110` | The report, the waiter-reap history, and the earlier account-plumbing gap in the wake monitor |
