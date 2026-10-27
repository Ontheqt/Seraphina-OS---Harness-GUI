"""The doctor's browser lines (BROWSER_AND_CODEX.md §4.3; DESIGN §43): ``browser pins`` (K1, T26), ``browser
sandbox``, ``browser timeouts`` and ``browser preset`` (K13, T23). The Open WebUI pin of K14 (T25) is the existing
``openwebui <provider> build`` line (DESIGN §33). Nothing here changes anything; the probes start short-lived
namespaces of their own and Chrome with ``--version`` only."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from ..harness.sandbox import bwrap_available, bwrap_size_supported, bwrap_version, minimal_args, nested_bwrap
from . import runs

Check = dict
PROBE_SECONDS = 20


def _sha256(path: str) -> str | None:
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _run(argv: list[str], seconds: float = PROBE_SECONDS) -> tuple[int | None, str]:
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=seconds,
                              env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": "/nonexistent"})
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"{type(exc).__name__}"
    return done.returncode, (done.stdout or done.stderr or "").strip()


def _inside(path: str, root: str) -> bool:
    real, base = os.path.realpath(path), os.path.realpath(root)
    return real == base or real.startswith(base.rstrip("/") + "/")


def lock_integrity(package: str) -> str | None:
    """The npm integrity the install recorded for @playwright/mcp (node_modules/.package-lock.json, or the
    package-lock.json beside node_modules)."""
    modules = Path(package).parent.parent
    for lock in (modules / ".package-lock.json", modules.parent / "package-lock.json"):
        try:
            data = json.loads(lock.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        entry = (data.get("packages") or {}).get("node_modules/@playwright/mcp") if isinstance(data, dict) else None
        if isinstance(entry, dict) and isinstance(entry.get("integrity"), str):
            return entry["integrity"]
    return None


def pins(config: Any, check: Callable[..., Check], run: Callable[[list[str]], tuple[int | None, str]] = _run
         ) -> list[Check]:
    """browser pins (K1, T26): the harness's own Node, Playwright MCP and Chrome, inside browser.runtime.dir, each
    with the recorded version, hash and npm integrity; never a program found on PATH."""
    merged = runs.settings(config)
    try:
        paths = runs.runtime_paths(config)
    except ValueError as exc:
        return [check("browser pins", False, "error" if merged["enabled"] else "info", str(exc))]
    root = merged["runtime"].get("dir")
    problems: list[str] = []
    found: list[str] = []
    for key in ("node", "playwright_mcp", "chrome"):
        if not os.path.exists(paths[key]):
            problems.append(f"{key} is missing ({paths[key]})")
        elif root and not _inside(paths[key], os.path.expanduser(root)):
            problems.append(f"{key} resolves outside the runtime directory ({os.path.realpath(paths[key])}): the "
                            f"grant names only the harness's own runtime")
    if problems:
        return [check("browser pins", False, "error", "; ".join(problems))]
    pin = merged["pins"]
    code, out = run([paths["node"], "--version"])
    node_version = out.lstrip("v") if code == 0 else None
    node_hash = _sha256(paths["node"])
    if pin["node"].get("version") and node_version != str(pin["node"]["version"]).lstrip("v"):
        problems.append(f"Node is {node_version or 'not runnable'}, the pin {pin['node']['version']}")
    if pin["node"].get("sha256") and node_hash != pin["node"]["sha256"]:
        problems.append("Node's sha256 differs from the pin")
    found.append(f"Node {node_version or '?'}")
    package = runs._package_version(paths["playwright_mcp"])
    integrity = lock_integrity(paths["playwright_mcp"])
    if package != pin["playwright_mcp"].get("version"):
        problems.append(f"Playwright MCP is {package or 'not installed'}, the pin {pin['playwright_mcp'].get('version')}")
    if pin["playwright_mcp"].get("integrity") and integrity != pin["playwright_mcp"]["integrity"]:
        problems.append("Playwright MCP's npm integrity differs from the pin" if integrity else
                        "Playwright MCP's npm integrity is not recorded (no package-lock)")
    found.append(f"Playwright MCP {package or '?'}")
    code, out = run([paths["chrome"], "--version"])
    chrome_version = out.split()[-1] if code == 0 and out else None
    chrome_hash = _sha256(paths["chrome"])
    if pin["chrome"].get("version") and chrome_version != pin["chrome"]["version"]:
        problems.append(f"Chrome is {chrome_version or 'not runnable'}, the pin {pin['chrome']['version']}")
    if pin["chrome"].get("sha256") and chrome_hash != pin["chrome"]["sha256"]:
        problems.append("Chrome's sha256 differs from the pin")
    found.append(f"Chrome {chrome_version or '?'}")
    unpinned = [name for name, entry in (("Node", pin["node"]), ("Chrome", pin["chrome"])) if not entry.get("sha256")]
    if problems:
        return [check("browser pins", False, "error", "; ".join(problems) + f" (found {', '.join(found)})")]
    note = f"; no sha256 pinned yet for {' and '.join(unpinned)}" if unpinned else ""
    return [check("browser pins", not unpinned, "warn", f"{', '.join(found)} in the harness's own runtime "
                                                        f"directory{note}")]


def probe_namespace(config: Any) -> tuple[bool, str]:
    """Chrome starts inside the minimal binds (--version) and, from the same namespace, the host's loopback, a
    tailnet address and a public host all fail. (ok, what it found)."""
    try:
        paths = runs.runtime_paths(config)
    except ValueError as exc:
        return False, str(exc)
    from .namespace import _python_binds, runtime_binds

    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    script = ("import socket, json, subprocess, sys\nout = {}\n"
              f"for name, addr in (('loopback', ('127.0.0.1', {port})), ('tailnet', ('100.100.100.100', 80)),"
              " ('public', ('1.1.1.1', 443))):\n"
              "    s = socket.socket(); s.settimeout(2)\n"
              "    try:\n        s.connect(addr); out[name] = 'reached'\n"
              "    except OSError:\n        out[name] = 'failed'\n"
              "done = subprocess.run([sys.argv[1], '--version'], capture_output=True, text=True, timeout=15)\n"
              "out['chrome'] = done.stdout.strip() if done.returncode == 0 else 'failed: ' + done.stderr[-200:]\n"
              "print(json.dumps(out))\n")
    argv = minimal_args(ro=[*_python_binds(), *runtime_binds({k: v for k, v in paths.items()})]) + [
        os.path.realpath(sys.executable), "-I", "-c", script, paths["chrome"]]
    try:
        code, out = _run(argv, 40)
    finally:
        listener.close()
    try:
        seen = json.loads(out.splitlines()[-1]) if code == 0 else None
    except (ValueError, IndexError):
        seen = None
    if seen is None:
        return False, f"the namespace did not start ({out[-200:] or code})"
    reached = [name for name in ("loopback", "tailnet", "public") if seen.get(name) != "failed"]
    if reached:
        return False, f"the namespace reached {', '.join(reached)}: it must have no network"
    if str(seen.get("chrome", "")).startswith("failed"):
        return False, f"Chrome does not start with the minimal binds: {seen['chrome']}"
    return True, f"{seen['chrome']} starts with the minimal binds; the host's loopback, a tailnet address and a " \
                 f"public host all fail"


def probe_sibling() -> tuple[bool, str]:
    """A first-level sibling bwrap (the preview's, §2.9) starts and answers on a Unix socket passed to it."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "probe.sock")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(path)
        listener.listen(1)
        listener.set_inheritable(True)
        script = ("import socket, sys\ns = socket.socket(fileno=int(sys.argv[1]))\nc, _ = s.accept()\n"
                  "c.sendall(b'ok')\nc.close()\n")
        from .namespace import _python_binds

        argv = minimal_args(ro=_python_binds()) + [os.path.realpath(sys.executable), "-I", "-c", script,
                                                    str(listener.fileno())]
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                    pass_fds=(listener.fileno(),))
        except OSError as exc:
            listener.close()
            return False, f"bwrap did not start ({exc.strerror or exc})"
        listener.close()
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(10)
        try:
            probe.connect(path)
            answer = probe.recv(8)
        except OSError as exc:
            answer = b""
            why = str(exc)
        finally:
            probe.close()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        stderr = (proc.stderr.read() if proc.stderr else b"").decode("utf-8", "replace").strip()
        if proc.stderr:
            proc.stderr.close()
        if answer == b"ok":
            return True, "a sibling namespace starts and its socket answers"
        return False, f"a sibling namespace did not answer ({stderr[:200] or why})"


