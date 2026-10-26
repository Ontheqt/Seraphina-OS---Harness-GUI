"""Drift sources (DESIGN.md section 38): outside, read-only drift checks that the Housekeeping Tracker reads.

A drift source is a service on the machine (the operator's checker, for example) that compares what is
deployed with what should be, and reports it over HTTP. The harness only reads it: config
``drift_sources.<name> {url, token_file | token_env | token_credential, host_header, ca_file, schema_versions,
interval, timeout}`` (ca_file: the CA, or the source's own certificate, an https source is checked against instead
of the system's; with host_header set, the certificate must name that host).

The client (fetch) sends a GET only, with exactly the configured Host header, a Bearer token read again for every
check from a file the harness user can read, from the environment, or from a systemd credential of the harness's
unit (token_credential: $CREDENTIALS_DIRECTORY/<name>; never logged), Accept: application/json, and no Origin or
Sec-Fetch-* header (plainhttp). It never follows a redirect. Its answers:

- 200 with a JSON object: an observation, read below;
- 401, 403 and 405: "cannot read" (the token, the source's policy, the method), with a long back-off;
- 429: "check in progress" (the source is busy with a check), backing off (its Retry-After when it sends one);
- anything else, or no answer: "cannot read" or "unreachable", backing off.

A source is never asked twice at once (one check at a time per source).

An observation is ``{schemaVersion, observedAt, validForSeconds, findings?, profiles?}``. A schemaVersion the
source's schema_versions do not list is "cannot read", never "no drift". observedAt plus validForSeconds (above 0,
at most a year) bounds it: past that it is "stale" and nothing is done with it, and an observation no newer than
the last one acted on (to the microsecond) changes nothing, neither the findings nor the role manifest. A
malformed finding makes the whole observation unreadable (a finding that cannot be read must never look like one
that went away). Whatever goes wrong in a check, its state is saved and the next check backs off.

Findings (``findings: [{key, title?, detail?, severity?}]``, PROPOSED with the source's contract) become
system-scope tracker items whose source is ``drift:<name>``: one per stable key (deduplicated), reopened when a
finding that had gone comes back, and closed only with evidence: a fresh, readable observation that no longer
reports a finding proves its item (verified by the factory, ``drift`` evidence naming the observation). Which
findings were present is read from the tracker as well as from the source's state file, so a lost or reset
state never leaves an item open for good. Nothing else closes one: an unreadable, stale, busy or failed check
leaves every item as it is. A person may drop or defer an item; while its finding is still reported it stays
closed, and it reopens only when the finding goes away and comes back.

A check worth a record (its status or detail changed, or it moved an item or wrote the role manifest) is also
appended to ``<data_dir>/drift-sources/<name>/checks-YYYY-MM.jsonl`` (refs and counts, never a finding's title),
which an operator's activity query lists (DESIGN §36).

Profiles (``profiles: [{base_model_id, providerKind, providerModelId, routeKind, versionIdAtSnapshot,
toolPackageCount, filterPackageCount, limitations}]``, an inventory of Open WebUI workspace models) produce a
generated role manifest, ``<data_dir>/drift-sources/<name>/role-manifest.json``: written in the harness data
dir only, read-only, never applied to anything.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import math
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import items as tracker
from . import outcomes as rule
from . import plainhttp

SOURCE_KEYS = ("url", "token_file", "token_env", "token_credential", "host_header", "ca_file", "schema_versions",
               "interval", "timeout")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
DEFAULT_INTERVAL = 3600.0
MIN_INTERVAL = 60.0
MAX_INTERVAL = 7 * 86400.0
DEFAULT_TIMEOUT = 10.0
MAX_TIMEOUT = 60.0
MAX_ANSWER_BYTES = 4 << 20
MAX_FINDINGS = 500
MAX_PROFILES = 1000
KEY_CHARS = 200
CLOCK_SKEW = 300.0          # seconds an observedAt may lie ahead of this machine's clock
MAX_VALID_SECONDS = 366 * 86400.0   # the longest validForSeconds read (a longer one cannot be read)
BUSY_BACKOFF = 30.0         # 429: the first wait without a Retry-After, doubling up to the interval
DOWN_BACKOFF = 60.0         # no answer: the first wait, doubling up to the interval
REFUSED_BACKOFF_MAX = 86400.0   # 401, 403, 405: the interval, doubling up to a day (a person must fix it)
POLL_SECONDS = 5.0
DIR = "drift-sources"
STATES = ("never", "ok", "cannot_read", "stale", "busy", "unreachable", "error")


# ---------------------------------------------------------------- settings


@dataclass(frozen=True)
class SourceSettings:
    name: str
    url: str
    token_file: str | None
    token_env: str | None
    host_header: str | None
    schema_versions: tuple[str, ...]
    interval: float = DEFAULT_INTERVAL
    timeout: float = DEFAULT_TIMEOUT
    ca_file: str | None = None
    token_credential: str | None = None

    def token(self) -> str | None:
        """The Bearer token, read now (plainhttp.read_bearer), or None."""
        return plainhttp.read_bearer(self.token_file, self.token_env, self.token_credential)

    def view(self) -> dict:
        """The settings as the API shows them: where the token is read from (file, env or credential), never the
        token."""
        token_from = "credential" if self.token_credential else "file" if self.token_file else "env"
        return {"name": self.name, "url": self.url, "host_header": self.host_header, "ca_file": self.ca_file,
                "token_from": token_from, "schema_versions": list(self.schema_versions),
                "interval": self.interval, "timeout": self.timeout}


def parse_source(name: Any, value: Any) -> SourceSettings:
    """drift_sources.<name> as SourceSettings; ValueError naming the bad field."""
    label = f"drift_sources.{name}"
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise ValueError(f"drift_sources: invalid name {name!r} (use a-z, 0-9, '-' and '_', at most 32)")
    if not isinstance(value, dict):
        raise ValueError(f'{label} must be an object like {{"url": ..., "token_file": ..., "schema_versions": [...]}}')
    unknown = sorted(str(key) for key in value if key not in SOURCE_KEYS)
    if unknown:
        raise ValueError(f"{label}: unknown keys {', '.join(unknown)} (allowed: {', '.join(SOURCE_KEYS)})")
    problem = plainhttp.check_url(value.get("url"))
    if problem:
        raise ValueError(f"{label}.url: {problem}")
    plainhttp.check_token_source(value, label, required=True)
    host = value.get("host_header")
    if host is not None and (not isinstance(host, str) or not plainhttp.HEADER_VALUE_RE.fullmatch(host)):
        raise ValueError(f"{label}.host_header must be one line of printable ASCII (a host name, with :port if "
                         f"needed), or null")
    ca_file = value.get("ca_file")
    if ca_file is not None and (not isinstance(ca_file, str) or not ca_file.strip()):
        raise ValueError(f"{label}.ca_file must be a file path (a PEM CA or certificate) or null")
    versions = value.get("schema_versions")
    if not isinstance(versions, list) or not versions or not all(
            isinstance(v, (str, int)) and not isinstance(v, bool) and str(v).strip() for v in versions):
        raise ValueError(f"{label}.schema_versions must be a non-empty list of the schema versions it may read")
    interval = value.get("interval", DEFAULT_INTERVAL)
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) \
            or not MIN_INTERVAL <= interval <= MAX_INTERVAL:
        raise ValueError(f"{label}.interval must be a number of seconds from {MIN_INTERVAL:g} to {MAX_INTERVAL:g}")
    timeout = value.get("timeout", DEFAULT_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= MAX_TIMEOUT:
        raise ValueError(f"{label}.timeout must be a number of seconds above 0 and at most {MAX_TIMEOUT:g}")
    return SourceSettings(name=name, url=str(value["url"]), token_file=value.get("token_file") or None,
                          token_env=value.get("token_env") or None,
                          token_credential=value.get("token_credential") or None, host_header=host or None,
                          schema_versions=tuple(str(v).strip() for v in versions),
                          interval=float(interval), timeout=float(timeout), ca_file=ca_file or None)


def parse_sources(raw: dict) -> dict[str, SourceSettings]:
    """Every configured drift source (null entries skipped); ValueError for the first bad one."""
    section = raw.get("drift_sources")
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise ValueError("drift_sources must be an object of names to sources")
    return {str(name): parse_source(name, entry) for name, entry in section.items() if entry is not None}


# ---------------------------------------------------------------- reading a source


@dataclass
class Fetched:
    """One GET: status is ok, cannot_read, busy or unreachable; http the HTTP status; payload the JSON object
    of a 200; retry_after the 429's Retry-After in seconds; detail the plain words."""

    status: str
    http: int | None = None
    payload: dict | None = None
    retry_after: float | None = None
    detail: str = ""


