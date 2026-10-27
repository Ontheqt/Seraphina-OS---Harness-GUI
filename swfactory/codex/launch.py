#!/usr/bin/python3 -I
"""sf-codex-launch: the one way the harness starts Codex (BROWSER_AND_CODEX.md §3.3, §3.11; DESIGN §44).

This file is self-contained (Python 3.10+ standard library, no import from the harness), so the owner can install it
as root (``/usr/local/libexec/seraphina/sf-codex-launch``) and one sudoers rule can let the harness's user run it,
and nothing else, as the Codex user (O3). In the test suite and without a Codex user it runs directly, through
``bin/sf-codex-launch``, as the harness's own user.

``sf-codex-launch [--settings FILE] <verb> ...``; under sudo only the root-owned ``/etc/seraphina/codex-launch.json``
is read and ``--settings`` is refused. Verbs:

- ``run --job REPO/JOB --run RUN --worktree DIR --provider URL --model ID [--deny PATH]... -- <codex arguments>``:
  checks every argument against fixed patterns (the worktree is ``<worktrees>/<repo>/<job>``, the output folder
  ``<work_root>/runs/<repo>--<job>/<run>/out``; the Codex arguments are ``exec`` with only ``--json``,
  ``--strict-config``, ``--ignore-rules``, ``--skip-git-repo-check``, ``--ephemeral``, ``-m`` (the model),
  ``-C`` (the worktree), ``--add-dir`` (the output folder), an optional ``resume <id>`` and the prompt on stdin
  (``-``): no ``-s``, no ``-c``, no ``--dangerously-…``, no profile), builds or checks the job's ``CODEX_HOME``
  from the template, writes its ``config.toml``, reads the provider's key from its key file into Codex's own
  environment only, sets the environment (PATH with /usr/bin first, so Codex uses /usr/bin/bwrap; HOME and TMPDIR
  in the run's folder; CODEX_HOME; the key) and execs the pinned binary;
- ``verify --job REPO/JOB --run RUN --worktree DIR --seconds N [--bind PATH]... -- <shell command>``: the
  repository's verify command in a minimal bubblewrap namespace (no network, the worktree read-only, a private
  HOME and /tmp, a clear environment), as the Codex user;
- ``report --job REPO/JOB``: the home's check again and the rollouts' hash, as JSON (for the run's receipt);
- ``check-home --job REPO/JOB``: the home's check alone;
- ``remove-home --job REPO/JOB``: delete the job's home (at job end, after the rollout's hash is recorded);
- ``share-worktree --job REPO/JOB --worktree DIR``: after a run, what the Codex user created in the worktree made
  the group's to read and write (a file written 0600 through a temporary name, or a folder made 0700, would keep
  the harness, a member of the group, from committing, reconciling or removing it); its own entries only, no
  symlink followed, ``.git`` left alone;
- ``version``: the pinned binary's ``--version``, run exactly as ``run`` starts it;
- ``sandbox-probe``: one sandboxed command under the job profile (``codex sandbox``, no model), as the Codex user:
  a denied canary must not be readable, ``/tmp`` must be read-only, the workspace writable and the provider key
  absent from the command's environment; it also says which bwrap Codex uses (the first on its PATH);
- ``can-read --path PATH...``: which of the paths the Codex user can read (the doctor's extended read check);
- ``key-check``: for each provider, whether its key, read exactly as ``run`` reads it, is there (the doctor's codex
  key line: names and yes or no only, never a value; the harness itself never reads the Codex route's key file);
- ``egress-probe --target HOST:PORT... [--seconds N]``: whether the Codex user's own process can connect, and how a
  connect that did not get through ended (``refused``: the loopback stack answered, so no rule dropped it;
  ``dropped``: timed out or not permitted, as the uid egress rule makes it); the rule must make every one fail.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Iterable

LAUNCHER_VERSION = 1
SERVER_SETTINGS = "/etc/seraphina/codex-launch.json"
PROFILE = "factory_worker"
PROVIDER_ID = "factory_local"
APPROVAL_POLICY = "never"

# ---------------------------------------------------------------- the per-job home
# The per-job ``CODEX_HOME`` (BROWSER_AND_CODEX.md §3.3, §3.8, §3.11 item 2; DESIGN §44).
#
# One home per job, built by the launcher from a fixed template and checked immediately before every spawn: the
# harness-written files (``config.toml`` and the launcher's own record of it, ``.sf-home.json``) must be what the
# launcher last wrote, and besides them only Codex's own state may be there (``sessions/`` with rollouts, its state
# databases, ``installation_id``, ``log/``, ``shell_snapshots/``, ``thread-writer-locks/``, ``tmp/`` and the system
# skills Codex installs itself under ``skills/.system``). No ``rules/``, ``AGENTS.md``, other ``skills/``, hooks,
# prompts, plugins, profiles, ``auth.json`` or anything else: the run refuses to start and names what it found.
# An execpolicy ``allow`` rule in ``rules/`` would make a matching command skip the sandbox, and
# ``--ignore-user-config`` does not drop rule files, so the check stays even though every run also passes
# ``--ignore-rules`` (codex-runtime.md; its runtime effect is unverified by a run).
#
# ``config.toml`` pins the local provider (a loopback ``base_url``, ``wire_api = "responses"``), turns off web search,
# the update check, analytics, feedback, memories, history and the features a bounded job has no use for, selects the
# ``factory_worker`` permissions profile (``extends = ":workspace"``, ``/tmp`` read-only, the deny-read entries) and
# sets the scrubbed shell environment policy. There is no ``sandbox_mode`` and no ``-s`` anywhere: a sandbox-mode
# override silently replaces the profile (probe P4).
#
# Verified with the pinned 0.163.0 under ``exec`` (DESIGN §44): a denied directory reads as missing inside the
# sandbox, a ``read`` entry below a denied directory does not open it again (so the deny list names every other
# job's paths one by one rather than the folders above them), and a writable root below a denied directory stays
# writable.

MANIFEST = ".sf-home.json"
CONFIG = "config.toml"
# Codex's own state at the pin (seen in its homes after exec runs): allowed as it is, never written by the harness.
STATE_FILE_RE = re.compile(r"^[a-z][a-z0-9_]*_\d+\.sqlite(?:-wal|-shm|-journal)?$")
STATE_FILES = frozenset({"installation_id", "version.json", "models_cache.json"})
STATE_DIRS = frozenset({"sessions", "log", "shell_snapshots", "thread-writer-locks", "tmp", ".tmp", "cache"})
# What Codex loads that would widen a run, named in the refusal (anything else unknown is refused too).
DANGEROUS = {"rules": "execpolicy rules (an allow rule skips the sandbox)", "AGENTS.md": "instructions",
             "AGENTS.override.md": "instructions", "hooks": "hooks", "hooks.json": "hooks", "prompts": "prompts",
             "plugins": "plugins", "auth.json": "a sign-in (no hosted model is used, O4)",
             "managed_config.toml": "managed configuration", "requirements.toml": "requirements",
             ".credentials.json": "MCP credentials", "history.jsonl": "history (persistence is none)"}
FEATURES_OFF = ("memories", "multi_agent", "plugins", "apps", "browser_use", "computer_use", "in_app_browser",
                "image_generation", "hooks", "goals", "remote_plugin", "tool_suggest", "skill_mcp_dependency_install",
                "shell_snapshot")
CORE_ENV = ("PATH", "HOME", "LANG", "TMPDIR")

HOME_TEMPLATE = """\
# Written by sf-codex-launch for one factory job (DESIGN §44). It is checked before every run: do not edit it.
model_provider = "{provider_id}"
model = {model}
web_search = "disabled"
check_for_update_on_startup = false
approval_policy = "{approval_policy}"
cli_auth_credentials_store = "ephemeral"
default_permissions = "{profile}"

