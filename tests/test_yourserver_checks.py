"""Tests for Your server's phases P1b and P1c on the harness side (DESIGN.md §42; docs/seraphina/YOUR_SERVER.md §2.6,
§6.8, §6.10, §7.2, §8, §10, §11.7, §12.2): T13 the update kinds, T14's harness part (an unknown outcome is asked about
and never replayed), T18's harness parsers (apt-get -s, the families and the dependency step, systemctl's JSON), T20's
keep pass seen from the cards (the previous version kept, How to go back), Docker's own health on the lights, the
Check buttons' results, Check 8's logs and the root page under their preconditions (T23, U7's data), T26's new files,
T29's new routes and T30's new doctor lines. The harness reads the placeholder server of tests/sfsystem_rig.py through
the real bin/sf-system; every name, address and line is made up.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

from swfactory import rootaccess, serverchecks as sc, yourserver as ys
from swfactory.helpers import HelperClient, HelperError
from tests.sfsystem_rig import FIXTURES, IDS, KATE, SERAPHINA, Rig, sx
from tests.test_yourserver import confirm_all, make

ALL = ["keep", "private", "read", "refresh"]
SERAPHINA_ID = {"uid": SERAPHINA, "user": "seraphina", "groups": ["seraphina"], "shell": "/usr/sbin/nologin",
                "uid_min": 1000}
OWNER_ID = {"uid": 1000, "user": "owner", "groups": ["owner"], "shell": "/bin/bash", "uid_min": 1000}


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = Rig()
        self.addCleanup(self.rig.cleanup)
        self.rig.config(allow=ALL, callers={str(SERAPHINA): ALL})
        self.items: list[tuple[str, bool]] = []

    def area(self, raw=None, identity=SERAPHINA_ID, **kw) -> ys.YourServer:
        base = {"harness": {"sandbox": "bwrap"}}
        base.update(raw or {})
        area = make(self.rig, base, run=self.rig.harness_run, background=False, identity=identity, **kw)
        area._system_item = lambda dedupe, title, detail, open_it, app=None, verb=None: self.items.append((dedupe,
                                                                                                        open_it))
        area.refresh_map()
        return area

    def cards(self, area) -> dict:
        return {c["id"]: c for c in area.cards()["apps"]}


class AptParserTests(unittest.TestCase):
    """T18 (harness part): apt-get -s's Inst lines and summary, the security set, the families and the dependency
    step, so Check 1's count is one the Install button (P2) could install."""

    def test_inst_lines_and_summary(self):
        parsed = sc.parse_apt_simulation((FIXTURES / "checks" / "apt-s-upgrade.txt").read_text())
        self.assertEqual(parsed["summary"], {"upgraded": 10, "new": 0, "remove": 0, "held": 4})
        self.assertEqual(len(parsed["packages"]), 10)
        curl = parsed["packages"][0]
        self.assertEqual((curl["package"], curl["old"], curl["new"], curl["security"]),
                         ("curl", "8.5.0-2ubuntu10.4", "8.5.0-2ubuntu10.5", True))
        self.assertEqual({p["package"] for p in parsed["packages"] if p["security"]},
                         {"curl", "libcurl4t64", "openssh-client", "openssh-server"})
        self.assertEqual(parsed["deferred"], ["example-phased"])
        self.assertEqual(parsed["kept_back"], ["linux-generic", "linux-headers-generic", "linux-image-generic"])
        dist = sc.parse_apt_simulation((FIXTURES / "checks" / "apt-s-dist-upgrade.txt").read_text())
        new = next(p for p in dist["packages"] if p["package"].startswith("linux-image-6"))
        self.assertIsNone(new["old"], "a new package has no old version")
        self.assertFalse(sc.parse_apt_simulation("garbage")["parsed"])

    def test_the_families_and_the_dependency_step(self):
        rig = Rig()
        self.addCleanup(rig.cleanup)
        upgrade = sc.parse_apt_simulation((FIXTURES / "checks" / "apt-s-upgrade.txt").read_text())

        def show(package, version):
            return rig.harness_run(["apt-cache", "show", f"{package}={version}"])[1]

        def simulate(pairs):
            return sc.parse_apt_simulation(rig.harness_run(["apt-get", "-s", "install", "--only-upgrade", *pairs])[1])
        plan = sc.plan_updates(upgrade, show, simulate)
        self.assertEqual(sorted(plan["ready"]), ["curl", "example-tool", "libcurl4t64", "tzdata"])
        self.assertEqual(sorted(plan["left"]["access"]), ["libsystemd-shared", "libsystemd0", "openssh-client",
                                                          "openssh-server", "systemd"])
        self.assertEqual(plan["left"]["docker"], ["docker-ce"])
        self.assertTrue(plan["simulated"])
        rig.simulate_extra = ["libnew-dependency=1"]
        plan = sc.plan_updates(upgrade, show, simulate)
        self.assertNotIn("libnew-dependency", plan["ready"])
        self.assertLess(len(plan["ready"]), 4, "what the simulation pulls in moves packages out")
        self.assertEqual(sc.exact_relations((FIXTURES / "checks" / "apt-cache" / "systemd.txt").read_text()),
                         ["libsystemd-shared", "libsystemd0"])

    def test_copy_blocks_name_exact_versions_never_a_bare_upgrade(self):
        by_name = {p["package"]: p for p in sc.parse_apt_simulation(
            (FIXTURES / "checks" / "apt-s-upgrade.txt").read_text())["packages"]}
        blocks = {b["id"]: b for b in sc.update_blocks({"access": ["systemd", "libsystemd0"], "docker": ["docker-ce"]},
                                                       by_name)}
        self.assertEqual(blocks["access"]["commands"], ["sudo apt-get install --only-upgrade "
                                                        "libsystemd0=259.1-1ubuntu2 systemd=259.1-1ubuntu2"])
        self.assertTrue(blocks["docker"]["commands"][0].startswith("sudo apt-mark hold docker-ce"))
        for block in blocks.values():
            self.assertFalse(any(c.strip() in ("sudo apt upgrade", "sudo apt-get upgrade") for c in block["commands"]))


