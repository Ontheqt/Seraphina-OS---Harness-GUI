"""Operator change receipts (DESIGN.md section 36): what an operator persona reports she changed, read only.

The operator (operator.py) holds root and acts through her own tools; for every change she writes one JSON
sidecar into ``operator.receipts_dir``, a directory she owns and the harness's user may only read. This module
reads them and nothing else:

- **Read only.** Scanning never writes, renames, creates, chmods, chowns or sets an ACL on anything in her
  directory, and never asks for more access than it has. A subdirectory or file the harness's user may not read
  (her 0700 root ones, or a folder it may list but not search) is listed as unreadable, by name, and left as it
  is; the rest is read, and what was read in it before is not marked gone. The directory is
  opened with ``O_NOFOLLOW``, every subdirectory (at most MAX_DEPTH levels) and file through the parent's
  descriptor with ``O_NOFOLLOW``; a symbolic link, a hard link (it could be a file outside the directory) and a
  file larger than ``operator.max_receipt_bytes`` are refused and listed, never followed. Past
  ``operator.max_receipts`` files the newest are read and the oldest listed (still there: never gone). Only
  ``*.json`` files are read: her prose journal is never parsed (a receipt may link to it). Hidden files (a write
  in progress) are skipped. One file can only ever be a cannot-read entry, never a failed scan. Every receipt
  is read and listed whoever its actor is (other agents' faults included), and carries its actor as written.
- **Two schemas, the same fields.** ``kate-change-receipt/1`` exactly as the operator defined it, and the generic
  alias ``operator-receipt/1``: schema, id (upper case allowed), time, actor, intent, undo_class, jobs (UUIDs),
  before and after (free-form objects, not hashes), rollback (prose), verified and not_verified (lists of
  strings). An unknown schema, a file that does not parse (nested deeper than MAX_NESTING, NaN or Infinity
  included) or a field of the wrong type is recorded as **cannot read** with the reason, never as an empty
  receipt. Times are read by the harness's own ISO-8601 parser, the same on every supported Python. Fields a
  schema does not have are kept as data (extra), never read.
- **The outcome of the change (DESIGN §40).** The harness's own schema ``operator-receipt/1`` may also say how the
  change ended: outcome succeeded, failed or unknown, and for a commit the other side's response (status, the ids
  it returned, the sha256 of its body), never only the action intended. The operator's own schema stays as she
  defined it: an outcome written there is kept as data. An unknown outcome is never read as success or failure.
- **Reported, never enforced.** Every receipt is labelled "reported by the operator". undo_class aliases map
  onto the harness's reversibility classes (UNDO_CLASSES), the original kept beside. An empty verified list
  never reads as success (outcome), and not_verified is shown wherever verified is, first.
- **The tracker keeps what she did not check.** Each not_verified entry becomes one proposed system-scope item
  owned by the operator (persona:<name>), verified by a person, linked to its receipt (links.receipt), taint
  private (and untrusted when the harness's own user could have written the file: harness_may_write, or when the
  receipt's actor does not name her: another agent's words are never hers, whoever owns the item). One item
  per claim on this server (claim_key: the words without a trailing "done", "not done", "verified" ...; never
  the owner, so marking the operator later or adding a second one opens nothing again: the live item follows
  the attribution), kept across rescans until a later receipt lists the claim as verified: the item then waits
  for a person's check (done, with the receipt as reported evidence); a still later receipt that lists it as not
  verified again reopens it. A person's verify, drop, dismiss or reopen is final for the reports seen so far,
  even when the index is lost (the newest reports applied are kept on the item: source.nv, source.v).
- **And what may or may not have happened.** A receipt whose outcome is unknown becomes one proposed item, worded
  "Outcome unknown: check before retrying (receipt <id>)", one per receipt id; when the same receipt later reports
  succeeded or failed (she re-observed and wrote it down), the item waits for a person's check.

The harness keeps its own index of what it read in ``<data_dir>/operator/receipts.json`` (0600, masked from
agents): each file's sha256, what it said, when it was first read and whether it changed or went away since, so
the activity query (activity.py) can answer after the operator's own memory is compacted.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import os
import re
import stat
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from . import items as tracker
from . import outcomes as rule
from .config import expand_path
from .events import redact, utc_now
from .operator import REPORTED, operators, owner as operator_owner

if TYPE_CHECKING:
    from .config import Config
    from .events import EventBus
    from .housekeeping import HousekeepingManager

SCHEMAS = ("kate-change-receipt/1", "operator-receipt/1")   # the operator's own schema id, and its generic alias
GENERIC = "operator-receipt/1"                                # the harness's own schema, which it may extend
FIELDS = ("schema", "id", "time", "actor", "intent", "undo_class", "jobs", "before", "after", "rollback",
          "verified", "not_verified")
# Optional fields a schema adds to the twelve (DESIGN §40): how the change ended, and the other side's answer.
OPTIONAL = {GENERIC: ("outcome", "response")}
RESPONSE_KEYS = ("status", "ids", "body_sha256")
RESULT_WORDS = {rule.SUCCEEDED: "reported succeeded", rule.FAILED: "reported failed",
                rule.UNKNOWN: "outcome unknown: it may or may not have happened; check before retrying"}
# The operator's undo classes and the harness's reversibility classes (docs/seraphina/SERAPHINA_OS_HARNESS.md §2.4);
# a receipt may also name a harness class itself (the v2 design's undo_by_reboot is read as the reboot fallback).
UNDO_CLASSES = {"undo-instant": "undo_instantly", "undo-restore": "undo_by_restore",
                "undo-reboot": "undo_by_reboot_fallback", "cannot-undo": "cannot_undo",
                "undo_instantly": "undo_instantly", "undo_by_restore": "undo_by_restore",
                "undo_by_reboot_fallback": "undo_by_reboot_fallback", "undo_by_reboot": "undo_by_reboot_fallback",
                "cannot_undo": "cannot_undo"}
UNDO_WORDS = {"undo_instantly": "reported as undoable at once",
              "undo_by_restore": "reported as undoable from a backup",
              "undo_by_reboot_fallback": "reported as going back by itself if the server does not start",
              "cannot_undo": "reported as not undoable"}
OUTCOMES = {"nothing_verified": "nothing reported verified",
            "partly_verified": "partly verified (reported): some checks not done",
            "reported_verified": "reported verified by the operator (not re-checked by the harness)",
            # operator-receipt/1's own outcome (DESIGN §40) comes first: verified checks never read as success then
            "outcome_unknown": "outcome unknown (reported): it may or may not have happened; the checks "
                               "reported verified do not settle it",
            "reported_failed": "reported failed: the operator says the change did not happen"}
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$")
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
INDEX_DIR = "operator"
INDEX_FILE = "receipts.json"
INDEX_VERSION = 1
PARSER = 2                    # what parse_receipt reads (2: outcome, response, fields kept as data); older: read again
# Receipts the owner or the operator attached to a workspace model's import (DESIGN §33 "Changes before import"):
# the harness had no copy before its first import, so these are the only record of what changed before it.
BEFORE_IMPORT_FILE = "before-import.json"
ATTACHED_BY = ("owner", "operator")
MAX_ATTACHED = 200
MAX_DEPTH = 4                 # subdirectory levels read below receipts_dir
MAX_NESTING = 64              # arrays and objects inside one another in a receipt (deeper: cannot read)
LIST_MAX = 100                # verified / not_verified entries kept per receipt (more: a warning)
ENTRY_CHARS = 500             # each verified / not_verified entry
TEXT_CHARS = {"intent": 2000, "rollback": 4000, "actor": 200}
OBJECT_KEEP_BYTES = 16384     # before / after kept in the index up to this size (canonical JSON); the file has all
REFUSED_KEEP = 200            # refused paths listed per scan
ACTOR_NOT_STATED = "not stated"   # a receipt that names no actor (one that cannot be read): said so, never guessed
# A subdirectory or file the harness's user may not read: listed by name and left as it is (her ruling, 26 Oct:
# sharing must not widen custody; some of hers are 0700 root and stay so)
UNREADABLE = "the harness's user may not read it: left as it is (the harness never changes its owner, mode or ACL)"
GONE_KEEP_FACTOR = 2          # receipts that went away stay in the index, up to this many times max_receipts
CLAIM_TAIL_RE = re.compile(
    r"(?:\s*[-:,;(–—]\s*|\s+)(?:(?:still|yet)\s+)?(?:not\s+(?:yet\s+)?|un)?"
    r"(?:done|run|tested|verified|checked|confirmed|passed|performed|complete|completed|ok|pending|todo|to do)"
    r"(?:\s+yet)?\)?$")
# An ISO-8601 time, read the same on every supported Python (3.10's fromisoformat takes far less than 3.11's):
# extended or basic form, a T or a space, minutes and seconds optional, a fraction of 1-9 digits, Z or +HH[:MM].
ISO_RE = re.compile(r"(?P<y>\d{4})-?(?P<mo>\d{2})-?(?P<d>\d{2})"
                    r"(?:[Tt ](?P<h>\d{2})(?::?(?P<mi>\d{2})(?::?(?P<s>\d{2})(?:[.,](?P<f>\d{1,9}))?)?)?"
                    r"\s*(?P<z>[Zz]|[+-]\d{2}(?::?\d{2})?)?)?")
_NESTING_RE = re.compile(r'"[^"\\]*(?:\\.[^"\\]*)*"|[\[\]{}]', re.DOTALL)   # a JSON string, or a bracket
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


class ReceiptsError(ValueError):
    """The receipts directory cannot be read at all (not configured, missing, a symlink, not a directory)."""


# ---------------------------------------------------------------- one receipt


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict:
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"the key {key!r} appears twice")
        seen[key] = value
    return seen


def iso_moment(text: str) -> datetime | None:
    """The moment an ISO-8601 text names (ISO_RE; without a zone: naive), or None when it is not one. The same
    on Python 3.10 and later: 2026-10-22T10:15:00+0200, 2026-10-22T08:15:00.5Z and 20261022T081500Z all read."""
    match = ISO_RE.fullmatch(text.strip()) if isinstance(text, str) else None
    if match is None:
        return None
    part = match.groupdict()
    try:
        moment = datetime(int(part["y"]), int(part["mo"]), int(part["d"]), int(part["h"] or 0), int(part["mi"] or 0),
                          int(part["s"] or 0), int((part["f"] or "")[:6].ljust(6, "0")))
    except ValueError:
        return None
    zone = part["z"]
    if not zone:
        return moment
    if zone in ("Z", "z"):
        return moment.replace(tzinfo=timezone.utc)
    digits = zone[1:].replace(":", "")
    hours, minutes = int(digits[:2]), int(digits[2:] or 0)
    if hours > 23 or minutes > 59:
        return None
    offset = timedelta(hours=hours, minutes=minutes)
    return moment.replace(tzinfo=timezone(-offset if zone.startswith("-") else offset))


def parse_time(value: Any) -> tuple[str | None, str | None]:
    """(the time as UTC ISO-8601 with seconds and Z, a warning) from an ISO-8601 text (iso_moment); (None, why)
    when it is not one. A time without a zone is read as UTC, with a warning."""
    if not isinstance(value, str) or not value.strip():
        return None, "time must be an ISO-8601 time such as 2026-10-22T08:15:00Z"
    text = value.strip()
    moment = iso_moment(text)
    if moment is None:
        return None, f"time {text[:40]!r} is not an ISO-8601 time"
    warning = None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
        warning = "time has no zone: read as UTC"
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), warning


def outcome(verified: list[str], not_verified: list[str], reported: str | None = None) -> dict:
    """What the checks say, never success from an empty list: {state, words, verified, not_verified} (counts).
    reported: the outcome the receipt itself gives (operator-receipt/1, reported_outcome.state); unknown or failed
    takes precedence (outcome_unknown, reported_failed), so a change whose outcome is unknown never reads as
    verified (DESIGN §40)."""
    if reported == rule.UNKNOWN:
        state = "outcome_unknown"
    elif reported == rule.FAILED:
        state = "reported_failed"
    elif not verified:
        state = "nothing_verified"
    elif not_verified:
        state = "partly_verified"
    else:
        state = "reported_verified"
    words = OUTCOMES[state]
    if not_verified:
        words += f"; {len(not_verified)} not verified"
    return {"state": state, "words": words, "not_verified": len(not_verified), "verified": len(verified)}


def reported_outcome(doc: dict, warnings: list[str]) -> dict | None:
    """operator-receipt/1's optional outcome and response (DESIGN §40) as {state, words, response}, or None when
    the receipt gives neither. state is succeeded, failed, unknown or None (a response without an outcome: the
    harness never guesses one from it); response is {status, ids, body_sha256} as the other side answered, or
    None. ValueError for a value or type this harness cannot read."""
    state, given = doc.get("outcome"), doc.get("response")
    if state is None and given is None:
        return None
    if state is not None and state not in rule.STATES:
        raise ValueError("outcome must be succeeded, failed or unknown")
    answer = None
    if given is not None:
        if not isinstance(given, dict):
            raise ValueError("response must be an object: what the other side answered (status, ids, body_sha256)")
        status = given.get("status")
        if status is not None and (isinstance(status, bool) or not isinstance(status, (int, str))
                                   or isinstance(status, str) and not 0 < len(status) <= 60):
            raise ValueError("response.status must be a number or a short text (an HTTP status, an exit code)")
        ids = given.get("ids", [])
        if not isinstance(ids, list) or not all(isinstance(value, str) for value in ids):
            raise ValueError("response.ids must be a list of strings (the ids the other side returned)")
        digest = given.get("body_sha256")
        if digest is not None and (not isinstance(digest, str) or not SHA256_RE.fullmatch(digest)):
            raise ValueError("response.body_sha256 must be the sha256 of the answer's body (64 hexadecimal digits)")
        odd = sorted(key for key in given if key not in RESPONSE_KEYS)
        if odd:
            warnings.append("response: fields this harness does not read: " + ", ".join(key[:40] for key in odd[:10]))
        if len(ids) > LIST_MAX:
            warnings.append(f"response.ids lists {len(ids)} ids: the first {LIST_MAX} are kept")
        answer = {"status": status, "ids": [value[:ENTRY_CHARS] for value in ids[:LIST_MAX]],
                  "body_sha256": digest.lower() if digest else None}
    if state is None:
        warnings.append("a response without an outcome: the receipt does not say how the change ended")
        words = "no outcome reported"
    else:
        words = RESULT_WORDS[state]
        if state == rule.SUCCEEDED:
            answered = "the other side's response is recorded" if answer else "no response from the other side"
            words += f"; {answered}"
    return {"state": state, "words": words, "response": answer}


def _clip(text: str, limit: int, field: str, warnings: list[str]) -> str:
    if len(text) <= limit:
        return text
    warnings.append(f"{field} is longer than {limit} characters: cut here (the file has all of it)")
    return text[:limit - 1] + "…"


def _string_list(value: Any, field: str, warnings: list[str]) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(entry, str) for entry in value):
        raise ValueError(f"{field} must be a list of strings")
    entries = [" ".join(entry.split()) for entry in value]
    blank = sum(1 for entry in entries if not entry)
    if blank:
        warnings.append(f"{field}: {blank} empty entr{'y' if blank == 1 else 'ies'} left out")
    entries = [_clip(entry, ENTRY_CHARS, f"a {field} entry", warnings) for entry in entries if entry]
    if len(entries) > LIST_MAX:
        warnings.append(f"{field} lists {len(entries)} entries: the first {LIST_MAX} are kept")
        entries = entries[:LIST_MAX]
    return entries


def _too_deep(text: str, limit: int = MAX_NESTING) -> bool:
    """Whether the JSON text nests arrays and objects deeper than limit (brackets inside strings do not count):
    checked before parsing, so a deeply nested file never reaches the recursive parser."""
    depth = 0
    for match in _NESTING_RE.finditer(text):
        token = match.group()
        if token in ("[", "{"):
            depth += 1
            if depth > limit:
                return True
        elif token in ("]", "}"):
            depth -= 1
    return False


def _no_constant(name: str) -> Any:
    raise ValueError(f"{name} is not JSON (NaN, Infinity and -Infinity are not numbers JSON knows)")


def parse_receipt(data: bytes) -> dict:
    """One receipt file's bytes as {status: "read", ...fields} or {status: "cannot read", problem}. Never raises:
    a file nested deeper than MAX_NESTING levels, or holding NaN or Infinity (which no browser reads as JSON), is
    cannot read too."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return {"status": "cannot read", "problem": "not UTF-8 text"}
    if _too_deep(text):
        return {"status": "cannot read", "problem": f"nested more than {MAX_NESTING} levels deep"}
    try:
        doc = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_no_constant)
    except ValueError as exc:
        return {"status": "cannot read", "problem": f"not JSON: {str(exc)[:160]}"}
    except (RecursionError, MemoryError) as exc:
        return {"status": "cannot read", "problem": f"not JSON this harness can read ({type(exc).__name__})"}
    if not isinstance(doc, dict):
        return {"status": "cannot read", "problem": "a receipt is a JSON object"}
    # who it names, as written, even when it cannot be read: every actor's receipt shows whose it is
    actor = doc["actor"].strip()[:TEXT_CHARS["actor"]] if isinstance(doc.get("actor"), str) else ""
    stated = {"actor": actor} if actor else {}
    schema = doc.get("schema")
    if schema not in SCHEMAS:
        shown = schema[:60] if isinstance(schema, str) else type(schema).__name__ if schema is not None else "none"
        return {"status": "cannot read", "problem": f"unknown schema {shown!r} (this harness reads "
                                                    f"{' and '.join(SCHEMAS)})", **stated}
    missing = [name for name in FIELDS if name not in doc]
    if missing:
        return {"status": "cannot read", "problem": f"missing {', '.join(missing)}", "schema": schema, **stated}
    warnings: list[str] = []
    try:
        receipt_id = doc["id"]
        if not isinstance(receipt_id, str) or not ID_RE.fullmatch(receipt_id):
            raise ValueError("id must be 1-96 letters, digits, '.', '_', ':' or '-', starting with a letter or digit")
        when, problem = parse_time(doc["time"])
        if when is None:
            raise ValueError(problem)
        if problem:
            warnings.append(problem)
        fields: dict[str, str] = {}
        for name in ("actor", "intent", "rollback"):
            value = doc[name]
            if not isinstance(value, str):
                raise ValueError(f"{name} must be text")
            fields[name] = _clip(value.strip(), TEXT_CHARS[name], name, warnings)
        if not fields["actor"]:
            raise ValueError("actor must name who made the change")
        if not fields["intent"]:
            raise ValueError("intent must say what the change was for")
        if not fields["rollback"]:
            warnings.append("no rollback written")
        undo = doc["undo_class"]
        if not isinstance(undo, str) or not undo.strip():
            raise ValueError("undo_class must be text such as undo-instant")
        mapped = UNDO_CLASSES.get(undo.strip())
        if mapped is None:
            warnings.append(f"undo_class {undo.strip()[:40]!r} is not one this harness knows: reversibility unknown")
        jobs = doc["jobs"]
        if not isinstance(jobs, list) or not all(isinstance(job, str) for job in jobs):
            raise ValueError("jobs must be a list of job ids (UUIDs)")
        odd = [job for job in jobs if not UUID_RE.fullmatch(job)]
        if odd:
            warnings.append(f"{len(odd)} job id{'s are' if len(odd) != 1 else ' is'} not a UUID")
        jobs = [job[:100] for job in jobs[:LIST_MAX]]
        for name in ("before", "after"):
            if not isinstance(doc[name], dict):
                raise ValueError(f"{name} must be an object (what the change found and left)")
        verified = _string_list(doc["verified"], "verified", warnings)
        not_verified = _string_list(doc["not_verified"], "not_verified", warnings)
        result = reported_outcome(doc, warnings) if schema in OPTIONAL else None
    except ValueError as exc:
        return {"status": "cannot read", "problem": str(exc), "schema": schema, **stated}
    known = set(FIELDS) | set(OPTIONAL.get(schema, ()))
    extra = {key: doc[key] for key in sorted(doc) if key not in known}
    if extra:   # kept as data, never read: the operator's schema is hers, a later field is not guessed at
        warnings.append("fields this harness does not read (kept as data): "
                        + ", ".join(key[:40] for key in list(extra)[:10]))
    read = {"status": "read", "problem": None, "schema": schema, "id": receipt_id, "time": when,
            "time_as_written": doc["time"] if isinstance(doc["time"], str) else None, "actor": fields["actor"],
            "intent": fields["intent"],
            "undo": {"class": mapped, "original": undo.strip()[:60], "words": UNDO_WORDS.get(mapped or "",
                                                                                          "reversibility unknown")},
            "jobs": jobs, "before": doc["before"], "after": doc["after"], "rollback": fields["rollback"],
            "not_verified": not_verified, "verified": verified,
            "outcome": outcome(verified, not_verified, result["state"] if result else None), "warnings": warnings}
    if result is not None:
        read["reported_outcome"] = result
    if extra:
        read["extra"] = extra
    return read


