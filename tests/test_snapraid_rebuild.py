"""Tests for swfactory/snapraid_rebuild.py, the owner's checklist for a whole failed disk (DESIGN.md §41 "P4";
docs/seraphina/BACKUPS_SNAPRAID.md §9.4, §9.5, D18): every step's check and its blocks, the order of the steps, the
new disk's typed serial number, the light and the card while a checklist is open, the "not answering" notice held
back while the owner has the protection service off, privacy of the record, the doctor line, the routes and the
CLI. The facts are built here (placeholder disks, paths and UUIDs only); the daemon route's integration test uses
tests/mock_snapraidd.py through the real link. Nothing runs SnapRAID, sudo or a block: the only programs the
checklist starts are systemctl show (faked here) and, in one test, sed on a temporary copy of an fstab.

L13 (a boot with the failed disk's line turned off, or with nofail for an absent disk) and L20 (the whole checklist
on loop images with SnapRAID 15.1 and the daemon) need a server started by systemd and a real protection service:
they are not run here, and the card, the checklist page and the doctor list them as not checked yet."""

from __future__ import annotations

import copy
import io
import json
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

from swfactory import snapraid, snapraid_rebuild as rb, snapraidd
from swfactory.config import Config
from swfactory.events import EventBus

U1 = "11111111-1111-4111-8111-111111111111"
U2 = "22222222-2222-4222-8222-222222222222"
U3 = "33333333-3333-4333-8333-333333333333"
NEW = "44444444-4444-4444-8444-444444444444"
OLD_ID = "/dev/disk/by-id/ata-Example_Disk_8TB_SN-EXAMPLE-2"
NEW_ID = "/dev/disk/by-id/ata-Example_Disk_8TB_SN-EXAMPLE-NEW1"
USED_ID = "/dev/disk/by-id/usb-Example_Portable_SN-EXAMPLE-USED9-0:0"
SERIAL = "SN-CANARY-SERIAL-7Q"          # searched for in everything kept (privacy)


def unit(state: str, status: int = 0, at: int = 1) -> dict:
    """systemctl show's properties for a unit in a given state; one that ran started at second `at` after the boot
    and exited a second later."""
    ran = {"ExecMainStartTimestampMonotonic": str(at * 10 ** 6),
           "ExecMainExitTimestampMonotonic": str((at + 1) * 10 ** 6)}
    return {"absent": {"LoadState": "not-found", "ActiveState": "inactive", "SubState": "dead"},
            "running": {"LoadState": "loaded", "ActiveState": "active", "SubState": "running", "ExecMainCode": "0"},
            "ended": {"LoadState": "loaded", "ActiveState": "active" if status == 0 else "failed",
                      "SubState": "exited" if status == 0 else "failed", "Result": "success" if status == 0
                      else "exit-code", "ExecMainCode": "1", "ExecMainStatus": str(status), **ran},
            "killed": {"LoadState": "loaded", "ActiveState": "failed", "SubState": "failed", "Result": "signal",
                       "ExecMainCode": "2", "ExecMainStatus": "9"},
            "on": {"LoadState": "loaded", "ActiveState": "active", "SubState": "running",
                   "UnitFileState": "enabled"},
            "off_enabled": {"LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead",
                            "UnitFileState": "enabled"},
            "off": {"LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead",
                    "UnitFileState": "disabled"}}[state]


FSTAB = (f"UUID={U1} /mnt/disk1 ext4 defaults,nofail,x-systemd.device-timeout=30s 0 2\n"
         f"UUID={U2} /mnt/disk2 ext4 defaults 0 2\n"
         f"UUID={U3} /mnt/parity1 ext4 defaults,nofail,x-systemd.device-timeout=30s 0 2\n")


def facts(route: str = "daemon") -> dict:
    """Disk 2 has failed: not mounted, the daemon reads it degraded, the service still on."""
    now = time.time()
    return {
        "route": route, "reader": {},
        "info": {"conf": "/etc/snapraid.conf", "binary": "/usr/bin/snapraid",
                 "mounts": {"d1": {"role": "data", "number": 1, "path": "/mnt/disk1", "mounted": True,
                                   "uuid_ok": True, "fstype": "ext4"},
                            "d2": {"role": "data", "number": 2, "path": "/mnt/disk2", "mounted": False,
                                   "uuid_ok": False, "fstype": None},
                            "parity": {"role": "parity", "number": 1, "path": "/mnt/parity1", "mounted": True,
                                       "uuid_ok": True}},
                 "uuids": {"d1": U1, "d2": U2, "parity": U3}, "data": ["/mnt/disk1", "/mnt/disk2"],
                 "schedulers": [], "snapraid_running": False},
        "disks": {"d2": {"model": "Example Disk 8TB", "serial": SERIAL, "by_id": OLD_ID, "size": 8_000_000_000_000,
                         "present": False, "seen_at": "2026-10-20T10:00:00Z", "fstype": "ext4", "mount": "/mnt/disk2",
                         "uuid": U2}},
        "spare": [{"device": "/dev/sdx", "by_id": NEW_ID, "size": 8_001_000_000_000, "model": "Example Disk 8TB",
                   "serial": "SN-EXAMPLE-NEW1", "signature": False},
                  {"device": "/dev/sdy", "by_id": USED_ID, "size": 4_000_000_000_000, "model": "Example Portable",
                   "serial": "SN-EXAMPLE-USED9", "signature": True},
                  {"device": "/dev/sdz", "by_id": None, "size": 1_000_000_000_000, "model": "Odd", "serial": "X1",
                   "signature": False}],
        "daemon": {"answering": True, "active": None, "hold_off": False, "health": "degraded",
                   "disk_health": {"d1": "passed", "d2": "degraded", "parity": "passed"},
                   "window": "tonight at 02:00", "offers": {"hold_off": True}, "protected_up_to": now - 5 * 3600}
        if route == "daemon" else None,
        "smart": {"d1": "ok", "d2": "fail"} if route == "reader" else None,
        "units": {rb.DAEMON_UNIT: unit("on") if route == "daemon" else unit("absent"),
                  rb.REBUILD_UNIT: unit("absent"), rb.CHECK_UNIT: unit("absent")},
        "snapraid_running": False, "fstab": FSTAB, "stacks": ["photos"], "power_off": "23:30",
        "protected_up_to": now - 5 * 3600}


def rebuilt(f: dict, fix: int = 0, check: int = 0) -> dict:
    """The owner ran step 5's blocks in turn: the rebuild ended, then the check started and ended."""
    f["units"][rb.REBUILD_UNIT] = unit("ended", fix, at=10)
    f["units"][rb.CHECK_UNIT] = unit("ended", check, at=20)
    return f


def stopped(f: dict) -> dict:
    """The owner ran step 1's block: the service off for good, /metrics silent."""
    f["units"][rb.DAEMON_UNIT] = unit("off")
    if f.get("daemon"):
        f["daemon"]["answering"] = False
    return f


def fitted(f: dict) -> dict:
    """The owner ran step 4's blocks: the new disk mounted at the old place, the reader told its UUID."""
    f["info"]["mounts"]["d2"].update(mounted=True, uuid_ok=True, fstype="ext4")
    f["info"]["uuids"]["d2"] = NEW
    f["fstab"] = FSTAB.replace(f"UUID={U2} /mnt/disk2 ext4 defaults 0 2\n", f"# UUID={U2} /mnt/disk2 ext4 defaults 0 2\n"
                               f"UUID={NEW} /mnt/disk2 ext4 defaults,nofail,x-systemd.device-timeout=30s 0 2\n")
    f["spare"] = [row for row in f["spare"] if row["by_id"] != NEW_ID]
    f["disks"]["d2"] = {"model": "Example Disk 8TB", "serial": "SN-EXAMPLE-NEW1", "by_id": NEW_ID, "present": True,
                        "fstype": "ext4", "mount": "/mnt/disk2", "uuid": NEW}
    return f


class Walk:
    """One checklist walked step by step against facts the test changes."""

    def __init__(self, route: str = "daemon") -> None:
        self.facts = facts(route)
        self.now = time.time()
        self.record = rb.open_record(self.facts, "d2", self.now)

    def go(self, step: str, confirm: list | None = None) -> dict:
        self.record, result = rb.advance(self.record, self.facts, step, confirm, self.now)
        return result

    def page(self) -> dict:
        return rb.build(self.record, self.facts, self.now)

    def step(self, step: str) -> dict:
        return next(s for s in self.page()["steps"] if s["id"] == step)


class OpeningTests(unittest.TestCase):
    def test_choices_mark_the_failed_disk_and_leave_out_protection_disks_and_one_being_taken_out(self):
        f = facts()
        f["info"]["mounts"]["d3"] = {"role": "data", "number": 3, "path": "/var/lib/snapraid-out/d3",
                                     "mounted": True, "uuid_ok": True, "placeholder": True}
        rows = rb.choices(f)
        self.assertEqual([(row["name"], row["label"], row["suggested"]) for row in rows],
                         [("d1", "Disk 1", False), ("d2", "Disk 2", True)])
        self.assertEqual(rows[1]["health_words"], "missing or not answering")

    def test_open_refuses_what_it_cannot_do(self):
        f = facts()
        for disk, words in (("parity", rb.NO_PROTECTION), ("d9", "Seraphina does not know that disk")):
            with self.assertRaises(ValueError) as caught:
                rb.open_record(f, disk, time.time())
            self.assertIn(words, str(caught.exception))
        f["info"]["mounts"]["d2"]["path"] = "/mnt/disk 2"
        with self.assertRaisesRegex(ValueError, "characters she does not put into a command"):
            rb.open_record(f, "d2", time.time())
        f = facts()
        f["info"] = None
        with self.assertRaisesRegex(ValueError, "needs the disk protection reader"):
            rb.open_record(f, "d2", time.time())
        f["reader"] = {"error": "sudo would not run the reader."}
        with self.assertRaisesRegex(ValueError, "did not answer: sudo would not run"):
            rb.open_record(f, "d2", time.time())

    def test_the_record_keeps_no_model_serial_or_by_id_name(self):
        walk = Walk()
        text = json.dumps(walk.record)
        for secret in (SERIAL, OLD_ID, "Example Disk"):
            self.assertNotIn(secret, text)
        self.assertEqual((walk.record["disk"], walk.record["number"], walk.record["label"], walk.record["mount"],
                          walk.record["old_uuid"], walk.record["fstype"]), ("d2", 2, "Disk 2", "/mnt/disk2", U2, "ext4"))

    def test_steps_go_in_order_and_later_steps_show_no_blocks(self):
        walk = Walk()
        with self.assertRaisesRegex(ValueError, "Do the steps in order: stop changes comes first"):
            walk.go("rebuild")
        page = walk.page()
        self.assertEqual([s["state"] for s in page["steps"]], ["current"] + ["later"] * 6)
        self.assertTrue(all(not s["blocks"] for s in page["steps"][1:]))
        self.assertEqual([s["title"] for s in page["steps"]], list(rb.TITLES.values()))
        self.assertTrue(page["steps"][3]["cannot_undo"])
        with self.assertRaises(ValueError):
            walk.go("stop", ["found"])          # only this step's lines can be ticked


