"""Mock backend for the Browser area: DESIGN §43 and docs/seraphina/BROWSER_AND_CODEX.md §2.6, §2.13, §4.3 (phase
BC1a). See tests/gui_mock/__init__.py for the hooks.

The answers are swfactory/browser's own: the seed writes real grants, session locks, action logs and a screenshot
into a temporary data directory with swfactory/browser/session_locks.py, and GET /api/browser and the chat's rows are
runs.card, runs.recent_sessions and runs.chat_rows over them. A browsing chat (c-b10e5e0001, Kate's browsing persona)
holds two finished turns: the first opened the fixture, filled the name, sent the form (expect matched) and saved a
screenshot; the second clicked a link that posted (reclassified, the origin on hold) and a slow order whose outcome is
unknown. A target's name carries hostile text (markup, a right-to-left override).

Routes: GET /api/browser, POST /api/browser/grants/<grant>/revoke (the active grant: browser.revoked, the chat idle),
GET /api/chats/<chat>/browser/<session lock>/<name>.png, and GET /api/chats/<chat> for the browsing chat.

States (POST /__mock/browser/<state>): on (the seed), off (browser.enabled false, no sessions), running (the chat's
third turn holds the session lock: Revoke shows), off_unknown (the seed's sessions, the unknown order among them,
after the owner switched the browser off: Needs you still lists it).
"""

from __future__ import annotations

import json
import struct
import tempfile
import zlib
from pathlib import Path

from swfactory.browser import runs
from swfactory.browser import session_locks as SL
from swfactory.browser.fixture import FIXTURE_ORIGIN
from swfactory.config import Config

from . import store

AREA = "browser"
STATES = ("on", "off", "running", "off_unknown")
CHAT = "c-b10e5e0001"
HOSTILE = "<img src=x onerror=alert('br')> Submit‮gpj"


def png(width: int = 320, height: int = 180) -> bytes:
    """A small real PNG (a page-like picture: a header bar and lines) made with zlib."""
    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            if y < 28:
                pixel = (52, 92, 160)
            elif 50 <= y <= 58 and 20 <= x <= 200 or 74 <= y <= 82 and 20 <= x <= 260 or 110 <= y <= 130 and 20 <= x <= 90:
                pixel = (120, 120, 130)
            else:
                pixel = (246, 247, 250)
            row += bytes(pixel)
        rows.append(bytes(row))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b"")


def _session(data: Path, run: str, write) -> tuple[str, str]:
    grant_id, path = SL.write_grant(data, locked_by={"kind": "chat", "ref": f"chat:{CHAT}", "run": run, "agent": "kate-browser"},
                                    profile="fixture", origins={"read": [FIXTURE_ORIGIN], "write": [FIXTURE_ORIGIN]},
                                    fixture_origins=[FIXTURE_ORIGIN], paths={})
    lock = SL.take_session_lock(data, SL.read_grant(path))
    log = SL.ActionLog(lock.folder / SL.ACTION_LOG, lock.lock_number, lock.number_file)
    write(log, lock.folder)
    lock.close()
    SL.update_grant(path, ended=SL.utc_now())
    return grant_id, lock.session_lock


def _first_turn(log, folder: Path) -> None:
    def name(text: str) -> str:
        return f'<factory-data source="browser-page" taint="untrusted">\n{text}\n</factory-data>'
    log.intent("A-1", **{"class": "navigate", "tool": "navigate", "target": None, "origin": "about:blank"})
    log.outcome("A-1", "ok", **{"class": "navigate", "reclassified": False,
                                "response": {"tool_status": "ok", "requests": [{"method": "GET", "status": "200"}]}})
    log.intent("A-2", **{"class": "input", "tool": "fill", "origin": FIXTURE_ORIGIN, "values_hmac": "ab" * 32,
                         "target": {"ref": "e7", "role": "textbox", "name_hmac": "cd" * 32, "name_block": name("Name")}})
    log.outcome("A-2", "ok", **{"class": "input", "reclassified": False, "response": {"tool_status": "ok", "requests": []}})
    log.write({"kind": "refusal", "tool": "click", "code": "stale_ref"})
    log.intent("A-3", **{"class": "commit", "tool": "click", "origin": FIXTURE_ORIGIN,
                         "target": {"ref": "e8", "role": "button", "name_hmac": "ef" * 32, "name_block": name(HOSTILE)}})
    log.outcome("A-3", "ok", **{"class": "commit", "reclassified": False,
                                "response": {"tool_status": "ok", "requests": [{"method": "POST", "status": "200"}]},
                                "expect": {"scope": "e9", "matched": True,
                                           "check": "text in the target's accessible subtree, not hidden by CSS"}})
    image = png()
    (folder / "artifacts" / "greeting.png").write_bytes(image)
    import hashlib

    log.write({"kind": "artifact", "artifact": "greeting.png", "sha256": hashlib.sha256(image).hexdigest(),
               "bytes": len(image), "observation": "O-6", "origin": FIXTURE_ORIGIN})


