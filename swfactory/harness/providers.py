"""LLM API clients for the builtin harness: OpenAI-compatible and Anthropic-compatible.

Both speak plain HTTP via urllib (so HTTPS_PROXY and SSL_CERT_FILE are honoured),
stream or not (a stream request answered with one whole JSON body is read as
that answer), retry transient failures with backoff, abort promptly on cancel,
and share one process-wide set of call slots per provider name
(``max_concurrency``) so parallel agents queue on a small local server. With
``concurrency_scope: "run"`` a runner holds a slot for a whole agent run
(``provider_run_slot``); model calls on that thread reuse it.

Every call returns a Completion whose ``message`` is the provider-native
assistant message, to be appended to the history verbatim (DeepSeek and gpt-oss
need their reasoning back under the key the server used; Anthropic needs thinking
blocks and signatures back).
"""

from __future__ import annotations

import contextlib
import email.utils
import functools
import http.client
import ipaddress
import json
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Iterable, Iterator

from .. import __version__

if TYPE_CHECKING:
    from ..config import ModelConfig, ProviderConfig

ANTHROPIC_VERSION = "2023-06-01"
USER_AGENT = f"software-factory/{__version__}"
RETRYABLE_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})
RETRYABLE_STREAM_ERRORS = frozenset({"overloaded_error", "api_error", "rate_limit_error", "timeout_error"})
MAX_ATTEMPTS = 6
BACKOFF_SECONDS = (2, 4, 8, 16, 32)
MAX_RETRY_AFTER = 120.0
SLICE_SECONDS = 0.5
ERROR_BODY_CHARS = 2000
DEFAULT_TIMEOUT = 900.0

OnDelta = Callable[[str, str], None]

_OPENAI_STOPS = {"stop": "end", "tool_calls": "tool_use", "function_call": "tool_use", "length": "length"}
_ANTHROPIC_STOPS = {"end_turn": "end", "stop_sequence": "end", "tool_use": "tool_use", "max_tokens": "length",
                    "refusal": "refusal"}
# Where OpenAI-compatible servers put reasoning: llama.cpp/DeepSeek, then vLLM/Ollama/OpenRouter.
_REASONING_KEYS = ("reasoning_content", "reasoning")
# A response whose structure trips these while parsing is malformed (not a transport error).
_MALFORMED_ERRORS = (TypeError, AttributeError, LookupError, ValueError)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON text from the model


@dataclass
class Completion:
    text: str
    reasoning: str
    tool_calls: list[ToolCall]
    stop: str  # "end" | "tool_use" | "length" | "refusal" | "other"
    usage: dict
    message: dict = field(default_factory=dict)
    stop_details: dict = field(default_factory=dict)  # Anthropic refusal details, e.g. {"category": "cyber"}


class ProviderError(Exception):
    """A failed model call. status is the HTTP status (None for connection errors / cancel)."""

    def __init__(self, status: int | None, body: str, retryable: bool = False,
                 retry_after: float | None = None) -> None:
        super().__init__(status, body)
        self.status = status
        self.body = body
        self.retryable = retryable
        self.retry_after = retry_after

    def __str__(self) -> str:
        return f"HTTP {self.status}: {self.body}" if self.status is not None else self.body

    def redact(self, secret: str) -> None:
        """Replace secret with *** in the body and in args (so str() and repr() are both clean)."""
        if secret:
            self.body = self.body.replace(secret, "***")
            self.args = (self.status, self.body)


# ---------------------------------------------------------------- concurrency

