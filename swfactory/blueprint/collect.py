"""The read-only snapshot collector: the first Blueprint capture of an Ubuntu server (DESIGN.md §28).

collect(runner, sudo=False) inventories the host and returns the snapshot (Capture format v0):
schema_version, host, collected_at, scope and redaction; then the raw sections (host_baseline,
gpu, storage, network, packages, systemd, cron, logging, docker, compose_files, cockpit, cosmos,
tailscale, backups), each a map of probe name -> {command, exit_code, stdout, stderr, seconds,
status, ...}; then the normalised indexes (systemd_units, host_packages, containers, compose_files,
endpoints, relationships), scope_limits, redaction_review and summary. write_snapshot() stores it as
snapshot.json beside a short overview.md. `factory snapshot --out DIR [--sudo]` runs both.

Safety is enforced here, not left to the probe list: every argv goes through check_argv() before
anything runs, and check_argv() accepts only a fixed allowlist of read-only command shapes (per
program, per subcommand, per flag, per path). Anything else raises UnsafeCommand and never reaches
the runner. Never run, whatever the probe list says: lxc (on Ubuntu it can start the on-demand LXD
installer), package managers in any installing mode, docker start/exec/run/pull/logs, anything
that writes, starts or stops. A program that is not on PATH is recorded as missing_command and is
never executed, so nothing gets a chance to offer an install. Every command has its own timeout,
runs with stdin closed, cwd /, a minimal environment (no API keys) and its own process group.

What is never read: application data, databases, volumes, log message bodies, env files, and the
contents of files under home directories (paths there are listed, not opened). Compose files
elsewhere are read and redacted (redact.py: every environment value, every secret-looking key).
A path a search, a listing or Docker named that the allowlist does not accept is recorded as
skipped (listed, not read); only the fixed probe list raises UnsafeCommand. Before a file is
printed its symlinks are followed to the end (as root when root reads it and this user cannot
see the way); a file found by a search or a listing must lead to a file a probe may read, a fixed
config file anywhere but a never-read place, and a path that cannot be followed is not read. The
check and the read are two steps, so a link swapped in between them is followed (cat cannot refuse
links): that needs write access to a directory on the path, in that moment.
"""

from __future__ import annotations

import ipaddress
import json
import os
import pwd
import re
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from .. import __version__
from ..events import env_secret_values, utc_now
from .redact import MARK, Redactor

SCHEMA_VERSION = "0"
SEARCH_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin"
SUDO = ["sudo", "-n", "--"]
OUTPUT_BYTES = 4 << 20          # kept per stream; the rest is cut and the probe marked truncated
DEFAULT_TIMEOUT = 15.0
CHUNK = 40                      # ids or units per docker inspect / systemctl show call
MAX_COMPOSE_FILES = 100
MAX_LISTED_FILES = 200          # files read out of one config directory (cron.d, sources.list.d)
SECTIONS = ("host_baseline", "gpu", "storage", "network", "packages", "systemd", "cron", "logging", "docker",
            "compose_files", "cockpit", "cosmos", "tailscale", "backups")
INDEXES = ("systemd_units", "host_packages", "containers", "compose_files", "endpoints", "relationships")
HOME_ROOTS = ("/home/", "/root/")
DOCKER_DATA = "/var/lib/docker/"
FIND_ROOTS = ("/opt", "/srv", "/etc")
FIND_DEPTH = 4
COMPOSE_PATTERNS = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml",
                    "docker-compose.*.yml", "docker-compose.*.yaml", "compose.*.yml", "compose.*.yaml")
COMPOSE_NAME_RE = re.compile(r"^(?:docker-)?compose(?:\.[A-Za-z0-9_-]+)*\.ya?ml$")
CONFIG_NAME_RE = re.compile(r"^[A-Za-z0-9_@+-][A-Za-z0-9_.@+-]*$")
UNIT_RE = re.compile(r"^[A-Za-z0-9@:_\\-][A-Za-z0-9@:._\\-]*\.(service|socket|timer|target|mount|path|slice|scope|"
                     r"automount|swap|device)$")
CONTAINER_REF_RE = re.compile(r"^(?:[0-9a-f]{12,64}|[A-Za-z0-9][A-Za-z0-9_.-]{0,127})$")
DEVICE_RE = re.compile(r"^/dev/[A-Za-z0-9/_-]{1,64}$")
SMART_TYPE_RE = re.compile(r"^[A-Za-z0-9,_+-]{1,32}$")
COLUMNS_RE = re.compile(r"^[A-Za-z0-9_,.:-]{1,300}$")
DENIED_RE = re.compile(r"permission denied|operation not permitted|must be root|need to be root|are you root|"
                       r"a password is required|access denied|not allowed|got permission denied", re.IGNORECASE)
# sudo's own refusal (a password or interactive authentication wanted, no-new-privileges, not in sudoers): classic
# sudo and sudo-rs both start it "sudo: " (sudo-rs 0.2.13 as /usr/bin/sudo); an older sudo-rs may name itself.
SUDO_REFUSED_RE = re.compile(r"^sudo(?:-rs)?: |is not in the sudoers file|may not run sudo", re.MULTILINE)
SHADOW_RE = re.compile(r"^g?shadow(?:-|~|\.[A-Za-z0-9-]+)?$")   # shadow, gshadow-, shadow.bak; not shadowsocks
DPKG_FORMAT = r"-f=${Package}\t${Version}\t${db:Status-Abbrev}\n"   # dpkg-query expands \t and \n itself
BACKUP_TOOLS = ("restic", "borg", "kopia", "snapraid", "timeshift", "rsnapshot", "duplicati", "rclone", "backup")

READ_FILES = frozenset({
    "/etc/os-release", "/etc/fstab", "/etc/hosts", "/etc/resolv.conf", "/etc/crontab", "/etc/apt/sources.list",
    "/etc/apt/apt.conf.d/20auto-upgrades", "/etc/apt/apt.conf.d/50unattended-upgrades",
    "/etc/systemd/journald.conf", "/etc/docker/daemon.json", "/etc/cockpit/cockpit.conf",
    "/var/lib/cosmos/cosmos.config.json", "/etc/snapraid.conf", "/etc/ufw/ufw.conf", "/etc/ufw/user.rules",
    "/etc/ufw/user6.rules"})
READ_DIRS = ("/etc/cron.d", "/etc/apt/sources.list.d", "/var/spool/cron/crontabs")   # their plain files
LIST_DIRS = frozenset({
    "/etc/cron.d", "/etc/cron.hourly", "/etc/cron.daily", "/etc/cron.weekly", "/etc/cron.monthly",
    "/var/spool/cron/crontabs", "/etc/apt/sources.list.d", "/etc/systemd/journald.conf.d", "/etc/logrotate.d",
    "/var/lib/cosmos", "/etc/cockpit"})
NEVER = {
    "lxc": "lxc is never run: on Ubuntu it can start the on-demand LXD installer",
    "lxd": "lxd is never run: on Ubuntu it can start the on-demand LXD installer",
    "apt": "package managers are never run", "apt-get": "package managers are never run",
    "aptitude": "package managers are never run", "dpkg": "dpkg is never run (dpkg-query reads the database)",
    "pip": "package managers are never run", "pip3": "package managers are never run",
    "npm": "package managers are never run", "snapd": "package managers are never run",
}


class UnsafeCommand(Exception):
    """A command outside the read-only allowlist. It is never run."""


# ---------------------------------------------------------------- the allowlist


def check_argv(argv: list[str]) -> None:
    """Accept argv only when it is one of the read-only command shapes below; UnsafeCommand otherwise.

    `sudo -n -- <argv>` is accepted when <argv> is. Programs are bare names (resolved on SEARCH_PATH),
    so no probe can run a file of its choosing.
    """
    if not isinstance(argv, (list, tuple)) or not argv or not all(isinstance(a, str) for a in argv):
        raise UnsafeCommand(f"not an argv list: {argv!r}")
    if any("\0" in a or "\n" in a for a in argv):
        raise UnsafeCommand(f"control characters in {argv!r}")
    argv = list(argv)
    if argv[0] == "sudo":
        if argv[:3] != SUDO or len(argv) < 4 or argv[3] == "sudo":
            raise UnsafeCommand(f"sudo only as `sudo -n -- <allowed command>`: {argv!r}")
        argv = argv[3:]
    program, args = argv[0], argv[1:]
    if program in NEVER:
        raise UnsafeCommand(f"{NEVER[program]}: {argv!r}")
    rule = RULES.get(program)
    if rule is None or "/" in program:
        raise UnsafeCommand(f"{program!r} is not in the read-only allowlist: {argv!r}")
    if not rule(args):
        raise UnsafeCommand(f"{program} is allowed only in its read-only forms: {argv!r}")


def readable_path(path: str) -> bool:
    """A file a probe may print: a fixed config file, a plain file directly in a READ_DIRS directory, or a
    compose file outside home directories and Docker's data. Never a dot file, a key or an env file."""
    if not _plain_path(path) or _never_read(path) or path.startswith(("/proc/", "/sys/", "/dev/")):
        return False
    name = path.rsplit("/", 1)[-1]
    if path in READ_FILES:
        return True
    parent = path.rsplit("/", 1)[0]
    if parent in READ_DIRS:
        return bool(CONFIG_NAME_RE.match(name))
    return bool(COMPOSE_NAME_RE.match(name))


def _never_read(path: str) -> bool:
    """A resolved path no probe reads, whatever the name that led there: home directories, Docker's
    data, dot files, keys, env files and the password databases (shadow, gshadow and their backups)."""
    name = path.rsplit("/", 1)[-1]
    return (path.startswith((*HOME_ROOTS, DOCKER_DATA)) or name.startswith(".")
            or name.endswith((".key", ".pem", ".env")) or SHADOW_RE.match(name) is not None)


