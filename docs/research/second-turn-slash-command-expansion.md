# Does a slash command delivered as a later turn expand into its skill, on both executors?

ADR-0028 (proposed) has a queued ticket's child open with `/triage`, escalate once, and then
receive `/implement <ticket path>` as the coordinator's ruling — delivered by `driver.py answer`
on a Claude child and by the Codex bridge's `send` on a Codex child. Until now the only path
verified in this project for reaching a `disable-model-invocation: true` skill inside a child was
the **first turn** (ADR-0003's `initialPrompt`, and the bridge's launch turn). This note records
what a live second-turn delivery actually does, on both executors.

It recommends nothing. The facts are the deliverable.

## How this was verified

Two throwaway children were driven on 2026-09-03 against a scratch worktree
(`/private/tmp/crew182-scratch`, branch `scratch/182-probe`, both since removed):

- **Claude:** Claude Code **2.1.258**, Opus 4.5, launched into its own detached tmux window with a
  `--agents` object carrying an `initialPrompt` — the same launch shape as
  `dispatch.py:launch_claude_child`. Later turns were delivered by the *decomposed* body of
  `driver.py:type_into_pane` (`skills/crew/assets/driver/driver.py:718`): the same
  `tmux send-keys -l` per line, `S-Enter` between lines, `Enter` at the end, then the same
  `composer_holds` check (`driver.py:738`). It was decomposed only so the composer could be
  captured *between* the literal text and the `Enter`; the bytes sent are identical. This is the
  channel `Loop.deliver` uses for a Claude child (`driver.py:2744`, `:2797`).
- **Codex:** codex-cli **0.152.1**, `gpt-5.6-sol`, launched and driven through the real bridge —
  `codex_bridge.py launch` then `codex_bridge.py send`
  (`skills/crew/assets/codex/codex_bridge.py:831`), which is exactly what `Loop.deliver` shells
  out to. Evidence is the rollout JSONL.

Durable artefacts:

| Artefact | Path |
|---|---|
| Claude transcript, facts 1 / 2 / 3a | `~/.claude-b/projects/-private-tmp-crew182-scratch/0d6bbbe5-a04a-49ff-9f38-367e78c279e4.jsonl` |
| Claude transcript, fact 6 (guards installed) | `~/.claude-b/projects/-private-tmp-crew182-scratch/05332282-b47f-4ec6-bc3d-6034f6d8e585.jsonl` |
| Codex rollout, facts 4a / 3b / 4b | `~/.codex/sessions/2026/09/03/rollout-2026-09-03T03-16-04-01a062b1-09db-7251-ba1f-e7cf0c0e08c4.jsonl` |
| Codex rollout, version re-confirmation probe | `~/.codex/sessions/2026/09/03/rollout-2026-09-03T03-23-15-01a062b7-9dab-79e0-92f1-6a4ba49e4af8.jsonl` |
| Bridge machine log for the three sends | `/tmp/crew182/codex/log.jsonl` (ephemeral; contents quoted in §5) |
| tmux pane captures | `/tmp/crew182/captures/` (ephemeral; the decisive lines are quoted inline) |

Both skills under test are gated: `implement/SKILL.md:4` and `triage/SKILL.md:4` in
`~/.claude/plugins/cache/mattpocock/mattpocock-skills/1.2.3/skills/engineering/` both carry
`disable-model-invocation: true`.

**Version drift.** `docs/research/codex-opening-skill.md` pins codex-cli 0.150.1; this machine runs
0.152.1. §6 re-confirms that note's §2 table at 0.152.1 rather than re-deriving it.

## Fact table

