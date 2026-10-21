from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from swfactory.agentdefs import MAX_FILE_BYTES
from swfactory.config import Config
from swfactory.skills import MAX_DEPTH, SkillIndex, find_skill_files, parse_skill_file, skill_dirs


def skill_md(name: str | None = None, description: str = "", extra: str = "", body: str = "Do the thing.") -> str:
    lines = ["---"]
    if name:
        lines.append(f"name: {name}")
    if description:
        lines.append(f"description: {description}")
    if extra:
        lines.append(extra)
    return "\n".join(lines + ["---", "", body, ""])


class SkillsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()

    def write(self, rel: str, text: str) -> Path:
        path = self.tmp / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def make_pocock_tree(self) -> Path:
        """A clone shaped like mattpocock/skills: skills/<category>/<skill>/SKILL.md."""
        root = self.tmp / "mattpocock-skills"
        self.write("mattpocock-skills/README.md", "# Skills\n")
        self.write("mattpocock-skills/skills/engineering/tdd/SKILL.md", skill_md(
            "tdd", "Test-driven development with a red-green-refactor loop. Use when the user wants "
                   "to build features test-first.", body="# TDD\n\nWrite a failing test first."))
        self.write("mattpocock-skills/skills/engineering/tdd/tests.md", "good tests")
        self.write("mattpocock-skills/skills/engineering/tdd/refs/mocking.md", "mocking")
        self.write("mattpocock-skills/skills/engineering/tdd/node_modules/x/index.js", "")
        self.write("mattpocock-skills/skills/engineering/tdd/examples/inner/SKILL.md", skill_md("inner", "nested"))
        self.write("mattpocock-skills/skills/engineering/improve-codebase-architecture/SKILL.md",
                   skill_md(None, "Find deepening opportunities in a codebase."))
        self.write("mattpocock-skills/skills/engineering/ddd/SKILL.md",
                   skill_md("domain-model", "Model the domain:\n  ubiquitous   language."))
        self.write("mattpocock-skills/skills/productivity/write-a-prd/SKILL.md", skill_md(
            "write-a-prd", "Create a PRD through an interview.", "disable-model-invocation: true"))
        self.write("mattpocock-skills/skills/productivity/grill-me/SKILL.md",
                   skill_md("grill-me", "Interview the user relentlessly.", "disable-model-invocation: false"))
        self.write("mattpocock-skills/skills/node_modules/pkg/SKILL.md", skill_md("from-node-modules", "no"))
        self.write("mattpocock-skills/.git/hooks/SKILL.md", skill_md("from-git", "no"))
        self.write("mattpocock-skills/skills/broken/SKILL.md", "no frontmatter here")
        self.write("mattpocock-skills/skills/badbool/SKILL.md",
                   skill_md("badbool", "x", "disable-model-invocation: perhaps"))
        return root


