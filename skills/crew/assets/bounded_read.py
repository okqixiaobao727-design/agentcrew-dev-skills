#!/usr/bin/env python3
"""Bound file-reading tools in a coordinator or manual advisor session."""

import argparse
import collections
import json
import pathlib
import re
import sys


READ_TOOL = "Read"
SEARCH_TOOLS = frozenset(("Grep", "Glob"))
SHELL_READ_COMMANDS = frozenset(("cat", "sed", "head", "tail", "grep", "rg", "find", "ls"))
STDIN_WHEN_OPERANDLESS = frozenset(("cat", "sed", "head", "tail", "grep", "rg"))
ANSI_C_ESCAPES = {
    "a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n",
    "r": "\r", "t": "\t", "v": "\v", "\\": "\\", "'": "'", '"': '"', "?": "?",
}
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
UNPARSEABLE_DETAIL = "The command could not be parsed."
Invocation = collections.namedtuple(
    "Invocation", ("name", "arguments", "words", "heredoc", "input_file")
)
BashReadResult = collections.namedtuple("BashReadResult", ("reads", "detail"))


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
    )


class ShellParseError(ValueError):
    """One command line that no bash-faithful reading of it can tokenise."""


class ShellScanner:
    """Walk one command line the way bash does, collecting the simple commands it runs.

    Quoted data is never mistaken for a command: a single, double or ANSI-C quoted word and a
    quoted-delimiter heredoc body are text. Only the regions that run a command are entered:
    `$(…)`, backticks, and the substitutions inside an unquoted-delimiter heredoc body, an
    arithmetic expansion or a parameter expansion. What no bash reading can tokenise raises
    ShellParseError, so the caller fails closed rather than guessing.
    """

    SEPARATORS = frozenset((";", "&", "|"))
    NESTED_SHELLS = frozenset(("bash", "sh", "zsh"))

    def __init__(self, text):
        self.text = text
        self.index = 0
        self.invocations = []

    def scan(self, terminator=None):
        """Consume the text from the current position up to `terminator`, or to its end."""
        words = []
        token = None
        quoted = False
        heredoc = False
        input_file = False
        expect = None
        strip_tabs = False
        pending = []
        depth = 0

        def begin_token():
            nonlocal token
            if token is None:
                token = []
            return token

        def finish_token():
            nonlocal token, quoted, expect, heredoc, input_file
            if token is None:
                return
            word = "".join(token)
            was_quoted = quoted
            token = None
            quoted = False
            if expect == "heredoc":
                pending.append((word, was_quoted, strip_tabs))
                heredoc = True
            elif expect == "input":
                input_file = True
            elif expect is None:
                words.append(word)
            expect = None

        def finish_command():
            nonlocal words, heredoc, input_file
            finish_token()
            invocation = simple_command(words, heredoc, input_file)
            if invocation is not None:
                self.invocations.append(invocation)
            words = []
            heredoc = False
            input_file = False

        text = self.text
        while self.index < len(text):
            character = text[self.index]
            following = text[self.index + 1:self.index + 2]
            if character == "\\":
                if not following:
                    raise ShellParseError("a command line may not end in a backslash")
                self.index += 2
                if following == "\n":
                    continue
                quoted = True
                begin_token().append(following)
                continue
            if character == "'":
                quoted = True
                self.index += 1
                closing = text.find("'", self.index)
                if closing == -1:
                    raise ShellParseError("unterminated single quote")
                begin_token().append(text[self.index:closing])
                self.index = closing + 1
                continue
            if character == '"' or (character == "$" and following == '"'):
                self.index += 1 if character == '"' else 2
                quoted = True
                self.read_double_quoted(begin_token())
                continue
            if character == "$" and following == "'":
                self.index += 2
                quoted = True
                self.read_ansi_c_quoted(begin_token())
                continue
            if character == "`" and terminator == "`":
                finish_command()
                self.index += 1
                return
            if self.read_expansion():
                continue
            if character == "#" and token is None:
                newline = text.find("\n", self.index)
                self.index = len(text) if newline == -1 else newline
                continue
            if character in " \t\r":
                finish_token()
                self.index += 1
                continue
            if character == "\n":
                finish_token()
                self.index += 1
                for delimiter, delimiter_quoted, dashed in pending:
                    self.read_heredoc_body(delimiter, delimiter_quoted, dashed)
                pending.clear()
                finish_command()
                continue
            if character in "<>":
                if token is not None and "".join(token).isdigit():
                    token = None
                    quoted = False
                finish_token()
                expect, strip_tabs = self.read_redirection(character)
                continue
            if character in self.SEPARATORS:
                finish_command()
                while self.index < len(text) and text[self.index] in self.SEPARATORS:
                    self.index += 1
                continue
            if character == "(":
                finish_command()
                depth += 1
                self.index += 1
                continue
            if character == ")":
                finish_command()
                self.index += 1
                if depth:
                    depth -= 1
                    continue
                if terminator != ")":
                    raise ShellParseError("unbalanced closing parenthesis")
                return
            if character in "{}" and token is None and following in ("", " ", "\t", "\n", ";"):
                finish_command()
                self.index += 1
                continue
            begin_token().append(character)
            self.index += 1
        if terminator is not None:
            raise ShellParseError("unterminated %s" % terminator)
        if depth:
            raise ShellParseError("unbalanced opening parenthesis")
        finish_command()

    def read_double_quoted(self, token):
        """Consume a double-quoted argument, recursing into the substitutions bash expands."""
        text = self.text
        while True:
            if self.index >= len(text):
                raise ShellParseError("unterminated double quote")
            character = text[self.index]
            if character == '"':
                self.index += 1
                return
            if character == "\\":
                token.append(text[self.index + 1:self.index + 2])
                self.index += 2
                continue
            if self.read_expansion():
                continue
            token.append(character)
            self.index += 1

    def read_ansi_c_quoted(self, token):
        """Consume a `$'…'` argument, decoding the escapes bash decodes inside it.

        The decoding matters as much as the quoting: a hex escape left undecoded would hide
        the very command the boundary exists to catch.
        """
        text = self.text
        while True:
            if self.index >= len(text):
                raise ShellParseError("unterminated ANSI-C quote")
            character = text[self.index]
            if character == "'":
                self.index += 1
                return
            if character != "\\":
                token.append(character)
                self.index += 1
                continue
            self.index += 1
            token.append(self.read_ansi_c_escape())

    def read_ansi_c_escape(self):
        """Decode the one backslash escape at the cursor into the character it stands for."""
        text = self.text
        character = text[self.index:self.index + 1]
        if not character:
            raise ShellParseError("unterminated ANSI-C escape")
        self.index += 1
        if character in ANSI_C_ESCAPES:
            return ANSI_C_ESCAPES[character]
        if character == "c":
            control = text[self.index:self.index + 1]
            self.index += 1 if control else 0
            return chr(ord(control.upper()) ^ 0x40) if control else "\\c"
        widths = {"x": 2, "u": 4, "U": 8}
        if character in widths:
            digits = self.read_escape_digits(widths[character], "0123456789abcdefABCDEF")
            if not digits:
                return character
            point = int(digits, 16)
            if point > 0x10FFFF or 0xD800 <= point <= 0xDFFF:
                raise ShellParseError("escape outside the Unicode range")
            return chr(point)
        if character in "01234567":
            self.index -= 1
            return chr(int(self.read_escape_digits(3, "01234567"), 8) & 0xFF)
        return character

    def read_escape_digits(self, width, alphabet):
        """Consume up to `width` digits of one numeric escape from the cursor."""
        digits = []
        while len(digits) < width and self.text[self.index:self.index + 1] in tuple(alphabet):
            digits.append(self.text[self.index])
            self.index += 1
        return "".join(digits)

    def read_expansion(self):
        """Consume one expansion at the cursor; report whether there was one to consume.

        The text of an arithmetic or parameter expansion is data, but a command substitution
        nested in either still runs, so those bodies are walked rather than skipped.
        """
        text = self.text
        if text.startswith("$((", self.index):
            self.read_arithmetic()
            return True
        if text.startswith("${", self.index):
            self.read_parameter()
            return True
        if text.startswith("$(", self.index):
            self.index += 2
            self.scan(")")
            return True
        if text.startswith("`", self.index):
            self.index += 1
            self.scan("`")
            return True
        return False

    def read_arithmetic(self):
        """Consume a `$((…))` expansion, whose body is arithmetic but may still substitute."""
        self.index += 3
        self.read_nesting("()", 2, "arithmetic expansion")

    def read_parameter(self):
        """Consume a `${…}` expansion, whose body is data but may still substitute."""
        self.index += 2
        self.read_nesting("{}", 1, "parameter expansion")

    def read_nesting(self, brackets, depth, name):
        """Consume one bracketed expansion body, entering the commands it still substitutes."""
        opening, closing = brackets
        text = self.text
        while self.index < len(text):
            if self.read_expansion():
                continue
            character = text[self.index]
            if character == "\\":
                self.index += 2
                continue
            depth += {opening: 1, closing: -1}.get(character, 0)
            self.index += 1
            if not depth:
                return
        raise ShellParseError("unterminated %s" % name)

    def read_redirection(self, character):
        """Consume one redirection operator, reporting what its following word means."""
        text = self.text
        if text.startswith("<<", self.index):
            self.index += 2
            dashed = text[self.index:self.index + 1] == "-"
            self.index += 1 if dashed else 0
            if text[self.index:self.index + 1] == "<":
                self.index += 1
                return "herestring", False
            return "heredoc", dashed
        self.index += 1
        if character == ">" and text[self.index:self.index + 1] == ">":
            self.index += 1
        if text[self.index:self.index + 1] == "&":
            self.index += 1
            return "descriptor", False
        return "input" if character == "<" else "output", False

    def read_heredoc_body(self, delimiter, delimiter_quoted, dashed):
        """Consume one heredoc body, expanding it only when bash would."""
        text = self.text
        lines = []
        while self.index < len(text):
            newline = text.find("\n", self.index)
            if newline == -1:
                line, self.index = text[self.index:], len(text)
            else:
                line, self.index = text[self.index:newline], newline + 1
            if (line.lstrip("\t") if dashed else line) == delimiter:
                break
            lines.append(line)
        if not delimiter_quoted:
            self.invocations.extend(substitution_invocations("\n".join(lines)))