class CheckTests(Case):
    """Checks 1-7 (U6's data): each result's words and rows; they only look."""

    def test_updates(self):
        area = self.area()
        stamp = self.rig.write("/var/lib/apt/periodic/update-success-stamp", "")
        os.utime(stamp, (time.time() - 3 * 3600, time.time() - 3 * 3600))
        area.roots["apt_stamp"] = str(stamp)
        area.roots["auto_upgrades"] = str(self.rig.write("/etc/apt/apt.conf.d/20auto-upgrades",
                                                         'APT::Periodic::Update-Package-Lists "1";\n'
                                                         'APT::Periodic::Unattended-Upgrade "1";\n'))
        check = next(c for c in area.run_check("updates")["checks"] if c["id"] == "updates")
        result = check["result"]
        self.assertEqual(result["counts"]["updates"], 14)
        self.assertEqual(result["counts"]["security"], 6)
        self.assertEqual(result["counts"]["ready"], 4)
        self.assertIn("14 updates, 6 of them security fixes.", result["words"])
        self.assertIn("4 are ordinary updates", result["words"])
        self.assertIn("must be done at home", result["words"])
        self.assertIn("Ubuntu is still rolling 1 out.", result["words"])
        self.assertIn("List checked 3 hours ago.", result["words"])
        families = {b["id"] for b in result["blocks"]}
        self.assertEqual(families, {"access", "docker", "kernel"})
        self.assertTrue(all(sc.AUTO_UPDATES in b["note"] for b in result["blocks"]))
        tied = {r["key"]: r["sub"] for r in result["rows"] if r["sub"]}
        self.assertEqual(tied["libsystemd0"], "waits with remote access and systemd")
        for argv in self.rig.harness_calls:
            if argv[0] == "apt-get":
                self.assertEqual(argv[1], "-s", "only simulations: " + " ".join(argv))

    def test_restart_failed_and_health(self):
        area = self.area()
        self.rig.write("/run/reboot-required", "*** System restart required ***\n")
        self.rig.write("/run/reboot-required.pkgs", "linux-image-6.18.0-14-generic\n")
        area.roots["run"] = str(self.rig.at("/run"))
        result = next(c for c in area.run_check("restart")["checks"] if c["id"] == "restart")["result"]
        self.assertIn("a new kernel is installed but not running yet", result["words"])
        rows = {r["key"]: r["sub"] for r in result["rows"]}
        self.assertEqual(rows["dbus.service"], sc.WORDS["restart_never"])
        self.assertEqual(rows["ssh.service"], sc.WORDS["restart_never"], "the units that reach the server")
        self.assertEqual(rows["example-notes.service"], sc.WORDS["restart_service"])
        result = next(c for c in area.run_check("failed")["checks"] if c["id"] == "failed")["result"]
        self.assertEqual({r["key"] for r in result["rows"]}, {"example-backup.service", "example-report.service"})
        result = next(c for c in area.run_check("health")["checks"] if c["id"] == "health")["result"]
        self.assertEqual(result["tone"], "err")
        self.assertIn("1 of 4 disks need a look.", result["words"])
        self.assertIn("was asleep", result["words"])

    def test_disk_use_and_folders(self):
        area = self.area()
        result = next(c for c in area.run_check("disk")["checks"] if c["id"] == "disk")["result"]
        self.assertIn("leftover build files", result["words"])
        self.assertEqual(result["docker"]["cache"], 5_000_000_000, "not in use, not shared, not internal")
        self.assertEqual(result["docker"]["volumes"], 9_000_000_000)
        self.assertIn("System logs: 3.3 GB.", result["words"])
        mount = area.checks_view()["disks"][0]["mount"]
        result = next(c for c in area.run_check("disk", {"mount": mount})["checks"] if c["id"] == "disk")["result"]
        self.assertEqual(result["folders"]["rows"][0]["path"], "/srv/data1/media")
        with self.assertRaises(ValueError):
            area.run_check("disk", {"mount": "/etc"})
        doc = (area.folder() / "apps.json").read_text()
        self.assertNotIn("/srv/data1/media", doc, "folder names stay in the cockpit")

    def test_listening_and_tailscale(self):
        area = self.area()
        confirm_all(area)
        result = next(c for c in area.run_check("listening")["checks"] if c["id"] == "listening")["result"]
        self.assertIn("can be reached from your home network without Tailscale", result["words"])
        self.assertEqual(result["rows"][0]["key"], "gateway", "the gateway's ports are one row")
        self.assertTrue(any(r["tone"] in ("warn", "err") for r in result["rows"][1:]))
        result = next(c for c in area.run_check("tailscale")["checks"] if c["id"] == "tailscale")["result"]
        self.assertIn("Funnel is off everywhere.", result["words"])
        self.assertIn(sc.WORDS["tailscale_policy"], result["words"])

    def test_published_ports_are_joined_to_their_apps(self):
        """Check 6: docker-proxy holds a published port, so ss names docker.service; the port is joined to its app
        through PortBindings, and a port that skips the gateway's sign-in is named."""
        area = self.area()
        confirm_all(area)
        result = next(c for c in area.run_check("listening")["checks"] if c["id"] == "listening")["result"]
        self.assertFalse(any(r["words"] == "docker.service" for r in result["rows"]), result["rows"])
        self.assertNotIn("docker.service", result["words"])
        files = next(r for r in result["rows"] if r["app"] == "a-filebrowser")
        self.assertIn("8081/tcp", files["ports"])
        self.assertIn(sc.WORDS["listening_bypass"], files["sub"])
        notes = next(r for r in result["rows"] if r["app"] == "a-notes")
        self.assertIn("3000/tcp", notes["ports"])
        self.assertIn(files["words"], result["words"], "the summary names the app")

    def test_a_host_tailscaled(self):
        """Check 7 counts this server's own Tailscale and its Funnel, like a container's."""
        funnel = {"running": True, "node": "server", "tailnet": "example.ts.net",
                  "web": [{"host": "server.example.ts.net", "port": 443, "path": "/", "funnel": True}]}
        result = sc.tailscale_result({"sidecars": [], "host": funnel}, lambda cid: None)
        self.assertEqual((result["tone"], len(result["rows"])), ("err", 1))
        self.assertIn(sc.WORDS["tailscale_funnel_on"], result["words"])
        self.assertEqual(result["rows"][0]["words"], sc.WORDS["tailscale_host"])
        sidecar = {"name": "photos-ts-1", "container": "x", "running": True,
                   "web": [{"host": "photos.example.ts.net", "port": 443, "funnel": False}]}
        result = sc.tailscale_result({"sidecars": [sidecar], "host": funnel}, lambda cid: "photos")
        self.assertEqual((result["tone"], len(result["rows"])), ("err", 2))
        self.assertNotIn(sc.WORDS["tailscale_funnel_off"], result["words"])
        quiet = dict(funnel, web=[dict(funnel["web"][0], funnel=False)])
        result = sc.tailscale_result({"sidecars": [], "host": quiet}, lambda cid: None)
        self.assertEqual(result["tone"], "ok")
        self.assertIn("1 Tailscale connection, 1 running. Funnel is off everywhere.", result["words"])
        self.assertEqual(sc.tailscale_result({"sidecars": [], "host": {"running": False}}, lambda cid: None)["words"],
                         sc.WORDS["tailscale_none"])

    def test_disk_health_never_healthy_when_nothing_was_checked(self):
        for smart, words in (({"state": "no_tool", "disks": []}, sc.WORDS["health_no_tool"]),
                             ({"state": "ok", "disks": []}, sc.WORDS["health_none"]),
                             ({"state": "ok", "disks": [{"key": "a", "state": "asleep"}, {"key": "b"}]},
                              "Seraphina could not read any of the 2 disks.")):
            with self.subTest(smart=smart):
                result = sc.health_result(smart)
                self.assertEqual(result["tone"], "muted")
                self.assertTrue(result["words"].startswith(words), result["words"])
                self.assertNotIn("healthy", result["words"])
        result = sc.health_result({"state": "ok", "disks": [{"key": "a", "state": "ok"}]})
        self.assertEqual((result["tone"], result["words"]), ("ok", "All 1 disk healthy."))
        # some read, some not: only the ones read count as healthy, and never green
        for states, words in ((["ok", "unknown", "unknown"], "1 of 3 disks healthy. 2 could not be read."),
                              (["ok", "unreadable"], "1 of 2 disks healthy. 1 could not be read."),
                              (["ok", "asleep"], "1 of 2 disks healthy. Disk 2 was asleep."),
                              (["ok", "ok", "unknown", "asleep"],
                               "2 of 4 disks healthy. 1 could not be read. Disk 4 was asleep.")):
            with self.subTest(states=states):
                result = sc.health_result({"state": "ok", "disks": [{"key": str(n), "state": s}
                                                                     for n, s in enumerate(states)]})
                self.assertEqual((result["tone"], result["words"]), ("muted", words))
                self.assertFalse(result["words"].startswith("All"))
        result = sc.health_result({"state": "ok", "disks": [{"key": "a", "state": "failing"}, {"key": "b"},
                                                            {"key": "c", "state": "ok"}]})
        self.assertEqual((result["tone"], result["words"]), ("err", "1 of 3 disks need a look. 1 could not be read."))

    def test_without_the_helper(self):
        area = make(self.rig, {"yourserver": {"helper": "/nonexistent/sf-system"}}, client=None,
                    run=self.rig.harness_run, background=False, identity=SERAPHINA_ID)
        view = area.checks_view()
        health = next(c for c in view["checks"] if c["id"] == "health")
        self.assertEqual((health["needs_access"], health["words"]), (True, sc.NEEDS_ACCESS))
        result = next(c for c in area.run_check("restart")["checks"] if c["id"] == "restart")["result"]
        self.assertIn(sc.WORDS["restart_no_access"], result["words"])
        with self.assertRaises(KeyError):
            area.run_check("explode")


