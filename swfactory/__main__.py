"""The `factory` command line (DESIGN.md section 16; `factory --help` lists every command).

Commands that act on jobs, backlogs and plans talk to the running server over
HTTP with the config's token. init, repo, run, logs, doctor, agents, models,
personas (listing and cards), snapshot and url also work without it. Output is plain text: data on stdout,
hints and errors on stderr, and `--json` where a listing is useful to scripts.
"""

# The version check must run before any import that could fail on an old Python.
# ruff: noqa: E402

from __future__ import annotations

import sys

if sys.version_info < (3, 10):
    sys.stderr.write("factory: Software Factory needs Python 3.10 or newer; this is Python %d.%d.\n"
                     "Ubuntu 22.04 and later ship it; FACTORY_PYTHON picks another interpreter.\n"
                     % sys.version_info[:2])
    sys.exit(1)

if __name__ == "__main__" and not (sys.flags.isolated or getattr(sys.flags, "safe_path", False)):
    # `python -m` put the working directory first on sys.path. Drop it, so a file in
    # a checkout (say an agent-written json.py) cannot shadow a module imported below.
    del sys.path[0]

import argparse
import contextlib
import errno
import fcntl
import getpass
import http.client
import json
import os
import queue
import re
import signal
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, TextIO

from . import __version__
from .agentdefs import AgentDef, read_small_text
from .config import MAX_KEY_FILE_BYTES, Config, ModelConfig, default_config_path, init_config
from .jobs import JOB_ID_RE, REPO_NAME_RE, JobStore

NOT_RUNNING = "server not running — start it with: factory serve"
PRESETS_HELP = ("deepseek (default config), anthropic, nanogpt, nanogpt-value, ollama, strata, hybrid, local-first, "
                "mixed, gpt-oss")
ENV_FILE = "env"
LOCK_FILE = "factory.lock"
LOCK_FILE_MAX_BYTES = 4096
SERVER_URL_RE = re.compile(r"^http://(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9._-]+):\d{1,5}$", re.ASCII)
JOB_EVENTS = ("job.created", "job.updated")
WILDCARD_HOSTS = ("", "0.0.0.0", "::", "*")
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
STOP_STAGES = ("merged", "delivered", "needs-human", "failed", "rejected", "cancelled")
TERMINAL_STAGES = ("merged", "rejected", "cancelled")   # a delivered job may still be approved (merged)
VERDICTS = ("PASS", "CHANGES", "ERROR")
RUN_EXIT_CODES = {"merged": 0, "delivered": 0, "needs-human": 2}
DELIVERY_HELP = "merge (default: merge.delivery), branch (keep the branch) or folder (export to the output dir)"
PROFILE_HELP = 'a routing profile from the config\'s profiles (default: default_profile; "" for none)'
LABEL_WIDTH = 17         # fits reviewer-security
INDENT = " " * (8 + 2 + LABEL_WIDTH + 2)
HTTP_TIMEOUT = 30.0
SSE_TIMEOUT = 90.0       # the server pings every 15 s
POLL_SECONDS = 2.0


class CliError(Exception):
    """A failure to report as one line on stderr, with exit status 1."""


class ServerNotRunning(CliError):
    """Nothing listens on the server's port."""


# -- config, env file, lock -----------------------------------------------------


def load_config() -> Config:
    """The config at $FACTORY_CONFIG or ~/.config/software-factory/config.json."""
    try:
        return Config.load()
    except ValueError as exc:
        raise CliError(str(exc)) from None


def require_token(config: Config) -> str:
    """The server token, or a CliError telling the user to run `factory init`."""
    token = _server_settings(config).get("token")
    if not token:
        raise CliError(f"no server token in {config.path}: run factory init first")
    return str(token)


def load_env_file(path: Path) -> None:
    """Export the NAME=value lines of the factory's env file (API keys); set variables win."""
    try:
        text = read_small_text(path, MAX_KEY_FILE_BYTES)
        mode = os.stat(path).st_mode
    except FileNotFoundError:
        return
    except (OSError, UnicodeDecodeError) as exc:
        warn(f"cannot read {path}: {exc}")
        return
    if mode & 0o077:
        warn(f"{path} holds API keys but other users can read it: chmod 600 {path}")
    from .envfile import parse_env_text   # the one parser of this file (the GUI's secrets endpoint writes it)

    for name, value in parse_env_text(text):
        os.environ.setdefault(name, value)


@contextlib.contextmanager
def factory_lock(data_dir: Path, url: str | None = None) -> Iterator[Path]:
    """Hold <data_dir>/factory.lock (fcntl.flock) for the block; CliError if another process holds it.

    While held, the file holds this process's pid and, for `factory serve`, the URL clients
    reach it at (so CLI commands find a server started with --host or --port). It is emptied
    on release.
    """
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = data_dir / LOCK_FILE
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    locked = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = (os.pread(fd, 64, 0).decode("ascii", "replace").split() or ["unknown"])[0]
            raise CliError(f"another factory (pid {holder}) is already running on {data_dir} "
                           f"(lock {path})") from None
        locked = True
        os.ftruncate(fd, 0)
        os.pwrite(fd, (f"{os.getpid()}\n" + (f"{url}\n" if url else "")).encode("ascii", "replace"), 0)
        yield path
    finally:
        if locked:
            with contextlib.suppress(OSError):
                os.ftruncate(fd, 0)
        os.close(fd)


def recorded_server_url(data_dir: Path) -> str | None:
    """The URL a running `factory serve` recorded in <data_dir>/factory.lock, or None.

    None when there is no such file, it names no URL (a one-shot `factory run` holds the
    lock), or its process is gone (a server that was killed leaves the file behind).
    """
    try:
        fields = read_small_text(data_dir / LOCK_FILE, LOCK_FILE_MAX_BYTES).split()
    except (OSError, UnicodeDecodeError):
        return None
    if len(fields) < 2 or not fields[0].isdigit() or not SERVER_URL_RE.fullmatch(fields[1]):
        return None
    return fields[1] if _process_alive(int(fields[0])) else None


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False
    return True


@contextlib.contextmanager
def sigterm_as_interrupt() -> Iterator[None]:
    """Turn SIGTERM (systemd stop) into KeyboardInterrupt, so shutdown runs the cleanup code."""
    previous = signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


# -- talking to the server --------------------------------------------------------


def server_url(config: Config) -> str:
    """Where the CLI reaches the server: the URL a running `factory serve` recorded in the data
    dir's lock file (it may run with --host or --port), else server.host and server.port."""
    settings = _server_settings(config)
    return recorded_server_url(config.data_dir) or connect_url(str(settings.get("host") or ""),
                                                               int(settings.get("port") or 8765))


def connect_url(host: str, port: int) -> str:
    """http://<host>:<port> for a client of a server bound to host; a wildcard address means 127.0.0.1."""
    host = host.strip()
    if host in WILDCARD_HOSTS:
        host = "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{port}"


class Client:
    """JSON over HTTP to the running server, with the config's token as a Bearer credential."""

    def __init__(self, config: Config) -> None:
        self.base_url = server_url(config)
        self._headers = {"Authorization": f"Bearer {require_token(config)}", "Accept": "application/json"}
        # The server is local: never route it through HTTP(S)_PROXY.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def get(self, path: str) -> Any:
        """GET path and return the decoded JSON body."""
        return self._json("GET", path, None)

    def post(self, path: str, body: dict | None = None) -> Any:
        """POST a JSON body to path and return the decoded JSON reply."""
        return self._json("POST", path, body or {})

    def events(self, since: int = 0) -> Iterator[dict]:
        """Events from /api/events (server-sent events) as dicts, until the server closes the stream."""
        response = self._open("GET", f"/api/events?since={since}", timeout=SSE_TIMEOUT)
        with contextlib.closing(response):
            try:
                yield from parse_sse(response)
            except (OSError, http.client.HTTPException) as exc:
                raise CliError(f"lost the event stream from {self.base_url}: {exc}") from None

    def _json(self, method: str, path: str, body: dict | None) -> Any:
        response = self._open(method, path, body)
        try:
            with response:
                payload = response.read()
        except (OSError, http.client.HTTPException) as exc:
            raise CliError(_no_answer(self.base_url, method, exc)) from None
        try:
            return json.loads(payload.decode("utf-8")) if payload else {}
        except ValueError:
            raise CliError(f"the server at {self.base_url} sent a reply that is not JSON") from None

    def _open(self, method: str, path: str, body: dict | None = None,
              timeout: float = HTTP_TIMEOUT) -> Any:
        headers = dict(self._headers)
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            return self._opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            raise CliError(_http_error(exc)) from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, ConnectionRefusedError):
                raise ServerNotRunning(NOT_RUNNING) from None
            raise CliError(f"cannot reach the server at {self.base_url}: {exc.reason}") from None
        except ConnectionRefusedError:
            raise ServerNotRunning(NOT_RUNNING) from None
        except (OSError, http.client.HTTPException) as exc:
            raise CliError(_no_answer(self.base_url, method, exc)) from None


def _no_answer(base_url: str, method: str, exc: BaseException) -> str:
    """The words for a request that was sent and got no answer: for anything but a read, it may have been done
    (DESIGN §40), so look before running it again."""
    words = f"no answer from the server at {base_url}: {exc or type(exc).__name__}"
    return words if method == "GET" else words + "; it may have been done, so look before you run it again"


def parse_sse(lines: Any) -> Iterator[dict]:
    """Events from a text/event-stream (an iterable of byte lines); comments and bad JSON are skipped.

    A data payload that is a whole event ({seq, type, data, ...}) is returned as is; any other
    payload is wrapped as the event's data, with type and seq from the event: and id: fields.
    """
    fields: dict[str, str] = {}
    data: list[str] = []
    for raw in lines:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if line.startswith(":"):
            continue
        if line:
            name, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if name == "data":
                data.append(value)
            else:
                fields[name] = value
            continue
        if data:
            try:
                payload = json.loads("\n".join(data))
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                yield payload if "type" in payload and "data" in payload else _wrap_event(payload, fields)
        fields, data = {}, []


def _wrap_event(payload: dict, fields: dict[str, str]) -> dict:
    seq = fields.get("id", "")
    return {"seq": int(seq) if seq.isdigit() else None, "ts": None, "type": fields.get("event", "message"),
            "repo": payload.get("repo"), "job": payload.get("job"), "agent": payload.get("agent"),
            "data": payload}


def _http_error(exc: urllib.error.HTTPError) -> str:
    try:
        message = json.loads(exc.read().decode("utf-8")).get("error")
    except (ValueError, AttributeError, OSError):
        message = None
    finally:
        exc.close()
    if exc.code == 401:
        return "the server refused the token in the config (was it re-initialised? restart the server)"
    return f"{message or exc.reason} (HTTP {exc.code})"


def _server_settings(config: Config) -> dict:
    settings = config.raw.get("server")
    return settings if isinstance(settings, dict) else {}


def _unwrap(payload: Any, key: str) -> Any:
    """payload[key] when the reply wraps the object (as in {"job": {...}}), else payload."""
    return payload.get(key, payload) if isinstance(payload, dict) else payload


def _quote(*parts: str) -> str:
    return "/".join(urllib.parse.quote(part, safe="") for part in parts)


def _reason(exc: Exception) -> str:
    """An exception's message (a KeyError's without the quotes str() adds)."""
    return str(exc.args[0]) if isinstance(exc, KeyError) and exc.args else str(exc)


# -- formatting -------------------------------------------------------------------


def warn(message: str) -> None:
    """One `factory: warning:` line on stderr."""
    print(f"factory: warning: {message}", file=sys.stderr)


def hint(message: str) -> None:
    """Human-oriented guidance on stderr (stdout stays parseable)."""
    print(message, file=sys.stderr)


def print_json(value: Any) -> None:
    """Pretty JSON on stdout."""
    print(json.dumps(value, indent=2, ensure_ascii=False))


