"""What the harness core does around a Codex run (BROWSER_AND_CODEX.md §3.3, §3.6, §3.7, §3.11; DESIGN §44).

- **Which runs:** ``is_worker`` (a driver on the ``codex-worker`` preset), ``commits_by_harness`` (a preset whose
  property ``commits`` is ``harness``: the driver cannot commit, so the orchestrator does).
- **Before a job starts:** ``routing_problem`` (codex-worker runs the builder only, on a model on this machine (O4);
  on a repository not marked ``personal_data: false`` no other role, and no route of the builder, is on a hosted
  provider, because they read Codex's diff, build.md and the verify output), ``model_blocker`` (the doctor's
  ``codex model`` line as a blocker, with GETs only: §3.3 (c)) and ``containment_blocker`` (with a Codex user: the
  installed launcher is this version's and the uid egress rule is loaded, which lasts only until a reboot unless
  block c6's unit loads it).
- **Around a run:** ``open_worktree`` (the worktree writable by the group, before the run's start), ``run_folders``
  (the run's out/, tmp/ and shell/ outside the job folder; only they are the group's to write) and
  ``remove_run_folders``, ``share_job`` and ``unshare_job`` (the job folder readable by the group for the run only;
  a restart's recovery and the job's end take the share back too), ``deny_paths`` (what the permissions
  profile denies, every other job's paths and every other repository's checkout named one by one),
  ``launcher_argv`` (the launcher, directly, through sudo as the Codex user, or through setpriv for a harness
  running as root), ``collect`` (build.md copied into the job
  folder, what Codex created in the worktree given to the group, and that share's answer, the home's report) and
  ``share_worktree`` (that gift alone: also when a run raised, at a restart's recovery before it counts the ignored
  files and reconciles, and before the harness removes the worktree).
- **The data folder:** ``seal_data_dir`` (once per version of the sweep, what older versions left readable made the
  harness's own, the job folders' old ACLs included; the harness runs with umask 077 under its lock).
- **After a run:** ``commit`` (gitops.commit_all after the protected-path check and the secret scan), ``verify`` (the
  repository's verify command, always under bwrap; a ``check`` record), ``reconcile`` (on an unclean end: git status,
  the changed paths and the diff's hash), the ``Receipts`` store (``<data_dir>/receipts/<repo>/<job>/<run>.json``,
  opened with O_NOFOLLOW and O_EXCL, hash-chained per job) and ``remove_home`` at job end.
- **The Responses check:** ``responses_check`` sends one minimal Responses request under a custody request lease (the
  doctor's ``--probe`` only) and records the answer (status and body hash); ``check_record`` reads it back.
"""

from __future__ import annotations

import contextlib
import grp
import hashlib
import json
import os
import pwd
import re
import shutil
import signal
import stat
import subprocess
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from . import CODEX_PIN, PRESET, PROFILE, PROTECTED_PATHS, SURFACE, APPROVAL_POLICY
from . import launch as L
from .settings import (SERVER_LAUNCH_SETTINGS, key_file, launch_settings_path, launcher_path, repo_work, settings,
                       work_root)

RECEIPT_SCHEMA = "swf.receipt.run/1"
CHECK_SCHEMA = "swf.codex.responses-check/1"
OUTPUT_KEEP = 1 << 20          # bytes of verify output kept for its hash and tail
TAIL_CHARS = 4000
BUILD_MD_MAX = 1 << 20
SAFE_GIT_NAME = ("Software Factory", "factory@localhost")
_RECEIPT_LOCK = threading.Lock()


class WorkError(Exception):
    """A step around a Codex run cannot go on; the message says why."""


# ---------------------------------------------------------------- which drivers


def preset_of(config: Any, driver: str | None) -> str | None:
    try:
        cfg = config.driver(str(driver))
    except (KeyError, ValueError, AttributeError):
        return None
    return getattr(cfg, "preset", None) if getattr(cfg, "type", None) == "cli" else None


def is_worker(config: Any, driver: str | None) -> bool:
    return preset_of(config, driver) == PRESET


def commits_by_harness(config: Any, driver: str | None) -> bool:
    from ..drivers.cli import PRESETS

    return PRESETS.get(preset_of(config, driver) or "", {}).get("commits") == "harness"


def job_key(job: dict) -> str:
    return f"{job['repo']}--{job['id']}"


# ---------------------------------------------------------------- routes, the routing check, the model check


def _route_urls(route: Any) -> list[str]:
    """Every provider base_url a route may run on: its own, its builds', its overflow's, its escalation rungs' and
    an independent alternate's."""
    if not isinstance(route, dict):
        return []
    out = []
    entry = route.get("provider_entry") if isinstance(route.get("provider_entry"), dict) else {}
    if entry.get("base_url"):
        out.append(str(entry["base_url"]))
    for build in route.get("builds") or []:
        if isinstance(build, dict) and isinstance(build.get("provider_entry"), dict):
            out.append(str(build["provider_entry"].get("base_url") or ""))
    for key in ("overflow", "independent_alternate"):
        out += _route_urls(route.get(key))
    for rung in route.get("escalate") or []:
        out += _route_urls(rung)
    return out


def _route_drivers(route: Any) -> list[str]:
    if not isinstance(route, dict):
        return []
    out = [str(route.get("driver") or "")]
    for key in ("overflow", "independent_alternate"):
        out += _route_drivers(route.get(key))
    for rung in route.get("escalate") or []:
        out += _route_drivers(rung)
    return out


def is_local_url(url: str) -> bool:
    from ..settings import is_local_url as local

    return local(url)


def hosted_route(config: Any, route: Any) -> str | None:
    """Why a route may send what it reads off this machine (None: it stays here). A CLI driver other than
    codex-worker reaches an endpoint of its own the harness cannot see, so it counts as hosted (fail closed)."""
    for driver in _route_drivers(route):
        cfg = None
        with contextlib.suppress(KeyError, ValueError, AttributeError):
            cfg = config.driver(driver)
        if cfg is not None and cfg.type == "cli" and cfg.preset != PRESET:
            return f"the {driver} driver reaches an endpoint of its own"
    for url in _route_urls(route):
        if url and not is_local_url(url):
            return f"it runs on {urllib.parse.urlsplit(url).hostname or url}"
    return None


def worker_agents(config: Any, routing: dict) -> list[str]:
    """The agents of a routing snapshot whose route (or a rung of it) runs on codex-worker."""
    return [name for name, route in (routing or {}).items()
            if any(is_worker(config, driver) for driver in _route_drivers(route))]


