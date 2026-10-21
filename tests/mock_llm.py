"""A scripted mock LLM server speaking the OpenAI and Anthropic wire formats.

It listens on 127.0.0.1 (random port) in a daemon thread and answers from a
script of neutral reply dicts, or from a handler callback that can inspect each
request (e.g. the system prompt) to script several agents at once.

Example::

    from tests.mock_llm import MockLLM

    script = [
        {"reasoning": "Look first.", "tool_calls": [{"name": "Read", "arguments": {"file_path": "a.py"}}]},
        {"text": "All done."},
    ]
    with MockLLM(script, require_reasoning_passback=True) as mock:
        provider = SimpleNamespace(name="mock", type="openai", base_url=mock.base_url, ...)
        ...                                       # run the code under test against it
        assert mock.requests[0]["path"] == "/chat/completions"
        assert mock.requests[1]["messages"][-1]["role"] == "tool"

    def handler(body, path):                      # takes precedence over the script
        system = body["messages"][0]["content"] if "chat" in path else body.get("system", "")
        return {"text": "VERDICT: PASS"} if "reviewer" in system else None   # None: use the script

Routes: ``POST /chat/completions`` and ``/v1/chat/completions`` (OpenAI format),
``POST /v1/messages`` (Anthropic format; ``anthropic_base_url`` is the same
server), ``GET /models`` and ``/v1/models`` (``model_ids``, default ["mock-model"]),
and ``GET /model`` and ``/v1/model`` (TabbyAPI's loaded model card, ``{"id":
loaded_model}``; 404 while ``loaded_model`` is None). Each POST honours the request's
``stream`` flag; streamed replies are split into several SSE chunks (text,
reasoning and tool-call argument deltas each in 2+ pieces, plus keep-alives).

Reply dict keys (all optional):
    text, reasoning      assistant text / reasoning ("reasoning" present, even "", sends the field)
    reasoning_key        OpenAI format: the field reasoning goes in ("reasoning_content" default, or
                         "reasoning" as vLLM/Ollama/OpenRouter send it)
    tool_calls           [{"name", "arguments": dict or raw JSON text, "id"?}]
    finish               "end" | "tool_use" | "length", or a provider-native value sent verbatim
    stop_details         Anthropic stop_details object (e.g. {"type": "refusal", "category": "cyber"})
    usage                provider-native usage object sent verbatim (default: rough counts)
    signature            Anthropic thinking signature (default derived from the reasoning)
    redacted             Anthropic redacted_thinking data
    delay                seconds to wait before answering; chunk_delay: seconds between SSE chunks
    truncate             stream: stop before the finish/usage events (an interrupted stream)
    raw                  exact response body to send instead (status 200)
    plain_json           answer with one whole JSON response (application/json) even when the request asked
                         to stream, as Open WebUI does when a preset's Stream Chat Response is off
    force_stream         answer with an SSE stream even when the request did not ask to stream, as Open WebUI
                         does when a preset's Stream Chat Response is on

``error_sequence`` lists responses served before normal replies, one per request:
an HTTP status, ``{"status": 429, "retry_after": "3", "body": {...} or "text"}``, or
``{"disconnect": True}`` (close the connection without answering).
``require_reasoning_passback=True`` emulates DeepSeek: an OpenAI-format request with
``tools`` whose history has an assistant message with ``tool_calls`` but no
``reasoning_content`` key gets HTTP 400. A key name instead of True (e.g.
``"reasoning"``) requires that key.

``requests`` records every POST: the parsed JSON body plus ``"path"`` and
``"headers"`` keys (also available as ``.body``, ``.path``, ``.headers``;
headers are case-insensitive). ``max_concurrent`` is the peak number of model
requests in flight at once.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Iterator

PASSBACK_ERROR = "The reasoning_content in the thinking mode must be passed back to the API."
OPENAI_PATHS = ("/chat/completions", "/v1/chat/completions")
ANTHROPIC_PATHS = ("/v1/messages",)
MODEL_PATHS = ("/models", "/v1/models")
LOADED_PATHS = ("/model", "/v1/model")
_OPENAI_FINISH = {"end": "stop", "tool_use": "tool_calls", "length": "length"}
_ANTHROPIC_FINISH = {"end": "end_turn", "tool_use": "tool_use", "length": "max_tokens"}

Handler = Callable[[dict, str], "dict | None"]


class RecordedRequest(dict):
    """A received request: the JSON body's keys plus "path" and "headers"."""

    def __init__(self, body: dict, path: str, headers: Any) -> None:
        super().__init__(body)
        self["path"] = path
        self["headers"] = headers
        self.body = body
        self.path = path
        self.headers = headers


