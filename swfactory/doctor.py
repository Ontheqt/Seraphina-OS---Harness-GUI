"""Doctor: environment and configuration checks for `factory doctor` and GET /api/doctor.

Each check is ``{name, ok, level, detail}``. ``level`` is the severity of a failed
check (``error`` or ``warn``) or ``info`` for passing checks and advice. run_checks
never raises; every check that touches the network or runs a program has its own
timeout, and the slow, independent ones (providers, drivers, model probes) run in
parallel threads.

setup_status is the offline summary behind GET /api/setup (the GUI's Welcome page and
Grid Card): one {ok, detail} per setup step, from the same checks without the network.

Placement (DESIGN §23): the providers of models' builds and overflows, of the agents'
escalation rungs and of the routing profiles are checked like routed ones; each provider's
model list is read and a configured model id it does not list is a warning; a model-aware
server (kind "tabby") reports the model it has loaded; one line says which local servers are up.

Open WebUI (DESIGN §33): each provider of kind "openwebui" (and each one a persona is bound to)
gets reachable (GET /api/version), key (accepted, and refused without it) and models (the
workspace models the key may read) checks, and each persona bound to it a check that its models
entry stays local, that Open WebUI lists its workspace model, and whether the preset changed
since the last drift check (read only: the doctor opens no tracker item).
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from . import gitops, routing, settings
from .agentdefs import AGENT_NAME_RE, AgentDef, required_agents
from .config import PROJECT_DIR, Config, ModelConfig, ProviderConfig, RepoConfig, dir_exists, expand_path
from .drivers.cli import CliRunner
from .harness.sandbox import bwrap_available
from .skills import find_skill_files

HTTP_TIMEOUT = 5.0
VERSION_TIMEOUT = 10.0
GRACE_SECONDS = 5.0
MIN_PYTHON = (3, 10)
INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md", "QWEN.md")
EXTRA_AGENTS = ("lead", "coder", "repo-scout")
OLLAMA_PORT = 11434
NOT_CONFIGURED = "not configured"   # how an optional MCP server's passing check starts when it has no entry
DEFAULT_OLLAMA_CONTEXT = 32768
OPTIONAL_TOOLS = (
    ("rg", "Grep falls back to a slower Python search (apt install ripgrep)"),
    ("agent-browser", "builder and UX/UI reviewers check pages with curl only (npm install -g agent-browser)"),
    ("node", "needed by the Claude Code, Qwen Code and Codex CLIs and by most web repos"),
)

Check = dict
Lookup = Callable[[RepoConfig | None], "dict[str, AgentDef]"]


def check(name: str, ok: bool, level: str, detail: str) -> Check:
    """One result; a passing check is always level "info"."""
    return {"name": name, "ok": bool(ok), "level": "info" if ok else level, "detail": detail}


def run_checks(config: Config, factory: Any = None, probe_models: bool = False) -> list[Check]:
    """Every check, in display order; never raises.

    factory (optional) supplies the agents it really runs; probe_models also sends every
    routed model the two Test probes (this costs model calls; keys that resolve to the
    same model are probed once, and a provider never gets more probes at once than
    its max_concurrency).
    """
    lookup = _agent_lookup(config, factory)
    checks = [
        *_safely("python", _python),
        *_safely("git", _git),
        *_safely("config file", lambda: _config_file(config)),
        *_safely("server token", lambda: _token(config)),
        *_safely("data dir", lambda: _data_dir(config)),
        *_safely("sandbox", lambda: _sandbox(config)),
        *_safely("tools", _optional_tools),
        *_safely("repos", lambda: _repos(config, lookup)),
    ]
    agents: dict[str, AgentDef] = {}
    try:
        agents = lookup(None)
    except Exception as exc:  # reported below as the agents check
        checks.append(check("agents", False, "error", str(exc)))
    else:
        checks += _safely("agents", lambda: _agents(config, agents))
        checks += _safely("mcp", lambda: _mcp_checks(config, agents))
    checks += _safely("skills dirs", lambda: _skills_dirs(config))
    try:
        routes, route_checks = _routes(config, agents)
    except Exception as exc:  # reported as the routing check
        routes, route_checks = [], [check("routing", False, "error", f"check failed: {type(exc).__name__}: {exc}")]
    checks += route_checks
    try:
        placed, placement_checks = _placement_routes(config, routes)
    except Exception as exc:  # reported as the placement check
        placed, placement_checks = [], [check("placement", False, "error", f"check failed: {type(exc).__name__}: {exc}")]
    checks += placement_checks
    providers = _providers(routes + placed)
    jobs = [(f"provider {provider.name}", lambda p=provider, u=users, m=models: _provider_checks(p, u, m))
            for provider, users, models in providers]
    jobs += [(f"driver {name}", lambda n=name, u=users: _driver_check(config, n, u))
             for name, users in _cli_drivers(routes).items()]
    openwebui_jobs, persona_notes = _openwebui_jobs(config)
    jobs += openwebui_jobs
    found = _parallel(jobs, 3 * HTTP_TIMEOUT + VERSION_TIMEOUT + GRACE_SECONDS)
    checks += found + _local_servers([provider for provider, _, _ in providers], found) + persona_notes
    if probe_models:
        checks += _safely("models", lambda: _model_probes(config, routes))
    return checks


# ---------------------------------------------------------------- local checks


def _python() -> list[Check]:
    ok = sys.version_info[:2] >= MIN_PYTHON
    detail = f"Python {platform.python_version()}" + ("" if ok else ": Software Factory needs Python 3.10 or newer")
    return [check("python", ok, "error", detail)]


def _git() -> list[Check]:
    try:
        version = gitops.git_version()
    except gitops.GitError as exc:
        return [check("git", False, "error", f"git is not installed or does not run: {exc}")]
    text = ".".join(str(part) for part in version)
    if version >= gitops.MERGE_TREE_MIN_VERSION:
        return [check("git", True, "info", f"git {text}")]
    return [check("git", False, "warn", f"git {text} is older than 2.38: conflict checks use slower trial "
                                         "merges in a temporary worktree")]


def _config_file(config: Config) -> list[Check]:
    try:
        mode = stat.S_IMODE(config.path.stat().st_mode)
    except FileNotFoundError:
        return [check("config file", False, "warn", f"{config.path} does not exist: run factory init")]
    if mode & 0o077:
        return [check("config file", False, "warn", f"{config.path} is mode {mode:o} but holds the server "
                                                     f"token: chmod 600 {config.path}")]
    return [check("config file", True, "info", f"{config.path} (mode {mode:o})")]


def _token(config: Config) -> list[Check]:
    server = config.raw.get("server")
    if isinstance(server, dict) and server.get("token"):
        return [check("server token", True, "info", "set")]
    return [check("server token", False, "error", "no server token: run factory init (the GUI and API "
                                                   "refuse every request without one)")]


def _data_dir(config: Config) -> list[Check]:
    directory = config.data_dir
    if dir_exists(directory):
        try:
            with tempfile.TemporaryFile(dir=directory, prefix=".doctor-"):
                pass
        except OSError as exc:
            return [check("data dir", False, "error", f"{directory} is not writable: {exc}")]
        return [check("data dir", True, "info", f"{directory} is writable")]
    if os.path.lexists(directory):  # a file, a broken symlink or something unreadable is in the way
        return [check("data dir", False, "error", f"{directory} exists but is not a directory the factory can use")]
    parent = directory.parent
    while not os.path.lexists(parent) and parent != parent.parent:
        parent = parent.parent
    if not dir_exists(parent):
        return [check("data dir", False, "error", f"{directory} cannot be created: {parent} is not a directory")]
    if os.access(parent, os.W_OK | os.X_OK):
        return [check("data dir", True, "info", f"{directory} will be created (in {parent})")]
    return [check("data dir", False, "error", f"{directory} does not exist and {parent} is not writable")]


def _sandbox(config: Config) -> list[Check]:
    harness = config.raw.get("harness")
    mode = harness.get("sandbox") if isinstance(harness, dict) else None
    if mode == "bwrap":
        if bwrap_available():
            return [check("sandbox", True, "info", "bubblewrap works: Bash runs sandboxed")]
        return [check("sandbox", False, "error", "harness.sandbox is bwrap but bubblewrap is missing or cannot "
                                                  "create a sandbox here: install it (apt install bubblewrap) "
                                                  "or set harness.sandbox to none")]
    return [check("sandbox", True, "info", "harness.sandbox is none: agents' Bash commands are not contained; "
                                           "run the factory as a dedicated Unix user or set it to bwrap")]


def _optional_tools() -> list[Check]:
    checks = []
    for name, missing in OPTIONAL_TOOLS:
        path = shutil.which(name)
        checks.append(check(name, path is not None, "info", path or f"not found: {missing}"))
    return checks


# ---------------------------------------------------------------- repos and agents


def _agent_lookup(config: Config, factory: Any) -> Lookup:
    """Agents for a repo (None = global): the factory's own resolution when given, else settings'."""
    def lookup(repo: RepoConfig | None) -> dict[str, AgentDef]:
        if factory is not None:
            return factory.agents(repo.name) if repo is not None else factory.agents()
        return settings.resolved_agents(config, repo.path if repo is not None else None)
    return lookup


def _repos(config: Config, lookup: Lookup) -> list[Check]:
    try:
        repos = config.repos()
    except ValueError as exc:
        return [check("repos", False, "error", str(exc))]
    if not repos:
        return [check("repos", False, "warn", "no repos configured: factory repo add <path>")]
    checks: list[Check] = []
    for repo in repos:
        checks += _safely(f"repo {repo.name}", lambda r=repo: _repo_checks(r, lookup))
    return checks


def _repo_checks(repo: RepoConfig, lookup: Lookup) -> list[Check]:
    name = f"repo {repo.name}"
    path = repo.path
    if not dir_exists(path):
        return [check(name, False, "error", f"{path} does not exist")]
    try:
        top = Path(gitops.git(path, "rev-parse", "--show-toplevel", timeout=30).strip()).resolve()
    except gitops.GitError:
        return [check(name, False, "error", f"{path} is not a git repository")]
    if top != path.resolve():
        return [check(name, False, "error", f"{path} is not the top level of its git repository ({top})")]
    try:
        base = gitops.detect_base(path, repo.base)
        commit = gitops.rev_parse(path, base)
    except gitops.GitError as exc:
        return [check(name, False, "error", f"the base branch does not resolve: {exc}")]
    checks = [check(name, True, "info", f"{path} (base {base} at {commit[:10]})")]
    found = gitops.git(path, "ls-tree", "--name-only", base, "--", *INSTRUCTION_FILES).split()
    if found:
        checks.append(check(f"{name} instructions", True, "info", f"{found[0]} on {base}"))
    else:
        checks.append(check(f"{name} instructions", False, "warn",
                            f"no AGENTS.md, CLAUDE.md or QWEN.md on {base}: agents get no repo instructions; "
                            f"draft one with the repo scout (Scout in the GUI) and commit it"))
    if (path / ".factory" / "agents").is_dir():
        try:
            found = lookup(repo)
        except Exception as exc:  # a broken repo agent file only affects this repo
            checks.append(check(f"{name} agents", False, "error", str(exc)))
        else:
            checks.append(check(f"{name} agents", True, "info", "repo agent overrides parse"))
            local = (path / ".factory" / "agents").resolve()
            checks += _items_contract({key: agent for key, agent in found.items()
                                       if local in Path(agent.path).resolve().parents}, f"{name} agents items")
    return checks


ITEMS_CONTRACT_AGENTS = frozenset({"builder", "approver", "coder", "lead"})   # and every reviewer-*


def _items_contract(agents: dict[str, AgentDef], name: str) -> list[Check]:
    """Overridden agent files that predate the Housekeeping Tracker (DESIGN.md section 32): a builder,
    reviewer, approver, coder or lead outside the shipped agents/ whose body has no ## Items section. The
    harness gives every run the contract anyway; the warning says to bring the file's own words up to date."""
    shipped = (PROJECT_DIR / "agents").resolve()
    old = []
    for key, agent in sorted(agents.items()):
        if key not in ITEMS_CONTRACT_AGENTS and not key.startswith("reviewer-"):
            continue
        with_contract = "## items" in (agent.body or "").lower()
        if not with_contract and shipped not in Path(agent.path).resolve().parents:
            old.append(f"{key} ({agent.path})")
    if not old:
        return []
    return [check(name, False, "warn", f"written before the Housekeeping Tracker, with no ## Items section: "
                                       f"{', '.join(old)}. Every run still gets the contract from the harness (the "
                                       "Items tool, the items note, the prompt's Items line); copy the ## Items "
                                       "section of the shipped agents/<name>.md so the file says the same")]