[history]
persistence = "none"

[analytics]
enabled = false

[feedback]
enabled = false

[features]
{features}

[model_providers.{provider_id}]
name = "the factory's local model"
base_url = {base_url}
wire_api = "responses"
env_key = {env_key}
request_max_retries = {request_retries}
stream_max_retries = {stream_retries}

[shell_environment_policy]
inherit = "core"
include_only = [{core_env}]

[permissions.{profile}]
extends = ":workspace"

[permissions.{profile}.filesystem]
":slash_tmp" = "read"
{deny}
{projects}"""
TEMPLATE_SHA256 = hashlib.sha256(HOME_TEMPLATE.encode("utf-8")).hexdigest()
# The template's hash as pinned (§3.10): a change to the template is an explicit change, with T19 run again.
TEMPLATE_PIN = "367ac953243b86da30231dec6e672f2a1583e5bfa15c6c026780150fbd1e3165"


class HomeError(Exception):
    """The home cannot be used: the run refuses to start (the message names what was found)."""


def toml_string(value: str) -> str:
    """value as a TOML basic string."""
    out = ['"']
    for ch in str(value):
        code = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif code < 0x20 or code == 0x7F:
            out.append(f"\\u{code:04X}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def config_text(*, model: str, base_url: str, env_key: str, deny: Iterable[str], request_retries: int = 1,
                stream_retries: int = 1, untrusted: Iterable[str] = ()) -> str:
    """The job's config.toml from the template (TEMPLATE_SHA256). untrusted: the worktree and its repository's
    root, written as untrusted projects. At the pin, exec run in a git repository otherwise appends
    `[projects."<repository root>"] trust_level = "trusted"` to config.toml (seen with 0.163.0, DESIGN §44), and a
    trusted project would load the repository's own .codex/config.toml (servers, hooks, providers) on a later run;
    an entry already there is left as it is."""
    lines = [f"{toml_string(path)} = \"deny\"" for path in dict.fromkeys(str(p) for p in deny)]
    projects = "".join(f"\n[projects.{toml_string(path)}]\ntrust_level = \"untrusted\"\n"
                       for path in dict.fromkeys(str(p) for p in untrusted))
    return HOME_TEMPLATE.format(
        provider_id=PROVIDER_ID, model=toml_string(model), approval_policy=APPROVAL_POLICY, profile=PROFILE,
        features="\n".join(f"{name} = false" for name in FEATURES_OFF), base_url=toml_string(base_url),
        env_key=toml_string(env_key), request_retries=int(request_retries), stream_retries=int(stream_retries),
        core_env=", ".join(toml_string(name) for name in CORE_ENV), deny="\n".join(lines), projects=projects)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_regular(path: Path, limit: int = 1 << 20, dir_fd: int | None = None) -> bytes:
    """path's bytes (up to limit), never through a symlink. O_NONBLOCK: a FIFO or a tty planted under the name opens
    at once and is refused, rather than blocking the reader for good (a regular file ignores the flag). dir_fd:
    path is relative to that open directory."""
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0),
                 dir_fd=dir_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise HomeError(f"{path.name} is not a regular file")
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise HomeError(f"{path.name} is larger than expected")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def problems(home: Path) -> list[str]:
    """What in home is not the harness's files or Codex's own state (empty: the home passes)."""
    found: list[str] = []
    try:
        entries = sorted(os.scandir(home), key=lambda e: e.name)
    except FileNotFoundError:
        return []
    for entry in entries:
        name = entry.name
        if name in DANGEROUS:
            found.append(f"{name} ({DANGEROUS[name]})")
            continue
        if entry.is_symlink():
            found.append(f"{name} (a symlink)")
            continue
        if name in (CONFIG, MANIFEST) or name in STATE_FILES or STATE_FILE_RE.fullmatch(name):
            if not entry.is_file(follow_symlinks=False):
                found.append(f"{name} (not a regular file)")
            continue
        if name == "skills":
            found += _skills_problems(Path(entry.path))
            continue
        if name in STATE_DIRS and entry.is_dir(follow_symlinks=False):
            if name == "sessions":
                found += _sessions_problems(Path(entry.path))
            continue
        if name.endswith(".config.toml"):
            found.append(f"{name} (a profile)")
            continue
        found.append(name)
    return found


def _skills_problems(skills: Path) -> list[str]:
    """skills/ may hold only the system skills Codex installs itself (skills/.system, with its marker)."""
    out = []
    for entry in os.scandir(skills):
        if entry.name == ".system" and entry.is_dir(follow_symlinks=False):
            continue
        out.append(f"skills/{entry.name} (a skill)")
    return out


def _sessions_problems(sessions: Path) -> list[str]:
    out = []
    for root, dirs, files in os.walk(sessions):
        for name in dirs + files:
            path = Path(root) / name
            if path.is_symlink():
                out.append(f"{path.relative_to(sessions.parent)} (a symlink)")
        for name in files:
            if not name.endswith(".jsonl"):
                out.append(str((Path(root) / name).relative_to(sessions.parent)))
    return out


def prepare(home: Path, text: str) -> dict:
    """Build or check home, then write config.toml (text). Refuses (HomeError) when anything in the home is not
    what the template and Codex's own state allow, or when config.toml is not what the launcher last wrote.
    Codex's system skills are removed first: Codex writes them again from its own binary at start, so nothing
    planted among them survives. Returns the home's record: template and config hashes."""
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = os.lstat(home)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise HomeError("the Codex home is not a directory of the Codex user's own")
    os.chmod(home, 0o700)
    found = problems(home)
    if found:
        raise HomeError("the Codex home holds what the template does not allow: " + ", ".join(found[:8]))
    config, manifest = home / CONFIG, home / MANIFEST
    if config.exists() or manifest.exists():
        try:
            record = json.loads(_read_regular(manifest))
            current = sha256_bytes(_read_regular(config))
        except (OSError, ValueError, HomeError) as exc:
            raise HomeError(f"the Codex home's config.toml has no record of the launcher's own ({exc})") from None
        if not isinstance(record, dict) or record.get("config_sha256") != current:
            raise HomeError("the Codex home's config.toml changed since the launcher wrote it")
    shutil.rmtree(home / "skills" / ".system", ignore_errors=True)
    data = text.encode("utf-8")
    _write_private(config, data)
    record = {"v": 1, "template_sha256": TEMPLATE_SHA256, "config_sha256": sha256_bytes(data)}
    _write_private(manifest, json.dumps(record).encode("utf-8"))
    return {**record, "verified": True}


