from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import tracemalloc
import unittest
from pathlib import Path
from typing import Any, Callable
from unittest import mock

from swfactory import jobs
from swfactory.jobs import JOB_ID_RE, JobStore, read_verdict, slugify, utc_now

TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
REPO_ROOT = Path(__file__).resolve().parents[1]

SECTION_4_KEYS = {
    "id", "repo", "feature", "base", "base_commit", "branch", "worktree", "job_dir", "port",
    "stage", "detail", "round", "resumes", "max_resumes", "reviewers", "reviews", "history",
    "decision", "reason", "agents", "usage", "merged_commit", "error", "created_at", "updated_at",
}
REVIEWERS = ["security", "ux", "ui", "code"]


def run_threads(target, count: int) -> None:
    threads = [threading.Thread(target=target, args=(n,)) for n in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


class TempDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.data_dir = self.tmp / "data"
        self.store = JobStore(self.data_dir)

    def new_job(self, feature: str = "Rate limit the signup form", repo: str = "my-app",
                **kwargs) -> dict:
        return self.store.new_job(repo, feature, kwargs.pop("reviewers", REVIEWERS),
                                  kwargs.pop("max_resumes", 2), **kwargs)

    def temp_files(self, root: Path) -> list[Path]:
        return [p for p in root.rglob("*") if p.name.startswith(".sf-")]

    def without_blocking(self, fn: Callable[[], Any], fifo: Path, timeout: float = 5.0) -> Any:
        """fn()'s result (or exception); fails, after releasing any reader, if fn blocks on fifo."""
        outcome: dict[str, Any] = {}

        def target() -> None:
            try:
                outcome["value"] = fn()
            except BaseException as exc:  # re-raised in the test thread
                outcome["error"] = exc

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            try:
                os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))  # EOF for the blocked reader
            except OSError:
                pass
            thread.join(timeout)
            self.fail(f"blocked on FIFO {fifo}")
        if "error" in outcome:
            raise outcome["error"]
        return outcome["value"]


class UtcNowTest(unittest.TestCase):
    def test_format_is_utc_seconds_with_z(self) -> None:
        self.assertRegex(utc_now(), TS_RE)


class SlugifyTest(unittest.TestCase):
    def test_design_example(self) -> None:
        self.assertEqual(slugify("Rate limit the signup form"), "rate-limit-signup-form")

    def test_drops_stopwords(self) -> None:
        self.assertEqual(slugify("Add a button to the page for my users"), "add-button-page-users")
        stopwords = "a an the to of for and or in on with by from into my our your this that be is are"
        self.assertEqual(slugify(stopwords.upper()), "job")

    def test_keeps_first_five_words(self) -> None:
        self.assertEqual(slugify("one two three four five six seven"), "one-two-three-four-five")

    def test_non_alphanumerics_become_separators(self) -> None:
        self.assertEqual(slugify("Fix: crash (bug #42) in /api/v2!!"), "fix-crash-bug-42-api")
        self.assertEqual(slugify("  snake_case\tand\nnewlines  "), "snake-case-newlines")

    def test_lowercases(self) -> None:
        self.assertEqual(slugify("ADD Login PAGE"), "add-login-page")

    def test_trims_to_forty_chars_at_word_boundary(self) -> None:
        slug = slugify("internationalization localization configuration subsystem")
        self.assertEqual(slug, "internationalization-localization")
        self.assertLessEqual(len(slug), 40)

    def test_exactly_forty_chars_is_kept(self) -> None:
        words = ["a" * 9 + c for c in "bcd"] + ["e" * 8]  # 10+1+10+1+10+1+8 = 41 > 40
        self.assertEqual(slugify(" ".join(words)), "-".join(words[:3]))
        words = ["x" * 9, "y" * 9, "z" * 9, "w" * 10]  # 9+1+9+1+9+1+10 = 40
        self.assertEqual(slugify(" ".join(words)), "-".join(words))

    def test_single_overlong_word_is_cut(self) -> None:
        self.assertEqual(slugify("x" * 55 + " tail"), "x" * 40)

    def test_fallback_is_job(self) -> None:
        for feature in ("", "   ", "!!!", "the of and", "日本語の機能"):
            with self.subTest(feature=feature):
                self.assertEqual(slugify(feature), "job")

    def test_accents_are_folded_to_ascii(self) -> None:
        self.assertEqual(slugify("Café menu für Größe"), "cafe-menu-fur-groe")

    def test_slug_always_forms_a_valid_job_id(self) -> None:
        samples = ["Rate limit the signup form", "x" * 100, "---", "a-b-c", "Ünïcödé ✓ tests 123",
                   "1 2 3 4 5 6", "Upgrade to Python 3.12 & drop 3.8", "١٢٣ digits"]
        for feature in samples:
            with self.subTest(feature=feature):
                slug = slugify(feature)
                self.assertTrue(JOB_ID_RE.fullmatch(f"001-{slug}"), slug)
                self.assertLessEqual(len(slug), 40)


class JobIdRegexTest(unittest.TestCase):
    def test_valid_ids(self) -> None:
        for job_id in ("003-rate-limit-signup", "001-job", "999-a1-b2", "000-x"):
            with self.subTest(job_id=job_id):
                self.assertTrue(JOB_ID_RE.fullmatch(job_id))

    def test_invalid_ids(self) -> None:
        for job_id in ("03-x", "0003-x", "003-", "003", "003-Rate", "003-a--b", "003-a-",
                       "003--a", "../003-x", "003-x/y", "003_x", "١٢٣-x"):
            with self.subTest(job_id=job_id):
                self.assertIsNone(JOB_ID_RE.fullmatch(job_id))