def fetch(settings: SourceSettings, send: Callable[..., plainhttp.Reply] = plainhttp.request) -> Fetched:
    """GET the source once, exactly as the module docstring says."""
    headers = {"Accept": "application/json"}
    token = settings.token()   # read again for every check, so a rotated token is used at once
    if not token:
        return Fetched("cannot_read", detail="the Bearer token could not be read (token_file, token_env or "
                                             "token_credential; factory doctor says why)")
    headers["Authorization"] = f"Bearer {token}"
    if settings.host_header:
        headers["Host"] = settings.host_header
    try:
        trust = {"ca_file": settings.ca_file} if settings.ca_file else {}
        reply = send("GET", settings.url, headers=headers, timeout=settings.timeout, max_bytes=MAX_ANSWER_BYTES,
                     **trust)
    except plainhttp.HttpFailure as exc:
        return Fetched("unreachable", detail=exc.detail.replace(token, "***"))
    words = {401: "the source refused the token (401)", 403: "the source refused the request (403)",
             405: "the source does not allow this method (405)"}
    if reply.status in words:
        return Fetched("cannot_read", reply.status, detail=words[reply.status])
    if reply.status == 429:
        return Fetched("busy", 429, retry_after=_retry_after(reply.headers.get("retry-after")),
                       detail="check in progress (429)")
    if reply.status != 200:
        return Fetched("cannot_read", reply.status, detail=f"the source answered HTTP {reply.status}")
    payload = reply.json()
    if not isinstance(payload, dict):
        return Fetched("cannot_read", 200, detail="the answer is not a JSON object"
                       + (" (it is too large)" if reply.truncated else ""))
    return Fetched("ok", 200, payload=payload)


