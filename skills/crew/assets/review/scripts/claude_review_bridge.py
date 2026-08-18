#!/usr/bin/env python3

"""Headless Claude code-review channel for a Codex child.

The sibling `tui_review_bridge.py` runs the other direction (a Claude session
obtains a Codex TUI review) and can watch a real pane. There is no pane here:
`claude -p` prints one JSON object and exits, so this bridge keeps the raw
stdout/stderr in a log file and the lineage in a state file.

Round one starts a review lineage; round two passes the stored lineage id back
and the bridge resumes the same Claude session with `-r`, so the follow-up
still sees round one's findings.

Given `--machine-log` and `--ticket`, every call also leaves the run's machine
log the pair of `review` lines the contract describes — `running` on entry and
`returned` on exit — so the dashboard's review annotation appears and disappears
with no operator action and no model token spent (ADR-0001).
"""

import argparse
import dataclasses
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import launch_hook  # noqa: E402  — a sibling asset, reached from this file's own directory

SESSION_STATE_VERSION = 1
DEFAULT_TIMEOUT_SECONDS = 7200
# The log's own writer, so the `review` event's shape and its closed set of states stay the
# log's alone: this bridge names the event, never spells it.
MACHINE_LOG = pathlib.Path(__file__).resolve().parents[2] / "machine_log.py"
# The vendor half of the lane this bridge reviews in. The model half is whatever the lineage was
# pinned to, so the lane needs no argument of its own.
REVIEW_VENDOR = "claude"
# Fixed by spec-113: a headless reviewer has nobody to answer a permission
# prompt, so the mode is not a caller-tunable option.
PERMISSION_MODE = "bypassPermissions"
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
# A reviewer-config override in a caller's environment (a test seam of whatever
# resolves the machine's reviewer) would follow this reviewer into its session
# and answer a lookup it never makes, so it is dropped.
REVIEWER_CONFIG_ENV_VAR = "CODE_REVIEWER_FILE"
BINARY_ENV_VAR = "CODE_REVIEW_CLAUDE_BINARY"
STATE_DIR_ENV_VAR = "CODE_REVIEW_CLAUDE_STATE_DIR"
# The optional knowledge-graph CLI that scores the range under review. It is an enhancement, never
# a requirement: every way it can be absent or unhelpful ends in the git summary instead.
GRAPH_CLI = "code-review-graph"
# Measured at ~0.2s against this repository; a graph query that takes minutes is a broken graph,
# and the review must not wait on one.
GRAPH_TIMEOUT_SECONDS = 60
GIT_TIMEOUT_SECONDS = 60
# Redirecting the graph through the environment was measured to answer with a silent zero-risk
# score, so an inherited redirection is dropped rather than obeyed: `--repo` is the only pointer.
GRAPH_REDIRECT_ENV_VARS = ("CRG_DATA_DIR", "CRG_REPO_ROOT")
# The brief report's changed-function count. Zero of them against a diff that plainly changed
# something is the stale-or-empty-graph signature, not an answer.
CHANGED_FUNCTIONS_PATTERN = re.compile(r"(\d+)\s+changed function", re.IGNORECASE)
# The last line the round-one prompt asks the reviewer for. The re-review gate reads it, so it is
# spelled once here and quoted into the prompt from there.
VERDICT_LINE = (
    "REVIEW VERDICT: spec-findings-requiring-fix=<count> standards-findings=<count>"
)
# The whole line or nothing: a fragment of it, or the phrase inside prose, is not the report
# saying a fix is required, and the gate must not read one as if it were.
VERDICT_PATTERN = re.compile(
    r"^\s*REVIEW VERDICT:\s+spec-findings-requiring-fix=(\d+)\s+standards-findings=\d+\s*$",
    re.IGNORECASE | re.MULTILINE,
)
# The four disjoint counters a `session-cost` line carries, in the machine log's own spelling.
COUNTERS = ("input", "output", "cache_read", "cache_creation")
# What the headless result's `usage` object calls each of them. Claude reports its cached tokens
# beside the input count rather than inside it, so the four arrive already disjoint.
USAGE_FIELDS = {
    "input": "input_tokens",
    "output": "output_tokens",
    "cache_read": "cache_read_input_tokens",
    "cache_creation": "cache_creation_input_tokens",
}