class RefreshTests(Case):
    """Check 1's Refresh the list: apt.refresh as a job; Check 1 again when it ends; refused when the class is off."""

    def test_refresh_then_check(self):
        area = self.area()
        view = area.run_check("updates", {"refresh": True})
        job = view["refresh"]["job"]["id"]
        self.rig.write("/proc/self/cgroup", f"0::/system.slice/{sx.unit_name('apt.refresh', job)}.service\n")
        sx.run_job(job, self.rig.paths, self.rig.system, self.rig.lstat, out=lambda _t: None)
        area._poll_jobs(force=True)
        self.assertTrue(area._jobs[job]["ended"])
        self.assertIn("updates", area.checks, "Check 1 ran again")
        page = area.job_view(job)
        self.assertEqual(page["job"]["outcome"], "ok")
        self.assertIn("Reading package lists", page["log"]["text"])

    def test_off(self):
        self.rig.config(allow=["read"], callers={str(SERAPHINA): ["read"]})
        area = self.area()
        with self.assertRaises(ys.ActionError) as caught:
            area.run_check("updates", {"refresh": True})
        self.assertEqual(str(caught.exception), ys.REFRESH_OFF)
        self.assertEqual(area.checks_view()["refresh"]["allowed"], False)


class UpdateKindTests(Case):
    """T13: a differing config hash is "waiting in its folder"; a tag naming another local image is "downloaded"; a
    digest pin never shows an update; upstream checks stay off (D4)."""

    def test_kinds(self):
        self.rig.compose_changed.add("/opt/stacks/photos")
        self.rig.moved_tags = {"jellyfin/jellyfin:10.9.11": "sha256:" + "e" * 64}
        area = self.area()
        confirm_all(area)
        cards = self.cards(area)
        self.assertEqual([k["kind"] for k in cards["a-photos"]["updates"]["kinds"]][:1], ["waiting"])
        self.assertEqual(cards["a-photos"]["updates"]["words"], ys.UPDATE_WORDS["waiting"])
        self.assertEqual(cards["a-media"]["updates"]["kinds"][0]["kind"], "downloaded")
        self.assertEqual(cards["a-beszel"]["updates"]["words"], ys.UPDATE_WORDS["none"])
        self.assertEqual(self.rig.system.docker_calls and all(m == "GET" for m, _ in self.rig.system.docker_calls),
                         True)
        version = ys.version_words({"image": "nginx@sha256:" + "a" * 64}, [])
        self.assertEqual(area._updates({"members": [{"container": {"image": "nginx@sha256:" + "a" * 64,
                                                                   "labels": {}, "image_id": "x"}}]}, version, {}),
                         {"kinds": [], "words": "Pinned to an exact version."})
        self.assertEqual(area._updates(next(a for a in area._built()["apps"] if area._built()["record"]["ids"]
                                            [a["key"]] == "a-photos"), {}, {"maintenance": {"mode": "pinned"}})
                         ["words"], ys.UPDATE_WORDS["pinned_app"])

    def test_docker_health_turns_a_working_card_amber(self):
        area = self.area()
        confirm_all(area)
        for _ in range(2):
            for probe in area.prober.state.values():
                probe["next_at"] = 0
            area.probe_all(time.time())
        cards = self.cards(area)
        self.assertEqual((cards["a-notes"]["light"]["code"], cards["a-notes"]["health"]), ("unhealthy", "unhealthy"))
        self.assertEqual(cards["a-photos"]["health"], "healthy")
        self.rig.docker_active = False
        area.refresh_map()
        self.assertEqual(area.facts["health"], {}, "never from the state files")


