# Why the driver's background task was killed twice in run #60

During the crew run for issue #60 the driver's background task died twice with no snapshot, no
error and no exit code — the coordinator saw only `[killed]` in the task's output file and had to
relaunch blind. This document names the cause, shows the evidence, and recommends what to do
about it.

**Verdict (confidence: high).** Nothing killed the driver. Claude Code's own background-shell
**memory-pressure reap** stopped it: when macOS raises a memory-pressure notification, an
interactive Claude Code session kills its *main-session* background bash tasks, provided the user
has not interacted for 30 minutes, no turn is in flight, and no agent-type task is live. The kill
is silent by construction — the harness writes `[killed]` and a `<status>killed</status>`
notification with no reason and no exit code. Both of run #60's kills follow a system
memory-pressure warning by under nine seconds, and every pressure event of the run that did *not*
kill a task is explained by the same rule's guards.

Versions and machine: Claude Code **2.1.233** (`/Users/simon/.local/share/claude/versions/2.1.233`,
a Bun-compiled binary), macOS on the maintainer's Mac, run #60 on 2026-08-17. Times below are UTC
where they come from Claude Code's own records and local (UTC+12) where they come from the macOS
unified log; both are given wherever the two are compared.

## What the harness recorded

The driver runs as a Claude Code background bash task started by the coordinator
(`skills/crew/SKILL.md:35-39`; the coordinator's session is `2c3ef71a-eb99-4ac9-932b-0e8b7f649dd6`,
pid 82811, permission mode `bypassPermissions`). Six such tasks carried run #60 (confidence: high;
source: the coordinator transcript
`~/.claude/projects/-Users-simon-Documents-coding-skills-agentcrew-dev-skills/2c3ef71a-eb99-4ac9-932b-0e8b7f649dd6.jsonl`
and the task output files under
`/private/tmp/claude-501/-Users-simon-Documents-coding-skills-agentcrew-dev-skills/2c3ef71a-eb99-4ac9-932b-0e8b7f649dd6/tasks/`):

| Task | Started (UTC) | Ended (UTC) | Lifetime | How it ended |
| --- | --- | --- | --- | --- |
| `b6e7xft5p` | 03:53:41.9 | ~03:56:20 | ~2m38s | snapshot, `[exited with code 0]` |
| `bj8fz8xa0` | 03:56:35.4 | ~04:23:27 | ~26m52s | snapshot, `[exited with code 0]` |
| `b9iuz3ag1` | 04:23:31.5 | **04:40:08.1** | **16m37s** | **`[killed]`, no output** |
| `b9mcdhk82` | 04:40:16.4 | ~04:43:38 | ~3m22s | snapshot, `[exited with code 0]` |
| `b06yp46bh` | 04:44:00.1 | **05:01:58.8** | **17m59s** | **`[killed]`, no output** |
| `bdv4q6ryn` | 05:02:07.2 | ~05:48:1x | ~46m | `run-complete` snapshot, exit 0 |

Both killed tasks' output files contain exactly the driver's first line plus `\n[killed]\n`
(verified byte-for-byte with `od -c` on `b9iuz3ag1.output` and `b06yp46bh.output`; confidence:
high). The wake the coordinator received was a task notification carrying `<status>killed</status>`
(transcript entries at 04:40:08.150Z and 05:01:58.947Z; confidence: high).

Two facts kill the obvious hypotheses on their own (confidence: high):

- **Not a fixed lifetime cap.** The two kills came at 16m37s and 17m59s, while `bj8fz8xa0` ran
  26m52s and `bdv4q6ryn` ran ~46m untouched.
- **Not an output-idle timeout.** The driver prints nothing between its first line and its exit
  snapshot, so the survivors were silent for their whole lives too. (Source: the four
  successful task output files, each two lines plus the exit marker.)

The driver's own code also has no silent exit: every exit path prints a snapshot
(`skills/crew/assets/driver/driver.py:333` `snapshot()`, `:2848` `main()`, `:2861`
`sys.exit(main())`), which is what the ticket's prior code audit found (confidence: high — the
killed tasks produced no snapshot at all, so the process did not reach any of its own exits).

## The mechanism, read out of the harness binary

Claude Code 2.1.233 registers a memory-pressure handler per background bash task. Reproduce with
`strings -a ~/.local/share/claude/versions/2.1.233 | grep -o 'function Y3p.\{0,420\}'` — the
bundled source reads (confidence: high, quoted verbatim):