def _agents(config: Config, agents: dict[str, AgentDef]) -> list[Check]:
    try:
        reviewers = config.pipeline().reviewers
    except ValueError as exc:
        return [check("agents", False, "error", str(exc))]
    core = [name for name in required_agents(reviewers) if name not in agents]
    extra = [name for name in EXTRA_AGENTS if name not in agents]
    if core:
        return [check("agents", False, "error", f"missing agents: {', '.join(core + extra)}")]
    if extra:
        return [check("agents", False, "warn", f"missing agents: {', '.join(extra)} (plans, chat or the repo "
                                                f"scout will not work)")]
    needed = len(required_agents(reviewers)) + len(EXTRA_AGENTS)
    return [check("agents", True, "info", f"all {needed} required agents parse ({len(agents)} found)"),
            *_items_contract(agents, "agents items")]


# ---------------------------------------------------------------- tools: MCP servers and skills dirs


def _mcp_checks(config: Config, agents: dict[str, AgentDef]) -> list[Check]:
    """One check per MCP server an agent's frontmatter names: defined in mcp_servers, and its command
    resolvable and executable the way McpClient starts it (nothing is started).

    A server only Chat agents use (none of the factory line, plans or the repo scout) is optional: a missing
    entry is advice (info, with the preset that adds it) and a bad command a warning, not an error."""
    users: dict[str, list[str]] = {}
    for agent in agents.values():
        for server in agent.mcp:
            users.setdefault(server, []).append(agent.name)
    if not users:
        return []
    try:
        servers = config.mcp_servers()
    except ValueError as exc:
        return [check("mcp", False, "error", str(exc))]
    line = _line_agents(config)
    checks: list[Check] = []
    for name in sorted(users):
        used_by = f"used by {', '.join(sorted(users[name]))}"
        chat_only = line is not None and not line.intersection(users[name])
        cli = sorted(agent for agent in users[name] if _runs_on_cli(config, agent))
        note = (f"; {', '.join(cli)} run on a CLI driver, which does not attach the factory's MCP servers"
                if cli else "")
        server = servers.get(name)
        if server is None:
            if chat_only:
                presets = _presets_with_server(name)
                hint = f" (the {' or '.join(presets)} preset adds it)" if presets else ""
                checks.append(check(f"mcp {name}", True, "info",
                                    f"{NOT_CONFIGURED} (optional: only Chat uses it): mcp_servers has no entry "
                                    f"{name!r}, so its tools are skipped until you add it{hint}; {used_by}{note}"))
            else:
                checks.append(check(f"mcp {name}", False, "warn",
                                    f"mcp_servers has no entry {name!r}: its tools are skipped; {used_by}{note}"))
            continue
        problem, program = _mcp_command(server)
        tools = f"tools: {', '.join(server.tools)}" if server.tools is not None else "all of its tools"
        if problem:
            checks.append(check(f"mcp {name}", False, "warn" if chat_only else "error",
                                f"{problem}; {used_by}" + (" (only Chat uses it)" if chat_only else "")))
        else:
            checks.append(check(f"mcp {name}", True, "info", f"{program} ({tools}); {used_by}{note}"))
    return checks


