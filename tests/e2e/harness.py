"""A real `factory serve` subprocess against scripted mock models, for tests/e2e/test_e2e_server.py.

FactoryServer(name, scenario) sets up one scenario from scratch:

- a temp root with its own HOME (so ~/.config/software-factory/agents, skills and
  ~/.gitconfig are empty), FACTORY_CONFIG, data_dir and a fresh copy (``cp -a``)
  of the fixture repo (``$FACTORY_E2E_FIXTURE``; without one, the same tiny todo
  app is created from FIXTURE_FILES);
- two MockLLM servers played by one ScriptedLLM: "main" (OpenAI and Anthropic
  formats, DeepSeek's reasoning-passback rule enforced) and "local" (an
  OpenAI-compatible local server, reasoning under the ``reasoning`` key);
- config.json: providers ``mock-openai`` (openai, key in $MOCK_OPENAI_KEY, set
  in the server's environment), ``mock-anthropic`` (anthropic, key
  $MOCK_ANTHROPIC_KEY, read from the env file next to the config) and
  ``mock-local-server`` (keyless, max_concurrency 1, concurrency_scope run);
  models ``opus`` -> mock-openai, ``sonnet`` and ``haiku`` -> mock-anthropic,
  ``mock-local`` -> mock-local-server;
- ``bin/factory serve --port <free port>`` with that environment (secret-looking
  variables of this process removed), its output in <root>/server.log;
- an EventCollector that follows /api/events the way the GUI does (reconnecting
  across restarts), keeping every event and the raw stream text.

Helpers: api()/get()/post() with the token, wait_job/wait_chat/wait_plan with
timeouts that fail with the job, the server log and the scripted model's
violations, git() on the fixture repo, cli() for `bin/factory` commands against
the live server, kill()/restart(). close() stops the server (SIGTERM, then
SIGKILL), checks that no process carrying this scenario's marker variable is left
(agent shells, dev servers), kills any it finds and reports them.

Environment: FACTORY_E2E_SCRATCH (where scenario roots go; default the system
temp dir), FACTORY_E2E_FIXTURE (the fixture template), FACTORY_E2E_KEEP=1 (keep
every root; a failed scenario's root is kept anyway), FACTORY_PYTHON (the
interpreter the server runs on; default this one).
"""

from __future__ import annotations

import http.client
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from tests.e2e.scripted_llm import Scenario, ScriptedLLM
from tests.mock_llm import MockLLM

ROOT = Path(__file__).resolve().parents[2]
FACTORY = ROOT / "bin" / "factory"
SCRATCH = Path(os.environ.get("FACTORY_E2E_SCRATCH") or Path(tempfile.gettempdir()) / "sf-e2e")


def under_tmpfs(path: Path) -> bool:
    """True if path is under /tmp (which bwrap replaces with a fresh tmpfs, hiding whatever is there)."""
    return str(Path(path).resolve()).startswith("/tmp/") or str(Path(path).resolve()) == "/tmp"


def bwrap_scratch() -> Path | None:
    """A scratch base bwrap does not mask (not under /tmp), for the sandboxed scenario; None if there is none.

    Order: $FACTORY_E2E_BWRAP_SCRATCH, SCRATCH itself when it is already outside /tmp, else a folder under
    $HOME. In production the repo lives under /srv or /home, not /tmp; the default scratch is a temp dir, so
    the sandboxed scenario needs a non-tmpfs base of its own.
    """
    for candidate in (os.environ.get("FACTORY_E2E_BWRAP_SCRATCH"), None if under_tmpfs(SCRATCH) else SCRATCH,
                      Path(os.path.expanduser("~")) / ".sf-e2e-bwrap"):
        if candidate is None:
            continue
        base = Path(candidate)
        if under_tmpfs(base):
            continue
        try:
            base.mkdir(parents=True, exist_ok=True)
            probe = base / ".probe"
            probe.write_text("x")
            probe.unlink()
            return base
        except OSError:
            continue
    return None
FIXTURE = os.environ.get("FACTORY_E2E_FIXTURE") or ""
KEEP = os.environ.get("FACTORY_E2E_KEEP") == "1"
PYTHON = os.environ.get("FACTORY_PYTHON") or sys.executable
REPO = "todo"
ACTIVE = ("queued", "spec", "build", "review", "approve", "merging")
STOPPED = ("merged", "delivered", "needs-human", "failed", "rejected", "cancelled")
SECRET_NAME_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)", re.IGNORECASE)
SECRET_PREFIXES = ("ANTHROPIC_", "OPENAI_", "DEEPSEEK_", "FACTORY_", "GIT_CONFIG", "CLAUDE_", "AWS_", "MOCK_")

