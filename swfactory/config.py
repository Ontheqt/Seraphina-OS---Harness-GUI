"""Configuration: defaults, config.json load/save, and model/driver resolution.

config.json is plain JSON. Every key is optional: the file is deep-merged over
DEFAULTS (dicts merge recursively, lists and scalars replace), so `null` on a
default entry (e.g. ``"drivers": {"codex": null}``) removes it.
"""

from __future__ import annotations

import copy
import json
import os
import re
import secrets
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .agentdefs import clean_value, read_small_text

if TYPE_CHECKING:
    from .agentdefs import AgentDef

PROJECT_DIR = Path(__file__).resolve().parent.parent
USER_DIR = "~/.config/software-factory"
REPO_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
REVIEWER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
PROVIDER_TYPES = ("openai", "anthropic")
AUTH_MODES = ("bearer", "x-api-key", "both")
CONCURRENCY_SCOPES = ("call", "run")
# What kind of server a provider is: tabby reports the model it has loaded (DESIGN §23, placement); openwebui is
# Open WebUI's API, whose workspace models personas bind to (§33).
PROVIDER_KINDS = ("tabby", "openwebui")
DELIVERY_MODES = ("merge", "branch", "folder")   # what happens to an approved job (DESIGN §25)
SWEEP_MODES = ("off", "regex", "model")          # the Housekeeping Tracker's sweep (DESIGN §32; model is v1.1)
DEFAULT_MCP_TIMEOUT = 120.0
DEFAULT_MCP_STARTUP_TIMEOUT = 60.0
MIN_SECRET_CHARS = 8
MAX_KEY_FILE_BYTES = 64 * 1024
ANTHROPIC_BETA = "server-side-fallback-2026-07-01"
_SECRET_NAME_RE = re.compile(r"key|token|secret|password|auth", re.IGNORECASE)
_AUTH_SCHEME_RE = re.compile(r"^(?:bearer|basic|token)\s+", re.IGNORECASE)
_MISSING = object()

_THINKING = {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}


def _hosted(type_: str, base_url: str, key_env: str, **extra: Any) -> dict:
    return {"type": type_, "base_url": base_url, "api_key_env": key_env, "timeout": 900, **extra}


def _local(base_url: str) -> dict:
    return {"type": "openai", "base_url": base_url, "api_key_env": None, "timeout": 1800,
            "concurrency_scope": "run"}


