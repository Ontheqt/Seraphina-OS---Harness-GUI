"""Error capture and the capture bundle (DESIGN.md section 39).

When a provider or tool call fails, the run log keeps what a server's own logs can be matched against
(``agent.error``, built by error_record and tool_error_record): the full status and response body (redacted,
at most CAPTURE_BODY_CHARS), exact timestamps (UTC, milliseconds) and any request ids the answer carried. A
persona chat backed by Open WebUI also records the workspace model id and the turn's time window
(``persona.window``).

``factory capture --since --until [--container NAME ...] --out DIR`` (bundle) is read-only: it copies the
harness's own run logs for that window, only their diagnostic events (DIAGNOSTIC_TYPES: run starts and ends,
errors, waits, log lines, persona turn windows; never transcript text, reasoning, tool input or output, job
requests, persona memory or messages: a failed tool call's error record is copied without its output, which
the bundle names only by its length and sha256) and, where this user may run ``docker logs``, those
containers' logs.
Everything is redacted (the harness's known secrets and the Blueprint text rules) and nothing is written
anywhere but DIR, which must be new or empty. "Capture before fix": take the bundle before anything is
restarted, edited or retried, so the evidence of a failure is not overwritten by the fix.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from .blueprint.redact import Redactor

CAPTURE_BODY_CHARS = 16384      # the response body an error record keeps (the error message itself stays short)
TOOL_OUTPUT_CHARS = 16384       # a failed tool call's output kept in its error record
REQUEST_ID_CHARS = 200
REQUEST_ID_RE = re.compile(r"^(?:x-)?(?:request|req|correlation|trace|amzn-?request|amz-request)[-_]?id$|^cf-ray$"
                           r"|^traceparent$|^x-amzn-trace-id$", re.IGNORECASE)
BODY_ID_KEYS = ("request_id", "requestId", "trace_id", "traceId", "correlation_id")
# What a capture bundle copies from the run logs: diagnostic events only, never what a person or a model wrote.
DIAGNOSTIC_TYPES = frozenset({"agent.start", "agent.end", "agent.error", "agent.waiting", "log", "persona.turn",
                              "persona.window"})
LOG_ROOTS = ("jobs", "chats", "plans")   # <data_dir>/<root>/**/logs/*.jsonl; wakes/ and notify/ are never read
CONTAINER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
DOCKER_TIMEOUT = 60.0
CONTAINER_LOG_BYTES = 16 << 20
MAX_LOG_LINE_BYTES = 1 << 20
_RELATIVE_RE = re.compile(r"^(\d+)\s*([smhd])$")


# ---------------------------------------------------------------- error records


def stamp(epoch: float | None = None) -> str:
    """A UTC time with milliseconds and a Z: exact enough to find the same moment in a server's own log."""
    when = datetime.fromtimestamp(time.time() if epoch is None else epoch, timezone.utc)
    return when.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def request_ids(headers: Any) -> dict[str, str]:
    """The request-id-like headers of an answer (x-request-id, request-id, cf-ray, traceparent, ...)."""
    found: dict[str, str] = {}
    items = headers.items() if hasattr(headers, "items") else headers or []
    for name, value in items:
        if REQUEST_ID_RE.match(str(name)) and value:
            found[str(name).lower()] = str(value)[:REQUEST_ID_CHARS]
    return found


def body_ids(text: str) -> dict[str, str]:
    """Request ids inside a JSON error body ({"request_id": ...} or {"error": {"request_id": ...}})."""
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return {}
    found: dict[str, str] = {}
    for holder in (payload, payload.get("error") if isinstance(payload, dict) else None):
        if isinstance(holder, dict):
            for key in BODY_ID_KEYS:
                value = holder.get(key)
                if isinstance(value, (str, int)) and str(value):
                    found[f"body.{key}"] = str(value)[:REQUEST_ID_CHARS]
    return found


def scrub(text: str, secrets: Iterable[str] = ()) -> str:
    """text with the harness's known secrets and secret-looking values (Bearer tokens, key=value pairs, JWTs,
    private keys, URL passwords) removed: the Blueprint redaction rules."""
    return Redactor(known=secrets).text(text or "")


def clip(text: str, limit: int) -> tuple[str, bool]:
    return (text, False) if len(text) <= limit else (text[:limit], True)


