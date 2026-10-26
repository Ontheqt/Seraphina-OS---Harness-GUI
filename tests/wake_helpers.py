"""Fixtures for the WakeManager tests (Wake & Messages W2, W2b, W3): a fake factory that records what the manager
asks of the Notifier and the tracker, a fake clock, a config with one persona on a mock model server."""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest import mock
from zoneinfo import ZoneInfo

from swfactory.clock import LocalClock, TimeSync
from swfactory.config import Config
from swfactory.events import EventBus

TZ = "Europe/London"
ZONE = ZoneInfo(TZ)
SECRET = "sk-wake-secret-value-0123456789"


def local(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=ZONE)


class FakeTime:
    """A wall clock the test moves (the LocalClock reads it); monotonic time stays real."""

    def __init__(self, text: str = "2026-10-21 12:00") -> None:
        self.t = local(text).timestamp()

    def __call__(self) -> float:
        return self.t

    def set(self, text: str) -> None:
        self.t = local(text).timestamp()

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeFactory:
    """Records notices, items and chat sends; serves jobs, items and channels the test sets."""

    def __init__(self) -> None:
        self.notices: list[Any] = []
        self.items_opened: list[dict] = []
        self.closed: list[tuple] = []
        self.dropped: list[tuple] = []
        self.declines: list[tuple] = []
        self.job_list: list[dict] = []
        self.item_rows: list[dict] = []
        self.channels: list[dict] | None = None
        self.tamper: Any = None
        self.acknowledged = None
        self.sent: list[tuple] = []
        self.chats: dict[str, dict] = {}
        self.cancelled_chats: list[str] = []
        self.baselines: list[str] = []
        self._n = 0
        self.lock = threading.Lock()

    def notify_notice(self, notice: Any) -> dict:
        with self.lock:
            self._n += 1
            self.notices.append(notice)
            return {"id": f"M-{self._n:06d}"}

    def secret_values(self) -> list[str]:
        return [SECRET]

    def jobs(self) -> list[dict]:
        return list(self.job_list)

    def items(self, **kwargs: Any) -> dict:
        return {"items": list(self.item_rows), "counts": {}}

    def notify_channels(self) -> list[dict] | None:
        return self.channels

    def notify_precondition(self) -> dict:
        return {"acknowledged_at": self.acknowledged}

    def notify_tamper(self) -> Any:
        return self.tamper

    def notify_baseline(self, why: str) -> None:
        self.baselines.append(why)

    def open_persona_item(self, persona: str, title: str, **fields: Any) -> dict:
        with self.lock:
            ref = f"system:S-{len(self.items_opened) + 1}"
            self.items_opened.append({"ref": ref, "persona": persona, "title": title, **fields})
            return {"ref": ref, "title": title}

    def close_wake_item(self, ref: str, run: str) -> dict:
        self.closed.append((ref, run))
        return {"ref": ref}

    def drop_wake_item(self, ref: str, reason: str) -> dict:
        self.dropped.append((ref, reason))
        return {"ref": ref}

    def record_decline(self, persona: str, wake: str, run: str, item: Any = None, title: str = "",
                       taint: Any = ()) -> dict:
        self.declines.append((persona, wake, run, item, title, tuple(taint)))
        return {"ref": item or "system:S-99"}

    def chat(self, chat_id: str) -> dict:
        if chat_id not in self.chats:
            raise KeyError(chat_id)
        return dict(self.chats[chat_id])

    def chat_send(self, chat_id: str, text: str, origin: str = "user", wake_id: str | None = None,
                  wake_item: str | None = None) -> dict:
        self.sent.append((chat_id, text, origin, wake_id, wake_item))
        return {"id": chat_id}

    def chat_cancel(self, chat_id: str) -> dict:
        self.cancelled_chats.append(chat_id)
        return {}

    def persona_check(self, name: str | None = None) -> dict:
        return {"checked": []}


class WakeTestCase(unittest.TestCase):
    """A WakeManager on a temporary data dir and HOME, one persona "seraphina" on models.local."""

    # no ambient check-in unless a test asks for one (its active window is a minute at 02:00, inside the power-off)
    persona_cfg: dict = {"ambient": {"every_minutes": 120, "active": "02:00-02:01"}}
    AMBIENT = {"ambient": {"every_minutes": 120, "active": "auto"}}

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sf-wakes-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patch = mock.patch.dict(os.environ, {"HOME": str(self.tmp / "home")})
        patch.start()
        self.addCleanup(patch.stop)
        self.time = FakeTime()
        self.factory = FakeFactory()
        self.bus = EventBus()
        self.events = self.bus.subscribe()

    def make(self, base_url: str, *, persona: dict | None = None, wakes: dict | None = None, raw: dict | None = None,
             attest: bool = True, get: Any = None, post: Any = None, time_sync: Any = None) -> Any:
        from swfactory.wakes import WakeManager

        cfg = {"enabled": True, "model": "local", **self.persona_cfg, **(persona or {})}
        config_raw = {"data_dir": str(self.tmp / "data"), "harness": {"sandbox": "bwrap"},
                      "providers": {"local": {"type": "openai", "base_url": base_url, "api_key_env": None}},
                      "models": {"local": {"provider": "local", "model": "mock-model", "max_tokens": 2048}},
                      "wakes": {"enabled": True, "timezone": TZ, "time_sync": {"require": False},
                                "personas": {"seraphina": cfg}, **(wakes or {})}}
        for key, value in (raw or {}).items():
            config_raw[key] = value
        config = Config(self.tmp / "conf" / "config.json", config_raw)
        config.save()
        self.config = Config.load(config.path)
        manager = WakeManager(self.config, self.bus, self.factory, clock=LocalClock(TZ, now=self.time),
                              time_sync=time_sync or TimeSync(require=False), get=get, post=post)
        self.addCleanup(manager.stop, time.monotonic() + 5)
        manager.start(threads=False)
        if attest:
            manager.attest_server("local", True)
        self.manager = manager
        return manager

    def run_tick(self, manager: Any = None, wait: float = 10.0) -> None:
        """One tick, then wait until no turn is in flight."""
        manager = manager or self.manager
        manager.tick()
        self.wait_idle(manager, wait)

    def wait_idle(self, manager: Any = None, wait: float = 10.0) -> None:
        manager = manager or self.manager
        deadline = time.monotonic() + wait
        while manager._turns and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(manager._turns, "a turn is still running")

    def history(self, manager: Any = None) -> list[dict]:
        import json

        manager = manager or self.manager
        rows = []
        for path in sorted(manager.root.glob("history-*.jsonl")):
            rows += [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        return rows

    def drain_events(self) -> list[Any]:
        out = []
        while True:
            try:
                out.append(self.events.get_nowait())
            except Exception:  # noqa: BLE001 - queue.Empty
                return out
