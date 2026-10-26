"""Tests for bin/sf-snapraidd, Seraphina's link to the SnapRAID Daemon and its report forwarder (DESIGN.md §41;
docs/seraphina/BACKUPS_SNAPRAID.md §3.2, §6, §7.1, §13.2): T27 only the table's requests, T28 the login and the
request checks, T31 and T32 the checks before acting, T35 outcomes, T38 stop, T42 the undelete patterns, T44 the
forwarder. The daemon is tests/mock_snapraidd.py, seeded from the recorded 2.1 exchanges; nothing touches a real
server."""

from __future__ import annotations

import io
import json
import os
import unittest

from tests.snapraidd_rig import CANARY, LinkRig, sd

ALLOWED = {(method, path) for method, path in sd.REQUESTS}


def sent(rig: LinkRig) -> list[tuple[str, str]]:
    return [(r["method"], r["path"].split("?")[0]) for r in rig.daemon.requests]


class LinkCase(unittest.TestCase):
    allow = ("read", "act", "accept")

    def setUp(self):
        self.rig = LinkRig(self.allow)
        self.addCleanup(self.rig.close)

    def tearDown(self):
        # T27: whatever the test did, nothing outside the link's table reached the daemon
        for method, path in sent(self.rig):
            self.assertIn((method, path), ALLOWED)
        for request in self.rig.daemon.requests:
            self.assertNotEqual(request["method"], "PATCH")

    def act(self, action: str, body: dict | None = None, expect: dict | None = None, approval: str | None = None):
        """One act with a fresh approval id and the full binding read now; expect overrides single keys."""
        self.approvals = getattr(self, "approvals", 0) + 1
        return self.rig.ask({"v": 1, "op": "act", "approval": approval or f"A-{self.approvals}", "action": action,
                             "body": body or {}, "expect": {**self.rig.expect(action, body), **(expect or {})}})

    def started_at(self) -> float:
        code, info = self.rig.ask({"v": 1, "op": "info"})
        return info["started_at"]


class RequestTableTests(LinkCase):
    """T27: every operation builds only §3.2's requests, with the limits the link sets."""

    def test_each_read_op_names_its_limits(self):
        self.rig.ask({"v": 1, "op": "read", "what": list(sd.READS)})
        paths = [r["path"] for r in self.rig.daemon.requests]
        self.assertIn("/snapraid/v1/tasks?limit_history=20&limit_messages=0", paths)
        self.assertIn("/snapraid/v1/activity?limit_messages=0", paths)
        self.assertIn("/snapraid/v1/array?limit_diffs=0&limit_fixes=0", paths)
        self.assertIn("/snapraid/v2/disks", paths)
        self.assertIn("/snapraid/v2/config", paths)

    def test_the_client_cannot_build_anything_else(self):
        client = sd.DaemonClient(self.rig.daemon.url, lambda: "Basic x")
        for method, path in (("POST", "/snapraid/v1/schedule"), ("POST", "/snapraid/v1/clear"),
                             ("POST", "/snapraid/v1/report"), ("POST", "/snapraid/v1/refresh"),
                             ("POST", "/snapraid/v1/suspend_idle"), ("GET", "/snapraid/v1/system"),
                             ("GET", "/snapraid/v1/disks"), ("GET", "/snapraid/v1/config"),
                             ("PATCH", "/snapraid/v2/config"), ("PATCH", "/snapraid/v1/config")):
            with self.subTest(method=method, path=path), self.assertRaises(RuntimeError):
                client.request(method, path)
        with self.assertRaises(RuntimeError):
            client.request("GET", "/snapraid/v1/tasks", {"limit_diffs": 1})
        with self.assertRaises(RuntimeError):
            client.request("GET", "/snapraid/v1/tasks", {"limit_history": -1})
        self.assertEqual(client.sent, [])

    def test_no_op_or_field_reaches_a_route_outside_the_table(self):
        for request in ({"v": 1, "op": "schedule"}, {"v": 1, "op": "read", "what": ["system"]},
                        {"v": 1, "op": "act", "approval": "A-1", "action": "clear", "body": {}, "expect": {}},
                        {"v": 1, "op": "act", "approval": "A-1", "action": "schedule", "body": {}, "expect": {}},
                        {"v": 1, "op": "read", "what": ["state"], "path": "/snapraid/v1/schedule"}):
            with self.subTest(request=request):
                code, answer = self.rig.ask(request)
                self.assertEqual(code, 2)
                self.assertFalse(answer["ok"])