DEFAULTS: dict[str, Any] = {
    "server": {"host": "127.0.0.1", "port": 8765, "token": None, "allowed_hosts": []},
    "data_dir": "~/.local/share/software-factory",
    "repos": [],
    "providers": {
        "deepseek": _hosted("openai", "https://api.deepseek.com", "DEEPSEEK_API_KEY"),
        "deepseek-anthropic": _hosted("anthropic", "https://api.deepseek.com/anthropic", "DEEPSEEK_API_KEY"),
        "anthropic": _hosted("anthropic", "https://api.anthropic.com", "ANTHROPIC_API_KEY",
                             headers={"anthropic-beta": ANTHROPIC_BETA}),
        "openai": _hosted("openai", "https://api.openai.com/v1", "OPENAI_API_KEY"),
        "openrouter": _hosted("openai", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
        "dashscope": _hosted("openai", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
                             "DASHSCOPE_API_KEY"),
        "ollama": _local("http://127.0.0.1:11434/v1"),
        "vllm": _local("http://127.0.0.1:8000/v1"),
        "llamacpp": _local("http://127.0.0.1:8080/v1"),
        "lmstudio": _local("http://127.0.0.1:1234/v1"),
        "nanogpt": _hosted("openai", "https://nano-gpt.com/api/v1", "NANOGPT_API_KEY"),
        "nanogpt-anthropic": _hosted("anthropic", "https://nano-gpt.com/api", "NANOGPT_API_KEY", auth="both"),
        "strata": {"type": "openai", "base_url": "http://127.0.0.1:18080/v1", "api_key_env": "STRATA_API_KEY",
                   "api_key_file": "/etc/ai-stack/strata.env", "max_concurrency": 1, "timeout": 1800,
                   "concurrency_scope": "run"},
        "tabby": {"type": "openai", "base_url": "http://127.0.0.1:5000/v1", "api_key_env": "TABBY_API_KEY",
                  "auth": "both", "max_concurrency": 1, "timeout": 1800, "concurrency_scope": "run"},
    },
    "models": {
        "opus": {"provider": "deepseek", "model": "deepseek-v4-pro", "max_tokens": 32768,
                 "stream": True, "extra_body": copy.deepcopy(_THINKING)},
        "sonnet": {"provider": "deepseek", "model": "deepseek-v4-flash", "max_tokens": 32768,
                   "stream": True, "extra_body": copy.deepcopy(_THINKING)},
        "haiku": {"provider": "deepseek", "model": "deepseek-v4-flash", "max_tokens": 16384,
                  "stream": True, "extra_body": {"thinking": {"type": "disabled"}}},
        "local": {"provider": "strata", "model": "strata", "max_tokens": 16384, "context_tokens": 262144,
                  "keep_reasoning": False, "stream": True, "extra_body": {}},
    },
    "drivers": {
        "builtin": {"type": "builtin"},
        "qwen": {"type": "cli", "preset": "qwen", "command": "qwen", "env": {}, "models": {}},
        "claude": {"type": "cli", "preset": "claude", "command": "claude", "env": {}, "models": {}},
        "codex": {"type": "cli", "preset": "codex", "command": "codex", "env": {}, "models": {}},
    },
    "agents": {"*": {"driver": "builtin"}},
    "pipeline": {"reviewers": ["security", "ux", "ui", "code"], "max_resumes": 2,
                 "max_parallel_jobs": 2, "auto_merge": True, "max_auto_merge_lines": 1500},
    "limits": {"max_turns": 150, "max_wall_seconds": 5400, "bash_timeout": 600,
               "tool_output_chars": 30000, "max_context_chars": 1500000},
    "harness": {"sandbox": "none", "sandbox_network": True, "extra_read_roots": [],
                "skills_dirs": []},
    "mcp_servers": {},
    "escalate_paths": [".github/**", ".gitlab-ci.yml", ".circleci/**", "Jenkinsfile",
                       ".claude/**", ".qwen/**", ".codex/**", ".agents/**", "AGENTS.md", "CLAUDE.md", "QWEN.md",
                       "**/Dockerfile", "docker-compose*.yml", "**/migrations/**",
                       "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
                       "requirements*.txt", "pyproject.toml", "poetry.lock", "uv.lock",
                       "go.mod", "go.sum", "Cargo.toml", "Cargo.lock", "Gemfile", "Gemfile.lock",
                       "**/.env*", "**/*.pem", "**/*.key"],
    "merge": {"push": False, "remote": "origin", "delivery": "merge", "output_dir": None},
    "profiles": {},           # routing profiles chosen per job (DESIGN §23): {name: {description, agents}}
    "default_profile": None,  # the profile a job gets when it names none
    "housekeeping": {"enabled": True, "reinject_every_tool_turns": 3, "reinject_every_tool_turns_api": 6,
                     "reinject_max_chars": 1200, "stop_guard_attempts": 2, "stale_rounds": 2,
                     "followup_due_days": 14, "sweep": "regex", "max_proposals_per_run": 10,
                     "max_proposals_per_scope": 300, "sweeper_model": None, "cli_hooks": False,
                     "mirror_cli_todos": False},
}


_ENV_LINE_BREAK_RE = re.compile(r"\r\n|\r|\n")


def env_file_lines(text: str) -> list[str]:
    """The lines of an env (dotenv) file, split at "\\n" and "\\r" only, as systemd's EnvironmentFile=
    splits them: not at the other breaks str.splitlines() knows (U+2028, U+2029, U+0085, \\v, \\f,
    \\x1c-\\x1e), so every reader of the file sees the same assignments."""
    return _ENV_LINE_BREAK_RE.split(text)


def read_dotenv_value(path: str | os.PathLike, name: str) -> str | None:
    """The last `name=value` in a dotenv file ("export " prefix, quotes and # comments allowed).

    Returns None when the file is missing, unreadable, not a regular file (a FIFO never
    blocks), over MAX_KEY_FILE_BYTES or not UTF-8, or when the name is absent or empty.
    """
    try:
        text = read_small_text(os.path.expanduser(str(path)), MAX_KEY_FILE_BYTES)
    except (OSError, UnicodeDecodeError):
        return None
    found: str | None = None
    for line in env_file_lines(text):
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, equals, value = line.partition("=")
        if equals and not line.startswith("#") and key.strip() == name:
            found = clean_value(value)
    return found or None


def resolve_api_key(api_key_env: str | None, api_key_file: str | None = None) -> str | None:
    """$api_key_env if set and non-empty, else its value in api_key_file, else None (also when keyless)."""
    if not api_key_env:
        return None
    value = os.environ.get(api_key_env)
    if value:
        return value
    return read_dotenv_value(api_key_file, api_key_env) if api_key_file else None


@dataclass
class ProviderConfig:
    name: str
    type: str
    base_url: str
    api_key_env: str | None = None
    timeout: float = 900.0
    headers: dict = field(default_factory=dict)
    auth: str = ""
    api_key_file: str | None = None
    max_concurrency: int | None = None
    concurrency_scope: str = "call"
    # Placement (DESIGN §23): a server that reports its loaded model, whether a request may make it
    # load another, the model key to overflow to and after how long a wait for a slot, its speed.
    kind: str | None = None
    loaded_model_url: str | None = None
    allow_model_swap: bool = False
    swap_back: bool = False
    overflow: str | None = None
    overflow_after_seconds: float | None = None
    tokens_per_second: float | None = None

    def __post_init__(self) -> None:
        if not self.auth:
            self.auth = "x-api-key" if self.type == "anthropic" else "bearer"
        if self.auth not in AUTH_MODES:
            raise ValueError(f"provider {self.name!r}: auth must be one of {', '.join(AUTH_MODES)}")
        if self.concurrency_scope not in CONCURRENCY_SCOPES:
            raise ValueError(f"provider {self.name!r}: concurrency_scope must be one of "
                             f"{', '.join(CONCURRENCY_SCOPES)}")

    def api_key(self) -> str | None:
        """The key: env var api_key_env, else that name in api_key_file (read now), else None."""
        return resolve_api_key(self.api_key_env, self.api_key_file)


@dataclass
class ModelConfig:
    key: str
    provider: ProviderConfig
    model: str
    max_tokens: int = 32768
    stream: bool = True
    extra_body: dict = field(default_factory=dict)
    context_tokens: int | None = None
    keep_reasoning: bool = True
    allow_history_edits: bool | None = None
    eager_tools: bool = False
    # USD per million tokens; None is unknown (not free), except on a local provider (DESIGN §23)
    price_in: float | None = None
    price_out: float | None = None
    price_cached_in: float | None = None

    def __post_init__(self) -> None:
        if self.allow_history_edits is None:
            self.allow_history_edits = getattr(self.provider, "type", None) != "anthropic"


@dataclass
class DriverConfig:
    name: str
    type: str
    preset: str | None = None
    command: str = ""
    run_args: list | None = None
    resume_args: list | None = None
    env: dict = field(default_factory=dict)
    models: dict = field(default_factory=dict)
    timeout: float | None = None
    profile: str | None = None


@dataclass
class RepoConfig:
    name: str
    path: Path
    base: str | None = None
    setup: str | None = None
    escalate_paths: list[str] = field(default_factory=list)


@dataclass
class PipelineConfig:
    reviewers: list[str] = field(default_factory=lambda: list(DEFAULTS["pipeline"]["reviewers"]))
    max_resumes: int = 2
    max_parallel_jobs: int = 2
    auto_merge: bool = True
    max_auto_merge_lines: int = 1500


@dataclass
class MergeConfig:
    push: bool = False
    remote: str = "origin"
    delivery: str = "merge"        # the default delivery mode of new jobs: merge, branch or folder
    output_dir: Path | None = None  # where folder deliveries go; None: <data_dir>/output


@dataclass
class HousekeepingConfig:
    """The Housekeeping Tracker (DESIGN §32). enabled False keeps recording items but turns off what acts on
    them: the gate rule, the build check's items rule, re-injection and the stop guard."""
    enabled: bool = True
    reinject_every_tool_turns: int = 3        # tool batches between notes on a local server (its prefix cache)
    reinject_every_tool_turns_api: int = 6    # on an API provider, which bills every old note again
    reinject_max_chars: int = 1200
    stop_guard_attempts: int = 2
    stale_rounds: int = 2
    followup_due_days: int = 14
    sweep: str = "regex"            # off | regex | model (model is v1.1 and runs as regex until then)
    max_proposals_per_run: int = 10
    max_proposals_per_scope: int = 300   # suggestions kept one by one per scope; the rest fold into one item
    sweeper_model: str | None = None   # v1.1
    cli_hooks: bool = False            # Claude Code hooks for CLI runs (SessionStart compact, Stop); off until a
    #                                    real headless run is verified (DESIGN §32)
    mirror_cli_todos: bool = False     # v1.1


@dataclass
class Limits:
    max_turns: int = 150
    max_wall_seconds: float = 5400.0
    bash_timeout: float = 600.0
    tool_output_chars: int = 30000
    max_context_chars: int = 1500000


@dataclass
class McpServerConfig:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict = field(default_factory=dict)   # name -> value; None (JSON null) removes the variable
    cwd: str | None = None
    timeout: float = DEFAULT_MCP_TIMEOUT
    tools: list[str] | None = None
    startup_timeout: float = DEFAULT_MCP_STARTUP_TIMEOUT
    # tool -> names of its arguments that give a file the tool writes; the builtin harness checks each
    # against the agent's write policy (like Write) before the call reaches the server
    write_args: dict[str, list[str]] = field(default_factory=dict)


def default_config_path() -> Path:
    """$FACTORY_CONFIG if set, else ~/.config/software-factory/config.json."""
    configured = os.environ.get("FACTORY_CONFIG")
    if configured:
        return Path(os.path.expanduser(configured)).absolute()
    return Path(os.path.expanduser(USER_DIR)) / "config.json"


def deep_merge(base: dict, override: dict) -> dict:
    """Return a new dict: nested dicts merge recursively; lists, scalars and None replace."""
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def expand_path(value: str | os.PathLike, base: Path | None = None) -> Path:
    """Expand "~"; a relative result is anchored at base when one is given."""
    path = Path(os.path.expanduser(str(value)))
    if base is not None and not path.is_absolute():
        path = base / path
    return path


def sanitize_repo_name(text: str) -> str:
    """Lowercase text and squeeze it into [a-z0-9][a-z0-9._-]* ("" if nothing is left)."""
    name = re.sub(r"[^a-z0-9._-]+", "-", text.lower())
    name = re.sub(r"^[^a-z0-9]+", "", name)
    return name.rstrip("-")


def dir_exists(path: Path) -> bool:
    """path.is_dir(), but False (not an error) when it cannot be checked, e.g. permission denied."""
    try:
        return path.is_dir()
    except OSError:
        return False


def agent_dirs(config: Config | None, repo_path: Path | None) -> list[Path]:
    """Existing agent dirs in override order (later wins): project, user, <repo>/.factory/agents.

    `config` is accepted for symmetry with skills.skill_dirs; no config key moves these dirs.
    """
    candidates = [PROJECT_DIR / "agents", Path(os.path.expanduser(USER_DIR)) / "agents"]
    if repo_path is not None:
        candidates.append(Path(repo_path) / ".factory" / "agents")
    return [directory for directory in candidates if dir_exists(directory)]


class Config:
    """The merged configuration (`raw` = DEFAULTS deep-merged with the file's contents).

    save() writes the file's own keys plus every value of raw that differs from DEFAULTS,
    so settings the file never made keep following DEFAULTS in later versions.
    """

    def __init__(self, path: str | os.PathLike, raw: dict | None = None):
        self.path = Path(os.path.expanduser(str(path))).absolute()
        self._file_data: dict = copy.deepcopy(raw or {})
        self.raw: dict = deep_merge(DEFAULTS, raw or {})
        self.data_dir = expand_path(self.raw.get("data_dir") or DEFAULTS["data_dir"], self.path.parent)
        self._lock = threading.RLock()

    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> Config:
        """Read config.json (default path when None); a missing file yields defaults with no token."""
        config_path = default_config_path() if path is None else Path(os.path.expanduser(str(path)))
        try:
            text = config_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return cls(config_path)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{config_path}: invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"{config_path}: the top level must be a JSON object")
        return cls(config_path, data)

    def save(self) -> None:
        """Write raw atomically (tmp file + os.replace) with mode 0600; parent dir created 0700.

        Written: the file's own keys and whatever differs from DEFAULTS (loading the
        result gives raw back).
        """
        with self._lock:
            data = _overrides(DEFAULTS, self.raw, self._file_data)
            text = json.dumps(data, indent=2) + "\n"
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(text)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(tmp_name, 0o600)
                os.replace(tmp_name, self.path)
            except BaseException:
                try:
                    os.unlink(tmp_name)
                except FileNotFoundError:
                    pass
                raise
            os.chmod(self.path, 0o600)
            self._file_data = data

    def validate(self) -> None:
        """Build every typed view once; raise ValueError naming the first invalid setting."""
        try:
            names = [repo.name for repo in self.repos()]
            for name in names:
                if not REPO_NAME_RE.fullmatch(name):
                    raise ValueError(f"invalid repo name {name!r}: use [a-z0-9][a-z0-9._-]*")
            if len(set(names)) != len(names):
                raise ValueError("repo names must be unique")
            for name, entry in self._section("providers").items():
                if entry is not None:
                    self.provider(name)
            for key, entry in self._section("models").items():
                if entry is not None:
                    self.model(key)
                    self.check_placement(f"models.{key}", entry)
            for name, entry in self._section("providers").items():
                if isinstance(entry, dict):
                    self.check_placement(f"providers.{name}", entry)
            for name, entry in self._section("drivers").items():
                if entry is not None:
                    self.driver(name)
            for name, entry in self._section("agents").items():
                if isinstance(entry, dict):
                    if entry.get("driver"):
                        self.driver(str(entry["driver"]))
                    if entry.get("model"):
                        self.model(str(entry["model"]))
                elif entry is not None:
                    raise ValueError(f"agents.{name} must be an object like {{\"driver\": ..., \"model\": ...}}")
            if not all(isinstance(glob, str) for glob in self._list("escalate_paths")):
                raise ValueError("escalate_paths must be a list of strings")
            self.pipeline()
            self.limits()
            self.merge()
            self.housekeeping()
            self.mcp_servers()
            default = self.raw.get("default_profile")
            if default is not None and not isinstance(self._section("profiles").get(default), dict):
                raise ValueError(f"default_profile {default!r} is not a key of profiles")
        except KeyError as exc:
            raise ValueError(str(exc.args[0]) if exc.args else str(exc)) from exc
        except TypeError as exc:
            raise ValueError(f"invalid config value: {exc}") from exc

    # -- repos ---------------------------------------------------------------

    def repos(self) -> list[RepoConfig]:
        """Configured repos, with "~" and relative paths expanded."""
        return [self._repo_from(entry) for entry in self._list("repos")]

    def repo(self, name: str) -> RepoConfig:
        """The repo called name (KeyError if unknown)."""
        for repo in self.repos():
            if repo.name == name:
                return repo
        raise KeyError(f"unknown repo {name!r}")

    def add_repo(self, path: str | os.PathLike, name: str | None = None) -> RepoConfig:
        """Register a git repo (its top-level dir) under a unique name and save the config."""
        top = _git_toplevel(Path(path))
        if name is None:
            name = sanitize_repo_name(top.name)
            if not name:
                raise ValueError(f"cannot derive a repo name from {top.name!r}; pass a name")
        elif not REPO_NAME_RE.fullmatch(name):
            raise ValueError(f"invalid repo name {name!r}: use [a-z0-9][a-z0-9._-]*")
        with self._lock:
            for repo in self.repos():
                if repo.name == name:
                    raise ValueError(f"a repo named {name!r} already exists")
                if repo.path.resolve() == top:
                    raise ValueError(f"{top} is already configured as repo {repo.name!r}")
            entry = {"name": name, "path": str(top), "base": None, "setup": None, "escalate_paths": []}
            if not isinstance(self.raw.get("repos"), list):
                self.raw["repos"] = []
            self.raw["repos"].append(entry)
            try:
                self.save()
            except BaseException:
                self.raw["repos"].remove(entry)
                raise
            return self._repo_from(entry)

    def remove_repo(self, name: str) -> None:
        """Unregister a repo and save the config (KeyError if unknown)."""
        with self._lock:
            entries = self._list("repos")
            for index, entry in enumerate(entries):
                if isinstance(entry, dict) and entry.get("name") == name:
                    del entries[index]
                    try:
                        self.save()
                    except BaseException:
                        entries.insert(index, entry)
                        raise
                    return
        raise KeyError(f"unknown repo {name!r}")

    def escalate_paths(self, repo: RepoConfig) -> list[str]:
        """Global escalate_paths followed by the repo's own, deduplicated in order."""
        combined = [*self._list("escalate_paths"), *(repo.escalate_paths or [])]
        return list(dict.fromkeys(str(glob) for glob in combined))

    # -- providers, models, drivers, agents ----------------------------------

    def provider(self, name: str) -> ProviderConfig:
        """The provider called name (KeyError if unknown, ValueError if malformed)."""
        entry = self._entry("providers", name, "provider")
        if entry.get("type") not in PROVIDER_TYPES:
            raise ValueError(f"providers.{name}.type must be one of {', '.join(PROVIDER_TYPES)}")
        if not entry.get("base_url"):
            raise ValueError(f"providers.{name} has no base_url")
        concurrency = entry.get("max_concurrency")
        if concurrency is not None and (isinstance(concurrency, bool) or int(concurrency) < 1):
            raise ValueError(f"providers.{name}.max_concurrency must be a positive integer or null")
        key_file = entry.get("api_key_file")
        kind = entry.get("kind")
        if kind is not None and kind not in PROVIDER_KINDS:
            raise ValueError(f"providers.{name}.kind must be one of {', '.join(PROVIDER_KINDS)} or null")
        wait, speed = entry.get("overflow_after_seconds"), entry.get("tokens_per_second")
        return ProviderConfig(
            name=name,
            type=str(entry["type"]),
            base_url=str(entry["base_url"]),
            api_key_env=entry.get("api_key_env") or None,
            timeout=float(entry.get("timeout") or 900),
            headers={str(k): str(v) for k, v in (entry.get("headers") or {}).items()},
            auth=str(entry.get("auth") or ""),
            api_key_file=str(key_file) if key_file else None,
            max_concurrency=int(concurrency) if concurrency is not None else None,
            concurrency_scope=str(entry.get("concurrency_scope") or "call"),
            kind=str(kind) if kind else None,
            loaded_model_url=str(entry["loaded_model_url"]) if entry.get("loaded_model_url") else None,
            allow_model_swap=_as_bool(entry.get("allow_model_swap"), f"providers.{name}.allow_model_swap", False),
            swap_back=_as_bool(entry.get("swap_back"), f"providers.{name}.swap_back", False),
            overflow=str(entry["overflow"]) if entry.get("overflow") else None,
            overflow_after_seconds=float(wait) if wait is not None else None,
            tokens_per_second=float(speed) if speed is not None else None,
        )

    def model(self, ref: str) -> ModelConfig:
        """A key of `models`, or else a literal model id run with the "opus" entry's provider and settings."""
        if not ref:
            raise ValueError("empty model reference")
        models = self._section("models")
        entry = models.get(ref)
        if isinstance(entry, dict):
            return self._model_from(ref, entry, entry.get("model"))
        base = models.get("opus")
        if not isinstance(base, dict):
            raise KeyError(f"model {ref!r} is not a key of models and there is no 'opus' entry to run it on")
        return self._model_from(ref, base, ref)

    def driver(self, name: str) -> DriverConfig:
        """The driver called name (KeyError if unknown or set to null)."""
        entry = self._entry("drivers", name, "driver")
        if not entry.get("type"):
            raise ValueError(f"drivers.{name} has no type")
        timeout = entry.get("timeout")
        return DriverConfig(
            name=name,
            type=str(entry["type"]),
            preset=entry.get("preset") or None,
            command=os.path.expanduser(str(entry.get("command") or entry.get("preset") or "")),
            run_args=_str_list_or_none(entry.get("run_args"), f"drivers.{name}.run_args"),
            resume_args=_str_list_or_none(entry.get("resume_args"), f"drivers.{name}.resume_args"),
            # null stays None: the CLI driver removes that variable from the agent's environment
            env={str(k): None if v is None else str(v) for k, v in (entry.get("env") or {}).items()},
            models=dict(entry.get("models") or {}),
            timeout=float(timeout) if timeout is not None else None,
            profile=str(entry["profile"]) if entry.get("profile") else None,
        )

    def agent_driver(self, agent_name: str) -> str:
        """Driver name for an agent: agents[name].driver, then agents["*"].driver, then "builtin"."""
        return str(self._agent_override(agent_name, "driver") or "builtin")

    def agent_model(self, agent: AgentDef) -> ModelConfig:
        """Model for an agent: agents[name].model, then agents["*"].model, then the file's tier."""
        ref = self._agent_override(agent.name, "model") or agent.model or "opus"
        return self.model(str(ref))

    # -- typed sections --------------------------------------------------------

    def pipeline(self) -> PipelineConfig:
        """The pipeline section (null values fall back to defaults)."""
        settings = self._settings("pipeline")
        reviewers = settings["reviewers"]
        if not isinstance(reviewers, list) or not all(
                isinstance(r, str) and REVIEWER_NAME_RE.fullmatch(r) for r in reviewers):
            raise ValueError("pipeline.reviewers must be a list of names like 'security'")
        return PipelineConfig(
            reviewers=list(dict.fromkeys(reviewers)),
            max_resumes=max(0, int(settings["max_resumes"])),
            max_parallel_jobs=max(1, int(settings["max_parallel_jobs"])),
            auto_merge=_as_bool(settings["auto_merge"], "pipeline.auto_merge"),
            max_auto_merge_lines=int(settings["max_auto_merge_lines"]),
        )

    def limits(self) -> Limits:
        """The limits section (null values fall back to defaults)."""
        settings = self._settings("limits")
        return Limits(
            max_turns=int(settings["max_turns"]),
            max_wall_seconds=float(settings["max_wall_seconds"]),
            bash_timeout=float(settings["bash_timeout"]),
            tool_output_chars=int(settings["tool_output_chars"]),
            max_context_chars=int(settings["max_context_chars"]),
        )

    def merge(self) -> MergeConfig:
        """The merge section (null values fall back to defaults); output_dir resolved, "~" expanded and a
        relative path anchored at the config file's directory, <data_dir>/output when unset."""
        settings = self._settings("merge")
        delivery = settings["delivery"]
        if delivery not in DELIVERY_MODES:
            raise ValueError(f"merge.delivery must be one of {', '.join(DELIVERY_MODES)}")
        remote = settings["remote"]
        if not isinstance(remote, str) or not remote.strip() or remote.startswith("-"):
            raise ValueError("merge.remote must be a remote name")
        output = settings.get("output_dir")
        if output is not None and (not isinstance(output, str) or not output.strip()):
            raise ValueError("merge.output_dir must be a directory path or null")
        return MergeConfig(
            push=_as_bool(settings["push"], "merge.push"),
            remote=remote,
            delivery=str(delivery),
            output_dir=expand_path(output, self.path.parent) if output else None,
        )

    def housekeeping(self) -> HousekeepingConfig:
        """The housekeeping section (null values fall back to defaults)."""
        settings = self._settings("housekeeping")
        sweep = settings["sweep"]
        if sweep not in SWEEP_MODES:
            raise ValueError(f"housekeeping.sweep must be one of {', '.join(SWEEP_MODES)}")
        model = settings.get("sweeper_model")
        if model is not None and not isinstance(model, str):
            raise ValueError("housekeeping.sweeper_model must be a model key or null")
        numbers = {}
        for key in ("reinject_every_tool_turns", "reinject_every_tool_turns_api", "reinject_max_chars",
                    "stop_guard_attempts", "stale_rounds", "followup_due_days", "max_proposals_per_run",
                    "max_proposals_per_scope"):
            value = settings[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"housekeeping.{key} must be a non-negative integer")
            numbers[key] = value
        return HousekeepingConfig(
            enabled=_as_bool(settings["enabled"], "housekeeping.enabled"),
            sweep=str(sweep), sweeper_model=model,
            cli_hooks=_as_bool(settings["cli_hooks"], "housekeeping.cli_hooks"),
            mirror_cli_todos=_as_bool(settings["mirror_cli_todos"], "housekeeping.mirror_cli_todos"),
            **numbers,
        )

    def output_dir(self) -> Path:
        """Where folder deliveries are written: merge.output_dir, else <data_dir>/output."""
        return self.merge().output_dir or self.data_dir / "output"

    def mcp_servers(self) -> dict[str, McpServerConfig]:
        """Configured stdio MCP servers by name (null entries are skipped)."""
        servers: dict[str, McpServerConfig] = {}
        for name, entry in self._section("mcp_servers").items():
            if entry is None:
                continue
            if not isinstance(entry, dict) or not entry.get("command"):
                raise ValueError(f"mcp_servers.{name} needs a command")
            cwd = entry.get("cwd")
            servers[name] = McpServerConfig(
                name=name,
                command=os.path.expanduser(str(entry["command"])),
                args=_str_list_or_none(entry.get("args"), f"mcp_servers.{name}.args") or [],
                env={str(k): None if v is None else str(v) for k, v in (entry.get("env") or {}).items()},
                cwd=os.path.expanduser(str(cwd)) if cwd else None,
                timeout=float(entry.get("timeout") or DEFAULT_MCP_TIMEOUT),
                tools=_str_list_or_none(entry.get("tools"), f"mcp_servers.{name}.tools"),
                startup_timeout=float(entry.get("startup_timeout") or DEFAULT_MCP_STARTUP_TIMEOUT),
                write_args=_write_args(entry.get("write_args"), f"mcp_servers.{name}.write_args"),
            )
        return servers

    # -- secrets ---------------------------------------------------------------

    def secret_values(self) -> list[str]:
        """Values to redact: provider keys (env or api_key_file), the values public_view masks
        (secret-named headers without a "Bearer " prefix, secret-named driver/MCP env) and the
        server token.

        Values shorter than MIN_SECRET_CHARS (dummy keys like "x") are left out so
        redaction does not mangle ordinary text.
        """
        candidates: list[Any] = []
        for entry in _dict_entries(self.raw, "providers"):
            candidates += provider_secret_values(entry)
        for section in ("drivers", "mcp_servers"):
            for entry in _dict_entries(self.raw, section):
                candidates += _secret_named_values(entry.get("env"))
        candidates.append(self._section("server").get("token"))
        values: list[str] = []
        for value in candidates:
            if isinstance(value, str) and len(value) >= MIN_SECRET_CHARS and value not in values:
                values.append(value)
        return values

    def public_view(self) -> dict:
        """A copy of raw for the GUI: no server token, providers get key_present, secret-named values "***".

        Masked: header values and driver/MCP env values whose name looks secret
        (contains key, token, secret, password or auth).
        """
        view = copy.deepcopy(self.raw)
        if isinstance(view.get("server"), dict):
            view["server"].pop("token", None)
        for entry in _dict_entries(view, "providers"):
            entry["key_present"] = resolve_api_key(entry.get("api_key_env"), entry.get("api_key_file")) is not None
            _mask_secret_values(entry, "headers")
        for section in ("drivers", "mcp_servers"):
            for entry in _dict_entries(view, section):
                _mask_secret_values(entry, "env")
        return view

    # -- internals -------------------------------------------------------------

    def _section(self, name: str) -> dict:
        value = self.raw.get(name)
        return value if isinstance(value, dict) else {}

    def _list(self, name: str) -> list:
        value = self.raw.get(name)
        return value if isinstance(value, list) else []

    def _settings(self, name: str) -> dict:
        settings = dict(DEFAULTS[name])
        settings.update({k: v for k, v in self._section(name).items() if v is not None})
        return settings

    def _entry(self, section: str, name: str, label: str) -> dict:
        entry = self._section(section).get(name)
        if not isinstance(entry, dict):
            raise KeyError(f"unknown {label} {name!r}")
        return entry

    def _agent_override(self, agent_name: str, key: str) -> Any:
        agents = self._section("agents")
        for candidate in (agent_name, "*"):
            entry = agents.get(candidate)
            if isinstance(entry, dict) and entry.get(key):
                return entry[key]
        return None

    def _model_from(self, key: str, entry: dict, model_id: Any) -> ModelConfig:
        if not model_id:
            raise ValueError(f"models.{key} has no model")
        if not entry.get("provider"):
            raise ValueError(f"models.{key} has no provider")
        max_tokens = int(entry.get("max_tokens") or 32768)
        context = entry.get("context_tokens")
        if context is not None and int(context) <= max_tokens:
            raise ValueError(f"models.{key}.context_tokens must be larger than max_tokens ({max_tokens})")
        provider = self.provider(str(entry["provider"]))
        return ModelConfig(
            key=key,
            provider=provider,
            model=str(model_id),
            max_tokens=max_tokens,
            stream=_as_bool(entry.get("stream", True), f"models.{key}.stream", True),
            extra_body=copy.deepcopy(entry.get("extra_body") or {}),
            context_tokens=int(context) if context is not None else None,
            keep_reasoning=_as_bool(entry.get("keep_reasoning"), f"models.{key}.keep_reasoning", True),
            allow_history_edits=_as_bool(entry.get("allow_history_edits"), f"models.{key}.allow_history_edits",
                                         provider.type != "anthropic"),
            eager_tools=_as_bool(entry.get("eager_tools"), f"models.{key}.eager_tools", False),
            price_in=_price(entry, "price_in", key), price_out=_price(entry, "price_out", key),
            price_cached_in=_price(entry, "price_cached_in", key),
        )

    def check_placement(self, label: str, entry: dict) -> None:
        """The placement references of a model or provider entry resolve: every build's provider, the
        overflow model (DESIGN §23). settings.validate_settings checks their shape strictly."""
        builds = (entry.get("builds") or []) if label.startswith("models.") else []
        for index, build in enumerate(builds):
            if not isinstance(build, dict) or not build.get("provider") or not build.get("model"):
                raise ValueError(f"{label}.builds[{index}] needs a provider and a model")
            self.provider(str(build["provider"]))
        overflow = entry.get("overflow")
        if overflow and not isinstance(self._section("models").get(overflow), dict):
            raise ValueError(f"{label}.overflow {overflow!r} is not a key of models")

    def _repo_from(self, entry: Any) -> RepoConfig:
        if not isinstance(entry, dict) or not entry.get("name") or not entry.get("path"):
            raise ValueError(f"repo entry {entry!r} needs a name and a path")
        return RepoConfig(
            name=str(entry["name"]),
            path=expand_path(entry["path"], self.path.parent),
            base=entry.get("base") or None,
            setup=entry.get("setup") or None,
            escalate_paths=[str(glob) for glob in (entry.get("escalate_paths") or [])],
        )


