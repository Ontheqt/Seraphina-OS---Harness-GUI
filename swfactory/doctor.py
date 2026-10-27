"""Doctor: environment and configuration checks for `factory doctor` and GET /api/doctor.

Each check is ``{name, ok, level, detail}``. ``level`` is the severity of a failed
check (``error`` or ``warn``) or ``info`` for passing checks and advice. run_checks
never raises; every check that touches the network or runs a program has its own
timeout, and the slow, independent ones (providers, drivers, model probes) run in
parallel threads.

setup_status is the offline summary behind GET /api/setup (the GUI's Welcome page and
Grid Card): one {ok, detail} per setup step, from the same checks without the network.

The look-around for model servers (one GET /v1/models, without a key, to each program listening on
this machine: listeners.model_servers) runs only when the owner asks for it: POST
/api/setup/look-around (Factory.look_around, through LookAround's rate limit) or `factory doctor
--look-around` (run_checks(look_around=True)). GET /api/setup and GET /api/doctor, which the GUI
reads on its own, never run it: setup_status shows the last result the owner asked for, with when
it was taken, and the "local servers" line says how to ask.

Placement (DESIGN §23): the providers of models' builds and overflows, of the agents'
escalation rungs and of the routing profiles are checked like routed ones; each provider's
model list is read and a configured model id it does not list is a warning; a model-aware
server (kind "tabby") reports the model it has loaded; one line says which local servers are up.

Messages (DESIGN §34): an error while a phone channel is on and agents run without bwrap or through a CLI
driver (S46, also while the typed acknowledgement stands); the time zone and the power timers; quiet hours;
each event source's digest; the sensitive-terms file; time sync; Seraphina stopped; the notifier's last tick;
and, in parallel, each enabled channel: ntfy's socket, health, an authenticated poll of each topic,
server.yml without forwarders and no Funnel; Telegram's getMe, no webhook and a paired account.

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
from .harness.sandbox import bwrap_available, nested_bwrap
from .skills import find_skill_files

HTTP_TIMEOUT = 5.0
VERSION_TIMEOUT = 10.0
GRACE_SECONDS = 5.0
MIN_PYTHON = (3, 10)
NEWEST_TESTED_PYTHON = (3, 14)   # the suite runs on 3.10 to 3.14 (DESIGN §1)
INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md", "QWEN.md")
EXTRA_AGENTS = ("lead", "coder", "repo-scout")
OLLAMA_PORT = 11434
CHANNEL_KWARGS: dict[str, Any] = {}   # tests point the channels at mock servers (api_base); never from config
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


def run_checks(config: Config, factory: Any = None, probe_models: bool = False,
               look_around: bool = False, look_skip_ports: set[int] | frozenset[int] = frozenset()) -> list[Check]:
    """Every check, in display order; never raises.

    factory (optional) supplies the agents it really runs; probe_models also sends every
    routed model the two Test probes (this costs model calls; keys that resolve to the
    same model are probed once, and a provider never gets more probes at once than
    its max_concurrency); look_around (`factory doctor --look-around`, never the server's
    GET /api/doctor) also looks for model servers on this machine once (look_for_model_servers):
    a "model servers here" line, and the "local servers" line names those on other ports. look_skip_ports: more
    ports that look skips besides the config's server.port (cmd_doctor: the one a running `factory serve` records).
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
        checks += _safely("account", lambda: _rights(config, agents))
        checks += _safely("mcp", lambda: _mcp_checks(config, agents))
    checks += _safely("privileges", lambda: _privileges(config))
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
    jobs += _custody_jobs(config, in_server=factory is not None)
    checks += _safely("custody", lambda: _custody_off(config))
    checks += _safely("drift sources", lambda: _drift_checks(config, in_server=factory is not None))
    checks += _safely("messages", lambda: _messages_checks(config))
    checks += _safely("wakes", lambda: _wake_checks(config))
    checks += _safely("operator", lambda: _operator_checks(config))
    checks += _safely("snapraid", lambda: _snapraid_checks(config))
    checks += _safely("your server", lambda: _yourserver_checks(config))
    checks += _safely("browser", lambda: _browser_checks(config))
    checks += _safely("codex", lambda: _codex_checks(config, probe_models))
    jobs += _channel_jobs(config)
    found = _parallel(jobs, 3 * HTTP_TIMEOUT + VERSION_TIMEOUT + GRACE_SECONDS)
    looked = look_for_model_servers(config, skip_ports=look_skip_ports) if look_around else None
    checks += found + _local_servers([provider for provider, _, _ in providers], found, config, looked)
    if looked is not None:
        checks.append(_looked_check(looked))
    checks += persona_notes
    if probe_models:
        checks += _safely("models", lambda: _model_probes(config, routes))
    return checks


# ---------------------------------------------------------------- local checks


def _python(version: tuple[int, int] | None = None, shown: str | None = None) -> list[Check]:
    version = version or tuple(sys.version_info[:2])
    ok = version >= MIN_PYTHON
    detail = f"Python {shown or platform.python_version()}" + ("" if ok else ": Software Factory needs Python 3.10 or newer")
    if version > NEWEST_TESTED_PYTHON:
        detail += (f" (newer than {NEWEST_TESTED_PYTHON[0]}.{NEWEST_TESTED_PYTHON[1]}, the newest the test suite runs on: "
                   f"it should work; say so if something does not)")
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
    """bwrap probed in the form every command runs in (a private /tmp, no network), then whether a sandbox works
    inside it: a named limitation (no browser in agents' Bash), never a failure of harness.sandbox."""
    harness = config.raw.get("harness")
    mode = harness.get("sandbox") if isinstance(harness, dict) else None
    if mode == "bwrap":
        if not bwrap_available():
            return [check("sandbox", False, "error", "harness.sandbox is bwrap but bubblewrap is missing or cannot "
                                                      "create a sandbox here: install it (apt install bubblewrap) "
                                                      "or set harness.sandbox to none")]
        checks = [check("sandbox", True, "info", "bubblewrap works: Bash runs sandboxed (checked with a private "
                                                 "/tmp and without network, as each command runs)")]
        nested, why = nested_bwrap()
        if nested:
            return checks + [check("sandbox inside the sandbox", True, "info", "works here: a browser can start "
                                                                               "inside agents' Bash")]
        browser = shutil.which("agent-browser")
        return checks + [check("sandbox inside the sandbox", False, "warn" if browser else "info",
                               f"not allowed on this server ({why}): inside agents' Bash a browser (agent-browser, "
                               "Chromium, Playwright) and any tool that builds its own sandbox cannot start, so "
                               "agents check pages with curl (their prompt says so). The harness never puts a CLI "
                               "agent such as Codex or Claude Code inside its sandbox. Keep the server's "
                               "user-namespace restriction on: do not loosen it to make this work")]
    return [check("sandbox", True, "info", "harness.sandbox is none: agents' Bash commands are not contained; "
                                           "run the factory as a dedicated Unix user or set it to bwrap")]


ROOT_GROUPS = ("docker", "lxd", "incus-admin", "sudo", "admin", "wheel", "disk")   # each is root, or a step from it
GROUP_WORDS = {"docker": "the Docker socket starts root containers", "lxd": "the LXD socket starts root containers",
               "incus-admin": "the Incus socket starts root containers", "sudo": "sudo with the account's password",
               "admin": "sudo with the account's password", "wheel": "sudo with the account's password",
               "disk": "raw read and write access to every disk"}


def account_groups(uid: int | None = None) -> tuple[str, list[str]]:
    """(user name, the groups of this process and of the user in the group database, sorted). A group the account
    was given after this process started is listed too: the harness gets it at its next start."""
    import grp
    import pwd

    uid = os.getuid() if uid is None else uid
    try:
        name = pwd.getpwuid(uid).pw_name
    except KeyError:
        name = str(uid)
    names = set()
    for gid in {os.getgid(), *os.getgroups()} if uid == os.getuid() else set():
        try:
            names.add(grp.getgrgid(gid).gr_name)
        except KeyError:
            continue
    try:
        names.update(group.gr_name for group in grp.getgrall() if name in group.gr_mem)
    except OSError:
        pass
    return name, sorted(names)


