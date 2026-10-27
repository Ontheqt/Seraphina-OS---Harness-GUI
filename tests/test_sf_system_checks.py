"""Tests for bin/sf-system's phases P1b and P1c (DESIGN.md §42; docs/seraphina/YOUR_SERVER.md §6.3-§6.10, §7.2,
§8.1, §12.2): T8 the Docker API client (only the fixed calls, never while Docker is stopped), T14 rids, jobs and
receipts, T18 the checks' parsers (smartctl's exit bits, needrestart, the journal's size, du), T20's keep pass, T21 SSH
keys, T22 and T22b sudo and root services, T23's helper part (rows keyed by kind, subject and target, never a
container id), T34 the compose verb's hashes and images, T35 the frozen redactor and the logs verb. The helper runs in
the placeholder server of tests/sfsystem_rig.py; every name, address, key and log line is made up.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

from swfactory.blueprint.redact import Redactor
from tests.sfsystem_rig import ACCESS, FIXTURES, IDS, KATE, SERAPHINA, Rig, sx

PRIVATE = {"read", "private", "refresh", "keep"}


class RigCase(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = Rig()
        self.addCleanup(self.rig.cleanup)
        self.rig.config(allow=sorted(PRIVATE), callers={str(SERAPHINA): sorted(PRIVATE), str(KATE): ["read", "keep"]})

    def ask(self, verb, uid=None, **fields):
        return self.rig.ask(verb, uid, **fields)


class DockerApiTests(RigCase):
    """T8: only the fixed method and path pairs; never a call while docker.service is not active."""

    def allowed(self, method, path, klass="read"):
        return any(m == method and pattern.fullmatch(path) and k == klass for m, pattern, k in sx.DOCKER_CALLS)

    def test_only_the_fixed_calls(self):
        for call in ("containers", "health", "images", "df", "info"):
            with self.subTest(call=call):
                code, answer = self.ask("docker", call=call)
                self.assertEqual(code, 0, answer)
                self.assertEqual(answer["source"], "api")
        code, answer = self.ask("docker", call="container", container=IDS["photos"])
        self.assertEqual(answer["health"][IDS["photos"]]["status"], "healthy")
        self.assertTrue(self.rig.system.docker_calls)
        for method, path in self.rig.system.docker_calls:
            self.assertTrue(self.allowed(method, path), (method, path))
            self.assertEqual(method, "GET", "the read class only asks")
        helper = sx.Helper(self.rig.paths, self.rig.system, self.rig.lstat, environ={"SUDO_UID": str(SERAPHINA)})
        for method, path, klass in (("DELETE", "/images/x", "read"), ("POST", "/containers/" + "1" * 64 + "/restart",
                                                                      "read"),
                                    ("POST", "/images/prune", "read"), ("POST", "/build/prune", "read"),
                                    ("GET", "/containers/json", "read"), ("GET", "/containers/../json", "read"),
                                    ("POST", f"/images/{'a' * 64}/tag?repo=seraphina-keep/x&tag=20261026-aaaaaaaaaaaa",
                                     "read")):
            with self.subTest(path=path), self.assertRaises(sx.Refused) as caught:
                helper.docker_api(method, path, klass)
            self.assertEqual(caught.exception.code, "docker_call_refused")

    def test_health_only_and_never_the_checks_output(self):
        code, answer = self.ask("docker", call="health")
        self.assertEqual(set(answer["health"]), {IDS["photos"], IDS["notes"]})
        self.assertEqual(answer["health"][IDS["notes"]], {"status": "unhealthy", "failing_streak": 17, "running": True})
        self.assertNotIn("PLANTED", json.dumps(answer))

    def test_docker_is_never_started(self):
        self.rig.docker_active = False
        for call in ("containers", "health", "images", "df", "info"):
            code, answer = self.ask("docker", call=call)
            self.assertEqual((code, answer["source"], answer["docker_active"]), (0, "state_files", False))
        self.assertEqual(self.rig.system.docker_calls, [], "never a connect while Docker is stopped")
        code, answer = self.ask("docker", call="containers")
        self.assertTrue(any(c["id"] == IDS["photos"] for c in answer["containers"]), "from the state files")
        self.assertEqual(self.ask("keep.images", rid="a" * 32)[1]["error"], "docker_stopped")

    def test_switched_off_and_the_answers(self):
        self.rig.config(docker_api=False, allow=["read"], callers={str(SERAPHINA): ["read"]})
        self.assertEqual(self.ask("docker", call="images")[1]["error"], "docker_api_off")
        self.rig.config(allow=["read"], callers={str(SERAPHINA): ["read"]})
        df = self.ask("docker", call="df")[1]["df"]
        self.assertEqual(len(df["build_cache"]), 5)
        self.assertEqual({v["name"] for v in df["volumes"]}, {"media_ts", "old_data"})
        info = self.ask("docker", call="info")[1]["info"]
        self.assertEqual((info["version"], info["containerd_store"]), ("29.9.1", True))
        self.assertEqual(sx.parse_http(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n4\r\n[1,2\r\n2\r\n]\n\r\n0"
                                       b"\r\n\r\n"), (200, b"[1,2]\n"))
        self.assertEqual(sx.parse_http(b"garbage"), (None, b""))


class ComposeHashTests(RigCase):
    """T34: hashes and images only; no file content or variable name; a failure is one fixed error."""

    def test_hashes_and_images(self):
        code, answer = self.ask("compose", dir="/opt/stacks/photos")
        self.assertEqual(code, 0, answer)
        self.assertEqual(answer["hashes"], {"photos": "a" * 64, "db": "a" * 64, "ts": "a" * 64})
        self.assertIn("mariadb:11", answer["images"])
        text = json.dumps(answer)
        for word in ("PLANTED", "DB_PASSWORD", "environment"):
            self.assertNotIn(word, text)
        for call in self.rig.system.calls:
            if call["argv"][0] == self.rig.paths.compose:
                self.assertIs(call["stderr"], False, "Compose's standard error is never read")
        tails = [c["argv"][-3:] for c in self.rig.system.calls if c["argv"][0] == self.rig.paths.compose]
        self.assertEqual(tails, [["config", "--format", "json"], ["config", "--hash", "*"],
                                 ["--project-directory", "/opt/stacks/photos", "config", "--images"][-3:]][:2] +
                         [tails[2]])
        self.assertEqual(tails[2][-2:], ["config", "--images"])

    def test_a_wrong_line_or_a_failure_fails_the_whole_answer(self):
        self.rig.compose_bad_line = True
        self.assertEqual(self.ask("compose", dir="/opt/stacks/photos")[1],
                         {"ok": False, "error": "compose_parse_failed", "words": "Compose could not read this app's folder."})
        self.rig.compose_bad_line = False
        self.rig.compose_images["/opt/stacks/photos"] = ["photoprism/photoprism:241021", "has space DB_PASSWORD=PLANTED"]
        answer = self.ask("compose", dir="/opt/stacks/photos")[1]
        self.assertEqual(answer["error"], "compose_parse_failed")
        self.assertNotIn("PLANTED", json.dumps(answer))


class CheckParserTests(unittest.TestCase):
    """T18: the helper's parsers on recorded shapes."""

    def smart(self, name):
        text = (FIXTURES / "smart" / f"{name}.json").read_text()
        return sx.parse_smart(json.loads(text)["smartctl"]["exit_status"], text)

    def test_smart_exit_bits(self):
        self.assertEqual(self.smart("sda")["state"], "ok")
        self.assertEqual(self.smart("nvme0n1")["attributes"]["percentage_used"], 3)
        failing = self.smart("sdb")
        self.assertEqual(failing["state"], "failing")
        self.assertEqual((failing["attributes"]["reallocated"], failing["attributes"]["failed"]),
                         (2312, ["Reallocated_Sector_Ct"]))
        self.assertEqual(self.smart("sdc")["state"], "asleep", "exit 3 with -n standby,3")
        nobody = self.smart("nobody")
        self.assertEqual(nobody["state"], "unreadable", "exit status 2 as nobody is can't read, never failing")
        self.assertNotIn("failing", nobody["bits"][0])
        self.assertEqual(sx.parse_smart(64, "{}")["state"], "warning")
        self.assertEqual(sx.parse_smart(None, "")["state"], "unknown")

    def test_needrestart(self):
        parsed = sx.parse_needrestart((FIXTURES / "checks" / "needrestart.txt").read_text())
        self.assertEqual(parsed["kernel_status"], "version_upgrade")
        self.assertEqual(parsed["services"], ["example-notes.service", "cron.service", "ssh.service", "dbus.service"])
        self.assertEqual(parsed["sessions"], 1)
        self.assertNotIn("owner", json.dumps(parsed), "sessions are counted, never named")
        self.assertFalse(sx.parse_needrestart("garbage")["parsed"])

    def test_journal_and_du(self):
        self.assertEqual(sx.parse_journal_usage((FIXTURES / "checks" / "journal-usage.txt").read_text()),
                         int(3.1 * (1 << 30)))
        self.assertIsNone(sx.parse_journal_usage((FIXTURES / "checks" / "journal-none.txt").read_text()),
                          "No journal files were found is unknown, never 0")
        total, rows = sx.parse_du((FIXTURES / "checks" / "du.txt").read_text())
        self.assertEqual((total, rows[0]["path"]), (160000000000, "/srv/data1/media"))


