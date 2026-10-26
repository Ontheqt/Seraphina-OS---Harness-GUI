"""Tests for swfactory.server: every route, auth, Host/Origin guards, bodies, SSE, static files, shutdown."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import http.client
import io
import json
import os
import re
import shutil
import signal
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from swfactory import personas
from swfactory import server as srv
from swfactory.agentdefs import AgentDef
from swfactory.config import Config
from swfactory.events import Event, EventBus
from swfactory.jobs import JobStore
from swfactory.orchestrator import Factory
from swfactory.settings import user_agents_dir
from tests.helpers import commit_file, make_repo, run_git

TOKEN = "0123456789abcdef" * 2
REPO = "demo"
JOB = "001-first-job"
CHAT = "c-0123456789"
PLAN = "P001-add-auth"
HTML_CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'")


class ActionError(Exception):
    """Stands in for orchestrator.ActionError: the server matches the class by name."""


class StageError(ActionError):
    pass


class CountingBus(EventBus):
    """An EventBus that tracks how many subscriptions are open."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.open: set[int] = set()

    def subscribe(self):  # type: ignore[override]
        subscription = super().subscribe()
        self.open.add(id(subscription))
        return subscription

    def unsubscribe(self, subscription) -> None:  # type: ignore[override]
        super().unsubscribe(subscription)
        self.open.discard(id(subscription))


class RacyBus(CountingBus):
    """Publishes an event between subscribe() and the replay snapshot, like a concurrent publisher."""

    def since(self, seq: int) -> list[Event]:
        if not getattr(self, "raced", False):
            self.raced = True
            self.publish("race", {"n": 1})
        return super().since(seq)


def job_dict(repo: str, job_id: str, **extra: Any) -> dict:
    job = {"id": job_id, "repo": repo, "feature": "First job", "stage": "needs-human", "base": "main",
           "branch": f"factory/{job_id}", "merged_commit": None}
    job.update(extra)
    return job


def agent_def(name: str, model: str, path: str = "/agents") -> AgentDef:
    return AgentDef(name=name, description=f"The {name}.", tools=["Read", "Write"], model=model, skills=[],
                    mcp=[], color="cyan", body=f"You are {name}.", path=Path(path) / f"{name}.md")


class FakeFactory:
    """The whole Factory interface, in memory. Records every call; fail[name] makes that method raise."""

    def __init__(self, config: Config, bus: EventBus) -> None:
        self.config = config
        self.bus = bus
        self.calls: list[tuple[str, tuple, dict]] = []
        self.fail: dict[str, BaseException] = {}
        self.lock = threading.Lock()
        self.stopped = False
        self.job_table = {(REPO, JOB): job_dict(REPO, JOB)}
        self.backlog_items = [{"text": "Add a footer", "done": False, "job": None}]
        self.next_job: dict | None = job_dict(REPO, "003-add-footer", stage="queued")
        self.agent_table = {a.name: a for a in (agent_def("builder", "opus"), agent_def("reviewer-code", "sonnet"),
                                                agent_def("approver", "opus"))}
        self.chat_table = {CHAT: {"id": CHAT, "repo": REPO, "status": "idle", "title": "hi"}}
        self.plan_table = {(REPO, PLAN): {"id": PLAN, "repo": REPO, "stage": "ready"}}
        self.view = {"providers": {"deepseek": {"key_present": False}}, "models": {}}

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        with self.lock:
            self.calls.append((name, args, kwargs))
        if name in self.fail:
            raise self.fail[name]

    def called(self, name: str) -> list[tuple]:
        return [args for called, args, _ in self.calls if called == name]

    def _known_repo(self, repo: str) -> None:
        self.config.repo(repo)  # KeyError for unknown repos, like the real Factory

    # lifecycle
    def start(self) -> None:
        self._record("start")

    def stop(self) -> None:
        self._record("stop")
        self.stopped = True

    # jobs
    def jobs(self) -> list[dict]:
        self._record("jobs")
        return list(self.job_table.values())

    def job(self, repo: str, job_id: str) -> dict:
        self._record("job", repo, job_id)
        return dict(self.job_table[(repo, job_id)])

    def job_files(self, repo: str, job_id: str) -> list[str]:
        self._record("job_files", repo, job_id)
        self.job_table[(repo, job_id)]
        return ["feature.md", "job.json"]

    def submit(self, repo: str, feature: str, from_branch: str | None = None, routing: Any = None,
               delivery: str | None = None) -> dict:
        self._record("submit", repo, feature, **({"routing": routing} if routing is not None else {}),
                     **({"delivery": delivery} if delivery is not None else {}))
        self._known_repo(repo)
        job = job_dict(repo, "002-new-job", feature=feature, stage="queued", routing=routing)
        self.job_table[(repo, job["id"])] = job
        return job

    def _job_action(self, name: str, repo: str, job_id: str, *args: Any) -> dict:
        self._record(name, repo, job_id, *args)
        return dict(self.job_table[(repo, job_id)], last_action=name)

    def approve(self, repo: str, job_id: str) -> dict:
        return self._job_action("approve", repo, job_id)

    def rework(self, repo: str, job_id: str, note: str) -> dict:
        return self._job_action("rework", repo, job_id, note)

    def reject(self, repo: str, job_id: str, reason: str = "") -> dict:
        return self._job_action("reject", repo, job_id, reason)

    def retry(self, repo: str, job_id: str) -> dict:
        return self._job_action("retry", repo, job_id)

    def cancel(self, repo: str, job_id: str) -> dict:
        return self._job_action("cancel", repo, job_id)

    def set_routing(self, repo: str, job_id: str, routing: Any) -> dict:
        return self._job_action("set_routing", repo, job_id, routing)

    # backlog and scout
    def backlog(self, repo: str) -> list[dict]:
        self._record("backlog", repo)
        self._known_repo(repo)
        return list(self.backlog_items)

    def add_backlog(self, repo: str, text: str) -> list[dict]:
        self._record("add_backlog", repo, text)
        self.backlog_items.append({"text": text, "done": False, "job": None})
        return list(self.backlog_items)

    def next(self, repo: str) -> dict:
        self._record("next", repo)
        if self.next_job is None:
            raise ActionError(f"the backlog of {repo} has no unchecked items")
        return self.next_job

    def scout(self, repo: str) -> None:
        self._record("scout", repo)

    def scout_status(self, repo: str) -> dict:
        self._record("scout_status", repo)
        return {"draft": "# AGENTS.md draft", "running": False}

    # agents
    def agents(self, repo: str | None = None) -> dict[str, AgentDef]:
        self._record("agents")
        return dict(self.agent_table)

    def agent_detail(self, name: str) -> dict:
        self._record("agent_detail", name)
        agent = self.agent_table[name]
        return {**agent.to_dict(), "source": "builtin", "path": str(agent.path)}

    def save_agent(self, name: str, text: str) -> AgentDef:
        self._record("save_agent", name, text)
        if not text.startswith("---"):
            raise ValueError("missing frontmatter")
        return agent_def(name, "opus", "/user")

    def new_agent(self, name: str, from_: str = "reviewer-code") -> AgentDef:
        """Like settings.new_agent: ValueError (400) for an existing name or an unknown agent to copy."""
        self._record("new_agent", name, from_)
        if name in self.agent_table:
            raise ValueError(f"agent {name!r} already exists (builtin); edit it instead")
        if from_ not in self.agent_table:
            raise ValueError(f"unknown agent {from_!r} to copy from")
        return agent_def(name, "sonnet", "/user")

    def reset_agent(self, name: str) -> dict | None:
        """Like orchestrator.Factory.reset_agent: delete <user agents dir>/<name>.md (settings.user_agents_dir,
        ~/.config/software-factory/agents whatever the config path; KeyError if there is no override),
        then return the agent now in effect, or None when none of that name remains."""
        self._record("reset_agent", name)
        try:
            (user_agents_dir(self.config) / f"{name}.md").unlink()
        except FileNotFoundError:
            raise KeyError(f"agent {name!r} has no user override") from None
        if name not in self.agent_table:
            return None
        return {"name": name, "source": "builtin"}

    def doctor(self) -> list[dict]:
        self._record("doctor")
        return [{"name": "git", "ok": True, "detail": "2.43", "level": "info"}]

    # chats
    def chats(self) -> list[dict]:
        self._record("chats")
        return list(self.chat_table.values())

    def chat(self, chat_id: str) -> dict:
        self._record("chat", chat_id)
        return {**self.chat_table[chat_id], "messages": [{"role": "user", "text": "hi"}]}

    def new_chat(self, repo: str, model: str | None, mode: str = "worktree", agent: str = "coder") -> dict:
        self._record("new_chat", repo, model, mode, agent=agent)
        self._known_repo(repo)
        return {"id": "c-aaaaaaaaaa", "repo": repo, "model": model, "mode": mode, "agent": agent}

    def chat_send(self, chat_id: str, text: str) -> dict:
        self._record("chat_send", chat_id, text)
        return dict(self.chat_table[chat_id], status="running")

    def chat_cancel(self, chat_id: str) -> dict:
        self._record("chat_cancel", chat_id)
        return dict(self.chat_table[chat_id])

    def chat_delete(self, chat_id: str) -> None:
        self._record("chat_delete", chat_id)
        del self.chat_table[chat_id]

    def chat_handoff(self, chat_id: str, feature: str, delivery: str | None = None) -> dict:
        self._record("chat_handoff", chat_id, feature, *(() if delivery is None else (delivery,)))
        self.chat_table[chat_id]
        return job_dict(REPO, "004-from-chat", from_branch=f"chat/{chat_id}")

    # settings
    def config_view(self) -> dict:
        self._record("config_view")
        return self.view

    def update_config(self, patch: dict) -> dict:
        """Like the real one: validate, save, reload (repos are only checked for duplicate names here)."""
        self._record("update_config", patch)
        if "repos" in patch:
            names = [entry["name"] for entry in patch["repos"]]
            if len(set(names)) != len(names):
                raise ValueError("a repo is listed twice")
            self.config = Config(self.config.path, {**self.config.raw, "repos": patch["repos"]})
            self.config.save()
        return {**self.view, "patched": sorted(patch)}

    def apply_preset(self, name: str) -> dict:
        """Like settings.apply_preset: ValueError (400) for an unknown preset."""
        self._record("apply_preset", name)
        if name != "hybrid":
            raise ValueError(f"unknown preset {name!r} (available: hybrid)")
        return {**self.view, "preset": name}

    def test_model(self, key: str) -> dict:
        """Like settings.test_model: never raises, an unknown key is reported in the result."""
        self._record("test_model", key)
        if key not in ("local", "opus"):
            return {"ok": False, "latency_ms": None, "text": "", "error": f"unknown model {key!r}",
                    "tool_call": False, "tokens_per_second": None}
        return {"ok": True, "latency_ms": 12, "text": "OK", "error": None, "tool_call": True}

    # plans
    def plans(self) -> list[dict]:
        self._record("plans")
        return list(self.plan_table.values())

    def plan(self, repo: str, plan_id: str) -> dict:
        self._record("plan", repo, plan_id)
        return dict(self.plan_table[(repo, plan_id)])

    def create_plan(self, repo: str, request: str, routing: Any = None, delivery: str | None = None) -> dict:
        self._record("create_plan", repo, request, routing, *(() if delivery is None else (delivery,)))
        self._known_repo(repo)
        return {"id": "P002-big-change", "repo": repo, "stage": "planning", "request": request}

    def update_plan_text(self, repo: str, plan_id: str, text: str) -> dict:
        self._record("update_plan_text", repo, plan_id, text)
        return dict(self.plan_table[(repo, plan_id)], edited=True)

    def _plan_action(self, name: str, repo: str, plan_id: str) -> dict:
        self._record(name, repo, plan_id)
        return dict(self.plan_table[(repo, plan_id)], last_action=name)

    def run_plan(self, repo: str, plan_id: str) -> dict:
        return self._plan_action("run_plan", repo, plan_id)

    def cancel_plan(self, repo: str, plan_id: str) -> dict:
        return self._plan_action("cancel_plan", repo, plan_id)

    def retry_plan(self, repo: str, plan_id: str) -> dict:
        return self._plan_action("retry_plan", repo, plan_id)

    # setup, secrets and docs (the real envfile and docs code on this config)
    def setup_status(self) -> dict:
        self._record("setup_status")
        return {"configured": False, "steps": {"repos": {"ok": False, "detail": "no repos yet"}}}

    def secret_status(self) -> dict:
        from swfactory.envfile import secret_status

        self._record("secret_status")
        return secret_status(self.config)

    def set_secret(self, name: str, value: str) -> dict:
        from swfactory.envfile import set_secret

        self._record("set_secret", name)   # never the value
        return set_secret(self.config, name, value)

    def procedures(self) -> dict:
        from swfactory.docs import procedures

        self._record("procedures")
        return procedures(self.config, dict(self.agent_table))

    def procedures_html(self) -> str:
        from swfactory.docs import procedures_html

        self._record("procedures_html")
        return procedures_html(self.procedures())

    def maps(self) -> list[dict]:
        from swfactory.docs import mermaid_maps

        self._record("maps")
        return mermaid_maps(self.config)

    # product assumptions
    def assumptions(self, repo: str, job_id: str) -> dict:
        self._record("assumptions", repo, job_id)
        job = self.job_table[(repo, job_id)]
        confirmed = (job.get("confirmed") or {}).get("lines") or []
        marked = ["Free plan says Hello (product)", "Paid plan gets no greeting (product)"]
        return {"marked": marked, "confirmed": confirmed, "unconfirmed": [m for m in marked if m not in confirmed]}

    def confirm(self, repo: str, job_id: str, lines: list[str] | None = None) -> dict:
        self._record("confirm", repo, job_id, lines)
        job = self.job_table[(repo, job_id)]
        marked = self.assumptions(repo, job_id)["marked"]
        for line in lines or []:
            if line not in marked:
                raise ValueError(f"spec.md does not mark this line (product): {line!r}")
        job["confirmed"] = {"lines": list(lines) if lines is not None else marked, "at": "2026-10-21T10:00:00Z"}
        return dict(job)


class Response:
    def __init__(self, status: int, headers: http.client.HTTPMessage, body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))

    @property
    def text(self) -> str:
        return self.body.decode("utf-8")


