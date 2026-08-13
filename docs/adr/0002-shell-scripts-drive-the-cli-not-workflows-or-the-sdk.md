---
status: accepted
---

# Orchestration mechanics run as shell scripts driving the CLI, not dynamic workflows or the Agent SDK

The zero-token layer of the crew run (rendering, launching, watching, merging, logging)
is plain shell scripts driving `claude` CLI sessions in tmux.

## Considered Options

- **Dynamic workflows** — the harness's real orchestration-as-code primitive — forbid
  mid-run user input ("for sign-off between stages, run each stage as its own workflow").
  The coordinator answering child escalations mid-wave is the whole point of `/crew`, so
  workflows could only host escalation-free stages. Rejected.
- **The Agent SDK** is API-key-billed only; Anthropic does not permit claude.ai
  subscription auth for SDK-built agents. This plugin targets subscription users.
  Rejected.
- **Shell scripts + tmux + CLI** cost zero model tokens, keep subscription auth, and keep
  the interactive child windows the escalation loop needs. Chosen.
