# Trackers

`docs/agents/issue-tracker.md` in the target repo names the tracker a run works against and holds
the commands for it — that document is the source of truth, and this file names the operations both
skills call on it, so a step can ask for an operation and stay out of the tracker's syntax.

Two trackers are supported, meaning exercised end to end:

- **github** — a ticket is an issue, reached through the `gh` CLI.
- **local** — a ticket is a markdown file in the repo, and its `Status:` line carries what a label
  carries on github.

Settle which one the repo uses at the first read of the convention document, and state it to the
user with the rest of what that step reports.

## The operations

| Operation | github | local |
| --- | --- | --- |
| **read** — a ticket's body, its `Blocked by:` edges, and its current `## Routing` | `gh issue view <n> --comments`, and the convention document's list command for the whole feature | read the ticket file at the path the convention document gives that feature |
| **edit** — replace a ticket's body with new text | `gh issue edit <n> --body-file -` with the complete new text, so the edit is one atomic replacement | rewrite the whole file |
| **mark** — declare who may pick the ticket up | the triage labels `ready-for-agent` and `ready-for-human` | the same two role strings on the ticket's `Status:` line |
| **close** — record a finished ticket and take it out of the pickup queue | `gh issue close <n>` with its pickup label removed; the undo is reopening it and restoring the label | set `Status:` to the finished value the convention document names, `done` where it names none; the undo is restoring the value it held |

**A local run stays in the working tree.** Every operation above is a file read or a file write
inside the repo, so a local-mode run reaches no remote and calls no tracker CLI at all — a step that
finds itself reaching for `gh` on this tracker has misread which tracker it is on.

## Every other tracker

GitLab, Jira, Linear and the rest run on these same operations, taken from the commands their own
convention document names: that document is the whole interface, so a repo whose document is
complete should work. That path is **untested** — nothing here has been run against it. Where the
convention document names no command for an operation a step needs, stop and ask the user for that
command rather than guessing a CLI.