# One re-review, and only for a spec finding: the contract's cap, enforced here rather than left
# to the caller's self-restraint.
MAX_ROUNDS = 2


class BridgeError(RuntimeError):
    pass


class ReviewEvent:
    """The pair of `review` lines one bridge call leaves in the run's machine log.

    Both the log path and the ticket are optional everywhere they appear, and a call given
    neither writes nothing: `--log` is optional on dispatch, and a run without one still reviews
    normally.
    """

    def __init__(self, log, ticket, model, vendor=REVIEW_VENDOR):
        # Absolute where the path enters, before it is forwarded to the writer (ADR-0007), so
        # what this bridge records cannot be moved by anyone's working directory. Symlinks are
        # left alone: the operator's own name for the run directory is the one to log under.
        self.log = os.path.abspath(log) if log else None
        self.ticket = ticket
        # Vendor then model, separated by a space: the annotation row prints the field verbatim
        # after collapsing whitespace, so this is the spelling the dashboard shows.
        self.lane = " ".join(part for part in (vendor, model) if part)
        # A review leaves exactly one cost line per call, whether it came back with figures or
        # with the reason there are none.
        self.costed = False

    def write(self, state):
        """Append one end of this review; returns nothing, and fails at nothing.

        A review that succeeded must not be reported as a failure because its bookkeeping could
        not be written, so every way the append can go wrong is swallowed here: the caller's exit
        status and the JSON object the reviewed child reads are the same either way.
        """
        if not self.log or not self.ticket:
            return
        try:
            subprocess.run(
                [
                    sys.executable, str(MACHINE_LOG), "--log", str(self.log), "review",
                    "--ticket", str(self.ticket), "--lane", self.lane, "--state", state,
                ],
                capture_output=True, text=True, check=False,
            )
        except OSError:
            pass

    def cost(self, session, model, counters, detail):
        """Append this review's one `session-cost` line; returns nothing, and fails at nothing.

        Lane-tagged, so a review's spend is told apart from the implementing child's, and written
        through the log's own writer, which holds the figures-or-diagnosis rule: `counters` are
        the four disjoint totals, or `None` with `detail` saying why nobody could tell.

        Accounting must never fail a review, so every way this can go wrong is swallowed here,
        exactly as the `review` pair's write is.
        """
        if not self.log or not self.ticket:
            return
        self.costed = True
        command = [
            sys.executable, str(MACHINE_LOG), "--log", str(self.log), "session-cost",
            "--ticket", str(self.ticket), "--executor", REVIEW_VENDOR,
            "--model", model or "", "--lane", self.lane,
        ]
        if session:
            command += ["--session", str(session)]
        if counters is None:
            command += ["--detail", detail]
        else:
            for name in COUNTERS:
                command += [f"--{name.replace('_', '-')}-tokens", str(counters[name])]
            command += ["--total-tokens", str(sum(counters.values()))]
        try:
            subprocess.run(command, capture_output=True, text=True, check=False)
        except OSError:
            pass


@dataclasses.dataclass(frozen=True)
class ClaudeResult:
    session_id: str
    result: str
    is_error: bool
    subtype: str
    permission_denials: list
    payload: dict


def resolve_claude_binary(explicit=None, environment=None):
    """Absolute path of the real `claude` executable.

    The launch below is an argv list with no shell, so a `claude` *shell
    function* is already out of the picture. Resolving symlinks as well means a
    wrapper script dropped on PATH cannot intercept the child either.
    """
    environment = os.environ if environment is None else environment
    candidate = explicit or environment.get(BINARY_ENV_VAR)
    if not candidate:
        candidate = shutil.which("claude", path=environment.get("PATH"))
    if not candidate:
        raise BridgeError("Cannot find the claude executable")
    resolved = pathlib.Path(candidate).resolve()
    if not resolved.is_file():
        raise BridgeError(f"claude executable does not exist: {resolved}")
    return str(resolved)