def clock(ts: Any) -> str:
    """An event timestamp ("2026-10-20T22:30:00Z") as local HH:MM:SS."""
    try:
        moment = datetime.strptime(str(ts), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return "--:--:--"
    return moment.astimezone().strftime("%H:%M:%S")


def job_info(job: dict) -> str:
    """The most useful short fact about a job: merge commit, why it stopped, or its detail line."""
    stage = job.get("stage")
    if stage == "merged":
        return str(job.get("merged_commit") or "")[:12]
    if stage == "delivered":
        delivered = job.get("delivered") if isinstance(job.get("delivered"), dict) else {}
        return str(delivered.get("path") or delivered.get("branch") or job.get("detail") or "")
    if stage in ("needs-human", "failed", "rejected", "cancelled"):
        return str(job.get("reason") or job.get("error") or job.get("detail") or "")
    return str(job.get("detail") or "")


def job_line(job: dict, width: int = 0) -> str:
    """`repo/id  stage  r<round> <resumes>/<max>  info` for status listings."""
    ref = f"{job.get('repo')}/{job.get('id')}"
    rounds = f"r{job.get('round') or 0} {job.get('resumes') or 0}/{job.get('max_resumes') or 0}"
    return f"{ref:<{width}}  {str(job.get('stage') or '?'):<11}  {rounds:<7}  {job_info(job)}".rstrip()


def plan_line(plan: dict, width: int = 0) -> str:
    """`repo/id  stage  <merged>/<total> jobs  reason-or-request` for plan listings."""
    jobs = [job for job in plan.get("jobs") or [] if isinstance(job, dict)]
    merged = sum(1 for job in jobs if job.get("stage") == "merged")
    request = (str(plan.get("request") or "").strip().splitlines() or [""])[0]
    info = str(plan.get("reason") or "") or _clip(request, 70)
    ref = f"{plan.get('repo')}/{plan.get('id')}"
    return f"{ref:<{width}}  {str(plan.get('stage') or '?'):<12}  {merged}/{len(jobs)} jobs  {info}".rstrip()


def _ref_width(items: list[dict]) -> int:
    return max((len(f"{item.get('repo')}/{item.get('id')}") for item in items), default=0)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _tokens(count: Any) -> str:
    number = int(count or 0)
    return f"{number / 1000:.1f}k" if number >= 1000 else str(number)


def _event_job(event: dict) -> tuple[Any, Any]:
    """(repo, job id) an event belongs to (job.* events may only carry it inside data.job)."""
    job = (event.get("data") or {}).get("job")
    if isinstance(job, dict):
        return event.get("repo") or job.get("repo"), event.get("job") or job.get("id")
    return event.get("repo"), event.get("job")


class EventPrinter:
    """Prints factory events as one line each: a whole transcript (logs) or only transitions (run).

    Streamed assistant text and reasoning continue on the same line block until another
    event arrives; job.json snapshots become one line per stage change and verdict.
    """

    def __init__(self, out: TextIO, transcript: bool) -> None:
        self.out = out
        self.transcript = transcript
        self._jobs: dict[tuple[Any, Any], dict] = {}
        self._stream_key: tuple[Any, Any] | None = None
        self._held_newlines = 0

    def handle(self, event: dict) -> None:
        """Print whatever this event adds."""
        kind = str(event.get("type") or "")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if kind in ("agent.text", "agent.reasoning"):
            if self.transcript:
                self._stream(event, kind, data)
            return
        for label, message in self._messages(kind, event, data):
            self._end_stream()
            self.out.write(f"{clock(event.get('ts'))}  {label:<{LABEL_WIDTH}}  {message}".rstrip() + "\n")
        self.out.flush()

    def track(self, event: dict) -> None:
        """Take in a job.created/job.updated snapshot without printing, so later ones print only changes."""
        job = (event.get("data") or {}).get("job")
        if isinstance(job, dict):
            self._jobs[(job.get("repo"), job.get("id"))] = job

    def finish(self) -> None:
        """Close a streamed text block that is still open."""
        self._end_stream()
        self.out.flush()

    def _messages(self, kind: str, event: dict, data: dict) -> list[tuple[str, str]]:
        agent = str(event.get("agent") or data.get("agent") or "-")
        if kind in ("job.created", "job.updated"):
            job = data.get("job")
            return self._job_changes(job) if isinstance(job, dict) else []
        if kind == "agent.start":
            details = [str(data[key]) for key in ("driver", "model") if data.get(key)]
            if data.get("round"):
                details.append(f"round {data['round']}")
            return [(agent, "started" + (f" ({', '.join(details)})" if details else ""))]
        if kind == "agent.end":
            return [(agent, _end_message(data))]
        if kind == "log":
            level = str(data.get("level") or "info")
            if self.transcript or level in ("warn", "warning", "error"):
                return [("log", f"{level}: {data.get('message', '')}")]
            return []
        if not self.transcript:
            return []
        if kind == "agent.tool_call":
            arguments = json.dumps(data.get("input"), ensure_ascii=False, sort_keys=True)
            return [(agent, _clip(f"> {data.get('name')} {arguments}", 240))]
        if kind == "agent.tool_result":
            status = "ok" if data.get("ok", True) else "error"
            first = (str(data.get("output") or "").strip().splitlines() or [""])[0]
            return [(agent, _clip(f"< {data.get('name')} {status} {first}".rstrip(), 240))]
        if kind == "agent.output":
            return [(agent, str(data.get("line") or ""))]
        return []

    def _job_changes(self, job: dict) -> list[tuple[str, str]]:
        key = (job.get("repo"), job.get("id"))
        previous = self._jobs.get(key)
        self._jobs[key] = job
        stage = str(job.get("stage") or "?")
        same_round = previous is not None and previous.get("round") == job.get("round")
        changes = []
        if not same_round or previous.get("stage") != stage:
            changes.append((stage, job_info(job)))
        before = (previous.get("reviews") or {}) if same_round else {}
        for reviewer, verdict in (job.get("reviews") or {}).items():
            if verdict in VERDICTS and before.get(reviewer) != verdict:
                changes.append((stage, f"{reviewer}: {verdict}"))
        return changes

    def _stream(self, event: dict, kind: str, data: dict) -> None:
        text = str(data.get("text") or "")
        key = (data.get("run") or event.get("agent"), kind)
        if key != self._stream_key:
            self._end_stream()
            prefix = "(thinking) " if kind == "agent.reasoning" else ""
            self.out.write(f"{clock(event.get('ts'))}  {str(event.get('agent') or '-'):<{LABEL_WIDTH}}  {prefix}")
            self._stream_key = key
            text = text.lstrip("\n")
        body = text.rstrip("\n")
        if body:
            continuation = "\n" + INDENT
            self.out.write(continuation * self._held_newlines + body.replace("\n", continuation))
            self._held_newlines = 0
        self._held_newlines += len(text) - len(body)
        self.out.flush()

    def _end_stream(self) -> None:
        if self._stream_key is not None:
            self.out.write("\n")
            self._stream_key = None
            self._held_newlines = 0


def _end_message(data: dict) -> str:
    if not data.get("ok", True):
        return f"failed: {data.get('error') or 'unknown error'}"
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    details = [f"{data['turns']} turns"] if data.get("turns") else []
    if usage.get("input_tokens") or usage.get("output_tokens"):
        details.append(f"{_tokens(usage.get('input_tokens'))} in / {_tokens(usage.get('output_tokens'))} out")
    return "finished" + (f" ({', '.join(details)})" if details else "")


def run_summary(job: dict) -> list[str]:
    """The closing lines of `factory run`: outcome, effort, where the files are, what to do next."""
    ref = f"{job.get('repo')}/{job.get('id')}"
    stage = str(job.get("stage") or "?")
    info = job_info(job)
    usage = job.get("usage") if isinstance(job.get("usage"), dict) else {}
    lines = [f"{ref}: {stage}" + (f" — {info}" if info else ""),
             f"  rounds {job.get('round') or 0}, resumes {job.get('resumes') or 0}/{job.get('max_resumes') or 0}, "
             f"tokens {_tokens(usage.get('input_tokens'))} in / {_tokens(usage.get('output_tokens'))} out",
             f"  job folder: {job.get('job_dir')}"]
    actions = {"needs-human": "approve, rework or reject", "failed": "retry or reject",
               "delivered": "approve (merges it) or reject"}.get(stage)
    if actions:
        lines.append(f"  next: factory serve, then {actions} {ref} (CLI or GUI)")
    return lines


# -- argument types ---------------------------------------------------------------


def job_ref(text: str) -> tuple[str, str]:
    """argparse type for <repo>/<job-id>."""
    return _ref(text, JOB_ID_RE, "job-id", "my-app/003-add-login")


def plan_ref(text: str) -> tuple[str, str]:
    """argparse type for <repo>/<plan-id>."""
    from .plans import PLAN_ID_RE

    return _ref(text, PLAN_ID_RE, "plan-id", "my-app/P001-billing")


def _ref(text: str, pattern: re.Pattern[str], kind: str, example: str) -> tuple[str, str]:
    repo, slash, ident = text.partition("/")
    if not slash or not REPO_NAME_RE.fullmatch(repo) or not pattern.fullmatch(ident):
        raise argparse.ArgumentTypeError(f"expected <repo>/<{kind}> such as {example}, got {text!r}")
    return repo, ident


def port_number(text: str) -> int:
    """argparse type for a TCP port."""
    try:
        port = int(text)
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"not a TCP port: {text!r}")
    return port


def text_arg(value: str) -> str:
    """A feature/request argument; "-" reads it from stdin. Empty text is a CliError."""
    text = sys.stdin.read() if value == "-" else value
    if not text.strip():
        raise CliError("the text is empty")
    return text


# -- commands: setup --------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    """Write the config (defaults + a new token), optionally a preset and data dir."""
    path = default_config_path()
    try:
        previous = path.read_bytes() if path.is_file() else None
        config = init_config(path, force=args.force)
    except FileExistsError as exc:
        raise CliError(str(exc)) from None
    except ValueError as exc:  # an existing file that is not a JSON object
        raise CliError(f"{exc} (fix it, or replace it with: factory init --force)") from None
    except OSError as exc:
        raise CliError(f"cannot write the config {path}: {exc}") from None
    try:
        if args.preset:
            from .settings import apply_preset

            config = apply_preset(config, args.preset)
        if args.data_dir:
            config.raw["data_dir"] = str(Path(args.data_dir).expanduser().absolute())
            config.save()
    except (ValueError, OSError) as exc:
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(previous)
        raise CliError(str(exc)) from None
    config = Config.load(path)
    print(path)
    keys = _needed_keys(config)
    env_path = path.parent / ENV_FILE
    hint(f"Wrote {path}" + (f" (preset {args.preset})" if args.preset else "") + f"; data in {config.data_dir}.")
    hint("Next:")
    if keys:
        hint(f"  1. API keys go in {env_path} (chmod 600), one NAME=value per line: {', '.join(keys)}")
    else:
        hint(f"  1. Local models only: no API keys needed (any go in {env_path})")
    hint("  2. factory repo add /path/to/repo")
    hint("  3. factory doctor")
    hint("  4. factory serve, then factory url for the login link")
    return 0


def _needed_keys(config: Config) -> list[str]:
    """api_key_env names of the providers that the resolved agents' models use."""
    from .settings import resolved_agents

    names: list[str] = []
    try:
        agents = resolved_agents(config)
    except ValueError:
        return names
    for agent in agents.values():
        try:
            env_name = config.agent_model(agent).provider.api_key_env
        except (KeyError, ValueError):
            continue
        if env_name and env_name not in names:
            names.append(env_name)
    return names


def cmd_url(args: argparse.Namespace) -> int:
    """Print the login URL, and how to tunnel to it over SSH."""
    config = load_config()
    token = require_token(config)
    base = server_url(config)
    print(f"{base}/login?token={urllib.parse.quote(token, safe='')}")
    address = urllib.parse.urlsplit(base)
    if address.hostname in LOOPBACK_HOSTS:
        port = address.port or 8765
        hint(f"From another machine, tunnel first: ssh -N -L {port}:127.0.0.1:{port} "
             f"{_login_name()}@{socket.gethostname()}")
    return 0


def _login_name() -> str:
    try:
        return getpass.getuser()
    except (KeyError, OSError):
        return "<user>"


def cmd_doctor(args: argparse.Namespace) -> int:
    """Run the checks in-process; exit 1 if an error-level check fails."""
    from .doctor import run_checks

    config = load_config()
    # The look-around skips the harness's own port: the config's, and the one a running `factory serve --port` took.
    running = recorded_server_url(config.data_dir) if args.look_around else None
    skip = {port} if running and (port := urllib.parse.urlsplit(running).port) else set()
    checks = run_checks(config, probe_models=args.probe, look_around=args.look_around, look_skip_ports=skip)
    if args.json:
        print_json(checks)
    errors = warnings = 0
    for check in checks:
        level = str(check.get("level") or "error")
        failed = not check.get("ok")
        if failed and level == "error":
            errors += 1
        elif failed and level == "warn":
            warnings += 1
        if not args.json:
            mark = "ok" if not failed else {"error": "FAIL", "warn": "warn"}.get(level, level)
            detail = f": {check['detail']}" if check.get("detail") else ""
            print(f"{mark:<5} {check.get('name')}{detail}")
    hint(f"{errors} error(s), {warnings} warning(s)")
    return 1 if errors else 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the HTTP server and the factory workers until interrupted."""
    config = load_config()
    require_token(config)
    _validate(config)
    host = args.host or str(_server_settings(config).get("host") or "127.0.0.1")
    port = args.port or int(_server_settings(config).get("port") or 8765)
    if host not in LOOPBACK_HOSTS:
        warn(f"listening on {host}: anyone who can reach port {port} and has the token can run agents")
    # The lock file records the address, so CLI commands reach a server on a --host/--port override.
    with factory_lock(config.data_dir, url=connect_url(host, port)):
        from .orchestrator import new_factory
        from .server import run_server

        # The bus redacts factory.secret_values(): the live config's secrets (it follows config
        # updates from the GUI) plus keys only a job's per-run preset knows.
        factory, bus = new_factory(config)
        factory.start()
        try:
            run_server(config, factory, bus, host=args.host, port=args.port)  # until SIGTERM / Ctrl-C
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                raise CliError(f"port {port} is already in use: stop the other program, or pick a port "
                               "with --port or server.port") from None
            raise
        finally:
            factory.stop()  # run_server stops it too; stopping twice is harmless
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Run one job in-process, without the server, and print its transitions."""
    config = load_config()
    repo = _known_repo(config, args.repo)
    feature = text_arg(args.feature)
    _validate(config)
    with factory_lock(config.data_dir), sigterm_as_interrupt():
        from .orchestrator import ActionError, new_factory

        factory, bus = new_factory(config)   # the bus redacts factory.secret_values() (per-run presets too)
        events = bus.subscribe()
        printer = EventPrinter(sys.stdout, transcript=False)
        # Restart recovery and workers only. Factory.start() would also start the plan manager,
        # which resumes plans left running or blocked on disk (submits their unblocked jobs, can
        # start the lead's final review) inside this one-shot process. They wait for `serve`.
        factory.start_jobs()
        try:
            try:
                job = factory.submit(repo, feature, **({"routing": args.preset} if args.preset else {}),
                                     **({"delivery": args.delivery} if args.delivery else {}),
                                     **({"profile": args.profile} if args.profile is not None else {}))
            except (KeyError, ValueError) as exc:
                raise CliError(_reason(exc)) from None
            job = _watch_job(factory, events, printer, job, ActionError)
        finally:
            printer.finish()
            bus.unsubscribe(events)
            factory.stop()
    for line in run_summary(job):
        print(line)
    return RUN_EXIT_CODES.get(str(job.get("stage")), 1)


