"""Phone channels for Wake & Messages (docs/seraphina/WAKE_AND_MESSAGES.md §2.7, §5.9, §5.10): ntfy on the
tailnet and Telegram, behind one small interface.

A channel only moves text that the Notifier's content gate already chose (notify.render); it never
decides what may leave. Both channels use outbound connections only: the ntfy server is on this
machine (a Unix socket, or a loopback listener as the fallback) and Telegram is long-polled, so no
port opens.

ntfy uses three topics, because an ntfy message does not say who published it: notices (written by
the harness; the receiver reads it too and reports any message it did not publish as foreign),
taps (button presses, JSON ``{"v": 1, "verb": ..., "cap": ...}`` and nothing else; anything else
there is forged) and chat (text the owner types on Android). The receiver reads all three in one
stream and decides by topic, never by content.

Telegram: getUpdates long polling (never a webhook), one poller per token, the offset saved after
each update even when handling it failed, and two-step pairing: a one-time code sent to the bot as
``/start <code>`` makes the bot answer that chat with a 6-digit code, and only typing that code in
the cockpit adds the sender's numeric id. Paired ids live in the channel's state file, never in
config. The bot token is in every request URL, so no error, repr or log line ever carries a URL.

Outcomes (DESIGN.md section 40): a send is "sent" only on the server's confirming answer, "failed" when the
server refused it or the request never left, and "unknown" when it may have arrived: no answer after sending,
or an answer of 500 or more (the server failed while handling it and does not say what it did). The Notifier
never sends an unknown one again by itself, and a retry of a Telegram send skips the accounts an earlier try
reached (already).

channel_privacy computes a channel's class (S30): ``tailnet`` only for ntfy whose publish URL is a
Unix socket or loopback and whose public URL is on this tailnet (the MagicDNS suffix, or a
100.64.0.0/10 address of this node or a listed peer); everything else is ``third_party``.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import http.client
import ipaddress
import json
import os
import re
import secrets
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import outcomes as rule

PRIVACY_TAILNET = "tailnet"
PRIVACY_THIRD_PARTY = "third_party"
PRIORITIES = ("min", "low", "default", "high", "max")        # ntfy 1-5
NTFY_PRIORITY = {name: index + 1 for index, name in enumerate(PRIORITIES)}
CHANNEL_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
CAP_RE = re.compile(r"^[A-Z2-7]{26}$")                       # 16 random bytes in base32, no padding
VERB_RE = re.compile(r"^[a-z][a-z0-9_]{0,23}$")
TAILNET_NET = ipaddress.ip_network("100.64.0.0/10")
# server.yml options that would send message text (or a pointer to it) off the machine (§2.7)
NTFY_FORWARDERS = ("firebase-key-file", "smtp-sender-addr", "smtp-server-listen", "twilio-account",
                   "web-push-public-key", "web-push-private-key", "web-push-file")
NTFY_RELAY = "upstream-base-url"
NTFY_SERVER_YML = Path("/etc/ntfy/server.yml")
TELEGRAM_API = "https://api.telegram.org"
USER_AGENT = "SoftwareFactory-notify/1"
MAX_RESPONSE_BYTES = 1 << 20
MAX_LINE_BYTES = 64 * 1024
MAX_INBOUND_TEXT = 2000
SEND_TIMEOUT = 15.0
NTFY_STREAM_TIMEOUT = 120.0          # the server sends a keepalive every 45 s
TELEGRAM_POLL_SECONDS = 50
TELEGRAM_HTTP_TIMEOUT = 65.0
BACKOFF_START = 5.0
BACKOFF_MAX = 300.0
PAIRING_CODE_CHARS = 12
PAIRING_MAX_PENDING = 3
CONFIRM_DIGITS = 6
CONFIRM_TRIES = 3
OWN_IDS_KEPT = 1000
OWN_TEXT_SECONDS = 600.0
SEEN_IDS_KEPT = 1000
BUTTON_LABELS = {"ack": "Got it", "snooze_1h": "Snooze 1 h", "snooze_3h": "Snooze 3 h",
                 "snooze_morning": "Snooze until morning", "quiet_3h": "Quiet 3 h", "quiet_morning": "Quiet until morning",
                 "stop": "Stop Seraphina"}
BOT_COMMANDS = [("stop", "Stop Seraphina (restart her on your computer)"), ("snooze", "snooze 1h, 3h or morning"),
                ("quiet", "quiet 3h or morning: hold messages"), ("mute", "mute an assistant until morning"),
                ("status", "what waits for you, in counts"), ("help", "the commands, and where to read answers")]
BOT_DESCRIPTION = "Telegram can read what you type here."   # the Notifier adds the power schedule (bot_description)
# The ntfy URLs are pasted into server.yml and into commands the owner runs as root: nothing but a plain URL passes.
UNIX_PATH_RE = re.compile(r"^/(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+$")
LOOPBACK_URL_RE = re.compile(r"^http://(?:127\.\d{1,3}\.\d{1,3}\.\d{1,3}|localhost|\[::1\])(?::\d{1,5})?/?$")
PUBLIC_URL_RE = re.compile(r"^https://(?P<host>[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                           r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+)(?::(?P<port>\d{1,5}))?/?$")
_URL_IN_TEXT_RE = re.compile(r"(?i)\b(?:https?://|www\.)\S+|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|net|org|io|me|"
                             r"ly|co|app|dev|xyz|info|ru|cn|uk|de)\b(?:/\S*)?")


def utc_stamp(at: float | None = None) -> str:
    """Seconds since the epoch (default now) as a stored UTC time."""
    return datetime.fromtimestamp(time.time() if at is None else at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_capability() -> str:
    """A fresh button capability: 16 random bytes in base32 (26 characters)."""
    return base64.b32encode(secrets.token_bytes(16)).decode("ascii").rstrip("=")


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class Button:
    label: str
    verb: str
    cap: str = ""                                            # the raw capability, in memory only while sending


@dataclass
class Rendered:
    """What one channel receives, after the content gate."""
    title: str
    text: str
    why: str
    priority: str
    buttons: list[Button] = field(default_factory=list)
    link: str | None = None
    sequence: str | None = None                              # ntfy sequence id, or the Telegram message to edit

    def body(self) -> str:
        """The text as the phone shows it under the title: the message, then the code-written why line."""
        return f"{self.text}\n{self.why}" if self.why else self.text

    def sha256(self) -> str:
        """The sha256 of exactly what is sent as words (title, text, why), for the outbox and the audit."""
        return digest(json.dumps([self.title, self.text, self.why], ensure_ascii=False))


@dataclass
class SendResult:
    state: str                                               # "sent" | "failed" | "unknown"
    remote_id: str | None = None
    error: str | None = None
    retry_after: float | None = None


@dataclass
class Inbound:
    channel: str
    kind: str          # "button" | "text" | "command" | "stranger" | "pairing" | "forged" | "foreign" | "ignored"
    topic: str | None = None                                 # ntfy: which of the three topics
    sender: str | None = None                                # an allowlisted id; None for a stranger
    text: str = ""
    verb: str | None = None
    cap: str | None = None
    reply_to: str | None = None                              # remote id of the notice it answers
    sent_at: str | None = None
    received_at: str = ""
    first_batch: bool = False                                # the first getUpdates batch after boot (S34)
    forwarded: bool = False
    remote_id: str = ""


class ChannelError(Exception):
    """A channel problem. Its message never holds a token or a URL with one."""

    def __init__(self, message: str, *, retryable: bool = True, status: int | None = None,
                 retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status
        self.retry_after = retry_after


class _NotSent(ChannelError):
    """The request never reached the server (connect or send failed): safe to retry."""


class _NoAnswer(ChannelError):
    """The request was sent and no answer confirmed or refused it (none came, or the server failed while handling
    it: 500 or more): it may have arrived (delivery state "unknown", DESIGN §40)."""


# ---------------------------------------------------------------- privacy (S30)


def _host_of(url: Any) -> str | None:
    if not isinstance(url, str):
        return None
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower() or None
    except ValueError:
        return None


def is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def local_publish_url(url: Any) -> bool:
    """True for "unix:<absolute path>" (letters, digits, '.', '_', '-' and '/', no "..") and for a plain http URL on
    a loopback host (http://127.0.0.1:2586, no path, query or user)."""
    if isinstance(url, str) and url.startswith("unix:"):
        path = url[5:]
        return bool(UNIX_PATH_RE.fullmatch(path)) and ".." not in path.split("/")
    if not isinstance(url, str) or not LOOPBACK_URL_RE.fullmatch(url):
        return False
    return is_loopback_host(_host_of(url))


def public_url_ok(url: Any) -> bool:
    """True for a plain https URL: a host name or address and an optional port, nothing else."""
    if not isinstance(url, str):
        return False
    match = PUBLIC_URL_RE.fullmatch(url)
    return bool(match) and (match.group("port") is None or 0 < int(match.group("port")) < 65536)


def could_be_tailnet(entry: Mapping[str, Any]) -> bool:
    """The static part of S30, for settings validation: an ntfy channel with a local publish URL and an https
    public URL on a *.ts.net name or a 100.64.0.0/10 address. Whether that name or address really is this
    tailnet's is checked at run time (channel_privacy)."""
    if entry.get("type") != "ntfy" or not local_publish_url(entry.get("publish_url")):
        return False
    public = entry.get("public_url")
    if not public_url_ok(public):
        return False
    host = _host_of(public)
    if not host:
        return False
    if host.endswith(".ts.net"):
        return True
    try:
        return ipaddress.ip_address(host) in TAILNET_NET
    except ValueError:
        return False


def ntfy_forwarders(server_yml: str | None) -> list[str]:
    """The forwarding options server.yml sets (a key with a non-empty value, outside comments)."""
    if not server_yml:
        return []
    found = []
    for raw in server_yml.splitlines():
        line = raw.split("#", 1)[0].strip()
        key, colon, value = line.partition(":")
        if colon and key.strip() in (*NTFY_FORWARDERS, NTFY_RELAY) and value.strip().strip("\"'"):
            found.append(key.strip())
    return found


def channel_privacy(entry: Mapping[str, Any], tailnet: Mapping[str, Any] | None,
                    server_yml: str | None = None) -> str:
    """S30: "tailnet" only for an enabled-or-not ntfy entry whose publish URL is local and whose public URL's
    host ends in this tailnet's MagicDNS suffix or is a 100.64.0.0/10 address listed in tailnet["addresses"],
    and whose server.yml (when given) sets no forwarder (the iPhone relay only with entry["ios_relay"] true,
    the owner's recorded consent). Everything else, and anything unknown, is "third_party"."""
    if not could_be_tailnet(entry) or not isinstance(tailnet, Mapping):
        return PRIVACY_THIRD_PARTY
    host = _host_of(entry.get("public_url")) or ""
    suffix = str(tailnet.get("suffix") or "").strip(".").lower()
    addresses = {str(a) for a in tailnet.get("addresses") or []}
    on_tailnet = bool(suffix) and host.endswith("." + suffix)
    if not on_tailnet:
        try:
            on_tailnet = ipaddress.ip_address(host) in TAILNET_NET and host in addresses
        except ValueError:
            on_tailnet = False
    if not on_tailnet:
        return PRIVACY_THIRD_PARTY
    forwarders = ntfy_forwarders(server_yml)
    if any(option != NTFY_RELAY for option in forwarders):
        return PRIVACY_THIRD_PARTY
    if NTFY_RELAY in forwarders and entry.get("ios_relay") is not True:
        return PRIVACY_THIRD_PARTY
    return PRIVACY_TAILNET


def tailnet_status(run: Callable[[list[str]], str] | None = None, confirmed: str | None = None) -> dict | None:
    """{suffix, addresses, funnel} from `tailscale status --json`, or None when Tailscale cannot be asked. On a server
    whose Tailscale runs only in containers there is no tailscale command to ask; then confirmed, the tailnet suffix
    the owner confirmed in Your server (yourserver.tailnet.suffix), is the suffix, with no addresses and Funnel
    unknown (YOUR_SERVER.md Appendix A11): {suffix, addresses: [], funnel: None, source: "confirmed"}."""
    runner = run or _run
    text = runner(["tailscale", "status", "--json"])
    try:
        data = json.loads(text) if text.strip() else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        suffix = str(confirmed or "").strip(".").lower()
        if suffix and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}(\.[a-z0-9][a-z0-9-]{0,62})*\.ts\.net", suffix):
            return {"suffix": suffix, "addresses": [], "funnel": None, "source": "confirmed"}
        return None
    addresses: list[str] = []
    self_node = data.get("Self") if isinstance(data.get("Self"), dict) else {}
    addresses += [str(a) for a in self_node.get("TailscaleIPs") or []]
    peers = data.get("Peer") if isinstance(data.get("Peer"), dict) else {}
    for peer in peers.values():
        if isinstance(peer, dict):
            addresses += [str(a) for a in peer.get("TailscaleIPs") or []]
    tailnet = data.get("CurrentTailnet") if isinstance(data.get("CurrentTailnet"), dict) else {}
    caps = self_node.get("CapMap") if isinstance(self_node.get("CapMap"), dict) else {}
    return {"suffix": str(tailnet.get("MagicDNSSuffix") or "") or None, "addresses": addresses,
            "funnel": any("funnel" in str(key) for key in caps)}


