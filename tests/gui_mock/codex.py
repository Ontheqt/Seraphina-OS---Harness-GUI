"""Mock backend for the Codex worker: DESIGN §44 and docs/seraphina/BROWSER_AND_CODEX.md §3.3, §3.6, §3.7, §4.4 (phase
BC1b). See tests/gui_mock/__init__.py for the hooks.

The answers are swfactory/codex's own: the seed writes real run receipts with work.Receipts into a temporary data
directory (hash-chained), and GET …/receipts is Receipts.summaries over them; GET /api/codex carries the setup
blocks of swfactory/codex/setup.py. Two jobs whose builder runs on codex-worker: my-app/051-fix-the-greeting needs
you (the approver escalated) after two Codex runs (round 1: verify failed; round 2: committed, verify ok), with a
preview, so "Verify in the browser" is offered; my-app/052-rename-the-greeting failed when its run hit the wall clock:
timed out, reconciled, nothing committed. A path in a receipt carries hostile text.

Routes: GET /api/codex, GET /api/jobs/<repo>/<job>/receipts, GET …/receipts/<run>, POST …/verify-in-browser (a
browsing chat whose chat.json carries the job's verify record).

States (POST /__mock/codex/<state>): on (the two jobs and their receipts), off (codex.enabled false). The seed is on
without the two jobs (the rail entry and GET /api/codex), so the other areas' job lists stay as they were; the Codex
suite asks for "on" first.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from swfactory.codex import setup as codex_setup
from swfactory.codex import work
from swfactory.config import Config

from . import store

AREA = "codex"
STATES = ("on", "off")
REPO = "my-app"
FIXED = "051-fix-the-greeting"
TIMED = "052-rename-the-greeting"
HOSTILE = "templates/<img src=x onerror=alert('cx')>.html"
VERIFY_CHAT = "c-c0de0e0001"


def _receipt(job_id: str, run: str, outcome: str, *, commit: str | None, verify: str | None, paths: list[str],
             reconciled: dict | None = None, result: str = "") -> dict:
    checks = [{"kind": "write_policy", "ok": True}]
    if commit:
        checks.append({"kind": "secret_scan", "ok": True})
    if verify:
        checks.append({"kind": "verify", "argv": ["bash", "-c", "python3 -m unittest -q"], "exit": 0 if verify == "ok" else 1,
                       "seconds": 0.4, "output_sha256": "5" * 64, "commit": commit, "check": verify,
                       "tail": "Ran 1 test in 0.000s\n\n" + ("OK" if verify == "ok" else "FAILED (failures=1)")})
    job = {"repo": REPO, "id": job_id}

    class _Provider:
        name = "strata"
        base_url = "http://127.0.0.1:18080/v1"
    return work.run_record(
        job=job, run=run, agent="builder", attempt=1, grid={"sha256": "9" * 64, "bytes": 1801}, version="0.163.0",
        thread_id="0199c000-0000-7000-8000-00000000000c", model="strata", provider=_Provider(),
        home={"template_sha256": work.L.TEMPLATE_PIN, "config_sha256": "3" * 64, "verified": True,
              "rollout_sha256": "4" * 64},
        started="2026-10-26T09:12:04Z", ended="2026-10-26T09:14:31Z", outcome=outcome, error=None,
        artifacts={"paths": paths, "diff_sha256": "6" * 64 if paths else None, "final_message_sha256": "7" * 64},
        checks=checks, commit_=({"by": "harness", "sha": commit, "paths": paths,
                                 "response": {"status": 0, "ids": [commit[:7]], "body_sha256": "8" * 64}} if commit else None),
        reconciled=reconciled, result=result or (f"committed {commit[:12]}" if commit else "nothing to commit"),
        exit_code=0 if outcome == "completed" else 1)


def _runs(core, job: dict, outcomes: list[str]) -> list[str]:
    """agent.start (with Codex's own labels) and agent.end (with the outcome) for each builder run, as the factory
    emits them; the runs' names."""
    names = []
    for outcome in outcomes:
        run = core.log_name(job, "builder")
        names.append(run)
        r = core.Run("builder", run, job=job, round_=1, instant=True)
        r.emit("agent.start", {"agent": "builder", "driver": "codex-worker", "model": "local", "model_id": "strata",
                               "provider": "codex-worker", "resume": False, "round": 1,
                               "codex": {"version": "0.163.0", "surface": "exec", "permissions_profile": "factory_worker",
                                         "approval_policy": "never"}})
        r.text("Fixing the greeting in app.py.")
        r.emit("agent.end", {"ok": outcome == "completed", "error": None if outcome == "completed" else "timed out after 5400s",
                             "usage": {"input_tokens": 12000, "output_tokens": 900, "cached_tokens": 0, "calls": 1},
                             "turns": 1, "seconds": 140, "outcome": outcome})
    return names


def build(core, state: str, jobs: bool = True) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="codex-mock-"))
    config = Config(tmp / "config.json", {"data_dir": str(tmp / "data"), "codex": {"enabled": state == "on",
                                                                                  "binary": "/usr/local/lib/seraphina-codex/codex"}})
    receipts = work.Receipts(config.data_dir)
    out: dict = {"config": config, "jobs": {}}
    if state == "off" or not jobs:
        return out
    fixed = core.new_job(REPO, "Fix the greeting so it shows the submitted name", created_ago=7200, id=FIXED)
    timed = core.new_job(REPO, "Rename the greeting's heading", created_ago=3600, id=TIMED)
    for job in (fixed, timed):
        job["_seed"] = True   # the scenario's trim never drops a seeded job
        job["routing"]["builder"] = {**job["routing"]["builder"], "driver": "codex-worker", "model": "local",
                                     "model_id": "strata", "provider": "strata"}
    fixed_runs = _runs(core, fixed, ["completed", "completed"])
    [timed_run] = _runs(core, timed, ["timed_out"])
    first = _receipt(FIXED, fixed_runs[0], "completed", commit="a1b2c3d4e5f60718293a4b5c6d7e8f9012345678", verify="failed",
                     paths=["app.py"])
    second = _receipt(FIXED, fixed_runs[1], "completed", commit="b2c3d4e5f60718293a4b5c6d7e8f901234567890", verify="ok",
                      paths=["app.py", HOSTILE])
    rels = [receipts.write(REPO, FIXED, fixed_runs[0], first), receipts.write(REPO, FIXED, fixed_runs[1], second)]
    reconciled = {"status": [" M app.py", "?? notes.txt"], "paths": ["app.py", "notes.txt"], "diff_sha256": "c" * 64,
                  "clean": False}
    rels_timed = [receipts.write(REPO, TIMED, timed_run, _receipt(
        TIMED, timed_run, "timed_out", commit=None, verify=None, paths=["app.py", "notes.txt"], reconciled=reconciled,
        result="run timed_out: reconciled, nothing committed, never replayed"))]
    fixed.update(stage="needs-human", round=2, decision="ESCALATE", reason="approver escalated: a person should look at the page",
                 runs=[{"run": run, "agent": "builder", "driver": "codex-worker", "outcome": "completed", "receipt": rel,
                        "sha256": digest} for run, (rel, digest) in zip(fixed_runs, rels)],
                 verify={"commit": second["commit"]["sha"], "check": "ok", "exit": 0, "seconds": 0.4, "run": fixed_runs[1]})
    timed.update(stage="failed", failed_step="build", reason="builder failed (outcome timed_out): timed out after 5400s",
                 runs=[{"run": timed_run, "agent": "builder", "driver": "codex-worker", "outcome": "timed_out",
                        "receipt": rels_timed[0][0], "sha256": rels_timed[0][1]}],
                 reconcile={"run": timed_run, "outcome": "timed_out",
                            "summary": work.retry_summary(reconciled), "paths": reconciled["paths"],
                            "diff_sha256": reconciled["diff_sha256"], "at": core.ago(3000)})
    for job in (fixed, timed):
        core.save_job(job)
    out["jobs"] = {FIXED: {"preview": {"offered": True, "persona": "kate-browser", "port": 18938}},
                   TIMED: {"preview": {"offered": False, "why": "offered while the job is in review, approved or needs you"}}}
    return out