def claim_key(text: str) -> str:
    """The claim a verified or not_verified entry is about, for matching across receipts: normalised (lower case,
    whitespace collapsed, end punctuation dropped) without a trailing status ("not done", "done", "verified",
    "pending" ...), so "restore drill not done" and a later "restore drill done" are the same claim."""
    key = tracker.normalise(text)
    for _ in range(2):
        stripped = CLAIM_TAIL_RE.sub("", key).strip(" .;:,!-")
        if not stripped or stripped == key:
            break
        key = stripped
    return key or tracker.normalise(text)


# ---------------------------------------------------------------- the directory, read only


def _open_dir(path: Path) -> int:
    try:
        return os.open(path, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC)
    except FileNotFoundError:
        raise ReceiptsError(f"the receipts directory {path} does not exist") from None
    except OSError as exc:
        if os.path.islink(path):
            raise ReceiptsError(f"{path} is a symbolic link: name the directory itself") from None
        if isinstance(exc, NotADirectoryError):
            raise ReceiptsError(f"{path} is not a directory") from None
        if isinstance(exc, PermissionError):
            raise ReceiptsError(f"the harness may not read {path}: give its user read and search (r-x) access") \
                from None
        raise ReceiptsError(f"cannot open {path}: {exc.strerror or exc}") from None


