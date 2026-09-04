#!/usr/bin/env python3
"""Run one fresh, read-only, budget-capped Witness operation."""

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import time

import accounts
import machine_log
import run_plan

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "dispatch"))
import dispatch  # noqa: E402


CLAUDE = "claude"
HEADLESS_FLAG = "--print"
BUDGET_FLAG = "--max-budget-usd"
READ_ONLY_PERMISSION_MODE = "plan"
TRACKER_READ_TOOL = "Bash(gh issue view:*)"
# A path part starts with a letter, underscore, dot, tilde, or slash; contains at
# least one ASCII letter; and is not a bare version/number token matching
# v?[0-9]+([.-][0-9]+)*. ASCII boundary classes deliberately let a pointer touch CJK prose.
PATH_BODY = (
    r"(?!v?[0-9]+(?:[.-][0-9]+)*(?=:))"
    r"(?=[A-Za-z_.~/])"
    r"(?=[^:\s]*[A-Za-z])"
    r"(?:/?(?:[A-Za-z0-9_.@+~-]+/)*[A-Za-z0-9_.@+~-]+)"
)
PATH_POINTER_BODY = rf"{PATH_BODY}:\d+"
PATH_POINTER_VALUE = rf"(?<![A-Za-z0-9_.@+~/-])(?P<elided>…)?(?P<path>{PATH_BODY}):(?P<line>\d+)"
PATH_POINTER = re.compile(PATH_POINTER_VALUE)
NORMALISED_PATH_POINTER = re.compile(PATH_POINTER_BODY)
LINE_CONTINUATION = re.compile(r"(?<![A-Za-z0-9_.@+~/-])(?:…)?:(?P<line>\d+)")
TICKET_POINTER = re.compile(r"(?<![A-Za-z0-9_#])#\d+")
ADR_POINTER = re.compile(r"(?<![A-Za-z0-9_])ADR-\d{4}(?![A-Za-z0-9_])")
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


class Pointer(str):
    """One normalised pointer value returned by the pointer grammar."""


def failed(reason, started, usage=None, coverage=None):
    document = {
        "brief": "",
        "outcome": "failed",
        "reason": str(reason).strip() or "witness failed",
        "duration_seconds": round(time.monotonic() - started, 3),
    }
    if coverage is not None:
        document["covered_count"], document["uncovered_count"] = coverage
    if isinstance(usage, dict):
        document["usage"] = usage
    return document


def read_escalation(source):
    if source == "-":
        return sys.stdin.read()
    return pathlib.Path(source).read_text(encoding="utf-8")


def _normalised_file(raw_path, worktree_root):
    root = pathlib.Path(worktree_root).expanduser().resolve()
    path = pathlib.Path(raw_path).expanduser()
    if not path.is_absolute():
        path = root / path
    identity = path.resolve()
    try:
        display = identity.relative_to(root).as_posix()
    except ValueError:
        display = identity.as_posix()
    return identity, display


def pointers(text: str, worktree_root: str | pathlib.Path) -> list[Pointer]:
    """Return cited pointers once, in order, with file spellings normalised."""
    found = []
    for pattern in (PATH_POINTER, LINE_CONTINUATION, TICKET_POINTER, ADR_POINTER):
        found.extend((match.start(), pattern, match) for match in pattern.finditer(text))

    ordered = []
    seen = set()
    prior_files = []
    for _, pattern, match in sorted(found, key=lambda item: item[0]):
        if pattern is TICKET_POINTER:
            display = match.group(0)
            identity = ("ticket", display)
        elif pattern is ADR_POINTER:
            display = match.group(0)
            identity = ("adr", display)
        else:
            line = int(match.group("line"))
            if pattern is LINE_CONTINUATION:
                if not prior_files:
                    continue
                file_identity, display_path = prior_files[-1]
            else:
                raw_path = match.group("path")
                if match.group("elided"):
                    matches = {
                        file_identity: display_path
                        for file_identity, display_path in prior_files
                        if display_path.endswith(raw_path)
                    }
                    if len(matches) == 1:
                        file_identity, display_path = next(iter(matches.items()))
                    else:
                        file_identity = ("unresolved", raw_path)
                        display_path = raw_path
                else:
                    file_identity, display_path = _normalised_file(raw_path, worktree_root)
            prior_files.append((file_identity, display_path))
            display = f"{display_path}:{line}"
            identity = ("file", file_identity, line)
        if identity in seen:
            continue
        seen.add(identity)
        ordered.append(Pointer(display))
    return ordered