class CheckVerbTests(RigCase):
    def test_smart_disks_by_key_never_waking_one(self):
        code, answer = self.ask("smart")
        self.assertEqual(code, 0)
        states = {d["name"]: d["state"] for d in answer["disks"]}
        self.assertEqual(states, {"nvme0n1": "ok", "sda": "ok", "sdb": "failing", "sdc": "asleep"})
        self.assertTrue(all(len(d["key"]) == 16 for d in answer["disks"]))
        argvs = [c["argv"] for c in self.rig.system.calls if c["argv"][0].endswith("smartctl")]
        self.assertTrue(all(a[1:6] == ["-H", "-A", "-j", "-n", "standby,3"] for a in argvs))
        self.rig.config(smart_types={"EXAMPLESN0004": "usbjmicron"}, allow=sorted(PRIVATE),
                        callers={str(SERAPHINA): sorted(PRIVATE)})
        self.rig.system.calls.clear()
        self.ask("smart")
        usb = next(c["argv"] for c in self.rig.system.calls if c["argv"][-1] == "/dev/sdc")
        self.assertEqual(usb[-3:], ["-d", "usbjmicron", "/dev/sdc"])
        self.rig.config(smart_types={"EXAMPLESN0004": "--scan"}, allow=sorted(PRIVATE),
                        callers={str(SERAPHINA): sorted(PRIVATE)})
        self.assertEqual(self.ask("smart")[1]["error"], "unsafe_install", "a type outside the fixed list")

    def test_needrestart_journal_and_du(self):
        answer = self.ask("needrestart")[1]
        self.assertEqual((answer["state"], answer["kernel_status"]), ("ok", "version_upgrade"))
        argv = next(c for c in self.rig.system.calls if c["argv"][0].endswith("needrestart"))["argv"]
        self.assertEqual(argv[1:], ["-b", "-r", "l"], "list only: it restarts nothing")
        self.rig.write("/var/log/journal/" + "0" * 32 + "/system@0001-0002.journal", b"x" * 100)
        self.rig.write("/var/log/journal/" + "0" * 32 + "/system.journal", b"x" * 50)
        answer = self.ask("journal")[1]
        self.assertEqual((answer["archived_count"], answer["archived_bytes"]), (1, 100))
        code, answer = self.ask("du", mount="/")
        self.assertEqual((code, answer["folders"][0]["path"]), (0, "/srv/data1/media"))
        argv = next(c for c in self.rig.system.calls if c["argv"][0].endswith("nice"))["argv"]
        self.assertEqual([os.path.basename(argv[0]), argv[1], argv[2], os.path.basename(argv[3]), argv[4],
                          os.path.basename(argv[5])] + argv[6:-1],
                         ["nice", "-n", "19", "ionice", "-c3", "du", "-x", "-d", "2", "-B1", "--"])
        self.assertEqual(self.ask("du", mount="/proc")[1]["error"], "bad_field", "only one of the server's disks")