def _unread_reason(path: str) -> str:
    """Why readable_path refuses path, for a file a search, a listing or Docker named."""
    if not _plain_path(path):
        return "not a plain absolute path: listed, not read"
    if path.startswith(HOME_ROOTS):
        return "under a home directory: listed, not read"
    if path.startswith(DOCKER_DATA):
        return "inside Docker's data directory (application data): listed, not read"
    if path.startswith(("/proc/", "/sys/", "/dev/")):
        return "under /proc, /sys or /dev: listed, not read"
    if _never_read(path):
        return "a dot file, key, env file or password database by its name: listed, not read"
    return "not a file a probe reads (a fixed config file, a file in a read directory or a compose file): " \
           "listed, not read"


def _target_refusal(path: str, target: str | None) -> str | None:
    """Why `cat path` must not run, now that path leads to target (None: it may). A fixed config file
    may lead anywhere but a never-read place (on Ubuntu /etc/os-release and /etc/resolv.conf are
    links); a file a search or a listing found must lead to a file a probe may read."""
    if target is None:
        return f"{path} could not be followed to its end as root, so where it leads is unknown: not read"
    if target == path:
        return None
    if path in READ_FILES:
        allowed = _plain_path(target) and not _never_read(target) and not target.startswith(("/proc/", "/sys/",
                                                                                              "/dev/"))
    else:
        allowed = readable_path(target)
    return None if allowed else f"{path} resolves to {target}, which is never read: listed, not read"


def _plain_path(path: str) -> bool:
    return (isinstance(path, str) and path.startswith("/") and os.path.normpath(path) == path and path != "/"
            and "//" not in path)


def _exact(*forms: tuple[str, ...]) -> Callable[[list[str]], bool]:
    allowed = {tuple(form) for form in forms}
    return lambda args: tuple(args) in allowed


def _cat(args: list[str]) -> bool:
    return len(args) == 1 and readable_path(args[0])


def _ls(args: list[str]) -> bool:
    flags = [a for a in args if a.startswith("-")]
    paths = [a for a in args if not a.startswith("-")]
    return (len(flags) == 1 and flags[0] in ("-la", "-1A") and bool(paths)
            and all(p in LIST_DIRS for p in paths))


def _find(args: list[str]) -> bool:
    """find <roots> -xdev -maxdepth <n> -type f ( -name <compose pattern> [-o -name …] ) -print, nothing else."""
    try:
        start = args.index("-xdev")
    except ValueError:
        return False
    roots, rest = args[:start], args[start:]
    if not roots or not all(root in FIND_ROOTS for root in roots):
        return False
    if rest[:5] != ["-xdev", "-maxdepth", rest[2] if len(rest) > 2 else "", "-type", "f"]:
        return False
    if not rest[2].isdigit() or not 1 <= int(rest[2]) <= FIND_DEPTH:
        return False
    names = rest[5:]
    if len(names) < 5 or names[0] != "(" or names[-2:] != [")", "-print"]:
        return False
    terms = names[1:-2]
    for index, term in enumerate(terms):
        position = index % 3
        if position == 0 and term != "-name":
            return False
        if position == 1 and term not in COMPOSE_PATTERNS:
            return False
        if position == 2 and term != "-o":
            return False
    return len(terms) % 3 == 2


def _systemctl(args: list[str]) -> bool:
    if not args:
        return False
    verb, rest = args[0], args[1:]
    options = {"--all", "--no-legend", "--no-pager", "--plain", "--full"}
    if verb in ("list-units", "list-unit-files", "list-timers", "list-sockets"):
        return all(a in options or re.fullmatch(r"--(type|state)=[a-z,-]+", a) for a in rest)
    if verb == "is-system-running":
        return not rest
    if verb == "show":
        props = [a for a in rest if a.startswith("--property=")]
        units = [a for a in rest if not a.startswith("-")]
        others = [a for a in rest if a.startswith("-") and not a.startswith("--property=")]
        # Always with --property: a bare `show` would print Environment= and the like.
        return (len(props) == 1 and re.fullmatch(r"--property=[A-Za-z,]+", props[0]) is not None
                and bool(units) and all(UNIT_RE.match(u) for u in units) and all(o == "--no-pager" for o in others))
    return False


def _docker(args: list[str]) -> bool:
    fmt = ["--format", "{{json .}}"]
    if args in (["version", *fmt], ["info", *fmt], ["ps", "-a", "--no-trunc", *fmt], ["volume", "ls", *fmt],
                ["image", "ls", "--no-trunc", *fmt], ["network", "ls", "--no-trunc", *fmt],
                ["compose", "ls", "--all", "--format", "json"]):
        return True
    if args[:1] == ["inspect"] or args[:2] == ["network", "inspect"]:
        refs = args[1:] if args[0] == "inspect" else args[2:]
        return bool(refs) and all(CONTAINER_REF_RE.match(ref) for ref in refs)
    return False


def _nvidia(args: list[str]) -> bool:
    if args in (["-L"], ["topo", "-m"], ["nvlink", "--status"]):
        return True
    return (len(args) == 2 and re.fullmatch(r"--query-(gpu|compute-apps)=[a-z_.,]+", args[0]) is not None
            and args[1] in ("--format=csv,noheader", "--format=csv,noheader,nounits"))


def _smartctl(args: list[str]) -> bool:
    if args == ["--scan", "-j"]:
        return True
    if args[:6] != ["-j", "-n", "standby", "-i", "-H", "-A"]:
        return False
    rest = args[6:]
    if len(rest) == 3 and rest[0] == "-d" and SMART_TYPE_RE.match(rest[1]):
        rest = rest[2:]
    return len(rest) == 1 and bool(DEVICE_RE.match(rest[0]))


def _columns(flags: set[str]) -> Callable[[list[str]], bool]:
    def check(args: list[str]) -> bool:
        for index, arg in enumerate(args):
            if arg in flags or arg == "-o":
                continue
            if index and args[index - 1] == "-o" and COLUMNS_RE.match(arg):
                continue
            return False
        return True
    return check


def _ip(args: list[str]) -> bool:
    family = [a for a in args if a in ("-4", "-6")]
    rest = [a for a in args if a not in ("-4", "-6")]
    return len(family) <= 1 and len(rest) == 3 and rest[0] == "-j" and rest[1] in ("addr", "route", "link", "rule") \
        and rest[2] == "show"


RULES: dict[str, Callable[[list[str]], bool]] = {
    "hostname": _exact(()),
    "uname": lambda args: all(re.fullmatch(r"-[asnrvmpio]+", a) for a in args),
    "uptime": _exact((), ("-s",), ("-p",)),
    "lscpu": _exact((), ("-J",)),
    "free": _exact(("-b",)),
    "systemd-detect-virt": _exact(()),
    "timedatectl": _exact(("show",), ("status",)),
    "lspci": _exact((), ("-nn",)),
    "lsblk": _columns({"-J", "-b"}),
    "findmnt": _columns({"-J"}),
    "df": _exact(("-P", "-B1", "-T"), ("-P", "-i")),
    "swapon": _exact(("--show", "--bytes")),
    "zpool": _exact(("list", "-H", "-o", "name,size,alloc,free,health"), ("status",)),
    "zfs": _exact(("list", "-H", "-o", "name,used,avail,mountpoint")),
    "smartctl": _smartctl,
    "nvidia-smi": _nvidia,
    "cat": _cat,
    "realpath": lambda args: len(args) == 3 and args[:2] == ["-e", "--"] and _plain_path(args[2]),
    "ls": _ls,
    "find": _find,
    "ip": _ip,
    "ss": _exact(("-H", "-l", "-n", "-t", "-u", "-p")),
    "resolvectl": _exact(("status",)),
    "nft": _exact(("list", "ruleset")),
    "netplan": _exact(("get",)),
    "dpkg-query": _exact(("-W", DPKG_FORMAT)),
    "apt-mark": _exact(("showhold",)),
    "snap": _exact(("list",)),
    "systemctl": _systemctl,
    "journalctl": lambda args: bool(args) and all(a in ("--disk-usage", "--list-boots", "--no-pager") for a in args),
    "crontab": _exact(("-l",)),
    "docker": _docker,
    "tailscale": _exact(("version",), ("status", "--json"), ("serve", "status", "--json"),
                        ("funnel", "status", "--json")),
    "restic": _exact(("version",)),
    "borg": _exact(("--version",)),
    "kopia": _exact(("--version",)),
    "snapraid": _exact(("--version",)),
    "timeshift": _exact(("--version",)),
}


# ---------------------------------------------------------------- running a command


@dataclass
class Result:
    exit_code: int | None
    stdout: str
    stderr: str
    seconds: float
    timed_out: bool = False
    truncated: bool = False
    error: str | None = None


class SubprocessRunner:
    """Runs one argv with a timeout: no shell, stdin closed, cwd /, a minimal environment (no secrets),
    its own process group (killed whole on timeout); output read through pipes (no temporary files),
    decoded as UTF-8 and kept up to OUTPUT_BYTES per stream."""

    def which(self, program: str) -> bool:
        return shutil.which(program, path=SEARCH_PATH) is not None

    def run(self, argv: list[str], timeout: float) -> Result:
        check_argv(argv)   # the runner refuses too, so nothing else can hand it a command
        started = time.monotonic()
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    cwd="/", env=_environment(), start_new_session=True)
        except OSError as exc:
            return Result(None, "", "", time.monotonic() - started, error=f"{type(exc).__name__}: {exc}")
        timed_out = False
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(proc)
            try:
                out, err = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:   # a grandchild kept the pipes open
                out, err = b"", b""
        seconds = time.monotonic() - started
        stdout, cut_out = _decode(out or b"")
        stderr, cut_err = _decode(err or b"")
        return Result(None if timed_out else proc.returncode, stdout, stderr, seconds, timed_out=timed_out,
                      truncated=cut_out or cut_err)