class MockLLM:
    """Scripted OpenAI/Anthropic-compatible server; see the module docstring."""

    def __init__(self, script: list[dict] | None = None, handler: Handler | None = None,
                 require_reasoning_passback: bool | str = False, error_sequence: list | None = None,
                 model_ids: list[str] | None = None, loaded_model: str | None = None) -> None:
        self.model_ids = list(model_ids) if model_ids is not None else ["mock-model"]
        self.loaded_model = loaded_model
        self.script = list(script or [])
        self.handler = handler
        self.require_reasoning_passback = require_reasoning_passback
        self.error_sequence = list(error_sequence or [])
        self.requests: list[RecordedRequest] = []
        self.max_concurrent = 0
        self._active = 0
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> str:
        """Start serving (idempotent) and return the base URL, e.g. http://127.0.0.1:PORT."""
        if self._server is None:
            server = ThreadingHTTPServer(("127.0.0.1", 0), _RequestHandler)
            server.daemon_threads = True
            server.mock = self  # type: ignore[attr-defined]
            self._server = server
            self._thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                                            name="mock-llm", daemon=True)
            self._thread.start()
        return self.base_url

    @property
    def base_url(self) -> str:
        if self._server is None:
            raise RuntimeError("MockLLM is not started")
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    @property
    def anthropic_base_url(self) -> str:
        """Base URL for an Anthropic-format provider (same server; it serves /v1/messages)."""
        return self.base_url

    def stop(self) -> None:
        """Stop serving and release the port."""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            if self._thread is not None:
                self._thread.join(timeout=5)
            self._server = self._thread = None

    def __enter__(self) -> "MockLLM":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # Used by the request handler.
    def _record(self, body: dict, path: str, headers: Any) -> None:
        with self._lock:
            self.requests.append(RecordedRequest(body, path, headers))

    def _enter(self) -> None:
        with self._lock:
            self._active += 1
            self.max_concurrent = max(self.max_concurrent, self._active)

    def _leave(self) -> None:
        with self._lock:
            self._active -= 1

    def _next_error(self) -> Any:
        with self._lock:
            return self.error_sequence.pop(0) if self.error_sequence else None

    def _next_reply(self, body: dict, path: str) -> dict | None:
        if self.handler is not None:
            reply = self.handler(body, path)
            if reply is not None:
                return reply
        with self._lock:
            return self.script.pop(0) if self.script else None

    def _new_id(self, prefix: str) -> str:
        return f"{prefix}{next(self._ids)}"


class _RequestHandler(BaseHTTPRequestHandler):
    server_version = "MockLLM/1.0"

    def log_message(self, message_format: str, *args: Any) -> None:
        pass  # keep test output quiet

    @property
    def mock(self) -> MockLLM:
        return self.server.mock  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in MODEL_PATHS:
            self._send_json(200, {"object": "list",
                                  "data": [{"id": model_id, "object": "model"} for model_id in self.mock.model_ids]})
        elif path in LOADED_PATHS and self.mock.loaded_model is not None:
            self._send_json(200, {"id": self.mock.loaded_model, "object": "model", "owned_by": "tabbyAPI"})
        else:
            self._send_json(404, {"error": {"message": f"no route {self.path}"}})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            self._send_json(400, {"error": {"message": "request body is not JSON"}})
            return
        self.mock._record(body, path, self.headers)
        if path in OPENAI_PATHS:
            fmt = "openai"
        elif path in ANTHROPIC_PATHS:
            fmt = "anthropic"
        else:
            self._send_json(404, {"error": {"message": f"no route {path}"}})
            return
        self.mock._enter()
        try:
            self._answer(fmt, body, path)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the client went away (e.g. cancelled)
        except Exception:  # a bug in a test handler: fail the call fast, not with retries
            self._send_json(400, {"error": {"message": "MockLLM handler error:\n" + traceback.format_exc()}})
        finally:
            self.mock._leave()

    def _answer(self, fmt: str, body: dict, path: str) -> None:
        error = self.mock._next_error()
        if error is not None:
            self._send_error_reply(error)
            return
        passback = self.mock.require_reasoning_passback
        if fmt == "openai" and passback:
            key = passback if isinstance(passback, str) else "reasoning_content"
            if _missing_reasoning(body, key):
                self._send_json(400, {"error": {"message": PASSBACK_ERROR.replace("reasoning_content", key),
                                                "type": "invalid_request_error"}})
                return
        reply = self.mock._next_reply(body, path)
        if reply is None:
            self._send_json(400, {"error": {"message": "MockLLM: script exhausted"}})
            return
        time.sleep(float(reply.get("delay") or 0))
        stream = (bool(body.get("stream")) or bool(reply.get("force_stream"))) and not reply.get("plain_json")
        if "raw" in reply:
            self._send_raw(reply["raw"], "text/event-stream" if stream else "application/json")
            return
        reply = _normalise(reply, fmt, self.mock)
        if fmt == "openai":
            if stream:
                include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
                self._send_stream(_openai_events(reply, body, include_usage), reply)
            else:
                self._send_json(200, _openai_response(reply, body))
        elif stream:
            self._send_stream(_anthropic_events(reply, body), reply)
        else:
            self._send_json(200, _anthropic_response(reply, body))

    def _send_error_reply(self, error: Any) -> None:
        spec = error if isinstance(error, dict) else {"status": int(error)}
        if spec.get("disconnect"):
            self.close_connection = True
            return
        status = int(spec.get("status", 500))
        payload = spec.get("body", {"error": {"message": f"mock error {status}", "code": status}})
        data = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if spec.get("retry_after") is not None:
            self.send_header("Retry-After", str(spec["retry_after"]))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_raw(self, text: str, content_type: str) -> None:
        data = text.encode()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_stream(self, chunks: Iterator[str], reply: dict) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        delay = float(reply.get("chunk_delay") or 0)
        for chunk in chunks:
            self.wfile.write(chunk.encode())
            self.wfile.flush()
            if delay:
                time.sleep(delay)


