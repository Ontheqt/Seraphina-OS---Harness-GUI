"""Tests for swfactory/yourserver.py, Your server's harness side, phase P1a (DESIGN.md §42; docs/seraphina/YOUR_SERVER.md
§12.2): T1 the cgroup readers, T2 the host strip, T3 GPUs, T5 and T12 the lights, T10 grouping, T11 addresses and
the record's hash chain, T15 links, T16 probes, T25 the Beszel watch, T26 what <data_dir>/yourserver holds, T27 never
in the request path, T28 phone texts, T29 routes, T38 probes behind the login gateway, U8 the novice words, the
settings and the doctor's lines. The harness reads a placeholder server (tests/sfsystem_rig.py) through the real
bin/sf-system run in this process; every name, address and path is a placeholder.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import re
import socket
import stat
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

from swfactory import settings as settings_mod, yourserver as ys
from swfactory.config import Config, sandbox_masks
from swfactory.helpers import HelperClient, HelperError
from tests.sfsystem_rig import IDS, Rig, sx

ROOT = Path(__file__).resolve().parent.parent
VIEW_JS = ROOT / "swfactory" / "web" / "views" / "yourserver.js"


def make(rig: Rig, raw: dict | None = None, **kw) -> ys.YourServer:
    tmp = tempfile.mkdtemp(prefix="ys-test-")
    base = {"data_dir": tmp, "yourserver": {"enabled": True, "tailnet": {"suffix": "example.ts.net"}}}
    for key, value in (raw or {}).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            ys._merge(base[key], value)
        else:
            base[key] = value
    config = Config(Path(tmp) / "config.json", base)
    roots = {name: str(rig.at(path)) for name, path in (
        ("proc", "/proc"), ("cgroup", "/sys/fs/cgroup"), ("hwmon", "/sys/class/hwmon"), ("run", "/run"),
        ("by_uuid", "/dev/disk/by-uuid"), ("fib", "/proc/net/fib_trie"), ("mounts", "/proc/self/mounts"))}
    kw.setdefault("client", HelperClient(sx.HELPER, runner=rig.runner, name="Seraphina's system access"))
    kw.setdefault("gpu_run", lambda argv: (None, ""))
    kw.setdefault("systemctl", lambda argv: "ActiveState=active\n")
    kw.setdefault("prober", ys.Prober(http=lambda h, p, path, host: {"status": 200}, tcp=lambda h, p: {"status": 0}))
    area = ys.YourServer(config, roots=roots, **kw)

    def patch(change: dict) -> None:
        for key, value in change.items():
            ys._merge(config.raw.setdefault(key, {}), value)
    area.update_config = patch
    area.numbers(force=True)
    return area


def confirm_all(area: ys.YourServer) -> None:
    rows = [{"app": r["app"], "url": r["url"], "expect": r["expect"] or "run"} if r["url"] else
            {"app": r["app"], "expect": r["expect"] or "run"} for r in area.review_view()["rows"]]
    area.review_confirm(rows)


def stop(rig: Rig, key: str, exit_code: int, manually: bool, finished: float, **extra) -> None:
    path = rig.at(f"/var/lib/docker/containers/{IDS[key]}/config.v2.json")
    data = json.loads(path.read_text())
    data["State"].update(Running=False, Pid=0, ExitCode=exit_code,
                         FinishedAt=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(finished)))
    data["HasBeenManuallyStopped"] = manually
    data.update(extra)
    path.write_text(json.dumps(data))
    for scope in rig.at("/sys/fs/cgroup").rglob(f"docker-{IDS[key]}.scope"):
        for child in scope.iterdir():
            child.unlink()
        scope.rmdir()


def keep_running(rig: Rig, key: str):
    """What stop() changes, kept: call the result to start the container again (its state file and cgroup scope)."""
    config = rig.at(f"/var/lib/docker/containers/{IDS[key]}/config.v2.json")
    text = config.read_text()
    scope = next(rig.at("/sys/fs/cgroup").rglob(f"docker-{IDS[key]}.scope"))
    files = {child.name: child.read_text() for child in scope.iterdir()}

    def start() -> None:
        config.write_text(text)
        scope.mkdir(parents=True, exist_ok=True)
        for name, data in files.items():
            (scope / name).write_text(data)
    return start


class AreaCase(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = Rig()
        self.addCleanup(self.rig.cleanup)

    def area(self, raw=None, **kw) -> ys.YourServer:
        area = make(self.rig, raw, **kw)
        area.refresh_map()
        return area

    def cards(self, area) -> dict:
        return {c["id"]: c for c in area.cards()["apps"]}


class CgroupTests(AreaCase):
    """T1: RAM is memory.current minus inactive_file; CPU from two reads; missing memory.current is unknown, never 0;
    a scope under a custom slice is found; a scope with no PIDs is not running."""

    def test_scan(self):
        scopes = ys.scan_cgroups(str(self.rig.at("/sys/fs/cgroup")))
        photos = scopes[IDS["photos"]]
        self.assertEqual((photos["ram"], photos["running"], photos["pids"]), (1_200_000_000, True, [4301]))
        self.assertIn(IDS["media"], scopes, "custom.slice")
        self.assertIn("custom.slice", scopes[IDS["media"]]["path"])
        empty = self.rig.at(f"/sys/fs/cgroup/system.slice/docker-{IDS['photos-db']}.scope")
        (empty / "cgroup.procs").write_text("")
        (empty / "memory.current").unlink()
        scopes = ys.scan_cgroups(str(self.rig.at("/sys/fs/cgroup")))
        self.assertEqual((scopes[IDS["photos-db"]]["running"], scopes[IDS["photos-db"]]["ram"]), (False, None))

    def test_cpu_from_two_reads(self):
        area = make(self.rig)
        stat_file = self.rig.at(f"/sys/fs/cgroup/system.slice/docker-{IDS['photos']}.scope/cpu.stat")
        with unittest.mock.patch.object(ys.os, "cpu_count", return_value=4):
            clock = [100.0]
            with unittest.mock.patch.object(ys.time, "monotonic", side_effect=lambda: clock[0]):
                area.numbers(force=True)
                stat_file.write_text("usage_usec 7000000\n")
                clock[0] = 102.0
                scopes = area.numbers(force=True)
        self.assertEqual(scopes[IDS["photos"]]["cpu_percent"], 25.0, "2 s of one core over 2 s on 4 CPUs")

    def test_the_summary_card_names_no_container(self):
        card = ys.summary_card(ys.scan_cgroups(str(self.rig.at("/sys/fs/cgroup"))), str(self.rig.at("/proc")))
        self.assertGreaterEqual(card["count"], 10)
        self.assertRegex(card["words"], r"^\d+ containers are running \(.+ and \d+ more\), using .+ of memory\. ")
        self.assertNotRegex(card["words"], r"[0-9a-f]{12}")


class HostTests(AreaCase):
    """T2: /proc/meminfo, /proc/uptime and /proc/stat; mounts de-duplicated by device; pseudo-filesystems skipped; a
    filesystem Seraphina's user cannot stat comes from the helper's answer. T3's host part: GPUs by UUID."""

    def test_proc_files(self):
        self.rig.write("/proc/meminfo", "MemTotal:  64000000 kB\nMemAvailable:  32000000 kB\nSwapTotal: 0 kB\n")
        self.rig.write("/proc/uptime", "7200.0 100.0\n")
        self.rig.write("/proc/stat", "cpu  100 0 100 800 0 0 0 0\ncpu0 1 1 1 1\n")
        self.assertEqual(ys.read_meminfo(str(self.rig.at("/proc/meminfo"))),
                         {"MemTotal": 65_536_000_000, "MemAvailable": 32_768_000_000, "SwapTotal": 0})
        self.assertEqual(ys.read_uptime(str(self.rig.at("/proc/uptime"))), 7200.0)
        self.assertEqual(ys.read_cpu(str(self.rig.at("/proc/stat"))), (200, 1000))
        area = make(self.rig)
        area.host(force=True)
        self.rig.write("/proc/stat", "cpu  300 0 100 900 0 0 0 0\n")
        view = area.host(force=True)
        self.assertEqual(view["cpu_percent"], 67, "200 busy of 300")
        words = [i["words"] for i in view["items"]]
        self.assertIn("33 GB of 66 GB free", words)
        self.assertIn("Up 2 hours", words)
        self.assertFalse(any("swap" in w for w in words), "swap only while it is used")

    def test_mounts(self):
        self.rig.write("/proc/self/mounts", "/dev/sda1 / ext4 rw 0 0\n/dev/sda1 /srv/again ext4 rw 0 0\n"
                                            "proc /proc proc rw 0 0\ntmpfs /run tmpfs rw 0 0\n"
                                            "overlay /var/lib/docker/x overlay rw 0 0\n/dev/sdb1 /srv/a\\040b xfs rw 0 0\n")
        found = ys.read_mounts(str(self.rig.at("/proc/self/mounts")), stat_of=lambda p: (_ for _ in ()).throw(OSError()))
        self.assertEqual([(m["source"], m["mount"]) for m in found], [("/dev/sda1", "/"), ("/dev/sdb1", "/srv/a b")])

    def test_a_disk_under_a_private_folder_comes_from_the_helper(self):
        area = self.area()
        self.rig.write("/proc/self/mounts", "/dev/sda /srv/private/data1 ext4 rw 0 0\n")
        disks = area.disks(area.settings()["host"])
        self.assertEqual(disks, [], "no helper answer for that mount")
        area.facts["disks"] = {"filesystems": [{"mount": "/srv/private/data1", "free": 50 * 10 ** 9,
                                                "total": 16 * 10 ** 12}]}
        disks = area.disks(area.settings()["host"])
        self.assertEqual((disks[0]["via_helper"], disks[0]["tone"]), (True, "warn"), "50 GB of 16 TB is below 100 GB")

    # A unit with ProtectHome=yes, InaccessiblePaths=-/backup and TemporaryFileSystem=/srv: each puts an empty
    # folder from /run's tmpfs over a disk mounted before, which /proc/self/mounts still lists as a real filesystem.
    COVERED = ("21 1 8:1 / / rw,relatime - ext4 /dev/sda1 rw\n"
               "22 21 8:17 / /backup rw,relatime - ext4 /dev/sdb1 rw\n"
               "23 21 8:33 / /home rw,relatime - ext4 /dev/sdc1 rw\n"
               "24 21 0:26 / /run rw,nosuid,nodev - tmpfs tmpfs rw,mode=755\n"
               "26 21 8:49 / /srv/data rw,relatime shared:9 - xfs /dev/sdd1 rw\n"
               "27 21 8:65 / /srv/a\\040b rw,relatime - ext4 /dev/sde1 rw\n"
               "28 22 0:26 /systemd/inaccessible/dir /backup ro,nosuid - tmpfs tmpfs ro,mode=000\n"
               "29 23 0:26 /systemd/inaccessible/dir /home ro,nosuid - tmpfs tmpfs ro,mode=000\n"
               "30 21 0:31 / /srv rw,nosuid,nodev - tmpfs tmpfs rw,mode=755\n"
               "31 30 8:65 / /srv/a\\040b rw,relatime - ext4 /dev/sde1 rw\n")

    def covered(self) -> None:
        mounts = []
        for line in self.COVERED.splitlines():
            fields = line.split()
            sep = fields.index("-")
            mounts.append(f"{fields[sep + 2]} {fields[4]} {fields[sep + 1]} {fields[5]} 0 0")
        self.rig.write("/proc/self/mounts", "\n".join(mounts) + "\n")
        self.rig.write("/proc/self/mountinfo", self.COVERED)

    def test_a_covered_mount_point_is_hidden_and_never_stated(self):
        """Only the mounts a lookup reaches are stat'ed (the cover's st_dev is /run's for all of them, so none is
        dropped as another's duplicate); a disk mounted again above its cover is the one a lookup reaches."""
        self.covered()
        asked = []

        def stat_of(path):
            asked.append(path)
            return os.stat_result((0,) * 2 + (26 if path != "/" else 2049,) + (0,) * 7)
        found = ys.read_mounts(str(self.rig.at("/proc/self/mounts")), stat_of=stat_of)
        self.assertEqual([(m["mount"], m.get("hidden", False)) for m in found],
                         [("/", False), ("/srv/a b", False), ("/backup", True), ("/home", True), ("/srv/data", True)])
        self.assertEqual(sorted(asked), ["/", "/srv/a b"], "a covered point is never stat'ed")
        before = ys.read_mounts(str(self.rig.at("/proc/self/mounts")), info="", stat_of=stat_of)
        self.assertEqual((before[0]["mount"], [m for m in before if m.get("hidden")]), ("/", []),
                         "without mountinfo: /proc/self/mounts as before, nothing marked")

    def test_a_covered_disk_comes_from_the_helper_or_cannot_be_seen(self):
        area = self.area()
        self.covered()
        area.facts["disks"] = {"filesystems": [{"mount": "/home", "free": 900 * 10 ** 9, "total": 2 * 10 ** 12}]}
        asked = []

        def statvfs(path):
            asked.append(path)
            return os.statvfs_result((4096, 4096, 10 ** 8, 5 * 10 ** 7, 5 * 10 ** 7, 0, 0, 0, 0, 255))
        with unittest.mock.patch.object(ys.os, "statvfs", statvfs):
            disks = {d["mount"]: d for d in area.disks(area.settings()["host"])}
            view = area.host(force=True)
            checks = area.checks_view()
        self.assertNotIn("/backup", asked)
        self.assertNotIn("/home", asked)
        self.assertNotIn("/srv/data", asked)
        backup = disks["/backup"]
        self.assertEqual((backup["words"], backup["tone"], backup["free"], backup["total"], backup["hidden"]),
                         ("/backup: cannot be seen from Seraphina's service", "muted", None, None, True))
        self.assertEqual((disks["/home"]["via_helper"], disks["/home"]["free"], disks["/home"]["hidden"]),
                         (True, 900 * 10 ** 9, True), "the helper's numbers, from PID 1's namespace")
        self.assertEqual(disks["/srv/data"]["words"], "/srv/data: cannot be seen from Seraphina's service")
        self.assertEqual((disks["/"]["via_helper"], disks["/"]["hidden"]), (False, False))
        self.assertIn(("/backup: cannot be seen from Seraphina's service", "muted"),
                      [(i["words"], i["tone"]) for i in view["items"]])
        self.assertNotIn("/backup", [d["mount"] for d in checks["disks"]], "Disk use cannot measure it")
        with self.assertRaises(ValueError):
            area.run_check("disk", {"mount": "/backup"})
        result = ys.serverchecks.disk_result(list(disks.values()), None, None, None, None)
        self.assertIn("/backup: cannot be seen from Seraphina's service", [r["words"] for r in result["rows"]])
        self.assertEqual(result["tone"], "ok", "a disk not seen is never a full one")

    def test_disk_tones(self):
        cfg = settings_mod_defaults()["host"]
        gb = 10 ** 9
        self.assertEqual(ys.disk_tone(71 * gb, 465 * gb, cfg), "ok")
        self.assertEqual(ys.disk_tone(60 * gb, 465 * gb, cfg), "warn", "below 15% of 465 GB")
        self.assertEqual(ys.disk_tone(15 * gb, 465 * gb, cfg), "err", "below 20 GB")
        self.assertEqual(ys.disk_tone(150 * gb, 16000 * gb, cfg), "ok", "100 GB cap on a big disk")
        self.assertEqual(ys.disk_tone(90 * gb, 16000 * gb, cfg, {"full_on_purpose": True}), "ok")
        self.assertEqual(ys.disk_tone(10 * gb, 16000 * gb, cfg, {"full_on_purpose": True}), "err")

    def test_gpus(self):
        lines = {"gpu": "0, GPU-00000000-1111-2222-3333-444444444444, Example GPU, 9300, 24576, 61, 37\n",
                 "apps": "4101, 9000, GPU-00000000-1111-2222-3333-444444444444\n"}
        gpus = ys.read_gpus(lambda argv: (0, lines["gpu" if "--query-gpu" in argv[1] else "apps"]))
        self.assertEqual(gpus["cards"][0]["uuid"], "GPU-00000000-1111-2222-3333-444444444444")
        self.assertEqual(gpus["processes"], [{"pid": 4101, "used_mib": 9000,
                                              "gpu_uuid": "GPU-00000000-1111-2222-3333-444444444444"}])
        for code, words in ((9, "the driver is not loaded"), (4, "no permission"), (6, "No GPU was found")):
            self.assertIn(words, ys.read_gpus(lambda argv: (code, ""))["words"])
        self.assertEqual(ys.read_gpus(lambda argv: (None, ""))["state"], "no_tool")


