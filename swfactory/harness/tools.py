"""The builtin harness's tools: Read Write Edit Glob Grep Bash, Skill, Items and MCP adapters.

Every file path is resolved (symlinks followed) and checked against the
policy's roots before it is touched. Tools never raise to the agent loop:
Toolset.call() turns every failure into an (error message, True) result the
model can act on, and truncates every output to the policy's budget.

Write and Edit also refuse any path with a component named ``.git`` (as given or
once resolved), even inside a write root: a linked worktree's ``.git`` pointer
file and a repository's ``.git`` directory (Chat repo mode works in the checkout
itself) are git metadata that host-side git trusts. Agents change a repository
only through git commands. Bash is not covered by these checks: a command can
write anywhere the server's Unix user can, ``.git`` included, unless
``harness.sandbox`` is ``bwrap`` (which narrows the writable git state to what a
commit needs; see loop.sandbox_writable) or the server runs as a dedicated user.

Items is the run's view of its scope's tracked items (DESIGN.md section 32): it never
writes a ledger, only the run's journal, which the scope's manager applies after the
run (an add's id is reserved through the scope's lock, so it is real at once). It is
offered whenever the run has an items context, whatever the agent's ``tools:`` list
says (an agent file written before the tracker still gets the contract), and never
without one.

An MCP server's ``write_args`` (config) name the arguments of its tools that give
a file the tool writes: each gets Write's checks and is passed on resolved, with
missing parent directories created, so a refused path never reaches the server.
"""

from __future__ import annotations

import contextlib
import copy
import fnmatch
import io
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, Callable, Iterable, Iterator

from .sandbox import Sandbox

if TYPE_CHECKING:
    from ..items import ItemsContext
    from ..skills import SkillIndex
    from .mcp import McpClient

BUILTIN_TOOLS = ("Read", "Write", "Edit", "Glob", "Grep", "Bash", "Skill", "Items")
# The check-in toolset (Wake & Messages §5.8; a read-only wake, decision c): no write, send or shell tool.
CHECKIN_TOOLS = ("Read", "Glob", "Grep")
RUN_CHECK_TIMEOUT = 120.0
SHELL_SYNTAX = frozenset(";|&$`<>\n\r\0")
REFUSED_OPTIONS = ("--output", "-o", "--exec", "--upload-pack", "--receive-pack", "--ext-diff", "--textconv",
                   "--web", "-w", "--config", "-c")
WAKE_TOOL_TEXT = (
    "Ask Software Factory to wake this chat later, so you can check on something such as a CI run or a long build, "
    "instead of waiting in Bash. schedule returns an id and the time it was set to; the time may be moved to fit "
    "the owner's power-off and quiet periods. The check-in turn can only {can}; it reports what it found, and the "
    "owner continues from there. list shows this chat's pending wakes; cancel removes one. You cannot message the "
    "owner or wake anything else, and the owner sees every wake.")
DEFAULT_CHECKIN_COMMANDS = (("gh", "run", "view"), ("gh", "run", "list"), ("gh", "pr", "checks"), ("git", "status"),
                            ("git", "log"))


def wake_tool_description(commands: Iterable[Iterable[str]] | None = None) -> str:
    """The Wake tool's words, with what a check-in turn can do from its command list (wakes.chat_wake_commands;
    None: the default list): an empty list says it only reads files and runs no commands."""
    listed = [" ".join(map(str, command)) for command in (DEFAULT_CHECKIN_COMMANDS if commands is None else commands)]
    can = (f"read files and run a few status commands ({', '.join(listed)})" if listed
           else "read files; it runs no commands")
    return WAKE_TOOL_TEXT.format(can=can)


WAKE_TOOL_DESCRIPTION = wake_tool_description()
WAKE_TOOL_SCHEMA = {"type": "object", "properties": {
    "action": {"type": "string", "enum": ["schedule", "list", "cancel"]},
    "in_minutes": {"type": "integer", "description": "schedule: minutes from now, 10 to 10080 (7 days)."},
    "note": {"type": "string", "description": "schedule: what to check, at most 300 characters. It comes back to the "
                                              "check-in as a reminder, not as an instruction."},
    "id": {"type": "string", "description": "cancel: the wake id, e.g. W-000231."}},
    "required": ["action"]}
READ_DEFAULT_LIMIT = 2000
MAX_LINE_CHARS = 2000
MAX_LINE_BYTES = MAX_LINE_CHARS * 4       # enough UTF-8 for MAX_LINE_CHARS characters
COUNT_CHUNK_BYTES = 1 << 20
BINARY_SNIFF_BYTES = 8192
GLOB_MAX_RESULTS = 500
GREP_DEFAULT_HEAD_LIMIT = 250
GREP_TIMEOUT_SECONDS = 120
GREP_MATCH_LINE_BYTES = 8 << 20           # Python fallback: longer lines are searched in their first 8 MiB only
GREP_STDERR_CHARS = 4000
GREP_OUTPUT_MODES = ("files_with_matches", "content", "count")
BASH_MAX_TIMEOUT = 1800
SKILL_MAX_FILES = 50
SKIP_DIRS = frozenset({".git", "node_modules"})
MAX_TOOL_NAME = 64
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9_-]")
_GLOB_CHARS = frozenset("*?[{")


class ToolError(Exception):
    """A tool failure the model should see: bad arguments, a policy violation, a missing file."""


@dataclass
class ToolPolicy:
    """What the tools may touch. write_paths: a directory allows anything below it, a file exactly itself."""

    cwd: Path
    read_roots: list[Path]
    write_paths: list[Path]
    bash_timeout: float = 600.0
    output_chars: int = 30000
    sandbox: Sandbox | None = None             # None: a plain sandbox in cwd is created on first Bash
    cancel: threading.Event | None = field(default=None, repr=False)   # aborts a running Bash command
    # Never read, even inside a readable root (a read-only wake's tools, Wake & Messages decision c: the data and
    # config dirs, the sensitive-terms file, the agent sockets). Glob skips them; Grep refuses a target holding one.
    denied: list[Path] = field(default_factory=list)
    # Read only through record (a read-only wake's view of the persona's own memory with the read ledger on, DESIGN
    # §35): a Read below one of these gives its bytes (at most record_bytes) to record before it shows them, and is
    # refused when record fails; Grep refuses them (a search cannot be recorded); Glob lists names only.
    recorded: list[Path] = field(default_factory=list)
    record: Callable[[Path, bytes, int, float], None] | None = field(default=None, repr=False)
    record_bytes: int = 64 * 1024