def substitution_invocations(body):
    """Return the simple commands in the substitutions of one unquoted heredoc body."""
    scanner = ShellScanner(body)
    while scanner.index < len(body):
        if body[scanner.index] == "\\":
            scanner.index += 2
            continue
        if not scanner.read_expansion():
            scanner.index += 1
    return scanner.invocations


def simple_command(words, heredoc, input_file):
    """Return the invocation one simple command's words name, or None when they name none."""
    index = 0
    while index < len(words) and (
        ASSIGNMENT.fullmatch(words[index])
        or words[index].startswith("-")
        or words[index] in SHELL_PREFIXES
    ):
        index += 1
    if index >= len(words):
        return None
    return Invocation(
        name=pathlib.PurePath(words[index]).name,
        arguments=tuple(words[index + 1:]),
        words=tuple(words),
        heredoc=heredoc,
        input_file=input_file,
    )


def all_shell_invocations(command):
    """Return the simple commands one shell string runs, including its `sh -c` bodies."""
    scanner = ShellScanner(command)
    scanner.scan()
    invocations = list(scanner.invocations)
    for invocation in tuple(invocations):
        body = nested_shell_body(invocation)
        if body is not None:
            invocations.extend(all_shell_invocations(body))
    return invocations


def nested_shell_body(invocation):
    """Return the command string one `sh -c` style invocation runs, or None when it runs none."""
    if invocation.name not in ShellScanner.NESTED_SHELLS:
        return None
    for position, argument in enumerate(invocation.arguments):
        bundle = argument.startswith("-") and not argument.startswith("--") and len(argument) > 1
        if bundle and argument.endswith("c") and position + 1 < len(invocation.arguments):
            return invocation.arguments[position + 1]
    return None