class SseClient:
    """A raw-socket EventSource: sends the request, parses status, headers and message blocks."""

    def __init__(self, port: int, path: str = "/api/events", headers: dict[str, str] | None = None,
                 token: str | None = TOKEN) -> None:
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        lines = [f"GET {path} HTTP/1.1", f"Host: 127.0.0.1:{port}", "Accept: text/event-stream"]
        if token:
            lines.append(f"Authorization: Bearer {token}")
        lines += [f"{key}: {value}" for key, value in (headers or {}).items()]
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
        self.file = self.sock.makefile("rb")
        self.status = int(self.file.readline().split()[1])
        self.headers: dict[str, str] = {}
        while True:
            line = self.file.readline().decode("latin-1")
            if line in ("\r\n", ""):
                break
            key, _, value = line.partition(":")
            self.headers[key.strip().lower()] = value.strip()

    def block(self) -> list[str]:
        """The next message block (lines up to a blank line); [] at end of stream."""
        lines: list[str] = []
        while True:
            raw = self.file.readline()
            if not raw:
                return lines
            line = raw.decode("utf-8").rstrip("\n")
            if not line:
                if lines:
                    return lines
                continue
            lines.append(line)

    def event(self) -> dict:
        """The next real event (skipping pings and the retry hint) as {id, event, data}."""
        while True:
            lines = self.block()
            if not lines:
                raise EOFError("stream ended")
            fields = dict(line.split(": ", 1) for line in lines if not line.startswith(":") and ": " in line)
            if "data" in fields:
                return {"id": int(fields["id"]), "event": fields["event"], "data": json.loads(fields["data"])}

    def close(self) -> None:
        self.file.close()
        self.sock.close()


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class ServerTestCase(unittest.TestCase):
    """Starts a server on 127.0.0.1:0 with a FakeFactory and a temporary data dir and web dir."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sf-server-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.web = self.tmp / "web"
        self.web.mkdir()
        (self.web / "index.html").write_text("<!doctype html><title>Factory GUI</title>")
        (self.web / "login.html").write_text("<!doctype html><title>Sign in</title><form></form>")
        (self.web / "app.js").write_text("console.log('app');")
        (self.web / "style.css").write_text("body{}")
        (self.web / "logo.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
        (self.web / "notes.md").write_text("not served")
        (self.tmp / "secret.js").write_text("secret")
        os.symlink(self.tmp / "secret.js", self.web / "link.js")
        self.data = self.tmp / "data"
        self.config = Config(self.tmp / "config.json", {
            "server": {"token": TOKEN, "allowed_hosts": ["factory.example"]},
            "data_dir": str(self.data),
            "repos": [{"name": REPO, "path": str(self.tmp / "repo")}],
            "agents": {"*": {"driver": "builtin"}, "builder": {"model": "local", "driver": "qwen"}},
        })
        self.bus = self.make_bus()
        self.factory = FakeFactory(self.config, self.bus)
        self.server = self.start_server(self.config, self.factory, self.bus)
        self.port = self.server.server_port

    def make_bus(self) -> CountingBus:
        return CountingBus(secrets_provider=lambda: [TOKEN])

    def start_server(self, config: Config, factory: Any, bus: EventBus) -> srv.FactoryHTTPServer:
        server = srv.make_server(config, factory, bus, "127.0.0.1", 0, ping_interval=0.05, web_dir=self.web)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()

        def stop() -> None:
            server.shutdown()
            server.server_close()
            thread.join(5)
        self.addCleanup(stop)
        return server

    def request(self, method: str, path: str, body: Any = None, *, token: str | None = TOKEN,
                headers: dict[str, str] | None = None, content_type: str | None = "application/json",
                host: str | None = None, raw: bytes | None = None, port: int | None = None) -> Response:
        port = port or self.port
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            host = f"127.0.0.1:{port}" if host is None else host
            if host:
                conn.putheader("Host", host)
            if token:
                conn.putheader("Authorization", f"Bearer {token}")
            data = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
            if method == "POST" and content_type:
                conn.putheader("Content-Type", content_type)
            if data or method == "POST":
                conn.putheader("Content-Length", str(len(data)))
            for key, value in (headers or {}).items():
                conn.putheader(key, value)
            conn.endheaders(data or None)
            response = conn.getresponse()
            return Response(response.status, response.headers, response.read())
        finally:
            conn.close()

    def raw_http(self, data: bytes) -> bytes:
        """Send raw bytes, return everything the server answers until it closes the connection."""
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as sock:
            sock.sendall(data)
            chunks = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)

    def sse(self, path: str = "/api/events", **kwargs: Any) -> SseClient:
        client = SseClient(self.port, path, **kwargs)
        self.addCleanup(client.close)
        return client

    def assert_error(self, response: Response, status: int, fragment: str = "") -> None:
        self.assertEqual(response.status, status, response.body[:500])
        self.assertTrue(response.headers["Content-Type"].startswith("application/json"))
        self.assertIn(fragment, response.json()["error"])

    def make_job_dir(self) -> Path:
        store = JobStore(self.data)
        job = store.new_job(REPO, "First job", ["code"], 2, base="main")
        self.assertEqual(job["id"], JOB)
        return Path(job["job_dir"])


class AuthTest(ServerTestCase):
    def test_api_requires_a_token(self) -> None:
        self.assert_error(self.request("GET", "/api/state", token=None), 401, "authentication")
        self.assertEqual(self.factory.calls, [])

    def test_bearer_token(self) -> None:
        self.assertEqual(self.request("GET", "/api/state").status, 200)

    def test_bearer_scheme_is_case_insensitive(self) -> None:
        response = self.request("GET", "/api/state", token=None, headers={"Authorization": f"bearer {TOKEN}"})
        self.assertEqual(response.status, 200)

    def test_wrong_or_empty_bearer_token(self) -> None:
        self.assert_error(self.request("GET", "/api/state", token="f" * 32), 401)
        self.assert_error(self.request("GET", "/api/state", token=None, headers={"Authorization": "Bearer "}), 401)
        self.assert_error(self.request("GET", "/api/state", token=None,
                                       headers={"Authorization": f"Basic {TOKEN}"}), 401)

    def test_cookie_token(self) -> None:
        response = self.request("GET", "/api/state", token=None, headers={"Cookie": f"theme=dark; sf_token={TOKEN}"})
        self.assertEqual(response.status, 200)

    def test_wrong_or_malformed_cookie(self) -> None:
        for cookie in ("sf_token=nope", "sf_token", "=;;;=", f"other={TOKEN}", "sf_token="):
            with self.subTest(cookie=cookie):
                self.assert_error(self.request("GET", "/api/state", token=None, headers={"Cookie": cookie}), 401)

    def test_post_login_json_sets_cookie(self) -> None:
        response = self.request("POST", "/api/login", {"token": TOKEN}, token=None)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.json(), {"ok": True})
        cookie = response.headers["Set-Cookie"]
        self.assertEqual(cookie, f"sf_token={TOKEN}; HttpOnly; SameSite=Strict; Path=/")
        self.assertNotIn("Secure", cookie)
        follow = self.request("GET", "/api/state", token=None, headers={"Cookie": cookie.split(";")[0]})
        self.assertEqual(follow.status, 200)

    def test_post_login_wrong_token(self) -> None:
        for body in ({"token": "nope"}, {"token": 5}, {}):
            with self.subTest(body=body):
                response = self.request("POST", "/api/login", body, token=None)
                self.assert_error(response, 401, "invalid token")
                self.assertIsNone(response.headers["Set-Cookie"])

    def test_post_login_form_encoded(self) -> None:
        form = "application/x-www-form-urlencoded"
        ok = self.request("POST", "/api/login", raw=f"token={TOKEN}".encode(), token=None, content_type=form)
        self.assertEqual(ok.status, 303)
        self.assertEqual(ok.headers["Location"], "/")
        self.assertIn(f"sf_token={TOKEN}", ok.headers["Set-Cookie"])
        bad = self.request("POST", "/api/login", raw=b"token=wrong", token=None, content_type=form)
        self.assertEqual(bad.status, 401)
        self.assertIn("Sign in", bad.text)
        self.assertIsNone(bad.headers["Set-Cookie"])

    def test_post_login_needs_json_or_form(self) -> None:
        response = self.request("POST", "/api/login", raw=f"token={TOKEN}".encode(), token=None,
                                content_type="text/plain")
        self.assert_error(response, 415)

    def test_login_link(self) -> None:
        ok = self.request("GET", f"/login?token={TOKEN}", token=None)
        self.assertEqual(ok.status, 302)
        self.assertEqual(ok.headers["Location"], "/")
        self.assertEqual(ok.headers["Set-Cookie"], f"sf_token={TOKEN}; HttpOnly; SameSite=Strict; Path=/")
        bad = self.request("GET", "/login?token=wrong", token=None)
        self.assertEqual(bad.status, 401)
        self.assertIsNone(bad.headers["Set-Cookie"])
        page = self.request("GET", "/login", token=None)
        self.assertEqual(page.status, 200)
        self.assertIn("Sign in", page.text)
        self.assertNotIn(TOKEN, page.text)

    def test_unauthenticated_pages_get_the_login_page(self) -> None:
        for path in ("/", "/index.html", "/login.html"):
            with self.subTest(path=path):
                response = self.request("GET", path, token=None)
                self.assertEqual(response.status, 200)
                self.assertIn("<title>Sign in</title>", response.text)
                self.assertEqual(response.headers["Content-Security-Policy"], HTML_CSP)

    def test_assets_are_public_for_the_login_page(self) -> None:
        self.assertEqual(self.request("GET", "/style.css", token=None).body, b"body{}")

    def test_builtin_login_form_when_login_html_is_missing(self) -> None:
        (self.web / "login.html").unlink()
        response = self.request("GET", "/", token=None)
        self.assertEqual(response.status, 200)
        self.assertIn('<form method="post" action="/api/login">', response.text)
        self.assertNotIn("<script", response.text)

    def test_tokens_are_compared_in_constant_time(self) -> None:
        with mock.patch("hmac.compare_digest", wraps=hmac.compare_digest) as compare:
            self.assertEqual(self.request("GET", "/api/state").status, 200)
            self.assertEqual(self.request("POST", "/api/login", {"token": TOKEN}, token=None).status, 200)
        self.assertGreaterEqual(compare.call_count, 2)
        self.assertIn(TOKEN.encode(), compare.call_args_list[0].args)

    def test_server_refuses_to_start_without_a_token(self) -> None:
        config = Config(self.tmp / "none.json", {"data_dir": str(self.data)})
        with self.assertRaises(ValueError):
            srv.make_server(config, self.factory, self.bus, "127.0.0.1", 0)

    def test_access_log_never_shows_the_token(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.request("GET", f"/login?token={TOKEN}", token=None)
            self.request("GET", "/login?token=wrong", token=None)
        self.assertNotIn(TOKEN, stderr.getvalue())


class HostAndOriginTest(ServerTestCase):
    def test_foreign_host_is_refused_before_auth(self) -> None:
        self.assert_error(self.request("GET", "/api/state", host="evil.example"), 403, "host")
        self.assert_error(self.request("GET", "/", host="evil.example:8765", token=None), 403)
        self.assert_error(self.request("GET", f"/login?token={TOKEN}", host="evil.example", token=None), 403)
        self.assertEqual(self.factory.calls, [])

    def test_local_names_with_any_port(self) -> None:
        for host in ("localhost", "localhost:1", "LocalHost:9999", "127.0.0.1", "[::1]", "[::1]:8765"):
            with self.subTest(host=host):
                self.assertEqual(self.request("GET", "/api/state", host=host).status, 200)

    def test_allowed_hosts_and_configured_host(self) -> None:
        self.assertEqual(self.request("GET", "/api/state", host="factory.example:443").status, 200)
        config = Config(self.tmp / "other.json", {"server": {"token": TOKEN, "host": "box.lan",
                                                              "allowed_hosts": ["tunnel.example:8443", "::2"]},
                                                   "data_dir": str(self.data)})
        other = self.start_server(config, FakeFactory(config, self.bus), self.bus)
        for host in ("box.lan:8765", "tunnel.example", "[::2]:1"):
            with self.subTest(host=host):
                self.assertEqual(self.request("GET", "/api/state", host=host, port=other.server_port).status, 200)
        self.assert_error(self.request("GET", "/api/state", host="factory.example", port=other.server_port), 403)

    def test_missing_duplicate_or_malformed_host(self) -> None:
        self.assert_error(self.request("GET", "/api/state", host=""), 403)
        self.assert_error(self.request("GET", "/api/state", headers={"Host": "evil.example"}), 403)
        for host in ("localhost:abc", "localhost:99999", "[::1", "user@localhost", "localhost:"):
            with self.subTest(host=host):
                self.assert_error(self.request("GET", "/api/state", host=host), 403)

    def test_post_origin_must_match_host(self) -> None:
        body = {"repo": REPO, "feature": "x"}
        same = self.request("POST", "/api/jobs", body, headers={"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(same.status, 201)
        for origin in ("http://evil.example", "null", f"http://127.0.0.1:{self.port + 1}",
                       f"http://localhost:{self.port}", f"ftp://127.0.0.1:{self.port}", "garbage"):
            with self.subTest(origin=origin):
                self.assert_error(self.request("POST", "/api/jobs", body, headers={"Origin": origin}), 403, "origin")
        self.assertEqual(len(self.factory.called("submit")), 1)

    def test_origin_default_ports(self) -> None:
        body = {"repo": REPO, "feature": "x"}
        ok = self.request("POST", "/api/jobs", body, host="factory.example",
                          headers={"Origin": "https://factory.example"})
        self.assertEqual(ok.status, 201)
        self.assert_error(self.request("POST", "/api/jobs", body, host="factory.example",
                                       headers={"Origin": "http://factory.example:8080"}), 403)

    def test_login_post_checks_origin(self) -> None:
        response = self.request("POST", "/api/login", {"token": TOKEN}, token=None,
                                headers={"Origin": "http://evil.example"})
        self.assert_error(response, 403)
        self.assertIsNone(response.headers["Set-Cookie"])

    def test_get_requests_are_not_origin_checked(self) -> None:
        response = self.request("GET", "/api/state", headers={"Origin": "http://evil.example"})
        self.assertEqual(response.status, 200)

    def test_split_host(self) -> None:
        cases = {"LOCALHOST:80": ("localhost", 80), "[::1]:8765": ("[::1]", 8765), "::1": ("[::1]", None),
                 "example.com": ("example.com", None), "a:b": None, "[::1]x": None, "": None, "a b": None,
                 "host:65536": None, "host:٣": None}
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(srv.split_host(value), expected)


class BodyTest(ServerTestCase):
    def test_post_requires_json_content_type(self) -> None:
        body = {"repo": REPO, "feature": "x"}
        for content_type in (None, "text/plain", "application/x-www-form-urlencoded", "multipart/form-data"):
            with self.subTest(content_type=content_type):
                self.assert_error(self.request("POST", "/api/jobs", body, content_type=content_type), 415)
        ok = self.request("POST", "/api/jobs", body, content_type="Application/JSON; charset=utf-8")
        self.assertEqual(ok.status, 201)
        self.assertEqual(len(self.factory.called("submit")), 1)

    def test_body_over_one_megabyte_is_refused(self) -> None:
        answer = self.raw_http(
            b"POST /api/config HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer " + TOKEN.encode()
            + b"\r\nContent-Type: application/json\r\nContent-Length: 1048577\r\n\r\n{")
        self.assertTrue(answer.startswith(b"HTTP/1.1 413"), answer[:100])
        self.assertIn(b"Connection: close", answer)
        self.assertEqual(self.factory.called("update_config"), [])

    def test_body_of_exactly_one_megabyte_is_accepted(self) -> None:
        overhead = len(json.dumps({"pad": ""}))
        body = {"pad": "x" * (srv.MAX_BODY_BYTES - overhead)}
        self.assertEqual(len(json.dumps(body)), srv.MAX_BODY_BYTES)
        response = self.request("POST", "/api/config", body)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.json()["patched"], ["pad"])

    def test_invalid_bodies(self) -> None:
        for raw, fragment in ((b"{not json", "not valid JSON"), (b"[1, 2]", "JSON object"),
                              (b"\xff\xfe{}", "not valid JSON"), (b'"text"', "JSON object")):
            with self.subTest(raw=raw):
                self.assert_error(self.request("POST", "/api/jobs", raw=raw), 400, fragment)

    def test_invalid_content_length_and_chunked_bodies(self) -> None:
        head = b"POST /api/jobs HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer " + TOKEN.encode()
        head += b"\r\nContent-Type: application/json\r\n"
        bad_length = self.raw_http(head + b"Content-Length: abc\r\n\r\n")
        self.assertTrue(bad_length.startswith(b"HTTP/1.1 400"), bad_length[:100])
        chunked = self.raw_http(head + b"Transfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n")
        self.assertTrue(chunked.startswith(b"HTTP/1.1 411"), chunked[:100])
        self.assertEqual(self.factory.called("submit"), [])

    def test_empty_body_counts_as_an_empty_object(self) -> None:
        response = self.request("POST", f"/api/jobs/{REPO}/{JOB}/approve")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.json()["job"]["last_action"], "approve")

    def test_wrong_field_types(self) -> None:
        cases = [({"repo": REPO, "feature": 5}, "feature"), ({"repo": REPO, "feature": "  "}, "feature"),
                 ({"repo": 1, "feature": "x"}, "repo"), ({"repo": REPO, "feature": "x", "routing": [1]}, "routing"),
                 ({"repo": REPO, "feature": "x", "routing": ""}, "routing")]
        for body, fragment in cases:
            with self.subTest(body=body):
                self.assert_error(self.request("POST", "/api/jobs", body), 400, fragment)
        self.assertEqual(self.factory.called("submit"), [])


class RoutingTest(ServerTestCase):
    def test_unknown_routes(self) -> None:
        self.assert_error(self.request("GET", "/api/nothing"), 404, "no such API route")
        self.assert_error(self.request("GET", f"/api/jobs/{REPO}/{JOB}/explode"), 404)
        self.assert_error(self.request("POST", f"/api/jobs/{REPO}/{JOB}/explode", {}), 404)
        self.assert_error(self.request("GET", "/nothing.html"), 404)
        self.assert_error(self.request("GET", "/no/such/page"), 404)

    def test_wrong_method(self) -> None:
        response = self.request("GET", f"/api/jobs/{REPO}/{JOB}/approve")
        self.assert_error(response, 405)
        self.assertEqual(response.headers["Allow"], "POST")
        self.assertEqual(self.request("POST", "/api/state", {}).headers["Allow"], "GET")
        self.assertEqual(self.request("POST", "/api/config", {}).status, 200)
        self.assert_error(self.request("POST", "/login", {}, token=None), 405)
        self.assert_error(self.request("GET", "/api/login", token=None), 405)
        self.assert_error(self.request("POST", "/", {}), 405)
        for method in ("PUT", "DELETE", "PATCH", "OPTIONS"):
            with self.subTest(method=method):
                self.assert_error(self.request(method, "/api/state"), 405)
        head = self.request("HEAD", "/api/state")
        self.assertEqual((head.status, head.body), (405, b""))

    def test_keep_alive_serves_several_requests_on_one_connection(self) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(conn.close)
        for _ in range(3):
            conn.request("GET", "/api/doctor", headers={"Authorization": f"Bearer {TOKEN}"})
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), json.dumps({"checks": self.factory.doctor()}).encode())

    def test_malformed_request_line_gets_json_error(self) -> None:
        answer = self.raw_http(b"GET /a b HTTP/1.1\r\nHost: localhost\r\n\r\n")
        self.assertTrue(answer.startswith(b"HTTP/1.1 400"), answer[:80])
        self.assertIn(b"X-Content-Type-Options: nosniff", answer)
        self.assertIn(b'{"error":', answer)

    def test_concurrent_requests(self) -> None:
        results: list[int] = []
        lock = threading.Lock()

        def worker(n: int) -> None:
            for i in range(8):
                if i % 2:
                    status = self.request("POST", "/api/jobs", {"repo": REPO, "feature": f"job {n}.{i}"}).status
                else:
                    status = self.request("GET", "/api/state").status
                with lock:
                    results.append(status)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        self.assertEqual(sorted(set(results)), [200, 201])
        self.assertEqual(len(results), 96)
        self.assertEqual(len(self.factory.called("submit")), 48)


class ErrorMappingTest(ServerTestCase):
    def test_key_error_is_404(self) -> None:
        self.factory.fail["doctor"] = KeyError("unknown repo 'x'")
        self.assert_error(self.request("GET", "/api/doctor"), 404, "not found: unknown repo 'x'")

    def test_action_error_is_409_by_class_name(self) -> None:
        self.factory.fail["approve"] = ActionError("job is merging")
        self.assert_error(self.request("POST", f"/api/jobs/{REPO}/{JOB}/approve", {}), 409, "job is merging")
        self.factory.fail["retry"] = StageError("not failed")
        self.assert_error(self.request("POST", f"/api/jobs/{REPO}/{JOB}/retry", {}), 409, "not failed")

    def test_value_error_is_400(self) -> None:
        self.factory.fail["update_config"] = ValueError("models.opus has no provider")
        self.assert_error(self.request("POST", "/api/config", {"models": {}}), 400, "models.opus has no provider")

    def test_file_errors_do_not_leak_paths(self) -> None:
        self.factory.fail["doctor"] = FileNotFoundError(2, "No such file", "/home/me/.secret/key")
        response = self.request("GET", "/api/doctor")
        self.assert_error(response, 404, "not found")
        self.assertNotIn(b"secret", response.body)

    def test_unexpected_errors_are_500_and_logged_without_secrets(self) -> None:
        events = self.bus.subscribe()
        self.factory.fail["doctor"] = RuntimeError(f"boom at /etc/ai-stack/strata.env with {TOKEN}")
        response = self.request("GET", "/api/doctor")
        self.assert_error(response, 500)
        self.assertEqual(response.json(), {"error": "internal server error"})
        self.assertNotIn(b"strata.env", response.body)
        event = events.get(timeout=2)
        self.assertEqual((event.type, event.data["level"]), ("log", "error"))
        self.assertIn("GET /api/doctor", event.data["message"])
        self.assertIn("RuntimeError", event.data["traceback"])
        self.assertNotIn(TOKEN, json.dumps(event.to_dict()))

    def test_missing_factory_method_is_501(self) -> None:
        self.server.factory = object()
        self.assert_error(self.request("GET", "/api/doctor"), 501, "doctor")

    def test_error_status_table(self) -> None:
        self.assertEqual(srv.error_status(ActionError()), 409)
        self.assertEqual(srv.error_status(KeyError("x")), 404)
        self.assertEqual(srv.error_status(IsADirectoryError()), 404)
        self.assertEqual(srv.error_status(ValueError()), 400)
        self.assertEqual(srv.error_status(UnicodeDecodeError("utf-8", b"", 0, 1, "x")), 400)
        self.assertEqual(srv.error_status(PermissionError()), 500)
        self.assertEqual(srv.error_status(TypeError()), 500)


class StateAndJobRoutesTest(ServerTestCase):
    def test_state(self) -> None:
        state = self.request("GET", "/api/state").json()
        self.assertEqual(set(state), {"version", "repos", "jobs", "pipeline", "models", "drivers", "agents"})
        self.assertEqual(state["version"], srv.__version__)
        self.assertEqual(state["repos"], [{"name": REPO, "path": str(self.tmp / "repo"), "base": None}])
        self.assertEqual(state["jobs"], [job_dict(REPO, JOB)])
        self.assertEqual(state["pipeline"]["reviewers"], ["security", "ux", "ui", "code"])
        self.assertEqual(state["models"]["opus"], {"provider": "deepseek", "model": "deepseek-v4-pro"})
        self.assertEqual(state["models"]["local"], {"provider": "strata", "model": "strata"})
        self.assertIn("builtin", state["drivers"])
        self.assertEqual([agent["name"] for agent in state["agents"]], ["approver", "builder", "reviewer-code"])
        builder = state["agents"][1]
        self.assertEqual(builder, {"name": "builder", "description": "The builder.", "model": "local",
                                   "tier": "opus", "driver": "qwen", "tools": ["Read", "Write"], "color": "cyan"})
        self.assertEqual((state["agents"][2]["model"], state["agents"][2]["driver"]), ("sonnet", "builtin"))

    def test_state_survives_a_malformed_agent_file(self) -> None:
        self.factory.fail["agents"] = ValueError("agents/x.md: missing frontmatter")
        state = self.request("GET", "/api/state").json()
        self.assertEqual((state["agents"], state["agents_error"]), ([], "agents/x.md: missing frontmatter"))
        self.assertEqual(state["jobs"], [job_dict(REPO, JOB)])

    def test_state_follows_a_reloaded_factory_config(self) -> None:
        self.factory.config = Config(self.tmp / "config.json", {
            "server": {"token": TOKEN}, "data_dir": str(self.data), "repos": [], "pipeline": {"reviewers": ["perf"]}})
        state = self.request("GET", "/api/state").json()
        self.assertEqual((state["repos"], state["pipeline"]["reviewers"]), ([], ["perf"]))

    def test_agent_summary_falls_back_to_the_tier(self) -> None:
        config = Config(self.tmp / "x.json", {"models": {"opus": None}, "agents": {"builder": {"model": "nope"}}})
        summary = srv._agent_summary(config, agent_def("builder", "opus"))
        self.assertEqual(summary["model"], "opus")

    def test_job_detail(self) -> None:
        response = self.request("GET", f"/api/jobs/{REPO}/{JOB}")
        self.assertEqual(response.json(), {"job": job_dict(REPO, JOB), "files": ["feature.md", "job.json"]})
        self.assert_error(self.request("GET", f"/api/jobs/{REPO}/002-missing"), 404)

    def test_submit(self) -> None:
        response = self.request("POST", "/api/jobs", {"repo": REPO, "feature": "Add a footer"})
        self.assertEqual(response.status, 201)
        self.assertEqual(response.json()["job"]["feature"], "Add a footer")
        self.assertEqual(self.factory.calls[-1], ("submit", (REPO, "Add a footer"), {}))
        routing = {"builder": {"model": "local", "driver": "builtin"}}
        self.request("POST", "/api/jobs", {"repo": REPO, "feature": "Again", "routing": routing})
        self.assertEqual(self.factory.calls[-1], ("submit", (REPO, "Again"), {"routing": routing}))
        self.request("POST", "/api/jobs", {"repo": REPO, "feature": "Preset", "routing": "hybrid"})
        self.assertEqual(self.factory.calls[-1][2], {"routing": "hybrid"})
        self.request("POST", "/api/jobs", {"repo": REPO, "feature": "Folder", "delivery": "folder"})
        self.assertEqual(self.factory.calls[-1][2], {"delivery": "folder"})
        self.request("POST", "/api/jobs", {"repo": REPO, "feature": "Default", "delivery": None})
        self.assertEqual(self.factory.calls[-1][2], {}, "null or absent: the config's merge.delivery")
        self.assert_error(self.request("POST", "/api/jobs", {"repo": REPO, "feature": "x", "delivery": 3}),
                          400, "delivery")

    def test_submit_errors(self) -> None:
        self.assert_error(self.request("POST", "/api/jobs", {"repo": "other", "feature": "x"}), 404, "unknown repo")
        self.assert_error(self.request("POST", "/api/jobs", {"repo": "../x", "feature": "x"}), 400, "repo name")
        self.assert_error(self.request("POST", "/api/jobs", {"feature": "x"}), 400, "repo")
        self.assert_error(self.request("POST", "/api/jobs", {"repo": REPO}), 400, "feature")

    def test_job_actions(self) -> None:
        base = f"/api/jobs/{REPO}/{JOB}"
        for action in ("approve", "retry", "cancel"):
            with self.subTest(action=action):
                response = self.request("POST", f"{base}/{action}", {})
                self.assertEqual(response.status, 200)
                self.assertEqual(response.json()["job"]["last_action"], action)
                self.assertEqual(self.factory.called(action), [(REPO, JOB)])
        self.request("POST", f"{base}/rework", {"note": "Use a constant."})
        self.assertEqual(self.factory.called("rework"), [(REPO, JOB, "Use a constant.")])
        self.request("POST", f"{base}/reject", {"reason": "Not needed"})
        self.request("POST", f"{base}/reject", {})
        self.assertEqual(self.factory.called("reject"), [(REPO, JOB, "Not needed"), (REPO, JOB, "")])
        routing = {"approver": {"model": "opus"}}
        response = self.request("POST", f"{base}/routing", {"routing": routing})
        self.assertEqual(response.json()["job"]["last_action"], "set_routing")
        self.assertEqual(self.factory.called("set_routing"), [(REPO, JOB, routing)])
        self.request("POST", f"{base}/routing", {"routing": None})
        self.assertEqual(self.factory.called("set_routing")[-1], (REPO, JOB, None), "null: the saved config")

    def test_job_action_errors(self) -> None:
        base = f"/api/jobs/{REPO}/{JOB}"
        self.assert_error(self.request("POST", f"{base}/rework", {}), 400, "note")
        self.assert_error(self.request("POST", f"{base}/reject", {"reason": 3}), 400, "reason")
        self.assert_error(self.request("POST", f"{base}/routing", {}), 400, "routing")
        self.assert_error(self.request("POST", f"/api/jobs/{REPO}/009-unknown/approve", {}), 404)
        self.factory.fail["set_routing"] = ActionError("job is running")
        self.assert_error(self.request("POST", f"{base}/routing", {"routing": "hybrid"}), 409, "running")

    def test_doctor(self) -> None:
        response = self.request("GET", "/api/doctor")
        self.assertEqual(response.json(), {"checks": [{"name": "git", "ok": True, "detail": "2.43",
                                                       "level": "info"}]})


class JobFilesTest(ServerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.job_dir = self.make_job_dir()
        (self.job_dir / "spec.md").write_text("# Spec\n\nDo it. ✓\n")
        (self.job_dir / "round-1").mkdir()
        (self.job_dir / "round-1" / "review-code.md").write_text("VERDICT: PASS\n")
        (self.job_dir / "logs").mkdir()
        self.log = self.job_dir / "logs" / "01-builder.jsonl"

    def file_url(self, path: str) -> str:
        return f"/api/jobs/{REPO}/{JOB}/file?path={path}"

    def log_url(self, file: str = "logs/01-builder.jsonl", offset: int | str = 0) -> str:
        return f"/api/jobs/{REPO}/{JOB}/log?file={file}&offset={offset}"

    def test_read_job_files(self) -> None:
        response = self.request("GET", self.file_url("spec.md"))
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Content-Type"], "text/plain; charset=utf-8")
        self.assertEqual(response.text, "# Spec\n\nDo it. ✓\n")
        self.assertEqual(self.request("GET", self.file_url("round-1/review-code.md")).text, "VERDICT: PASS\n")
        self.assertEqual(self.request("GET", self.file_url("round-1%2Freview-code.md")).text, "VERDICT: PASS\n")

    def test_job_file_errors(self) -> None:
        self.assert_error(self.request("GET", self.file_url("missing.md")), 404)
        self.assert_error(self.request("GET", self.file_url("round-1")), 404)
        self.assert_error(self.request("GET", f"/api/jobs/{REPO}/{JOB}/file"), 400, "path")
        self.assert_error(self.request("GET", f"/api/jobs/{REPO}/002-missing/file?path=spec.md"), 404)

    def test_job_file_traversal(self) -> None:
        (self.tmp / "outside.txt").write_text("outside")
        os.symlink(self.tmp / "outside.txt", self.job_dir / "escape.md")
        for path in ("../../../config.json", "..%2F..%2Fconfig.json", "%2Fetc%2Fpasswd", "/etc/passwd",
                     "round-1/../../" + JOB + "/spec.md", "escape.md", "spec.md%00.txt"):
            with self.subTest(path=path):
                response = self.request("GET", self.file_url(path))
                self.assertIn(response.status, (400, 404))
                self.assertNotIn(b"outside", response.body)
                self.assertNotIn(b"root:", response.body)

    def test_log_reads_complete_lines_incrementally(self) -> None:
        events = [{"seq": n, "type": "agent.text", "data": {"text": f"line {n}"}} for n in range(3)]
        first = "".join(json.dumps(event) + "\n" for event in events)
        with open(self.log, "w") as fh:
            fh.write(first + '{"seq": 3, "type": "agent.te')
        response = self.request("GET", self.log_url()).json()
        self.assertEqual(response["lines"], events)
        self.assertEqual(response["offset"], len(first.encode()))
        self.assertFalse(response["more"])
        again = self.request("GET", self.log_url(offset=response["offset"])).json()
        self.assertEqual((again["lines"], again["offset"]), ([], response["offset"]))
        with open(self.log, "a") as fh:
            fh.write('xt"}\nnot json\n{"seq": 4}\n')
        rest = self.request("GET", self.log_url(offset=response["offset"])).json()
        self.assertEqual(rest["lines"], [{"seq": 3, "type": "agent.text"}, {"seq": 4}])
        self.assertEqual(rest["offset"], self.log.stat().st_size)

    def test_log_caps_lines_per_call(self) -> None:
        self.log.write_text("".join(json.dumps({"n": n}) + "\n" for n in range(2500)))
        first = self.request("GET", self.log_url()).json()
        self.assertEqual((len(first["lines"]), first["lines"][-1], first["more"]), (2000, {"n": 1999}, True))
        second = self.request("GET", self.log_url(offset=first["offset"])).json()
        self.assertEqual((len(second["lines"]), second["lines"][0], second["more"]), (500, {"n": 2000}, False))
        self.assertEqual(second["offset"], self.log.stat().st_size)

    def test_log_skips_a_line_longer_than_the_read_budget(self) -> None:
        self.log.write_text("x" * 100 + "\n" + '{"ok": 1}\n')
        with mock.patch.object(srv, "MAX_LOG_BYTES", 64):
            first = srv.read_log(self.log, 0)
            self.assertEqual((first["lines"], first["offset"], first["more"]), ([], 64, True))
            second = srv.read_log(self.log, first["offset"])
        self.assertEqual(second["lines"], [{"ok": 1}])  # the rest of the long line is not JSON: skipped
        self.assertEqual(second["offset"], self.log.stat().st_size)

    def test_log_errors_and_traversal(self) -> None:
        self.log.write_text('{"a": 1}\n')
        (self.tmp / "outside.jsonl").write_text('{"secret": 1}\n')
        os.symlink(self.tmp / "outside.jsonl", self.job_dir / "logs" / "02-evil.jsonl")
        for file in ("job.json", "logs/../job.json", "logs%2F..%2Fjob.json", "logs/x.txt", "logs/sub/x.jsonl",
                     "../logs/01-builder.jsonl", "/etc/passwd", "logs/.hidden.jsonl", ""):
            with self.subTest(file=file):
                self.assert_error(self.request("GET", self.log_url(file)), 400, "logs/")
        self.assert_error(self.request("GET", self.log_url("logs/09-missing.jsonl")), 404)
        self.assert_error(self.request("GET", self.log_url("logs/02-evil.jsonl")), 404)
        for offset in ("-1", "abc", "1.5"):
            with self.subTest(offset=offset):
                self.assert_error(self.request("GET", self.log_url(offset=offset)), 400, "offset")
        self.assert_error(self.request("GET", f"/api/jobs/{REPO}/002-missing/log?file=logs/01-builder.jsonl"), 404)

    def test_log_folder_symlink_is_refused(self) -> None:
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "01-x.jsonl").write_text('{"secret": 1}\n')
        shutil.rmtree(self.job_dir / "logs")
        os.symlink(elsewhere, self.job_dir / "logs")
        self.assert_error(self.request("GET", self.log_url("logs/01-x.jsonl")), 404)


class DiffTest(ServerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = make_repo(self.tmp, "repo")
        run_git(self.repo, "checkout", "-q", "-b", f"factory/{JOB}")
        commit_file(self.repo, "feature.txt", "hello\n", "add feature")
        run_git(self.repo, "checkout", "-q", "main")

    def diff(self) -> dict:
        response = self.request("GET", f"/api/jobs/{REPO}/{JOB}/diff")
        self.assertEqual(response.status, 200)
        return response.json()

    def test_diff_of_the_job_branch(self) -> None:
        result = self.diff()
        self.assertIn("feature.txt", result["stat"])
        self.assertIn("+hello", result["diff"])
        self.assertFalse(result["truncated"])
        self.assertNotIn("error", result)

    def test_diff_is_truncated(self) -> None:
        with mock.patch.object(srv, "MAX_DIFF_BYTES", 40):
            result = self.diff()
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["diff"]), 40)

    def test_diff_of_a_merged_job_after_its_branch_is_gone(self) -> None:
        run_git(self.repo, "merge", "--no-ff", "-q", "-m", "merge job", f"factory/{JOB}")
        run_git(self.repo, "branch", "-D", f"factory/{JOB}")
        merged = run_git(self.repo, "rev-parse", "HEAD").strip()
        self.factory.job_table[(REPO, JOB)]["merged_commit"] = merged
        result = self.diff()
        self.assertIn("+hello", result["diff"])
        self.assertIn("feature.txt", result["stat"])

    def test_missing_branch_reports_an_error(self) -> None:
        self.factory.job_table[(REPO, JOB)]["branch"] = "factory/999-gone"
        result = self.diff()
        self.assertEqual((result["stat"], result["diff"], result["truncated"]), ("", "", False))
        self.assertTrue(result["error"])

    def test_job_without_base_yet(self) -> None:
        self.factory.job_table[(REPO, JOB)]["base"] = None
        self.assertEqual(self.diff(), {"stat": "", "diff": "", "truncated": False})

    def test_unknown_job(self) -> None:
        self.assert_error(self.request("GET", f"/api/jobs/{REPO}/002-missing/diff"), 404)


class RepoRoutesTest(ServerTestCase):
    def test_backlog(self) -> None:
        response = self.request("GET", f"/api/repos/{REPO}/backlog")
        self.assertEqual(response.json(), {"items": [{"text": "Add a footer", "done": False, "job": None}]})
        added = self.request("POST", f"/api/repos/{REPO}/backlog", {"text": "Dark mode"})
        self.assertEqual(added.json()["items"][-1]["text"], "Dark mode")
        self.assert_error(self.request("POST", f"/api/repos/{REPO}/backlog", {}), 400, "text")
        self.assert_error(self.request("GET", "/api/repos/unknown/backlog"), 404)

    def test_next(self) -> None:
        response = self.request("POST", f"/api/repos/{REPO}/next", {})
        self.assertEqual(response.json(), {"job": job_dict(REPO, "003-add-footer", stage="queued")})
        self.factory.next_job = None
        self.assert_error(self.request("POST", f"/api/repos/{REPO}/next"), 409, "no unchecked items")

    def test_add_repo(self) -> None:
        events = self.bus.subscribe()
        path = self.tmp / "Other App"
        response = self.request("POST", "/api/repos", {"path": str(path), "name": "other"})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.json()["repo"], {"name": "other", "path": str(path), "base": None,
                                                   "setup": None, "escalate_paths": []})
        repos = self.factory.called("update_config")[0][0]["repos"]
        self.assertEqual([entry["name"] for entry in repos], [REPO, "other"])
        self.assertEqual([repo.name for repo in Config.load(self.tmp / "config.json").repos()], [REPO, "other"])
        self.assertEqual(events.get(timeout=2).type, "repos.updated")
        state = self.request("GET", "/api/state").json()
        self.assertEqual([repo["name"] for repo in state["repos"]], [REPO, "other"])

    def test_add_repo_derives_the_name_from_the_folder(self) -> None:
        response = self.request("POST", "/api/repos", {"path": str(self.tmp / "My_App.v2")})
        self.assertEqual(response.json()["repo"]["name"], "my_app.v2")

    def test_add_repo_errors(self) -> None:
        self.factory.fail["update_config"] = ValueError("repos.x.path /tmp/x is not a git repository")
        self.assert_error(self.request("POST", "/api/repos", {"path": "/tmp/x"}), 400, "not a git repository")
        del self.factory.fail["update_config"]
        self.assert_error(self.request("POST", "/api/repos", {"path": "/tmp/x", "name": REPO}), 400, "twice")
        self.assert_error(self.request("POST", "/api/repos", {}), 400, "path")
        self.assert_error(self.request("POST", "/api/repos", {"path": "/tmp/x", "name": "Bad Name"}), 400,
                          "repo name")
        self.assert_error(self.request("POST", "/api/repos", {"path": "/"}), 400, "pass a name")

    def test_scout(self) -> None:
        response = self.request("POST", f"/api/repos/{REPO}/scout", {})
        self.assertEqual(response.json(), {"started": True})
        self.assertEqual(self.factory.called("scout"), [(REPO,)])
        status = self.request("GET", f"/api/repos/{REPO}/scout")
        self.assertEqual(status.json(), {"draft": "# AGENTS.md draft", "running": False})
        self.factory.fail["scout"] = ActionError("scout already running")
        self.assert_error(self.request("POST", f"/api/repos/{REPO}/scout"), 409, "already running")


class AgentRoutesTest(ServerTestCase):
    def setUp(self) -> None:
        super().setUp()
        # User overrides live in ~/.config/software-factory/agents whatever the config path: HOME points
        # into the temp dir so the tests never touch the real one.
        home_patch = mock.patch.dict(os.environ, {"HOME": str(self.tmp / "home")})
        home_patch.start()
        self.addCleanup(home_patch.stop)

    def test_agent_detail(self) -> None:
        detail = self.request("GET", "/api/agents/builder").json()
        self.assertEqual((detail["name"], detail["source"], detail["body"]), ("builder", "builtin", "You are builder."))
        self.assert_error(self.request("GET", "/api/agents/nobody"), 404)

    def test_save_agent(self) -> None:
        text = "---\nname: builder\n---\nBuild."
        response = self.request("POST", "/api/agents/builder", {"text": text})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.json()["agent"]["path"], "/user/builder.md")
        self.assertEqual(self.factory.called("save_agent"), [("builder", text)])
        self.assert_error(self.request("POST", "/api/agents/builder", {"text": "no frontmatter"}), 400, "frontmatter")
        self.assert_error(self.request("POST", "/api/agents/builder", {}), 400, "text")

    def test_new_agent(self) -> None:
        response = self.request("POST", "/api/agents", {"name": "reviewer-perf", "from": "builder"})
        self.assertEqual(response.status, 201)
        self.assertEqual(response.json()["agent"]["name"], "reviewer-perf")
        self.request("POST", "/api/agents", {"name": "reviewer-docs"})
        self.assertEqual(self.factory.called("new_agent"),
                         [("reviewer-perf", "builder"), ("reviewer-docs", "reviewer-code")])
        self.assert_error(self.request("POST", "/api/agents", {"name": "x", "from": "nobody"}), 400, "unknown agent")
        self.assert_error(self.request("POST", "/api/agents", {"name": "builder"}), 400, "already exists")

    def user_file(self, name: str) -> Path:
        path = user_agents_dir(self.config) / f"{name}.md"
        self.assertTrue(str(path).startswith(str(self.tmp / "home")))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\nname: {name}\ndescription: Mine.\n---\nBody.\n")
        return path

    def test_reset_agent(self) -> None:
        override = self.user_file("builder")
        response = self.request("POST", "/api/agents/builder/reset", {})
        self.assertEqual((response.status, response.json()), (200, {"agent": {"name": "builder", "source": "builtin"}}))
        self.assertFalse(override.exists())
        self.assert_error(self.request("POST", "/api/agents/builder/reset", {}), 404, "no user override")

    def test_reset_of_an_agent_that_only_exists_as_a_user_file(self) -> None:
        """The Factory deletes the file and returns None as no agent remains: the reset still worked."""
        user_only = self.user_file("reviewer-perf")
        response = self.request("POST", "/api/agents/reviewer-perf/reset", {})
        self.assertEqual((response.status, response.json()), (200, {"agent": None}))
        self.assertFalse(user_only.exists())
        self.assert_error(self.request("POST", "/api/agents/reviewer-perf/reset", {}), 404, "no user override")

    def test_reset_keeps_404_when_the_override_was_not_deleted(self) -> None:
        override = self.user_file("approver")
        self.factory.fail["reset_agent"] = KeyError("agent 'approver' is locked")
        self.assert_error(self.request("POST", "/api/agents/approver/reset", {}), 404, "locked")
        self.assertTrue(override.exists())

    def test_reset_agent_returning_none_means_nothing_remains(self) -> None:
        with mock.patch.object(self.factory, "reset_agent", return_value=None):
            response = self.request("POST", "/api/agents/reviewer-perf/reset", {})
        self.assertEqual((response.status, response.json()), (200, {"agent": None}))

    def test_writable_agent_names(self) -> None:
        for name in ("Builder", "a_b", "x" * 42, "-x", "a.b"):
            with self.subTest(name=name):
                self.assert_error(self.request("POST", "/api/agents", {"name": name}), 400, "agent name")
                self.assert_error(self.request("POST", f"/api/agents/{name}", {"text": "---\n---\n"}), 400)
                self.assert_error(self.request("POST", f"/api/agents/{name}/reset", {}), 400)
        self.assertEqual(self.factory.called("new_agent") + self.factory.called("save_agent"), [])


class ChatRoutesTest(ServerTestCase):
    def test_chat_list_and_detail(self) -> None:
        self.assertEqual(self.request("GET", "/api/chats").json(), {"chats": [self.factory.chat_table[CHAT]]})
        detail = self.request("GET", f"/api/chats/{CHAT}").json()
        self.assertEqual(detail, {"chat": self.factory.chat_table[CHAT],
                                  "messages": [{"role": "user", "text": "hi"}]})
        self.assert_error(self.request("GET", "/api/chats/c-ffffffffff"), 404)

    def test_new_chat(self) -> None:
        response = self.request("POST", "/api/chats", {"repo": REPO, "model": "local", "mode": "repo"})
        self.assertEqual(response.status, 201)
        self.assertEqual(response.json()["chat"]["mode"], "repo")
        self.request("POST", "/api/chats", {"repo": REPO, "model": "", "agent": ""})
        self.request("POST", "/api/chats", {"repo": REPO, "agent": "builder"})
        self.assertEqual([(args, kwargs) for name, args, kwargs in self.factory.calls if name == "new_chat"],
                         [((REPO, "local", "repo"), {"agent": "coder"}), ((REPO, None, "worktree"), {"agent": "coder"}),
                          ((REPO, None, "worktree"), {"agent": "builder"})])
        self.assert_error(self.request("POST", "/api/chats", {"repo": REPO, "mode": "yolo"}), 400, "mode")
        self.assert_error(self.request("POST", "/api/chats", {"repo": "nope"}), 404)
        self.assert_error(self.request("POST", "/api/chats", {"repo": REPO, "agent": "../x"}), 400, "agent")

    def test_chat_actions(self) -> None:
        sent = self.request("POST", f"/api/chats/{CHAT}/messages", {"text": "add tests"})
        self.assertEqual((sent.status, sent.json()["chat"]["status"]), (202, "running"))
        self.assertEqual(self.factory.called("chat_send"), [(CHAT, "add tests")])
        self.assertEqual(self.request("POST", f"/api/chats/{CHAT}/cancel", {}).json()["chat"]["id"], CHAT)
        handoff = self.request("POST", f"/api/chats/{CHAT}/handoff", {"feature": "Ship it"})
        self.assertEqual(handoff.json()["job"]["from_branch"], f"chat/{CHAT}")
        self.request("POST", f"/api/chats/{CHAT}/handoff", {"feature": "As a folder", "delivery": "folder"})
        self.assertEqual(self.factory.called("chat_handoff"), [(CHAT, "Ship it"), (CHAT, "As a folder", "folder")])
        self.assertEqual(self.request("POST", f"/api/chats/{CHAT}/delete", {}).json(), {"ok": True})
        self.assert_error(self.request("POST", f"/api/chats/{CHAT}/delete", {}), 404)

    def test_chat_action_errors(self) -> None:
        self.assert_error(self.request("POST", f"/api/chats/{CHAT}/messages", {"text": ""}), 400, "text")
        self.assert_error(self.request("POST", f"/api/chats/{CHAT}/handoff", {}), 400, "feature")
        self.factory.fail["chat_send"] = ActionError("a turn is already running")
        self.assert_error(self.request("POST", f"/api/chats/{CHAT}/messages", {"text": "x"}), 409, "running")
        self.assert_error(self.request("POST", f"/api/chats/{CHAT}/explode", {}), 404)


class SetupSecretsDocsTest(ServerTestCase):
    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.dict(os.environ, {})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("DEEPSEEK_API_KEY", None)
        self.env_file = self.tmp / "env"

    def test_setup_status(self) -> None:
        response = self.request("GET", "/api/setup")
        self.assertEqual(response.json(), {"configured": False,
                                           "steps": {"repos": {"ok": False, "detail": "no repos yet"}}})
        self.assert_error(self.request("POST", "/api/setup", {}), 405)
        self.assert_error(self.request("GET", "/api/setup", token=None), 401)

    def test_secrets_are_written_and_listed_by_name_only(self) -> None:
        status = self.request("GET", "/api/secrets").json()
        self.assertEqual(status["env_file"], str(self.env_file))
        deepseek = next(entry for entry in status["secrets"] if entry["name"] == "DEEPSEEK_API_KEY")
        self.assertEqual(deepseek, {"name": "DEEPSEEK_API_KEY", "set": False, "in_env_file": False,
                                    "providers": ["deepseek", "deepseek-anthropic"]})
        value = "sk-test-0123456789abcdef"
        response = self.request("POST", "/api/secrets", {"name": "DEEPSEEK_API_KEY", "value": value})
        self.assertEqual(response.status, 200)
        self.assertNotIn(value.encode(), response.body, "the value is never echoed")
        self.assertEqual(response.json()["secret"], {"name": "DEEPSEEK_API_KEY", "set": True, "in_env_file": True,
                                                     "providers": ["deepseek", "deepseek-anthropic"]})
        self.assertEqual(os.environ["DEEPSEEK_API_KEY"], value)
        self.assertIn(f"DEEPSEEK_API_KEY={value}\n", self.env_file.read_text())
        self.assertEqual(self.env_file.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(value.encode(), self.request("GET", "/api/secrets").body)
        self.assertEqual(self.factory.called("set_secret"), [("DEEPSEEK_API_KEY",)])

    def test_secret_errors_never_show_the_value(self) -> None:
        value = "sk-bad value with 'quote'"
        for body, status, message in (
                ({"name": "deepseek", "value": "x"}, 400, "name must match"),
                ({"name": "HOME_DIR", "value": "x"}, 400, "does not look like a credential"),
                ({"name": "OPENAI_API_KEY", "value": value}, 400, "single quote"),
                ({"name": "OPENAI_API_KEY", "value": "a\nb"}, 400, "single line"),
                ({"name": "OPENAI_API_KEY", "value": "sk-bad LD_PRELOAD=/tmp/x.so"}, 400, "single line"),
                ({"name": "OPENAI_API_KEY", "value": "sk-bad​"}, 400, "printable"),
                ({"name": "OPENAI_API_KEY", "value": "x" * 5000}, 400, "longer than 4096"),
                ({"name": "OPENAI_API_KEY"}, 400, "'name' and 'value' must be strings"),
                ({"name": 5, "value": "x"}, 400, "'name' and 'value' must be strings")):
            with self.subTest(body=body):
                response = self.request("POST", "/api/secrets", body)
                self.assert_error(response, status, message)
                self.assertNotIn(b"sk-bad", response.body)
        self.assertFalse(self.env_file.exists())
        self.assertNotIn("OPENAI_API_KEY", os.environ)

    def test_procedures_json_and_booklet(self) -> None:
        data = self.request("GET", "/api/docs/procedures").json()
        self.assertEqual([agent["name"] for agent in data["agents"]], ["builder", "reviewer-code", "approver"])
        self.assertEqual({m["id"] for m in data["maps"]}, {"factory-line", "plan-flow"})
        booklet = self.request("GET", "/api/docs/procedures.html")
        self.assertEqual(booklet.status, 200)
        self.assertEqual(booklet.headers["Content-Type"], "text/html; charset=utf-8")
        self.assertNotIn("Content-Disposition", booklet.headers)
        self.assertIn("<svg", booklet.text)
        self.assertIn("script-src 'self'", booklet.headers["Content-Security-Policy"])
        download = self.request("GET", "/api/docs/procedures.html?download=1")
        self.assertEqual(download.headers["Content-Disposition"],
                         'attachment; filename="software-factory-procedures.html"')
        self.assert_error(self.request("GET", "/api/docs/proceduresXhtml"), 404)
        self.assert_error(self.request("GET", "/api/docs/procedures.html", token=None), 401)
        self.assertIn('<details class="mermaid-source"><summary>Mermaid source</summary>', booklet.text)

    def test_mermaid_maps_and_mmd_download(self) -> None:
        from swfactory.docs import mermaid_maps

        maps = self.request("GET", "/api/docs/maps").json()
        self.assertEqual(maps, mermaid_maps(self.factory.config))
        self.assertEqual([m["id"] for m in maps], ["factory-line", "plan-flow"])
        for entry in maps:
            self.assertEqual(set(entry), {"id", "title", "mermaid", "sha256"})
            self.assertEqual(entry["sha256"], hashlib.sha256(entry["mermaid"].encode("utf-8")).hexdigest())
            self.assertIn("flowchart TD", entry["mermaid"])
        download = self.request("GET", "/api/docs/maps/plan-flow.mmd")
        self.assertEqual(download.status, 200)
        self.assertEqual(download.headers["Content-Type"], "text/plain; charset=utf-8")
        self.assertEqual(download.headers["Content-Disposition"], 'attachment; filename="plan-flow.mmd"')
        self.assertEqual(download.text, maps[1]["mermaid"])
        self.assert_error(self.request("GET", "/api/docs/maps/nope.mmd"), 404, "no map named nope")
        self.assert_error(self.request("GET", "/api/docs/maps/..%2Fx.mmd"), 400, "invalid map id")
        self.assert_error(self.request("GET", "/api/docs/maps", token=None), 401)


class AssumptionRoutesTest(ServerTestCase):
    """GET .../assumptions and POST .../confirm: a person's confirmation of the spec's (product) lines."""

    def test_list_and_confirm(self) -> None:
        path = f"/api/jobs/{REPO}/{JOB}"
        view = self.request("GET", f"{path}/assumptions").json()
        self.assertEqual(view["unconfirmed"], ["Free plan says Hello (product)", "Paid plan gets no greeting (product)"])
        reply = self.request("POST", f"{path}/confirm", {"lines": ["Free plan says Hello (product)"]}).json()
        self.assertEqual(reply["job"]["confirmed"]["lines"], ["Free plan says Hello (product)"])
        self.assertEqual(reply["assumptions"]["unconfirmed"], ["Paid plan gets no greeting (product)"])
        everything = self.request("POST", f"{path}/confirm", {"all": True}).json()
        self.assertEqual(everything["assumptions"]["unconfirmed"], [])
        self.assertEqual(self.factory.called("confirm"), [(REPO, JOB, ["Free plan says Hello (product)"]),
                                                          (REPO, JOB, None)])

    def test_bad_requests(self) -> None:
        path = f"/api/jobs/{REPO}/{JOB}/confirm"
        for body in ({}, {"lines": []}, {"lines": "Free plan says Hello (product)"}, {"lines": [3]}, {"lines": [" "]},
                     {"all": "yes"}, {"all": True, "lines": ["Free plan says Hello (product)"]},
                     {"lines": ["x" * 201]}, {"lines": ["x"] * 201}):
            with self.subTest(body=body):
                self.assert_error(self.request("POST", path, body), 400, "give 'lines'")
        self.assert_error(self.request("POST", path, {"lines": ["Not marked"]}), 400, "does not mark this line")
        self.assert_error(self.request("GET", f"/api/jobs/{REPO}/999-nope/assumptions"), 404)
        self.assert_error(self.request("GET", f"/api/jobs/{REPO}/{JOB}/assumptions", token=None), 401)
        self.assert_error(self.request("GET", path), 405)
        self.factory.fail["confirm"] = type("ActionError", (Exception,), {})("cannot confirm assumptions of "
                                                                              "app/001 while it is merged")
        self.assert_error(self.request("POST", path, {"all": True}), 409, "while it is merged")


class SettingsRoutesTest(ServerTestCase):
    def test_config(self) -> None:
        self.assertEqual(self.request("GET", "/api/config").json(), self.factory.view)
        patch = {"pipeline": {"max_resumes": 3}, "agents": {"builder": {"model": "local"}}}
        response = self.request("POST", "/api/config", patch)
        self.assertEqual(response.json()["patched"], ["agents", "pipeline"])
        self.assertEqual(self.factory.called("update_config"), [(patch,)])

    def test_preset(self) -> None:
        response = self.request("POST", "/api/config/preset", {"name": "hybrid"})
        self.assertEqual(response.json()["preset"], "hybrid")
        self.assert_error(self.request("POST", "/api/config/preset", {"name": "missing"}), 400, "unknown preset")
        for name in ("../etc/passwd", "a/b", "", "Hybrid"):
            with self.subTest(name=name):
                self.assert_error(self.request("POST", "/api/config/preset", {"name": name}), 400)
        self.assertEqual(self.factory.called("apply_preset"), [("hybrid",), ("missing",)])

    def test_model_test(self) -> None:
        response = self.request("POST", "/api/models/local/test", {})
        self.assertEqual(response.json()["tool_call"], True)
        self.assertEqual(self.factory.called("test_model"), [("local",)])
        unknown = self.request("POST", "/api/models/unknown/test", {})
        self.assertEqual((unknown.status, unknown.json()["ok"], unknown.json()["error"]),
                         (200, False, "unknown model 'unknown'"))
        self.assert_error(self.request("GET", "/api/models/local/test"), 405)


class PlanRoutesTest(ServerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.plan_dir = self.data / "plans" / REPO / PLAN
        (self.plan_dir / "logs").mkdir(parents=True)
        (self.plan_dir / "plan.md").write_text("PLAN: READY\n\n## Jobs\n1. Add login\n")
        (self.plan_dir / "request.md").write_text("Add auth")
        (self.plan_dir / "logs" / "01-lead.jsonl").write_text("{}\n")
        (self.plan_dir / ".plan.json.tmp").write_text("{}")
        (self.plan_dir / "sessions").mkdir()
        (self.plan_dir / "sessions" / "lead.json").write_text("{}")

    def test_plan_list_and_detail(self) -> None:
        self.assertEqual(self.request("GET", "/api/plans").json(), {"plans": [self.factory.plan_table[(REPO, PLAN)]]})
        detail = self.request("GET", f"/api/plans/{REPO}/{PLAN}").json()
        self.assertEqual(detail["plan"]["id"], PLAN)
        self.assertEqual(detail["files"], ["logs/01-lead.jsonl", "plan.md", "request.md"])
        self.assert_error(self.request("GET", f"/api/plans/{REPO}/P009-missing"), 404)

    def test_plan_without_folder_yet(self) -> None:
        shutil.rmtree(self.plan_dir)
        self.assertEqual(self.request("GET", f"/api/plans/{REPO}/{PLAN}").json()["files"], [])

    def test_plan_files(self) -> None:
        response = self.request("GET", f"/api/plans/{REPO}/{PLAN}/file?path=plan.md")
        self.assertEqual((response.status, response.headers["Content-Type"]), (200, "text/plain; charset=utf-8"))
        self.assertTrue(response.text.startswith("PLAN: READY"))
        (self.tmp / "outside.md").write_text("outside")
        os.symlink(self.tmp / "outside.md", self.plan_dir / "escape.md")
        for path in ("../../../../config.json", "%2Fetc%2Fpasswd", "escape.md", "logs/../../x", ""):
            with self.subTest(path=path):
                response = self.request("GET", f"/api/plans/{REPO}/{PLAN}/file?path={path}")
                self.assertEqual(response.status, 400, response.body)
                self.assertNotIn(b"outside", response.body)
        self.assert_error(self.request("GET", f"/api/plans/{REPO}/{PLAN}/file?path=missing.md"), 404)
        self.assert_error(self.request("GET", f"/api/plans/{REPO}/{PLAN}/file?path=logs"), 404)
        self.assert_error(self.request("GET", f"/api/plans/{REPO}/P009-missing/file?path=plan.md"), 404)
        self.assert_error(self.request("GET", f"/api/plans/{REPO}/{PLAN}/file"), 400, "path")

    def test_create_plan(self) -> None:
        response = self.request("POST", "/api/plans", {"repo": REPO, "request": "Add auth everywhere"})
        self.assertEqual((response.status, response.json()["plan"]["stage"]), (201, "planning"))
        self.request("POST", "/api/plans", {"repo": REPO, "request": "Again", "routing": "hybrid"})
        self.request("POST", "/api/plans", {"repo": REPO, "request": "Branches", "delivery": "branch"})
        self.assertEqual(self.factory.called("create_plan"),
                         [(REPO, "Add auth everywhere", None), (REPO, "Again", "hybrid"),
                          (REPO, "Branches", None, "branch")])
        self.assert_error(self.request("POST", "/api/plans", {"repo": REPO}), 400, "request")
        self.assert_error(self.request("POST", "/api/plans", {"repo": "nope", "request": "x"}), 404)

    def test_plan_log(self) -> None:
        log = self.plan_dir / "logs" / "01-lead.jsonl"
        log.write_text('{"seq": 1, "type": "agent.start"}\n{"seq": 2, "type": "agent.te')
        url = f"/api/plans/{REPO}/{PLAN}/log?file=logs/01-lead.jsonl"
        first = self.request("GET", url).json()
        self.assertEqual((first["lines"], first["more"]), ([{"seq": 1, "type": "agent.start"}], False))
        with open(log, "a") as handle:
            handle.write('xt"}\n')
        rest = self.request("GET", f"{url}&offset={first['offset']}").json()
        self.assertEqual(rest["lines"], [{"seq": 2, "type": "agent.text"}])
        (self.tmp / "outside.jsonl").write_text('{"secret": 1}\n')
        os.symlink(self.tmp / "outside.jsonl", self.plan_dir / "logs" / "02-evil.jsonl")
        self.assert_error(self.request("GET", f"/api/plans/{REPO}/{PLAN}/log?file=logs/02-evil.jsonl"), 404)
        for file in ("plan.json", "logs/../plan.json", "logs/x.txt", ""):
            with self.subTest(file=file):
                self.assert_error(self.request("GET", f"/api/plans/{REPO}/{PLAN}/log?file={file}"), 400, "logs/")
        self.assert_error(self.request("GET", f"/api/plans/{REPO}/P009-missing/log?file=logs/01-lead.jsonl"), 404)
        self.assert_error(self.request("GET", f"/api/plans/{REPO}/{PLAN}/log?file=logs/01-lead.jsonl&offset=x"),
                          400, "offset")

    def test_plan_actions(self) -> None:
        response = self.request("POST", f"/api/plans/{REPO}/{PLAN}/plan", {"text": "PLAN: READY\n"})
        self.assertTrue(response.json()["plan"]["edited"])
        self.assertEqual(self.factory.called("update_plan_text"), [(REPO, PLAN, "PLAN: READY\n")])
        for action in ("run", "cancel", "retry"):
            with self.subTest(action=action):
                result = self.request("POST", f"/api/plans/{REPO}/{PLAN}/{action}", {}).json()
                self.assertEqual(result["plan"]["last_action"], f"{action}_plan")
        self.factory.fail["run_plan"] = ActionError("plan is not ready")
        self.assert_error(self.request("POST", f"/api/plans/{REPO}/{PLAN}/run", {}), 409, "not ready")
        self.assert_error(self.request("POST", f"/api/plans/{REPO}/{PLAN}/explode", {}), 404)
        self.assert_error(self.request("POST", f"/api/plans/{REPO}/{PLAN}/plan", {}), 400, "text")


class PathValidationTest(ServerTestCase):
    """Traversal and junk in every path parameter is refused before the Factory sees it."""

    def test_every_parameter_is_validated(self) -> None:
        cases = [
            ("GET", "/api/jobs/..%2F..%2Fetc/001-first-job"),
            ("GET", "/api/jobs/%2e%2e/001-first-job"),
            ("GET", "/api/jobs/Demo/001-first-job"),
            ("GET", f"/api/jobs/{REPO}/..%2F001-first-job"),
            ("GET", f"/api/jobs/{REPO}/001-first-job%2F..%2F..%2Fx"),
            ("GET", f"/api/jobs/{REPO}/1-short"),
            ("GET", f"/api/jobs/{REPO}/%ff%fe/file?path=x"),
            ("GET", f"/api/jobs/{REPO}/..%2F..%2F/log?file=logs/a.jsonl"),
            ("GET", f"/api/jobs/{REPO}/%2e%2e/diff"),
            ("POST", f"/api/jobs/{REPO}/..%2Fx/approve"),
            ("POST", "/api/jobs/..%2F/001-first-job/cancel"),
            ("GET", "/api/repos/..%2F/backlog"),
            ("POST", "/api/repos/%2e%2e/backlog"),
            ("POST", "/api/repos/..%2Fother/next"),
            ("POST", "/api/repos/.hidden/scout"),
            ("GET", "/api/repos/" + "a" * 300 + "/scout"),
            ("GET", "/api/chats/..%2Fc-0123456789"),
            ("GET", "/api/chats/c-01234"),
            ("POST", "/api/chats/c-ABCDEF0123/messages"),
            ("GET", "/api/agents/..%2Fbuilder"),
            ("GET", "/api/agents/%2e%2e"),
            ("POST", "/api/agents/..%2Fbuilder"),
            ("POST", "/api/agents/..%2Fbuilder/reset"),
            ("POST", "/api/models/..%2F..%2Fx/test"),
            ("POST", "/api/models/%2e%2e/test"),
            ("POST", "/api/models/a%20b/test"),
            ("POST", "/api/models/%0a/test"),
            ("GET", f"/api/plans/{REPO}/..%2FP001-add-auth"),
            ("GET", f"/api/plans/{REPO}/p001-add-auth"),
            ("GET", "/api/plans/..%2F/P001-add-auth"),
            ("GET", f"/api/plans/%2e%2e/{PLAN}/file?path=plan.md"),
            ("POST", f"/api/plans/{REPO}/..%2F{PLAN}/run"),
            ("POST", f"/api/plans/..%2F/{PLAN}/plan"),
        ]
        for method, path in cases:
            with self.subTest(method=method, path=path):
                response = self.request(method, path, {} if method == "POST" else None)
                self.assert_error(response, 400, "invalid")
        self.assertEqual(self.factory.calls, [])

    def test_view_modules_are_served_from_web_views(self) -> None:
        """The GUI's view modules (web/views/<area>.js, loaded by index.html) are served like app.js; only
        JavaScript, only one level, never a symlink and never a views folder that is itself a symlink."""
        views = self.web / "views"
        views.mkdir()
        (views / "tracker.js").write_text("SF.registerView({});")
        (views / "notes.md").write_text("not served")
        (views / "deep").mkdir()
        (views / "deep" / "x.js").write_text("deep")
        os.symlink(self.tmp / "secret.js", views / "link.js")
        response = self.request("GET", "/views/tracker.js")
        self.assertEqual((response.status, response.headers["Content-Type"]), (200, "text/javascript; charset=utf-8"))
        self.assertEqual(response.text, "SF.registerView({});")
        self.assertNotEqual(response.headers["Content-Security-Policy"], HTML_CSP)
        self.assertEqual(self.request("GET", "/views/tracker.js", token=None).status, 200, "assets are public, like app.js")
        for path in ("/views/notes.md", "/views/deep/x.js", "/views/link.js", "/views/../secret.js", "/views/%2e%2e/secret.js",
                     "/views/..%2Fsecret.js", "/views/", "/views", "/views/.hidden.js", "/other/tracker.js", "/views/index.html"):
            with self.subTest(path=path):
                response = self.request("GET", path)
                self.assertEqual(response.status, 404, response.body[:200])
                self.assertNotIn(b"secret", response.body)
        shutil.rmtree(views)
        os.symlink(self.tmp, views)   # a views folder that points elsewhere is not followed
        self.assertEqual(self.request("GET", "/views/secret.js").status, 404)

    def test_static_paths_stay_in_the_web_folder(self) -> None:
        for path in ("/..%2Fsecret.js", "/../secret.js", "/%2e%2e/secret.js", "/.hidden", "/link.js",
                     "/notes.md", "/sub/style.css", "/server.py", "/index.html%00", "/style.css/", "/a..css"):
            with self.subTest(path=path):
                response = self.request("GET", path)
                self.assertEqual(response.status, 404, response.body[:200])
                self.assertNotIn(b"secret", response.body)


class StaticTest(ServerTestCase):
    def assert_security_headers(self, response: Response) -> None:
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertTrue(response.headers["Content-Security-Policy"])

    def test_pages(self) -> None:
        for path in ("/", "/index.html"):
            with self.subTest(path=path):
                response = self.request("GET", path)
                self.assertEqual(response.status, 200)
                self.assertEqual(response.text, "<!doctype html><title>Factory GUI</title>")
                self.assertEqual(response.headers["Content-Type"], "text/html; charset=utf-8")
                self.assertEqual(response.headers["Content-Security-Policy"], HTML_CSP)
                self.assert_security_headers(response)
        self.assertIn("Sign in", self.request("GET", "/login.html").text)

    def test_assets(self) -> None:
        cases = {"/app.js": "text/javascript; charset=utf-8", "/style.css": "text/css; charset=utf-8",
                 "/logo.svg": "image/svg+xml"}
        for path, content_type in cases.items():
            with self.subTest(path=path):
                response = self.request("GET", path)
                self.assertEqual((response.status, response.headers["Content-Type"]), (200, content_type))
                self.assertEqual(response.body, (self.web / path[1:]).read_bytes())
                self.assertNotEqual(response.headers["Content-Security-Policy"], HTML_CSP)
                self.assert_security_headers(response)

    def test_security_headers_on_every_response(self) -> None:
        responses = [self.request("GET", "/api/state"), self.request("GET", "/api/state", token=None),
                     self.request("GET", "/api/nothing"), self.request("GET", "/api/state", host="evil"),
                     self.request("POST", "/api/jobs", {}, content_type="text/plain"),
                     self.request("GET", f"/login?token={TOKEN}", token=None)]
        for response in responses:
            with self.subTest(status=response.status):
                self.assert_security_headers(response)
        client = self.sse()
        self.assertEqual(client.headers["x-content-type-options"], "nosniff")
        self.assertEqual(client.headers["x-frame-options"], "DENY")

    def test_inline_scripts_are_allowed_by_hash_only(self) -> None:
        script = "document.title = 'ok';"
        digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
        (self.web / "login.html").write_text(f"<!doctype html><title>Sign in</title><script>{script}</script>")
        csp = self.request("GET", "/", token=None).headers["Content-Security-Policy"]
        self.assertEqual(csp, HTML_CSP.replace("script-src 'self'", f"script-src 'self' 'sha256-{digest}'"))
        self.assertEqual(srv.page_csp(b"<p>no scripts</p><script src='/app.js'></script>"), HTML_CSP)
        two = srv.page_csp(b"<SCRIPT>a()</SCRIPT><script>\nb()\n</script>")
        self.assertEqual(two.count("'sha256-"), 2)

    def test_shipped_gui_is_served(self) -> None:
        if not (srv.WEB_DIR / "index.html").is_file():
            self.skipTest("the GUI is not built yet")
        server = srv.make_server(self.config, self.factory, self.bus, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        response = self.request("GET", "/", port=server.server_port)
        self.assertEqual((response.status, response.headers["Content-Type"]), (200, "text/html; charset=utf-8"))
        self.assertTrue(response.text.lower().startswith("<!doctype html"))
        login = self.request("GET", "/", token=None, port=server.server_port)
        for script in srv._INLINE_SCRIPT_RE.findall(login.body):  # the login page's own script may run
            digest = base64.b64encode(hashlib.sha256(script).digest()).decode()
            self.assertIn(f"'sha256-{digest}'", login.headers["Content-Security-Policy"])
        # Every script and stylesheet the page loads is served, the view modules in web/views/ included.
        assets = re.findall(r'<(?:script|link)[^>]+(?:src|href)="(/[^"]+)"', response.text)
        self.assertIn("/views/tracker.js", assets)
        for path in assets:
            with self.subTest(asset=path):
                asset = self.request("GET", path, port=server.server_port)
                self.assertEqual(asset.status, 200, asset.body[:200])
                self.assertEqual(asset.body, (srv.WEB_DIR / path[1:]).read_bytes())


class EventStreamTest(ServerTestCase):
    def publish(self, n: int) -> list[Event]:
        return [self.bus.publish("job.updated", {"job": {"id": f"00{i}-x"}}, repo=REPO) for i in range(n)]

    def test_headers_and_replay_then_live_events_and_pings(self) -> None:
        published = self.publish(3)
        client = self.sse("/api/events?since=1")
        self.assertEqual(client.status, 200)
        self.assertEqual(client.headers["content-type"], "text/event-stream")
        self.assertEqual(client.headers["cache-control"], "no-store")
        self.assertEqual(client.headers["x-accel-buffering"], "no")
        self.assertEqual(client.block(), [f"retry: {srv.SSE_RETRY_MS}"])
        replayed = [client.event(), client.event()]
        self.assertEqual([event["id"] for event in replayed], [2, 3])
        self.assertEqual(replayed[0]["data"], published[1].to_dict())
        self.assertEqual(replayed[0]["event"], "job.updated")
        live = self.bus.publish("agent.text", {"text": "héllo\nworld", "run": "01-builder"}, repo=REPO, job=JOB)
        self.assertEqual(client.event()["data"], live.to_dict())
        self.assertEqual(client.block(), [": ping"])

    def test_last_event_id_header(self) -> None:
        self.publish(3)
        client = self.sse("/api/events?since=1", headers={"Last-Event-ID": "2"})
        self.assertEqual(client.event()["id"], 3)

    def test_stale_since_from_a_previous_server_run_replays_everything(self) -> None:
        self.publish(3)
        client = self.sse("/api/events?since=999")
        self.assertEqual([client.event()["id"] for _ in range(3)], [1, 2, 3])

    def test_since_zero_and_empty_bus(self) -> None:
        client = self.sse("/api/events")
        self.assertEqual(client.block(), [f"retry: {srv.SSE_RETRY_MS}"])
        event = self.bus.publish("log", {"level": "info", "message": "hi"})
        self.assertEqual(client.event()["id"], event.seq)

    def test_events_racing_the_replay_are_sent_once(self) -> None:
        self.bus = RacyBus()
        self.server.bus = self.bus
        client = self.sse("/api/events")
        first = client.event()
        self.assertEqual(first["event"], "race")
        after = self.bus.publish("log", {"message": "after"})
        self.assertEqual(client.event()["id"], after.seq)

    def test_invalid_since(self) -> None:
        for since in ("abc", "-1", "1e3"):
            with self.subTest(since=since):
                self.assert_error(self.request("GET", f"/api/events?since={since}"), 400, "since")
        self.assertEqual(self.bus.open, set())

    def test_requires_auth(self) -> None:
        client = self.sse(token=None)
        self.assertEqual(client.status, 401)
        self.assertEqual(self.bus.open, set())

    def test_disconnect_unsubscribes(self) -> None:
        client = self.sse()
        client.block()
        self.assertEqual(len(self.bus.open), 1)
        client.close()
        self.assertTrue(wait_until(lambda: not self.bus.open), "the subscription was not released")

    def test_many_clients_get_every_event(self) -> None:
        clients = [self.sse() for _ in range(5)]
        for client in clients:
            client.block()
        self.assertTrue(wait_until(lambda: len(self.bus.open) == 5))
        events = self.publish(4)
        for client in clients:
            self.assertEqual([client.event()["id"] for _ in range(4)], [event.seq for event in events])

    def test_shutdown_with_an_open_stream_closes_it(self) -> None:
        client = self.sse()
        client.block()
        started = time.monotonic()
        self.server.shutdown()
        self.server.server_close()
        self.assertLess(time.monotonic() - started, 3)
        client.sock.settimeout(3)
        while client.block():
            pass  # pings that were already in flight, then end of stream
        self.assertTrue(wait_until(lambda: not self.bus.open))

    def test_close_streams_keeps_serving_requests(self) -> None:
        client = self.sse()
        client.block()
        self.server.close_streams()
        client.sock.settimeout(3)
        while client.block():
            pass
        self.assertEqual(self.request("GET", "/api/state").status, 200)

    def test_streams_opened_after_close_streams_stay_open(self) -> None:
        old = self.sse()
        old.block()
        self.server.close_streams()
        old.sock.settimeout(3)
        while old.block():
            pass
        new = self.sse()
        self.assertEqual(new.block(), [f"retry: {srv.SSE_RETRY_MS}"])
        time.sleep(4 * srv.SSE_POLL_SECONDS)  # several poll rounds: a closed stream would have ended by now
        event = self.bus.publish("log", {"level": "info", "message": "after close_streams"})
        self.assertEqual(new.event()["id"], event.seq)
        self.assertEqual(len(self.bus.open), 1)
        self.server.close_streams()  # a second call ends the streams open at that time
        new.sock.settimeout(3)
        while new.block():
            pass
        self.assertTrue(wait_until(lambda: not self.bus.open))

    def test_shutdown_after_close_streams_still_ends_new_streams(self) -> None:
        self.server.close_streams()
        client = self.sse()
        client.block()
        self.server.shutdown()
        client.sock.settimeout(3)
        while client.block():
            pass
        self.assertTrue(wait_until(lambda: not self.bus.open))

    def test_sse_frame_is_one_line_of_ascii_json(self) -> None:
        event = self.bus.publish("agent.text", {"text": "a\nb\r\nc   ü"})
        frame = srv.sse_frame(event).decode("ascii")
        lines = frame.split("\n")
        self.assertEqual(lines[:2], [f"id: {event.seq}", "event: agent.text"])
        self.assertEqual(json.loads(lines[2][len("data: "):]), event.to_dict())
        self.assertEqual(lines[3:], ["", ""])


class RealFactoryCase(ServerTestCase):
    """The server in front of a real orchestrator.Factory, settings, ChatManager and PlanManager.

    The Factory is not started, so no worker drives a job, and its runner factory refuses to
    build a runner, so no agent (and no model call) can run. HOME points into the temp dir, so
    the user agent dir the Factory reads and the one settings writes are both the test's own.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sf-server-real-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        home = self.tmp / "home"
        home_patch = mock.patch.dict(os.environ, {"HOME": str(home)})
        home_patch.start()
        self.addCleanup(home_patch.stop)
        self.web = srv.WEB_DIR
        self.data = self.tmp / "data"
        self.repo = make_repo(self.tmp, "repo")
        config_dir = home / ".config" / "software-factory"
        self.agents_dir = config_dir / "agents"
        self.config = Config(config_dir / "config.json", {
            "server": {"token": TOKEN}, "data_dir": str(self.data),
            "repos": [{"name": REPO, "path": str(self.repo)}]})
        self.config.save()
        self.bus = EventBus(secrets_provider=lambda: [TOKEN])
        self.runner_requests: list[str] = []
        self.factory = Factory(self.config, self.bus, runner_factory=self.no_runner)
        self.addCleanup(self.factory.stop, 5)
        self.server = self.start_server(self.config, self.factory, self.bus)
        self.port = self.server.server_port

    def tearDown(self) -> None:
        self.assertEqual(self.runner_requests, [], "an agent run was attempted")

    def no_runner(self, config: Config, driver: str) -> Any:
        self.runner_requests.append(driver)
        raise RuntimeError("no agent may run in these tests")

    def submit(self, feature: str, repo: str = REPO) -> dict:
        response = self.request("POST", "/api/jobs", {"repo": repo, "feature": feature})
        self.assertEqual(response.status, 201, response.body)
        return response.json()["job"]