def routing_problem(config: Any, repo: str, routing: dict) -> str | None:
    """§3.3's routing check, failing closed (None: the job may start)."""
    workers = worker_agents(config, routing)
    if not workers:
        return None
    others = [name for name in workers if name != "builder"]
    if others:
        return (f"codex-worker runs the builder only (BC1b); {', '.join(others)} is routed to it: route it to "
                f"another driver")
    builder = routing.get("builder")
    for url in _route_urls(builder):
        if url and not is_local_url(url):
            return ("the builder runs on codex-worker, which uses models on this machine only (the owner's O4): "
                    f"its route reaches {urllib.parse.urlsplit(url).hostname or url}")
    if repo_work(config, repo).personal_data:
        for name, route in sorted((routing or {}).items()):
            if name == "builder":
                continue
            why = hosted_route(config, route)
            if why:
                return (f"{name} would read what Codex wrote, and {why}: on a repository not marked "
                        f"personal_data: false no role of a codex-worker job leaves this machine "
                        f"(BROWSER_AND_CODEX.md §3.3)")
    return None


def worker_provider(model: Any) -> Any:
    return getattr(model, "provider", None)


def model_blocker(config: Any, provider: Any, model_id: str, *, residency: bool = True) -> str | None:
    """The doctor's codex model line as a blocker (§3.3 (c)), sending GETs only (to the custody register): None
    when a codex-worker run may start on provider/model_id."""
    from ..custody import client_for, needs_lease, register_names, server_of, settings_of

    if not settings(config)["enabled"]:
        return "the Codex worker is off (codex.enabled is false)"
    base_url = str(getattr(provider, "base_url", "") or "")
    if not L._loopback(base_url.rstrip("/")) and not L._loopback(base_url):
        return f"the Codex route's provider {getattr(provider, 'name', '?')} is not on this machine's loopback"
    if not needs_lease(provider):
        missing = ("has no custody register" if settings_of(provider) is None
                   else "sets neither lazy_load nor allow_model_swap")
        return (f"the Codex route's provider {provider.name} {missing}, so no custody request lease would cover a "
                f"Codex run: the harness sends such a server no POST at all (K20)")
    record = check_record(config).get(server_of(base_url))
    if not isinstance(record, dict) or not record.get("ok"):
        why = "has not passed" if record is None else f"failed ({record.get('detail') or record.get('status')})"
        return (f"the Responses route of {provider.name} {why} its check: run `factory doctor --probe` in a swap "
                f"window the owner schedules, under a custody request lease (K20)")
    if residency:
        register, model = register_names(provider, model_id)
        try:
            found = client_for(settings_of(provider)).observe()
        except Exception as exc:  # noqa: BLE001 - any failure to read is a blocker, never a pass
            return f"the custody register's residency cannot be read ({type(exc).__name__}: {exc})"
        if (register, model) not in found.on(register):
            return (f"the Responses endpoint is not loaded: the register lists "
                    f"{', '.join(m for _, m in found.on(register)) or 'nothing'} on {register}")
    return None


EGRESS_PROBE_PORTS = (9, 7, 13, 19, 37)   # loopback ports no model uses: the first one the rule does not allow


def containment_blocker(config: Any, provider: Any = None) -> str | None:
    """The host's containment of the Codex user as a blocker, at submit and before every Codex run (the cheap part of
    the doctor's codex launcher and egress lines). None without a Codex user (the test suite's containment, which the
    doctor names as such). Otherwise a refusal while the installed launcher is not this version's (block c3), or the
    uid egress rule is not loaded: a loopback connect from the Codex user to a port the rule does not allow must be
    dropped (refused or connected means no rule dropped it; nothing leaves this machine for the check), and one to
    the route's own model port (provider) must not be (an older rule would drop the model). A rule loaded by nft lasts
    until the next boot, unless block c6's unit loads it at boot, so this runs every time."""
    if not settings(config).get("user"):
        return None
    from .checks import file_sha256
    from .setup import EGRESS_UNIT, model_ports

    installed = launcher_path(config)
    if file_sha256(installed) != file_sha256(Path(L.__file__)):
        return f"the installed launcher {installed} is not this version's: install it again (block c3)"
    allowed = {port for _, port in model_ports(config)}
    probe = f"127.0.0.1:{next(port for port in EGRESS_PROBE_PORTS if port not in allowed)}"
    targets = [probe]
    base_url = str(getattr(provider, "base_url", "") or "").rstrip("/")
    parts = urllib.parse.urlsplit(base_url)
    own = None
    if L._loopback(base_url) and parts.port:
        own = f"{'[::1]' if parts.hostname == '::1' else '127.0.0.1'}:{parts.port}"
        targets.append(own)
    try:
        answer = run_launcher(config, "egress-probe", *sum((["--target", t] for t in targets), []), "--seconds", "1",
                              timeout=30)
    except (OSError, subprocess.SubprocessError, WorkError) as exc:
        return f"the uid egress rule could not be checked: {exc}"
    how = answer.get("how") if isinstance(answer.get("how"), dict) else None
    if how is None:
        return f"the uid egress rule could not be checked: {answer.get('error') or 'the launcher gave no answer'}"
    if how.get(probe) != "dropped":
        return (f"the uid egress rule is not loaded: a connect from the Codex user to {probe} was "
                f"{how.get(probe) or 'not tried'}, not dropped (block c6 loads it, and its unit {EGRESS_UNIT} at "
                f"every boot)")
    if own and how.get(own) == "dropped":
        return (f"the uid egress rule drops the Codex route's own model at {own}: stage again (block c0) and repeat "
                f"block c6")
    return None


def checks_dir(config: Any) -> Path:
    return Path(config.data_dir) / "receipts" / "_codex"