def error_record(*, provider: str, model_id: str, url: str, attempt: int, will_retry: bool, status: int | None,
                 message: str, body: str = "", headers: Any = None, started: float | None = None,
                 failed: float | None = None, connection: bool = False, secrets: Iterable[str] = (),
                 http_status: int | None = None) -> dict:
    """agent.error for one failed model call attempt: {kind: provider, provider, model_id, url, attempt,
    will_retry, status, http_status, error, body, body_truncated, request_ids, started_at, failed_at, elapsed_ms,
    connection}. status is the failure's code (an error payload's, inside a 200 answer); http_status the answer's
    own HTTP status (None without an answer). url has no query string; body is the full response body as read,
    redacted and capped. connection: no answer, or one cut off before it was whole."""
    secrets = list(secrets)
    clean, truncated = clip(scrub(body or "", secrets), CAPTURE_BODY_CHARS)
    ids = {**request_ids(headers), **body_ids(body or "")}
    failed = time.time() if failed is None else failed
    return {"kind": "provider", "provider": provider, "model_id": model_id, "url": url.split("?", 1)[0],
            "attempt": attempt, "will_retry": will_retry, "status": status, "http_status": http_status,
            "error": scrub(message, secrets)[:2000], "body": clean, "body_truncated": truncated,
            "request_ids": ids, "started_at": stamp(started) if started is not None else None,
            "failed_at": stamp(failed),
            "elapsed_ms": int(round((failed - started) * 1000)) if started is not None else None,
            "connection": connection}


def tool_error_record(*, name: str, call_id: str, output: str, started: float, failed: float,
                      secrets: Iterable[str] = ()) -> dict:
    """agent.error for a failed tool call: {kind: tool, name, id, output, output_truncated, started_at,
    failed_at, elapsed_ms}; output (an MCP server's error, a command's exit status and output) redacted and
    capped."""
    clean, truncated = clip(scrub(output or "", list(secrets)), TOOL_OUTPUT_CHARS)
    return {"kind": "tool", "name": name, "id": call_id, "output": clean, "output_truncated": truncated,
            "started_at": stamp(started), "failed_at": stamp(failed),
            "elapsed_ms": int(round((failed - started) * 1000))}


# ---------------------------------------------------------------- the capture bundle


class CaptureError(ValueError):
    """The capture cannot run as asked (a bad time, a bad container name, an --out that is not new or empty)."""


def parse_when(text: str, now: datetime | None = None) -> datetime:
    """A time for --since and --until, in UTC: "now", a time ago ("15m", "2h", "1d", "90s"), or ISO-8601 (with
    a zone or Z; without one it is this machine's local time)."""
    now = now or datetime.now(timezone.utc)
    value = str(text or "").strip()
    if value.lower() == "now":
        return now
    match = _RELATIVE_RE.match(value.lower())
    if match:
        unit = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[match.group(2)]
        return now - timedelta(**{unit: int(match.group(1))})
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        raise CaptureError(f"{text!r} is not a time: use now, 15m, 2h, 1d or an ISO-8601 time such as "
                           f"2026-10-22T09:30:00Z") from None
    if when.tzinfo is None:
        when = when.astimezone()   # this machine's local time
    return when.astimezone(timezone.utc)


def _event_time(line: dict) -> datetime | None:
    data = line.get("data") if isinstance(line.get("data"), dict) else {}
    for value in (data.get("failed_at"), line.get("ts"), line.get("at")):
        if isinstance(value, str):
            try:
                when = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                continue
            return when if when.tzinfo else when.replace(tzinfo=timezone.utc)
    return None


def log_files(data_dir: Path) -> Iterator[Path]:
    """The harness's own run logs: every <data_dir>/{jobs,chats,plans}/**/logs/*.jsonl (never wakes/ or
    notify/: Wake & Messages data is personal), sorted."""
    for root in LOG_ROOTS:
        base = Path(data_dir) / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.jsonl")):
            if path.parent.name == "logs" and path.is_file() and not path.is_symlink():
                yield path


def harness_lines(path: Path, since: datetime, until: datetime) -> Iterator[dict]:
    """The diagnostic events of one run log inside [since, until]."""
    try:
        handle = open(path, "rb")
    except OSError:
        return
    with handle:
        for raw in handle:
            if len(raw) > MAX_LOG_LINE_BYTES:
                continue
            try:
                line = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue
            if not isinstance(line, dict) or line.get("type") not in DIAGNOSTIC_TYPES:
                continue
            when = _event_time(line)
            if when is not None and since <= when <= until:
                yield line


def _diagnostic(line: dict) -> dict:
    """line as a bundle keeps it: a failed tool call's agent.error without its output (a command's output is
    what a person or a model wrote: only its length and sha256 are kept, to match against the run log)."""
    data = line.get("data")
    if line.get("type") != "agent.error" or not isinstance(data, dict) or data.get("kind") != "tool":
        return line
    output = data.get("output")
    kept = {key: value for key, value in data.items() if key != "output"}
    if isinstance(output, str):
        kept.update(output_chars=len(output), output_sha256=hashlib.sha256(output.encode("utf-8")).hexdigest())
    return {**line, "data": kept}


def _redacted(value: Any, redactor: Redactor) -> Any:
    if isinstance(value, str):
        return redactor.text(value)
    if isinstance(value, dict):
        return {key: _redacted(item, redactor) for key, item in value.items()}
    if isinstance(value, list):
        return [_redacted(item, redactor) for item in value]
    return value


