"""The lab checks of docs/seraphina/BACKUPS_SNAPRAID.md section 13.2 that P1 can run (DESIGN.md section 41): the
helper bin/sf-snapraid against the real SnapRAID 13.0 on real ext4 filesystems in loop files, as root, with the
real mounts, filesystem UUIDs and file ownership (no fake system, no fake lstat).

They run only when asked: SF_SNAPRAID_LAB=1 and SF_SNAPRAID_BIN naming a SnapRAID 13.0 binary by its absolute path
(PATH is never changed), as root, with losetup, mkfs.ext4, mount, umount and blkid. Each test builds its own
machine in a temporary folder under /tmp (four 96 MiB loop disks: three data disks and one protection disk), runs
a first sync with the real binary directly (the helper runs none in P1), and takes it all down afterwards.

    sudo SF_SNAPRAID_LAB=1 SF_SNAPRAID_BIN=/path/to/snapraid-13.0/bin/snapraid \\
        python3 -m unittest tests.test_snapraid_lab -v

L1 here is the report half of L1 (P1 runs no sync): six of nine files removed on one disk make the guard's
report say a run would hold, and the protection file and the file lists are byte for byte unchanged. L3: a data
disk unmounted with its mount point left in place is held by G3 before any check for changes runs. Three more
pin the review round's fixes on the real 13.0: a damaged first copy of the file list is a content problem (red,
held at G4), not "never protected" or a guard error; a config line SnapRAID reads whole (a vertical tab inside a
smartctl line) is refused whole; an unsafe SnapRAID binary is unsafe_install. L8, L9, L10 and L13 need a server
started by systemd (units, sudo through the harness's user, a power-off) and are reported as not run here.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import ClassVar

from swfactory import snapraid
from tests.snapraid_rig import HELPER_FILE, sf

LAB = os.environ.get("SF_SNAPRAID_LAB") == "1"
BINARY = os.environ.get("SF_SNAPRAID_BIN", "")
TOOLS = ("losetup", "mkfs.ext4", "mount", "umount", "blkid")
DISK_MIB = 96
SYSTEMD = "not run: needs a systemd-booted host"


def _why_not() -> str | None:
    if not LAB:
        return "the SnapRAID lab runs only with SF_SNAPRAID_LAB=1"
    if not BINARY or not os.path.isabs(BINARY) or not os.access(BINARY, os.X_OK):
        return "SF_SNAPRAID_BIN must name the SnapRAID 13.0 binary by its absolute path"
    try:
        version = subprocess.run([BINARY, "--version"], capture_output=True, text=True, timeout=15, check=False).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return f"{BINARY} does not answer --version ({type(exc).__name__})"
    if " v13.0 " not in f" {version} ":
        return f"{BINARY} is not SnapRAID 13.0: {version.strip()[:80]}"
    if os.geteuid() != 0:
        return "the lab mounts loop disks: run it as root"
    missing = [tool for tool in TOOLS if shutil.which(tool) is None]
    if missing:
        return "the lab needs " + ", ".join(missing)
    return None


WHY_NOT = _why_not()


def run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(argv, check=True, capture_output=True, text=True, timeout=300, **kwargs)


def forget_cached_uuids(loops: list[str]) -> None:
    """Drop the lab's loop devices from libblkid's cache. SnapRAID reads filesystem UUIDs through it when
    /dev/disk/by-uuid is missing (a container), and the cache trusts an entry for a few minutes: a loop device
    reused by the next test would otherwise give the old filesystem's UUID at the first sync and the new one
    later, and SnapRAID would rightly refuse with "UUID change"."""
    cache = Path(os.environ.get("BLKID_FILE") or "/run/blkid/blkid.tab")
    try:
        lines = cache.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError:
        return
    kept = [line for line in lines if not any(line.rstrip().endswith(f">{loop}</device>") for loop in loops)]
    if len(kept) != len(lines):
        cache.write_text("".join(kept), encoding="utf-8")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Lab:
    """One machine: the helper's fixed locations moved under a temporary folder (Paths.under, with the real /proc,
    /dev and scheduler inventory), four loop disks mounted there, the 13.0 binary copied to where the helper looks,
    a conf, the helper's config with the real filesystem UUIDs, a few files and a first sync."""

    NAMES: ClassVar[dict[str, str]] = {"d1": "mnt/disk1", "d2": "mnt/disk2", "d3": "mnt/disk3",
                                       "parity": "mnt/parity1"}

    def __init__(self, case: unittest.TestCase) -> None:
        self.case = case
        self.root = Path(tempfile.mkdtemp(prefix="srlab-", dir="/tmp"))
        case.addCleanup(shutil.rmtree, self.root, True)
        self.paths = dataclasses.replace(sf.Paths.under(self.root), root="/")
        self.system = sf.System(self.paths)
        self.loops: dict[str, str] = {}
        uuids = {}
        for name, rel in self.NAMES.items():
            image = self.root / "img" / f"{name}.img"
            image.parent.mkdir(parents=True, exist_ok=True)
            with open(image, "wb") as handle:
                handle.truncate(DISK_MIB << 20)
            run(["mkfs.ext4", "-q", "-F", "-L", f"srlab-{name}", str(image)])
            loop = run(["losetup", "-f", "--show", str(image)]).stdout.strip()
            case.addCleanup(forget_cached_uuids, [loop])
            case.addCleanup(subprocess.run, ["losetup", "-d", loop], capture_output=True)
            forget_cached_uuids([loop])
            self.loops[name] = loop
            point = self.root / rel
            point.mkdir(parents=True)
            self.mount(name)
            uuids[name] = run(["blkid", "-p", "-s", "UUID", "-o", "value", loop]).stdout.strip()
        for rel in ("var/snapraid", "etc/seraphina", "usr/local/libexec/seraphina", "usr/local/bin",
                    "mnt/disk1/.snapraid", "mnt/disk2/.snapraid"):
            (self.root / rel).mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o755)
        shutil.copy(HELPER_FILE, self.root / "usr/local/libexec/seraphina/sf-snapraid")
        os.chmod(self.root / "usr/local/libexec/seraphina/sf-snapraid", 0o755)
        shutil.copy(BINARY, self.root / "usr/local/bin/snapraid")
        os.chmod(self.root / "usr/local/bin/snapraid", 0o755)
        r = self.root
        self.conf = r / "etc/snapraid.conf"
        self.conf.write_text(
            f"parity {r}/mnt/parity1/snapraid.parity\n"
            f"content {r}/var/snapraid/snapraid.content\ncontent {r}/mnt/disk1/.snapraid/snapraid.content\n"
            f"content {r}/mnt/disk2/.snapraid/snapraid.content\n"
            f"data d1 {r}/mnt/disk1/\ndata d2 {r}/mnt/disk2/\ndata d3 {r}/mnt/disk3/\n"
            "exclude /lost+found/\nexclude /.snapraid/\nblocksize 64\nautosave 1\n", encoding="utf-8")
        os.chmod(self.conf, 0o644)
        (r / "etc/seraphina/snapraid-helper.json").write_text(json.dumps({
            "v": 1, "conf": str(self.conf), "allow": ["read"], "mounts": uuids,
            "guard": {"content_free_min_mib": 1}}), encoding="utf-8")
        os.chmod(r / "etc/seraphina/snapraid-helper.json", 0o644)
        for disk, count in (("disk1", 9), ("disk2", 12), ("disk3", 4)):
            folder = r / "mnt" / disk / "files"
            folder.mkdir()
            for i in range(count):
                (folder / f"file {i:02d}.bin").write_bytes(os.urandom(48 * 1024 + i * 1000))
        run([str(r / "usr/local/bin/snapraid"), "-c", str(self.conf), "sync"])

    def mount(self, name: str) -> None:
        point = self.root / self.NAMES[name]
        run(["mount", self.loops[name], str(point)])
        self.case.addCleanup(subprocess.run, ["umount", "-l", str(point)], capture_output=True)

    def ask(self, request: dict) -> tuple[int, dict]:
        out = io.StringIO()
        line = json.dumps(request).encode("utf-8") + b"\n"
        code = sf.main([], stdin=io.BytesIO(line), stdout=out, paths=self.paths, system=self.system)
        return code, json.loads(out.getvalue())

    def commands(self) -> list[str]:
        """The SnapRAID commands the helper has run, oldest first, from its job records."""
        folder = Path(self.paths.state, "jobs")
        if not folder.is_dir():
            return []
        records = [json.loads(path.read_text(encoding="utf-8")) for path in folder.glob("J-*.json")]
        return [record["command"] for record in sorted(records, key=lambda rec: int(rec["job"][2:]))]

    def fingerprints(self, *, without: str = "") -> dict[str, str]:
        """sha256 of the protection file and every copy of the file list (but those on the disk named)."""
        r = self.root
        files = [r / "mnt/parity1/snapraid.parity", r / "var/snapraid/snapraid.content",
                 r / "mnt/disk1/.snapraid/snapraid.content", r / "mnt/disk2/.snapraid/snapraid.content"]
        return {str(path.relative_to(r)): sha256(path) for path in files
                if not without or not str(path.relative_to(r)).startswith(without + "/")}