def _write_private(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".new")
    with contextlib.suppress(FileNotFoundError):
        os.unlink(tmp)
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def rollout_sha256(home: Path) -> str | None:
    """A hash over every rollout (sessions/**/*.jsonl: names and contents, in order), or None without one."""
    sessions = home / "sessions"
    if not sessions.is_dir():
        return None
    digest = hashlib.sha256()
    count = 0
    for path in sorted(p for p in sessions.rglob("*.jsonl") if p.is_file() and not p.is_symlink()):
        digest.update(str(path.relative_to(sessions)).encode("utf-8") + b"\0")
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 16), b""):
                digest.update(chunk)
        count += 1
    return digest.hexdigest() if count else None


def report(home: Path) -> dict:
    """What the receipt records of a home after a run: the rollouts' hash, the manifest, and the check again,
    config.toml's hash included (a config.toml Codex changed during the run is a problem, so the home is not
    verified; config_sha256_now is the hash on disk)."""
    try:
        record = json.loads(_read_regular(home / MANIFEST))
    except (OSError, ValueError, HomeError):
        record = {}
    if not isinstance(record, dict):
        record = {}
    try:
        current = sha256_bytes(_read_regular(home / CONFIG))
    except (OSError, HomeError):
        current = None
    found = problems(home)
    if record and current != record.get("config_sha256"):
        found.append(f"{CONFIG} (changed since the launcher wrote it)")
    return {"template_sha256": record.get("template_sha256"), "config_sha256": record.get("config_sha256"),
            "config_sha256_now": current, "rollout_sha256": rollout_sha256(home), "problems": found,
            "verified": not found and bool(record)}