def _missing_reasoning(body: dict, key: str) -> bool:
    if not body.get("tools"):
        return False
    return any(isinstance(m, dict) and m.get("role") == "assistant" and m.get("tool_calls")
               and key not in m for m in body.get("messages") or [])


def _normalise(reply: dict, fmt: str, mock: MockLLM) -> dict:
    """Fill in tool-call ids and argument text, the native finish reason and usage."""
    prefix = "call_" if fmt == "openai" else "toolu_"
    calls = []
    for call in reply.get("tool_calls") or []:
        arguments = call.get("arguments", {})
        calls.append({"id": call.get("id") or mock._new_id(prefix), "name": call["name"],
                      "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments)})
    native = _OPENAI_FINISH if fmt == "openai" else _ANTHROPIC_FINISH
    finish = reply.get("finish") or ("tool_use" if calls else "end")
    out = dict(reply, tool_calls=calls, finish=native.get(finish, finish))
    if "usage" not in reply:
        output = max(1, (len(reply.get("text", "")) + len(reply.get("reasoning", ""))
                         + sum(len(c["arguments"]) for c in calls)) // 4)
        out["usage"] = ({"prompt_tokens": 10, "completion_tokens": output, "total_tokens": 10 + output}
                        if fmt == "openai" else {"input_tokens": 10, "output_tokens": output})
    return out


def _pieces(text: str, count: int = 3) -> list[str]:
    """Split text into up to `count` pieces (2+ when it has 2+ characters)."""
    if len(text) <= 1:
        return [text]
    size = max(1, -(-len(text) // count))
    return [text[i:i + size] for i in range(0, len(text), size)]


def _sse(payload: dict, event: str | None = None) -> str:
    head = f"event: {event}\n" if event else ""
    return f"{head}data: {json.dumps(payload)}\n\n"


# ---------------------------------------------------------------- OpenAI format


def _openai_message(reply: dict) -> dict:
    message: dict[str, Any] = {"role": "assistant", "content": reply.get("text") or None}
    if "reasoning" in reply:
        message[reply.get("reasoning_key") or "reasoning_content"] = reply["reasoning"]
    if reply["tool_calls"]:
        message["tool_calls"] = [{"id": c["id"], "type": "function",
                                  "function": {"name": c["name"], "arguments": c["arguments"]}}
                                 for c in reply["tool_calls"]]
    return message


def _openai_response(reply: dict, body: dict) -> dict:
    return {"id": "chatcmpl-mock", "object": "chat.completion", "created": 0,
            "model": body.get("model", "mock-model"),
            "choices": [{"index": 0, "message": _openai_message(reply), "finish_reason": reply["finish"]}],
            "usage": reply["usage"]}


def _openai_events(reply: dict, body: dict, include_usage: bool) -> Iterator[str]:
    base = {"id": "chatcmpl-mock", "object": "chat.completion.chunk", "created": 0,
            "model": body.get("model", "mock-model")}

    def chunk(delta: dict, finish: str | None = None) -> str:
        return _sse(dict(base, choices=[{"index": 0, "delta": delta, "finish_reason": finish}]))

    yield ": keep-alive\n\n"
    yield chunk({"role": "assistant", "content": ""})
    if "reasoning" in reply:
        key = reply.get("reasoning_key") or "reasoning_content"
        for piece in _pieces(reply["reasoning"]):
            yield chunk({key: piece})
    if reply.get("text"):
        for piece in _pieces(reply["text"]):
            yield chunk({"content": piece})
    for index, call in enumerate(reply["tool_calls"]):
        yield chunk({"tool_calls": [{"index": index, "id": call["id"], "type": "function",
                                     "function": {"name": call["name"], "arguments": ""}}]})
        for piece in _pieces(call["arguments"]):
            yield chunk({"tool_calls": [{"index": index, "function": {"arguments": piece}}]})
    if reply.get("truncate"):
        return
    yield "\n"
    yield chunk({}, reply["finish"])
    if include_usage:
        yield _sse(dict(base, choices=[], usage=reply["usage"]))
    yield "data: [DONE]\n\n"


# ---------------------------------------------------------------- Anthropic format


def _anthropic_blocks(reply: dict) -> list[dict]:
    blocks: list[dict] = []
    if "reasoning" in reply:
        signature = reply.get("signature") or "sig-" + hashlib.sha256(reply["reasoning"].encode()).hexdigest()[:32]
        blocks.append({"type": "thinking", "thinking": reply["reasoning"], "signature": signature})
    if reply.get("redacted"):
        blocks.append({"type": "redacted_thinking", "data": reply["redacted"]})
    if reply.get("text"):
        blocks.append({"type": "text", "text": reply["text"]})
    for call in reply["tool_calls"]:
        try:
            tool_input = json.loads(call["arguments"]) if call["arguments"].strip() else {}
        except ValueError:
            tool_input = {}
        blocks.append({"type": "tool_use", "id": call["id"], "name": call["name"], "input": tool_input,
                       "_raw": call["arguments"]})
    return blocks


def _anthropic_response(reply: dict, body: dict) -> dict:
    content = [{k: v for k, v in block.items() if k != "_raw"} for block in _anthropic_blocks(reply)]
    return {"id": "msg_mock", "type": "message", "role": "assistant", "model": body.get("model", "mock-model"),
            "content": content, "stop_reason": reply["finish"], "stop_sequence": None,
            "stop_details": reply.get("stop_details"), "usage": reply["usage"]}


def _anthropic_events(reply: dict, body: dict) -> Iterator[str]:
    usage = dict(reply["usage"])
    output_tokens = usage.pop("output_tokens", 0)
    yield _sse({"type": "message_start", "message": {
        "id": "msg_mock", "type": "message", "role": "assistant", "model": body.get("model", "mock-model"),
        "content": [], "stop_reason": None, "stop_sequence": None, "usage": dict(usage, output_tokens=1)}},
        "message_start")
    yield _sse({"type": "ping"}, "ping")
    for index, block in enumerate(_anthropic_blocks(reply)):
        kind = block["type"]
        if kind == "thinking":
            start = {"type": "thinking", "thinking": "", "signature": ""}
            deltas = [{"type": "thinking_delta", "thinking": p} for p in _pieces(block["thinking"])]
            deltas.append({"type": "signature_delta", "signature": block["signature"]})
        elif kind == "text":
            start = {"type": "text", "text": ""}
            deltas = [{"type": "text_delta", "text": p} for p in _pieces(block["text"])]
        elif kind == "tool_use":
            start = {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}
            deltas = [{"type": "input_json_delta", "partial_json": p} for p in _pieces(block["_raw"])]
        else:
            start, deltas = block, []
        yield _sse({"type": "content_block_start", "index": index, "content_block": start}, "content_block_start")
        for delta in deltas:
            yield _sse({"type": "content_block_delta", "index": index, "delta": delta}, "content_block_delta")
        yield _sse({"type": "content_block_stop", "index": index}, "content_block_stop")
    if reply.get("truncate"):
        return
    yield _sse({"type": "message_delta", "delta": {"stop_reason": reply["finish"], "stop_sequence": None,
                                                   "stop_details": reply.get("stop_details")},
                "usage": {"output_tokens": output_tokens}}, "message_delta")
    yield _sse({"type": "message_stop"}, "message_stop")