class FindSkillFilesTest(SkillsTestCase):
    def test_depth_limit_and_order(self) -> None:
        self.write("root/SKILL.md", skill_md("root-skill", "the dir itself"))
        self.write("other/a/b/c/d/SKILL.md", skill_md("depth-4", "found"))
        self.write("other/a/b/c/d/e/SKILL.md", skill_md("depth-5", "too deep"))
        self.write("other/z/SKILL.md", skill_md("depth-1", "shallow"))
        self.write("other/m/n/SKILL.md", skill_md("depth-2", "middle"))
        self.assertEqual(MAX_DEPTH, 4)
        self.assertEqual(find_skill_files(self.tmp / "root"), [self.tmp / "root" / "SKILL.md"])
        found = [p.relative_to(self.tmp / "other").as_posix() for p in find_skill_files(self.tmp / "other")]
        self.assertEqual(found, ["z/SKILL.md", "m/n/SKILL.md", "a/b/c/d/SKILL.md"])
        self.assertEqual(find_skill_files(self.tmp / "other", max_depth=1), [self.tmp / "other/z/SKILL.md"])

    def test_missing_root_and_file_root(self) -> None:
        self.assertEqual(find_skill_files(self.tmp / "missing"), [])
        self.assertEqual(find_skill_files(self.write("file.txt", "x")), [])

    def scandir_calls(self, root: Path) -> tuple[list[Path], list[str]]:
        calls: list[str] = []
        real_scandir = os.scandir

        def spy(path):
            calls.append(str(path))
            return real_scandir(path)

        with mock.patch("swfactory.skills.os.scandir", side_effect=spy):
            return find_skill_files(root), calls

    def test_unreadable_folder_is_skipped(self) -> None:
        self.write("root/ok/SKILL.md", skill_md("ok", "readable"))
        self.write("root/locked/inner/SKILL.md", skill_md("inner", "hidden"))
        real_is_file = Path.is_file

        def is_file(path: Path) -> bool:
            if "locked" in path.parts:
                raise PermissionError(13, "Permission denied", str(path))
            return real_is_file(path)

        with mock.patch.object(Path, "is_file", autospec=True, side_effect=is_file):
            index = SkillIndex([self.tmp / "root"])
        self.assertEqual([skill.name for skill in index.list()], ["ok"])
        self.assertEqual(index.errors, [])

    @unittest.skipIf(os.geteuid() == 0, "root ignores directory permissions")
    def test_mode_000_folder_is_skipped(self) -> None:
        self.write("root/ok/SKILL.md", skill_md("ok", "readable"))
        locked = self.tmp / "root" / "locked"
        self.write("root/locked/sub/SKILL.md", skill_md("sub", "hidden"))
        os.chmod(locked, 0)
        self.addCleanup(os.chmod, locked, 0o755)
        self.assertEqual([skill.name for skill in SkillIndex([self.tmp / "root"]).list()], ["ok"])

    def test_symlinks_into_pseudo_filesystems_are_not_walked(self) -> None:
        self.write("root/tdd/SKILL.md", skill_md("tdd", "real"))
        for name, target in (("zz-proc1", "/proc/1"), ("zz-proc", "/proc"), ("zz-sys", "/sys"), ("zz-dev", "/dev")):
            os.symlink(target, self.tmp / "root" / name)
        found, calls = self.scandir_calls(self.tmp / "root")
        self.assertEqual(found, [self.tmp / "root/tdd/SKILL.md"])
        self.assertEqual(calls, [str(self.tmp / "root")])
        self.assertEqual(find_skill_files(Path("/proc")), [])

    def test_symlink_loops_list_each_real_folder_once(self) -> None:
        self.write("root/real-skill/SKILL.md", skill_md(None, "no name"))
        self.write("root/group/deep/SKILL.md", skill_md("deep", "nested"))
        for number in range(30):
            os.symlink(".", self.tmp / "root" / f"loop{number:02d}")
        os.symlink("real-skill", self.tmp / "root" / "alias")
        os.symlink("..", self.tmp / "root" / "group" / "up")
        found, calls = self.scandir_calls(self.tmp / "root")
        self.assertEqual(sorted(calls), sorted([str(self.tmp / "root"), str(self.tmp / "root/group")]))
        self.assertEqual([p.relative_to(self.tmp / "root").as_posix() for p in found],
                         ["alias/SKILL.md", "real-skill/SKILL.md", "group/deep/SKILL.md"])
        names = [skill.name for skill in SkillIndex([self.tmp / "root"]).list()]
        self.assertEqual(names, ["alias", "deep", "real-skill"])