def settings_mod_defaults() -> dict:
    from swfactory.config import DEFAULTS

    return json.loads(json.dumps(DEFAULTS["yourserver"]))


class GroupingTests(AreaCase):
    """T10: one app per Compose folder and project name; Dockge stacks; sidecars joined; one-off and job runs left out;
    renamed predecessors and old copies; the owner's merges, splits and hides survive refreshes."""

    def test_the_apps(self):
        area = self.area()
        cards = self.cards(area)
        self.assertEqual(set(cards), {"a-ai", "a-beszel", "a-cockpit", "a-dockge", "a-filebrowser", "a-gateway",
                                      "a-media", "a-mediaold", "a-notes", "a-photos"})
        self.assertEqual(cards["a-photos"]["managed"]["words"], "Managed by Dockge (stack photos)")
        self.assertEqual(cards["a-mediaold"]["managed"]["words"], "Made from the same folder as the Dockge stack "
                                                                  "media, under another name. Dockge does not show it.")
        self.assertEqual((cards["a-mediaold"]["name"], cards["a-media"]["name"]), ("mediaold", "media"),
                         "two projects from one folder: two cards")
        self.assertEqual(cards["a-notes"]["name"], "notes", "a compose file in a home folder: named by its service")
        self.assertEqual(cards["a-notes"]["managed"]["state"], "owned:compose")
        self.assertEqual(cards["a-dockge"]["name"], "Dockge", "recognised by its image")
        self.assertEqual(cards["a-filebrowser"]["managed"]["words"], "Started by hand.")
        self.assertEqual(cards["a-cockpit"]["managed"]["state"], "owned:systemd")
        ns = area.cards()["not_shown"]
        self.assertEqual((ns["one_off"], ns["job_runs"], ns["unknown_format"]), (1, 1, 1))

    def test_sidecars_join_the_apps_they_serve(self):
        area = self.area()
        built = area._built()
        ids = built["record"]["ids"]
        by = {ids[a["key"]]: a for a in built["apps"]}
        names = {app: sorted(m["container"]["name"] for m in a["members"]) for app, a in by.items()}
        self.assertIn("photos-ts-1", names["a-photos"], "by container name on a shared network")
        self.assertIn("media-ts-1", names["a-media"], "the app shares the sidecar's namespace")
        self.assertIn("tsshared-ts-1", names["a-notes"], "a sidecar in a stack of its own joins its first app")
        self.assertNotIn("a-tsshared", by, "and leaves no sidecar-only card")
        self.assertEqual([c["url"] for c in by["a-filebrowser"]["candidates"] if c["source"] == "tailscale"],
                         ["https://shared.example.ts.net:8443"], "one sidecar, two apps, each its own candidate")
        self.assertEqual(self.cards(area)["a-filebrowser"]["shares_with"], ["a-notes"])

    def test_old_copies_and_job_runs(self):
        area = self.area()
        cards = self.cards(area)
        photos = {o["name"]: o["state"] for o in cards["a-photos"]["old_copies"]}
        self.assertEqual(photos, {"photos_old": "proposed"}, "a renamed predecessor with the same labels")
        self.assertEqual({o["name"] for o in cards["a-media"]["old_copies"]}, {"jellyfin-2023"},
                         "a stopped container by hand with the same image repository")
        self.assertEqual(cards["a-photos"]["job_runs"], 1, "a job run sharing photos' exact image")
        self.assertEqual(cards["a-photos"]["numbers"]["ram"], 1_200_000_000 + 2 * 200_000_000,
                         "old copies are left out of the RAM sum")

    def test_a_recreate_is_the_same_member(self):
        area = self.area()
        area.app_update("a-photos", {"name": "Family photos"})
        old = IDS["photos"]
        new = "9" * 64
        folder = self.rig.at(f"/var/lib/docker/containers/{old}")
        data = json.loads((folder / "config.v2.json").read_text())
        data["ID"] = new
        data["Image"] = "sha256:" + "c" * 64
        os.rename(folder, self.rig.at(f"/var/lib/docker/containers/{new}"))
        (self.rig.at(f"/var/lib/docker/containers/{new}") / "config.v2.json").write_text(json.dumps(data))
        area.refresh_map()
        cards = self.cards(area)
        self.assertEqual(cards["a-photos"]["name"], "Family photos", "the owner's name stays on the member")
        self.assertIn("image", [c["kind"] for c in area.record.changes("a-photos")], "one change-log line")

    def test_the_owners_corrections_survive(self):
        area = self.area()
        area.groups({"action": "merge", "app": "a-mediaold", "into": "a-media"})
        area.app_update("a-filebrowser", {"hidden": True})
        area.refresh_map()
        cards = area.cards()
        ids = {c["id"] for c in cards["apps"]}
        self.assertNotIn("a-mediaold", ids)
        self.assertNotIn("a-filebrowser", ids)
        self.assertEqual([h["id"] for h in cards["not_shown"]["hidden"]], ["a-filebrowser"])
        area.groups({"action": "undo"})
        area.groups({"action": "undo"})
        self.assertIn("a-mediaold", {c["id"] for c in area.cards()["apps"]}, "undone with one click each")
        photos_db = next(m for m in area._built()["apps"] if area.record.load()["ids"][m["key"]] == "a-photos")
        db = next(m["key"] for m in photos_db["members"] if m["container"]["name"] == "photos-db-1")
        area.groups({"action": "split", "member": db, "app": "a-photos"})
        area.refresh_map()
        names = {c["name"] for c in area.cards()["apps"]}
        self.assertIn("photos", names)
        self.assertEqual(len(area.cards()["apps"]), 11, "the split part is an app of its own")

    def test_a_removed_member_is_dated_never_deleted(self):
        area = self.area()
        os.rename(self.rig.at(f"/var/lib/docker/containers/{IDS['filebrowser']}"),
                  self.rig.at("/var/lib/docker/gone-filebrowser"))
        area.refresh_map()
        members = area.record.load()["members"]
        gone = [m for m in members.values() if m.get("app") == "a-filebrowser"]
        self.assertTrue(gone and gone[0].get("removed_at"), "kept with its date")


