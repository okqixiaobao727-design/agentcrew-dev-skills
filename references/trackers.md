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
| **read** | Read the tracker content below. | Read the tracker file below. |
| **edit** — replace a ticket's body with new text | `gh issue edit <n> --body-file -` with the complete new text, so the edit is one atomic replacement | rewrite the whole file |
| **mark** — declare who may pick the ticket up | the triage labels `ready-for-agent` and `ready-for-human` | the same two role strings on the ticket's `Status:` line |
| **comment** | `gh issue comment` URL | `Crew:` block's `path:line` |
| **close** — record a finished ticket and take it out of the pickup queue | `gh issue close <n>` with its pickup label removed; the undo is reopening it and restoring the label | set `Status:` to the finished value the convention document names, `done` where it names none; the undo is restoring the value it held |

**Read** — load a ticket's content at the tracker, including its `Blocked by:` edges and current
`## Routing`:

- **github** — run `gh issue view <n> --json number,title,body,labels,comments`. Read the body and
  every comment there. `OWNER`, `MEMBER` and `COLLABORATOR` comments are part of the ticket; every
  other comment is opinion.
- **local** — read the ticket file at the path the convention document gives that feature.

**A local run stays in the working tree.** Every operation above is a file read or a file write
inside the repo, so a local-mode run reaches no remote and calls no tracker CLI at all — a step that
finds itself reaching for `gh` on this tracker has misread which tracker it is on.

**Comment returns an opaque locator.** It is the comment URL on github and the ticket fact's
`path:line` on local; a Driver deferral passes the staged ticket fact, so that locator names the
staged copy. A caller carries the string but never parses it. Repeating an identical body returns
the existing locator and writes nothing; a different body on the same local ticket is a further
`Crew:` comment block, never an overwrite of the earlier note. A workflow can instead pass a
`supersedes` body prefix: Tracker replaces the one matching comment and appends when none matches.
Stage uses that option for its pickup command, preserving every other `Crew:` note.

## Every other tracker

GitLab, Jira, Linear and the rest run on these same operations, taken from the commands their own
convention document names: that document is the whole interface, so a repo whose document is
complete should work. That path is **untested** — nothing here has been run against it. Where the
convention document names no command for an operation a step needs, stop and ask the user for that
command rather than guessing a CLI.