def valid_pointer(pointer):
    patterns = (NORMALISED_PATH_POINTER, TICKET_POINTER, ADR_POINTER)
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


def _largest_ordered_subset(values):
    """Return the largest strictly increasing subset, favouring expected order on ties."""
    best_at_position = []
    for position, value in enumerate(values):
        best = (value,)
        for earlier_position, earlier in enumerate(values[:position]):
            if earlier >= value:
                continue
            candidate = best_at_position[earlier_position] + (value,)
            if len(candidate) > len(best) or (
                len(candidate) == len(best) and candidate < best
            ):
                best = candidate
        best_at_position.append(best)
    if not best_at_position:
        return set()
    largest = min(best_at_position, key=lambda candidate: (-len(candidate), candidate))
    return set(largest)


def structured_check_findings(value, expected):
    if not isinstance(value, dict) or set(value) != {"cited", "uncited"}:
        raise ValueError("session returned invalid structured check output")
    cited = value["cited"]
    uncited = value["uncited"]
    if not isinstance(cited, list) or not isinstance(uncited, list):
        raise ValueError("session returned invalid structured check output")
    expected = [str(pointer) for pointer in expected]
    expected_indexes = {pointer: index for index, pointer in enumerate(expected)}
    cited_findings = []
    for item in cited:
        line = finding_line(item)
        cited_findings.append((item["pointer"], line, item))
    uncited_findings = []
    for item in uncited:
        line = finding_line(item, "uncited ")
        uncited_findings.append((item["pointer"], line, item))
    occurrences = {pointer: 0 for pointer in expected}
    for pointer, _, _ in cited_findings + uncited_findings:
        if pointer in occurrences:
            occurrences[pointer] += 1

    candidate_indexes = [
        expected_indexes[pointer]
        for pointer, _, _ in cited_findings
        if pointer in expected_indexes and occurrences[pointer] == 1
    ]
    selected_indexes = _largest_ordered_subset(candidate_indexes)
    cited_by_index = {
        expected_indexes[pointer]: line
        for pointer, line, _ in cited_findings
        if pointer in expected_indexes and expected_indexes[pointer] in selected_indexes
    }
    lines = [cited_by_index[index] for index in sorted(selected_indexes)]

    missing = []
    structural_rejections = []
    for index, pointer in enumerate(expected):
        if occurrences[pointer] == 0:
            missing.append(pointer)
        elif occurrences[pointer] > 1:
            structural_rejections.append(
                f"structural rejection (repeated): {pointer}"
            )
        elif index not in selected_indexes:
            cited_once = any(item_pointer == pointer for item_pointer, _, _ in cited_findings)
            shape = "out of order" if cited_once else "uncited"
            structural_rejections.append(
                f"structural rejection ({shape}): {pointer}"
            )

    extra_cited = set()
    rendered_uncited = set()
    for pointer, _, item in cited_findings:
        if pointer in expected_indexes:
            continue
        if pointer not in extra_cited:
            structural_rejections.append(
                f"structural rejection (extra cited): {pointer}"
            )
            extra_cited.add(pointer)
        if pointer not in rendered_uncited:
            lines.append(finding_line(item, "uncited "))
            rendered_uncited.add(pointer)
    for pointer, line, _ in uncited_findings:
        if pointer not in expected_indexes and pointer not in rendered_uncited:
            lines.append(line)
            rendered_uncited.add(pointer)

    uncovered = [
        pointer for index, pointer in enumerate(expected) if index not in selected_indexes
    ]
    return lines, uncovered, missing, structural_rejections