class StopTests(unittest.TestCase):
    """Step 1 (§9.4): skip the next run, turn the service off only when nothing runs, stop the apps."""

    def test_the_service_on_then_off_for_good(self):
        walk = Walk()
        stop = walk.step("stop")
        self.assertEqual(stop["check"]["code"], "service_up")
        self.assertTrue(stop["skip"], "the skip of the next run is offered (the existing approval)")
        self.assertEqual(stop["blocks"][0]["commands"], ["sudo systemctl disable --now snapraidd.service"])
        self.assertIn("Apps Seraphina found that use these disks: photos.", stop["facts"])
        self.assertFalse(walk.go("stop", ["apps"])["done"])
        walk.facts["daemon"]["answering"] = False
        walk.facts["units"][rb.DAEMON_UNIT] = unit("off_enabled")
        self.assertEqual(walk.go("stop")["check"]["code"], "service_enabled", "it would start again at the reboot")
        stopped(walk.facts)
        result = walk.go("stop")
        self.assertTrue(result["done"], result)
        self.assertEqual(walk.record["confirmed"]["stop"], ["apps"], "a tick is kept across presses")

    def test_a_running_task_comes_first_and_the_apps_must_be_ticked(self):
        walk = Walk()
        walk.facts["daemon"]["active"] = {"command": "sync", "high_command": "maintenance"}
        self.assertEqual(walk.step("stop")["check"]["words"],
                         "The protection service is running now (protecting the latest changes). Wait until it ends, "
                         "or stop it safely on the Backups page, then turn the service off.")
        walk.facts["daemon"]["active"] = None
        stopped(walk.facts)
        result = walk.go("stop")
        self.assertEqual((result["done"], result["missing"], result["words"]), (False, ["apps"], rb.TICK_FIRST))
        walk.facts["snapraid_running"] = True
        self.assertEqual(walk.go("stop", ["apps"])["check"]["code"], "snapraid_running")
        walk.facts["snapraid_running"] = False
        walk.facts["units"][rb.DAEMON_UNIT] = None
        result = walk.go("stop")
        self.assertEqual((result["check"]["code"], result["done"], result["missing"]),
                         ("service_unknown", False, ["service_off"]), "the apps' tick alone is not enough")
        self.assertFalse(walk.record["done"].get("stop"), "systemctl not answering, /metrics silent: cannot tell")
        self.assertIn(rb.CONFIRMS["service_off"], [c["words"] for c in walk.step("stop")["confirms"]])
        self.assertTrue(walk.go("stop", ["service_off"])["done"], "the owner checked the service himself")

    def test_a_skip_already_set_is_not_offered_again(self):
        walk = Walk()
        walk.facts["daemon"]["hold_off"] = True
        stop = walk.step("stop")
        self.assertFalse(stop["skip"])
        self.assertTrue(stop["skipped"])

    def test_the_readers_route_lists_what_starts_runs_and_asks_both_ticks(self):
        walk = Walk("reader")
        walk.facts["info"]["schedulers"] = ["/etc/cron.d/snapraid"]
        stop = walk.step("stop")
        self.assertEqual(stop["check"]["code"], "quiet")
        self.assertEqual(stop["blocks"], [], "no service to turn off on this route")
        self.assertIn("What starts protection runs here: /etc/cron.d/snapraid.", stop["facts"])
        self.assertEqual([c["item"] for c in stop["confirms"]], ["schedulers", "apps"])
        self.assertTrue(walk.go("stop", ["schedulers", "apps"])["done"])

    def test_dockge_unreadable_or_empty(self):
        walk = Walk()
        walk.facts["stacks"] = None
        self.assertIn(rb.FACT_WORDS["stacks_unknown"], walk.step("stop")["facts"])
        walk.facts["stacks"] = []
        self.assertIn(rb.FACT_WORDS["no_stacks"], walk.step("stop")["facts"])


class NameAndBootTests(unittest.TestCase):
    def advance_to(self, walk: Walk, step: str) -> None:
        stopped(walk.facts)
        for before in rb.STEPS[:rb.STEPS.index(step)]:
            items = [c["item"] for c in walk.step(before)["confirms"]]
            self.assertTrue(walk.go(before, items)["done"], before)

    def test_name_shows_the_disk_live_and_needs_the_owner_to_find_it(self):
        walk = Walk()
        self.advance_to(walk, "name")
        name = walk.step("name")
        self.assertEqual(name["facts"][:3], ["Disk 2 (d2), mounted at /mnt/disk2.",
                                             f"Model Example Disk 8TB, serial number {SERIAL}.",
                                             f"Its name that never changes: {OLD_ID}."])
        self.assertIn("Its health: missing or not answering.", name["facts"])
        self.assertFalse(walk.go("name")["done"])
        self.assertTrue(walk.go("name", ["found"])["done"])

    def test_no_record_of_the_disk_and_too_many_failed(self):
        walk = Walk()
        self.advance_to(walk, "name")
        walk.facts["disks"] = {}
        self.assertEqual(walk.step("name")["check"]["code"], "no_identity")
        walk.facts["info"]["mounts"]["d1"]["mounted"] = False
        check = walk.step("name")["check"]
        self.assertEqual((check["ok"], check["code"]), (False, "too_many"))
        self.assertIn("2 disks have failed, more than the protection can rebuild (1)", check["words"])
        self.assertFalse(walk.go("name", ["found"])["done"])

    def test_boot_a_line_without_nofail_gets_the_block_then_passes(self):
        walk = Walk()
        self.advance_to(walk, "boot")
        boot = walk.step("boot")
        self.assertEqual(boot["check"]["code"], "needs_change")
        commands = boot["blocks"][0]["commands"]
        self.assertRegex(commands[0], r"^sudo cp -a /etc/fstab /etc/fstab\.before-disk-2-\d{8}-\d{4}$")
        self.assertEqual(commands[2:], ["sudo findmnt --verify", "sudo systemctl daemon-reload"])
        self.assertIn(commands[0].split()[-1], boot["blocks"][0]["if_not"], "if not: the copy goes back")
        self.assertFalse(walk.go("boot")["done"])
        walk.facts["fstab"] = run_sed(commands[1], FSTAB)
        self.assertEqual(walk.step("boot")["check"]["code"], "line_off")
        self.assertTrue(walk.go("boot")["done"])

    def test_boot_other_answers(self):
        walk = Walk()
        self.advance_to(walk, "boot")
        walk.facts["fstab"] = FSTAB.replace(f"UUID={U2} /mnt/disk2 ext4 defaults",
                                            f"UUID={U2} /mnt/disk2 ext4 defaults,nofail,x-systemd.device-timeout=10s")
        self.assertEqual((walk.step("boot")["check"]["code"], walk.step("boot")["blocks"]), ("nofail", []))
        walk.facts["fstab"] = f"UUID={U1} /mnt/disk1 ext4 defaults 0 2\n"
        boot = walk.step("boot")
        self.assertEqual((boot["check"]["ok"], boot["check"]["code"]), (None, "no_line"))
        self.assertEqual([c["item"] for c in boot["confirms"]], ["boot"], "cannot tell: the owner says he checked")
        self.assertFalse(walk.go("boot")["done"])
        walk.facts["fstab"] = None
        self.assertEqual(walk.step("boot")["check"]["code"], "fstab_unreadable")
        self.assertTrue(walk.go("boot", ["boot"])["done"])

    def test_an_indented_line_is_turned_off_too(self):
        """fstab_lines reads an indented line as active, so the block must reach it (else step 3 never passes)."""
        walk = Walk()
        self.advance_to(walk, "boot")
        walk.facts["fstab"] = FSTAB.replace(f"UUID={U2} /mnt/disk2", f"  \tUUID={U2} /mnt/disk2")
        boot = walk.step("boot")
        self.assertEqual(boot["check"]["code"], "needs_change")
        walk.facts["fstab"] = run_sed(boot["blocks"][0]["commands"][1], walk.facts["fstab"])
        self.assertIn(f"#   \tUUID={U2} /mnt/disk2 ext4 defaults 0 2\n", walk.facts["fstab"])
        self.assertEqual(walk.step("boot")["check"]["code"], "line_off")
        self.assertTrue(walk.go("boot")["done"])

    def test_the_line_is_found_by_its_filesystem_too(self):
        lines = rb.fstab_lines(f"/dev/disk/by-uuid/{U2} /srv/elsewhere ext4 defaults 0 2\n", "/mnt/disk2", U2)
        self.assertEqual(len(lines), 1)


def run_sed(command: str, text: str) -> str:
    """Run a block's sed command (sudo dropped) on a temporary copy of an fstab, with the real sed."""
    if not shutil.which("sed"):
        raise unittest.SkipTest("no sed on this machine")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "fstab"
        path.write_text(text, encoding="utf-8")
        argv = command.split(" ", 1)[1].replace("/etc/fstab", str(path))
        subprocess.run(["sh", "-c", argv], check=True)
        return path.read_text(encoding="utf-8")


class SedTests(unittest.TestCase):
    """The comment-out command touches exactly the line for its folder (regular expression characters escaped)."""

    def test_only_that_line(self):
        text = ("UUID=a /mnt/disk.1 ext4 defaults 0 2\nUUID=b /mnt/diskx1 ext4 defaults 0 2\n"
                "UUID=c /mnt/disk.1/sub ext4 defaults 0 2\n# UUID=d /mnt/disk.1 ext4 defaults 0 2\n"
                "/dev/sdq1\t/mnt/disk.1/\text4\tdefaults\t0\t2\n")
        out = run_sed(rb.comment_out_sed("/mnt/disk.1"), text).splitlines()
        self.assertEqual(out, ["# UUID=a /mnt/disk.1 ext4 defaults 0 2", "UUID=b /mnt/diskx1 ext4 defaults 0 2",
                               "UUID=c /mnt/disk.1/sub ext4 defaults 0 2", "# UUID=d /mnt/disk.1 ext4 defaults 0 2",
                               "# /dev/sdq1\t/mnt/disk.1/\text4\tdefaults\t0\t2"])

    def test_running_it_twice_changes_nothing_more(self):
        once = run_sed(rb.comment_out_sed("/mnt/disk2"), FSTAB)
        self.assertEqual(run_sed(rb.comment_out_sed("/mnt/disk2"), once), once)

    def test_indented_lines_and_a_line_of_two_fields(self):
        """Every line fstab_lines reads as active for the folder, as it reads it: indented by spaces or a tab, or
        with no fields after the folder; never one already turned off, nor another folder."""
        text = ("  UUID=a /mnt/disk2 ext4 defaults,nofail 0 2\n\tUUID=b\t/mnt/disk2/\text4\tdefaults\t0\t2\n"
                "  # UUID=c /mnt/disk2 ext4 defaults 0 2\n  UUID=d /mnt/disk20 ext4 defaults 0 2\nUUID=e /mnt/disk2\n"
                "UUID=f /mnt/disk2x ext4 defaults 0 2\n")
        once = run_sed(rb.comment_out_sed("/mnt/disk2"), text)
        self.assertEqual(once.splitlines(), ["#   UUID=a /mnt/disk2 ext4 defaults,nofail 0 2",
                                             "# \tUUID=b\t/mnt/disk2/\text4\tdefaults\t0\t2",
                                             "  # UUID=c /mnt/disk2 ext4 defaults 0 2",
                                             "  UUID=d /mnt/disk20 ext4 defaults 0 2", "# UUID=e /mnt/disk2",
                                             "UUID=f /mnt/disk2x ext4 defaults 0 2"])
        self.assertEqual([line["active"] for line in rb.fstab_lines(once, "/mnt/disk2")], [False] * 4)
        self.assertEqual(run_sed(rb.comment_out_sed("/mnt/disk2"), once), once)


