#!/usr/bin/env python3
"""Fact-check one escalation in one fresh, read-only, budget-capped Claude session."""

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
DEFAULT_TIMEOUT_SECONDS = 900.0
# A path part starts with a letter, underscore, dot, tilde, or slash; contains at
# least one letter; and is not a bare version/number token matching v?[0-9]+([.-][0-9]+)*.
PATH_POINTER_VALUE = (
    r"(?<![\w.@+~/-])"
    r"(?!v?[0-9]+(?:[.-][0-9]+)*:)"
    r"(?=[A-Za-z_.~/])"
    r"(?=[^:\s]*[A-Za-z])"
    r"(?:/?(?:[\w.@+~-]+/)*[\w.@+~-]+):\d+"
)
PATH_POINTER = re.compile(PATH_POINTER_VALUE)
TICKET_POINTER = re.compile(r"(?<![\w#])#\d+")
ADR_POINTER = re.compile(r"\bADR-\d{4}\b")
STATUS = r"(?:held|contradicted|missing)"
POINTER_VALUE = rf"(?:{PATH_POINTER_VALUE}|#\d+|ADR-\d{{4}})"
UNCITED_LINE = re.compile(
    rf"uncited (?P<pointer>{POINTER_VALUE}) — {STATUS} — .+"
)


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


def checked_brief(text, escalation):
    brief = text.strip()
    if not brief:
        raise ValueError("session returned an empty brief")
    lines = brief.splitlines()
    if any(not line.strip() for line in lines):
        raise ValueError("brief contains an empty line")
    cited = pointers(escalation)
    cited_lines = set()
    for pointer in cited:
        matches = [
            index for index, line in enumerate(lines)
            if re.fullmatch(rf"{re.escape(pointer)} — {STATUS} — .+", line)
        ]
        if len(matches) != 1:
            raise ValueError(f"brief does not carry exactly one shaped line for {pointer}")
        cited_lines.add(matches[0])
    uncited = set()
    for index, line in enumerate(lines):
        if index in cited_lines:
            continue
        match = UNCITED_LINE.fullmatch(line)
        if not match:
            raise ValueError("brief contains a line outside the witness shape")
        pointer = match.group("pointer")
        if pointer in cited or pointer in uncited:
            raise ValueError(f"brief repeats the pointer {pointer}")
        uncited.add(pointer)
    return brief


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


def command(prompt, model, budget):
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
        "--output-format",
        "json",
    ]


def environment(account):
    current = dict(os.environ)
    if account:
        current[accounts.CONFIG_HOME_VARIABLE] = accounts.profile_directory(account)
    return current


def witness(args):
    started = time.monotonic()
    try:
        escalation = read_escalation(args.escalation)
        if not escalation.strip():
            return failed("escalation is empty", started)
        worktree = pathlib.Path(args.worktree).resolve(strict=True)
        if not worktree.is_dir():
            return failed(f"worktree {worktree} is not a directory", started)
        fault = run_plan.model_problem("witness model", args.model)
        if fault:
            return failed(fault, started)
        if args.budget_usd <= 0:
            return failed("budget-usd must be positive", started)
        if args.timeout_seconds <= 0:
            return failed("timeout-seconds must be positive", started)
        before = working_state(worktree)
        try:
            result = subprocess.run(
                command(
                    dispatch.render_witness_prompt(escalation), args.model, args.budget_usd
                ),
                cwd=worktree,
                env=environment(args.account),
                capture_output=True,
                text=True,
                timeout=args.timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return failed("witness session timed out", started)
        after = working_state(worktree)
        if after != before:
            return failed("witness session changed the worktree", started)
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
            brief = checked_brief(response.get("result", ""), escalation)
        except (TypeError, ValueError) as error:
            return failed(error, started, usage)
        document = {
            "brief": brief,
            "outcome": "checked",
            "reason": "",
            "duration_seconds": round(time.monotonic() - started, 3),
        }
        if isinstance(usage, dict):
            document["usage"] = usage
        return document
    except (OSError, subprocess.CalledProcessError, accounts.AccountsError) as error:
        return failed(error, started)


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--escalation", required=True, help="an escalation file, or - for stdin")
    parser.add_argument("--worktree", required=True, help="the child worktree to check")
    parser.add_argument("--model", required=True, help="the full Claude model ID")
    parser.add_argument("--budget-usd", required=True, type=float, help="the hard session budget")
    parser.add_argument(
        "--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS,
        help="how long the fresh session may run",
    )
    parser.add_argument("--account", help="the named Claude account to spend on")
    return parser.parse_args(argv)


def main(argv=None):
    print(json.dumps(witness(parse_args(argv))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
