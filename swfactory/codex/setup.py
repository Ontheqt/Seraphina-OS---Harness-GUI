"""The owner's setup blocks for the Codex worker (BROWSER_AND_CODEX.md §3.11, §4.7 step 7; DESIGN §44).

The harness never makes these changes itself: users, groups, ACLs on the owner's folders, sudoers, nftables and files
under /etc are the owner's own, each reversible and recorded (§4.7 step 7). ``factory codex setup`` prints them in the
{id, title, commands, expect, then?, note?, if_not} shape of the other copy blocks (bin/sf-snapraid's setup), with
this install's paths filled in; ``--stage`` (block c0, the first) writes the files they install (the launcher, its
settings, the route's key, the sudoers rule, the nftables rule and the unit that loads it at boot) into
``<data_dir>/codex-setup/`` for the owner to read first. The owner's decisions they carry:

- O3: a separate Unix user for Codex (``ai-codex``), the worktree group with default ACLs, and one sudoers rule that
  lets the harness's user run only the launcher as that user;
- O9 (as the owner changed it): the nftables rule for the Codex user is on, supplied here as a block (with a unit
  that loads it at every boot) and never applied by the harness, which refuses a Codex run while it is not loaded
  (work.containment_blocker); the host-wide ``/etc/codex/requirements.toml`` is off, because the operator runs her own
  Codex on this host and from a Windows PC on the LAN and the harness never gates the operator's own tools. The
  per-job CODEX_HOME and the launcher hold the configuration instead, with the residual risk §3.11 names, which the
  owner accepted (DESIGN §44).

The pinned Codex comes from its release's standalone binary (the server has no Node or npm), checked against the
sha256 GitHub publishes for it, which the owner compares himself: the harness carries no checked copy of it.
"""

from __future__ import annotations

import getpass
import json
import os
import platform
import pwd
import shlex
import stat
import urllib.parse
from pathlib import Path
from typing import Any, Callable

from .settings import PROJECT_DIR, SERVER_KEY_FILE, SERVER_LAUNCH_SETTINGS, SERVER_LAUNCHER, settings, work_root

SUDOERS = "/etc/sudoers.d/seraphina-codex"
NFT_FILE = "/etc/seraphina/codex-egress.nft"
NFT_TABLE = "inet seraphina_codex"
EGRESS_UNIT = "seraphina-codex-egress.service"   # loads NFT_FILE at every boot (block c6)
EGRESS_UNIT_PATH = "/etc/systemd/system/" + EGRESS_UNIT
EGRESS_UNIT_LINK = "/etc/systemd/system/sysinit.target.wants/" + EGRESS_UNIT   # what `systemctl enable` makes
NFT = "/usr/sbin/nft"
DEFAULT_USER = "ai-codex"
DEFAULT_GROUP = "seraphina-work"
CODEX_DIR = "/usr/local/lib/seraphina-codex"
DEFAULT_BINARY = CODEX_DIR + "/codex"   # where block c7 installs the pinned Codex; the staged settings' default
RELEASES = "https://github.com/openai/codex/releases"
SUDO = "/usr/bin/sudo"
# Each sudo program's own visudo (sudo-rs's under /usr/lib/cargo, classic sudo's): they read the same sudoers files
# but do not always parse them alike, so block c5 checks the rule with every one installed.
VISUDOS = ("/usr/lib/cargo/bin/visudo", "/usr/sbin/visudo", "/usr/sbin/visudo.ws")
LAUNCHER_SOURCE = Path(__file__).resolve().parent / "launch.py"
RESIDUAL_RISK = ("No /etc/codex/requirements.toml (the owner's O9): a Codex process that ignored its own "
                 "configuration (a bug, or a behaviour change at an upgrade) would be held only by the Codex user's "
                 "file permissions and the uid egress rule. Every upgrade of the pin runs the containment probes "
                 "(T19) again before use. Accepted by the owner.")


def names(config: Any) -> dict:
    merged = settings(config)
    return {"user": merged.get("user") or DEFAULT_USER, "group": merged.get("group") or DEFAULT_GROUP,
            "harness": _harness_user()}


def _harness_user() -> str:
    """The user this process runs as (its effective uid, never $LOGNAME, which sudo sets to the target user)."""
    try:
        return pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:
        try:
            return getpass.getuser()
        except (KeyError, OSError):
            return "<the harness's user>"