class DiskTests(unittest.TestCase):
    """Step 4 (cannot be undone): the new disk named by /dev/disk/by-id, its serial number typed first."""

    def walk_to_disk(self) -> Walk:
        walk = Walk()
        NameAndBootTests.advance_to(self, walk, "boot")
        walk.facts["fstab"] = run_sed(rb.comment_out_sed("/mnt/disk2"), FSTAB)
        self.assertTrue(walk.go("boot")["done"])
        return walk

    def test_the_free_disks_and_the_typed_serial(self):
        walk = self.walk_to_disk()
        disk = walk.step("disk")
        self.assertEqual([b["commands"] for b in disk["blocks"]], [["sudo systemctl poweroff"]])
        rows = disk["spare"]
        self.assertEqual([(r["by_id"], r["signed"], r["usable"]) for r in rows],
                         [(NEW_ID, False, True), (None, False, False), (USED_ID, True, True)])
        self.assertTrue(rows[2]["smaller"])
        with self.assertRaisesRegex(ValueError, "not this disk's serial number"):
            rb.prepare(walk.record, walk.facts, NEW_ID, "NEW2", walk.now)
        with self.assertRaisesRegex(ValueError, "not this disk's serial number"):
            rb.prepare(walk.record, walk.facts, USED_ID, "SED9", walk.now)    # a used disk: the whole number
        with self.assertRaisesRegex(ValueError, "not one of the free disks"):
            rb.prepare(walk.record, walk.facts, OLD_ID, "PLE2", walk.now)
        blocks = rb.prepare(walk.record, walk.facts, NEW_ID, "new1", walk.now)["blocks"]
        self.assertEqual([b["id"] for b in blocks], ["4c", "4d", "4e"])
        text = "\n".join(c for b in blocks for c in b["commands"] + b.get("then", []))
        self.assertNotIn("/dev/sd", text, "never a /dev/sdX name")
        self.assertIn(f"sudo parted -s -a optimal {NEW_ID} mklabel gpt mkpart primary ext4 0% 100%", text)
        self.assertEqual(blocks[0]["commands"][1:], ["sudo udevadm settle", f"sudo wipefs -a {NEW_ID}-part1",
                                                     f"sudo mkfs.ext4 {NEW_ID}-part1"],
                         "what an old partition held shows again in the new one: erased right before mkfs")
        self.assertIn(f'echo "UUID=$(sudo blkid -s UUID -o value {NEW_ID}-part1) /mnt/disk2 ext4 '
                      'defaults,nofail,x-systemd.device-timeout=30s 0 2" | sudo tee -a /etc/fstab', text)
        self.assertIn(f'sudo sed -i "s/{U2}/$(sudo findmnt -n -o UUID --mountpoint /mnt/disk2)/" '
                      f'/etc/seraphina/snapraid-helper.json.new', text)
        self.assertEqual(blocks[2]["then"], ["sudo /usr/local/libexec/seraphina/sf-snapraid explain helper-config > "
                                             "/dev/null && sudo mv /etc/seraphina/snapraid-helper.json.new "
                                             "/etc/seraphina/snapraid-helper.json"])
        used = rb.prepare(walk.record, walk.facts, USED_ID, "SN-EXAMPLE-USED9", walk.now)["blocks"]
        self.assertEqual(used[0]["commands"], [f"sudo wipefs -a {USED_ID}"])
        self.assertEqual(used[1]["commands"][2:], [f"sudo wipefs -a {USED_ID}-part1",
                                                   f"sudo mkfs.ext4 {USED_ID}-part1"])
        self.assertNotIn("SN-EXAMPLE-NEW1", json.dumps(walk.record), "nothing of the new disk is kept")

    def test_no_serial_no_by_id_and_not_now(self):
        walk = self.walk_to_disk()
        walk.facts["spare"].append({"device": "/dev/sdw", "by_id": "/dev/disk/by-id/wwn-0x5000", "size": 1,
                                    "model": "Q", "serial": None, "signature": False})
        with self.assertRaisesRegex(ValueError, "cannot read this disk's serial number"):
            rb.prepare(walk.record, walk.facts, "/dev/disk/by-id/wwn-0x5000", "x", walk.now)
        walk.facts["spare"].append({"device": "/dev/sdv", "by_id": "/dev/disk/by-id/bad name;rm", "size": 1,
                                    "model": "Q", "serial": "ABCD", "signature": False})
        with self.assertRaisesRegex(ValueError, "no name in /dev/disk/by-id"):
            rb.prepare(walk.record, walk.facts, "/dev/disk/by-id/bad name;rm", "ABCD", walk.now)
        early = Walk()
        with self.assertRaisesRegex(ValueError, "the step to fit and prepare it"):
            rb.prepare(early.record, early.facts, NEW_ID, "NEW1", early.now)

    def test_the_check_after_fitting(self):
        walk = self.walk_to_disk()
        self.assertEqual(walk.step("disk")["check"]["code"], "not_mounted")
        walk.facts["info"]["mounts"]["d2"].update(mounted=True, uuid_ok=True)
        self.assertEqual(walk.step("disk")["check"]["code"], "old_disk_back")
        walk.facts["info"]["mounts"]["d2"]["uuid_ok"] = False
        walk.facts["info"]["uuids"]["d2"] = U2
        self.assertEqual(walk.step("disk")["check"]["code"], "reader_old")
        fitted(walk.facts)
        safe = f"UUID={NEW} /mnt/disk2 ext4 defaults,nofail,x-systemd.device-timeout=30s 0 2\n"
        walk.facts["fstab"] = walk.facts["fstab"].replace(safe, f"UUID={NEW} /mnt/disk2 ext4 defaults 0 2\n")
        self.assertEqual(walk.step("disk")["check"]["code"], "fstab_unsafe")
        fitted(walk.facts)
        walk.facts["fstab"] += safe.replace(NEW, U1)
        self.assertEqual(walk.step("disk")["check"]["code"], "fstab_twice", "the server would use only the first")
        fitted(walk.facts)
        walk.facts["units"][rb.DAEMON_UNIT] = unit("on")
        self.assertEqual(walk.step("disk")["check"]["code"], "service_back", "the service came back at the reboot")
        walk.facts["units"][rb.DAEMON_UNIT] = unit("off")
        disk = walk.step("disk")
        self.assertEqual(disk["check"]["code"], "mounted")
        self.assertIn("Mounted at /mnt/disk2 now: Example Disk 8TB, serial number SN-EXAMPLE-NEW1.", disk["facts"])
        self.assertTrue(walk.go("disk")["done"])

    def test_an_indented_old_line_with_nofail_is_turned_off_by_the_new_disks_block(self):
        """Step 3 passes an indented old line that has nofail; step 4's block must turn it off, or the server would
        mount the old, absent disk's line first at the next start, and step 4 must not pass while both are on."""
        walk = Walk()
        stopped(walk.facts)
        walk.facts["fstab"] = FSTAB.replace(f"UUID={U2} /mnt/disk2 ext4 defaults",
                                            f"  UUID={U2} /mnt/disk2 ext4 defaults,nofail,x-systemd.device-timeout=30s")
        for step in ("stop", "name", "boot"):
            self.assertTrue(walk.go(step, [c["item"] for c in walk.step(step)["confirms"]])["done"], step)
        self.assertEqual(walk.record["checks"]["boot"]["code"], "nofail")
        sed = rb.prepare(walk.record, walk.facts, USED_ID, "SN-EXAMPLE-USED9", walk.now)["blocks"][2]["commands"][1]
        new_line = f"UUID={NEW} /mnt/disk2 ext4 defaults,nofail,x-systemd.device-timeout=30s 0 2\n"
        both = walk.facts["fstab"] + new_line
        fitted(walk.facts)
        walk.facts["fstab"] = both
        self.assertEqual(walk.step("disk")["check"]["code"], "fstab_twice")
        self.assertFalse(walk.go("disk")["done"])
        walk.facts["fstab"] = run_sed(sed, walk.facts["fstab"].replace(new_line, "")) + new_line
        self.assertEqual([line["active"] for line in rb.fstab_lines(walk.facts["fstab"], "/mnt/disk2")], [False, True])
        self.assertTrue(walk.go("disk")["done"])

    def test_the_reader_must_answer_before_the_new_disk_counts_as_fitted(self):
        """Step 4 cannot be undone, and its proof is the reader's: without it the step never passes, and a service
        that came back on is still reported first."""
        walk = self.walk_to_disk()
        fitted(walk.facts)
        walk.facts["reader"] = {"error": "The disk protection reader took too long."}
        result = walk.go("disk")
        self.assertEqual((result["done"], result["check"]["ok"], result["check"]["code"]),
                         (False, False, "reader_unknown"))
        self.assertEqual(walk.step("disk")["confirms"], [], "no tick can stand in for the reader here")
        self.assertEqual(rb.current(walk.record), "disk")
        walk.facts["units"][rb.DAEMON_UNIT] = unit("on")
        self.assertEqual(walk.step("disk")["check"]["code"], "service_back")
        walk.facts["units"][rb.DAEMON_UNIT] = unit("off")
        walk.facts["reader"] = {}
        self.assertTrue(walk.go("disk")["done"])

    def test_the_old_disk_back_shows_the_way_back(self):
        """It works after all: the service turned on again (the daemon's route) and, when step 3 turned the line
        off, the copy it kept put back; then the checklist may be closed."""
        walk = Walk()
        stopped(walk.facts)
        walk.facts["fstab"] = FSTAB.replace(f"UUID={U2} /mnt/disk2 ext4 defaults",
                                            f"UUID={U2} /mnt/disk2 ext4 defaults,nofail,x-systemd.device-timeout=30s")
        for step in ("stop", "name", "boot"):
            self.assertTrue(walk.go(step, [c["item"] for c in walk.step(step)["confirms"]])["done"], step)
        walk.facts["info"]["mounts"]["d2"].update(mounted=True, uuid_ok=True)
        disk = walk.step("disk")
        self.assertEqual(disk["check"]["code"], "old_disk_back")
        self.assertIn("turn on again what they turned off", disk["check"]["words"])
        commands = [c for b in disk["blocks"] for c in b["commands"]]
        self.assertIn("sudo systemctl enable --now snapraidd.service", commands)
        self.assertFalse([c for c in commands if "/etc/fstab" in c], "its line was never turned off")
        walk.facts["fstab"] = run_sed(rb.comment_out_sed("/mnt/disk2"), walk.facts["fstab"])
        back = next(b for b in walk.step("disk")["blocks"] if b["id"] == "3z")
        self.assertEqual(back["commands"][:2], ["ls -1 /etc/fstab.before-disk-2-*",
                                                "sudo cp -a /etc/fstab.before-disk-2-<the newest one> /etc/fstab"])
        reader = Walk("reader")
        for step in ("stop", "name", "boot"):
            reader.record["done"][step] = reader.now
        reader.facts["info"]["mounts"]["d2"].update(mounted=True, uuid_ok=True)
        self.assertEqual(reader.step("disk")["check"]["code"], "old_disk_back")
        self.assertNotIn("snapraidd", json.dumps(reader.step("disk")["blocks"]), "no service on the reader's route")

    def test_a_checklist_closed_before_the_new_disk_shows_how_to_undo_step_1(self):
        walk = Walk()
        self.assertIsNone(rb.build(dict(walk.record, state="cancelled"), walk.facts, walk.now)["way_back"],
                          "nothing was turned off yet")
        stopped(walk.facts)
        walk.go("stop", ["apps"])
        page = rb.build(dict(walk.record, state="cancelled", closed_at=walk.now), walk.facts, walk.now)
        self.assertEqual(page["way_back"]["blocks"][0]["commands"], ["sudo systemctl enable --now snapraidd.service"])
        self.assertIn("Do it only while no new disk is mounted at /mnt/disk2", page["way_back"]["words"][0])
        self.assertIn(rb.WHAT["done_apps"], page["way_back"]["words"])
        walk.record["done"].update(name=walk.now, boot=walk.now, disk=walk.now)
        page = rb.build(dict(walk.record, state="cancelled"), walk.facts, walk.now)
        self.assertIsNone(page["way_back"], "a new disk went in: turning the service on would take it in empty")

    def test_a_protected_disk_is_never_offered_nor_erased(self):
        """Defence in depth: the reader leaves out every disk in use, and the checklist still refuses any free disk
        that is one of the disks SnapRAID protects (present or last known, the failed one too), by its
        /dev/disk/by-id name or its serial number."""
        walk = self.walk_to_disk()
        parity_id = "/dev/disk/by-id/ata-Example_Disk_8TB_SN-EXAMPLE-P1"
        walk.facts["disks"]["parity"] = {"model": "Example Disk 8TB", "serial": "SN-EXAMPLE-P1",
                                         "by_id": parity_id + "-part1", "present": True}
        walk.facts["spare"] += [{"device": "/dev/sdp", "by_id": parity_id, "size": 8_001_000_000_000,
                                 "model": "Example Disk 8TB", "serial": "SN-EXAMPLE-P1", "signature": False},
                                {"device": "/dev/sdq", "by_id": OLD_ID, "size": 8_000_000_000_000,
                                 "model": "Example Disk 8TB", "serial": "OTHER", "signature": True},
                                {"device": "/dev/sdr", "by_id": "/dev/disk/by-id/wwn-0x5000c500", "size": 1,
                                 "model": "Example Disk 8TB", "serial": SERIAL.lower(), "signature": False}]
        rows = walk.step("disk")["spare"]
        self.assertEqual([r["by_id"] for r in rows], [NEW_ID, None, USED_ID])
        for by_id, typed in ((parity_id, "E-P1"), (OLD_ID, "OTHER"), ("/dev/disk/by-id/wwn-0x5000c500", "L-7Q")):
            with self.assertRaisesRegex(ValueError, "one of the disks Seraphina protects"):
                rb.prepare(walk.record, walk.facts, by_id, typed, walk.now)

    def test_an_xfs_disk_and_an_unknown_old_filesystem_uuid(self):
        walk = self.walk_to_disk()
        walk.record["fstype"], walk.record["old_uuid"] = "xfs", None
        blocks = rb.prepare(walk.record, walk.facts, NEW_ID, "NEW1", walk.now)["blocks"]
        self.assertIn(f"sudo mkfs.xfs {NEW_ID}-part1", blocks[0]["commands"])
        self.assertEqual(blocks[-1]["commands"][2], "sudoedit /etc/seraphina/snapraid-helper.json.new")
        self.assertIn("put the new one in place of the old one for d2", blocks[-1]["note"])

    def test_once_the_new_disk_is_mounted_its_last_block_comes_from_the_record(self):
        """Block 4d mounts the new disk, so the reader rightly lists it as free no more and its commands cannot be
        asked again; the page itself then gives block 4e, built from the record alone (after a reload, or on
        another device, the commands shown after the serial number are gone), and no other disk's commands."""
        walk = self.walk_to_disk()
        prepared = rb.prepare(walk.record, walk.facts, NEW_ID, "NEW1", walk.now)["blocks"]
        walk.facts["info"]["mounts"]["d2"].update(mounted=True, uuid_ok=False)      # 4d ran, 4e not yet
        walk.facts["spare"] = [row for row in walk.facts["spare"] if row["by_id"] != NEW_ID]
        disk = walk.step("disk")
        self.assertEqual((disk["check"]["code"], [b["id"] for b in disk["blocks"]]), ("reader_old", ["4a", "4e"]))
        self.assertIn("with the block below", disk["check"]["words"])
        self.assertEqual(disk["blocks"][1], prepared[-1], "the same block as the one after the serial number")
        self.assertEqual(disk["blocks"][1]["commands"][1],
                         f'sudo sed -i "s/{U2}/$(sudo findmnt -n -o UUID --mountpoint /mnt/disk2)/" '
                         "/etc/seraphina/snapraid-helper.json.new")
        self.assertIsNone(disk["spare"], "no free disk's commands once a new disk is mounted at the folder")
        for by_id, typed in ((NEW_ID, "NEW1"), (USED_ID, "SN-EXAMPLE-USED9")):
            with self.assertRaisesRegex(ValueError, "A new disk is mounted at /mnt/disk2 already"):
                rb.prepare(walk.record, walk.facts, by_id, typed, walk.now)
        walk.record["old_uuid"] = None            # the old filesystem unknown: the hand-edited 4e, from the record too
        self.assertEqual(walk.step("disk")["blocks"][1]["commands"][0],
                         "sudo findmnt -n -o UUID --mountpoint /mnt/disk2")
        walk.record["old_uuid"] = U2
        fitted(walk.facts)
        disk = walk.step("disk")
        self.assertEqual((disk["check"]["code"], [b["id"] for b in disk["blocks"]], disk["spare"]),
                         ("mounted", ["4a"], None))
        self.assertTrue(walk.go("disk")["done"])

    def test_the_readers_block_tells_it_what_is_mounted_and_nothing_when_nothing_is(self):
        """Block 4e run on a test machine (findmnt's answer given): the reader's new settings name the filesystem
        mounted at the folder; with nothing mounted there, the reader refuses them, so its move does not happen."""
        from tests.snapraid_rig import UUIDS, Rig, sf

        if not shutil.which("sed"):
            raise unittest.SkipTest("no sed on this machine")
        block = rb.reader_block({"mount": "/mnt/disk2", "disk": "d2", "old_uuid": UUIDS["d2"]})
        found = "sudo findmnt -n -o UUID --mountpoint /mnt/disk2"
        for answer, code in ((f"echo {NEW}", 0), ("true", 2)):
            rig = Rig()
            self.addCleanup(rig.close)
            for command in block["commands"][:2]:
                command = command.replace(found, answer).replace(rb.READER_CONFIG, rig.paths.helper_config)
                subprocess.run(["sh", "-c", command.split(" ", 1)[1]], check=True)          # sudo dropped
            self.assertEqual(block["commands"][2], f"sudo {rb.READER} explain helper-config")
            out = io.StringIO()
            self.assertEqual(sf.main(["explain", "helper-config"], stdout=out, paths=rig.paths, system=rig.system,
                                     lstat=rig.lstat), code, out.getvalue())
            if code == 0:
                self.assertIn(f"d2: the filesystem {NEW} not mounted now (was {UUIDS['d2']})", out.getvalue())
            else:
                self.assertIn("mounts must map each disk's SnapRAID name to its filesystem UUID", out.getvalue())
        self.assertEqual(block["then"][0].split(" && ")[0], f"sudo {rb.READER} explain helper-config > /dev/null",
                         "the move only when the reader takes the new settings")

    def test_a_rebuild_left_from_an_earlier_checklist_is_cleared_first(self):
        """The two units outlive their checklist (RemainAfterExit; closing runs no tidy, and the last block may not
        have been run) until the next start of the server: step 4 does not pass while either is there, so step 5
        never takes an earlier run's end for this one's, and its own rebuild can start."""
        walk = self.walk_to_disk()
        fitted(walk.facts)
        units = walk.facts["units"]
        for rebuild_state, check_state in ((unit("ended", 0, at=10), unit("ended", 0, at=20)),
                                           (unit("ended", 1), unit("absent")), (unit("running"), unit("absent")),
                                           (unit("absent"), unit("killed"))):
            units[rb.REBUILD_UNIT], units[rb.CHECK_UNIT] = rebuild_state, check_state
            disk = walk.step("disk")
            self.assertEqual((disk["check"]["ok"], disk["check"]["code"]), (False, "old_rebuild"))
            self.assertEqual(disk["blocks"][-1]["id"], "4f")
            self.assertEqual(disk["blocks"][-1]["commands"],
                             ["sudo systemctl stop seraphina-disk-rebuild.service seraphina-disk-check.service "
                              "2>/dev/null; true",
                              "sudo systemctl reset-failed seraphina-disk-rebuild.service seraphina-disk-check.service "
                              "2>/dev/null; true"])
            self.assertFalse(walk.go("disk")["done"])
        units[rb.REBUILD_UNIT] = units[rb.CHECK_UNIT] = None
        self.assertEqual(walk.step("disk")["check"]["code"], "mounted",
                         "systemctl silent: step 5 asks the owner's own tick about the units then")
        units[rb.REBUILD_UNIT], units[rb.CHECK_UNIT] = unit("absent"), unit("absent")    # the owner ran 4f
        self.assertTrue(walk.go("disk")["done"])
        self.assertNotIn("units_unread", walk.record, "both read, and gone")
        self.assertEqual(walk.step("rebuild")["check"]["code"], "not_started")

    def test_the_tidy_block_prints_nothing_when_a_unit_is_not_loaded(self):
        """Units left from an earlier checklist are often only half there (a rebuild with no check, a check alone).
        systemctl stop then says the missing one is not loaded, and exits 5, though it stops the other one: blocks 4f
        and 7a print what their Expect line says, nothing, whichever of the two is loaded. The systemctl here behaves
        as systemd's does for a unit that is not loaded (Failed to stop X: Unit X not loaded.)."""
        if not shutil.which("sh"):
            raise unittest.SkipTest("no sh on this machine")
        tidy = rb._tidy("4f")
        self.assertEqual(tidy["expect"], "Nothing printed.")
        self.assertEqual(rb._tidy("7a")["commands"], tidy["commands"])
        folder = Path(tempfile.mkdtemp(prefix="sf-tidy-"))
        self.addCleanup(shutil.rmtree, folder, True)
        fake = folder / "systemctl"
        fake.write_text('#!/bin/sh\nverb=$1; shift; status=0\nfor u in "$@"; do\n  case " $LOADED " in\n'
                        '    *" $u "*) echo "$verb $u" >> "$DONE" ;;\n'
                        '    *) echo "Failed to $verb $u: Unit $u not loaded." >&2; status=5 ;;\n  esac\ndone\n'
                        'exit $status\n', encoding="utf-8")
        fake.chmod(0o755)
        for loaded in ([rb.REBUILD_UNIT], [rb.CHECK_UNIT], [], [rb.REBUILD_UNIT, rb.CHECK_UNIT]):
            done = folder / "done"
            done.write_text("", encoding="utf-8")
            env = {"PATH": f"{folder}:/usr/bin:/bin", "LOADED": " ".join(loaded), "DONE": str(done)}
            for command in tidy["commands"]:
                self.assertTrue(command.startswith("sudo systemctl "), command)
                ran = subprocess.run(["sh", "-c", command.split(" ", 1)[1]], capture_output=True, text=True,
                                     env=env, check=False)          # sudo dropped
                self.assertEqual((ran.stdout, ran.stderr, ran.returncode), ("", "", 0), (loaded, command))
            self.assertEqual(done.read_text(encoding="utf-8").split("\n")[:-1],
                             [f"{verb} {name}" for verb in ("stop", "reset-failed") for name in loaded],
                             "the loaded one is still stopped and forgotten")