def _watch_job(factory: Any, events: queue.Queue, printer: EventPrinter, job: dict,
               action_error: type[Exception]) -> dict:
    """Print the job's events until it stops (Ctrl-C cancels it); returns its final job.json."""
    key = (job["repo"], job["id"])
    cancelling = False
    next_poll = time.monotonic() + POLL_SECONDS
    while True:
        try:
            try:
                event = events.get(timeout=0.5).to_dict()
            except queue.Empty:
                event = None
            if event is not None and _event_job(event) == key:
                printer.handle(event)
                latest = (event.get("data") or {}).get("job")
                if event["type"] == "job.updated" and isinstance(latest, dict) \
                        and latest.get("stage") in STOP_STAGES:
                    return latest
            if time.monotonic() >= next_poll:  # a backstop, should events have been dropped
                next_poll = time.monotonic() + POLL_SECONDS
                latest = factory.job(*key)
                if latest.get("stage") in STOP_STAGES:
                    printer.handle({"type": "job.updated", "ts": latest.get("updated_at"),
                                    "data": {"job": latest}})
                    return latest
        except KeyboardInterrupt:
            if cancelling:
                raise
            cancelling = True
            hint("factory: cancelling the job (Ctrl-C again to quit at once)")
            try:
                factory.cancel(*key)
            except action_error as exc:
                hint(f"factory: cannot cancel: {exc}")


def _known_repo(config: Config, name: str) -> str:
    try:
        return config.repo(name).name
    except KeyError:
        known = ", ".join(repo.name for repo in config.repos()) or "none yet: factory repo add <path>"
        raise CliError(f"unknown repo {name!r} (configured: {known})") from None
    except ValueError as exc:
        raise CliError(str(exc)) from None


def _validate(config: Config) -> None:
    try:
        config.validate()
    except ValueError as exc:
        raise CliError(f"invalid config {config.path}: {exc}") from None


# -- commands: repos ------------------------------------------------------------------


def cmd_repo_add(args: argparse.Namespace) -> int:
    """Register a repo; through the server when it runs, so its in-memory config stays current."""
    config = load_config()
    path = str(Path(args.path).expanduser().resolve())
    client = _client_if_running(config)
    if client is not None:
        body = {"path": path, **({"name": args.name} if args.name else {})}
        repo = _unwrap(client.post("/api/repos", body), "repo")
        print(f"{repo.get('name')}  {repo.get('path')}")
        return 0
    try:
        added = config.add_repo(path, args.name)
    except ValueError as exc:
        raise CliError(str(exc)) from None
    print(f"{added.name}  {added.path}")
    return 0


def _client_if_running(config: Config) -> Client | None:
    """A Client when the config has a token and the server answers, else None (work offline)."""
    if not _server_settings(config).get("token"):
        return None
    client = Client(config)
    try:
        client.get("/api/state")
    except ServerNotRunning:
        return None
    return client


def cmd_repo_list(args: argparse.Namespace) -> int:
    """List configured repos (read from the config file)."""
    try:
        repos = load_config().repos()
    except ValueError as exc:
        raise CliError(str(exc)) from None
    width = max((len(repo.name) for repo in repos), default=0)
    for repo in repos:
        print(f"{repo.name:<{width}}  {repo.path}  base={repo.base or 'auto'}")
    if not repos:
        hint("no repos yet: factory repo add /path/to/repo")
    return 0


def cmd_repo_remove(args: argparse.Namespace) -> int:
    """Unregister a repo (its jobs and worktrees stay on disk)."""
    config = load_config()
    client = _client_if_running(config)
    if client is not None:
        repos = [repo for repo in client.get("/api/config").get("repos") or [] if isinstance(repo, dict)]
        kept = [repo for repo in repos if repo.get("name") != args.name]
        if len(kept) == len(repos):
            raise CliError(f"unknown repo {args.name!r}")
        client.post("/api/config", {"repos": kept})
    else:
        try:
            config.remove_repo(args.name)
        except KeyError:
            raise CliError(f"unknown repo {args.name!r}") from None
    print(args.name)
    return 0


# -- commands: jobs (server) ------------------------------------------------------


def cmd_submit(args: argparse.Namespace) -> int:
    """Queue a feature on the running server; prints <repo>/<job-id>."""
    body = {"repo": args.repo, "feature": text_arg(args.feature)}
    if args.preset:
        body["routing"] = args.preset
    if args.delivery:
        body["delivery"] = args.delivery
    if args.profile is not None:
        body["profile"] = args.profile
    job = _unwrap(Client(load_config()).post("/api/jobs", body), "job")
    print(f"{job.get('repo')}/{job.get('id')}")
    hint(f"{job.get('stage', 'queued')}; follow it with: factory logs {job.get('repo')}/{job.get('id')} -f")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """List jobs (all, or one repo's) from the running server."""
    state = Client(load_config()).get("/api/state")
    jobs = [job for job in state.get("jobs") or [] if isinstance(job, dict)]
    if args.repo:
        names = [repo.get("name") for repo in state.get("repos") or [] if isinstance(repo, dict)]
        if args.repo not in names:
            raise CliError(f"unknown repo {args.repo!r}")
        jobs = [job for job in jobs if job.get("repo") == args.repo]
    if args.json:
        print_json(jobs)
        return 0
    width = _ref_width(jobs)
    for job in jobs:
        print(job_line(job, width))
    if not jobs:
        hint("no jobs yet")
    return 0


def make_action(action: str, body: Callable[[argparse.Namespace], dict] | None = None,
                ) -> Callable[[argparse.Namespace], int]:
    """A command that POSTs /api/jobs/<repo>/<id>/<action> with body(args) and prints the job."""
    def command(args: argparse.Namespace) -> int:
        repo, job_id = args.ref
        payload = body(args) if body else {}
        job = _unwrap(Client(load_config()).post(f"/api/jobs/{_quote(repo, job_id, action)}", payload), "job")
        print(job_line(job))
        return 0

    command.__doc__ = f"{action.capitalize()} a job on the running server."
    return command


def cmd_approve(args: argparse.Namespace) -> int:
    """Approve a job on the running server; with --carry-open-items, name its unproven must-do items."""
    from .items import holds_gate

    repo, job_id = args.ref
    client = Client(load_config())
    body: dict[str, Any] = {}
    if args.carry_open_items:
        listed = client.get(f"/api/jobs/{_quote(repo, job_id)}/items")
        waiting = [row for row in (listed.get("items") or []) if isinstance(row, dict) and holds_gate(row)]
        body["carry_items"] = [str(row.get("id")) for row in waiting]
        for row in waiting:
            hint(f"carrying {row.get('id')} [{row.get('word') or row.get('status')}] {row.get('title')}")
        if not waiting:
            hint("no must-do item is open: nothing to carry")
    job = _unwrap(client.post(f"/api/jobs/{_quote(repo, job_id, 'approve')}", body), "job")
    print(job_line(job))
    return 0


def cmd_backlog(args: argparse.Namespace) -> int:
    """Show a repo's backlog, add an item, or run the next one."""
    client = Client(load_config())
    path = f"/api/repos/{_quote(args.repo)}"
    if args.action == "next":
        job = _unwrap(client.post(f"{path}/next"), "job")
        print(f"{job.get('repo')}/{job.get('id')}")
        hint(f"started: {job.get('feature', '')}")
        return 0
    if args.action == "add":
        reply = client.post(f"{path}/backlog", {"text": args.text})
    else:
        reply = client.get(f"{path}/backlog")
    items = reply.get("items") or []
    for item in items:
        job = f" → {item['job']}" if item.get("done") and item.get("job") else ""
        print(f"[{'x' if item.get('done') else ' '}] {item.get('text')}{job}")
    if not items:
        hint(f"the backlog of {args.repo} is empty")
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    """Print a job's agent events from its log files; with -f, keep following the server's stream."""
    config = load_config()
    repo, job_id = args.ref
    store = JobStore(config.data_dir)
    job_dir = store.job_dir(repo, job_id)
    if not job_dir.is_dir():
        raise CliError(f"no job {repo}/{job_id} in {store.jobs_root}")
    printer = EventPrinter(sys.stdout, transcript=True)
    seen = set()
    history = read_job_events(job_dir)
    for event in history:
        seen.add(_event_identity(event))
        printer.handle(event)
    printer.finish()
    if not args.follow:
        return 0
    # job.* snapshots are not in the log files (the transcript shows stage changes as log
    # lines), so the server's replay would print the whole stage history again, after the
    # transcript. Snapshots from before the transcript's last event only set the state.
    last = _event_order(history[-1]) if history else None
    try:
        stage = store.load(repo, job_id).get("stage")
    except (KeyError, ValueError):
        stage = None
    if stage in TERMINAL_STAGES:
        hint(f"factory: {repo}/{job_id} is {stage}; nothing more to follow")
        return 0
    try:
        for event in Client(config).events(since=0):
            if _event_job(event) != (repo, job_id) or _event_identity(event) in seen:
                continue
            if event.get("type") in JOB_EVENTS and last is not None and isinstance(event.get("ts"), str) \
                    and _event_order(event) <= last:
                printer.track(event)
                continue
            printer.handle(event)
            latest = (event.get("data") or {}).get("job")
            if event.get("type") == "job.updated" and isinstance(latest, dict) \
                    and latest.get("stage") in TERMINAL_STAGES:
                return 0
    finally:
        printer.finish()
    raise CliError("the server closed the event stream")


def read_job_events(job_dir: Path) -> list[dict]:
    """Every event in <job_dir>/logs/*.jsonl, oldest first (unparseable lines are skipped)."""
    events = []
    for path in sorted((job_dir / "logs").glob("*.jsonl")):
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    events.append(event)
    events.sort(key=_event_order)
    return events


def _event_identity(event: dict) -> tuple[Any, Any, Any]:
    return event.get("seq"), event.get("ts"), event.get("type")


def _event_order(event: dict) -> tuple[str, int]:
    """Sort key: timestamp (seconds; restarts reset seq), then seq within the second."""
    seq = event.get("seq")
    return str(event.get("ts") or ""), seq if isinstance(seq, int) and not isinstance(seq, bool) else 0


# -- commands: plans (server) ---------------------------------------------------------


def cmd_plan(args: argparse.Namespace) -> int:
    """Hand a large request to the lead, which splits it into jobs."""
    config = load_config()
    body = {"repo": args.repo, "request": text_arg(args.request)}
    if args.delivery:
        body["delivery"] = args.delivery
    if args.profile is not None:
        body["profile"] = args.profile
    plan = _unwrap(Client(config).post("/api/plans", body), "plan")
    ref = f"{plan.get('repo')}/{plan.get('id')}"
    print(ref)
    plan_md = config.data_dir / "plans" / str(plan.get("repo")) / str(plan.get("id")) / "plan.md"
    hint(f"{plan.get('stage', 'planning')}: the lead writes {plan_md}; "
         f"read or edit it (GUI: Plans), then: factory plans run {ref}")
    return 0


def cmd_plans(args: argparse.Namespace) -> int:
    """List plans from the running server."""
    reply = Client(load_config()).get("/api/plans")
    plans = [plan for plan in _unwrap(reply, "plans") or [] if isinstance(plan, dict)]
    if args.json:
        print_json(plans)
        return 0
    width = _ref_width(plans)
    for plan in plans:
        print(plan_line(plan, width))
    if not plans:
        hint('no plans yet: factory plan <repo> "<request>"')
    return 0


def make_plan_action(action: str) -> Callable[[argparse.Namespace], int]:
    """A command that POSTs /api/plans/<repo>/<id>/<action>."""
    def command(args: argparse.Namespace) -> int:
        repo, plan_id = args.ref
        plan = _unwrap(Client(load_config()).post(f"/api/plans/{_quote(repo, plan_id, action)}"), "plan")
        print(plan_line(plan))
        return 0

    command.__doc__ = f"{action.capitalize()} a plan on the running server."
    return command


# -- commands: agents and models (offline) ------------------------------------------


def cmd_agents(args: argparse.Namespace) -> int:
    """List the resolved agents: routed model, driver and where the file comes from."""
    from .settings import resolved_agents

    config = load_config()
    repo_path = config.repo(_known_repo(config, args.repo)).path if args.repo else None
    try:
        agents = resolved_agents(config, repo_path)
    except ValueError as exc:
        raise CliError(str(exc)) from None
    rows = [_agent_row(config, agents[name]) for name in sorted(agents)]
    if args.json:
        print_json(rows)
        return 0
    width = max((len(row["name"]) for row in rows), default=0)
    for row in rows:
        if row.get("cli_model"):   # what agent.start reports: the driver serves it, with the CLI's model
            target = f"{row['driver']}/{row['cli_model']}"
        else:
            target = f"{row['provider']}/{row['model_id']}" if row["provider"] else row["error"]
        print(f"{row['name']:<{width}}  {row['model']:<14}  {row['driver']:<8}  {row['source']:<7}  {target}")
    return 0


def _agent_row(config: Config, agent: AgentDef) -> dict:
    from .settings import agent_source

    row = {"name": agent.name, "description": agent.description, "tier": agent.model,
           "model": agent.model, "provider": None, "model_id": None, "cli_model": None, "error": None,
           "driver": config.agent_driver(agent.name), "source": agent_source(config, agent),
           "path": str(agent.path)}
    try:
        model = config.agent_model(agent)
    except (KeyError, ValueError) as exc:
        row["error"] = f"invalid route: {_reason(exc)}"
    else:
        row.update(model=model.key, provider=model.provider.name, model_id=model.model)
        row["cli_model"] = _cli_model(config, row["driver"], model)
    return row


def _cli_model(config: Config, driver_name: str, model: ModelConfig) -> str | None:
    """The model a CLI driver launches its CLI with for this route (drivers.cli.cli_model), None for
    a builtin driver or one that does not resolve: the CLI picks its own endpoint, so the model
    entry's provider is not what serves the run."""
    from .drivers.cli import cli_model

    try:
        driver = config.driver(driver_name)
    except (KeyError, ValueError):
        return None
    return cli_model(driver, model.key, model.model) if driver.type == "cli" else None


