"""HTTP server: the JSON API, the live event stream (SSE), token auth and the static GUI.

Every request passes the Host check (DNS-rebinding guard) first. Everything except
the login routes and the GUI's non-HTML assets needs the token, as an
``Authorization: Bearer`` header or the ``sf_token`` cookie; an unauthenticated
page request gets the login page. POSTs must be JSON (the login form may also post
form-encoded) and, when they carry an Origin, come from the same host.

Handlers call Factory methods and map their exceptions to status codes:
ActionError 409, KeyError 404, ValueError 400, anything else 500 (logged to the
event bus, never echoed to the client). An ActionError with a ``payload`` dict
(approve with must-do items unnamed) adds its keys to the error body.

An outside agent's token (``Authorization: Bearer sfi_…``, made on the agent-tokens
routes; DESIGN.md section 32) is accepted on the item routes only (AGENT_TOKEN_ROUTES):
it lists only its own items (system scope and follow-ups), adds items there as
``agent:<name>`` and updates its own items; anything else is 403.
"""

from __future__ import annotations

import base64
import copy
import dataclasses
import hashlib
import hmac
import json
import os
import queue
import re
import signal
import socket
import socketserver
import stat
import sys
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, unquote, urlsplit

from . import __version__, gitops
from .agentdefs import AGENT_NAME_RE
from .items import OWNER_RE
from .config import expand_path, sanitize_repo_name
from .jobs import JOB_ID_RE, REPO_NAME_RE, JobStore

if TYPE_CHECKING:
    from .config import Config
    from .events import Event, EventBus

WEB_DIR = Path(__file__).resolve().parent / "web"
SF_ITEMS = Path(__file__).resolve().parent.parent / "bin" / "sf-items"   # outside agents' command (DESIGN §32)
COOKIE_NAME = "sf_token"
MAX_BODY_BYTES = 1 << 20
MAX_PARAM_CHARS = 200
MAX_LOG_LINES = 2000
MAX_LOG_BYTES = 4 << 20
MAX_DIFF_BYTES = 2_000_000
MAX_ITEMS_LIMIT = 1000
ITEM_SCOPES = ("job", "chat", "plan", "followups", "system")
ITEM_ACTIONS = ("accept", "dismiss", "verify", "reopen", "drop", "defer", "assign", "block", "unblock", "make-job",
                "update")
AGENT_TOKEN_PREFIX = "sfi_"
TOKEN_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}", re.ASCII)
MAX_EVIDENCE = 10
PING_INTERVAL = 15.0
SSE_POLL_SECONDS = 0.25
SSE_RETRY_MS = 3000
LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]")

CHAT_ID_RE = re.compile(r"^c-[0-9a-f]{10}$", re.ASCII)
PLAN_ID_RE = re.compile(r"^P\d{3}-[a-z0-9]+(-[a-z0-9]+)*$", re.ASCII)
MODEL_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]*$", re.ASCII)
WRITABLE_AGENT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$", re.ASCII)
PERSONA_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$", re.ASCII)   # personas.PERSONA_NAME_RE
PROVIDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$", re.ASCII)
PRESET_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$", re.ASCII)
STATIC_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+)+$", re.ASCII)
LOG_FILE_RE = re.compile(r"^logs/([A-Za-z0-9][A-Za-z0-9._-]*\.jsonl)$", re.ASCII)
_INLINE_SCRIPT_RE = re.compile(rb"<script>(.*?)</script>", re.DOTALL | re.IGNORECASE)
_PORT_RE = re.compile(r"^\d{1,5}$", re.ASCII)
_INT_RE = re.compile(r"^\d{1,18}$", re.ASCII)

HTML_CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'")
OTHER_CSP = "default-src 'none'; frame-ancestors 'none'; sandbox"
HTML_TYPE = "text/html; charset=utf-8"
TEXT_TYPE = "text/plain; charset=utf-8"
JSON_TYPE = "application/json"
FORM_TYPE = "application/x-www-form-urlencoded"
CONTENT_TYPES = {
    ".html": HTML_TYPE,
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".txt": TEXT_TYPE,
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".webmanifest": "application/manifest+json",
    ".woff2": "font/woff2",
}