# ---------------------------------------------------------------- the launcher's settings and the run's paths

REPO_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
JOB_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
RUN_RE = re.compile(r"^[0-9]{2,4}-[a-z0-9][a-z0-9-]{0,40}$")
THREAD_RE = re.compile(r"^[0-9a-fA-F][0-9a-fA-F-]{7,63}$")
ENV_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$")
FLAGS = frozenset({"--json", "--strict-config", "--ignore-rules", "--skip-git-repo-check", "--ephemeral"})
REQUIRED_FLAGS = ("--json", "--strict-config", "--ignore-rules")
DEFAULT_PATH = "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin"
VERIFY_ETC = ("/etc/passwd", "/etc/group", "/etc/nsswitch.conf", "/etc/localtime", "/etc/ld.so.cache",
              "/etc/alternatives", "/etc/hosts")
VERIFY_SYSTEM = ("/usr", "/bin", "/lib", "/lib64", "/lib32", "/sbin")


class Refused(Exception):
    """An argument or a setting the launcher does not accept: it exits 2 and starts nothing."""


def load_settings(path: str | None) -> dict:
    """The launcher's settings. Under sudo only the root-owned SERVER_SETTINGS (no --settings); otherwise path."""
    under_sudo = "SUDO_UID" in os.environ
    if under_sudo and path not in (None, SERVER_SETTINGS):
        raise Refused("under sudo the launcher reads only " + SERVER_SETTINGS)
    chosen = SERVER_SETTINGS if path is None else path
    try:
        info = os.lstat(chosen)
        if not stat.S_ISREG(info.st_mode):
            raise Refused(f"{chosen} is not a regular file")
        if under_sudo and (info.st_uid != 0 or info.st_mode & 0o022):
            raise Refused(f"{chosen} must be owned by root and writable by root only")
        data = json.loads(_read_regular(Path(chosen)))
    except (OSError, ValueError, HomeError) as exc:
        raise Refused(f"cannot read the launcher's settings {chosen}: {exc}") from None
    if not isinstance(data, dict) or data.get("v") != 1:
        raise Refused(f"{chosen} is not the launcher's settings (v 1)")
    for key in ("binary", "worktrees", "work_root"):
        value = data.get(key)
        if not isinstance(value, str) or not os.path.isabs(value):
            raise Refused(f"{chosen}: {key} must be an absolute path")
    providers = data.get("providers")
    if not isinstance(providers, list):
        raise Refused(f"{chosen}: providers must be a list")
    for entry in providers:
        if not isinstance(entry, dict) or not _loopback(entry.get("base_url")) \
                or not ENV_RE.fullmatch(str(entry.get("env_key") or "")):
            raise Refused(f"{chosen}: every provider needs a loopback base_url and an env_key")
    return data


def _loopback(url: object) -> bool:
    match = re.fullmatch(r"http://(127\.0\.0\.1|localhost|\[::1\]):(\d{1,5})(/[A-Za-z0-9._~/-]*)?", str(url or ""))
    return bool(match) and 0 < int(match.group(2)) < 65536


def job_parts(job: str) -> tuple[str, str]:
    repo, _, job_id = str(job).partition("/")
    if not REPO_RE.fullmatch(repo) or not JOB_RE.fullmatch(job_id):
        raise Refused(f"--job must be <repo>/<job id>, got {job!r}")
    return repo, job_id


def home_of(settings: dict, job: str) -> Path:
    repo, job_id = job_parts(job)
    return Path(settings["work_root"]) / "homes" / f"{repo}--{job_id}"


def run_dir_of(settings: dict, job: str, run: str) -> Path:
    repo, job_id = job_parts(job)
    if not RUN_RE.fullmatch(str(run)):
        raise Refused(f"--run must be a run name such as 04-builder, got {run!r}")
    return Path(settings["work_root"]) / "runs" / f"{repo}--{job_id}" / run


