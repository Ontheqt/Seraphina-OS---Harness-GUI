"""Tests for error capture and the capture bundle (DESIGN.md section 39, swfactory/capture.py): every failed
model call attempt and failed tool call in the run log with its status, full (redacted, capped) body, request ids
and exact times; an Open WebUI-backed persona's turn window; and `factory capture`, which is read-only, keeps only
diagnostic events, redacts everything and writes nothing anywhere but --out."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import socket
import stat
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest import mock

from swfactory import capture
from swfactory.__main__ import main
from swfactory.capture import CaptureError, bundle, error_record, parse_when
from swfactory.harness.providers import make_provider
from tests.mock_llm import MockLLM
from tests.test_loop import LoopCase, model_cfg, provider_cfg

EXACT = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
SECRET = "sk-capture-secret-0123456789"


class ErrorRecordTest(unittest.TestCase):
    def test_a_failed_attempt_keeps_status_body_request_ids_and_exact_times(self) -> None:
        body = {"error": {"message": "overloaded " + "x" * 20000 + f" key={SECRET}", "request_id": "body-req-9"}}
        errors = [{"status": 503, "body": body, "headers": {"x-request-id": "req-abc", "cf-ray": "ray-1",
                                                             "server": "nginx"}}]
        records: list[dict] = []
        with mock.patch.dict(os.environ, {"CAPTURE_TEST_API_KEY": SECRET}), \
                MockLLM([{"text": "fine"}], error_sequence=errors) as llm:
            provider = make_provider(provider_cfg(llm.base_url, api_key_env="CAPTURE_TEST_API_KEY"))
            provider._sleep = lambda seconds: None
            answer = provider.complete(model_cfg(provider.cfg, stream=False), "", [{"role": "user", "content": "x"}],
                                       [], on_error=records.append)
            url = llm.base_url + "/chat/completions"
        self.assertEqual(answer.text, "fine")
        [record] = records
        self.assertEqual((record["kind"], record["status"], record["attempt"], record["will_retry"]),
                         ("provider", 503, 1, True))
        self.assertEqual(record["request_ids"], {"x-request-id": "req-abc", "cf-ray": "ray-1",
                                                 "body.request_id": "body-req-9"})
        self.assertTrue(record["body_truncated"])
        self.assertEqual(len(record["body"]), capture.CAPTURE_BODY_CHARS)
        self.assertNotIn(SECRET, json.dumps(record))
        self.assertTrue(EXACT.match(record["started_at"]) and EXACT.match(record["failed_at"]), record)
        self.assertGreaterEqual(record["elapsed_ms"], 0)
        self.assertEqual(record["url"], url)
        self.assertEqual(record["model_id"], "mock-model")

    def test_a_connection_failure_and_a_redacted_message(self) -> None:
        record = error_record(provider="p", model_id="m", url="http://x/v1/chat?key=abc", attempt=2, will_retry=False,
                              status=None, message="connection error: Authorization: Bearer abcdefghijklmnop",
                              connection=True, started=100.0, failed=100.25)
        self.assertEqual((record["url"], record["connection"], record["elapsed_ms"]),
                         ("http://x/v1/chat", True, 250))
        self.assertNotIn("abcdefghijklmnop", record["error"])
        self.assertEqual(record["started_at"], "1970-01-01T00:01:40.000Z")


class RawServer:
    """Answers every request with the same raw HTTP response (headers and body as given), then closes."""

    def __init__(self, response: bytes) -> None:
        self.response = response
        self.count = 0
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.url = f"http://127.0.0.1:{self.sock.getsockname()[1]}"
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                head, _, rest = data.partition(b"\r\n\r\n")
                length = next((int(item.split(b":")[1]) for item in head.split(b"\r\n")
                               if item.lower().startswith(b"content-length")), 0)
                while len(rest) < length:
                    rest += conn.recv(65536)
                self.count += 1
                conn.sendall(self.response)

    def close(self) -> None:
        self.sock.close()


def raw_response(content_type: str, body: bytes, request_id: str) -> bytes:
    return (f"HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\nx-request-id: {request_id}\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode() + body


class AnswerErrorRecordTest(unittest.TestCase):
    """An error read from a 200 answer keeps that answer's request ids and HTTP status, and its whole body."""

    def records_for(self, response: bytes, stream: bool) -> list[dict]:
        server = RawServer(response)
        self.addCleanup(server.close)
        provider = make_provider(provider_cfg(server.url))
        provider._sleep = lambda seconds: None
        records: list[dict] = []
        with self.assertRaises(Exception):
            provider.complete(model_cfg(provider.cfg, stream=stream), "", [{"role": "user", "content": "x"}], [],
                              on_error=records.append)
        return records

    def test_an_error_payload_mid_stream_keeps_the_request_id_and_the_http_status(self) -> None:
        body = (b'data: {"choices":[{"index":0,"delta":{"content":"par"}}]}\n\n'
                b'data: {"error":{"code":500,"message":"CUDA out of memory","type":"server_error"}}\n\n')
        records = self.records_for(raw_response("text/event-stream", body, "req-STREAM-42"), stream=True)
        first = records[0]
        self.assertEqual((first["status"], first["http_status"]), (500, 200))
        self.assertEqual(first["request_ids"], {"x-request-id": "req-STREAM-42"})
        self.assertIn("CUDA out of memory", first["body"])

    def test_a_page_that_is_not_json_is_kept_whole_with_its_request_id(self) -> None:
        page = b"<html><body>upstream model server failed while loading; " + b"t" * 3000 + b" END-OF-PAGE</body></html>"
        [record] = self.records_for(raw_response("text/html", page, "req-HTML-7"), stream=False)
        self.assertIn("END-OF-PAGE", record["body"])
        self.assertEqual(len(record["body"]), len(page))
        self.assertEqual((record["request_ids"], record["http_status"], record["status"]),
                         ({"x-request-id": "req-HTML-7"}, 200, None))
        self.assertLessEqual(len(record["error"]), 2000, "the message itself stays short")


