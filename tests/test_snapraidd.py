"""Tests for swfactory/snapraidd.py, the SnapRAID Daemon route's harness side (DESIGN.md §41;
docs/seraphina/BACKUPS_SNAPRAID.md §3 to §7, §12, §13.2): T29 /metrics, T30 reading, T31 to T33 the checks before
acting, T34 the binding, T35 and T37 outcomes and the look afterwards, T36 matching, T38 stop, T39 skip, T40 a
freeze, T41 accept, T42 and T43 undelete and heal, T45 reports, T46 "protected up to", T47 restarts, T48 privacy, T49
health, T53 the hook, the doctor's daemon lines, the settings and the routes. The panel reads tests/mock_snapraidd.py
(seeded from the recorded 2.1 exchanges) through the real link (bin/sf-snapraidd), in-process; nothing touches a
real server.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from swfactory import outcomes, settings, snapraid, snapraidd
from swfactory.config import Config, sandbox_masks
from swfactory.events import EventBus
from swfactory.housekeeping import HousekeepingManager
from tests.mock_snapraidd import FIXTURES, MockDaemon
from tests.snapraidd_rig import CANARY, LinkRig

HOSTILE = ("<img src=x onerror=alert(1)>", "Private Letter.doc", "a [b]*c?.txt", "back\\slash", "tab\tname",
           "invoice‮gpj.exe")


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

    def kinds(self) -> list[str]:
        return [kind for kind, _, _ in self.events]


class Clock:
    def __init__(self) -> None:
        self.offset = 0.0

    def __call__(self) -> float:
        return time.time() + self.offset


def reader_info(mounted: bool = True, places: list | None = None) -> dict:
    """The reader's info summary as the panel keeps it: its inventory and its mount check."""
    return {"at": "2026-10-25T15:00:00Z", "schedulers": {"found": places or [], "places_checked": 9},
            "mounts": {name: {"role": "data", "mounted": mounted, "uuid_ok": mounted} for name in ("d1", "d2")},
            "snapraid": {"version": "15.1"}}


class DaemonCase(unittest.TestCase):
    allow = ("read", "act", "accept")
    schedule = "02:00"
    reader = True

    def setUp(self) -> None:
        self.rig = LinkRig(self.allow)
        self.addCleanup(self.rig.close)
        self.daemon: MockDaemon = self.rig.daemon
        self.daemon.config["maintenance_schedule"] = self.schedule
        self.tmp = Path(tempfile.mkdtemp(prefix="sdpanel-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config = Config(self.tmp / "config.json", {"data_dir": str(self.tmp / "data"),
                                                        "harness": {"sandbox": "bwrap"},
                                                        "snapraid": {"enabled": True}})
        self.bus = EventBus()
        self.events = self.bus.subscribe()
        self.system = HousekeepingManager(self.config.data_dir, self.bus)
        self.notifier = FakeNotifier()
        self.clock = Clock()
        self.link = snapraidd.LinkClient("/nonexistent", connector=self.rig.connector())
        self.metrics = snapraidd.MetricsReader(self.daemon.url + "/metrics")
        self.panel = snapraid.SnapraidPanel(self.config, self.system, self.notifier, self.bus,
                                            local=lambda: {"binary": "/usr/bin/snapraid", "version": "15.1"},
                                            clock=self.clock, metrics=self.metrics, link=self.link,
                                            quiet=lambda cfg: None, proc_root=str(self.tmp))
        if self.reader:
            self.panel.reader_present = lambda: True
            self.set_reader(reader_info())
        self.route = self.panel.daemon

    def set_reader(self, info: dict) -> None:
        facts = self.panel.facts()
        facts["info"] = info
        facts["info_tried"] = snapraid._iso(time.time())
        self.panel._save(facts)

    def poll(self) -> list[str]:
        """One poll now, whatever the interval."""
        d = self.route.facts()
        d.pop("metrics_tried", None)
        self.route.save(d)
        return self.route.tick()

    def full(self) -> list[str]:
        d = self.route.facts()
        d.pop("metrics_tried", None)
        d["full_at"] = 0
        self.route.save(d)
        return self.route.tick()

    def link_reads(self) -> list[str]:
        return [r["path"] for r in self.daemon.requests if r["path"] != "/metrics"]

    def posts(self) -> list[dict]:
        return [r for r in self.daemon.requests if r["method"] == "POST"]

    def items(self) -> list[dict]:
        store = self.system.store()
        return store.ledger().items if store.path.is_file() else []

    def open_items(self, prefix: str) -> list[dict]:
        return [i for i in self.items() if str((i.get("source") or {}).get("hash", "")).startswith(prefix)
                and i["status"] not in ("verified", "dropped", "deferred")]

    def approve(self, preview: dict, **kw) -> dict:
        return self.route.approve(preview["approval"], preview["shown_sha256"], **kw)


class MetricsTests(unittest.TestCase):
    """T29: every recorded /metrics reads into typed facts."""

    def test_every_recorded_metrics_parses(self):
        files = [p for p in FIXTURES.rglob("*.json") if isinstance(json.loads(p.read_text()), dict)
                 and json.loads(p.read_text())["path"].split("?")[0] == "/metrics"
                 and json.loads(p.read_text())["status"] == 200]
        self.assertGreater(len(files), 10)
        for path in files:
            with self.subTest(path=path.name):
                facts = snapraidd.parse_metrics(json.loads(path.read_text())["text"])
                self.assertIn(facts["health"], snapraidd.HEALTH_BY_NUMBER.values())
                self.assertIn("uptime", facts)
                self.assertEqual(set(facts["pulse"]), {"array", "config", "disks", "tasks", "activity"})

    def test_typed_facts_from_a_freeze(self):
        text = json.loads((FIXTURES / "lab/07-after-threshold-abort/GET_metrics.json").read_text())["text"]
        facts = snapraidd.parse_metrics(text)
        self.assertEqual(facts["last_exit"]["sync"], 2)
        self.assertEqual((facts["diff"]["removed"], facts["thresholds"]), (25, {"deletes": 20, "updates": 50}))
        self.assertEqual(facts["build"], {"daemon": "2.1", "engine": "15.1"})
        running = snapraidd.parse_metrics(json.loads((FIXTURES / "lab/03-during-sync/GET_metrics.json")
                                                     .read_text())["text"])
        self.assertEqual(running["active"]["command"], "sync")
        self.assertEqual(running["active"]["progress"], 37)

    def test_health_numbers_unknown_names_and_malformed_lines(self):
        for number, name in ((0, "pending"), (1, "passed"), (2, "corrupt"), (3, "degraded"), (4, "prefail"),
                             (5, "failing")):
            self.assertEqual(snapraidd.parse_metrics(f"snapraid_array_health {number}\n")["health"], name)
        self.assertNotIn("x", snapraidd.parse_metrics("something_else{a=\"b\"} 3\n# HELP x\n"))
        with self.assertRaises(snapraidd.MetricsError):
            snapraidd.parse_metrics("snapraid_array_health one two\n")
        with self.assertRaises(snapraidd.MetricsError):
            snapraidd.MetricsReader(fetcher=lambda url: (200, b"{bad\n")).read()
        with self.assertRaises(snapraidd.MetricsError) as caught:
            snapraidd.MetricsReader(fetcher=lambda url: (_ for _ in ()).throw(ConnectionRefusedError())).read()
        self.assertEqual(caught.exception.kind, "down")

    def test_reads_are_on_the_retried_list_and_actions_are_not(self):
        self.assertIn("SnapRAID daemon reads", outcomes.RETRIED)
        self.assertNotIn("hold_off", " ".join(outcomes.RETRIED))