def truncate_output(text: str, limit: int) -> str:
    """Keep the first 60% and the last 40% of limit chars with a marker between; limit <= 0 keeps all."""
    if limit <= 0 or len(text) <= limit:
        return text
    head = limit * 3 // 5
    tail = limit - head
    dropped = len(text) - limit
    return f"{text[:head]}\n… [{dropped} chars truncated] …\n{text[len(text) - tail:]}"


def _schema(description: str, properties: dict, required: list[str]) -> dict:
    return {"description": description,
            "input_schema": {"type": "object", "properties": properties, "required": required}}


def _builtin_schemas(bash_timeout: float) -> dict[str, dict]:
    path_note = "Paths may be absolute or relative to the working directory."
    return {
        "Read": _schema(
            "Read a text file. Output is `cat -n` style: the line number, a tab, then the line. Reads up to "
            f"{READ_DEFAULT_LIMIT} lines starting at `offset` (a 1-based line number); page through long files "
            f"with offset and limit. Lines longer than {MAX_LINE_CHARS} characters are cut. Binary files are "
            f"refused. {path_note}",
            {"file_path": {"type": "string", "description": "The file to read."},
             "offset": {"type": "integer", "minimum": 1,
                        "description": "1-based line number to start reading at (default 1)."},
             "limit": {"type": "integer", "minimum": 1,
                       "description": f"Maximum number of lines to return (default {READ_DEFAULT_LIMIT})."}},
            ["file_path"]),
        "Write": _schema(
            "Create a file or overwrite it completely with `content`; missing parent directories are created. "
            "Only paths the write policy allows can be written, never anything in or named .git. To change "
            f"part of an existing file use Edit. {path_note}",
            {"file_path": {"type": "string", "description": "The file to write."},
             "content": {"type": "string", "description": "The complete new content of the file."}},
            ["file_path", "content"]),
        "Edit": _schema(
            "Replace an exact piece of text in a file. `old_string` must match the file exactly (whitespace "
            "and indentation included, without Read's line-number prefix) and must be unique unless "
            "`replace_all` is true; include surrounding lines to make it unique. Read the file first. "
            f"{path_note}",
            {"file_path": {"type": "string", "description": "The file to edit."},
             "old_string": {"type": "string", "description": "The exact text to replace."},
             "new_string": {"type": "string", "description": "The replacement text (must differ)."},
             "replace_all": {"type": "boolean",
                             "description": "Replace every occurrence instead of exactly one (default false)."}},
            ["file_path", "old_string", "new_string"]),
        "Glob": _schema(
            "Find files by name pattern, e.g. `**/*.py`, `src/**/*.{ts,tsx}` or `*.md`. `*` and `?` match "
            "within one path segment and `**` matches any number of directories. Returns absolute paths, "
            f"newest first (at most {GLOB_MAX_RESULTS}). Skips .git and node_modules.",
            {"pattern": {"type": "string", "description": "Glob pattern, relative to `path`."},
             "path": {"type": "string",
                      "description": "Directory to search in (default: the working directory)."}},
            ["pattern"]),
        "Grep": _schema(
            "Search file contents with a regular expression (ripgrep syntax). Output modes: "
            "`files_with_matches` (default; matching file paths, newest first), `content` (matching lines as "
            "path:line:text, optionally with context lines) and `count` (path:number of matching lines). "
            f"Searches hidden files but skips .git. Results are capped by head_limit (default "
            f"{GREP_DEFAULT_HEAD_LIMIT}; 0 = no cap) and by the output size limit; a note then gives the "
            "full count.",
            {"pattern": {"type": "string", "description": "Regular expression to search for."},
             "path": {"type": "string",
                      "description": "File or directory to search (default: the working directory)."},
             "glob": {"type": "string", "description": "Only search files matching this glob, e.g. `*.py`."},
             "output_mode": {"type": "string", "enum": list(GREP_OUTPUT_MODES),
                             "description": "What to return (default files_with_matches)."},
             "-i": {"type": "boolean", "description": "Case-insensitive search."},
             "-n": {"type": "boolean", "description": "Show line numbers in content mode (default true)."},
             "-C": {"type": "integer", "minimum": 0,
                    "description": "Lines of context before and after each match (content mode)."},
             "-A": {"type": "integer", "minimum": 0, "description": "Lines of context after each match."},
             "-B": {"type": "integer", "minimum": 0, "description": "Lines of context before each match."},
             "head_limit": {"type": "integer", "minimum": 0,
                            "description": "Maximum lines (content) or entries (other modes) to return."}},
            ["pattern"]),
        "Bash": _schema(
            "Run a command with bash in the working directory and return its combined stdout and stderr "
            "(`[exit code N]` is appended when it fails). Non-interactive: stdin is closed, so never run "
            "anything that waits for input. Start long-running processes such as dev servers in the "
            "background with `&` and redirect their output to a file; they are stopped when the run ends. "
            f"Timeout in seconds: default {bash_timeout:g}, max {BASH_MAX_TIMEOUT}.",
            {"command": {"type": "string", "description": "The bash command to run."},
             "timeout": {"type": "number", "exclusiveMinimum": 0,
                         "description": f"Timeout in seconds (max {BASH_MAX_TIMEOUT})."},
             "description": {"type": "string", "description": "A few words on what the command does."}},
            ["command"]),
        "Skill": _schema(
            "Load a skill by name (see the skills listed in the system prompt). Returns the skill's "
            "instructions and the absolute paths of its other files, which you can then Read.",
            {"name": {"type": "string", "description": "The skill's name."}},
            ["name"]),
        "Items": _items_schema(),
    }


def _items_schema() -> dict:
    from ..items import ITEMS_TOOL_DESCRIPTION, ITEMS_TOOL_SCHEMA

    return {"description": ITEMS_TOOL_DESCRIPTION, "input_schema": copy.deepcopy(ITEMS_TOOL_SCHEMA)}


