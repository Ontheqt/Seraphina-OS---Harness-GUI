"""Tests for swfactory.harness.mcp, run against tests/mock_mcp_server.py."""

from __future__ import annotations

import itertools
import json
import os
import re
import signal
import sys
import tempfile
import threading
import time
import tracemalloc
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from swfactory import __version__
from swfactory.harness import mcp
from swfactory.harness.mcp import McpClient, McpError
from swfactory.harness import sandbox as sandbox_mod
from swfactory.harness.sandbox import Sandbox, bwrap_available
from tests.helpers import outside_tmp

MOCK_SERVER = Path(__file__).resolve().with_name("mock_mcp_server.py")
ALL_TOOLS = ["echo", "image", "fail", "sleep", "ask_roots", "env", "big", "progress", "write"]


def server_config(*server_args: str, program: Path = MOCK_SERVER, **overrides) -> SimpleNamespace:
    """An McpServerConfig look-alike that runs the mock server (program) with server_args."""
    fields = dict(name="mock", command=sys.executable, args=[str(program), *server_args],
                  env={}, cwd=None, timeout=10.0, tools=None, startup_timeout=10.0)
    fields.update(overrides)
    return SimpleNamespace(**fields)


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.02)
    return True


def pid_alive(pid: int) -> bool:
    """True if pid exists and is not a zombie (init may reap orphans slowly)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat[stat.rfind(")") + 2:].split()[0] not in ("Z", "X")


def kill_quietly(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


class ChunkStream:
    """A stand-in for a pipe: read1() returns the next chunk, then b"" (end of file)."""

    def __init__(self, chunks) -> None:
        self._chunks = iter(chunks)

    def read1(self, size: int = -1) -> bytes:
        return next(self._chunks, b"")


class McpTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.logs: list[str] = []

    def make_client(self, cfg: SimpleNamespace | None = None, start: bool = True) -> McpClient:
        client = McpClient(cfg or server_config(), self.tmp, log=self.logs.append)
        self.addCleanup(client.close)
        if start:
            client.start()
        return client

    def wait_for_log(self, pattern: str, timeout: float = 5.0) -> re.Match:
        regex = re.compile(pattern)
        found: list[re.Match] = []

        def check() -> bool:
            found.extend(m for m in map(regex.search, list(self.logs)) if m)
            return bool(found)

        if not wait_until(check, timeout):
            self.fail(f"no log line matches {pattern!r}; logs:\n" + "\n".join(self.logs))
        return found[0]

    def server_pid(self) -> int:
        return int(self.wait_for_log(r"started pid=(\d+)").group(1))


class FormatResultTests(unittest.TestCase):
    def test_text_items_are_joined(self) -> None:
        result = {"content": [{"type": "text", "text": "one"}, {"type": "text", "text": "two"}]}
        self.assertEqual(mcp._format_result(result), ("one\ntwo", False))

    def test_image_and_audio_are_replaced_by_a_note(self) -> None:
        result = {"content": [{"type": "image", "data": "AAAA", "mimeType": "image/png"},
                              {"type": "audio", "data": "BBBB", "mimeType": "audio/wav"},
                              {"type": "image", "data": "CCCC"}]}
        self.assertEqual(mcp._format_result(result)[0],
                         "[image omitted: image/png]\n[audio omitted: audio/wav]\n"
                         "[image omitted: unknown type]")

    def test_resources_give_their_text_or_uri(self) -> None:
        result = {"content": [
            {"type": "resource", "resource": {"uri": "file:///a.txt", "text": "file body"}},
            {"type": "resource", "resource": {"uri": "file:///b.bin", "blob": "AAAA"}},
            {"type": "resource_link", "uri": "file:///c.md", "name": "c"},
        ]}
        self.assertEqual(mcp._format_result(result)[0], "file body\nfile:///b.bin\nfile:///c.md")

    def test_structured_content_is_used_only_without_text(self) -> None:
        structured = {"answer": 42, "items": ["é"]}
        self.assertEqual(mcp._format_result({"content": [], "structuredContent": structured}),
                         ('{"answer": 42, "items": ["é"]}', False))
        with_text = {"content": [{"type": "text", "text": "42"}], "structuredContent": structured}
        self.assertEqual(mcp._format_result(with_text), ("42", False))

    def test_is_error_flag(self) -> None:
        result = {"content": [{"type": "text", "text": "bad"}], "isError": True}
        self.assertEqual(mcp._format_result(result), ("bad", True))

    def test_malformed_results_do_not_raise(self) -> None:
        self.assertEqual(mcp._format_result({}), ("", False))
        self.assertEqual(mcp._format_result({"content": "not a list"}), ("", False))
        unknown = {"content": [{"type": "hologram", "x": 1}, "bare"]}
        self.assertEqual(mcp._format_result(unknown)[0], '{"type": "hologram", "x": 1}\n"bare"')


class StartupTests(McpTestCase):
    def test_start_lists_tools(self) -> None:
        client = self.make_client()
        tools = client.tools()
        self.assertEqual([tool["name"] for tool in tools], ALL_TOOLS)
        for tool in tools:
            self.assertEqual(set(tool), {"name", "description", "inputSchema"})
            self.assertTrue(tool["description"])
            self.assertEqual(tool["inputSchema"]["type"], "object")

    def test_tools_returns_copies(self) -> None:
        client = self.make_client()
        client.tools()[0]["name"] = "changed"
        client.tools().clear()
        self.assertEqual(client.tools()[0]["name"], "echo")

    def test_follows_next_cursor_pages(self) -> None:
        for pages in (3, 7):
            with self.subTest(pages=pages):
                self.logs.clear()
                client = self.make_client(server_config("--pages", str(pages)))
                self.assertEqual([tool["name"] for tool in client.tools()], ALL_TOOLS)
                self.wait_for_log(rf"tools/list page {pages - 1}$")
                client.close()

    def test_initialize_params(self) -> None:
        self.make_client()
        params = json.loads(self.wait_for_log(r"initialize params=(.*)$").group(1))
        self.assertEqual(params, {
            "protocolVersion": "2025-06-18",
            "capabilities": {"roots": {"listChanged": False}},
            "clientInfo": {"name": "software-factory", "version": __version__},
        })

    def test_accepts_older_server_protocol_version(self) -> None:
        self.make_client()  # the mock answers 2025-03-26
        self.wait_for_log(rf"connected to mock-mcp 1\.0 \(protocol 2025-03-26, {len(ALL_TOOLS)} tools\)")

    def test_logs_junk_stdout_stderr_and_log_notifications(self) -> None:
        client = self.make_client()
        self.assertEqual(client.call("echo", {"text": "still works"}), ("still works", False))
        self.wait_for_log(r"^mcp mock: non-JSON output: mock-mcp-server starting")
        self.wait_for_log(r"^mcp mock: stderr: mock-mcp: started pid=")
        self.wait_for_log(r"^mcp mock: \[info\] mock ready$")

    def test_env_is_inherited_and_cfg_env_overrides(self) -> None:
        with mock.patch.dict(os.environ, {"MCP_MOCK_TAG": "inherited"}):
            self.make_client().close()
            self.wait_for_log(r"started pid=\d+ cwd=.* tag=inherited$")
            self.make_client(server_config(env={"MCP_MOCK_TAG": "hello"}))
            self.wait_for_log(r"started pid=\d+ cwd=.* tag=hello$")

    def test_secrets_are_removed_from_the_server_environment(self) -> None:
        names = ["ANTHROPIC_BASE_URL", "DEEPSEEK_API_KEY", "FACTORY_TOKEN", "MY_ENDPOINT", "PLAIN_SETTING",
                 "STRATA_API_KEY", "AWS_SECRET_ACCESS_KEY", "PGPASSWORD"]
        cfg = server_config(env={"STRATA_API_KEY": "explicit"})
        expected = {"default": {"MY_ENDPOINT": "custom", "PLAIN_SETTING": "kept", "STRATA_API_KEY": "explicit"},
                    "named": {"PLAIN_SETTING": "kept", "STRATA_API_KEY": "explicit"}}
        for case, secret_names in (("default", ()), ("named", ["MY_ENDPOINT"])):
            with self.subTest(case=case):
                with mock.patch.dict(os.environ, {"ANTHROPIC_BASE_URL": "https://x", "DEEPSEEK_API_KEY": "sk-1",
                                                  "FACTORY_TOKEN": "tok", "MY_ENDPOINT": "custom",
                                                  "AWS_SECRET_ACCESS_KEY": "aws", "PGPASSWORD": "pg",
                                                  "PLAIN_SETTING": "kept", "STRATA_API_KEY": "inherited"}):
                    client = McpClient(cfg, self.tmp, secret_names=secret_names)
                    self.addCleanup(client.close)
                    client.start()
                text, is_error = client.call("env", {"names": names})
                self.assertFalse(is_error)
                self.assertEqual(json.loads(text), expected[case])

    def test_a_null_env_value_removes_the_variable(self) -> None:
        with mock.patch.dict(os.environ, {"PLAIN_SETTING": "inherited", "OTHER_SETTING": "kept"}):
            client = McpClient(server_config(env={"PLAIN_SETTING": None}), self.tmp)
            self.addCleanup(client.close)
            client.start()
        text, is_error = client.call("env", {"names": ["PLAIN_SETTING", "OTHER_SETTING"]})
        self.assertFalse(is_error)
        self.assertEqual(json.loads(text), {"OTHER_SETTING": "kept"})

    def test_cfg_cwd_is_the_process_cwd(self) -> None:
        server_cwd = self.tmp / "server-cwd"
        server_cwd.mkdir()
        self.make_client(server_config(cwd=str(server_cwd)))
        match = self.wait_for_log(r"started pid=\d+ cwd=(.*) tag=")
        self.assertEqual(Path(match.group(1)).resolve(), server_cwd.resolve())

    def test_process_cwd_defaults_to_client_cwd(self) -> None:
        self.make_client()
        match = self.wait_for_log(r"started pid=\d+ cwd=(.*) tag=")
        self.assertEqual(Path(match.group(1)).resolve(), self.tmp.resolve())

    def test_slow_initialize_within_startup_timeout(self) -> None:
        client = self.make_client(server_config("--slow-init", "0.5", startup_timeout=5))
        self.assertEqual(len(client.tools()), len(ALL_TOOLS))

    def test_initialize_timeout_raises_and_kills_server(self) -> None:
        client = self.make_client(server_config("--slow-init", "30", startup_timeout=0.3), start=False)
        with mock.patch.object(mcp, "_CLOSE_GRACE", 0.2):
            started = time.monotonic()
            with self.assertRaises(McpError) as caught:
                client.start()
            self.assertLess(time.monotonic() - started, 5)
        self.assertIn("failed to start", str(caught.exception))
        self.assertIn("initialize timed out after 0.3s", str(caught.exception))
        self.assertFalse(pid_alive(self.server_pid()))

    def test_startup_timeout_defaults_to_60_seconds(self) -> None:
        cfg = SimpleNamespace(name="old", command=sys.executable, args=[str(MOCK_SERVER)],
                              env={}, cwd=None, timeout=10.0)  # no tools / startup_timeout
        client = McpClient(cfg, self.tmp)
        self.assertEqual(client._startup_timeout, 60.0)
        self.addCleanup(client.close)
        client.start()
        self.assertEqual(len(client.tools()), len(ALL_TOOLS))

    def test_missing_command(self) -> None:
        client = self.make_client(server_config(command="/nonexistent/mcp-server"), start=False)
        with self.assertRaises(McpError) as caught:
            client.start()
        self.assertIn("cannot start MCP server mock", str(caught.exception))
        self.assertIn("/nonexistent/mcp-server", str(caught.exception))

    def test_missing_cwd(self) -> None:
        client = self.make_client(server_config(cwd=str(self.tmp / "missing")), start=False)
        with self.assertRaises(McpError):
            client.start()

    def test_server_that_exits_immediately(self) -> None:
        script = "import sys; print('boom: bad config', file=sys.stderr); sys.exit(2)"
        client = self.make_client(server_config(args=["-c", script]), start=False)
        with self.assertRaises(McpError) as caught:
            client.start()
        message = str(caught.exception)
        self.assertIn("exited with code 2", message)
        self.assertIn("boom: bad config", message)

    def test_non_mcp_process_times_out(self) -> None:
        script = "import sys, time; print('hello'); sys.stdout.flush(); time.sleep(30)"
        cfg = server_config(args=["-c", script], startup_timeout=0.3)
        client = self.make_client(cfg, start=False)
        with mock.patch.object(mcp, "_CLOSE_GRACE", 0.2), self.assertRaises(McpError) as caught:
            client.start()
        self.assertIn("timed out", str(caught.exception))
        self.wait_for_log(r"non-JSON output: hello")

    def test_start_twice_is_a_no_op(self) -> None:
        client = self.make_client()
        client.start()
        self.assertEqual(client.call("echo", {"text": "x"}), ("x", False))
        self.assertEqual(len([line for line in self.logs if "initialize params" in line]), 1)

    def test_allowlist_filters_tools_and_refuses_calls(self) -> None:
        client = self.make_client(server_config("--pages", "2", tools=["fail", "echo", "missing"]))
        self.assertEqual([tool["name"] for tool in client.tools()], ["echo", "fail"])
        self.wait_for_log(r"allowlisted tool missing is not offered by the server")
        self.assertEqual(client.call("echo", {"text": "ok"}), ("ok", False))
        text, is_error = client.call("image", {})
        self.assertTrue(is_error)
        self.assertIn("allowlist", text)

    def test_empty_allowlist_exposes_nothing(self) -> None:
        client = self.make_client(server_config(tools=[]))
        self.assertEqual(client.tools(), [])
        self.assertTrue(client.call("echo", {"text": "x"})[1])


def unknown_after(seconds: float) -> str:
    return (f"MCP call timed out after {seconds:g}s. The request was sent, so the tool may have run: its outcome is "
            "unknown. Check before you call it again.")


def not_sent_after(seconds: float) -> str:
    return f"MCP call timed out after {seconds:g}s before the request was sent: the tool did not run."


class CallTests(McpTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.client = self.make_client()

    def test_echo(self) -> None:
        self.assertEqual(self.client.call("echo", {"text": "héllo\nworld"}), ("héllo\nworld", False))

    def test_image_is_omitted(self) -> None:
        text, is_error = self.client.call("image", {})
        self.assertFalse(is_error)
        self.assertEqual(text, "an image follows\n[image omitted: image/png]")

    def test_fail_sets_is_error(self) -> None:
        self.assertEqual(self.client.call("fail", {}), ("tool failed on purpose", True))

    def test_json_rpc_error_is_returned_as_error(self) -> None:
        self.assertEqual(self.client.call("no-such-tool", {}), ("Unknown tool: no-such-tool", True))

    def test_timeout_sends_cancellation(self) -> None:
        started = time.monotonic()
        result = self.client.call("sleep", {"seconds": 30}, timeout=0.3)
        self.assertEqual(result, (unknown_after(0.3), True))
        self.assertLess(time.monotonic() - started, 5)
        self.wait_for_log(r"cancelled request \d+ reason=timed out after 0\.3s$")
        self.assertEqual(self.client.call("echo", {"text": "after"}), ("after", False))

    def test_tiny_timeouts_do_not_fail_a_healthy_session(self) -> None:
        for timeout in (0, 0.001) * 15:
            text, is_error = self.client.call("sleep", {"seconds": 5}, timeout=timeout)
            self.assertTrue(is_error)
            self.assertIn(text, (unknown_after(timeout), not_sent_after(timeout)))
        self.assertEqual(self.client.call("echo", {"text": "alive"}), ("alive", False))

    def test_default_call_timeout_comes_from_cfg(self) -> None:
        client = self.make_client(server_config(timeout=0.3))
        self.assertEqual(client.call("sleep", {"seconds": 30}), (unknown_after(0.3), True))

    def test_a_call_that_never_left_says_the_tool_did_not_run(self) -> None:
        """DESIGN §40: failed only when provably not sent; a sent call with no answer is unknown, never "failed"."""
        with mock.patch.object(self.client, "_request", side_effect=mcp._Timeout("tools/call", 9, 0.3, sent=False)):
            self.assertEqual(self.client.call("echo", {"text": "x"}, timeout=0.3), (not_sent_after(0.3), True))
        with mock.patch.object(self.client, "_request", side_effect=mcp._Timeout("tools/call", 9, 0.3, sent=True)):
            self.assertEqual(self.client.call("echo", {"text": "x"}, timeout=0.3), (unknown_after(0.3), True))
        with mock.patch.object(self.client, "_request", side_effect=McpError("Unknown tool: x", -32602)):
            self.assertEqual(self.client.call("x", {}), ("Unknown tool: x", True), "a refusal is the server's answer")

    def test_late_response_is_ignored(self) -> None:
        first = self.client.call("sleep", {"seconds": 0.4, "ignore_cancel": True}, timeout=0.1)
        self.assertEqual(first, (unknown_after(0.1), True))
        self.assertEqual(self.client.call("sleep", {"seconds": 0.6}), ("slept 0.6s", False))

    def test_concurrent_calls_get_their_own_responses(self) -> None:
        results: dict = {}

        def worker(index: int) -> None:
            results[index] = self.client.call("echo", {"text": f"msg-{index}"})

        slow = threading.Thread(target=lambda: results.setdefault(
            "slow", self.client.call("sleep", {"seconds": 0.3})))
        threads = [slow] + [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        expected: dict = {i: (f"msg-{i}", False) for i in range(20)}
        expected["slow"] = ("slept 0.3s", False)
        self.assertEqual(results, expected)

    def test_roots_list_round_trip(self) -> None:
        text, is_error = self.client.call("ask_roots", {})
        self.assertFalse(is_error)
        root = self.tmp.resolve()
        self.assertEqual(json.loads(text), {"result": {"roots": [{"uri": root.as_uri(), "name": root.name}]}})

    def test_server_ping_and_unknown_requests(self) -> None:
        self.assertEqual(json.loads(self.client.call("ask_roots", {"method": "ping"})[0]), {"result": {}})
        reply = json.loads(self.client.call("ask_roots", {"method": "sampling/createMessage"})[0])
        self.assertEqual(reply["error"]["code"], -32601)

    def test_call_before_start(self) -> None:
        client = self.make_client(start=False)
        text, is_error = client.call("echo", {"text": "x"})
        self.assertTrue(is_error)
        self.assertIn("not started", text)
        self.assertEqual(client.tools(), [])


class LifecycleTests(McpTestCase):
    def test_crash_during_call(self) -> None:
        client = self.make_client(server_config("--crash-after", "0"))
        text, is_error = client.call("echo", {"text": "x"})
        self.assertTrue(is_error)
        self.assertIn("exited with code 3", text)
        self.assertIn("crashing on purpose", text)
        self.assertIn("The request was sent before that, so the tool may have run: its outcome is unknown", text)
        started = time.monotonic()
        again, is_error = client.call("echo", {"text": "again"})
        self.assertTrue(is_error)
        self.assertEqual(again, text.split(". The request was sent")[0], "not sent: nothing may have run")
        self.assertLess(time.monotonic() - started, 1)
        self.wait_for_log(r"mcp mock: MCP server mock exited with code 3")

    def test_crash_after_some_calls(self) -> None:
        client = self.make_client(server_config("--crash-after", "2"))
        self.assertEqual(client.call("echo", {"text": "1"}), ("1", False))
        self.assertEqual(client.call("echo", {"text": "2"}), ("2", False))
        text, is_error = client.call("echo", {"text": "3"})
        self.assertTrue(is_error)
        self.assertIn("exited with code 3", text)

    def test_crash_wakes_every_waiting_call(self) -> None:
        client = self.make_client(server_config("--crash-after", "1"))
        results: list = []
        sleeper = threading.Thread(target=lambda: results.append(
            client.call("sleep", {"seconds": 30}, timeout=20)))
        sleeper.start()
        time.sleep(0.2)  # let the sleep request reach the server first
        self.assertTrue(client.call("echo", {"text": "boom"})[1])
        sleeper.join(5)
        self.assertFalse(sleeper.is_alive())
        self.assertTrue(results[0][1])
        self.assertIn("exited with code 3", results[0][0])

    def test_close_is_idempotent_and_clean(self) -> None:
        client = self.make_client()
        pid = self.server_pid()
        started = time.monotonic()
        client.close()
        self.assertLess(time.monotonic() - started, 1.5)  # exits on stdin EOF, no signals needed
        client.close()
        self.assertFalse(pid_alive(pid))
        self.wait_for_log(r"mock-mcp: stdin closed")
        self.assertNotIn("exited with code", "\n".join(self.logs))  # a requested close is not a crash
        text, is_error = client.call("echo", {"text": "x"})
        self.assertTrue(is_error)
        self.assertIn("closed", text)
        with self.assertRaises(McpError):
            client.start()

    def test_close_before_start(self) -> None:
        client = McpClient(server_config(), self.tmp)
        client.close()
        client.close()
        with self.assertRaises(McpError):
            client.start()

    def test_close_kills_a_stubborn_server(self) -> None:
        client = self.make_client(server_config("--stubborn"))
        pid = self.server_pid()
        with mock.patch.object(mcp, "_CLOSE_GRACE", 0.3):
            started = time.monotonic()
            client.close()
            self.assertLess(time.monotonic() - started, 3)
        self.assertFalse(pid_alive(pid))

    def test_close_wakes_a_waiting_call(self) -> None:
        client = self.make_client()
        results: list = []
        caller = threading.Thread(target=lambda: results.append(
            client.call("sleep", {"seconds": 30}, timeout=20)))
        caller.start()
        time.sleep(0.2)
        client.close()
        caller.join(5)
        self.assertFalse(caller.is_alive())
        self.assertEqual(results, [("MCP server mock is closed. The request was sent before that, " + mcp.UNKNOWN_WORDS,
                                    True)], "a call that was out when the session closed may have run")

    def test_concurrent_close_calls(self) -> None:
        client = self.make_client()
        threads = [threading.Thread(target=client.close) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertFalse(pid_alive(self.server_pid()))

    def test_broken_log_callback_does_not_break_the_client(self) -> None:
        def explode(line: str) -> None:
            raise RuntimeError("log sink down")

        client = McpClient(server_config(), self.tmp, log=explode)
        self.addCleanup(client.close)
        client.start()
        self.assertEqual(client.call("echo", {"text": "fine"}), ("fine", False))


class ProcessGroupTests(McpTestCase):
    """Helpers a server starts share its process group and must not outlive close()."""

    def helper_pid(self) -> int:
        pid = int(self.wait_for_log(r"helper pid=(\d+)").group(1))
        self.addCleanup(kill_quietly, pid)  # even if close() regresses
        return pid

    def test_close_stops_helpers_of_a_server_that_exits_cleanly(self) -> None:
        client = self.make_client(server_config("--helper", "quiet"))
        helper = self.helper_pid()
        self.assertTrue(pid_alive(helper))
        started = time.monotonic()
        client.close()
        self.assertLess(time.monotonic() - started, 1.5)
        self.wait_for_log(r"mock-mcp: stdin closed")  # the server left on its own
        self.assertFalse(pid_alive(helper))
        self.assertFalse(pid_alive(self.server_pid()))

    def test_crash_is_reported_while_a_helper_holds_stdout_open(self) -> None:
        client = self.make_client(server_config("--helper", "inherit", "--crash-after", "0"))
        helper = self.helper_pid()
        started = time.monotonic()
        text, is_error = client.call("echo", {"text": "x"})  # cfg.timeout is 10s
        self.assertLess(time.monotonic() - started, 3)
        self.assertTrue(is_error)
        self.assertIn("exited with code 3", text)
        self.assertIn("crashing on purpose", text)
        self.assertTrue(pid_alive(helper))
        started = time.monotonic()
        client.close()
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertFalse(pid_alive(helper))

    def test_close_stops_helpers_of_a_stubborn_server(self) -> None:
        client = self.make_client(server_config("--stubborn", "--helper", "inherit"))
        helper, server = self.helper_pid(), self.server_pid()
        with mock.patch.object(mcp, "_CLOSE_GRACE", 0.3):
            started = time.monotonic()
            client.close()
            self.assertLess(time.monotonic() - started, 3)
        self.assertFalse(pid_alive(server))
        self.assertFalse(pid_alive(helper))


class SandboxedServerTests(McpTestCase):
    """With the agent run's Sandbox, the server starts the way that sandbox runs a Bash command."""

    def sandboxed(self, mode: str, *server_args: str) -> tuple[McpClient, Path]:
        work = self.tmp / "work"
        work.mkdir(exist_ok=True)
        # under bwrap a server kept below /tmp (a checkout there) is hidden by the sandbox's own /tmp
        program = outside_tmp(MOCK_SERVER, self) if mode == "bwrap" else MOCK_SERVER
        client = McpClient(server_config(*server_args, program=program), work, log=self.logs.append,
                           sandbox=Sandbox(mode, work, [work], True, {}))
        self.addCleanup(client.close)
        client.start()
        return client, work

    def test_a_plain_sandbox_starts_the_server_as_it_is(self) -> None:
        client, work = self.sandboxed("none")
        outside = self.tmp / "outside.txt"
        self.assertEqual(client.call("write", {"path": str(outside), "text": "x"}), (f"wrote {outside}", False))
        self.assertEqual(outside.read_text(), "x", "without bwrap a server writes wherever the user can")
        match = self.wait_for_log(r"started pid=\d+ cwd=(.*) tag=")
        self.assertEqual(Path(match.group(1)).resolve(), work.resolve())

    @unittest.skipUnless(sandbox_mod.setpriv_path(), "setpriv (util-linux) is not installed here")
    def test_without_bwrap_the_server_starts_without_new_privileges(self) -> None:
        """DESIGN §17: what an agent starts outside bwrap never gains rights through a setuid program (sudo)."""
        if sandbox_mod.no_new_privs_set():
            self.skipTest("this test process has no-new-privileges already")
        self.sandboxed("none")
        pid = int(self.wait_for_log(r"started pid=(\d+) ").group(1))
        status = Path(f"/proc/{pid}/status").read_text()
        self.assertRegex(status, r"NoNewPrivs:\s+1")

    @unittest.skipUnless(bwrap_available(), "bubblewrap is not usable here")
    def test_under_bwrap_the_server_writes_only_what_the_agents_bash_can(self) -> None:
        client, work = self.sandboxed("bwrap")
        self.assertFalse(client.call("write", {"path": str(work / "inside.txt"), "text": "kept"})[1])
        self.assertEqual((work / "inside.txt").read_text(), "kept")
        probe = Path("/usr/sf-mcp-bwrap-probe")
        self.addCleanup(lambda: probe.unlink(missing_ok=True))
        text, is_error = client.call("write", {"path": str(probe), "text": "x"})
        self.assertTrue(is_error)
        self.assertIn("Read-only file system", text)
        self.assertFalse(probe.exists())
        victim = self.tmp / "victim.txt"
        victim.write_text("precious")
        client.call("write", {"path": str(victim), "text": "pwned"})
        self.assertEqual(victim.read_text(), "precious", "the server's /tmp is the sandbox's own")
        self.assertEqual(client.call("echo", {"text": "still here"}), ("still here", False))

    @unittest.skipUnless(bwrap_available(), "bubblewrap is not usable here")
    def test_close_stops_a_sandboxed_server_and_its_helpers(self) -> None:
        client, _ = self.sandboxed("bwrap", "--helper", "quiet")
        self.wait_for_log(r"helper pid=\d+")
        group = client._proc.pid
        self.assertTrue(mcp._group_running(group))
        started = time.monotonic()
        client.close()
        self.assertLess(time.monotonic() - started, 3)
        self.assertFalse(mcp._group_running(group), "bwrap keeps the server and its helpers in the group")

    def test_bwrap_configured_but_unusable_fails_the_start(self) -> None:
        client = McpClient(server_config(), self.tmp, log=self.logs.append,
                           sandbox=Sandbox("bwrap", self.tmp, [self.tmp], True, {}))
        self.addCleanup(client.close)
        with mock.patch.object(mcp, "bwrap_available", return_value=False):
            with self.assertRaises(McpError) as caught:
                client.start()
        self.assertIn("bubblewrap is not usable on this host", str(caught.exception))


