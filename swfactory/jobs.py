"""JobStore: job ids, job.json persistence, job-folder artifacts, verdict parsing, backlog.

Only the orchestrator writes job.json (via JobStore.save). Every write here is
atomic (temporary file + os.replace), so readers never see a partial file.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import re
import stat
import tempfile
import threading
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Callable, Iterator, TextIO

from . import listeners
from .events import utc_now
from .items import empty_summary

__all__ = [
    "JOB_ID_RE",
    "REPO_NAME_RE",
    "JobStore",
    "read_verdict",
    "slugify",
    "utc_now",
]

JOB_ID_RE = re.compile(r"^\d{3}-[a-z0-9]+(-[a-z0-9]+)*$", re.ASCII)
REPO_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$", re.ASCII)
AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", re.ASCII)

STOPWORDS = frozenset(
    "a an the to of for and or in on with by from into my our your this that be is are".split()
)
SLUG_MAX_WORDS = 5
SLUG_MAX_CHARS = 40
MAX_JOB_NUMBER = 999
BASE_PORT = 5100
PORT_STRIDE = 10
JOB_PORTS = 3            # the builder's port, then reviewer-ux's (+1) and reviewer-ui's (+2): orchestrator.PORT_OFFSETS
OVERFLOW_PORT = 15100    # a job whose block something already listens on moves to 15100 + 10n, or a spare block
SPARE_PORT = OVERFLOW_PORT + PORT_STRIDE * (MAX_JOB_NUMBER + 1)   # 25100: above every job number's overflow block
LAST_PORT = 32767        # below Linux's ephemeral range (32768 and up), which outgoing connections draw from

VERDICT_LINE_LIMIT = 4096  # chars; a first non-empty line this long is malformed
VERDICT_SCAN_LIMIT = 64 * 1024  # chars read at most while looking for that line

VERDICTS = {
    "review": {"VERDICT: PASS": "PASS", "VERDICT: CHANGES": "CHANGES"},
    "build": {"STATUS: READY": "READY", "STATUS: BLOCKED": "BLOCKED"},
    "decision": {"APPROVE": "APPROVE", "ESCALATE": "ESCALATE"},
}

ALLOCATE_LOCK_FILE = ".allocate.lock"
BACKLOG_FILE = "backlog.md"
SUPPRESS_FILE = "sweep-suppress.json"
BACKLOG_HEADER = "# Backlog\n\n"
CLAIMED = "claimed"
_BACKLOG_ITEM_RE = re.compile(r"^\s*[-*]\s+\[( |x|X)\]\s+(.*?)\s*$")
_BACKLOG_JOB_RE = re.compile(r"^(.*\S)\s*(?:→|->)\s*(\S+)$")
_JOB_NUMBER_RE = re.compile(r"^(\d{3})-", re.ASCII)
_LOG_NUMBER_RE = re.compile(r"^(\d+)-.+\.jsonl$", re.ASCII)
_REWORK_RE = re.compile(r"^rework-(\d+)\.md$", re.ASCII)
_TMP_PREFIX = ".sf-"
_TMP_SUFFIX = ".tmp"
_BOM = "﻿"

_locks: dict[tuple[str, str], threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(kind: str, path: Path) -> threading.Lock:
    """Process-wide lock for (kind, path), shared by every JobStore instance."""
    key = (kind, os.path.abspath(path))
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


@contextlib.contextmanager
def _allocation_lock(root: Path) -> Iterator[None]:
    """Hold the repo lock plus an exclusive flock on root/.allocate.lock (creating root)."""
    with _lock_for("repo", root):
        root.mkdir(parents=True, exist_ok=True)
        fd = os.open(root / ALLOCATE_LOCK_FILE, os.O_RDWR | os.O_CREAT | os.O_NOCTTY, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)  # also releases the flock


def _atomic_write(path: Path, text: str) -> None:
    """Write text verbatim to path via a temporary file in the same directory and os.replace."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=_TMP_PREFIX + path.name + ".",
                               suffix=_TMP_SUFFIX)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _is_temp_file(name: str) -> bool:
    return name.startswith(_TMP_PREFIX) and name.endswith(_TMP_SUFFIX)