def _environment() -> dict[str, str]:
    env = {"PATH": SEARCH_PATH, "LC_ALL": "C", "LANG": "C", "TERM": "dumb", "NO_COLOR": "1",
           "SYSTEMD_PAGER": "", "SYSTEMD_COLORS": "0", "PAGER": "cat"}
    for name in ("HOME", "USER", "LOGNAME", "DOCKER_HOST", "DOCKER_CONTEXT", "XDG_RUNTIME_DIR"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    return env


def _follow(path: str) -> str:
    """path with every symlink followed to the end, as this user (OSError when that cannot be done)."""
    return os.path.realpath(path, strict=True)


def systemd_booted() -> bool:
    """Whether systemd is this host's init (sd_booted(): /run/systemd/system exists), so a socket unit
    could start a daemon on first contact."""
    return os.path.isdir("/run/systemd/system")


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _decode(data: bytes) -> tuple[str, bool]:
    return data[:OUTPUT_BYTES].decode("utf-8", "replace"), len(data) > OUTPUT_BYTES


# ---------------------------------------------------------------- the collector


class Collector:
    """One capture: runs the probes through check_argv and the runner, redacts, then normalises."""

    def __init__(self, runner: Any, *, sudo: bool = False, known_secrets: Iterable[str] = (), helper: Any = None) -> None:
        self.runner = runner
        self.sudo = sudo
        self.helper = helper   # helper mode (YOUR_SERVER.md §11.9): sf-system's read verbs replace the root probes
        self.sudo_missing = sudo and not runner.which("sudo")   # root-only probes then run as this user
        self.systemd_booted = systemd_booted()
        self.redactor = Redactor(known_secrets)
        self.raw: dict[str, dict[str, dict]] = {section: {} for section in SECTIONS}
        self.parsed: dict[tuple[str, str], Any] = {}
        self.withheld: list[str] = []
        self.sudo_refused: list[str] = []
        self.compose_found: dict[str, list[str]] = {}

    # -- probes ----------------------------------------------------------------------

    def probe(self, section: str, name: str, argv: list[str], *, timeout: float = DEFAULT_TIMEOUT,
              root: bool = False, kind: str = "text") -> dict:
        """Run argv (as `sudo -n --` when root and the capture runs with sudo), store the redacted record
        under raw[section][name] and return it. kind: text, json, jsonl (one JSON value per line), yaml
        or compose. UnsafeCommand if argv is not allowlisted (nothing runs then). A cat whose path leads
        somewhere it may not (_target_refusal) is recorded as skipped and never runs."""
        command = self._command(argv, root)
        check_argv(command)
        if argv[0] == "cat":
            target = self._resolve(argv[1], as_root=command[0] == "sudo", probe=f"{section}.{name}")
            command = self._command(argv, root)   # sudo may have refused while resolving
            refusal = _target_refusal(argv[1], target)
            if refusal:
                return self.skip(section, name, command, refusal)
        missing = next((p for p in (argv[0], command[0]) if not self.runner.which(p)), None)
        if missing:
            record = {"command": command, "exit_code": None, "stdout": "", "stderr": "", "seconds": 0.0,
                      "status": "missing_command", "error": f"{missing}: command not found (not installed here)"}
            self.raw[section][name] = record
            return record
        result = self.runner.run(command, timeout)
        if command[0] == "sudo" and result.exit_code == 1 and SUDO_REFUSED_RE.search(result.stderr or ""):
            # sudo itself said no (it wants a password, or cannot raise rights here): this probe and every later
            # one run as this user.
            self.sudo_refused.append(f"{section}.{name}")
            note = (result.stderr or "").strip().splitlines()[0][:200]
            command = list(argv)
            result = self.runner.run(command, timeout)
            result.error = f"sudo refused ({note}); ran without sudo" + (f"; {result.error}" if result.error else "")
        record = self._record(section, name, command, result, kind)
        self.raw[section][name] = record
        return record

    def _command(self, argv: list[str], root: bool) -> list[str]:
        """argv as it runs: through `sudo -n --` for a root probe of a --sudo capture, unless sudo is
        not installed or has refused."""
        via_sudo = root and self.sudo and not self.sudo_missing and not self.sudo_refused
        return [*SUDO, *argv] if via_sudo else list(argv)

    def _resolve(self, path: str, as_root: bool, probe: str) -> str | None:
        """path with every symlink followed to the end, or None when root reads it and the way cannot be
        followed. Nothing there (for anyone: sudo sees the same tree) gives path back, and cat fails the
        same way. When this user cannot follow it (a directory on the way it may not search), a read as
        this user meets the same wall, and a read as root asks root (`sudo -n -- realpath -e`)."""
        try:
            return _follow(path)
        except (FileNotFoundError, NotADirectoryError):
            return path
        except OSError:
            if not as_root:
                return path
        if not self.runner.which("realpath"):
            return None
        result = self.runner.run([*SUDO, "realpath", "-e", "--", path], DEFAULT_TIMEOUT)
        if result.exit_code == 1 and SUDO_REFUSED_RE.search(result.stderr or ""):
            self.sudo_refused.append(probe)   # this read and every later one run as this user, who meets the wall
            return path
        resolved = (result.stdout or "").strip()
        return resolved if result.exit_code == 0 and _plain_path(resolved) else None

    def helper_probe(self, section: str, name: str, verb: str, **fields: Any) -> dict:
        """One read verb of Seraphina's system access (sf-system, through `sudo -n` and its one sudoers line), stored
        like a probe: the answer as JSON, redacted by structure. A refusal is recorded with the helper's own words
        (denied when its settings or the caller do not allow it, error otherwise), never raised."""
        command = ["sf-system", verb, *(f"{key}={value}" for key, value in fields.items())]
        started = time.monotonic()
        try:
            answer = self.helper.call(verb, **fields)
        except Exception as exc:  # noqa: BLE001 - a helper that refuses or is not there is this probe's record
            code = str(getattr(exc, "code", type(exc).__name__))
            record = {"command": command, "exit_code": None, "stdout": "", "stderr": "",
                      "seconds": round(time.monotonic() - started, 3),
                      "status": "denied" if code in ("not_allowed", "caller_is_login_account", "caller_not_allowed",
                                                     "caller_holds_root", "docker_api_off", "sudo_refused")
                      else "error", "error": self.redactor.text(f"{code}: {getattr(exc, 'words', '')}".strip())}
            self.raw.setdefault(section, {})[name] = record
            return record
        result = Result(0, json.dumps(answer, ensure_ascii=False), "", time.monotonic() - started)
        record = self._record(section, name, command, result, "json")
        self.raw.setdefault(section, {})[name] = record
        return record

    def skip(self, section: str, name: str, command: list[str], reason: str) -> dict:
        record = {"command": command, "exit_code": None, "stdout": "", "stderr": "", "seconds": 0.0,
                  "status": "skipped_by_policy", "error": reason}
        self.raw[section][name] = record
        return record

    def _record(self, section: str, name: str, command: list[str], result: Result, kind: str) -> dict:
        before = self.redactor.total
        stdout, parsed = self._redact_stdout(f"{section}.{name}", result.stdout or "", kind,
                                             cut=result.truncated)
        stderr = self.redactor.text(result.stderr or "")
        if result.timed_out:
            status = "timeout"
        elif result.exit_code is None:
            status = "error"
        elif result.exit_code == 0:
            status = "ok"
        elif DENIED_RE.search(stderr) or DENIED_RE.search(stdout[:2000]):
            status = "denied"
        else:
            status = "nonzero_exit"
        record: dict[str, Any] = {"command": command, "exit_code": result.exit_code, "stdout": stdout,
                                  "stderr": stderr, "seconds": round(float(result.seconds), 3), "status": status}
        if kind in ("json", "jsonl") and parsed is not None:
            record["stdout_format"] = "json, re-serialised after redaction"
        if result.truncated:
            record["truncated"] = True
        if result.error:
            record["error"] = self.redactor.text(result.error)
        if self.redactor.total > before:
            record["redactions"] = self.redactor.total - before
        if parsed is not None:   # docker inspect exits 1 when one container vanished, yet prints the rest
            self.parsed[(section, name)] = parsed
        return record

    def _redact_stdout(self, probe: str, text: str, kind: str, cut: bool = False) -> tuple[str, Any]:
        """(stored stdout, the redacted parsed value or None). JSON output (kind json or jsonl, whenever it
        starts like JSON, whatever the exit code) is parsed and redacted by structure; output that looks
        like JSON but does not parse (or was cut at OUTPUT_BYTES) is withheld."""
        if kind == "compose":
            return self.redactor.compose(text), None
        if kind == "yaml":
            return self.redactor.yaml(text), None
        if kind in ("json", "jsonl") and text.lstrip()[:1] in ("{", "["):
            try:
                if cut:
                    raise ValueError("cut")
                if kind == "json":
                    value = self.redactor.value(json.loads(text))
                    return json.dumps(value, ensure_ascii=False), value
                values = [self.redactor.value(json.loads(line)) for line in text.splitlines() if line.strip()]
            except ValueError:
                return self._withhold(probe), None
            return "\n".join(json.dumps(v, ensure_ascii=False) for v in values) + "\n", values
        return self.redactor.text(text), None

    def _withhold(self, probe: str) -> str:
        """JSON that does not parse cannot be redacted by structure (container Env values), so it is not kept."""
        self.withheld.append(probe)
        return f"{MARK}: output withheld: it was not complete, valid JSON, so it could not be redacted by structure"

    def ok(self, section: str, name: str) -> dict | None:
        record = self.raw[section].get(name)
        return record if record and record["status"] == "ok" else None

    # -- the capture -----------------------------------------------------------------

    def run(self) -> None:
        self._host()
        self._gpu()
        self._storage()
        self._network()
        self._systemd()   # before the probes that ask it whether a daemon runs
        self._packages()
        self._cron()
        self._logging()
        self._docker()
        self._compose()
        self._cockpit()
        self._cosmos()
        self._tailscale()
        self._backups()
        if self.helper is not None:
            self._users()

    def _host(self) -> None:
        s = "host_baseline"
        self.probe(s, "hostname", ["hostname"])
        self.probe(s, "uname", ["uname", "-snrvm"])
        self.probe(s, "os_release", ["cat", "/etc/os-release"])
        self.probe(s, "uptime", ["uptime"])
        self.probe(s, "boot_time", ["uptime", "-s"])
        self.probe(s, "cpu", ["lscpu", "-J"], kind="json")
        self.probe(s, "memory", ["free", "-b"])
        self.probe(s, "virtualization", ["systemd-detect-virt"])
        self.probe(s, "time", ["timedatectl", "show"])
        self.probe(s, "pci", ["lspci", "-nn"])

    def _gpu(self) -> None:
        s = "gpu"
        listed = self.probe(s, "list", ["nvidia-smi", "-L"])
        if listed["status"] == "missing_command":
            return
        fields = ("index,name,uuid,driver_version,pci.bus_id,memory.total,memory.used,power.limit,power.draw,"
                  "persistence_mode,pstate,temperature.gpu,utilization.gpu")
        self.probe(s, "query", ["nvidia-smi", f"--query-gpu={fields}", "--format=csv,noheader"])
        self.probe(s, "processes", ["nvidia-smi", "--query-compute-apps=gpu_uuid,pid,process_name,used_memory",
                                    "--format=csv,noheader"])
        self.probe(s, "topology", ["nvidia-smi", "topo", "-m"])
        self.probe(s, "nvlink", ["nvidia-smi", "nvlink", "--status"])

    def _storage(self) -> None:
        s = "storage"
        self.probe(s, "block_devices", ["lsblk", "-J", "-b", "-o", "NAME,KNAME,PATH,TYPE,SIZE,ROTA,TRAN,MODEL,SERIAL,"
                                                                    "FSTYPE,UUID,LABEL,MOUNTPOINTS"], kind="json")
        self.probe(s, "mounts", ["findmnt", "-J", "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"], kind="json")
        self.probe(s, "usage", ["df", "-P", "-B1", "-T"])
        self.probe(s, "inodes", ["df", "-P", "-i"])
        self.probe(s, "fstab", ["cat", "/etc/fstab"])
        self.probe(s, "swap", ["swapon", "--show", "--bytes"])
        self.probe(s, "zfs_pools", ["zpool", "list", "-H", "-o", "name,size,alloc,free,health"])
        self.probe(s, "zfs_datasets", ["zfs", "list", "-H", "-o", "name,used,avail,mountpoint"])
        if self.helper is not None:   # helper mode: SMART from sf-system's smart verb (never waking a disk)
            self.helper_probe(s, "smart", "smart")
            return
        scan = self.probe(s, "smart_scan", ["smartctl", "--scan", "-j"], root=True, kind="json")
        found = self.parsed.get((s, "smart_scan")) if scan["status"] == "ok" else None
        devices = found.get("devices") if isinstance(found, dict) else None
        for device in devices if isinstance(devices, list) else []:
            name, dev_type = (device or {}).get("name"), (device or {}).get("type")
            if not isinstance(name, str) or not DEVICE_RE.match(name):
                continue
            argv = ["smartctl", "-j", "-n", "standby", "-i", "-H", "-A"]
            if isinstance(dev_type, str) and SMART_TYPE_RE.match(dev_type):
                argv += ["-d", dev_type]
            # smartctl exits non-zero for disk warnings and standby; its JSON says which.
            self.probe(s, f"smart:{name}", [*argv, name], root=True, kind="json", timeout=30)

    def _network(self) -> None:
        s = "network"
        self.probe(s, "addresses", ["ip", "-j", "addr", "show"], kind="json")
        self.probe(s, "routes", ["ip", "-j", "route", "show"], kind="json")
        self.probe(s, "routes6", ["ip", "-j", "-6", "route", "show"], kind="json")
        if self.helper is not None:   # helper mode: who listens, with process names, from sf-system's ports verb
            self.helper_probe(s, "listeners", "ports")
        else:
            self.probe(s, "listeners", ["ss", "-H", "-l", "-n", "-t", "-u", "-p"], root=True)
        self.probe(s, "resolver", ["resolvectl", "status"])
        self.probe(s, "resolv_conf", ["cat", "/etc/resolv.conf"])
        self.probe(s, "hosts", ["cat", "/etc/hosts"])
        # ufw's own `status` takes a lock file in /run; its config files say the same without writing.
        self.probe(s, "ufw_conf", ["cat", "/etc/ufw/ufw.conf"])
        self.probe(s, "ufw_rules", ["cat", "/etc/ufw/user.rules"], root=True)
        self.probe(s, "ufw_rules6", ["cat", "/etc/ufw/user6.rules"], root=True)
        self.probe(s, "firewall_nft", ["nft", "list", "ruleset"], root=True)
        self.probe(s, "netplan", ["netplan", "get"], root=True, kind="yaml")

    def _packages(self) -> None:
        s = "packages"
        self.probe(s, "dpkg", ["dpkg-query", "-W", DPKG_FORMAT], timeout=60)
        self.probe(s, "holds", ["apt-mark", "showhold"])
        refusal = self.daemon_refusal("snapd.service", "`snap list`", "snapd.socket")
        if refusal:
            self.skip(s, "snaps", ["snap", "list"], refusal)
        else:
            self.probe(s, "snaps", ["snap", "list"], timeout=30)
        self.probe(s, "sources", ["cat", "/etc/apt/sources.list"])
        self._read_dir(s, "sources.list.d", "/etc/apt/sources.list.d", r"\.(list|sources)$")
        self.probe(s, "auto_upgrades", ["cat", "/etc/apt/apt.conf.d/20auto-upgrades"])
        self.probe(s, "unattended_upgrades", ["cat", "/etc/apt/apt.conf.d/50unattended-upgrades"])

    def _read_dir(self, section: str, label: str, directory: str, pattern: str, root: bool = False) -> None:
        """List directory (ls -1A), then print each plain config file in it whose name matches pattern."""
        listing = self.probe(section, f"{label}:list", ["ls", "-1A", directory], root=root)
        if listing["status"] != "ok":
            return
        names = [n for n in listing["stdout"].splitlines() if CONFIG_NAME_RE.match(n) and re.search(pattern, n)]
        for name in names[:MAX_LISTED_FILES]:
            self.read_found(section, f"{label}/{name}", f"{directory}/{name}", root=root)

    def read_found(self, section: str, name: str, path: str, *, root: bool, kind: str = "text") -> dict:
        """Print a file a search, a listing or Docker named. One the allowlist does not accept is recorded
        as skipped (listed, not read), never raised: only the fixed probe list stops a capture."""
        try:
            check_argv(["cat", path])
        except UnsafeCommand:
            return self.skip(section, name, ["cat", path], _unread_reason(path))
        return self.probe(section, name, ["cat", path], root=root, kind=kind)

    def _systemd(self) -> None:
        s = "systemd"
        self.probe(s, "unit_files", ["systemctl", "list-unit-files", "--no-legend", "--no-pager"])
        units = self.probe(s, "units", ["systemctl", "list-units", "--all", "--no-legend", "--no-pager", "--plain",
                                        "--full"])
        self.probe(s, "timers", ["systemctl", "list-timers", "--all", "--no-legend", "--no-pager", "--full"])
        self.probe(s, "state", ["systemctl", "is-system-running"])
        if units["status"] != "ok":
            return
        names = [row["unit"] for row in parse_list_units(units["stdout"])
                 if row["load"] == "loaded" and row["unit"].endswith((".service", ".socket", ".timer"))
                 and UNIT_RE.match(row["unit"])]
        properties = ("Id,Description,LoadState,ActiveState,SubState,UnitFileState,FragmentPath,DropInPaths,"
                      "Requires,Wants,After,BindsTo,TriggeredBy,Triggers,Restart")
        for index in range(0, len(names), CHUNK):
            self.probe(s, f"show:{index // CHUNK + 1}",
                       ["systemctl", "show", "--no-pager", f"--property={properties}", *names[index:index + CHUNK]],
                       timeout=30)

    def _cron(self) -> None:
        s = "cron"
        self.probe(s, "crontab", ["cat", "/etc/crontab"])
        self._read_dir(s, "cron.d", "/etc/cron.d", r".")
        self.probe(s, "periodic", ["ls", "-la", "/etc/cron.hourly", "/etc/cron.daily", "/etc/cron.weekly",
                                   "/etc/cron.monthly"])
        self.probe(s, "user_crontab", ["crontab", "-l"])
        self._read_dir(s, "spool", "/var/spool/cron/crontabs", r".", root=True)

    def _logging(self) -> None:
        s = "logging"
        self.probe(s, "journal_usage", ["journalctl", "--disk-usage"])
        self.probe(s, "boots", ["journalctl", "--list-boots", "--no-pager"], timeout=30)
        self.probe(s, "journald_conf", ["cat", "/etc/systemd/journald.conf"])
        self.probe(s, "journald_dropins", ["ls", "-la", "/etc/systemd/journald.conf.d"])
        self.probe(s, "logrotate", ["ls", "-1A", "/etc/logrotate.d"])

    def daemon_refusal(self, unit: str, client: str, socket: str) -> str | None:
        """Why client must not be asked (None: it may), since asking could start unit through socket.
        Asked when systemd lists unit as active or does not list it (not installed as that unit, such
        as Docker from a snap), and when systemd is not this host's init (nothing is socket-activated
        then). Not asked when the unit is listed but not running, or when systemd runs but its unit
        list is unavailable (a timeout, no bus): fail closed."""
        listed = self.ok("systemd", "units")
        if listed is None:
            if not self.systemd_booted:
                return None
            return (f"systemd runs here but its unit list is unavailable, so whether {unit} runs is unknown, and "
                    f"{client} could start it through {socket}")
        rows = [row for row in parse_list_units(listed["stdout"]) if row["unit"] == unit]
        if rows and rows[0]["active"] != "active":
            return f"{unit} is not running, and {client} could start it through {socket}"
        return None

    def _docker(self) -> None:
        s = "docker"
        fmt = ["--format", "{{json .}}"]
        self.probe(s, "daemon_json", ["cat", "/etc/docker/daemon.json"], kind="json")
        if self.helper is not None:
            # helper mode: the container map from Docker's state files, and while Docker runs (and the helper's
            # settings allow it) its own image list and health, through fixed read-only API calls; no socket here
            self.helper_probe(s, "containers", "containers")
            for call in ("info", "images", "health"):
                self.helper_probe(s, f"api_{call}", "docker", call=call)
            return
        # A docker command reaches the daemon through docker.socket, which would start dockerd (and with it
        # every container whose restart policy says so).
        reason = self.daemon_refusal("docker.service", "the docker CLI", "docker.socket")
        if reason:
            for name, argv in (("version", ["docker", "version", *fmt]),
                               ("containers", ["docker", "ps", "-a", "--no-trunc", *fmt])):
                self.skip(s, name, argv, reason)
            return
        version = self.probe(s, "version", ["docker", "version", *fmt], root=True, kind="json")
        if version["status"] == "missing_command":
            return
        self.probe(s, "info", ["docker", "info", *fmt], root=True, kind="json", timeout=30)
        self.probe(s, "containers", ["docker", "ps", "-a", "--no-trunc", *fmt], root=True, kind="jsonl", timeout=30)
        ids = [row.get("ID") for row in self.parsed.get((s, "containers")) or [] if isinstance(row, dict)]
        ids = [i for i in ids if isinstance(i, str) and CONTAINER_REF_RE.match(i)]
        for index in range(0, len(ids), CHUNK):
            self.probe(s, f"inspect:{index // CHUNK + 1}", ["docker", "inspect", *ids[index:index + CHUNK]],
                       root=True, kind="json", timeout=60)
        self.probe(s, "networks", ["docker", "network", "ls", "--no-trunc", *fmt], root=True, kind="jsonl")
        nets = [row.get("ID") for row in self.parsed.get((s, "networks")) or [] if isinstance(row, dict)]
        nets = [n for n in nets if isinstance(n, str) and CONTAINER_REF_RE.match(n)]
        for index in range(0, len(nets), CHUNK):
            self.probe(s, f"network_inspect:{index // CHUNK + 1}", ["docker", "network", "inspect",
                                                                    *nets[index:index + CHUNK]],
                       root=True, kind="json", timeout=60)
        self.probe(s, "volumes", ["docker", "volume", "ls", *fmt], root=True, kind="jsonl")
        self.probe(s, "images", ["docker", "image", "ls", "--no-trunc", *fmt], root=True, kind="jsonl", timeout=30)
        self.probe(s, "compose_projects", ["docker", "compose", "ls", "--all", "--format", "json"], root=True,
                   kind="json")

    def _compose(self) -> None:
        s = "compose_files"
        names: list[str] = ["("]
        for pattern in COMPOSE_PATTERNS:
            names += ["-name", pattern, "-o"]
        names[-1] = ")"
        search = self.probe(s, "search", ["find", *FIND_ROOTS, "-xdev", "-maxdepth", str(FIND_DEPTH), "-type", "f",
                                          *names, "-print"], root=True, timeout=60)
        found: dict[str, set[str]] = {}
        if search["status"] in ("ok", "nonzero_exit", "denied"):   # find exits 1 on one unreadable dir
            for line in search["stdout"].splitlines():
                if line.startswith("/"):
                    found.setdefault(line, set()).add("search")
        for path in compose_paths_from_docker(self):
            found.setdefault(path, set()).add("docker")
        if self.helper is not None:   # helper mode: what Compose makes of each folder (hashes and images, no contents)
            folders = sorted({str((c.get("labels") or {}).get("com.docker.compose.project.working_dir") or "")
                              for c in _helper_containers(self)} - {""})
            for folder in folders[:MAX_COMPOSE_FILES]:
                self.helper_probe(s, f"helper:{folder}", "compose", dir=folder)
            for c in _helper_containers(self):
                for path in _split_paths((c.get("labels") or {}).get("com.docker.compose.project.config_files")):
                    found.setdefault(path, set()).add("docker")
        self.compose_found = {path: sorted(how) for path, how in sorted(found.items())}
        for path in list(self.compose_found)[:MAX_COMPOSE_FILES]:
            name = f"file:{path}"
            if not _plain_path(path) or not COMPOSE_NAME_RE.match(path.rsplit("/", 1)[-1]):
                self.skip(s, name, ["cat", path], "not a compose file name or not a plain absolute path: listed, "
                                                  "not read")
            else:
                self.read_found(s, name, path, root=True, kind="compose")

    def _users(self) -> None:
        """Helper mode, once Seraphina's system access may read them (class private): who can act as root, from the
        access enumeration it last kept (accounts, groups, sudo rules, keys by fingerprint; names kept, no secret)."""
        record = self.helper_probe("users", "access", "access", read=True)
        if record["status"] == "ok":
            result = (self.parsed.get(("users", "access")) or {}).get("result") or {}
            offset = len(result.get("rows") or [])
            while offset < int(result.get("total") or 0) and offset < 5000:
                page = self.helper_probe("users", f"access:{offset}", "access", read=True, offset=offset)
                rows = ((self.parsed.get(("users", f"access:{offset}")) or {}).get("result") or {}).get("rows") or []
                if page["status"] != "ok" or not rows:
                    break
                offset += len(rows)

    def _cockpit(self) -> None:
        s = "cockpit"
        self.probe(s, "units", ["systemctl", "show", "--no-pager",
                                "--property=Id,LoadState,ActiveState,SubState,UnitFileState,Listen,TriggeredBy",
                                "cockpit.socket", "cockpit.service"])
        self.probe(s, "config", ["cat", "/etc/cockpit/cockpit.conf"])
        self.probe(s, "config_dir", ["ls", "-la", "/etc/cockpit"])

    def _cosmos(self) -> None:
        """Cosmos's units, its folder and its config (routes, with its private key redacted), when Cosmos is
        installed: a unit or a container of its own. Without either, the folder is listed (names, sizes and dates)
        and the config is not read: a stale route table would show routes nothing serves, beside a private key."""
        s = "cosmos"
        files = self.ok("systemd", "unit_files")
        units = sorted({row["unit"] for row in parse_list_unit_files(files["stdout"] if files else "")
                        if "cosmos" in row["unit"].lower() and UNIT_RE.match(row["unit"])})
        if units:
            self.probe(s, "units", ["systemctl", "show", "--no-pager",
                                    "--property=Id,LoadState,ActiveState,SubState,UnitFileState,FragmentPath",
                                    *units])
        listing = self.probe(s, "config_dir", ["ls", "-la", "/var/lib/cosmos"], root=True)
        if units or self._cosmos_container():
            self.probe(s, "config", ["cat", "/var/lib/cosmos/cosmos.config.json"], root=True, kind="json")
            return
        stale = listing["status"] == "ok" and bool((listing["stdout"] or "").strip())
        self.skip(s, "config", ["cat", "/var/lib/cosmos/cosmos.config.json"],
                  "Cosmos is not installed (no unit, no container of its own)" +
                  (": stale files are left in /var/lib/cosmos, listed by name, size and date, never read" if stale
                   else ""))

    def _cosmos_container(self) -> bool:
        """A container that is Cosmos itself (its image or its name), not one of the apps Cosmos once managed: those
        keep their cosmos-* labels after Cosmos is gone."""
        for row in self.parsed.get(("docker", "containers")) or []:
            if not isinstance(row, dict):
                continue
            image, names = str(row.get("Image") or "").lower(), str(row.get("Names") or "").lower()
            if "cosmos-server" in image or "azukaar/cosmos" in image or any(
                    name.strip().lstrip("/") in ("cosmos", "cosmos-server") for name in names.split(",")):
                return True
        return False

    def _tailscale(self) -> None:
        s = "tailscale"
        version = self.probe(s, "version", ["tailscale", "version"])
        if version["status"] == "missing_command":
            return
        self.probe(s, "status", ["tailscale", "status", "--json"], kind="json")
        self.probe(s, "serve", ["tailscale", "serve", "status", "--json"], kind="json")
        self.probe(s, "funnel", ["tailscale", "funnel", "status", "--json"], kind="json")

    def _backups(self) -> None:
        s = "backups"
        self.probe(s, "restic", ["restic", "version"])
        self.probe(s, "borg", ["borg", "--version"])
        self.probe(s, "kopia", ["kopia", "--version"])
        self.probe(s, "snapraid", ["snapraid", "--version"])
        self.probe(s, "snapraid_conf", ["cat", "/etc/snapraid.conf"])
        self.probe(s, "timeshift", ["timeshift", "--version"])


def compose_paths_from_docker(collector: Collector) -> list[str]:
    """Compose files the Docker side names: container labels (com.docker.compose.project.config_files)
    and `docker compose ls` ConfigFiles. Configured paths: a claim about the past, not proof the file
    on disk made the container."""
    paths: list[str] = []
    for value in _inspect_objects(collector):
        labels = ((value.get("Config") or {}).get("Labels") or {})
        paths += _split_paths(labels.get("com.docker.compose.project.config_files"))
    projects = collector.parsed.get(("docker", "compose_projects"))
    for project in projects if isinstance(projects, list) else []:
        if isinstance(project, dict):
            paths += _split_paths(project.get("ConfigFiles"))
    return paths


def _split_paths(value: Any) -> list[str]:
    if not isinstance(value, str) or MARK in value:
        return []
    return [part.strip() for part in value.split(",") if part.strip().startswith("/")]


def _inspect_objects(collector: Collector) -> list[dict]:
    out = []
    for (section, name), value in collector.parsed.items():
        if section == "docker" and name.startswith("inspect:") and isinstance(value, list):
            out += [item for item in value if isinstance(item, dict)]
    return out


# ---------------------------------------------------------------- parsing


def parse_os_release(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and re.fullmatch(r"[A-Z_]+", key):
            out[key] = value.strip().strip('"')
    return out


def parse_list_units(text: str) -> list[dict]:
    """`systemctl list-units --plain --no-legend --full` rows: unit, load, active, sub, description."""
    rows = []
    for line in text.splitlines():
        parts = line.split(None, 4)
        if parts and parts[0] in ("●", "*"):
            parts = line.split(None, 5)[1:]
        if len(parts) >= 4:
            rows.append({"unit": parts[0], "load": parts[1], "active": parts[2], "sub": parts[3],
                         "description": parts[4] if len(parts) > 4 else ""})
    return rows


def parse_list_unit_files(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            rows.append({"unit": parts[0], "state": parts[1], "preset": parts[2] if len(parts) > 2 else None})
    return rows


def parse_systemctl_show(text: str) -> list[dict[str, str]]:
    blocks, current = [], {}
    for line in text.splitlines() + [""]:
        if not line.strip():
            if current:
                blocks.append(current)
                current = {}
            continue
        key, sep, value = line.partition("=")
        if sep:
            current[key] = value
    return blocks


def parse_ss(text: str) -> list[dict]:
    """`ss -H -l -n -t -u -p` rows as endpoints: proto, address, port, interface, scope, processes."""
    out = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0] not in ("tcp", "udp"):
            continue
        address, _, port = parts[4].rpartition(":")
        interface = None
        if "%" in address:
            address, _, interface = address.partition("%")
        address = address.strip("[]")
        processes = sorted({name for name, _pid in re.findall(r'\("([^"]+)",pid=(\d+)', " ".join(parts[6:]))})
        out.append({"proto": parts[0], "address": address, "port": int(port) if port.isdigit() else port,
                    "interface": interface, "scope": address_scope(address), "processes": processes, "source": "ss"})
    return out


def address_scope(address: str) -> str:
    """loopback, all (every interface), tailnet (Tailscale's CGNAT and ULA ranges), private, link-local or public."""
    if address in ("*", "", "0.0.0.0", "::"):
        return "all"
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return "unknown"
    mapped = getattr(ip, "ipv4_mapped", None)
    ip = mapped or ip
    if ip.is_loopback:
        return "loopback"
    if ip.is_unspecified:
        return "all"
    if ip in ipaddress.ip_network("100.64.0.0/10") or ip in ipaddress.ip_network("fd7a:115c:a1e0::/48"):
        return "tailnet"
    if ip.is_link_local:
        return "link-local"
    if ip.is_private:
        return "private"
    return "public"


def compose_services(text: str) -> list[str]:
    """The service names under a compose file's top-level `services:` key (a line scan, no YAML parser)."""
    services, inside, child = [], False, None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            inside, child = line.rstrip().startswith("services:"), None
            continue
        if inside:
            child = indent if child is None else child
            match = re.match(r'\s*["\']?([A-Za-z0-9._-]+)["\']?\s*:', line) if indent == child else None
            if match:
                services.append(match.group(1))
    return services


# ---------------------------------------------------------------- the indexes


def build_indexes(collector: Collector) -> dict:
    units = _systemd_units(collector)
    packages = _host_packages(collector)
    containers = _containers(collector)
    compose = _compose_index(collector, containers)
    endpoints = _endpoints(collector, containers)
    relationships = _relationships(collector, units, containers, endpoints)
    return {"systemd_units": units, "host_packages": packages, "containers": containers, "compose_files": compose,
            "endpoints": endpoints, "relationships": relationships}


def _systemd_units(c: Collector) -> dict:
    units: dict[str, dict] = {}
    files = c.ok("systemd", "unit_files")
    for row in parse_list_unit_files(files["stdout"] if files else ""):
        units.setdefault(row["unit"], {})["unit_file_state"] = row["state"]
    listed = c.ok("systemd", "units")
    for row in parse_list_units(listed["stdout"] if listed else ""):
        units.setdefault(row["unit"], {}).update(load=row["load"], active=row["active"], sub=row["sub"],
                                                 description=row["description"])
    for name, record in c.raw["systemd"].items():
        if not name.startswith("show:") or record["status"] != "ok":
            continue
        for block in parse_systemctl_show(record["stdout"]):
            unit = block.get("Id")
            if not unit:
                continue
            entry = units.setdefault(unit, {})
            entry.update({key: value for key, value in (
                ("description", block.get("Description")), ("load", block.get("LoadState")),
                ("active", block.get("ActiveState")), ("sub", block.get("SubState")),
                ("unit_file_state", block.get("UnitFileState")), ("fragment_path", block.get("FragmentPath")),
                ("restart", block.get("Restart"))) if value})
            entry["drop_ins"] = (block.get("DropInPaths") or "").split()
            for prop in ("Requires", "Wants", "After", "BindsTo", "TriggeredBy", "Triggers"):
                entry[_snake(prop)] = (block.get(prop) or "").split()
    return dict(sorted(units.items()))


def _host_packages(c: Collector) -> dict:
    packages: dict[str, dict] = {}
    dpkg = c.ok("packages", "dpkg")
    for line in (dpkg["stdout"] if dpkg else "").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0]:
            packages[parts[0]] = {"version": parts[1], "status": parts[2].strip() if len(parts) > 2 else "",
                                  "source": "dpkg", "held": False}
    holds = c.ok("packages", "holds")
    for name in (holds["stdout"] if holds else "").split():
        if name in packages:
            packages[name]["held"] = True
    snaps = c.ok("packages", "snaps")
    for line in (snaps["stdout"] if snaps else "").splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 3:
            packages[f"snap:{parts[0]}"] = {"version": parts[1], "revision": parts[2],
                                            "tracking": parts[3] if len(parts) > 3 else None, "source": "snap"}
    return dict(sorted(packages.items()))


def _helper_containers(c: Collector) -> list[dict]:
    answer = c.parsed.get(("docker", "containers"))
    return [x for x in (answer or {}).get("containers") or [] if isinstance(x, dict) and not x.get("error")] \
        if isinstance(answer, dict) else []


def _containers_from_helper(c: Collector) -> dict:
    """indexes.containers from sf-system's container map (helper mode), in the shape docker inspect gives; health
    from its read-only Docker API calls when they answered, else unknown."""
    health = ((c.parsed.get(("docker", "api_health")) or {}).get("health") or {})
    out: dict[str, dict] = {}
    for item in _helper_containers(c):
        labels = item.get("labels") or {}
        mounts = [{"type": m.get("type"), "name": m.get("name"), "source": m.get("source"),
                   "destination": m.get("destination"), "mode": None, "rw": m.get("rw")} for m in item.get("mounts") or []]
        out[str(item.get("name") or item.get("id"))] = {
            "id": item.get("id"), "image": item.get("image"), "image_id": item.get("image_id"),
            "state": "running" if item.get("running") else "exited",
            "health": (health.get(item.get("id")) or {}).get("status"),
            "restart_policy": item.get("restart_policy"), "created": item.get("created"),
            "started_at": item.get("started_at"), "network_mode": item.get("network_mode"),
            "privileged": bool(item.get("privileged")),
            "gpu": any("nvidia" in d or "/dev/dri" in d for d in item.get("device_paths") or []),
            "networks": {net: {"ip": (info or {}).get("ip"), "static_ip": None}
                         for net, info in (item.get("networks") or {}).items()},
            "ports": [{"container_port": str(p.get("container_port")), "protocol": p.get("proto"),
                       "host_ip": p.get("host_ip"), "host_port": str(p.get("host_port"))} for p in item.get("ports") or []],
            "mounts": mounts, "docker_socket": bool(item.get("docker_socket")),
            "compose_project": labels.get("com.docker.compose.project"),
            "compose_service": labels.get("com.docker.compose.service"),
            "compose_config_files": _split_paths(labels.get("com.docker.compose.project.config_files")),
            "compose_working_dir": labels.get("com.docker.compose.project.working_dir"),
            "env_count": None, "labels_count": len(labels), "evidence": "sf-system.containers"}
    return dict(sorted(out.items()))


def _containers(c: Collector) -> dict:
    if c.helper is not None:
        return _containers_from_helper(c)
    containers: dict[str, dict] = {}
    for item in _inspect_objects(c):
        config, host, state = item.get("Config") or {}, item.get("HostConfig") or {}, item.get("State") or {}
        labels = config.get("Labels") or {}
        name = str(item.get("Name") or item.get("Id") or "").lstrip("/")
        networks = (item.get("NetworkSettings") or {}).get("Networks") or {}
        ports = []
        for spec, bindings in ((item.get("NetworkSettings") or {}).get("Ports") or {}).items():
            port, _, proto = str(spec).partition("/")
            for binding in bindings or []:
                ports.append({"container_port": port, "protocol": proto or "tcp", "host_ip": binding.get("HostIp"),
                              "host_port": binding.get("HostPort")})
        mounts = [{"type": m.get("Type"), "name": m.get("Name"), "source": m.get("Source"),
                   "destination": m.get("Destination"), "mode": m.get("Mode"), "rw": m.get("RW")}
                  for m in item.get("Mounts") or [] if isinstance(m, dict)]
        requests = host.get("DeviceRequests") or []
        gpu = host.get("Runtime") == "nvidia" or any(
            "gpu" in [cap for caps in (r.get("Capabilities") or []) for cap in (caps or [])]
            for r in requests if isinstance(r, dict))
        containers[name] = {
            "id": item.get("Id"), "image": config.get("Image"), "image_id": item.get("Image"),
            "state": state.get("Status"), "health": (state.get("Health") or {}).get("Status"),
            "restart_policy": (host.get("RestartPolicy") or {}).get("Name"), "created": item.get("Created"),
            "started_at": state.get("StartedAt"), "network_mode": host.get("NetworkMode"),
            "privileged": bool(host.get("Privileged")), "gpu": bool(gpu),
            "networks": {net: {"ip": (info or {}).get("IPAddress") or None,
                               "static_ip": ((info or {}).get("IPAMConfig") or {}).get("IPv4Address") or None}
                         for net, info in networks.items()},
            "ports": ports, "mounts": mounts,
            "docker_socket": any(m["source"] in ("/var/run/docker.sock", "/run/docker.sock") for m in mounts),
            "compose_project": labels.get("com.docker.compose.project"),
            "compose_service": labels.get("com.docker.compose.service"),
            "compose_config_files": _split_paths(labels.get("com.docker.compose.project.config_files")),
            "compose_working_dir": labels.get("com.docker.compose.project.working_dir"),
            "env_count": len(config.get("Env") or []), "labels_count": len(labels),
            "evidence": "docker.inspect"}
    if not containers:   # no inspect (denied): what docker ps shows
        for row in c.parsed.get(("docker", "containers")) or []:
            if isinstance(row, dict) and row.get("Names"):
                containers[str(row["Names"])] = {"id": row.get("ID"), "image": row.get("Image"),
                                                 "state": row.get("State"), "status": row.get("Status"),
                                                 "ports_text": row.get("Ports"), "evidence": "docker.containers"}
    return dict(sorted(containers.items()))


def _compose_index(c: Collector, containers: dict) -> list[dict]:
    out = []
    for path, how in c.compose_found.items():
        record = c.raw["compose_files"].get(f"file:{path}") or {}
        users = sorted(name for name, entry in containers.items() if path in (entry.get("compose_config_files") or []))
        entry: dict[str, Any] = {"path": path, "found_by": how, "read": record.get("status") == "ok",
                                 "containers": users}
        if record.get("status") == "ok":
            entry["services"] = compose_services(record.get("stdout") or "")
        elif record:
            entry["reason"] = record.get("error") or record.get("status")
        else:
            entry["reason"] = f"not read: more than {MAX_COMPOSE_FILES} compose files"
        out.append(entry)
    return out


def _endpoints(c: Collector, containers: dict) -> list[dict]:
    listeners = c.ok("network", "listeners")
    if c.helper is not None:   # helper mode: sf-system's ports answer, already parsed
        endpoints = [{"proto": e.get("proto"), "address": e.get("address"), "port": e.get("port"),
                      "scope": address_scope(str(e.get("address") or "")), "process": e.get("process"),
                      "unit": e.get("unit"), "source": "sf-system.ports"}
                     for e in (c.parsed.get(("network", "listeners")) or {}).get("listeners") or []
                     if isinstance(e, dict)] if listeners else []
    else:
        endpoints = parse_ss(listeners["stdout"]) if listeners else []
    for name, entry in containers.items():
        for port in entry.get("ports") or []:
            address = port.get("host_ip") or "0.0.0.0"
            endpoints.append({"proto": port.get("protocol"), "address": address,
                              "port": int(port["host_port"]) if str(port.get("host_port") or "").isdigit()
                              else port.get("host_port"), "scope": address_scope(address), "container": name,
                              "container_port": port.get("container_port"), "source": "docker"})
    return endpoints


def _relationships(c: Collector, units: dict, containers: dict, endpoints: list[dict]) -> list[dict]:
    edges: list[dict] = []

    def edge(source: str, relation: str, target: str, evidence: str, confidence: str, **extra: Any) -> None:
        edges.append({"from": source, "relation": relation, "to": target, "evidence": evidence,
                      "confidence": confidence, **{k: v for k, v in extra.items() if v is not None}})

    for name, entry in containers.items():
        me = f"container:{name}"
        if entry.get("image"):
            edge(me, "uses_image", f"image:{entry['image']}", "docker.inspect", "observed")
        for net, info in (entry.get("networks") or {}).items():
            edge(me, "attached_to", f"network:{net}", "docker.inspect", "observed", static_ip=info.get("static_ip"))
        for mount in entry.get("mounts") or []:
            target = f"volume:{mount['name']}" if mount.get("type") == "volume" and mount.get("name") \
                else f"path:{mount.get('source')}"
            edge(me, "mounts", target, "docker.inspect", "observed", destination=mount.get("destination"),
                 mode="rw" if mount.get("rw") else "ro")
        if entry.get("docker_socket"):
            edge(me, "docker_socket_mounted", "path:/var/run/docker.sock", "docker.inspect", "observed")
        if entry.get("gpu"):
            edge(me, "gpu_access_configured", "gpu:all", "docker.inspect",
                 "configured: capability, not proof of live allocation")
        for path in entry.get("compose_config_files") or []:
            edge(me, "defined_by", f"compose:{path}", "docker.inspect labels",
                 "observed label; the file on disk can have changed since")
    for endpoint in endpoints:
        point = f"endpoint:{endpoint['proto']}/{endpoint['address']}:{endpoint['port']}"
        if endpoint.get("container"):
            edge(f"container:{endpoint['container']}", "publishes", point, "docker.inspect", "observed",
                 container_port=endpoint.get("container_port"))
        for process in endpoint.get("processes") or []:
            edge(f"process:{process}", "listens_on", point, "network.listeners", "observed")
    for name, entry in units.items():
        for prop, relation in (("requires", "Requires"), ("wants", "Wants"), ("after", "After"),
                               ("binds_to", "BindsTo"), ("triggers", "Triggers")):
            for other in entry.get(prop) or []:
                edge(f"unit:{name}", relation, f"unit:{other}", "systemd.show", "systemd declared")
    serve = c.parsed.get(("tailscale", "serve"))
    for hostport, web in ((serve or {}).get("Web") or {}).items() if isinstance(serve, dict) else []:
        for path, handler in ((web or {}).get("Handlers") or {}).items():
            target = (handler or {}).get("Proxy") or (handler or {}).get("Path") or (handler or {}).get("Text")
            if target:
                edge(f"tailscale-serve:{hostport}{path}", "routes_to", f"url:{target}", "tailscale.serve", "configured")
    for port, tcp in ((serve or {}).get("TCP") or {}).items() if isinstance(serve, dict) else []:
        if isinstance(tcp, dict) and tcp.get("TCPForward"):
            edge(f"tailscale-serve:tcp/{port}", "routes_to", f"tcp:{tcp['TCPForward']}", "tailscale.serve",
                 "configured")
    cosmos = c.parsed.get(("cosmos", "config"))
    routes = (((cosmos or {}).get("HTTPConfig") or {}).get("ProxyConfig") or {}).get("Routes") \
        if isinstance(cosmos, dict) else None
    for route in routes if isinstance(routes, list) else []:
        if isinstance(route, dict) and route.get("Target"):
            label = route.get("Name") or f"{route.get('Host') or ''}{route.get('PathPrefix') or ''}"
            edge(f"cosmos-route:{label}", "routes_to", f"url:{route['Target']}", "cosmos.config", "configured",
                 host=route.get("Host") or None, mode=route.get("Mode"), auth=route.get("AuthEnabled"))
    return edges


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)([A-Z])", r"_\1", name).lower()