def _rights(config: Config, agents: dict[str, AgentDef], identity: tuple[int, str, list[str]] | None = None
            ) -> list[Check]:
    """The harness's account: root, or a member of a group that is root (docker, lxd, sudo, disk, ...), is a
    warning, and an error when an agent runs commands outside bwrap (harness.sandbox none, or a CLI driver).
    identity: (uid, name, groups) for tests; read from this process otherwise."""
    from .notify import sandbox_mode, uses_cli_driver

    if identity is None:
        name, groups = account_groups()
        identity = (os.getuid(), name, groups)
    uid, name, groups = identity
    risky = [group for group in ROOT_GROUPS if group in groups]
    if uid != 0 and not risky:
        return [check("account", True, "info", f"the harness runs as {name}, in none of the groups that give root "
                                               f"({', '.join(ROOT_GROUPS)})")]
    what = ("root" if uid == 0 else f"{name}, a member of {', '.join(risky)} ("
            + "; ".join(f"{group}: {GROUP_WORDS[group]}" for group in risky) + ")")
    bash = any(any(str(tool).strip().lower() == "bash" for tool in agent.tools) for agent in agents.values())
    outside = sandbox_mode(config) != "bwrap" or uses_cli_driver(config)
    if bash and outside:
        why = "harness.sandbox is none" if sandbox_mode(config) != "bwrap" else "an agent route uses a CLI driver"
        return [check("account", False, "error", f"the harness runs as {what}, and {why}: any agent command can "
                                                 "become root on this machine. Run the harness as its own user, in "
                                                 "none of these groups (DESIGN §17)")]
    return [check("account", False, "warn", f"the harness runs as {what}. Under bwrap agents cannot reach raw "
                                            "disks, sudo cannot raise their rights, and the container sockets there "
                                            "when a run starts are hidden, but a socket made during a run (Docker or "
                                            "LXD restarted, or installed) is not, and the harness itself and "
                                            "anything it starts outside the sandbox can become root: run it as its "
                                            "own user, in none of these groups (DESIGN §17)")]


def sudo_programs(which: Callable[[str], str | None] = shutil.which,
                  run: Callable[[list[str]], str] | None = None) -> list[dict]:
    """Each sudo program here: [{name, path, implementation: "sudo-rs" | "sudo" | "unknown", version}], sudo first.
    Ubuntu 25.10 and later ship sudo-rs as sudo and keep classic sudo as sudo.ws; both read /etc/sudoers*."""
    def version(path: str) -> str:
        if run is not None:
            return run([path, "--version"])
        try:
            done = subprocess.run([path, "--version"], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                  errors="replace", timeout=VERSION_TIMEOUT, env={**os.environ, "LC_ALL": "C"})
        except (OSError, subprocess.SubprocessError):
            return ""
        return done.stdout or ""
    found = []
    for name in ("sudo", "sudo.ws", "sudo-rs"):
        path = which(name)
        if not path:
            continue
        real = os.path.realpath(path)
        if any(item["real"] == real for item in found):
            continue
        first = (version(path).strip().splitlines() or [""])[0].strip()
        if first.lower().startswith("sudo-rs") or "/cargo/" in real:
            kind, number = "sudo-rs", first.split()[-1] if first.lower().startswith("sudo-rs") else ""
        elif first.startswith("Sudo version"):
            kind, number = "sudo", first.split()[-1]
        else:
            kind, number = "unknown", ""
        found.append({"name": name, "path": path, "real": real, "implementation": kind, "version": number})
    return found


def _privileges(config: Config, programs: list[dict] | None = None, nnp: bool | None = None,
                setpriv: str | None = "") -> list[Check]:
    """sudo's implementations (sudo-rs and classic sudo can both be installed, and read the same sudoers files
    differently), whether this process runs with no-new-privileges (sudo cannot run from it then), and whether
    agents' programs outside bwrap start without new privileges (setpriv). The arguments are for tests."""
    from .harness.sandbox import no_new_privs_set, setpriv_path
    from .notify import sandbox_mode, uses_cli_driver

    programs = sudo_programs() if programs is None else programs
    nnp = no_new_privs_set() if nnp is None else nnp
    setpriv = setpriv_path() if setpriv == "" else setpriv
    checks = []
    if programs:
        names = {"sudo-rs": "sudo-rs", "sudo": "classic sudo", "unknown": "a sudo of unknown kind"}
        words = [f"{item['name']} is {names[item['implementation']]} {item['version']}".strip() for item in programs]
        kinds = {item["implementation"] for item in programs}
        detail = "; ".join(words)
        if {"sudo-rs", "sudo"} <= kinds:
            detail += (". Both read /etc/sudoers and its folder, but not always alike: a rule one ignores the other "
                       "may honour, so check an account's grants with each (sudo -l -U <user>, sudo.ws -l -U <user>)")
        checks.append(check("sudo", True, "info", detail))
    if nnp:
        checks.append(check("no new privileges", True, "info", "this process runs with no-new-privileges (its unit "
                            "sets NoNewPrivileges=yes): sudo cannot run from it, so helpers that need sudo (the disk "
                            "protection reader) cannot be reached. The shipped units leave it off; agents get it "
                            "per command"))
    elif setpriv:
        checks.append(check("no new privileges", True, "info", "agents' commands outside bwrap, CLI agents, MCP "
                            "servers and the harness's own git (whose filter and merge drivers come from the repo's "
                            "config) start with no-new-privileges (setpriv), so no setuid program such as sudo "
                            "gives them more rights than the harness's user; bwrap sets it for commands inside"))
    else:
        outside = sandbox_mode(config) != "bwrap" or uses_cli_driver(config)
        checks.append(check("no new privileges", False, "warn" if outside else "info", "setpriv (util-linux) is "
                            "not installed: agents' programs outside bwrap (CLI agents, MCP servers, every command "
                            "with harness.sandbox none), and a filter or merge driver an agent sets in a repo's "
                            "config for the harness's own git, could use setuid programs such as sudo. Install "
                            "util-linux"))
    return checks


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
    each routed model, its overflow, the agents' escalation rungs and independent alternates and every
    profile's models, rungs and alternates, so their providers get checked too; and one placement check:
    an error naming the first reference that does not resolve, else what is configured (nothing when
    there is none)."""
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
                routing.check_placement({key: entry[key] for key in ("effort", "escalate", "reason",
                                                                     "independent_alternate") if key in entry},
                                        f"agents.{name}", models, agent=str(name))
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
        if _dict(entry).get("independent_alternate"):
            keys.append((str(entry["independent_alternate"]), f"{name} independent alternate", builtin))
    profiles = _dict(raw.get("profiles"))
    for profile, body in profiles.items():
        for name, entry in _dict(_dict(body).get("agents")).items():
            builtin = _builtin(config, _dict(entry).get("driver"), name)
            for key in [_dict(entry).get("model"), *(_dict(entry).get("escalate") or []),
                        _dict(entry).get("independent_alternate")]:
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
    alternates = sum(1 for entry in _dict(raw.get("agents")).values() if _dict(entry).get("independent_alternate"))
    if not (counts["builds"] or counts["overflows"] or ladders or alternates or profiles):
        return extra, []
    default = raw.get("default_profile")
    detail = (f"{counts['builds']} extra builds, {counts['overflows']} overflows, {ladders} escalation ladders, "
              + (f"{alternates} independent alternates, " if alternates else "")
              + f"{len(profiles)} profiles" + (f" (default {default})" if default else "") + "; references resolve")
    return extra, [check("placement", True, "info", detail)]


def _builtin(config: Config, driver: Any, agent: str) -> bool:
    try:
        return config.driver(str(driver or config.agent_driver(agent))).type == "builtin"
    except (KeyError, ValueError):
        return True


def _local_servers(providers: list[ProviderConfig], checks: list[Check], config: Config | None = None,
                   looked: dict | None = None) -> list[Check]:
    """One line on the local servers the routes use: which answered (and what a model-aware one has loaded). When
    one is down, it names the model servers the owner's look-around found on other ports (looked: what
    look_for_model_servers returned, from `factory doctor --look-around`), so a server on another port than the
    preset's placeholder is found; without one it says how to look (LOOK_AROUND_ADVICE): it never looks itself."""
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
    if down:
        detail += _other_model_servers(local, looked) if looked is not None else "; " + LOOK_AROUND_ADVICE
    if up:
        return [check("local servers", True, "info", detail)]
    return [check("local servers", False, "warn", detail + " (start the one that should hold the GPUs; placements "
                                                           "fall to the next build or the overflow)")]


