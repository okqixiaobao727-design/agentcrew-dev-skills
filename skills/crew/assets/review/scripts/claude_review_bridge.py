#!/usr/bin/env python3

"""Headless Claude code-review channel for a Codex child.

The sibling `tui_review_bridge.py` runs the other direction (a Claude session
obtains a Codex TUI review) and can watch a real pane. There is no pane here:
`claude -p` prints one JSON object and exits, so this bridge keeps the raw
stdout/stderr in a log file and the lineage in a state file.

Round one starts a review lineage; round two passes the stored lineage id back
and the bridge resumes the same Claude session with `-r`, so the follow-up
still sees round one's findings.
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


class BridgeError(RuntimeError):
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


# The whole review request, stated here rather than delegated to a skill name:
# a headless reviewer that names a skill resolves that name a second time, in a
# session nobody is watching. Same contract as references/rounds.md, which the
# reviewed child carries; the two copies are the two ends of one review.
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


def build_review_prompt(target):
    return (
        f"Review {target} and report your findings. You are a headless reviewer "
        "with full local access and no pane a human can watch, so ask no "
        "questions. If the originating issue or spec cannot be fetched, treat it "
        "as no spec available: run the standards axis and report the spec axis "
        "as skipped. Report on the code as it stands; this is a review-only "
        f"task.\n\n{ROUNDS_CONTRACT}"
    )


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


def update_state_after_round(state, args, parsed, exit_code, duration_ms):
    state["sessionId"] = parsed.session_id
    state["target"] = args.target
    state["rounds"] = state.get("rounds", 0) + 1
    state["lastExitCode"] = exit_code
    state["lastSubtype"] = parsed.subtype
    state["lastIsError"] = parsed.is_error
    state["lastResult"] = parsed.result
    state["lastDurationMs"] = duration_ms
    state["permissionDenials"] = parsed.permission_denials
    state["updatedAt"] = time.time()


def run_bridge(args):
    if not pathlib.Path(args.cwd).is_dir():
        raise BridgeError(f"Working directory does not exist: {args.cwd}")
    binary = resolve_claude_binary(args.claude_binary)
    store = SessionStore(args.state_dir)

    if args.resume_session:
        state = store.read(args.resume_session)
        apply_session_model_choice(args, state)
        lineage_id = state["lineageId"]
        resume_session_id = state.get("sessionId") or lineage_id
        prompt = build_followup_prompt(args.target)
        resumed = True
    else:
        state = None
        lineage_id = None
        resume_session_id = None
        prompt = build_review_prompt(args.target)
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
        "--resume-session",
        help="Lineage id from a previous round; resumes that Claude session",
    )
    parser.add_argument("--state-dir", help=argparse.SUPPRESS)
    parser.add_argument("--claude-binary", help=argparse.SUPPRESS)
    return parser


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    args = build_parser().parse_args(argv)
    try:
        return run_bridge(args)
    except (BridgeError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