FALLBACK_LOGIN_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Software Factory: sign in</title>
<style>
body{font:16px/1.5 system-ui,sans-serif;max-width:26rem;margin:12vh auto;padding:0 1rem}
input,button{font:inherit;width:100%;box-sizing:border-box;padding:.55rem;margin-top:.4rem}
</style>
</head>
<body>
<h1>Software Factory</h1>
<p>Sign in with the access token. On the server, <code>factory url</code> prints a login link.</p>
<form method="post" action="/api/login">
<label for="token">Token</label>
<input id="token" name="token" type="password" autocomplete="current-password" required autofocus>
<button type="submit">Sign in</button>
</form>
</body>
</html>
"""

_PARAM_RULES: dict[str, tuple[re.Pattern[str], str]] = {
    "repo": (REPO_NAME_RE, "repo name"),
    "job": (JOB_ID_RE, "job id"),
    "chat": (CHAT_ID_RE, "chat id"),
    "plan": (PLAN_ID_RE, "plan id"),
    "agent": (AGENT_NAME_RE, "agent name"),
    "model": (MODEL_KEY_RE, "model key"),
    "map": (re.compile(r"[a-z0-9][a-z0-9-]{0,63}"), "map id"),
    "persona": (PERSONA_RE, "persona name"),
    "token_name": (TOKEN_NAME_RE, "agent token name"),
}
MAX_CONFIRM_LINES = 200          # lines one POST .../confirm may name


class HttpError(Exception):
    """An error response with a status code and a message safe to show the client."""

    def __init__(self, status: int, message: str, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.headers = headers or {}


@dataclass
class Reply:
    """A complete (non-streamed) response."""

    status: int
    body: bytes
    content_type: str
    headers: dict[str, str] = field(default_factory=dict)


def json_reply(payload: Any, status: int = 200, headers: dict[str, str] | None = None) -> Reply:
    """A JSON response (ASCII-escaped, so any text survives encoding)."""
    body = json.dumps(payload, default=_json_default).encode("ascii")
    return Reply(status, body, JSON_TYPE, headers or {})


def _json_default(obj: Any) -> Any:
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    if isinstance(obj, os.PathLike):
        return os.fspath(obj)
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    raise TypeError(f"{type(obj).__name__} is not JSON serialisable")


def error_status(exc: BaseException) -> int:
    """HTTP status for an exception raised by a handler or a Factory method.

    Any exception class named ActionError (the orchestrator's, or a subclass of it) is a 409; an Open WebUI that
    does not answer or refuses (personas.OpenWebUIError) is a 502, but a setup that keeps the harness from asking
    it at all (personas.OpenWebUISetupError: no key, a provider not on this machine) is a 400.
    """
    names = {cls.__name__ for cls in type(exc).__mro__}
    if "ActionError" in names:
        return 409
    if "OpenWebUISetupError" in names:
        return 400
    if "OpenWebUIError" in names:
        return 502
    if isinstance(exc, (KeyError, FileNotFoundError, NotADirectoryError, IsADirectoryError)):
        return 404
    if isinstance(exc, ValueError):
        return 400
    return 500


def _error_message(exc: BaseException, status: int) -> str:
    if isinstance(exc, KeyError):
        detail = exc.args[0] if exc.args and isinstance(exc.args[0], str) else ""
        return f"not found: {detail}" if detail else "not found"
    if isinstance(exc, OSError):  # its text would carry a filesystem path
        return "not found"
    return str(exc) or HTTPStatus(status).phrase


def split_host(value: str) -> tuple[str, int | None] | None:
    """("host", port or None) from "host[:port]" or "[v6][:port]", lowercased; None if malformed.

    A bare IPv6 address ("::1") is bracketed, so configured hosts compare like Host headers.
    """
    value = value.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        if end < 0:
            return None
        host, rest = value[:end + 1], value[end + 1:]
    elif value.count(":") > 1:
        host, rest = f"[{value}]", ""
    else:
        host, colon, port = value.partition(":")
        rest = colon + port
    if not host or host == "[]" or any(ch.isspace() or ch in "/@\\" for ch in host):
        return None
    if not rest:
        return host, None
    if rest[0] != ":" or not _PORT_RE.fullmatch(rest[1:]) or int(rest[1:]) > 65535:
        return None
    return host, int(rest[1:])


def _allowed_hosts(config: Config, bind_host: str) -> frozenset[str]:
    server = _section(config, "server")
    extra = server.get("allowed_hosts") if isinstance(server.get("allowed_hosts"), list) else []
    names = [*LOCAL_HOSTS, bind_host, str(server.get("host") or ""), *(str(h) for h in extra)]
    parsed = (split_host(name) for name in names if name)
    return frozenset(entry[0] for entry in parsed if entry)


def _open_regular(path: Path) -> Any:
    """Open path for binary reading if it is a regular file and not a symlink; never blocks on a FIFO."""
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise FileNotFoundError("not found") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise FileNotFoundError("not found")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def _contained(root: Path, rel: str) -> Path:
    """rel resolved inside root (no absolute paths, '..' or symlink escapes); ValueError otherwise."""
    if not rel or "\0" in rel or PurePosixPath(rel).is_absolute() or ".." in PurePosixPath(rel).parts:
        raise ValueError("path must be relative and stay inside the folder")
    base = root.resolve()
    target = (base / rel).resolve()
    if base not in target.parents:
        raise ValueError("path must be relative and stay inside the folder")
    return target


def page_csp(html: bytes) -> str:
    """HTML_CSP, with each inline <script> block of the page allowed by its sha256 hash."""
    sources = [f"'sha256-{base64.b64encode(hashlib.sha256(match.group(1)).digest()).decode()}'"
               for match in _INLINE_SCRIPT_RE.finditer(html)]
    return HTML_CSP.replace("script-src 'self'", " ".join(["script-src 'self'", *sources]))


def _list_files(root: Path) -> list[str]:
    """Sorted POSIX paths of the regular files below root (dotfiles, symlinks and sessions/ left out)."""
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        if Path(dirpath) == root and "sessions" in dirnames:
            dirnames.remove("sessions")
        for name in filenames:
            path = Path(dirpath) / name
            if not name.startswith(".") and path.is_file() and not path.is_symlink():
                found.append(path.relative_to(root).as_posix())
    return sorted(found)


def read_log(path: Path, offset: int) -> dict:
    """Complete JSONL lines of path from byte offset: {lines: [parsed objects], offset, more}.

    At most MAX_LOG_LINES lines and MAX_LOG_BYTES per call; a trailing partial line
    stays unread until it is finished, lines that are not JSON are skipped.
    """
    with _open_regular(path) as handle:
        handle.seek(offset)
        data = handle.read(MAX_LOG_BYTES)
    end = data.rfind(b"\n")
    if end < 0:
        # No complete line: wait for it, unless one line alone overflows the budget (skip it).
        skipped = len(data) if len(data) >= MAX_LOG_BYTES else 0
        return {"lines": [], "offset": offset + skipped, "more": bool(skipped)}
    pieces = data[:end].split(b"\n")
    lines: list[Any] = []
    consumed = 0
    for piece in pieces[:MAX_LOG_LINES]:
        consumed += len(piece) + 1
        try:
            lines.append(json.loads(piece.decode("utf-8")))
        except ValueError:
            continue
    more = len(pieces) > MAX_LOG_LINES or len(data) >= MAX_LOG_BYTES
    return {"lines": lines, "offset": offset + consumed, "more": more}


def sse_frame(event: Event) -> bytes:
    """One SSE message: id, event type and the event as single-line JSON."""
    kind = re.sub(r"[\r\n]", " ", event.type)
    data = json.dumps(event.to_dict(), separators=(",", ":"), default=str)
    return f"id: {event.seq}\nevent: {kind}\ndata: {data}\n\n".encode("ascii")


def _replay(bus: EventBus, since: int) -> list[Event]:
    """Buffered events after since; everything buffered if since is from a previous server run."""
    events = bus.since(since)
    if not events and since > 0:
        everything = bus.since(0)
        if everything and everything[-1].seq < since:
            return everything
    return events


def _agent_summary(config: Config, agent: Any) -> dict:
    try:
        model = config.agent_model(agent).key
    except (KeyError, ValueError):
        model = agent.model
    return {"name": agent.name, "description": agent.description, "model": model, "tier": agent.model,
            "driver": config.agent_driver(agent.name), "tools": list(agent.tools), "color": agent.color}


def _section(config: Config, name: str) -> dict:
    value = config.raw.get(name)
    return value if isinstance(value, dict) else {}


@dataclass
class ApiRequest:
    """A routed API call: decoded path parameters, query string and JSON body."""

    params: dict[str, str]
    query: dict[str, list[str]]
    body: dict

    def arg(self, key: str, required: bool = False) -> str | None:
        """First value of a query parameter (HttpError 400 when required and absent)."""
        values = self.query.get(key)
        if not values:
            if required:
                raise HttpError(400, f"missing query parameter {key!r}")
            return None
        return values[0]

    def int_arg(self, key: str) -> int:
        """A non-negative integer query parameter (0 when absent)."""
        value = self.arg(key)
        if value is None or value == "":
            return 0
        if not _INT_RE.fullmatch(value):
            raise HttpError(400, f"{key} must be a non-negative integer")
        return int(value)

    def text(self, key: str, required: bool = True) -> str | None:
        """A string body field; required ones must be non-blank."""
        value = self.body.get(key)
        if value is None and not required:
            return None
        if not isinstance(value, str) or (required and not value.strip()):
            raise HttpError(400, f"{key!r} must be a non-empty string" if required else f"{key!r} must be a string")
        return value

    def name(self, key: str, pattern: re.Pattern[str], label: str, required: bool = True) -> str | None:
        """A body field that must match pattern (a repo, agent or preset name); optional ones may be ""."""
        value = self.text(key, required)
        if not value:
            return None
        if len(value) > MAX_PARAM_CHARS or not pattern.fullmatch(value):
            raise HttpError(400, f"invalid {label}")
        return value

    def delivery(self) -> str | None:
        """The optional delivery mode (merge, branch or folder; the Factory validates the name)."""
        value = self.body.get("delivery")
        if value is None or value == "":
            return None
        if not isinstance(value, str) or len(value) > MAX_PARAM_CHARS:
            raise HttpError(400, "'delivery' must be one of merge, branch, folder")
        return value

    def profile(self) -> str | None:
        """The optional routing profile: absent or null → None (the default profile), "" → no profile,
        else a profile name (the Factory checks it)."""
        value = self.body.get("profile")
        if value is None:
            return None
        if not isinstance(value, str) or len(value) > MAX_PARAM_CHARS:
            raise HttpError(400, "'profile' must be a profile name, or \"\" for none")
        return value

    def routing(self, required: bool = False) -> dict | str | None:
        """The optional per-run routing: a preset name or {agent: {model, driver, extra_body}}."""
        value = self.body.get("routing")
        if value is None and not required:
            return None
        if not isinstance(value, (dict, str)) or not value:
            raise HttpError(400, "'routing' must be a preset name or an object of per-agent routes")
        return value


class FactoryHTTPServer(ThreadingHTTPServer):
    """The factory's HTTP server; call serve_forever(), then shutdown() and server_close()."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, config: Config, factory: Any, bus: EventBus, host: str, port: int, *,
                 ping_interval: float = PING_INTERVAL, web_dir: Path = WEB_DIR):
        server = _section(config, "server")
        token = server.get("token")
        if not isinstance(token, str) or not token:
            raise ValueError("server.token is not set: run `factory init` first")
        self.config = config
        self.factory = factory
        self.bus = bus
        self.token = token
        self.ping_interval = ping_interval
        self.web_dir = Path(web_dir)
        self.store = JobStore(config.data_dir)
        self.allowed_hosts = _allowed_hosts(config, host)
        self.stopping = threading.Event()   # set for good by shutdown(): every stream ends, new ones too
        self._streams_lock = threading.Lock()
        self._streams_closed = threading.Event()   # replaced by each close_streams() call
        self.repos_lock = threading.Lock()
        self.address_family = socket.AF_INET6 if ":" in host else socket.AF_INET
        super().__init__((host, port), FactoryRequestHandler)

    def server_bind(self) -> None:
        """Bind without HTTPServer's reverse DNS lookup of the address."""
        socketserver.TCPServer.server_bind(self)
        self.server_name = str(self.server_address[0])
        self.server_port = int(self.server_address[1])

    @property
    def url(self) -> str:
        """http://host:port/ (no token)."""
        host = self.server_name
        return f"http://[{host}]:{self.server_port}/" if ":" in host else f"http://{host}:{self.server_port}/"

    def current_config(self) -> Config:
        """The Factory's config (it may reload it after an update), else the one given at start."""
        return getattr(self.factory, "config", None) or self.config

    def stream_closer(self) -> threading.Event:
        """The event the next close_streams() sets; a stream takes it when it opens and ends once it is set."""
        with self._streams_lock:
            return self._streams_closed

    def close_streams(self) -> None:
        """End the event streams open now (each notices within SSE_POLL_SECONDS).

        The server keeps serving: streams opened afterwards (an EventSource reconnecting)
        stay open until the next close_streams() or shutdown().
        """
        with self._streams_lock:
            closing, self._streams_closed = self._streams_closed, threading.Event()
        closing.set()

    def shutdown(self) -> None:
        """End every event stream for good (later ones close at once), then stop serve_forever()."""
        self.stopping.set()
        self.close_streams()
        super().shutdown()

    def log_exception(self, where: str, exc: BaseException) -> None:
        """Publish an unexpected error with its traceback as a 'log' event (the bus redacts secrets)."""
        trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        self.bus.publish("log", {"level": "error", "message": f"server error in {where}: {type(exc).__name__}",
                                 "traceback": trace})

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Ignore clients that went away; report anything else on the bus."""
        exc = sys.exc_info()[1]
        if exc is None or isinstance(exc, (ConnectionError, TimeoutError)):
            return
        self.log_exception("request handling", exc)


class FactoryRequestHandler(BaseHTTPRequestHandler):
    """One HTTP connection (HTTP/1.1 keep-alive; event streams close when they end)."""

    server: FactoryHTTPServer
    protocol_version = "HTTP/1.1"
    server_version = "SoftwareFactory"
    sys_version = ""
    timeout = 60

    def do_GET(self) -> None:
        self._handle()

    do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = do_GET

    def log_message(self, format: str, *args: Any) -> None:
        """Silenced: an access log would record /login?token=… URLs."""

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        """JSON errors (with the security headers) for requests http.server rejects itself."""
        self._body_pending = True
        try:
            self._send(json_reply({"error": message or HTTPStatus(code).phrase}, code))
        except (ConnectionError, TimeoutError):
            self.close_connection = True

    # -- request flow -----------------------------------------------------------

    def _handle(self) -> None:
        self._body_pending = bool(self.headers.get("Transfer-Encoding")) or \
            self.headers.get("Content-Length", "0").strip() not in ("", "0")
        try:
            reply = self._process()
        except Exception as exc:  # noqa: BLE001 - every failure becomes a JSON error response
            reply = self._error_reply(exc)
        if reply is None:
            return
        try:
            self._send(reply)
        except (ConnectionError, TimeoutError):
            self.close_connection = True

    def _process(self) -> Reply | None:
        path, _, query_string = self.path.partition("#")[0].partition("?")
        if not path.startswith("/"):
            raise HttpError(400, "bad request path")
        self._check_host()
        if self.command not in ("GET", "POST"):
            raise HttpError(405, "method not allowed", {"Allow": "GET, POST"})
        if self.command == "POST":
            self._check_origin()
        query = parse_qs(query_string, keep_blank_values=True)
        if path == "/login":
            self._require_method("GET")
            return self._login_get(query)
        if path == "/api/login":
            self._require_method("POST")
            return self._login_post()
        if path.startswith("/api/"):
            self.agent: str | None = None
            if not self._authenticated():
                self.agent = self._agent_token()
                if self.agent is None:
                    raise HttpError(401, "authentication required")
            return self._api(path, query)
        return self._static(path)

    def _error_reply(self, exc: Exception) -> Reply:
        if isinstance(exc, HttpError):
            return json_reply({"error": exc.message}, exc.status, exc.headers)
        status = error_status(exc)
        if status == 500:
            self.server.log_exception(f"{self.command} {self.path.partition('?')[0]}", exc)
            return json_reply({"error": "internal server error"}, 500)
        payload = getattr(exc, "payload", None)
        extra = {key: value for key, value in payload.items() if key != "error"} if isinstance(payload, dict) else {}
        return json_reply({"error": _error_message(exc, status), **extra}, status)

    def _require_method(self, method: str) -> None:
        if self.command != method:
            raise HttpError(405, "method not allowed", {"Allow": method})

    # -- guards -----------------------------------------------------------------

    def _check_host(self) -> None:
        values = self.headers.get_all("Host") or []
        parsed = split_host(values[0]) if len(values) == 1 else None
        if parsed is None or parsed[0] not in self.server.allowed_hosts:
            raise HttpError(403, "host not allowed (add it to server.allowed_hosts)")

    def _check_origin(self) -> None:
        origin = self.headers.get("Origin")
        if origin is None:
            return
        parts = urlsplit(origin.strip())
        source = split_host(parts.netloc) if parts.scheme in ("http", "https") else None
        target = split_host(self.headers.get("Host", ""))
        default_port = 443 if parts.scheme == "https" else 80
        if (source is None or target is None or source[0] != target[0]
                or (source[1] or default_port) != (target[1] or default_port)):
            raise HttpError(403, "cross-origin request refused")

    def _token_matches(self, candidate: str) -> bool:
        return hmac.compare_digest(candidate.encode("utf-8", "replace"), self.server.token.encode("utf-8"))

    def _authenticated(self) -> bool:
        scheme, _, value = self.headers.get("Authorization", "").strip().partition(" ")
        if scheme.lower() == "bearer" and value.strip() and self._token_matches(value.strip()):
            return True
        return any(self._token_matches(value) for value in self._cookies(COOKIE_NAME))

    def _agent_token(self) -> str | None:
        """The name of the outside agent whose token this request carries (a Bearer header only, never a
        cookie), or None."""
        scheme, _, value = self.headers.get("Authorization", "").strip().partition(" ")
        value = value.strip()
        if scheme.lower() != "bearer" or not value.startswith(AGENT_TOKEN_PREFIX):
            return None
        match = getattr(self.server.factory, "agent_token_name", None)
        return match(value) if callable(match) else None

    def _cookies(self, name: str) -> list[str]:
        values = []
        for header in self.headers.get_all("Cookie") or []:
            for item in header.split(";"):
                key, equals, value = item.strip().partition("=")
                if equals and key.strip() == name and value.strip():
                    values.append(value.strip().strip('"'))
        return values

    # -- body -------------------------------------------------------------------

    def _media_type(self) -> str:
        return self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()

    def _read_body(self) -> bytes:
        if self.headers.get("Transfer-Encoding"):
            raise HttpError(411, "send the body with a Content-Length")
        raw = self.headers.get("Content-Length", "").strip() or "0"
        if not _INT_RE.fullmatch(raw):
            raise HttpError(400, "invalid Content-Length")
        length = int(raw)
        if length > MAX_BODY_BYTES:
            raise HttpError(413, "request body too large (limit 1 MB)")
        data = self.rfile.read(length) if length else b""
        if len(data) != length:
            raise HttpError(400, "incomplete request body")
        self._body_pending = False
        return data

    def _read_json(self) -> dict:
        if self._media_type() != JSON_TYPE:
            raise HttpError(415, "Content-Type must be application/json")
        data = self._read_body()
        if not data.strip():
            return {}
        try:
            body = json.loads(data.decode("utf-8"))
        except ValueError:
            raise HttpError(400, "request body is not valid JSON") from None
        if not isinstance(body, dict):
            raise HttpError(400, "request body must be a JSON object")
        return body

    # -- responses --------------------------------------------------------------

    def _security_headers(self, csp: str) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", csp)

    def _send(self, reply: Reply) -> None:
        self.send_response(reply.status)
        self.send_header("Content-Type", reply.content_type)
        self.send_header("Content-Length", str(len(reply.body)))
        self._security_headers(page_csp(reply.body) if reply.content_type == HTML_TYPE else OTHER_CSP)
        for key, value in reply.headers.items():
            self.send_header(key, value)
        if self._body_pending:  # unread body bytes would be parsed as the next request
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(reply.body)

    def _cookie_header(self) -> dict[str, str]:
        return {"Set-Cookie": f"{COOKIE_NAME}={self.server.token}; HttpOnly; SameSite=Strict; Path=/"}

    def _redirect(self, status: int, location: str, headers: dict[str, str]) -> Reply:
        return Reply(status, b"", TEXT_TYPE, {"Location": location, **headers})

    # -- login and static files -------------------------------------------------

    def _login_page(self, status: int) -> Reply:
        try:
            body = self._read_static("login.html")
        except FileNotFoundError:
            body = FALLBACK_LOGIN_HTML.encode("utf-8")
        return Reply(status, body, HTML_TYPE)

    def _login_get(self, query: dict[str, list[str]]) -> Reply:
        token = (query.get("token") or [None])[0]
        if token is None:
            return self._login_page(200)
        if self._token_matches(token):
            return self._redirect(302, "/", self._cookie_header())
        return self._login_page(401)

    def _login_post(self) -> Reply:
        media = self._media_type()
        if media not in (JSON_TYPE, FORM_TYPE):
            raise HttpError(415, "Content-Type must be application/json")
        if media == FORM_TYPE:
            form = parse_qs(self._read_body().decode("utf-8", "replace"))
            if self._token_matches((form.get("token") or [""])[0].strip()):
                return self._redirect(303, "/", self._cookie_header())
            return self._login_page(401)
        token = self._read_json().get("token")
        if not isinstance(token, str) or not self._token_matches(token.strip()):
            raise HttpError(401, "invalid token")
        return json_reply({"ok": True}, headers=self._cookie_header())

    def _read_static(self, name: str) -> bytes:
        with _open_regular(self.server.web_dir / name) as handle:
            return handle.read()

    def _static(self, path: str) -> Reply:
        self._require_method("GET")
        name = "index.html" if path == "/" else path[1:]
        content_type = CONTENT_TYPES.get(os.path.splitext(name)[1].lower()) if STATIC_NAME_RE.fullmatch(name) else None
        if content_type is None:
            raise HttpError(404, "not found")
        # Pages need the token; assets (CSS, JS, icons) are public so the login page can use them.
        if content_type == HTML_TYPE and not self._authenticated():
            return self._login_page(200)
        return Reply(200, self._read_static(name), content_type)

    # -- API routing --------------------------------------------------------------

    def _api(self, path: str, query: dict[str, list[str]]) -> Reply | None:
        allowed: list[str] = []
        for method, pattern, handler in _ROUTES:
            match = pattern.fullmatch(path)
            if not match:
                continue
            if method != self.command:
                allowed.append(method)
                continue
            if getattr(self, "agent", None) is not None and handler not in AGENT_TOKEN_ROUTES:
                raise HttpError(403, "an agent token is valid only for the item routes (list items, add a system "
                                     "item or a follow-up, propose, update your own items)")
            params = {key: _decode_param(key, raw) for key, raw in match.groupdict().items()}
            body = self._read_json() if method == "POST" else {}
            return handler(self, ApiRequest(params, query, body))
        if allowed:
            raise HttpError(405, "method not allowed", {"Allow": ", ".join(sorted(set(allowed)))})
        raise HttpError(404, "no such API route")

    def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        method = getattr(self.server.factory, name, None)
        if not callable(method):
            raise HttpError(501, f"this factory does not support {name}()")
        return method(*args, **kwargs)

    # -- API handlers -------------------------------------------------------------

    def api_state(self, req: ApiRequest) -> Reply:
        config = self.server.current_config()
        state: dict[str, Any] = {}
        try:
            agents = self._call("agents")
        except ValueError as exc:  # one malformed agent file must not take the whole board down
            agents, state["agents_error"] = {}, str(exc)
        models = {key: {"provider": entry.get("provider"), "model": entry.get("model")}
                  for key, entry in _section(config, "models").items() if isinstance(entry, dict)}
        state.update({
            "version": __version__,
            "repos": [{"name": repo.name, "path": str(repo.path), "base": repo.base} for repo in config.repos()],
            "jobs": self._call("jobs"),
            "pipeline": dataclasses.asdict(config.pipeline()),
            "models": models,
            "drivers": [name for name, entry in _section(config, "drivers").items() if isinstance(entry, dict)],
            "agents": [_agent_summary(config, agents[name]) for name in sorted(agents)],
        })
        return json_reply(state)

    def api_events(self, req: ApiRequest) -> None:
        last_id = self.headers.get("Last-Event-ID", "").strip()
        since = max(req.int_arg("since"), int(last_id) if _INT_RE.fullmatch(last_id) else 0)
        bus = self.server.bus
        closed = self.server.stream_closer()
        subscription = bus.subscribe()
        try:
            replay = _replay(bus, since)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self._security_headers(OTHER_CSP)
            self.end_headers()
            self._stream(subscription, replay, closed)
        except (ConnectionError, TimeoutError):
            pass
        finally:
            bus.unsubscribe(subscription)
            self.close_connection = True

    def _stream(self, subscription: queue.Queue, replay: list[Event], closed: threading.Event) -> None:
        write = self.wfile.write
        write(f"retry: {SSE_RETRY_MS}\n\n".encode("ascii"))
        last_sent = 0
        for event in replay:
            write(sse_frame(event))
            last_sent = event.seq
        interval = self.server.ping_interval
        next_ping = time.monotonic() + interval
        stopping = self.server.stopping
        while not (stopping.is_set() or closed.is_set()):
            wait = min(SSE_POLL_SECONDS, max(0.0, next_ping - time.monotonic()))
            try:
                event = subscription.get(timeout=wait)
            except queue.Empty:
                if time.monotonic() >= next_ping:
                    write(b": ping\n\n")
                    next_ping = time.monotonic() + interval
                continue
            if event.seq > last_sent:  # events published while replaying arrive twice
                write(sse_frame(event))
                next_ping = time.monotonic() + interval

    def api_doctor(self, req: ApiRequest) -> Reply:
        return json_reply({"checks": self._call("doctor")})

    # jobs

    def api_job(self, req: ApiRequest) -> Reply:
        repo, job_id = req.params["repo"], req.params["job"]
        return json_reply({"job": self._call("job", repo, job_id), "files": self._call("job_files", repo, job_id)})

    def api_job_file(self, req: ApiRequest) -> Reply:
        text = self.server.store.read_file(req.params["repo"], req.params["job"], req.arg("path", required=True))
        return Reply(200, text.encode("utf-8", "replace"), TEXT_TYPE)

    def api_job_log(self, req: ApiRequest) -> Reply:
        match = LOG_FILE_RE.fullmatch(req.arg("file", required=True) or "")
        if not match:
            raise HttpError(400, "file must be logs/<name>.jsonl")
        offset = req.int_arg("offset")
        job_dir = self.server.store.job_dir(req.params["repo"], req.params["job"]).resolve()
        if not job_dir.is_dir():
            raise KeyError(f"{req.params['repo']}/{req.params['job']}")
        path = (job_dir / "logs" / match.group(1)).resolve()
        if path.parent != job_dir / "logs":  # the logs folder must not be a symlink
            raise HttpError(404, "not found")
        return json_reply(read_log(path, offset))

    def api_job_diff(self, req: ApiRequest) -> Reply:
        repo = req.params["repo"]
        job = self._call("job", repo, req.params["job"])
        base, head = job.get("base"), job.get("branch")
        if job.get("merged_commit"):  # the branch is gone: show what the merge brought in
            base, head = f"{job['merged_commit']}^1", job["merged_commit"]
        result: dict[str, Any] = {"stat": "", "diff": "", "truncated": False}
        if not base or not head:
            return json_reply(result)
        repo_path = self.server.current_config().repo(repo).path
        try:
            result["stat"] = gitops.diffstat(repo_path, base, head)
            result["diff"], result["truncated"] = gitops.diff_truncated(repo_path, base, head, MAX_DIFF_BYTES)
        except gitops.GitError as exc:
            lines = exc.stderr.strip().splitlines()
            result.update(stat="", diff="", truncated=False,
                          error=(lines[0][:300] if lines else "") or "git diff failed")
        return json_reply(result)

    def api_submit(self, req: ApiRequest) -> Reply:
        repo = req.name("repo", REPO_NAME_RE, "repo name")
        routing, delivery, profile = req.routing(), req.delivery(), req.profile()
        extra: dict[str, Any] = {"routing": routing} if routing is not None else {}
        if delivery is not None:
            extra["delivery"] = delivery
        if profile is not None:
            extra["profile"] = profile
        return json_reply({"job": self._call("submit", repo, req.text("feature"), **extra)}, 201)

    def api_job_action(self, req: ApiRequest) -> Reply:
        repo, job_id, action = req.params["repo"], req.params["job"], req.params["action"]
        if action == "rework":
            job = self._call("rework", repo, job_id, req.text("note"))
        elif action == "reject":
            job = self._call("reject", repo, job_id, req.text("reason", required=False) or "")
        elif action == "routing":
            # {routing: null} re-routes every agent from the saved config; a missing key is an error.
            if "routing" not in req.body:
                raise HttpError(400, "'routing' is required: a preset name, an object of per-agent routes, "
                                     "or null for the saved config")
            routing = None if req.body["routing"] is None else req.routing(required=True)
            profile = req.profile()
            job = self._call("set_routing", repo, job_id, routing, **({"profile": profile} if profile is not None else {}))
        elif action == "approve" and req.body.get("carry_items") is not None:
            carry = req.body["carry_items"]
            if not isinstance(carry, list) or len(carry) > 1000 or not all(isinstance(i, str) for i in carry):
                raise HttpError(400, "'carry_items' must be a list of item ids such as I-2")
            job = self._call("approve", repo, job_id, carry_items=carry)
        else:
            job = self._call(action, repo, job_id)
        return json_reply({"job": job})

    # repos

    def api_backlog(self, req: ApiRequest) -> Reply:
        return json_reply({"items": self._call("backlog", req.params["repo"])})

    def api_add_backlog(self, req: ApiRequest) -> Reply:
        return json_reply({"items": self._call("add_backlog", req.params["repo"], req.text("text"))})

    def api_next(self, req: ApiRequest) -> Reply:
        return json_reply({"job": self._call("next", req.params["repo"])})

    def api_add_repo(self, req: ApiRequest) -> Reply:
        """Register a repo through factory.update_config, which validates it like Config.add_repo."""
        path = req.text("path") or ""
        name = req.name("name", REPO_NAME_RE, "repo name", required=False)
        with self.server.repos_lock:
            config = self.server.current_config()
            name = name or sanitize_repo_name(expand_path(path, config.path.parent).resolve().name)
            if not name:
                raise HttpError(400, "cannot derive a repo name from the path: pass a name")
            repos = config.raw.get("repos") if isinstance(config.raw.get("repos"), list) else []
            entry = {"name": name, "path": path, "base": None, "setup": None, "escalate_paths": []}
            self._call("update_config", {"repos": [*copy.deepcopy(repos), entry]})
        repo = self.server.current_config().repo(name)
        self.server.bus.publish("repos.updated", {})
        return json_reply({"repo": {"name": repo.name, "path": str(repo.path), "base": repo.base,
                                    "setup": repo.setup, "escalate_paths": repo.escalate_paths}})

    def api_scout(self, req: ApiRequest) -> Reply:
        self._call("scout", req.params["repo"])
        return json_reply({"started": True})

    def api_scout_status(self, req: ApiRequest) -> Reply:
        return json_reply(self._call("scout_status", req.params["repo"]))

    # agents

    def api_agent(self, req: ApiRequest) -> Reply:
        return json_reply(self._call("agent_detail", req.params["agent"]))

    def api_save_agent(self, req: ApiRequest) -> Reply:
        name = self._writable_agent(req.params["agent"])
        return json_reply({"agent": self._call("save_agent", name, req.text("text"))})

    def api_new_agent(self, req: ApiRequest) -> Reply:
        name = req.name("name", WRITABLE_AGENT_RE, "agent name (use a-z, 0-9 and '-', max 41 chars)")
        source = req.name("from", AGENT_NAME_RE, "agent name", required=False) or "reviewer-code"
        return json_reply({"agent": self._call("new_agent", name, source)}, 201)

    def api_reset_agent(self, req: ApiRequest) -> Reply:
        """Delete the user override: {agent: the agent now in effect, or null when none of that name remains}.

        Factory.reset_agent returns None when the agent existed only as the override (still a
        reset, 200) and raises KeyError when there was no override to delete (404).
        """
        name = self._writable_agent(req.params["agent"])
        return json_reply({"agent": self._call("reset_agent", name)})

    @staticmethod
    def _writable_agent(name: str) -> str:
        if not WRITABLE_AGENT_RE.fullmatch(name):
            raise HttpError(400, "invalid agent name (use a-z, 0-9 and '-', max 41 chars)")
        return name

    # chats

    def api_chats(self, req: ApiRequest) -> Reply:
        return json_reply({"chats": self._call("chats")})

    def api_new_chat(self, req: ApiRequest) -> Reply:
        """POST /api/chats {repo, model?, mode?, agent?, profile?}; mode "none" without repo is a chat
        without a repository, {persona, model?, profile?} a persona's chat (DESIGN §19, §33)."""
        persona = req.name("persona", PERSONA_RE, "persona name", required=False)
        mode = req.text("mode", required=False) or None
        if persona:
            if req.body.get("repo"):
                raise HttpError(400, "a persona's chat has no repository: leave out 'repo'")
            if req.body.get("agent") and req.body.get("agent") != persona:
                raise HttpError(400, "give either 'agent' or 'persona', not both")
            if mode not in (None, "none"):
                raise HttpError(400, "a persona's chat has mode 'none' (no repository)")
            mode = "none"
        repo = req.name("repo", REPO_NAME_RE, "repo name", required=mode != "none")
        mode = mode or "worktree"
        if mode not in ("worktree", "repo", "none"):
            raise HttpError(400, "mode must be 'worktree', 'repo' or 'none' (a chat without a repository)")
        if mode == "none" and repo:
            raise HttpError(400, "mode 'none' is a chat without a repository: leave out 'repo'")
        model = req.text("model", required=False) or None
        agent = req.name("agent", AGENT_NAME_RE, "agent name", required=False)
        extra: dict[str, Any] = {"agent": agent} if agent and not persona else {}
        if persona:
            extra["persona"] = persona
        profile = req.profile()
        if profile is not None:
            extra["profile"] = profile
        return json_reply({"chat": self._call("new_chat", repo, model, mode, **extra)}, 201)

    # personas and Open WebUI profiles (section 33)

    def api_personas(self, req: ApiRequest) -> Reply:
        return json_reply({"personas": self._call("personas")})

    def api_persona(self, req: ApiRequest) -> Reply:
        return json_reply(self._call("persona", req.params["persona"]))

    def api_persona_card(self, req: ApiRequest) -> Reply:
        return json_reply(self._call("persona_card", req.params["persona"]))

    def api_persona_import(self, req: ApiRequest) -> Reply:
        """POST /api/personas/import {export: [records], model_id?, snapshot?} or {source: "openwebui", model_id,
        provider?, snapshot?} → 201 {card, path, unavailable, unknown_variables, fingerprint, snapshot}."""
        return json_reply(self._call("persona_import", req.body), 201)

    def api_persona_check(self, req: ApiRequest) -> Reply:
        """POST /api/personas/check {persona?} → {checked: [...]}: the drift check, now."""
        persona = req.name("persona", PERSONA_RE, "persona name", required=False)
        return json_reply(self._call("persona_check", persona))

    def api_openwebui_models(self, req: ApiRequest) -> Reply:
        """GET /api/openwebui/models?provider= → {provider, open_webui_version, models}."""
        provider = req.arg("provider") or None
        if provider is not None and not PROVIDER_RE.fullmatch(provider):
            raise HttpError(400, "invalid provider name")
        return json_reply(self._call("openwebui_models", provider))

    def api_chat(self, req: ApiRequest) -> Reply:
        chat = dict(self._call("chat", req.params["chat"]))
        messages = chat.pop("messages", [])
        return json_reply({"chat": chat, "messages": messages})

    def api_chat_action(self, req: ApiRequest) -> Reply:
        chat_id, action = req.params["chat"], req.params["action"]
        if action == "messages":
            return json_reply({"chat": self._call("chat_send", chat_id, req.text("text"))}, 202)
        if action == "handoff":
            delivery, profile = req.delivery(), req.profile()
            extra = {"delivery": delivery} if delivery is not None else {}
            if profile is not None:
                extra["profile"] = profile
            return json_reply({"job": self._call("chat_handoff", chat_id, req.text("feature"), **extra)})
        if action == "delete":
            self._call("chat_delete", chat_id)
            return json_reply({"ok": True})
        return json_reply({"chat": self._call("chat_cancel", chat_id)})

    # settings

    def api_config(self, req: ApiRequest) -> Reply:
        return json_reply(self._call("config_view"))

    def api_update_config(self, req: ApiRequest) -> Reply:
        return json_reply(self._call("update_config", req.body))

    def api_preset(self, req: ApiRequest) -> Reply:
        return json_reply(self._call("apply_preset", req.name("name", PRESET_RE, "preset name")))

    def api_test_model(self, req: ApiRequest) -> Reply:
        return json_reply(self._call("test_model", req.params["model"]))

    def api_estimate(self, req: ApiRequest) -> Reply:
        """GET /api/estimate?repo=&kind=feature|plan&profile=&routing=<preset>: expected tokens, cost and
        duration before a run (Factory.estimate)."""
        repo = req.arg("repo", required=True)
        if not REPO_NAME_RE.fullmatch(repo or ""):
            raise HttpError(400, "invalid repo name")
        kind = req.arg("kind") or "feature"
        profile = req.arg("profile")
        routing = req.arg("routing") or None
        if routing is not None and not PRESET_RE.fullmatch(routing):
            raise HttpError(400, "'routing' must be a preset name here")
        return json_reply(self._call("estimate", repo, kind, profile, routing))

    # plans

    def api_plans(self, req: ApiRequest) -> Reply:
        return json_reply({"plans": self._call("plans")})

    def api_new_plan(self, req: ApiRequest) -> Reply:
        repo = req.name("repo", REPO_NAME_RE, "repo name")
        delivery, profile = req.delivery(), req.profile()
        extra: dict[str, Any] = {"delivery": delivery} if delivery is not None else {}
        if profile is not None:
            extra["profile"] = profile
        plan = self._call("create_plan", repo, req.text("request"), req.routing(), **extra)
        return json_reply({"plan": plan}, 201)

    def api_plan(self, req: ApiRequest) -> Reply:
        repo, plan_id = req.params["repo"], req.params["plan"]
        plan = self._call("plan", repo, plan_id)
        plan_dir = self._plan_dir(repo, plan_id)
        return json_reply({"plan": plan, "files": _list_files(plan_dir) if plan_dir.is_dir() else []})

    def api_plan_file(self, req: ApiRequest) -> Reply:
        plan_dir = self._plan_dir(req.params["repo"], req.params["plan"])
        if not plan_dir.is_dir():
            raise KeyError(f"{req.params['repo']}/{req.params['plan']}")
        with _open_regular(_contained(plan_dir, req.arg("path", required=True) or "")) as handle:
            return Reply(200, handle.read(), TEXT_TYPE)

    def api_plan_log(self, req: ApiRequest) -> Reply:
        """One lead run's log, like the job log endpoint: ?file=logs/<NN-lead>.jsonl&offset=<n>."""
        match = LOG_FILE_RE.fullmatch(req.arg("file", required=True) or "")
        if not match:
            raise HttpError(400, "file must be logs/<name>.jsonl")
        offset = req.int_arg("offset")
        plan_dir = self._plan_dir(req.params["repo"], req.params["plan"]).resolve()
        if not plan_dir.is_dir():
            raise KeyError(f"{req.params['repo']}/{req.params['plan']}")
        path = (plan_dir / "logs" / match.group(1)).resolve()
        if path.parent != plan_dir / "logs":  # the logs folder must not be a symlink
            raise HttpError(404, "not found")
        return json_reply(read_log(path, offset))

    def api_plan_text(self, req: ApiRequest) -> Reply:
        repo, plan_id = req.params["repo"], req.params["plan"]
        return json_reply({"plan": self._call("update_plan_text", repo, plan_id, req.text("text"))})

    def api_plan_action(self, req: ApiRequest) -> Reply:
        repo, plan_id, action = req.params["repo"], req.params["plan"], req.params["action"]
        return json_reply({"plan": self._call(f"{action}_plan", repo, plan_id)})

    def _plan_dir(self, repo: str, plan_id: str) -> Path:
        return self.server.current_config().data_dir / "plans" / repo / plan_id

    # the Housekeeping Tracker (section 32)

    def api_items(self, req: ApiRequest) -> Reply:
        """Every tracked item: ?repo=&scope=job|chat|plan|followups|system&status=open,doing&owner=&waiting=1
        &overdue=1&limit=200 → {items, counts, total}. An agent token lists the system scope and its own
        follow-ups only, never a persona's item or one a private turn wrote."""
        repo = req.arg("repo") or None
        if repo is not None and (len(repo) > MAX_PARAM_CHARS or not REPO_NAME_RE.fullmatch(repo)):
            raise HttpError(400, "invalid repo name")
        scope = req.arg("scope") or None
        if scope is not None and scope not in ITEM_SCOPES:
            raise HttpError(400, f"scope must be one of {', '.join(ITEM_SCOPES)}")
        status = [part.strip() for part in (req.arg("status") or "").split(",") if part.strip()]
        owner = req.arg("owner") or None
        if owner is not None and (len(owner) > 80 or not OWNER_RE.fullmatch(owner)):
            raise HttpError(400, "invalid owner")
        limit = req.int_arg("limit") if req.arg("limit") else 200
        if limit > MAX_ITEMS_LIMIT:
            raise HttpError(400, f"limit must be at most {MAX_ITEMS_LIMIT}")
        extra: dict[str, Any] = {}
        if self.agent is not None:
            if scope not in (None, "system", "followups"):
                raise HttpError(403, "an agent token lists its own items in the system scope and the follow-ups only")
            extra["for_agent"] = self.agent
        return json_reply(self._call("items", repo=repo, scope=scope, status=status or None, owner=owner,
                                     waiting=_flag(req.arg("waiting")), overdue=_flag(req.arg("overdue")),
                                     limit=limit, **extra))

    def api_item_history(self, req: ApiRequest) -> Reply:
        """GET /api/items/history?ref=<item ref> → {item, log}: every transition of one item, oldest first."""
        ref = req.arg("ref") or ""
        if not ref or len(ref) > MAX_PARAM_CHARS:
            raise HttpError(400, "ref must name an item, e.g. job:<repo>/<job-id>/I-5")
        return json_reply(self._call("item_history", ref))

    def api_item_action(self, req: ApiRequest) -> Reply:
        """POST /api/items/<action> {ref, note?, reason?, owner?, due?} → {item} (+ {job} for make-job);
        update {ref, to, evidence?, reason?} moves an item by an agent-facing verb (an agent token: only its
        own items); dismiss {refs: [...], reason} dismisses several suggestions with one reason → {items,
        refused}."""
        action = req.params["action"]
        if self.agent is not None and action != "update":
            raise HttpError(403, "an agent token may only update its own items (never verify, drop, dismiss or "
                                 "unblock)")
        if action == "dismiss" and "refs" in req.body:
            refs = req.body["refs"]
            if not isinstance(refs, list) or not refs or len(refs) > 300 or not all(
                    isinstance(ref, str) and 0 < len(ref) <= MAX_PARAM_CHARS for ref in refs):
                raise HttpError(400, "'refs' must be a list of 1 to 300 item references")
            return json_reply(self._call("dismiss_items", refs, req.text("reason")))
        ref = req.text("ref") or ""
        if len(ref) > MAX_PARAM_CHARS:
            raise HttpError(400, "invalid item reference")
        fields: dict[str, Any] = {}
        for key in ("note", "reason", "owner", "due", "to"):
            value = req.body.get(key)
            if value is None or value == "":
                continue
            if not isinstance(value, str) or len(value) > 2000:
                raise HttpError(400, f"{key!r} must be a string")
            fields[key] = value
        if "evidence" in req.body and req.body["evidence"] is not None:
            fields["evidence"] = _evidence(req.body["evidence"])
        if action == "update" and not fields.get("to"):
            raise HttpError(400, "update needs 'to': doing, done, defer, verified, not met, accept, dismiss, drop "
                                 "or reopen")
        if self.agent is not None:
            fields["by"] = f"agent:{self.agent}"
        return json_reply(self._call("item_action", ref, action, **fields))

    def _add_item(self, scope_ref: str, req: ApiRequest, keys: tuple[str, ...]) -> Reply:
        unknown = sorted(key for key in req.body if key not in keys)
        if unknown:
            raise HttpError(400, f"unknown field {', '.join(unknown)} (use {', '.join(keys)})")
        fields: dict[str, Any] = {"title": req.text("title")}
        for key in keys[1:]:
            value = req.body.get(key)
            if value is None:
                continue
            if key == "blocking":
                if not isinstance(value, bool):
                    raise HttpError(400, "'blocking' must be true or false")
            elif not isinstance(value, str) or len(value) > 4000:
                raise HttpError(400, f"{key!r} must be a string")
            fields[key] = value
        if self.agent is not None:
            fields["by"] = f"agent:{self.agent}"
        return json_reply({"item": self._call("add_item", scope_ref, **fields)}, 201)

    def api_system_items(self, req: ApiRequest) -> Reply:
        """GET /api/system/items → {items, summary, log}: the system scope (what belongs to no repo, §32, §33)."""
        return json_reply(self._call("scope_items", "system"))

    def api_add_system_item(self, req: ApiRequest) -> Reply:
        """POST /api/system/items {title, detail?, kind?, blocking?, due?} → {item} 201 (an agent token's is its
        own, verified by a person)."""
        return self._add_item("system", req, ("title", "detail", "kind", "blocking", "due"))

    def api_propose_item(self, req: ApiRequest) -> Reply:
        """POST /api/items/propose {scope: "system" | "repo:<repo>", title, detail?, blocking?} → {item} 201:
        an item owned by its proposer (an outside agent: agent:<token name>), verified by a person."""
        scope = req.text("scope") or ""
        if scope != "system" and not (scope.startswith("repo:") and REPO_NAME_RE.fullmatch(scope[5:])):
            raise HttpError(400, "'scope' must be \"system\" or \"repo:<repo>\"")
        if scope != "system":
            self.server.current_config().repo(scope[5:])
        keys = ("scope", "title", "detail", "blocking")
        unknown = sorted(key for key in req.body if key not in keys)
        if unknown:
            raise HttpError(400, f"unknown field {', '.join(unknown)} (use {', '.join(keys)})")
        fields: dict[str, Any] = {"title": req.text("title")}
        if req.body.get("detail") is not None:
            if not isinstance(req.body["detail"], str) or len(req.body["detail"]) > 4000:
                raise HttpError(400, "'detail' must be a string")
            fields["detail"] = req.body["detail"]
        if req.body.get("blocking") is not None:
            if not isinstance(req.body["blocking"], bool):
                raise HttpError(400, "'blocking' must be true or false")
            fields["blocking"] = req.body["blocking"]
        by = f"agent:{self.agent}" if self.agent is not None else "human"
        return json_reply({"item": self._call("propose_item", scope, by=by, **fields)}, 201)

    def _connect(self) -> dict:
        """What an outside agent's config needs besides its token (the "Connect your own agents" card): the
        sf-items command, its MCP arguments, the token's variable and this server's URL."""
        return {"sf_items": str(SF_ITEMS), "mcp_args": ["mcp"], "token_env": "SF_ITEMS_TOKEN",
                "url_env": "SF_ITEMS_URL", "url": self.server.url.rstrip("/")}

    def api_agent_tokens(self, req: ApiRequest) -> Reply:
        """GET /api/agent-tokens → {tokens: [{name, created, last_used}], connect: {...}}."""
        return json_reply({"tokens": self._call("agent_tokens"), "connect": self._connect()})

    def api_create_agent_token(self, req: ApiRequest) -> Reply:
        """POST /api/agent-tokens {name} → {name, token, connect} 201: the token is shown this once."""
        return json_reply({**self._call("create_agent_token", req.text("name")), "connect": self._connect()}, 201)

    def api_revoke_agent_token(self, req: ApiRequest) -> Reply:
        self._call("revoke_agent_token", req.params["token_name"])
        return json_reply({"tokens": self._call("agent_tokens"), "connect": self._connect()})

    def api_job_items(self, req: ApiRequest) -> Reply:
        return json_reply(self._call("scope_items", f"job:{req.params['repo']}/{req.params['job']}"))

    def api_add_job_item(self, req: ApiRequest) -> Reply:
        return self._add_item(f"job:{req.params['repo']}/{req.params['job']}", req,
                              ("title", "detail", "blocking", "owner", "verifier"))

    def api_chat_items(self, req: ApiRequest) -> Reply:
        return json_reply(self._call("scope_items", f"chat:{req.params['chat']}"))

    def api_add_chat_item(self, req: ApiRequest) -> Reply:
        return self._add_item(f"chat:{req.params['chat']}", req, ("title", "detail", "blocking"))

    def api_plan_items(self, req: ApiRequest) -> Reply:
        return json_reply(self._call("scope_items", f"plan:{req.params['repo']}/{req.params['plan']}"))

    def api_followups(self, req: ApiRequest) -> Reply:
        self.server.current_config().repo(req.params["repo"])
        return json_reply(self._call("scope_items", f"repo:{req.params['repo']}"))

    def api_add_followup(self, req: ApiRequest) -> Reply:
        self.server.current_config().repo(req.params["repo"])
        return self._add_item(f"repo:{req.params['repo']}", req, ("title", "detail", "due"))

    # setup, secrets and docs

    def api_setup(self, req: ApiRequest) -> Reply:
        return json_reply(self._call("setup_status"))

    def api_secrets(self, req: ApiRequest) -> Reply:
        return json_reply(self._call("secret_status"))

    def api_set_secret(self, req: ApiRequest) -> Reply:
        """Store one key in <config dir>/env: {name, value} ("" removes it). The reply names the key and
        whether it is set; the value is never echoed, logged or put in an error."""
        name = req.body.get("name")
        value = req.body.get("value")
        if not isinstance(name, str) or not isinstance(value, str):
            raise HttpError(400, "'name' and 'value' must be strings")
        entry = self._call("set_secret", name, value)
        return json_reply({"secret": entry, **self._call("secret_status")})

    def api_procedures(self, req: ApiRequest) -> Reply:
        return json_reply(self._call("procedures"))

    def api_procedures_html(self, req: ApiRequest) -> Reply:
        body = str(self._call("procedures_html")).encode("utf-8")
        headers = {}
        if (req.arg("download") or "") not in ("", "0"):
            headers["Content-Disposition"] = 'attachment; filename="software-factory-procedures.html"'
        return Reply(200, body, HTML_TYPE, headers)

    def api_maps(self, req: ApiRequest) -> Reply:
        """The process maps as Mermaid source: [{id, title, mermaid, sha256}] (docs.mermaid_maps)."""
        return json_reply(self._call("maps"))

    def api_map_source(self, req: ApiRequest) -> Reply:
        """One map's Mermaid source as a .mmd download (the same text GET /api/docs/maps carries)."""
        wanted = req.params["map"]
        for entry in self._call("maps"):
            if isinstance(entry, dict) and entry.get("id") == wanted:
                body = str(entry.get("mermaid") or "").encode("utf-8")
                return Reply(200, body, TEXT_TYPE, {"Content-Disposition": f'attachment; filename="{wanted}.mmd"'})
        raise HttpError(404, f"no map named {wanted}")

    # product assumptions (section 7)

    def api_assumptions(self, req: ApiRequest) -> Reply:
        """The spec's product assumptions: {marked, confirmed, unconfirmed} (normalized lines)."""
        return json_reply(self._call("assumptions", req.params["repo"], req.params["job"]))

    def api_confirm(self, req: ApiRequest) -> Reply:
        """A person confirms product assumptions: {lines: [...]} (spec.md lines it marks) or {all: true}
        → {job, assumptions}. The only writer of the job's confirmed.md."""
        repo, job_id = req.params["repo"], req.params["job"]
        lines = req.body.get("lines")
        if req.body.get("all") is True and lines is None:
            lines = None
        elif ("all" in req.body or not isinstance(lines, list) or not lines or len(lines) > MAX_CONFIRM_LINES
              or not all(isinstance(line, str) and line.strip() and len(line) <= MAX_PARAM_CHARS for line in lines)):
            raise HttpError(400, "give 'lines', a list of the spec.md lines marked (product) to confirm, or "
                                 "{\"all\": true}")
        job = self._call("confirm", repo, job_id, lines)
        return json_reply({"job": job, "assumptions": self._call("assumptions", repo, job_id)})