class ReadVerdictTest(TempDirTestCase):
    def verdict(self, content: str | bytes, kind: str) -> str | None:
        path = self.tmp / "verdict.md"
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return read_verdict(path, kind)

    def test_valid_tokens_per_kind(self) -> None:
        cases = [
            ("review", "VERDICT: PASS", "PASS"), ("review", "VERDICT: CHANGES", "CHANGES"),
            ("build", "STATUS: READY", "READY"), ("build", "STATUS: BLOCKED", "BLOCKED"),
            ("decision", "APPROVE", "APPROVE"), ("decision", "ESCALATE", "ESCALATE"),
        ]
        for kind, line, token in cases:
            with self.subTest(kind=kind, line=line):
                self.assertEqual(self.verdict(f"{line}\n\n## Notes\nbody\n", kind), token)

    def test_normalises_whitespace_bom_and_markdown(self) -> None:
        cases = [
            ("**VERDICT: PASS**", "review", "PASS"),
            ("# VERDICT: CHANGES", "review", "CHANGES"),
            ("### **STATUS: READY**", "build", "READY"),
            ("`APPROVE`", "decision", "APPROVE"),
            ("_ESCALATE_", "decision", "ESCALATE"),
            ("__**`STATUS: BLOCKED`**__", "build", "BLOCKED"),
            ("   \tAPPROVE  \t", "decision", "APPROVE"),
            ("﻿VERDICT: PASS", "review", "PASS"),
            ("﻿# **APPROVE**", "decision", "APPROVE"),
            ("APPROVE\r\nmore\r\n", "decision", "APPROVE"),
        ]
        for line, kind, token in cases:
            with self.subTest(line=line):
                self.assertEqual(self.verdict(line, kind), token)

    def test_skips_leading_blank_lines(self) -> None:
        self.assertEqual(self.verdict("\n   \n\t\n﻿\nVERDICT: CHANGES\n", "review"), "CHANGES")

    def test_only_the_first_non_empty_line_counts(self) -> None:
        self.assertIsNone(self.verdict("# Review\nVERDICT: PASS\n", "review"))
        self.assertIsNone(self.verdict("```\nVERDICT: PASS\n```\n", "review"))
        self.assertEqual(self.verdict("VERDICT: CHANGES\nVERDICT: PASS\n", "review"), "CHANGES")

    def test_case_sensitive_exact_match(self) -> None:
        for line in ("verdict: pass", "Verdict: PASS", "VERDICT: Pass", "approve", "Approve"):
            with self.subTest(line=line):
                kind = "decision" if "pprove" in line.lower() else "review"
                self.assertIsNone(self.verdict(line, kind))

    def test_malformed_lines(self) -> None:
        for line in ("VERDICT: PASS.", "VERDICT:PASS", "VERDICT: MAYBE", "VERDICT: PASS extra",
                     "VERDICT:  PASS", "- VERDICT: PASS", "> APPROVE", "APPROVED", "#"):
            with self.subTest(line=line):
                kind = "decision" if "APPROVE" in line or line == "#" else "review"
                self.assertIsNone(self.verdict(line, kind))

    def test_token_of_another_kind_is_rejected(self) -> None:
        self.assertIsNone(self.verdict("APPROVE", "review"))
        self.assertIsNone(self.verdict("VERDICT: PASS", "decision"))
        self.assertIsNone(self.verdict("VERDICT: PASS", "build"))
        self.assertIsNone(self.verdict("STATUS: READY", "review"))

    def test_missing_empty_and_unreadable(self) -> None:
        self.assertIsNone(read_verdict(self.tmp / "nope.md", "review"))
        self.assertIsNone(self.verdict("", "review"))
        self.assertIsNone(self.verdict("  \n\n\t\n", "decision"))
        self.assertIsNone(read_verdict(self.tmp, "review"))
        self.assertIsNone(self.verdict(b"\xff\xfe\x00garbage\x00\n", "build"))

    def test_fifo_and_devices_are_none_without_blocking(self) -> None:
        fifo = self.tmp / "build.md"
        os.mkfifo(fifo)
        self.assertIsNone(self.without_blocking(lambda: read_verdict(fifo, "build"), fifo))
        link = self.tmp / "link.md"
        link.symlink_to(fifo)
        self.assertIsNone(self.without_blocking(lambda: read_verdict(link, "build"), fifo))
        for device in ("/dev/null", "/dev/zero"):
            if os.path.exists(device):
                with self.subTest(device=device):
                    self.assertIsNone(read_verdict(device, "build"))

    def test_symlink_to_regular_file_is_followed(self) -> None:
        target = self.tmp / "real.md"
        target.write_text("STATUS: READY\n")
        (self.tmp / "build.md").symlink_to(target)
        self.assertEqual(read_verdict(self.tmp / "build.md", "build"), "READY")

    def test_overlong_first_line_is_malformed(self) -> None:
        limit = jobs.VERDICT_LINE_LIMIT
        self.assertEqual(self.verdict("STATUS: READY".ljust(limit - 1) + "\n", "build"), "READY")
        self.assertEqual(self.verdict("STATUS: READY".ljust(limit - 1), "build"), "READY")
        self.assertIsNone(self.verdict("STATUS: READY".ljust(limit) + "\n", "build"))
        self.assertIsNone(self.verdict(" " * limit + "STATUS: READY\n", "build"))
        self.assertIsNone(self.verdict("x" * (3 * limit), "build"))

    def test_long_blank_lines_are_still_skipped(self) -> None:
        limit = jobs.VERDICT_LINE_LIMIT
        self.assertEqual(self.verdict(" " * (3 * limit) + "\n\nSTATUS: READY\n", "build"), "READY")

    def test_gives_up_after_scan_limit(self) -> None:
        scan = jobs.VERDICT_SCAN_LIMIT
        self.assertEqual(self.verdict("\n" * (scan - 1) + "APPROVE\n", "decision"), "APPROVE")
        self.assertIsNone(self.verdict("\n" * scan + "APPROVE\n", "decision"))

    def test_huge_single_line_is_read_in_bounded_memory(self) -> None:
        path = self.tmp / "build.md"
        chunk = "x" * (1 << 20)
        with open(path, "w", encoding="utf-8") as fh:
            for _ in range(32):
                fh.write(chunk)
        tracemalloc.start()
        try:
            self.assertIsNone(read_verdict(path, "build"))
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 4 << 20)

    def test_accepts_str_path(self) -> None:
        path = self.tmp / "decision.md"
        path.write_text("APPROVE\n")
        self.assertEqual(read_verdict(str(path), "decision"), "APPROVE")

    def test_unknown_kind_raises(self) -> None:
        with self.assertRaises(ValueError):
            read_verdict(self.tmp / "x.md", "spec")


