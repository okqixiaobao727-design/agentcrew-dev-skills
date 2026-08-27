#!/usr/bin/env python3
"""Bound file-reading tools in a coordinator or manual advisor session."""

import argparse
import json
import pathlib
import re
import shlex
import sys


READ_TOOL = "Read"
SEARCH_TOOLS = frozenset(("Grep", "Glob"))
SHELL_READ_COMMANDS = frozenset(("cat", "sed", "head", "tail", "grep", "rg", "find", "ls"))
SHELL_SEPARATORS = frozenset((";", ";;", "&&", "||", "|", "&", "(", ")", "{", "}"))
SHELL_PREFIXES = frozenset((
    "!", "builtin", "command", "do", "elif", "else", "env", "for", "if", "nohup",
    "sudo", "then", "time", "until", "while", "xargs",
))
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")
MAX_LINES = 80
DENIAL_REASON = (
    "Blocked: coordinator file reads are limited to one bounded Read for checking the "
    "escalation against its witness brief. Use an explicit offset and a limit of at most 80 lines."
)


def read_is_bounded(tool_input):
    """Whether one Read names the explicit bounded range the Contract permits."""
    offset = tool_input.get("offset")
    limit = tool_input.get("limit")
    return (
        isinstance(offset, int)
        and not isinstance(offset, bool)
        and isinstance(limit, int)
        and not isinstance(limit, bool)
        and limit <= MAX_LINES
    )


def path_is_in_crew_dir(payload, value, crew_dir):
    """Whether one path names the crew skill directory or a descendant, resolved by location."""
    if not isinstance(value, str) or not value:
        return False
    path = pathlib.Path(value)
    if not path.is_absolute():
        cwd = payload.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            return False
        path = pathlib.Path(cwd) / path
    try:
        path.resolve().relative_to(crew_dir.resolve())
    except (OSError, ValueError):
        return False
    return True


def read_is_in_crew_dir(payload, tool_input, crew_dir):
    """Whether one Read targets the crew skill itself, compared by resolved location."""
    return path_is_in_crew_dir(payload, tool_input.get("file_path"), crew_dir)


def search_is_in_crew_dir(payload, tool_input, crew_dir):
    """Whether one Grep or Glob confines its search root to the crew skill directory."""
    root = tool_input.get("path")
    if root is None:
        root = payload.get("cwd")
    return path_is_in_crew_dir(payload, root, crew_dir)


def mark_unquoted_newlines(command):
    """Turn shell newlines into separators without changing newlines inside quoted data."""
    marked = []
    quote = None
    escaped = False
    for character in command:
        if escaped:
            marked.append(character)
            escaped = False
            continue
        if character == "\\" and quote != "'":
            marked.append(character)
            escaped = True
            continue
        if character in ("'", '"', "`"):
            if quote == character:
                quote = None
            elif quote is None:
                quote = character
            marked.append(character)
            continue
        marked.append(";" if character == "\n" and quote is None else character)
    return "".join(marked)


def shell_invocations(command):
    """Return the simple command words in one shell string, split at control operators."""
    if not isinstance(command, str):
        return []
    lexer = shlex.shlex(
        mark_unquoted_newlines(command), posix=True, punctuation_chars="();&|<>{}"
    )
    lexer.whitespace_split = True
    try:
        words = list(lexer)
    except ValueError:
        return None
    segments = []
    segment = []
    for word in words:
        if word in SHELL_SEPARATORS:
            if segment:
                segments.append(segment)
                segment = []
            continue
        segment.append(word)
    if segment:
        segments.append(segment)
    invocations = []
    for segment in segments:
        index = 0
        while index < len(segment) and (
            ASSIGNMENT.fullmatch(segment[index])
            or segment[index].startswith("-")
            or segment[index] in SHELL_PREFIXES
        ):
            index += 1
        if index < len(segment):
            invocations.append((pathlib.PurePath(segment[index]).name, segment[index + 1:]))
    return invocations


def all_shell_invocations(command):
    """Return simple invocations from a shell command and its backtick or shell-c bodies."""
    invocations = shell_invocations(command)
    if invocations is None:
        return None
    nested = re.findall(r"`([^`]*)`", command, flags=re.DOTALL)
    for name, arguments in invocations:
        if name not in ("bash", "sh", "zsh") or "-c" not in arguments:
            continue
        index = arguments.index("-c")
        if index + 1 < len(arguments):
            nested.append(arguments[index + 1])
    combined = list(invocations)
    for body in nested:
        found = all_shell_invocations(body)
        if found is None:
            return None
        combined.extend(found)
    return combined


