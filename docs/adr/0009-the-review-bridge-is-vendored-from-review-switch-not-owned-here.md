---
status: accepted
---

# The review bridge is vendored from Review-Switch, not owned here

`tui_review_bridge.py` had been hand-copied into four trees and had drifted in every one of them
(#16). Three of the four lagged behind #13's review recovery and #14's MCP readiness gate, so the
same defect the fixes closed was still live wherever a copy was the one actually installed. A fix
landing in one copy reached none of the others, and nothing in any tree said which copy was the one
to fix.

Issue #16 settled that question in this repo's favour: agentcrew would own the bridge and
Review-Switch would vendor it. That direction was correct only while Review-Switch was a folder
inside a private brainstorming repo. Spec-142 published Review-Switch as its own repo, and the
bridge is the capability that repo exists to ship — a review lane is the whole of what it is. An
owner that ships the file as its product and a consumer that ships it as one asset among many are
not symmetric, so the direction #16 recorded was settled the wrong way round and is superseded by
Review-Switch's ADR-0001.

What that leaves this repo needing is not ownership but a copy whose provenance cannot go quiet.
The failure #16 describes is not that copies exist; it is that a copy could drift with nothing
noticing and nothing naming what it drifted from.

## Considered Options

- **Symlink the installed upstream file.** Removes the copy outright, but the link's absolute path
  is committed and is correct only on the machine that wrote it — the same class of defect
  ADR-0007 exists to keep out, and the residue lint already fails a published tree for it.
  Rejected, as it was in #16.
- **Depend on Review-Switch at install time.** Makes the bridge one artifact again, but it couples
  two independently published plugins for a user who installed only one of them. Rejected.
- **Vendor with a provenance comment and no check.** This is the arrangement that already existed
  informally, and it is exactly how the newest version of the file came to live in the wrong repo.
  A note nothing enforces is a note that stops being true silently. Rejected.
- **Vendor a copy pinned to an upstream commit, refreshed by a script, checked by CI.** The copy
  stays, so the plugin installs and runs alone; the pin names what it is a copy of; the check makes
  a drifted copy a red build rather than a discovery. Chosen.

## Decision

Review-Switch is the source of truth for `tui_review_bridge.py`. This repo keeps a **Vendored
Copy** at `skills/crew/assets/review/scripts/tui_review_bridge.py`, and three things hold it
honest:

1. **A header on the file itself**, naming the upstream repo, the pinned commit, and the sync
   script — so the answer to "may I edit this?" is in the file being edited, not in a doc the
   editor would have to already know to look for.
2. **`scripts/sync-bridge.sh`**, which holds the pin as its one upstream commit, fetches that
   version, and restores the header beneath the upstream shebang. Upgrading is editing the pin line
   and running the script; there is no other supported way to change the file.
3. **A CI step** that re-runs the sync and `git diff --exit-code`s the result. A copy that no
   longer matches its pin fails the build.

The pin is a fixed commit, never a branch. An upstream commit cannot reach this repo without
someone changing the pin, so upstream can never break this repo's CI on its own schedule.

Bridge changes belong upstream. A fix discovered here is made in Review-Switch, released there,
and arrives by moving the pin — the route the back-port in spec-142 took to get the newest version
home in the first place.

## Consequences

- `skills/crew/assets/review/scripts/tui_review_bridge.py` is not editable in this repo. A patch
  applied here is reverted by the next sync and fails CI before then.
- CI gains a network dependency: the drift check fetches from the upstream repo, so an upstream
  outage or a rename fails the build for a reason that has nothing to do with the commit under
  test. The step runs on one Python version only, so the cost is one fetch per run.
- Upgrading the bridge is a reviewable one-line diff plus the synced file, which is what makes the
  version this plugin ships a version somebody chose.
- The count in #16 is now two live copies, not four: this vendored one and upstream's own. The
  `spec-orchestrator` fork was deleted with the back-port, and the copy under the superseded
  `brainstorming/agentcrew-dev-skills` checkout is not installed anywhere.
- #16's remaining scope — carrying #13 and #14 into Review-Switch — was satisfied by the
  back-port rather than by this repo pushing fixes outward, which is the shape every future fix
  takes.