class Toolset:
    """The tools one agent may call, bound to a ToolPolicy."""

    def __init__(self, names: list[str], policy: ToolPolicy, skills: "SkillIndex | None" = None,
                 mcp_clients: "dict[str, McpClient] | None" = None, items: "ItemsContext | None" = None,
                 wake: Any = None, checkin_commands: list[list[str]] | None = None,
                 jobs: Callable[[], str] | None = None) -> None:
        """wake: a chat turn's WakeContext (Wake & Messages §5.8): registers Wake, whatever the agent's tools:
        list says. checkin_commands: the check-in toolset: only Read, Glob, Grep, Wake (with a context) and
        RunCheck, which runs an argv starting with one of these commands, no shell, in the policy's sandbox; no
        Write, Edit, Bash, Skill or MCP tool. An empty list keeps that toolset without RunCheck (no command can
        run, so the tool is not offered). jobs: a read-only wake's Jobs tool (the factory's jobs by ref)."""
        self.policy = policy
        self.skills = skills
        self.items = items
        self.wake = wake
        self.checkin_commands = [list(map(str, command)) for command in checkin_commands or []
                                 if isinstance(command, (list, tuple)) and command]
        self.jobs = jobs
        if checkin_commands is not None:
            names = list(CHECKIN_TOOLS)
            mcp_clients = {}
            skills = None
            self.skills = None
        self._sandbox = policy.sandbox
        self._mcp_clients = dict(mcp_clients or {})
        self._skill_roots: list[Path] = []
        self._schemas: list[dict] = []
        self._handlers: dict[str, Callable[[dict], str]] = {}
        builtin = _builtin_schemas(policy.bash_timeout)
        canonical = {tool.lower(): tool for tool in BUILTIN_TOOLS}
        names = list(names or ())
        # an operator persona's read-only view of her items (items.ItemsView, DESIGN §36) offers no tool
        tool_items = items if items is not None and getattr(items, "offers_tool", True) else None
        if tool_items is not None and not any(str(raw).strip().lower() == "items" for raw in names):
            names.append("Items")   # the contract lives in the harness, whatever the agent's tools: list says
        for raw in names:
            name = canonical.get(str(raw).strip().lower())
            if name is None or (name == "Skill" and skills is None) or (name == "Items" and tool_items is None):
                continue
            self._add(name, builtin[name]["description"], builtin[name]["input_schema"],
                      getattr(self, f"_tool_{name.lower()}"))
        for server, client in self._mcp_clients.items():
            self._add_mcp_tools(server, client)
        if wake is not None:
            said = checkin_commands if checkin_commands is not None else getattr(wake, "commands", None)
            self._add("Wake", wake_tool_description(said), copy.deepcopy(WAKE_TOOL_SCHEMA), self._tool_wake)
        if checkin_commands is not None and self.checkin_commands:
            allowed = "; ".join(" ".join(command) for command in self.checkin_commands)
            self._add("RunCheck", "Run one allowed status command and return its output (no shell: give the argv as a "
                      f"list of strings). Allowed commands start with: {allowed}. Nothing can be written.",
                      {"type": "object", "properties": {"argv": {"type": "array", "items": {"type": "string"},
                                                                 "description": "The command and its arguments."}},
                       "required": ["argv"]}, self._tool_run_check)
        if jobs is not None:
            self._add("Jobs", "List the factory's jobs by reference and stage (read-only).",
                      {"type": "object", "properties": {}}, lambda args: self.jobs())

    # -- public API -------------------------------------------------------------

    def schemas(self) -> list[dict]:
        """Tool definitions in neutral form: [{name, description, input_schema}]."""
        return copy.deepcopy(self._schemas)

    def call(self, name: str, arguments: dict) -> tuple[str, bool]:
        """Run a tool; returns (output, is_error). Never raises."""
        handler = self._handlers.get(name)
        if handler is None:
            available = ", ".join(self._handlers) or "none"
            return f"Unknown tool {name!r}. Available tools: {available}.", True
        if not isinstance(arguments, dict):
            return f"Invalid arguments for {name}: expected a JSON object.", True
        try:
            output, is_error = handler(arguments), False
        except ToolError as exc:
            output, is_error = str(exc), True
        except Exception as exc:   # a tool bug or OS error must not kill the agent loop
            output, is_error = f"{name} failed: {type(exc).__name__}: {exc}", True
        return truncate_output(output, self.policy.output_chars), is_error

    def close(self) -> None:
        """Kill background processes started by Bash and close the MCP clients."""
        if self._sandbox is not None:
            self._sandbox.cleanup()
        for client in self._mcp_clients.values():
            try:
                client.close()
            except Exception:
                pass

    # -- registration -----------------------------------------------------------

    def _add(self, name: str, description: str, input_schema: dict, handler: Callable[[dict], str]) -> None:
        if name in self._handlers:
            return
        self._handlers[name] = handler
        self._schemas.append({"name": name, "description": description, "input_schema": input_schema})

    def _add_mcp_tools(self, server: str, client: "McpClient") -> None:
        allowed = getattr(getattr(client, "cfg", None), "tools", None)
        try:
            tools = client.tools()
        except Exception:   # a server that failed to start offers no tools
            return
        for tool in tools:
            tool_name = str(tool.get("name") or "") if isinstance(tool, dict) else ""
            if not tool_name or (allowed is not None and tool_name not in allowed):
                continue
            schema = tool.get("inputSchema")
            description = f"[MCP {server}] {tool.get('description') or ''}".rstrip()
            self._add(mcp_tool_name(server, tool_name), description,
                      schema if isinstance(schema, dict) and schema else {"type": "object"},
                      self._mcp_handler(client, tool_name))

    def _mcp_handler(self, client: "McpClient", tool: str) -> Callable[[dict], str]:
        write_args = (getattr(getattr(client, "cfg", None), "write_args", None) or {}).get(tool) or []

        def handler(arguments: dict) -> str:
            if write_args:
                arguments = self._checked_write_args(tool, arguments, write_args)
            text, is_error = client.call(tool, arguments)
            if is_error:
                raise ToolError(text or f"MCP tool {tool} failed")
            return text or "(no output)"
        return handler

    def _checked_write_args(self, tool: str, arguments: dict, names: Iterable[str]) -> dict:
        """arguments with every write argument checked like Write's file_path and replaced by its resolved
        absolute path; missing parent directories are created. A refused path never reaches the server."""
        checked = dict(arguments)
        for key in names:
            value = checked.get(key)
            if value is None:
                continue   # the server decides whether it is required
            if not isinstance(value, str) or not value.strip():
                raise ToolError(f"Invalid arguments for {tool}: '{key}' must be a file path string.")
            path = self._writable_path(checked, key)
            if not path.parent.exists():
                self._check_write(path.parent)
                path.parent.mkdir(parents=True, exist_ok=True)
                if self._resolve(str(path)) != path:   # a component was swapped for a symlink meanwhile
                    raise ToolError(f"Access denied: {path} changed while it was checked; try again.")
            checked[key] = str(path)
        return checked

    # -- policy -------------------------------------------------------------------

    def _resolve(self, raw: str) -> Path:
        path = Path(os.path.expanduser(raw))
        if not path.is_absolute():
            path = Path(self.policy.cwd) / path
        return path.resolve()

    def _readable_roots(self) -> list[Path]:
        roots = [self.policy.cwd, *self.policy.read_roots, *self.policy.write_paths, *self._skill_roots]
        resolved: list[Path] = []
        for root in roots:
            root = Path(root).resolve()
            if root not in resolved:
                resolved.append(root)
        return resolved

    def _check_read(self, path: Path) -> None:
        roots = self._readable_roots()
        if not any(_within(path, root) for root in roots):
            raise ToolError(f"Access denied: {path} is outside the readable roots "
                            f"({', '.join(map(str, roots))}).")
        if self._denied(path):
            raise ToolError(f"Access denied: {path} is never read by this run.")

    def _denied(self, path: Path) -> bool:
        return any(_within(path, Path(root).resolve()) for root in self.policy.denied)

    def _holds_denied(self, path: Path) -> bool:
        """Whether a directory a search would walk contains a path that is never read."""
        return any(_within(Path(root).resolve(), path) for root in self.policy.denied)

    def _recorded(self, path: Path) -> bool:
        """Whether path is (or is below) one the harness records every read of (ToolPolicy.recorded)."""
        return any(_within(path, Path(root).resolve()) for root in self.policy.recorded)

    def _recorded_read(self, path: Path) -> tuple[bytes, str]:
        """(the bytes a Read shows, a footer) for a file whose reads are recorded: at most record_bytes, opened
        with O_NOFOLLOW and checked with fstat, given to record before anything is shown; a read that cannot be
        recorded shows nothing."""
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        except OSError as exc:
            raise ToolError(f"{path} cannot be read ({exc.strerror}).") from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ToolError(f"{path} is not a regular file.")
            chunks, left = [], self.policy.record_bytes
            while left > 0:
                chunk = os.read(fd, left)
                if not chunk:
                    break
                chunks.append(chunk)
                left -= len(chunk)
            data = b"".join(chunks)
        finally:
            os.close(fd)
        if b"\0" in data[:BINARY_SNIFF_BYTES]:
            raise ToolError(f"{path} looks like a binary file; Read only shows text files.")
        try:
            self.policy.record(path, data, info.st_size, info.st_mtime)   # type: ignore[misc]
        except Exception:   # a full disk, a file the ledger cannot write: the read does not happen
            raise ToolError(f"{path} is in the persona's own memory, and the harness records every read there; this "
                            "read could not be recorded, so nothing was read.") from None
        size = f"{len(data) // 1024} KB" if len(data) >= 1024 else f"{len(data)} bytes"
        footer = (f"\n\n(the harness records reads here and read only the first {size} of this file)"
                  if len(data) < info.st_size else "")
        return data, footer

    def _writable_path(self, args: dict, key: str) -> Path:
        """The resolved path of args[key] for Write/Edit, after the .git and write-policy checks."""
        path = self._path(args, key)
        raw = Path(os.path.expanduser(str(args.get(key))))
        given = raw if raw.is_absolute() else Path(self.policy.cwd) / raw
        if _has_git_component(given) or _has_git_component(path):
            raise ToolError(f"Access denied: {path} is git metadata (a path component is named .git: a "
                            "worktree's .git pointer file or a repository's .git directory). Write and Edit "
                            "never change it, even inside a writable root; change the repository with git "
                            "commands instead.")
        self._check_write(path)
        return path

    def _check_write(self, path: Path) -> None:
        for raw in self.policy.write_paths:
            allowed = Path(raw).resolve()
            if path == allowed or (allowed.is_dir() and allowed in path.parents):
                return
        raise ToolError(f"Access denied: {path} is not writable by this agent. "
                        f"You may write: {self._describe_writable()}.")

    def _describe_writable(self) -> str:
        parts = []
        for raw in self.policy.write_paths:
            path = Path(raw).resolve()
            parts.append(f"{path}/ (anything below it)" if path.is_dir() else str(path))
        return "; ".join(parts) or "nothing (this agent is read-only)"

    def _output_budget(self) -> int | None:
        """Chars a listing may fill, leaving room for its footer note; None when output is unlimited."""
        return self.policy.output_chars - 200 if self.policy.output_chars > 0 else None

    def _path(self, args: dict, key: str, default: Path | None = None) -> Path:
        value = args.get(key)
        if value is None or value == "":
            if default is None:
                raise ToolError(f"Invalid arguments: '{key}' is required (a file path string).")
            return Path(default).resolve()
        if not isinstance(value, str):
            raise ToolError(f"Invalid arguments: '{key}' must be a string path.")
        return self._resolve(value)

    # -- tools ----------------------------------------------------------------------

    def _tool_read(self, args: dict) -> str:
        path = self._path(args, "file_path")
        self._check_read(path)
        offset = max(_int_arg(args, "offset", 1, minimum=0), 1)
        limit = _int_arg(args, "limit", READ_DEFAULT_LIMIT, minimum=1)
        if path.is_dir():
            raise ToolError(f"{path} is a directory; use Glob to list its files.")
        if not path.exists():
            raise ToolError(f"File not found: {path}")
        if not path.is_file():
            raise ToolError(f"{path} is not a regular file.")
        footer = ""
        if self.policy.record is not None and self._recorded(path):
            data, footer = self._recorded_read(path)
            opened: Any = contextlib.nullcontext(io.BytesIO(data))
        else:
            opened = open(path, "rb")
        budget = self._output_budget()
        shown: list[str] = []
        used = total = 0
        with opened as fh:
            if b"\0" in fh.read(BINARY_SNIFF_BYTES):
                raise ToolError(f"{path} looks like a binary file; Read only shows text files.")
            fh.seek(0)
            for raw in _bounded_lines(fh, MAX_LINE_BYTES):
                total += 1
                if total < offset:
                    continue
                line = _numbered_line(total, raw)
                if budget is not None and shown and used + len(line) + 1 > budget:
                    break
                shown.append(line)
                used += len(line) + 1
                if len(shown) == limit:
                    break
            total += _count_lines(fh)
        if total == 0:
            return "(empty file)" + footer
        if offset > total:
            return f"(file has {total} lines; offset {offset} is past the end)" + footer
        last = offset + len(shown) - 1
        text = "\n".join(shown)
        if offset > 1 or last < total:
            text += f"\n\n(file has {total} lines; showing {offset}-{last})"
        return text + footer

    def _tool_write(self, args: dict) -> str:
        path = self._writable_path(args, "file_path")
        content = _str_arg(args, "content")
        if path.is_dir():
            raise ToolError(f"{path} is a directory; give a file path.")
        if path.exists() and not path.is_file():
            raise ToolError(f"{path} is not a regular file.")
        data = content.encode("utf-8", errors="replace")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return f"Wrote {len(data)} bytes to {path}"

    def _tool_edit(self, args: dict) -> str:
        path = self._writable_path(args, "file_path")
        old = _str_arg(args, "old_string")
        new = _str_arg(args, "new_string")
        replace_all = _bool_arg(args, "replace_all", False)
        if not path.is_file():
            raise ToolError(f"File not found: {path}. Use Write to create a new file.")
        if old == "":
            raise ToolError("old_string is empty. Give the exact text to replace, or use Write for a whole file.")
        if old == new:
            raise ToolError("old_string and new_string are identical; there is nothing to change.")
        text = path.read_bytes().decode("utf-8", errors="surrogateescape")
        count = text.count(old)
        if count == 0 and "\r\n" in text and "\n" in old and "\r\n" not in old:
            old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")   # CRLF file, LF text from Read
            count = text.count(old)
        if count == 0:
            raise ToolError(f"old_string was not found in {path}. Re-read the file with Read and copy the "
                            "exact text, including whitespace and indentation (without the line-number prefix).")
        if count > 1 and not replace_all:
            raise ToolError(f"old_string matches {count} times in {path}. Include more surrounding lines to "
                            "make it unique, or set replace_all to true to replace every occurrence.")
        replaced = count if replace_all else 1
        path.write_bytes(text.replace(old, new, replaced).encode("utf-8", errors="surrogateescape"))
        return f"Edited {path}: {replaced} replacement{'s' if replaced != 1 else ''}."

    def _tool_glob(self, args: dict) -> str:
        pattern = _str_arg(args, "pattern", nonempty=True)
        base = self._path(args, "path", default=self.policy.cwd)
        prefix, rest = _split_glob(pattern)
        if prefix:
            base = (base / prefix).resolve()
        self._check_read(base)
        if not rest:   # no wildcards: the pattern names one path
            return str(base) if base.is_file() else "(no matches)"
        if not base.is_dir():
            raise ToolError(f"Not a directory: {base}")
        patterns = [p.split("/") for p in expand_braces(rest)]
        max_depth = None if any("**" in parts for parts in patterns) else max(len(p) for p in patterns) - 1
        matches: list[str] = []
        for dirpath, dirnames, filenames in os.walk(base):
            rel_dir = Path(dirpath).relative_to(base).parts
            if max_depth is not None and len(rel_dir) >= max_depth:
                dirnames.clear()
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS
                           and not (self.policy.denied and self._denied(Path(dirpath, d).resolve()))]
            for name in filenames:
                if any(glob_match(parts, (*rel_dir, name)) for parts in patterns):
                    matches.append(os.path.join(dirpath, name))
        if not matches:
            return "(no matches)"
        matches = _newest_first(matches)
        text = "\n".join(matches[:GLOB_MAX_RESULTS])
        if len(matches) > GLOB_MAX_RESULTS:
            text += (f"\n(truncated: showing {GLOB_MAX_RESULTS} of {len(matches)} matches; "
                     "use a narrower pattern or path)")
        return text

    def _tool_grep(self, args: dict) -> str:
        pattern = _str_arg(args, "pattern", nonempty=True)
        target = self._path(args, "path", default=self.policy.cwd)
        self._check_read(target)
        if not target.exists():
            raise ToolError(f"Path not found: {target}")
        if self.policy.denied and target.is_dir() and self._holds_denied(target):
            raise ToolError(f"Access denied: {target} contains a folder this run never reads; search a narrower path.")
        if self.policy.recorded and (self._recorded(target) or (target.is_dir() and any(
                _within(Path(root).resolve(), target) for root in self.policy.recorded))):
            raise ToolError(f"Grep does not search {target}: the harness records every read of the persona's own "
                            "memory there, and a search cannot be recorded. List its files with Glob and Read the ones "
                            "you need (or search a narrower path).")
        mode = args.get("output_mode") or "files_with_matches"
        if mode not in GREP_OUTPUT_MODES:
            raise ToolError(f"Invalid arguments: output_mode must be one of {', '.join(GREP_OUTPUT_MODES)}.")
        glob = args.get("glob") or None
        if glob is not None and not isinstance(glob, str):
            raise ToolError("Invalid arguments: 'glob' must be a string.")
        context = _int_arg(args, "-C", 0, minimum=0)
        opts = GrepOptions(
            pattern=pattern, target=target, mode=mode, glob=glob,
            ignore_case=_bool_arg(args, "-i", False), line_numbers=_bool_arg(args, "-n", True),
            before=_int_arg(args, "-B", context, minimum=0), after=_int_arg(args, "-A", context, minimum=0),
        )
        head_limit = _int_arg(args, "head_limit", GREP_DEFAULT_HEAD_LIMIT, minimum=0)
        rg = _rg_executable()
        lines: Iterable[str] = grep_with_rg(rg, opts) if rg else grep_with_python(opts)
        if mode == "files_with_matches":
            lines = _newest_first(list(lines))   # sorting needs every path; paths are short
        shown, total = _leading_lines((_clip(line) for line in lines), head_limit or None, self._output_budget())
        if not total:
            return "(no matches)"
        text = "\n".join(shown)
        if len(shown) < total:
            unit = "lines" if mode == "content" else "entries"
            reason = "; raise head_limit or" if len(shown) == head_limit else ", all that fit the output limit;"
            text += f"\n\n(showing the first {len(shown)} of {total} {unit}{reason} narrow the pattern, path or glob)"
        return text

    def _tool_bash(self, args: dict) -> str:
        command = _str_arg(args, "command", nonempty=True)
        requested = _number_arg(args, "timeout")
        timeout = min(requested or self.policy.bash_timeout, BASH_MAX_TIMEOUT)
        if self._sandbox is None:
            self._sandbox = Sandbox("none", Path(self.policy.cwd), list(self.policy.write_paths), True, {})
        exit_code, output, timed_out = self._sandbox.run(command, timeout, self.policy.cancel)
        text = output.rstrip("\n")
        if timed_out:   # killed part way: what it changed is unknown, never "failed" (DESIGN §40)
            footer = (f"[timed out after {timeout:g}s and stopped: what it did before that is unknown, so check "
                      f"before running it again]")
        elif exit_code is None:   # stopped by a cancel or the run's time limit: the same unknown, in its words
            footer = ("[stopped part way (cancelled, or the run's time limit): what it did before that is unknown, "
                      "so check before running it again]")
        elif exit_code != 0:
            footer = f"[exit code {exit_code}]"
        else:
            return text or "(no output)"
        raise ToolError(f"{text}\n{footer}" if text else footer)

    def _tool_wake(self, args: dict) -> str:
        """Wake: schedule, list or cancel this chat's check-ins (Wake & Messages §5.8). A scheduled wake opens a
        tracker item through the run's items context: a commitment due at the wake, closed by code when it runs."""
        import json as _json

        action = args.get("action")
        try:
            if action == "schedule":
                result = self.wake.schedule(args.get("in_minutes"), args.get("note"))
                if self.items is not None:
                    try:
                        item = self.items.open_wake(f"Scheduled check-in {result['id']} at {result['at']}",
                                                    str(result.get("at") or ""), result["id"], taint=("untrusted",))
                        self.wake.attach(result["id"], item)
                        result = {**result, "item": item}
                    except Exception:   # noqa: BLE001 - the wake stands; the tracker says why in its own log
                        pass
                return _json.dumps(result)
            if action == "list":
                return _json.dumps({"wakes": self.wake.list()})
            if action == "cancel":
                wake_id = args.get("id")
                if not isinstance(wake_id, str):
                    raise ToolError("cancel needs the wake id, e.g. W-000231")
                result = self.wake.cancel(wake_id)
                if self.items is not None and result.get("item"):
                    try:
                        self.items.cancel_wake(result["item"], "the agent cancelled its wake")
                    except Exception:   # noqa: BLE001
                        pass
                return _json.dumps(result)
        except ValueError as exc:
            raise ToolError(str(exc)) from None
        raise ToolError("action must be schedule, list or cancel")

    def _tool_run_check(self, args: dict) -> str:
        """RunCheck: an argv (no shell) that starts with one of the allowed commands, in the policy's sandbox with
        no writable path; shell syntax and options that write or run other programs are refused."""
        argv = args.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
            raise ToolError("argv must be a non-empty list of strings")
        if any(set(arg) & SHELL_SYNTAX for arg in argv):
            raise ToolError("RunCheck runs no shell: remove ; | & $ ` < > and line breaks")
        if any(arg == option or arg.startswith(option + "=") or (option.startswith("--") and arg.startswith(option))
               for arg in argv[1:] for option in REFUSED_OPTIONS):
            raise ToolError("that option could write or run something: RunCheck only reads")
        if not any(argv[:len(command)] == command for command in self.checkin_commands):
            allowed = "; ".join(" ".join(command) for command in self.checkin_commands) or "none"
            raise ToolError(f"not an allowed status command (allowed: {allowed})")
        if self._sandbox is None:
            self._sandbox = Sandbox("none", Path(self.policy.cwd), [], False, {})
        exit_code, output, timed_out = self._sandbox.run_argv(argv, RUN_CHECK_TIMEOUT, self.policy.cancel)
        text = output.rstrip("\n")
        if timed_out:
            raise ToolError(f"{text}\n[timed out after {RUN_CHECK_TIMEOUT:g}s]")
        if exit_code is None:
            raise ToolError(f"{text}\n[cancelled]")
        if exit_code != 0:
            raise ToolError(f"{text}\n[exit code {exit_code}]")
        return text or "(no output)"

    def _tool_items(self, args: dict) -> str:
        from ..items import ItemError

        try:
            return self.items.call(args)
        except ItemError as exc:
            raise ToolError(str(exc)) from None

    def _tool_skill(self, args: dict) -> str:
        name = _str_arg(args, "name", nonempty=True).strip()
        skill = self.skills.get(name)
        if skill is None:
            available = [s.name for s in self.skills.list() if s.model_invocable]
            raise ToolError(f"Unknown skill {name!r}. Available skills: {', '.join(available) or 'none'}.")
        if not skill.model_invocable:
            raise ToolError(f"Skill {name!r} cannot be loaded with the Skill tool (disable-model-invocation).")
        skill_dir = Path(skill.dir).resolve()
        if skill_dir not in self._skill_roots:
            self._skill_roots.append(skill_dir)
        text = skill.body.rstrip()
        files = _skill_files(skill_dir)
        if files:
            text += "\n\nSkill files:\n" + "\n".join(files)
        return text