def _other_model_servers(local: list[ProviderConfig], looked: dict) -> str:
    """"; answering /v1/models elsewhere on this machine: ..." for the model servers the look-around found on ports
    no configured local server uses, or what it found instead (nothing, or a look that broke)."""
    if looked.get("error"):
        return f"; the look for other model servers here did not work ({looked['error']})"
    used = {urllib.parse.urlsplit(provider.base_url).port for provider in local}
    found = [item for item in looked.get("model_servers") or [] if item.get("port") not in used]
    if not found:
        return "; the look-around found no other model server on this machine"
    return ("; answering /v1/models elsewhere on this machine: " + ", ".join(_server_text(item) for item in found)
            + ". If one is the server that is down, set that provider's base_url to it (the presets' ports are "
              "placeholders)")


def _server_text(item: dict) -> str:
    """One model server the look-around found: "http://127.0.0.1:5005/v1 (lists a, b, c …)" or "(wants a key)"."""
    ids = item.get("ids") or []
    what = (f"lists {', '.join(ids[:3])}{' …' if len(ids) > 3 else ''}" if item.get("status") == 200
            else "wants a key")
    return f"{item.get('url')} ({what})"


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
                                                         f"{api.root}{used} (the version it reports; the build is "
                                                         f"checked on its own line)"),
              _openwebui_build(config, name)]
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


def _openwebui_build(config: Config, name: str, facts: dict | None = None) -> Check:
    """The running Open WebUI build against providers.<name>.reviewed_build (DESIGN §33): "not checked" while what
    runs cannot be read (personas.build_facts), never "ok" from the version it reports. facts: for tests."""
    from . import personas

    pin = personas.reviewed_build(config, name)
    found = personas.compare_build(pin, facts if facts is not None else personas.build_facts(config, name))
    label = f"openwebui {name} build"
    if found["state"] == "matches":
        return check(label, True, "info", found["words"])
    if found["state"] == "changed":
        return check(label, False, "error", found["words"])
    if found["state"] == "no_pin":
        return check(label, False, "warn", found["words"])
    return check(label, False, "warn" if pin else "info", found["words"] + ": the persona contract (§33) was read "
                 "from Open WebUI's upstream source, so a local or customised build is unverified")


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
    warnings = personas.base_model_warnings(base, str(entry.get("name") or model_id),
                                            personas.local_model_ids(config))
    if warnings:
        return check(name, False, "warn", detail + "; " + "; ".join(warnings))
    words = {"new": "not checked before", "none": "unchanged since the last check",
             "cosmetic": "changed cosmetically since the last check"}
    return check(name, True, "info", detail + f"; {words.get(drift['kind'], drift['kind'])}")


# ---------------------------------------------------------------- messages (section 34)


# ---------------------------------------------------------------- GPU custody and drift sources (§37, §38)


def _custody_jobs(config: Config, in_server: bool = False) -> list[tuple[str, Callable[[], list[Check]]]]:
    """One check per provider under a GPU custody register (its own, or that of another entry on the same server,
    which it follows): does the register answer its residency? in_server: the running server's doctor (GET
    /api/doctor), not a command in a shell (_shell_credential)."""
    from .custody import custody_on_server

    providers = _dict(config.raw.get("providers"))
    jobs = []
    for name, entry in providers.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("custody") is not None or custody_on_server(providers, entry.get("base_url"), skip=str(name)):
            jobs.append((f"custody {name}", lambda n=str(name): _custody_check(config, n, in_server)))
    return jobs


def _custody_off(config: Config) -> list[Check]:
    """With no GPU custody register configured, an info line that says so, and for the local servers the harness
    may load or swap models on (lazy_load, allow_model_swap) that nothing coordinates those loads with what else
    uses the cards here. Info only: custody is off by default and needs the operator's register (DESIGN §37)."""
    providers = _dict(config.raw.get("providers"))
    if any(isinstance(entry, dict) and entry.get("custody") is not None for entry in providers.values()):
        return []
    loaders = sorted(str(name) for name, entry in providers.items() if isinstance(entry, dict)
                     and (entry.get("lazy_load") is True or entry.get("allow_model_swap") is True))
    if not loaders:
        return [check("custody", True, "info", "off: no GPU custody register on this host. None is needed while the "
                                               "harness never loads or swaps a model itself (no provider sets "
                                               "lazy_load or allow_model_swap; DESIGN §37)")]
    verb = "loads or swaps" if len(loaders) == 1 else "load or swap"
    return [check("custody", True, "info", f"off: no GPU custody register on this host is asked before "
                                           f"{', '.join(loaders)} {verb} a "
                                           "model, so other programs that use the cards (timers, containers, other "
                                           "model servers) are not coordinated with those loads (DESIGN §37). A "
                                           "register names cards by UUID (GPU-...), never by number")]


def _token_problem(token_file: str | None, token_env: str | None, token_credential: str | None) -> str | None:
    """Why a configured Bearer token cannot be read now, in plain words, or None (also when none is configured).
    For a systemd credential the words name it and the LoadCredential= line to add to the harness's unit."""
    from .plainhttp import credential_problem, in_credentials_dir, read_bearer

    if not (token_file or token_env or token_credential) or read_bearer(token_file, token_env, token_credential):
        return None
    if token_credential:
        return credential_problem(token_credential)
    if token_file and in_credentials_dir(token_file):
        return (f"its token_file {token_file} is inside a systemd credentials directory, which the harness never "
                f"reads: name the harness's own copy with token_credential")
    return "its Bearer token cannot be read (" + (f"token_file {token_file}" if token_file
                                                  else f"token_env {token_env}") + ")"


def _shell_credential(token_credential: str | None, in_server: bool) -> str | None:
    """Why a command in a shell (factory doctor, not the running server) cannot check a token that is a systemd
    credential, in plain words, or None: only the harness's own unit gives it its credentials, so in a shell
    CREDENTIALS_DIRECTORY is not set. That is a warning, not an error: the running server's doctor checks it."""
    from .plainhttp import credentials_present

    if not token_credential or in_server or credentials_present():
        return None
    return (f"the token is the systemd credential {token_credential}, which only the harness's own unit gives it: "
            f"this command was not started by that unit (CREDENTIALS_DIRECTORY is not set), so it cannot check the "
            f"credential. The running server's doctor (GET /api/doctor, the GUI's doctor) does; when it says the "
            f"credential is missing, add LoadCredential={token_credential}:<path to the token file> to the "
            f"harness's unit and restart it")