class AddressTests(AreaCase):
    """T11: candidates in order; the fingerprint asks again; a confirmed address never changes by itself; refusals; an
    edit outside the API breaks the chain and the Open link waits."""

    def test_candidates_in_order_and_ticks(self):
        area = self.area()
        built = area._built()
        ids = built["record"]["ids"]
        by = {ids[a["key"]]: a for a in built["apps"]}
        self.assertEqual([c["source"] for c in by["a-filebrowser"]["candidates"]], ["tailscale", "gateway", "port"])
        rows = {r["app"]: r for r in area.review_view()["rows"]}
        self.assertTrue(rows["a-photos"]["ticked"])
        self.assertFalse(rows["a-beszel"]["ticked"], "a gateway route is never ticked in advance")
        self.assertEqual(rows["a-beszel"]["url"], "https://beszel.home.example")
        self.assertIsNone(rows["a-ai"]["url"])

    def test_url_rule(self):
        home = ["192.0.2.10", "beszel.home.example"]
        for url, ok in (("https://photos.example.ts.net", True), ("http://photos.example.ts.net", False),
                        ("https://photos.other.ts.net", False), ("javascript:alert(1)", False),
                        ("https://evil.example.com", False), ("http://192.0.2.10:8081", True),
                        ("http://192.0.2.99:8081", False), ("https://beszel.home.example", True),
                        ("http://127.0.0.1:9090", True), ("https://user@photos.example.ts.net", False),
                        ("https://photos.example.ts.net/?q=1", False), ("http://0.0.0.0:80", False)):
            with self.subTest(url=url):
                self.assertEqual(ys.url_problem(url, "example.ts.net", home) is None, ok)
        self.assertIsNotNone(ys.url_problem("https://photos.example.ts.net", None, home), "suffix not confirmed")

    def test_confirm_and_the_fingerprint(self):
        area = self.area()
        confirm_all(area)
        card = self.cards(area)["a-photos"]
        self.assertEqual((card["address"]["state"], card["address"]["url"]), ("confirmed",
                                                                              "https://photos.example.ts.net"))
        state = json.loads(self.rig.at("/opt/stacks/photos/ts-state/tailscaled.state").read_text())
        from tests.sfsystem_rig import b64, web
        state["_serve/a1b2"] = b64(web("photos.example.ts.net", 443, "http://photos:2343"))
        self.rig.write("/opt/stacks/photos/ts-state/tailscaled.state", json.dumps(state), 0o600)
        area.refresh_map()
        card = self.cards(area)["a-photos"]
        self.assertEqual(card["address"]["state"], "changed", "a new target asks again")
        self.assertIsNone(card["address"]["url"], "and the Open link waits")
        saved = area.record.load()["apps"]["a-photos"]["address"]["url"]
        self.assertEqual(saved, "https://photos.example.ts.net", "never changed by itself")
        area.address("a-photos", {"action": "confirm", "candidate": card["address"]["proposed_candidate"]})
        self.assertEqual(self.cards(area)["a-photos"]["address"]["state"], "confirmed")

    def test_refusals(self):
        area = self.area()
        for url in ("https://photos.other.ts.net", "javascript:alert(1)", "https://evil.example.com/"):
            with self.assertRaises(ValueError):
                area.address("a-photos", {"action": "set", "url": url})
            with self.assertRaises(ValueError):
                area.review_confirm([{"app": "a-photos", "url": url}])

    def test_an_outside_edit_breaks_the_chain(self):
        area = self.area()
        confirm_all(area)
        path = area.folder() / "apps.json"
        doc = json.loads(path.read_text())
        doc["record"]["apps"]["a-photos"]["address"]["url"] = "https://photos-login.example.ts.net"
        path.write_text(json.dumps(doc))
        cards = self.cards(area)
        self.assertEqual(cards["a-photos"]["address"]["state"], "changed_outside")
        self.assertEqual(cards["a-photos"]["address"]["words"], ys.OUTSIDE_EDIT)
        self.assertIsNone(cards["a-photos"]["address"]["url"])
        self.assertEqual(cards["a-media"]["address"]["state"], "confirmed", "only the address it touched waits")
        lines = (area.folder() / "changes.jsonl").read_text().splitlines()
        chain = [json.loads(line) for line in lines]
        self.assertTrue(all(b["prev"] == a["hash"] for a, b in zip(chain, chain[1:])), "each line holds the last hash")

    def test_an_outside_edit_still_waits_after_the_harness_writes_and_restarts(self):
        """The harness's own write (a refresh's member stamps) seals the edited file again; the wait is sealed with
        it, so the edited address is never trusted, with a QR code, after a restart."""
        now = [time.time()]
        area = self.area(clock=lambda: now[0])
        confirm_all(area)
        path = area.folder() / "apps.json"
        doc = json.loads(path.read_text())
        doc["record"]["apps"]["a-photos"]["address"]["url"] = "https://evil.example.ts.net"
        path.write_text(json.dumps(doc))
        now[0] += 400
        area.refresh_map()
        self.assertNotEqual(json.loads(path.read_text())["hash"], doc["hash"], "the harness wrote and sealed it")
        self.assertEqual(self.cards(area)["a-photos"]["address"]["state"], "changed_outside")
        fresh = make(self.rig, {"data_dir": str(area.config.data_dir)})
        fresh.refresh_map()
        address = self.cards(fresh)["a-photos"]["address"]
        self.assertEqual((address["state"], address["url"], address["qr"]), ("changed_outside", None, False))
        self.assertEqual(self.cards(fresh)["a-media"]["address"]["state"], "confirmed", "only the edited one waits")
        found = next(c["id"] for c in fresh.app_view("a-photos")["candidates"]
                     if c["url"] == "https://photos.example.ts.net")
        fresh.address("a-photos", {"action": "confirm", "candidate": found})
        again = make(self.rig, {"data_dir": str(area.config.data_dir)})
        again.refresh_map()
        address = self.cards(again)["a-photos"]["address"]
        self.assertEqual((address["state"], address["url"]), ("confirmed", "https://photos.example.ts.net"),
                         "the owner's answer is sealed too")

    def test_yes_to_an_address_changed_outside_saves_the_address_shown(self):
        """The question shows the saved address: Yes (confirm, no candidate named) saves that address, checked again,
        never the found one whose fingerprint it carries or the first one found."""
        area = self.area()
        confirm_all(area)
        path = area.folder() / "apps.json"
        doc = json.loads(path.read_text())
        doc["record"]["apps"]["a-photos"]["address"]["url"] = "https://photos-login.example.ts.net"
        path.write_text(json.dumps(doc))
        address = self.cards(area)["a-photos"]["address"]
        self.assertEqual((address["state"], address["saved"]),
                         ("changed_outside", "https://photos-login.example.ts.net"))
        area.address("a-photos", {"action": "confirm"})
        saved = area.record.load()["apps"]["a-photos"]["address"]
        self.assertEqual((saved["url"], saved["source"], saved["fingerprint"]),
                         ("https://photos-login.example.ts.net", "typed", None), "no found address has that URL")
        self.assertNotIn("a-photos", area.record.outside)
        address = self.cards(area)["a-photos"]["address"]
        self.assertEqual((address["state"], address["url"]), ("confirmed", "https://photos-login.example.ts.net"))
        # changed outside back to a found one's URL: Yes saves it as that one, with its source and fingerprint
        doc = json.loads(path.read_text())
        doc["record"]["apps"]["a-photos"]["address"]["url"] = "https://photos.example.ts.net"
        path.write_text(json.dumps(doc))
        self.assertEqual(self.cards(area)["a-photos"]["address"]["state"], "changed_outside")
        area.address("a-photos", {"action": "home_only"})
        saved = area.record.load()["apps"]["a-photos"]["address"]
        found = next(c for c in area.app_view("a-photos")["candidates"] if c["url"] == "https://photos.example.ts.net")
        self.assertEqual((saved["url"], saved["candidate"], saved["source"], saved["home_only"]),
                         ("https://photos.example.ts.net", found["id"], found["source"], True))
        # an edited address outside the tailnet is refused, never swapped for a found one
        doc = json.loads(path.read_text())
        doc["record"]["apps"]["a-photos"]["address"]["url"] = "https://evil.example.com"
        path.write_text(json.dumps(doc))
        with self.assertRaises(ValueError):
            area.address("a-photos", {"action": "confirm"})
        self.assertEqual(area.record.load()["apps"]["a-photos"]["address"]["url"], "https://evil.example.com")
        self.assertEqual(self.cards(area)["a-photos"]["address"]["state"], "changed_outside", "it still waits")

    def test_no_to_an_address_changed_outside_removes_only_that_address(self):
        """The question shows the edited saved address: No (reject, no candidate named, as the page sends it) removes
        that address and puts nothing aside, so the found one whose fingerprint it carried is proposed again; an
        older client naming that found one puts it aside only when its URL is the one the owner said no to."""
        area = self.area()
        confirm_all(area)
        found = area.record.load()["apps"]["a-photos"]["address"]["candidate"]
        path = area.folder() / "apps.json"

        def edit(url):
            doc = json.loads(path.read_text())
            doc["record"]["apps"]["a-photos"]["address"].update(url=url, note="edited")
            path.write_text(json.dumps(doc))
            address = self.cards(area)["a-photos"]["address"]
            self.assertEqual((address["state"], address["saved"]), ("changed_outside", url))
            return address
        edit("https://evil.example.ts.net")
        area.address("a-photos", {"action": "reject"})
        entry = area.record.load()["apps"]["a-photos"]
        self.assertEqual((entry.get("address"), entry.get("rejected")), (None, None), "only the saved address went")
        address = self.cards(area)["a-photos"]["address"]
        self.assertEqual((address["state"], address["proposed"], address["proposed_candidate"]),
                         ("proposed", "https://photos.example.ts.net", found))
        row = next(r for r in area.review_view()["rows"] if r["app"] == "a-photos")
        self.assertEqual(row["candidate"], found, "and the review list proposes it")
        # an older client still names the found candidate the card carried: it is not put aside for a URL it lacks
        confirm_all(area)
        sent = edit("https://evil.example.ts.net")
        self.assertEqual(sent["candidate"], found)
        area.address("a-photos", {"action": "reject", "candidate": sent["candidate"]})
        entry = area.record.load()["apps"]["a-photos"]
        self.assertEqual((entry.get("address"), entry.get("rejected")), (None, None))
        # with the URL left as found, No to that address is No to the found one too
        confirm_all(area)
        sent = edit("https://photos.example.ts.net")
        area.address("a-photos", {"action": "reject", "candidate": sent["candidate"]})
        self.assertEqual(area.record.load()["apps"]["a-photos"].get("rejected"), [found])

    def test_yes_with_no_candidate_named_to_a_found_address_that_changed_saves_the_one_proposed(self):
        """State changed (the saved address's candidate is no longer found): the question shows the one proposed, so
        Yes or home-only with no candidate named saves it with its source and fingerprint, never the stale saved
        address as a typed one; naming the saved one's gone candidate still answers for the saved address."""
        def stale(record):
            record["apps"]["a-photos"]["address"].update(url="https://photos-old.example.ts.net",
                                                         fingerprint="f" * 64, candidate="f" * 12)
        for action in ("home_only", "confirm"):
            with self.subTest(action=action):
                area = self.area()
                confirm_all(area)
                area.record.change("address", "a-photos", stale, what="test")
                address = self.cards(area)["a-photos"]["address"]
                self.assertEqual((address["state"], address["words"]), (
                    "changed", "Is this the address you use for this app? https://photos.example.ts.net"))
                built = area._built()
                app = next(a for a in built["apps"] if built["record"]["ids"][a["key"]] == "a-photos")
                proposed = next(c for c in app["candidates"] if c["id"] == address["proposed_candidate"])
                area.address("a-photos", {"action": action})
                saved = area.record.load()["apps"]["a-photos"]["address"]
                self.assertEqual((saved["url"], saved["source"], saved["fingerprint"], saved["candidate"],
                                  saved["home_only"]), (proposed["url"], proposed["source"], proposed["fingerprint"],
                                                        proposed["id"], action == "home_only"))
                self.assertEqual(self.cards(area)["a-photos"]["address"]["state"], "confirmed")
        area.record.change("address", "a-photos", stale, what="test")
        area.address("a-photos", {"action": "confirm", "candidate": "f" * 12})
        saved = area.record.load()["apps"]["a-photos"]["address"]
        self.assertEqual((saved["url"], saved["source"]), ("https://photos-old.example.ts.net", "typed"),
                         "the saved one named: the saved address")

    def test_yes_keeps_a_typed_address(self):
        """A typed home address touched outside the API: Yes keeps it typed, never the first one found; a candidate
        named that is no longer found is refused, never answered with the saved address."""
        area = self.area()
        confirm_all(area)
        area.address("a-photos", {"action": "set", "url": "http://192.0.2.10:9999", "home_only": True})
        path = area.folder() / "apps.json"
        doc = json.loads(path.read_text())
        doc["record"]["apps"]["a-photos"]["address"]["note"] = "touched"
        path.write_text(json.dumps(doc))
        self.assertEqual(self.cards(area)["a-photos"]["address"]["state"], "changed_outside")
        area.address("a-photos", {"action": "confirm"})
        saved = area.record.load()["apps"]["a-photos"]["address"]
        self.assertEqual((saved["url"], saved["source"], saved["home_only"]), ("http://192.0.2.10:9999", "typed", True))
        self.assertEqual(self.cards(area)["a-photos"]["address"]["state"], "confirmed")
        with self.assertRaises(ValueError):
            area.address("a-photos", {"action": "confirm", "candidate": "0" * 12})
        self.assertEqual(area.record.load()["apps"]["a-photos"]["address"]["url"], "http://192.0.2.10:9999")

    def test_no_its_another_one(self):
        """A rejected candidate is never proposed again; the next one is, else "Type the one you use"; naming it
        later still confirms it; rejecting a new proposal keeps the confirmed address."""
        area = self.area()
        built = area._built()
        app = next(a for a in built["apps"] if built["record"]["ids"][a["key"]] == "a-filebrowser")
        found = [c["id"] for c in app["candidates"]]
        self.assertGreaterEqual(len(found), 2)
        self.assertEqual(self.cards(area)["a-filebrowser"]["address"]["proposed_candidate"], found[0])
        area.address("a-filebrowser", {"action": "reject", "candidate": found[0]})
        address = self.cards(area)["a-filebrowser"]["address"]
        self.assertEqual((address["state"], address["proposed_candidate"]), ("proposed", found[1]))
        row = next(r for r in area.review_view()["rows"] if r["app"] == "a-filebrowser")
        self.assertEqual(row["candidate"], found[1], "the review list proposes the next one")
        for candidate in found[1:]:
            area.address("a-filebrowser", {"action": "reject", "candidate": candidate})
        address = self.cards(area)["a-filebrowser"]["address"]
        self.assertEqual((address["state"], address["words"]), ("none", ys.NO_ADDRESS))
        area.address("a-filebrowser", {"action": "confirm", "candidate": found[0]})
        self.assertEqual(self.cards(area)["a-filebrowser"]["address"]["state"], "confirmed", "named, it still confirms")
        self.assertNotIn(found[0], area.record.load()["apps"]["a-filebrowser"].get("rejected") or [])
        # a new target proposes a new address; "No" to it keeps the one the owner confirmed
        confirm_all(area)
        state = json.loads(self.rig.at("/opt/stacks/photos/ts-state/tailscaled.state").read_text())
        from tests.sfsystem_rig import b64, web
        state["_serve/a1b2"] = b64(web("photos.example.ts.net", 443, "http://photos:2343"))
        self.rig.write("/opt/stacks/photos/ts-state/tailscaled.state", json.dumps(state), 0o600)
        area.refresh_map()
        address = self.cards(area)["a-photos"]["address"]
        self.assertEqual(address["state"], "changed")
        area.address("a-photos", {"action": "reject", "candidate": address["proposed_candidate"]})
        self.assertEqual(area.record.load()["apps"]["a-photos"]["address"]["url"], "https://photos.example.ts.net")
        self.assertNotEqual(self.cards(area)["a-photos"]["address"].get("proposed_candidate"),
                            address["proposed_candidate"])

    def test_after_a_restart_an_address_no_candidate_matches_waits(self):
        area = self.area()
        confirm_all(area)
        os.unlink(self.rig.at("/opt/stacks/photos/ts-state/tailscaled.state"))
        fresh = make(self.rig, {"data_dir": str(area.config.data_dir)})
        fresh.refresh_map()
        self.assertEqual(self.cards(fresh)["a-photos"]["address"]["state"], "changed_outside")

    def test_the_qr_code_only_for_a_confirmed_tailnet_address(self):
        area = self.area()
        with self.assertRaises(ValueError):
            area.qr("a-photos")
        confirm_all(area)
        code = area.qr("a-photos")
        self.assertEqual(code["size"], len(code["rows"]))
        with self.assertRaises(ValueError):
            area.qr("a-cockpit")
        with self.assertRaises(KeyError):
            area.qr("a-nothing")