# -- Grep backends ----------------------------------------------------------------

@dataclass
class GrepOptions:
    pattern: str
    target: Path
    mode: str
    glob: str | None = None
    ignore_case: bool = False
    line_numbers: bool = True
    before: int = 0
    after: int = 0


def _rg_executable() -> str | None:
    return shutil.which("rg")


def grep_with_rg(rg: str, opts: GrepOptions) -> Iterator[str]:
    """Run ripgrep and yield its output lines as they arrive (always with file names, sorted by path)."""
    argv = [rg, "--no-config", "--no-heading", "--color", "never", "--hidden", "--glob", "!.git",
            "--with-filename", "--sort", "path"]
    if opts.ignore_case:
        argv.append("--ignore-case")
    if opts.mode == "files_with_matches":
        argv.append("--files-with-matches")
    elif opts.mode == "count":
        argv.append("--count")
    else:
        argv.append("--line-number" if opts.line_numbers else "--no-line-number")
        if opts.before:
            argv += ["--before-context", str(opts.before)]
        if opts.after:
            argv += ["--after-context", str(opts.after)]
    if opts.glob:
        argv += ["--glob", opts.glob]
    argv += ["--regexp", opts.pattern, "--", str(opts.target)]
    with tempfile.TemporaryFile() as stderr:   # a file, so a flood of errors can never block rg
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=stderr)
        expired = threading.Event()
        timer = threading.Timer(GREP_TIMEOUT_SECONDS, lambda: (expired.set(), proc.kill()))
        timer.daemon = True
        timer.start()
        produced = False
        try:
            for raw in _bounded_lines(proc.stdout, MAX_LINE_BYTES):
                produced = True
                yield raw.rstrip(b"\n").decode("utf-8", errors="replace")
            proc.wait()
        finally:
            timer.cancel()
            if proc.returncode is None:   # the caller stopped early or failed: stop rg too
                proc.kill()
                proc.wait()
            proc.stdout.close()
        if expired.is_set():
            raise ToolError(f"Grep timed out after {GREP_TIMEOUT_SECONDS}s; narrow the path, glob or pattern.")
        if proc.returncode == 2 and not produced:
            stderr.seek(0)
            message = stderr.read(GREP_STDERR_CHARS).decode("utf-8", errors="replace").strip()
            raise ToolError("Grep failed: " + message)