def invocation_reads_file(name, arguments):
    """Return whether one parsed command invocation reads file or directory contents."""
    if name == "ls" and ("-d" in arguments or "--directory" in arguments):
        return False
    return name in SHELL_READ_COMMANDS


def bash_reads_file(command):
    """Return whether Bash contains one of the shell read-command shapes in the Contract."""
    invocations = all_shell_invocations(command)
    return invocations is None or any(
        invocation_reads_file(name, arguments) for name, arguments in invocations
    )


def read_targets(name, arguments, cwd):
    """Return the file operands read by one named shell command, conservatively."""
    if name == "cat":
        return [word for word in arguments if word != "-" and not word.startswith("-")]
    if name in ("head", "tail"):
        values = []
        skip = False
        for word in arguments:
            if skip:
                skip = False
                continue
            if word in ("-c", "--bytes", "-n", "--lines"):
                skip = True
            elif not word.startswith("-"):
                values.append(word)
        return values
    if name == "sed":
        values = []
        expression_seen = False
        skip_expression = False
        for word in arguments:
            if skip_expression:
                skip_expression = False
                expression_seen = True
            elif word in ("-e", "--expression"):
                skip_expression = True
            elif word in ("-f", "--file"):
                skip_expression = True
            elif word.startswith("-"):
                continue
            elif not expression_seen:
                expression_seen = True
            else:
                values.append(word)
        return values
    if name in ("grep", "rg"):
        values = [word for word in arguments if not word.startswith("-")]
        return values[1:]
    if name == "find":
        roots = []
        for word in arguments:
            if word.startswith("-"):
                break
            roots.append(word)
        return roots or [cwd]
    if name == "ls":
        if "-d" in arguments or "--directory" in arguments:
            return []
        values = [word for word in arguments if not word.startswith("-")]
        return values or [cwd]
    return []


def bash_reads_only_in_crew_dir(payload, command, crew_dir):
    """Return whether every shell read target is inside the crew skill directory."""
    invocations = all_shell_invocations(command)
    if not invocations:
        return False
    cwd = payload.get("cwd")
    targets = []
    for name, arguments in invocations:
        if invocation_reads_file(name, arguments):
            targets.extend(read_targets(name, arguments, cwd))
    return bool(targets) and all(
        path_is_in_crew_dir(payload, target, crew_dir) for target in targets
    )


def emit_denial():
    """Print Claude Code's one-line PreToolUse denial and return exit status zero."""
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": DENIAL_REASON,
        }
    }))
    return 0


def run_hook(args):
    """Apply the PreToolUse boundary to one JSON object from stdin."""
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return 0
    if not isinstance(payload, dict):
        return 0
    if args.session_id is not None and payload.get("session_id") != args.session_id:
        return 0
    tool_input = payload.get("tool_input", {})
    if payload.get("tool_name") in SEARCH_TOOLS:
        if isinstance(tool_input, dict) and search_is_in_crew_dir(
            payload, tool_input, args.crew_dir
        ):
            return 0
        return emit_denial()
    if payload.get("tool_name") == "Bash" and isinstance(tool_input, dict):
        if bash_reads_file(tool_input.get("command")):
            if bash_reads_only_in_crew_dir(payload, tool_input.get("command"), args.crew_dir):
                return 0
            return emit_denial()
    if payload.get("tool_name") == READ_TOOL and isinstance(tool_input, dict):
        if read_is_bounded(tool_input) or read_is_in_crew_dir(payload, tool_input, args.crew_dir):
            return 0
        return emit_denial()
    return 0


def parser():
    command = argparse.ArgumentParser(description=__doc__)
    subcommands = command.add_subparsers(dest="command", required=True)
    hook = subcommands.add_parser("hook", help="apply the PreToolUse read boundary")
    hook.add_argument("--crew-dir", type=pathlib.Path, required=True)
    hook.add_argument("--session-id", help="the one coordinator or advisor session to bound")
    hook.add_argument(
        "--owner-log",
        help="identify the run whose atomic machine-log install owns this registration",
    )
    hook.set_defaults(handler=run_hook)
    return command


def main(argv=None):
    args = parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