class LightTests(AreaCase):
    """T5 and T12: each row of the light table, the main member, never green on stale facts, GPU and model apps get a
    connection only, a stop inside a power hold counts from its end."""

    def light(self, area, app):
        return self.cards(area)[app]["light"]

    def test_the_table(self):
        now = time.time()
        stop(self.rig, "ollama", 1, False, now - 600)
        stop(self.rig, "notes", 137, True, now - 7200)
        stop(self.rig, "filebrowser", 137, True, now - 60)
        stop(self.rig, "dockge", 0, False, now - 60)
        area = self.area()
        confirm_all(area)
        area.app_update("a-dockge", {"expect": "off"})
        self.assertEqual(self.light(area, "a-ai")["code"], "down_error", "non-zero without the flag: red")
        self.assertEqual(self.light(area, "a-ai")["tone"], "err")
        self.assertEqual(self.light(area, "a-notes")["code"], "stopped", "137 with the flag, past its grace: amber")
        self.assertEqual(self.light(area, "a-notes")["tone"], "warn")
        self.assertEqual(self.light(area, "a-filebrowser")["code"], "stopped_recent", "inside its grace: not yet")
        self.assertEqual(self.light(area, "a-dockge")["tone"], "off", "expected off, whatever its state")
        self.assertEqual(self.light(area, "a-photos")["code"], "unchecked", "no probe yet")
        area.probe_all(time.time())
        self.assertEqual(self.light(area, "a-photos")["code"], "working")

    def test_never_started_restarting_and_unknown_expect(self):
        area = self.area()
        app = {"kind": "compose", "members": []}
        base = dict(now=1000.0, grace_minutes=15, facts_at=990.0, stale_minutes=10, running_of=lambda c: False,
                    probe=None, restarts=0, unit_state=None, holds_end=None, docker_active=True, sidecar_ids=set())
        never = {"key": "m", "container": {"id": "x", "started_before": False, "started_at": "0001-01-01T00:00:00Z",
                                           "restart_policy": "always"}}
        self.assertEqual(ys.compute_light(dict(app, members=[never]), expect="run", main=never, **base)["code"],
                         "never_started")
        looping = {"key": "m", "container": {"id": "x", "restarting": True}}
        self.assertEqual(ys.compute_light(dict(app, members=[looping]), expect="run", main=looping,
                                          **dict(base, running_of=lambda c: True))["tone"], "err")
        oneshot = {"key": "m", "container": {"id": "x", "restart_policy": "no", "exit_code": 0, "started_before": True,
                                             "finished_at": "1970-01-01T00:10:00Z"}}
        self.assertEqual(ys.compute_light(dict(app, members=[oneshot]), expect=None, main=oneshot, **base)["code"],
                         "finished")
        stale = ys.compute_light(dict(app, members=[looping]), expect="run", main=looping,
                                 **dict(base, facts_at=0.0, running_of=lambda c: True))
        self.assertEqual(stale["tone"], "muted", "green is never shown on stale facts")
        self.assertTrue(area)

    def test_three_restarts_in_ten_minutes(self):
        area = self.area()
        c = {"id": "x", "restart_count": 2}
        self.assertEqual(area._restart_count(c, 0.0), 0)
        c["restart_count"] = 5
        self.assertEqual(area._restart_count(c, 300.0), 3)
        c["restart_count"] = 5
        self.assertEqual(area._restart_count(c, 1200.0), 0, "outside the window")

    def test_stale_facts_are_grey(self):
        area = self.area()
        area.facts["at"] = time.time() - 3600
        self.assertTrue(all(c["light"]["tone"] == "muted" for c in area.cards()["apps"]))

    def test_a_failed_containers_read_never_shows_an_old_running(self):
        """Docker's answer keeps its own time: a stopped app whose answer could not be read again is grey, never
        green from the old answer, and every container card is grey once that answer is past stale_minutes."""
        now = [time.time()]
        area = self.area(clock=lambda: now[0])
        confirm_all(area)
        stop(self.rig, "dockge", 1, False, now[0] - 60)

        def runner(argv, data, timeout):
            if json.loads(data)["verb"] == "containers":
                return None, b"", b"(timed out)"
            return self.rig.runner(argv, data, timeout)
        area._client = HelperClient(sx.HELPER, runner=runner, name="Seraphina's system access")
        now[0] += 5
        area.numbers(force=True)
        area.refresh_map()
        self.assertIn("containers", area.errors)
        light = self.light(area, "a-dockge")
        self.assertEqual((light["tone"], light["code"]), ("muted", "stale"), "never green from the old answer")
        self.assertEqual(self.light(area, "a-photos")["tone"], "ok", "a part its scan still finds is still read")
        self.assertFalse(area.cards()["stale"])
        now[0] += 11 * 60
        area.numbers(force=True)
        area.refresh_map()
        cards = area.cards()
        self.assertTrue(cards["stale"])
        self.assertTrue(all(c["light"]["tone"] == "muted" for c in cards["apps"] if c["kind"] != "unit"))

    def test_a_service_started_on_demand_is_idle_not_stopped(self):
        """Cockpit's service is inactive between visits while cockpit.socket listens: ready, never "Stopped", and
        no notice or item; with its socket stopped too it is stopped, and a failed service is red."""
        states = {"cockpit.service": "ActiveState=inactive\nSubState=dead\n",
                  "cockpit.socket": "ActiveState=active\nSubState=listening\n"}
        events = []
        notifier = unittest.mock.Mock()
        notifier.app_event.side_effect = lambda kind, key, subject=None: events.append((kind, key))
        area = self.area(systemctl=lambda argv: states.get(argv[-1], "ActiveState=active\n"), notifier=notifier)
        confirm_all(area)
        light = self.light(area, "a-cockpit")
        self.assertEqual((light["tone"], light["code"]), ("ok", "idle"))
        for _ in range(3):
            area.findings(time.time())
        self.assertNotIn(("service.down", "a-cockpit"), events)
        self.assertFalse(any(n["key"].endswith(":a-cockpit") for n in area.needs(area.cards())))
        states["cockpit.socket"] = "ActiveState=inactive\nSubState=dead\n"
        area._units.clear()
        self.assertEqual(self.light(area, "a-cockpit")["code"], "stopped")
        states["cockpit.service"] = "ActiveState=failed\nSubState=failed\n"
        area._units.clear()
        self.assertEqual(self.light(area, "a-cockpit")["code"], "unit_failed")

    def test_gpu_and_model_apps_get_a_connection_only(self):
        calls = []
        area = self.area(prober=ys.Prober(http=lambda h, p, path, host: calls.append(("http", h, p)) or {"status": 200},
                                          tcp=lambda h, p: calls.append(("tcp", h, p)) or {"status": 0}))
        area.facts["gpu"] = {"processes": [{"container": IDS["media"], "used_mib": 300}]}
        confirm_all(area)
        area.probe_all(time.time())
        self.assertIn(("tcp", "172.20.0.2", 8096), calls, "media holds GPU memory: a connection only")
        self.assertIn(("http", "172.19.0.3", 2342), calls)

    def test_a_stop_inside_a_power_hold_counts_from_its_end(self):
        now = time.time()
        stop(self.rig, "photos", 137, True, now - 7200)
        self.rig.write("/run/seraphina/holds/system-J-1.json", {
            "id": "J-1", "holder": "backup", "why": "nightly", "resources": ["app:a-photos"],
            "until": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 120))})
        area = self.area()
        confirm_all(area)
        self.assertEqual(self.light(area, "a-photos")["code"], "stopped_recent", "the hold ended 2 minutes ago")

    def test_the_main_member(self):
        area = self.area()
        built = area._built()
        app = next(a for a in built["apps"] if built["record"]["ids"][a["key"]] == "a-photos")
        main = ys.main_member(app, None, None, {IDS["photos-ts"]})
        self.assertEqual(main["container"]["name"], "photos-photos-1", "the one nothing depends on")
        media = next(a for a in built["apps"] if built["record"]["ids"][a["key"]] == "a-media")
        confirm_all(area)
        self.assertEqual(self.cards(area)["a-media"]["version"]["value"], "10.9.11",
                         "the app the address reaches, not its Tailscale container")
        self.assertTrue(media)


