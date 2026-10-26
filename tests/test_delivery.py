"""Tests for swfactory.delivery and the gitops helpers it uses (format_patch, tree_entries, blob)."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from swfactory import delivery, gitops
from tests.helpers import commit_file, make_repo, run_git


class CheckDeliveryTest(unittest.TestCase):
    def test_modes_and_default(self) -> None:
        self.assertEqual(delivery.DELIVERY_MODES, ("merge", "branch", "folder"))
        for mode in delivery.DELIVERY_MODES:
            self.assertEqual(delivery.check_delivery(mode), mode)
        self.assertEqual(delivery.check_delivery(None, "folder"), "folder")
        self.assertEqual(delivery.check_delivery("", "branch"), "branch")
        for bad in ("Merge", "zip", 1, ["merge"], {"mode": "merge"}):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "delivery must be one of"):
                delivery.check_delivery(bad)


class RepoTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sf-delivery-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = make_repo(self.tmp, files={"README.md": "# r\n", "old/file.txt": "was a file\n",
                                               "gone.txt": "bye\n"})
        run_git(self.repo, "checkout", "-q", "-b", "factory/001-x")
        commit_file(self.repo, "a[1]*.txt", "glob chars\n", "glob name")
        run_git(self.repo, "rm", "-q", "-r", "old", "gone.txt")
        commit_file(self.repo, "old", "now a file\n", "dir becomes a file")
        run_git(self.repo, "checkout", "-q", "main")


class GitHelpersTest(RepoTestCase):
    def test_branch_files_and_tree_entries(self) -> None:
        files = gitops.branch_files(self.repo, "main", "factory/001-x")
        self.assertEqual(files, ["a[1]*.txt", "gone.txt", "old", "old/file.txt"])
        entries = gitops.tree_entries(self.repo, "factory/001-x", files + ["nope.txt"])
        self.assertEqual(sorted((mode, kind, path) for mode, kind, _, path in entries),
                         [("100644", "blob", "a[1]*.txt"), ("100644", "blob", "old")])
        self.assertEqual(gitops.tree_entries(self.repo, "factory/001-x", ["*.txt"]), [], "pathspecs are literal")

    def test_blob_and_patch(self) -> None:
        oid = gitops.tree_entries(self.repo, "factory/001-x", ["old"])[0][2]
        self.assertEqual(gitops.blob(self.repo, oid), b"now a file\n")
        for bad in ("HEAD", "--output=x", "abc"):
            with self.subTest(bad=bad), self.assertRaises(gitops.GitError):
                gitops.blob(self.repo, bad)
        patch = gitops.format_patch(self.repo, "main", "factory/001-x")
        self.assertTrue(patch.startswith(b"From "))
        self.assertEqual(patch.count(b"\nSubject: [PATCH "), 2)
        with self.assertRaisesRegex(gitops.GitError, "larger than 100 bytes"):
            gitops.format_patch(self.repo, "main", "factory/001-x", max_bytes=100)
        with self.assertRaises(gitops.GitError):
            gitops.format_patch(self.repo, "-main", "factory/001-x")

    def test_branch_exists_and_checkout_worktree(self) -> None:
        self.assertTrue(gitops.branch_exists(self.repo, "factory/001-x"))
        self.assertFalse(gitops.branch_exists(self.repo, "factory/002-y"))
        worktree = self.tmp / "worktrees" / "app" / "001-x"
        gitops.checkout_worktree(self.repo, worktree, "factory/001-x")
        self.assertEqual(run_git(worktree, "symbolic-ref", "--short", "HEAD").strip(), "factory/001-x")
        self.assertTrue((worktree / "old").is_file())
        with self.assertRaises(gitops.GitError):
            gitops.checkout_worktree(self.repo, self.tmp / "again", "factory/001-x")   # checked out already


class ExportTest(RepoTestCase):
    def job(self) -> dict:
        job_dir = self.tmp / "jobs" / "app" / "001-x"
        job_dir.mkdir(parents=True)
        return {"id": "001-x", "repo": "app", "feature": "Do x", "base": "main", "base_commit": "f" * 40,
                "branch": "factory/001-x", "job_dir": str(job_dir), "reviewers": ["code"], "round": 1,
                "resumes": 0, "max_resumes": 2, "history": [{"round": 1, "reviews": {"code": "PASS"}}],
                "decision": "APPROVE"}

    def test_export_writes_patch_files_and_report(self) -> None:
        tip = run_git(self.repo, "rev-parse", "factory/001-x").strip()
        out = self.tmp / "out"
        record = delivery.export_folder(self.job(), self.repo, out, tip)
        folder = out / "app" / "001-x"
        self.assertEqual(record, {"path": str(folder), "files": 2, "skipped": [],
                                  "patch_bytes": (folder / "changes.patch").stat().st_size})
        self.assertEqual((folder / "files" / "a[1]*.txt").read_text(), "glob chars\n")
        self.assertEqual((folder / "files" / "old").read_text(), "now a file\n")
        report = (folder / "REPORT.md").read_text()
        self.assertIn("Deleted on the branch: `gone.txt`, `old/file.txt`.", report)
        self.assertIn("(spec.md is missing)", report)
        self.assertIn("**APPROVE**", report)
        self.assertIn("| 1 | PASS |", report)

    def test_export_refuses_to_replace_a_symlink(self) -> None:
        tip = run_git(self.repo, "rev-parse", "factory/001-x").strip()
        out = self.tmp / "out"
        (out / "app").mkdir(parents=True)
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "keep.txt").write_text("keep\n")
        os.symlink(elsewhere, out / "app" / "001-x")
        with self.assertRaisesRegex(OSError, "not a directory of an earlier export"):
            delivery.export_folder(self.job(), self.repo, out, tip)
        self.assertEqual((elsewhere / "keep.txt").read_text(), "keep\n")
        self.assertEqual([p.name for p in (out / "app").iterdir()], ["001-x"], "the staging folder is removed")

    def test_safe_destination(self) -> None:
        root = self.tmp / "files"
        root.mkdir()
        (root / "a").write_text("file")
        self.assertEqual(delivery._safe_destination(root, "x/y.txt"), root / "x" / "y.txt")
        for bad in ("../x", "/etc/passwd", "x/../y", ".git/config", "x/.GIT/hooks", "a/b", "./x", ""):
            with self.subTest(path=bad):
                self.assertIsNone(delivery._safe_destination(root, bad))

    def test_report_summaries_come_from_the_job_files(self) -> None:
        job = self.job()
        job_dir = Path(job["job_dir"])
        (job_dir / "spec.md").write_text("# Greeting\n\n> Feature: x\n\n## Context\nctx\n\n## Requirements\n"
                                         "1. Say hi.\n\n## Acceptance criteria\n1. It says hi.\n")
        (job_dir / "build.md").write_text("STATUS: READY\n\n# Build: Greeting\n\n## Summary\nSays hi.\n\n"
                                          "### Detail\nmore\n\n## Log\nround 1\n")
        (job_dir / "decision.md").write_text("APPROVE\n\n## Why\nIt works.\n\n## For the human\nnothing\n")
        report = delivery.report_markdown(job, tip="a" * 40, written=["x"], skipped=[("l", "symlink")],
                                          deleted=[], patch_bytes=10)
        self.assertIn("## Spec summary\n\n**Greeting**\n\n1. Say hi.", report)
        self.assertNotIn("It says hi", report)
        self.assertIn("## Build summary\n\n**Build: Greeting**\n\nSays hi.\n\n### Detail\nmore", report)
        self.assertNotIn("round 1", report)
        self.assertIn("**APPROVE**: It works.", report)
        self.assertNotIn("nothing", report)
        self.assertIn("Not in `files/`: `l` (symlink).", report)
        self.assertNotIn("Independence", report, "every judge was independent")

    def test_the_report_says_when_independence_was_reduced(self) -> None:
        """DESIGN §23: a folder delivery is read outside the GUI, so its report carries the badge's record."""
        job = self.job()
        job.update(independence="reduced", shared_models=[
            {"round": 1, "agent": "reviewer-security", "model": "flash-next", "model_id": "qwen-x", "family": "qwen",
             "builder_model": "flash-next", "accepted": True},
            {"round": 2, "agent": "reviewer-code", "model": "sol", "model_id": "openai/sol", "family": "openai",
             "builder_model": "sol"}])
        report = delivery.report_markdown(job, tip="a" * 40, written=[], skipped=[], deleted=[], patch_bytes=1)
        reviews, decision = report.split("## Reviews", 1)[1].split("## Decision", 1)
        self.assertIn("Independence reduced: a judge ran on a model family the builder ran on, with no way out.",
                      reviews)
        self.assertIn("- round 1: reviewer-security on `flash-next` (qwen family), like the builder's `flash-next` "
                      "(accepted by the routing)", reviews)
        self.assertIn("- round 2: reviewer-code on `sol` (openai family), like the builder's `sol`\n", reviews)
        job.update(independence="unknown", shared_models=[])
        report = delivery.report_markdown(job, tip="a" * 40, written=[], skipped=[], deleted=[], patch_bytes=1)
        self.assertIn("Independence unknown: the code it judged was written in a chat", report)


if __name__ == "__main__":
    unittest.main()