class LoopErrorEventsTest(LoopCase):
    def test_a_failed_model_attempt_and_a_failed_tool_call_are_in_the_run_log(self) -> None:
        server = self.start_mock([{"tool_calls": [{"name": "Read", "arguments": {"file_path": "missing.py"}}]},
                                  {"text": "done"}],
                                 error_sequence=[{"status": 500, "headers": {"x-request-id": "req-1"}}])
        session, events = self.session(server, stream=False)
        session.provider._sleep = lambda seconds: None
        run = session.send("go")
        self.assertTrue(run.ok, run.error)
        provider_error, tool_error = events.of("agent.error")
        self.assertEqual((provider_error["kind"], provider_error["status"], provider_error["request_ids"]),
                         ("provider", 500, {"x-request-id": "req-1"}))
        self.assertEqual((tool_error["kind"], tool_error["name"]), ("tool", "Read"))
        self.assertIn("missing.py", tool_error["output"])
        self.assertTrue(EXACT.match(tool_error["failed_at"]))


class PersonaWindowTest(unittest.TestCase):
    def test_an_open_webui_persona_turn_records_its_model_and_time_window(self) -> None:
        from swfactory.chats import ChatManager
        from swfactory.events import EventBus
        from tests.test_personas import PersonaTestCase, _Factory, wait_for

        case = PersonaTestCase("setUp")
        case.setUp()
        try:
            config = case.make_config()
            manager = ChatManager(config, EventBus(), _Factory())
            try:
                chat = manager.new_chat(None, persona="kate")
                manager.chat_send(chat["id"], "hello")
                done = wait_for(lambda: (lambda c: c if c["status"] != "running" else None)(manager.chat(chat["id"])))
            finally:
                manager.stop()
            window = done["persona"]["turns"][-1]["window"]
            self.assertEqual(window["model"], "kate")
            self.assertTrue(EXACT.match(window["started_at"]) and EXACT.match(window["ended_at"]))
            self.assertLessEqual(window["started_at"], window["ended_at"])
            log = (case.tmp / "data" / "chats" / chat["id"] / "logs" / "01-turn.jsonl").read_text()
            events = [json.loads(line) for line in log.splitlines()]
            [event] = [e for e in events if e["type"] == "persona.window"]
            self.assertEqual((event["data"]["model"], event["data"]["ok"]), ("kate", True))
        finally:
            case.doCleanups()

    def test_the_window_follows_the_persona_not_the_provider_entry_kind(self) -> None:
        """An Open WebUI-backed persona on a provider entry without kind: "openwebui" (binding_provider's default
        provider name) still records its window."""
        from swfactory.chats import ChatManager
        from swfactory.events import EventBus
        from tests.test_personas import PersonaTestCase, _Factory, wait_for

        case = PersonaTestCase("setUp")
        case.setUp()
        try:
            config = case.make_config()
            config.raw["providers"]["openwebui"].pop("kind", None)
            manager = ChatManager(config, EventBus(), _Factory())
            try:
                chat = manager.new_chat(None, persona="kate")
                manager.chat_send(chat["id"], "hello")
                done = wait_for(lambda: (lambda c: c if c["status"] != "running" else None)(manager.chat(chat["id"])))
            finally:
                manager.stop()
            self.assertEqual(done["persona"]["turns"][-1]["window"]["model"], "kate")
        finally:
            case.doCleanups()