def check_record(config: Any) -> dict:
    """{server: the last Responses check}, from <data_dir>/receipts/_codex/responses-check.json."""
    try:
        data = json.loads((checks_dir(config) / "responses-check.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data.get("servers") if isinstance(data, dict) and isinstance(data.get("servers"), dict) else {}


def responses_check(config: Any, model: Any, purpose: str = "codex model check") -> dict:
    """§3.3 (b): under a custody request lease, one minimal Responses request; the answer (status and a hash of its
    body) is recorded for the provider's server. A denied lease sends nothing. Returns the record."""
    from .. import plainhttp
    from ..custody import CustodyDenied, leased, needs_lease, server_of

    provider = model.provider
    base_url = str(provider.base_url).rstrip("/")
    server = server_of(base_url)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if not needs_lease(provider):
        return {"server": server, "ok": False, "sent": False, "at": now,
                "detail": "no custody request lease would cover it: nothing sent"}
    try:
        with leased(model, purpose) as held:
            key = held.provider.api_key()
            headers = {"Accept": "application/json"}
            if key:
                headers["Authorization"] = f"Bearer {key}"
            body = {"model": str(model.model), "input": "Reply with: ok", "max_output_tokens": 16, "stream": False}
            try:
                reply = plainhttp.request("POST", base_url + "/responses", headers, body, timeout=60.0)
                status, raw = reply.status, reply.body
            except plainhttp.HttpFailure as exc:
                status, raw = None, b""
                detail = exc.detail
            else:
                detail = None
    except CustodyDenied as exc:
        return {"server": server, "ok": False, "sent": False, "at": now, "detail": f"the lease was denied: {exc}"}
    parsed: Any = None
    with contextlib.suppress(ValueError):
        parsed = json.loads(raw.decode("utf-8")) if raw else None
    well_formed = (status == 200 and isinstance(parsed, dict)
                   and (parsed.get("object") == "response" or isinstance(parsed.get("output"), list)))
    record = {"server": server, "base_url": base_url, "model": str(model.model), "ok": bool(well_formed),
              "sent": True, "status": status, "body_sha256": hashlib.sha256(raw).hexdigest(), "at": now,
              "detail": detail or ("a well-formed Responses answer" if well_formed
                                   else f"HTTP {status}: not a Responses answer")}
    folder = _private_dirs(checks_dir(config))
    path = folder / "responses-check.json"
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        current = {}
    servers = current.get("servers") if isinstance(current, dict) and isinstance(current.get("servers"), dict) else {}
    servers[server] = record
    _replace_private(path, json.dumps({"schema": CHECK_SCHEMA, "servers": servers}, indent=2).encode("utf-8"))
    return record


# ---------------------------------------------------------------- the run's folders, the deny list, the launcher


def _private_dirs(path: Path) -> Path:
    """path made (0700) with every missing parent; refused when any part of it is a symlink."""
    path = Path(path)
    for part in [*reversed(path.parents), path]:
        if part == Path(part.anchor):
            continue
        try:
            info = os.lstat(part)
        except FileNotFoundError:
            os.mkdir(part, 0o700)
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise WorkError(f"{part} is not a plain directory")
    return path


SEALED_MARK = ".private-modes"
SEAL_VERSION = "2"   # 2: the job folders' old ACLs too; a marker of an older sweep (or none) sweeps again
ACL_XATTRS = ("system.posix_acl_access", "system.posix_acl_default")


def shared_dirs(config: Any) -> list[Path]:
    """What the setup's block c2 shares with the Codex user's group by design: the worktrees and the work root (their
    ACLs and setgid modes are the owner's, and seal_data_dir leaves them alone)."""
    return [Path(config.data_dir) / "worktrees", work_root(config)]


def _sealed(marker: Path) -> bool:
    try:
        fd = os.open(str(marker), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return False
    try:
        return stat.S_ISREG(os.fstat(fd).st_mode) and os.read(fd, 64).decode("ascii", "replace").strip() == SEAL_VERSION
    except OSError:
        return False
    finally:
        os.close(fd)


def seal_data_dir(data_dir: Path | str, keep: Iterable[Path | str] = ()) -> int | None:
    """Once per data folder and SEAL_VERSION (a marker file records it): the group and other bits come off everything
    older versions created there under the default umask (0755 folders, 0644 chat transcripts and job logs), now that
    the harness runs with umask 077. Block c2 lets the Codex user's group pass through the data folder, so it is only
    as private as what is in it. Folders keep their group x, where an ACL's traverse entry (block c2's on the data
    folder and jobs/) lives; alone it lists nothing. Below each job folder (jobs/<repo>/<job> and everything in it)
    the extended ACLs come off too, first: an older version shared a job folder with the group and never took the
    share back, and its default entries would hand every file the harness writes there later (a run's log) to the
    group, whatever the umask, while chmod alone changes only the mask. No job is in flight now (this runs under the
    lock, before the restart's recovery). The worktrees and keep (shared_dirs: the work root), symlinks and what is
    not the harness's own are left alone. The walk keeps its own stack (os.walk recurses before Python 3.12): a tree
    deeper than the recursion limit is swept like any other, and what cannot be read is passed by, never a reason for
    the start to fail. Returns how many entries changed (None: swept before)."""
    data_dir = Path(data_dir)
    marker = data_dir / SEALED_MARK
    if _sealed(marker):
        return None
    skip = {os.path.abspath(str(p)) for p in (data_dir / "worktrees", *keep)}
    jobs = os.path.abspath(str(data_dir / "jobs"))
    uid = os.getuid()
    changed = 0

    def seal(path: str, strip: bool) -> bool:
        """Seal one entry; whether it is a folder to walk into."""
        nonlocal changed
        try:
            info = os.lstat(path)
        except OSError:
            return False
        if stat.S_ISLNK(info.st_mode):
            return False
        is_dir = stat.S_ISDIR(info.st_mode)
        if info.st_uid != uid:
            return is_dir
        if strip:
            for name in ACL_XATTRS if is_dir else ACL_XATTRS[:1]:
                with contextlib.suppress(OSError):
                    os.removexattr(path, name, follow_symlinks=False)
                    changed += 1
        with contextlib.suppress(OSError):
            mode = stat.S_IMODE(os.lstat(path).st_mode)
            sealed = mode & ~(0o067 if is_dir else 0o077)
            if sealed != mode:
                os.chmod(path, sealed)
                changed += 1
        return is_dir

    def in_job(path: str) -> bool:   # jobs/<repo>/<job> and below
        rel = os.path.relpath(path, jobs)
        return not rel.startswith("..") and len(Path(rel).parts) >= 2

    seal(str(data_dir), False)
    stack = [str(data_dir)]
    while stack:
        top = stack.pop()
        try:
            with os.scandir(top) as listing:
                names = [entry.name for entry in listing]
        except OSError:
            continue
        for name in names:
            path = os.path.join(top, name)
            if os.path.abspath(path) in skip:
                continue
            if seal(path, in_job(path)):
                stack.append(path)
    with contextlib.suppress(OSError):
        _replace_private(marker, (SEAL_VERSION + "\n").encode("ascii"))
    return changed


def _group_id(config: Any) -> int | None:
    name = settings(config).get("group")
    if not name:
        return None
    try:
        return grp.getgrnam(str(name)).gr_gid
    except KeyError:
        raise WorkError(f"the worktree group {name} does not exist (codex.group; the setup block creates it)") from None


RUN_SHARED = ("out", "tmp", "shell")


def run_folders(config: Any, job: dict, run: str) -> dict[str, Path]:
    """<work_root>/runs/<repo>--<job>/<run>/{out,tmp,shell}, made fresh for this run. Never inside the job folder:
    nothing Codex writes there is trusted later. With codex.group set, only out/, tmp/ and shell/ are the group's to
    write (2770). The work root, runs/, runs/<repo>--<job>/ and the run's own folder stay the harness's (2750: the
    group passes through and lists, but never renames or replaces a folder), so no folder the harness later deletes
    or reads from can be swapped for a symlink. A part an older setup left writable for the group is tightened, or
    the run is refused when that part is not the harness's own."""
    root = work_root(config)
    gid = _group_id(config)
    shared, own = (0o2770, 0o2750) if gid is not None else (0o700, 0o700)
    base = root / "runs" / job_key(job) / run
    _private_dirs(root)
    for part in (root, root / "runs", root / "runs" / job_key(job), base):
        try:
            info = os.lstat(part)
        except FileNotFoundError:
            os.mkdir(part, own)
            if gid is not None:
                os.chown(part, -1, gid)
                os.chmod(part, own)
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise WorkError(f"{part} is not a plain directory")
        if info.st_mode & 0o022:
            if info.st_uid != os.getuid():
                raise WorkError(f"{part} is writable by others than the harness, which cannot tighten it (not its "
                                f"own): chmod g-w,o-w it (the setup's block c2 makes it 2750)")
            os.chmod(part, stat.S_IMODE(info.st_mode) & ~0o022)
    fd = _open_below(root, base.relative_to(root).parts)
    try:
        for name in RUN_SHARED:
            _remove_at(fd, name)
            os.mkdir(name, shared, dir_fd=fd)
            if gid is not None:
                os.chown(name, -1, gid, dir_fd=fd, follow_symlinks=False)
                os.chmod(name, shared, dir_fd=fd)
    finally:
        os.close(fd)
    out = {name: base / name for name in RUN_SHARED}
    out["base"] = base
    return out


def _open_below(top: Path, names: Iterable[str]) -> int:
    """An fd of the directory top/<names...>: each name is opened below the one before with O_NOFOLLOW, so a folder
    swapped for a symlink fails (ELOOP, ENOTDIR) rather than leading somewhere else. The caller closes it."""
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(str(top), flags)
    try:
        for name in names:
            child = os.open(name, flags | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


def _remove_at(parent_fd: int, name: str) -> None:
    """Delete name in the open directory parent_fd and everything below it, through directory fds: a symlink is
    removed as itself and never followed. Best effort, as shutil.rmtree(ignore_errors=True)."""
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode):
            os.unlink(name, dir_fd=parent_fd)
            return
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                     dir_fd=parent_fd)
    except OSError:
        return
    try:
        with os.scandir(fd) as entries:
            names = [entry.name for entry in entries]
        for child in names:
            _remove_at(fd, child)
    except (OSError, RecursionError):
        pass
    finally:
        os.close(fd)
    with contextlib.suppress(OSError):
        os.rmdir(name, dir_fd=parent_fd)


def _run_base_fd(folders: dict[str, Path]) -> int | None:
    """An fd of the run's own folder, opened from the work root down (runs/<repo>--<job>/<run>, no symlink)."""
    base = folders.get("base")
    if base is None:
        return None
    root = base.parents[2]
    try:
        return _open_below(root, base.relative_to(root).parts)
    except OSError:
        return None


def _setfacl(*args: str) -> None:
    setfacl = shutil.which("setfacl")
    if setfacl is None:
        return
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run([setfacl, *args], stdin=subprocess.DEVNULL, capture_output=True, timeout=60, check=False)


def open_worktree(config: Any, job: dict) -> bool:
    """With a worktree group (codex.group), before a Codex run starts: the job's worktree writable by the group,
    through ACLs on the harness's own files (no root, nothing host-wide; the owner's default ACLs of the setup's
    block c2 cover new files in the worktree, but a file made with a narrower mode, as a repository's setup makes
    them, takes the group's entry from here). setfacl changes the ctime of every file whose ACL it changes, and a
    run's changes to the files git ignores are told by their ctime, so the orchestrator calls this before it notes
    the run's start and lists them (Factory._ignored_left); the driver never does. The worktree stays shared (block
    c2 shares the worktrees by design). Returns whether there is a group (False: nothing was done)."""
    name = settings(config).get("group")
    if not name:
        return False
    spec = f"g:{name}:rwX"
    _setfacl("-R", "-m", spec, "-m", f"d:{spec}", str(Path(job["worktree"])))
    return True


def share_job(config: Any, job: dict) -> None:
    """With a worktree group (codex.group), for one Codex run: the job's own folder readable by the group (and
    jobs/<repo>/, which the harness creates private, passable: x only, so the group lists nothing there), through
    ACLs on the harness's own files. unshare_job takes the job folder's share back when the run ends; when the
    harness was killed mid-run, its restart's recovery and the job's end do. The worktree is open_worktree's, before
    the run starts. Without a group, nothing."""
    name = settings(config).get("group")
    if not name:
        return
    job_dir = Path(job["job_dir"])
    _setfacl("-m", f"g:{name}:x", str(job_dir.parent))
    spec = f"g:{name}:rX"
    _setfacl("-R", "-m", spec, "-m", f"d:{spec}", str(job_dir))


def unshare_job(config: Any, job: dict) -> None:
    """When the Codex run ends (and at a restart's recovery of a run in flight, and at job end): the group's entries
    and default entries come off every file and folder in the job's own folder, so nothing the harness writes there
    later is the Codex user's to read. The worktree stays shared (block c2 shares the worktrees by design), and so
    does the traverse on jobs/<repo>/ (it lists nothing)."""
    name = settings(config).get("group")
    if not name:
        return
    _setfacl("-R", "-x", f"g:{name}", "-x", f"d:g:{name}", str(Path(job["job_dir"])))


def remove_run_folders(folders: dict[str, Path]) -> None:
    """The run's tmp/ and shell/ are deleted at the end of the run (out/ stays until the job ends), through
    directory fds opened from the work root down: a run folder swapped for a symlink is never followed."""
    fd = _run_base_fd(folders)
    if fd is None:
        return
    try:
        for name in ("tmp", "shell"):
            _remove_at(fd, name)
    finally:
        os.close(fd)


def _others(folder: Path, keep: str) -> list[str]:
    try:
        return [str(folder / name) for name in sorted(os.listdir(folder)) if name != keep]
    except OSError:
        return []


def deny_paths(config: Any, job: dict) -> list[str]:
    """What the factory_worker profile denies for this job's run (§3.11 item 2): the harness's config directory and
    ~/.config/software-factory, every key file and env file, what the operator and the owner keep in the harness
    user's home (~/.ssh, ~/.gnupg, ~/.codex: the operator's own Codex), every top-level entry of the data directory
    but jobs/ and worktrees/ (and the work root), every other repository's and every other job's folder and
    worktree, every other job's home and run folders. A denied folder reads as missing and a read entry below it
    does not open it again (verified at the pin), so other jobs are named one by one; what the job may read (its
    own job folder and worktree) stays open."""
    from ..config import USER_DIR

    data = Path(config.data_dir)
    root = work_root(config)
    repo, job_id = str(job["repo"]), str(job["id"])
    out = [str(config.path.parent), os.path.expanduser(USER_DIR)]
    home = Path(os.path.expanduser("~"))
    out += [str(home / name) for name in (".ssh", ".gnupg", ".codex", ".netrc", ".git-credentials", ".aws",
                                          ".docker", ".kube")]
    providers = (getattr(config, "raw", {}) or {}).get("providers") or {}
    for entry in providers.values():
        if isinstance(entry, dict) and entry.get("api_key_file"):
            out.append(os.path.expanduser(str(entry["api_key_file"])))
    keep_top = {"jobs", "worktrees"}
    with contextlib.suppress(ValueError):
        keep_top.add(str(root.relative_to(data)).split(os.sep)[0])
    with contextlib.suppress(OSError):
        out += [str(data / name) for name in sorted(os.listdir(data)) if name not in keep_top]
    for top in ("jobs", "worktrees"):
        out += _others(data / top, repo)
        out += _others(data / top / repo, job_id)
    with contextlib.suppress(ValueError):
        if root.relative_to(data):
            out += [str(root / name) for name in sorted(os.listdir(root)) if name not in ("homes", "runs")] \
                if root.is_dir() else []
    out += _others(root / "homes", job_key(job))
    out += _others(root / "runs", job_key(job))
    out += other_repositories(config, job)
    return [path for path in dict.fromkeys(os.path.abspath(p) for p in out) if os.path.lexists(path)]


def other_repositories(config: Any, job: dict) -> list[str]:
    """Every configured repository's checkout but the job's own (another repository's code and history, which a
    folder the Codex user may pass through, such as a 0750 home with block c2's traverse entry, would otherwise leave
    readable), except one that holds what the run needs: the job's own repository, its worktree, its job folder, the
    data folder or the work root (a deny entry there would deny them too)."""
    try:
        repos = list(config.repos())
    except (AttributeError, KeyError, ValueError, OSError):
        return []
    needed = [os.path.abspath(str(p)) for p in (job.get("worktree") or "", job.get("job_dir") or "", config.data_dir,
                                                 work_root(config)) if str(p)]
    for repo in repos:
        if repo.name == job.get("repo"):
            needed.append(os.path.abspath(str(repo.path)))
    out = []
    for repo in repos:
        path = os.path.abspath(str(repo.path))
        if repo.name == job.get("repo") or any(need == path or need.startswith(path.rstrip("/") + "/")
                                               for need in needed):
            continue
        out.append(path)
    return out


def launch_settings(config: Any) -> dict:
    """The launcher's settings as this harness would write them (without a Codex user they are written into the
    work root; with one the owner installs them as root, and the doctor compares)."""
    merged = settings(config)
    providers = []
    for name, entry in ((getattr(config, "raw", {}) or {}).get("providers") or {}).items():
        if not isinstance(entry, dict) or not entry.get("base_url") or not entry.get("api_key_env"):
            continue
        base_url = str(entry["base_url"]).rstrip("/")
        if L._loopback(base_url):
            providers.append({"name": name, "base_url": base_url, "env_key": str(entry["api_key_env"]),
                              "key_file": key_file(config, entry.get("api_key_file"))})
    binary = merged.get("binary")
    return {"v": 1, "binary": os.path.expanduser(binary) if binary else "/nonexistent/codex",
            "pin": merged["pin"], "worktrees": str(Path(config.data_dir) / "worktrees"),
            "work_root": str(work_root(config)), "path": L.DEFAULT_PATH, "providers": providers,
            "retries": dict(merged["retries"]), "deny": [], "verify_binds": []}


def ensure_launch_settings(config: Any) -> Path:
    """The settings file the launcher reads: written now (0600) when the harness has no Codex user; with one, the
    root-owned file the owner installed (WorkError when it is missing)."""
    merged = settings(config)
    path = launch_settings_path(config)
    if merged.get("user") and merged.get("via") == "sudo":
        if not path.is_file():
            raise WorkError(f"the launcher's settings {path} are not installed (factory codex setup prints the block)")
        return path
    _private_dirs(path.parent)
    data = json.dumps(launch_settings(config), indent=2, sort_keys=True).encode("utf-8")
    try:
        if path.read_bytes() == data:
            return path
    except OSError:
        pass
    _replace_private(path, data)
    if merged.get("user"):   # setpriv: the Codex user must read it
        os.chmod(path, 0o644)
        os.chmod(path.parent, 0o755)
    return path


def launcher_prefix(config: Any) -> list[str]:
    """How the launcher is started: directly (no Codex user), `sudo -n -u <user>` (the server: no prompt ever) or
    setpriv (a harness running as root, as the test suite may)."""
    merged = settings(config)
    launcher = str(launcher_path(config))
    user = merged.get("user")
    if not user:
        return [launcher, "--settings", str(ensure_launch_settings(config))]
    if merged["via"] == "sudo":
        path = launch_settings_path(config)
        tail = [] if str(path) == SERVER_LAUNCH_SETTINGS else ["--settings", str(path)]
        return ["sudo", "-n", "-u", str(user), launcher, *tail]
    if os.geteuid() != 0:
        raise WorkError("codex.via setpriv needs a harness running as root (the server uses sudo)")
    try:
        account = pwd.getpwnam(str(user))
    except KeyError:
        raise WorkError(f"the Codex user {user} does not exist (codex.user; the setup block creates it)") from None
    gid = _group_id(config)
    groups = ["--groups", str(gid)] if gid is not None else ["--clear-groups"]
    return ["setpriv", "--reuid", str(account.pw_uid), "--regid", str(account.pw_gid), *groups, "--no-new-privs",
            launcher, "--settings", str(ensure_launch_settings(config))]


def uses_sudo(config: Any) -> bool:
    merged = settings(config)
    return bool(merged.get("user")) and merged["via"] == "sudo"


def launcher_argv(config: Any, verb: str, *args: str) -> list[str]:
    return [*launcher_prefix(config), verb, *args]


def launcher_env(config: Any, base_url: str | None = None) -> dict[str, str]:
    """The launcher's own environment: PATH and LANG, and, without a Codex user and without a key file, the one
    provider key Codex needs (the launcher hands it to Codex only)."""
    env = {"PATH": L.DEFAULT_PATH, "LANG": "C.UTF-8"}
    if base_url and not settings(config).get("user"):
        for entry in launch_settings(config)["providers"]:
            if entry["base_url"] == base_url.rstrip("/") and not entry.get("key_file"):
                value = os.environ.get(entry["env_key"])
                if value:
                    env[entry["env_key"]] = value
    return env


def run_launcher(config: Any, verb: str, *args: str, timeout: float = 60.0) -> dict:
    """A launcher verb that answers JSON (report, check-home, remove-home)."""
    argv = launcher_argv(config, verb, *args)
    done = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
                          env=launcher_env(config))
    text = (done.stdout or b"").decode("utf-8", "replace").strip()
    try:
        answer = json.loads(text.splitlines()[-1]) if text else {}
    except ValueError:
        answer = {}
    if not isinstance(answer, dict):
        answer = {}
    answer["exit"] = done.returncode
    if done.returncode not in (0, 3):
        answer["error"] = (done.stderr or b"").decode("utf-8", "replace").strip()[-300:]
    return answer


def collect(config: Any, job: dict, folders: dict[str, Path], job_dir: Path) -> dict:
    """After a run: build.md copied from the run's out/ into the job folder (a regular file only, opened from the work
    root down with no symlink anywhere on the way, never a FIFO, capped), what Codex created in the worktree made
    the group's (share_worktree; its answer is "share": with an error there nothing of the run is committed), and the
    home's report (its check again, the rollouts' hash) from the launcher."""
    copied = False
    data = None
    fd = _run_base_fd(folders)
    if fd is not None:
        try:
            out_fd = os.open("out", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                             dir_fd=fd)
            try:
                data = L._read_regular(Path("build.md"), BUILD_MD_MAX, dir_fd=out_fd)
            finally:
                os.close(out_fd)
        except (OSError, L.HomeError):
            data = None
        finally:
            os.close(fd)
    if data is not None:
        target = job_dir / "build.md"
        if target.is_symlink():
            target.unlink()
        tmp = target.with_name(".build.md.from-codex")
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        os.replace(tmp, target)
        copied = True
    share = share_worktree(config, job)
    try:
        home = run_launcher(config, "report", "--job", f"{job['repo']}/{job['id']}")
    except (OSError, subprocess.SubprocessError) as exc:
        home = {"error": str(exc)}
    return {"build_md_copied": copied, "home": home, "share": share}


def share_worktree(config: Any, job: dict) -> dict:
    """With a Codex user, after its run (collect, or the launch's finally when the run raised), at a restart's
    recovery before its reconcile, and before the harness removes the worktree: the launcher's share-worktree, as
    that user, makes what Codex created in the worktree the group's to read and write, so the harness (a member of
    the group) can commit, reconcile, revert and remove it; a file Codex wrote 0600 or a folder it made 0700 would
    otherwise be closed to it. Without a Codex user the harness owns what Codex wrote: nothing to do."""
    if not settings(config).get("user"):
        return {}
    try:
        return run_launcher(config, "share-worktree", "--job", f"{job['repo']}/{job['id']}", "--worktree",
                            str(job["worktree"]))
    except (OSError, subprocess.SubprocessError, WorkError, ValueError, KeyError) as exc:
        return {"error": str(exc)}   # never raises: a restart's recovery and a removal carry on without it


def remove_home(config: Any, job: dict) -> dict:
    try:
        return run_launcher(config, "remove-home", "--job", f"{job['repo']}/{job['id']}")
    except (OSError, subprocess.SubprocessError) as exc:
        return {"error": str(exc)}


# ---------------------------------------------------------------- after a run: commit, verify, reconcile


def spec_paths(job_dir: Path) -> set[str]:
    """Paths the job's spec names in backticks or as words with a slash or a dot: a protected path is committed only
    when the spec names it."""
    try:
        text = (Path(job_dir) / "spec.md").read_text(encoding="utf-8", errors="replace")[:200_000]
    except OSError:
        return set()
    return {match.strip("`'\"()[],:;").rstrip(".") for match in re.findall(r"[`'\"]?[\w./-]+[`'\"]?", text)}


def commit(config: Any, job: dict, round_: int, secrets: Iterable[str], extra_texts: Iterable[str] = ()) -> dict:
    """The harness commit after a builder run on a commits: harness driver (§3.3): gitops.commit_all with global and
    system config off, the protected paths refused unless the spec names them, the staged diff and build.md
    scanned for every secret value. Returns {by, sha, response, paths} ({} when nothing changed); WorkError for a
    refusal (nothing is committed then)."""
    from .. import gitops

    worktree, repo_path = Path(job["worktree"]), Path(config.repo(job["repo"]).path)
    gitops.verify_worktree(repo_path, worktree)
    message = f"factory: {job['id']} build round {round_} (builder on {PRESET})"
    try:
        result = gitops.commit_all(worktree, message, protected=list(PROTECTED_PATHS),
                                   allowed=spec_paths(Path(job["job_dir"])), secrets=list(secrets),
                                   extra_texts=list(extra_texts))
    except gitops.CommitRefused as exc:
        raise WorkError(str(exc)) from None
    if result is None:
        return {}
    return {"by": "harness", "sha": result.sha, "paths": result.paths,
            "response": {"status": result.status, "ids": result.ids, "body_sha256": result.body_sha256}}


def verify(config: Any, job: dict, run: str, commit_sha: str | None, folders: dict[str, Path] | None = None, *,
           ignored_left: Iterable[str] | None = (), cancel: threading.Event | None = None) -> dict:
    """The repository's verify command, always under bwrap (§3.3): the worktree read-only, a private HOME and /tmp,
    a clear environment, no network, gone with its parent; as the Codex user through the launcher when there is
    one, started with the launcher's own environment only (PATH and LANG). Returns the check record {kind: verify,
    argv, exit, seconds, output_sha256, commit, check, tail}.

    verify runs in the worktree, so its result speaks for the commit only when the worktree holds nothing the
    commit does not: ignored_left (the ignored files the job's Codex runs left there; None when they could not be
    listed) fails the check without running it (not_run: a result about the worktree, never the commit's). cancel
    (the job's Stop) ends the command's process group at once: the check is failed, "stopped"."""
    from ..harness.sandbox import bwrap_available

    work = repo_work(config, job["repo"])
    if not work.verify:
        return {}
    worktree = Path(job["worktree"])
    record: dict[str, Any] = {"kind": "verify", "argv": ["bash", "-c", work.verify], "commit": commit_sha}
    failed = {"exit": None, "seconds": 0.0, "output_sha256": None, "check": "failed"}
    left = None if ignored_left is None else list(ignored_left)
    if left is None:
        return {**record, **failed, "not_run": True,
                "reason": "the worktree's ignored files could not be listed, so verify could not run on the commit alone"}
    if left:
        shown = ", ".join(left[:5]) + (f" and {len(left) - 5} more" if len(left) > 5 else "")
        return {**record, **failed, "not_run": True, "ignored": left[:50],
                "reason": f"the run left files git ignores, which the commit does not hold: {shown}; remove them "
                          f"(verify runs on the commit alone)"}
    if not bwrap_available():
        return {**record, **failed,
                "reason": "bubblewrap is not usable here, so verify did not run (the doctor's sandbox line)"}
    if settings(config).get("user"):
        if folders is None:
            folders = run_folders(config, job, run)
        argv = launcher_argv(config, "verify", "--job", f"{job['repo']}/{job['id']}", "--run", run,
                             "--worktree", str(worktree), *sum((["--bind", p] for p in work.verify_binds), []),
                             "--", work.verify)
    else:
        argv = L.verify_argv(worktree, work.verify, work.verify_binds)
    started = time.monotonic()
    output, code, ended = _run_capped(argv, work.verify_seconds, launcher_env(config), cancel)
    seconds = round(time.monotonic() - started, 3)
    record.update(exit=code, seconds=seconds, output_sha256=hashlib.sha256(output).hexdigest(),
                  tail=output.decode("utf-8", "replace")[-TAIL_CHARS:],
                  check="ok" if code == 0 and ended is None else "failed")
    if ended == "timed_out":
        record["reason"] = f"verify ran longer than {work.verify_seconds:g}s and was stopped"
    elif ended == "cancelled":
        record.update(reason="verify was stopped (Stop)", stopped=True)
    from .. import gitops

    with contextlib.suppress(gitops.GitError):
        if not gitops.is_clean(worktree):
            record.update(check="failed", reason="the worktree was not clean after verify")
    return record


def _run_capped(argv: list[str], seconds: float, env: dict[str, str] | None = None,
                cancel: threading.Event | None = None) -> tuple[bytes, int | None, str | None]:
    """Run argv in a process group of its own with env (the caller's minimal one; never the harness's), its output
    capped at OUTPUT_KEEP; (output, exit status, None, or why it was ended early: "timed_out" past seconds,
    "cancelled" once cancel is set; the whole group is killed either way)."""
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            start_new_session=True, env=env)
    chunks: list[bytes] = []
    size = [0]

    def pump() -> None:
        assert proc.stdout is not None
        for chunk in iter(lambda: proc.stdout.read(1 << 16), b""):
            if size[0] < OUTPUT_KEEP:
                chunks.append(chunk[:OUTPUT_KEEP - size[0]])
            size[0] += len(chunk)

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    ended: str | None = None
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        try:
            proc.wait(timeout=max(0.0, min(0.2, remaining)))
            break
        except subprocess.TimeoutExpired:
            pass
        if cancel is not None and cancel.is_set():
            ended = "cancelled"
        elif time.monotonic() >= deadline:
            ended = "timed_out"
        if ended:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            break
    reader.join(5)
    if proc.stdout is not None:
        proc.stdout.close()
    return b"".join(chunks), proc.returncode, ended


