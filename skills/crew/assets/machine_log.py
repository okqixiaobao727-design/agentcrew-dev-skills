#!/usr/bin/env python3

"""The machine log: one append-only event log per run, written without model tokens.

Scripts append what they did — a child launched, a receipt verified, a branch merged, a ticket
settled — and a `PostToolUse` hook on `SendMessage` copies every outgoing message in verbatim, so
escalations and rulings land in the log as the messages are sent rather than costing a
coordinator turn to transcribe (ADR-0001).

The file is JSON Lines: one object per line, appended and never rewritten, every line stamped
`%Y-%m-%dT%H:%M:%SZ` in UTC — the run's one timestamp format, so any two lines subtract to a
duration. The audience is a later auditing agent, not a human; `docs/machine-log.md` publishes the
schema this writes.

    machine_log.py --log <path> launch|receipt|merge|outcome ...  # a script's own event
    machine_log.py --log <path> hook --role coordinator|child     # a hook, reading JSON on stdin

The hook never speaks on a channel a model reads: it writes nothing to stdout on the happy path,
writes nothing to stderr ever, and exits 0 even when the log cannot be written, because a send
that already happened must not be reported as a failure and an audit log must not block a run.
"""

import argparse
import datetime
import json
import os
import pathlib
import shlex
import sys

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# The tool whose outgoing messages the hook copies, and the event it is copied after. Anything
# else the hook is handed is not its business: a matcher is configuration, and configuration can
# be wrong.
MESSAGE_TOOL = "SendMessage"
HOOK_EVENT = "PostToolUse"
# What a registered hook runs, and the subcommand that marks a registration as one of ours.
PYTHON = "python3"
HOOK_SUBCOMMAND = "hook"

COORDINATOR = "coordinator"
CHILD = "child"

# The one verb the hook reads off a message: an escalation announces itself, and it is the only
# thing a child sends that the coordinator must answer. A receipt is deliberately not read here —
# a child's own `CREW COMPLETE` is a claim, and only the verifying script's `receipt` event says
# whether it held, so the two must never share an event name in the log.
ESCALATION_VERB = "CREW ASK"

# The closed sets. A log that accepts an unknown verdict is a log a later agent cannot trust.
VERDICTS = ("landable", "parked", "failed")
OUTCOMES = ("completed", "failed", "parked", "blocked")
MERGE_RESULTS = ("clean", "conflict", "repaired", "escalated")

LOG_FILE_MODE = 0o644


def now():
    """The current moment in the run's one timestamp format."""
    return datetime.datetime.now(datetime.timezone.utc).strftime(TIMESTAMP_FORMAT)


def entry(event, **fields):
    """One log record: the stamp and the event first, then the fields that were supplied.

    A field left unset is left out rather than written empty, so a reader can tell "not recorded"
    from "recorded as nothing".
    """
    record = {"ts": now(), "event": event}
    record.update({name: value for name, value in fields.items() if value is not None})
    return record


def append(path, record):
    """Append one record to the log at `path`, returning nothing; an unwritable log raises OSError.

    The line is handed to a single `write` on a descriptor opened `O_APPEND`, which is what lets
    the monitor, the merge driver and one hook per child share a file: their lines interleave,
    their characters never do.
    """
    line = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, LOG_FILE_MODE)
    try:
        os.write(descriptor, line)
    finally:
        os.close(descriptor)


def message_event(message, role):
    """The event name for an outgoing message: what the role sends, or a child's escalation.

    Only a child escalates — the coordinator is the top of the ladder — so the verb is read on
    the child side alone, and everything the coordinator sends is a ruling whatever it opens with.
    """
    if role == COORDINATOR:
        return "ruling"
    if isinstance(message, str) and message.lstrip().startswith(ESCALATION_VERB):
        return "escalation"
    return "message"


def hook_record(payload, role, ticket):
    """The record for a hook payload, or None when the payload carries no sent message.

    A payload that is not a `SendMessage` call, or not the shape one has, is not an error: the
    hook's job is to copy what it recognises and stay out of the way of everything else.
    """
    if not isinstance(payload, dict) or payload.get("tool_name") != MESSAGE_TOOL:
        return None
    arguments = payload.get("tool_input")
    if not isinstance(arguments, dict) or "message" not in arguments:
        return None
    recipient = arguments.get("to")
    return entry(
        message_event(arguments["message"], role),
        ticket=ticket,
        role=role,
        to=recipient if isinstance(recipient, str) else None,
        # Verbatim: the argument the sender gave the tool, neither truncated nor reformatted.
        message=arguments["message"],
    )


def run_hook(args):
    """Copy the message that was just sent into the log; returns 0 always, and always will.

    A send has already happened by the time this runs, so there is no failure left to report:
    the exit code is the hook's only channel a model reads, and it stays silent.
    """
    try:
        payload = json.loads(sys.stdin.read())
    except (ValueError, OSError):
        return 0
    record = hook_record(payload, args.role, args.ticket)
    if record is None:
        return 0
    try:
        append(args.log, record)
    except OSError as error:
        # The one channel left that no model reads. Reporting it any louder — stderr, a nonzero
        # exit — would feed the model a failure of bookkeeping it is not meant to know about.
        json.dump({"systemMessage": f"machine log: {args.log}: {error}"}, sys.stdout)
    return 0