# ---------------------------------------------------------------- the snapshot


def collect(runner: Any = None, *, sudo: bool = False, known_secrets: Iterable[str] | None = None,
            helper: Any = None) -> dict:
    """Inventory this host read-only and return the snapshot (Capture format v0, module docstring).

    runner: anything with which(program) -> bool and run(argv, timeout) -> Result (SubprocessRunner by
    default). known_secrets: values to redact wherever they appear (default: the credential-named
    environment variables of this process, such as the factory's API keys). helper: a client of Seraphina's
    system access (swfactory.helpers.HelperClient for sf-system): helper mode, in which its read verbs replace the
    probes that needed root (the Docker section and indexes.containers, listeners with their processes, SMART,
    Compose's hashes and images) and, once it may read them, a users section from its access enumeration.
    """
    collector = Collector(runner or SubprocessRunner(), sudo=sudo, helper=helper,
                          known_secrets=env_secret_values() if known_secrets is None else known_secrets)
    collected_at = utc_now()
    collector.run()
    indexes = build_indexes(collector)
    snapshot: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "host": _host(collector),
        "collected_at": collected_at,
        "scope": _scope(collector),
        "redaction": {"marker": MARK, "policy": REDACTION_POLICY},
    }
    snapshot.update({section: collector.raw[section] for section in SECTIONS})
    if "users" in collector.raw:
        snapshot["users"] = collector.raw["users"]
    snapshot["indexes"] = indexes
    snapshot["scope_limits"] = _scope_limits(collector, indexes)
    snapshot["redaction_review"] = _redaction_review(collector)
    snapshot["summary"] = _summary(collector, indexes)
    return snapshot