def _real_dir(path: Path, what: str) -> Path:
    try:
        info = os.lstat(path)
    except OSError:
        raise Refused(f"{what} {path} does not exist") from None
    if not stat.S_ISDIR(info.st_mode):
        raise Refused(f"{what} {path} is not a directory")
    real = Path(os.path.realpath(path))
    if real != Path(os.path.abspath(path)):
        raise Refused(f"{what} {path} goes through a symlink")
    return real


def worktree_of(settings: dict, job: str, given: str) -> Path:
    repo, job_id = job_parts(job)
    expected = Path(settings["worktrees"]) / repo / job_id
    if os.path.abspath(given) != str(expected):
        raise Refused(f"--worktree must be {expected}")
    return _real_dir(expected, "the worktree")


def run_folders(settings: dict, job: str, run: str) -> dict:
    """The run's out/ (build.md, a writable root), tmp/ (TMPDIR) and shell/ (HOME of Codex's commands), which the
    harness made before the run; each a real directory, none a symlink."""
    base = run_dir_of(settings, job, run)
    return {name: _real_dir(base / name, f"the run's {name} folder") for name in ("out", "tmp", "shell")}


def repository_root(worktree: Path) -> Path:
    """The repository a linked worktree belongs to (its .git file's gitdir, that git dir's commondir, the folder
    holding it), or the worktree itself for a main checkout."""
    dotgit = Path(worktree) / ".git"
    try:
        text = _read_regular(dotgit, 4096).decode("utf-8").strip()
    except (OSError, UnicodeDecodeError, HomeError):
        return Path(worktree)
    if not text.startswith("gitdir:"):
        return Path(worktree)
    gitdir = (Path(worktree) / text[len("gitdir:"):].strip()).resolve()
    try:
        common = (gitdir / _read_regular(gitdir / "commondir", 4096).decode("utf-8").strip()).resolve()
    except (OSError, UnicodeDecodeError, HomeError):
        return Path(worktree)
    return common.parent if common.name == ".git" else common


def check_codex_args(args: list[str], *, model: str, worktree: Path, out: Path) -> list[str]:
    """The Codex arguments, refused unless they are exactly the shape the codex-worker preset builds."""
    if not args or args[0] != "exec" or args[-1] != "-":
        raise Refused("the Codex arguments must be `exec ... -` (the prompt on stdin)")
    seen: set[str] = set()
    index = 1
    resumed = False
    while index < len(args) - 1:
        item = args[index]
        if item in FLAGS:
            seen.add(item)
            index += 1
            continue
        if item in ("-m", "-C", "--add-dir") and index + 1 < len(args) - 1:
            value = args[index + 1]
            want = {"-m": model, "-C": str(worktree), "--add-dir": str(out)}[item]
            if value != want:
                raise Refused(f"{item} must be {want}")
            seen.add(item)
            index += 2
            continue
        if item == "resume" and not resumed and index + 1 < len(args) - 1 and THREAD_RE.fullmatch(args[index + 1]):
            resumed = True
            index += 2
            continue
        raise Refused(f"the Codex argument {item!r} is not allowed (no -s, -c, profiles, bypass or other flags)")
    missing = [flag for flag in (*REQUIRED_FLAGS, "-m", "-C", "--add-dir") if flag not in seen]
    if missing:
        raise Refused("the Codex arguments miss " + ", ".join(missing))
    return list(args)


def provider_key(settings: dict, base_url: str) -> tuple[str, str]:
    """(env_key, the key) for base_url, which must be one of the launcher's providers. The key comes from the
    provider's key file (NAME=value lines), read as the Codex user: a key file it cannot read, or one that does not
    define the name, refuses the run (never a placeholder the model would answer 401 to). Without a key file, under
    sudo the run is refused (a key never comes from the caller's environment); otherwise from the launcher's own
    environment, or "unused" for a keyless local server."""
    for entry in settings.get("providers") or []:
        if entry.get("base_url") == base_url:
            name = str(entry["env_key"])
            key_file = entry.get("key_file")
            if key_file:
                return name, _key_from_file(str(key_file), name)
            if "SUDO_UID" in os.environ:
                raise Refused(f"the provider {base_url} has no key file, and under sudo the launcher never takes a "
                              f"key from the caller's environment: install the Codex route's key file (the setup's "
                              f"block c4)")
            return name, os.environ.get(name) or "unused"
    raise Refused(f"the provider {base_url} is not one the launcher's settings allow")


def _key_from_file(path: str, name: str) -> str:
    try:
        text = _read_regular(Path(path), 64 * 1024).decode("utf-8")
    except (OSError, UnicodeDecodeError, HomeError) as exc:
        why = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
        raise Refused(f"the key file {path} cannot be read by the Codex user (uid {os.getuid()}): {why}") from None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, sep, value = line.partition("=")
        if sep and key.strip() == name:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            if value:
                return value
    raise Refused(f"the key file {path} does not define {name}")


