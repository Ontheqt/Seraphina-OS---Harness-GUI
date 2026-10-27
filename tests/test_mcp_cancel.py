"""McpClient.call_result's typed statuses and its cancel event, and Stop reaching an MCP call through the Toolset and
the builtin loop (BROWSER_AND_CODEX.md §2.5, T7's unit half), against tests/mock_mcp_server.py."""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

from swfactory.config import McpServerConfig
from swfactory.harness.mcp import CALL_STATUSES, McpClient
from swfactory.harness.tools import ToolPolicy, Toolset
from tests.test_loop import RunnerCase, make_agent, model_cfg, provider_cfg
from tests.test_mcp import MOCK_SERVER, server_config


class CallResultTests(unittest.TestCase):
    def client(self, *args: str, **overrides) -> McpClient:
        client = McpClient(server_config(*args, **overrides), Path.cwd())
        self.addCleanup(client.close)
        client.start()
        return client

    def test_ok_tool_error_and_the_raw_parts(self) -> None:
        client = self.client()
        ok = client.call_result("echo", {"text": "hi"})
        self.assertEqual((ok.status, ok.text, ok.sent, ok.unknown), ("ok", "hi", True, False))
        image = client.call_result("image", {})
        self.assertEqual([part["type"] for part in image.content], ["text", "image"])   # the data itself is kept
        self.assertTrue(image.content[1]["data"])
        self.assertIn("[image omitted: image/png]", image.text)                          # and call() still drops it
        failed = client.call_result("fail", {})
        self.assertEqual((failed.status, failed.sent, failed.unknown), ("tool_error", True, False))
        unknown_tool = client.call_result("nope", {})   # a JSON-RPC error: the server saw it and refused
        self.assertEqual(unknown_tool.status, "tool_error")

    def test_a_timeout_after_sending_is_unknown(self) -> None:
        client = self.client()
        result = client.call_result("sleep", {"seconds": 5}, timeout=0.3)
        self.assertEqual((result.status, result.sent, result.unknown), ("timeout_sent", True, True))
        self.assertIn("outcome is unknown", result.text)

    def test_a_dead_server_is_transport_failed_and_unknown_when_sent(self) -> None:
        client = self.client("--crash-after", "0")
        result = client.call_result("echo", {"text": "x"})
        self.assertEqual(result.status, "transport_failed")
        self.assertTrue(result.sent)
        after = client.call_result("echo", {"text": "y"})   # the session is gone before this one is written
        self.assertEqual((after.status, after.sent, after.unknown), ("transport_failed", False, False))

    def test_cancel_returns_at_once_and_tells_the_server(self) -> None:
        client = self.client()
        cancel = threading.Event()
        threading.Timer(0.3, cancel.set).start()
        started = time.monotonic()
        result = client.call_result("sleep", {"seconds": 30}, timeout=60, cancel=cancel)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual((result.status, result.sent, result.unknown), ("cancelled", True, True))
        again = client.call_result("echo", {"text": "still here"})   # the session is usable afterwards
        self.assertEqual(again.text, "still here")

    def test_cancel_before_sending_never_sends(self) -> None:
        client = self.client()
        cancel = threading.Event()
        cancel.set()
        result = client.call_result("echo", {"text": "x"}, cancel=cancel)
        self.assertEqual(result.status, "cancelled")
        self.assertFalse(result.unknown and not result.sent)

    def test_the_allowlist_refuses_before_anything_is_sent(self) -> None:
        client = self.client(tools=["echo"])
        result = client.call_result("sleep", {"seconds": 1})
        self.assertEqual((result.status, result.sent), ("tool_error", False))

    def test_call_is_a_wrapper(self) -> None:
        client = self.client()
        self.assertEqual(client.call("echo", {"text": "a"}), ("a", False))
        text, is_error = client.call("sleep", {"seconds": 5}, timeout=0.2)
        self.assertTrue(is_error)
        self.assertIn("timed out after 0.2s", text)
        self.assertEqual(set(CALL_STATUSES), {"ok", "tool_error", "timeout_sent", "timeout_unsent",
                                              "transport_failed", "cancelled"})


class ToolsetCancelTests(unittest.TestCase):
    def test_stop_reaches_an_mcp_call_through_the_toolset(self) -> None:
        cfg = McpServerConfig(name="mock", command=sys.executable, args=[str(MOCK_SERVER)], timeout=60,
                              startup_timeout=10)
        client = McpClient(cfg, Path.cwd())
        client.start()
        cancel = threading.Event()
        toolset = Toolset([], ToolPolicy(cwd=Path.cwd(), read_roots=[], write_paths=[], cancel=cancel), None,
                          {"mock": client})
        self.addCleanup(toolset.close)
        threading.Timer(0.3, cancel.set).start()
        started = time.monotonic()
        text, is_error = toolset.call("mcp__mock__sleep", {"seconds": 30})
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertTrue(is_error)
        self.assertIn("cancelled", text)


class LoopStopTests(RunnerCase):
    def test_a_turn_ends_within_five_seconds_of_stop_during_an_mcp_call(self) -> None:
        servers = {"mock": McpServerConfig(name="mock", command=sys.executable, args=[str(MOCK_SERVER)],
                                           timeout=120, startup_timeout=10)}
        server = self.start_mock([
            {"tool_calls": [{"name": "mcp__mock__sleep", "arguments": {"seconds": 60}}]},
            {"text": "never reached"}])
        agent = make_agent(tools=["Read"], mcp=["mock"])
        ctx, _ = self.ctx(model_cfg(provider_cfg(server.base_url)), agent=agent, mcp_servers=servers)
        done: list = []
        thread = threading.Thread(target=lambda: done.append(self.runner.run(ctx, "Sleep.")), daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.requests and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.5)   # the sleep call is in flight now
        stopped = time.monotonic()
        ctx.cancel.set()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - stopped, 5.0)
        self.assertFalse(done[0].ok)
        self.assertEqual(len(server.requests), 1)   # the model was never asked again


if __name__ == "__main__":
    unittest.main()