def cmd_personas(args: argparse.Namespace) -> int:
    """List the personas (DESIGN §33): agent files bound to an Open WebUI workspace model, snapshots, wake ones."""
    from .personas import discover_personas, persona_view

    config = load_config()
    try:
        found = discover_personas(config)
    except ValueError as exc:
        raise CliError(str(exc)) from None
    rows = [persona_view(config, found[name]) for name in sorted(found)]
    if args.json:
        print_json(rows)
        return 0
    width = max((len(row["name"]) for row in rows), default=0)
    for row in rows:
        drift = row.get("drift") or {}
        state = f"  last check: {drift.get('kind')}" if drift else ""
        problem = f"  invalid: {row['error']}" if row.get("error") else ""
        print(f"{row['name']:<{width}}  {row['kind']:<9}  {row['label']}  (models.{row.get('model') or '-'}, "
              f"{row['privacy']}, card {'yes' if row.get('card') else 'no'}){state}{problem}")
    if not rows:
        hint("no personas: agent files with openwebui: <workspace model id> in ~/.config/software-factory/agents/")
    return 0


def cmd_personas_card(args: argparse.Namespace) -> int:
    """Print a persona's saved profile card, what runs where (its capability map) and what Open WebUI lists for
    it now."""
    from .personas import OpenWebUIError, capability_map, find_persona, live_card, load_profile

    config = load_config()
    try:
        persona = find_persona(config, args.name)
    except (KeyError, ValueError) as exc:
        raise CliError(_reason(exc)) from None
    model_id = (persona.binding or {}).get("model")
    if not model_id:
        raise CliError(f"persona {args.name} is not bound to an Open WebUI workspace model")
    try:
        live = live_card(config, persona)
    except OpenWebUIError as exc:
        live = {"error": str(exc)}
    card = load_profile(model_id)
    view = {"persona": args.name, "card": card, "live": live,
            "capability_map": capability_map(card, persona, config) if card else None,
            "import": _offline_import_record(config, model_id, card) if card else None}
    if args.json:
        print_json(view)
        return 0
    card = view["card"]
    if card is None:
        hint(f"no profile card for {model_id!r} yet: import one with `factory personas import <Export file>` "
             f"(server) or from the GUI")
    else:
        print(f"{card['name']} ({card['model_id']}), version {str(card.get('version_id'))[:12]}, imported "
              f"{str(card.get('imported_at'))[:10]} from the {card['source']}")
        print(f"base model: {card.get('base_model_id')}; system prompt: {card['system']['chars']} characters")
        for part in card.get("parts") or []:
            mark = "yes" if part["through_api"] else "no "
            ids = f" ({', '.join(part['ids'])})" if part["ids"] else ""
            print(f"  [{mark}] {part['part']}{ids}: {part['note']}")
        _print_capability_map(view["capability_map"])
        _print_import_record(view["import"])
    if live.get("error"):
        hint(f"live: {live['error']}")
    elif live.get("listed"):
        print(f"live: version {str(live.get('version_id'))[:12]}, base model {live.get('base_model_id')}")
        for warning in live.get("warnings") or []:
            hint(f"live: {warning}")
    else:
        hint(f"live: Open WebUI does not list {model_id!r} for this key")
    return 0


def _print_capability_map(cmap: dict) -> None:
    """The capability map (DESIGN §33): one line of what each surface has and one of what it does not."""
    def parts(rows: list[dict]) -> str:
        return "; ".join(row["part"] + (f" ({', '.join(row['ids'])})" if row["ids"] else "") for row in rows) or "-"

    print(f"what runs where: {cmap['note']}")
    for surface in cmap["surfaces"]:
        state = {False: " (not set up)", None: ""}.get(surface["exists"], "")
        names = f" [{', '.join(surface['personas'])}]" if surface.get("personas") else ""
        print(f"  {surface['title']}{state}{names}")
        print(f"    has: {parts(surface['available'])}")
        print(f"    not: {parts(surface['unavailable'])}")
    for route in cmap["delegation"]:
        print(f"  {route['route']}: {route['text']}")


def _offline_import_record(config: Config, model_id: str, card: dict) -> dict:
    """The card's import record read here without the server (read only): the baseline note and the receipts
    attached to the import (DESIGN §33 "Changes before import")."""
    from .personas import IMPORT_BASELINE
    from .receipts import REPORTED

    reader = _offline_receipts(config)
    rows = []
    for row in reader.attached(model_id):
        try:
            receipt = reader.get(row["id"])["receipt"]
        except KeyError:
            rows.append({**row, "missing": True, "label": REPORTED})
            continue
        rows.append({**row, **{key: receipt.get(key) for key in ("time", "intent", "not_verified", "verified",
                                                                  "outcome", "reported_outcome")},
                     "label": REPORTED})
    return {"model": model_id, "imported_at": card.get("imported_at"), "version_id": card.get("version_id"),
            "diff": None, "baseline": IMPORT_BASELINE, "changes_before_import": rows}


def _print_import_record(record: dict | None) -> None:
    """The import is the baseline; the changes before it are only the receipts attached to it: each one's outcome,
    then not_verified before verified."""
    if not record:
        return
    print(f"import: {record['baseline']}")
    rows = record.get("changes_before_import") or []
    if not rows:
        print("  changes before import: none attached (factory personas before-import <name> --attach <receipt id>)")
    for row in rows:
        if row.get("missing"):
            print(f"  {row['id']}: attached by the {row.get('by')}, no longer in the receipts index")
            continue
        print(f"  {row.get('time')}  {row['id']}  ({row['label']}; attached by the {row.get('by')})")
        print(f"    intent: {row.get('intent')}")
        if isinstance(row.get("outcome"), dict):   # an outcome reported unknown or failed never reads as verified (§40)
            print(f"    outcome: {row['outcome'].get('words')}")
        for text in row.get("not_verified") or []:
            print(f"    NOT verified: {text}")
        for text in row.get("verified") or []:
            print(f"    verified (reported): {text}")
        if not row.get("verified"):
            print("    verified: nothing")


def cmd_personas_before_import(args: argparse.Namespace) -> int:
    """(server) Attach the operator's receipts (by id) to a persona's import as its changes before import."""
    if not args.attach and not args.detach:
        raise CliError("name receipts with --attach ID or --detach ID")
    body = {"attach": args.attach or [], "detach": args.detach or [], "by": args.by}
    result = Client(load_config()).post(f"/api/personas/{args.name}/before-import", body)
    if args.json:
        print_json(result)
        return 0
    _print_import_record(result.get("import"))
    return 0


def cmd_personas_ledger(args: argparse.Namespace) -> int:
    """(server) The read ledger of a persona's external memory: what the harness read, per file, and the kept
    copies; --show one, --diff two, --restore one (the earlier text for her own tools or you to apply: the
    harness never writes her store), or --erase one (its copy goes, the ledger keeps its sha256 only)."""
    client = Client(load_config())
    base = f"/api/personas/{args.name}/external"
    if args.erase:
        result = client.post(f"{base}/erase", {"sha256": args.erase}).get("erased") or {}
        if args.json:
            print_json(result)
            return 0
        print(f"erased {str(result.get('sha256'))[:12]}: {result.get('copies', 0)} copy removed, "
              f"{result.get('entries', 0)} manifest entries keep its sha256 only")
        hint("her own store is not touched: while a file there holds that text, wakes still read it")
        return 0
    if args.show:
        result = client.get(f"{base}/versions/{args.show}")
        if args.json:
            print_json(result)
            return 0
        print(f"{result['sha256'][:12]}  {', '.join(result.get('names') or [])}  (private; read "
              f"{str(result.get('first_read_at'))[:16]} to {str(result.get('last_read_at'))[:16]})")
        if result.get("redacted"):
            hint("a secret the harness knows was taken out of this copy")
        print(result.get("text") or "", end="" if str(result.get("text") or "").endswith("\n") else "\n")
        return 0
    if args.diff:
        older, newer = args.diff
        result = client.get(f"{base}/diff?" + urllib.parse.urlencode({"from": older, "to": newer}))
        if args.json:
            print_json(result)
            return 0
        print(result.get("diff") or "(no difference)")
        return 0
    if args.restore:
        body = {"sha256": args.restore, **({"file": args.file} if args.file else {})}
        result = client.post(f"{base}/restore", body).get("restore") or {}
        if args.json:
            print_json(result)
            return 0
        current = result.get("current") or {}
        print(f"restore {result.get('name')} to {str(result.get('sha256'))[:12]} (the newest read held "
              f"{str(current.get('sha256') or 'nothing')[:12]})")
        for warning in result.get("warnings") or []:
            hint(warning)
        hint(result.get("apply") or "")
        print(result.get("text") or "", end="" if str(result.get("text") or "").endswith("\n") else "\n")
        return 0
    result = client.get(base)
    if args.json:
        print_json(result)
        return 0
    print(f"read ledger: {'on' if result.get('ledger') else 'off'}; {result.get('reads', 0)} reads, "
          f"{result.get('kept_bytes', 0)} of {result.get('cap_bytes', 0)} bytes kept (private)")
    caps = result.get("caps") or {}
    for key, words in (("evicted", "older copies removed to stay under the cap (the newest of each file kept)"),
                       ("trimmed", "oldest manifests removed to stay under the cap"),
                       ("not_kept", "versions not kept: they did not fit the cap")):
        if caps.get(key):
            hint(f"{caps[key]} {words}, the last at {str(caps.get(key + '_at'))[:16]}")
    for file in result.get("files") or []:
        print(file["name"])
        for row in file["versions"]:
            kept = "kept" + (", redacted" if row.get("redacted") else "") if row.get("kept") else "not kept"
            cut = ", only the start read" if row.get("truncated") else ""
            print(f"  {row['sha256'][:12]}  {row.get('bytes')} bytes  read {str(row.get('first_read_at'))[:16]} "
                  f"to {str(row.get('last_read_at'))[:16]} ({row.get('reads')}x)  {kept}{cut}")
    for row in result.get("erased") or []:
        print(f"erased  {str(row.get('sha256'))[:12]}  at {str(row.get('erased_at'))[:16]} ({row.get('reads')}x read)")
    for row in (result.get("sqlite") or [])[:5]:
        if row.get("error"):
            detail = f": {row['detail']}" if row.get("detail") else ""
            print(f"database read {str(row.get('at'))[:16]}: could not be read ({row['error']}{detail})")
        else:
            print(f"database read {str(row.get('at'))[:16]}: {row.get('rows')} rows, sha256 "
                  f"{str(row.get('sha256'))[:12]}")
    if not result.get("files") and not result.get("sqlite") and not result.get("erased"):
        hint("nothing recorded yet" + ("" if result.get("ledger") else
                                       " (memory.external.ledger is off for this persona)"))
    return 0