class StalledStdinTests(McpTestCase):
    """A server that stops reading its stdin must not hang call(), other calls or the reader."""

    def deaf_client(self) -> McpClient:
        client = self.make_client(server_config("--deaf"))
        self.wait_for_log(r"deaf: no longer reading stdin")
        return client

    def call_within(self, limit: float, client: McpClient, tool: str, arguments: dict,
                    timeout: float | None = None) -> tuple[str, bool]:
        """client.call() in a daemon thread, so a regression fails the test instead of hanging it."""
        results: list = []
        caller = threading.Thread(target=lambda: results.append(client.call(tool, arguments, timeout)),
                                  daemon=True)
        caller.start()
        caller.join(limit)
        if caller.is_alive():
            self.fail(f"call({tool!r}) still blocked after {limit:g}s")
        return results[0]

    def test_large_request_fails_the_session_within_its_timeout(self) -> None:
        client = self.deaf_client()
        text, is_error = self.call_within(2, client, "echo", {"text": "a" * 300_000}, timeout=0.5)
        self.assertTrue(is_error)
        failure = "MCP server mock stopped reading its stdin (a tools/call request could not be written within 0.5s)"
        self.assertEqual(text, f"{failure}. Part of the request was written and the rest may still reach the "
                               f"server, so the tool may have run: its outcome is unknown. Check before you call "
                               f"it again.", "half in the pipe: unknown, never 'failed' (DESIGN §40)")
        self.assertEqual(self.call_within(0.5, client, "echo", {"text": "x"}), (failure, True),
                         "a call after the session failed never left")
        server = self.server_pid()
        with mock.patch.object(mcp, "_CLOSE_GRACE", 0.3):
            started = time.monotonic()
            client.close()
            self.assertLess(time.monotonic() - started, 3)
        self.assertFalse(pid_alive(server))

    def test_small_requests_stay_bounded_while_the_pipe_fills(self) -> None:
        client = self.deaf_client()
        for _ in range(200):
            text, is_error = self.call_within(1, client, "echo", {"text": "b" * 4000}, timeout=0.05)
            self.assertTrue(is_error)
            if "stopped reading its stdin" in text:
                break
            self.assertIn(text, (unknown_after(0.05), not_sent_after(0.05)))
        else:
            self.fail("the stalled stdin was never detected")

    def test_reader_keeps_routing_while_a_write_is_stuck(self) -> None:
        # The deaf server keeps sending pings; their replies must queue, not block the reader.
        client = self.deaf_client()
        results: list = []
        caller = threading.Thread(target=lambda: results.append(
            client.call("echo", {"text": "c" * 300_000}, timeout=4)), daemon=True)
        caller.start()
        time.sleep(0.3)
        tick = re.compile(r"\[info\] tick (\d+)$")

        def last_tick() -> int:
            return max([int(m.group(1)) for m in map(tick.search, list(self.logs)) if m] or [0])

        seen = last_tick()
        self.assertTrue(wait_until(lambda: last_tick() >= seen + 10, timeout=2.5))
        self.assertTrue(caller.is_alive())  # the stuck write is still within its timeout
        caller.join(8)
        self.assertFalse(caller.is_alive())
        self.assertIn("stopped reading its stdin", results[0][0])

    def test_server_that_closed_its_stdin(self) -> None:
        client = self.make_client(server_config("--close-stdin"))
        self.wait_for_log(r"mock-mcp: closed stdin")
        with mock.patch.object(mcp, "_CLOSE_GRACE", 0.2):
            started = time.monotonic()
            text, is_error = client.call("echo", {"text": "x"})
            self.assertLess(time.monotonic() - started, 3)
        self.assertTrue(is_error)
        self.assertIn("MCP server mock stopped reading its stdin: [Errno 32] Broken pipe", text)