class LoginAndRequestTests(LinkCase):
    """T28: one constant header from the credential, never logged or returned; the caller and the request checks."""

    def test_the_header_is_read_per_call_and_never_returned(self):
        code, answer = self.rig.ask({"v": 1, "op": "read", "what": ["state", "config", "tasks"]})
        self.assertEqual(code, 0, answer)
        text = json.dumps(answer)
        self.assertNotIn("canary", text)
        self.assertTrue(all(r["auth"] in ("ok", "none") for r in self.rig.daemon.requests))
        # a new password file is used at the very next call
        (self.rig.root / "creds/basic").write_text("seraphina:another\n", encoding="utf-8")
        code, answer = self.rig.ask({"v": 1, "op": "read", "what": ["state"]})
        self.assertEqual(answer["error"], "login_refused")
        self.assertNotIn("another", json.dumps(answer))
        journal = (self.rig.root / "state").glob("*")
        for path in journal:
            self.assertNotIn("canary", path.read_text(encoding="utf-8"))

    def test_credential_file_is_the_alternative_and_must_be_root_only(self):
        (self.rig.root / "etc/seraphina/snapraidd-basic.cred").write_text(CANARY + "\n", encoding="utf-8")
        cfg = {**self.rig.cfg, "credential_file": "/etc/seraphina/snapraidd-basic.cred"}
        self.rig.write_config(cfg)
        self.rig.env = {"STATE_DIRECTORY": str(self.rig.root / "state")}
        code, answer = self.rig.ask({"v": 1, "op": "read", "what": ["state"]})
        self.assertEqual(code, 0, answer)
        self.rig.lstat.spoiled[str(self.rig.root / "etc/seraphina/snapraidd-basic.cred")] = (0, 0o044)
        code, answer = self.rig.ask({"v": 1, "op": "read", "what": ["state"]})
        self.assertEqual(answer["error"], "no_credential")
        self.assertNotIn("canary", json.dumps(answer))

    def test_no_credential_directory_is_refused_in_words(self):
        self.rig.env = {}
        code, answer = self.rig.ask({"v": 1, "op": "read", "what": ["state"]})
        self.assertEqual(answer["error"], "no_credential")

    def test_a_caller_other_than_the_harness_user_is_refused(self):
        self.rig.peer = 1000
        code, answer = self.rig.ask({"v": 1, "op": "info"})
        self.assertEqual((code, answer["error"]), (2, "wrong_caller"))
        self.rig.peer = None
        self.assertEqual(self.rig.ask({"v": 1, "op": "info"})[1]["error"], "wrong_caller")
        self.assertEqual(self.rig.daemon.requests, [])

    def test_an_unsafe_installation_is_refused(self):
        self.rig.lstat.spoiled[str(self.rig.root / "etc/seraphina/snapraidd.json")] = (1000, 0)
        self.assertEqual(self.rig.ask({"v": 1, "op": "info"})[1]["error"], "unsafe_install")
        self.rig.lstat.spoiled = {str(self.rig.root / "usr/local/libexec/seraphina"): (0, 0o002)}
        self.assertEqual(self.rig.ask({"v": 1, "op": "info"})[1]["error"], "unsafe_install")

    def test_bad_settings_are_refused(self):
        for cfg in ({**self.rig.cfg, "daemon": "http://10.0.0.5:7627"}, {**self.rig.cfg, "allow": ["act"]},
                    {**self.rig.cfg, "allow": ["read", "accept"]}, {**self.rig.cfg, "extra": 1},
                    {**self.rig.cfg, "hook": {"url": "http://example.org/api/hooks/event"}},
                    {**self.rig.cfg, "credential_file": "/tmp/x"}):
            with self.subTest(cfg=cfg):
                self.rig.write_config(cfg)
                self.assertEqual(self.rig.ask({"v": 1, "op": "info"})[1]["error"], "unsafe_install")

    def test_requests_are_checked_before_anything_is_sent(self):
        cases = [b"x" * (17 * 1024) + b"\n", b"\xff\xfe\n", b"[1]\n",
                 b'{"v": true, "op": "info"}\n', b'{"v": 2, "op": "info"}\n', b'{"v": 1, "op": "nope"}\n',
                 b'{"v": 1, "op": "info", "extra": 1}\n', b'{"v": 1, "op": "run", "number": "7"}\n',
                 b'{"v": 1, "op": "review", "offset": -1}\n', b'{"v": 1, "op": "review", "change": "gone"}\n',
                 b'{"v": 1, "op": "info", "op": "read"}\n']
        for line in cases:
            with self.subTest(line=line[:60]):
                code, answer = self.rig.ask(line)
                self.assertEqual(code, 2)
                self.assertFalse(answer["ok"])
        self.assertEqual(self.rig.daemon.requests, [])

    def test_allow_classes_limit_the_ops(self):
        self.rig.write_config({**self.rig.cfg, "allow": ["read"]})
        code, answer = self.act("hold_off", {"enabled": True})
        self.assertEqual(answer["error"], "not_allowed")
        self.rig.write_config({**self.rig.cfg, "allow": ["read", "act"]})
        code, answer = self.act("maintenance", {"ignore_thresholds": True})
        self.assertEqual(answer["error"], "not_allowed")
        self.assertNotIn(("POST", "/snapraid/v1/maintenance"), sent(self.rig))


