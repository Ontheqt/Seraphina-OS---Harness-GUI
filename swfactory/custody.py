"""GPU custody client (DESIGN.md section 37): before a local call that could load or swap a model, the harness
holds a request lease from the operator's GPU custody register.

The operator persona owns the custody register. The harness never mutates the register's state directly. A
POST /leases is a request that the register adjudicates, records and answers; the ledger is the operator's. The
harness runs no broker of its own: it is a client of the register's API, and only for a provider that names one
(``providers.<name>.custody``; absent, the default, custody is off).

Which calls need a lease (needs_lease): every call to a provider with custody that is marked ``lazy_load`` (a
server that loads its model on the first request) or has ``allow_model_swap`` (a Tabby-style server that loads
whatever model a request names). Custody follows the server, not a provider's name (server_of,
custody_on_server): another provider entry on the same server, or an older job's snapshot of a renamed one,
carries the same register. The placement takes the lease before the run (routing.choose_build with a
Claims): a denial, an error, a timeout or an unreachable register makes that build unavailable, so the run
follows the configured overflow path. Two refusals differ (the operator's decision): HELD (someone else holds the
card) waits, for at most the provider's overflow_after_seconds, asking again every HELD_RETRY_SECONDS, when the
placement can wait (Claims with a cancel event); one placement waits that long once in all, however many of its
builds answer HELD. FULL (the card cannot fit the request, whoever holds it) never waits: the placement goes on at
once to the model's next build (another card, or a smaller build), then the overflow. Nothing bypasses custody:
Provider.complete refuses a call to such a provider unless the call's provider config carries a valid lease for
that model (lease_problem), so a path that is not placed (a Test probe, a swap-back, a wake turn) takes its own
lease (leased) or does not call.

The register's contract is PROPOSED, pending the operator's confirmation, so everything that knows its paths
and JSON shapes is in RegisterAdapter:

- residency: ``GET <url>/residency`` → ``{resident: [{provider, model, vram_mib, since}], generation,
  observed_at, epoch?}``;
- a request lease: ``POST <url>/leases {provider, model, purpose, holder}`` → ``{granted, lease_id, holder,
  until, generation, reason_code, epoch?}``; ``POST <url>/leases/<lease_id>/renew`` and ``.../release`` answer
  in the same shape. reason_code is OK, HELD, FULL, NOT_RESIDENT, LEASED, MAINTENANCE or UNKNOWN (anything else
  reads as UNKNOWN; REFUSAL_WORDS says each in plain words). HELD: the card is held outside the register's leases
  (by the operator's own use of it, say); LEASED: a lease the register granted to another request covers it. A
  refusal may come with an error status (409, 423, 503) when its body is that shape; any other answer of 500 or
  more to a POST is OUTCOME_UNKNOWN (the register failed while handling it, so a grant or a release may hide in
  it: DESIGN §40), and another status is ERROR. The generation only grows within one epoch. The epoch names the
  register's instance: the register should derive it from the host's boot id (/proc/sys/kernel/random/boot_id)
  plus a counter it persists and moves on at every start, so a reboot or a restart of the register gives a new
  one. The client treats it as an opaque name.

The client's rules (CustodyClient):

- residency first: a claim reads residency before it asks for a lease (a crashed requester's model may still
  be resident after its lease expired), and the lease keeps what it saw;
- generation fencing: an answer whose generation is older than the last one seen in its epoch, or whose until
  has passed, is stale and refused (a stale grant is released, once); calls to one register go one at a time,
  so answers arrive in order. A new epoch restarts the fence (its generation may start again from 0) and loses
  every lease of the earlier epoch: no renew or release is sent for it again (a new instance may hand out the same
  lease id to someone else), and before a renew or a release of a lease granted under an epoch the client reads
  residency, so a restart it has not seen yet is seen first;
- releasing a request lease is never evidence that VRAM is free: residency is read again, separately, after
  every release, and kept with the lease;
- a lost connection while holding a lease (to the register, or to the model server during a leased call: no
  answer, or an answer cut off before it was whole) is outcome unknown: the client reads residency again,
  never assumes the lease was released and never replays the request;
- leases live in this process only: a restarted harness holds none and assumes none (each process is a new
  holder, HOLDER); an earlier process's lease expires at the register. Runs of one process that want the same
  model on the same server share one lease (lease_identity: whichever provider entry reaches the server),
  released when the last of them ends.

What the client notes (its short history) is also appended to a journal once a Factory names the data dir
(set_journal): ``<data_dir>/custody/journal-YYYY-MM.jsonl``, a record only (never read back as state), which the
operator's activity query lists (activity.py, DESIGN §36).
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import math
import os
import re
import threading
import time
import urllib.parse
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from . import outcomes as rule
from . import plainhttp

REASON_CODES = ("OK", "HELD", "FULL", "NOT_RESIDENT", "LEASED", "MAINTENANCE", "UNKNOWN")
# The harness's own words for an answer it did not get or cannot use (never sent to the register).
NO_ANSWER_CODES = ("UNREACHABLE", "TIMEOUT", "ERROR", "STALE", "OUTCOME_UNKNOWN")
# Every code in plain words, as a placement, a log line, a journal line and the GUI say it (refusal_words).
REFUSAL_WORDS = {
    "OK": "refused",
    "HELD": "the card is held by someone else",
    "FULL": "the card cannot fit this model now",
    "NOT_RESIDENT": "the model is not resident and may not be loaded now",
    "LEASED": "another lease covers it",
    "MAINTENANCE": "the server is in maintenance",
    "UNKNOWN": "the register gave no reason",
    "UNREACHABLE": "the custody register could not be reached",
    "TIMEOUT": "the custody register did not answer in time",
    "ERROR": "the custody register's answer could not be used",
    "STALE": "the custody register's answer was stale",
    "OUTCOME_UNKNOWN": "the outcome is unknown and the request was not replayed",
}
SETTINGS_KEYS = ("url", "token_file", "token_env", "token_credential", "host_header", "ca_file", "provider", "models",
                 "timeout")
LEASE_STATES = ("held", "lost", "unknown", "released", "unreleased")
DEFAULT_TIMEOUT = 5.0
MAX_TIMEOUT = 60.0
MAX_ANSWER_BYTES = 256 * 1024
MAX_ID_CHARS = 200
MIN_RENEW_SECONDS = 1.0
MAX_RENEW_WAIT = 3600.0          # the keeper looks at a lease at least hourly, however far off its until is
HELD_RETRY_SECONDS = 5.0         # a placement waiting out a HELD answer asks the register again this often
MAX_UNTIL = 253402300799.0       # 9999-12-31T23:59:59Z: an until past it cannot be read
HISTORY_KEPT = 50
JOURNAL_DIR = "custody"          # <data_dir>/custody/journal-YYYY-MM.jsonl: what the clients note, as a record
JOURNAL_REASON_CHARS = 300
# This process as the register sees it: a restarted harness is another holder, so it never takes an earlier
# process's lease for its own.
HOLDER = f"seraphina-os-harness:{os.getpid()}:{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------- settings


@dataclass(frozen=True)
class CustodySettings:
    """providers.<name>.custody: the register's url, its Bearer token (optional: token_file, token_env, or
    token_credential, a systemd credential of the harness's unit), the exact Host header to send, the CA to trust
    for an https register (ca_file; default the system's), the register's name for this server (provider; default
    the provider's own name), the register's names for its models (models: {model id: register name}) and the
    timeout."""

    url: str
    token_file: str | None = None
    token_env: str | None = None
    host_header: str | None = None
    provider: str | None = None
    models: tuple[tuple[str, str], ...] = ()
    timeout: float = DEFAULT_TIMEOUT
    ca_file: str | None = None
    token_credential: str | None = None

    def key(self) -> tuple:
        """One register, one client: the same url, token, Host header and trust."""
        return (self.url.rstrip("/"), self.token_file, self.token_env, self.host_header, self.ca_file,
                self.token_credential)

    def token(self) -> str | None:
        """The Bearer token, read now (plainhttp.read_bearer), or None."""
        return plainhttp.read_bearer(self.token_file, self.token_env, self.token_credential)

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"url": self.url, "timeout": self.timeout}
        for name in ("token_file", "token_env", "token_credential", "host_header", "ca_file", "provider"):
            if getattr(self, name):
                out[name] = getattr(self, name)
        if self.models:
            out["models"] = dict(self.models)
        return out


def parse_settings(value: Any, label: str) -> CustodySettings | None:
    """value (a provider's custody entry) as CustodySettings, None for none; ValueError naming the bad field."""
    if value is None:
        return None
    if isinstance(value, CustodySettings):
        return value
    if not isinstance(value, dict):
        raise ValueError(f'{label} must be an object like {{"url": "http://127.0.0.1:<port>"}} or null')
    unknown = sorted(str(key) for key in value if key not in SETTINGS_KEYS)
    if unknown:
        raise ValueError(f"{label}: unknown keys {', '.join(unknown)} (allowed: {', '.join(SETTINGS_KEYS)})")
    problem = plainhttp.check_url(value.get("url"))
    if problem:
        raise ValueError(f"{label}.url: {problem}")
    for key in ("ca_file", "provider"):
        item = value.get(key)
        if item is not None and (not isinstance(item, str) or not item.strip()):
            raise ValueError(f"{label}.{key} must be a non-empty string or null")
    plainhttp.check_token_source(value, label)
    host = value.get("host_header")
    if host is not None and (not isinstance(host, str) or not plainhttp.HEADER_VALUE_RE.fullmatch(host)):
        raise ValueError(f"{label}.host_header must be one line of printable ASCII (a host name, with :port if "
                         f"needed), or null")
    models = value.get("models")
    if models is not None and (not isinstance(models, dict) or not all(
            isinstance(k, str) and k and isinstance(v, str) and v for k, v in models.items())):
        raise ValueError(f"{label}.models must be an object of model ids to the register's names")
    timeout = value.get("timeout")
    if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                                or not 0 < timeout <= MAX_TIMEOUT):
        raise ValueError(f"{label}.timeout must be a number of seconds above 0 and at most {MAX_TIMEOUT:g}")
    return CustodySettings(url=str(value["url"]), token_file=value.get("token_file") or None,
                           token_env=value.get("token_env") or None,
                           token_credential=value.get("token_credential") or None, host_header=host or None,
                           ca_file=value.get("ca_file") or None, provider=value.get("provider") or None,
                           models=tuple(sorted((models or {}).items())),
                           timeout=float(timeout) if timeout is not None else DEFAULT_TIMEOUT)


def settings_of(provider: Any) -> CustodySettings | None:
    found = getattr(provider, "custody", None)
    return found if isinstance(found, CustodySettings) else None


def needs_lease(provider: Any) -> bool:
    """Whether a call to provider must hold a request lease: custody is configured and the server may load or
    swap a model on a request (lazy_load, or allow_model_swap)."""
    return settings_of(provider) is not None and bool(getattr(provider, "lazy_load", False)
                                                      or getattr(provider, "allow_model_swap", False))


def register_names(provider: Any, model_id: str) -> tuple[str, str]:
    """(the register's name for provider's server, for model_id on it)."""
    settings = settings_of(provider)
    if settings is None:
        return str(getattr(provider, "name", "")), model_id
    return settings.provider or str(provider.name), dict(settings.models).get(model_id, model_id)


def lease_identity(provider: Any, model_id: str) -> tuple | None:
    """What one lease is for, however many provider entries reach the server: (the register, the register's
    name for the server, for the model). None without custody."""
    settings = settings_of(provider)
    if settings is None:
        return None
    return (settings.key(), *register_names(provider, model_id))


_LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})


