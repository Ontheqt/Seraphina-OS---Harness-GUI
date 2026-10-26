"""A mock GPU custody register speaking the PROPOSED contract (DESIGN.md section 37, swfactory/custody.py).

It listens on 127.0.0.1 (random port) in a daemon thread. Its clock is ``now`` (epoch seconds; tests move it),
leases last ``ttl`` seconds and expire at the register (expiry never frees VRAM: ``resident`` changes only when a
test says a model loaded, ``load``). One lease per server at a time: a claim on a server another holder's
live lease covers is answered LEASED (409); ``maintenance`` answers MAINTENANCE (503). ``held`` maps a server
to the holder that holds its card outside any lease (a claim there is answered HELD, 423), and ``full`` lists the
(server, model) pairs whose card cannot fit the model (FULL, 409).

Routes: ``GET /residency``, ``POST /leases {provider, model, purpose, holder}``, ``POST /leases/<id>/renew``,
``POST /leases/<id>/release``. ``requests`` records every request as (method, path, headers, body), in order.
``drop`` maps a route kind ("acquire", "renew", "release", "residency") to how many of its next requests are
read and then left unanswered (the connection is closed: outcome unknown). ``stale`` makes the next that many
lease answers carry a generation older than the register's. ``token`` (when set) is the Bearer token required.
``epoch`` (when set) is sent with every answer; ``restart(epoch)`` starts the register over (generation 1, no
leases; models stay resident), as a register that keeps nothing across restarts would.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class MockRegister:
    def __init__(self, ttl: float = 60.0, token: str | None = None) -> None:
        self.ttl = ttl
        self.token = token
        self.now = time.time()
        self.generation = 1
        self.resident: list[dict] = []
        self.leases: dict[str, dict] = {}
        self.requests: list[tuple[str, str, dict, Any]] = []
        self.drop: dict[str, int] = {}
        self.stale = 0
        self.maintenance = False
        self.held: dict[str, str] = {}
        self.full: set[tuple[str, str]] = set()
        self.epoch: str | None = None
        self.lock = threading.RLock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------------------

    def start(self) -> str:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self._server.register = self   # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self.url

    @property
    def url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    def __enter__(self) -> "MockRegister":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # -- what tests do -------------------------------------------------------------------

    def load(self, provider: str, model: str, vram_mib: int = 20000, gpus: list[Any] | None = None) -> None:
        """A model is now resident on provider (whatever was there before is gone): the generation moves on. gpus:
        the cards it sits on as the register lists them, in its order (GPU UUIDs, or an index a register should not
        send); left out of the answer when None."""
        with self.lock:
            self.resident = [item for item in self.resident if item["provider"] != provider]
            item = {"provider": provider, "model": model, "vram_mib": vram_mib, "since": iso(self.now)}
            self.resident.append({**item, "gpus": list(gpus)} if gpus is not None else item)
            self.generation += 1

    def restart(self, epoch: str | None = None) -> None:
        """The register starts over: generation 1 and no leases (what is loaded stays loaded), with epoch."""
        with self.lock:
            self.generation, self.leases, self.epoch = 1, {}, epoch

    def paths(self, method: str | None = None) -> list[str]:
        return [path for verb, path, _, _ in self.requests if method is None or verb == method]

    def ops(self) -> list[str]:
        """The requests as short words: residency, acquire, renew, release."""
        words = []
        for verb, path, _, _ in self.requests:
            words.append("residency" if path == "/residency" else "acquire" if path == "/leases"
                         else path.rsplit("/", 1)[-1])
        return words

    # -- the register ------------------------------------------------------------------------

    def _purge(self) -> None:
        for lease_id in [key for key, lease in self.leases.items() if lease["until"] <= self.now]:
            del self.leases[lease_id]   # expired at the register: VRAM is not freed by this

    def _answer(self, granted: bool, lease: dict | None, code: str) -> dict:
        generation = self.generation
        if self.stale > 0:
            self.stale -= 1
            generation = max(0, self.generation - 5)
        answer = {"granted": granted, "lease_id": lease["id"] if lease else None,
                  "holder": lease["holder"] if lease else None, "until": iso(lease["until"]) if lease else None,
                  "generation": generation, "reason_code": code}
        return {**answer, "epoch": self.epoch} if self.epoch is not None else answer

    def handle(self, method: str, path: str, body: Any) -> tuple[int, dict]:
        with self.lock:
            self._purge()
            if method == "GET" and path == "/residency":
                residency = {"resident": [dict(item) for item in self.resident], "generation": self.generation,
                             "observed_at": iso(self.now)}
                return 200, {**residency, "epoch": self.epoch} if self.epoch is not None else residency
            if method == "POST" and path == "/leases":
                if self.maintenance:
                    return 503, self._answer(False, None, "MAINTENANCE")
                provider, model = body.get("provider"), body.get("model")
                if (provider, model) in self.full:
                    return 409, self._answer(False, None, "FULL")
                if provider in self.held:
                    return 423, {**self._answer(False, None, "HELD"), "holder": self.held[provider]}
                other = next((lease for lease in self.leases.values() if lease["provider"] == provider), None)
                if other is not None:
                    return 409, {**self._answer(False, None, "LEASED"), "holder": other["holder"]}
                lease = {"id": uuid.uuid4().hex, "provider": provider, "model": model, "holder": body.get("holder"),
                         "purpose": body.get("purpose"), "until": self.now + self.ttl}
                self.leases[lease["id"]] = lease
                self.generation += 1
                return 200, self._answer(True, lease, "OK")
            if method == "POST" and path.startswith("/leases/"):
                _, _, lease_id, action = path.split("/", 3)
                lease = self.leases.get(lease_id)
                if action == "renew":
                    if lease is None:
                        return 409, self._answer(False, None, "UNKNOWN")
                    lease["until"] = self.now + self.ttl
                    self.generation += 1
                    return 200, self._answer(True, lease, "OK")
                if action == "release":
                    if lease is None:
                        return 200, self._answer(False, None, "UNKNOWN")
                    del self.leases[lease_id]
                    self.generation += 1
                    return 200, {**self._answer(False, None, "OK"), "lease_id": lease_id}
            return 404, {"error": "not found"}


def _kind(method: str, path: str) -> str:
    if path == "/residency":
        return "residency"
    if path == "/leases":
        return "acquire"
    return path.rsplit("/", 1)[-1]


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, message_format: str, *args: Any) -> None:
        pass

    @property
    def register(self) -> MockRegister:
        return self.server.register   # type: ignore[attr-defined]

    def _serve(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            body = None
        register = self.register
        with register.lock:
            register.requests.append((method, self.path, {k.lower(): v for k, v in self.headers.items()}, body))
            kind = _kind(method, self.path)
            dropped = register.drop.get(kind, 0) > 0
            if dropped:
                register.drop[kind] -= 1
        if register.token is not None and self.headers.get("Authorization") != f"Bearer {register.token}":
            self._send(401, {"error": "unauthorized"})
            return
        if dropped:   # read it, then close without an answer: the requester cannot know the outcome
            self.close_connection = True
            self.connection.close()
            return
        status, payload = register.handle(method, self.path, body if isinstance(body, dict) else {})
        self._send(status, payload)

    def _send(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._serve("GET")

    def do_POST(self) -> None:
        self._serve("POST")