def _line_agents(config: Config) -> set[str] | None:
    """The agents jobs, plans and the repo scout run (everything but Chat agents); None if the pipeline is
    invalid (then every server counts as needed)."""
    try:
        reviewers = config.pipeline().reviewers
    except ValueError:
        return None
    return {*required_agents(reviewers), "lead", "repo-scout"}


def _presets_with_server(name: str) -> list[str]:
    """The shipped presets whose mcp_servers define name (e.g. rea for the analyst's server)."""
    found = []
    for preset in settings.list_presets():
        try:
            servers = settings.load_preset(preset).get("mcp_servers")
        except (OSError, ValueError, KeyError):
            continue
        if isinstance(servers, dict) and isinstance(servers.get(name), dict):
            found.append(preset)
    return found


def _runs_on_cli(config: Config, agent_name: str) -> bool:
    try:
        return config.driver(config.agent_driver(agent_name)).type == "cli"
    except (KeyError, ValueError):
        return False


def _mcp_command(server: Any) -> tuple[str | None, str]:
    """(problem or None, the program) for an MCP server's command, resolved like subprocess resolves it
    for McpClient: a bare name through PATH (the entry's env may set PATH), a path as given; a relative
    path without a cwd depends on the agent's working directory."""
    command = str(server.command)
    env_path = (server.env or {}).get("PATH") or os.environ.get("PATH", os.defpath)
    if os.sep not in command:
        found = shutil.which(command, path=env_path)
        return (None, found) if found else (f"command {command!r} is not on PATH", command)
    path = Path(command)
    if not path.is_absolute():
        if not server.cwd:
            return (f"command {command!r} is relative and the entry has no cwd, so it would resolve in each "
                    f"agent's working directory (a job's worktree): use an absolute path", command)
        path = Path(server.cwd) / path
    if not path.is_file():
        return f"command {path} does not exist", str(path)
    if not os.access(path, os.X_OK):
        return f"command {path} is not executable", str(path)
    return None, str(path)


def _skills_dirs(config: Config) -> list[Check]:
    """One check per harness.skills_dirs entry: the directory exists and how many SKILL.md it holds."""
    harness = config.raw.get("harness")
    entries = harness.get("skills_dirs") if isinstance(harness, dict) else None
    checks: list[Check] = []
    for entry in entries if isinstance(entries, list) else []:
        directory = expand_path(str(entry), config.path.parent)
        name = f"skills dir {entry}"
        if not dir_exists(directory):
            checks.append(check(name, False, "warn", f"{directory} does not exist (harness.skills_dirs)"))
            continue
        found = find_skill_files(directory)
        if not found:
            checks.append(check(name, False, "warn", f"{directory} holds no SKILL.md (searched 4 levels deep)"))
        else:
            checks.append(check(name, True, "info", f"{directory}: {len(found)} skill(s)"))
    return checks


# ---------------------------------------------------------------- routes