def grep_with_python(opts: GrepOptions) -> Iterator[str]:
    """Pure-Python Grep yielding the same lines as grep_with_rg (no .gitignore support), file by file."""
    try:
        regex = re.compile(opts.pattern, re.IGNORECASE if opts.ignore_case else 0)
    except re.error as exc:
        raise ToolError(f"Invalid regular expression {opts.pattern!r}: {exc}")
    separate = False   # whether a context group was already printed (for the `--` separators)
    for path in _grep_candidates(opts.target, opts.glob):
        lines = _text_lines(path, GREP_MATCH_LINE_BYTES)
        if opts.mode == "files_with_matches":
            if any(regex.search(line) for line in lines):
                yield str(path)
        elif opts.mode == "count":
            hits = sum(1 for line in lines if regex.search(line))
            if hits:
                yield f"{path}:{hits}"
        else:
            for out in _content_lines(str(path), lines, regex, opts, separate):
                separate = True
                yield out


def _text_lines(path: Path, max_bytes: int) -> Iterator[str]:
    """The decoded lines of a text file, each cut to about max_bytes; nothing for binary or unreadable files."""
    try:
        with open(path, "rb") as fh:
            if b"\0" in fh.read(BINARY_SNIFF_BYTES):
                return
            fh.seek(0)
            for raw in _bounded_lines(fh, max_bytes):
                yield raw.rstrip(b"\n").decode("utf-8", errors="replace")
    except OSError:
        return