def _retry_after(value: str | None) -> float | None:
    try:
        seconds = float(value) if value is not None else None
    except ValueError:
        return None
    return seconds if seconds is not None and 0 <= seconds <= MAX_INTERVAL else None


@dataclass
class Finding:
    key: str
    title: str
    detail: str = ""
    severity: str | None = None


@dataclass
class Observation:
    schema_version: str
    observed_at: datetime
    valid_for: float
    findings: list[Finding] | None   # None: the observation carries no findings list (no statement about drift)
    profiles: list[dict] | None
    profiles_problem: str | None = None

    @property
    def valid_until(self) -> datetime:
        return datetime.fromtimestamp(self.observed_at.timestamp() + self.valid_for, timezone.utc)


def read_observation(payload: dict, accepted: tuple[str, ...], now: float) -> tuple[Observation | None, str, str]:
    """(the observation, "ok", "") or (None, "cannot_read" | "stale", why). A time that cannot be represented
    is "cannot read" too, never an error."""
    try:
        return _read_observation(payload, accepted, now)
    except (OverflowError, ValueError, OSError) as exc:
        return None, "cannot_read", f"a time in the observation cannot be read ({type(exc).__name__})"


def _read_observation(payload: dict, accepted: tuple[str, ...], now: float) -> tuple[Observation | None, str, str]:
    version = payload.get("schemaVersion")
    if isinstance(version, bool) or not isinstance(version, (str, int)) or str(version).strip() not in accepted:
        return None, "cannot_read", (f"unknown schemaVersion {str(version)[:40]!r} (this source may send "
                                     f"{', '.join(accepted)}): cannot read, which is not \"no drift\"")
    observed_text = payload.get("observedAt")
    try:
        observed = datetime.fromisoformat(str(observed_text).replace("Z", "+00:00"))
    except ValueError:
        return None, "cannot_read", "observedAt is not a time"
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    valid_for = payload.get("validForSeconds")
    if isinstance(valid_for, bool) or not isinstance(valid_for, (int, float)) or not math.isfinite(valid_for) \
            or not 0 < valid_for <= MAX_VALID_SECONDS:
        return None, "cannot_read", f"validForSeconds is not a number of seconds above 0 and at most a year " \
                                     f"({MAX_VALID_SECONDS:.0f})"
    if observed.timestamp() > now + CLOCK_SKEW:
        return None, "cannot_read", "observedAt lies in the future (check both clocks)"
    findings = None
    if "findings" in payload:
        findings, problem = _findings(payload.get("findings"))
        if problem:
            return None, "cannot_read", problem
    profiles, profiles_problem = None, None
    if "profiles" in payload:
        profiles, profiles_problem = _profiles(payload.get("profiles"))
    observation = Observation(str(version).strip(), observed.astimezone(timezone.utc), float(valid_for), findings,
                              profiles, profiles_problem)
    if observation.valid_until.timestamp() <= now:
        return observation, "stale", (f"the observation from {_iso(observed)} was valid for {valid_for:g}s: it is "
                                      f"too old to act on")
    return observation, "ok", ""


