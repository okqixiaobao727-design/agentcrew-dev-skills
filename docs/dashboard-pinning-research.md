# Pinning the dashboard to the coordinator's screen

The wave dashboard gets its own tmux window today ([`docs/monitor-dashboard.md`](monitor-dashboard.md)),
so watching the run means leaving the coordinator's Claude Code session — and leaves a window the
human has to close by hand afterwards. The question this answers is where that frame can live
instead: roughly 8 rows tall, up to ~180 columns wide, colour in the `STATE` column, and growing a
row for every ticket and every annotation.

Three requirements decide it, and every candidate below is judged against all three:

1. **No window, no pane, no popup of its own.** The dashboard lives in a region that already
   exists, or it does not qualify.
2. **The coordinator pane keeps its full size.** Nothing shrinks it.
3. **Lifecycle is automatic.** The surface appears when a run starts and disappears when it ends,
   driven by the run. The human never opens it and never closes it, and a crashed or killed run
   leaves no visible residue.

Versions measured against: **Claude Code 2.1.232**, **tmux 3.6a** installed (**tmux 3.7b** is the
current stable release). Everything below marked *measured* was run in throwaway detached sessions
against a real Claude Code process; the sessions and their sockets were removed afterwards.

The answer is that exactly one candidate clears all three, and it is Claude Code's own
`statusLine`. tmux's extra status lines come second and fail on requirement 2 and on capacity.

## Requirement 2 has a catch worth stating first

Nothing can add eight rows of display without taking eight rows from somewhere. The only
mechanisms that draw *over* a pane without costing it rows are overlays — tmux popups and tmux 3.7
floating panes — and both are disqualified by requirement 1, because both are things the human
ends up closing. So requirement 2 has to be read at the level it was written: **does the
coordinator's pane get resized?**

On that reading the two live candidates part company, and it is the single most decisive
measurement in this document:

| Surface | tmux pane before | during | after |
| --- | --- | --- | --- |
| Claude Code `statusLine` | 190×45 | **190×45** | 190×45 |
| tmux `status 5` (+ pane border) | 190×49 | **190×44** | 190×49 |

Claude Code reallocates rows *inside* its own pane; the pane itself is never resized, so nothing
else in the window moves. tmux's status lines take rows off the window and genuinely shrink the
pane, which forces Claude Code to redraw at a new size every time the bar appears or disappears.

## The winner: Claude Code's `statusLine`