def harness_may_write(info: os.stat_result) -> bool:
    """Whether the harness's own user may change what lies at info: it is root, it owns it (an owner may always
    chmod), or the mode lets its group or everyone write (ACLs aside). Then the harness, or an agent of it that
    runs without bwrap, could plant a receipt there, and it would read as the operator's own words."""
    euid = os.geteuid()
    if euid == 0 or info.st_uid == euid or info.st_mode & stat.S_IWOTH:
        return True
    return bool(info.st_mode & stat.S_IWGRP) and info.st_gid in {os.getegid(), *os.getgroups()}


def _fd_writable(fd: int) -> bool:
    try:
        return harness_may_write(os.fstat(fd))
    except OSError:
        return False


def receipts_problem(config: Config) -> str | None:
    """Why operator.receipts_dir may not be where it is: the same as, inside or around the harness's data
    directory (its worktrees, the tracker, this index), its config directory, ~/.config/software-factory or a
    configured repository, where the harness and its agents write; None when it stands apart."""
    raw = config.raw.get("operator") if isinstance(config.raw.get("operator"), dict) else {}
    directory = raw.get("receipts_dir")
    if not isinstance(directory, str) or not directory.strip():
        return None
    from .config import USER_DIR

    target = _resolved(expand_path(directory, config.path.parent))
    places = [("the harness's data directory", Path(config.data_dir)), ("the config directory", config.path.parent),
              ("the harness's user directory", Path(os.path.expanduser(USER_DIR)))]
    with contextlib.suppress(Exception):
        places += [(f"repository {repo.name}", Path(repo.path)) for repo in config.repos()]
    for label, place in places:
        other = _resolved(place)
        if target == other or other in target.parents or target in other.parents:
            return (f"{directory} overlaps {label} ({place}), where the harness and its agents write: name a "
                    "directory of the operator's own, apart from the harness's files")
    return None


