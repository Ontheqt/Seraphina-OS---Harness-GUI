"""Mock backend for the Your server area: DESIGN §42 and docs/seraphina/YOUR_SERVER.md §11.6, phases P1a to P1c (it
only looks, but for the list of updates refreshed and kept names, when allowed). See tests/gui_mock/__init__.py for the
hooks.

The answers are swfactory/yourserver.py's own: each state builds a fresh placeholder server (tests/sfsystem_rig.py:
Docker 29 state files, cgroups, Tailscale state files, a login gateway's nginx config) and a real YourServer reading it
through the real bin/sf-system (run in this process, as `sudo -n` would), with fake probes and a fake Beszel. So tones,
codes, words and shapes are the module's; only the server is made up.

Routes: GET /api/yourserver, POST /api/yourserver/refresh, GET /api/yourserver/apps, GET|POST
/api/yourserver/apps/<a-id>, POST .../address, GET .../qr, POST .../ask (a canned local model's answer), GET|POST
/api/yourserver/review, POST /api/yourserver/groups, GET /api/yourserver/setup, POST /api/yourserver/setup/stage|check,
GET /api/yourserver/checks, POST /api/yourserver/checks/<check>, GET /api/yourserver/apps/<a-id>/logs, POST
/api/yourserver/keep, GET /api/yourserver/jobs[/<J-n>], GET /api/yourserver/receipts[/<R-n>], GET /api/yourserver/root,
POST /api/yourserver/root/check, POST /api/yourserver/root/<row>/mark, and POST /api/config {yourserver: {...}} (Turn
it on, That's mine, the links).

States (POST /__mock/yourserver/<state>): off; absent (no system access: the summary card); unsafe (a file with the
wrong owner); review (nothing confirmed: the review list, some rows ticked); ready (the seed: every address confirmed,
every app answering); mixed (an app down with an error, one stopped through Docker with exit 137 after its grace, one
answering with errors, a login gateway with a part down, an app kept off by choice, an old copy that starts at boot);
stale (facts 30 minutes old: never green); docker_stopped; outside_edit (apps.json changed outside the API: the Open
button waits); beszel_down, beszel_not_recording, beszel_no_login; gpu_off (the driver not loaded); hostile (names
with markup, a right-to-left override, a 600-character name, a javascript: address and one on another tailnet, as
saved in the record). From P1b and P1c (Seraphina runs as her own account, her agents in bwrap, every class allowed):
checks (every Check run once, an update waiting in its folder, a newer copy downloaded, an app Docker reports
unhealthy, logs readable with hostile lines); keep (the previous version kept: How to go back); root (who can take
full control: two sudo programs, a root service running a script from a home folder, 3 rows marked known and 2 new);
root_before_move (Seraphina still runs as a person's account: the page waits, with the explain access paste);
keep_failed (Docker refuses every new name, so the keep pass names nothing and its record says it failed);
keep_partly (no keep pass yet, and Docker will refuse the names of the photos stack: Keep the current versions now
names only some); review_tailnet (the review list while the Tailscale network is not confirmed yet: That's mine).

Not states (POST /__mock/yourserver/<name>, the current state kept): app_event {app, stop?} stops the rig's container
stop (a key of tests/sfsystem_rig.IDS), reads the server again and publishes yourserver.app for app with its light,
as the area's findings do when a light changes; beszel_accepts {accepts} makes the fake Beszel accept the login's
password or refuse it.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from swfactory import plainhttp, yourserver as ys
from swfactory.config import Config
from swfactory.helpers import HelperClient

from . import store

AREA = "yourserver"
STATES = ("off", "absent", "unsafe", "review", "ready", "mixed", "stale", "docker_stopped", "outside_edit",
          "beszel_down", "beszel_not_recording", "beszel_no_login", "gpu_off", "hostile", "checks", "keep", "root",
          "root_before_move", "keep_failed", "review_tailnet", "keep_partly")
LATER_STATES = ("checks", "keep", "root", "root_before_move", "keep_failed", "keep_partly")
KEEP_STATES = ("checks", "keep", "keep_failed", "keep_partly")   # an old version to keep
REVIEW_STATES = ("review", "review_tailnet")   # nothing confirmed: the review list
ALL_CLASSES = ["keep", "private", "read", "refresh"]
SERAPHINA_ID = {"uid": 991, "user": "seraphina", "groups": ["seraphina"], "shell": "/usr/sbin/nologin", "uid_min": 1000}
OWNER_ID = {"uid": 1000, "user": "owner", "groups": ["owner"], "shell": "/bin/bash", "uid_min": 1000}
HOSTILE = ["<img src=x onerror=alert('app')>", "invoice‮gpj.exe", "x" * 600]
GPU_LINE = "0, GPU-00000000-1111-2222-3333-444444444444, Example GPU 24GB, 9300, 24576, 61, 37\n"


def _rig():
    from tests.sfsystem_rig import Rig

    return Rig()


def _stop(rig, key: str, *, exit_code: int, manually: bool, finished: str) -> None:
    """Stop one of the rig's containers: its state file says so and its cgroup folder is gone."""
    from tests.sfsystem_rig import IDS

    cid = IDS[key]
    path = rig.at(f"/var/lib/docker/containers/{cid}/config.v2.json")
    data = json.loads(path.read_text())
    data["State"].update(Running=False, Pid=0, ExitCode=exit_code, FinishedAt=finished)
    data["HasBeenManuallyStopped"] = manually
    path.write_text(json.dumps(data))
    for scope in rig.at("/sys/fs/cgroup").rglob(f"docker-{cid}.scope"):
        for child in scope.iterdir():
            child.unlink()
        scope.rmdir()


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class _Beszel:
    """A Beszel hub's answers: PocketBase's sign-in and the systems list."""

    def __init__(self, recording: bool = True) -> None:
        self.recording = recording
        self.accepts = True   # the login's password (beszel_accepts)

    def __call__(self, method, url, headers=None, body=None, timeout=5.0, **_):
        if url.endswith("/auth-with-password"):
            if not self.accepts:
                return plainhttp.Reply(400, {}, json.dumps({"message": "Failed to authenticate."}).encode())
            return plainhttp.Reply(200, {}, json.dumps({"token": "mock-token"}).encode())
        updated = _iso(time.time() - (60 if self.recording else 7200)).replace("T", " ")
        return plainhttp.Reply(200, {}, json.dumps({"items": [{"id": "abc123def456", "status": "up" if self.recording
                                                                else "down", "updated": updated}]}).encode())