def server_of(base_url: Any) -> str:
    """One server however its provider entries spell it: the scheme, the host (localhost, 127.0.0.1 and ::1 as
    one), the port (the default one written out) and the path without a trailing slash or /v1, so an
    OpenAI-style ``.../v1`` entry and an Anthropic-style entry on the same server are one server."""
    try:
        parts = urllib.parse.urlsplit(str(base_url or "").strip())
        port = parts.port
    except ValueError:
        return str(base_url or "").strip().rstrip("/")
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    host = "loopback" if host in _LOOPBACK_NAMES else host
    port = port or {"http": 80, "https": 443}.get(scheme)
    path = parts.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3].rstrip("/")
    return f"{scheme}://{host}:{port}{path}"


def custody_on_server(entries: Any, base_url: Any, skip: str | None = None
                      ) -> tuple[str, CustodySettings, bool] | None:
    """The provider entry (the first by name, other than skip) on base_url's server that names a custody
    register: (its name, its custody with the register's name for the server written out, whether a request
    there may load a model: its lazy_load or allow_model_swap). Custody follows the server: an entry without
    custody of its own on that server, or an older job's snapshot of it, takes this one (DESIGN §37)."""
    if not isinstance(entries, dict):
        return None
    server = server_of(base_url)
    for name in sorted(str(key) for key in entries):
        entry = entries.get(name)
        if name == skip or not isinstance(entry, dict) or entry.get("custody") is None \
                or server_of(entry.get("base_url")) != server:
            continue
        try:
            settings = parse_settings(entry["custody"], f"providers.{name}.custody")
        except ValueError:
            continue
        if settings is None:
            continue
        loads = entry.get("lazy_load") is True or entry.get("allow_model_swap") is True
        return name, dataclasses.replace(settings, provider=settings.provider or name), loads
    return None


# ---------------------------------------------------------------- the register's answers


class RegisterError(Exception):
    """No usable answer from the register. code: UNREACHABLE, TIMEOUT, ERROR or OUTCOME_UNKNOWN; sent: whether
    the request may have reached it (True: its outcome is unknown and it is never replayed)."""

    def __init__(self, code: str, detail: str, sent: bool = False) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.sent = sent