def sandbox(config: Any, check: Callable[..., Check]) -> list[Check]:
    """browser sandbox: bubblewrap and its --size, the namespace probe, the sibling for a preview, and the nested
    sandbox named as a limitation (never a failure of harness.sandbox)."""
    merged = runs.settings(config)
    level = "error" if merged["enabled"] else "warn"
    if not bwrap_available():
        return [check("browser sandbox", False, level, "bubblewrap is missing or cannot create a namespace here: the "
                                                       "browser needs it (apt install bubblewrap)")]
    out = []
    version = bwrap_version() or "?"
    if not bwrap_size_supported():
        out.append(check("browser sandbox", False, level, f"bubblewrap {version} has no --size: the download "
                                                          f"tmpfs cannot be capped (0.9.0 or later has it)"))
    else:
        ok, words = probe_namespace(config) if merged["enabled"] or _runtime_set(config) else (
            True, "not probed: the runtime is not set up")
        out.append(check("browser sandbox", ok, level, f"bubblewrap {version} with --size; {words}"))
    sibling, why = probe_sibling()
    out.append(check("browser preview sibling", sibling, "warn",
                     why if sibling else f"{why}: \"Verify in the browser\" would run without a live preview"))
    nested, reason = nested_bwrap()
    out.append(check("browser nested sandbox", True, "info",
                     "a sandbox inside a sandbox works here; Chrome still runs with --no-sandbox inside the "
                     "harness's namespace for fixture pages (O11)" if nested else
                     f"no nested sandbox here ({reason}): Chrome runs with --no-sandbox for fixture pages, and "
                     f"Codex is never nested (the recorded, expected state; O11)"))
    return out


