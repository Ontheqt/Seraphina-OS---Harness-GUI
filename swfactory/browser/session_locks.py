"""Grants, session locks, lock numbers, session folders and the action log (BROWSER_AND_CODEX.md §2.5, §2.6).

Layout under ``<data_dir>/browser/``:

- ``grants/<G>.json`` (0600): the policy the harness core writes for one locking run, changed in place afterwards
  (``revoked``, ``ended``: the namespace binds this one file, so a new file under its name would never reach the
  shim); ``grants/<G>.lock`` is the per-grant ``flock`` that keeps a second shim off the same grant (and, once it is
  free again, tells run end that no shim of the grant can still write);
  ``grants/<G>.session`` names the session folder the grant's launcher made (the core finds a grant's folder there,
  never from the folder's own files); ``grants/number`` counts grants.
- ``locks/<profile>.lock``: the profile's session lock, an ``flock`` that ``bin/sf-browser`` takes and keeps open for
  the locking run's whole life (the kernel frees it when that run dies); ``locks/<profile>.number``: the profile's
  lock number, one higher for every session lock; ``locks/sessions.number`` counts session folders.
- ``sessions/<SL-id>/``: ``session_lock.json`` (who, when, which grant), ``action-log.jsonl`` (an intent line before
  every non-read action is sent, an outcome line after), ``artifacts/`` (screenshots the shim wrote),
  ``fixture.jsonl`` (the fixture site's own log), ``work/`` (Playwright's working directory), and at run end
  ``receipt.json`` (written by the core). Each folder holds a ``README.untrusted``. The folder is writable inside the
  browser's namespace, so the core reads its files with ``read_untrusted`` (no symlink, no FIFO, a size cap) and
  writes there only through new files of its own.

The words are the session lock's own (K6): no name here uses a word of a GPU custody record. Standard library only.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import re
import secrets
import shutil
import stat
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import UNTRUSTED_README

GRANT_ID_RE = re.compile(r"^G-\d{8}-\d{4,}$")
SESSION_LOCK_ID_RE = re.compile(r"^SL-\d{8}-\d{4,}$")
PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
LOCKED_BY_KINDS = ("chat", "job", "codex", "test")
ACTION_LOG = "action-log.jsonl"
SESSION_FILE = "session_lock.json"
RECEIPT_FILE = "receipt.json"
FIXTURE_LOG = "fixture.jsonl"
README_NAME = "README.untrusted"
DEFAULT_LIMITS = {"max_minutes": 60, "call_seconds": 20, "commit_seconds": 20, "page_chars": 8000,
                  "download_bytes": 50_000_000}
MAX_GRANT_BYTES = 64 * 1024
MAX_RECORD_BYTES = 64 * 1024          # session_lock.json and grants/<G>.session
MAX_LOG_BYTES = 64 * 1024 * 1024      # an action log over this is read as unreadable
_GRANT_WRITES = threading.Lock()      # the core's read-modify-write of a grant (revoke and run end may meet)


class SessionLockBusy(Exception):
    """Another run has this profile's session lock, or another shim already holds this grant."""

    def __init__(self, locked_by_kind: str | None, what: str) -> None:
        super().__init__(what)
        self.locked_by_kind = locked_by_kind


class GrantError(ValueError):
    """A grant file that is missing, unreadable or not one the core wrote (code no_grant)."""


class StaleLockNumber(Exception):
    """An action-log write that carries a lock number other than the profile's current one."""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def browser_root(data_dir: Path | str) -> Path:
    return Path(data_dir) / "browser"


def _private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def _readme(path: Path) -> None:
    target = path / README_NAME
    if not target.exists():
        with contextlib.suppress(OSError):
            target.write_text(UNTRUSTED_README, encoding="utf-8")


