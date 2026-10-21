"""Tests for swfactory.harness.providers, against tests/mock_llm.py (no network, no real keys)."""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from types import SimpleNamespace
from unittest import mock as umock

from swfactory.harness import providers
from swfactory.harness.providers import (
    AnthropicProvider, Completion, OpenAIProvider, ProviderError, ProviderSlots, make_provider, provider_run_slot,
    provider_semaphore,
)
from tests.mock_llm import PASSBACK_ERROR, MockLLM

KEY = "sk-test-secret-123456"
TOOLS = [
    {"name": "Read", "description": "Read a file.",
     "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}},
    {"name": "Bash", "description": "Run a command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}}},
]


def provider_cfg(base_url: str, type: str = "openai", key: str | None = KEY, api_key_env: str | None = "TEST_API_KEY",
                 **extra: object) -> SimpleNamespace:
    """A ProviderConfig-shaped object (DESIGN §11/§20) with a unique name."""
    fields = dict(name=f"test-{uuid.uuid4().hex[:8]}", type=type, base_url=base_url, api_key_env=api_key_env,
                  timeout=10, headers={}, auth="", api_key_file=None, max_concurrency=None)
    fields.update(extra)
    return SimpleNamespace(api_key=lambda: key, **fields)


def model_cfg(provider: SimpleNamespace, stream: bool = True, max_tokens: int = 1024,
              extra_body: dict | None = None, eager_tools: bool = False) -> SimpleNamespace:
    """A ModelConfig-shaped object (DESIGN §11/§20/§21)."""
    return SimpleNamespace(key="opus", provider=provider, model="mock-model", max_tokens=max_tokens,
                           stream=stream, extra_body=extra_body or {}, context_tokens=None, keep_reasoning=True,
                           eager_tools=eager_tools)


def guard_event(test: unittest.TestCase, seconds: float = 10) -> threading.Event:
    """A cancel event that fires after `seconds`, so a deadlock fails the test instead of hanging it."""
    event = threading.Event()
    timer = threading.Timer(seconds, event.set)
    timer.start()
    test.addCleanup(timer.cancel)
    return event


class FakeSleep:
    """Records sleeps instead of sleeping; optionally runs a hook on each call."""

    def __init__(self, hook=None) -> None:
        self.calls: list[float] = []
        self.hook = hook

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if self.hook:
            self.hook(len(self.calls))

    @property
    def total(self) -> float:
        return sum(self.calls)


class Deltas:
    def __init__(self) -> None:
        self.items: list[tuple[str, str]] = []

    def __call__(self, kind: str, chunk: str) -> None:
        self.items.append((kind, chunk))

    def joined(self, kind: str) -> str:
        return "".join(chunk for k, chunk in self.items if k == kind)

    def count(self, kind: str) -> int:
        return sum(1 for k, _ in self.items if k == kind)


def sse(*payloads: object) -> str:
    return "".join(f"data: {p if isinstance(p, str) else json.dumps(p)}\n\n" for p in payloads)


def closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class MockTestCase(unittest.TestCase):
    def start_mock(self, script=None, **kwargs) -> MockLLM:
        server = MockLLM(script, **kwargs)
        server.start()
        self.addCleanup(server.stop)
        return server

    def openai(self, server: MockLLM, **cfg) -> tuple[OpenAIProvider, FakeSleep]:
        sleep = FakeSleep()
        return OpenAIProvider(provider_cfg(server.base_url, **cfg), sleep=sleep), sleep

    def anthropic(self, server: MockLLM, **cfg) -> tuple[AnthropicProvider, FakeSleep]:
        sleep = FakeSleep()
        return AnthropicProvider(provider_cfg(server.anthropic_base_url, type="anthropic", **cfg), sleep=sleep), sleep


class OpenAIStreamingTests(MockTestCase):
    def test_text_reply_streams_deltas_and_builds_message(self) -> None:
        server = self.start_mock([{"text": "Hello there, world.",
                                   "usage": {"prompt_tokens": 12, "completion_tokens": 5}}])
        provider, _ = self.openai(server)
        deltas = Deltas()
        result = provider.complete(model_cfg(provider.cfg), "You are a test.", [provider.user_message("hi")], [],
                                   on_delta=deltas)
        self.assertIsInstance(result, Completion)
        self.assertEqual(result.text, "Hello there, world.")
        self.assertEqual(deltas.joined("text"), "Hello there, world.")
        self.assertGreater(deltas.count("text"), 1)
        self.assertEqual(result.stop, "end")
        self.assertEqual(result.tool_calls, [])
        self.assertEqual(result.usage, {"input_tokens": 12, "output_tokens": 5, "cached_tokens": 0})
        self.assertEqual(result.message, {"role": "assistant", "content": "Hello there, world."})

    def test_request_shape_headers_and_extra_body(self) -> None:
        server = self.start_mock([{"text": "ok"}])
        provider, _ = self.openai(server)
        model = model_cfg(provider.cfg, extra_body={"thinking": {"type": "enabled"}, "max_tokens": 77,
                                                     "reasoning_effort": "high"})
        provider.complete(model, "SYS", [provider.user_message("hi")], TOOLS)
        request = server.requests[0]
        self.assertEqual(request.path, "/chat/completions")
        self.assertEqual(request.headers["Authorization"], f"Bearer {KEY}")
        self.assertIsNone(request.headers.get("x-api-key"))
        self.assertEqual(request.headers["Content-Type"], "application/json")
        self.assertEqual(request.headers["Accept"], "text/event-stream")
        self.assertTrue(request.headers["User-Agent"].startswith("software-factory/"))
        body = request.body
        self.assertEqual(body["model"], "mock-model")
        self.assertEqual(body["messages"], [{"role": "system", "content": "SYS"}, {"role": "user", "content": "hi"}])
        self.assertEqual(body["max_tokens"], 77)  # extra_body wins
        self.assertIs(body["stream"], True)
        self.assertEqual(body["stream_options"], {"include_usage": True})
        self.assertEqual(body["thinking"], {"type": "enabled"})
        self.assertEqual(body["reasoning_effort"], "high")
        self.assertEqual(body["tools"][0], {"type": "function", "function": {
            "name": "Read", "description": "Read a file.", "parameters": TOOLS[0]["input_schema"]}})
        self.assertEqual(len(body["tools"]), 2)

    def test_no_tools_key_when_empty_and_none_in_extra_body_removes_key(self) -> None:
        server = self.start_mock([{"text": "ok"}])
        provider, _ = self.openai(server)
        model = model_cfg(provider.cfg, extra_body={"max_tokens": None, "max_completion_tokens": 50})
        provider.complete(model, "", [provider.user_message("hi")], [])
        body = server.requests[0].body
        self.assertNotIn("tools", body)
        self.assertNotIn("max_tokens", body)
        self.assertEqual(body["max_completion_tokens"], 50)
        self.assertEqual(body["messages"], [{"role": "user", "content": "hi"}])  # empty system omitted

    def test_base_url_trailing_slash_and_v1_route(self) -> None:
        server = self.start_mock([{"text": "a"}, {"text": "b"}])
        provider = OpenAIProvider(provider_cfg(server.base_url + "/"))
        provider.complete(model_cfg(provider.cfg), "s", [provider.user_message("x")], [])
        provider_v1 = OpenAIProvider(provider_cfg(server.base_url + "/v1/"))
        self.assertEqual(provider_v1.complete(model_cfg(provider_v1.cfg), "s", [], []).text, "b")
        self.assertEqual([r.path for r in server.requests], ["/chat/completions", "/v1/chat/completions"])

    def test_multiple_tool_calls_are_assembled_by_index(self) -> None:
        server = self.start_mock([{"text": "Let me look.", "tool_calls": [
            {"name": "Read", "arguments": {"file_path": "src/app.py"}, "id": "call_a"},
            {"name": "Bash", "arguments": {"command": "ls -la && echo done"}, "id": "call_b"},
        ]}])
        provider, _ = self.openai(server)
        result = provider.complete(model_cfg(provider.cfg), "s", [provider.user_message("go")], TOOLS)
        self.assertEqual(result.stop, "tool_use")
        self.assertEqual([(c.id, c.name) for c in result.tool_calls], [("call_a", "Read"), ("call_b", "Bash")])
        self.assertEqual(json.loads(result.tool_calls[0].arguments), {"file_path": "src/app.py"})
        self.assertEqual(json.loads(result.tool_calls[1].arguments), {"command": "ls -la && echo done"})
        self.assertEqual(result.message["content"], "Let me look.")
        self.assertEqual(result.message["tool_calls"][1], {"id": "call_b", "type": "function", "function": {
            "name": "Bash", "arguments": result.tool_calls[1].arguments}})
        self.assertNotIn("reasoning_content", result.message)

    def test_tool_call_only_reply_has_null_content(self) -> None:
        server = self.start_mock([{"tool_calls": [{"name": "Read", "arguments": {"file_path": "a"}}]}])
        provider, _ = self.openai(server)
        result = provider.complete(model_cfg(provider.cfg), "s", [], TOOLS)
        self.assertIsNone(result.message["content"])
        self.assertTrue(result.tool_calls[0].id.startswith("call_"))

    def test_empty_reply_without_tools_has_empty_string_content(self) -> None:
        server = self.start_mock([{"text": ""}])
        provider, _ = self.openai(server)
        result = provider.complete(model_cfg(provider.cfg), "s", [], [])
        self.assertEqual(result.message, {"role": "assistant", "content": ""})
        self.assertEqual(result.stop, "end")

    def test_reasoning_is_streamed_and_kept_in_message(self) -> None:
        server = self.start_mock([{"reasoning": "Thinking hard about it.", "text": "Answer."}])
        provider, _ = self.openai(server)
        deltas = Deltas()
        result = provider.complete(model_cfg(provider.cfg), "s", [], [], on_delta=deltas)
        self.assertEqual(result.reasoning, "Thinking hard about it.")
        self.assertEqual(deltas.joined("reasoning"), "Thinking hard about it.")
        self.assertGreater(deltas.count("reasoning"), 1)
        self.assertEqual(deltas.items[0][0], "reasoning")
        self.assertEqual(result.message["reasoning_content"], "Thinking hard about it.")

    def test_empty_reasoning_field_is_still_passed_back(self) -> None:
        server = self.start_mock([{"reasoning": "", "tool_calls": [{"name": "Read", "arguments": {"file_path": "a"}}]}])
        provider, _ = self.openai(server)
        result = provider.complete(model_cfg(provider.cfg), "s", [], TOOLS)
        self.assertEqual(result.reasoning, "")
        self.assertEqual(result.message["reasoning_content"], "")

    def test_length_and_other_finish_reasons(self) -> None:
        server = self.start_mock([{"text": "cut", "finish": "length"}, {"text": "x", "finish": "content_filter"},
                                  {"tool_calls": [{"name": "Read", "arguments": {}}], "finish": "stop"}])
        provider, _ = self.openai(server)
        model = model_cfg(provider.cfg)
        self.assertEqual(provider.complete(model, "s", [], []).stop, "length")
        self.assertEqual(provider.complete(model, "s", [], []).stop, "other")
        self.assertEqual(provider.complete(model, "s", [], TOOLS).stop, "tool_use")

    def test_whole_tool_calls_in_one_delta_without_index(self) -> None:
        base = {"object": "chat.completion.chunk"}
        raw = sse(
            dict(base, choices=[{"index": 0, "delta": {"role": "assistant", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "Read", "arguments": '{"file_path": "a"}'}},
                {"id": "c2", "type": "function", "function": {"name": "Bash", "arguments": {"command": "ls"}}},
            ]}}]),
            dict(base, choices=[{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]),
            "[DONE]")
        server = self.start_mock([{"raw": raw}])
        provider, _ = self.openai(server)
        result = provider.complete(model_cfg(provider.cfg), "s", [], TOOLS)
        self.assertEqual([(c.id, c.name) for c in result.tool_calls], [("c1", "Read"), ("c2", "Bash")])
        self.assertEqual(json.loads(result.tool_calls[1].arguments), {"command": "ls"})

    def test_parallel_calls_reusing_an_index_and_empty_arguments(self) -> None:
        base = {"object": "chat.completion.chunk"}
        raw = (": comment\n\n"
               + sse(dict(base, choices=[{"index": 0, "delta": {"tool_calls": [
                   {"index": 0, "id": "c1", "function": {"name": "Read", "arguments": '{"file_'}}]}}]))
               + "event: ignored\n"
               + sse(dict(base, choices=[{"index": 0, "delta": {"tool_calls": [
                   {"index": 0, "function": {"arguments": 'path": "a"}'}}]}}]),
                     dict(base, choices=[{"index": 0, "delta": {"tool_calls": [
                         {"index": 0, "id": "c2", "function": {"name": "Bash", "arguments": ""}}]}}]),
                     "not json at all",
                     dict(base, choices=[{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                          usage={"prompt_tokens": 3, "completion_tokens": 4}),
                     "[DONE]"))
        server = self.start_mock([{"raw": raw}])
        provider, _ = self.openai(server)
        result = provider.complete(model_cfg(provider.cfg), "s", [], TOOLS)
        self.assertEqual([(c.id, c.name, c.arguments) for c in result.tool_calls],
                         [("c1", "Read", '{"file_path": "a"}'), ("c2", "Bash", "{}")])
        self.assertEqual(result.usage["output_tokens"], 4)

    def test_truncated_stream_is_retried(self) -> None:
        server = self.start_mock([{"text": "partial", "truncate": True}, {"text": "complete"}])
        provider, sleep = self.openai(server)
        result = provider.complete(model_cfg(provider.cfg), "s", [], [])
        self.assertEqual(result.text, "complete")
        self.assertEqual(len(server.requests), 2)
        self.assertEqual(sleep.total, 2)

    def test_a_plain_json_answer_to_a_stream_request_is_the_answer(self) -> None:
        """Open WebUI answers a stream request with one JSON body when the preset's Stream Chat Response is off:
        it is read as the whole answer, once, instead of "stream ended" and six retries."""
        server = self.start_mock([{"text": "Whole answer.", "tool_calls": [{"name": "Read", "id": "c1",
                                                                            "arguments": {"file_path": "a"}}],
                                   "plain_json": True, "usage": {"prompt_tokens": 7, "completion_tokens": 3}}])
        provider, sleep = self.openai(server)
        deltas = Deltas()
        result = provider.complete(model_cfg(provider.cfg), "s", [provider.user_message("hi")], TOOLS,
                                   on_delta=deltas)
        self.assertIs(server.requests[0].body["stream"], True)
        self.assertEqual(server.requests[0].headers["Accept"], "text/event-stream")
        self.assertEqual((result.text, result.stop, [c.name for c in result.tool_calls]),
                         ("Whole answer.", "tool_use", ["Read"]))
        self.assertEqual(deltas.joined("text"), "Whole answer.")
        self.assertEqual(result.usage, {"input_tokens": 7, "output_tokens": 3, "cached_tokens": 0})
        self.assertEqual((len(server.requests), sleep.total), (1, 0))

    def test_a_stream_answering_a_request_without_one_is_the_answer(self) -> None:
        """Open WebUI streams when the preset's Stream Chat Response is on, even to stream: false: the SSE body is
        read as a stream, once, instead of "invalid JSON response"."""
        server = self.start_mock([{"text": "Streamed anyway.", "force_stream": True,
                                   "usage": {"prompt_tokens": 5, "completion_tokens": 2}}])
        provider, sleep = self.openai(server)
        deltas = Deltas()
        result = provider.complete(model_cfg(provider.cfg, stream=False), "s", [provider.user_message("hi")], [],
                                   on_delta=deltas)
        self.assertIs(server.requests[0].body["stream"], False)
        self.assertEqual(server.requests[0].headers["Accept"], "application/json")
        self.assertEqual((result.text, result.stop), ("Streamed anyway.", "end"))
        self.assertEqual(deltas.joined("text"), "Streamed anyway.")
        self.assertEqual((len(server.requests), sleep.total), (1, 0))
        server = self.start_mock([{"raw": json.dumps({"choices": [{"message": {"content": "plain"},
                                                                    "finish_reason": "stop"}]})}])
        provider, _ = self.openai(server)
        self.assertEqual(provider.complete(model_cfg(provider.cfg, stream=False), "s", [], []).text, "plain",
                         "a JSON body to a request without a stream is read as before")

    def test_an_sse_body_is_still_a_stream_and_a_cut_one_is_retried(self) -> None:
        server = self.start_mock([{"raw": json.dumps({"choices": [{"message": {"content": "x"}}]})},
                                  {"text": "ok"}])
        provider, sleep = self.openai(server)
        # "raw" on a stream request is served as text/event-stream: a JSON line without "data:" is no answer
        self.assertEqual(provider.complete(model_cfg(provider.cfg), "s", [], []).text, "ok")
        self.assertEqual((len(server.requests), sleep.total), (2, 2))

    def test_error_chunk_mid_stream_raises(self) -> None:
        raw = sse({"choices": [{"index": 0, "delta": {"content": "Hi"}}]},
                  {"error": {"code": 400, "message": "bad things"}})
        server = self.start_mock([{"raw": raw}])
        provider, _ = self.openai(server)
        with self.assertRaises(ProviderError) as ctx:
            provider.complete(model_cfg(provider.cfg), "s", [], [])
        self.assertEqual(ctx.exception.status, 400)
        self.assertFalse(ctx.exception.retryable)
        self.assertIn("bad things", ctx.exception.body)

    def test_cancel_during_stream(self) -> None:
        server = self.start_mock([{"text": "one two three four five six", "chunk_delay": 0.01}])
        provider, _ = self.openai(server)
        cancel = threading.Event()
        with self.assertRaises(ProviderError) as ctx:
            provider.complete(model_cfg(provider.cfg), "s", [], [],
                              on_delta=lambda kind, chunk: cancel.set(), cancel=cancel)
        self.assertEqual((ctx.exception.status, ctx.exception.body, ctx.exception.retryable),
                         (None, "cancelled", False))


class OpenAINonStreamingTests(MockTestCase):
    def test_text_tool_calls_reasoning_and_usage(self) -> None:
        server = self.start_mock([{"reasoning": "hmm", "text": "Reading.", "tool_calls": [
            {"name": "Read", "arguments": {"file_path": "a"}, "id": "c1"},
            {"name": "Bash", "arguments": {"command": "pwd"}, "id": "c2"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "prompt_cache_hit_tokens": 60,
                      "prompt_cache_miss_tokens": 40}}])
        provider, _ = self.openai(server)
        deltas = Deltas()
        result = provider.complete(model_cfg(provider.cfg, stream=False), "s", [], TOOLS, on_delta=deltas)
        request = server.requests[0]
        self.assertIs(request.body["stream"], False)
        self.assertNotIn("stream_options", request.body)
        self.assertEqual(request.headers["Accept"], "application/json")
        self.assertEqual(result.text, "Reading.")
        self.assertEqual(deltas.items, [("reasoning", "hmm"), ("text", "Reading.")])
        self.assertEqual([c.id for c in result.tool_calls], ["c1", "c2"])
        self.assertEqual(result.stop, "tool_use")
        self.assertEqual(result.usage, {"input_tokens": 100, "output_tokens": 20, "cached_tokens": 60})
        self.assertEqual(result.message["reasoning_content"], "hmm")
        self.assertEqual(len(result.message["tool_calls"]), 2)

    def test_openai_cached_tokens_details(self) -> None:
        server = self.start_mock([{"text": "x", "usage": {"prompt_tokens": 50, "completion_tokens": 2,
                                                          "prompt_tokens_details": {"cached_tokens": 32}}}])
        provider, _ = self.openai(server)
        result = provider.complete(model_cfg(provider.cfg, stream=False), "s", [], [])
        self.assertEqual(result.usage, {"input_tokens": 50, "output_tokens": 2, "cached_tokens": 32})

    def test_missing_usage_is_zero(self) -> None:
        raw = json.dumps({"choices": [{"message": {"content": [{"type": "text", "text": "parts"}]},
                                       "finish_reason": "stop"}]})
        server = self.start_mock([{"raw": raw}])
        provider, _ = self.openai(server)
        result = provider.complete(model_cfg(provider.cfg, stream=False), "s", [], [])
        self.assertEqual(result.text, "parts")
        self.assertEqual(result.usage, {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0})

    def test_extra_body_can_turn_streaming_off(self) -> None:
        server = self.start_mock([{"text": "plain"}])
        provider, _ = self.openai(server)
        result = provider.complete(model_cfg(provider.cfg, extra_body={"stream": False}), "s", [], [])
        self.assertEqual(result.text, "plain")
        self.assertNotIn("stream_options", server.requests[0].body)  # OpenAI rejects it without stream
        self.assertEqual(server.requests[0].headers["Accept"], "application/json")

    def test_error_in_200_body_and_invalid_json(self) -> None:
        server = self.start_mock([{"raw": json.dumps({"error": {"message": "quota", "code": 429}})},
                                  {"text": "after"}, {"raw": "<html>not json</html>"}, {"raw": "{}"}])
        provider, sleep = self.openai(server)
        model = model_cfg(provider.cfg, stream=False)
        self.assertEqual(provider.complete(model, "s", [], []).text, "after")  # code 429 → retried
        self.assertEqual(sleep.total, 2)
        with self.assertRaises(ProviderError) as ctx:
            provider.complete(model, "s", [], [])
        self.assertIn("invalid JSON", ctx.exception.body)
        self.assertFalse(ctx.exception.retryable)
        with self.assertRaises(ProviderError) as ctx:
            provider.complete(model, "s", [], [])
        self.assertIn("no choices", ctx.exception.body)


class ReasoningPassbackTests(MockTestCase):
    """DeepSeek returns 400 when reasoning_content is dropped from a tool loop."""

    def run_loop(self, stream: bool, drop_reasoning: bool,
                 reasoning_key: str = "reasoning_content") -> tuple[MockLLM, Completion]:
        server = self.start_mock([
            {"reasoning": "I need to read the file first.", "reasoning_key": reasoning_key,
             "tool_calls": [{"name": "Read", "arguments": {"file_path": "a.py"}}]},
            {"reasoning": "Now I know.", "reasoning_key": reasoning_key, "text": "Done."},
        ], require_reasoning_passback=True if reasoning_key == "reasoning_content" else reasoning_key)
        provider, _ = self.openai(server)
        model = model_cfg(provider.cfg, stream=stream)
        messages = [provider.user_message("Fix a.py")]
        first = provider.complete(model, "s", messages, TOOLS)
        assistant = dict(first.message)
        if drop_reasoning:
            assistant.pop(reasoning_key)
        messages.append(assistant)
        messages.extend(provider.tool_results_message([(first.tool_calls[0].id, "print('hi')", False)]))
        return server, provider.complete(model, "s", messages, TOOLS)

    def test_round_trip_streaming(self) -> None:
        server, result = self.run_loop(stream=True, drop_reasoning=False)
        self.assertEqual(result.text, "Done.")
        sent = server.requests[1].body["messages"]
        self.assertEqual([m["role"] for m in sent], ["system", "user", "assistant", "tool"])
        self.assertEqual(sent[2]["reasoning_content"], "I need to read the file first.")
        self.assertEqual(sent[3], {"role": "tool", "tool_call_id": sent[2]["tool_calls"][0]["id"],
                                   "content": "print('hi')"})

    def test_round_trip_non_streaming(self) -> None:
        _, result = self.run_loop(stream=False, drop_reasoning=False)
        self.assertEqual(result.text, "Done.")

    def test_dropping_reasoning_is_a_non_retryable_400(self) -> None:
        with self.assertRaises(ProviderError) as ctx:
            self.run_loop(stream=True, drop_reasoning=True)
        self.assertEqual(ctx.exception.status, 400)
        self.assertFalse(ctx.exception.retryable)
        self.assertIn(PASSBACK_ERROR, ctx.exception.body)

    def test_reasoning_key_is_written_back_under_the_same_key(self) -> None:
        """gpt-oss on vLLM / Ollama / OpenRouter sends `reasoning`, and needs it back there (DESIGN §22)."""
        for stream in (True, False):
            with self.subTest(stream=stream):
                server, result = self.run_loop(stream=stream, drop_reasoning=False, reasoning_key="reasoning")
                self.assertEqual((result.text, result.reasoning), ("Done.", "Now I know."))
                self.assertEqual(result.message["reasoning"], "Now I know.")
                sent = server.requests[1].body["messages"][2]
                self.assertEqual(sent["reasoning"], "I need to read the file first.")
                self.assertNotIn("reasoning_content", sent)

    def test_dropping_reasoning_key_is_rejected_by_such_a_server(self) -> None:
        with self.assertRaises(ProviderError) as ctx:
            self.run_loop(stream=True, drop_reasoning=True, reasoning_key="reasoning")
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("The reasoning in the thinking mode", ctx.exception.body)

    def test_reasoning_key_choice_when_servers_send_both_or_null(self) -> None:
        base = {"object": "chat.completion.chunk"}

        def stream_of(*deltas: dict) -> str:
            return sse(*[dict(base, choices=[{"index": 0, "delta": d}]) for d in deltas],
                       dict(base, choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}]), "[DONE]")

        cases = [  # (deltas, key expected in the message, reasoning)
            ([{"reasoning_content": None, "reasoning": "a"}, {"reasoning_content": None, "reasoning": "b"}],
             "reasoning", "ab"),
            ([{"reasoning_content": "a", "reasoning": "a"}, {"reasoning_content": "b", "reasoning": "b"}],
             "reasoning_content", "ab"),
            ([{"reasoning": ""}, {"reasoning_content": "x"}], "reasoning_content", "x"),
            ([{"reasoning": ""}, {"content": "hi"}], "reasoning", ""),
            ([{"reasoning_content": None, "reasoning": None, "content": "hi"}], None, ""),
        ]
        server = self.start_mock([{"raw": stream_of(*deltas)} for deltas, _, _ in cases])
        provider, _ = self.openai(server)
        for deltas, key, reasoning in cases:
            with self.subTest(deltas=deltas):
                received = Deltas()
                result = provider.complete(model_cfg(provider.cfg), "s", [], [], on_delta=received)
                self.assertEqual(result.reasoning, reasoning)
                self.assertEqual(received.joined("reasoning"), reasoning)  # duplicates not echoed twice
                present = [k for k in ("reasoning_content", "reasoning") if k in result.message]
                self.assertEqual(present, [key] if key else [])
                if key:
                    self.assertEqual(result.message[key], reasoning)


class RetryTests(MockTestCase):
    def test_retryable_statuses_back_off_then_succeed(self) -> None:
        server = self.start_mock([{"text": "finally"}], error_sequence=[429, 503, 529])
        provider, sleep = self.openai(server)
        result = provider.complete(model_cfg(provider.cfg), "s", [], [])
        self.assertEqual(result.text, "finally")
        self.assertEqual(len(server.requests), 4)
        self.assertEqual(sleep.total, 2 + 4 + 8)
        self.assertTrue(all(0 < s <= 0.5 for s in sleep.calls))

    def test_retry_after_is_honoured_when_larger_and_capped(self) -> None:
        server = self.start_mock([{"text": "ok"}], error_sequence=[
            {"status": 429, "retry_after": "10"}, {"status": 503, "retry_after": "1"},
            {"status": 429, "retry_after": "9999"}])
        provider, sleep = self.openai(server)
        provider.complete(model_cfg(provider.cfg), "s", [], [])
        self.assertEqual(sleep.total, 10 + 4 + 120)

    def test_gives_up_after_six_attempts(self) -> None:
        server = self.start_mock([{"text": "never"}], error_sequence=[500] * 6)
        provider, sleep = self.openai(server)
        with self.assertRaises(ProviderError) as ctx:
            provider.complete(model_cfg(provider.cfg), "s", [], [])
        self.assertEqual(ctx.exception.status, 500)
        self.assertTrue(ctx.exception.retryable)
        self.assertEqual(len(server.requests), 6)
        self.assertEqual(sleep.total, 2 + 4 + 8 + 16 + 32)

    def test_401_is_not_retried_and_key_is_redacted(self) -> None:
        body = {"error": {"message": f"invalid key {KEY}"}}
        server = self.start_mock([{"text": "unused"}], error_sequence=[{"status": 401, "body": body}])
        provider, sleep = self.openai(server)
        with self.assertRaises(ProviderError) as ctx:
            provider.complete(model_cfg(provider.cfg), "s", [], [])
        err = ctx.exception
        self.assertEqual((err.status, err.retryable), (401, False))
        self.assertIn("invalid key ***", err.body)
        self.assertNotIn(KEY, err.body)
        self.assertNotIn(KEY, str(err))
        self.assertNotIn(KEY, repr(err))
        self.assertNotIn(KEY, repr(err.args))
        self.assertEqual(err.args, (401, err.body))
        self.assertTrue(str(err).startswith("HTTP 401: "))
        self.assertEqual(len(server.requests), 1)
        self.assertEqual(sleep.calls, [])

    def test_error_body_is_truncated(self) -> None:
        server = self.start_mock([], error_sequence=[{"status": 400, "body": "x" * 9000}])
        provider, _ = self.openai(server)
        with self.assertRaises(ProviderError) as ctx:
            provider.complete(model_cfg(provider.cfg), "s", [], [])
        self.assertEqual(len(ctx.exception.body), providers.ERROR_BODY_CHARS)

    def test_dropped_connection_is_retried(self) -> None:
        server = self.start_mock([{"text": "back"}], error_sequence=[{"disconnect": True}])
        provider, sleep = self.anthropic(server)
        self.assertEqual(provider.complete(model_cfg(provider.cfg, stream=False), "s", [], []).text, "back")
        self.assertEqual((len(server.requests), sleep.total), (2, 2))

    def test_connection_refused_is_retried(self) -> None:
        sleep = FakeSleep()
        provider = OpenAIProvider(provider_cfg(f"http://127.0.0.1:{closed_port()}"), sleep=sleep)
        with self.assertRaises(ProviderError) as ctx:
            provider.complete(model_cfg(provider.cfg), "s", [], [])
        self.assertIsNone(ctx.exception.status)
        self.assertTrue(ctx.exception.retryable)
        self.assertIn("connection error", ctx.exception.body)
        self.assertEqual(sleep.total, 62)

    def test_cancel_during_backoff(self) -> None:
        server = self.start_mock([{"text": "unused"}], error_sequence=[503, 503])
        cancel = threading.Event()
        sleep = FakeSleep(hook=lambda n: cancel.set() if n == 2 else None)
        provider = OpenAIProvider(provider_cfg(server.base_url), sleep=sleep)
        with self.assertRaises(ProviderError) as ctx:
            provider.complete(model_cfg(provider.cfg), "s", [], [], cancel=cancel)
        self.assertEqual((ctx.exception.status, ctx.exception.body, ctx.exception.retryable),
                         (None, "cancelled", False))
        self.assertEqual(len(server.requests), 1)
        self.assertEqual(len(sleep.calls), 2)

    def test_cancel_before_start_sends_nothing(self) -> None:
        server = self.start_mock([{"text": "unused"}])
        provider, _ = self.openai(server)
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(ProviderError):
            provider.complete(model_cfg(provider.cfg), "s", [], [], cancel=cancel)
        self.assertEqual(server.requests, [])

    def test_retry_after_parsing(self) -> None:
        self.assertEqual(providers._parse_retry_after("7"), 7.0)
        self.assertIsNone(providers._parse_retry_after("soon"))
        self.assertIsNone(providers._parse_retry_after(None))
        self.assertEqual(providers._parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT"), 0.0)


class AuthAndKeyTests(MockTestCase):
    def test_missing_key_fails_before_any_request(self) -> None:
        server = self.start_mock([{"text": "unused"}])
        provider, _ = self.openai(server, key=None, api_key_env="MISSING_TEST_KEY")
        with self.assertRaises(ProviderError) as ctx:
            provider.complete(model_cfg(provider.cfg), "s", [], [])
        self.assertEqual(ctx.exception.body, "environment variable MISSING_TEST_KEY is not set")
        self.assertIsNone(ctx.exception.status)
        self.assertFalse(ctx.exception.retryable)
        self.assertEqual(server.requests, [])

    def test_missing_key_mentions_key_file(self) -> None:
        server = self.start_mock([])
        provider, _ = self.openai(server, key=None, api_key_env="STRATA_API_KEY", api_key_file="/nope/strata.env")
        with self.assertRaises(ProviderError) as ctx:
            provider.complete(model_cfg(provider.cfg), "s", [], [])
        self.assertIn("STRATA_API_KEY is not set and /nope/strata.env", ctx.exception.body)

    def test_keyless_local_server_sends_no_auth_header(self) -> None:
        server = self.start_mock([{"text": "local"}])
        provider, _ = self.openai(server, key=None, api_key_env=None)
        self.assertEqual(provider.complete(model_cfg(provider.cfg), "s", [], []).text, "local")
        headers = server.requests[0].headers
        self.assertIsNone(headers.get("Authorization"))
        self.assertIsNone(headers.get("x-api-key"))

    def test_auth_modes(self) -> None:
        cases = [("openai", "", {"Authorization": f"Bearer {KEY}", "x-api-key": None}),
                 ("openai", "x-api-key", {"Authorization": None, "x-api-key": KEY}),
                 ("openai", "both", {"Authorization": f"Bearer {KEY}", "x-api-key": KEY}),
                 ("anthropic", "", {"Authorization": None, "x-api-key": KEY}),
                 ("anthropic", "bearer", {"Authorization": f"Bearer {KEY}", "x-api-key": None}),
                 ("anthropic", "both", {"Authorization": f"Bearer {KEY}", "x-api-key": KEY})]
        server = self.start_mock([{"text": "ok"}] * len(cases))
        for type_, auth, expected in cases:
            with self.subTest(type=type_, auth=auth):
                provider = make_provider(provider_cfg(server.base_url, type=type_, auth=auth))
                provider.complete(model_cfg(provider.cfg), "s", [], [])
                headers = server.requests[-1].headers
                for name, value in expected.items():
                    self.assertEqual(headers.get(name), value)

    def test_unknown_auth_mode(self) -> None:
        server = self.start_mock([])
        provider, _ = self.openai(server, auth="magic")
        with self.assertRaises(ProviderError) as ctx:
            provider.complete(model_cfg(provider.cfg), "s", [], [])
        self.assertIn("unknown auth mode", str(ctx.exception))
        self.assertEqual(server.requests, [])

    def test_custom_headers_are_sent(self) -> None:
        server = self.start_mock([{"text": "ok"}])
        provider, _ = self.openai(server, headers={"HTTP-Referer": "https://example.invalid", "X-Title": "factory"})
        provider.complete(model_cfg(provider.cfg), "s", [], [])
        self.assertEqual(server.requests[0].headers["X-Title"], "factory")
        self.assertEqual(server.requests[0].headers["HTTP-Referer"], "https://example.invalid")

    def test_api_key_is_read_at_call_time(self) -> None:
        server = self.start_mock([{"text": "a"}, {"text": "b"}])
        keys = iter(["first-key-0001", "second-key-0002"])
        cfg = provider_cfg(server.base_url)
        cfg.api_key = lambda: next(keys)
        provider = OpenAIProvider(cfg)
        provider.complete(model_cfg(cfg), "s", [], [])
        provider.complete(model_cfg(cfg), "s", [], [])
        self.assertEqual([r.headers["Authorization"] for r in server.requests],
                         ["Bearer first-key-0001", "Bearer second-key-0002"])

    def test_loopback_bypasses_proxy(self) -> None:
        server = self.start_mock([{"text": "direct"}])
        env = {k: v for k, v in os.environ.items() if k.lower() not in ("no_proxy", "http_proxy", "https_proxy")}
        env["http_proxy"] = env["HTTP_PROXY"] = f"http://127.0.0.1:{closed_port()}"
        with umock.patch.dict(os.environ, env, clear=True):
            provider = OpenAIProvider(provider_cfg(server.base_url), sleep=FakeSleep())
            self.assertEqual(provider.complete(model_cfg(provider.cfg), "s", [], []).text, "direct")
        self.assertTrue(providers._is_loopback("localhost"))
        self.assertTrue(providers._is_loopback("::1"))
        self.assertFalse(providers._is_loopback("api.deepseek.com"))

    def test_invalid_base_url(self) -> None:
        provider = OpenAIProvider(provider_cfg("not-a-url"))
        with self.assertRaises(ProviderError) as ctx:
            provider.complete(model_cfg(provider.cfg), "s", [], [])
        self.assertFalse(ctx.exception.retryable)
        self.assertIn("invalid base_url", ctx.exception.body)


class ConcurrencyTests(MockTestCase):
    def run_two(self, server: MockLLM, cfg: SimpleNamespace) -> list[object]:
        results: list[object] = [None, None]

        def worker(slot: int) -> None:
            provider = OpenAIProvider(cfg, sleep=FakeSleep())  # separate instances share the semaphore
            try:
                results[slot] = provider.complete(model_cfg(cfg), "s", [], []).text
            except ProviderError as err:
                results[slot] = err

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        return results

    def test_max_concurrency_one_serialises_calls(self) -> None:
        server = self.start_mock(handler=lambda body, path: {"text": "slow", "delay": 0.3})
        results = self.run_two(server, provider_cfg(server.base_url, max_concurrency=1))
        self.assertEqual(results, ["slow", "slow"])
        self.assertEqual(server.max_concurrent, 1)

    def test_unlimited_concurrency_overlaps(self) -> None:
        barrier = threading.Barrier(2, timeout=10)

        def handler(body: dict, path: str) -> dict:
            barrier.wait()  # only passes if both requests are in flight together
            return {"text": "fast"}

        server = self.start_mock(handler=handler)
        results = self.run_two(server, provider_cfg(server.base_url, max_concurrency=None))
        self.assertEqual(results, ["fast", "fast"])
        self.assertEqual(server.max_concurrent, 2)

    def test_slot_is_released_during_backoff(self) -> None:
        server = self.start_mock([{"text": "inner"}, {"text": "outer"}], error_sequence=[503])
        cfg = provider_cfg(server.base_url, max_concurrency=1)
        inner_results: list[str] = []

        def during_backoff(call: int) -> None:
            if call == 1:  # a second agent calls the same provider while the first backs off
                guard = threading.Event()
                timer = threading.Timer(5, guard.set)
                timer.start()
                try:
                    inner_results.append(OpenAIProvider(cfg).complete(model_cfg(cfg), "s", [], [], cancel=guard).text)
                finally:
                    timer.cancel()

        provider = OpenAIProvider(cfg, sleep=FakeSleep(hook=during_backoff))
        self.assertEqual(provider.complete(model_cfg(cfg), "s", [], []).text, "outer")
        self.assertEqual(inner_results, ["inner"])

    def test_on_wait_is_called_once_per_wait_and_never_breaks_the_call(self) -> None:
        release = threading.Event()
        server = self.start_mock(handler=lambda body, path: release.wait(10) and {"text": "done"})
        cfg = provider_cfg(server.base_url, max_concurrency=1)
        holder = threading.Thread(target=lambda: OpenAIProvider(cfg).complete(model_cfg(cfg), "s", [], []))
        holder.start()
        self.addCleanup(holder.join, 10)
        self.addCleanup(release.set)
        deadline = time.monotonic() + 5
        while not server.requests and time.monotonic() < deadline:
            time.sleep(0.01)
        waits: list[str] = []

        def on_wait() -> None:
            waits.append("waiting")
            threading.Timer(0.2, release.set).start()
            raise RuntimeError("a broken notification")   # swallowed: the call keeps waiting

        result = OpenAIProvider(cfg).complete(model_cfg(cfg), "s", [], [], on_wait=on_wait)
        self.assertEqual((result.text, waits), ("done", ["waiting"]))
        free: list[str] = []
        OpenAIProvider(cfg).complete(model_cfg(cfg), "s", [], [], on_wait=lambda: free.append("x"))
        self.assertEqual(free, [], "a free slot means no wait")

    def test_cancel_while_waiting_for_a_slot(self) -> None:
        release = threading.Event()
        server = self.start_mock(handler=lambda body, path: release.wait(10) and {"text": "held"})
        cfg = provider_cfg(server.base_url, max_concurrency=1)
        holder_result: list[str] = []
        holder = threading.Thread(target=lambda: holder_result.append(
            OpenAIProvider(cfg).complete(model_cfg(cfg), "s", [], []).text))
        holder.start()
        self.addCleanup(holder.join, 10)
        self.addCleanup(release.set)
        deadline = time.monotonic() + 5
        while not server.requests and time.monotonic() < deadline:
            time.sleep(0.01)
        cancel = threading.Event()
        threading.Timer(0.2, cancel.set).start()
        started = time.monotonic()
        with self.assertRaises(ProviderError) as ctx:
            OpenAIProvider(cfg).complete(model_cfg(cfg), "s", [], [], cancel=cancel)
        self.assertEqual(ctx.exception.body, "cancelled")
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(len(server.requests), 1)  # the waiter never reached the server
        release.set()
        holder.join(10)
        self.assertEqual(holder_result, ["held"])

    def test_semaphore_registry(self) -> None:
        name = f"reg-{uuid.uuid4().hex}"
        self.assertIsNone(provider_semaphore(name, None))
        self.assertIsNone(provider_semaphore(name, 0))
        first = provider_semaphore(name, 2)
        self.assertIs(provider_semaphore(name, 2), first)
        self.assertIsNot(provider_semaphore(name, 1), first)  # resized by a config reload

    def test_run_scope_holds_the_slot_across_calls_without_deadlock(self) -> None:
        """concurrency_scope "run" (DESIGN §21): one slot per agent run, calls inside reuse it."""
        server = self.start_mock(handler=lambda body, path: {"text": body["messages"][-1]["content"]})
        cfg = provider_cfg(server.base_url, max_concurrency=1, concurrency_scope="run")
        guard = guard_event(self)
        a_holds, b_waiting = threading.Event(), threading.Event()
        texts: list[str] = []

        def call(text: str) -> None:
            completion = OpenAIProvider(cfg).complete(model_cfg(cfg), "", [{"role": "user", "content": text}], [],
                                                      cancel=guard)
            texts.append(completion.text)

        def run_a() -> None:
            with provider_run_slot(cfg, guard):
                call("a1")
                a_holds.set()
                b_waiting.wait(5)
                call("a2")

        def run_b() -> None:
            a_holds.wait(5)
            with provider_run_slot(cfg, guard, on_wait=b_waiting.set):
                call("b1")

        threads = [threading.Thread(target=run_a), threading.Thread(target=run_b)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(15)
        self.assertFalse(guard.is_set(), "a run deadlocked on its own slot")
        self.assertTrue(b_waiting.is_set())
        self.assertEqual(texts, ["a1", "a2", "b1"])  # b queued for the whole of a's run
        self.assertEqual(server.max_concurrent, 1)

    def test_thread_holding_the_semaphore_directly_can_still_call(self) -> None:
        server = self.start_mock([{"text": "ok"}])
        cfg = provider_cfg(server.base_url, max_concurrency=1)
        slots = provider_semaphore(cfg.name, 1)
        with slots:
            result = OpenAIProvider(cfg).complete(model_cfg(cfg), "s", [], [], cancel=guard_event(self, 5))
            self.assertTrue(slots.held())  # the call's release did not free the outer hold
        self.assertFalse(slots.held())
        self.assertEqual(result.text, "ok")

    def test_run_slot_only_holds_for_run_scope(self) -> None:
        cfg = provider_cfg("http://127.0.0.1:1", max_concurrency=1)
        slots = provider_semaphore(cfg.name, 1)
        with provider_run_slot(cfg):  # no concurrency_scope: "call" (the default)
            self.assertFalse(slots.held())
        cfg.concurrency_scope = "call"
        with provider_run_slot(cfg):
            self.assertFalse(slots.held())
        cfg.concurrency_scope = "run"
        with provider_run_slot(cfg):
            self.assertTrue(slots.held())
        self.assertFalse(slots.held())
        with provider_run_slot(provider_cfg("http://127.0.0.1:1", concurrency_scope="run")):
            pass  # unlimited provider: nothing to hold

    def test_cancel_while_waiting_for_a_run_slot(self) -> None:
        cfg = provider_cfg("http://127.0.0.1:1", max_concurrency=1, concurrency_scope="run")
        holding, done = threading.Event(), threading.Event()

        def holder() -> None:
            with provider_run_slot(cfg):
                holding.set()
                done.wait(10)

        thread = threading.Thread(target=holder)
        thread.start()
        self.addCleanup(thread.join, 10)
        self.addCleanup(done.set)
        self.assertTrue(holding.wait(5))
        cancel = threading.Event()
        waits: list[int] = []

        def on_wait() -> None:
            waits.append(1)
            cancel.set()

        with self.assertRaises(ProviderError) as ctx:
            with provider_run_slot(cfg, cancel, on_wait=on_wait):
                self.fail("acquired a busy slot")
        self.assertEqual(ctx.exception.body, "cancelled")
        self.assertEqual(waits, [1])

    def test_provider_slots_are_reentrant_per_thread(self) -> None:
        slots = ProviderSlots(1)
        with self.assertRaises(RuntimeError):
            slots.release()
        self.assertTrue(slots.acquire())
        self.assertTrue(slots.acquire())  # re-entered, not a second slot

        def try_other_thread() -> list[bool]:
            got: list[bool] = []
            thread = threading.Thread(target=lambda: got.append(slots.acquire(timeout=0.05)))
            thread.start()
            thread.join(5)
            return got

        self.assertEqual(try_other_thread(), [False])
        slots.release()
        self.assertEqual(try_other_thread(), [False])  # still held once
        slots.release()
        self.assertEqual(try_other_thread(), [True])


class AnthropicTests(MockTestCase):
    def test_streaming_thinking_text_and_signature_verbatim(self) -> None:
        server = self.start_mock([{"reasoning": "Let me think step by step.", "signature": "SIG/abc+==",
                                   "text": "The answer is 42.",
                                   "usage": {"input_tokens": 20, "cache_read_input_tokens": 100,
                                             "cache_creation_input_tokens": 5, "output_tokens": 9}}])
        provider, _ = self.anthropic(server)
        deltas = Deltas()
        result = provider.complete(model_cfg(provider.cfg), "Be brief.", [provider.user_message("q")], [],
                                   on_delta=deltas)
        self.assertEqual(result.text, "The answer is 42.")
        self.assertEqual(result.reasoning, "Let me think step by step.")
        self.assertEqual(deltas.joined("reasoning"), "Let me think step by step.")
        self.assertGreater(deltas.count("text"), 1)
        self.assertEqual(result.stop, "end")
        self.assertEqual(result.usage, {"input_tokens": 125, "output_tokens": 9, "cached_tokens": 100})
        self.assertEqual(result.message, {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "Let me think step by step.", "signature": "SIG/abc+=="},
            {"type": "text", "text": "The answer is 42."}]})

    def test_request_shape_and_headers(self) -> None:
        server = self.start_mock([{"text": "ok"}])
        provider, _ = self.anthropic(server)
        model = model_cfg(provider.cfg, extra_body={"thinking": {"type": "enabled", "budget_tokens": 2048}})
        provider.complete(model, "SYS", [provider.user_message("hi")], TOOLS)
        request = server.requests[0]
        self.assertEqual(request.path, "/v1/messages")
        self.assertEqual(request.headers["x-api-key"], KEY)
        self.assertEqual(request.headers["anthropic-version"], "2023-06-01")
        self.assertEqual(request.headers["content-type"], "application/json")
        self.assertIsNone(request.headers.get("Authorization"))
        body = request.body
        self.assertEqual(body["system"], "SYS")
        self.assertEqual(body["messages"], [{"role": "user", "content": "hi"}])
        self.assertEqual(body["max_tokens"], 1024)
        self.assertIs(body["stream"], True)
        self.assertEqual(body["thinking"], {"type": "enabled", "budget_tokens": 2048})
        self.assertEqual(body["tools"], [{"name": t["name"], "description": t["description"],
                                          "input_schema": t["input_schema"]} for t in TOOLS])
        self.assertNotIn("stream_options", body)

    def test_no_tools_and_no_system_keys_when_empty(self) -> None:
        server = self.start_mock([{"text": "ok"}])
        provider, _ = self.anthropic(server)
        provider.complete(model_cfg(provider.cfg, stream=False), "", [provider.user_message("hi")], [])
        self.assertNotIn("tools", server.requests[0].body)
        self.assertNotIn("system", server.requests[0].body)

    def test_streaming_multiple_tool_uses(self) -> None:
        server = self.start_mock([{"reasoning": "Two tools.", "text": "Checking.", "tool_calls": [
            {"name": "Read", "arguments": {"file_path": "README.md"}, "id": "toolu_1"},
            {"name": "Bash", "arguments": "", "id": "toolu_2"}]}])
        provider, _ = self.anthropic(server)
        result = provider.complete(model_cfg(provider.cfg), "s", [], TOOLS)
        self.assertEqual(result.stop, "tool_use")
        self.assertEqual([(c.id, c.name) for c in result.tool_calls], [("toolu_1", "Read"), ("toolu_2", "Bash")])
        self.assertEqual(json.loads(result.tool_calls[0].arguments), {"file_path": "README.md"})
        self.assertEqual(result.tool_calls[1].arguments, "{}")
        blocks = result.message["content"]
        self.assertEqual([b["type"] for b in blocks], ["thinking", "text", "tool_use", "tool_use"])
        self.assertEqual(blocks[2], {"type": "tool_use", "id": "toolu_1", "name": "Read",
                                     "input": {"file_path": "README.md"}})
        self.assertEqual(blocks[3]["input"], {})

    def test_truncated_tool_json_is_passed_raw(self) -> None:
        server = self.start_mock([{"tool_calls": [{"name": "Write", "arguments": '{"file_path": "a", "content": "abc'}],
                                   "finish": "length"}])
        provider, _ = self.anthropic(server)
        result = provider.complete(model_cfg(provider.cfg), "s", [], TOOLS)
        self.assertEqual(result.stop, "length")
        self.assertEqual(result.tool_calls[0].arguments, '{"file_path": "a", "content": "abc')
        self.assertEqual(result.message["content"][0]["input"], {})

    def test_redacted_thinking_is_preserved(self) -> None:
        server = self.start_mock([{"reasoning": "visible", "redacted": "ENCRYPTED==", "text": "hi"}] * 2)
        provider, _ = self.anthropic(server)
        for stream in (True, False):
            with self.subTest(stream=stream):
                result = provider.complete(model_cfg(provider.cfg, stream=stream), "s", [], [])
                self.assertEqual(result.message["content"][1], {"type": "redacted_thinking", "data": "ENCRYPTED=="})

    def test_non_streaming_blocks_and_deltas(self) -> None:
        server = self.start_mock([{"reasoning": "deep", "text": "Result.", "tool_calls": [
            {"name": "Read", "arguments": {"file_path": "x"}, "id": "toolu_9"}],
            "usage": {"input_tokens": 7, "output_tokens": 3}}])
        provider, _ = self.anthropic(server)
        deltas = Deltas()
        result = provider.complete(model_cfg(provider.cfg, stream=False), "s", [], TOOLS, on_delta=deltas)
        self.assertEqual(deltas.items, [("reasoning", "deep"), ("text", "Result.")])
        self.assertEqual(result.tool_calls[0].arguments, json.dumps({"file_path": "x"}))
        self.assertEqual(result.stop, "tool_use")
        self.assertEqual(result.usage, {"input_tokens": 7, "output_tokens": 3, "cached_tokens": 0})
        self.assertTrue(result.message["content"][0]["signature"].startswith("sig-"))
        self.assertEqual([b["type"] for b in result.message["content"]], ["thinking", "text", "tool_use"])

    def test_stop_reason_mapping(self) -> None:
        cases = [("end", "end"), ("stop_sequence", "end"), ("length", "length"), ("refusal", "refusal"),
                 ("pause_turn", "other")]
        server = self.start_mock([{"text": "x", "finish": finish} for finish, _ in cases])
        provider, _ = self.anthropic(server)
        for finish, expected in cases:
            with self.subTest(finish=finish):
                self.assertEqual(provider.complete(model_cfg(provider.cfg), "s", [], []).stop, expected)

    def test_refusal_stop_and_details(self) -> None:
        details = {"type": "refusal", "category": "cyber", "explanation": None}
        server = self.start_mock([{"text": "I can't help", "finish": "refusal", "stop_details": details}] * 2
                                 + [{"tool_calls": [{"name": "Read", "arguments": {}}], "finish": "refusal",
                                     "stop_details": details}, {"text": "fine"}])
        provider, _ = self.anthropic(server)
        for stream in (True, False):
            with self.subTest(stream=stream):
                result = provider.complete(model_cfg(provider.cfg, stream=stream), "s", [], [])
                self.assertEqual(result.stop, "refusal")
                self.assertEqual(result.stop_details, details)
        partial = provider.complete(model_cfg(provider.cfg), "s", [], TOOLS)
        self.assertEqual(partial.stop, "refusal")  # not "tool_use": the loop must not run the tool
        normal = provider.complete(model_cfg(provider.cfg), "s", [], [])
        self.assertEqual((normal.stop, normal.stop_details), ("end", {}))

    def test_a_stream_answering_a_request_without_one_is_the_answer(self) -> None:
        server = self.start_mock([{"reasoning": "think", "text": "Streamed.", "force_stream": True}])
        provider, sleep = self.anthropic(server)
        result = provider.complete(model_cfg(provider.cfg, stream=False), "s", [provider.user_message("hi")], [])
        self.assertIs(server.requests[0].body["stream"], False)
        self.assertEqual((result.text, result.reasoning, result.stop), ("Streamed.", "think", "end"))
        self.assertEqual((len(server.requests), sleep.total), (1, 0))

    def test_a_plain_json_answer_to_a_stream_request_is_the_answer(self) -> None:
        server = self.start_mock([{"reasoning": "think", "text": "Whole.", "plain_json": True}])
        provider, sleep = self.anthropic(server)
        deltas = Deltas()
        result = provider.complete(model_cfg(provider.cfg), "s", [provider.user_message("hi")], [], on_delta=deltas)
        self.assertIs(server.requests[0].body["stream"], True)
        self.assertEqual((result.text, result.reasoning, result.stop), ("Whole.", "think", "end"))
        self.assertEqual(deltas.items, [("reasoning", "think"), ("text", "Whole.")])
        self.assertEqual((len(server.requests), sleep.total), (1, 0))

    def test_eager_tools_only_when_streaming(self) -> None:
        server = self.start_mock([{"text": "ok"}] * 4)
        provider, _ = self.anthropic(server)
        provider.complete(model_cfg(provider.cfg, eager_tools=True), "s", [], TOOLS)
        provider.complete(model_cfg(provider.cfg, stream=False, eager_tools=True), "s", [], TOOLS)
        provider.complete(model_cfg(provider.cfg, eager_tools=True, extra_body={"stream": False}), "s", [], TOOLS)
        provider.complete(model_cfg(provider.cfg), "s", [], TOOLS)
        eager = [[tool.get("eager_input_streaming") for tool in request.body["tools"]] for request in server.requests]
        self.assertEqual(eager, [[True, True], [None, None], [None, None], [None, None]])
        self.assertEqual(server.requests[0].body["tools"][0]["input_schema"], TOOLS[0]["input_schema"])

    def test_stream_events_without_index(self) -> None:
        """Some Anthropic-compatible gateways omit `index`: deltas go to the latest started block."""
        raw = sse({"type": "message_start", "message": {"usage": {"input_tokens": 3}}},
                  {"type": "content_block_start", "content_block": {"type": "thinking", "thinking": "",
                                                                    "signature": ""}},
                  {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "hm"}},
                  {"type": "content_block_delta", "delta": {"type": "signature_delta", "signature": "s1"}},
                  {"type": "content_block_start", "content_block": {"type": "text", "text": ""}},
                  {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}},
                  {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
                  {"type": "message_stop"})
        no_start = sse({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "bare"}},
                       {"type": "message_delta", "delta": {"stop_reason": "end_turn"}})
        server = self.start_mock([{"raw": raw}, {"raw": no_start}])
        provider, _ = self.anthropic(server)
        result = provider.complete(model_cfg(provider.cfg), "s", [], [])
        self.assertEqual(result.message["content"], [{"type": "thinking", "thinking": "hm", "signature": "s1"},
                                                     {"type": "text", "text": "hi"}])
        self.assertEqual(provider.complete(model_cfg(provider.cfg), "s", [], []).text, "bare")

    def test_tool_loop_round_trip_keeps_thinking_verbatim(self) -> None:
        server = self.start_mock([
            {"reasoning": "Read it.", "signature": "sig-1",
             "tool_calls": [{"name": "Read", "arguments": {"file_path": "a"}}]},
            {"text": "Done."}])
        provider, _ = self.anthropic(server)
        model = model_cfg(provider.cfg)
        messages = [provider.user_message("go")]
        first = provider.complete(model, "s", messages, TOOLS)
        messages.append(first.message)
        messages.extend(provider.tool_results_message([(first.tool_calls[0].id, "file text", False),
                                                       ("toolu_x", "boom", True)]))
        self.assertEqual(provider.complete(model, "s", messages, TOOLS).text, "Done.")
        sent = server.requests[1].body["messages"]
        self.assertEqual(sent[1]["content"][0], {"type": "thinking", "thinking": "Read it.", "signature": "sig-1"})
        self.assertEqual(sent[2], {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": first.tool_calls[0].id, "content": "file text", "is_error": False},
            {"type": "tool_result", "tool_use_id": "toolu_x", "content": "boom", "is_error": True}]})

    def test_overloaded_error_event_is_retried(self) -> None:
        raw = ("event: message_start\ndata: " + json.dumps({"type": "message_start", "message": {"usage": {}}}) + "\n\n"
               "event: error\ndata: " + json.dumps({"type": "error", "error": {"type": "overloaded_error",
                                                                              "message": "Overloaded"}}) + "\n\n")
        server = self.start_mock([{"raw": raw}, {"text": "recovered"}])
        provider, sleep = self.anthropic(server)
        self.assertEqual(provider.complete(model_cfg(provider.cfg), "s", [], []).text, "recovered")
        self.assertEqual(sleep.total, 2)

    def test_invalid_request_error_event_is_not_retried(self) -> None:
        raw = "event: error\ndata: " + json.dumps({"type": "error", "error": {"type": "invalid_request_error",
                                                                              "message": "nope"}}) + "\n\n"
        server = self.start_mock([{"raw": raw}])
        provider, _ = self.anthropic(server)
        with self.assertRaises(ProviderError) as ctx:
            provider.complete(model_cfg(provider.cfg), "s", [], [])
        self.assertFalse(ctx.exception.retryable)
        self.assertIn("nope", ctx.exception.body)

    def test_truncated_stream_is_retried(self) -> None:
        server = self.start_mock([{"text": "partial", "truncate": True}, {"text": "whole"}])
        provider, _ = self.anthropic(server)
        self.assertEqual(provider.complete(model_cfg(provider.cfg), "s", [], []).text, "whole")
        self.assertEqual(len(server.requests), 2)

    def test_empty_text_blocks_are_not_echoed(self) -> None:
        raw = json.dumps({"type": "message", "content": [{"type": "text", "text": ""},
                                                         {"type": "tool_use", "id": "t1", "name": "Read", "input": {}}],
                          "stop_reason": "tool_use", "usage": {"input_tokens": 1, "output_tokens": 1}})
        server = self.start_mock([{"raw": raw}])
        provider, _ = self.anthropic(server)
        result = provider.complete(model_cfg(provider.cfg, stream=False), "s", [], TOOLS)
        self.assertEqual(result.message["content"], [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}])

    def test_retries_apply_to_anthropic_too(self) -> None:
        server = self.start_mock([{"text": "ok"}], error_sequence=[529])
        provider, sleep = self.anthropic(server)
        self.assertEqual(provider.complete(model_cfg(provider.cfg), "s", [], []).text, "ok")
        self.assertEqual(sleep.total, 2)