class GatewayTests(AreaCase):
    """T38 and T37's harness part: routes become candidates for their upstream's app, never the gateway's; probes go
    to the upstream; the gateway's card is red when a part is down while the apps behind it keep their own lights."""

    def test_routes_and_probes(self):
        calls = []
        area = self.area(prober=ys.Prober(http=lambda h, p, path, host: calls.append((h, p, host)) or {"status": 200},
                                          tcp=lambda h, p: {"status": 0}))
        cards = self.cards(area)
        self.assertEqual(cards["a-gateway"]["address"]["state"], "none", "the gateway owns no route's address")
        self.assertTrue(cards["a-gateway"]["gateway"])
        confirm_all(area)
        area.probe_all(time.time())
        self.assertIn(("172.21.0.5", 8090, "beszel.home.example"), calls, "the upstream, never the route")
        self.assertFalse(any(h == "192.0.2.10" for h, _, _ in calls), "never the gateway's own address")

    def test_a_gateway_part_down(self):
        stop(self.rig, "gateway-dex", 2, False, time.time() - 900)
        area = self.area()
        confirm_all(area)
        area.probe_all(time.time())
        cards = self.cards(area)
        self.assertEqual(cards["a-gateway"]["light"]["code"], "gateway_down")
        self.assertEqual(cards["a-beszel"]["light"]["code"], "working", "apps behind it keep their own lights")

    def test_a_one_shot_part_that_finished_is_not_down(self):
        """§2.5: a one-shot member that finished with code 0 (restart policy no) is left out of the light; with
        another exit code it is down."""
        host = self.rig.at(f"/var/lib/docker/containers/{IDS['gateway-db']}/hostconfig.json")
        data = json.loads(host.read_text())
        data["RestartPolicy"] = {"Name": "no"}
        host.write_text(json.dumps(data))
        stop(self.rig, "gateway-db", 0, False, time.time() - 3600)
        events = []
        notifier = unittest.mock.Mock()
        notifier.app_event.side_effect = lambda kind, key, subject=None: events.append((kind, key))
        area = self.area(notifier=notifier)
        confirm_all(area)
        self.assertNotEqual(self.cards(area)["a-gateway"]["light"]["code"], "gateway_down")
        for _ in range(3):
            area.findings(time.time())
        self.assertNotIn(("service.down", "a-gateway"), events)
        stop(self.rig, "gateway-db", 1, False, time.time() - 3600)
        area.refresh_map()
        self.assertEqual(self.cards(area)["a-gateway"]["light"]["code"], "gateway_down")


class ProbeTests(unittest.TestCase):
    """T16: one GET (or a TCP connect), no cookie, credential or redirect followed, at most once a minute per app,
    backing off after 10 failures."""

    def test_one_plain_get(self):
        seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                seen.append({"path": self.path, "headers": dict(self.headers)})
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:1/elsewhere")
                self.send_header("Set-Cookie", "session=x")
                self.end_headers()

            def log_message(self, *a):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        result = ys.http_probe("127.0.0.1", server.server_port, "/", "photos.example.ts.net")
        self.assertEqual(result["status"], 302, "a redirect is an answer, not followed")
        self.assertEqual(len(seen), 1)
        headers = {k.lower(): v for k, v in seen[0]["headers"].items()}
        self.assertEqual(headers["host"], "photos.example.ts.net")
        self.assertFalse({"cookie", "authorization", "origin", "user-agent"} & set(headers))

    def test_the_schedule(self):
        calls = []
        prober = ys.Prober(http=lambda h, p, path, host: calls.append(1) or {"status": None, "error": "refused"})
        target = {"host": "127.0.0.1", "port": 9}
        for second in range(0, 60, 5):
            prober.run("a-x", target, "http", "/", None, float(second), 60.0)
        self.assertEqual(len(calls), 1, "at most once a minute")
        now = 60.0
        for _ in range(12):
            state = prober.run("a-x", target, "http", "/", None, now, 60.0)
            now = state["next_at"]
        self.assertEqual(state["next_at"] - state["at"], ys.PROBE_BACKOFF_SECONDS, "every 5 minutes after 10 failures")
        unchecked = prober.run("a-y", None, "http", "/", None, 0.0, 60.0)
        self.assertTrue(unchecked["unchecked"])


