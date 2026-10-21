"""Stdio MCP client: JSON-RPC 2.0, one JSON object per line.

The builtin harness exposes an MCP server's tools to the model as
``mcp__<server>__<tool>``. This module only speaks the protocol: it spawns the
server, runs the initialize handshake, lists the tools and calls them.

Four daemon threads serve each server: a writer that owns its stdin (so no
caller, and never the stdout reader, blocks on a full pipe), a stdout reader
that routes responses and answers server requests, a stderr reader, and an
exit watcher that fails the session as soon as the server process exits.
"""

from __future__ import annotations

import collections
import itertools
import json
import os
import queue
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from .. import __version__
from .sandbox import Sandbox, bwrap_available, scrub_env

PROTOCOL_VERSION = "2025-06-18"
DEFAULT_STARTUP_TIMEOUT = 60.0
DEFAULT_CALL_TIMEOUT = 120.0
METHOD_NOT_FOUND = -32601
_CLOSE_GRACE = 2.0                 # seconds between close steps: stdin EOF, SIGTERM, SIGKILL
_DRAIN_SECONDS = 0.5               # after an exit, time to route output the server wrote before it
_WRITE_GRACE = 0.2                 # after a deadline, time a write in progress gets to finish
_MAX_MESSAGE_BYTES = 32 * 2**20    # one stdout line; a larger one fails the session
_STDERR_LINE_BYTES = 4096          # kept per stderr line; the rest of a longer line is dropped
_READ_CHUNK = 65536
_WRITE_CHUNK = 16384               # write progress is tracked per chunk
_LOG_LINE_CHARS = 2000
_STDERR_TAIL_LINES = 10            # kept to explain why a server died

# Write states of an outgoing message (guarded by McpClient._lock).
_QUEUED, _WRITING, _SENT, _DROPPED = "queued", "writing", "sent", "dropped"