def _custody_check(config: Config, name: str, in_server: bool = False) -> list[Check]:
    from .custody import client_for, custody_on_server, needs_lease, settings_of

    label = f"custody {name}"
    try:
        provider = config.provider(name)
    except (KeyError, ValueError) as exc:
        return [check(label, False, "error", str(exc))]
    settings = settings_of(provider)
    if settings is None:
        return []
    entry = _dict(config.raw.get("providers")).get(name)
    owner = custody_on_server(config.raw.get("providers"), provider.base_url, skip=name) \
        if isinstance(entry, dict) and entry.get("custody") is None else None
    follows = f" (it follows {owner[0]}'s custody: the same server)" if owner else ""
    if not needs_lease(provider):
        return [check(label, False, "warn", f"{settings.url} is configured, but neither lazy_load nor allow_model_swap "
                                           f"is set on {name}: no call there takes a lease{follows}")]
    fallback = _custody_fallback(config, name)
    token = _token_problem(settings.token_file, settings.token_env, settings.token_credential)
    shell = _shell_credential(settings.token_credential, in_server)
    if token and not shell:
        return [check(label, False, "error", f"{token}: every run on {name} that needs a lease is unavailable and "
                                             f"follows its overflow{follows}")] + fallback
    if shell:   # still ask the register (without the token), so a register that is down is still an error
        fallback = [check(f"{label} credential", False, "warn", shell)] + fallback
    client = client_for(settings)
    seen = client.reread()
    if seen is None:
        if client.fenced:
            fenced = client.fenced
            return [check(label, False, "error", f"the register's generation went back from {fenced['seen']} to "
                                                 f"{fenced['answered']} (since {fenced['since']}): its answers are "
                                                 f"refused as stale until the generation passes {fenced['seen']} or "
                                                 f"this harness restarts, so every run on {name} that needs a lease "
                                                 f"follows its overflow. A register should send an epoch (from the "
                                                 f"host's boot id and a counter it keeps) and keep its generation "
                                                 f"growing within it (DESIGN §37){follows}")] + fallback
        history = client.history
        why = str(history[-1].get("reason") if history else "no answer")
        if shell and not why.startswith(("UNREACHABLE", "TIMEOUT")):   # it answered: perhaps only for want of the token
            return [check(label, False, "warn", f"the register at {settings.url} answered this command but could not "
                                                f"be read ({why}): it was asked without its token, which only the "
                                                f"running server has{follows}")] + fallback
        return [check(label, False, "error", f"the register at {settings.url} cannot be read ({why}): every run on "
                                             f"{name} that needs a lease is unavailable and follows its overflow"
                                             f"{follows}")] + fallback
    resident = ", ".join(f"{item.provider}/{item.model}" for item in seen.resident) or "nothing resident"
    wait = (f"a HELD answer (the card is held by someone else) goes at once to another build that is free, else "
            f"waits up to {provider.overflow_after_seconds:g}s (overflow_after_seconds)"
            if provider.overflow_after_seconds else
            "a HELD answer (the card is held by someone else) does not wait (no overflow_after_seconds)")
    return [check(label, True, "info", f"the register answers (generation {seen.generation}; {resident}); runs on "
                                       f"{name} take a request lease; {wait}; a FULL answer (the card cannot fit "
                                       f"the model now) goes straight to the next build or the overflow{follows}")
            ] + fallback


def _custody_fallback(config: Config, name: str) -> list[Check]:
    """A warning naming the models that run only on provider name, under custody, with no other build and no
    overflow: a FULL answer (or any refusal) leaves their runs nowhere to go."""
    from .routing import config_builds, overflow_key

    alone = []
    models = _dict(config.raw.get("models"))
    for key in sorted(name for name, entry in models.items() if isinstance(entry, dict)):
        try:
            model = config.model(str(key))
            builds = config_builds(config, model)
        except (KeyError, ValueError, TypeError):
            continue
        if any(build.provider.name == name for build in builds) and len(builds) == 1 \
                and not overflow_key(config, model):
            alone.append(str(key))
    if not alone:
        return []
    return [check(f"custody {name} fallback", False, "warn",
                  f"{', '.join(alone)} {'runs' if len(alone) == 1 else 'run'} only on {name}, with no other build and "
                  f"no overflow: when the register answers FULL (the card cannot fit the model now), or gives no "
                  f"lease at all, such a run has nowhere else to go and fails. Add a smaller build or another card "
                  f"(builds), or an overflow")]


def _drift_checks(config: Config, in_server: bool = False) -> list[Check]:
    """Each drift source: its token can be read (in a shell, a systemd credential cannot be: a warning,
    _shell_credential), and its last check's status (no request is sent here)."""
    from .driftsources import DriftSources, parse_sources

    try:
        sources = parse_sources(config.raw)
    except ValueError as exc:
        return [check("drift sources", False, "error", str(exc))]
    checks = []
    reader = DriftSources(config, system=None)
    for name, settings in sources.items():
        label = f"drift source {name}"
        token = _token_problem(settings.token_file, settings.token_env, settings.token_credential)
        shell = _shell_credential(settings.token_credential, in_server)
        if token and not shell:
            checks.append(check(label, False, "error", token))
            continue
        if shell:
            checks.append(check(f"{label} credential", False, "warn", shell))
        state = reader.state(name)
        status = state.get("status") or "never"
        detail = f"last check: {status}" + (f" ({state['detail']})" if state.get("detail") else "") + \
            (f", observed {state['observed_at']}" if state.get("observed_at") else "")
        checks.append(check(label, status in ("never", "ok", "busy"), "warn", detail))
    return checks


def _snapraid_checks(config: Config) -> list[Check]:
    """Disk protection with SnapRAID (DESIGN §41, BACKUPS_SNAPRAID.md §12.7): read-only, through the reader's info."""
    from .snapraid import doctor_checks

    return doctor_checks(config)


def _yourserver_checks(config: Config) -> list[Check]:
    """Your server (DESIGN §42, YOUR_SERVER.md §11.7): read-only, through the helper's info."""
    from .yourserver import doctor_checks

    return doctor_checks(config)


def _browser_checks(config: Config) -> list[Check]:
    """The browser adapter (DESIGN §43, BROWSER_AND_CODEX.md §4.3): browser pins, sandbox, timeouts and preset; one
    info line while it is off and unconfigured."""
    from .browser.checks import all_checks

    return all_checks(config, check)


def _codex_checks(config: Config, probe: bool = False) -> list[Check]:
    """The Codex worker (DESIGN §44, BROWSER_AND_CODEX.md §4.4): codex version, launcher, home, model (a blocker
    until the Responses route has passed its check under a custody request lease; --probe runs that check), sandbox,
    reads and egress; one info line while it is off and no agent runs on it."""
    from .codex.checks import all_checks

    return all_checks(config, check, probe=probe)


def _notify_run(argv: list[str]) -> str:
    """stdout of a read-only status command (tailscale, timedatectl, systemctl), "" when it cannot run."""
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=HTTP_TIMEOUT,
                              env={**os.environ, "LC_ALL": "C", "SYSTEMD_PAGER": "", "SYSTEMD_COLORS": "0"})
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""
    return done.stdout if done.returncode == 0 else ""


def _phone_channels(config: Config) -> dict[str, dict]:
    notify = _dict(config.raw.get("notify"))
    if notify.get("enabled") is False:
        return {}
    return {name: entry for name, entry in _dict(notify.get("channels")).items()
            if isinstance(entry, dict) and entry.get("enabled", True) is not False}


