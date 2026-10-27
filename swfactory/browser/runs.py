"""What the harness core does around a browsing run (BROWSER_AND_CODEX.md §2.6, §2.7, §2.10, §2.13; DESIGN §43).

- ``settings``: the ``browser`` section (off by default), with its profiles, the harness's own runtime and its pins,
  the limits and the caps on what the sessions keep (the owner's O6: 5 GB and 30 days).
- ``check_agent``: §2.10 rule 2, refused at chat start and again on every turn: an agent that names the browser
  server has nothing else (no builtin tool, no other MCP server, no Wake, no memory), and neither has the agent of a
  chat that browsed, whose session holds page text, when its file no longer names the browser.
- ``begin``: the grant for one locking run (profile and origins from config, never from a model), after the caps are
  applied; ``register`` keeps the run's ``McpClient`` so a person can revoke (a client registered after its grant was
  revoked is closed at once).
- ``revoke``: sets ``revoked`` in the grant and closes the locking run's client, which ends the shim's namespace.
- ``finish``: at run end the grant is marked ended, an action still without an outcome becomes ``unknown`` (rule 4;
  once the shim is gone: after a revoke, whose close runs on a thread of its own, it waits for the grant's lock),
  the action log is read into the run's receipt
  (``receipt.json`` in the session folder: ids, classes, outcomes, responses and hashes; names only as keyed
  hashes), a ``login_required`` opens a proposed system item for the owner, and a summary goes to chat.json.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..harness.mcp import _CLOSE_GRACE
from . import CHROME_PIN, PLAYWRIGHT_MCP_INTEGRITY, PLAYWRIGHT_MCP_PIN, SHIM_VERSION, UNTRUSTED_LABEL
from . import rules as R
from .fixture import FIXTURE_ORIGIN
from .session_locks import (ACTION_LOG, RECEIPT_FILE, SESSION_FILE, ActionLog, browser_root, grant_path, read_grant,
                            read_record, prune_sessions, revoke_grant, session_of_grant, settle_folder, update_grant,
                            utc_now, wait_grant_free, write_grant, _write_private)

PROJECT_DIR = Path(__file__).resolve().parents[2]
FIXTURE_SITE = PROJECT_DIR / "tests" / "fixtures" / "browser" / "site"
SF_BROWSER = PROJECT_DIR / "bin" / "sf-browser"
BROWSER_SERVER = "browser"
RUNTIME_LAYOUT = {"node": "node/bin/node", "playwright_mcp": "playwright-mcp/node_modules/@playwright/mcp",
                  "chrome": "chrome/chrome-headless-shell-linux64/chrome-headless-shell"}
SETTINGS_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "runtime": {"dir": None, "node": None, "playwright_mcp": None, "chrome": None},
    "pins": {"node": {"version": None, "sha256": None},
             "playwright_mcp": {"version": PLAYWRIGHT_MCP_PIN, "integrity": PLAYWRIGHT_MCP_INTEGRITY},
             "chrome": {"version": CHROME_PIN, "sha256": None}},
    "profiles": {
        "fixture": {"site": None, "origins": {"read": [FIXTURE_ORIGIN], "write": [FIXTURE_ORIGIN]}, "save": "never"},
        "fixture-login": {"site": None, "origins": {"read": [FIXTURE_ORIGIN], "write": [FIXTURE_ORIGIN]},
                          "save": "never", "login": {"path": "/login"}, "seed_cookie_seconds": 2},
    },
    "preset": "kate-browse",
    "preset_checked_by_hand": {"export": None, "functions": None, "pipelines": None},
    "limits": {"max_minutes": 60, "call_seconds": 20, "commit_seconds": 20, "page_chars": 8000,
               "download_bytes": 50_000_000},
    "caps": {"max_gb": 5, "max_days": 30},
}
SETTINGS_KEYS = frozenset(SETTINGS_DEFAULTS)
LIMIT_BOUNDS = {"max_minutes": (1, 240), "call_seconds": (2, 110), "commit_seconds": (1, 110),
                "page_chars": (1000, 30000), "download_bytes": (1 << 20, 1 << 30)}
CAP_BOUNDS = {"max_gb": (1, 1000), "max_days": (1, 3650)}
PROFILE_KEYS = frozenset({"site", "origins", "save", "login", "seed_cookie_seconds", "shop_delay", "fixture_port",
                          "chrome_sandbox"})
_CLIENTS: dict[str, Any] = {}       # grant id -> the locking run's McpClient (for revoke)
_REVOKED: set[str] = set()          # grants revoked in this process while their run had no client registered yet
_CLIENTS_LOCK = threading.Lock()
# How long run end waits for a shim that is still ending before it settles (a revoke's close runs on a thread of
# its own, and the run's own close then returns at once): McpClient.close's steps (stdin EOF, SIGTERM, SIGKILL, the
# process group) and a second more.
SHIM_EXIT_SECONDS = 4 * _CLOSE_GRACE + 1


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (over or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict) and key != "profiles":
            out[key] = _merge(out[key], value)
        elif key == "profiles" and isinstance(value, dict):
            profiles = copy.deepcopy(out.get("profiles") or {})
            for name, entry in value.items():
                if entry is None:
                    profiles.pop(name, None)
                elif isinstance(entry, dict):
                    profiles[name] = _merge(profiles.get(name) or {}, entry)
            out["profiles"] = profiles
        else:
            out[key] = copy.deepcopy(value)
    return out


def settings(config: Any) -> dict:
    """The browser section merged over its defaults (config.raw already holds DEFAULTS["browser"])."""
    raw = getattr(config, "raw", config) or {}
    section = raw.get("browser") if isinstance(raw.get("browser"), dict) else {}
    return _merge(SETTINGS_DEFAULTS, section)


def check_settings(raw: dict) -> None:
    """ValueError naming the first invalid browser setting."""
    section = raw.get("browser")
    if section is None:
        return
    if not isinstance(section, dict):
        raise ValueError("browser must be an object")
    unknown = sorted(set(section) - SETTINGS_KEYS)
    if unknown:
        raise ValueError(f"browser.{unknown[0]} is not a setting (use {', '.join(sorted(SETTINGS_KEYS))})")
    merged = settings(raw)
    if not isinstance(merged["enabled"], bool):
        raise ValueError("browser.enabled must be true or false")
    for key in ("dir", "node", "playwright_mcp", "chrome"):
        value = merged["runtime"].get(key)
        if value is not None and (not isinstance(value, str) or "\0" in value or not value.strip()):
            raise ValueError(f"browser.runtime.{key} must be a path or null")
    if merged["runtime"].get("dir") is not None and not os.path.isabs(os.path.expanduser(merged["runtime"]["dir"])):
        raise ValueError("browser.runtime.dir must be an absolute path (the harness's own runtime directory)")
    for name, (low, high) in LIMIT_BOUNDS.items():
        value = merged["limits"].get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
            raise ValueError(f"browser.limits.{name} must be a number from {low} to {high}")
    for name, (low, high) in CAP_BOUNDS.items():
        value = merged["caps"].get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
            raise ValueError(f"browser.caps.{name} must be a number from {low} to {high}")
    if not isinstance(merged["preset"], str) or not merged["preset"].strip():
        raise ValueError("browser.preset must name the browsing preset")
    for name, entry in merged["profiles"].items():
        profile_of(merged, name)


def profile_of(merged: dict, name: str) -> dict:
    """One profile, checked: BC1 accepts only origins on the namespace's loopback (§2.7), save never."""
    entry = merged["profiles"].get(name)
    if not isinstance(entry, dict):
        raise ValueError(f"browser.profiles has no profile {name!r}")
    unknown = sorted(set(entry) - PROFILE_KEYS)
    if unknown:
        raise ValueError(f"browser.profiles.{name}.{unknown[0]} is not a setting")
    origins = entry.get("origins") if isinstance(entry.get("origins"), dict) else {}
    read = [R.normalize_origin(origin) for origin in origins.get("read") or []]
    write = [R.normalize_origin(origin) for origin in origins.get("write") or []]
    if not read:
        raise ValueError(f"browser.profiles.{name} has no read origins")
    if not set(write) <= set(read):
        raise ValueError(f"browser.profiles.{name}: every write origin must also be a read origin")
    for origin in read:
        if not R.loopback_origin(origin):
            raise ValueError(f"browser.profiles.{name}: {origin} is not inside the browser's own namespace; BC1 "
                             f"browses the fixture and a job's preview only (real sites wait for BC2)")
    if entry.get("save", "never") != "never":
        raise ValueError(f"browser.profiles.{name}.save must be never in BC1")
    login = entry.get("login")
    if login is not None and not (isinstance(login, dict) and isinstance(login.get("path"), str)
                                  and login["path"].startswith("/")):
        raise ValueError(f"browser.profiles.{name}.login must be {{\"path\": \"/login\"}} or null")
    site = entry.get("site")
    return {"name": name, "site": str(Path(os.path.expanduser(site)) if site else FIXTURE_SITE),
            "origins": {"read": read, "write": write}, "fixture_origins": list(read), "login": login,
            "settings": {key: entry[key] for key in ("login", "seed_cookie_seconds", "shop_delay", "fixture_port",
                                                     "chrome_sandbox") if entry.get(key) is not None}}