# ---------------------------------------------------------------- the bundle


def line(event: str, ts: str, **data: Any) -> str:
    return json.dumps({"seq": 1, "ts": ts, "type": event, "repo": "demo", "job": "001-x", "agent": "builder",
                       "data": data})


class FakeDocker(capture.DockerLogs):
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def run(self, name: str, since: str, until: str) -> tuple[int | None, str, str]:
        self.calls.append((name, since, until))
        if name == "denied":
            return 1, "permission denied while trying to connect to the Docker daemon socket", ""
        if name == "absent":
            return None, "", "docker is not installed (or not on PATH)"
        return 0, (f"2026-10-22T09:00:01.000Z request 7 failed Authorization: Bearer tok-{'z' * 12}\n"
                   f"2026-10-22T09:00:02.000Z api_key={SECRET}\n"), ""


class BundleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sf-capture-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = self.tmp / "data"
        logs = self.data / "jobs" / "demo" / "001-x" / "logs"
        logs.mkdir(parents=True)
        (logs / "03-builder.jsonl").write_text("\n".join([
            line("agent.start", "2026-10-22T08:59:00Z", provider="tabby"),          # before the window
            line("agent.start", "2026-10-22T09:00:00Z", provider="tabby"),
            line("agent.text", "2026-10-22T09:00:01Z", text="the owner's words"),
            line("agent.tool_call", "2026-10-22T09:00:01Z", input={"command": "cat notes"}),
            line("agent.error", "2026-10-22T09:00:02Z", kind="provider", status=500, body=f"key {SECRET}",
                 failed_at="2026-10-22T09:00:02.123Z", request_ids={"x-request-id": "req-1"}),
            line("agent.end", "2026-10-22T09:00:03Z", ok=False, error="model call failed"),
            "not json",
        ]) + "\n")
        (logs / "events.jsonl").write_text(line("job.updated", "2026-10-22T09:00:00Z", job={"feature": "secret plan"})
                                           + "\n")
        chat_logs = self.data / "chats" / "c-0123456789" / "logs"
        chat_logs.mkdir(parents=True)
        (chat_logs / "01-turn.jsonl").write_text(line("persona.window", "2026-10-22T09:00:05Z", model="m",
                                                      started_at="2026-10-22T09:00:04.100Z") + "\n")
        for private in ("wakes", "notify"):
            (self.data / private / "logs").mkdir(parents=True)
            (self.data / private / "logs" / "x.jsonl").write_text(line("log", "2026-10-22T09:00:00Z", message="p"))
        self.since = datetime(2026, 10, 22, 9, 0, 0, tzinfo=timezone.utc)
        self.until = datetime(2026, 10, 22, 9, 30, 0, tzinfo=timezone.utc)

    def tree(self) -> dict[str, bytes]:
        return {str(path.relative_to(self.tmp)): path.read_bytes() for path in sorted(self.tmp.rglob("*"))
                if path.is_file() and "out" not in path.relative_to(self.tmp).parts}

    def test_only_diagnostic_events_in_the_window_redacted_and_nothing_written_outside_out(self) -> None:
        before = self.tree()
        docker = FakeDocker()
        out = self.tmp / "out"
        manifest = bundle(self.data, out, self.since, self.until, ["open-webui", "denied", "absent"],
                          secrets=[SECRET], docker=docker)
        self.assertEqual(self.tree(), before, "read-only: nothing outside --out changed")
        files = sorted(str(path.relative_to(out)) for path in out.rglob("*") if path.is_file())
        self.assertEqual(files, ["containers/open-webui.log", "harness/chats/c-0123456789/logs/01-turn.jsonl",
                                 "harness/jobs/demo/001-x/logs/03-builder.jsonl", "manifest.json"])
        kept = [json.loads(text) for text in (out / "harness/jobs/demo/001-x/logs/03-builder.jsonl").read_text()
                .splitlines()]
        self.assertEqual([event["type"] for event in kept], ["agent.start", "agent.error", "agent.end"])
        self.assertEqual(kept[1]["data"]["request_ids"], {"x-request-id": "req-1"})
        text = "".join(path.read_text() for path in out.rglob("*") if path.is_file())
        for never in (SECRET, "the owner's words", "cat notes", "secret plan", f"tok-{'z' * 12}"):
            self.assertNotIn(never, text)
        self.assertEqual(docker.calls[0], ("open-webui", "2026-10-22T09:00:00.000Z", "2026-10-22T09:30:00.000Z"))
        rows = {row["name"]: row for row in manifest["containers"]}
        self.assertEqual((rows["open-webui"]["taken"], rows["open-webui"]["lines"]), (True, 2))
        self.assertIn("may not run docker logs", rows["denied"]["why"])
        self.assertIn("not installed", rows["absent"]["why"])
        self.assertGreater(sum(manifest["redaction"].values()), 0)
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o700)
        for path in out.rglob("*"):
            if path.is_file():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, path)

    def test_a_failed_tool_calls_output_never_leaves_in_the_bundle(self) -> None:
        output = "PRIVATE: a personal note the command printed\n[exit code 3]"
        logs = self.data / "chats" / "c-0123456789" / "logs"
        (logs / "02-turn.jsonl").write_text(line("agent.error", "2026-10-22T09:00:06Z", kind="tool", name="Bash",
                                                 id="call-1", output=output, output_truncated=False,
                                                 failed_at="2026-10-22T09:00:06.500Z") + "\n")
        out = self.tmp / "out"
        bundle(self.data, out, self.since, self.until)
        text = "".join(path.read_text() for path in out.rglob("*") if path.is_file())
        self.assertNotIn("PRIVATE", text)
        [event] = [json.loads(item) for item in (out / "harness/chats/c-0123456789/logs/02-turn.jsonl").read_text()
                   .splitlines()]
        data = event["data"]
        self.assertEqual((data["name"], data["id"], data["failed_at"]), ("Bash", "call-1", "2026-10-22T09:00:06.500Z"))
        self.assertNotIn("output", data)
        self.assertEqual((data["output_chars"], data["output_sha256"]),
                         (len(output), hashlib.sha256(output.encode()).hexdigest()))

    def test_missing_parents_of_out_are_created_private_too(self) -> None:
        existing = self.tmp / "existing"
        existing.mkdir()
        os.chmod(existing, 0o755)
        old = os.umask(0o022)
        try:
            bundle(self.data, existing / "captures" / "a" / "b" / "out", self.since, self.until)
        finally:
            os.umask(old)
        for folder in ("captures", "captures/a", "captures/a/b", "captures/a/b/out"):
            self.assertEqual(stat.S_IMODE((existing / folder).stat().st_mode), 0o700, folder)
        self.assertEqual(stat.S_IMODE(existing.stat().st_mode), 0o755, "an existing parent is left as it was")

    def test_refusals(self) -> None:
        busy = self.tmp / "busy"
        busy.mkdir()
        (busy / "earlier.txt").write_text("an earlier capture")
        with self.assertRaisesRegex(CaptureError, "not empty"):
            bundle(self.data, busy, self.since, self.until)
        with self.assertRaisesRegex(CaptureError, "not a container name"):
            bundle(self.data, self.tmp / "o1", self.since, self.until, ["bad name; rm"])
        with self.assertRaisesRegex(CaptureError, "before --since"):
            bundle(self.data, self.tmp / "o2", self.until, self.since)
        self.assertFalse((self.tmp / "o1").exists() or (self.tmp / "o2").exists())

    def test_times(self) -> None:
        now = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(parse_when("now", now), now)
        self.assertEqual(parse_when("90m", now), now - timedelta(minutes=90))
        self.assertEqual(parse_when("2026-10-22T09:00:00Z", now), datetime(2026, 10, 22, 9, tzinfo=timezone.utc))
        self.assertEqual(parse_when("2026-10-22T11:00:00+02:00", now), datetime(2026, 10, 22, 9, tzinfo=timezone.utc))
        with self.assertRaisesRegex(CaptureError, "not a time"):
            parse_when("yesterday-ish", now)

    def test_the_cli_works_without_the_server(self) -> None:
        config = self.tmp / "config.json"
        config.write_text(json.dumps({"data_dir": str(self.data)}))
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"FACTORY_CONFIG": str(config)}), redirect_stdout(out), \
                redirect_stderr(io.StringIO()):
            code = main(["capture", "--since", "2026-10-22T09:00:00Z", "--until", "2026-10-22T09:30:00Z",
                         "--out", str(self.tmp / "cli-out"), "--json"])
        self.assertEqual(code, 0)
        manifest = json.loads(out.getvalue())
        self.assertEqual(len(manifest["harness"]), 2)
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"FACTORY_CONFIG": str(config)}), redirect_stdout(io.StringIO()), \
                redirect_stderr(err):
            code = main(["capture", "--since", "1h", "--out", str(self.tmp / "cli-out")])
        self.assertEqual(code, 1)
        self.assertIn("not empty", err.getvalue())


if __name__ == "__main__":
    unittest.main()
