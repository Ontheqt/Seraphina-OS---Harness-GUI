"""The factory's env file: API keys as NAME=value lines in <config dir>/env (DESIGN.md sections 3, 25).

One format, read three ways: `factory` loads it at startup (load_env_file, variables
already set win), the systemd unit loads it with EnvironmentFile=, and a provider's
api_key_file is read the same way (config.read_dotenv_value). Lines are `NAME=value`
(an `export ` prefix, `#` comments and blank lines allowed); a value may be wrapped in
single or double quotes. Lines end at "\\n" or "\\r" only (config.env_file_lines), as systemd
reads them.

set_secret writes one name from the GUI (POST /api/secrets): the file is rewritten
atomically with mode 0600, every other line kept, and the value is set in this
process's environment. Values are never logged, returned or put in an exception
message. Only credential-looking names (events.is_credential_name) and names a
provider uses as api_key_env are accepted, so every value written here is also
redacted from events and kept out of agent environments (sections 10 and 12).
"""

from __future__ import annotations

import os
import re
import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .agentdefs import clean_value, read_small_text
from .config import MAX_KEY_FILE_BYTES, env_file_lines, resolve_api_key
from .events import is_credential_name

if TYPE_CHECKING:
    from .config import Config

ENV_FILE = "env"
SECRET_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$", re.ASCII)
MAX_VALUE_CHARS = 4096
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$", re.ASCII)
_PLAIN_VALUE_RE = re.compile(r"^[A-Za-z0-9_@%+=:,./~^-]*$", re.ASCII)   # written without quotes
_LOCK = threading.Lock()
HEADER = ("# API keys for Software Factory: one NAME=value per line. `factory` reads this\n"
          "# file at startup (variables already set win), and so does the systemd unit.\n"
          "# Keep it mode 600.\n")


def env_file_path(config: Config) -> Path:
    """<config dir>/env: next to config.json, wherever $FACTORY_CONFIG puts it."""
    return config.path.parent / ENV_FILE


def parse_line(line: str) -> tuple[str, str] | None:
    """(name, value) of one `NAME=value` line (`export ` prefix allowed), None for comments and other lines."""
    line = line.strip()
    if line.startswith("export "):
        line = line[len("export "):].lstrip()
    name, equals, value = line.partition("=")
    name = name.strip()
    if not equals or line.startswith("#") or not _ENV_NAME_RE.fullmatch(name):
        return None
    return name, clean_value(value)


def parse_env_text(text: str) -> list[tuple[str, str]]:
    """Every (name, value) assignment of an env file's text, in file order (lines split as systemd does)."""
    return [pair for pair in map(parse_line, env_file_lines(text)) if pair is not None]


def read_env_file(path: Path) -> str | None:
    """The env file's text, or None when there is none (OSError/UnicodeDecodeError when unreadable)."""
    try:
        return read_small_text(path, MAX_KEY_FILE_BYTES)
    except FileNotFoundError:
        return None


def check_secret(config: Config, name: Any, value: Any) -> tuple[str, str]:
    """(name, value) after validation; ValueError naming the problem (never echoing the value).

    name: ^[A-Z][A-Z0-9_]{1,63}$, and either a provider's api_key_env or a credential-looking
    name (*_API_KEY, *_TOKEN, *_SECRET, ...). value: at most 4096 printable characters
    (str.isprintable: no control, format, line or paragraph separator characters, so a value is
    always one line for every reader), no surrounding spaces, no "'"; "" removes the name.
    """
    if not isinstance(name, str) or not SECRET_NAME_RE.fullmatch(name):
        raise ValueError("name must match ^[A-Z][A-Z0-9_]{1,63}$ (for example DEEPSEEK_API_KEY)")
    if name not in provider_key_names(config) and not is_credential_name(name):
        raise ValueError(f"{name} is not an api_key_env of a configured provider and does not look like a "
                         f"credential (…_API_KEY, …_TOKEN, …_SECRET): only secrets are stored here")
    if not isinstance(value, str):
        raise ValueError("value must be a string")
    if len(value) > MAX_VALUE_CHARS:
        raise ValueError(f"value is longer than {MAX_VALUE_CHARS} characters")
    if not value.isprintable():
        raise ValueError("value must be a single line of printable characters (no control, format, "
                         "line or paragraph separator characters)")
    if value != value.strip():
        raise ValueError("value must not start or end with spaces")
    if "'" in value:
        raise ValueError("value cannot contain a single quote (')")
    return name, value