def seed(core, fresh: bool) -> None:
    st = store(core, AREA)
    st.clear()
    st.update(build(core, "off" if fresh else "on", jobs=False))


def api(core, handler, method, parts, query, body):
    st = store(core, AREA)
    if not st:
        seed(core, False)
    config = st["config"]
    if method == "GET" and parts == ["codex"]:
        merged = work.settings(config)
        return handler.send_json({"enabled": merged["enabled"], "pin": merged["pin"], "user": merged["user"],
                                  "via": merged["via"], "group": merged["group"], "binary": merged["binary"],
                                  "launcher": str(work.launcher_path(config)), "work_root": "<data_dir>/codex",
                                  "checks": {"http://loopback:18080": {"ok": True, "status": 200, "body_sha256": "1" * 64,
                                                                      "at": core.ago(86400)}} if merged["enabled"] else {},
                                  "setup": codex_setup.blocks(config), "risk": codex_setup.RESIDUAL_RISK})
    if len(parts) >= 4 and parts[0] == "jobs" and parts[3] in ("receipts", "verify-in-browser"):
        repo, job_id = parts[1], parts[2]
        info = st["jobs"].get(job_id) if repo == REPO else None
        with core.LOCK:
            job = core.STATE["jobs"].get((repo, job_id))
            job = json.loads(json.dumps(core.public_job(job))) if job else None
        if job is None:
            return handler.error(404, "no such job")
        receipts = work.Receipts(config.data_dir)
        if method == "GET" and len(parts) == 4:
            return handler.send_json({"receipts": receipts.summaries(repo, job_id) if info else [],
                                      "chain_broken": receipts.verify_chain(repo, job_id) if info else [],
                                      "reconcile": job.get("reconcile"), "verify": job.get("verify"),
                                      "preview": (info or {}).get("preview") or {"offered": False,
                                                                                 "why": "the repository has no preview setting"}})
        if method == "GET" and len(parts) == 5:
            try:
                return handler.send_json({"receipt": receipts.read(repo, job_id, parts[4])})
            except KeyError:
                return handler.error(404, "no such receipt")
        if method == "POST" and len(parts) == 4 and parts[3] == "verify-in-browser":
            if not ((info or {}).get("preview") or {}).get("offered"):
                return handler.error(409, "Verify in the browser is not offered: the repository has no preview setting")
            chat = {"id": VERIFY_CHAT, "title": f"Verify {repo}/{job_id} in the browser", "repo": None, "repo_path": None,
                    "agent": "kate-browser", "model": "kate-browse", "driver": "builtin", "extra_body": {}, "reason": None,
                    "profile": None, "mode": "none", "base": None, "branch": None, "worktree": None,
                    "cwd": f"/srv/ai-work/factory/chats/{VERIFY_CHAT}/work", "status": "idle", "error": None, "session": None,
                    "session_model": None, "turns": 0, "models": [],
                    "usage": {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "calls": 0},
                    "cost": {"usd": 0.0, "priced": True, "unpriced_runs": 0}, "job": None, "created_at": core.now(),
                    "updated_at": core.now(), "browser": [],
                    "verify": {"repo": repo, "job": job_id, "branch": job.get("branch"),
                               "preview": {"command": ["python3", "app.py"], "port": 18938, "startup_seconds": 20}},
                    "persona": {"type": "openwebui", "persona": "kate-browser", "label": "Kate-browser through Open WebUI",
                                "model": "kate-browse", "warnings": [], "turns": []}}
            with core.LOCK:
                core.STATE["chats"][VERIFY_CHAT] = chat
                core.STATE["messages"][VERIFY_CHAT] = []
            core.BUS.publish("chat.updated", {"chat": json.loads(json.dumps(chat))})
            return handler.send_json({"chat": chat}, status=201)
    return NotImplemented