def _not_regular(path: str | os.PathLike) -> FileNotFoundError:
    return FileNotFoundError(errno.ENOENT, "not a regular file", os.fspath(path))


def _open_regular(path: str | os.PathLike, *, follow_symlinks: bool = True,
                  errors: str = "replace") -> TextIO:
    """Open a regular file as UTF-8 text without ever blocking on a FIFO or device.

    FileNotFoundError if path is missing or is not a regular file (directory,
    FIFO, socket, device, or a symlink when follow_symlinks is False).
    """
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY
    if not follow_symlinks:
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise _not_regular(path) from None
        raise
    try:
        if stat.S_ISREG(os.fstat(fd).st_mode):
            return open(fd, encoding="utf-8", errors=errors)
    except BaseException:
        os.close(fd)
        raise
    os.close(fd)
    raise _not_regular(path)


def _is_regular_file(path: Path) -> bool:
    """True for a regular file that is not a symlink; False if it is anything else or gone."""
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def slugify(feature: str) -> str:
    """Kebab slug of up to 5 non-stopword words and 40 chars; 'job' if nothing is left."""
    ascii_text = unicodedata.normalize("NFKD", feature).encode("ascii", "ignore").decode("ascii")
    words = [w for w in re.sub(r"[^a-z0-9]+", " ", ascii_text.lower()).split() if w not in STOPWORDS]
    slug = ""
    for word in words[:SLUG_MAX_WORDS]:
        candidate = f"{slug}-{word}" if slug else word
        if len(candidate) > SLUG_MAX_CHARS:
            break
        slug = candidate
    if not slug and words:
        slug = words[0][:SLUG_MAX_CHARS]
    return slug or "job"


def _strip_blank(line: str) -> str:
    return line.strip().strip(_BOM).strip()


def _normalise_verdict_line(line: str) -> str:
    """Strip whitespace, BOM, leading '#'s and wrapping '*', '_' and '`' from a line."""
    text = _strip_blank(line)
    while True:
        stripped = text.lstrip("#").strip().strip("*_`").strip()
        if stripped == text:
            return text
        text = stripped


def _first_non_empty_line(fh: TextIO) -> str | None:
    """The first non-blank line within VERDICT_SCAN_LIMIT chars, or None (also if overlong)."""
    scanned = 0
    continued = False  # the previous chunk ended mid-line
    while scanned < VERDICT_SCAN_LIMIT:
        chunk = fh.readline(VERDICT_LINE_LIMIT)
        if not chunk:
            return None
        scanned += len(chunk)
        ends_line = chunk.endswith("\n") or len(chunk) < VERDICT_LINE_LIMIT
        if _strip_blank(chunk):
            return chunk if ends_line and not continued else None
        continued = not ends_line
    return None


def read_verdict(path: str | os.PathLike, kind: str) -> str | None:
    """The verdict token on the first non-empty line of path, or None if missing/empty/malformed.

    kind is 'review', 'build' or 'decision' (see DESIGN.md section 5). Anything
    but a regular file, and a first line of VERDICT_LINE_LIMIT chars or more,
    count as malformed; reads are bounded and never block.
    """
    allowed = VERDICTS.get(kind)
    if allowed is None:
        raise ValueError(f"unknown verdict kind {kind!r}")
    try:
        with _open_regular(path) as fh:
            line = _first_non_empty_line(fh)
    except OSError:
        return None
    return None if line is None else allowed.get(_normalise_verdict_line(line))


def _parse_backlog_line(line: str) -> dict | None:
    match = _BACKLOG_ITEM_RE.match(line)
    if not match:
        return None
    done = match.group(1) != " "
    text = match.group(2)
    job = None
    if done:
        tagged = _BACKLOG_JOB_RE.match(text)
        if tagged and (tagged.group(2) == CLAIMED or JOB_ID_RE.fullmatch(tagged.group(2))):
            text, job = tagged.group(1), tagged.group(2)
    if not text:
        return None
    return {"text": text, "done": done, "job": job}