class SkillIndexTest(SkillsTestCase):
    def test_pocock_tree(self) -> None:
        index = SkillIndex([self.make_pocock_tree()])
        names = [skill.name for skill in index.list()]
        self.assertEqual(names, ["domain-model", "grill-me", "improve-codebase-architecture", "tdd", "write-a-prd"])
        tdd = index.get("tdd")
        root = self.tmp / "mattpocock-skills"
        self.assertEqual(tdd.path, root / "skills/engineering/tdd/SKILL.md")
        self.assertEqual(tdd.dir, root / "skills/engineering/tdd")
        self.assertEqual(tdd.body, "# TDD\n\nWrite a failing test first.")
        self.assertTrue(tdd.model_invocable)
        self.assertEqual(index.get("improve-codebase-architecture").description,
                         "Find deepening opportunities in a codebase.")
        self.assertEqual(index.get("domain-model").description, "Model the domain: ubiquitous language.")
        self.assertFalse(index.get("write-a-prd").model_invocable)
        self.assertTrue(index.get("grill-me").model_invocable)
        self.assertIsNone(index.get("ddd"))
        self.assertIsNone(index.get("inner"))
        self.assertIsNone(index.get("missing"))

    def test_errors_are_recorded_and_bad_skills_skipped(self) -> None:
        index = SkillIndex([self.make_pocock_tree()])
        self.assertEqual(len(index.errors), 2)
        self.assertTrue(any("broken/SKILL.md" in e and "missing frontmatter" in e for e in index.errors))
        self.assertTrue(any("badbool/SKILL.md" in e and "disable-model-invocation" in e for e in index.errors))
        self.assertIsNone(index.get("badbool"))

    def test_catalog_text(self) -> None:
        index = SkillIndex([self.make_pocock_tree()])
        self.assertEqual(index.catalog_text().splitlines(), [
            "- domain-model: Model the domain: ubiquitous language.",
            "- grill-me: Interview the user relentlessly.",
            "- improve-codebase-architecture: Find deepening opportunities in a codebase.",
            "- tdd: Test-driven development with a red-green-refactor loop. Use when the user wants to build "
            "features test-first.",
        ])
        self.write("solo/nodesc/SKILL.md", skill_md("nodesc"))
        self.assertEqual(SkillIndex([self.tmp / "solo"]).catalog_text(), "- nodesc:")
        self.assertEqual(SkillIndex([]).catalog_text(), "")

    def test_first_found_name_wins(self) -> None:
        self.write("project/tdd/SKILL.md", skill_md("tdd", "project version"))
        self.write("home/aaa/deep/tdd/SKILL.md", skill_md("tdd", "deep home version"))
        self.write("home/zzz/SKILL.md", skill_md("tdd", "shallow home version"))
        self.write("home/only-home/SKILL.md", skill_md(None, "home only"))
        index = SkillIndex([self.tmp / "project", self.tmp / "home"])
        self.assertEqual(index.get("tdd").description, "project version")
        self.assertEqual(SkillIndex([self.tmp / "home"]).get("tdd").description, "shallow home version")
        self.assertEqual(index.get("only-home").description, "home only")

    def test_symlinked_skill_is_followed(self) -> None:
        real = self.tmp / "store" / "real-skill"
        self.write("store/real-skill/SKILL.md", skill_md(None, "installed by symlink"))
        self.write("store/real-skill/ref.md", "ref")
        skills = self.tmp / "home" / ".claude" / "skills"
        skills.mkdir(parents=True)
        os.symlink(real, skills / "linked")
        skill = SkillIndex([skills]).get("linked")
        self.assertIsNotNone(skill)
        self.assertEqual((skill.dir, skill.path), (real, real / "SKILL.md"))
        self.assertEqual(skill.description, "installed by symlink")
        self.assertEqual(SkillIndex([skills]).read_roots(), [real])

    def test_read_roots(self) -> None:
        index = SkillIndex([self.make_pocock_tree()])
        roots = index.read_roots()
        self.assertEqual(len(roots), 5)
        self.assertIn(self.tmp / "mattpocock-skills/skills/engineering/tdd", roots)

    def test_oversized_skill_file_is_an_error_not_a_hang(self) -> None:
        self.write("root/huge/SKILL.md", skill_md("huge", "big", body="x" * MAX_FILE_BYTES))
        self.write("root/fine/SKILL.md", skill_md("fine", "small"))
        index = SkillIndex([self.tmp / "root"])
        self.assertEqual([skill.name for skill in index.list()], ["fine"])
        self.assertTrue(any("huge/SKILL.md" in e and "larger than" in e for e in index.errors))

    def test_parse_skill_file_meta(self) -> None:
        path = self.write("x/SKILL.md", skill_md("x", "d", "allowed-tools: Read, Grep\nlicense: MIT"))
        skill = parse_skill_file(path)
        self.assertEqual(skill.meta["allowed-tools"], "Read, Grep")
        self.assertEqual(skill.meta["license"], "MIT")
        with self.assertRaisesRegex(ValueError, "cannot read"):
            parse_skill_file(self.tmp / "nope/SKILL.md")