def _content_lines(name: str, lines: Iterable[str], regex: re.Pattern, opts: GrepOptions,
                   separate: bool) -> Iterator[str]:
    """rg-style content lines for one file: path:N:text for matches, path-N-text for context and `--`
    before a group that does not join the previous one (separate: an earlier file printed a group)."""
    context = opts.before or opts.after
    pending: deque[tuple[int, str]] = deque(maxlen=opts.before)   # unprinted lines that may become context
    after_left = 0
    last_shown = -1   # index of the last line printed for this file
    for index, text in enumerate(lines):
        if regex.search(text):
            first = pending[0][0] if pending else index
            if context and separate and (last_shown < 0 or first > last_shown + 1):
                yield "--"
            for number, kept in pending:
                yield _grep_line(name, number, "-", kept, opts.line_numbers)
            pending.clear()
            yield _grep_line(name, index, ":", text, opts.line_numbers)
            last_shown, after_left, separate = index, opts.after, True
        elif after_left:
            yield _grep_line(name, index, "-", text, opts.line_numbers)
            last_shown, after_left = index, after_left - 1
        elif opts.before:
            pending.append((index, _clip(text)))


def _grep_line(name: str, index: int, mark: str, text: str, line_numbers: bool) -> str:
    return f"{name}{mark}{index + 1}{mark}{text}" if line_numbers else f"{name}{mark}{text}"