def _runtime_set(config: Any) -> bool:
    try:
        runs.runtime_paths(config)
        return True
    except ValueError:
        return False


def timeouts(config: Any, check: Callable[..., Check]) -> list[Check]:
    """browser timeouts: the shim's own limit stays below the harness's MCP timeout, so the shim, not the harness,
    records an unknown outcome (§2.5)."""
    merged = runs.settings(config)
    try:
        servers = config.mcp_servers()
    except ValueError as exc:
        return [check("browser timeouts", False, "error", str(exc))]
    found = [server for server in servers.values() if runs.is_browser_server(server)]
    if not found:
        return [check("browser timeouts", True, "info", "no browser server configured (the browser preset adds it)")]
    shim = max(float(merged["limits"]["call_seconds"]), float(merged["limits"]["commit_seconds"]))
    out = []
    for server in found:
        ok = shim + 5 < float(server.timeout)
        out.append(check("browser timeouts", ok, "error",
                         f"the shim's limit {shim:g}s is below mcp_servers.{server.name}.timeout {server.timeout:g}s"
                         if ok else f"the shim's limit {shim:g}s is not below mcp_servers.{server.name}.timeout "
                                    f"{server.timeout:g}s by 5s: the harness would time out first and the shim "
                                    f"could not record the unknown"))
    return out


def preset(config: Any, check: Callable[..., Check]) -> list[Check]:
    """browser preset (K13, T23): every browsing persona's Open WebUI preset carries no writer and no transmitter,
    and every other browsing agent stays off Open WebUI (its preset would never be read; chats.py refuses it)."""
    from .. import personas
    from ..agentdefs import discover_agents
    from . import preset as P

    try:
        found = personas.discover_personas(config)
        servers = config.mcp_servers()
        agents = discover_agents(personas.persona_dirs(config))
    except ValueError as exc:
        return [check("browser preset", False, "warn", str(exc))]
    browsing = [p for p in found.values() if p.agent is not None and runs.browses(p.agent, servers)
                and p.kind == "openwebui"]
    out = []
    for name, agent in sorted(agents.items()):
        persona = found.get(name)
        if not runs.browses(agent, servers) or (persona is not None and persona.kind == "openwebui"):
            continue
        try:
            model = config.agent_model(agent)
        except (KeyError, ValueError):
            continue   # no model it could run on: no turn starts
        if getattr(model.provider, "kind", None) == personas.OPENWEBUI_KIND:
            out.append(check(f"browser preset {name}", False, "error",
                             f"agent {name!r} browses through Open WebUI (models.{model.key}) without being a browsing "
                             f"persona: its chats are refused (BROWSER_AND_CODEX.md §2.10 rule 7); bind it to the "
                             f"checked preset with an openwebui: line, or give it a model on this machine"))
    if not browsing:
        return out or [check("browser preset", True, "info",
                             f"no browsing persona yet (the design's {runs.settings(config)['preset']!r} preset is "
                             f"checked once a <persona>-browser file binds to it)")]
    for persona in sorted(browsing, key=lambda p: p.name):
        model_id = (persona.binding or {}).get("model")
        try:
            api = personas.client(config, persona)
        except (KeyError, personas.OpenWebUIError) as exc:
            out.append(check(f"browser preset {persona.name}", False, "warn", str(exc.args[0] if exc.args else exc)))
            continue
        result = P.check(api, str(model_id), local_ids=personas.local_model_ids(config),
                         checked_by_hand=runs.settings(config)["preset_checked_by_hand"])
        if result["ok"]:
            notes = f" ({'; '.join(result['notes'])})" if result["notes"] else ""
            out.append(check(f"browser preset {persona.name}", True, "info",
                             f"{model_id!r} carries no writer and no transmitter{notes}"))
        else:
            out.append(check(f"browser preset {persona.name}", False, "error", P.reason(result, str(model_id))))
    return out


def all_checks(config: Any, check: Callable[..., Check]) -> list[Check]:
    merged = runs.settings(config)
    try:
        servers = config.mcp_servers()
    except ValueError:
        servers = {}
    configured = any(runs.is_browser_server(server) for server in servers.values())
    if not merged["enabled"] and not configured:
        return [check("browser", True, "info", "off (browser.enabled is false; DESIGN §43): nothing to check")]
    out = [check("browser", True, "info", f"{'on' if merged['enabled'] else 'off'}; profiles "
                                          f"{', '.join(sorted(merged['profiles']))}; caps {merged['caps']['max_gb']} GB "
                                          f"and {merged['caps']['max_days']} days")]
    out += pins(config, check)
    out += sandbox(config, check)
    out += timeouts(config, check)
    out += preset(config, check)
    return out
