#!/usr/bin/env python3
"""Test double for the `codex` CLI, driven by scenario files.

Emulates the two invocations codex_bridge.py makes:

    codex app-server --listen unix://<socket>
    codex --remote unix://<socket> --sandbox <s> --ask-for-approval <a> \
        [resume <thread-id>] <prompt>

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
"""

import asyncio
import json
import os
import pathlib
import sys
import time

from aiohttp import web, WSMsgType

STUB_SHA = "1234567890abcdef1234567890abcdef12345678"
TURN_DELAY_SECONDS = float(os.environ.get("CODEX_STUB_DELAY", "0.3"))


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
    def __init__(self, prompt):
        self.id = "stub-thread-1"
        self.preview = prompt
        self.turns = [
            {"kind": "first", "text": prompt, "created": time.monotonic()}
        ]

    def start_turn(self, text):
        self.turns.append(
            {"kind": "followup", "text": text, "created": time.monotonic()}
        )
        return len(self.turns) - 1

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
    def __init__(self, socket_dir, active_scenario):
        self.prompt_file = socket_dir / "tui-prompt.txt"
        self.scenario = active_scenario
        self.thread = None

    def ensure_thread(self):
        if self.thread is None and self.prompt_file.is_file():
            self.thread = StubThread(
                self.prompt_file.read_text(encoding="utf-8")
            )

    def handle(self, method, params):
        self.ensure_thread()
        if method == "initialize":
            return {}
        if method == "thread/list":
            data = []
            if self.thread is not None:
                data.append({"id": self.thread.id, "preview": self.thread.preview})
            return {"data": data}
        if method == "thread/read":
            if self.thread is None or params.get("threadId") != self.thread.id:
                raise ValueError("unknown thread")
            return {"thread": self.thread.render(self.scenario)}
        if method == "turn/start":
            if self.thread is None or params.get("threadId") != self.thread.id:
                raise ValueError("unknown thread")
            text = "".join(
                part.get("text", "") for part in params.get("input") or []
            )
            index = self.thread.start_turn(text)
            return {"turn": {"id": f"stub-turn-{index}", "status": "inProgress"}}
        raise ValueError(f"unsupported method: {method}")

    async def websocket_handler(self, request):
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
    server = StubServer(socket_path.parent, scenario())
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


def tui_main(argv):
    if scenario() == "tui-exit":
        print("stub TUI exiting immediately", file=sys.stderr)
        return 1
    remote = argv[argv.index("--remote") + 1]
    socket_dir = pathlib.Path(remote.removeprefix("unix://")).parent
    prompt = argv[-1]
    (socket_dir / "tui-prompt.txt").write_text(prompt, encoding="utf-8")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    return 0


def main():
    argv = sys.argv[1:]
    with pathlib.Path("stub-argv.jsonl").open("a", encoding="utf-8") as stream:
        json.dump(argv, stream)
        stream.write("\n")
    if argv and argv[0] == "app-server":
        return app_server_main(argv)
    return tui_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