def _evidence(value: Any) -> list[dict]:
    """The evidence list of an update: [{kind, ref, detail?}] (HttpError 400 if malformed)."""
    if not isinstance(value, list) or len(value) > MAX_EVIDENCE:
        raise HttpError(400, f"'evidence' must be a list of at most {MAX_EVIDENCE} {{kind, ref, detail?}} entries")
    found = []
    for entry in value:
        if not isinstance(entry, dict) or set(entry) - {"kind", "ref", "detail", "line"} \
                or not all(isinstance(entry.get(key), str) and len(entry[key]) <= 2000 for key in ("kind", "ref")) \
                or not isinstance(entry.get("detail", ""), str):
            raise HttpError(400, "each evidence entry is {kind: commit|command|file, ref, detail?}")
        found.append(dict(entry))
    return found


def _flag(value: str | None) -> bool:
    """A boolean query parameter: 1/true/yes (any case) is True; absent, empty, 0/false/no False."""
    if value is None or value.strip().lower() in ("", "0", "false", "no"):
        return False
    if value.strip().lower() in ("1", "true", "yes"):
        return True
    raise HttpError(400, f"invalid flag {value!r} (use 1 or 0)")


def _decode_param(key: str, raw: str) -> str:
    """URL-decode one path segment and validate it by its parameter name (HttpError 400)."""
    try:
        value = unquote(raw, errors="strict")
    except UnicodeDecodeError:
        raise HttpError(400, f"invalid {key}") from None
    rule = _PARAM_RULES.get(key)
    if rule is not None and (len(value) > MAX_PARAM_CHARS or not rule[0].fullmatch(value)):
        raise HttpError(400, f"invalid {rule[1]}")
    return value