`statusLine` runs a shell command and draws whatever it prints, in a region between the prompt box
and the footer badges ([statusline docs](https://code.claude.com/docs/en/statusline)). It is a
region that already exists — `~/.claude/settings.json` already points it at
`~/.claude/statusline.sh` — so requirement 1 is satisfied by construction: the run does not create
a surface, it changes what an existing one prints.

### It self-refreshes, which is the whole question

Without `refreshInterval` the command runs on conversation events only, and the docs name this
project's exact failure: *"The event-driven triggers can go quiet when the main session is idle,
for example while a coordinator waits on background subagents."* `refreshInterval` is the
documented fix — *"The optional `refreshInterval` field re-runs your command every N seconds in
addition to the event-driven updates. The minimum is `1`."* Updates are debounced at 300 ms, and a
trigger arriving while the script is still running cancels the in-flight one.

Measured, at `refreshInterval: 2`, in a 190×45 pane with a writer loop rewriting a frame file: the
frame counter advanced in the region with **no keystroke sent and no API call made**. So the
answer to "can it show more than a stale snapshot" is yes, unambiguously.

### The full lifecycle, measured

The design is a wrapper script that always prints the existing two lines and appends the frame
file only while a run is live. Nothing else changes. The whole sequence was run end to end with
the only input being the creation and deletion of a marker file:

| Moment | What the operator sees | Pane size |
| --- | --- | --- |
| no run | the existing 2 lines | 190×45 |
| run starts (`touch` a marker) | 10 lines: the 2 plus the full 8-row table, within one tick | 190×45 |
| run advances | rows update on their own, no keystroke | 190×45 |
| run ends *or is killed* (marker gone) | back to 2 lines, prompt box back in place, **zero residue** | 190×45 |

That is requirement 3 satisfied with no hook, no watchdog, and no teardown command — because there
is nothing to tear down. The run never mutates any tmux or Claude Code state; it writes a file and
deletes it. A killed run cannot leave the bar up, because the bar is a function of the marker.

The one permanent change is `refreshInterval` in `settings.json`, set once at install. It is inert
between runs — the wrapper just prints two lines slightly more often.

### What it will and will not draw

| Property | Behaviour | Source |
| --- | --- | --- |
| multiple lines | *"each `echo` or `print` statement displays as a separate row"* | [docs][sl] |
| line cap | none documented, none found — 12 printed lines drew as 12 rows | measured |
| colour | raw ANSI passes through, foreground and background alike | [docs][sl], measured |
| width | `COLUMNS` and `LINES` exported before the command runs | [docs][sl], changelog 2.1.153 |
| overlong lines | cut at terminal width with `…`, never wrapped | measured; changelog 2.1.141 |
| arbitrary text | no format language, no expansion — bytes are drawn as given | measured |
| cost | *"The status line runs locally and does not consume API tokens"* | [docs][sl] |

[sl]: https://code.claude.com/docs/en/statusline

Four sharp edges came out of the measurements:

- **Blank lines are dropped.** A `printf '\n'` between two rows vanishes, so a spacer has to be a
  line with characters on it.
- **`\033[0m` does not reset to the terminal default.** Claude Code parses the ANSI and re-emits
  it, rewriting a reset into its own grey (`\033[38;5;246m`) plus `\033[49m`. Colours survive;
  "plain" becomes Claude Code's grey.
- **Printing nothing leaves one blank row**, not a collapsed region — the wrapper must always
  print at least the existing two lines rather than exiting silently.
- **Rows past the bottom of the terminal are lost.** A 32-line script in a 50-row pane drew 21
  rows and the rest were gone. The dashboard is unbounded in principle, so the renderer has to cap
  itself against `LINES`.

Two costs are real and worth naming. The region is **bottom-anchored** — there is no position
setting, so this is pinned to the bottom, not the top. And the region grows downward-anchored,
pushing the prompt box up, so an 8-row table costs 8 rows of visible transcript on top of the 2 the
existing statusline already spends.

### What the existing statusline already occupies

`~/.claude/settings.json` carries `"statusLine": {"type": "command", "command":
"~/.claude/statusline.sh"}` — no `padding`, and **no `refreshInterval`**, so today it redraws on
conversation events only. The script prints two lines: model, effort, git branch and abbreviated
cwd; then context tokens and percentage, the 5-hour and 7-day rate-limit windows with countdowns,
the scoped weekly cap, session cost and line churn.

It costs **~80 ms per run** (measured, five runs, 70–100 ms), dominated by its `git status
--porcelain` call. At `refreshInterval: 2` that is roughly 4% of one core, continuously, per
session. Reading the frame file adds nothing measurable (<10 ms). If that matters, the interval is
the dial, or the wrapper can skip the git work while a run is live.

### Residue moves rather than disappearing

Because nothing is mutated, the only way this surface can get stuck is a **stale marker** — a run
that died without deleting it. The guard is to make staleness itself the condition: draw the frame
only if the frame file's mtime is within a few refresh intervals, so a dead writer takes the
display down with it. That is the same shape as the toast-state and usage-cache staleness guards
already in this project.

## The runner-up: tmux extra status lines

This was the expected front-runner and it does clear requirement 1 and most of requirement 3. It
fails on requirement 2 and on capacity, and it carries a security problem.

### It works, and it self-heals

Everything necessary was confirmed in a nested throwaway session at 190 columns:

- **Per-session scoping works.** `tmux set -t <session> status 5` and friends, with no `-g`, apply
  to the crew session alone and leave every other session untouched.
- **`status-position top`** puts the bar at the top, which is what was originally wanted.
- **`status-interval 1` self-refreshes reliably.** Eight consecutive frames were picked up at one
  per second with no input.
- **`refresh-client -S` forces an immediate redraw**, even with `status-interval 0` — so a
  pure event-driven model with no polling is possible, pushed by the run.
- **One line per slot via `#(sed -n 'Np' frame.txt)` works**, which is the way around `#()`
  returning only its last line.
- **Teardown can be self-healing.** The `#()` command that draws the bar can also remove it: when
  the run marker vanishes, the same script runs `tmux set status on` and `set -u` on
  `status-format[N]`, `status-position` and `status-interval`. Measured: within one tick the bar
  collapsed, the pane went back from 45 to 49 rows, and `show` reported no residue at all. A
  crashed run cleans up after itself with no external watchdog. (Gotcha: the option takes `off |
  on | 2 | 3 | 4 | 5` — `set status 1` answers `unknown value: 1`.)

### Why it still loses

**Six lines, hard ceiling.** *"Using `on` gives a status line one row in height; `2`, `3`, `4` or
`5` more rows"* (`man tmux`, `status`). `set -g status 6` answers `unknown value: 6`, and
`status-format[5]` is accepted silently but never drawn. Stacking `pane-border-status top` with a
`pane-border-format` buys exactly one more row — measured, and it renders as
`──3  09  Skill copy and ADR …────────`, a border with text in it rather than a clean row. Six
total, against a dashboard that is 8 rows for a 6-ticket run and grows with every ticket and every
annotation row. This is not "8 versus 5", it is "unbounded versus 6".

**It shrinks the pane.** Measured 190×49 → 190×45 for `status 5`, → 190×44 with the border line.
Requirement 2 fails outright, and Claude Code gets resized twice per run.

**Colour must be rewritten.** Raw ANSI from `#()` is not interpreted — `\033[32m` renders as
visible `[32m` junk inside the status line's own styling. tmux's own `#[fg=green]` markup *does*
work when the shell command emits it (measured), so colour is achievable, but only in tmux's
dialect, and `#[default]` resets to the status-line style rather than the terminal's.

**Text is truncated at the client width with no ellipsis** — a 205-character line rendered as
exactly 190 characters with the tail silently gone.

**`#()` output is re-expanded as a tmux format, and that is a shell-injection hazard.** Measured:
a command printing `TITLE: fix #{host} and #(id -un)` rendered as `TITLE: fix
SImons-MacBook-Air.local and …` — the `#{host}` was substituted and the nested `#(id -un)` was
*executed*. Ticket titles in this project come from GitHub Issues, so they are attacker-influenced
text. Every `#` would have to be doubled to `##` before it reaches a format; measured, `sed
's/#/##/g'` restores the literal text exactly. That is a correctness and security burden the
`statusLine` route does not have, because `statusLine` output is drawn as bytes with no expansion.

## Ruled out quickly

**`display-popup`** — fails requirement 1 and requirement 3 by definition: it is a thing the human
dismisses. It is also modal. *"A popup is a rectangular box drawn over the top of any panes. Panes
are not updated while a popup is present."* (`man tmux`). Measured on 3.6a: keystrokes sent to the
client landed **inside the popup**, not in the pane behind, and the pane behind stopped repainting.
The operator could not type to the coordinator. There is no non-modal or click-through flag.

**tmux 3.7 floating panes** — in the previous round this was the closest match to "pinned to the
top", and it works exactly as advertised (measured on an extracted 3.7b binary: Claude Code's pane
stayed 150×40 and kept the keyboard while an 8-row coloured dashboard floated at `0,0`). Under the
new constraint it is **out**: it is a pane. It is listed in `list-panes`, the human can focus it,
and it has to be killed. Its teardown is scriptable (`kill-pane`), but requirement 1 is not about
teardown, it is about not creating a pane in the first place.

**`pane-border-status` alone** — one line, format-only, and it costs the pane a row. Useful only
as the sixth line stacked under `status 5`; not a home for the table.

**Claude Code `subagentStatusLine`** — an interesting near-miss worth recording. It is an existing
region with a *perfect* lifecycle: Claude Code creates and destroys one row per subagent in the
agent panel, and the setting only overrides each row's body, with ANSI and OSC 8 allowed
([docs][sl]). If crew children were the coordinator's own subagents this would be the ideal
answer — rows that appear and vanish with the children, no marker file, no teardown. They are not:
crew children are separate sessions in their own tmux windows, so the coordinator's agent panel is
empty and there are no rows to override. Worth re-checking if the dispatch model ever changes.

**Output styles, hooks, slash commands, terminal chrome** — none of these is a persistent screen
region. Output styles *"directly modify Claude Code's system prompt"* with no UI component
([docs](https://code.claude.com/docs/en/output-styles)). SessionStart `additionalContext` goes to
the **model**, not the screen — *"stdout is added as context that Claude can see and act on"*
([hooks docs](https://code.claude.com/docs/en/hooks)) — which would spend tokens and put run state
in the coordinator's context, exactly what
[ADR-0001](adr/0001-coordinator-spends-tokens-only-on-judgment.md) forbids. Slash-command output
scrolls away. Terminal-level status bars are one row each (iTerm2's is a single row of components;
WezTerm's `set_left_status` renders into the tab-bar row) and Ghostty, which is what runs here,
ships none — all three would also tie the dashboard to one terminal.

**A Claude Code overlay or pinned-header API** — does not exist. Every overlay in Claude Code
(`?`, `/rewind`, the `/goal` panel, the error overlay) is first-party and modal. Checked the
statusline, hooks, output-styles and settings documentation and grepped the full CHANGELOG for
`overlay`, `pinned`, `floating` and `header`.

## Verdict

| | no window/pane/popup | pane keeps full size | automatic lifecycle | fits the table |
| --- | --- | --- | --- | --- |
| **Claude Code `statusLine`** | yes | **yes** — pane never resized | yes, and nothing to tear down | yes, no line cap found |
| tmux status lines (+border) | yes | **no** — 49→44 rows | yes, self-healing via `#()` | **no** — 6 rows, hard cap |
| `pane-border-status` alone | yes | no — costs a row | yes | no — 1 row |
| `display-popup` | **no** | yes | **no** — human dismisses it | yes |
| tmux 3.7 floating pane | **no** — it is a pane | yes | scriptable, but it is a pane | yes |
| `subagentStatusLine` | yes | yes | perfect — but no rows exist | no — 1 line per row |

**Best option: Claude Code's `statusLine`, driven by a wrapper script and a run marker.** It is the
only candidate that clears all three requirements, and it clears them without mutating anything:
the run writes a frame file and a marker, the wrapper prints them, and deleting the marker removes
the display. It has no line cap, takes raw ANSI so the renderer's existing colour works unchanged,
needs no escaping of ticket titles, and never resizes the coordinator's pane. Verified end to end
against a live 2.1.232 session, including the crash case. Its honest costs: the region is anchored
at the **bottom**, not the top; it spends 8 rows of visible transcript while a run is live; and
`refreshInterval` has to be added to `settings.json` once, costing ~80 ms every N seconds
thereafter.

**Nearest fallback: tmux extra status lines**, `status 5` + `status-position top` +
`status-interval 1`, scoped to the crew session, with the `#()` command self-healing the bar away
when the run marker disappears. Take this only if top placement is worth more than capacity,
because it caps at 6 rows against an unbounded table, it shrinks the coordinator's pane by 5 rows,
its colour must be re-expressed as `#[…]` markup, and every `#` in a ticket title must be doubled
or a GitHub issue title can execute a shell command through `#()`.

**Not possible as asked:** a surface that adds eight rows *and* costs the coordinator nothing. The
only mechanisms that draw over a pane without taking rows from it are popups and floating panes,
and both are the kind of thing the human has to close — which is the constraint that ruled them
out. The nearest thing is the winner above: the rows come out of Claude Code's own transcript
area rather than out of the pane, and they come back the moment the run ends.