def child_session_env(hook):
    """Environment for the headless Claude this bridge spawns.

    A review child is a child launch like any other, so it carries whatever the
    project's on-child-launch hook adds — nothing, until a project configures it.
    """
    env = hook.child_env()
    env.pop(REVIEWER_CONFIG_ENV_VAR, None)
    return env


class SessionStore:
    """State and log files for review lineages, one pair per lineage."""

    def __init__(self, root=None):
        if root is None:
            configured = os.environ.get(STATE_DIR_ENV_VAR)
            if configured:
                root = configured
            else:
                claude_root = pathlib.Path(
                    os.environ.get(
                        "CLAUDE_CONFIG_DIR",
                        pathlib.Path.home() / ".claude",
                    )
                )
                root = claude_root / "state" / "code-review-claude"
        self.root = pathlib.Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)

    def _safe_id(self, lineage_id):
        if not lineage_id or not SESSION_ID_PATTERN.fullmatch(lineage_id):
            raise BridgeError("Invalid review session id")
        return lineage_id

    def state_path(self, lineage_id):
        return self.root / f"{self._safe_id(lineage_id)}.json"

    def log_path(self, lineage_id):
        return self.root / f"{self._safe_id(lineage_id)}.log"

    def write(self, lineage_id, state):
        destination = self.state_path(lineage_id)
        temporary = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=self.root,
            prefix=f".{lineage_id}.",
            suffix=".tmp",
            delete=False,
        )
        try:
            with temporary:
                json.dump(state, temporary, ensure_ascii=False, sort_keys=True)
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.chmod(temporary.name, 0o600)
            os.replace(temporary.name, destination)
        except Exception:
            pathlib.Path(temporary.name).unlink(missing_ok=True)
            raise

    def read(self, lineage_id):
        try:
            state = json.loads(
                self.state_path(lineage_id).read_text(encoding="utf-8")
            )
        except FileNotFoundError as error:
            raise BridgeError(
                f"Unknown review session: {lineage_id}"
            ) from error
        except (OSError, json.JSONDecodeError) as error:
            raise BridgeError(
                f"Unreadable review session: {lineage_id}"
            ) from error
        if state.get("version") != SESSION_STATE_VERSION:
            raise BridgeError(
                f"Unsupported review session version: {state.get('version')}"
            )
        return state

    def append_log(self, lineage_id, header, stdout, stderr):
        path = self.log_path(lineage_id)
        existed = path.exists()
        with path.open("a", encoding="utf-8") as log_file:
            log_file.write(f"===== {header} =====\n")
            log_file.write("----- stdout -----\n")
            log_file.write(stdout or "")
            if stdout and not stdout.endswith("\n"):
                log_file.write("\n")
            log_file.write("----- stderr -----\n")
            log_file.write(stderr or "")
            if stderr and not stderr.endswith("\n"):
                log_file.write("\n")
        if not existed:
            os.chmod(path, 0o600)
        return path


def run_command_capture(command, cwd, timeout_seconds, environment=None):
    """`(exit code, stdout)` of a short read-only command, or `None` if it could not be run.

    Every caller here is gathering context for a prompt, so a command that is missing, crashes, or
    hangs is a slot to fill differently — never a failed review. Output that is not valid UTF-8
    is decoded with replacements for the same reason: an optional tool's bad byte must not take
    the review down with it.
    """
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return completed.returncode, completed.stdout


def resolve_main_checkout(cwd):
    """Absolute path of the checkout whose graph and object database this worktree shares.

    A child reviews from its own worktree, and a worktree's graph is empty; the common git
    directory names the checkout that holds the real one.
    """
    answer = run_command_capture(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd,
        GIT_TIMEOUT_SECONDS,
    )
    if not answer or answer[0] != 0 or not answer[1].strip():
        return None
    return str(pathlib.Path(answer[1].strip()).parent)