| # | Fact | Result | Pointer |
|---|---|---|---|
| 1 | Claude child mid-session receives `/implement <path>`; the skill is invoked | **Yes** — and the composer rewrote the short name to `/mattpocock-skills:implement` | §1 |
| 2 | Same with `/mattpocock-skills:triage <path>` as a later turn | **Yes**, unchanged by the rewrite | §2 |
| 3a | Claude: does a non-leading slash command still expand? | **No.** Preamble first → plain text, no expansion, no error. Slash first → expands, trailing preamble swallowed into `<command-args>` | §3 |
| 3b | Codex: same question, given `opening_skill_name` is start-anchored | **It still expands** — the structured item is suppressed, but the core text resolver rescues a *canonical-name* mention. Contradicts the brief | §4 |
| 4a | Codex follow-up beginning `$implement <path>` injects the skill as a structured item resolved by path | **Yes** — one 611-char `<skill>` block | §4 |
| 4b | Canonical-name form `$mattpocock-skills:implement` on a follow-up | **The bridge refuses to send it at all** — `BridgeError` before any network call. Contradicts the brief | §4 |
| 5 | The ruling hook / machine log record the delivered slash-command answer verbatim | **Yes on Codex**, by artefact. **Was no on Claude** — #187's Run showed the child expanding it while the log kept a placeholder; the cause was the composer's clearing lag, fixed in [#191](https://github.com/okqixiaobao727-design/agentcrew-dev-skills/issues/191), and Claude now records it too | §5 |
| 6 | The bounded-read / red-line guard hooks do not refuse the expanded skill's first actions | **No refusal on Claude.** On Codex the question does not arise — the guards are never installed for a Codex child | §6 |

## 1. Claude, fact 1: `/implement <path>` as a later turn

The child was launched, answered its `initialPrompt` with `READY`, and went idle. Then
`/implement /tmp/crew182/dummy-ticket.md` was typed into the live composer.

**The autocomplete overlay never appeared.** The pane captured after the literal text and before
the `Enter` shows the composer holding the line with no popup drawn over it:

```
────────────────────────────────────────────────── crew-probe ─
❯ /implement /tmp/crew182/dummy-ticket.md
────────────────────────────────────────────────────────────────
```

The trailing ` <path>` argument closes the completion popup, so the `Enter` that
`type_into_pane` sends reaches the composer as a submit. `composer_holds` returned `False`
— the line left the composer on the first `Enter`.

**The skill expanded.** The transcript records a command block, not a plain user message:

```
<command-message>mattpocock-skills:implement</command-message>
<command-name>/mattpocock-skills:implement</command-name>
<command-args>/tmp/crew182/dummy-ticket.md</command-args>
```

followed by a second user message carrying the SKILL.md body verbatim
(`Base directory for this skill: …/skills/engineering/implement`). The child then did the
skill's own opening thing — read the named ticket path — rather than replying in prose.

**Unanticipated: the composer canonicalises the name.** `/implement` was typed; the submitted
message is `/mattpocock-skills:implement`. Claude Code resolved the unambiguous short name to its
plugin-qualified form at submit time. The overlay did not alter the *target*, so the brief's
worry — "may select a different entry" — did not materialise here; but it does mean the text the
driver types and the text the child receives are not always the same string, which matters for any
future check that compares them.

Confidence: **high**. Direct transcript evidence, reproduced twice (§6 repeats it).

## 2. Claude, fact 2: `/mattpocock-skills:triage <path>` as a later turn

Same child, next turn. Same result — no overlay before `Enter`, `composer_holds` `False`, and the
transcript records:

```
<command-message>mattpocock-skills:triage</command-message>
<command-name>/mattpocock-skills:triage</command-name>
<command-args>/tmp/crew182/dummy-ticket.md</command-args>
```

The already-canonical form passes through unchanged. Confidence: **high**.

## 3. Claude, fact 3a: position matters, and failure is silent

Both orderings were delivered as one message (lines joined with `S-Enter`, per
`driver.py:718`).

**Preamble first — no expansion.** `"Ruling: proceed.\n/implement <path>"` arrived as an ordinary
user message. The transcript entry is the literal text:

```
'Ruling: proceed.\n/implement /tmp/crew182/dummy-ticket.md'
```

No `<command-name>` block, no SKILL.md injection. The pane shows it unrewritten (`❯ Ruling:
proceed.` / `  /implement …`), and the child answered it as prose.

**Slash first — expansion, preamble absorbed.** `"/implement <path>\nRuling: proceed."` produced:

```
<command-message>mattpocock-skills:implement</command-message>
<command-name>/mattpocock-skills:implement</command-name>
<command-args>/tmp/crew182/dummy-ticket.md
Ruling: proceed.</command-args>
```

The trailing preamble became part of `<command-args>` — it reaches the skill as arguments, not as
a separate instruction.