def _messages_checks(config: Config) -> list[Check]:
    from . import notify
    from .clock import PowerSchedule, detect_power, detect_zone, load_zone, ntp_synchronized

    channels = _phone_channels(config)
    wakes = _dict(config.raw.get("wakes"))
    data = config.data_dir
    checks: list[Check] = []
    state = notify._read_json(data / "notify" / "state.json")
    section = _dict(config.raw.get("notify"))
    if section.get("enabled") is False and _dict(section.get("channels")):
        checks.append(check("phone messages", True, "info", "turned off (notify.enabled): no channel sends, "
                            "listens or answers"))
    if channels:
        mode, cli = notify.sandbox_mode(config), notify.uses_cli_driver(config)
        acknowledged = (state.get("acknowledged") or {}).get("at")
        if mode != "bwrap" or cli:
            why = "harness.sandbox is none" if mode != "bwrap" else "an agent route uses a CLI driver"
            detail = (f"phone messages are on but {why}: your AI agents can read the message tokens, the "
                      "master token and everything you write to Seraphina, and send it anywhere. Set "
                      "harness.sandbox to bwrap and use the builtin driver")
            if acknowledged:
                detail += f" (you accepted this risk on {acknowledged}; withdraw it in Phone setup)"
            checks.append(check("messages sandbox", False, "error", detail))
        else:
            checks.append(check("messages sandbox", True, "info", "agents run under bwrap with your config "
                                "directory (config.json, the env file), the message data and your SSH and GPG "
                                "agent sockets hidden from them"))
            harness = _dict(config.raw.get("harness"))
            if harness.get("sandbox_network", True):
                checks.append(check("messages network", True, "info", "harness.sandbox_network is true: agents "
                                    "can reach the network (they still cannot read your messages)"))
    zone = wakes.get("timezone")
    machine = detect_zone()
    try:
        if zone:
            load_zone(zone)
    except ValueError as exc:
        checks.append(check("time zone", False, "error", f"wakes.timezone: {exc}"))
        zone = None
    if channels and not zone and not any(c["name"] == "time zone" for c in checks):
        checks.append(check("time zone", False, "error", "wakes.timezone is not set: do-not-disturb time needs "
                            "your local time (an IANA name such as Europe/London)"))
    elif zone and machine and machine != zone and machine not in ("UTC", "Etc/UTC"):
        checks.append(check("time zone", False, "warn", f"wakes.timezone is {zone} but this machine says {machine}"))
    elif zone:
        checks.append(check("time zone", True, "info", f"{zone}" + (f" (the machine says {machine})" if machine
                                                                     and machine != zone else "")))
    try:
        power = PowerSchedule.from_config(wakes.get("power"))
    except ValueError as exc:
        power = None
        checks.append(check("power schedule", False, "error", str(exc)))
    if power is not None and channels:
        found = detect_power(run=lambda argv, timeout=5.0: _notify_run(argv), zone=zone)
        if found.get("off") and found["off"] != power.off.strftime("%H:%M"):
            checks.append(check("power schedule", False, "warn", f"wakes.power.off is {power.off:%H:%M} but the "
                                f"timer {found.get('off_unit') or ''} powers off at {found['off']}"))
        else:
            checks.append(check("power schedule", True, "info", f"off {power.text()}"
                                + (" (matches the machine's timer)" if found.get("off") else "")))
    if power is not None:   # phone channels or not: a wake armed an hour off moves every morning's quiet time
        alarm = _wake_alarm(power, zone or machine or "UTC", quiet=not channels)
        checks += [alarm] if alarm else []
    window, source = notify.quiet_window(config)
    if channels:
        checks.append(check("do-not-disturb time", window is not None, "warn",
                            f"{window.text()} ({source})" if window else "none: every notice may buzz at night"))
    for name, hook in sorted(_dict(wakes.get("hooks")).items()):
        ok = isinstance(hook, dict) and isinstance(hook.get("token_sha256"), str) and len(hook["token_sha256"]) == 64
        checks.append(check(f"event source {name}", ok, "error", "token digest set" if ok else
                            "no token digest: rotate its token in Phone setup (Event sources)"))
    terms, status = notify.read_terms(notify.terms_path(config))
    if status["exists"] and status["mode_ok"] is False:
        checks.append(check("sensitive words", False, "error", f"{status['path']} can be read by other users: "
                            f"chmod 600 {status['path']}"))
    elif channels and not terms and not state.get("terms_none"):
        checks.append(check("sensitive words", False, "warn", "no list of words that must never appear on your "
                            "phone: add one in Phone setup, or tell it you have none"))
    elif status["exists"]:
        checks.append(check("sensitive words", True, "info", f"{len(terms)} words or phrases"))
    if channels:
        sync = _dict(wakes.get("time_sync"))
        synced = ntp_synchronized(run=lambda argv, timeout=5.0: _notify_run(argv))
        if synced is False and sync.get("require", True) is not False:
            checks.append(check("time sync", False, "warn", "the clock is not synchronised (timedatectl): quiet "
                                "hours and expiry wait up to the fallback time after boot"))
        elif synced:
            checks.append(check("time sync", True, "info", "the clock is synchronised"))
    paused = data / "wakes" / "PAUSED"
    if paused.exists():
        record = notify._read_json(paused)
        checks.append(check("seraphina", True, "info", f"stopped by {record.get('by') or 'someone'} at "
                            f"{record.get('at') or '?'}: restart her in the cockpit (or factory wake resume)"))
    if state.get("tamper"):
        checks.append(check("messages settings", False, "error", "settings changed outside the cockpit ("
                            + ", ".join(state["tamper"].get("what") or []) + "): review them in the cockpit"))
    if state.get("liveness"):
        checks.append(check("notifier", True, "info", f"last tick {state['liveness']}"))
    ok, detail = notify.Audit(data).readable()
    if not ok:
        checks.append(check("audit log", False, "warn", detail))
    return checks


def _wake_alarm(power: Any, zone: str, alarm_path: Path | None = None, now: float | None = None,
                quiet: bool = False) -> Check | None:
    """The RTC wake alarm (readable without root) against the next power-on wakes.power expects, in local time on
    the day it wakes: a wake armed with the wrong side of a clock change is an hour off (DESIGN §34). quiet: None
    instead of the line when no alarm is set (a machine that never powers off has nothing to compare)."""
    from .clock import WAKE_ALARM, LocalClock, check_wake_alarm, read_wake_alarm

    try:
        clock = LocalClock(zone, now=(lambda: now) if now is not None else time.time)
    except ValueError:
        clock = LocalClock("UTC", now=(lambda: now) if now is not None else time.time)
    state, words = check_wake_alarm(power, read_wake_alarm(alarm_path or WAKE_ALARM), clock.local())
    if quiet and state == "unset":
        return None
    return check("wake alarm", state in ("ok", "unset"), "warn", words)


def _operator_checks(config: Config) -> list[Check]:
    """The operator persona (DESIGN §36): the one-operator rule, marks on names that cannot be an operator, a
    role: line in a persona file (it means nothing), her wake persona's own mark (operator.wake_mark_problems), whether the receipts directory can be read (opened as the
    scan opens it; nothing in it is read or written), that it stands apart from the harness's own files and that
    the harness's user cannot write it (else a planted file would read as her words), and the last scan's
    max_receipts cut."""
    from . import operator as op
    from . import personas
    from .receipts import ReceiptsError, Receipts, _open_dir, harness_may_write, receipts_problem

    try:
        found = personas.discover_personas(config)
    except ValueError:
        found = {}
    checks = [check("operator", False, level, words) for level, words in op.doctor_problems(config, found)]
    # her wake persona carries the mark itself, and operator.several lists both names: verified, not assumed
    checks += [check("operator wake persona", ok, "warn", words) for ok, words in op.wake_mark_problems(config, found)]
    names = op.operators(config)
    reader = Receipts(lambda: config, config.data_dir)
    directory = reader.directory()
    if not names and not op.marked(config.raw):
        if directory is not None:
            checks.append(check("operator receipts", False, "warn", f"operator.receipts_dir is {directory}, but no "
                                "persona is marked the operator (agents.<name>.role)"))
        return checks or [check("operator", True, "info", "no persona is marked the operator (optional)")]
    if names:
        checks.append(check("operator", True, "info", f"{', '.join(names)} (marked by your config; the harness "
                            "records what she reports and never gates her own actions)"))
    if directory is None:
        checks.append(check("operator receipts", False, "warn", "operator.receipts_dir is not set: her change "
                            "receipts are not read and her unchecked work is not tracked"))
        return checks
    overlap = receipts_problem(config)
    if overlap:
        checks.append(check("operator receipts apart", False, "error", f"operator.receipts_dir: {overlap}"))
    try:
        fd = _open_dir(directory)
    except ReceiptsError as exc:
        checks.append(check("operator receipts", False, "error", str(exc)))
        return checks
    try:
        writable = harness_may_write(os.fstat(fd))
        listed = [name for name in os.listdir(fd) if not name.startswith(".")]
        count = sum(1 for name in listed if name.endswith(".json"))
        if listed:
            try:   # read without search (r--): listed, but no receipt in it can be opened
                os.stat(listed[0], dir_fd=fd, follow_symlinks=False)
            except PermissionError:
                count = None
                checks.append(check("operator receipts", False, "error", f"{directory} can be listed but not "
                                    "searched: give the harness's user read and search (r-x) access"))
    except OSError as exc:
        writable, count = False, None
        checks.append(check("operator receipts", False, "error", f"cannot list {directory}: {exc.strerror or exc}"))
    finally:
        os.close(fd)
    if count is not None:
        checks.append(check("operator receipts", True, "info", f"{directory} can be read ({count} receipt file(s) at "
                            "its top level; read only: the harness never writes there)"))
    if writable:
        checks.append(check("operator receipts read-only", False, "error",
                            f"the harness's own user ({'root' if os.geteuid() == 0 else 'uid ' + str(os.geteuid())}) "
                            f"may write {directory}: a file the harness or one of its agents put there would read as "
                            "her own words. Give the directory to the operator's user, readable but not writable by "
                            "the harness's (for example owner root, mode 755); until then her items from it are "
                            "marked untrusted"))
    summary = reader.summary()
    if summary.get("past_limit"):
        checks.append(check("operator receipts", False, "warn", f"the last scan read the newest "
                            f"{reader.settings()['max_receipts']} files and left "
                            f"{summary['past_limit']} older ones unread (operator.max_receipts): raise it, or move old "
                            "receipts to an archive outside receipts_dir"))
    return checks