class CustodyDenied(Exception):
    """No lease: the register refused (a reason code) or could not be asked or believed (NO_ANSWER_CODES)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class StaleAnswer(Exception):
    """An answer older than one already seen (generation fencing)."""


# A GPU's UUID as nvidia-smi gives it ("GPU-" and a UUID). A card's index (0, 1) is not an identity: the order can
# change between boots (it did on the owner's server between two captures), so per-card fields are kept by UUID.
GPU_UUID_RE = re.compile(r"^GPU-[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}$")


@dataclass(frozen=True)
class Resident:
    provider: str
    model: str
    vram_mib: int | None = None
    since: str | None = None
    gpus: tuple[str, ...] = ()      # the cards it sits on, by UUID only (optional in the contract)
    cards_by_index: bool = False    # the register named a card by its index, which was left out


@dataclass
class Residency:
    """What the register says is loaded: the resident models, the register's generation, when it observed them
    and when the harness read it (read_at, its own clock)."""

    resident: list[Resident]
    generation: int
    observed_at: str | None = None
    read_at: float = 0.0
    epoch: str | None = None

    def on(self, provider: str) -> list[tuple[str, str]]:
        """The (provider, model) pairs resident on one server, sorted: what exact restoration compares."""
        return sorted((item.provider, item.model) for item in self.resident if item.provider == provider)

    def view(self) -> dict:
        return {"generation": self.generation, "epoch": self.epoch, "observed_at": self.observed_at,
                "read_at": _iso(self.read_at),
                "resident": [_resident_view(item) for item in self.resident]}


def _resident_view(item: Resident) -> dict:
    """One resident model as the API shows it; its cards (by UUID) and a note on cards named by index only when the
    register sent them."""
    view: dict[str, Any] = {"provider": item.provider, "model": item.model, "vram_mib": item.vram_mib,
                            "since": item.since}
    if item.gpus:
        view["gpus"] = list(item.gpus)
    if item.cards_by_index:
        view["cards_note"] = ("the register named a card by its number, which can change between boots: ask it for "
                              "the card's UUID (GPU-...)")
    return view


@dataclass
class Answer:
    """A lease answer: granted or not, the lease id, its holder, until (epoch seconds and as sent), the
    register's generation, reason code and epoch (optional: the register instance; see CustodyClient)."""

    granted: bool
    lease_id: str | None
    holder: str | None
    until: float | None
    until_text: str | None
    generation: int
    reason_code: str
    epoch: str | None = None


def parse_residency(payload: Any) -> Residency:
    """The residency answer, or RegisterError(ERROR) when it is not the contract's shape."""
    if not isinstance(payload, dict):
        raise RegisterError("ERROR", "the residency answer is not a JSON object")
    generation = _generation(payload.get("generation"), "residency")
    items = payload.get("resident")
    if not isinstance(items, list):
        raise RegisterError("ERROR", "the residency answer has no resident list")
    resident = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("provider"), str) \
                or not isinstance(item.get("model"), str):
            raise RegisterError("ERROR", "a resident entry needs a provider and a model")
        vram = item.get("vram_mib")
        gpus, by_index = _cards(item.get("gpus"))
        resident.append(Resident(provider=item["provider"], model=item["model"],
                                 vram_mib=int(vram) if isinstance(vram, int) and not isinstance(vram, bool) else None,
                                 since=str(item["since"]) if isinstance(item.get("since"), str) else None,
                                 gpus=gpus, cards_by_index=by_index))
    observed = payload.get("observed_at")
    return Residency(resident=resident, generation=generation,
                     observed_at=str(observed) if isinstance(observed, str) else None,
                     epoch=_epoch(payload.get("epoch"), "residency"))


def _cards(value: Any) -> tuple[tuple[str, ...], bool]:
    """(the GPU UUIDs a resident entry names, sorted and each once; whether it named any card another way). A card
    by index (0, "1") or anything else is left out: the index order can change between boots, so it never says
    which card."""
    if value is None:
        return (), False
    entries = value if isinstance(value, list) else [value]
    uuids = sorted({entry for entry in entries if isinstance(entry, str) and GPU_UUID_RE.fullmatch(entry)})
    return tuple(uuids), len(uuids) != len(entries)


def parse_answer(payload: Any) -> Answer:
    """A lease answer; RegisterError(OUTCOME_UNKNOWN) when it cannot be read (a grant may hide in it)."""
    try:
        if not isinstance(payload, dict) or not isinstance(payload.get("granted"), bool):
            raise ValueError("it has no granted true or false")
        generation = _generation(payload.get("generation"), "lease")
        lease_id = payload.get("lease_id")
        if lease_id is not None and (not isinstance(lease_id, str) or not lease_id or len(lease_id) > MAX_ID_CHARS):
            raise ValueError("its lease_id is not a short string")
        until_text, until = _until(payload.get("until"))
        granted = payload["granted"]
        if granted and (lease_id is None or until is None):
            raise ValueError("a grant without a lease_id and an until")
        code = str(payload.get("reason_code") or ("OK" if granted else "UNKNOWN")).upper()
        holder = payload.get("holder")
        return Answer(granted=granted, lease_id=lease_id, holder=str(holder)[:MAX_ID_CHARS] if holder else None,
                      until=until, until_text=until_text, generation=generation,
                      reason_code=code if code in REASON_CODES else "UNKNOWN",
                      epoch=_epoch(payload.get("epoch"), "lease"))
    except RegisterError as exc:
        raise RegisterError("OUTCOME_UNKNOWN", f"the register's answer could not be read: {exc.detail}", True) from None
    except (ValueError, OverflowError, OSError) as exc:   # a grant may hide in it: never taken as a refusal
        raise RegisterError("OUTCOME_UNKNOWN", f"the register's answer could not be read: {exc}", True) from None


