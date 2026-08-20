# AGENTS.md

## Validating your work

This repository has one test entry point, `scripts/test.py`, and two situations to use it in. Run
the command that matches your situation; do not substitute another.

**While you are working — run the suite of the asset you changed, and only that one:**

```sh
python3 scripts/test.py --asset driver
```

The name is the asset's directory name: `dispatch`, `driver`, `launch`, `monitor`, `review`,
`stage`. Anything outside `skills/*/assets/` — `scripts/`, hooks, the merge driver — is the `root`
suite: `python3 scripts/test.py --asset root`. Changed two assets? Run the two focused commands.

**At the end of the ticket, exactly once — run the full gate:**

```sh
python3 scripts/test.py
python3 scripts/validate_plugin_tree.py
```

The full run takes around fifteen minutes. Run it once, when the work is finished, and not again
after each edit; that is the whole reason the focused run exists. Never run
`python3 -m unittest discover` — it is no longer an interface, and it costs the full fifteen
minutes with none of the per-suite timing.

Report what you ran, verbatim, alongside the result. If you skipped the full gate, say so; a
skipped gate reported as a pass is worse than a red one (ADR-0016).

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