def invocation(config: Any, user: str | None = None) -> str:
    """This install's own `factory`, as the harness's user (user: another) with its own configuration: the server's
    `factory` is not on PATH, its user logs in nowhere, and a run as root (sudo env ... factory) would stage for
    root."""
    env = [f"HOME={shlex.quote(os.path.expanduser('~'))}", f"FACTORY_CONFIG={shlex.quote(str(config.path))}"]
    if os.environ.get("FACTORY_PYTHON"):
        env.append(f"FACTORY_PYTHON={shlex.quote(os.environ['FACTORY_PYTHON'])}")
    return (f"sudo -u {shlex.quote(user or names(config)['harness'])} env {' '.join(env)} "
            f"{shlex.quote(str(PROJECT_DIR / 'bin' / 'factory'))}")


def model_ports(config: Any) -> list[tuple[str, int]]:
    """(host, port) of the loopback provider of each codex-worker route (checks.codex_routes): the egress rule allows
    only these, and the doctor's egress probe tries every other configured loopback port. A provider no Codex route
    uses is never allowed; with no Codex route at all the list is empty and the rule drops everything (fail closed)."""
    from . import launch as L
    from .checks import codex_routes

    out = []
    for _, model in codex_routes(config):
        base_url = str(getattr(model.provider, "base_url", "") or "").rstrip("/")
        parts = urllib.parse.urlsplit(base_url)
        if L._loopback(base_url) and parts.port:
            out.append((parts.hostname or "127.0.0.1", parts.port))
    return sorted(set(out))


def sudo_implementation(realpath: Callable[[str], str] = os.path.realpath) -> str:
    """sudo-rs or classic sudo: the one /usr/bin/sudo resolves to, which is the one the harness runs (Ubuntu 25.10
    and later ship sudo-rs as sudo, under /usr/lib/cargo, and keep classic sudo as sudo.ws; bin/sf-system decides the
    same way)."""
    real = realpath(SUDO)
    return "sudo-rs" if "/cargo/" in real or os.path.basename(real).startswith("sudo-rs") else "sudo"


def visudo_paths(exists: Callable[[str], bool] = os.path.exists,
                 realpath: Callable[[str], str] = os.path.realpath) -> list[str]:
    """Every installed visudo by its absolute path, one per program (two names for one binary count once)."""
    out, seen = [], set()
    for path in VISUDOS:
        if exists(path) and realpath(path) not in seen:
            seen.add(realpath(path))
            out.append(path)
    return out or [VISUDOS[1]]


def visudo_of(implementation: str, exists: Callable[[str], bool] = os.path.exists) -> str:
    """The visudo of the sudo program the harness runs (the one that must accept the rule before it is moved)."""
    if implementation == "sudo-rs" and exists(VISUDOS[0]):
        return VISUDOS[0]
    return VISUDOS[1]


def sudoers_text(harness_user: str, codex_user: str, launcher: str = SERVER_LAUNCHER,
                 implementation: str = "sudo") -> str:
    """The one sudoers rule: the harness's user may run the launcher, and nothing else, as the Codex user. The
    launcher checks its own arguments against fixed patterns. Written for the sudo the harness runs (sudo-rs: no
    Defaults line) and checked with every installed visudo (§3.11)."""
    lines = ["# Seraphina OS Harness: the Codex launcher as the Codex user, and nothing else (DESIGN §44; "
             "docs/seraphina/BROWSER_AND_CODEX.md §3.11)"]
    if implementation != "sudo-rs":
        lines.append(f"Defaults!{launcher} !use_pty")
    lines.append(f"{harness_user} ALL=({codex_user}) NOPASSWD: {launcher}")
    return "\n".join(lines) + "\n"