def _generation(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RegisterError("ERROR", f"the {what} answer has no generation (a non-negative integer)")
    return value


def _epoch(value: Any, what: str) -> str | None:
    """The register instance an answer names (optional): a short string or number, else None."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip() \
            or len(str(value)) > MAX_ID_CHARS:
        raise RegisterError("ERROR", f"the {what} answer's epoch is not a short string")
    return str(value).strip()


def _until(value: Any) -> tuple[str | None, float | None]:
    """until as sent and as epoch seconds: an ISO-8601 time (a zone or Z; none reads as UTC) or epoch seconds,
    from 1970 to 9999 (anything else, NaN and infinities too, is not a time: ValueError)."""
    if value is None:
        return None, None
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            seconds = float(value)
            if not math.isfinite(seconds) or not 0 <= seconds <= MAX_UNTIL:
                raise ValueError("out of range")
            return _iso(seconds), seconds
        if isinstance(value, str):
            when = datetime.fromisoformat(value.strip().replace("Z", "+00:00").replace("z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            seconds = when.timestamp()
            if not 0 <= seconds <= MAX_UNTIL:
                raise ValueError("out of range")
            return value, seconds
    except (ValueError, OverflowError, OSError):
        raise ValueError(f"its until {str(value)[:40]!r} is not a time") from None
    raise ValueError("its until is not a time")


def _epoch_change(old: str | None, new: str | None) -> str:
    """A register restart in words: "epoch A became B", or "it now sends epoch B" after answers without one."""
    return f"epoch {old} became {new}" if old is not None else f"it now sends epoch {new}"


def _iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class RegisterAdapter:
    """The register's API as PROPOSED (pending the operator's confirmation): its paths and JSON shapes and
    nothing else. When the contract is confirmed or changed, this class changes and nothing else does."""

    def __init__(self, settings: CustodySettings,
                 send: Callable[..., plainhttp.Reply] = plainhttp.request) -> None:
        self.settings = settings
        self.send = send

    def residency(self) -> Residency:
        return parse_residency(self._call("GET", "residency"))

    def acquire(self, provider: str, model: str, purpose: str, holder: str) -> Answer:
        return parse_answer(self._call("POST", "leases", {"provider": provider, "model": model, "purpose": purpose,
                                                          "holder": holder}, answer=True))

    def renew(self, lease_id: str) -> Answer:
        return parse_answer(self._call("POST", f"leases/{urllib.parse.quote(lease_id, safe='')}/renew", {},
                                       answer=True))

    def release(self, lease_id: str) -> Answer:
        return parse_answer(self._call("POST", f"leases/{urllib.parse.quote(lease_id, safe='')}/release", {},
                                       answer=True))

    def _call(self, method: str, path: str, body: Any = None, answer: bool = False) -> dict:
        settings = self.settings
        headers = {"Accept": "application/json"}
        token = settings.token()   # read again for every request, so a rotated token is used at once
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if settings.host_header:
            headers["Host"] = settings.host_header
        try:
            trust = {"ca_file": settings.ca_file} if settings.ca_file else {}
            reply = self.send(method, settings.url.rstrip("/") + "/" + path, headers=headers, body=body,
                              timeout=settings.timeout, max_bytes=MAX_ANSWER_BYTES, **trust)
        except plainhttp.HttpFailure as exc:
            detail = exc.detail.replace(token, "***") if token else exc.detail
            if exc.sent:
                raise RegisterError("OUTCOME_UNKNOWN", detail, True) from None
            raise RegisterError("TIMEOUT" if exc.kind == "timeout" else "UNREACHABLE" if exc.kind == "unreachable"
                                else "ERROR", detail) from None
        payload = reply.json()
        if 200 <= reply.status < 300:
            if not isinstance(payload, dict):
                raise RegisterError("OUTCOME_UNKNOWN" if answer else "ERROR",
                                    f"{method} /{path} answered HTTP {reply.status} without a JSON object", answer)
            return payload
        if answer and isinstance(payload, dict) and payload.get("granted") is False:
            return payload   # a refusal with an error status (409 LEASED, 423 HELD, 503 MAINTENANCE)
        if answer and rule.of_status(reply.status) == rule.UNKNOWN:
            # 500 or more: it failed while handling the request, and a grant may hide in it; 1xx or 3xx (plainhttp
            # follows no redirect): nothing confirmed or refused it. Either way the outcome is unknown (§40)
            raise RegisterError("OUTCOME_UNKNOWN", f"{method} /{path} answered HTTP {reply.status} without an "
                                                   f"answer this client can read", True)
        raise RegisterError("ERROR", f"{method} /{path} answered HTTP {reply.status}")


# ---------------------------------------------------------------- leases


@dataclass(frozen=True)
class Refusal:
    """What a build's provider carries instead of a lease when the register refused one, or (lapsed) when the
    lease taken at placement was no longer valid when the run started (Provider.complete then refuses the call
    with why)."""

    why: str
    lapsed: bool = False


class Lease:
    """One request lease this process holds: shared by the runs that want the same model on the same server
    (users), renewed by a keeper thread (keep()), released by the last user (CustodyClient.release)."""

    def __init__(self, client: "CustodyClient", answer: Answer, *, provider: str, model: str, purpose: str,
                 holder: str, harness_provider: str, model_id: str, before: Residency | None) -> None:
        self.client = client
        self.lease_id = str(answer.lease_id)
        self.provider = provider              # the register's names
        self.model = model
        self.harness_provider = harness_provider
        self.model_id = model_id              # the harness's model id
        self.purpose = purpose
        self.holder = answer.holder or holder
        self.until = float(answer.until or 0.0)
        self.generation = answer.generation
        self.epoch = client.epoch             # the register's epoch it was granted in (None: the register sends none)
        self.acquired_at = client.now()
        self.state = "held"
        self.error: str | None = None
        self.before = before                  # the residency read just before the lease was asked for
        self.after: Residency | None = None   # the residency read after the release
        self.users = 1
        self._stop = threading.Event()
        self._keeper: threading.Thread | None = None

    def __copy__(self) -> "Lease":   # a provider config copied for one call still carries this same lease
        return self

    def __deepcopy__(self, memo: dict) -> "Lease":
        return self

    def problem(self) -> str | None:
        """Why no call may go out on this lease now, or None."""
        if self.state == "held":
            if self.until <= self.client.now():
                return "the lease expired"
            return None
        if self.state == "unknown":
            return (f"the lease's state is unknown ({self.error}); residency was read again and nothing was "
                    f"replayed")
        if self.state == "lost":
            return f"the lease was lost ({self.error})"
        return "the lease was released"

    def keep(self) -> None:
        """Renew the lease in the background, at half its remaining time, until it is released or lost."""
        if self._keeper is None and self.state == "held":
            self._keeper = threading.Thread(target=self._keep, name=f"custody-{self.lease_id[:16]}", daemon=True)
            self._keeper.start()

    def _keep(self) -> None:
        while self.state == "held":
            left = self.until - self.client.now()
            if left <= 0:
                return
            if self._stop.wait(min(max(MIN_RENEW_SECONDS, left / 2), MAX_RENEW_WAIT)):
                return
            if self.state != "held":
                return
            if self.until - self.client.now() > 2 * MAX_RENEW_WAIT:
                continue   # far from its until: look again later, renew nearer the time
            with contextlib.suppress(Exception):
                self.client.renew(self)

    @property
    def identity(self) -> tuple:
        """What this lease is for (lease_identity): its register, and the register's names for server and model."""
        return (self.client.settings.key(), self.provider, self.model)

    def view(self) -> dict:
        return {"lease_id": self.lease_id, "provider": self.harness_provider, "model": self.model_id,
                "register_provider": self.provider, "register_model": self.model, "purpose": self.purpose,
                "holder": self.holder, "until": _iso(self.until), "generation": self.generation, "epoch": self.epoch,
                "state": self.state, "error": self.error, "users": self.users,
                "resident_before": [list(pair) for pair in self.before.on(self.provider)] if self.before else None,
                "resident_after": [list(pair) for pair in self.after.on(self.provider)] if self.after else None}


# ---------------------------------------------------------------- the client