def _wake_checks(config: Config, get: Callable[..., tuple[Any, Any]] | None = None,
                 bwrap: Callable[[], bool] | None = None) -> list[Check]:
    """Personas that wake themselves (Wake & Messages W2, W3): the notice rulebook, the S46 state as an error,
    bubblewrap for read-only wake tools (said plainly when it is missing), each enabled persona's file, soul
    integrity and model (attested local, no cloud id, its server's kind, loaded and awake), external memory paths,
    and the last scheduler tick. Read only: no state is written, no model is woken or called."""
    from . import notify, personas, rules, wakes
    from .harness.sandbox import bwrap_available

    get = get or wakes.http_get
    bwrap = bwrap or bwrap_available
    checks: list[Check] = []
    found = rules.load_rules(config)
    if found["error"]:
        checks.append(check("notice rules", False, "error", f"{found['error']}: every persona's words stay off the "
                            "phone until it is fixed"))
    else:
        checks.append(check("notice rules", True, "info", f"version {found['version']} from "
                            + (rules.RULES_FILE if found["source"] == "file" else "config.json (no rules file yet)")))
    section = _dict(config.raw.get("wakes"))
    enabled = section.get("enabled") is True
    entries = {name: wakes.persona_settings(config, name) for name in _dict(section.get("personas"))}
    active = {name: cfg for name, cfg in entries.items() if cfg.get("enabled")} if enabled else {}
    chat_wakes = enabled and _dict(section.get("chat_wakes")).get("enabled") is True
    state = notify._read_json(config.data_dir / "notify" / "state.json")
    if active or chat_wakes:
        mode, cli = notify.sandbox_mode(config), notify.uses_cli_driver(config)
        if mode != "bwrap" or cli:
            why = "harness.sandbox is none" if mode != "bwrap" else "an agent route uses a CLI driver"
            detail = (f"personas or chat check-ins are on but {why}: your AI agents can read everything you tell "
                      "Seraphina and her memory, and send it anywhere. Set harness.sandbox to bwrap and use the "
                      "builtin driver")
            if (state.get("acknowledged") or {}).get("at"):
                detail += " (you accepted this risk; withdraw it in Phone setup)"
            checks.append(check("personas sandbox", False, "error", detail))
    if not bwrap():
        level = "error" if any((cfg.get("wake") or {}).get("tools") == "read_only" for cfg in active.values()) else "info"
        checks.append(check("bubblewrap", level == "info", level, "bubblewrap (bwrap) is not installed or not usable "
                            "here: agents run without the sandbox, and a persona's read-only wake tools are refused "
                            "unless you type the acknowledgement. Install it: sudo apt install bubblewrap"))
    try:
        found_personas = {name: p for name, p in personas.discover_personas(config).items() if p.kind == "wake"}
    except ValueError as exc:
        found_personas = {}
        checks.append(check("personas", False, "error", str(exc)))
    wake_state = notify._read_json(config.data_dir / "wakes" / "state.json")
    attested = _dict(wake_state.get("attested"))
    roots = [Path(os.path.expanduser(str(r))) for r in section.get("external_memory_roots") or []]
    masks = config_masks(config)
    for name, cfg in sorted(active.items()):
        label = f"persona {name}"
        persona = found_personas.get(name)
        if persona is None:
            checks.append(check(label, False, "error", f"wakes.personas.{name} is on but there is no personas/{name}.md"))
            continue
        if persona.error:
            checks.append(check(label, False, "error", persona.error))
            continue
        approved = _dict(wake_state.get("approved")).get(name)
        if persona.file_sha256 != personas.SHIPPED_SOUL_SHA256 and persona.file_sha256 != approved:
            checks.append(check(f"{label} instructions", False, "warn", "changed outside her page: she is stopped "
                                "until you review the change (Messages, Personas)"))
        if persona.lists_tools:
            checks.append(check(f"{label} tools", False, "warn", "the file has a tools: line; a persona has no tools "
                                "from its file (wake.tools in the settings decides)"))
        external = _dict(cfg.get("memory")).get("external")
        if isinstance(external, dict) and external:
            problems = personas.check_external_paths(external, roots, masks)
            checks.append(check(f"{label} memory", not problems, "error",
                                "; ".join(problems) or "external memory read-only from allowlisted paths"))
            checks += _external_memory_checks(config, name, external, problems)
        checks += _persona_model_check(config, name, cfg, attested, get)
    tick = wake_state.get("tick_at")
    if enabled:
        checks.append(check("wake scheduler", bool(tick), "info" if tick else "warn",
                            f"last tick {tick}" if tick else "has not ticked yet (is the server running?)"))
    breaker = _dict(wake_state.get("breaker"))
    if breaker.get("open"):
        checks.append(check("wake breaker", False, "warn", f"Seraphina stopped herself: {breaker.get('why')}; restart "
                            "her in the cockpit"))
    return checks


# personas.sqlite_rows: the query ran (or was refused) as the persona's own; any other error is the file itself
QUERY_ERRORS = ("the query failed", "the query is not one SELECT statement")
SQLITE_SNAPSHOT_ADVICE = ("If the harness's user may not read it, do not add an ACL to her live store or its folder, "
                          "or loosen their mode: that can break the store. Point sqlite.path at a read-only snapshot "
                          "her own tools publish, or leave sqlite out")


