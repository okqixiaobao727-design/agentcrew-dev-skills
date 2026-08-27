#!/usr/bin/env python3
"""Bridge between a Claude orchestrator and Codex sessions in tmux.

Every Codex session (child or integrator) is launched as a tmux window running
the Codex TUI attached to a private `codex app-server` unix socket. The
orchestrator talks to the session through this CLI:

    launch  start app-server + TUI window, submit the first turn, write a state file
    send    submit a follow-up turn (answer, fix-up request) to a session
    watch   block while every watched session is busy; exit with a JSON snapshot
            as soon as any session is idle (turn finished) or vanished
    stop    kill a session's window and runtime

Each command prints one JSON object on stdout. Exit 0 on success, 1 on error.

`watch` reads the thread's latest finished turn rather than the turn `send` started: a
coordinator answers in the pane as readily as through this CLI, and a turn that carries no marker
of ours is still the session speaking. The launch marker is only the pane's proof that the first
turn it posted has materialised before the TUI attaches; thread identity comes from the pane's
single bootstrap result.
What is copied to the machine log is keyed on the message rather than on the busy-to-idle edge
that carried it, because an edge is seen only by the watch that happens to be polling either side
of it, and one missed edge used to drop a child's last word for good.
"""

import argparse
import asyncio
import json
import os
import pathlib
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

try:
    import aiohttp
except ImportError as error:
    raise SystemExit(
        "codex_bridge requires Python package 'aiohttp'. "
        "Install it for the Python interpreter used by Claude Code."
    ) from error


STATE_VERSION = 1
TERMINAL_TURN_STATUSES = {"completed", "failed", "interrupted"}
DEFAULT_STARTUP_TIMEOUT_SECONDS = 60
DEFAULT_WATCH_TIMEOUT_SECONDS = 7200
DEFAULT_WATCH_INTERVAL_SECONDS = 2.0
TUI_STARTUP_LIVENESS_SECONDS = 0.25
PROCESS_POLL_INTERVAL_SECONDS = 0.05
CONSECUTIVE_FAILURE_LIMIT = 3
MARKER_PREFIX = "agentcrew"
BOOTSTRAP_RESULT_NAME = "bootstrap-result.json"
MACHINE_LOG = pathlib.Path(__file__).resolve().parent.parent / "machine_log.py"
SKILL_PLUGIN_NAME = "mattpocock-skills"


class BridgeError(RuntimeError):
    pass


class AppServerError(BridgeError):
    pass


def new_marker():
    return f"[{MARKER_PREFIX}:{uuid.uuid4()}]"


def write_json_atomic(path, payload):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    try:
        with temporary:
            json.dump(payload, temporary, ensure_ascii=False, sort_keys=True)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary.name, path)
    except Exception:
        pathlib.Path(temporary.name).unlink(missing_ok=True)
        raise


