"""``bin/sf-browser mcp <grant>``: take the session lock, then replace itself with bwrap over the shim
(BROWSER_AND_CODEX.md §2.1, §2.9).

The namespace has no network but its own loopback, its own PID and IPC namespaces, dies with its parent, starts
with an empty environment and binds only what the shim and its browser need: the system's programs and libraries,
the Python the launcher runs on, the ``swfactory`` package, the harness's own runtime (Node, the Playwright MCP
install and the Chrome build: only the grant's paths, never a Node or Chrome from ``PATH``), the fixture site, the
grant and the profile's lock number read-only, the session folder writable, and a ``--size``-capped tmpfs for
Playwright's output. ``HOME`` and ``/tmp`` are empty private tmpfs mounts. The harness's data directory, its config,
the chats and every key file are not in it.

The session lock's descriptors stay open across the exec, so the kernel frees the lock when the namespace ends.
A launcher that cannot lock or read its grant still speaks MCP: it answers every call with session_lock_busy or
no_grant (``shim.refusing``), so the run sees why it has no browser.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import sysconfig
from pathlib import Path

from ..harness.sandbox import minimal_args
from . import fixture as F
from .session_locks import GrantError, SessionLockBusy, read_grant, take_session_lock
from .shim import DOWNLOADS_DIR, refusing

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = ("import sys; root = sys.argv.pop(1); sys.path.insert(0, root); "
             "from swfactory.browser.shim import main; sys.exit(main())")
PREVIEW_PORT = 18938


def data_dir_of(grant_path: Path) -> Path:
    """<data_dir>/browser/grants/<G>.json -> <data_dir>."""
    return grant_path.resolve().parents[2]


def _system(path: str) -> bool:
    """Whether path is inside the system directories every minimal namespace binds already (/usr)."""
    return path == "/usr" or path.startswith("/usr/")


def _python_binds() -> list[str]:
    """The interpreter and its standard library, when they live outside /usr (a uv or pyenv Python)."""
    out = []
    for path in (os.path.realpath(sys.executable), os.path.realpath(sys.base_prefix),
                 os.path.realpath(sysconfig.get_paths()["stdlib"])):
        if not _system(path):
            out.append(path)
    return list(dict.fromkeys(out))


def runtime_binds(paths: dict) -> list[str]:
    """What the grant's runtime needs read-only: the Node binary, the node_modules holding Playwright MCP and its
    playwright-core, the Chrome build's folder, the fixture site; and a test's own Playwright argv files."""
    out: list[str] = []

    def absolute(value: object) -> str | None:   # only absolute paths: realpath("") would be the cwd
        return os.path.realpath(value) if isinstance(value, str) and value.startswith("/") else None
    if absolute(paths.get("node")):
        out.append(absolute(paths["node"]))
    package = absolute(paths.get("playwright_mcp"))
    if package:
        real = Path(package)
        out.append(str(real.parent.parent if real.parent.name == "@playwright" else real))
    if absolute(paths.get("chrome")):
        out.append(str(Path(absolute(paths["chrome"])).parent))
    if absolute(paths.get("fixture")):
        out.append(absolute(paths["fixture"]))
    for item in paths.get("playwright_argv") or []:   # tests only: a fake server and the interpreter it runs on
        if absolute(item) and os.path.exists(item):
            real = Path(absolute(item))
            out.append(str(real.parent.parent if real.parent.name == "bin" and os.access(real, os.X_OK) else real))
    return [path for path in dict.fromkeys(out) if not _system(path)]


def namespace_argv(grant_path: Path, grant: dict, session: dict, *, inner: list[str] | None = None) -> list[str]:
    """The bwrap argv that runs the shim for this session lock (inner: another program, for the tests that fix
    the exact binds by starting a browser with them)."""
    paths = grant.get("paths") if isinstance(grant.get("paths"), dict) else {}
    folder = Path(session["folder"])
    ro = [*_python_binds(), str(PROJECT_ROOT / "swfactory"), str(grant_path), str(session["number_file"]),
          *runtime_binds(paths)]
    if session.get("preview_socket"):
        ro.append(str(session["preview_socket"]))
    limit = int((grant.get("limits") or {}).get("download_bytes") or 50_000_000)
    argv = minimal_args(ro=ro, rw=[str(folder)], sized=[(DOWNLOADS_DIR, limit)], chdir=str(folder / "work"))
    python = os.path.realpath(sys.executable)
    return argv + (inner or [python, "-I", "-c", BOOTSTRAP, str(PROJECT_ROOT), str(grant_path), json.dumps(session)])


def start_preview(grant: dict, folder: Path) -> tuple[subprocess.Popen, str] | None:
    """The job's preview in a sibling bwrap of its own (never nested in the browser's namespace, §2.9): its own
    network, PID and IPC namespaces, the worktree read-only and nothing writable; it listens on its own loopback,
    and a relay inside it hands connections from a Unix socket (created here, passed in as a descriptor) to that
    port. Returns (the process, the socket's path) once the socket answers, or None without a preview."""
    preview = grant.get("preview")
    if not isinstance(preview, dict) or not preview.get("command"):
        return None
    worktree = os.path.realpath(str(preview.get("dir") or "/"))
    port = int(preview.get("port") or PREVIEW_PORT)
    path = str(folder / "preview.sock")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    listener.listen(16)
    os.chmod(path, 0o600)
    listener.set_inheritable(True)
    python = os.path.realpath(sys.executable)
    relay = [python, "-I", "-c", "import sys; sys.path.insert(0, sys.argv.pop(1)); "
             "from swfactory.browser.fixture import main; sys.exit(main())", str(PROJECT_ROOT), "preview-relay",
             "--fd", str(listener.fileno()), "--port", str(port)]
    command = [str(part) for part in preview["command"]]
    script = ('"$@" & app=$!; ' + " ".join(f"'{part}'" for part in relay) + ' & wait "$app"')
    argv = minimal_args(ro=[*_python_binds(), str(PROJECT_ROOT / "swfactory"), worktree], chdir=worktree,
                        env={"PORT": str(port)}) + ["sh", "-c", script, "preview", *command]
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            pass_fds=(listener.fileno(),))
    listener.close()
    if not F.wait_for_socket(path, float(preview.get("startup_seconds") or 20)):
        proc.kill()
        return None
    return proc, path


