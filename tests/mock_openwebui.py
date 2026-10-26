"""A mock Open WebUI (0.12.3 as docs/seraphina/research/openwebui-profiles.md reads its source) for tests.

It listens on 127.0.0.1 (random port) and mimics what the harness relies on, for an API caller that sends
no ``chat_id`` (DESIGN.md section 33):

- ``GET /api/version`` (no key): ``{"version": ...}``.
- ``GET /api/models`` (``/api/v1/models``) with a key: base models plus workspace models with ``preset:
  true`` and ``info`` (``version_id``, ``base_model_id``, ``meta``; ``params`` only as ``model_controls``
  reduced as ``utils/models.py public_model_params`` does: each control's label, description, default and
  display, and only each option's label; the system prompt and settings are never listed). The admin's
  Model Defaults metadata (``default_metadata``) is merged into every listed model's ``meta`` as
  ``utils/models.py`` does (capabilities key by key, other keys only where the model has none), never into
  the export. 401 without a known key.
- ``GET /api/v1/models/export?ids=`` (``/api/v1/models/export``): the full records, for a key with write
  access (``writer``); 403 otherwise.
- ``GET /api/v1/configs/models``: ``{"DEFAULT_MODEL_PARAMS": ...}`` for an admin key; 403 otherwise.
- ``POST /api/chat/completions`` (``/api/v1/chat/completions``): the preset applied the way §1 of the
  research describes, then a scripted "upstream" model answers (``upstream`` records each request as
  TabbyAPI would get it):
  - one system message at position 0, the preset's prompt first, then the caller's (``f"{preset}\\n{caller}"``),
    with placeholders filled from the request's ``variables`` first and then server-side (``{{USER_NAME}}``
    from the key's user; ``{{CURRENT_DATE}}`` from the server's clock); ``params.system`` in the request
    does not override the preset;
  - the preset's settings fill only what the request left out (each model control's default option
    deep-merged into custom_params, custom_params parsed and sent top-level, Open WebUI-only keys never sent
    upstream, ``stop`` as stored: on this path Open WebUI does not unescape it);
  - the preset's ``stream_response``, when set, overrides the request's ``stream``: off means one whole
    JSON answer even to a stream request;
  - ``files`` (knowledge) adds a ``{"sources": [...]}`` first SSE frame (or a top-level ``sources``);
  - the caller's ``tools`` pass through and the upstream ``tool_calls`` come back unexecuted;
  - ``model`` becomes the preset's ``base_model_id`` upstream; an unknown model is a 400.
  ``forbidden`` records every request that carried ``tool_ids``, ``chat_id``, ``id``, ``session_id`` or
  ``features`` (which switches on Open WebUI's built-in memory, among others).
- Anything that would change Open WebUI's state (a POST other than chat completions, any PUT, PATCH or
  DELETE: creating or updating a model, a tool, a function, a knowledge base, a setting) is recorded in
  ``writes`` as ``(method, path)`` and refused (404 or 405): the harness never writes there (DESIGN §33,
  the capability map's acceptance test).

Example::

    with MockOpenWebUI() as owui:
        owui.add_workspace_model("kate", system="You are Kate. Hello {{USER_NAME}}.", params={"temperature": 0.6})
        provider = {"type": "openai", "kind": "openwebui", "base_url": owui.base_url + "/api",
                    "api_key_env": "OPENWEBUI_API_KEY"}
        ...
        assert owui.upstream[0]["messages"][0]["content"].startswith("You are Kate.")
"""

from __future__ import annotations

import copy
import itertools
import json
import threading
import urllib.parse
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from tests.mock_llm import _normalise, _openai_events, _openai_response, _sse

OWNER_KEY = "sk-owner-0123456789abcdef0123456789abcdef"
READER_KEY = "sk-reader-0123456789abcdef0123456789abcd"
OPENWEBUI_ONLY = ("stream_response", "stream_delta_chunk_size", "function_calling", "reasoning_tags",
                  "compact_token_threshold", "system", "note_id", "tool_approval_mode", "model_controls")
FORBIDDEN = ("tool_ids", "chat_id", "id", "session_id", "features")
Reply = Callable[[dict], "dict | None"]


