"""Tests for bin/sf-snapraid, the SnapRAID panel's root helper, phase P1 (DESIGN.md §41;
docs/seraphina/BACKUPS_SNAPRAID.md §8, §8.6, §13.2): T1 the log reader on every recorded 13.0 run, T2 outcomes, T3
"a sync is needed", T6 requests refused before anything starts, T7 the argv it builds, T8 the installation and
config checks and explain, T9 one job at a time, the guard as a report (L1 and L3's unit forms), and T25 the
scheduler inventory and the content fingerprint. The helper runs in a temporary tree (tests/snapraid_rig.py) with
tests/fake_snapraid.py as SnapRAID, answering from tests/fixtures/snapraid13/ (real 13.0 runs, placeholder paths).
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import fcntl
import io
import json
import os
import re
import threading
import time
import unittest
from pathlib import Path

from swfactory.snapraid import internal_terms
from tests.snapraid_rig import FIXTURES, HEALTHY, UUIDS, Rig, sf


def plain_terms(words: str) -> list:
    """The internal terms in a refusal's words once paths and quoted SnapRAID names ('parity', '2-parity': the
    config's own words, shown as names) are left out: the card's light shows those words (U7)."""
    words = re.sub(r"'[^']*'", "", words)
    return internal_terms(" ".join(word for word in words.split() if "/" not in word))


RUNS = json.loads((FIXTURES / "runs.json").read_text(encoding="utf-8"))["runs"]


def fixture(name: str) -> sf.Log:
    return sf.parse_log((FIXTURES / f"{name}.log").read_bytes())


def out_text(name: str) -> str:
    path = FIXTURES / f"{name}.out"
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


class LogReaderTests(unittest.TestCase):
    """T1: every recorded log reads into typed records; T2: outcomes; T3: a sync is needed."""

    def test_every_recorded_log_parses_into_tagged_lines(self):
        for name in RUNS:
            with self.subTest(run=name):
                log = fixture(name)
                self.assertTrue(log.lines, name)
                self.assertEqual(log.tagged("version")[0][1], "13.0")
                self.assertEqual(log.stray, [], "every line is a tag or continues a message")

    def test_fields_are_unescaped(self):
        self.assertEqual(sf.unescape(r"a\db\nc\rd\\e"), "a:b\nc\rd\\e")
        names = {path for disk, path, *_ in sf.read_list(fixture("list"))}
        for name in ("media/notes/a:b.txt", "media/notes/back\\slash.txt", "media/notes/line\nbreak.txt",
                     "media/notes/<img src=x onerror=alert(1)>.jpg", "media/notes/invoice‮gpj.exe"):
            self.assertIn(name, names)

    def test_an_untagged_continuation_attaches_to_its_fatal_message(self):
        log = fixture("diff-unmounted")
        fatal = log.text("fatal")
        self.assertRegex(fatal, r"present in disk 'd3' at dir '/mnt/disk3/'\nare now missing or have been rewritten")
        self.assertNotIn("msg:fatal:", fatal)

    def test_status_facts(self):
        ok = sf.read_status(fixture("status-ok"))
        self.assertEqual({name: d["files"] for name, d in ok["disks"].items()}, {"d1": 9, "d2": 12, "d3": 9})
        self.assertEqual((ok["has_unsynced"], ok["has_bad"]["count"], ok["block_size"]), (0, 0, 65536))
        self.assertEqual(ok["parity_block_free_min"], 837)
        self.assertIsNone(ok["oldest_scrubbed"])
        bad = sf.read_status(fixture("status-bad"))
        self.assertEqual(bad["has_bad"], {"count": 1, "first": 13, "last": 13})
        self.assertIsNotNone(bad["oldest_scrubbed"])
        interrupted = sf.read_status(fixture("status-int"))
        self.assertGreater(interrupted["has_unsynced"], 0)
        empty = sf.read_status(fixture("status-empty"))
        self.assertTrue(empty["array_empty"])
        self.assertFalse(ok["array_empty"])

    def test_changes_from_diff_and_sync_scans(self):
        partial = sf.read_changes(fixture("diff-partial"))
        self.assertEqual([(c["disk"], c["kind"]) for c in partial], [("d3", "removed")] * 4)
        mixed = sf.read_changes(fixture("diff-mixed"))
        kinds = {(c["disk"], c["kind"]) for c in mixed}
        self.assertEqual(kinds, {("d1", "updated"), ("d2", "moved"), ("d2", "added"), ("d1", "removed"),
                                 ("d3", "removed")})
        moved = next(c for c in mixed if c["kind"] == "moved")
        self.assertEqual((moved["from"], moved["path"]), ("docs/readme.txt", "docs/readme-moved.txt"))
        self.assertEqual(next(c for c in mixed if c["kind"] == "updated")["path"], "media/big.bin")
        live = sf.read_changes(fixture("sync-live"))   # a sync logs its own scan as diff does (the after-check, P2)
        self.assertEqual(len([c for c in live if c["kind"] == "added"]), 8)

    def test_smart_reads_every_status_bit_and_unknown_is_never_healthy(self):
        fake = sf.read_smart(fixture("smart-fake"))
        health = {name: entry["health"] for name, entry in fake["devices"].items()}
        self.assertEqual(health["d7"], "fail")      # flags 8: DISK FAILING
        self.assertEqual(health["d8"], "prefail")   # flags 16: prefail attribute at or below its threshold
        self.assertEqual(health["d4"], "unknown")   # flags 1: not recognised (needs -d)
        self.assertEqual(health["d6"], "unknown")   # flags 2: could not open
        self.assertEqual(health["d1"], "ok")
        self.assertAlmostEqual(fake["devices"]["d2"]["probability"], 0.0463418)
        self.assertEqual(fake["devices"]["d3"]["error"], 1)
        none = sf.read_smart(fixture("smart-unsupported"))
        self.assertTrue(none["unsupported"])
        self.assertEqual(none["devices"], {})

    def test_probe_and_fix_and_check(self):
        self.assertTrue(sf.read_probe(fixture("probe-unsupported"))["unsupported"])
        fixed = sf.read_fix(fixture("fix-e"))
        self.assertEqual((fixed["fixed"], fixed["unrecoverable"], fixed["exit"]), (1, 0, "recovered"))
        checked = sf.read_fix(fixture("check-a"))
        self.assertEqual(checked["files"].get("damaged"), 1)
        self.assertEqual(checked["exit"], "error")
        rebuilt = sf.read_fix(fixture("fix-d1"))
        self.assertGreater(rebuilt["files"].get("recovered", 0), 0)
        corrupt = fixture("scrub-corrupt")
        self.assertEqual(corrupt.summary_int("error_data"), 1)

    def test_gui_progress_lines(self):
        progress = sf.read_progress(fixture("sync-gui"))
        self.assertEqual(progress["percent"], 98)
        self.assertEqual(set(progress), {"block", "count", "bytes", "percent", "eta_seconds", "mb_per_second", "cpu",
                                         "elapsed"})
        broken = sf.parse_log("version:13.0\nrun:pos:1:2:x:4\n")
        self.assertEqual(sf.read_progress(broken), {"elapsed": 4})
        self.assertIsNone(sf.read_progress(fixture("status-ok")))

    def test_outcomes(self):
        """T2: 0 ok; 2 from diff changes; 130 interrupted even with "Everything OK"; 1 with the lock text busy;
        1 with a sigint: tag failed and interrupted; 137 killed; anything else unknown."""
        def outcome(name, **kw):
            return sf.outcome_of(RUNS[name]["exit"], fixture(name), out_text(name), **kw)
        self.assertEqual(outcome("status-ok")["outcome"], "ok")
        self.assertEqual(outcome("diff-partial", diff=True)["outcome"], "changes")
        term = outcome("sync-term")
        self.assertIn("Everything OK", fixture("sync-term").text())
        self.assertEqual((term["outcome"], term["interrupted"]), ("interrupted", True))
        self.assertIn("Everything OK", out_text("sync-int"))
        self.assertEqual(outcome("sync-int")["outcome"], "interrupted")
        self.assertEqual(outcome("diff-busy", diff=True)["outcome"], "busy")
        self.assertEqual(outcome("status-busy")["outcome"], "busy")
        self.assertEqual(outcome("sync-kill")["outcome"], "killed")
        self.assertEqual(sf.outcome_of(137, sf.Log())["outcome"], "killed")
        failed = outcome("sync-unmounted")
        self.assertEqual(failed["outcome"], "failed")
        self.assertRegex(failed["words"], r"^Failed: ")
        both = sf.outcome_of(1, sf.parse_log("version:13.0\nsigint:36: SIGINT received\nmsg:error: x\n"))
        self.assertEqual((both["outcome"], both["interrupted"]), ("failed", True))
        self.assertEqual(sf.outcome_of(3, sf.Log())["outcome"], "unknown")
        self.assertEqual(sf.outcome_of(None, sf.Log(), timed_out=True)["outcome"], "timeout")

    def test_a_sync_is_needed_after_an_interrupt_even_with_zero_counts(self):
        """T3: diff exits 2 with every count zero after an interrupted sync; status says has_unsynced > 0."""
        log = fixture("diff-after-int")
        self.assertEqual(RUNS["diff-after-int"]["exit"], 2)
        self.assertEqual([log.summary_int(k) for k in ("added", "removed", "updated", "moved")], [0, 0, 0, 0])
        self.assertEqual(sf.outcome_of(2, log, diff=True)["outcome"], "changes")
        self.assertGreater(sf.read_status(fixture("status-int"))["has_unsynced"], 0)
        rig = Rig({**HEALTHY, "status": {"fixture": "status-int", "exit": 0},
                   "diff": {"fixture": "diff-after-int", "exit": 2}})
        self.addCleanup(rig.close)
        code, answer = rig.ask({"v": 1, "verb": "guard"})
        self.assertEqual(code, 0)
        self.assertEqual(answer["verdict"], "would_run", "exit 2 means a run is needed, not nothing to do")


class RequestTests(unittest.TestCase):
    """T6: refused before anything starts; T7: the argv it builds."""

    def setUp(self):
        self.rig = Rig(dict(HEALTHY))
        self.addCleanup(self.rig.close)

    def refused(self, request, code="bad_field"):
        status, answer = self.rig.ask(request)
        self.assertEqual(status, 2, answer)
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["error"], code, answer)
        self.assertTrue(answer["words"].endswith("."))
        return answer

    def test_refused_requests_never_start_snapraid(self):
        self.refused({"v": 1, "verb": "format"}, "unknown_verb")
        self.refused({"v": 1, "verb": "status", "conf": "/tmp/x"}, "unknown_field")
        self.refused({"v": 1, "verb": "status", "argv": ["--test-run", "id"]}, "unknown_field")
        self.refused({"v": 2, "verb": "status"}, "bad_request")
        self.refused(b"not json\n", "bad_request")
        self.refused(b"[1]\n", "bad_request")
        self.refused(b'{"v": 1, "verb": "status", "pad": "' + b"x" * 17000 + b'"}\n', "bad_request")
        for verb in ("sync", "scrub", "release", "restore", "repair.bad", "restore.disk", "drill", "stop"):
            self.refused({"v": 1, "verb": verb, "plan": {"percent": 101}}, "not_in_this_version")
        self.refused({"v": 1, "verb": "files", "disk": "d9"})
        for bad in ("../etc", "a/../b", "a/\0b", "a\nb", "a/.../b", "a//b", "//x", "./x"):
            with self.subTest(dir=bad):
                self.refused({"v": 1, "verb": "files", "dir": bad})
        for bad in ("../../../log/auth", "J-1/2", "J-1\0", "G-", "J-", "J-1234567890", 7, None):
            with self.subTest(job=bad):
                self.refused({"v": 1, "verb": "job", "job": bad})
                self.refused({"v": 1, "verb": "changes", "guard": bad})
        self.refused({"v": 1, "verb": "job", "job": "J-99"}, "no_record")
        self.refused({"v": 1, "verb": "changes", "guard": "G-99"}, "no_record")
        self.refused({"v": 1, "verb": "log", "job": "J-99", "text": True}, "no_record")
        self.refused({"v": 1, "verb": "files", "offset": -1})
        self.refused({"v": 1, "verb": "files", "q": "x" * 201})
        self.refused({"v": 1, "verb": "changes", "guard": "G-1", "kind": "deleted"})
        self.assertEqual(self.rig.commands_run(), [], "SnapRAID was never started")

    def test_a_request_not_utf8_or_not_version_one_is_refused(self):
        """Not UTF-8 is a refusal (exit 2), never the reader's own failure; true and 1.0 are not version 1."""
        answer = self.refused(b'\xff\xfe{"v": 1, "verb": "status"}\n', "bad_request")
        self.assertIn("UTF-8", answer["words"])
        for version in (True, 1.0, "1", None):
            with self.subTest(v=version):
                self.refused({"v": version, "verb": "status"}, "bad_request")
        self.assertEqual(self.rig.commands_run(), [])

    def test_arguments_other_than_explain_are_refused(self):
        for argv in (["run", "J-1"], ["--help"], ["explain"], ["explain", "x"], ["status"], ["-c", "/etc/x"]):
            out = io.StringIO()
            code = sf.main(argv, stdin=io.BytesIO(b'{"v": 1, "verb": "status"}\n'), stdout=out,
                           paths=self.rig.paths, system=self.rig.system, lstat=self.rig.lstat)
            self.assertEqual(code, 2, argv)
            self.assertEqual(json.loads(out.getvalue())["error"], "bad_arguments")
        self.assertEqual(self.rig.commands_run(), [])

    def test_plans_and_patterns_for_the_later_verbs(self):
        """T6's plan and -f pattern cases, on the functions the P2 verbs use (a plan of 101 or "fulll" is refused;
        each pattern selects exactly one name under glibc's fnmatch with SnapRAID's flags)."""
        for bad in ({"percent": 101}, {"percent": 0}, "fulll", {"percent": 5, "older_than_days": 4000},
                    {"percent": True}, {"percent": 5, "x": 1}, None):
            with self.subTest(plan=bad):
                with self.assertRaises(sf.Refused):
                    sf.check_plan(bad)
        self.assertEqual(sf.check_plan({"percent": 10, "older_than_days": 10}), {"percent": 10, "older_than_days": 10})
        self.assertEqual(sf.check_plan("new"), {"word": "new"})
        self.assertEqual(sf.fix_pattern("Photos/2019"), "/Photos/2019")
        self.assertEqual(sf.fix_pattern("Photos", folder=True), "/Photos/")
        self.assertEqual(sf.fix_pattern("a\\b.txt"), "/a\\\\b.txt")
        self.assertEqual(sf.fix_pattern("x*y?.txt"), "/x\\*y\\?.txt")
        self.assertEqual(sf.fix_pattern("[a].txt"), "/\\[a].txt")
        for bad in ("../x", "a/./b", "", "/"):
            with self.subTest(path=bad):
                with self.assertRaises(sf.Refused):
                    sf.fix_pattern(bad)
        name = ctypes.util.find_library("c")
        if not name:
            self.skipTest("no C library to call fnmatch in")
        fnmatch = ctypes.CDLL(name).fnmatch
        pathname = 1   # FNM_PATHNAME on glibc; SnapRAID sets it and leaves FNM_NOESCAPE off (elem.c)

        def matches(pattern, path):
            return fnmatch(pattern.encode(), path.encode(), pathname) == 0
        names = ["notes.txt", "sub/notes.txt", "a\\b.txt", "ab.txt", "x*y.txt", "xay.txt", "[a].txt", "a.txt"]
        for wanted in ("notes.txt", "a\\b.txt", "x*y.txt", "[a].txt"):
            pattern = sf.fix_pattern(wanted)
            hits = [n for n in names if matches(pattern, "/" + n)]
            self.assertEqual(hits, [wanted], pattern)
        folder = sf.fix_pattern("sub", folder=True)
        self.assertTrue(matches(folder, "/sub/"))
        self.assertFalse(matches("notes.txt", "/sub/notes.txt"), "an unanchored name would match at any depth "
                                                                  "only with SnapRAID's own rule, not fnmatch's")

    def test_nofollow_stat_refuses_a_link_component(self):
        root = self.rig.root / "mnt/disk1"
        (root / "real").mkdir()
        (root / "real" / "f.bin").write_bytes(b"12345")
        os.symlink(root / "real", root / "link")
        self.assertEqual(sf.nofollow_stat(str(root), "real/f.bin").st_size, 5)
        self.assertIsNone(sf.nofollow_stat(str(root), "link/f.bin"))
        os.symlink(root / "real" / "f.bin", root / "real" / "f-link")
        self.assertIsNone(sf.nofollow_stat(str(root), "real/f-link"))
        self.assertIsNone(sf.nofollow_stat(str(root), "real/../real/f.bin"))

    def test_every_argv_is_the_verb_tables(self):
        """T7: the binary from the fixed list, -c CONF, -l inside the run's own folder, -q -q -q for diff and list,
        no test or expert option."""
        rig = self.rig
        for verb in ("status", "smart", "probe", "list", "guard"):
            self.assertEqual(rig.ask({"v": 1, "verb": verb})[0], 0, verb)
        argvs = [argv for argv in rig.calls_made() if argv != ["--version"]]
        self.assertTrue(argvs)
        commands = set()
        for argv in argvs:
            self.assertEqual(argv[0:2], ["-c", str(rig.conf)])
            self.assertEqual(argv[2], "-l")
            self.assertRegex(argv[3], re.escape(str(rig.paths.state)) + r"/runs/J-\d+/snapraid\.log$")
            command = argv[-1]
            commands.add(command)
            if command in ("diff", "list"):
                self.assertEqual(argv[4:-1], ["-q", "-q", "-q"])
            else:
                self.assertEqual(argv[4:-1], [])
            for word in argv:
                self.assertFalse(word.startswith("--test"))
                self.assertNotIn(word, ("-R", "-F", "-i", "--gen-conf", "pool", "touch", "rehash", "up", "down",
                                        "-h", "-S", "-U", "--force-empty", "--force-zero", "-e", "-m", "-f", "-d"))
        self.assertEqual(commands, {"status", "smart", "probe", "list", "diff"})
        for bad in (["/x", "-c", "c", "-l", "l", "sync"], ["/x", "-c", "c", "-l", "l", "--test-run", "status"],
                    ["/x", "-c", "c", "-l", "l", "-R", "status"], ["/x", "-l", "l", "-c", "c", "status"],
                    ["/x", "-c", "c", "-l", "l", "-q", "-q", "-q", "-q", "diff"]):
            with self.assertRaises(RuntimeError):
                sf.check_argv(bad)

    def test_the_binary_must_be_13_or_12(self):
        rig = self.rig
        rig.scenario.write_text(json.dumps({**json.loads(rig.scenario.read_text()), "version": "none"}))
        status, answer = rig.ask({"v": 1, "verb": "status"})
        self.assertEqual((status, answer["error"]), (2, "no_snapraid"))
        status, answer = rig.ask({"v": 1, "verb": "info"})
        self.assertEqual(answer["snapraid"]["version"], None)
        self.assertIn("No SnapRAID this reader reads", answer["snapraid"]["warning"])
        self.assertEqual(rig.commands_run(), [])