class RealFactoryTest(RealFactoryCase):
    def test_submit_then_the_job_its_files_and_a_log(self) -> None:
        job = self.submit("Add a footer")
        self.assertEqual((job["repo"], job["stage"], job["feature"], job["base"]), (REPO, "queued", "Add a footer", None))
        self.assertIn("builder", job["routing"])
        base = f"/api/jobs/{REPO}/{job['id']}"
        detail = self.request("GET", base).json()
        self.assertEqual(detail["job"]["id"], job["id"])
        self.assertTrue({"feature.md", "job.json"} <= set(detail["files"]), detail["files"])
        self.assertEqual(self.request("GET", f"{base}/file?path=feature.md").text, "Add a footer")
        logs = Path(job["job_dir"]) / "logs"
        logs.mkdir(exist_ok=True)
        (logs / "01-spec-writer.jsonl").write_text('{"type": "agent.text"}\n')
        log = self.request("GET", f"{base}/log?file=logs/01-spec-writer.jsonl").json()
        self.assertEqual((log["lines"], log["more"]), ([{"type": "agent.text"}], False))
        self.assertEqual(self.request("GET", f"{base}/diff").json(), {"stat": "", "diff": "", "truncated": False})
        self.assert_error(self.request("GET", f"{base}/file?path=../../../../home"), 400)
        self.assert_error(self.request("GET", f"{base}/file?path=missing.md"), 404)
        self.assert_error(self.request("GET", f"/api/jobs/{REPO}/999-missing"), 404)
        self.assert_error(self.request("POST", "/api/jobs", {"repo": "unknown", "feature": "x"}), 404)
        self.assertEqual([j["id"] for j in self.request("GET", "/api/state").json()["jobs"]], [job["id"]])

    def test_cancel_then_approve_is_409_and_reject_closes_the_job(self) -> None:
        job = self.submit("Dark mode")
        base = f"/api/jobs/{REPO}/{job['id']}"
        cancelled = self.request("POST", f"{base}/cancel")
        self.assertEqual((cancelled.status, cancelled.json()["job"]["stage"]), (200, "cancelled"))
        self.assert_error(self.request("POST", f"{base}/approve"), 409, "cannot approve")
        self.assert_error(self.request("POST", f"{base}/cancel"), 409, "cannot cancel")
        self.assert_error(self.request("POST", f"{base}/retry"), 409, "cannot retry")
        self.assert_error(self.request("POST", f"{base}/rework", {"note": "again"}), 409, "cannot rework")
        self.assert_error(self.request("POST", f"{base}/rework", {"note": " "}), 400, "note")
        rejected = self.request("POST", f"{base}/reject", {"reason": "not needed"})
        self.assertEqual((rejected.status, rejected.json()["job"]["stage"]), (200, "rejected"))
        self.assertEqual(self.request("GET", base).json()["job"]["reason"], "not needed")
        self.assert_error(self.request("POST", f"{base}/reject"), 409, "cannot reject")

    def test_backlog_and_next(self) -> None:
        self.assert_error(self.request("POST", f"/api/repos/{REPO}/next"), 409, "no unchecked items")
        added = self.request("POST", f"/api/repos/{REPO}/backlog", {"text": "Add a footer"}).json()
        self.assertEqual(added, {"items": [{"text": "Add a footer", "done": False, "job": None}]})
        job = self.request("POST", f"/api/repos/{REPO}/next").json()["job"]
        self.assertEqual((job["feature"], job["stage"]), ("Add a footer", "queued"))
        backlog = self.request("GET", f"/api/repos/{REPO}/backlog").json()
        self.assertEqual(backlog, {"items": [{"text": "Add a footer", "done": True, "job": job["id"]}]})
        self.assert_error(self.request("POST", f"/api/repos/{REPO}/next"), 409, "no unchecked items")
        self.assert_error(self.request("GET", "/api/repos/unknown/backlog"), 404)
        self.assertEqual(self.request("GET", f"/api/repos/{REPO}/scout").json(), {"draft": None, "running": False})

    def test_add_repo_reloads_the_factory_config(self) -> None:
        other = make_repo(self.tmp, "Other.App")
        response = self.request("POST", "/api/repos", {"path": str(other)})
        self.assertEqual(response.status, 200, response.body)
        self.assertEqual(response.json()["repo"]["name"], "other.app")
        state = self.request("GET", "/api/state").json()
        self.assertEqual([repo["name"] for repo in state["repos"]], [REPO, "other.app"])
        self.assertEqual([repo.name for repo in Config.load(self.config.path).repos()], [REPO, "other.app"])
        self.assertEqual(self.submit("Works on the new repo", repo="other.app")["repo"], "other.app")
        self.assert_error(self.request("POST", "/api/repos", {"path": str(other), "name": "again"}), 400,
                          "already configured")
        self.assert_error(self.request("POST", "/api/repos", {"path": str(self.data), "name": "plain"}), 400)

    def test_new_agent_then_reset_it(self) -> None:
        created = self.request("POST", "/api/agents", {"name": "reviewer-perf"})
        self.assertEqual(created.status, 201, created.body)
        self.assertEqual((created.json()["agent"]["name"], created.json()["agent"]["source"]), ("reviewer-perf", "user"))
        path = self.agents_dir / "reviewer-perf.md"
        self.assertIn("review-perf.md", path.read_text())
        state = self.request("GET", "/api/state").json()
        self.assertIn("reviewer-perf", [agent["name"] for agent in state["agents"]])
        self.assert_error(self.request("POST", "/api/agents", {"name": "reviewer-perf"}), 400, "already exists")
        self.assert_error(self.request("POST", "/api/agents", {"name": "x", "from": "nobody"}), 400, "unknown agent")
        reset = self.request("POST", "/api/agents/reviewer-perf/reset")
        self.assertEqual((reset.status, reset.json()), (200, {"agent": None}))
        self.assertFalse(path.exists())
        self.assert_error(self.request("POST", "/api/agents/reviewer-perf/reset"), 404, "no user override")
        self.assert_error(self.request("GET", "/api/agents/reviewer-perf"), 404)

    def test_override_a_shipped_agent_then_reset_it(self) -> None:
        shipped = self.request("GET", "/api/agents/reviewer-code").json()
        self.assertEqual(shipped["source"], "builtin")
        saved = self.request("POST", "/api/agents/reviewer-code", {"text": shipped["text"] + "\nBe brief.\n"})
        self.assertEqual((saved.status, saved.json()["agent"]["source"]), (200, "user"), saved.body)
        renamed = "---\nname: other\ndescription: x\n---\nBody\n"
        self.assert_error(self.request("POST", "/api/agents/reviewer-code", {"text": renamed}), 400, "name")
        reset = self.request("POST", "/api/agents/reviewer-code/reset")
        self.assertEqual(reset.status, 200, reset.body)
        self.assertEqual((reset.json()["agent"]["source"], reset.json()["agent"]["text"]), ("builtin", shipped["text"]))

    def test_diff_of_a_merged_job_and_of_a_job_whose_branch_is_gone(self) -> None:
        store = JobStore(self.data)
        merged = store.new_job(REPO, "Add a feature", ["code"], 2, base="main")
        run_git(self.repo, "checkout", "-q", "-b", merged["branch"])
        commit_file(self.repo, "feature.txt", "hello\n", "add feature")
        run_git(self.repo, "checkout", "-q", "main")
        run_git(self.repo, "merge", "--no-ff", "-q", "-m", "merge the job", merged["branch"])
        run_git(self.repo, "branch", "-D", merged["branch"])
        merged.update(stage="merged", merged_commit=run_git(self.repo, "rev-parse", "HEAD").strip())
        store.save(merged)
        diff = self.request("GET", f"/api/jobs/{REPO}/{merged['id']}/diff").json()
        self.assertIn("feature.txt", diff["stat"])
        self.assertIn("+hello", diff["diff"])
        self.assertNotIn("error", diff)
        gone = store.new_job(REPO, "Rejected work", ["code"], 2, base="main")
        store.save(dict(gone, stage="rejected"))
        diff = self.request("GET", f"/api/jobs/{REPO}/{gone['id']}/diff")
        self.assertEqual(diff.status, 200)
        self.assertEqual((diff.json()["diff"], diff.json()["truncated"]), ("", False))
        self.assertTrue(diff.json()["error"])

    def test_settings_and_the_token_never_reaches_the_gui(self) -> None:
        for path in ("/api/state", "/api/config"):
            with self.subTest(path=path):
                response = self.request("GET", path)
                self.assertEqual(response.status, 200)
                self.assertNotIn(TOKEN.encode(), response.body)
        self.assertIn("opus", self.request("GET", "/api/config").json()["models"])
        self.assert_error(self.request("POST", "/api/config", {"server": {"token": "f" * 32}}), 400, "server")
        self.assert_error(self.request("POST", "/api/config", {"pipeline": {"max_resumes": "two"}}), 400, "max_resumes")
        self.assertEqual(Config.load(self.config.path).raw["server"]["token"], TOKEN)
        updated = self.request("POST", "/api/config", {"pipeline": {"max_resumes": 3}})
        self.assertEqual(updated.status, 200, updated.body)
        self.assertNotIn(TOKEN.encode(), updated.body)
        self.assertEqual(self.request("GET", "/api/state").json()["pipeline"]["max_resumes"], 3)
        self.assert_error(self.request("POST", "/api/config/preset", {"name": "nope"}), 400, "unknown preset")
        unknown = self.request("POST", "/api/models/nope/test").json()
        self.assertEqual((unknown["ok"], unknown["error"]), (False, "unknown model 'nope'"))

    def test_chats_and_plans(self) -> None:
        self.assertEqual(self.request("GET", "/api/chats").json(), {"chats": []})
        created = self.request("POST", "/api/chats", {"repo": REPO})
        self.assertEqual(created.status, 201, created.body)
        chat = created.json()["chat"]
        self.assertEqual((chat["repo"], chat["mode"], chat["status"]), (REPO, "worktree", "idle"))
        detail = self.request("GET", f"/api/chats/{chat['id']}").json()
        self.assertEqual((detail["chat"]["id"], detail["messages"]), (chat["id"], []))
        self.assertNotIn("messages", detail["chat"])
        self.assertEqual(self.request("POST", f"/api/chats/{chat['id']}/delete").json(), {"ok": True})
        self.assert_error(self.request("GET", f"/api/chats/{chat['id']}"), 404)
        self.assert_error(self.request("POST", "/api/chats", {"repo": "unknown"}), 404)
        self.assert_error(self.request("POST", "/api/chats", {}), 400, "'repo'")
        self.assert_error(self.request("POST", "/api/chats", {"repo": REPO, "mode": "none"}), 400, "leave out 'repo'")
        self.assert_error(self.request("POST", "/api/chats", {"mode": "elsewhere"}), 400, "'repo'")
        self.assert_error(self.request("POST", "/api/chats", {"repo": REPO, "mode": "elsewhere"}), 400, "'none'")
        repoless = self.request("POST", "/api/chats", {"mode": "none"})
        self.assertEqual(repoless.status, 201, repoless.body)
        self.assertEqual((repoless.json()["chat"]["repo"], repoless.json()["chat"]["mode"]), (None, "none"))
        self.assertEqual(self.request("GET", "/api/plans").json(), {"plans": []})
        self.assert_error(self.request("GET", f"/api/plans/{REPO}/{PLAN}"), 404)
        self.assert_error(self.request("GET", f"/api/plans/{REPO}/{PLAN}/file?path=plan.md"), 404)