def _run(argv: list[str], timeout: float = 5.0) -> str:
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""
    return done.stdout if done.returncode == 0 else ""


# ---------------------------------------------------------------- HTTP over a Unix socket


class UnixHTTPConnection(http.client.HTTPConnection):
    """An HTTPConnection that connects with AF_UNIX to path ("unix:<path>" publish URLs)."""

    def __init__(self, path: str, timeout: float | None = None) -> None:
        super().__init__("localhost", timeout=timeout)
        self.unix_path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self.unix_path)
        except BaseException:
            sock.close()
            raise
        self.sock = sock


def connection_for(url: str, timeout: float) -> tuple[http.client.HTTPConnection, str]:
    """(an unopened connection, the path prefix) for "unix:/path" or an http(s) URL."""
    if url.startswith("unix:"):
        return UnixHTTPConnection(url[5:], timeout=timeout), ""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ChannelError("the publish URL must be unix:/path or an http(s) URL", retryable=False)
    cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    return cls(parts.hostname, parts.port, timeout=timeout), parts.path.rstrip("/")


def _shutdown(conn: http.client.HTTPConnection | None) -> None:
    sock = getattr(conn, "sock", None)
    if sock is not None:
        with contextlib.suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)


# ---------------------------------------------------------------- the interface


class Channel:
    """One way to the phone. entry() returns the live config entry each time it is called."""

    type: str = ""
    can_receive = False
    has_buttons = False
    max_title = 60
    max_text = 300

    def __init__(self, name: str, entry: Callable[[], dict], environ: Mapping[str, str], state_path: Path, *,
                 privacy: Callable[[dict], str] | None = None, now: Callable[[], float] = time.time,
                 backoff: tuple[float, float] = (BACKOFF_START, BACKOFF_MAX)) -> None:
        self.name = name
        self.entry = entry
        self.environ = environ
        self.state_path = Path(state_path)
        self._privacy = privacy or (lambda _entry: PRIVACY_THIRD_PARTY)
        self._now = now
        self.backoff = backoff
        self._state_lock = threading.RLock()
        self.on_event: Callable[[str, dict], None] = lambda kind, data: None

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name}>"

    @property
    def privacy(self) -> str:
        try:
            return self._privacy(self.entry())
        except Exception:   # noqa: BLE001 - unknown means third party
            return PRIVACY_THIRD_PARTY

    # state file -------------------------------------------------------------------

    def state(self) -> dict:
        with self._state_lock:
            try:
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {}
            return data if isinstance(data, dict) else {}

    def update_state(self, change: Callable[[dict], Any]) -> Any:
        """Read the state file, apply change(state) and write it back atomically (0600); change's result."""
        with self._state_lock:
            data = self.state()
            result = change(data)
            self.state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.state_path.parent, prefix=f".{self.state_path.name}.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(data, handle, indent=2, sort_keys=True)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, self.state_path)
            except BaseException:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(tmp)
                raise
            return result

    def _health(self, ok: bool, detail: str, **extra: Any) -> None:
        """Record the channel's health. Bookkeeping only: a failed write (a full disk) is dropped, never raised, so
        it cannot turn a send the other side answered into an error (DESIGN §40)."""
        def change(state: dict) -> bool:
            health = state.setdefault("health", {})
            before = (health.get("ok"), health.get("detail"))
            health.update({"ok": ok, "detail": detail, "checked_at": utc_stamp(self._now()), **extra})
            if ok:
                health["last_ok"] = health["checked_at"]
            return before != (ok, detail)
        try:
            changed = self.update_state(change)
        except OSError:
            return
        if changed:
            self.on_event("health", {"ok": ok, "detail": detail})

    def note(self, key: str) -> None:
        """Record now as health[key] (last_sent, last_received, last_answer); bookkeeping, like _health."""
        stamp = utc_stamp(self._now())
        with contextlib.suppress(OSError):
            self.update_state(lambda state: state.setdefault("health", {}).__setitem__(key, stamp))

    def health(self) -> dict:
        health = dict(self.state().get("health") or {})
        return {"ok": health.get("ok"), "detail": health.get("detail") or "not checked yet",
                "checked_at": health.get("checked_at"), "last_ok": health.get("last_ok"),
                "last_sent": health.get("last_sent"), "last_received": health.get("last_received"),
                "last_answer": health.get("last_answer")}

    # what a subclass provides ---------------------------------------------------------

    def token_names(self) -> list[str]:
        return []

    def secrets(self) -> list[str]:
        """Its token values, for redaction."""
        return [value for name in self.token_names() if (value := self.environ.get(name))]

    def ready(self) -> bool:
        """Configured well enough to send (a token, and for Telegram a paired account)."""
        return False

    def can_listen(self) -> bool:
        """Whether its receiver should run (Telegram also while pairing)."""
        return self.can_receive and self.ready()

    def restart_keys(self) -> tuple:
        return ()

    def send(self, rendered: Rendered, idempotency_key: str, *, already: str | None = None) -> SendResult:
        """Send rendered. already: the remote ids an earlier, failed try of this delivery got (a channel that
        reaches several accounts skips those; one publish reaches every phone at once and ignores it)."""
        raise NotImplementedError

    def update(self, remote_id: str, rendered: Rendered) -> SendResult:
        return self.send(rendered, f"update:{remote_id}")

    def receive(self, stop: threading.Event) -> Iterator[Inbound]:
        return iter(())

    def finish(self, inbound: Inbound, answer: str | None) -> SendResult | None:
        """After the Notifier handled inbound: answer it (when answer) and tidy up (Telegram removes buttons and
        deletes typed text)."""
        return None

    def close(self) -> None:
        """End a blocking read now (the receiver's stop event is already set)."""

    def start_pairing(self) -> dict | None:
        return None

    def confirm_pairing(self, code: str) -> dict | None:
        return None

    def pair_status(self) -> dict:
        return {"state": "paired" if self.ready() else "waiting", "account": None}

    def paired(self) -> list[dict]:
        return []

    def unpair(self, account_id: int | str) -> bool:
        return False

    def unpair_all(self) -> int:
        return 0

    def pairing_fingerprint(self) -> str:
        """sha256 of the pairing state (S47): the paired accounts."""
        return digest(json.dumps(sorted(str(entry.get("id")) for entry in self.paired())))