@unittest.skipIf(WHY_NOT is not None, WHY_NOT or "")
class SnapraidLabTests(unittest.TestCase):
    def setUp(self):
        self.lab = Lab(self)

    def test_the_reader_answers_on_a_real_machine(self):
        lab = self.lab
        code, info = lab.ask({"v": 1, "verb": "info"})
        self.assertEqual(code, 0, info)
        self.assertTrue(info["conf_ok"], info.get("conf_problems"))
        code, status = lab.ask({"v": 1, "verb": "status"})
        self.assertEqual((code, status["outcome"]), (0, "ok"), status)
        self.assertEqual({name: entry["mounted"] for name, entry in status["mounts"].items()},
                         {"d1": True, "d2": True, "d3": True, "parity": True})
        self.assertTrue(all(entry["uuid_ok"] for entry in status["mounts"].values()), status["mounts"])
        self.assertEqual(status["facts"]["file_count"], 25)
        code, listed = lab.ask({"v": 1, "verb": "list"})
        self.assertEqual((code, listed["outcome"]), (0, "ok"), listed)
        self.assertEqual(lab.commands(), ["status", "status", "list"],
                         "list reads the status first: was the last run good?")

    def test_l1_six_of_nine_removed_on_a_disk_would_be_held_and_nothing_is_written(self):
        lab = self.lab
        self.assertEqual(lab.ask({"v": 1, "verb": "list"})[0], 0)   # the index of the last good run
        code, nothing = lab.ask({"v": 1, "verb": "guard"})
        self.assertEqual((code, nothing["verdict"]), (0, "nothing_to_do"), nothing)
        before = lab.fingerprints()
        for i in range(6):
            (lab.root / "mnt/disk1/files" / f"file {i:02d}.bin").unlink()
        code, guard = lab.ask({"v": 1, "verb": "guard"})
        self.assertEqual(code, 0, guard)
        self.assertEqual(guard["verdict"], "would_hold", guard)
        self.assertTrue(guard["report_only"])
        g10 = next(check for check in guard["checks"] if check["id"] == "G10")
        self.assertEqual((g10["result"], g10["reason"]), ("hold", "lost_over"), g10)
        self.assertEqual(guard["counts"]["disks"]["d1"]["removed"], 6)
        code, page = lab.ask({"v": 1, "verb": "changes", "guard": guard["guard"]})
        self.assertEqual(code, 0, page)
        self.assertEqual(sorted(entry["path"] for entry in page["entries"]),
                         [f"files/file {i:02d}.bin" for i in range(6)])
        self.assertEqual(lab.fingerprints(), before, "the guard wrote nothing to the protection or its lists")
        self.assertNotIn("sync", lab.commands())

    def test_l3_an_unmounted_data_disk_is_held_by_g3_before_any_diff(self):
        lab = self.lab
        point = lab.root / lab.NAMES["d2"]
        before = lab.fingerprints(without=lab.NAMES["d2"])
        run(["umount", str(point)])
        self.assertTrue(point.is_dir(), "the mount point stays")
        code, guard = lab.ask({"v": 1, "verb": "guard"})
        self.assertEqual(code, 0, guard)
        self.assertEqual(guard["verdict"], "would_hold", guard)
        g3 = next(check for check in guard["checks"] if check["id"] == "G3")
        self.assertEqual((g3["result"], g3["reason"]), ("hold", "disk_missing"), g3)
        self.assertIn("disk 2", g3["words"])
        self.assertNotIn("diff", lab.commands(), "no check for changes ran on a missing disk")
        self.assertEqual(lab.fingerprints(without=lab.NAMES["d2"]), before)
        code, status = lab.ask({"v": 1, "verb": "status"})
        self.assertEqual(code, 0, status)
        self.assertFalse(status["mounts"]["d2"]["mounted"])
        lab.mount("d2")
        code, guard = lab.ask({"v": 1, "verb": "guard"})
        self.assertEqual(guard["verdict"], "nothing_to_do", guard)


    def test_a_damaged_first_copy_of_the_file_list_is_red_and_held(self):
        """The first copy's bytes changed, its size kept: the real 13.0 status stops ("Error decoding", "CRC
        mismatch") with no fallback to another copy. The reader says the file list cannot be read, the guard holds
        at G4 (content_problem) instead of failing, the panel's light is red with the last good facts kept, and
        nothing is written to the protection or its lists."""
        lab = self.lab
        code, good = lab.ask({"v": 1, "verb": "status"})
        self.assertEqual((code, good["outcome"]), (0, "ok"), good)
        first = lab.root / "var/snapraid/snapraid.content"
        data = bytearray(first.read_bytes())
        for index in range(80, 120):
            data[index] ^= 0x5A
        first.write_bytes(bytes(data))
        before = lab.fingerprints()
        code, status = lab.ask({"v": 1, "verb": "status"})
        self.assertEqual((code, status["outcome"]), (0, "failed"), status)
        self.assertTrue(status["content"]["read_failed"] and status["content"]["other"], status["content"])
        merged = snapraid.merge_status(snapraid.merge_status({}, good), status)
        self.assertEqual(merged["facts"]["file_count"], 25, "the last good facts are kept")
        lit = snapraid.light({"status": merged}, {"stale_minutes": 180}, time.time())
        self.assertEqual((lit["tone"], lit["code"]), ("err", "content_problem"), lit)
        code, guard = lab.ask({"v": 1, "verb": "guard"})
        self.assertEqual(code, 0, guard)
        self.assertEqual((guard["verdict"], guard["reasons"]), ("would_hold", ["content_problem"]), guard["checks"])
        self.assertEqual(lab.fingerprints(), before, "nothing was written")

    def test_a_config_line_snapraid_reads_whole_is_refused_whole(self):
        """A vertical tab inside a smartctl line: the real 13.0 reads ONE smartctl option up to the newline (it
        refuses this one only because a second %s follows), where the reader used to see a harmless option plus an
        exclude line and pass the config. The reader now refuses the config and starts no SnapRAID command."""
        lab = self.lab
        line = len(lab.conf.read_text(encoding="utf-8").split("\n"))
        with open(lab.conf, "a", encoding="utf-8") as handle:
            handle.write("smartctl d1 -d sat %s\x0bexclude %s\n")
        log = lab.root / "vt.log"
        done = subprocess.run([str(lab.root / "usr/local/bin/snapraid"), "-c", str(lab.conf), "-l", str(log), "smart"],
                              capture_output=True, text=True, timeout=120, check=False)
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        fatal = sf.parse_log(log.read_bytes()).text("fatal")
        self.assertIn(f"Invalid 'smartctl' option in '{lab.conf}' at line {line}", fatal,
                      "SnapRAID read the vertical tab as part of one line")
        before = lab.commands()
        for verb in ("smart", "status", "guard"):
            code, answer = lab.ask({"v": 1, "verb": verb})
            self.assertEqual((code, answer["error"]), (2, "unsafe_conf"), answer)
            self.assertIn(f"line {line} holds a control character", answer["words"])
        _code, info = lab.ask({"v": 1, "verb": "info"})
        self.assertFalse(info["conf_ok"])
        self.assertEqual(lab.commands(), before, "the reader started no SnapRAID command")

    def test_an_unsafe_snapraid_binary_is_refused_for_every_verb(self):
        """A SnapRAID binary others can change is unsafe_install, naming it, for every verb (it read as "not
        installed" before, and the doctor passed)."""
        lab = self.lab
        binary = lab.root / "usr/local/bin/snapraid"
        os.chmod(binary, 0o757)
        self.addCleanup(os.chmod, binary, 0o755)
        for verb in ("info", "status", "smart"):
            code, answer = lab.ask({"v": 1, "verb": verb})
            self.assertEqual((code, answer["error"]), (2, "unsafe_install"), answer)
            self.assertIn(str(binary), answer["words"])
        os.chmod(binary, 0o755)
        self.assertEqual(lab.ask({"v": 1, "verb": "status"})[1]["outcome"], "ok")


class SystemdLabTests(unittest.TestCase):
    """Section 10.2's checks that need a server started by systemd: listed by the doctor and the card as not
    checked yet (snapraid.LAB_CHECKS)."""

    @unittest.skip(SYSTEMD)
    def test_l8_the_reader_is_reached_only_through_the_harness_user_and_never_from_the_sandbox(self):
        """sudo -n as the harness's user runs the reader and nothing else; from inside the agents' bwrap sandbox
        (NoNewPrivs) sudo is refused; another user is refused."""

    @unittest.skip(SYSTEMD)
    def test_l9_long_runs_are_fenced_in_their_own_units(self):
        """Phase P2: a sync or scrub runs in a transient systemd unit with the data disks read only."""

    @unittest.skip(SYSTEMD)
    def test_l10_a_power_off_waits_for_a_safe_stop(self):
        """Phase P2: the nightly power-off stops a run with SIGINT and waits instead of cutting it."""

    @unittest.skip(SYSTEMD)
    def test_l13_the_server_starts_when_a_disk_has_failed(self):
        """The data disks are mounted nofail: a dead disk never keeps the server from booting."""


if __name__ == "__main__":
    unittest.main()