class OutputLimitTests(McpTestCase):
    def test_oversized_stdout_message_fails_the_session(self) -> None:
        with mock.patch.object(mcp, "_MAX_MESSAGE_BYTES", 4096):
            client = self.make_client()
            self.assertEqual(client.call("big", {"size": 1000}), ("x" * 1000, False))
            text, is_error = client.call("big", {"size": 100_000})
            self.assertTrue(is_error)
            self.assertEqual(text, "MCP server mock sent a message larger than 4096 bytes. The request was sent before "
                               "that, " + mcp.UNKNOWN_WORDS)
            self.assertEqual(client.call("echo", {"text": "x"}),
                             ("MCP server mock sent a message larger than 4096 bytes", True), "never sent: did not run")
            client.close()

    def test_progress_bar_on_stderr_logs_its_final_state(self) -> None:
        client = self.make_client()
        self.assertEqual(client.call("progress", {"steps": 50}), ("progressed", False))
        self.wait_for_log(r"stderr: progress done$")
        self.assertEqual([line for line in self.logs if "progress 1/" in line], [])


class StderrLinesTests(unittest.TestCase):
    @staticmethod
    def lines(*chunks: bytes) -> list:
        return list(mcp._stderr_lines(ChunkStream(chunks)))

    def test_newlines_crlf_and_last_line_without_newline(self) -> None:
        self.assertEqual(self.lines(b"one\ntwo\r\n\nthree"), ["one", "two", "", "three"])

    def test_lines_split_across_chunks(self) -> None:
        self.assertEqual(self.lines(b"spl", b"it\r", b"\nnext\r", b"\n"), ["split", "next"])

    def test_carriage_return_redraws_the_line(self) -> None:
        self.assertEqual(self.lines(b"\r10%\r50%", b"\r100%\n", b"a\rb\n", b"x\r", b"y\n"),
                         ["100%", "b", "y"])

    def test_long_lines_keep_their_head(self) -> None:
        with mock.patch.object(mcp, "_STDERR_LINE_BYTES", 10):
            self.assertEqual(self.lines(b"a" * 25, b"b" * 25 + b"\nok\n", b"c" * 30), ["a" * 10, "ok", "c" * 10])

    def test_memory_stays_bounded_without_newlines(self) -> None:
        chunk = b"\rprogress " + b"#" * 65000
        tracemalloc.start()
        try:
            lines = list(mcp._stderr_lines(ChunkStream(itertools.repeat(chunk, 512))))  # 32 MiB
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 1_000_000)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0], ("progress " + "#" * 65000)[:mcp._STDERR_LINE_BYTES])


if __name__ == "__main__":
    unittest.main()