class RobustnessTests(MockTestCase):
    def test_malformed_structure_is_a_non_retryable_provider_error(self) -> None:
        cases = [
            ("openai", True, sse({"choices": ["not a dict"]}, "[DONE]")),
            ("openai", False, json.dumps({"choices": [{"message": {"content": "x"}, "finish_reason": "stop"}],
                                          "usage": {"prompt_tokens": "lots"}})),
            ("anthropic", True, sse({"type": "content_block_delta", "index": 0, "delta": "text"},
                                    {"type": "message_stop"})),
            ("anthropic", False, json.dumps({"content": [{"type": "text", "text": "x"}], "stop_reason": "end_turn",
                                             "usage": ["not", "a", "dict"]})),
        ]
        server = self.start_mock([{"raw": raw} for _, _, raw in cases])
        for type_, stream, _ in cases:
            with self.subTest(type=type_, stream=stream):
                sleep = FakeSleep()
                provider = make_provider(provider_cfg(server.base_url, type=type_))
                provider._sleep = sleep
                with self.assertRaises(ProviderError) as ctx:
                    provider.complete(model_cfg(provider.cfg, stream=stream), "s", [], [])
                self.assertIn("malformed response", ctx.exception.body)
                self.assertFalse(ctx.exception.retryable)
                self.assertEqual(sleep.calls, [])
        self.assertEqual(len(server.requests), len(cases))

    def test_on_delta_errors_propagate_unchanged_and_are_not_retried(self) -> None:
        server = self.start_mock([{"text": "hello"}] * 2)
        provider, sleep = self.openai(server)

        def buggy(kind: str, chunk: str) -> None:
            raise KeyError("bug in the caller")

        def disk_full(kind: str, chunk: str) -> None:
            raise OSError("disk full")

        with self.assertRaises(KeyError):  # not disguised as a "malformed response"
            provider.complete(model_cfg(provider.cfg), "s", [], [], on_delta=buggy)
        with self.assertRaises(OSError) as ctx:  # not a retryable "connection error"
            provider.complete(model_cfg(provider.cfg), "s", [], [], on_delta=disk_full)
        self.assertEqual(str(ctx.exception), "disk full")
        self.assertEqual(len(server.requests), 2)
        self.assertEqual(sleep.calls, [])


