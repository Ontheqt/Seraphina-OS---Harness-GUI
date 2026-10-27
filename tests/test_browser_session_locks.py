"""Grants, session locks, lock numbers, the action log and the caps (BROWSER_AND_CODEX.md §2.5, §2.6; T10's unit
half and T22)."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from swfactory.browser import ERROR_CODES, UNTRUSTED_README
from swfactory.browser import session_locks as SL
from swfactory.browser.fixture import FIXTURE_ORIGIN

ROOT = Path(__file__).resolve().parents[1]
CUSTODY_WORDS = ("lease", "holder", "generation", "epoch", "journal", "granted", "reason_code", "resident", "held",
                 "lost", "released", "unreleased")


def grant(data_dir: Path, profile: str = "fixture", kind: str = "chat", ref: str = "chat:1") -> tuple[str, Path]:
    return SL.write_grant(data_dir, locked_by={"kind": kind, "ref": ref, "run": "01-turn", "agent": "kate-browser"},
                          profile=profile, origins={"read": [FIXTURE_ORIGIN], "write": [FIXTURE_ORIGIN]},
                          fixture_origins=[FIXTURE_ORIGIN], paths={})


def words(name: str) -> list[str]:
    """A name's whole words: split at _, -, . and spaces, and at lower-to-upper case changes."""
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", str(name))
    return [part.lower() for part in re.split(r"[_\-.\s/:]+", spaced) if part]


def custody_word(name: str) -> str | None:
    """The custody word a name is made of, as a whole word or a whole name ("release" and "code" do not match)."""
    lowered = str(name).lower()
    for word in CUSTODY_WORDS:
        if lowered == word or word in words(name) or ("_" in word and word in lowered.split(".")):
            return word
    return None


class GrantTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data = Path(tempfile.mkdtemp())

    def test_a_grant_is_private_and_checked(self) -> None:
        grant_id, path = grant(self.data)
        self.assertRegex(grant_id, SL.GRANT_ID_RE)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        data = SL.read_grant(path)
        self.assertEqual(data["profile"], "fixture")
        self.assertEqual(data["limits"]["max_minutes"], 60)
        self.assertIsNone(data["revoked"])
        other_id, _ = grant(self.data)
        self.assertNotEqual(grant_id, other_id)

    def test_what_is_not_a_grant(self) -> None:
        bad = self.data / "G-20260101-0001.json"
        for text in ("not json", json.dumps({"grant": "G-20260101-0002"}), json.dumps([1])):
            bad.write_text(text)
            with self.assertRaises(SL.GrantError):
                SL.read_grant(bad)
        with self.assertRaises(SL.GrantError):
            SL.read_grant("relative.json")
        with self.assertRaises(SL.GrantError):
            SL.read_grant(self.data / "missing.json")
        with self.assertRaises(ValueError):
            SL.write_grant(self.data, locked_by={"kind": "model"}, profile="fixture", origins={"read": []},
                           fixture_origins=[], paths={})

    def test_revoke_marks_the_grant(self) -> None:
        _, path = grant(self.data)
        SL.revoke_grant(path)
        self.assertTrue(SL.read_grant(path)["revoked"]["at"])

    def test_revoke_reaches_a_reader_of_the_same_file(self) -> None:
        """The namespace binds the grant as one file (its inode): revoke and run end change it in place, so the
        shim's per-call read sees them (rule 3)."""
        _, path = grant(self.data)
        inode = path.stat().st_ino
        fd = os.open(path, os.O_RDONLY)   # what the bind mount pins
        self.addCleanup(os.close, fd)
        SL.revoke_grant(path)
        self.assertEqual(path.stat().st_ino, inode)
        seen = json.loads(os.pread(fd, SL.MAX_GRANT_BYTES, 0).decode())
        self.assertTrue(seen["revoked"]["at"])
        SL.update_grant(path, ended="2026-10-27T00:00:00Z")
        seen = json.loads(os.pread(fd, SL.MAX_GRANT_BYTES, 0).decode())
        self.assertEqual((bool(seen["revoked"]), seen["ended"]), (True, "2026-10-27T00:00:00Z"))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


class SessionLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data = Path(tempfile.mkdtemp())

    def test_one_session_lock_per_profile_and_none_stolen(self) -> None:
        _, first = grant(self.data, ref="chat:1")
        lock = SL.take_session_lock(self.data, SL.read_grant(first))
        self.addCleanup(lock.close)
        self.assertRegex(lock.session_lock, SL.SESSION_LOCK_ID_RE)
        record = json.loads((lock.folder / SL.SESSION_FILE).read_text())
        self.assertEqual((record["lock_number"], record["locked_by"]["ref"]), (1, "chat:1"))
        self.assertEqual((lock.folder / SL.README_NAME).read_text(), UNTRUSTED_README)
        _, second = grant(self.data, kind="job", ref="job:7")
        with self.assertRaises(SL.SessionLockBusy) as caught:
            SL.take_session_lock(self.data, SL.read_grant(second))
        self.assertEqual(caught.exception.locked_by_kind, "chat")   # the kind of run that has it, nothing more
        _, other_profile = grant(self.data, profile="fixture-login")
        other = SL.take_session_lock(self.data, SL.read_grant(other_profile))   # another profile is free
        other.close()

    def test_the_same_grant_twice_is_busy(self) -> None:
        _, path = grant(self.data)
        data = SL.read_grant(path)
        lock = SL.take_session_lock(self.data, data)
        self.addCleanup(lock.close)
        os.close(lock.profile_fd)   # the profile lock alone would refuse first; the grant's own lock refuses too
        lock.profile_fd = os.open(os.devnull, os.O_RDONLY)
        with self.assertRaises(SL.SessionLockBusy):
            SL.take_session_lock(self.data, data)

    def test_lock_numbers_rise_and_an_old_one_is_refused(self) -> None:
        numbers = []
        logs = []
        for _ in range(3):
            _, path = grant(self.data)
            lock = SL.take_session_lock(self.data, SL.read_grant(path))
            numbers.append(lock.lock_number)
            logs.append(SL.ActionLog(lock.folder / SL.ACTION_LOG, lock.lock_number, lock.number_file))
            lock.close()
        self.assertEqual(numbers, [1, 2, 3])
        logs[-1].intent("A-1", **{"class": "read"})
        with self.assertRaises(SL.StaleLockNumber):
            logs[0].intent("A-1", **{"class": "read"})

    def test_the_kernel_frees_the_lock_when_its_process_dies(self) -> None:
        _, path = grant(self.data)
        code = ("import sys, time; sys.path.insert(0, sys.argv[1]); from swfactory.browser import session_locks as S;"
                "lock = S.take_session_lock(sys.argv[2], S.read_grant(sys.argv[3])); print('locked', flush=True);"
                "time.sleep(60)")
        proc = subprocess.Popen([sys.executable, "-c", code, str(ROOT), str(self.data), str(path)],
                                stdout=subprocess.PIPE, text=True)
        self.assertEqual(proc.stdout.readline().strip(), "locked")
        _, second = grant(self.data)
        with self.assertRaises(SL.SessionLockBusy):
            SL.take_session_lock(self.data, SL.read_grant(second))
        proc.kill()   # kill -9: no code of its own runs
        proc.wait(5)
        proc.stdout.close()
        lock = SL.take_session_lock(self.data, SL.read_grant(second))
        self.assertEqual(lock.lock_number, 2)
        lock.close()

    def test_two_launchers_racing_leave_exactly_one_locking_run(self) -> None:
        paths = [grant(self.data, ref=f"chat:{n}")[1] for n in range(6)]
        code = ("import sys, time; sys.path.insert(0, sys.argv[1]); from swfactory.browser import session_locks as S\n"
                "try:\n    lock = S.take_session_lock(sys.argv[2], S.read_grant(sys.argv[3])); print('locked', flush=True)"
                "; time.sleep(3)\nexcept S.SessionLockBusy:\n    print('busy', flush=True)")
        procs = [subprocess.Popen([sys.executable, "-c", code, str(ROOT), str(self.data), str(p)],
                                  stdout=subprocess.PIPE, text=True) for p in paths]
        answers = sorted(proc.stdout.readline().strip() for proc in procs)
        for proc in procs:
            proc.wait(10)
            proc.stdout.close()
        self.assertEqual(answers.count("locked"), 1, answers)
        self.assertEqual(answers.count("busy"), 5, answers)

    def test_dangling_intents_become_unknown_at_the_next_start(self) -> None:
        _, path = grant(self.data)
        lock = SL.take_session_lock(self.data, SL.read_grant(path))
        log = SL.ActionLog(lock.folder / SL.ACTION_LOG, lock.lock_number, lock.number_file)
        log.intent("A-1", **{"class": "commit", "tool": "click"})
        log.intent("A-2", **{"class": "input", "tool": "fill"})
        log.outcome("A-2", "ok")
        lock.close()   # the run died with A-1 in flight
        _, again = grant(self.data)
        nxt = SL.take_session_lock(self.data, SL.read_grant(again))
        nxt.close()
        actions = {a["action_id"]: a for a in SL.ActionLog.actions(lock.folder / SL.ACTION_LOG)}
        self.assertEqual(actions["A-1"]["outcome"], "unknown")
        self.assertTrue(actions["A-1"]["settled_at_start"])
        self.assertEqual(actions["A-2"]["outcome"], "ok")
        self.assertEqual(SL.ActionLog.dangling(lock.folder / SL.ACTION_LOG), [])