class PathsTest(TempDirTestCase):
    def test_layout(self) -> None:
        self.assertEqual(self.store.repo_dir("my-app"), self.data_dir / "jobs" / "my-app")
        self.assertEqual(self.store.job_dir("my-app", "003-x"), self.data_dir / "jobs" / "my-app" / "003-x")
        self.assertEqual(self.store.worktree_path("my-app", "003-x"),
                         self.data_dir / "worktrees" / "my-app" / "003-x")

    def test_data_dir_expands_user_and_is_absolute(self) -> None:
        with mock.patch.dict(os.environ, {"HOME": str(self.tmp)}):
            self.assertEqual(JobStore("~/sf").data_dir, self.tmp / "sf")
        self.assertTrue(JobStore("relative/dir").data_dir.is_absolute())

    def test_invalid_repo_names(self) -> None:
        for repo in ("", "My-App", "../x", "a/b", ".hidden", "-x", "_x", "x\n", "a b", None):
            with self.subTest(repo=repo):
                with self.assertRaises(ValueError):
                    self.store.repo_dir(repo)
                with self.assertRaises(ValueError):
                    self.store.job_dir(repo, "001-x")

    def test_invalid_job_ids(self) -> None:
        for job_id in ("..", "001", "001-x\n", "001-../../etc", "1-x", "001-X", None):
            with self.subTest(job_id=job_id):
                with self.assertRaises(ValueError):
                    self.store.job_dir("my-app", job_id)
                with self.assertRaises(ValueError):
                    self.store.worktree_path("my-app", job_id)

    def test_valid_repo_names(self) -> None:
        for repo in ("a", "my-app", "app.v2", "x_y", "0day"):
            with self.subTest(repo=repo):
                self.assertEqual(self.store.repo_dir(repo).name, repo)


class AllocateTest(TempDirTestCase):
    def test_first_job_is_001(self) -> None:
        job_id, path = self.store.allocate("my-app", "Rate limit the signup form")
        self.assertEqual(job_id, "001-rate-limit-signup-form")
        self.assertEqual(path, self.store.job_dir("my-app", job_id))
        self.assertTrue(path.is_dir())
        self.assertEqual(list(path.iterdir()), [])

    def test_next_after_highest_existing_folder(self) -> None:
        root = self.store.repo_dir("my-app")
        (root / "002-old").mkdir(parents=True)
        (root / "007-older").mkdir()
        job_id, _ = self.store.allocate("my-app", "new thing")
        self.assertEqual(job_id, "008-new-thing")

    def test_ignores_non_job_entries(self) -> None:
        root = self.store.repo_dir("my-app")
        root.mkdir(parents=True)
        (root / "backlog.md").write_text("# Backlog\n")
        (root / "050-a-file").write_text("not a folder")
        for name in ("abc", "1234-x", "12-x", "AGENTS.draft"):
            (root / name).mkdir()
        job_id, _ = self.store.allocate("my-app", "thing")
        self.assertEqual(job_id, "001-thing")

    def test_retries_next_number_when_claimed(self) -> None:
        root = self.store.repo_dir("my-app")
        (root / "001-add-login").mkdir(parents=True)
        (root / "002-add-login").mkdir()
        with mock.patch.object(JobStore, "_highest_job_number", staticmethod(lambda root: 0)):
            job_id, _ = self.store.allocate("my-app", "Add login")
        self.assertEqual(job_id, "003-add-login")

    def test_exhausted_numbers_raise(self) -> None:
        (self.store.repo_dir("my-app") / "999-last").mkdir(parents=True)
        with self.assertRaises(RuntimeError):
            self.store.allocate("my-app", "one more")

    def test_numbers_are_per_repo(self) -> None:
        self.assertEqual(self.store.allocate("a", "x")[0], "001-x")
        self.assertEqual(self.store.allocate("a", "x")[0], "002-x")
        self.assertEqual(self.store.allocate("b", "x")[0], "001-x")

    def test_invalid_repo_creates_nothing(self) -> None:
        with self.assertRaises(ValueError):
            self.store.allocate("../evil", "x")
        self.assertFalse(self.data_dir.exists())

    def test_concurrent_allocation_is_unique(self) -> None:
        other = JobStore(self.data_dir)
        ids: list[str] = []
        lock = threading.Lock()

        def worker(n: int) -> None:
            store = self.store if n % 2 else other
            job_id, _ = store.allocate("my-app", "same feature")
            with lock:
                ids.append(job_id)

        run_threads(worker, 24)
        self.assertEqual(sorted(ids), [f"{n:03d}-same-feature" for n in range(1, 25)])

    def test_waits_for_the_cross_process_lock(self) -> None:
        root = self.store.repo_dir("my-app")
        root.mkdir(parents=True)
        fd = os.open(root / jobs.ALLOCATE_LOCK_FILE, os.O_RDWR | os.O_CREAT)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)  # what another process's allocate() holds
        ids: list[str] = []
        thread = threading.Thread(target=lambda: ids.append(self.store.allocate("my-app", "x")[0]))
        thread.start()
        thread.join(0.3)
        self.assertTrue(thread.is_alive())
        self.assertEqual(ids, [])
        fcntl.flock(fd, fcntl.LOCK_UN)
        thread.join(10)
        self.assertEqual(ids, ["001-x"])

    def test_numbers_are_unique_across_processes(self) -> None:
        go = self.tmp / "go"
        code = (
            "import os, sys, time\n"
            "from swfactory.jobs import JobStore\n"
            "store = JobStore(sys.argv[1])\n"
            "while not os.path.exists(sys.argv[3]):\n"
            "    time.sleep(0.005)\n"
            "print(' '.join(store.allocate('my-app', sys.argv[2])[0] for _ in range(40)))\n"
        )
        procs = [subprocess.Popen([sys.executable, "-c", code, str(self.data_dir), slug, str(go)],
                                  cwd=REPO_ROOT, stdout=subprocess.PIPE, text=True)
                 for slug in ("alpha", "beta", "gamma")]
        go.touch()
        ids = []
        for proc in procs:
            out, _ = proc.communicate(timeout=60)
            self.assertEqual(proc.returncode, 0)
            ids += out.split()
        self.assertEqual(len(ids), 120)
        self.assertEqual(sorted(int(job_id[:3]) for job_id in ids), list(range(1, 121)))