def resolve_review_branch(cwd):
    """The branch under review, or its commit when the worktree's HEAD is detached."""
    answer = run_command_capture(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd, GIT_TIMEOUT_SECONDS
    )
    if not answer or answer[0] != 0 or not answer[1].strip():
        return None
    branch = answer[1].strip()
    if branch != "HEAD":
        return branch
    detached = run_command_capture(["git", "rev-parse", "HEAD"], cwd, GIT_TIMEOUT_SECONDS)
    if not detached or detached[0] != 0 or not detached[1].strip():
        return None
    return detached[1].strip()


def graph_analysis(checkout, range_spec, environment=None):
    """The graph CLI's brief risk report for the range, or `None` when it has no answer.

    The `--repo` flag is the only supported way to point the CLI at a checkout: redirection
    through `CRG_DATA_DIR` / `CRG_REPO_ROOT` was measured to answer with a silent zero-risk score,
    so this call sets neither.
    """
    environment = os.environ if environment is None else environment
    executable = shutil.which(GRAPH_CLI, path=environment.get("PATH"))
    if not executable:
        return None
    environment = {
        name: value
        for name, value in environment.items()
        if name not in GRAPH_REDIRECT_ENV_VARS
    }
    answer = run_command_capture(
        [
            executable, "detect-changes",
            "--base", range_spec,
            "--brief",
            "--repo", checkout,
        ],
        checkout,
        GRAPH_TIMEOUT_SECONDS,
        environment,
    )
    if not answer or answer[0] != 0 or not answer[1].strip():
        return None
    return answer[1].strip()


def git_change_summary(worktree, base):
    """Diff stat, changed files, and untracked files since `base`, or `None` if git could not say.

    The review runs before the child commits, so the range that matters ends at the working tree
    rather than at the branch tip: `git diff <base>` spans both, and the untracked files it does
    not list are appended in git's own porcelain spelling.
    """
    stat = run_command_capture(
        ["git", "diff", "--stat", base], worktree, GIT_TIMEOUT_SECONDS
    )
    names = run_command_capture(
        ["git", "diff", "--name-status", base], worktree, GIT_TIMEOUT_SECONDS
    )
    untracked = run_command_capture(
        ["git", "ls-files", "--others", "--exclude-standard"],
        worktree,
        GIT_TIMEOUT_SECONDS,
    )
    if not stat or stat[0] != 0 or not names or names[0] != 0:
        return None
    listed = names[1].strip()
    if untracked and untracked[0] == 0 and untracked[1].strip():
        new_files = "\n".join(
            f"??\t{path}" for path in untracked[1].strip().splitlines()
        )
        listed = "\n".join(part for part in (listed, new_files) if part)
    return "\n\n".join(part for part in (stat[1].strip(), listed) if part)


def has_pending_changes(worktree):
    """True when the working tree carries changes no commit — and so no graph — has seen yet."""
    status = run_command_capture(
        ["git", "status", "--porcelain"], worktree, GIT_TIMEOUT_SECONDS
    )
    if not status or status[0] != 0:
        return False
    return bool(status[1].strip())


def has_changed_functions(analysis):
    """True when the brief report counts at least one changed function."""
    match = CHANGED_FUNCTIONS_PATTERN.search(analysis or "")
    return bool(match) and int(match.group(1)) > 0


def build_scoped_context(checkout, worktree, base, branch, environment=None):
    """The round-one prompt's change-analysis block, or `None` when the range is unknown.

    A worktree shares the object database with its checkout, so committed work needs no checkout
    of its own: the graph is queried where it lives. Work that is not committed yet is invisible
    to any graph, so a dirty working tree is answered by git alone.
    """
    if not checkout or not worktree or not base or not branch:
        return None
    range_spec = f"{base}...{branch}"
    summary = git_change_summary(worktree, base)
    pending = has_pending_changes(worktree)
    analysis = None if pending else graph_analysis(checkout, range_spec, environment)
    # A zero changed-function count against a diff that plainly changed something is a stale or
    # empty graph, and a silent zero-risk score is worse than no score at all.
    if analysis and (has_changed_functions(analysis) or not summary):
        return (
            f"Change analysis for {range_spec}, computed before this review started:\n\n"
            f"{analysis}"
        )
    if not summary:
        return None
    reason = (
        "The working tree carries changes no commit holds yet, so no graph can have seen them"
        if pending
        else "The knowledge graph had no usable answer for this range"
    )
    return (
        f"Change analysis for everything in this worktree since {base}, computed before this "
        f"review started. {reason}, so this is git's own summary of it:\n\n{summary}"
    )