```js
function Y3p(e,t,r,n,o,i){
  Mye(i,`bash:${e}`,r);
  let s;
  if(i===void 0 && !xn() && !V.CLAUDE_CODE_DISABLE_BG_SHELL_PRESSURE_REAP){
    let a=()=>{
      let l=r.get(e);
      if(l?.status!=="running"||l.notified||Date.now()-o5()<$fS||$ps()||N4e(r.all()))return;
      _e("task_local_shell_pressure_reap"),
      YRa(e,t,"killed",void 0,r,n,o,i,void 0),
      hYe(e,r)
    };
    process.on("memoryPressure",a), s=()=>process.off("memoryPressure",a)
  }
  ...
}
```

Reading the identifiers out of the same bundle (each `grep`-able in the binary; confidence: high):

- `i` is the task's `agentId`. `i===void 0` means the reap is armed **only for main-session
  background tasks** — a subagent's background shell is not reaped, it gets a hard lifetime cap
  instead: `function V3p(e){if(e===void 0)return;return V.CLAUDE_SUBAGENT_BG_SHELL_MAX_MS||BfS}`
  with `BfS=3600000` (60 minutes).
- `xn()` is `!isInteractive()`, so the reap is armed **only in interactive sessions** — exactly the
  coordinator's case.
- `V.CLAUDE_CODE_DISABLE_BG_SHELL_PRESSURE_REAP` is an environment-variable kill switch, unset on
  this machine (it appears in the binary's env-var table and nowhere in `~/.claude/settings.json`).
- `o5()` is `userPresence.lastInteractionTime()` and `$fS=1800000` — the reap is **suppressed while
  the user interacted within the last 30 minutes** (`BfS=3600000,$fS=1800000` is one grep away in
  the constants block).
- `$ps()` is `surfaceCapabilities.mainLoopBusy()` — suppressed while a turn is in flight.
- `N4e(r.all())` is true when some agent/teammate/remote-agent task is live — suppressed then too.
- `YRa(...,"killed",void 0,...)` writes `[${r==="killed"?"killed":...}]` to the output file and
  emits the task notification: **no exit code, no reason** — the silence the coordinator saw.
- `hYe` logs `LocalShellTask ${e} kill requested` to the debug log (not enabled for this run) and
  sets the task's status to `killed`.

`process.on("memoryPressure", …)` is a Bun runtime event (the binary is Bun-compiled; the strings
`memoryPressure`, `memoryPressureLevel` and `Bun.ant.memoryPressureLevel()` all appear in it),
fed by the same macOS dispatch memory-pressure source other processes log against. That the Bun
event is a faithful mirror of the macOS notification is **medium** confidence — inferred from the
name, the runtime, and the timing correlation below, not from Bun's source.

## The timeline that closes it

macOS logged exactly four warning-level memory-pressure events during run #60 (confidence: high;
command and output reproduced from the unified log, which is why local time appears here):

```
$ /usr/bin/log show --start "2026-08-17 15:50:00" --end "2026-08-17 17:50:00" \
    --predicate 'eventMessage CONTAINS "Received dispatch memory pressure event"' --style compact
2026-08-17 15:55:27.184  modelmanagerd  ... memory pressure event: warning
2026-08-17 16:02:39.298  modelmanagerd  ... memory pressure event: normal
2026-08-17 16:10:01.191  modelmanagerd  ... memory pressure event: warning
2026-08-17 16:39:59.254  modelmanagerd  ... memory pressure event: warning
2026-08-17 16:54:26.465  modelmanagerd  ... memory pressure event: normal
2026-08-17 17:01:51.072  modelmanagerd  ... memory pressure event: warning
2026-08-17 17:05:16.509  modelmanagerd  ... memory pressure event: normal
```

The coordinator's last *typed* prompt was `/agentcrew-dev-skills:crew #60` at **03:51:58.2Z**
(15:51:58 local); everything after it in the transcript is a cross-session message or a task
notification, never a user prompt (confidence: high). Applying the rule above to each pressure
warning predicts the outcome in all four cases:

| Pressure warning (local / UTC) | Task then running | Since last typed prompt | Rule predicts | What happened |
| --- | --- | --- | --- | --- |
| 15:55:27 / 03:55:27 | `b6e7xft5p` | 3m29s | suppressed (<30m) | survived |
| 16:10:01 / 04:10:01 | `bj8fz8xa0` | 18m03s | suppressed (<30m) | survived |
| 16:39:59 / 04:39:59 | `b9iuz3ag1` | 48m01s | **reaped** | **killed 04:40:08.1Z (+8.8s)** |
| 17:01:51 / 05:01:51 | `b06yp46bh` | 69m53s | **reaped** | **killed 05:01:58.8Z (+7.8s)** |