def init_config(path: str | os.PathLike | None = None, force: bool = False) -> Config:
    """Write the full default config plus a fresh server token.

    An existing file is refused unless force (which overwrites it), except a file with no
    server token yet (e.g. written by `repo add` before `init`): it gets a token and keeps
    everything else.
    """
    config_path = default_config_path() if path is None else Path(os.path.expanduser(str(path)))
    token = secrets.token_hex(16)
    if config_path.exists() and not force:
        config = Config.load(config_path)
        server = config.raw.get("server")
        if isinstance(server, dict) and server.get("token"):
            raise FileExistsError(f"{config_path} already exists (use --force to overwrite)")
        if not isinstance(server, dict):
            server = config.raw["server"] = copy.deepcopy(DEFAULTS["server"])
        server["token"] = token
    else:
        config = Config(config_path, deep_merge(DEFAULTS, {"server": {"token": token}}))
    config.save()
    return config


def _git_toplevel(path: Path) -> Path:
    """path resolved, if it is the top-level dir of a git work tree; ValueError otherwise."""
    resolved = path.expanduser().resolve()
    if not resolved.is_dir():
        raise ValueError(f"{resolved} is not a directory")
    from . import gitops   # hardened host-side git (no hooks, no fsmonitor)

    try:
        out = gitops.git(resolved, "rev-parse", "--show-toplevel", timeout=30)
    except gitops.GitError as exc:
        if exc.returncode is None:
            raise ValueError(f"cannot run git in {resolved}: {exc.stderr.strip()}") from exc
        raise ValueError(f"{resolved} is not a git repository: {exc.stderr.strip()}") from exc
    top = Path(out.strip()).resolve()
    if top != resolved:
        raise ValueError(f"{resolved} is not the top level of its git repository ({top})")
    return resolved