class JobTests(RigCase):
    """T14 (jobs and rids): a job in its own transient unit; `run` only inside it; one job at a time; the outcome
    in its record; a repeated rid starts nothing."""

    def cgroup(self, job, verb="apt.refresh"):
        self.rig.write("/proc/self/cgroup", f"0::/system.slice/{sx.unit_name(verb, job)}.service\n")

    def run_job(self, job):
        out = []
        code = sx.run_job(job, self.rig.paths, self.rig.system, self.rig.lstat, out=out.append)
        return code, "\n".join(out)

    def test_apt_refresh_in_a_unit(self):
        code, answer = self.ask("apt.refresh", rid="1" * 32)
        self.assertEqual(code, 0, answer)
        job = answer["job"]
        unit = self.rig.units[-1]
        self.assertIn(f"--unit=seraphina-system-apt-refresh-{job}", unit)
        self.assertIn("--property=SendSIGKILL=no", unit)
        self.assertNotIn("--property=PrivateNetwork=yes", unit, "apt-get update needs the network")
        self.assertEqual(unit[-3:], ["/usr/local/libexec/seraphina/sf-system", "run", job])
        self.assertTrue(any(p.startswith("--property=StandardOutput=append:/var/lib/seraphina-system/runs/")
                            for p in unit), "its output to its own file, never the journal")
        record = self.ask("job", job=job)[1]["job"]
        self.assertEqual((record["state"], record["verb"]), ("starting", "apt.refresh"))
        again = self.ask("apt.refresh", rid="1" * 32)[1]
        self.assertEqual((again["repeated"], again["job"]), (True, job))
        self.assertEqual(len(self.rig.units), 1, "a repeated rid starts nothing")
        code, text = self.run_job(job)
        self.assertEqual(code, 2, "not inside its unit")
        self.cgroup(job)
        code, text = self.run_job(job)
        self.assertEqual(code, 0, text)
        record = self.ask("job", job=job)[1]["job"]
        self.assertEqual((record["state"], record["outcome"], record["steps"][0]["argv"]),
                         ("ended", "ok", ["apt-get", "update"]))
        self.assertFalse(os.path.exists(self.rig.at(f"/run/seraphina/holds/system-{job}.json")), "the hold ends")
        log = self.ask("log", job=job)[1]
        self.assertIn("Reading package lists", log["text"])
        argv = next(c for c in self.rig.system.calls if c["argv"][0].endswith("apt-get"))["argv"]
        self.assertEqual(argv[1:], ["update"], "nothing but the lists")
        self.assertEqual(self.run_job(job)[0], 2, "a job runs once")

    def test_one_job_at_a_time_and_refusals(self):
        os.makedirs(self.rig.at(sx.RUN_DIR), exist_ok=True)
        import fcntl
        fd = os.open(self.rig.at(sx.RUN_DIR) / "job.lock", os.O_RDWR | os.O_CREAT)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            self.assertEqual(self.ask("apt.refresh", rid="2" * 32)[1]["error"], "busy")
            self.assertEqual(self.ask("info")[1]["job_running"], True)
        finally:
            os.close(fd)
        self.assertEqual(self.ask("receipts", rid="2" * 32)[1]["rid"]["outcome"], "refused")
        self.assertEqual(self.ask("receipts", rid="3" * 32)[1]["rid"]["state"], "not_started",
                         "no record and no lock: it never started")
        self.rig.config(allow=["read"], callers={str(SERAPHINA): ["read"]})
        self.assertEqual(self.ask("apt.refresh", rid="4" * 32)[1]["error"], "not_allowed")
        self.assertEqual(self.ask("apt.refresh")[1]["error"], "bad_field", "a rid is needed")

    def test_a_unit_that_does_not_start(self):
        self.rig.systemd_run_exit = 1
        code, answer = self.ask("apt.refresh", rid="5" * 32)
        self.assertEqual(answer["error"], "job_not_started")
        record = self.ask("job", job=answer["job"])[1]["job"]
        self.assertEqual(record["outcome"], "failed")


