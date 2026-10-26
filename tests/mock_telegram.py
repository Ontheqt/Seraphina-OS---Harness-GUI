"""A mock Telegram Bot API for tests (Bot API 10.3 as docs/seraphina/WAKE_AND_MESSAGES.md cites it).

It listens on 127.0.0.1 (random port); a channel reaches it with ``api_base=mock.api_base``. Every call is
``POST /bot<token>/<method>`` with a JSON body and answers ``{"ok": true, "result": ...}`` or ``{"ok": false,
"error_code": ..., "description": ..., "parameters": {"retry_after": ...}}``. A wrong token is a 401.

- ``getUpdates {offset, timeout}``: updates with ``update_id >= offset`` (older ones are confirmed and
  dropped); with none pending it waits up to ``min(timeout, max_wait)`` seconds. ``conflict = True`` answers
  409 (another poller).
- ``sendMessage`` records the body in ``sent`` and gives each message an id; ``editMessageText``,
  ``editMessageReplyMarkup``, ``answerCallbackQuery``, ``deleteMessage``, ``setMyCommands``,
  ``setMyDescription``, ``setMyShortDescription``, ``getMe`` and ``getWebhookInfo`` are recorded in ``calls``.
- ``fail[method] = (code, description, retry_after)`` makes the next call of that method fail once.

``add_message`` and ``add_callback`` queue updates the way the owner's (or a stranger's) Telegram would.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

TOKEN = "7123456789:AAH-test-token-0123456789abcdefghijk"
OWNER_ID = 424242
STRANGER_ID = 999001


class MockTelegram:
    def __init__(self, token: str = TOKEN, max_wait: float = 0.3) -> None:
        self.token = token
        self.max_wait = max_wait
        self.bot = {"id": 7123456789, "is_bot": True, "first_name": "Helper", "username": "quiet_helper_bot"}
        self.webhook_url = ""
        self.conflict = False
        self.fail: dict[str, tuple[int, str, float | None]] = {}
        self.updates: list[dict] = []
        self.next_update = 1000
        self.next_message = 5000
        self.calls: list[tuple[str, dict]] = []
        self.sent: list[dict] = []
        self.offsets: list[int] = []
        self.lock = threading.Condition()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> "MockTelegram":
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        server.daemon_threads = True
        server.mock = self   # type: ignore[attr-defined]
        self._server = server
        self.api_base = f"http://127.0.0.1:{server.server_address[1]}"
        self._thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(5)

    def __enter__(self) -> "MockTelegram":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # the owner's (or a stranger's) Telegram ------------------------------------------------

    def _queue(self, update: dict) -> dict:
        with self.lock:
            update["update_id"] = self.next_update
            self.next_update += 1
            self.updates.append(update)
            self.lock.notify_all()
        return update

    def add_message(self, from_id: int, text: str | None, *, chat_id: int | None = None, chat_type: str = "private",
                    date: float | None = None, reply_to: int | None = None, forward: bool = False,
                    username: str = "owner_name", first_name: str = "Owner", photo: bool = False) -> dict:
        with self.lock:
            self.next_message += 1
            message_id = self.next_message
        message: dict[str, Any] = {"message_id": message_id, "date": int(date if date is not None else time.time()),
                                   "from": {"id": from_id, "is_bot": False, "first_name": first_name,
                                            "username": username},
                                   "chat": {"id": from_id if chat_id is None else chat_id, "type": chat_type}}
        if text is not None:
            message["text"] = text
        if photo:
            message["photo"] = [{"file_id": "x", "width": 1, "height": 1}]
        if reply_to is not None:
            message["reply_to_message"] = {"message_id": reply_to}
        if forward:
            message["forward_origin"] = {"type": "user", "date": int(time.time())}
        return self._queue({"message": message})

    def add_callback(self, from_id: int, data: str, message_id: int, *, chat_id: int | None = None) -> dict:
        return self._queue({"callback_query": {
            "id": f"cb-{self.next_update}", "from": {"id": from_id, "is_bot": False, "first_name": "Owner"},
            "chat_instance": "ci", "data": data,
            "message": {"message_id": message_id, "date": int(time.time()),
                        "chat": {"id": from_id if chat_id is None else chat_id, "type": "private"}}}})

    def called(self, method: str) -> list[dict]:
        with self.lock:
            return [params for name, params in self.calls if name == method]

    def wait_for(self, predicate: Any, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return predicate()

    # the Bot API ---------------------------------------------------------------------

    def answer(self, method: str, params: dict) -> tuple[int, dict]:
        with self.lock:
            self.calls.append((method, params))
        if method in self.fail:
            code, description, retry = self.fail.pop(method)
            body: dict[str, Any] = {"ok": False, "error_code": code, "description": description}
            if retry is not None:
                body["parameters"] = {"retry_after": retry}
            return code, body
        if method == "getMe":
            return 200, {"ok": True, "result": self.bot}
        if method == "getWebhookInfo":
            return 200, {"ok": True, "result": {"url": self.webhook_url, "pending_update_count": 0}}
        if method == "getUpdates":
            if self.conflict:
                return 409, {"ok": False, "error_code": 409,
                             "description": "Conflict: terminated by other getUpdates request"}
            offset = int(params.get("offset") or 0)
            wait = min(float(params.get("timeout") or 0), self.max_wait)
            deadline = time.monotonic() + wait
            with self.lock:
                self.offsets.append(offset)
                self.updates = [u for u in self.updates if u["update_id"] >= offset]
                while not self.updates and time.monotonic() < deadline:
                    self.lock.wait(max(0.0, deadline - time.monotonic()))
                pending = [u for u in self.updates if u["update_id"] >= offset][:int(params.get("limit") or 100)]
            return 200, {"ok": True, "result": pending}
        if method == "sendMessage":
            with self.lock:
                self.next_message += 1
                record = {**params, "message_id": self.next_message}
                self.sent.append(record)
            return 200, {"ok": True, "result": {"message_id": record["message_id"], "date": int(time.time()),
                                                 "chat": {"id": params.get("chat_id"), "type": "private"},
                                                 "text": params.get("text")}}
        return 200, {"ok": True, "result": True}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_POST(self) -> None:
        mock: MockTelegram = self.server.mock  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        prefix, _, method = self.path.lstrip("/").partition("/")
        if prefix != f"bot{mock.token}":
            status, body = 401, {"ok": False, "error_code": 401, "description": "Unauthorized"}
        else:
            try:
                params = json.loads(raw.decode() or "{}")
            except ValueError:
                params = {}
            status, body = mock.answer(method, params if isinstance(params, dict) else {})
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