def reconcile(config: Any, job: dict) -> dict:
    """What a run that did not end cleanly left (§3.6): git status, the changed paths and the sha256 of the diff
    (with untracked files' names and contents), with global and system config off. Read only: nothing is
    committed, reverted or sent again."""
    from .. import gitops

    worktree = Path(job["worktree"])
    try:
        gitops.verify_worktree(Path(config.repo(job["repo"]).path), worktree)
        found = gitops.reconcile_diff(worktree)
    except (gitops.GitError, KeyError, ValueError, OSError) as exc:
        return {"error": f"the worktree could not be looked at: {exc}"}
    return found


def retry_summary(reconciled: dict) -> str:
    paths = reconciled.get("paths") or []
    if reconciled.get("error"):
        return f"the previous run's worktree could not be read ({reconciled['error']}); check it before you retry"
    if not paths:
        return "the previous run left no changes; the retry starts from the last commit"
    shown = ", ".join(paths[:5]) + (f" and {len(paths) - 5} more" if len(paths) > 5 else "")
    return f"the previous run left changes in {shown}; the retry starts from them"


# ---------------------------------------------------------------- receipts


def _replace_private(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.new")
    with contextlib.suppress(FileNotFoundError):
        os.unlink(tmp)
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


RUN_NAME_RE = re.compile(r"^[0-9]{2,4}-[a-z0-9][a-z0-9-]{0,40}$")


class Receipts:
    """Run receipts in <data_dir>/receipts/<repo>/<job>/<run>.json (§3.7): outside every root an agent can write,
    each opened with O_NOFOLLOW | O_EXCL (a planted file or symlink is never followed or overwritten), mode 0600,
    and hash-chained per job (each records the sha256 of the job's previous receipt)."""

    def __init__(self, data_dir: Path | str) -> None:
        self.root = Path(data_dir) / "receipts"

    def folder(self, repo: str, job_id: str) -> Path:
        if not L.REPO_RE.fullmatch(str(repo)) or not L.JOB_RE.fullmatch(str(job_id)):
            raise WorkError("not a job")
        return self.root / repo / job_id

    def names(self, repo: str, job_id: str) -> list[str]:
        folder = self.folder(repo, job_id)
        try:
            return sorted(name[:-5] for name in os.listdir(folder)
                          if name.endswith(".json") and RUN_NAME_RE.fullmatch(name[:-5]))
        except OSError:
            return []

    def last_sha256(self, repo: str, job_id: str) -> str | None:
        names = self.names(repo, job_id)
        if not names:
            return None
        return hashlib.sha256(self._read(repo, job_id, names[-1])).hexdigest()

    def _read(self, repo: str, job_id: str, run: str) -> bytes:
        if not RUN_NAME_RE.fullmatch(str(run)):
            raise KeyError(f"no receipt {run}")
        try:
            return L._read_regular(self.folder(repo, job_id) / f"{run}.json", 4 << 20)
        except (OSError, L.HomeError):
            raise KeyError(f"no receipt {run}") from None

    def read(self, repo: str, job_id: str, run: str) -> dict:
        return json.loads(self._read(repo, job_id, run))

    def write(self, repo: str, job_id: str, run: str, record: dict) -> tuple[str, str]:
        """Write the receipt (its previous_sha256 filled in); (its path relative to data_dir, its sha256)."""
        if not RUN_NAME_RE.fullmatch(str(run)):
            raise WorkError(f"not a run name: {run}")
        with _RECEIPT_LOCK:
            folder = _private_dirs(self.folder(repo, job_id))
            record = {"schema": RECEIPT_SCHEMA, "previous_sha256": self.last_sha256(repo, job_id), **record}
            data = (json.dumps(record, indent=2, sort_keys=False) + "\n").encode("utf-8")
            path = folder / f"{run}.json"
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                         0o600)
            try:
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
        return f"receipts/{repo}/{job_id}/{run}.json", hashlib.sha256(data).hexdigest()

    def verify_chain(self, repo: str, job_id: str) -> list[str]:
        """The runs whose previous_sha256 does not match the receipt before them (empty: the chain holds)."""
        broken, previous = [], None
        for name in self.names(repo, job_id):
            raw = self._read(repo, job_id, name)
            try:
                if json.loads(raw).get("previous_sha256") != previous:
                    broken.append(name)
            except ValueError:
                broken.append(name)
            previous = hashlib.sha256(raw).hexdigest()
        return broken

    def summaries(self, repo: str, job_id: str) -> list[dict]:
        """What the run card shows of each receipt: ids, outcome, paths, the commit, the checks; never text."""
        out = []
        for name in self.names(repo, job_id):
            try:
                raw = self._read(repo, job_id, name)
                record = json.loads(raw)
            except (KeyError, ValueError):
                continue
            out.append(summary(name, record, hashlib.sha256(raw).hexdigest()))
        return out

    def prune(self, max_bytes: int, max_days: float, keep: Callable[[str, str], bool] = lambda r, j: False) -> int:
        """The caps the browser's sessions keep too (the owner's O6: 5 GB, 30 days): whole jobs' receipt folders,
        oldest first, never one keep(repo, job) holds (a job still open). Returns how many were removed."""
        folders = []
        with contextlib.suppress(OSError):
            for repo in os.listdir(self.root):
                if not L.REPO_RE.fullmatch(repo):
                    continue
                for job_id in os.listdir(self.root / repo):
                    path = self.root / repo / job_id
                    if path.is_dir() and not path.is_symlink():
                        files = [p for p in path.iterdir() if p.is_file()]
                        newest = max((p.stat().st_mtime for p in files), default=path.stat().st_mtime)
                        folders.append((newest, sum(p.stat().st_size for p in files), repo, job_id, path))
        folders.sort()
        total = sum(size for _, size, *_ in folders)
        removed = 0
        cutoff = time.time() - max_days * 86400
        for newest, size, repo, job_id, path in folders:
            if keep(repo, job_id):
                continue
            if newest < cutoff or total > max_bytes:
                shutil.rmtree(path, ignore_errors=True)
                total -= size
                removed += 1
        return removed


