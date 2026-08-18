---
status: accepted
---

# The statusline tick reads the CLI's sessions files; `claude agents --json` is the fallback

The pinned dashboard ([ADR-0008](0008-the-pinned-dashboard-lives-in-claude-codes-statusline.md))
redraws every statusline tick, and each frame needs the agents list — which sessions are alive and
what each is doing. The list was read by spawning `claude agents --json`, a complete CLI start:
~350 MB of transient RSS and 0.58 s of CPU per call, independent of session count, since ~75% of
that footprint is the binary itself being mapped (`claude --version` alone measures 262 MB). Every
pane of the pinned session ticks independently, so one ten-pane run at a two-second interval
spawned ~7 CLIs per second — enough to congest the machine until every call exceeded the tick's
own timeout, at which point the frames drew `unknown` while the full cost was still paid. No
lighter invocation exists: every documented flag and environment switch was measured within noise.

The same information exists on disk. The CLI maintains a JSON file per live session under its
config directory, and against `agents --json` the files measured as the same set of sessions with
byte-identical `cwd`s and a strict superset of fields. But that directory is undocumented — the
official agent-view docs list other files and none of them carry interactive sessions — so nothing
promises it survives an upgrade.

## Considered Options

- **Read the sessions files; keep the command as fallback. Chosen.** The tick's cost drops to a
  few file reads — the same order as drawing the frame — on the path taken essentially always. The
  undocumented dependency is fenced: a tick that cannot read the directory falls back to spawning
  the documented command, sharing the parsed result machine-wide through a small cache file so the
  spawn rate is bounded by a freshness window rather than by pane count. An upgrade that moves the
  directory degrades the dashboard's cost, never its correctness.
- **Keep spawning the command, but cache and lock around it.** A machine-level cache with a
  single-flight lock bounds the spawn rate the same way, but every piece of it — the cache, the
  lock, the lock's expiry — exists to manage a cost the files simply do not have, and a lock file
  is one more thing a crashed process leaves behind in a subsystem whose defect was exactly that.
  Rejected as machinery without a beneficiary.
- **A resident daemon that polls once and serves every tick.** The cheapest steady state and the
  most lifecycle: something must start it, notice it died, and stop it — the pin's whole design
  avoids owning any resident process, which is
  [ADR-0008](0008-the-pinned-dashboard-lives-in-claude-codes-statusline.md)'s "no background
  process". Rejected.

## Decision

The Claude lane's live source reads the per-session files. Only when the directory cannot be read
does the tick spawn `claude agents --json`, writing the parsed result to a machine-level cache
file (atomic rename) that all panes share for a named freshness window; a tick whose fallback also
fails draws `unknown` in silence, as ADR-0008 requires. Both the sessions directory and the cache
resolve through the same config-directory root the pin registry uses, which is also the test seam.

The files' one semantic divergence is kept deliberately visible: their `status` carries `shell`,
which the command folds into `busy`. The lane maps `shell` to `waiting` with its own toast
wording; in fallback mode such a child is necessarily drawn `running`, and that asymmetry is
accepted.

## Consequences

- A tick costs milliseconds on the primary path, so pane count and refresh interval stop being
  load dials; the two-second default beat is affordable again.
- The primary path can never go stale — there is no cache on it — and the fallback is at most one
  freshness window old, on a surface that redraws every two seconds anyway.
- The project now depends on an undocumented directory, by name and by schema. The fallback is the
  contract that makes that acceptable: any breakage must degrade to the documented command, and a
  regression test holds the primary path to zero spawns.
- In the rare instant where several panes see the fallback cache expire together, a few concurrent
  spawns can race. Accepted: the worst case matches one tick of the old storm and ends there,
  which is not worth a lock's failure modes.