class ReadTests(LinkCase):
    """§3.6: only typed fields leave the link; names only through review, fixes and run."""

    def test_read_passes_typed_fields_only(self):
        code, answer = self.rig.ask({"v": 1, "op": "read", "what": list(sd.READS)})
        self.assertEqual(code, 0, answer)
        text = json.dumps(answer)
        for secret in ("/usr/local/bin/notify-example", "/dev/loop", "/mnt/d1", "lab-d1", "log_file", "report_output",
                       "instance", "/etc/snapraid", "engine_content"):
            self.assertNotIn(secret, text)
        self.assertEqual(answer["config"]["sync_threshold_deletes"], 20)
        self.assertFalse(answer["config"]["notify_result_is_forwarder"])
        self.assertTrue(answer["config"]["notify_result_set"])
        self.assertEqual({d["name"] for d in answer["disks"]}, {"d1", "d2", "parity"})
        self.assertIn("pulse", answer)

    def test_only_fixed_cancel_messages_pass(self):
        self.assertEqual(sd.task_view({"exit_msg": "The preceding sync operation failed with exit code 2"})["exit_msg"],
                         "The preceding sync operation failed with exit code 2")
        hidden = sd.task_view({"exit_msg": "error on /mnt/d1/secret name.doc"})
        self.assertNotIn("exit_msg", hidden)
        self.assertTrue(hidden["exit_msg_hidden"])

    def test_every_fixed_message_format_of_the_daemon_passes(self):
        """Each exit_msg the daemon writes itself, as its source formats it (runner.c, elem.c), passes whole."""
        for message in ("The preceding sync operation failed with exit code 2",
                        "The preceding up operation failed with exit code -1",
                        "The preceding sync operation was canceled",
                        "The preceding sync operation was signaled with signal SIGKILL(9)",
                        "The preceding scrub operation was signaled with signal SIGTERM(15)",
                        "The preceding fix operation failed to execute",
                        "The preceding sync operation failed during the pre-hook",
                        "The preceding sync operation failed during the post-hook",
                        "The maintenance operation was canceled during report notification",
                        "Array is DEGRADED! Task aborted!", "Array is in PREFAIL! Task aborted!",
                        "Array is FAILING!!! Task aborted!!!", "Canceled by user request", "Canceled before shutdown",
                        "Notification failed (check syslog for details)", "Stop requested",
                        "Pre-hook aborted because the system is shutting down",
                        "The docker executable was not found", "Failed to pause docker containers",
                        "The pre_run_script terminated with exit code 3",
                        "The post_run_script terminated with signal SIGTERM(15)"):
            with self.subTest(message=message):
                self.assertEqual(sd.task_view({"status": "canceled", "exit_msg": message}).get("exit_msg"), message)
        for message in ("Array is FAILING!!! Task aborted! /mnt/d1", "The preceding sync operation was canceled x",
                        "The pre_run_script failed to start (check syslog for details), errno=No such file(2)"):
            with self.subTest(message=message):
                self.assertTrue(sd.task_view({"exit_msg": message}).get("exit_msg_hidden"))

    def test_a_failed_notification_is_passed_on_as_a_flag(self):
        self.rig.daemon.notify_fails = True
        self.rig.daemon.queue("maintenance", ["up", "sync", "report"])
        self.rig.daemon.run_all()
        code, answer = self.rig.ask({"v": 1, "op": "read", "what": ["tasks"]})
        report = answer["tasks"]["history"][0]
        self.assertEqual((report["command"], report["status"], report["exit_code"], report.get("notify_failed")),
                         ("report", "terminated", -1, True))
        self.assertNotIn("Notification failed", json.dumps(answer), "the message itself stays in the link")

    def test_review_pages_counts_per_disk_and_the_scan(self):
        removed = {"d1": [f"docs/f-{i:04d}.bin" for i in range(700)], "d2": ["a [weird]*name?.txt"]}
        self.rig.daemon.set_scan(removed, updated=3)
        code, page = self.rig.ask({"v": 1, "op": "review", "offset": 0})
        self.assertEqual(code, 0, page)
        self.assertEqual((page["total"], len(page["entries"])), (704, 500))
        self.assertEqual(page["per_disk"]["d1"]["removed"], 700)
        self.assertEqual(page["scan"]["counts"]["removed"], 701)
        self.assertFalse(page["cut"])
        code, page = self.rig.ask({"v": 1, "op": "review", "disk": "d2", "change": "removed", "offset": 0})
        self.assertEqual([e["path"] for e in page["entries"]], ["a [weird]*name?.txt"])
        code, page = self.rig.ask({"v": 1, "op": "review", "q": "F-069", "offset": 0})
        self.assertEqual(page["total"], 10)

    def test_run_returns_one_task_with_its_messages(self):
        code, tasks = self.rig.ask({"v": 1, "op": "read", "what": ["tasks"]})
        number = tasks["tasks"]["history"][0]["number"]
        code, answer = self.rig.ask({"v": 1, "op": "run", "number": number})
        self.assertEqual((code, answer["task"]["number"]), (0, number))
        self.assertEqual(self.rig.ask({"v": 1, "op": "run", "number": 99999})[1]["error"], "no_record")

    def test_run_says_whether_the_daemon_marked_a_stop_on_the_latest_task(self):
        daemon = self.rig.daemon
        daemon.queue("maintenance", ["up", "sync", "report"])
        daemon.begin()
        daemon.finish()
        sync = daemon.begin()
        for index in range(40):                            # more messages than the cockpit's 20
            sync["messages"].append({"level": "info", "text": f"line {index}"})
        code, answer = self.rig.ask({"v": 1, "op": "run", "number": sync["number"]})
        self.assertIs(answer["stop_requested"], False)
        self.assertEqual(len(answer["messages"]), 20)
        daemon.stop_running()
        code, answer = self.rig.ask({"v": 1, "op": "run", "number": sync["number"]})
        self.assertIs(answer["stop_requested"], True, "the stop message is the 41st: every message was read")
        self.assertEqual(answer["task"]["status"], "processing", "15.1 keeps processing while it saves")
        daemon.finish()
        daemon.run_all()
        code, answer = self.rig.ask({"v": 1, "op": "run", "number": sync["number"]})
        self.assertIs(answer["stop_requested"], True, "after the report the latest is the stopped step again")
        up = sync["number"] - 1
        self.assertIsNone(self.rig.ask({"v": 1, "op": "run", "number": up})[1]["stop_requested"],
                          "not the latest: its whole message list cannot be read")

    def test_a_204_activity_is_no_latest_task(self):
        self.rig.daemon.history, self.rig.daemon.active = [], []
        code, answer = self.rig.ask({"v": 1, "op": "read", "what": ["activity"]})
        self.assertEqual((code, answer["activity"]), (0, None))

    def test_a_429_read_is_asked_once_more(self):
        self.rig.daemon.rate_seconds = 30.0
        self.rig.daemon._cached = None
        self.rig.daemon._last_check = __import__("time").monotonic()
        code, answer = self.rig.ask({"v": 1, "op": "read", "what": ["state"]})
        self.assertEqual(answer.get("error"), "busy")       # the mock keeps refusing: asked twice, then reported
        self.assertEqual(self.rig.slept, [1.0])