class InstallTests(unittest.TestCase):
    """T8: the installation and the config's contents."""

    def setUp(self):
        self.rig = Rig(dict(HEALTHY))
        self.addCleanup(self.rig.close)

    def test_unsafe_ownership_or_modes_refuse_every_verb(self):
        rig = self.rig
        targets = [rig.paths.helper, os.path.dirname(rig.paths.helper), str(rig.root / "usr/bin/snapraid"),
                   str(rig.conf), rig.paths.helper_config]
        for target in targets:
            for spoil in ((1000, 0), (0, 0o002), (0, 0o020)):
                with self.subTest(path=target, spoil=spoil):
                    rig.lstat.spoiled = {target: spoil}
                    code, answer = rig.ask({"v": 1, "verb": "status"})
                    self.assertEqual((code, answer["error"]), (2, "unsafe_install"), answer)
                    self.assertIn(target, answer["words"])
                    self.assertEqual(plain_terms(answer["words"]), [], "the card shows these words (U7)")
                    code, answer = rig.ask({"v": 1, "verb": "info"})
                    self.assertEqual(answer["error"], "unsafe_install")
        rig.lstat.spoiled = {}
        self.assertEqual(rig.commands_run(), [])
        self.assertEqual(rig.ask({"v": 1, "verb": "status"})[0], 0)

    def test_a_sticky_root_folder_may_be_writable(self):
        rig = self.rig
        parent = os.path.dirname(os.path.dirname(rig.paths.helper))
        rig.lstat.spoiled = {parent: (0, 0o1777)}
        self.assertEqual(rig.ask({"v": 1, "verb": "status"})[0], 0)
        rig.lstat.spoiled = {parent: (0, 0o0777)}
        self.assertEqual(rig.ask({"v": 1, "verb": "status"})[1]["error"], "unsafe_install")

    def test_the_helper_config_must_name_a_fixed_conf(self):
        rig = self.rig
        for change in ({"conf": "/tmp/evil.conf"}, {"allow": ["sync"]}, {"mounts": {"d1": "not a uuid!"}},
                       {"guard": {"disk_percent": 0}}, {"guard": {"nonsense": 1}}, {"extra": 1}, {"v": 2}):
            with self.subTest(change=change):
                rig.write_helper_cfg({**rig.helper_cfg, **change})
                code, answer = rig.ask({"v": 1, "verb": "status"})
                self.assertEqual((code, answer["error"]), (2, "unsafe_install"), answer)
                self.assertEqual(plain_terms(answer["words"]), [], "the card shows these words (U7)")
        rig.write_helper_cfg(rig.helper_cfg)

    def test_an_older_stop_wait_is_ignored(self):
        """The first revision's stop_wait_minutes: revision 2 has no stop verb, so an older file keeping it (any
        value, even past poweroff.target's forced 30 minutes) is accepted, never used, and not reported back."""
        rig = self.rig
        for wait in (60, 5000, 0):
            with self.subTest(wait=wait):
                rig.write_helper_cfg({**rig.helper_cfg, "stop_wait_minutes": wait})
                code, info = rig.ask({"v": 1, "verb": "info"})
                self.assertEqual(code, 0, info)
                self.assertNotIn("stop_wait_minutes", info["helper_config"])
        rig.write_helper_cfg(rig.helper_cfg)

    def conf_problem(self, extra: str = "", text: str | None = None) -> dict:
        rig = self.rig
        rig.write_conf(extra, text)
        code, answer = rig.ask({"v": 1, "verb": "status"})
        self.assertEqual((code, answer["error"]), (2, "unsafe_conf"), answer)
        code, info = rig.ask({"v": 1, "verb": "info"})
        self.assertEqual(code, 0)
        self.assertFalse(info["conf_ok"])
        self.assertTrue(info["conf_problems"])
        return info["conf_problems"][0]

    def test_smartctl_lines_off_the_allow_list_are_refused(self):
        for option in ("-d sat %s; rm -rf /", "-d sat %s $(id)", "-d sat `id` %s", "-d sat %s | sh",
                       "-d sat %s && id", "-d megaraid,0 %s", "-a %s", "%s", "-d sat %s -T permissive"):
            with self.subTest(option=option):
                problem = self.conf_problem(f"smartctl d1 {option}\n")
                self.assertIn("smartctl", problem["words"])
                self.assertEqual(problem["line"], 13)
        for option in ("-d sat %s", "-d sat,12 -T permissive %s", "-d nvme %s", "-d usbjmicron %s"):
            with self.subTest(ok=option):
                self.rig.write_conf(f"smartctl d1 {option}\n")
                self.assertEqual(self.rig.ask({"v": 1, "verb": "status"})[0], 0)
        self.assertNotIn("smart", self.rig.commands_run()[:0])

    def test_paths_and_keywords(self):
        r = self.rig.root
        conf = self.rig.conf.read_text()
        cases = {
            "unknown keyword": (conf + "frobnicate yes\n", "not a setting"),
            "parity in /etc": (conf.replace(f"parity {r}/mnt/parity1/snapraid.parity",
                                            f"parity {r}/etc/snapraid.parity"), "system folder"),
            "content outside .snapraid": (conf.replace(f"{r}/mnt/disk1/.snapraid/snapraid.content",
                                                       f"{r}/mnt/disk1/sub/snapraid.content"), ".snapraid"),
            "content in a .snapraid of no disk": (conf.replace(f"{r}/mnt/disk1/.snapraid/snapraid.content",
                                                               f"{r}/srv/.snapraid/snapraid.content"), "protected"),
            "content under /home": (conf.replace(f"{r}/var/snapraid/snapraid.content",
                                                 f"{r}/home/u/.snapraid/snapraid.content"), "snapraid"),
            "data elsewhere than its filesystem": (conf.replace(f"data d3 {r}/mnt/disk3/",
                                                                f"data d3 {r}/mnt/disk2/"), "recorded filesystem"),
            "data not recorded": (conf + f"data d4 {r}/mnt/disk4/\n", "no filesystem recorded"),
            "relative path": (conf.replace(f"data d3 {r}/mnt/disk3/", "data d3 mnt/disk3/"), "plain absolute"),
            "dot dot": (conf.replace(f"data d3 {r}/mnt/disk3/", f"data d3 {r}/mnt/../etc/"), "plain absolute"),
            "parity on the system disk": (conf.replace(f"parity {r}/mnt/parity1/snapraid.parity",
                                                       f"parity {r}/srv/snapraid.parity"), "recorded filesystem"),
        }
        for label, (text, words) in cases.items():
            with self.subTest(label):
                problem = self.conf_problem(text=text)
                self.assertIn(words, problem["words"], problem)

    def test_an_unmounted_disk_is_the_guards_not_a_config_problem(self):
        rig = self.rig
        rig.system.unmounted = {"d3"}
        _code, info = rig.ask({"v": 1, "verb": "info"})
        self.assertTrue(info["conf_ok"], info["conf_problems"])
        self.assertFalse(info["mounts"]["d3"]["mounted"])

    def test_explain_prints_every_change_in_plain_words(self):
        rig = self.rig
        new = {**rig.helper_cfg, "allow": ["read", "sync", "scrub"], "guard": {"disk_percent": 2},
               "mounts": {**UUIDS, "d3": "3333dddd-0000-4000-8000-000000000003"}, "stop_wait_minutes": 60}
        Path(rig.paths.helper_config + ".new").write_text(json.dumps(new))
        out = io.StringIO()
        code = sf.main(["explain", "helper-config"], stdout=out, paths=rig.paths, system=rig.system, lstat=rig.lstat)
        text = out.getvalue()
        self.assertEqual(code, 0, text)
        self.assertIn("Allowed: reading → reading, protecting, checking for damage", text)
        self.assertIn("... that are at least this share of the disk's files (%): 5 → 2", text)
        self.assertIn("d3: the filesystem 3333dddd-0000-4000-8000-000000000003 not mounted now (was "
                      "1111aaaa-0000-4000-8000-000000000003)", text)
        self.assertIn(f"d1: the filesystem {UUIDS['d1']} mounted at {rig.root}/mnt/disk1", text)
        self.assertIn("this version of the reader only reads", text)
        self.assertIn("stop_wait_minutes: ignored (this reader stops nothing; you can leave it out)", text)
        self.assertNotIn("Wait for a safe stop", text)
        self.assertEqual(plain_terms(text.replace("helper-config", "")), [], "explain speaks of the reader (U7)")
        Path(rig.paths.helper_config + ".new").write_text(json.dumps({**new, "conf": "/tmp/x.conf"}))
        out = io.StringIO()
        self.assertEqual(sf.main(["explain", "helper-config"], stdout=out, paths=rig.paths, system=rig.system,
                                 lstat=rig.lstat), 2)
        self.assertIn("conf must be one of", out.getvalue())
        rig.lstat.spoiled = {rig.paths.helper_config + ".new": (1000, 0)}
        out = io.StringIO()
        self.assertEqual(sf.main(["explain", "helper-config"], stdout=out, paths=rig.paths, system=rig.system,
                                 lstat=rig.lstat), 2)
        self.assertIn("not owned by root", out.getvalue())

    def test_explain_snapraid_config(self):
        rig = self.rig
        r = rig.root
        Path(str(rig.conf) + ".new").write_text(rig.conf.read_text() + "smartctl d1 -d sat %s\nexclude /tmp/\n")
        out = io.StringIO()
        code = sf.main(["explain", "snapraid-config"], stdout=out, paths=rig.paths, system=rig.system,
                       lstat=rig.lstat)
        text = out.getvalue()
        self.assertEqual(code, 0, text)
        self.assertIn(f"Disk 1 (d1): protects {r}/mnt/disk1/", text)
        self.assertIn("Added: smartctl d1 -d sat %s", text)
        self.assertIn("Added: exclude /tmp/", text)
        Path(str(rig.conf) + ".new").write_text(rig.conf.read_text() + "smartctl d1 -d sat %s; id\n")
        out = io.StringIO()
        self.assertEqual(sf.main(["explain", "snapraid-config"], stdout=out, paths=rig.paths, system=rig.system,
                                 lstat=rig.lstat), 2)
        self.assertIn("unsafe_conf", out.getvalue())
        self.assertEqual(rig.commands_run(), [], "explain runs nothing")