def _findings(value: Any) -> tuple[list[Finding] | None, str | None]:
    if not isinstance(value, list):
        return None, "findings is not a list"
    if len(value) > MAX_FINDINGS:
        return None, f"more than {MAX_FINDINGS} findings"
    found: dict[str, Finding] = {}
    for index, entry in enumerate(value):
        if not isinstance(entry, dict) or not isinstance(entry.get("key"), str) or not entry["key"].strip():
            return None, f"finding {index + 1} has no key (a stable string)"
        key = entry["key"].strip()[:KEY_CHARS]
        title = entry.get("title") if isinstance(entry.get("title"), str) and entry["title"].strip() else key
        severity = entry.get("severity")[:40] if isinstance(entry.get("severity"), str) else None
        detail = entry.get("detail") if isinstance(entry.get("detail"), str) else ""
        found[key] = Finding(key=key, title=title.strip(), detail=detail, severity=severity)
    return list(found.values()), None


PROFILE_FIELDS = {"base_model_id": "base_model_id", "providerKind": "provider_kind",
                  "providerModelId": "provider_model_id", "routeKind": "route_kind",
                  "versionIdAtSnapshot": "version_id", "toolPackageCount": "tool_packages",
                  "filterPackageCount": "filter_packages", "limitations": "limitations"}


def _profiles(value: Any) -> tuple[list[dict] | None, str | None]:
    """The inventory as the role manifest's rows (snake_case); (None, why) when it cannot be read."""
    if not isinstance(value, list) or len(value) > MAX_PROFILES:
        return None, "profiles is not a list (or is too long)"
    rows = []
    for index, entry in enumerate(value):
        if not isinstance(entry, dict) or not isinstance(entry.get("base_model_id"), str):
            return None, f"profile {index + 1} has no base_model_id"
        row: dict[str, Any] = {}
        for name, key in PROFILE_FIELDS.items():
            item = entry.get(name)
            if name in ("toolPackageCount", "filterPackageCount"):
                row[key] = item if isinstance(item, int) and not isinstance(item, bool) and item >= 0 else None
            elif name == "limitations":
                row[key] = [str(text)[:300] for text in item[:50]] if isinstance(item, list) else []
            else:
                row[key] = str(item)[:KEY_CHARS] if isinstance(item, (str, int)) and not isinstance(item, bool) else None
        rows.append(row)
    return rows, None


# ---------------------------------------------------------------- the tracker items