def _external_memory_checks(config: Config, name: str, external: dict, problems: list[str]) -> list[Check]:
    """A persona's own database, queried once read-only (whether the query works and how many rows it returns;
    nothing of the text is shown or kept), and what the read ledger's caps did (memory.external.ledger)."""
    from . import personas

    out: list[Check] = []
    label = f"persona {name}"
    sqlite = external.get("sqlite")
    if isinstance(sqlite, dict) and sqlite.get("path") and not problems:
        path = Path(os.path.expanduser(str(sqlite["path"])))
        if path.is_symlink():   # the wake never opens one either (read_external_memory)
            rows, error, detail = None, "it is a symlink", None
        else:
            rows, error, detail = personas.sqlite_rows(path, str(sqlite.get("query") or ""))
        if error is None:
            out.append(check(f"{label} database", True, "info",
                             f"her query reads {len(rows or [])} rows (read-only; at most {personas.EXTERNAL_ROWS})"))
        else:
            why = f"{error} ({detail})" if detail else error
            # the query ran and failed: her schema decides; the file could not be opened: never open up her live
            # store for the harness (an ACL or a looser mode can break it, DESIGN §35 b), name a snapshot instead
            advice = ("The query is hers and must match her own schema" if error in QUERY_ERRORS else
                      SQLITE_SNAPSHOT_ADVICE)
            out.append(check(f"{label} database", False, "warn",
                             f"cannot be read: {why}; her wakes say so and read nothing from it. {advice}"))
    if external.get("ledger") is True:
        health = personas.PersonaStore(config.data_dir).ledger_health(name)
        words = []
        if health.get("evicted"):
            words.append(f"{health['evicted']} older copies removed, the newest version of each file kept (the last "
                         f"at {str(health.get('evicted_at'))[:16]})")
        if health.get("trimmed"):
            words.append(f"{health['trimmed']} oldest manifests removed (the last at "
                         f"{str(health.get('trimmed_at'))[:16]})")
        if health.get("not_kept"):
            words.append(f"{health['not_kept']} versions not kept (the last at {str(health.get('not_kept_at'))[:16]})")
        caps = personas.external_caps(external)   # memory.external's caps as set, else the defaults
        cap = f"{caps['ledger_mib']} MiB, {caps['ledger_copies']} copies"
        detail = (f"at its cap ({cap}): " + "; ".join(words) + ". The oldest rollbacks are gone: raise "
                  "memory.external.ledger_mib or ledger_copies, or see factory personas ledger") if words else (
            f"on: each read of her own memory is recorded, with private copies (the ledger keeps {cap} at most; "
            f"one read opens {caps['max_files']} files and {caps['read_bytes'] // personas.MIB} MiB at most)")
        out.append(check(f"{label} read ledger", not words, "warn", detail))
    return out


def config_masks(config: Config) -> list[Path]:
    from .config import sandbox_masks

    masks = sandbox_masks(config)
    return [Path(config.data_dir), *[Path(p) for p in masks["dirs"]], *[Path(p) for p in masks["files"]]]


def _persona_model_check(config: Config, name: str, cfg: dict, attested: dict,
                         get: Callable[..., tuple[Any, Any]]) -> list[Check]:
    from . import wakes

    label = f"persona {name} model"
    key = cfg.get("model")
    if not key:
        return [check(label, False, "warn", "no model yet: choose one in persona setup")]
    try:
        model = config.model(str(key))
    except (KeyError, ValueError) as exc:
        return [check(label, False, "error", f"models.{key}: {exc}")]
    provider = model.provider
    if not _dict(attested.get(provider.name)).get("local"):
        return [check(label, False, "error", f"{provider.name} is not confirmed to run on this machine: tick it in "
                      "persona setup (S7)")]
    if str(model.model).endswith(("-cloud", ":cloud")):
        return [check(label, False, "error", f"{model.model} is a hosted model; personas use local models only")]
    if provider.kind == "ollama" or "11434" in str(provider.base_url):
        if os.environ.get("OLLAMA_NO_CLOUD") != "1":
            return [check(label, False, "error", "an Ollama server needs OLLAMA_NO_CLOUD=1 (or disable_ollama_cloud), "
                          "or a name could reach a cloud model")]
    server = wakes.server_settings(config, provider)
    root = server["root_url"]
    what = wakes.identify_server(get, root)
    if what is not None:
        words = "Open WebUI" if what == "openwebui" else "a proxy (LiteLLM)"
        return [check(label, False, "error", f"{root} is {words}, not a model server: point the persona at the "
                      "model server itself (check-ins refuse it, S7)")]
    if server["kind"] == "llamacpp":
        status, props = get(root + "/props", wakes.PROBE_TIMEOUT)
        if isinstance(props, dict) and props.get("is_sleeping"):
            return [check(label, True, "info", f"{model.model} on {provider.name} (llama.cpp) is asleep: check-ins "
                          "wait for it and never wake it")]
    if server["kind"] == "tabby":
        status, loaded = get(root + "/v1/model", wakes.PROBE_TIMEOUT)
        loaded_id = loaded.get("id") if isinstance(loaded, dict) else None
        if loaded_id != model.model:
            return [check(label, False, "warn", f"TabbyAPI has {loaded_id or 'no model'} loaded, not {model.model}: "
                          "check-ins wait")]
    return [check(label, True, "info", f"{model.model} on {provider.name} ({server['kind']}), attested local")]


def _channel_jobs(config: Config) -> list[tuple[str, Callable[[], list[Check]]]]:
    return [(f"channel {name}", lambda n=name, e=entry: _channel_checks(config, n, e))
            for name, entry in sorted(_phone_channels(config).items())]


def _channel_checks(config: Config, name: str, entry: dict) -> list[Check]:
    from . import channels as ch
    from .notify import _read_server_yml

    label = f"channel {name}"
    channel = ch.make_channel(name, lambda: dict(entry), os.environ, config.data_dir / "notify" / "channels",
                              **CHANNEL_KWARGS)
    if entry.get("type") == "telegram":
        return _telegram_checks(label, channel)
    checks: list[Check] = []
    publish = str(entry.get("publish_url") or "")
    if publish.startswith("unix:") and not os.path.exists(publish[5:]):
        checks.append(check(f"{label} socket", False, "error", f"{publish[5:]} does not exist: is ntfy running? "
                            "(sudo systemctl status ntfy)"))
    healthy, detail = channel.check_health()   # type: ignore[attr-defined]
    checks.append(check(f"{label} health", healthy, "error", detail))
    if healthy:
        if not channel.ready():
            checks.append(check(f"{label} token", False, "error", f"{entry.get('token_env')} is not set in the env "
                                "file: Prepare the settings in Phone setup"))
        else:
            reads = {topic: channel.poll(topic) for topic in channel.topics()}   # type: ignore[attr-defined]
            bad = [f"{topic} ({status})" for topic, status in reads.items() if status != 200]
            checks.append(check(f"{label} access", not bad, "error", "the harness can read its three topics" if not
                                bad else "cannot read " + ", ".join(bad) + ": the access rules in server.yml"))
    yml = _read_server_yml()
    forwarders = ch.ntfy_forwarders(yml)
    relay_ok = forwarders == [ch.NTFY_RELAY] and entry.get("ios_relay") is True
    if forwarders and not relay_ok:
        checks.append(check(f"{label} forwarding", False, "error", "/etc/ntfy/server.yml sets "
                            + ", ".join(forwarders) + ": message text could leave the machine, so the channel "
                            "counts as third-party"))
    status = ch.tailnet_status(run=_notify_run)
    serve = _notify_run(["tailscale", "serve", "status", "--json"])
    funnel = _notify_run(["tailscale", "funnel", "status"])
    funnel_on = '"AllowFunnel"' in serve or bool(status and status.get("funnel")) or "Funnel on" in funnel
    checks.append(check(f"{label} funnel", not funnel_on, "error", "no Funnel: the message server is not public"
                        if not funnel_on else "Tailscale Funnel is on for this node: turn it off (tailscale funnel "
                        "reset), the message server must be reachable from your tailnet only"))
    privacy = ch.channel_privacy(entry, status, yml)
    checks.append(check(f"{label} privacy", privacy == ch.PRIVACY_TAILNET, "warn",
                        "private: on this tailnet" if privacy == ch.PRIVACY_TAILNET else
                        "third-party: its public URL is not on this tailnet (or Tailscale cannot be asked), so it "
                        "gets heads-ups only"))
    return checks


