# AGENTS.md

## Validating your work

`scripts/test.py` is the only test command; a name it doesn't know prints the list of suites.

**While you work — the suite that tests what you changed, plus the validator:**

```sh
python3 scripts/test.py --asset driver
python3 scripts/validate_plugin_tree.py
```

A file inside `skills/*/assets/<asset>/` is tested by that asset's suite. Everything else is
tested by `root`: `scripts/`, `config/`, hooks, and the **spine** — the files under
`skills/crew/assets/` that sit outside any asset (`accounts.py`, `advance.py`, `machine_log.py`,
`merge_driver.py`, `codex/`).

**Before you hand the work back — the full gate, if you touched the spine:**

```sh
python3 scripts/test.py   # every suite; give it a fifteen-minute timeout
```

Six of the seven suites import the spine, so a focused run cannot see what a spine change broke.
A change contained to one asset needs no local full gate when its commit is pushed before another
run is cut: CI runs the full gate on every push and pull request to `main`. For an unpushed base,
this repo's `[preflight] gate` runs that same full gate after the Driver checks out and
fast-forwards the base and immediately before it cuts an integration branch.

Run the validator every time — it costs a second, and its residue lint reads your gitignored
`.agentcrew-local-identifiers`, which CI has no copy of.

Report what you ran, verbatim, beside the result. A skipped gate reported as a pass is worse than
a red one (ADR-0016).