class ItemRoutesTest(RealFactoryCase):
    """DESIGN §32: the Housekeeping Tracker's routes in front of a real (not started) Factory."""

    def test_job_items_and_actions(self) -> None:
        job = self.submit("Add a footer")
        base = f"/api/jobs/{REPO}/{job['id']}/items"
        self.assertEqual(self.request("GET", base).json()["items"], [])
        created = self.request("POST", base, {"title": "Check the other pages", "blocking": True, "owner": "human"})
        self.assertEqual(created.status, 201, created.body)
        item = created.json()["item"]
        self.assertEqual((item["id"], item["ref"], item["word"], item["blocking"], item["owner"]),
                         ("I-1", f"job:{REPO}/{job['id']}/I-1", "To do", True, "human"))
        view = self.request("GET", base).json()
        self.assertEqual(sorted(view), ["items", "log", "notices", "summary", "unparsed"])
        self.assertEqual(view["summary"]["blocking_open"], 1)
        self.assertEqual(self.request("GET", f"/api/jobs/{REPO}/{job['id']}").json()["job"]["items"]["total"], 1)
        refused = self.request("POST", "/api/items/drop", {"ref": item["ref"]})
        self.assert_error(refused, 400, "dropping an item needs a reason")
        verified = self.request("POST", "/api/items/verify", {"ref": item["ref"], "note": "looked at every page"})
        self.assertEqual((verified.status, verified.json()["item"]["status"]), (200, "verified"))
        self.assert_error(self.request("POST", "/api/items/verify", {"ref": f"job:{REPO}/{job['id']}/I-9"}), 404)
        self.assert_error(self.request("POST", "/api/items/verify", {"ref": "nonsense"}), 400, "invalid item reference")
        self.assert_error(self.request("POST", "/api/items/verify", {}), 400, "'ref'")
        self.assert_error(self.request("POST", "/api/items/explode", {"ref": item["ref"]}), 404)
        self.assert_error(self.request("POST", base, {"title": "x", "colour": "red"}), 400, "unknown field")
        self.assert_error(self.request("POST", base, {"title": "x", "blocking": "yes"}), 400, "blocking")
        self.assert_error(self.request("POST", base, {"title": " "}), 400, "title")
        self.assert_error(self.request("GET", f"/api/jobs/{REPO}/999-missing/items"), 404)
        self.assert_error(self.request("GET", base, token=None), 401)
        log = self.request("GET", base).json()["log"]
        self.assertEqual([(e["by"], e["to"]) for e in log], [("human", "open"), ("human", "verified")])
        history = self.request("GET", f"/api/items/history?ref={item['ref']}")
        self.assertEqual(history.status, 200, history.body)
        self.assertEqual((history.json()["item"]["status"], [e["to"] for e in history.json()["log"]]),
                         ("verified", ["open", "verified"]))
        self.assert_error(self.request("GET", "/api/items/history"), 400, "ref")
        self.assert_error(self.request("GET", f"/api/items/history?ref=job:{REPO}/{job['id']}"), 400, "names a scope")
        self.assert_error(self.request("GET", f"/api/items/history?ref=job:{REPO}/{job['id']}/I-9"), 404)
        self.assert_error(self.request("POST", "/api/items/history", {"ref": item["ref"]}), 405)

    def test_listing_follow_ups_and_make_job(self) -> None:
        added = self.request("POST", f"/api/repos/{REPO}/followups", {"title": "Re-run the restore drill",
                                                                     "due": "2020-01-01"})
        self.assertEqual(added.status, 201, added.body)
        follow = added.json()["item"]
        self.assertEqual((follow["ref"], follow["kind"], follow["overdue"], follow["waiting"]),
                         (f"repo:{REPO}/F-1", "followup", True, "overdue"))
        self.assertEqual(self.request("GET", f"/api/repos/{REPO}/followups").json()["summary"]["total"], 1)
        self.assert_error(self.request("GET", "/api/repos/unknown/followups"), 404)
        self.assert_error(self.request("POST", f"/api/repos/{REPO}/followups", {"title": "x", "due": "soon"}), 400,
                          "date")
        listing = self.request("GET", f"/api/items?repo={REPO}&waiting=1").json()
        self.assertEqual([row["ref"] for row in listing["items"]], [f"repo:{REPO}/F-1"])
        self.assertEqual((listing["counts"]["overdue"], listing["counts"]["waiting"]), (1, 1))
        self.assertEqual(self.request("GET", f"/api/items?scope=job&status=open").json()["items"], [])
        self.assert_error(self.request("GET", "/api/items?scope=galaxy"), 400, "scope")
        self.assert_error(self.request("GET", "/api/items?status=finished"), 400, "unknown status")
        self.assert_error(self.request("GET", "/api/items?waiting=maybe"), 400, "flag")
        self.assert_error(self.request("GET", "/api/items?limit=5000"), 400, "limit")
        made = self.request("POST", "/api/items/make-job", {"ref": follow["ref"]})
        self.assertEqual(made.status, 200, made.body)
        body = made.json()
        self.assertEqual((body["item"]["status"], body["item"]["links"]["job"]), ("doing", body["job"]["id"]))
        self.assertTrue(body["job"]["feature"].startswith("Follow-up F-1: Re-run the restore drill"))

    def test_the_system_scope(self) -> None:
        self.assertEqual(self.request("GET", "/api/system/items").json()["items"], [])
        added = self.request("POST", "/api/system/items", {"title": "Renew the TLS certificate", "due": "2020-01-01"})
        self.assertEqual(added.status, 201, added.body)
        item = added.json()["item"]
        self.assertEqual((item["ref"], item["scope"]["kind"], item["waiting"]), ("system:S-1", "system", "overdue"))
        self.assertEqual(self.request("GET", "/api/items?scope=system").json()["total"], 1)
        self.assertEqual(self.request("GET", f"/api/items?repo={REPO}&scope=system").json()["total"], 0)
        dropped = self.request("POST", "/api/items/drop", {"ref": "system:S-1", "reason": "renewed by hand"})
        self.assertEqual(dropped.json()["item"]["status"], "dropped")
        self.assertEqual(self.request("GET", "/api/items/history?ref=system:S-1").json()["item"]["status"], "dropped")
        self.assert_error(self.request("POST", "/api/system/items", {"title": "x", "owner": "someone"}), 400,
                          "unknown field")
        self.assert_error(self.request("GET", "/api/items/system"), 404)
        self.assert_error(self.request("POST", "/api/items/verify", {"ref": "system:S-9"}), 404)

    def test_chat_and_plan_items_and_the_event_stream(self) -> None:
        client = self.sse()
        chat = self.request("POST", "/api/chats", {"repo": REPO}).json()["chat"]
        added = self.request("POST", f"/api/chats/{chat['id']}/items", {"title": "Ask about the copy"})
        self.assertEqual(added.status, 201, added.body)
        self.assertEqual(added.json()["item"]["ref"], f"chat:{chat['id']}/C-1")
        self.assertEqual(self.request("GET", f"/api/chats/{chat['id']}/items").json()["summary"]["total"], 1)
        self.assert_error(self.request("POST", f"/api/chats/{chat['id']}/items", {"title": "x", "owner": "me"}),
                          400, "unknown field")
        while True:
            event = client.event()
            if event["event"] == "items.updated":
                break
        self.assertEqual(event["data"]["data"]["ref"], f"chat:{chat['id']}")
        self.assert_error(self.request("GET", f"/api/plans/{REPO}/{PLAN}/items"), 404)
        self.assert_error(self.request("GET", "/api/chats/c-0000000000/items"), 404)

    def test_a_scope_mid_ingest_answers_409(self) -> None:
        job = self.submit("Busy one")
        from swfactory import items as items_lib
        from swfactory import orchestrator

        store = items_lib.ItemStore(Path(job["job_dir"]), items_lib.job_scope(REPO, job["id"]))
        holding, release = threading.Event(), threading.Event()

        def hold() -> None:
            with store.transaction():
                holding.set()
                release.wait(10)

        thread = threading.Thread(target=hold, daemon=True)
        thread.start()
        holding.wait(5)
        try:
            with mock.patch.object(orchestrator, "BUSY_WAIT", 0.05):
                busy = self.request("POST", f"/api/jobs/{REPO}/{job['id']}/items", {"title": "x"})
        finally:
            release.set()
            thread.join(5)
        self.assert_error(busy, 409, "updating its items")


