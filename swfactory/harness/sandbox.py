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
import shutil
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
# H2 (DESIGN §34): an SSH or GPG agent socket in the server's environment would hand its keys to every Bash.
AGENT_SOCKET_NAMES = ("SSH_AUTH_SOCK", "GPG_AGENT_INFO")
# The bwrap options every command gets (argv) and the availability probe tries. --unshare-pid gives
# the command its own PID namespace, so the fresh /proc shows only the sandbox's processes: without
# it /proc/<host pid>/root reaches the host's mount namespace, where the paths bound read-only here
# are writable. No --die-with-parent: bwrap's PID 1 would then kill background children (a dev
# server started with `&`) as soon as the command returns; cleanup() kills them with the group.
# No --new-session either: start_new_session already detaches from any terminal, and a new session
# inside would take the command out of the process group that timeouts and cleanup() signal.
BWRAP_BASE_ARGS = ("--unshare-pid", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc")
# What bwrap_available() probes: the base options plus what every command adds (a private /tmp) and the strictest
# option a run may add (no network), so a host where only a lighter form works never reads as usable.
BWRAP_PROBE_ARGS = (*BWRAP_BASE_ARGS, "--tmpfs", "/tmp", "--unshare-net")
# util-linux's setpriv sets PR_SET_NO_NEW_PRIVS and then executes the program in its place (same pid, same group).
SETPRIV = "setpriv"
NNP_ARGS = ("--no-new-privs",)
EXTRA_ENV = {"CI": "1", "GIT_TERMINAL_PROMPT": "0", "PAGER": "cat", "GIT_PAGER": "cat"}
# Under bwrap the owner's GPG and SSH agent sockets are hidden (H2, config.agent_socket_paths), so a commit an
# agent makes there cannot be signed with the owner's key: signing is turned off for it rather than failing it.
BWRAP_GIT_CONFIG = (("commit.gpgsign", "false"), ("tag.gpgsign", "false"))
OUTPUT_CAP_BYTES = 10 * 1024 * 1024   # kept per command: first half + last half
KILL_GRACE_SECONDS = 3.0
KILL_SETTLE_SECONDS = 1.0           # after SIGKILL, for the kernel to finish the kill
READ_GRACE_SECONDS = 0.5
POLL_SECONDS = 0.05


def is_secret_name(name: str, secret_names: Collection[str] = ()) -> bool:
    """True if an environment variable name looks like a credential (events.is_credential_name),
    starts with ANTHROPIC_, OPENAI_, DEEPSEEK_ or FACTORY_, names an SSH or GPG agent socket, or is one of
    secret_names."""
    return (is_credential_name(name) or name.upper().startswith(SECRET_PREFIXES) or name.upper() in AGENT_SOCKET_NAMES
            or name in secret_names)


def scrub_env(environ: Mapping[str, str], secret_names: Iterable[str] = ()) -> dict[str, str]:
    """A copy of environ without secret-looking variables and without the explicit secret_names."""
    names = set(secret_names)
    return {k: v for k, v in environ.items() if not is_secret_name(k, names)}


def _within(path: Path, directory: Path) -> bool:
    """True when path is directory or below it."""
    return path == directory or directory in path.parents


def mask_plan(dirs: Iterable[Path], files: Iterable[Path], needed: Iterable[Path],
              fallback_files: Iterable[Path] = ()) -> tuple[list[Path], list[Path], list[Path]]:
    """(directories to hide, files to hide, paths to bind back) under bwrap (H1).

    Every directory is hidden whole (an empty tmpfs). A path the run needs below one (its cwd, write paths,
    read roots, the repository, an MCP server's program) is bound back after it; when the run needs the
    directory itself, its entries are bound back one by one instead, except fallback_files (config.json, the
    env file, personas, ...) and symlinks. So a sensitive file in these directories is never hidden by a bind
    over the file itself: the harness replaces those files atomically, and a rename onto a bound file detaches
    that bind in a sandbox that is already running. files (a sensitive-terms file kept elsewhere, an agent's
    socket) are hidden with /dev/null; one inside a bound-back path is hidden too. A bound-back path that is
    sensitive itself, or inside a sensitive one, is never bound back. Only existing paths are listed."""
    wanted = list(dict.fromkeys(Path(p).resolve() for p in needed))
    fallback = list(dict.fromkeys(Path(p).resolve() for p in fallback_files))
    hidden = [d for d in dict.fromkeys(Path(d).resolve() for d in dirs) if d.is_dir()]

    def sensitive(path: Path) -> bool:
        return any(_within(path, item) for item in fallback)
    rebind: list[Path] = []
    for directory in hidden:
        for path in wanted:
            if directory in path.parents and path.exists() and not sensitive(path):
                rebind.append(path)
        if directory in wanted:
            try:
                entries = sorted(directory.iterdir())
            except OSError:
                entries = []
            for entry in entries:
                if entry.is_symlink() or sensitive(entry) or entry in hidden:
                    continue
                rebind.append(entry)
    rebind = list(dict.fromkeys(rebind))
    hidden_files: list[Path] = []
    for path in dict.fromkeys(Path(f).resolve() for f in files):
        if not path.exists() or path.is_dir():
            continue
        exposed = any(_within(path, r) for r in rebind)
        if exposed or not any(d in path.parents for d in hidden):
            hidden_files.append(path)
    for path in fallback:
        if path.exists() and not path.is_dir() and any(_within(path, r) for r in rebind):
            hidden_files.append(path)
    return hidden, list(dict.fromkeys(hidden_files)), rebind


@functools.lru_cache(maxsize=None)
def bwrap_available() -> bool:
    """True if bubblewrap is installed and can create the sandbox every command runs in here, with a private /tmp
    and without network (BWRAP_PROBE_ARGS; checked once, cached)."""
    try:
        done = subprocess.run(["bwrap", *BWRAP_PROBE_ARGS, "true"], stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


@functools.lru_cache(maxsize=None)
def nested_bwrap() -> tuple[bool, str]:
    """(works, bwrap's own words when it does not) for a sandbox inside the sandbox: bwrap started inside bwrap,
    both with the base options. Where AppArmor restricts unprivileged user namespaces (Ubuntu 24.04 and later),
    bwrap may make its own namespaces while the programs it starts may not: then a browser's own sandbox
    (Chromium, so agent-browser and Playwright), a CLI agent's own bwrap (Codex, Claude Code) and anything else
    that builds a sandbox cannot start one inside agents' Bash. A named limitation, never a reason to loosen that
    restriction. Checked once, cached; (False, why) also when bwrap itself does not run."""
    try:
        done = subprocess.run(["bwrap", *BWRAP_BASE_ARGS, "bwrap", *BWRAP_BASE_ARGS, "true"],
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"bwrap could not be started ({type(exc).__name__})"
    if done.returncode == 0:
        return True, ""
    words = (done.stderr or b"").decode("utf-8", "replace").strip().splitlines()
    return False, (words[0][:200] if words else f"exit {done.returncode}")


def no_new_privs_set(status: str = "/proc/self/status") -> bool | None:
    """True when this process runs with no-new-privileges (a unit with NoNewPrivileges=yes, or inside bwrap):
    setuid programs such as sudo, either implementation, cannot raise its privileges or its children's. False when
    it does not; None when /proc does not say."""
    try:
        with open(status, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("NoNewPrivs:"):
                    return line.split(":", 1)[1].strip() == "1"
    except OSError:
        return None
    return None


@functools.lru_cache(maxsize=None)
def setpriv_path() -> str | None:
    """Where setpriv (util-linux) is, or None (checked once, cached)."""
    return shutil.which(SETPRIV)


def without_new_privileges(argv: list[str], path: str | None = None, cwd: Path | str | None = None) -> list[str]:
    """argv started with no new privileges (`setpriv --no-new-privs`), for a program an agent controls that runs
    outside bwrap (bwrap sets the flag itself): an agent's command with harness.sandbox none, a CLI agent, an MCP
    server. A setuid program (sudo, sudo-rs or classic) or file capabilities can then never give it more rights
    than the harness's user, even when the harness's own unit leaves NoNewPrivileges off so that its helpers can
    use sudo (DESIGN §17). Unchanged when this process has the flag already (its children inherit it), when
    setpriv is not installed (the doctor says so), or when argv[0] cannot be found on path (or below cwd, for a
    relative path), so a missing program still fails as before."""
    argv = list(argv)
    if not argv or no_new_privs_set():
        return argv
    setpriv = setpriv_path()
    if setpriv is None:
        return argv
    program = argv[0]
    if os.sep in program:
        candidate = program if os.path.isabs(program) else os.path.join(str(cwd or os.getcwd()), program)
        found = os.path.isfile(candidate) and os.access(candidate, os.X_OK)
    else:
        found = shutil.which(program, path=path) is not None
    return [setpriv, *NNP_ARGS, *argv] if found else argv


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
                 secret_names: Iterable[str] | None = None, masked: Iterable[Path] = (),
                 masked_files: Iterable[Path] = (), readonly_rebind: Iterable[Path] = ()) -> None:
        if mode not in SANDBOX_MODES:
            raise ValueError(f"unknown sandbox mode {mode!r} (expected one of {', '.join(SANDBOX_MODES)})")
        self.mode = mode
        self.cwd = Path(cwd)
        self.writable = [Path(w) for w in writable]
        self.network = network
        self.env = dict(env or {})
        self.secret_names = list(secret_names or [])
        self.masked = [Path(p) for p in masked]                    # H1: an empty tmpfs over each
        self.masked_files = [Path(p) for p in masked_files]        # H1: /dev/null over each
        self.readonly_rebind = [Path(p) for p in readonly_rebind]  # bound back below a masked directory
        #                                                            (skills, paths the run needs: mask_plan)
        self._groups: dict[int, _Group] = {}
        self._lock = threading.Lock()

    def environment(self) -> dict[str, str]:
        """The environment a command sees: os.environ minus secrets, plus env, plus the fixed extras."""
        merged = scrub_env(os.environ, self.secret_names)
        merged.update({str(k): str(v) for k, v in self.env.items()})
        merged.update(EXTRA_ENV)
        if self.mode == "bwrap":   # appended to any GIT_CONFIG_COUNT entries already there
            try:
                count = max(0, int(merged.get("GIT_CONFIG_COUNT") or 0))
            except ValueError:
                count = 0
            for index, (key, value) in enumerate(BWRAP_GIT_CONFIG, count):
                merged[f"GIT_CONFIG_KEY_{index}"] = key
                merged[f"GIT_CONFIG_VALUE_{index}"] = value
            merged["GIT_CONFIG_COUNT"] = str(count + len(BWRAP_GIT_CONFIG))
        return merged

    def argv(self, command: str) -> list[str]:
        """The argument vector that runs command in this sandbox's mode."""
        return self.wrap(["bash", "-c", command])

    def wrap(self, argv: list[str], cwd: Path | None = None) -> list[str]:
        """argv run the way this sandbox runs a command: for "none" with no new privileges (setpriv, see
        without_new_privileges); for "bwrap" inside bubblewrap (which sets that flag itself) with the same binds
        (writable paths, the masked directories hidden, what is bound back below them, the masked files, a private
        /tmp, no network unless allowed), in cwd (default: the sandbox's). Starting it is the caller's job; MCP
        servers are started this way."""
        if self.mode == "none":
            return without_new_privileges(argv, self.environment().get("PATH"), cwd or self.cwd)
        args = ["bwrap", *BWRAP_BASE_ARGS, "--tmpfs", "/tmp"]
        masked = sorted((p for p in dict.fromkeys(self.masked) if p.is_dir()), key=lambda p: len(p.parts))
        writable = [p for p in dict.fromkeys(self.writable) if p.exists()]   # bwrap binds only existing paths

        def deepest_mask(path: Path) -> Path | None:
            above = [m for m in masked if m in path.parents]
            return max(above, key=lambda m: len(m.parts)) if above else None
        # 1. Writable paths outside every masked directory (or one itself): bound first, so a mask inside one
        #    still applies on top of it.
        for path in writable:
            if deepest_mask(path) is None:
                args += ["--bind", str(path), str(path)]
        # 2. Each masked directory, outermost first, then what is bound back below it (shallowest first, a
        #    writable path after a read-only one): a path inside a writable path is bound writable.
        rebinds = [(p, False) for p in dict.fromkeys(self.readonly_rebind) if p.exists()]
        rebinds += [(p, True) for p in writable if deepest_mask(p) is not None]
        for directory in masked:
            args += ["--tmpfs", str(directory)]
            inside = sorted((item for item in rebinds if deepest_mask(item[0]) == directory),
                            key=lambda item: (len(item[0].parts), item[1]))
            for path, rw in inside:
                rw = rw or any(_within(path, w) for w in writable)
                args += ["--bind" if rw else "--ro-bind", str(path), str(path)]
        # 3. /dev/null over each masked file (a sensitive-terms file kept elsewhere, an agent's socket).
        for path in dict.fromkeys(self.masked_files):
            if path.exists() and not path.is_dir():
                args += ["--ro-bind", "/dev/null", str(path)]
        # 4. The empty tmpfs of each masked directory read-only (not what is bound below it), last: a write
        #    there fails instead of vanishing with the sandbox.
        for directory in masked:
            args += ["--remount-ro", str(directory)]
        if not self.network:
            args.append("--unshare-net")
        return args + ["--chdir", str(cwd or self.cwd), *argv]

    def run(self, command: str, timeout: float | None,
            cancel: threading.Event | None = None) -> tuple[int | None, str, bool]:
        """Run command; return (exit code or None if killed by timeout/cancel, output, timed_out)."""
        return self._run(self.argv(command), timeout, cancel)

    def run_argv(self, argv: list[str], timeout: float | None,
                 cancel: threading.Event | None = None) -> tuple[int | None, str, bool]:
        """Run argv directly (no shell), wrapped like a command (RunCheck, Wake & Messages §5.8)."""
        return self._run(self.wrap(list(argv)), timeout, cancel)

    def _run(self, argv: list[str], timeout: float | None,
             cancel: threading.Event | None = None) -> tuple[int | None, str, bool]:
        if self.mode == "bwrap" and not bwrap_available():
            raise RuntimeError("the bwrap sandbox is configured but bubblewrap is not usable on this host")
        self._release_finished()
        proc = subprocess.Popen(
            argv, cwd=str(self.cwd), env=self.environment(),
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