def check_result(value, expected):
    findings, uncovered, missing, structural_rejections = structured_check_findings(
        value, expected
    )
    covered_count = len(expected) - len(uncovered)
    coverage = {
        "covered_count": covered_count,
        "uncovered_count": len(uncovered),
    }
    reason_parts = []
    if missing:
        reason_parts.append(f"uncovered pointers: {', '.join(missing)}")
    reason_parts.extend(structural_rejections)
    reason = "; ".join(reason_parts)
    if not covered_count and (uncovered or structural_rejections):
        return {"brief": "", "outcome": "failed", "reason": reason, **coverage}
    brief = "\n".join(findings)
    if not brief:
        return {
            "brief": "",
            "outcome": "failed",
            "reason": "witness matched none of the expected or uncited pointers",
            **coverage,
        }
    if uncovered or structural_rejections:
        return {
            "brief": brief,
            "outcome": "partial",
            "reason": reason,
            **coverage,
        }
    return {"brief": brief, "outcome": "checked", "reason": "", **coverage}


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


def execute(
    prompt, worktree, model, budget, timeout, session_environment, schema, render, started,
    failure_coverage=None,
):
    # The session is read-only by construction — a plan permission mode with one allowed Bash
    # form, which reads the tracker — so there is no worktree change of its own to guard against.
    # The worktree it reads is the escalating child's, live and still being worked in, and a
    # comparison here would report that child's ordinary edit as this session's failure (#196).
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
    if failure is not None:
        return failed(failure, started, coverage=failure_coverage)
    if result.returncode:
        detail = result.stderr.strip() or f"witness session exited {result.returncode}"
        return failed(detail, started, coverage=failure_coverage)
    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        return failed(
            f"witness session returned invalid JSON: {error}", started,
            coverage=failure_coverage,
        )
    usage = response.get("usage") if isinstance(response, dict) else None
    if not isinstance(response, dict) or response.get("is_error"):
        return failed(
            "witness session returned an error result", started, usage, failure_coverage
        )
    try:
        content = render(response.get("structured_output"))
    except (TypeError, ValueError) as error:
        return failed(error, started, usage, failure_coverage)
    if not isinstance(content, dict):
        return failed(
            "witness session returned an invalid result",
            started,
            usage,
            failure_coverage,
        )
    if content.get("outcome") != "failed" and not str(content.get("brief", "")).strip():
        return failed("witness session returned an empty brief", started, usage, failure_coverage)
    document = dict(content)
    document["duration_seconds"] = round(time.monotonic() - started, 3)
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
    fault = run_plan.witness_timeout_problem("timeout-seconds", timeout)
    if fault:
        raise ValueError(fault)
    return worktree


def usage_counters(document):
    """The document's token counters under the log's own names, or None where it counted none."""
    usage = document.get("usage")
    if not isinstance(usage, dict):
        return None
    counted = {
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cache_read_tokens": usage.get("cache_read_input_tokens"),
        "cache_creation_tokens": usage.get("cache_creation_input_tokens"),
    }
    if not all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0
        for value in counted.values()
    ):
        return None
    return {**counted, "total_tokens": sum(counted.values())}


def record(document, log, ticket, operation, model):
    """Record this operation's own Machine-log event; returns the document either way.

    The Witness holds the outcome, the reason, the duration, the brief and the token counters, so
    it writes them down itself rather than handing them out for a caller to transcribe — which is
    what left the coordinator-initiated `ask` out of the log and out of the run's cost rollup
    entirely (#196). An invocation with no run to record against records nothing and returns
    normally. A log that could not be written is a failure of the record and not of the operation:
    the document stands, and carries `record_error` so the failure is visible to a caller reading
    the document, whether that is the Driver or the coordinator itself.
    """
    if log is None or not ticket or not model:
        return document
    try:
        machine_log.record_witness(
            log,
            ticket=str(ticket),
            operation=operation,
            executor=run_plan.WITNESS_EXECUTOR,
            model=model,
            outcome=document["outcome"],
            reason=document.get("reason", ""),
            brief=document.get("brief", ""),
            duration_seconds=document["duration_seconds"],
            covered_count=document.get("covered_count", 0),
            uncovered_count=document.get("uncovered_count", 0),
            counters=usage_counters(document),
        )
    except (OSError, ValueError, KeyError, TypeError) as error:
        document["record_error"] = f"the witness event was not recorded: {error}"
        print(f"witness: {document['record_error']}", file=sys.stderr)
    return document


