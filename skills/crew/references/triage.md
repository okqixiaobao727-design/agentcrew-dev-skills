# Triage: rule on a child

Reached from a `judgment-needed` or `driver-error` wake snapshot, whose `detail`, `ticket`, `child`
and `window` are what you rule from: a `CREW ASK` a child sent, a semantic conflict it bounced back
a second time, or a state the rule table has no row for. A Claude child `waiting` at a permission
prompt is that last case — read the question with `tmux capture-pane -p -t <window>`.

## Decide

Apply the authority contract in `SKILL.md`: approve reversible actions and record an exact undo
for effects outside the worktree. Park actions with no credible undo. Prefer a reversible
alternative when one exists.

Rule from the message as sent — pick an option, redirect the approach, or sketch direction in
pseudocode; the child implements. When the ASK lacks the evidence to decide, ask the child on
its channel for a distilled summary; the child's files stay unopened.

## Answer an ASK

Reply to a Claude child by SendMessage to its recorded name, ending with `ts=<unix time>` —
identical bodies are silently dropped as duplicates.

A Codex child has no message channel: its answer is its next turn, delivered through the bridge.

```bash
python3 <crew-skill-dir>/assets/codex/codex_bridge.py send \
  --state-file <run-dir>/codex/<NN>.json --prompt-file <answer-file>
```

## Answer a permission prompt

A Claude child alone reaches this: Codex children run with approvals off. Only send keys while the
session reports `waiting`.

- Numbered option: send its digit; it submits immediately.
- Arrow option: send `Up`/`Down`, then `Enter`.
- Free text: select its numbered row, type with `send-keys -l`, read the text back from the pane,
  then send `Enter`.
- Option plus caveat: use free text because a numbered option submits before a caveat can be added.
- Multiple lines: use `S-Enter` between lines; a literal newline or plain `Enter` submits.

Example free-text sequence:

```bash
tmux send-keys -t <window-id> "4"
tmux send-keys -t <window-id> -l "Use the existing retention_audit table"
tmux capture-pane -p -t <window-id>
tmux send-keys -t <window-id> Enter
```

## Log

The ruling hook copies every Claude message you send into the machine log verbatim as you send it,
so an answer is logged by being sent. A Codex answer goes through `codex_bridge.py send`, which
records the prompt in the same way from the state file's machine-log configuration. The driver
builds the report from these lines, so name the effect and its exact reversal inside the ruling
itself whenever you approve something outside the worktree.

An answer sent as tmux keys passes no hook and so reaches no log. The driver has already written a
`CREW RULED` line for the escalation it woke you on; where the ruling itself belongs in the record,
send it to that child as a message too.

## Park

When no credible undo exists, tell the child to leave the work where it stands, write its parked
checklist, and send `CREW PARKED <checklist path>`. The driver records the parked receipt, puts the
child's worktree where the wake monitor reads it as parked, and blocks that ticket's descendants
when the wave settles.

**Done when** the answer is sent and logged, or the child has been told to park and settle by
receipt.