def _telegram_checks(label: str, channel: Any) -> list[Check]:
    from .channels import ChannelError

    try:
        me = channel.call("getMe", http_timeout=HTTP_TIMEOUT)
        hook = channel.call("getWebhookInfo", http_timeout=HTTP_TIMEOUT)
    except ChannelError as exc:
        return [check(f"{label} bot", False, "error", str(exc))]
    checks = [check(f"{label} bot", True, "info", f"@{(me or {}).get('username')}")]
    webhook = isinstance(hook, dict) and bool(hook.get("url"))
    checks.append(check(f"{label} webhook", not webhook, "error", "no webhook: the harness reads updates" if not
                        webhook else "a webhook is set: another program receives this bot's messages"))
    paired = channel.paired()
    checks.append(check(f"{label} paired", bool(paired), "warn", f"{len(paired)} paired account(s)" if paired else
                        "no paired account: pair your phone in Phone setup"))
    return checks


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


def setup_status(config: Config, factory: Any = None, looked: dict | None = None) -> dict:
    """{configured, steps: {providers, models, routing, repos, tools, output}: {ok, detail}, host (setup_host)},
    offline: it never looks for model servers.

    configured is true when providers, models, routing and repos are ok (tools and output are
    optional). Never raises: a step whose check breaks is reported not ok with the reason.
    looked is the last look-around the owner asked for (LookAround.last), or None: host then
    carries its model_servers and model_servers_at (when it was taken), or for a look that broke
    model_servers_error instead of model_servers (it found nothing, so it says nothing answered); without one, none.
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
    host = setup_host()
    if looked is not None and looked.get("error"):
        host.update(model_servers_error=str(looked["error"]), model_servers_at=looked.get("at"))
    elif looked is not None:
        host.update(model_servers=[dict(item) for item in looked.get("model_servers") or []],
                    model_servers_at=looked.get("at"))
    return {"configured": all(steps[name]["ok"] for name in REQUIRED_STEPS), "steps": steps, "host": host}


def setup_host(proc_net: Path | None = None) -> dict:
    """What the setup wizard may suggest from this machine, read from files only (it sends nothing): {key_dirs:
    {folder: exists} for the key files the default providers name (a preset's placeholder such as /etc/ai-stack
    need not exist here), listening: the TCP ports from 1024 up that something listens on at 127.0.0.1 or every
    address (at most 64), so a local server on another port than the preset's is noticed}. Which of them is a model
    server only the owner's look-around tells (look_for_model_servers)."""
    from . import listeners
    from .config import DEFAULTS

    folders = sorted({os.path.dirname(os.path.expanduser(str(entry["api_key_file"])))
                      for entry in DEFAULTS["providers"].values()
                      if isinstance(entry, dict) and entry.get("api_key_file")})
    found = listeners.listening(proc_net) if proc_net is not None else listeners.listening()
    ports = sorted({port for address, port in found if port >= 1024
                    and (listeners.is_wildcard(address) or address in ("127.0.0.1", "::1"))})
    return {"key_dirs": {folder: os.path.isdir(folder) for folder in folders}, "listening": ports[:64]}


# ---------------------------------------------------------------- the look-around (only when the owner asks)

LOOK_AROUND_GAP = 30.0          # seconds: the GUI's look-around runs at most once in this long
LOOK_AROUND_PER_HOUR = 10       # and at most this many times an hour, so a stuck script cannot make it a scan loop
LOOK_AROUND_NOTE = ("Sent one GET /v1/models, without a key, to each program listening on this machine "
                    "(ports from 1024 up, at most {limit}, {timeout:g} s each; the harness's own port skipped). "
                    "Nothing was changed.")
LOOK_AROUND_ADVICE = ('to look for a model server on another port, press "Look for model servers" on the setup '
                      "page's Server details step (Setup, then Server details), or run factory doctor --look-around")


def look_for_model_servers(config: Config | None = None, find: Callable[[set[int]], list[dict]] | None = None,
                           skip_ports: set[int] | frozenset[int] = frozenset()) -> dict:
    """The look-around, once, because the owner asked: one GET /v1/models without a key to each program listening
    here (listeners.model_servers; find: for tests), the harness's own port skipped (the config's server.port and
    skip_ports: the port the server really listens on, which `factory serve --port` may change) → {at (UTC),
    model_servers: [{url, port, status, ids}] for those that answer like a model server (a model list, or a JSON
    refusal from one that wants a key; at most 6, five ids each), note (what was sent)}; when the look itself broke,
    {at, error} alone: it has no answer to give, so it neither says that nothing answered nor what it sent."""
    from . import listeners

    at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        server = _dict(config.raw.get("server")) if config is not None else {}
        raw = (find or listeners.model_servers)({int(server.get("port") or 8765), *skip_ports})
    except Exception as exc:  # noqa: BLE001 - reported with the result, never raised
        return {"at": at, "error": f"{type(exc).__name__}: {exc}"}
    result: dict[str, Any] = {"at": at, "model_servers": [],
                              "note": LOOK_AROUND_NOTE.format(limit=listeners.PROBE_LIMIT,
                                                              timeout=listeners.PROBE_TIMEOUT)}
    for item in raw[:6]:
        address = str(item.get("address") or "127.0.0.1")
        host = f"[{address}]" if ":" in address else address
        result["model_servers"].append({"url": f"http://{host}:{item.get('port')}/v1", "port": item.get("port"),
                                        "status": item.get("status"),
                                        "ids": [str(x) for x in (item.get("ids") or [])[:5]]})
    return result


def _looked_check(looked: dict) -> Check:
    """The "model servers here" line of `factory doctor --look-around`: what the look found, and what it sent."""
    if looked.get("error"):
        return check("model servers here", False, "warn", f"the look did not work: {looked['error']}")
    found = looked.get("model_servers") or []
    what = (", ".join(_server_text(item) for item in found) if found
            else "none of the programs listening here answered like a model server")
    return check("model servers here", True, "info", f"{what}. {looked.get('note') or ''}".strip())


class LookAroundRefused(Exception):
    """The look-around asked for again too soon, or while one runs: POST /api/setup/look-around answers 429 with
    Retry-After (retry_after, in seconds)."""

    def __init__(self, message: str, retry_after: float) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class LookAround:
    """The GUI's look-around (Factory.look_around, POST /api/setup/look-around): one at a time, at most once in
    LOOK_AROUND_GAP seconds and LOOK_AROUND_PER_HOUR times an hour (counted from each start), so a double click or
    a stuck script cannot turn it into a scan loop. The last result is kept: GET /api/setup shows it, with when it
    was taken, and never looks itself."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started: list[float] = []
        self._running = False
        self._last: dict | None = None

    def ask(self, config: Config, now: float, find: Callable[[set[int]], list[dict]] | None = None,
            skip_ports: set[int] | frozenset[int] = frozenset()) -> dict:
        """Look once (look_for_model_servers) and keep the result; LookAroundRefused instead when asked too soon.
        now: a monotonic clock's reading (the Factory's)."""
        with self._lock:
            if self._running:
                raise LookAroundRefused("A look for model servers is running now. Its answer comes in a few "
                                        "seconds.", 5.0)
            self._started = [at for at in self._started if now - at < 3600.0]
            since = now - self._started[-1] if self._started else None
            if since is not None and since < LOOK_AROUND_GAP:
                wait = LOOK_AROUND_GAP - since
                ago = "just now" if since < 2 else f"{int(since)} seconds ago"
                raise LookAroundRefused(f"You looked {ago}. You can look again in {int(wait + 0.999)} seconds.",
                                        wait)
            if len(self._started) >= LOOK_AROUND_PER_HOUR:
                wait = 3600.0 - (now - self._started[0])
                raise LookAroundRefused(f"You looked {LOOK_AROUND_PER_HOUR} times in the last hour. You can look "
                                        f"again in {int(wait / 60 + 0.999)} minutes.", wait)
            self._started.append(now)
            self._running = True
        try:
            result = look_for_model_servers(config, find, skip_ports)
        finally:
            with self._lock:
                self._running = False
        with self._lock:
            self._last = result
        return json.loads(json.dumps(result))

    def last(self) -> dict | None:
        """The last look's result (a copy), or None before the first."""
        with self._lock:
            return json.loads(json.dumps(self._last)) if self._last is not None else None


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