class JobLockTests(unittest.TestCase):
    """T9: one job at a time."""

    def test_a_second_read_is_busy_status_answers_from_the_last_result_and_smart_runs(self):
        rig = Rig(dict(HEALTHY))
        self.addCleanup(rig.close)
        self.assertEqual(rig.ask({"v": 1, "verb": "status"})[0], 0)
        os.makedirs(rig.paths.run_dir, exist_ok=True)
        fd = os.open(os.path.join(rig.paths.run_dir, "job.lock"), os.O_RDWR | os.O_CREAT, 0o644)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.ftruncate(fd, 0)
        os.pwrite(fd, json.dumps({"verb": "guard", "since": "2026-10-25T08:00:00Z"}).encode(), 0)
        before = rig.commands_run()
        code, answer = rig.ask({"v": 1, "verb": "guard"})
        self.assertEqual((code, answer["error"], answer["running"]), (2, "busy", "guard"))
        self.assertEqual(rig.ask({"v": 1, "verb": "list"})[1]["error"], "busy")
        code, answer = rig.ask({"v": 1, "verb": "status"})
        self.assertEqual(code, 0)
        self.assertTrue(answer["from_last"])
        self.assertEqual(answer["busy_with"], "guard")
        self.assertEqual(rig.commands_run(), before, "nothing started while the job runs")
        self.assertEqual(rig.ask({"v": 1, "verb": "smart"})[0], 0)
        self.assertEqual(rig.ask({"v": 1, "verb": "probe"})[0], 0)
        self.assertEqual(rig.commands_run(), before + ["smart", "probe"])
        self.assertEqual(rig.ask({"v": 1, "verb": "info"})[1]["running"]["verb"], "guard")

    def test_a_second_request_is_told_the_running_ids(self):
        """T9: busy names what runs: the guard's G-n and its SnapRAID run's J-n, written into the job lock."""
        rig = Rig({**HEALTHY, "diff": {"fixture": "diff0", "exit": 0, "sleep": 3}})
        self.addCleanup(rig.close)
        first: dict = {}
        thread = threading.Thread(target=lambda: first.update(rig.ask({"v": 1, "verb": "guard"})[1]))
        thread.start()
        self.addCleanup(thread.join)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and rig.commands_run()[-1:] != ["diff"]:
            time.sleep(0.05)
        time.sleep(0.2)
        code, answer = rig.ask({"v": 1, "verb": "guard"})
        thread.join()
        self.assertEqual((code, answer["error"], answer["running"]), (2, "busy", "guard"))
        self.assertEqual(answer["guard"], first["guard"])
        self.assertEqual(answer["job"], first["runs"][-1])
        self.assertIn(f"({first['guard']}, {first['runs'][-1]})", answer["words"])
        with contextlib.suppress(OSError):
            self.assertEqual(Path(rig.paths.run_dir, "job.lock").read_text(), "", "emptied when it ends")

    def test_snapraid_run_by_hand_is_busy(self):
        rig = Rig({**HEALTHY, "status": {"fixture": "status-busy", "exit": 1}})
        self.addCleanup(rig.close)
        code, answer = rig.ask({"v": 1, "verb": "status"})
        self.assertEqual((code, answer["outcome"]), (0, "busy"))
        self.assertEqual(answer["words"], "SnapRAID is busy")
        code, answer = rig.ask({"v": 1, "verb": "guard"})
        self.assertEqual((answer["verdict"], answer["reasons"]), ("would_wait", ["snapraid_busy"]))