class ProviderSlots:
    """A provider's process-wide call slots: a bounded semaphore that is re-entrant per thread.

    A thread that already holds a slot (a runner holding one for a whole agent run)
    acquires again at once without taking a second slot, so it cannot deadlock itself.
    """

    def __init__(self, size: int) -> None:
        self.size = size
        self._semaphore = threading.BoundedSemaphore(size)
        self._local = threading.local()

    def held(self) -> bool:
        """Whether the current thread holds a slot."""
        return getattr(self._local, "depth", 0) > 0

    def acquire(self, blocking: bool = True, timeout: float | None = None) -> bool:
        """Take a slot (free if this thread holds one); False if not acquired in time."""
        depth = getattr(self._local, "depth", 0)
        if not depth and not self._semaphore.acquire(blocking, timeout):
            return False
        self._local.depth = depth + 1
        return True

    def release(self) -> None:
        """Undo one acquire by this thread; the slot is freed when the outermost one is released."""
        depth = getattr(self._local, "depth", 0)
        if not depth:
            raise RuntimeError("this thread holds no slot of this provider")
        self._local.depth = depth - 1
        if depth == 1:
            self._semaphore.release()

    def __enter__(self) -> "ProviderSlots":
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


_SLOTS: dict[str, ProviderSlots] = {}
_SLOTS_LOCK = threading.Lock()


def provider_semaphore(name: str, size: int | None) -> ProviderSlots | None:
    """The process-wide slots shared by every call to provider `name` (None = unlimited)."""
    if size is None or int(size) < 1:
        return None
    with _SLOTS_LOCK:
        slots = _SLOTS.get(name)
        if slots is None or slots.size != int(size):
            slots = _SLOTS[name] = ProviderSlots(int(size))
        return slots


@contextlib.contextmanager
def provider_run_slot(cfg: "ProviderConfig", cancel: threading.Event | None = None,
                      on_wait: Callable[[], None] | None = None) -> Iterator[None]:
    """Hold cfg's slot for a whole agent run when cfg.concurrency_scope is "run"; otherwise do nothing.

    Model calls made on this thread inside the block reuse the held slot. Waiting is
    cancel-aware (ProviderError "cancelled"); on_wait() is called once if the slot is busy.
    """
    if getattr(cfg, "concurrency_scope", None) != "run":
        yield
        return
    with _concurrency_slot(cfg, cancel, on_wait):
        yield


def _guarded_wait(on_wait: Callable[[], None] | None) -> Callable[[], None] | None:
    """on_wait, with its exceptions swallowed: a failing notification must not fail the model call."""
    if on_wait is None:
        return None

    def notify() -> None:
        try:
            on_wait()
        except Exception:  # noqa: BLE001 - the caller's callback; the call goes on waiting
            pass
    return notify


@contextlib.contextmanager
def _concurrency_slot(cfg: Any, cancel: threading.Event | None,
                      on_wait: Callable[[], None] | None = None) -> Iterator[None]:
    slots = provider_semaphore(cfg.name, getattr(cfg, "max_concurrency", None))
    if slots is None:
        yield
        return
    if not slots.acquire(blocking=False):
        if on_wait:
            on_wait()
        while not slots.acquire(timeout=SLICE_SECONDS):
            _raise_if_cancelled(cancel)
    try:
        yield
    finally:
        slots.release()


# ---------------------------------------------------------------- base provider