def check(args):
    started = time.monotonic()
    expected = []
    try:
        _, _, _, timeout = run_plan.witness_routing(
            args.model, args.budget_usd, args.timeout_seconds
        )
        escalation = read_escalation(args.escalation)
        if not escalation.strip():
            document = failed("escalation is empty", started, coverage=(0, 0))
            return record(document, args.log, args.ticket, "check", args.model)
        expected = pointers(escalation, args.worktree)
        worktree = checked_context(args.worktree, args.model, args.budget_usd, timeout)
        document = execute(
            dispatch.render_witness_prompt(
                escalation, operation="check", check_pointers=expected
            ),
            worktree,
            args.model,
            args.budget_usd,
            timeout,
            environment(args.account),
            CHECK_SCHEMA,
            lambda value: check_result(value, expected),
            started,
            failure_coverage=(0, len(expected)),
        )
    except (
        OSError, subprocess.CalledProcessError, accounts.AccountsError, run_plan.RunPlanError,
        ValueError,
    ) as error:
        document = failed(error, started, coverage=(0, len(expected)))
    return record(document, args.log, args.ticket, "check", args.model)


def ask(args):
    started = time.monotonic()
    log = None
    identifier = args.ticket
    model = None
    try:
        run_dir = run_plan.resolve_run_dir(args.run)
        # Derived rather than asked for: `ask` already names the run it belongs to, and a Machine
        # log passed beside it could name a different one.
        log = run_dir / machine_log.LOG_NAME
        plan = run_plan.load(run_dir / WAVE_TABLE)
        ticket = plan.ticket(args.ticket)
        identifier = ticket.id
        model = plan.run.witness_model
        # After the run is resolved rather than before it, so that the refusal is an event this
        # run recorded against its ticket, on the model it would have spent.
        if not args.question.strip():
            document = failed("question is empty", started)
            return record(document, log, identifier, "ask", model)
        _, _, _, timeout = run_plan.witness_routing(
            plan.run.witness_model, plan.run.witness_budget_usd, args.timeout_seconds
        )
        worktree = checked_context(
            dispatch.worktree_path(plan.run, ticket),
            plan.run.witness_model,
            plan.run.witness_budget_usd,
            timeout,
        )
        document = execute(
            dispatch.render_witness_prompt(
                f"Ticket: #{ticket.id}\nQuestion: {args.question.strip()}", operation="ask"
            ),
            worktree,
            plan.run.witness_model,
            plan.run.witness_budget_usd,
            timeout,
            accounts.process_environment(ticket.binding),
            ASK_SCHEMA,
            lambda value: {
                "brief": structured_ask_brief(value), "outcome": "checked", "reason": "",
            },
            started,
        )
    except (
        OSError, subprocess.CalledProcessError, accounts.AccountsError, run_plan.RunPlanError,
        TypeError, ValueError,
    ) as error:
        document = failed(error, started)
    return record(document, log, identifier, "ask", model)


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    operations = parser.add_subparsers(dest="operation", required=True)
    check = operations.add_parser("check", help="fact-check one escalation")
    check.add_argument("--escalation", required=True, help="an escalation file, or - for stdin")
    check.add_argument("--worktree", required=True, help="the child worktree to check")
    check.add_argument("--model", required=True, help="the full Claude model ID")
    check.add_argument("--budget-usd", required=True, type=float, help="the hard session budget")
    check.add_argument(
        "--timeout-seconds", type=float,
        help="how long the fresh session may run, over the configured `[witness] timeout_seconds`",
    )
    check.add_argument("--account", help="the named Claude account to spend on")
    # A check with no run behind it — the manual, driver-less flow — has nothing to record
    # against, and records nothing rather than failing.
    check.add_argument("--log", help="the run's Machine log, to record this check in")
    check.add_argument("--ticket", help="the ticket this check is recorded against")
    ask_parser = operations.add_parser("ask", help="answer one factual coordinator question")
    ask_parser.add_argument("--run", required=True, help="the active run directory")
    ask_parser.add_argument("--ticket", required=True, help="the ticket number in the Run plan")
    ask_parser.add_argument("--question", required=True, help="the factual question to answer")
    ask_parser.add_argument(
        "--timeout-seconds", type=float,
        help="how long the fresh session may run, over the configured `[witness] timeout_seconds`",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.operation == "check" and bool(args.log) != bool(args.ticket):
        raise SystemExit("witness: check records against a --log and a --ticket, or neither")
    operation = {"check": check, "ask": ask}[args.operation]
    print(json.dumps(operation(args)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
