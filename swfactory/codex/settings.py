"""The ``codex`` section and the repository settings of BC1b (BROWSER_AND_CODEX.md §3.3, §3.11; DESIGN §44).

``codex`` (off by default):

- ``enabled``: a ``codex-worker`` job may start only while this is true (and the doctor's ``codex model`` line passes);
- ``binary``: the pinned Codex binary, an absolute path (the release's standalone binary, which the setup's block c7
  installs at ``/usr/local/lib/seraphina-codex/codex``, or the native binary of ``@openai/codex@0.163.0`` on a test
  machine); never a ``codex`` found on PATH;
- ``pin``: the version the doctor and the launcher expect (0.163.0);
- ``user``: the Codex user (the owner's O3, for example ``ai-codex``); null runs Codex as the harness's own user,
  as the test suite does, and the doctor says that this is not the server's containment;
- ``via``: how the harness becomes that user: ``sudo`` (the server: one sudoers rule for the launcher) or
  ``setpriv`` (only when the harness itself runs as root, as the test suite may);
- ``group``: the worktree group shared by the harness's user and the Codex user (O3);
- ``launcher``: the installed launcher (on the server root-installed, ``/usr/local/libexec/seraphina/sf-codex-launch``);
  null: this checkout's ``bin/sf-codex-launch``;
- ``launch_settings``: the launcher's settings file; null: the harness writes it into ``work_root`` (allowed only
  without a Codex user, since a Codex user's launcher reads only its root-owned file);
- ``work_root``: the per-job homes and the per-run folders (null: ``<data_dir>/codex``);
- ``key_file``: the file the launcher reads the Codex route's key from, as the Codex user (``NAME=value`` lines;
  null: on the server, a Codex user under sudo, ``/etc/seraphina/codex-key.env``, which the setup's block c4 installs
  readable by the Codex user only (its own, 0400: not by the worktree group, which the harness's user is in, so
  neither the harness nor an agent it runs reads it), since the provider's own key file is the harness's; otherwise
  the provider's own ``api_key_file``);
- ``verify_persona``: the browsing persona "Verify in the browser" opens (``kate-browser``);
- ``retries``: Codex's own ``request_max_retries`` and ``stream_max_retries`` (small, so Codex fails before the
  harness's wall clock does);
- ``private_roots``: the owner's personal and health data roots, which the doctor's ``codex reads`` line checks the
  Codex user cannot read (§3.11 item 3).

Repository entries gain ``verify`` (a shell command the harness runs itself, always under bwrap), ``verify_seconds``,
``verify_binds`` (more read-only toolchain paths), ``preview`` (``{command, port, startup_seconds}`` for "Verify in
the browser") and ``personal_data`` (the owner's O10: true, personal data present, unless the entry says false).
"""

from __future__ import annotations

import copy
import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import CODEX_PIN

PROJECT_DIR = Path(__file__).resolve().parents[2]
BUNDLED_LAUNCHER = PROJECT_DIR / "bin" / "sf-codex-launch"
SERVER_LAUNCHER = "/usr/local/libexec/seraphina/sf-codex-launch"
SERVER_LAUNCH_SETTINGS = "/etc/seraphina/codex-launch.json"
SERVER_KEY_FILE = "/etc/seraphina/codex-key.env"
VIAS = ("sudo", "setpriv")
SETTINGS_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "binary": None,
    "pin": CODEX_PIN,
    "user": None,
    "via": "sudo",
    "group": None,
    "launcher": None,
    "launch_settings": None,
    "work_root": None,
    "key_file": None,
    "verify_persona": "kate-browser",
    "retries": {"request_max_retries": 1, "stream_max_retries": 1},
    "private_roots": [],
}
SETTINGS_KEYS = frozenset(SETTINGS_DEFAULTS)
USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
REPO_KEYS = ("verify", "verify_seconds", "verify_binds", "preview", "personal_data")
PREVIEW_KEYS = frozenset({"command", "port", "startup_seconds"})
DEFAULT_VERIFY_SECONDS = 600