class KeepTests(Case):
    """T20's keep pass from the harness: names added, the previous version on the card, How to go back; receipts in
    <data_dir> as ids and counts only."""

    def setUp(self):
        super().setUp()
        self.rig.extra_images.append({"Id": "sha256:" + "c" * 64, "RepoTags": [], "RepoDigests": [
            "photoprism/photoprism@sha256:" + "d" * 64], "Created": 1759000000, "Size": 2_000_000_000, "Labels": {}})

    def test_keep_and_how_to_go_back(self):
        area = self.area()
        confirm_all(area)
        self.assertTrue(area._keep_due(time.time()))
        answer = area.keep_now()
        self.assertIn("Kept:", answer["words"])
        self.assertFalse(area._keep_due(time.time()))
        card = self.cards(area)["a-photos"]
        previous = next(line for line in card["backup"]["lines"] if line["key"] == "previous")
        self.assertTrue(previous["words"].startswith("Previous version: kept on the server (from 2025-09-27)"),
                        previous)
        self.assertTrue(card["backup"]["restore"].startswith("partly"))
        page = area.app_view("a-photos")
        back = page["how_back"]
        self.assertTrue(back["available"])
        steps = back["services"][0]
        self.assertEqual(steps["service"], "photos")
        self.assertEqual(steps["steps"][0], "In Dockge, open the stack photos and press Edit.")
        self.assertIn("image: photoprism/photoprism:241021", steps["steps"][1])
        self.assertRegex(steps["kept_line"], r"^image: seraphina-keep/photos-photos:\d{8}-c{12}$")
        self.assertEqual(back["data"], ys.BACK_WORDS["data"])
        beszel = area.app_view("a-beszel")
        self.assertEqual(next(line for line in self.cards(area)["a-beszel"]["backup"]["lines"]
                              if line["key"] == "previous")["words"], ys.BACK_WORDS["current_only"])
        self.assertFalse(beszel["how_back"]["available"])
        mirror = (area.folder() / "receipts.jsonl").read_text()
        entry = json.loads(mirror.split("\n")[0])
        self.assertEqual(set(entry), {"id", "verb", "class", "outcome", "count", "at"})
        self.assertNotIn("seraphina-keep", mirror, "no name in <data_dir>")
        self.assertEqual(stat.S_IMODE(os.stat(area.folder() / "receipts.jsonl").st_mode), 0o600)
        receipts = area.receipts_view()
        self.assertEqual(receipts["receipts"][0]["label"], ys.RECEIPT_LABELS["system"])
        self.assertEqual(area.receipt_view(receipts["receipts"][0]["id"])["receipt"]["class"], "undo_instantly")

    def refuse(self, part: str) -> None:
        """Docker refuses every POST /images/<id>/tag whose path holds part."""
        answer_of = self.rig.docker_answer
        self.rig.docker_answer = lambda method, path: (500, b'{"message": "refused"}') if method == "POST" and \
            part in path else answer_of(method, path)
        self.addCleanup(setattr, self.rig, "docker_answer", answer_of)
        return answer_of

    def test_a_pass_docker_refuses_never_reads_as_kept(self):
        area = self.area()
        confirm_all(area)
        answer_of = self.refuse("/tag?")
        answer = area.keep_now()
        self.assertEqual((answer["keep"]["outcome"], answer["words"]), ("failed", ys.KEEP_FAILED))
        self.assertIsNone(answer["keep"]["kept_at"], "nothing kept yet")
        self.assertIsNone(area.view()["keep"]["kept_at"])
        row = area.receipts_view()["receipts"][0]
        self.assertEqual((row["outcome"], row["outcome_words"]), ("failed", "Failed"))
        self.assertFalse(row["words"].startswith("Kept"), row["words"])
        self.assertRegex(row["words"], r"^Tried to keep [1-9]\d* versions .*none were kept\.$")
        self.assertEqual((row["verified"], row["way_back"]), ([], "Nothing was named, so there is nothing to undo."))
        entry = json.loads((area.folder() / "receipts.jsonl").read_text().split("\n")[0])
        self.assertEqual(entry["count"], 0, "the count kept is the names added, never the names planned")
        # Docker answers again: the pass names what it could not before, and says when
        self.rig.docker_answer = answer_of
        answer = area.keep_now()
        self.assertEqual(answer["keep"]["outcome"], "ok")
        self.assertTrue(answer["words"].startswith("Kept:"))
        kept_at = answer["keep"]["kept_at"]
        self.assertTrue(kept_at)
        row = area.receipts_view()["receipts"][0]
        self.assertEqual((row["outcome_words"], row["words"].startswith("Kept ")), ("Done", True))
        answer = area.keep_now()
        self.assertEqual((answer["keep"]["outcome"], answer["words"], answer["keep"]["kept_at"]),
                         ("nothing", ys.KEEP_NOTHING, kept_at), "a pass with nothing to name keeps the last kept time")

    def test_a_pass_docker_refuses_in_part(self):
        area = self.area()
        confirm_all(area)
        self.refuse("seraphina-keep/photos-")
        answer = area.keep_now()
        self.assertEqual(answer["keep"]["outcome"], "partly")
        self.assertTrue(answer["words"].endswith("Docker did not add the others."), answer["words"])
        self.assertTrue(answer["keep"]["kept_at"])
        row = area.receipts_view()["receipts"][0]
        self.assertEqual(row["outcome_words"], "Partly done")
        kept, planned = map(int, re.match(r"^Kept (\d+) of (\d+) versions", row["words"]).groups())
        self.assertEqual(kept, answer["keep"]["tagged"])
        self.assertLess(kept, planned)

    def test_a_record_whose_outcome_is_not_known_says_so(self):
        area = self.area()
        self.rig.write(f"{sx.STATE}/receipts/R-1.json", {
            "schema": "seraphina-system-receipt/1", "id": "R-1", "verb": "keep.images", "class": "undo_instantly",
            "request": {"count": 3}, "after": {"names": []}, "ended": "2026-10-01T10:00:00Z"}, 0o600)
        row = area.receipts_view()["receipts"][0]
        self.assertEqual((row["outcome"], row["outcome_words"]), (None, "Outcome unknown"))
        self.assertTrue(row["words"].startswith("Tried to keep 3 versions"), row["words"])

    def test_automatic_after_an_update_and_off(self):
        area = self.area()
        ran = area.tick()
        self.assertIn("keep", ran, "with the keep class on, a running app without a kept name gets one")
        self.rig.config(allow=["read"], callers={str(SERAPHINA): ["read"]})
        area = self.area()
        self.assertFalse(area._keep_due(time.time()))
        with self.assertRaises(ys.ActionError):
            area.keep_now()

    def test_an_unknown_outcome_is_asked_about_and_never_sent_again(self):
        area = self.area()
        sent = []

        def runner(argv, data, timeout):
            request = json.loads(data)
            sent.append(request)
            if request["verb"] == "keep.images":
                return None, b"", b"(timed out)"
            return self.rig.runner(argv, data, timeout)
        area._client = HelperClient(sx.HELPER, runner=runner, name="Seraphina's system access")
        with self.assertRaises(ys.ActionError) as caught:
            area.keep_now()
        self.assertIn("It never started", str(caught.exception))
        self.assertEqual([r["verb"] for r in sent], ["keep.images", "receipts"], "asked once, never replayed")
        self.assertEqual(sent[1]["rid"], sent[0]["rid"])
        self.assertEqual(area._unknown[sent[0]["rid"]]["state"], "not_started")
        self.assertEqual(area.receipts_view()["unknown"], [], "it never started: Records of changes does not call its "
                                                           "outcome unknown")

    def ticking(self, runner):
        """An area on a clock the test moves, its helper calls through runner; (area, the requests sent, clock)."""
        now = [time.time()]
        area = self.area(clock=lambda: now[0])
        sent = []

        def recording(argv, data, timeout):
            sent.append(json.loads(data))
            return runner(argv, data, timeout)
        area._client = HelperClient(sx.HELPER, runner=recording, name="Seraphina's system access")
        return area, sent, now

    def keeps(self, sent):
        return [r for r in sent if r["verb"] == "keep.images"]

    def test_an_automatic_pass_whose_outcome_is_unknown_is_never_sent_again_by_itself(self):
        """§6.8: a pass that timed out after the helper started it (its rid left "started") opens one item, and
        the automatic pass waits for the owner, however many ticks pass; his own press is sent."""
        def runner(argv, data, timeout):
            request = json.loads(data)
            if request["verb"] == "keep.images":
                self.rig.write(f"{sx.STATE}/rids/{request['rid']}.json",
                               {"rid": request["rid"], "verb": "keep.images", "state": "started"}, 0o600)
                return None, b"", b"(timed out)"
            return self.rig.runner(argv, data, timeout)
        area, sent, now = self.ticking(runner)
        for _ in range(4):
            area.tick()
            now[0] += 700
        self.assertEqual(len(self.keeps(sent)), 1, "sent once, never replayed")
        self.assertEqual(len([key for key, opened in self.items if key.startswith("system:unknown:") and opened]), 1)
        self.assertFalse(area._keep_due(now[0]))
        self.assertTrue(area.receipts_view()["unknown"][0]["held"])
        with self.assertRaises(ys.ActionError):
            area.keep_now()
        self.assertEqual(len(self.keeps(sent)), 2, "the owner's own press is his choice")

    def test_a_refused_automatic_pass_waits_ten_minutes(self):
        self.rig.config(allow=ALL, callers={str(SERAPHINA): ALL}, docker_api=False)
        area, sent, now = self.ticking(self.rig.runner)
        for _ in range(6):
            area.tick()
            now[0] += 5
        self.assertEqual(len(self.keeps(sent)), 1, "a refusal is not sent again at every tick")
        self.assertEqual(len(os.listdir(self.rig.at(f"{sx.STATE}/rids"))), 1)
        now[0] += ys.KEEP_SPACING
        area.tick()
        self.assertEqual(len(self.keeps(sent)), 2, "and again after 10 minutes")

    def test_a_held_pass_is_asked_about_again_and_settles_when_it_is_done(self):
        """The helper ran the pass but its answer and the first receipts question were lost: held, one item; 10
        minutes later the record says done, the item closes and nothing was sent again."""
        lost = {"receipts": 1}

        def runner(argv, data, timeout):
            request = json.loads(data)
            if request["verb"] == "keep.images":
                self.rig.runner(argv, data, timeout)
                return None, b"", b"(timed out)"
            if request["verb"] == "receipts" and request.get("rid") and lost["receipts"]:
                lost["receipts"] -= 1
                return None, b"", b"(timed out)"
            return self.rig.runner(argv, data, timeout)
        area, sent, now = self.ticking(runner)
        area.tick()
        rid = self.keeps(sent)[0]["rid"]
        self.assertEqual(area._unknown[rid]["state"], "unknown")
        self.assertIn((f"system:unknown:{rid}", True), self.items)
        now[0] += 5
        area.tick()
        self.assertEqual([r["verb"] for r in sent].count("receipts"), 1, "not asked again at every tick")
        now[0] += ys.KEEP_SPACING
        area.tick()
        self.assertEqual((area._unknown[rid]["state"], area._unknown[rid]["held"]), ("done", False))
        self.assertIn((f"system:unknown:{rid}", False), self.items, "closed on that fresh observation")
        self.assertEqual(len(self.keeps(sent)), 1)

    def restarting(self, runner):
        """(a function making the area again on the same data dir, ledger and clock (a restart), the requests sent,
        the clock, the ledger's items under system:unknown:)."""
        from swfactory.events import EventBus
        from swfactory.housekeeping import HousekeepingManager

        data = tempfile.mkdtemp(prefix="ys-held-")
        self.addCleanup(shutil.rmtree, data, True)
        system = HousekeepingManager(Path(data), EventBus())
        now, sent = [time.time()], []

        def recording(argv, body, timeout):
            sent.append(json.loads(body))
            return runner(argv, body, timeout)

        def area():
            made = make(self.rig, {"harness": {"sandbox": "bwrap"}, "data_dir": data}, run=self.rig.harness_run,
                        background=False, identity=SERAPHINA_ID, system=system, clock=lambda: now[0],
                        client=HelperClient(sx.HELPER, runner=recording, name="Seraphina's system access"))
            made.refresh_map()
            return made

        def items():
            return {i["source"]["hash"]: i for i in system.store().ledger().items
                    if str((i.get("source") or {}).get("hash")).startswith("system:unknown:")}
        return area, sent, now, items

    def test_a_held_pass_stays_held_after_a_restart(self):
        """§6.8 across a restart: the hold is read back from the pass's open item, the helper is asked about that rid
        at the first tick, and while its record says started nothing is sent by itself; the owner's press still is."""
        def runner(argv, body, timeout):
            request = json.loads(body)
            if request["verb"] == "keep.images":
                self.rig.write(f"{sx.STATE}/rids/{request['rid']}.json",
                               {"rid": request["rid"], "verb": "keep.images", "state": "started"}, 0o600)
                return None, b"", b"(timed out)"
            return self.rig.runner(argv, body, timeout)
        area, sent, now, items = self.restarting(runner)
        area().tick()
        rid = self.keeps(sent)[0]["rid"]
        item = items()[f"system:unknown:{rid}"]
        self.assertEqual((item["status"], item["source"]["verb"]), ("open", "keep.images"))
        fresh = area()                       # the harness restarted: nothing of the hold is in memory
        for _ in range(3):
            now[0] += ys.KEEP_SPACING + 5
            fresh.tick()
        self.assertEqual(len(self.keeps(sent)), 1, "never sent again by itself, before or after the restart")
        self.assertEqual([r["rid"] for r in sent if r["verb"] == "receipts" and r.get("rid")][:2], [rid, rid],
                         "asked about at the first tick after the restart")
        unknown = fresh.receipts_view()["unknown"]
        self.assertEqual([(u["rid"], u["held"], u["state"]) for u in unknown], [(rid, True, "started")])
        with self.assertRaises(ys.ActionError):
            fresh.keep_now()
        self.assertEqual(len(self.keeps(sent)), 2, "the owner's own press is his choice")

    def test_a_held_pass_done_before_a_restart_settles_at_the_first_tick(self):
        lost = {"receipts": 1}

        def runner(argv, body, timeout):
            request = json.loads(body)
            if request["verb"] == "keep.images":
                self.rig.runner(argv, body, timeout)
                return None, b"", b"(timed out)"
            if request["verb"] == "receipts" and request.get("rid") and lost["receipts"]:
                lost["receipts"] -= 1
                return None, b"", b"(timed out)"
            return self.rig.runner(argv, body, timeout)
        area, sent, now, items = self.restarting(runner)
        area().tick()
        rid = self.keeps(sent)[0]["rid"]
        self.assertEqual(items()[f"system:unknown:{rid}"]["status"], "open")
        fresh = area()
        fresh.tick()
        self.assertEqual(items()[f"system:unknown:{rid}"]["status"], "verified", "closed on that fresh observation")
        self.assertEqual((fresh._unknown[rid]["held"], fresh._unknown[rid]["state"]), (False, "done"))
        self.assertEqual(fresh.receipts_view()["unknown"], [], "settled: no longer named as unknown")
        self.assertEqual(len(self.keeps(sent)), 1)

    def test_the_owners_press_lifts_the_hold_after_a_restart_too(self):
        """A timed-out automatic pass left "started" holds the automatic one; the owner's press lifts that hold and
        the lift is kept with the pass's item (still open: only the record closes it), so after a restart an app
        left without a kept name gets one by itself again. The pass is still named as unknown."""
        timeouts = {"keep": 1}

        def runner(argv, body, timeout):
            request = json.loads(body)
            if request["verb"] == "keep.images" and timeouts["keep"]:
                timeouts["keep"] -= 1
                self.rig.write(f"{sx.STATE}/rids/{request['rid']}.json",
                               {"rid": request["rid"], "verb": "keep.images", "state": "started"}, 0o600)
                return None, b"", b"(timed out)"
            return self.rig.runner(argv, body, timeout)
        area, sent, now, items = self.restarting(runner)
        first = area()
        first.tick()
        rid = self.keeps(sent)[0]["rid"]
        self.assertTrue(first._unknown[rid]["held"])
        now[0] += 5
        answer = first.keep_now()
        self.assertTrue(answer["words"].startswith("Kept:"), answer["words"])
        item = items()[f"system:unknown:{rid}"]
        self.assertEqual((item["status"], item["source"].get("released")), ("open", True))
        self.assertTrue(item["detail"].endswith(ys.UNKNOWN_RELEASED), item["detail"])
        fresh = area()                       # the harness restarted
        now[0] += ys.KEEP_SPACING + 5
        fresh.tick()
        self.assertEqual((fresh._unknown[rid]["held"], fresh._unknown[rid]["state"]), (False, "started"))
        self.assertEqual(items()[f"system:unknown:{rid}"]["status"], "open", "a record left started never closes it")
        self.assertEqual([(u["rid"], u["held"]) for u in fresh.receipts_view()["unknown"]], [(rid, False)])
        # an update leaves an app without a kept name: the automatic pass is due again, and is sent
        for image_id, tags in self.rig.keep_tags.items():
            self.rig.keep_tags[image_id] = [t for t in tags if not t.startswith("seraphina-keep/photos-")]
        for image in fresh.facts["images"]:
            image["tags"] = [t for t in image.get("tags") or [] if not t.startswith("seraphina-keep/photos-")]
        self.assertTrue(fresh._keep_due(now[0]))
        before = len(self.keeps(sent))
        now[0] += ys.KEEP_SPACING + 5
        fresh.tick()
        self.assertEqual(len(self.keeps(sent)), before + 1)

    def test_a_press_while_the_pass_runs_closes_its_item_when_it_is_done(self):
        """The press comes while the held pass's record still says running: the hold is lifted, and the helper is
        still asked about that rid, so its item closes when the record says done."""
        timeouts = {"keep": 1}

        def runner(argv, body, timeout):
            request = json.loads(body)
            if request["verb"] == "keep.images" and timeouts["keep"]:
                timeouts["keep"] -= 1
                self.rig.write(f"{sx.STATE}/rids/{request['rid']}.json",
                               {"rid": request["rid"], "verb": "keep.images", "state": "running"}, 0o600)
                return None, b"", b"(timed out)"
            return self.rig.runner(argv, body, timeout)
        area, sent, now, items = self.restarting(runner)
        made = area()
        made.tick()
        rid = self.keeps(sent)[0]["rid"]
        made.keep_now()
        self.assertEqual((made._unknown[rid]["held"], items()[f"system:unknown:{rid}"]["status"]), (False, "open"))
        self.rig.write(f"{sx.STATE}/rids/{rid}.json", {"rid": rid, "verb": "keep.images", "state": "done"}, 0o600)
        now[0] += ys.KEEP_SPACING + 5
        made.tick()
        self.assertEqual(made._unknown[rid]["state"], "done")
        self.assertEqual(items()[f"system:unknown:{rid}"]["status"], "verified", "closed on that fresh observation")
        self.assertEqual(made.receipts_view()["unknown"], [])

    def test_images_under_the_owners_prefixes_are_never_kept_again_by_itself(self):
        """The helper's keep plan leaves an image under the owner's keep_tag_prefixes alone (outcome nothing), so
        the automatic pass is not due for it: never a keep.images every 10 minutes. The repository is the helper's
        (a registry with a port is not a prefix's name)."""
        probe = self.area()
        prefixes = sorted({ys.image_repository(c["image"]) for c in probe.facts["containers"]
                           if c.get("running") and ys._labels(c).get("com.docker.compose.project")})
        self.rig.config(allow=ALL, callers={str(SERAPHINA): ALL})
        now = [time.time()]
        sent = []

        def recording(argv, data, timeout):
            sent.append(json.loads(data))
            return self.rig.runner(argv, data, timeout)
        area = self.area({"yourserver": {"keep_tag_prefixes": prefixes}}, clock=lambda: now[0],
                         client=HelperClient(sx.HELPER, runner=recording, name="Seraphina's system access"))
        self.assertFalse(area._keep_due(now[0]))
        for _ in range(4):
            area.tick()
            now[0] += 700
        self.assertEqual(self.keeps(sent), [], "nothing for the helper to name, so nothing is sent")
        area.facts["images"] = [{"id": "sha256:" + "e" * 64, "tags": ["registry.example:5000/team/app:1"]},
                                {"id": "sha256:" + "f" * 64, "tags": ["team/app:2"]}]
        area.config.raw["yourserver"]["keep_tag_prefixes"] = ["registry.example", "team/app"]
        self.assertEqual(area._kept_ids(), {"sha256:" + "f" * 64})

    def test_a_helper_that_ends_without_an_answer_is_asked_about(self):
        def runner(argv, data, timeout):
            if json.loads(data)["verb"] == "keep.images":
                return 1, b"", b"Traceback (most recent call last):\n"
            return self.rig.runner(argv, data, timeout)
        area, sent, _ = self.ticking(runner)
        with self.assertRaises(ys.ActionError):
            area.keep_now()
        self.assertEqual([r["verb"] for r in sent[-2:]], ["keep.images", "receipts"])
        self.assertEqual(sent[-1]["rid"], sent[-2]["rid"], "receipts {rid}, never a replay")