def _leading_lines(lines: Iterable[str], max_lines: int | None,
                   max_chars: int | None) -> tuple[list[str], int]:
    """The longest prefix of lines within max_lines and max_chars (at least one line), and the count of all.

    Lines after the prefix are only counted, so memory stays bounded however much output there is.
    """
    kept: list[str] = []
    used = total = 0
    full = False
    for line in lines:
        total += 1
        if not full and kept and ((max_lines is not None and len(kept) >= max_lines)
                                  or (max_chars is not None and used + len(line) + 1 > max_chars)):
            full = True
        if not full:
            kept.append(line)
            used += len(line) + 1
    return kept, total


def _grep_candidates(target: Path, glob: str | None) -> list[Path]:
    if target.is_file():
        return [target]
    include, exclude = [], []
    if glob:
        negated = glob.startswith("!")
        (exclude if negated else include).extend(expand_braces(glob[1:] if negated else glob))
    found: list[tuple[tuple[str, ...], Path]] = []
    for dirpath, dirnames, filenames in os.walk(target):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        rel_dir = Path(dirpath).relative_to(target).parts
        for name in filenames:
            path = Path(dirpath, name)
            rel = (*rel_dir, name)
            if path.is_symlink() or not path.is_file():
                continue
            if include and not any(_glob_includes(g, rel) for g in include):
                continue
            if any(_glob_excludes(g, rel) for g in exclude):
                continue
            found.append((rel, path))
    return [path for _, path in sorted(found)]


def _glob_includes(glob: str, rel: tuple[str, ...]) -> bool:
    """Like rg --glob: without '/', the glob matches the file name at any depth; else the relative path."""
    if "/" not in glob:
        return fnmatch.fnmatchcase(rel[-1], glob)
    return glob_match(glob.strip("/").split("/"), rel)