class MessageHelperTests(unittest.TestCase):
    def test_openai_messages(self) -> None:
        provider = OpenAIProvider(provider_cfg("http://127.0.0.1:1"))
        self.assertEqual(provider.type, "openai")
        self.assertEqual(provider.user_message("hello"), {"role": "user", "content": "hello"})
        self.assertEqual(provider.tool_results_message([("a", "out", False), ("b", "err", True)]), [
            {"role": "tool", "tool_call_id": "a", "content": "out"},
            {"role": "tool", "tool_call_id": "b", "content": "err"}])
        self.assertEqual(provider.tool_results_message([]), [])

    def test_anthropic_messages(self) -> None:
        provider = AnthropicProvider(provider_cfg("http://127.0.0.1:1", type="anthropic"))
        self.assertEqual(provider.type, "anthropic")
        self.assertEqual(provider.user_message("hello"), {"role": "user", "content": "hello"})
        self.assertEqual(provider.tool_results_message([("t", "out", True)]), [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t", "content": "out", "is_error": True}]}])
        self.assertEqual(provider.tool_results_message([]), [])

    def test_make_provider(self) -> None:
        self.assertIsInstance(make_provider(provider_cfg("http://x", type="openai")), OpenAIProvider)
        self.assertIsInstance(make_provider(provider_cfg("http://x", type="anthropic")), AnthropicProvider)
        with self.assertRaises(ValueError):
            make_provider(provider_cfg("http://x", type="gemini"))

    def test_provider_error_str(self) -> None:
        self.assertEqual(str(ProviderError(None, "cancelled")), "cancelled")
        self.assertEqual(str(ProviderError(503, "busy", True)), "HTTP 503: busy")


class MockLLMTests(unittest.TestCase):
    def get(self, url: str) -> tuple[int, dict]:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    def test_models_route_unknown_route_and_lifecycle(self) -> None:
        with MockLLM() as server:
            self.assertEqual(server.anthropic_base_url, server.base_url)
            self.assertEqual(server.start(), server.base_url)  # idempotent
            status, body = self.get(server.base_url + "/v1/models")
            self.assertEqual((status, body["data"][0]["id"]), (200, "mock-model"))
            self.assertEqual(self.get(server.base_url + "/nope")[0], 404)
        with self.assertRaises(RuntimeError):
            server.base_url

    def test_handler_precedence_and_exhausted_script(self) -> None:
        seen: list[str] = []

        def handler(body: dict, path: str) -> dict | None:
            seen.append(path)
            return {"text": "from handler"} if "reviewer" in body["messages"][0]["content"] else None

        with MockLLM([{"text": "from script"}], handler=handler) as server:
            provider = OpenAIProvider(provider_cfg(server.base_url))
            self.assertEqual(provider.complete(model_cfg(provider.cfg), "reviewer", [], []).text, "from handler")
            self.assertEqual(provider.complete(model_cfg(provider.cfg), "builder", [], []).text, "from script")
            with self.assertRaises(ProviderError) as ctx:
                provider.complete(model_cfg(provider.cfg), "builder", [], [])
            self.assertIn("script exhausted", ctx.exception.body)
        self.assertEqual(seen, ["/chat/completions"] * 3)

    def test_handler_exception_fails_fast(self) -> None:
        def handler(body: dict, path: str) -> dict:
            raise KeyError("oops")

        with MockLLM(handler=handler) as server:
            provider = OpenAIProvider(provider_cfg(server.base_url))
            with self.assertRaises(ProviderError) as ctx:
                provider.complete(model_cfg(provider.cfg), "s", [], [])
            self.assertEqual(ctx.exception.status, 400)
            self.assertIn("KeyError", ctx.exception.body)

    def test_streamed_chunks_are_split(self) -> None:
        with MockLLM([{"reasoning": "abcdef", "text": "Hello world",
                       "tool_calls": [{"name": "Read", "arguments": {"file_path": "x"}}]}]) as server:
            request = urllib.request.Request(server.base_url + "/v1/chat/completions", method="POST",
                                             data=json.dumps({"stream": True, "messages": []}).encode(),
                                             headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=5) as response:
                text = response.read().decode()
        self.assertGreaterEqual(text.count('"reasoning_content"'), 2)
        self.assertGreaterEqual(text.count('"content": "'), 3)
        self.assertGreaterEqual(text.count('"arguments": "'), 3)  # name delta + 2+ argument pieces
        self.assertTrue(text.rstrip().endswith("data: [DONE]"))
        self.assertNotIn('"usage"', text)  # no stream_options.include_usage in this request


if __name__ == "__main__":
    unittest.main()
