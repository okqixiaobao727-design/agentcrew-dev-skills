---
status: accepted
---

# The pin names its renderer; the statusline wrapper is a permanent stub

*Recorded after the fact. The decision was made while triaging the first real runs of the pinned
dashboard, and #56 and #58 were written against it, but the entry itself was never written; this
back-fills the record and invents nothing beyond what those tickets already state.*

[ADR-0008](0008-the-pinned-dashboard-lives-in-claude-codes-statusline.md) put the dashboard in
Claude Code's `statusLine`, wired in by `monitor.py pin-install`. The install wrote a wrapper script
ending in `exec <interpreter> <this release's monitor.py> pin`, with both paths taken at install
time. A plugin release lives in a version-pinned cache directory, so the first upgrade after an
install left the wrapper pointing at a `monitor.py` that was no longer there and an interpreter that
might not be either. The statusline then went blank — and worse than blank: the wrapper ended in
`exec`, so a dead reference exited non-zero, and Claude Code blanks the *whole* statusline, the
operator's own readout included, when the statusline command does not exit 0.

Two properties are in tension. The wrapper is written once and must stay correct for years, so it
cannot name anything a release owns. The renderer is version-specific, so something has to name it.

## Considered Options

- **Record the paths in the pin instead of the wrapper. Chosen.** The pin is written at dispatch by
  the running release and removed when the run ends, so both paths it records are alive at the
  moment they are written and are gone before they can expire. The wrapper reads the registry at its
  stable location and runs what the pin names.
- **Have the wrapper find the current release itself** — a glob over the plugin cache, or a marker
  file the installer maintains. Rejected: it makes the wrapper carry a search that has to keep step
  with how the harness lays plugins out, and a glob over version directories has no way to tell
  which one the run was dispatched by.
- **Self-rewriting installs**: each release detects a stale wrapper and rewrites it. Rejected. The
  wrapper is a file in the operator's own configuration, and nothing this project ships edits that
  without being asked. `pin-install --apply`, re-run once by the operator, is the migration.

## Decision

**The pin carries its renderer.** Alongside the run directory, the coordinator's pid and its tmux
session, the pin file records `renderer` — the writing release's own `monitor.py` — and
`interpreter`, the `sys.executable` running it. The full contract is in
[`docs/monitor-dashboard.md`](../monitor-dashboard.md#the-pin-registry).

**The wrapper is a dumb, permanent stub.** It contains no plugin path and no interpreter path. It
reads the pin registry at its stable location; with no pin it prints nothing beyond the operator's
restored previous statusline. Selecting among concurrent pins stays in the renderer: the stub may
run any live pin's renderer and let the existing selection logic choose the run.

**One exception to the silence contract.** ADR-0008 chose silence for render failures, and that
holds. It does not hold for a dead *reference*: when pins are present but none names a renderer and
interpreter this machine has — the paths are gone, the pin is unreadable, or it was written by a
release older than these fields — a single actionable line is printed. Silence there is
indistinguishable from "no run is happening", which is the one thing the operator cannot act on.

**Which half prints it follows from what each half can know.** A shell has no JSON parser, and the
interpreter that would be one is the very thing being looked up, so the stub reads the two paths
out with `sed`. That bootstrap is trusted forwards only: what it finds is tested before it is run;
the renderer it reaches is told it was reached that way (`pin --from-wrapper`) and judges the
registry with a real parser, printing the same line for a file that is not a pin at all; and a path
`sed` cannot read back — one carrying a quote or a backslash — falls through to the line rather
than to a blank statusline. Both halves print one sentence, defined once.

**Every path exits 0**, because the alternative is blanking the operator's own statusline.

## Consequences

- An upgrade needs no re-install: the release that dispatched the run is the release that draws it,
  including for a run dispatched by an older release than the one now installed.
- Operators carrying a wrapper written by an older release re-run `pin-install --apply` once. The
  install already measures its idempotency against the wrapper text it would write, so that
  wrapper is reported as a `rewrite` line rather than "nothing to change"; from this release on,
  that line means a pre-fix wrapper, because the text no longer varies between releases.
- The statusline can now print one line that is not a frame. It is bounded — one line, only for the
  dead-reference case — and it names what to do about it.
- The wrapper reads two string fields out of the pin's JSON in shell. The pin is written for that:
  one line, and non-ASCII paths written as themselves rather than as `\uXXXX` escapes. A renderer
  or interpreter path containing `"` or `\` is the documented bound — such a run is not drawn on
  the statusline, and says so in the one line.
- A stale pin left behind by a run that died without unpinning, whose release has since been
  removed, keeps that one line on screen until the pin file is deleted. Naming the registry in the
  line is what makes that recoverable.