class KeepTests(RigCase):
    """T20's keep pass and T14's receipts: the current image of every running Compose service named, the previous
    one the first time, within the limits; nothing removed; one receipt with its way back; a repeated rid runs
    nothing."""

    def setUp(self):
        super().setUp()
        self.previous = "sha256:" + "c" * 64
        self.rig.extra_images.append({"Id": self.previous, "RepoTags": [], "RepoDigests": [
            "photoprism/photoprism@sha256:" + "d" * 64], "Created": 1759000000, "Size": 2_000_000_000, "Labels": {}})

    def tags(self):
        return {t for i in self.rig.images() for t in i["RepoTags"] if t.startswith("seraphina-keep/")}

    def test_the_keep_pass(self):
        code, answer = self.ask("keep.images", rid="a" * 32)
        self.assertEqual(code, 0, answer)
        kinds = {t["service"]: t["kind"] for t in answer["tagged"] if t["kind"] == "previous"}
        self.assertEqual(kinds, {"photos-photos": "previous"}, "the first time, the unnamed previous version too")
        names = self.tags()
        self.assertTrue(any(n.startswith("seraphina-keep/photos-photos:") for n in names))
        self.assertTrue(all(sx.KEEP_TAG_RE.fullmatch(n) for n in names), names)
        self.assertFalse(any("mediaold" in n and "previous" in n for n in names))
        for method, path in self.rig.system.docker_calls:
            self.assertIn(method, ("GET", "POST"))
            if method == "POST":
                self.assertRegex(path, r"^/images/[0-9a-f]{64}/tag\?repo=seraphina-keep/")
        receipt = self.ask("receipts", id=answer["receipt"])[1]["receipt"]
        self.assertEqual(set(receipt) >= {"schema", "id", "rid", "started", "ended", "caller", "verb", "class", "flags",
                                          "approval", "request", "before", "steps", "after", "way_back", "outcome",
                                          "verified", "not_verified"}, True)
        self.assertEqual((receipt["schema"], receipt["class"], receipt["way_back"]["kind"], receipt["outcome"]),
                         ("seraphina-system-receipt/1", "undo_instantly", "remove_names", "ok"))
        self.assertEqual(receipt["caller"], {"uid": SERAPHINA, "user": "seraphina"})
        calls = len(self.rig.system.docker_calls)
        again = self.ask("keep.images", rid="a" * 32)[1]
        self.assertEqual((again["repeated"], again["receipt"]), (True, answer["receipt"]))
        self.assertEqual(len(self.rig.system.docker_calls), calls, "a repeated rid asks Docker nothing")
        self.assertEqual(self.ask("receipts", rid="a" * 32)[1]["rid"]["record"]["id"], answer["receipt"])
        second = self.ask("keep.images", rid="b" * 32)[1]
        self.assertEqual((second["tagged"], second["outcome"], second["receipt"]), ([], "nothing", None),
                         "everything is kept already: no change, no receipt")
        listed = self.ask("receipts")[1]["receipts"]
        self.assertEqual([r["id"] for r in listed], [answer["receipt"]])

    def test_a_pass_docker_refuses_checks_nothing_and_has_nothing_to_undo(self):
        answer_of = self.rig.docker_answer
        self.rig.docker_answer = lambda method, path: (500, b'{"message": "refused"}') if method == "POST" and \
            "/tag?" in path else answer_of(method, path)
        code, answer = self.ask("keep.images", rid="7" * 32)
        self.assertEqual((code, answer["outcome"], answer["tagged"]), (0, "failed", []))
        receipt = self.ask("receipts", id=answer["receipt"])[1]["receipt"]
        self.assertEqual((receipt["outcome"], receipt["after"]["names"], receipt["verified"],
                          receipt["not_verified"]), ("failed", [], [], []), "no name added: nothing was checked")
        self.assertGreater(receipt["request"]["count"], 0)
        self.assertEqual(receipt["way_back"]["how"], "Nothing was named, so there is nothing to undo.")

    def test_receipts_a_page_at_a_time(self):
        """The cursor is the last id shown and the next page starts below it: the record at a page boundary is
        listed once, never skipped."""
        for n in range(1, 26):
            self.rig.write(f"{sx.STATE}/receipts/R-{n}.json", {"schema": "seraphina-system-receipt/1",
                                                               "id": f"R-{n}", "verb": "keep.images"}, 0o600)
        first = self.ask("receipts")[1]
        self.assertEqual([r["id"] for r in first["receipts"]], [f"R-{n}" for n in range(25, 5, -1)])
        self.assertEqual(first["next"], "R-6", "the last one shown")
        second = self.ask("receipts", cursor=first["next"])[1]
        self.assertEqual([r["id"] for r in second["receipts"]], [f"R-{n}" for n in range(5, 0, -1)])
        self.assertIsNone(second["next"])

    def test_within_the_limits(self):
        self.rig.config(keep={"images_per_app": 1, "max_gib": 1}, allow=sorted(PRIVATE),
                        callers={str(SERAPHINA): sorted(PRIVATE)})
        answer = self.ask("keep.images", rid="c" * 32)[1]
        self.assertFalse(any(t["kind"] == "previous" for t in answer["tagged"]), "2 GB is over a 1 GB limit")
        self.assertTrue(any(t["kind"] == "current" for t in answer["tagged"]), "the current version costs nothing yet")
        # an update: the current image moves on, the kept one becomes the previous version
        self.rig.config(keep={"images_per_app": 1, "max_gib": 50}, allow=sorted(PRIVATE),
                        callers={str(SERAPHINA): sorted(PRIVATE)})
        kept = next(t for t, ids in [(t, i) for i, tags in self.rig.keep_tags.items() for t in tags]
                    if t.startswith("seraphina-keep/photos-photos:"))
        self.assertTrue(kept)
        self.rig.keep_tags.setdefault(self.previous, []).append("seraphina-keep/photos-photos:20260901-cccccccccccc")
        self.rig.keep_tags.setdefault("sha256:" + "e" * 64, [])
        self.rig.extra_images.append({"Id": "sha256:" + "e" * 64, "RepoTags": ["seraphina-keep/photos-photos:20260801-"
                                                                              "eeeeeeeeeeee"], "RepoDigests": [],
                                      "Created": 1758000000, "Size": 1_000_000_000, "Labels": {}})
        answer = self.ask("keep.images", rid="d" * 32)[1]
        self.assertEqual(answer["apps"]["photos-photos"], {"previous": 2, "over_limit": True},
                         "over the limit is said, and nothing is removed in this version")
        self.assertFalse(any(m == "DELETE" for m, _ in self.rig.system.docker_calls))

    def test_the_owners_own_copies_count_as_kept(self):
        self.rig.moved_tags = {}
        answer = self.ask("keep.images", rid="e" * 32, prefixes=["mariadb"])[1]
        self.assertEqual(answer["apps"]["photos-db"], {"previous": 0, "over_limit": False, "owner_kept": True})
        self.assertFalse(any(t["service"] == "photos-db" for t in answer["tagged"]))

    def test_the_class_and_the_caller(self):
        self.rig.config(allow=["read"], callers={str(SERAPHINA): ["read"]})
        self.assertEqual(self.ask("keep.images", rid="f" * 32)[1]["error"], "not_allowed")
        self.rig.config(allow=sorted(PRIVATE), callers={str(SERAPHINA): sorted(PRIVATE), str(KATE): ["read", "keep"]})
        self.assertEqual(self.ask("keep.images", uid=KATE, rid="9" * 32)[0], 0, "keep stays open to a login account")