def build_verification_block(verification):
    """What the author already ran, and the instruction not to run it all again."""
    recorded = (verification or "").strip()
    if recorded:
        stated = f"The author recorded this verification of the change:\n\n{recorded}"
    else:
        stated = "The author recorded no verification of the change."
    return (
        f"{stated}\n\nRe-run only the tests the diff touches, rather than the full suite: "
        "confirming the whole tree is green a second time is the caller's job, not this review's."
    )


# The whole review request, stated here rather than delegated to a skill name:
# a headless reviewer that names a skill resolves that name a second time, in a
# session nobody is watching. Same contract as the `rounds` block in
# dispatch/templates/shapes.toml, which the reviewed child carries in its first
# turn; the two copies are the two ends of one review.
ROUNDS_CONTRACT = (
    "Rounds contract. Classify each finding on two axes: standards — style, "
    "naming, convention, anything that leaves behaviour intact — and spec — "
    "correctness, security, deviation from the spec or ticket. The caller fixes "
    "the standards findings it accepts in one pass; spec findings that required "
    "fixes earn one re-review, scoped to exactly those fixes. A spec finding "
    "still open after that re-review, or a finding reopened once it was ruled "
    "on, ends the review: state the disagreement with both positions, which the "
    "caller escalates to its coordinator rather than opening another round."
)


VERDICT_INSTRUCTION = (
    "End your report with a final line in exactly this form, counting the findings you just "
    "reported and, on the spec axis, only those that require a fix rather than a ruling:\n\n"
    f"{VERDICT_LINE}\n\nThat line is read by machine to decide whether a re-review is permitted "
    "at all, and a report without it earns none, so it must be present and it must be last."
)


def build_review_prompt(target, scoped_context=None, verification=None):
    """The round-one prompt: the request, the scope, the author's verification, the contract."""
    sections = [
        f"Review {target} and report your findings. You are a headless reviewer "
        "with full local access and no pane a human can watch, so ask no "
        "questions. If the originating issue or spec cannot be fetched, treat it "
        "as no spec available: run the standards axis and report the spec axis "
        "as skipped. Report on the code as it stands; this is a review-only task."
    ]
    if scoped_context:
        sections.append(scoped_context)
    sections.append(build_verification_block(verification))
    sections.append(ROUNDS_CONTRACT)
    sections.append(VERDICT_INSTRUCTION)
    return "\n\n".join(sections)


def build_followup_prompt(target):
    return (
        f"Continue this review thread with the updated {target}. Compare it with "
        "the findings already in this thread, and say for each earlier finding "
        "whether it is now resolved. This is the one re-review the contract "
        "allows, so scope it to those fixes. Ask no questions. Report on the "
        f"code as it stands; this is a review-only task.\n\n{ROUNDS_CONTRACT}"
    )


def build_command(binary, prompt, model, effort, resume_session_id):
    """The exact argv handed to the headless Claude, in a stable order."""
    command = [
        binary,
        "-p",
        "--output-format",
        "json",
        "--permission-mode",
        PERMISSION_MODE,
    ]
    if model:
        command.extend(["--model", model])
    if effort:
        command.extend(["--effort", effort])
    if resume_session_id:
        command.extend(["-r", resume_session_id])
    command.append(prompt)
    return command