**The important part is the failure mode.** In the preamble-first case nothing errored: the
`Enter` submitted, `composer_holds` returned `False`, and `Loop.deliver` would have recorded the
ruling as delivered. The brief's concern is confirmed in a different shape than expected — a
mis-*targeted* delivery, not a mis-*submitted* one, and `composer_holds` cannot see it because it
only checks whether a line is left standing (`driver.py:738`). A ruling whose slash command is not
the first characters of the message is silently downgraded to prose.

Confidence: **high**.

## 4. Codex: facts 4a, 4b and 3b

Three follow-ups were sent through the real bridge into one live thread
(`01a062b1-09db-7251-ba1f-e7cf0c0e08c4`), which had already answered its launch turn with `READY`.

**4a — `$implement <path>` at the start of the message: injected.** The rollout carries the user
message at ordinal 19 and, at ordinal 21, a separate `response_item` of role `user`:

```
<skill>
<name>mattpocock-skills:implement</name>
<path>…/mattpocock-skills/1.2.3/skills/engineering/implement/SKILL.md</path>
---
name: implement
description: "Implement a piece of work based on a spec or set of tickets."
disable-model-invocation: true
---
…
```

611 characters — the same block size `docs/research/codex-opening-skill.md` measured on the launch
path. So the follow-up path and the launch path produce an identical injection, as the brief
predicted: `cmd_send` builds its turn through the same `turn_input`
(`codex_bridge.py:438`, called at `:831`), and the name in the structured item (`implement`, not
the canonical name) is irrelevant because a structured item resolves **by path**. The child's next
message confirms it read the skill's rules rather than the bare text.

Confidence: **high**.

**4b — the canonical-name form never leaves the bridge.** The brief asks whether
`$mattpocock-skills:implement` resolves on a follow-up "so the delivery does not depend solely on
`turn_input`'s start-anchored regex". It cannot be asked that way, because the bridge rejects it
before sending. `opening_skill_name`'s character class is `[A-Za-z0-9][A-Za-z0-9_-]*`
(`codex_bridge.py:359`) — **no colon**. So the regex matches only the prefix:

```
opening_skill_name('$implement /a/b')                  -> 'implement'
opening_skill_name('$mattpocock-skills:implement /a/b') -> 'mattpocock-skills'
opening_skill_name('Ruling: proceed.\n$implement /a/b') -> None
```

and `resolve_skill_path('mattpocock-skills')` then raises:

```
BridgeError: Skill 'mattpocock-skills' has no unique existing SKILL.md in
installed Codex plugin 'mattpocock-skills'
```

`turn_input` propagates it, so `send` fails outright. **A ruling that opens with the canonical
Codex mention is not delivered at all.** This is the mirror image of the Claude side, where the
canonical form is the one that works and the short form is silently rewritten *into* it.

Confidence: **high** (mechanism read at `codex_bridge.py:357-437` and executed directly against
the installed plugin).

**3b — a preamble suppresses the structured item, but the skill is still injected.** The brief
states that on Codex "a protocol preamble ahead of the `$` suppresses the structured item, and the
core text resolver will not rescue a bare name." The first half is right; the second half is
right only for a *bare* name, and the conclusion drawn from it is wrong.

`"Ruling: proceed.\n$mattpocock-skills:implement <path>"` was sent. `opening_skill_name` returned
`None`, so `turn_input` attached **no** structured item — yet the rollout shows, at ordinal 37, a
second `<skill>` block byte-identical to the one at ordinal 21. Codex's own core resolver scanned
the message text, matched the canonical name, and injected the skill. Position is irrelevant to
it, exactly as `codex-opening-skill.md` §2 describes.

The negative control pins the boundary: `"Ruling: proceed.\n$implement <path>"` (ordinal 59)
produced **no** third block — the rollout total stayed at 2. `implement` is not a skill name;
`mattpocock-skills:implement` is.

So on Codex the deciding factor is the **name form**, not the position:

| Follow-up message | Structured item | `<skill>` block |
|---|---|---|
| `$implement <path>` | yes (by path) | 1 |
| `<preamble>\n$mattpocock-skills:implement <path>` | no | 1 (core resolver) |
| `<preamble>\n$implement <path>` | no | 0 |
| `$mattpocock-skills:implement <path>` | — | send fails (`BridgeError`) |

Confidence: **high** for the three rows observed in the rollout; **high** for the fourth, which is
a deterministic refusal reproduced directly.