def deny_list(settings: dict, given: Iterable[str], home: Path) -> list[str]:
    """What the profile denies: the settings' fixed list, every provider's key file (the Codex user reads its own
    route's key, so only the profile keeps it from the commands) and the harness's list for this run (the paths that
    exist), and always the job's own auth.json (a deny entry for a missing path is accepted at the pin)."""
    out = []
    keys = [entry.get("key_file") for entry in settings.get("providers") or [] if isinstance(entry, dict)]
    for path in [*(settings.get("deny") or []), *(str(k) for k in keys if k), *given]:
        path = os.path.abspath(str(path))
        if os.path.lexists(path) and path not in out:
            out.append(path)
    return [*out, str(home / "auth.json")]


def codex_env(settings: dict, folders: dict, home: Path, env_key: str, key: str) -> dict:
    path = str(settings.get("path") or DEFAULT_PATH)
    if not path.startswith("/usr/bin"):
        raise Refused("the launcher's PATH must start with /usr/bin (Codex uses the first bwrap on PATH)")
    return {"PATH": path, "HOME": str(folders["shell"]), "LANG": "C.UTF-8", "TMPDIR": str(folders["tmp"]),
            "CODEX_HOME": str(home), env_key: key}


def binary_of(settings: dict) -> str:
    binary = str(settings["binary"])
    if not os.path.isfile(binary) or not os.access(binary, os.X_OK):
        raise Refused(f"the pinned Codex binary {binary} is not there")
    return binary


# ---------------------------------------------------------------- verbs


def do_run(settings: dict, opts: dict, codex_args: list[str]) -> int:
    job, run = opts["job"], opts["run"]
    worktree = worktree_of(settings, job, opts["worktree"])
    folders = run_folders(settings, job, run)
    model = str(opts["model"])
    if not MODEL_RE.fullmatch(model):
        raise Refused("--model is not a model id")
    base_url = str(opts["provider"])
    args = check_codex_args(codex_args, model=model, worktree=worktree, out=folders["out"])
    env_key, key = provider_key(settings, base_url)
    home = home_of(settings, job)
    retries = settings.get("retries") if isinstance(settings.get("retries"), dict) else {}
    text = config_text(model=model, base_url=base_url, env_key=env_key, deny=deny_list(settings, opts["deny"], home),
                       request_retries=int(retries.get("request_max_retries", 1)),
                       stream_retries=int(retries.get("stream_max_retries", 1)),
                       untrusted=[str(repository_root(worktree)), str(worktree)])
    try:
        (home.parent).mkdir(mode=0o700, parents=True, exist_ok=True)
        prepare(home, text)
    except HomeError as exc:
        raise Refused(str(exc)) from None
    binary = binary_of(settings)
    env = codex_env(settings, folders, home, env_key, key)
    os.chdir(worktree)
    child_guards()
    os.execve(binary, [binary, *args], env)
    return 1   # not reached


def child_guards() -> None:
    """Before exec: no new privileges (a setuid program never gives Codex more rights), and Codex is killed when
    the process that started the launcher dies (sudo, or the harness itself), so no Codex outlives the harness.
    Best effort (Linux prctl through ctypes)."""
    with contextlib.suppress(Exception):
        import ctypes
        import signal

        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(38, 1, 0, 0, 0)                        # PR_SET_NO_NEW_PRIVS
        libc.prctl(1, int(signal.SIGKILL), 0, 0, 0)       # PR_SET_PDEATHSIG


def do_verify(settings: dict, opts: dict, command: list[str]) -> int:
    job, run = opts["job"], opts["run"]
    worktree = worktree_of(settings, job, opts["worktree"])
    folders = run_folders(settings, job, run)
    if len(command) != 1 or not command[0].strip():
        raise Refused("verify takes one shell command after --")
    binds = [os.path.abspath(p) for p in [*(settings.get("verify_binds") or []), *opts["bind"]]]
    argv = verify_argv(worktree, command[0], binds)
    del folders   # checked: the run exists
    child_guards()
    os.execvp(argv[0], argv)
    return 1   # not reached


def verify_argv(worktree: Path, command: str, binds: Iterable[str] = (), home: str = "/home/verify") -> list[str]:
    """bwrap: own PID, IPC and network namespaces, the system read-only, the worktree read-only, a private HOME
    and /tmp, a clear environment, gone with its parent; then `bash -c command` in the worktree. Python's bytecode
    cache is under the private /tmp (PYTHONPYCACHEPREFIX), so no .pyc in the worktree's __pycache__ is read: one
    is valid by the source's mtime, which the commit does not hold."""
    args = ["bwrap", "--unshare-pid", "--unshare-ipc", "--unshare-net", "--die-with-parent", "--clearenv",
            "--new-session"]
    for path in VERIFY_SYSTEM:
        if os.path.islink(path):
            args += ["--symlink", os.readlink(path), path]
        elif os.path.isdir(path):
            args += ["--ro-bind", path, path]
    for path in VERIFY_ETC:
        if os.path.exists(path):
            args += ["--ro-bind", path, path]
    args += ["--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp", "--tmpfs", home]
    for path in dict.fromkeys(binds):
        if os.path.exists(path) and not any(path == s or path.startswith(s + "/") for s in VERIFY_SYSTEM):
            args += ["--ro-bind", path, path]
    args += ["--ro-bind", str(worktree), str(worktree)]
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": home, "TMPDIR": "/tmp",
           "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPYCACHEPREFIX": "/tmp/pycache", "CI": "1"}
    for key, value in env.items():
        args += ["--setenv", key, value]
    return args + ["--chdir", str(worktree), "bash", "-c", command]