def settings(config: Any) -> dict:
    """The codex section merged over its defaults."""
    raw = getattr(config, "raw", config) or {}
    section = raw.get("codex") if isinstance(raw.get("codex"), dict) else {}
    out = copy.deepcopy(SETTINGS_DEFAULTS)
    for key, value in section.items():
        if key == "retries" and isinstance(value, dict):
            out["retries"].update(value)
        elif key in SETTINGS_KEYS:
            out[key] = copy.deepcopy(value)
    return out


def check_settings(raw: dict) -> None:
    """ValueError naming the first invalid codex setting or repository setting of BC1b."""
    section = raw.get("codex")
    if section is not None:
        if not isinstance(section, dict):
            raise ValueError("codex must be an object")
        unknown = sorted(set(section) - SETTINGS_KEYS)
        if unknown:
            raise ValueError(f"codex.{unknown[0]} is not a setting (use {', '.join(sorted(SETTINGS_KEYS))})")
        merged = settings(raw)
        if not isinstance(merged["enabled"], bool):
            raise ValueError("codex.enabled must be true or false")
        for key in ("binary", "launcher", "launch_settings", "work_root", "key_file"):
            value = merged.get(key)
            if value is not None and (not isinstance(value, str) or "\0" in value
                                      or not os.path.isabs(os.path.expanduser(value))):
                raise ValueError(f"codex.{key} must be an absolute path or null")
        if not isinstance(merged["pin"], str) or not re.fullmatch(r"\d+\.\d+\.\d+", merged["pin"]):
            raise ValueError("codex.pin must be a version like 0.163.0")
        for key in ("user", "group"):
            value = merged.get(key)
            if value is not None and (not isinstance(value, str) or not USER_RE.fullmatch(value)):
                raise ValueError(f"codex.{key} must be a Unix user or group name, or null")
        if merged["via"] not in VIAS:
            raise ValueError(f"codex.via must be one of {', '.join(VIAS)}")
        if merged["user"] is not None and merged["launch_settings"] is None and merged["via"] == "sudo":
            raise ValueError("codex.launch_settings must name the launcher's root-owned settings file when "
                             f"codex.user is set (the setup block installs {SERVER_LAUNCH_SETTINGS})")
        roots = merged.get("private_roots")
        if not isinstance(roots, list) or not all(isinstance(p, str) and os.path.isabs(os.path.expanduser(p))
                                                  for p in roots):
            raise ValueError("codex.private_roots must be a list of absolute paths")
        if not isinstance(merged["verify_persona"], str) or not merged["verify_persona"].strip():
            raise ValueError("codex.verify_persona must name the browsing persona")
        retries = merged["retries"]
        if not isinstance(retries, dict) or set(retries) - {"request_max_retries", "stream_max_retries"}:
            raise ValueError("codex.retries takes request_max_retries and stream_max_retries")
        for key, value in retries.items():
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 5:
                raise ValueError(f"codex.retries.{key} must be a whole number from 0 to 5")
    for entry in raw.get("repos") or []:
        if isinstance(entry, dict):
            repo_settings(entry)


@dataclass
class RepoWork:
    """What a repository entry says about verify, the preview and personal data."""
    verify: str | None = None
    verify_seconds: float = DEFAULT_VERIFY_SECONDS
    verify_binds: list[str] = field(default_factory=list)
    preview: dict | None = None
    personal_data: bool = True