class ReadingTests(DaemonCase):
    """T30: no link read while the counters stay; a moved counter reads only its endpoint; the intervals."""

    def test_counters_decide_what_is_read(self):
        self.poll()                                  # the first poll reads everything
        first = len(self.link_reads())
        self.assertTrue(first)
        self.poll()
        self.assertEqual(len(self.link_reads()), first, "no counter moved: no link call")
        self.daemon.pulse["config"] += 1
        self.poll()
        new = self.link_reads()[first:]
        self.assertIn("/snapraid/v2/config", new)
        self.assertNotIn("/snapraid/v1/tasks?limit_history=20&limit_messages=0", new)
        self.assertNotIn("/snapraid/v2/disks", new)
        count = len(self.link_reads())
        self.daemon.pulse["disks"] += 1
        self.poll()
        self.assertNotIn("/snapraid/v2/disks", self.link_reads()[count:], "disks at most once a minute")

    def test_metrics_intervals(self):
        calls = []

        class Metrics:
            def read(inner) -> dict:
                calls.append(self.clock())
                return {"pulse": {}, "uptime": 100.0, "build": {"engine": "15.1"}, "last": {}, "last_exit": {},
                        "disks": {}, "diff": {}, "thresholds": {}, "active": active[0]}
        active = [None]
        self.route._metrics = Metrics()
        self.route._link = None
        self.panel.daemon._link = None
        self.route.tick()
        self.clock.offset += 30
        self.route.tick()
        self.assertEqual(len(calls), 1, "every 60 seconds")
        self.clock.offset += 31
        self.route.tick()
        self.assertEqual(len(calls), 2)
        active[0] = {"command": "sync", "high_command": "maintenance"}
        self.clock.offset += 61
        self.route.tick()
        self.clock.offset += 11
        self.route.tick()
        self.assertEqual(len(calls), 4, "every 10 seconds while a run is active")
        self.clock.offset += 3
        self.route.tick()
        self.assertEqual(len(calls), 4, "never faster than 5 seconds")

    def test_a_204_activity_and_the_link_limits(self):
        self.daemon.active, self.daemon.history = [], []
        self.full()
        self.assertIsNone(self.route.facts().get("activity"))
        for path in self.link_reads():
            if path.startswith(("/snapraid/v1/tasks", "/snapraid/v1/activity", "/snapraid/v1/array")):
                self.assertIn("limit_", path, path)


class ChecksTests(DaemonCase):
    """T31, T32, T33: the checks of §2.4 before an action is offered."""

    def test_auth_off_refuses_and_is_red(self):
        self.daemon.auth = False
        self.full()
        with self.assertRaisesRegex(ValueError, "without a password"):
            self.route.preview("hold_off")
        self.assertEqual(self.panel.view_light()["code"], "auth_off")
        self.assertEqual(self.panel.view_light()["tone"], "err")

    def test_old_engines_refuse(self):
        for version in ("13.0", "14.10", ""):
            with self.subTest(version=version):
                self.daemon.engine_version = version
                self.full()
                with self.assertRaisesRegex(ValueError, "cannot drive|SnapRAID"):
                    self.route.preview("maintenance")
        self.daemon.engine_version = "13.0"
        self.full()
        self.assertEqual(self.panel.view_light()["code"], "engine_old")

    def test_schedulers(self):
        info = reader_info(places=[{"place": "/etc/systemd/system/backup.timer", "kind": "unit",
                                    "sha256": "a" * 64, "schedule": []},
                                   {"place": "/etc/systemd/system/snapraidd.service", "kind": "unit",
                                    "sha256": "b" * 64}])
        count = snapraidd.schedulers(info, {"maintenance_schedule": "02:00"}, [])
        self.assertEqual(count["count"], 2, "the daemon's own unit never counts")
        self.assertEqual(snapraidd.schedulers(info, {"maintenance_schedule": "02:00"},
                                              [{"place": "/etc/systemd/system/backup.timer",
                                                "sha256": "a" * 64}])["count"], 1)
        self.assertEqual(snapraidd.schedulers(info, {"maintenance_schedule": "02:00"},
                                              [{"place": "/etc/systemd/system/backup.timer",
                                                "sha256": "c" * 64}])["count"], 2, "changed contents count again")
        self.assertIsNone(snapraidd.schedulers(None, {}, [])["count"])
        self.assertEqual(snapraidd.schedulers(reader_info(), {"maintenance_schedule": ""}, [])["count"], 0)
        self.set_reader(info)
        self.full()
        for action in ("maintenance", "heal", "undelete"):
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, "Two things"):
                self.route.preview(action, paths=[{"disk": "d1", "path": "x"}] if action == "undelete" else None)
        self.assertTrue(self.route.preview("hold_off")["approval"])
        self.assertEqual(self.panel.view_light()["code"], "two_schedulers")
        # brief section 2, design section 1.4: a tracker finding with the count, never which places
        items = self.open_items("snapraid:schedulers")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Two things start protection runs")
        self.assertIn("found 2 things", items[0]["detail"])
        self.assertNotIn("backup.timer", items[0]["detail"])
        self.full()
        self.assertEqual(len([i for i in self.items() if (i.get("source") or {}).get("key") == "schedulers"]), 1,
                         "one item, not one per poll")
        self.set_reader(reader_info())
        self.full()
        self.assertEqual(self.open_items("snapraid:schedulers"), [], "one scheduler again closes it")
        self.daemon.config["maintenance_schedule"] = ""
        self.full()
        self.assertEqual([i["title"] for i in self.open_items("snapraid:schedulers")],
                         ["Nothing starts protection runs"], "none is a finding too (design section 7.4)")

    def test_no_reader_means_unknown_and_refused(self):
        self.panel.reader_present = lambda: False
        self.full()
        with self.assertRaisesRegex(ValueError, "cannot see"):
            self.route.preview("maintenance")

    def test_read_only_link_offers_nothing(self):
        self.rig.write_config({**self.rig.cfg, "allow": ["read"]})
        self.full()
        with self.assertRaisesRegex(ValueError, "may only read"):
            self.route.preview("hold_off")
        offers = self.route.view()[0]["offers"]
        self.assertFalse(any(offers.values()))