class CustodyClient:
    """One register as this process sees it: the highest generation seen (fencing), the last residency read,
    the leases held and a short history. Calls go one at a time (a lock), so answers arrive in order.

    The register's generation must only grow within one epoch (the PROPOSED contract). The epoch names the
    register's instance, sent with every answer: the register derives it from the host's boot id
    (/proc/sys/kernel/random/boot_id) plus a counter it persists and moves on at every start, so a reboot or a
    restart of the register gives a new epoch; the client never parses it. The first epoch seen is taken as it
    is. A changed epoch (or the first one from a register whose earlier answers had none) restarts the fence: the
    generation may start again from 0, and every lease of the earlier epoch is lost (the register no longer
    knows it: its calls are refused, and no renew or release is sent for it, since the new instance may have
    given its id to another holder). A renew or a release of a lease granted under an epoch reads residency
    first, so a restart not seen yet is seen before the request would go out. Within one epoch, or without an
    epoch, a generation that went back is refused as stale until it passes the highest one seen, and the client
    says so (fenced, in view() and the doctor)."""

    def __init__(self, settings: CustodySettings, adapter: RegisterAdapter | None = None,
                 now: Callable[[], float] = time.time) -> None:
        self.settings = settings
        self.adapter = adapter or RegisterAdapter(settings)
        self.now = now
        self.generation: int | None = None
        self.epoch: str | None = None
        self.fenced: dict | None = None   # {seen, answered, since}: answers refused because the generation went back
        self.residency: Residency | None = None
        self.leases: dict[tuple[str, str], Lease] = {}   # (register provider, register model) -> the shared lease
        self.history: deque[dict] = deque(maxlen=HISTORY_KEPT)
        self._lock = threading.RLock()

    # -- reading -----------------------------------------------------------------------

    def observe(self) -> Residency:
        """Residency, read now (RegisterError without an answer, StaleAnswer for one older than seen)."""
        with self._lock:
            found = self.adapter.residency()
            found.read_at = self.now()
            stale = self._stale(found.generation, epoch=found.epoch)
            if stale:
                self._note("residency", outcome="stale", reason=stale)
                raise StaleAnswer(stale)
            self._seen(found.generation)
            self.residency = found
            return found

    def reread(self) -> Residency | None:
        """observe(), or None when the register cannot be read or believed (noted in the history)."""
        try:
            return self.observe()
        except (RegisterError, StaleAnswer) as exc:
            self._note("residency", outcome="unread", reason=str(exc))
            return None

    def view(self) -> dict:
        with self._lock:
            return {"url": self.settings.url, "generation": self.generation, "epoch": self.epoch,
                    "fenced": dict(self.fenced) if self.fenced else None,
                    "residency": self.residency.view() if self.residency else None,
                    "leases": [lease.view() for lease in self.leases.values()], "history": list(self.history)}

    # -- leases --------------------------------------------------------------------------

    def claim(self, provider: str, model: str, purpose: str, holder: str = HOLDER, *,
              harness_provider: str | None = None, model_id: str | None = None, quiet: bool = False) -> Lease:
        """A request lease for model on provider (the register's names): residency first, then the lease.
        A run that wants a model this process already holds a valid lease for shares it. CustodyDenied when
        there is none: the register's reason code, or UNREACHABLE, TIMEOUT, ERROR, STALE or OUTCOME_UNKNOWN.
        quiet: a placement asking again while it waits out a HELD answer; another HELD is then not noted (the
        wait is, when it starts and when it ends), and the journal answers its request line quietly."""
        with self._lock:
            held = self.leases.get((provider, model))
            if held is not None and held.problem() is None:
                held.users += 1
                return held
            try:
                before = self.observe()
            except RegisterError as exc:
                self._note("claim", provider=provider, model=model, purpose=purpose, outcome="denied", reason=exc.code)
                raise CustodyDenied("ERROR" if exc.code == "OUTCOME_UNKNOWN" else exc.code,
                                    f"residency could not be read first ({exc.detail})") from None
            except StaleAnswer as exc:
                raise CustodyDenied("STALE", f"the residency answer is stale ({exc})") from None
            self._intend("claim", provider=provider, model=model, purpose=purpose, holder=holder)
            try:
                answer = self.adapter.acquire(provider, model, purpose, holder)
            except RegisterError as exc:
                if not exc.sent:
                    self._note("claim", provider=provider, model=model, purpose=purpose, outcome="denied",
                               reason=exc.code)
                    raise CustodyDenied(exc.code, exc.detail) from None
                # outcome unknown: the register may have granted it. Read residency again, never replay the
                # request; a lease granted unseen expires at the register.
                seen = self.reread()
                self._note("claim", provider=provider, model=model, purpose=purpose, outcome="unknown",
                           reason=exc.detail, generation=seen.generation if seen else None)
                raise CustodyDenied("OUTCOME_UNKNOWN", f"{exc.detail}; residency was read again"
                                    f"{'' if seen else ' (it could not be)'} and the request was not replayed") from None
            stale = self._stale(answer.generation, answer.until if answer.granted else None, answer.epoch)
            if stale:
                if answer.granted and answer.lease_id:
                    self._release_once(answer.lease_id)
                self._note("claim", provider=provider, model=model, purpose=purpose, outcome="stale", reason=stale)
                raise CustodyDenied("STALE", f"the register's answer is stale ({stale})")
            self._seen(answer.generation)
            if not answer.granted:
                if quiet and answer.reason_code == "HELD":   # its request line still gets its answer (§40)
                    self._answer_quietly("claim", provider=provider, model=model, purpose=purpose,
                                         outcome="denied", reason=answer.reason_code, generation=answer.generation)
                else:
                    self._note("claim", provider=provider, model=model, purpose=purpose, outcome="denied",
                               reason=answer.reason_code, words=refusal_words(answer.reason_code, model),
                               holder=answer.holder, generation=answer.generation)
                raise CustodyDenied(answer.reason_code, _refusal_words(answer, provider, model))
            lease = Lease(self, answer, provider=provider, model=model, purpose=purpose, holder=holder,
                          harness_provider=harness_provider or provider, model_id=model_id or model, before=before)
            self.leases[(provider, model)] = lease
            self._note("claim", provider=provider, model=model, purpose=purpose, outcome="granted",
                       lease_id=lease.lease_id, generation=answer.generation, until=answer.until_text)
            return lease

    def renew(self, lease: Lease) -> bool:
        """Renew lease once (the keeper calls it). False when it was not renewed: refused or stale (the lease is
        lost), granted under an earlier epoch of the register (lost; nothing sent, _granted_earlier), outcome
        unknown (unknown; residency read again, the request never replayed), or the register could not be
        reached (the lease stays held until its until; the next renewal is a new request)."""
        with self._lock:
            if lease.state != "held":
                return False
            if self._granted_earlier(lease)[0]:
                self._note("renew", lease_id=lease.lease_id, purpose=lease.purpose, outcome="lost",
                           reason=f"not sent: {lease.error}")
                return False
            try:
                answer = self.adapter.renew(lease.lease_id)
            except RegisterError as exc:
                if exc.sent:
                    lease.state, lease.error = "unknown", f"renew: {exc.detail}"
                    self.reread()
                else:
                    lease.error = f"renew: {exc.detail}"
                self._note("renew", lease_id=lease.lease_id, purpose=lease.purpose, outcome=lease.state,
                           reason=exc.code)
                return False
            stale = self._stale(answer.generation, answer.until if answer.granted else None, answer.epoch)
            if lease.state != "held":   # the answer came from a new epoch of the register: the lease is gone
                return False
            if stale or not answer.granted:
                lease.state = "lost"
                lease.error = f"renew: stale answer ({stale})" if stale else f"renew refused: {answer.reason_code}"
                self._note("renew", lease_id=lease.lease_id, purpose=lease.purpose, outcome="lost",
                           reason=lease.error)
                return False
            self._seen(answer.generation)
            lease.until, lease.generation, lease.error = float(answer.until or lease.until), answer.generation, None
            return True

    def release(self, lease: Lease, why: str = "") -> dict:
        """One user of lease is done; the last one releases it at the register (once, never replayed). A
        release is never evidence that VRAM is free: residency is read again afterwards and kept in
        lease.after. A lease granted under an earlier epoch of the register is not released at all: it stays
        lost, with its error, and only residency is read (_granted_earlier). Returns lease.view()."""
        with self._lock:
            if lease.users > 1:
                lease.users -= 1
                return lease.view()
            lease.users = 0
            lease._stop.set()
            if self.leases.get((lease.provider, lease.model)) is lease:
                del self.leases[(lease.provider, lease.model)]
            if lease.state in ("released", "unreleased"):
                return lease.view()
            earlier, seen = self._granted_earlier(lease)
            if earlier:   # never sent to the register's new instance: it does not know the lease
                lease.after = seen if seen is not None else self.reread()
                self._note("release", lease_id=lease.lease_id, provider=lease.provider, model=lease.model,
                           purpose=lease.purpose, outcome="lost",
                           reason=f"not sent: {lease.error}" + (f" ({why})" if why else ""),
                           resident_after=[list(pair) for pair in lease.after.on(lease.provider)]
                           if lease.after else None)
                return lease.view()
            self._intend("release", lease_id=lease.lease_id, provider=lease.provider, model=lease.model,
                         purpose=lease.purpose)
            try:
                answer = self.adapter.release(lease.lease_id)
            except RegisterError as exc:
                lease.state = "unknown" if exc.sent else "unreleased"
                lease.error = f"release: {exc.detail}" + ("" if exc.sent else "; the register keeps it until it expires")
            else:
                stale = self._stale(answer.generation, epoch=answer.epoch)
                if stale:   # an answer that cannot be believed: not taken as a release, and not replayed
                    lease.state, lease.error = "unknown", f"release: the answer is stale ({stale})"
                else:
                    self._seen(answer.generation)
                    lease.state = "released"
                    if answer.reason_code == "UNKNOWN":
                        lease.error = "the register no longer knew this lease (it may have expired)"
            lease.after = self.reread()
            self._note("release", lease_id=lease.lease_id, provider=lease.provider, model=lease.model,
                       purpose=lease.purpose, outcome=lease.state, reason=why or None,
                       resident_after=[list(pair) for pair in lease.after.on(lease.provider)] if lease.after else None)
            return lease.view()

    def connection_lost(self, lease: Lease, detail: str) -> str:
        """The model server's connection was lost during a call on lease: outcome unknown. Residency is read
        again (and noted); nothing is assumed released and the call is not replayed. Returns a short note."""
        with self._lock:
            seen = self.reread()
            self._note("call", lease_id=lease.lease_id, purpose=lease.purpose, outcome="unknown", reason=detail[:200],
                       resident=[list(pair) for pair in seen.on(lease.provider)] if seen else None)
        if seen is None:
            return "residency could not be read again"
        pairs = seen.on(lease.provider)
        return (f"residency read again (generation {seen.generation}): "
                + (", ".join(model for _, model in pairs) or "nothing resident") + f" on {lease.provider}")

    # -- internals ---------------------------------------------------------------------------

    def _stale(self, generation: int, until: float | None = None, epoch: str | None = None) -> str | None:
        """Why an answer cannot be believed (generation fencing, a passed until), or None. An answer from a new
        epoch of the register (another epoch than the one seen, or the first one after answers without an
        epoch) restarts the fence first (_new_epoch); the very first answer's epoch is taken as it is."""
        if epoch is not None and epoch != self.epoch:
            if self.epoch is not None or self.generation is not None:
                self._new_epoch(epoch)
            self.epoch = epoch
        if self.generation is not None and generation < self.generation:
            if self.fenced is None:
                self.fenced = {"seen": self.generation, "answered": generation, "since": _iso(self.now())}
            else:
                self.fenced["answered"] = generation
            return f"generation {generation} is older than {self.generation}, the last one seen"
        if until is not None and until <= self.now():
            return "its until has passed"
        return None

    def _seen(self, generation: int) -> None:
        self.fenced = None
        if self.generation is None or generation > self.generation:
            self.generation = generation

    def _new_epoch(self, epoch: str) -> None:
        """The register answers as a new instance: its generation may start over, and it no longer knows the
        leases of the earlier one (they are lost: their calls are refused, nothing is released, renewed or
        replayed)."""
        change = _epoch_change(self.epoch, epoch)
        self.generation = None
        self.fenced = None
        for lease in self.leases.values():
            if lease.state in ("held", "unknown"):
                lease.state, lease.error = "lost", f"the register restarted ({change})"
                lease._stop.set()
        self._note("epoch", outcome="restarted", reason=change)

    def _granted_earlier(self, lease: Lease) -> tuple[bool, Residency | None]:
        """(whether lease was granted under an earlier epoch of the register, the residency read to tell, or
        None). Before a renew or a release of a lease granted under an epoch, residency is read, so a restart
        this client has not seen yet is seen first: the register's new instance no longer knows the lease, and
        the contract does not keep lease ids apart across epochs, so the call could reach another holder's lease.
        Such a lease is lost (its error says the register restarted), and nothing is sent for it."""
        seen = self.reread() if lease.epoch is not None and lease.epoch == self.epoch else None
        if lease.epoch == self.epoch:
            return False, seen
        if lease.state != "lost":
            lease.state, lease.error = "lost", f"the register restarted ({_epoch_change(lease.epoch, self.epoch)})"
        lease._stop.set()
        return True, seen

    def _release_once(self, lease_id: str) -> None:
        with contextlib.suppress(RegisterError):
            self.adapter.release(lease_id)

    def _note(self, op: str, **fields: Any) -> None:
        entry = {"at": _iso(self.now()), "op": op, **{k: v for k, v in fields.items() if v is not None}}
        self.history.append(entry)
        _journal({**entry, "register": self.settings.url})

    def _intend(self, op: str, **fields: Any) -> None:
        """Journal a lease request before it is sent (outcome "requested"; the journal only, not the history): the
        intent comes first, so a crash while the request is out leaves a request with no answer after it, which
        reads as outcome unknown (DESIGN §40). Its answer is noted on the next line (calls go one at a time)."""
        _journal({"at": _iso(self.now()), "op": op, "outcome": "requested", "register": self.settings.url,
                  **{k: v for k, v in fields.items() if v is not None}})

    def _answer_quietly(self, op: str, **fields: Any) -> None:
        """Journal the answer to a request that is not noted (the journal only, with quiet: true): a HELD asked
        again while a HELD wait runs. The wait is noted when it starts and when it ends; this line only answers
        the request's "requested" line, so the activity query never reads that request as outcome unknown, and
        it lists no entry for it."""
        _journal({"at": _iso(self.now()), "op": op, "register": self.settings.url,
                  **{k: v for k, v in fields.items() if v is not None}, "quiet": True})


