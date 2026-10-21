"""Tests for swfactory.gitops, run against real temporary git repositories."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from swfactory import gitops
from swfactory.gitops import GitError, MergeBusy
from tests.helpers import commit_file, make_repo, run_git

OLD_GIT = (2, 34, 1)


def commit_dated(repo: Path, when: str, files: dict[str, str]) -> str:
    """Commit files with a fixed author and committer date; return the new sha."""
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        run_git(repo, "add", rel)
    env = dict(os.environ, GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", f"commit at {when}"],
                   check=True, capture_output=True, env=env)
    return run_git(repo, "rev-parse", "HEAD").strip()


class GitTestCase(unittest.TestCase):
    """Gives each test a fresh temp dir holding a repo on main with one commit."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sf-gitops-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = make_repo(self.tmp, files={"README.md": "# test\n", "shared.txt": "a\nb\nc\n"})

    def branch_with(self, branch: str, files: dict[str, str], start: str = "main") -> str:
        """Create branch at start with one commit writing files, leaving the checkout as it was."""
        previous = run_git(self.repo, "symbolic-ref", "--short", "HEAD").strip()
        run_git(self.repo, "checkout", "-q", "-b", branch, start)
        for rel, text in files.items():
            sha = commit_file(self.repo, rel, text, message=f"{branch}: {rel}")
        run_git(self.repo, "checkout", "-q", previous)
        return sha

    def worktrees(self) -> list[str]:
        out = run_git(self.repo, "worktree", "list", "--porcelain")
        return [line.split(" ", 1)[1] for line in out.splitlines() if line.startswith("worktree ")]

    def has_merge_head(self, path: Path) -> bool:
        return subprocess.run(["git", "-C", str(path), "rev-parse", "-q", "--verify", "MERGE_HEAD"],
                              capture_output=True, check=False).returncode == 0