def runtime_paths(config: Any) -> dict:
    """The harness's own Node, Playwright MCP package and Chrome (K1): absolute paths from browser.runtime, a
    relative one inside runtime.dir; never a program found on PATH. ValueError when it is not set up."""
    runtime = settings(config)["runtime"]
    root = runtime.get("dir")
    out: dict[str, str] = {}
    for key, default in RUNTIME_LAYOUT.items():
        value = runtime.get(key) or (default if root else None)
        if not value:
            raise ValueError("the harness's own browser runtime is not set up: set browser.runtime.dir (Node, "
                             "Playwright MCP and the Chrome build in one directory; DESIGN §43)")
        path = Path(os.path.expanduser(value))
        if not path.is_absolute():
            if not root:
                raise ValueError(f"browser.runtime.{key} is relative and browser.runtime.dir is not set")
            path = Path(os.path.expanduser(root)) / path
        out[key] = str(path)
    return out


# ---------------------------------------------------------------- which agents browse, and the session rule


def is_browser_server(cfg: Any) -> bool:
    return getattr(cfg, "sandbox", "run") == "self"


def browser_servers(agent: Any, servers: dict | None) -> list[str]:
    """The names in agent.mcp that are the browser (a server with sandbox "self")."""
    servers = servers or {}
    return [name for name in getattr(agent, "mcp", []) or [] if name in servers and is_browser_server(servers[name])]


