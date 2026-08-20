# Wave advance

The run advances itself. One settled wave goes in, one decision comes out, and the coordinator is
not consulted about any of it: the plan the user approved up front is the whole authority the
chain runs on ([ADR-0001](adr/0001-coordinator-spends-tokens-only-on-judgment.md)).

The driver is [`skills/crew/assets/advance.py`](../skills/crew/assets/advance.py).

```sh
advance.py advance --table <wave table> --wave N --log <machine log> \
                   --out-dir <launch artifacts> --repair-model <full model ID> \
                   [--repair-budget-usd 2] [--repair-attempts 2] [--repair-timeout 900]
```

The wave table is the same file the dispatch renderer and the merge driver read, with the same
authority (ADR-0003). The repair flags are the merge driver's and are passed straight through;
`docs/merge-driver.md` publishes what they do.

## The chain

1. **The wave must have settled.** Every ticket of wave N carries a `receipt` or an `outcome` in
   the machine log. A wave still being worked is a caller that called too early: nothing is
   merged, nothing is decided, and the tickets still live are named on stderr.
2. **It must not already have been advanced past.** A wave whose log carries a `launched` decision
   for the wave after it, or a `complete` for itself, is refused: a second run would start that
   next wave again in the worktrees the first one is working in. A wave that `escalated` or was
   `interrupted` is not refused — re-running it is how the run carries on once the coordinator has
   ruled.
3. **Land it.** [`merge_driver.py land`](merge-driver.md) merges every landable branch. A wave the
   driver refuses to start at all — a repository carrying uncommitted work of its own — is the
   same non-decision: the driver's reason is printed and nothing is recorded.
4. **Read the wave back out of the log.** The wave is green when every ticket either settled
   `landable` and merged `clean`, `resolved` or `repaired`, or settled `parked` and has no
   descendants in the wave table. Both halves are read from the log rather than from the driver's
   output, so what advances the run is what the run recorded.
5. **Launch the next wave**, when there is one and the wave was green, through
   [`dispatch.py dispatch`](../skills/crew/assets/dispatch/dispatch.py) — cut from what the wave
   just landed, not from where the run began. The driver reads the integration branch's head after
   the merges and passes it as the renderer's `--base-commit`, which fixes both the commit each new
   worktree is cut from and the base each child's review runs against.

## The four decisions

Each is recorded once, as one `advance` event in the machine log
([`docs/machine-log.md`](machine-log.md)), and is the last line the driver prints:

| Decision | The run | Exit |
| --- | --- | --- |
| `launched` | the next wave is running; the operator is toasted `crew wave <N> launched` | 0 |
| `complete` | that was the last wave, and there is nothing to advance to | 0 |
| `escalated` | failed, parked with descendants, or did not land — chain stops | 1 |
| `interrupted` | the operator stopped the run | 130 |

A fifth decision, `stopped`, belongs to the log rather than to this script: the wave loop appends
it against the wave that ended when the escalation it read leaves nothing to launch and nothing to
rule on, because `escalated` alone cannot tell a run that ended from a wave awaiting a ruling
([`docs/machine-log.md`](machine-log.md)).

`launched` is recorded against the wave that started; the other three against the wave that ended.
The toast goes to `tmux display-message`, the operator's terminal, in the family the monitor's
toasts already speak ([`docs/monitor-dashboard.md`](monitor-dashboard.md)); a run watched from
outside tmux advances without one. The end of the run gets no toast of its own — the monitor
already says `crew wave <N> complete` when the last wave settles.

## What an escalation is, and is not

An `escalated` decision carries the reason and the pointers a ruling starts from — each offending
ticket's number, its verdict, its path and its branch — and there is exactly one per halted wave,
however many tickets are in it.

A parked ticket with no descendants is not an offender. When one is passed over as settled, the
decision detail carries a separate `passed over as settled` note so the report can account for it.

It reaches the coordinator by being in the log, not by being sent: this script writes nothing into
anybody's context (ADR-0001), and the coordinator is already awake, woken by the child's own
`CREW FAILED` or `CREW ASK` or by the merge driver's own escalation. A halt does not re-tell it
what it was already told.

## Blocked descendants

A halt marks every ticket that can no longer start `blocked`, as one `outcome` event each, which
is one of the four outcomes the crew contract gives every ticket. Reached this way:

- the **roots** are the wave's tickets that settled `failed`, or `parked` while having descendants
  — a ticket awaiting a ruling on its merge is not one, because a ruling may yet land it;
- the **descendants** are every ticket that reaches a root through the wave table's `blocked_by`
  edges, however far down: a ticket blocked by a ticket blocked by a failure is blocked too;
- a descendant the log carries a `launch` or a settling event for is left alone — it already ran,
  and it has an outcome of its own.

`blocked_by` is a list of ticket ids on a table ticket, and the table is where it belongs: the
approved table is the run's one routing authority, and an edge kept anywhere else is a second one.
The renderer ignores it. A table that carries no edges blocks nothing, and blocks nothing wrongly.

Because a halt launches nothing, a blocked ticket never starts — the halt is what makes that true,
not a filter somewhere downstream.

## The operator's interrupt

SIGINT or SIGTERM — Ctrl-C in the run's window — is taken at the next step boundary. A merge or
the launch already in flight is left to finish, and is shielded from the signal that reached the
driver, because a merge torn down halfway is exactly the corrupted run state an operator
interrupts to avoid. The run stops with every step it took either finished or never started, and
one `interrupted` decision saying where it stopped.

An interrupt that arrives while the next wave is launching finds no advancement left to stop: the
wave is running, and the decision is `launched`, because that is what happened. Stopping the
children it started is the operator's, on the windows they run in.