class BindingTests(DaemonCase):
    """T34: a changed fact refuses and sends nothing; age, one send, the hash; a restart voids an unsent one."""

    def test_a_changed_threshold_refuses(self):
        self.full()
        preview = self.route.preview("maintenance")
        self.daemon.config["sync_threshold_deletes"] = 999
        with self.assertRaises(snapraidd.Changed):
            self.approve(preview)
        self.assertEqual(self.posts(), [])
        self.assertEqual(self.route.approval(preview["approval"])["state"], "refused")

    def test_age_one_send_and_the_hash(self):
        self.full()
        preview = self.route.preview("hold_off")
        with self.assertRaisesRegex(ValueError, "not what Seraphina showed"):
            self.route.approve(preview["approval"], "0" * 64)
        result = self.approve(preview)
        self.assertEqual(result["outcome"], "succeeded")
        with self.assertRaisesRegex(ValueError, "already used"):
            self.approve(preview)
        old = self.route.preview("hold_off", enabled=False)
        self.clock.offset += 16 * 60
        with self.assertRaisesRegex(ValueError, "too old"):
            self.approve(old)
        self.assertEqual(len(self.posts()), 1)

    def test_a_restart_voids_an_unsent_approval(self):
        self.full()
        preview = self.route.preview("maintenance")
        self.daemon.restart()
        self.full()
        self.assertEqual(self.route.approval(preview["approval"])["state"], "void")
        with self.assertRaisesRegex(ValueError, "already used"):
            self.approve(preview)


class OutcomeTests(DaemonCase):
    """T35, T36, T37: outcomes, matching, and the read-only look after an unknown one."""

    def test_202_is_matched_to_its_run_and_followed(self):
        self.full()
        result = self.approve(self.route.preview("maintenance"))
        self.assertEqual(result["outcome"], "succeeded")
        record = self.route.approval(result["approval"])
        self.assertEqual(record["response"]["status"], 202)
        self.assertTrue(record["run"]["numbers"])
        self.daemon.run_all()
        self.full()
        record = self.route.approval(result["approval"])
        self.assertEqual((record["run"]["verdict"], record["run"]["ended"]), ("done", True))
        self.assertIn("parity.ok", self.notifier.kinds())

    def test_409_is_failed_and_5xx_unknown_then_looked_at(self):
        self.full()
        self.daemon.queue("heal", ["up", "fix", "scrub", "report"])
        self.daemon.array["blocks_bad"] = 3
        self.full()
        result = self.approve(self.route.preview("heal"))
        self.assertEqual((result["outcome"], self.route.approval(result["approval"])["status"]), ("failed", 409))
        self.daemon.pending = []
        self.daemon.fail_with = 500
        self.full()
        result = self.approve(self.route.preview("heal"))
        record = self.route.approval(result["approval"])
        self.assertTrue(record.get("did_not_take_effect"), record)
        self.assertEqual(record["outcome"], "failed")
        self.assertEqual(len([p for p in self.posts() if p["path"].endswith("/heal")]), 2, "never sent again")

    def test_a_lost_answer_is_seen_afterwards(self):
        self.full()
        self.daemon.drop_answer = True
        result = self.approve(self.route.preview("maintenance"))
        record = self.route.approval(result["approval"])
        self.assertEqual((record["outcome"], record.get("seen_afterwards")), ("succeeded", True))
        self.assertEqual(len(self.posts()), 1)

    def test_an_unknown_with_a_restart_between_stays_unknown(self):
        self.full()
        self.daemon.drop_answer = True
        original = self.daemon.post

        def post_then_restart(name, body):
            answer = original(name, body)
            self.daemon.clock_offset = 0
            self.daemon.restart()
            return answer
        self.daemon.post = post_then_restart
        result = self.approve(self.route.preview("maintenance"))
        record = self.route.approval(result["approval"])
        self.assertEqual(record["outcome"], "unknown")
        titles = [i["title"] for i in self.open_items("snapraid:unknown:")]
        self.assertEqual(titles, [f"{outcomes.UNKNOWN_TITLE} (a protection run)"])
        self.assertEqual(self.panel.view_light()["code"], "unknown_outcome")
        self.assertEqual(len(self.posts()), 1)

    def test_matching_ignores_other_runs(self):
        before = {"highest": 10, "daemon_now": "2026-10-25T15:00:00"}
        tasks = {"pending": [], "active": [], "history": [
            {"number": 14, "command": "report", "high_command": "maintenance", "status": "terminated",
             "scheduled_at": "2026-10-25T15:00:01"},
            {"number": 12, "command": "sync", "high_command": "maintenance", "status": "terminated",
             "exit_code": 0, "scheduled_at": "2026-10-25T15:00:01"},
            {"number": 13, "command": "up", "high_command": "maintenance", "status": "terminated",
             "scheduled_at": "2026-10-25T15:00:01"},
            {"number": 9, "command": "sync", "high_command": "maintenance", "status": "terminated",
             "scheduled_at": "2026-10-25T14:00:00"},
            {"number": 11, "command": "fix", "high_command": "heal", "status": "terminated",
             "scheduled_at": "2026-10-25T15:00:01"},
            {"number": 20, "command": "sync", "high_command": "maintenance", "status": "queued",
             "scheduled_at": "2026-10-25T18:00:00"}]}
        run = snapraidd.match_run(tasks, "maintenance", before)
        self.assertEqual(run["numbers"], [12, 13, 14])
        self.assertIsNone(snapraidd.match_run(tasks, "undelete", before))
        self.assertEqual(snapraidd.match_run(tasks, "accept", before)["numbers"], [12, 13, 14])


class StopTests(DaemonCase):
    """T38: stop is bound to the running step, and a different step in the answer raises the alarm."""

    def test_stop_the_shown_step(self):
        self.daemon.queue("maintenance", ["up", "sync", "report"])
        self.daemon.begin()
        self.full()
        preview = self.route.preview("stop")
        self.assertEqual(preview["shows"]["what"], "starting the disks")
        self.assertIn("Don't turn the server off", preview["shows"]["line"])
        result = self.approve(preview)
        self.assertEqual(result["outcome"], "succeeded")

    def test_a_different_step_raises_the_alarm_and_nothing_more(self):
        self.daemon.queue("maintenance", ["up", "sync", "report"])
        self.daemon.begin()
        self.full()
        preview = self.route.preview("stop")
        original = self.daemon.stop_running

        def wrong():
            status, body, headers = original()
            return status, {**body, "number": body["number"] + 1}, headers
        self.daemon.stop_running = wrong
        self.approve(preview)
        self.assertTrue(self.open_items("snapraid:wrongstop:"))
        self.assertEqual(len([p for p in self.posts() if p["path"].endswith("/stop")]), 1)

    def test_nothing_running_offers_no_stop(self):
        self.full()
        with self.assertRaisesRegex(ValueError, "Nothing is running"):
            self.route.preview("stop")


class SkipTests(DaemonCase):
    """T39: a skip, a restart that undoes it, and the window's line."""

    def test_a_restart_undoes_an_approved_skip_and_asks_again(self):
        self.full()
        preview = self.route.preview("hold_off")
        self.assertIn("A restart of the protection service undoes this.", preview["shows"]["lines"])
        self.assertEqual(self.approve(preview)["outcome"], "succeeded")
        self.full()
        self.assertIn("will be skipped", self.panel.view()["undo_line"])
        self.daemon.restart()
        self.poll()
        self.assertIn("parity.lapsed", self.notifier.kinds())
        self.assertTrue(self.open_items("snapraid:lapsed"))
        self.assertEqual(self.panel.view_light()["code"], "lapsed")
        self.assertEqual(len([p for p in self.posts() if p["path"].endswith("/hold_off")]), 1, "never set again")

    def test_no_window_nothing_to_skip(self):
        self.daemon.config["maintenance_schedule"] = ""
        self.full()
        with self.assertRaisesRegex(ValueError, "nothing to skip"):
            self.route.preview("hold_off")

    def test_next_window(self):
        now = time.mktime((2026, 10, 25, 12, 0, 0, 0, 0, -1))
        self.assertEqual(time.localtime(snapraidd.next_window("02:00", now))[2:5], (26, 2, 0))
        self.assertEqual(time.localtime(snapraidd.next_window("Mon 03:00, Thu 04:00", now))[6], 0)
        self.assertIsNone(snapraidd.next_window("whenever", now))
        self.assertIsNone(snapraidd.next_window("", now))


