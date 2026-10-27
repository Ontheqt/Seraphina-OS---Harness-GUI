"""The doctor's codex lines (BROWSER_AND_CODEX.md §4.4; DESIGN §44).

``codex`` (one line while the worker is off and no route uses it), ``codex version`` (the pinned binary, run exactly
as the launcher starts it), ``codex launcher`` (the launcher's hash; with a Codex user, the installed root copy and
its sudoers rule), ``codex home`` (the template's hash against the pin, the launcher's settings), ``codex model``
(the blocker of §3.3 (c): until the Responses route has passed its check in a swap window under a custody request
lease, whenever the endpoint is not loaded, and whenever the route's provider needs no lease; GETs only, unless
``factory doctor --probe`` asks for the check itself), ``codex sandbox`` (one sandboxed probe as the Codex user: a
denied canary, /tmp read-only, the workspace writable, no key in the command's environment; which bwrap Codex uses),
``codex reads`` (the extended read check: the Codex user cannot read the data directory, the config directory, the
key files and env files, the owner's private roots, nor what block c2's traverse entries above the data folder
open; under sudo-rs a password prompt reads as "not checked"), ``codex key`` (the launcher, as the Codex user,
gets the route's key from its key file exactly as a run does: the file is readable to it and defines the key's
name; the harness never reads that file, which is the Codex user's alone) and ``codex egress`` (a connect from the
Codex user to a public address, and to every other loopback port, fails, and the unit that loads the rule at boot is
enabled).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any, Callable

from . import CODEX_PIN, PRESET
from . import launch as L
from . import work
from .settings import SERVER_LAUNCHER, key_file, launcher_path, settings, work_root

Check = dict
PUBLIC_PROBE = "1.1.1.1:443"   # a public address the uid egress rule must make unreachable
NOT_CHECKED_SUDO = ("interactive authentication is required", "a password is required", "a terminal is required")


def codex_routes(config: Any) -> list[tuple[str, Any]]:
    """(agent, its ModelConfig) for each configured agent whose driver is a codex-worker driver."""
    out = []
    agents = (getattr(config, "raw", {}) or {}).get("agents") or {}
    for name, entry in sorted(agents.items()):
        if name == "*" or not isinstance(entry, dict) or not work.is_worker(config, entry.get("driver")):
            continue
        try:
            model = config.model(str(entry.get("model") or "local"))
        except (KeyError, ValueError):
            continue
        out.append((name, model))
    default = agents.get("*") if isinstance(agents.get("*"), dict) else {}
    if work.is_worker(config, default.get("driver")) and not out:
        try:
            out.append(("*", config.model(str(default.get("model") or "local"))))
        except (KeyError, ValueError):
            pass
    return out


def all_checks(config: Any, check: Callable[..., Check], probe: bool = False,
               run: Callable[..., Any] | None = None) -> list[Check]:
    merged = settings(config)
    routes = codex_routes(config)
    if not merged["enabled"] and not routes:
        return [check("codex", True, "info", f"off (codex.enabled is false; no agent runs on {PRESET})")]
    run = run or _run
    out = [check("codex", merged["enabled"], "warn",
                 "on" if merged["enabled"] else f"off, but {', '.join(a for a, _ in routes)} is routed to {PRESET}: "
                 f"such a job does not start")]
    out.append(_version(config, check, run))
    out.append(_launcher(config, check, run))
    out.append(_home(config, check))
    for agent, model in routes or [("builder", None)]:
        if model is not None:
            out.append(_model(config, check, agent, model, probe))
    out.append(_sandbox(config, check, run))
    out.append(_reads(config, check, run))
    out.append(_key(config, check, run))
    out.append(_egress(config, check, run))
    return out


def _run(argv: list[str], timeout: float = 60.0, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
                          env=env if env is not None else {"PATH": L.DEFAULT_PATH, "LANG": "C.UTF-8"})


def _launcher_json(config: Any, run: Callable[..., Any], verb: str, *args: str) -> tuple[dict | None, str]:
    try:
        done = run(work.launcher_argv(config, verb, *args))
    except (OSError, subprocess.SubprocessError, work.WorkError) as exc:
        return None, str(exc)
    err = (done.stderr or b"").decode("utf-8", "replace").strip()
    text = (done.stdout or b"").decode("utf-8", "replace").strip()
    try:
        return json.loads(text.splitlines()[-1]), err
    except (ValueError, IndexError):
        return None, err or f"the launcher answered {done.returncode} with no JSON"


def _version(config: Any, check: Callable[..., Check], run: Callable[..., Any]) -> Check:
    merged = settings(config)
    binary = merged.get("binary")
    if not binary:
        from .setup import DEFAULT_BINARY

        return check("codex version", False, "error", f"codex.binary is not set: the setup's block c7 installs the "
                     f"pinned Codex {merged['pin']} at {DEFAULT_BINARY}; set codex.binary to it (never a codex found "
                     f"on PATH)")
    try:
        done = run(work.launcher_argv(config, "version"))
    except (OSError, subprocess.SubprocessError, work.WorkError) as exc:
        return check("codex version", False, "error", f"the launcher could not run it: {exc}")
    text = (done.stdout or b"").decode("utf-8", "replace").strip()
    if done.returncode != 0:
        err = (done.stderr or b"").decode("utf-8", "replace").strip()
        return check("codex version", False, "error", f"{binary} did not answer --version: {err or text}"[:400])
    ok = text.split()[-1:] == [merged["pin"]]
    note = "" if merged["pin"] == CODEX_PIN else f" (the design's pin is {CODEX_PIN})"
    return check("codex version", ok, "error", f"{text} at {binary}, as the launcher starts it{note}" if ok else
                 f"{binary} is {text or 'unknown'}, not the pin {merged['pin']}: an upgrade is a reviewed change "
                 f"with the containment probes (T19) run again")


def file_sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def _launcher(config: Any, check: Callable[..., Check], run: Callable[..., Any]) -> Check:
    merged = settings(config)
    source = file_sha256(Path(L.__file__))
    if not merged.get("user"):
        return check("codex launcher", True, "warn", f"this checkout's bin/sf-codex-launch (sha256 {source[:16]}…), "
                     f"as the harness's own user: no Codex user yet (codex.user), so this is the test suite's "
                     f"containment, not the server's (O3)")
    installed = launcher_path(config)
    found = file_sha256(installed)
    if found is None:
        return check("codex launcher", False, "error", f"{installed} is not installed (the setup's block c3)")
    if found != source:
        return check("codex launcher", False, "error", f"{installed} (sha256 {found[:16]}…) is not this version's "
                     f"launcher ({source[:16]}…): install it again (block c3)")
    if merged["via"] != "sudo":
        return check("codex launcher", True, "warn", f"{installed} matches; started through setpriv (a harness "
                     f"running as root), not sudo")
    try:
        done = run(["sudo", "-n", "-l", "-u", str(merged["user"]), str(installed)])
    except (OSError, subprocess.SubprocessError) as exc:
        return check("codex launcher", False, "error", f"sudo could not be asked: {exc}")
    err = (done.stderr or b"").decode("utf-8", "replace").lower()
    if any(word in err for word in NOT_CHECKED_SUDO):
        return check("codex launcher", False, "warn", "the sudoers rule is not checked: sudo wants a password "
                     "(it is not the NOPASSWD rule of block c5)")
    ok = done.returncode == 0
    return check("codex launcher", ok, "error", f"{installed} matches this version (sha256 {found[:16]}…) and the "
                 f"sudoers rule lets this user run it as {merged['user']}" if ok else
                 f"no sudoers rule lets this user run {installed} as {merged['user']} (block c5)")


def _home(config: Any, check: Callable[..., Check]) -> Check:
    if L.TEMPLATE_SHA256 != L.TEMPLATE_PIN:
        return check("codex home", False, "error", f"the CODEX_HOME template's hash {L.TEMPLATE_SHA256[:16]}… is not "
                     f"the pinned {L.TEMPLATE_PIN[:16]}…: a template change is a reviewed change (T19 again)")
    merged = settings(config)
    detail = (f"the template's sha256 {L.TEMPLATE_SHA256[:16]}… is the pin; a home is checked before every spawn "
              f"(no rules, AGENTS.md, skills, hooks or anything but Codex's own state)")
    if merged.get("user") and merged["via"] == "sudo":
        from .settings import launch_settings_path
        from .setup import staged_settings

        path = launch_settings_path(config)
        if not path.is_file():
            return check("codex home", False, "error", f"{path} (the launcher's settings) is not installed (block c4)")
        try:
            installed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return check("codex home", False, "error", f"{path} (the launcher's settings) cannot be read: {exc}")
        want = staged_settings(config)
        differ = [key for key in sorted(want) if not isinstance(installed, dict) or installed.get(key) != want[key]]
        if differ:
            return check("codex home", False, "error", f"{path} differs from this configuration in "
                         f"{', '.join(differ)}: stage again (block c0) and repeat block c4")
    return check("codex home", True, "info", detail)


def _model(config: Any, check: Callable[..., Check], agent: str, model: Any, probe: bool) -> Check:
    name = f"codex model {agent}"
    if probe:
        from ..custody import needs_lease

        if needs_lease(model.provider):
            record = work.responses_check(config, model)
            if not record.get("sent"):
                return check(name, False, "error", f"the Responses check sent nothing: {record.get('detail')}")
    blocker = work.model_blocker(config, model.provider, model.model)
    if blocker:
        return check(name, False, "error", f"blocker: {blocker}")
    record = work.check_record(config).get(_server(model.provider.base_url)) or {}
    return check(name, True, "info", f"{model.provider.name}/{model.model}: Responses route checked {record.get('at')} "
                 f"(HTTP {record.get('status')}, body sha256 {str(record.get('body_sha256') or '')[:12]}…), loaded, "
                 f"and every run takes a custody request lease")


def _server(base_url: str) -> str:
    from ..custody import server_of

    return server_of(base_url)


def _sandbox(config: Any, check: Callable[..., Check], run: Callable[..., Any]) -> Check:
    if not settings(config).get("binary"):
        return check("codex sandbox", False, "warn", "not checked: codex.binary is not set")
    found, err = _launcher_json(config, run, "sandbox-probe")
    if found is None:
        return check("codex sandbox", False, "error", f"the sandboxed probe did not run: {err}"[:400])
    bwrap = found.get("bwrap") or "none on its PATH"
    if found.get("ok"):
        return check("codex sandbox", True, "info", f"a sandboxed probe as the Codex user: the denied canary is "
                     f"unreadable, /tmp is read-only, the workspace is writable and the key is not in the "
                     f"command's environment; Codex uses {bwrap}")
    wrong = [what for key, what in (("canary_read", "the denied canary was readable"),
                                     ("tmp_write", "/tmp was writable"), ("key_seen", "the key reached the command"))
             if found.get(key)]
    if not found.get("ws_write"):
        wrong.append("the workspace was not writable")
    if not found.get("ran"):
        wrong.append("the sandboxed command did not run (one level of bwrap must work; it is never nested)")
    return check("codex sandbox", False, "error", "; ".join(wrong) + f" (Codex uses {bwrap})")


def read_paths(config: Any) -> list[str]:
    """What the Codex user must not read (§3.11 item 3): the data directory and what is below it (every top-level
    entry but jobs/, the worktrees and the work root, each with one file found inside it, such as a chat's
    transcript; each jobs/<repo>/, which it may pass through but never list; and the folder, job.json and one log of
    each job Codex ran in but has no run in flight, whose share a harness killed mid-run, or an older version, would
    have left), the config directory, every key file and env file but the Codex route's own provider key (the
    launcher reads that one as the Codex user and hands it to Codex only; the profile denies it to every command),
    the owner's private roots, and what block c2's traverse entries on the folders above the data folder open
    (setup.exposed_below: in a 0750 home, the personal folders and dotfiles other users may read; an entry holding a
    configured repository is left out, since a Codex job reads its own repository's git folder, and each job's
    profile denies every other one). A data folder the Codex user may pass through is only as private as what is in
    it, so the check looks below it."""
    from .setup import exposed_below

    repos = [os.path.abspath(str(repo.path)) for repo in _repos(config)]
    above = [path for path in exposed_below(config)
             if not any(repo == path or repo.startswith(path.rstrip("/") + "/") for repo in repos)]
    out = [str(Path(config.data_dir)), *_data_paths(config), str(config.path.parent), str(config.path), *above]
    env_file = config.path.parent / "env"
    out.append(str(env_file))
    own = set(route_key_files(config).values())   # the launcher reads the route's own key, as the Codex user (§3.9)
    for entry in ((getattr(config, "raw", {}) or {}).get("providers") or {}).values():
        if isinstance(entry, dict) and entry.get("api_key_file"):
            path = os.path.expanduser(str(entry["api_key_file"]))
            if path not in own:
                out.append(path)
    out += [os.path.expanduser(str(p)) for p in ((getattr(config, "raw", {}) or {}).get("codex") or {}).get(
        "private_roots") or []]
    return [p for p in dict.fromkeys(out) if os.path.lexists(p)]


def _repos(config: Any) -> list[Any]:
    try:
        return list(config.repos())
    except (AttributeError, KeyError, ValueError, OSError):
        return []


def _data_paths(config: Any) -> list[str]:
    data = Path(config.data_dir)
    keep = {"jobs", "worktrees"}
    with contextlib.suppress(ValueError, IndexError):
        keep.add(work_root(config).relative_to(data).parts[0])
    out = []
    with contextlib.suppress(OSError):
        for name in sorted(os.listdir(data)):
            if name not in keep:
                out += [str(data / name), *_one_file(data / name)]
    with contextlib.suppress(OSError):
        for repo in sorted(os.listdir(data / "jobs")):
            out.append(str(data / "jobs" / repo))
            with contextlib.suppress(OSError):
                out += [path for job in sorted(os.listdir(data / "jobs" / repo))
                        if _shared_once(data / "jobs" / repo / job)
                        for path in (str(data / "jobs" / repo / job), str(data / "jobs" / repo / job / "job.json"),
                                     *_one_file(data / "jobs" / repo / job / "logs", budget=50))]
    return out


def _shared_once(job_dir: Path) -> bool:
    """Whether a job folder may still carry a Codex run's share: Codex ran in the job (a run with its labels or a
    receipt) and has no run in flight (codex_run: during a run the folder is the group's to read by design). A
    job.json that cannot be read counts as one; a job Codex never ran in was never shared and is not probed, so the
    probe's argument list grows with Codex's jobs only."""
    try:
        found = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    if not isinstance(found, dict):
        return True
    if isinstance(found.get("codex_run"), dict):
        return False
    runs = found.get("runs") if isinstance(found.get("runs"), list) else []
    return any(isinstance(entry, dict) and ("codex" in entry or "receipt" in entry) for entry in runs)


def _one_file(folder: Path, budget: int = 400) -> list[str]:
    """The first regular file below folder (sorted, no symlink followed, at most budget entries looked at)."""
    for top, dirs, files in os.walk(folder, followlinks=False):
        dirs.sort()
        for name in sorted(files):
            path = os.path.join(top, name)
            with contextlib.suppress(OSError):
                if stat.S_ISREG(os.lstat(path).st_mode):
                    return [path]
        budget -= len(dirs) + len(files)
        if budget <= 0:
            break
    return []


def _reads(config: Any, check: Callable[..., Check], run: Callable[..., Any]) -> Check:
    merged = settings(config)
    if not merged.get("user"):
        return check("codex reads", False, "warn", "not checked: no Codex user (codex.user), so Codex's own process "
                     "reads what the harness's user reads; only its permissions profile keeps its commands out")
    paths = read_paths(config)
    found, err = _launcher_json(config, run, "can-read", *sum((["--path", p] for p in paths), []))
    if found is None:
        low = err.lower()
        if any(word in low for word in NOT_CHECKED_SUDO):
            return check("codex reads", False, "warn", "not checked: sudo wants a password")
        return check("codex reads", False, "error", f"not checked: {err}"[:400])
    readable = [path for path, ok in (found.get("readable") or {}).items() if ok]
    if readable:
        return check("codex reads", False, "error", f"the Codex user can read {', '.join(readable[:5])}: fix the "
                     f"permissions before any Codex job on this machine")
    return check("codex reads", True, "info", f"the Codex user can read none of {len(paths)} paths (the data "
                 f"directory and what is below it, the config directory, key and env files, the private roots)")


def route_key_files(config: Any) -> dict[str, str | None]:
    """{provider name: the key file its launcher entry names} for each Codex route's provider (settings.key_file)."""
    return {model.provider.name: key_file(config, getattr(model.provider, "api_key_file", None))
            for _, model in codex_routes(config)}


def _key(config: Any, check: Callable[..., Check], run: Callable[..., Any]) -> Check:
    """The launcher's key-check, as the Codex user: the harness itself cannot read the route's key file (block c4
    installs it the Codex user's alone), so the launcher says, by name and yes or no only, whether a run would get
    the key."""
    merged = settings(config)
    files = route_key_files(config)
    if not merged.get("user"):
        return check("codex key", True, "info", "no Codex user: the launcher reads the route's key as the harness's "
                     "own user")
    if not files:
        return check("codex key", True, "info", f"no agent runs on {PRESET}, so no route's key is read")
    missing = sorted(name for name, path in files.items() if not path)
    if missing:
        return check("codex key", False, "error", f"the Codex route's provider {', '.join(missing)} has no key file "
                     f"the Codex user can read, so every run would be refused: set codex.key_file, or install the "
                     f"staged key (block c4)")
    paths = sorted(set(files.values()))
    found, err = _launcher_json(config, run, "key-check")
    if found is None:
        low = err.lower()
        if any(word in low for word in NOT_CHECKED_SUDO):
            return check("codex key", False, "warn", "not checked: sudo wants a password")
        return check("codex key", False, "error", f"not checked: {err}"[:400])
    answers = found.get("providers")
    if not isinstance(answers, dict):
        return check("codex key", False, "error", "not checked: the installed launcher has no key-check (install "
                     "this version's launcher, block c3)")
    wrong, names = [], []
    for _, model in codex_routes(config):
        url = str(getattr(model.provider, "base_url", "") or "").rstrip("/")
        answer = answers.get(url)
        if not isinstance(answer, dict):
            wrong.append(f"{url} is not in the launcher's settings (stage again, block c0, and repeat block c4)")
        elif not answer.get("defined"):
            wrong.append(str(answer.get("reason") or f"no key for {url}"))
        else:
            names.append(str(answer.get("env_key") or "?"))
    if wrong:
        user = merged["user"]
        return check("codex key", False, "error", f"{'; '.join(dict.fromkeys(wrong))}: every run would be refused. "
                     f"Block c4 installs the key file {user}:root 0400 (the Codex user's alone); write NAME=<the key> "
                     f"in it with sudo (a keyless local server needs a placeholder value)"[:600])
    return check("codex key", True, "info", f"the Codex user gets its route's key {', '.join(dict.fromkeys(names))} "
                 f"from {', '.join(paths)}, which the harness never reads (the profile denies it to every command)")


def egress_targets(config: Any) -> list[str]:
    """A public address and every loopback port of a configured provider (with a key or keyless, used by a route or
    not) other than the Codex routes' own (setup.model_ports, the ports the egress rule allows)."""
    import urllib.parse

    from .setup import model_ports

    allowed = {port for _, port in model_ports(config)}
    others = set()
    for entry in ((getattr(config, "raw", {}) or {}).get("providers") or {}).values():
        if isinstance(entry, dict) and entry.get("base_url"):
            parts = urllib.parse.urlsplit(str(entry["base_url"]))
            if not parts.port or parts.port in allowed:
                continue
            if parts.hostname in ("127.0.0.1", "localhost"):
                others.add(f"127.0.0.1:{parts.port}")
            elif parts.hostname == "::1":
                others.add(f"[::1]:{parts.port}")
    return [PUBLIC_PROBE, *sorted(others)]


def egress_unit_enabled() -> bool:
    """Whether the unit that loads the egress rule at boot is enabled (the link `systemctl enable` makes)."""
    from .setup import EGRESS_UNIT_LINK

    return os.path.lexists(EGRESS_UNIT_LINK)


def _egress(config: Any, check: Callable[..., Check], run: Callable[..., Any]) -> Check:
    merged = settings(config)
    if not merged.get("user"):
        return check("codex egress", False, "warn", "not checked: no Codex user, so no uid egress rule can hold it "
                     "(the setup's block c6)")
    from .setup import EGRESS_UNIT

    targets = egress_targets(config)
    found, err = _launcher_json(config, run, "egress-probe", *sum((["--target", t] for t in targets), []))
    if found is None:
        return check("codex egress", False, "error", f"not checked: {err}"[:400])
    reached = [target for target, ok in (found.get("connected") or {}).items() if ok]
    if reached:
        return check("codex egress", False, "error", f"the Codex user reached {', '.join(reached)}: the nftables "
                     f"rule of block c6 is not in place")
    if not egress_unit_enabled():
        return check("codex egress", False, "warn", f"a connect from the Codex user to {', '.join(targets)} fails "
                     f"now, but {EGRESS_UNIT} is not enabled, so the rule is gone after the next boot (block c6); "
                     f"until it is loaded again no Codex run starts")
    return check("codex egress", True, "info", f"a connect from the Codex user to {', '.join(targets)} fails; "
                 f"{EGRESS_UNIT} loads the rule at every boot")


def which_bwrap() -> str | None:
    return shutil.which("bwrap", path=L.DEFAULT_PATH)
