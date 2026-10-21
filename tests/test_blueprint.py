"""Tests for swfactory.blueprint: the read-only snapshot collector and its redaction (DESIGN.md §28).

FakeRunner stands in for the host. It answers from fixture outputs of a small AI server (Docker
with Open WebUI and a Tailscale sidecar, two GPUs, Cosmos, Cockpit) and fails the test on any
command that is not allow-listed, that could change something, or that the fixture does not
expect, so the command set the collector runs is pinned here. CollectTestCase also stands in for
the host's symlinks (none, unless a test says) and makes systemd this host's init.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from typing import Any, Iterable
from unittest import mock

from swfactory import __main__ as cli
from swfactory.blueprint import collect as bp
from swfactory.blueprint.collect import (INDEXES, SECTIONS, Collector, Result, SubprocessRunner, UnsafeCommand,
                                         address_scope, check_argv, collect, compose_services, overview, parse_ss,
                                         write_snapshot)
from swfactory.blueprint.redact import MARK, Redactor

IDS = {name: ch * 64 for name, ch in (("open-webui", "a"), ("ts-webui", "b"), ("old-llm", "c"), ("dockge", "d"))}
NET_ID = "e" * 64
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJkb2NrZ2UifQ.c2lnbmF0dXJlMDAwNQ"
PRIVATE_KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA0012\n-----END OPENSSH PRIVATE KEY-----"
KNOWN = "factory-known-secret-0013"
SECRETS = ["sk-live-openwebui-0001", "cosmos-label-secret-0002", "tskey-auth-0003", "llm-arg-secret-0004",
           "c2lnbmF0dXJlMDAwNQ", "sk-compose-secret-0006", "tskey-compose-0007", "pg-pass-0008", "restic-cron-0009",
           "wifi-pass-0010", "cifs-pass-0011", "b3BlbnNzaC1rZXktdjEAAAAA0012", KNOWN, "cosmos-private-key-0014",
           "mongo-pass-0015", "smtp-pass-0016", "nodekey:5ec2e7", "Home AI"]

# Commands that would change the host. The fake fails on them before it even asks check_argv.
NEVER_PROGRAMS = {"lxc", "lxd", "apt", "apt-get", "aptitude", "dpkg", "pip", "pip3", "npm", "bash", "sh", "rm",
                  "ufw", "reboot", "shutdown", "kill"}
NEVER_DOCKER = {"start", "restart", "stop", "exec", "run", "pull", "logs", "rm", "kill", "create", "cp", "update",
                "build", "push", "up", "down", "prune", "attach"}


def container(name: str, *, image: str, state: str = "running", restart: str = "unless-stopped",
              env: list[str] | None = None, labels: dict | None = None, mounts: list[dict] | None = None,
              ports: dict | None = None, networks: dict | None = None, gpu: bool = False,
              cmd: list[str] | None = None) -> dict:
    return {"Id": IDS[name], "Name": f"/{name}", "Created": "2026-10-01T00:00:00Z", "Image": "sha256:" + "1" * 64,
            "State": {"Status": state, "StartedAt": "2026-10-21T06:00:00Z", "Health": {"Status": "healthy"}},
            "HostConfig": {"RestartPolicy": {"Name": restart}, "NetworkMode": "ai", "Privileged": False,
                           "DeviceRequests": [{"Count": -1, "Capabilities": [["gpu"]]}] if gpu else None},
            "Config": {"Image": image, "Env": env or [], "Cmd": cmd or [], "Labels": labels or {}},
            "Mounts": mounts or [],
            "NetworkSettings": {"Networks": networks or {}, "Ports": ports or {}}}


CONTAINERS = [
    container("open-webui", image="ghcr.io/open-webui/open-webui:main",
              env=["OPENAI_API_KEY=sk-live-openwebui-0001", "WEBUI_NAME=Home AI", "EMPTY="],
              labels={"com.docker.compose.project": "ai", "com.docker.compose.service": "open-webui",
                      "com.docker.compose.project.config_files": "/opt/stacks/ai/compose.yaml",
                      "com.docker.compose.project.working_dir": "/opt/stacks/ai",
                      "cosmos-auth-token": "cosmos-label-secret-0002"},
              mounts=[{"Type": "volume", "Name": "open-webui", "Source": "/var/lib/docker/volumes/open-webui/_data",
                       "Destination": "/app/backend/data", "Mode": "z", "RW": True}],
              ports={"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "3000"}]},
              networks={"ai": {"IPAddress": "172.20.0.5", "IPAMConfig": {"IPv4Address": "172.20.0.5"}}}),
    container("ts-webui", image="tailscale/tailscale:latest", env=["TS_AUTHKEY=tskey-auth-0003", "TS_HOSTNAME=webui"],
              labels={"com.docker.compose.project.config_files": "/opt/stacks/ai/compose.yaml"},
              mounts=[{"Type": "bind", "Source": "/opt/stacks/ai/ts-state", "Destination": "/var/lib/tailscale",
                       "Mode": "", "RW": True}],
              networks={"ai": {"IPAddress": "172.20.0.6"}}),
    container("old-llm", image="vllm/vllm-openai:v0.6", state="exited", restart="always", gpu=True,
              cmd=["--api-key", "llm-arg-secret-0004", "--port", "8000"],
              labels={"com.docker.compose.project.config_files": "/home/owner/old/docker-compose.yml"},
              ports={"8000/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8000"}]}),
    container("dockge", image="louislam/dockge:1", labels={"note": f"session {JWT}"},
              mounts=[{"Type": "bind", "Source": "/var/run/docker.sock", "Destination": "/var/run/docker.sock",
                       "Mode": "", "RW": True},
                      {"Type": "bind", "Source": "/opt/stacks", "Destination": "/opt/stacks", "Mode": "ro",
                       "RW": False}]),
]

COMPOSE_AI = """services:
  open-webui:
    image: ghcr.io/open-webui/open-webui:main
    environment:
      - OPENAI_API_KEY=sk-compose-secret-0006
      - WEBUI_NAME=Home AI
    ports:
      - "127.0.0.1:3000:8080"
  ts-webui:
    image: tailscale/tailscale
    environment:
      TS_AUTHKEY: tskey-compose-0007
      TS_HOSTNAME: webui
volumes:
  open-webui:
"""
COMPOSE_MEDIA = """services:
  media:
    image: jellyfin/jellyfin
    command: --db postgres://media:pg-pass-0008@db:5432/media
    labels:
      - "note=factory-known-secret-0013"
