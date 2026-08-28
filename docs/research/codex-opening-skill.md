# Codex: how a session started from the command line gets its opening skill

The Codex bridge's `launch` used to pass the whole first turn as the TUI's positional
`[PROMPT]` argument. One process created the thread, so there was nothing to hand over.
#150 moved that turn to an app-server `turn/start` so it could carry a
`{"type": "skill", "name", "path"}` input item, and the cost of that was a second
client: the bridge now creates the thread and the TUI `resume`s it from the rollout on
disk. Every launch failure since — #155, and the `rollout … is empty` failure of
2026-08-28 — is that handoff racing Codex's rollout writer (ADR-0021).

The question this note answers is whether the handoff buys anything Codex does not
already offer to a single process: **can a TUI session started from the command line
carry a skill on its first turn as a first-class skill input, with no second client?**

It can. The route is a linked mention in the argv text —
`[$implement](/abs/path/to/SKILL.md)` — and it produces the byte-identical `<skill>`
block that the app-server input item produces.

## How this was verified

Everything below is either a command run on this machine against **codex-cli 0.150.1**
(`codex exec`, `codex debug prompt-input`, `codex app-server`) or **0.149.1** (the
TUI — the launcher's `--version` reports 0.150.1 but the TUI it dispatches to records
`cli_version: 0.149.1` in `session_meta`), or the Rust source at tag `rust-v0.150.1`
read through `gh api repos/openai/codex/contents/<path>?ref=rust-v0.150.1`. Rollout
evidence is the JSONL under `~/.codex/sessions/2026/08/28/`. The app-server protocol is
self-describing: `codex app-server generate-json-schema --out <dir>` writes every
request, response and notification schema, and is the first place to look when this
behaviour changes.

## 1. CLI surface: is there a flag?

**No, and none is needed.** `codex --help`, `codex exec --help`, `codex resume --help`,
`codex queue --help` and `codex plugin --help` on 0.150.1 expose no `--skill`, no skill
argument and no skill-bearing config key; the only positional input is `[PROMPT]`.
`codex features list` shows `skill_search`, `skill_mcp_dependency_install` and
`mentions_v2` as stable, but these are model- and UI-side behaviours, not input
surfaces. The GitHub release notes for `rust-v0.140.*` through `rust-v0.150.1`
(`gh api repos/openai/codex/releases`) contain no CLI skill-input entry. The official
docs agree by omission: <https://learn.chatgpt.com/docs/build-skills.md> says only "In
Codex CLI or the IDE extension, run `/skills` or type `$` to mention a skill."

Confidence: **high** for the absence of a flag on 0.150.1.

## 2. Mention resolution: when does `$name` become a skill input?

**Two independent resolvers act on the same text, and neither requires the composer
popup or the app-server.**

The TUI resolver runs at submit time. `ChatWidget::submit_user_message` builds the
app-server input list and, for whatever text it is about to send, calls
`find_skill_mentions_with_tool_mentions` and pushes a `UserInput::Skill` item for every
match (`codex-rs/tui/src/chatwidget/input_submission.rs:196-240`,
`codex-rs/tui/src/chatwidget/skills.rs:200-238`). A skill matches on an exact linked
path or an exact `skill.name`. The initial argv prompt goes through exactly that path:
it is wrapped by `create_initial_user_message`
(`codex-rs/tui/src/chatwidget/user_messages.rs:180-205`) and submitted by
`submit_initial_user_message_if_pending`, whose only action is
`self.submit_user_message(user_message)`
(`codex-rs/tui/src/chatwidget/input_restore.rs:120-137`). So argv text and typed text
are the same code path; the popup only supplies `mention_bindings`, which are an
additional matching key, not a required one.

The core resolver runs for every client on every turn.
`collect_explicit_skill_mentions` (`codex-rs/skills/src/selection.rs:42-109`) is called
from `codex-rs/core/src/session/turn.rs:745` and `:821`; it resolves structured
`UserInput::Skill` items by path first, then **scans every `UserInput::Text` for `$`
mentions** and selects skills by exact path, or by plain name when that name is
unambiguous (exactly one enabled skill of that name, no colliding connector slug —
`selection.rs:164-196`). The selected skills' `SKILL.md` bodies become the injected
prompt fragments at `turn.rs:838-853`.