def build(state: str) -> dict:
    """A fresh placeholder server and the real area reading it, in state."""
    rig = _rig()
    tmp = tempfile.mkdtemp(prefix="ys-mock-")
    raw = {"data_dir": tmp, "yourserver": {"enabled": state != "off", "tailnet": {"suffix": "example.ts.net"},
                                           "links": {"dockge": "http://192.0.2.10:5001",
                                                     "cockpit": "https://cockpit.example.ts.net",
                                                     "beszel": "https://beszel.example.ts.net"},
                                           "beszel": {"login_email": "seraphina@localhost",
                                                      "login_secret": "SERAPHINA_BESZEL_PASSWORD"}},
           "models": {"local": {"provider": "local", "model": "example-local"}},
           "providers": {"local": {"type": "openai", "base_url": "http://127.0.0.1:11434/v1"}},
           "services": {}, "harness": {"sandbox": "bwrap" if state in LATER_STATES else "none"}}
    if state in LATER_STATES:
        from tests.sfsystem_rig import SERAPHINA

        rig.config(allow=ALL_CLASSES if state != "root_before_move" else ["read"],
                   callers={str(SERAPHINA): ALL_CLASSES if state != "root_before_move" else ["read"]})
    if state in KEEP_STATES:
        rig.extra_images.append({"Id": "sha256:" + "c" * 64, "RepoTags": [], "RepoDigests": [
            "photoprism/photoprism@sha256:" + "d" * 64], "Created": 1759000000, "Size": 2_000_000_000, "Labels": {}})
        rig.compose_changed.add("/opt/stacks/photos")
        rig.moved_tags = {"jellyfin/jellyfin:10.9.11": "sha256:" + "e" * 64}
    if state == "root":
        _two_sudos(rig)
    if state == "keep_failed":   # Docker refuses every new name: the keep pass names nothing
        docker_answer = rig.docker_answer
        rig.docker_answer = lambda method, path: (500, b'{"message": "refused"}') if method == "POST" and \
            "/tag?" in path else docker_answer(method, path)
    if state == "keep_partly":   # the photos stack's names refused, every other one added
        docker_answer = rig.docker_answer
        rig.docker_answer = lambda method, path: (500, b'{"message": "refused"}') if method == "POST" and \
            "/tag?" in path and "seraphina-keep/photos-" in path else docker_answer(method, path)
    if state == "beszel_no_login":
        raw["yourserver"]["beszel"] = {"login_email": None, "login_secret": None}
    if state == "review_tailnet":
        raw["yourserver"]["tailnet"] = {}
    if state == "absent":
        raw["yourserver"]["helper"] = str(Path(tmp) / "not-installed" / "sf-system")
    config = Config(Path(tmp) / "config.json", raw)
    statuses: dict[tuple, int | None] = {}

    def http(host, port, path, host_header, timeout=5.0):
        status = statuses.get((host, port), 200)
        return {"status": status, "error": None if status is not None else "unreachable"}

    def tcp(host, port, timeout=5.0):
        status = statuses.get((host, port), 0)
        return {"status": status, "error": None if status is not None else "refused"}

    def gpu(argv):
        if state == "gpu_off":
            return 9, ""
        return (0, GPU_LINE) if "--query-gpu" in argv[1] else (0, "4101, 9000, GPU-00000000-1111-2222-3333-444444444444\n")

    roots = {name: str(rig.at(path)) for name, path in (
        ("proc", "/proc"), ("cgroup", "/sys/fs/cgroup"), ("hwmon", "/sys/class/hwmon"), ("run", "/run"),
        ("by_uuid", "/dev/disk/by-uuid"), ("fib", "/proc/net/fib_trie"), ("mounts", "/proc/self/mounts"))}
    rig.write("/proc/meminfo", "MemTotal:       65536000 kB\nMemFree:         1000000 kB\nMemAvailable:   "
                               "31000000 kB\nSwapTotal:       8000000 kB\nSwapFree:        6000000 kB\n")
    rig.write("/proc/stat", "cpu  1000 0 500 8000 100 0 0 0 0 0\n")
    rig.write("/proc/uptime", "32400.5 100000.0\n")
    rig.write("/proc/loadavg", "0.52 0.40 0.30 1/500 1234\n")
    rig.write("/sys/class/hwmon/hwmon0/name", "k10temp\n")
    rig.write("/sys/class/hwmon/hwmon0/temp1_input", "54000\n")
    rig.write("/sys/class/hwmon/hwmon1/name", "nvme\n")
    rig.write("/sys/class/hwmon/hwmon1/temp1_input", "41000\n")
    rig.write("/run/reboot-required", "*** System restart required ***\n")
    stamp = rig.write("/var/lib/apt/periodic/update-success-stamp", "")
    os.utime(stamp, (time.time() - 3 * 3600, time.time() - 3 * 3600))
    roots["apt_stamp"] = str(stamp)
    roots["auto_upgrades"] = str(rig.write("/etc/apt/apt.conf.d/20auto-upgrades", 'APT::Periodic::Unattended-Upgrade "1";\n'))
    roots["apt_cache"] = str(rig.at("/var/cache/apt/archives"))
    environ = {"SERAPHINA_BESZEL_PASSWORD": "mock-password"}
    beszel = _Beszel(recording=state != "beszel_not_recording")
    area = ys.YourServer(config, client=None if state == "absent" else HelperClient(
        "/usr/local/libexec/seraphina/sf-system", runner=rig.runner, name="Seraphina's system access"),
        roots=roots, gpu_run=gpu, prober=ys.Prober(http=http, tcp=tcp), systemctl=lambda argv: "ActiveState=active\n",
        beszel_request=beszel, environ=environ,
        asker=lambda model, system, prompt: "From the facts above: it is running and answering. Nothing changed in "
                                            "the last day. <b>This is text, not markup.</b>",
        run=rig.harness_run, background=False,
        identity=OWNER_ID if state == "root_before_move" else SERAPHINA_ID)

    def patch(change: dict) -> None:
        for key, value in change.items():
            ys._merge(config.raw.setdefault(key, {}), value)
    area.update_config = patch
    area.set_secret = lambda name, value: environ.__setitem__(name, value)
    if state == "unsafe":
        rig.lstat.spoiled[rig.paths.helper] = (1000, 0)
    if state == "docker_stopped":
        rig.docker_active = False
    if state == "mixed":
        _stop(rig, "ollama", exit_code=1, manually=False, finished=_iso(time.time() - 600))
        _stop(rig, "notes", exit_code=137, manually=True, finished=_iso(time.time() - 7200))
        _stop(rig, "gateway-dex", exit_code=2, manually=False, finished=_iso(time.time() - 900))
    if state == "beszel_down":
        _stop(rig, "beszel-agent", exit_code=137, manually=True, finished=_iso(time.time() - 3600))
        statuses[("172.21.0.5", 8090)] = None
    area.numbers(force=True)
    area.numbers(force=True)
    area.host(force=True)
    area.host(force=True)
    if state not in ("off", "absent"):
        area.refresh_map()
    if state not in ("off", "absent", "unsafe", *REVIEW_STATES):
        rows = [{"app": r["app"], "url": r["url"], "expect": r["expect"] or "run"} for r in area.review_view()["rows"]
                if r["url"]]
        rows += [{"app": r["app"], "expect": "off" if r["app"] == "a-dockge" and state == "mixed" else r["expect"] or "run"}
                 for r in area.review_view()["rows"] if not r["url"]]
        area.review_confirm(rows)
        if state == "ready":
            area.update_config({"services": {"a-media": {"probe": {"kind": "none"}}}})   # can't be checked (N8)
        if state == "mixed":
            statuses[("127.0.0.1", 8081)] = 503
            area.record.change("retire", "a-media", lambda r: r["apps"].setdefault("a-media", {}).setdefault(
                "old_copies", {}).update({k: "retired" for k in list(r["apps"]["a-media"].get("old_copies") or {})}))
        for _ in range(3):
            for probe in area.prober.state.values():
                probe["next_at"] = 0
            area.probe_all(time.time())
        area._beszel["next_at"] = 0
        area.beszel_check(time.time())
        if state == "beszel_down":
            area._beszel["next_at"] = 0
            area.beszel_check(time.time())
        for _ in range(3):
            area.findings(time.time())
    if state in ("checks", "keep", "keep_failed"):
        area.keep_now()
        area.refresh_map()
    if state == "checks":
        rig.write("/run/reboot-required.pkgs", "linux-image-6.18.0-14-generic\n")
        for check in ys.serverchecks.CHECK_IDS:
            area.run_check(check)
    if state == "root":
        _look(rig, area)
        view = area.root_view()
        rows = [r for g in view["groups"] for r in g["rows"] if r["way"]]
        for row in rows[:3]:
            area.root_mark(row["id"], {"known": True, "note": "mine" if row is rows[0] else None})
        with open(rig.at("/etc/group"), encoding="utf-8") as handle:
            groups = handle.read()
        rig.write("/etc/group", groups.replace("docker:x:998:owner,dockerpal", "docker:x:998:owner,dockerpal,kate"))
        rig.lstat.spoiled[str(rig.at("/opt/example/nightly.sh"))] = (1000, 0)
        _look(rig, area)
    if state == "stale":
        area.facts["at"] = time.time() - 1800
    if state == "outside_edit":
        path = area.folder() / "apps.json"
        doc = json.loads(path.read_text())
        doc["record"]["apps"]["a-photos"]["address"]["url"] = "https://photos-login.example.ts.net"
        path.write_text(json.dumps(doc))
        area.record.load()
    if state == "hostile":
        def plant(record: dict) -> None:
            apps = record["apps"]
            apps.setdefault("a-notes", {})["name"] = HOSTILE[0]
            apps.setdefault("a-filebrowser", {})["name"] = HOSTILE[1]
            apps.setdefault("a-mediaold", {})["name"] = HOSTILE[2]
            apps.setdefault("a-dockge", {})["address"] = {"url": "javascript:alert(1)", "source": "typed",
                                                         "fingerprint": None, "confirmed_at": _iso(time.time())}
            apps.setdefault("a-ai", {})["address"] = {"url": "https://ai.other.ts.net", "source": "typed",
                                                     "fingerprint": None, "confirmed_at": _iso(time.time())}
        area.record.change("correction", None, plant)
    return {"rig": rig, "area": area, "state": state, "raw": raw, "beszel": beszel}