def repo_settings(entry: dict) -> RepoWork:
    """The BC1b settings of one repos[] entry, checked (ValueError naming the bad one)."""
    name = entry.get("name") or "?"
    verify = entry.get("verify")
    if verify is not None and (not isinstance(verify, str) or not verify.strip() or "\0" in verify):
        raise ValueError(f"repos[{name}].verify must be a shell command or null")
    seconds = entry.get("verify_seconds", DEFAULT_VERIFY_SECONDS)
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 1 <= seconds <= 7200:
        raise ValueError(f"repos[{name}].verify_seconds must be a number of seconds from 1 to 7200")
    binds = entry.get("verify_binds") or []
    if not isinstance(binds, list) or not all(isinstance(p, str) and os.path.isabs(os.path.expanduser(p))
                                              for p in binds):
        raise ValueError(f"repos[{name}].verify_binds must be a list of absolute paths (read-only toolchain)")
    preview = entry.get("preview")
    if preview is not None:
        if not isinstance(preview, dict) or set(preview) - PREVIEW_KEYS or not preview.get("command"):
            raise ValueError(f"repos[{name}].preview must be {{\"command\": ..., \"port\": ..., "
                             f"\"startup_seconds\": ...}} or null")
        command = preview["command"]
        if isinstance(command, str):
            command = shlex.split(command)
        if not isinstance(command, list) or not command or not all(isinstance(p, str) and p for p in command):
            raise ValueError(f"repos[{name}].preview.command must be a command line or a list of arguments")
        port = preview.get("port", 18938)
        if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
            raise ValueError(f"repos[{name}].preview.port must be a port from 1024 to 65535")
        startup = preview.get("startup_seconds", 20)
        if isinstance(startup, bool) or not isinstance(startup, (int, float)) or not 1 <= startup <= 120:
            raise ValueError(f"repos[{name}].preview.startup_seconds must be from 1 to 120")
        preview = {"command": command, "port": port, "startup_seconds": startup}
    personal = entry.get("personal_data", True)
    if not isinstance(personal, bool):
        raise ValueError(f"repos[{name}].personal_data must be true or false (true unless the repository is free "
                         f"of personal data)")
    return RepoWork(verify=verify.strip() if isinstance(verify, str) else None, verify_seconds=float(seconds),
                    verify_binds=[os.path.expanduser(p) for p in binds], preview=preview, personal_data=personal)


def repo_work(config: Any, repo: str) -> RepoWork:
    """The BC1b settings of the repository called repo (defaults when it has none)."""
    for entry in (getattr(config, "raw", {}) or {}).get("repos") or []:
        if isinstance(entry, dict) and entry.get("name") == repo:
            return repo_settings(entry)
    return RepoWork()


def work_root(config: Any) -> Path:
    merged = settings(config)
    root = merged.get("work_root")
    return Path(os.path.expanduser(root)) if root else Path(config.data_dir) / "codex"


def key_file(config: Any, api_key_file: str | None) -> str | None:
    """The file the launcher reads a provider's key from, as the Codex user: codex.key_file when set; with a Codex
    user under sudo (the server), SERVER_KEY_FILE, which the setup's block c4 installs readable by the Codex user
    only (0400, its own; the harness never reads it, and agents' Bash gets /dev/null there: config.sandbox_masks)
    (the provider's own api_key_file, a systemd credential or a file of the harness's, is not the Codex user's to
    read); otherwise the provider's own api_key_file (None: none)."""
    merged = settings(config)
    if merged.get("key_file"):
        return os.path.expanduser(str(merged["key_file"]))
    if merged.get("user") and merged["via"] == "sudo":
        return SERVER_KEY_FILE
    return os.path.expanduser(str(api_key_file)) if api_key_file else None


def masked_key_files(config: Any) -> list[Path]:
    """The Codex route's key files an agent's Bash must never read (config.sandbox_masks hides each with
    /dev/null when it exists): SERVER_KEY_FILE and codex.key_file, whatever their modes."""
    out = [Path(SERVER_KEY_FILE)]
    chosen = settings(config).get("key_file")
    if isinstance(chosen, str) and chosen.strip():
        out.append(Path(os.path.expanduser(chosen)))
    return list(dict.fromkeys(out))


def launcher_path(config: Any) -> Path:
    merged = settings(config)
    return Path(os.path.expanduser(merged["launcher"])) if merged.get("launcher") else BUNDLED_LAUNCHER


def launch_settings_path(config: Any) -> Path:
    merged = settings(config)
    if merged.get("launch_settings"):
        return Path(os.path.expanduser(merged["launch_settings"]))
    return work_root(config) / "launch.json"