class RedactorAndLogsTests(RigCase):
    """T35: the frozen redactor equals the Blueprint's on one corpus; a page from the helper never holds a planted
    token; logs only for Seraphina's own account."""

    CORPUS = [
        "plain line with nothing secret",
        "password=hunter2 user=bob", "DB_PASSWORD: s3cr3t-value", '{"api_token": "abc123def", "ok": true}',
        "Authorization: Bearer abcdefghijklmnop", "curl https://user:pass@example.com/x", "--auth-token sekrit123",
        '["--client-secret", "value-here"]', "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJlLXg",
        "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk\n-----END OPENSSH PRIVATE KEY-----",
        "host:port 192.0.2.10:8080 stays", "SECRET_KEY = 'quoted value'", "token: ", "x" * 300,
        "Basic dXNlcjpwYXNzd29yZA== and Token zzzzzzzzzzzz", "auth=already [REDACTED]",
    ]

    def test_the_same_output_as_the_blueprint_redactor(self):
        for line in self.CORPUS:
            with self.subTest(line=line[:30]):
                self.assertEqual(sx.redact(line)[0], Redactor().text(line))

    def test_a_page_of_logs(self):
        code, answer = self.ask("logs", container=IDS["photos"], lines=10)
        self.assertEqual(code, 0, answer)
        texts = [line["text"] for line in answer["lines"]]
        self.assertEqual(len(texts), 4)
        self.assertNotIn("PLANTED", json.dumps(answer), "no planted token leaves the helper")
        self.assertIn("<img src=x onerror=alert('log')>", texts, "a hostile line stays text (the page escapes it)")
        self.assertGreaterEqual(answer["redacted"], 2)
        self.assertEqual(answer["lines"][0]["stream"], "stdout")
        answer = self.ask("logs", container=IDS["photos"], lines=10, before="2026-10-26T08:00:00Z")[1]
        self.assertEqual(len(answer["lines"]), 2, "older than before")
        self.rig.write_logs("photos", [("2026-10-26T09:00:00Z", "stdout", "new")],
                            rotated=[("2026-10-26T06:00:00Z", "stdout", "rotated")])
        texts = [line["text"] for line in self.ask("logs", container=IDS["photos"], lines=2)[1]["lines"]]
        self.assertEqual(texts, ["rotated", "new"], "the rotated copy is read too, oldest first")

    def test_journald_units_and_refusals(self):
        self.rig.journal_lines = [{"__REALTIME_TIMESTAMP": "1761465600000000", "MESSAGE": "started token=PLANTED-x",
                                   "PRIORITY": "6"},
                                  {"__REALTIME_TIMESTAMP": "1761465601000000", "MESSAGE": [104, 105], "PRIORITY": "3"}]
        answer = self.ask("logs", unit="ssh.service")[1]
        self.assertEqual([line["text"] for line in answer["lines"]], ["started token=[REDACTED]", "hi"])
        self.assertEqual(answer["lines"][1]["stream"], "stderr")
        argv = [c["argv"] for c in self.rig.system.calls if c["argv"][0].endswith("journalctl")][-1]
        self.assertEqual(argv[-1], "--unit=ssh.service", "a unit name is one --unit= argument")
        self.assertEqual(self.ask("logs", unit="gone.service")[1]["error"], "no_record")
        self.assertEqual(self.ask("logs", unit="-x.service")[1]["error"], "bad_field")
        self.assertEqual(self.ask("logs", container=IDS["photos"], unit="ssh.service")[1]["error"], "bad_field")
        code, answer = self.ask("logs", uid=KATE, container=IDS["photos"])
        self.assertEqual(answer["error"], "caller_is_login_account", "never for a person's login account")
        host = self.rig.at(f"/var/lib/docker/containers/{IDS['notes']}/hostconfig.json")
        data = json.loads(host.read_text())
        data["LogConfig"] = {"Type": "local"}
        host.write_text(json.dumps(data))
        self.assertEqual(self.ask("logs", container=IDS["notes"])[1]["error"], "log_driver_unsupported")