class FreezeTests(DaemonCase):
    """T40, T41: a freeze is read from typed fields; one item and one incident; an accept after the review."""

    removed = {"d1": [f"docs/f-{i:03d}.bin" for i in range(24)] + list(HOSTILE[:1]), "d2": list(HOSTILE[1:])}

    def test_a_freeze_and_its_ways_out(self):
        self.full()
        for _ in range(3):
            self.daemon.freeze(self.removed)
            self.full()
        light = self.panel.view_light()
        self.assertEqual((light["tone"], light["code"]), ("warn", "frozen"))
        view = self.panel.view()["daemon"]
        self.assertEqual(view["frozen"]["counts"]["removed"], 30)
        self.assertEqual(view["frozen"]["which"], ["deletes"])
        self.assertEqual(len([i for i in self.items() if (i.get("source") or {}).get("hash") == "snapraid:frozen"]), 1)
        held = [e for e in self.notifier.events if e[0] == "parity.held"]
        self.assertEqual({key for _, _, key in held}, {"-"}, "one incident key")
        self.assertEqual(held[0][1]["removed"], 30)
        # an accept is offered only after the review was opened
        with self.assertRaisesRegex(ValueError, "Open the list"):
            self.route.preview("accept")
        page = self.route.review(offset=0)
        self.assertEqual(page["total"], 30)
        preview = self.route.preview("accept")
        self.assertIn("may be more than you looked at", preview["shows"]["accept_line"])
        self.assertEqual(preview["shows"]["per_disk"]["d2"]["removed"], 5)
        self.assertFalse(preview["typed"])
        record_text = (self.panel.folder() / "actions" / f"{preview['approval']}.json").read_text()
        for name in HOSTILE:
            self.assertNotIn(json.dumps(name)[1:-1], record_text)
        result = self.approve(preview)
        self.assertEqual(result["outcome"], "succeeded")
        body = json.loads(self.posts()[-1]["body"])
        self.assertEqual(body, {"ignore_thresholds": True, "spindown_on_finish": False})
        self.daemon.run_all()
        self.full()
        self.assertFalse(self.open_items("snapraid:frozen"))
        self.assertIn("parity.ok", self.notifier.kinds())
        self.assertNotIn("parity.released_outside", self.notifier.kinds())

    def test_far_over_the_limit_asks_for_the_typed_count(self):
        self.full()
        self.daemon.freeze({"d1": [f"x/{i}" for i in range(300)]})
        self.full()
        self.route.review(offset=0)
        preview = self.route.preview("accept")
        self.assertEqual(preview["typed"], True)
        with self.assertRaisesRegex(ValueError, "Type the number"):
            self.approve(preview, typed=12)
        self.assertEqual(self.approve(preview, typed=300)["outcome"], "succeeded")

    def test_a_run_with_the_limits_off_outside_seraphina(self):
        self.full()
        self.daemon.freeze(self.removed)
        self.full()
        self.daemon.queue("maintenance", ["up", "sync", "scrub", "report"])     # e.g. its web page, limits off
        self.daemon.run_all()
        self.full()
        self.assertIn("parity.released_outside", self.notifier.kinds())
        self.assertTrue(self.open_items("snapraid:outside"))

    def test_accept_needs_the_accept_class(self):
        self.rig.write_config({**self.rig.cfg, "allow": ["read", "act"]})
        self.full()
        self.daemon.freeze(self.removed)
        self.full()
        self.route.review(offset=0)
        with self.assertRaisesRegex(ValueError, "may only read"):
            self.route.preview("accept")


