"""Plain HTTP for the harness's own clients of outside services: the GPU custody register and drift sources
(DESIGN.md sections 37 and 38).

One request, with exactly the headers the caller gives: http.client adds only Host (unless the caller gives
one), Accept-Encoding: identity and Content-Length. Nothing else goes out: no User-Agent unless given, and
never an Origin or a Sec-Fetch-* header (a browser sends those; a service that also serves browsers can tell
the harness apart, and no header the caller did not choose leaves the machine). No redirect is followed (a
3xx is an answer, so a Bearer token never follows a redirect to another host), no proxy is used (these
services live on the machine or its private network), and the body read is capped.

HTTPS is verified, against the system's trust store or, with ca_file, against that file only (a CA or the
service's own certificate, for a service with a private certificate). With a Host header set, the certificate
must name that host (and the TLS server name sent is that host), so ``https://127.0.0.1:<port>`` with
``Host: checker.internal`` is checked against checker.internal.

A failure says whether the request may have reached the service. Before the connection is made it did not
(unreachable, timeout: ``sent`` False); once the request went out, a lost connection or a timeout is outcome
unknown (``sent`` True), which the custody client never replays.

A Bearer token (read_bearer) is read again for every request, from one of three places a setting names:
``token_file`` (a file the harness user can read), ``token_env`` (an environment variable; the env file is
loaded into the environment at start) or ``token_credential`` (a systemd credential: the file
``$CREDENTIALS_DIRECTORY/<name>`` that a ``LoadCredential=<name>:<path>`` line in the harness's unit gives it).
A setting names token_credential alone, never with token_file or token_env (check_token_source). A token_file
is never inside a systemd unit's credentials directory (in_credentials_dir): the harness reads only its own copy of
a token, through token_credential, never another service's credentials.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import ssl
import stat
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

MAX_BYTES = 1 << 20
MAX_TOKEN_BYTES = 4096
HEADER_VALUE_RE = re.compile(r"^[\x20-\x7e]{1,255}$")   # one line of printable ASCII (no CR or LF)
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
# A systemd credential's name (LoadCredential=<name>:<path>): a plain file name, never a path.
CREDENTIAL_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.@+-]{0,127}$")
CREDENTIALS_ENV = "CREDENTIALS_DIRECTORY"   # where systemd puts a unit's credentials
# Where systemd keeps a running unit's credentials: /run/credentials/<unit>/ for a system unit and
# /run/user/<uid>/credentials/<unit>/ ($XDG_RUNTIME_DIR/credentials/<unit>/) for a user unit.
_CREDENTIALS_DIR_RE = re.compile(r"^/run/(user/[0-9]+/)?credentials(/|$)")


class HttpFailure(Exception):
    """No answer. kind: bad_url, unreachable, timeout or lost; sent: whether the request may have reached the
    service (True: its outcome is unknown)."""

    def __init__(self, kind: str, detail: str, sent: bool) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail
        self.sent = sent


@dataclass
class Reply:
    """An answer: the status, the headers (lower-case names) and the body (at most max_bytes; truncated when
    there was more)."""

    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    truncated: bool = False

    def json(self) -> Any:
        """The body as JSON, or None when it is not JSON (or was cut off)."""
        if self.truncated:
            return None
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None

    def text(self, limit: int = 2000) -> str:
        return self.body[:limit].decode("utf-8", "replace")


def check_url(url: Any) -> str | None:
    """Why url cannot be used (an http(s) URL with a host, no user or password in it), or None."""
    if not isinstance(url, str) or not url.strip():
        return "it must be an http(s) URL"
    try:
        parts = urllib.parse.urlsplit(url)
        parts.port   # noqa: B018 - raises ValueError for a bad port
    except ValueError as exc:
        return f"it is not a valid URL ({exc})"
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "it must be an http(s) URL with a host"
    if parts.username or parts.password:
        return "it must not carry a user or password (use token_file, token_env or token_credential)"
    if parts.fragment:
        return "it must not have a #fragment"
    return None


class _Https(http.client.HTTPSConnection):
    """HTTPS to host whose certificate is checked against (and whose TLS server name is) tls_name."""

    def __init__(self, host: str, port: int | None, *, timeout: float, context: ssl.SSLContext,
                 tls_name: str) -> None:
        super().__init__(host, port, timeout=timeout, context=context)
        self._tls_name = tls_name

    def connect(self) -> None:
        http.client.HTTPConnection.connect(self)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self._tls_name)


def tls_name(url: str, headers: dict[str, str] | None) -> str:
    """The name an https service's certificate must carry: the Host header's host when one is set, else the
    URL's host."""
    for name, value in (headers or {}).items():
        if name.lower() == "host" and value:
            try:
                host = urllib.parse.urlsplit(f"//{value}").hostname
            except ValueError:
                host = None
            if host:
                return host
    return urllib.parse.urlsplit(url).hostname or ""