class GitCommandTests(GitTestCase):
    def test_returns_stdout(self) -> None:
        self.assertEqual(gitops.git(self.repo, "rev-parse", "--abbrev-ref", "HEAD").strip(), "main")

    def test_accepts_path_arguments(self) -> None:
        out = gitops.git(self.repo, "ls-files", "--", Path("README.md"))
        self.assertEqual(out.strip(), "README.md")

    def test_failure_raises_git_error_with_details(self) -> None:
        with self.assertRaises(GitError) as caught:
            gitops.git(self.repo, "rev-parse", "--verify", "no-such-ref")
        err = caught.exception
        self.assertEqual(err.cmd[:3], ["git", "-C", str(self.repo)])
        self.assertIn("no-such-ref", err.cmd)
        self.assertEqual(err.returncode, 128)
        self.assertIn("fatal", err.stderr)
        self.assertIn("exit 128", str(err))

    def test_check_false_returns_output_without_raising(self) -> None:
        self.assertEqual(gitops.git(self.repo, "rev-parse", "--verify", "-q", "nope", check=False), "")

    def test_runs_without_shell_and_with_parseable_env(self) -> None:
        with mock.patch.object(gitops.subprocess, "run", wraps=subprocess.run) as spy:
            gitops.git(self.repo, "status")
        args, kwargs = spy.call_args
        self.assertEqual(args[0], ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                                   "-C", str(self.repo), "status"])
        self.assertFalse(kwargs.get("shell", False))
        self.assertEqual(kwargs["env"]["LC_ALL"], "C")
        self.assertEqual(kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(kwargs["timeout"], 120)

    def test_inherited_git_dir_does_not_override_repo(self) -> None:
        bogus = str(self.tmp / "elsewhere.git")
        with mock.patch.dict(os.environ, {"GIT_DIR": bogus, "GIT_WORK_TREE": bogus}):
            top = gitops.git(self.repo, "rev-parse", "--show-toplevel").strip()
        self.assertEqual(Path(top), self.repo)

    def test_timeout_becomes_git_error(self) -> None:
        timeout = subprocess.TimeoutExpired(["git"], 5)
        with mock.patch.object(gitops.subprocess, "run", side_effect=timeout), \
                self.assertRaises(GitError) as caught:
            gitops.git(self.repo, "status", timeout=5)
        self.assertIn("timed out after 5s", str(caught.exception))
        self.assertIsNone(caught.exception.returncode)

    def test_missing_git_binary_becomes_git_error(self) -> None:
        with mock.patch.object(gitops.subprocess, "run", side_effect=FileNotFoundError("git")), \
                self.assertRaises(GitError):
            gitops.git(self.repo, "status")

    def test_missing_directory_raises(self) -> None:
        with self.assertRaises(GitError):
            gitops.git(self.tmp / "missing", "status")

    def test_long_error_output_is_capped_in_message(self) -> None:
        err = GitError(["git", "x"], "e" * 10_000, 1)
        self.assertLess(len(str(err)), 2200)
        self.assertEqual(len(err.stderr), 10_000)

    def test_merge_busy_is_a_git_error(self) -> None:
        self.assertTrue(issubclass(MergeBusy, GitError))


class GitVersionTests(unittest.TestCase):
    def test_real_version(self) -> None:
        version = gitops.git_version()
        self.assertEqual(len(version), 3)
        self.assertGreaterEqual(version, (2, 0, 0))

    def test_parse_variants(self) -> None:
        self.assertEqual(gitops._parse_version("git version 2.43.0\n"), (2, 43, 0))
        self.assertEqual(gitops._parse_version("git version 2.39.2 (Apple Git-143)"), (2, 39, 2))
        self.assertEqual(gitops._parse_version("git version 2.34.1.windows.1"), (2, 34, 1))
        self.assertEqual(gitops._parse_version("git version 2.45"), (2, 45, 0))

    def test_parse_garbage_raises(self) -> None:
        with self.assertRaises(GitError):
            gitops._parse_version("not git")


class DetectBaseTests(GitTestCase):
    def test_configured_branch_wins(self) -> None:
        run_git(self.repo, "branch", "release")
        self.assertEqual(gitops.detect_base(self.repo, "release"), "release")

    def test_configured_branch_must_exist(self) -> None:
        with self.assertRaises(GitError) as caught:
            gitops.detect_base(self.repo, "nope")
        self.assertIn("'nope' does not exist", str(caught.exception))

    def test_configured_remote_only_branch_is_rejected(self) -> None:
        clone = self.clone()
        with self.assertRaises(GitError):
            gitops.detect_base(clone, "origin/main")

    def test_defaults_to_main(self) -> None:
        self.assertEqual(gitops.detect_base(self.repo, None), "main")
        self.assertEqual(gitops.detect_base(self.repo, ""), "main")

    def test_main_preferred_over_master(self) -> None:
        run_git(self.repo, "branch", "master")
        self.assertEqual(gitops.detect_base(self.repo, None), "main")

    def test_master_when_no_main(self) -> None:
        run_git(self.repo, "branch", "-m", "main", "master")
        self.assertEqual(gitops.detect_base(self.repo, None), "master")

    def test_current_branch_as_last_resort(self) -> None:
        run_git(self.repo, "branch", "-m", "main", "trunk")
        self.assertEqual(gitops.detect_base(self.repo, None), "trunk")

    def test_detached_head_without_candidates_raises(self) -> None:
        run_git(self.repo, "branch", "-m", "main", "trunk")
        run_git(self.repo, "checkout", "-q", "--detach")
        with self.assertRaises(GitError):
            gitops.detect_base(self.repo, None)

    def test_repo_without_commits_raises(self) -> None:
        empty = self.tmp / "empty"
        subprocess.run(["git", "init", "-q", "-b", "main", str(empty)], check=True)
        with self.assertRaises(GitError):
            gitops.detect_base(empty, None)

    def test_not_a_repo_raises_git_error(self) -> None:
        plain = self.tmp / "plain"
        plain.mkdir()
        with self.assertRaises(GitError) as caught:
            gitops.detect_base(plain, None)
        self.assertIn("not a git repository", str(caught.exception))

    def test_origin_head_branch_preferred(self) -> None:
        run_git(self.repo, "branch", "develop")
        clone = self.clone()
        run_git(clone, "remote", "set-head", "origin", "develop")
        # origin/HEAD names develop, but there is no local develop yet: fall back to main.
        self.assertEqual(gitops.detect_base(clone, None), "main")
        run_git(clone, "branch", "develop", "origin/develop")
        self.assertEqual(gitops.detect_base(clone, None), "develop")

    def test_origin_head_with_slashed_branch(self) -> None:
        run_git(self.repo, "branch", "release/1.x")
        clone = self.clone()
        run_git(clone, "remote", "set-head", "origin", "release/1.x")
        run_git(clone, "branch", "release/1.x", "origin/release/1.x")
        self.assertEqual(gitops.detect_base(clone, None), "release/1.x")

    def test_clone_default_origin_head(self) -> None:
        run_git(self.repo, "branch", "-m", "main", "trunk")
        clone = self.clone()
        self.assertEqual(run_git(clone, "symbolic-ref", "refs/remotes/origin/HEAD").strip(),
                         "refs/remotes/origin/trunk")
        run_git(clone, "branch", "main")  # origin/HEAD still wins over a local main
        self.assertEqual(gitops.detect_base(clone, None), "trunk")

    def clone(self) -> Path:
        clone = self.tmp / "clone"
        subprocess.run(["git", "clone", "-q", str(self.repo), str(clone)], check=True, capture_output=True)
        return clone


class RevParseTests(GitTestCase):
    def test_returns_full_sha(self) -> None:
        sha = gitops.rev_parse(self.repo, "main")
        self.assertRegex(sha, r"^[0-9a-f]{40}$")
        self.assertEqual(sha, run_git(self.repo, "rev-parse", "HEAD").strip())

    def test_peels_annotated_tag_to_commit(self) -> None:
        run_git(self.repo, "tag", "-a", "v1", "-m", "v1")
        self.assertEqual(gitops.rev_parse(self.repo, "v1"), gitops.rev_parse(self.repo, "main"))

    def test_unknown_ref_raises(self) -> None:
        with self.assertRaises(GitError):
            gitops.rev_parse(self.repo, "nope")

    def test_option_like_ref_is_refused(self) -> None:
        with self.assertRaises(GitError):
            gitops.rev_parse(self.repo, "--all")


class WorktreeTests(GitTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.wt = self.tmp / "data" / "worktrees" / "repo" / "001-add-thing"
        self.branch = "factory/001-add-thing"

    def test_lifecycle(self) -> None:
        gitops.add_worktree(self.repo, self.wt, self.branch, "main")
        self.assertTrue((self.wt / "README.md").is_file())
        self.assertEqual(run_git(self.wt, "symbolic-ref", "--short", "HEAD").strip(), self.branch)
        self.assertEqual(gitops.checked_out_at(self.repo, self.branch), self.wt)
        self.assertTrue(gitops.is_clean(self.wt))
        self.assertEqual(gitops.commits_ahead(self.repo, "main", self.branch), 0)

        commit_file(self.wt, "feature.txt", "new\n")
        self.assertEqual(gitops.commits_ahead(self.repo, "main", self.branch), 1)
        (self.wt / "scratch.txt").write_text("untracked\n")
        self.assertFalse(gitops.is_clean(self.wt))

        gitops.remove_worktree(self.repo, self.wt)
        self.assertFalse(self.wt.exists())
        self.assertEqual(self.worktrees(), [str(self.repo)])
        self.assertIsNone(gitops.checked_out_at(self.repo, self.branch))
        self.assertEqual(gitops.commits_ahead(self.repo, "main", self.branch), 1)  # branch kept
        self.assertTrue(self.wt.parent.is_dir())

    def test_existing_branch_raises(self) -> None:
        run_git(self.repo, "branch", self.branch)
        with self.assertRaises(GitError):
            gitops.add_worktree(self.repo, self.wt, self.branch, "main")
        self.assertFalse(self.wt.exists())

    def test_unknown_base_raises(self) -> None:
        with self.assertRaises(GitError):
            gitops.add_worktree(self.repo, self.wt, self.branch, "no-such-base")

    def test_worktree_starts_at_base(self) -> None:
        run_git(self.repo, "checkout", "-q", "-b", "other")
        commit_file(self.repo, "other.txt", "x\n")
        gitops.add_worktree(self.repo, self.wt, self.branch, "main")
        self.assertFalse((self.wt / "other.txt").exists())
        self.assertEqual(gitops.rev_parse(self.wt, "HEAD"), gitops.rev_parse(self.repo, "main"))

    def test_remove_is_idempotent(self) -> None:
        gitops.add_worktree(self.repo, self.wt, self.branch, "main")
        gitops.remove_worktree(self.repo, self.wt)
        gitops.remove_worktree(self.repo, self.wt)
        gitops.remove_worktree(self.repo, self.tmp / "never-existed")
        self.assertEqual(self.worktrees(), [str(self.repo)])

    def test_remove_discards_dirty_changes(self) -> None:
        gitops.add_worktree(self.repo, self.wt, self.branch, "main")
        (self.wt / "README.md").write_text("changed\n")
        (self.wt / "new.txt").write_text("untracked\n")
        gitops.remove_worktree(self.repo, self.wt)
        self.assertFalse(self.wt.exists())

    def test_remove_after_directory_deleted_prunes(self) -> None:
        gitops.add_worktree(self.repo, self.wt, self.branch, "main")
        shutil.rmtree(self.wt)
        gitops.remove_worktree(self.repo, self.wt)
        self.assertEqual(self.worktrees(), [str(self.repo)])
        gitops.delete_branch(self.repo, self.branch)  # no longer "checked out" anywhere

    def test_remove_deletes_leftover_checkout(self) -> None:
        gitops.add_worktree(self.repo, self.wt, self.branch, "main")
        shutil.rmtree(self.repo / ".git" / "worktrees")  # git no longer knows this worktree
        gitops.remove_worktree(self.repo, self.wt)
        self.assertFalse(self.wt.exists())

    def test_remove_deletes_empty_leftover_directory(self) -> None:
        self.wt.mkdir(parents=True)
        gitops.remove_worktree(self.repo, self.wt)
        self.assertFalse(self.wt.exists())

    def test_remove_refuses_to_delete_non_worktree_directory(self) -> None:
        self.wt.mkdir(parents=True)
        (self.wt / "precious.txt").write_text("keep me\n")
        with self.assertRaises(GitError):
            gitops.remove_worktree(self.repo, self.wt)
        self.assertTrue((self.wt / "precious.txt").is_file())

    def test_remove_refuses_other_repository(self) -> None:
        other = make_repo(self.tmp, name="other")
        with self.assertRaises(GitError):
            gitops.remove_worktree(self.repo, other)
        self.assertTrue((other / ".git").is_dir())

    def test_remove_refuses_worktree_of_another_repository(self) -> None:
        other = make_repo(self.tmp, name="other")
        foreign = self.tmp / "other-wt"
        run_git(other, "worktree", "add", "-q", "-b", "x", str(foreign))
        (foreign / "precious.txt").write_text("uncommitted work\n")
        with self.assertRaises(GitError) as caught:
            gitops.remove_worktree(self.repo, foreign)
        self.assertIn("not a worktree of", str(caught.exception))
        self.assertEqual((foreign / "precious.txt").read_text(), "uncommitted work\n")
        self.assertIn(str(foreign), run_git(other, "worktree", "list"))

    def test_remove_refuses_checkout_whose_gitdir_is_elsewhere(self) -> None:
        self.wt.mkdir(parents=True)
        (self.wt / ".git").write_text(f"gitdir: {self.repo / '.git' / 'modules' / 'sub'}\n")
        (self.wt / "precious.txt").write_text("keep me\n")
        with self.assertRaises(GitError):
            gitops.remove_worktree(self.repo, self.wt)
        self.assertTrue((self.wt / "precious.txt").is_file())

    def test_remove_deletes_leftover_checkout_with_relative_gitdir(self) -> None:
        gitops.add_worktree(self.repo, self.wt, self.branch, "main")
        record = Path(run_git(self.wt, "rev-parse", "--git-dir").strip())
        (self.wt / ".git").write_text(f"gitdir: {os.path.relpath(record, self.wt)}\n")
        shutil.rmtree(self.repo / ".git" / "worktrees")  # git no longer knows this worktree
        gitops.remove_worktree(self.repo, self.wt)
        self.assertFalse(self.wt.exists())

    def test_remove_refuses_main_working_tree(self) -> None:
        with self.assertRaises(GitError):
            gitops.remove_worktree(self.repo, self.repo)
        self.assertTrue((self.repo / "README.md").is_file())
        self.assertTrue((self.repo / ".git").is_dir())


class CheckedOutAtTests(GitTestCase):
    def test_main_checkout(self) -> None:
        self.assertEqual(gitops.checked_out_at(self.repo, "main"), self.repo)

    def test_unknown_and_not_checked_out(self) -> None:
        run_git(self.repo, "branch", "idle")
        self.assertIsNone(gitops.checked_out_at(self.repo, "idle"))
        self.assertIsNone(gitops.checked_out_at(self.repo, "nope"))

    def test_detached_worktree_is_not_a_branch(self) -> None:
        detached = self.tmp / "detached"
        run_git(self.repo, "worktree", "add", "-q", "--detach", str(detached), "main")
        self.assertEqual(gitops.checked_out_at(self.repo, "main"), self.repo)
        run_git(self.repo, "checkout", "-q", "--detach")
        self.assertIsNone(gitops.checked_out_at(self.repo, "main"))

    def test_prefix_branch_names_do_not_match(self) -> None:
        wt = self.tmp / "wt"
        gitops.add_worktree(self.repo, wt, "main-2", "main")
        self.assertEqual(gitops.checked_out_at(self.repo, "main-2"), wt)
        self.assertEqual(gitops.checked_out_at(self.repo, "main"), self.repo)

    def test_bare_repository_entries_are_skipped(self) -> None:
        bare = self.tmp / "bare.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(self.repo), str(bare)], check=True)
        self.assertIsNone(gitops.checked_out_at(bare, "main"))
        wt = self.tmp / "from-bare"
        run_git(bare, "worktree", "add", "-q", str(wt), "main")
        self.assertEqual(gitops.checked_out_at(bare, "main"), wt)


class BranchAndDiffTests(GitTestCase):
    def test_delete_merged_branch(self) -> None:
        run_git(self.repo, "branch", "done")
        gitops.delete_branch(self.repo, "done")
        self.assertIsNone(gitops.checked_out_at(self.repo, "done"))
        self.assertEqual(run_git(self.repo, "branch", "--list", "done"), "")

    def test_delete_unmerged_branch_needs_force(self) -> None:
        self.branch_with("wip", {"wip.txt": "x\n"})
        with self.assertRaises(GitError):
            gitops.delete_branch(self.repo, "wip")
        gitops.delete_branch(self.repo, "wip", force=True)
        self.assertEqual(run_git(self.repo, "branch", "--list", "wip"), "")

    def test_delete_missing_branch_is_noop(self) -> None:
        gitops.delete_branch(self.repo, "never-existed")
        gitops.delete_branch(self.repo, "never-existed", force=True)

    def test_delete_checked_out_branch_raises(self) -> None:
        wt = self.tmp / "wt"
        gitops.add_worktree(self.repo, wt, "busy", "main")
        with self.assertRaises(GitError):
            gitops.delete_branch(self.repo, "busy", force=True)

    def test_commits_ahead(self) -> None:
        self.branch_with("feat", {"a.txt": "1\n", "b.txt": "2\n"})
        commit_file(self.repo, "main-only.txt", "m\n")
        self.assertEqual(gitops.commits_ahead(self.repo, "main", "feat"), 2)
        self.assertEqual(gitops.commits_ahead(self.repo, "feat", "main"), 1)

    def test_is_clean(self) -> None:
        self.assertTrue(gitops.is_clean(self.repo))
        (self.repo / "README.md").write_text("changed\n")
        self.assertFalse(gitops.is_clean(self.repo))
        run_git(self.repo, "checkout", "--", "README.md")
        (self.repo / "untracked.txt").write_text("u\n")
        self.assertFalse(gitops.is_clean(self.repo))

    def test_is_clean_ignores_ignored_files_and_user_untracked_setting(self) -> None:
        commit_file(self.repo, ".gitignore", "*.log\n")
        (self.repo / "debug.log").write_text("x\n")
        self.assertTrue(gitops.is_clean(self.repo))
        run_git(self.repo, "config", "status.showUntrackedFiles", "no")
        (self.repo / "untracked.txt").write_text("u\n")
        self.assertFalse(gitops.is_clean(self.repo))

    def test_changed_files_with_rename_deletion_and_odd_names(self) -> None:
        commit_file(self.repo, "old name.txt", "".join(f"line {i}\n" for i in range(20)))
        commit_file(self.repo, "gone.txt", "bye\n")
        run_git(self.repo, "checkout", "-q", "-b", "feat")
        run_git(self.repo, "mv", "old name.txt", "new name.txt")
        run_git(self.repo, "rm", "-q", "gone.txt")
        (self.repo / "README.md").write_text("# changed\n")
        (self.repo / "dir").mkdir()
        (self.repo / "dir" / "ünïcode.txt").write_text("u\n")
        run_git(self.repo, "add", "-A")
        run_git(self.repo, "commit", "-q", "-m", "feat")
        run_git(self.repo, "checkout", "-q", "main")
        commit_file(self.repo, "main-only.txt", "m\n")  # not part of base...feat

        files = gitops.changed_files(self.repo, "main", "feat")
        self.assertEqual(sorted(files), sorted(
            ["README.md", "dir/ünïcode.txt", "gone.txt", "new name.txt", "old name.txt"]))

    def test_changed_files_empty_when_no_changes(self) -> None:
        run_git(self.repo, "branch", "same")
        self.assertEqual(gitops.changed_files(self.repo, "main", "same"), [])

    def test_changed_lines_counts_added_and_deleted(self) -> None:
        self.branch_with("feat", {"shared.txt": "a\nB\nc\nd\n", "new.txt": "1\n2\n3\n"})
        # shared.txt: b -> B (1 added, 1 deleted) + d added; new.txt: 3 added.
        self.assertEqual(gitops.changed_lines(self.repo, "main", "feat"), 6)

    def test_changed_lines_binary_counts_zero(self) -> None:
        run_git(self.repo, "checkout", "-q", "-b", "bin")
        (self.repo / "blob.bin").write_bytes(bytes(range(256)) * 4)
        commit_file(self.repo, "one.txt", "1\n")
        run_git(self.repo, "add", "blob.bin")
        run_git(self.repo, "commit", "-q", "-m", "binary")
        run_git(self.repo, "checkout", "-q", "main")
        self.assertEqual(gitops.changed_lines(self.repo, "main", "bin"), 1)

    def test_changed_lines_pure_rename_counts_zero(self) -> None:
        commit_file(self.repo, "big.txt", "".join(f"line {i}\n" for i in range(50)))
        run_git(self.repo, "checkout", "-q", "-b", "mv")
        run_git(self.repo, "mv", "big.txt", "moved.txt")
        run_git(self.repo, "commit", "-q", "-m", "mv")
        run_git(self.repo, "checkout", "-q", "main")
        self.assertEqual(gitops.changed_lines(self.repo, "main", "mv"), 0)

    def test_diff_and_diffstat(self) -> None:
        self.branch_with("feat", {"shared.txt": "a\nB\nc\n"})
        text = gitops.diff(self.repo, "main", "feat")
        self.assertIn("diff --git a/shared.txt b/shared.txt", text)
        self.assertIn("-b\n+B\n", text)
        self.assertNotIn("\x1b[", text)
        stat = gitops.diffstat(self.repo, "main", "feat")
        self.assertIn("shared.txt", stat)
        self.assertIn("1 file changed", stat)

    def test_diff_ignores_user_color_config(self) -> None:
        run_git(self.repo, "config", "color.ui", "always")
        self.branch_with("feat", {"shared.txt": "a\nB\nc\n"})
        self.assertNotIn("\x1b[", gitops.diff(self.repo, "main", "feat"))
        self.assertNotIn("\x1b[", gitops.diffstat(self.repo, "main", "feat"))

    def test_diff_truncated_on_line_boundary(self) -> None:
        self.branch_with("big", {"big.txt": "".join(f"line number {i}\n" for i in range(500))})
        full, cut = gitops.diff_truncated(self.repo, "main", "big", 10_000_000)
        self.assertFalse(cut)
        self.assertEqual(full, gitops.diff(self.repo, "main", "big"))

        text, cut = gitops.diff_truncated(self.repo, "main", "big", 1000)
        self.assertTrue(cut)
        self.assertLessEqual(len(text.encode()), 1000)
        self.assertTrue(text.endswith("\n"))
        self.assertTrue(full.startswith(text))
        self.assertGreater(len(text), 900)  # cut at the last newline, not much earlier
        self.assertEqual(gitops.diff(self.repo, "main", "big", max_bytes=1000), text)

    def test_diff_exactly_max_bytes_is_not_truncated(self) -> None:
        self.branch_with("feat", {"shared.txt": "a\nB\nc\n"})
        full = gitops.diff(self.repo, "main", "feat")
        size = len(full.encode())
        self.assertEqual(gitops.diff_truncated(self.repo, "main", "feat", size), (full, False))
        self.assertTrue(gitops.diff_truncated(self.repo, "main", "feat", size - 1)[1])

    def test_diff_without_newline_in_budget_hard_cuts(self) -> None:
        self.branch_with("feat", {"shared.txt": "a\nB\nc\n"})
        text, cut = gitops.diff_truncated(self.repo, "main", "feat", 5)
        self.assertTrue(cut)
        self.assertEqual(text, "diff ")

    def test_diff_truncated_stops_git_instead_of_reading_everything(self) -> None:
        self.branch_with("huge", {"huge.txt": "".join(f"generated line {i}\n" for i in range(300_000))})
        procs: list[subprocess.Popen] = []
        real_popen = subprocess.Popen

        def spy(*args, **kwargs):  # type: ignore[no-untyped-def]
            procs.append(real_popen(*args, **kwargs))
            return procs[-1]

        with mock.patch.object(gitops.subprocess, "Popen", side_effect=spy):
            text, cut = gitops.diff_truncated(self.repo, "main", "huge", 4096)
        self.assertTrue(cut)
        self.assertLessEqual(len(text.encode()), 4096)
        self.assertTrue(text.endswith("\n"))
        self.assertEqual(len(procs), 1)
        self.assertLess(procs[0].returncode, 0)  # killed early: its 6 MB of output was never drained

    def test_diff_timeout_raises_git_error(self) -> None:
        real_popen = subprocess.Popen

        def hang(cmd, **kwargs):  # type: ignore[no-untyped-def]
            return real_popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)

        with mock.patch.object(gitops, "DEFAULT_TIMEOUT", 0.3), \
                mock.patch.object(gitops.subprocess, "Popen", side_effect=hang), \
                self.assertRaises(GitError) as caught:
            gitops.diff(self.repo, "main", "main")
        self.assertIn("timed out after 0.3s", str(caught.exception))

    def test_diff_missing_git_binary_raises_git_error(self) -> None:
        with mock.patch.object(gitops.subprocess, "Popen", side_effect=FileNotFoundError("git")), \
                self.assertRaises(GitError):
            gitops.diff(self.repo, "main", "main")

    def test_diff_unknown_branch_raises(self) -> None:
        with self.assertRaises(GitError):
            gitops.diff(self.repo, "main", "nope")
        with self.assertRaises(GitError):
            gitops.changed_files(self.repo, "main", "nope")

    def test_option_like_refs_are_refused(self) -> None:
        target = self.tmp / "pwned"
        for call in (gitops.changed_files, gitops.changed_lines, gitops.diff, gitops.diffstat,
                     gitops.commits_ahead):
            with self.subTest(call=call.__name__), self.assertRaises(GitError):
                call(self.repo, "main", f"--output={target}")
        self.assertFalse(target.exists())

    def test_stash_all_clean_returns_false(self) -> None:
        self.assertFalse(gitops.stash_all(self.repo, "factory: nothing"))
        self.assertEqual(run_git(self.repo, "stash", "list"), "")

    def test_stash_all_includes_untracked(self) -> None:
        wt = self.tmp / "wt"
        gitops.add_worktree(self.repo, wt, "factory/002-x", "main")
        (wt / "README.md").write_text("reviewer edit\n")
        (wt / "notes.txt").write_text("untracked\n")
        self.assertTrue(gitops.stash_all(wt, "factory: discarded reviewer edits round 1"))
        self.assertTrue(gitops.is_clean(wt))
        self.assertFalse((wt / "notes.txt").exists())
        self.assertIn("factory: discarded reviewer edits round 1", run_git(wt, "stash", "list"))
        self.assertEqual(gitops.commits_ahead(self.repo, "main", "factory/002-x"), 0)

    def test_stash_all_raises_when_changes_remain(self) -> None:
        nested = self.repo / "vendor" / "lib"
        nested.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(nested)], check=True)
        (nested / "x.py").write_text("reviewer edit\n")
        with self.assertRaises(GitError) as caught:
            gitops.stash_all(self.repo, "factory: discarded reviewer edits round 1")
        self.assertIn("vendor/", str(caught.exception))
        self.assertFalse(gitops.is_clean(self.repo))