class BeszelTests(AreaCase):
    """T25: the hub check, the agent check, the records check; the 21-hour replay; Working again; no login."""

    def test_the_21_hour_replay(self):
        events = []
        notifier = unittest.mock.Mock()
        notifier.app_event.side_effect = lambda kind, key, subject=None: events.append((kind, key, subject))
        answers = {"status": 200}
        area = self.area(notifier=notifier, prober=ys.Prober(
            http=lambda h, p, path, host: {"status": answers["status"]}, tcp=lambda h, p: {"status": 0}))
        area.beszel_check(time.time())
        area.findings(time.time())
        self.assertEqual(area.beszel_view()["code"], "no_login")
        now = time.time()
        stop(self.rig, "beszel-hub", 137, True, now - 60)
        stop(self.rig, "beszel-agent", 137, True, now - 60)
        answers["status"] = None
        area.numbers(force=True)
        area.refresh_map()
        area._beszel["next_at"] = 0
        area.beszel_check(now)
        self.assertEqual(area.beszel_view()["tone"], "err", "red at the next agent read")
        area.findings(now)
        area._beszel["next_at"] = 0
        area.beszel_check(now + 300)
        area.findings(now + 300)
        self.assertEqual([e for e in events if e[1] == "beszel"], [("service.down", "beszel", "beszel")],
                         "one incident, with the product's name")
        answers["status"] = 200
        self.rig.container("beszel-agent", "beszel-agent", "henrygd/beszel-agent:0.21.1", pid=4611,
                           network_mode="host")
        area.numbers(force=True)
        area.refresh_map()
        area._beszel["next_at"] = 0
        area.beszel_check(now + 600)
        area.findings(now + 600)
        self.assertIn(("service.up", "beszel", "beszel"), events, "Working again")

    def test_records_with_the_read_only_login(self):
        from swfactory import plainhttp

        state = {"updated": time.time() - 60, "token": "t"}

        def request(method, url, headers=None, body=None, timeout=5.0, **_):
            if url.endswith("auth-with-password"):
                return plainhttp.Reply(200 if body["password"] == "pw" else 400, {},
                                       json.dumps({"token": state["token"]}).encode())
            stamp = time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(state["updated"]))
            return plainhttp.Reply(200, {}, json.dumps({"items": [{"id": "abc", "status": "up",
                                                                   "updated": stamp}]}).encode())
        area = self.area({"yourserver": {"beszel": {"login_email": "seraphina@localhost",
                                                    "login_secret": "SERAPHINA_BESZEL_PASSWORD"}}},
                         beszel_request=request, environ={"SERAPHINA_BESZEL_PASSWORD": "pw"})
        area.beszel_check(time.time())
        self.assertEqual(area.beszel_view()["code"], "ok")
        state["updated"] = time.time() - 7200
        area._beszel["next_at"] = 0
        area.beszel_check(time.time())
        self.assertEqual(area.beszel_view()["code"], "not_recording")
        area.environ = {"SERAPHINA_BESZEL_PASSWORD": "wrong"}
        area._beszel["next_at"] = 0
        area.beszel_check(time.time())
        self.assertEqual(area.beszel_view()["login_error"], "Beszel did not accept the password.")
        self.assertNotIn("wrong", json.dumps(area.view()))


class BeszelLoginTests(AreaCase):
    """§4.4: the page makes the read-only login's password, keeps it in the env file, shows it once; Check reads."""

    def test_password_and_check(self):
        from swfactory import plainhttp

        kept = {}

        def request(method, url, headers=None, body=None, timeout=5.0, **_):
            if url.endswith("auth-with-password"):
                ok = body["password"] == kept.get(ys.BESZEL_SECRET)
                return plainhttp.Reply(200 if ok else 400, {}, json.dumps({"token": "t"}).encode())
            stamp = time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(time.time() - 30))
            return plainhttp.Reply(200, {}, json.dumps({"items": [{"id": "abc", "status": "up",
                                                                   "updated": stamp}]}).encode())
        area = self.area(beszel_request=request, environ={})
        area.set_secret = lambda name, value: kept.__setitem__(name, value)
        made = area.beszel_login({"action": "password"})
        self.assertEqual(len(made["password"]), 32)
        self.assertEqual(kept, {ys.BESZEL_SECRET: made["password"]}, "kept in the secret store")
        self.assertEqual(area.settings()["beszel"]["login_secret"], ys.BESZEL_SECRET)
        checked = area.beszel_login({"action": "check", "email": "seraphina@localhost"})
        self.assertEqual(checked["words"], "Seraphina can read Beszel: 1 system, recording.")
        self.assertNotIn(made["password"], json.dumps(area.view()), "never in a view")
        with self.assertRaises(ValueError):
            area.beszel_login({"action": "check", "email": "not an email"})


class PrivacyTests(AreaCase):
    """T26: <data_dir>/yourserver holds no time series, log line, mount path, address outside apps.json or secret;
    0700 and 0600; masked. T27: never in the request path."""

    def test_the_folder(self):
        area = self.area()
        confirm_all(area)
        area.probe_all(time.time())
        area.findings(time.time())
        area.stage(user="seraphina")
        folder = area.folder()
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        for path in folder.rglob("*"):
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode, 0o700 if path.is_dir() else 0o600, path)
            if path.is_dir() or path.parent.name == "setup":
                continue
            text = path.read_text()
            for needle in ("/srv/", "/opt/stacks", "/var/lib/docker", "PLANTED", "tskey", "ram", "cpu_percent"):
                self.assertNotIn(needle, text, f"{path.name}: {needle}")
            if path.name != "apps.json":
                self.assertNotIn("example.ts.net", text, f"{path.name} holds an address")
                self.assertNotIn("192.0.2.", text)
        masks = sandbox_masks(area.config)["dirs"]
        self.assertIn(Path(area.config.data_dir) / "yourserver", masks)

    def test_never_in_the_request_path(self):
        def digest(root: Path) -> dict:
            return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()}
        stacks = self.rig.at("/opt/stacks")
        before = digest(stacks)
        binds = []
        real_bind = socket.socket.bind

        def bind(sock, address):
            binds.append(address)
            return real_bind(sock, address)
        with unittest.mock.patch.object(socket.socket, "bind", bind), \
                unittest.mock.patch.object(socket.socket, "listen", side_effect=AssertionError("listen")):
            area = self.area()
            confirm_all(area)
            area.tick()
            area.probe_all(time.time())
            area.beszel_check(time.time())
        self.assertEqual(binds, [], "no socket of its own (N1, N9)")
        self.assertEqual(digest(stacks), before, "nothing written in a stacks folder, a serve config or a .env (N2)")


class NoticeTests(unittest.TestCase):
    """T28: phone texts from typed fields; an app's name only when its phone_name is on."""

    def test_subject_only_when_allowed(self):
        rig = Rig()
        self.addCleanup(rig.cleanup)
        events = []
        notifier = unittest.mock.Mock()
        notifier.app_event.side_effect = lambda kind, key, subject=None: events.append((kind, key, subject))
        stop(rig, "ollama", 1, False, time.time() - 600)
        area = make(rig, notifier=notifier)
        area.refresh_map()
        confirm_all(area)
        for _ in range(3):
            area.findings(time.time())
        self.assertIn(("service.down", "a-ai", None), events, "no name by default")
        area.app_update("a-photos", {"phone_name": True})
        stop(rig, "photos", 1, False, time.time() - 600)
        area.numbers(force=True)
        area.refresh_map()
        for _ in range(3):
            area.findings(time.time())
        self.assertIn(("service.down", "a-photos", "photos"), events)

    def test_the_notifier_texts(self):
        from tests.test_notify import NotifyCase

        case = NotifyCase("run")
        case.setUp()
        try:
            notifier = case.notifier
            first = notifier.app_event("service.down", "a-photos")
            entry = notifier.message(first["message"])["message"]
            self.assertEqual((entry["title"], entry["body"]), ("A service is down",
                                                              "A service is down. See the cockpit for what to do."))
            self.assertTrue(notifier.app_event("service.down", "a-photos").get("deduped"))
            other = notifier.app_event("service.down", "a-media", "media")
            self.assertEqual(notifier.message(other["message"])["message"]["body"],
                             "The media service is down. See the cockpit for what to do.")
            self.assertTrue(notifier.app_event("service.up", "a-photos")["message"])
            with self.assertRaises(Exception):
                notifier.app_event("system.failed", "a-photos")
            payload = json.dumps(notifier.message(first["message"]))
            self.assertNotIn("a-photos", payload.split('"fields"')[0])
        finally:
            case.doCleanups()