class Provider:
    """Shared HTTP, retry, auth and concurrency logic; subclasses speak one wire format."""

    type: str = ""
    default_auth: str = "bearer"

    def __init__(self, cfg: "ProviderConfig", sleep: Callable[[float], None] = time.sleep) -> None:
        self.cfg = cfg
        self._sleep = sleep
        self._opener = _build_opener(cfg.base_url)

    def complete(self, model: "ModelConfig", system: str, messages: list[dict], tools: list[dict],
                 on_delta: OnDelta | None = None,
                 cancel: threading.Event | None = None,
                 on_wait: Callable[[], None] | None = None) -> Completion:
        """One model call (with retries). Raises ProviderError; cancel raises ProviderError(None, "cancelled").

        on_wait() is called when an attempt has to queue for one of the provider's max_concurrency
        slots (once per wait, before it blocks); a thread that holds the slot for its whole run
        (concurrency_scope "run") never waits here.
        """
        key = self._api_key()
        stream = _wants_stream(model)
        body = _merge_extra(self._body(model, system, messages, tools, stream), getattr(model, "extra_body", None))
        request = self._request(body, self._headers(key, stream))
        attempt = 0
        while True:
            attempt += 1
            _raise_if_cancelled(cancel)
            try:
                return self._attempt(request, stream, on_delta, cancel, on_wait)
            except ProviderError as err:
                if key:
                    err.redact(key)
                if not err.retryable or attempt >= MAX_ATTEMPTS:
                    raise
                delay = float(BACKOFF_SECONDS[attempt - 1])
                if err.retry_after:
                    delay = max(delay, min(err.retry_after, MAX_RETRY_AFTER))
            self._wait(delay, cancel)

    def user_message(self, text: str) -> dict:
        """A plain user turn."""
        return {"role": "user", "content": text}

    def tool_results_message(self, results: list[tuple[str, str, bool]]) -> list[dict]:
        """Messages carrying (tool_call_id, output, is_error) results, to append to the history."""
        raise NotImplementedError

    # Wire-format hooks, implemented by subclasses.
    def _url(self) -> str:
        raise NotImplementedError

    def _protocol_headers(self) -> dict[str, str]:
        return {}

    def _body(self, model: "ModelConfig", system: str, messages: list[dict], tools: list[dict],
              stream: bool) -> dict:
        raise NotImplementedError

    def _parse(self, payload: dict, on_delta: OnDelta | None) -> Completion:
        raise NotImplementedError

    def _parse_stream(self, data_lines: Iterable[str], on_delta: OnDelta | None) -> Completion:
        raise NotImplementedError

    # Plumbing.
    def _api_key(self) -> str | None:
        env_name = getattr(self.cfg, "api_key_env", None)
        if not env_name:
            return None
        key = self.cfg.api_key()
        if not key:
            message = f"environment variable {env_name} is not set"
            key_file = getattr(self.cfg, "api_key_file", None)
            if key_file:
                message += f" and {key_file} does not define it"
            raise ProviderError(None, message, False)
        return key

    def _headers(self, key: str | None, stream: bool) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
            "User-Agent": USER_AGENT,
            **self._protocol_headers(),
        }
        if key:
            mode = getattr(self.cfg, "auth", None) or self.default_auth
            if mode not in ("bearer", "x-api-key", "both"):
                raise ProviderError(None, f"provider {self.cfg.name!r}: unknown auth mode {mode!r}", False)
            if mode in ("bearer", "both"):
                headers["Authorization"] = f"Bearer {key}"
            if mode in ("x-api-key", "both"):
                headers["x-api-key"] = key
        for name, value in (getattr(self.cfg, "headers", None) or {}).items():
            headers[str(name)] = str(value)
        return headers

    def _request(self, body: dict, headers: dict[str, str]) -> urllib.request.Request:
        try:
            return urllib.request.Request(self._url(), data=json.dumps(body).encode("utf-8"),
                                          headers=headers, method="POST")
        except ValueError as err:
            raise ProviderError(None, f"invalid base_url {self.cfg.base_url!r}: {err}", False) from None

    def _attempt(self, request: urllib.request.Request, stream: bool, on_delta: OnDelta | None,
                 cancel: threading.Event | None, on_wait: Callable[[], None] | None = None) -> Completion:
        with _concurrency_slot(self.cfg, cancel, _guarded_wait(on_wait)):
            _raise_if_cancelled(cancel)
            try:
                response = self._opener.open(request, timeout=self._timeout())
            except urllib.error.HTTPError as err:
                raise _http_error(err) from None
            except ValueError as err:
                raise ProviderError(None, f"invalid request: {err}", False) from None
            except (OSError, http.client.HTTPException) as err:
                raise _connection_error(err) from None
            guarded = _guard_callback(on_delta)
            with response:
                try:
                    # A server may answer otherwise than asked (Open WebUI does when a workspace model's Stream
                    # Chat Response overrides the request): one whole JSON body to a stream request is a full
                    # answer, not a stream that ended early, and an SSE stream to a request without one is
                    # read as a stream, instead of failing (and retrying) either way.
                    streamed = not _is_json_response(response) if stream else _is_event_stream(response)
                    if streamed:
                        return self._parse_stream(_sse_data(response, cancel), guarded)
                    return self._parse(_read_json(response), guarded)
                except _CallbackError as failed:  # the caller's own error, untouched and not retried
                    raise failed.error from failed.error.__cause__
                except (OSError, http.client.HTTPException) as err:
                    raise _connection_error(err) from None
                except _MALFORMED_ERRORS as err:
                    raise ProviderError(None, f"malformed response: {type(err).__name__}: {err}", False) from None

    def _timeout(self) -> float:
        return float(getattr(self.cfg, "timeout", None) or DEFAULT_TIMEOUT)

    def _wait(self, seconds: float, cancel: threading.Event | None) -> None:
        remaining = seconds
        while remaining > 0:
            _raise_if_cancelled(cancel)
            step = min(SLICE_SECONDS, remaining)
            self._sleep(step)
            remaining -= step
        _raise_if_cancelled(cancel)