def lsblk(name: str, kind: str = "disk", fstype: str | None = None, uuid: str | None = None,
          mountpoint: str | None = None, model: str | None = None, serial: str | None = None,
          pttype: str | None = None, children: list | None = None, path: str | None = None) -> dict:
    """One device of `lsblk -J -b -o NAME,PATH,PKNAME,TYPE,SIZE,FSTYPE,UUID,MOUNTPOINT,MODEL,SERIAL,PTTYPE`, its
    children nested as lsblk prints them."""
    node = {"name": name, "path": path or f"/dev/{name}", "pkname": None, "type": kind, "size": 8_001_563_222_016,
            "fstype": fstype, "uuid": uuid, "mountpoint": mountpoint, "model": model, "serial": serial,
            "pttype": pttype}
    if children:
        node["children"] = [dict(child, pkname=name) for child in children]
    return node


class SpareDiskTests(unittest.TestCase):
    """The reader's free disks (bin/sf-snapraid's disks verb, spare), which step 4 turns into commands that erase
    one: from a real nested lsblk tree, only the disks nothing at any depth uses."""

    def test_only_the_disks_nothing_uses_are_free(self):
        from tests.snapraid_rig import HEALTHY, UUIDS, Rig, sf

        rig = Rig(dict(HEALTHY))
        self.addCleanup(rig.close)
        root, r = rig.root, str(rig.root)
        os_fs = "5555eeee-0000-4000-8000-000000000001"
        tree = [lsblk("sda", model="OS SSD", serial="SN-OS", pttype="gpt", children=[
                    lsblk("sda1", "part", "vfat", "AB12-CD34", "/boot/efi", pttype="gpt"),
                    lsblk("sda2", "part", "btrfs", os_fs, "/", pttype="gpt")]),
                lsblk("sdb", model="Disk", serial="SN-D1", pttype="gpt", children=[            # Disk 1
                    lsblk("sdb1", "part", "ext4", UUIDS["d1"], f"{r}/mnt/disk1", pttype="gpt")]),
                lsblk("sdc", model="Disk", serial="SN-D3", pttype="gpt", children=[            # Disk 3, on LVM
                    lsblk("sdc1", "part", "LVM2_member", "lvm-pv-uuid-1", pttype="gpt", children=[
                        lsblk("vg0-d3", "lvm", "ext4", UUIDS["d3"], f"{r}/mnt/disk3", path="/dev/mapper/vg0-d3")])]),
                lsblk("sdd", model="Disk", serial="SN-P1", pttype="gpt", children=[            # the protection disk
                    lsblk("sdd1", "part", "ext4", UUIDS["parity"], f"{r}/mnt/parity1", pttype="gpt")])] + [
                lsblk(name, model="Disk", serial=f"SN-{name}", pttype="gpt", children=[         # the second, on md
                    lsblk(f"{name}1", "part", "linux_raid_member", "md-uuid-1", pttype="gpt", children=[
                        lsblk("md0", "raid1", "ext4", UUIDS["2-parity"], f"{r}/mnt/parity2")])])
                for name in ("sde", "sdf")] + [
                lsblk("sdg", model="Vault", serial="SN-VAULT", fstype="crypto_LUKS", uuid="luks-uuid-1", children=[
                    lsblk("luks-1", "crypt", "ext4", "6666ffff-0000-4000-8000-000000000001", "/mnt/vault",
                          path="/dev/mapper/luks-1")]),
                lsblk("sdh", model="Used", serial="SN-USED", pttype="gpt", children=[            # free, holds a fs
                    lsblk("sdh1", "part", "ext4", "7777aaaa-0000-4000-8000-000000000001", pttype="gpt")]),
                lsblk("sdi", model="New", serial="SN-NEW"),                                       # free and blank
                lsblk("sdj", model="Disk", serial="SN-D2", pttype="gpt", children=[            # the failed Disk 2
                    lsblk("sdj1", "part", "ext4", UUIDS["d2"], pttype="gpt")]),
                lsblk("sdk", model="Swap", serial="SN-SWAP", pttype="gpt", children=[
                    lsblk("sdk1", "part", "swap", "8888bbbb-0000-4000-8000-000000000001", "[SWAP]", pttype="gpt")]),
                lsblk("sdl", model="Held", serial="SN-HELD", pttype="gpt", children=[            # held, not shown
                    lsblk("sdl1", "part", "ext4", "9999cccc-0000-4000-8000-000000000002", pttype="gpt")]),
                lsblk("sdm", model="Mirror", serial="SN-MIRROR", pttype="gpt", children=[        # the system's twin
                    lsblk("sdm1", "part", "btrfs", os_fs, pttype="gpt")]),
                lsblk("sdn", model="Labelled", serial="SN-LABEL", pttype="gpt"),                 # an empty table
                lsblk("sdo", model="Pool", serial="SN-POOL", fstype="zfs_member", uuid="1234567890"),
                lsblk("sr0", "rom", model="DVD")]
        mounts = [sf.Mount("/", "btrfs", "/dev/sda2", "0:31"), sf.Mount("/boot/efi", "vfat", "/dev/sda1", "8:1"),
                  sf.Mount(f"{r}/mnt/disk1", "ext4", "/dev/sdb1", "8:17"),
                  sf.Mount(f"{r}/mnt/disk3", "ext4", "/dev/mapper/vg0-d3", "253:0"),
                  sf.Mount(f"{r}/mnt/parity1", "ext4", "/dev/sdd1", "8:49"),
                  sf.Mount(f"{r}/mnt/parity2", "ext4", "/dev/md0", "9:0"),
                  sf.Mount("/mnt/vault", "ext4", "/dev/mapper/luks-1", "253:1")]
        uuids = {"/dev/sda2": os_fs, "/dev/sdb1": UUIDS["d1"], "/dev/mapper/vg0-d3": UUIDS["d3"],
                 "/dev/sdd1": UUIDS["parity"], "/dev/md0": UUIDS["2-parity"]}
        real_run = rig.system.run
        rig.system.mounts = lambda: mounts
        rig.system.uuid = lambda mount: uuids.get(mount.source)
        rig.system.run = lambda argv, timeout, out_path=None: (0, json.dumps({"blockdevices": tree}), False) \
            if argv[0] == "lsblk" else real_run(argv, timeout, out_path)
        (root / "sys/class/block/sdl1/holders/dm-9").mkdir(parents=True)     # device mapper built on it
        (root / "dev/disk/by-id").mkdir(parents=True)
        for name, disk in (("SN-NEW", "sdi"), ("SN-USED", "sdh"), ("SN-LABEL", "sdn"), ("SN-D3", "sdc"),
                           ("SN-D3-part1", "sdc1"), ("SN-P1", "sdd")):
            (root / "dev/disk/by-id" / f"ata-Disk_{name}").symlink_to(f"/dev/{disk}")
        code, answer = rig.ask({"v": 1, "verb": "disks"})
        self.assertEqual(code, 0, answer)
        self.assertEqual([(row["device"], row["signature"], row["by_id"]) for row in answer["spare"]],
                         [("/dev/sdh", True, "/dev/disk/by-id/ata-Disk_SN-USED"),
                          ("/dev/sdi", False, "/dev/disk/by-id/ata-Disk_SN-NEW"),
                          ("/dev/sdn", True, "/dev/disk/by-id/ata-Disk_SN-LABEL")])
        d3 = next(row for row in answer["disks"] if row["name"] == "d3")
        self.assertEqual((d3["device"], d3["serial"], d3["by_id"]),
                         ("/dev/mapper/vg0-d3", "SN-D3", "/dev/disk/by-id/ata-Disk_SN-D3"),
                         "a disk on LVM is named by the whole disk under it")


