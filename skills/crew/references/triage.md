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

The ruling hook copies every message you send into the machine log verbatim as you send it, so an
answer is logged by being sent. What the log then holds is exactly what you wrote: name the effect
and its exact reversal inside the ruling itself whenever you approve something outside the
worktree, because the report is built from these lines.

An answer sent as tmux keys passes no hook and so reaches no log: it is a ruling only the report
will carry, so carry it there.

## Park

When no credible undo exists, leave the child where it stands, append its exact worktree path to
`<run-dir>/parked-paths` so the wake monitor reads it as parked, and settle the ticket:

```bash
python3 <crew-skill-dir>/assets/machine_log.py --log <run-dir>/log.jsonl \
  receipt --ticket <NN> --verdict parked --detail '<the blocked action and its worktree path>'
```

Descendants of that ticket are blocked, which the advance driver marks when the wave settles.

**Done when** the answer is sent and logged, or the parked ticket's verdict, blocked action, and
parked path are all recorded.