class GuardReportTests(unittest.TestCase):
    """The guard as a report (section 4.2, P1): which checks hold, wait or pass, with counts and codes only."""

    def guard(self, commands: dict, prepare=None) -> tuple[Rig, dict]:
        rig = Rig({**HEALTHY, **commands})
        self.addCleanup(rig.close)
        if prepare:
            prepare(rig)
        code, answer = rig.ask({"v": 1, "verb": "guard"})
        self.assertEqual(code, 0, answer)
        return rig, answer

    def check(self, answer: dict, check: str) -> list[dict]:
        return [c for c in answer["checks"] if c["id"] == check]

    def test_nothing_changed_is_nothing_to_do(self):
        _rig, answer = self.guard({})
        self.assertEqual(answer["verdict"], "nothing_to_do")
        self.assertTrue(answer["report_only"])
        self.assertEqual([c["id"] for c in answer["checks"]], [f"G{i}" for i in range(1, 12)])
        self.assertNotIn("changes", answer, "the change list stays with the helper")
        self.assertEqual(len(answer["list_sha256"]), 64)

    def test_six_of_nine_removed_would_be_held_and_four_would_not(self):
        """L1 in its report form: deleting most of one disk's files would be held (6 of 9 here: 6 removed and 1
        rewritten, over half the disk), with counts and codes only; 4 of 9 passes (under the floor of 10 and
        under half)."""
        _rig, answer = self.guard({"diff": {"fixture": "diff-mixed", "exit": 2}})
        self.assertEqual(answer["verdict"], "would_hold")
        self.assertEqual(answer["reasons"], ["lost_over"])
        held = self.check(answer, "G10")[0]
        self.assertEqual(held["disks"], ["d1"])
        self.assertEqual(held["held"][0]["clause"], "disk_half")
        self.assertEqual(answer["counts"]["disks"]["d1"]["lost"], 7)
        self.assertEqual(answer["counts"]["disks"]["d2"]["moved"], 1)
        self.assertNotRegex(json.dumps(answer), r"img\d\.jpg|readme|big\.bin", "no file name in the answer")
        _, partial = self.guard({"diff": {"fixture": "diff-partial", "exit": 2}})
        self.assertEqual(partial["verdict"], "would_run")
        self.assertEqual(partial["counts"]["disks"]["d3"]["lost"], 4)

    def test_the_change_list_pages_names_as_text(self):
        rig, answer = self.guard({"diff": {"fixture": "diff-mixed", "exit": 2}})
        code, page = rig.ask({"v": 1, "verb": "changes", "guard": answer["guard"], "disk": "d1", "kind": "removed"})
        self.assertEqual(code, 0)
        self.assertEqual(page["total"], 6)
        self.assertEqual(page["list_sha256"], answer["list_sha256"])
        self.assertEqual(page["report"]["runs"], answer["runs"], "the cockpit links the report's runs")
        code, found = rig.ask({"v": 1, "verb": "changes", "guard": answer["guard"], "q": "README"})
        self.assertEqual({e["path"] for e in found["entries"]}, {"docs/readme.txt", "docs/readme-moved.txt"})

    def test_an_unmounted_disk_is_held_by_g3_before_the_diff(self):
        """L3 in its unit form: G3 holds disk_missing, and SnapRAID is not run at all: not the diff (where its
        own empty-disk guard sits), and not the status, which fails on a missing disk's file list (found in the
        lab: the verdict became an error instead of a hold)."""
        rig, answer = self.guard({"diff": {"fixture": "diff-unmounted", "exit": 2}},
                                 prepare=lambda rig: rig.system.unmounted.add("d3"))
        self.assertEqual(answer["verdict"], "would_hold")
        self.assertIn("disk_missing", answer["reasons"])
        self.assertEqual(self.check(answer, "G3")[0]["disks"], ["d3"])
        self.assertIn("disk 3", self.check(answer, "G3")[0]["words"])
        self.assertEqual(rig.commands_run(), [], "no SnapRAID command on a missing disk")
        self.assertEqual([self.check(answer, g)[0]["result"] for g in ("G5", "G6", "G8", "G9", "G10", "G11")],
                         ["skipped"] * 6)

    def test_snapraids_empty_disk_warning_holds_as_disk_looks_empty(self):
        _, answer = self.guard({"diff": {"fixture": "diff-unmounted", "exit": 2}})
        self.assertIn("disk_looks_empty", answer["reasons"])
        self.assertEqual(self.check(answer, "G9")[0]["disks"], ["d3"])

    def test_other_holds(self):
        cases = {
            "zero_size": ({"diff": {"fixture": "diff-zero", "exit": 2}}, None),
            "bad_blocks": ({"status": {"fixture": "status-bad", "exit": 0}}, None),
            "diff_error": ({"diff": {"fixture": "diff-nomountpoint", "exit": 1}}, None),
            "disk_uuid_changed": ({}, lambda rig: rig.system.uuids.update(d2="5555eeee-0000-4000-8000-000000000002")),
            "content_problem": ({}, lambda rig: (rig.root / "mnt/disk2/.snapraid/snapraid.content").unlink()),
            "content_space_low": ({}, lambda rig: rig.system.space.update({"mnt/disk1": __import__(
                "tests.snapraid_rig", fromlist=["Space"]).Space(free=100 << 20)})),
            "backup_running": ({}, lambda rig: rig.system.running.append("restic")),
        }
        for reason, (commands, prepare) in cases.items():
            with self.subTest(reason=reason):
                _, answer = self.guard(commands, prepare)
                self.assertIn(reason, answer["reasons"], answer["checks"])
                self.assertIn(answer["verdict"], ("would_hold", "would_wait"))

    def test_the_room_for_the_file_list_is_the_helper_configs_floor(self):
        """G4's floor is guard.content_free_min_mib from the helper's config (found on the lab's small disks)."""
        space = __import__("tests.snapraid_rig", fromlist=["Space"]).Space
        for floor, verdict in ((1024, "content_space_low"), (50, None)):
            with self.subTest(floor=floor):
                def prepare(rig, floor=floor):
                    rig.write_helper_cfg({**rig.helper_cfg, "guard": {"content_free_min_mib": floor}})
                    rig.system.space.update({"mnt/disk1": space(free=100 << 20)})
                _, answer = self.guard({}, prepare)
                if verdict:
                    self.assertIn(verdict, answer["reasons"], answer["checks"])
                else:
                    self.assertEqual(self.check(answer, "G4")[0]["result"], "pass", answer["checks"])

    def test_different_sizes_of_the_file_list_hold(self):
        def prepare(rig):
            (rig.root / "mnt/disk1/.snapraid/snapraid.content").write_bytes(b"older")
        _, answer = self.guard({}, prepare)
        self.assertIn("content_problem", answer["reasons"])
        self.assertIn("different sizes", self.check(answer, "G4")[0]["words"])

    def test_a_damaged_file_list_holds_as_a_content_problem(self):
        """A real 13.0 status on a first copy of the file list whose bytes were changed, its size kept (exit 1,
        "Error decoding", "CRC mismatch"): the reader says the file list cannot be read, and the guard holds at G4
        (content_problem) instead of failing at G5 (DESIGN.md section 41, G4: a corrupt first copy aborts with no
        fallback). Any other failed status stays an error, never a content problem."""
        rig = Rig({**HEALTHY, "status": {"fixture": "status-damaged", "exit": 1}})
        self.addCleanup(rig.close)
        code, status = rig.ask({"v": 1, "verb": "status"})
        self.assertEqual((code, status["outcome"], status["facts"]), (0, "failed", None))
        self.assertIn("Error decoding", status["words"])
        content = status["content"]
        self.assertTrue(content["read_failed"] and content["other"], content)
        self.assertIn("damaged", content["problem"])
        self.assertIn("Error decoding", content["error"])
        self.assertEqual(plain_terms(content["problem"]), [], "the guard page shows these words (U7)")
        _code, guard = rig.ask({"v": 1, "verb": "guard"})
        self.assertEqual((guard["verdict"], guard["reasons"]), ("would_hold", ["content_problem"]))
        self.assertEqual(self.check(guard, "G4")[0]["result"], "hold")
        self.assertEqual(self.check(guard, "G5")[0]["result"], "skipped")
        rig.set_commands({**HEALTHY, "status": {"text": "version:13.0\nmsg:fatal: Something else broke\n", "exit": 1}})
        _code, status = rig.ask({"v": 1, "verb": "status"})
        self.assertEqual((status["outcome"], status["content"]["read_failed"], status["content"]["problem"]),
                         ("failed", None, None))
        _code, guard = rig.ask({"v": 1, "verb": "guard"})
        self.assertEqual((guard["verdict"], guard["reasons"]), ("error", ["status_error"]))
        warned = "version:13.0\nmsg:fatal: WARNING! Content file '/x' not found, attempting with another copy...\n" \
                 "msg:fatal: Something else broke\n"
        self.assertIsNone(sf.content_failure(sf.parse_log(warned)), "SnapRAID's own warnings are not why it stopped")

    def test_smart_fail_holds_and_unreadable_is_a_note(self):
        failing = "version:13.0\nsmart:/dev/sda:d2\nattr:/dev/sda:d2:serial:X\nattr:/dev/sda:d2:flags:8:8\n"
        _, answer = self.guard({"smart": {"text": failing, "exit": 1}})
        self.assertIn("smart_fail", answer["reasons"])
        _, answer = self.guard({})
        self.assertEqual(self.check(answer, "G6")[0]["result"], "note")
        self.assertIn("not a pass", self.check(answer, "G6")[0]["words"])

    def test_an_unfinished_run_started_outside_is_counted_against_the_last_good_list(self):
        """G8: status reports an unfinished sync the helper did not run; a fresh list against the index of the
        last good sync counts what it recorded as removed or changed (the diff shows zero)."""
        def prepare(rig):
            self.assertTrue(rig.ask({"v": 1, "verb": "list"})[1]["good"])
            listed = (FIXTURES / "list.log").read_text(encoding="utf-8")
            kept = "\n".join(line for line in listed.splitlines() if not line.startswith("file:d1:media/photos")
                             and not line.startswith("file:d1:docs"))
            rig.set_commands({**HEALTHY, "status": {"fixture": "status-int", "exit": 0},
                              "list": {"text": kept, "exit": 0}, "diff": {"fixture": "diff-after-int", "exit": 2}})
        _, answer = self.guard({}, prepare)
        g8 = self.check(answer, "G8")[0]
        self.assertEqual(g8["result"], "hold", g8)
        self.assertEqual(g8["lost"], 6)
        self.assertIn("unguarded_partial_sync", answer["reasons"])
        _, answer = self.guard({"status": {"fixture": "status-int", "exit": 0}})
        self.assertEqual(self.check(answer, "G8")[0]["result"], "note")

    def test_parity_room(self):
        def prepare(rig):
            from tests.snapraid_rig import Space
            rig.ask({"v": 1, "verb": "guard"})    # nothing changed: records each disk's used space
            rig.set_commands({**HEALTHY, "diff": {"fixture": "diff-partial", "exit": 2}})
            rig.system.space["mnt/disk2"] = Space(used=(50 << 30) + (2 << 30))   # 2 GiB more on disk 2
        _, answer = self.guard({}, prepare)
        self.assertIn("parity_full", answer["reasons"])
        self.assertEqual(self.check(answer, "G11")[0]["disks"], ["d2"])

    def test_first_run_skips_the_thresholds(self):
        def prepare(rig):
            for rel in ("var/snapraid/snapraid.content", "mnt/disk1/.snapraid/snapraid.content",
                        "mnt/disk2/.snapraid/snapraid.content"):
                (rig.root / rel).unlink()
        _, answer = self.guard({"status": {"fixture": "status-empty", "exit": 0},
                                "diff": {"fixture": "diff-first", "exit": 2}}, prepare)
        self.assertEqual(answer["verdict"], "would_run")
        self.assertTrue(answer["first_run"])
        self.assertEqual(self.check(answer, "G10")[0]["result"], "note")
        self.assertEqual(self.check(answer, "G4")[0]["result"], "note")

    def test_replaced_by_a_similar_file_is_shown_and_counted(self):
        def prepare(rig):
            self.assertTrue(rig.ask({"v": 1, "verb": "list"})[1]["good"])
            (rig.root / "mnt/disk3/media/photos").mkdir(parents=True)
            (rig.root / "mnt/disk3/media/photos/img1-better.jpg").write_bytes(b"x" * 250_000)
            text = (FIXTURES / "diff-partial.log").read_text(encoding="utf-8") + \
                "scan:add:d3:media/photos/img1-better.jpg\n"
            rig.set_commands({**HEALTHY, "diff": {"text": text, "exit": 2}})
        rig, answer = self.guard({}, prepare)
        self.assertEqual(answer["counts"]["disks"]["d3"]["replaced"], 1)
        self.assertEqual(answer["counts"]["disks"]["d3"]["lost"], 4)
        _code, page = rig.ask({"v": 1, "verb": "changes", "guard": answer["guard"], "kind": "replaced"})
        self.assertEqual(page["entries"][0]["replaced_by"], "media/photos/img1-better.jpg")

    def test_thresholds(self):
        """The guard report's limits (G10, DESIGN.md section 41; the first revision's full table, T11, left with its
        enforced guard: design section 13.2)."""
        limits = sf.GUARD_DEFAULTS
        self.assertTrue(sf.over_limits({"d": 8}, {"d": 9}, limits))      # half the disk
        self.assertTrue(sf.over_limits({"d": 6}, {"d": 9}, limits))
        self.assertFalse(sf.over_limits({"d": 4}, {"d": 9}, limits))
        self.assertTrue(sf.over_limits({"d": 12}, {"d": 200}, limits))   # 6%, at least 10
        self.assertFalse(sf.over_limits({"d": 9}, {"d": 100}, limits))
        self.assertFalse(sf.over_limits({"d": 99}, {"d": 1000000}, limits))
        self.assertTrue(sf.over_limits({"d": 100}, {"d": 1000000}, limits))
        self.assertTrue(sf.over_limits({"a": 90, "b": 90, "c": 90}, {"a": 2000, "b": 2000, "c": 2000}, limits))
        self.assertFalse(sf.over_limits({"a": 90}, {"a": 2000, "b": 2000, "c": 2000}, limits))