def do_report(settings: dict, opts: dict) -> int:
    print(json.dumps(report(home_of(settings, opts["job"]))))
    return 0


def do_check_home(settings: dict, opts: dict) -> int:
    found = report(home_of(settings, opts["job"]))["problems"]
    print(json.dumps({"problems": found, "ok": not found}))
    return 0 if not found else 3


def do_share_worktree(settings: dict, opts: dict) -> int:
    """Walked with an explicit stack, not os.walk (recursive before Python 3.12): a tree Codex made deeper than the
    recursion limit is shared like any other."""
    worktree = worktree_of(settings, opts["job"], opts["worktree"])
    uid = os.getuid()
    changed = 0
    stack = [str(worktree)]
    while stack:
        top = stack.pop()
        try:
            with os.scandir(top) as listing:
                names = [entry.name for entry in listing]
        except OSError:
            continue
        for name in names:
            if top == str(worktree) and name == ".git":
                continue
            path = os.path.join(top, name)
            try:
                info = os.lstat(path)
            except OSError:
                continue
            if stat.S_ISLNK(info.st_mode):
                continue
            if info.st_uid == uid:
                mode = stat.S_IMODE(info.st_mode)
                wanted = mode | 0o060 | (0o010 if stat.S_ISDIR(info.st_mode) or mode & 0o100 else 0)
                if wanted != mode:
                    with contextlib.suppress(OSError):
                        os.chmod(path, wanted)
                        changed += 1
            if stat.S_ISDIR(info.st_mode):
                stack.append(path)
    print(json.dumps({"shared": changed}))
    return 0


def do_remove_home(settings: dict, opts: dict) -> int:
    home = home_of(settings, opts["job"])
    if os.path.islink(home):
        raise Refused("the home is a symlink")
    shutil.rmtree(home, ignore_errors=True)
    print(json.dumps({"removed": not os.path.lexists(home)}))
    return 0


def do_version(settings: dict) -> int:
    binary = binary_of(settings)
    done = subprocess.run([binary, "--version"], stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
                          env={"PATH": str(settings.get("path") or DEFAULT_PATH), "LANG": "C.UTF-8",
                               "HOME": "/nonexistent", "CODEX_HOME": "/nonexistent"})
    sys.stdout.write((done.stdout or b"").decode("utf-8", "replace"))
    return done.returncode


