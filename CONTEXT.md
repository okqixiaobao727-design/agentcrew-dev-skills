# AgentCrew

A Claude Code plugin that routes spec tickets (`/route`) and runs them unattended
(`/crew`) as parallel child agent sessions, coordinated by one expensive-model session
whose only job is judgment.

## Language

One document owns the project vocabulary: [`docs/glossary.md`](docs/glossary.md). Every term, and
the synonyms each one displaces, is defined there.