def _second_turn(log, folder: Path) -> None:
    log.intent("A-1", **{"class": "navigate", "tool": "click", "origin": FIXTURE_ORIGIN,
                         "target": {"ref": "e5", "role": "link", "name_hmac": "12" * 32,
                                    "name_block": '<factory-data source="browser-page" taint="untrusted">\nRead more\n</factory-data>'}})
    log.outcome("A-1", "ok", **{"class": "commit", "reclassified": True,
                                "response": {"tool_status": "ok", "requests": [{"method": "POST", "status": "200"}]}})
    log.intent("A-2", **{"class": "commit", "tool": "click", "origin": FIXTURE_ORIGIN,
                         "target": {"ref": "e4", "role": "button", "name_hmac": "34" * 32,
                                    "name_block": '<factory-data source="browser-page" taint="untrusted">\nPlace order\n</factory-data>'}})
    log.outcome("A-2", "unknown", **{"class": "commit", "reclassified": False,
                                     "response": {"tool_status": "timeout_sent", "requests": [{"method": "POST", "status": "200"}]}})
    log.write({"kind": "refusal", "tool": "click", "code": "commit_hold"})


def build(core, state: str) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="browser-mock-"))
    runtime = tmp / "runtime"
    config = Config(tmp / "config.json", {"data_dir": str(tmp / "data"), "browser": {
        "enabled": state not in ("off", "off_unknown"), "runtime": {"dir": str(runtime)}}})
    records = []
    if state != "off":
        for turn, (run, write) in enumerate((("01-turn", _first_turn), ("02-turn", _second_turn)), start=1):
            grant_id, session_lock = _session(config.data_dir, run, write)
            record = {"grant": grant_id, "session_lock": session_lock, "run": run, "turn_run": run, "turn": turn,
                      "started": True, "profile": "fixture", "label": runs.UNTRUSTED_LABEL}
            folder = SL.browser_root(config.data_dir) / "sessions" / session_lock
            actions = SL.ActionLog.actions(folder / SL.ACTION_LOG)
            record.update(actions=len(actions), commits=sum(1 for a in actions if a.get("class") == "commit"),
                          unknown=sum(1 for a in actions if a.get("outcome") == "unknown"))
            record["rows"] = runs.chat_rows(config.data_dir, record)
            records.append(record)
    chat = {"id": CHAT, "title": "Open the test page and greet as Kate", "repo": None, "repo_path": None,
            "agent": "kate-browser", "model": "kate-browse", "driver": "builtin", "extra_body": {}, "reason": None,
            "profile": None, "mode": "none", "base": None, "branch": None, "worktree": None,
            "cwd": f"/srv/ai-work/factory/chats/{CHAT}/work", "status": "running" if state == "running" else "idle",
            "error": None, "session": None, "session_model": None, "turns": 2, "models": [],
            "usage": {"input_tokens": 9100, "output_tokens": 620, "cached_tokens": 0, "calls": 9},
            "cost": {"usd": 0.0, "priced": True, "unpriced_runs": 0}, "job": None,
            "created_at": core.ago(9000), "updated_at": core.now(), "browser": records,   # newer than any replayed event
            "persona": {"type": "openwebui", "persona": "kate-browser", "label": "Kate-browser through Open WebUI",
                        "model": "kate-browse", "warnings": [], "turns": []}}
    if state == "running":
        chat["browser_active"] = {"grant": "G-20261026-0099", "profile": "fixture", "since": core.ago(20)}
    messages = [{"role": "user", "text": "Open the test page, greet as Kate and take a screenshot.", "turn": 1},
                {"role": "assistant", "text": "The page now says Hello, Kate. I saved a screenshot for you.", "tools": [], "turn": 1},
                {"role": "user", "text": "Read more, then place the order.", "turn": 2},
                {"role": "assistant", "text": "The order may have gone through: the shop took too long to answer. I will not try again; "
                                              "please check it. <script>alert('br')</script>", "tools": [], "turn": 2}]
    if state == "running":
        messages.append({"role": "user", "text": "Look at the shop page again.", "turn": 3})
    if state != "off":
        with core.LOCK:
            core.STATE["chats"][CHAT] = chat
            core.STATE["messages"][CHAT] = messages
    else:
        with core.LOCK:
            core.STATE["chats"].pop(CHAT, None)
    return {"config": config, "state": state}