def do_sandbox_probe(settings: dict) -> int:
    """§3.11 item 6: one sandboxed probe as the Codex user before relying on the sandbox."""
    root = Path(settings["work_root"]) / "homes" / "_probe"
    shutil.rmtree(root, ignore_errors=True)
    for name in ("home", "ws", "tmp"):
        (root / name).mkdir(mode=0o700, parents=True, exist_ok=True)
    canary = root / "canary.txt"
    canary.write_text("canary\n", encoding="utf-8")
    text = config_text(model="probe", base_url="http://127.0.0.1:9/v1", env_key="SF_PROBE_KEY", deny=[str(canary)])
    (root / "home" / CONFIG).write_text(text, encoding="utf-8")
    script = (f"cat {canary} >/dev/null 2>&1 && echo canary-read; echo x > /tmp/sf-probe-$$ 2>/dev/null && "
              f"echo tmp-write; echo x > {root / 'ws' / 'w'} && echo ws-write; env | grep -q SF_PROBE_KEY && "
              f"echo key-seen; echo done")
    path = str(settings.get("path") or DEFAULT_PATH)
    env = {"PATH": path, "HOME": str(root / "tmp"), "LANG": "C.UTF-8", "TMPDIR": str(root / "tmp"),
           "CODEX_HOME": str(root / "home"), "SF_PROBE_KEY": "sf-probe-secret"}
    bwrap = shutil.which("bwrap", path=path)
    try:
        done = subprocess.run([binary_of(settings), "sandbox", "--", "/bin/sh", "-c", script], cwd=str(root / "ws"),
                              env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
        out = (done.stdout or b"").decode("utf-8", "replace").split()
        code = done.returncode
    except (OSError, subprocess.SubprocessError) as exc:
        out, code = [f"error: {exc}"], None
    finally:
        shutil.rmtree(root, ignore_errors=True)
    result = {"bwrap": bwrap, "ran": "done" in out, "canary_read": "canary-read" in out,
              "tmp_write": "tmp-write" in out, "ws_write": "ws-write" in out, "key_seen": "key-seen" in out,
              "exit": code}
    result["ok"] = (result["ran"] and result["ws_write"] and not result["canary_read"] and not result["tmp_write"]
                    and not result["key_seen"])
    print(json.dumps(result))
    return 0


def do_can_read(paths: list[str]) -> int:
    print(json.dumps({"readable": {path: os.access(path, os.R_OK) for path in paths}, "uid": os.getuid()}))
    return 0


def do_key_check(settings: dict) -> int:
    """For each provider of the settings: whether a run would get its key (provider_key, as the Codex user), and
    why not. Prints the key's name and yes or no only, never a value."""
    found = {}
    for entry in settings.get("providers") or []:
        base_url = str(entry.get("base_url") or "")
        answer: dict = {"env_key": str(entry.get("env_key") or ""), "key_file": entry.get("key_file")}
        try:
            provider_key(settings, base_url)
            answer["defined"] = True
        except Refused as exc:
            answer.update(defined=False, reason=str(exc))
        found.setdefault(base_url, answer)
    print(json.dumps({"providers": found, "uid": os.getuid()}))
    return 0


def do_egress_probe(targets: list[str], seconds: object = None) -> int:
    import errno
    import socket

    try:
        timeout = min(10.0, max(0.2, float(seconds))) if seconds is not None else 3.0
    except ValueError:
        raise Refused("--seconds must be a number") from None
    found, how = {}, {}
    for target in targets:
        host, _, port = target.rpartition(":")
        try:
            with socket.create_connection((host.strip("[]"), int(port)), timeout=timeout):
                found[target], how[target] = True, "connected"
        except ConnectionRefusedError:
            found[target], how[target] = False, "refused"
        except (TimeoutError, PermissionError):
            found[target], how[target] = False, "dropped"
        except OSError as exc:
            found[target] = False
            how[target] = "dropped" if exc.errno in (errno.ETIMEDOUT, errno.EPERM, errno.EACCES) else "unreachable"
        except ValueError:
            found[target], how[target] = False, "unreachable"
    print(json.dumps({"connected": found, "how": how, "uid": os.getuid()}))
    return 0


def parse(argv: list[str]) -> tuple[str | None, str, dict, list[str]]:
    """(settings path, verb, options, the arguments after --)."""
    settings_path = None
    if argv[:1] == ["--settings"] and len(argv) >= 2:
        settings_path, argv = argv[1], argv[2:]
    if not argv:
        raise Refused("usage: sf-codex-launch [--settings FILE] run|verify|report|check-home|remove-home|"
                      "share-worktree|version|sandbox-probe|can-read|key-check|egress-probe ...")
    verb, rest = argv[0], argv[1:]
    tail: list[str] = []
    if "--" in rest:
        at = rest.index("--")
        rest, tail = rest[:at], rest[at + 1:]
    opts: dict = {"deny": [], "bind": [], "path": [], "target": []}
    single = {"--job": "job", "--run": "run", "--worktree": "worktree", "--provider": "provider", "--model": "model",
              "--seconds": "seconds"}
    index = 0
    while index < len(rest):
        item = rest[index]
        if item in single and index + 1 < len(rest):
            opts[single[item]] = rest[index + 1]
        elif item in ("--deny", "--bind", "--path", "--target") and index + 1 < len(rest):
            opts[item[2:]].append(rest[index + 1])
        else:
            raise Refused(f"unknown argument {item!r}")
        index += 2
    needs = {"run": ("job", "run", "worktree", "provider", "model"), "verify": ("job", "run", "worktree"),
             "report": ("job",), "check-home": ("job",), "remove-home": ("job",),
             "share-worktree": ("job", "worktree"), "version": (),
             "sandbox-probe": (), "can-read": (), "key-check": (), "egress-probe": ()}
    if verb not in needs:
        raise Refused(f"unknown verb {verb!r}")
    missing = [f"--{name}" for name in needs[verb] if name not in opts]
    if missing:
        raise Refused(f"{verb} needs " + ", ".join(missing))
    return settings_path, verb, opts, tail


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        settings_path, verb, opts, tail = parse(argv)
        settings = load_settings(settings_path)
        if verb == "run":
            return do_run(settings, opts, tail)
        if verb == "verify":
            return do_verify(settings, opts, tail)
        if verb == "report":
            return do_report(settings, opts)
        if verb == "check-home":
            return do_check_home(settings, opts)
        if verb == "remove-home":
            return do_remove_home(settings, opts)
        if verb == "share-worktree":
            return do_share_worktree(settings, opts)
        if verb == "sandbox-probe":
            return do_sandbox_probe(settings)
        if verb == "can-read":
            return do_can_read(opts["path"])
        if verb == "key-check":
            return do_key_check(settings)
        if verb == "egress-probe":
            return do_egress_probe(opts["target"], opts.get("seconds"))
        return do_version(settings)
    except Refused as exc:
        sys.stderr.write(f"sf-codex-launch: refused: {exc}\n")
        return 2
    except OSError as exc:
        sys.stderr.write(f"sf-codex-launch: cannot start: {exc}\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