def _two_sudos(rig) -> None:
    """sudo-rs as sudo, classic sudo beside it as sudo.ws, which honours one rule sudo-rs does not."""
    import os

    cargo = rig.paths.tool("sudo-rs")
    rig.write(cargo[len(str(rig.tmp)):], "#!/bin/false\n", 0o4755)
    os.unlink(rig.paths.sudo)
    os.symlink(cargo, rig.paths.sudo)
    rig.write("/usr/bin/sudo.ws", "#!/bin/false\n", 0o4755)
    rig.sudo_version = {"sudo-rs": "sudo-rs 0.2.13\n", "sudo.ws": "Sudo version 1.9.17p2\n"}
    rig.sudo_l = {"sudo-rs": {"owner": "sudo-l-owner.txt", "backupsvc": "sudo-l-backupsvc.txt",
                              "seraphina": "sudo-l-seraphina.txt"},
                  "sudo.ws": {"owner": "sudo-l-owner.txt", "backupsvc": "sudo-l-backupsvc.txt",
                              "seraphina": "sudo-l-seraphina.txt", "kate": "sudo-l-kate-restart.txt"}}
    rig.sudo_ll = {"sudo-rs": {"owner": "sudo-ll-owner.txt", "backupsvc": "sudo-ll-backupsvc.txt"}}