def nft_text(codex_user: str, ports: list[tuple[str, int]]) -> str:
    """The uid egress rule (§3.11 item 4): the Codex user's own process may reach only the local model's loopback
    port(s); everything else it sends is dropped. Unix sockets are not affected (the Codex user is in no group that
    reaches one). The file replaces its table whole, in one transaction (`nft -f` applies a file atomically): the
    empty declaration makes sure the table exists, so the delete never fails on a fresh host, and a load over an
    older rule (block c6 again after the provider changed, or the boot unit) never appends to the rules there."""
    allow = []
    for host, port in ports:
        family = "ip6" if ":" in host else "ip"
        address = "::1" if family == "ip6" else "127.0.0.1"
        allow.append(f"    meta skuid \"{codex_user}\" {family} daddr {address} tcp dport {port} accept")
    return ("# Seraphina OS Harness: the Codex user reaches the local model only (DESIGN §44; the owner's O9)\n"
            f"table {NFT_TABLE}\ndelete table {NFT_TABLE}\n"
            f"table {NFT_TABLE} {{\n  chain output {{\n    type filter hook output priority 0; policy accept;\n"
            + "\n".join(allow) + ("\n" if allow else "")
            + f"    meta skuid \"{codex_user}\" counter drop\n  }}\n}}\n")


def egress_unit_text() -> str:
    """The oneshot unit that loads the egress rule at every boot, before the network comes up (block c6). No
    ExecStop: stopping it never opens the Codex user's traffic, and it never flushes anyone else's rules."""
    return (f"# Seraphina OS Harness: the Codex user's egress rule ({NFT_FILE}) at every boot, before the network\n"
            "# (DESIGN §44; the owner's O9). Installed by block c6 of `factory codex setup`; the file replaces its\n"
            "# own table, so a reload never appends to it. Undo: systemctl disable it, remove this file and the rule.\n"
            "[Unit]\n"
            "Description=Seraphina OS Harness: the Codex user's egress rule\n"
            "DefaultDependencies=no\n"
            "Wants=network-pre.target\n"
            "Before=network-pre.target shutdown.target\n"
            "Conflicts=shutdown.target\n"
            f"RequiresMountsFor={os.path.dirname(NFT_FILE)}\n"
            "\n[Service]\n"
            "Type=oneshot\n"
            "RemainAfterExit=yes\n"
            f"ExecStart={NFT} -f {NFT_FILE}\n"
            "\n[Install]\n"
            "WantedBy=sysinit.target\n")


def staged_dir(config: Any) -> Path:
    return Path(config.data_dir) / "codex-setup"


def server_key_file(config: Any) -> str:
    """The route's key file the server's launcher reads, as the Codex user: codex.key_file, or SERVER_KEY_FILE."""
    chosen = settings(config).get("key_file")
    return os.path.expanduser(str(chosen)) if chosen else SERVER_KEY_FILE


def staged_settings(config: Any) -> dict:
    """The launcher's settings as the server needs them (block c4 installs them): codex.binary, or DEFAULT_BINARY
    where block c7 installs the pinned Codex; every provider's key from the route's key file of block c4."""
    from .work import launch_settings

    merged = settings(config)
    record = launch_settings(config)
    record["binary"] = os.path.expanduser(str(merged["binary"])) if merged.get("binary") else DEFAULT_BINARY
    record["providers"] = [{**entry, "key_file": server_key_file(config)} for entry in record["providers"]]
    return record


def key_values(config: Any) -> list[tuple[str, str | None]]:
    """(name, the harness's own value or None) of the key of each Codex route's provider, each name once."""
    from .checks import codex_routes

    out: list[tuple[str, str | None]] = []
    for _, model in codex_routes(config):
        name = getattr(model.provider, "api_key_env", None)
        if not name or name in [seen for seen, _ in out]:
            continue
        value = model.provider.api_key()
        out.append((str(name), value if value and "\n" not in value else None))
    return out


def missing_keys(config: Any) -> list[str]:
    """The names of the Codex routes' keys the harness has no value for (key_text writes each as a comment)."""
    return [name for name, value in key_values(config) if value is None]


def key_text(config: Any) -> str:
    """The route's key file (NAME=value lines) from the harness's own key for each Codex route's provider; a key the
    harness has no value for is a comment saying where to write it (the installed file, never this staged copy in
    the harness's data folder)."""
    user, key = names(config)["user"], server_key_file(config)
    lines = [f"# The Codex route's key, read by sf-codex-launch as the Codex user (DESIGN §44). Block c4 installs it "
             f"{user}:root 0400: only the Codex user (and root) reads it, never the harness's user or its group."]
    for name, value in key_values(config):
        lines.append(f"{name}={value}" if value is not None
                     else f"# {name}: the harness has no value for it; after block c4 write {name}=<the key> into "
                          f"{key} (sudo nano {key}; a keyless local server needs a placeholder value)")
    return "\n".join(lines) + "\n"


