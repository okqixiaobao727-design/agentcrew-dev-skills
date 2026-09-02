# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed
- A slash-command ruling delivered to a Claude child is recorded in the machine log again.
  `driver.py answer` read the child's composer once, immediately after the `Enter` that submitted
  the instruction, and a Claude composer takes up to about 100ms to clear a slash command — the
  command is resolved and its skill body loaded before the input clears — against about 20ms for
  prose. Both of `type_into_pane`'s reads landed inside that lag, so a ruling the child had
  received and expanded was called a failed delivery and `record_ruling` never ran, leaving the
  run's log and report holding the hand-over placeholder. Each `Enter` is now given
  `COMPOSER_CLEAR_SECONDS`, polled every `COMPOSER_POLL_SECONDS`, to empty the composer before it
  counts as dropped; a composer that genuinely never clears still ends in the same `stuck`
  Unreachable, and a dropped `Enter` is still rescued by the one retry. The driver suite's tmux
  stub models the lag with a `tmux-linger-reads` knob, which is what lets the chain test see the
  defect (#191).

## [0.9.17] - 2026-09-01

### Added
- A live driver accepts a coordinator handover in place (ADR-0025). A run whose coordinator session
  has gone is no longer undirectable: `/crew <run-dir>` typed in a new session resolves one
  immutable coordinator context and hands it to the driver already on the run, which records it,
  re-anchors every live child whose address changed, re-scopes the coordinator hook, re-pins the
  dashboard and retires the old waiter before the new invocation reports success. A driver-side
  authorized-action boundary refuses an answer from any address that is no longer the run's, so a
  stale coordinator reaches neither a child nor the machine log. Coordinator control owns the whole
  start / attach / hand-over decision, and the launcher no longer inspects run metadata to make it
  (#112).

### Fixed
- The shipped default `[hooks.on-child-launch] command = ""` no longer stops preflight with
  `run: launch_hook.command is not a non-empty string`. An empty command declares no hook, as the
  config's own comment says and as the dispatch consumer has always read it, so every
  default-shaped `agentcrew.toml` stages again. The normalized wave table omits the key rather than
  persisting an empty string, and a non-empty command still reaches the plan verbatim with its
  `env` (#138).
- A `CREW COMPLETE` that arrives after the run has settled is no longer lost. Before returning a
  terminal run's old report, `adopt` observes each strictly correlated, unlanded Codex child once
  through the bridge that owns its thread; a message appended by that observation is ordered after
  the old ending and reaches the loop's existing rule table, so a parked ticket resumed by hand
  merges, closes its tracker entry and dispatches its blocked descendants instead of `/crew`
  returning `run-complete` with the run untouched. Identity is re-verified against the recorded
  launch — executor, ticket, thread and machine log — and a mismatch is a driver error rather than
  a silent skip (#171).

## [0.9.16] - 2026-09-01

### Fixed
- The coordinator's bounded-read hook no longer refuses a reader fed by a pipe. `gh issue view N |
  head -40`, `git log --oneline | head -20`, `git status | grep modified` and
  `driver.py answer --help 2>&1 | head -40` were all denied, contradicting SKILL.md's own promise
  that `gh issue view` stays open because the tracker is the ticket. A reader with no file operands
  at the tail of a pipeline reads the previous command's output, not a file; the scanner now
  records whether a lone `|` preceded a command, and `grep`, `rg` and `sed` no longer count the
  pattern or script they spend their first operand on as a file (#179).

### Added
- `ls` of the run's own directory is permitted, joining the existing `ls -d` exemption: a run
  directory holds the run's operational state, not a repository source fact. Operands are compared
  by realpath against the run directory (ADR-0007), and one reaching outside it, or that will not
  resolve, leaves the listing refused (#179).

## [0.9.15] - 2026-09-01

### Added
- `driver.py defer` carries a ruling that places a finding on a later ticket into the tracker the
  later child actually reads. It verifies the target has never launched, writes a
  `Deferred from #<source>:` comment through Tracker, and only then delivers and records
  `<line> — deferred #<target> (comment: <locator>)`. `deferred <ticket reference>` joins the
  placements the triage rule table names, and the identical-comment retry is idempotent (#174).

### Fixed
- Dashboard rows and the run summary now show a Witness fact-check with elapsed seconds while it
  runs. An escalation becomes awaiting-ruling only after its atomic wake snapshot has landed, so
  an in-progress check no longer looks like a missed ASK (#173).
- A Witness fact-check that covers some of an escalation's pointers now delivers what it verified
  instead of dropping the whole brief. Pointers are normalised before the structured check —
  non-file citations such as `#NN`, branch names and ADR ids are held apart from the files — and
  the uncovered ones are named in `witness_reason`, so a partial brief reaches the coordinator
  rather than an empty one (#175).
- The coordinator's bounded-read hook reads a Bash command the way bash does. A single-pass scanner
  replaces the `shlex` tokenizer: it tracks `'…'`, `"…"`, `$'…'`, backticks, `$(…)`, `${…}`,
  `$((…))` and heredoc bodies, matches a read command only in command position, and never inspects
  quoted argument text. `driver.py answer --text` carrying an apostrophe or the word
  `characterization`, and `gh issue comment --body "$(cat <<'EOF' … EOF)"`, are no longer refused,
  while `ls -d /tmp # x⏎cat /etc/passwd` no longer slips through (#176).
- `driver.py answer` accepts the run directory the wake snapshot's `resume` command and SKILL.md
  use. Every entry point resolves `.crew` through one resolver, the older `.crew` path still works,
  and a driver error now names the path it looked in and the form it expects (#177).
- The machine-log tests no longer inherit the ambient `CLAUDE_CODE_SESSION_ID`. An install scopes
  its hook to the session it ran in, so a suite run from inside a Claude Code session pinned a
  session the tests never send and the bounded-read hook silently passed the tool through.

## [0.9.14] - 2026-08-31

### Fixed
- A `/crew` slash-command turn that launched before its first mode-bearing transcript record was
  written no longer fails the coordinator permission-mode read. The transcript stays authoritative,
  but it is reread for a bounded interval before the read fails closed (#172).
- The plugin-tree validator no longer reports every file inside a Run's Crew worktree as shipped
  residue. Git will not descend into a nested checkout to list the files it ignores, so an ignored
  path is now read as a prefix, and the validator's own tree fixture stops copying ignored paths
  into a fresh repository that would ignore nothing.

## [0.9.13] - 2026-08-31

### Added
- Every Run now owns a dedicated Crew worktree under the repository's ignored worktree root: the
  Driver performs the base gate, Integration-branch checkout, Wave merges, adoption, resume and
  clear there, so the invoking checkout stays on its own branch and remains free for unrelated
  work while Crew runs. Run metadata records the worktree as `crew_worktree`, the completion
  snapshot and report name it, and the dead `return_branch` field is gone (#169).

### Changed
- Review lanes may now use the same vendor as their Implementer while remaining fresh independent
  Review-Switch sessions. All four vendor combinations pass route staging, Run-plan validation,
  dispatch rendering and independent cost attribution; shipped defaults remain cross-vendor (#170).

## [0.9.12] - 2026-08-30

### Added
- Witness is now one taskable fact-finding interface with explicit `check` and `ask` operations.
  Checks consume structured findings and render deterministic pointer-backed briefs, while an ask
  resolves the ticket's frozen worktree, account, model and budget from the active Run plan. Both
  operations can read GitHub issue bodies and authoritative comments without gaining write access
  or making recommendations (#163).

## [0.9.11] - 2026-08-29

### Fixed
- A ticket routed to a second Claude account launched a child whose CREW ASK channel was dead in
  both directions: cross-session name resolution reads a session registry under the sender's own
  configuration home, and two accounts are two configuration homes holding disjoint registries.
  The coordinator is now addressed by the socket its registry entry records — carried on the run's
  metadata and rendered wherever the channel is described — so the send side follows the identity
  the receive side already used, on every run rather than through a code path only the rare case
  takes. Message logging and ticket correlation now record that address beside the launch name,
  which also repairs correlation for a coordinator that copied a message's `from` field as the
  tool's own instructions describe (#160).

### Changed
- ADR-0019 is marked deferred: #123 closed without a Tracker module being built, so the ADR's
  decision stands but its status no longer claims code that does not exist.

## [0.9.10] - 2026-08-28

### Fixed
- The Codex bridge end-to-end shell suite now gives `mktemp` a portable template on Linux and
  macOS.
- The test gate now declares the bridge's existing `aiohttp` dependency and reports the install
  command when it is missing. v0.9.8 and v0.9.9 release CI was red after #155 first admitted the
  pre-existing suite because its Linux setup requirements were incomplete; product behavior was
  unaffected, and v0.9.9's bridge change passed its real Codex acceptance (#157).
- A fresh Codex launch now treats the pane launcher's recognized final terminal log line as
  authoritative before transient tmux pane liveness. Opening-skill assertion and TUI-exit details
  therefore survive WebSocket shutdown races; without a terminal ruling, a failed pane-list read
  remains unknown rather than being mislabeled as a vanished TUI (#157).
- Driver tests now serialize their process-per-command tmux stub like one tmux server, preventing
  concurrent polling and answer delivery from losing composer state during the release gate.

## [0.9.9] - 2026-08-28

### Fixed
- v0.9.7 and v0.9.8 could not launch a Codex child in a git working directory: both transferred a
  thread from an app-server client to the TUI through an asynchronously written rollout, and the
  second version failed as soon as it saw Codex's initial empty rollout. The TUI now creates its
  own thread from the positional prompt, an opening skill travels as a path-linked mention after
  an exact `skills/list` assertion, and the outer bridge only discovers the thread by its marker;
  both app-server bootstrap layers and every read of Codex persistence state are removed (#157).

  Verified from the source checkout rather than the globally installed v0.9.8 plugin: three
  direct launches in a temporary git worktree succeeded with codex-cli 0.150.1 on both sides and
  one `<skill>` block per rollout; an isolated Claude `--plugin-dir` session then launched
  GPT-VoiceCoding wave 1 with app-server 0.150.1, TUI 0.149.1, and one `<skill>` block. Repeating
  that `/crew` launch after v0.9.9 is installed globally is a post-release check, not this merge
  gate.

## [0.9.8] - 2026-08-28

### Fixed
- v0.9.7 broke every Codex launch by asking the TUI to resume a thread before its first turn had
  materialised. The pane now posts and confirms the structured first turn before attaching the
  TUI, then reports one atomic bootstrap result so `launch` cannot return success before the TUI
  survives startup; failed launches preserve their app-server log (#155).

## [0.9.7] - 2026-08-28

### Added
- Codex turns whose opening line invokes a skill now carry the matching installed `SKILL.md` as a
  first-class app-server input item. The bridge resolves the path from `codex plugin list`, reports
  a missing skill instead of silently dropping it, and keeps ordinary prompts unchanged (#150).

### Changed
- Child first turns now contain one shared protocol plus a concise workflow brief in the order the
  child works. Review commands are rendered into per-ticket scripts, repeated skill guidance is
  removed, and Codex lanes open with an explicit `$skill` invocation (#147).

### Fixed
- A reaped waiter no longer strands the run. The waiter — the process `/crew` leaves blocking on
  the run's wake snapshot — is a main-session background shell, and Claude Code reaps those under
  OS memory pressure: it died three times in one run while the driver and its children carried on,
  and a `CREW ASK` sat unanswered until a human noticed and re-typed `/crew`. It now records its
  liveness the way the driver does (`<run-dir>/waiter.pid`, one pid per line so a second `/crew`
  attaching cannot hide the waiter already there, written before the driver starts and each name
  taken out again on that waiter's own endings), a driver that writes a wake with no live waiter
  types `/crew <feature-dir>` into the coordinator's own pane once, and the dashboard carries
  `✖ no waiter — /crew <feature-dir> to re-attach` in the driver-dead slot until one attaches
  (#127).
- A failed Codex tmux query no longer becomes a false `vanished` verdict. Failed pane reads use the
  bridge's existing bounded retry and driver-error path, while the dashboard renders an unreadable
  state as annotated `unknown`; only a successful pane-list read can prove absence (#140).
- The bounded-read hook now receives the coordinator's session ID explicitly on fresh launches,
  adoption and re-anchoring, so other sessions in the same repository are not bounded (#148).
- Driver re-ask, nudge, bounce and merge instructions now tell Claude children to record receipts
  through the run-local machine log while Codex children retain send-based instructions (#149).

## [0.9.6] - 2026-08-27

### Changed
- Review instructions now follow each Review-Switch result's `next` and `nextCall` instead of
  copying its flags, timeout, recovery semantics, and report-validity rules. AgentCrew retains
  only its caller-owned run-again budget and its `CREW ASK` mappings, so future Bridge protocol
  changes stay behind the installed command's result boundary (#139).

## [0.9.5] - 2026-08-26

### Fixed
- Reading a ticket asks GitHub for the fields by name. The read operation
  `references/trackers.md` names ran `gh issue view <n> --comments`, which prints the comment
  thread *in place of* the body: on a fresh ticket that is empty output and a success exit
  code, so an agent settling the tracker saw no requirements at all and had nothing to tell it
  so. It now asks for `--json number,title,body,labels,comments`. The guidance came from the
  upstream template and was wrong there in the same way (review-switch#30) (#129).

## [0.9.4] - 2026-08-26

### Removed
- The `code-review-graph` MCP registration and both graph hooks. `.mcp.json` sits at this
  plugin's root, so the registration shipped: every install has carried a `code-review-graph`
  server since 0.2.0, started automatically wherever the plugin was enabled, and on a machine
  without that CLI on `PATH` it could only fail to connect. Upgrading drops it — there is
  nothing to uninstall. On the machine that did have the CLI it merely succeeded at doing
  nothing: the server started in every Claude session, eight at once when measured, and
  nothing consumed it. The review path reaches the graph through the Review-Switch Bridge's
  CLI call alone, and the `AGENTS.md` rule sending agents to the graph before Grep/Glob/Read
  arrived with the installer rather than from a need anyone stated. The `PostToolUse` hook was
  worse than idle — since it began deriving the repository from the git common directory it
  re-parsed nothing from a crew child's worktree, an interpreter start per edit for no result.
  A worktree cannot share or copy the main checkout's graph, because the graph file stores
  absolute paths, and the Bridge now builds one in the checkout under review, so nothing here
  has to keep a graph fresh. `.mcp.json` and `.claude/settings.json` carried these entries and
  nothing else, so both files go; the guard that pinned the server's launch command now guards
  that no graph server is registered at all, under any name. The four skills the same installer
  generated — `debug-issue`, `explore-codebase`, `refactor-safely` and `review-changes` — go
  with it: every step in them is a graph MCP call, so unregistering the server left four skills
  that fail on their first tool call, which is worse than their absence. The CLI remains an
  optional dependency of Review-Switch, documented there, and CONTRIBUTING.md no longer asks a
  contributor here to install it (review-switch ADR-0005, #128).

## [0.9.3] - 2026-08-25

### Changed
- A reviewed ticket's child reviews through the installed Review-Switch command instead of
  through a copy of it vendored here. ADR-0009 kept this repository's own review bridge
  honest by pinning it upstream and checking for drift, which made every upstream fix a
  re-vendoring chore and left two implementations of one thing free to disagree. The child
  now calls `review-bridge` across a process boundary, and AgentCrew keeps only the
  reviewer — vendor, model, effort, account — while the review itself belongs to
  Review-Switch (ADR-0020, superseding ADR-0009). AgentCrew's own lifecycle events and
  per-axis cost accounting survive that boundary through the hooks it renders onto the
  command, so the dashboard and the machine log say what they always said. (#124, #125)

  **This adds an external dependency to an existing install.** Review-Switch is no longer
  carried in the plugin, so a run whose wave table has a review lane — every `tdd` and
  every `refactor` ticket — needs `review-bridge` on the operator's `PATH`. Both READMEs
  now name it under their environment requirements.

### Added
- A fifth preflight check: the installed Review-Switch command, asked only of a run that
  reviews at all. Preflight's other four checks are read-only git questions, so a machine
  without the command had nothing stop it until a child that had already written its work
  reached the review — the one failure in a run that costs the most to arrive at. The check
  is the plan's own question rather than the machine's: a run whose wave table carries no
  review lane calls the command never, and a machine that reviews nowhere is not
  misconfigured for lacking it.

### Fixed
- The driver reconciles stale terminal state instead of deadlocking on it. A receipt
  arriving after a failed settlement deadline left the run stuck, and descendants marked
  `outcome=blocked` were never rescinded once the block cleared. Current ticket state is now
  re-derived from later verified facts, so late completions and relaunched descendants
  recover without rewriting the append-only log, and Claude text submission is confirmed
  before delivery is recorded. (#126)

## [0.9.2] - 2026-08-24

### Changed
- The Driver, monitor, advance and merge driver read the run's current facts from one
  Run projection instead of each deriving them from the Machine log itself. The facts
  always came from one ordered log, but the ordering rules lived in the four readers, so
  the same event sequence had four interpretations and changing one fact could leave
  parts of a single run disagreeing about it. `skills/crew/assets/machine_log.py` now
  owns reading records and deriving those facts; it does not read the Wave table, choose
  the Driver's next action, or translate a fact into the human-facing Ticket state — the
  Driver keeps its rule table and the monitor keeps its presentation-only state mapping
  (ADR-0017). Behaviour-preserving: commands, output, state meanings and execution order
  are unchanged, and `driver.py` shed 238 lines saying the same thing. (#121)
- One Run plan module owns the whole meaning of the Wave table. Construction, validation,
  JSON reading, wave lookup, ticket traversal and dependency interpretation were split
  across the Driver, dispatch, advance, merge driver, monitor and route staging, so a
  change to the plan's shape meant coordinated edits in six modules that only wanted the
  resulting plan. `skills/crew/assets/run_plan.py` now builds a plan from ticket input,
  resolves and validates it, reads and writes the same local JSON file, and hands callers
  immutable run, wave, ticket and dependency facts; no caller parses that JSON or
  reimplements a plan query. The plan says only what should happen — it does not read the
  Machine log, launch children, merge branches or render the monitor (ADR-0018). This is a
  replacement rather than a compatibility layer: the migrated helpers are deleted from
  their former callers, and the plan stays reloadable rather than hidden behind a
  write-once cache. (#122)

## [0.9.1] - 2026-08-21

### Changed
- `scripts/test.py` splits every suite across worker interpreters instead of
  giving each suite one. ADR-0016 made the gate cost its slowest suite rather
  than the sum of them all, and that worked so well it became the next problem:
  the gate measured 417.7s and the driver suite alone measured 417.5s, so the
  driver suite *was* the gate, and no single test in it was slow enough to be
  worth deleting. A work item is now one shard of one suite: the child discovers
  its own suite, sorts by test id and takes every Nth test, so the parent never
  imports a suite's modules and the isolation one worker per item exists for is
  untouched. The shard count and the worker count both derive from
  `os.cpu_count()` — no new flag. `--jobs` keeps its name and its role as the
  escape hatch, but now counts worker processes rather than suites; `--jobs 1`
  still runs the whole inventory one item at a time, and asking for more workers
  than the machine has cores is capped at the core count and said out loud. Reporting is unchanged in
  shape: one line per suite, with the suite's whole test count and its slowest
  shard's time. On a ten-core machine `--asset driver` went from 402s to 71.1s
  and the full gate from 417.7s to 135.2s, every run green. (#119)

## [0.9.0] - 2026-08-21

### Changed
- The driver runs detached from the coordinator's session, in a tmux window of
  its own opened through the same windowing path every child is launched
  through, and `/crew` leaves behind only a waiter. A live run stalled for forty
  minutes showing `waiting` on a ticket whose child had already sent a valid
  receipt: the driver — the only process that reads the machine log, verifies
  receipts and advances waves — had been killed 45 minutes in by Claude Code's
  own background-task termination path, with no user input and no model turn in
  the coordinator's session, and nothing on the dashboard said it was gone. The
  fix is to stop depending on a coordinator-session background task being
  allowed to live. The coordinator's one background task is now a stateless
  waiter that blocks on the run's wake snapshot and prints it; killing it costs
  nothing, because the driver is untouched and re-typing `/crew <run-dir>` puts
  another waiter back. The run directory gains three files for this: `wake.json`
  (the snapshot the waiter reads — the driver's stdout now belongs to its own
  pane), `driver.log` (the driver's output, since no task output file collects
  it any more), and `driver.pid` (below). A run resumed from a release older
  than this one is started by a launcher that expects those files and a driver
  that writes them, so upgrade both ends together — an older driver leaves no
  wake, and the waiter will block until the operator reads the driver's window.
  Do not upgrade across a live run: an older release's driver keeps no pid
  record, so `/crew` reads the run as undriven and starts a second driver beside
  it. Stop the run's driver before upgrading. (Nothing in the run directory can
  tell an older release's live run from one that ended properly, so there is no
  loud failure to raise; a run-format marker would be its own change.)
  One consequence of detachment is deliberately left open and tracked in #112: a
  driver now outlives the coordinator it was started for, and it carries that
  coordinator for its whole life — the pid every child authenticates a ruling
  against. So a run adopted from a session that has since exited keeps answering
  to the old one, and rulings made in the new session are refused by children
  launched under the old. Closing it means re-anchoring the run's children as
  well as its driver, which is its own piece of work. Until then, a run whose
  coordinator session has exited should be cleared and restarted rather than
  adopted.
- The dashboard says when the run's driver is dead. The run directory names its
  driver in `driver.pid` while its loop runs, and every deliberate exit — a wake
  handing judgment to the coordinator, a driver error, the run finishing, an
  operator's Ctrl-C in the driver's own window — takes that name away on the way
  out. A kill cannot, so a record naming a process that no longer runs is a
  killed driver by construction. Both the window dashboard and the statusline
  frame carry a red segment in the slot the awaiting-ruling banner uses,
  `✖ driver dead — /crew <run-dir> to resume`; the two never render together,
  and the render path stays a pure reader that respawns nothing (#87). `/crew`
  reads the same record: a run whose driver is alive is attached to rather than
  started again, so the command stays safe to type at any moment and no run is
  ever driven twice.
- The merge driver resolves a conflict it has already classified as mechanical
  itself, instead of paying a repair session to do it. Every hunk with an empty
  base section is both sides inserting at the same point, which the classifier
  already proves; the driver now keeps both insertions — ours, then theirs, with
  the markers and the empty base removed — stages, commits, and records the merge
  `resolved`, a new word in the machine log's closed vocabulary that says the
  merge cost no model anything where `repaired` says a session ran. With every
  ticket appending a `CHANGELOG.md` entry, this conflict shape recurs on
  essentially every multi-ticket wave, and it was costing a session each time —
  or two, plus an escalation, when the session wandered. The repair rung is
  unchanged and still reached by the mechanical conflict the rewrite refuses: a
  file whose markers do not open and close in order, or whose own text carries a
  line that reads as one, since `=======` is also how prose underlines a heading.
  A semantic conflict still skips that rung for the coordinator, and a merge with
  one semantic file among mechanical ones is semantic entire, as it always was.
- **Validation has one entry point with two intents**: `scripts/test.py`
  (ADR-0016). It owns the suite inventory — `tests/` plus every
  `skills/*/assets/*/tests` directory — so `--asset driver` runs one suite while
  you work and no argument runs the whole gate; each suite's size and wall time
  are reported on stderr. Selection is declared, never inferred: the script does
  not read `git diff` to guess what changed, because an inference that guesses
  wrong skips tests silently. CONTRIBUTING.md, AGENTS.md, CI and
  `scripts/release.py` all invoke it, and the raw `unittest discover`
  incantation is no longer an interface. The full gate is unchanged in what it
  runs, and the two guards it always had still hold: every suite in the
  inventory must contribute tests, and walking the asset suites leaves the root
  walk alone. The suite that grew from 59 tests in 4.6 seconds to 940 in 872 had
  no way to run less; a change to one asset now costs that asset's suite.
- **CI runs the matrix endpoints only**, Python 3.11 and 3.14, instead of all
  four interior versions. These tests exercise `git`, `tmux` stubs and process
  lifecycles, not version-sensitive language surface, so four identical full
  runs bought almost nothing over two — and halving them halves the cost of
  every pull request. A row is one line to restore if an interior-version
  failure ever appears.

### Fixed
- A nudge now belongs to the one silence it was sent into, so a child that went
  quiet, was nudged, got going again and fell quiet later is nudged afresh
  instead of being failed on the old attempt. The driver asked whether a ticket
  had *ever* been nudged, which is not the same question: in a live run, ticket
  104 was nudged, then ran its review, escalated, and was answered by the
  coordinator — and four seconds after that answer put it back to work it was
  settled `failed` as "a nudged child went idle again", with its review still
  running. The predicate now asks whether the nudge is still standing, and
  anything the log shows the ticket moving by after it closes the attempt: the
  child speaking or escalating, its review lane starting or returning, or a
  coordinator ruling that answers the question. A child that is nudged and stays
  silent still settles `failed` on the next idle observation, exactly as before,
  and `RESEND`, `RECHECK` and `MERGE` are untouched. The answer is read off the
  machine log's own ordering, so a driver that adopts a run part-way through
  reaches the same verdict as the one that sent the nudge (#111).
- An account-less ticket now means "the login this run was started on", not "the
  default configuration home, spelled out". The wave table's resolved account is
  a **binding** of two facts — the configuration home the ticket is identified,
  observed and attributed by, and whether that home is selected explicitly or
  inherited — and one shared contract turns a binding into the environment every
  Claude process of the ticket is started in: the implementer child's window, the
  reviewer, the merge-repair session and the wake monitor. Two live failures
  close with it. Reviewers and repair sessions for account-less tickets were
  being told `Not logged in` on a machine whose operator was signed in, because
  `CLAUDE_CONFIG_DIR` set to the default home fails the credential lookup that
  leaving it unset succeeds at. And a Claude wake monitor, the one part of the
  stack the account feature never reached, polled a single live-agents list for a
  whole wave: a child launched on a second account is missing from a list that
  could not contain it, so it was reported `vanished` on the monitor's first poll
  and settled `failed` ten seconds after launch — while it was working, and about
  to escalate. Monitors are now armed one per account binding, each polling under
  the account its children run on, and a lane is re-armed per group so no two
  monitors watch one session. A genuinely exited child still settles `failed`
  under either mode, and a single-account run's monitor, window, reviewer and
  repair session are byte-for-byte what they were: no `CLAUDE_CONFIG_DIR` delta
  anywhere. Wave tables written before this release carry an account with no
  mode, and are read as the explicit selection that release made.
- A receipt that misses the verb grammar is answered instead of being dropped. A
  child that appended prose to its `CREW COMPLETE` line once left a finished
  ticket reading `waiting` for eight and a half minutes behind a live, polling
  driver: the line failed the whole-line pattern, `final_verb` answered the same
  "no verb here" it answers a message that never reached for one, and the driver
  read a receipt attempt as conversation with nothing anywhere saying so. The
  grammar is unchanged — prose about a receipt still cannot settle a ticket —
  but the machine log now tells a near miss from a silence
  (`malformed_receipt`), and the driver answers a near miss on the scripted rung:
  one bounce quoting the offending line and naming the shape of every verb the
  run knows, then `failed` if the next line misses too or the child goes idle
  without resending, with no coordinator turn spent either way. The
  first-turn templates and `references/triage.md` now state the rule
  that decides it — the verb line is the message's whole final line, and prose
  belongs above it — so neither a child following its instructions nor a
  coordinator ruling on one can induce an unparseable receipt (#105, ADR-0015).
- Resumed runs now read a ticket's settlement from one machine-log predicate, so a tracker-close
  `completed` outcome remains landed when advance, halt handling, and report rendering read it.
  Advance decisions account for those completed tickets as already landed, and the driver refuses
  to record the run as `stopped` while unrelated launchable work remains (#104, #108).

## [0.8.3] - 2026-08-19

### Fixed
- The vendored review bridge is back in step with the repository it is vendored
  from, so the check that holds ADR-0009 passes again. The review lane's
  session-cost harvest was written straight into the vendored copy of
  `tui_review_bridge.py` and never reached upstream, so `sync-bridge.sh`
  overwrote those 210 lines on every run and the diff behind it never came back
  empty — red on every push since 0.7.0. The check was right and the change was
  in the wrong repository: it now lives upstream (review-switch#4), tested there
  at the seam that repository tests, since the machine-log writer belongs to
  whichever consumer configures `--machine-log` and what the lane owns is the
  argv it hands over. The pin moves to that merge commit and the vendored copy
  is what the script fetches from it, byte-identical to what was already here.
  Worth knowing for the next release: this check runs only on the 3.11 job and
  fails in four seconds, so it went red for four releases while the three other
  jobs went green beside it.

## [0.8.2] - 2026-08-19

### Added
- A ticket may name the **account** it runs on. `## Routing` takes an optional
  `Account:` line — the one routing value `/route` records rather than
  concludes, written from what the user names at the approval checkpoint — and
  the driver resolves it where it builds the wave table: every row carries a
  concrete `account`, the Claude Code profile directory that ticket's processes
  run under, with a ticket naming none taking the coordinator's own
  configuration home, which the run section now records
  (ADR-0014). A name resolves through a machine-level **account registry** at
  `~/.claude/agentcrew/accounts.toml`, deliberately not resolved through
  `CLAUDE_CONFIG_DIR` and overridable by `AGENTCREW_ACCOUNT_REGISTRY`
  (ADR-0013); `skills/crew/assets/accounts.py` is the one entry point from a
  name to a directory. The repository carries names only: `agentcrew.toml` may
  declare the account names it expects under `[accounts] names`, and never a
  path. A ticket naming an account the registry does not hold — or that the
  config never declared — stops the run in preflight, in a message that says
  which of the two is missing and never falls back to the coordinator's
  account, and a machine with no registry file runs its single-account waves
  with nothing to create (#97).
- A crew child launches on its ticket's account and is verified there. The
  child's tmux window is created with that account's configuration home in its
  own environment — the only injection point that works, since a new window's
  environment otherwise comes from the tmux server as it was when that server
  started and the launch line deliberately bypasses the window's interactive
  shell — so the whole window belongs to the account, including a `claude` the
  operator types into it by hand. Post-launch verification then reads that same
  account for both of its surfaces, the live agents list and the transcript that
  asserts the child's model, so a correctly launched child is never reported as
  missing and the coordinator need not be logged into any account it dispatches
  into. The machine log's `launch` event records the account a Claude child
  launched under, which is what makes a run's spend attributable after the
  fact, and the verification timeout — the one surface an unauthenticated
  profile appears on, since nothing here checks a login — names that account
  rather than the worktree. `account` therefore joins the routing keys the
  renderer requires of every row, absolute like every other path the table
  records, and preflight — which asks the renderer for its verdict on a
  candidate table — resolves the same concrete account onto that candidate's
  rows before handing it over. A project launch hook's own variables cannot
  overrule the account. A `codex` ticket is unaffected: it launches on its own
  vendor's credentials and its launch event records no account (#98).
- A ticket's **reviewer child and merge-repair session run on that ticket's
  account**, so the one-ticket-one-account invariant covers every Claude process
  a ticket owns rather than the implementer alone. The rendered Claude review
  command carries `--account <profile directory>`, which
  `claude_review_bridge.py` puts in the headless reviewer's environment, and the
  merge ladder launches its repair session under the account on the ticket's own
  row. Both read that already-resolved directory from the wave table and neither
  opens the account registry, and neither falls back: the merge ladder escalates
  a conflict whose ticket row carries no account rather than repairing it on
  whichever account it happens to be running under. A `codex` review lane is
  untouched: another vendor, its own credentials (#99).
- Accounts are documented for the operator who has to run one:
  [`docs/accounts.md`](docs/accounts.md) is what an account is, where the
  machine-level registry lives and how to move it, the file format, the names a
  repo may declare in `agentcrew.toml`, and what the run does on each path an
  operator actually hits — a ticket naming none, a name the config never
  declared, a name the registry does not hold, a machine with no registry file,
  a wave split across two accounts, a resume, a `codex` ticket. It states
  plainly that no login check is performed anywhere, and quotes the
  verification-timeout message that is therefore the only surface an
  unauthenticated profile appears on, so that failure is diagnosable from the
  docs rather than from the source. The README's configuration reference gains
  the `[accounts]` section, and ADR-0013 and ADR-0014 — written ahead of the
  code and marked `proposed` until it landed — are now `accepted` (#102).

### Fixed
- The dashboard reads every account the run touches, so a healthy child on
  another account is no longer drawn `vanished` and no longer pushes a
  `vanished` toast. Both of the Claude lane's live sources are per account — the
  per-session files it judges ticket state from and the shared agents-list
  fallback cache behind them — and each ticket is read from the profile
  directory its wave-table row names, with the fallback's CLI spawned under that
  account. The primary path spawns nothing for the extra account, only an
  account whose per-session source cannot be read falls back, and a run naming
  one account behaves exactly as it did, spawn count included. A child that
  really has gone is still `vanished`, and still toasts, on any account (#100).

### Changed
- The cost pass now reads each Claude child's transcripts from the profile directory
  named by its wave-table `account`, so a mixed-account run includes every child's
  figures in its rollup. An unreadable wave table diagnoses Claude rows instead of
  failing the pass or silently undercounting them; Codex remains on its own root
  (#101).

## [0.8.1] - 2026-08-18

### Added
- `driver.py answer` delivers a coordinator's ruling to a Codex child. The
  subcommand rejected every ticket whose executor was not Claude, so a Codex
  child's `CREW ASK` had no deliverable ruling and the only path left was
  hand-typed tmux keys, which rotate no marker and so leave the answered turn
  invisible to the bridge's watch — a ticket answered that way settled parked.
  Text was always deliverable: `deliver()` already carries it over the bridge's
  `send`, the same path the driver's nudges ride, so lifting the guard costs no
  new transport and the bridge records the prompt and rotates the marker as it
  sends. The guard now covers `--key` alone, which stays Claude-only because a
  Codex child runs with approvals off and has no permission prompt to answer;
  its rejection now says so. Ruling therefore stops asking which executor a
  child runs on (ADR-0010), and the triage guidance answers a Codex ask with the
  same subcommand rather than reaching for the bridge by hand (#90).

### Changed
- The repo-scope `code-review-graph` MCP server launches from the installed
  console command instead of `uvx`. The uvx shell stayed resident beside the
  server it started, so every session paid two processes per server rather than
  one — measured machine-wide during a live crew run: 38 shells beside 38
  servers, 364 MB across 24 sessions, half of it pure wrapper, and every crew
  child multiplied it by carrying its own pair. uvx also re-resolved the
  dependency tree on each launch and built a fresh ~440 MB environment whenever
  any transitive dependency released, ~7 GB over three weeks here. `.mcp.json`
  now names the command and lets PATH resolve it, so no machine-specific path is
  committed; the one-time `uv tool install code-review-graph` is documented in
  CONTRIBUTING.md, and a machine without it degrades exactly as before — the
  server fails to connect and agents fall back to Grep/Glob/Read as AGENTS.md
  prescribes. The graph hooks in `.claude/settings.json` already called the
  command this way. A guard test keeps a convenience revert from slipping back
  in (#88).

### Fixed
- Staging withholds a bare `/crew` command when the repository cannot resolve a
  default base branch. `refs/remotes/origin/HEAD` is written by `git clone` and
  never by `git init` plus `git remote add`, so such a repository permanently
  lacks it, and `stage.py`'s self-check skipped the base-branch question
  entirely — conflating *which* branch a run cuts from, genuinely an argument of
  the run's start, with *whether a default can be resolved at all*, a property
  of the repository at staging time. A fully green staging therefore printed a
  command guaranteed to stop in the driver's preflight, the exact defect the
  self-check exists to remove. The check is now `driver.default_base_branch()`
  itself, called through the driver stage.py already imports, so staging and
  preflight cannot drift apart; an explicit `--base-branch` at start time still
  overrides it. Both messages now name the permanent fix,
  `git remote set-head origin -a`, beside the per-run `--base-branch` workaround
  — staging never runs `set-head` itself, because it touches the network and
  mutates repository state, which is the operator's call (#91).
- A Codex child's final word can no longer be lost on the way in, at any of the
  three points it used to be. The receipt grammar was anchored to the start of
  the whole message, so a child that wrote a summary and bundled its
  `CREW COMPLETE` under it was heard as making conversation and its ticket
  stalled its wave — and the same strictness ate asks, logging a bundled
  `CREW ASK` as a plain message the coordinator was never woken for. The verbs
  are now read line by line: a whole line of its own, `CREW COMPLETE` spelled
  with a full 40-character sha, so the same words quoted inside a sentence stay
  prose; where a message carries more than one, the last is the word it sent.
  The driver's rule table and the machine log's own classification share that
  one judgment, so they cannot disagree about what a child said. The Codex
  bridge's `watch` was marker-scoped and edge-triggered, which made every turn
  it had not started itself — a child's answer to a ruling typed into its pane —
  invisible for the life of the session, and dropped any message whose
  busy-to-idle edge no watch happened to straddle. It now evaluates the thread's
  latest terminal turn whatever started it, keeping the marker for finding the
  thread, and logs on the message differing from the one already recorded rather
  than on the edge, which also deduplicates repeated observations. Finally, a
  child owed a handed-over ruling is no longer nudged while it waits: the nudge
  asked a child that had nothing to report to report something, it honestly
  answered `CREW PARKED`, and a ticket whose question had already been answered
  settled parked. A `CREW COMPLETE` that arrives for a parked ticket takes the
  ordinary verify path, and the landable receipt it earns supersedes the parked
  one (#92).
- A run abandoned before its own ending no longer leaves its hook in the repo's
  settings and its landed worktrees on disk for ever. Only a run that reaches
  `finish()` cleans up after itself, so an unresumed judgment-needed pause left a
  `PostToolUse/SendMessage` hook spawning a python interpreter on every message
  sent in that repo — one was found still firing for a run dead since Aug 15 —
  and 36 MB of already-merged worktrees sitting where the epilogue would have
  cleared them. A `start` now sweeps that repo before it does anything else:
  every crew hook in the repo's settings whose run names no live coordinator is
  uninstalled, and that dead run's landed worktrees and branches are cleared
  through the same inventory and epilogue plan the run's own ending uses. The
  liveness judgment is the pid the run recorded, read exactly as the pin
  registry's own sweep reads it, and a hook is recognised by the script it runs
  rather than by the arguments it carries, so a hook this project did not install
  is never touched. Parked and failed children are untouched, here as everywhere
  else — unmerged work is a human's call, and no automatic path deletes it; so
  are the artefacts of a dead run that recorded a different repository, whose git
  is not this driver's to edit even though the settings file is. Every problem
  the sweep meets is a warning on stderr and never a stopped run, and the run
  being started or adopted is never swept (#89).
- A wake monitor no longer outlives a driver that was killed. `disarm` covers
  every ordinary exit, but a `kill -9` — the memory-pressure reap of
  `docs/driver-external-kill.md` among them — left `monitor-wave.sh` looping for
  a reader that would never read it, spawning a ~350 MB `claude agents --json`
  every 20 seconds indefinitely. The driver now arms each monitor with its own
  pid, and the monitor asks `kill -0` about it before every poll, leaving quietly
  the first poll after that pid is gone (#89).

## [0.8.0] - 2026-08-18

### Fixed
- The pinned dashboard's statusline tick no longer spawns a CLI per pane per
  tick. It read its Claude lane by running `claude agents --json`, a complete
  CLI start costing ~350 MB of transient RSS and 0.58 s of CPU per call whatever
  the session count, because ~75% of that footprint is the binary itself being
  mapped. Every pane of the pinned session ticks independently, so a ten-pane run
  at the two-second default spawned ~7 CLIs a second — enough to congest the
  machine until every call exceeded the tick's own two-second timeout, at which
  point the frame drew `unknown` and the whole cost had been paid for nothing.
  The lane now reads the per-session JSON files the CLI maintains under its
  config directory, which measured as the same set of sessions with
  byte-identical `cwd`s and a superset of fields, and costs a few file reads —
  the same order as drawing the frame. `claude agents --json` remains the
  fallback for the day that undocumented directory moves, and its parsed result
  is shared machine-wide through a small cache file written by atomic rename, so
  the spawn rate is bounded by a ten-second freshness window rather than by pane
  count; a fetch that failed is cached too, so a CLI that cannot answer is asked
  once a window rather than once a tick. Nothing is cached on the primary path,
  so nothing there can go stale, and a tick whose sources have both failed draws
  `unknown` in silence exactly as ADR-0008 requires — entering fallback is
  recorded as one `live-source` line in the run's own machine log and never on
  screen. A tick that runs out of budget is kept distinct from one whose
  directory is missing: it draws `unknown` rather than reaching for the slower
  source, and an unreadable directory falls back rather than reading as empty,
  which would have drawn every live child `vanished`. The data-source choice and
  the fallback contract that fences the undocumented dependency are recorded in
  ADR-0012 (#87).
- A run abandoned after a judgment-needed or driver-error pause no longer leaves
  its pin in the registry for ever. Nothing but a normal finish removed one, so
  the burn continued for as long as the coordinator's session stayed open and the
  stale file outlived it. A tick now removes every pin whose recorded coordinator
  pid is dead before matching any of them — safe because a pin is not state, and
  every wave writes it again, so a resumed run re-pins itself on its next
  dispatch. A pin whose coordinator is alive is untouched, which is what keeps the
  awaiting-ruling frame on screen through a pause; the driver's exit paths are not
  changed (#87).

### Added
- `shell` joins the live-state vocabulary as `waiting`, with its own attention
  toast — a child sitting at a shell prompt sends the operator somewhere
  different from one that stopped without finishing, so neither is announced in
  the other's words. The word exists only in the sessions files; the fallback
  command folds it into `busy`, so in fallback mode such a child is drawn
  `running`, and that asymmetry is accepted (#87).
- A `live-source` machine-log event, recording which source a lane's live
  children were read from when a dashboard could not read its first choice
  (#87).

## [0.7.0] - 2026-08-18

### Added
- A `session-cost` line for every review, written by the bridge that ran it, so
  the run's own log can grade the review lane rather than leaving it a blind
  spot. The event gained an optional `lane` field carrying the reviewing vendor
  and its model; a row without one is an implementing child's, as every row
  written until now was. The Claude lane takes its four counters from the
  headless result it already parses, summed over the review's rounds; the Codex
  lane reads the last cumulative `token_count` out of the rollout its own thread
  id names, which covers a resumed round two in one read. Both record the model
  the session resolved to rather than the alias asked for, and both swallow every
  bookkeeping failure — a review is never failed by its accounting, and a harvest
  that came up empty writes the diagnosis in place of the figures. The cost pass
  now skips the sessions those rows name, so a Claude child reviewed on the
  Claude lane is no longer billed for its own review. A review that ran two
  rounds writes a second line whose figures already cover the first, so a
  consumer takes the last line per review session rather than summing them —
  the log is append-only and no bridge knows at the end of round one whether a
  round two is coming (#80).
- A driver `answer` subcommand, so a coordinator's reply to a child's terminal
  permission prompt cannot be delivered without being recorded. The command
  takes the run directory and the ticket, plus `--text` for literal typing and
  `--key` for the narrow set of tmux keys a numbered menu needs (digits, arrows,
  Enter, Shift-Enter), and it reuses the driver's own deliver-then-record pair:
  the pane is typed into first and the coordinator ruling is written to the
  machine log only once delivery succeeded, so the log never claims an answer
  the child did not receive. Multi-line text types line by line with Shift-Enter
  between lines and one Enter at the end, and the recorded ruling carries what
  was actually sent. A ticket with no recorded child, or a Codex child — which
  is reached through its bridge, not through keys — is refused rather than
  answered. The triage reference now mandates the subcommand; its raw
  `tmux send-keys` recipe and the paragraph asking the coordinator to please
  also send the ruling as a message are both gone, so no honour-system logging
  path is left documented (#77).
- A machine-level dashboard surface preference, recorded by `pin-install` at the
  moment the operator opts into the pin and removed again by `pin-uninstall`, so
  the surface is chosen once per machine rather than once per project. It lives
  beside the pin registry under the operator's Claude config, and the monitor's
  existing surface-reading function now resolves three levels in order: the
  project's explicit `[dashboard] surface`, else the machine preference, else the
  shipped `window` default. A preference that is missing, unreadable, or carries
  a value outside the surface vocabulary is treated as absent, so a damaged file
  can never stop a run — and anyone who never ran `pin-install` sees exactly the
  behaviour they had before (#81).

### Changed
- `docs/glossary.md` is now the single home for the project's vocabulary. It
  absorbed every term from `CONTEXT.md`'s Language section into its existing six
  sections, carrying each term's _Avoid_ annotation and merging rather than
  duplicating the terms both documents defined, and `CONTEXT.md`'s section
  shrank to a one-line pointer — so every agent's per-run context load drops
  while the vocabulary stays reachable and a future term can no longer land in
  one document and miss the other. The glossary also gained **stopped**, the
  fifth `advance` decision shipped in v0.5.0, defined against the two terms it
  exists to be distinguished from: `escalated` (a halted wave awaiting a ruling)
  and `interrupted` (the operator stopped the run). Every existing link into the
  glossary is unchanged (#68).

## [0.6.0] - 2026-08-17

### Added
- Two words in the dashboard's state vocabulary, for the two intervals it used to
  draw as frozen rows. A ticket whose merge escalated and whose child was then
  sent the run's rework instruction is `reworking` rather than a bare abnormal
  `waiting` — but only while its lane still sees that child busy, so a child that
  stopped under its instruction is not drawn as though it were working — and a
  ticket that is landable in a wave whose last receipt is in is
  `settling` rather than `landable` while its merge — a sub-second operation —
  happens. A conflict bounced back a second time keeps `waiting`, because that
  one is the coordinator's to answer, and a run that is over keeps `landable`,
  because no merge is coming for it (#76).

### Fixed
- Three dashboard tests failed on a clean checkout. The tests that watch the
  monitor's refresh loop time-boxed it with a fixed half-second nap and then
  counted the frames in the pipe, but one frame of their fixture costs a quarter
  of a second or more — every draw spawns the stub `claude` and the stub `tmux` —
  so "at least two frames" was unreachable and even "exactly one frame" raced the
  first draw on a loaded machine. They wait for the frame they are waiting for
  now, reading it from a file the loop writes as it runs rather than collecting a
  pipe, which only returns once the loop has exited. The monitor's behaviour was
  correct throughout; only the test expectations were wrong (#73).

## [0.5.0] - 2026-08-17

### Added
- A fifth `advance` decision, `stopped`, written by the wave loop when a run ends
  on an escalation the rule table had already settled. `escalated` is the same
  word a halted wave carries, so a run that ended on one was indistinguishable
  from a run waiting on the coordinator; `stopped` is what the surfaces read the
  end of such a run from (#57).
- Two fields on the pin file, `renderer` and `interpreter`: the `monitor.py` and
  the interpreter of the release that wrote the pin, recorded at dispatch, which
  is what the installed statusline wrapper now runs (#56).
- **The staging script** (`skills/route/assets/stage/stage.py`): `/route`'s exit.
  It turns a set of tracker tickets into `crewtask/<n>/` — `spec.md` plus one
  `<number>.md` per ticket at the root — resolves the dependency closure, where
  an edge to a closed ticket outside the set is stripped and an open one is a
  named blocking item, and self-checks the result by importing the driver's own
  parsing, validation and wave-table build rather than restating them. Only a
  fully green self-check prints the `/crew crewtask/<n>` command; any failure
  exits non-zero with each blocking item beside its fix and the command
  withheld. Re-staging the same parent refreshes its directory in place, and
  `crewtask/` is gitignored, so staging never touches the tracked tree (#62).
- The staging script's tracker side: `--parent <n>` expands a parent into its
  open native sub-issues, so routing a triaged piece of work needs nothing but
  one number; `--routing <file.json>` carries the table the user approved and is
  the only thing that authorises a tracker write, putting each ticket's
  `## Routing` section and role label back where the work state lives; and on a
  green self-check the staged `/crew crewtask/<n>` command is commented on the
  parent — or on every ticket of a parentless set — so the pickup point survives
  a `/clear` (#64).
- A **comment** operation in the tracker vocabulary
  (`references/trackers.md`), the write the staged command needs:
  `gh issue comment` under github, and an idempotent `Crew:` line in the ticket
  file under local, rewritten rather than repeated (#64).
- **The launch script** (`skills/crew/assets/launch/launch.py`): `/crew`'s whole
  start-up in one command. Given a run directory it resolves the three values
  the driver's start requires — the coordinator pid from the invoking shell's
  parent process, the session name from the harness's per-pid session registry,
  and the live permission mode from the newest entry of the session's
  transcript, the one on-disk source that reflects a mid-session switch —
  without a single agent exploration turn. These are harness-internal formats,
  so a resolution it cannot make aborts loudly naming the flag that supplies the
  value by hand; it never guesses a default, because a wrong mode launches every
  child of the run outside the mode a message can cross. A run already in flight
  is adopted rather than doubled (#63).

### Changed
- `/route` now has three entrances and one exit. A parent ticket number, an
  explicit ticket list, or a spec whose tickets are not cut yet all end at the
  staging script, and what the user is handed is the command that script printed
  or the blocking items it named — never a `/crew` line composed by hand. The
  `to-tickets+route` path gained the closing step that gets it there: link the
  published tickets to the parent as native sub-issues, then stage. `/crew`'s
  launch step is the single launch-script command now, and the
  environment-exploration instructions it used to carry are gone (#65).

### Fixed
- Preflight's "no tickets" error named neither what it had searched nor what it
  wanted, and it is the error a hand-assembled run directory lands on most. It
  names the directory searched and the `<number>.md` pattern wanted at that
  directory's root now, and warns that a `tickets/` subdirectory there is the
  layout a finished run is archived into, not where a run's input tickets go.
  Both layouts are written down beside the driver's own code, so the next
  hand-assembler — human or agent — has a contract to read (#61).
- Every dashboard surface froze at the first wave that escalated. The monitor
  counted an `advance` decision of `escalated` or `interrupted` as the end of
  the run, so a multi-wave run held a stale frame in its tmux window and blanked
  its pinned statusline from its first escalation on, while the run itself
  carried on. A run ends now on `complete` or on the `stopped` above, in the
  one implementation the driver imports rather than restates — the two used to
  disagree, which is how the defect survived (#57).
- A wave halted awaiting a ruling read as a frozen frame. The summary line now
  carries `⚠ awaiting your ruling` while the log's newest `advance` is
  `escalated` or `interrupted`, and drops it when the next `advance` says the
  wave has carried on (#57).
- A `merge` result of `conflict` or `escalated` left its ticket drawn
  `landable`, indistinguishable from a branch waiting its turn. It is drawn
  `waiting` now, with the `last event:` line naming what blocked it and a clock
  that follows that event rather than the earlier receipt (#57).
- `pin-install` baked the installing release's `monitor.py` path and its
  interpreter into the statusline wrapper, so the first upgrade after an install
  stranded the pin on a release that was no longer there — and, because the
  wrapper ended in `exec`, a dead reference exited non-zero and Claude Code
  blanked the operator's entire statusline, their own readout with it. The
  wrapper is a permanent stub now: it reads the pin registry and runs the
  renderer and interpreter the live pin names, so the release that dispatched a
  run is the release that draws it, with no re-install. It exits 0 on every path
  (#56).
- A statusline that had quietly stopped working looked exactly like a machine
  with no run on it. Where pins are present but none names a renderer this
  machine still has — the paths are gone, the pin is unreadable, or it predates
  those fields — one actionable line is printed instead of nothing: by the
  wrapper when it can reach no renderer at all, and by the renderer it does
  reach, under the new `pin --from-wrapper`, for the registry files only a JSON
  parser can judge. This is the only exception to the silence contract; failures
  inside the renderer stay silent, and `monitor.py pin` on its own is unchanged
  (ADR-0011, #56).
- The pinned dashboard drew in every window. With one run in flight, every
  Claude Code window — every tab, every project, including projects with no run
  at all — showed that run's frame, because pin selection fell back to a lone
  pin whatever session the tick came from. That fallback is gone: a pin draws
  only in the tmux session it records, and a tick matching no pin draws nothing
  however many pins the registry holds. Two accepted consequences: a session
  outside tmux can see no pin at all, and a run's frame is visible only in the
  tab that launched it (#67).

## [0.4.1] - 2026-08-16

The two config keys 0.4.0 made required were settled nowhere the operator
passes through: the wizard left them out of an upgraded config, the validator
the wizard runs called that config sound, and a fresh install got a tracker
nobody chose. Upgrading from 0.4.0 is worth it for the third alone — a run
could close its merged tickets in the wrong place without failing.

### Fixed
- `validate_plugin_tree.py --config` accepted a project `agentcrew.toml` that
  named no `[repair] model` and no `[tracker] kind` — the exact file a run then
  stopped on in preflight. Both keys were checked only when the config had to
  be complete, which the shipped defaults are and a project file is not; but
  neither key is inherited from the shipped defaults, so a project file is the
  only place either can be answered. They are now required of every config this
  validates, and the verdict says why nothing inherits them. The setup wizard
  runs this command, so the wizard was the surface reporting the green (#54).
- The setup wizard read a missing `[repair]` or `[tracker]` as an override the
  project had declined and kept the file as it was, which is how a config
  written before 0.4.0 reached its first run of the driver intact and stopped
  it. A missing section is now named as a hole and appended from the shipped
  defaults, leaving every edit above it untouched (#54).
- `[tracker] kind` was copied into every new project as `"local"`, the shipped
  placeholder, and never reconciled with the `docs/agents/issue-tracker.md` the
  wizard had just settled. A repo whose tickets are GitHub issues would close
  its merged tickets by editing markdown `Status:` lines instead — not a
  failure, a quiet write to the wrong place. Settling it is now a step of the
  wizard: read the convention document, propose the kind it describes, and
  write what the user confirms (#54).

## [0.4.0] - 2026-08-15

The coordinator stops running the run and starts only ruling on it. A scripted
driver now does every mechanical thing the coordinator used to narrate —
preflight, the wave table, dispatch, receipts, settlement, merges, tracker
closes, advancement and the report — as a background task of the coordinator's
own session, and wakes it for exactly three things: a `CREW ASK`, a semantic
merge conflict a child has bounced back twice, and any state the rule table has
no row for. Typing `/crew <feature-dir>` is the whole sign-off, and typing it
again is how an interrupted run resumes.

Upgrading needs one edit: name a `[repair] model` and a `[tracker] kind` in
your `agentcrew.toml`, or the run stops in preflight.

### Added
- **The crew driver** (`skills/crew/assets/driver/driver.py`): one script that
  runs a whole run as a state machine — preflight, the wave table, dispatch,
  receipt verification, settlement, the merge ladder, tracker closes, wave
  advancement and the rendered report — launched as a background task of the
  coordinator's own session, costing it nothing while it works. Typing `/crew
  <feature-dir>` is now the run's whole sign-off: the interactive approval step
  is gone, because across three measured runs it raised no objection while
  costing the most expensive model a table-construction pass and a round-trip.
  Why a script rather than a second resident agent, a crew-state MCP server or
  the Agent SDK, and the three measurements that decided it, is
  [ADR-0010](docs/adr/0010-the-driver-runs-the-run-the-coordinator-rules.md)
  (#41, #47).
- **The rule table**, which settles in the driver everything a written rule
  already decided: a verified receipt settles in silence, an invalid one earns
  exactly one re-ask, an idle child exactly one nudge, a vanished child settles
  failed, parked and failed receipts are recorded by the driver, a settled wave
  advances, a merged ticket is closed in the run's tracker with its exact undo,
  and each wave's monitors are re-armed without anyone being asked. It is a
  transcription of the skill document's own settlement prose, not a new policy.
  The loop keeps no state of its own — every count it acts on is read out of
  the machine log when it is needed, which is what makes resuming the same code
  path as carrying on (#48).
- **A wake surface of exactly three items.** A `CREW ASK` of any kind, a
  semantic merge conflict a child has bounced back a second time, and any state
  the rule table has no row for — a driver crash, a timeout, an unknown status,
  a child at a permission prompt, a monitor that failed. The driver's last line
  before every exit is one JSON wake snapshot, and that object is the whole of
  what a woken coordinator reads: it never opens a run file, so its rulings
  rest only on what a message shows it. Each wake carries the one command that
  puts the loop back, so a driver error is recovered exactly as a ruling is.
  Of 31 in-run wakes across the three audited runs, 26 needed no judgment; a
  clean run now costs the coordinator one turn to launch, one per ASK, and one
  to point at the report (#48, #49).
- **Starting and resuming are one action.** `start` over a feature that already
  carries a run directory adopts that run instead of cutting a second one
  beside it: nothing settled is dispatched again, children keep their worktrees
  and windows, hooks are put back where those worktrees still stand, the
  dashboard is drawn again, and the loop picks the run up from its log. A
  coordinator that restarted re-anchors the run it adopts, so every later
  ticket carries the live identity and every live Claude child is sent the new
  socket. An interruption, a driver crash or a coordinator restart therefore
  costs exactly one re-typed command (#50).
- **`driver.py clear`**, a standalone terminal command that inventories a
  finished run, asks for confirmation, and removes its worktrees, branches,
  windows and hooks. Cleanup is the operator's, so the coordinator never holds
  cleanup context (#51).
- Two `agentcrew.toml` keys the loop needs and no document named, both with no
  default and both validated in preflight: `[repair] model`, the cheap model
  the merge ladder's repair rung runs on, and `[tracker] kind`, `github` or
  `local`, which decides the close operation and the undo a merged ticket
  records. Both are recorded into the run at start, so a mid-run config edit
  cannot retarget a run already under way, and neither decision is ever
  composed in a model turn (#48).
- `machine_log.py uninstall --settings <file>` removes every SendMessage hook
  entry installed for its `--log` and nothing else. The log an entry writes is
  what identifies it as that run's — whichever plugin version registered it —
  so another live run's entry, the guard hooks, and a third party's watcher all
  stay where they are. It is idempotent: a settings file carrying none of ours
  is left byte for byte as it was found. The crew skill calls it when the run
  ends, for the coordinator's settings file and each launched child's, and
  again when the run is cleared, so a finished run leaves no hook behind (#37).

### Changed
- **Upgrading needs one config edit.** A run whose `agentcrew.toml` names no
  `[repair] model` or no `[tracker] kind` stops in preflight rather than
  guessing a model or a CLI. Copy both blocks from
  [`config/agentcrew.default.toml`](config/agentcrew.default.toml) and set them
  for your repo before the first run on this version (#48).
- The crew `SKILL.md` is 60 lines where it was 400: the reversibility contract,
  the pure-oracle boundary, the one start command, the triage pointer and the
  resume line. It is the oracle's resident prefix and nothing else — every
  procedural step it used to carry now lives in the driver. ADR-0003 gains the
  amendment that the driver, not the coordinator, builds and validates the wave
  table, and the glossary gains the driver and the wake surface (#52).
- The interactive wave-table approval round-trip is removed. A routing
  validation failure is now a preflight failure, fixed by re-running `/route`
  and never interpreted by a model; the full problem list reaches the operator
  through a detached `crew-preflight` window that the next start kills by name,
  so a stale notice cannot outlive its fix (#47).

### Removed
- `skills/crew/references/resume.md`. Resuming is no longer a procedure anyone
  reads: it is `start` adopting the unfinished run (#50).

### Fixed
- An advance treated a parked ticket as unsettled, so a parked ticket with no
  descendants held its wave open and the run stopped moving on work nobody was
  waiting for. A parked leaf now settles its wave (#44).
- The monitor dropped its own failures silently and toasted an escalation once
  per run rather than once per occurrence, so a second ASK from the same child
  raised nothing on screen. Errors are recorded as events and escalations toast
  per occurrence (#45).
- The test suite could not pass inside a linked git worktree — which is exactly
  where every crew child runs it — because the plugin-tree fixture borrowed the
  enclosing repository. The fixture now has a git repository of its own (#46).
- Every message a child sent was copied into the machine log twice: once as the
  child's, once more as a coordinator `ruling`. A child's worktree sits inside
  the repository the coordinator runs in, so the coordinator's hook loads in
  the child's session too and both hooks fired on the one send — which put the
  child's words in the log under the coordinator's role and doubled every
  message count a later auditor would take from the file. Each installed entry
  now carries the directory it was installed for and copies only what was sent
  from exactly that directory, so a message is logged once, by the side that
  sent it (#37).
- Installed hook commands named the plugin's own copy of `machine_log.py`,
  whose path carries the plugin version, so upgrading the plugin left every
  registered hook pointing at a file that was no longer there — a failing hook
  on every `SendMessage` of a run in progress. The install now registers the
  run's own copy beside the log, refreshed on each install, which carries no
  version and outlives every upgrade (#37).
- Codex child turn messages and coordinator rulings now enter the machine log through the bridge,
  with the same event classification and timestamp format as Claude child messages (#43).

## [0.3.8] - 2026-08-15

Watching a run stops meaning leaving it. The dashboard can now be drawn into
the coordinator's own Claude Code statusline, so the frame and the
coordinator's prompt are on screen at once and there is nothing to close when
the run ends (#29). Separately, the review bridge stops being a file this repo
owns and becomes a copy pinned to Review-Switch, held to that pin by CI.

The tmux window is unchanged, undeprecated and still the default surface, so
upgrading changes nobody's run.

### Added
- `monitor.py pin` draws the dashboard's frame — the same rows, states,
  annotations and summary line — into Claude Code's `statusLine`. The
  statusline's own `refreshInterval` tick is the refresh loop: each tick
  renders one frame on demand, so there is no background process and no frame
  file. Why the statusline rather than tmux's status lines, popups, floating
  panes or a terminal's own status bar, each measured against the three
  requirements that decided it, is ADR-0008; the measurements are in
  `docs/dashboard-pinning-research.md`. Like every other part of the dashboard
  the frame costs no model token — it is a rendered display region, never added
  to the coordinator's context and never an API call (ADR-0001) (#32).
- The **pin registry**, the run's only trace: a directory of JSON pin files at
  `$CLAUDE_CONFIG_DIR/agentcrew/pins/`, falling back to
  `~/.claude/agentcrew/pins/` and overridable with `--pin-dir`. `pin` takes no
  `--run-dir`; it discovers the live run from the registry, preferring the pin
  whose tmux session matches the caller's own, accepting a lone pin where none
  matches so the single-run case needs no configuration, and drawing nothing
  when several pins exist and none matches, because two crews at once must
  never cross frames. The run writes its pin at dispatch and `monitor.py unpin`
  removes it after the report is written; a run that wrote no pin has nothing
  to remove, and that is a success rather than a complaint (#32, #35).
- `[dashboard] surface` in `agentcrew.toml` chooses where a run draws itself:
  `window` (the default), `pin`, or `both` — which runs each and dedupes toasts
  through the run's one toast state (#35).
- `monitor.py pin-install` wires the pin into the operator's Claude Code
  settings. It edits a file that is the user's and not this repo's, so it is a
  dry run that writes nothing until `--apply`, it names the exact change for
  each of the three cases (no statusline, an existing statusline the pin is
  drawn beneath, already installed), it copies every file it writes aside
  first, a second `--apply` changes nothing, and `--uninstall` puts the
  statusline back exactly (#34).
- The setup wizard offers the pin install as its own step, showing the dry
  run's lines to the user and asking before anything is written (#36).
- The pinned frame is bounded: the renderer caps itself against `LINES`,
  because rows past the bottom of the terminal are lost silently rather than
  scrolled to, and toast updates are bounded with it (#33).
- The pin's contract — the registry, what a pin file carries, how one is
  selected, and every condition under which nothing is drawn — is stated in
  `docs/monitor-dashboard.md`, with ADR-0008 and the glossary entries beside it
  (#31).
- A drift check for the vendored Review-Switch review bridge:
  `tui_review_bridge.py` carries a header naming its upstream repo and pinned
  commit, `scripts/sync-bridge.sh` holds that pin and restores the header after
  the upstream shebang, and CI re-runs the sync and fails on any diff. The pin
  is a fixed commit, never a branch, so an upstream commit cannot reach this
  repo or break its CI without someone moving the pin.
- ADR-0009 records why: Review-Switch, now its own published repo, owns the
  bridge, and this repo ships a Vendored Copy it does not edit. It supersedes
  the opposite ownership direction recorded in #16, which was settled while
  Review-Switch was still a folder in a private repo.

### Fixed
- The setup wizard named `monitor.py` by a literal path under the plugin root
  when printing the pin's dry run, which the plugin-tree validator rejects and
  which is correct only on the machine it was written on. It now records
  `<crew-skill-dir>` and names the asset relative to it, the placeholder every
  other reference to that asset already used (#38).

## [0.3.7] - 2026-08-14

### Added
- `monitor.py cost --coordinator-session ID` reads the session driving the run
  out of its own transcript and prints it as a `coordinator` row beneath the
  rollup's total, in the same four counters. Step 6 of the crew skill passes
  `$CLAUDE_CODE_SESSION_ID` and copies the printed rollup whole, so the report's
  coordinator row is a figure the coordinator takes rather than one it asks the
  operator for (#28). The row stays out of the run total — it is the judgment
  the run cost, measured against the children's work (ADR-0001) — and is
  printed only, never logged, because a `session-cost` line belongs to a
  launched ticket. Two consequences of a session measuring itself are carried
  in the skill and the doc: the figure is the whole session's, honest as it
  stands only where the session did nothing but the run and otherwise labelled
  a session-wide upper bound, and the transcript is still open as it is read,
  so its last line alone is forgiven for not parsing — the request in flight,
  unbilled either way — while an unparsed line with more of the session after
  it is a hole in the history and takes the row to `--`. A session id with no
  transcript behind it is the same `--` row plus the line saying why.

## [0.3.6] - 2026-08-14

The two defects the first `/crew` run of the fixed code exposed in its own
records (#26, #27): a run now shows a review while it is running, and bills a
child that worked in a subdirectory of its own worktree.

### Added
- A `review` subcommand on the machine-log CLI — `--ticket`, `--lane` and
  `--state` required, `--detail` optional — validating `--state` against the
  closed set `running`/`returned` the way the other event subcommands validate
  theirs (#26).
- Both review bridges write the run's `review` event themselves, one `running`
  line on entry and one `returned` on every exit path they control, including
  an interrupted or errored review and a resumed round two. The bridge writes
  it because it is the only party that deterministically knows a review both
  started and ended, which keeps the dashboard's inputs script-written and
  zero-token (ADR-0001). A run with no log configured writes nothing and
  reviews normally, and a logging failure never changes a review's exit status
  or the JSON its child reads (#26).

### Fixed
- The cost pass compared a transcript's working directory to the ticket's
  worktree by equality, so a child that changed directory inside its own
  worktree failed an identity check against itself and lost its whole cost row
  — three tickets of the #18 run were reported `not measured` for this reason.
  A cwd at or below the worktree is now the same identity, compared by path
  component after resolving both sides, so a sibling sharing a prefix and a
  parent directory both stay outside and stay diagnosed. Both lanes take the
  new rule: the Claude reader's per-record check and the Codex reader's
  `session_meta` check (#27).
- The `review` event was documented and rendered but nothing emitted it, so a
  ticket sitting in a multi-minute review was drawn as a plain `running` row,
  indistinguishable from one still writing code. The "nothing writes it yet"
  notes in `docs/machine-log.md` and `docs/monitor-dashboard.md` are retired
  (#26).

## [0.3.5] - 2026-08-14

The defects the first real `/crew` run exposed, fixed together (#18): a run now
records what it cost, shows itself in one place, and refuses to trust a path it
cannot resolve.

### Added
- A `session-cost` event and a run cost rollup: at completion the monitor reads
  each launched child's transcripts, appends one event per child — ticket,
  executor, model, session, four disjoint token counters and their total — and
  prints the rollup the report quotes. Codex figures are converted to the same
  four counters as Claude's so the totals add across lanes. Nothing unreadable
  is billed: a missing, ambiguous or usage-silent transcript leaves a diagnosed
  event and a `--` row, and the log refuses a record carrying both figures and
  a diagnosis (#23).
- One `crew-dashboard` tmux window per run, owned by the monitor's new `window`
  subcommand: it records the window id, recreates it when the operator closed
  it or the run is resumed, and never closes it. Check, creation and record are
  one critical section under a lock, so overlapping callers still leave one
  window (#21).
- Each child's `launch` event, written by dispatch through the log's own
  writer. The launched set is what wave advancement and the dashboard read, and
  nothing wrote it before — the dashboard was permanently empty (#19).
- The Ticket state vocabulary, in the glossary, and the machine log's `review`
  event shape (#21).
- [ADR-0007](docs/adr/0007-worktree-paths-are-absolute-at-the-boundary-and-compared-by-realpath.md),
  recording the two path invariants — absolute at the boundary, compared by
  realpath — with the first-run defects as their evidence (#24).
- [`docs/cost-baseline.md`](docs/cost-baseline.md): the redacted forensic audit
  of one predecessor `/orchestrate` run, so the figures ADR-0001 decided on
  have a checkable source in the repo and a future run's cost record has
  something to be graded against (#25).

### Changed
- `dashboard` takes the run directory instead of a wave and a worktree list,
  and draws every ticket of every wave from the approved wave table joined with
  the machine log — unlaunched ones as `pending`. Colour is drawn only when a
  terminal is watching, and at end of run it draws its last frame and stops
  (#21).
- Child windows are created detached, so a launching wave leaves the operator's
  focus where it was rather than dragging it through every new child (#19).
- The crew skill's report step runs the cost pass and carries the coordinator's
  own token row beside the run rollup, so the judgment-only design (ADR-0001)
  is checkable from a run's artifacts alone; the resume reference gains a step
  that re-runs the idempotent dashboard window command (#24).
- `/route`'s spec-only overlay is renamed `references/to-tickets+route.md` for
  what it actually is: the rules that ride along on a user-typed `to-tickets`
  run.

### Fixed
- Worktree identity is what a path resolves to, in the monitor, the wake
  monitor and the red-line guard alike. macOS reaches the same directory as
  `/tmp` and `/private/tmp`, so comparing two spellings as strings called a
  live child `vanished` — a false toast, a false wake, and a row nobody could
  act on (#20).
- The machine-log hook embeds absolute paths for both the script and the log. A
  relative spelling resolved against the child's worktree at fire time, so the
  escalation landed in a file nobody reads (#20).
- The red-line guard denies and explains itself when it cannot say where its
  worktree is, instead of standing its own checks down. An unsubstituted
  worktree placeholder disabled the "git runs against this worktree only" check
  outright, and a relative `rm -rf` target was skipped for want of a leading
  slash — a safety check that quietly disables itself is worse than none (#20).
- Dispatch resolves `--out-dir` at the boundary, making "the artifact list is
  absolute" an invariant rather than an error case. The first crew run lost its
  whole first wave to a relative one, and the operator saw only a verification
  timeout (#19).
- Live state comes from each lane's own source: the agents list for a Claude
  child, the bridge state file for a Codex one, which appears in no agents list
  — reading only the agents list drew every Codex ticket of a mixed run
  `vanished` (#21).
- The review thread carries the approval and sandbox policy into the app-server
  thread (#22).

## [0.3.0] - 2026-08-14

### Changed
- Spec-only `/route` no longer drives `/mattpocock-skills:to-tickets` itself: it
  prints the command for the user to type, and that user-typed run cuts,
  confirms, and publishes with `/route`'s rules riding along as additions — the
  skill's upstream gate against model invocation is honoured, not worked around
  (ADR-0006).
- `/route`'s body slims to a thin router. The judgment core — classification
  tests, table shape, `## Routing` template — moves to `references/classify.md`,
  force-read at the point of use, superseding ADR-0005's body-structure bullet;
  the cutting overlay is absorbed into `references/spec-only.md`, which ends
  with a verification pass over the published tickets.
- The plugin-tree validator checks relative links in every skill's
  `references/*.md`, not just its `SKILL.md`, so the moved judgment core keeps
  its broken-link protection.

### Removed
- `trackers.md`'s **publish** operation: publication belongs to `to-tickets`,
  and `/route` reads, edits, and marks.

### Fixed
- `docs/dogfooding-run.md` recorded a wrong root cause ("`to-tickets` may not
  be a slash command"); the real cause is the skill's deliberate
  `disable-model-invocation` gate, which keeps its description out of model
  context entirely.

## [0.2.1] - 2026-08-14

### Fixed
- Test collection died before running anything on every Python that does not
  restore `loader.top_level_dir` after a nested `discover`: loading the asset
  suites repointed the walk that loaded them. Each asset suite now loads on its
  own loader.
- CI installs `aiohttp`, without which the review bridge's 44 tests error on
  import rather than run.

## [0.2.0] - 2026-08-14

Crew v2: the coordinator is reduced to judgment, and everything mechanical
moves into scripts, hooks and a machine-readable log (#4).

### Added
- A machine log, written by scripts and hooks, so the coordinator reads a run's
  state instead of scraping tmux panes (#7).
- A dispatch renderer that composes and launches each child agent's first turn
  from its ticket, model and worktree (#6).
- The monitor's operator dashboard, desktop toasts and receipt check (#8).
- A merge driver that lands a finished wave's branches without waking the
  coordinator (#9).
- Wave auto-advance: a run moves itself from one wave to the next (#10).
- The `code-review-graph` MCP server, registered for both Claude Code and
  Codex.
- ADRs 0001–0005 and the Crew v2 and `/route` glossary entries.

### Changed
- `/route` applies the coordinator-cost lessons learned from `/crew` (#5).
- The crew skill body is slimmed to the judgment core, pointing at its schema
  and grammar sources rather than restating them (#11).
- The coordinator-cost evidence is stated as proportions rather than absolute
  amounts, so it stays true as model pricing moves.

### Fixed
- A review whose bridge driver was killed is now recovered rather than
  restarted; a killed driver used to orphan a live session into a duplicate
  Codex review pane (#13).
- The first review turn is held until MCP has started, instead of being
  injected early and tripping the startup-interrupt prompt (#14).
- Dispatch strips a context suffix before judging a model name, so
  context-suffixed and versioned aliases pass the alias check (#15).
- `gpt-5.6` is registered as an alias, and the glossary is aligned with
  ADR-0003.
- The MCP registration no longer carries the maintainer's absolute path.
- The residue lint reads quoted names and ignored files correctly.

## [0.1.0] - 2026-08-13

Initial public release.

### Added
- AgentCrew as a Claude Code plugin: `/route` classifies and splits a spec's
  tickets across Claude and Codex subscriptions, `/crew` runs them as
  unattended waves of tmux child agents.
- CI workflow that runs the test suite and the plugin-tree validator on every
  push and pull request to `main`, across Python 3.11–3.14.
- `CONTRIBUTING.md` documenting the test/validator commands, the
  one-concern-per-PR rule, and the local-identifier residue check.

### Fixed
- The residue lint walked `.git/`, so a maintainer's own git identity could
  produce permanent false positives; shipped-file enumeration now skips any
  path with a `.git` component (#1, #2).

[Unreleased]: https://github.com/okqixiaobao727-design/agentcrew-dev-skills/compare/v0.4.1...HEAD
[0.4.1]: https://github.com/okqixiaobao727-design/agentcrew-dev-skills/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/okqixiaobao727-design/agentcrew-dev-skills/compare/v0.3.8...v0.4.0
[0.3.5]: https://github.com/okqixiaobao727-design/agentcrew-dev-skills/compare/v0.3.0...v0.3.5
[0.3.0]: https://github.com/okqixiaobao727-design/agentcrew-dev-skills/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/okqixiaobao727-design/agentcrew-dev-skills/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/okqixiaobao727-design/agentcrew-dev-skills/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/okqixiaobao727-design/agentcrew-dev-skills/releases/tag/v0.1.0
