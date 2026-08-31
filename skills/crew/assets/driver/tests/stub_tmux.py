#!/usr/bin/env python3
"""A tmux stand-in for the driver tests.

Every call is appended to `AGENTCREW_STUB_DIR/tmux-calls.jsonl`, which is where a test reads the
preflight notice's text, the dashboard command, and the kill that cleared a stale notice. Windows
live in `tmux-windows.json` there: `new-window` records one and prints its id, `list-windows`
prints them in the format it was asked for, and `kill-window` takes one away.

`send-keys` runs the keys it was given through `sh -c` in the window's own directory and in the
window's own environment — the `-e NAME=VALUE` pairs it was created with, which is how a child is
put on its ticket's account — so a stubbed
launch reaches the stub `claude` exactly as a real one reaches the real CLI — unless they were sent
with `-l`, which is text typed at whoever is reading the pane and is recorded rather than run. A
window's own command is recorded and never run: the dashboard's refresh loop would otherwise
outlive the test.
"""

import fcntl
import json
import os
import pathlib
import subprocess
import sys


PANE_PREFIX = "%"
ANSWER_KEYS = set("0123456789") | {"Up", "Down", "Left", "Right", "Enter", "S-Enter"}


def state_dir():
    return pathlib.Path(os.environ["AGENTCREW_STUB_DIR"])


def windows_path():
    return state_dir() / "tmux-windows.json"


def windows():
    path = windows_path()
    return json.loads(path.read_text()) if path.exists() else {}


def save_windows(table):
    windows_path().write_text(json.dumps(table))