def browses(agent: Any, servers: dict | None) -> bool:
    return bool(browser_servers(agent, servers)) or BROWSER_SERVER in (getattr(agent, "mcp", []) or [])


def check_agent(agent: Any, servers: dict | None, *, browsed: bool = False) -> None:
    """§2.10 rule 2: an agent that browses has nothing else. ValueError naming what it combines. browsed: its chat
    browsed before (chat.json's browser list), so the rule holds although the agent's file no longer names the
    browser: the chat's session still holds page text."""
    browsing = browses(agent, servers)
    if not browsing and not browsed:
        return
    problems = []
    tools = [str(tool) for tool in getattr(agent, "tools", []) or []]
    if tools:
        problems.append(f"the tools {', '.join(tools)} (a browsing agent has tools: none)")
    others = [name for name in agent.mcp if name not in browser_servers(agent, servers) and name != BROWSER_SERVER]
    if others:
        problems.append(f"the MCP server{'s' if len(others) > 1 else ''} {', '.join(others)}")
    if len([name for name in agent.mcp if name in browser_servers(agent, servers) or name == BROWSER_SERVER]) > 1:
        problems.append("more than one browser server")
    meta = getattr(agent, "meta", {}) or {}
    if str(meta.get("wake") or "").strip().lower() not in ("", "false", "no", "off", "0"):
        problems.append("wake: true (Wake)")
    if str(meta.get("memory") or "").strip():
        problems.append("a memory line")
    if problems and not browsing:
        raise ValueError(f"agent {agent.name!r} no longer names the browser, but this chat browsed: its session holds "
                         f"page text, so its agent has nothing else, and it has {'; '.join(problems)} "
                         f"(BROWSER_AND_CODEX.md §2.10 rule 2; start a new chat for it)")
    if problems:
        raise ValueError(f"agent {agent.name!r} combines the browser with {'; '.join(problems)}: a browsing session "
                         f"has nothing else (BROWSER_AND_CODEX.md §2.10 rule 2)")


# ---------------------------------------------------------------- grants


@dataclass
class Grant:
    id: str
    path: Path
    profile: str
    server: str
    locked_by: dict