@dataclass
class Route:
    """Where one tier or agent runs: its model reference, the resolved model and the driver."""

    label: str
    ref: str
    driver: str | None = None  # None for a tier
    model: ModelConfig | None = None
    builtin: bool = True
    is_key: bool = False
    error: str | None = None

    def describe(self) -> str:
        if self.error is not None or self.model is None:
            return f"{self.label} → {self.ref}: {self.error}"
        where = self.model.provider.name + (f", {self.driver}" if self.driver else "")
        return f"{self.label} → {self.ref} ({where})"


def _route(config: Config, label: str, ref: str, driver: str | None = None) -> Route:
    route = Route(label, ref, driver, is_key=isinstance(_dict(config.raw.get("models")).get(ref), dict))
    try:
        route.model = config.model(ref)
        if driver is not None:
            route.builtin = config.driver(driver).type == "builtin"
    except Exception as exc:  # any malformed entry makes this route broken, not the doctor
        route.error = str(exc.args[0]) if isinstance(exc, KeyError) and exc.args else str(exc)
    return route


def _routes(config: Config, agents: dict[str, AgentDef]) -> tuple[list[Route], list[Check]]:
    models = _dict(config.raw.get("models"))
    routes = [_route(config, f"{tier} tier", tier) for tier in settings.TIERS if isinstance(models.get(tier), dict)]
    try:
        reviewers = config.pipeline().reviewers
    except ValueError:
        reviewers = []
    for name in [*required_agents(reviewers), *EXTRA_AGENTS]:
        agent = agents.get(name)
        if agent is not None:
            ref = settings.route_model(config, name, agent.model)
            routes.append(_route(config, name, ref, config.agent_driver(name)))
    broken = [route for route in routes if route.error is not None]
    literal = [route for route in routes if route.error is None and not route.is_key]
    lines = "; ".join(route.describe() for route in routes)
    if broken:
        return routes, [check("routing", False, "error", lines)]
    if literal:
        names = ", ".join(f"{route.label} ({route.ref})" for route in literal)
        return routes, [check("routing", False, "warn", f"not keys of models, so they run as literal model ids "
                                                         f"on the opus entry's provider: {names}. {lines}")]
    return routes, [check("routing", True, "info", lines)]


def _placement_routes(config: Config, routes: list[Route]) -> tuple[list[Route], list[Check]]:
    """Routes for what a placement can run on besides the routed models (DESIGN §23): the other builds of
    each routed model, its overflow, the agents' escalation rungs and every profile's models and rungs,
    so their providers get checked too; and one placement check: an error naming the first reference
    that does not resolve, else what is configured (nothing when there is none)."""
    raw = config.raw
    models = {key for key, entry in _dict(raw.get("models")).items() if isinstance(entry, dict)}
    drivers = {name for name, entry in _dict(raw.get("drivers")).items() if isinstance(entry, dict)}
    try:
        for key in sorted(models):
            config.check_placement(f"models.{key}", raw["models"][key])
        for name, entry in _dict(raw.get("providers")).items():
            if isinstance(entry, dict):
                config.check_placement(f"providers.{name}", entry)
        for name, entry in _dict(raw.get("agents")).items():
            if isinstance(entry, dict):
                routing.check_placement({key: entry[key] for key in ("effort", "escalate", "reason") if key in entry},
                                        f"agents.{name}", models)
        routing.check_profiles(raw, models, drivers, AGENT_NAME_RE)
    except (KeyError, ValueError) as exc:
        return [], [check("placement", False, "error", str(exc.args[0]) if isinstance(exc, KeyError) and exc.args
                          else str(exc))]
    extra: list[Route] = []
    keys = [(route.ref, route.label, route.builtin) for route in routes if route.is_key and route.error is None]
    for name, entry in _dict(raw.get("agents")).items():
        builtin = _builtin(config, entry.get("driver") if isinstance(entry, dict) else None, name)
        for index, key in enumerate(_dict(entry).get("escalate") or [], start=1):
            keys.append((str(key), f"{name} escalation rung {index}", builtin))
    profiles = _dict(raw.get("profiles"))
    for profile, body in profiles.items():
        for name, entry in _dict(_dict(body).get("agents")).items():
            builtin = _builtin(config, _dict(entry).get("driver"), name)
            for key in [_dict(entry).get("model"), *(_dict(entry).get("escalate") or [])]:
                if key:
                    keys.append((str(key), f"profile {profile}: {name}", builtin))
    counts = {"builds": 0, "overflows": 0}
    seen: set[str] = set()
    while keys:
        key, label, builtin = keys.pop(0)
        if key not in models:
            continue
        model = config.model(key)
        if not any(route.ref == key and route.label == label for route in routes):
            extra.append(Route(label, key, "builtin" if builtin else "cli", model, builtin, True))
        if key in seen:
            continue
        seen.add(key)
        for index, build in enumerate(routing.config_builds(config, model)[1:], start=2):
            counts["builds"] += 1
            extra.append(Route(f"{key} build {index}", key, "builtin", routing.on_build(model, build), builtin, True))
        overflow = routing.overflow_key(config, model)
        if overflow:
            counts["overflows"] += 1
            keys.append((overflow, f"{key} overflow", builtin))
    ladders = sum(1 for entry in _dict(raw.get("agents")).values() if _dict(entry).get("escalate"))
    if not (counts["builds"] or counts["overflows"] or ladders or profiles):
        return extra, []
    default = raw.get("default_profile")
    detail = (f"{counts['builds']} extra builds, {counts['overflows']} overflows, {ladders} escalation ladders, "
              f"{len(profiles)} profiles" + (f" (default {default})" if default else "") + "; references resolve")
    return extra, [check("placement", True, "info", detail)]


def _builtin(config: Config, driver: Any, agent: str) -> bool:
    try:
        return config.driver(str(driver or config.agent_driver(agent))).type == "builtin"
    except (KeyError, ValueError):
        return True