class BeforeActingTests(LinkCase):
    """T31, T32 and the health rule of §2.4, checked by the link before any POST."""

    def test_auth_off_refuses_every_read_and_action(self):
        self.rig.daemon.auth = False
        self.assertEqual(self.rig.ask({"v": 1, "op": "read", "what": ["state"]})[1]["error"], "auth_off")
        self.assertEqual(self.act("hold_off", {"enabled": True})[1]["error"], "auth_off")
        code, info = self.rig.ask({"v": 1, "op": "info"})
        self.assertIs(info["auth_on"], False)
        self.assertEqual([m for m, _ in sent(self.rig) if m == "POST"], [])

    def test_info_reports_versions_and_the_login(self):
        code, info = self.rig.ask({"v": 1, "op": "info"})
        self.assertEqual(code, 0, info)
        self.assertIs(info["auth_on"], True)
        self.assertEqual((info["engine_version"], info["daemon_version"]), ("15.1", "2.1"))
        self.assertEqual(info["link"]["allow"], ["read", "act", "accept"])

    def test_old_or_unknown_engines_refuse_every_action(self):
        for version in ("13.0", "14.10", "", "v0"):
            with self.subTest(version=version):
                self.rig.daemon.engine_version = version
                code, answer = self.act("hold_off", {"enabled": True})
                self.assertEqual(answer["error"], "engine_too_old")
        self.assertFalse(sd.engine_ok(None))
        self.assertTrue(sd.engine_ok("15.1") and sd.engine_ok("16.0"))
        self.assertNotIn("POST", [m for m, _ in sent(self.rig)])

    def test_bad_health_refuses_runs_but_not_skip_or_stop(self):
        self.rig.daemon.health = "degraded"
        self.assertEqual(self.act("maintenance")[1]["error"], "health")
        self.assertEqual(self.act("heal")[1]["error"], "health")
        code, answer = self.act("hold_off", {"enabled": True})
        self.assertEqual(answer["outcome"], "succeeded")


