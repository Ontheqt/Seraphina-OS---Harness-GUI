"""Tests for swfactory/snapraid.py, the disk protection panel's harness side, phase P1 (DESIGN.md §41;
docs/seraphina/BACKUPS_SNAPRAID.md §4, §8, §11, §12, §13.2): the HelperClient over an injectable runner, the light
(T4, T5), the facts cache and its privacy (T21), the empty states (T22), the doctor (T23), notices and tracker items
with counts only (T15, T16's health part), the settings, the routes and the CLI, and U7's word check. The panel runs
against the real helper (bin/sf-snapraid) in a temporary tree (tests/snapraid_rig.py), called in-process instead of
through sudo.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path
from typing import Any, ClassVar

from swfactory import settings, snapraid, snapraidd
from swfactory.config import Config, sandbox_masks
from swfactory.events import EventBus
from swfactory.housekeeping import HousekeepingManager
from swfactory.notify import (
    PARITY_KINDS,
    RULE_IDS,
    RULE_ROUTES,
    Notice,
    filter_text,
    operational_text,
)
from tests.snapraid_rig import FIXTURES, HEALTHY, Rig, sf
from tests.test_notify import NotifyCase
from tests.test_server import ServerTestCase

ROOT = Path(__file__).resolve().parent.parent
NOW = 1792930000.0          # 2026-10-25 12:06 UTC, a little after the fixtures' runs
HOSTILE = ("<img src=x onerror=alert(1)>", "a:b.txt", "back\\slash", "line\nbreak", "invoice\u202egpj.exe",
           "img1.jpg", "readme", "big.bin")
LOCAL = {"binary": "/usr/bin/snapraid", "version": "13.0", "configs": {"/etc/snapraid.conf": True},
         "readable_disks": 3}


def no_daemon(url: str) -> tuple[int, bytes]:
    """A /metrics fetcher with no SnapRAID Daemon behind it (the reader's tests stay off the network)."""
    raise ConnectionRefusedError(url)


NO_DAEMON = snapraidd.MetricsReader(fetcher=no_daemon)


class InProcess:
    """A runner that hands the request to bin/sf-snapraid's main() in the rig, as sudo would, and records it."""

    def __init__(self, rig: Rig) -> None:
        self.rig = rig
        self.calls: list[dict] = []
        self.down: str | None = None   # "sudo" or "timeout": the call never reaches the helper

    def __call__(self, argv: list[str], data: bytes, timeout: float) -> tuple[int | None, bytes, bytes]:
        self.calls.append(json.loads(data))
        if self.down == "sudo":
            return 1, b"", b"sudo: a password is required\n"
        if self.down == "timeout":
            return None, b"", b"(timed out)"
        out = io.StringIO()
        code = sf.main([], stdin=io.BytesIO(data), stdout=out, paths=self.rig.paths, system=self.rig.system,
                       lstat=self.rig.lstat)
        return code, out.getvalue().encode(), b""

    def verbs(self) -> list[str]:
        return [call["verb"] for call in self.calls]


class FakeNotifier:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict, str]] = []
        self.cleared: list[tuple[str, str]] = []

    def parity_event(self, kind: str, fields: dict, key: str = "-") -> dict:
        self.events.append((kind, dict(fields), key))
        return {"message": "M-000001"}

    def parity_clear(self, kind: str, key: str = "-") -> bool:
        self.cleared.append((kind, key))
        return True


class Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class PanelCase(unittest.TestCase):
    commands: dict = HEALTHY
    sandbox = "bwrap"

    def setUp(self) -> None:
        self.rig = Rig(dict(self.commands))
        self.addCleanup(self.rig.close)
        self.tmp = Path(tempfile.mkdtemp(prefix="srpanel-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config = self.make_config()
        self.bus = EventBus()
        self.events = self.bus.subscribe()
        self.system = HousekeepingManager(self.config.data_dir, self.bus)
        self.notifier = FakeNotifier()
        self.runner = InProcess(self.rig)
        self.clock = Clock(time.time())    # the helper in the rig keeps real time
        # quiet: no wait applies. The real quiet_reason reads this machine's /proc, where another test's fake
        # snapraid (the rig's /bin/sh wrapper is named snapraid) may still be running; test_quiet_reasons covers it.
        self.panel = snapraid.SnapraidPanel(self.config, self.system, self.notifier, self.bus,
                                            client=snapraid.HelperClient(runner=self.runner),
                                            local=lambda: dict(LOCAL), clock=self.clock, metrics=NO_DAEMON,
                                            quiet=lambda cfg: None)

    def make_config(self, **snap: Any) -> Config:
        raw = {"data_dir": str(self.tmp / "data"), "harness": {"sandbox": self.sandbox},
               "wakes": {"timezone": "Europe/London"},
               "snapraid": {"enabled": True, **snap}}
        return Config(self.tmp / "config.json", raw)

    def items(self) -> list[dict]:
        store = self.system.store()
        return store.ledger().items if store.path.is_file() else []

    def drain(self) -> list:
        found = []
        while not self.events.empty():
            found.append(self.events.get_nowait())
        return found


class HelperClientTests(unittest.TestCase):
    def client(self, code, out=b"", err=b"", status_path="/nonexistent"):
        calls = []

        def runner(argv, data, timeout):
            calls.append((argv, data, timeout))
            return code, out, err
        return snapraid.HelperClient(runner=runner, status_path=status_path), calls

    def test_one_request_line_through_sudo_n(self):
        client, calls = self.client(0, b'{"ok": true, "verb": "status"}\n')
        self.assertEqual(client.call("status")["verb"], "status")
        argv, data, timeout = calls[0]
        self.assertEqual(argv, ["sudo", "-n", snapraid.HELPER])
        self.assertEqual(json.loads(data), {"v": 1, "verb": "status"})
        self.assertTrue(data.endswith(b"\n") and data.count(b"\n") == 1)
        self.assertEqual(timeout, snapraid.TIMEOUTS["status"])

    def test_refusals_and_failures(self):
        client, _ = self.client(2, b'{"ok": false, "error": "unsafe_conf", "words": "line 3: no."}\n')
        with self.assertRaises(snapraid.HelperError) as caught:
            client.call("status")
        self.assertEqual((caught.exception.code, caught.exception.words), ("unsafe_conf", "line 3: no."))
        client, _ = self.client(1, b"", b"sudo: a password is required\n")
        with self.assertRaises(snapraid.HelperError) as caught:
            client.call("info")
        self.assertEqual(caught.exception.code, "sudo_refused")
        self.assertIn("sudoers line", caught.exception.words)
        client, _ = self.client(None, b"", b"")
        with self.assertRaises(snapraid.HelperError) as caught:
            client.call("guard")
        self.assertEqual(caught.exception.code, "timeout")
        client, _ = self.client(0, b"not json")
        with self.assertRaises(snapraid.HelperError) as caught:
            client.call("status")
        self.assertEqual(caught.exception.code, "unreadable")

        def missing(argv, data, timeout):
            raise FileNotFoundError("sudo")
        with self.assertRaises(snapraid.HelperError) as caught:
            snapraid.HelperClient(runner=missing).call("info")
        self.assertEqual(caught.exception.code, "no_sudo")

    def test_no_new_privs_is_named_as_the_cause(self):
        status = Path(tempfile.mkdtemp()) / "status"
        self.addCleanup(shutil.rmtree, status.parent, True)
        status.write_text("Name:\tpython3\nNoNewPrivs:\t1\nSeccomp:\t0\n")
        self.assertTrue(snapraid.no_new_privs(str(status)))
        client, _ = self.client(1, b"", b"sudo: effective uid is not 0, is /usr/bin/sudo on a file system with the "
                                         b"'nosuid' option set or an NFS file system without root privileges?\n",
                                status_path=str(status))
        with self.assertRaises(snapraid.HelperError) as caught:
            client.call("info")
        self.assertIn("NoNewPrivs", caught.exception.words)
        self.assertIn("own system unit (systemd/factory@.service)", caught.exception.words)
        self.assertNotIn("user unit", caught.exception.words)
        status.write_text("NoNewPrivs:\t0\n")
        self.assertFalse(snapraid.no_new_privs(str(status)))

    def test_sudo_rs_and_classic_sudo_refusals_read_alike(self):
        """Ubuntu 25.10 and later ship sudo-rs as sudo and classic sudo as sudo.ws: either one's refusal is a sudo
        refusal, and under no-new-privileges the cause is named whichever sudo said it."""
        status = Path(tempfile.mkdtemp()) / "status"
        self.addCleanup(shutil.rmtree, status.parent, True)
        refusals = [b"sudo: interactive authentication is required\n",   # sudo-rs 0.2.13 (checked on a server)
                    b'sudo: The "no new privileges" flag is set, which prevents sudo from running as root.\n',   # classic
                    b"sudo: sudo must be owned by uid 0 and have the setuid bit set\n"]   # sudo-rs under NNP (judgement)
        for words in refusals:
            for nnp in ("0", "1"):
                with self.subTest(words=words, nnp=nnp):
                    status.write_text(f"NoNewPrivs:\t{nnp}\n")
                    client, _ = self.client(1, b"", words, status_path=str(status))
                    with self.assertRaises(snapraid.HelperError) as caught:
                        client.call("info")
                    self.assertEqual(caught.exception.code, "sudo_refused")
                    self.assertIn("NoNewPrivs" if nnp == "1" else "sudoers line", caught.exception.words)

    def test_the_default_runner_never_kills(self):
        code, out, _err = snapraid.run_command(["/bin/sh", "-c", "trap 'exit 130' INT; sleep 5"], b"", 0.3)
        self.assertIsNone(code)
        code, out, _ = snapraid.run_command(["/bin/cat"], b"hello\n", 5)
        self.assertEqual((code, out), (0, b"hello\n"))


class LightTests(unittest.TestCase):
    """The light of section 2.2 (T4, T5): the first rule that matches; red and amber stand on old facts with a note;
    grey only where it would be green or unknown; never green on stale facts."""

    cfg: ClassVar[dict] = {"stale_minutes": 180, "light": {"amber_after_hours": 36, "red_after_days": 7,
                                                           "fp_amber_percent": 20, "smart_unreadable_ok": []}}

    def facts(self, **status: Any) -> dict:
        state = {"array_empty": False, "has_unsynced": 0, "has_bad": {"count": 0}, "newest_block": NOW - 3600,
                 "oldest_scrubbed": NOW - 86400, "unscrubbed_blocks": 0, "histogram": [], "parity_block_free_min": 800,
                 "parity": {"parity": {"total": 900, "free": 800}}, "block_size": 65536}
        state.update(status.pop("state", {}))
        mounts = {"d1": {"role": "data", "mounted": True, "uuid_ok": True},
                  "d2": {"role": "data", "mounted": True, "uuid_ok": True},
                  "parity": {"role": "parity", "mounted": True, "uuid_ok": True}}
        mounts.update(status.pop("mounts", {}))
        facts = {"status": {"at": snapraid._iso(NOW - 60), "facts": state, "mounts": mounts,
                            "numbers": {"d1": 1, "d2": 2}, "content": status.pop("content", {})},
                 "smart": {"at": snapraid._iso(status.pop("smart_at", NOW - 120)),
                           "disks": status.pop("smart", {"d1": {"health": "ok"}, "d2": {"health": "ok"},
                                                         "parity": {"health": "ok"}})},
                 "guard": status.pop("guard", {"at": snapraid._iso(NOW - 30), "verdict": "nothing_to_do",
                                               "counts": {"array": {"changed": 0}}})}
        facts.update(status)
        return facts

    def light(self, facts, cfg=None, **kw):
        return snapraid.light(facts, cfg or self.cfg, NOW, **kw)

    def test_green_only_on_fresh_facts(self):
        lit = self.light(self.facts())
        self.assertEqual((lit["tone"], lit["code"]), ("ok", "ok"))
        self.assertRegex(lit["reason"], r"^Protected up to this (morning|afternoon|evening) at \d\d:\d\d\.$")
        stale = self.facts()
        stale["status"]["at"] = snapraid._iso(NOW - 5 * 3600)
        lit = self.light(stale)
        self.assertEqual((lit["tone"], lit["reason"]), ("muted", "Last checked 5 hours ago."))
        self.assertTrue(lit["last_known"].startswith("unknown (last known: protected up to"))

    def test_red_rules(self):
        cases = {
            "bad_blocks": (self.facts(state={"has_bad": {"count": 1}}), "Hidden damage was found in 1 place."),
            "disk_missing": (self.facts(mounts={"d2": {"role": "data", "mounted": False}}),
                             "Disk 2 is not mounted where it should be."),
            "protection_missing": (self.facts(mounts={"parity": {"role": "parity", "mounted": False}}),
                                   "The protection disk is not mounted where it should be."),
            "disk_uuid_changed": (self.facts(mounts={"d1": {"role": "data", "mounted": True, "uuid_ok": False}}),
                                  "Disk 1 is mounted with a different filesystem than before."),
            "smart_fail": (self.facts(smart={"d1": {"health": "fail"}}), "Disk 1 says it is failing. Replace it soon."),
            "content_problem": (self.facts(content={"problem": "x", "other": True}),
                                "The protection's file list has a problem."),
        }
        for code, (facts, words) in cases.items():
            with self.subTest(code):
                lit = self.light(facts)
                self.assertEqual((lit["tone"], lit["code"], lit["reason"]), ("err", code, words))

    def test_protection_disks_are_named_by_their_level(self):
        """Never "Disk ?" or "Disk with the protection": the protection disk, or protection disk 2 of two."""
        lit = self.light(self.facts(smart={"d1": {"health": "ok"}, "parity": {"health": "fail"}}))
        self.assertEqual(lit["reason"], "The protection disk says it is failing. Replace it soon.")
        lit = self.light(self.facts(mounts={"parity": {"role": "parity", "mounted": True, "uuid_ok": False}}))
        self.assertEqual(lit["reason"], "The protection disk is mounted with a different filesystem than before.")
        two = self.facts(mounts={"2-parity": {"role": "parity", "mounted": True, "uuid_ok": True}},
                         smart={"parity": {"health": "ok"}, "q-parity": {"health": "unknown"}})
        two["status"]["mounts"]["q-parity"] = two["status"]["mounts"].pop("2-parity")
        self.assertEqual(self.light(two)["reason"], "Protection disk 2's health can't be read.")
        two["smart"]["disks"]["q-parity"] = {"health": "ok", "probability": 0.3}
        self.assertEqual(self.light(two)["reason"], "Protection disk 2 is more likely than usual to fail this year (30%).")
        self.assertEqual(snapraid.disk_label("parity", {}, 2), "Protection disk 1")
        self.assertEqual((snapraid._health_key("q-parity", {}), snapraid._health_key("2-parity", {}),
                          snapraid._health_key("1-parity", {}), snapraid._health_key("z-parity", {})),
                         ("p2", "p2", "p1", "p3"))

    def test_red_and_amber_stand_on_old_facts_or_an_unreachable_reader(self):
        """The first rule that matches gives the light: red comes before grey. A dying disk that makes the reader
        time out stays red; the note says the facts are old or the reader did not answer."""
        stale_red = self.facts(mounts={"d1": {"role": "data", "mounted": False}})
        stale_red["status"]["at"] = snapraid._iso(NOW - 4 * 3600)
        lit = self.light(stale_red)
        self.assertEqual((lit["tone"], lit["code"], lit["note"]), ("err", "disk_missing", "Last checked 4 hours ago."))
        unreachable_red = self.facts(smart={"d1": {"health": "fail"}}, errors={"status": {
            "at": snapraid._iso(NOW), "code": "timeout", "words": "The reader did not answer within 240 seconds."}})
        lit = self.light(unreachable_red)
        self.assertEqual((lit["tone"], lit["code"]), ("err", "smart_fail"))
        self.assertIn("did not answer", lit["note"])
        stale_amber = self.facts(state={"has_unsynced": 3})
        stale_amber["status"]["at"] = snapraid._iso(NOW - 5 * 3600)
        lit = self.light(stale_amber)
        self.assertEqual((lit["tone"], lit["code"], lit["note"]), ("warn", "interrupted", "Last checked 5 hours ago."))

    def test_a_failed_status_keeps_the_last_facts_and_says_why(self):
        """A status that ends without SnapRAID's facts never reads as "Not protecting anything yet": a damaged file
        list is red (content_problem), anything else its own amber reason with the reader's words, and the last
        good facts are kept with their time."""
        rig = Rig({**HEALTHY, "status": {"fixture": "status-damaged", "exit": 1}})
        self.addCleanup(rig.close)
        _, damaged = rig.ask({"v": 1, "verb": "status"})
        good = self.facts()
        merged = snapraid.merge_status(good["status"], damaged, snapraid._iso(NOW))
        self.assertEqual(merged["facts"], good["status"]["facts"])
        self.assertEqual(merged["facts_at"], good["status"]["at"])
        self.assertEqual(merged["failed"]["outcome"], "failed")
        lit = self.light({**good, "status": {**merged, "at": snapraid._iso(NOW - 60)}})
        self.assertEqual((lit["tone"], lit["code"]), ("err", "content_problem"))
        first = snapraid.merge_status({}, damaged)
        self.assertEqual(self.light({"status": {**first, "at": snapraid._iso(NOW - 60)}})["code"], "content_problem")
        timeout = {**damaged, "outcome": "timeout", "words": "Took too long and was stopped",
                   "content": {"problem": None, "other": False}}
        merged = snapraid.merge_status(good["status"], timeout)
        lit = self.light({**good, "status": {**merged, "at": snapraid._iso(NOW - 60)}})
        self.assertEqual((lit["tone"], lit["code"], lit["reason"]),
                         ("warn", "status_failed", ("SnapRAID could not read its own state: took too long and was "
                                                    "stopped.")))
        never = snapraid.merge_status({}, {**timeout, "words": "Failed: Something else broke"})
        lit = self.light({"status": {**never, "at": snapraid._iso(NOW - 60)}})
        self.assertEqual((lit["code"], lit["reason"]),
                         ("status_failed", "SnapRAID could not read its own state: something else broke."))
        ok = snapraid.merge_status(merged, {**damaged, "outcome": "ok", "facts": {"array_empty": True}})
        self.assertIsNone(ok["failed"])
        self.assertEqual(snapraid.merge_status(good["status"], {"outcome": "busy"}), good["status"])

    def test_missing_mount_point_while_status_exits_zero_is_red(self):
        """T4: status never touches the data disks and exits 0; the helper's own mount check makes it red."""
        rig = Rig(dict(HEALTHY))
        self.addCleanup(rig.close)
        rig.system.unmounted = {"d3"}
        code, answer = rig.ask({"v": 1, "verb": "status"})
        self.assertEqual((code, answer["outcome"], answer["exit"]), (0, "ok", 0))
        facts = {"status": {**snapraid._status_summary(answer), "at": snapraid._iso(NOW - 60)}}
        lit = self.light(facts)
        self.assertEqual((lit["tone"], lit["reason"]), ("err", "Disk 3 is not mounted where it should be."))

    def test_bad_blocks_from_a_real_status_log(self):
        rig = Rig({**HEALTHY, "status": {"fixture": "status-bad", "exit": 0}})
        self.addCleanup(rig.close)
        _, answer = rig.ask({"v": 1, "verb": "status"})
        lit = self.light({"status": {**snapraid._status_summary(answer), "at": snapraid._iso(NOW - 60)}})
        self.assertEqual((lit["tone"], lit["code"]), ("err", "bad_blocks"))

    def test_age_rules_fire_without_a_diff(self):
        """T4: with no diff since the last run, files may have changed, so the age rules still fire."""
        old = self.facts(state={"newest_block": NOW - 2 * 86400}, guard={})
        self.assertEqual((self.light(old)["code"], self.light(old)["reason"]), ("old", "Protected up to 2 days ago."))
        older = self.facts(state={"newest_block": NOW - 9 * 86400}, guard={})
        self.assertEqual((self.light(older)["tone"], self.light(older)["reason"]),
                         ("err", "Nothing new has been protected for 9 days."))
        before = self.facts(state={"newest_block": NOW - 9 * 86400},
                            guard={"at": snapraid._iso(NOW - 10 * 86400), "verdict": "nothing_to_do"})
        self.assertEqual(self.light(before)["tone"], "err", "a diff older than the last run says nothing")
        unchanged = self.facts(state={"newest_block": NOW - 9 * 86400})
        self.assertEqual(self.light(unchanged)["tone"], "ok", "a fresh diff with no changes: nothing to protect")
        cfg = {**self.cfg, "light": {**self.cfg["light"], "red_after_days": 1}}
        self.assertEqual(self.light(self.facts(state={"newest_block": NOW - 1.5 * 86400}, guard={}), cfg)["reason"],
                         "Nothing new has been protected for 1 day.")

    def test_a_nothing_changed_report_ages(self):
        """A diff that found nothing counts only while it is fresh (stale_minutes): a month-old "nothing changed"
        says nothing about the files now, so the protection's age shows again."""
        month = self.facts(state={"newest_block": NOW - 30 * 86400},
                           guard={"at": snapraid._iso(NOW - 29 * 86400), "verdict": "nothing_to_do",
                                  "counts": {"array": {"changed": 0}}})
        lit = self.light(month)
        self.assertEqual((lit["tone"], lit["reason"]), ("err", "Nothing new has been protected for 30 days."))
        hours = self.facts(state={"newest_block": NOW - 2 * 86400},
                           guard={"at": snapraid._iso(NOW - 4 * 3600), "verdict": "nothing_to_do",
                                  "counts": {"array": {"changed": 0}}})
        self.assertEqual(self.light(hours)["code"], "old")
        hours["guard"]["at"] = snapraid._iso(NOW - 2 * 3600)
        self.assertEqual(self.light(hours)["code"], "ok")

    def test_amber_rules(self):
        cases = {
            "never_protected": self.facts(state={"array_empty": True}),
            "interrupted": self.facts(state={"has_unsynced": 12}),
            "check_overdue": self.facts(state={"oldest_scrubbed": NOW - 50 * 86400}),
            "health_unknown": self.facts(smart={"d1": {"health": "ok"}, "d2": {"health": "unknown"}}),
            "failure_likely": self.facts(smart={"d1": {"health": "ok", "probability": 0.24}}),
            "almost_full": self.facts(state={"parity_block_free_min": 20}),
            "list_room_low": self.facts(content={"space_low": True}),
            "health_old": self.facts(smart_at=NOW - 3 * 86400),
            "health_failed": self.facts(errors={"smart": {"at": snapraid._iso(NOW), "code": "timeout", "words": "x"}}),
        }
        for code, facts in cases.items():
            with self.subTest(code):
                lit = self.light(facts)
                self.assertEqual((lit["tone"], lit["code"]), ("warn", code), lit)
        self.assertEqual(self.light(self.facts(smart={"d1": {"health": "ok", "probability": 0.24}}))["reason"],
                         "Disk 1 is more likely than usual to fail this year (24%).")
        self.assertEqual(self.light(self.facts(), outside_open=True)["code"], "outside")
        self.assertEqual(self.light(cases["health_old"])["reason"], "The disks' health was last read 3 days ago.")
        self.assertEqual(self.light(cases["check_overdue"])["reason"],
                         "Part of the disks has not been checked for hidden damage for 50 days.")

    def test_old_or_failed_health_is_never_healthy_and_a_failing_disk_stays_red(self):
        """A SMART reading older than twice refresh.health_hours, or a newer failed read, is "health can't be read"
        (amber), and the card's words never say "healthy" on it; a disk that read failing stays red."""
        old = self.facts(smart_at=NOW - 3 * 86400)
        words = snapraid.fact_words(old, self.cfg, NOW)
        self.assertNotIn("All 3 disks are healthy.", words)
        self.assertIn("The disks' health was last read 3 days ago.", words)
        cfg = {**self.cfg, "refresh": {"health_hours": 48}}
        self.assertEqual(self.light(old, cfg)["code"], "ok", "within twice the refresh: fresh")
        failing = self.facts(smart_at=NOW - 9 * 86400, smart={"d1": {"health": "fail"}})
        self.assertEqual(self.light(failing)["code"], "smart_fail")

    def test_the_scrub_target_and_the_room_rule_follow_the_settings(self):
        overdue = self.facts(state={"oldest_scrubbed": NOW - 50 * 86400})
        cfg = {**self.cfg, "schedule": {"scrub": {"target_days": 60}}}
        self.assertEqual(self.light(overdue, cfg)["code"], "ok", "60 + 15 days not reached")
        cfg = {**self.cfg, "schedule": {"scrub": {"target_days": 7}}}
        self.assertEqual(self.light(self.facts(state={"oldest_scrubbed": NOW - 23 * 86400}), cfg)["code"],
                         "check_overdue")
        growing = self.facts(room=[[NOW - 10 * 86400, 1000], [NOW - 5 * 86400, 750], [NOW, 500]])
        growing["status"]["facts"].update(parity_block_free_min=500, parity={"parity": {"total": 2000}})
        self.assertAlmostEqual(snapraid.room_days(growing), 10.0)
        self.assertEqual(self.light(growing)["code"], "almost_full", "10 days of growth left, under 30")
        slow = self.facts(room=[[NOW - 10 * 86400, 520], [NOW, 500]])
        slow["status"]["facts"].update(parity_block_free_min=500, parity={"parity": {"total": 2000}})
        self.assertEqual(self.light(slow)["code"], "ok", "250 days left")
        self.assertIsNone(snapraid.room_days(self.facts(room=[[NOW - 3600, 600], [NOW, 500]])), "under a day")

    def test_the_cards_words(self):
        facts = self.facts(smart={"d1": {"health": "ok"}, "d2": {"health": "ok"}, "parity": {"health": "ok"}},
                           state={"oldest_scrubbed": NOW - 3600})
        words = snapraid.fact_words(facts, self.cfg, NOW)
        self.assertIn("All 3 disks are healthy.", words, "the protection disk counts")
        self.assertIn("Every part of the disks was checked for hidden damage in the last day.", words)
        self.assertIn("The protection disk has room for about 52 MB more data.", words)
        self.assertTrue(any(w.startswith("Since the last run, as checked just now: 0 added") for w in words), words)
        one = self.facts(smart={"d1": {"health": "ok"}})
        self.assertIn("The disk is healthy.", snapraid.fact_words(one, self.cfg, NOW))
        failing = self.facts(smart={"d1": {"health": "ok"}, "parity": {"health": "fail"}})
        self.assertIn("1 disk says it is failing.", snapraid.fact_words(failing, self.cfg, NOW))
        self.assertEqual(snapraid.size_words(1.234e12), "1.2 TB")
        self.assertEqual(snapraid.size_words(640e9), "640 GB")

    def test_unreadable_smart_is_amber_never_green(self):
        """T5: SMART "unsupported" gives health unknown and amber, until the owner accepts that disk."""
        rig = Rig(dict(HEALTHY))
        self.addCleanup(rig.close)
        _, smart = rig.ask({"v": 1, "verb": "smart"})
        self.assertTrue(smart["unsupported"])
        facts = self.facts(smart=snapraid._smart_summary(smart)["disks"])
        lit = self.light(facts)
        self.assertEqual((lit["tone"], lit["reason"]), ("warn", "Disk 1's health can't be read."))
        cfg = json.loads(json.dumps(self.cfg))
        cfg["light"]["smart_unreadable_ok"] = ["d1", "d2", "d3", "parity", "2-parity"]
        self.assertEqual(snapraid.light(facts, cfg, NOW)["tone"], "ok")

    def test_an_unreachable_reader_is_grey(self):
        facts = self.facts(errors={"status": {"at": snapraid._iso(NOW), "code": "sudo_refused",
                                              "words": "sudo would not run the reader."}})
        lit = self.light(facts)
        self.assertEqual((lit["tone"], lit["code"]), ("muted", "unreachable"))
        self.assertIn("sudo would not run the reader.", lit["reason"])
        self.assertTrue(lit["last_known"].startswith("unknown (last known: protected up to"))
        facts = {"errors": {"info": {"at": snapraid._iso(NOW), "code": "unsafe_install",
                                     "words": "/usr/bin/snapraid is not owned by root."}}}
        lit = self.light(facts)
        self.assertEqual((lit["tone"], lit["code"]), ("muted", "unsafe"))
        self.assertIn("/usr/bin/snapraid", lit["reason"])
        # an info error kept beside a status read that worked is not the reader's state (info runs no SnapRAID)
        facts = self.facts(errors={"info": {"at": snapraid._iso(NOW + 60), "code": "timeout",
                                            "words": "The reader did not answer."}})
        self.assertEqual(self.light(facts)["tone"], "ok")
        # a status error is the latest attempt's, whichever clock stamped it (cleared by the next read)
        facts = self.facts(errors={"status": {"at": snapraid._iso(NOW - 3600), "code": "sudo_refused",
                                              "words": "sudo would not run the reader."}})
        self.assertEqual(self.light(facts)["code"], "unreachable")

    def test_time_words(self):
        from zoneinfo import ZoneInfo

        zone = ZoneInfo("Europe/London")
        self.assertEqual(snapraid.when_words(NOW - 3 * 3600, NOW, zone), "this morning at 09:06")
        self.assertEqual(snapraid.when_words(NOW - 86400, NOW, zone), "yesterday at 13:06")
        self.assertEqual(snapraid.when_words(NOW - 3 * 86400, NOW, zone), "Thursday at 13:06")
        self.assertEqual(snapraid.when_words(NOW - 30 * 86400, NOW, zone), "25 Sep at 13:06")
        self.assertEqual(snapraid.age_words(5 * 3600), "5 hours ago")
        self.assertEqual(snapraid.age_words(60), "just now")
        lit = snapraid.light(self.facts(state={"newest_block": NOW - 3 * 86400}), self.cfg, NOW, zone=zone)
        self.assertEqual(lit["reason"], "Protected up to Thursday at 13:06.", "never \"up to on\"")


class PanelTests(PanelCase):
    def test_a_status_refresh_fills_the_card_with_counts_only(self):
        view = self.panel.refresh("status")
        self.assertEqual(self.runner.verbs()[:2], ["info", "status"])
        self.assertEqual(view["state"], "ready")
        self.assertEqual((view["phase"], view["route"]), ("P4", "reader"))
        self.assertEqual(view["undo_line"], snapraid.WATCHING)
        self.assertEqual(view["sentences"], list(snapraid.SENTENCES))
        self.assertEqual(view["light"]["tone"], "warn", view["light"])   # health not read yet → amber
        self.assertFalse(view["facts"]["stale"])
        self.assertEqual([row["number"] for row in view["disks"] if row["role"] == "data"], [1, 2, 3])
        self.assertTrue(view["facts"]["last_run"]["inferred"])
        self.assertIn("list", self.runner.verbs(), "the first refresh takes the file list once")
        self.assertEqual(view["allowed"], ["read"])
        self.assertEqual(view["schedule"], None)
        self.assertEqual([check["id"] for check in view["lab"]], ["L8", "L13", "L19", "L20"])
        self.assertTrue(all(check["status"] == "not_run" for check in view["lab"]))
        events = [event.type for event in self.drain()]
        self.assertIn("snapraid.updated", events)

    def test_health_opens_one_finding_per_disk_number_and_only_a_fresh_reading_closes_it(self):
        """T16, health part: findings open once per disk number (not per disk name), are never doubled by a
        repeat, survive an unreadable observation, and close only on a fresh, readable observation."""
        self.panel.refresh("status")
        self.panel.refresh("health")
        items = [item for item in self.items() if (item["source"].get("hash") or "").startswith("snapraid:health")]
        self.assertEqual(sorted(item["source"]["hash"] for item in items),
                         ["snapraid:health:1", "snapraid:health:2", "snapraid:health:3", "snapraid:health:p1",
                          "snapraid:health:p2"])
        self.assertEqual({item["kind"] for item in items}, {"finding"})
        self.assertEqual(items[0]["title"], "Disk 1's health can't be read")
        titles = {item["source"]["hash"]: item["title"] for item in items}
        self.assertEqual((titles["snapraid:health:p1"], titles["snapraid:health:p2"]),
                         ("Protection disk 1's health can't be read", "Protection disk 2's health can't be read"))
        self.panel.refresh("health")
        self.assertEqual(len([i for i in self.items() if i["source"].get("type") == "snapraid"]), 5, "no doubles")
        # a smart run that read nothing (SnapRAID failed) changes nothing
        failing = "version:13.0\nmsg:fatal: Failed to read the config\n"
        self.rig.set_commands({**HEALTHY, "smart": {"text": failing, "exit": 1}})
        self.panel.refresh("health")
        self.assertEqual(sum(1 for i in self.items() if i["status"] == "open"), 5)
        # disk 2 fails: its item is retitled and a disk.failing notice goes out once
        bad = ("version:13.0\nsmart:/dev/sdb:d2\nattr:/dev/sdb:d2:serial:SER-123\nattr:/dev/sdb:d2:model:Disk X\n"
               "attr:/dev/sdb:d2:flags:8:8\n")
        self.rig.set_commands({**HEALTHY, "smart": {"text": bad, "exit": 1}})
        self.panel.refresh("health")
        self.panel.refresh("health")
        item = next(i for i in self.items() if i["source"].get("hash") == "snapraid:health:2")
        self.assertEqual(item["title"], "Disk 2 says it is failing")
        self.assertEqual(self.notifier.events, [("disk.failing", {}, "disk-2")])
        # a fresh healthy reading closes it, by the factory, with the reader's run as evidence
        good = ("version:13.0\nsmart:/dev/sdb:d2\nattr:/dev/sdb:d2:serial:SER-123\nattr:/dev/sdb:d2:model:Disk X\n"
                "attr:/dev/sdb:d2:flags:0:0\n")
        self.rig.set_commands({**HEALTHY, "smart": {"text": good, "exit": 0}})
        self.panel.refresh("health")
        item = next(i for i in self.items() if i["source"].get("hash") == "snapraid:health:2")
        self.assertEqual((item["status"], item["verified_by"]), ("verified", "factory"))
        self.assertEqual(item["evidence"][-1]["kind"], "receipt")
        self.assertRegex(item["evidence"][-1]["ref"], r"^snapraid:J-\d+$")
        self.assertIn(("disk.failing", "disk-2"), self.notifier.cleared)
        text = json.dumps(self.items())
        for secret in ("SER-123", "Disk X", "/dev/sdb", '"d2"'):
            self.assertNotIn(secret, text, "no serial, model, device or SnapRAID name in the tracker")

    def test_an_accepted_unreadable_disk_opens_no_finding(self):
        self.config = self.make_config(light={"smart_unreadable_ok": ["d1", "d2", "d3", "parity", "2-parity"]})
        self.panel.config = self.config
        self.panel.refresh("status")
        self.panel.refresh("health")
        self.assertEqual([i for i in self.items() if i["source"].get("type") == "snapraid"], [])

    def test_a_guard_report_keeps_counts_and_codes_only(self):
        """T15 and T21: with hostile file names in the change list, no event, tracker item, journal line or file
        of <data_dir>/snapraid holds a file name; the cockpit reads the names from the helper, a page at a time."""
        self.rig.set_commands({**HEALTHY, "diff": {"text": (FIXTURES / "diff-mixed.log").read_text() +
                                                   "scan:add:d2:media/notes/<img src=x onerror=alert(1)>.jpg\n"
                                                   "scan:remove:d2:media/notes/invoice\u202egpj.exe\n"
                                                   "scan:remove:d2:media/notes/a\\db.txt\n"
                                                   "scan:remove:d2:media/notes/line\\nbreak.txt\n", "exit": 2}})
        self.panel.refresh("status")
        self.panel.refresh("diff")
        view = self.panel.view()
        guard = view["guard"]
        self.assertEqual(guard["verdict"], "would_hold")
        self.assertEqual(guard["counts"]["disks"]["d2"]["removed"], 3)
        page = self.panel.guard_page(guard["guard"], disk="d2")
        self.assertIn("media/notes/line\nbreak.txt", {entry["path"] for entry in page["page"]["entries"]})
        self.assertTrue(page["guard"]["runs"] and page["guard"]["runs"] == guard["runs"], "the report's runs, linked")
        self.panel.job(self.panel.jobs()["jobs"][0]["job"], log=True)
        stored = ""
        for path in sorted((Path(self.config.data_dir) / "snapraid").rglob("*")):
            if path.is_file():
                stored += path.read_text(encoding="utf-8")
                self.assertEqual(path.stat().st_mode & 0o077, 0, path)
            else:
                self.assertEqual(path.stat().st_mode & 0o077, 0, path)
        self.assertEqual((Path(self.config.data_dir) / "snapraid").stat().st_mode & 0o777, 0o700)
        published = json.dumps([event.data for event in self.drain()])
        tracker = json.dumps(self.items())
        for name in HOSTILE:
            for where, text in (("data dir", stored), ("events", published), ("tracker", tracker)):
                self.assertNotIn(name, text, f"{name!r} in {where}")
        guard_events = [event for event in self.bus.since(0) if event.type == "snapraid.guard"]
        self.assertEqual(guard_events[-1].data["verdict"], "would_hold")

    def test_a_run_outside_seraphina_opens_a_finding_unless_a_scheduler_was_found(self):
        self.panel.refresh("status")
        content = self.rig.root / "var/snapraid/snapraid.content"
        before = content.stat()
        content.write_bytes(b"CHANGED " * 100)
        os.utime(content, ns=(before.st_atime_ns, before.st_mtime_ns + 10 ** 9))
        view = self.panel.refresh("status")
        items = [i for i in self.items() if i["source"].get("hash") == "snapraid:outside"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "SnapRAID ran outside Seraphina")
        self.assertEqual(view["light"]["code"], "outside")
        self.assertEqual(len(view["outside"]), 1)
        journal = (Path(self.config.data_dir) / "snapraid").glob("journal-*.jsonl")
        self.assertIn("outside", "".join(path.read_text() for path in journal))
        self.assertIn("list", self.runner.verbs()[-1:] + self.runner.verbs(), "the index is taken again")

    def test_an_outside_run_opens_a_finding_even_with_a_known_scheduler(self):
        """Design section 8.5 makes no exception: a run some other scheduler started did not pass Seraphina's checks
        either (an fstab or mount-table mention once made fstrim.timer such a scheduler and hid every outside run).
        The finding says how many schedulers were found; a second run updates the open item, never a second one."""
        crontab = self.rig.root / "etc/crontab"
        crontab.write_text("30 3 * * * root /usr/bin/snapraid sync\n")
        self.panel.refresh("status")
        content = self.rig.root / "var/snapraid/snapraid.content"
        before = content.stat()
        content.write_bytes(b"CHANGED " * 100)
        os.utime(content, ns=(before.st_atime_ns, before.st_mtime_ns + 10 ** 9))
        view = self.panel.refresh("status")
        items = [i for i in self.items() if i["source"].get("hash") == "snapraid:outside"]
        self.assertEqual(len(items), 1)
        self.assertIn("1 other program on this machine starts SnapRAID on a timetable", items[0]["detail"])
        self.assertEqual(view["light"]["code"], "outside")
        self.assertEqual(view["schedulers"][0]["place"], "/etc/crontab")
        content.write_bytes(b"AGAIN!! " * 100)
        os.utime(content, ns=(before.st_atime_ns, before.st_mtime_ns + 2 * 10 ** 9))
        self.panel.refresh("status")
        self.assertEqual(len([i for i in self.items() if i["source"].get("hash") == "snapraid:outside"]), 1)

    def test_one_refresh_at_a_time_and_tick_runs_what_is_due(self):
        self.assertEqual(self.panel.tick(), ["status", "health"])
        self.assertEqual(self.panel.tick(), [])
        self.clock.now += 3601
        self.assertEqual(self.panel.tick(), ["status"])
        self.clock.now += 86400
        self.assertEqual(self.panel.tick(), ["status", "health"])
        self.panel._busy.acquire()
        self.addCleanup(self.panel._busy.release)
        self.assertTrue(self.panel.refresh("status")["busy"])

    def test_a_failed_read_is_kept_and_cleared_and_never_retried_in_a_loop(self):
        self.panel.refresh("status")
        self.runner.down = "sudo"
        before = len(self.runner.calls)
        view = self.panel.refresh("status")
        self.assertEqual(len(self.runner.calls) - before, 2, "info and status, once each (no probe after them)")
        self.assertEqual(view["light"]["tone"], "warn", "the amber of the last facts stands (health not read)")
        self.assertIn("cannot reach the disk protection reader", view["light"]["note"])
        self.runner.down = None
        view = self.panel.refresh("status")
        self.assertEqual(view["errors"], {})
        self.assertNotIn("note", view["light"])

    def test_a_damaged_file_list_is_red_and_the_last_facts_stay(self):
        """A status that fails on a damaged first copy of the file list: red (content_problem), never "Not
        protecting anything yet"; the last good facts and their time are kept, the failure beside them."""
        view = self.panel.refresh("status")
        good = view["facts"]["status"]
        self.rig.set_commands({**HEALTHY, "status": {"fixture": "status-damaged", "exit": 1}})
        view = self.panel.refresh("status")
        self.assertEqual((view["light"]["tone"], view["light"]["code"]), ("err", "content_problem"))
        self.assertEqual(view["facts"]["status"], good)
        self.assertEqual(view["facts"]["failed"]["outcome"], "failed")
        self.assertIn("Error decoding", view["facts"]["failed"]["words"])
        self.rig.set_commands(dict(HEALTHY))
        view = self.panel.refresh("status")
        self.assertIsNone(view["facts"]["failed"])
        self.assertNotEqual(view["light"]["code"], "content_problem")

    def test_probe_runs_with_each_status_read_and_room_is_sampled(self):
        """design section 4.1: the disks' power state with each status refresh (probe wakes no disk); health reads
        smart and disks only. One sample of the protection disk's room per half day, for its growth rate."""
        self.panel.refresh("status")
        self.assertEqual([verb for verb in self.runner.verbs() if verb != "list"], ["info", "status", "probe"])
        facts = self.panel.facts()
        self.assertEqual(set(facts["probe"]["disks"]), {"d1", "d2", "d3", "parity", "2-parity"})
        self.assertEqual(len(facts["room"]), 1)
        self.panel.refresh("status")
        self.assertEqual(len(self.panel.facts()["room"]), 1, "one sample per half day")
        self.clock.now += 13 * 3600
        self.panel.refresh("status")
        self.assertEqual(len(self.panel.facts()["room"]), 2)
        before = len(self.runner.calls)
        self.panel.refresh("health")
        self.assertEqual(self.runner.verbs()[before:], ["smart", "disks"])

    def test_unsafe_install_names_the_file(self):
        self.rig.lstat.spoiled = {str(self.rig.conf): (1000, 0)}
        view = self.panel.refresh("status")
        self.assertEqual((view["light"]["tone"], view["light"]["code"]), ("muted", "unsafe"))
        self.assertIn(str(self.rig.conf), view["light"]["reason"])

    def test_the_agents_sandbox_warning(self):
        """T21: the card's one line while the agents' sandbox is off."""
        self.assertEqual(self.panel.view()["warnings"], [])
        self.panel.config = Config(self.tmp / "config.json", {**self.config.raw, "harness": {"sandbox": "none"}})
        self.assertEqual(self.panel.view()["warnings"],
                         [{"code": "agents_unsandboxed", "words": snapraid.SANDBOX_LINE}])
        self.assertEqual(snapraid.SANDBOX_LINE,
                         "Your AI agents can read the names of your files while their sandbox is off.")

    def test_files_and_jobs_pages(self):
        self.panel.refresh("status")
        page = self.panel.files_page(disk="d2", folder="media/notes")
        self.assertIn("media/notes/<img src=x onerror=alert(1)>.jpg", {e["path"] for e in page["entries"]})
        jobs = self.panel.jobs()["jobs"]
        self.assertTrue(jobs)
        record = self.panel.job(jobs[-1]["job"], log=True)
        self.assertIn("text", record["log"])
        with self.assertRaises(KeyError):
            self.panel.job("J-999999")
        with self.assertRaises(ValueError):
            self.panel.files_page(disk="d9")


class EmptyStateTests(unittest.TestCase):
    """T22: absent SnapRAID, a missing reader, a missing config."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="srempty-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def panel(self, local: dict, enabled: bool = True, client: Any = None) -> snapraid.SnapraidPanel:
        config = Config(self.tmp / "config.json", {"data_dir": str(self.tmp / "data"),
                                                   "snapraid": {"enabled": enabled,
                                                                "helper": str(self.tmp / "no-such-helper")}})
        return snapraid.SnapraidPanel(config, None, FakeNotifier(), None, client=client, local=lambda: dict(local),
                                      metrics=NO_DAEMON)

    def test_absent_snapraid_never_polls_or_notifies(self):
        calls = []
        client = snapraid.HelperClient(runner=lambda *a: calls.append(a) or (0, b"{}", b""))
        panel = self.panel({"binary": None, "version": None, "configs": {}, "readable_disks": None}, client=client)
        view = panel.view()
        self.assertEqual((view["installed"], view["state"], view["light"]["code"]), (False, "absent", "absent"))
        self.assertEqual(view["light"]["reason"], "Disk protection is not set up. It is optional: it rebuilds a dead "
                                                  "data disk from the others.")
        self.assertEqual(panel.tick(), [])
        with self.assertRaises(ValueError):
            panel.refresh("status")
        self.assertEqual(calls, [])
        self.assertFalse((self.tmp / "data" / "snapraid").exists())
        checks = snapraid.doctor_checks(panel.config, local={"binary": None}, daemon={"metrics": NO_DAEMON})
        self.assertEqual([(c["name"], c["ok"], c["level"]) for c in checks], [("snapraid", True, "info")])

    def test_the_reader_missing_shows_what_this_user_reads_and_the_install_offer(self):
        local = {"binary": "/usr/bin/snapraid", "version": "13.0", "configs": {"/etc/snapraid.conf": True},
                 "readable_disks": 3}
        view = self.panel(local).view()
        self.assertEqual((view["state"], view["light"]["code"]), ("no_reader", "no_reader"))
        self.assertEqual(view["snapraid"]["version"], "13.0")
        self.assertEqual(view["snapraid"]["readable_disks"], 3)
        self.assertEqual(view["light"]["reason"], snapraid.REASONS["no_reader"])
        self.assertEqual(self.panel(local, enabled=False).view()["state"], "no_reader")

    def test_off_and_not_set_up(self):
        rig = Rig(dict(HEALTHY))
        self.addCleanup(rig.close)
        client = snapraid.HelperClient(runner=InProcess(rig))
        local = dict(LOCAL)
        self.assertEqual(self.panel(local, enabled=False, client=client).view()["state"], "off")
        os.unlink(rig.paths.helper_config)
        panel = self.panel(local, client=client)
        view = panel.refresh("status")
        self.assertEqual((view["light"]["code"], view["light"]["reason"]),
                         ("not_set_up", "Disk protection is installed but not set up."))


class NoticeTests(NotifyCase):
    """T15: the six kinds have fixed texts with counts only; parity.held is high; nothing a phone gets names a file
    or a disk's SnapRAID name."""

    def test_the_kinds_are_rules(self):
        for kind in PARITY_KINDS:
            self.assertIn(kind, RULE_IDS)
            self.assertEqual(RULE_ROUTES[kind], "needs_you")
        defaults = Config(self.tmp / "x.json", {}).raw["notify"]["rule_priority"]
        self.assertEqual({kind: defaults[kind] for kind in PARITY_KINDS},
                         {"parity.held": "high", "parity.failed": "high", "parity.ok": "low", "disk.failing": "high",
                          "parity.released_outside": "high", "parity.down": "high", "parity.lapsed": "default"})
        self.assertIn("disk.failing", Config(self.tmp / "x.json", {}).raw["notify"]["quiet_breakthrough"])

    def test_parity_held_is_high_with_the_fixed_text_and_counts(self):
        result = self.notifier.parity_event("parity.held", {"removed": 1204, "updated": 3, "disks": 1,
                                                            "path": "/mnt/disk1/Photos", "name": "d1"})
        entry = self.notifier.message(result["message"])["message"]
        self.assertEqual(entry["priority"], "high")
        self.assertEqual(entry["title"], "Disk protection is waiting for you")
        self.assertEqual(entry["body"], "1,204 files deleted and 3 changed on 1 disk(s). Open the cockpit to look "
                                        "before protecting them.")
        self.notifier.tick()
        sent = self.channel("phone").sent
        self.assertTrue(sent)
        self.assertEqual(sent[0].priority, "high")
        payload = json.dumps([[item.title, item.text, item.why] for item in sent])
        for name in ("Photos", "/mnt", '"d1"', "disk1"):
            self.assertNotIn(name, payload)
        again = self.notifier.parity_event("parity.held", {"removed": 2000, "updated": 0, "disks": 2})
        self.assertTrue(again.get("deduped"), "one incident until it closes")

    def test_parity_ok_closes_parity_failed(self):
        first = self.notifier.parity_event("parity.failed", {})
        self.assertTrue(first["message"])
        self.assertTrue(self.notifier.parity_event("parity.failed", {}).get("deduped"))
        self.assertTrue(self.notifier.parity_event("parity.ok", {"time": "08:42"})["message"])
        self.assertTrue(self.notifier.parity_event("parity.failed", {})["message"], "a new failure alerts again")

    def test_the_texts_fit_a_phone_and_name_nothing(self):
        for kind in PARITY_KINDS:
            for fields in ({}, {"removed": 9999999, "updated": 1, "disks": 3, "count": 150, "time": "08:42"},
                           {"removed": "/mnt/disk1", "time": "../etc", "count": "d1"}):
                title, body = operational_text(Notice(kind, "operational", "needs_you", "high", "", "", fields=fields),
                                               names=True, safe_repos=set())
                self.assertLessEqual(len(title), 60)
                self.assertLessEqual(len(body), 300)
                self.assertEqual(filter_text(title + "\n" + body, terms=[], secrets=[])[1], [], (kind, body))
                self.assertNotIn("/mnt", body)
                self.assertNotIn("d1", body)
        from swfactory.notify import NotifyError

        with self.assertRaises(NotifyError):
            self.notifier.parity_event("backup.failed", {})
        with self.assertRaises(NotifyError):
            self.notifier.parity_event("disk.failing", {}, "../x")


class DoctorTests(PanelCase):
    """T23: each doctor check of section 9.7 at its level."""

    sandbox = "none"

    def checks(self, local: dict | None = None, **kw) -> list[dict]:
        kw.setdefault("daemon", {"metrics": NO_DAEMON, "link": None})
        return snapraid.doctor_checks(self.panel.config, client=snapraid.HelperClient(runner=self.runner),
                                      local=dict(local or LOCAL), **kw)

    def test_the_version_line_is_honest_for_versions_it_does_not_read(self):
        def make(name, ok, level, detail):
            return {"name": name, "ok": ok, "level": level, "detail": detail}
        line = snapraid.version_check({"version": "15.1", "binary": "/usr/bin/snapraid"}, make)
        self.assertEqual((line["ok"], line["level"]), (False, "warn"))
        self.assertIn("needs the SnapRAID Daemon, which is not installed or not answering", line["detail"])
        line = snapraid.version_check({"version": "15.1", "binary": "/usr/bin/snapraid"}, make, daemon_found=True)
        self.assertEqual((line["ok"], line["detail"]), (True, "SnapRAID 15.1 at /usr/bin/snapraid: read through the "
                                                               "SnapRAID Daemon"))
        line = snapraid.version_check({"version": "11.6", "binary": "/usr/bin/snapraid"}, make, daemon_found=True)
        self.assertIn("not a version Seraphina reads", line["detail"])

    def doctor(self, local: dict | None = None, **kw) -> dict[str, dict]:
        checks = self.checks(local, **kw)
        found: dict[str, dict] = {}
        for check in checks:
            found.setdefault(check["name"], check)
            if not check["ok"]:
                found[check["name"]] = check
        return found

    def test_levels(self):
        found = self.doctor(fstab="/nonexistent")
        self.assertEqual(found["snapraid"]["level"], "info")
        self.assertEqual((found["snapraid sandbox"]["ok"], found["snapraid sandbox"]["level"]), (False, "error"))
        self.assertIn("names of your files", found["snapraid sandbox"]["detail"])
        self.assertEqual(found["snapraid config"]["level"], "info")
        self.assertEqual((found["snapraid lab checks"]["level"], found["snapraid lab checks"]["ok"]), ("warn", False))
        self.assertIn("L8", found["snapraid lab checks"]["detail"])
        self.assertIn("server started by systemd", found["snapraid lab checks"]["detail"])

    def test_unsafe_conf_and_install_are_errors(self):
        self.rig.write_conf("smartctl d1 -d sat %s; id\n")
        found = self.doctor(fstab="/nonexistent")
        self.assertEqual((found["snapraid config"]["level"], found["snapraid config"]["ok"]), ("error", False))
        self.assertIn("unsafe_conf", found["snapraid config"]["detail"])
        self.rig.write_conf()
        self.rig.lstat.spoiled = {self.rig.paths.helper: (1000, 0)}
        found = self.doctor(fstab="/nonexistent")
        self.assertEqual(found["snapraid reader"]["level"], "error")
        self.assertIn("not owned by root", found["snapraid reader"]["detail"])

    def test_sudo_refused_names_no_new_privs(self):
        status = self.tmp / "status"
        status.write_text("NoNewPrivs:\t1\n")
        self.runner.down = "sudo"
        found = self.doctor(fstab="/nonexistent", status_path=str(status))
        self.assertEqual(found["snapraid reader"]["level"], "error")
        self.assertIn("NoNewPrivs", found["snapraid reader"]["detail"])
        self.assertIn("own system unit (systemd/factory@.service)", found["snapraid reader"]["detail"],
                      "DESIGN §17: on a server the harness runs as its own user, never from the owner's user unit")
        self.assertIn("the harness's own system unit", found["snapraid reader"]["detail"])
        self.assertNotIn("user unit", found["snapraid reader"]["detail"])

    def test_fstab_and_the_first_copy(self):
        fstab = self.tmp / "fstab"
        r = self.rig.root
        fstab.write_text(f"UUID=1 / ext4 defaults 0 1\nUUID=2 {r}/mnt/disk1 ext4 defaults 0 2\n"
                         f"UUID=3 {r}/mnt/disk2 ext4 defaults,nofail,x-systemd.device-timeout=30s 0 2\n"
                         f"UUID=4 {r}/mnt/parity1 ext4 defaults 0 2\n"
                         f"UUID=5 {r}/mnt/parity2/ ext4 nofail,x-systemd.device-timeout=30s 0 2\n")
        lines = [check["detail"] for check in self.checks(fstab=str(fstab)) if check["name"] == "snapraid fstab"]
        self.assertEqual(len(lines), 2, lines)
        self.assertIn(f"{r}/mnt/disk1", lines[0])
        self.assertIn(f"{r}/mnt/parity1", lines[1], "a protection disk is an array disk too")
        self.assertTrue(all("disk2" not in line and "parity2" not in line for line in lines))
        found = self.doctor(fstab=str(fstab))
        self.assertEqual(found["snapraid fstab"]["level"], "warn")
        self.assertEqual(found["snapraid file list"]["level"], "info", "the first copy is not in /var/snapraid/")

    def test_releases_without_the_cockpit_are_an_error(self):
        state = Path(self.rig.paths.state)
        state.mkdir(parents=True, exist_ok=True)
        (state / "releases.jsonl").write_text('{"guard": "G-1", "at": "2026-10-25T08:00:00Z"}\n')
        found = self.doctor(fstab="/nonexistent")
        self.assertEqual(found["snapraid releases"]["level"], "error")

    def test_a_12_snapraid_warns_whether_or_not_the_panel_is_on(self):
        """design section 12.7: 12.x is a warning, not only while the panel is on."""
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                self.panel.config = self.make_config(enabled=enabled)
                first = self.checks({**LOCAL, "version": "12.3"}, fstab="/nonexistent")[0]
                self.assertEqual((first["name"], first["ok"], first["level"]), ("snapraid", False, "warn"))
                self.assertIn("12.3", first["detail"])
        self.assertEqual(self.checks(fstab="/nonexistent")[0]["level"], "info", "13.0 is at info")

    def test_a_reader_that_is_not_this_releases_warns(self):
        with open(self.rig.paths.helper, "a", encoding="utf-8") as handle:
            handle.write("# changed after the release\n")
        found = self.doctor(fstab="/nonexistent")
        self.assertEqual((found["snapraid reader"]["ok"], found["snapraid reader"]["level"]), (False, "warn"))
        self.assertIn("not this release's", found["snapraid reader"]["detail"])
        self.assertIn("release notes", found["snapraid reader"]["detail"])

    def test_too_few_disks_for_the_file_list_is_an_error(self):
        r = self.rig.root
        self.rig.write_conf(text=self.rig.conf.read_text().replace(
            f"content {r}/mnt/disk2/.snapraid/snapraid.content\n", ""))
        found = self.doctor(fstab="/nonexistent")
        self.assertEqual((found["snapraid file list"]["ok"], found["snapraid file list"]["level"]), (False, "error"))
        self.assertIn("fewer than 3 different disks", found["snapraid file list"]["detail"])

    def test_the_panels_folder_not_masked_under_bwrap_warns(self):
        self.panel.config = Config(self.tmp / "config.json", {**self.config.raw, "harness": {"sandbox": "bwrap"}})
        self.assertNotIn("snapraid masks", self.doctor(fstab="/nonexistent"))
        with unittest.mock.patch("swfactory.config.sandbox_masks", return_value={"dirs": [], "files": []}):
            found = self.doctor(fstab="/nonexistent")
        self.assertEqual((found["snapraid masks"]["ok"], found["snapraid masks"]["level"]), (False, "warn"))

    def test_no_snapraid_where_the_reader_looks_is_an_error(self):
        """The harness's user may find a SnapRAID on its PATH while the reader, which looks only at its fixed places,
        finds none: every read is then refused, and the doctor says so instead of passing."""
        os.unlink(self.rig.root / "usr/bin/snapraid")
        found = self.doctor(fstab="/nonexistent")
        self.assertEqual((found["snapraid binary"]["ok"], found["snapraid binary"]["level"]), (False, "error"))
        self.assertIn("/usr/bin/snapraid", found["snapraid binary"]["detail"])

    def test_an_unsafe_snapraid_binary_is_an_error_naming_it(self):
        """DESIGN.md section 41 (G1): a SnapRAID binary others can change is unsafe_install for every verb, info
        too (it was "not installed" before, and the doctor passed)."""
        binary = str(self.rig.root / "usr/bin/snapraid")
        self.rig.lstat.spoiled = {binary: (0, 0o002)}
        found = self.doctor(fstab="/nonexistent")
        self.assertEqual((found["snapraid reader"]["ok"], found["snapraid reader"]["level"]), (False, "error"))
        self.assertIn(binary, found["snapraid reader"]["detail"])
        self.assertIn("can be changed by users other than root", found["snapraid reader"]["detail"])

    def test_the_doctor_runs_the_snapraid_line(self):
        from swfactory import doctor

        checks = doctor.run_checks(self.panel.config)
        self.assertTrue(any(check["name"].startswith("snapraid") for check in checks))


class MaskAndSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="srset-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = self.tmp / "config.json"
        self.path.write_text(json.dumps({"data_dir": str(self.tmp / "data")}))
        self.config = Config.load(self.path)

    def test_the_panels_folder_is_masked_from_agents(self):
        """T21: <data_dir>/snapraid is in sandbox_masks."""
        self.assertIn(self.config.data_dir / "snapraid", sandbox_masks(self.config)["dirs"])

    def test_settings(self):
        self.assertIn("snapraid", settings.UPDATABLE_SECTIONS)
        self.assertIn("maintenance", settings.UPDATABLE_SECTIONS)
        saved = settings.update_config(self.config, {"snapraid": {"enabled": True, "light": {"red_after_days": 3}},
                                                     "maintenance": {"mode": "manual"}})
        self.assertTrue(saved.raw["snapraid"]["enabled"])
        for bad, words in ((({"snapraid": {"nonsense": 1}}), "snapraid.nonsense"),
                           ({"snapraid": {"enabled": "yes"}}, "snapraid.enabled"),
                           ({"snapraid": {"refresh": {"status_minutes": 1}}}, "snapraid.refresh.status_minutes"),
                           ({"snapraid": {"light": {"fp_amber_percent": 0}}}, "snapraid.light.fp_amber_percent"),
                           ({"snapraid": {"light": {"smart_unreadable_ok": ["d 1"]}}}, "smart_unreadable_ok"),
                           ({"snapraid": {"schedule": {"sync": {"at": "25:00"}}}}, "snapraid.schedule.sync.at"),
                           ({"snapraid": {"schedule": {"sync": {"mode": "often"}}}}, "snapraid.schedule.sync.mode"),
                           ({"snapraid": {"schedule": {"scrub": {"days": ["fri", "funday"]}}}}, "days"),
                           ({"snapraid": {"restore": {"folder_mode": "merge"}}}, "folder_mode"),
                           ({"snapraid": {"live_folders": ["../etc"]}}, "live_folders"),
                           ({"snapraid": {"helper": "sf-snapraid"}}, "snapraid.helper"),
                           ({"maintenance": {"mode": "sometimes"}}, "maintenance.mode"),
                           ({"maintenance": {"extra": 1}}, "maintenance.extra")):
            with self.subTest(words):
                with self.assertRaisesRegex(ValueError, re.escape(words)):
                    settings.update_config(saved, bad)


class SetupTests(PanelCase):
    def test_copy_blocks_install_to_new_paths_then_check_then_move(self):
        setup = self.panel.stage(user="owner")
        folder = Path(setup["setup_dir"])
        self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
        for name in ("sf-snapraid", "snapraid-helper.json", "seraphina-snapraid"):
            self.assertEqual((folder / name).stat().st_mode & 0o777, 0o600, name)
        self.assertEqual((folder / "sf-snapraid").read_bytes(), (ROOT / "bin" / "sf-snapraid").read_bytes())
        sudoers = (folder / "seraphina-snapraid").read_text()
        self.assertIn(f'owner ALL=(root) NOPASSWD: {snapraid.HELPER} ""', sudoers)
        self.assertIn(f"Defaults!{snapraid.HELPER} !use_pty", sudoers)
        draft = json.loads((folder / "snapraid-helper.json").read_text())
        self.assertEqual((draft["v"], draft["allow"]), (1, ["read"]))
        blocks = setup["blocks"]
        self.assertEqual([b["title"] for b in blocks], ["Check SnapRAID", "Install the disk protection reader",
                                                        "Install its settings",
                                                        "Allow Seraphina to call it, and nothing else", "Check"])
        install = blocks[1]["commands"]
        self.assertTrue(install[1].endswith(f"{snapraid.HELPER}.new"))
        self.assertTrue(install[2].startswith("sha256sum"))
        self.assertEqual(blocks[1]["expect"], snapraid.file_sha256(ROOT / "bin" / "sf-snapraid"))
        self.assertEqual(blocks[1]["if_not"], f"sudo rm {snapraid.HELPER}.new")
        self.assertIn("explain helper-config", " ".join(blocks[2]["commands"]))
        self.assertIn("visudo -cf", " ".join(blocks[3]["commands"]))
        self.assertIn("another device", blocks[1]["note"])
        # No block's copied text moves a file into place without its check: the commands never move, and every
        # move is a step of its own that runs only when its check passes again.
        checks = {"2": "sha256sum -c", "3": "explain helper-config > /dev/null", "4": "visudo -cf"}
        for block in blocks:
            self.assertFalse(any(" mv " in f" {command} " for command in block["commands"]), block["id"])
            for command in block.get("then") or []:
                check, _, move = command.partition(" && ")
                self.assertTrue(move.startswith("sudo mv "), command)
                self.assertIn(checks[block["id"]], check)
        self.assertEqual([b["id"] for b in blocks if b.get("then")], ["2", "3", "4"])
        removal = setup["removal"]
        self.assertIn(f"sudo rm {snapraid.SUDOERS} {snapraid.HELPER_CONFIG} {snapraid.HELPER}", removal[0]["commands"])
        with self.assertRaises(ValueError):
            self.panel.stage(user="owner; rm -rf /")
        self.assertTrue(self.panel.check()["ok"])

    def test_the_install_step_moves_only_a_matching_file(self):
        """Block 2's second step, run for real in a temporary folder (sudo left out): a file that does not match
        the release's hash is never moved into place."""
        folder = self.tmp / "libexec"
        folder.mkdir()
        good = (ROOT / "bin" / "sf-snapraid").read_bytes()
        step = snapraid.copy_blocks(None, release_sha256=snapraid.file_sha256(ROOT / "bin" / "sf-snapraid"))[1]
        command = step["then"][0].replace(snapraid.HELPER, str(folder / "sf-snapraid")).replace("sudo ", "")
        for data, moved in ((good + b"# swapped\n", False), (good, True)):
            with self.subTest(moved=moved):
                (folder / "sf-snapraid.new").write_bytes(data)
                done = subprocess.run(["/bin/sh", "-c", command], capture_output=True, text=True, check=False)
                self.assertEqual(done.returncode == 0, moved, done.stdout + done.stderr)
                self.assertEqual((folder / "sf-snapraid").exists(), moved)
                self.assertEqual((folder / "sf-snapraid.new").exists(), not moved)

    def test_the_draft_reads_uuids_where_it_can(self):
        draft = snapraid.draft_helper_config("parity /mnt/parity1/snapraid.parity\ndata d1 /mnt/disk1/\n"
                                             "data d2 /mnt/disk2/\n",
                                             uuid_of=lambda path: "1111-2222" if path == "/mnt/disk1/" else None)
        self.assertEqual(draft["mounts"]["d1"], "1111-2222")
        self.assertEqual(draft["mounts"]["d2"], "<filesystem UUID of /mnt/disk2>")
        self.assertEqual(draft["mounts"]["parity"], "<filesystem UUID of /mnt/parity1>")
        self.assertNotIn("stop_wait_minutes", draft, "the reader stops nothing: no wait to draft")


class DraftTests(unittest.TestCase):
    def test_the_draft_reads_only_a_filesystem_mounted_exactly_there(self):
        """findmnt --mountpoint: a disk not mounted while the draft is made gets the placeholder, never the UUID of
        the filesystem above its folder (the system disk's, with --target)."""
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        with unittest.mock.patch("swfactory.snapraid.subprocess.run", fake_run):
            self.assertIsNone(snapraid._findmnt_uuid("/mnt/disk9/"))
        self.assertEqual(calls[0][:5], ["findmnt", "-n", "-o", "UUID", "--mountpoint"])
        self.assertEqual(calls[0][5], "/mnt/disk9")
        self.assertNotIn("--target", calls[0])


class WordTests(unittest.TestCase):
    """U7, this design's own word check (section 9.5): no internal term of the words table on a novice screen."""

    def test_no_internal_term_in_the_panels_novice_words(self):
        for text in snapraid.novice_strings():
            self.assertEqual(snapraid.internal_terms(text), [], text)
        self.assertEqual(snapraid.internal_terms("the parity disk needs a sync"), ["parity", "sync"])
        self.assertEqual(snapraid.internal_terms("synced, scrubbed, fixes, the helper, a content file, released"),
                         ["synced", "scrubbed", "fixes", "helper", "content file", "released"])

    def test_no_internal_term_in_the_backups_page(self):
        js = (ROOT / "swfactory" / "web" / "views" / "backups.js").read_text(encoding="utf-8")
        start = js.index("const WORDS = {")
        end = js.index("\n  };", start)
        block = js[start:end]
        strings = re.findall(r"'((?:[^'\\]|\\.)*)'", block)
        self.assertGreater(len(strings), 20)
        for text in strings:
            self.assertEqual(snapraid.internal_terms(text), [], text)

    def test_the_three_sentences_word_for_word(self):
        design = (ROOT / "docs" / "seraphina" / "BACKUPS_SNAPRAID.md").read_text(encoding="utf-8")
        quoted = design[design.index("### 1.1 The sentences on the page"):design.index("Each clause is a fact:")]
        text = " ".join(line.lstrip("> ").strip() for line in quoted.splitlines() if line.startswith(">"))
        for sentence in snapraid.SENTENCES:
            self.assertIn(sentence, re.sub(r"\s+", " ", text))
        self.assertIn(snapraid.WATCHING, re.sub(r"\s+", " ", design))


class ActivityTests(PanelCase):
    def test_the_journal_is_an_operators_activity_source(self):
        from swfactory import activity

        self.panel.refresh("status")
        self.panel.refresh("health")
        config = Config(self.tmp / "config.json", {**self.config.raw, "agents": {"kate": {"role": "operator"}}})
        with unittest.mock.patch("swfactory.activity.is_operator", return_value=True):
            result = activity.activity(config, agent="kate")
        self.assertEqual(activity.SOURCES[-1], "snapraid")
        entries = [entry for entry in result["entries"] if entry["source"] == "snapraid"]
        self.assertTrue(entries)
        self.assertEqual(entries[0]["kind"], "health")
        self.assertIn("finding(s) opened", entries[0]["summary"])


class RouteTests(ServerTestCase):
    def test_the_routes_reach_the_factory(self):
        calls: list = []
        factory: Any = self.factory
        factory.snapraid_view = lambda: {"phase": "P1", "light": {"tone": "warn"}}
        factory.snapraid_refresh = lambda what, wait=False: calls.append((what, wait)) or {"refreshing": what}
        factory.snapraid_jobs = lambda: {"jobs": []}
        factory.snapraid_job = lambda job, log=False, offset=0: calls.append((job, log, offset)) or {"job": {}}
        factory.snapraid_guard = lambda guard, **page: calls.append((guard, page)) or {"guard": {}, "page": {}}
        factory.snapraid_files = lambda **page: calls.append(page) or {"entries": []}
        factory.snapraid_setup = lambda: {"blocks": []}
        factory.snapraid_setup_action = lambda action: calls.append(action) or {"ok": True}
        self.assertEqual(self.request("GET", "/api/snapraid").json()["phase"], "P1")
        response = self.request("POST", "/api/snapraid/refresh", {"what": "diff"})
        self.assertEqual((response.status, response.json()), (202, {"refreshing": "diff"}))
        self.assertEqual(self.request("POST", "/api/snapraid/refresh", {"what": "sync"}).status, 400)
        self.assertEqual(self.request("POST", "/api/snapraid/refresh", {"what": "status", "x": 1}).status, 400)
        self.assertEqual(self.request("POST", "/api/snapraid/refresh", {"what": "status", "wait": True}).status, 200)
        self.assertEqual(self.request("GET", "/api/snapraid/jobs/J-12?log=1&offset=5").status, 200)
        self.assertEqual(self.request("GET", "/api/snapraid/jobs/..%2F..%2Fetc").status, 400)
        self.assertEqual(self.request("GET", "/api/snapraid/jobs/J-1x").status, 400)
        self.assertEqual(self.request("GET", "/api/snapraid/guard/G-3?disk=d1&q=x").status, 200)
        self.assertEqual(self.request("GET", "/api/snapraid/guard/G-").status, 400)
        self.assertEqual(self.request("GET", "/api/snapraid/files?disk=d2&dir=media").status, 200)
        self.assertEqual(self.request("GET", "/api/snapraid/setup").status, 200)
        self.assertEqual(self.request("POST", "/api/snapraid/setup/stage", {}).status, 200)
        self.assertEqual(self.request("POST", "/api/snapraid/setup/format", {}).status, 404)
        for later in ("/api/snapraid/sync", "/api/snapraid/scrub", "/api/snapraid/guard/G-1/release"):
            self.assertEqual(self.request("POST", later, {}).status, 404, later)
        self.assertEqual(calls[:2], [("diff", False), ("status", True)])
        self.assertIn(("J-12", True, 5), calls)
        self.assertIn(("G-3", {"disk": "d1", "kind": None, "folder": None, "q": "x", "offset": 0}), calls)
        self.assertEqual(self.request("GET", "/api/snapraid", token=None).status, 401)

    def test_no_snapraid_route_takes_an_agent_token(self):
        from swfactory import server

        handlers = [handler for _, pattern, handler in server._ROUTES if "snapraid" in pattern.pattern]
        self.assertEqual(len(handlers), 20)
        self.assertFalse(set(handlers) & server.AGENT_TOKEN_ROUTES)


class CliTests(unittest.TestCase):
    def test_the_card_in_plain_text(self):
        view = {"light": {"tone": "warn", "reason": "Not protecting anything yet."}, "undo_line": snapraid.WATCHING,
                "facts": {"words": ["All 3 disks are healthy."]}, "lab_words": snapraid.LAB_WORDS,
                "disks": [{"number": 1, "name": "d1", "mounted": True, "mount": "/mnt/disk1", "files": 9,
                           "health": "unknown"}], "guard": {"guard": "G-2", "verdict": "would_hold",
                                                            "reasons": ["lost_over"]}}
        lines = snapraid.card_lines(view)
        self.assertEqual(lines[0], "Disk protection: AMBER  Not protecting anything yet.")
        self.assertIn("  " + snapraid.WATCHING, lines)
        self.assertTrue(any("G-2: would_hold (lost_over)" in line for line in lines))
        self.assertTrue(any(line.startswith("  Disk 1") for line in lines))
        red = {**view, "light": {"tone": "err", "reason": "Disk 1 says it is failing. Replace it soon.",
                                 "note": "Last checked 4 hours ago."}}
        self.assertEqual(snapraid.card_lines(red)[:2], [("Disk protection: RED  Disk 1 says it is failing. Replace it "
                                                         "soon."), "  Last checked 4 hours ago."])

    def test_helper_install_prints_each_move_as_its_own_checked_step(self):
        """The terminal shows the same blocks as the setup page: every move after its expect line, as its own step,
        chained on its check."""
        from swfactory import __main__ as cli

        blocks = snapraid.copy_blocks(Path("/srv/staged"), release_sha256="ab" * 32)

        class FakeClient:
            def __init__(self, *_args: Any) -> None:
                pass

            def post(self, path: str, body: dict) -> dict:
                return {"blocks": blocks, "setup_dir": "/srv/staged"}

        args = cli.build_parser().parse_args(["snapraid", "helper", "install"])
        out = io.StringIO()
        with unittest.mock.patch.object(cli, "Client", FakeClient), \
                unittest.mock.patch.object(cli, "load_config", lambda: None), \
                unittest.mock.patch("sys.stdout", out), unittest.mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(cli.cmd_snapraid(args), 0)
        lines = out.getvalue().splitlines()
        moves = [index for index, line in enumerate(lines) if " mv " in f" {line} "]
        self.assertEqual(len(moves), 3)
        for index in moves:
            self.assertIn(" && sudo mv ", lines[index])
            self.assertTrue(lines[index - 1].startswith("#   then, only once it shows what you expect"))
            self.assertTrue(lines[index - 2].startswith("#   expect: "))

    def test_the_parser_has_no_release_and_no_later_actions(self):
        from swfactory.__main__ import build_parser

        parser = build_parser()
        args = parser.parse_args(["snapraid", "check-now", "diff"])
        self.assertEqual((args.snapraid_action, args.what), ("check-now", "diff"))
        for later in (["snapraid", "release"], ["snapraid", "sync"], ["snapraid", "scrub"]):
            with self.assertRaises(SystemExit):
                with unittest.mock.patch("sys.stderr", io.StringIO()):
                    parser.parse_args(later)


if __name__ == "__main__":
    unittest.main()


class QuietAndLayoutTests(PanelCase):
    """T52 (the harness's part) and the reader route's new reasons (section 4.2): the waits of section 8.1, a disk
    being taken out, a version read with care."""

    def test_quiet_reasons(self):
        proc = self.tmp / "proc"
        (proc / "41").mkdir(parents=True)
        (proc / "41" / "comm").write_text("snapraid\n")
        cfg = {"quiet_units": ["backup.service"], "quiet_margin_minutes": 30}
        self.assertEqual(snapraid.quiet_reason(cfg, self.config, NOW, proc=str(proc), run=lambda argv: ""),
                         "snapraid_running")
        (proc / "41" / "comm").write_text("bash\n")
        active = lambda argv: "active\n" if argv[1] == "is-active" else ""   # noqa: E731
        self.assertEqual(snapraid.quiet_reason(cfg, self.config, NOW, proc=str(proc), run=active), "unit_active")
        soon = time.strftime("%a %Y-%m-%d %H:%M:%S UTC", time.localtime(NOW + 600))
        timer = lambda argv: "inactive\n" if argv[1] == "is-active" else soon   # noqa: E731
        self.assertEqual(snapraid.quiet_reason(cfg, self.config, NOW, proc=str(proc), run=timer), "unit_soon")
        later = time.strftime("%a %Y-%m-%d %H:%M:%S UTC", time.localtime(NOW + 7200))
        self.assertIsNone(snapraid.quiet_reason(cfg, self.config, NOW, proc=str(proc),
                                                run=lambda argv: "inactive" if argv[1] == "is-active" else later))
        power = Config(self.tmp / "c.json", {"wakes": {"timezone": "UTC", "power": {"off": "23:00", "wake": "07:00"}}})
        at_night = time.mktime(time.strptime("2026-10-25 23:30", "%Y-%m-%d %H:%M")) - time.timezone
        self.assertEqual(snapraid.quiet_reason({}, power, at_night, proc=str(proc), run=lambda argv: ""),
                         "power_quiet")

    def test_a_quiet_wait_runs_no_read(self):
        self.panel._quiet = lambda cfg: "unit_active"
        self.assertEqual(self.panel.tick(), [])
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.panel.view()["waiting_words"], "your backup job is running")

    def test_a_disk_being_taken_out_is_amber(self):
        self.panel.refresh("status")
        facts = self.panel.facts()
        facts["status"]["mounts"]["d3"].update(placeholder=True, mounted=True)
        self.panel._save(facts)
        light = self.panel.view_light()
        self.assertEqual((light["tone"], light["code"]), ("warn", "taking_out"))
        self.assertEqual(light["reason"], "Disk 3 is being taken out of protection.")
        # design section 8.3 item 2: the card lists it under Not covered, before the changes since the last run
        listed = self.panel.view()["not_covered"]
        self.assertEqual(listed[-2:], ["Disk 3, which is being taken out of protection",
                                       "every change made after the last protection run"])
        self.assertEqual(listed[:-2], list(snapraid.NOT_COVERED[:-1]))
        facts["status"]["mounts"]["d3"].update(placeholder=False)
        self.panel._save(facts)
        self.assertEqual(self.panel.view()["not_covered"], list(snapraid.NOT_COVERED))

    def test_no_new_privs_advice_names_the_system_unit(self):
        status = self.tmp / "status"
        status.write_text("NoNewPrivs:\t1\n")
        client = snapraid.HelperClient(runner=lambda *a: (1, b"", b"sudo: a password is required"),
                                       status_path=str(status))
        with self.assertRaises(snapraid.HelperError) as caught:
            client.call("info")
        self.assertIn("the harness's own system unit", caught.exception.words)
        self.assertNotIn("user unit", caught.exception.words)