class PlantedFileTests(unittest.TestCase):
    """The session folder is writable inside the namespace: a FIFO or a symlink planted there never blocks or
    misleads the harness (finish, the next launcher's settle, the browser page)."""

    def setUp(self) -> None:
        self.data = Path(tempfile.mkdtemp())
        self.outside = self.data / "outside.json"
        self.outside.write_text(json.dumps({"session_lock": "SL-20260101-9999", "profile": "fixture",
                                            "grant": "G-20260101-9999"}))

    def session(self) -> tuple[str, Path, Path]:
        grant_id, path = grant(self.data)
        lock = SL.take_session_lock(self.data, SL.read_grant(path))
        log = SL.ActionLog(lock.folder / SL.ACTION_LOG, lock.lock_number, lock.number_file)
        log.intent("A-1", **{"class": "commit", "tool": "click"})
        lock.close()
        return grant_id, path, lock.folder

    def within(self, seconds: float, call, *args):
        out: list = []
        thread = threading.Thread(target=lambda: out.append(call(*args)), daemon=True)
        thread.start()
        thread.join(seconds)
        self.assertFalse(thread.is_alive(), f"{getattr(call, '__name__', call)} blocked on a planted file")
        return out[0] if out else None

    def test_fifos_and_symlinks_never_block_or_mislead(self) -> None:
        from swfactory.browser import runs

        for name, plant in (("session_lock.json", "fifo"), ("action-log.jsonl", "fifo"),
                            ("session_lock.json", "symlink"), ("action-log.jsonl", "symlink")):
            with self.subTest(name=name, plant=plant):
                grant_id, path, folder = self.session()
                (folder / name).unlink()
                if plant == "fifo":
                    os.mkfifo(folder / name)
                else:
                    (folder / name).symlink_to(self.outside)
                # the run's own end, the next launcher of the profile, and the browser page
                record = self.within(3, runs.finish, self.data,
                                     runs.Grant(grant_id, path, "fixture", "browser", {"run": "01-turn"}))
                self.assertEqual(record["session_lock"], folder.name)   # found from the core's own index
                self.assertEqual(SL.session_of_grant(self.data, grant_id), folder)
                self.assertIsNone(SL.session_of_grant(self.data, "G-20260101-9999"))   # the planted claim
                _, other = grant(self.data)
                self.within(3, lambda: SL.take_session_lock(self.data, SL.read_grant(other)).close())
                self.within(3, runs.recent_sessions, self.data)
                if name == SL.ACTION_LOG:
                    self.assertEqual(self.within(3, SL.ActionLog.read, folder / name), [])
                    with self.assertRaises(OSError):   # never written through, never blocking
                        SL.ActionLog.append_raw(folder / name, {"kind": "x"})
                self.assertEqual(self.outside.read_text().count("SL-20260101-9999"), 1)   # never written through

    def test_a_planted_temp_file_never_blocks_the_receipt(self) -> None:
        from swfactory.browser import runs

        grant_id, path, folder = self.session()
        os.mkfifo(folder / f".{SL.RECEIPT_FILE}.{os.getpid()}.tmp")   # the old, predictable temp name
        record = self.within(3, runs.finish, self.data, runs.Grant(grant_id, path, "fixture", "browser",
                                                                    {"run": "01-turn"}))
        self.assertTrue((folder / SL.RECEIPT_FILE).is_file())
        self.assertEqual(record["unknown"], 1)


