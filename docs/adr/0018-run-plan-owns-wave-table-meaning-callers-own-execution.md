---
status: accepted
---

# Run plan owns Wave-table meaning; callers own execution

Wave-table knowledge is currently divided between the Driver, dispatch, advance, merge driver,
monitor and route staging: construction, validation, JSON reading, wave lookup, ticket traversal and
dependency interpretation do not have one owner. A change to the plan shape therefore requires
coordinated edits across modules that should only need the resulting plan.

One **Run plan** module owns the complete meaning of the Wave table. It builds a plan from ticket
input, resolves and validates it, reads and writes its existing JSON representation, and provides
immutable run, wave, ticket and dependency facts. Route staging and the Driver use that module to
build or check a plan; dispatch, advance, merge driver and monitor consume the validated plan. No
caller parses the Wave-table JSON or reimplements plan queries.

The Run plan contains only what should happen. It does not read the Machine log, decide the
Driver's next action, launch children, merge branches, close tracker tickets or render the monitor.
Those callers retain execution and presentation ownership. The plan remains reloadable rather than
being hidden behind a write-once cache, so centralising its meaning does not remove the operator's
ability to replace a valid plan.

This is a replacement, not a compatibility layer: migrated construction, validation and query
helpers are deleted from their former callers. The local JSON file remains the sole persistence
form, so the module introduces no storage adapter, alternate schema, cache, version protocol or
second planning framework.