class RefusalTests(Case):
    """DESIGN §42: a press the area cannot carry out now is a 409 with the helper's own words, a record that does
    not exist a 404, never a 500."""

    def hold_the_job_lock(self) -> None:
        import fcntl

        os.makedirs(self.rig.at(sx.RUN_DIR), exist_ok=True)
        fd = os.open(self.rig.at(sx.RUN_DIR) / "job.lock", os.O_RDWR | os.O_CREAT)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.addCleanup(os.close, fd)

    def test_busy_and_other_refusals(self):
        from swfactory import server

        area = self.area()
        self.hold_the_job_lock()
        for press in (area.root_check, lambda: area.run_check("updates", {"refresh": True})):
            with self.assertRaises(ys.ActionError) as caught:
                press()
            self.assertEqual(server.error_status(caught.exception), 409)
            self.assertIn("Another job of Seraphina's system access is running",
                          server._error_message(caught.exception, 409))
            self.assertEqual(caught.exception.payload["code"], "busy")
        for missing in (lambda: area.job_view("J-999"), lambda: area.receipt_view("R-999")):
            with self.assertRaises(KeyError) as caught:
                missing()
            self.assertEqual(server.error_status(caught.exception), 404)
        self.rig.docker_active = False
        with self.assertRaises(ys.ActionError) as caught:
            area.keep_now()
        self.assertIn("Docker is stopped", str(caught.exception))
        self.assertEqual(server.error_status(caught.exception), 409)

    def test_a_logs_page_the_helper_refuses(self):
        from swfactory import server

        area = self.area()
        confirm_all(area)

        def refusing(argv, data, timeout):
            if json.loads(data)["verb"] == "logs":
                return None, b"", b"(timed out)"
            return self.rig.runner(argv, data, timeout)
        area._client = HelperClient(sx.HELPER, runner=refusing, name="Seraphina's system access")
        with self.assertRaises(ys.ActionError) as caught:
            area.logs("a-photos")
        self.assertEqual(server.error_status(caught.exception), 409)
        self.assertIn("did not answer", str(caught.exception))