class IncidentTests(unittest.TestCase):
    """A down incident closes from what is kept, not from this process's memory: after a restart the first green
    sends "Working again"; Keep it off, hiding and removal close it without a notice; the next outage is
    announced again (a real Notifier and a real system ledger)."""

    def setUp(self) -> None:
        from swfactory.events import EventBus
        from swfactory.housekeeping import HousekeepingManager
        from tests.test_notify import NotifyCase

        self.rig = Rig()
        self.addCleanup(self.rig.cleanup)
        self.case = NotifyCase("run")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.data = tempfile.mkdtemp(prefix="ys-incidents-")
        self.system = HousekeepingManager(Path(self.data), EventBus())
        self.sent: list[tuple[str, str, bool]] = []

    def area(self, restart: bool = False, **kw) -> ys.YourServer:
        """The area on the same data dir; restart: a new Notifier on the notifier's own folder too (a restart)."""
        notifier = self.case.restart() if restart else self.case.notifier
        real = getattr(notifier.app_event, "real", notifier.app_event)

        def app_event(kind, key, subject=None):
            answer = real(kind, key, subject)
            self.sent.append((kind, key, bool(answer.get("message"))))
            return answer
        app_event.real = real
        notifier.app_event = app_event
        area = make(self.rig, {"data_dir": self.data}, notifier=notifier, system=self.system, **kw)
        area.refresh_map()
        return area

    def item(self, app_id: str) -> dict | None:
        found = [i for i in self.system.store().ledger().items
                 if (i.get("source") or {}).get("hash") == f"yourserver:down:{app_id}"]
        return found[-1] if found else None

    def down(self, area) -> None:
        for _ in range(3):
            area.findings(time.time())

    def test_after_a_restart_the_first_green_closes_it(self):
        start = keep_running(self.rig, "ollama")
        stop(self.rig, "ollama", 1, False, time.time() - 600)
        area = self.area()
        confirm_all(area)
        self.down(area)
        self.assertIn(("service.down", "a-ai", True), self.sent)
        self.assertEqual(self.case.notifier.app_incidents(), ["a-ai"])
        self.assertNotIn(self.item("a-ai")["status"], ("verified", "dropped"))
        start()
        fresh = self.area(restart=True)      # the harness restarted; the app works again
        fresh.findings(time.time())
        self.assertIn(("service.up", "a-ai", True), self.sent, "Working again, from the kept incident")
        self.assertEqual(self.item("a-ai")["status"], "verified")
        self.assertEqual(self.case.notifier.app_incidents(), [])
        stop(self.rig, "ollama", 1, False, time.time() - 600)
        fresh.numbers(force=True)
        fresh.refresh_map()
        self.down(fresh)
        self.assertEqual([s for s in self.sent if s[0] == "service.down"],
                         [("service.down", "a-ai", True)] * 2, "the next outage is announced, never deduped")

    def test_keep_it_off_hiding_and_removal_close_it_without_a_notice(self):
        stop(self.rig, "ollama", 1, False, time.time() - 600)
        stop(self.rig, "notes", 1, False, time.time() - 600)
        stop(self.rig, "filebrowser", 1, False, time.time() - 600)
        area = self.area()
        confirm_all(area)
        self.down(area)
        self.assertEqual(self.case.notifier.app_incidents(), ["a-ai", "a-filebrowser", "a-notes"])
        area.app_update("a-ai", {"expect": "off"})
        area.app_update("a-notes", {"hidden": True})
        os.rename(self.rig.at(f"/var/lib/docker/containers/{IDS['filebrowser']}"),
                  self.rig.at("/var/lib/docker/gone-filebrowser"))
        area.refresh_map()
        area.findings(time.time())
        self.assertEqual(self.case.notifier.app_incidents(), [])
        for app_id in ("a-ai", "a-notes", "a-filebrowser"):
            self.assertEqual(self.item(app_id)["status"], "verified", app_id)
        self.assertFalse([s for s in self.sent if s[0] == "service.up"], "no Working again for an app kept off")
        self.assertEqual(self.item("a-filebrowser")["note"], "it is no longer on the server")
        self.assertEqual(self.item("a-notes")["note"], "you hid it in Your server")

    def test_a_service_that_stopped_listening_is_still_down(self):
        """A service is an app only while it listens (rule 5): when it fails it leaves the page at the next map, a
        restart whose ports read fails sees no service at all, yet its incident stays open until it works again or
        its unit is gone from the server."""
        listening, ports = [True], [True]
        states = {"jellyfin.service": "LoadState=loaded\nActiveState=active\nSubState=running\n"}

        def runner(argv, data, timeout):
            if json.loads(data)["verb"] == "ports" and not ports[0]:
                return None, b"", b"(timed out)"
            code, out, err = self.rig.runner(argv, data, timeout)
            if json.loads(data)["verb"] == "ports" and listening[0]:
                answer = json.loads(out.decode().splitlines()[-1])
                answer["listeners"].append({"proto": "tcp", "address": "0.0.0.0", "port": 8096, "unit": "jellyfin.service",
                                            "process": "jellyfin", "pid": 1600,
                                            "processes": [{"process": "jellyfin", "pid": 1600}]})
                out = (json.dumps(answer) + "\n").encode()
            return code, out, err

        def area(restart=False):
            return self.area(restart, client=HelperClient(sx.HELPER, runner=runner, name="x"),
                             systemctl=lambda argv: states.get(argv[-1], "LoadState=loaded\nActiveState=active\n"))

        def fail(area) -> None:
            states["jellyfin.service"] = "LoadState=loaded\nActiveState=failed\nSubState=failed\n"
            area._units.clear()
            self.down(area)
            listening[0] = False             # a failed service no longer listens
            area.refresh_map()
            area.findings(time.time())
        first = area()
        confirm_all(first)
        fail(first)
        self.assertNotIn("a-jellyfin", {c["id"] for c in first.cards()["apps"]}, "gone from the page")
        self.assertEqual(self.case.notifier.app_incidents(), ["a-jellyfin"], "but still down")
        self.assertEqual(self.item("a-jellyfin")["status"], "open")
        ports[0] = False
        fresh = area(restart=True)
        self.assertIn("ports", fresh.errors)
        fresh.findings(time.time())
        self.assertEqual(self.case.notifier.app_incidents(), ["a-jellyfin"], "a failed ports read after a restart")
        self.assertEqual(self.item("a-jellyfin")["status"], "open")
        ports[0] = listening[0] = True
        states["jellyfin.service"] = "LoadState=loaded\nActiveState=active\nSubState=running\n"
        fresh.refresh_map()
        fresh.findings(time.time())
        self.assertIn(("service.up", "a-jellyfin", True), self.sent, "it listens again: Working again")
        self.assertEqual(self.item("a-jellyfin")["status"], "verified")
        fail(fresh)
        self.assertEqual([s for s in self.sent if s[0] == "service.down"], [("service.down", "a-jellyfin", True)] * 2)
        states["jellyfin.service"] = "LoadState=not-found\nActiveState=inactive\nSubState=dead\n"
        fresh._units.clear()
        fresh.findings(time.time())
        self.assertEqual(self.case.notifier.app_incidents(), [], "its unit is gone from the server")
        self.assertEqual(self.item("a-jellyfin")["note"], "its service is no longer on the server")
        self.assertEqual([s for s in self.sent if s[0] == "service.up"], [("service.up", "a-jellyfin", True)])

    def test_an_app_shown_as_an_old_copy_or_unreadable_is_still_down(self):
        """A lone app that stops while another runs its image shows as that one's old copy, and a container Docker's
        answer cannot read shows nowhere: neither is gone. The owner marking the copy, and the container's removal,
        close them."""
        area = self.area()
        confirm_all(area)
        for app_id in ("a-mediaold", "a-filebrowser"):     # opened before a restart
            self.case.notifier.app_event("service.down", app_id)
            area._item(app_id, open_it=True, code="stopped")
        stop(self.rig, "mediaold-web", 1, False, time.time() - 600)
        broken = self.rig.at(f"/var/lib/docker/containers/{IDS['filebrowser']}/config.v2.json")
        broken.write_text(json.dumps({"ID": IDS["filebrowser"], "Shape": "new"}))
        area = self.area()
        cards = {c["id"]: c for c in area.cards()["apps"]}
        self.assertNotIn("a-mediaold", cards)
        self.assertNotIn("a-filebrowser", cards)
        copy = next(o for o in cards["a-media"]["old_copies"] if o["name"] == "mediaold-web-1")
        self.assertEqual(copy["state"], "proposed")
        area.findings(time.time())
        self.assertEqual(self.case.notifier.app_incidents(), ["a-filebrowser", "a-mediaold"], "not on the page")
        self.assertEqual((self.item("a-mediaold")["status"], self.item("a-filebrowser")["status"]), ("open", "open"))
        area.groups({"action": "retire", "app": "a-media", "member": copy["key"]})
        os.rename(self.rig.at(f"/var/lib/docker/containers/{IDS['filebrowser']}"),
                  self.rig.at("/var/lib/docker/gone-filebrowser"))
        area.refresh_map()
        area.findings(time.time())
        self.assertEqual(self.case.notifier.app_incidents(), [])
        self.assertEqual(self.item("a-mediaold")["note"], "you marked it as an old copy of another app")
        self.assertEqual(self.item("a-filebrowser")["note"], "it is no longer on the server")
        self.assertFalse([s for s in self.sent if s[0] == "service.up"])

    def old_part(self, area, cid: str = "0" * 16) -> None:
        """A part of a-mediaold removed from its compose file long ago: the record entry _remember leaves."""
        old = {"app": "a-mediaold", "seen": "2026-01-01T00:00:00Z", "last": "2026-01-02T00:00:00Z",
               "removed_at": "2026-01-02T00:00:00Z", "cid": cid}
        area.record.change("observed", None, lambda r: r["members"].__setitem__("f" * 16, old))
        self.case.notifier.app_event("service.down", "a-mediaold")
        area._item("a-mediaold", open_it=True, code="stopped")
        stop(self.rig, "mediaold-web", 1, False, time.time() - 600)

    def retire(self, area) -> None:
        cards = {c["id"]: c for c in area.cards()["apps"]}
        copy = next(o for o in cards["a-media"]["old_copies"] if o["name"] == "mediaold-web-1")
        area.findings(time.time())
        self.assertEqual(self.item("a-mediaold")["status"], "open", "proposed, not marked: still down")
        area.groups({"action": "retire", "app": "a-media", "member": copy["key"]})
        area.findings(time.time())

    def test_an_old_copy_with_a_part_removed_long_ago_closes_when_marked(self):
        """The record keeps a removed part's entry: an app with one part removed long ago, whose last part the owner
        retires as another app's old copy, is settled (each part marked or removed) and closes without a notice."""
        area = self.area()
        confirm_all(area)
        self.old_part(area)
        area = self.area()
        self.retire(area)
        self.assertEqual(self.case.notifier.app_incidents(), [])
        self.assertEqual((self.item("a-mediaold")["status"], self.item("a-mediaold")["note"]),
                         ("verified", "you marked it as an old copy of another app"))
        self.assertFalse([s for s in self.sent if s[0] == "service.up"])

    def test_an_old_copy_with_a_part_that_may_be_unreadable_stays_down(self):
        """The removed part may be a container Docker's answer cannot read: one part marked is not settled."""
        area = self.area()
        confirm_all(area)
        self.old_part(area, cid=ys._sha16("container", IDS["filebrowser"]))
        broken = self.rig.at(f"/var/lib/docker/containers/{IDS['filebrowser']}/config.v2.json")
        broken.write_text(json.dumps({"ID": IDS["filebrowser"], "Shape": "new"}))
        area = self.area()
        self.retire(area)
        self.assertIn("a-mediaold", self.case.notifier.app_incidents())
        self.assertEqual(self.item("a-mediaold")["status"], "open")


