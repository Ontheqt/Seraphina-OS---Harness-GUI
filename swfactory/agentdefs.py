"""Agent definitions: Claude Code subagent files (markdown + frontmatter).

The frontmatter parser here is shared with skills.py. It reads the YAML subset
these files use: ``key: value`` lines between two ``---`` lines, values as
strings, lists comma-separated or ``[a, b]``.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

REQUIRED_AGENTS = ("spec-writer", "builder", "approver")
DEFAULT_TOOLS = ("Read", "Write", "Edit", "Glob", "Grep", "Bash", "Skill")
AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
MAX_FILE_BYTES = 1 << 20  # agent and skill files

_DELIMITER = "---"
_BLOCK_SCALAR_RE = re.compile(r"^[>|][-+0-9]*$")
_INLINE_COMMENT_RE = re.compile(r"\s+#")
_TRUE = {"true", "yes", "on", "1"}
_FALSE = {"false", "no", "off", "0"}


@dataclass
class AgentDef:
    name: str
    description: str
    tools: list[str]
    model: str
    skills: list[str]
    mcp: list[str]
    color: str | None
    body: str
    path: Path
    meta: dict[str, str] = field(default_factory=dict)
    # the browser profile a browsing agent may use (BROWSER_AND_CODEX.md §2.6): read only from the project and user
    # agent directories, never from a repository's .factory/agents (discover_agents refuses it there)
    browser: str | None = None

    def to_dict(self) -> dict:
        """JSON-ready view (path as a string, raw frontmatter under "meta")."""
        return {"name": self.name, "description": self.description, "tools": list(self.tools),
                "model": self.model, "skills": list(self.skills), "mcp": list(self.mcp),
                "color": self.color, "body": self.body, "path": str(self.path), "meta": dict(self.meta)}


def read_small_text(path: str | os.PathLike, max_bytes: int) -> str:
    """UTF-8 text of a regular file of at most max_bytes; never blocks on a FIFO or reads a device.

    Raises OSError if the file is missing, unreadable, not a regular file or too large,
    and UnicodeDecodeError if it is not UTF-8.
    """
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{path}: not a regular file")
        handle = open(fd, "rb")
    except BaseException:
        os.close(fd)
        raise
    with handle:
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise OSError(f"{path}: larger than {max_bytes} bytes")
    return data.decode("utf-8")


def clean_value(raw: str) -> str:
    """Strip whitespace, matching surrounding quotes, and a trailing " # comment" on unquoted values."""
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    if value[:1] in ("'", '"'):
        end = value.find(value[0], 1)
        if end > 0 and value[end + 1:].lstrip().startswith("#"):
            return value[1:end]
        return value
    comment = _INLINE_COMMENT_RE.search(value)
    if comment:
        value = value[:comment.start()]
    return value.strip()


def parse_frontmatter(text: str, path: str | Path) -> tuple[dict[str, str], str]:
    """Split a markdown file into (frontmatter, body); ValueError naming path if malformed.

    Blank lines and lines starting with "#" are ignored. Indented lines continue the
    previous key (YAML block scalars and multi-line values are folded into one string;
    "- item" lines become a comma-separated list).
    """
    lines = text.lstrip("﻿").splitlines()
    if not lines or lines[0].strip() != _DELIMITER:
        raise ValueError(f"{path}: missing frontmatter (the first line must be '---')")
    meta: dict[str, str] = {}
    parts: dict[str, list[str]] = {}
    modes: dict[str, str] = {}
    key: str | None = None
    for index in range(1, len(lines)):
        line = lines[index]
        stripped = line.strip()
        if line.rstrip() == _DELIMITER:
            for name, pieces in parts.items():
                meta[name] = _join(pieces, modes[name])
            return meta, "\n".join(lines[index + 1:]).strip()
        if not stripped or stripped.startswith("#"):
            continue
        is_item = stripped == "-" or stripped.startswith("- ")
        if line[:1].isspace() or is_item:
            if key is None:
                raise ValueError(f"{path}:{index + 1}: continuation line before any key: {line!r}")
            if is_item and not parts[key] and modes[key] == "fold":
                modes[key] = "list"
            parts[key].append(clean_value(stripped[1:]) if modes[key] == "list" and is_item else stripped)
            continue
        name, colon, value = stripped.partition(":")
        name = name.strip()
        if not colon or not name:
            raise ValueError(f"{path}:{index + 1}: expected 'key: value', got {line!r}")
        key = name
        value = value.strip()
        if _BLOCK_SCALAR_RE.match(value):
            parts[key], modes[key] = [], "literal" if value.startswith("|") else "fold"
        else:
            cleaned = clean_value(value)
            parts[key], modes[key] = ([cleaned] if cleaned else []), "fold"
    raise ValueError(f"{path}: frontmatter is not closed by a '---' line")


def _join(pieces: list[str], mode: str) -> str:
    if mode == "list":
        return ", ".join(piece for piece in pieces if piece)
    return ("\n" if mode == "literal" else " ").join(pieces)


def split_list(value: str | None) -> list[str]:
    """Parse "a, b" or "[a, b]" into ["a", "b"] (items unquoted, empties dropped)."""
    if not value:
        return []
    text = value.strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    items = (clean_value(item) for item in text.split(","))
    return [item for item in items if item]


def parse_bool(value: str | None, default: bool = False) -> bool:
    """true/yes/on/1 or false/no/off/0 (any case); None or "" gives default; anything else ValueError."""
    if value is None or not value.strip():
        return default
    word = value.strip().lower()
    if word in _TRUE:
        return True
    if word in _FALSE:
        return False
    raise ValueError(f"expected true or false, got {value!r}")


def parse_tools(value: str | None) -> list[str]:
    """The tools: line: a list, all builtin tools when it is missing or empty, none for "none" (an agent that
    only talks, such as a persona in a chat without a repository)."""
    if value is not None and value.strip().lower() == "none":
        return []
    return split_list(value) or list(DEFAULT_TOOLS)


def parse_agent_file(path: str | Path) -> AgentDef:
    """Read one agent file. Name defaults to the file stem, model to "opus", tools to all builtin tools
    ("tools: none" for none)."""
    path = Path(path)
    try:
        text = read_small_text(path, MAX_FILE_BYTES)
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"{path}: cannot read agent file: {exc}") from exc
    meta, body = parse_frontmatter(text, path)
    name = meta.get("name") or path.stem
    if not AGENT_NAME_RE.fullmatch(name):
        raise ValueError(f"{path}: invalid agent name {name!r} (use letters, digits, '.', '_', '-')")
    model = meta.get("model") or "opus"
    if model == "inherit":
        model = "opus"
    return AgentDef(
        name=name,
        description=meta.get("description", ""),
        tools=parse_tools(meta.get("tools")),
        model=model,
        skills=split_list(meta.get("skills")),
        mcp=split_list(meta.get("mcp")),
        color=meta.get("color") or None,
        body=body,
        path=path,
        meta=meta,
        browser=(meta.get("browser") or "").strip() or None,
    )


def discover_agents(dirs: list[Path]) -> dict[str, AgentDef]:
    """Parse every *.md in each dir (missing dirs skipped); a later dir's agent replaces an earlier one.

    README.md and dotfiles are skipped. Two files in the same dir defining one name is a ValueError.
    """
    agents: dict[str, AgentDef] = {}
    for directory in dirs:
        directory = Path(directory)
        if not directory.is_dir():
            continue
        defined_here: dict[str, Path] = {}
        for path in sorted(directory.glob("*.md")):
            if path.name.startswith(".") or path.name.lower() == "readme.md" or not path.is_file():
                continue
            agent = parse_agent_file(path)
            if agent.browser is not None and directory.name == "agents" and directory.parent.name == ".factory":
                raise ValueError(f"{path}: a browser: line is refused in a repository's agent file (repository content "
                                 f"is untrusted, DESIGN §17): browsing agents live in the project or user agent "
                                 f"directory")
            if agent.name in defined_here:
                raise ValueError(f"{path}: agent {agent.name!r} is already defined in {defined_here[agent.name]}")
            defined_here[agent.name] = path
            agents[agent.name] = agent
    return agents


def required_agents(reviewers: list[str]) -> list[str]:
    """Agent names the factory line needs: spec-writer, builder, approver, then reviewer-<r> per reviewer."""
    return [*REQUIRED_AGENTS, *(f"reviewer-{reviewer}" for reviewer in reviewers)]