class DockerLogs:
    """`docker logs --timestamps --since --until NAME` with no shell, stdin closed, a minimal environment (no
    secrets), a timeout and the output capped. run() -> (exit code or None, stdout+stderr, why it did not
    run); tests replace it."""

    def run(self, name: str, since: str, until: str) -> tuple[int | None, str, str]:
        docker = shutil.which("docker")
        if docker is None:
            return None, "", "docker is not installed (or not on PATH)"
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C"}
        for key in ("HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "XDG_RUNTIME_DIR"):
            if os.environ.get(key):
                env[key] = os.environ[key]
        try:
            done = subprocess.run([docker, "logs", "--timestamps", "--since", since, "--until", until, name],
                                  stdin=subprocess.DEVNULL, capture_output=True, timeout=DOCKER_TIMEOUT, env=env,
                                  cwd="/", check=False)
        except subprocess.TimeoutExpired:
            return None, "", f"docker logs {name} did not finish within {DOCKER_TIMEOUT:g}s"
        except OSError as exc:
            return None, "", f"docker could not run: {exc.strerror or type(exc).__name__}"
        out = (done.stdout or b"")[:CONTAINER_LOG_BYTES] + (done.stderr or b"")[:CONTAINER_LOG_BYTES]
        return done.returncode, out.decode("utf-8", "replace"), ""


def _new_out(out: Path) -> Path:
    """out, created 0700 (and so is every missing parent the capture creates), or CaptureError when it exists
    and is not an empty directory (a capture never mixes with or overwrites another)."""
    out = Path(os.path.expanduser(str(out)))
    if out.is_symlink():
        raise CaptureError(f"{out} is a symbolic link: give a new directory")
    if out.exists():
        if not out.is_dir():
            raise CaptureError(f"{out} exists and is not a directory")
        if any(out.iterdir()):
            raise CaptureError(f"{out} is not empty: give a new directory (a capture never overwrites another)")
    missing = []
    for parent in [out, *out.parents]:
        if parent.exists() or parent.is_symlink():
            break
        missing.append(parent)
    for folder in reversed(missing):   # outermost first, each one 0700 whatever the umask
        folder.mkdir(mode=0o700, exist_ok=True)
        os.chmod(folder, 0o700)
    out.mkdir(mode=0o700, exist_ok=True)
    os.chmod(out, 0o700)
    return out


def _write_new(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def bundle(data_dir: Path, out: Path, since: datetime, until: datetime, containers: list[str] | None = None,
           secrets: Iterable[str] = (), docker: DockerLogs | None = None,
           now: Callable[[], datetime] | None = None) -> dict:
    """Write the capture bundle into out (new or empty): harness/<path under data_dir> with each run log's
    diagnostic events in the window, containers/<name>.log with each container's logs, and manifest.json
    (what was taken, what was skipped and why, and the redaction counts). Returns the manifest."""
    if until < since:
        raise CaptureError("--until is before --since")
    names = list(dict.fromkeys(containers or []))
    for name in names:
        if not CONTAINER_RE.fullmatch(name):
            raise CaptureError(f"{name!r} is not a container name")
    out = _new_out(out)
    redactor = Redactor(known=[value for value in secrets if isinstance(value, str)])
    window = {"since": stamp(since.timestamp()), "until": stamp(until.timestamp())}
    manifest: dict[str, Any] = {"version": 1, "created_at": stamp((now() if now else datetime.now(timezone.utc))
                                                                  .timestamp()),
                                "window": window, "harness": [], "containers": [],
                                "kept": sorted(DIAGNOSTIC_TYPES),
                                "never": "transcript text, reasoning, tool input and output (a failed tool call's "
                                         "error keeps only the output's length and sha256), job requests, persona "
                                         "memory, threads and messages (wakes/ and notify/ are never read)"}
    data_dir = Path(data_dir)
    for path in log_files(data_dir):
        lines = [json.dumps(_redacted(_diagnostic(line), redactor), ensure_ascii=False)
                 for line in harness_lines(path, since, until)]
        if not lines:
            continue
        relative = path.relative_to(data_dir)
        _write_new(out / "harness" / relative, "\n".join(lines) + "\n")
        manifest["harness"].append({"file": str(Path("harness") / relative), "events": len(lines)})
    runner = docker or DockerLogs()
    for name in names:
        code, text, problem = runner.run(name, window["since"], window["until"])
        entry: dict[str, Any] = {"name": name}
        if problem:
            entry.update(taken=False, why=problem)
        elif code != 0:
            first = redactor.text(text.strip().splitlines()[0] if text.strip() else f"exit status {code}")[:300]
            denied = "permission denied" in text.lower()
            entry.update(taken=False, why=("this user may not run docker logs (" + first + ")") if denied
                         else f"docker logs failed: {first}")
        else:
            body = redactor.text(text)
            _write_new(out / "containers" / f"{name}.log", body)
            entry.update(taken=True, file=f"containers/{name}.log", lines=body.count("\n"))
        manifest["containers"].append(entry)
    manifest["redaction"] = {key: count for key, count in redactor.counts.items() if count}
    manifest["review"] = "Redaction is pattern-based, not proof: read the bundle before you share it."
    _write_new(out / "manifest.json", json.dumps(manifest, indent=1, ensure_ascii=False) + "\n")
    return manifest