The mention grammar is in `codex-rs/skills/src/mentions.rs`: sigil `$`, name characters
`[A-Za-z0-9_-:]` (`:225`), and a linked form `[$name](path)` whose path is captured for
exact matching (`:76-79`, `:147-202`). Position is irrelevant — the whole text is
scanned byte by byte (`:91-139`), so a bridge marker line before the mention changes
nothing. Verified: an argv prompt of `AGENTCREW-LAUNCH-MARKER 12345\n[$implement](…)` on
0.150.1 still injected the skill (rollout
`rollout-2026-08-28T14-09-36-01a04621-34dc-7ed1-b3b1-443f5bd0158d.jsonl`, one `<skill>`
block).

**Why `$implement` on argv produced nothing.** Not a mechanism gap — a name mismatch.
The installed skill's canonical name is `mattpocock-skills:implement`, not `implement`.
Querying the app-server directly (`skills/list` with `cwds: ["/tmp/codex-research"]`,
`forceReload: true`) returns exactly one matching entry:

```
'mattpocock-skills:implement'  enabled=True scope=user
path=/Users/<account>/.codex/plugins/cache/mattpocock/mattpocock-skills/1.2.3/skills/engineering/implement/SKILL.md
```

`$implement` matches no skill name, so both resolvers correctly decline. The bridge's
app-server item worked despite carrying `"name": "implement"` because a structured item
is resolved **by path**, and the name is only used to block a later plain-name match
(`selection.rs:61-91`).

Three probes settle it, all `codex exec` on 0.150.1 in a scratch git repo, all with a
trivial "reply OK" prompt, each inspected in its rollout:

| argv text | `<skill>` blocks in rollout |
|---|---|
| `$implement …` | 0 |
| `$mattpocock-skills:implement …` | 1 |
| `[$implement](<abs SKILL.md path>) …` | 1 |

The block in the last two cases is the same 611-character item #150 measured from the
app-server path: a `response_item` of role `user` containing
`<skill><name>mattpocock-skills:implement</name><path>…</path>` followed by `SKILL.md`
verbatim. It is a *separate* rollout item, not part of the user message — which is worth
noting, because "the rollout's user message carries no `<skill>` block" is true even in
the runs where the skill was injected.

**And it works in the real TUI, not just `exec`.** A `codex` TUI started under tmux with
`[$implement](<abs path>) Protocol probe…` as its positional argument produced rollout
`rollout-2026-08-28T14-08-03-01a0461f-ca91-7c73-947e-4058983bf4db.jsonl`
(`originator: codex-tui`, `cli_version: 0.149.1`) containing the user message verbatim
and one identical `<skill>` block. One process, one thread, no `resume`.

**What "locate the skill" means.** The claim comes from the app-server documentation,
not the developer-guide gist: <https://learn.chatgpt.com/docs/app-server> (fetched
2026-08-28) says "If you omit the `skill` item, the model will still parse the
`$<skill-name>` marker and try to locate the skill, which can add latency." (The gist at
<https://gist.github.com/oneryalcin/ee2c27e2d8aa040da8fbe7eebcc2ecea>, revision 1,
created 2026-04-29, does not contain that sentence; #150 attributed it there.) The
sentence is misleading in both directions. When the mention resolves, *core* injects the
body before the model ever sees the turn — there is no model-side locating and no
latency. When it does not resolve, the model is under no obligation to look: in the
measured run below it never opened `implement/SKILL.md` at all and simply ignored the
marker. Silent loss of the skill, not latency.

Confidence: **high** (source read at the pinned tag, plus five live runs).

## 3. Thread handoff: is there a readiness signal?

**No signal, and no supported cross-process attach to a live thread.** ADR-0021 already
records that a thread does not exist on disk until its first user message; the source
confirms both error strings — `no rollout found for thread id …`
(`codex-rs/thread-store/src/local/read_thread.rs`) and `thread … is not materialized
yet; includeTurns is unavailable before first user message`
(`codex-rs/app-server/src/request_processors/thread_processor.rs`). No notification in
`ServerNotification.json` announces rollout durability; nothing in `ThreadResumeParams`
expresses retry or wait semantics.

