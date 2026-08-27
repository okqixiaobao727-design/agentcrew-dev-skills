#!/usr/bin/env python3
"""Test double for the `codex` CLI, driven by scenario files.

Emulates the two invocations codex_bridge.py makes:

    codex app-server --listen unix://<socket>
    codex --remote unix://<socket> --sandbox <s> --ask-for-approval <a> \
        resume <thread-id>

The scenario is read from `.codex-stub-scenario` in the working directory
(falling back to $CODEX_STUB_SCENARIO, then "receipt"):

    receipt      first turn completes with a CREW COMPLETE receipt
    question     first turn completes with a clarifying question
    failed-turn  first turn ends with turn status "failed"
    slow         first turn completes (with a receipt) only once a file named
                 `stub-release` appears in the working directory
    tui-exit     the TUI process exits immediately (vanished window)
    no-server    the app-server exits without creating its socket

Follow-up turns started via turn/start always complete with a receipt.

A file named `stub-typed-turn` in the working directory is the turn nobody sent through the
bridge: the operator typing into the TUI pane. Its contents become the agent's final message on
a turn carrying no bridge marker, which is what a session answering a hand-delivered ruling looks
like from the outside. `stub-typed-turn-held` is the same turn still being worked: it never
completes, so the thread holds a finished turn behind an unfinished one.
"""

import os
import sys

TUI_EXIT_MARKER = ".codex-stub-tui-exited"


def exits_immediately():
    argv = sys.argv[1:]
    if (argv and argv[0] == "app-server") or argv[:3] == ["plugin", "list", "--json"]:
        return False
    try:
        with open(".codex-stub-scenario", encoding="utf-8") as stream:
            active_scenario = stream.read().strip()
    except OSError:
        active_scenario = os.environ.get("CODEX_STUB_SCENARIO", "receipt")
    return active_scenario == "tui-exit"


if exits_immediately():
    print("stub TUI exiting immediately", file=sys.stderr, flush=True)
    with open(TUI_EXIT_MARKER, "w", encoding="utf-8"):
        pass
    os._exit(1)


import asyncio
import json
import pathlib
import time

STUB_SHA = "1234567890abcdef1234567890abcdef12345678"
TURN_DELAY_SECONDS = float(os.environ.get("CODEX_STUB_DELAY", "0.3"))
MATERIALIZED_THREADS_FILE = pathlib.Path("stub-materialized-threads")


def scenario():
    override = pathlib.Path(".codex-stub-scenario")
    if override.is_file():
        return override.read_text(encoding="utf-8").strip()
    return os.environ.get("CODEX_STUB_SCENARIO", "receipt")


def receipt_message():
    return f"Ticket done, receipt follows.\nCREW COMPLETE {STUB_SHA}"


def first_turn_result(active_scenario):
    if active_scenario == "question":
        return "completed", "Should I extend the existing view or create a new one?"
    if active_scenario == "escalation":
        return "completed", (
            "CREW ASK 18 scope — choose option A or option B.\n"
            "ts=1755060060"
        )
    if active_scenario == "message":
        return "completed", "ordinary child update"
    if active_scenario == "failed-turn":
        return "failed", ""
    return "completed", receipt_message()


class StubThread:
    def __init__(self, thread_id="stub-thread-1", materialized=False):
        self.id = thread_id
        self.preview = ""
        self.turns = []
        self.materialized = materialized

    def start_turn(self, text):
        kind = "first" if not self.turns else "followup"
        if not self.preview:
            self.preview = text
        self.materialized = True
        self.turns.append(
            {"kind": kind, "text": text, "created": time.monotonic()}
        )
        return len(self.turns) - 1

    def absorb_typed_turn(self):
        """Take up a turn typed into the pane, once, if a scenario file has left one there."""
        if any(turn["kind"] == "typed" for turn in self.turns):
            return
        for name, held in (("stub-typed-turn", False), ("stub-typed-turn-held", True)):
            typed = pathlib.Path(name)
            if not typed.is_file():
                continue
            self.turns.append({
                "kind": "typed",
                "text": "a ruling typed straight into the pane",
                "answer": typed.read_text(encoding="utf-8"),
                "held": held,
                "created": time.monotonic(),
            })
            return

    def render_turn(self, index, active_scenario):
        turn = self.turns[index]
        elapsed = time.monotonic() - turn["created"]
        items = [
            {
                "type": "userMessage",
                "content": [{"type": "text", "text": turn["text"]}],
            }
        ]
        status = "inProgress"
        message = ""
        if turn["kind"] == "first" and active_scenario == "slow":
            if pathlib.Path("stub-release").exists():
                status, message = "completed", receipt_message()
        elif elapsed >= TURN_DELAY_SECONDS:
            if turn["kind"] == "first":
                status, message = first_turn_result(active_scenario)
            elif turn["kind"] == "typed":
                if not turn["held"]:
                    status, message = "completed", turn["answer"]
            else:
                status, message = "completed", receipt_message()
        if status == "completed" and message:
            items.append(
                {"type": "agentMessage", "phase": "final_answer", "text": message}
            )
        return {"id": f"stub-turn-{index}", "status": status, "items": items}

    def render(self, active_scenario):
        return {
            "id": self.id,
            "preview": self.preview,
            "turns": [
                self.render_turn(index, active_scenario)
                for index in range(len(self.turns))
            ],
        }