class McpError(Exception):
    """An MCP server could not start, died, or answered with a JSON-RPC error."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class _Timeout(McpError):
    def __init__(self, method: str, request_id: int, seconds: float, sent: bool) -> None:
        super().__init__(f"MCP request {method} timed out after {seconds:g}s")
        self.request_id = request_id
        self.sent = sent   # False: the server never saw the request, so there is nothing to cancel


class _Pending:
    """One request waiting for its response (or for the session to fail)."""

    __slots__ = ("done", "response", "failure")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.response: dict | None = None
        self.failure: str | None = None


class _Outgoing:
    """One message queued for the writer thread."""

    __slots__ = ("data", "state", "written")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.state = _QUEUED
        self.written = threading.Event()   # set once the whole message is in the pipe


class McpClient:
    """One stdio MCP server process and the JSON-RPC session with it.

    ``cfg`` is duck-typed like ``config.McpServerConfig``: name, command, args,
    env, cwd, timeout, and optionally tools (allowlist, None = all) and
    startup_timeout. ``cwd`` is the agent's working directory, offered to the
    server as its only root (and used as the process cwd unless cfg.cwd is set).
    The server gets the factory's environment without secrets (``*_API_KEY``,
    ``*_TOKEN``, ``FACTORY_*`` ..., plus ``secret_names``) and then ``cfg.env``
    (a None value removes the variable), so a key the server needs must be set
    there explicitly. With a ``sandbox`` (the agent run's) the server is started
    the way that sandbox runs a command (Sandbox.wrap): inside bubblewrap for
    ``bwrap``, so it can write only what the agent's Bash can.
    """

    def __init__(self, cfg: Any, cwd: Path, log: Callable[[str], None] | None = None, *,
                 secret_names: Iterable[str] = (), sandbox: Sandbox | None = None) -> None:
        self.cfg = cfg
        self.name = str(cfg.name)
        self.cwd = Path(cwd)
        self._log = log
        self._secret_names = frozenset(secret_names)
        self._sandbox = sandbox
        allowlist = getattr(cfg, "tools", None)
        self._allow = None if allowlist is None else frozenset(allowlist)
        self._startup_timeout = float(getattr(cfg, "startup_timeout", None) or DEFAULT_STARTUP_TIMEOUT)
        self._call_timeout = float(getattr(cfg, "timeout", None) or DEFAULT_CALL_TIMEOUT)
        self._tools: list[dict] = []
        self._ids = itertools.count(1)
        self._lock = threading.Lock()        # guards _proc, _pending, _failure, _closed, _writing, states
        self._proc: subprocess.Popen | None = None
        self._pending: dict[int, _Pending] = {}
        self._failure: str | None = None     # once set, the session is unusable
        self._failed = threading.Event()     # set together with _failure
        self._closed = False
        self._outbox: queue.Queue[_Outgoing | None] = queue.Queue()
        self._writing = False                # the writer holds a message
        self._last_progress = time.monotonic()   # when it took that message or last got bytes into stdin
        self._threads: dict[str, threading.Thread] = {}
        self._stderr_tail: collections.deque = collections.deque(maxlen=_STDERR_TAIL_LINES)

    # -- public API -------------------------------------------------------------

    def start(self) -> None:
        """Spawn the server, initialize, send notifications/initialized and list all tools.

        Raises McpError (after cleaning up the process) if any step fails.
        Calling start() on a running client does nothing.
        """
        with self._lock:
            if self._closed:
                raise McpError(f"MCP server {self.name} is closed")
            if self._proc is not None:
                return
            self._proc = self._spawn()
        # The watcher joins the readers, so it starts last.
        for stream, target in (("stderr", self._read_stderr), ("stdout", self._read_stdout),
                               ("stdin", self._write_stdin), ("exit", self._watch_exit)):
            self._threads[stream] = threading.Thread(target=target, name=f"mcp-{self.name}-{stream}",
                                                     daemon=True)
            self._threads[stream].start()
        try:
            self._handshake()
        except McpError as exc:
            self.close()
            raise McpError(f"MCP server {self.name} failed to start: {exc}", exc.code) from exc

    def tools(self) -> list[dict]:
        """The server's allowlisted tools as [{name, description, inputSchema}]."""
        return [dict(tool) for tool in self._tools]

    def call(self, tool: str, arguments: dict, timeout: float | None = None) -> tuple[str, bool]:
        """Call a tool and return (text, is_error). Never raises: failures come back as errors.

        ``timeout`` defaults to cfg.timeout and bounds the whole call, including
        writing the request. On timeout a sent request is cancelled with
        notifications/cancelled.
        """
        if self._allow is not None and tool not in self._allow:
            return f"MCP tool {tool} is not in the tools allowlist of server {self.name}", True
        limit = self._call_timeout if timeout is None else timeout
        try:
            result = self._request("tools/call", {"name": tool, "arguments": arguments or {}}, limit)
        except _Timeout as exc:
            if exc.sent:
                self._send({"jsonrpc": "2.0", "method": "notifications/cancelled",
                            "params": {"requestId": exc.request_id, "reason": f"timed out after {limit:g}s"}})
            return f"MCP call timed out after {limit:g}s", True
        except McpError as exc:
            return str(exc), True
        return _format_result(result)

    def close(self) -> None:
        """Stop the server and every process in its group: stdin EOF, SIGTERM, SIGKILL. Idempotent."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            proc = self._proc
        self._fail_all(f"MCP server {self.name} is closed")
        if proc is None:
            return
        self._outbox.put(None)   # the writer closes stdin, which asks the server to exit
        for next_signal in (signal.SIGTERM, signal.SIGKILL, None):
            try:
                proc.wait(timeout=_CLOSE_GRACE)
                break
            except subprocess.TimeoutExpired:
                if next_signal is not None:
                    _signal_leader_group(proc, next_signal)
        _kill_group(proc.pid)    # helpers the server started may outlive it
        threads = dict(self._threads)
        for thread in threads.values():
            thread.join(timeout=_CLOSE_GRACE)
        for name, stream in (("stdin", proc.stdin), ("stdout", proc.stdout), ("stderr", proc.stderr)):
            thread = threads.get(name)
            if thread is None or not thread.is_alive():  # never close a stream a thread is blocked on
                _close_quietly(stream)

    # -- process ----------------------------------------------------------------

    def _spawn(self) -> subprocess.Popen:
        argv = [str(self.cfg.command), *(str(arg) for arg in (self.cfg.args or []))]
        cwd = Path(self.cfg.cwd or self.cwd)
        if self._sandbox is not None and self._sandbox.mode != "none":
            if not bwrap_available():
                raise McpError(f"cannot start MCP server {self.name}: the bwrap sandbox is configured but "
                               "bubblewrap is not usable on this host")
            argv = self._sandbox.wrap(argv, cwd)
        env = scrub_env(os.environ, self._secret_names)
        for key, value in (self.cfg.env or {}).items():
            if value is None:   # JSON null removes the variable, as for a CLI driver's env
                env.pop(str(key), None)
            else:
                env[str(key)] = str(value)
        try:
            # A new session lets close() signal wrapper scripts and their children together (bwrap does not
            # start another session, so a sandboxed server and its helpers stay in this group too).
            return subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, cwd=str(cwd), env=env, start_new_session=True)
        except OSError as exc:
            raise McpError(f"cannot start MCP server {self.name} ({argv[0]}): {exc}") from exc

    def _exit_reason(self, what: str) -> str:
        reason = f"MCP server {self.name} {what}"
        if self._stderr_tail:
            reason += "; stderr: " + " | ".join(self._stderr_tail)
        return reason[:_LOG_LINE_CHARS]

    # -- protocol ---------------------------------------------------------------

    def _handshake(self) -> None:
        init = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"roots": {"listChanged": False}},
            "clientInfo": {"name": "software-factory", "version": __version__},
        }, self._startup_timeout)
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        offered = self._list_tools()
        if self._allow is not None:
            names = {tool["name"] for tool in offered}
            for missing in sorted(self._allow - names):
                self._emit(f"allowlisted tool {missing} is not offered by the server")
            offered = [tool for tool in offered if tool["name"] in self._allow]
        self._tools = offered
        info = init.get("serverInfo") if isinstance(init.get("serverInfo"), dict) else {}
        self._emit(f"connected to {info.get('name', '?')} {info.get('version', '?')} "
                   f"(protocol {init.get('protocolVersion', '?')}, {len(offered)} tools)")

    def _list_tools(self) -> list[dict]:
        tools: list[dict] = []
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            page = self._request("tools/list", {} if cursor is None else {"cursor": cursor},
                                 self._startup_timeout)
            entries = page.get("tools")
            for entry in entries if isinstance(entries, list) else []:
                tool = _normalize_tool(entry)
                if tool is None:
                    self._emit(f"ignored malformed tool entry: {str(entry)[:200]}")
                else:
                    tools.append(tool)
            cursor = page.get("nextCursor")
            if not isinstance(cursor, str) or not cursor:
                return tools
            if cursor in seen:
                raise McpError(f"tools/list repeated cursor {cursor!r}")
            seen.add(cursor)

    def _request(self, method: str, params: dict, timeout: float) -> dict:
        """Send a request and wait at most ``timeout`` for its result; raises McpError (or _Timeout)."""
        pending = _Pending()
        with self._lock:
            if self._failure is not None:
                raise McpError(self._failure)
            if self._proc is None:
                raise McpError(f"MCP server {self.name} is not started")
            request_id = next(self._ids)
            self._pending[request_id] = pending
        outgoing = self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        if not pending.done.wait(timeout):
            if outgoing.state == _WRITING:
                outgoing.written.wait(_WRITE_GRACE)
            with self._lock:
                self._pending.pop(request_id, None)
                state = outgoing.state
                # Stuck: part of the request may be in the pipe (its framing is lost), or it
                # waited behind a write that made no progress during the whole timeout.
                stalled_for = time.monotonic() - self._last_progress
                stuck = state == _WRITING or (state == _QUEUED and self._writing
                                              and stalled_for >= max(timeout, _WRITE_GRACE))
                if state == _QUEUED and not stuck:
                    outgoing.state = _DROPPED   # behind a slow but moving write: never send it
            if not pending.done.is_set():       # the response may have raced the timeout
                if stuck:
                    self._fail_all(f"MCP server {self.name} stopped reading its stdin "
                                   f"(a {method} request could not be written within {timeout:g}s)")
                    raise McpError(self._failure or "")
                raise _Timeout(method, request_id, timeout, sent=state == _SENT)
        if pending.failure is not None:
            raise McpError(pending.failure)
        response = pending.response or {}
        if response.get("error") is not None:
            raise _rpc_error(response["error"])
        result = response.get("result")
        if not isinstance(result, dict):
            raise McpError(f"MCP server {self.name} sent an invalid result for {method}")
        return result

    def _send(self, message: dict) -> _Outgoing:
        """Queue a message for the writer thread; never blocks. Dropped once the session failed."""
        outgoing = _Outgoing((json.dumps(message) + "\n").encode("utf-8"))
        with self._lock:
            if self._failure is not None:
                outgoing.state = _DROPPED
                return outgoing
        self._outbox.put(outgoing)
        return outgoing

    def _fail_all(self, reason: str) -> None:
        """Mark the session failed (first reason wins) and wake every waiting request."""
        with self._lock:
            if self._failure is None:
                self._failure = reason
            pending, self._pending = self._pending, {}
            for waiter in pending.values():
                waiter.failure = self._failure
                waiter.done.set()
        self._failed.set()

    # -- threads ----------------------------------------------------------------

    def _write_stdin(self) -> None:
        """Write queued messages in order; close stdin when close() asks or a write fails."""
        proc = self._proc
        fd = proc.stdin.fileno()
        while True:
            outgoing = self._outbox.get()
            if outgoing is None:
                break
            with self._lock:
                if outgoing.state != _QUEUED or self._failure is not None:
                    continue
                outgoing.state = _WRITING
                self._writing = True
                self._last_progress = time.monotonic()
            try:
                self._write_all(fd, outgoing.data)
            except OSError as exc:
                self._failed.wait(_CLOSE_GRACE + _DRAIN_SECONDS)  # prefer the exit reason, if it comes
                self._fail_all(self._exit_reason(f"stopped reading its stdin: {exc}"))
                break
            with self._lock:
                outgoing.state = _SENT
                self._writing = False
            outgoing.written.set()
        _close_quietly(proc.stdin)

    def _write_all(self, fd: int, data: bytes) -> None:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view[:_WRITE_CHUNK]):]
            self._last_progress = time.monotonic()

    def _read_stdout(self) -> None:
        proc = self._proc
        oversized = False
        for raw in iter(lambda: proc.stdout.readline(_MAX_MESSAGE_BYTES), b""):
            if oversized:              # the rest of a message that was too large
                oversized = not raw.endswith(b"\n")
                continue
            if len(raw) >= _MAX_MESSAGE_BYTES and not raw.endswith(b"\n"):
                oversized = True
                self._fail_all(f"MCP server {self.name} sent a message larger than "
                               f"{_MAX_MESSAGE_BYTES} bytes")
                continue
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                self._emit(f"non-JSON output: {line}")
                continue
            self._dispatch(message)
        try:
            proc.wait(timeout=_CLOSE_GRACE)   # exited: the exit watcher reports it
        except subprocess.TimeoutExpired:
            reason = self._exit_reason("closed its stdout")
            if not self._closed:
                self._emit(reason)
            self._fail_all(reason)

    def _read_stderr(self) -> None:
        for line in _stderr_lines(self._proc.stderr):
            if line:
                self._stderr_tail.append(line[:500])
                self._emit(f"stderr: {line}")

    def _watch_exit(self) -> None:
        """Fail the session when the server exits, even if a helper keeps its pipes open."""
        code = self._proc.wait()
        for name in ("stdout", "stderr"):   # let output written before the exit arrive first
            self._threads[name].join(timeout=_DRAIN_SECONDS)
        reason = self._exit_reason(f"exited with code {code}")
        if not self._closed:
            self._emit(reason)
        self._fail_all(reason)

    def _dispatch(self, message: Any) -> None:
        if not isinstance(message, dict):
            self._emit(f"ignored non-object message: {str(message)[:200]}")
            return
        method = message.get("method")
        request_id = message.get("id")
        if isinstance(method, str):
            if request_id is not None:
                self._answer(request_id, method)
            elif method == "notifications/message":
                self._log_notification(message.get("params"))
            return
        if not isinstance(request_id, (int, str)):
            return
        with self._lock:
            waiter = self._pending.pop(request_id, None)  # None: already timed out
            if waiter is not None:
                waiter.response = message
                waiter.done.set()

    def _answer(self, request_id: Any, method: str) -> None:
        """Answer a server-to-client request."""
        reply: dict = {"jsonrpc": "2.0", "id": request_id}
        if method == "ping":
            reply["result"] = {}
        elif method == "roots/list":
            root = self.cwd.resolve()
            reply["result"] = {"roots": [{"uri": root.as_uri(), "name": root.name or str(root)}]}
        else:
            reply["error"] = {"code": METHOD_NOT_FOUND, "message": f"Method not found: {method}"}
        self._send(reply)

    def _log_notification(self, params: Any) -> None:
        params = params if isinstance(params, dict) else {}
        data = params.get("data")
        text = data if isinstance(data, str) else json.dumps(data)
        self._emit(f"[{params.get('level', 'info')}] {text}")

    def _emit(self, text: str) -> None:
        if self._log is None:
            return
        line = f"mcp {self.name}: {text}"
        if len(line) > _LOG_LINE_CHARS:
            line = line[:_LOG_LINE_CHARS] + "…"
        try:
            self._log(line)
        except Exception:  # a broken log sink must not kill a reader thread
            pass


def _stderr_lines(stream: Any) -> Iterator[str]:
    """Decoded stderr lines in bounded memory.

    A carriage return redraws the line, as on a terminal, so progress bars
    yield only their final state; at most _STDERR_LINE_BYTES of a line are kept.
    """
    line = b""
    for chunk in iter(lambda: stream.read1(_READ_CHUNK), b""):
        *complete, line = (line + chunk).split(b"\n")
        for text in complete:
            yield _last_redraw(text.rstrip(b"\r"))[:_STDERR_LINE_BYTES].decode("utf-8", "replace").rstrip()
        redraw = line.rfind(b"\r", 0, len(line) - 1)   # a trailing \r may start a \r\n
        line = line[redraw + 1:][:_STDERR_LINE_BYTES]
    if line:
        yield _last_redraw(line.rstrip(b"\r")).decode("utf-8", "replace").rstrip()


def _last_redraw(line: bytes) -> bytes:
    return line[line.rfind(b"\r") + 1:]


def _normalize_tool(entry: Any) -> dict | None:
    if not isinstance(entry, dict) or not isinstance(entry.get("name"), str) or not entry["name"]:
        return None
    description = entry.get("description")
    schema = entry.get("inputSchema")
    return {
        "name": entry["name"],
        "description": description if isinstance(description, str) else "",
        "inputSchema": schema if isinstance(schema, dict) else {"type": "object", "properties": {}},
    }


def _rpc_error(error: Any) -> McpError:
    if not isinstance(error, dict):
        return McpError(str(error))
    code = error.get("code") if isinstance(error.get("code"), int) else None
    return McpError(str(error.get("message") or f"MCP error {code}"), code)


def _format_result(result: dict) -> tuple[str, bool]:
    """Flatten a tools/call result into (text, is_error) for a text-only model."""
    content = result.get("content")
    parts = [_content_text(item) for item in (content if isinstance(content, list) else [])]
    text = "\n".join(part for part in parts if part)
    structured = result.get("structuredContent")
    if not text and structured is not None:
        text = json.dumps(structured, ensure_ascii=False)
    return text, bool(result.get("isError"))


def _content_text(item: Any) -> str:
    if not isinstance(item, dict):
        return json.dumps(item, ensure_ascii=False)
    kind = item.get("type")
    if kind == "text":
        return str(item.get("text", ""))
    if kind in ("image", "audio"):
        return f"[{kind} omitted: {item.get('mimeType', 'unknown type')}]"
    if kind == "resource":
        resource = item.get("resource") if isinstance(item.get("resource"), dict) else {}
        text = resource.get("text")
        return text if isinstance(text, str) else str(resource.get("uri", ""))
    if kind == "resource_link":
        return str(item.get("uri", ""))
    return json.dumps(item, ensure_ascii=False)


def _signal_leader_group(proc: subprocess.Popen, sig: int) -> None:
    """Signal the server's process group, or the server alone if it left the group."""
    try:
        os.killpg(proc.pid, sig)
    except OSError:
        try:
            proc.send_signal(sig)
        except OSError:
            pass


def _kill_group(pgid: int) -> None:
    """SIGTERM what is left of a process group, wait up to _CLOSE_GRACE, then SIGKILL."""
    if not _group_running(pgid):
        return
    _signal_quietly(pgid, signal.SIGTERM)
    deadline = time.monotonic() + _CLOSE_GRACE
    while time.monotonic() < deadline:
        if not _group_running(pgid):
            return
        time.sleep(0.05)
    _signal_quietly(pgid, signal.SIGKILL)


def _group_running(pgid: int) -> bool:
    """True while the group has a member that is not a zombie (init may reap orphans slowly)."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        entries = os.listdir("/proc")
    except OSError:
        return True   # no /proc: assume the members are alive
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", "rb") as fh:
                stat = fh.read().decode("ascii", "replace")
        except OSError:
            continue
        fields = stat[stat.rfind(")") + 2:].split()   # state ppid pgrp ...
        if len(fields) > 2 and fields[2] == str(pgid) and fields[0] not in ("Z", "X"):
            return True
    return False


def _signal_quietly(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except OSError:
        pass


def _close_quietly(stream: Any) -> None:
    if stream is None:
        return
    try:
        stream.close()
    except (OSError, ValueError):
        pass
