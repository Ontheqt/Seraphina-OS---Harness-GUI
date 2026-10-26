"""A mock ntfy server for tests (the parts Wake & Messages uses, as ntfy's docs describe them).

It listens on 127.0.0.1 (random port) or on a Unix socket and serves:

- ``POST /`` with a JSON body ``{topic, title, message, priority, actions, click, sequence_id}``: publish,
  answered with the stored message (``id``, ``time``, ``event: "message"``, ...);
- ``POST /<topic>`` with a raw body: publish that body as the message (what a phone's http action does);
- ``GET /<topic>[,<topic>...]/json?since=<id|10m|all>[&poll=1]``: newline-delimited JSON, an ``open`` event,
  the stored messages after ``since``, then (unless ``poll=1``) new ones as they arrive and a ``keepalive``
  every ``keepalive`` seconds, until the client goes away or the server stops;
- ``GET /v1/health``: ``{"healthy": true}``.

Access works like ``auth-default-access: deny-all``: ``tokens`` maps a token to a user, ``acl`` maps a user to
``{topic: "rw" | "ro" | "wo"}``; anything else is 403 (401 without a known token). ``fail_publish`` holds
status codes for the next publishes; ``hang_publish`` reads a publish and never answers (the client times
out after sending: delivery state "unknown").

Example::

    with MockNtfy(unix=True) as ntfy:
        entry = {"type": "ntfy", "publish_url": ntfy.publish_url, ...}
        ...
        ntfy.post("seraphina-taps", '{"v":1,"verb":"ack","cap":"..."}', token=REPLY_TOKEN)
"""

from __future__ import annotations

import json
import os
import secrets
import socketserver
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

HARNESS_TOKEN = "tk_harness0123456789abcdefghijkl"     # tk_ + 29 characters, as ntfy requires
REPLY_TOKEN = "tk_reply0123456789abcdefghijklmn"
PHONE_TOKEN = "tk_phone0123456789abcdefghijklmn"


def default_acl() -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    tokens = {HARNESS_TOKEN: "seraphina", REPLY_TOKEN: "phone-reply", PHONE_TOKEN: "owner-phone"}
    acl = {"seraphina": {"seraphina": "rw", "seraphina-taps": "ro", "seraphina-chat": "ro"},
           "phone-reply": {"seraphina-taps": "wo"},
           "owner-phone": {"seraphina": "ro", "seraphina-chat": "rw"}}
    return tokens, acl