## 5. Fact 5: is the delivered slash command recorded verbatim?

**Codex: yes, by artefact.** `cmd_send` calls `log_message` with the prompt it sent
(`codex_bridge.py:848`). The bridge's log for the three sends holds them exactly, newlines
included:

```
coordinator | '$implement /private/tmp/crew182-scratch/dummy-ticket.md'
coordinator | 'Ruling: proceed.\n$mattpocock-skills:implement /private/tmp/crew182-scratch/dummy-ticket.md'
coordinator | 'Ruling: proceed.\n$implement /private/tmp/crew182-scratch/dummy-ticket.md'
```

Note the launch turn is *not* in that log — `log_message` is reached from `cmd_send`
(`:848`) and from the watch loop (`:929`), not from `cmd_launch`.

**Claude: no — established by observation, and the answer is negative.** For a Claude child the
keys pass no hook, so `Loop.deliver` writes the instruction into the log itself via
`record_ruling` (`driver.py:2809-2820`), recording `keys` and `text` joined by a space, and only
after `type_into_pane` returned. That last clause is the row: `type_into_pane` did not return.

#187's live acceptance ran the full driver Run this row asked for — a scratch Run under `/tmp`
on the local tracker, whose queued Claude child was answered with `driver.py answer --text
'/implement <path>'`. The child's `design` escalation is in that Run's log at
`2026-09-02T21:13:22Z` (`escalation`, ticket 02), and it received and expanded the ruling: its
transcript record 99 is a `<command-name>/mattpocock-skills:implement</command-name>` block
carrying the ticket path, and it went on to implement and complete at `9f25783`. **But the
`answer` failed the composer check** — `02's instruction remained in the composer at @125` — so
`record_ruling` never ran, and the only `ruling` line the log holds for that delivery, at
`2026-09-02T21:15:22Z`, is the hand-over placeholder. The run report's Rulings section shows the
same. So the slash command reached the child and did not reach the log.

Two controls pinned it to the slash command rather than to the pane or the timing. Prose typed
into that same pane two minutes later (`2026-09-02T21:17:07Z`) was recorded verbatim, and
`$implement <path>` to the Codex child (`2026-09-02T21:24:14Z`) was too.

**Cause, and the fix.** Neither `Enter` was at fault, and the autocomplete overlay §1 ruled out
was not involved either: `type_into_pane` read the composer too early. #191's timing probe
(Claude Code 2.1.258, a throwaway child driven with the real `composer_holds` and the same bytes)
measured the driver's whole retry — first `Enter`, check, second `Enter`, check — completing in
about 30ms, while a Claude composer takes about 20ms to clear prose and up to about 100ms to
clear a slash command, whose name is resolved and whose skill body is loaded before the input is
cleared. Both checks landed inside that lag, both read the typed line, and `stuck` was raised for
a message the child had already received. The #187 child's single command record 99 is what this
predicts: the first `Enter` submitted and the second fell on an empty composer.