def request(method: str, url: str, headers: dict[str, str] | None = None, body: Any = None,
            timeout: float = 5.0, max_bytes: int = MAX_BYTES, ca_file: str | None = None) -> Reply:
    """Send one request (body: a JSON value, or None for none) and read the answer. HttpFailure when there is
    no answer; any status is an answer (a 3xx is never followed). ca_file: the only certificates an https
    service is checked against (default: the system's)."""
    problem = check_url(url)
    if problem:
        raise HttpFailure("bad_url", f"cannot use {url!r}: {problem}", False)
    parts = urllib.parse.urlsplit(url)
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    host, port = parts.hostname or "", parts.port
    where = f"{parts.scheme}://{parts.netloc}{parts.path}"
    if parts.scheme == "https":
        try:
            context = ssl.create_default_context(cafile=os.path.expanduser(ca_file) if ca_file else None)
        except (OSError, ssl.SSLError) as exc:
            why = getattr(exc, "strerror", None) or type(exc).__name__
            raise HttpFailure("unreachable", f"cannot reach {where}: ca_file {ca_file} cannot be loaded ({why})",
                              False) from None
        conn: http.client.HTTPConnection = _Https(host, port, timeout=timeout, context=context,
                                                  tls_name=tls_name(url, headers))
    else:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        try:
            conn.connect()
        except TimeoutError:
            raise HttpFailure("timeout", f"{where} did not accept a connection within {timeout:g}s", False) from None
        except ssl.SSLError as exc:
            raise HttpFailure("unreachable", f"cannot reach {where}: TLS: {exc}", False) from None
        except OSError as exc:
            raise HttpFailure("unreachable", f"cannot reach {where}: {exc.strerror or type(exc).__name__}",
                              False) from None
        data = None if body is None else json.dumps(body).encode("utf-8")
        sent_headers = dict(headers or {})
        if data is not None and not any(name.lower() == "content-type" for name in sent_headers):
            sent_headers["Content-Type"] = "application/json"
        try:
            conn.request(method, target, body=data, headers=sent_headers)
            response = conn.getresponse()
            raw = response.read(max_bytes + 1)
        except TimeoutError:
            raise HttpFailure("timeout", f"{where} sent no answer within {timeout:g}s (outcome unknown)",
                              True) from None
        except (OSError, http.client.HTTPException) as exc:
            raise HttpFailure("lost", f"the connection to {where} was lost (outcome unknown): "
                                      f"{type(exc).__name__}", True) from None
        return Reply(status=int(response.status),
                     headers={name.lower(): value for name, value in response.getheaders()},
                     body=raw[:max_bytes], truncated=len(raw) > max_bytes)
    finally:
        conn.close()


def check_credential_name(value: Any) -> str | None:
    """Why value cannot be a systemd credential name, or None. A name is a plain file name: 1 to 128 letters,
    digits and ``_ . @ + -``, not starting with a dot or a dash, with no slash, no ``..`` and no NUL."""
    if not isinstance(value, str) or not CREDENTIAL_NAME_RE.fullmatch(value) or ".." in value:
        return ("it must be a systemd credential name such as register-token (1 to 128 letters, digits and "
                "_ . @ + -, not starting with a dot or a dash, no slash and no '..')")
    return None


def in_credentials_dir(path: Any) -> bool:
    """Whether path ("~" expanded, symbolic links followed) is inside a systemd unit's credentials directory:
    /run/credentials/, /run/user/<uid>/credentials/, $XDG_RUNTIME_DIR/credentials/ or this process's own
    $CREDENTIALS_DIRECTORY. A token_file there is refused: the harness reads its own copy through
    token_credential and never reaches into another service's credentials."""
    try:
        real = os.path.realpath(os.path.expanduser(str(path)))
    except (OSError, ValueError):
        return False
    if _CREDENTIALS_DIR_RE.match(real):
        return True
    runtime = os.environ.get("XDG_RUNTIME_DIR") or ""
    for root in (os.path.join(runtime, "credentials") if runtime else "", os.environ.get(CREDENTIALS_ENV) or ""):
        if os.path.isabs(root):
            root = os.path.realpath(root)
            if root != "/" and (real == root or real.startswith(root.rstrip("/") + "/")):
                return True
    return False