class ActTests(LinkCase):
    """T34's link part (the binding), T35 outcomes, T38 stop."""

    def test_maintenance_sends_the_daemon_body_and_records_before(self):
        started = self.started_at()
        code, answer = self.act("maintenance", {}, {"started_at": started, "nothing_queued": True,
                                                    "thresholds": {"deletes": 20, "updates": 50}})
        self.assertEqual((code, answer["outcome"], answer["status"]), (0, "succeeded", 202), answer)
        posted = [r for r in self.rig.daemon.requests if r["method"] == "POST"]
        self.assertEqual(json.loads(posted[0]["body"]), {"ignore_thresholds": False, "spindown_on_finish": False})
        self.assertEqual(answer["before"]["highest"], 7)
        lines = self.rig.journal()
        self.assertEqual([("action" in l, l.get("outcome")) for l in lines], [(True, None), (False, "succeeded")])

    def test_a_changed_fact_refuses_and_sends_nothing(self):
        started = self.started_at()
        for expect in ({"started_at": started - 100}, {"thresholds": {"deletes": 99, "updates": 50}},
                       {"schedule": "02:00"}, {"hold_off": True}, {"blocks_bad": 3}, {"health": "passed"},
                       {"prevent_truncations": False}, {"active_number": 5},
                       {"scan": {"last_command": "sync", "last_command_at": "x", "last_sync_at": None,
                                 "counts": {}}}):
            with self.subTest(expect=expect):
                code, answer = self.act("maintenance", {}, expect)
                self.assertEqual(answer["error"], "changed", answer)
        self.rig.daemon.queue("maintenance", ["up", "sync", "report"])
        self.assertEqual(self.act("maintenance", {}, {"nothing_queued": True})[1]["error"], "changed")
        self.assertEqual([m for m, _ in sent(self.rig) if m == "POST"], [])

    def test_refusals_are_failed(self):
        self.rig.daemon.queue("heal", ["up", "fix", "scrub", "report"])
        code, answer = self.act("heal")
        self.assertEqual((answer["outcome"], answer["status"]), ("failed", 409))
        self.assertEqual(answer["message"], "Heal already running or scheduled")

    def test_5xx_is_unknown(self):
        for status in (500, 503):
            with self.subTest(status=status):
                self.rig.daemon.fail_with = status
                code, answer = self.act("heal")
                self.assertEqual((answer["outcome"], answer["status"]), ("unknown", status))

    def test_no_connection_is_failed_never_left_and_a_reset_is_unknown(self):
        self.rig.daemon.reset_after_request = False
        client = sd.DaemonClient("http://127.0.0.1:9", lambda: "Basic x", connect_timeout=0.5)
        with self.assertRaises(sd.Transport) as caught:
            client.request("POST", "/snapraid/v1/heal", body={})
        self.assertEqual(caught.exception.kind, "never_left")
        link = sd.Link(self.rig.paths, **self.rig.link_kwargs())
        link.client = sd.DaemonClient(self.rig.daemon.url, lambda: "Basic x")
        self.rig.daemon.reset_after_request = True
        reply, outcome, _ = link.send("heal", {"spindown_on_finish": False})
        self.assertEqual((reply, outcome), (None, "unknown"))
        self.rig.daemon.kill()
        reply, outcome, words = link.send("heal", {"spindown_on_finish": False})
        self.assertEqual((reply, outcome), (None, "failed"))
        self.assertIn("never left", words)

    def test_a_429_is_sent_once_more_after_its_second_with_the_binding_checked_again(self):
        started = self.started_at()
        original = self.rig.daemon.post
        calls = []

        def post(name, body):
            calls.append(name)
            if len(calls) == 1:
                return 429, b"", {"Retry-After": "1"}
            return original(name, body)
        self.rig.daemon.post = post
        code, answer = self.act("maintenance", {}, {"started_at": started})
        self.assertEqual((answer["outcome"], len(calls)), ("succeeded", 2))
        self.assertEqual(self.rig.slept, [1.0])

    def test_stop_is_bound_to_the_running_step(self):
        self.rig.daemon.queue("maintenance", ["up", "sync", "report"])
        self.rig.daemon.begin()
        number = self.rig.daemon.active[0]["number"]
        self.assertEqual(self.act("stop", {}, {"active_number": number + 1})[1]["error"], "changed")
        code, answer = self.act("stop", {}, {"active_number": number})
        self.assertEqual((answer["outcome"], answer["number"]), ("succeeded", number))
        self.rig.daemon.finish()
        self.rig.daemon.run_all()
        code, answer = self.act("stop", {}, {})
        self.assertEqual((answer["outcome"], answer["status"], answer["message"]), ("failed", 409, "No task running"))

    def test_hold_off_body(self):
        code, answer = self.act("hold_off", {"enabled": True})
        self.assertEqual((answer["outcome"], answer["status"], answer["message"]),
                         ("succeeded", 200, "Hold off enabled"))
        self.assertIs(self.rig.daemon.array["hold_off"], True)
        self.assertEqual(self.act("hold_off", {})[1]["error"], "bad_field")
        self.assertEqual(self.act("stop", {"now": True})[1]["error"], "bad_field")