def mock(core, handler, method, name, body):
    if method == "POST" and name in STATES:
        st = store(core, AREA)
        st.clear()
        st.update(build(core, name))
        return handler.send_json({"state": name})
    return NotImplemented


def contract(test) -> None:
    """The run card reads Receipts.summaries' keys, and the routes answer in swfactory/server.py's shapes."""
    test.assertEqual(test.request("POST", "/__mock/codex/on", {})[0], 200)
    status, _, raw = test.request("GET", f"/api/jobs/{REPO}/{FIXED}/receipts")
    test.assertEqual(status, 200)
    data = json.loads(raw)
    test.assertEqual(set(data), {"receipts", "chain_broken", "reconcile", "verify", "preview"})
    [first, second] = data["receipts"]
    test.assertEqual(set(first), set(work.summary("01-builder", {}, "0" * 64)))
    test.assertEqual(data["chain_broken"], [])
    test.assertEqual(second["previous_sha256"], first["sha256"])
    status, _, raw = test.request("GET", f"/api/jobs/{REPO}/{FIXED}/receipts/{second['run']}")
    test.assertEqual((status, json.loads(raw)["receipt"]["schema"]), (200, "swf.receipt.run/1"))
    status, _, raw = test.request("GET", "/api/codex")
    test.assertEqual(status, 200)
    test.assertEqual([b["id"] for b in json.loads(raw)["setup"]], [b["id"] for b in codex_setup.blocks(Config("/x"))])