class ReceiptPageTests(Case):
    """Records of changes a page at a time: the next page starts right after the last record shown, so none is
    skipped at a page boundary."""

    def test_every_record_once(self):
        area = self.area()
        for n in range(1, 26):
            self.rig.write(f"{sx.STATE}/receipts/R-{n}.json", {
                "schema": "seraphina-system-receipt/1", "id": f"R-{n}", "verb": "keep.images",
                "class": "undo_instantly", "outcome": "ok", "ended": f"2026-10-{n:02d}T10:00:00Z"}, 0o600)
        first = area.receipts_view()
        self.assertEqual(first["next"], "R-6")
        second = area.receipts_view(first["next"])
        self.assertIsNone(second["next"])
        ids = [r["id"] for r in first["receipts"] + second["receipts"]]
        self.assertEqual(sorted(ids, key=lambda i: -int(i[2:])), [f"R-{n}" for n in range(25, 0, -1)])
        self.assertEqual(len(ids), len(set(ids)), "none twice")


class LogsTests(Case):
    """Check 8: read through the helper only under P1c's preconditions; otherwise the links and the reasons."""

    def test_the_preconditions(self):
        for identity, raw, code in ((OWNER_ID, {}, "login_account"),
                                    (dict(SERAPHINA_ID, groups=["docker"]), {}, "root_groups"),
                                    (SERAPHINA_ID, {"harness": {"sandbox": "none"}}, "sandbox"),
                                    (SERAPHINA_ID, {"drivers": {"cc": {"type": "cli"}},
                                                    "agents": {"coder": {"driver": "cc"}}}, "cli_driver")):
            with self.subTest(code=code):
                area = self.area(raw, identity=identity)
                state = area.private_state()
                self.assertFalse(state["ready"])
                self.assertIn(code, [r["code"] for r in state["reasons"]])
                page = area.logs("a-photos")
                self.assertEqual((page["available"], page["words"]), (False, ys.LOGS_WAIT))
                self.assertNotIn("lines", page)
        self.rig.config(allow=["read"], callers={str(SERAPHINA): ["read"]})
        area = self.area()
        self.assertEqual([r["code"] for r in area.private_state()["reasons"]], ["class"])

    def test_a_job_or_chat_on_a_command_line_agent(self):
        """A job routed to a CLI driver, or a chat on one, runs an agent outside the sandbox as Seraphina's user:
        logs and the root page wait for it as for one in the agents section, and the doctor says so."""
        def doctor(config):
            client = HelperClient(sx.HELPER, runner=self.rig.runner, name="Seraphina's system access")
            return {c["name"]: c for c in ys.doctor_checks(
                config, client=client, identity=("seraphina", ["seraphina"]),
                check=lambda name, ok, level, words: {"name": name, "ok": ok, "level": level, "words": words},
                ports=[], ingress=[], identity_full=SERAPHINA_ID)}
        area = self.area({"drivers": {"builtin": {"type": "builtin"}, "claude": {"type": "cli", "command": "claude"}}})
        self.assertTrue(area.private_state()["ready"])
        self.assertTrue(doctor(area.config)["your server logs and root page"]["ok"])
        data = Path(area.config.data_dir)
        job = data / "jobs" / "example" / "001-fix-it" / "job.json"
        job.parent.mkdir(parents=True)
        job.write_text(json.dumps({"id": "001-fix-it", "repo": "example", "stage": "building",
                                   "routing": {"builder": {"driver": "claude", "model": "opus"}}}))
        self.assertIn("cli_driver", [r["code"] for r in area.private_state()["reasons"]])
        self.assertEqual(area.logs("a-photos")["available"], False)
        self.assertEqual(area.root_view()["available"], False)
        self.assertFalse(doctor(area.config)["your server logs and root page"]["ok"])
        job.write_text(json.dumps({"id": "001-fix-it", "repo": "example", "stage": "merged",
                                   "routing": {"builder": {"driver": "claude", "model": "opus"}}}))
        self.assertTrue(area.private_state()["ready"], "a job that is over runs no agent")
        chat = data / "chats" / "c-0123456789" / "chat.json"
        chat.parent.mkdir(parents=True)
        chat.write_text(json.dumps({"id": "c-0123456789", "agent": "coder", "driver": "claude", "model": "opus"}))
        self.assertIn("cli_driver", [r["code"] for r in area.private_state()["reasons"]])

    def test_a_plan_lead_or_a_judge_alternate_on_a_command_line_agent(self):
        """A plan's lead runs an agent too, routed by the plan's routing and profile: one that is not finished and
        leads on a CLI driver closes logs and the root page; so does a job whose judge's independent alternate (or
        overflow) runs on one."""
        area = self.area({"drivers": {"builtin": {"type": "builtin"}, "claude": {"type": "cli", "command": "claude"}}})
        data = Path(area.config.data_dir)
        plan = data / "plans" / "example" / "P001-tidy-up" / "plan.json"
        plan.parent.mkdir(parents=True)
        plan.write_text(json.dumps({"id": "P001-tidy-up", "repo": "example", "stage": "ready", "profile": "",
                                    "routing": {"lead": {"driver": "builtin"}}}))
        self.assertTrue(area.private_state()["ready"], "a lead on the built-in driver")
        plan.write_text(json.dumps({"id": "P001-tidy-up", "repo": "example", "stage": "ready", "profile": "",
                                    "routing": {"lead": {"driver": "claude", "model": "opus"}}}))
        self.assertIn("cli_driver", [r["code"] for r in area.private_state()["reasons"]])
        self.assertEqual(area.logs("a-photos")["available"], False)
        self.assertEqual(area.root_view()["available"], False)
        plan.write_text(json.dumps({"id": "P001-tidy-up", "repo": "example", "stage": "done", "profile": "",
                                    "routing": {"lead": {"driver": "claude", "model": "opus"}}}))
        self.assertTrue(area.private_state()["ready"], "a finished plan runs no lead")
        plan.write_text(json.dumps({"id": "P001-tidy-up", "repo": "example", "stage": "blocked", "profile": "",
                                    "routing": "made-up-preset"}))
        with unittest.mock.patch("swfactory.settings.load_preset", return_value={"agents": {"lead": {"driver":
                                                                                                    "claude"}}}):
            self.assertIn("cli_driver", [r["code"] for r in area.private_state()["reasons"]], "the preset's lead")
        plan.unlink()
        job = data / "jobs" / "example" / "002-check-it" / "job.json"
        job.parent.mkdir(parents=True)
        job.write_text(json.dumps({"id": "002-check-it", "repo": "example", "stage": "reviewing", "routing": {
            "reviewer": {"driver": "builtin", "model": "opus", "independent_alternate": {"driver": "claude",
                                                                                        "model": "opus"}}}}))
        self.assertIn("cli_driver", [r["code"] for r in area.private_state()["reasons"]])

    def test_a_page(self):
        area = self.area({"yourserver": {"links": {"dockge": "http://192.0.2.10:5001"}}})
        confirm_all(area)
        page = area.logs("a-photos")
        self.assertTrue(page["available"])
        texts = [line["text"] for line in page["lines"]]
        self.assertIn("<img src=x onerror=alert('log')>", texts)
        self.assertNotIn("PLANTED", json.dumps(page))
        self.assertNotIn("eyJ", json.dumps(page))
        for name in os.listdir(area.folder()):
            if os.path.isfile(area.folder() / name):
                self.assertNotIn("Starting the photo library", (area.folder() / name).read_text(), name)
        with self.assertRaises(ValueError):
            area.logs("a-photos", lines="9999")
        with self.assertRaises(ValueError):
            area.logs("a-photos", member="0" * 16)