def _local_servers(providers: list[ProviderConfig], checks: list[Check]) -> list[Check]:
    """One line on the local servers the routes use: which answered (and what a model-aware one has loaded)."""
    local = [provider for provider in providers if settings.is_local_url(provider.base_url)]
    if not local:
        return []
    by_name = {c["name"]: c for c in checks}
    up, down = [], []
    for provider in local:
        reachable = by_name.get(f"provider {provider.name} reachable")
        loaded = by_name.get(f"provider {provider.name} loaded model")
        if reachable is not None and reachable["ok"]:
            known = loaded is not None and not loaded["detail"].startswith("cannot tell")
            up.append(provider.name + (f" ({loaded['detail'].split(';')[0]})" if known else ""))
        else:
            down.append(provider.name)
    detail = f"up: {', '.join(up) or 'none'}; down: {', '.join(down) or 'none'}"
    if up:
        return [check("local servers", True, "info", detail)]
    return [check("local servers", False, "warn", detail + " (start the one that should hold the GPUs; placements "
                                                           "fall to the next build or the overflow)")]


def _providers(routes: list[Route]) -> list[tuple[ProviderConfig, list[str], list[ModelConfig]]]:
    """Each provider a builtin-driver route or a tier uses, with who uses it and its models."""
    found: dict[str, tuple[ProviderConfig, list[str], list[ModelConfig]]] = {}
    for route in routes:
        if route.error is not None or route.model is None or not route.builtin:
            continue
        provider = route.model.provider
        entry = found.setdefault(provider.name, (provider, [], []))
        entry[1].append(route.label)
        entry[2].append(route.model)
    return list(found.values())


def _cli_drivers(routes: list[Route]) -> dict[str, list[str]]:
    """Each CLI driver a route uses, with who uses it."""
    drivers: dict[str, list[str]] = {}
    for route in routes:
        if route.driver and route.error is None and not route.builtin:
            drivers.setdefault(route.driver, []).append(route.label)
    return drivers


# ---------------------------------------------------------------- providers, drivers, models


def _provider_checks(provider: ProviderConfig, users: list[str], models: list[ModelConfig]) -> list[Check]:
    name = f"provider {provider.name}"
    used_by = f"used by {', '.join(users)}"
    checks: list[Check] = []
    key = provider.api_key()
    if provider.api_key_env:
        if key:
            where = "the environment" if os.environ.get(provider.api_key_env) else provider.api_key_file
            checks.append(check(f"{name} key", True, "info", f"{provider.api_key_env} resolves (from {where})"))
        else:
            checks.append(check(f"{name} key", False, "error",
                                f"{settings.missing_key_message(provider)}: set it before starting the "
                                f"factory; {used_by}"))
    url = settings.models_url(provider)
    local = settings.is_local_url(provider.base_url)
    status, listed, problem = settings.list_models(provider, HTTP_TIMEOUT)
    if status is None:
        checks.append(check(f"{name} reachable", False, "warn", f"{problem}; {used_by}"))
    elif status in (401, 403):
        fix = f"check {provider.api_key_env}" if key else "it needs a key: set api_key_env"
        checks.append(check(f"{name} reachable", False, "error",
                            f"{url} rejected the request (HTTP {status}): {fix}; {used_by}"))
    elif status >= 500:
        checks.append(check(f"{name} reachable", False, "warn", f"{url} answered HTTP {status}; {used_by}"))
    else:
        checks.append(check(f"{name} reachable", True, "info", f"{url} answered HTTP {status}; {used_by}"))
    if status is not None and status < 400:
        checks += _model_id_checks(provider, url, listed, models)
    if status is not None and status < 500 and routing.model_aware(provider):
        checks += _loaded_model_check(provider, models)
    if local and key and status is not None and status not in (401, 403):
        open_status, _ = settings.reach(provider, HTTP_TIMEOUT, authenticated=False)
        if open_status in (401, 403):
            checks.append(check(f"{name} auth", True, "info", "rejects requests without the key"))
        elif open_status is not None:
            checks.append(check(f"{name} auth", False, "warn",
                                f"{url} answers without the key (HTTP {open_status}): any local user or "
                                f"process can use the server; turn on its API key"))
    if provider.name == "ollama" or urllib.parse.urlsplit(provider.base_url).port == OLLAMA_PORT:
        context = max((model.context_tokens or DEFAULT_OLLAMA_CONTEXT for model in models),
                      default=DEFAULT_OLLAMA_CONTEXT)
        checks.append(check(f"{name} context", True, "info",
                            f"Ollama's default context is small: start it with OLLAMA_CONTEXT_LENGTH={context} "
                            f"(or PARAMETER num_ctx in a Modelfile) so agent prompts fit"))
    return checks


def _model_id_checks(provider: ProviderConfig, url: str, listed: list[str] | None,
                     models: list[ModelConfig]) -> list[Check]:
    """A warning for each configured model id the provider's model list does not have (preset ids are
    best guesses until checked here); nothing when the list could not be read."""
    name = f"provider {provider.name} models"
    wanted = sorted({model.model for model in models})
    if listed is None or not wanted:
        return [] if listed is not None else [check(name, True, "info", f"{url} did not return a model list to "
                                                                          f"check {', '.join(wanted)} against")]
    missing = [model_id for model_id in wanted if model_id not in listed]
    if not missing:
        return [check(name, True, "info", f"{', '.join(wanted)} listed by {url} ({len(listed)} models)")]
    hint = ("; a llama.cpp-style server serves the model it loaded whatever the id says"
            if settings.is_local_url(provider.base_url) else "; correct the id in Settings → Models")
    return [check(name, False, "warn", f"{url} does not list {', '.join(missing)} ({len(listed)} models listed)"
                                       f"{hint}")]


def _loaded_model_check(provider: ProviderConfig, models: list[ModelConfig]) -> list[Check]:
    """Which model a model-aware server (kind "tabby") has loaded, and whether the builds on it can run."""
    name = f"provider {provider.name} loaded model"
    loaded, why = routing.probe_loaded(provider, HTTP_TIMEOUT)
    if loaded is None:
        return [check(name, False, "warn", f"cannot tell which model it has loaded: {why}")]
    others = sorted({model.model for model in models} - {loaded})
    result = check(name, True, "info", f"{loaded} loaded")
    if others and not provider.allow_model_swap:
        result = check(name, False, "warn", f"{loaded} loaded; with allow_model_swap off, runs on "
                                            f"{', '.join(others)} go to their next build or overflow until it is")
    elif others:
        result["detail"] += f"; runs on {', '.join(others)} make it load them (allow_model_swap)"
    return [result]