def begin(config: Any, agent: Any, *, kind: str, ref: str, run: str, verify: dict | None = None) -> Grant:
    """Write the grant for one locking run: the profile from the agent file's browser: line, its origins from
    config. ValueError when browsing is off, the profile or the runtime is not set up, or the file is not allowed
    to browse. verify (a "Verify in the browser" chat's record in chat.json, written by the core, DESIGN §44): the
    grant names the job's preview instead of the fixture: its origin (the relay's loopback port inside the
    namespace) and its command, run from the job's worktree in a sibling namespace (§2.9); the command, the port and
    the worktree come from the repository's config and the job store, never from the record's own text."""
    merged = settings(config)
    if not merged["enabled"]:
        raise ValueError("the browser is off (browser.enabled is false): turn it on in Settings, with the harness's "
                         "own runtime set up (DESIGN §43)")
    servers = getattr(config, "mcp_servers", lambda: {})()
    names = browser_servers(agent, servers)
    if not names:
        raise ValueError(f"agent {agent.name!r} names the browser, but mcp_servers.{BROWSER_SERVER} is not the "
                         f"installed sf-browser with sandbox \"self\" (apply the browser preset)")
    check_agent(agent, servers)
    profile_name = getattr(agent, "browser", None) or (getattr(agent, "meta", {}) or {}).get("browser")
    if not profile_name:
        raise ValueError(f"agent {agent.name!r} names the browser but has no browser: line naming a profile")
    profile = profile_of(merged, str(profile_name).strip())
    paths = runtime_paths(config)
    if not paths.get("playwright_argv"):
        missing = [f"{key} ({paths[key]})" for key in RUNTIME_LAYOUT if not os.path.exists(paths[key])]
        if missing:   # never a Node or Chrome from somewhere else instead (K1)
            raise ValueError(f"the harness's own browser runtime is missing: {', '.join(missing)} (browser.runtime; "
                             f"the doctor's browser pins line)")
    paths["fixture"] = profile["site"]
    data_dir = Path(config.data_dir)
    preview = None
    if verify:
        preview, origin = preview_of(config, verify)
        profile = {**profile, "origins": {"read": [origin], "write": [origin]}, "fixture_origins": []}
        paths.pop("fixture", None)
    caps = merged["caps"]
    with contextlib.suppress(OSError):
        prune_sessions(data_dir, int(float(caps["max_gb"]) * (1 << 30)), float(caps["max_days"]))
    worker = {"playwright_mcp": _package_version(paths["playwright_mcp"]) or merged["pins"]["playwright_mcp"]["version"],
              "browser": f"chrome-headless-shell {merged['pins']['chrome'].get('version') or '?'} (the pin; the "
                         f"doctor's browser pins line reads the build)"}
    grant_id, path = write_grant(data_dir, locked_by={"kind": kind, "ref": ref, "run": run, "agent": agent.name},
                                 profile=profile["name"], origins=profile["origins"],
                                 fixture_origins=profile["fixture_origins"], paths=paths,
                                 limits=merged["limits"], profile_settings=profile["settings"], worker=worker,
                                 preview=preview)
    return Grant(grant_id, path, profile["name"], names[0], {"kind": kind, "ref": ref, "run": run})


def preview_of(config: Any, verify: dict) -> tuple[dict, str]:
    """(the grant's preview, its origin) for a verify record {repo, job}: the repository's preview command and port,
    run in the job's worktree (<data_dir>/worktrees/<repo>/<job>). ValueError when the repository has none or the
    worktree is gone."""
    from ..jobs import JobStore

    repo_name, job_id = str(verify.get("repo") or ""), str(verify.get("job") or "")
    repo = config.repo(repo_name)
    if not repo.preview:
        raise ValueError(f"the repository {repo_name} has no preview setting (DESIGN §44)")
    worktree = JobStore(config.data_dir).worktree_path(repo_name, job_id)
    if not worktree.is_dir():
        raise ValueError(f"the worktree of {repo_name}/{job_id} is gone: nothing to preview")
    port = int(repo.preview.get("port") or 18938)
    origin = R.normalize_origin(f"http://127.0.0.1:{port}")
    return ({"command": list(repo.preview["command"]), "dir": str(worktree), "port": port,
             "startup_seconds": repo.preview.get("startup_seconds") or 20,
             "job": f"{repo_name}/{job_id}", "branch": verify.get("branch")}, origin)


