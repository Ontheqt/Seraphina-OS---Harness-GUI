#!/usr/bin/env python3
"""A small stdio MCP server for tests/test_mcp.py (standard library only).

Usage: python3 tests/mock_mcp_server.py [--slow-init S] [--pages N] [--crash-after K] [--stubborn]
                                         [--deaf] [--close-stdin] [--helper quiet|inherit]

  --slow-init S    sleep S seconds before answering initialize
  --pages N        split tools/list into N pages linked by nextCursor
  --crash-after K  answer K tools/call requests, then exit with code 3 on the next one
  --stubborn       ignore SIGTERM and stdin EOF (only SIGKILL stops it)
  --deaf           after the handshake stop reading stdin, and every 0.05s send the client
                   a ping request and a "tick N" log notification
  --close-stdin    after the handshake close stdin (writes to it then fail) and keep running
  --helper MODE    start a long-sleeping child process and log "helper pid=N"; it shares the
                   server's stdout and stderr with MODE inherit, and no pipes with MODE quiet

Tools: echo {text}, image, fail, sleep {seconds, ignore_cancel?}, ask_roots {method?}
(sends `method`, default roots/list, to the client and returns its response as JSON),
env {names} (the named environment variables that are set, as JSON), big {size}
(`size` bytes of text), progress {steps} (draws a progress bar on stderr with
carriage returns, then writes "progress done") and write {path, text?} (writes `text`
to the file `path`; an OSError is an isError result).
It answers initialize with protocolVersion 2025-03-26, refuses requests before
notifications/initialized, prints one non-JSON line to stdout on startup and
logs to stderr as "mock-mcp: ...".
"""

from __future__ import annotations

import argparse
import base64
import itertools
import json
import math
import os
import signal
import subprocess
import sys
import threading
import time

_OBJECT = {"type": "object", "properties": {}}
TOOLS = [
    {"name": "echo", "description": "Return the given text.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "image", "description": "Return a text item and a tiny PNG.", "inputSchema": _OBJECT},
    {"name": "fail", "description": "Always fail with isError.", "inputSchema": _OBJECT},
    {"name": "sleep", "description": "Sleep for `seconds` (honours cancellation unless ignore_cancel).",
     "inputSchema": {"type": "object", "properties": {"seconds": {"type": "number"},
                                                      "ignore_cancel": {"type": "boolean"}}}},
    {"name": "ask_roots", "description": "Send a request (default roots/list) to the client.",
     "inputSchema": {"type": "object", "properties": {"method": {"type": "string"}}}},
    {"name": "env", "description": "Return the named environment variables that are set.",
     "inputSchema": {"type": "object", "properties": {"names": {"type": "array", "items": {"type": "string"}}}}},
    {"name": "big", "description": "Return `size` bytes of text.",
     "inputSchema": {"type": "object", "properties": {"size": {"type": "integer"}}}},
    {"name": "progress", "description": "Draw a progress bar on stderr.",
     "inputSchema": {"type": "object", "properties": {"steps": {"type": "integer"}}}},
    {"name": "write", "description": "Write `text` to the file `path`.",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}, "text": {"type": "string"}},
                     "required": ["path"]}},
]
PNG_DATA = base64.b64encode(b"\x89PNG\r\n\x1a\nnot really a png").decode("ascii")


def log(text: str) -> None:
    print(f"mock-mcp: {text}", file=sys.stderr, flush=True)


