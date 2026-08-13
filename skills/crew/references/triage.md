# Triage: rule on a child

Reached from a `CREW ASK` — sent by a Claude child as a message, by a Codex child as its turn's
`finalMessage` — or when the monitor reports a Claude child `waiting` at a permission prompt (read
that question with `tmux capture-pane -p -t <window-id>`).

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
python3 <bridge> send --state-file <state-dir>/<NN>.json --prompt-file <answer-file>
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

Append every answer to `<feature-dir>/decisions.md`:

```text
## <NN> <ticket title> — <timestamp>
Q: <question>
A: <answer and reason>
```

For an effect outside the worktree, also append:

```text
## OUTSIDE WORKTREE — <NN> <effect> — <timestamp>
Effect: <what changed and where>
Undo: <exact reversal>
```

## Park

When no credible undo exists, leave the child where it stands, rename its window `<NN>?`, append
its exact worktree path to the wave's parked-path file, and log the blocked action. Descendants of
that ticket are blocked.

**Done when** the submitted answer matches the log, or the parked ticket, blocked action, parked
path, and window marker are all recorded.