# ---------------------------------------------------------------- ntfy


class NtfyChannel(Channel):
    """ntfy on this machine: POST <publish_url>/ (JSON publish); one stream over the three topics."""

    type = "ntfy"
    can_receive = True
    has_buttons = True

    def __init__(self, *args: Any, stream_timeout: float = NTFY_STREAM_TIMEOUT, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.stream_timeout = stream_timeout
        self._active: http.client.HTTPConnection | None = None
        self._active_lock = threading.Lock()
        self._own_ids: deque[str] = deque(maxlen=OWN_IDS_KEPT)
        self._own_texts: dict[str, float] = {}
        self._seen: deque[str] = deque(maxlen=SEEN_IDS_KEPT)
        self._own_lock = threading.Lock()
        for message_id in self.state().get("own_ids") or []:
            self._own_ids.append(str(message_id))

    # settings -----------------------------------------------------------------------

    def _e(self, key: str, default: Any = None) -> Any:
        value = self.entry().get(key)
        return default if value in (None, "") else value

    def topics(self) -> tuple[str, str, str]:
        return (str(self._e("topic", "seraphina")), str(self._e("taps_topic", "seraphina-taps")),
                str(self._e("chat_topic", "seraphina-chat")))

    def token_names(self) -> list[str]:
        return [str(name) for name in (self._e("token_env"), self._e("reply_token_env")) if name]

    def _token(self) -> str | None:
        name = self._e("token_env")
        return self.environ.get(str(name)) if name else None

    def _reply_token(self) -> str | None:
        name = self._e("reply_token_env")
        return self.environ.get(str(name)) if name else None

    def ready(self) -> bool:
        return bool(self._token()) and bool(self._e("publish_url"))

    def restart_keys(self) -> tuple:
        entry = self.entry()
        return tuple(entry.get(key) for key in ("publish_url", "token_env", "topic", "taps_topic", "chat_topic"))

    def buttons_ready(self) -> bool:
        """Buttons need the reply token and a public URL the phone can post to."""
        return bool(self._reply_token()) and isinstance(self._e("public_url"), str)

    # HTTP -----------------------------------------------------------------------------

    def _request(self, method: str, path: str, body: dict | None = None, timeout: float = SEND_TIMEOUT,
                 token: str | None = None) -> tuple[int, bytes]:
        conn, prefix = connection_for(str(self._e("publish_url")), timeout)
        headers = {"User-Agent": USER_AGENT}
        token = token if token is not None else self._token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            try:
                conn.request(method, prefix + path, body=data, headers=headers)
            except (OSError, http.client.HTTPException) as exc:
                raise _NotSent(f"could not reach the ntfy server ({type(exc).__name__})") from None
            try:
                response = conn.getresponse()
                payload = response.read(MAX_RESPONSE_BYTES)
            except (OSError, http.client.HTTPException) as exc:
                raise _NoAnswer(f"the ntfy server did not answer ({type(exc).__name__})") from None
            return response.status, payload
        finally:
            conn.close()

    def publish_payload(self, rendered: Rendered) -> dict:
        topic = self.topics()[0]
        payload: dict[str, Any] = {"topic": topic, "title": rendered.title, "message": rendered.body(),
                                   "priority": NTFY_PRIORITY.get(rendered.priority, 3)}
        if rendered.sequence:
            payload["sequence_id"] = rendered.sequence
        if rendered.link:
            payload["click"] = rendered.link
        actions = []
        if rendered.buttons and self.buttons_ready():
            url = str(self._e("public_url")).rstrip("/") + "/" + self.topics()[1]
            for button in rendered.buttons[:3]:
                body = json.dumps({"v": 1, "verb": button.verb, "cap": button.cap}, separators=(",", ":"))
                action = {"action": "http", "label": button.label, "url": url, "method": "POST",
                          "headers": {"Authorization": f"Bearer {self._reply_token()}"}, "body": body}
                if button.verb == "ack":
                    action["clear"] = True
                actions.append(action)
        if actions:
            payload["actions"] = actions
        return payload

    def _remember_text(self, title: str, message: str) -> str:
        key = digest(json.dumps([title, message], ensure_ascii=False))
        now = time.monotonic()
        with self._own_lock:
            for old in [k for k, t in self._own_texts.items() if now - t > OWN_TEXT_SECONDS]:
                del self._own_texts[old]
            self._own_texts[key] = now
        return key

    def _remember_id(self, message_id: str) -> None:
        """Keep the id of a notice ntfy accepted, so the stream drops its echo. Bookkeeping after a confirmed
        answer: a failed write keeps the id in memory only and never fails the send (DESIGN §40)."""
        with self._own_lock:
            self._own_ids.append(message_id)
            kept = list(self._own_ids)
        with contextlib.suppress(OSError):
            self.update_state(lambda state: state.__setitem__("own_ids", kept[-OWN_IDS_KEPT:]))

    def is_own(self, message_id: str, title: str | None = None, message: str | None = None) -> bool:
        with self._own_lock:
            if message_id in self._own_ids:
                return True
            if title is None and message is None:
                return False
            return digest(json.dumps([title or "", message or ""], ensure_ascii=False)) in self._own_texts

    def send(self, rendered: Rendered, idempotency_key: str, *, already: str | None = None) -> SendResult:
        if not self.ready():
            return SendResult("failed", error="the ntfy token or publish URL is not set")
        payload = self.publish_payload(rendered)
        self._remember_text(payload["title"], payload["message"])
        try:
            status, body = self._request("POST", "/", payload)
        except _NoAnswer as exc:
            return SendResult("unknown", error=str(exc))
        except ChannelError as exc:
            self._health(False, str(exc))
            return SendResult("failed", error=str(exc))
        outcome = rule.of_status(status)
        if outcome == rule.UNKNOWN:   # it failed while handling the publish: the phone may have it already
            detail = f"the ntfy server answered HTTP {status} after the notice was sent: it may have arrived"
            self._health(False, detail)
            return SendResult("unknown", error=detail)
        if outcome == rule.FAILED:
            retry = 60.0 if status == 429 else None
            detail = f"the ntfy server answered HTTP {status}"
            if status in (401, 403):
                detail += " (check the seraphina token and its access)"
            self._health(False, detail)
            return SendResult("failed", error=detail, retry_after=retry)
        try:
            message_id = str(json.loads(body.decode("utf-8")).get("id") or "")
        except (ValueError, AttributeError, UnicodeDecodeError):
            message_id = ""
        if message_id:
            self._remember_id(message_id)
        self._health(True, "sending works")
        self.note("last_sent")
        return SendResult("sent", remote_id=message_id or None)

    def update(self, remote_id: str, rendered: Rendered) -> SendResult:
        """Publish again with the notice's sequence id (Android replaces the notification; iPhone shows a new one)."""
        return self.send(rendered, f"update:{remote_id}")

    def finish(self, inbound: Inbound, answer: str | None) -> SendResult | None:
        if not answer:
            return None
        return self.send(Rendered(title="Your server", text=answer, why="", priority="low"), f"answer:{inbound.remote_id}")

    def check_health(self) -> tuple[bool, str]:
        """GET /v1/health: (ok, detail)."""
        try:
            status, body = self._request("GET", "/v1/health", timeout=5.0, token="")
        except ChannelError as exc:
            return False, str(exc)
        try:
            healthy = json.loads(body.decode("utf-8")).get("healthy") is True
        except (ValueError, AttributeError, UnicodeDecodeError):
            healthy = False
        return (status == 200 and healthy), f"/v1/health answered HTTP {status}"

    def poll(self, topic: str, token: str | None = None) -> int | None:
        """An authenticated poll of one topic (GET /<topic>/json?poll=1&since=1m): the HTTP status, None when
        the server cannot be reached."""
        try:
            status, _ = self._request("GET", f"/{topic}/json?poll=1&since=1m", timeout=5.0, token=token)
        except ChannelError:
            return None
        return status

    # receiving ---------------------------------------------------------------------

    def close(self) -> None:
        with self._active_lock:
            _shutdown(self._active)

    def receive(self, stop: threading.Event) -> Iterator[Inbound]:
        delay = self.backoff[0]
        while not stop.is_set():
            notices, taps, chat = self.topics()
            since = str(self.state().get("last_id") or "10m")
            path = f"/{notices},{taps},{chat}/json?since={urllib.parse.quote(since, safe='')}"
            conn: http.client.HTTPConnection | None = None
            try:
                conn, prefix = connection_for(str(self._e("publish_url")), self.stream_timeout)
                with self._active_lock:
                    self._active = conn
                token = self._token()
                conn.request("GET", prefix + path, headers={"User-Agent": USER_AGENT,
                                                            **({"Authorization": f"Bearer {token}"} if token else {})})
                response = conn.getresponse()
                if response.status != 200:
                    raise ChannelError(f"the ntfy server answered HTTP {response.status} to the subscription")
                self._health(True, "listening")
                delay = self.backoff[0]
                while not stop.is_set():
                    line = response.readline(MAX_LINE_BYTES)
                    if not line:
                        break
                    inbound = self._line_to_inbound(line, (notices, taps, chat))
                    if inbound is not None:
                        self.note("last_received")
                        yield inbound
            except (OSError, ValueError, http.client.HTTPException, ChannelError) as exc:
                if stop.is_set():
                    break
                detail = str(exc) if isinstance(exc, ChannelError) else \
                    f"the stream from the ntfy server ended ({type(exc).__name__})"
                self._health(False, detail)
                rule.retried("ntfy subscription")
                stop.wait(delay)
                delay = min(delay * 2, self.backoff[1])
            finally:
                with self._active_lock:
                    self._active = None
                if conn is not None:
                    conn.close()

    def _line_to_inbound(self, line: bytes, topics: tuple[str, str, str]) -> Inbound | None:
        try:
            event = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(event, dict) or event.get("event") != "message":
            return None   # open, keepalive, poll_request
        message_id = str(event.get("id") or "")
        if not message_id:
            return None
        with self._own_lock:
            if message_id in self._seen:
                return None
            self._seen.append(message_id)
        self.update_state(lambda state: state.__setitem__("last_id", message_id))
        notices, taps, chat = topics
        topic = str(event.get("topic") or "")
        sent = event.get("time")
        sent_at = utc_stamp(float(sent)) if isinstance(sent, (int, float)) and not isinstance(sent, bool) else None
        text = str(event.get("message") or "")[:MAX_INBOUND_TEXT]
        base = {"channel": self.name, "topic": topic, "sent_at": sent_at, "received_at": utc_stamp(self._now()),
                "remote_id": message_id}
        if topic == notices:
            if self.is_own(message_id, str(event.get("title") or ""), str(event.get("message") or "")):
                return None
            return Inbound(kind="foreign", text=text, **base)
        if topic == taps:
            tap = parse_tap(event.get("message"))
            if tap is None:
                return Inbound(kind="forged", text=text, **base)
            return Inbound(kind="button", verb=tap[0], cap=tap[1], sender="phone", **base)
        if topic == chat:
            return Inbound(kind="text", text=text.strip(), sender="phone", forwarded=has_link(text), **base)
        return None

    def start_pairing(self) -> dict | None:
        notices, taps, chat = self.topics()
        return {"server_url": self._e("public_url"), "topics": {"notices": notices, "taps": taps, "chat": chat},
                "username": "owner-phone"}

    def pair_status(self) -> dict:
        last = self.health().get("last_answer")
        return {"state": "paired" if last else "waiting", "account": {"id": "owner-phone"} if last else None}


def parse_tap(message: Any) -> tuple[str, str] | None:
    """(verb, cap) from a taps-topic body that is exactly {"v": 1, "verb": ..., "cap": ...}; None otherwise."""
    if not isinstance(message, str) or len(message) > 200:
        return None
    try:
        data = json.loads(message)
    except ValueError:
        return None
    if not isinstance(data, dict) or set(data) != {"v", "verb", "cap"} or data.get("v") != 1:
        return None
    verb, cap = data.get("verb"), data.get("cap")
    if not isinstance(verb, str) or not VERB_RE.fullmatch(verb) or not isinstance(cap, str) or not CAP_RE.fullmatch(cap):
        return None
    return verb, cap


def has_link(text: str) -> bool:
    return bool(_URL_IN_TEXT_RE.search(text or ""))


# ---------------------------------------------------------------- Telegram


class TelegramError(ChannelError):
    """A Bot API refusal or failure, named by method only (the URL holds the token)."""

    def __init__(self, method: str, detail: str, **kwargs: Any) -> None:
        super().__init__(f"Telegram {method}: {detail}", **kwargs)
        self.method = method


class TelegramChannel(Channel):
    """A Telegram bot, long-polled. api_base is a constructor argument (tests point it at a mock), never a
    config key, so a config edit cannot send the bot token elsewhere."""

    type = "telegram"
    can_receive = True
    has_buttons = True

    def __init__(self, *args: Any, api_base: str = TELEGRAM_API, poll_seconds: int = TELEGRAM_POLL_SECONDS,
                 http_timeout: float = TELEGRAM_HTTP_TIMEOUT, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.api_base = api_base.rstrip("/")
        self.poll_seconds = poll_seconds
        self.http_timeout = http_timeout
        local = is_loopback_host(_host_of(self.api_base))
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if local \
            else urllib.request.build_opener()
        self.describe: Callable[[], str] = lambda: BOT_DESCRIPTION   # the Notifier sets it from wakes.power

    def token_names(self) -> list[str]:
        name = self.entry().get("token_env")
        return [str(name)] if name else []

    def _token(self) -> str | None:
        names = self.token_names()
        return self.environ.get(names[0]) if names else None

    def ready(self) -> bool:
        return bool(self._token()) and bool(self.paired())

    def can_listen(self) -> bool:
        return bool(self._token())

    def restart_keys(self) -> tuple:
        return (self.entry().get("token_env"),)

    def _redact(self, text: str) -> str:
        for secret in self.secrets():
            if secret:
                text = text.replace(secret, "***")
        return text

    def call(self, method: str, params: dict | None = None, http_timeout: float = SEND_TIMEOUT) -> Any:
        """One Bot API call; TelegramError (named by method only) on any refusal or network trouble."""
        token = self._token()
        if not token:
            raise TelegramError(method, "no bot token is set", retryable=False)
        request = urllib.request.Request(f"{self.api_base}/bot{token}/{method}",
                                         data=json.dumps(params or {}).encode("utf-8"), method="POST",
                                         headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
        status = 200
        try:
            with self._opener.open(request, timeout=http_timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES)
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                raw = exc.read(MAX_RESPONSE_BYTES)
            except OSError:
                raw = b""
            finally:
                exc.close()
        except urllib.error.URLError as exc:
            reason = type(exc.reason).__name__ if isinstance(exc.reason, BaseException) else "network error"
            raise _NotSent(f"Telegram {method}: could not reach Telegram ({reason})") from None
        except (OSError, http.client.HTTPException) as exc:
            raise _NoAnswer(f"Telegram {method}: no answer from Telegram ({type(exc).__name__})") from None
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            if rule.of_status(status) == rule.FAILED:
                raise TelegramError(method, f"HTTP {status} without a JSON answer", status=status) from None
            raise _NoAnswer(f"Telegram {method}: HTTP {status} without a JSON answer", status=status) from None
        if isinstance(data, dict) and data.get("ok") is True:
            return data.get("result")
        if rule.of_status(status) == rule.UNKNOWN:   # Telegram failed while handling it: it may have been done
            raise _NoAnswer(f"Telegram {method}: answered HTTP {status} (it may have been done)", status=status)
        code = data.get("error_code", status) if isinstance(data, dict) else status
        description = self._redact(str(data.get("description") or "refused"))[:200] if isinstance(data, dict) else ""
        parameters = data.get("parameters") if isinstance(data, dict) and isinstance(data.get("parameters"), dict) else {}
        retry_after = parameters.get("retry_after")
        raise TelegramError(method, f"{code} {description}".strip(), status=int(code) if isinstance(code, int) else None,
                            retry_after=float(retry_after) if isinstance(retry_after, (int, float)) else None,
                            retryable=code not in (400, 401, 403, 404))

    # sending ------------------------------------------------------------------------

    def _message_body(self, rendered: Rendered) -> dict:
        lines = [rendered.title, rendered.text] + ([rendered.why] if rendered.why else [])
        body: dict[str, Any] = {"text": "\n".join(line for line in lines if line)[:4096], "protect_content": True,
                                "link_preview_options": {"is_disabled": True},
                                "disable_notification": rendered.priority in ("min", "low")}
        if rendered.buttons:
            body["reply_markup"] = {"inline_keyboard": [[{"text": b.label, "callback_data": f"v1:{b.verb}:{b.cap}"}
                                                         for b in rendered.buttons[:3]]]}
        return body

    def send(self, rendered: Rendered, idempotency_key: str, *, already: str | None = None) -> SendResult:
        """sendMessage to each paired account, in order. An account named in already (an earlier try's remote
        ids) is skipped: a retry never reaches an account twice (DESIGN §40)."""
        accounts = self.paired()
        if not accounts or not self._token():
            return SendResult("failed", error="Telegram is not paired")
        reached = {part.partition(":")[0]: part for part in (already or "").split(",") if part}
        remote: list[str] = []
        body = self._message_body(rendered)
        for account in accounts:
            if str(account["id"]) in reached:
                remote.append(reached[str(account["id"])])
                continue
            try:
                result = self.call("sendMessage", {"chat_id": account["id"], **body})
            except _NoAnswer as exc:
                return SendResult("unknown", remote_id=",".join(remote) or None, error=str(exc))
            except ChannelError as exc:
                self._health(False, str(exc))
                return SendResult("failed", remote_id=",".join(remote) or None, error=str(exc),
                                  retry_after=exc.retry_after)
            message_id = result.get("message_id") if isinstance(result, dict) else None
            remote.append(f"{account['id']}:{message_id}")
        self._health(True, "sending works")
        self.note("last_sent")
        return SendResult("sent", remote_id=",".join(remote))

    def update(self, remote_id: str, rendered: Rendered) -> SendResult:
        """editMessageText on each message of remote_id. An account whose edit was refused (or whose message id
        cannot be read) gets one new message instead; the accounts edited are passed as already, so none gets the
        notice twice. An edit with no answer may have been applied: the result is unknown and nothing is sent in
        its place (DESIGN §40)."""
        body = self._message_body(rendered)
        edited: list[str] = []
        refused = False
        for part in remote_id.split(","):
            chat_id, _, message_id = part.partition(":")
            try:
                params = {"chat_id": int(chat_id), "message_id": int(message_id), "text": body["text"],
                          "link_preview_options": {"is_disabled": True},
                          **({"reply_markup": body["reply_markup"]} if "reply_markup" in body else {})}
            except ValueError:
                refused = True
                continue
            try:
                self.call("editMessageText", params)
            except _NoAnswer as exc:
                return SendResult("unknown", error=f"{exc}: the edit may have been applied")
            except ChannelError:
                refused = True
                continue
            edited.append(part)
        if refused:
            return self.send(rendered, f"update:{remote_id}", already=",".join(edited) or None)
        return SendResult("sent", remote_id=remote_id)

    def finish(self, inbound: Inbound, answer: str | None) -> SendResult | None:
        """A tap: answer the callback and remove the buttons. Typed text: delete it from the chat (S37), then
        answer in the chat when there is something to say."""
        if inbound.kind in ("stranger", "pairing") or inbound.sender is None:
            return None
        chat_id = int(inbound.sender)
        with contextlib.suppress(ChannelError):
            if inbound.kind == "button":
                self.call("answerCallbackQuery", {"callback_query_id": inbound.remote_id,
                                                  "text": (answer or "")[:200]})
                if inbound.reply_to and inbound.reply_to.isdigit():
                    self.call("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": int(inbound.reply_to),
                                                         "reply_markup": {"inline_keyboard": []}})
                return None
            if inbound.remote_id.isdigit():
                self.call("deleteMessage", {"chat_id": chat_id, "message_id": int(inbound.remote_id)})
        if answer:
            try:
                self.call("sendMessage", {"chat_id": chat_id, "text": answer[:4096], "protect_content": True,
                                          "link_preview_options": {"is_disabled": True}, "disable_notification": True})
            except ChannelError as exc:
                return SendResult("failed", error=str(exc))
            return SendResult("sent")
        return None

    # pairing ------------------------------------------------------------------------

    def paired(self) -> list[dict]:
        return [dict(entry) for entry in self.state().get("paired") or [] if isinstance(entry, dict)
                and isinstance(entry.get("id"), int)]

    def paired_ids(self) -> set[int]:
        return {entry["id"] for entry in self.paired()}

    def bot(self) -> dict:
        """{id, username} of the bot as getMe last said (asked now when unknown)."""
        bot = self.state().get("bot")
        if isinstance(bot, dict) and bot.get("username"):
            return bot
        me = self.call("getMe")
        bot = {"id": me.get("id"), "username": me.get("username")} if isinstance(me, dict) else {}
        self.update_state(lambda state: state.__setitem__("bot", bot))
        return bot

    def start_pairing(self, minutes: float = 10) -> dict:
        """A one-time code (12 characters, single use, 10 minutes, at most 3 pending): {code, link, text,
        expires_at}. Only its sha256 is stored."""
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
        code = "".join(secrets.choice(alphabet) for _ in range(PAIRING_CODE_CHARS))
        expires = self._now() + minutes * 60
        username = self.bot().get("username") or ""

        def change(state: dict) -> None:
            now = self._now()
            codes = [c for c in state.get("codes") or [] if isinstance(c, dict) and c.get("expires", 0) > now]
            codes.append({"sha256": digest(code), "expires": expires})
            state["codes"] = codes[-PAIRING_MAX_PENDING:]
            state["pairing_started"] = utc_stamp(now)
        self.update_state(change)
        return {"code": code, "link": f"https://t.me/{username}?start={code}" if username else None,
                "text": f"/start {code}", "expires_at": utc_stamp(expires)}

    def _take_code(self, code: str) -> bool:
        wanted = digest(code)

        def change(state: dict) -> bool:
            now = self._now()
            codes = [c for c in state.get("codes") or [] if isinstance(c, dict) and c.get("expires", 0) > now]
            match = next((c for c in codes if secrets.compare_digest(str(c.get("sha256")), wanted)), None)
            if match is not None:
                codes.remove(match)
            state["codes"] = codes
            return match is not None
        return self.update_state(change)

    def _begin_confirm(self, user: dict, chat_id: int) -> None:
        confirm = "".join(secrets.choice("0123456789") for _ in range(CONFIRM_DIGITS))
        self.call("sendMessage", {"chat_id": chat_id, "protect_content": True,
                                  "text": f"Your code is {confirm}. Type it into the cockpit on your computer "
                                          "(Messages, Phone setup). Never type it anywhere else."})
        expires = self._now() + 10 * 60
        self.update_state(lambda state: state.__setitem__("confirm", {
            "id": int(user.get("id")), "username": user.get("username"), "first_name": user.get("first_name"),
            "sha256": digest(confirm), "expires": expires, "tries": 0}))

    def confirm_pairing(self, code: str) -> dict:
        """{state, account}: "paired" when code is the 6-digit code the bot sent; three wrong codes cancel."""
        def change(state: dict) -> dict:
            pending = state.get("confirm")
            if not isinstance(pending, dict) or pending.get("expires", 0) <= self._now():
                state.pop("confirm", None)
                return {"state": "expired", "account": None}
            if not secrets.compare_digest(str(pending.get("sha256")), digest(str(code).strip())):
                pending["tries"] = int(pending.get("tries") or 0) + 1
                if pending["tries"] >= CONFIRM_TRIES:
                    state.pop("confirm", None)
                    return {"state": "expired", "account": None}
                return {"state": "confirm", "account": None, "wrong": True}
            account = {"id": int(pending["id"]), "username": pending.get("username"),
                       "first_name": pending.get("first_name"), "paired_at": utc_stamp(self._now())}
            paired = [entry for entry in state.get("paired") or [] if isinstance(entry, dict)
                      and entry.get("id") != account["id"]]
            state["paired"] = [*paired, account]
            state["last_paired"] = account
            state.pop("confirm", None)
            state["codes"] = []
            return {"state": "paired", "account": account}
        result = self.update_state(change)
        if result["state"] == "paired":
            self.on_event("pairing", {"paired": True, "account": result["account"]["id"]})
            self._set_menu()
        return result

    def _set_menu(self) -> bool:
        """The bot's commands and its description (self.describe(): the power schedule); the description's
        sha256 is kept in the state file, so menu_current() can tell when it is out of date."""
        text = str(self.describe() or BOT_DESCRIPTION)[:512]
        try:
            self.call("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in BOT_COMMANDS]})
            self.call("setMyDescription", {"description": text})
            self.call("setMyShortDescription", {"short_description": text[:120]})
        except ChannelError:
            return False
        self.update_state(lambda state: state.__setitem__("menu", digest(text)))
        return True

    def menu_current(self) -> bool:
        """True when the bot's description says what self.describe() says now."""
        return self.state().get("menu") == digest(str(self.describe() or BOT_DESCRIPTION)[:512])

    def refresh_menu(self) -> bool:
        """Set the menu again (the Notifier calls it while menu_current() is False, at most hourly)."""
        rule.retried("Telegram bot menu")
        return self._set_menu()

    def pair_status(self) -> dict:
        state = self.state()
        now = self._now()
        pending = state.get("confirm")
        if isinstance(pending, dict) and pending.get("expires", 0) > now:
            return {"state": "confirm", "account": {"id": pending.get("id"), "username": pending.get("username"),
                                                    "first_name": pending.get("first_name")}}
        codes = [c for c in state.get("codes") or [] if isinstance(c, dict) and c.get("expires", 0) > now]
        if codes:
            return {"state": "waiting", "account": None}
        last = state.get("last_paired")
        if isinstance(last, dict) and str(last.get("paired_at") or "") >= str(state.get("pairing_started") or ""):
            return {"state": "paired", "account": {k: last.get(k) for k in ("id", "username", "first_name")}}
        return {"state": "expired" if state.get("pairing_started") else "waiting", "account": None}

    def unpair(self, account_id: int | str) -> bool:
        try:
            wanted = int(account_id)
        except (TypeError, ValueError):
            return False

        def change(state: dict) -> bool:
            before = list(state.get("paired") or [])
            state["paired"] = [e for e in before if not (isinstance(e, dict) and e.get("id") == wanted)]
            return len(state["paired"]) != len(before)
        removed = self.update_state(change)
        if removed:
            self.on_event("pairing", {"paired": False, "account": wanted})
        return removed

    def unpair_all(self) -> int:
        def change(state: dict) -> int:
            count = len(state.get("paired") or [])
            state.update(paired=[], codes=[])
            state.pop("confirm", None)
            return count
        count = self.update_state(change)
        if count:
            self.on_event("pairing", {"paired": False, "account": "all"})
        return count

    # receiving ----------------------------------------------------------------------

    def boot_check(self) -> None:
        """S51: getMe and getWebhookInfo before the first poll; a changed bot or a webhook is an alarm."""
        try:
            me = self.call("getMe")
            hook = self.call("getWebhookInfo")
        except ChannelError as exc:
            self._health(False, str(exc))
            return
        bot = {"id": me.get("id"), "username": me.get("username")} if isinstance(me, dict) else {}
        known = self.state().get("bot")
        if isinstance(known, dict) and known.get("id") and known.get("id") != bot.get("id"):
            self.on_event("alarm", {"kind": "bot_changed"})
        self.update_state(lambda state: state.__setitem__("bot", bot))
        if isinstance(hook, dict) and hook.get("url"):
            self.on_event("alarm", {"kind": "webhook"})

    def receive(self, stop: threading.Event) -> Iterator[Inbound]:
        """Long-poll getUpdates. It first drains what waited while nothing listened (getUpdates with timeout 0
        until it returns nothing): only those updates are first_batch (S34), never a tap the owner makes while
        the first long poll waits."""
        self.boot_check()
        first = True
        delay = self.backoff[0]
        while not stop.is_set():
            offset = int(self.state().get("offset") or 0)
            try:
                updates = self.call("getUpdates", {"offset": offset, "timeout": 0 if first else self.poll_seconds,
                                                   "limit": 100, "allowed_updates": ["message", "callback_query"]},
                                    http_timeout=self.http_timeout)
            except TelegramError as exc:
                if stop.is_set():
                    break
                if exc.status == 409:
                    self._health(False, "Another program is reading this bot's messages.")
                    self.on_event("conflict", {})
                else:
                    self._health(False, str(exc))
                rule.retried("Telegram getUpdates")
                stop.wait(exc.retry_after or 10)
                continue
            except ChannelError as exc:
                if stop.is_set():
                    break
                self._health(False, str(exc))
                rule.retried("Telegram getUpdates")
                stop.wait(delay)
                delay = min(delay * 2, self.backoff[1])
                continue
            delay = self.backoff[0]
            self._health(True, "listening")
            for update in updates if isinstance(updates, list) else []:
                update_id = update.get("update_id") if isinstance(update, dict) else None
                try:
                    inbound = self._to_inbound(update, first)
                except Exception as exc:  # noqa: BLE001 - one bad update never stalls the channel
                    inbound = Inbound(channel=self.name, kind="error", text=f"{type(exc).__name__}",
                                      received_at=utc_stamp(self._now()), remote_id=str(update_id))
                if inbound is not None:
                    self.note("last_received")
                    yield inbound
                if isinstance(update_id, int):
                    self.update_state(lambda state, n=update_id + 1: state.__setitem__("offset", n))
            if first and not (isinstance(updates, list) and updates):
                first = False   # the backlog is drained: from now on every update arrives as it happens

    def _to_inbound(self, update: dict, first_batch: bool) -> Inbound | None:
        now = utc_stamp(self._now())
        paired = self.paired_ids()
        query = update.get("callback_query")
        if isinstance(query, dict):
            sender = (query.get("from") or {}).get("id")
            message = query.get("message") if isinstance(query.get("message"), dict) else {}
            chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
            if sender not in paired or chat.get("type") != "private" or chat.get("id") != sender:
                return Inbound(channel=self.name, kind="stranger", received_at=now, remote_id=str(query.get("id")))
            verb = cap = None
            parts = str(query.get("data") or "").split(":")
            if len(parts) == 3 and parts[0] == "v1" and VERB_RE.fullmatch(parts[1]) and CAP_RE.fullmatch(parts[2]):
                verb, cap = parts[1], parts[2]
            return Inbound(channel=self.name, kind="button", sender=str(sender), verb=verb, cap=cap,
                           reply_to=str(message.get("message_id")) if message.get("message_id") is not None else None,
                           received_at=now, first_batch=first_batch, remote_id=str(query.get("id")))
        message = update.get("message")
        if not isinstance(message, dict):
            return None
        sender = (message.get("from") or {}).get("id")
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        private = chat.get("type") == "private" and chat.get("id") == sender and isinstance(sender, int)
        text = message.get("text")
        date = message.get("date")
        sent_at = utc_stamp(float(date)) if isinstance(date, (int, float)) and not isinstance(date, bool) else None
        base = {"channel": self.name, "received_at": now, "sent_at": sent_at, "first_batch": first_batch,
                "remote_id": str(message.get("message_id"))}
        if private and isinstance(text, str):
            start = re.fullmatch(r"/start ([A-Za-z0-9]{%d})" % PAIRING_CODE_CHARS, text.strip())
            if start and self._take_code(start.group(1)):
                self._begin_confirm(message.get("from") or {}, sender)
                return Inbound(kind="pairing", **base)
        if not private or sender not in paired:
            return Inbound(kind="stranger", channel=self.name, received_at=now, remote_id=base["remote_id"])
        if not isinstance(text, str):
            return Inbound(kind="ignored", sender=str(sender), **base)
        reply = message.get("reply_to_message") if isinstance(message.get("reply_to_message"), dict) else {}
        entities = message.get("entities") if isinstance(message.get("entities"), list) else []
        linked = any(isinstance(e, dict) and e.get("type") in ("url", "text_link") for e in entities)
        return Inbound(kind="text", sender=str(sender), text=text.strip()[:MAX_INBOUND_TEXT],
                       reply_to=f"{sender}:{reply.get('message_id')}" if reply.get("message_id") is not None else None,
                       forwarded=bool(message.get("forward_origin")) or linked or has_link(text), **base)


CHANNEL_TYPES: dict[str, type[Channel]] = {"ntfy": NtfyChannel, "telegram": TelegramChannel}


def make_channel(name: str, entry: Callable[[], dict], environ: Mapping[str, str], state_dir: Path,
                 **kwargs: Any) -> Channel:
    """The channel for a config entry (ValueError for an unknown type). kwargs for a type it does not take
    (api_base for ntfy, stream_timeout for Telegram) are dropped."""
    current = entry()
    cls = CHANNEL_TYPES.get(str(current.get("type")))
    if cls is None:
        raise ValueError(f"notify.channels.{name}.type must be one of {', '.join(CHANNEL_TYPES)}")
    allowed = {"privacy", "now", "backoff"} | ({"stream_timeout"} if cls is NtfyChannel else
                                                {"api_base", "poll_seconds", "http_timeout"})
    return cls(name, entry, environ, Path(state_dir) / f"{name}.json",
               **{key: value for key, value in kwargs.items() if key in allowed})