FIXTURE_FILES = {
    "AGENTS.md": """# AGENTS.md

## Overview
A tiny todo web app. Plain HTML/CSS/JS, no dependencies. `src/todo.js` holds the
pure logic (ES module), `src/app.js` wires it to the DOM in `index.html`.

## Setup for a fresh checkout
None.

## Dev server
`python3 -m http.server <port> --bind 127.0.0.1`

## Validation
- Tests: `node --test` (one file: `node --test test/todo.test.js`)
- Lint: `node --check src/todo.js src/app.js`

## Conventions
- ES modules, 2-space indent, double quotes, no dependencies.
- Pure logic lives in `src/todo.js` and has tests in `test/`.
- Colours and spacing come from the CSS variables in `styles.css`.

## Styling
Design tokens are the `:root` variables in `styles.css`.

## Hot spots
None.
""",
    "package.json": '{ "type": "module", "private": true }\n',
    "src/todo.js": """export function addItem(items, text) {
  const trimmed = text.trim();
  if (!trimmed) return items;
  return [...items, { text: trimmed, done: false }];
}

export function toggleItem(items, index) {
  return items.map((item, i) => (i === index ? { ...item, done: !item.done } : item));
}

export function removeItem(items, index) {
  return items.filter((_, i) => i !== index);
}
""",
    "src/app.js": """import { addItem, toggleItem, removeItem } from "./todo.js";

let items = [];
const list = document.getElementById("list");
const empty = document.getElementById("empty");

function render() {
  list.replaceChildren(...items.map((item, i) => {
    const li = document.createElement("li");
    const label = document.createElement("label");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = item.done;
    box.addEventListener("change", () => { items = toggleItem(items, i); render(); });
    label.append(box, " ", item.text);
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "Remove";
    remove.addEventListener("click", () => { items = removeItem(items, i); render(); });
    li.append(label, remove);
    return li;
  }));
  empty.hidden = items.length > 0;
}

document.getElementById("add-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const input = document.getElementById("new-item");
  items = addItem(items, input.value);
  input.value = "";
  render();
});

render();
""",
    "test/todo.test.js": """import { test } from "node:test";
import assert from "node:assert/strict";
import { addItem, toggleItem, removeItem } from "../src/todo.js";

test("addItem trims and appends", () => {
  assert.deepEqual(addItem([], "  milk "), [{ text: "milk", done: false }]);
});

test("addItem ignores blank text", () => {
  assert.deepEqual(addItem([], "   "), []);
});

test("toggleItem flips one item", () => {
  const items = [{ text: "a", done: false }, { text: "b", done: false }];
  assert.equal(toggleItem(items, 1)[1].done, true);
  assert.equal(toggleItem(items, 1)[0].done, false);
});

test("removeItem removes by index", () => {
  assert.deepEqual(removeItem([{ text: "a", done: false }], 0), []);
});
""",
    "index.html": """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Todo</title>
  <link rel="stylesheet" href="styles.css">
</head>
<body>
  <main class="card">
    <h1>Todo</h1>
    <form id="add-form">
      <label for="new-item" class="visually-hidden">New item</label>
      <input id="new-item" placeholder="What needs doing?" autocomplete="off">
      <button type="submit">Add</button>
    </form>
    <ul id="list"></ul>
    <p id="empty" class="muted">Nothing to do yet.</p>
  </main>
  <script type="module" src="src/app.js"></script>
</body>
</html>
""",
    "styles.css": ":root { --fg: #1d1d1f; --bg: #fff; --space: 8px; }\nbody { color: var(--fg); background: var(--bg); }\n",
}


class E2EError(AssertionError):
    """A wait timed out or the server misbehaved; the message carries the diagnostics."""


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def clean_environment() -> dict[str, str]:
    """This process's environment without anything that looks like a credential or redirects git config."""
    return {name: value for name, value in os.environ.items()
            if not SECRET_NAME_RE.search(name) and not name.startswith(SECRET_PREFIXES)}