# ---------------------------------------------------------------- OpenAI


class OpenAIProvider(Provider):
    """POST {base_url}/chat/completions (OpenAI, DeepSeek, OpenRouter, vLLM, Ollama, ...)."""

    type = "openai"
    default_auth = "bearer"

    def tool_results_message(self, results: list[tuple[str, str, bool]]) -> list[dict]:
        return [{"role": "tool", "tool_call_id": call_id, "content": output}
                for call_id, output, _is_error in results]

    def _url(self) -> str:
        return self.cfg.base_url.rstrip("/") + "/chat/completions"

    def _body(self, model: "ModelConfig", system: str, messages: list[dict], tools: list[dict],
              stream: bool) -> dict:
        prefix = [{"role": "system", "content": system}] if system else []
        body: dict[str, Any] = {"model": model.model, "messages": prefix + list(messages),
                                "max_tokens": model.max_tokens, "stream": stream}
        if tools:
            body["tools"] = [{"type": "function", "function": {
                "name": tool["name"], "description": tool.get("description", ""),
                "parameters": tool.get("input_schema") or {"type": "object", "properties": {}}}}
                for tool in tools]
        if stream:
            body["stream_options"] = {"include_usage": True}
        return body

    def _parse(self, payload: dict, on_delta: OnDelta | None) -> Completion:
        _raise_for_error_payload(payload)
        choices = payload.get("choices") or []
        if not choices:
            raise ProviderError(None, "response has no choices: " + _clip(json.dumps(payload)), False)
        message = dict(choices[0].get("message") or {})
        message["tool_calls"] = [dict(call, index=i) for i, call in enumerate(message.get("tool_calls") or [])
                                 if isinstance(call, dict)]
        acc = _OpenAIAccumulator(on_delta)
        acc.add(message)
        acc.finish = choices[0].get("finish_reason")
        acc.usage = payload.get("usage") or {}
        return acc.completion()

    def _parse_stream(self, data_lines: Iterable[str], on_delta: OnDelta | None) -> Completion:
        acc = _OpenAIAccumulator(on_delta)
        done = False
        for data in data_lines:
            if data == "[DONE]":
                done = True
                break
            chunk = _loads(data)
            if chunk is None:
                continue
            _raise_for_error_payload(chunk)
            if chunk.get("usage"):
                acc.usage = chunk["usage"]
            for choice in chunk.get("choices") or []:
                if choice.get("index", 0) != 0:
                    continue
                acc.add(choice.get("delta") or {})
                if choice.get("finish_reason"):
                    acc.finish = choice["finish_reason"]
        if not done and acc.finish is None:
            raise ProviderError(None, "response stream ended before the model finished", True)
        return acc.completion()