class OneSendTests(LinkCase):
    """The binding and the one send are the link's rules too (design section 6.1), not only the harness's: an act
    without its binding, a second act of one approval, and a POST after the deadline are refused, nothing sent."""

    def test_an_act_without_every_bound_fact_sends_nothing(self):
        for action, body in (("stop", {}), ("hold_off", {"enabled": True}), ("maintenance", {}), ("heal", {}),
                             ("maintenance", {"ignore_thresholds": True}),
                             ("undelete", {"paths": [{"disk": "d1", "path": "docs/one.bin"}]})):
            kind = "accept" if body.get("ignore_thresholds") else action
            with self.subTest(kind=kind):
                code, answer = self.rig.ask({"v": 1, "op": "act", "approval": "A-5", "action": action, "body": body,
                                             "expect": {}})
                self.assertEqual((code, answer["error"]), (2, "no_binding"))
                self.assertEqual(answer["missing"], list(sd.REQUIRED_BINDING[kind]))
        expect = self.rig.expect("maintenance")
        code, answer = self.rig.ask({"v": 1, "op": "act", "approval": "A-5", "action": "maintenance", "body": {},
                                     "expect": {**expect, "nothing_queued": False}})
        self.assertEqual(answer["error"], "no_binding")
        self.assertEqual([m for m, _ in sent(self.rig) if m == "POST"], [])

    def test_one_approval_is_sent_once_whoever_asks(self):
        code, answer = self.act("hold_off", {"enabled": True}, approval="A-9")
        self.assertEqual(answer["outcome"], "succeeded")
        for action, body in (("hold_off", {"enabled": True}), ("hold_off", {"enabled": False}), ("heal", {})):
            with self.subTest(action=action):
                code, answer = self.act(action, body, approval="A-9")
                self.assertEqual((code, answer["error"]), (2, "used"))
        self.assertEqual(len([m for m, _ in sent(self.rig) if m == "POST"]), 1)
        code, journal = self.rig.ask({"v": 1, "op": "journal"})
        self.assertEqual([line["approval"] for line in journal["lines"] if "action" in line], ["A-9"])

    def test_no_post_after_the_deadline(self):
        offset = [0.0]
        self.rig.clock = lambda: __import__("time").time() + offset[0]
        original = sd.Link.check_act

        def slow_checks(link, *args):
            original(link, *args)
            offset[0] += sd.SEND_DEADLINE + 1               # the reads before the POST took too long
        sd.Link.check_act = slow_checks
        self.addCleanup(setattr, sd.Link, "check_act", original)
        code, answer = self.act("hold_off", {"enabled": True})
        self.assertEqual((code, answer["error"]), (2, "too_slow"))
        self.assertEqual([m for m, _ in sent(self.rig) if m == "POST"], [])
        self.assertEqual([l for l in self.rig.journal() if "action" in l], [], "no intent: nothing was sent")

    def test_the_deadline_and_the_harness_wait_fit_the_unit(self):
        """The link's last POST ends before systemd stops the instance (RuntimeMaxSec), and the harness waits
        longer than that, so its read-only look comes when the link can no longer send (DESIGN §40)."""
        from swfactory import snapraidd
        unit = (sd_fixtures().parent.parent.parent / "systemd" / "seraphina-snapraidd@.service").read_text()
        runtime = float(__import__("re").search(r"^RuntimeMaxSec=([0-9]+)$", unit, __import__("re").M).group(1))
        self.assertLess(sd.SEND_DEADLINE + sd.CONNECT_TIMEOUT + sd.READ_TIMEOUT, runtime)
        self.assertGreaterEqual(snapraidd.ACT_TIMEOUT, runtime + 10)

    def test_a_journal_failure_after_the_post_keeps_the_outcome(self):
        original = sd.Link.journal
        calls = []

        def journal(link, line):
            calls.append(line)
            if "outcome" in line:
                raise OSError(28, "No space left on device")
            return original(link, line)
        sd.Link.journal = journal
        self.addCleanup(setattr, sd.Link, "journal", original)
        code, answer = self.act("maintenance")
        self.assertEqual((code, answer["outcome"], answer["status"]), (0, "succeeded", 202))
        self.assertEqual(len([m for m, _ in sent(self.rig) if m == "POST"]), 1)