class TrackerRefinementRoutesTest(RealFactoryCase):
    """DESIGN §32 (v2 §10.1.10): the system scope, outside agents' tokens, approve's 409 and bulk dismiss."""

    def test_a_deleted_chat_without_a_repository_carries_its_items_to_the_system_scope(self) -> None:
        """§19 meets §32: no follow-ups to defer to; on delete the HousekeepingManager takes the open items."""
        client = self.sse()
        chat = self.request("POST", "/api/chats", {"mode": "none"}).json()["chat"]
        added = self.request("POST", f"/api/chats/{chat['id']}/items", {"title": "Ask about the copy"})
        self.assertEqual(added.status, 201, added.body)
        for body in ({"ref": f"chat:{chat['id']}/C-1", "reason": "later"},
                     {"ref": f"chat:{chat['id']}/C-1", "to": "defer", "reason": "later"}):
            action = "defer" if "to" not in body else "update"
            self.assert_error(self.request("POST", f"/api/items/{action}", body), 400, "no follow-ups")
        self.request("POST", "/api/system/items", {"title": "Rotate the backup key"})
        self.assertEqual(self.request("POST", f"/api/chats/{chat['id']}/delete").json(), {"ok": True})
        view = self.request("GET", "/api/system/items").json()
        self.assertEqual([(row["ref"], row["title"], row["owner"], row["links"]["chat"]) for row in view["items"]],
                         [("system:S-1", "Rotate the backup key", "human", None),
                          ("system:S-2", "Ask about the copy", "human", chat["id"])])
        self.assertEqual(view["items"][1]["source"]["reason"], "chat deleted")
        refs = []
        while len(refs) < 2:
            event = client.event()
            if event["event"] == "items.updated" and event["data"]["data"]["ref"] == "system":
                refs.append(event["data"]["data"]["changed"])
        self.assertEqual(refs, [["S-1"], ["S-2"]], "the carry is announced on the system scope")

    def test_the_system_scope(self) -> None:
        added = self.request("POST", "/api/system/items", {"title": "Re-run the restore test", "kind": "risk",
                                                           "due": "2026-12-01"})
        self.assertEqual(added.status, 201, added.body)
        item = added.json()["item"]
        self.assertEqual((item["ref"], item["kind"], item["owner"], item["scope"]["host"], item["waiting"]),
                         ("system:S-1", "risk", "human", "local", "yours"))
        view = self.request("GET", "/api/system/items").json()
        self.assertEqual((view["summary"]["total"], [row["ref"] for row in view["items"]]), (1, ["system:S-1"]))
        listed = self.request("GET", "/api/items?scope=system").json()["items"]
        self.assertEqual([row["ref"] for row in listed], ["system:S-1"])
        verified = self.request("POST", "/api/items/verify", {"ref": "system:S-1", "note": "ran it"})
        self.assertEqual(verified.json()["item"]["status"], "verified")
        self.assertEqual(self.request("GET", "/api/items/history?ref=system:S-1").json()["item"]["status"], "verified")
        self.assert_error(self.request("POST", "/api/items/verify", {"ref": "host:nas-2/system:S-1"}), 404, "host")
        self.assert_error(self.request("POST", "/api/system/items", {"title": "x", "kind": "galaxy"}), 400, "kind")
        self.assert_error(self.request("POST", "/api/items/make-job", {"ref": "system:S-1"}), 400, "no repo")

    def test_an_agent_token_reaches_the_item_routes_only(self) -> None:
        created = self.request("POST", "/api/agent-tokens", {"name": "qwen-code"})
        self.assertEqual(created.status, 201, created.body)
        token = created.json()["token"]
        self.assertTrue(token.startswith("sfi_"))
        stored = (self.data / "auth" / "agent-tokens.json").read_text()
        self.assertNotIn(token, stored, "only the token's hash is kept")
        self.assertEqual(oct((self.data / "auth" / "agent-tokens.json").stat().st_mode & 0o777), "0o600")
        added = self.request("POST", "/api/items/propose", {"scope": "system", "title": "Rotate the backup key"},
                             token=token)
        self.assertEqual(added.status, 201, added.body)
        mine = added.json()["item"]
        self.assertEqual((mine["owner"], mine["verifier"], mine["status"]), ("agent:qwen-code", "human", "open"))
        follow = self.request("POST", f"/api/repos/{REPO}/followups", {"title": "Update the docs"}, token=token)
        self.assertEqual(follow.json()["item"]["owner"], "agent:qwen-code")
        self.request("POST", f"/api/repos/{REPO}/followups", {"title": "A person's own"})
        self.factory.open_persona_item("seraphina", "Seraphina: a follow-up (private, see the cockpit)",
                                       taint=["private"])
        self.request("POST", "/api/system/items", {"title": "A person's system item"})
        self.factory.open_persona_item("seraphina", "Ask the owner about the clinic appointment on Friday",
                                       for_owner=True)
        listed = self.request("GET", "/api/items", token=token)
        self.assertEqual(listed.status, 200, listed.body)
        refs = sorted(row["ref"] for row in listed.json()["items"])
        self.assertEqual(refs, [f"repo:{REPO}/F-1", "system:S-1"],
                         "only its own items: never a person's, nor a persona's (its suggestion for the owner too)")
        self.assertEqual(self.request("GET", "/api/items?scope=system", token=token).json()["items"][0]["ref"],
                         "system:S-1")
        done = self.request("POST", "/api/items/update", {"ref": "system:S-1", "to": "done",
                                                          "evidence": [{"kind": "command", "ref": "restic check",
                                                                        "detail": "no errors"}]}, token=token)
        self.assertEqual((done.status, done.json()["item"]["status"]), (200, "done"))
        self.assert_error(self.request("POST", "/api/items/update", {"ref": "system:S-3", "to": "doing"},
                                       token=token), 400, "not yours")
        shared = self.request("POST", "/api/items/assign", {"ref": "system:S-3", "owner": "agent:qwen-code"})
        self.assertEqual(shared.status, 200, shared.body)
        self.assertIn("system:S-3", [row["ref"] for row in self.request("GET", "/api/items", token=token).json()[
            "items"]], "a person shares an item with an outside agent by assigning it")
        self.assert_error(self.request("POST", "/api/items/assign", {"ref": "system:S-4", "owner": "agent:qwen-code"}),
                          400, "never given to an outside agent")
        for kind in ("decision", "criterion", "rework"):
            self.assert_error(self.request("POST", "/api/system/items", {"title": f"a {kind}", "kind": kind},
                                           token=token), 400, "an outside agent adds")
        self.assert_error(self.request("POST", "/api/items/update", {"ref": "system:S-1", "to": "verified",
                                                                     "reason": "x"}, token=token), 400)
        for action in ("verify", "drop", "unblock", "dismiss"):
            self.assert_error(self.request("POST", f"/api/items/{action}", {"ref": "system:S-1", "reason": "x"},
                                           token=token), 403, "only update its own items")
        for method, path in (("GET", "/api/state"), ("GET", "/api/system/items"), ("GET", "/api/agent-tokens"),
                             ("POST", "/api/jobs"), ("GET", f"/api/repos/{REPO}/followups")):
            self.assert_error(self.request(method, path, {} if method == "POST" else None, token=token), 403,
                              "valid only for the item routes")
        self.assert_error(self.request("GET", "/api/items?scope=job", token=token), 403)
        self.assert_error(self.request("POST", f"/api/jobs/{REPO}/001-x/items", {"title": "x"}, token=token), 403)
        listing = self.request("GET", "/api/agent-tokens").json()
        tokens = listing["tokens"]
        self.assertEqual(listing["connect"], {"sf_items": str(srv.SF_ITEMS), "mcp_args": ["mcp"],
                                              "token_env": "SF_ITEMS_TOKEN", "url_env": "SF_ITEMS_URL",
                                              "url": f"http://127.0.0.1:{self.port}"})
        self.assertTrue(srv.SF_ITEMS.is_file())
        self.assertEqual([t["name"] for t in tokens], ["qwen-code"])
        self.assertNotIn("token", tokens[0])
        self.assertIsNotNone(tokens[0]["last_used"])
        self.assert_error(self.request("POST", "/api/agent-tokens", {"name": "qwen-code"}), 400, "exists")
        self.assert_error(self.request("POST", "/api/agent-tokens", {"name": "Bad Name"}), 400, "token name")
        revoked = self.request("POST", "/api/agent-tokens/qwen-code/revoke")
        self.assertEqual((revoked.status, revoked.json()["tokens"]), (200, []))
        self.assert_error(self.request("GET", "/api/items", token=token), 401)
        self.assert_error(self.request("POST", "/api/agent-tokens/qwen-code/revoke"), 404)
        self.assert_error(self.request("GET", "/api/items", token="sfi_forged"), 401)

    def test_a_person_s_update_takes_the_path_of_its_action(self) -> None:
        from swfactory import items as items_lib

        job = self.submit("Add a footer")
        base = f"/api/jobs/{REPO}/{job['id']}/items"
        first = self.request("POST", base, {"title": "Check the other pages", "blocking": True}).json()["item"]
        second = self.request("POST", base, {"title": "Check the print stylesheet", "blocking": True}).json()["item"]
        by_defer = self.request("POST", "/api/items/defer", {"ref": first["ref"], "reason": "next sprint"})
        by_update = self.request("POST", "/api/items/update", {"ref": second["ref"], "to": "defer",
                                                               "reason": "next sprint"})
        self.assertEqual(by_update.status, 200, by_update.body)
        self.assertEqual([r.json()["item"]["links"]["followup"] for r in (by_defer, by_update)],
                         [f"repo:{REPO}/F-1", f"repo:{REPO}/F-2"], "update to defer moves it to the follow-ups too")
        self.factory.cancel(REPO, job["id"])
        follow = self.request("GET", f"/api/repos/{REPO}/followups").json()["items"]
        self.assertEqual([f["title"] for f in follow], ["Check the other pages", "Check the print stylesheet"])
        live = self.factory._entry((REPO, job["id"]))
        self.assert_error(self.request("POST", "/api/items/update", {"ref": second["ref"], "to": "not met",
                                                                     "reason": "x"}), 400, "has ended")
        self.assertEqual(live["stage"], "cancelled")
        other = self.submit("Add a header")
        with self.factory._job_items(self.factory._entry((REPO, other["id"]))) as ledger:
            items_lib.apply_sweep(ledger, [("reply", "I will update the docs later.")], "builder")
        ref = f"job:{REPO}/{other['id']}/I-1"
        dismissed = self.request("POST", "/api/items/update", {"ref": ref, "to": "dismiss", "reason": "narration"})
        self.assertEqual(dismissed.json()["item"]["status"], "dropped")
        self.assertIn(items_lib.text_hash("I will update the docs later."),
                      self.factory.store.sweep_suppressed(REPO), "update to dismiss teaches the suppress list too")

    def test_approve_names_the_open_must_do_items_or_gets_409(self) -> None:
        job = self.submit("Add a footer")
        self.request("POST", f"/api/jobs/{REPO}/{job['id']}/items", {"title": "Check the other pages",
                                                                     "blocking": True})
        live = self.factory._entry((REPO, job["id"]))
        with self.factory._job_lock((REPO, job["id"])):
            self.factory._update(live, stage="needs-human", reason="the gate stopped it")
        refused = self.request("POST", f"/api/jobs/{REPO}/{job['id']}/approve")
        self.assert_error(refused, 409, "name every one in carry_items")
        self.assertEqual(refused.json()["open_blocking"], ["I-1"])
        self.assert_error(self.request("POST", f"/api/jobs/{REPO}/{job['id']}/approve", {"carry_items": "I-1"}),
                          400, "carry_items")
        approved = self.request("POST", f"/api/jobs/{REPO}/{job['id']}/approve", {"carry_items": ["I-1"]})
        self.assertEqual(approved.status, 200, approved.body)
        self.assertEqual(approved.json()["job"]["carried_items"], ["I-1"])

    def test_bulk_dismiss_with_one_reason(self) -> None:
        job = self.submit("Add a footer")
        base = f"/api/jobs/{REPO}/{job['id']}/items"
        refs = []
        for title in ("one", "two"):
            item = self.request("POST", base, {"title": title}).json()["item"]
            with self.factory._job_items(self.factory._entry((REPO, job["id"]))) as ledger:
                ledger.get(item["id"])["status"] = "proposed"
                ledger.dirty = True
            refs.append(item["ref"])
        result = self.request("POST", "/api/items/dismiss", {"refs": [*refs, f"job:{REPO}/{job['id']}/I-9"],
                                                             "reason": "narration, not promises"}).json()
        self.assertEqual([row["status"] for row in result["items"]], ["dropped", "dropped"])
        self.assertEqual([entry["ref"] for entry in result["refused"]], [f"job:{REPO}/{job['id']}/I-9"])
        self.assert_error(self.request("POST", "/api/items/dismiss", {"refs": refs}), 400, "reason")
        self.assert_error(self.request("POST", "/api/items/dismiss", {"refs": "x", "reason": "y"}), 400, "refs")

    def test_update_moves_an_item_by_its_verb(self) -> None:
        job = self.submit("Add a footer")
        item = self.request("POST", f"/api/jobs/{REPO}/{job['id']}/items", {"title": "x", "owner": "human"}).json()
        ref = item["item"]["ref"]
        self.assert_error(self.request("POST", "/api/items/update", {"ref": ref}), 400, "'to'")
        self.assert_error(self.request("POST", "/api/items/update", {"ref": ref, "to": "done", "evidence": "x"}),
                          400, "evidence")
        done = self.request("POST", "/api/items/update", {"ref": ref, "to": "done", "evidence": [
            {"kind": "file", "ref": "README.md"}]})
        self.assertEqual(done.json()["item"]["status"], "done")