def _driver_check(config: Config, name: str, users: list[str]) -> list[Check]:
    """Run the driver's command with --version exactly as the driver would start it: the same argv
    prefix (drivers.cli: an existing path with spaces is one program, a relative path is anchored at
    the config's directory) and the same environment (secrets removed, the driver's env applied)."""
    label = f"driver {name}"
    used_by = f"used by {', '.join(users)}"
    try:
        driver = config.driver(name)
        if driver.type != "cli":
            raise ValueError(f"drivers.{name} has an unknown type {driver.type!r} (expected builtin or cli)")
        runner = CliRunner(config, driver)
    except (KeyError, ValueError) as exc:
        return [check(label, False, "error", f"{exc}; {used_by}")]
    argv, env = runner.command, runner.environment()
    program = argv[0]
    found = shutil.which(program, path=env.get("PATH", os.defpath))
    if found is None:
        where = "is not on PATH" if os.sep not in program else "does not exist or is not executable"
        return [check(label, False, "error", f"command {program!r} {where}; {used_by}")]
    try:
        proc = subprocess.run([found, *argv[1:], "--version"], capture_output=True, text=True, errors="replace",
                              stdin=subprocess.DEVNULL, env=env, timeout=VERSION_TIMEOUT, check=False)
    except subprocess.TimeoutExpired:
        return [check(label, False, "error", f"{found} --version did not finish within {VERSION_TIMEOUT:g}s")]
    except OSError as exc:
        return [check(label, False, "error", f"cannot run {found}: {exc}")]
    output = (proc.stdout.strip() or proc.stderr.strip()).splitlines()
    first = output[0][:200] if output else "no output"
    if proc.returncode != 0:
        return [check(label, False, "error", f"{found} --version failed (exit {proc.returncode}): {first}")]
    return [check(label, True, "info", f"{found}: {first}; {used_by}")]


# ---------------------------------------------------------------- Open WebUI and personas (§33)


def _openwebui_jobs(config: Config) -> tuple[list[tuple[str, Callable[[], list[Check]]]], list[Check]]:
    """(one parallel job per Open WebUI provider, the passing notes of personas whose provider is not set up)."""
    from . import personas

    try:
        found = personas.discover_personas(config)
    except ValueError:
        found = {}   # the agents check reports the file that does not parse
    providers: dict[str, list[Any]] = {name: [] for name in personas.openwebui_providers(config)}
    notes: list[Check] = []
    configured = _dict(config.raw.get("providers"))
    for persona in [found[name] for name in sorted(found)]:
        if persona.kind not in ("openwebui", "snapshot"):
            continue
        name = personas.binding_provider(config, persona)
        if not isinstance(configured.get(name), dict):
            model_id = (persona.binding or {}).get("model")
            if persona.kind == "snapshot":
                notes.append(_persona_check(config, persona, None))
                continue
            notes.append(check(f"persona {persona.name}", True, "info",
                               f"bound to Open WebUI's {model_id!r}, not set up (optional): apply the openwebui preset "
                               f"(provider {name} and models.kate) and set {personas.OPENWEBUI_KEY_ENV} to chat with it"))
            continue
        providers.setdefault(name, []).append(persona)
    return [(f"openwebui {name}", lambda n=name, bound=bound: _openwebui_checks(config, n, bound))
            for name, bound in sorted(providers.items())], notes


def _openwebui_checks(config: Config, name: str, bound: list[Any]) -> list[Check]:
    from . import personas

    label = f"openwebui {name}"
    try:
        api = personas.client(config, provider=name)
    except (KeyError, ValueError, personas.OpenWebUIError) as exc:
        return [check(f"{label} reachable", False, "error", str(exc.args[0] if exc.args else exc))]
    api.timeout = HTTP_TIMEOUT
    used = f"; personas: {', '.join(p.name for p in bound)}" if bound else ""
    try:
        version = api.version()
    except personas.OpenWebUIError as exc:
        return [check(f"{label} reachable", False, "warn", f"{exc} (start Open WebUI){used}")]
    checks = [check(f"{label} reachable", True, "info", f"Open WebUI {version or '(it gives no version)'} at "
                                                         f"{api.root}{used}")]
    problem = api.key_problem()
    if problem:
        return checks + [check(f"{label} key", False, "error", problem)]
    try:
        entries = api.models()
    except personas.OpenWebUIError as exc:
        return checks + [check(f"{label} key", False, "error", str(exc))]
    try:
        open_status, _ = api.get("/models", authenticated=False)
    except personas.OpenWebUIError:
        open_status = None
    if open_status in (401, 403):
        checks.append(check(f"{label} key", True, "info", f"{api.provider.api_key_env} accepted; without a key Open "
                                                           f"WebUI answers HTTP {open_status}"))
    else:
        checks.append(check(f"{label} key", False, "warn", f"{api.provider.api_key_env} accepted, but GET /api/models "
                                                            f"also answers without a key (HTTP {open_status}): turn "
                                                            f"Open WebUI's sign-in on"))
    workspace = [entry for entry in entries if entry.get("preset")]
    listed = ", ".join(f"{entry['id']} ({entry.get('name') or entry['id']}, version "
                       f"{str(_dict(entry.get('info')).get('version_id') or '?')[:12]})" for entry in workspace)
    checks.append(check(f"{label} models", True, "info",
                        f"workspace models this key may use: {listed or 'none'}; "
                        f"{len(entries) - len(workspace)} other models"))
    return checks + [_persona_check(config, persona, entries) for persona in bound]