class ConflictsContract:
    """Behaviour both conflict-check implementations must share (mixed into two suites)."""

    tmp: Path
    repo: Path

    def conflicts(self, base: str, branch: str) -> list[str]:
        raise NotImplementedError

    def assert_repo_untouched(self, head: str) -> None:
        self.assertEqual(gitops.rev_parse(self.repo, "HEAD"), head)
        self.assertTrue(gitops.is_clean(self.repo))
        self.assertFalse(self.has_merge_head(self.repo))
        self.assertEqual(self.worktrees(), [str(self.repo)])

    def test_clean_merge_has_no_conflicts(self) -> None:
        self.branch_with("feat", {"new.txt": "n\n"})
        commit_file(self.repo, "main.txt", "m\n")
        head = gitops.rev_parse(self.repo, "HEAD")
        self.assertEqual(self.conflicts("main", "feat"), [])
        self.assert_repo_untouched(head)

    def test_conflicting_paths_are_listed_sorted(self) -> None:
        self.branch_with("feat", {"shared.txt": "a\nFEAT\nc\n", "b file.txt": "feat\n", "ok.txt": "f\n"})
        commit_file(self.repo, "shared.txt", "a\nMAIN\nc\n")
        commit_file(self.repo, "b file.txt", "main\n")
        head = gitops.rev_parse(self.repo, "HEAD")
        self.assertEqual(self.conflicts("main", "feat"), ["b file.txt", "shared.txt"])
        self.assert_repo_untouched(head)

    def test_modify_delete_conflict(self) -> None:
        self.branch_with("feat", {"shared.txt": "a\nFEAT\nc\n"})
        run_git(self.repo, "rm", "-q", "shared.txt")
        run_git(self.repo, "commit", "-q", "-m", "delete")
        self.assertEqual(self.conflicts("main", "feat"), ["shared.txt"])

    def test_already_merged_branch(self) -> None:
        run_git(self.repo, "branch", "old")
        commit_file(self.repo, "later.txt", "l\n")
        self.assertEqual(self.conflicts("main", "old"), [])

    def test_fast_forwardable_branch(self) -> None:
        self.branch_with("feat", {"shared.txt": "a\nFEAT\nc\n"})
        self.assertEqual(self.conflicts("main", "feat"), [])

    def test_unknown_branch_raises(self) -> None:
        with self.assertRaises(GitError):
            self.conflicts("main", "nope")
        self.assertEqual(self.worktrees(), [str(self.repo)])

    def test_unrelated_histories_raise(self) -> None:
        run_git(self.repo, "checkout", "-q", "--orphan", "lone")
        run_git(self.repo, "rm", "-rqf", ".")
        commit_file(self.repo, "lone.txt", "z\n")
        run_git(self.repo, "checkout", "-q", "main")
        with self.assertRaises(GitError):
            self.conflicts("main", "lone")
        self.assertEqual(self.worktrees(), [str(self.repo)])

    def test_works_when_base_is_checked_out_and_dirty(self) -> None:
        self.branch_with("feat", {"shared.txt": "a\nFEAT\nc\n"})
        commit_file(self.repo, "shared.txt", "a\nMAIN\nc\n")
        (self.repo / "README.md").write_text("user is editing\n")
        self.assertEqual(self.conflicts("main", "feat"), ["shared.txt"])
        self.assertEqual((self.repo / "README.md").read_text(), "user is editing\n")