def _route(template: str) -> re.Pattern[str]:
    """Compile "/api/jobs/{repo}/{job}/{action:approve|retry}" into a regex with named groups."""
    def group(match: re.Match[str]) -> str:
        name, _, choices = match.group(1).partition(":")
        return f"(?P<{name}>{choices or '[^/]+'})"
    return re.compile(re.sub(r"\{([^}]+)\}", group, template))


Handler = Callable[[FactoryRequestHandler, ApiRequest], "Reply | None"]
_H = FactoryRequestHandler
_ROUTES: list[tuple[str, re.Pattern[str], Handler]] = [
    (method, _route(template), handler) for method, template, handler in [
        ("GET", "/api/state", _H.api_state),
        ("GET", "/api/events", _H.api_events),
        ("GET", "/api/doctor", _H.api_doctor),
        ("POST", "/api/jobs", _H.api_submit),
        ("GET", "/api/jobs/{repo}/{job}", _H.api_job),
        ("GET", "/api/jobs/{repo}/{job}/file", _H.api_job_file),
        ("GET", "/api/jobs/{repo}/{job}/log", _H.api_job_log),
        ("GET", "/api/jobs/{repo}/{job}/diff", _H.api_job_diff),
        ("GET", "/api/jobs/{repo}/{job}/items", _H.api_job_items),
        ("POST", "/api/jobs/{repo}/{job}/items", _H.api_add_job_item),
        ("POST", "/api/jobs/{repo}/{job}/{action:approve|rework|reject|retry|cancel|routing}", _H.api_job_action),
        ("POST", "/api/repos", _H.api_add_repo),
        ("GET", "/api/repos/{repo}/backlog", _H.api_backlog),
        ("POST", "/api/repos/{repo}/backlog", _H.api_add_backlog),
        ("POST", "/api/repos/{repo}/next", _H.api_next),
        ("GET", "/api/repos/{repo}/scout", _H.api_scout_status),
        ("POST", "/api/repos/{repo}/scout", _H.api_scout),
        ("GET", "/api/repos/{repo}/followups", _H.api_followups),
        ("POST", "/api/repos/{repo}/followups", _H.api_add_followup),
        ("POST", "/api/agents", _H.api_new_agent),
        ("GET", "/api/agents/{agent}", _H.api_agent),
        ("POST", "/api/agents/{agent}", _H.api_save_agent),
        ("POST", "/api/agents/{agent}/reset", _H.api_reset_agent),
        ("GET", "/api/chats", _H.api_chats),
        ("POST", "/api/chats", _H.api_new_chat),
        ("GET", "/api/chats/{chat}", _H.api_chat),
        ("GET", "/api/chats/{chat}/items", _H.api_chat_items),
        ("POST", "/api/chats/{chat}/items", _H.api_add_chat_item),
        ("POST", "/api/chats/{chat}/{action:messages|cancel|handoff|delete}", _H.api_chat_action),
        ("GET", "/api/personas", _H.api_personas),
        ("POST", "/api/personas/import", _H.api_persona_import),
        ("POST", "/api/personas/check", _H.api_persona_check),
        ("GET", "/api/personas/{persona}", _H.api_persona),
        ("GET", "/api/personas/{persona}/card", _H.api_persona_card),
        ("GET", "/api/openwebui/models", _H.api_openwebui_models),
        ("GET", "/api/config", _H.api_config),
        ("POST", "/api/config", _H.api_update_config),
        ("POST", "/api/config/preset", _H.api_preset),
        ("POST", "/api/models/{model}/test", _H.api_test_model),
        ("GET", "/api/estimate", _H.api_estimate),
        ("GET", "/api/plans", _H.api_plans),
        ("POST", "/api/plans", _H.api_new_plan),
        ("GET", "/api/plans/{repo}/{plan}", _H.api_plan),
        ("GET", "/api/plans/{repo}/{plan}/file", _H.api_plan_file),
        ("GET", "/api/plans/{repo}/{plan}/log", _H.api_plan_log),
        ("GET", "/api/plans/{repo}/{plan}/items", _H.api_plan_items),
        ("POST", "/api/plans/{repo}/{plan}/plan", _H.api_plan_text),
        ("POST", "/api/plans/{repo}/{plan}/{action:run|cancel|retry}", _H.api_plan_action),
        ("GET", "/api/items", _H.api_items),
        ("GET", "/api/items/history", _H.api_item_history),
        ("POST", "/api/items/propose", _H.api_propose_item),
        ("POST", "/api/items/{action:" + "|".join(ITEM_ACTIONS) + "}", _H.api_item_action),
        ("GET", "/api/system/items", _H.api_system_items),
        ("POST", "/api/system/items", _H.api_add_system_item),
        ("GET", "/api/agent-tokens", _H.api_agent_tokens),
        ("POST", "/api/agent-tokens", _H.api_create_agent_token),
        ("POST", "/api/agent-tokens/{token_name}/revoke", _H.api_revoke_agent_token),
        ("GET", "/api/setup", _H.api_setup),
        ("GET", "/api/secrets", _H.api_secrets),
        ("POST", "/api/secrets", _H.api_set_secret),
        ("GET", "/api/docs/procedures", _H.api_procedures),
        ("GET", r"/api/docs/procedures\.html", _H.api_procedures_html),
        ("GET", "/api/docs/maps", _H.api_maps),
        ("GET", r"/api/docs/maps/{map}\.mmd", _H.api_map_source),
        ("GET", "/api/jobs/{repo}/{job}/assumptions", _H.api_assumptions),
        ("POST", "/api/jobs/{repo}/{job}/confirm", _H.api_confirm),
    ]
]


