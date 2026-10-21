"""In-process event bus with a replay ring buffer, secret redaction and JSONL logs.

Publishers never block: every subscriber has a bounded queue and, when a slow
subscriber falls behind, its oldest undelivered events are dropped. Payloads are
converted to plain JSON values before redaction, so no secret can come back
later when an object is turned into text.
"""

from __future__ import annotations

import json
import math
import os
import queue
import threading
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

REDACTED = "***"
MIN_SECRET_LEN = 8
SUBSCRIBER_QUEUE_SIZE = 10000
# Environment variable names that hold credentials (section 10): a name ending in one of the
# suffixes or containing one of the markers. *_KEY covers *_API_KEY, *_SECRET_KEY, *_ACCESS_KEY and
# *_PRIVATE_KEY; the markers catch AWS_SECRET_ACCESS_KEY, PGPASSWORD, GOOGLE_APPLICATION_CREDENTIALS.
SECRET_ENV_SUFFIXES = ("_KEY", "APIKEY", "_KEY_ID", "_TOKEN", "_AUTHTOKEN", "_SECRET", "_PWD", "_AUTH",
                       "_CONNECTION_STRING")
SECRET_ENV_MARKERS = ("SECRET", "PASSWORD", "PASSWD", "PASSPHRASE", "CREDENTIAL")

_log_locks: dict[str, threading.Lock] = {}
_log_locks_guard = threading.Lock()