def parse_claude_json(stdout):
    """The single JSON object `--output-format json` prints, or a clear error."""
    text = (stdout or "").strip()
    if not text:
        raise BridgeError("Headless Claude produced no output")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise BridgeError(f"Headless Claude output is not JSON: {error}") from error
    if not isinstance(payload, dict):
        raise BridgeError("Headless Claude output is not a JSON object")
    session_id = payload.get("session_id") or ""
    if not session_id:
        raise BridgeError("Headless Claude output carries no session_id")
    denials = payload.get("permission_denials") or []
    if not isinstance(denials, list):
        denials = [denials]
    return ClaudeResult(
        session_id=session_id,
        result=payload.get("result") or "",
        is_error=bool(payload.get("is_error")),
        subtype=payload.get("subtype") or "",
        permission_denials=denials,
        payload=payload,
    )


def run_claude(command, cwd, timeout_seconds):
    hook = launch_hook.load_hook(cwd)
    # The headless reviewer owns no window, so the hook is told which working
    # directory launched and nothing else.
    hook.run(cwd)
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=child_session_env(hook),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout or ""
        stderr = error.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", "replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        return None, stdout, stderr, error
    except OSError as error:
        raise BridgeError(f"Cannot launch headless Claude: {error}") from error
    return completed.returncode, completed.stdout, completed.stderr, None


def new_state(lineage_id, args, log_path):
    now = time.time()
    return {
        "version": SESSION_STATE_VERSION,
        "lineageId": lineage_id,
        "sessionId": lineage_id,
        "target": args.target,
        "cwd": str(pathlib.Path(args.cwd).resolve()),
        "model": args.model,
        "effort": args.effort,
        "permissionMode": PERMISSION_MODE,
        "logFile": str(log_path),
        "rounds": 0,
        "createdAt": now,
        "updatedAt": now,
    }


def apply_session_model_choice(args, state):
    """Reconcile a follow-up's model/effort with the ones the lineage carries.

    A follow-up that names neither inherits whatever round one pinned, so the
    whole lineage keeps one model and one effort; naming either repins the
    lineage from this round on.
    """
    if args.model:
        state["model"] = args.model
    else:
        args.model = state.get("model")
    if args.effort:
        state["effort"] = args.effort
    else:
        args.effort = state.get("effort")


def read_spec_finding_count(report):
    """The count of spec findings requiring a fix on the report's verdict line, or `None`.

    `None` means the report carried no readable verdict line at all, which is a different thing
    from a verdict that counted zero.
    """
    matches = VERDICT_PATTERN.findall(report or "")
    if not matches:
        return None
    return int(matches[-1])


def refuse_second_pass(state):
    """Why this lineage may not run another pass, or `None` when it may.

    The permission is evidence, not the absence of a refusal: a second pass needs a round one
    that said, in the line the prompt asked for, that a spec finding required a fix. A round one
    that said nothing readable is not that evidence, so it does not earn one either.
    """
    if state.get("rounds", 0) >= MAX_ROUNDS:
        return (
            "The contract allows one re-review and this lineage has had it; a finding still open "
            "after it ends the review and goes to the coordinator instead."
        )
    requiring_fix = state.get("specFindingsRequiringFix")
    if requiring_fix is None:
        return (
            "Round one printed no readable REVIEW VERDICT line, so nothing on record says a spec "
            "finding required a fix; the review ends on the round already in hand."
        )
    if requiring_fix == 0:
        return (
            "Round one reported no spec finding requiring a fix, and only such a finding earns a "
            "second pass; standards findings are fixed without re-review."
        )
    return None


def result_usage(payload):
    """The four disjoint counters this round billed, or `None` when the result reported none.

    All four or nothing: a `usage` object missing one of them is not a partial answer, it is a
    shape this bridge does not recognise, and billing a review for three of its four counters
    would understate it without saying so.
    """
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    counters = {}
    for name, field in USAGE_FIELDS.items():
        value = usage.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return None
        counters[name] = value
    return counters


def resolved_model(payload):
    """The model id this round actually ran on, or `None` when the result named no single one.

    The result's per-model breakdown is keyed by the model that was billed, so an alias like
    `opus` is resolved to the id behind it. More than one key is a round that ran on more than
    one model, which no single id describes. Either way the alias never stands in for the
    answer: it is already on the row, in the lane, and writing it into the model field would
    dress a guess up as a measurement.
    """
    billed = payload.get("modelUsage")
    if isinstance(billed, dict) and len(billed) == 1:
        return next(iter(billed))
    return None