class UndeleteHealTests(DaemonCase):
    """T42, T43: the paths stay in memory; the reader's mount check; the verdict from the fix counters; heal."""

    def test_undelete_keeps_no_names_and_is_judged_by_counts(self):
        self.full()
        self.daemon.set_scan({"d1": ["docs/Private Letter.doc", "docs/other.bin"]})
        self.full()
        preview = self.route.preview("undelete", paths=[{"disk": "d1", "path": "docs/Private Letter.doc"}])
        self.assertEqual(preview["shows"]["count"], 1)
        self.assertNotIn("Private", json.dumps(preview))
        self.assertNotIn("Private", (self.panel.folder() / "actions" / f"{preview['approval']}.json").read_text())
        result = self.approve(preview)
        self.assertEqual(result["outcome"], "succeeded")
        self.daemon.run_all()
        self.full()
        record = self.route.approval(result["approval"])
        self.assertEqual((record["run"]["verdict"], record["run"]["recovered"]), ("done", 1))

    def test_undelete_needs_the_reader_to_see_the_disk_mounted(self):
        self.set_reader(reader_info(mounted=False))
        self.full()
        with self.assertRaisesRegex(ValueError, "mounted"):
            self.route.preview("undelete", paths=[{"disk": "d1", "path": "docs/a"}])
        self.set_reader(reader_info())
        self.daemon.disks["data_disks"][0]["devices"] = []
        self.full()
        with self.assertRaisesRegex(ValueError, "disk first"):
            self.route.preview("undelete", paths=[{"disk": "d1", "path": "docs/a"}])
        for bad in ("../etc", "a//b", "a\nb"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.route.preview("undelete", paths=[{"disk": "d2", "path": bad}])

    def test_heal_only_with_damage_and_green_after(self):
        self.full()
        with self.assertRaisesRegex(ValueError, "No hidden damage"):
            self.route.preview("heal")
        self.daemon.array["blocks_bad"] = 3
        self.daemon.health = "corrupt"
        self.full()
        self.assertEqual(self.panel.view_light()["code"], "bad_blocks")
        self.assertTrue(self.route.view()[0]["offers"]["heal"])
        result = self.approve(self.route.preview("heal"))
        self.assertEqual(result["outcome"], "succeeded")
        self.daemon.run_all()
        self.full()
        self.assertNotEqual(self.panel.view_light()["code"], "bad_blocks")

    def test_bad_health_offers_no_runs(self):
        self.daemon.health = "degraded"
        self.daemon.disks["data_disks"][1]["health"] = "degraded"
        self.full()
        self.assertEqual(self.panel.view_light()["tone"], "err")
        with self.assertRaisesRegex(ValueError, "disk first"):
            self.route.preview("maintenance")
        self.assertFalse(self.route.view()[0]["offers"]["maintenance"])


class ReportAndRestartTests(DaemonCase):
    """T45, T46, T47, T49: reports, "protected up to", restarts, health."""

    def test_a_report_is_handled_once_whether_by_event_or_read(self):
        self.full()
        self.daemon.queue("maintenance", ["up", "sync", "scrub", "report"])
        self.daemon.run_all({"sync": 1})
        self.panel.daemon.report_event("maintenance", "level=error code=1")
        self.full()
        self.assertEqual(self.notifier.kinds().count("parity.failed"), 1)
        self.assertTrue(self.open_items("snapraid:failed"))

    def test_protected_up_to_never_moves_on_a_stopped_sync(self):
        self.full()
        before = self.route.facts().get("protected_up_to")
        self.daemon.queue("maintenance", ["up", "sync", "report"])
        self.daemon.begin()
        self.daemon.finish()
        self.daemon.begin()
        self.daemon.stop_running()
        self.daemon.finish()
        self.daemon.run_all()
        self.full()
        self.assertEqual(self.route.facts().get("protected_up_to"), before)
        self.assertEqual(self.panel.view_light()["code"], "partial")

    def test_a_restart_makes_running_approved_runs_unknown(self):
        self.full()
        result = self.approve(self.route.preview("maintenance"))
        self.daemon.begin()
        self.daemon.restart()
        self.poll()
        record = self.route.approval(result["approval"])
        self.assertEqual(record["outcome"], "unknown")
        self.assertTrue(self.route.facts().get("restarted_at"))
        self.assertTrue(self.open_items("snapraid:unknown:"))

    def test_a_clock_step_alone_only_rebases(self):
        self.full()
        self.daemon.started -= 3600          # the uptime jumps, no start-up check, config counter the same
        self.poll()
        self.assertFalse(self.route.facts().get("restarted_at"))
        self.assertLess(abs(self.route.facts()["start"] - (time.time() - (time.time() - self.daemon.started))), 15)

    def test_down_then_up(self):
        self.full()
        self.daemon.kill()
        self.poll()
        self.clock.offset += 31 * 60
        self.poll()
        self.assertIn("parity.down", self.notifier.kinds())
        self.assertEqual(self.panel.view_light()["code"], "down")
        self.daemon.revive()
        self.route._metrics = snapraidd.MetricsReader(self.daemon.url + "/metrics")
        self.poll()
        self.assertIn(("parity.down", "-"), self.notifier.cleared)

    def test_health(self):
        self.full()
        light = self.panel.view_light()
        self.assertEqual(light["code"], "health_unknown", "pending past the start is unknown, never healthy")
        self.daemon.disks["data_disks"][0]["health"] = "failing"
        self.full()
        self.assertEqual(self.panel.view_light()["code"], "failing")
        self.assertIn(("disk.failing", {}, "disk-1"), self.notifier.events)
        self.assertTrue(self.open_items("snapraid:health:1"))

    def test_a_disk_with_no_resolved_device_is_unknown_never_healthy(self):
        """Brief section 13, design T49: /metrics may say "passed" for a disk whose devices the daemon never
        resolved (the link's disks read gives devices []); its SMART was never read, so the card and the light say
        unknown. A failing reading still stands, and an unknown one closes no failing finding."""
        for disk in self.daemon.disks["data_disks"] + self.daemon.disks["parity_disks"]:
            disk["health"] = "passed"
        self.daemon.health = "passed"
        self.full()
        view = self.route.view()[0]
        self.assertTrue(all(disk["health"] == "passed" for disk in view["disks"]), view["disks"])
        self.assertNotEqual(self.panel.view_light()["code"], "health_unknown")
        self.daemon.disks["data_disks"][0]["devices"] = []
        self.full()
        view = self.route.view()[0]
        first = next(disk for disk in view["disks"] if disk["key"] == "1")
        self.assertEqual(first["health"], "unknown")
        light = self.panel.view_light()
        self.assertEqual((light["code"], light["reason"]), ("health_unknown", "Disk 1's health can't be read."))
        self.assertEqual(snapraidd.disk_health("failing", []), "failing")
        self.assertEqual(snapraidd.disk_health("passed", None), "passed", "no disks read: /metrics decides")
        d = self.route.facts()
        d["health_seen"] = {first["name"]: "failing"}
        self.route.save(d)
        self.route._finding("snapraid:health:1", "Disk 1 says it is failing", "test")
        self.full()
        self.assertTrue(self.open_items("snapraid:health:1"), "an unknown reading is not a healthy one")

    def test_a_refused_password_opens_a_finding_and_waits(self):
        """Brief section 13: a 401 to the link's password opens snapraid:service once, the link's calls wait for
        the next full read, and a read that works again closes it."""
        self.full()
        (self.rig.root / "creds/basic").write_text("seraphina:changed\n", encoding="utf-8")
        self.full()
        items = self.open_items("snapraid:service")
        self.assertEqual([i["title"] for i in items], ["The protection service refused Seraphina's link"])
        self.assertEqual(self.route.view()[0]["link"]["code"], "login_refused")
        before = len(self.link_reads())
        self.full()
        self.full()
        self.assertEqual(len(self.link_reads()), before, "no calls with a refused password until the next full read")
        self.assertEqual(len(self.open_items("snapraid:service")), 1)
        (self.rig.root / "creds/basic").write_text(CANARY + "\n", encoding="utf-8")
        self.clock.offset += 31 * 60
        self.full()
        self.assertEqual(self.open_items("snapraid:service"), [], "a read that works again closes it")


class PrivacyTests(DaemonCase):
    """T48: hostile names and the canary credential reach nothing the harness keeps or sends."""

    def test_nothing_kept_names_a_file_or_holds_the_password(self):
        self.full()
        self.daemon.freeze({"d1": list(HOSTILE)})
        self.full()
        self.route.review(offset=0)
        preview = self.route.preview("accept")
        self.approve(preview)
        self.daemon.run_all()
        self.full()
        kept = ""
        for path in Path(self.config.data_dir).rglob("*"):
            if path.is_file():
                kept += path.read_text(encoding="utf-8", errors="replace")
        events = []
        while not self.events.empty():
            events.append(self.events.get_nowait())
        kept += json.dumps([getattr(e, "to_dict", lambda: str(e))() for e in events], default=str)
        kept += json.dumps(self.notifier.events) + json.dumps(self.panel.view(), default=str)
        for name in HOSTILE:
            self.assertNotIn(json.dumps(name)[1:-1], kept, name)
        self.assertNotIn("canary", kept)
        self.assertNotIn("/usr/local/bin/notify-example", kept)
        self.assertNotIn("/dev/loop", kept)


class HookTests(unittest.TestCase):
    """T53: parity.report from a source whose events name it, under its own token, once per key."""

    def test_the_hook_kind_reaches_the_panel_and_no_rule(self):
        from tests.test_notify import NotifyCase

        case = NotifyCase("run")
        case.setUp()
        self.addCleanup(case.doCleanups)
        reports = []
        case.factory.snapraid_report = lambda subject, detail: reports.append((subject, detail))
        notifier = case.notifier
        added = notifier.hook_add({"name": "snapraidd", "may": ["event"], "events": ["parity.report"]})
        self.assertTrue(added["token"])
        body = {"kind": "parity.report", "subject": "maintenance", "detail": "level=error code=2"}
        first = notifier.hook_event("snapraidd", body, "snapraidd-abc")
        again = notifier.hook_event("snapraidd", body, "snapraidd-abc")
        self.assertEqual((first, again), ({"message": None, "panel": True}, {"message": None, "panel": True}))
        self.assertEqual(reports, [("maintenance", "level=error code=2")])
        self.assertFalse(notifier.messages().get("messages"), "a report makes no notice by itself")
        from swfactory.notify import HookRefused

        notifier.hook_add({"name": "backup", "may": ["event"], "events": ["backup.failed"]})
        with self.assertRaises(HookRefused):
            notifier.hook_event("backup", body)


class DoctorTests(DaemonCase):
    def make(self, name: str, ok: bool, level: str, detail: str) -> dict:
        return {"name": name, "ok": ok, "level": level, "detail": detail}

    def checks(self, **kw) -> dict:
        found: dict[str, dict] = {}
        cfg = snapraid.settings(self.config)
        kw.setdefault("state_status", lambda url: 401)
        kw.setdefault("groups", [])
        for line in snapraidd.doctor_checks(self.config, cfg, self.make, metrics=self.metrics, link=self.link,
                                            reader_info=reader_info(), data_dir=self.config.data_dir,
                                            proc_root=str(self.tmp), **kw):
            if not line["ok"] or line["name"] not in found:
                found[line["name"]] = line
        return found

    def test_the_daemon_lines(self):
        found = self.checks()
        self.assertTrue(found["snapraid daemon"]["ok"])
        self.assertEqual(found["snapraid daemon login"]["level"], "info")
        self.assertEqual(found["snapraid daemon reports"]["level"], "warn", "notify_result is not the forwarder")
        self.assertNotIn("snapraid schedulers", found)
        self.assertEqual(self.checks(state_status=lambda url: 200)["snapraid daemon login"]["level"], "error")
        self.assertEqual(self.checks(groups=["docker"])["snapraid harness user"]["level"], "error")
        self.daemon.engine_version = "14.10"
        self.assertEqual(self.checks()["snapraid daemon engine"]["level"], "error")
        self.daemon.config["sync_threshold_deletes"] = 0
        self.assertEqual(self.checks()["snapraid daemon limits"]["level"], "error")

    def test_listener_beyond_loopback(self):
        net = self.tmp / "proc" / "net"
        net.mkdir(parents=True)
        port = f"{snapraidd._port(snapraidd.METRICS_URL):04X}"
        (net / "tcp").write_text("  sl  local_address rem_address   st\n"
                                 f"   0: 00000000:{port} 00000000:0000 0A 0\n")
        self.assertEqual(self.checks()["snapraid daemon listener"]["level"], "warn")
        self.assertEqual(self.checks(state_status=lambda url: 200)["snapraid daemon listener"]["level"], "error")
        (net / "tcp").write_text("  sl  local_address rem_address   st\n"
                                 f"   0: 0100007F:{port} 00000000:0000 0A 0\n")
        self.assertNotIn("snapraid daemon listener", self.checks())

    def test_a_link_journal_line_without_an_approval_is_an_error(self):
        code, answer = self.rig.ask({"v": 1, "op": "act", "approval": "A-77", "action": "hold_off",
                                     "body": {"enabled": True}, "expect": self.rig.expect("hold_off")})
        self.assertEqual(answer["outcome"], "succeeded", answer)
        self.assertEqual(self.checks()["snapraid link journal"]["level"], "error")

    def test_acting_without_the_sandbox_is_an_error(self):
        self.assertNotIn("snapraid link sandbox", self.checks())
        self.config.raw["harness"]["sandbox"] = "none"
        self.assertEqual(self.checks()["snapraid link sandbox"]["level"], "error")

    def test_no_daemon_is_info(self):
        down = snapraidd.MetricsReader(fetcher=lambda url: (_ for _ in ()).throw(ConnectionRefusedError()))
        lines = snapraidd.doctor_checks(self.config, {}, self.make, metrics=down, daemon_conf=str(self.tmp / "none"))
        self.assertEqual([(line["ok"], line["level"]) for line in lines], [(True, "info")])


class SettingsTests(unittest.TestCase):
    def test_the_daemon_keys(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        path = tmp / "config.json"
        path.write_text(json.dumps({"data_dir": str(tmp / "data")}))
        config = Config.load(path)
        good = {"snapraid": {"daemon": {"metrics_url": "http://127.0.0.1:7627/metrics", "active_seconds": 10},
                             "quiet_units": ["backup.service"], "quiet_margin_minutes": 30,
                             "not_schedulers": [{"place": "/etc/crontab", "sha256": "a" * 64}],
                             "light": {"scrub_target_days": 30}}}
        saved = settings.update_config(config, good)
        self.assertEqual(saved.raw["snapraid"]["quiet_units"], ["backup.service"])
        for bad, words in (({"daemon": {"metrics_url": "http://10.0.0.1:7627/metrics"}}, "metrics_url"),
                           ({"daemon": {"active_seconds": 1}}, "active_seconds"),
                           ({"daemon": {"token": "x"}}, "snapraid.daemon.token"),
                           ({"quiet_units": ["backup"]}, "quiet_units"),
                           ({"not_schedulers": [{"place": "x"}]}, "not_schedulers")):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, words):
                settings.update_config(saved, {"snapraid": bad})
        settings.update_config(saved, {"snapraid": {"schedule": {"sync": {"at": "02:00"}}}})   # P1's: ignored

    def test_the_link_socket_is_masked(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        config = Config(tmp / "c.json", {"data_dir": str(tmp / "d")})
        self.assertIn(Path("/run/seraphina-snapraidd/link.sock"), sandbox_masks(config)["files"])


class RouteTests(unittest.TestCase):
    def test_the_routes(self):
        from tests.test_server import ServerTestCase

        case = ServerTestCase("run")
        case.setUp()
        self.addCleanup(case.doCleanups)
        factory: Any = case.factory
        calls = []
        factory.snapraid_action_preview = lambda action, **f: calls.append((action, f)) or {"approval": "A-1"}

        def approve(approval, sha, typed=None):
            if sha == "changed":
                raise snapraidd.Changed("the limits changed")
            calls.append((approval, sha, typed))
            return {"outcome": "succeeded"}
        factory.snapraid_action_approve = approve
        factory.snapraid_action_cancel = lambda approval: {"state": "cancelled"}
        factory.snapraid_actions = lambda: {"actions": []}
        factory.snapraid_action = lambda approval: {"approval": approval}
        factory.snapraid_review = lambda **page: calls.append(page) or {"entries": []}
        factory.snapraid_fixes = lambda offset=0: {"entries": []}
        factory.snapraid_runs = lambda: {"runs": []}
        factory.snapraid_run = lambda number: calls.append(number) or {"task": {}}
        self.assertEqual(case.request("POST", "/api/snapraid/actions", {"action": "hold_off", "enabled": True}).status,
                         201)
        self.assertEqual(case.request("POST", "/api/snapraid/actions", {"action": "x", "extra": 1}).status, 400)
        self.assertEqual(case.request("POST", "/api/snapraid/actions/A-1/approve", {"shown_sha256": "s"}).status, 200)
        response = case.request("POST", "/api/snapraid/actions/A-1/approve", {"shown_sha256": "changed"})
        self.assertEqual((response.status, response.json()["error"]), (409, "changed"))
        self.assertEqual(case.request("POST", "/api/snapraid/actions/B-1/approve", {"shown_sha256": "s"}).status, 400)
        self.assertEqual(case.request("POST", "/api/snapraid/actions/A-1/cancel", {}).status, 200)
        self.assertEqual(case.request("GET", "/api/snapraid/review?disk=d1&offset=500").status, 200)
        self.assertEqual(case.request("GET", "/api/snapraid/runs/12").status, 200)
        self.assertEqual(case.request("GET", "/api/snapraid/runs/x").status, 400)
        self.assertEqual(case.request("GET", "/api/snapraid/actions").status, 200)
        self.assertIn(("hold_off", {"enabled": True}), calls)
        self.assertIn({"disk": "d1", "change": None, "q": None, "offset": 500}, calls)
        self.assertIn(12, calls)
        from swfactory import server

        handlers = [handler for _, pattern, handler in server._ROUTES if "snapraid" in pattern.pattern]
        self.assertFalse(set(handlers) & server.AGENT_TOKEN_ROUTES)


class OutcomeKeptTests(DaemonCase):
    """DESIGN §40 after review: a link failure after the POST is never "failed"; a restart never relabels a recorded
    refusal or a send that did not take effect; a cancelled skip never lapses; refusals and skips never hold the
    poller at the active rate."""

    def test_a_link_failure_answer_is_unknown_not_failed(self):
        self.full()
        real = self.rig.connector()

        def crashes_after_the_post(line, timeout):
            out = real(line, timeout)
            if b'"op":"act"' in line:
                return b'{"ok":false,"error":"failed","words":"The link failed: OSError."}\n'
            return out
        self.route._link = snapraidd.LinkClient("/nonexistent", connector=crashes_after_the_post)
        result = self.approve(self.route.preview("maintenance"))
        record = self.route.approval(result["approval"])
        self.assertEqual(len([p for p in self.posts() if p["path"].endswith("/maintenance")]), 1)
        self.assertNotEqual(record["outcome"], "failed")
        self.assertEqual(record["outcome"], "unknown", "no answer to match the run by: unknown, with its item")
        self.assertTrue(self.open_items("snapraid:unknown:"))
        self.assertEqual(len(self.posts()), 1, "nothing sent again")

    def test_no_answer_from_the_link_is_looked_at_only_once_it_can_no_longer_send(self):
        """The harness gave up while the link still worked (here it sends 0.3 s later): the look waits for
        ACT_TIMEOUT, past the link's RuntimeMaxSec, so a skip still on its way is never called "did not take
        effect"."""
        import threading
        self.full()
        preview = self.route.preview("hold_off")
        real = self.rig.connector()
        workers = []

        def slow(line, timeout):
            if b'"op":"act"' not in line:
                return real(line, timeout)
            self.assertGreaterEqual(timeout, snapraidd.ACT_TIMEOUT)
            worker = threading.Thread(target=lambda: (time.sleep(0.3), real(line, timeout)))
            worker.start()
            workers.append(worker)
            raise snapraidd.LinkError("no_answer", "Seraphina's link did not answer (TimeoutError).")
        self.route._link = snapraidd.LinkClient("/nonexistent", connector=slow)
        result = self.approve(preview)
        for worker in workers:
            worker.join()
        record = self.route.approval(result["approval"])
        self.assertEqual((record["outcome"], record.get("looked_at")), ("unknown", None), "no look yet")
        self.route._link = self.link
        self.poll()
        self.assertEqual(self.route.approval(result["approval"])["outcome"], "unknown", "still too early")
        self.clock.offset += snapraidd.ACT_TIMEOUT + 1
        self.poll()
        record = self.route.approval(result["approval"])
        self.assertEqual((record["outcome"], record.get("seen_afterwards")), ("succeeded", True))
        self.assertEqual(len([p for p in self.posts() if p["path"].endswith("/hold_off")]), 1)

    def test_a_known_refusal_is_failed(self):
        self.full()
        preview = self.route.preview("maintenance")
        self.daemon.engine_version = "14.10"
        result = self.approve(preview)
        self.assertEqual((result["outcome"], self.route.approval(result["approval"])["refused"]),
                         ("failed", "engine_too_old"))

    def test_a_restart_keeps_a_409_and_a_did_not_take_effect(self):
        self.full()
        self.daemon.queue("heal", ["up", "fix", "scrub", "report"])
        self.daemon.array["blocks_bad"] = 3
        self.full()
        refused = self.approve(self.route.preview("heal"))
        self.daemon.pending = []
        self.daemon.fail_with = 500
        self.full()
        looked = self.approve(self.route.preview("heal"))
        self.assertTrue(self.route.approval(looked["approval"]).get("did_not_take_effect"))
        self.daemon.restart()
        self.full()
        for result in (refused, looked):
            self.assertEqual(self.route.approval(result["approval"])["outcome"], "failed")
        self.assertEqual(self.open_items("snapraid:unknown:"), [])
        self.daemon.array["blocks_bad"] = 0
        self.full()
        self.assertNotEqual(self.panel.view_light()["code"], "unknown_outcome")

    def test_a_cancelled_skip_never_lapses_and_a_lapse_is_told_once(self):
        self.full()
        self.assertEqual(self.approve(self.route.preview("hold_off"))["outcome"], "succeeded")
        self.full()
        self.assertEqual(self.approve(self.route.preview("hold_off", enabled=False))["outcome"], "succeeded")
        self.daemon.restart()
        self.poll()
        self.assertNotIn("parity.lapsed", self.notifier.kinds())
        self.assertFalse(self.open_items("snapraid:lapsed"))
        self.full()
        self.assertEqual(self.approve(self.route.preview("hold_off"))["outcome"], "succeeded")
        for _ in range(2):                                  # two restarts seen (closer than the mock can show)
            d = self.route.facts()
            self.route._after_restart(d, self.route.now(), self.route.now())
            self.route.save(d)
        self.assertEqual(self.notifier.kinds().count("parity.lapsed"), 1)

    def test_only_a_taken_run_request_keeps_the_poller_active(self):
        self.full()
        self.assertEqual(self.approve(self.route.preview("hold_off"))["outcome"], "succeeded")
        self.full()
        self.assertFalse(self.route._own_active(self.route.facts()), "a skip is not a run")
        result = self.approve(self.route.preview("maintenance"))
        self.assertEqual(result["outcome"], "succeeded")
        self.assertTrue(self.route._own_active(self.route.facts()))
        self.daemon.run_all()
        self.full()
        self.assertFalse(self.route._own_active(self.route.facts()), "its run ended")
        record = self.route.approval(result["approval"])
        record["run"]["ended"] = False
        self.route._write(record)
        self.clock.offset += snapraidd.OWN_ACTIVE_SECONDS + 60
        self.assertFalse(self.route._own_active(self.route.facts()), "never longer than a day")


class StopVerdictTests(DaemonCase):
    """Section 3.5 rule 11 and section 6.7: a step Seraphina's stop reached reads as stopped, not failed, also when
    it is a run's last engine step; the read-only look after a lost stop answer decides by the daemon's own "Stop
    requested", never by the task's status."""

    def test_verdicts_of_a_stopped_last_step(self):
        def run(*steps):
            return {"steps": [{"number": n, "command": c, "status": st, "exit_code": code, "scheduled_at": "T"}
                              for n, c, st, code in steps], "numbers": [n for n, *_ in steps]}
        undelete = run((1, "up", "terminated", 0), (2, "fix", "terminated", 1), (3, "report", "terminated", 0))
        self.assertEqual(snapraidd.verdict(undelete)["verdict"], "failed", "a sync exit 1 alone is ambiguous")
        self.assertEqual(snapraidd.verdict(undelete, [(2, "T")])["verdict"], "stopped")
        self.assertEqual(snapraidd.verdict(undelete, [(2, "other run")])["verdict"], "failed", "number reused")
        scrub = run((1, "up", "terminated", 0), (2, "sync", "terminated", 0), (3, "scrub", "terminated", 1),
                    (4, "report", "terminated", 1))
        self.assertEqual(snapraidd.verdict(scrub, [(3, "T")])["verdict"], "stopped")

    def test_stopping_a_heals_last_check_is_no_failure(self):
        self.full()
        self.daemon.array["blocks_bad"] = 3
        self.daemon.queue("heal", ["up", "fix", "scrub", "report"])
        for _ in range(2):
            self.daemon.begin()
            self.daemon.finish()
        self.daemon.begin()                                 # the scrub, the run's last engine step
        self.full()
        result = self.approve(self.route.preview("stop"))
        self.assertEqual(result["outcome"], "succeeded")
        self.daemon.finish()                                # 15.1: terminated 1 after a stop
        self.daemon.run_all()
        self.full()
        self.assertNotIn("parity.failed", self.notifier.kinds())
        self.assertFalse(self.open_items("snapraid:failed"))
        self.assertEqual(self.route.runs()["runs"][0]["verdict"], "stopped")

    def test_a_lost_stop_answer_is_judged_by_the_daemons_mark(self):
        self.daemon.queue("maintenance", ["up", "sync", "report"])
        self.daemon.begin()
        self.full()
        self.daemon.drop_answer = True
        result = self.approve(self.route.preview("stop"))
        record = self.route.approval(result["approval"])
        self.assertEqual((record["outcome"], record.get("seen_afterwards")), ("succeeded", True),
                         "Stop requested is there, though the task still reads processing")
        self.assertEqual(len([p for p in self.posts() if p["path"].endswith("/stop")]), 1)

    def test_a_stop_that_never_arrived_is_unknown_then_did_not_take_effect(self):
        self.daemon.queue("maintenance", ["up", "sync", "report"])
        self.daemon.begin()
        self.full()
        original = self.daemon.handle

        def lost(h):                                        # the POST is read, never carried out, never answered
            self.daemon.reset_after_request = h.command == "POST"
            original(h)
        self.daemon.handle = lost
        result = self.approve(self.route.preview("stop"))
        record = self.route.approval(result["approval"])
        self.assertEqual(record["outcome"], "unknown", "still running, no mark: nothing can be said yet")
        self.daemon.finish()
        self.assertIs(self.route._stop_seen(self.link, record), False, "it ended by itself with no mark")


class ReportFailedTests(DaemonCase):
    """Section 13: a report the forwarder could not deliver is read from the task list (terminated -1)."""

    def test_a_failed_notification_shows(self):
        self.full()
        self.daemon.notify_fails = True
        self.daemon.queue("maintenance", ["up", "sync", "report"])
        self.daemon.run_all()
        self.full()
        self.assertTrue(self.route.view()[0]["report_failed"])
        self.assertEqual(self.panel.view_light()["code"], "report_failed")
        self.assertTrue(any("could not tell Seraphina" in i["title"] for i in self.open_items("snapraid:service")))
        self.daemon.notify_fails = False
        self.daemon.queue("maintenance", ["up", "sync", "report"])
        self.daemon.run_all()
        self.full()
        self.assertFalse(self.route.view()[0]["report_failed"])


class MatchingAfterRestartTests(DaemonCase):
    """R3: numbers are given out again after a restart, so a run belongs to an approval only with the same
    scheduled_at."""

    def test_a_reused_number_is_not_her_run(self):
        record = {"id": "A-1", "run": {"numbers": [8, 9, 10], "scheduled_at": "2026-10-25T02:00:00"}}
        other = {"numbers": [8, 9], "scheduled_at": "2026-10-25T09:00:00"}
        same = {"numbers": [9, 10], "scheduled_at": "2026-10-25T02:00:00"}
        self.assertIsNone(self.route._own_run(other, [record]))
        self.assertIs(self.route._own_run(same, [record]), record)


class WordsAndBlocksTests(DaemonCase):
    """A limit of 0 is no limit; the link's password is never echoed; undelete is one disk at a time; the
    sandbox and the link's act class; approval ids above the link's journal."""

    def test_a_zero_limit_reads_as_no_limit(self):
        self.assertEqual(snapraidd.limits_line({"deletes": 0, "updates": 0}),
                         "The protection service does not stop for deleted or changed files (no limit is set).")
        self.assertEqual(snapraidd.limits_line({"deletes": 100, "updates": 0}),
                         "The protection service stops a run when 100 or more files were deleted, across all disks. "
                         "It does not stop for changed files (no limit is set).")
        self.assertNotIn("any", snapraidd.limits_line({"deletes": 0, "updates": 1000}))
        self.daemon.config["sync_threshold_deletes"] = 0
        self.full()
        self.assertIn("does not stop for deleted files", self.route.view()[0]["limits_line"])
        self.assertIn("does not stop for deleted files", self.route.preview("maintenance")["shows"]["limits_line"])

    def test_the_password_block_never_echoes(self):
        block = next(b for b in snapraidd.link_blocks(None) if b["id"] == "L3")
        self.assertIn("systemd-ask-password", block["commands"][1])
        self.assertTrue(block["commands"][1].endswith(f"/dev/stdin {snapraidd.LINK_CREDENTIAL}"))
        self.assertEqual(block["commands"][0], "sudo -v", "sudo asks first, so the two prompts never mix")
        self.assertNotIn("Nothing is printed", block["expect"])

    def test_undelete_is_one_disk_at_a_time(self):
        self.full()
        self.daemon.set_scan({"d1": ["docs/a.bin"], "d2": ["photos/b.jpg"]})
        self.full()
        with self.assertRaisesRegex(ValueError, "one disk at a time"):
            self.route.preview("undelete", paths=[{"disk": "d1", "path": "docs/a.bin"},
                                                  {"disk": "d2", "path": "photos/b.jpg"}])

    def test_new_ids_stay_above_the_links_journal(self):
        self.full()
        code, answer = self.rig.ask({"v": 1, "op": "act", "approval": "A-40", "action": "hold_off",
                                     "body": {"enabled": True}, "expect": self.rig.expect("hold_off")})
        self.assertEqual(answer["outcome"], "succeeded")
        self.full()
        self.assertEqual(self.route.preview("hold_off", enabled=False)["approval"], "A-41")


class WordTests(unittest.TestCase):
    def test_novice_words(self):
        for text in snapraidd.novice_strings():
            words = " ".join(w for w in text.split() if "/" not in w and not w.startswith("{"))
            self.assertEqual(snapraid.internal_terms(words), [], text)
            for term in ("daemon", "threshold", "hold off", "hold-off", "undelete", "heal", "engine"):
                self.assertIsNone(re.search(rf"\b{term}\b", words.lower()), text)


if __name__ == "__main__":
    unittest.main()