def utc_now() -> str:
    """Current UTC time as ISO-8601 with seconds precision and a 'Z' suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def truncate(text: str, limit: int) -> str:
    """Keep the first 60% and last 40% of `limit` chars, with a marker in between."""
    if len(text) <= limit:
        return text
    limit = max(limit, 0)
    head = limit * 3 // 5
    tail = limit - head
    dropped = len(text) - limit
    return f"{text[:head]}\n… [{dropped} chars truncated] …\n{text[len(text) - tail:]}"


def _usable_secrets(secrets: Iterable[str]) -> list[str]:
    """Distinct secrets long enough to redact safely, longest first."""
    unique = {s for s in secrets if isinstance(s, str) and len(s) >= MIN_SECRET_LEN}
    return sorted(unique, key=len, reverse=True)


def _redact(obj: Any, secrets: list[str]) -> Any:
    if isinstance(obj, str):
        for secret in secrets:
            if secret in obj:
                obj = obj.replace(secret, REDACTED)
        return obj
    if isinstance(obj, dict):
        return {_redact(key, secrets): _redact(value, secrets) for key, value in obj.items()}
    if isinstance(obj, list):
        return [_redact(item, secrets) for item in obj]
    if isinstance(obj, tuple):
        return tuple(_redact(item, secrets) for item in obj)
    return obj


def redact(obj: Any, secrets: Iterable[str]) -> Any:
    """Return a copy of obj with every secret (len >= 8) in any string replaced by '***'.

    Dict keys are redacted too. Dicts, lists and tuples are rebuilt recursively
    as plain dict/list/tuple, so the result never shares containers with the
    input; other values are returned unchanged.
    """
    return _redact(obj, _usable_secrets(secrets))


def _json_native(obj: Any) -> Any:
    """obj as plain JSON values, so that json.dumps needs no default= and redaction sees all text.

    str, int, float, bool and None are kept (NaN and infinities become text);
    mappings become dicts with str keys; lists and tuples become lists; bytes
    are decoded as UTF-8; anything else becomes str(obj).
    """
    if obj is None or isinstance(obj, (str, int)):  # bool is an int
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else str(obj)
    if isinstance(obj, Mapping):
        return {key if isinstance(key, str) else str(key): _json_native(value)
                for key, value in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_native(item) for item in obj]
    if isinstance(obj, (bytes, bytearray)):
        return bytes(obj).decode("utf-8", errors="replace")
    return str(obj)


def is_credential_name(name: str) -> bool:
    """True if an environment variable name says it holds a credential (any case): *_KEY,
    *APIKEY, *_KEY_ID, *_TOKEN, *_AUTHTOKEN, *_SECRET, *_PWD, *_AUTH, *_CONNECTION_STRING, or a name
    containing SECRET, PASSWORD, PASSWD, PASSPHRASE or CREDENTIAL."""
    upper = name.upper()
    return upper.endswith(SECRET_ENV_SUFFIXES) or any(marker in upper for marker in SECRET_ENV_MARKERS)


def env_secret_values(environ: Mapping[str, str] | None = None) -> list[str]:
    """Values of the environment variables whose names say they hold credentials (is_credential_name)."""
    env = os.environ if environ is None else environ
    return [value for name, value in env.items() if is_credential_name(name)]


def _log_lock(path: Path) -> threading.Lock:
    key = os.path.abspath(path)
    with _log_locks_guard:
        return _log_locks.setdefault(key, threading.Lock())


@dataclass
class Event:
    seq: int
    ts: str
    type: str
    repo: str | None
    job: str | None
    agent: str | None
    data: dict

    def to_dict(self) -> dict:
        """The event as a JSON-serialisable object."""
        return {
            "seq": self.seq,
            "ts": self.ts,
            "type": self.type,
            "repo": self.repo,
            "job": self.job,
            "agent": self.agent,
            "data": self.data,
        }


class EventBus:
    """Thread-safe publish/subscribe with a ring buffer of recent events for replay."""

    def __init__(
        self,
        ring_size: int = 5000,
        secrets_provider: Callable[[], list[str]] | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._seq = 0
        self._ring: deque[Event] = deque(maxlen=ring_size)
        self._subscribers: list[queue.Queue] = []
        self._secrets_provider = secrets_provider

    def _secrets(self) -> list[str]:
        provided = self._secrets_provider() if self._secrets_provider else []
        return [*provided, *env_secret_values()]

    def publish(
        self,
        type: str,
        data: dict | None = None,
        repo: str | None = None,
        job: str | None = None,
        agent: str | None = None,
        log_path: str | os.PathLike | None = None,
    ) -> Event:
        """Redact and record an event, deliver it to subscribers, optionally append it to a JSONL log.

        data is snapshotted as plain JSON values (see _json_native) and then redacted.
        """
        payload = redact(_json_native(dict(data)) if data else {}, self._secrets())
        if log_path is None:
            return self._record(type, payload, repo, job, agent)
        path = Path(log_path)
        # Holding the path lock across seq assignment keeps each log file in seq order.
        with _log_lock(path):
            event = self._record(type, payload, repo, job, agent)
            path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(event.to_dict(), ensure_ascii=False)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        return event

    def _record(self, type: str, payload: dict, repo: str | None, job: str | None,
                agent: str | None) -> Event:
        with self._lock:
            self._seq += 1
            event = Event(self._seq, utc_now(), type, repo, job, agent, payload)
            self._ring.append(event)
            for subscriber in self._subscribers:
                _offer(subscriber, event)
        return event

    def subscribe(self) -> "queue.Queue[Event]":
        """A queue that receives every event published from now on."""
        subscriber: queue.Queue = queue.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        with self._lock:
            self._subscribers.append(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: "queue.Queue[Event]") -> None:
        """Stop delivering to a queue returned by subscribe(); unknown queues are ignored."""
        with self._lock:
            if subscriber in self._subscribers:
                self._subscribers.remove(subscriber)

    def since(self, seq: int) -> list[Event]:
        """Buffered events with a sequence number greater than seq, oldest first."""
        with self._lock:
            return [event for event in self._ring if event.seq > seq]


def _offer(subscriber: queue.Queue, event: Event) -> None:
    """Enqueue without blocking, discarding the oldest queued events when full."""
    while True:
        try:
            subscriber.put_nowait(event)
            return
        except queue.Full:
            try:
                subscriber.get_nowait()
            except queue.Empty:
                pass