class StubServer:
    def __init__(self, active_scenario):
        self.scenario = active_scenario
        self.thread = None

    def ensure_thread(self):
        if self.thread is not None:
            self.thread.absorb_typed_turn()

    def materialized_thread_ids(self):
        try:
            return set(MATERIALIZED_THREADS_FILE.read_text(encoding="utf-8").splitlines())
        except FileNotFoundError:
            return set()

    def remember_materialized_thread(self, thread_id):
        known = self.materialized_thread_ids()
        if thread_id in known:
            return
        known.add(thread_id)
        MATERIALIZED_THREADS_FILE.write_text(
            "".join(f"{item}\n" for item in sorted(known)),
            encoding="utf-8",
        )

    def handle(self, method, params):
        self.ensure_thread()
        with pathlib.Path("stub-requests.jsonl").open("a", encoding="utf-8") as stream:
            json.dump({"method": method, "params": params}, stream)
            stream.write("\n")
        if method == "initialize":
            return {}
        if method == "thread/start":
            self.thread = StubThread()
            return {"thread": {"id": self.thread.id}}
        if method == "thread/resume":
            thread_id = params["threadId"]
            if self.thread is not None and self.thread.id == thread_id:
                if not self.thread.materialized:
                    raise ValueError(f"no rollout found for thread id {thread_id}")
            elif thread_id in self.materialized_thread_ids():
                self.thread = StubThread(thread_id=thread_id, materialized=True)
            else:
                raise ValueError(f"no rollout found for thread id {thread_id}")
            return {"thread": {"id": self.thread.id}}
        if method == "thread/list":
            data = []
            if self.thread is not None:
                data.append({"id": self.thread.id, "preview": self.thread.preview})
            return {"data": data}
        if method == "thread/read":
            if self.thread is None or params.get("threadId") != self.thread.id:
                raise ValueError("unknown thread")
            if not self.thread.materialized:
                raise ValueError(f"thread {self.thread.id} is not materialized yet")
            return {"thread": self.thread.render(self.scenario)}
        if method == "turn/start":
            if self.thread is None or params.get("threadId") != self.thread.id:
                raise ValueError("unknown thread")
            text = "".join(
                part.get("text", "") for part in params.get("input") or []
            )
            index = self.thread.start_turn(text)
            self.remember_materialized_thread(self.thread.id)
            return {"turn": {"id": f"stub-turn-{index}", "status": "inProgress"}}
        raise ValueError(f"unsupported method: {method}")

    async def websocket_handler(self, request):
        from aiohttp import web, WSMsgType

        websocket = web.WebSocketResponse()
        await websocket.prepare(request)
        async for message in websocket:
            if message.type != WSMsgType.TEXT:
                break
            payload = json.loads(message.data)
            if payload.get("id") is None:
                continue
            try:
                result = self.handle(payload.get("method"), payload.get("params") or {})
                await websocket.send_json({"id": payload["id"], "result": result})
            except ValueError as error:
                await websocket.send_json(
                    {"id": payload["id"], "error": {"code": -32600, "message": str(error)}}
                )
        return websocket


async def run_app_server(socket_path):
    from aiohttp import web

    server = StubServer(scenario())
    app = web.Application()
    app.router.add_get("/", server.websocket_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.UnixSite(runner, path=str(socket_path))
    await site.start()
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()


def app_server_main(argv):
    if scenario() == "no-server":
        print("stub app-server refusing to start", file=sys.stderr)
        return 1
    listen = argv[argv.index("--listen") + 1]
    socket_path = pathlib.Path(listen.removeprefix("unix://"))
    try:
        asyncio.run(run_app_server(socket_path))
    except KeyboardInterrupt:
        pass
    return 0


async def resume_tui_thread(remote, thread_id):
    from aiohttp import ClientSession, UnixConnector, WSMsgType

    connector = UnixConnector(path=remote.removeprefix("unix://"))
    async with ClientSession(connector=connector) as session:
        async with session.ws_connect("http://localhost/") as websocket:
            request_id = 1

            async def request(method, params):
                nonlocal request_id
                current_id = request_id
                request_id += 1
                await websocket.send_json(
                    {"id": current_id, "method": method, "params": params}
                )
                while True:
                    message = await websocket.receive()
                    if message.type != WSMsgType.TEXT:
                        raise RuntimeError(
                            f"unexpected app-server WebSocket message type: {message.type}"
                        )
                    payload = json.loads(message.data)
                    if payload.get("id") != current_id:
                        continue
                    if payload.get("error"):
                        error = payload["error"]
                        raise RuntimeError(error.get("message", str(error)))
                    return payload.get("result", {})

            await request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "stub_codex_tui",
                        "title": "Stub Codex TUI",
                        "version": "0.1.0",
                    },
                    "capabilities": {"experimentalApi": False},
                },
            )
            await websocket.send_json({"method": "initialized", "params": {}})
            await request("thread/resume", {"threadId": thread_id})


def tui_main(argv):
    if scenario() == "tui-exit":
        print("stub TUI exiting immediately", file=sys.stderr)
        return 1
    remote = argv[argv.index("--remote") + 1]
    thread_id = argv[argv.index("resume") + 1]
    try:
        asyncio.run(resume_tui_thread(remote, thread_id))
    except Exception as error:
        print(f"stub TUI resume failed: {error}", file=sys.stderr, flush=True)
        return 1
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    return 0


def main():
    argv = sys.argv[1:]
    if argv[:3] == ["plugin", "list", "--json"]:
        plugin_root = os.environ["CODEX_STUB_PLUGIN_ROOT"]
        print(json.dumps({
            "installed": [{
                "name": "mattpocock-skills",
                "marketplaceName": "mattpocock",
                "version": os.environ.get("CODEX_STUB_PLUGIN_VERSION", "1.2.3"),
                "installed": True,
                "enabled": True,
                "source": {"source": "local", "path": plugin_root},
            }]
        }))
        return 0
    with pathlib.Path("stub-argv.jsonl").open("a", encoding="utf-8") as stream:
        json.dump(argv, stream)
        stream.write("\n")
    if argv and argv[0] == "app-server":
        return app_server_main(argv)
    return tui_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
