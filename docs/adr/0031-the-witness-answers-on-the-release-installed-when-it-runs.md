---
status: accepted
---

# The Witness answers on the release installed when it runs

[ADR-0010](0010-the-driver-runs-the-run-the-coordinator-rules.md) puts the fact-check in front of
the ruling, and #194 made the coordinator run it itself: every escalation ends on one fixed Witness
line, rendered into the child's first turn by dispatch with the run directory and the ticket
already filled in, copied by the child into its `CREW ASK` and copied again by the coordinator that
rules on it.

That line names a script inside the plugin tree, and a plugin is installed one directory per
version. The line is composed once, at launch, from the release the Driver started on, and it is
re-pasted unchanged for the rest of the run. In run crewtask/21 the operator upgraded the plugin
mid-run — to a release whose whole changelog was Witness fixes — and ran `/reload-plugins`. All
later ASK still carried the old release's path, and `references/triage.md` says to copy that line
out of the message and run it, so every later fact-check would have gone on running the release the
run began with. The coordinator noticed only because the operator said so in chat, and substituted
the path by hand for the remaining seven checks (#204).

Nothing recorded that it had. The `witness` event carried the operation, the executor, the model,
the outcome, the coverage and the timeline, and no release, so a run that had in fact spanned two
of them read afterwards as though it had spanned one.

## Considered Options

- **A release installed since supersedes the one invoked. Chosen.** The line stays as it is; the
  script it names resolves, at the moment it is run, which release of this plugin is installed
  now, and runs that one in its own place. The succession happens before the arguments are parsed,
  so a release that changed them is the release that reads them, and it happens once. This is not
  the coordinator's hand-over of a run, nor the Driver's hand-over of an escalation: nothing about
  the run changes hands, and the same operation continues on newer code.
- **Pin a run to the release it was launched on, and say so in the coordinator documents.**
  Rejected by the maintainer. It is a defensible position — it is the one
  [ADR-0011](0011-the-pin-names-its-renderer-the-wrapper-is-a-permanent-stub.md) takes for the
  statusline, where the release that dispatched a run is the release that draws it — but it makes
  an operator's upgrade silently inapplicable to the run in front of them, which is what the
  reported incident was about.
- **A version-independent copy of the Witness in the run directory**, the way the Machine-log
  writer, the coordinator control script and the bounded-read runtime are copied there (#37).
  Rejected: those copies are refreshed by the process performing the install, which is the Driver,
  which itself runs from the release the run was launched on. A copy would have moved the pin
  rather than removed it, and would not have served the driver-less Witness line, which has no run
  directory to keep one in.
- **Resolve the release by searching the plugin cache** — a glob over version directories, or a
  marker file this project maintains. Rejected for the reason ADR-0011 gives: a search that has to
  keep step with how the harness lays plugins out. What is read instead is the harness's own
  registry of what is installed, which is a record rather than a search.

## Decision

**The Witness decides which release answers, at the moment it is run.** It finds the plugin tree it
is running from by the manifest that names it, reads the registry of installed plugins under the
configuration home this process belongs to, and runs the release named there in its own place.

**A candidate is a release of this plugin, by identity and by position.** A release of one plugin
is a version directory among its siblings, so a registry entry must sit in the same family
directory as the running tree *and* its own manifest must name the plugin the running tree's
manifest names. Position alone would be close enough in practice and is still not identity: it
says where a tree was put, and the manifest says what was put there. A source checkout is in no
family and carries no sibling releases, so it matches nothing either way, and this repository's
own runs and suites are unaffected by whatever is installed on the machine.

**The release running is a candidate like any other**, which is what makes "exactly one candidate"
the whole rule. A registry naming only this release leaves nothing to do. A registry naming a
second — the same plugin installed at two scopes — is one no answer can be read out of, so nothing
is chosen. Excluding the running release before counting would have turned that second case into a
silent move onto whichever other release happened to be listed.

**Silence, never an error.** A registry that is absent, unreadable, or not the shape this reads; a
named release that is not on disk, or whose Witness this process cannot read; two releases of this
plugin named at once — each leaves the invoked Witness running. The fact-check is the thing being
protected: it serves a ruling far better on a slightly old release than it does by failing on a
file this process does not own. The unreadable case is refused where the release is chosen rather
than after: what replaces this process is the interpreter, not the script, so a Witness that
cannot be read would be started and then fail where the coordinator expects a brief.

**Every fact-check records the release it ran under.** The `witness` event carries
`plugin_version`, read after any succession so it names the code that did the checking, and the run
report names the releases a run's checks ran under when it ran any. The field is optional and
absent rather than empty where there is no release to name — a source checkout, or a run from
before the field existed.

**A mixed-version run is accepted, not prevented.** That is the cost of the choice above, and the
record is what makes it visible. Nothing else the run owns follows the installed release: the
Driver, the children and every other script keep the release they started on.

## Consequences

- An operator who upgrades mid-run and reloads gets the new Witness on the next fact-check, with no
  hand-edited path and nothing to re-type. The incident that opened #204 cannot recur silently.
- **It applies to runs launched on a release that carries this decision, and to no earlier one.**
  The line a run launched on an older release names that release's Witness, and that Witness has
  no such decision to make. This is not a limitation of the mechanism but of where it lives: any
  fix to what the line runs takes effect only once the release the line names carries it. A run
  launched on an older release stays as it was, and the operator's remedy there is the one they
  already had — finish the run, or restart it on the installed release.
- A run's records can now name two releases. Anything reading `witness` events as a single-release
  fact is wrong, and the report says so plainly rather than leaving it to be inferred.
- The Driver of a long run and the Witness answering its escalations may be different releases.
  This is bounded by what the Witness is: a read-only session that returns pointer-backed facts and
  no ruling, over a run directory whose formats are the Driver's. A Witness release that needed a
  Driver change to work is a release that has to land with it.
- This project now reads one file the harness owns. It is read defensively — every failure is
  silence — and it is read at one place, so a layout change has one site to follow. This is the
  coupling ADR-0011 declined to take on for the statusline wrapper; it is taken on here because the
  alternative is an operator's upgrade that does not apply, and because a fact-check failing open
  costs a stale brief where a blank statusline costs the operator their whole readout.
- The driver-less Witness line, which a developer runs itself in a flow with no Run, is served by
  the same succession without naming anything of its own.