def _glob_excludes(glob: str, rel: tuple[str, ...]) -> bool:
    """Like rg --glob '!x': excluding a directory excludes everything below it."""
    if "/" not in glob:
        return any(fnmatch.fnmatchcase(part, glob) for part in rel)
    parts = glob.strip("/").split("/")
    return any(glob_match(parts, rel[:k]) for k in range(1, len(rel) + 1))


# -- glob helpers -------------------------------------------------------------------

def glob_match(pattern_parts: list[str], path_parts: tuple[str, ...] | list[str]) -> bool:
    """Match path segments against pattern segments; `**` matches zero or more segments."""
    if not pattern_parts:
        return not path_parts
    head, rest = pattern_parts[0], pattern_parts[1:]
    if head == "**":
        return any(glob_match(rest, path_parts[i:]) for i in range(len(path_parts) + 1))
    return bool(path_parts) and fnmatch.fnmatchcase(path_parts[0], head) and glob_match(rest, path_parts[1:])


def expand_braces(pattern: str) -> list[str]:
    """Expand `{a,b}` alternatives (nested allowed): 'x.{py,md}' -> ['x.py', 'x.md']."""
    depth, start = 0, -1
    for i, char in enumerate(pattern):
        if char == "{":
            if depth == 0:
                start = i
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0:
                options = _split_top_level(pattern[start + 1:i])
                if len(options) > 1:
                    head, tail = pattern[:start], pattern[i + 1:]
                    return [out for option in options for out in expand_braces(head + option + tail)]
    return [pattern]


def _split_top_level(text: str) -> list[str]:
    parts, depth, current = [], 0, []
    for char in text:
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        depth += char == "{"
        depth -= char == "}" and depth > 0
        current.append(char)
    parts.append("".join(current))
    return parts


def _split_glob(pattern: str) -> tuple[str, str]:
    """Split a pattern into its literal leading directories and the wildcard rest."""
    segments = pattern.split("/")
    for i, segment in enumerate(segments):
        if _GLOB_CHARS & set(segment):
            return ("/".join(segments[:i]) or ("/" if pattern.startswith("/") else "")), "/".join(segments[i:])
    return pattern, ""


# -- small helpers -----------------------------------------------------------------------

def mcp_tool_name(server: str, tool: str) -> str:
    """`mcp__<server>__<tool>`, with characters providers reject replaced by '_' (max 64 chars)."""
    name = f"mcp__{_UNSAFE_NAME_CHARS.sub('_', server)}__{_UNSAFE_NAME_CHARS.sub('_', tool)}"
    return name[:MAX_TOOL_NAME]


def _has_git_component(path: Path) -> bool:
    """True if any component of path is named .git (case-insensitively, for case-insensitive filesystems)."""
    return any(part.lower() == ".git" for part in path.parts)


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _numbered_line(number: int, raw: bytes) -> str:
    if raw.endswith(b"\n"):
        raw = raw[:-1]
    if raw.endswith(b"\r"):
        raw = raw[:-1]
    return "%6d\t%s" % (number, _clip(raw.decode("utf-8", errors="replace")))


def _clip(line: str) -> str:
    return line if len(line) <= MAX_LINE_CHARS else line[:MAX_LINE_CHARS] + "…"


def _bounded_lines(fh: IO[bytes], max_bytes: int) -> Iterator[bytes]:
    """Yield the lines of a binary stream (with their newline) without ever holding a long line whole.

    A line longer than max_bytes is cut to max_bytes + 1 bytes (so it still reads as too long)
    and the rest of it is skipped in chunks.
    """
    while True:
        line = fh.readline(max_bytes + 1)
        if not line:
            return
        if len(line) > max_bytes and not line.endswith(b"\n"):
            rest = line
            while rest and not rest.endswith(b"\n"):
                rest = fh.readline(COUNT_CHUNK_BYTES)
        yield line


def _count_lines(fh: IO[bytes]) -> int:
    """Count the lines left in a binary stream, a last line without a newline included."""
    count, last = 0, b"\n"
    for chunk in iter(lambda: fh.read(COUNT_CHUNK_BYTES), b""):
        count += chunk.count(b"\n")
        last = chunk[-1:]
    return count + (last != b"\n")


def _newest_first(paths: list[str]) -> list[str]:
    def key(path: str) -> tuple[float, str]:
        try:
            return -os.lstat(path).st_mtime, path
        except OSError:
            return 0.0, path
    return sorted(paths, key=key)


def _skill_files(skill_dir: Path) -> list[str]:
    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(skill_dir):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            path = Path(dirpath, name)
            if path != skill_dir / "SKILL.md":
                files.append(str(path))
    if len(files) > SKILL_MAX_FILES:
        return files[:SKILL_MAX_FILES] + [f"… and {len(files) - SKILL_MAX_FILES} more files"]
    return files


def _str_arg(args: dict, key: str, nonempty: bool = False) -> str:
    value = args.get(key)
    if not isinstance(value, str):
        raise ToolError(f"Invalid arguments: '{key}' is required and must be a string.")
    if nonempty and not value.strip():
        raise ToolError(f"Invalid arguments: '{key}' must not be empty.")
    return value


def _int_arg(args: dict, key: str, default: int, minimum: int = 0) -> int:
    value: Any = args.get(key)
    if value is None or value == "":
        return default
    if isinstance(value, str) and re.fullmatch(r"\s*-?\d+\s*", value):
        value = int(value)
    elif isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError(f"Invalid arguments: '{key}' must be an integer.")
    if value < minimum:
        raise ToolError(f"Invalid arguments: '{key}' must be >= {minimum}.")
    return value


def _number_arg(args: dict, key: str) -> float | None:
    value: Any = args.get(key)
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            pass
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0:
        raise ToolError(f"Invalid arguments: '{key}' must be a positive number of seconds.")
    return float(value)


def _bool_arg(args: dict, key: str, default: bool) -> bool:
    value = args.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false", "1", "0", "yes", "no"):
        return value.strip().lower() in ("true", "1", "yes")
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise ToolError(f"Invalid arguments: '{key}' must be true or false.")