class MockOpenWebUI:
    """See the module docstring. keys: {key: {"name", "admin", "writer"}}; reply(upstream_body) -> a mock_llm
    reply dict (default: "ECHO <last user text>")."""

    def __init__(self, keys: dict | None = None, version: str = "0.12.3", reply: Reply | None = None,
                 default_params: dict | None = None, default_metadata: dict | None = None) -> None:
        self.keys = keys if keys is not None else {
            OWNER_KEY: {"name": "Owner", "admin": True, "writer": True},
            READER_KEY: {"name": "Reader", "admin": False, "writer": False}}
        self.version = version
        self.reply = reply
        self.default_params = dict(default_params or {})
        self.default_metadata = dict(default_metadata or {})   # the admin's Model Defaults metadata
        self.records: dict[str, dict] = {}          # workspace models, as the export returns them
        self.base_models: list[dict] = [{"id": "qwen3.8-flash", "name": "qwen3.8-flash", "object": "model",
                                         "owned_by": "openai", "connection_type": "local"}]
        self.requests: list[dict] = []              # every POST /api/chat/completions body, as received
        self.upstream: list[dict] = []              # what the upstream server got for each
        self.forbidden: list[dict] = []             # requests that carried a FORBIDDEN key
        self.gets: list[tuple[str, str | None]] = []   # (path, the key used or None)
        self.writes: list[tuple[str, str]] = []       # (method, path) of every request that would change state
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- the workspace models ---------------------------------------------------------

    def add_relay_connection(self, model_id: str = "gpt-6.1-codex") -> dict:
        """A base model on a connection an admin labelled local that is a relay to a hosted service (a proxy
        signed in to a subscription, in a container on the same machine): Open WebUI lists it like any local
        model. Returns its listing entry."""
        entry = {"id": model_id, "name": model_id, "object": "model", "owned_by": "openai",
                 "connection_type": "local"}
        self.base_models.append(entry)
        return entry

    def add_workspace_model(self, model_id: str, *, system: str = "", params: dict | None = None,
                            meta: dict | None = None, name: str | None = None,
                            base_model_id: str = "qwen3.8-flash") -> dict:
        """Add (or replace) a workspace model; returns its export record."""
        record = {"id": model_id, "user_id": "u-owner", "base_model_id": base_model_id, "name": name or model_id.capitalize(),
                  "params": {**({"system": system} if system else {}), **(params or {})},
                  "meta": {"profile_image_url": "data:image/png;base64,AAAA", "description": f"{model_id} test model",
                           **(meta or {})},
                  "access_grants": [{"id": "g1", "resource_type": "model", "resource_id": model_id,
                                     "principal_type": "user", "principal_id": "u-owner", "permission": "write"}],
                  "is_active": True, "created_at": 1750000000, "updated_at": 1760000000,
                  "version_id": uuid.uuid4().hex, "background_image_data": None}
        with self._lock:
            self.records[model_id] = record
        return copy.deepcopy(record)

    def edit(self, model_id: str, *, system: str | None = None, params: dict | None = None,
             meta: dict | None = None, name: str | None = None) -> str:
        """Change a workspace model as Open WebUI's editor would: a new version_id. Returns it."""
        with self._lock:
            record = self.records[model_id]
            if system is not None:
                record["params"]["system"] = system
            if params:
                record["params"].update(params)
            if meta:
                record["meta"].update(meta)
            if name is not None:
                record["name"] = name
            record["version_id"] = uuid.uuid4().hex
            record["updated_at"] += 60
            return record["version_id"]

    def export_file(self, *model_ids: str) -> list[dict]:
        """The UI's Export file: the same shape as GET /api/v1/models/export."""
        with self._lock:
            return [copy.deepcopy(self.records[model_id]) for model_id in (model_ids or self.records)]

    # -- serving --------------------------------------------------------------------------

    def start(self) -> str:
        if self._server is None:
            server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
            server.daemon_threads = True
            server.mock = self  # type: ignore[attr-defined]
            self._server = server
            self._thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                                            name="mock-openwebui", daemon=True)
            self._thread.start()
        return self.base_url

    @property
    def base_url(self) -> str:
        if self._server is None:
            raise RuntimeError("MockOpenWebUI is not started")
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            if self._thread is not None:
                self._thread.join(timeout=5)
            self._server = self._thread = None

    def __enter__(self) -> "MockOpenWebUI":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # -- what Open WebUI does --------------------------------------------------------------

    def listing(self) -> list[dict]:
        with self._lock:
            records = [copy.deepcopy(record) for record in self.records.values()]
        found = [dict(model) for model in self.base_models]
        for record in records:
            meta = {key: value for key, value in record["meta"].items() if key != "profile_image_url"}
            params = record.get("params") or {}
            info = {"version_id": record["version_id"], "id": record["id"], "user_id": record["user_id"],
                    "base_model_id": record["base_model_id"], "name": record["name"], "meta": meta,
                    "access_grants": record["access_grants"], "is_active": True, "updated_at": record["updated_at"],
                    "created_at": record["created_at"]}
            if params.get("model_controls"):
                info["params"] = {"model_controls": {
                    key: {**{name: control[name] for name in ("label", "description", "default", "display")
                             if name in control},
                          "options": {option: {"label": value.get("label")}
                                      for option, value in (control.get("options") or {}).items()}}
                    for key, control in params["model_controls"].items()}}
            found.append({"id": record["id"], "name": record["name"], "object": "model", "created": record["created_at"],
                          "owned_by": "openai", "connection_type": "local", "preset": True, "info": info,
                          "actions": [], "filters": [], "tags": []})
        for model in found:
            if not self.default_metadata:
                break
            meta = model.setdefault("info", {}).setdefault("meta", {})
            for key, value in self.default_metadata.items():
                if key == "capabilities":
                    meta["capabilities"] = {**copy.deepcopy(value), **(meta.get("capabilities") or {})}
                elif meta.get(key) is None:
                    meta[key] = copy.deepcopy(value)
        return found

    def apply_preset(self, body: dict, user: dict) -> tuple[dict, bool, list]:
        """(the upstream body, whether to stream, the sources) for one chat request."""
        with self._lock:
            record = copy.deepcopy(self.records.get(body.get("model")))
        base_ids = {model["id"] for model in self.base_models}
        if record is None and body.get("model") not in base_ids:
            raise LookupError("Model not found")
        upstream = {key: copy.deepcopy(value) for key, value in body.items()
                    if key not in ("files", "filter_ids", "variables", "params", *FORBIDDEN)}
        stream = bool(body.get("stream"))
        sources = [{"source": {"id": entry.get("id"), "type": entry.get("type")}, "document": ["…"]}
                   for entry in body.get("files") or [] if isinstance(entry, dict)]
        if record is None:
            return upstream, stream, sources
        params = record.get("params") or {}
        upstream["model"] = record["base_model_id"]
        # main.py: the defaults, the preset's params over them (custom_params key by key); apply_model_controls:
        # each control's default option deep-merged into custom_params; apply_params_to_form_data: custom_params
        # parsed and deep-merged over the params, Open WebUI-only keys dropped, then fill-only top-level fields.
        merged = {**self.default_params, **{k: v for k, v in params.items() if k != "custom_params"}}
        custom = {**(self.default_params.get("custom_params") or {}), **(params.get("custom_params") or {})}
        for control in (params.get("model_controls") or {}).values():   # the caller picks none: the defaults
            option = (control.get("options") or {}).get(control.get("default")) or {}
            custom = _deep_update(custom, copy.deepcopy(option.get("params") or {}))
        settings = {key: value for key, value in merged.items() if key not in OPENWEBUI_ONLY and value is not None}
        for key, value in custom.items():
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except ValueError:
                    pass
            settings = _deep_update(settings, {key: value})
        for key, value in settings.items():
            if key not in upstream:
                upstream[key] = value
        if isinstance(params.get("stream_response"), bool):
            stream = params["stream_response"]
        upstream["stream"] = stream
        preset = self.render(params.get("system") or "", body.get("variables") or {}, user)
        messages = [dict(m) for m in upstream.get("messages") or []]
        caller = [m for m in messages if m.get("role") == "system"]
        rest = [m for m in messages if m.get("role") != "system"]
        texts = [preset] if preset else []
        texts += [self.render(str(m.get("content") or ""), body.get("variables") or {}, user) for m in caller]
        upstream["messages"] = ([{"role": "system", "content": "\n".join(texts)}] if texts else []) + rest
        return upstream, stream, sources

    @staticmethod
    def render(text: str, variables: dict, user: dict) -> str:
        for key, value in variables.items():
            text = text.replace(str(key), str(value))
        return (text.replace("{{USER_NAME}}", user.get("name", ""))
                .replace("{{CURRENT_DATE}}", datetime.now(timezone.utc).strftime("%Y-%m-%d")))

    def answer(self, upstream: dict) -> dict:
        if self.reply is not None:
            found = self.reply(upstream)
            if found is not None:
                return found
        last = next((m for m in reversed(upstream.get("messages") or []) if m.get("role") == "user"), {})
        content = last.get("content")
        text = content if isinstance(content, str) else json.dumps(content)
        return {"text": f"ECHO {text}"}