REDACTION_POLICY = [
    "Every container Env value (docker inspect Config.Env): NAME=[REDACTED], whatever the name.",
    "Every value under a key whose name contains KEY, TOKEN, SECRET, PASS or AUTH (any case); booleans and "
    "null are kept.",
    "Every `name=value`, `name: value` and `--name value` whose name contains one of those words.",
    "Private-key blocks, JWT-like strings, the user:password part of credential URLs and Bearer/Basic/Token "
    "header values.",
    "In YAML (netplan get, compose files): every value in the block of a key with a secret word (keys:, auth:), "
    "every line of its block scalar, and the list item after a secret-looking --flag.",
    "Every value of a compose file's environment: block; env_file contents are never read.",
    "Any value of this process's own credential-named environment variables (the factory's API keys).",
    "JSON output that does not parse is withheld, since it cannot be redacted by structure.",
]


def _host(c: Collector) -> dict:
    hostname = c.ok("host_baseline", "hostname")
    uname = c.ok("host_baseline", "uname")
    release = c.ok("host_baseline", "os_release")
    virt = c.raw["host_baseline"].get("virtualization") or {}
    parts = uname["stdout"].split() if uname else []
    os_release = parse_os_release(release["stdout"]) if release else {}
    return {"hostname": hostname["stdout"].strip() if hostname else None,
            "os": os_release.get("PRETTY_NAME"), "kernel": parts[2] if len(parts) > 2 else None,
            "machine": parts[-1] if len(parts) > 4 else None,
            "virtualization": (virt.get("stdout") or "").strip() or None}