def read_untrusted(path: Path, cap: int) -> bytes | None:
    """A file in a session folder (writable inside the namespace), read without trusting what is there: never
    through a symlink, never blocking on a FIFO or a device, only a regular file of at most cap bytes. None
    otherwise, or when it cannot be read."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(fd, min(1 << 20, cap + 1 - size))
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            size += len(chunk)
            if size > cap:
                return None
    except OSError:
        return None
    finally:
        os.close(fd)


def read_record(path: Path) -> dict | None:
    """A small JSON object from a session folder (session_lock.json), read with read_untrusted; None otherwise."""
    raw = read_untrusted(path, MAX_RECORD_BYTES)
    try:
        data = json.loads(raw.decode("utf-8")) if raw is not None else None
    except (UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_private(path: Path, data: Any) -> None:
    """data as JSON, written whole (a new temp file, fsync, rename) with mode 0600. The temp file's name is
    unpredictable and must not exist, so nothing planted in the folder is opened or followed."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=1, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _next_number(counter: Path) -> int:
    """The counter file's number plus one, under its own flock (a file the core and the launcher both use)."""
    fd = os.open(counter, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        raw = os.read(fd, 64).decode("ascii", "replace").strip()
        number = (int(raw) if raw.isdigit() else 0) + 1
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, f"{number}\n".encode("ascii"))
        os.fsync(fd)
        return number
    finally:
        os.close(fd)


def read_number(counter: Path) -> int:
    try:
        raw = counter.read_text(encoding="ascii").strip()
    except OSError:
        return 0
    return int(raw) if raw.isdigit() else 0


def _day() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


# ---------------------------------------------------------------- grants


def write_grant(data_dir: Path | str, *, locked_by: dict, profile: str, origins: dict, fixture_origins: Iterable[str],
                paths: dict, limits: dict | None = None, preview: dict | None = None,
                profile_settings: dict | None = None, worker: dict | None = None) -> tuple[str, Path]:
    """Write a grant (0600) and return (its id, its path). Only the harness core calls this; a model never chooses
    the profile or the origins (§2.6). locked_by: {kind, ref, run, agent}."""
    if locked_by.get("kind") not in LOCKED_BY_KINDS:
        raise ValueError(f"locked_by.kind must be one of {', '.join(LOCKED_BY_KINDS)}")
    if not PROFILE_RE.fullmatch(str(profile)):
        raise ValueError(f"a browser profile name must match {PROFILE_RE.pattern}")
    grants = _private_dir(browser_root(data_dir) / "grants")
    grant_id = f"G-{_day()}-{_next_number(grants / 'number'):04d}"
    record = {"grant": grant_id, "locked_by": dict(locked_by), "profile": profile,
              "origins": {"read": list(origins.get("read") or []), "write": list(origins.get("write") or [])},
              "fixture_origins": list(fixture_origins), "paths": dict(paths), "preview": preview,
              "limits": {**DEFAULT_LIMITS, **(limits or {})}, "profile_settings": dict(profile_settings or {}),
              "worker": dict(worker or {}), "created": utc_now(), "revoked": None, "ended": None}
    path = grants / f"{grant_id}.json"
    _write_private(path, record)
    return grant_id, path


def read_grant(path: Path | str) -> dict:
    """The grant at path, checked for shape (GrantError: the shim answers no_grant)."""
    path = Path(path)
    if not path.is_absolute():
        raise GrantError("the grant path must be absolute")
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_GRANT_BYTES + 1)
    except OSError as exc:
        raise GrantError(f"cannot read the grant: {exc.strerror or exc}") from None
    if len(raw) > MAX_GRANT_BYTES:
        raise GrantError("the grant file is too large")
    try:
        grant = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise GrantError("the grant is not JSON") from None
    if not isinstance(grant, dict) or not GRANT_ID_RE.fullmatch(str(grant.get("grant") or "")):
        raise GrantError("not a grant the harness wrote")
    if path.name != f"{grant['grant']}.json":
        raise GrantError("the grant's id does not match its file name")
    if not PROFILE_RE.fullmatch(str(grant.get("profile") or "")):
        raise GrantError("the grant names no valid profile")
    origins = grant.get("origins")
    if not isinstance(origins, dict) or not isinstance(origins.get("read"), list):
        raise GrantError("the grant has no read origins")
    if not isinstance(grant.get("locked_by"), dict) or grant["locked_by"].get("kind") not in LOCKED_BY_KINDS:
        raise GrantError("the grant names no locking run")
    grant["limits"] = {**DEFAULT_LIMITS, **(grant.get("limits") if isinstance(grant.get("limits"), dict) else {})}
    return grant