def _look(rig, area) -> None:
    """The root page's enumeration, run as its job unit would run it."""
    from tests.sfsystem_rig import sx

    area.root_check()
    job = next(j for j, v in area._jobs.items() if v["verb"] == "access" and not v.get("ended"))
    rig.write("/proc/self/cgroup", f"0::/system.slice/{sx.unit_name('access', job)}.service\n")
    sx.run_job(job, rig.paths, rig.system, rig.lstat, out=lambda _t: None)
    area._poll_jobs(force=True)


def _area(core):
    st = store(core, AREA)
    if "area" not in st:
        st.update(build(st.get("state") or "ready"))
    return st["area"]


def seed(core, fresh: bool) -> None:
    st = store(core, AREA)
    old = st.get("rig")
    if old is not None:
        old.cleanup()
    st.clear()
    st["state"] = "absent" if fresh else "ready"


def api(core, handler, method, parts, query, body):
    if parts[:1] != ["yourserver"] and not (parts == ["config"] and method == "POST" and isinstance(body, dict)
                                            and set(body) == {"yourserver"}):
        return NotImplemented
    area = _area(core)
    rest = parts[1:]
    try:
        result = _answer(core, area, method, parts, rest, body or {}, query or {})
    except ValueError as exc:
        raise core.Invalid(str(exc)) from None
    except ys.ActionError as exc:
        return handler.send_json({"error": str(exc), **exc.payload}, 409)
    if result is NotImplemented:
        return NotImplemented
    return handler.send_json(result)