class GateDiffContract:
    """changed_files/changed_lines must cover what the merge really does to base.

    Mixed into a merge-tree suite and a trial-merge suite, like ConflictsContract.
    """

    tmp: Path
    repo: Path

    def merge_result_call(self, func, base: str, branch: str):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def changed_files(self, base: str, branch: str) -> list[str]:
        return self.merge_result_call(gitops.changed_files, base, branch)

    def changed_lines(self, base: str, branch: str) -> int:
        return self.merge_result_call(gitops.changed_lines, base, branch)

    def merged_paths(self, branch: str) -> set[str]:
        """Merge branch into main for real and return the paths it changed on main."""
        before = gitops.rev_parse(self.repo, "main")
        gitops.merge(self.repo, "main", branch, f"merge {branch}", self.tmp / "merge-tmp")
        return set(run_git(self.repo, "diff", "--name-only", "--no-renames", before, "main").split())

    def test_plain_branch_lists_its_own_changes(self) -> None:
        self.branch_with("feat", {"new.txt": "n\n", "shared.txt": "a\nB\nc\n"})
        commit_file(self.repo, "main-only.txt", "m\n")
        self.assertEqual(self.changed_files("main", "feat"), ["new.txt", "shared.txt"])
        self.assertEqual(self.changed_lines("main", "feat"), 3)

    def test_already_merged_branch_changes_nothing(self) -> None:
        run_git(self.repo, "branch", "old")
        commit_file(self.repo, "later.txt", "l\n")
        self.assertEqual(self.changed_files("main", "old"), [])
        self.assertEqual(self.changed_lines("main", "old"), 0)

    def test_several_merge_bases_cannot_hide_a_revert_of_base(self) -> None:
        # Base hardened ci.yml on a side branch (Y) and merged it. The agent's commit has
        # parents X (main before that merge) and Y, and X's tree: git's three-dot diff
        # picks X as the merge base and shows only b.txt, but the merge reverts ci.yml.
        ci = ".github/workflows/ci.yml"
        commit_dated(self.repo, "2026-01-01T00:00:00", {ci: "old\n"})
        run_git(self.repo, "checkout", "-q", "-b", "side")
        y = commit_dated(self.repo, "2026-01-02T00:00:00", {ci: "hardened\n"})
        run_git(self.repo, "checkout", "-q", "main")
        x = commit_dated(self.repo, "2026-01-03T00:00:00", {"shared.txt": "a\nX\nc\n"})
        run_git(self.repo, "merge", "-q", "--no-ff", "--no-edit", "side")
        run_git(self.repo, "checkout", "-q", "-b", "agent", x)
        (self.repo / "b.txt").write_text("feature\n")
        run_git(self.repo, "add", "b.txt")
        tree = run_git(self.repo, "write-tree").strip()
        crafted = run_git(self.repo, "commit-tree", tree, "-p", x, "-p", y, "-m", "feature").strip()
        run_git(self.repo, "reset", "-q", "--hard", crafted)
        run_git(self.repo, "checkout", "-q", "main")
        self.assertEqual(len(run_git(self.repo, "merge-base", "--all", "main", "agent").split()), 2)
        self.assertEqual(gitops._names(self.repo, "main...agent"), ["b.txt"])  # what the old gate saw

        files = self.changed_files("main", "agent")
        self.assertEqual(files, [ci, "b.txt"])
        self.assertEqual(gitops.matches_any(ci, list(MatchesAnyTests.DEFAULTS)), ".github/**")
        self.assertEqual(self.changed_lines("main", "agent"), 3)  # b.txt +1, ci.yml +1 -1
        self.assertEqual(gitops.conflicts(self.repo, "main", "agent"), [])
        self.assertLessEqual(self.merged_paths("agent"), set(files))
        self.assertEqual((self.repo / ci).read_text(), "old\n")  # the merge really does revert it

    def test_rename_on_base_carries_branch_edit_into_protected_path(self) -> None:
        commit_file(self.repo, "docs/x.yml", "old\n")
        self.branch_with("feat", {"docs/x.yml": "new\n"})
        (self.repo / ".github" / "workflows").mkdir(parents=True)
        run_git(self.repo, "mv", "docs/x.yml", ".github/workflows/x.yml")
        run_git(self.repo, "commit", "-q", "-m", "move")
        self.assertEqual(gitops._names(self.repo, "main...feat"), ["docs/x.yml"])
        files = self.changed_files("main", "feat")
        self.assertEqual(files, [".github/workflows/x.yml", "docs/x.yml"])
        self.assertLessEqual(self.merged_paths("feat"), set(files))
        self.assertEqual((self.repo / ".github/workflows/x.yml").read_text(), "new\n")

    def test_directory_rename_on_base_moves_new_file_into_protected_dir(self) -> None:
        run_git(self.repo, "config", "merge.directoryRenames", "true")
        commit_file(self.repo, "ci/build.yml", "x\n")
        self.branch_with("feat", {"ci/evil.yml": "evil\n"})
        (self.repo / ".github").mkdir()
        run_git(self.repo, "mv", "ci", ".github/workflows")
        run_git(self.repo, "commit", "-q", "-m", "move ci")
        files = self.changed_files("main", "feat")
        self.assertIn(".github/workflows/evil.yml", files)
        self.assertLessEqual(self.merged_paths("feat"), set(files))

    def test_conflicted_paths_are_included(self) -> None:
        self.branch_with("feat", {"shared.txt": "a\nFEAT\nc\n", "ok.txt": "f\n"})
        commit_file(self.repo, "shared.txt", "a\nMAIN\nc\n")
        self.assertEqual(self.changed_files("main", "feat"), ["ok.txt", "shared.txt"])
        self.assertGreaterEqual(self.changed_lines("main", "feat"), 3)

    def test_gate_diff_of_unrelated_histories_raises(self) -> None:
        run_git(self.repo, "checkout", "-q", "--orphan", "lone")
        run_git(self.repo, "rm", "-rqf", ".")
        commit_file(self.repo, "lone.txt", "z\n")
        run_git(self.repo, "checkout", "-q", "main")
        for func in (gitops.changed_files, gitops.changed_lines):
            with self.subTest(func=func.__name__), self.assertRaises(GitError):
                self.merge_result_call(func, "main", "lone")


