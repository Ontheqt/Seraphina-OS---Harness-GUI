"""The notice rulebook: one versioned file for every path that reaches the owner's phone (operator decision g;
docs/seraphina/WAKE_AND_MESSAGES.md §2.7, §3.2, §3.4, §3.9).

A persona may have its own notify tool outside the harness (an Open WebUI tool, say), not coupled to the
harness notifier, so the two fail independently. Quiet hours, the data boundary, the rate limits and the audit
format are therefore defined once, in ``<config dir>/notice-rules.json``, which the harness notifier reads and an
outside notifier can read too; there is no second copy of the logic. The file is read-only to agents (bound back
read-only below the masked config directory, H1) and written only by the harness, when the owner changes it in
the cockpit (POST /api/notify/rules) or confirms a persona's profile clean.

Schema (version 1)::

    {"version": 1,
     "quiet_hours": "auto" | "HH:MM-HH:MM" | null,          # do-not-disturb time; auto follows the power schedule
     "quiet_breakthrough": ["backup.failed", ...],          # deterministic rules whose high alerts may buzz in it
     "rate_limits": {"per_persona_min_minutes": 30, "per_persona_max_per_day": 4, "personas_max_per_day": 8,
                     "phone_max_per_day": 12, "dedupe_hours": 24},
     "boundary": {"third_party": "short_only",              # a third-party channel only ever gets fixed short notices
                  "tailnet_model_text": "clean_only" | "never",   # a persona's own words, only from a clean turn
                  "phone_short_until_profile_confirmed": true,    # the shared boundary (below)
                  "max_title": 60, "max_body": 300, "token_run_chars": 20, "digit_run": 8,
                  "force_short_on": ["url", "markdown", "html", "secret", "token_run", "digit_run",
                                     "sensitive_term", "too_long"]},
     "confirmed_profiles": {"<persona>": {"sha256": "<64 hex>", "what": "soul" | "profile", "at": "..."}},
     "audit": {"format": "jsonl-v2", "path": "<data_dir>/wakes/audit-YYYY-MM.jsonl",
               "line": ["at", "kind", "instance", "agent", "...", "prev"]}}

The shared boundary: anything that reaches the phone, from the harness or from a persona's own notifier, stays a
fixed short notice until the owner confirms that the persona's imported profile (its soul file, or its Open WebUI
system prompt) holds no personal or health details; the confirmation is bound to that text's sha256 and lives in
``confirmed_profiles``, the one place both notifiers read it. The boundary numbers may be made stricter (a shorter
body, a shorter token run), never weaker than the floors in code.

Until the file exists, the same values come from config.json's notify section (the keys this file replaces), so
an install without it keeps working; the first change through the cockpit writes the file, and from then on
config patches to those keys are refused (settings._check_notify names this file).
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import os
import re
import threading
from pathlib import Path
from typing import Any

from .events import utc_now

RULES_FILE = "notice-rules.json"
RULES_VERSION = 1
FLOORS = {"max_title": 60, "max_body": 300, "token_run_chars": 20, "digit_run": 8}   # never weaker than these
FORCE_SHORT = ("url", "markdown", "html", "secret", "token_run", "digit_run", "sensitive_term", "too_long")
RATE_KEYS = {"per_persona_min_minutes": (0, 1440), "per_persona_max_per_day": (0, 48),
             "personas_max_per_day": (0, 200), "phone_max_per_day": (0, 200), "dedupe_hours": (1, 168)}
# The config.json notify keys this file replaces (read as the fallback while the file does not exist).
CONFIG_KEYS = ("quiet_hours", "quiet_breakthrough", "per_persona", "personas_max_per_day", "phone_max_per_day",
               "dedupe_hours")
AUDIT = {"format": "jsonl-v2", "path": "<data_dir>/wakes/audit-YYYY-MM.jsonl",
         "line": ["at", "kind", "instance", "agent", "...the kind's fields", "prev"]}
DEFAULT_RULES: dict = {
    "version": RULES_VERSION,
    "quiet_hours": "auto",
    "quiet_breakthrough": ["backup.failed", "service.down", "disk.failing"],
    "rate_limits": {"per_persona_min_minutes": 30, "per_persona_max_per_day": 4, "personas_max_per_day": 8,
                    "phone_max_per_day": 12, "dedupe_hours": 24},
    "boundary": {"third_party": "short_only", "tailnet_model_text": "clean_only",
                 "phone_short_until_profile_confirmed": True, **FLOORS, "force_short_on": list(FORCE_SHORT)},
    "confirmed_profiles": {},
    "audit": AUDIT,
}
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
_WINDOW_RE = re.compile(r"^\d{2}:\d{2}-\d{2}:\d{2}$")
_LOCK = threading.Lock()
_CACHE: dict[str, tuple[tuple, dict]] = {}   # path -> (its stat, the rules read from it)


def rules_path(config: Any) -> Path:
    """<config dir>/notice-rules.json."""
    return Path(config.path).parent / RULES_FILE


def _legacy(config: Any) -> dict:
    """The rules as config.json's notify section gives them (no rules file yet)."""
    notify = config.raw.get("notify") if isinstance(getattr(config, "raw", None), dict) else None
    notify = notify if isinstance(notify, dict) else {}
    rules = copy.deepcopy(DEFAULT_RULES)
    if "quiet_hours" in notify:
        rules["quiet_hours"] = notify.get("quiet_hours")
    if isinstance(notify.get("quiet_breakthrough"), list):
        rules["quiet_breakthrough"] = list(notify["quiet_breakthrough"])
    per = notify.get("per_persona") if isinstance(notify.get("per_persona"), dict) else {}
    limits = rules["rate_limits"]
    for source, target in (("min_minutes", "per_persona_min_minutes"), ("max_per_day", "per_persona_max_per_day")):
        if _is_int(per.get(source)):
            limits[target] = per[source]
    for key in ("personas_max_per_day", "phone_max_per_day", "dedupe_hours"):
        if _is_int(notify.get(key)):
            limits[key] = notify[key]
    return rules