def summary(run: str, record: dict, sha256: str) -> dict:
    codex = record.get("codex") if isinstance(record.get("codex"), dict) else {}
    artifacts = record.get("artifacts") if isinstance(record.get("artifacts"), dict) else {}
    commit_ = record.get("commit") if isinstance(record.get("commit"), dict) else {}
    checks = [c for c in record.get("checks") or [] if isinstance(c, dict)]
    return {"run": run, "sha256": sha256, "outcome": record.get("run_outcome"), "result": record.get("result"),
            "started": record.get("started"), "ended": record.get("ended"),
            "codex": {key: codex.get(key) for key in ("version", "surface", "permissions_profile", "approval_policy",
                                                     "thread_id")},
            "provider": (codex.get("provider") or {}).get("base_url") if isinstance(codex.get("provider"), dict)
            else None,
            "home_verified": ((codex.get("home") or {}).get("verified") if isinstance(codex.get("home"), dict)
                              else None),
            "paths": list(artifacts.get("paths") or [])[:50], "diff_sha256": artifacts.get("diff_sha256"),
            "commit": commit_.get("sha"), "commit_by": commit_.get("by"),
            "checks": [{key: c.get(key) for key in ("kind", "ok", "check", "exit", "seconds", "reason")}
                       for c in checks],
            "reconciled": record.get("reconciled"), "browser": len(record.get("browser") or []),
            "previous_sha256": record.get("previous_sha256")}


