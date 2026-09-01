#!/usr/bin/env python3
"""Run one fresh, read-only, budget-capped Witness operation."""

import argparse
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import time

import accounts
import run_plan

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "dispatch"))
import dispatch  # noqa: E402


CLAUDE = "claude"
HEADLESS_FLAG = "--print"
BUDGET_FLAG = "--max-budget-usd"
READ_ONLY_PERMISSION_MODE = "plan"
TRACKER_READ_TOOL = "Bash(gh issue view:*)"
DEFAULT_TIMEOUT_SECONDS = 900.0
# A path part starts with a letter, underscore, dot, tilde, or slash; contains at
# least one letter; and is not a bare version/number token matching v?[0-9]+([.-][0-9]+)*.
PATH_POINTER_BODY = (
    r"(?!v?[0-9]+(?:[.-][0-9]+)*:)"
    r"(?=[A-Za-z_.~/])"
    r"(?=[^:\s]*[A-Za-z])"
    r"(?:/?(?:[\w.@+~-]+/)*[\w.@+~-]+):\d+"
)
PATH_POINTER_VALUE = rf"(?<![\w.@+~/-]){PATH_POINTER_BODY}"
PATH_POINTER = re.compile(PATH_POINTER_VALUE)
TICKET_POINTER = re.compile(r"(?<![\w#])#\d+")
ADR_POINTER = re.compile(r"\bADR-\d{4}\b")
SCHEMA_POINTER_PATTERN = rf"^(?:{PATH_POINTER_BODY}|#\d+|ADR-\d{{4}})$"
POINTER_SCHEMA = {
    "type": "string", "minLength": 1, "pattern": SCHEMA_POINTER_PATTERN,
}
CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "cited": {
            "type": "array",
            "items": {"$ref": "#/$defs/finding"},
        },
        "uncited": {
            "type": "array",
            "items": {"$ref": "#/$defs/finding"},
        },
    },
    "required": ["cited", "uncited"],
    "additionalProperties": False,
    "$defs": {
        "finding": {
            "type": "object",
            "properties": {
                "pointer": POINTER_SCHEMA,
                "status": {"type": "string", "enum": ["held", "contradicted", "missing"]},
                "reason": {"type": "string", "minLength": 1},
            },
            "required": ["pointer", "status", "reason"],
            "additionalProperties": False,
        },
    },
}
ASK_SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string", "minLength": 1},
                    "pointers": {
                        "type": "array",
                        "minItems": 1,
                        "items": POINTER_SCHEMA,
                    },
                },
                "required": ["claim", "pointers"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["claims"],
    "additionalProperties": False,
}
WAVE_TABLE = "wave-table.json"


def failed(reason, started, usage=None):
    document = {
        "brief": "",
        "outcome": "failed",
        "reason": str(reason).strip() or "witness failed",
        "duration_seconds": round(time.monotonic() - started, 3),
    }
    if isinstance(usage, dict):
        document["usage"] = usage
    return document


def read_escalation(source):
    if source == "-":
        return sys.stdin.read()
    return pathlib.Path(source).read_text(encoding="utf-8")


def pointers(escalation):
    found = []
    for pattern in (PATH_POINTER, TICKET_POINTER, ADR_POINTER):
        found.extend((match.start(), match.group(0)) for match in pattern.finditer(escalation))
    ordered = []
    for _, pointer in sorted(found):
        if pointer not in ordered:
            ordered.append(pointer)
    return ordered


def valid_pointer(pointer):
    patterns = (PATH_POINTER, TICKET_POINTER, ADR_POINTER)
    return (
        isinstance(pointer, str)
        and any(pattern.fullmatch(pointer) for pattern in patterns)
    )


def finding_line(value, prefix=""):
    if not isinstance(value, dict) or set(value) != {"pointer", "status", "reason"}:
        raise ValueError("structured finding has an invalid shape")
    pointer = value["pointer"]
    status = value["status"]
    reason = value["reason"]
    if not valid_pointer(pointer):
        raise ValueError(f"structured finding has an invalid pointer {pointer!r}")
    if status not in ("held", "contradicted", "missing"):
        raise ValueError(f"structured finding has an invalid status {status!r}")
    if not isinstance(reason, str) or not reason.strip() or "\n" in reason or "\r" in reason:
        raise ValueError("structured finding has an invalid reason")
    return f"{prefix}{pointer} — {status} — {reason.strip()}"