def _scope(c: Collector) -> dict:
    try:
        user = pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        user = str(os.getuid())
    if c.sudo and not c.sudo_missing:
        how = "through `sudo -n` where root sees more"
    elif c.sudo:
        how = f"as {user} (sudo is not installed)"
    else:
        how = f"as {user}"
    return {"mode": "read-only", "sudo": c.sudo, "user": user, "uid": os.getuid(),
            "collector": f"factory snapshot {__version__}",
            "description": ("One read-only inventory of this host: an allowlist of side-effect-free commands, each "
                            f"with its own timeout, run {how}; nothing was installed, started, stopped or changed.")}


def _scope_limits(c: Collector, indexes: dict) -> list[str]:
    records = [(f"{section}.{name}", record) for section in SECTIONS for name, record in c.raw[section].items()]
    by_status: dict[str, list[str]] = {}
    for probe, record in records:
        by_status.setdefault(record["status"], []).append(probe)
    missing = sorted({record["error"].split(":", 1)[0] for _probe, record in records
                      if record["status"] == "missing_command"})
    limits = [
        "Only allow-listed read-only commands ran; nothing was installed, started, stopped or changed. Never run: "
        "lxc (on Ubuntu it can start the on-demand LXD installer), package managers, docker start/exec/run/pull/"
        "logs, backup tools against their repositories (they can take locks).",
        "Not read: application data, databases, Docker volumes, log message bodies, env files and file contents "
        "under home directories (paths there are listed, not opened).",
        f"Compose files are searched for under {', '.join(FIND_ROOTS)} (depth {FIND_DEPTH}, one filesystem each); "
        "others are known only from container labels and docker compose ls.",
        "SMART data comes only from disks that were awake (smartctl -n standby never wakes a sleeping disk).",
        _daemon_limit(c) + "; timedatectl and resolvectl ask their services over D-Bus (timedated starts on demand "
        "and exits idle).",
        "Not captured in v0: users, groups and sudoers, global pip/npm packages, certificates, model servers, "
        "backup history and restore tests.",
    ]
    if c.helper is not None:
        limits.append("Helper mode: Docker's containers, who listens on which port, SMART and Compose's hashes and "
                      "images came from Seraphina's system access (sf-system's fixed read verbs; no Docker socket "
                      "and no general sudo). Users, groups and sudo rules come from its access enumeration only once "
                      "it may read them.")
    elif not c.sudo and os.getuid() != 0:
        limits.append("Run without --sudo: root-only probes ran as this user, so some saw less or were denied.")
    if c.sudo_missing:
        limits.append("sudo is not installed here, so root-only probes ran without it, "
                      + ("as root (this capture runs as root)." if os.getuid() == 0
                         else "as this user: some saw less or were denied."))
    if c.sudo_refused:
        limits.append("sudo -n was refused (each probe's record has sudo's own words), so these ran without sudo: "
                      + ", ".join(c.sudo_refused) + ".")
    if missing:
        limits.append("Commands not installed here were recorded, not installed: " + ", ".join(missing) + ".")
    for status, label in (("denied", "Denied"), ("timeout", "Timed out"), ("skipped_by_policy", "Skipped by policy"),
                          ("error", "Could not start")):
        if by_status.get(status):
            limits.append(f"{label}: " + ", ".join(by_status[status]) + ".")
    unread = [entry["path"] for entry in indexes["compose_files"] if not entry["read"]]
    if unread:
        limits.append(f"Compose files listed but not read: {len(unread)}.")
    return limits