"""

UNITS = {
    "docker.service": ("enabled", "loaded", "active", "running", "Docker Application Container Engine",
                       {"Requires": "docker.socket", "Wants": "network-online.target containerd.service",
                        "After": "network-online.target docker.socket containerd.service",
                        "DropInPaths": "/etc/systemd/system/docker.service.d/wait-for-data.conf",
                        "TriggeredBy": "docker.socket", "Restart": "always"}),
    "snapd.service": ("enabled", "loaded", "active", "running", "Snap Daemon", {}),
    "CosmosCloud.service": ("enabled", "loaded", "active", "running", "Cosmos Cloud", {"After": "network.target"}),
    "ssh.service": ("enabled", "loaded", "active", "running", "OpenBSD Secure Shell server", {}),
    "failed-thing.service": ("enabled", "loaded", "failed", "failed", "A unit that failed", {}),
    "restic-backup.timer": ("disabled", "loaded", "inactive", "dead", "Nightly restic backup",
                            {"Triggers": "restic-backup.service"}),
}

COSMOS = {"HTTPConfig": {"Hostname": "home.example", "AuthPrivateKey": "cosmos-private-key-0014",
                         "ProxyConfig": {"Routes": [
                             {"Name": "webui", "Host": "ai.home.example", "Mode": "SERVAPP",
                              "Target": "http://open-webui:8080", "AuthEnabled": False},
                             {"Name": "media", "Host": "media.home.example", "Mode": "SERVAPP",
                              "Target": "http://media:8096", "AuthEnabled": True}]}},
          "MongoDB": "mongodb://cosmos:mongo-pass-0015@127.0.0.1:27017/",
          "EmailConfig": {"Host": "smtp.example", "Username": "owner", "Password": "smtp-pass-0016"}}

SERVE = {"TCP": {"443": {"HTTPS": True}, "5432": {"TCPForward": "127.0.0.1:5432"}},
         "Web": {"webui.tail1234.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:3000"}}}}}

SS = """tcp   LISTEN 0      4096       127.0.0.1:3000      0.0.0.0:*    users:(("docker-proxy",pid=1200,fd=4))
tcp   LISTEN 0      4096         0.0.0.0:8000      0.0.0.0:*
tcp   LISTEN 0      128             [::]:22           [::]:*    users:(("sshd",pid=800,fd=3))
tcp   LISTEN 0      4096   100.101.102.103:443     0.0.0.0:*    users:(("tailscaled",pid=900,fd=12))
udp   UNCONN 0      0      127.0.0.53%lo:53        0.0.0.0:*    users:(("systemd-resolve",pid=600,fd=14))
"""

FILES = {
    "/etc/os-release": 'PRETTY_NAME="Ubuntu 24.04.1 LTS"\nNAME="Ubuntu"\nVERSION_ID="24.04"\n',
    "/etc/fstab": "UUID=1234 / ext4 defaults 0 1\n//nas/backup /mnt/nas cifs username=owner,password=cifs-pass-0011 0 0\n",
    "/etc/hosts": "127.0.0.1 localhost\n",
    "/etc/resolv.conf": "nameserver 127.0.0.53\n",
    "/etc/ufw/ufw.conf": "ENABLED=yes\nLOGLEVEL=low\n",
    "/etc/apt/sources.list": "",
    "/etc/apt/sources.list.d/docker.list": "deb [signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu noble stable\n",
    "/etc/apt/apt.conf.d/20auto-upgrades": 'APT::Periodic::Unattended-Upgrade "1";\n',
    "/etc/crontab": "SHELL=/bin/sh\n17 * * * * root cd / && run-parts --report /etc/cron.hourly\n",
    "/etc/cron.d/backup": "RESTIC_PASSWORD=restic-cron-0009\n0 3 * * * root restic backup /srv\n",
    "/etc/cron.d/deploy-key": PRIVATE_KEY + "\n",
    "/etc/systemd/journald.conf": "[Journal]\n#Storage=auto\n",
    "/etc/docker/daemon.json": '{"data-root": "/srv/docker", "registry-mirrors": []}',
    "/var/lib/cosmos/cosmos.config.json": json.dumps(COSMOS),
    "/opt/stacks/ai/compose.yaml": COMPOSE_AI,
    "/srv/media/docker-compose.yml": COMPOSE_MEDIA,
    "/etc/snapraid.conf": "parity /mnt/parity1/snapraid.parity\n",
}
LISTINGS = {
    ("-1A", "/etc/apt/sources.list.d"): "docker.list\nnotes.txt\n",
    ("-1A", "/etc/cron.d"): ".placeholder\nbackup\ndeploy-key\n",
    ("-1A", "/etc/logrotate.d"): "apt\nrsyslog\n",
    ("-la", "/etc/cockpit"): "total 0\n",
    ("-la", "/var/lib/cosmos"): "total 8\n-rw------- 1 root root 2048 Oct 21 cosmos.config.json\n",
}

OUTPUTS: dict[tuple[str, ...], tuple[int, str, str]] = {
    ("hostname",): (0, "seraphina-test\n", ""),
    ("uname", "-snrvm"): (0, "Linux seraphina-test 6.8.0-45-generic #45-Ubuntu SMP PREEMPT_DYNAMIC x86_64\n", ""),
    ("uptime",): (0, " 10:00:00 up 4:00,  1 user,  load average: 0.10, 0.20, 0.30\n", ""),
    ("uptime", "-s"): (0, "2026-10-21 06:00:00\n", ""),
    ("lscpu", "-J"): (0, json.dumps({"lscpu": [{"field": "Architecture:", "data": "x86_64"}]}), ""),
    ("free", "-b"): (0, "Mem: 67108864000 10000 20000\n", ""),
    ("systemd-detect-virt",): (1, "none\n", ""),
    ("timedatectl", "show"): (0, "Timezone=Europe/London\nNTPSynchronized=yes\n", ""),
    ("lspci", "-nn"): (0, "01:00.0 VGA compatible controller [0300]: NVIDIA Corporation GA102 [GeForce RTX 3090]\n", ""),
    ("nvidia-smi", "-L"): (0, "GPU 0: NVIDIA GeForce RTX 3090 (UUID: GPU-1)\nGPU 1: NVIDIA GeForce RTX 3090 (UUID: GPU-2)\n", ""),
    ("nvidia-smi", "topo", "-m"): (0, "GPU0 X NV4\nGPU1 NV4 X\n", ""),
    ("nvidia-smi", "nvlink", "--status"): (0, "GPU 0: Link 0: 14.062 GB/s\n", ""),
    ("lsblk", "-J", "-b", "-o", "NAME,KNAME,PATH,TYPE,SIZE,ROTA,TRAN,MODEL,SERIAL,FSTYPE,UUID,LABEL,MOUNTPOINTS"):
        (0, json.dumps({"blockdevices": [{"name": "sda", "type": "disk", "size": 4000787030016}]}), ""),
    ("findmnt", "-J", "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"):
        (0, json.dumps({"filesystems": [{"target": "/", "source": "/dev/sda1", "fstype": "ext4"}]}), ""),
    ("df", "-P", "-B1", "-T"): (0, "Filesystem Type 1-blocks Used Available Capacity Mounted on\n", ""),
    ("df", "-P", "-i"): (0, "Filesystem Inodes IUsed IFree IUse% Mounted on\n", ""),
    ("swapon", "--show", "--bytes"): (0, "", ""),
    ("smartctl", "--scan", "-j"): (0, json.dumps({"devices": [{"name": "/dev/sda", "type": "sat"},
                                                              {"name": "/dev/sda; reboot", "type": "sat"}]}), ""),
    ("smartctl", "-j", "-n", "standby", "-i", "-H", "-A", "-d", "sat", "/dev/sda"):
        (2, "", "Smartctl open device: /dev/sda failed: Permission denied\n"),
    ("ip", "-j", "addr", "show"): (0, json.dumps([{"ifname": "eth0", "addr_info": [{"local": "192.168.1.10"}]}]), ""),
    ("ip", "-j", "route", "show"): (0, "[]", ""),
    ("ip", "-j", "-6", "route", "show"): (0, "[]", ""),
    ("ss", "-H", "-l", "-n", "-t", "-u", "-p"): (0, SS, ""),
    ("resolvectl", "status"): (0, "Global\n  Current DNS Server: 100.100.100.100\n", ""),
    ("nft", "list", "ruleset"): (1, "", "Operation not permitted (you must be root)\n"),
    ("netplan", "get"): (0, "network:\n  wifis:\n    wlan0:\n      access-points:\n        home:\n"
                            "          password: wifi-pass-0010\n", ""),
    ("dpkg-query", "-W", bp.DPKG_FORMAT): (0, "docker-ce\t5:27.3.1-1~ubuntu.24.04~noble\tii \nnvidia-driver-550\t"
                                              "550.120-0ubuntu1\tii \ncockpit\t316-1\tii \n", ""),
    ("apt-mark", "showhold"): (0, "nvidia-driver-550\n", ""),
    ("snap", "list"): (0, "Name Version Rev Tracking Publisher Notes\ncore22 20240904 1621 latest/stable canonical** "
                          "base\n", ""),
    ("crontab", "-l"): (1, "", "no crontab for owner\n"),
    ("journalctl", "--disk-usage"): (0, "Archived and active journals take up 1.2G in the file system.\n", ""),
    ("journalctl", "--list-boots", "--no-pager"): (0, " -1 aaa Mon 2026-10-20 06:00 Mon 2026-10-20 23:00\n", ""),
    ("systemctl", "list-unit-files", "--no-legend", "--no-pager"):
        (0, "".join(f"{name} {spec[0]} enabled\n" for name, spec in UNITS.items()), ""),
    ("systemctl", "list-units", "--all", "--no-legend", "--no-pager", "--plain", "--full"):
        (0, "".join(f"{name} {spec[1]} {spec[2]} {spec[3]} {spec[4]}\n" for name, spec in UNITS.items()), ""),
    ("systemctl", "list-timers", "--all", "--no-legend", "--no-pager", "--full"): (0, "", ""),
    ("systemctl", "is-system-running"): (1, "degraded\n", ""),
    ("docker", "version", "--format", "{{json .}}"): (0, json.dumps({"Server": {"Version": "27.3.1"}}), ""),
    ("docker", "info", "--format", "{{json .}}"): (0, json.dumps({"Containers": 4, "RegistryConfig": {}}), ""),
    ("docker", "ps", "-a", "--no-trunc", "--format", "{{json .}}"):
        (0, "".join(json.dumps({"ID": IDS[c["Name"][1:]], "Names": c["Name"][1:], "Image": c["Config"]["Image"],
                                "State": c["State"]["Status"]}) + "\n" for c in CONTAINERS), ""),
    ("docker", "network", "ls", "--no-trunc", "--format", "{{json .}}"):
        (0, json.dumps({"ID": NET_ID, "Name": "ai", "Driver": "bridge"}) + "\n", ""),
    ("docker", "network", "inspect", NET_ID):
        (0, json.dumps([{"Name": "ai", "IPAM": {"Config": [{"Subnet": "172.20.0.0/16"}]}}]), ""),
    ("docker", "volume", "ls", "--format", "{{json .}}"): (0, json.dumps({"Name": "open-webui"}) + "\n", ""),
    ("docker", "image", "ls", "--no-trunc", "--format", "{{json .}}"):
        (0, json.dumps({"ID": "sha256:" + "1" * 64, "Repository": "ghcr.io/open-webui/open-webui"}) + "\n", ""),
    ("docker", "compose", "ls", "--all", "--format", "json"):
        (0, json.dumps([{"Name": "ai", "ConfigFiles": "/opt/stacks/ai/compose.yaml"}]), ""),
    ("tailscale", "version"): (0, "1.76.1\n", ""),
    ("tailscale", "status", "--json"):
        (0, json.dumps({"BackendState": "Running", "Self": {"HostName": "seraphina-test", "PublicKey": "nodekey:5ec2e7",
                                                            "TailscaleIPs": ["100.101.102.103"]},
                        "Peer": {"nodekey:00ff": {"HostName": "laptop"}}}), ""),
    ("tailscale", "serve", "status", "--json"): (0, json.dumps(SERVE), ""),
    ("tailscale", "funnel", "status", "--json"): (0, "{}", ""),
    ("restic", "version"): (0, "restic 0.16.4\n", ""),
}
SHOW_PREFIX = "--property=Id,Description,"
LIST_UNITS = ("systemctl", "list-units", "--all", "--no-legend", "--no-pager", "--plain", "--full")
WG_PRIVATE = "4GgaQCy68nzNsUE5aJ9fuLzHhB65tAlwbmA72MWnOm8="
WG_SHARED = "7voRZ/ojfXgfPOlswo3Lpma1RJq7qijIEEUEMShQFV8="
# `netplan get` of a WireGuard tunnel, as netplan 1.1 prints it (from a --root-dir run, so no real config).
NETPLAN_WG = f"""network:
  version: 2
  tunnels:
    wg0:
      addresses:
      - "10.10.10.20/24"
      mode: "wireguard"
      port: 51820
      keys:
        private: "{WG_PRIVATE}"
      peers:
      - endpoint: "1.2.3.4:5"
        keys:
          public: "M9nt4YujIOmNrRmpIRTmYSfMdrpvE7u6WkG8FY8WjG4="
          shared: "{WG_SHARED}"
        allowed-ips:
        - "0.0.0.0/0"