def hook_command(script, log, role, ticket):
    """The shell command a registered hook runs: this script, in hook mode, for that side."""
    command = f"{shlex.quote(PYTHON)} {shlex.quote(str(script))} --log {shlex.quote(str(log))}"
    command += f" {HOOK_SUBCOMMAND} --role {shlex.quote(role)}"
    if ticket is not None:
        command += f" --ticket {shlex.quote(ticket)}"
    return command


def settings_shape_is_sound(settings):
    """Whether the two containers this install writes through are the shape they must be.

    Anything else is a file this script did not write and does not understand, and a settings
    file it does not understand is one it must not rewrite.
    """
    if not isinstance(settings, dict):
        return False
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        return False
    events = hooks.get(HOOK_EVENT, [])
    if not isinstance(events, list):
        return False
    # The block this install writes through is the one it would edit, nested list and all.
    return all(
        isinstance(block.get("hooks", []), list)
        for block in events
        if isinstance(block, dict) and block.get("matcher") == MESSAGE_TOOL
    )


def install_hook(settings, command, script):
    """The settings document with this hook registered in it, and nothing else disturbed.

    An entry already running `script` is replaced rather than added to, so installing twice — a
    resumed run, a re-prepared worktree — leaves one hook and not two. The path is what identifies
    it: two copies of this script are two different hooks, and a hook that is not this script's is
    nobody's business but its owner's.
    """
    hooks = settings.setdefault("hooks", {})
    events = hooks.setdefault(HOOK_EVENT, [])
    for block in events:
        if isinstance(block, dict) and block.get("matcher") == MESSAGE_TOOL:
            break
    else:
        block = {"matcher": MESSAGE_TOOL, "hooks": []}
        events.append(block)
    installed = shlex.quote(str(script))
    block["hooks"] = [
        hook
        for hook in block.get("hooks", [])
        if not (
            isinstance(hook, dict)
            and installed in str(hook.get("command", ""))
            and f" {HOOK_SUBCOMMAND} --role " in str(hook.get("command", ""))
        )
    ]
    block["hooks"].append({"type": "command", "command": command})
    return settings


def run_install(args):
    """Register the hook in a settings file; returns 0, or 1 when that file cannot be read."""
    path = pathlib.Path(args.settings)
    try:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        # A missing or empty file is a fresh document to write: the refusal below exists to
        # protect the guard hooks that live in this file, and an empty file holds none.
        settings = json.loads(text.strip() or "{}")
    except (OSError, UnicodeDecodeError, ValueError) as error:
        # Never overwrite a settings file that was not understood: the guard hooks live there.
        print(f"machine log: {path}: {error}", file=sys.stderr)
        return 1
    if not settings_shape_is_sound(settings):
        print(f"machine log: {path}: not a settings document", file=sys.stderr)
        return 1
    command = hook_command(args.hook_script, args.log, args.role, args.ticket)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(install_hook(settings, command, args.hook_script), indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as error:
        print(f"machine log: {path}: {error}", file=sys.stderr)
        return 1
    print(path)
    return 0


def run_event(args):
    """Append one script event, named by the subcommand called; returns 0, or 1 on an OSError."""
    fields = {
        name: value
        for name, value in vars(args).items()
        if name not in ("log", "event", "handler")
    }
    try:
        append(args.log, entry(args.event, **fields))
    except OSError as error:
        print(f"machine log: {args.log}: {error}", file=sys.stderr)
        return 1
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--log", required=True, help="the run's machine log")
    subcommands = parser.add_subparsers(dest="event", required=True)

    def event_command(name, help_text):
        command = subcommands.add_parser(name, help=help_text)
        command.set_defaults(handler=run_event)
        command.add_argument("--ticket", required=True, help="the ticket number, as written")
        return command

    launch = event_command("launch", "a child started on a ticket")
    launch.add_argument("--child", required=True)
    launch.add_argument("--workflow", required=True)
    launch.add_argument("--executor", required=True)
    launch.add_argument("--model", required=True, help="the full model ID, never an alias")
    launch.add_argument("--effort", required=True)
    launch.add_argument("--branch")
    launch.add_argument("--worktree")
    launch.add_argument("--window")

    receipt = event_command("receipt", "a child's final word, as verified by script")
    receipt.add_argument("--verdict", required=True, choices=VERDICTS)
    receipt.add_argument("--sha")
    receipt.add_argument("--detail")

    merge = event_command("merge", "one ticket branch's trip into the integration branch")
    merge.add_argument("--result", required=True, choices=MERGE_RESULTS)
    merge.add_argument("--branch")
    merge.add_argument("--into")
    merge.add_argument("--sha")
    merge.add_argument("--detail")

    outcome = event_command("outcome", "a ticket's one report outcome")
    outcome.add_argument("--outcome", required=True, choices=OUTCOMES)
    outcome.add_argument("--detail")

    hook = subcommands.add_parser("hook", help="copy an outgoing SendMessage into the log")
    hook.set_defaults(handler=run_hook)
    hook.add_argument("--role", required=True, choices=(COORDINATOR, CHILD))
    hook.add_argument("--ticket", help="the ticket this side of the channel serves, where known")

    install = subcommands.add_parser("install", help="register that hook in a settings file")
    install.set_defaults(handler=run_install)
    install.add_argument("--settings", required=True, help="the settings file to register in")
    install.add_argument("--role", required=True, choices=(COORDINATOR, CHILD))
    install.add_argument("--ticket", help="the ticket this side of the channel serves, where known")
    install.add_argument(
        "--hook-script",
        default=pathlib.Path(__file__).resolve(),
        help="the copy of this script the hook should run (default: this one)",
    )

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