def dedupe_key(source: str, key: str) -> str:
    """The stable key a finding's item is found by (items.open_once's source.hash)."""
    return "drift:" + hashlib.sha256(f"{source}\0{key}".encode("utf-8")).hexdigest()[:32]


def _latest(ledger: tracker.Ledger, source: str, dedupe: str) -> dict | None:
    """The latest item of this source's finding: its type and its key's hash, so the other system-scope items (an
    operator's receipt claims, an Open WebUI profile's change, a person's own) are never taken for one."""
    found = [item for item in ledger.items if (item.get("source") or {}).get("type") == f"drift:{source}"
             and (item.get("source") or {}).get("hash") == dedupe]
    return found[-1] if found else None


def _tracked(ledger: tracker.Ledger, source: str) -> dict[str, dict]:
    """{finding key: its latest item} for every item this source's findings opened (the tracker's own record of
    which findings were reported, independent of the source's state file)."""
    found: dict[str, dict] = {}
    for item in ledger.items:
        origin = item.get("source") or {}
        key = origin.get("key")
        if origin.get("type") == f"drift:{source}" and isinstance(key, str) \
                and origin.get("hash") == dedupe_key(source, key):
            found[key] = item   # later items win: the latest one per finding
    return found


def sync_findings(ledger: tracker.Ledger, source: str, observation: Observation, known: dict[str, dict]) -> dict:
    """Apply one fresh, readable observation's findings to the system scope's ledger (inside its transaction).
    known: the source's per-key state ({item, present, first_seen, last_seen, gone_at}), updated in place; the
    findings present before are those known says, and those whose item is still open in the tracker (a lost or
    reset state never leaves an item open for good). Returns {opened, reopened, closed, kept} (item refs)."""
    at = _iso(observation.observed_at)
    exact = _iso_exact(observation.observed_at)
    evidence_ref = f"drift:{source}@{exact}"
    tracked = _tracked(ledger, source)
    result: dict[str, list[str]] = {"opened": [], "reopened": [], "closed": [], "kept": []}
    current = {finding.key: finding for finding in observation.findings or []}
    for key, finding in current.items():
        dedupe = dedupe_key(source, key)
        record = known.setdefault(key, {"first_seen": at})
        item = _latest(ledger, source, dedupe)
        if item is None:
            item, _ = tracker.open_once(ledger, _fields(source, finding, at), dedupe)
            result["opened"].append(ledger.scope.item_ref(item["id"]))
        elif item.get("status") not in tracker.CLOSED:
            result["kept"].append(ledger.scope.item_ref(item["id"]))
        elif record.get("present") is False or (record.get("present") is None and item.get("status") == "verified"
                                                 and item.get("verified_by") == tracker.FACTORY):
            # it had gone (and its item was proven, or a person had closed it): it recurred. Without a record (a
            # lost state), an item the factory proved is one whose finding had gone
            ledger.change(tracker.FACTORY, item["id"], "open",
                          reason=f"{source} reports it again (observed {at})")
            result["reopened"].append(ledger.scope.item_ref(item["id"]))
        else:
            result["kept"].append(ledger.scope.item_ref(item["id"]))   # closed by a person while still reported
        record.update(item=ledger.scope.item_ref(item["id"]), present=True, last_seen=at, gone_at=None)
    still_open = {key for key, item in tracked.items() if item.get("status") not in tracker.TERMINAL}
    for key in sorted(set(known) | still_open):
        record = known.setdefault(key, {})
        if key in current or (record.get("present") is not True and key not in still_open):
            continue
        record.update(present=False, gone_at=at)
        item = tracked.get(key)
        if item is None or item.get("status") in tracker.TERMINAL:
            continue
        record["item"] = ledger.scope.item_ref(item["id"])
        ledger.change(tracker.FACTORY, item["id"], "verified", note=f"{source} no longer reports it (observed {at})",
                      evidence=[{"kind": "drift", "ref": evidence_ref,
                                 "detail": "a fresh, readable observation no longer reports this finding"}])
        result["closed"].append(ledger.scope.item_ref(item["id"]))
    return result