@unittest.skipIf(gitops.git_version() < gitops.MERGE_TREE_MIN_VERSION, "git < 2.38 has no merge-tree --write-tree")
class MergeTreeConflictsTests(ConflictsContract, GateDiffContract, GitTestCase):
    def conflicts(self, base: str, branch: str) -> list[str]:
        return self.merge_result_call(gitops.conflicts, base, branch)

    def merge_result_call(self, func, base: str, branch: str):  # type: ignore[no-untyped-def]
        with mock.patch.object(gitops, "_trial_merge", side_effect=AssertionError("fallback used")):
            return func(self.repo, base, branch)


class TrialMergeConflictsTests(ConflictsContract, GateDiffContract, GitTestCase):
    def conflicts(self, base: str, branch: str) -> list[str]:
        return self.merge_result_call(gitops.conflicts, base, branch)

    def merge_result_call(self, func, base: str, branch: str):  # type: ignore[no-untyped-def]
        self.trial_root = self.tmp / "data" / "tmp"
        with mock.patch.object(gitops, "git_version", return_value=OLD_GIT), \
                mock.patch.object(gitops, "_merge_tree", side_effect=AssertionError("merge-tree used")):
            try:
                return func(self.repo, base, branch, tmp_dir=self.trial_root)
            finally:
                if self.trial_root.exists():
                    self.assertEqual(list(self.trial_root.iterdir()), [])  # temp worktree removed
                self.assertEqual(self.worktrees(), [str(self.repo)])

    def test_default_tmp_dir_is_system_temp(self) -> None:
        self.branch_with("feat", {"shared.txt": "a\nFEAT\nc\n"})
        commit_file(self.repo, "shared.txt", "a\nMAIN\nc\n")
        system_tmp = self.tmp / "system-tmp"
        with mock.patch.object(gitops, "git_version", return_value=OLD_GIT), \
                mock.patch.object(gitops.tempfile, "gettempdir", return_value=str(system_tmp)):
            self.assertEqual(gitops.conflicts(self.repo, "main", "feat"), ["shared.txt"])
        self.assertEqual(list(system_tmp.iterdir()), [])
        self.assertEqual(self.worktrees(), [str(self.repo)])

    def test_unknown_base_raises(self) -> None:
        with self.assertRaises(GitError):
            self.conflicts("nope", "main")


