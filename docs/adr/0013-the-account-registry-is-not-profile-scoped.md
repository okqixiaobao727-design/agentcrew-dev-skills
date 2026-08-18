---
status: proposed — decided while specifying #95, accepted when #95 lands
---

# The account registry is a machine-level file, deliberately not resolved through `CLAUDE_CONFIG_DIR`

A ticket may name the **account** it runs on (#95). The name has to resolve to a Claude Code
profile directory somewhere, and every candidate location in this project already has a
convention attached to it — which is the problem, because the obvious convention is the wrong one
here.

Claude Code scopes login state to `CLAUDE_CONFIG_DIR`: each profile gets its own credential-store
slot, so which account a process runs on is decided entirely by that one variable at exec time.
Measured on the reporter's machine (Claude Code 2.1.234, macOS 26.5.2): two profiles return
**disjoint** live agents lists, and a child dispatched by a coordinator on the second profile
appears only in the *default* profile's list. That is the defect #95 removes.

This project already has a house convention for machine-level state: the pin registry and the
dashboard surface preference both live at `$CLAUDE_CONFIG_DIR/agentcrew/<name>`, falling back to
`~/.claude/agentcrew/<name>`
([ADR-0008](0008-the-pinned-dashboard-lives-in-claude-codes-statusline.md),
[ADR-0012](0012-the-statusline-tick-reads-the-sessions-files.md)). Following it here would be one
line of reuse and a category error: a registry whose entire job is to record the mapping *between*
accounts cannot itself be stored per account.

## Considered Options

- **Profile directories in the crew config file.** The config file lives at the target repo's root
  and is committed. A profile directory is a path on one machine belonging to one person, so
  committing it means a second machine, or any other clone, carries configuration that is wrong
  and still runnable — the worst combination, because the wrongness shows up as spend on the wrong
  account rather than as a crash. Rejected.
- **Follow the house convention: `$CLAUDE_CONFIG_DIR/agentcrew/accounts.toml`, falling back to
  `~/.claude/agentcrew/accounts.toml`.** This works, right up until someone does the thing the
  convention invites. Create the file only at the fallback location and every coordinator reads the
  same map; create a second copy under a non-default profile — which the convention says you may —
  and the coordinator's own account now selects which mapping is in force. Two copies that disagree
  produce exactly one symptom: children launching on the wrong account, silently. Rejected for
  having the defect under repair as its failure mode.
- **A fixed machine-level location, plus an explicit override. Chosen.** The registry resolves to
  one path per machine regardless of which account the coordinator is on, so no coordinator can
  select its own mapping. The override is explicit and single-valued, which is a different thing
  from being profile-scoped: it moves the one registry, it never splits it into several.

## Decision

The account registry lives at a fixed default path under the operator's Claude configuration home
and is **not** resolved through `CLAUDE_CONFIG_DIR`. It is the one file in this project that
intentionally departs from the machine-level convention above; the departure is the point, not an
oversight.

Its location is overridable by an explicit switch, in the shape the pin registry already uses — a
fixed default plus an override — which is also the test seam, since no test may write into a real
home directory.

Concretely, as #97 fixes it and the shipped config file's own header states it:

- **Default location** — `~/.claude/agentcrew/accounts.toml`, read from the home directory itself
  and never through `CLAUDE_CONFIG_DIR`.
- **Format** — TOML, one `[accounts]` table of `name = "<profile directory>"` entries.
- **Override** — `AGENTCREW_ACCOUNT_REGISTRY`, an absolute path to the registry file; a relative
  one is refused rather than resolved.
- **Entry point** — `profile_directory(name)` in `skills/crew/assets/accounts.py`. Nothing else
  reads the registry.

The override is an environment variable rather than a command-line flag because a run is many
processes — the driver, the dispatch renderer, the dashboard, and every one a resume starts later
— and each of them has to reach the same registry. A flag would have to be threaded through each
launch, and a launch that forgot it would read a different registry, which is the shadowing
failure this ADR exists to prevent. The variable is single-valued: it moves the one registry, and
unlike `CLAUDE_CONFIG_DIR` it does not split it per account.

Two rules ride with it:

1. **The repository carries names, never paths.** Tickets name an account; the crew config file may
   declare which account names the repo expects. Neither ever holds a profile directory.
2. **An unregistered name is a hard failure before anything launches.** Falling back to the
   coordinator's account for a name the registry does not hold is forbidden. A silent fallback is
   the defect being repaired, and a correct silent fallback is indistinguishable from an incorrect
   one to every later reader.

## Consequences

- Moving a profile directory is one edit on one machine. No ticket and no committed file changes.
- A clone of this repo on a machine with no registry file runs single-account waves exactly as
  before. The file is required only once a ticket names an account, so the operator who never uses
  a second subscription pays nothing for this feature.
- A typo'd account name stops the run with a message naming the account and the registry searched,
  rather than quietly spending on the coordinator's subscription.
- A future reader who notices this file breaking the `$CLAUDE_CONFIG_DIR` pattern has this record;
  without it the obvious "fix" is to make the registry profile-scoped, which reintroduces #95 in a
  form that is much harder to diagnose than the original.
- The project now has two shapes of machine-level state — profile-scoped and machine-fixed — and
  new state must choose. The test is whether the state describes something *within* one account
  (the pin registry, the surface preference: yes) or *across* accounts (this registry: no).