def stage(config: Any) -> Path:
    """Write the files the blocks install into <data_dir>/codex-setup/ (0700, each file 0600): the launcher's
    settings as the server needs them, the route's key file, the sudoers rule for the sudo the harness runs, the
    nftables rule, the unit that loads it at boot and a copy of the launcher. Nothing is installed. Refused
    (WorkError) when this process is not the data folder's owner, the harness's user (a run through sudo as root
    would write a sudoers rule for root and leave a folder the harness cannot replace)."""
    from .work import WorkError, _private_dirs, _replace_private

    data = Path(config.data_dir)
    try:
        owner = os.stat(data).st_uid
    except FileNotFoundError:
        owner = os.geteuid()
    if owner != os.geteuid():
        try:
            owner_name = pwd.getpwuid(owner).pw_name
        except KeyError:
            owner_name = str(owner)
        raise WorkError(f"stage as the harness's user {owner_name}, who owns {data}, not as {_harness_user()}: "
                        f"{invocation(config, owner_name)} codex setup --stage")
    who = names(config)
    folder = _private_dirs(staged_dir(config))
    record = staged_settings(config)
    _replace_private(folder / "codex-launch.json", (json.dumps(record, indent=2, sort_keys=True) + "\n").encode())
    _replace_private(folder / "codex-key.env", key_text(config).encode())
    _replace_private(folder / "seraphina-codex",
                     sudoers_text(who["harness"], who["user"], implementation=sudo_implementation()).encode())
    _replace_private(folder / "codex-egress.nft", nft_text(who["user"], model_ports(config)).encode())
    _replace_private(folder / EGRESS_UNIT, egress_unit_text().encode())
    _replace_private(folder / "sf-codex-launch", LAUNCHER_SOURCE.read_bytes())
    return folder


def closed_ancestors(config: Any) -> list[Path]:
    """The folders above the data folder (and above the work root when it lies outside it) that other users cannot
    pass through (no o+x, such as a 0750 home or a 0700 /var/lib/seraphina), from / down: the Codex user needs a
    traverse entry on each, or it never reaches the worktrees and its run folders."""
    data = Path(config.data_dir)
    targets = [data]
    root = work_root(config)
    try:
        root.relative_to(data)
    except ValueError:
        targets.append(root)
    out: list[Path] = []
    for target in targets:
        for parent in reversed(target.parents):
            if parent == Path(parent.anchor) or parent in out:
                continue
            try:
                mode = os.stat(parent).st_mode
            except OSError:
                continue
            if not mode & stat.S_IXOTH:
                out.append(parent)
    return out


def exposed_below(config: Any) -> list[str]:
    """What block c2's traverse entries on closed_ancestors open to the Codex user: each entry directly below such a
    folder that other users may read or enter (o+r or o+x; a symlink is left out), but the one on the way to the
    data folder or the work root. The closed folder hid it; with the entry the Codex user opens it by its path (in a
    0750 home: other repositories' checkouts, personal folders, dotfiles)."""
    targets = [Path(config.data_dir), work_root(config)]
    out: list[str] = []
    for parent in closed_ancestors(config):
        on_the_way = {target.relative_to(parent).parts[0] for target in targets if parent in target.parents}
        try:
            entries = sorted(os.listdir(parent))
        except OSError:
            continue
        for name in entries:
            path = parent / name
            try:
                info = os.lstat(path)
            except OSError:
                continue
            if name not in on_the_way and not stat.S_ISLNK(info.st_mode) \
                    and info.st_mode & (stat.S_IROTH | stat.S_IXOTH):
                out.append(str(path))
    return out


def codex_asset() -> str:
    """The release's standalone binary for this machine (x86_64 or aarch64, musl, as the npm package ships it)."""
    machine = platform.machine().lower()
    arch = "aarch64" if machine in ("aarch64", "arm64") else "x86_64"
    return f"codex-{arch}-unknown-linux-musl"