class LookAroundRoutesTest(RealFactoryCase):
    """The look-around for model servers runs only when the owner asks: GET /api/setup and GET /api/doctor (what
    the GUI reads on load, every 15 s on the Grid and for the Doctor badge) never call listeners.model_servers;
    POST /api/setup/look-around calls it exactly once per request, for the master token only, and not again too
    soon (429 with Retry-After)."""

    def setUp(self) -> None:
        super().setUp()
        self.calls: list[set] = []

        def finder(skip_ports=frozenset(), *args: Any, **kwargs: Any) -> list[dict]:
            self.calls.append(set(skip_ports))
            return [{"address": "127.0.0.1", "port": 5005, "status": 200, "ids": ["qwen3.8-flash"]}]
        patcher = mock.patch("swfactory.listeners.model_servers", finder)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.moment = [1000.0]
        self.factory.clock = lambda: self.moment[0]
        with socket.socket() as sock:   # a configured local server that is down: the doctor used to look then
            sock.bind(("127.0.0.1", 0))
            down = sock.getsockname()[1]
        patched = self.request("POST", "/api/config", {
            "providers": {"tabby": {"type": "openai", "base_url": f"http://127.0.0.1:{down}/v1", "api_key_env": None}},
            "models": {"sonnet": {"provider": "tabby", "model": "x"}}})
        self.assertEqual(patched.status, 200, patched.body)

    def test_reading_the_setup_status_and_the_doctor_never_looks(self) -> None:
        for _ in range(3):
            host = self.request("GET", "/api/setup").json()["host"]
            self.assertEqual(set(host), {"key_dirs", "listening"})
        doctor_checks = {c["name"]: c for c in self.request("GET", "/api/doctor").json()["checks"]}
        self.assertIn("down: tabby", doctor_checks["local servers"]["detail"])
        self.assertIn('press "Look for model servers" on the setup page', doctor_checks["local servers"]["detail"])
        self.assertNotIn("model servers here", doctor_checks)
        self.assertEqual(self.calls, [], "nothing the GUI reads by itself looks for model servers")

    def test_the_owner_s_look_runs_once_per_request_and_is_rate_limited(self) -> None:
        looked = self.request("POST", "/api/setup/look-around", {})
        self.assertEqual(looked.status, 200, looked.body)
        result = looked.json()
        self.assertEqual(self.calls, [{8765, self.port}], "once, the harness's own ports skipped")
        self.assertEqual(result["model_servers"], [{"url": "http://127.0.0.1:5005/v1", "port": 5005, "status": 200,
                                                    "ids": ["qwen3.8-flash"]}])
        self.assertIn("Sent one GET /v1/models, without a key, to each program listening on this machine",
                      result["note"])
        self.assertRegex(result["at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.moment[0] += 1
        again = self.request("POST", "/api/setup/look-around", {})
        self.assert_error(again, 429, "You can look again in 29 seconds")
        self.assertEqual(again.headers["Retry-After"], "29")
        self.assertEqual(len(self.calls), 1, "a double click looks once")
        host = self.request("GET", "/api/setup").json()["host"]
        self.assertEqual((host["model_servers"], host["model_servers_at"]), (result["model_servers"], result["at"]),
                         "the setup status shows the owner's last look, with when it was taken")
        self.request("GET", "/api/doctor")
        self.assertEqual(len(self.calls), 1)
        self.moment[0] += 29
        self.assertEqual(self.request("POST", "/api/setup/look-around", {}).status, 200)
        self.assertEqual(len(self.calls), 2, "a later ask looks again, once")
        self.assert_error(self.request("GET", "/api/setup/look-around"), 405)
        self.assert_error(self.request("POST", "/api/setup/look-around", {}, token=None), 401)
        self.assertEqual(len(self.calls), 2)

    def test_an_agent_token_cannot_look(self) -> None:
        token = self.request("POST", "/api/agent-tokens", {"name": "qwen-code"}).json()["token"]
        self.assert_error(self.request("POST", "/api/setup/look-around", {}, token=token), 403,
                          "valid only for the item routes")
        self.assert_error(self.request("GET", "/api/setup", token=token), 403)
        self.assertEqual(self.calls, [])


class RunServerTest(unittest.TestCase):
    """run_server in the main thread: SIGTERM/SIGINT stop it, stop the factory and close event streams."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sf-run-server-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config = Config(self.tmp / "config.json", {"server": {"token": TOKEN}, "data_dir": str(self.tmp)})
        self.bus = CountingBus()
        self.factory = FakeFactory(self.config, self.bus)

    def run_until_signal(self, signum: int) -> tuple[str, list[bytes]]:
        out = io.StringIO()
        stream_tail: list[bytes] = []
        previous = signal.getsignal(signum)

        def driver() -> None:
            if not wait_until(lambda: "listening on http://" in out.getvalue()):
                os.kill(os.getpid(), signum)
                return
            port = int(out.getvalue().split("listening on http://127.0.0.1:")[1].split("/")[0])
            client = SseClient(port)
            client.block()
            self.assertTrue(wait_until(lambda: len(self.bus.open) == 1))
            os.kill(os.getpid(), signum)
            client.sock.settimeout(5)
            while True:
                block = client.block()
                if not block:
                    break
                stream_tail.append("\n".join(block).encode())
            stream_tail.append(b"EOF")
            client.close()

        thread = threading.Thread(target=driver, daemon=True)
        thread.start()
        with contextlib.redirect_stdout(out):
            srv.run_server(self.config, self.factory, self.bus, "127.0.0.1", 0)
        thread.join(10)
        self.assertIs(signal.getsignal(signum), previous)
        return out.getvalue(), stream_tail

    def test_sigterm_stops_everything(self) -> None:
        output, tail = self.run_until_signal(signal.SIGTERM)
        self.assertTrue(self.factory.stopped)
        self.assertIn("listening on http://127.0.0.1:", output)
        self.assertNotIn(TOKEN, output)
        self.assertEqual(tail[-1:], [b"EOF"])
        self.assertTrue(wait_until(lambda: not self.bus.open))

    def test_sigint_stops_everything(self) -> None:
        self.run_until_signal(signal.SIGINT)
        self.assertTrue(self.factory.stopped)

    def test_url_brackets_ipv6(self) -> None:
        if not socket.has_ipv6:
            self.skipTest("no IPv6")
        try:
            server = srv.make_server(self.config, self.factory, self.bus, "::1", 0)
        except OSError:
            self.skipTest("cannot bind ::1")
        self.addCleanup(server.server_close)
        self.assertEqual(server.url, f"http://[::1]:{server.server_port}/")
        self.assertIn("[::1]", server.allowed_hosts)


if __name__ == "__main__":
    unittest.main()


class MessagesRoutesTest(RealFactoryCase):
    """Wake & Messages (DESIGN §34): the event-source routes' own authentication (S10), and the inbox, notify and
    stop routes behind the master token."""

    def setUp(self) -> None:
        super().setUp()
        self.server.hook_fail_delay = 0.01

    def add_hook(self, **body: Any) -> str:
        response = self.request("POST", "/api/notify/hooks", {"name": "backup-script", "events": ["backup.failed",
                                                                                                 "backup.ok"],
                                                             "subjects": ["nightly"], **body})
        self.assertEqual(response.status, 201, response.body)
        return response.json()["token"]

    def hook(self, body: Any, token: str | None, path: str = "/api/hooks/event", **kwargs: Any) -> Response:
        return self.request("POST", path, body, token=token, **kwargs)

    def test_a_hook_token_reports_an_event_and_only_its_digest_is_stored(self) -> None:
        token = self.add_hook()
        saved = self.config.path.read_text()
        self.assertNotIn(token, saved)
        self.assertIn(hashlib.sha256(token.encode()).hexdigest(), saved)
        response = self.hook({"kind": "backup.failed", "subject": "nightly"}, token)
        self.assertEqual(response.status, 202, response.body)
        message = response.json()["message"]
        self.assertRegex(message, r"^M-\d{6}$")
        inbox = self.request("GET", "/api/messages").json()
        self.assertEqual(inbox["messages"][0]["body"], "The nightly backup failed. See the cockpit for what to do.")
        self.assertEqual(self.hook({"kind": "backup.failed", "subject": "nightly"}, token).json(),
                         {"message": None, "deduped": True})

    def test_the_master_token_a_query_token_and_unknown_tokens_are_refused(self) -> None:
        self.add_hook()
        self.assert_error(self.hook({"kind": "backup.failed"}, TOKEN), 401, "master token is not accepted")
        self.assert_error(self.hook({"kind": "backup.failed"}, "wrong-token-" * 3), 401)
        self.assert_error(self.hook({"kind": "backup.failed"}, None), 401)
        self.assert_error(self.hook({"kind": "backup.failed"}, None, path="/api/hooks/event?token=abc"), 400,
                          "never in the URL")
        self.assertEqual(self.request("GET", "/api/hooks/event", token=None).status, 405)
        notices = self.request("GET", "/api/messages?kind=notice").json()["messages"]
        self.assertIn("wrong event-source token", notices[0]["body"])

    def test_scope_size_budget_and_idempotency(self) -> None:
        token = self.add_hook(per_minute=3)
        self.assert_error(self.hook({"kind": "service.down"}, token), 403, "may not report service.down")
        self.assert_error(self.hook(None, token, raw=b'{"kind": "backup.ok", "detail": "' + b"x" * 2100 + b'"}'), 413)
        first = self.hook({"kind": "backup.failed"}, token, headers={"Idempotency-Key": "run-7"})
        again = self.hook({"kind": "backup.failed"}, token, headers={"Idempotency-Key": "run-7"})
        self.assertEqual(first.json(), again.json())
        limited = self.hook({"kind": "backup.ok"}, token)
        self.assertEqual(limited.status, 429)
        self.assertGreaterEqual(int(limited.headers["Retry-After"]), 1)

    def test_wrong_tokens_never_block_a_valid_one(self) -> None:
        token = self.add_hook()
        for _ in range(15):
            self.hook({"kind": "backup.failed"}, "x" * 43)
        self.assertEqual(self.hook({"kind": "backup.ok", "subject": "nightly"}, token).status, 202)

    def test_the_host_check_still_applies(self) -> None:
        token = self.add_hook()
        self.assert_error(self.hook({"kind": "backup.failed"}, token, host="evil.example"), 403, "host not allowed")

    def test_rotate_and_delete(self) -> None:
        token = self.add_hook()
        rotated = self.request("POST", "/api/notify/hooks/backup-script/rotate").json()
        self.assertEqual(self.hook({"kind": "backup.failed"}, token).status, 401)
        self.assertEqual(self.hook({"kind": "backup.failed"}, rotated["token"]).status, 202)
        self.assertEqual(self.request("POST", "/api/notify/hooks/backup-script/delete").status, 200)
        self.assertEqual(self.hook({"kind": "backup.failed"}, rotated["token"]).status, 401)
        self.assertEqual(self.request("GET", "/api/notify/hooks").json()["hooks"], [])

    def test_inbox_routes_need_the_session(self) -> None:
        for method, path in (("GET", "/api/messages"), ("GET", "/api/notify"), ("POST", "/api/notify/events"),
                             ("POST", "/api/wakes/control"), ("GET", "/api/notify/terms")):
            self.assertEqual(self.request(method, path, {} if method == "POST" else None, token=None).status, 401,
                             path)
        self.assertEqual(self.request("GET", "/api/messages/M-12").status, 400)

    def test_owner_event_ack_and_the_events_carry_no_text(self) -> None:
        response = self.request("POST", "/api/notify/events", {"kind": "backup.failed", "subject": "nightly",
                                                               "detail": "disk sdb1 is failing"})
        self.assertEqual(response.status, 202)
        message = response.json()["message"]
        detail = self.request("GET", f"/api/messages/{message}").json()
        self.assertIn("disk sdb1 is failing", detail["message"]["body"])
        acked = self.request("POST", f"/api/messages/{message}/ack").json()["message"]
        self.assertFalse(acked["unread"])
        dumped = json.dumps([event.to_dict() for event in self.bus.since(0)])
        self.assertIn("notify.queued", dumped)
        self.assertNotIn("sdb1", dumped)

    def test_stop_and_restart_and_the_acknowledgement(self) -> None:
        self.assertIsNotNone(self.request("POST", "/api/wakes/control", {"action": "pause"}).json()["paused"])
        self.assertTrue((self.data / "wakes" / "PAUSED").exists())
        self.assertIsNone(self.request("POST", "/api/wakes/control", {"action": "resume"}).json()["paused"])
        self.assert_error(self.request("POST", "/api/wakes/acknowledge", {"text": "ok"}), 400, "type exactly")
        sentence = "I understand my AI agents can read my messages and send them anywhere"
        self.assertTrue(self.request("POST", "/api/wakes/acknowledge", {"text": sentence}).json()["acknowledged_at"])
        self.assertIsNone(self.request("POST", "/api/wakes/acknowledge", {"withdraw": True}).json()["acknowledged_at"])

    def test_the_sensitive_words_need_the_sandbox_and_a_channel_needs_it_too(self) -> None:
        self.assert_error(self.request("POST", "/api/notify/terms", {"text": "diabetes"}), 400, "sandbox")
        self.assertTrue(self.request("POST", "/api/notify/terms", {"none": True}).json()["none_declared"])
        patch = {"wakes": {"timezone": "Europe/London"},
                 "notify": {"channels": {"telegram": {"type": "telegram", "token_env": "SERAPHINA_TELEGRAM_BOT_TOKEN",
                                                      "model_text": "never"}}}}
        self.assert_error(self.request("POST", "/api/config", patch), 400, "phone messages stay off")
        self.assertEqual(self.request("POST", "/api/config", {**patch, "harness": {"sandbox": "bwrap"}}).status, 200)
        channels = self.request("GET", "/api/notify/channels").json()["channels"]
        self.assertEqual([(c["name"], c["privacy"]) for c in channels], [("telegram", "third_party")])


class HookLimiterTest(unittest.TestCase):
    def test_per_minute_and_per_day(self) -> None:
        now = [1000.0]
        day = ["2026-10-21"]
        limiter = srv.HookLimiter(monotonic=lambda: now[0], today=lambda: day[0])
        self.assertEqual([limiter.take("a", 2, 3) for _ in range(3)][:2], [None, None])
        self.assertGreater(limiter.take("a", 2, 3) or 0, 0)
        self.assertIsNone(limiter.take("b", 2, 3), "each source has its own budget")
        now[0] += 61
        self.assertIsNone(limiter.take("a", 2, 3))
        self.assertEqual(limiter.take("a", 5, 3), 3600.0, "the day's budget is spent")
        day[0] = "2026-10-22"
        self.assertIsNone(limiter.take("a", 5, 3))


class WakeRoutesTest(RealFactoryCase):
    """Wake & Messages W2/W3: /api/hooks/wake (S10), the S46 sandbox precondition on the settings patch, and the
    session on every new route."""

    PATCH = {"harness": {"sandbox": "bwrap"},
             "wakes": {"enabled": True, "timezone": "Europe/London", "time_sync": {"require": False},
                       "personas": {"seraphina": {"enabled": True, "model": None}}}}

    def setUp(self) -> None:
        super().setUp()
        self.server.hook_fail_delay = 0.01

    def enable(self) -> None:
        response = self.request("POST", "/api/config", self.PATCH)
        self.assertEqual(response.status, 200, response.body)

    def add_hook(self, **body: Any) -> str:
        response = self.request("POST", "/api/notify/hooks", {"name": "home-assistant", "may": ["wake"],
                                                             "personas": ["seraphina"], **body})
        self.assertEqual(response.status, 201, response.body)
        return response.json()["token"]

    def wake(self, body: Any, token: str | None, path: str = "/api/hooks/wake", **kwargs: Any) -> Response:
        return self.request("POST", path, body, token=token, **kwargs)

    def test_personas_need_the_sandbox_s46(self) -> None:
        no_sandbox = {"wakes": self.PATCH["wakes"]}
        self.assert_error(self.request("POST", "/api/config", no_sandbox), 400, "stay off")
        chat_wakes = {"wakes": {"chat_wakes": {"enabled": True}}}
        self.assert_error(self.request("POST", "/api/config", chat_wakes), 400, "stay off")
        self.enable()

    def test_local_attested_cannot_be_set_through_the_config(self) -> None:
        self.enable()
        patch = {"wakes": {"servers": {"local": {"local_attested": True}}}}
        self.assertEqual(self.request("POST", "/api/config", patch).status, 400)
        self.assertNotIn("local_attested", self.config.path.read_text())

    def test_the_hook_wake_route_is_scoped_sized_and_idempotent(self) -> None:
        self.enable()
        token = self.add_hook()
        self.assert_error(self.wake({"persona": "seraphina"}, TOKEN), 401, "master token is not accepted")
        self.assert_error(self.wake({"persona": "seraphina"}, None, path="/api/hooks/wake?token=" + token), 400,
                          "never in the URL")
        self.assert_error(self.wake(None, token, raw=b'{"persona": "seraphina", "reason": "' + b"x" * 2100 + b'"}'),
                          413)
        self.assert_error(self.wake({"persona": "kate"}, token), 403, "may not wake that persona")
        self.assert_error(self.wake({"persona": "seraphina", "approve": True}, token), 400, "the body is")
        first = self.wake({"persona": "seraphina", "reason": "the front door opened"}, token,
                          headers={"Idempotency-Key": "door-1"})
        self.assertEqual(first.status, 202, first.body)
        self.assertRegex(first.json()["wake"], r"^W-\d{6}$")
        again = self.wake({"persona": "seraphina", "reason": "the front door opened"}, token,
                          headers={"Idempotency-Key": "door-1"})
        self.assertEqual(again.json(), first.json())
        queue = self.request("GET", "/api/wakes").json()
        self.assertIn(first.json()["wake"], json.dumps(queue))
        dumped = json.dumps([event.to_dict() for event in self.bus.since(0)])
        self.assertIn("wake.requested", dumped)
        self.assertNotIn("front door", dumped, "a wake's reason is never in an event (S27)")
        events_only = self.request("POST", "/api/notify/hooks", {"name": "backup-script", "events": ["backup.ok"]})
        self.assert_error(self.wake({"persona": "seraphina"}, events_only.json()["token"]), 403,
                          "may not wake a persona")

    def test_every_new_route_needs_the_session(self) -> None:
        routes = [("GET", "/api/wakes"), ("POST", "/api/wakes"), ("GET", "/api/wakes/reminders"),
                  ("POST", "/api/wakes/reminders"), ("POST", "/api/wakes/reminders/R-000001/cancel"),
                  ("POST", "/api/wakes/servers/local/attest"), ("POST", "/api/wakes/W-000001/cancel"),
                  ("POST", "/api/personas/seraphina/soul"), ("POST", "/api/personas/seraphina/privacy"),
                  ("POST", "/api/personas/seraphina/integrity/approve"), ("POST", "/api/personas/seraphina/memory"),
                  ("POST", "/api/personas/seraphina/memory/rollback"),
                  ("GET", "/api/personas/seraphina/memory/versions/1"),
                  ("POST", "/api/personas/seraphina/consolidation/accept"),
                  ("POST", "/api/personas/seraphina/journal/J-000001/retract"),
                  ("POST", "/api/personas/seraphina/forget"), ("POST", "/api/personas/seraphina/messages"),
                  ("POST", "/api/personas/seraphina/sound"), ("POST", "/api/personas/seraphina/reimport"),
                  ("POST", "/api/personas/seraphina/reimport/accept"),
                  ("GET", "/api/personas/seraphina/runs/01-wake"), ("GET", "/api/personas/seraphina/external"),
                  ("GET", "/api/personas/seraphina/external/versions/" + "a" * 64),
                  ("GET", "/api/personas/seraphina/external/diff"),
                  ("POST", "/api/personas/seraphina/external/restore"),
                  ("POST", "/api/personas/seraphina/external/erase"),
                  ("POST", "/api/personas/seraphina/before-import"), ("GET", "/api/chats/c-1/wakes"),
                  ("POST", "/api/chats/c-1/wakes"), ("GET", "/api/notify/rules"), ("POST", "/api/notify/rules")]
        for method, path in routes:
            with self.subTest(path=path, method=method):
                self.assertEqual(self.request(method, path, {} if method == "POST" else None, token=None).status, 401)

    def test_a_chat_s_check_in_switch_reads_back(self) -> None:
        """GET /api/chats/<chat>/wakes says whether the chat may schedule check-ins (the owner's switch), so the
        cockpit shows the switch as it is after a reload, not only in the answer to the POST that set it."""
        created = self.request("POST", "/api/chats", {"repo": REPO})
        self.assertEqual(created.status, 201, created.body)
        chat = created.json()["chat"]["id"]
        self.assertEqual(self.request("GET", f"/api/chats/{chat}/wakes").json(), {"chat": chat, "allow": False, "wakes": []})
        on = self.request("POST", f"/api/chats/{chat}/wakes", {"allow": True})
        self.assertEqual(on.json(), {"chat": chat, "allow": True, "wakes": []})
        self.assertEqual(self.request("GET", f"/api/chats/{chat}/wakes").json(), {"chat": chat, "allow": True, "wakes": []})
        self.request("POST", f"/api/chats/{chat}/wakes", {"allow": False})
        self.assertIs(self.request("GET", f"/api/chats/{chat}/wakes").json()["allow"], False)

    def test_the_read_ledger_routes(self) -> None:
        """memory.external.ledger: list the versions per file, show one, diff two, prepare a restore; bad digests
        are refused before anything is read, and nothing writes her store."""
        self.enable()
        root = self.tmp / "her-memory"
        root.mkdir()
        note = root / "pets.md"
        store = self.factory._wake_manager().store
        for text in ("Pip is 3.\n", "Pip is 4.\n"):
            note.write_text(text)
            record = store.external_read("seraphina", wake="W-000001")
            personas.read_external_memory({"folder": str(root)}, 1000, roots=[root], record=record)
            record.commit()
        listing = self.request("GET", "/api/personas/seraphina/external").json()
        self.assertEqual((listing["ledger"], listing["label"], listing["reads"]), (False, "private", 2))
        newer, older = (row["sha256"] for row in listing["files"][0]["versions"])
        shown = self.request("GET", f"/api/personas/seraphina/external/versions/{older}").json()
        self.assertEqual((shown["text"], shown["names"]), ("Pip is 3.\n", ["pets.md"]))
        diff = self.request("GET", f"/api/personas/seraphina/external/diff?from={older}&to={newer}").json()
        self.assertIn("+Pip is 4.", diff["diff"])
        restore = self.request("POST", "/api/personas/seraphina/external/restore", {"sha256": older}).json()
        self.assertEqual((restore["restore"]["text"], restore["restore"]["current"]["sha256"]), ("Pip is 3.\n", newer))
        self.assertEqual(note.read_text(), "Pip is 4.\n", "the harness never writes her store")
        self.assert_error(self.request("GET", "/api/personas/seraphina/external/versions/" + "0" * 64), 404)
        self.assert_error(self.request("GET", "/api/personas/seraphina/external/versions/nothex"), 400, "sha256")
        self.assert_error(self.request("GET", "/api/personas/seraphina/external/diff?from=x&to=y"), 400, "sha256")
        self.assert_error(self.request("POST", "/api/personas/seraphina/external/restore", {"sha256": "x"}), 400)
        self.assert_error(self.request("POST", "/api/personas/seraphina/external/restore",
                                       {"sha256": older, "file": 3}), 400, "file")
        self.assert_error(self.request("GET", "/api/personas/ghost/external"), 404)
        erased = self.request("POST", "/api/personas/seraphina/external/erase", {"sha256": older})
        self.assertEqual(erased.status, 200, erased.body)
        self.assertEqual((erased.json()["erased"]["sha256"], erased.json()["erased"]["copies"]), (older, 1))
        self.assert_error(self.request("GET", f"/api/personas/seraphina/external/versions/{older}"), 404, "erased")
        listing = self.request("GET", "/api/personas/seraphina/external").json()
        self.assertEqual([row["sha256"] for row in listing["erased"]], [older])
        self.assert_error(self.request("POST", "/api/personas/seraphina/external/erase", {"sha256": "x"}), 400)
        self.assert_error(self.request("POST", "/api/personas/seraphina/external/erase", {"sha256": "2" * 64}), 404)
        self.assertEqual(note.read_text(), "Pip is 4.\n", "erasing never touches her store")

    def test_the_harness_s_own_writes_never_trip_the_settings_alarm(self) -> None:
        """S47: a reminder, a clean confirmation and a rulebook change through the cockpit are the new baseline; a
        hand edit of the rulebook afterwards is caught."""
        self.enable()
        notifier = self.factory._notify_manager()
        notifier.start(threads=False)
        self.addCleanup(notifier.stop)
        added = self.request("POST", "/api/wakes/reminders", {"at": "2026-10-22T17:00:00Z", "text": "Call the plumber"})
        self.assertEqual(added.status, 201, added.body)
        reminder = added.json()["reminder"]["id"]
        self.assertEqual(self.request("POST", f"/api/wakes/reminders/{reminder}/cancel").status, 200)
        persona = self.request("GET", "/api/personas/seraphina").json()
        sha = json.dumps(persona)
        import re

        file_sha = re.search(r'"file_sha256": "([0-9a-f]{64})"', sha).group(1)
        confirmed = self.request("POST", "/api/personas/seraphina/privacy", {"what": "soul", "ref": file_sha})
        self.assertEqual(confirmed.status, 200, confirmed.body)
        self.assertEqual(self.request("POST", "/api/notify/rules", {"quiet_hours": "22:30-07:00"}).status, 200)
        notifier.tick()
        self.assertIsNone(self.factory.notify_tamper(), "the harness's own writes are not hand edits")
        rules_file = self.config.path.parent / "notice-rules.json"
        rules_file.write_text(rules_file.read_text().replace("22:30-07:00", "00:00-00:01"))
        notifier.tick()
        self.assertEqual(self.factory.notify_tamper()["what"], ["rules"])

    def test_run_now_and_cancel_through_the_api(self) -> None:
        self.enable()
        wake = self.request("POST", "/api/wakes", {"persona": "seraphina", "reason": "check the backups"})
        self.assertEqual(wake.status, 202, wake.body)
        wake_id = wake.json()["wake"]["id"]
        self.assertEqual(self.request("POST", f"/api/wakes/{wake_id}/cancel").status, 200)
        self.assertNotIn(wake_id, json.dumps(self.request("GET", "/api/wakes").json().get("queue")))