class RootPageTests(Case):
    """T23 and U7's data: the page waits with its reasons and the explain block; then rows grouped, marks, new rows,
    the can't-check list; root.json keeps ids only; P3 changes only as words."""

    def look(self, area):
        area.root_check()
        job = next(j for j, v in area._jobs.items() if v["verb"] == "access" and not v.get("ended"))
        self.rig.write("/proc/self/cgroup", f"0::/system.slice/{sx.unit_name('access', job)}.service\n")
        sx.run_job(job, self.rig.paths, self.rig.system, self.rig.lstat, out=lambda _t: None)
        area._poll_jobs(force=True)
        return area.root_view()

    def test_waits_with_its_reasons(self):
        area = self.area(identity=OWNER_ID)
        view = area.root_view()
        self.assertFalse(view["available"])
        self.assertEqual(view["explain"]["commands"], ["sudo /usr/local/libexec/seraphina/sf-system explain access"])
        self.assertEqual(view["groups"], [])
        with self.assertRaises(ys.ActionError):
            area.root_check()

    def test_the_page_marks_and_new_rows(self):
        area = self.area()
        view = self.look(area)
        self.assertTrue(view["available"])
        self.assertTrue(view["headline"].startswith("Seraphina found"))
        self.assertTrue(view["headline"].endswith(rootaccess.HEADLINE_TAIL))
        self.assertEqual([g["id"] for g in view["groups"]][:2], ["people", "programs"])
        self.assertGreaterEqual(len(view["cannot_check"]), 8)
        self.assertEqual(view["new_ways"], 0, "the first look is not new")
        people = next(g for g in view["groups"] if g["id"] == "people")["rows"]
        sudo_row = next(r for r in people if r["kind"] == "sudo" and r["way"])
        self.assertEqual([p["id"] for p in sudo_row["p3"]], ["remove_rule", "lock"])
        self.assertEqual(view["p3_why"], rootaccess.P3_WHY)
        marked = area.root_mark(sudo_row["id"], {"known": True, "note": "my own"})
        row = next(r for g in marked["groups"] for r in g["rows"] if r["id"] == sudo_row["id"])
        self.assertEqual((row["known"], row["note"]), (True, "my own"))
        # a new way appears: Kate joins the docker group
        with open(self.rig.at("/etc/group"), encoding="utf-8") as handle:
            text = handle.read()
        self.rig.write("/etc/group", text.replace("docker:x:998:owner,dockerpal", "docker:x:998:owner,dockerpal,kate"))
        view = self.look(area)
        self.assertEqual(view["new_ways"], 1)
        self.assertIn("1 new since Seraphina last looked.", view["headline"])
        self.assertTrue(any(key.startswith("yourserver:root:") and opened for key, opened in self.items))
        self.assertTrue(any(n["key"] == "yourserver:root" for n in area.needs(area.cards())))
        doc = (area.folder() / "root.json").read_text()
        for word in ("kate", "owner", "docker", "sudo", "laptop", "/home"):
            self.assertNotIn(word, doc.replace("my own", ""), f"root.json holds ids, times and marks only: {word}")
        self.assertEqual(stat.S_IMODE(os.stat(area.folder() / "root.json").st_mode), 0o600)
        for key, _ in self.items:
            self.assertRegex(key, r"^(yourserver:root:[0-9a-f]{16}|system:.*)$", "never a name in a tracker key")
        with self.assertRaises(ValueError):
            area.root_mark("x", {"known": True})
        with self.assertRaises(KeyError):
            area.root_mark("0" * 16, {"known": True})

    def test_events_carry_counts_only(self):
        events = []

        class Bus:
            def publish(self, kind, payload):
                events.append((kind, payload))
        area = self.area(bus=Bus())
        self.look(area)
        area.run_check("restart")
        text = json.dumps(events)
        for word in ("owner", "kate", "sudo", "ssh.service", "example-notes"):
            self.assertNotIn(word, text)


