---
status: accepted
---

# Paths are made absolute where they enter, and compared by realpath, never as strings

The first crew run on v0.2.1 lost its first launch to a relative `--out-dir`. Dispatch recorded
the caller's spelling verbatim into the artifact list, the launch line ran in the child's own
worktree, the read of that artifact missed there, and the operator saw only a verification
timeout: no message named a path, because nothing on that route ever thought about one. The same
root cause sat silent in two more places — the machine-log hook command, which the child runs from
its worktree cwd, and two red-line guard checks that skipped their containment test outright when
the installed worktree value was relative, which is a safety check disabled without a word.

A second class produced the mirror failure. Worktree identity was compared as text, so on macOS
`/tmp/x` and `/private/tmp/x` — one directory under two spellings — were two worktrees: the
dashboard drew live rows as `vanished`, toasts fired on them, and the coordinator was woken to
rule on children that had never gone anywhere.

Both classes share one shape. A path crosses a boundary — a caller's argument, a cwd change, a
symlinked or aliased root — and code downstream trusts the spelling it was handed as the identity.

## Considered Options

For paths entering the system:

- **Validate and reject a relative path.** Turns a silent miss into a loud one, which is better,
  but it keeps an error case the operator has to learn, hit, and fix by retyping. Rejected.
- **Document "pass absolute paths".** A note the caller reads once and the failure ignores: the
  bad path still travels, and the failure is still a timeout with no cause attached. Rejected.
- **Resolve to absolute at the boundary the path enters through.** The failure has no code path
  left to take, so there is no error case, nothing to document, and nothing to retype. Chosen.

For paths being compared:

- **Compare strings, and normalise every producer instead.** Correct only while every producer
  stays correct, and one that drifts breaks a comparison far away from itself, where the evidence
  reads as a vanished child rather than a path bug. Rejected.
- **Compare through realpath at the comparison itself.** Resolution happens where the question is
  asked, so a comparison is right regardless of which spelling reached it. Chosen.

## Decision

Two invariants hold across this repo's scripts:

1. **Absolute at the boundary.** A caller-supplied path is resolved to absolute where it enters —
   argument parsing, template substitution, hook-command rendering — before anything records,
   forwards, or embeds it. What is written down is already absolute, so no consumer's cwd can
   change what it means.
2. **Compare by realpath, never as strings.** Every path-equality and containment check resolves
   both sides first. A path that does not exist yet resolves through its deepest existing
   ancestor, so a place still to be created is compared as the place it will be.

## Consequences

- The dispatch renderer, the wave-advance forwarding path, the machine-log hook command, and the
  red-line guard take relative input without a warning and without a failure; `--out-dir tmp/x`
  and its absolute spelling are one run.
- A guard check handed an unusable worktree value now fails loudly instead of skipping, because
  after resolution the only remaining bad value is one that was never substituted at all.
- Path comparisons in the monitor, the wake monitor, and the red-line guard cost a stat call each.
  That is the price of a comparison that survives macOS's `/private` aliasing, symlinked
  checkouts, and any future root that has two names.
- New code that records a caller's path, or compares two paths, is reviewed against these two
  invariants; either one broken is the same defect class as the ones above, and the evidence it
  produces will again point somewhere else.
