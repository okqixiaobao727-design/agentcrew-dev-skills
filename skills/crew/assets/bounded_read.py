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
SPEC_NAME = "spec.md"
REFERENCE_FILES_BEFORE_ADRS = (
    pathlib.Path("CONTEXT.md"),
    pathlib.Path("docs/glossary.md"),
)
DENIAL_REASON = (
    "Blocked: the coordinator may read judgment Markdown whole, but checks a source fact only "
    "at the escalation's pointer against its witness brief. Use Read with an explicit offset "
    "and a limit of at most 80 lines; searches and shell reads are hunts."
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


def path_is_below(path, root):
    """Whether one resolved path is below another resolved path."""
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def path_is_reference_entry(path, run_root):
    """Whether resolved target-repository Markdown belongs in this run's Reference index.

    The hook additionally admits the Crew skill's own resident documents; those are already in the
    coordinator's context, so this shared staging subset deliberately does not index them again.
    """
    repo_root = run_root.parent.parent
    return (
        (path.parent == run_root and path.name != SPEC_NAME)
        or any(path == repo_root / relative for relative in REFERENCE_FILES_BEFORE_ADRS)
        or path_is_below(path, repo_root / "docs" / "adr")
        or path.parent == repo_root / "docs" / "agents"
    )


def reference_index_paths(run_dir):
    """Existing physical files in the per-run Reference index's stable category order."""
    try:
        run_root = pathlib.Path(run_dir).resolve(strict=True)
    except (OSError, RuntimeError):
        return ()
    repo_root = run_root.parent.parent
    candidates = [
        *sorted(run_root.glob("*.md"), key=lambda path: path.name),
        *(repo_root / relative for relative in REFERENCE_FILES_BEFORE_ADRS),
        *sorted((repo_root / "docs" / "adr").rglob("*.md"), key=lambda path: str(path)),
        *sorted((repo_root / "docs" / "agents").glob("*.md"), key=lambda path: path.name),
    ]
    found = []
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if (
            resolved.suffix == ".md"
            and resolved.is_file()
            and path_is_reference_entry(resolved, run_root)
            and resolved not in found
        ):
            found.append(resolved)
    return tuple(found)


def read_is_judgment_markdown(payload, tool_input, run_dir, crew_dir):
    """Whether one Read targets maintainer-authored Markdown used for judgment."""
    value = tool_input.get("file_path")
    if not isinstance(value, str) or not value:
        return False
    path = pathlib.Path(value)
    if not path.is_absolute():
        cwd = payload.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            return False
        path = pathlib.Path(cwd) / path
    try:
        resolved = path.resolve(strict=True)
        run_root = run_dir.resolve(strict=True)
        crew_root = crew_dir.resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    if resolved.suffix != ".md":
        return False
    repo_root = run_root.parent.parent
    return (
        resolved == run_root / SPEC_NAME
        or path_is_reference_entry(resolved, run_root)
        or resolved == crew_root / "SKILL.md"
        or path_is_below(resolved, crew_root / "references")
        or resolved == repo_root / "references" / "trackers.md"
        or resolved.parent == repo_root / "docs" / "agents"
    )


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
        return emit_denial()
    if payload.get("tool_name") == "Bash" and isinstance(tool_input, dict):
        if bash_reads_file(tool_input.get("command")):
            return emit_denial()
    if payload.get("tool_name") == READ_TOOL and isinstance(tool_input, dict):
        if (
            read_is_bounded(tool_input)
            or (
                args.run_dir is not None
                and read_is_judgment_markdown(
                    payload, tool_input, args.run_dir, args.crew_dir
                )
            )
        ):
            return 0
        return emit_denial()
    return 0


def parser():
    command = argparse.ArgumentParser(description=__doc__)
    subcommands = command.add_subparsers(dest="command", required=True)
    hook = subcommands.add_parser("hook", help="apply the PreToolUse read boundary")
    hook.add_argument("--crew-dir", type=pathlib.Path, required=True)
    hook.add_argument("--run-dir", type=pathlib.Path)
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