class SkillDirsTest(SkillsTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.home = self.tmp / "home"
        self.home.mkdir()
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def mkdirs(self, *rels: str) -> list[Path]:
        paths = [self.tmp / rel for rel in rels]
        for path in paths:
            path.mkdir(parents=True, exist_ok=True)
        return paths

    def test_order_existing_only_and_harness_dirs(self) -> None:
        expected = self.mkdirs(
            "wt/.claude/skills", "wt/.agents/skills", "repo/.qwen/skills", "repo/.agents/skills",
            "home/.claude/skills", "home/.agents/skills", "home/.config/software-factory/skills",
            "home/extra", "conf/relative")
        config = SimpleNamespace(raw={"harness": {"skills_dirs": ["~/extra", "relative", "/does/not/exist"]}},
                                 path=self.tmp / "conf" / "config.json")
        self.assertEqual(skill_dirs(config, self.tmp / "repo", self.tmp / "wt"), expected)

    def test_without_config_or_project(self) -> None:
        expected = self.mkdirs("home/.qwen/skills")
        self.mkdirs("wt/.claude/skills")
        self.assertEqual(skill_dirs(None, None, None), expected)

    def test_deduplicates_and_resolves(self) -> None:
        (project,) = self.mkdirs("repo/.agents/skills")
        os.symlink(project, self.home / "link")
        config = SimpleNamespace(raw={"harness": {"skills_dirs": ["~/link", str(project)]}}, path=self.tmp / "c.json")
        self.assertEqual(skill_dirs(config, self.tmp / "repo", self.tmp / "repo"), [project])

    def test_real_config_and_null_harness(self) -> None:
        (extra,) = self.mkdirs("extra")
        config = Config(self.tmp / "config.json", {"harness": {"skills_dirs": [str(extra)]}})
        self.assertEqual(skill_dirs(config, None, None), [extra])
        self.assertEqual(skill_dirs(Config(self.tmp / "config.json", {"harness": None}), None, None), [])

    def test_project_dirs_escaping_the_project_are_dropped(self) -> None:
        (outside,) = self.mkdirs("outside")
        (inside,) = self.mkdirs("wt/.agents/skills")
        (self.tmp / "wt/.claude").mkdir()
        os.symlink("/", self.tmp / "wt/.claude/skills")
        (self.tmp / "wt/.qwen").mkdir()
        os.symlink(outside, self.tmp / "wt/.qwen/skills")
        (self.tmp / "repo/.claude").mkdir(parents=True)
        os.symlink("../.agents/skills", self.tmp / "repo/.claude/skills")
        (repo_skills,) = self.mkdirs("repo/.agents/skills")
        self.assertEqual(skill_dirs(None, self.tmp / "repo", self.tmp / "wt"), [inside, repo_skills])

    def test_home_and_configured_dirs_may_point_anywhere(self) -> None:
        (store,) = self.mkdirs("store/skills")
        (self.home / ".agents").mkdir()
        os.symlink(store, self.home / ".agents" / "skills")
        config = SimpleNamespace(raw={"harness": {"skills_dirs": ["/"]}}, path=self.tmp / "c.json")
        self.assertEqual(skill_dirs(config, None, None), [store, Path("/")])

    def test_dirs_that_cannot_be_checked_are_skipped(self) -> None:
        (home_skills,) = self.mkdirs("home/.claude/skills")
        self.mkdirs("wt/.agents/skills")
        real_is_dir = Path.is_dir

        def is_dir(path: Path) -> bool:
            if "wt" in path.parts:
                raise PermissionError(13, "Permission denied", str(path))
            return real_is_dir(path)

        with mock.patch.object(Path, "is_dir", autospec=True, side_effect=is_dir):
            self.assertEqual(skill_dirs(None, None, self.tmp / "wt"), [home_skills])

    def test_reviewer_repro_proc_symlink_in_worktree(self) -> None:
        self.write("wt/.agents/skills/tdd/SKILL.md", skill_md("tdd", "d"))
        os.symlink("/proc/1", self.tmp / "wt/.agents/skills/zz-link")
        index = SkillIndex(skill_dirs(None, None, self.tmp / "wt"))
        self.assertEqual(([skill.name for skill in index.list()], index.errors), (["tdd"], []))

    def test_index_over_skill_dirs(self) -> None:
        self.write("wt/.agents/skills/tdd/SKILL.md", skill_md("tdd", "worktree copy"))
        self.write("repo/.agents/skills/tdd/SKILL.md", skill_md("tdd", "main checkout copy"))
        self.write("repo/.agents/skills/untracked/SKILL.md", skill_md("untracked", "only in the checkout"))
        self.write("home/.claude/skills/personal/SKILL.md", skill_md("personal", "user level"))
        index = SkillIndex(skill_dirs(None, self.tmp / "repo", self.tmp / "wt"))
        self.assertEqual([s.name for s in index.list()], ["personal", "tdd", "untracked"])
        self.assertEqual(index.get("tdd").description, "worktree copy")


if __name__ == "__main__":
    unittest.main()