_JOURNAL: dict[str, Any] = {"dir": None}
_JOURNAL_LOCK = threading.Lock()


def set_journal(data_dir: Any) -> None:
    """Append what this process's clients note (their history: claims, releases, failed renewals and residency
    reads, a register's restart) to <data_dir>/custody/journal-YYYY-MM.jsonl as well, a durable record the
    operator's activity query reads (DESIGN §36, §37); None: no journal. The Factory names its data dir. The record
    is never read back as state (leases live in this process only, rule 7)."""
    with _JOURNAL_LOCK:
        _JOURNAL["dir"] = Path(data_dir) / JOURNAL_DIR if data_dir is not None else None


def _journal(entry: dict) -> None:
    """One noted entry as a journal line (0600, in a 0700 folder made inside the data dir once it exists; the
    journal never makes the data dir itself, so one that is gone stays gone). A record that cannot be written is
    skipped: the journal never stops a claim or a release."""
    with _JOURNAL_LOCK:
        folder = _JOURNAL["dir"]
        if folder is None:
            return
        line = {key: (value[:JOURNAL_REASON_CHARS] if isinstance(value, str) else value)
                for key, value in entry.items()}
        stamp = str(line.get("at") or "")[:7] or datetime.now(timezone.utc).strftime("%Y-%m")
        try:
            if not folder.is_dir():
                if not folder.parent.is_dir():
                    return
                folder.mkdir(mode=0o700)
            fd = os.open(folder / f"journal-{stamp}.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n")
        except (OSError, TypeError, ValueError):
            return