def _daemon_limit(c: Collector) -> str:
    """What the capture did about Docker and snap, whose sockets would start their daemons."""
    if c.ok("systemd", "units") is not None:
        return "Docker and snap were not asked when systemd listed their daemon as stopped (their sockets would " \
               "start it)"
    if c.systemd_booted:
        return "systemd's unit list was unavailable, so Docker and snap were not asked (their sockets could start " \
               "their daemons)"
    return "systemd is not this host's init, so nothing is socket-activated: Docker and snap were asked directly"


def _redaction_review(c: Collector) -> dict:
    probes = [f"{section}.{name}" for section in SECTIONS for name, record in c.raw[section].items()
              if record.get("redactions")]
    return {"policy": REDACTION_POLICY, "counts": dict(c.redactor.counts), "total": c.redactor.total,
            "probes_with_redactions": probes, "withheld": list(c.withheld),
            "retained": ["host, container, image, network, unit and package names", "IP addresses, ports and "
                         "routes", "file paths and mount points", "labels without a secret-looking name",
                         "user names in process lists, crontab file names and Tailscale's own status"],
            "limitation": "Pattern-based redaction is not proof. Read snapshot.json before you share it."}


def _summary(c: Collector, indexes: dict) -> dict:
    statuses: dict[str, int] = {}
    for section in SECTIONS:
        for record in c.raw[section].values():
            statuses[record["status"]] = statuses.get(record["status"], 0) + 1
    containers = indexes["containers"]
    by_state: dict[str, int] = {}
    for entry in containers.values():
        by_state[str(entry.get("state"))] = by_state.get(str(entry.get("state")), 0) + 1
    units = indexes["systemd_units"]
    scopes: dict[str, int] = {}
    for endpoint in indexes["endpoints"]:
        scopes[endpoint["scope"]] = scopes.get(endpoint["scope"], 0) + 1
    relations: dict[str, int] = {}
    for item in indexes["relationships"]:
        relations[item["relation"]] = relations.get(item["relation"], 0) + 1
    packages = indexes["host_packages"]
    gpus = c.ok("gpu", "query")
    backup_units = sorted(name for name in units if any(tool in name.lower() for tool in BACKUP_TOOLS))
    backup_tools = sorted(name for name, record in c.raw["backups"].items()
                          if record["status"] == "ok" and name != "snapraid_conf")
    compose = indexes["compose_files"]
    return {
        "probes": {"total": sum(statuses.values()), **dict(sorted(statuses.items()))},
        "gpus": len([line for line in gpus["stdout"].splitlines() if line.strip()]) if gpus else 0,
        "containers": {"total": len(containers), **dict(sorted(by_state.items()))},
        "images": len(c.parsed.get(("docker", "images")) or []),
        "networks": len(c.parsed.get(("docker", "networks")) or []),
        "volumes": len(c.parsed.get(("docker", "volumes")) or []),
        "systemd_units": {"total": len(units), "active": sum(1 for u in units.values() if u.get("active") == "active"),
                          "failed": sum(1 for u in units.values() if u.get("active") == "failed")},
        "host_packages": {"dpkg": sum(1 for p in packages.values() if p.get("source") == "dpkg"),
                          "snap": sum(1 for p in packages.values() if p.get("source") == "snap"),
                          "held": sum(1 for p in packages.values() if p.get("held"))},
        "compose_files": {"found": len(compose), "read": sum(1 for e in compose if e["read"]),
                          "listed_only": sum(1 for e in compose if not e["read"])},
        "endpoints": {"total": len(indexes["endpoints"]), **dict(sorted(scopes.items()))},
        "relationships": {"total": len(indexes["relationships"]), **dict(sorted(relations.items()))},
        "backup_tools": backup_tools, "backup_units": backup_units,
        "redactions": c.redactor.total,
        "computed_by": "collector",
    }