class NewDiskLabTests(unittest.TestCase):
    """Step 4's commands for a disk that held something, run with the real wipefs, parted and mkfs.xfs on a sparse
    image file (no root, no device: the new partition is copied out of the image, as the kernel would show it)."""

    def test_an_old_filesystem_where_the_new_partition_starts_does_not_stop_mkfs(self):
        tools = ("wipefs", "parted", "mkfs.ext4", "mkfs.xfs", "blkid")
        if not all(shutil.which(tool) for tool in tools):
            raise unittest.SkipTest("needs " + ", ".join(tools))
        walk = DiskTests.walk_to_disk(self)
        walk.record["fstype"] = "xfs"
        blocks = rb.prepare(walk.record, walk.facts, USED_ID, "SN-EXAMPLE-USED9", walk.now)["blocks"]
        self.assertEqual([b["id"] for b in blocks[:2]], ["4b", "4c"])
        tmp = Path(tempfile.mkdtemp(prefix="sfnewdisk-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        disk, part = tmp / "disk.img", tmp / "disk.img-part1"
        run = lambda *argv: subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL)  # noqa: E731
        with open(disk, "wb") as handle:
            handle.truncate(320 << 20)
        # the disk as an old Linux box left it: one GPT partition at 1 MiB holding ext4
        self.assertEqual(run("parted", "-s", "-a", "optimal", str(disk), "mklabel", "gpt", "mkpart", "primary",
                             "ext4", "0%", "100%").returncode, 0)
        self.assertEqual(run("mkfs.ext4", "-q", "-F", "-E", "offset=1048576", str(disk), "32M").returncode, 0)
        for command in blocks[0]["commands"] + blocks[1]["commands"]:
            argv = shlex.split(command.replace(USED_ID, str(disk)))[1:]          # sudo dropped
            if argv[0] == "udevadm":
                table = run("parted", "-s", "-m", str(disk), "unit", "B", "print").stdout.splitlines()
                start, _, size = (int(field.rstrip("B")) for field in table[2].split(":")[1:4])
                self.assertEqual(start, 1 << 20, "the new partition starts where the old one did")
                with open(disk, "rb") as source, open(part, "wb") as target:   # what the kernel shows as -part1
                    source.seek(start)
                    target.truncate(size)                    # sparse: past its first 64 MiB the image is all zeros
                    target.write(source.read(64 << 20))
                continue
            done = run(*argv)
            if argv[0] == "mkfs.xfs":
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn('TYPE="xfs"', run("blkid", "-p", str(part)).stdout)


class RebuildTests(unittest.TestCase):
    """Step 5: the owner starts the rebuild and its check; the units' states are read with systemctl show."""

    def walk_to_rebuild(self, route: str = "daemon", silent: tuple = ()) -> Walk:
        walk = Walk(route)
        stopped(walk.facts)
        walk.facts["fstab"] = run_sed(rb.comment_out_sed("/mnt/disk2"), FSTAB)
        for step in ("stop", "name", "boot"):
            self.assertTrue(walk.go(step, [c["item"] for c in walk.step(step)["confirms"]])["done"], step)
        fitted(walk.facts)
        for name in silent:             # systemctl did not answer about these at step 4's check
            walk.facts["units"][name] = None
        self.assertTrue(walk.go("disk")["done"])
        return walk

    def test_the_blocks_run_snapraid_only_as_the_owner_and_never_a_writing_command_but_the_rebuild(self):
        walk = self.walk_to_rebuild()
        rebuild = walk.step("rebuild")
        self.assertEqual(rebuild["check"]["code"], "not_started")
        start, check = rebuild["blocks"][0]["commands"][1], rebuild["blocks"][1]["commands"][0]
        self.assertTrue(start.startswith("sudo systemd-run --unit=seraphina-disk-rebuild "), start)
        self.assertIn("-p RemainAfterExit=yes -p KillSignal=SIGINT -p SendSIGKILL=no", start)
        self.assertTrue(start.endswith("/usr/bin/snapraid -c /etc/snapraid.conf -d d2 -l "
                                       "/var/lib/seraphina-snapraid/rebuild/rebuild.log fix"), start)
        self.assertTrue(check.endswith("-d d2 -a -l /var/lib/seraphina-snapraid/rebuild/check.log check"), check)
        for block in rebuild["blocks"]:
            for command in block["commands"]:
                self.assertNotRegex(command, r"\b(sync|scrub|touch|-E|--force)\b", command)
        self.assertIn("This server turns itself off at 23:30 every night (Settings): change that until the rebuild "
                      "has ended, or it stops.", rebuild["what"])

    def test_each_state_of_the_two_units(self):
        walk = self.walk_to_rebuild()
        units = walk.facts["units"]
        for (rebuild_state, status), check_state, code in ((("running", 0), "absent", "rebuild_running"),
                                                           (("killed", 0), "absent", "rebuild_stopped"),
                                                           (("ended", 203), "absent", "rebuild_stopped"),
                                                           (("ended", 1), "absent", "rebuild_failed"),
                                                           (("ended", 0), "absent", "check_not_started"),
                                                           (("ended", 0), "running", "check_running"),
                                                           (("ended", 0), "killed", "check_stopped")):
            units[rb.REBUILD_UNIT], units[rb.CHECK_UNIT] = unit(rebuild_state, status), unit(check_state)
            step = walk.step("rebuild")
            self.assertEqual(step["check"]["code"], code, (rebuild_state, status, check_state))
            self.assertEqual("5c" in [b["id"] for b in step["blocks"]], code.endswith(("stopped", "failed")), code)
            self.assertFalse(walk.go("rebuild")["done"])
        again = walk.step("rebuild")["blocks"][2]["commands"]
        self.assertEqual(again[:2], ["sudo systemctl stop seraphina-disk-check.service",
                                     "sudo systemctl reset-failed seraphina-disk-check.service"])
        units[rb.REBUILD_UNIT] = None
        self.assertEqual(walk.step("rebuild")["check"]["code"], "unit_unknown")
        units[rb.REBUILD_UNIT], units[rb.CHECK_UNIT] = unit("ended", 0), unit("absent")
        self.assertEqual(walk.step("rebuild")["check"]["words"],
                         'The rebuild has ended. Now run the check, with the block "Then check what came back".')
        rebuilt(walk.facts, 1, 1)
        walk.facts["daemon"]["answering"] = True
        self.assertEqual(walk.step("rebuild")["check"]["code"], "service_back")
        self.assertEqual(walk.step("rebuild")["blocks"], [], "no rebuild block while the service is on")
        walk.facts["daemon"]["answering"] = False
        self.assertTrue(walk.go("rebuild")["done"])
        self.assertEqual(walk.record["results"]["rebuild"], {"fix": 1, "check": 1})

    def test_a_rebuild_that_ended_with_an_error_is_not_taken_for_one_that_ran(self):
        walk = self.walk_to_rebuild()
        units = walk.facts["units"]
        units[rb.REBUILD_UNIT] = unit("ended", 1)
        step = walk.step("rebuild")
        self.assertIn("sudo tail -n 20 /var/lib/seraphina-snapraid/rebuild/rebuild.out", step["check"]["words"])
        self.assertEqual([b["id"] for b in step["blocks"]], ["5a", "5b", "5c"])
        self.assertEqual(step["blocks"][2]["commands"][:2],
                         ["sudo systemctl stop seraphina-disk-rebuild.service",
                          "sudo systemctl reset-failed seraphina-disk-rebuild.service"])
        self.assertEqual(rb.unit_state(unit("ended", 203)), ("stopped", None), "203: systemd could not run it")
        self.assertEqual(rb.unit_state(unit("ended", 1)), ("ended", 1))

    def test_a_check_started_before_the_rebuild_ended_does_not_count(self):
        """5a then 5b pasted at once: the check stops at once on SnapRAID's lock, and says nothing."""
        walk = self.walk_to_rebuild()
        units = walk.facts["units"]
        units[rb.REBUILD_UNIT], units[rb.CHECK_UNIT] = unit("ended", 0, at=10), unit("ended", 1, at=5)
        step = walk.step("rebuild")
        self.assertEqual((step["check"]["ok"], step["check"]["code"]), (False, "check_too_soon"))
        self.assertEqual(step["blocks"][2]["commands"][0], "sudo systemctl stop seraphina-disk-check.service")
        self.assertFalse(walk.go("rebuild")["done"])
        units[rb.CHECK_UNIT] = unit("ended", 0, at=11)        # 10 + 1: the very second the rebuild exited
        self.assertTrue(walk.go("rebuild")["done"])
        self.assertEqual(walk.record["results"]["rebuild"], {"fix": 0, "check": 0})

    def test_a_state_seraphina_cannot_read_needs_the_owners_tick_and_records_no_result(self):
        for change, code in ((lambda units: units.update({rb.REBUILD_UNIT: None}), "unit_unknown"),
                             (lambda units: (rebuilt({"units": units}),
                                             units[rb.CHECK_UNIT].pop("ExecMainStartTimestampMonotonic")),
                              "order_unknown")):
            walk = self.walk_to_rebuild()
            change(walk.facts["units"])
            result = walk.go("rebuild")
            self.assertEqual((result["check"]["ok"], result["check"]["code"], result["done"], result["missing"]),
                             (None, code, False, ["rebuild_self"]))
            self.assertTrue(walk.go("rebuild", ["rebuild_self"])["done"])
            self.assertNotIn("rebuild", walk.record["results"], "no exit codes to keep")
            lost = walk.step("lost")
            self.assertEqual(lost["what"][0], rb.WHAT["lost_unknown"])
            self.assertEqual([b["id"] for b in lost["blocks"]], ["6a", "6b", "6c"])

    def test_units_step_4_could_not_read_are_not_taken_for_this_rebuilds(self):
        """systemctl silent at step 4's check (it passes: what it proves is the reader's): nothing proved that no
        rebuild from an earlier checklist was left, so the end step 5 reads once systemctl answers again may be that
        one's, on a disk no rebuild ever ran on. It needs the owner's tick, keeps no exit codes, and step 6 says it
        cannot tell; unless step 5 read both units gone first."""
        for silent in ((rb.REBUILD_UNIT, rb.CHECK_UNIT), (rb.CHECK_UNIT,)):
            walk = self.walk_to_rebuild(silent=silent)
            self.assertIs(walk.record.get("units_unread"), True, silent)
            rebuilt(walk.facts)       # systemctl answers again: R-1's units, both ended 0, the check after (same boot)
            result = walk.go("rebuild")
            self.assertEqual((result["check"]["ok"], result["check"]["code"], result["done"], result["missing"]),
                             (None, "units_unproven", False, ["rebuild_self"]), silent)
            self.assertIn("she cannot tell that these are the ones you started", result["check"]["words"])
            self.assertEqual(walk.step("rebuild")["check"]["code"], "units_unproven")
            self.assertTrue(walk.go("rebuild", ["rebuild_self"])["done"])
            self.assertNotIn("rebuild", walk.record["results"], "no exit codes kept for units that may not be its own")
            lost = walk.step("lost")
            self.assertEqual(lost["what"][0], rb.WHAT["lost_unknown"])
            self.assertEqual([b["id"] for b in lost["blocks"]], ["6a", "6b", "6c"])
        walk = self.walk_to_rebuild(silent=(rb.REBUILD_UNIT, rb.CHECK_UNIT))
        units = walk.facts["units"]
        units[rb.REBUILD_UNIT], units[rb.CHECK_UNIT] = unit("absent"), None
        self.assertFalse(walk.go("rebuild")["done"])
        self.assertIs(walk.record.get("units_unread"), True, "one still not read")
        units[rb.CHECK_UNIT] = unit("absent")
        self.assertEqual(walk.go("rebuild")["check"]["code"], "not_started")
        self.assertNotIn("units_unread", walk.record, "both read, and gone: what comes next is this checklist's")
        rebuilt(walk.facts)
        self.assertTrue(walk.go("rebuild")["done"])
        self.assertEqual(walk.record["results"]["rebuild"], {"fix": 0, "check": 0})
        self.assertNotIn("units_unread", self.walk_to_rebuild().record, "both read at step 4")

    def test_the_check_is_sent_to_a_block_on_the_page_when_the_rebuilds_is_hidden(self):
        """While the reader does not answer, the rebuild's own block (5a) is not shown: the words that send the owner
        to the check name its block by its title, which is on the page, not by a place that may not be there."""
        for error in (None, "The disk protection reader took too long."):
            walk = self.walk_to_rebuild()
            walk.facts["reader"] = {"error": error} if error else {}
            walk.facts["units"][rb.REBUILD_UNIT] = unit("ended", 0, at=10)
            step = walk.step("rebuild")
            self.assertEqual(step["check"]["code"], "check_not_started", error)
            self.assertEqual([b["id"] for b in step["blocks"]], ["5a", "5b"] if error is None else ["5b"])
            titles = [b["title"] for b in step["blocks"] if b["id"] == "5b"]
            self.assertIn(f'"{titles[0]}"', step["check"]["words"])
            self.assertNotRegex(step["check"]["words"], r"\b(first|second|last)\b")

    def test_no_rebuild_block_before_the_new_disk_is_known_mounted(self):
        """Step 5 checks the mount again: the rebuild writes to whatever is at the folder, the system disk too."""
        walk = self.walk_to_rebuild()
        walk.facts["info"]["mounts"]["d2"]["mounted"] = False
        step = walk.step("rebuild")
        self.assertEqual((step["check"]["code"], step["blocks"]), ("disk_not_mounted", []))
        self.assertFalse(walk.go("rebuild")["done"])
        walk.facts["info"]["mounts"]["d2"]["mounted"] = True
        walk.facts["reader"] = {"error": "The disk protection reader took too long."}
        step = walk.step("rebuild")
        self.assertEqual((step["check"]["ok"], step["check"]["code"], step["blocks"], step["confirms"]),
                         (False, "reader_unknown", [], []))
        walk.facts["units"][rb.REBUILD_UNIT] = unit("running")
        step = walk.step("rebuild")
        self.assertEqual(step["check"]["code"], "rebuild_running", "the units are still read")
        self.assertEqual([b["id"] for b in step["blocks"]], ["5b"], "but no block that starts the rebuild")
        # the new disk dropped off (a cable): the rebuild ended with errors and the reader does not answer. What
        # comes next would start the rebuild again, so it waits for the reader, as a first start does
        walk.facts["info"]["mounts"]["d2"]["mounted"] = False
        walk.facts["units"][rb.REBUILD_UNIT] = unit("ended", 1)
        step = walk.step("rebuild")
        self.assertEqual((step["check"]["code"], step["blocks"]), ("reader_unknown", []))
        self.assertFalse(walk.go("rebuild")["done"])
        walk.facts["units"][rb.CHECK_UNIT] = unit("killed")       # a check to start again: that block only
        step = walk.step("rebuild")
        self.assertEqual((step["check"]["code"], [b["id"] for b in step["blocks"]]), ("check_stopped", ["5b", "5c"]))
        self.assertEqual(step["blocks"][1]["commands"][0], "sudo systemctl stop seraphina-disk-check.service")
        walk.facts["reader"] = {}
        self.assertEqual(walk.step("rebuild")["check"]["code"], "disk_not_mounted")
        walk.facts["info"]["mounts"]["d2"]["mounted"] = True
        walk.facts["units"][rb.CHECK_UNIT] = unit("absent")
        step = walk.step("rebuild")
        self.assertEqual((step["check"]["code"], [b["id"] for b in step["blocks"]]),
                         ("rebuild_failed", ["5a", "5b", "5c"]))
        walk.facts["units"][rb.REBUILD_UNIT] = unit("running")
        self.assertEqual([b["id"] for b in walk.step("rebuild")["blocks"]], ["5a", "5b"])

    def test_what_could_not_be_brought_back(self):
        """Only 0 and 0 say that every file came back: an error code cannot tell files left as .unrecoverable from
        a run that stopped at its start, so the page says it cannot tell and shows how each one ended."""
        for (fix, check), first in (((0, 0), "Every protected file on Disk 2 came back as it was at the last "
                                              "protection run"),
                                    ((1, 1), "Seraphina cannot tell whether every file came back"),
                                    ((0, 1), "Seraphina cannot tell whether every file came back")):
            walk = self.walk_to_rebuild()
            rebuilt(walk.facts, fix, check)
            self.assertTrue(walk.go("rebuild")["done"])
            lost = walk.step("lost")
            self.assertTrue(lost["what"][0].startswith(first), lost["what"][0])
            self.assertEqual("6c" in [b["id"] for b in lost["blocks"]], (fix, check) != (0, 0))
            self.assertNotIn("Nothing, when everything came back", json.dumps(lost["blocks"]))
            self.assertIn(rb.WHAT["lost_elsewhere"], lost["what"])
            self.assertRegex(lost["what"][1], r"^Files added to Disk 2 after the last protection run, .+ "
                                              r"\d\d:\d\d, are gone")
            self.assertEqual(lost["blocks"][0]["commands"], ["sudo find /mnt/disk2 -name '*.unrecoverable' "
                                                             "-printf '%P\\n'"])
            self.assertFalse(walk.go("lost")["done"])
            self.assertTrue(walk.go("lost", ["read"])["done"])

    def test_finish_on_the_daemons_route(self):
        walk = self.walk_to_rebuild()
        rebuilt(walk.facts)
        walk.go("rebuild")
        walk.go("lost", ["read"])
        done = walk.step("done")
        self.assertEqual([b["commands"][-1] for b in done["blocks"]],
                         ["sudo systemctl reset-failed seraphina-disk-rebuild.service seraphina-disk-check.service "
                          "2>/dev/null; true", "sudo systemctl enable --now snapraidd.service"])
        self.assertIn("The next protection run, tonight at 02:00, takes the new disk in.", " ".join(done["what"]))
        self.assertEqual(done["check"]["code"], "not_answering")
        walk.facts["daemon"].update(answering=True, health="degraded")
        self.assertEqual(walk.go("done", ["ready", "apps_back"])["check"]["code"], "still_missing")
        self.assertEqual(walk.record["state"], "open")
        walk.facts["daemon"].update(health="passed", disk_health={"d1": "passed", "d2": "passed"})
        self.assertTrue(walk.go("done")["done"])
        self.assertEqual(walk.record["state"], "finished")
        self.assertIsNone(rb.current(walk.record))
        self.assertIsNone(rb.light(walk.record))
        with self.assertRaisesRegex(ValueError, "closed"):
            walk.go("done")

    def test_finish_on_the_readers_route(self):
        walk = self.walk_to_rebuild("reader")
        rebuilt(walk.facts)
        walk.go("rebuild")
        walk.go("lost", ["read"])
        done = walk.step("done")
        self.assertEqual([b["id"] for b in done["blocks"]], ["7a"], "no service to turn on")
        self.assertIn(rb.WHAT["done_reader"], done["what"])
        self.assertTrue(walk.go("done", ["ready", "apps_back"])["done"])

    def test_unit_states(self):
        self.assertEqual(rb.unit_state(None), ("unknown", None))
        self.assertEqual(rb.unit_state(rb.unit_props("LoadState=not-found\nActiveState=inactive\n")), ("absent", None))
        self.assertEqual(rb.unit_state(unit("ended", 0)), ("ended", 0))
        self.assertEqual(rb.unit_state(unit("ended", 2)), ("ended", 2))
        self.assertEqual(rb.unit_state(unit("killed")), ("stopped", None))
        self.assertEqual(rb.unit_state(unit("running")), ("running", None))


class Reader:
    """The reader's info and disks answers for a panel (the client a HelperClient would be)."""

    def __init__(self) -> None:
        self.verbs: list[str] = []
        self.mounted = False

    def call(self, verb: str, timeout: float | None = None, **fields: Any) -> dict:
        self.verbs.append(verb)
        if verb == "info":
            mounts = {"d1": {"role": "data", "number": 1, "path": "/mnt/disk1", "mounted": True, "uuid_ok": True},
                      "d2": {"role": "data", "number": 2, "path": "/mnt/disk2", "mounted": self.mounted,
                             "uuid_ok": self.mounted},
                      "parity": {"role": "parity", "number": 1, "path": "/mnt/parity1", "mounted": True,
                                 "uuid_ok": True}}
            return {"ok": True, "snapraid": {"binary": "/usr/bin/snapraid", "version": "15.1"},
                    "helper_config": {"conf": "/etc/snapraid.conf", "mounts": {"d1": U1, "d2": U2, "parity": U3}},
                    "parsed": {"data": [{"name": "d1", "dir": "/mnt/disk1"}, {"name": "d2", "dir": "/mnt/disk2"}]},
                    "mounts": mounts, "schedulers": {"found": []}, "snapraid_running": False, "set_up": True,
                    "conf_ok": True}
        if verb == "disks":
            return {"ok": True, "disks": [{"name": "d2", "role": "data", "model": "Example Disk 8TB",
                                           "serial": SERIAL, "by_id": OLD_ID, "present": False}],
                    "spare": [{"device": "/dev/sdx", "by_id": NEW_ID, "size": 8_001_000_000_000,
                               "model": "Example Disk 8TB", "serial": "SN-EXAMPLE-NEW1", "signature": False}]}
        raise AssertionError(f"the checklist asked the reader for {verb}")


class PanelTests(unittest.TestCase):
    """The checklist inside a panel on the daemon route: the mock daemon through the real link."""

    def setUp(self) -> None:
        from tests.mock_snapraidd import MockDaemon  # noqa: F401  (the rig below starts it)
        from tests.snapraidd_rig import LinkRig
        from tests.test_snapraidd import FakeNotifier
        from swfactory.housekeeping import HousekeepingManager

        self.rig = LinkRig(("read", "act", "accept"))
        self.addCleanup(self.rig.close)
        self.daemon = self.rig.daemon
        self.daemon.config["maintenance_schedule"] = "02:00"
        self.tmp = Path(tempfile.mkdtemp(prefix="sfrebuild-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "fstab").write_text(FSTAB, encoding="utf-8")
        (self.tmp / "proc").mkdir()
        self.config = Config(self.tmp / "config.json", {"data_dir": str(self.tmp / "data"),
                                                        "harness": {"sandbox": "bwrap"},
                                                        "snapraid": {"enabled": True}})
        self.bus = EventBus()
        self.events = self.bus.subscribe()
        self.notifier = FakeNotifier()
        self.system = HousekeepingManager(self.config.data_dir, self.bus)
        self.reader = Reader()
        self.clock_offset = 0.0
        self.panel = snapraid.SnapraidPanel(
            self.config, self.system, self.notifier, self.bus, client=self.reader,
            local=lambda: {"binary": "/usr/bin/snapraid", "version": "15.1"}, clock=lambda: time.time() +
            self.clock_offset, metrics=snapraidd.MetricsReader(self.daemon.url + "/metrics"),
            link=snapraidd.LinkClient("/nonexistent", connector=self.rig.connector()), quiet=lambda cfg: None,
            proc_root=str(self.tmp))
        self.units = {rb.DAEMON_UNIT: unit("on"), rb.REBUILD_UNIT: unit("absent"), rb.CHECK_UNIT: unit("absent")}
        self.shown: list[str] = []

        def show(name: str) -> dict | None:
            self.shown.append(name)
            return self.units.get(name)
        self.panel.rebuild = rb.Checklist(self.panel, systemctl=show, fstab=str(self.tmp / "fstab"),
                                          proc=str(self.tmp / "proc"), stacks=str(self.tmp / "stacks"))
        facts = self.panel.facts()
        facts["info"] = {"at": "2026-10-25T15:00:00Z", "schedulers": {"found": []},
                         "mounts": {"d1": {"role": "data", "mounted": True, "uuid_ok": True}}}
        facts["info_tried"] = snapraid._iso(time.time())
        self.panel._save(facts)
        self.poll()

    def poll(self) -> None:
        d = self.panel.daemon.facts()
        d.pop("metrics_tried", None)
        self.panel.daemon.save(d)
        self.panel.daemon.tick()

    def test_the_light_the_card_the_down_notice_and_the_journal(self):
        self.assertEqual(self.panel.view()["rebuild"], None)
        page = self.panel.rebuild.page()
        self.assertFalse(page["open"])
        self.assertEqual([row["name"] for row in page["choices"] if row["suggested"]], ["d2"])
        page = self.panel.rebuild.start("d2")
        with self.assertRaisesRegex(ValueError, "already open"):
            self.panel.rebuild.start("d1")
        self.assertEqual(page["current"], "stop")
        light = self.panel.view_light()
        self.assertEqual((light["tone"], light["code"], light["reason"]),
                         ("err", "rebuild", "Disk 2 is being replaced: follow the checklist to the end."))
        view = self.panel.view()
        self.assertEqual({k: view["rebuild"][k] for k in ("state", "label", "step", "n", "of")},
                         {"state": "open", "label": "Disk 2", "step": "stop", "n": 1, "of": 7})
        self.assertIn("A disk is being replaced: Disk 2, step 1 of 7 (Stop changes)", "\n".join(snapraid.card_lines(view)))
        self.assertEqual(self.panel.rebuild.step("stop", ["apps"])["result"]["code"], "service_up")
        self.daemon.kill()                               # the owner's block: the service off for good
        self.units[rb.DAEMON_UNIT] = unit("off")
        result = self.panel.rebuild.step("stop")["result"]
        self.assertTrue(result["done"], result)
        self.clock_offset = 3 * 3600
        self.poll()
        self.poll()
        self.assertNotIn("parity.down", self.notifier.kinds(), "the owner turned it off on purpose")
        self.assertEqual(set(self.shown), {rb.DAEMON_UNIT, rb.REBUILD_UNIT, rb.CHECK_UNIT}, "only systemctl show")
        self.assertEqual(set(self.reader.verbs), {"info", "disks"}, "the reader runs no SnapRAID command")
        self.assertFalse([r for r in self.daemon.requests if r["method"] != "GET"], "nothing sent to the service")
        types = [event.type for event in self.drain()]
        self.assertIn("snapraid.rebuild", types)
        journal = "".join(p.read_text(encoding="utf-8") for p in (self.tmp / "data" / "snapraid").glob("journal-*"))
        self.assertIn('"kind": "rebuild"', journal)
        kept = journal + (self.tmp / "data" / "snapraid" / rb.FILE).read_text(encoding="utf-8")
        for secret in (SERIAL, OLD_ID, NEW_ID, "SN-EXAMPLE-NEW1", "Example Disk"):
            self.assertNotIn(secret, kept)
        self.assertEqual(oct((self.tmp / "data" / "snapraid" / rb.FILE).stat().st_mode & 0o777), "0o600")
        closed = self.panel.rebuild.close()
        self.assertFalse(closed["open"])
        self.assertNotEqual(self.panel.view_light()["code"], "rebuild")
        self.clock_offset = 4 * 3600
        self.poll()
        self.assertIn("parity.down", self.notifier.kinds(), "once the checklist is closed, silence is an alarm again")

    def test_no_run_is_offered_or_sent_while_a_disk_is_being_replaced_but_the_skip_is(self):
        """§9.1: no run is started while a restore is open; skipping the next run and stopping one stay."""
        self.assertTrue(self.panel.view()["daemon"]["offers"]["undelete"])
        self.panel.rebuild.start("d2")
        offers = self.panel.view()["daemon"]["offers"]
        self.assertFalse(offers["maintenance"] or offers["undelete"] or offers["heal"] or offers["accept"])
        self.assertTrue(offers["hold_off"])
        for action in ("maintenance", "heal"):
            with self.assertRaisesRegex(ValueError, "A disk is being replaced: finish its checklist first"):
                self.panel.daemon.preview(action)
        preview = self.panel.daemon.preview("hold_off", enabled=True)
        self.assertEqual(preview["action"], "hold_off")
        self.panel.rebuild.close()
        self.assertTrue(self.panel.view()["daemon"]["offers"]["undelete"])

    def test_prepare_through_the_panel(self):
        self.panel.rebuild.start("d2")
        record = self.panel.rebuild.open_one()
        for step in ("stop", "name", "boot"):
            record["done"][step] = time.time()
        self.panel.rebuild._save(record)
        answer = self.panel.rebuild.prepare(NEW_ID, "NEW1")
        self.assertEqual([b["id"] for b in answer["blocks"]], ["4c", "4d", "4e"])
        self.assertNotIn("SN-EXAMPLE-NEW1", (self.tmp / "data" / "snapraid" / rb.FILE).read_text(encoding="utf-8"))

    def test_doctor_line(self):
        make = lambda name, ok, level, detail: {"name": name, "ok": ok, "level": level, "detail": detail}  # noqa: E731
        self.assertEqual(rb.doctor_lines(self.config, make), [])
        self.panel.rebuild.start("d2")
        lines = rb.doctor_lines(self.config, make)
        self.assertEqual((lines[0]["level"], lines[0]["name"]), ("warn", "snapraid disk checklist"))
        self.assertIn("Disk 2 is being replaced: the checklist in Backups is at step 1 of 7 (stop changes)",
                      lines[0]["detail"])

    def drain(self) -> list:
        out = []
        while True:
            try:
                out.append(self.events.get_nowait())
            except Exception:  # noqa: BLE001 - empty
                return out


class WordTests(unittest.TestCase):
    """U7: no internal term, and none of the service's own words, in anything a person reads here."""

    def test_novice_words(self):
        import re

        for text in rb.novice_strings():
            words = " ".join(w for w in text.split() if "/" not in w and not w.startswith("{"))
            self.assertEqual(snapraid.internal_terms(words), [], text)
            for term in ("daemon", "threshold", "hold off", "hold-off", "undelete", "heal", "engine"):
                self.assertIsNone(re.search(rf"\b{term}\b", words.lower()), text)
        self.assertIn(rb.TITLE, snapraid.novice_strings())

    def test_the_page_words_built_from_facts(self):
        walk = Walk()
        stopped(walk.facts)
        texts = []
        for step in rb.STEPS:
            entry = walk.step(step)
            texts += entry["what"] + entry["facts"] + [entry["title"], (entry["check"] or {}).get("words") or ""]
            texts += [c["words"] for c in entry["confirms"]]
            texts += [b.get(k) or "" for b in entry["blocks"] for k in ("title", "expect", "note")]
            if step == "disk":
                fitted(walk.facts)
            if step == "rebuild":
                rebuilt(walk.facts, 1, 1)
            walk.go(step, [c["item"] for c in entry["confirms"]]) if step != "boot" else \
                walk.go(step, ["boot"] if entry["check"]["ok"] is None else None)
            if step == "boot" and "boot" not in walk.record["done"]:
                walk.facts["fstab"] = run_sed(rb.comment_out_sed("/mnt/disk2"), FSTAB)
                walk.go("boot")
        for text in texts:
            words = " ".join(w for w in text.split() if "/" not in w and not w.startswith(("{", "$", "-")))
            self.assertEqual(snapraid.internal_terms(words), [], text)


class RouteAndCliTests(unittest.TestCase):
    def test_the_routes(self):
        from swfactory import server
        from tests.test_server import ServerTestCase

        case = ServerTestCase("run")
        case.setUp()
        self.addCleanup(case.doCleanups)
        factory: Any = case.factory
        calls = []
        factory.snapraid_rebuild = lambda: {"open": False}
        factory.snapraid_rebuild_start = lambda disk: calls.append(("start", disk)) or {"open": True}
        factory.snapraid_rebuild_step = lambda step, confirm: calls.append(("step", step, confirm)) or {"open": True}
        factory.snapraid_rebuild_prepare = lambda by_id, typed: calls.append(("prepare", by_id, typed)) or {}
        factory.snapraid_rebuild_close = lambda: calls.append(("close",)) or {"open": False}
        self.assertEqual(case.request("GET", "/api/snapraid/rebuild").status, 200)
        self.assertEqual(case.request("POST", "/api/snapraid/rebuild", {"disk": "d2"}).status, 201)
        self.assertEqual(case.request("POST", "/api/snapraid/rebuild", {"disk": 2}).status, 400)
        self.assertEqual(case.request("POST", "/api/snapraid/rebuild/step", {"step": "stop", "confirm": ["apps"]})
                         .status, 200)
        self.assertEqual(case.request("POST", "/api/snapraid/rebuild/step", {"step": "stop", "confirm": "apps"})
                         .status, 400)
        self.assertEqual(case.request("POST", "/api/snapraid/rebuild/prepare", {"by_id": NEW_ID, "typed": "NEW1"})
                         .status, 200)
        self.assertEqual(case.request("POST", "/api/snapraid/rebuild/prepare", {"by_id": NEW_ID}).status, 400)
        self.assertEqual(case.request("POST", "/api/snapraid/rebuild/close", {}).status, 200)
        self.assertEqual(case.request("POST", "/api/snapraid/rebuild/run", {}).status, 404)
        self.assertEqual(calls, [("start", "d2"), ("step", "stop", ["apps"]), ("prepare", NEW_ID, "NEW1"), ("close",)])
        handlers = [handler for _, pattern, handler in server._ROUTES if "rebuild" in pattern.pattern]
        self.assertEqual(len(handlers), 3)
        self.assertFalse(set(handlers) & server.AGENT_TOKEN_ROUTES)

    def test_the_cli_prints_the_current_step_and_its_blocks(self):
        from swfactory import __main__ as cli

        walk = Walk()
        page = walk.page()

        class Client:
            def __init__(self, config: Any) -> None:
                pass

            def get(self, path: str) -> dict:
                assert path == "/api/snapraid/rebuild", path
                return copy.deepcopy(page)
        out = io.StringIO()
        with mock.patch.object(cli, "Client", Client), mock.patch.object(cli, "load_config", lambda: None), \
                redirect_stdout(out), redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["snapraid", "rebuild"]), 0)
        text = out.getvalue()
        self.assertIn("Replace a failed disk: Disk 2 (d2) at /mnt/disk2", text)
        self.assertIn("[>] 1. Stop changes", text)
        self.assertIn("sudo systemctl disable --now snapraidd.service", text)
        self.assertIn("[ ] 5. Rebuild the disk", text)
        self.assertIn(rb.LAB_LINE, text)


class LabListTests(unittest.TestCase):
    def test_l13_and_l20_are_listed_as_not_checked_on_both_routes(self):
        self.assertIn("L20", [c["id"] for c in snapraid.LAB_CHECKS])
        self.assertTrue({"L13", "L20"} <= {c["id"] for c in snapraidd.LAB_CHECKS})
        self.assertIn("L20, the checklist for a failed disk works end to end", snapraid.LAB_WORDS)
        self.assertIn("L13, the server still starts when a disk has failed", snapraid.DAEMON_LAB_WORDS)


if __name__ == "__main__":
    unittest.main()