`type_into_pane` now polls the composer for up to `COMPOSER_CLEAR_SECONDS` after each `Enter`
before calling that `Enter` dropped, so a slash-command ruling Claude accepts on the first press
is recorded once, verbatim (#191). The driver suite's tmux stub grew a `tmux-linger-reads` knob
that holds the typed line in the composer for a set number of `capture-pane` reads, which is what
lets `DiagnosingChildChainTests` and `AnswerTests` fail on the old single read and pass on the
polled one.

#191's own live acceptance ran both drivers against one scratch Run and the same live Claude child
(Claude Code 2.1.258, Haiku 4.5, a 210x43 detached tmux window): the `crew/72` driver answered
`01's instruction remained in the composer at @135` and wrote no `ruling`, while the child's pane
shows it expanded `/mattpocock-skills:implement` all the same; the fixed driver recorded that same
ruling verbatim, and a prose ruling on the same pane after it, each from a single submission.

Confidence: **high** for Codex; **high** for Claude, now by direct observation in a Run.

## 6. Fact 6: do the guard hooks refuse the expanded skill's first actions?

**Claude: no refusal.** The first probe ran without guards, so the child was relaunched into the
same worktree after installing all three `GUARD_ASSETS` — `red-line.sh`, `worktree-guard.sh` and
`settings.local.json`, with `<WORKTREE_ABSOLUTE_PATH>` substituted, exactly as
`install_guard_assets` does (`dispatch.py:535-548`, list at `:109-111`). `/implement <path>` was
delivered again as a later turn. The skill expanded as in §1, wrote `PROBE.md` in the worktree
(passing the `Edit|Write` matcher on `worktree-guard.sh`) and got as far as proposing a `git
commit` (passing the `Bash` matcher on `red-line.sh`). No hook denial appears anywhere in the
transcript. The only interruptions were ordinary permission prompts for reads outside the
worktree, which are the harness's, not the guards'.

**Codex: the question does not arise.** `install_guard_assets` is called from exactly one place —
`launch_claude_child` (`dispatch.py:775`) — and the assets it installs are a `.claude`
directory. A Codex child is never given them. Whatever refuses a Codex child's actions, it is not
these hooks.

Confidence: **high** for both halves.

## 7. Version re-confirmation, and what `codex-opening-skill.md` §2 still says

`codex-opening-skill.md` §2 was established at codex-cli 0.150.1. Its name-resolution table was
re-confirmed at **0.152.1** without re-deriving it, partly by the §4 probes above and partly by one
`codex exec` run:

| Form | §2 at 0.150.1 | Observed at 0.152.1 | Where |
|---|---|---|---|
| `$implement …` as plain text | 0 blocks | 0 blocks | §4 negative control, ordinal 59 |
| `$mattpocock-skills:implement …` as plain text | 1 block | 1 block | §4, ordinal 37 |
| `[$implement](<abs SKILL.md path>) …` | 1 block | 1 block | `codex exec` probe, rollout `…01a062b7…jsonl`, one `<skill>` block |

No contradiction with §2 at the newer version. Confidence: **high**.

## 8. Where these results contradict the brief or ADR-0028

1. **The brief's item 3 — "row 3 is not symmetric across executors" — is right about the mechanism
   and wrong about the outcome.** It concludes that on Codex "a protocol preamble ahead of the `$`
   suppresses the structured item, and the core text resolver will not rescue a bare name."
   The rescue does happen, for the canonical name (§4, ordinal 37). The asymmetry is real but runs
   the other way from what the brief implies: **Claude is the position-sensitive executor, Codex is
   the name-sensitive one.**

2. **Row 4b as written cannot be tested.** It asks whether the canonical-name mention resolves on a
   follow-up "so the delivery does not depend solely on `turn_input`'s start-anchored regex". The
   bridge raises `BridgeError` on that string before sending (§4). Delivery through the bridge
   *does* depend on that regex — but only for whether a structured item is attached; the core
   resolver is an independent second path that the bridge cannot reach with a canonical mention,
   because the bridge rejects the message first.

3. **ADR-0028's delivery paragraph holds, narrowly.** Both `/implement <path>` on Claude and
   `$implement <path>` on Codex work as a second turn, provided the mention is the **first
   characters of the message**. Nothing in this spike suggests the mechanism is unavailable. What
   it does show is that each executor has a distinct silent-failure shape at the edges: on Claude a
   preamble downgrades the ruling to prose with no error anywhere; on Codex the canonical name form
   fails loudly at the bridge, and a bare name after a preamble injects nothing while the message
   still arrives.

## 9. Open questions

- ~~**Does `record_ruling` log a Claude slash-command answer verbatim in a real Run?**~~ Closed
  twice over. #187's live acceptance ran that driver run and found it did not, because
  `type_into_pane` did not return for a slash command; #191 established why — the composer's
  clearing lag, not the overlay §1 had left open — and fixed it, so it now does (§5).
- **Does the autocomplete overlay ever alter the submitted text when the command takes no
  argument?** Every Claude probe here ended in a path argument, which closes the popup. A ruling
  that is a bare `/implement` with no argument was not tested.
- **Is the short-name canonicalisation (§1) guaranteed, or does it depend on the short name being
  unambiguous across installed plugins?** Only one `implement` skill is installed on this machine,
  so ambiguity was never exercised.
- **`opening_skill_name` has no test coverage.** Narrowed by #187: `turn_input` is now exercised
  under `send` by `DiagnosingChildChainTests.structured_skill_items`, which calls it with
  `resolve_skill_path` stood in and asserts the structured item a ruling and a queued opening line
  each produce. `opening_skill_name` is still reached only through it, never directly.