class UndeleteTests(LinkCase):
    """T42's link part: only removed entries of the current scan, exact escaped patterns, refusals before sending."""

    def test_patterns_escape_and_anchor(self):
        self.assertEqual(sd.pattern_of("docs/a.bin"), "/docs/a.bin")
        self.assertEqual(sd.pattern_of("a [b]*c?.txt"), "/a \\[b\\]\\*c\\?.txt")
        self.assertEqual(sd.pattern_of("back\\slash"), "/back\\\\slash")
        self.assertEqual(sd.pattern_of("photos", folder=True), "/photos/")
        for bad in ("", "/", "a//b", "./a", "a/../b", "a\0b", "a\nb", "x" * 4096):
            with self.subTest(bad=bad[:20]), self.assertRaises(sd.Refused):
                sd.pattern_of(bad)

    def test_only_removed_entries_of_the_scan(self):
        self.rig.daemon.set_scan({"d1": ["docs/one.bin", "docs/two [x].bin"]})
        code, answer = self.act("undelete", {"paths": [{"disk": "d1", "path": "docs/two [x].bin"}]})
        self.assertEqual(answer["outcome"], "succeeded", answer)
        body = json.loads([r for r in self.rig.daemon.requests if r["method"] == "POST"][0]["body"])
        self.assertEqual(body, {"filters": ["/docs/two \\[x\\].bin"], "filter_disks": ["d1"],
                                "spindown_on_finish": False})
        self.assertEqual(self.rig.journal()[0]["paths"], 1)
        self.assertNotIn("two", json.dumps(self.rig.journal()))
        self.rig.daemon.run_all()
        code, fixes = self.rig.ask({"v": 1, "op": "fixes", "offset": 0})
        self.assertEqual(fixes["fix_recovered"], 1)

    def test_one_disk_per_undelete(self):
        """The daemon passes -d and -f as two lists, so two disks would bring back every chosen name on both."""
        self.rig.daemon.set_scan({"d1": ["docs/a.bin"], "d2": ["photos/b.jpg"]})
        code, answer = self.act("undelete", {"paths": [{"disk": "d1", "path": "docs/a.bin"},
                                                       {"disk": "d2", "path": "photos/b.jpg"}]})
        self.assertEqual((code, answer["error"]), (2, "one_disk"))
        self.assertEqual([m for m, _ in sent(self.rig) if m == "POST"], [])

    def test_refused_before_sending(self):
        self.rig.daemon.set_scan({"d1": ["docs/one.bin"]})
        self.assertEqual(self.act("undelete", {"paths": [{"disk": "d1", "path": "docs/other.bin"}]})[1]["error"],
                         "changed")
        self.assertEqual(self.act("undelete", {"paths": [{"disk": "d9", "path": "docs/one.bin"}]})[1]["error"],
                         "health")
        self.rig.daemon.disks["data_disks"][0]["devices"] = []
        self.assertEqual(self.act("undelete", {"paths": [{"disk": "d1", "path": "docs/one.bin"}]})[1]["error"],
                         "health")
        self.assertEqual(self.act("undelete", {"paths": []})[1]["error"], "bad_field")
        self.assertEqual(self.act("undelete", {"paths": [{"disk": "d1", "path": "../etc"}]})[1]["error"], "bad_path")
        self.assertEqual([m for m, _ in sent(self.rig) if m == "POST"], [])