class FilesAndRecordsTests(unittest.TestCase):
    def test_files_pages_the_last_good_index(self):
        rig = Rig(dict(HEALTHY))
        self.addCleanup(rig.close)
        _, answer = rig.ask({"v": 1, "verb": "files"})
        self.assertEqual(answer["index"], None)
        rig.ask({"v": 1, "verb": "list"})
        _, answer = rig.ask({"v": 1, "verb": "files", "disk": "d2", "dir": "media/notes"})
        names = {entry["path"] for entry in answer["entries"]}
        self.assertIn("media/notes/<img src=x onerror=alert(1)>.jpg", names)
        self.assertIn("media/notes/line\nbreak.txt", names)
        _, root = rig.ask({"v": 1, "verb": "files", "disk": "d1", "dir": ""})
        self.assertEqual({f["name"] for f in root["folders"]}, {"docs", "media"})
        _, found = rig.ask({"v": 1, "verb": "files", "q": "a:b"})
        self.assertEqual(found["total"], 3)
        _, found = rig.ask({"v": 1, "verb": "files", "q": "%"})
        self.assertEqual(found["total"], 0, "LIKE wildcards in the search are literal")
        state = Path(rig.paths.state)
        self.assertEqual(oct(state.stat().st_mode & 0o777), "0o700")
        for path in (state / "index").iterdir():
            self.assertEqual(path.stat().st_mode & 0o077, 0, path)

    def test_job_and_log(self):
        rig = Rig(dict(HEALTHY))
        self.addCleanup(rig.close)
        job = rig.ask({"v": 1, "verb": "status"})[1]["job"]
        _, answer = rig.ask({"v": 1, "verb": "job", "job": job})
        self.assertEqual((answer["job"]["verb"], answer["job"]["outcome"], answer["job"]["exit"]), ("status", "ok", 0))
        _, answer = rig.ask({"v": 1, "verb": "job"})
        self.assertEqual(answer["jobs"][0]["job"], job)
        _, answer = rig.ask({"v": 1, "verb": "log", "job": job, "text": True})
        self.assertIn("SnapRAID status report", answer["text"])
        _code, answer = rig.ask({"v": 1, "verb": "log", "job": job, "text": True, "offset": 10})
        self.assertEqual(answer["offset"], 10)
        out = Path(rig.paths.state, "runs", job, "snapraid.out")
        self.assertEqual(out.stat().st_mode & 0o077, 0)

    def test_log_pages_by_the_bytes_read(self):
        """next_offset is the offset plus the bytes read: a name that is not UTF-8, or a character cut at a page's
        end, decodes to a different length, and a page counting decoded text would skip or repeat output."""
        rig = Rig(dict(HEALTHY))
        self.addCleanup(rig.close)
        job = rig.ask({"v": 1, "verb": "status"})[1]["job"]
        raw = b"name\xff\xfe.txt caf\xc3\xa9 " * 3
        Path(rig.paths.state, "runs", job, "snapraid.out").write_bytes(raw)
        self.addCleanup(setattr, sf, "MAX_LOG_TEXT", sf.MAX_LOG_TEXT)
        sf.MAX_LOG_TEXT = 7
        offset, differs = 0, False
        while True:
            _code, page = rig.ask({"v": 1, "verb": "log", "job": job, "text": True, "offset": offset})
            self.assertEqual(page["offset"], offset)
            self.assertEqual(page["next_offset"] - offset, min(7, len(raw) - offset))
            differs |= len(page["text"].encode("utf-8")) != page["next_offset"] - offset
            offset = page["next_offset"]
            if not page["more"]:
                break
        self.assertEqual(offset, len(raw))
        self.assertTrue(differs, "the decoded text's length would have drifted")

    def test_old_runs_are_pruned(self):
        rig = Rig(dict(HEALTHY))
        self.addCleanup(rig.close)
        helper = rig.helper()
        for _ in range(sf.KEEP_RUNS + 3):
            helper.snapraid("probe", "probe", str(rig.conf))
        runs = sorted(Path(rig.paths.state, "runs").iterdir())
        self.assertEqual(len(runs), sf.KEEP_RUNS)
        self.assertFalse(Path(rig.paths.state, "jobs", "J-1.json").exists())