def cmd_personas_import(args: argparse.Namespace) -> int:
    """(server) Import an Open WebUI Export file (or the model through the API) as a profile card."""
    body: dict[str, Any] = {}
    if args.file:
        try:
            records = json.loads(Path(args.file).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CliError(f"cannot read {args.file}: {_reason(exc)}") from None
        if isinstance(records, dict):
            records = [records]
        if not isinstance(records, list):
            raise CliError(f"{args.file} is not an Open WebUI Export file (a JSON array of models)")
        for record in records:   # images are cosmetic, and keep the upload under the server's 1 MB limit
            if isinstance(record, dict):
                record.pop("background_image_data", None)
                if isinstance(record.get("meta"), dict):
                    record["meta"].pop("profile_image_url", None)
        body["export"] = records
    else:
        if not args.model:
            raise CliError("give an Export file, or --model to read it from Open WebUI with the owner's key")
        body["source"] = "openwebui"
    if args.model:
        body["model_id"] = args.model
    if args.snapshot:
        body["snapshot"] = {"name": args.snapshot, **({"model": args.snapshot_model} if args.snapshot_model else {}),
                            **({"user_name": args.user_name} if args.user_name else {})}
    result = Client(load_config()).post("/api/personas/import", body)
    card = result.get("card") or {}
    print(f"{card.get('model_id')}: profile card saved ({result.get('path')})")
    unavailable = {key: value for key, value in (result.get("unavailable") or {}).items() if value}
    if unavailable:
        hint("not in an imported copy: " + "; ".join(f"{key} {', '.join(value)}" for key, value in unavailable.items()))
    if result.get("snapshot"):
        hint(f"offline snapshot: persona {result['snapshot']['persona']} on models.{result['snapshot']['model']} "
             f"({result['snapshot']['path']})")
    return 0


def cmd_personas_check(args: argparse.Namespace) -> int:
    """(server) Run the drift check now; a changed preset opens a system-scope tracker item."""
    body = {"persona": args.name} if args.name else {}
    rows = Client(load_config()).post("/api/personas/check", body).get("checked") or []
    if args.json:
        print_json(rows)
        return 0
    for row in rows:
        if row.get("error"):
            print(f"{row['persona']}  error: {row['error']}")
        else:
            item = f"  → {row['item']}" if row.get("item") and row.get("kind") in ("behaviour", "unknown") else ""
            print(f"{row['persona']}  {row['kind']}  version {str(row.get('version_id'))[:12]}{item}")
    if not rows:
        hint("no persona is bound to an Open WebUI workspace model")
    return 0


# -- operator personas: receipts and activity (DESIGN.md section 36) ----------------------


def _offline_receipts(config: Config) -> Any:
    """The operator's receipts read here without the server: read only, nothing written (not even the index)."""
    from .events import env_secret_values
    from .receipts import Receipts

    reader = Receipts(lambda: config, config.data_dir, secrets=lambda: [*config.secret_values(), *env_secret_values()])
    if reader.directory() is not None:
        reader.scan(write=False)
    return reader


def cmd_activity(args: argparse.Namespace) -> int:
    """What an agent or persona did since a time: its receipts (an operator's), tracker transitions, messages and
    wakes audit lines, run starts and ends, the GPU custody leases its runs took (an operator: every custody line)
    and an operator's drift checks, oldest first, each with the file it was read from. From the server when it
    runs; else read here from the same files (nothing is written)."""
    config = load_config()
    client = _client_if_running(config)
    if client is not None:
        query = urllib.parse.urlencode({key: value for key, value in (
            ("agent", args.agent), ("since", args.since), ("until", args.until), ("limit", args.limit),
            ("after", args.after)) if value not in (None, "")})
        result = client.get(f"/api/activity?{query}")
    else:
        from .activity import activity
        from .events import env_secret_values

        try:
            result = activity(config, agent=args.agent, since=args.since, until=args.until, limit=args.limit,
                              after=args.after, receipts=_offline_receipts(config),
                              secrets=lambda: [*config.secret_values(), *env_secret_values()])
        except ValueError as exc:
            raise CliError(str(exc)) from None
    if args.json:
        print_json(result)
        return 0
    from .activity import activity_lines

    for line in activity_lines(result):
        print(line)
    if not result.get("entries"):
        hint(f"nothing recorded for {result.get('agent')} since {result.get('since') or 'the start'}")
    for error in result.get("errors") or []:
        hint(f"could not read {error}")
    return 0


def cmd_operator(args: argparse.Namespace) -> int:
    """Who the owner's config marks as the operator, and what the last scan of her receipts found."""
    config = load_config()
    client = _client_if_running(config)
    if client is not None:
        state = client.get("/api/operator")
    else:
        from . import operator as op

        reader = _offline_receipts(config)
        summary = reader.summary()
        state = {"operators": op.operators(config), "marked": op.marked(config.raw), "problem":
                 op.rule_problem(config.raw), "receipts_dir": reader.settings()["receipts_dir"], "scan": summary}
    if args.json:
        print_json(state)
        return 0
    print(f"operator: {', '.join(state.get('operators') or []) or 'none'}")
    if state.get("problem"):
        print(f"problem: {state['problem']}")
    scan = state.get("scan") or {}
    print(f"receipts: {state.get('receipts_dir') or 'operator.receipts_dir is not set'}; {scan.get('read', 0)} read, "
          f"{scan.get('cannot_read', 0)} cannot read, {scan.get('refused', 0)} refused (last scan "
          f"{scan.get('scanned_at') or 'never'})")
    if scan.get("problem"):
        print(f"  {scan['problem']}")
    if scan.get("writable"):
        print("  the harness's own user may write the receipts directory: a file put there would read as hers "
              "(her items from it are marked untrusted; see factory doctor)")
    if scan.get("past_limit"):
        print(f"  {scan['past_limit']} older file(s) past operator.max_receipts were not read")
    return 0


def cmd_operator_receipts(args: argparse.Namespace) -> int:
    """The operator's change receipts, newest first: what each reports, its checks not verified first."""
    from .receipts import receipt_lines

    config = load_config()
    client = _client_if_running(config)
    if client is not None:
        query = urllib.parse.urlencode({key: value for key, value in (
            ("limit", args.limit), ("since", args.since), ("rescan", "1" if args.rescan else None))
            if value not in (None, "")})
        result = client.get(f"/api/operator/receipts?{query}")
    else:
        from .activity import parse_when

        reader = _offline_receipts(config)
        try:
            rows = reader.rows(limit=args.limit, since=parse_when(args.since))
        except ValueError as exc:
            raise CliError(str(exc)) from None
        result = {"receipts": rows, "refused": reader.index().get("refused") or [], "scan": reader.summary()}
    if args.json:
        print_json(result)
        return 0
    for row in result.get("receipts") or []:
        for line in receipt_lines(row):
            print(line)
    for row in result.get("refused") or []:
        print(f"{row.get('file')}  refused: {row.get('why')}")
    problem = (result.get("scan") or {}).get("problem")
    if problem:
        hint(problem)
    elif not result.get("receipts"):
        hint("no receipts read yet")
    return 0


def cmd_operator_scan(args: argparse.Namespace) -> int:
    """(server) Read the operator's receipts directory now and update the tracker items of what she did not check."""
    scan = Client(load_config()).post("/api/operator/receipts/scan").get("scan") or {}
    if args.json:
        print_json(scan)
        return 0
    print(f"{scan.get('read', 0)} read, {scan.get('cannot_read', 0)} cannot read, {scan.get('refused', 0)} refused; "
          f"{scan.get('new', 0)} new, {scan.get('changed', 0)} changed, {scan.get('gone', 0)} gone; items changed: "
          f"{', '.join(scan.get('items') or []) or 'none'}")
    if scan.get("problem"):
        hint(scan["problem"])
    return 0


# -- messages, Seraphina's stop switch, secrets (DESIGN.md section 34) ----------------------


def cmd_notify_status(args: argparse.Namespace) -> int:
    """(server) The phone channels, their health, and whether Seraphina is stopped."""
    state = Client(load_config()).get("/api/notify")
    if args.json:
        print_json(state)
        return 0
    for row in state.get("channels") or []:
        health = row.get("health") or {}
        mark = "ok" if row.get("active") and health.get("ok") is not False else "off" if not row.get("active") else "down"
        why = row.get("inactive_why") or health.get("detail") or ""
        answered = f"  phone last answered {row['last_answer']}" if row.get("last_answer") else ""
        print(f"{row.get('name')}  {row.get('type')}  {row.get('privacy')}  {mark}  {why}{answered}".rstrip())
    if not state.get("channels"):
        hint("no phone channel yet: Messages, Phone setup in the cockpit")
    quiet = (state.get("quiet_hours") or {})
    if quiet.get("window"):
        hint(f"do-not-disturb {quiet['window']} ({quiet.get('source')})")
    paused = state.get("paused")
    if paused:
        hint(f"Seraphina is stopped (by {paused.get('by')} at {paused.get('at')}): factory wake resume")
    today = state.get("today") or {}
    hint(f"{today.get('phone', 0)} of {today.get('cap', 12)} phone messages today")
    return 0


def cmd_notify_test(args: argparse.Namespace) -> int:
    """(server) Send a test notice with a Got it button to one channel, or to each."""
    client = Client(load_config())
    names = [args.channel] if args.channel else [row["name"] for row in client.get("/api/notify/channels").get(
        "channels") or [] if row.get("enabled")]
    if not names:
        raise CliError("no phone channel is set up: Messages, Phone setup in the cockpit")
    for name in names:
        message = client.post("/api/notify/test", {"channel": name}).get("message") or {}
        state = (message.get("deliveries") or {}).get(name)
        print(f"{name}  {message.get('id')}  {state}")
    hint("tap Got it on your phone; the cockpit shows when it arrives")
    return 0


def cmd_notify_event(args: argparse.Namespace) -> int:
    """(server) Report an event as the owner (POST /api/notify/events)."""
    body: dict[str, Any] = {"kind": args.kind}
    if args.subject:
        body["subject"] = args.subject
    if args.detail:
        body["detail"] = args.detail
    result = Client(load_config()).post("/api/notify/events", body)
    print(result.get("message") or "recorded (no new notice: an open incident, or nothing to recover)")
    return 0


def _paused_file(config: Config) -> Path:
    return config.data_dir / "wakes" / "PAUSED"


def cmd_wake_poke(args: argparse.Namespace) -> int:
    """Write a poke into the spool (works without a running server; the server reads it every few seconds). The
    reason is untrusted text for the persona: at most 200 characters."""
    import uuid

    from .personas import PERSONA_NAME_RE

    if not PERSONA_NAME_RE.fullmatch(args.persona or ""):
        raise CliError("give a persona name, e.g. factory wake poke seraphina --reason \"check the backup\"")
    if args.reason is not None and len(args.reason) > 200:
        raise CliError("--reason is at most 200 characters")
    config = load_config()
    spool = config.data_dir / "wakes" / "spool"
    for part in ("tmp", "new"):
        (spool / part).mkdir(mode=0o700, parents=True, exist_ok=True)
    name = f"{args.persona}.{uuid.uuid4().hex}.json"
    body = json.dumps({"reason": args.reason}) if args.reason else ""
    tmp = spool / "tmp" / name
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(body)
    os.replace(tmp, spool / "new" / name)
    print(f"poked {args.persona}: the server reads the spool every 5 seconds")
    return 0


def _persona_stop_offline(config: Config, persona: str, until: str | None, resume: bool) -> str | None:
    """With no server running, a persona's stop is written into the wake state directly (the server reads it).
    --until (HH:MM in wakes.timezone, or an ISO time) is stored as an ISO UTC time, as the server stores it.
    Returns the until as stored (None without one)."""
    from . import wakes
    from .notify import Audit, write_json

    end = None
    if until and not resume:
        clock = wakes.wake_clock(config)
        try:
            end = wakes.iso(wakes.until_time(until, clock, clock.utc()))
        except wakes.WakeError as exc:
            raise CliError(str(exc)) from None
    path = config.data_dir / "wakes" / "state.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    stops = state.setdefault("stops", {})
    if resume:
        if (stops.get(persona) or {}).get("by") == "integrity":
            raise CliError(f"{persona}'s instructions changed outside her page: review the change in the cockpit")
        stops.pop(persona, None)
    else:
        stops[persona] = {"at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "by": "cli", "via": "cli",
                          "until": end, "kind": "stop"}
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_json(path, state)
    Audit(config.data_dir).write("resume" if resume else "pause", persona=persona, by="cli", via="cli", until=end)
    return end


def cmd_wake_pause(args: argparse.Namespace) -> int:
    """Stop Seraphina at once (the PAUSED file, which works without the server), then tell the server; with
    --persona, stop only that persona (--until HH:MM ends it by itself)."""
    config = load_config()
    if args.persona:
        client = _client_if_running(config)
        if client is not None:
            client.post("/api/wakes/control", {"action": "pause", "persona": args.persona,
                                               **({"until": args.until} if args.until else {})})
        else:
            _persona_stop_offline(config, args.persona, args.until, resume=False)
        print(f"{args.persona} is stopped" + (f" until {args.until}" if args.until else "") +
              f". Restart: factory wake resume --persona {args.persona}")
        return 0
    if args.until:
        raise CliError("--until goes with --persona; `factory wake pause` stops Seraphina until you resume her")
    path = _paused_file(config)
    if not path.exists():
        from .notify import Audit, write_json

        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_json(path, {"at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "by": "cli", "via": "cli"})
        Audit(config.data_dir).write("stop", by="cli", via="cli")
    client = _client_if_running(config)
    if client is not None:
        client.post("/api/wakes/control", {"action": "pause"})
    print("Seraphina is stopped. Alerts from your server still come. Restart: factory wake resume")
    return 0


def cmd_wake_resume(args: argparse.Namespace) -> int:
    """(server) Restart Seraphina; with no server running, remove the PAUSED file directly."""
    config = load_config()
    client = _client_if_running(config)
    if getattr(args, "persona", None):
        if client is not None:
            client.post("/api/wakes/control", {"action": "resume", "persona": args.persona})
        else:
            _persona_stop_offline(config, args.persona, None, resume=True)
        print(f"{args.persona} is running.")
        return 0
    if client is not None:
        client.post("/api/wakes/control", {"action": "resume"})
    else:
        path = _paused_file(config)
        if path.exists():
            from .notify import Audit

            path.unlink()
            Audit(config.data_dir).write("restart", by="cli")
    print("Seraphina is running.")
    return 0


def cmd_wake_status(args: argparse.Namespace) -> int:
    """Whether Seraphina is stopped (from the PAUSED file; works without the server); with the server, each
    persona's next check-ins, budget and the last gate reason."""
    config = load_config()
    client = _client_if_running(config)
    if client is not None:
        state = client.get("/api/wakes")
        if args.json:
            print_json(state)
            return 0
        print("stopped" if state.get("paused") else ("running" if state.get("enabled") else "check-ins are off"))
        for row in state.get("personas") or []:
            budget = row.get("budget") or {}
            nxt = (row.get("next") or [{}])[0]
            print(f"{row.get('name')}  {'on' if row.get('enabled') else 'off'}  {row.get('privacy')}  "
                  f"{budget.get('model_wakes', 0)}/{budget.get('max_model_wakes')} check-ins  "
                  f"next {nxt.get('at') or '-'}  {nxt.get('gate') or ''}".rstrip())
        return 0
    path = _paused_file(config)
    record: dict[str, Any] = {}
    if path.exists():
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            record = {}
    state = {"paused": bool(path.exists()), "by": record.get("by"), "at": record.get("at")}
    if args.json:
        print_json(state)
    elif state["paused"]:
        print(f"stopped by {state['by'] or 'someone'} at {state['at'] or '?'}")
    else:
        print("running")
    return 0


def cmd_secret_set(args: argparse.Namespace) -> int:
    """Read one secret from standard input into the env file, never echoed (envfile.check_secret's rules).
    With the server running it is set there too, so it takes effect at once."""
    from .envfile import read_secret_from_stdin, set_secret

    if not args.stdin:
        raise CliError("give the value on standard input with --stdin (never on the command line, where "
                       "other users could read it in /proc)")
    try:
        value = read_secret_from_stdin(sys.stdin)
    except ValueError as exc:
        raise CliError(str(exc)) from None
    config = load_config()
    client = _client_if_running(config)
    try:
        if client is not None:
            client.post("/api/secrets", {"name": args.name, "value": value})
        else:
            set_secret(config, args.name, value)
    except ValueError as exc:
        raise CliError(str(exc)) from None
    print(f"{args.name} is set in {config.path.parent / ENV_FILE}" + (" and in the running server" if client else ""))
    return 0


def cmd_agents_new(args: argparse.Namespace) -> int:
    """Copy an agent into the user agents dir (~/.config/software-factory/agents) and print its path."""
    from .settings import new_agent

    try:
        agent = new_agent(load_config(), args.name, args.source)
    except (KeyError, ValueError) as exc:
        raise CliError(_reason(exc)) from None
    print(agent["path"])
    hint(f"Copied from {args.source}; edit the file (or the Agents view in the GUI) to make it yours.")
    if args.name.startswith("reviewer-"):
        hint(f'To run it as a review lens, add "{args.name[len("reviewer-"):]}" to pipeline.reviewers.')
    return 0


def _model_keys(config: Config) -> list[str]:
    models = config.raw.get("models")
    return [key for key, entry in models.items() if isinstance(entry, dict)] if isinstance(models, dict) else []


def cmd_models(args: argparse.Namespace) -> int:
    """List the model entries: provider, model id, context and whether the server is local."""
    from .settings import is_local_url

    config = load_config()
    rows = []
    for key in _model_keys(config):
        try:
            model = config.model(key)
        except (KeyError, ValueError) as exc:
            rows.append({"key": key, "error": _reason(exc)})
            continue
        rows.append({"key": key, "provider": model.provider.name, "model": model.model,
                     "context_tokens": model.context_tokens,
                     "where": "local" if is_local_url(model.provider.base_url) else "api"})
    if args.json:
        print_json(rows)
        return 0
    width = max((len(row["key"]) for row in rows), default=0)
    for row in rows:
        if "error" in row:
            print(f"{row['key']:<{width}}  invalid: {row['error']}")
            continue
        context = f"ctx {row['context_tokens']}" if row["context_tokens"] else "ctx -"
        print(f"{row['key']:<{width}}  {row['where']:<5}  {row['provider']}/{row['model']}  {context}")
    return 0


def cmd_estimate(args: argparse.Namespace) -> int:
    """Expected tokens, cost and duration of one job (or plan) per step, from the repo's past jobs (DESIGN §23)."""
    from .events import EventBus
    from .orchestrator import Factory

    config = load_config()
    repo = _known_repo(config, args.repo)
    try:
        result = Factory(config, EventBus()).estimate(repo, args.kind, args.profile, args.preset)
    except (KeyError, ValueError) as exc:
        raise CliError(_reason(exc)) from None
    if args.json:
        print_json(result)
        return 0
    width = max((len(row["agent"]) for row in result["roles"]), default=0)
    for row in result["roles"]:
        tokens = row["tokens"]
        cost = f"${row['cost_usd']:.2f}" if row["priced"] else "no price"
        status = row.get("independence")
        guarded = (f"  (instead of {row['instead_of']}: independent alternate)" if status == "alternate"
                   else f"  (independence {status})" if status else "")
        print(f"{row['agent']:<{width}}  {row['model']} ({row['provider']}/{row['model_id']})  "
              f"{tokens['input_tokens']:,} in / {tokens['output_tokens']:,} out  {cost}  "
              f"~{_minutes(row['seconds'])}  [{row['basis']}]{guarded}")
    for row in result.get("independence") or []:   # the independence guard (DESIGN §23)
        moves = [f"{judge} moves to {model}" for judge, model in row["alternate"].items()]
        moves += [f"{judge} runs with reduced independence on {model}" for judge, model in row["reduced"].items()]
        moves += [f"{judge} shares the builder's family on {model} (accepted)"
                  for judge, model in (row.get("accepted") or {}).items()]
        if moves:
            builder = row["builder"]
            print(f"independence: builder on {builder['model']} ({builder['family']}, {builder['via']}): "
                  + "; ".join(moves))
    total = result["total"]
    unpriced = "" if total["priced"] else " + steps without prices"
    jobs = f" for {result['jobs']} jobs" if result["kind"] == "plan" else ""
    profile = f", profile {result['profile']}" if result.get("profile") else ""
    print(f"total{jobs}: ${total['cost_usd']:.2f}{unpriced}, ~{_minutes(total['seconds'])} "
          f"({result['basis']}{profile})")
    for note in result["notes"]:
        hint(note)
    return 0


def _minutes(seconds: float) -> str:
    return f"{seconds / 60:.0f} min" if seconds >= 60 else f"{seconds:.0f} s"


def cmd_models_test(args: argparse.Namespace) -> int:
    """Probe one model entry (plain reply + tool call); exit 1 unless it answers and calls tools."""
    config = load_config()
    keys = _model_keys(config)
    if args.key not in keys:
        raise CliError(f"unknown model {args.key!r} (known: {', '.join(sorted(keys))})")
    from .settings import test_model

    result = test_model(config, args.key)
    ok = bool(result.get("ok"))
    parts = ["ok" if ok else "FAILED"]
    if result.get("latency_ms") is not None:
        parts.append(f"{result['latency_ms']} ms")
    tool_call = result.get("tool_call")
    if tool_call is not None:
        parts.append("tool calls: " + ("yes" if tool_call else "NO"))
    if result.get("tokens_per_second"):
        parts.append(f"{float(result['tokens_per_second']):.1f} tok/s")
    print(f"{args.key}: {', '.join(parts)}")
    if result.get("text"):
        print(f"  reply: {_clip(str(result['text']).strip(), 200)}")
    if result.get("error"):
        print(f"  error: {result['error']}")
    if ok and tool_call is False:
        hint("This model does not call tools here; agents cannot work with it. For llama.cpp, start "
             "llama-server with --jinja; otherwise try another model or chat template.")
    return 0 if ok and tool_call is not False else 1


def cmd_snapshot(args: argparse.Namespace) -> int:
    """Inventory this server read-only (the first Blueprint capture) into snapshot.json and overview.md."""
    from .blueprint.collect import UnsafeCommand, collect, write_snapshot

    hint("Collecting a read-only inventory of this host (allow-listed commands only; this takes a minute)"
         + (", with sudo -n where root sees more." if args.sudo else "."))
    try:
        snapshot = collect(sudo=args.sudo)
    except UnsafeCommand as exc:   # the probe list itself asked for something outside the allowlist
        raise CliError(f"the capture stopped; this command was refused and never ran: {exc}") from None
    try:
        paths = write_snapshot(snapshot, Path(args.out))
    except OSError as exc:
        raise CliError(f"cannot write the snapshot to {args.out}: {exc}") from None
    for path in paths:
        print(path)
    probes = snapshot["summary"]["probes"]
    hint(f"{probes['total']} probes: " + ", ".join(f"{n} {status}" for status, n in probes.items()
                                                    if status != "total") + ".")
    hint("Read snapshot.json before you share it: redaction is pattern-based, not proof.")
    return 0


def cmd_capture(args: argparse.Namespace) -> int:
    """Bundle the harness's own logs (and containers' logs) for a time window, read-only, into --out."""
    from .capture import CaptureError, bundle, parse_when
    from .events import env_secret_values

    try:
        config = Config.load()
    except ValueError as exc:
        raise CliError(str(exc)) from None
    try:
        secrets = config.secret_values()
    except Exception:  # noqa: BLE001 - an unreadable key file must not stop the capture; env values still count
        secrets = []
    try:
        since, until = parse_when(args.since), parse_when(args.until)
        manifest = bundle(config.data_dir, Path(args.out), since, until, args.container or [],
                          secrets=[*secrets, *env_secret_values()])
    except CaptureError as exc:
        raise CliError(str(exc)) from None
    except OSError as exc:
        raise CliError(f"cannot write the capture to {args.out}: {exc}") from None
    if args.json:
        print_json(manifest)
        return 0
    out = Path(os.path.expanduser(args.out))
    print(out / "manifest.json")
    events = sum(row["events"] for row in manifest["harness"])
    hint(f"{events} harness events from {len(manifest['harness'])} run logs between {manifest['window']['since']} "
         f"and {manifest['window']['until']}.")
    for row in manifest["containers"]:
        hint(f"container {row['name']}: " + (f"{row['lines']} lines" if row.get("taken") else f"not taken ({row['why']})"))
    hint("Read the bundle before you share it: redaction is pattern-based, not proof.")
    return 0


def cmd_drift(args: argparse.Namespace) -> int:
    """(server) The drift sources the tracker reads: their status, or check one now."""
    client = Client(load_config())
    if args.action == "check":
        result = client.post(f"/api/drift-sources/{_quote(args.name)}/check", {})
        if args.json:
            print_json(result)
            return 0
        print(f"{result.get('name')}  {result.get('status')}  {result.get('detail') or ''}".rstrip())
        changes = result.get("result") or {}
        for key in ("opened", "reopened", "closed"):
            if changes.get(key):
                print(f"  {key}: {', '.join(changes[key])}")
        return 0
    rows = client.get("/api/drift-sources").get("sources") or []
    if args.json:
        print_json(rows)
        return 0
    for row in rows:
        print(f"{row['name']}  {row.get('status')}  observed {row.get('observed_at') or '-'}  "
              f"{len(row.get('present') or [])} findings  next {row.get('next_at') or '-'}"
              + (f"  ({row['detail']})" if row.get("detail") else ""))
    if not rows:
        hint("no drift source is configured (drift_sources in config.json)")
    return 0


SNAPRAID_WAIT = {"status": 300.0, "health": 600.0, "diff": 4 * 3600.0}


def cmd_snapraid(args: argparse.Namespace) -> int:
    """(server) Disk protection with SnapRAID (DESIGN §41): the card in plain text (with the SnapRAID Daemon's facts
    on the daemon route), a check now, the reader's runs, a guard report (counts and reasons only: file names stay in
    the cockpit), and the copy blocks that install or remove the reader or Seraphina's link to the daemon. There is
    no action command: an approval must follow seeing what the cockpit shows."""
    action = getattr(args, "snapraid_action", None)
    if action == "helper":
        return _snapraid_helper(args)
    if action == "link":
        return _snapraid_link(args)
    client = Client(load_config())
    if action == "check-now":
        what = args.what or "status"
        started = client.post("/api/snapraid/refresh", {"what": what})
        if started.get("busy"):
            raise CliError(f"another check is running ({started.get('refreshing')}); try again when it ends")
        hint(f"checking {what}…")
        deadline = time.monotonic() + SNAPRAID_WAIT[what]
        view = client.get("/api/snapraid")
        while view.get("refreshing") and time.monotonic() < deadline:
            time.sleep(2.0)
            view = client.get("/api/snapraid")
        return _snapraid_print(args, view)
    if action == "jobs":
        if args.id:
            result = client.get(f"/api/snapraid/jobs/{_quote(args.id)}")
            if args.json:
                print_json(result)
                return 0
            job = result.get("job") or {}
            print(f"{job.get('job')}  {job.get('verb')} ({job.get('command')})  {job.get('outcome')}  exit "
                  f"{job.get('exit')}  {job.get('started')} → {job.get('ended') or '…'}")
            print(f"  {job.get('words')}")
            return 0
        rows = client.get("/api/snapraid/jobs").get("jobs") or []
        if args.json:
            print_json(rows)
            return 0
        for job in rows:
            print(f"{job.get('job')}  {job.get('verb'):<7} {job.get('command'):<7} {job.get('outcome') or '…':<12} "
                  f"{job.get('started')}")
        if not rows:
            hint("the reader has no runs yet")
        return 0
    if action == "guard":
        view = client.get("/api/snapraid")
        guard = view.get("guard") or {}
        if args.id and args.id != guard.get("guard"):
            guard = (client.get(f"/api/snapraid/guard/{_quote(args.id)}?offset=0").get("guard") or {})
        if args.json:
            print_json(guard)
            return 0
        if not guard.get("verdict"):
            hint("no guard report yet: run `factory snapraid check-now diff`")
            return 0
        words = {"would_hold": "this run would be held", "would_wait": "this run would wait",
                 "would_run": "this run would go ahead", "nothing_to_do": "nothing to protect", "error": "error"}
        print(f"{guard.get('guard')}  {words.get(guard.get('verdict'), guard.get('verdict'))}  {guard.get('at')}"
              + (f"  ({', '.join(guard.get('reasons') or [])})" if guard.get("reasons") else ""))
        for check in guard.get("checks") or []:
            print(f"  {check.get('id'):<4} {check.get('result'):<8} {check.get('words')}")
        array = ((guard.get("counts") or {}).get("array") or {})
        if array:
            print(f"  across the disks: {array.get('added', 0)} added, {array.get('removed', 0)} removed, "
                  f"{array.get('updated', 0)} changed, {array.get('moved', 0)} moved; {array.get('lost', 0)} would "
                  f"be lost")
        hint("file names stay in the cockpit (Backups, the change list); this phase only reports")
        return 0
    return _snapraid_print(args, client.get("/api/snapraid"))


def _snapraid_print(args: argparse.Namespace, view: dict) -> int:
    from .snapraid import card_lines

    if args.json:
        print_json(view)
        return 0
    for line in card_lines(view):
        print(line)
    if view.get("state") != "ready":
        hint({"absent": "SnapRAID is not installed (optional)",
              "no_reader": "install the reader: factory snapraid helper install",
              "off": "turn the panel on: snapraid.enabled in Settings, once the reader is installed"}
             .get(view.get("state"), ""))
    return 0


def _snapraid_link(args: argparse.Namespace) -> int:
    """`factory snapraid link install|check|remove`: the link's copy blocks (BACKUPS_SNAPRAID.md §10.4)."""
    client = Client(load_config())
    if args.link_action == "check":
        result = client.post("/api/snapraid/setup/link-check", {})
        if args.json:
            print_json(result)
            return 0
        print(result.get("words") or ("ok" if result.get("ok") else "not ok"))
        return 0 if result.get("ok") else 1
    if args.link_action == "install":
        link = client.post("/api/snapraid/setup/link-stage", {}).get("link") or {}
        blocks = link.get("blocks") or []
    else:
        blocks = (client.get("/api/snapraid/setup").get("link") or {}).get("removal") or []
    if args.json:
        print_json(blocks)
        return 0
    _print_blocks(blocks, "factory snapraid link check")
    return 0


def _print_blocks(blocks: list, check_command: str) -> None:
    for block in blocks:
        print(f"# {block['id']}. {block['title']}")
        for command in block.get("commands") or []:
            print(command)
        if block.get("action") in ("check", "link-check"):
            print(check_command)
        if block.get("expect"):
            print(f"#   expect: {block['expect']}")
        if block.get("then"):
            print("#   then, only once it shows what you expect (the move runs only when its check passes again):")
            for command in block["then"]:
                print(command)
        if block.get("note"):
            print(f"#   note: {block['note']}")
        if block.get("if_not"):
            print(f"#   if not: {block['if_not']}")
        print()


def _snapraid_helper(args: argparse.Namespace) -> int:
    """`factory snapraid helper install|check|remove`: the copy blocks (the harness never runs sudo)."""
    client = Client(load_config())
    if args.helper_action == "check":
        result = client.post("/api/snapraid/setup/check", {})
        if args.json:
            print_json(result)
            return 0
        print(result.get("words") or ("ok" if result.get("ok") else "not ok"))
        return 0 if result.get("ok") else 1
    if args.helper_action == "install":
        setup = client.post("/api/snapraid/setup/stage", {})
        blocks = setup.get("blocks") or []
    else:
        setup = client.get("/api/snapraid/setup")
        blocks = setup.get("removal") or []
    if args.json:
        print_json(blocks)
        return 0
    for block in blocks:
        print(f"# {block['id']}. {block['title']}")
        for command in block.get("commands") or []:
            print(command)
        if block.get("action") == "check":
            print("factory snapraid helper check")
        if block.get("expect"):
            print(f"#   expect: {block['expect']}")
        if block.get("then"):
            print("#   then, only once it shows what you expect (the move runs only when its check passes again):")
            for command in block["then"]:
                print(command)
        if block.get("note"):
            print(f"#   note: {block['note']}")
        if block.get("if_not"):
            print(f"#   if not: {block['if_not']}")
        print()
    if args.helper_action == "install":
        hint(f"staged in {setup.get('setup_dir')} (0700); run each block yourself, with sudo")
    return 0


def cmd_custody(args: argparse.Namespace) -> int:
    """(server) The providers under GPU custody: the register's residency and the leases this harness holds."""
    view = Client(load_config()).get("/api/custody" + ("?refresh=1" if args.refresh else ""))
    if args.json:
        print_json(view)
        return 0
    for row in view.get("providers") or []:
        register = row.get("register") or {}
        residency = register.get("residency") or {}
        resident = ", ".join(f"{item['provider']}/{item['model']}" for item in residency.get("resident") or [])
        follows = f"  (follows {row['custody_from']}: the same server)" if row.get("custody_from") else ""
        print(f"{row['provider']}  needs a lease: {'yes' if row.get('needs_lease') else 'no'}  "
              f"generation {register.get('generation')}  resident: {resident or '-'}{follows}")
        held = row.get("held_wait_seconds")
        if row.get("needs_lease"):
            print(f"  HELD (the card is held by someone else): {f'waits up to {held:g}s' if held else 'no wait'}; "
                  f"FULL (the card cannot fit the model now): the next build at once")
        fenced = register.get("fenced")
        if fenced:
            print(f"  the register's generation went back from {fenced['seen']} to {fenced['answered']} (since "
                  f"{fenced['since']}): its answers are refused until it passes {fenced['seen']}")
        for lease in register.get("leases") or []:
            print(f"  lease {lease['lease_id']}  {lease['model']}  {lease['state']}  until {lease['until']}  "
                  f"({lease['purpose']})")
        refused = [entry for entry in register.get("history") or []
                   if entry.get("op") == "claim" and entry.get("outcome") == "denied"][-3:]
        for entry in refused:   # the last few refusals, in the register's code and plain words
            words = f" ({entry['words']})" if entry.get("words") else ""
            print(f"  refused {entry.get('at')}  {entry.get('model')}  {entry.get('reason')}{words}  "
                  f"({entry.get('purpose')})")
    if not view.get("providers"):
        hint("no provider names a custody register (providers.<name>.custody)")
    return 0


def cmd_confirm(args: argparse.Namespace) -> int:
    """List a job's product assumptions, or confirm them: you typing this is the person deciding."""
    repo, job_id = args.ref
    if args.all and args.lines:
        raise CliError("give the lines to confirm or --all, not both")
    client = Client(load_config())
    path = f"/api/jobs/{_quote(repo, job_id)}"
    if args.all or args.lines:
        payload = {"all": True} if args.all else {"lines": args.lines}
        view = _unwrap(client.post(f"{path}/confirm", payload), "assumptions")
    else:
        view = client.get(f"{path}/assumptions")
    for line in view.get("confirmed") or []:
        print(f"[x] {line}")
    for line in view.get("unconfirmed") or []:
        print(f"[ ] {line}")
    if not view.get("marked"):
        hint("spec.md marks no assumption (product): nothing to confirm.")
    elif view.get("unconfirmed"):
        hint(f"Each [ ] line is a decision only a person can make. Confirm with `factory confirm {repo}/{job_id} "
             "--all`, or name the lines to confirm.")
    return 0


# -- parser ---------------------------------------------------------------------------


DESCRIPTION = """\
Software Factory: a self-hosted line of coding agents with a web GUI.

  feature -> spec-writer -> builder -> reviewers (all in parallel) -> approver -> merge
                                ^------ any CHANGES: resume the builder --'   or needs-human
"""

EPILOG = """\
Commands marked (server) talk to a running `factory serve` with the config's token.
Config: $FACTORY_CONFIG or ~/.config/software-factory/config.json; API keys in the
env file next to it (NAME=value lines). Run `factory <command> --help` for details.
"""


def build_parser() -> argparse.ArgumentParser:
    """The argparse tree for every command."""
    parser = argparse.ArgumentParser(prog="factory", description=DESCRIPTION, epilog=EPILOG,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"factory {__version__}")
    commands = parser.add_subparsers(title="commands", metavar="<command>")

    def add(group: Any, name: str, func: Callable[[argparse.Namespace], int] | None, summary: str,
            details: str = "") -> argparse.ArgumentParser:
        sub = group.add_parser(name, help=summary, description=f"{summary}\n\n{details}".strip(),
                               formatter_class=argparse.RawDescriptionHelpFormatter)
        if func is not None:
            sub.set_defaults(func=func)
        return sub

    sub = add(commands, "init", cmd_init, "write the config with defaults and a new server token",
              "Prints the config path, then what to do next.")
    sub.add_argument("--force", action="store_true", help="overwrite an existing config (new token)")
    sub.add_argument("--preset", metavar="NAME", help=f"starting model routing: {PRESETS_HELP}")
    sub.add_argument("--data-dir", metavar="DIR", help="where jobs, worktrees and logs live "
                     "(default ~/.local/share/software-factory)")

    sub = add(commands, "serve", cmd_serve, "run the web server and the factory workers",
              "Holds <data_dir>/factory.lock, so only one factory runs per data dir, and records\n"
              "its address there: CLI commands find it on a --host/--port override too.\n"
              "Binds 127.0.0.1 by default; reach it from elsewhere through an SSH tunnel.")
    sub.add_argument("--host", help="bind address for this run (default: server.host)")
    sub.add_argument("--port", type=port_number, help="port for this run (default: server.port, 8765)")

    add(commands, "url", cmd_url, "print the login URL (with the token) and the SSH tunnel command")

    sub = add(commands, "doctor", cmd_doctor, "check git, python, providers, keys, agents and drivers",
              "Runs without the server. Exit status 1 if any error-level check fails.")
    sub.add_argument("--probe", action="store_true", help="also send each routed model the Test probes "
                     "(a plain reply and a tool call; costs model calls)")
    sub.add_argument("--look-around", action="store_true", help="also look for model servers on this machine: "
                     "one GET /v1/models, without a key, to each program listening here (nothing is changed)")
    sub.add_argument("--json", action="store_true", help="print the checks as JSON")

    repo = add(commands, "repo", None, "add, list or remove repos")
    repo_actions = repo.add_subparsers(title="actions", metavar="<action>", required=True)
    sub = add(repo_actions, "add", cmd_repo_add, "register a git repo (its top-level directory)")
    sub.add_argument("path", help="path to the repo checkout")
    sub.add_argument("--name", help="repo name (default: derived from the directory name)")
    add(repo_actions, "list", cmd_repo_list, "list configured repos")
    sub = add(repo_actions, "remove", cmd_repo_remove, "unregister a repo (jobs and the repo stay on disk)")
    sub.add_argument("name", help="repo name")

    for name, func, summary, details in (
            ("submit", cmd_submit, "(server) queue a feature as a new job", "Prints <repo>/<job-id>."),
            ("run", cmd_run, "run one job in-process, without the server, and wait for it",
             "Prints one line per transition and a summary. Exit status: 0 merged or delivered,\n2 needs-human, "
             "1 failed, rejected or cancelled. Ctrl-C cancels the job. Needs factory.lock.\n"
             "Runs only this job: plans and chats left on disk wait for the next factory serve.")):
        sub = add(commands, name, func, summary, details)
        sub.add_argument("repo", help="repo name")
        sub.add_argument("feature", help='the feature request ("-" reads it from stdin)')
        sub.add_argument("--preset", metavar="NAME", help=f"route this job with a preset: {PRESETS_HELP}")
        sub.add_argument("--delivery", choices=("merge", "branch", "folder"), help=DELIVERY_HELP)
        sub.add_argument("--profile", metavar="NAME", help=PROFILE_HELP)

    sub = add(commands, "status", cmd_status, "(server) list jobs with stage, round and detail")
    sub.add_argument("repo", nargs="?", help="only this repo's jobs")
    sub.add_argument("--json", action="store_true", help="print the jobs' job.json documents")

    sub = add(commands, "approve", cmd_approve,
              "(server) approve a needs-human or delivered job: merge it (still refuses conflicts), or deliver a "
              "branch or folder job not yet delivered",
              "Must-do items that are not proven are never waived: the server refuses (HTTP 409) and lists\n"
              "them, unless --carry-open-items names them all; they then move to the repo's follow-ups\n"
              "(DESIGN.md §32).")
    sub.add_argument("ref", type=job_ref, metavar="<repo>/<id>", help="the job")
    sub.add_argument("--carry-open-items", action="store_true",
                     help="list the job's unproven must-do items and approve, moving them to follow-ups")
    for action, summary in (("retry", "(server) re-run a failed job from the step that failed"),
                            ("cancel", "(server) stop an active job")):
        add(commands, action, make_action(action), summary).add_argument(
            "ref", type=job_ref, metavar="<repo>/<id>", help="the job")
    sub = add(commands, "rework", make_action("rework", lambda args: {"note": text_arg(args.note)}),
              "(server) send a needs-human job back to the builder with a note")
    sub.add_argument("ref", type=job_ref, metavar="<repo>/<id>", help="the job")
    sub.add_argument("note", help='what to change ("-" reads it from stdin)')
    sub = add(commands, "reject", make_action("reject", lambda args: {"reason": args.reason}),
              "(server) drop a job, its worktree and its branch")
    sub.add_argument("ref", type=job_ref, metavar="<repo>/<id>", help="the job")
    sub.add_argument("reason", nargs="?", default="", help="why (optional)")
    sub = add(commands, "confirm", cmd_confirm, "(server) list a job's (product) assumptions, or confirm them",
              "Each spec.md line the spec-writer marked (product) is a decision only a person can make;\n"
              "the approver escalates on one nobody confirmed. Without lines or --all it lists them.\n"
              "The job's confirmed.md is written from what you confirm here (DESIGN.md §7).")
    sub.add_argument("ref", type=job_ref, metavar="<repo>/<id>", help="the job")
    sub.add_argument("lines", nargs="*", metavar="LINE", help="a spec.md line to confirm, as listed")
    sub.add_argument("--all", action="store_true", help="confirm every line not confirmed yet")

    sub = add(commands, "backlog", cmd_backlog, "(server) show a repo's backlog, add to it, or run the next item")
    sub.add_argument("repo", help="repo name")
    backlog_actions = sub.add_subparsers(title="actions", metavar="<action>", dest="action")
    add(backlog_actions, "add", cmd_backlog, "append an item").add_argument("text", help="one line")
    add(backlog_actions, "next", cmd_backlog, "start a job for the first unchecked item")

    sub = add(commands, "logs", cmd_logs, "print a job's agent transcript",
              "Reads the job's log files; with -f, then follows the running server's event stream\n"
              "until the job is merged, rejected or cancelled.")
    sub.add_argument("ref", type=job_ref, metavar="<repo>/<id>", help="the job")
    sub.add_argument("-f", "--follow", action="store_true", help="keep printing new events (server)")

    sub = add(commands, "plan", cmd_plan, "(server) have the lead split a large request into jobs",
              "Prints <repo>/<plan-id>. Read or edit plan.md when it is ready, then run it.")
    sub.add_argument("repo", help="repo name")
    sub.add_argument("request", help='the request ("-" reads it from stdin)')
    sub.add_argument("--delivery", choices=("merge", "branch", "folder"), help="for every job of the plan: "
                     + DELIVERY_HELP)
    sub.add_argument("--profile", metavar="NAME", help="for every job of the plan and its lead: " + PROFILE_HELP)

    sub = add(commands, "plans", cmd_plans, "(server) list plans, or run / cancel / retry one")
    sub.add_argument("--json", action="store_true", help="print the plans' plan.json documents")
    plan_actions = sub.add_subparsers(title="actions", metavar="<action>", dest="action")
    for action, summary in (("run", "submit the plan's jobs in order"), ("cancel", "stop the plan"),
                            ("retry", "re-run the lead after a failure")):
        add(plan_actions, action, make_plan_action(action), summary).add_argument(
            "ref", type=plan_ref, metavar="<repo>/<plan-id>", help="the plan")

    sub = add(commands, "agents", cmd_agents, "list the resolved agents, or create your own",
              "Agents are markdown files: shipped ones in agents/, yours in\n"
              "~/.config/software-factory/agents/ (also when $FACTORY_CONFIG moves the config)\n"
              "and a repo's .factory/agents/ (later wins).")
    sub.add_argument("--json", action="store_true", help="print the agents as JSON")
    sub.add_argument("--repo", help="include this repo's .factory/agents overrides")
    agent_actions = sub.add_subparsers(title="actions", metavar="<action>", dest="action")
    new = add(agent_actions, "new", cmd_agents_new, "copy an agent to your agents dir and print the path")
    new.add_argument("name", help="new agent name, e.g. reviewer-perf")
    new.add_argument("--from", dest="source", default="reviewer-code", metavar="AGENT",
                     help="agent to copy (default: reviewer-code)")

    sub = add(commands, "personas", cmd_personas, "list personas (Open WebUI profiles such as Kate), or import one",
              "A persona is an agent file in ~/.config/software-factory/agents/ (or agents/) with\n"
              "openwebui: <workspace model id>: its chats run through Open WebUI (DESIGN.md §33).")
    sub.add_argument("--json", action="store_true", help="print the personas as JSON")
    persona_actions = sub.add_subparsers(title="actions", metavar="<action>", dest="action")
    card = add(persona_actions, "card", cmd_personas_card, "print a persona's profile card and what Open WebUI lists now")
    card.add_argument("name", help="persona name, e.g. kate")
    card.add_argument("--json", action="store_true", help="print the card as JSON")
    imp = add(persona_actions, "import", cmd_personas_import,
              "(server) import an Open WebUI workspace model as a read-only profile card",
              "From the Export file Open WebUI's workspace saves, or with --model alone from\n"
              "GET /api/v1/models/export (the owner's key). --snapshot also writes an offline copy\n"
              "that runs on the harness's own routing: only when you want that mode.")
    imp.add_argument("file", nargs="?", help="the Export file (a JSON array of model records)")
    imp.add_argument("--model", metavar="ID", help="the workspace model's id (needed when the file holds several)")
    imp.add_argument("--snapshot", metavar="NAME", help="also write the offline snapshot persona NAME")
    imp.add_argument("--snapshot-model", metavar="KEY", help="the local models entry the snapshot runs on")
    imp.add_argument("--user-name", metavar="NAME", help="what {{USER_NAME}} becomes in the snapshot")
    chk = add(persona_actions, "check", cmd_personas_check,
              "(server) run the drift check: has the live preset changed since it was last seen?")
    chk.add_argument("name", nargs="?", help="only this persona")
    chk.add_argument("--json", action="store_true", help="print the results as JSON")
    before = add(persona_actions, "before-import", cmd_personas_before_import,
                 "(server) attach the operator's receipts to a persona's import as its changes before import",
                 "At the first import the harness had no earlier copy, so the import is the baseline and\n"
                 "there is no diff. The card lists the receipts attached here, by id only (DESIGN.md §33).")
    before.add_argument("name", help="persona name")
    before.add_argument("--attach", nargs="+", metavar="ID", help="receipt ids the harness has read")
    before.add_argument("--detach", nargs="+", metavar="ID", help="receipt ids to take off again")
    before.add_argument("--by", choices=("owner", "operator"), default="owner", help="who attaches (default owner)")
    before.add_argument("--json", action="store_true", help="print the import record as JSON")
    ledger = add(persona_actions, "ledger", cmd_personas_ledger,
                 "(server) the read ledger of a persona's own memory: versions read, show, diff, prepare a restore, "
                 "erase one",
                 "With memory.external.ledger true the harness records each read of the persona's own\n"
                 "memory and keeps a private copy of each file version. It never writes her store: a\n"
                 "restore gives back the earlier text for her own tools or you to apply. --erase removes\n"
                 "one version's copy and keeps only its sha256.")
    ledger.add_argument("name", help="persona name")
    what = ledger.add_mutually_exclusive_group()
    what.add_argument("--show", metavar="SHA256", help="print one kept version")
    what.add_argument("--diff", nargs=2, metavar=("OLDER", "NEWER"), help="a diff of two kept versions")
    what.add_argument("--restore", metavar="SHA256", help="prepare a restore of one kept version")
    what.add_argument("--erase", metavar="SHA256", help="erase one version: its copy goes, its sha256 stays")
    ledger.add_argument("--file", metavar="NAME", help="with --restore: the file, when the version had several names")
    ledger.add_argument("--json", action="store_true", help="print the result as JSON")

    sub = add(commands, "activity", cmd_activity, "what an agent or persona did since a time, with evidence",
              "Merges, oldest first: an operator's change receipts (reported by her), the agent's tracker\n"
              "transitions, the messages and wakes audit lines that name it, and its run starts and ends.\n"
              "Each entry names the file (and line) it was read from. Works without the server (read only).")
    sub.add_argument("--agent", required=True, metavar="NAME", help="an agent or persona name, e.g. builder or a "
                     "persona's name (persona:<name> and agent:<name> work too)")
    sub.add_argument("--since", metavar="TIME", help="an ISO time or date, or a span back from now: 30m, 24h, 7d, 2w")
    sub.add_argument("--until", metavar="TIME", help="the same forms (default: now)")
    sub.add_argument("--limit", type=int, default=500, help="at most this many entries (1-5000, default 500)")
    sub.add_argument("--after", metavar="CURSOR", help="continue a cut list: the next_after value it ended with")
    sub.add_argument("--json", action="store_true", help="print the result as JSON")

    sub = add(commands, "operator", cmd_operator, "the operator persona: who it is and her change receipts",
              "The owner marks one persona the operator (agents.<name>.role: \"operator\"); she writes one\n"
              "JSON receipt per change into operator.receipts_dir, which the harness only reads (DESIGN.md §36).")
    sub.add_argument("--json", action="store_true", help="print the state as JSON")
    operator_actions = sub.add_subparsers(title="actions", metavar="<action>", dest="action")
    rec = add(operator_actions, "receipts", cmd_operator_receipts, "list her receipts, newest first",
              "Every receipt is reported by the operator: the harness never re-runs her checks. What she\n"
              "lists as not verified is shown first. Works without the server (read only).")
    rec.add_argument("--since", metavar="TIME", help="only receipts from this time on (an ISO time, or 24h, 7d)")
    rec.add_argument("--limit", type=int, default=50, help="at most this many (default 50)")
    rec.add_argument("--rescan", action="store_true", help="(server) read the directory again first")
    rec.add_argument("--json", action="store_true", help="print the receipts as JSON")
    scan = add(operator_actions, "scan", cmd_operator_scan, "(server) read the receipts directory now")
    scan.add_argument("--json", action="store_true", help="print the scan as JSON")

    sub = add(commands, "notify", None, "phone messages: channel status, a test notice, or report an event")
    notify_actions = sub.add_subparsers(title="actions", metavar="<action>", dest="action")
    status = add(notify_actions, "status", cmd_notify_status, "(server) the phone channels and their health")
    status.add_argument("--json", action="store_true", help="print the state as JSON")
    test = add(notify_actions, "test", cmd_notify_test, "(server) send a test notice with a Got it button")
    test.add_argument("--channel", metavar="NAME", help="one channel (default: each enabled one)")
    event = add(notify_actions, "event", cmd_notify_event, "(server) report an event as the owner",
                "Kinds: backup.failed, backup.ok, disk.low, service.down, service.up. A failure opens an\n"
                "incident (one alert, a reminder after 6 hours); its recovery closes it.")
    event.add_argument("kind", help="e.g. backup.failed")
    event.add_argument("--subject", metavar="S", help="what it is about, e.g. nightly")
    event.add_argument("--detail", metavar="TEXT", help="at most 200 characters, shown in the cockpit only")

    sub = add(commands, "wake", None, "stop or restart Seraphina (the persona that messages you)")
    wake_actions = sub.add_subparsers(title="actions", metavar="<action>", dest="action")
    poke = add(wake_actions, "poke", cmd_wake_poke, "wake a persona now through the spool (works without the server)")
    poke.add_argument("persona", help="e.g. seraphina")
    poke.add_argument("--reason", metavar="TEXT", help="at most 200 characters; the persona reads it as untrusted data")
    pause = add(wake_actions, "pause", cmd_wake_pause, "stop Seraphina at once (works without the server)")
    pause.add_argument("--persona", metavar="P", help="stop only this persona")
    pause.add_argument("--until", metavar="HH:MM", help="with --persona: until this local time")
    resume = add(wake_actions, "resume", cmd_wake_resume, "(server) restart Seraphina; offline, remove the stop file")
    resume.add_argument("--persona", metavar="P", help="restart only this persona")
    wstatus = add(wake_actions, "status", cmd_wake_status, "whether Seraphina is stopped")
    wstatus.add_argument("--json", action="store_true", help="print the state as JSON")

    sub = add(commands, "secret", None, "store a secret in the env file without it ever being shown")
    secret_actions = sub.add_subparsers(title="actions", metavar="<action>", dest="action")
    sset = add(secret_actions, "set", cmd_secret_set, "read NAME's value from standard input (never echoed)",
               "Example: factory secret set SERAPHINA_TELEGRAM_BOT_TOKEN --stdin (then paste and press Enter).")
    sset.add_argument("name", help="e.g. SERAPHINA_TELEGRAM_BOT_TOKEN")
    sset.add_argument("--stdin", action="store_true", help="read the value from standard input")

    sub = add(commands, "snapshot", cmd_snapshot, "inventory this server read-only into snapshot.json + overview.md",
              "The first Blueprint capture (DESIGN.md §28): host, GPUs, storage, network, packages,\n"
              "systemd, cron, logging, Docker and compose files, Cockpit, Cosmos, Tailscale and backups.\n"
              "Only allow-listed read-only commands run; nothing is installed, started or changed, and\n"
              "secrets are redacted. Works without the config or the server.")
    sub.add_argument("--out", required=True, metavar="DIR", help="where to write snapshot.json and overview.md "
                     "(created 0700; files 0600)")
    sub.add_argument("--sudo", action="store_true", help="run root-only probes through `sudo -n` (never prompts; "
                     "falls back to this user when sudo needs a password)")

    sub = add(commands, "capture", cmd_capture, "bundle the harness's logs for a time window, read-only (capture "
              "before fix)",
              "Copies the run logs' diagnostic events (run starts and ends, failed calls with their status,\n"
              "response body and request ids, waits, log lines, persona turn windows) between --since and\n"
              "--until, and with --container that container's `docker logs` when this user may run it.\n"
              "Everything is redacted; nothing is written anywhere but --out (new or empty). Take it before\n"
              "you restart, edit or retry anything. Works without the server.")
    sub.add_argument("--since", required=True, metavar="WHEN", help="now, 15m, 2h, 1d or an ISO-8601 time")
    sub.add_argument("--until", default="now", metavar="WHEN", help="the same forms (default: now)")
    sub.add_argument("--container", action="append", metavar="NAME", help="also this container's logs (repeatable)")
    sub.add_argument("--out", required=True, metavar="DIR", help="a new or empty directory (created 0700; files 0600)")
    sub.add_argument("--json", action="store_true", help="print the manifest as JSON")

    sub = add(commands, "drift", cmd_drift, "(server) drift sources: their status, or check one now",
              "Findings become system-scope tracker items (source drift:<name>), closed only when a fresh\n"
              "observation no longer reports them.")
    sub.add_argument("--json", action="store_true", help="print JSON")
    drift_actions = sub.add_subparsers(title="actions", metavar="<action>", dest="action")
    dcheck = add(drift_actions, "check", cmd_drift, "(server) check one drift source now")
    dcheck.add_argument("name", help="the drift source's name")
    dcheck.add_argument("--json", action="store_true", help="print the result as JSON")

    sub = add(commands, "snapraid", cmd_snapraid, "(server) disk protection with SnapRAID",
              "The card in plain text: the light and its reason, the disks, the SnapRAID Daemon's facts or the last\n"
              "guard report. Actions need your go-ahead in the cockpit; there is no action command. File names stay\n"
              "in the cockpit.")
    sub.add_argument("--json", action="store_true", help="print JSON")
    snapraid_actions = sub.add_subparsers(title="actions", metavar="<action>", dest="snapraid_action")
    check = add(snapraid_actions, "check-now", cmd_snapraid, "(server) read now: status, health or diff (a guard "
                "report: would a run be held?)")
    check.add_argument("what", nargs="?", choices=("status", "health", "diff"), help="what to read (default status)")
    check.add_argument("--json", action="store_true", help="print JSON")
    jobs = add(snapraid_actions, "jobs", cmd_snapraid, "(server) the reader's runs, or one")
    jobs.add_argument("id", nargs="?", help="a run id such as J-12")
    jobs.add_argument("--json", action="store_true", help="print JSON")
    guard = add(snapraid_actions, "guard", cmd_snapraid, "(server) the last guard report: counts and reasons only")
    guard.add_argument("id", nargs="?", help="a guard report id such as G-3")
    guard.add_argument("--json", action="store_true", help="print JSON")
    helper = add(snapraid_actions, "helper", cmd_snapraid, "(server) the copy blocks that install or remove the "
                 "disk protection reader, or check it")
    helper.add_argument("helper_action", choices=("install", "check", "remove"))
    helper.add_argument("--json", action="store_true", help="print JSON")
    link = add(snapraid_actions, "link", cmd_snapraid, "(server) the copy blocks that install or remove Seraphina's "
               "link to the SnapRAID Daemon, or check it")
    link.add_argument("link_action", choices=("install", "check", "remove"))
    link.add_argument("--json", action="store_true", help="print JSON")

    sub = add(commands, "custody", cmd_custody, "(server) GPU custody: residency and the leases this harness holds")
    sub.add_argument("--refresh", action="store_true", help="read residency from the register now")
    sub.add_argument("--json", action="store_true", help="print JSON")

    sub = add(commands, "models", cmd_models, "list model entries, or test one")
    sub.add_argument("--json", action="store_true", help="print the models as JSON")
    model_actions = sub.add_subparsers(title="actions", metavar="<action>", dest="action")
    add(model_actions, "test", cmd_models_test, "send a plain and a tool-call probe to a model entry",
        "Exit status 1 unless the model answers and calls the probe tool.").add_argument(
        "key", help="a key of models, e.g. local or opus")

    sub = add(commands, "estimate", cmd_estimate, "estimate a job's or plan's tokens, cost and duration per step",
              "From the repo's past jobs (the median per step once three have it, else a default table),\n"
              "the models' prices and the providers' slots. Nothing runs.")
    sub.add_argument("repo", help="repo name")
    sub.add_argument("--kind", choices=("feature", "plan"), default="feature", help="one job (default) or a plan")
    sub.add_argument("--profile", metavar="NAME", help=PROFILE_HELP)
    sub.add_argument("--preset", metavar="NAME", help=f"route with a preset: {PRESETS_HELP}")
    sub.add_argument("--json", action="store_true", help="print the estimate as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse argv, run the command and return its exit status."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "func", None) is None:
        parser.print_help(sys.stderr)
        return 2
    load_env_file(default_config_path().parent / ENV_FILE)
    try:
        return args.func(args)
    except CliError as exc:
        print(f"factory: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        # stdout went away (`factory logs ... | head`): exit without a traceback.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 1


if __name__ == "__main__":
    sys.exit(main())
