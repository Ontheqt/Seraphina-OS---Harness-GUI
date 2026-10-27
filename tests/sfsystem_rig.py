"""A temporary server for the Your server tests (docs/seraphina/YOUR_SERVER.md §12.2): bin/sf-system loaded by path, its
fixed locations moved into a temporary tree (Paths.under), the tools' outputs faked (FakeSystem, answering from
tests/fixtures/sfsystem/), root ownership faked (FakeLstat), and a machine built in code: Docker 29 state files for a
set of placeholder apps (a Dockge stack with a Tailscale container that reaches its app by name, one that shares the
app's network, two projects from one folder, a login gateway on the host network with its routes, Beszel, Dockge in a
home folder, a compose file kept directly in a home folder, a container started by hand, a Tailscale container in a
stack of its own serving two apps, a model server on the GPU, a renamed predecessor, a one-off container, a job run,
an unreadable file), their cgroup folders and /proc entries, Tailscale state files with planted key material and
nginx configs with planted secrets. Every name, address and path is a placeholder.

The harness side reaches the helper through runner(): the real main() of bin/sf-system, run in this process, as a
`sudo -n` call would (the request on standard input, SUDO_UID set)."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HELPER_FILE = ROOT / "bin" / "sf-system"
FIXTURES = ROOT / "tests" / "fixtures" / "sfsystem"
ACCESS = FIXTURES / "access"
SERAPHINA, OWNER, KATE, DOCKERPAL = 991, 1000, 1001, 992
PLANTED = ("PLANTED", "tskey-", "privkey:", "nodekey:")


def load_helper():
    """bin/sf-system as a module (it has no .py name and is never imported by the harness)."""
    loader = importlib.machinery.SourceFileLoader("sf_system", str(HELPER_FILE))
    spec = importlib.util.spec_from_loader("sf_system", loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    loader.exec_module(module)
    return module


sx = load_helper()


def cid(n: int) -> str:
    return f"{n:02x}" * 32


IDS = {name: cid(i) for i, name in enumerate((
    "photos", "photos-db", "photos-ts", "media", "media-ts", "mediaold-web", "jellyfin-2023", "gateway-nginx",
    "gateway-dex", "gateway-db", "gateway-oauth", "beszel-hub", "beszel-agent", "dockge", "notes", "filebrowser",
    "photos-run", "job-run", "shared-ts", "ollama", "photos-old", "broken"), start=0x20)}
IDS["photos"] = "1" * 64        # the recorded-shape fixture (tests/fixtures/sfsystem/docker29)


class FakeLstat:
    """os.lstat with every path owned by root and writable by nobody else, except the ones a test spoils."""

    def __init__(self) -> None:
        self.spoiled: dict[str, tuple[int, int]] = {}

    def __call__(self, path: str):
        real = os.lstat(path)
        uid, extra = self.spoiled.get(path, (0, 0))
        return types.SimpleNamespace(st_uid=uid, st_mode=(real.st_mode & ~0o022) | extra)


class FakeSystem(sx.System):
    """The tools' outputs from the fixtures; the calls recorded (argv, env, whether standard error was asked for), and
    the Docker API calls (method, path) answered by the rig."""

    def __init__(self, paths, rig: "Rig") -> None:
        super().__init__(paths)
        self.rig = rig
        self.calls: list[dict] = []
        self.docker_calls: list[tuple[str, str]] = []
        self.root = 0
        self.terminal = True
        self.answers: list[str] = []
        self.said: list[str] = []

    def docker(self, method, path, timeout, sock):
        self.docker_calls.append((method, path))
        return self.rig.docker_answer(method, path)

    def run(self, argv, timeout, env=None, stderr=False):
        self.calls.append({"argv": list(argv), "env": dict(env or {}), "stderr": stderr})
        tool = os.path.basename(argv[0])
        rig = self.rig
        args = list(argv[1:])
        if tool == "systemctl":
            if args[:2] == ["is-active", "docker.service"]:
                return 0 if rig.docker_active else 3, "active\n" if rig.docker_active else "inactive\n", False
            if args[:1] == ["list-sockets"]:
                return 0, (FIXTURES / "list-sockets.json").read_text(), False
            if args[:2] == ["list-units", "--type=service"]:
                return 0, (ACCESS / "systemctl-services.json").read_text(), False
            if args[:3] == ["show", "-p", "LoadState"]:
                return 0, "loaded\n" if args[-1] in rig.loaded_units else "not-found\n", False
            if args[:1] == ["show"]:
                return 0, (ACCESS / "systemctl-show.txt").read_text(), False
            if args[:1] == ["list-unit-files"]:
                return 0, "cockpit.socket enabled enabled\n", False
        if tool == "ss":
            if args == ["-Hxp"]:
                return 0, (ACCESS / "ss-x.txt").read_text(), False
            name = "ss-tcp.txt" if args[0] == "-Htlnp" else "ss-udp.txt"
            return 0, (FIXTURES / name).read_text(), False
        if tool == "lsblk":
            if "-d" in args:
                return 0, (FIXTURES / "smart" / "lsblk-disks.json").read_text(), False
            return 0, (FIXTURES / "lsblk.json").read_text(), False
        if tool == "nvidia-smi":
            if rig.gpu_exit:
                return rig.gpu_exit, "", False
            return 0, (FIXTURES / "nvidia-compute.csv").read_text(), False
        if tool == "docker-compose":
            folder = argv[argv.index("--project-directory") + 1]
            if rig.compose_fail:
                return 1, "", False
            if "--hash" in args:
                if rig.compose_bad_line:
                    return 0, "photos aaaa\n", False
                digest = ("b" if folder in rig.compose_changed else "a") * 64
                return 0, "".join(f"{svc} {digest}\n" for svc in rig.compose_services(folder)), False
            if "--images" in args:
                return 0, "".join(f"{i}\n" for i in rig.compose_images.get(folder) or rig.folder_images(folder)), False
            name = rig.compose_names.get(folder, os.path.basename(folder))
            rendered = {"name": name, "services": {"app": {"environment": {"DB_PASSWORD": "PLANTED-COMPOSE-ENV"}}}}
            return 0, json.dumps(rendered) if not rig.compose_garbage else "name: not json", False
        if tool in ("visudo",):
            return rig.visudo_exit, "parsed OK\n", False
        if tool in ("sudo", "sudo.ws"):
            program = "sudo.ws" if tool == "sudo.ws" else "sudo-rs" if "/cargo/" in os.path.realpath(argv[0]) else "sudo"
            if args == ["--version"]:
                return 0, rig.sudo_version.get(program, "Sudo version 1.9.15p5\n"), False
            if args[:2] in (["-l", "-U"], ["-ll", "-U"]):
                user = args[2]
                table = rig.sudo_ll if args[0] == "-ll" else rig.sudo_l
                name = (table.get(program) or {}).get(user)
                text = (ACCESS / name).read_text() if name else f"User {user} is not allowed to run sudo on server.\n"
                return 0, text, False
        if tool == "passwd" and args == ["-S", "root"]:
            return 0, (ACCESS / "passwd-S-root.txt").read_text(), False
        if tool == "sshd" and args == ["-T"]:
            return 0, (ACCESS / "sshd-T.txt").read_text(), False
        if tool == "find":
            base = args[0].rstrip("/")
            return 0, "".join(base + line + "\n" for line in (ACCESS / "find-setuid.txt").read_text().split()), False
        if tool == "dpkg":
            if args[0] == "-S":
                package = rig.packages.get(args[1])
                return (0, f"{package}: {args[1]}\n", False) if package else \
                    (1, "dpkg-query: no path found matching pattern\n", False)
            if args[0] == "-V":
                return 0, rig.dpkg_changed.get(args[1], ""), False
        if tool == "getcap":
            base = args[1].rstrip("/")
            return 0, "".join(base + line + "\n" for line in (ACCESS / "getcap.txt").read_text().split("\n") if line),\
                False
        if tool == "cvtsudoers":
            return 0, (ACCESS / "cvtsudoers.json").read_text(), False
        if tool == "smartctl":
            disk = os.path.basename(argv[-1])
            path = FIXTURES / "smart" / f"{rig.smart_files.get(disk, disk)}.json"
            if not path.exists():
                return 2, "", False
            text = path.read_text()
            return json.loads(text)["smartctl"]["exit_status"], text, False
        if tool == "needrestart":
            return 0, (FIXTURES / "checks" / "needrestart.txt").read_text(), False
        if tool == "journalctl":
            if "--disk-usage" in args:
                return 0, (FIXTURES / "checks" / rig.journal_usage).read_text(), False
            return 0, "".join(json.dumps(line) + "\n" for line in rig.journal_lines), False
        if tool == "nice":
            target = args[-1]
            text = "".join(f"{size}\t{target.rstrip('/')}{path}\n" for size, path in (
                line.split("\t") for line in (FIXTURES / "checks" / "du.txt").read_text().split("\n") if line))
            return 0, text, False
        if tool == "apt-get" and args == ["update"]:
            return rig.apt_exit, (FIXTURES / "checks" / "apt-update.txt").read_text(), False
        if tool == "systemd-run":
            rig.units.append(list(argv))
            return rig.systemd_run_exit, "", False
        return None, "", False

    def euid(self):
        return self.root

    def tty(self):
        return self.terminal

    def ask(self, prompt):
        self.said.append(prompt)
        return self.answers.pop(0) if self.answers else ""

    def chown_root(self, path):
        return None


def b64(value) -> str:
    data = value if isinstance(value, bytes) else (value.encode() if isinstance(value, str) else
                                                    json.dumps(value).encode())
    return base64.b64encode(data).decode()


def ts_state(node: str, serve: dict | None, tailnet: str = "example.ts.net") -> str:
    """A tailscaled.state: the three keys the helper decodes, and planted key material it must never return."""
    state = {"_current-profile": b64("a1b2"),
             "_profiles": b64({"a1b2": {"ID": "a1b2", "Name": "owner@example.com", "NodeID": "nPLANTEDNODE",
                                        "NetworkProfile": {"MagicDNSName": tailnet + ".", "DomainName": tailnet},
                                        "Key": "profile-a1b2"}}),
             "_machinekey": b64("privkey:PLANTED-MACHINE-KEY"),
             "profile-a1b2": b64({"Hostname": node, "Persist": {"PrivateNodeKey": "privkey:PLANTED-NODE-KEY"},
                                  "AuthKey": "tskey-PLANTED-PROFILE"}),
             "_daemon-config": b64("nodekey:PLANTED-DAEMON")}
    if serve is not None:
        state["_serve/a1b2"] = b64(serve)
    return json.dumps(state)


def web(host: str, port: int, proxy: str, path: str = "/") -> dict:
    return {"TCP": {str(port): {"HTTPS": True}}, "Web": {f"{host}:{port}": {"Handlers": {path: {"Proxy": proxy}}}}}


class Rig:
    """The temporary server. ask(verb, ...) runs one request through the helper's main(); runner is the HelperClient
    runner the harness side uses."""

    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sfsystem-")).resolve()
        self.paths = sx.Paths.under(self.tmp)
        self.lstat = FakeLstat()
        self.system = FakeSystem(self.paths, self)
        self.docker_active = True
        self.gpu_exit = 0
        self.compose_fail = False
        self.compose_garbage = False
        self.compose_names: dict[str, str] = {"/opt/stacks/media": "media"}
        self.visudo_exit = 0
        self.uid = SERAPHINA
        self.containers: dict[str, dict] = {}
        # the checks' and the root page's tools (tests change these)
        self.compose_changed: set[str] = set()
        self.compose_images: dict[str, list[str]] = {}
        self.compose_bad_line = False
        self.sudo_version = {"sudo": "Sudo version 1.9.15p5\n"}
        self.sudo_l = {"sudo": {"owner": "sudo-l-owner.txt", "backupsvc": "sudo-l-backupsvc.txt",
                                "seraphina": "sudo-l-seraphina.txt"}}
        self.sudo_ll = {"sudo": {"owner": "sudo-ll-owner.txt", "backupsvc": "sudo-ll-backupsvc.txt"}}
        self.packages = {"/usr/bin/su": "util-linux", "/usr/bin/passwd": "passwd", "/usr/bin/sudo": "sudo",
                         "/usr/bin/ping": "iputils-ping"}
        self.dpkg_changed: dict[str, str] = {}
        self.smart_files: dict[str, str] = {}
        self.journal_usage = "journal-usage.txt"
        self.journal_lines: list[dict] = []
        self.apt_exit = 0
        self.systemd_run_exit = 0
        self.units: list[list[str]] = []
        self.harness_calls: list[list[str]] = []
        self.simulate_extra: list[str] = []
        self.loaded_units = {"ssh.service", "cockpit.service", "example-indexer.service"}
        self.health: dict[str, str] = {"photos": "healthy", "notes": "unhealthy"}
        self.extra_images: list[dict] = []
        self.moved_tags: dict[str, str] = {}
        self.keep_tags: dict[str, list[str]] = {}
        self.build_cache = [{"ID": "c1" * 16, "Type": "regular", "InUse": False, "Shared": False, "Size": 3_000_000_000},
                            {"ID": "c2" * 16, "Type": "regular", "InUse": False, "Shared": False, "Size": 2_000_000_000},
                            {"ID": "c3" * 16, "Type": "regular", "InUse": True, "Shared": False, "Size": 1_000_000_000},
                            {"ID": "c4" * 16, "Type": "internal", "InUse": False, "Shared": False, "Size": 500_000},
                            {"ID": "c5" * 16, "Type": "regular", "InUse": False, "Shared": True, "Size": 700_000_000}]
        self._build()

    def cleanup(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- files ---------------------------------------------------------------------------------------------------

    def at(self, path: str) -> Path:
        return Path(self.paths.at(path))

    def write(self, path: str, data, mode: int = 0o644) -> Path:
        target = self.at(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, (dict, list)):
            data = json.dumps(data, indent=1)
        target.write_bytes(data.encode() if isinstance(data, str) else data)
        os.chmod(target, mode)
        return target

    def config(self, **changes) -> dict:
        cfg = {"v": 1, "allow": ["read"], "callers": {str(SERAPHINA): ["read"], str(KATE): ["read"],
                                                      str(DOCKERPAL): ["read"]},
               "owners": [OWNER], "stacks_dir": "/opt/stacks", "docker_api": True,
               "keep": {"images_per_app": 2, "max_gib": 50}, "smart_types": {}, **changes}
        self.write("/etc/seraphina/system-helper.json", cfg)
        return cfg

    def _build(self) -> None:
        self.write(sx.HELPER, HELPER_FILE.read_bytes(), 0o755)
        self.config()
        self.write("/etc/passwd", "root:x:0:0:root:/root:/bin/bash\n"
                                  f"seraphina:x:{SERAPHINA}:{SERAPHINA}:Seraphina:/var/lib/seraphina:/usr/sbin/nologin\n"
                                  f"dockerpal:x:{DOCKERPAL}:{DOCKERPAL}::/nonexistent:/usr/sbin/nologin\n"
                                  f"owner:x:{OWNER}:{OWNER}:Owner:/home/owner:/bin/bash\n"
                                  f"kate:x:{KATE}:{KATE}:Kate:/home/operator:/bin/bash\n")
        self.write("/etc/group", "root:x:0:\nsudo:x:27:owner\ndocker:x:998:owner,dockerpal\nlxd:x:997:owner\n"
                                 f"seraphina:x:{SERAPHINA}:\nowner:x:{OWNER}:\nkate:x:{KATE}:\ndockerpal:x:{DOCKERPAL}:\n")
        self.write("/etc/login.defs", "# placeholder\nUID_MIN 1000\nUID_MAX 60000\n")
        self.write("/proc/sys/kernel/random/boot_id", "00000000-0000-4000-8000-0000000000b1\n")
        self.write("/proc/sys/kernel/osrelease", "6.18.0-example\n")
        self.write("/proc/net/fib_trie", "Main:\n  +-- 0.0.0.0/0 3 0 5\n     |-- 192.0.2.10\n        /32 host LOCAL\n"
                                         "     |-- 172.17.0.1\n        /32 host LOCAL\n"
                                         "     |-- 127.0.0.1\n        /32 host LOCAL\nLocal:\n")
        self.write("/proc/self/mounts", "/dev/nvme0n1p2 / ext4 rw 0 0\noverlay /var/lib/docker/rootfs/overlayfs/x "
                                        "overlay rw 0 0\ntmpfs /run tmpfs rw 0 0\n")
        for tool in sx.TOOLS.values():
            if (not tool.endswith(("tailscale", "visudo", "sudo.ws", "/cargo/bin/sudo", "cvtsudoers")) or
                    tool.endswith("/sbin/visudo")):
                self.write(tool, "#!/bin/false\n", 0o755)
        self.write(sx.COMPOSE, "#!/bin/false\n", 0o755)
        self.write(sx.SUDO, "#!/bin/false\n", 0o4755)
        self.write("/run/seraphina/holds/system-J-1.json", {"id": "J-1", "holder": "backup", "why": "nightly backup",
                                                             "until": "2026-10-25T04:00:00Z", "max_delay_minutes": 30,
                                                             "resources": ["app:a-photos"]})
        self._apps()
        self._access()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.at("/run").mkdir(parents=True, exist_ok=True)
        sock.bind(str(self.at("/run/docker.sock")))
        sock.close()

    def _access(self) -> None:
        """The files the root page reads: more accounts (a service account with a NOPASSWD rule, an agent account),
        home folders with SSH keys, sudoers drop-ins (one with a dot, which sudo skips), polkit rules, cron, the unit
        files and the scripts root services run, a script in a person's home folder."""
        with open(self.at("/etc/passwd"), "a", encoding="utf-8") as handle:
            handle.write("backupsvc:x:995:995::/var/lib/backupsvc:/usr/sbin/nologin\n"
                         "agent:x:996:996::/nonexistent:/usr/sbin/nologin\n")
        with open(self.at("/etc/group"), "a", encoding="utf-8") as handle:
            handle.write("backupsvc:x:995:\nagent:x:996:\nshadow:x:42:kate\n")
        self.write("/home/owner/.ssh/authorized_keys", (ACCESS / "authorized_keys").read_text(), 0o600)
        self.write("/home/owner/.ssh/authorized_keys2", (ACCESS / "authorized_keys2").read_text(), 0o600)
        self.write("/root/.ssh/authorized_keys", (ACCESS / "authorized_keys").read_text().split("\n")[1] + "\n", 0o600)
        self.write("/home/operator/indexer/run.py", "print('placeholder')\n", 0o644)
        self.write("/etc/sudoers", "# placeholder: the rules come from the fixtures' sudo -l answers\n", 0o440)
        self.write("/etc/sudoers.d/90-backup", "# placeholder\n", 0o440)
        self.write("/etc/sudoers.d/old.rules", "# placeholder: a name with a dot, which sudo never reads\n", 0o440)
        self.write("/etc/nsswitch.conf", "passwd: files\ngroup: files\nsudoers: files\n")
        self.write("/usr/share/polkit-1/rules.d/49-ubuntu-admin.rules", "// placeholder\n")
        self.write("/etc/polkit-1/rules.d/60-example.rules", "// placeholder\n")
        self.write("/etc/crontab", "SHELL=/bin/sh\n17 * * * * root cd / && run-parts --report /etc/cron.hourly\n"
                                   "30 3 * * * root /opt/example/nightly.sh\n")
        self.write("/etc/cron.d/example", "*/5 * * * * owner /home/owner/bin/poll\n")
        self.write("/etc/cron.daily/example-report", "#!/bin/sh\n", 0o755)
        self.write("/opt/example/nightly.sh", "#!/bin/sh\n", 0o755)
        self.write("/opt/example/start.sh", "#!/bin/sh\n", 0o755)
        self.write("/opt/tools/bin/sync", "#!/bin/sh\n", 0o755)
        for unit in ("example-indexer", "example-shell", "example-agent", "example-writable"):
            self.write(f"/etc/systemd/system/{unit}.service", "[Service]\n")
        self.write("/run/systemd/system/example-indexer.service.d/50-refresh.conf", "[Service]\n")
        self.write("/usr/lib/systemd/system/ssh.service", "[Service]\n")
        self.write("/usr/local/bin/helper-suid", "#!/bin/false\n", 0o4755)
        # who else can change things: the owner owns a root service's script folder; kate's indexer is hers
        self.lstat.spoiled[str(self.at("/opt/example/start.sh"))] = (OWNER, 0)
        self.lstat.spoiled[str(self.at("/opt/tools"))] = (0, 0o020)
        self.lstat.spoiled[str(self.at("/home/operator"))] = (KATE, 0)
        self.lstat.spoiled[str(self.at("/home/operator/indexer"))] = (KATE, 0)
        self.lstat.spoiled[str(self.at("/home/operator/indexer/run.py"))] = (KATE, 0)
        self.lstat.spoiled[str(self.at("/home/owner"))] = (OWNER, 0)
        self.lstat.spoiled[str(self.at("/opt/stacks/photos"))] = (OWNER, 0)

    # -- the Docker API (answered from the state files the rig wrote) ------------------------------------------

    def _configs(self) -> list[dict]:
        out = []
        folder = self.at("/var/lib/docker/containers")
        for child in sorted(folder.iterdir()):
            with contextlib.suppress(OSError, ValueError):
                config = json.loads((child / "config.v2.json").read_text())
                if isinstance(config.get("Config"), dict) and config.get("Name"):
                    out.append(config)
        return out

    def images(self) -> list[dict]:
        """GET /images/json from the containers' images, the extra images a test adds, moved tags and kept names."""
        found: dict[str, dict] = {}
        for config in self._configs():
            image_id, ref = config.get("Image"), config["Config"].get("Image") or ""
            entry = found.setdefault(image_id, {"Id": image_id, "RepoTags": [], "RepoDigests": [], "Created":
                                                1760000000 + len(found), "Size": 400_000_000, "Labels": {}})
            if ref and not ref.startswith("sha256:") and "@" not in ref:
                tag = ref if ":" in ref.rsplit("/", 1)[-1] else ref + ":latest"
                if tag not in self.moved_tags and tag not in entry["RepoTags"]:
                    entry["RepoTags"].append(tag)
                repo = tag.rsplit(":", 1)[0]
                digest = f"{repo}@sha256:" + hashlib.sha256(image_id.encode()).hexdigest()
                if digest not in entry["RepoDigests"]:
                    entry["RepoDigests"].append(digest)
        for tag, image_id in self.moved_tags.items():
            found.setdefault(image_id, {"Id": image_id, "RepoTags": [], "RepoDigests": [], "Created": 1761500000,
                                        "Size": 450_000_000, "Labels": {}})["RepoTags"].append(tag)
        for extra in self.extra_images:
            found.setdefault(extra["Id"], json.loads(json.dumps(extra)))
        for image_id, tags in self.keep_tags.items():
            if image_id in found:
                found[image_id]["RepoTags"] += [t for t in tags if t not in found[image_id]["RepoTags"]]
        return list(found.values())

    def docker_answer(self, method: str, path: str):
        def reply(status: int, doc) -> tuple[int, bytes]:
            return status, (json.dumps(doc).encode() if doc is not None else b"")

        if method == "GET" and path == "/images/json":
            return reply(200, self.images())
        if method == "GET" and path == "/containers/json?all=1":
            rows = []
            for config in self._configs():
                key = next((k for k, v in IDS.items() if v == config["ID"]), "")
                health = f" ({self.health[key]})" if key in self.health else ""
                rows.append({"Id": config["ID"], "Names": [config["Name"]], "Image": config["Config"].get("Image"),
                             "ImageID": config.get("Image"), "State": "running" if config["State"].get("Running")
                             else "exited", "Status": ("Up 3 hours" if config["State"].get("Running") else "Exited")
                             + health})
            return reply(200, rows)
        match = re.fullmatch(r"/containers/([0-9a-f]{64})/json", path)
        if method == "GET" and match:
            config = next((c for c in self._configs() if c["ID"] == match.group(1)), None)
            if config is None:
                return reply(404, {"message": "No such container"})
            key = next((k for k, v in IDS.items() if v == config["ID"]), "")
            state = {"Running": bool(config["State"].get("Running"))}
            if key in self.health:
                state["Health"] = {"Status": self.health[key], "FailingStreak": 17 if self.health[key] == "unhealthy"
                                   else 0, "Log": [{"Output": "PLANTED-HEALTH-OUTPUT token=abc"}]}
            return reply(200, {"Id": config["ID"], "State": state, "Config": {"Env": ["PLANTED-ENV=1"]}})
        if method == "GET" and path == "/system/df":
            images = self.images()
            used = {}
            for config in self._configs():
                used[config.get("Image")] = used.get(config.get("Image"), 0) + 1
            return reply(200, {"LayersSize": sum(i["Size"] for i in images),
                               "Images": [{"Id": i["Id"], "RepoTags": i["RepoTags"], "Size": i["Size"],
                                           "SharedSize": 0, "Containers": used.get(i["Id"], 0)} for i in images],
                               "Containers": [{"Id": c["ID"], "SizeRw": 1_000_000} for c in self._configs()],
                               "Volumes": [{"Name": "media_ts", "UsageData": {"Size": 2_000_000, "RefCount": 1}},
                                           {"Name": "old_data", "UsageData": {"Size": 9_000_000_000, "RefCount": 0}}],
                               "BuildCache": self.build_cache})
        if method == "GET" and path == "/info":
            return reply(200, {"ServerVersion": "29.9.1", "Driver": "overlayfs", "DriverStatus": [["driver-type",
                                                                                                 "io.containerd.snapshotter.v1"]],
                               "Containers": len(self._configs()), "ContainersRunning": 18, "Images": len(self.images())})
        match = re.fullmatch(r"/images/([0-9a-f]{64})/tag\?repo=(seraphina-keep/[a-z0-9._-]+)&tag=([0-9a-f-]+)", path)
        if method == "POST" and match:
            self.keep_tags.setdefault("sha256:" + match.group(1), []).append(f"{match.group(2)}:{match.group(3)}")
            return 201, b""
        return reply(404, {"message": "page not found"})

    def compose_services(self, folder: str) -> list[str]:
        services = []
        for config in self._configs():
            labels = config["Config"].get("Labels") or {}
            if labels.get("com.docker.compose.project.working_dir") == folder and \
                    labels.get("com.docker.compose.service") not in services:
                services.append(labels["com.docker.compose.service"])
        return services or ["app"]

    def folder_images(self, folder: str) -> list[str]:
        return sorted({c["Config"].get("Image") for c in self._configs() if (c["Config"].get("Labels") or {}).get(
            "com.docker.compose.project.working_dir") == folder and c["State"].get("Running")})

    def container(self, key: str, name: str, image: str, *, labels: dict | None = None, env: list | None = None,
                  running: bool = True, pid: int = 0, network_mode: str = "bridge", networks: dict | None = None,
                  mounts: dict | None = None, ports: dict | None = None, restart: str = "unless-stopped",
                  exit_code: int = 0, manually_stopped: bool = False, started_before: bool = True,
                  finished_at: str = "2026-10-24T22:58:12Z", restart_count: int = 0, slice_: str = "system.slice",
                  programs: list[str] | None = None, ram: int = 200_000_000) -> str:
        container_id = IDS[key]
        folder = f"/var/lib/docker/containers/{container_id}"
        nets = {}
        for net, (ip, aliases) in (networks or {}).items():
            nets[net] = {"Aliases": aliases, "DNSNames": [name] + aliases, "Gateway": ".".join(ip.split(".")[:3]) + ".1",
                         "IPAddress": ip, "NetworkID": "d" * 64}
        config = {"ID": container_id, "Name": "/" + name, "Created": "2026-10-01T10:00:00Z", "RestartCount": restart_count,
                  "HasBeenStartedBefore": started_before, "HasBeenManuallyStopped": manually_stopped,
                  "Image": "sha256:" + container_id[::-1],
                  "Config": {"Image": image, "User": "", "Labels": labels or {}, "Env": env or ["PATH=/usr/bin"],
                             "Cmd": ["serve", "--token=PLANTED-CMD"],
                             **({"Healthcheck": {"Test": ["CMD", "true"]}} if key in self.health else {})},
                  "State": {"Running": running, "Restarting": False, "Pid": pid if running else 0, "ExitCode": exit_code,
                            "StartedAt": "2026-10-25T06:00:00Z" if started_before else "0001-01-01T00:00:00Z",
                            "FinishedAt": finished_at if not running else "0001-01-01T00:00:00Z"},
                  "MountPoints": mounts or {}, "NetworkSettings": {"Networks": nets}}
        host = {"NetworkMode": network_mode, "PortBindings": ports or {}, "RestartPolicy": {"Name": restart},
                "Privileged": False, "CapAdd": None, "PidMode": "", "Devices": None}
        self.write(f"{folder}/config.v2.json", config, 0o600)
        self.write(f"{folder}/hostconfig.json", host, 0o600)
        if running and pid:
            scope = f"/sys/fs/cgroup/{slice_}/docker-{container_id}.scope"
            self.write(f"/proc/{pid}/cgroup", f"0::/{slice_}/docker-{container_id}.scope\n")
            self.write(f"/proc/{pid}/comm", (programs or [name.split("-")[0]])[0] + "\n")
            extra = [pid + i for i in range(1, len(programs or []))]
            for i, other in enumerate(extra, 1):
                self.write(f"/proc/{other}/comm", programs[i] + "\n")
                self.write(f"/proc/{other}/cgroup", f"0::/{slice_}/docker-{container_id}.scope\n")
            self.write(scope + "/cgroup.procs", "\n".join(str(p) for p in [pid, *extra]) + "\n")
            self.write(scope + "/memory.current", f"{ram + 50_000_000}\n")
            self.write(scope + "/memory.stat", "anon 1000\nfile 60000000\ninactive_file 50000000\nactive_file 10\n")
            self.write(scope + "/cpu.stat", "usage_usec 1000000\nuser_usec 800000\nsystem_usec 200000\n")
        self.containers[key] = {"id": container_id, "name": name}
        return container_id

    def _apps(self) -> None:
        def compose(project: str, service: str, folder: str, number: str = "1", file: str = "compose.yaml",
                    **extra) -> dict:
            labels = {"com.docker.compose.project": project, "com.docker.compose.service": service,
                      "com.docker.compose.container-number": number, "com.docker.compose.oneoff": "False",
                      "com.docker.compose.project.working_dir": folder,
                      "com.docker.compose.project.config_files": f"{folder}/{file}",
                      "com.docker.compose.config-hash": "a" * 64}
            labels.update(extra)
            return labels

        for folder, file in (("/opt/stacks/photos", "compose.yaml"), ("/opt/stacks/media", "compose.yaml"),
                             ("/opt/stacks/gateway", "compose.yaml"), ("/opt/stacks/beszel", "compose.yaml"),
                             ("/home/owner/dockge", "compose.yaml"), ("/home/owner", "compose.json"),
                             ("/opt/stacks/tsshared", "compose.yaml"), ("/opt/stacks/ai", "compose.yaml")):
            self.write(f"{folder}/{file}", "services: {}\n# PLANTED-COMPOSE-FILE\n")
        self.write("/opt/stacks/photos/.env", "DB_PASSWORD=PLANTED-DOTENV\n")
        # photos: the recorded-shape fixture, its database, and a Tailscale container reaching it by name
        folder = f"/var/lib/docker/containers/{IDS['photos']}"
        self.write(f"{folder}/config.v2.json", (FIXTURES / "docker29" / "config.v2.json").read_bytes(), 0o600)
        self.write(f"{folder}/hostconfig.json", (FIXTURES / "docker29" / "hostconfig.json").read_bytes(), 0o600)
        config = json.loads(self.at(f"{folder}/config.v2.json").read_text())
        config["Config"]["Healthcheck"] = {"Test": ["CMD-SHELL", "curl -f http://localhost:2342/ || exit 1"]}
        self.write(f"{folder}/config.v2.json", config, 0o600)
        self.write_logs("photos", [("2026-10-26T07:59:58.000000001Z", "stdout", "Starting the photo library"),
                                   ("2026-10-26T07:59:59.000000001Z", "stderr", "login failed for admin password=hunter2-PLANTED"),
                                   ("2026-10-26T08:00:00.000000001Z", "stdout", "<img src=x onerror=alert('log')>"),
                                   ("2026-10-26T08:00:01.000000001Z", "stdout", "GET /api Authorization: Bearer 0123456789abcdefghijklmnopqrstuvwxyzPLANTED")])
        self._scope("photos", 4301)
        self.container("photos-db", "photos-db-1", "mariadb:11", labels=compose("photos", "db", "/opt/stacks/photos"),
                       pid=4311, networks={"photos_default": ("172.19.0.2", ["db"])})
        self.container("photos-ts", "photos-ts-1", "tailscale/tailscale:latest",
                       labels=compose("photos", "ts", "/opt/stacks/photos"), pid=4321, programs=["containerboot",
                                                                                                  "tailscaled"],
                       networks={"photos_default": ("172.19.0.4", ["ts"])},
                       mounts={"/var/lib/tailscale": {"Source": "/opt/stacks/photos/ts-state",
                                                      "Destination": "/var/lib/tailscale", "RW": True, "Type": "bind"}})
        self.write("/opt/stacks/photos/ts-state/tailscaled.state",
                   ts_state("photos", web("photos.example.ts.net", 443, "http://photos:2342")), 0o600)
        # a renamed predecessor of photos: the same Compose labels, stopped
        self.container("photos-old", "photos_old", "photoprism/photoprism:240901",
                       labels=compose("photos", "photos", "/opt/stacks/photos"), running=False, manually_stopped=True,
                       finished_at="2026-09-01T10:00:00Z")
        # a one-off run of photos, and a job run sharing photos' exact image
        self.container("photos-run", "photos-photos-run-0a1b", "photoprism/photoprism:241021",
                       labels=compose("photos", "photos", "/opt/stacks/photos", **{"com.docker.compose.oneoff": "True"}),
                       running=False)
        self.container("job-run", "transcode-0f1e2d3c-1111-4222-8333-444455556666", "photoprism/photoprism:241021",
                       running=False, network_mode="none", restart="no")
        job = self.at(f"/var/lib/docker/containers/{IDS['job-run']}/config.v2.json")
        data = json.loads(job.read_text())
        data["Image"] = "sha256:" + "b" * 64
        job.write_text(json.dumps(data))
        # media: a Tailscale container whose network the app shares; its state in a named volume; PATH only
        self.container("media-ts", "media-ts-1", "tailscale/tailscale:v1.104",
                       labels=compose("media", "ts", "/opt/stacks/media"), pid=4401, programs=["containerboot"],
                       networks={"media_default": ("172.20.0.2", ["ts"])},
                       mounts={"/var/lib/tailscale": {"Source": "", "Name": "media_ts", "Driver": "local",
                                                      "Destination": "/var/lib/tailscale", "RW": True, "Type": "volume"}})
        self.write("/var/lib/docker/volumes/media_ts/_data/tailscaled.state",
                   ts_state("media", web("media.example.ts.net", 443, "http://127.0.0.1:8096")), 0o600)
        self.container("media", "media-jellyfin-1", "jellyfin/jellyfin:10.9.11",
                       labels=compose("media", "jellyfin", "/opt/stacks/media"), pid=4201,
                       network_mode=f"container:{IDS['media-ts']}", slice_="custom.slice", ram=1_200_000_000)
        # two projects from one folder: the old name still runs one service
        self.container("mediaold-web", "mediaold-web-1", "jellyfin/jellyfin:10.8",
                       labels=compose("mediaold", "web", "/opt/stacks/media"), pid=4501,
                       networks={"mediaold_default": ("172.22.0.2", [])})
        # an older copy of jellyfin started by hand, stopped
        self.container("jellyfin-2023", "jellyfin-2023", "jellyfin/jellyfin:10.7", running=False,
                       restart="always", manually_stopped=True, finished_at="2026-03-01T10:00:00Z")
        # the login gateway: nginx on the host network, its sign-in service, database and oauth2-proxy
        self.write("/opt/stacks/gateway/nginx.conf", (FIXTURES / "nginx" / "nginx.conf").read_text())
        self.write("/opt/stacks/gateway/conf.d/routes.conf", (FIXTURES / "nginx" / "routes.conf").read_text())
        self.write("/opt/stacks/gateway/secret-headers.conf", (FIXTURES / "nginx" / "secret-headers.conf").read_text())
        self.write("/opt/stacks/gateway/certs/gateway.key", "-----BEGIN PRIVATE KEY-----\nPLANTED-TLS-KEY\n", 0o600)
        gw_mounts = {"/etc/nginx/nginx.conf": {"Source": "/opt/stacks/gateway/nginx.conf",
                                               "Destination": "/etc/nginx/nginx.conf", "RW": False, "Type": "bind"},
                     "/etc/nginx/conf.d": {"Source": "/opt/stacks/gateway/conf.d", "Destination": "/etc/nginx/conf.d",
                                           "RW": False, "Type": "bind"},
                     "/etc/nginx/secret-headers.conf": {"Source": "/opt/stacks/gateway/secret-headers.conf",
                                                        "Destination": "/etc/nginx/secret-headers.conf", "RW": False,
                                                        "Type": "bind"},
                     "/etc/nginx/certs": {"Source": "/opt/stacks/gateway/certs", "Destination": "/etc/nginx/certs",
                                          "RW": False, "Type": "bind"}}
        self.container("gateway-nginx", "gateway-nginx-1", "nginx:1.27", labels=compose("gateway", "nginx",
                                                                                        "/opt/stacks/gateway"),
                       pid=2101, network_mode="host", mounts=gw_mounts)
        self.container("gateway-dex", "gateway-dex-1", "ghcr.io/dexidp/dex:v2.41",
                       labels=compose("gateway", "dex", "/opt/stacks/gateway"), pid=2301,
                       networks={"gateway_default": ("172.23.0.2", ["dex"])})
        self.container("gateway-db", "gateway-db-1", "postgres:16", labels=compose("gateway", "db", "/opt/stacks/gateway"),
                       pid=2401, networks={"gateway_default": ("172.23.0.3", ["db"])})
        self.container("gateway-oauth", "gateway-oauth2-home-1", "quay.io/oauth2-proxy/oauth2-proxy:v7.7",
                       labels=compose("gateway", "oauth2-home", "/opt/stacks/gateway"), pid=2201,
                       networks={"gateway_default": ("172.23.0.4", ["oauth2-home"])},
                       ports={"4180/tcp": [{"HostIp": "127.0.0.1", "HostPort": "4180"}]})
        # Beszel: the hub on its own network (the gateway routes to its address), the agent on the host network
        self.container("beszel-hub", "beszel", "henrygd/beszel:0.21.1", labels=compose("beszel", "beszel",
                                                                                     "/opt/stacks/beszel"),
                       pid=4601, networks={"beszel_default": ("172.21.0.5", ["beszel"])})
        self.container("beszel-agent", "beszel-agent", "henrygd/beszel-agent:0.21.1",
                       labels=compose("beszel", "beszel-agent", "/opt/stacks/beszel"), pid=4611, network_mode="host")
        # Dockge itself, from a folder in the owner's home, on a floating tag
        self.container("dockge", "dockge", "louislam/dockge:1", labels=compose("dockge", "dockge", "/home/owner/dockge"),
                       pid=4701, env=["PATH=/usr/bin", "DOCKGE_STACKS_DIR=/opt/stacks", "DOCKGE_SECRET=PLANTED-DOCKGE"],
                       networks={"dockge_default": ("172.24.0.2", [])},
                       mounts={"/var/run/docker.sock": {"Source": "/var/run/docker.sock",
                                                        "Destination": "/var/run/docker.sock", "RW": True,
                                                        "Type": "bind"}},
                       ports={"5001/tcp": [{"HostIp": "127.0.0.1", "HostPort": "5001"}]})
        # a compose file kept directly in the owner's home folder (compose.json), published on every address
        self.container("notes", "owner-notes-1", "ghcr.io/example/notes:2.0",
                       labels=compose("owner", "notes", "/home/owner", file="compose.json"), pid=4801,
                       networks={"owner_default": ("172.25.0.2", ["notes"])},
                       ports={"3000/tcp": [{"HostIp": "", "HostPort": "3000"}]})
        # a container started by hand, published on the home-network address only
        self.container("filebrowser", "filebrowser", "filebrowser/filebrowser:v2.31", pid=4901,
                       networks={"bridge": ("172.17.0.2", [])},
                       ports={"80/tcp": [{"HostIp": "192.0.2.10", "HostPort": "8081"}]},
                       mounts={"/srv": {"Source": "/srv/data1", "Destination": "/srv", "RW": True, "Type": "bind"}})
        # a Tailscale container in a stack of its own, on the host network, serving two apps by host port
        shared = {"TCP": {"443": {"HTTPS": True}, "8443": {"HTTPS": True}},
                  "Web": {"shared.example.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:3000"}}},
                          "shared.example.ts.net:8443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8081"}}}}}
        self.container("shared-ts", "tsshared-ts-1", "ghcr.io/tailscale/tailscale:latest",
                       labels=compose("tsshared", "ts", "/opt/stacks/tsshared"), pid=5001, network_mode="host",
                       env=["PATH=/usr/bin", "TS_HOSTNAME=shared-old", "TS_AUTHKEY=tskey-PLANTED-ENV",
                            "TS_EXTRA_ARGS=--authkey=PLANTED-EXTRA", "TS_SERVE_CONFIG=/config/serve.json"],
                       mounts={"/var/lib/tailscale": {"Source": "/opt/stacks/tsshared/state",
                                                      "Destination": "/var/lib/tailscale", "RW": True, "Type": "bind"},
                               "/config": {"Source": "/opt/stacks/tsshared/config", "Destination": "/config",
                                           "RW": False, "Type": "bind"}})
        self.write("/opt/stacks/tsshared/state/tailscaled.state", ts_state("shared", shared), 0o600)
        self.write("/opt/stacks/tsshared/config/serve.json",
                   {"TCP": {"443": {"HTTPS": True}},
                    "Web": {"${TS_CERT_DOMAIN}:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:3000"}}}}})
        # a model server on the GPU
        self.container("ollama", "ai-ollama-1", "ollama/ollama:latest", labels=compose("ai", "ollama", "/opt/stacks/ai"),
                       pid=4101, networks={"ai_default": ("172.26.0.2", ["ollama"])},
                       ports={"11434/tcp": [{"HostIp": "127.0.0.1", "HostPort": "11434"}]}, ram=9_000_000_000)
        # a file of an unknown shape
        self.write(f"/var/lib/docker/containers/{IDS['broken']}/config.v2.json", {"ID": IDS["broken"], "Config": {}},
                   0o600)
        self.write(f"/var/lib/docker/containers/{IDS['broken']}/hostconfig.json", {}, 0o600)
        # the host-side owners of listeners (ss -p's PIDs)
        self.write("/proc/1/cgroup", "0::/init.scope\n")
        self.write("/proc/2102/cgroup", f"0::/system.slice/docker-{IDS['gateway-nginx']}.scope\n")
        self.write("/proc/3001/cgroup", "0::/system.slice/docker.service\n")
        self.write("/proc/3002/cgroup", "0::/system.slice/docker.service\n")
        self.write("/proc/900/cgroup", "0::/system.slice/ssh.service\n")
        self.write("/proc/1500/cgroup", "0::/system.slice/seraphina.service\n")

    def write_logs(self, key: str, lines: list[tuple[str, str, str]], rotated: list[tuple[str, str, str]] | None = None
                   ) -> None:
        """Docker's json-file log of one container (and one rotated copy): one JSON object per line."""
        cid = IDS[key]
        folder = f"/var/lib/docker/containers/{cid}"
        for suffix, rows in (("", lines), (".1", rotated or [])):
            if rows or not suffix:
                self.write(f"{folder}/{cid}-json.log{suffix}", "".join(json.dumps({"log": text + "\n", "stream": stream,
                                                                                   "time": at}) + "\n"
                                                                       for at, stream, text in rows), 0o640)

    def _scope(self, key: str, pid: int, slice_: str = "system.slice") -> None:
        container_id = IDS[key]
        scope = f"/sys/fs/cgroup/{slice_}/docker-{container_id}.scope"
        self.write(f"/proc/{pid}/cgroup", f"0::/{slice_}/docker-{container_id}.scope\n")
        self.write(f"/proc/{pid}/comm", "photoprism\n")
        self.write(scope + "/cgroup.procs", f"{pid}\n")
        self.write(scope + "/memory.current", "1450000000\n")
        self.write(scope + "/memory.stat", "anon 1\ninactive_file 250000000\n")
        self.write(scope + "/cpu.stat", "usage_usec 5000000\n")
        self.containers[key] = {"id": container_id, "name": "photos-photos-1"}

    # -- what the harness's own user runs (Checks 1 and 4) --------------------------------------------------------

    def harness_run(self, argv: list[str]) -> tuple[int | None, str]:
        """YourServer's run= injectable: apt-get -s, apt-cache show, apt-mark and systemctl's lists from the
        fixtures; a simulated install of exactly the pairs asked (plus rig.simulate_extra when a test wants one)."""
        self.harness_calls.append(list(argv))
        checks = FIXTURES / "checks"
        if argv[:2] == ["apt-get", "-s"]:
            if argv[-1] == "upgrade":
                return 0, (checks / "apt-s-upgrade.txt").read_text()
            if argv[-1] == "dist-upgrade":
                return 0, (checks / "apt-s-dist-upgrade.txt").read_text()
            if "install" in argv:
                pairs = [a for a in argv if "=" in a and not a.startswith("Debug")]
                lines = [f"Inst {p.split('=')[0]} [0] ({p.split('=', 1)[1]} Ubuntu:26.04/resolute-updates [amd64])"
                         for p in pairs + self.simulate_extra]
                return 0, "\n".join(lines + [f"{len(lines)} upgraded, 0 newly installed, 0 to remove and 0 not "
                                              "upgraded."]) + "\n"
        if argv[:2] == ["apt-cache", "show"]:
            path = checks / "apt-cache" / (argv[2].split("=")[0] + ".txt")
            return (0, path.read_text()) if path.exists() else (100, "")
        if argv[:2] == ["apt-mark", "showhold"]:
            return 0, ""
        if argv[:2] == ["systemctl", "list-units"] and "--failed" in argv:
            return 0, (checks / "systemctl-failed.json").read_text()
        if argv[:2] == ["systemctl", "list-timers"]:
            return 0, (checks / "systemctl-timers.json").read_text()
        if argv[:2] == ["systemctl", "show"] and "Id,Result" in argv:
            return 0, (checks / "systemctl-show-result.txt").read_text()
        if argv[:2] == ["systemctl", "show"]:
            return 0, "ActiveState=active\n"
        return None, ""

    # -- calling the helper ----------------------------------------------------------------------------------------

    def main(self, argv: list[str], data: bytes = b"", uid: int | None = None, environ: dict | None = None):
        out = io.StringIO()
        env = {"SUDO_UID": str(self.uid if uid is None else uid)} if environ is None else environ
        code = sx.main(argv, stdin=io.BytesIO(data), stdout=out, paths=self.paths, system=self.system,
                       lstat=self.lstat, execv=lambda *a: None, environ=env)
        return code, out.getvalue()

    def ask(self, verb: str, uid: int | None = None, **fields):
        line = json.dumps({"v": 1, "verb": verb, **fields}).encode() + b"\n"
        code, text = self.main([], line, uid)
        return code, json.loads(text.strip().splitlines()[-1])

    def runner(self, argv, data, timeout):
        """A HelperClient runner: `sudo -n <helper>` answered by the helper's main() in this process."""
        assert argv[1:] == ["-n", sx.HELPER], argv
        code, text = self.main([], data)
        return code, text.encode(), b""