class MapRetryTests(unittest.TestCase):
    """A helper that refuses, is not set up or does not answer is asked again after map_minutes, never at every
    5-second tick (helpers.py: "never in a loop")."""

    def check(self, answer) -> None:
        rig = Rig()
        self.addCleanup(rig.cleanup)
        calls = []

        def runner(argv, data, timeout):
            calls.append(json.loads(data)["verb"])
            return answer
        now = [time.time()]
        area = make(rig, client=HelperClient(sx.HELPER, runner=runner, name="Seraphina's system access",
                                             status_path="/nonexistent"), clock=lambda: now[0])
        for _ in range(12):
            area.tick()
            now[0] += 5
        self.assertEqual(calls, ["info"], "one try in a minute of ticks")
        self.assertIn(area.helper_state()["state"], ("unreachable", "not_set_up"))
        now[0] += area.settings()["refresh"]["map_minutes"] * 60
        area.tick()
        self.assertEqual(calls, ["info", "info"], "asked again after map_minutes")

    def test_sudo_refused(self):
        self.check((1, b"", b"sudo: a password is required\n"))

    def test_not_set_up(self):
        self.check((0, json.dumps({"ok": True, "set_up": False}).encode() + b"\n", b""))

    def test_no_answer(self):
        self.check((None, b"", b"(timed out)"))


class RouteTests(unittest.TestCase):
    """T29: every route needs the master token; none is an agent's; no persona or agent names a route."""

    def test_routes(self):
        from swfactory import server

        mine = [(m, p.pattern, h) for m, p, h in server._ROUTES if "/api/yourserver" in p.pattern]
        self.assertGreaterEqual(len(mine), 13)
        self.assertFalse(any(h in server.AGENT_TOKEN_ROUTES for _, _, h in mine))
        self.assertTrue(server._PARAM_RULES["app"][0].fullmatch("a-photos"))
        self.assertFalse(server._PARAM_RULES["app"][0].fullmatch("../x"))
        for folder in ("agents", "personas"):
            for path in (ROOT / folder).rglob("*"):
                if path.is_file():
                    self.assertNotIn("/api/yourserver", path.read_text(errors="replace"), path)


class WordTests(unittest.TestCase):
    """U8: no novice template holds an internal word; the page says it only looks."""

    def test_module_words(self):
        for text in ys.novice_strings():
            with self.subTest(text=text[:60]):
                self.assertEqual(ys.internal_terms(text), [])

    def test_view_words(self):
        js = VIEW_JS.read_text(encoding="utf-8")
        block = js[js.index("const WORDS = {"):js.index("\n  };\n")]
        strings = re.findall(r"'((?:[^'\\]|\\.)*)'", block)
        self.assertGreater(len(strings), 100)
        for text in strings:
            with self.subTest(text=text[:60]):
                self.assertEqual(ys.internal_terms(text), [])
        self.assertIn("This area only looks.", block)

    def test_the_terms_are_found(self):
        self.assertEqual(ys.internal_terms("the sidecar's cgroup and a probe"), ["sidecar", "cgroup", "probe"])


class SettingsTests(unittest.TestCase):
    def test_validation(self):
        good = {"yourserver": {"enabled": True, "tailnet": {"suffix": "example.ts.net"},
                               "links": {"dockge": "http://192.0.2.10:5001", "cockpit": "https://cockpit.example.ts.net"},
                               "beszel": {"login_email": "seraphina@localhost", "login_secret": "SERAPHINA_BESZEL"}},
                "services": {"a-photos": {"maintenance": {"mode": "pinned"}, "phone_name": True,
                                          "expect": {"state": "run", "grace_minutes": 20}},
                             "os-packages": {"maintenance": {"mode": "inherit"}}}}
        settings_mod._check_yourserver(good["yourserver"])
        settings_mod._check_services(good["services"])
        for bad in ({"links": {"dockge": "javascript:alert(1)"}}, {"links": {"cockpit": "https://x.other.ts.net"},
                                                                    "tailnet": {"suffix": "example.ts.net"}},
                    {"tailnet": {"suffix": "example.com"}}, {"updates": {"registry_checks": True}},
                    {"refresh": {"probe_seconds": 1}}, {"nope": 1}, {"beszel": {"login_secret": "lower case"}}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                settings_mod._check_yourserver(bad)
        for bad in ({"photos": {}}, {"a-photos": {"maintenance": {"mode": "sometimes"}}},
                    {"a-photos": {"expect": {"state": "maybe"}}}, {"a-photos": {"probe": {"kind": "ping"}}}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                settings_mod._check_services(bad)
        self.assertIn("yourserver", settings_mod.UPDATABLE_SECTIONS)
        self.assertIn("services", settings_mod.UPDATABLE_SECTIONS)

    def test_off_by_default(self):
        from swfactory.config import DEFAULTS

        self.assertIs(DEFAULTS["yourserver"]["enabled"], False)
        self.assertIs(DEFAULTS["yourserver"]["updates"]["registry_checks"], False, "D4")


class DoctorTests(AreaCase):
    """§11.7's P1a lines (T30 is P1b's; the lines that need only info, ports and ingress are here)."""

    def checks(self, raw=None, identity=("seraphina", ["seraphina"]), **kw):
        area = make(self.rig, raw)
        return {c["name"]: c for c in ys.doctor_checks(area.config, client=area.client(), identity=identity,
                                                       check=None, **kw)}

    def test_lines(self):
        found = self.checks()
        self.assertTrue(found["your server helper"]["ok"] or found["your server helper"]["level"] == "warn")
        self.assertEqual(found["your server sandbox"]["level"], "warn", "read only while the sandbox is off")
        self.assertEqual(found["your server lab checks"]["level"], "warn")
        self.assertEqual(found["your server beszel"]["level"], "info")
        found = self.checks(identity=("owner", ["docker", "lxd", "owner"]))
        self.assertEqual(found["your server account"]["level"], "error")
        self.assertIn("control Docker", found["your server account"]["detail"])
        found = self.checks(identity=("owner", ["sudo"]))
        self.assertIn("already controls the server", found["your server account"]["detail"])

    def test_shared_address_and_gateway_forward(self):
        found = self.checks({"server": {"host": "192.0.2.10", "port": 8765}})
        self.assertEqual(found["your server address"]["level"], "error", "other web apps listen on 192.0.2.10")
        found = self.checks(ingress=[{"routes": [{"upstream": [{"host": "127.0.0.1", "port": 8765}]}]}])
        self.assertEqual(found["your server gateway"]["level"], "warn")

    def test_no_new_privs_is_named(self):
        status = self.rig.write("/proc/self/status", "Name: x\nNoNewPrivs:\t1\n")

        def refused(argv, data, timeout):
            return 1, b"", b"sudo: The \"no new privileges\" flag is set\n"
        area = make(self.rig)
        found = {c["name"]: c for c in ys.doctor_checks(area.config, client=HelperClient(sx.HELPER, runner=refused,
                                                                                         status_path=str(status)),
                                                        identity=("seraphina", []), status_path=str(status))}
        self.assertEqual(found["your server helper"]["level"], "error")
        self.assertIn("NoNewPrivs", found["your server helper"]["detail"])

    def test_off(self):
        area = make(self.rig, {"yourserver": {"enabled": False}})
        found = ys.doctor_checks(area.config, identity=("seraphina", []))
        self.assertEqual([c["name"] for c in found], ["your server"])


class ClientTests(unittest.TestCase):
    def test_both_sudo_programs_refusals(self):
        for words in (b"sudo: a password is required\n", b"sudo: interactive authentication is required\n",
                      b"sudo-rs: interactive authentication is required\n"):
            client = HelperClient("/x", runner=lambda argv, data, t, w=words: (1, b"", w), status_path="/nonexistent")
            with self.assertRaises(HelperError) as caught:
                client.call("info")
            self.assertEqual(caught.exception.code, "sudo_refused")

    def test_unknown_outcome_for_a_changing_request(self):
        client = HelperClient("/x", runner=lambda argv, data, t: (None, b"", b"(timed out)"))
        with self.assertRaises(HelperError) as caught:
            client.call("info", changing=True)
        self.assertEqual(caught.exception.code, "unknown")

    def test_a_lost_answer_to_a_changing_request_is_unknown(self):
        """A helper that ended without an answer after the request was written (killed, or its own failure) may
        have done it: unknown for a changing request, never "unreadable" (which is asked again); sudo's own
        refusal stays a refusal; a read stays unreadable."""
        for code, err in ((1, b"Traceback (most recent call last):\n"), (-9, b"")):
            client = HelperClient("/x", runner=lambda argv, data, t, c=code, e=err: (c, b"", e),
                                  status_path="/nonexistent")
            with self.assertRaises(HelperError) as caught:
                client.call("keep.images", changing=True, rid="a" * 32)
            self.assertEqual(caught.exception.code, "unknown")
            self.assertIn("may or may not have done it", caught.exception.words)
            with self.assertRaises(HelperError) as caught:
                client.call("info")
            self.assertEqual(caught.exception.code, "unreadable")
        client = HelperClient("/x", runner=lambda argv, data, t: (1, b"", b"sudo: a password is required\n"),
                              status_path="/nonexistent")
        with self.assertRaises(HelperError) as caught:
            client.call("keep.images", changing=True, rid="a" * 32)
        self.assertEqual(caught.exception.code, "sudo_refused", "it never reached the helper")

    def test_the_sudoers_texts_agree(self):
        for implementation in ("sudo", "sudo-rs"):
            self.assertEqual(ys.sudoers_text("seraphina", implementation), sx.sudoers_text("seraphina", implementation))


class AskTests(AreaCase):
    def test_facts_only_and_a_local_model(self):
        seen = {}
        area = self.area({"models": {"local": {"provider": "local", "model": "m"}},
                          "providers": {"local": {"type": "openai", "base_url": "http://127.0.0.1:11434/v1"}}},
                         asker=lambda model, system, prompt: seen.update(prompt=prompt, model=model.key) or "It runs.")
        confirm_all(area)
        answer = area.ask("a-photos", "Is it healthy?")
        self.assertEqual((answer["answer"], answer["label"]), ("It runs.", "AI, from the facts above"))
        for needle in ("example.ts.net", "/srv/", "PLANTED", "172.19."):
            self.assertNotIn(needle, seen["prompt"], "facts only: no address, path, secret or log")
        cloud = self.area({"yourserver": {"ask_model": "cloud"}, "models": {"cloud": {"provider": "cloud", "model": "m"}},
                           "providers": {"cloud": {"type": "openai", "base_url": "https://api.example.com/v1"}}})
        self.assertIsNone(cloud.ask("a-photos", "Why?")["answer"], "never a cloud model (D19)")


if __name__ == "__main__":
    unittest.main()