class MergeTests(GitTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.merge_tmp = self.tmp / "data" / "tmp"

    def merge(self, branch: str = "feat", message: str = "Merge feat") -> str:
        return gitops.merge(self.repo, "main", branch, message, self.merge_tmp)

    def parents(self, sha: str) -> list[str]:
        return run_git(self.repo, "rev-list", "--parents", "-n", "1", sha).split()[1:]

    def assert_no_temp_worktrees(self) -> None:
        self.assertEqual([p for p in self.worktrees() if "merge-" in p], [])
        if self.merge_tmp.exists():
            self.assertEqual(list(self.merge_tmp.iterdir()), [])

    def test_merge_into_clean_checked_out_base(self) -> None:
        feature = self.branch_with("feat", {"feature.txt": "f\n"})
        before = gitops.rev_parse(self.repo, "main")
        (self.repo / "untracked.txt").write_text("does not block\n")
        sha = self.merge(message="factory: merge 001-feat")
        self.assertEqual(sha, gitops.rev_parse(self.repo, "main"))
        self.assertEqual(self.parents(sha), [before, feature])
        self.assertEqual(run_git(self.repo, "log", "-1", "--format=%s", sha).strip(), "factory: merge 001-feat")
        self.assertEqual((self.repo / "feature.txt").read_text(), "f\n")  # checkout updated
        self.assertEqual(run_git(self.repo, "status", "--porcelain", "--untracked-files=no"), "")
        self.assert_no_temp_worktrees()

    def test_fast_forwardable_branch_still_gets_merge_commit(self) -> None:
        self.branch_with("feat", {"feature.txt": "f\n"})
        sha = self.merge()
        self.assertEqual(len(self.parents(sha)), 2)

    def test_dirty_checked_out_base_is_busy(self) -> None:
        self.branch_with("feat", {"feature.txt": "f\n"})
        before = gitops.rev_parse(self.repo, "main")
        (self.repo / "README.md").write_text("user edit in progress\n")
        with self.assertRaises(MergeBusy) as caught:
            self.merge()
        self.assertIn("uncommitted changes", str(caught.exception))
        self.assertEqual(gitops.rev_parse(self.repo, "main"), before)
        self.assertEqual((self.repo / "README.md").read_text(), "user edit in progress\n")

    def test_staged_change_on_checked_out_base_is_busy(self) -> None:
        self.branch_with("feat", {"feature.txt": "f\n"})
        (self.repo / "staged.txt").write_text("s\n")
        run_git(self.repo, "add", "staged.txt")
        with self.assertRaises(MergeBusy):
            self.merge()
        self.assertIn("A  staged.txt", run_git(self.repo, "status", "--porcelain"))

    def test_user_merge_in_progress_is_busy_and_left_alone(self) -> None:
        run_git(self.repo, "checkout", "-q", "-b", "empty")
        run_git(self.repo, "commit", "-q", "--allow-empty", "-m", "empty")
        run_git(self.repo, "checkout", "-q", "main")
        run_git(self.repo, "merge", "--no-commit", "--no-ff", "empty")
        self.assertTrue(self.has_merge_head(self.repo))
        self.branch_with_worktree("feat", {"feature.txt": "f\n"})
        with self.assertRaises(MergeBusy) as caught:
            self.merge()
        self.assertIn("merge in progress", str(caught.exception))
        self.assertTrue(self.has_merge_head(self.repo))  # the user's merge was not aborted

    def test_base_not_checked_out_uses_temp_worktree(self) -> None:
        feature = self.branch_with("feat", {"feature.txt": "f\n"})
        run_git(self.repo, "checkout", "-q", "-b", "dev")
        (self.repo / "README.md").write_text("dirty on another branch is fine\n")
        before = gitops.rev_parse(self.repo, "main")
        sha = self.merge()
        self.assertEqual(sha, gitops.rev_parse(self.repo, "main"))
        self.assertEqual(self.parents(sha), [before, feature])
        self.assertEqual(run_git(self.repo, "symbolic-ref", "--short", "HEAD").strip(), "dev")
        self.assertFalse((self.repo / "feature.txt").exists())
        self.assertEqual((self.repo / "README.md").read_text(), "dirty on another branch is fine\n")
        self.assertEqual(self.worktrees(), [str(self.repo)])
        self.assert_no_temp_worktrees()

    def test_temp_worktree_lives_in_tmp_dir(self) -> None:
        self.branch_with("feat", {"feature.txt": "f\n"})
        run_git(self.repo, "checkout", "-q", "-b", "dev")
        seen = []
        real_merge_in = gitops._merge_in

        def spy(checkout: Path, branch: str, message: str) -> None:
            seen.append(checkout)
            real_merge_in(checkout, branch, message)

        with mock.patch.object(gitops, "_merge_in", side_effect=spy):
            self.merge()
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].parent, self.merge_tmp)
        self.assertTrue(seen[0].name.startswith("merge-"))
        self.assertFalse(seen[0].exists())

    def test_base_checked_out_in_another_worktree(self) -> None:
        self.branch_with("feat", {"feature.txt": "f\n"})
        run_git(self.repo, "checkout", "-q", "-b", "dev")
        elsewhere = self.tmp / "main-checkout"
        run_git(self.repo, "worktree", "add", "-q", str(elsewhere), "main")
        sha = self.merge()
        self.assertEqual(run_git(elsewhere, "rev-parse", "HEAD").strip(), sha)
        self.assertTrue((elsewhere / "feature.txt").is_file())

    def test_deleted_checkout_of_base_does_not_block(self) -> None:
        self.branch_with("feat", {"feature.txt": "f\n"})
        run_git(self.repo, "checkout", "-q", "-b", "dev")
        gone = self.tmp / "gone"
        run_git(self.repo, "worktree", "add", "-q", str(gone), "main")
        shutil.rmtree(gone)
        sha = self.merge()
        self.assertEqual(sha, gitops.rev_parse(self.repo, "main"))
        self.assertEqual(self.worktrees(), [str(self.repo)])

    def test_conflict_on_checked_out_base_aborts(self) -> None:
        self.branch_with("feat", {"shared.txt": "a\nFEAT\nc\n"})
        before = commit_file(self.repo, "shared.txt", "a\nMAIN\nc\n")
        with self.assertRaises(GitError) as caught:
            self.merge()
        self.assertNotIsInstance(caught.exception, MergeBusy)
        self.assertIn("CONFLICT", str(caught.exception))
        self.assertEqual(gitops.rev_parse(self.repo, "main"), before)
        self.assertTrue(gitops.is_clean(self.repo))
        self.assertFalse(self.has_merge_head(self.repo))
        self.assertEqual((self.repo / "shared.txt").read_text(), "a\nMAIN\nc\n")

    def test_conflict_in_temp_worktree_cleans_up(self) -> None:
        self.branch_with("feat", {"shared.txt": "a\nFEAT\nc\n"})
        before = commit_file(self.repo, "shared.txt", "a\nMAIN\nc\n")
        run_git(self.repo, "checkout", "-q", "-b", "dev")
        with self.assertRaises(GitError) as caught:
            self.merge()
        self.assertNotIsInstance(caught.exception, MergeBusy)
        self.assertEqual(gitops.rev_parse(self.repo, "main"), before)
        self.assertEqual(self.worktrees(), [str(self.repo)])
        self.assert_no_temp_worktrees()

    def test_base_being_rebased_is_busy(self) -> None:
        self.branch_with("feat", {"feature.txt": "f\n"})
        self.branch_with("upstream", {"shared.txt": "a\nUP\nc\n"})
        commit_file(self.repo, "shared.txt", "a\nMAIN\nc\n")
        rebase = subprocess.run(["git", "-C", str(self.repo), "rebase", "upstream"],
                                capture_output=True, check=False)
        self.assertNotEqual(rebase.returncode, 0)  # stopped on a conflict, HEAD detached
        self.addCleanup(subprocess.run, ["git", "-C", str(self.repo), "rebase", "--abort"],
                        capture_output=True, check=False)
        with self.assertRaises(MergeBusy):
            self.merge()
        self.assert_no_temp_worktrees()

    def test_unknown_branch_raises(self) -> None:
        before = gitops.rev_parse(self.repo, "main")
        with self.assertRaises(GitError):
            self.merge(branch="nope")
        self.assertEqual(gitops.rev_parse(self.repo, "main"), before)
        self.assertTrue(gitops.is_clean(self.repo))

    def test_option_like_branch_is_refused(self) -> None:
        with self.assertRaises(GitError):
            self.merge(branch="--abort")

    def test_concurrent_merges_queue(self) -> None:
        for n in range(4):
            self.branch_with(f"feat{n}", {f"f{n}.txt": f"{n}\n"})
        errors: list[BaseException] = []

        def work(n: int) -> None:
            try:
                self.merge(branch=f"feat{n}", message=f"merge {n}")
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(n,)) for n in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertEqual(errors, [])
        for n in range(4):
            self.assertTrue((self.repo / f"f{n}.txt").is_file())
        self.assertEqual(gitops.commits_ahead(self.repo, "feat0", "main"), 7)  # 3 features + 4 merges

    def branch_with_worktree(self, branch: str, files: dict[str, str]) -> None:
        """Like branch_with, for when the main checkout cannot switch branches."""
        wt = self.tmp / f"wt-{branch}"
        gitops.add_worktree(self.repo, wt, branch, "main")
        for rel, text in files.items():
            commit_file(wt, rel, text)


HOOKS = ("pre-commit", "prepare-commit-msg", "commit-msg", "post-commit", "pre-merge-commit", "post-merge",
         "post-checkout", "post-rewrite", "reference-transaction", "post-index-change", "pre-auto-gc")


