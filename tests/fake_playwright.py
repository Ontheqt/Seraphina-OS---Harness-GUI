#!/usr/bin/env python3
"""A fake Playwright MCP 0.0.83 for the shim's unit tests: it replays responses recorded from the real pinned server
(tests/fixtures/browser/playwright-0.0.83/<scenario>.json, written by tests/browser_rig.py's recorder).

Usage: python3 tests/fake_playwright.py <recording.json> [--log <calls.jsonl>]

It answers initialize and tools/list as the recording does, then each tools/call with the next recorded call: the
tool and its arguments must match (placeholders such as <session> stand for the session's own paths), else it
answers an error that names the difference, so a shim that drifts from the recording fails its test. A recorded call
whose status is not ok had no answer (a timeout, a cancel): the fake then answers nothing and waits for
notifications/cancelled, as the real one did. A call with "crash": true exits the process (code 3) instead.
Standard library only; every call it got is appended to --log.
"""

from __future__ import annotations

import json
import sys
import threading
import time

PLACEHOLDERS = ("<session>", "<downloads>", "<runtime>", "<time>")


def matches(recorded, got) -> bool:
    if isinstance(recorded, str) and isinstance(got, str):
        if recorded == got:
            return True
        if any(mark in recorded for mark in PLACEHOLDERS):
            head = recorded.split("<", 1)[0]
            return got.startswith(head)
        return False
    if isinstance(recorded, dict) and isinstance(got, dict):
        return set(recorded) == set(got) and all(matches(recorded[k], got[k]) for k in recorded)
    if isinstance(recorded, list) and isinstance(got, list):
        return len(recorded) == len(got) and all(matches(a, b) for a, b in zip(recorded, got))
    return recorded == got


class Fake:
    def __init__(self, recording: dict, log: str | None) -> None:
        self.recording = recording
        self.calls = list(recording.get("calls") or [])
        self.index = 0
        self.log = log
        self.lock = threading.Lock()
        self.out = threading.Lock()

    def send(self, message: dict) -> None:
        with self.out:
            sys.stdout.write(json.dumps(message) + "\n")
            sys.stdout.flush()

    def note(self, line: dict) -> None:
        if self.log:
            with open(self.log, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(line) + "\n")

    def call(self, request_id, params: dict) -> None:
        tool, arguments = params.get("name"), params.get("arguments") or {}
        with self.lock:
            entry = self.calls[self.index] if self.index < len(self.calls) else None
            self.index += 1
        self.note({"tool": tool, "arguments": arguments, "index": self.index - 1})
        if entry is None:
            return self.send({"jsonrpc": "2.0", "id": request_id, "result": {"isError": True, "content": [
                {"type": "text", "text": f"### Error\nfake playwright: no recorded call left for {tool}"}]}})
        if entry.get("crash"):
            sys.stdout.flush()
            import os

            os._exit(3)
        if entry.get("tool") != tool or not matches(entry.get("arguments") or {}, arguments):
            return self.send({"jsonrpc": "2.0", "id": request_id, "result": {"isError": True, "content": [
                {"type": "text", "text": f"### Error\nfake playwright: expected {entry.get('tool')} "
                                         f"{json.dumps(entry.get('arguments'))}, got {tool} {json.dumps(arguments)}"}]}})
        if entry.get("status") != "ok" and entry.get("status") != "tool_error":
            return   # no answer was recorded: the shim timed out or cancelled; wait for notifications/cancelled
        delay = float(entry.get("delay") or 0)
        if delay:
            time.sleep(delay)
        self.send({"jsonrpc": "2.0", "id": request_id, "result": {
            "content": entry.get("content") or [], **({"isError": True} if entry.get("status") == "tool_error" else {})}})

    def serve(self) -> None:
        for raw in sys.stdin:
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            method, request_id = message.get("method"), message.get("id")
            if method == "initialize":
                self.send({"jsonrpc": "2.0", "id": request_id, "result": self.recording.get("initialize") or {
                    "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                    "serverInfo": {"name": "Playwright", "version": "1.64.0-alpha-1790635538000"}}})
            elif method == "tools/list":
                self.send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": self.recording.get("tools") or []}})
            elif method == "tools/call":
                threading.Thread(target=self.call, args=(request_id, message.get("params") or {}), daemon=True).start()
            elif method == "notifications/cancelled":
                self.note({"cancelled": (message.get("params") or {}).get("requestId")})
            elif request_id is not None and method:
                self.send({"jsonrpc": "2.0", "id": request_id, "result": {}})


def main() -> int:
    args = sys.argv[1:]
    log = None
    if "--log" in args:
        position = args.index("--log")
        log = args[position + 1]
        del args[position:position + 2]
    with open(args[0], encoding="utf-8") as handle:
        recording = json.load(handle)
    Fake(recording, log).serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