"""


def show_blocks(units: list[str]) -> str:
    blocks = []
    for unit in units:
        spec = UNITS[unit]
        props = {"Id": unit, "Description": spec[4], "LoadState": spec[1], "ActiveState": spec[2], "SubState": spec[3],
                 "UnitFileState": spec[0], "FragmentPath": f"/usr/lib/systemd/system/{unit}", **spec[5]}
        blocks.append("".join(f"{key}={value}\n" for key, value in props.items()))
    return "\n".join(blocks)


class FakeRunner:
    """The fixture host. Fails the test on a command that is not allow-listed, could change something, or is
    not in the fixture. sudo: "allow" answers `sudo -n --` like the command itself, "refuse" like a sudo that
    wants a password."""

    def __init__(self, test: unittest.TestCase, *, missing: set[str] | frozenset = frozenset({"zpool", "zfs"}),
                 sudo: str = "allow", overrides: dict | None = None) -> None:
        self.test = test
        self.missing = set(missing)
        self.sudo = sudo
        self.overrides = {tuple(k): v for k, v in (overrides or {}).items()}
        self.links: dict[str, str | None] = {}   # what root's `realpath -e` answers (None: it fails)
        self.calls: list[tuple[str, ...]] = []

    def which(self, program: str) -> bool:
        return program not in self.missing

    def run(self, argv: list[str], timeout: float) -> Result:
        argv_t = tuple(argv)
        self.calls.append(argv_t)
        inner = argv_t[3:] if argv_t[:3] == ("sudo", "-n", "--") else argv_t
        self.guard(argv_t, inner)
        self.test.assertGreater(timeout, 0)
        if inner is not argv_t and self.sudo == "refuse":
            return Result(1, "", "sudo: a password is required\n", 0.01)
        if inner[0] in self.missing:
            self.test.fail(f"a missing command was run: {argv_t}")
        answer = self.answer(inner)
        if isinstance(answer, Result):   # a timeout or a start error
            return answer
        exit_code, out, err = answer
        return Result(exit_code, out, err, 0.01)

    def guard(self, argv: tuple[str, ...], inner: tuple[str, ...]) -> None:
        if inner[0] in NEVER_PROGRAMS or (inner[0] == "docker" and (set(inner[1:3]) & NEVER_DOCKER)):
            self.test.fail(f"a command that can change the host reached the runner: {argv}")
        if inner[0] == "systemctl" and inner[1] not in ("list-units", "list-unit-files", "list-timers", "show",
                                                        "is-system-running"):
            self.test.fail(f"systemctl {inner[1]} reached the runner: {argv}")
        try:
            check_argv(list(argv))
        except UnsafeCommand as exc:
            self.test.fail(f"a non-allowlisted command reached the runner: {argv}: {exc}")

    def answer(self, argv: tuple[str, ...]) -> tuple[int, str, str]:
        if argv in self.overrides:
            return self.overrides[argv]
        if argv in OUTPUTS:
            return OUTPUTS[argv]
        if argv[0] == "cat" and len(argv) == 2:
            text = FILES.get(argv[1])
            return (0, text, "") if text is not None else (1, "", f"cat: {argv[1]}: No such file or directory\n")
        if argv[0] == "ls" and len(argv) == 3 and argv[1:] in LISTINGS:
            return 0, LISTINGS[argv[1:]], ""
        if argv[0] == "ls":
            return 2, "", f"ls: cannot access '{argv[-1]}': No such file or directory\n"
        if argv[:3] == ("systemctl", "show", "--no-pager"):
            return 0, show_blocks([unit for unit in argv[4:] if unit in UNITS]), ""
        if argv[:2] == ("docker", "inspect"):
            return 0, json.dumps([c for c in CONTAINERS if c["Id"] in argv[2:]]), ""
        if argv[0] == "nvidia-smi" and argv[1].startswith("--query-gpu="):
            return 0, "0, NVIDIA GeForce RTX 3090, GPU-1, 550.120\n1, NVIDIA GeForce RTX 3090, GPU-2, 550.120\n", ""
        if argv[0] == "nvidia-smi" and argv[1].startswith("--query-compute-apps="):
            return 0, "", ""
        if argv[:3] == ("realpath", "-e", "--"):
            target = self.links.get(argv[3], argv[3])
            if target is None:
                return 1, "", f"realpath: {argv[3]}: Too many levels of symbolic links\n"
            return 0, target + "\n", ""
        if argv[0] == "find":
            return 0, "/opt/stacks/ai/compose.yaml\n/srv/media/docker-compose.yml\n", ""
        if argv[0] in ("borg", "kopia", "snapraid", "timeshift"):
            return 127, "", ""
        self.test.fail(f"a command the fixture does not expect: {argv}")
        raise AssertionError


def fake_follow(links: dict[str, str] | None = None, walls: Iterable[str] = ()) -> Any:
    """Patch the collector's symlink resolution: links maps a path to where it leads, a path in walls
    cannot be followed by this user (PermissionError), anything else leads to itself."""
    blocked = set(walls)

    def follow(path: str) -> str:
        if path in blocked:
            raise PermissionError(13, "Permission denied", path)
        return (links or {}).get(path, path)
    return mock.patch.object(bp, "_follow", follow)


class CollectTestCase(unittest.TestCase):
    def setUp(self) -> None:
        for patcher in (fake_follow(), mock.patch.object(bp, "systemd_booted", lambda: True)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def capture(self, *, missing: set[str] | None = None, overrides: dict | None = None, sudo_mode: str = "allow",
                known_secrets: tuple[str, ...] | list[str] = (KNOWN,), sudo: bool = False) -> tuple[dict, FakeRunner]:
        runner = FakeRunner(self, sudo=sudo_mode, overrides=overrides,
                            **({"missing": missing} if missing is not None else {}))
        return collect(runner, sudo=sudo, known_secrets=list(known_secrets)), runner


class SnapshotFormatTest(CollectTestCase):
    def test_the_capture_format(self) -> None:
        snapshot, _ = self.capture()
        self.assertEqual(list(snapshot), ["schema_version", "host", "collected_at", "scope", "redaction", *SECTIONS,
                                          "indexes", "scope_limits", "redaction_review", "summary"])
        self.assertEqual(snapshot["schema_version"], "0")
        self.assertEqual(snapshot["host"], {"hostname": "seraphina-test", "os": "Ubuntu 24.04.1 LTS",
                                            "kernel": "6.8.0-45-generic", "machine": "x86_64", "virtualization": "none"})
        self.assertRegex(snapshot["collected_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual((snapshot["scope"]["mode"], snapshot["scope"]["sudo"]), ("read-only", False))
        self.assertEqual(snapshot["redaction"]["marker"], MARK)
        for section in SECTIONS:
            self.assertTrue(snapshot[section], f"{section} has probes")
            for name, record in snapshot[section].items():
                with self.subTest(probe=f"{section}.{name}"):
                    self.assertLessEqual({"command", "exit_code", "stdout", "stderr", "seconds", "status"}, set(record))
                    self.assertIsInstance(record["command"], list)
                    self.assertIn(record["status"], ("ok", "nonzero_exit", "denied", "missing_command", "timeout",
                                                     "skipped_by_policy", "error"))
        self.assertEqual(list(snapshot["indexes"]), list(INDEXES))
        self.assertEqual(snapshot["host_baseline"]["hostname"],
                         {"command": ["hostname"], "exit_code": 0, "stdout": "seraphina-test\n", "stderr": "",
                          "seconds": 0.01, "status": "ok"})
        json.dumps(snapshot)   # it is plain JSON

    def test_summary_is_computed_from_the_indexes(self) -> None:
        snapshot, _ = self.capture()
        summary, indexes = snapshot["summary"], snapshot["indexes"]
        self.assertEqual(summary["containers"], {"total": 4, "exited": 1, "running": 3})
        self.assertEqual(summary["gpus"], 2)
        self.assertEqual(summary["host_packages"], {"dpkg": 3, "snap": 1, "held": 1})
        self.assertEqual(summary["systemd_units"], {"total": 6, "active": 4, "failed": 1})
        self.assertEqual(summary["compose_files"], {"found": 3, "read": 2, "listed_only": 1})
        self.assertEqual(summary["relationships"]["total"], len(indexes["relationships"]))
        self.assertEqual(summary["endpoints"]["total"], len(indexes["endpoints"]))
        self.assertEqual(summary["backup_tools"], ["restic"])
        self.assertEqual(summary["backup_units"], ["restic-backup.timer"])
        self.assertEqual(summary["probes"]["total"], sum(len(snapshot[s]) for s in SECTIONS))
        self.assertEqual(summary["computed_by"], "collector")


class SafetyTest(CollectTestCase):
    def test_every_command_is_allowlisted_and_read_only(self) -> None:
        """FakeRunner fails the test on anything else; this pins what a capture runs."""
        _, runner = self.capture()
        programs = {call[0] for call in runner.calls}
        self.assertLessEqual(programs, set(bp.RULES))
        self.assertNotIn("lxc", programs)
        self.assertIn(("docker", "inspect", *IDS.values()), runner.calls)
        self.assertIn(("cat", "/opt/stacks/ai/compose.yaml"), runner.calls)
        self.assertFalse(any(call[0] == "cat" and call[1].startswith("/home/") for call in runner.calls))
        self.assertFalse(any("reboot" in " ".join(call) for call in runner.calls), "a device name from smartctl --scan "
                         "that is not a plain device is never used")

    def test_a_probe_outside_the_allowlist_never_runs(self) -> None:
        for argv in (["lxc", "list"], ["docker", "start", "old-llm"], ["docker", "exec", "open-webui", "ls"],
                     ["docker", "pull", "nginx"], ["apt-get", "install", "-y", "jq"], ["snap", "install", "jq"],
                     ["cat", "/home/owner/.bashrc"], ["systemctl", "restart", "docker.service"], ["sh", "-c", "id"]):
            with self.subTest(argv=argv):
                runner = FakeRunner(self)
                collector = Collector(runner)
                with self.assertRaises(UnsafeCommand):
                    collector.probe("host_baseline", "bad", argv)
                self.assertEqual(runner.calls, [], "nothing reached the runner")

    def test_a_tampered_probe_list_stops_the_capture(self) -> None:
        runner = FakeRunner(self)
        with mock.patch.object(Collector, "_backups", lambda self: self.probe("backups", "lxd", ["lxc", "info"])):
            with self.assertRaisesRegex(UnsafeCommand, "on-demand LXD installer"):
                collect(runner, known_secrets=[])
        self.assertNotIn("lxc", {call[0] for call in runner.calls})

    def test_missing_commands_are_recorded_not_run(self) -> None:
        snapshot, runner = self.capture(missing={"nvidia-smi", "tailscale", "zpool", "zfs"})
        self.assertEqual(snapshot["gpu"]["list"]["status"], "missing_command")
        self.assertEqual(list(snapshot["gpu"]), ["list"], "no other GPU probe is tried")
        self.assertEqual(snapshot["tailscale"]["version"]["error"], "tailscale: command not found (not installed here)")
        self.assertFalse(any(call[0] in ("nvidia-smi", "tailscale", "zpool", "zfs") for call in runner.calls))
        self.assertIn("Commands not installed here were recorded, not installed: nvidia-smi, tailscale, zfs, zpool.",
                      snapshot["scope_limits"])

    def test_without_a_unit_list_docker_and_snap_are_not_asked(self) -> None:
        """systemd runs, but list-units timed out or could not reach the bus: fail closed."""
        for label, answer in (("timeout", Result(None, "", "", 15.0, timed_out=True)),
                              ("no bus", (1, "", "Failed to connect to bus: Connection refused\n"))):
            with self.subTest(label):
                snapshot, runner = self.capture(overrides={LIST_UNITS: answer})
                self.assertFalse(any(call[0] in ("docker", "snap") for call in runner.calls))
                for record in (snapshot["docker"]["version"], snapshot["docker"]["containers"],
                               snapshot["packages"]["snaps"]):
                    self.assertEqual(record["status"], "skipped_by_policy")
                    self.assertIn("unit list is unavailable", record["error"])
                self.assertTrue(any(l.startswith("systemd's unit list was unavailable, so Docker and snap were not "
                                                 "asked") for l in snapshot["scope_limits"]))

    def test_without_systemd_docker_and_snap_are_asked(self) -> None:
        with mock.patch.object(bp, "systemd_booted", lambda: False):
            snapshot, runner = self.capture(overrides={
                LIST_UNITS: (1, "", "System has not been booted with systemd as init system (PID 1). Can't operate.\n")})
        self.assertIn(("docker", "version", "--format", "{{json .}}"), runner.calls)
        self.assertIn(("snap", "list"), runner.calls)
        self.assertTrue(any(l.startswith("systemd is not this host's init, so nothing is socket-activated")
                            for l in snapshot["scope_limits"]))

    def test_docker_and_snap_are_not_woken_through_their_sockets(self) -> None:
        stopped = {name: spec for name, spec in UNITS.items()}
        stopped["docker.service"] = ("enabled", "loaded", "inactive", "dead", "Docker", {})
        stopped["snapd.service"] = ("enabled", "loaded", "inactive", "dead", "Snap Daemon", {})
        with mock.patch.dict(UNITS, stopped):
            listing = "".join(f"{n} {s[1]} {s[2]} {s[3]} {s[4]}\n" for n, s in UNITS.items())
            snapshot, runner = self.capture(overrides={
                ("systemctl", "list-units", "--all", "--no-legend", "--no-pager", "--plain", "--full"): (0, listing, "")})
        self.assertFalse(any(call[0] in ("docker", "snap") for call in runner.calls))
        self.assertEqual(snapshot["docker"]["containers"]["status"], "skipped_by_policy")
        self.assertIn("docker.socket", snapshot["docker"]["version"]["error"])
        self.assertEqual(snapshot["docker"]["daemon_json"]["status"], "ok", "its config file is still read")
        self.assertEqual(snapshot["packages"]["snaps"]["status"], "skipped_by_policy")
        self.assertTrue(any(l.startswith("Docker and snap were not asked when systemd listed their daemon as stopped")
                            for l in snapshot["scope_limits"]))

    def test_home_directories_and_docker_data_are_listed_not_read(self) -> None:
        snapshot, _ = self.capture()
        home = snapshot["compose_files"]["file:/home/owner/old/docker-compose.yml"]
        self.assertEqual((home["status"], home["error"]), ("skipped_by_policy",
                                                           "under a home directory: listed, not read"))
        entry = next(e for e in snapshot["indexes"]["compose_files"] if e["path"].startswith("/home/"))
        self.assertEqual((entry["read"], entry["found_by"], entry["containers"]), (False, ["docker"], ["old-llm"]))
        ai = next(e for e in snapshot["indexes"]["compose_files"] if e["path"] == "/opt/stacks/ai/compose.yaml")
        self.assertEqual((ai["read"], ai["found_by"], ai["services"]), (True, ["docker", "search"],
                                                                        ["open-webui", "ts-webui"]))
        self.assertEqual(ai["containers"], ["open-webui", "ts-webui"])
        self.assertNotIn("cat", [c["command"][0] for c in snapshot["cron"].values()
                                 if c["command"][-1].endswith("/.placeholder")])

    def test_a_symlink_into_a_home_directory_is_not_followed(self) -> None:
        with fake_follow({"/srv/media/docker-compose.yml": "/home/owner/stack/compose.yaml"}):
            snapshot, runner = self.capture()
        record = snapshot["compose_files"]["file:/srv/media/docker-compose.yml"]
        self.assertEqual(record["status"], "skipped_by_policy")
        self.assertIn("resolves to /home/owner/stack/compose.yaml", record["error"])
        self.assertNotIn(("cat", "/srv/media/docker-compose.yml"), runner.calls)

    def test_a_found_file_must_lead_to_a_file_a_probe_may_read(self) -> None:
        """A compose-named link to a database is not read; a fixed config file may be a link (Ubuntu's
        /etc/os-release and /etc/resolv.conf are), but never into a never-read place."""
        with fake_follow({"/srv/media/docker-compose.yml": "/var/lib/app/users.db",
                          "/opt/stacks/ai/compose.yaml": "/srv/real/docker-compose.yml",
                          "/etc/os-release": "/usr/lib/os-release",
                          "/etc/hosts": "/etc/shadow",
                          "/etc/cron.d/backup": "/usr/share/backup/cron"}):
            snapshot, runner = self.capture()
        media = snapshot["compose_files"]["file:/srv/media/docker-compose.yml"]
        self.assertEqual(media["status"], "skipped_by_policy")
        self.assertIn("resolves to /var/lib/app/users.db, which is never read", media["error"])
        self.assertNotIn(("cat", "/srv/media/docker-compose.yml"), runner.calls)
        self.assertIn(("cat", "/opt/stacks/ai/compose.yaml"), runner.calls, "a link to another compose file is read")
        self.assertEqual(snapshot["host_baseline"]["os_release"]["status"], "ok")
        self.assertEqual(snapshot["network"]["hosts"]["status"], "skipped_by_policy")
        self.assertEqual(snapshot["cron"]["cron.d/backup"]["status"], "skipped_by_policy",
                         "a listed file must lead to a file in a read directory")

    def test_real_symlinks_are_followed_to_the_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(bp, "_follow",
                                                                     lambda path: os.path.realpath(path, strict=True)):
            root = Path(tmp).resolve()
            if str(root).startswith(bp.HOME_ROOTS):
                self.skipTest("the temporary directory is under a home directory")
            (root / "db").mkdir()
            (root / "db" / "users.db").write_text("row1|alice\n")
            (root / "real").mkdir()
            (root / "real" / "compose.yaml").write_text("services: {}\n")
            for name, target in (("to-db", "db/users.db"), ("hop", "to-db"), ("to-compose", "real/compose.yaml"),
                                 ("dangling", "nowhere/compose.yaml")):
                (root / name).mkdir()
                (root / name / "compose.yaml").symlink_to(root / target if name != "hop" else root / "to-db"
                                                          / "compose.yaml")
            collector = Collector(FakeRunner(self))
            records = {name: collector.probe("compose_files", name, ["cat", str(root / name / "compose.yaml")],
                                             kind="compose") for name in ("to-db", "hop", "to-compose", "dangling")}
        self.assertEqual(records["to-db"]["status"], "skipped_by_policy")
        self.assertIn(f"resolves to {root}/db/users.db", records["hop"]["error"], "a chain is followed to its end")
        self.assertEqual(records["to-compose"]["status"], "nonzero_exit", "read (the fake has no such file)")
        self.assertEqual(records["dangling"]["status"], "nonzero_exit", "nothing there: cat fails the same way")

    def test_with_sudo_root_resolves_what_this_user_cannot_see(self) -> None:
        runner = FakeRunner(self)
        runner.links["/opt/stacks/ai/compose.yaml"] = "/root/private/compose.yaml"
        runner.links["/etc/ufw/user.rules"] = None   # root cannot follow it either (a loop)
        with fake_follow(walls={"/opt/stacks/ai/compose.yaml", "/srv/media/docker-compose.yml",
                                "/etc/ufw/user.rules"}):
            snapshot = collect(runner, sudo=True, known_secrets=[])
        self.assertIn(("sudo", "-n", "--", "realpath", "-e", "--", "/opt/stacks/ai/compose.yaml"), runner.calls)
        record = snapshot["compose_files"]["file:/opt/stacks/ai/compose.yaml"]
        self.assertEqual(record["status"], "skipped_by_policy")
        self.assertIn("resolves to /root/private/compose.yaml", record["error"])
        self.assertNotIn(("sudo", "-n", "--", "cat", "/opt/stacks/ai/compose.yaml"), runner.calls)
        self.assertIn(("sudo", "-n", "--", "cat", "/srv/media/docker-compose.yml"), runner.calls,
                      "root says it leads to itself")
        rules = snapshot["network"]["ufw_rules"]
        self.assertEqual(rules["status"], "skipped_by_policy", "a way root cannot follow is not read (fail closed)")
        self.assertIn("could not be followed to its end as root", rules["error"])
        self.assertNotIn(("sudo", "-n", "--", "cat", "/etc/ufw/user.rules"), runner.calls)
        self.assertNotIn(("sudo", "-n", "--", "realpath", "-e", "--", "/etc/hosts"), runner.calls,
                         "root is asked only when this user cannot follow the way")

    def test_a_sudo_that_refuses_while_resolving_falls_back_to_this_user(self) -> None:
        runner = FakeRunner(self, sudo="refuse")
        collector = Collector(runner, sudo=True)
        with fake_follow(walls={"/etc/ufw/user.rules"}):
            record = collector.probe("network", "ufw_rules", ["cat", "/etc/ufw/user.rules"], root=True)
        self.assertEqual(runner.calls, [("sudo", "-n", "--", "realpath", "-e", "--", "/etc/ufw/user.rules"),
                                        ("cat", "/etc/ufw/user.rules")],
                         "the read runs as this user, who meets the same wall; sudo is not tried again")
        self.assertEqual(record["command"], ["cat", "/etc/ufw/user.rules"])
        self.assertEqual(collector.sudo_refused, ["network.ufw_rules"])

    def test_files_a_search_or_listing_names_are_listed_not_read_when_refused(self) -> None:
        """A name only the data supplies never stops the capture: it is recorded, not raised."""
        listings = dict(LISTINGS)
        listings[("-1A", "/etc/cron.d")] = "backup\ndeploy.key\nshadow-backup\n"
        overrides = {("docker", "compose", "ls", "--all", "--format", "json"):
                     (0, json.dumps([{"Name": "ai", "ConfigFiles": "/opt/stacks/ai/compose.yaml,"
                                                                   "/dev/shm/stack/compose.yaml"}]), "")}

        class Runner(FakeRunner):
            def answer(self, argv: tuple[str, ...]) -> Any:
                if argv[0] == "find":
                    return 0, "/opt/stacks/proxy/docker-compose.shadowsocks.yml\n", ""
                return super().answer(argv)
        runner = Runner(self, overrides=overrides)
        with mock.patch.dict(LISTINGS, listings):
            snapshot = collect(runner, known_secrets=[])
        shm = snapshot["compose_files"]["file:/dev/shm/stack/compose.yaml"]
        self.assertEqual((shm["status"], shm["error"]), ("skipped_by_policy",
                                                         "under /proc, /sys or /dev: listed, not read"))
        key = snapshot["cron"]["cron.d/deploy.key"]
        self.assertEqual(key["status"], "skipped_by_policy")
        self.assertIn("key, env file or password database", key["error"])
        self.assertIn(("cat", "/opt/stacks/proxy/docker-compose.shadowsocks.yml"), runner.calls,
                      "a Shadowsocks stack is not a password database")
        self.assertIn(("cat", "/etc/cron.d/shadow-backup"), runner.calls)
        self.assertIn("summary", snapshot, "the capture finished")
        for argv in (["cat", "/etc/shadow"], ["cat", "/etc/gshadow-"], ["cat", "/srv/x/shadow.bak"]):
            with self.subTest(argv=argv), self.assertRaises(UnsafeCommand):
                check_argv(argv)


class SudoTest(CollectTestCase):
    def test_root_probes_go_through_sudo_n(self) -> None:
        snapshot, runner = self.capture(sudo=True)
        self.assertIn(("sudo", "-n", "--", "ss", "-H", "-l", "-n", "-t", "-u", "-p"), runner.calls)
        self.assertIn(("sudo", "-n", "--", "docker", "ps", "-a", "--no-trunc", "--format", "{{json .}}"), runner.calls)
        self.assertIn(("hostname",), runner.calls, "probes that need no root run as the user")
        self.assertTrue(snapshot["scope"]["sudo"])
        self.assertEqual(snapshot["network"]["listeners"]["command"][:3], ["sudo", "-n", "--"])

    def test_a_sudo_that_wants_a_password_falls_back_once(self) -> None:
        snapshot, runner = self.capture(sudo=True, sudo_mode="refuse")
        sudo_calls = [call for call in runner.calls if call[0] == "sudo"]
        self.assertEqual(len(sudo_calls), 1, "after the first refusal, sudo is not tried again")
        first = snapshot["storage"]["smart_scan"]
        self.assertEqual(first["command"], ["smartctl", "--scan", "-j"])
        self.assertIn("sudo refused (sudo: a password is required); ran without sudo", first["error"])
        self.assertTrue(any(limit.startswith("sudo -n was refused") for limit in snapshot["scope_limits"]))

    def test_without_sudo_installed_root_probes_run_without_it(self) -> None:
        snapshot, runner = self.capture(sudo=True, missing={"sudo", "zpool", "zfs"})
        self.assertFalse(any(call[0] == "sudo" for call in runner.calls))
        self.assertIn(("ss", "-H", "-l", "-n", "-t", "-u", "-p"), runner.calls)
        self.assertEqual(snapshot["network"]["listeners"]["status"], "ok")
        self.assertEqual(snapshot["summary"]["containers"]["total"], 4, "Docker is still asked")
        self.assertTrue(any(l.startswith("sudo is not installed here, so root-only probes ran without it")
                            for l in snapshot["scope_limits"]))
        self.assertIn("Commands not installed here were recorded, not installed: zfs, zpool.", snapshot["scope_limits"])
        self.assertIn("(sudo is not installed)", snapshot["scope"]["description"])

    def test_check_argv_wraps_only_allowed_commands_in_sudo(self) -> None:
        check_argv(["sudo", "-n", "--", "ss", "-H", "-l", "-n", "-t", "-u", "-p"])
        for argv in (["sudo", "ss", "-H"], ["sudo", "-n", "--", "sudo", "-n", "--", "hostname"],
                     ["sudo", "-n", "--", "lxc", "list"], ["sudo", "-n", "-u", "root", "--", "hostname"]):
            with self.subTest(argv=argv), self.assertRaises(UnsafeCommand):
                check_argv(argv)


class AllowlistTest(unittest.TestCase):
    ALLOWED = [
        ["hostname"], ["uname", "-a"], ["cat", "/etc/os-release"], ["cat", "/etc/cron.d/backup"],
        ["cat", "/opt/stacks/ai/compose.yaml"], ["cat", "/srv/x/docker-compose.override.yml"],
        ["ls", "-1A", "/etc/cron.d"], ["systemctl", "show", "--no-pager", "--property=Id,After", "ssh.service"],
        ["docker", "inspect", "a" * 64], ["docker", "network", "inspect", "ai"],
        ["find", "/opt", "/srv", "-xdev", "-maxdepth", "3", "-type", "f", "(", "-name", "compose.yaml", ")", "-print"],
        ["smartctl", "-j", "-n", "standby", "-i", "-H", "-A", "-d", "sat", "/dev/sda"],
        ["nvidia-smi", "--query-gpu=index,name", "--format=csv,noheader"], ["ip", "-j", "-6", "route", "show"],
        ["journalctl", "--disk-usage"], ["tailscale", "serve", "status", "--json"], ["crontab", "-l"],
    ]
    REFUSED = [
        ["lxc", "list"], ["lxd", "init"], ["apt", "list", "--upgradable"], ["apt-get", "-s", "upgrade"],
        ["dpkg", "-l"], ["snap", "install", "jq"], ["snap", "refresh"], ["pip", "list"], ["npm", "ls", "-g"],
        ["docker", "start", "x"], ["docker", "exec", "x", "id"], ["docker", "run", "alpine"], ["docker", "pull", "x"],
        ["docker", "logs", "x"], ["docker", "compose", "up"], ["docker", "inspect", "--format", "{{.Config.Env}}"],
        ["docker", "inspect", "$(id)"], ["systemctl", "restart", "docker.service"], ["systemctl", "daemon-reload"],
        ["systemctl", "show", "docker.service"], ["systemctl", "show", "--property=Id", "-H", "host", "x.service"],
        ["cat", "/etc/shadow"], ["cat", "/home/owner/.env"], ["cat", "/home/owner/stack/compose.yaml"],
        ["cat", "/root/compose.yml"], ["cat", "/srv/app/.env"], ["cat", "/etc/ssl/private/server.key"],
        ["cat", "/var/lib/docker/volumes/x/_data/compose.yml"], ["cat", "/etc/cron.d/../shadow"], ["cat", "etc/hosts"],
        ["cat", "/etc/hosts", "/etc/shadow"], ["cat", "/srv/app/config.yml"],
        ["cat", "/proc/self/root/home/owner/compose.yml"], ["realpath", "/etc/hosts"],
        ["ls", "-la", "/home/owner"], ["ls", "/etc/cron.d"],
        ["find", "/", "-xdev", "-maxdepth", "3", "-type", "f", "(", "-name", "compose.yaml", ")", "-print"],
        ["find", "/opt", "-xdev", "-maxdepth", "3", "-type", "f", "(", "-name", "compose.yaml", ")", "-delete"],
        ["find", "/opt", "-xdev", "-maxdepth", "3", "-type", "f", "(", "-name", "*", ")", "-print"],
        ["find", "/opt", "-xdev", "-maxdepth", "9", "-type", "f", "(", "-name", "compose.yaml", ")", "-print"],
        ["ss", "-K", "dst", "1.2.3.4"], ["ip", "addr", "add", "10.0.0.1/24", "dev", "eth0"], ["ip", "-j", "link", "set"],
        ["hostname", "newname"], ["swapon", "/dev/sdb"], ["nvidia-smi", "-pl", "200"], ["nvidia-smi", "-r"],
        ["smartctl", "-t", "short", "/dev/sda"], ["smartctl", "-s", "on", "/dev/sda"],
        ["smartctl", "-j", "-n", "standby", "-i", "-H", "-A", "/dev/sda;reboot"],
        ["journalctl", "--vacuum-size=1M"], ["journalctl", "-u", "ssh"], ["crontab", "-r"], ["crontab", "/tmp/x"],
        ["tailscale", "up"], ["tailscale", "serve", "--bg", "80"], ["netplan", "apply"], ["ufw", "status"],
        ["nft", "flush", "ruleset"], ["zpool", "scrub", "tank"], ["timedatectl", "set-ntp", "true"],
        ["/usr/bin/hostname"], ["./hostname"], ["restic", "snapshots"], ["kopia", "snapshot", "list"],
        ["timeshift", "--list"], ["uname", "-a\nreboot"], [], "hostname",
    ]

    def test_allowed_shapes(self) -> None:
        for argv in self.ALLOWED:
            with self.subTest(argv=argv):
                check_argv(argv)

    def test_refused_shapes(self) -> None:
        for argv in self.REFUSED:
            with self.subTest(argv=argv), self.assertRaises(UnsafeCommand):
                check_argv(argv)   # type: ignore[arg-type]


class RedactionTest(CollectTestCase):
    def test_no_secret_survives_into_the_snapshot_or_the_overview(self) -> None:
        snapshot, _ = self.capture()
        text = json.dumps(snapshot) + overview(snapshot)
        for secret in SECRETS:
            with self.subTest(secret=secret):
                self.assertNotIn(secret, text)
        counts = snapshot["redaction_review"]["counts"]
        for category in ("container_env", "secret_key", "secret_assignment", "secret_argument", "compose_environment",
                         "private_key", "jwt", "credential_url", "known_secret"):
            self.assertGreater(counts[category], 0, category)
        self.assertEqual(snapshot["redaction_review"]["total"], sum(counts.values()))
        self.assertIn("limitation", snapshot["redaction_review"])

    def test_what_redaction_keeps(self) -> None:
        snapshot, _ = self.capture(known_secrets=[])
        webui = next(c for c in json.loads(snapshot["docker"]["inspect:1"]["stdout"]) if c["Name"] == "/open-webui")
        self.assertEqual(webui["Config"]["Env"], ["OPENAI_API_KEY=[REDACTED]", "WEBUI_NAME=[REDACTED]", "EMPTY="])
        self.assertEqual(webui["Config"]["Labels"]["com.docker.compose.project"], "ai")
        self.assertEqual(webui["Config"]["Labels"]["cosmos-auth-token"], MARK)
        old = next(c for c in json.loads(snapshot["docker"]["inspect:1"]["stdout"]) if c["Name"] == "/old-llm")
        self.assertEqual(old["Config"]["Cmd"], ["--api-key", MARK, "--port", "8000"])
        cosmos = json.loads(snapshot["cosmos"]["config"]["stdout"])
        self.assertEqual(cosmos["HTTPConfig"]["AuthPrivateKey"], MARK)
        self.assertEqual(cosmos["MongoDB"], f"mongodb://{MARK}@127.0.0.1:27017/")
        self.assertIs(cosmos["HTTPConfig"]["ProxyConfig"]["Routes"][0]["AuthEnabled"], False, "booleans stay")
        self.assertEqual(cosmos["EmailConfig"]["Username"], "owner")
        compose = snapshot["compose_files"]["file:/opt/stacks/ai/compose.yaml"]["stdout"]
        self.assertIn("      - OPENAI_API_KEY=[REDACTED]\n      - WEBUI_NAME=[REDACTED]\n", compose)
        self.assertIn("      TS_AUTHKEY: [REDACTED]\n      TS_HOSTNAME: [REDACTED]\n", compose)
        self.assertIn('      - "127.0.0.1:3000:8080"\n', compose)
        self.assertEqual(snapshot["cron"]["cron.d/backup"]["stdout"],
                         "RESTIC_PASSWORD=[REDACTED]\n0 3 * * * root restic backup /srv\n")
        self.assertIn("password=[REDACTED] 0 0", snapshot["storage"]["fstab"]["stdout"])
        self.assertIn("username=owner", snapshot["storage"]["fstab"]["stdout"])
        self.assertEqual(snapshot["host_baseline"]["os_release"]["stdout"], FILES["/etc/os-release"], "untouched")
        status = json.loads(snapshot["tailscale"]["status"]["stdout"])
        self.assertEqual(status["Self"]["PublicKey"], MARK)
        self.assertEqual(status["Peer"], {"nodekey:00ff": {"HostName": "laptop"}}, "a key-shaped map key is not a name")
        self.assertEqual(snapshot["network"]["netplan"]["redactions"], 1)

    def test_wireguard_keys_from_netplan_and_compose_argv_secrets_are_redacted(self) -> None:
        compose = ("services:\n  llm:\n    image: vllm/vllm-openai\n    command:\n      - --api-key\n"
                   "      - sk-compose-list-0019\n      - --port\n      - \"8000\"\n  bot:\n    image: some/bot\n"
                   "    entrypoint: [\"serve\", \"--token\", \"sk-compose-flow-0020\"]\n")
        with mock.patch.dict(FILES, {"/srv/media/docker-compose.yml": compose}):
            snapshot, _ = self.capture(sudo=True, overrides={("netplan", "get"): (0, NETPLAN_WG, "")})
        text = json.dumps(snapshot)
        for secret in (WG_PRIVATE, WG_SHARED, "sk-compose-list-0019", "sk-compose-flow-0020"):
            self.assertNotIn(secret, text)
        netplan = snapshot["network"]["netplan"]["stdout"]
        self.assertIn('      keys:\n        private: "[REDACTED]"\n      peers:\n      - endpoint: "1.2.3.4:5"\n',
                      netplan)
        self.assertIn('          shared: "[REDACTED]"\n        allowed-ips:\n        - "0.0.0.0/0"\n', netplan)
        stored = snapshot["compose_files"]["file:/srv/media/docker-compose.yml"]["stdout"]
        self.assertIn("      - --api-key\n      - [REDACTED]\n      - --port\n      - \"8000\"\n", stored)
        self.assertIn('entrypoint: ["serve", "--token", "[REDACTED]"]', stored)

    def test_json_that_does_not_parse_is_withheld(self) -> None:
        snapshot, _ = self.capture(overrides={("docker", "inspect", *IDS.values()):
                                              (0, '[{"Config": {"Env": ["A=sk-live-openwebui-0001"]', "")})
        record = snapshot["docker"]["inspect:1"]
        self.assertTrue(record["stdout"].startswith(f"{MARK}: output withheld"))
        self.assertNotIn("sk-live-openwebui-0001", json.dumps(snapshot))
        self.assertEqual(snapshot["redaction_review"]["withheld"], ["docker.inspect:1"])

    def test_json_with_a_failing_exit_is_still_redacted_by_structure(self) -> None:
        """docker inspect exits 1 when one container vanished after ps, and still prints the others."""
        partial = json.dumps([CONTAINERS[0]])
        snapshot, _ = self.capture(overrides={("docker", "inspect", *IDS.values()):
                                              (1, partial, "Error: No such object: " + IDS["dockge"] + "\n")})
        self.assertEqual(snapshot["docker"]["inspect:1"]["status"], "nonzero_exit")
        self.assertNotIn("sk-live-openwebui-0001", json.dumps(snapshot))
        self.assertEqual(list(snapshot["indexes"]["containers"]), ["open-webui"])


class IndexTest(CollectTestCase):
    def test_containers(self) -> None:
        snapshot, _ = self.capture()
        containers = snapshot["indexes"]["containers"]
        self.assertEqual(list(containers), ["dockge", "old-llm", "open-webui", "ts-webui"])
        webui, old = containers["open-webui"], containers["old-llm"]
        self.assertEqual((webui["state"], webui["restart_policy"], webui["health"]), ("running", "unless-stopped",
                                                                                     "healthy"))
        self.assertEqual(webui["networks"], {"ai": {"ip": "172.20.0.5", "static_ip": "172.20.0.5"}})
        self.assertEqual(webui["ports"], [{"container_port": "8080", "protocol": "tcp", "host_ip": "127.0.0.1",
                                           "host_port": "3000"}])
        self.assertEqual((webui["compose_project"], webui["compose_config_files"], webui["env_count"]),
                         ("ai", ["/opt/stacks/ai/compose.yaml"], 3))
        self.assertEqual((old["state"], old["restart_policy"], old["gpu"]), ("exited", "always", True))
        self.assertTrue(containers["dockge"]["docker_socket"])

    def test_endpoints(self) -> None:
        snapshot, _ = self.capture()
        points = {(e["source"], e["address"], e["port"]): e for e in snapshot["indexes"]["endpoints"]}
        self.assertEqual(points[("ss", "127.0.0.1", 3000)]["processes"], ["docker-proxy"])
        self.assertEqual(points[("ss", "::", 22)]["scope"], "all")
        self.assertEqual(points[("ss", "100.101.102.103", 443)]["scope"], "tailnet")
        self.assertEqual((points[("ss", "127.0.0.53", 53)]["interface"], points[("ss", "127.0.0.53", 53)]["scope"]),
                         ("lo", "loopback"))
        self.assertEqual(points[("docker", "0.0.0.0", 8000)]["container"], "old-llm")
        self.assertEqual(points[("docker", "0.0.0.0", 8000)]["scope"], "all")

    def test_relationships_carry_evidence(self) -> None:
        snapshot, _ = self.capture()
        edges = snapshot["indexes"]["relationships"]
        for edge in edges:
            self.assertTrue(edge["evidence"] and edge["confidence"], edge)

        def has(source: str, relation: str, target: str) -> dict:
            found = [e for e in edges if (e["from"], e["relation"], e["to"]) == (source, relation, target)]
            self.assertTrue(found, f"{source} {relation} {target}")
            return found[0]
        self.assertEqual(has("container:open-webui", "mounts", "volume:open-webui")["destination"], "/app/backend/data")
        self.assertEqual(has("container:dockge", "mounts", "path:/opt/stacks")["mode"], "ro")
        has("container:dockge", "docker_socket_mounted", "path:/var/run/docker.sock")
        self.assertEqual(has("container:open-webui", "attached_to", "network:ai")["static_ip"], "172.20.0.5")
        self.assertIn("not proof", has("container:old-llm", "gpu_access_configured", "gpu:all")["confidence"])
        self.assertIn("can have changed", has("container:open-webui", "defined_by",
                                              "compose:/opt/stacks/ai/compose.yaml")["confidence"])
        has("container:old-llm", "publishes", "endpoint:tcp/0.0.0.0:8000")
        has("process:sshd", "listens_on", "endpoint:tcp/:::22")
        has("tailscale-serve:webui.tail1234.ts.net:443/", "routes_to", "url:http://127.0.0.1:3000")
        has("tailscale-serve:tcp/5432", "routes_to", "tcp:127.0.0.1:5432")
        self.assertIs(has("cosmos-route:webui", "routes_to", "url:http://open-webui:8080")["auth"], False)
        self.assertEqual(has("unit:docker.service", "Requires", "unit:docker.socket")["confidence"], "systemd declared")
        has("unit:docker.service", "Wants", "unit:containerd.service")
        has("unit:docker.service", "After", "unit:network-online.target")
        has("unit:restic-backup.timer", "Triggers", "unit:restic-backup.service")

    def test_units_and_packages(self) -> None:
        snapshot, _ = self.capture()
        units, packages = snapshot["indexes"]["systemd_units"], snapshot["indexes"]["host_packages"]
        docker = units["docker.service"]
        self.assertEqual((docker["active"], docker["unit_file_state"], docker["restart"]), ("active", "enabled",
                                                                                           "always"))
        self.assertEqual(docker["drop_ins"], ["/etc/systemd/system/docker.service.d/wait-for-data.conf"])
        self.assertEqual(units["failed-thing.service"]["active"], "failed")
        self.assertEqual(packages["nvidia-driver-550"], {"version": "550.120-0ubuntu1", "status": "ii", "source": "dpkg",
                                                         "held": True})
        self.assertEqual(packages["snap:core22"]["revision"], "1621")


class OutputTest(CollectTestCase):
    def test_write_snapshot_and_overview(self) -> None:
        snapshot, _ = self.capture()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "capture"
            json_path, md_path = write_snapshot(snapshot, out)
            self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o700)
            for path in (json_path, md_path):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(json.loads(json_path.read_text()), snapshot)
            text = md_path.read_text()
            self.assertEqual(sorted(p.name for p in out.iterdir()), ["overview.md", "snapshot.json"], "no temp files left")
        self.assertTrue(text.startswith("# Server snapshot: seraphina-test\n"))
        self.assertIn("| Containers | 4 (1 exited, 3 running) |", text)
        self.assertIn("| OS | Ubuntu 24.04.1 LTS, kernel 6.8.0-45-generic, x86_64 |", text)
        self.assertIn("Read snapshot.json before you share it", text)
        self.assertIn("## Not captured", text)

    def test_factory_snapshot_command(self) -> None:
        runner = FakeRunner(self)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(bp, "SubprocessRunner", lambda: runner), \
                contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            code = cli.main(["snapshot", "--out", str(Path(tmp) / "snap")])
            self.assertEqual(code, 0, err.getvalue())
            self.assertEqual(out.getvalue().splitlines(), [str(Path(tmp) / "snap" / "snapshot.json"),
                                                           str(Path(tmp) / "snap" / "overview.md")])
            self.assertIn("Read snapshot.json before you share it", err.getvalue())
            self.assertFalse(json.loads((Path(tmp) / "snap" / "snapshot.json").read_text())["scope"]["sudo"])
        self.assertTrue(runner.calls)


class SubprocessRunnerTest(unittest.TestCase):
    def test_it_refuses_what_check_argv_refuses(self) -> None:
        with self.assertRaises(UnsafeCommand):
            SubprocessRunner().run(["lxc", "list"], 5)

    def test_minimal_environment_timeout_and_missing(self) -> None:
        runner = SubprocessRunner()
        with mock.patch.dict(bp.RULES, {"env": lambda args: not args, "sleep": lambda args: args == ["5"]}), \
                mock.patch.dict(os.environ, {"SOME_API_KEY": "sk-env-never-0017"}):
            result = runner.run(["env"], 10)
            self.assertEqual(result.exit_code, 0)
            self.assertNotIn("sk-env-never-0017", result.stdout)
            self.assertIn("PATH=" + bp.SEARCH_PATH, result.stdout)
            slow = runner.run(["sleep", "5"], 0.3)
            self.assertTrue(slow.timed_out)
            self.assertIsNone(slow.exit_code)
            self.assertLess(slow.seconds, 4)
        self.assertFalse(runner.which("definitely-not-a-command-xyz"))


class RedactorTest(unittest.TestCase):
    def test_text_rules(self) -> None:
        r = Redactor(known=["sk-known-value-0018"])
        cases = {
            "DB_PASSWORD=abc OTHER=1": "DB_PASSWORD=[REDACTED] OTHER=1",
            "password: my secret phrase  # note": "password: [REDACTED]  # note",
            '{"api_key": "x", "user": "bob"}': '{"api_key": "[REDACTED]", "user": "bob"}',
            "--api-key=xyz --token abc --verbose": "--api-key=[REDACTED] --token [REDACTED] --verbose",
            "Authorization: Bearer abcdefghijklmnop": "Authorization: [REDACTED]",
            "postgres://user:pa55@db:5432/app": "postgres://[REDACTED]@db:5432/app",
            "http://authelia:9091/api": "http://authelia:9091/api",
            "After=network.target ssh-keygen.service": "After=network.target ssh-keygen.service",
            "x " + JWT + " y": "x [REDACTED] y",
            "export FOO=sk-known-value-0018": "export FOO=[REDACTED]",
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(r.text(raw), want)
        self.assertEqual(r.text(PRIVATE_KEY), f"-----BEGIN OPENSSH PRIVATE KEY----- {MARK}")
        self.assertEqual(r.text(r.text("token=abc")), "token=[REDACTED]", "redacting twice changes nothing more")

    def test_compose_environment_blocks(self) -> None:
        r = Redactor()
        text = ("services:\n  a:\n    environment:\n    - SAME=indent\n    - PASSTHROUGH\n    labels:\n"
                "    - x=y\n  b:\n    environment: {A: b}\n  c:\n    environment:\n      CERT: |\n        line one\n"
                "      EMPTY:\n")
        self.assertEqual(r.compose(text), "services:\n  a:\n    environment:\n    - SAME=[REDACTED]\n    - PASSTHROUGH\n"
                                          "    labels:\n    - x=y\n  b:\n    environment: [REDACTED]\n  c:\n"
                                          "    environment:\n      CERT: [REDACTED]\n        [REDACTED]\n      EMPTY:\n")

    def test_yaml_secret_blocks(self) -> None:
        r = Redactor()
        text = ("network:\n  tunnels:\n    wg0:\n      key: abc123secretvalue\n      peers:\n        - keys:\n"
                "            public: pub\n            shared: psk-0021\n          endpoint: 1.2.3.4:5\n"
                "  wifis:\n    wlan0:\n      access-points:\n        \"Home\":\n          auth:\n"
                "            key-management: psk\n            password: wifi-0022\n          mode: infrastructure\n"
                "cert: &c\n  private-key: |\n    -----x-----\n    user: inside-0023\n\n    tail-0024\n  name: x\n"
                "after: kept\n")
        self.assertEqual(r.yaml(text), (
            "network:\n  tunnels:\n    wg0:\n      key: [REDACTED]\n      peers:\n        - keys:\n"
            "            public: [REDACTED]\n            shared: [REDACTED]\n          endpoint: 1.2.3.4:5\n"
            "  wifis:\n    wlan0:\n      access-points:\n        \"Home\":\n          auth:\n"
            "            key-management: [REDACTED]\n            password: [REDACTED]\n          mode: infrastructure\n"
            "cert: &c\n  private-key: [REDACTED]\n    [REDACTED]\n    [REDACTED]\n\n    [REDACTED]\n  name: x\n"
            "after: kept\n"))

    def test_yaml_blocks_follow_whole_words_and_argv_lists(self) -> None:
        r = Redactor()
        compose = ("services:\n  authelia:\n    image: authelia/authelia\n    ports:\n      - \"9091:9091\"\n"
                   "  keycloak:\n    image: quay.io/keycloak/keycloak\n    command:\n      - start\n"
                   "      - \"--db-password\"\n      - \"pg-0025\"\n      - --verbose\n"
                   "    healthcheck:\n      test: [CMD, --token, tk-0026, --port, \"8080\"]\n")
        self.assertEqual(r.compose(compose), (
            "services:\n  authelia:\n    image: authelia/authelia\n    ports:\n      - \"9091:9091\"\n"
            "  keycloak:\n    image: quay.io/keycloak/keycloak\n    command:\n      - start\n"
            "      - \"--db-password\"\n      - \"[REDACTED]\"\n      - --verbose\n"
            "    healthcheck:\n      test: [CMD, --token, [REDACTED], --port, \"8080\"]\n"))
        self.assertEqual(r.compose(r.compose(compose)), r.compose(compose), "redacting twice changes nothing more")
        self.assertEqual(r.text('["serve", "--api-key", "sk-0027", "--port", "1"]'),
                         '["serve", "--api-key", "[REDACTED]", "--port", "1"]')

    def test_parsers(self) -> None:
        self.assertEqual([address_scope(a) for a in ("*", "::", "127.0.0.1", "::1", "100.64.1.2", "fd7a:115c:a1e0::5",
                                                     "192.168.1.2", "fe80::1", "8.8.8.8", "::ffff:127.0.0.1", "x")],
                         ["all", "all", "loopback", "loopback", "tailnet", "tailnet", "private", "link-local", "public",
                          "loopback", "unknown"])
        rows = parse_ss(SS)
        self.assertEqual([(r["proto"], r["address"], r["port"]) for r in rows],
                         [("tcp", "127.0.0.1", 3000), ("tcp", "0.0.0.0", 8000), ("tcp", "::", 22),
                          ("tcp", "100.101.102.103", 443), ("udp", "127.0.0.53", 53)])
        self.assertEqual(compose_services(COMPOSE_AI), ["open-webui", "ts-webui"])


if __name__ == "__main__":
    unittest.main()