def _parse_backlog(content: str) -> list[dict]:
    items = (_parse_backlog_line(line) for line in content.splitlines())
    return [item for item in items if item is not None]


def job_port(number: int, in_use: set[int] | frozenset[int]) -> int:
    """The first of a job's JOB_PORTS ports: BASE_PORT + PORT_STRIDE * number, the block every job number has; when
    something on this machine already listens on one of its ports (a server's own web app, say), the same number's
    block above OVERFLOW_PORT, else the first free spare block from SPARE_PORT up to LAST_PORT. Neither the numbered
    blocks nor the overflow blocks of other job numbers are ever taken, so a moved job cannot land on another job's
    ports, even one whose preview server is not listening right now. Two moved jobs can share a spare block only
    when both their numbered and overflow blocks are taken and the first one's servers were down when the second
    was made. When nothing is free (or in_use is unknown, empty), the numbered block."""
    def free(first: int) -> bool:
        return first + JOB_PORTS - 1 <= LAST_PORT and not any(first + i in in_use for i in range(JOB_PORTS))
    natural = BASE_PORT + PORT_STRIDE * number
    if free(natural):
        return natural
    overflow = OVERFLOW_PORT + PORT_STRIDE * number
    if free(overflow):
        return overflow
    candidate = SPARE_PORT
    while candidate + JOB_PORTS - 1 <= LAST_PORT:
        if free(candidate):
            return candidate
        candidate += PORT_STRIDE
    return natural


