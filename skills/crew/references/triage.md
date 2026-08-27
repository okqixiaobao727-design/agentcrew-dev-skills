# Triage: rule on a child

Reached from a `judgment-needed` or `driver-error` wake snapshot. Rule from its `detail`, `ticket`,
`child` and `window`; a child escalation also carries its fact-check in `brief`. A failed fact-check
carries an empty `brief` and a plain-string `witness_reason` beside it; that field is absent on
success. The snapshot may instead carry a semantic conflict bounced back a second time, or a state
the rule table has no row for. A Claude child `waiting` at a permission prompt is that last case —
read the question with `tmux capture-pane -p -t <window>`.

## Decide

Apply the authority contract in `SKILL.md`: approve reversible actions and record an exact undo
for effects outside the worktree. Park actions with no credible undo. Prefer a reversible
alternative when one exists.

Rule as the Contract says — from the ASK and its witness brief — by picking an option, redirecting
the approach, or sketching direction in pseudocode; the child implements. When the ASK lacks the
evidence to decide, ask the child on its channel for exactly the pointer it lacks.

When a ruling asks a child to carry information back in its receipt, say that the information goes
on the lines *above* the verb line. The receipt verb is matched as a whole line; a ruling that has
a child append prose to it produces a receipt the run refuses, which the driver bounces once and
then settles `failed` (ADR-0015).

## Place leftovers

A `wrap-up` is ruled line by line: every leftover the child listed gets one of the three placements
the Contract names, and the ruling is one message back on the child's channel. Write it so the
report can set each line beside its placement — repeat the leftover's own line, then the placement:

```
<leftover line as the child wrote it> — this ticket
<leftover line as the child wrote it> — opened <ticket reference>
<leftover line as the child wrote it> — dropped
```

A line ruled *this ticket* keeps the child running: it makes the edit and sends its receipt when
done. A line ruled *opened* names the ticket you opened (below) before the ruling is sent, so the
child's receipt and the report both carry the reference. The ruling hook copies the message
verbatim, so the placements need no second write.

## Open a ticket

You open follow-up tickets yourself, during the run, through the tracker convention the repository
names in `docs/agents/issue-tracker.md` — that document holds the command, and
[`references/trackers.md`](../../../references/trackers.md) names the operations both skills call
on it. Seed the ticket from the wrap-up line: its body carries the leftover as the child stated it
and every pointer the child cited, so the evidence travels with the ticket rather than with your
memory of the run. Once the Tracker module lands
([ADR-0019](../../../docs/adr/0019-tracker-owns-ticket-operations-callers-own-workflow.md), #123),
creating a ticket goes through its create operation on either tracker; until then the convention
document's command is the path.

## Rule on an acceptance run

Whether a ticket's acceptance runs at all is your ruling, and the ruling is the whole of it — a
per-run word from the human is never part of the path. Rule it as any reversible action: approve,
and write the exact undo inside the ruling. Credentials come from the project's own convention,
which the child reads; a run whose credential the run lacks is parked, as in *Park* below, and
the human supplies it on their own time.

## Speak to the human

The Contract names exactly four decisions that are the human's — product direction, a material
change of architecture, a widening of product scope, an action with no undo. One of those reaches
them as one message carrying the decision and its options; everything else is silent, and status
is theirs to ask for.

## Answer an ASK

Reply to a Claude child by SendMessage to its recorded name, ending with `ts=<unix time>` —
identical bodies are silently dropped as duplicates.

A Codex child has no message channel: its answer is its next turn, delivered by the driver over
whatever transport the child was launched on, so ruling never asks you which one that is.

```bash
python3 <crew-skill-dir>/assets/driver/driver.py answer \
  --run-dir <run-dir> --ticket <NN> --text "Use the existing retention_audit table"
```

## Answer a permission prompt

A Claude child alone reaches this: Codex children run with approvals off. Only answer while the
session reports `waiting`; the command delivers the answer and records it in one action.

- Numbered option:

  ```bash
  python3 <crew-skill-dir>/assets/driver/driver.py answer \
    --run-dir <run-dir> --ticket <NN> --key 4
  ```

- Arrow option:

  ```bash
  python3 <crew-skill-dir>/assets/driver/driver.py answer \
    --run-dir <run-dir> --ticket <NN> --key Down --key Enter
  ```

- Free text: select its numbered row with `--key`, then pass the text with `--text`:

  ```bash
  python3 <crew-skill-dir>/assets/driver/driver.py answer \
    --run-dir <run-dir> --ticket <NN> --key 4 \
    --text "Use the existing retention_audit table"
  ```

- Option plus caveat: use `--key <row> --text <answer>` because a numbered option submits before a
  caveat can be added.
- Multiple lines: use shell's `$'line one\nline two'` form for `--text`; the command sends `S-Enter`
  between lines and `Enter` to submit.

The accepted `--key` names are single digits `0`–`9`, `Up`, `Down`, `Left`, `Right`, `Enter`, and
`S-Enter`.

## Log

The ruling hook copies every Claude message you send into the machine log verbatim as you send it,
so an ordinary ASK answer is logged by being sent. A Codex answer goes through `driver.py answer`,
which relays it over the bridge; the bridge records the prompt as it sends it, and the marker it
rotates is what makes the child's next turn visible to the watch. Permission answers go through
`driver.py answer` too: it delivers first, then reuses the coordinator message event shape for the
ruling. A delivery failure is surfaced and writes no ruling. The driver builds the report from
these lines, so name the effect and its exact reversal inside the ruling itself whenever you
approve something outside the worktree.

## Park

When no credible undo exists, tell the child to leave the work where it stands, write its parked
checklist, and send `CREW PARKED <checklist path>`. The driver records the parked receipt, puts the
child's worktree where the wake monitor reads it as parked, and blocks that ticket's descendants
when the wave settles.

**Done when** the answer is sent and logged, or the child has been told to park and settle by
receipt.
