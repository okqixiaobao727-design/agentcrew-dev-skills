---
status: accepted
---

# A live Driver accepts coordinator handover in place

When a Coordinator exits or becomes unusable while its Driver remains live, a new Coordinator
takes over through an in-place **Coordinator handover**: the existing Driver continues running,
records the new Coordinator identity and re-anchors every live child whose coordinator-address
channel changed.
The Driver is not stopped or replaced. Replacement risks overlapping work already in flight and,
after ADR-0024, would also enter Wave activation as a new Driver taking up the run.

Coordinator handover is distinct from attaching a stateless Waiter and from a new Driver adopting
an unfinished run. It completes before ADR-0024's Wave activation or polling continues under the
new Coordinator, so recovered children and children launched afterward receive the same current
identity.

An explicit `/crew <run-dir>` invocation from a Coordinator whose address differs from the run's
current address requests handover even when the old Coordinator process still appears alive. A
stuck or unusable session can remain live at the process level, so liveness cannot veto the manual
recovery action. An invocation from the current address only attaches another Waiter and does not
perform handover.

A successful handover is a hard switch. It atomically supersedes the old Coordinator: every later
Coordinator-originated action must belong to the run's current Coordinator address, and a stale
answer is rejected before it reaches a child or the Machine log. Waiters belonging to the old
Coordinator stop with a superseded notice rather than carrying later wake snapshots back to it.
This decision does not choose the handover transport or its authentication; #112 must settle those
details before it is ready for an agent.