def fold_usage(state, counters):
    """Fold this round's counters into the review's running total, and answer with the total.

    `None` means this review's total is not knowable: a round that reported no usage leaves the
    sum short by whatever it spent, and a total that quietly drops a round is worse than none at
    all. Cumulative by round so that a resumed review is one record rather than two to add up.
    """
    running = state.get("usage", {name: 0 for name in COUNTERS})
    if counters is None or running is None:
        state["usage"] = None
    else:
        state["usage"] = {name: running[name] + counters[name] for name in COUNTERS}
    return state["usage"]


def record_review_cost(review, state, parsed):
    """Leave this review's `session-cost` line, cumulative over the rounds it has had."""
    counters = fold_usage(state, result_usage(parsed.payload))
    detail = None
    if counters is None:
        detail = (
            f"round {state['rounds']} of this review reported no usage,"
            " so what the review spent cannot be totalled"
        )
    review.cost(parsed.session_id, resolved_model(parsed.payload), counters, detail)


def update_state_after_round(state, args, parsed, exit_code, duration_ms):
    state["sessionId"] = parsed.session_id
    state["target"] = args.target
    state["rounds"] = state.get("rounds", 0) + 1
    state["lastExitCode"] = exit_code
    state["lastSubtype"] = parsed.subtype
    state["lastIsError"] = parsed.is_error
    state["lastResult"] = parsed.result
    state["specFindingsRequiringFix"] = read_spec_finding_count(parsed.result)
    state["lastDurationMs"] = duration_ms
    state["permissionDenials"] = parsed.permission_denials
    state["updatedAt"] = time.time()


def run_bridge(args, review=None):
    if not pathlib.Path(args.cwd).is_dir():
        raise BridgeError(f"Working directory does not exist: {args.cwd}")
    binary = resolve_claude_binary(args.claude_binary)
    store = SessionStore(args.state_dir)

    if args.resume_session:
        state = store.read(args.resume_session)
        refusal = refuse_second_pass(state)
        if refusal:
            print(json.dumps(
                {
                    "status": "refused",
                    "reason": refusal,
                    "lineageId": state["lineageId"],
                    "sessionId": state.get("sessionId"),
                    "resumed": False,
                    "round": state.get("rounds", 0),
                    "specFindingsRequiringFix": state.get("specFindingsRequiringFix"),
                    "stateFile": str(store.state_path(state["lineageId"])),
                    "logFile": str(store.log_path(state["lineageId"])),
                    "findings": "",
                },
                ensure_ascii=False,
            ))
            # A refusal is the gate working, not a review that failed: the caller acts on the
            # round already in hand rather than retrying this call.
            return 0
        apply_session_model_choice(args, state)
        lineage_id = state["lineageId"]
        resume_session_id = state.get("sessionId") or lineage_id
        prompt = build_followup_prompt(args.target)
        resumed = True
    else:
        state = None
        lineage_id = None
        resume_session_id = None
        scoped_context = None
        if args.base:
            scoped_context = build_scoped_context(
                resolve_main_checkout(args.cwd),
                args.cwd,
                args.base,
                resolve_review_branch(args.cwd),
            )
        prompt = build_review_prompt(args.target, scoped_context, args.verification)
        resumed = False

    command = build_command(
        binary,
        prompt,
        args.model,
        args.effort,
        resume_session_id,
    )

    started = time.monotonic()
    exit_code, stdout, stderr, timeout_error = run_claude(
        command, args.cwd, args.timeout
    )
    duration_ms = int((time.monotonic() - started) * 1000)
    header = (
        f"round {(state or {}).get('rounds', 0) + 1} "
        f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} argv={json.dumps(command)}"
    )

    if timeout_error is not None:
        # A lineage without a session id has nothing to resume, so a timed-out
        # first round is logged under a temporary handle and reported as failed.
        store.append_log(lineage_id or "timed-out", header, stdout, stderr)
        raise BridgeError(
            f"Headless Claude timed out after {args.timeout}s"
            + (f" (lineage {lineage_id})" if lineage_id else "")
        )

    try:
        parsed = parse_claude_json(stdout)
    except BridgeError:
        store.append_log(lineage_id or "unparsed", header, stdout, stderr)
        raise

    if lineage_id is None:
        lineage_id = parsed.session_id
        state = new_state(lineage_id, args, store.log_path(lineage_id))
    store.append_log(lineage_id, header, stdout, stderr)
    update_state_after_round(state, args, parsed, exit_code, duration_ms)
    if review is not None:
        # After the round is folded into the state and before the report is printed, so the
        # figures cover every round this lineage has had.
        record_review_cost(review, state, parsed)
    store.write(lineage_id, state)

    output = {
        "status": "error" if parsed.is_error else "completed",
        "lineageId": lineage_id,
        "sessionId": parsed.session_id,
        "resumed": resumed,
        "round": state["rounds"],
        "model": args.model,
        "effort": args.effort,
        "exitCode": exit_code,
        "durationMs": duration_ms,
        "stateFile": str(store.state_path(lineage_id)),
        "logFile": str(store.log_path(lineage_id)),
        "permissionDenials": parsed.permission_denials,
        "findings": parsed.result,
    }
    print(json.dumps(output, ensure_ascii=False))
    if parsed.permission_denials:
        # Denials mean the reviewer was blocked from reading something, so the
        # report may be partial: say so where a caller cannot miss it.
        print(
            "Headless Claude reported "
            f"{len(parsed.permission_denials)} permission denial(s); "
            f"see {store.log_path(lineage_id)}",
            file=sys.stderr,
        )
    succeeded = (
        exit_code == 0
        and not parsed.is_error
        and bool(parsed.result)
        and not parsed.permission_denials
    )
    return 0 if succeeded else 1