def blocks(config: Any) -> list[dict]:
    """The owner's blocks, in order (§4.7 step 7). Each is his own decision and action; the doctor checks after."""
    who = names(config)
    user, group, harness = who["user"], who["group"], who["harness"]
    pin = settings(config)["pin"]
    data = shlex.quote(str(Path(config.data_dir)))
    root = shlex.quote(str(work_root(config)))
    staged = shlex.quote(str(staged_dir(config)))
    launcher = SERVER_LAUNCHER
    key = shlex.quote(server_key_file(config))
    closed = closed_ancestors(config)
    above = " ".join(shlex.quote(str(p)) for p in closed)
    exposed = exposed_below(config)
    home = Path(os.path.expanduser("~"))
    invoke = invocation(config)
    missing = missing_keys(config)
    implementation = sudo_implementation()
    visudos = visudo_paths()
    asset = codex_asset()
    download = CODEX_DIR + "/download"
    return [
        {"id": "c0", "title": "Stage the files the blocks install",
         "commands": [f"{invoke} codex setup --stage"],
         "expect": f"it names {staged} and prints these blocks again; read the files there before any block installs "
                   f"them",
         "note": f"Run it as printed: as the harness's user ({harness}), with this install's own factory and "
                 f"configuration (it refuses another user, whose sudoers rule and staged folder would be wrong). "
                 f"Every later block installs from that folder: stage again whenever codex.binary, codex.key_file or "
                 f"a Codex route changes, and repeat the blocks that install what changed (c3 to c6); repeat c4 "
                 f"after every stage, since its last step removes the staged copy of the route's key.",
         "if_not": f"sudo rm -r {staged}"},
        {"id": "c1", "title": "Create the Codex user and the worktree group (O3)",
         "commands": [f"sudo groupadd --system {group}",
                      f"sudo useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "
                      f"--gid {group} {user}",
                      f"sudo usermod -aG {group} {harness}"],
         "expect": f"nothing printed; `id {user}` names the group {group}",
         "note": "The Codex user runs Codex and nothing else; it is never the harness's user. Log the harness out "
                 "and in again (or restart its service) so it is in the group.",
         "if_not": f"sudo userdel {user}; sudo groupdel {group}"},
        {"id": "c2", "title": "Share the worktrees through the group, with default ACLs",
         "commands": ([f"sudo setfacl -m g:{group}:x {above}"] if above else [])
         + [f"sudo -u {harness} mkdir -p -m 0700 {data}/jobs {data}/worktrees",
            f"sudo setfacl -m g:{group}:x {data} {data}/jobs {data}/worktrees",
            f"sudo setfacl -R -m g:{group}:rwX -m d:g:{group}:rwX {data}/worktrees",
            f"sudo install -d -o {harness} -g {group} -m 2750 {root} {root}/runs",
            f"sudo setfacl -m d:g:{group}:rwX {root}/runs",
            f"sudo install -d -o {user} -g {group} -m 2750 {root}/homes"],
         "expect": "nothing printed",
         "note": (_exposure_note(closed, exposed, home) if closed else "")
                 + "The Codex user can then pass through the data folder without listing or reading anything there "
                 "(a traverse entry only: everything in it is the harness's alone). It writes only in the "
                 "worktrees and in the out/, tmp/ and shell/ folders the harness makes for each run (the folders "
                 "above them stay the harness's: 2750), and keeps its homes. Everything else in the data folder the "
                 "harness creates private (umask 077; at its first start after an upgrade it takes the group and "
                 "other bits off what older versions left there). It gives the group read access to a job's own "
                 "folder only while a Codex run has it, and takes it back when the run ends (for a run the harness "
                 "was killed during: at its next start, and again when the job ends). getfacl shows what was set.",
         "if_not": (f"sudo setfacl -x g:{group} {above}; " if above else "")
         + f"sudo setfacl -x g:{group} {data} {data}/jobs; "
         f"sudo setfacl -R -x g:{group} -x d:g:{group} {data}/worktrees {data}/jobs; "
         f"sudo rmdir {root}/homes {root}/runs {root}   # rmdir keeps a folder that still holds files"},
        {"id": "c3", "title": "Install the launcher as root",
         "commands": ["sudo install -d -m 0755 -o root -g root /usr/local/libexec/seraphina",
                      f"sudo install -m 0755 -o root -g root {staged}/sf-codex-launch {launcher}.new",
                      f"sha256sum {launcher}.new"],
         "expect": "the sha256 the doctor's codex launcher line names",
         "then": [f"sudo mv {launcher}.new {launcher}"],
         "if_not": f"sudo rm {launcher}.new"},
        {"id": "c4", "title": "Install the launcher's settings and the Codex route's key",
         "commands": ["sudo install -d -m 0755 -o root -g root /etc/seraphina",
                      f"sudo install -m 0644 -o root -g root {staged}/codex-launch.json {SERVER_LAUNCH_SETTINGS}.new",
                      f"sudo install -m 0400 -o {user} -g root {staged}/codex-key.env {key}.new"],
         "expect": "nothing printed; read both files first: the settings name the pinned binary, the worktrees, the "
                   "work root, the local model's loopback address and the key file; the key file holds only the "
                   "Codex route's key (NAME=value)",
         "then": [f"sudo mv {SERVER_LAUNCH_SETTINGS}.new {SERVER_LAUNCH_SETTINGS}", f"sudo mv {key}.new {key}",
                  f"sudo rm {staged}/codex-key.env"],
         "note": "The settings hold no secret: only where the key file is. Under sudo the launcher reads only this "
                 f"root-owned settings file, and it reads the key file as the Codex user, the only user (with root) "
                 f"who can: {user}:root 0400, not the group {group}, which the harness's user is in, so neither the "
                 f"harness nor any agent it runs can read it (the harness never needs to: the doctor's codex key "
                 f"line asks the launcher, as the Codex user, whether it defines the key), and the permissions "
                 f"profile denies it to every command Codex runs. The provider's own key file stays the harness's. "
                 + (f"The harness has no value for {', '.join(missing)}: after the move, write "
                    f"{', '.join(name + '=<the key>' for name in missing)} into {key} yourself (sudo nano {key}; a "
                    f"keyless local server needs a placeholder value), never into the staged copy in the harness's "
                    f"data folder. " if missing else "")
                 + "After a change to codex.binary, codex.key_file or a Codex route, stage again (c0) and repeat "
                 "this block.",
         "if_not": f"sudo rm -f {SERVER_LAUNCH_SETTINGS}.new {key}.new"},
        {"id": "c5", "title": "Allow the harness to run the launcher as the Codex user, and nothing else",
         "commands": [f"sudo install -m 0440 -o root -g root {staged}/seraphina-codex {SUDOERS}.new"]
         + [f"sudo {visudo} -cf {SUDOERS}.new" for visudo in visudos],
         "expect": "parsed OK from each visudo above (one per sudo program installed here)",
         "then": [f"sudo {visudo_of(implementation)} -cf {SUDOERS}.new && sudo mv {SUDOERS}.new {SUDOERS}"],
         "note": "Until the move, sudo ignores the file (its name has a dot). The rule is written for "
                 + ("sudo-rs, the sudo program " + SUDO + " is here (so it has no Defaults line)"
                    if implementation == "sudo-rs" else "classic sudo, the sudo program " + SUDO + " is here")
                 + ". The harness's unit must not set NoNewPrivileges, or sudo cannot run under it.",
         "if_not": f"sudo rm {SUDOERS}.new"},
        {"id": "c6", "title": "Keep the Codex user's traffic on the local model, at every boot too (O9: on)",
         "commands": [f"sudo install -m 0644 -o root -g root {staged}/codex-egress.nft {NFT_FILE}",
                      f"sudo nft -c -f {NFT_FILE}", f"sudo nft -f {NFT_FILE}",
                      f"sudo install -m 0644 -o root -g root {staged}/{EGRESS_UNIT} {EGRESS_UNIT_PATH}",
                      "sudo systemctl daemon-reload", f"sudo systemctl enable {EGRESS_UNIT}"],
         "expect": f"nothing printed but systemctl's line that it created {EGRESS_UNIT_LINK}; the doctor's codex "
                   f"egress line then passes",
         "note": "The rule allows only the loopback port of each Codex route's provider: route the builder to "
                 "codex-worker before staging (with no Codex route it drops everything). The file replaces its "
                 "table whole, so when that provider changes, stage again and repeat this block: the old port closes "
                 f"and the new one opens. A rule loaded by nft lasts until the next boot; the unit {EGRESS_UNIT} "
                 "loads the same file at every boot, before the network comes up. Do not enable nftables.service "
                 "for it instead: Ubuntu's /etc/nftables.conf starts with `flush ruleset`, which would take Docker's "
                 "and Tailscale's rules with it. The harness only checks the rule; it never loads it: the doctor's "
                 "codex egress line (a connect from the Codex user to a public address and to every other configured "
                 "loopback port must fail, and the unit must be enabled), and before every Codex run a loopback "
                 "connect from the Codex user to a port the rule does not allow must be dropped, or the run does not "
                 "start. The host-wide /etc/codex/requirements.toml is not installed (O9): the operator's own Codex "
                 "on this host is never gated by the harness.",
         "if_not": f"sudo systemctl disable {EGRESS_UNIT}; sudo rm -f {EGRESS_UNIT_PATH} {NFT_FILE}; "
                   f"sudo systemctl daemon-reload; sudo nft delete table {NFT_TABLE}"},
        {"id": "c7", "title": "Install the pinned Codex for the Codex user (its standalone binary, no Node or npm)",
         "commands": [f"sudo install -d -m 0755 -o root -g root {CODEX_DIR}",
                      f"sudo install -d -m 0700 -o root -g root {download}",
                      f"sudo curl -fsSL --proto '=https' -o {download}/{asset}.tar.gz "
                      f"{RELEASES}/download/rust-v{pin}/{asset}.tar.gz",
                      f"sudo sha256sum {download}/{asset}.tar.gz"],
         "expect": f"a sha256 equal to the one GitHub shows for {asset}.tar.gz on the release page "
                   f"{RELEASES}/tag/rust-v{pin}: compare the two yourself. The harness carries no checked copy of "
                   f"that checksum, so your comparison is the download's only check",
         "then": [f"sudo tar -xzf {download}/{asset}.tar.gz -C {download}",
                  f"sudo install -m 0755 -o root -g root {download}/{asset} {DEFAULT_BINARY}",
                  f"sudo rm -r {download}"],
         "note": f"Set codex.binary to {DEFAULT_BINARY}, the path the staged settings name while it is unset; if it "
                 f"named another path when you staged, stage again (c0) and repeat c4. The release page lists the "
                 f"asset for each architecture ({asset} is this machine's). No sign-in and no hosted model (O4): the "
                 f"route's provider is the local model.",
         "if_not": f"sudo rm -r {CODEX_DIR}"},
        {"id": "c8", "title": "Check", "commands": [f"{invoke} doctor"], "action": "check",
         "expect": "the codex lines pass, except codex model until Strata's Responses route has passed its check in "
                   "a swap window the owner schedules (factory doctor --probe, under a custody request lease)",
         "note": "Or the GUI's Doctor, which runs inside the service with its own credentials.",
         "if_not": "Each line says what to fix."},
    ]