def make_fixture(dest: Path) -> Path:
    """A fresh copy of the fixture repo at dest (cp -a of $FACTORY_E2E_FIXTURE, else built from FIXTURE_FILES)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if FIXTURE and Path(FIXTURE).is_dir():
        subprocess.run(["cp", "-a", FIXTURE, str(dest)], check=True)
        return dest
    dest.mkdir(parents=True)
    for rel, text in FIXTURE_FILES.items():
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        (dest / rel).write_text(text, encoding="utf-8")
    env = dict(clean_environment(), GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
    for args in (["init", "-q", "-b", "main"], ["config", "user.name", "Fixture Owner"],
                 ["config", "user.email", "fixture@example.invalid"], ["config", "commit.gpgsign", "false"],
                 ["add", "-A"], ["commit", "-q", "-m", "Initial todo app"]):
        subprocess.run(["git", "-C", str(dest), *args], check=True, env=env, capture_output=True)
    return dest


class Response:
    def __init__(self, status: int, body: bytes, headers: dict) -> None:
        self.status = status
        self.body = body
        self.headers = headers
        try:
            self.json: Any = json.loads(body) if body else None
        except ValueError:
            self.json = None

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def __repr__(self) -> str:
        return f"<Response {self.status} {self.text[:300]!r}>"


class EventCollector(threading.Thread):
    """Follows /api/events like the GUI: every event (with the server generation it came from) and the raw text."""

    def __init__(self, server: "FactoryServer") -> None:
        super().__init__(name="e2e-events", daemon=True)
        self.server = server
        self.events: list[dict] = []
        self.raw: list[str] = []
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self._conn: http.client.HTTPConnection | None = None

    def run(self) -> None:
        gen, last = -1, 0
        while not self.stopping.is_set():
            current = self.server.generation
            since = last if current == gen else 0
            gen = current
            try:
                conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=40)
                self._conn = conn
                conn.request("GET", f"/api/events?since={since}", headers=self.server.auth_headers())
                response = conn.getresponse()
                if response.status != 200:
                    raise OSError(f"events: HTTP {response.status}")
                frame: dict[str, str] = {}
                while not self.stopping.is_set():
                    line = response.fp.readline()
                    if not line:
                        break
                    text = line.decode("utf-8", "replace")
                    with self.lock:
                        self.raw.append(text)
                    text = text.rstrip("\r\n")
                    if not text:
                        if "data" in frame:
                            event = json.loads(frame["data"])
                            event["gen"] = gen
                            with self.lock:
                                self.events.append(event)
                            last = max(last, int(event.get("seq") or 0))
                        frame = {}
                    elif not text.startswith(":") and ":" in text:
                        name, _, value = text.partition(":")
                        frame[name] = value[1:] if value.startswith(" ") else value
            except (OSError, http.client.HTTPException, ValueError):
                pass
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
            if self.server.generation != gen:
                last = 0
            self.stopping.wait(0.2)

    def stop(self) -> None:
        self.stopping.set()
        conn = self._conn
        if conn is not None and conn.sock is not None:
            try:
                conn.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.join(5)

    def of(self, type_: str | None = None, **match: Any) -> list[dict]:
        """Events of a type whose top-level fields (repo, job, agent) or data fields (data__x=) match."""
        with self.lock:
            events = list(self.events)
        found = []
        for event in events:
            if type_ is not None and event.get("type") != type_:
                continue
            ok = True
            for key, value in match.items():
                if key.startswith("data__"):
                    ok = ok and (event.get("data") or {}).get(key[6:]) == value
                else:
                    ok = ok and event.get(key) == value
            if ok:
                found.append(event)
        return found

    def text(self) -> str:
        with self.lock:
            return "".join(self.raw)


class FactoryServer:
    """One scenario's server, mocks, repo and data; see the module docstring."""

    def __init__(self, name: str, scenario: Scenario | None = None, *, sandbox: str = "none",
                 config_patch: dict | None = None, max_parallel_jobs: int = 2,
                 extra_env: dict | None = None, scratch: Path | None = None) -> None:
        base = Path(scratch) if scratch is not None else SCRATCH
        base.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[-40:]
        self.root = Path(tempfile.mkdtemp(prefix=f"srv-{slug}-", dir=base)).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.config_dir = self.root / "config"
        self.config_dir.mkdir(mode=0o700)
        self.config_path = self.config_dir / "config.json"
        self.data_dir = self.root / "data"
        self.repo = make_fixture(self.root / "repos" / REPO)
        self.log_path = self.root / "server.log"
        self.marker = uuid.uuid4().hex
        self.token = secrets.token_hex(16)
        self.openai_key = "sk-e2e-openai-" + secrets.token_hex(12)
        self.anthropic_key = "sk-ant-e2e-" + secrets.token_hex(12)
        self.scenario = scenario or Scenario()
        self.scripted = ScriptedLLM(self.scenario)
        self.mock = MockLLM(handler=self.scripted.handler("main"), require_reasoning_passback=True)
        self.local_mock = MockLLM(handler=self.scripted.handler("local"), require_reasoning_passback="reasoning")
        self.mock.start()
        self.local_mock.start()
        self.port = free_port()
        self.generation = 0
        self.proc: subprocess.Popen | None = None
        self.failed = False
        self._log_handle: Any = None
        self.config = self._config(sandbox, max_parallel_jobs, config_patch or {})
        self.config_path.write_text(json.dumps(self.config, indent=2) + "\n", encoding="utf-8")
        os.chmod(self.config_path, 0o600)
        env_file = self.config_dir / "env"
        env_file.write_text(f"# keys for the e2e mock providers\nMOCK_ANTHROPIC_KEY={self.anthropic_key}\n",
                            encoding="utf-8")
        os.chmod(env_file, 0o600)
        self.env = dict(clean_environment(), HOME=str(self.home), FACTORY_CONFIG=str(self.config_path),
                        MOCK_OPENAI_KEY=self.openai_key, FACTORY_PYTHON=PYTHON, E2E_MARKER=self.marker,
                        PYTHONDONTWRITEBYTECODE="1", **(extra_env or {}))
        self.events = EventCollector(self)
        self.start()
        self.events.start()

    # -- config and process -------------------------------------------------------

    def _config(self, sandbox: str, max_parallel_jobs: int, patch: dict) -> dict:
        base = self.mock.base_url
        config = {
            "server": {"host": "127.0.0.1", "port": self.port, "token": self.token},
            "data_dir": str(self.data_dir),
            "repos": [{"name": REPO, "path": str(self.repo)}],
            "providers": {
                "mock-openai": {"type": "openai", "base_url": base, "api_key_env": "MOCK_OPENAI_KEY", "timeout": 60},
                "mock-anthropic": {"type": "anthropic", "base_url": base, "api_key_env": "MOCK_ANTHROPIC_KEY",
                                   "timeout": 60},
                "mock-local-server": {"type": "openai", "base_url": self.local_mock.base_url + "/v1",
                                      "api_key_env": None, "max_concurrency": 1, "concurrency_scope": "run",
                                      "timeout": 60},
            },
            "models": {
                "opus": {"provider": "mock-openai", "model": "mock-gpt-e2e", "max_tokens": 4096, "stream": True,
                         "extra_body": {"reasoning_effort": "high"}},
                "sonnet": {"provider": "mock-anthropic", "model": "mock-claude-e2e", "max_tokens": 4096,
                           "stream": True, "extra_body": {"thinking": {"type": "adaptive", "display": "summarized"}}},
                "haiku": {"provider": "mock-anthropic", "model": "mock-claude-e2e-small", "max_tokens": 2048,
                          "stream": False, "extra_body": {}},
                "mock-local": {"provider": "mock-local-server", "model": "mock-local-model-7b", "max_tokens": 2048,
                               "context_tokens": 65536, "stream": True, "extra_body": {}},
            },
            "agents": {"*": {"driver": "builtin"}},
            "pipeline": {"max_parallel_jobs": max_parallel_jobs},
            "limits": {"max_wall_seconds": 600, "bash_timeout": 120},
            "harness": {"sandbox": sandbox},
        }
        return _merge(config, patch)

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def start(self, timeout: float = 30) -> None:
        """Start `factory serve` and wait until /api/state answers."""
        self.generation += 1
        self._log_handle = open(self.log_path, "ab")
        self._log_handle.write(f"\n===== start {self.generation} =====\n".encode())
        self._log_handle.flush()
        self.proc = subprocess.Popen([str(FACTORY), "serve", "--port", str(self.port)], cwd=str(self.root),
                                     env=self.env, stdin=subprocess.DEVNULL, stdout=self._log_handle,
                                     stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise E2EError(f"factory serve exited with {self.proc.returncode}:\n{self.log_tail()}")
            try:
                if self.api("GET", "/api/state", timeout=2).status == 200:
                    return
            except OSError:
                pass
            time.sleep(0.1)
        raise E2EError(f"factory serve did not answer within {timeout}s:\n{self.log_tail()}")

    def stop(self, sig: int = signal.SIGTERM, timeout: float = 25) -> float:
        """Signal the server and wait for it to exit (SIGKILL after timeout); returns the seconds it took."""
        started = time.monotonic()
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return 0.0
        try:
            os.kill(proc.pid, sig)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout)
        except subprocess.TimeoutExpired:
            os.kill(proc.pid, signal.SIGKILL)
            proc.wait(10)
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None
        return time.monotonic() - started

    def kill(self) -> None:
        """SIGKILL the server (no shutdown code runs)."""
        self.stop(signal.SIGKILL)

    def restart(self) -> None:
        self.start()

    def server_pid(self) -> int | None:
        return self.proc.pid if self.proc is not None else None

    def close(self) -> list[str]:
        """Stop everything; returns descriptions of stray processes found (and killed)."""
        self.events.stop()
        self.stop()
        time.sleep(0.2)
        strays = self.strays()
        for pid, _ in strays:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.mock.stop()
        self.local_mock.stop()
        if not KEEP and not self.failed:
            shutil.rmtree(self.root, ignore_errors=True)
        return [f"pid {pid}: {cmd}" for pid, cmd in strays]

    def strays(self) -> list[tuple[int, str]]:
        """Processes (other than this one) whose environment carries this scenario's marker."""
        found = []
        needle = f"E2E_MARKER={self.marker}".encode()
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit() or int(entry.name) == os.getpid():
                continue
            try:
                environ = (entry / "environ").read_bytes()
                if needle not in environ.split(b"\0"):
                    continue
                cmd = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace").strip()
            except OSError:
                continue
            found.append((int(entry.name), cmd))
        return found

    def log_tail(self, lines: int = 60) -> str:
        try:
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "(no server log)"
        return "\n".join(text.splitlines()[-lines:])

    # -- HTTP ---------------------------------------------------------------------

    def api(self, method: str, path: str, body: Any = None, *, token: bool = True, timeout: float = 30,
            headers: dict | None = None) -> Response:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            sent = dict(self.auth_headers() if token else {})
            data = None
            if method == "POST":
                sent["Content-Type"] = "application/json"
                data = json.dumps(body if body is not None else {}).encode()
            sent.update(headers or {})
            conn.request(method, path, data, sent)
            response = conn.getresponse()
            return Response(response.status, response.read(), dict(response.getheaders()))
        finally:
            conn.close()

    def get(self, path: str, status: int = 200) -> Any:
        response = self.api("GET", path)
        if response.status != status:
            raise E2EError(f"GET {path}: {response!r}")
        return response.json if response.json is not None else response.text

    def post(self, path: str, body: Any = None, status: int | tuple = (200, 201, 202)) -> Any:
        response = self.api("POST", path, body)
        wanted = status if isinstance(status, tuple) else (status,)
        if response.status not in wanted:
            raise E2EError(f"POST {path} {body!r}: {response!r}")
        return response.json

    def job(self, job_id: str, repo: str = REPO) -> dict:
        return self.get(f"/api/jobs/{repo}/{job_id}")["job"]

    def job_files(self, job_id: str, repo: str = REPO) -> list[str]:
        return self.get(f"/api/jobs/{repo}/{job_id}")["files"]

    def job_file(self, job_id: str, rel: str, repo: str = REPO) -> str:
        return self.get(f"/api/jobs/{repo}/{job_id}/file?path={rel}")

    def submit(self, feature: str, routing: Any = None, repo: str = REPO) -> dict:
        body = {"repo": repo, "feature": feature}
        if routing is not None:
            body["routing"] = routing
        return self.post("/api/jobs", body, status=201)["job"]

    # -- waiting ------------------------------------------------------------------

    def diagnostics(self, extra: Any = None) -> str:
        parts = []
        if extra is not None:
            parts.append(json.dumps(extra, indent=1, default=str)[:6000])
        parts.append("scripted violations: " + json.dumps(self.scripted.violations[-10:], indent=1))
        parts.append("tool errors: " + json.dumps(self.scripted.tool_errors[-10:], indent=1, default=str)[:4000])
        parts.append("server log:\n" + self.log_tail())
        return "\n".join(parts)

    def wait_for(self, predicate: Callable[[], Any], timeout: float, what: str,
                 context: Callable[[], Any] | None = None, interval: float = 0.15) -> Any:
        deadline = time.monotonic() + timeout
        while True:
            value = predicate()
            if value:
                return value
            if time.monotonic() > deadline:
                self.failed = True
                raise E2EError(f"timed out after {timeout}s waiting for {what}\n"
                               + self.diagnostics(context() if context else None))
            if self.proc is not None and self.proc.poll() is not None:
                self.failed = True
                raise E2EError(f"the server exited ({self.proc.returncode}) while waiting for {what}\n"
                               + self.diagnostics())
            time.sleep(interval)

    def wait_job(self, job_id: str, stages: str | tuple = "merged", timeout: float = 150, repo: str = REPO) -> dict:
        """Wait until the job is in one of stages (and idle); fail at once if it stops in another stage."""
        wanted = (stages,) if isinstance(stages, str) else tuple(stages)
        seen: dict = {}

        def check() -> dict | None:
            job = self.job(job_id, repo)
            seen["job"] = job
            if job["stage"] in wanted:
                return job
            if job["stage"] in STOPPED and job["stage"] not in wanted:
                time.sleep(0.5)   # a stop stage can be momentary (approve -> override is requeued)
                job = self.job(job_id, repo)
                if job["stage"] in STOPPED and job["stage"] not in wanted:
                    self.failed = True
                    raise E2EError(f"job {job_id} stopped at {job['stage']} ({job.get('reason')}), wanted "
                                   f"{wanted}\n" + self.diagnostics(job))
            return None

        self.wait_for(check, timeout, f"job {job_id} to reach {wanted}", lambda: seen.get("job"))
        # The worker may still be finishing the step that saved the stage; let it settle.
        time.sleep(0.3)
        return self.job(job_id, repo)

    def wait_chat(self, chat_id: str, timeout: float = 60) -> dict:
        seen: dict = {}

        def check() -> dict | None:
            reply = self.get(f"/api/chats/{chat_id}")
            seen["chat"] = reply
            return reply if reply["chat"]["status"] != "running" else None

        return self.wait_for(check, timeout, f"chat {chat_id} to finish its turn", lambda: seen.get("chat"))

    def wait_plan(self, plan_id: str, stages: str | tuple, timeout: float = 240, repo: str = REPO) -> dict:
        wanted = (stages,) if isinstance(stages, str) else tuple(stages)
        seen: dict = {}

        def check() -> dict | None:
            reply = self.get(f"/api/plans/{repo}/{plan_id}")
            seen["plan"] = reply
            stage = reply["plan"]["stage"]
            if stage in wanted:
                return reply
            if stage in ("needs-human", "failed", "cancelled", "blocked") and stage not in wanted:
                self.failed = True
                raise E2EError(f"plan {plan_id} is {stage} ({reply['plan'].get('reason')}), wanted {wanted}\n"
                               + self.diagnostics(reply))
            return None

        return self.wait_for(check, timeout, f"plan {plan_id} to reach {wanted}", lambda: seen.get("plan"))

    # -- the repo, the CLI ------------------------------------------------------------

    def git(self, *args: str, cwd: Path | None = None, check: bool = True) -> str:
        env = dict(clean_environment(), HOME=str(self.home), GIT_CONFIG_NOSYSTEM="1")
        done = subprocess.run(["git", "-C", str(cwd or self.repo), *args], capture_output=True, text=True, env=env)
        if check and done.returncode != 0:
            raise E2EError(f"git {' '.join(args)} failed: {done.stderr}")
        return done.stdout

    def node_test(self, cwd: Path | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(["node", "--test"], cwd=str(cwd or self.repo), capture_output=True, text=True,
                              env=dict(clean_environment(), HOME=str(self.home)), timeout=120)

    def cli(self, *args: str, input: str | None = None, timeout: float = 60) -> subprocess.CompletedProcess:
        return subprocess.run([str(FACTORY), *args], cwd=str(self.root), env=self.env, input=input,
                              capture_output=True, text=True, timeout=timeout)

    def lock_pid(self) -> int | None:
        try:
            fields = (self.data_dir / "factory.lock").read_text().split()
        except OSError:
            return None
        return int(fields[0]) if fields and fields[0].isdigit() else None


def _merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out