def _persona_check(config: Config, persona: Any, entries: list[dict] | None) -> Check:
    """One persona bound to (or copied from) an Open WebUI workspace model; entries None: no Open WebUI to ask
    (an offline snapshot is then only checked for staying local)."""
    from . import personas

    name = f"persona {persona.name}"
    model_id = (persona.binding or {}).get("model")
    key = personas.chat_model_key(config, persona)
    try:
        if persona.error:
            raise ValueError(persona.error)
        if key is None:
            raise ValueError(f"no models entry runs Open WebUI's {model_id!r}: apply the openwebui preset or name one "
                             f"in the persona file's model:")
        personas.check_model(config, persona, config.model(key))
    except (KeyError, ValueError) as exc:
        return check(name, False, "error", str(exc.args[0] if exc.args else exc))
    if entries is None:
        return check(name, True, "info", f"{personas.label(persona)} on models.{key}; no Open WebUI provider is "
                                         f"configured to compare it with the live {model_id!r}")
    entry = personas.listed(entries, model_id)
    if entry is None:
        level = "warn" if persona.kind == "snapshot" else "error"
        return check(name, False, level, f"Open WebUI does not list {model_id!r} for this key (models.{key})")
    info = _dict(entry.get("info"))
    drift = personas.check_drift(config, model_id, info, write=False)
    meta = _dict(info.get("meta"))
    binding = persona.binding or {}
    missing = [part["part"] for part in personas.parts(meta, binding.get("knowledge", False), binding.get("filters", True))
               if not part["through_api"]]
    base = personas.base_model_view(entries, entry)
    detail = (f"{personas.label(persona)} on models.{key}: Open WebUI lists {entry.get('name') or model_id} (version "
              f"{str(info.get('version_id') or '?')[:12]}, base model {info.get('base_model_id') or '?'}"
              + (f", whose connection Open WebUI labels {base['connection_type']}"
                 if base and base.get("listed") and base.get("connection_type") else "") + ")")
    if missing:
        detail += f"; not through the API: {', '.join(missing)}"
    if persona.kind == "snapshot":
        if info.get("version_id") != binding.get("version_id"):
            return check(name, False, "warn", detail + f"; the snapshot is of version {str(binding.get('version_id'))[:12]}: "
                                                       f"re-import it to follow Open WebUI")
        return check(name, True, "info", detail + "; the snapshot matches the live version")
    if drift["kind"] in ("behaviour", "unknown"):
        return check(name, False, "warn", detail + f"; changed in Open WebUI since the last check ({drift['kind']}): "
                                                   f"run the drift check (POST /api/personas/check) and review it")
    warnings = personas.base_model_warnings(base, str(entry.get("name") or model_id))
    if warnings:
        return check(name, False, "warn", detail + "; " + "; ".join(warnings))
    words = {"new": "not checked before", "none": "unchanged since the last check",
             "cosmetic": "changed cosmetically since the last check"}
    return check(name, True, "info", detail + f"; {words.get(drift['kind'], drift['kind'])}")


def _model_probes(config: Config, routes: list[Route]) -> list[Check]:
    """The Test probes for every routed model key, sorted by key.

    Keys whose entries resolve to the same model (tiers that copy `local`, for example)
    are probed once and share the result. Probes of one provider run in at most
    max_concurrency lanes, one probe after another per lane, so the doctor's own probes
    never queue for a slot past their timeout; different providers run in parallel.
    """
    models: dict[str, ModelConfig] = {}
    for route in routes:
        if route.error is None and route.is_key and route.model is not None:
            models.setdefault(route.ref, route.model)
    groups: dict[str, list[str]] = {}
    for key in sorted(models, key=lambda k: (k in settings.TIERS, k)):  # a named entry before the tiers copying it
        groups.setdefault(_model_identity(models[key]), []).append(key)
    by_provider: dict[str, list[list[str]]] = {}
    for keys in groups.values():
        by_provider.setdefault(models[keys[0]].provider.name, []).append(keys)
    jobs: list[tuple[str, Callable[[], list[Check]]]] = []
    longest = 0
    for provider_groups in by_provider.values():
        width = models[provider_groups[0][0]].provider.max_concurrency or len(provider_groups)
        for lane in (provider_groups[start::width] for start in range(min(width, len(provider_groups)))):
            name = "model " + ", ".join(keys[0] for keys in lane)
            jobs.append((name, lambda lane=lane: [c for keys in lane for c in _probe_group(config, keys)]))
            longest = max(longest, len(lane))
    checks = _parallel(jobs, longest * settings.probe_budget() + GRACE_SECONDS)
    return sorted(checks, key=lambda c: c["name"])


def _model_identity(model: ModelConfig) -> str:
    """What a probe actually sends: everything about the resolved model except its key.

    Top-level null extra_body keys are left out: the provider drops them from the
    request, and a saved config writes them for default keys an entry replaced (a
    tier copying `local` gets {"thinking": null, "reasoning_effort": null}).
    """
    data = asdict(model)
    data.pop("key", None)
    data["extra_body"] = {key: value for key, value in (model.extra_body or {}).items() if value is not None}
    return json.dumps(data, sort_keys=True, default=str)


def _probe_group(config: Config, keys: list[str]) -> list[Check]:
    """Probe keys[0]; the other keys resolve to the same model and get its result."""
    first, *copies = keys
    checks = _probe(config, first)
    result = checks[0]
    for key in copies:
        checks.append(check(f"model {key}", result["ok"], result["level"],
                            f"{result['detail']} (same model as {first}, probed once)"))
    return checks


def _probe(config: Config, key: str) -> list[Check]:
    result = settings.test_model(config, key)
    name = f"model {key}"
    if not result["ok"]:
        return [check(name, False, "error", result["error"] or "the model did not answer")]
    speed = f", {result['tokens_per_second']} tokens/s" if result.get("tokens_per_second") else ""
    summary = f"replied in {result['latency_ms']} ms{speed}"
    if not result["tool_call"]:
        why = result["error"] or "it answered without calling get_time"
        return [check(name, False, "warn", f"{summary}, but the tool-call probe failed: {why} (local servers: "
                                            f"llama.cpp needs --jinja; check the model's tool-call template)")]
    return [check(name, True, "info", f"{summary}; tool calling works")]


# ---------------------------------------------------------------- setup status (GET /api/setup)

SETUP_STEPS = ("providers", "models", "routing", "repos", "tools", "output")
REQUIRED_STEPS = ("providers", "models", "routing", "repos")


def setup_status(config: Config, factory: Any = None) -> dict:
    """{configured, steps: {providers, models, routing, repos, tools, output}: {ok, detail}}, offline.

    configured is true when providers, models, routing and repos are ok (tools and output are
    optional). Never raises: a step whose check breaks is reported not ok with the reason.
    """
    lookup = _agent_lookup(config, factory)
    agents: dict[str, AgentDef] = {}
    agents_error = None
    try:
        agents = lookup(None)
    except Exception as exc:  # reported in the routing step
        agents_error = str(exc)
    try:
        routes, _ = _routes(config, agents)
    except Exception as exc:  # reported in the models step
        routes, routes_error = [], f"{type(exc).__name__}: {exc}"
    else:
        routes_error = None
    steps = {
        "providers": _step(lambda: _setup_providers(routes)),
        "models": _step(lambda: _setup_models(config, routes, routes_error)),
        "routing": _step(lambda: _setup_routing(config, agents, routes, agents_error)),
        "repos": _step(lambda: _setup_repos(config)),
        "tools": _step(lambda: _setup_tools(config, agents)),
        "output": _step(lambda: _setup_output(config)),
    }
    return {"configured": all(steps[name]["ok"] for name in REQUIRED_STEPS), "steps": steps}