def structured_check_brief(value, escalation):
    if not isinstance(value, dict) or set(value) != {"cited", "uncited"}:
        raise ValueError("session returned invalid structured check output")
    cited = value["cited"]
    uncited = value["uncited"]
    if not isinstance(cited, list) or not isinstance(uncited, list):
        raise ValueError("session returned invalid structured check output")
    expected = pointers(escalation)
    if [item.get("pointer") if isinstance(item, dict) else None for item in cited] != expected:
        raise ValueError("structured check does not cover cited pointers once in citation order")
    lines = [finding_line(item) for item in cited]
    seen = set(expected)
    for item in uncited:
        pointer = item.get("pointer") if isinstance(item, dict) else None
        if pointer in seen:
            raise ValueError(f"structured check repeats the pointer {pointer}")
        lines.append(finding_line(item, "uncited "))
        seen.add(pointer)
    return "\n".join(lines)


def structured_ask_brief(value):
    if not isinstance(value, dict) or set(value) != {"claims"}:
        raise ValueError("session returned invalid structured ask output")
    claims = value["claims"]
    if not isinstance(claims, list) or not claims:
        raise ValueError("session returned an empty answer")
    lines = []
    seen_pointers = set()
    for value in claims:
        if not isinstance(value, dict) or set(value) != {"claim", "pointers"}:
            raise ValueError("structured claim has an invalid shape")
        claim = value["claim"]
        claim_pointers = value["pointers"]
        if not isinstance(claim, str) or not claim.strip() or "\n" in claim or "\r" in claim:
            raise ValueError("structured claim is empty or multiline")
        if not isinstance(claim_pointers, list) or not claim_pointers:
            raise ValueError("structured claim has no pointer")
        if any(not valid_pointer(pointer) for pointer in claim_pointers):
            raise ValueError("structured claim has an invalid pointer")
        if any(pointer in seen_pointers for pointer in claim_pointers):
            raise ValueError("structured claim repeats a pointer")
        seen_pointers.update(claim_pointers)
        lines.append(f"{claim.strip()} — {', '.join(claim_pointers)}")
    return "\n".join(lines)