class JobStore:
    """Job folders under <data_dir>/jobs/<repo>/<job-id>/ and worktree paths under <data_dir>/worktrees/."""

    def __init__(self, data_dir: str | os.PathLike,
                 ports_in_use: Callable[[], set[int]] = listeners.ports_in_use) -> None:
        self.data_dir = Path(os.path.abspath(os.path.expanduser(data_dir)))
        self.jobs_root = self.data_dir / "jobs"
        self.worktrees_root = self.data_dir / "worktrees"
        self.ports_in_use = ports_in_use   # what listens here now (listeners.py), read when a job is made

    # -- paths ---------------------------------------------------------------

    def repo_dir(self, repo: str) -> Path:
        """<data_dir>/jobs/<repo>; ValueError for an invalid repo name."""
        if not isinstance(repo, str) or not REPO_NAME_RE.fullmatch(repo):
            raise ValueError(f"invalid repo name {repo!r}")
        return self.jobs_root / repo

    def job_dir(self, repo: str, job_id: str) -> Path:
        """<data_dir>/jobs/<repo>/<job-id>; ValueError for an invalid repo or job id."""
        root = self.repo_dir(repo)
        if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
            raise ValueError(f"invalid job id {job_id!r}")
        return root / job_id

    def worktree_path(self, repo: str, job_id: str) -> Path:
        """<data_dir>/worktrees/<repo>/<job-id>; ValueError for an invalid repo or job id."""
        self.job_dir(repo, job_id)  # validates both names
        return self.worktrees_root / repo / job_id

    def _existing_job_dir(self, repo: str, job_id: str) -> Path:
        """The job folder; KeyError if the names are invalid or the folder does not exist."""
        try:
            job_dir = self.job_dir(repo, job_id)
        except ValueError:
            raise KeyError(f"{repo}/{job_id}") from None
        if not job_dir.is_dir():
            raise KeyError(f"{repo}/{job_id}")
        return job_dir

    # -- creation ------------------------------------------------------------

    def allocate(self, repo: str, feature: str) -> tuple[str, Path]:
        """Claim the next free job id for repo by creating its folder; returns (job_id, job_dir).

        Safe across threads and processes (e.g. `factory run` beside `factory serve`).
        """
        root = self.repo_dir(repo)
        slug = slugify(feature)
        with _allocation_lock(root):
            number = self._highest_job_number(root) + 1
            while number <= MAX_JOB_NUMBER:
                job_id = f"{number:03d}-{slug}"
                path = root / job_id
                try:
                    os.mkdir(path)
                except FileExistsError:
                    number += 1
                    continue
                return job_id, path
        raise RuntimeError(f"no job numbers left for repo {repo!r}")

    @staticmethod
    def _highest_job_number(root: Path) -> int:
        highest = 0
        with os.scandir(root) as entries:
            for entry in entries:
                match = _JOB_NUMBER_RE.match(entry.name)
                if match and entry.is_dir():
                    highest = max(highest, int(match.group(1)))
        return highest

    def new_job(self, repo: str, feature: str, reviewers: list[str], max_resumes: int,
                base: str | None = None) -> dict:
        """Allocate a job, write feature.md (verbatim) and job.json, and return the job dict."""
        if not isinstance(feature, str) or not feature.strip():
            raise ValueError("feature must be a non-empty string")
        job_id, job_dir = self.allocate(repo, feature)
        number = int(job_id[:3])
        now = utc_now()
        job = {
            "id": job_id,
            "repo": repo,
            "feature": feature,
            "base": base,
            "base_commit": None,
            "branch": f"factory/{job_id}",
            "worktree": str(self.worktree_path(repo, job_id)),
            "job_dir": str(job_dir),
            "port": job_port(number, self.ports_in_use()),
            "stage": "queued",
            "detail": "",
            "round": 0,
            "resumes": 0,
            "max_resumes": max_resumes,
            "reviewers": list(reviewers),
            "reviews": {reviewer: "pending" for reviewer in reviewers},
            "history": [],
            "decision": None,
            "reason": None,
            "agents": {},
            "usage": {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "calls": 0},
            "merged_commit": None,
            "error": None,
            "failed_step": None,
            "from_branch": None,
            "items": empty_summary(),
            "created_at": now,
            "updated_at": now,
        }
        _atomic_write(job_dir / "feature.md", feature)
        return self.save(job)

    # -- job.json ------------------------------------------------------------

    def load(self, repo: str, job_id: str) -> dict:
        """Read job.json; KeyError if the job does not exist, ValueError if it is malformed."""
        path = self._existing_job_dir(repo, job_id) / "job.json"
        try:
            with _open_regular(path, errors="strict") as fh:
                text = fh.read()
        except (FileNotFoundError, NotADirectoryError):
            raise KeyError(f"{repo}/{job_id}") from None
        try:
            job = json.loads(text)
        except ValueError as exc:
            raise ValueError(f"malformed {path}: {exc}") from None
        if not isinstance(job, dict):
            raise ValueError(f"malformed {path}: not a JSON object")
        return job

    def save(self, job: dict) -> dict:
        """Set updated_at and atomically write the whole job to its job.json; returns job."""
        job_dir = self.job_dir(job["repo"], job["id"])
        with _lock_for("job", job_dir):
            job["updated_at"] = utc_now()
            text = json.dumps(job, indent=2, ensure_ascii=False) + "\n"
            _atomic_write(job_dir / "job.json", text)
        return job

    def list(self, repo: str | None = None) -> list[dict]:
        """All jobs (or one repo's), sorted by repo then id; unreadable job.json → failed placeholder."""
        if repo is not None:
            repos = [repo] if self.repo_dir(repo).is_dir() else []
        elif self.jobs_root.is_dir():
            repos = sorted(p.name for p in self.jobs_root.iterdir()
                           if p.is_dir() and REPO_NAME_RE.fullmatch(p.name))
        else:
            repos = []
        jobs = []
        for name in repos:
            for job_dir in sorted(self.repo_dir(name).iterdir()):
                if not JOB_ID_RE.fullmatch(job_dir.name) or not (job_dir / "job.json").is_file():
                    continue
                try:
                    jobs.append(self.load(name, job_dir.name))
                except KeyError:
                    continue  # removed while listing
                except (ValueError, OSError):
                    jobs.append({"id": job_dir.name, "repo": name, "stage": "failed",
                                 "reason": "unreadable job.json"})
        return jobs

    # -- job-folder files ----------------------------------------------------

    @staticmethod
    def _contained(job_dir: Path, rel: str) -> Path:
        """Resolve rel inside job_dir, rejecting absolute paths, '..' and symlink escapes."""
        if not isinstance(rel, str) or not rel or "\0" in rel:
            raise ValueError(f"invalid path {rel!r}")
        parts = PurePosixPath(rel)
        if parts.is_absolute() or os.path.isabs(rel) or ".." in parts.parts:
            raise ValueError(f"path must be relative and inside the job folder: {rel!r}")
        root = job_dir.resolve()
        try:
            target = (root / rel).resolve()
        except RuntimeError:  # symlink loop (Python < 3.13)
            raise ValueError(f"cannot resolve path {rel!r}") from None
        if root not in target.parents:
            raise ValueError(f"path escapes the job folder: {rel!r}")
        return target

    def files(self, repo: str, job_id: str) -> list[str]:
        """Sorted POSIX paths, relative to the job folder, of its regular files except sessions/."""
        job_dir = self._existing_job_dir(repo, job_id)
        found = []
        for dirpath, dirnames, filenames in os.walk(job_dir):
            base = Path(dirpath)
            if base == job_dir and "sessions" in dirnames:
                dirnames.remove("sessions")
            for name in filenames:
                path = base / name
                if not _is_temp_file(name) and _is_regular_file(path):
                    found.append(path.relative_to(job_dir).as_posix())
        return sorted(found)

    def read_file(self, repo: str, job_id: str, rel: str) -> str:
        """Text of a regular file in the job folder.

        FileNotFoundError if it is absent or not a regular file (directory, FIFO, ...),
        ValueError if rel is invalid or leads outside the folder, KeyError if the job is unknown.
        """
        target = self._contained(self._existing_job_dir(repo, job_id), rel)
        with _open_regular(target, follow_symlinks=False) as fh:
            return fh.read()

    def write_file(self, job: dict, rel: str, text: str) -> Path:
        """Atomically write text to rel inside the job's folder (parents created); returns the path."""
        target = self._contained(self._existing_job_dir(job["repo"], job["id"]), rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, text)
        return target

    def next_rework_index(self, job: dict) -> int:
        """1 + the highest k of existing rework-<k>.md files (1 when there are none)."""
        job_dir = self._existing_job_dir(job["repo"], job["id"])
        numbers = [int(m.group(1)) for m in map(_REWORK_RE.fullmatch, os.listdir(job_dir)) if m]
        return max(numbers, default=0) + 1

    def next_log_path(self, job: dict, agent: str) -> Path:
        """Create and return logs/NN-<agent>.jsonl with the next run number (01, 02, ...)."""
        if not isinstance(agent, str) or not AGENT_NAME_RE.fullmatch(agent):
            raise ValueError(f"invalid agent name {agent!r}")
        logs = self._existing_job_dir(job["repo"], job["id"]) / "logs"
        with _lock_for("logs", logs):
            logs.mkdir(exist_ok=True)
            numbers = [int(m.group(1)) for m in map(_LOG_NUMBER_RE.fullmatch, os.listdir(logs)) if m]
            number = max(numbers, default=0) + 1
            while True:
                path = logs / f"{number:02d}-{agent}.jsonl"
                try:
                    with open(path, "x", encoding="utf-8"):
                        return path
                except FileExistsError:
                    number += 1

    # -- follow-ups and the sweep's suppress list (DESIGN.md section 32) ------

    def followups_dir(self, repo: str) -> Path:
        """Where the repo's follow-ups live: followups.json and followups.log.jsonl in <data_dir>/jobs/<repo>/."""
        return self.repo_dir(repo)

    def sweep_suppressed(self, repo: str) -> set[str]:
        """The text hashes a person marked "not a promise" in this repo (sweep-suppress.json)."""
        try:
            data = json.loads((self.repo_dir(repo) / SUPPRESS_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
        hashes = data.get("hashes") if isinstance(data, dict) else None
        return {h for h in hashes if isinstance(h, str)} if isinstance(hashes, list) else set()

    def suppress_sweep(self, repo: str, digest: str) -> None:
        """Add a text hash to the repo's sweep-suppress.json (under the repo lock)."""
        path = self.repo_dir(repo) / SUPPRESS_FILE
        with _lock_for("repo", path.parent):
            hashes = self.sweep_suppressed(repo)
            if digest in hashes:
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(path, json.dumps({"hashes": sorted(hashes | {digest})}, indent=1) + "\n")

    # -- backlog -------------------------------------------------------------

    def _backlog_path(self, repo: str) -> Path:
        return self.repo_dir(repo) / BACKLOG_FILE

    def _read_backlog_text(self, repo: str) -> str:
        try:
            return self._backlog_path(repo).read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def backlog(self, repo: str) -> list[dict]:
        """Backlog items [{text, done, job}] in file order; [] when there is no backlog.md."""
        return _parse_backlog(self._read_backlog_text(repo))

    def add_backlog(self, repo: str, text: str) -> list[dict]:
        """Append '- [ ] text' (one line, stripped, non-empty) and return the updated items."""
        item = text.strip() if isinstance(text, str) else ""
        if not item or item.splitlines() != [item]:
            raise ValueError("backlog item must be a single non-empty line")
        path = self._backlog_path(repo)
        with _lock_for("repo", path.parent):
            path.parent.mkdir(parents=True, exist_ok=True)
            content = self._read_backlog_text(repo) or BACKLOG_HEADER
            if not content.endswith("\n"):
                content += "\n"
            content += f"- [ ] {item}\n"
            _atomic_write(path, content)
        return _parse_backlog(content)

    def _rewrite_first(self, repo: str, wanted: Callable[[dict], bool],
                       replacement: Callable[[dict], str]) -> dict | None:
        """Under the repo lock, replace the first backlog line whose item satisfies wanted(item)."""
        path = self._backlog_path(repo)
        with _lock_for("repo", path.parent):
            lines = self._read_backlog_text(repo).splitlines()
            for index, line in enumerate(lines):
                item = _parse_backlog_line(line)
                if item is not None and wanted(item):
                    lines[index] = replacement(item)
                    _atomic_write(path, "\n".join(lines) + "\n")
                    return item
        return None

    def claim_next(self, repo: str) -> str | None:
        """Mark the first unchecked item '- [x] text → claimed' and return its text (None if none)."""
        item = self._rewrite_first(repo, lambda it: not it["done"],
                                   lambda it: f"- [x] {it['text']} → {CLAIMED}")
        return item["text"] if item else None

    def assign_backlog(self, repo: str, text: str, job_id: str) -> bool:
        """Replace the first '- [x] text → claimed' with the job id; False if no such line."""
        if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
            raise ValueError(f"invalid job id {job_id!r}")
        wanted_text = text.strip()
        item = self._rewrite_first(
            repo,
            lambda it: it["done"] and it["job"] == CLAIMED and it["text"] == wanted_text,
            lambda it: f"- [x] {it['text']} → {job_id}",
        )
        return item is not None

    def unclaim(self, repo: str, text: str) -> bool:
        """Turn the first '- [x] text → claimed' back into '- [ ] text' (under the repo lock), for an
        item whose job could not be created; False if no such line."""
        wanted_text = text.strip() if isinstance(text, str) else ""
        item = self._rewrite_first(
            repo,
            lambda it: it["done"] and it["job"] == CLAIMED and it["text"] == wanted_text,
            lambda it: f"- [ ] {it['text']}",
        )
        return item is not None