def flag(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


def window_environment(argv):
    """The `NAME=VALUE` pairs the window is created with, as real tmux's `-e` gives it one."""
    environment = {}
    for index, value in enumerate(argv):
        if value == "-e" and index + 1 < len(argv):
            name, _, setting = argv[index + 1].partition("=")
            environment[name] = setting
    return environment


def fill(template, window_id, window):
    """One window rendered into the format string tmux was asked for."""
    text = template.replace("#{window_id}", window_id)
    text = text.replace("#{window_name}", window.get("name") or "")
    return text.replace("#{session_id}", window.get("target") or "")


def known_session():
    """The one session this stub server holds, where the fixture named one."""
    path = state_dir() / "tmux-session"
    return path.read_text().strip() if path.exists() else None


def unknown_session(target):
    """Whether tmux would answer that target with `can't find session`, as a real server does."""
    session = known_session()
    return target is not None and session is not None and target != session


def new_window(argv):
    if (state_dir() / "tmux-new-window-fails").exists():
        print("create window failed: index in use", file=sys.stderr)
        return 1
    selective_failure = state_dir() / "tmux-new-window-fails-for"
    if selective_failure.exists() and flag(argv, "-n") == selective_failure.read_text().strip():
        print("create window failed: index in use", file=sys.stderr)
        return 1
    if unknown_session(flag(argv, "-t")):
        print(f"can't find session: {flag(argv, '-t')}", file=sys.stderr)
        return 1
    table = windows()
    counter = state_dir() / "tmux-window-counter"
    issued = int(counter.read_text()) + 1 if counter.exists() else 1
    counter.write_text(str(issued))
    window_id = f"@{issued}"
    table[window_id] = {
        "name": flag(argv, "-n"),
        "target": flag(argv, "-t"),
        "cwd": flag(argv, "-c") or os.getcwd(),
        "env": window_environment(argv),
        "command": argv[-1],
        "detached": "-d" in argv,
        "composer": "",
    }
    save_windows(table)
    if flag(argv, "-n") == "crew-driver":
        subprocess.Popen(
            ["sh", "-c", argv[-1]],
            cwd=flag(argv, "-c") or os.getcwd(),
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    print(window_id)
    return 0


def loose_pane(target):
    """Returns that target where it is a pane this server answers for, or None where it is not.

    A real tmux server holds panes the run never opened as windows: the pane the coordinator
    itself is sitting in, which the driver re-types `/crew` into when a wake finds no waiter, and
    which reaches this stub only as a `-t` target it has no window for. Recording one on first use
    gives it a composer of its own, flagged so `list-windows` never reports it as a window of the
    run. Everything else — a window id that has gone — stays the error it was.
    """
    if not isinstance(target, str) or not target.startswith(PANE_PREFIX):
        return None
    table = windows()
    if target not in table:
        table[target] = {"name": None, "target": target, "pane": True, "composer": ""}
        save_windows(table)
    return target


def list_windows(argv):
    template = flag(argv, "-F") or "#{window_id}"
    target = flag(argv, "-t")
    if unknown_session(target):
        print(f"can't find session: {target}", file=sys.stderr)
        return 1
    for window_id, window in windows().items():
        if window.get("pane"):
            continue
        if target is not None and window.get("target") not in (None, target):
            continue
        print(fill(template, window_id, window))
    return 0


def kill_window(argv):
    table = windows()
    target = flag(argv, "-t")
    if target not in table:
        print(f"can't find window: {target}", file=sys.stderr)
        return 1
    del table[target]
    save_windows(table)
    return 0


def send_keys(argv):
    """Keys sent as a command are run; keys sent literally are typed and nothing else.

    `-l` is how tmux is told the argument is text for whoever is reading the pane rather than a
    line for a shell — the driver instructing a child sends it that way, and running that text
    through `sh` would be the stub answering for a child that never saw it. Every call is recorded
    either way, so a test reads what was typed out of `tmux-calls.jsonl`.
    """
    target = flag(argv, "-t")
    sent = argv[argv.index(target) + 1:]
    keys = [value for value in sent if value != "Enter"]
    if target not in windows():
        loose_pane(target)
    table = windows()
    if target not in table:
        # What a real tmux answers for a window that has gone, which is how a run learns that the
        # child it is holding a recorded id for cannot be reached any more.
        print(f"can't find window: {target}", file=sys.stderr)
        return 1
    if "-l" in argv:
        literal = argv[argv.index("-l") + 1:]
        option_terminated = literal and literal[0] == "--"
        if option_terminated:
            literal = literal[1:]
        if not option_terminated and literal and literal[0].startswith("-"):
            print("command send-keys: invalid flag -", file=sys.stderr)
            return 1
        table[target]["composer"] = table[target].get("composer", "") + "".join(literal)
        save_windows(table)
        return 0
    if sent == ["S-Enter"]:
        table[target]["composer"] = table[target].get("composer", "") + "\n"
        save_windows(table)
        return 0
    if sent == ["Enter"]:
        if (state_dir() / "tmux-ignore-enter").exists():
            return 0
        dropped = state_dir() / "tmux-drop-enter-once"
        if dropped.exists():
            dropped.unlink()
            return 0
        table[target]["composer"] = ""
        save_windows(table)
        return 0
    if all(key in ANSWER_KEYS for key in sent):
        return 0
    window = table.get(target, {})
    environment = dict(os.environ)
    environment.update(window.get("env") or {})
    result = subprocess.run(
        ["sh", "-c", " ".join(keys)], cwd=window.get("cwd") or os.getcwd(), env=environment,
    )
    return result.returncode


def serve_one_command():
    """Handle one client command while the stub server owns its shared state."""
    argv = sys.argv[1:]
    with (state_dir() / "tmux-calls.jsonl").open("a") as handle:
        handle.write(json.dumps({"argv": argv}) + "\n")

    if (state_dir() / "tmux-server-gone").exists():
        print("error connecting to tmux server (No such file or directory)", file=sys.stderr)
        return 1

    if not argv:
        return 1
    command = argv[0]

    if command == "new-window":
        return new_window(argv)
    if command == "list-windows":
        return list_windows(argv)
    if command == "kill-window":
        return kill_window(argv)
    if command == "send-keys":
        return send_keys(argv)
    if command == "capture-pane":
        target = flag(argv, "-t")
        if target not in windows():
            loose_pane(target)
        if target not in windows():
            print(f"can't find window: {target}", file=sys.stderr)
            return 1
        composer = windows()[target].get("composer", "")
        print(f"❯ {composer.splitlines()[-1] if composer else ''}".rstrip())
        return 0
    if command == "display-message" and "-p" in argv:
        if argv[-1] == "#{cursor_y}":
            print("0")
            return 0
        path = state_dir() / "tmux-session"
        if path.exists():
            print(path.read_text().strip())
        return 0
    return 0


def main():
    """Serialize process-per-command clients like one tmux server."""
    with (state_dir() / "tmux-command.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return serve_one_command()


if __name__ == "__main__":
    sys.exit(main())