class _OpenAIAccumulator:
    """Collects deltas (or one whole non-streamed message) into a Completion."""

    def __init__(self, on_delta: OnDelta | None) -> None:
        self._on_delta = on_delta
        self._text: list[str] = []
        self._reasoning: list[str] = []
        self._key_with_text: str | None = None  # the reasoning key that carried text first
        self._key_present: str | None = None    # else the first reasoning key sent at all (even "")
        self._tools = _ToolCallAssembler()
        self.finish: str | None = None
        self.usage: dict = {}

    def add(self, delta: dict) -> None:
        reasoning = ""
        for key in _REASONING_KEYS:
            value = delta.get(key)
            if not isinstance(value, str):
                continue
            self._key_present = self._key_present or key
            if value and not reasoning:  # a server sending both keys duplicates the text
                reasoning = value
                self._key_with_text = self._key_with_text or key
        if reasoning:
            self._reasoning.append(reasoning)
            if self._on_delta:
                self._on_delta("reasoning", reasoning)
        text = _content_text(delta.get("content"))
        if text:
            self._text.append(text)
            if self._on_delta:
                self._on_delta("text", text)
        for call in delta.get("tool_calls") or []:
            if isinstance(call, dict):
                self._tools.add(call)

    def completion(self) -> Completion:
        text, reasoning = "".join(self._text), "".join(self._reasoning)
        calls = self._tools.tool_calls()
        message: dict[str, Any] = {"role": "assistant", "content": text if text else (None if calls else "")}
        if calls:
            message["tool_calls"] = [{"id": c.id, "type": "function",
                                      "function": {"name": c.name, "arguments": c.arguments}} for c in calls]
        reasoning_key = self._key_with_text or self._key_present
        if reasoning_key:  # written back under the server's own key (DeepSeek and gpt-oss need it)
            message[reasoning_key] = reasoning
        return Completion(text=text, reasoning=reasoning, tool_calls=calls,
                          stop=_stop(self.finish, _OPENAI_STOPS, bool(calls)),
                          usage=_openai_usage(self.usage), message=message)


class _ToolCallAssembler:
    """Assembles tool-call deltas by index (or id), tolerating whole calls in one delta."""

    def __init__(self) -> None:
        self._slots: list[dict] = []
        self._by_index: dict[Any, dict] = {}

    def add(self, delta: dict) -> None:
        call_id = delta.get("id") or ""
        index = delta.get("index")
        if index is not None:
            slot = self._by_index.get(index)
        else:
            slot = next((s for s in self._slots if call_id and s["id"] == call_id), None)
            if slot is None and not call_id and self._slots:
                slot = self._slots[-1]
        if slot is not None and call_id and slot["id"] and slot["id"] != call_id:
            slot = None  # a new call reusing an index
        if slot is None:
            slot = {"id": "", "name": "", "arguments": ""}
            self._slots.append(slot)
            if index is not None:
                self._by_index[index] = slot
        if call_id:
            slot["id"] = call_id
        function = delta.get("function") or {}
        if function.get("name"):
            slot["name"] = function["name"]
        arguments = function.get("arguments")
        if isinstance(arguments, (dict, list)):
            arguments = json.dumps(arguments)
        if arguments:
            slot["arguments"] += arguments

    def tool_calls(self) -> list[ToolCall]:
        return [ToolCall(id=slot["id"] or f"call_{uuid.uuid4().hex[:24]}", name=slot["name"],
                         arguments=slot["arguments"] if slot["arguments"].strip() else "{}")
                for slot in self._slots if slot["name"]]