class NewJobTest(TempDirTestCase):
    def test_initialises_every_key(self) -> None:
        job = self.new_job(base="main")
        self.assertTrue(SECTION_4_KEYS <= set(job), SECTION_4_KEYS - set(job))
        job_id = "001-rate-limit-signup-form"
        expected = {
            "id": job_id, "repo": "my-app", "feature": "Rate limit the signup form", "base": "main",
            "base_commit": None, "branch": f"factory/{job_id}",
            "worktree": str(self.data_dir / "worktrees" / "my-app" / job_id),
            "job_dir": str(self.data_dir / "jobs" / "my-app" / job_id),
            "port": 5110, "stage": "queued", "detail": "", "round": 0, "resumes": 0,
            "max_resumes": 2, "reviewers": REVIEWERS,
            "reviews": {"security": "pending", "ux": "pending", "ui": "pending", "code": "pending"},
            "history": [], "decision": None, "reason": None, "agents": {},
            "usage": {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "calls": 0},
            "merged_commit": None, "error": None, "failed_step": None, "from_branch": None,
            "items": {"v": 0, "total": 0, "open": 0, "blocking_open": 0, "proposed": 0, "done": 0, "stale": 0,
                      "verified": 0},
        }
        self.assertEqual({k: v for k, v in job.items() if k not in ("created_at", "updated_at")}, expected)
        self.assertRegex(job["created_at"], TS_RE)
        self.assertRegex(job["updated_at"], TS_RE)

    def test_writes_job_json_and_feature_md(self) -> None:
        feature = "Ünïcode feature\r\nwith CRLF, trailing spaces   \n\n  and no final newline"
        job = self.new_job(feature)
        job_dir = Path(job["job_dir"])
        self.assertEqual(json.loads((job_dir / "job.json").read_text(encoding="utf-8")), job)
        self.assertEqual((job_dir / "feature.md").read_bytes(), feature.encode("utf-8"))
        self.assertEqual(self.temp_files(job_dir), [])

    def test_port_follows_job_number(self) -> None:
        ports = [self.new_job(f"feature {n}")["port"] for n in range(3)]
        self.assertEqual(ports, [5110, 5120, 5130])

    def test_base_defaults_to_none(self) -> None:
        self.assertIsNone(self.new_job()["base"])

    def test_reviewers_are_copied(self) -> None:
        reviewers = ["ux"]
        job = self.new_job(reviewers=reviewers, max_resumes=0)
        reviewers.append("code")
        self.assertEqual(job["reviewers"], ["ux"])
        self.assertEqual(job["reviews"], {"ux": "pending"})
        self.assertEqual(job["max_resumes"], 0)

    def test_no_reviewers(self) -> None:
        job = self.new_job(reviewers=[])
        self.assertEqual((job["reviewers"], job["reviews"]), ([], {}))

    def test_empty_feature_rejected_before_allocating(self) -> None:
        for feature in ("", "  \n\t"):
            with self.subTest(feature=feature):
                with self.assertRaises(ValueError):
                    self.new_job(feature)
        self.assertFalse(self.store.repo_dir("my-app").exists())