def load_rules(config: Any) -> dict:
    """The rules in force: the file when it exists (validated; a file that does not validate fails closed: the
    defaults with a persona's words kept off every phone, and error says why), else config.json's notify keys.
    Adds "source" ("file" | "config") and "error"."""
    path = rules_path(config)
    try:
        stat = path.stat()
    except OSError:
        return {**_legacy(config), "source": "config", "error": None}
    key = (str(path), stat.st_mtime_ns, stat.st_size, stat.st_ino)
    cached = _CACHE.get(str(path))
    if cached is not None and cached[0] == key:
        return copy.deepcopy(cached[1])
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        result = {**validate_rules(doc), "source": "file", "error": None}
    except (OSError, ValueError) as exc:
        closed = copy.deepcopy(DEFAULT_RULES)
        closed["boundary"]["tailnet_model_text"] = "never"
        result = {**closed, "source": "file", "error": f"{path.name}: {exc}"}
    _CACHE[str(path)] = (key, copy.deepcopy(result))
    return result


def validate_rules(doc: Any) -> dict:
    """The rules document, checked (ValueError naming the key); unknown keys are refused."""
    if not isinstance(doc, dict):
        raise ValueError("the rules file must be a JSON object")
    unknown = sorted(set(doc) - set(DEFAULT_RULES))
    if unknown:
        raise ValueError(f"{unknown[0]} is not a key of the rules file (version {RULES_VERSION})")
    if doc.get("version") != RULES_VERSION:
        raise ValueError(f"version must be {RULES_VERSION}")
    out = copy.deepcopy(DEFAULT_RULES)
    quiet = doc.get("quiet_hours", out["quiet_hours"])
    if quiet is not None and quiet != "auto" and not (isinstance(quiet, str) and _WINDOW_RE.fullmatch(quiet)):
        raise ValueError('quiet_hours must be "auto", "HH:MM-HH:MM" or null')
    if isinstance(quiet, str) and quiet != "auto":
        from .clock import Window

        Window.parse(quiet)
    out["quiet_hours"] = quiet
    breakthrough = doc.get("quiet_breakthrough", out["quiet_breakthrough"])
    from .notify import RULE_IDS

    if not isinstance(breakthrough, list) or not all(isinstance(x, str) and x in RULE_IDS for x in breakthrough):
        raise ValueError("quiet_breakthrough must be a list of rule ids")
    out["quiet_breakthrough"] = list(breakthrough)
    limits = doc.get("rate_limits", {})
    if not isinstance(limits, dict) or set(limits) - set(RATE_KEYS):
        raise ValueError("rate_limits must be an object of " + ", ".join(RATE_KEYS))
    for key, (low, high) in RATE_KEYS.items():
        if key in limits:
            if not _is_int(limits[key]) or not low <= limits[key] <= high:
                raise ValueError(f"rate_limits.{key} must be an integer from {low} to {high}")
            out["rate_limits"][key] = limits[key]
    boundary = doc.get("boundary", {})
    if not isinstance(boundary, dict) or set(boundary) - set(DEFAULT_RULES["boundary"]):
        raise ValueError("boundary has an unknown key")
    if boundary.get("third_party", "short_only") != "short_only":
        raise ValueError('boundary.third_party is always "short_only": a third party only ever gets fixed heads-ups')
    if boundary.get("tailnet_model_text", "clean_only") not in ("clean_only", "never"):
        raise ValueError('boundary.tailnet_model_text must be "clean_only" or "never"')
    out["boundary"]["tailnet_model_text"] = boundary.get("tailnet_model_text", "clean_only")
    if boundary.get("phone_short_until_profile_confirmed", True) is not True:
        raise ValueError("boundary.phone_short_until_profile_confirmed is always true (the shared boundary)")
    for key, floor in FLOORS.items():
        if key in boundary:
            value = boundary[key]
            if not _is_int(value) or value < 1 or value > floor:
                raise ValueError(f"boundary.{key} must be an integer from 1 to {floor} (stricter, never weaker)")
            out["boundary"][key] = value
    force = boundary.get("force_short_on", list(FORCE_SHORT))
    if not isinstance(force, list) or not set(FORCE_SHORT) <= set(force) or not set(force) <= set(FORCE_SHORT):
        raise ValueError("boundary.force_short_on must list every filter: " + ", ".join(FORCE_SHORT))
    confirmed = doc.get("confirmed_profiles", {})
    if not isinstance(confirmed, dict):
        raise ValueError("confirmed_profiles must be an object")
    for name, entry in confirmed.items():
        if not _NAME_RE.fullmatch(str(name)) or not isinstance(entry, dict) \
                or not _SHA_RE.fullmatch(str(entry.get("sha256") or "")) or entry.get("what") not in ("soul", "profile"):
            raise ValueError(f"confirmed_profiles.{name} must be {{sha256, what: soul | profile, at}}")
        out["confirmed_profiles"][str(name)] = {"sha256": entry["sha256"], "what": entry["what"],
                                                "at": str(entry.get("at") or "")}
    if "audit" in doc and doc["audit"] != AUDIT:
        raise ValueError("audit describes the harness's audit line format; it cannot be changed")
    return out