def _resolved(path: Path) -> Path:
    try:
        return Path(os.path.realpath(path))
    except (OSError, ValueError):
        return Path(os.path.abspath(path))


def _walk(dir_fd: int, prefix: str, depth: int, found: list[tuple[str, int, str, os.stat_result, bool]],
          refused: list[dict], opened: list[int]) -> None:
    """Every *.json regular file below dir_fd as (relative path, its dir fd, its name, lstat, whether the harness's
    user may write its directory); symlinks refused. A folder the harness's user may not list, or may list but not
    search (read without execute: no entry in it can be looked at), is refused as unreadable by name, never
    skipped in silence."""
    def unreadable_here() -> None:
        why = UNREADABLE
        with contextlib.suppress(OSError):
            why = f"{UNREADABLE}; {_owner_mode(os.fstat(dir_fd))}"
        refused.append({"file": prefix or ".", "why": why, "unreadable": True})

    try:
        names = sorted(os.listdir(dir_fd))
    except PermissionError:
        unreadable_here()
        return
    except OSError as exc:
        refused.append({"file": prefix or ".", "why": f"cannot list: {exc.strerror or exc}"})
        return
    writable = _fd_writable(dir_fd)
    for name in names:
        if name.startswith("."):
            continue   # hidden: a write in progress, an editor's swap file
        rel = f"{prefix}{name}"
        try:
            info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except PermissionError:   # listed but not searchable (r without x): every entry in it fails alike
            unreadable_here()
            return
        except OSError:
            continue   # gone during the scan
        if stat.S_ISLNK(info.st_mode):
            refused.append({"file": rel, "why": "a symbolic link: refused (it could point outside the directory)"})
        elif stat.S_ISDIR(info.st_mode):
            if depth >= MAX_DEPTH:
                refused.append({"file": rel + "/", "why": f"deeper than {MAX_DEPTH} levels: not read"})
                continue
            try:
                child = os.open(name, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC, dir_fd=dir_fd)
            except PermissionError:   # her 0700 root folder, say: named, never widened
                refused.append({"file": rel + "/", "why": f"{UNREADABLE}; {_owner_mode(info)}", "unreadable": True})
                continue
            except OSError as exc:
                refused.append({"file": rel + "/", "why": f"cannot open: {exc.strerror or exc}"})
                continue
            opened.append(child)
            _walk(child, rel + "/", depth + 1, found, refused, opened)
        elif stat.S_ISREG(info.st_mode) and name.endswith(".json"):
            found.append((rel, dir_fd, name, info, writable))