def set_secret(config: Config, name: Any, value: Any) -> dict:
    """Write name=value to the env file (atomic, 0600; other lines kept; "" removes it) and set it in
    os.environ (unset for ""). Returns the name's status entry {name, set, in_env_file, providers}."""
    name, value = check_secret(config, name, value)
    path = env_file_path(config)
    with _LOCK:
        text = read_env_file(path)
        lines = env_file_lines(text if text is not None else HEADER)
        kept: list[str] = []
        placed = False
        for line in lines:
            parsed = parse_line(line)
            if parsed is not None and parsed[0] == name:
                if not placed and value:
                    kept.append(_assignment(name, value))
                placed = True
                continue
            kept.append(line)
        if not placed and value:
            placeholder = next((i for i, line in enumerate(kept) if _commented_name(line) == name), None)
            if placeholder is not None:
                kept[placeholder] = _assignment(name, value)
            else:
                kept.append(_assignment(name, value))
        _write_private(path, "\n".join(kept).rstrip("\n") + "\n")
        if value:
            os.environ[name] = value
        else:
            os.environ.pop(name, None)
    return _entry(config, name, set(name for name, _ in parse_env_text(read_env_file(path) or "")))


def secret_status(config: Config) -> dict:
    """{env_file, secrets: [{name, set, in_env_file, providers}]}: every provider's api_key_env, plus the
    credential-looking names the env file assigns, sorted. Names and booleans only, never values.

    set: the key resolves now (environment, or the provider's api_key_file); in_env_file: the env
    file assigns the name; providers: the providers that use it as api_key_env.
    """
    path = env_file_path(config)
    try:
        in_file = {name for name, _ in parse_env_text(read_env_file(path) or "")}
    except (OSError, UnicodeDecodeError):
        in_file = set()
    names = set(provider_key_names(config)) | {name for name in in_file
                                               if SECRET_NAME_RE.fullmatch(name) and is_credential_name(name)}
    return {"env_file": str(path), "secrets": [_entry(config, name, in_file) for name in sorted(names)]}


def provider_key_names(config: Config) -> dict[str, list[str]]:
    """{api_key_env: [provider names]} for every configured provider that has a key variable."""
    names: dict[str, list[str]] = {}
    providers = config.raw.get("providers")
    for provider, entry in (providers.items() if isinstance(providers, dict) else []):
        if isinstance(entry, dict) and isinstance(entry.get("api_key_env"), str) and entry["api_key_env"]:
            names.setdefault(entry["api_key_env"], []).append(str(provider))
    return names


def _entry(config: Config, name: str, in_file: set[str]) -> dict:
    users = provider_key_names(config).get(name, [])
    resolved = bool(os.environ.get(name))
    if not resolved:
        providers = config.raw.get("providers") if isinstance(config.raw.get("providers"), dict) else {}
        resolved = any(resolve_api_key(name, providers[p].get("api_key_file")) for p in users
                       if isinstance(providers.get(p), dict) and providers[p].get("api_key_file"))
    return {"name": name, "set": resolved, "in_env_file": name in in_file, "providers": sorted(users)}


def _assignment(name: str, value: str) -> str:
    """NAME=value as every reader of the file parses it back: plain when safe, else in single quotes
    (taken literally by systemd and by clean_value)."""
    return f"{name}={value}" if _PLAIN_VALUE_RE.fullmatch(value) else f"{name}='{value}'"


def _commented_name(line: str) -> str | None:
    """NAME for a commented-out placeholder line like "# NAME=" (what install.sh writes)."""
    stripped = line.strip()
    if not stripped.startswith("#"):
        return None
    parsed = parse_line(stripped.lstrip("#").strip())
    return parsed[0] if parsed is not None and parsed[1] == "" else None


def _write_private(path: Path, text: str) -> None:
    """Write text to path atomically with mode 0600 (the directory is created 0700)."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    os.chmod(path, 0o600)
