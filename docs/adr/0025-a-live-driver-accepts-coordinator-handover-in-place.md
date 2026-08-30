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

The invoking `/crew` waits synchronously for a handover result and never reports success while the
Driver is still switching ownership, re-anchoring children or retiring old Waiters. It does not
enter ordinary Waiter attachment before that result. The existing trust boundaries remain the
whole protocol: the launcher resolves the invoking Coordinator from the harness session registry,
the live Driver applies the handover and uses its existing child-delivery path to re-anchor, and a
Coordinator action is accepted only when its resolved address is the run's current address. No
token, lease, handover state machine, rollback protocol, retry counter or background recovery loop
is added. A failure uses the existing Driver-error and re-typed `/crew` recovery path and continues
forward toward the new Coordinator rather than restoring the old one.

Coordinator control is one deep Module rather than a handover branch copied into each caller. Its
Interface has three role-shaped operations: the launcher attends with one resolved Coordinator
context, the live Driver services control before every Wave activation and poll, and an authorized
action boundary encloses Coordinator-originated side effects. Attendance owns all three cases —
starting a missing Driver, attaching to a live Driver at the same address and synchronously handing
over a live Driver at a different address — together with the Waiter lifecycle. The launcher does
not inspect Run metadata and choose among those cases itself. Cross-process request correlation,
atomic replacement, ordering and local control artefacts are Implementation hidden behind that
Interface; they are not Machine-log facts and do not add a Driver socket server.

The immutable Coordinator context groups the new Coordinator's name, pid, harness session ID,
address, pane, permission mode and tmux display session. The address alone is the authorization
identity. A handover applies that context as one unit: name, pid, harness session, address and
permission mode replace the corresponding recorded and in-process facts; the pane updates
no-Waiter recovery; and the display session re-pins the dashboard. It also re-scopes the
Coordinator hook, updates later child launches and reuses the existing live-child re-anchor
Implementation. The Run's existing tmux session remains the execution location of its Driver and
child windows, and its coordinator configuration home continues to fix the inherited account. The
display session does not move those windows, and handover does not change that account.

Waiter ownership changes in the same handover commit. The new invocation is registered as the
current Coordinator's Waiter before success is returned, and old-address registrations are made
ineligible to carry a later wake. Their processes are not part of the commit: each old Waiter
checks ownership before reading a wake, prints the superseded notice and removes its own record.
Handover therefore does not wait for an old shell process to exit and has no gap in which a wake
belongs to neither Coordinator.

The authorized-action boundary owns the address check and the side effect it can enclose. The
Driver answer command resolves its caller and crosses that boundary for delivery and Machine-log
recording. An ordinary Claude `SendMessage` keeps its existing interface: a Coordinator-scoped
pre-tool hook rejects a stale sender before the send, the re-anchored child keeps the address as
its trust boundary, and the post-tool hook appends only for the current sender. These callers reuse
Coordinator control's authorization and ordering instead of reproducing current-address tests.