class LoadSaveTest(TempDirTestCase):
    def test_round_trip(self) -> None:
        job = self.new_job()
        self.assertEqual(self.store.load("my-app", job["id"]), job)

    def test_load_missing_raises_key_error(self) -> None:
        job = self.new_job()
        for repo, job_id in (("my-app", "002-nope"), ("other", job["id"]), ("my-app", "bad id"),
                             ("../x", job["id"])):
            with self.subTest(repo=repo, job_id=job_id):
                with self.assertRaises(KeyError):
                    self.store.load(repo, job_id)

    def test_load_folder_without_job_json_raises_key_error(self) -> None:
        job_id, _ = self.store.allocate("my-app", "half made")
        with self.assertRaises(KeyError):
            self.store.load("my-app", job_id)

    def test_load_malformed_raises_value_error(self) -> None:
        job = self.new_job()
        path = Path(job["job_dir"]) / "job.json"
        for text in ("{not json", "[1, 2]", '"string"', ""):
            with self.subTest(text=text):
                path.write_text(text)
                with self.assertRaises(ValueError):
                    self.store.load("my-app", job["id"])
        path.write_bytes(b'{"id": "\xff"}')
        with self.assertRaises(ValueError):
            self.store.load("my-app", job["id"])

    def test_load_non_regular_job_json_raises_key_error_without_blocking(self) -> None:
        job = self.new_job()
        path = Path(job["job_dir"]) / "job.json"
        path.unlink()
        os.mkfifo(path)
        with self.assertRaises(KeyError):
            self.without_blocking(lambda: self.store.load("my-app", job["id"]), path)
        self.assertEqual(self.without_blocking(self.store.list, path), [])
        path.unlink()
        path.mkdir()
        with self.assertRaises(KeyError):
            self.store.load("my-app", job["id"])

    def test_save_sets_updated_at_and_returns_job(self) -> None:
        job = self.new_job()
        job["stage"] = "build"
        with mock.patch.object(jobs, "utc_now", return_value="2030-01-02T03:04:05Z"):
            result = self.store.save(job)
        self.assertIs(result, job)
        self.assertEqual(job["updated_at"], "2030-01-02T03:04:05Z")
        loaded = self.store.load("my-app", job["id"])
        self.assertEqual((loaded["stage"], loaded["updated_at"]), ("build", "2030-01-02T03:04:05Z"))
        self.assertNotEqual(loaded["created_at"], loaded["updated_at"])

    def test_save_writes_whole_document(self) -> None:
        job = self.new_job()
        del job["detail"]
        job["extra"] = {"nested": [1, "ü"]}
        self.store.save(job)
        loaded = self.store.load("my-app", job["id"])
        self.assertNotIn("detail", loaded)
        self.assertEqual(loaded["extra"], {"nested": [1, "ü"]})

    def test_failed_replace_keeps_old_file_and_cleans_temp(self) -> None:
        job = self.new_job()
        job_dir = Path(job["job_dir"])
        before = (job_dir / "job.json").read_text()
        job["stage"] = "build"
        with mock.patch.object(jobs.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.store.save(job)
        self.assertEqual((job_dir / "job.json").read_text(), before)
        self.assertEqual(self.temp_files(job_dir), [])

    def test_unserialisable_job_leaves_file_intact(self) -> None:
        job = self.new_job()
        job_dir = Path(job["job_dir"])
        before = (job_dir / "job.json").read_text()
        job["bad"] = object()
        with self.assertRaises(TypeError):
            self.store.save(job)
        self.assertEqual((job_dir / "job.json").read_text(), before)
        self.assertEqual(self.temp_files(job_dir), [])

    def test_save_requires_valid_identity(self) -> None:
        job = self.new_job()
        with self.assertRaises(ValueError):
            self.store.save({**job, "id": "../escape"})
        with self.assertRaises(KeyError):
            self.store.save({"repo": "my-app"})

    def test_concurrent_saves_leave_valid_json(self) -> None:
        job = self.new_job()

        def worker(n: int) -> None:
            for i in range(20):
                self.store.save({**job, "detail": f"{n}-{i}"})

        run_threads(worker, 8)
        loaded = self.store.load("my-app", job["id"])
        self.assertRegex(loaded["detail"], r"^\d-\d+$")
        self.assertEqual(self.temp_files(Path(job["job_dir"])), [])


class ListTest(TempDirTestCase):
    def test_empty(self) -> None:
        self.assertEqual(self.store.list(), [])
        self.assertEqual(self.store.list("my-app"), [])

    def test_sorted_by_repo_then_id(self) -> None:
        self.new_job("first b", repo="b")
        self.new_job("first x", repo="a")
        self.new_job("second b", repo="b")
        self.new_job("second x", repo="a")
        listed = [(j["repo"], j["id"]) for j in self.store.list()]
        self.assertEqual(listed, [("a", "001-first-x"), ("a", "002-second-x"),
                                  ("b", "001-first-b"), ("b", "002-second-b")])
        self.assertEqual([j["id"] for j in self.store.list("b")], ["001-first-b", "002-second-b"])

    def test_numeric_order_across_many_jobs(self) -> None:
        for n in range(11):
            self.new_job(f"job {n}")
        self.assertEqual([j["id"][:3] for j in self.store.list()], [f"{n:03d}" for n in range(1, 12)])

    def test_malformed_job_json_becomes_failed_placeholder(self) -> None:
        good = self.new_job("good one")
        bad = self.new_job("bad one")
        (Path(bad["job_dir"]) / "job.json").write_text("{broken")
        listed = self.store.list("my-app")
        self.assertEqual(listed[0], good)
        self.assertEqual(listed[1], {"id": bad["id"], "repo": "my-app", "stage": "failed",
                                     "reason": "unreadable job.json"})

    def test_skips_incomplete_and_foreign_entries(self) -> None:
        job = self.new_job()
        self.store.allocate("my-app", "allocated only")
        root = self.store.repo_dir("my-app")
        (root / "backlog.md").write_text("# Backlog\n")
        (root / "notes").mkdir()
        (root / "notes" / "job.json").write_text("{}")
        (self.data_dir / "jobs" / "Not A Repo").mkdir()
        (self.data_dir / "jobs" / "stray.txt").write_text("x")
        self.assertEqual(self.store.list(), [job])

    def test_unknown_and_invalid_repo(self) -> None:
        self.new_job()
        self.assertEqual(self.store.list("other"), [])
        with self.assertRaises(ValueError):
            self.store.list("../my-app")

    def test_job_removed_while_listing_is_skipped(self) -> None:
        job = self.new_job()
        with mock.patch.object(JobStore, "load", side_effect=KeyError(job["id"])):
            self.assertEqual(self.store.list(), [])


class JobFilesTest(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.job = self.new_job()
        self.job_dir = Path(self.job["job_dir"])
        self.outside = self.tmp / "outside.txt"
        self.outside.write_text("outside secret")

    def write(self, rel: str, text: str = "x") -> Path:
        path = self.job_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_files_sorted_posix_excluding_sessions(self) -> None:
        for rel in ("spec.md", "round-1/review-ux.md", "round-10/review-ui.md", "logs/01-builder.jsonl",
                    "sessions/builder.json", "sessions/nested/x.json", "sessions.md",
                    "round-1/sessions/kept.md"):
            self.write(rel)
        self.assertEqual(self.store.files("my-app", self.job["id"]), [
            "feature.md", "job.json", "logs/01-builder.jsonl", "round-1/review-ux.md",
            "round-1/sessions/kept.md", "round-10/review-ui.md", "sessions.md", "spec.md",
        ])

    def test_files_skip_temp_files_and_symlinks(self) -> None:
        self.write(".sf-job.json.abc.tmp")
        self.write(".hidden.md")
        outside_dir = self.tmp / "outside-dir"
        outside_dir.mkdir()
        (outside_dir / "leak.txt").write_text("x")
        (self.job_dir / "link.txt").symlink_to(self.outside)
        (self.job_dir / "linkdir").symlink_to(outside_dir, target_is_directory=True)
        self.assertEqual(self.store.files("my-app", self.job["id"]),
                         [".hidden.md", "feature.md", "job.json"])

    def test_files_skip_fifos(self) -> None:
        os.mkfifo(self.job_dir / "build.md")
        os.mkfifo(self.job_dir / "round-1.md")
        self.write("round-1/review-ux.md")
        os.mkfifo(self.job_dir / "round-1" / "review-ui.md")
        files = self.without_blocking(lambda: self.store.files("my-app", self.job["id"]),
                                      self.job_dir / "build.md")
        self.assertEqual(files, ["feature.md", "job.json", "round-1/review-ux.md"])

    def test_files_missing_job(self) -> None:
        with self.assertRaises(KeyError):
            self.store.files("my-app", "099-nope")

    def test_read_file(self) -> None:
        self.write("round-1/review-ux.md", "VERDICT: PASS\nü\n")
        self.assertEqual(self.store.read_file("my-app", self.job["id"], "round-1/review-ux.md"),
                         "VERDICT: PASS\nü\n")
        self.assertEqual(self.store.read_file("my-app", self.job["id"], "./feature.md"),
                         self.job["feature"])

    def test_read_file_replaces_undecodable_bytes(self) -> None:
        (self.job_dir / "bin.dat").write_bytes(b"ok\xff")
        self.assertEqual(self.store.read_file("my-app", self.job["id"], "bin.dat"), "ok�")

    def test_read_file_rejects_escapes(self) -> None:
        (self.job_dir / "link.txt").symlink_to(self.outside)
        (self.job_dir / "linkdir").symlink_to(self.tmp, target_is_directory=True)
        bad = [str(self.outside), "/etc/passwd", "../outside.txt", "round-1/../../outside.txt",
               "a/../feature.md", "..", "", ".", "link.txt", "linkdir/outside.txt", "x\0y"]
        for rel in bad:
            with self.subTest(rel=rel):
                with self.assertRaises(ValueError):
                    self.store.read_file("my-app", self.job["id"], rel)

    def test_read_file_allows_internal_symlink(self) -> None:
        self.write("spec.md", "the spec")
        (self.job_dir / "alias.md").symlink_to(self.job_dir / "spec.md")
        self.assertEqual(self.store.read_file("my-app", self.job["id"], "alias.md"), "the spec")

    def test_read_file_rejects_non_regular_files_without_blocking(self) -> None:
        fifo = self.job_dir / "build.md"
        os.mkfifo(fifo)
        (self.job_dir / "alias.md").symlink_to(fifo)
        self.write("logs/01-builder.jsonl")
        for rel in ("build.md", "alias.md", "logs", "logs/"):
            with self.subTest(rel=rel):
                with self.assertRaises(FileNotFoundError):
                    self.without_blocking(
                        lambda: self.store.read_file("my-app", self.job["id"], rel), fifo)

    def test_read_file_symlink_loop_is_a_client_error(self) -> None:
        (self.job_dir / "a").symlink_to("b")
        (self.job_dir / "b").symlink_to("a")
        for rel in ("a", "a/x"):
            with self.subTest(rel=rel):
                with self.assertRaises((FileNotFoundError, ValueError)):
                    self.store.read_file("my-app", self.job["id"], rel)

    def test_read_file_missing(self) -> None:
        with self.assertRaises(FileNotFoundError):
            self.store.read_file("my-app", self.job["id"], "spec.md")
        with self.assertRaises(KeyError):
            self.store.read_file("my-app", "099-nope", "spec.md")

    def test_write_file_creates_parents_atomically(self) -> None:
        path = self.store.write_file(self.job, "round-2/review-code.md", "VERDICT: CHANGES\r\n")
        self.assertEqual(path, (self.job_dir / "round-2" / "review-code.md").resolve())
        self.assertEqual(path.read_bytes(), b"VERDICT: CHANGES\r\n")
        self.store.write_file(self.job, "round-2/review-code.md", "VERDICT: PASS\n")
        self.assertEqual(path.read_text(), "VERDICT: PASS\n")
        self.assertEqual(self.temp_files(self.job_dir), [])

    def test_write_file_rejects_escapes(self) -> None:
        (self.job_dir / "linkdir").symlink_to(self.tmp, target_is_directory=True)
        for rel in ("../evil.md", "/tmp/evil.md", "a/../../evil.md", "linkdir/evil.md", ""):
            with self.subTest(rel=rel):
                with self.assertRaises(ValueError):
                    self.store.write_file(self.job, rel, "x")
        self.assertFalse((self.tmp / "evil.md").exists())
        self.assertFalse((self.data_dir / "jobs" / "my-app" / "evil.md").exists())

    def test_write_file_missing_job(self) -> None:
        with self.assertRaises(KeyError):
            self.store.write_file({**self.job, "id": "099-nope"}, "x.md", "x")


class ReworkAndLogsTest(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.job = self.new_job()
        self.job_dir = Path(self.job["job_dir"])

    def test_next_rework_index(self) -> None:
        self.assertEqual(self.store.next_rework_index(self.job), 1)
        self.store.write_file(self.job, "rework-1.md", "note")
        self.assertEqual(self.store.next_rework_index(self.job), 2)
        for name in ("rework-3.md", "rework-x.md", "rework-9.txt", "rework-12.md.bak"):
            (self.job_dir / name).write_text("n")
        self.assertEqual(self.store.next_rework_index(self.job), 4)

    def test_next_log_path_numbers_runs_in_order(self) -> None:
        first = self.store.next_log_path(self.job, "builder")
        second = self.store.next_log_path(self.job, "reviewer-ux")
        third = self.store.next_log_path(self.job, "builder")
        self.assertEqual(first, self.job_dir / "logs" / "01-builder.jsonl")
        self.assertEqual(second, self.job_dir / "logs" / "02-reviewer-ux.jsonl")
        self.assertEqual(third, self.job_dir / "logs" / "03-builder.jsonl")
        self.assertTrue(first.is_file())
        self.assertEqual(first.read_text(), "")

    def test_next_log_path_ignores_other_logs_and_grows_past_99(self) -> None:
        logs = self.job_dir / "logs"
        logs.mkdir()
        (logs / "events.jsonl").write_text("{}\n")
        (logs / "09-spec-writer.jsonl").write_text("")
        self.assertEqual(self.store.next_log_path(self.job, "builder").name, "10-builder.jsonl")
        (logs / "99-builder.jsonl").write_text("")
        self.assertEqual(self.store.next_log_path(self.job, "approver").name, "100-approver.jsonl")

    def test_next_log_path_concurrent_is_unique(self) -> None:
        paths: list[Path] = []
        lock = threading.Lock()

        def worker(n: int) -> None:
            path = self.store.next_log_path(self.job, f"reviewer-{n}")
            with lock:
                paths.append(path)

        run_threads(worker, 30)
        numbers = sorted(int(p.name.split("-", 1)[0]) for p in paths)
        self.assertEqual(numbers, list(range(1, 31)))

    def test_next_log_path_rejects_bad_agent_names(self) -> None:
        for agent in ("", "../x", "a/b", "-x", "x\n", None):
            with self.subTest(agent=agent):
                with self.assertRaises(ValueError):
                    self.store.next_log_path(self.job, agent)
        self.assertFalse((self.job_dir / "logs").exists())

    def test_next_log_path_missing_job(self) -> None:
        with self.assertRaises(KeyError):
            self.store.next_log_path({**self.job, "id": "099-nope"}, "builder")


class BacklogTest(TempDirTestCase):
    def path(self, repo: str = "my-app") -> Path:
        return self.store.repo_dir(repo) / "backlog.md"

    def write_backlog(self, text: str, repo: str = "my-app") -> None:
        self.path(repo).parent.mkdir(parents=True, exist_ok=True)
        self.path(repo).write_text(text, encoding="utf-8")

    def test_no_file_is_empty(self) -> None:
        self.assertEqual(self.store.backlog("my-app"), [])

    def test_parse_lines(self) -> None:
        self.write_backlog(
            "# Backlog\n\nSome intro text\n"
            "- [ ] Rate limit signup\n"
            "- [x] Dark mode → 001-dark-mode\n"
            "- [x] Export CSV -> 002-export-csv\n"
            "- [x] Done by hand\n"
            "- [X] Upper X → 003-upper\n"
            "* [ ] Star bullet\n"
            "  - [ ]   padded item   \n"
            "- [x] Taken → claimed\n"
            "- [x] Arrow in text → not a job id\n"
            "- [x] a → b → 004-two-arrows\n"
            "- [ ]\n- [ ] \n-[ ] no space\n- [y] bad box\n"
        )
        self.assertEqual(self.store.backlog("my-app"), [
            {"text": "Rate limit signup", "done": False, "job": None},
            {"text": "Dark mode", "done": True, "job": "001-dark-mode"},
            {"text": "Export CSV", "done": True, "job": "002-export-csv"},
            {"text": "Done by hand", "done": True, "job": None},
            {"text": "Upper X", "done": True, "job": "003-upper"},
            {"text": "Star bullet", "done": False, "job": None},
            {"text": "padded item", "done": False, "job": None},
            {"text": "Taken", "done": True, "job": "claimed"},
            {"text": "Arrow in text → not a job id", "done": True, "job": None},
            {"text": "a → b", "done": True, "job": "004-two-arrows"},
        ])

    def test_unchecked_arrow_is_part_of_text(self) -> None:
        self.write_backlog("- [ ] Map a → b 001-x\n- [ ] Go → 002-y\n")
        self.assertEqual([(i["text"], i["job"]) for i in self.store.backlog("my-app")],
                         [("Map a → b 001-x", None), ("Go → 002-y", None)])

    def test_add_creates_file_with_header(self) -> None:
        items = self.store.add_backlog("my-app", "  First item  ")
        self.assertEqual(items, [{"text": "First item", "done": False, "job": None}])
        self.assertEqual(self.path().read_text(encoding="utf-8"), "# Backlog\n\n- [ ] First item\n")
        items = self.store.add_backlog("my-app", "Second ü item\n")
        self.assertEqual([i["text"] for i in items], ["First item", "Second ü item"])
        self.assertEqual(self.store.backlog("my-app"), items)

    def test_add_appends_newline_to_unterminated_file(self) -> None:
        self.write_backlog("# My list\n- [ ] existing")
        self.store.add_backlog("my-app", "new")
        self.assertEqual(self.path().read_text(), "# My list\n- [ ] existing\n- [ ] new\n")

    def test_add_writes_header_for_empty_file(self) -> None:
        self.write_backlog("")
        self.store.add_backlog("my-app", "item")
        self.assertEqual(self.path().read_text(), "# Backlog\n\n- [ ] item\n")

    def test_add_rejects_empty_and_multiline(self) -> None:
        for text in ("", "   ", "\n", "a\nb", "a\rb", "a\r\nb", "a b", "a\x0bb", None, 42):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    self.store.add_backlog("my-app", text)
        self.assertFalse(self.path().exists())

    def test_add_invalid_repo(self) -> None:
        with self.assertRaises(ValueError):
            self.store.add_backlog("../x", "item")

    def test_claim_next_marks_first_unchecked(self) -> None:
        self.write_backlog("# Backlog\n\n- [x] old → 001-old\n- [ ] first\n- [ ] second\n")
        self.assertEqual(self.store.claim_next("my-app"), "first")
        self.assertEqual(self.path().read_text(),
                         "# Backlog\n\n- [x] old → 001-old\n- [x] first → claimed\n- [ ] second\n")
        self.assertEqual(self.store.claim_next("my-app"), "second")
        self.assertIsNone(self.store.claim_next("my-app"))

    def test_claim_next_without_backlog(self) -> None:
        self.assertIsNone(self.store.claim_next("my-app"))
        self.assertFalse(self.path().exists())

    def test_claim_next_concurrent_claims_each_item_once(self) -> None:
        for n in range(20):
            self.store.add_backlog("my-app", f"item {n}")
        claimed: list[str | None] = []
        lock = threading.Lock()

        def worker(n: int) -> None:
            text = self.store.claim_next("my-app")
            with lock:
                claimed.append(text)

        run_threads(worker, 25)
        taken = [t for t in claimed if t is not None]
        self.assertEqual(sorted(taken), sorted(f"item {n}" for n in range(20)))
        self.assertEqual(claimed.count(None), 5)
        self.assertTrue(all(i["job"] == "claimed" for i in self.store.backlog("my-app")))

    def test_assign_backlog_replaces_first_claimed_line(self) -> None:
        self.write_backlog("# Backlog\n- [x] dup → claimed\n- [x] dup → claimed\n- [ ] other\n")
        self.assertTrue(self.store.assign_backlog("my-app", " dup ", "005-dup"))
        self.assertEqual(self.path().read_text(),
                         "# Backlog\n- [x] dup → 005-dup\n- [x] dup → claimed\n- [ ] other\n")
        self.assertTrue(self.store.assign_backlog("my-app", "dup", "006-dup"))
        self.assertFalse(self.store.assign_backlog("my-app", "dup", "007-dup"))
        self.assertFalse(self.store.assign_backlog("my-app", "other", "008-other"))

    def test_assign_backlog_without_file_or_match(self) -> None:
        self.assertFalse(self.store.assign_backlog("my-app", "x", "001-x"))
        self.write_backlog("- [x] x → 001-x\n")
        self.assertFalse(self.store.assign_backlog("my-app", "x", "002-x"))

    def test_assign_backlog_rejects_bad_job_id(self) -> None:
        self.write_backlog("- [x] x → claimed\n")
        for job_id in ("claimed", "bad", "001-x\n", "001-x → y"):
            with self.subTest(job_id=job_id):
                with self.assertRaises(ValueError):
                    self.store.assign_backlog("my-app", "x", job_id)
        self.assertEqual(self.path().read_text(), "- [x] x → claimed\n")

    def test_unclaim_puts_the_first_claimed_line_back(self) -> None:
        self.write_backlog("# Backlog\n- [x] dup → 004-dup\n- [x] dup → claimed\n- [x] dup → claimed\n- [ ] other\n")
        self.assertTrue(self.store.unclaim("my-app", " dup "))
        self.assertEqual(self.path().read_text(),
                         "# Backlog\n- [x] dup → 004-dup\n- [ ] dup\n- [x] dup → claimed\n- [ ] other\n")
        self.assertTrue(self.store.unclaim("my-app", "dup"))
        self.assertFalse(self.store.unclaim("my-app", "dup"), "a line with a job id is never unclaimed")
        self.assertFalse(self.store.unclaim("my-app", "other"), "an unchecked line is not claimed")
        self.assertEqual(self.store.claim_next("my-app"), "dup")

    def test_unclaim_without_file_or_match(self) -> None:
        self.assertFalse(self.store.unclaim("my-app", "x"))
        self.assertFalse(self.path().exists())
        self.write_backlog("- [x] y → claimed\n")
        self.assertFalse(self.store.unclaim("my-app", "x"))
        self.assertEqual(self.path().read_text(), "- [x] y → claimed\n")

    def test_unclaim_races_with_claims_under_the_repo_lock(self) -> None:
        for n in range(20):
            self.store.add_backlog("my-app", f"item {n}")
        results: list[tuple[str | None, bool]] = []
        lock = threading.Lock()

        def worker(_n: int) -> None:
            text = self.store.claim_next("my-app")
            undone = self.store.unclaim("my-app", text) if text is not None else False
            with lock:
                results.append((text, undone))

        run_threads(worker, 20)
        self.assertEqual(len(results), 20)
        self.assertTrue(all(text is not None and undone for text, undone in results), results)
        self.assertTrue(all(not item["done"] for item in self.store.backlog("my-app")), self.store.backlog("my-app"))
        self.assertEqual(len(self.store.backlog("my-app")), 20)

    def test_claim_then_assign_round_trip(self) -> None:
        self.store.add_backlog("my-app", "Rate limit signup")
        text = self.store.claim_next("my-app")
        job = self.new_job(text)
        self.assertTrue(self.store.assign_backlog("my-app", text, job["id"]))
        self.assertEqual(self.store.backlog("my-app"),
                         [{"text": "Rate limit signup", "done": True, "job": "001-rate-limit-signup"}])

    def test_backlog_is_per_repo(self) -> None:
        self.store.add_backlog("a", "for a")
        self.assertEqual(self.store.backlog("b"), [])
        self.assertIsNone(self.store.claim_next("b"))
        self.assertEqual(self.store.claim_next("a"), "for a")


if __name__ == "__main__":
    unittest.main()