def _fields(source: str, finding: Finding, at: str) -> dict:
    severity = f" Severity: {finding.severity}." if finding.severity else ""
    detail = (f"{finding.detail.strip()}\n\n" if finding.detail.strip() else "") + \
        f"Reported by the drift source {source} (observed {at}).{severity} It closes on its own, with the " \
        f"observation as evidence, once a fresh check no longer reports it."
    return {"title": f"{source}: {finding.title}", "detail": detail, "kind": "commitment",
            "source": {"type": f"drift:{source}", "key": finding.key, "severity": finding.severity,
                       "observed_at": at}}


# ---------------------------------------------------------------- the manager


def _iso(when: datetime | float | None) -> str | None:
    if when is None:
        return None
    if isinstance(when, (int, float)):
        when = datetime.fromtimestamp(when, timezone.utc)
    return when.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _iso_exact(when: datetime) -> str:
    """An observedAt as sent, to the microsecond: what "no newer than the last one acted on" compares."""
    return when.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_when(text: Any) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _parse_iso(text: Any) -> float | None:
    if not isinstance(text, str):
        return None
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (when if when.tzinfo else when.replace(tzinfo=timezone.utc)).timestamp()


class DriftSources:
    """Every configured drift source: a background thread checks each when it is due (its interval, sooner when
    its observation stops being valid, later while it backs off); check(name) runs one now. Its state is
    <data_dir>/drift-sources/<name>/state.json, its role manifest role-manifest.json next to it (0600, dir
    0700). system: the HousekeepingManager whose transaction() writes the system scope."""

    def __init__(self, config: Any, system: Any, bus: Any = None, clock: Callable[[], float] = time.time,
                 send: Callable[..., plainhttp.Reply] = plainhttp.request) -> None:
        self.config = config
        self.system = system
        self.bus = bus
        self.clock = clock
        self.send = send
        self._busy: dict[str, threading.Lock] = {}
        self._due: dict[str, float] = {}   # each source's next check, also when its state could not be saved
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle -------------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="drift-sources", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(max(0.0, timeout))

    def _loop(self) -> None:
        while not self._stop.wait(POLL_SECONDS):
            try:
                sources = self.sources()
            except ValueError:
                continue
            for name in sources:
                if self._stop.is_set():
                    return
                state = self.state(name)
                due = max((when for when in (_parse_iso(state.get("next_at")), self._due.get(name))
                           if when is not None), default=None)
                if due is None or due <= self.clock():
                    try:   # a source's trouble is in its state, never a dead thread
                        self.check(name)
                    except Exception:  # noqa: BLE001 - and never a check every few seconds either
                        self._due[name] = self.clock() + DOWN_BACKOFF

    # -- reading ---------------------------------------------------------------------------

    def sources(self) -> dict[str, SourceSettings]:
        return parse_sources(self.config.raw)

    def folder(self, name: str) -> Path:
        return Path(self.config.data_dir) / DIR / name

    def state(self, name: str) -> dict:
        try:
            data = json.loads((self.folder(name) / "state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"status": "never", "findings": {}}
        return data if isinstance(data, dict) else {"status": "never", "findings": {}}

    def views(self) -> list[dict]:
        """GET /api/drift-sources rows: settings (never the token), status, times, open findings."""
        rows = []
        for name, settings in self.sources().items():
            state = self.state(name)
            findings = state.get("findings") if isinstance(state.get("findings"), dict) else {}
            rows.append({**settings.view(),
                         **{key: state.get(key) for key in ("status", "detail", "checked_at", "observed_at",
                                                             "valid_until", "schema_version", "next_at", "failures",
                                                             "http", "last_result")},
                         "status": state.get("status") or "never",
                         "present": sorted(key for key, rec in findings.items() if rec.get("present")),
                         "items": sorted({rec["item"] for rec in findings.values() if rec.get("item")}),
                         "manifest": (self.folder(name) / "role-manifest.json").is_file()})
        return rows

    def manifest(self, name: str) -> dict | None:
        if name not in self.sources():
            raise KeyError(f"unknown drift source {name!r}")
        try:
            return json.loads((self.folder(name) / "role-manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    # -- checking ---------------------------------------------------------------------------

    def check(self, name: str) -> dict:
        """Check one source now: {name, status, detail, ...the state}. A check already running for it answers
        "busy" without asking the source again (one call at a time)."""
        settings = self.sources().get(name)
        if settings is None:
            raise KeyError(f"unknown drift source {name!r}")
        with self._lock:
            busy = self._busy.setdefault(name, threading.Lock())
        if not busy.acquire(blocking=False):
            return {"name": name, "status": "busy", "detail": "check in progress (this harness is checking it now)"}
        try:
            return self._check(settings)
        finally:
            busy.release()

    def _check(self, settings: SourceSettings) -> dict:
        """One check. Whatever goes wrong in it, the state is saved with a back-off (never a check every few
        seconds) and no item is opened or closed by half."""
        name, now = settings.name, self.clock()
        state = self.state(name)
        if not isinstance(state.get("findings"), dict):
            state["findings"] = {}
        before = (state.get("status") or "never", state.get("detail"))
        result: dict[str, Any] = {}
        try:
            fetched = fetch(settings, self.send)
            state.update(checked_at=_iso(now), http=fetched.http)
            if fetched.status != "ok":
                self._failed(state, settings, fetched.status, fetched.detail, fetched.retry_after, now)
            else:
                observation, status, detail = read_observation(fetched.payload or {}, settings.schema_versions, now)
                if observation is None:
                    self._failed(state, settings, status, detail, None, now)
                else:
                    state.update(schema_version=observation.schema_version,
                                 observed_at=_iso(observation.observed_at), valid_until=_iso(observation.valid_until))
                    if status == "stale":
                        self._failed(state, settings, "stale", detail, None, now)
                    else:
                        try:
                            result = self._act(settings, state, observation, now)
                        except (OSError, ValueError, tracker.ItemsBusy) as exc:
                            self._failed(state, settings, "error", f"the observation was read, but the tracker or "
                                                                   f"the manifest could not be written: "
                                                                   f"{type(exc).__name__}: {exc}", None, now)
        except Exception as exc:  # noqa: BLE001 - recorded in the state, with the back-off
            state.update(checked_at=_iso(now))
            self._failed(state, settings, "error", f"the check failed: {type(exc).__name__}: {str(exc)[:200]}",
                         None, now)
            result = {}
        self._due[name] = _parse_iso(state.get("next_at")) or now + DOWN_BACKOFF
        self._save(name, state)
        self._journal(name, state, result, before)
        self._publish(name, state)
        return {"name": name, **state, "result": result}

    def _journal(self, name: str, state: dict, result: dict, before: tuple[Any, Any]) -> None:
        """A check worth a record (its status or detail changed, or it opened, reopened or closed an item or wrote
        the role manifest) as a line of <folder>/checks-YYYY-MM.jsonl (0600): status, detail, times, the HTTP
        status and item refs, never a finding's title. The operator's activity query lists them (DESIGN §36);
        a line that cannot be written is skipped."""
        acted = {key: list(result.get(key) or []) for key in ("opened", "reopened", "closed")}
        wrote = isinstance(result.get("profiles"), int) and not isinstance(result.get("profiles"), bool)
        if (state.get("status"), state.get("detail")) == before and not any(acted.values()) and not wrote:
            return
        line = {"at": state.get("checked_at") or _iso(self.clock()), "source": name, "status": state.get("status"),
                "detail": state.get("detail"), "http": state.get("http"), "observed_at": state.get("observed_at"),
                "previous": before[0], **acted, "kept": len(result.get("kept") or []),
                "profiles": result.get("profiles"), "findings": result.get("findings")}
        line = {key: value for key, value in line.items() if value is not None}
        folder = self.folder(name)
        try:
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(folder / f"checks-{str(line['at'])[:7]}.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT,
                         0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n")
        except (OSError, TypeError, ValueError):
            return

    def _act(self, settings: SourceSettings, state: dict, observation: Observation, now: float) -> dict:
        """A fresh, readable observation: the role manifest, then the findings (once per observation)."""
        name = settings.name
        result: dict[str, Any] = {}
        detail = ""
        if observation.profiles is not None:
            written = _parse_when(self._manifest_observed_at(name))
            if written is None or observation.observed_at > written:
                self._write_manifest(settings, observation)
                result["profiles"] = len(observation.profiles)
            else:
                result["profiles"] = "unchanged"   # an older (or the same) inventory never replaces a newer one
        elif observation.profiles_problem:
            detail = f"no role manifest: {observation.profiles_problem}"
        acted = _parse_when(state.get("acted_observed_at"))
        if observation.findings is None:
            result["findings"] = None   # an inventory only: no statement about drift, nothing opened or closed
        elif acted is not None and observation.observed_at <= acted:
            result["findings"] = "unchanged"   # the same (or an older) observation again: nothing new to act on
        else:
            known = copy.deepcopy(state["findings"])   # taken in only once the tracker's transaction is written
            with self.system.transaction() as ledger:
                changes = sync_findings(ledger, name, observation, known)
            state["findings"] = known
            state["acted_observed_at"] = _iso_exact(observation.observed_at)
            state["last_result"] = changes
            result.update(changes)
        until = observation.valid_until.timestamp()
        state.update(status="ok", detail=detail or None, failures=0,
                     next_at=_iso(max(now + MIN_INTERVAL, min(now + settings.interval, until))))
        return result

    def _failed(self, state: dict, settings: SourceSettings, status: str, detail: str, retry_after: float | None,
                now: float) -> None:
        """A check that read nothing usable: no item is opened or closed; the next check backs off (a GET, read
        again later: DESIGN §40)."""
        rule.retried("drift source read")
        failures = int(state.get("failures") or 0) + 1
        if status == "busy":
            wait = retry_after if retry_after is not None else BUSY_BACKOFF * 2 ** (failures - 1)
            wait = min(wait, settings.interval)
        elif status in ("unreachable", "error"):
            wait = min(DOWN_BACKOFF * 2 ** (failures - 1), settings.interval)
        elif status == "stale":
            wait = settings.interval
        else:   # cannot read: 401, 403, 405, an unknown schema, a malformed answer
            wait = min(settings.interval * 2 ** (failures - 1), REFUSED_BACKOFF_MAX)
        state.update(status=status, detail=detail, failures=failures, next_at=_iso(now + max(1.0, wait)))

    def _manifest_observed_at(self, name: str) -> str | None:
        """The observedAt of the role manifest written last (None when there is none, or it cannot be read)."""
        try:
            data = json.loads((self.folder(name) / "role-manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data.get("observed_at") if isinstance(data, dict) else None

    def _write_manifest(self, settings: SourceSettings, observation: Observation) -> None:
        manifest = {"version": 1, "generated_at": _iso(self.clock()), "source": settings.name,
                    "schema_version": observation.schema_version, "observed_at": _iso_exact(observation.observed_at),
                    "valid_until": _iso(observation.valid_until), "roles": observation.profiles or [],
                    "note": "Generated from the drift source's inventory; read-only. The harness never applies it."}
        self._write(settings.name, "role-manifest.json", manifest)

    def _save(self, name: str, state: dict) -> None:
        self._write(name, "state.json", {"version": 1, "name": name, **state})

    def _write(self, name: str, file: str, data: dict) -> None:
        folder = self.folder(name)
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(folder, 0o700)
        fd, tmp = tempfile.mkstemp(dir=folder, prefix=f".{file}.", suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=1, ensure_ascii=False)
            os.replace(tmp, folder / file)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def _publish(self, name: str, state: dict) -> None:
        if self.bus is not None:
            self.bus.publish("drift.checked", {"source": name, "status": state.get("status"),
                                               "detail": state.get("detail"), "observed_at": state.get("observed_at"),
                                               "result": state.get("last_result") if state.get("status") == "ok"
                                               else None})