def write_rules(config: Any, doc: dict) -> dict:
    """Validate and write the rules file (0600, atomically); returns the rules in force."""
    from .chats import write_atomic

    rules = validate_rules({key: value for key, value in doc.items() if key not in ("source", "error")})
    path = rules_path(config)
    with _LOCK:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_atomic(path, json.dumps(rules, indent=2, sort_keys=True) + "\n")
        os.chmod(path, 0o600)
    return load_rules(config)


def update_rules(config: Any, change: dict) -> dict:
    """The rules in force with change applied on top (nested objects merged), written to the file."""
    current = {key: value for key, value in load_rules(config).items() if key not in ("source", "error")}
    for key, value in change.items():
        if isinstance(value, dict) and isinstance(current.get(key), dict) and key != "confirmed_profiles":
            current[key] = {**current[key], **value}
        else:
            current[key] = value
    return write_rules(config, current)


def confirm_profile(config: Any, persona: str, sha256: str, *, what: str, clean: bool) -> dict:
    """The owner's "this text has no personal or health details" for exactly the text with this sha256 (clean
    True), or its withdrawal; written to confirmed_profiles."""
    if not _NAME_RE.fullmatch(str(persona)) or not _SHA_RE.fullmatch(str(sha256)) or what not in ("soul", "profile"):
        raise ValueError("a confirmation names a persona, a sha256 and what it covers (soul or profile)")
    with _LOCK:
        current = {key: value for key, value in load_rules(config).items() if key not in ("source", "error")}
    confirmed = dict(current.get("confirmed_profiles") or {})
    if clean:
        confirmed[persona] = {"sha256": sha256, "what": what, "at": utc_now()}
    else:
        confirmed.pop(persona, None)
    current["confirmed_profiles"] = confirmed
    return write_rules(config, current)


def confirmed(rules: dict, persona: str | None, sha256: str | None) -> bool:
    """Whether the owner confirmed this persona's exact text clean."""
    entry = (rules.get("confirmed_profiles") or {}).get(str(persona or ""))
    return bool(entry and sha256 and entry.get("sha256") == sha256)


def rules_digest(config: Any) -> str | None:
    """sha256 of the rules file's bytes (S47), None when there is none."""
    with contextlib.suppress(OSError):
        return hashlib.sha256(rules_path(config).read_bytes()).hexdigest()
    return None


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)