class InventoryTests(unittest.TestCase):
    """T25: other schedulers of SnapRAID, found by contents; the content fingerprint."""

    def test_the_inventory_finds_snapraid_wherever_it_is_scheduled_and_reports_nothing_else(self):
        rig = Rig(dict(HEALTHY))
        self.addCleanup(rig.close)
        r = rig.root

        def write(rel, text, mode=0o644):
            path = r / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            path.chmod(mode)
        write("etc/crontab", "SHELL=/bin/sh\n# m h dom mon dow user command\n17 * * * * root cd / && run-parts "
                             "--report /etc/cron.hourly\n30 3 * * * root /usr/bin/snapraid sync\n")
        write("etc/cron.daily/disk-care", "#!/bin/sh\nsnapraid -c /etc/snapraid.conf scrub -p 5\n", 0o755)
        write("etc/cron.daily/logrotate", "#!/bin/sh\nlogrotate /etc/logrotate.conf\n", 0o755)
        write("var/spool/cron/crontabs/owner", "0 4 * * 0 /usr/local/bin/snapraid-runner -c /etc/runner.conf\n"
                                               "5 4 * * * /home/owner/bin/backup-photos\n")
        write("etc/passwd", "root:x:0:0:root:/root:/bin/bash\nowner:x:1000:1000::/home/owner:/bin/bash\n")
        write("home/owner/.config/systemd/user/parity.timer", "[Timer]\nOnCalendar=*-*-* 05:00\n")
        write("home/owner/.config/systemd/user/parity.service", "[Service]\nExecStart=/usr/local/bin/nightly\n")
        write("usr/local/bin/nightly", "#!/bin/sh\nexec /usr/bin/snapraid touch\n", 0o755)
        write("etc/systemd/system/web.service", "[Service]\nExecStart=/usr/bin/python3 -m http.server\n")
        write("etc/systemd/system/disks.service", "[Service]\nExecStart=/opt/tools/wrap.sh --quiet\n")
        write("opt/tools/wrap.sh", "#!/bin/sh\n/usr/local/bin/snapraid-aio-script.sh\n", 0o755)
        write("opt/stacks/parity/compose.yaml", "services:\n  sr:\n    image: example/snapraid:13\n")
        write("opt/stacks/media/compose.yaml", f"services:\n  app:\n    volumes:\n      - {r}/mnt/parity1:/data\n")
        write("opt/stacks/blog/compose.yaml", "services:\n  web:\n    image: nginx\n")
        _code, info = rig.ask({"v": 1, "verb": "info"})
        found = {(entry["place"], entry["kind"], entry["via"]): entry for entry in info["schedulers"]["found"]}
        places = {key[0] for key in found}
        self.assertEqual(places, {"/etc/crontab", "/etc/cron.daily/disk-care", "/var/spool/cron/crontabs/owner",
                                  "/home/owner/.config/systemd/user/parity.timer",
                                  "/home/owner/.config/systemd/user/parity.service",
                                  "/etc/systemd/system/disks.service", "/opt/stacks/parity/compose.yaml",
                                  "/opt/stacks/media/compose.yaml"})
        self.assertEqual(found[("/etc/crontab", "cron", "direct")]["schedule"], ["30", "3", "*", "*", "*"])
        self.assertEqual(found[("/var/spool/cron/crontabs/owner", "user-crontab", "direct")]["schedule"],
                         ["0", "4", "*", "*", "0"])
        self.assertEqual(found[("/home/owner/.config/systemd/user/parity.timer", "user-timer", "script")]["schedule"],
                         ["*-*-* 05:00"])
        self.assertEqual(found[("/etc/systemd/system/disks.service", "unit", "script")]["schedule"], [])
        self.assertIn("mounts the protection disk", found[("/opt/stacks/media/compose.yaml", "compose",
                                                           "mounts the protection disk")]["via"])
        text = json.dumps(info["schedulers"])
        for secret in ("logrotate", "backup-photos", "http.server", "nginx", "runner.conf", "scrub -p 5", "touch"):
            self.assertNotIn(secret, text, "only places, how they matched and schedule fields")
        # crontab, 2 scripts, 1 user crontab, 4 units, 3 stacks
        self.assertEqual(info["schedulers"]["places_checked"], 11)

    def test_only_what_a_command_starts_counts_never_the_files_it_reads(self):
        """An argument is data, not a script it starts: fstrim's --listed-in /etc/fstab:/proc/self/mountinfo names
        two files that mention snapraid on a SnapRAID server, and fstrim is no scheduler of SnapRAID (fstrim.timer
        was listed on a real machine). A script handed to an interpreter, a program behind wrappers (flock, nice)
        found on the PATH, and the commands of sh -c still count; a compiled program is never read as a script."""
        rig = Rig(dict(HEALTHY))
        self.addCleanup(rig.close)

        def write(rel, text, mode=0o644):
            path = rig.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            path.chmod(mode)
        write("etc/fstab", "UUID=1 /mnt/disk1 ext4 defaults 0 2   # snapraid data disk\n")
        write("proc/self/mountinfo", "36 1 7:0 / /mnt/disk1 rw - ext4 /dev/loop0 rw snapraid\n")
        write("sbin/fstrim", "\x7fELF and the word snapraid among a binary's strings\n", 0o755)
        write("etc/systemd/system/fstrim.service", "[Service]\nExecStart=/sbin/fstrim --listed-in "
                                                   "/etc/fstab:/proc/self/mountinfo --verbose\n")
        write("etc/systemd/system/fstrim.timer", "[Timer]\nOnCalendar=weekly\n")
        write("etc/systemd/system/strings.service", "[Service]\nExecStart=/sbin/fstrim --all\n")
        write("bin/sh", "\x7fELF a shell\n", 0o755)
        write("usr/bin/flock", "\x7fELF\n", 0o755)
        write("opt/jobs/parity-job", "snapraid -c /etc/snapraid.conf sync\n")     # not executable: sh is handed it
        write("usr/local/bin/nightly", "#!/bin/sh\nexec /usr/bin/snapraid touch\n", 0o755)
        write("etc/cron.d/jobs", "0 1 * * * root /bin/sh /opt/jobs/parity-job\n"
                                 "0 2 * * * root /usr/bin/flock -n /run/lock/n.lock nice -n 10 nightly\n"
                                 "0 3 * * * root /bin/sh -c '/usr/local/bin/nightly --now' 2>/dev/null\n"
                                 "0 4 * * * root /usr/bin/flock /etc/fstab true\n"
                                 "0 5 * * * root /sbin/fstrim -v /proc/self/mountinfo\n")
        _code, info = rig.ask({"v": 1, "verb": "info"})
        found = info["schedulers"]["found"]
        self.assertEqual({entry["place"] for entry in found}, {"/etc/cron.d/jobs"}, found)
        self.assertEqual([entry["schedule"][1] for entry in found], ["1", "2", "3"])
        self.assertEqual({entry["via"] for entry in found}, {"script"})

    def test_a_changed_content_file_with_no_helper_job_is_a_run_outside(self):
        rig = Rig(dict(HEALTHY))
        self.addCleanup(rig.close)
        first = rig.ask({"v": 1, "verb": "status"})[1]["fingerprint"]
        self.assertEqual((first["known"], first["changed_now"], first["outside"]), (False, False, []))
        again = rig.ask({"v": 1, "verb": "status"})[1]["fingerprint"]
        self.assertEqual((again["known"], again["changed_now"]), (True, False))
        content = rig.root / "var/snapraid/snapraid.content"
        stat_before = content.stat()
        content.write_bytes(b"CONTENT " * 100)      # same size; block times (status) unchanged
        os.utime(content, ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns + 10 ** 9))
        seen = rig.ask({"v": 1, "verb": "status"})[1]
        self.assertTrue(seen["fingerprint"]["changed_now"])
        self.assertEqual(len(seen["fingerprint"]["outside"]), 1)
        self.assertEqual(seen["facts"]["newest_block"], first and rig.ask({"v": 1, "verb": "status"})[1]["facts"]
                         ["newest_block"])
        later = rig.ask({"v": 1, "verb": "info"})[1]["fingerprint"]
        self.assertFalse(later["changed_now"])
        self.assertEqual(len(later["outside"]), 1, "kept, so a harness that missed an answer still sees it")
        os.utime(content, ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns + 2 * 10 ** 9))
        touched = rig.ask({"v": 1, "verb": "status"})[1]["fingerprint"]
        self.assertFalse(touched["changed_now"], "a new time with the same bytes is no run")