def text_result(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


class MockServer:
    def __init__(self, options: argparse.Namespace) -> None:
        self.options = options
        self.out_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.initialized = False
        self.deaf = False
        self.calls = 0
        self.client_requests = 0
        self.cancels: dict = {}     # our request id -> Event set by notifications/cancelled
        self.waiting: dict = {}     # our request id to the client -> (Event, [response])

    def send(self, message: dict) -> None:
        with self.out_lock:
            sys.stdout.write(json.dumps(message) + "\n")
            sys.stdout.flush()

    def reply(self, request_id, result: dict) -> None:
        self.send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def error(self, request_id, code: int, message: str) -> None:
        self.send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})

    def serve(self) -> None:
        print("mock-mcp-server starting: this line is not JSON", flush=True)
        log(f"started pid={os.getpid()} cwd={os.getcwd()} tag={os.environ.get('MCP_MOCK_TAG', '')}")
        if self.options.helper:
            pipe = None if self.options.helper == "inherit" else subprocess.DEVNULL
            helper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1000)"],
                                      stdin=subprocess.DEVNULL, stdout=pipe, stderr=pipe)
            log(f"helper pid={helper.pid}")
        for line in iter(sys.stdin.readline, ""):
            if line.strip():
                self.handle(json.loads(line))
            if self.deaf:
                self.tick_forever()
        log("stdin closed")
        while self.options.stubborn:
            time.sleep(60)

    def tick_forever(self) -> None:
        log("deaf: no longer reading stdin")
        for tick in itertools.count(1):
            self.send({"jsonrpc": "2.0", "id": f"deaf-{tick}", "method": "ping"})
            self.send({"jsonrpc": "2.0", "method": "notifications/message",
                       "params": {"level": "info", "data": f"tick {tick}"}})
            time.sleep(0.05)

    def handle(self, message: dict) -> None:
        method = message.get("method")
        request_id = message.get("id")
        if method is None:
            entry = self.waiting.pop(request_id, None)
            if entry is not None:
                entry[1].append(message)
                entry[0].set()
        elif method == "initialize":
            log("initialize params=" + json.dumps(message.get("params"), sort_keys=True))
            time.sleep(self.options.slow_init)
            self.reply(request_id, {"protocolVersion": "2025-03-26",
                                    "capabilities": {"tools": {}, "logging": {}},
                                    "serverInfo": {"name": "mock-mcp", "version": "1.0"}})
        elif method == "notifications/initialized":
            self.initialized = True
            self.send({"jsonrpc": "2.0", "method": "notifications/message",
                       "params": {"level": "info", "logger": "mock", "data": "mock ready"}})
        elif method == "notifications/cancelled":
            params = message.get("params") or {}
            log(f"cancelled request {params.get('requestId')} reason={params.get('reason')}")
            event = self.cancels.get(params.get("requestId"))
            if event is not None:
                event.set()
        elif request_id is None:
            pass  # other notifications
        elif method == "ping":
            self.reply(request_id, {})
        elif not self.initialized:
            self.error(request_id, -32002, f"{method} before notifications/initialized")
        elif method == "tools/list":
            self.list_tools(request_id, message.get("params") or {})
        elif method == "tools/call":
            self.start_call(request_id, message.get("params") or {})
        else:
            self.error(request_id, -32601, f"Method not found: {method}")

    def list_tools(self, request_id, params: dict) -> None:
        pages = max(1, self.options.pages)
        size = math.ceil(len(TOOLS) / pages)
        try:
            index = int(params.get("cursor") or 0)
        except ValueError:
            self.error(request_id, -32602, "Invalid cursor")
            return
        log(f"tools/list page {index}")
        result: dict = {"tools": TOOLS[index * size:(index + 1) * size]}
        if index + 1 < pages:
            result["nextCursor"] = str(index + 1)
        else:
            self.deaf = self.options.deaf
        self.reply(request_id, result)
        if self.options.close_stdin and "nextCursor" not in result:
            os.close(sys.stdin.fileno())
            log("closed stdin")
            while True:
                time.sleep(60)

    def start_call(self, request_id, params: dict) -> None:
        if self.options.crash_after is not None and self.calls >= self.options.crash_after:
            log("crashing on purpose")
            os._exit(3)
        self.calls += 1
        self.cancels[request_id] = threading.Event()
        threading.Thread(target=self.run_tool, args=(request_id, params), daemon=True).start()

    def run_tool(self, request_id, params: dict) -> None:
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "echo":
            result = text_result(str(args.get("text", "")))
        elif name == "image":
            result = {"content": [{"type": "text", "text": "an image follows"},
                                  {"type": "image", "data": PNG_DATA, "mimeType": "image/png"}]}
        elif name == "fail":
            result = {"content": [{"type": "text", "text": "tool failed on purpose"}], "isError": True}
        elif name == "sleep":
            seconds = float(args.get("seconds", 1))
            if args.get("ignore_cancel"):
                time.sleep(seconds)
            elif self.cancels[request_id].wait(seconds):
                return  # cancelled: no response, as the MCP spec asks
            result = text_result(f"slept {seconds:g}s")
        elif name == "env":
            names = args.get("names") or []
            result = text_result(json.dumps({key: os.environ[key] for key in names if key in os.environ},
                                            sort_keys=True))
        elif name == "big":
            result = text_result("x" * int(args.get("size", 0)))
        elif name == "progress":
            steps = int(args.get("steps", 10))
            for step in range(1, steps + 1):
                sys.stderr.write(f"\rprogress {step}/{steps} " + "#" * step)
                sys.stderr.flush()
            sys.stderr.write("\rprogress done\n")
            sys.stderr.flush()
            result = text_result("progressed")
        elif name == "write":
            try:
                with open(str(args.get("path")), "w", encoding="utf-8") as handle:
                    handle.write(str(args.get("text", "")))
                result = text_result(f"wrote {args.get('path')}")
            except OSError as exc:
                result = {"content": [{"type": "text", "text": f"cannot write: {exc}"}], "isError": True}
        elif name == "ask_roots":
            result = text_result(json.dumps(self.ask_client(str(args.get("method", "roots/list"))),
                                            sort_keys=True))
        else:
            self.error(request_id, -32602, f"Unknown tool: {name}")
            return
        self.reply(request_id, result)

    def ask_client(self, method: str) -> dict:
        with self.state_lock:
            self.client_requests += 1
            request_id = f"srv-{self.client_requests}"
        done, box = threading.Event(), []
        self.waiting[request_id] = (done, box)
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method})
        if not done.wait(10):
            return {"error": "no response from client"}
        return {key: box[0][key] for key in ("result", "error") if key in box[0]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mock stdio MCP server for tests.")
    parser.add_argument("--slow-init", type=float, default=0.0)
    parser.add_argument("--pages", type=int, default=1)
    parser.add_argument("--crash-after", type=int, default=None)
    parser.add_argument("--stubborn", action="store_true")
    parser.add_argument("--deaf", action="store_true")
    parser.add_argument("--close-stdin", action="store_true")
    parser.add_argument("--helper", choices=("quiet", "inherit"), default=None)
    options = parser.parse_args(argv)
    if options.stubborn:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    MockServer(options).serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