# ---------------------------------------------------------------- writing it out


def overview(snapshot: dict) -> str:
    """overview.md: a short human summary of the snapshot."""
    host, summary = snapshot.get("host") or {}, snapshot.get("summary") or {}
    probes, containers = summary.get("probes") or {}, summary.get("containers") or {}
    units, packages = summary.get("systemd_units") or {}, summary.get("host_packages") or {}
    compose, endpoints = summary.get("compose_files") or {}, summary.get("endpoints") or {}

    def counts(data: dict, skip: tuple[str, ...] = ("total",)) -> str:
        return ", ".join(f"{value} {key}" for key, value in data.items() if key not in skip and value) or "none"

    scope = snapshot.get("scope") or {}
    lines = [
        f"# Server snapshot: {host.get('hostname') or 'unknown host'}",
        "",
        f"Collected {snapshot.get('collected_at')} by `{scope.get('collector')}`, read-only, "
        + ("with sudo." if scope.get("sudo") else "without sudo."),
        "Read snapshot.json before you share it: redaction is pattern-based, not proof.",
        "",
        "| What | Found |",
        "| ---- | ----- |",
        f"| OS | {host.get('os') or '?'}, kernel {host.get('kernel') or '?'}, {host.get('machine') or '?'} |",
        f"| GPUs | {summary.get('gpus', 0)} |",
        f"| Containers | {containers.get('total', 0)} ({counts(containers)}) |",
        f"| Images, networks, volumes | {summary.get('images', 0)}, {summary.get('networks', 0)}, "
        f"{summary.get('volumes', 0)} |",
        f"| systemd units | {units.get('total', 0)} ({units.get('active', 0)} active, {units.get('failed', 0)} "
        "failed) |",
        f"| Packages | {packages.get('dpkg', 0)} dpkg, {packages.get('snap', 0)} snap, {packages.get('held', 0)} "
        "held |",
        f"| Compose files | {compose.get('found', 0)} found, {compose.get('read', 0)} read, "
        f"{compose.get('listed_only', 0)} listed only |",
        f"| Listening endpoints | {endpoints.get('total', 0)} ({counts(endpoints)}) |",
        f"| Relationships | {(summary.get('relationships') or {}).get('total', 0)} |",
        f"| Backup tools, units | {', '.join(summary.get('backup_tools') or []) or 'none found'}; "
        f"{len(summary.get('backup_units') or [])} units |",
        f"| Probes | {probes.get('total', 0)} ({counts(probes)}) |",
        f"| Redactions | {summary.get('redactions', 0)} |",
        "",
        "## Not captured",
        "",
        *[f"- {limit}" for limit in snapshot.get("scope_limits") or []],
        "",
    ]
    return "\n".join(lines)


def write_snapshot(snapshot: dict, out_dir: Path) -> tuple[Path, Path]:
    """Write snapshot.json and overview.md into out_dir (created 0700; files 0600, replaced atomically)."""
    out_dir = Path(out_dir).expanduser()
    out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not out_dir.is_dir():
        raise NotADirectoryError(str(out_dir))
    paths = (out_dir / "snapshot.json", out_dir / "overview.md")
    for path, text in zip(paths, (json.dumps(snapshot, indent=1, ensure_ascii=False) + "\n", overview(snapshot))):
        fd, tmp = tempfile.mkstemp(dir=out_dir, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    return paths