def grant_path(data_dir: Path | str, grant_id: str) -> Path:
    if not GRANT_ID_RE.fullmatch(str(grant_id)):
        raise ValueError(f"not a grant id: {grant_id!r}")
    return browser_root(data_dir) / "grants" / f"{grant_id}.json"


def update_grant(path: Path, **fields: Any) -> dict:
    """Set fields of a grant (revoked, ended) and write it again in place, on the same inode: the namespace binds
    the grant as one file, so a renamed new file would never reach the shim, which reads it on every call (rule 3).
    A read that meets the write half done is not JSON, which the shim takes as revoked (fail closed)."""
    with _GRANT_WRITES:
        grant = read_grant(path)
        grant.update(fields)
        data = (json.dumps(grant, indent=1, sort_keys=True) + "\n").encode("utf-8")
        fd = os.open(path, os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            written = 0
            while written < len(data):
                written += os.pwrite(fd, data[written:], written)
            os.ftruncate(fd, len(data))
            os.fsync(fd)
        finally:
            os.close(fd)
    return grant


def revoke_grant(path: Path, by: str = "person") -> dict:
    """Mark a grant revoked (rule 3): the shim answers session_lock_revoked from its next call."""
    return update_grant(path, revoked={"at": utc_now(), "by": by})


# ---------------------------------------------------------------- session locks


@dataclass
class SessionLock:
    """One session lock, held: the open descriptors keep it (they are inherited by bwrap and the shim)."""

    session_lock: str
    lock_number: int
    profile: str
    folder: Path
    number_file: Path
    profile_fd: int
    grant_fd: int

    def descriptors(self) -> tuple[int, int]:
        return self.profile_fd, self.grant_fd

    def close(self) -> None:
        for fd in (self.profile_fd, self.grant_fd):
            with contextlib.suppress(OSError):
                os.close(fd)


def _flock(path: Path) -> int | None:
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            return None
        raise
    return fd


def wait_grant_free(data_dir: Path | str, grant_id: str, seconds: float) -> bool:
    """Wait up to seconds until no process holds the grant's own lock (grants/<G>.lock: the launcher takes it, and
    bwrap and the shim inherit it, so the kernel frees it only when the namespace's last holder is gone). True when
    it is free (or was never taken), False when it is still held at the end or cannot be read."""
    path = grant_path(data_dir, grant_id).with_suffix(".lock")
    deadline = time.monotonic() + max(0.0, float(seconds))
    while True:
        try:
            fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True   # free: closing the descriptor below lets it go again at once
        except OSError as exc:
            if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                return False
        finally:
            os.close(fd)
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _holder_kind(locks: Path, profile: str) -> str | None:
    try:
        current = json.loads((locks / f"{profile}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    kind = (current.get("locked_by") or {}).get("kind") if isinstance(current, dict) else None
    return kind if kind in LOCKED_BY_KINDS else None


def take_session_lock(data_dir: Path | str, grant: dict) -> SessionLock:
    """Take the profile's session lock and the grant's own lock (non-blocking), raise the profile's lock number,
    settle what an earlier run of the profile left (rule 4) and make the session folder. SessionLockBusy when
    another run has the profile or another shim the grant; nothing is stolen."""
    root = browser_root(data_dir)
    locks = _private_dir(root / "locks")
    sessions = _private_dir(root / "sessions")
    _readme(sessions)
    profile = grant["profile"]
    profile_fd = _flock(locks / f"{profile}.lock")
    if profile_fd is None:
        raise SessionLockBusy(_holder_kind(locks, profile), f"another run has the session lock of profile {profile}")
    grant_fd = _flock(root / "grants" / f"{grant['grant']}.lock")
    if grant_fd is None:
        os.close(profile_fd)
        raise SessionLockBusy(grant["locked_by"].get("kind"), f"another shim already holds grant {grant['grant']}")
    try:
        settle_dangling(sessions, profile)
        lock_number = _next_number(locks / f"{profile}.number")
        session_id = f"SL-{_day()}-{_next_number(locks / 'sessions.number'):04d}"
        folder = _private_dir(sessions / session_id)
        _readme(folder)
        for sub in ("artifacts", "work"):
            _private_dir(folder / sub)
        record = {"session_lock": session_id, "lock_number": lock_number, "profile": profile,
                  "grant": grant["grant"], "locked_by": grant["locked_by"], "started": utc_now(), "pid": os.getpid()}
        _write_private(folder / SESSION_FILE, record)
        _write_private(locks / f"{profile}.json", record)
        _write_private(root / "grants" / f"{grant['grant']}.session", {"session_lock": session_id, "profile": profile})
        (folder / ACTION_LOG).touch(mode=0o600)
    except BaseException:
        os.close(grant_fd)
        os.close(profile_fd)
        raise
    return SessionLock(session_id, lock_number, profile, folder, locks / f"{profile}.number", profile_fd, grant_fd)


def settle_dangling(sessions: Path, profile: str) -> int:
    """Rule 4: every action-log entry of the profile's earlier sessions with an intent and no outcome becomes
    unknown (an outcome line written now, marked settled_at_start). Returns how many were settled. Called while
    the profile's session lock is held, so no other session of the profile is live."""
    settled = 0
    for folder in sorted(sessions.glob("SL-*")):
        record = read_record(folder / SESSION_FILE)
        if record is None or record.get("profile") != profile:
            continue
        settled += settle_folder(folder, "settled_at_start")
    return settled


def settle_folder(folder: Path, mark: str) -> int:
    """Every intent of one session folder's action log that has no outcome line gets outcome unknown now, marked
    mark (settled_at_start, settled_at_end). Only when no shim of that session can still write. Returns how many."""
    settled = 0
    for entry in ActionLog.dangling(folder / ACTION_LOG):
        with contextlib.suppress(OSError):
            ActionLog.append_raw(folder / ACTION_LOG, {
                "kind": "outcome", "action_id": entry.get("action_id"), "lock_number": entry.get("lock_number"),
                "outcome": "unknown", mark: True, "at": utc_now()})
            settled += 1
    return settled


def session_folders(data_dir: Path | str) -> list[Path]:
    root = browser_root(data_dir) / "sessions"
    return sorted(root.glob("SL-*")) if root.is_dir() else []


def session_of_grant(data_dir: Path | str, grant_id: str) -> Path | None:
    """The session folder a grant's launcher made (None when it never started), from the core's own index
    (grants/<G>.session), never from a folder's own files, which the namespace can rewrite."""
    if not GRANT_ID_RE.fullmatch(str(grant_id)):
        return None
    index = read_record(browser_root(data_dir) / "grants" / f"{grant_id}.session")
    session_lock = str((index or {}).get("session_lock") or "")
    if not SESSION_LOCK_ID_RE.fullmatch(session_lock):
        return None
    folder = browser_root(data_dir) / "sessions" / session_lock
    return folder if folder.is_dir() and not folder.is_symlink() else None


# ---------------------------------------------------------------- the action log


class ActionLog:
    """The session folder's action-log.jsonl: an intent line before a non-read action is sent, an outcome line after.
    Every write carries the session lock's lock_number and is refused (StaleLockNumber) when the profile's current
    lock number is another one (§2.6)."""

    def __init__(self, path: Path, lock_number: int, number_file: Path) -> None:
        self.path = Path(path)
        self.lock_number = int(lock_number)
        self.number_file = Path(number_file)

    def _check(self) -> None:
        current = read_number(self.number_file)
        if current != self.lock_number:
            raise StaleLockNumber(f"lock number {self.lock_number} is not the current one ({current})")

    def write(self, entry: dict) -> dict:
        self._check()
        line = {**entry, "lock_number": self.lock_number, "at": entry.get("at") or utc_now()}
        self.append_raw(self.path, line)
        return line

    def intent(self, action_id: str, **fields: Any) -> dict:
        return self.write({"kind": "intent", "action_id": action_id, **fields})

    def outcome(self, action_id: str, outcome: str, **fields: Any) -> dict:
        return self.write({"kind": "outcome", "action_id": action_id, "outcome": outcome, **fields})

    def entries(self) -> list[dict]:
        return self.read(self.path)

    @staticmethod
    def append_raw(path: Path, line: dict) -> None:
        """One line appended; OSError when the log is not a regular file (never blocking on a planted FIFO)."""
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(errno.EINVAL, "the action log is not a regular file", str(path))
            os.write(fd, (json.dumps(line, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def read(path: Path) -> list[dict]:
        """The log's entries ([] when it is not a regular file of at most MAX_LOG_BYTES, see read_untrusted)."""
        raw = read_untrusted(Path(path), MAX_LOG_BYTES)
        if raw is None:
            return []
        text = raw.decode("utf-8", "replace")
        out = []
        for line in text.splitlines():
            with contextlib.suppress(ValueError):
                entry = json.loads(line)
                if isinstance(entry, dict):
                    out.append(entry)
        return out

    @classmethod
    def dangling(cls, path: Path) -> list[dict]:
        """The intents that have no outcome line."""
        entries = cls.read(path)
        done = {entry.get("action_id") for entry in entries if entry.get("kind") == "outcome"}
        return [entry for entry in entries if entry.get("kind") == "intent" and entry.get("action_id") not in done]

    @classmethod
    def actions(cls, path: Path) -> list[dict]:
        """One record per action: the intent with its outcome's fields merged in (outcome None while it has none)."""
        merged: dict[str, dict] = {}
        order: list[str] = []
        for entry in cls.read(path):
            action_id = entry.get("action_id")
            if not isinstance(action_id, str):
                continue
            if entry.get("kind") == "intent":
                merged[action_id] = {**entry, "outcome": None}
                order.append(action_id)
            elif entry.get("kind") == "outcome" and action_id in merged:
                intent_at = merged[action_id].get("at")
                merged[action_id].update({k: v for k, v in entry.items() if k not in ("kind", "at")})
                merged[action_id]["at"], merged[action_id]["ended"] = intent_at, entry.get("at")
        return [merged[action_id] for action_id in order]


# ---------------------------------------------------------------- caps (the owner's O6: 5 GB, 30 days)


def folder_bytes(path: Path) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(root, name)).st_size
    return total


def prune_sessions(data_dir: Path | str, max_bytes: int, max_days: float, keep: Iterable[str] = (),
                   now: float | None = None) -> list[str]:
    """Delete session folders (screenshots, action logs, receipts) older than max_days, then the oldest until the
    rest fit in max_bytes. keep: session lock ids never deleted (a live one). Returns the deleted ids."""
    now = time.time() if now is None else now
    keep = set(keep)
    folders = [folder for folder in session_folders(data_dir) if folder.name not in keep]
    deleted: list[str] = []
    sizes = {folder: folder_bytes(folder) for folder in folders}
    for folder in folders:
        with contextlib.suppress(OSError):
            if now - folder.stat().st_mtime > max_days * 86400:
                shutil.rmtree(folder)
                deleted.append(folder.name)
    left = [folder for folder in folders if folder.name not in deleted]
    total = sum(sizes[folder] for folder in left) + sum(folder_bytes(Path(browser_root(data_dir) / "sessions" / k))
                                                        for k in keep)
    for folder in sorted(left, key=lambda f: f.stat().st_mtime if f.exists() else 0):
        if total <= max_bytes:
            break
        with contextlib.suppress(OSError):
            shutil.rmtree(folder)
            deleted.append(folder.name)
            total -= sizes[folder]
    return deleted
