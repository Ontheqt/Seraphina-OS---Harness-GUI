"""A temporary machine for the daemon route's tests: bin/sf-snapraidd (the link and the forwarder) loaded by path,
its fixed locations moved into a temporary tree, root ownership faked, the daemon's login as a systemd credential
in a temporary $CREDENTIALS_DIRECTORY, and tests/mock_snapraidd.py as the SnapRAID Daemon.

LinkRig.connector() is what the harness's LinkClient uses in the tests instead of the Unix socket: each call runs
one request through the link's main(), as systemd would start one instance per connection."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
import os
import sys
import tempfile
import types
from pathlib import Path

from tests.mock_snapraidd import MockDaemon

ROOT = Path(__file__).resolve().parent.parent
LINK_FILE = ROOT / "bin" / "sf-snapraidd"
CANARY = "seraphina:canary-password-0001"        # searched for everywhere afterwards (T28)
CALLER_UID = 4242


def load_link():
    loader = importlib.machinery.SourceFileLoader("sf_snapraidd", str(LINK_FILE))
    spec = importlib.util.spec_from_loader("sf_snapraidd", loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    loader.exec_module(module)
    return module


sd = load_link()


class FakeLstat:
    """os.lstat with every path owned by root and writable by nobody else (secret files 0600), except the ones a
    test spoils."""

    def __init__(self) -> None:
        self.spoiled: dict[str, tuple[int, int]] = {}

    def __call__(self, path: str):
        real = os.lstat(path)
        uid, extra = self.spoiled.get(path, (0, 0))
        return types.SimpleNamespace(st_uid=uid, st_mode=(real.st_mode & ~0o077) | extra)


class LinkRig:
    """One machine: the link's settings, its credential, the mock daemon, and a hook header for the forwarder."""

    def __init__(self, allow: tuple[str, ...] = ("read", "act", "accept"), *, auth: bool = True,
                 daemon: MockDaemon | None = None, hook_port: int = 8765, clock=None) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sdtest-")
        self.root = Path(self.tmp.name)
        self.paths = sd.Paths.under(self.root)
        self.lstat = FakeLstat()
        self.daemon = (daemon or MockDaemon(CANARY, auth=auth, rate_seconds=0.0)).start()
        for folder in ("usr/local/libexec/seraphina", "etc/seraphina", "creds", "state"):
            (self.root / folder).mkdir(parents=True, exist_ok=True)
        (self.root / "usr/local/libexec/seraphina/sf-snapraidd").write_bytes(LINK_FILE.read_bytes())
        (self.root / "creds/basic").write_text(CANARY + "\n", encoding="utf-8")
        self.cfg = {"v": 1, "caller": "seraphina", "allow": list(allow), "daemon": self.daemon.url,
                    "hook": {"url": f"http://127.0.0.1:{hook_port}/api/hooks/event",
                             "header_file": "/etc/seraphina/snapraidd-hook.header"}}
        self.write_config(self.cfg)
        self.env = {"CREDENTIALS_DIRECTORY": str(self.root / "creds"), "STATE_DIRECTORY": str(self.root / "state")}
        self.slept: list[float] = []
        self.clock = clock
        self.peer = CALLER_UID

    def write_config(self, cfg: dict) -> None:
        self.cfg = cfg
        (self.root / "etc/seraphina/snapraidd.json").write_text(json.dumps(cfg), encoding="utf-8")

    def link_kwargs(self) -> dict:
        out = {"env": self.env, "peer_uid": self.peer, "lstat": self.lstat,
               "uid_of": lambda name: CALLER_UID if name == "seraphina" else None, "sleep": self.slept.append}
        if self.clock is not None:
            out["clock"] = self.clock
        return out

    def ask(self, request: dict | bytes) -> tuple[int, dict]:
        line = request if isinstance(request, bytes) else json.dumps(request).encode("utf-8") + b"\n"
        out = io.StringIO()
        code = sd.main(["serve"], stdin=io.BytesIO(line), stdout=out, paths=self.paths, **self.link_kwargs())
        return code, json.loads(out.getvalue())

    def connector(self):
        """The harness's LinkClient connector: one request line in, one answer line out, through the link."""
        def call(line: bytes, timeout: float) -> bytes:
            out = io.StringIO()
            sd.main(["serve"], stdin=io.BytesIO(line), stdout=out, paths=self.paths, **self.link_kwargs())
            return out.getvalue().encode("utf-8")
        return call

    def expect(self, action: str, body: dict | None = None) -> dict:
        """The full binding of an act as the harness would send it now (every key REQUIRED_BINDING names), read
        through the link's own facts; a test overrides single keys to spoil one."""
        link = sd.Link(self.paths, **self.link_kwargs())
        link.cfg = link.setup()
        link.client = sd.DaemonClient(self.cfg["daemon"], lambda: sd.basic_header(link.cfg, self.paths, self.env,
                                                                                    self.lstat))
        try:
            facts = link.facts(action, body or {})
        except sd.Refused:
            return {}
        active = facts["active"][0]["number"] if facts["active"] else None
        kind = "accept" if action == "maintenance" and (body or {}).get("ignore_thresholds") else action
        values = {"started_at": round(facts["started_at"]), "active_number": active, "schedule": facts["schedule"],
                  "hold_off": bool(facts["hold_off"]), "thresholds": facts["thresholds"],
                  "prevent_truncations": facts["prevent_truncations"], "nothing_queued": True, "scan": facts["scan"],
                  "blocks_bad": facts["blocks_bad"], "health": facts["health"]}
        return {key: values[key] for key in sd.REQUIRED_BINDING[kind]}

    def journal(self) -> list[dict]:
        path = self.root / "state" / "actions.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def close(self) -> None:
        self.daemon.stop()
        self.tmp.cleanup()