def _answer(core, area, method, parts, rest, body, query):
    if parts == ["config"]:
        area.update_config(body)
        core.BUS.publish("yourserver.updated", {"helper": area.helper_state()["state"]})
        return {"saved": True}
    if method == "GET" and rest == []:
        return area.view()
    if method == "POST" and rest == ["refresh"]:
        area.numbers(force=True)
        if area.settings().get("enabled") and area.helper_present():
            area.refresh_map()
        return area.view()
    if method == "GET" and rest == ["apps"]:
        return area.apps_view()
    if len(rest) >= 2 and rest[0] == "apps":
        app = rest[1]
        if len(rest) == 2:
            return area.app_view(app) if method == "GET" else area.app_update(app, body)
        if rest[2:] == ["address"] and method == "POST":
            return area.address(app, body)
        if rest[2:] == ["qr"] and method == "GET":
            return area.qr(app)
        if rest[2:] == ["ask"] and method == "POST":
            return area.ask(app, body.get("question"))
        if rest[2:] == ["logs"] and method == "GET":
            return area.logs(app, query.get("member") or None, query.get("lines") or None, query.get("before") or None)
        raise KeyError(rest[2] if len(rest) > 2 else app)
    if rest == ["review"]:
        return area.review_view() if method == "GET" else area.review_confirm(body.get("rows"), body.get("tailnet"))
    if rest == ["groups"] and method == "POST":
        return area.groups(body)
    if rest == ["beszel"] and method == "POST":
        return area.beszel_login(body)
    if rest == ["setup"] and method == "GET":
        return area.setup()
    if rest == ["setup", "stage"] and method == "POST":
        return area.stage(user="seraphina", classes=body.get("classes"))
    if rest == ["setup", "check"] and method == "POST":
        return area.check()
    if rest == ["checks"] and method == "GET":
        return area.checks_view()
    if len(rest) == 2 and rest[0] == "checks" and method == "POST":
        return area.run_check(rest[1], body)
    if rest == ["keep"] and method == "POST":
        return area.keep_now()
    if rest == ["jobs"] and method == "GET":
        return area.jobs_view()
    if len(rest) == 2 and rest[0] == "jobs" and method == "GET":
        return area.job_view(rest[1])
    if rest == ["receipts"] and method == "GET":
        return area.receipts_view(query.get("cursor") or None)
    if len(rest) == 2 and rest[0] == "receipts" and method == "GET":
        return area.receipt_view(rest[1])
    if rest == ["root"] and method == "GET":
        return area.root_view()
    if rest == ["root", "check"] and method == "POST":
        view = area.root_check()
        rig = store(core, AREA)["rig"]
        job = next((j for j, v in area._jobs.items() if v["verb"] == "access" and not v.get("ended")), None)
        if job:   # the job unit, run at once in the mock
            from tests.sfsystem_rig import sx

            rig.write("/proc/self/cgroup", f"0::/system.slice/{sx.unit_name('access', job)}.service\n")
            sx.run_job(job, rig.paths, rig.system, rig.lstat, out=lambda _t: None)
            area._poll_jobs(force=True)
            view = area.root_view()
        return view
    if len(rest) == 3 and rest[0] == "root" and rest[2] == "mark" and method == "POST":
        return area.root_mark(rest[1], body)
    return NotImplemented