def seed(core, fresh: bool) -> None:
    built = build(core, "off" if fresh else "on")   # a fresh install has no chat and the browser off
    st = store(core, AREA)
    st.clear()
    st.update(built)


def api(core, handler, method, parts, query, body):
    st = store(core, AREA)
    if not st:
        seed(core, False)
    config = st["config"]
    if method == "GET" and parts == ["browser"]:
        return handler.send_json({**runs.card(config), "sessions": runs.recent_sessions(config.data_dir)})
    if method == "POST" and len(parts) == 4 and parts[:2] == ["browser", "grants"] and parts[3] == "revoke":
        with core.LOCK:
            chat = core.STATE["chats"].get(CHAT)
            active = (chat or {}).get("browser_active") or {}
            if active.get("grant") != parts[2]:
                return handler.error(404, "no such grant")
            chat.pop("browser_active", None)
            chat.update(status="idle", updated_at=core.now())
            snapshot = json.loads(json.dumps(chat))
        core.BUS.publish("browser.revoked", {"grant": parts[2], "closed": True})
        core.BUS.publish("chat.updated", {"chat": snapshot})
        return handler.send_json({"grant": parts[2], "revoked": {"at": core.now(), "by": "person"}, "closed": True})
    if method == "GET" and len(parts) == 5 and parts[0] == "chats" and parts[2] == "browser":
        try:
            path = runs.artifact_path(config.data_dir, parts[3], parts[4])
        except KeyError:
            return handler.error(404, "no such screenshot")
        data = path.read_bytes()
        handler.send_response(200)
        handler.send_header("Content-Type", "image/png")
        handler.send_header("Content-Length", str(len(data)))
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        handler.wfile.write(data)
        return True
    return NotImplemented


def mock(core, handler, method, name, body):
    if method == "POST" and name in STATES:
        built = build(core, name)   # built first, then swapped: a page loading meanwhile still finds its files
        st = store(core, AREA)
        st.clear()
        st.update(built)
        return handler.send_json({"state": name})
    return NotImplemented


def contract(test) -> None:
    """GET /api/browser has runs.card's keys plus sessions in every state; the chat's rows are runs.chat_rows'."""
    keys = set(runs.card(Config("/nonexistent/config.json", {}))) | {"sessions"}
    for state in STATES:
        status, _, raw = test.request("POST", f"/__mock/browser/{state}", {})
        test.assertEqual(status, 200, state)
        status, _, raw = test.request("GET", "/api/browser")
        test.assertEqual(status, 200, state)
        view = json.loads(raw)
        test.assertEqual(set(view), keys, state)
        test.assertEqual(bool(view["enabled"]), state not in ("off", "off_unknown"))
        if state == "off":
            continue
        test.assertTrue(any(session["unknown"] for session in view["sessions"]), state)
        status, _, raw = test.request("GET", f"/api/chats/{CHAT}")
        chat = json.loads(raw)["chat"]
        rows = [row for record in chat["browser"] for row in record["rows"]]
        test.assertTrue({"action", "screenshot", "refusal"} <= {row["kind"] for row in rows})
        shot = next(row for row in rows if row["kind"] == "screenshot")
        status, ctype, raw = test.request("GET", f"/api/chats/{CHAT}/browser/{chat['browser'][0]['session_lock']}/{shot['artifact']}")
        test.assertEqual((status, ctype.split(";")[0]), (200, "image/png"))
        test.assertTrue(raw.startswith(b"\x89PNG"))
    test.request("POST", "/__mock/browser/on", {})