class ConfLineTests(unittest.TestCase):
    """The config is read where SnapRAID 13.0 breaks lines, at "\\n" only (stream.c). str.splitlines() also broke at
    \\v, \\f, a lone \\r, \\x1c-\\x1e, \\x85 and U+2028/9, so a smartctl option SnapRAID hands to popen() whole
    as root was checked as two harmless lines and passed (the reviewer's repro: "smartctl d1 -d sat %s<VT>exclude
    x;<command>")."""

    SEPARATORS = ("\x0b", "\x0c", "\r", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029")

    def setUp(self):
        self.rig = Rig(dict(HEALTHY))
        self.addCleanup(self.rig.close)

    def test_lines_break_at_newline_only(self):
        self.assertEqual(sf.conf_lines("a\x0bb\nc\r\nd\re\n"), ["a\x0bb", "c", "d\re", ""])
        self.assertEqual(sf.conf_lines("last\r"), ["last\r"], "a \\r at the very end is no Windows line end")

    def test_a_smartctl_line_with_any_other_separator_is_refused_and_nothing_runs(self):
        for separator in self.SEPARATORS:
            with self.subTest(separator=hex(ord(separator))):
                self.rig.write_conf(f"smartctl d1 -d sat %s{separator}exclude x;touch /tmp/owned\n")
                for verb in ("smart", "status", "guard", "probe"):
                    code, answer = self.rig.ask({"v": 1, "verb": verb})
                    self.assertEqual((code, answer["error"]), (2, "unsafe_conf"), (verb, answer))
                    self.assertIn("line 13", answer["words"])
                    self.assertEqual(plain_terms(answer["words"]), [], "the card shows these words (U7)")
                _code, info = self.rig.ask({"v": 1, "verb": "info"})
                self.assertFalse(info["conf_ok"])
                self.assertEqual(info["conf_problems"][0]["line"], 13)
        self.assertEqual(self.rig.commands_run(), [], "SnapRAID was never started")

    def test_explain_refuses_it_too(self):
        """The owner's review path: explain must not call such a config one the reader would run."""
        rig = self.rig
        Path(str(rig.conf) + ".new").write_text(rig.conf.read_text() + "smartctl d1 -d sat %s\x0bexclude x;id\n")
        out = io.StringIO()
        self.assertEqual(sf.main(["explain", "snapraid-config"], stdout=out, paths=rig.paths, system=rig.system,
                                 lstat=rig.lstat), 2)
        self.assertIn("control character", out.getvalue())
        self.assertNotIn("would run this config", out.getvalue())
        Path(str(rig.conf) + ".new").write_bytes(rig.conf.read_bytes() + b"exclude /caf\xe9/\n")
        out = io.StringIO()
        self.assertEqual(sf.main(["explain", "snapraid-config"], stdout=out, paths=rig.paths, system=rig.system,
                                 lstat=rig.lstat), 2, "not UTF-8: refused as every call refuses it")

    def test_windows_line_ends_are_read_as_snapraid_reads_them(self):
        self.rig.write_conf(text=self.rig.conf.read_text().replace("\n", "\r\n"))
        self.assertEqual(self.rig.ask({"v": 1, "verb": "status"})[0], 0)

    def test_the_tokenizer_follows_snapraids(self):
        conf = sf.parse_conf("data\td1 /mnt/disk1/  \nparity /mnt/p1/a,,/mnt/p1/b\t\n smartctl d1 -d sat %s\n"
                             "#c\n\t\nnohidden yes\n1-parity /x\nsmartctl\td2\t-d sat %s \n")
        self.assertEqual(conf.data, [("d1", "/mnt/disk1/", 1)])
        self.assertEqual(conf.parity, [("parity", ["/mnt/p1/a", "/mnt/p1/b"], 2)])
        self.assertEqual(conf.smartctl, [("d2", "-d sat %s", 8)])
        words = " | ".join(problem["words"] for problem in conf.problems)
        self.assertIn("line 3 starts with a space or a tab", words)
        self.assertIn("line 6: nohidden takes nothing after it", words)
        self.assertIn("line 7: protection level 1 is named twice ('parity' and '1-parity')", words)
        self.assertEqual(plain_terms(words), [])

    def test_no_internal_term_in_the_readers_sentences(self):
        """U7 for the reader: every sentence it writes for a person (refusals, the config's problems, the guard's
        checks, explain) holds no internal term of the design's words table, once paths, placeholders and quoted
        SnapRAID names are left out."""
        import ast

        tree = ast.parse(Path(sf.__file__).read_text(encoding="utf-8"))
        texts: list[str] = []

        def strings(node: ast.AST) -> list[str]:
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return [node.value]
            if isinstance(node, ast.JoinedStr):
                return [part.value for part in node.values if isinstance(part, ast.Constant)]
            if isinstance(node, ast.BinOp):
                return strings(node.left) + strings(node.right)
            if isinstance(node, ast.Dict):
                return [text for key, value in zip(node.keys, node.values)
                        if isinstance(key, ast.Constant) and key.value == "words" for text in strings(value)]
            return []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else \
                node.func.attr if isinstance(node.func, ast.Attribute) else ""
            if name == "Refused":
                texts += [text for arg in node.args[1:] for text in strings(arg)]
            elif name in ("add", "out", "append"):
                texts += [text for arg in node.args for text in strings(arg)]
        texts += list(sf.GUARD_WORDS.values()) + list(sf.OUTCOME_WORDS.values())
        self.assertGreater(len(texts), 80)
        for text in texts:
            if re.fullmatch(r"[a-z0-9_]+", text):
                continue        # a reason code, never shown as words
            cleaned = re.sub(r"(helper|snapraid)-config", "", text)
            with self.subTest(text=text[:60]):
                self.assertEqual(plain_terms(cleaned), [], text)


class LevelNameTests(unittest.TestCase):
    """A protection disk's SMART and probe lines carry SnapRAID's level name ("parity", "2-parity"), whatever
    keyword the config used: "1-parity", "q-parity", "r-parity" and "z-parity" are matched by their level."""

    def test_1_parity_and_q_parity_get_their_readings(self):
        probe = "version:13.0\nprobe:/dev/sdp:parity:1\nprobe:/dev/sdq:2-parity:0\n"
        smart = (FIXTURES / "smart-fake.log").read_text(encoding="utf-8") + \
            "smart:/dev/sdq:2-parity\nattr:/dev/sdq:2-parity:serial:Q\nattr:/dev/sdq:2-parity:flags:8:8\n"
        rig = Rig({**HEALTHY, "smart": {"text": smart, "exit": 1}, "probe": {"text": probe, "exit": 0}})
        self.addCleanup(rig.close)
        rig.write_conf(text=rig.conf.read_text().replace("parity ", "1-parity ", 1).replace("2-parity ", "q-parity "))
        mounts = {**{k: v for k, v in UUIDS.items() if k.startswith("d")}, "1-parity": UUIDS["parity"],
                  "q-parity": UUIDS["2-parity"]}
        rig.write_helper_cfg({**rig.helper_cfg, "mounts": mounts})
        code, answer = rig.ask({"v": 1, "verb": "smart"})
        self.assertEqual(code, 0, answer)
        expected = sf.read_smart(fixture("smart-fake"))["devices"]["parity"]["health"]
        self.assertNotEqual(expected, "unknown")
        self.assertEqual(answer["disks"]["1-parity"]["health"], expected)
        self.assertEqual(answer["disks"]["q-parity"]["health"], "fail")
        _code, power = rig.ask({"v": 1, "verb": "probe"})
        self.assertEqual(power["disks"], {"d1": "unknown", "d2": "unknown", "d3": "unknown", "1-parity": "active",
                                          "q-parity": "standby"})
        _code, status = rig.ask({"v": 1, "verb": "status"})
        self.assertEqual((status["mounts"]["1-parity"]["number"], status["mounts"]["q-parity"]["number"]), (1, 2))


if __name__ == "__main__":
    unittest.main()


class VersionPolicyTests(unittest.TestCase):
    """T50: the version policy of design section 8.2, and the daemon's presence (section 8.1)."""

    def setUp(self):
        self.rig = Rig(dict(HEALTHY))
        self.addCleanup(self.rig.close)

    def version(self, value: str) -> None:
        self.rig.scenario.write_text(json.dumps({**json.loads(self.rig.scenario.read_text()), "version": value}))

    def test_v0_is_13_only_when_the_package_owns_the_binary(self):
        rig = self.rig
        self.version("0")
        status, answer = rig.ask({"v": 1, "verb": "status"})
        self.assertEqual(answer["error"], "no_snapraid", "a v0 binary no package owns")
        rig.system.dpkg = {"/usr/bin/snapraid": ("snapraid", "13.0-1")}
        status, answer = rig.ask({"v": 1, "verb": "info"})
        self.assertEqual((answer["snapraid"]["version"], answer["snapraid"]["note"]),
                         ("13.0", "the system's package, which does not report its own version"))
        self.assertEqual(rig.ask({"v": 1, "verb": "status"})[0], 0)
        rig.system.dpkg = {"/usr/bin/snapraid": ("snapraid", "12.2-1")}
        self.assertEqual(rig.ask({"v": 1, "verb": "status"})[1]["error"], "no_snapraid")

    def test_two_binaries_of_different_versions_are_refused(self):
        rig = self.rig
        other = rig.root / "usr/local/bin"
        other.mkdir(parents=True, exist_ok=True)
        (other / "snapraid").write_text("#!/bin/sh\necho 'snapraid v15.1 by Andrea Mazzoleni'\n", encoding="utf-8")
        (other / "snapraid").chmod(0o755)
        status, answer = rig.ask({"v": 1, "verb": "status"})
        self.assertEqual(answer["error"], "two_binaries")
        self.assertIn("is 13.0", answer["words"])
        self.assertIn("is 15.1", answer["words"])
        self.assertEqual(rig.commands_run(), [])

    def test_14_is_read_with_care_and_15_only_with_the_daemon(self):
        rig = self.rig
        self.version("14.10")
        self.assertEqual(rig.ask({"v": 1, "verb": "status"})[0], 0)
        status, info = rig.ask({"v": 1, "verb": "info"})
        self.assertTrue(info["snapraid"]["read_only"])
        for verb in ("guard", "list"):
            self.assertEqual(rig.ask({"v": 1, "verb": verb})[1]["error"], "not_for_this_version")
        self.version("15.1")
        self.assertEqual(rig.ask({"v": 1, "verb": "status"})[1]["error"], "no_snapraid")

    def test_with_the_daemon_installed_snapraid_never_starts(self):
        rig = self.rig
        unit = rig.root / "etc/systemd/system"
        unit.mkdir(parents=True)
        (unit / "snapraidd.service").write_text("[Service]\nExecStart=/usr/bin/snapraidd\n", encoding="utf-8")
        self.version("15.1")
        (rig.root / "etc/snapraidd.conf").write_text("net_port = 127.0.0.1:7627\nnet_auth_credential = u:$argon2id$x\n"
                                                     "notify_result = /usr/local/libexec/seraphina/sf-snapraidd "
                                                     "notify result --narrow %l %h %c \"%s\"\n", encoding="utf-8")
        for verb in ("status", "smart", "probe", "guard", "list"):
            self.assertEqual(rig.ask({"v": 1, "verb": verb})[1]["error"], "daemon_runs", verb)
        status, info = rig.ask({"v": 1, "verb": "info"})
        self.assertEqual(status, 0)
        self.assertEqual(info["daemon"]["net_port"], ["127.0.0.1:7627"])
        self.assertTrue(info["daemon"]["auth_set"] and info["daemon"]["notify_forwarder"])
        self.assertNotIn("argon2", json.dumps(info))
        self.assertNotIn("%l", json.dumps(info))
        self.assertEqual(rig.commands_run(), [], "only --version ran")

    def test_versions_it_does_not_read_are_named_honestly(self):
        self.version("11.6")
        words = self.rig.ask({"v": 1, "verb": "status"})[1]["words"]
        self.assertIn("is SnapRAID 11.6, not a version Seraphina reads", words)
        self.assertNotIn("SnapRAID Daemon", words)
        self.version("15.1")
        words = self.rig.ask({"v": 1, "verb": "status"})[1]["words"]
        self.assertIn("is SnapRAID 15.1, which needs the SnapRAID Daemon, and the SnapRAID Daemon is not installed",
                      words)

    def test_info_on_the_daemon_route_hashes_nothing_and_opens_no_array_disk(self):
        """The daemon rewrites the content file at every run and the reader keeps no mark then: info reads its
        size and times only, and checks only the copy on the system disk (no data disk is read or woken)."""
        rig = self.rig
        unit = rig.root / "etc/systemd/system"
        unit.mkdir(parents=True)
        (unit / "snapraidd.service").write_text("[Service]\nExecStart=/usr/bin/snapraidd\n", encoding="utf-8")
        self.version("15.1")
        hashed, opened = [], []
        real_hash, real_open = sf.file_sha256, os.open

        def counting_hash(path):
            hashed.append(path)
            return real_hash(path)

        def counting_open(path, *args, **kwargs):
            if str(path).endswith("snapraid.content"):
                opened.append(str(path))
            return real_open(path, *args, **kwargs)
        sf.file_sha256 = counting_hash
        self.addCleanup(setattr, sf, "file_sha256", real_hash)
        sf.os.open = counting_open
        self.addCleanup(setattr, sf.os, "open", real_open)
        for _ in range(3):
            status, info = rig.ask({"v": 1, "verb": "info"})
            self.assertEqual(status, 0, info)
        self.assertEqual([h for h in hashed if h.endswith("snapraid.content")], [], "no content file is hashed")
        self.assertTrue(opened and all("/var/snapraid/" in path for path in opened), opened)
        content = info["content"]
        self.assertEqual(content["unchecked"], 2)
        self.assertEqual([c.get("checked", True) for c in content["copies"]], [True, False, False])
        self.assertIsNone(content["problem"])
        self.assertFalse(content["missing_all"] or content["too_few_disks"])
        self.assertTrue(info["fingerprint"]["present"])
        self.assertEqual(rig.commands_run(), [], "only --version ran")

    def test_reads_wait_while_snapraid_runs(self):
        rig = self.rig
        rig.system.running = ["snapraid"]
        for verb in ("status", "guard", "list"):
            self.assertEqual(rig.ask({"v": 1, "verb": verb})[1]["error"], "waiting")
        self.assertEqual(rig.ask({"v": 1, "verb": "info"})[0], 0)
        self.assertTrue(rig.ask({"v": 1, "verb": "info"})[1]["snapraid_running"])
        self.assertEqual(rig.commands_run(), [])

    def test_inventory_places_carry_their_sha256(self):
        rig = self.rig
        (rig.root / "etc").mkdir(exist_ok=True)
        (rig.root / "etc/crontab").write_text("0 3 * * * root snapraid sync\n", encoding="utf-8")
        status, info = rig.ask({"v": 1, "verb": "info"})
        place = info["schedulers"]["found"][0]
        self.assertEqual(place["sha256"], sf.file_sha256(str(rig.root / "etc/crontab")))


class LayoutTests(unittest.TestCase):
    """T51: content copies in /var/lib/snapraid and at a disk's root, a disk being taken out, sibling mounts."""

    def setUp(self):
        self.rig = Rig(dict(HEALTHY))
        self.addCleanup(self.rig.close)

    def problems(self, conf_text: str, cfg: dict | None = None, mounts=None) -> list[str]:
        conf = sf.parse_conf(conf_text)
        system = self.rig.system
        return [p["words"] for p in sf.check_conf(conf, cfg or self.rig.helper_cfg, mounts or system.mounts(),
                                                  system.uuid, self.rig.paths, self.rig.lstat)]

    def conf(self, **swap: str) -> str:
        text = self.rig.conf.read_text(encoding="utf-8")
        for old, new in swap.items():
            text = text.replace(old, new)
        return text

    def test_content_in_var_lib_and_at_a_disk_root(self):
        r = self.rig.root
        (r / "var/lib/snapraid").mkdir(parents=True)
        text = self.conf(**{f"{r}/var/snapraid/snapraid.content": f"{r}/var/lib/snapraid/snapraid.content",
                            f"{r}/mnt/disk1/.snapraid/snapraid.content": f"{r}/mnt/disk1/snapraid.content"})
        self.assertEqual(self.problems(text), [])

    def test_a_disk_being_taken_out(self):
        r = self.rig.root
        holder = r / "var/lib/snapraid-placeholder/d3"
        holder.mkdir(parents=True)
        text = self.conf(**{f"data d3 {r}/mnt/disk3/": f"data d3 {holder}/"})
        cfg = {**self.rig.helper_cfg, "placeholders": {"d3": str(holder)}}
        self.assertEqual(self.problems(text, cfg), [])
        self.assertTrue(any("system folder" in p or "recorded" in p for p in self.problems(text)), "undeclared")
        (holder / "stray.txt").write_text("x")
        self.assertTrue(any("not empty" in p for p in self.problems(text, cfg)))
        (holder / "stray.txt").unlink()
        self.rig.write_helper_cfg(cfg)
        self.rig.write_conf(text=text)
        status, info = self.rig.ask({"v": 1, "verb": "info"})
        self.assertTrue(info["mounts"]["d3"]["placeholder"])
        self.assertTrue(info["conf_ok"], info["conf_problems"])

    def test_sibling_mounts_and_a_subfolder_bind(self):
        r = self.rig.root
        base = self.rig.system.mounts()
        twin = sf.Mount(str(r / "mnt/disk1-alias"), "ext4", "/dev/loop0", "7:0")
        for order in ((twin, *base[1:]), (*base[1:], twin)):
            with self.subTest(first=order[0].point):
                mounts = [base[0], *order]
                self.assertEqual(self.problems(self.rig.conf.read_text(), mounts=mounts), [])
                alias = self.conf(**{f"data d1 {r}/mnt/disk1/": f"data d1 {r}/mnt/disk1-alias/",
                                     f"{r}/mnt/disk1/.snapraid": f"{r}/mnt/disk1-alias/.snapraid"})
                self.assertEqual(self.problems(alias, mounts=mounts), [])
        bind = sf.Mount(str(r / "mnt/disk1-bind"), "ext4", "/dev/loop0", "7:0", root="/sub")
        text = self.conf(**{f"data d1 {r}/mnt/disk1/": f"data d1 {r}/mnt/disk1-bind/"})
        self.assertTrue(any("bind" in p for p in self.problems(text, mounts=[*base, bind])))
        twin_parity = sf.Mount(str(r / "mnt/parity1-alias"), "ext4", "/dev/loop3", "7:3")
        (r / "mnt/parity1-alias").mkdir()
        text = self.conf(**{f"parity {r}/mnt/parity1/snapraid.parity": f"parity {r}/mnt/parity1-alias/snapraid.parity"})
        self.assertEqual(self.problems(text, mounts=[*base, twin_parity]), [])

    def test_15_keywords_and_forbidden_options(self):
        text = self.rig.conf.read_text() + f"extra x1 {self.rig.root}/mnt/extra/\n"
        self.assertEqual(sf.parse_conf(text).problems, [])
        for option in ("-W", "--force-realloc-tail", "--gui-threshold-removes"):
            with self.subTest(option=option), self.assertRaises(RuntimeError):
                sf.check_argv(["/usr/bin/snapraid", "-c", "c", "-l", "l", option, "status"])


class Pid1Tests(unittest.TestCase):
    """Section 8.4: with "pid1": true each request is handed to the same program in a transient unit."""

    def test_the_request_goes_through_pid1(self):
        rig = Rig(dict(HEALTHY))
        self.addCleanup(rig.close)
        rig.write_helper_cfg({**rig.helper_cfg, "pid1": True})
        calls = []
        out = io.StringIO()
        sf.main([], stdin=io.BytesIO(b'{"v":1,"verb":"info"}\n'), stdout=out, paths=rig.paths, system=rig.system,
                lstat=rig.lstat, execv=lambda path, argv: calls.append((path, argv)), environ={})
        self.assertEqual(calls[0][0], "/usr/bin/systemd-run")
        self.assertEqual(calls[0][1][-2:], [rig.paths.helper, "inner"])
        self.assertIn("--pipe", calls[0][1])
        code = sf.main(["inner"], stdin=io.BytesIO(b'{"v":1,"verb":"info"}\n'), stdout=out, paths=rig.paths,
                       system=rig.system, lstat=rig.lstat, environ={"INVOCATION_ID": "x"})
        self.assertEqual(code, 0)
        self.assertEqual(sf.main(["inner"], stdin=io.BytesIO(b"{}\n"), stdout=io.StringIO(), paths=rig.paths,
                                 system=rig.system, lstat=rig.lstat, environ={}), 2, "inner only inside a unit")
