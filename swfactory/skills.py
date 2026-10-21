"""Skills: discovery of SKILL.md folders (Claude Code / Codex / Qwen Code / mattpocock/skills format).

A skill is a directory holding SKILL.md (frontmatter with name/description, body
= instructions) plus any supporting files. Collections such as mattpocock/skills
nest them (skills/engineering/tdd/SKILL.md), so each search dir is walked a few
levels deep.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .agentdefs import MAX_FILE_BYTES, parse_bool, parse_frontmatter, read_small_text
from .config import USER_DIR, dir_exists, expand_path

if TYPE_CHECKING:
    from .config import Config

SKILL_FILE = "SKILL.md"
MAX_DEPTH = 4
SKIP_DIRS = frozenset({".git", "node_modules"})
PSEUDO_FS_DIRS = (Path("/proc"), Path("/sys"), Path("/dev"))


@dataclass
class Skill:
    name: str
    description: str
    path: Path
    dir: Path
    body: str
    model_invocable: bool = True
    meta: dict[str, str] = field(default_factory=dict)


def parse_skill_file(path: str | Path) -> Skill:
    """Read one SKILL.md. Name = frontmatter name or the folder name; ValueError naming path if malformed."""
    path = Path(path)
    try:
        text = read_small_text(path, MAX_FILE_BYTES)
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"{path}: cannot read skill file: {exc}") from exc
    meta, body = parse_frontmatter(text, path)
    try:
        disabled = parse_bool(meta.get("disable-model-invocation"), False)
    except ValueError as exc:
        raise ValueError(f"{path}: disable-model-invocation: {exc}") from exc
    return Skill(
        name=meta.get("name") or path.parent.name,
        description=" ".join(meta.get("description", "").split()),
        path=path.resolve(),
        dir=path.parent.resolve(),
        body=body,
        model_invocable=not disabled,
        meta=meta,
    )


def find_skill_files(root: Path, max_depth: int = MAX_DEPTH) -> list[Path]:
    """SKILL.md files whose folder is at most max_depth levels below root, shallowest first.

    Breadth-first, names sorted within a level, symlinked dirs followed. A folder that
    holds SKILL.md is a skill: its subfolders are its files and are not searched.
    Unreadable folders are skipped, /proc, /sys and /dev are never entered, and each real
    folder is listed once, so symlink loops cannot multiply the walk.
    """
    found: list[Path] = []
    listed: set[Path] = set()
    level = [(Path(root), Path(os.path.realpath(root)))]
    for depth in range(max_depth + 1):
        deeper: list[tuple[Path, Path]] = []
        for directory, real in level:
            if _in_pseudo_fs(real):
                continue
            skill_file = directory / SKILL_FILE
            try:
                is_skill = skill_file.is_file()
            except OSError:
                continue
            if is_skill:
                found.append(skill_file)
                continue
            if depth == max_depth or real in listed:
                continue
            listed.add(real)
            try:
                entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
            except OSError:
                continue
            for entry in entries:
                try:
                    if entry.name in SKIP_DIRS or not entry.is_dir():
                        continue
                    target = Path(os.path.realpath(entry.path)) if entry.is_symlink() else real / entry.name
                except OSError:
                    continue
                deeper.append((Path(entry.path), target))
        level = deeper
    return found


def _in_pseudo_fs(path: Path) -> bool:
    return any(path == top or top in path.parents for top in PSEUDO_FS_DIRS)


class SkillIndex:
    """All skills found under dirs. On a name clash the first found wins (earlier dir, then shallower)."""

    def __init__(self, dirs: list[Path]):
        self.dirs = [Path(directory) for directory in dirs]
        self.errors: list[str] = []
        self._skills: dict[str, Skill] = {}
        for root in self.dirs:
            for skill_file in find_skill_files(root):
                try:
                    skill = parse_skill_file(skill_file)
                except ValueError as exc:
                    self.errors.append(str(exc))
                    continue
                self._skills.setdefault(skill.name, skill)

    def get(self, name: str) -> Skill | None:
        """The skill called name, or None. Includes skills hidden from the model (for agent preloads)."""
        return self._skills.get(name)

    def catalog_text(self) -> str:
        """One "- name: description" line per model-invocable skill, sorted by name."""
        return "\n".join(f"- {skill.name}: {skill.description}".rstrip()
                         for skill in self.list() if skill.model_invocable)

    def read_roots(self) -> list[Path]:
        """Resolved folders agents must be able to read: every skill dir (covers symlinked skills)."""
        roots: list[Path] = []
        for skill in self.list():
            for directory in (skill.dir, skill.path.parent):
                if directory not in roots:
                    roots.append(directory)
        return roots

    def list(self) -> list[Skill]:
        """Every skill, sorted by name."""
        return sorted(self._skills.values(), key=lambda skill: skill.name)


def skill_dirs(config: Config | None, repo_path: Path | None, worktree: Path | None) -> list[Path]:
    """Existing skill search dirs, resolved and deduplicated, in priority order (first wins):

    <worktree> then <repo_path> each with .claude/skills, .qwen/skills, .agents/skills;
    ~/.claude/skills, ~/.qwen/skills, ~/.agents/skills, ~/.config/software-factory/skills;
    then harness.skills_dirs ("~" expanded, relative paths anchored at the config file's dir).
    The repo checkout comes after the worktree because untracked skills exist only there.
    A project dir that resolves outside its project (a committed symlink such as
    `.agents/skills -> /`) is dropped; dirs that cannot be checked are skipped.
    """
    candidates: list[tuple[Path, Path | None]] = []
    for project in (worktree, repo_path):
        if project is not None:
            root = Path(project).resolve()
            candidates += [(Path(project) / sub / "skills", root) for sub in (".claude", ".qwen", ".agents")]
    home = Path(os.path.expanduser("~"))
    trusted = [home / ".claude" / "skills", home / ".qwen" / "skills", home / ".agents" / "skills",
               Path(os.path.expanduser(USER_DIR)) / "skills"]
    if config is not None:
        harness = config.raw.get("harness")
        extra = harness.get("skills_dirs") if isinstance(harness, dict) else None
        trusted += [expand_path(entry, config.path.parent) for entry in extra or []]
    candidates += [(candidate, None) for candidate in trusted]
    result: list[Path] = []
    for candidate, confine in candidates:
        if not dir_exists(candidate):
            continue
        resolved = candidate.resolve()
        if confine is not None and resolved != confine and confine not in resolved.parents:
            continue
        if resolved not in result:
            result.append(resolved)
    return result