def read_state(path):
    try:
        state = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise BridgeError(f"Unknown session state file: {path}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise BridgeError(f"Unreadable session state file: {path}") from error
    if state.get("version") != STATE_VERSION:
        raise BridgeError(
            f"Unsupported session state version in {path}: {state.get('version')}"
        )
    return state


def pane_exists(pane_id):
    """Whether tmux's pane list holds `pane_id`; raises where that list could not be read.

    Only a successful observation can say a child is gone (#140). A tmux call that did not run —
    a client killed by a signal, a server momentarily unreachable, no tmux on PATH at all — knows
    nothing about the pane, and answering `False` there told every caller the window had exited.
    The failure is raised as an `OSError`, which is the family `cmd_watch` already counts and
    retries, so a look that failed costs a poll instead of a ticket.

    A `False` therefore has one meaning left: the pane list was read and this pane is not in it.
    """
    # display-message -t on a dead pane exits 0 on tmux >= 3.6, so test
    # membership in the full pane list instead.
    result = subprocess.run(
        ["tmux", "list-panes", "-a", "-F", "#{pane_id}"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise OSError(
            f"tmux list-panes exited {result.returncode}: "
            f"{(result.stderr or result.stdout).strip()}"
        )
    return pane_id in result.stdout.split()


def kill_window(window_id):
    subprocess.run(
        ["tmux", "kill-window", "-t", window_id],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def read_log_tail(path, limit=4000):
    try:
        text = pathlib.Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-limit:].strip()


def wait_for_path(path, timeout_seconds):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if os.path.exists(path):
            return True
        time.sleep(PROCESS_POLL_INTERVAL_SECONDS)
    return False


def process_exit_within(process, timeout_seconds):
    """Return the process's exit code if it dies within the bounded startup window."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        exit_code = process.poll()
        if exit_code is not None:
            return exit_code
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(PROCESS_POLL_INTERVAL_SECONDS, remaining))


def terminate_process(process):
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


class AppServerClient:
    def __init__(self, socket_path):
        self.socket_path = socket_path
        self.next_id = 1

    async def __aenter__(self):
        connector = aiohttp.UnixConnector(path=self.socket_path)
        self.session = aiohttp.ClientSession(connector=connector)
        try:
            self.websocket = await self.session.ws_connect("http://localhost/")
            await self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "agentcrew_codex_bridge",
                        "title": "AgentCrew Codex Bridge",
                        "version": "0.1.0",
                    },
                    "capabilities": {"experimentalApi": False},
                },
            )
            await self.websocket.send_json({"method": "initialized", "params": {}})
        except Exception:
            await self.session.close()
            raise
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        await self.websocket.close()
        await self.session.close()

    async def request(self, method, params):
        request_id = self.next_id
        self.next_id += 1
        await self.websocket.send_json(
            {"id": request_id, "method": method, "params": params}
        )

        while True:
            message = await self.websocket.receive()
            if message.type != aiohttp.WSMsgType.TEXT:
                raise AppServerError(
                    f"Unexpected app-server WebSocket message type: {message.type}"
                )

            payload = json.loads(message.data)
            if payload.get("id") == request_id:
                if payload.get("error"):
                    error = payload["error"]
                    raise AppServerError(
                        f"{method} failed: {error.get('message', error)}"
                    )
                return payload.get("result", {})

            if payload.get("id") is not None and payload.get("method"):
                await self.websocket.send_json(
                    {
                        "id": payload["id"],
                        "error": {
                            "code": -32601,
                            "message": (
                                "The AgentCrew Codex bridge does not answer "
                                f"server request {payload['method']}."
                            ),
                        },
                    }
                )


def latest_terminal_turn(thread):
    """The last turn of `thread` that has finished, whatever started it, or None where none has.

    Whatever started it, because a session answers rulings that never came through `send`: a
    coordinator types into the pane, and the turn that follows carries no marker of ours. Reading
    only the marked turn made every such answer invisible for as long as the session lived.

    The last *finished* one rather than the last one, because a turn can be started on top of a
    turn whose message nobody has read yet — the session finishes, the operator types the next
    thing before a watch polls — and the message under it is only ever in the thread.
    """
    for turn in reversed(thread.get("turns") or []):
        if turn.get("status") in TERMINAL_TURN_STATUSES:
            return turn
    return None


def final_agent_message(turn):
    messages = [
        item.get("text", "")
        for item in turn.get("items") or []
        if item.get("type") == "agentMessage"
        and item.get("phase") == "final_answer"
    ]
    if messages:
        return messages[-1]
    fallback = [
        item.get("text", "")
        for item in turn.get("items") or []
        if item.get("type") == "agentMessage"
    ]
    return fallback[-1] if fallback else ""


async def wait_for_turn_marker(
    client,
    thread_id,
    marker,
    timeout_seconds,
):
    """Wait until a prepared thread contains the newly submitted launch marker."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            result = await client.request(
                "thread/read", {"threadId": thread_id, "includeTurns": True}
            )
        except AppServerError as error:
            if "not materialized yet" not in str(error):
                raise
            await asyncio.sleep(0.25)
            continue
        thread = result.get("thread") or {}
        for turn in thread.get("turns") or []:
            for item in turn.get("items") or []:
                if item.get("type") != "userMessage":
                    continue
                text = "".join(
                    part.get("text", "") for part in item.get("content") or []
                )
                if marker in text:
                    return
        await asyncio.sleep(0.25)
    raise BridgeError("Timed out waiting for the Codex launch turn")


def read_prompt(args):
    if args.prompt is not None:
        return args.prompt
    if args.prompt_file:
        return pathlib.Path(args.prompt_file).read_text(encoding="utf-8")
    raise BridgeError("Provide --prompt or --prompt-file")


def opening_skill_name(prompt):
    """Return the skill named at the start of the prompt, if one is present."""
    match = re.match(r"^\$([A-Za-z0-9][A-Za-z0-9_-]*)\b", prompt)
    return match.group(1) if match else None


def resolve_skill_path(skill_name):
    """Return the installed mattpocock skill's unique existing SKILL.md path."""
    result = subprocess.run(
        ["codex", "plugin", "list", "--json"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "command failed"
        raise BridgeError(f"Cannot list installed Codex plugins: {detail}")
    try:
        plugins = json.loads(result.stdout).get("installed") or []
    except (AttributeError, json.JSONDecodeError) as error:
        raise BridgeError("Cannot read installed Codex plugins: invalid JSON") from error
    plugin = next(
        (
            item
            for item in plugins
            if item.get("name") == SKILL_PLUGIN_NAME
            and item.get("installed")
            and item.get("enabled")
        ),
        None,
    )
    if plugin is None:
        raise BridgeError(f"Installed Codex plugin {SKILL_PLUGIN_NAME!r} is unavailable")

    plugin_roots = []
    marketplace_name = plugin.get("marketplaceName")
    plugin_name = plugin.get("name")
    plugin_version = plugin.get("version")
    if marketplace_name and plugin_name and plugin_version:
        codex_home = pathlib.Path(
            os.environ.get("CODEX_HOME", pathlib.Path.home() / ".codex")
        ).expanduser()
        cache_root = (
            codex_home
            / "plugins"
            / "cache"
            / marketplace_name
            / plugin_name
            / plugin_version
        )
        if cache_root.is_dir():
            plugin_roots.append(cache_root)

    source_path = (plugin.get("source") or {}).get("path")
    if source_path:
        plugin_roots.append(pathlib.Path(source_path).expanduser())

    for plugin_root in plugin_roots:
        matches = list(plugin_root.glob(f"skills/*/{skill_name}/SKILL.md"))
        if len(matches) == 1 and matches[0].is_file():
            return matches[0].resolve()
        if len(matches) > 1:
            break

    raise BridgeError(
        f"Skill {skill_name!r} has no unique existing SKILL.md in "
        f"installed Codex plugin {SKILL_PLUGIN_NAME!r}"
    )


def turn_input(marker, message):
    """Return app-server input items, resolving an opening skill when present."""
    inputs = [
        {
            "type": "text",
            "text": f"{marker}\n{message}",
            "text_elements": [],
        }
    ]
    skill_name = opening_skill_name(message)
    if skill_name:
        inputs.append(
            {
                "type": "skill",
                "name": skill_name,
                "path": str(resolve_skill_path(skill_name)),
            }
        )
    return inputs


async def prepare_launch_thread(socket_path, args):
    """Return the thread id after creating or resuming the launch thread."""
    client = AppServerClient(socket_path)
    await client.__aenter__()
    try:
        if args.thread_id:
            await client.request("thread/resume", {"threadId": args.thread_id})
            thread_id = args.thread_id
        else:
            result = await client.request(
                "thread/start",
                {
                    "cwd": args.cwd,
                    "approvalPolicy": args.approval,
                    "sandbox": args.sandbox,
                },
            )
            thread_id = result["thread"]["id"]
        return thread_id
    finally:
        await client.__aexit__(None, None, None)


async def post_launch_turn(socket_path, thread_id, marker, inputs, timeout_seconds):
    """Start the prepared launch turn and wait until its rollout is readable."""
    client = AppServerClient(socket_path)
    await client.__aenter__()
    try:
        await client.request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": inputs,
            },
        )
        await wait_for_turn_marker(
            client,
            thread_id,
            marker,
            timeout_seconds,
        )
    finally:
        await client.__aexit__(None, None, None)


def bootstrap_error(result, default_log_path):
    """Return the pane's failed bootstrap as one caller-facing error message."""
    message = result.get("error") or "Codex pane bootstrap failed"
    log_path = result.get("logPath") or default_log_path
    detail = read_log_tail(log_path)
    if detail and detail not in message:
        message = f"{message}: {detail}"
    return BridgeError(message)


async def wait_for_bootstrap_result(result_path, pane_id, timeout_seconds, log_path):
    """Return the pane's one atomic bootstrap result, or raise its recorded failure."""
    result_path = pathlib.Path(result_path)
    deadline = time.monotonic() + timeout_seconds
    last_pane_error = None
    while time.monotonic() < deadline:
        if result_path.is_file():
            break
        try:
            alive = pane_exists(pane_id)
        except OSError as error:
            last_pane_error = error
        else:
            if not alive:
                if result_path.is_file():
                    break
                detail = read_log_tail(log_path)
                raise BridgeError(
                    detail or "Codex pane exited before reporting its bootstrap result"
                )
        await asyncio.sleep(PROCESS_POLL_INTERVAL_SECONDS)
    else:
        detail = read_log_tail(log_path)
        suffix = detail or str(last_pane_error or "no result was written")
        raise BridgeError(f"Timed out waiting for Codex pane bootstrap: {suffix}")

    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BridgeError(f"Unreadable Codex pane bootstrap result: {result_path}") from error
    if result.get("ok") is False:
        raise bootstrap_error(result, log_path)
    thread_id = result.get("threadId")
    if result.get("ok") is not True or not isinstance(thread_id, str) or not thread_id:
        raise BridgeError(f"Invalid Codex pane bootstrap result: {result_path}")
    return result


def log_message(state, role, message, log=None, ticket=None):
    """Copy a bridge message through the machine-log writer's existing schema."""
    log = log if log is not None else state.get("machineLog")
    ticket = ticket if ticket is not None else state.get("ticket")
    if not log:
        return
    command = [
        sys.executable,
        str(MACHINE_LOG),
        "--log",
        os.path.abspath(str(log)),
        "message",
        "--role",
        role,
        "--message",
        message,
    ]
    if ticket is not None:
        command.extend(["--ticket", str(ticket)])
    try:
        subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        pass


def model_config_overrides(args):
    overrides = []
    if args.model:
        overrides.extend(["-c", f"model={json.dumps(args.model)}"])
    if args.effort:
        overrides.extend(
            ["-c", f"model_reasoning_effort={json.dumps(args.effort)}"]
        )
    return overrides


def launch_window(args, runtime_dir, input_file):
    pane_command = [
        sys.executable,
        str(pathlib.Path(__file__).resolve()),
        "_pane",
        "--runtime-dir",
        str(runtime_dir),
        "--input-file",
        str(input_file),
        "--cwd",
        args.cwd,
        "--sandbox",
        args.sandbox,
        "--approval",
        args.approval,
        "--startup-timeout",
        str(args.startup_timeout),
    ]
    if args.thread_id:
        pane_command.extend(["--thread-id", args.thread_id])
    if args.model:
        pane_command.extend(["--model", args.model])
    if args.effort:
        pane_command.extend(["--effort", args.effort])

    tmux_command = [
        "tmux",
        "new-window",
        "-d",
        "-P",
        "-F",
        "#{window_id}\t#{pane_id}",
        "-t",
        args.tmux_session,
        "-n",
        args.window_name,
        "-c",
        args.cwd,
        shlex.join(pane_command),
    ]
    result = subprocess.run(
        tmux_command,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise BridgeError(result.stderr.strip() or "tmux new-window failed")
    try:
        window_id, pane_id = result.stdout.strip().split("\t")
    except ValueError as error:
        raise BridgeError(
            f"Unexpected tmux new-window output: {result.stdout!r}"
        ) from error
    return window_id, pane_id


def run_pane(args):
    runtime_dir = pathlib.Path(args.runtime_dir)
    socket_path = runtime_dir / "app-server.sock"
    log_path = runtime_dir / "app-server.log"
    result_path = runtime_dir / BOOTSTRAP_RESULT_NAME
    inputs = json.loads(pathlib.Path(args.input_file).read_text(encoding="utf-8"))
    marker = inputs[0]["text"].splitlines()[0]
    log_file = log_path.open("a", encoding="utf-8")
    tui = None
    launch_confirmed = False
    app_server_command = [
        "codex",
        "app-server",
        "--listen",
        f"unix://{socket_path}",
    ]
    app_server_command.extend(model_config_overrides(args))
    app_server = subprocess.Popen(
        app_server_command,
        cwd=args.cwd,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        text=True,
    )

    def cleanup(*_ignored):
        if tui is not None:
            terminate_process(tui)
        terminate_process(app_server)
        if launch_confirmed:
            shutil.rmtree(runtime_dir, ignore_errors=True)

    def close_log():
        if not log_file.closed:
            log_file.flush()
            log_file.close()

    def publish_failure(message):
        print(message, file=log_file, flush=True)
        try:
            write_json_atomic(
                result_path,
                {
                    "ok": False,
                    "error": message,
                    "logPath": str(log_path.resolve()),
                },
            )
        except Exception as error:
            print(
                f"Codex bootstrap failure result could not be written: {error}",
                file=log_file,
                flush=True,
            )
        return 1

    def stop_and_exit(signum, _frame):
        close_log()
        cleanup()
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGHUP, stop_and_exit)
    signal.signal(signal.SIGTERM, stop_and_exit)
    signal.signal(signal.SIGINT, stop_and_exit)

    try:
        if not wait_for_path(socket_path, args.startup_timeout):
            detail = read_log_tail(log_path) or "app-server socket did not appear"
            return publish_failure(f"Codex app-server failed to start: {detail}")

        try:
            thread_id = asyncio.run(prepare_launch_thread(socket_path, args))
        except Exception as error:
            return publish_failure(f"Codex turn failed to start: {error}")

        try:
            asyncio.run(
                post_launch_turn(
                    socket_path,
                    thread_id,
                    marker,
                    inputs,
                    args.startup_timeout,
                )
            )
        except Exception as error:
            return publish_failure(f"Codex turn failed to confirm: {error}")

        command = [
            "codex",
            "--remote",
            f"unix://{socket_path}",
            "--sandbox",
            args.sandbox,
            "--ask-for-approval",
            args.approval,
        ]
        command.extend(model_config_overrides(args))
        command.extend(["resume", thread_id])
        try:
            tui = subprocess.Popen(command, cwd=args.cwd, text=True)
        except Exception as error:
            return publish_failure(f"Codex TUI failed to start: {error}")
        tui_exit_code = process_exit_within(tui, TUI_STARTUP_LIVENESS_SECONDS)
        if tui_exit_code is not None:
            return publish_failure(
                "Codex TUI exited before the turn was confirmed"
                f" (exit code {tui_exit_code})"
            )
        try:
            write_json_atomic(
                result_path,
                {
                    "ok": True,
                    "threadId": thread_id,
                },
            )
        except Exception as error:
            return publish_failure(f"Codex bootstrap result failed to write: {error}")
        launch_confirmed = True
        return tui.wait()
    finally:
        close_log()
        cleanup()


def build_state(args, runtime_dir, window_id, pane_id, thread_id, marker):
    now = time.time()
    state = {
        "version": STATE_VERSION,
        "name": args.window_name,
        "cwd": str(pathlib.Path(args.cwd).resolve()),
        "runtimeDir": str(runtime_dir),
        "socketPath": str(runtime_dir / "app-server.sock"),
        "windowId": window_id,
        "paneId": pane_id,
        "threadId": thread_id,
        "model": args.model,
        "effort": args.effort,
        "marker": marker,
        "status": "busy",
        "turnStatus": None,
        "finalMessage": None,
        "createdAt": now,
        "updatedAt": now,
    }
    if args.machine_log:
        state["machineLog"] = os.path.abspath(args.machine_log)
    if args.ticket is not None:
        state["ticket"] = args.ticket
    return state


def inherit_resume_pins(args):
    if not args.thread_id or not pathlib.Path(args.state_file).is_file():
        return
    try:
        previous_state = read_state(args.state_file)
    except BridgeError:
        return
    if not args.model:
        args.model = previous_state.get("model")
    if not args.effort:
        args.effort = previous_state.get("effort")
    if not args.machine_log:
        args.machine_log = previous_state.get("machineLog")
    if not args.ticket:
        args.ticket = previous_state.get("ticket")


async def cmd_launch(args):
    cwd = pathlib.Path(args.cwd)
    if not cwd.is_dir():
        raise BridgeError(f"Working directory does not exist: {args.cwd}")
    args.cwd = str(cwd.resolve())
    inherit_resume_pins(args)
    marker = new_marker()
    message = read_prompt(args)
    inputs = turn_input(marker, message)

    runtime_dir = pathlib.Path(tempfile.mkdtemp(prefix="agentcrew-codex-"))
    input_file = runtime_dir / "input.json"
    input_file.write_text(json.dumps(inputs, ensure_ascii=False), encoding="utf-8")

    try:
        window_id, pane_id = launch_window(args, runtime_dir, input_file)
    except Exception:
        shutil.rmtree(runtime_dir, ignore_errors=True)
        raise

    try:
        result = await wait_for_bootstrap_result(
            runtime_dir / BOOTSTRAP_RESULT_NAME,
            pane_id,
            args.startup_timeout,
            runtime_dir / "app-server.log",
        )
        thread_id = result["threadId"]
    except Exception:
        kill_window(window_id)
        raise

    state = build_state(args, runtime_dir, window_id, pane_id, thread_id, marker)
    write_json_atomic(args.state_file, state)
    print(
        json.dumps(
            {
                "ok": True,
                "stateFile": str(args.state_file),
                "windowId": window_id,
                "paneId": pane_id,
                "threadId": thread_id,
            },
            ensure_ascii=False,
        )
    )
    return 0


async def connect_existing(state, timeout_seconds=3):
    deadline = time.monotonic() + timeout_seconds
    last_error = None
    while time.monotonic() < deadline:
        if not pane_exists(state["paneId"]):
            raise BridgeError(
                f"Session {state['name']} vanished: its tmux window is gone"
            )
        try:
            client = AppServerClient(state["socketPath"])
            return await client.__aenter__()
        except (OSError, aiohttp.ClientError, AppServerError) as error:
            last_error = error
        await asyncio.sleep(0.1)
    raise BridgeError(
        f"Cannot reach app-server for session {state['name']}: {last_error}"
    )


async def cmd_send(args):
    state = read_state(args.state_file)
    marker = new_marker()
    message = read_prompt(args)

    client = await connect_existing(state)
    try:
        await client.request(
            "turn/start",
            {
                "threadId": state["threadId"],
                "input": turn_input(marker, message),
            },
        )
    finally:
        await client.__aexit__(None, None, None)

    log_message(
        state,
        "coordinator",
        message,
        log=args.machine_log,
        ticket=args.ticket,
    )
    state["marker"] = marker
    state["status"] = "busy"
    state["turnStatus"] = None
    # The recorded message is left standing rather than cleared: it is the message this turn is
    # answering, it has already been copied to the log, and forgetting it here would have the
    # next watch read the same turn out of the thread and copy it in a second time.
    state["updatedAt"] = time.time()
    write_json_atomic(args.state_file, state)
    print(
        json.dumps(
            {"ok": True, "stateFile": str(args.state_file), "threadId": state["threadId"]},
            ensure_ascii=False,
        )
    )
    return 0


async def evaluate_session(state):
    """Return (status, turn_status, final_message) for one session."""
    if not pane_exists(state["paneId"]):
        return "vanished", None, None
    client = AppServerClient(state["socketPath"])
    await client.__aenter__()
    try:
        result = await client.request(
            "thread/read", {"threadId": state["threadId"], "includeTurns": True}
        )
    finally:
        await client.__aexit__(None, None, None)
    thread = result.get("thread") or {}
    turns = thread.get("turns") or []
    turn = latest_terminal_turn(thread)
    message = final_agent_message(turn) if turn is not None else None
    # The session is what its last turn is doing; the message is the last thing it said, which is
    # a turn or more behind that whenever something was started on top of it.
    if turn is not None and turns[-1] is turn:
        return "idle", turn.get("status"), message
    return "busy", None, message


async def cmd_watch(args):
    state_files = [str(pathlib.Path(path).resolve()) for path in args.state_files]
    if len(set(state_files)) != len(state_files):
        raise BridgeError("Duplicate state files supplied to watch")
    for path in state_files:
        read_state(path)

    failures = {path: 0 for path in state_files}
    deadline = time.monotonic() + args.timeout
    while True:
        snapshot = []
        actionable = False
        for path in state_files:
            state = read_state(path)
            try:
                status, turn_status, message = await evaluate_session(state)
                failures[path] = 0
            except (OSError, aiohttp.ClientError, AppServerError) as error:
                failures[path] += 1
                if failures[path] >= CONSECUTIVE_FAILURE_LIMIT:
                    raise BridgeError(
                        f"Session {state['name']} is unreachable but its window "
                        f"is alive: {error}"
                    ) from error
                status, turn_status, message = "busy", None, None
            # Keyed on what the session said rather than on the edge it said it across. An edge
            # is seen once and only by the watch that happened to be polling either side of it:
            # a watch that started after one, or died across it, dropped the message for good.
            # The message itself keeps — the thread holds it for as long as the session lives,
            # and the state file holds the last one copied to the log — so a message differing
            # from the recorded one is a message to log, and an identical one is the same
            # observation read a second time.
            recorded = state.get("finalMessage")
            if message and message != recorded:
                log_message(state, "child", message)
            # A recorded message stands until another replaces it. `busy` carries none, and a
            # busy that is really a transport failure retried carries none either; forgetting the
            # last message there would log it again when the same turn is read next poll.
            kept = message or recorded
            if (
                status != state.get("status")
                or turn_status != state.get("turnStatus")
                or kept != recorded
            ):
                state["status"] = status
                state["turnStatus"] = turn_status
                state["finalMessage"] = kept
                state["updatedAt"] = time.time()
                write_json_atomic(path, state)
            row = {
                "stateFile": path,
                "name": state["name"],
                "status": status,
            }
            if status == "idle":
                row["turnStatus"] = turn_status
                row["finalMessage"] = message
            snapshot.append(row)
            if status != "busy":
                actionable = True
        if actionable:
            print(json.dumps({"sessions": snapshot}, ensure_ascii=False))
            return 0
        if time.monotonic() >= deadline:
            raise BridgeError(
                f"watch timed out after {args.timeout} seconds with every session busy"
            )
        await asyncio.sleep(args.interval)


async def cmd_stop(args):
    state = read_state(args.state_file)
    kill_window(state["windowId"])
    deadline = time.monotonic() + 2
    while pane_exists(state["paneId"]) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    shutil.rmtree(state["runtimeDir"], ignore_errors=True)
    state["status"] = "stopped"
    state["updatedAt"] = time.time()
    write_json_atomic(args.state_file, state)
    print(json.dumps({"ok": True, "stateFile": str(args.state_file)}, ensure_ascii=False))
    return 0


def add_prompt_arguments(parser):
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--prompt")
    group.add_argument("--prompt-file")


def add_log_arguments(parser):
    parser.add_argument(
        "--machine-log",
        help="the run's machine log, when logging is enabled",
    )
    parser.add_argument("--ticket", help="the ticket this bridge session serves")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    launch = commands.add_parser("launch", help="Start a Codex session in a tmux window")
    launch.add_argument("--cwd", required=True)
    launch.add_argument("--tmux-session", required=True,
                        help="tmux target for the new window, e.g. '$3:'")
    launch.add_argument("--window-name", required=True)
    launch.add_argument("--state-file", required=True)
    launch.add_argument("--thread-id",
                        help="Resume this Codex thread instead of starting fresh")
    launch.add_argument("--sandbox", default="danger-full-access")
    launch.add_argument("--approval", default="never")
    launch.add_argument("--model")
    launch.add_argument("--effort")
    add_log_arguments(launch)
    launch.add_argument("--startup-timeout", type=float,
                        default=DEFAULT_STARTUP_TIMEOUT_SECONDS)
    add_prompt_arguments(launch)

    send = commands.add_parser("send", help="Send a follow-up turn to a session")
    send.add_argument("--state-file", required=True)
    add_log_arguments(send)
    add_prompt_arguments(send)

    watch = commands.add_parser(
        "watch", help="Block until any watched session is idle or vanished"
    )
    watch.add_argument("state_files", nargs="+")
    watch.add_argument("--interval", type=float,
                       default=DEFAULT_WATCH_INTERVAL_SECONDS)
    watch.add_argument("--timeout", type=float,
                       default=DEFAULT_WATCH_TIMEOUT_SECONDS)

    stop = commands.add_parser("stop", help="Kill a session's window and runtime")
    stop.add_argument("--state-file", required=True)

    return parser


def build_pane_parser():
    pane_parser = argparse.ArgumentParser(add_help=False)
    pane_parser.add_argument("--runtime-dir", required=True)
    pane_parser.add_argument("--input-file", required=True)
    pane_parser.add_argument("--cwd", required=True)
    pane_parser.add_argument("--sandbox", required=True)
    pane_parser.add_argument("--approval", required=True)
    pane_parser.add_argument("--startup-timeout", type=float, required=True)
    pane_parser.add_argument("--thread-id")
    pane_parser.add_argument("--model")
    pane_parser.add_argument("--effort")
    return pane_parser


COMMANDS = {
    "launch": cmd_launch,
    "send": cmd_send,
    "watch": cmd_watch,
    "stop": cmd_stop,
}


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "_pane":
        args = build_pane_parser().parse_args(sys.argv[2:])
        return run_pane(args)
    args = build_parser().parse_args()
    try:
        return asyncio.run(COMMANDS[args.command](args))
    except (BridgeError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