There *is* an in-memory attach path, but only inside one process. `resume_running_thread`
(`thread_processor.rs:4026-4303`) looks the thread up with
`self.thread_manager.get_thread(...)` and, when it is loaded, delegates the resume to the
running thread's listener instead of cold-reading the rollout. Two caveats: the thread
manager is per-app-server-process, and even the running path re-reads the stored thread
whenever history is wanted (`needs_history`, true unless the caller passes
`excludeTurns`). Putting the TUI in the same process is possible in principle — the
docs describe `codex app-server --listen unix://PATH` plus `codex --remote unix://PATH`
(<https://learn.chatgpt.com/docs/app-server>, "Connect the CLI terminal UI"), and
`codex app-server daemon` / `codex agents` exist for a shared local daemon — but this
was **not** tested here, and the transport is documented as experimental and unsupported
for production.

Confidence: **high** that no readiness notification exists; **medium** on the
running-thread resume reading (source only); **unverified** that `--remote` would remove
the race.

## 4. Community practice

**No primary evidence found.** Searches of `openai/codex` issues and discussions
(`gh search issues --repo openai/codex` for skill/tmux/initial-prompt phrasings, and a
GraphQL discussion search) surfaced skill-mention bugs — #23454 "$skill explicit
invocation ignores local explicit-only skills absent from implicit skill list"
(2026-05-19), #9930 on duplicate skill names across scopes (2026-01-26), #32170 (2026-07-10)
— but nothing about starting a session with a skill from a harness. A web search turned
up tmux managers for Codex (for example `waskosky/agent-cli-farm`) whose launch scripts
pass an ordinary prompt and never touch skills. No harness was found that starts a
Codex thread through the app-server in order to carry a skill.

Confidence: **low** — this is an absence of evidence from a bounded search, not proof
that no such practice exists.

## 5. What an unresolved mention actually costs

Two real runs on 0.150.1, same ticket (`Create a file named notes.txt whose only content
is the word hello`), same trivial repo, `--sandbox workspace-write`, differing only in
the mention:

| | run A: `$implement` (unresolved) | run B: `$mattpocock-skills:implement` |
|---|---|---|
| `<skill>` blocks | 0 | 1 |
| tool calls | 14 | 3 |
| wall clock | 188 s | 48 s |
| total tokens | 415,409 | 102,027 |
| `implement/SKILL.md` read | never | injected |
| outcome | implemented the ticket its own way | followed the skill: stopped to confirm the TDD seam |

Rollouts: `rollout-2026-08-28T14-00-30-01a04618-….jsonl` and
`rollout-2026-08-28T14-03-45-01a0461b-….jsonl`.

Read the table for its shape, not its arithmetic: the two runs did different work, so
the token and time gap is mostly the divergence, not overhead. The load-bearing rows are
the last two. Run A never opened `implement/SKILL.md` — it read `tdd/SKILL.md` on its own
initiative and proceeded. The skill's instructions never reached the model. So the cost
of a mention that does not resolve is not latency; it is that the skill silently does
not apply, which is precisely the failure `/crew` cannot tolerate on a first turn.

For completeness: the model *could* have found it. The session's developer prompt carries
a `<skills_instructions>` inventory of every skill's name, description and path
(observed in both rollouts), so a self-service read is one `exec` call away. It just did
not take it.

Confidence: **high** for what the two runs did; **medium** for generalising the model's
willingness to ignore an unresolvable marker from n=2.

## Recommendation

**(c) — a Codex-native route we had missed.** Give the thread back to the TUI and carry
the skill in the argv text as a linked mention:

```
codex [--sandbox …] "<marker>
[$implement](/abs/path/to/implement/SKILL.md) <rest of the first turn>"
```

This is the v0.9.6 shape — one client, one thread, no rollout handoff, no race — and it
keeps #150's actual criterion, because both the TUI and core turn that text into a
first-class skill selection and inject the identical `<skill>` block. It is verified end
to end in a real TUI session and in `codex exec`, with the marker line ahead of the
mention.

Three things follow for whoever implements it:

- The bridge already computes what the linked form needs. `resolve_skill_path`
  (`skills/crew/assets/codex/codex_bridge.py:339`) returns the absolute `SKILL.md` path
  it currently puts in the structured item; the same string goes in the parentheses.
  The visible text stays `$implement`, so nothing about the rendered first turn changes
  for a human watching the pane.
- Path matching is exact string matching after normalisation
  (`selection.rs:141-162`, `mentions.rs:72-74`). `resolve_skill_path` calls `.resolve()`;
  on this machine the resolved path equals the catalog path and the probe matched, but a
  symlinked `CODEX_HOME` would break it silently. The canonical name
  (`$mattpocock-skills:implement`) is the fallback key, and `skills/list` is how to check
  either one.
- Failure is silent by design: an unresolvable mention injects nothing and reports
  nothing. Whatever the bridge does here should assert the mention resolves (against
  `skills/list`) rather than assume it, since #150's whole point was that the opening
  skill actually applies.

Option (d) — keeping the app-server handoff — is not required by anything found here.
Option (a) is what we would fall back to if the linked form regressed, and it is
strictly worse: run A shows an unresolved `$implement` means no skill at all. Option (b)
(inlining `SKILL.md` as text) is unnecessary, since Codex will inline it for us, in its
own `<skill>` framing, keyed to the file it actually loaded.