class HostSideHardeningTests(GitTestCase):
    """The factory's own git calls never run repository-controlled code: hooks or core.fsmonitor."""

    def setUp(self) -> None:
        super().setUp()
        self.marker = self.tmp / "ran.log"
        self.branch_with("feat", {"feature.txt": "f\n", "shared.txt": "a\nFEAT\nc\n"})
        self.branch_with("clash", {"shared.txt": "a\nCLASH\nc\n"})
        self.wt = self.tmp / "data" / "worktrees" / "repo" / "001-x"
        gitops.add_worktree(self.repo, self.wt, "factory/001-x", "main")
        commit_file(self.wt, "built.txt", "built\n")
        hooks = self.repo / ".git" / "hooks"
        hooks.mkdir(exist_ok=True)
        for name in HOOKS:
            hook = hooks / name
            hook.write_text(f"#!/bin/sh\necho \"hook {name}\" >> '{self.marker}'\nexit 0\n")
            hook.chmod(0o755)
        monitor = self.tmp / "fsmonitor.sh"
        monitor.write_text(f"#!/bin/sh\necho \"fsmonitor $*\" >> '{self.marker}'\nexit 1\n")
        monitor.chmod(0o755)
        run_git(self.repo, "config", "core.fsmonitor", str(monitor))

    def plain_git(self, path: Path, *args: str) -> None:
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)

    def hardened_git(self, path: Path, *args: str) -> None:
        """Test setup that must not itself trip the markers."""
        subprocess.run(["git", *gitops.HARDENING, "-C", str(path), *args], check=True, capture_output=True)

    def ran(self) -> str:
        return self.marker.read_text() if self.marker.exists() else ""

    def assert_the_markers_work(self) -> None:
        """Control: plain git does run the fsmonitor command and the hooks this repo installs."""
        self.plain_git(self.wt, "status", "--porcelain")
        self.plain_git(self.repo, "worktree", "add", "-q", "--detach", str(self.tmp / "control"), "main")
        ran = self.ran()
        self.marker.unlink(missing_ok=True)
        self.hardened_git(self.repo, "worktree", "remove", "--force", str(self.tmp / "control"))
        if "fsmonitor" not in ran or "hook post-checkout" not in ran:
            self.skipTest(f"this git runs neither hooks nor core.fsmonitor here ({ran!r})")

    def test_gate_reads_status_and_stash_run_no_repository_code(self) -> None:
        self.assert_the_markers_work()
        (self.wt / "README.md").write_text("reviewer edit\n")
        (self.wt / "notes.txt").write_text("untracked\n")
        self.assertFalse(gitops.is_clean(self.wt, self.repo))
        self.assertTrue(gitops.is_clean(self.repo, self.repo))
        self.assertEqual(gitops.commits_ahead(self.repo, "main", "factory/001-x"), 1)
        self.assertEqual(gitops.changed_files(self.repo, "main", "feat"), ["feature.txt", "shared.txt"])
        self.assertGreater(gitops.changed_lines(self.repo, "main", "feat"), 0)
        self.assertEqual(gitops.conflicts(self.repo, "feat", "clash"), ["shared.txt"])
        self.assertIn("feature.txt", gitops.diff(self.repo, "main", "feat"))
        self.assertTrue(gitops.stash_all(self.wt, "factory: discarded reviewer edits round 1", self.repo))
        self.assertTrue(gitops.is_clean(self.wt, self.repo))
        self.assertEqual(self.ran(), "")

    def test_trial_merge_of_old_git_runs_no_repository_code(self) -> None:
        self.assert_the_markers_work()
        tmp_dir = self.tmp / "data" / "tmp"
        with mock.patch.object(gitops, "git_version", return_value=OLD_GIT):
            self.assertEqual(gitops.conflicts(self.repo, "feat", "clash", tmp_dir=tmp_dir), ["shared.txt"])
            self.assertEqual(gitops.changed_files(self.repo, "main", "feat", tmp_dir=tmp_dir),
                             ["feature.txt", "shared.txt"])
        self.assertEqual(self.ran(), "")

    def test_merge_commit_skips_the_hooks(self) -> None:
        self.assert_the_markers_work()
        sha = gitops.merge(self.repo, "main", "feat", "Merge feat", self.tmp / "data" / "tmp")
        self.assertEqual(sha, gitops.rev_parse(self.repo, "main"))
        self.assertEqual(len(run_git(self.repo, "rev-list", "--parents", "-n", "1", sha).split()), 3)
        self.hardened_git(self.repo, "checkout", "-q", "--detach")   # base no longer checked out
        sha = gitops.merge(self.repo, "main", "factory/001-x", "Merge job", self.tmp / "data" / "tmp")
        self.assertEqual(sha, gitops.rev_parse(self.repo, "main"))
        gitops.remove_worktree(self.repo, self.wt)
        self.assertEqual(self.ran(), "")

    def test_hooks_path_set_in_config_is_overridden_too(self) -> None:
        planted = self.tmp / "planted-hooks"
        planted.mkdir()
        (planted / "post-checkout").write_text(f"#!/bin/sh\necho planted >> '{self.marker}'\n")
        (planted / "post-checkout").chmod(0o755)
        run_git(self.repo, "config", "core.hooksPath", str(planted))
        gitops.add_worktree(self.repo, self.tmp / "wt2", "factory/002-y", "main")
        self.assertTrue(gitops.is_clean(self.tmp / "wt2", self.repo))
        self.assertEqual(self.ran(), "")


class VerifyWorktreeTests(GitTestCase):
    """is_clean and stash_all on a worktree first check its git pointers still lead back to the repo."""

    def setUp(self) -> None:
        super().setUp()
        self.wt = self.tmp / "data" / "worktrees" / "repo" / "001-x"
        gitops.add_worktree(self.repo, self.wt, "factory/001-x", "main")
        self.gitdir = self.repo / ".git" / "worktrees" / "001-x"
        self.other = make_repo(self.tmp, "other")
        self.other_wt = self.tmp / "other-wt"
        run_git(self.other, "worktree", "add", "-q", "-b", "evil", str(self.other_wt), "main")
        (self.wt / "dirty.txt").write_text("edit\n")

    def assert_refused(self, why: str) -> None:
        with self.assertRaisesRegex(GitError, why):
            gitops.verify_worktree(self.repo, self.wt)
        with self.assertRaisesRegex(GitError, "refusing to run git"):
            gitops.is_clean(self.wt, self.repo)
        with self.assertRaisesRegex(GitError, "refusing to run git"):
            gitops.stash_all(self.wt, "factory: discarded", self.repo)
        self.assertEqual(run_git(self.other, "stash", "list"), "", "nothing ran in the other repository")

    def test_a_real_worktree_and_the_main_checkout_pass(self) -> None:
        gitops.verify_worktree(self.repo, self.wt)
        gitops.verify_worktree(self.repo, self.repo)
        self.assertFalse(gitops.is_clean(self.wt, self.repo))
        self.assertTrue(gitops.stash_all(self.wt, "factory: discarded", self.repo))
        self.assertTrue(gitops.is_clean(self.wt, self.repo))

    def test_relative_gitdir_pointers_pass(self) -> None:
        (self.wt / ".git").write_text(f"gitdir: {os.path.relpath(self.gitdir, self.wt)}\n")
        gitops.verify_worktree(self.repo, self.wt)

    def test_dot_git_repointed_at_another_repository(self) -> None:
        (self.wt / ".git").write_text(f"gitdir: {self.other / '.git' / 'worktrees' / 'other-wt'}\n")
        self.assert_refused("not into")

    def test_dot_git_repointed_at_another_worktree_of_the_repo(self) -> None:
        sibling = self.tmp / "wt-sibling"
        gitops.add_worktree(self.repo, sibling, "factory/002-y", "main")
        (self.wt / ".git").write_text(f"gitdir: {self.repo / '.git' / 'worktrees' / 'wt-sibling'}\n")
        self.assert_refused("does not point back")

    def test_dot_git_as_symlink_directory_or_garbage(self) -> None:
        dotgit = self.wt / ".git"
        text = dotgit.read_text()
        (self.tmp / "pointer").write_text(text)
        for name, plant in (("symlink", lambda: dotgit.symlink_to(self.tmp / "pointer")),
                            ("directory", lambda: shutil.copytree(self.other / ".git", dotgit)),
                            ("garbage", lambda: dotgit.write_text("not a pointer\n")),
                            ("missing", lambda: None)):
            with self.subTest(plant=name):
                if dotgit.is_dir() and not dotgit.is_symlink():
                    shutil.rmtree(dotgit)
                else:
                    dotgit.unlink(missing_ok=True)
                plant()
                self.assert_refused("not a gitdir pointer")
        if dotgit.is_dir() and not dotgit.is_symlink():
            shutil.rmtree(dotgit)
        dotgit.write_text(text)
        gitops.verify_worktree(self.repo, self.wt)

    def test_commondir_repointed(self) -> None:
        (self.gitdir / "commondir").write_text(f"{self.other / '.git'}\n")
        self.assert_refused("commondir")
        (self.gitdir / "commondir").unlink()
        self.assert_refused("commondir")

    def test_gitdir_back_pointer_changed(self) -> None:
        (self.gitdir / "gitdir").write_text(f"{self.other_wt / '.git'}\n")
        self.assert_refused("does not point back")

    def test_merge_refuses_a_base_checkout_whose_pointer_was_changed(self) -> None:
        run_git(self.repo, "checkout", "-q", "--detach")
        base_wt = self.tmp / "base-wt"
        run_git(self.repo, "worktree", "add", "-q", str(base_wt), "main")
        (base_wt / ".git").write_text(f"gitdir: {self.other / '.git' / 'worktrees' / 'other-wt'}\n")
        before = gitops.rev_parse(self.other, "evil")
        with self.assertRaisesRegex(GitError, "refusing to run git"):
            gitops.merge(self.repo, "main", "factory/001-x", "Merge", self.tmp / "data" / "tmp")
        self.assertEqual(gitops.rev_parse(self.other, "evil"), before)