def launch(argv: list[str]) -> int:
    """sf-browser mcp <grant>: lock, then exec bwrap over the shim. Returns only when it refuses."""
    if len(argv) != 2 or argv[0] != "mcp":
        sys.stderr.write("usage: sf-browser mcp <grant path>\n")
        return refusing("no_grant") if argv[:1] == ["mcp"] else 2
    grant_path = Path(argv[1])
    try:
        grant = read_grant(grant_path)
    except GrantError:
        return refusing("no_grant")
    if grant.get("revoked") or grant.get("ended"):
        return refusing("session_lock_revoked", at=(grant.get("revoked") or {}).get("at"))
    if shutil.which("bwrap") is None:
        return refusing("worker_unavailable", reason="bubblewrap is not installed")
    try:
        lock = take_session_lock(data_dir_of(grant_path), grant)
    except SessionLockBusy as exc:
        return refusing("session_lock_busy", locked_by_kind=exc.locked_by_kind)
    session = {"session_lock": lock.session_lock, "lock_number": lock.lock_number, "folder": str(lock.folder),
               "number_file": str(lock.number_file)}
    paths = grant.get("paths") if isinstance(grant.get("paths"), dict) else {}
    if paths.get("playwright_argv"):   # tests only: a fake Playwright MCP replaying the recorded responses
        session["playwright"] = [str(part) for part in paths["playwright_argv"]]
    if paths.get("record"):            # tests only: record the real one's responses into the session folder
        session["record"] = str(lock.folder / "playwright-calls.jsonl")
    preview = start_preview(grant, lock.folder)
    if preview is not None:
        session["preview_socket"] = preview[1]
    for fd in lock.descriptors():
        os.set_inheritable(fd, True)
    args = namespace_argv(grant_path.resolve(), grant, session)
    try:
        os.execvp(args[0], args)
    except OSError as exc:
        lock.close()
        return refusing("worker_unavailable", reason=f"bwrap could not start: {exc.strerror or exc}")
    return 1   # not reached


def main(argv: list[str] | None = None) -> int:
    return launch(list(sys.argv[1:] if argv is None else argv))
