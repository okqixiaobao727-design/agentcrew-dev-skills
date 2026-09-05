---
status: accepted
---

# A machine lays its own overlay over the committed config

Every decision `agentcrew.toml` carries is the project's: which model repairs a merge, which
tracker closes a ticket, which command gates the base. That is right for the decisions themselves,
and it leaves one kind of fact nowhere to live — a fact about the *machine* a run happens to be
on. The same repository is worked on a laptop with ten cores and on a server with eight; one of
them has a tool the other lacks; one of them is busy with something else while the gate runs. The
only ways to say so were an uncommitted edit to the committed file, which the next `git diff`
review cannot tell from a real change and which `[preflight]` reads out of the working tree
anyway, or a flag on every command — and the Driver's preflight gate takes no flags at all.

The test runner had the same gap at its own seam. `scripts/test.py` is the one validation entry
point (ADR-0016), so the gate, CI and every agent following AGENTS.md name it and nothing else. A
machine that wanted the suites run somewhere else — another host, another interpreter, under a
load limit — could not say so without every caller learning a new command.

## Decision

**`agentcrew.local.toml`, beside `agentcrew.toml` and never committed, is merged over it.** The
Driver's `project_config` reads both, and the overlay wins key by key: a table the overlay names is
merged into the committed table of that name rather than replacing it, so overriding one key leaves
its neighbours as the project committed them. Either file may be absent. An overlay that cannot be
parsed stops the run by its own name, the way the committed file does. The file is gitignored in
this repository; a project that adopts one gitignores it itself, and an untracked one already
passes the dirty-tree check, which counts tracked paths only.

**`[test] runner` hands the run to a command the machine names.** `scripts/test.py` reads the
same two documents, merged the same way. Where `[test] runner` is a non-empty argv list, the
script starts it with its own arguments verbatim, in the repository root, and exits with its
status; it says so on stderr first. `--no-delegate` runs the suites where the script is invoked,
which is what a runner passes when it comes back to this script — and what keeps a copy of the
tree, overlay included, from delegating a second time. A runner that is not an argv list, or that
cannot be started, is an error, not a fallback to running here: a machine that asked for its
runner and quietly got the local suites instead is exactly the outcome the key exists to prevent.

**No caller changes.** `[preflight] gate` stays `["python3", "scripts/test.py"]`, AGENTS.md still
names that command, and CI runs it on a runner with no overlay. The machine decides what the
command does; the command every caller names is the same one.

## Consequences

A machine-level fact has a file of its own, read wherever the project's config is read, and the
committed file stays a clean record of the project's decisions. A `git diff` on `agentcrew.toml`
is a real change again. The test runner gains one optional key and one flag, and its behaviour
with neither present is unchanged; the runner's own tests assert the hand-over, the exit status,
the merge order and the two refusals against a throwaway tree. Readers that take the config
*file's path* rather than the Driver's merged document — the dashboard's `--config` — still see
the committed file alone; each of them is a one-line change to route through `project_config`
when a machine-level fact first needs to reach it.