def invocation_reads_file(invocation):
    """Return whether one parsed command invocation reads file or directory contents."""
    if invocation.name not in SHELL_READ_COMMANDS:
        return False
    if invocation.input_file:
        return True
    if invocation.name == "ls" and (
        "-d" in invocation.arguments or "--directory" in invocation.arguments
    ):
        return False
    if invocation.name not in STDIN_WHEN_OPERANDLESS:
        return True
    operands = [word for word in invocation.arguments if not word.startswith("-")]
    return not (invocation.heredoc and not operands)


def bash_read_detail(command):
    """Classify one Bash command as reading a file, not reading one, or unreadable to us."""
    if not isinstance(command, str):
        return BashReadResult(None, UNPARSEABLE_DETAIL)
    try:
        invocations = all_shell_invocations(command)
    except (ShellParseError, RecursionError):
        return BashReadResult(None, UNPARSEABLE_DETAIL)
    for invocation in invocations:
        if invocation_reads_file(invocation):
            return BashReadResult(
                True,
                "Matched `%s` in `%s`." % (invocation.name, " ".join(invocation.words)),
            )
    return BashReadResult(False, None)


def emit_denial(detail=None):
    """Print Claude Code's one-line PreToolUse denial and return exit status zero."""
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                DENIAL_REASON if detail is None else "%s %s" % (DENIAL_REASON, detail)
            ),
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
        verdict = bash_read_detail(tool_input.get("command"))
        if verdict.reads is not False:
            return emit_denial(verdict.detail)
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