def _openai_usage(usage: dict) -> dict:
    details = usage.get("prompt_tokens_details") or {}
    cached = usage.get("prompt_cache_hit_tokens") or details.get("cached_tokens") or 0
    return {"input_tokens": int(usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
            "cached_tokens": int(cached)}


# ---------------------------------------------------------------- Anthropic


class AnthropicProvider(Provider):
    """POST {base_url}/v1/messages (Anthropic, DeepSeek's and NanoGPT's Anthropic endpoints, ...)."""

    type = "anthropic"
    default_auth = "x-api-key"

    def tool_results_message(self, results: list[tuple[str, str, bool]]) -> list[dict]:
        if not results:
            return []
        return [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": call_id, "content": output, "is_error": bool(is_error)}
            for call_id, output, is_error in results]}]

    def _url(self) -> str:
        return self.cfg.base_url.rstrip("/") + "/v1/messages"

    def _protocol_headers(self) -> dict[str, str]:
        return {"anthropic-version": ANTHROPIC_VERSION}

    def _body(self, model: "ModelConfig", system: str, messages: list[dict], tools: list[dict],
              stream: bool) -> dict:
        body: dict[str, Any] = {"model": model.model}
        if system:
            body["system"] = system
        body.update(messages=list(messages), max_tokens=model.max_tokens, stream=stream)
        if tools:
            eager = {"eager_input_streaming": True} if stream and getattr(model, "eager_tools", False) else {}
            body["tools"] = [{"name": tool["name"], "description": tool.get("description", ""),
                              "input_schema": tool.get("input_schema") or {"type": "object", "properties": {}},
                              **eager}
                             for tool in tools]
        return body

    def _parse(self, payload: dict, on_delta: OnDelta | None) -> Completion:
        _raise_for_error_payload(payload)
        blocks = [dict(block) for block in payload.get("content") or [] if isinstance(block, dict)]
        calls = [ToolCall(id=b.get("id", ""), name=b.get("name", ""), arguments=json.dumps(b.get("input") or {}))
                 for b in blocks if b.get("type") == "tool_use"]
        completion = _anthropic_completion(blocks, calls, payload.get("stop_reason"), payload.get("usage") or {},
                                           payload.get("stop_details"))
        if on_delta and completion.reasoning:
            on_delta("reasoning", completion.reasoning)
        if on_delta and completion.text:
            on_delta("text", completion.text)
        return completion

    def _parse_stream(self, data_lines: Iterable[str], on_delta: OnDelta | None) -> Completion:
        blocks: dict[int, dict] = {}
        partial_json: dict[int, list[str]] = {}
        usage: dict = {}
        stop_reason: str | None = None
        stop_details: Any = None
        current: int | None = None  # the most recently started block, for deltas without an index
        finished = False
        for data in data_lines:
            event = _loads(data)
            if event is None:
                continue
            kind = event.get("type")
            if kind == "message_start":
                usage.update(_present((event.get("message") or {}).get("usage")))
            elif kind == "content_block_start":
                index = event.get("index")
                current = len(blocks) if index is None else index
                blocks[current] = dict(event.get("content_block") or {})
                if blocks[current].get("type") == "tool_use":
                    partial_json[current] = []
            elif kind == "content_block_delta":
                index = event.get("index")
                if index is None:
                    index = 0 if current is None else current
                _apply_block_delta(blocks, partial_json, index, event.get("delta") or {}, on_delta)
            elif kind == "message_delta":
                delta = event.get("delta") or {}
                stop_reason = delta.get("stop_reason") or stop_reason
                stop_details = delta.get("stop_details") or stop_details
                usage.update(_present(event.get("usage")))
            elif kind == "message_stop":
                finished = True
                break
            elif kind == "error":
                _raise_for_error_payload(event)
        if not finished and stop_reason is None:
            raise ProviderError(None, "response stream ended before the model finished", True)
        calls = []
        for index in sorted(blocks):
            block = blocks[index]
            if block.get("type") != "tool_use":
                continue
            raw = "".join(partial_json.get(index, []))
            arguments = json.dumps(block.get("input") or {})
            if raw.strip():
                try:
                    block["input"] = json.loads(raw)
                    arguments = json.dumps(block["input"])
                except ValueError:
                    block["input"], arguments = {}, raw  # truncated JSON: the loop reports it
            calls.append(ToolCall(id=block.get("id", ""), name=block.get("name", ""), arguments=arguments))
        return _anthropic_completion([blocks[i] for i in sorted(blocks)], calls, stop_reason, usage, stop_details)


