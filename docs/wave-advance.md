# Wave advance

The run advances itself. One settled wave goes in, one decision comes out, and the coordinator is
not consulted about any of it: the plan the user approved up front is the whole authority the
chain runs on ([ADR-0001](adr/0001-coordinator-spends-tokens-only-on-judgment.md)).

The landing and advancement classifier is
[`skills/crew/assets/advance.py`](../skills/crew/assets/advance.py). The crew Driver owns Wave
activation after that script returns the following Wave.

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
5. **Return the next Wave to the Driver**, when there is one and the wave was green. The Driver
   invokes the one activation path used for both the first and later Waves. Activation adopts
   children already visible in the existing live sources, rechecks a recorded launch whose
   verification failed, and dispatches only the missing ticket ids through
   [`dispatch.py dispatch`](../skills/crew/assets/dispatch/dispatch.py). Dispatch receives the
   Run projection's latest landed `merge.sha` as `--base-commit`, or the RunPlan integration base
   before any merge has landed. It never uses a current integration HEAD that may include tracker
   close commits.
6. **Commit the Wave transition only after activation succeeds.** The Driver writes the existing
   `advance: launched` event after every planned child is accounted for. A following-Wave
   activation Driver error writes `advance: escalated` and returns a recovery error with `resume`;
   it never advances the projection's current Wave. Tracker closes follow that decision, so their
   administrative commits never enter the next Wave's base.

## The four decisions

They are recorded as `advance` events in the machine log
([`docs/machine-log.md`](machine-log.md)):

| Decision | The run | Exit |
| --- | --- | --- |
| `launched` | next Wave activated; Driver toasts `crew wave <N> launched` | 0 |
| `complete` | that was the last wave, and there is nothing to advance to | 0 |
| `escalated` | landing or following-Wave activation stopped | 1 (advance) / 2 (Driver) |
| `interrupted` | the operator stopped the run | 130 |

A fifth decision, `stopped`, belongs to the log rather than to this script: the wave loop appends
it against the wave that ended when the escalation it read leaves nothing to launch and nothing to
rule on, because `escalated` alone cannot tell a run that ended from a wave awaiting a ruling
([`docs/machine-log.md`](machine-log.md)).

`launched` is recorded against the Wave that started. An activation `escalated` is recorded
against the Wave that was attempted; the landing forms of `escalated`, `complete`, and
`interrupted` are recorded against the Wave that ended.
`RunProjection.current_wave` follows only the last `launched` event. Consequently a partial
activation leaves it at the preceding successfully launched Wave: resuming advances that Wave
again, activation adopts already-started children and dispatches only missing ticket ids, and the
single successful `launched` event then moves the projection forward.

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

Because a halt launches nothing, a blocked ticket does not start while its root remains stopped —
the halt is what makes that true, not a filter somewhere downstream. If newer verified evidence
repairs the root and advancement later launches the descendant, that launch begins its current
settlement epoch. The earlier blocked outcome remains auditable, but it no longer settles the live
child.

## The operator's interrupt

Inside the advance command, SIGINT or SIGTERM — Ctrl-C in its window — is taken at the next step
boundary. A merge already in flight is left to finish and is shielded from that signal, because a
merge torn down halfway is exactly the corrupted run state an operator interrupts to avoid. The
command stops with every landing step either finished or never started, and one `interrupted`
decision saying where it stopped.

Once this script has returned the next Wave, activation belongs to the Driver: a successful
activation is committed as `launched`, while one that returns a Driver error is recorded as
`escalated` and is recoverable through the Driver's `resume` path.