def run_record(*, job: dict, run: str, agent: str, attempt: int, grid: Any, version: str | None, thread_id: str | None,
               model: str, provider: Any, home: dict, started: str, ended: str, outcome: str, error: str | None,
               artifacts: dict, checks: list[dict], commit_: dict | None, reconciled: dict | None,
               result: str, exit_code: int | None = None) -> dict:
    """The receipt of one Codex run, in the shape of §3.7."""
    return {
        "request": {"ref": f"job:{job['repo']}/{job['id']}", "run": run, "agent": agent, "attempt": attempt,
                    "client_message_id": f"{run}-{attempt}", "grid": grid},
        "codex": {"version": version or CODEX_PIN, "surface": SURFACE, "schema_sha256": None, "thread_id": thread_id,
                  "turn_ids": [], "model": {"asked": model, "reported": None},
                  "provider": {"id": "factory_local", "name": getattr(provider, "name", None),
                               "base_url": str(getattr(provider, "base_url", "") or "").rstrip("/")},
                  "permissions_profile": PROFILE, "approval_policy": APPROVAL_POLICY,
                  "home": {"template_sha256": home.get("template_sha256"), "config_sha256": home.get("config_sha256"),
                           "verified": bool(home.get("verified")), "problems": home.get("problems") or []},
                  "rollout_sha256": home.get("rollout_sha256"), "exit": exit_code},
        "started": started, "ended": ended, "run_outcome": outcome, "error": error, "approvals": [],
        "artifacts": artifacts, "checks": checks, "commit": commit_ or None, "browser": [], "reconciled": reconciled,
        "result": result}


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def text_sha256(text: str | None) -> str | None:
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None


@dataclass
class Intent:
    """A Codex run in flight, as job.json records it before the run starts (so a restart can reconcile it)."""
    run: str
    started: str
    round: int | None