class CapsTests(unittest.TestCase):
    def test_old_and_oversized_sessions_go(self) -> None:
        data = Path(tempfile.mkdtemp())
        folders = []
        for index in range(4):
            _, path = grant(data)
            lock = SL.take_session_lock(data, SL.read_grant(path))
            lock.close()
            (lock.folder / "artifacts" / "shot.png").write_bytes(b"x" * 1000)
            folders.append(lock.folder)
        old = time.time() - 40 * 86400
        os.utime(folders[0], (old, old))
        deleted = SL.prune_sessions(data, max_bytes=2500, max_days=30)
        self.assertIn(folders[0].name, deleted)          # older than 30 days
        self.assertIn(folders[1].name, deleted)          # the oldest until the rest fit
        self.assertTrue(folders[3].is_dir())
        self.assertFalse(folders[0].exists())


class VocabularyTests(unittest.TestCase):
    """T22 (K6): the browser's records use the session lock's own words; custody's records never the browser's."""

    def test_the_word_rule_itself(self) -> None:
        self.assertEqual(custody_word("lease_id"), "lease")
        self.assertEqual(custody_word("heldBy"), "held")
        self.assertIsNone(custody_word("release"))
        self.assertIsNone(custody_word("code"))
        self.assertIsNone(custody_word("session_lock_busy"))

    def test_browser_names_avoid_custody_words(self) -> None:
        data = Path(tempfile.mkdtemp())
        _, path = grant(data)
        lock = SL.take_session_lock(data, SL.read_grant(path))
        log = SL.ActionLog(lock.folder / SL.ACTION_LOG, lock.lock_number, lock.number_file)
        log.intent("A-1", **{"class": "commit", "tool": "click", "target": {"ref": "e7", "role": "button"}})
        log.outcome("A-1", "unknown", response={"tool_status": "timeout_sent", "requests": []})
        lock.close()
        names: list[str] = list(ERROR_CODES)
        names += [SL.ACTION_LOG, SL.SESSION_FILE, SL.RECEIPT_FILE, SL.FIXTURE_LOG, SL.README_NAME, "grants", "locks",
                  "sessions", "artifacts", "work", "browser.grant", "browser.receipt", "browser.revoked"]

        def keys(value: object) -> None:
            if isinstance(value, dict):
                for key, inner in value.items():
                    names.append(str(key))
                    keys(inner)
            elif isinstance(value, list):
                for inner in value:
                    keys(inner)
        keys(SL.read_grant(path))
        keys(json.loads((lock.folder / SL.SESSION_FILE).read_text()))
        for entry in SL.ActionLog.read(lock.folder / SL.ACTION_LOG):
            keys(entry)
        from swfactory.browser import runs

        keys(runs.receipt(data, path.stem))
        keys(runs.card({}))
        found = {name: custody_word(name) for name in names if custody_word(name)}
        self.assertEqual(found, {})
        for label in runs.card({})["limitations"]:
            self.assertFalse([w for w in words(label) if w in CUSTODY_WORDS], label)

    def test_custody_records_carry_no_browser_name(self) -> None:
        text = (ROOT / "swfactory" / "custody.py").read_text()
        for name in ("session_lock", "lock_number", "locked_by"):
            self.assertNotIn(name, text)

    def test_the_gui_labels_avoid_custody_words(self) -> None:
        view = (ROOT / "swfactory" / "web" / "views" / "browser.js").read_text()
        match = re.search(r"const WORDS = \{(.*?)\n  \};", view, re.DOTALL)
        self.assertIsNotNone(match, "views/browser.js keeps every word a person reads in WORDS")
        # single-quoted (the file's own style), double-quoted and template strings alike
        found = re.findall(r"'((?:[^'\\]|\\.)*)'|\"((?:[^\"\\]|\\.)*)\"|`((?:[^`\\]|\\.)*)`", match.group(1))
        labels = [next(part for part in groups if part) for groups in found if any(groups)]
        self.assertGreater(len(labels), 50, "the extraction found the labels")
        for label in labels:
            self.assertFalse([w for w in words(label) if w in CUSTODY_WORDS], label)
        keys = re.findall(r"(?:^|[{,]\s*)([A-Za-z_]\w*):", match.group(1), re.MULTILINE)
        self.assertGreater(len(keys), 50)
        for key in keys:   # the keys become data and dataset names
            self.assertIsNone(custody_word(key), key)


if __name__ == "__main__":
    unittest.main()
