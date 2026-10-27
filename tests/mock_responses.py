"""A Responses-API stub for the Codex worker's tests (BROWSER_AND_CODEX.md §4.1, DESIGN §44).

It listens on 127.0.0.1 (a random port) in a daemon thread and answers ``POST /v1/responses`` the way the pinned
Codex 0.163.0 reads it: a server-sent event stream of ``response.created``, ``response.output_item.done`` and
``response.completed`` (with usage). ``script`` is what the model "does", one entry per request, in order:

- ``{"cmd": "<shell text>"}``: a call of Codex's ``exec_command`` tool (its output comes back in the next request);
  ``{out_dir}`` in it becomes the run's out folder, read from the harness note in the request;
- ``{"text": "..."}``: a final assistant message (also what an exhausted script answers: "Done.");
- ``{"status": 500}``: an HTTP error with a JSON body;
- ``{"hang": seconds}``: the stream starts, then nothing arrives for that long (a turn to stop or to kill).

A request without ``"stream": true`` (the doctor's Responses check) gets one JSON response object. ``GET
/v1/models`` lists ``models``. ``no_responses = True`` makes ``/v1/responses`` a 404, as a server without the route
(TabbyAPI at the pin) answers. ``requests`` records every request as (method, path, headers, body), in order, so a
test can say what reached the model server, and whether any POST did.
"""

from __future__ import annotations

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class MockResponses:
    def __init__(self, script: list[dict] | None = None, models: tuple[str, ...] = ("stub-model",)) -> None:
        self.script = list(script or [])
        self.models = list(models)
        self.no_responses = False
        self.requests: list[tuple[str, str, dict, Any]] = []
        self.lock = threading.Lock()
        self.release = threading.Event()   # set to end every hang early
        self._server: ThreadingHTTPServer | None = None
        self._count = 0

    def start(self) -> str:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self._server.stub = self   # type: ignore[attr-defined]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self.url

    @property
    def url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def stop(self) -> None:
        self.release.set()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    def __enter__(self) -> "MockResponses":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def posts(self) -> list[tuple[str, str, dict, Any]]:
        with self.lock:
            return [entry for entry in self.requests if entry[0] == "POST"]

    def tool_outputs(self) -> list[str]:
        """Every exec_command output Codex sent back to the model, in order."""
        found = []
        for _, _, _, body in self.posts():
            for item in (body or {}).get("input") or []:
                if isinstance(item, dict) and item.get("type") == "function_call_output":
                    output = item.get("output")
                    found.append(output if isinstance(output, str) else json.dumps(output))
        return list(dict.fromkeys(found))

    def next_step(self) -> dict:
        with self.lock:
            self._count += 1
            return self.script.pop(0) if self.script else {"text": "Done."}

    def number(self) -> int:
        with self.lock:
            return self._count


OUT_DIR_RE = re.compile(r"Write build\.md in (/\S+) \(the job folder")


def _out_dir(body: dict) -> str:
    """The run's out folder, as the harness note in the prompt names it."""
    found = OUT_DIR_RE.search(json.dumps(body.get("input") or [], ensure_ascii=False).replace("\\n", " "))
    return found.group(1) if found else "/nonexistent-out-dir"


def _usage() -> dict:
    return {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15, "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0}}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, message_format: str, *args: Any) -> None:
        pass

    @property
    def stub(self) -> MockResponses:
        return self.server.stub   # type: ignore[attr-defined]

    def _record(self, method: str, body: Any) -> None:
        with self.stub.lock:
            self.stub.requests.append((method, self.path, {k.lower(): v for k, v in self.headers.items()}, body))

    def _json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._record("GET", None)
        if self.path.rstrip("/").endswith("/models"):
            self._json(200, {"object": "list", "data": [{"id": name, "object": "model"} for name in self.stub.models]})
        else:
            self._json(404, {"detail": "Not Found"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            body = None
        self._record("POST", body)
        if not self.path.rstrip("/").endswith("/responses") or self.stub.no_responses:
            self._json(404, {"detail": "Not Found"})
            return
        if not isinstance(body, dict) or not body.get("stream"):
            text = {"type": "message", "id": "msg_check", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": "ok"}]}
            self._json(200, {"id": "resp_check", "object": "response", "status": "completed",
                             "model": body.get("model") if isinstance(body, dict) else None,
                             "output": [text], "usage": _usage()})
            return
        step = self.stub.next_step()
        number = self.stub.number()
        if "status" in step:
            self._json(int(step["status"]), {"error": {"message": "stub error", "type": "server_error"}})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        rid = f"resp_{number}"
        self._event({"type": "response.created", "response": {"id": rid}})
        if "hang" in step:
            self.stub.release.wait(float(step["hang"]))
            return
        if "cmd" in step:
            command = str(step["cmd"]).replace("{out_dir}", _out_dir(body))
            item = {"type": "function_call", "id": f"fc_{number}", "call_id": f"call_{number}",
                    "name": "exec_command", "arguments": json.dumps({"cmd": command})}
        else:
            item = {"type": "message", "id": f"msg_{number}", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": str(step.get("text", "Done."))}]}
        self._event({"type": "response.output_item.done", "item": item})
        self._event({"type": "response.completed", "response": {"id": rid, "usage": _usage()}})

    def _event(self, event: dict) -> None:
        try:
            self.wfile.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode("utf-8"))
            self.wfile.flush()
        except OSError:
            pass


def wait_for(predicate: Any, seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return bool(predicate())