class MockNtfy:
    def __init__(self, *, unix: bool = False, tokens: dict | None = None, acl: dict | None = None,
                 keepalive: float = 0.5) -> None:
        default_tokens, default_access = default_acl()
        self.tokens = default_tokens if tokens is None else tokens
        self.acl = default_access if acl is None else acl
        self.unix = unix
        self.keepalive = keepalive
        self.messages: list[dict] = []          # every stored message, in order
        self.publishes: list[dict] = []         # {body, headers} of each POST /
        self.subscriptions: list[dict] = []     # {topics, since, poll, token}
        self.fail_publish: list[int] = []
        self.hang_publish = False
        self.healthy = True
        self.lock = threading.Condition()
        self.stopping = threading.Event()
        self._server: Any = None
        self._thread: threading.Thread | None = None
        self._dir: str | None = None

    # lifecycle ----------------------------------------------------------------------

    def start(self) -> "MockNtfy":
        if self.unix:
            self._dir = tempfile.mkdtemp(prefix="sf-ntfy-")
            self.socket_path = os.path.join(self._dir, "ntfy.sock")
            server = socketserver.ThreadingUnixStreamServer(self.socket_path, _Handler)
            self.publish_url = f"unix:{self.socket_path}"
        else:
            server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
            self.publish_url = f"http://127.0.0.1:{server.server_address[1]}"
        server.daemon_threads = True
        server.mock = self   # type: ignore[attr-defined]
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self.stopping.set()
        with self.lock:
            self.lock.notify_all()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(5)
        if self._dir:
            for name in os.listdir(self._dir):
                os.unlink(os.path.join(self._dir, name))
            os.rmdir(self._dir)

    def __enter__(self) -> "MockNtfy":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # helpers --------------------------------------------------------------------------

    def allowed(self, token: str | None, topic: str, write: bool) -> int:
        """200, 401 or 403."""
        user = self.tokens.get(token or "")
        if user is None:
            return 401 if token else 403
        perm = self.acl.get(user, {}).get(topic, "")
        ok = ("w" in perm) if write else (perm in ("rw", "ro"))
        return 200 if ok else 403

    def store(self, topic: str, message: str, **fields: Any) -> dict:
        record = {"id": secrets.token_hex(6), "time": int(time.time()), "expires": int(time.time()) + 86400,
                  "event": "message", "topic": topic, "message": message}
        record.update({k: v for k, v in fields.items() if v is not None})
        with self.lock:
            self.messages.append(record)
            self.lock.notify_all()
        return record

    def post(self, topic: str, message: str, token: str | None = None, title: str | None = None) -> tuple[int, dict]:
        """Publish as a client (the phone's tap, or someone forging a notice) through the access rules."""
        status = self.allowed(token, topic, write=True)
        if status != 200:
            return status, {}
        return 200, self.store(topic, message, title=title)

    def on_topic(self, topic: str) -> list[dict]:
        with self.lock:
            return [m for m in self.messages if m["topic"] == topic]

    def wait_for(self, predicate: Any, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        with self.lock:
            while not predicate():
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self.lock.wait(min(left, 0.05))
        return True


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        pass

    @property
    def mock(self) -> MockNtfy:
        return self.server.mock  # type: ignore[attr-defined]

    def _token(self) -> str | None:
        scheme, _, value = (self.headers.get("Authorization") or "").partition(" ")
        return value.strip() if scheme.lower() == "bearer" else None

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        mock = self.mock
        path = urllib.parse.urlsplit(self.path).path
        if path == "/":
            try:
                body = json.loads(raw.decode())
            except ValueError:
                return self._json(400, {"error": "bad json"})
            mock.publishes.append({"body": body, "headers": dict(self.headers.items())})
            if mock.hang_publish:
                time.sleep(3)
                return
            if mock.fail_publish:
                return self._json(mock.fail_publish.pop(0), {"error": "refused"})
            topic = str(body.get("topic") or "")
            status = mock.allowed(self._token(), topic, write=True)
            if status != 200:
                return self._json(status, {"error": "forbidden"})
            record = mock.store(topic, str(body.get("message") or ""), title=body.get("title"),
                                priority=body.get("priority"), actions=body.get("actions"), click=body.get("click"),
                                sequence_id=body.get("sequence_id"))
            return self._json(200, record)
        topic = path.strip("/")
        status, record = mock.post(topic, raw.decode("utf-8", "replace"), self._token())
        if status != 200:
            return self._json(status, {"error": "forbidden"})
        return self._json(200, record)

    def do_GET(self) -> None:
        mock = self.mock
        parts = urllib.parse.urlsplit(self.path)
        if parts.path == "/v1/health":
            return self._json(200 if mock.healthy else 500, {"healthy": mock.healthy})
        if not parts.path.endswith("/json"):
            return self._json(404, {"error": "not found"})
        topics = parts.path[1:-len("/json")].split(",")
        query = urllib.parse.parse_qs(parts.query)
        since = (query.get("since") or ["all"])[0]
        poll = (query.get("poll") or ["0"])[0] == "1"
        token = self._token()
        mock.subscriptions.append({"topics": topics, "since": since, "poll": poll, "token": token})
        for topic in topics:
            status = mock.allowed(token, topic, write=False)
            if status != 200:
                return self._json(status, {"error": "forbidden"})
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        with mock.lock:
            known = list(mock.messages)
        start = 0
        if since == "all":
            start = 0
        elif since.endswith("m") and since[:-1].isdigit():
            cutoff = time.time() - int(since[:-1]) * 60
            start = next((i for i, m in enumerate(known) if m["time"] >= cutoff), len(known))
        else:
            start = next((i + 1 for i, m in enumerate(known) if m["id"] == since), 0)
        sent = start
        try:
            self._line({"id": secrets.token_hex(6), "time": int(time.time()), "event": "open",
                        "topic": ",".join(topics)})
            while True:
                with mock.lock:
                    batch = mock.messages[sent:]
                    sent = len(mock.messages)
                for record in batch:
                    if record["topic"] in topics:
                        self._line(record)
                if poll or mock.stopping.is_set():
                    return
                with mock.lock:
                    if len(mock.messages) == sent and not mock.stopping.is_set():
                        mock.lock.wait(mock.keepalive)
                    fresh = len(mock.messages) != sent
                if not fresh:
                    self._line({"id": secrets.token_hex(6), "time": int(time.time()), "event": "keepalive",
                                "topic": ",".join(topics)})
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def _line(self, record: dict) -> None:
        self.wfile.write(json.dumps(record).encode() + b"\n")
        self.wfile.flush()
