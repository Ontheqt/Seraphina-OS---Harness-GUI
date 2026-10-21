"""Bash execution for the builtin harness: plain or inside bubblewrap.

Every command runs as `bash -c <command>` in its own session (so its process
group can be killed as a whole), with stdin closed, a scrubbed environment and
stdout+stderr combined. Background children (dev servers started with `&`)
may keep the output pipe open after bash exits, so a run waits for bash
itself, reads whatever is buffered for a short grace period and returns; the
remembered process groups are killed by cleanup() when the agent run ends.

A group's id is its leader's pid, and Linux never hands out a pid that is still
in use by a process, a zombie included. So the leader is left unreaped (its exit
status is read with WNOWAIT) for as long as anything else lives in its group:
a remembered group id can then never be recycled for an unrelated process, and
cleanup() can only ever signal processes this sandbox started.
"""

from __future__ import annotations

import functools
import os
import select
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Collection, Iterable, Mapping

from ..events import is_credential_name

SANDBOX_MODES = ("none", "bwrap")
SECRET_PREFIXES = ("ANTHROPIC_", "OPENAI_", "DEEPSEEK_", "FACTORY_")
# The bwrap options every command gets (argv) and the availability probe tries. --unshare-pid gives
# the command its own PID namespace, so the fresh /proc shows only the sandbox's processes: without
# it /proc/<host pid>/root reaches the host's mount namespace, where the paths bound read-only here
# are writable. No --die-with-parent: bwrap's PID 1 would then kill background children (a dev
# server started with `&`) as soon as the command returns; cleanup() kills them with the group.
# No --new-session either: start_new_session already detaches from any terminal, and a new session
# inside would take the command out of the process group that timeouts and cleanup() signal.
BWRAP_BASE_ARGS = ("--unshare-pid", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc")
EXTRA_ENV = {"CI": "1", "GIT_TERMINAL_PROMPT": "0", "PAGER": "cat", "GIT_PAGER": "cat"}
OUTPUT_CAP_BYTES = 10 * 1024 * 1024   # kept per command: first half + last half
KILL_GRACE_SECONDS = 3.0
KILL_SETTLE_SECONDS = 1.0           # after SIGKILL, for the kernel to finish the kill
READ_GRACE_SECONDS = 0.5
POLL_SECONDS = 0.05


def is_secret_name(name: str, secret_names: Collection[str] = ()) -> bool:
    """True if an environment variable name looks like a credential (events.is_credential_name),
    starts with ANTHROPIC_, OPENAI_, DEEPSEEK_ or FACTORY_, or is one of secret_names."""
    return is_credential_name(name) or name.upper().startswith(SECRET_PREFIXES) or name in secret_names


def scrub_env(environ: Mapping[str, str], secret_names: Iterable[str] = ()) -> dict[str, str]:
    """A copy of environ without secret-looking variables and without the explicit secret_names."""
    names = set(secret_names)
    return {k: v for k, v in environ.items() if not is_secret_name(k, names)}


@functools.lru_cache(maxsize=None)
def bwrap_available() -> bool:
    """True if bubblewrap is installed and can create a sandbox here (checked once, cached)."""
    try:
        done = subprocess.run(["bwrap", *BWRAP_BASE_ARGS, "true"], stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


class _OutputReader(threading.Thread):
    """Drains a pipe: keeps the head and tail of the output (capped) until told to stop capturing.

    After stop_capture() it keeps draining (and discarding) so background
    processes that still hold the pipe never block or get EPIPE; close() makes
    it exit at its next poll and closes the pipe.
    """

    def __init__(self, fd: int, cap_bytes: int = OUTPUT_CAP_BYTES) -> None:
        super().__init__(daemon=True, name="sandbox-output")
        self.fd = fd
        self.eof = threading.Event()
        self._half = max(cap_bytes // 2, 1)
        self._head = bytearray()
        self._tail: deque[bytes] = deque()
        self._tail_len = 0
        self._dropped = 0
        self._capturing = True
        self._closing = False
        self._lock = threading.Lock()

    def run(self) -> None:
        try:
            while not self._closing:
                ready, _, _ = select.select([self.fd], [], [], 0.2)
                if not ready:
                    continue
                chunk = os.read(self.fd, 65536)
                if not chunk:
                    break
                with self._lock:
                    if self._capturing:
                        self._keep(chunk)
        except (OSError, ValueError):
            pass
        finally:
            self.eof.set()
            try:
                os.close(self.fd)
            except OSError:
                pass

    def _keep(self, chunk: bytes) -> None:
        room = self._half - len(self._head)
        if room > 0:
            self._head += chunk[:room]
            chunk = chunk[room:]
        if not chunk:
            return
        self._tail.append(chunk)
        self._tail_len += len(chunk)
        while self._tail_len > self._half:
            excess = self._tail_len - self._half
            first = self._tail[0]
            if len(first) <= excess:
                self._tail.popleft()
                self._tail_len -= len(first)
                self._dropped += len(first)
            else:
                self._tail[0] = first[excess:]
                self._tail_len -= excess
                self._dropped += excess

    def stop_capture(self) -> str:
        """Stop keeping output and return what was captured, decoded (UTF-8, replacement chars)."""
        with self._lock:
            self._capturing = False
            head = bytes(self._head)
            tail = b"".join(self._tail)
            dropped = self._dropped
        text = head.decode("utf-8", errors="replace")
        if dropped:
            text += f"\n… [{dropped} bytes of output dropped] …\n"
        return text + tail.decode("utf-8", errors="replace")

    def close(self) -> None:
        self._closing = True


@dataclass
class _Group:
    """One started command: its leader (reaped only by release()) and the reader draining its output."""

    proc: subprocess.Popen
    reader: _OutputReader
    active: bool = True   # run() is still waiting for it; only that run() may release it

    def release(self) -> None:
        self.reader.close()
        self.proc.wait()   # reaps the zombie leader; its pid (the group id) is free for reuse after this


class Sandbox:
    """Runs bash commands for one agent run and remembers their process groups for cleanup()."""

    def __init__(self, mode: str, cwd: Path, writable: list[Path], network: bool, env: dict,
                 secret_names: Iterable[str] | None = None) -> None:
        if mode not in SANDBOX_MODES:
            raise ValueError(f"unknown sandbox mode {mode!r} (expected one of {', '.join(SANDBOX_MODES)})")
        self.mode = mode
        self.cwd = Path(cwd)
        self.writable = [Path(w) for w in writable]
        self.network = network
        self.env = dict(env or {})
        self.secret_names = list(secret_names or [])
        self._groups: dict[int, _Group] = {}
        self._lock = threading.Lock()

    def environment(self) -> dict[str, str]:
        """The environment a command sees: os.environ minus secrets, plus env, plus the fixed extras."""
        merged = scrub_env(os.environ, self.secret_names)
        merged.update({str(k): str(v) for k, v in self.env.items()})
        merged.update(EXTRA_ENV)
        return merged

    def argv(self, command: str) -> list[str]:
        """The argument vector that runs command in this sandbox's mode."""
        return self.wrap(["bash", "-c", command])

    def wrap(self, argv: list[str], cwd: Path | None = None) -> list[str]:
        """argv run the way this sandbox runs a command: unchanged for "none"; for "bwrap" inside bubblewrap
        with the same binds (writable paths, a private /tmp, no network unless allowed), in cwd (default: the
        sandbox's). Starting it is the caller's job; MCP servers are started this way."""
        if self.mode == "none":
            return list(argv)
        args = ["bwrap", *BWRAP_BASE_ARGS, "--tmpfs", "/tmp"]
        for path in self.writable:
            if path.exists():   # bwrap cannot bind a path that does not exist yet
                args += ["--bind", str(path), str(path)]
        if not self.network:
            args.append("--unshare-net")
        return args + ["--chdir", str(cwd or self.cwd), *argv]

    def run(self, command: str, timeout: float | None,
            cancel: threading.Event | None = None) -> tuple[int | None, str, bool]:
        """Run command; return (exit code or None if killed by timeout/cancel, output, timed_out)."""
        if self.mode == "bwrap" and not bwrap_available():
            raise RuntimeError("the bwrap sandbox is configured but bubblewrap is not usable on this host")
        self._release_finished()
        proc = subprocess.Popen(
            self.argv(command), cwd=str(self.cwd), env=self.environment(),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        group = _Group(proc, _OutputReader(os.dup(proc.stdout.fileno())))
        proc.stdout.close()
        with self._lock:
            self._groups[proc.pid] = group
        group.reader.start()
        deadline = time.monotonic() + timeout if timeout and timeout > 0 else None
        killed = timed_out = False
        status = _exit_status(proc)
        while status is None:
            if cancel is not None and cancel.is_set():
                killed = True
            elif deadline is not None and time.monotonic() >= deadline:
                killed = timed_out = True
            if killed:
                _kill_groups([proc.pid])
                _exit_status(proc, block=True)
                break
            time.sleep(POLL_SECONDS)
            status = _exit_status(proc)
        group.reader.eof.wait(READ_GRACE_SECONDS)
        output = group.reader.stop_capture()
        group.active = False
        self._release_finished()
        return (None if killed else status), output, timed_out

    def cleanup(self) -> None:
        """SIGTERM every remembered process group still alive, then SIGKILL survivors after 3s."""
        with self._lock:
            pgids = list(self._groups)
        _kill_groups(pgids)
        self._release_finished()

    def _release_finished(self) -> None:
        """Forget (and reap) every idle command whose leader has exited and whose group has nothing left alive."""
        with self._lock:
            idle = [(pgid, group) for pgid, group in self._groups.items() if not group.active]
        if not idle:
            return
        live = _live_groups([pgid for pgid, _ in idle])
        for pgid, group in idle:
            if pgid in live or _exit_status(group.proc) is None:
                continue
            with self._lock:
                if self._groups.get(pgid) is not group:
                    continue
                del self._groups[pgid]
            group.release()


def _exit_status(proc: subprocess.Popen, block: bool = False) -> int | None:
    """The exit code (-signal if killed) of proc without reaping it; None while it runs (unless block)."""
    if proc.returncode is not None:
        return proc.returncode
    flags = os.WEXITED | os.WNOWAIT | (0 if block else os.WNOHANG)
    try:
        info = os.waitid(os.P_PID, proc.pid, flags)
    except ChildProcessError:   # reaped elsewhere (e.g. SIGCHLD is ignored): Popen knows or reports 0
        return proc.poll()
    if info is None:
        return None
    return info.si_status if info.si_code == os.CLD_EXITED else -info.si_status


def _live_groups(pgids: Iterable[int]) -> set[int]:
    """Those of pgids that still have a process which is not a zombie (one scan of /proc)."""
    wanted = {str(pgid) for pgid in pgids}
    try:
        entries = os.listdir("/proc")
    except OSError:   # no /proc to tell zombies apart: treat every existing group as alive
        return {int(pgid) for pgid in wanted if _group_exists(int(pgid))}
    live: set[int] = set()
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", "rb") as fh:
                stat = fh.read().decode("ascii", errors="replace")
        except OSError:
            continue
        fields = stat[stat.rfind(")") + 2:].split()   # state ppid pgrp ...
        if len(fields) > 2 and fields[2] in wanted and fields[0] not in ("Z", "X"):
            live.add(int(fields[2]))
    return live


def _group_exists(pgid: int) -> bool:
    """True while any process, zombies included, is in the group."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _wait_settled(pgids: list[int], seconds: float) -> list[int]:
    """Wait up to seconds for the groups to have no live process; return the ones still alive."""
    deadline = time.monotonic() + seconds
    pending = sorted(_live_groups(pgids))
    while pending and time.monotonic() < deadline:
        time.sleep(POLL_SECONDS)
        pending = sorted(_live_groups(pending))
    return pending


def _kill_groups(pgids: list[int]) -> None:
    """SIGTERM the live groups, wait up to KILL_GRACE_SECONDS for them to exit, then SIGKILL what is left.

    Only call this for groups whose leader this process has not reaped (see the module docstring).
    """
    targets = sorted(_live_groups(pgids))
    for pgid in targets:
        _signal_group(pgid, signal.SIGTERM)
    survivors = _wait_settled(targets, KILL_GRACE_SECONDS)
    for pgid in survivors:
        _signal_group(pgid, signal.SIGKILL)
    _wait_settled(survivors, KILL_SETTLE_SECONDS)