def _apply_block_delta(blocks: dict[int, dict], partial_json: dict[int, list[str]], index: int, delta: dict,
                       on_delta: OnDelta | None) -> None:
    kind = delta.get("type")
    if index not in blocks:
        if kind == "text_delta":
            blocks[index] = {"type": "text", "text": ""}
        elif kind in ("thinking_delta", "signature_delta"):
            blocks[index] = {"type": "thinking", "thinking": "", "signature": ""}
        else:
            return
    block = blocks[index]
    if kind == "text_delta":
        block["text"] = block.get("text", "") + delta.get("text", "")
        if on_delta and delta.get("text"):
            on_delta("text", delta["text"])
    elif kind == "thinking_delta":
        block["thinking"] = block.get("thinking", "") + delta.get("thinking", "")
        if on_delta and delta.get("thinking"):
            on_delta("reasoning", delta["thinking"])
    elif kind == "signature_delta":
        block["signature"] = block.get("signature", "") + delta.get("signature", "")
    elif kind == "input_json_delta":
        partial_json.setdefault(index, []).append(delta.get("partial_json") or "")


def _anthropic_completion(blocks: list[dict], calls: list[ToolCall], stop_reason: str | None,
                          usage: dict, stop_details: Any = None) -> Completion:
    # The API rejects empty text blocks in requests, so they are not echoed back.
    content = [b for b in blocks if not (b.get("type") == "text" and not b.get("text"))]
    text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
    reasoning = "".join(b.get("thinking", "") for b in content if b.get("type") == "thinking")
    cached = int(usage.get("cache_read_input_tokens") or 0)
    created = int(usage.get("cache_creation_input_tokens") or 0)
    return Completion(
        text=text, reasoning=reasoning, tool_calls=calls,
        stop=_stop(stop_reason, _ANTHROPIC_STOPS, bool(calls)),
        usage={"input_tokens": int(usage.get("input_tokens") or 0) + cached + created,
               "output_tokens": int(usage.get("output_tokens") or 0), "cached_tokens": cached},
        message={"role": "assistant", "content": content},
        stop_details=dict(stop_details) if isinstance(stop_details, dict) else {})


# ---------------------------------------------------------------- shared helpers


def make_provider(cfg: "ProviderConfig") -> Provider:
    """The Provider for cfg.type ("openai" or "anthropic")."""
    if cfg.type == "openai":
        return OpenAIProvider(cfg)
    if cfg.type == "anthropic":
        return AnthropicProvider(cfg)
    raise ValueError(f"unknown provider type {cfg.type!r} for provider {cfg.name!r}")


@functools.lru_cache(maxsize=1)
def _ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context()  # honours SSL_CERT_FILE