def _deep_update(base: dict, update: dict) -> dict:
    """utils/misc.py deep_update: dicts merge key by key, anything else replaces."""
    merged = dict(base)
    for key, value in update.items():
        merged[key] = (_deep_update(merged[key] if isinstance(merged.get(key), dict) else {}, value)
                       if isinstance(value, dict) else value)
    return merged


class _Handler(BaseHTTPRequestHandler):
    server_version = "MockOpenWebUI/0.12.3"

    def log_message(self, message_format: str, *args: Any) -> None:
        pass

    @property
    def mock(self) -> MockOpenWebUI:
        return self.server.mock  # type: ignore[attr-defined]

    def _key(self) -> str | None:
        auth = self.headers.get("Authorization") or ""
        return auth[7:].strip() if auth.lower().startswith("bearer ") else (self.headers.get("x-api-key") or None)

    def _user(self) -> dict | None:
        key = self._key()
        return self.mock.keys.get(key) if key else None

    def do_GET(self) -> None:
        split = urllib.parse.urlsplit(self.path)
        path, query = split.path, urllib.parse.parse_qs(split.query)
        self.mock.gets.append((path, self._key()))
        if path == "/api/version":
            return self._json(200, {"version": self.mock.version})
        user = self._user()
        if user is None:
            return self._json(401, {"detail": "Not authenticated"})
        if path in ("/api/models", "/api/v1/models"):
            return self._json(200, {"data": self.mock.listing()})
        if path == "/api/v1/models/export":
            if not user.get("writer"):
                return self._json(403, {"detail": "You do not have permission to access this resource."})
            ids = [part for value in query.get("ids", []) for part in value.split(",") if part]
            with self.mock._lock:
                found = [copy.deepcopy(r) for i, r in self.mock.records.items() if not ids or i in ids]
            return self._json(200, found)
        if path == "/api/v1/configs/models":
            if not user.get("admin"):
                return self._json(401, {"detail": "Unauthorized"})
            return self._json(200, {"DEFAULT_MODELS": "", "MODEL_ORDER_LIST": [],
                                    "DEFAULT_MODEL_PARAMS": copy.deepcopy(self.mock.default_params)})
        return self._json(404, {"detail": "Not Found"})

    def _refuse_write(self, method: str) -> None:
        path = urllib.parse.urlsplit(self.path).path
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        with self.mock._lock:
            self.mock.writes.append((method, path))
        self._json(405 if method != "POST" else 404, {"detail": "Not Found"})

    def do_PUT(self) -> None:
        self._refuse_write("PUT")

    def do_PATCH(self) -> None:
        self._refuse_write("PATCH")

    def do_DELETE(self) -> None:
        self._refuse_write("DELETE")

    def do_POST(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if path not in ("/api/chat/completions", "/api/v1/chat/completions"):
            with self.mock._lock:
                self.mock.writes.append(("POST", path))
            return self._json(404, {"detail": "Not Found"})
        user = self._user()
        if user is None:
            return self._json(401, {"detail": "Not authenticated"})
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return self._json(400, {"detail": "invalid JSON"})
        with self.mock._lock:
            self.mock.requests.append(copy.deepcopy(body))
            if any(key in body for key in FORBIDDEN):
                self.mock.forbidden.append(copy.deepcopy(body))
        try:
            upstream, stream, sources = self.mock.apply_preset(body, user)
        except LookupError as exc:
            return self._json(400, {"detail": str(exc)})
        with self.mock._lock:
            self.mock.upstream.append(copy.deepcopy(upstream))
        reply = _normalise(self.mock.answer(upstream), "openai", _Ids(self.mock))
        if not stream:
            payload = _openai_response(reply, {"model": body.get("model")})
            if sources:
                payload["sources"] = sources
            return self._json(200, payload)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        if sources:
            self.wfile.write(_sse({"sources": sources}).encode())
        include = bool((upstream.get("stream_options") or {}).get("include_usage"))
        for chunk in _openai_events(reply, {"model": body.get("model")}, include):
            self.wfile.write(chunk.encode())
            self.wfile.flush()

    def _json(self, status: int, payload: Any) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class _Ids:
    """What mock_llm._normalise needs of a MockLLM: fresh tool-call ids."""

    def __init__(self, mock: MockOpenWebUI) -> None:
        self._mock = mock

    def _new_id(self, prefix: str) -> str:
        return f"{prefix}{next(self._mock._ids)}"