def check_token_source(value: dict, label: str, required: bool = False) -> None:
    """ValueError naming the bad field when a setting's Bearer token source is wrong: token_file a non-empty
    string outside every systemd credentials directory (in_credentials_dir), token_env an environment variable
    name, token_credential a systemd credential name, and token_credential never together with token_file or
    token_env (required: one of them must be set)."""
    token_file, token_env, credential = value.get("token_file"), value.get("token_env"), value.get("token_credential")
    if token_file is not None and (not isinstance(token_file, str) or not token_file.strip()):
        raise ValueError(f"{label}.token_file must be a file path or null")
    if token_file and in_credentials_dir(token_file):
        raise ValueError(f"{label}.token_file {token_file} is inside a systemd credentials directory, which the "
                         f"harness never reads: give the harness its own copy with a LoadCredential=<name>:<path to "
                         f"the token file> line in its unit, and name it with token_credential")
    if token_env is not None and (not isinstance(token_env, str) or not _ENV_NAME_RE.fullmatch(token_env)):
        raise ValueError(f"{label}.token_env must be an environment variable name such as REGISTER_TOKEN, or null")
    if credential is not None:
        problem = check_credential_name(credential)
        if problem:
            raise ValueError(f"{label}.token_credential: {problem}")
        if token_file or token_env:
            raise ValueError(f"{label}: token_credential cannot be set together with token_file or token_env "
                             f"(name one place to read the token from)")
    if required and not (token_file or token_env or credential):
        raise ValueError(f"{label} needs token_file (a file the harness user can read), token_env (a credential "
                         f"name in the env file) or token_credential (a systemd credential of the harness's unit)")


def credential_path(name: Any) -> str | None:
    """$CREDENTIALS_DIRECTORY/<name>, or None when the directory is not set (or not an absolute path) or name
    is not a credential name."""
    directory = os.environ.get(CREDENTIALS_ENV) or ""
    if not os.path.isabs(directory) or check_credential_name(name):
        return None
    return os.path.join(directory, str(name))


def credentials_present() -> bool:
    """Whether this process was given systemd credentials ($CREDENTIALS_DIRECTORY set to an absolute path). A
    command run in a shell (factory doctor) never is: only the harness's unit gives them."""
    return os.path.isabs(os.environ.get(CREDENTIALS_ENV) or "")


def credential_problem(name: Any) -> str:
    """Why the systemd credential name gives no Bearer token, in plain words, with the unit line that fixes it."""
    line = f"LoadCredential={name}:<path to the token file>"
    fix = (f"Add the line {line} to the harness's systemd unit (for the system unit, as root: sudo systemctl edit "
           f"factory@<user>; for the user unit: ./install.sh --systemd-user --credential {name}:<path to the token "
           f"file>, which keeps the unit's other credential lines) and restart the harness.")
    if check_credential_name(name):
        return f"token_credential {name!r} is not a systemd credential name"
    directory = os.environ.get(CREDENTIALS_ENV) or ""
    if not credentials_present():
        return (f"its Bearer token is the systemd credential {name}, but {CREDENTIALS_ENV} is not set: this process "
                f"was not started by a systemd unit that loads credentials. {fix} A command run in a shell (such as "
                f"factory doctor) never has the credential: the doctor of the running server (GET /api/doctor, "
                f"the GUI's doctor) shows what the server sees.")
    path = os.path.join(directory, str(name))
    if not os.path.lexists(path):
        return (f"its Bearer token is the systemd credential {name}, but {path} does not exist: the harness's unit "
                f"has no {line} line, or its path is wrong. {fix}")
    return (f"its Bearer token is the systemd credential {name}, but {path} cannot be read or holds no token (it "
            f"must be a regular file of at most {MAX_TOKEN_BYTES} bytes with the token on its first non-empty line)")


def read_bearer(token_file: str | None, token_env: str | None, token_credential: str | None = None) -> str | None:
    """A Bearer token, read now (every request reads it again, so a rotated token is used at once): the first
    non-empty line of the systemd credential token_credential ($CREDENTIALS_DIRECTORY/<name>, never through a
    symbolic link) when one is named; else of token_file (a regular file this user can read, at most 4 KiB,
    UTF-8, never inside a systemd credentials directory), else the environment variable token_env (the env file
    is loaded into the environment at start). None when none gives one. The value is never logged, returned in a
    view or put in an error message."""
    if token_credential:
        path = credential_path(token_credential)
        return _first_line(path, os.O_NOFOLLOW) if path else None
    if token_file and not in_credentials_dir(token_file):   # never another service's credentials
        found = _first_line(os.path.expanduser(str(token_file)))
        if found:
            return found
    if token_env and _ENV_NAME_RE.fullmatch(str(token_env)):
        value = os.environ.get(str(token_env))
        if value and value.strip():
            return value.strip()
    return None


def bearer_of(entry: Any) -> str | None:
    """The Bearer token a config entry's token_file, token_env or token_credential gives (read_bearer), or None."""
    if not isinstance(entry, dict):
        return None
    return read_bearer(entry.get("token_file"), entry.get("token_env"), entry.get("token_credential"))


def _first_line(path: str, flags: int = 0) -> str | None:
    """The first non-empty line of path when it is a regular file of at most MAX_TOKEN_BYTES, UTF-8; else None."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | flags)   # a FIFO never blocks
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if stat.S_ISREG(info.st_mode) and info.st_size <= MAX_TOKEN_BYTES:
            text = os.read(fd, MAX_TOKEN_BYTES + 1).decode("utf-8")
            for line in text.splitlines():
                if line.strip():
                    return line.strip()
    except (OSError, UnicodeDecodeError):
        pass
    finally:
        os.close(fd)
    return None