def _build_opener(base_url: str) -> urllib.request.OpenerDirector:
    handlers: list[urllib.request.BaseHandler] = [urllib.request.HTTPSHandler(context=_ssl_context())]
    if _is_loopback(urllib.parse.urlsplit(base_url).hostname or ""):
        handlers.append(urllib.request.ProxyHandler({}))  # a local server is never behind the proxy
    return urllib.request.build_opener(*handlers)


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _raise_if_cancelled(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise ProviderError(None, "cancelled", False)


def _wants_stream(model: Any) -> bool:
    """Whether the call streams: extra_body["stream"], when given, overrides model.stream."""
    extra = getattr(model, "extra_body", None) or {}
    return bool(extra["stream"]) if "stream" in extra else bool(getattr(model, "stream", True))


class _CallbackError(Exception):
    """Carries an exception raised by the caller's on_delta out of the response parsers."""

    def __init__(self, error: Exception) -> None:
        super().__init__(error)
        self.error = error


def _guard_callback(on_delta: OnDelta | None) -> OnDelta | None:
    if on_delta is None:
        return None

    def guarded(kind: str, chunk: str) -> None:
        try:
            on_delta(kind, chunk)
        except Exception as err:
            raise _CallbackError(err) from err

    return guarded


def _merge_extra(body: dict, extra: dict | None) -> dict:
    """extra_body merged at top level; extra wins, and a None value removes the key."""
    for key, value in (extra or {}).items():
        if value is None:
            body.pop(key, None)
        else:
            body[key] = value
    return body


def _is_json_response(response: Any) -> bool:
    """Whether the response says its body is JSON (application/json or a +json type), not an SSE stream."""
    headers = getattr(response, "headers", None)
    get_type = getattr(headers, "get_content_type", None)
    if not callable(get_type) or not headers.get("Content-Type"):
        return False
    content_type = str(get_type()).lower()
    return content_type == "application/json" or content_type.endswith("+json")


def _is_event_stream(response: Any) -> bool:
    """Whether the response says its body is an SSE stream (text/event-stream)."""
    headers = getattr(response, "headers", None)
    get_type = getattr(headers, "get_content_type", None)
    if not callable(get_type) or not headers.get("Content-Type"):
        return False
    return str(get_type()).lower() == "text/event-stream"


def _sse_data(response: Any, cancel: threading.Event | None) -> Iterator[str]:
    """The payloads of an SSE stream's `data:` lines (comments, event names and blanks skipped)."""
    for raw in response:
        _raise_if_cancelled(cancel)
        line = raw.decode("utf-8", "replace").strip()
        if line.startswith("data:"):
            yield line[5:].strip()


def _loads(data: str) -> dict | None:
    try:
        value = json.loads(data)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _read_json(response: Any) -> dict:
    raw = response.read()
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        raise ProviderError(None, "invalid JSON response: " + _clip(text), False)
    return payload


def _raise_for_error_payload(payload: dict) -> None:
    """Some servers report errors inside a 200 response or mid-stream."""
    error = payload.get("error")
    if not error and payload.get("type") != "error":
        return
    code = error.get("code") if isinstance(error, dict) else None
    status = code if isinstance(code, int) else None
    retryable = status in RETRYABLE_STATUSES or (
        isinstance(error, dict) and error.get("type") in RETRYABLE_STREAM_ERRORS)
    raise ProviderError(status, _clip(json.dumps(payload)), retryable)


def _http_error(err: urllib.error.HTTPError) -> ProviderError:
    try:
        body = err.read(ERROR_BODY_CHARS * 4).decode("utf-8", "replace")
    except (OSError, http.client.HTTPException):
        body = ""
    finally:
        err.close()
    retry_after = _parse_retry_after(err.headers.get("Retry-After") if err.headers else None)
    return ProviderError(err.code, _clip(body or str(err.reason)), err.code in RETRYABLE_STATUSES, retry_after)


def _connection_error(err: BaseException) -> ProviderError:
    reason = err.reason if isinstance(err, urllib.error.URLError) else err
    if isinstance(reason, ssl.SSLCertVerificationError):
        return ProviderError(None, f"TLS certificate verification failed: {reason}", False)
    return ProviderError(None, f"connection error: {type(reason).__name__}: {reason}", True)


def _parse_retry_after(value: str | None) -> float | None:
    """Retry-After as seconds (delta-seconds or an HTTP date); None if absent or unparseable."""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text") or "" for part in content
                       if isinstance(part, dict) and isinstance(part.get("text"), str))
    return ""


def _stop(reason: str | None, mapping: dict[str, str], has_tool_calls: bool) -> str:
    stop = mapping.get(reason or "", "other")
    if has_tool_calls and stop in ("end", "other"):
        return "tool_use"
    return stop


def _present(usage: Any) -> dict:
    return {k: v for k, v in usage.items() if v is not None} if isinstance(usage, dict) else {}


def _clip(text: str) -> str:
    return text[:ERROR_BODY_CHARS]
