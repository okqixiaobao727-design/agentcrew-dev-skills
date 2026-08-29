---
status: accepted
---

# Driver activates every Wave through one path

Fresh Wave 1, adoption, explicit resume and post-advance launch currently reach dispatch through
different paths. A launch interrupted before its Machine-log record can therefore leave the Driver
waiting on a planned ticket that no process watches, while failures before the wave loop omit the
run's resume command. This ADR amends ADR-0010's consequence that these module responsibilities do
not move: the Driver now owns one activation module whose interface is to make a named Wave ready
to poll, whichever path reached it.

The module combines the Run plan, Run projection and one current reading of the executor's existing
live source. A recorded launch is adopted and never redispatched; a launch whose post-launch
verification failed is checked once more, then adopted or reported. Without a launch record, an
existing child is adopted, a child confirmed absent is dispatched, and an unknown reading stops
with a recoverable Driver error. Each `/crew` invocation makes at most one dispatch attempt, leaves
already launched siblings running on failure, and never adds a background retry loop.

The Driver gives dispatch the exact tickets to execute; dispatch neither reads the Machine log nor
chooses work. Advance lands the current Wave and identifies the following one, then returns control
to the Driver for activation. Adoption restores the run's hooks and coordinator address before
activation, and every activation failure is handled inside the wave loop so its wake snapshot
carries the resume command.

Run-plan meaning remains owned by ADR-0018 and Machine-log facts remain owned by ADR-0017. This
decision adds no Ticket state, Machine-log event, discrepancy framework, retry policy or second
workflow engine, and it does not change the existing rule that a launched child confirmed vanished
settles failed rather than being relaunched.
