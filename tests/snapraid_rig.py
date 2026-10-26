"""A temporary machine for the SnapRAID helper's tests: bin/sf-snapraid loaded by path, its fixed locations moved
into a temporary tree (Paths.under), the SnapRAID binary a wrapper around tests/fake_snapraid.py, the mounts and
filesystem UUIDs faked (FakeSystem), and root ownership faked (FakeLstat), so the tests run as any user."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HELPER_FILE = ROOT / "bin" / "sf-snapraid"
FAKE = ROOT / "tests" / "fake_snapraid.py"
FIXTURES = ROOT / "tests" / "fixtures" / "snapraid13"


def load_helper():
    """bin/sf-snapraid as a module (it has no .py name and is never imported by the harness)."""
    loader = importlib.machinery.SourceFileLoader("sf_snapraid", str(HELPER_FILE))
    spec = importlib.util.spec_from_loader("sf_snapraid", loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module   # dataclasses look their module up while the file runs
    loader.exec_module(module)
    return module


sf = load_helper()
UUIDS = {"d1": "1111aaaa-0000-4000-8000-000000000001", "d2": "1111aaaa-0000-4000-8000-000000000002",
         "d3": "1111aaaa-0000-4000-8000-000000000003", "parity": "2222bbbb-0000-4000-8000-000000000001",
         "2-parity": "2222bbbb-0000-4000-8000-000000000002"}
MOUNTS = {"d1": "mnt/disk1", "d2": "mnt/disk2", "d3": "mnt/disk3", "parity": "mnt/parity1", "2-parity": "mnt/parity2"}


class FakeLstat:
    """os.lstat with every path owned by root and writable by nobody else, except the ones a test spoils."""

    def __init__(self) -> None:
        self.spoiled: dict[str, tuple[int, int]] = {}   # path: (uid, mode bits to add)

    def __call__(self, path: str):
        real = os.lstat(path)
        uid, extra = self.spoiled.get(path, (0, 0))
        return types.SimpleNamespace(st_uid=uid, st_mode=(real.st_mode & ~0o022) | extra)


class Space:
    def __init__(self, free: int = 50 << 30, total: int = 100 << 30, used: int | None = None) -> None:
        self.f_frsize = 4096
        self.f_blocks = total // 4096
        self.f_bfree = (total - used) // 4096 if used is not None else free // 4096
        self.f_bavail = free // 4096


class FakeSystem(sf.System):
    """Mounts, UUIDs, free space and programs as a test sets them; commands really run (the fake SnapRAID)."""

    def __init__(self, paths, root: Path) -> None:
        super().__init__(paths)
        self.root = root
        self.unmounted: set[str] = set()
        self.uuids = dict(UUIDS)
        self.running: list[str] = []
        self.space: dict[str, Space] = {}
        self.daemon_port_open = False
        self.dpkg: dict[str, tuple[str, str]] = {}

    def mounts(self):
        out = [sf.Mount("/", "ext4", "/dev/vda", "254:0")]
        for index, (name, rel) in enumerate(MOUNTS.items()):
            if name not in self.unmounted:
                out.append(sf.Mount(str(self.root / rel), "ext4", f"/dev/loop{index}", f"7:{index}"))
        return out

    def uuid(self, mount):
        if mount.device == "254:0":
            return "9999cccc-0000-4000-8000-000000000000"
        name = list(MOUNTS)[int(mount.device.split(":")[1])]
        return self.uuids.get(name)

    def statvfs(self, path):
        for rel, space in self.space.items():
            if str(path).startswith(str(self.root / rel)):
                return space
        return Space()

    def programs(self):
        return list(self.running)

    def port_open(self, port):
        return self.daemon_port_open

    def run(self, argv, timeout, out_path=None):
        """The package database is faked (dpkg): {path: (package, version)}; every other command really runs."""
        if argv and argv[0] == "dpkg":
            owner = self.dpkg.get(argv[-1])
            return (0, f"{owner[0]}: {argv[-1]}\n", False) if owner else (1, f"dpkg-query: no path found matching "
                                                                           f"pattern {argv[-1]}\n", False)
        if argv and argv[0] == "dpkg-query":
            versions = {package: version for package, version in self.dpkg.values()}
            return (0, versions[argv[-1]], False) if argv[-1] in versions else (1, "", False)
        return super().run(argv, timeout, out_path)



class Rig:
    """One temporary machine: the conf, the helper's config, the disks' folders and content copies, the fake
    SnapRAID and its scenario."""

    def __init__(self, commands: dict | None = None, *, extra_conf: str = "", helper_cfg: dict | None = None) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="srtest-")
        self.root = Path(self.tmp.name)
        self.paths = sf.Paths.under(self.root)
        self.lstat = FakeLstat()
        self.system = FakeSystem(self.paths, self.root)
        for rel in MOUNTS.values():
            (self.root / rel).mkdir(parents=True)
        for rel in ("mnt/disk1/.snapraid", "mnt/disk2/.snapraid", "var/snapraid", "etc/seraphina",
                    "usr/local/libexec/seraphina", "usr/bin"):
            (self.root / rel).mkdir(parents=True, exist_ok=True)
        for rel in ("var/snapraid/snapraid.content", "mnt/disk1/.snapraid/snapraid.content",
                    "mnt/disk2/.snapraid/snapraid.content"):
            (self.root / rel).write_bytes(b"content " * 100)
        (self.root / "mnt/parity1/snapraid.parity").write_bytes(b"")
        (self.root / "mnt/parity2/snapraid.2-parity").write_bytes(b"")
        shutil.copy(HELPER_FILE, self.root / "usr/local/libexec/seraphina/sf-snapraid")
        self.conf = self.root / "etc/snapraid.conf"
        self.write_conf(extra_conf)
        self.helper_cfg = helper_cfg or {"v": 1, "conf": str(self.conf), "allow": ["read"], "mounts": dict(UUIDS)}
        self.write_helper_cfg(self.helper_cfg)
        self.calls = self.root / "calls.jsonl"
        self.scenario = self.root / "scenario.json"
        self.set_commands(commands or {})
        wrapper = self.root / "usr/bin/snapraid"
        wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE} --scenario {self.scenario} \"$@\"\n",
                           encoding="utf-8")
        wrapper.chmod(0o755)

    def write_conf(self, extra: str = "", text: str | None = None) -> None:
        r = self.root
        body = text if text is not None else (
            f"parity {r}/mnt/parity1/snapraid.parity\n2-parity {r}/mnt/parity2/snapraid.2-parity\n"
            f"content {r}/var/snapraid/snapraid.content\ncontent {r}/mnt/disk1/.snapraid/snapraid.content\n"
            f"content {r}/mnt/disk2/.snapraid/snapraid.content\n"
            f"data d1 {r}/mnt/disk1/\ndata d2 {r}/mnt/disk2/\ndata d3 {r}/mnt/disk3/\n"
            "exclude /lost+found/\nexclude *.unrecoverable\nblocksize 64\nautosave 1\n")
        self.conf.write_text(body + extra, encoding="utf-8")

    def write_helper_cfg(self, cfg: dict) -> None:
        (self.root / "etc/seraphina/snapraid-helper.json").write_text(json.dumps(cfg), encoding="utf-8")

    def set_commands(self, commands: dict) -> None:
        r = self.root
        paths = {"/mnt/disk1": f"{r}/mnt/disk1", "/mnt/disk2": f"{r}/mnt/disk2", "/mnt/disk3": f"{r}/mnt/disk3",
                 "/mnt/parity1": f"{r}/mnt/parity1", "/mnt/parity2": f"{r}/mnt/parity2",
                 "/var/snapraid": f"{r}/var/snapraid", "/etc/snapraid.conf": str(self.conf)}
        self.scenario.write_text(json.dumps({"calls": str(self.calls), "paths": paths, "commands": commands}),
                                 encoding="utf-8")
        count = Path(str(self.scenario) + ".count")
        count.unlink(missing_ok=True)

    def helper(self):
        return sf.Helper(self.paths, self.system, self.lstat)

    def ask(self, request: dict | str | bytes) -> tuple[int, dict]:
        """One request through main(), as sudo would pass it: (exit status, answer)."""
        line = request if isinstance(request, bytes) else \
            (request if isinstance(request, str) else json.dumps(request)).encode("utf-8") + b"\n"
        out = io.StringIO()
        code = sf.main([], stdin=io.BytesIO(line), stdout=out, paths=self.paths, system=self.system,
                       lstat=self.lstat)
        return code, json.loads(out.getvalue())

    def calls_made(self) -> list[list[str]]:
        if not self.calls.exists():
            return []
        return [json.loads(line)["argv"] for line in self.calls.read_text(encoding="utf-8").splitlines() if line]

    def commands_run(self) -> list[str]:
        return [argv[-1] for argv in self.calls_made() if argv != ["--version"]]

    def close(self) -> None:
        self.tmp.cleanup()


HEALTHY = {"status": {"fixture": "status-ok", "exit": 0}, "diff": {"fixture": "diff0", "exit": 0},
           "smart": {"fixture": "smart-unsupported", "exit": 0}, "probe": {"fixture": "probe-unsupported", "exit": 0},
           "list": {"fixture": "list", "exit": 0}}