class AccessTests(RigCase):
    """T21, T22, T22b and T23's helper part: the enumeration on the placeholder server."""

    def scan(self):
        helper = sx.Helper(self.rig.paths, self.rig.system, self.rig.lstat, environ={})
        return sx.AccessScan(helper).run()

    def rows(self, result, kind=None):
        return [r for r in result["rows"] if kind is None or r["kind"] == kind]

    def test_ssh_keys(self):
        result = self.scan()
        keys = {r["target"]: r for r in self.rows(result, "ssh_key") if r["subject"] == "owner"}
        expected = [line.split()[1] for line in (ACCESS / "fingerprints.txt").read_text().split("\n") if line]
        self.assertEqual(sorted(keys), sorted(expected), "fingerprints equal ssh-keygen -lf's, both key files read")
        if shutil.which("ssh-keygen"):
            for name in ("authorized_keys", "authorized_keys2"):
                done = subprocess.run(["ssh-keygen", "-lf", str(ACCESS / name)], capture_output=True, text=True)
                for line in done.stdout.split("\n"):
                    if line:
                        self.assertIn(line.split()[1], keys)
        phone = next(r for r in keys.values() if "phone-key" in r["words"])
        self.assertIn("limited: from=192.0.2.0/24", phone["words"])
        laptop = next(r for r in keys.values() if "owner@laptop" in r["words"])
        self.assertIn("with no limits", laptop["words"])
        self.assertFalse(laptop["way"], "the owner's sudo asks for his password: half the way, not counted")
        root = [r for r in self.rows(result, "ssh_key") if r["subject"] == "root"]
        self.assertTrue(root and root[0]["way"], "a key for root, where root may sign in with keys")
        self.assertTrue(self.rows(result, "ssh_garbage"), "a garbage line is reported, not skipped")
        self.assertTrue(any(r["target"] == "password" for r in self.rows(result, "ssh_setting")))

    def test_keys_from_a_command_cannot_be_listed(self):
        text = (ACCESS / "sshd-T.txt").read_text().replace("authorizedkeyscommand none",
                                                           "authorizedkeyscommand /usr/bin/fetch-keys")
        self.rig.write("/tmp-fixture-sshd", text)
        original = self.rig.system.run

        def run(argv, timeout, env=None, stderr=False):
            if os.path.basename(argv[0]) == "sshd":
                return 0, text, False
            return original(argv, timeout, env, stderr)
        self.rig.system.run = run
        self.assertTrue(self.rows(self.scan(), "ssh_keys_command"))

    def test_sudo(self):
        result = self.scan()
        sudo = {r["subject"]: r for r in self.rows(result, "sudo")}
        self.assertEqual(set(sudo), {"owner", "backupsvc", "seraphina"})
        self.assertTrue(sudo["owner"]["way"] and "with its own password" in sudo["owner"]["words"])
        self.assertIn("/etc/sudoers", sudo["owner"]["words"], "the granting file from sudo -ll")
        self.assertTrue(sudo["backupsvc"]["way"], "a service account with no login shell and NOPASSWD is found")
        self.assertIn("without any password", sudo["backupsvc"]["words"])
        self.assertIn("tar", sudo["backupsvc"]["words"], "a command that can start others counts as full")
        self.assertFalse(sudo["seraphina"]["way"], "the helper alone is not full control")
        self.assertEqual(result["sudo"]["implementation"], "sudo")
        self.assertTrue(any(r["subject"] == "/etc/sudoers.d/old.rules" for r in self.rows(result, "sudoers_file")))
        self.assertNotIn(sx.CANNOT_SUDO_RS, result["cannot_check"])
        groups = {(r["subject"], r["target"]) for r in self.rows(result, "group")}
        self.assertTrue({("owner", "docker"), ("owner", "sudo"), ("owner", "lxd"), ("dockerpal", "docker")} <= groups)
        shadow = next(r for r in self.rows(result, "group") if r["target"] == "shadow")
        self.assertTrue(shadow["escape"] and not shadow["way"])
        self.assertTrue(self.rows(result, "root_password")[0]["way"])

    def test_two_sudo_programs(self):
        cargo = self.rig.paths.tool("sudo-rs")
        self.rig.write(cargo[len(str(self.rig.tmp)):], "#!/bin/false\n", 0o4755)
        os.unlink(self.rig.paths.sudo)
        os.symlink(cargo, self.rig.paths.sudo)
        self.rig.write("/usr/bin/sudo.ws", "#!/bin/false\n", 0o4755)
        self.rig.sudo_version = {"sudo-rs": "sudo-rs 0.2.13\n", "sudo.ws": "Sudo version 1.9.17p2\n"}
        self.rig.sudo_l = {"sudo-rs": {"owner": "sudo-l-owner.txt", "backupsvc": "sudo-l-backupsvc.txt"},
                           "sudo.ws": {"owner": "sudo-l-owner.txt", "backupsvc": "sudo-l-backupsvc.txt",
                                       "kate": "sudo-l-kate-restart.txt"}}
        self.rig.sudo_ll = {"sudo-rs": {"owner": "sudo-ll-owner.txt"}}
        result = self.scan()
        self.assertEqual(result["sudo"]["implementation"], "sudo-rs", "from where sudo resolves")
        self.assertEqual([p["name"] for p in result["sudo"]["programs"]], ["sudo", "sudo.ws"])
        self.assertTrue(self.rows(result, "sudo_programs"), "both are listed as doors")
        disagree = self.rows(result, "sudo_disagree")
        self.assertEqual([r["subject"] for r in disagree], ["kate"], "a rule one allows and the other refuses")
        self.assertTrue(any(r["subject"] == "backupsvc" and r["way"] for r in self.rows(result, "sudo")),
                        "the service account's rule under both programs")
        self.assertIn(sx.CANNOT_SUDO_RS, result["cannot_check"], "shown only under sudo-rs")
        self.assertIn(sx.CANNOT_TWO_SUDOS, result["cannot_check"])

    def test_cvtsudoers_names_not_reached(self):
        self.rig.write("/usr/bin/cvtsudoers", "#!/bin/false\n", 0o755)
        unknown = {r["subject"] for r in self.rows(self.scan(), "sudo_unknown")}
        self.assertEqual(unknown, {"oldadmin"}, "a rule for a name getent does not list")

    def test_root_services_and_cron(self):
        result = self.scan()
        home = self.rows(result, "root_service_home")
        self.assertEqual([(r["subject"], r["target"]) for r in home],
                         [("example-indexer.service", "/home/operator/indexer/run.py")],
                         "python3 -B <script>: the script is walked, not only /usr/bin/python3")
        services = {(r["subject"], r["target"]) for r in self.rows(result, "root_service")}
        self.assertIn(("example-shell.service", "/opt/example/start.sh"), services, "bash <script>")
        self.assertIn(("example-writable.service", "/opt/tools/bin/sync"), services, "a group-writable folder")
        self.assertIn(("example-indexer.service", "/home/operator/indexer/run.py"), services)
        self.assertFalse(any(s == "example-agent.service" for s, _ in services), "not a root service")
        self.lstat_spoil("/run/systemd/system/example-indexer.service.d/50-refresh.conf", 0, 0o002)
        services = {(r["subject"], r["target"]) for r in self.rows(self.scan(), "root_service")}
        self.assertIn(("example-indexer.service", "/run/systemd/system/example-indexer.service.d/50-refresh.conf"),
                      services, "a runtime drop-in is read with the rest")
        self.lstat_spoil("/opt/example/nightly.sh", 1000, 0)
        cron = {(r["subject"], r["target"]) for r in self.rows(self.scan(), "cron")}
        self.assertIn(("/etc/crontab", "/opt/example/nightly.sh"), cron)
        self.assertFalse(any("/home/owner/bin/poll" in t for _, t in cron), "a job that runs as owner is not root's")

    def lstat_spoil(self, path, uid, extra):
        self.rig.lstat.spoiled[str(self.rig.at(path))] = (uid, extra)

    def test_containers_setuid_and_capabilities(self):
        self.rig.container("broken", "monitor-agent-1", "example/monitor:1", pid=0, running=False,
                           labels={"com.docker.compose.project": "monitor", "com.docker.compose.service": "agent"},
                           mounts={"/run/dbus/system_bus_socket": {"Source": "/run/dbus/system_bus_socket",
                                                                    "Destination": "/run/dbus/system_bus_socket",
                                                                    "RW": True, "Type": "bind"}})
        result = self.scan()
        full = {r["subject"]: r for r in self.rows(result, "container") if r["way"]}
        self.assertIn("monitor/agent", full, "root in a container with the system bus is a way")
        self.assertIn("system bus", full["monitor/agent"]["words"])
        escape = {r["subject"] for r in self.rows(result, "container") if r["escape"]}
        self.assertIn("photos/db", escape, "root inside a container is listed apart")
        self.assertIn("filebrowser", escape, "a writable data disk")
        socket_rows = {r["subject"] for r in self.rows(result, "dockge")}
        self.assertEqual(socket_rows, {"dockge/dockge"})
        self.assertTrue(any(r["subject"] == "/usr/local/bin/helper-suid" for r in self.rows(result, "setuid")))
        self.assertFalse(any(r["subject"] == "/usr/bin/su" for r in self.rows(result, "setuid")))
        self.rig.dpkg_changed["passwd"] = "??5??????   /usr/bin/passwd\n"
        self.assertTrue(any(r["subject"] == "/usr/bin/passwd" and r["target"] == "changed"
                            for r in self.rows(self.scan(), "setuid")))
        caps = {r["subject"] for r in self.rows(result, "capability")}
        self.assertEqual(caps, {"/usr/local/bin/backup-reader"}, "ping's cap_net_raw is a package's and harmless")
        self.assertTrue(any(r["subject"] == "beszel-agent" for r in self.rows(result, "docker_client")))

    def test_rows_are_keyed_by_kind_subject_and_target(self):
        result = self.scan()
        text = json.dumps(result)
        for cid in IDS.values():
            self.assertNotIn(cid, text, "never a container id")
            self.assertNotIn(cid[:12], text)
        before = {r["id"] for r in result["rows"]}
        # Dockge recreated under a new id: the same rows
        old = self.rig.at(f"/var/lib/docker/containers/{IDS['dockge']}")
        new_id = "9" * 64
        config = json.loads((old / "config.v2.json").read_text())
        config["ID"] = new_id
        shutil.move(old, self.rig.at(f"/var/lib/docker/containers/{new_id}"))
        (self.rig.at(f"/var/lib/docker/containers/{new_id}") / "config.v2.json").write_text(json.dumps(config))
        after = {r["id"] for r in self.scan()["rows"]}
        self.assertEqual(before, after)
        self.assertEqual(result["counts"]["ways"], sum(1 for r in result["rows"] if r["way"]))
        self.assertFalse(any(r["way"] and r["escape"] for r in result["rows"]))
        self.assertGreaterEqual(len(result["cannot_check"]), 8, "the can't-check list is always there")

    def test_the_job_and_its_pages(self):
        code, answer = self.ask("access")
        self.assertEqual(code, 0, answer)
        job = answer["job"]
        self.assertIn("--property=PrivateNetwork=yes", self.rig.units[-1])
        self.rig.write("/proc/self/cgroup", f"0::/system.slice/{sx.unit_name('access', job)}.service\n")
        self.assertEqual(sx.run_job(job, self.rig.paths, self.rig.system, self.rig.lstat, out=lambda _t: None), 0)
        page = self.ask("access", read=True)[1]["result"]
        self.assertTrue(page["rows"] and page["total"] >= len(page["rows"]))
        self.assertEqual(self.ask("log", job=job)[1]["error"], "not_allowed", "its output stays on the server")
        self.assertEqual(self.ask("access", uid=KATE)[1]["error"], "caller_is_login_account")
        self.assertEqual(os.stat(self.rig.at(f"{sx.STATE}/access/last.json")).st_mode & 0o777, 0o600)

    def test_explain_access_for_the_owner(self):
        code, text = self.rig.main(["explain", "access"])
        self.assertEqual(code, 0, text)
        self.assertIn("Who can take full control of your server?", text)
        self.assertIn("What Seraphina can't see from here:", text)
        self.assertIn("Nothing was changed.", text)


if __name__ == "__main__":
    unittest.main()