# The only handlers an outside agent's token reaches (each narrows what the token may do itself).
AGENT_TOKEN_ROUTES = frozenset({_H.api_items, _H.api_add_system_item, _H.api_add_followup, _H.api_propose_item,
                                _H.api_item_action})


def make_server(config: Config, factory: Any, bus: EventBus, host: str | None = None, port: int | None = None,
                *, ping_interval: float = PING_INTERVAL, web_dir: Path = WEB_DIR) -> FactoryHTTPServer:
    """Bind the server (host/port default to config server.host/port; port 0 picks a free one).

    The caller runs serve_forever() and later shutdown() + server_close().
    ValueError if the config has no server token.
    """
    server = _section(config, "server")
    host = host or str(server.get("host") or "127.0.0.1")
    port = int(server.get("port") or 8765) if port is None else int(port)
    return FactoryHTTPServer(config, factory, bus, host, port, ping_interval=ping_interval, web_dir=web_dir)


def run_server(config: Config, factory: Any, bus: EventBus, host: str | None = None,
               port: int | None = None) -> None:
    """Serve until SIGTERM or SIGINT; then close event streams, stop the server and factory.stop().

    bus must redact factory.secret_values() (orchestrator.new_factory builds the pair that way), so
    a key only a per-run preset's provider knows never reaches an event stream or a run log.
    """
    server = make_server(config, factory, bus, host, port)
    stop = threading.Event()
    restore = _install_signal_handlers(lambda signum, frame: stop.set())
    worker = threading.Thread(target=server.serve_forever, name="factory-http", daemon=True)
    worker.start()
    print(f"Software Factory {__version__} listening on {server.url}", flush=True)
    print("Sign in with the link `factory url` prints (it carries the token).", flush=True)
    try:
        while worker.is_alive() and not stop.wait(0.5):
            pass
    finally:
        server.shutdown()
        try:
            factory.stop()
        finally:
            server.server_close()
            restore()
            worker.join(timeout=5)


def _install_signal_handlers(handler: Callable[[int, Any], None]) -> Callable[[], None]:
    """Route SIGTERM and SIGINT to handler (main thread only); returns a function that restores them."""
    if threading.current_thread() is not threading.main_thread():
        return lambda: None
    previous = {sig: signal.signal(sig, handler) for sig in (signal.SIGTERM, signal.SIGINT)}

    def restore() -> None:
        for sig, old in previous.items():
            signal.signal(sig, old)
    return restore