def _exposure_note(closed: list[Path], exposed: list[str], home: Path) -> str:
    """Block c2's words for its traverse entries above the data folder, in plain terms (what they open)."""
    where = ", ".join(str(p) for p in closed)
    homes = [str(p) for p in closed if p == home]
    shown = ", ".join(exposed[:8]) + (f" and {len(exposed) - 8} more" if len(exposed) > 8 else "")
    return (f"The first line lets the Codex user through the folders above the data folder that other users cannot "
            f"enter ({where}" + (f"; among them the harness user's home, {homes[0]}" if homes else "")
            + "): a traverse entry lists nothing, but it lets the Codex user open, by its path, anything below them "
            "that other users may read. "
            + (f"Below them that is now: {shown}. Make those private (chmod o-rwx), or move data_dir and "
               "codex.work_root out of these folders (for example under /srv/seraphina, a folder nothing else "
               "shares) and stage again; " if exposed else
               "Nothing below them is readable by other users now; what is made there later with other users' read "
               "bits would be, so a data_dir and codex.work_root of their own (for example under /srv/seraphina) are "
               "safer; ")
            + "the doctor's codex reads line checks what is directly below them (each Codex job's profile also "
            "denies every other repository's checkout). ")


def text(config: Any) -> str:
    """The blocks as a terminal shows them."""
    out = ["Codex worker setup (DESIGN §44). Nothing here was applied: each block is the owner's to paste.", ""]
    for block in blocks(config):
        out.append(f"[{block['id']}] {block['title']}")
        out += [f"    {line}" for line in block.get("commands") or []]
        if block.get("then"):
            out.append("  then, only when that matched:")
            out += [f"    {line}" for line in block["then"]]
        out.append(f"  expect: {block['expect']}")
        if block.get("note"):
            out.append(f"  note: {block['note']}")
        out.append(f"  undo: {block['if_not']}")
        out.append("")
    out.append("Residual risk, accepted by the owner: " + RESIDUAL_RISK)
    return "\n".join(out) + "\n"