def working_state(worktree):
    listed = subprocess.run(
        [
            "git", "-C", str(worktree), "status", "--porcelain=v1", "-z",
            "--untracked-files=all", "--no-renames",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    state = {}
    for record in listed.split("\0"):
        if not record:
            continue
        relative = record[3:]
        path = worktree / relative
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            digest = None
        state[relative] = (record[:2], digest)
    return state


def command(prompt, model, budget, schema):
    return [
        CLAUDE,
        HEADLESS_FLAG,
        prompt,
        "--model",
        model,
        BUDGET_FLAG,
        f"{budget:g}",
        "--permission-mode",
        READ_ONLY_PERMISSION_MODE,
        "--allowedTools",
        TRACKER_READ_TOOL,
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(schema, separators=(",", ":")),
    ]


def environment(account):
    current = dict(os.environ)
    if account:
        current[accounts.CONFIG_HOME_VARIABLE] = accounts.profile_directory(account)
    return current


def execute(prompt, worktree, model, budget, timeout, session_environment, schema, render, started):
    before = working_state(worktree)
    result = None
    failure = None
    try:
        result = subprocess.run(
            command(prompt, model, budget, schema),
            cwd=worktree,
            env=session_environment,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        failure = "witness session timed out"
    except OSError as error:
        failure = error
    try:
        after = working_state(worktree)
    except (OSError, subprocess.CalledProcessError) as error:
        return failed(error, started)
    if after != before:
        return failed("witness session changed the worktree", started)
    if failure is not None:
        return failed(failure, started)
    if result.returncode:
        detail = result.stderr.strip() or f"witness session exited {result.returncode}"
        return failed(detail, started)
    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        return failed(f"witness session returned invalid JSON: {error}", started)
    usage = response.get("usage") if isinstance(response, dict) else None
    if not isinstance(response, dict) or response.get("is_error"):
        return failed("witness session returned an error result", started, usage)
    try:
        brief = render(response.get("structured_output"))
    except (TypeError, ValueError) as error:
        return failed(error, started, usage)
    if not isinstance(brief, str) or not brief.strip():
        return failed("witness session returned an empty brief", started, usage)
    document = {
        "brief": brief,
        "outcome": "checked",
        "reason": "",
        "duration_seconds": round(time.monotonic() - started, 3),
    }
    if isinstance(usage, dict):
        document["usage"] = usage
    return document


def checked_context(worktree, model, budget, timeout):
    worktree = pathlib.Path(worktree).resolve(strict=True)
    if not worktree.is_dir():
        raise ValueError(f"worktree {worktree} is not a directory")
    fault = run_plan.model_problem("witness model", model)
    if fault:
        raise ValueError(fault)
    if budget <= 0:
        raise ValueError("budget-usd must be positive")
    if timeout <= 0:
        raise ValueError("timeout-seconds must be positive")
    return worktree


def check(args):
    started = time.monotonic()
    try:
        escalation = read_escalation(args.escalation)
        if not escalation.strip():
            return failed("escalation is empty", started)
        worktree = checked_context(
            args.worktree, args.model, args.budget_usd, args.timeout_seconds
        )
        return execute(
            dispatch.render_witness_prompt(escalation, operation="check"),
            worktree,
            args.model,
            args.budget_usd,
            args.timeout_seconds,
            environment(args.account),
            CHECK_SCHEMA,
            lambda value: structured_check_brief(value, escalation),
            started,
        )
    except (OSError, subprocess.CalledProcessError, accounts.AccountsError, ValueError) as error:
        return failed(error, started)


def ask(args):
    started = time.monotonic()
    try:
        if not args.question.strip():
            return failed("question is empty", started)
        run_dir = run_plan.resolve_run_dir(args.run)
        plan = run_plan.load(run_dir / WAVE_TABLE)
        ticket = plan.ticket(args.ticket)
        worktree = checked_context(
            dispatch.worktree_path(plan.run, ticket),
            plan.run.witness_model,
            plan.run.witness_budget_usd,
            args.timeout_seconds,
        )
        return execute(
            dispatch.render_witness_prompt(
                f"Ticket: #{ticket.id}\nQuestion: {args.question.strip()}", operation="ask"
            ),
            worktree,
            plan.run.witness_model,
            plan.run.witness_budget_usd,
            args.timeout_seconds,
            accounts.process_environment(ticket.binding),
            ASK_SCHEMA,
            structured_ask_brief,
            started,
        )
    except (
        OSError, subprocess.CalledProcessError, accounts.AccountsError, run_plan.RunPlanError,
        TypeError, ValueError,
    ) as error:
        return failed(error, started)


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    operations = parser.add_subparsers(dest="operation", required=True)
    check = operations.add_parser("check", help="fact-check one escalation")
    check.add_argument("--escalation", required=True, help="an escalation file, or - for stdin")
    check.add_argument("--worktree", required=True, help="the child worktree to check")
    check.add_argument("--model", required=True, help="the full Claude model ID")
    check.add_argument("--budget-usd", required=True, type=float, help="the hard session budget")
    check.add_argument(
        "--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS,
        help="how long the fresh session may run",
    )
    check.add_argument("--account", help="the named Claude account to spend on")
    ask_parser = operations.add_parser("ask", help="answer one factual coordinator question")
    ask_parser.add_argument("--run", required=True, help="the active run directory")
    ask_parser.add_argument("--ticket", required=True, help="the ticket number in the Run plan")
    ask_parser.add_argument("--question", required=True, help="the factual question to answer")
    ask_parser.add_argument(
        "--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS,
        help="how long the fresh session may run",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    operation = {"check": check, "ask": ask}[args.operation]
    print(json.dumps(operation(args)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
