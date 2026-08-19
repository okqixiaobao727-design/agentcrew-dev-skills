# Accounts

An **account** is a named Claude Code login. A ticket may name one, and every Claude process that
ticket owns — its implementer child, its reviewer, and the handler that repairs its merge conflict
— then runs under that login and spends that subscription. That is the whole of the feature: an
operator with two subscriptions decides, ticket by ticket, which one pays.

A ticket naming no account runs on the coordinator's own login, exactly as every ticket did before
this key existed. An operator with one subscription creates no file, names no account, and reads
nothing further here.

Claude Code scopes a login to `CLAUDE_CONFIG_DIR`: each profile directory carries its own
credential slot, and two profiles answer with disjoint agents lists and keep disjoint transcript
roots. So the profile directory *is* the account at exec time, and the whole of routing an account
is deciding which directory a ticket's processes are launched with.

The repository carries account **names**. The machine carries the map from a name to a directory.
Nothing committed here holds a profile path.

## Naming an account on a ticket

`/route` writes the `Account:` line into a ticket's `## Routing` section from what you name at the
approval checkpoint. It is the one routing value recorded rather than concluded — no classification
proposes it, and no config cell carries it, because which subscription pays is a fact about your
wallet and not about the kind of work:

```markdown
## Routing

Workflow: direct
Executor: claude
Model: claude-opus-5
Effort: medium
Account: side
Reasons: …
```

The line is optional, and it appears only on the tickets you asked for it on.

From there the name travels the road every routing key travels. The ticket's section is advisory;
the driver resolves the name to a profile directory as it builds the wave table, and from the
validated table onward every row carries a concrete `account`
([ADR-0014](adr/0014-optional-routing-keys-are-resolved-at-the-wave-table-boundary.md)). A resumed
run reads its assignment out of the table it already has, so a restart cannot split one ticket's
spend across two subscriptions.

## The registry: where a name becomes a directory

| | |
| --- | --- |
| Default location | `~/.claude/agentcrew/accounts.toml` |
| Format | TOML, one `[accounts]` table of `name = "<profile directory>"` entries |
| Override | `AGENTCREW_ACCOUNT_REGISTRY`, an absolute path to the registry file |
| Entry point | [`skills/crew/assets/accounts.py`](../skills/crew/assets/accounts.py) — nothing else reads the registry |

```toml
[accounts]
work = "/Users/you/.claude"
side = "/Users/you/.claude-side"
```

The default location is read from the home directory itself and deliberately **not** through
`CLAUDE_CONFIG_DIR`, which every other machine-level file of this project resolves through. A
registry whose whole job is to record the mapping *between* accounts cannot be stored per account:
a second copy under a non-default profile would let the coordinator's own login select which map is
in force, and children would launch on the wrong account silently. The reasoning is
[ADR-0013](adr/0013-the-account-registry-is-not-profile-scoped.md).

The override moves the one registry; it never splits it into several. It is an environment variable
rather than a flag because a run is many processes — the driver, the dispatch renderer, the
dashboard, and every one a resume starts later — and all of them must reach the same file, so
export it in the environment the run is started from. A relative value is refused rather than
resolved: a run's processes start from many working directories, and a relative path names a
different file in each.

## Declaring the names a repo expects

`agentcrew.toml` at your repo root may declare which account names this repository's tickets are
allowed to name. Names only, never a path:

```toml
[accounts]
names = ["work", "side"]
```

The declaration is optional and purely diagnostic. Declare none and any registered name is
accepted; declare them and a ticket naming something else is stopped in preflight in the config's
own terms, rather than discovered as a machine missing a profile.

## What the run does on each path

| The case | What the run does |
| --- | --- |
| No ticket names an account | Every ticket resolves to the coordinator's own configuration home, which the run section records. The registry is never opened. |
| No registry file, and no ticket names an account | Runs as it always did. The file is needed only once a ticket asks for an account, so there is nothing to create. |
| No registry file, and a ticket names an account | Preflight stops the run, naming the account and the registry path it searched. |
| A name this repo's config does not declare | Preflight stops the run, naming the config file and the names it expects. |
| A name the registry does not hold | Preflight stops the run, naming the account and the registry to register it in. It never falls back to the coordinator's account — a silent fallback is the defect this feature removes. |
| A registered name whose profile directory is not there | Preflight stops the run, naming the directory and the registry entry that points at it. |
| A wave split across two accounts | Each child is launched in a tmux window whose own environment carries its account, so a `claude` you type into that window by hand stays on it too. Verification, the dashboard's liveness reads and the cost rollup each read the account that child's row names, and the machine log's `launch` line records it. The coordinator need not be logged into any account it dispatches into. |
| A `codex` ticket | Unaffected. It launches on its own vendor's credentials and its launch event records no account. |

## No login check is performed

Registration and directory existence are the whole of the check. Whether that profile is *logged
in* is deliberately not asked, at any point. The CLI cannot answer it: an unauthenticated profile
answers `agents --json` with an empty list and exit code 0, which is what an authenticated profile
with nothing running answers too.

So a ticket routed to an account nobody logged in launches, and then fails at post-launch
verification after the timeout (90 s by default), on a message that names the account:

```
no entry for this child in the live agents list of the account /Users/you/.claude-side
after 90s — that account may not be logged in
```

That message is the one surface an unauthenticated profile appears on. Log that profile in through
the ordinary login flow with `CLAUDE_CONFIG_DIR` set to the directory the message names, and
dispatch the ticket again.