def build_parser():
    parser = argparse.ArgumentParser(
        description="Run or resume a headless Claude code review."
    )
    parser.add_argument("target")
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument(
        "--model", help="Claude model for this review lineage (default: Claude config)"
    )
    parser.add_argument(
        "--effort",
        help="Effort level for this review lineage (default: Claude config)",
    )
    parser.add_argument(
        "--base",
        help="the commit this review's range starts at; round one opens with an analysis of"
             " <base>...<the worktree's branch>, and without it the prompt carries no scope",
    )
    parser.add_argument(
        "--verification",
        help="what the author ran to verify the change and how it came out; the reviewer is"
             " handed this and asked to re-run only the tests the diff touches",
    )
    parser.add_argument(
        "--resume-session",
        help="Lineage id from a previous round; resumes that Claude session",
    )
    parser.add_argument(
        "--machine-log",
        help="the run's machine log, where this review's `review` event pair is appended;"
             " the pair is written only when this and --ticket are both given",
    )
    parser.add_argument(
        "--ticket",
        help="the ticket this review is for, as the machine log spells it; the review's event"
             " pair is written only when this and --machine-log are both given",
    )
    parser.add_argument("--state-dir", help=argparse.SUPPRESS)
    parser.add_argument("--claude-binary", help=argparse.SUPPRESS)
    return parser


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    args = build_parser().parse_args(argv)
    # The pair straddles the whole call, so the `returned` line is written on every exit path
    # this bridge controls — a review that failed, timed out, or raised included. A row claiming
    # a review that is no longer running is worse than no row at all.
    review = ReviewEvent(args.machine_log, args.ticket, args.model)
    review.write("running")
    try:
        return run_bridge(args, review)
    except (BridgeError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1
    finally:
        # A review that never returned a readable result read no counters, and one that is left
        # out of the log entirely is the blind spot this event exists to close: the diagnosis
        # goes in where the figures cannot.
        if not review.costed:
            # A resumed round names the session it was about to spend in, so even the failed
            # round is attributable — and its transcript is kept out of the reviewed child's
            # figures. A first round that never answered named none, and none is written.
            review.cost(
                args.resume_session, None, None,
                "this review returned no result to read a cost from",
            )
        review.write("returned")


if __name__ == "__main__":
    raise SystemExit(main())
