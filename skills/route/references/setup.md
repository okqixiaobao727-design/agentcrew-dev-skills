# Setup wizard

The first run of AgentCrew in a repo. It settles the repo's issue-tracker convention document, then
writes the crew config — the per-project file holding the model tables `/route` and `/crew` read.

Everything here happens in the **target repo**: the repo whose feature you are routing, not the
plugin's own tree.

Record the plugin root — the installed directory holding this plugin's `config/` and `skills/`, of
which this file's own path is a descendant — as `<plugin-dir>`, absolute. The two files below are
read from there, so the wizard runs from wherever the plugin is installed.

## 1. Settle the tracker convention document

Read `docs/agents/issue-tracker.md` in the target repo. Both skills read it to learn where tickets
are read from and where status is written back, and neither has a fallback.

When it exists, go to step 2.

When it is missing, tracker configuration belongs to `/setup-matt-pocock-skills` — its one canonical
place, which this wizard never duplicates. Tell the user to run that skill in this repo, and wait
for them to say it is done. Then read the path again: it exists, so resume at step 2. Should the
user decline, say plainly that `/route` and `/crew` both stop at their first read of that document
until it exists, and go to step 2 anyway — the config is independent of it.

**Done when** `docs/agents/issue-tracker.md` exists in the target repo, or the user has declined to
create it and knows what that costs.

## 2. Write the crew config

Copy the shipped defaults at `<plugin-dir>/config/agentcrew.default.toml` to `agentcrew.toml` at
the target repo root, comments and all. The comments are the documentation the user edits against,
so a stripped copy is a broken one.

When `agentcrew.toml` is already there, it carries edits, and those edits are the reason to ask
before touching it. Show the user how the file on disk differs from the shipped defaults — each
cell whose executor, model, or effort disagrees, plus the hook command when it is set — and offer
two outcomes: keep the file as it stands, or replace it wholesale with the shipped defaults. Keep
it unless the user explicitly chooses replacement.

**A missing section is not a declined override.** Every cell here is inheritable — a project that
leaves one out gets the shipped default, which is why a trimmed file is sound. Two keys are
**uninheritable**: `[repair] model` and `[tracker] kind`, which the driver reads from the project's
own file and nowhere else. A config written before they existed carries neither, so keeping it as
it stands is what stops that repo's next run in preflight. Name each absent one as a hole rather
than a difference, and append it from the shipped defaults, leaving every edit above it as it was.

**Done when** `agentcrew.toml` exists at the target repo root, holding either a fresh copy of the
shipped defaults or the content the user chose to keep, and carrying both uninheritable keys.

## 3. Settle `[tracker] kind`

The one value the shipped defaults cannot ship correct: it is this repo's own convention, and what
you copied carries a placeholder. A wrong tracker does not fail — the run closes its merged tickets
in the wrong place, quietly.

Read what step 1 settled, `docs/agents/issue-tracker.md`, and name the kind it describes:
`"github"` where a ticket is a GitHub issue reached through the `gh` CLI, `"local"` where a ticket
is a markdown file whose `Status:` line carries what a label carries on github. State the
document's answer and the value it makes, ask the user to confirm, and write what they confirm.
Where the document is missing — declined in step 1 — ask them outright which of the two their
tickets live in, saying that `/crew` closes every merged ticket through their answer.

**Done when** `[tracker] kind` names the tracker the user confirmed.

## 4. Validate what you wrote

```bash
python3 <plugin-dir>/scripts/validate_plugin_tree.py --config agentcrew.toml
```

It prints one line per problem and exits non-zero. Fix each line in `agentcrew.toml`, then run it
again. A *cell* the file leaves out is sound, not a problem: the shipped defaults answer every
case, so a project file only has to carry the cells it overrides. A missing uninheritable key is a
problem, and this command reports it in the words the run's preflight would use.

**Done when** the command exits 0.

Then tell the user the config's path and that every cell in it is theirs to edit per repo.

## 5. Offer the pin

**The pin** draws a run's frame into the operator's own Claude Code statusline, so the run and the
coordinator's prompt are on screen at once —
[`docs/monitor-dashboard.md`](../../../docs/monitor-dashboard.md#the-pin) is where it is described,
and the link to give the user. Wiring it edits the user's Claude Code settings, which is theirs and
not this repo's, so the wizard shows the exact edit and asks before anything is written.

The pin ships with the crew skill, so it is reached through that skill's own directory — the `crew`
slot of `<plugin-dir>`'s skills — recorded here as `<crew-skill-dir>`, absolute. Which copy of the
release runs the command does not matter: what it writes names no release, and every run afterwards
is drawn by whichever release dispatched it.

Print what the install would do. It is a dry run: it writes nothing.

```bash
python3 <crew-skill-dir>/assets/monitor/monitor.py pin-install
```

That output is the whole of what you ask about. Read which case it shows and say so:

- `statusLine.command: (absent) -> …` — there is no statusline today, and the install creates one
  that draws the pin and nothing else.
- `statusLine.command: <their command> -> …` — a statusline is already there. It keeps running and
  its output is printed first; the pin's frame is drawn beneath it.
- `the pin is already installed here; nothing to change` — the wiring is in place. Say so and stop
  here; there is nothing to ask about.
- `rewrite <path>` — a wrapper is there but it is not what this release writes, usually one from
  before the wrapper became release-independent. Treat it as any other edit: show it and ask.

Show the dry run's lines to the user as they were printed, and ask whether to make that change.

On approval, run the same command with `--apply`. Every file it writes is copied aside first, a
second `--apply` changes nothing, and `pin-install --uninstall` puts the statusline back exactly.
Say that a run draws into it once `[dashboard] surface` in `agentcrew.toml` is `"pin"` or `"both"`;
on the default `"window"` the wiring sits idle.

On a decline, write nothing and say plainly that the settings are untouched and that the same
command installs the pin whenever they want it.

**Done when** the user has approved and `--apply` has run, or has declined and knows their
statusline is as it was.