And the last task, `bdv4q6ryn`, ran 46 minutes past the 30-minute presence window untouched
because **no warning-level pressure event occurred** between 05:02 and the run's end at 05:48
(confidence: high) — the rule needs a pressure event, not just an absent user.

Four predictions, four hits, plus one negative control. The remaining looseness: `lastInteraction`
is moved by keystrokes and terminal focus, which the transcript does not record, so the two
"suppressed" rows are **medium** confidence (a keystroke could also have refreshed the window);
the two kill rows do not depend on that reading. The ~8-second lag between the logged system
event and the kill is unexplained in detail — most likely dispatch-source coalescing before Bun
re-emits — **low** confidence on the explanation, high on the correlation.

Why the machine was under pressure at all: run #60 had four to six Claude children plus Codex
review panes live, each `claude` process resident at roughly 430 MB (measured on a comparable
later run with `ps aux`; confidence: medium — measured on run #61's children, not on #60's, whose
processes are gone).

## What else this exposes

- **Children's background review commands are equally reapable** (confidence: medium-high). Each
  crew child is its own interactive `claude` process running the review bridge as a main-session
  background task — the same `i===void 0 && !xn()` arm. A review that outlives 30 minutes of
  operator absence during a pressure event dies the same silent death. The dispatch prompt already
  tells children to recover a review with `--recover-session` rather than start a second one, so
  the contract covers it; the reason it fires is now named.
- **A driver launched from a subagent would face a 60-minute hard cap**
  (`CLAUDE_SUBAGENT_BG_SHELL_MAX_MS`, default `BfS=3600000`) instead of the reap (confidence:
  high). AgentCrew launches the driver from the coordinator's main session, so this does not bite
  today — it is a reason not to move that launch under a subagent.
- **The reap is not a bug in AgentCrew and cannot be prevented by driver code** (confidence: high):
  the decision is taken in the harness, about the harness's own child process, with no signal the
  driver can catch first (`shellCommand.kill()` then `cleanup()`).

## Recommendation: accept-with-adopt, and document the one-line opt-out

**Accept-with-adopt is the recommendation** (confidence in the reasoning: high; it is a judgement,
not a measurement). The evidence says the failure is loud enough and cheap enough to live with:
the harness wakes the coordinator the moment it reaps, the coordinator relaunched in **8 seconds**
(04:40:08.1 → 04:40:16.4) and **9 seconds** (05:01:58.8 → 05:02:07.2), and `resume` adopts the run
in progress, so run #60 lost no ticket, no receipt and no merge. The remaining cost is precisely
two coordinator wakes — the cost this spec's quiet-coordinator work is otherwise attacking.

Two changes are worth making on top of that, and both are cheap:

1. **Name the cause where the coordinator meets it.** The wake that says `killed` should be
   readable as "the harness reaped this under memory pressure; relaunch it" rather than as an
   unexplained external kill. That is prose in the crew skill's triage reference, not code.
2. **Document the opt-out for operators who see it often**: setting
   `CLAUDE_CODE_DISABLE_BG_SHELL_PRESSURE_REAP=1` in the coordinator session's environment
   disarms the reap entirely (it is read at task start, per the quoted guard). Recommended as an
   operator-chosen environment setting, **not** as something AgentCrew sets on the user's behalf:
   it is an undocumented internal variable of Claude Code 2.1.233 that may change without notice,
   and it disables a safety valve on a machine that genuinely reached memory pressure.

Rejected alternatives, with reasons:

- **Automatic re-launch by the driver or a supervisor** — already out of scope in the spec, and the
  measured 8–9 second manual recovery does not justify a new resident process (ADR-0001).
- **Keeping the driver's output alive to look "busy"** — the reap does not consult output; the
  guards are user presence, main-loop busy, and agent-task liveness. Chatter would only pollute the
  wake channel.
- **Running the driver outside the harness** (`nohup`, a launchd job, a tmux pane) — it would
  dodge the reap, but it also dodges the wake channel that makes the driver's snapshot reach the
  coordinator, which is the whole transport ADR-0010 rests on.

## Open questions

- Whether Bun's `memoryPressure` event fires on `warning` as well as `critical` levels, and with
  what coalescing — the ~8-second lag is consistent with either. Answering it needs a Bun source
  or a deliberate pressure experiment, neither done here (confidence: low on the current guess).
- Whether `lastInteractionTime` counts terminal focus changes and stray keystrokes; if it does, an
  operator who touches the coordinator window every half hour is immune, which would make "leave a
  keystroke" an unwritten mitigation. Not verified.
- Whether the same reap explains any earlier unexplained silent deaths in runs before #60. Their
  task output files under `/private/tmp/claude-501/...` are the place to look; not checked here.