def _as_bool(value: Any, label: str, default: bool | None = None) -> bool:
    if value is None and default is not None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be true or false")
    return value


def _price(entry: dict, field: str, key: str) -> float | None:
    value = entry.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError(f"models.{key}.{field} must be a non-negative number (USD per million tokens) or null")
    return float(value)


def _write_args(value: Any, label: str) -> dict[str, list[str]]:
    """An MCP server's write_args: {tool: [argument, ...]}; None means none."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object of tool names to lists of argument names")
    return {str(tool): _str_list_or_none(names, f"{label}.{tool}") or [] for tool, names in value.items()}


def _str_list_or_none(value: Any, label: str) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list of strings")
    return [str(item) for item in value]


def _overrides(defaults: dict, current: dict, kept: dict) -> dict:
    """The part of current to persist: keys present in kept plus values that differ from defaults.

    deep_merge(defaults, result) == current; a defaults key missing from current becomes null.
    """
    result: dict = {}
    for key, value in current.items():
        default = defaults.get(key, _MISSING)
        own = kept.get(key, _MISSING)
        if isinstance(value, dict) and isinstance(default, dict):
            nested = _overrides(default, value, own if isinstance(own, dict) else {})
            if nested or own is not _MISSING:
                result[key] = nested
        elif own is not _MISSING or default is _MISSING or not _same_json(value, default):
            result[key] = copy.deepcopy(value)
    result.update({key: None for key in defaults if key not in current})
    return result


def _same_json(a: Any, b: Any) -> bool:
    """Equal as JSON (unlike ==, keeps 1, 1.0 and true apart)."""
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def provider_secret_values(entry: dict) -> list[str]:
    """The secrets of one provider entry (a config entry or a routing snapshot's provider_entry): its
    key, read now from api_key_env or api_key_file, and its secret-named header values (without an
    auth scheme prefix). Short values are not filtered here."""
    if not isinstance(entry, dict):
        return []
    key = resolve_api_key(entry.get("api_key_env") or None, entry.get("api_key_file") or None)
    return ([key] if key else []) + _secret_named_values(entry.get("headers"))


def _secret_named_values(values: Any) -> list[str]:
    """Values of a {name: value} mapping whose name looks secret, without an auth scheme prefix."""
    if not isinstance(values, dict):
        return []
    return [_AUTH_SCHEME_RE.sub("", str(value).strip()) for name, value in values.items()
            if value is not None and _SECRET_NAME_RE.search(str(name))]


def _dict_entries(view: dict, section: str) -> list[dict]:
    entries = view.get(section)
    if not isinstance(entries, dict):
        return []
    return [entry for entry in entries.values() if isinstance(entry, dict)]


def _mask_secret_values(entry: dict, key: str) -> None:
    values = entry.get(key)
    if isinstance(values, dict):
        entry[key] = {name: "***" if _SECRET_NAME_RE.search(str(name)) else value
                      for name, value in values.items()}