def _package_version(package: str) -> str | None:
    try:
        data = json.loads((Path(package) / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = data.get("version") if isinstance(data, dict) else None
    return version if isinstance(version, str) else None


def server_args(cfg: Any, grant: Path) -> Any:
    """The browser server's config with the grant's path appended to its args (sf-browser mcp <grant>)."""
    clone = copy.copy(cfg)
    clone.args = [*(cfg.args or ["mcp"]), str(grant)]
    return clone


def register(grant_id: str, client: Any) -> None:
    """Keep the locking run's client for revoke. A revoke that came first (before the client existed) closes it now:
    revoke marks the grant and looks for the client under the same lock, so one of the two always finds the other."""
    with _CLIENTS_LOCK:
        revoked = grant_id in _REVOKED
        if not revoked:
            _CLIENTS[grant_id] = client
    if revoked:
        threading.Thread(target=client.close, name=f"revoke-{grant_id}", daemon=True).start()


def unregister(grant_id: str) -> None:
    with _CLIENTS_LOCK:
        _CLIENTS.pop(grant_id, None)
        _REVOKED.discard(grant_id)


def revoke(data_dir: Path | str, grant_id: str, by: str = "person") -> dict:
    """Rule 3: revoked in the grant, then the locking run's client closed (the namespace ends with it; a commit in
    flight is recorded unknown by the shim and never sent again). KeyError for an unknown grant."""
    path = grant_path(data_dir, grant_id)
    if not path.is_file():
        raise KeyError(f"no grant {grant_id}")
    grant = revoke_grant(path, by)
    with _CLIENTS_LOCK:
        client = _CLIENTS.pop(grant_id, None)
        if client is None and not grant.get("ended"):
            _REVOKED.add(grant_id)   # its run may still be starting: register closes the client it brings
    if client is not None:
        threading.Thread(target=client.close, name=f"revoke-{grant_id}", daemon=True).start()
    return {"grant": grant_id, "revoked": grant["revoked"], "closed": client is not None}


# ---------------------------------------------------------------- receipts (§2.13)

RECEIPT_ACTION_KEYS = ("action_id", "tool", "class", "reclassified", "outcome", "origin", "observation_before",
                       "observation_after", "response", "expect", "path_hash", "url_after_hmac", "values_hmac",
                       "lock_number", "at", "ended", "settled_at_start", "settled_at_end", "error")


def _receipt_action(entry: dict) -> dict:
    line = {key: entry.get(key) for key in RECEIPT_ACTION_KEYS if key in entry}
    target = entry.get("target") if isinstance(entry.get("target"), dict) else None
    if target is not None:   # the cleaned name stays in the session folder's action log only
        line["target"] = {key: target.get(key) for key in ("ref", "role", "name_hmac", "observation")}
    return line


def receipt(data_dir: Path | str, grant_id: str) -> dict | None:
    """The receipt of a grant's run built from its session folder (None when the shim never started)."""
    folder = session_of_grant(data_dir, grant_id)
    if folder is None:
        return None
    try:
        grant = read_grant(grant_path(data_dir, grant_id))
    except ValueError:
        grant = {"grant": grant_id}
    record = read_record(folder / SESSION_FILE) or {}   # the namespace can write it: never trusted, never blocking
    entries = ActionLog.read(folder / ACTION_LOG)
    actions = [_receipt_action(entry) for entry in ActionLog.actions(folder / ACTION_LOG)]
    artifacts = []
    for entry in entries:
        if entry.get("kind") == "artifact" and isinstance(entry.get("artifact"), str):
            name = os.path.basename(entry["artifact"])
            artifacts.append({"kind": "screenshot", "name": name, "path": str(folder / "artifacts" / name),
                              "sha256": entry.get("sha256"), "bytes": entry.get("bytes"),
                              "observation": entry.get("observation"), "at": entry.get("at")})
    locked_by = grant.get("locked_by") or record.get("locked_by") or {}
    from .shim import worker_versions

    return {"receipt": "browser/1", "grant": grant_id, "session_lock": folder.name,
            "lock_number": record.get("lock_number"), "run": f"{locked_by.get('ref')}/{locked_by.get('run')}",
            "locked_by": locked_by, "profile": grant.get("profile") or record.get("profile"),
            "started": record.get("started"),
            "ended": grant.get("ended"), "revoked": grant.get("revoked"), "actions": actions,
            "refusals": [{key: entry.get(key) for key in ("tool", "code", "at")} for entry in entries
                         if entry.get("kind") == "refusal"],
            "login_required": sorted({str(entry.get("origin")) for entry in entries
                                      if entry.get("kind") == "login_required"}),
            "cancelled": [{key: entry.get(key) for key in ("tool", "at")} for entry in entries
                          if entry.get("kind") == "cancelled"],
            "artifacts": artifacts, "taint": "untrusted", "worker": worker_versions(grant)}


def finish(data_dir: Path | str, grant: Grant, *, open_item: Callable[..., Any] | None = None) -> dict | None:
    """Run end: the grant is ended, every action of the run still without an outcome becomes unknown (marked
    settled_at_end; §2.5, rule 4), the receipt written (receipt.json, 0600) and summarized for chat.json; each
    login_required opens one proposed system item for the owner ("Log in to <origin> again"; code-written).
    Settled only once the shim is gone, so it never writes an outcome after the settle's: the run's client is closed
    by now, but a revoke closes it on a thread of its own, so while the grant was revoked or an intent has no
    outcome, run end first waits (SHIM_EXIT_SECONDS at most) for the grant's lock, which the shim holds until it
    exits."""
    unregister(grant.id)
    ended: dict = {}
    with contextlib.suppress(OSError, ValueError):
        ended = update_grant(grant.path, ended=utc_now())
    folder = session_of_grant(data_dir, grant.id)
    if folder is not None:
        if ended.get("revoked") or ActionLog.dangling(folder / ACTION_LOG):
            wait_grant_free(data_dir, grant.id, SHIM_EXIT_SECONDS)
        settle_folder(folder, "settled_at_end")
    found = receipt(data_dir, grant.id)
    if found is None or folder is None:
        return {"grant": grant.id, "session_lock": None, "run": grant.locked_by.get("run"), "actions": 0,
                "commits": 0, "unknown": 0, "artifacts": [], "started": False}
    _write_private(folder / RECEIPT_FILE, found)
    items = []
    for origin in found["login_required"]:
        if open_item is None:
            break
        with contextlib.suppress(Exception):
            made = open_item({"title": f"Log in to {origin} again", "kind": "commitment", "status": "proposed",
                              "taint": ["untrusted"], "detail": f"A browsing run ({found['run']}) reached the login "
                              f"page of {origin}: the owner logs in himself (BROWSER_AND_CODEX.md §2.7).",
                              "source": {"type": "browser", "by": "factory", "grant": grant.id}},
                             dedupe=hashlib.sha256(f"browser-login:{origin}".encode()).hexdigest())
            items.append((made or {}).get("ref"))
    actions = found["actions"]
    return {"grant": grant.id, "session_lock": found["session_lock"], "run": grant.locked_by.get("run"),
            "profile": found["profile"], "actions": len(actions),
            "commits": sum(1 for a in actions if a.get("class") == "commit"),
            "reclassified": sum(1 for a in actions if a.get("reclassified")),
            "unknown": sum(1 for a in actions if a.get("outcome") == "unknown"),
            "refusals": len(found["refusals"]), "login_required": found["login_required"], "items": items,
            "artifacts": [{key: art[key] for key in ("name", "sha256", "bytes")} for art in found["artifacts"]],
            "revoked": bool(found.get("revoked")), "receipt": f"browser/sessions/{folder.name}/{RECEIPT_FILE}",
            "started": True, "label": UNTRUSTED_LABEL}


def chat_rows(data_dir: Path | str, record: dict) -> list[dict]:
    """The transcript's action rows of one browsing run: class, reclassified, outcome, the role, the cleaned name
    inside its data block (untrusted, for a person), and the screenshots."""
    folder = browser_root(data_dir) / "sessions" / str(record.get("session_lock") or "")
    if not record.get("session_lock") or not folder.is_dir():
        return []
    rows = []
    for entry in ActionLog.actions(folder / ACTION_LOG):
        target = entry.get("target") if isinstance(entry.get("target"), dict) else {}
        rows.append({"kind": "action", "action_id": entry.get("action_id"), "tool": entry.get("tool"),
                     "class": entry.get("class"), "reclassified": bool(entry.get("reclassified")),
                     "outcome": entry.get("outcome"), "role": target.get("role"),
                     "name": _block_text(target.get("name_block")), "origin": entry.get("origin"),
                     "requests": (entry.get("response") or {}).get("requests") or [],
                     "requests_unread": bool((entry.get("response") or {}).get("requests_unread")),
                     "expect": entry.get("expect"), "at": entry.get("at")})
    for entry in ActionLog.read(folder / ACTION_LOG):
        if entry.get("kind") == "artifact":
            rows.append({"kind": "screenshot", "artifact": os.path.basename(str(entry.get("artifact"))),
                         "sha256": entry.get("sha256"), "bytes": entry.get("bytes"), "at": entry.get("at")})
        elif entry.get("kind") == "refusal":
            rows.append({"kind": "refusal", "tool": entry.get("tool"), "code": entry.get("code"),
                         "at": entry.get("at")})
        elif entry.get("kind") == "login_required":
            rows.append({"kind": "login_required", "origin": entry.get("origin"), "at": entry.get("at")})
    return sorted(rows, key=lambda row: str(row.get("at") or ""))


def _block_text(block: Any) -> str | None:
    if not isinstance(block, str):
        return None
    lines = block.split("\n")
    return "\n".join(lines[1:-1]) if len(lines) >= 2 and lines[0].startswith("<factory-data") else block


def artifact_path(data_dir: Path | str, session_lock: str, name: str) -> Path:
    """A screenshot's file, for the GUI: only a plain name inside that session's artifacts folder."""
    from .session_locks import SESSION_LOCK_ID_RE

    if not SESSION_LOCK_ID_RE.fullmatch(str(session_lock)) or os.path.basename(name) != name or not name.endswith(
            ".png") or name.startswith("."):
        raise KeyError("no such screenshot")
    path = browser_root(data_dir) / "sessions" / session_lock / "artifacts" / name
    if not path.is_file() or path.is_symlink():
        raise KeyError("no such screenshot")
    return path


def card(config: Any) -> dict:
    """The browser card's facts: on or off, the runtime, the profiles and BC1's stated limits."""
    merged = settings(config)
    try:
        runtime = runtime_paths(config)
        runtime_problem = None
    except ValueError as exc:
        runtime, runtime_problem = {}, str(exc)
    return {"enabled": merged["enabled"], "runtime": runtime, "runtime_problem": runtime_problem,
            "profiles": sorted(merged["profiles"]), "preset": merged["preset"], "caps": merged["caps"],
            "profile_origins": {name: (entry.get("origins") if isinstance(entry, dict) else {}) or {}
                                for name, entry in merged["profiles"].items()},
            "limits": merged["limits"], "pins": merged["pins"], "shim": SHIM_VERSION,
            "limitations": [
                "Phase BC1: the fixture site and a job's preview only, inside the browser's own namespace with no "
                "network; no real sites, no logins, no uploads, no kept downloads.",
                "The session lock, the origin rules and the commit rules are enforced by the harness's own code "
                "running as the harness's user: any other unsandboxed process of that user could change them "
                "(cooperative). Real sites wait for BC2's worker under its own user.",
                "Chrome runs with --no-sandbox inside the harness's namespace (the owner's O11): for fixture pages "
                "only.",
                "Everything the browser read is untrusted: " + UNTRUSTED_LABEL + "."]}


def recent_sessions(data_dir: Path | str, limit: int = 20) -> list[dict]:
    """The newest session folders, summarized for the browser page: who locked it, when, counts, the receipt's
    presence; never page text."""
    from .session_locks import session_folders

    out = []
    for folder in reversed(session_folders(data_dir)[-limit:]):
        record = read_record(folder / SESSION_FILE)   # never trusted, never blocking (the namespace writes there)
        if record is None:
            continue
        actions = ActionLog.actions(folder / ACTION_LOG)
        entries = ActionLog.read(folder / ACTION_LOG)
        locked_by = record.get("locked_by") if isinstance(record.get("locked_by"), dict) else {}
        ref = str(locked_by.get("ref") or "")
        out.append({"session_lock": folder.name, "lock_number": record.get("lock_number"),
                    "profile": record.get("profile"), "grant": record.get("grant"), "started": record.get("started"),
                    "kind": locked_by.get("kind"), "chat": ref[5:] if ref.startswith("chat:") else None,
                    "actions": len(actions), "commits": sum(1 for a in actions if a.get("class") == "commit"),
                    "unknown": sum(1 for a in actions if a.get("outcome") == "unknown"),
                    "reclassified": sum(1 for a in actions if a.get("reclassified")),
                    "refusals": sum(1 for e in entries if e.get("kind") == "refusal"),
                    "screenshots": sum(1 for e in entries if e.get("kind") == "artifact"),
                    "receipt": (folder / RECEIPT_FILE).is_file()})
    return out