def refusal_words(code: Any, model: str | None = None) -> str:
    """A reason code (or one of the harness's own NO_ANSWER_CODES) in plain words; NOT_RESIDENT names model."""
    if code == "NOT_RESIDENT" and model:
        return f"{model} is not resident and may not be loaded now"
    return REFUSAL_WORDS.get(str(code), "refused")


def _refusal_words(answer: Answer, provider: str, model: str) -> str:
    holder = f" (held by {answer.holder})" if answer.holder else ""
    return f"no lease for {model} on {provider}: {refusal_words(answer.reason_code, model)}{holder}"


_CLIENTS: dict[tuple, CustodyClient] = {}
_CLIENTS_LOCK = threading.Lock()


def client_for(settings: CustodySettings) -> CustodyClient:
    """The process-wide client of settings' register (one per url, token and Host header)."""
    with _CLIENTS_LOCK:
        client = _CLIENTS.get(settings.key())
        if client is None:
            client = _CLIENTS[settings.key()] = CustodyClient(settings)
        return client


def known_clients() -> list[CustodyClient]:
    with _CLIENTS_LOCK:
        return list(_CLIENTS.values())


# ---------------------------------------------------------------- what callers use


def lease_problem(provider: Any, model_id: str) -> str | None:
    """Why a call to provider for model_id may not go out (it needs a lease and carries no valid one for that
    model), or None. Provider.complete asks before every attempt: nothing bypasses custody."""
    if not needs_lease(provider):
        return None
    token = getattr(provider, "lease", None)
    if isinstance(token, Refusal):
        if token.lapsed:
            return f"the lease taken for {provider.name}/{model_id} at placement is no longer valid: {token.why}"
        return f"the custody register gave no lease for {provider.name}/{model_id}: {token.why}"
    if not isinstance(token, Lease):
        return (f"{provider.name} is under GPU custody and this call holds no lease (a call that could load or "
                f"swap a model never goes out without one)")
    if lease_identity(provider, model_id) != token.identity:   # the same server and model, however it is reached
        name, model = register_names(provider, model_id)
        return f"the lease is for {token.provider}/{token.model}, not {name}/{model}"
    return token.problem()


def lease_of(provider: Any) -> Lease | None:
    token = getattr(provider, "lease", None)
    return token if isinstance(token, Lease) else None