class StageAndDoctorTests(Case):
    """Staging the classes the owner ticks, and T30's new lines."""

    def test_stage_classes(self):
        area = self.area()
        page = area.stage(user="seraphina", classes=["keep", "refresh"])
        staged = json.loads((area.folder() / "setup" / "system-helper.json").read_text())
        self.assertEqual(staged["allow"], ["read", "refresh", "keep"])
        self.assertIn("keep the previous version of each app", page["blocks"][0]["expect"])
        self.assertTrue(all(c["staged"] == (c["id"] in ("keep", "refresh")) for c in page["classes"]))
        owner = self.area(identity=OWNER_ID)
        with self.assertRaises(ValueError):
            owner.stage(user="owner", classes=["private"])
        with self.assertRaises(ValueError):
            area.stage(user="seraphina", classes=["maintain"])

    def test_doctor_lines(self):
        def make_check(name, ok, level, words):
            return {"name": name, "ok": ok, "level": level, "words": words}
        client = HelperClient(sx.HELPER, runner=self.rig.runner, name="Seraphina's system access")
        config = make(self.rig, {"harness": {"sandbox": "none"}}).config
        lines = {c["name"]: c for c in ys.doctor_checks(config, client=client, identity=("seraphina", ["seraphina"]),
                                                         check=make_check, ports=[], ingress=[],
                                                         identity_full=SERAPHINA_ID)}
        self.assertEqual(lines["your server sandbox"]["level"], "error", "private while the sandbox is off")
        self.assertFalse(lines["your server logs and root page"]["ok"])
        self.rig.config(allow=["read", "keep"], callers={str(SERAPHINA): ["read", "keep"]})
        lines = {c["name"]: c for c in ys.doctor_checks(config, client=client, identity=("seraphina", ["seraphina"]),
                                                         check=make_check, ports=[], ingress=[],
                                                         identity_full=SERAPHINA_ID)}
        self.assertEqual(lines["your server sandbox"]["level"], "warn")
        self.assertIn("add kept names", lines["your server sandbox"]["words"])
        self.rig.config(allow=ALL, callers={str(SERAPHINA): ALL})
        config = make(self.rig, {"harness": {"sandbox": "bwrap"}}).config
        lines = {c["name"]: c for c in ys.doctor_checks(config, client=client, identity=("seraphina", ["seraphina"]),
                                                         check=make_check, ports=[], ingress=[],
                                                         identity_full=SERAPHINA_ID)}
        self.assertTrue(lines["your server logs and root page"]["ok"])
        lines = {c["name"]: c for c in ys.doctor_checks(config, client=client, identity=("owner", ["owner"]),
                                                         check=make_check, ports=[], ingress=[],
                                                         identity_full=OWNER_ID)}
        self.assertEqual(lines["your server private"]["level"], "error")


class RouteAndWordTests(unittest.TestCase):
    """T29 for the new routes, and U8 over the new modules' templates."""

    def test_routes(self):
        from swfactory import server

        handlers = [h for name, h in vars(server._H).items() if name.startswith("api_yourserver")]
        self.assertGreaterEqual(len(handlers), 25)
        mine = [(m, p.pattern, h) for m, p, h in server._ROUTES if "/api/yourserver" in p.pattern]
        self.assertGreaterEqual(len(mine), 25)
        for handler in handlers:
            self.assertNotIn(handler, server.AGENT_TOKEN_ROUTES, "never an agent's route")
        for name in ("api_yourserver_checks", "api_yourserver_check", "api_yourserver_logs", "api_yourserver_keep",
                     "api_yourserver_jobs", "api_yourserver_job", "api_yourserver_receipts", "api_yourserver_receipt",
                     "api_yourserver_root", "api_yourserver_root_check", "api_yourserver_root_mark"):
            self.assertTrue(hasattr(server._H, name), name)

    def test_words(self):
        for text in ys.novice_strings():
            self.assertEqual(ys.internal_terms(text), [], text)


if __name__ == "__main__":
    unittest.main()


class TailnetFallbackTests(unittest.TestCase):
    """Appendix A11: with Tailscale only in containers, tailnet_status falls back to the confirmed suffix, so the
    notifier may put a link in a notice again."""

    def test_the_fallback(self):
        import unittest.mock

        from swfactory import channels, notify

        self.assertIsNone(channels.tailnet_status(run=lambda argv: ""))
        self.assertEqual(channels.tailnet_status(run=lambda argv: "", confirmed="example.ts.net"),
                         {"suffix": "example.ts.net", "addresses": [], "funnel": None, "source": "confirmed"})
        self.assertIsNone(channels.tailnet_status(run=lambda argv: "", confirmed="example.com"), "only a ts.net name")
        live = json.dumps({"Self": {"TailscaleIPs": ["100.64.0.1"]}, "CurrentTailnet": {"MagicDNSSuffix": "live.ts.net"}})
        self.assertEqual(channels.tailnet_status(run=lambda argv: live, confirmed="example.ts.net")["suffix"],
                         "live.ts.net", "Tailscale's own answer wins when it can be asked")
        with unittest.mock.patch.object(channels, "_run", lambda argv, timeout=5.0: ""):
            get = notify._cached_tailnet(confirmed=lambda: "example.ts.net")
            self.assertEqual(get()["suffix"], "example.ts.net")
        config = type("C", (), {"raw": {"yourserver": {"tailnet": {"suffix": "example.ts.net"}}}})()
        self.assertEqual(notify._confirmed_suffix(config), "example.ts.net")