class MatchesAnyTests(unittest.TestCase):
    DEFAULTS = (".github/**", ".gitlab-ci.yml", ".circleci/**", "Jenkinsfile",
                ".claude/**", ".qwen/**", ".codex/**", ".agents/**", "AGENTS.md", "CLAUDE.md", "QWEN.md",
                "**/Dockerfile", "docker-compose*.yml", "**/migrations/**",
                "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
                "requirements*.txt", "pyproject.toml", "poetry.lock", "uv.lock",
                "go.mod", "go.sum", "Cargo.toml", "Cargo.lock", "Gemfile", "Gemfile.lock",
                "**/.env*", "**/*.pem", "**/*.key")

    def check(self, cases: list[tuple[str, str, bool]]) -> None:
        for glob, path, expected in cases:
            with self.subTest(glob=glob, path=path):
                self.assertEqual(gitops.matches_any(path, [glob]), glob if expected else None)

    def test_basename_patterns_match_at_any_depth(self) -> None:
        self.check([
            ("package.json", "package.json", True),
            ("package.json", "web/package.json", True),
            ("package.json", "a/b/c/package.json", True),
            ("package.json", "package.json5", False),
            ("package.json", "mypackage.json", False),
            ("package.json", "docs/package.json.md", False),
            ("*.pem", "a/b/c.pem", True),
            ("*.pem", "c.pem", True),
            ("*.pem", "c.pem.txt", False),
            (".env*", "x/.env.local", True),
            (".env*", ".env", True),
            (".env*", "x/my.env", False),
            ("docker-compose*.yml", "docker-compose.prod.yml", True),
            ("docker-compose*.yml", "deploy/docker-compose.yml", True),
            ("requirements*.txt", "requirements-dev.txt", True),
            ("Jenkinsfile", "ci/Jenkinsfile", True),
        ])

    def test_basename_pattern_matching_a_directory_covers_its_contents(self) -> None:
        self.check([
            ("node_modules", "node_modules/x/index.js", True),
            ("node_modules", "web/node_modules/x/index.js", True),
            ("*.key", "secrets.key/readme.txt", True),
        ])

    def test_anchored_patterns(self) -> None:
        self.check([
            (".github/**", ".github/workflows/ci.yml", True),
            (".github/**", ".github/CODEOWNERS", True),
            (".github/**", "x/.github/workflows/ci.yml", False),
            (".github/**", ".githubx/ci.yml", False),
            (".github/**", "my.github/ci.yml", False),
            ("src/*.py", "src/a.py", True),
            ("src/*.py", "src/x/a.py", False),
            ("src/*.py", "lib/src/a.py", False),
            ("docs/build", "docs/build/index.html", True),
            ("docs/build", "docs/build", True),
            ("docs/build", "docs/builder", False),
        ])

    def test_double_star(self) -> None:
        self.check([
            ("**/migrations/**", "migrations/1.sql", True),
            ("**/migrations/**", "db/migrations/1.sql", True),
            ("**/migrations/**", "a/b/migrations/x/2.sql", True),
            ("**/migrations/**", "dbmigrations/1.sql", False),
            ("**/migrations/**", "migrations.sql", False),
            ("**/Dockerfile", "Dockerfile", True),
            ("**/Dockerfile", "svc/Dockerfile", True),
            ("**/Dockerfile", "svc/api/Dockerfile", True),
            ("**/Dockerfile", "svc/Dockerfile.dev", False),
            ("**/Dockerfile", "svc/MyDockerfile", False),
            ("a/**/b", "a/b", True),
            ("a/**/b", "a/x/b", True),
            ("a/**/b", "a/x/y/b", True),
            ("a/**/b", "a/xb", False),
            ("a/**/b", "xa/b", False),
            ("**", "anything/at/all.txt", True),
            ("**/*.key", "deep/dir/server.key", True),
            ("**/*.key", "server.key", True),
            ("**/.env*", "x/.env.local", True),
            ("a**b", "axxb", True),
            ("a**b", "ax/xb", False),
        ])

    def test_wildcards_never_cross_slash(self) -> None:
        self.check([
            ("a?c", "abc", True),
            ("a?c", "a/c", False),
            ("a*c", "a/c", False),
            ("a[bx]c", "abc", True),
            ("a[bx]c", "ayc", False),
            ("a[!x]c", "ayc", True),
            ("a[!x]c", "axc", False),
            ("a[!x]c", "a/c", False),
            ("a[^x]c", "ayc", True),
            ("file[0-9].txt", "file7.txt", True),
            ("file[0-9].txt", "filex.txt", False),
            ("[]]x", "]x", True),
            ("a[bc", "a[bc", True),
        ])

    def test_trailing_slash_means_everything_below(self) -> None:
        self.check([
            ("build/", "build/x.js", True),
            ("build/", "lib/build/x.js", True),
            ("build/", "builds/x.js", False),
            ("build/", "builds", False),
            ("vendor/lib/", "vendor/lib/a/b.c", True),
            ("vendor/lib/", "x/vendor/lib/a.c", False),
        ])

    def test_directory_glob_matches_the_directory_path_itself(self) -> None:
        # A file or symlink at a protected directory's path (".claude -> docs/cfg") is a
        # change to that directory, so the gate must see it.
        self.check([
            ("build/", "build", True),
            ("build/", "lib/build", True),
            ("vendor/lib/", "vendor/lib", True),
            (".github/**", ".github", True),
            (".github/**", ".githubx", False),
            (".github/**", "x/.github", False),
            ("**/migrations/**", "migrations", True),
            ("**/migrations/**", "db/migrations", True),
            ("**/migrations/**", "dbmigrations", False),
            ("a/**/**", "a", True),
            ("/.claude/**", ".claude", True),
            ("**", "x", True),
        ])

    def test_leading_dot_slash_and_slash_anchor(self) -> None:
        self.check([
            ("./x.yml", "x.yml", True),
            ("/x.yml", "x.yml", True),
            ("/x.yml", "sub/x.yml", False),
            ("./.github/**", ".github/ci.yml", True),
            ("/.github/**", ".github/ci.yml", True),
            ("x.yml", "./x.yml", True),
            ("x.yml", "/x.yml", True),
        ])

    def test_regex_metacharacters_are_literal(self) -> None:
        self.check([
            ("a+b(1).txt", "a+b(1).txt", True),
            ("a+b(1).txt", "aab1.txt", False),
            ("x.y", "xzy", False),
            ("$HOME", "$HOME", True),
            ("\\*literal", "*literal", True),
            ("\\*literal", "xliteral", False),
        ])

    def test_returns_first_matching_pattern(self) -> None:
        globs = ["*.md", "docs/**", "**/*.md"]
        self.assertEqual(gitops.matches_any("docs/readme.md", globs), "*.md")
        self.assertEqual(gitops.matches_any("docs/img.png", globs), "docs/**")
        self.assertIsNone(gitops.matches_any("src/app.py", globs))

    def test_empty_inputs(self) -> None:
        self.assertIsNone(gitops.matches_any("a.txt", []))
        self.assertIsNone(gitops.matches_any("a.txt", ["", "  ", "/", "./"]))
        self.assertEqual(gitops.matches_any("a.txt", ["", "a.txt"]), "a.txt")

    def test_default_escalate_paths(self) -> None:
        protected = {
            ".github/workflows/ci.yml": ".github/**",
            ".gitlab-ci.yml": ".gitlab-ci.yml",
            ".circleci/config.yml": ".circleci/**",
            ".claude/settings.json": ".claude/**",
            ".agents/skills/x/SKILL.md": ".agents/**",
            "AGENTS.md": "AGENTS.md",
            "services/api/AGENTS.md": "AGENTS.md",
            "svc/Dockerfile": "**/Dockerfile",
            "docker-compose.yml": "docker-compose*.yml",
            "app/db/migrations/0001_init.py": "**/migrations/**",
            "web/package.json": "package.json",
            "web/pnpm-lock.yaml": "pnpm-lock.yaml",
            "requirements-dev.txt": "requirements*.txt",
            "backend/pyproject.toml": "pyproject.toml",
            "go.sum": "go.sum",
            "config/.env.production": "**/.env*",
            "certs/server.pem": "**/*.pem",
            "certs/server.key": "**/*.key",
        }
        protected.update({".claude": ".claude/**", ".github": ".github/**", ".agents": ".agents/**",
                          ".codex": ".codex/**", ".qwen": ".qwen/**", ".circleci": ".circleci/**",
                          "migrations": "**/migrations/**", "db/migrations": "**/migrations/**"})
        for path, glob in protected.items():
            with self.subTest(path=path):
                self.assertEqual(gitops.matches_any(path, list(self.DEFAULTS)), glob)
        for path in ["src/app.py", "README.md", "docs/github.md", "web/src/package.ts",
                     "tests/test_migrations.py", "Dockerfile.md", "keys.txt", "environment.py",
                     ".claudeignore", "docs/.github", "migrations.py"]:
            with self.subTest(path=path):
                self.assertIsNone(gitops.matches_any(path, list(self.DEFAULTS)))


if __name__ == "__main__":
    unittest.main()