def _step(run: Callable[[], tuple[bool, str]]) -> dict:
    try:
        ok, detail = run()
    except Exception as exc:  # a broken check is a step that needs attention
        ok, detail = False, f"check failed: {type(exc).__name__}: {exc}"
    return {"ok": bool(ok), "detail": detail}


def _setup_providers(routes: list[Route]) -> tuple[bool, str]:
    providers = _providers(routes)
    if not providers:
        return False, "no provider is in use: route the tiers and agents to models first"
    missing, ready = [], []
    for provider, users, _ in providers:
        if provider.api_key_env and not provider.api_key():
            missing.append(f"{provider.api_key_env} ({provider.name}, used by {', '.join(users[:3])}"
                           f"{', …' if len(users) > 3 else ''})")
        else:
            ready.append(provider.name + ("" if provider.api_key_env else " (no key needed)"))
    if missing:
        return False, "missing keys: " + "; ".join(missing)
    return True, "keys present for " + ", ".join(ready)


def _setup_models(config: Config, routes: list[Route], error: str | None) -> tuple[bool, str]:
    if error is not None:
        return False, error
    broken = [route.describe() for route in routes if route.error is not None]
    if broken:
        return False, "; ".join(broken)
    tiers = [f"{route.ref} → {route.model.model} ({route.model.provider.name})" for route in routes
             if route.driver is None and route.model is not None]
    models = _dict(config.raw.get("models"))
    count = sum(1 for entry in models.values() if isinstance(entry, dict))
    return True, f"{count} model entries; " + ", ".join(tiers)


def _setup_routing(config: Config, agents: dict[str, AgentDef], routes: list[Route],
                   error: str | None) -> tuple[bool, str]:
    if error is not None:
        return False, error
    reviewers = config.pipeline().reviewers
    missing = [name for name in required_agents(reviewers) if name not in agents]
    if missing:
        return False, f"missing agents: {', '.join(missing)}"
    agent_routes = [route for route in routes if route.driver is not None]
    broken = [route.label for route in agent_routes if route.error is not None]
    if broken:
        return False, f"routes that do not resolve: {', '.join(broken)}"
    local = sum(1 for route in agent_routes if route.model is not None and route.builtin
                and settings.is_local_url(route.model.provider.base_url))
    cli = sum(1 for route in agent_routes if not route.builtin)
    parts = [f"{len(agent_routes)} agents routed", f"{local} on local models",
             f"{len(agent_routes) - local - cli} on API models"]
    if cli:
        parts.append(f"{cli} on CLI drivers")
    return True, ", ".join(parts)


def _setup_repos(config: Config) -> tuple[bool, str]:
    repos = config.repos()
    if not repos:
        return False, "no repos yet: add one (GUI or factory repo add <path>)"
    broken = [repo.name for repo in repos if not (dir_exists(repo.path) and os.path.lexists(repo.path / ".git"))]
    if broken:
        return False, f"not a git checkout any more: {', '.join(broken)}"
    return True, ", ".join(f"{repo.name} ({repo.path})" for repo in repos)


def _setup_tools(config: Config, agents: dict[str, AgentDef]) -> tuple[bool, str]:
    checks = [*_skills_dirs(config), *_mcp_checks(config, agents)]
    harness = config.raw.get("harness")
    sandbox = str(harness.get("sandbox") or "none") if isinstance(harness, dict) else "none"
    failed = [f"{c['name']}: {c['detail']}" for c in checks if not c["ok"]]
    if failed:
        return False, "; ".join(failed)
    skills = [c for c in checks if c["name"].startswith("skills dir")]
    mcp = [c for c in checks if c["name"].startswith("mcp ")]
    absent = [c["name"][len("mcp "):] for c in mcp if c["detail"].startswith(NOT_CONFIGURED)]
    used = [c["name"][len("mcp "):] for c in mcp if not c["detail"].startswith(NOT_CONFIGURED)]
    parts = [f"{len(skills)} skills dir(s)" if skills else "no extra skills dirs",
             f"MCP: {', '.join(used)}" if used else "no MCP servers in use"]
    if absent:
        parts.append(f"not configured, Chat only: {', '.join(absent)}")
    return True, "; ".join([*parts, f"sandbox {sandbox}"])


def _setup_output(config: Config) -> tuple[bool, str]:
    merge = config.merge()
    if merge.delivery == "merge":
        return True, "merge: approved jobs merge into the base branch" + (
            f" and push to {merge.remote}" if merge.push else "")
    if merge.delivery == "branch":
        return True, "branch: approved jobs stay on their branch for you to merge"
    directory = config.output_dir()
    probe = directory
    while not os.path.lexists(probe) and probe != probe.parent:
        probe = probe.parent
    if not dir_exists(probe) or not os.access(probe, os.W_OK | os.X_OK):
        return False, f"folder: {directory} cannot be written"
    return True, f"folder: approved jobs are exported to {directory}/<repo>/<job-id>/"


# ---------------------------------------------------------------- plumbing


def _safely(name: str, run: Callable[[], list[Check]]) -> list[Check]:
    try:
        return run()
    except Exception as exc:  # a broken check is reported, never raised
        return [check(name, False, "error", f"check failed: {type(exc).__name__}: {exc}")]


def _parallel(jobs: list[tuple[str, Callable[[], list[Check]]]], timeout: float) -> list[Check]:
    """Run each job in its own thread; results in job order, a job still running at timeout reports so."""
    results: list[list[Check] | None] = [None] * len(jobs)

    def run(index: int, name: str, job: Callable[[], list[Check]]) -> None:
        results[index] = _safely(name, job)

    threads = [threading.Thread(target=run, args=(index, name, job), name=f"doctor-{name}", daemon=True)
               for index, (name, job) in enumerate(jobs)]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
    checks: list[Check] = []
    for (name, _), result in zip(jobs, list(results), strict=True):
        checks += result if result is not None else [check(name, False, "warn", f"no result within {timeout:g}s")]
    return checks


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}
