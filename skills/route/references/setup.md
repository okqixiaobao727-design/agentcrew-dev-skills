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

**Done when** `agentcrew.toml` exists at the target repo root, holding either a fresh copy of the
shipped defaults or the content the user chose to keep.

## 3. Validate what you wrote

```bash
python3 <plugin-dir>/scripts/validate_plugin_tree.py --config agentcrew.toml
```

It prints one line per problem and exits non-zero. Fix each line in `agentcrew.toml`, then run it
again. A case the file leaves out is sound, not a problem: the shipped defaults answer every case,
so a project file only has to carry the cells it overrides.

**Done when** the command exits 0.

Then tell the user the config's path and that every cell in it is theirs to edit per repo.