class ForwarderTests(unittest.TestCase):
    """T44: the forwarder's arguments, its report reading, its one POST and its exit status."""

    def setUp(self):
        self.rig = LinkRig(("read",))
        self.addCleanup(self.rig.close)
        (self.rig.root / "etc/seraphina/snapraidd-hook.header").write_text(
            "Authorization: Bearer " + "t" * 43 + "\n", encoding="utf-8")
        self.posts = []

    def run_notify(self, argv, report=b"", status=202):
        def post(url, authorization, key, event):
            self.posts.append({"url": url, "authorization": authorization, "key": key, "event": event})
            return status
        stdin = io.BytesIO(report)
        code = sd.main(["notify", *argv], stdin=stdin, stdout=io.StringIO(), paths=self.rig.paths,
                       lstat=self.rig.lstat, post=post, clock=lambda: 1_800_000_000.0)
        return code, stdin

    def test_a_report_becomes_one_typed_event_and_its_text_is_dropped(self):
        hostile = ("[ERROR] removed /mnt/d1/Private Name.doc Bearer abc " * 40000).encode()
        code, stdin = self.run_notify(["result", "--narrow", "error", "maintenance", "2", "[ERROR] SnapRAID Maintenance"],
                                      hostile)
        self.assertEqual(code, 0)
        self.assertEqual(stdin.tell(), min(len(hostile), sd.MAX_REPORT))
        self.assertEqual(len(self.posts), 1)
        event = self.posts[0]["event"]
        self.assertEqual(event, {"kind": "parity.report", "subject": "maintenance", "detail": "level=error code=2"})
        self.assertTrue(self.posts[0]["key"].startswith("snapraidd-"))
        self.assertNotIn("Private", json.dumps(self.posts))

    def test_every_recorded_argument_line_is_accepted(self):
        rows = json.loads((sd_fixtures() / "notify" / "result-argv.json").read_text(encoding="utf-8"))
        for row in rows:
            # the lab passed "%s" "%l" "%n" "%t" "%h" "%c"; the forwarder's line passes %l %h %c "%s"
            words = row["argv"].split(" ")
            subject = " ".join(words[:3])
            level, kind, code = words[3], words[-2], words[-1]
            with self.subTest(argv=row["argv"]):
                self.assertEqual(self.run_notify(["result", level, kind, code, subject])[0], 0)

    def test_arguments_outside_the_fixed_sets_send_nothing(self):
        for argv in (["result", "error", "maintenance", "2", "/mnt/d1/x"], ["result", "loud", "maintenance", "2",
                     "[ERROR] SnapRAID Maintenance"], ["result", "error", "schedule", "2", "[ERROR] SnapRAID X"],
                     ["result", "error", "maintenance", "two", "[ERROR] SnapRAID Maintenance"], ["send"], []):
            with self.subTest(argv=argv):
                self.assertEqual(self.run_notify(argv)[0], 2)
        self.assertEqual(self.posts, [])

    def test_exit_1_without_2xx_or_a_safe_header(self):
        self.assertEqual(self.run_notify(["test"], status=401)[0], 1)
        self.assertEqual(self.run_notify(["test"], status=0)[0], 1)
        self.rig.lstat.spoiled[str(self.rig.root / "etc/seraphina/snapraidd-hook.header")] = (0, 0o044)
        posts = len(self.posts)
        self.assertEqual(self.run_notify(["test"])[0], 1)
        self.assertEqual(len(self.posts), posts)

    def test_the_token_is_never_on_its_command_line(self):
        line = "/usr/local/libexec/seraphina/sf-snapraidd notify result --narrow %l %h %c \"%s\""
        self.assertLess(len(line), 1024)
        self.assertNotIn("Bearer", line)


def sd_fixtures():
    from tests.mock_snapraidd import FIXTURES
    return FIXTURES


class ExplainTests(unittest.TestCase):
    def test_explain_names_the_allow_change(self):
        rig = LinkRig(("read",))
        self.addCleanup(rig.close)
        new = {**rig.cfg, "allow": ["read", "act"]}
        (rig.root / "etc/seraphina/snapraidd.json.new").write_text(json.dumps(new), encoding="utf-8")
        out = io.StringIO()
        code = sd.main(["explain"], stdout=out, paths=rig.paths, lstat=rig.lstat)
        self.assertEqual(code, 0)
        self.assertIn("reading → reading, asking for runs", out.getvalue())
        self.assertIn("only while the AI agents run in Seraphina's sandbox", out.getvalue())
        self.assertNotIn("canary", out.getvalue())
        self.assertEqual(sd.main(["frobnicate"], stdout=io.StringIO(), paths=rig.paths), 2)


if __name__ == "__main__":
    unittest.main()
