#!/usr/bin/env python3

"""The machine log: one append-only event log per run, written without model tokens.

Scripts append what they did — a child launched, a receipt verified, a branch merged, a ticket
settled, a wave advanced or halted — and a `PostToolUse` hook on `SendMessage` copies every
outgoing message in verbatim, so escalations and rulings land in the log as the messages are sent
rather than costing a coordinator turn to transcribe (ADR-0001).

The file is JSON Lines: one object per line, appended and never rewritten, every line stamped
`%Y-%m-%dT%H:%M:%SZ` in UTC — the run's one timestamp format, so any two lines subtract to a
duration. The audience is a later auditing agent, not a human; `docs/machine-log.md` publishes the
schema this writes.

    machine_log.py --log <path> launch|receipt|merge|outcome|review|advance|monitor-error|
                                  session-cost|message ...
                                                              # a script's own event
    machine_log.py --log <path> hook --role coordinator|child  # a hook, on stdin
    machine_log.py --log <path> install|uninstall --settings <file> ...  # register it, or not

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
import tempfile

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# The tool whose outgoing messages the hook copies, and the event it is copied after. Anything
# else the hook is handed is not its business: a matcher is configuration, and configuration can
# be wrong.
MESSAGE_TOOL = "SendMessage"
HOOK_EVENT = "PostToolUse"
# What a registered hook runs, and the subcommand that marks a registration as one of ours.
PYTHON = "python3"
HOOK_SUBCOMMAND = "hook"
# The name the run's own copy of this script is installed under, beside the log it writes. The
# copy is what keeps a registered command version-independent: the plugin directory a run installs
# from carries the version in its path, so an upgrade would leave the entry naming a file that is
# no longer there, while the run directory outlives every upgrade (#37).
SCRIPT_NAME = "machine_log.py"
# The directory a settings file lives in when it belongs to a checkout: `<project>/.claude/`.
SETTINGS_DIRECTORY = ".claude"

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
# The two ends of one review. A review that started and never came back is a row the dashboard
# would leave standing, so the vocabulary is closed at "it is running" and "it is not".
REVIEW_STATES = ("running", "returned")
# What the run decided about carrying on after a wave. One of these per decision, and a decision
# is about a wave rather than a ticket, so this is the one event that carries no ticket. Two of
# them end a run — `complete`, every wave landed, and `stopped`, the chain halted on reasons no
# ruling will undo — and every surface that asks whether a run is over asks for exactly those two:
# `escalated` alone cannot say, because it is also the word for a wave awaiting a ruling.
DECISIONS = ("launched", "escalated", "complete", "interrupted", "stopped")
# The two lanes a child runs in. A usage figure is only readable against the executor that wrote
# it, so an executor this log does not know is an executor whose figures nobody can check.
EXECUTORS = ("claude", "codex")
# What a session cost is made of: four disjoint counters and the total they must come to. A line
# carrying some of them, or a total that is not their sum, is arithmetic a later agent would trust
# and be wrong to, so it is refused like a value outside a closed set.
COST_COUNTERS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")
COST_TOTAL = "total_tokens"

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


def sent_from_scope(payload, scope):
    """Whether this send came from the session whose settings registered this hook.

    A child's worktree sits inside the repository the coordinator runs in, and Claude Code loads
    the enclosing checkout's settings in the child's session too: every hook both files register
    fires on one send. Without this check the coordinator's hook copies the child's message in a
    second time and labels it a ruling, so the log counts two messages where the run had one and
    attributes the child's words to the coordinator (#37).

    The scope is the directory the install was registered for, and the match is that directory
    exactly — a worktree is a descendant of the checkout above it, so containment would let the
    same duplicate through. A hook registered without a scope is one an older install wrote, and
    it copies what it is handed as it always did.
    """
    if scope is None:
        return True
    sender = payload.get("cwd")
    if not isinstance(sender, str):
        return False
    return os.path.realpath(sender) == os.path.realpath(scope)


def run_hook(args):
    """Copy the message that was just sent into the log; returns 0 always, and always will.

    A send has already happened by the time this runs, so there is no failure left to report:
    the exit code is the hook's only channel a model reads, and it stays silent.
    """
    try:
        payload = json.loads(sys.stdin.read())
    except (ValueError, OSError):
        return 0
    if not isinstance(payload, dict) or not sent_from_scope(payload, args.scope):
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


def absolute(path):
    """`path` as an absolute path, without following the symlinks along the way.

    A hook runs in the cwd of the session that fired it — a child's worktree — while the paths
    it was registered with were spelled wherever the install ran. A relative one recorded here
    would resolve against the wrong directory at fire time and the escalation would land in a
    file nobody reads, so the spelling is settled at the boundary. Symlinks are left alone: the
    operator's own name for the run directory is the one the log should be found under.
    """
    return os.path.abspath(str(path))


def hook_command(script, log, role, ticket, scope):
    """The shell command a registered hook runs: this script, in hook mode, for that side.

    Every path in it is absolute, because the cwd it will run in is not the cwd it was written
    in — and the scope is the one it may write for.
    """
    command = (
        f"{shlex.quote(PYTHON)} {shlex.quote(absolute(script))}"
        f" --log {shlex.quote(absolute(log))}"
    )
    command += f" {HOOK_SUBCOMMAND} --role {shlex.quote(role)}"
    if ticket is not None:
        command += f" --ticket {shlex.quote(ticket)}"
    command += f" --scope {shlex.quote(absolute(scope))}"
    return command


def settings_scope(settings):
    """The directory whose sessions a settings file governs: the checkout it sits in.

    A settings file lives at `<project>/.claude/settings.local.json`, so the project above it is
    the cwd its sessions send from. Anywhere else, the file's own directory is the best answer
    there is, and `--scope` is there for a caller who knows better.
    """
    directory = pathlib.Path(absolute(pathlib.Path(settings).parent))
    if directory.name == SETTINGS_DIRECTORY:
        return directory.parent
    return directory


def run_script(log):
    """Where a run keeps the copy of this script its hooks run: beside its own log.

    The run directory carries no plugin version, which is the whole point — an entry pointing into
    the versioned plugin tree stops working the moment the plugin is upgraded (#37).
    """
    return pathlib.Path(absolute(pathlib.Path(log).parent / SCRIPT_NAME))


def materialise_script(source, destination):
    """Put a copy of `source` at `destination`, replacing whatever was there; raises OSError.

    Written to a neighbouring temporary file and moved into place, so a hook firing during an
    install runs either the old copy or the new one and never half of either.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    if absolute(source) == str(destination):
        return destination
    handle, temporary = tempfile.mkstemp(dir=str(destination.parent), prefix=f".{SCRIPT_NAME}.")
    try:
        with open(handle, "wb") as copy:
            copy.write(pathlib.Path(source).read_bytes())
        os.replace(temporary, destination)
    except BaseException:
        pathlib.Path(temporary).unlink(missing_ok=True)
        raise
    return destination


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


def command_log(command):
    """The log a registered command writes in hook mode, or None when it is not one of ours.

    The command is read as the shell will read it — split into its words, the `--log` argument
    taken whole — rather than searched for a substring, because one log's path is a prefix of
    another's the moment a run directory is named after it and a hook nobody installed here must
    never be mistaken for one this script owns.
    """
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    if HOOK_SUBCOMMAND not in words or "--role" not in words:
        return None
    for index, word in enumerate(words):
        if word == "--log" and index + 1 < len(words):
            return absolute(words[index + 1])
        if word.startswith("--log="):
            return absolute(word[len("--log="):])
    return None


def registered_for(hook, log):
    """Whether this registered hook is an entry an install for `log` wrote.

    The log it writes is what identifies it, not the script that runs it: an upgrade changes
    the registered path — the plugin's copy carries its version — and an entry the new install
    failed to recognise would be left beside the new one, firing an old writer at the same log
    (#37). One run owns one entry per settings file, whichever version wrote it; another run's
    entry, and a hook that is not this script's at all, are nobody's business but their owners'.
    """
    if not isinstance(hook, dict):
        return False
    return command_log(str(hook.get("command", ""))) == absolute(log)


def message_blocks(settings):
    """Every block of the settings document that claims the outgoing-message matcher."""
    events = settings.get("hooks", {}).get(HOOK_EVENT, [])
    return [
        block for block in events
        if isinstance(block, dict) and block.get("matcher") == MESSAGE_TOOL
    ]


def install_hook(settings, command, log):
    """The settings document with this hook registered in it, and nothing else disturbed.

    An entry already writing `log` is replaced rather than added to, so installing twice — a
    resumed run, a re-prepared worktree, a run that outlived a plugin upgrade — leaves one hook
    and not two.
    """
    hooks = settings.setdefault("hooks", {})
    events = hooks.setdefault(HOOK_EVENT, [])
    blocks = message_blocks(settings)
    if blocks:
        block = blocks[0]
    else:
        block = {"matcher": MESSAGE_TOOL, "hooks": []}
        events.append(block)
    block["hooks"] = [
        hook for hook in block.get("hooks", []) if not registered_for(hook, log)
    ]
    block["hooks"].append({"type": "command", "command": command})
    return settings


def uninstall_hook(settings, log):
    """The settings document with every entry installed for `log` taken out of it.

    Every other hook stays exactly where it is, and a block this leaves empty goes with the entry
    that was its only occupant — an empty matcher block registers nothing and was not there before
    the install. Returns whether anything was removed, so a file with nothing of ours in it is
    left byte for byte as it was found.
    """
    removed = False
    events = settings.get("hooks", {}).get(HOOK_EVENT, [])
    for block in message_blocks(settings):
        registered = block.get("hooks", [])
        kept = [hook for hook in registered if not registered_for(hook, log)]
        if len(kept) == len(registered):
            continue
        removed = True
        block["hooks"] = kept
        if not kept:
            events.remove(block)
    return removed


def read_settings(path):
    """The settings document at `path`, or the reason it must not be rewritten.

    Returns the document and None, or None and the message to print: a missing or empty file is a
    fresh document to write, and anything this script did not write and does not understand is a
    file it refuses to touch, because the guard hooks live there too.
    """
    try:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        settings = json.loads(text.strip() or "{}")
    except (OSError, UnicodeDecodeError, ValueError) as error:
        return None, f"machine log: {path}: {error}"
    if not settings_shape_is_sound(settings):
        return None, f"machine log: {path}: not a settings document"
    return settings, None


def write_settings(path, settings):
    """Write the settings document back; returns 0, or 1 when it cannot be written."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    except OSError as error:
        print(f"machine log: {path}: {error}", file=sys.stderr)
        return 1
    print(path)
    return 0


def run_install(args):
    """Register the hook in a settings file; returns 0, or 1 when that file cannot be read.

    By default the command registered runs the run's own copy of this script rather than the one
    installing, because the plugin is installed one directory per version: an entry naming the
    plugin's copy stops working at the next upgrade, while the run directory outlives every one of
    them (#37). The copy is refreshed from the script that is installing, so an upgraded plugin's
    log writer is the one a resumed run's hooks go on running. A caller that keeps its own copy
    names it with `--hook-script` and that path is registered as it was given.
    """
    path = pathlib.Path(args.settings)
    settings, problem = read_settings(path)
    if problem is not None:
        print(problem, file=sys.stderr)
        return 1
    try:
        script = (
            absolute(args.hook_script) if args.hook_script is not None
            else str(materialise_script(pathlib.Path(__file__).resolve(), run_script(args.log)))
        )
    except OSError as error:
        print(f"machine log: {error}", file=sys.stderr)
        return 1
    scope = args.scope if args.scope is not None else settings_scope(path)
    command = hook_command(script, args.log, args.role, args.ticket, scope)
    return write_settings(path, install_hook(settings, command, args.log))


def run_uninstall(args):
    """Take this run's hooks out of a settings file; returns 0, or 1 on a file it must not touch.

    What it removes is every entry writing this run's log, whichever version of this script
    installed it, and nothing else. Idempotent by construction: a file that carries none of ours
    is left exactly as it was found and a second call has nothing left to do. Every other hook
    in the file — the guard hooks, another run's entry, a watcher — stays where it is.
    """
    path = pathlib.Path(args.settings)
    if not path.exists():
        return 0
    settings, problem = read_settings(path)
    if problem is not None:
        print(problem, file=sys.stderr)
        return 1
    if not uninstall_hook(settings, args.log):
        return 0
    return write_settings(path, settings)


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


def run_message(args):
    """Append one outgoing message; returns 0, or 1 on an OSError."""
    record = entry(
        message_event(args.message, args.role),
        ticket=args.ticket,
        role=args.role,
        to=args.to,
        message=args.message,
    )
    try:
        append(args.log, record)
    except OSError as error:
        print(f"machine log: {args.log}: {error}", file=sys.stderr)
        return 1
    return 0


def cost_problem(args):
    """Why this session cost contradicts itself, or None when it holds together.

    A session cost answers one of two questions and never both: what the child spent, in all five
    figures, or why nobody could tell. Anything between the two is a line whose reader cannot know
    which of the two it is holding.
    """
    counters = [getattr(args, name) for name in COST_COUNTERS]
    total = getattr(args, COST_TOTAL)
    figures = [value for value in counters + [total] if value is not None]
    if not figures:
        if args.detail is None:
            return "a session cost with no figures carries the diagnosis that says why"
        return None
    if len(figures) < len(COST_COUNTERS) + 1:
        return f"a session cost carries all of {', '.join(COST_COUNTERS)} and {COST_TOTAL}, or none"
    if args.detail is not None:
        return "a session cost carries its figures or a diagnosis, never both"
    if any(value < 0 for value in figures):
        return "a token count is never negative"
    if total != sum(counters):
        return f"{COST_TOTAL} {total} is not the sum of the four counters"
    return None


def run_session_cost(args):
    """Append one session cost; returns 0, or 2 for a record that contradicts itself."""
    problem = cost_problem(args)
    if problem is not None:
        print(f"machine log: session-cost: {problem}", file=sys.stderr)
        return 2
    return run_event(args)


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

    review = event_command("review", "one end of a ticket's trip through its review lane")
    review.add_argument(
        "--lane", required=True,
        help="the reviewing vendor and its model, as the wave table approved them",
    )
    review.add_argument("--state", required=True, choices=REVIEW_STATES)
    review.add_argument("--detail")

    cost = event_command("session-cost", "what one child's session spent, in tokens")
    cost.set_defaults(handler=run_session_cost)
    cost.add_argument("--executor", required=True, choices=EXECUTORS)
    cost.add_argument("--model", required=True, help="the full model ID, never an alias")
    cost.add_argument("--session", help="the session whose transcript these figures were read off")
    for tokens in ("input", "output", "cache-read", "cache-creation", "total"):
        cost.add_argument(f"--{tokens}-tokens", type=int, help=f"{tokens} tokens, as counted")
    # Left unset when the transcript could not be read, which is what makes `detail` the diagnosis
    # rather than a note: a line with no figures and no detail would be a silent gap.
    cost.add_argument("--detail", help="why the figures are missing, when they are")

    advance = subcommands.add_parser("advance", help="what the run decided after a wave settled")
    advance.set_defaults(handler=run_event)
    advance.add_argument("--wave", required=True, help="the wave the decision is about")
    advance.add_argument("--decision", required=True, choices=DECISIONS)
    advance.add_argument("--detail")

    monitor_error = subcommands.add_parser(
        "monitor-error", help="record a monitor that exited with an error"
    )
    monitor_error.set_defaults(handler=run_event)
    monitor_error.add_argument("--monitor", required=True, help="the monitor that failed")
    monitor_error.add_argument("--reason", required=True, help="why the monitor failed")

    message = subcommands.add_parser("message", help="record an outgoing message")
    message.set_defaults(handler=run_message)
    message.add_argument("--role", required=True, choices=(COORDINATOR, CHILD))
    message.add_argument("--ticket")
    message.add_argument("--to")
    message.add_argument("--message", required=True)

    hook = subcommands.add_parser("hook", help="copy an outgoing SendMessage into the log")
    hook.set_defaults(handler=run_hook)
    hook.add_argument("--role", required=True, choices=(COORDINATOR, CHILD))
    hook.add_argument("--ticket", help="the ticket this side of the channel serves, where known")
    hook.add_argument(
        "--scope",
        help="the directory whose sends this hook copies; anything else is another side's",
    )

    install = subcommands.add_parser("install", help="register that hook in a settings file")
    install.set_defaults(handler=run_install)
    install.add_argument("--settings", required=True, help="the settings file to register in")
    install.add_argument("--role", required=True, choices=(COORDINATOR, CHILD))
    install.add_argument("--ticket", help="the ticket this side of the channel serves, where known")
    install.add_argument(
        "--hook-script",
        help="a copy of this script to register as given (default: the run's own, beside the log)",
    )
    install.add_argument(
        "--scope",
        help="the directory this settings file's sessions run in (default: the checkout above it)",
    )

    uninstall = subcommands.add_parser(
        "uninstall", help="remove every entry installed for this log from a settings file"
    )
    uninstall.set_defaults(handler=run_uninstall)
    uninstall.add_argument("--settings", required=True, help="the settings file to remove from")

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
