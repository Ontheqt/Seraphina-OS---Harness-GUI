"""The real backend for the GUI's end-to-end test (tests/gui/e2e_real.mjs): `factory serve` with its mocks.

    python3 -m tests.e2e.gui_backend [--scratch DIR] [--keep]

It sets up, from scratch, what the test drives through the browser:

- a temp root with its own HOME, FACTORY_CONFIG, data_dir and a scratch git repository (the e2e todo fixture,
  tests/e2e/harness.py make_fixture), **not** registered: the test adds it through the setup wizard;
- one scripted local model server (tests/e2e/scripted_llm.py, the "local" server: reasoning under the
  ``reasoning`` key), **not** configured: the test adds it through the setup wizard as a llama.cpp server. Its
  spec-writer marks one Assumptions line (product), so a job holds for a person's decision. Requests no factory
  agent sent (Settings' model Test probes) get a plain answer;
- a mock Open WebUI (tests/mock_openwebui.py) with the workspace model ``kate``, the provider ``openwebui``
  (kind openwebui, its key in the env file next to the config) and ``models.kate``, so the shipped kate
  persona (agents/kate.md) chats through it;
- a mock ntfy on a Unix socket (tests/mock_ntfy.py) as the channel ``phone`` (its tokens in the server's
  environment), quiet hours far from now; the precondition (no sandbox) is left for the test to accept;
- one routing profile, ``local-quick``, on the model key ``local`` (which the wizard points at the scripted
  server), each placement with its reason;
- an empty receipts folder for the operator persona (the test marks kate and sets the folder in the GUI).

Things a browser cannot do (what a phone or the operator does) go through a small control server on loopback:

- ``GET /status``: the scripted model's protocol violations and failed tool calls, counts of what each mock saw;
- ``POST /phone/tap {title?, timeout?}``: the phone taps Got it on the newest notice with that title (default
  "Test") the mock ntfy received, through the taps topic with the reply token, as the ntfy app does;
- ``POST /receipt {id?, intent?, not_verified?, verified?}``: the operator writes one change receipt
  (operator-receipt/1) into the receipts folder; answers {path, id};
- ``POST /stop``.

It prints one line ``READY <json>`` ({url, token, control, root, repo, repo_name, llm_url, llm_model, receipts_dir,
openwebui}) once the server answers, then serves until /stop, SIGTERM or stdin closes. It then stops the server
(SIGTERM, then SIGKILL), kills any process left with its marker and removes the root unless --keep (or
FACTORY_E2E_KEEP=1), printing ``KEPT <root>`` when kept. The server's output is in <root>/server.log.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from swfactory.settings import PLAIN_PROMPT, TOOL_PROMPT
from tests.e2e.harness import FACTORY, PYTHON, REPO, clean_environment, free_port, make_fixture
from tests.e2e.scripted_llm import Scenario, ScriptedLLM
from tests.mock_llm import MockLLM
from tests.mock_ntfy import HARNESS_TOKEN, REPLY_TOKEN, MockNtfy
from tests.mock_openwebui import OWNER_KEY, MockOpenWebUI

LLM_MODEL = "mock-local-model-7b"
PRODUCT_LINE = "Clearing completed items cannot be undone, and users are not asked to confirm"
PROFILE = "local-quick"
TAP_JSON = re.compile(r'^\{"v":\s*1,')
TAPS_TOPIC = "seraphina-taps"   # the channel's default taps topic (channels.NtfyChannel.topics)


def _scratch(arg: str | None) -> Path:
    base = Path(arg or os.environ.get("FACTORY_E2E_SCRATCH") or Path(tempfile.gettempdir()) / "sf-e2e")
    base.mkdir(parents=True, exist_ok=True)
    return base


def plain_handler(scripted: ScriptedLLM, server: str):
    """The scripted model for the factory's agents, and a plain answer to a model Test's two probes ("Reply with
    OK", then a get_time tool call), which the scripted model would refuse as an unknown agent. Anything else goes
    to the scripted model, which records it as a protocol violation."""
    scripted_handle = scripted.handler(server)

    def handle(body: dict, path: str) -> dict:
        messages = [m for m in body.get("messages") or [] if isinstance(m, dict)]
        system = any(m.get("role") == "system" for m in messages)
        last = str(messages[-1].get("content") if messages else "")
        tools = [t.get("function", t).get("name") for t in body.get("tools") or [] if isinstance(t, dict)]
        if not system and last == PLAIN_PROMPT:
            return {"reasoning": "Answer.", "reasoning_key": "reasoning", "text": "OK"}
        if not system and last == TOOL_PROMPT and tools == ["get_time"]:
            return {"reasoning": "Call the tool.", "reasoning_key": "reasoning",
                    "tool_calls": [{"name": "get_time", "arguments": {}}]}
        return scripted_handle(body, path)

    return handle


class Backend:
    def __init__(self, scratch: Path, keep: bool) -> None:
        self.keep = keep
        self.root = Path(tempfile.mkdtemp(prefix="gui-e2e-", dir=scratch)).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.config_dir = self.root / "config"
        self.config_dir.mkdir(mode=0o700)
        self.config_path = self.config_dir / "config.json"
        self.data_dir = self.root / "data"
        self.receipts_dir = self.root / "receipts"
        self.receipts_dir.mkdir()
        self.repo = make_fixture(self.root / "repos" / REPO)
        self.log_path = self.root / "server.log"
        self.marker = uuid.uuid4().hex
        self.token = secrets.token_hex(16)
        self.port = free_port()
        self.scripted = ScriptedLLM(Scenario(product_assumption=PRODUCT_LINE))
        self.llm = MockLLM(handler=plain_handler(self.scripted, "local"), require_reasoning_passback="reasoning",
                           model_ids=[LLM_MODEL])
        self.llm.start()
        self.owui = MockOpenWebUI()
        self.owui.add_workspace_model("kate", system="You are Kate. Hello {{USER_NAME}}.", name="Kate",
                                      params={"temperature": 0.6})
        self.owui.start()
        self.ntfy = MockNtfy(unix=True).start()
        self.tapped: set[str] = set()
        self.proc: subprocess.Popen | None = None
        self.failed = False
        self._log: Any = None
        self.stopping = threading.Event()
        self._write_config()
        self.env = dict(clean_environment(), HOME=str(self.home), FACTORY_CONFIG=str(self.config_path),
                        FACTORY_PYTHON=PYTHON, E2E_MARKER=self.marker, PYTHONDONTWRITEBYTECODE="1",
                        SERAPHINA_NTFY_TOKEN=HARNESS_TOKEN, SERAPHINA_NTFY_REPLY_TOKEN=REPLY_TOKEN)

    # -- the config a fresh install has, plus what no browser can set up --------------

    def _write_config(self) -> None:
        hour = datetime.now(timezone.utc).hour   # keep do-not-disturb time far from now
        why = {"kind": "cost", "text": "a one-model test server: every step on the local model, free"}
        judge = {"model": "local", "independent_alternate": False,
                 "reason": {"kind": "stage-fit", "text": "one model on this server: the judges share the builder's "
                                                         "model by choice"}}
        config = {
            "server": {"host": "127.0.0.1", "port": self.port, "token": self.token},
            "data_dir": str(self.data_dir),
            "providers": {"openwebui": {"type": "openai", "kind": "openwebui", "base_url": self.owui.base_url + "/api",
                                        "api_key_env": "OPENWEBUI_API_KEY", "auth": "bearer", "max_concurrency": 1,
                                        "concurrency_scope": "run", "timeout": 60}},
            "models": {"kate": {"provider": "openwebui", "model": "kate", "max_tokens": 2048,
                                "context_tokens": 65536, "keep_reasoning": False, "stream": True, "extra_body": {}}},
            "agents": {"*": {"driver": "builtin"}},
            "pipeline": {"max_parallel_jobs": 1},
            "limits": {"max_wall_seconds": 600, "bash_timeout": 120},
            "harness": {"sandbox": "none"},
            "profiles": {PROFILE: {
                "description": "Every step on the local model; the judges share it by choice.",
                "agents": {"spec-writer": {"model": "local", "reason": why},
                           "builder": {"model": "local", "reason": why},
                           **{f"reviewer-{lens}": dict(judge) for lens in ("security", "ux", "ui", "code")},
                           "approver": dict(judge)}}},
            "wakes": {"timezone": "UTC", "time_sync": {"require": False}},
            "notify": {"quiet_hours": f"{(hour + 6) % 24:02d}:00-{(hour + 7) % 24:02d}:00",
                       "channels": {"phone": {
                           "type": "ntfy", "publish_url": self.ntfy.publish_url,
                           "public_url": "https://box.tail0123.ts.net:8445",
                           "token_env": "SERAPHINA_NTFY_TOKEN", "reply_token_env": "SERAPHINA_NTFY_REPLY_TOKEN",
                           "operational": True}}},
        }
        self.config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        os.chmod(self.config_path, 0o600)
        env_file = self.config_dir / "env"
        env_file.write_text(f"# the mock Open WebUI's key\nOPENWEBUI_API_KEY={OWNER_KEY}\n", encoding="utf-8")
        os.chmod(env_file, 0o600)

    # -- the server -------------------------------------------------------------------

    def start(self, timeout: float = 30) -> None:
        self._log = open(self.log_path, "ab")
        self.proc = subprocess.Popen([str(FACTORY), "serve", "--port", str(self.port)], cwd=str(self.root),
                                     env=self.env, stdin=subprocess.DEVNULL, stdout=self._log,
                                     stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"factory serve exited with {self.proc.returncode}:\n{self.log_tail()}")
            try:
                request = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/state",
                                                 headers={"Authorization": f"Bearer {self.token}"})
                with urllib.request.urlopen(request, timeout=2) as response:   # noqa: S310 (loopback)
                    if response.status == 200:
                        return
            except OSError:
                pass
            time.sleep(0.1)
        raise RuntimeError(f"factory serve did not answer within {timeout}s:\n{self.log_tail()}")

    def log_tail(self, lines: int = 80) -> str:
        try:
            return "\n".join(self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
        except OSError:
            return "(no server log)"

    def close(self) -> list[str]:
        """Stop everything; the descriptions of stray processes found (and killed)."""
        proc = self.proc
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(25)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(10)
        if self._log is not None:
            self._log.close()
            self._log = None
        time.sleep(0.2)
        strays = []
        needle = f"E2E_MARKER={self.marker}".encode()
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit() or int(entry.name) == os.getpid():
                continue
            try:
                if needle not in (entry / "environ").read_bytes().split(b"\0"):
                    continue
                cmd = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace").strip()
                os.kill(int(entry.name), signal.SIGKILL)
            except OSError:
                continue
            strays.append(f"pid {entry.name}: {cmd}")
        for mock in (self.llm, self.owui, self.ntfy):
            try:
                mock.stop()
            except Exception:  # noqa: BLE001 (best effort on the way out)
                pass
        if self.keep or self.failed or strays:
            print(f"KEPT {self.root}", flush=True)
        else:
            shutil.rmtree(self.root, ignore_errors=True)
        return strays

    # -- what the phone and the operator do ---------------------------------------------

    def status(self) -> dict:
        return {"violations": list(self.scripted.violations), "tool_errors": list(self.scripted.tool_errors),
                "llm_requests": len(self.llm.requests), "agents": sorted({c["agent"] for c in self.scripted.calls}),
                "openwebui_requests": len(self.owui.requests), "openwebui_writes": list(self.owui.writes),
                "ntfy_publishes": [{"title": p["body"].get("title"), "topic": p["body"].get("topic")}
                                   for p in list(self.ntfy.publishes)],
                "server_alive": self.proc is not None and self.proc.poll() is None}

    def tap(self, title: str, timeout: float) -> dict:
        """Got it on the newest notice titled `title` not tapped yet, as the phone's ntfy app sends it."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for publish in reversed(list(self.ntfy.publishes)):
                body = publish["body"]
                key = str(body.get("sequence_id") or body.get("message"))
                if body.get("title") != title or key in self.tapped or not body.get("actions"):
                    continue
                got_it = next((a for a in body["actions"] if a.get("label") == "Got it"), None)
                if got_it is None or not TAP_JSON.match(str(got_it.get("body") or "")):
                    continue
                status, _ = self.ntfy.post(TAPS_TOPIC, got_it["body"], token=REPLY_TOKEN)
                self.tapped.add(key)
                return {"tapped": status == 200, "status": status, "title": title,
                        "message": body.get("message"), "sequence_id": body.get("sequence_id")}
            time.sleep(0.1)
        return {"tapped": False, "error": f"no notice titled {title!r} with a Got it button within {timeout}s",
                "seen": [p["body"].get("title") for p in list(self.ntfy.publishes)]}

    def receipt(self, over: dict) -> dict:
        """One synthetic change receipt (operator-receipt/1) in the receipts folder, timed now."""
        now = datetime.now(timezone.utc).replace(microsecond=0)
        rid = str(over.get("id") or "CHG-" + now.strftime("%Y%m%d-%H%M%S"))
        doc = {"schema": "operator-receipt/1", "id": rid, "time": (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "actor": "kate", "intent": over.get("intent") or "Keep two weeks of the web server's access logs",
               "undo_class": "undo-restore", "jobs": [],
               "before": {"logrotate": {"path": "/etc/logrotate.d/example", "rotate": 7}},
               "after": {"logrotate": {"path": "/etc/logrotate.d/example", "rotate": 14}},
               "rollback": "Restore /etc/logrotate.d/example from the snapshot taken before the change.",
               "verified": list(over.get("verified") or ["logrotate --debug passes"]),
               "not_verified": list(over.get("not_verified") or ["restore drill not done"])}
        path = self.receipts_dir / f"{rid.lower()}.json"
        tmp = path.with_name("." + path.name + ".tmp")   # hidden while written, as her tools would
        tmp.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        return {"path": str(path), "id": rid, "intent": doc["intent"]}


def control_server(backend: Backend) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def _reply(self, status: int, payload: Any) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if self.path == "/status":
                return self._reply(200, backend.status())
            if self.path == "/log":
                return self._reply(200, {"log": backend.log_tail(200)})
            return self._reply(404, {"error": "not found"})

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            except ValueError:
                return self._reply(400, {"error": "bad json"})
            if self.path == "/phone/tap":
                return self._reply(200, backend.tap(str(body.get("title") or "Test"), float(body.get("timeout") or 20)))
            if self.path == "/receipt":
                return self._reply(200, backend.receipt(body if isinstance(body, dict) else {}))
            if self.path == "/failed":
                backend.failed = True
                return self._reply(200, {"failed": True})
            if self.path == "/stop":
                self._reply(200, {"stopping": True})
                backend.stopping.set()
                return None
            return self._reply(404, {"error": "not found"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scratch", help="where the root goes (default $FACTORY_E2E_SCRATCH or the temp dir)")
    parser.add_argument("--keep", action="store_true", help="keep the root afterwards")
    args = parser.parse_args(argv)
    backend = Backend(_scratch(args.scratch), args.keep or os.environ.get("FACTORY_E2E_KEEP") == "1")
    control = None
    try:
        backend.start()
        control = control_server(backend)
        ready = {"url": f"http://127.0.0.1:{backend.port}", "token": backend.token,
                 "control": f"http://127.0.0.1:{control.server_address[1]}", "root": str(backend.root),
                 "repo": str(backend.repo), "repo_name": REPO, "llm_url": backend.llm.base_url + "/v1",
                 "llm_model": LLM_MODEL, "receipts_dir": str(backend.receipts_dir),
                 "openwebui": backend.owui.base_url, "product_line": PRODUCT_LINE, "profile": PROFILE,
                 "log": str(backend.log_path)}
        print("READY " + json.dumps(ready), flush=True)
        signal.signal(signal.SIGTERM, lambda *_: backend.stopping.set())
        signal.signal(signal.SIGINT, lambda *_: backend.stopping.set())

        def watch_stdin() -> None:
            try:
                while sys.stdin.read(4096):
                    pass
            except (OSError, ValueError):
                pass
            backend.stopping.set()

        threading.Thread(target=watch_stdin, daemon=True).start()
        while not backend.stopping.wait(0.5):
            if backend.proc is not None and backend.proc.poll() is not None:
                backend.failed = True
                print(f"SERVER EXITED {backend.proc.returncode}\n{backend.log_tail()}", flush=True)
                break
        return 0
    except Exception as exc:  # noqa: BLE001 (report, then clean up)
        backend.failed = True
        print(f"FAILED {type(exc).__name__}: {exc}", flush=True)
        return 1
    finally:
        if control is not None:
            control.shutdown()
        strays = backend.close()
        if strays:
            print("STRAYS " + json.dumps(strays), flush=True)


if __name__ == "__main__":
    sys.exit(main())