def mock(core, handler, method, name, body):
    if method == "POST" and name in STATES:
        st = store(core, AREA)
        old = st.get("rig")
        if old is not None:
            old.cleanup()
        st.clear()
        st["state"] = name
        st.update(build(name))
        core.BUS.publish("yourserver.updated", {"helper": st["area"].helper_state()["state"]})
        return handler.send_json(st["area"].view())
    if method == "POST" and name == "app_event":
        area = _area(core)
        st = store(core, AREA)
        body = body or {}
        if body.get("stop"):
            _stop(st["rig"], str(body["stop"]), exit_code=1, manually=False, finished=_iso(time.time() - 60))
            area.numbers(force=True)
            area.refresh_map()
        card = next(c for c in area.apps_view()["apps"] if c["id"] == body.get("app"))
        light = card["light"]
        core.BUS.publish("yourserver.app", {"app": card["id"], "tone": light["tone"], "code": light["code"]})
        return handler.send_json({"app": card["id"], "light": light})
    if method == "POST" and name == "beszel_accepts":
        _area(core)
        store(core, AREA)["beszel"].accepts = bool((body or {}).get("accepts"))
        return handler.send_json({"accepts": store(core, AREA)["beszel"].accepts})
    return NotImplemented


def contract(test) -> None:
    """GET /api/yourserver has YourServer.view()'s keys in every state; each card has the module's tones and
    reasons; the review rows, one app's page and the setup page have the shapes the view module reads."""
    reasons = set(ys.REASONS) | {"stale"}
    view_keys = None
    for state in STATES:
        status, _, raw = test.request("POST", f"/__mock/yourserver/{state}", {})
        test.assertEqual(status, 200, state)
        view = json.loads(raw)
        view_keys = view_keys or set(view)
        test.assertEqual(set(view), view_keys, state)
        test.assertEqual(view["looks_only"], ys.LOOKS_ONLY)
        status, _, raw = test.request("GET", "/api/yourserver/apps")
        apps = json.loads(raw)
        for card in apps.get("apps") or []:
            test.assertIn(card["light"]["tone"], ys.TONES, (state, card["id"]))
            test.assertIn(card["light"]["code"], reasons, (state, card["id"]))
            test.assertTrue(ys.APP_ID_RE.fullmatch(card["id"]))
            url = card["address"].get("url")
            if url:
                test.assertIsNone(ys.url_problem(url, "example.ts.net", ["192.0.2.10", "beszel.home.example",
                                                                         "files.home.example"]), (state, url))
    test.request("POST", "/__mock/yourserver/review", {})
    status, _, raw = test.request("GET", "/api/yourserver/review")
    review = json.loads(raw)
    test.assertEqual(set(review), {"rows", "words", "tailnet", "done_at"})
    test.assertTrue(any(r["ticked"] for r in review["rows"]) and not all(r["ticked"] for r in review["rows"]))
    test.request("POST", "/__mock/yourserver/ready", {})
    status, _, raw = test.request("GET", "/api/yourserver/apps/a-photos")
    page = json.loads(raw)
    test.assertTrue({"app", "parts", "candidates", "changes", "facts", "home_steps", "qr_words"} <= set(page))
    status, _, raw = test.request("GET", "/api/yourserver/apps/a-photos/qr")
    test.assertEqual(set(json.loads(raw)), {"size", "rows", "version"})
    status, _, raw = test.request("GET", "/api/yourserver/setup")
    test.assertEqual(set(json.loads(raw)), {"blocks", "details", "removal", "staged", "setup_dir", "release_sha256",
                                            "looks_only", "helper", "classes", "staged_allow"})
    test.request("POST", "/__mock/yourserver/checks", {})
    status, _, raw = test.request("GET", "/api/yourserver/checks")
    checks = json.loads(raw)
    test.assertEqual([c["id"] for c in checks["checks"]], list(ys.serverchecks.CHECK_IDS))
    test.assertTrue(all(c["state"] == "done" for c in checks["checks"]))
    status, _, raw = test.request("GET", "/api/yourserver/apps/a-photos/logs")
    logs = json.loads(raw)
    test.assertTrue(logs["available"] and logs["lines"])
    test.assertNotIn("PLANTED", raw.decode() if isinstance(raw, bytes) else raw)
    test.request("POST", "/__mock/yourserver/root", {})
    status, _, raw = test.request("GET", "/api/yourserver/root")
    root = json.loads(raw)
    test.assertTrue(root["available"])
    test.assertEqual(root["new_ways"], 2)
    test.assertEqual(sum(1 for g in root["groups"] for r in g["rows"] if r["known"]), 3)
    test.assertIn(rootaccess_sudo_rs_line(), root["cannot_check"])
    test.request("POST", "/__mock/yourserver/root_before_move", {})
    status, _, raw = test.request("GET", "/api/yourserver/root")
    test.assertFalse(json.loads(raw)["available"])
    status, _, _ = test.request("GET", "/api/yourserver/apps/a-nothing")
    test.assertEqual(status, 404)


def rootaccess_sudo_rs_line() -> str:
    from tests.sfsystem_rig import sx

    return sx.CANNOT_SUDO_RS
