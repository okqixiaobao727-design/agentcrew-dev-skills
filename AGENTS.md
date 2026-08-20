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
A change contained to one asset needs no local gate: CI runs the full gate on every push and pull
request to `main`.

Run the validator every time — it costs a second, and its residue lint reads your gitignored
`.agentcrew-local-identifiers`, which CI has no copy of.

Report what you ran, verbatim, beside the result. A skipped gate reported as a pass is worse than
a red one (ADR-0016).

<!-- code-review-graph MCP tools -->
## MCP Tools: code-review-graph

**IMPORTANT: This project has a knowledge graph. ALWAYS use the
code-review-graph MCP tools BEFORE using Grep/Glob/Read to explore
the codebase.** The graph is faster, cheaper (fewer tokens), and gives
you structural context (callers, dependents, test coverage) that file
scanning cannot.

### When to use graph tools FIRST

- **Exploring code**: `semantic_search_nodes_tool` or `query_graph_tool` instead of Grep
- **Understanding impact**: `get_impact_radius_tool` instead of manually tracing imports
- **Code review**: `detect_changes_tool` + `get_review_context_tool` instead of reading entire files
- **Finding relationships**: `query_graph_tool` with callers_of/callees_of/imports_of/tests_for
- **Architecture questions**: `get_architecture_overview_tool` + `list_communities_tool`

Fall back to Grep/Glob/Read **only** when the graph doesn't cover what you need.

### Key Tools

| Tool | Use when |
| ------ | ---------- |
| `detect_changes_tool` | Reviewing code changes — gives risk-scored analysis |
| `get_review_context_tool` | Need source snippets for review — token-efficient |
| `get_impact_radius_tool` | Understanding blast radius of a change |
| `get_affected_flows_tool` | Finding which execution paths are impacted |
| `query_graph_tool` | Tracing callers, callees, imports, tests, dependencies |
| `semantic_search_nodes_tool` | Finding functions/classes by name or keyword |
| `get_architecture_overview_tool` | Understanding high-level codebase structure |
| `refactor_tool` | Planning renames, finding dead code |

### Workflow

1. The graph auto-updates on file changes (via hooks).
2. Use `detect_changes_tool` for code review.
3. Use `get_affected_flows_tool` to understand impact.
4. Use `query_graph_tool` pattern="tests_for" to check coverage.