class Claims:
    """The custody side of one run's placement. routing.choose_build calls it for each build it would use: a
    build whose provider needs a lease is usable only once the register grants one, so a denial passes the
    build over (and the run follows the overflow path). attach() puts the lease of the build the run goes to
    (or the register's refusal) on the placed model's provider and gives back any other one it took (a run
    that waited for its slot and then overflowed); release() gives everything back when the run ends.

    The two refusals the operator told apart (her decision): HELD (someone else holds the card) waits, FULL (the
    card cannot fit the request, whoever holds it) does not. A placement that can wait passes its cancel event
    (a job's run, repo-scout, a chat turn, the lead): a HELD answer then waits for at most the provider's
    overflow_after_seconds, asking again every HELD_RETRY_SECONDS (on_wait(provider name, seconds, the refusal)
    is called once when the wait starts), and the build counts as unavailable only after that. The builds are
    asked in order, so a HELD first build is waited for before a later build is asked, even one that is free.
    One placement has one wait (wait_until): the first HELD answer starts it, a later HELD answer (another build,
    the overflow) waits only for what is left of it, and none waits after stop_waiting (a slot wait that ran
    out). Without a cancel event (a wake turn, a Test probe, a swap-back), or a provider without
    overflow_after_seconds, nothing waits. FULL, and every other refusal (LEASED too), passes the build over at
    once: the next build (another card, or a smaller build), then the overflow."""

    def __init__(self, purpose: str, holder: str = HOLDER, keep: bool = True, cancel: threading.Event | None = None,
                 on_wait: Callable[[str, float, CustodyDenied], None] | None = None) -> None:
        self.purpose = str(purpose)[:MAX_ID_CHARS]
        self.holder = holder
        self.keep = keep
        self.cancel = cancel
        self.on_wait = on_wait
        self.leases: list[Lease] = []
        self.attached: Lease | None = None
        self.attached_as: tuple[str, str] | None = None   # the (provider, model id) the attached lease is used as
        self.denied: dict[tuple[str, str], CustodyDenied] = {}
        self.released: list[dict] = []
        self.waited = False   # a HELD answer was waited out (a caller checks its cancel event afterwards)
        self.wait_until: float | None = None   # time.monotonic() when this placement's one wait ends
        self.waited_for = ""                   # what used the wait, for a later HELD answer's refusal

    def stop_waiting(self, why: str) -> None:
        """No HELD answer of this placement waits from now on (why: what the run already waited for, said in
        the refusal). The slot wait calls it when it ran out and the run goes on to its overflow."""
        self.wait_until = time.monotonic()
        self.waited_for = why

    def __call__(self, provider: Any, model_id: str) -> tuple[bool, str]:
        settings = settings_of(provider)
        if settings is None or not needs_lease(provider):
            return True, ""
        if self._held(provider, model_id) is not None:   # this placement holds it already (another build there)
            return True, ""
        if self.cancel is not None and self.cancel.is_set():   # a cancelled run asks the register for nothing more
            return False, "the run was cancelled"
        name, model = register_names(provider, model_id)
        client = client_for(settings)
        try:
            lease = client.claim(name, model, self.purpose, self.holder, harness_provider=str(provider.name),
                                 model_id=model_id)
        except CustodyDenied as exc:
            found, denied = self._wait_out(client, provider, model_id, exc) if exc.code == "HELD" else (None, exc)
            if found is None:
                self.denied[(str(provider.name), model_id)] = denied
                return False, str(denied)
            lease = found
        self.denied.pop((str(provider.name), model_id), None)   # refused earlier in this placement, granted now
        self.leases.append(lease)
        if self.keep:
            lease.keep()
        return True, ""

    def _wait_out(self, client: CustodyClient, provider: Any, model_id: str,
                  denied: CustodyDenied) -> tuple[Lease | None, CustodyDenied]:
        """A HELD answer: wait for the hold to end, asking again every HELD_RETRY_SECONDS, for at most provider's
        overflow_after_seconds and what is left of this placement's one wait, when this placement can wait.
        (the lease, denied) when one is granted; else (None, the last refusal): another HELD past the wait, no
        wait left, a cancel, or any other answer, which ends the wait at once (FULL: the card cannot fit the
        model, so waiting cannot help)."""
        seconds = float(getattr(provider, "overflow_after_seconds", None) or 0.0)
        if self.cancel is None or seconds <= 0:
            return None, denied
        started = time.monotonic()
        if self.wait_until is None:   # the first wait of this placement: later HELD answers get what is left
            self.wait_until = started + seconds
            self.waited_for = f"waited out a HELD answer for up to {seconds:g}s"
        else:
            seconds = round(min(seconds, self.wait_until - started), 1)
            if seconds <= 0:
                return None, CustodyDenied("HELD", f"{denied.detail}; no wait left (the run {self.waited_for} "
                                                   f"already: one wait per placement)")
        name, model = register_names(provider, model_id)
        noted = {"provider": name, "model": model, "purpose": self.purpose}
        self.waited = True
        client._note("wait", **noted, outcome="waiting", reason="HELD", seconds=seconds)
        if self.on_wait is not None:
            with contextlib.suppress(Exception):   # a notice that fails never stops the wait
                self.on_wait(str(provider.name), seconds, denied)
        deadline = started + seconds
        while True:
            if self.cancel.wait(max(0.0, min(HELD_RETRY_SECONDS, deadline - time.monotonic()))):
                client._note("wait", **noted, outcome="cancelled", reason="HELD")
                return None, CustodyDenied("HELD", f"{denied.detail}; the run was cancelled while it waited")
            try:
                lease = client.claim(name, model, self.purpose, self.holder, harness_provider=str(provider.name),
                                     model_id=model_id, quiet=True)
            except CustodyDenied as exc:
                if exc.code != "HELD":
                    client._note("wait", **noted, outcome="ended", reason=exc.code)
                    return None, exc
                denied = exc
                if time.monotonic() >= deadline:
                    break
                continue
            client._note("wait", **noted, outcome="granted", reason="HELD",
                         seconds=round(time.monotonic() - started, 1))
            return lease, denied
        client._note("wait", **noted, outcome="gave_up", reason="HELD", seconds=seconds)
        return None, CustodyDenied("HELD", f"{denied.detail}; waited {seconds:g}s (overflow_after_seconds)")

    def common_refusal(self, builds: list[tuple[str, str]]) -> str | None:
        """The register's refusal in plain words when it refused every one of builds ((provider name, model id)
        pairs) with the same reason code (FULL: "the card cannot fit this model now"); else None."""
        codes = set()
        for pair in builds:
            denied = self.denied.get(pair)
            if denied is None or denied.code not in REASON_CODES:
                return None
            codes.add(denied.code)
        return refusal_words(codes.pop()) if len(codes) == 1 else None

    def attach(self, model: Any) -> Any:
        """model whose provider carries the lease taken for it, or the register's refusal when it needs a lease
        and none was granted for it; leases taken for other builds are given back."""
        provider = model.provider
        needs = needs_lease(provider)
        lease = self._held(provider, str(model.model)) if needs else None
        lapsed = self._held(provider, str(model.model), valid=False) if needs and lease is None else None
        why = lapsed.problem() if lapsed is not None else None
        for other in [item for item in self.leases if item is not lease]:
            self._give_back(other, "the run went to another build")
        self.attached = lease
        self.attached_as = (str(provider.name), str(model.model)) if lease is not None else None
        if not needs:
            return model
        if lease is not None:
            token: Any = lease
        elif lapsed is not None:
            token = Refusal(why or "the lease was given back", lapsed=True)
        else:
            denied = self.denied.get((str(provider.name), str(model.model)))
            token = Refusal(str(denied) if denied else "no lease was asked for this build")
        return dataclasses.replace(model, provider=dataclasses.replace(provider, lease=token))

    def info(self) -> dict:
        """What agent.start and job.runs record: custody {lease_id, provider, model, until, generation,
        resident_before} for the attached lease, and custody_refused [{provider, model, code, words}] for every
        build the register gave no lease for while the run was placed (words: the code in plain words, FULL "the
        card cannot fit this model now"); {} for neither."""
        out: dict[str, Any] = {}
        refused = [{"provider": provider, "model": model, "code": denied.code,
                    "words": refusal_words(denied.code, model)} for (provider, model), denied in self.denied.items()]
        lease = self.attached
        if lease is not None:
            view = lease.view()
            if self.attached_as is not None:   # a shared lease is recorded as this run uses it
                view["provider"], view["model"] = self.attached_as
            out["custody"] = {key: view[key] for key in ("lease_id", "provider", "model", "until", "generation",
                                                         "resident_before")}
        if refused:
            out["custody_refused"] = refused
        return out

    def release(self, why: str = "") -> list[dict]:
        """Give back every lease this placement holds (each to its register, once); their views."""
        views = [self._give_back(lease, why) for lease in list(self.leases)]
        self.attached = None
        return views

    def _held(self, provider: Any, model_id: str, valid: bool = True) -> Lease | None:
        """The lease this placement holds for model_id on provider's server (lease_identity: two provider entries
        for one server share it), valid unless valid is False."""
        identity = lease_identity(provider, model_id)
        return next((lease for lease in self.leases if lease.identity == identity
                     and (not valid or lease.problem() is None)), None)

    def _give_back(self, lease: Lease, why: str) -> dict:
        while lease in self.leases:
            self.leases.remove(lease)
        view = lease.client.release(lease, why)
        self.released.append(view)
        return view


@contextlib.contextmanager
def leased(model: Any, purpose: str, holder: str = HOLDER) -> Iterator[Any]:
    """model ready for one call outside a placement (a Test probe, a swap-back, a wake turn): its provider
    carries a lease when it needs one (CustodyDenied when the register refuses), released when the block ends."""
    claims = Claims(purpose, holder)
    ok, _ = claims(model.provider, model.model)
    if not ok:
        raise claims.denied[(str(model.provider.name), str(model.model))]
    try:
        yield claims.attach(model)
    finally:
        claims.release("done")


def residency_matches(before: Residency | None, after: Residency | None, provider: str) -> bool:
    """Exact restoration: the models resident on provider now are the ones resident before."""
    return before is not None and after is not None and before.on(provider) == after.on(provider)