def _read_file(dir_fd: int, name: str, limit: int) -> tuple[bytes | None, str | None, os.stat_result | None]:
    """(bytes, None, fstat) of one regular, single-link file opened with O_NOFOLLOW, or (None, why, fstat)."""
    try:
        fd = os.open(name, os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK | _O_CLOEXEC, dir_fd=dir_fd)
    except PermissionError:
        return None, UNREADABLE, None
    except OSError as exc:
        return None, f"cannot open: {exc.strerror or exc}", None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None, "not a regular file", info
        if info.st_nlink != 1:
            return None, "a hard link: refused (it could be a file outside the directory)", info
        if info.st_size > limit:
            return None, f"larger than {limit} bytes (operator.max_receipt_bytes)", info
        chunks, size = [], 0
        while size <= limit:
            chunk = os.read(fd, min(65536, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        if size > limit:
            return None, f"larger than {limit} bytes (operator.max_receipt_bytes)", info
        return b"".join(chunks), None, info
    finally:
        os.close(fd)


def _owner_mode(info: os.stat_result) -> str:
    return f"owner uid {info.st_uid}, mode {stat.S_IMODE(info.st_mode):04o}"


def _stamp(info: os.stat_result) -> list[int]:
    return [int(info.st_ino), int(info.st_size), int(info.st_mtime_ns), int(info.st_ctime_ns)]


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _kept_object(value: Any, field: str, warnings: list[str]) -> Any:
    text = _canonical(value)
    if len(text.encode("utf-8")) <= OBJECT_KEEP_BYTES:
        return value
    warnings.append(f"{field} is larger than {OBJECT_KEEP_BYTES} bytes: the harness keeps its keys; the file has all")
    return {"_omitted": f"{len(text.encode('utf-8'))} bytes: read the receipt file",
            "_keys": sorted(str(key) for key in value)[:50] if isinstance(value, dict) else []}


# ---------------------------------------------------------------- the index and the tracker


class Receipts:
    """The receipts of operator.receipts_dir as the harness read them, and their tracker items.

    config: a callable returning the live Config. system: a callable returning the HousekeepingManager (the
    system scope's only writer), or None for a read-only scan (the CLI without the server: nothing is written,
    the index included). secrets: what is redacted from everything kept or served. One scan at a time."""

    def __init__(self, config: Callable[[], Config], data_dir: Path, *, secrets: Callable[[], list[str]] = list,
                 system: Callable[[], HousekeepingManager] | None = None, bus: EventBus | None = None,
                 clock: Callable[[], float] | None = None) -> None:
        self._config = config
        self.root = Path(data_dir) / INDEX_DIR
        self.path = self.root / INDEX_FILE
        self.secrets = secrets
        self.system = system
        self.bus = bus
        self._clock = clock
        self._lock = threading.Lock()
        self._attach_lock = threading.Lock()
        self._last_scan = 0.0
        self._unsaved: dict | None = None   # a read-only scan's result (write False), served by index()

    # -- settings ------------------------------------------------------------------------------

    def settings(self) -> dict:
        from .config import DEFAULTS

        config = self._config()
        raw = config.raw.get("operator") if isinstance(config.raw.get("operator"), dict) else {}
        merged = {**DEFAULTS["operator"], **{k: v for k, v in raw.items() if v is not None}}
        directory = raw.get("receipts_dir")
        merged["receipts_dir"] = str(expand_path(directory, config.path.parent)) \
            if isinstance(directory, str) and directory.strip() else None
        return merged

    def directory(self) -> Path | None:
        value = self.settings()["receipts_dir"]
        return Path(value) if value else None

    def _now(self) -> float:
        import time

        return self._clock() if self._clock is not None else time.time()

    # -- the index -------------------------------------------------------------------------------

    def index(self) -> dict:
        """The harness's index ({version, receipts_dir, scanned_at, files, handled, refused, problem}); empty
        when there is none or it does not parse. After a read-only scan, what that scan found (nothing written)."""
        if self._unsaved is not None:
            return copy.deepcopy(self._unsaved)
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            doc = None
        if not isinstance(doc, dict) or doc.get("version") != INDEX_VERSION:
            doc = {"version": INDEX_VERSION, "receipts_dir": None, "scanned_at": None, "files": {}, "handled": {},
                   "refused": [], "problem": None}
        for key, empty in (("files", {}), ("handled", {}), ("refused", [])):
            if not isinstance(doc.get(key), type(empty)):
                doc[key] = copy.deepcopy(empty)
        return doc

    def _save(self, doc: dict, path: Path | None = None) -> None:
        path = path or self.path
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self.root, 0o700)
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(doc, ensure_ascii=False, sort_keys=True))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise

    # -- changes before an import (DESIGN §33) ---------------------------------------------------------

    def _attachments(self) -> dict:
        try:
            doc = json.loads((self.root / BEFORE_IMPORT_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            doc = None
        if not isinstance(doc, dict) or not isinstance(doc.get("models"), dict):
            doc = {"version": 1, "models": {}}
        return doc

    def attached(self, model_id: str) -> list[dict]:
        """The receipts attached to a workspace model's import, in the order attached: [{id, attached_at, by}]."""
        entry = self._attachments()["models"].get(str(model_id))
        rows = entry.get("receipts") if isinstance(entry, dict) else None
        return [dict(row) for row in rows or [] if isinstance(row, dict) and isinstance(row.get("id"), str)]

    def attach(self, model_id: str, *, attach: list[str] | tuple = (), detach: list[str] | tuple = (),
               by: str = "owner") -> list[dict]:
        """Attach receipts to a workspace model's import, or take them off, by id only (never chosen by their
        text): each id to attach must be one the index holds, read (ValueError naming every unknown one, and
        nothing changes); one to take off must be attached. by: "owner" or "operator", as the caller says (one
        master token cannot tell them apart). Every change is logged in the file. The attached list."""
        if by not in ATTACHED_BY:
            raise ValueError(f"by must be {' or '.join(ATTACHED_BY)}")
        attach, detach = list(attach or []), list(detach or [])
        for value in attach + detach:
            if not isinstance(value, str) or not ID_RE.fullmatch(value):
                raise ValueError(f"{str(value)[:100]!r} is not a receipt id")
        if not attach and not detach:
            raise ValueError("name receipts to attach or detach")
        known = {entry.get("id") for entry in self.index()["files"].values() if entry.get("status") == "read"}
        unknown = sorted({value for value in attach if value not in known})
        if unknown:
            raise ValueError(f"unknown receipt id{'s' if len(unknown) > 1 else ''} {', '.join(unknown)}: only a "
                             "receipt the harness has read can be attached (scan her receipts first)")
        with self._attach_lock:
            doc = self._attachments()
            entry = doc["models"].setdefault(str(model_id), {"receipts": [], "log": []})
            rows = [row for row in entry.get("receipts") or [] if isinstance(row, dict)]
            have = {row.get("id") for row in rows}
            missing = sorted({value for value in detach if value not in have})
            if missing:
                raise ValueError(f"not attached: {', '.join(missing)}")
            now = utc_now()
            added = [value for value in dict.fromkeys(attach) if value not in have]
            rows = [row for row in rows if row.get("id") not in set(detach)]
            rows += [{"id": value, "attached_at": now, "by": by} for value in added]
            if len(rows) > MAX_ATTACHED:
                raise ValueError(f"at most {MAX_ATTACHED} receipts can be attached to one import")
            entry["receipts"] = rows
            entry["log"] = [*(entry.get("log") or []), {"at": now, "by": by, "attached": added,
                                                        "detached": sorted(set(detach))}][-100:]
            self._save(doc, self.root / BEFORE_IMPORT_FILE)
            return [dict(row) for row in rows]

    # -- scanning ---------------------------------------------------------------------------------

    def scan(self, *, force: bool = True, write: bool | None = None) -> dict:
        """Read the receipts directory (only what changed since the last scan is read again), keep the index and
        bring the tracker items up to date. force False: skip when the last scan is younger than
        operator.scan_seconds. write: False never writes anything (default: True when a system scope is given).
        Returns {scanned_at, receipts_dir, read, cannot_read, refused, past_limit, writable, new, changed, gone,
        items, problem, skipped}; never raises for the directory (problem says why it could not be read) nor for
        one file (it is cannot read). Only a scan that ended counts as the last one: one that failed (the tracker
        was busy) is tried again by the next automatic scan."""
        write = self.system is not None if write is None else write
        settings = self.settings()
        with self._lock:
            now = self._now()
            if not force and self._last_scan and now - self._last_scan < float(settings["scan_seconds"] or 0):
                doc = self.index()
                return {**self._summary(doc), "skipped": True}
            doc = self.index()
            stats = self._scan_into(doc, settings, now)
            items: list[str] = []
            if write:
                items = self._apply_items(doc)
                self._save(doc)
            else:
                self._unsaved = copy.deepcopy(doc)
            self._last_scan = now
            summary = {**self._summary(doc), **stats, "items": items, "skipped": False}
        if write and self.bus is not None and (stats["new"] or stats["changed"] or stats["gone"] or items):
            self.bus.publish("operator.receipts", {key: summary[key] for key in (
                "read", "cannot_read", "refused", "new", "changed", "gone")} | {"items": items})
        return summary

    def summary(self) -> dict:
        """What the last scan found (from the index; nothing is read from the directory)."""
        return self._summary(self.index())

    def _summary(self, doc: dict) -> dict:
        files = [entry for entry in doc["files"].values() if not entry.get("gone")]
        return {"scanned_at": doc.get("scanned_at"), "receipts_dir": doc.get("receipts_dir"),
                "read": sum(1 for entry in files if entry.get("status") == "read"),
                "cannot_read": sum(1 for entry in files if entry.get("status") != "read"),
                "refused": len(doc.get("refused") or []), "past_limit": int(doc.get("past_limit") or 0),
                "unreadable": len(unreadable(doc)),
                "writable": bool(doc.get("writable")), "problem": doc.get("problem"),
                "new": 0, "changed": 0, "gone": sum(1 for entry in doc["files"].values() if entry.get("gone"))}

    def _scan_into(self, doc: dict, settings: dict, now: float) -> dict:
        directory = settings["receipts_dir"]
        stats = {"new": 0, "changed": 0, "gone": 0}
        doc["scanned_at"] = _iso(now)
        if doc.get("receipts_dir") not in (None, directory):
            doc["moved_from"] = doc.get("receipts_dir")   # the owner pointed elsewhere: keep what was read
        doc["receipts_dir"] = directory
        if not directory:
            doc["problem"] = "operator.receipts_dir is not set"
            doc["refused"] = []
            return stats
        found: list[tuple[str, int, str, os.stat_result, bool]] = []
        refused: list[dict] = []
        opened: list[int] = []
        try:
            top = _open_dir(Path(directory))
        except ReceiptsError as exc:
            doc["problem"] = str(exc)
            return stats
        opened.append(top)
        secrets = self.secrets()
        try:
            doc["writable"] = _fd_writable(top)
            _walk(top, "", 1, found, refused, opened)
            present = {rel for rel, *_ in found}   # on disk: never marked gone, read or not
            # a folder the harness may not read now: what was read in it before is still there, only unreadable
            hidden = tuple("" if row["file"] == "." else row["file"] for row in refused
                           if row.get("unreadable") and str(row["file"]).endswith(("/", ".")))
            limit = int(settings["max_receipts"])
            doc["past_limit"] = max(0, len(found) - limit)
            if len(found) > limit:   # the newest files are read; the oldest past the limit wait (already read: kept)
                found.sort(key=lambda row: (-row[3].st_mtime_ns, row[0]))
                refused += [{"file": rel, "why": f"past operator.max_receipts ({limit}): an older file, not read"}
                            for rel in sorted(rel for rel, *_ in found[limit:])]
                found = found[:limit]
            for rel, dir_fd, name, info, writable in found:
                entry = doc["files"].get(rel)
                if entry is not None and entry.get("stamp") == _stamp(info) and not entry.get("gone") \
                        and entry.get("parser", 1) == PARSER:
                    continue   # unchanged since the last scan (and read by this parser)
                try:
                    data, why, real = _read_file(dir_fd, name, int(settings["max_receipt_bytes"]))
                    fresh = self._entry(rel, data, why, real or info, now, secrets, writable)
                except Exception as exc:  # noqa: BLE001 - one file is only ever a cannot-read entry, never a failed scan
                    fresh = self._entry(rel, None, f"could not be read ({type(exc).__name__})", info, now, secrets,
                                        writable)
                if entry is None or (entry.get("gone") and entry.get("sha256") != fresh.get("sha256")):
                    stats["new"] += 1
                else:
                    if entry.get("sha256") != fresh.get("sha256"):
                        stats["changed"] += 1
                    if entry.get("status") == "read":   # what it said when first read stays the reference
                        fresh["first_read_at"] = entry.get("first_read_at") or fresh["first_read_at"]
                        fresh["first_sha256"] = entry.get("first_sha256") or entry.get("sha256")
                        fresh["changed_at"] = entry.get("changed_at")
                if fresh.get("sha256") and fresh.get("first_sha256") and fresh["first_sha256"] != fresh["sha256"]:
                    fresh["changed_at"] = fresh.get("changed_at") or _iso(now)
                    fresh.setdefault("warnings", []).append(
                        f"changed after it was first read on {fresh['first_read_at']} (sha256 "
                        f"{str(fresh['first_sha256'])[:12]}… → {str(fresh['sha256'])[:12]}…)")
                elif not fresh.get("changed_at"):
                    fresh.pop("changed_at", None)
                doc["files"][rel] = fresh
            for rel, entry in doc["files"].items():
                if rel not in present and not entry.get("gone") and not rel.startswith(hidden):
                    entry["gone"] = True
                    entry["gone_at"] = _iso(now)
                    stats["gone"] += 1
            gone = sorted((entry.get("gone_at") or "", rel) for rel, entry in doc["files"].items() if entry.get("gone"))
            for _, rel in gone[:max(0, len(gone) - GONE_KEEP_FACTOR * limit)]:
                del doc["files"][rel]   # the oldest of many receipts that went away (the tracker keeps their items)
        finally:
            for fd in opened:
                with contextlib.suppress(OSError):
                    os.close(fd)
        self._mark_duplicates(doc)
        attribute = self.attributor()
        for entry in doc["files"].values():
            if entry.get("status") == "read":
                entry["operator"], entry["operator_named"] = attribute(entry.get("actor"))
        doc["refused"] = refused[:REFUSED_KEEP]
        doc["problem"] = None
        return stats

    def _entry(self, rel: str, data: bytes | None, why: str | None, info: os.stat_result, now: float,
               secrets: list[str], writable: bool = False) -> dict:
        entry: dict[str, Any] = {"file": rel, "stamp": _stamp(info), "bytes": int(info.st_size),
                                 "mtime": _iso(info.st_mtime), "read_at": _iso(now), "first_read_at": _iso(now),
                                 "label": REPORTED, "parser": PARSER}
        if writable or harness_may_write(info):   # its folder, or the file itself
            entry["harness_writable"] = True
        if data is None:
            entry.update(status="cannot read", problem=why, sha256=None)
            if why == UNREADABLE:   # named, never widened
                entry.update(problem=f"{UNREADABLE}; {_owner_mode(info)}", unreadable=True)
            return entry
        entry["sha256"] = hashlib.sha256(data).hexdigest()
        entry["first_sha256"] = entry["sha256"]
        parsed = parse_receipt(data)
        if parsed["status"] == "read":
            warnings = parsed["warnings"]
            parsed["before"] = _kept_object(parsed["before"], "before", warnings)
            parsed["after"] = _kept_object(parsed["after"], "after", warnings)
            if "extra" in parsed:
                parsed["extra"] = _kept_object(parsed["extra"], "the fields kept as data", warnings)
            if entry.get("harness_writable"):
                warnings.append("the harness's own user may write this file or its folder: it may not be the "
                                "operator's (its tracker items are marked untrusted)")
        entry.update(parsed)
        return redact(entry, secrets)

    @staticmethod
    def _mark_duplicates(doc: dict) -> None:
        owners: dict[str, str] = {}
        for rel in sorted(doc["files"]):
            entry = doc["files"][rel]
            entry.pop("duplicate_of", None)
            if entry.get("status") != "read" or entry.get("gone"):
                continue
            first = owners.setdefault(str(entry.get("id")), rel)
            if first != rel:
                entry["duplicate_of"] = first

    # -- attribution ------------------------------------------------------------------------------------

    def attributor(self) -> Callable[[Any], tuple[str | None, bool]]:
        """attribute(actor): (the operator persona a receipt is attributed to, whether its actor names her). Named:
        one whose name or instance name is the actor (any case). Else the only operator when there is exactly one,
        not named: that decides who owns its items only, never whose words they are (another agent's receipt in
        the shared folder is not hers). Else (None, False)."""
        config = self._config()
        names = operators(config)
        aliases = {name: {name.lower()} for name in names}
        if names:
            with contextlib.suppress(Exception):
                from .personas import discover_personas

                found = discover_personas(config)
                for name in names:
                    persona = found.get(name)
                    if persona is not None and persona.instance_name:
                        aliases[name].add(persona.instance_name.lower())

        def attribute(actor: Any) -> tuple[str | None, bool]:
            wanted = str(actor or "").strip().lower()
            for name in names:
                if wanted in aliases[name]:
                    return name, True
            return (names[0] if len(names) == 1 else None), False

        return attribute

    # -- the tracker ------------------------------------------------------------------------------------------

    @staticmethod
    def claim_hash(text: str) -> str:
        """The tracker's key of a claim (claim_key) on this server, whoever reported it and whoever it is attributed
        to now: marking the operator later, or adding a second one, never opens a claim again (DESIGN §36)."""
        return hashlib.sha256(f"receipt-claim|{claim_key(text)}".encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def outcome_hash(receipt_id: str) -> str:
        """The tracker's key of one receipt's unknown outcome (DESIGN §40): one item per receipt id, whichever file
        it is in and however often it is rewritten."""
        return hashlib.sha256(f"receipt-outcome|{receipt_id}".encode("utf-8")).hexdigest()[:16]

    def _outcome_reports(self, doc: dict) -> dict[str, dict]:
        """{hash: {"id", "unknown": [entry], "known": [entry]}} over every read receipt that reports an outcome
        (operator-receipt/1; gone ones too, a duplicate id once), in the receipts' order."""
        reports: dict[str, dict] = {}
        for entry in sorted(doc["files"].values(), key=lambda e: (str(e.get("time")), str(e.get("id")))):
            state = (entry.get("reported_outcome") or {}).get("state") if entry.get("status") == "read" else None
            if state is None or entry.get("duplicate_of"):
                continue
            report = reports.setdefault(self.outcome_hash(str(entry.get("id"))),
                                        {"id": entry.get("id"), "unknown": [], "known": []})
            report["unknown" if state == rule.UNKNOWN else "known"].append(entry)
        return reports

    def _mentions(self, doc: dict) -> dict[str, dict]:
        """{hash: {"nv": [mention], "v": [mention], "first"}} over every read receipt, gone ones too (what she
        reported stays reported), a duplicate id counted once. A mention is (order, entry, text, operator name or
        None); order is the receipt's time then its id, so the same receipt moved to another file is the same
        report."""
        claims: dict[str, dict] = {}
        attribute = self.attributor()
        for entry in doc["files"].values():
            if entry.get("status") != "read" or entry.get("duplicate_of"):
                continue
            name, named = attribute(entry.get("actor"))
            entry["operator"], entry["operator_named"] = name, named
            order = f"{entry.get('time')}|{entry.get('id')}"
            for kind, field in (("nv", "not_verified"), ("v", "verified")):
                for index, text in enumerate(entry.get(field) or []):
                    digest = self.claim_hash(text)
                    claim = claims.setdefault(digest, {"nv": [], "v": [], "first": (order, index)})
                    claim["first"] = min(claim["first"], (order, index))
                    claim[kind].append((order, entry, text, name))
        return dict(sorted(claims.items(), key=lambda pair: pair[1]["first"]))   # new items in the receipts' order

    def _apply_items(self, doc: dict) -> list[str]:
        """Bring the system scope's receipt items up to date with every receipt read; the refs it changed.

        Per claim, the newest not-verified (nv) and verified (v) reports already applied are kept in the index
        (handled) and on the claim's items themselves (source.nv, source.v), so the tracker alone rebuilds them
        when the index is lost: a report already applied never acts again over a person's verify, drop, dismiss
        or reopen."""
        claims = self._mentions(doc)
        reports = self._outcome_reports(doc)
        if not claims and not reports or self.system is None:
            return []
        system = self.system()
        handled = doc.setdefault("handled", {})
        changed: list[str] = []
        with system.transaction() as ledger:
            by_hash: dict[str, list[dict]] = {}
            for item in ledger.items:
                source = item.get("source") or {}
                if source.get("type") == "receipt" and source.get("hash"):
                    by_hash.setdefault(str(source["hash"]), []).append(item)
            for digest, claim in claims.items():
                record = handled.setdefault(digest, {})
                known = by_hash.get(digest, [])
                seen_nv = max([record.get("nv") or ""] + [str((i.get("source") or {}).get("nv") or "") for i in known])
                seen_v = max([record.get("v") or ""] + [str((i.get("source") or {}).get("v") or "") for i in known])
                nv = max(claim["nv"], key=lambda m: m[0]) if claim["nv"] else None
                v = max(claim["v"], key=lambda m: m[0]) if claim["v"] else None
                live = next((i for i in reversed(known) if i.get("status") not in tracker.TERMINAL), None)
                if live is not None and nv is not None and self._reattribute(ledger, live, nv):
                    changed.append(ledger.scope.item_ref(live["id"]))
                if nv is not None and nv[0] > seen_nv:
                    if live is None:
                        live = self._open_item(ledger, digest, nv)
                        changed.append(ledger.scope.item_ref(live["id"]))
                    else:
                        if live.get("status") == "done" and (v is None or nv[0] > v[0]):
                            ledger.change(tracker.FACTORY, live["id"], "open",
                                          reason=f"receipt {nv[1].get('id')} reports it not verified again ({REPORTED})")
                            changed.append(ledger.scope.item_ref(live["id"]))
                        ledger.set_source(live["id"], {"nv": nv[0], "receipt": nv[1].get("id"),
                                                       "file": nv[1].get("file")})
                    record["nv"] = nv[0]
                if v is not None and live is not None and v[0] > seen_v \
                        and live.get("status") != "done" and (nv is None or v[0] > nv[0]):
                    entry = v[1]
                    ledger.change(tracker.FACTORY, live["id"], "done",
                                  note=f"receipt {entry.get('id')} lists it as verified ({REPORTED})",
                                  evidence=[{"kind": "receipt", "ref": str(entry.get("id")), "check": "reported",
                                             "detail": f"{REPORTED} at {entry.get('time')}: verified: {v[2]}"}])
                    ledger.set_source(live["id"], {"v": v[0]})
                    record["v"] = v[0]
                    changed.append(ledger.scope.item_ref(live["id"]))
            attribute = self.attributor()
            for digest, report in reports.items():
                self._apply_outcome(ledger, digest, report, by_hash.get(digest, []), handled.setdefault(digest, {}),
                                    changed, attribute)
            for entry in doc["files"].values():   # which items each receipt's not_verified entries are
                if entry.get("status") == "read":
                    entry["items"] = []
            for digest, claim in claims.items():
                found = [i for i in ledger.items if (i.get("source") or {}).get("type") == "receipt"
                         and (i.get("source") or {}).get("hash") == digest]
                if not found:
                    continue
                ref = ledger.scope.item_ref(found[-1]["id"])
                for _, entry, _, _ in claim["nv"]:
                    if ref not in entry["items"]:
                        entry["items"].append(ref)
            for digest, report in reports.items():   # and its unknown outcome's
                found = [i for i in ledger.items if (i.get("source") or {}).get("type") == "receipt"
                         and (i.get("source") or {}).get("hash") == digest]
                if found:
                    ref = ledger.scope.item_ref(found[-1]["id"])
                    for entry in report["unknown"] + report["known"]:
                        if ref not in entry["items"]:
                            entry["items"].append(ref)
        return list(dict.fromkeys(changed))

    def _apply_outcome(self, ledger: tracker.Ledger, digest: str, report: dict, known: list[dict], record: dict,
                       changed: list[str], attribute: Callable[[Any], tuple[str | None, bool]]) -> None:
        """One receipt id's reported outcome on the tracker (DESIGN §40). Unknown, and no item for it yet: one
        proposed item "Outcome unknown: check before retrying". A version that reports succeeded or failed and is
        newer than every unknown one still read (she re-observed it and rewrote the receipt) makes a live item done,
        waiting for a person's check. Whatever a person decided about the item stands: a receipt id never opens a
        second one, also when the index is lost (the item is found by its hash)."""
        if report["unknown"] and not known and not record.get("u"):
            entry = report["unknown"][-1]
            known = [self._open_outcome_item(ledger, digest, entry, attribute(entry.get("actor"))[0])]
            record["u"] = entry.get("sha256")
            changed.append(ledger.scope.item_ref(known[0]["id"]))
        live = next((i for i in reversed(known) if i.get("status") not in tracker.TERMINAL), None)
        unknown_at = max((str(e.get("mtime") or "") for e in report["unknown"]), default="")
        later = [e for e in report["known"] if str(e.get("mtime") or "") > unknown_at or not report["unknown"]]
        if live is None or not later:
            return
        latest = max(later, key=lambda e: str(e.get("mtime") or ""))
        name = attribute(latest.get("actor"))[0]
        if self._reattribute(ledger, live, ("", latest, "", name)):
            changed.append(ledger.scope.item_ref(live["id"]))
        sha = latest.get("sha256")
        if live.get("status") == "done" or sha in (record.get("k"), (live.get("source") or {}).get("k")):
            return
        result = latest["reported_outcome"]
        answer = result.get("response") or {}
        detail = f"{REPORTED} at {latest.get('time')}: the outcome is now {result['state']}"
        if answer:
            detail += f" (response status {answer.get('status')})"
        ledger.change(tracker.FACTORY, live["id"], "done",
                      note=f"receipt {latest.get('id')} now reports the outcome {result['state']} ({REPORTED})",
                      evidence=[{"kind": "receipt", "ref": str(latest.get("id")), "check": "reported",
                                 "detail": detail}])
        ledger.set_source(live["id"], {"k": sha})
        record["k"] = sha
        changed.append(ledger.scope.item_ref(live["id"]))

    @staticmethod
    def _open_outcome_item(ledger: tracker.Ledger, digest: str, entry: dict, name: str | None) -> dict:
        owner = operator_owner(name) if name else tracker.HUMAN
        detail = (f"Receipt {entry.get('id')} of {entry.get('time')} by {entry.get('actor')} reports the outcome as "
                  f"unknown: the change may or may not have happened. Look again, read only, before trying it again "
                  f"(DESIGN §40).\nIntent: {entry.get('intent')}")
        if entry.get("rollback"):
            detail += f"\nRollback (as written): {entry['rollback']}"
        return ledger.add(tracker.FACTORY, f"{rule.UNKNOWN_TITLE} (receipt {entry.get('id')})", kind="gap",
                          detail=detail, owner=owner, verifier=tracker.HUMAN, status="proposed", uncapped=True,
                          taint=_receipt_taint(entry), links={"receipt": entry.get("id")},
                          source={"type": "receipt", "agent": owner, "receipt": entry.get("id"),
                                  "file": entry.get("file"), "hash": digest, "outcome": rule.UNKNOWN,
                                  "u": entry.get("sha256"), "reported": REPORTED})

    @staticmethod
    def _reattribute(ledger: tracker.Ledger, item: dict, mention: tuple[str, dict, str, str | None]) -> bool:
        """Give a live item the operator its newest not-verified report is attributed to now, when the harness chose
        its owner (a person's assignment stands) and that is an operator persona (no operator, or several and none
        named: it keeps its owner). True when the owner changed."""
        name = mention[3]
        source = item.get("source") or {}
        if not name or item.get("owner") != source.get("agent") or item.get("owner") == operator_owner(name):
            return False
        new = operator_owner(name)
        ledger.reown(item["id"], new, note=f"receipt {mention[1].get('id')} is attributed to {new} ({REPORTED})")
        ledger.set_source(item["id"], {"agent": new})
        return True

    @staticmethod
    def _open_item(ledger: tracker.Ledger, digest: str, mention: tuple[str, dict, str, str | None]) -> dict:
        order, entry, text, name = mention
        owner = operator_owner(name) if name else tracker.HUMAN
        detail = (f"Reported as not verified in receipt {entry.get('id')} of {entry.get('time')} by "
                  f"{entry.get('actor')}: {entry.get('intent')}")
        if entry.get("rollback"):
            detail += f"\nRollback (as written): {entry['rollback']}"
        return ledger.add(tracker.FACTORY, text, kind="gap", detail=detail, owner=owner, verifier=tracker.HUMAN,
                          status="proposed", uncapped=True, taint=_receipt_taint(entry),
                          links={"receipt": entry.get("id")},
                          source={"type": "receipt", "agent": owner, "receipt": entry.get("id"),
                                  "file": entry.get("file"), "hash": digest, "nv": order, "reported": REPORTED})

    # -- reading ---------------------------------------------------------------------------------------

    def rows(self, *, limit: int = 200, since: str | None = None, until: str | None = None,
             include_gone: bool = True) -> list[dict]:
        """The receipts the index holds, newest first: the list view (before and after left out); cannot-read
        files by their modification time."""
        doc = self.index()
        rows = []
        for entry in doc["files"].values():
            if entry.get("gone") and not include_gone:
                continue
            when = entry.get("time") or entry.get("mtime") or ""
            if since and when < since or until and when > until:
                continue
            rows.append(self.view(entry, full=False))
        rows.sort(key=lambda row: (row.get("time") or row.get("mtime") or "", row["file"]), reverse=True)
        return rows[:max(0, int(limit))]

    def get(self, receipt_id: str) -> dict:
        """{receipt, duplicates}: one receipt by its id (the index's copy, with before and after); KeyError."""
        doc = self.index()
        matches = sorted((rel for rel, entry in doc["files"].items() if entry.get("id") == receipt_id))
        if not matches:
            raise KeyError(f"receipt {receipt_id}")
        return {"receipt": self.view(doc["files"][matches[0]], full=True), "duplicates": matches[1:]}

    def view(self, entry: dict, *, full: bool) -> dict:
        """A receipt as the API serves it: not_verified before verified, the label, never a stamp."""
        keep = ("file", "status", "problem", "schema", "id", "time", "time_as_written", "actor", "operator",
                "intent", "undo", "jobs", "rollback", "not_verified", "verified", "outcome", "reported_outcome",
                "warnings", "label", "items", "sha256", "first_sha256", "bytes", "mtime", "read_at", "first_read_at",
                "changed_at", "gone", "gone_at", "duplicate_of", "harness_writable", "unreadable")
        row = {key: copy.deepcopy(entry.get(key)) for key in keep if key in entry}
        row["actor"] = row.get("actor") or None   # as written in the receipt; null: not stated (ACTOR_NOT_STATED)
        row["label"] = REPORTED
        row.setdefault("not_verified", [])
        row.setdefault("verified", [])
        if row.get("status") == "read":   # computed again: an index written before reported_outcome counted
            row["outcome"] = outcome(row["verified"], row["not_verified"],
                                     (row.get("reported_outcome") or {}).get("state"))
        if full:
            for key in ("before", "after"):
                row[key] = copy.deepcopy(entry.get(key))
            if "extra" in entry:
                row["extra"] = copy.deepcopy(entry["extra"])
        return redact(row, self.secrets())


def _receipt_taint(entry: dict) -> tuple[str, ...]:
    """The taint of the items a receipt opens: private, and untrusted unless its words are hers. They are not when
    the harness's own user could have written the file (harness_may_write), nor when its actor does not name her
    (operator_named false: another agent's receipt in the shared folder, attributed to her only because she is the
    one operator, or one attributed to nobody). Her wake then frames them untrusted: wakes._operator_items keeps
    the item's own taint."""
    if entry.get("harness_writable") or not entry.get("operator_named"):
        return "private", "untrusted"
    return ("private",)


def unreadable(doc: dict) -> list[str]:
    """What the last scan could not read for want of access (a 0700 root folder or file of hers), by name: listed,
    left as it is, and never widened."""
    names = [str(row.get("file")) for row in doc.get("refused") or [] if isinstance(row, dict) and row.get("unreadable")]
    names += [rel for rel, entry in (doc.get("files") or {}).items() if entry.get("unreadable") and not entry.get("gone")]
    return sorted(names)


def receipt_lines(row: dict) -> list[str]:
    """A receipt for a terminal: what it says, not_verified before verified, always "reported by the operator"."""
    if row.get("status") != "read":
        return [f"{row.get('file')}  cannot read: {row.get('problem')}  (actor: "
                f"{row.get('actor') or ACTOR_NOT_STATED})"]
    undo = row.get("undo") or {}
    lines = [f"{row.get('time')}  {row.get('id')}  {row.get('actor')}  ({REPORTED})",
             f"  intent: {row.get('intent')}",
             f"  undo: {undo.get('original')} → {undo.get('class') or 'unknown'} ({undo.get('words')})",
             f"  outcome: {(row.get('outcome') or {}).get('words')}"]
    result = row.get("reported_outcome") or {}
    if result:
        answer = result.get("response") or {}
        lines.append(f"  the change: {result.get('words')}" + (
            f" (response status {answer.get('status')}, ids {', '.join(answer.get('ids') or []) or 'none'}, body "
            f"sha256 {str(answer.get('body_sha256') or 'none')[:12]})" if answer else ""))
    lines += [f"  NOT verified: {text}" for text in row.get("not_verified") or []]
    lines += [f"  verified (reported): {text}" for text in row.get("verified") or []]
    if not row.get("verified"):
        lines.append("  verified: nothing")
    lines += [f"  warning: {text}" for text in row.get("warnings") or []]
    return lines
