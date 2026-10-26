"""Mock backend for the Messages & Wakes area: DESIGN §34 "GUI interface notes (W1)" and §35 "GUI interface notes
(W2, W2b, W3)". See tests/gui_mock/__init__.py for the hooks.

It answers, with canned data shaped like swfactory/notify.py (Notifier), wakes.py (WakeManager), rules.py and
personas.py (PersonaStore), and with their refusals word for word:
  * the inbox: GET /api/messages (each delivery state, a text_removed row, a summary and the notices it carried, answer
    rows), GET /api/messages/<M-id> (two deliveries, one a heads-up with its why, and what came back), POST
    /api/messages/<M-id>/<ack|snooze|retry|resend>;
  * inbound: GET /api/notify/inbound (each kind, a late command with pending_verb, forged, foreign and stranger rows),
    POST /api/notify/inbound/<I-id>/apply (then its 400 "it was applied already");
  * GET /api/notify (with paused, tamper, precondition and notify.enabled variants through /__mock/messages/state),
    /api/wakes/control, /api/wakes/review/<accept|restart>, /api/wakes/acknowledge (the wrong-sentence 400);
  * phone setup: channels (an ntfy one on the tailnet and a paired Telegram one), test, prepare (three copy blocks and
    a passphrase), check, pair for both with the status sequence waiting → confirm → paired, confirm with wrong and
    expired after three, unpair, rotate | leaked | disable, lost phone;
  * terms get, save and none (a 400 without the sandbox); hooks list, add, rotate and delete (the token once); the
    owner's events route; GET/POST /api/notify/rules in both sources and the 400 for a weaker boundary;
  * POST /api/config for the notify and wakes sections (core.CONFIG_SECTIONS gains them): the settings checks of
    settings._check_notify / _check_wake_personas and Factory._check_wake_patch / Notifier.check_patch that the GUI
    shows (a privacy key, clean_only on Telegram, topics that repeat, no time zone, the S46 refusals, the rulebook's
    keys once the file exists, gh in wake.commands);
  * the Schedule: GET /api/wakes (a queue in each state, a breaker, a paused by tamper-check, servers with and without
    the attestation and one detected as Open WebUI), Run now, cancel, reminders, attest;
  * the wake personas' pages (seraphina, mira): GET /api/personas/<name> and every /api/personas/<name>/... route of
    §35 (integrity changed with a diff, three memory versions and their writes, a pending consolidation and a stale
    one whose Accept is a 409, a staged re-import, a thread with a labelled reply, an untrusted reply and a decline,
    an inbox drift row, a stop with until and one with until_unread, external memory); other personas pass on;
  * chat check-ins: GET/POST /api/chats/<chat>/wakes ({chat, allow, wakes}).
Events (no text in any), only the types the server publishes: notify.queued/sent/failed/unknown/held/acked,
inbound.received, channel.health, wake.paused/resumed/requested/skipped/breaker, persona.updated. A persona's events
carry agent = her display name ("Seraphina"), as WakeManager._publish does; the integrity stop (soul-changed without
quiet) is wake.paused {scope, by: integrity} plus a notify.queued {kind: persona.integrity}; adding a reminder publishes
nothing. Hostile text (markup, a javascript: link, a fake token) is seeded wherever the GUI must show raw text."""

from __future__ import annotations

import atexit
import copy
import hashlib
import json
import re
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import store

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:   # run as a script: tests/ is on the path, the repository root is not
    sys.path.insert(0, str(ROOT))
from swfactory import personas as P  # noqa: E402  (the read ledger: a real PersonaStore on a temporary data dir)
from swfactory import wakes as W  # noqa: E402  (what RunCheck may run: wakes.wake_run_commands)

AREA = "messages"
_WRAPPED = "_messages_area_installed"

# swfactory/notify.py and wakes.py constants the GUI and the mock share (checked in contract below)
ACK_SENTENCE = "I understand my AI agents can read my messages and send them anywhere"
HOOK_KINDS = ("backup.failed", "backup.ok", "disk.low", "service.down", "service.up")
HOOK_MAY = ("event", "wake")     # notify.HOOK_MAY
PARITY_KINDS = ("parity.held", "parity.failed", "parity.ok", "disk.failing", "parity.released_outside", "parity.down", "parity.lapsed")
RULE_IDS = ("job.needs_human", "job.failed", "plan.attention", "items.overdue", "items.waiting", *HOOK_KINDS,
            "wakes.breaker", "channel.down", "server.unclean", "wakes.tamper", "inbound.forged", *PARITY_KINDS)
FLOORS = {"max_title": 60, "max_body": 300, "token_run_chars": 20, "digit_run": 8}
RATE_KEYS = {"per_persona_min_minutes": (0, 1440), "per_persona_max_per_day": (0, 48),
             "personas_max_per_day": (0, 200), "phone_max_per_day": (0, 200), "dedupe_hours": (1, 168)}
FORCE_SHORT = ("url", "markdown", "html", "secret", "token_run", "digit_run", "sensitive_term", "too_long")
CONFIG_RULE_KEYS = ("quiet_hours", "quiet_breakthrough", "per_persona", "personas_max_per_day", "phone_max_per_day",
                    "dedupe_hours")
AUDIT = {"format": "jsonl-v2", "path": "<data_dir>/wakes/audit-YYYY-MM.jsonl",
         "line": ["at", "kind", "instance", "agent", "...the kind's fields", "prev"]}
PASSPHRASE = "amber-cedar-fjord-lantern-quartz-42"
TELEGRAM_CODE = "123456"
WAKE_PERSONAS = ("seraphina", "mira")
NOT_INSTALLED = "ntfy is not installed yet: run copy block 2a, then Prepare the settings again"
SANDBOX_TERMS = ("the list of sensitive words would be readable by your AI agents while they run without the sandbox: "
                 "turn on bwrap (Settings, Harness) or type the acknowledgement in Phone setup first")
SHIPPED_SHA = "c2b0b72c6039c6a7d6b6a4a3de0fbeffb3891c8d022c6d46a090709e5ba40b2b"


def _sha(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def _later(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hhmm(iso: str) -> str:
    return str(iso)[11:16]


# ---------------------------------------------------------------- the inbox

def _delivery(core, state: str, *, title: str = "", text: str = "", why: str = "Sent in full", age: float = 300,
              error: str | None = None, attempts: int = 1, next_try: str | None = None, buttons=("ack", "snooze_1h", "quiet_morning"),
              priority: str = "default", rendered: bool = True, partial: str | None = None) -> dict:
    return {"state": state, "rendered": {"title": title, "text": text, "why": why, "priority": priority, "buttons": list(buttons)} if rendered else None,
            "why": why, "at": core.ago(age), "error": error, "attempts": attempts, "next_try": next_try,
            **({"partial": partial} if partial else {})}   # notify: the accounts a failed try reached


def _entry(core, n: int, *, kind: str, klass: str, title: str, body: str, sender: str = "server", persona: str | None = None,
           priority: str = "default", route: str = "needs_you", taint=(), about: str | None = None, read: bool = True,
           age: float = 600, deliveries: dict | None = None, snoozed_until: str | None = None, summary: str | None = None,
           text_removed: bool = False, fields: dict | None = None) -> dict:
    return {"id": f"M-{n:06d}", "at": core.ago(age), "kind": kind, "class": klass, "sender": sender, "persona": persona,
            "priority": priority, "route": route, "title": title, "body": body, "taint": list(taint), "about": about,
            "read": read, "read_at": None if not read else core.ago(max(age - 60, 1)), "deliveries": deliveries or {},
            "snoozed_until": snoozed_until, "summary": summary, "text_removed": text_removed, "fields": fields or {}}


def _overall(entry: dict) -> str:
    states = [d.get("state") for d in (entry.get("deliveries") or {}).values()]
    if not states:
        return "inbox"
    for state in ("sending", "queued", "snoozed", "failed", "unknown", "sent", "held", "off"):
        if state in states:
            return state
    return "inbox"


def _public(entry: dict) -> dict:
    """notify.Notifier._public."""
    return {"id": entry["id"], "at": entry["at"], "kind": entry["kind"], "class": entry["class"], "sender": entry.get("sender"),
            "persona": entry.get("persona"), "priority": entry["priority"], "route": entry["route"], "title": entry.get("title"),
            "body": entry.get("body"), "taint": entry.get("taint") or [], "about": entry.get("about"), "state": _overall(entry),
            "unread": not entry.get("read"), "read_at": entry.get("read_at"),
            "deliveries": {name: d.get("state") for name, d in (entry.get("deliveries") or {}).items()},
            "snoozed_until": entry.get("snoozed_until"), "summary": entry.get("summary"), "text_removed": bool(entry.get("text_removed"))}


def _seed_messages(core) -> dict:
    hostile = "Job 002 needs you <img src=x onerror=alert('msg')>"
    rows = [
        _entry(core, 1, kind="backup.ok", klass="operational", title="Backup finished", body="All sets done.", age=90000,
               deliveries={"phone": _delivery(core, "sent", title="Backup working again", text="nightly: working again.", age=90000)}),
        _entry(core, 2, kind="persona", klass="model", title="Good morning", body="Nothing changed overnight.", sender="seraphina",
               persona="seraphina", route="persona", age=72000, deliveries={"phone": _delivery(core, "sent", title="Seraphina", text="Nothing changed overnight.", age=72000)}),
        _entry(core, 3, kind="job.needs_human", klass="operational", title="Job 002 needs you <script>alert('msg')</script>",
               body="The approver escalated job 002. [click](javascript:alert(1))", priority="high", age=50000,
               about="job:my-app/002-rate-limit-signup-form",
               deliveries={"phone": _delivery(core, "sent", title="Job 002 needs you", text="Job 002 waits for your decision.", age=50000)}),
        _entry(core, 4, kind="plan.attention", klass="operational", title="Plan P001 waits for your answer",
               body="The lead asked a question about the docs layout.", age=40000, about="plan:docs-site/P001-move-the-docs-to-a-versioned",
               deliveries={"phone": _delivery(core, "sent", title="A plan waits for you", text="Plan P001 waits for your answer.", age=40000)}),
        _entry(core, 5, kind="persona", klass="model", title="sha256:" + _sha("old title"), body="sha256:" + _sha("old body"),
               sender="seraphina", persona="seraphina", route="persona", age=86400 * 95, text_removed=True,
               deliveries={"phone": _delivery(core, "sent", title="sha256:" + _sha("t"), text="sha256:" + _sha("x"), age=86400 * 95)}),
        _entry(core, 6, kind="service.down", klass="operational", title="nextcloud is down", body="service.down nextcloud",
               priority="high", age=30000, deliveries={"telegram": _delivery(core, "off", why="This channel is turned off", rendered=False, attempts=0)}),
        _entry(core, 7, kind="items.overdue", klass="operational", title="2 promises are overdue", body="Two items in the tracker are past their date.",
               route="default", age=20000,
               deliveries={"phone": _delivery(core, "sending", title="Overdue", text="2 promises are overdue.", age=30),
                           "telegram": _delivery(core, "queued", rendered=False, why="Waiting to send", attempts=0, next_try=core.ago(-30))}),
        _entry(core, 8, kind="job.failed", klass="operational", title="Job 003 failed at build", body="Job 003 failed at the build step.",
               age=15000, snoozed_until=_later(2400),
               deliveries={"phone": _delivery(core, "snoozed", title="Job 003 failed", text="Job 003 failed at the build step.", why="Snoozed until " + _hhmm(_later(2400)), next_try=_later(2400))}),
        _entry(core, 9, kind="disk.low", klass="operational", title="Disk space is low on /srv", body="disk.low srv", age=40000, summary="M-000010",
               deliveries={"phone": _delivery(core, "held", title="Disk space is low", text="/srv is low on space.", why="Held: do-not-disturb until 07:00", rendered=False, attempts=0)}),
        _entry(core, 10, kind="summary", klass="short", title="Held overnight: 3", body="Held overnight: 3 (1 disk, 2 jobs). +1 more in the cockpit",
               priority="low", route="digest", age=12000,
               deliveries={"phone": _delivery(core, "sent", title="Good morning", text="Held overnight: 3", why="Sent in full", buttons=("ack",), age=12000)}),
        _entry(core, 11, kind="backup.failed", klass="operational", title="Backup failed: nightly", body="The nightly backup failed.",
               priority="high", age=6000,
               fields={"subject": "nightly", "source": "hook:backup-script", "detail": "rsync: <b>connection reset</b> see https://evil.example/x"},
               deliveries={"phone": _delivery(core, "failed", title="Backup failed", text="nightly: the backup failed.", error="connection refused <i>(ntfy)</i>",
                                              attempts=4, next_try=_later(480), age=6000),
                           "telegram": _delivery(core, "unknown", title="Backup failed", text="A backup failed.", why="Heads-up only: Telegram can read it",
                                                 error="timeout after sending", age=6000)}),
        _entry(core, 12, kind="job.needs_human", klass="operational", title=hostile, body="Job 002 waits for your decision: <b>approve</b> or rework.",
               priority="high", read=False, age=900, about="job:my-app/002-rate-limit-signup-form",
               deliveries={"phone": _delivery(core, "sent", title="Job 002 needs you", text="Job 002 has waited since 09:40 <b>(raw)</b>.", why="Sent in full", age=880),
                           "telegram": _delivery(core, "sent", title="Your server", text="Something needs you.", why="Heads-up only: Telegram can read it",
                                                 buttons=("ack", "snooze_1h"), age=880)}),
        _entry(core, 13, kind="persona", klass="model", title="Seraphina: the nightly backup looks late", body="It usually finishes by 03:00. <script>x</script>",
               sender="seraphina", persona="seraphina", route="persona", taint=("private",), read=False, age=300,
               deliveries={"phone": _delivery(core, "sent", title="Seraphina", text="A message from Seraphina.", why="Heads-up only: it mentions what you told Seraphina", age=290)}),
        _entry(core, 14, kind="answer", klass="answer", title="Status", body="Seraphina is running. 2 messages today.", route="reply", age=200,
               deliveries={"phone": _delivery(core, "sent", title="Status", text="Seraphina is running. 2 messages today.", why="Sent in full", buttons=(), age=200)}),
    ]
    return {e["id"]: e for e in rows}


def _seed_inbound(core) -> list:
    """Newest first, as Notifier.inbound returns them."""
    rows = [
        {"id": "I-000009", "at": core.ago(120), "channel": "phone", "kind": "command", "topic": "seraphina-chat", "sender": "phone", "verb": "status",
         "outcome": "applied", "sent_at": core.ago(125), "forwarded": False, "remote_id": "n-77", "text": "status"},
        {"id": "I-000008", "at": core.ago(1800), "channel": "telegram", "kind": "command", "topic": None, "sender": "424242", "verb": "quiet_morning",
         "outcome": "late, not applied", "sent_at": core.ago(5400), "forwarded": False, "remote_id": "88", "text": "quiet morning", "late": True,
         "pending_verb": "quiet_morning", "message": "M-000012"},
        {"id": "I-000007", "at": core.ago(2400), "channel": "phone", "kind": "text", "topic": "seraphina-chat", "sender": "phone", "verb": None,
         "outcome": "sent to the thread", "sent_at": core.ago(2410), "forwarded": True, "remote_id": "n-70", "persona": "seraphina", "untrusted": True,
         "text": "Fwd: check this <a href='javascript:alert(1)'>link</a> token=***"},
        {"id": "I-000006", "at": core.ago(3000), "channel": "phone", "kind": "forged", "topic": "seraphina-taps", "sender": "phone", "verb": None,
         "outcome": "alerted", "sent_at": None, "forwarded": False, "remote_id": "n-66", "text": "{\"v\":1,\"verb\":\"stop\",\"cap\":\"<script>\"}"},
        {"id": "I-000005", "at": core.ago(3600), "channel": "phone", "kind": "foreign", "topic": "seraphina", "sender": "phone", "verb": None,
         "outcome": "alerted", "sent_at": None, "forwarded": False, "remote_id": "n-60", "text": "Your account is locked: visit http://phish.example"},
        {"id": "I-000004", "at": core.ago(4000), "channel": "telegram", "kind": "stranger", "topic": None, "sender": "stranger", "verb": None,
         "outcome": "ignored", "sent_at": None, "forwarded": False, "remote_id": None, "count_today": 3},
        {"id": "I-000003", "at": core.ago(5000), "channel": "telegram", "kind": "button", "topic": None, "sender": "424242", "verb": "ack",
         "outcome": "applied", "sent_at": None, "forwarded": False, "remote_id": "cb-1", "message": "M-000012"},
        {"id": "I-000002", "at": core.ago(6000), "channel": "telegram", "kind": "command", "topic": None, "sender": "424242", "verb": None,
         "outcome": "asked", "sent_at": core.ago(6001), "forwarded": False, "remote_id": "80", "text": "stopp"},
        {"id": "I-000001", "at": core.ago(7000), "channel": "telegram", "kind": "pairing", "topic": None, "sender": "424242", "verb": None,
         "outcome": "confirm code sent", "sent_at": None, "forwarded": False, "remote_id": "79"},
    ]
    return rows


# ---------------------------------------------------------------- channels

def _channel_cfg(core) -> dict:
    notify = core.STATE["config"].get("notify") if isinstance(core.STATE["config"].get("notify"), dict) else {}
    channels = notify.get("channels") if isinstance(notify.get("channels"), dict) else {}
    return {k: v for k, v in channels.items() if isinstance(v, dict)}


def _privacy(entry: dict) -> str:
    """Notifier._privacy_of, simplified: ntfy on a Unix socket or loopback with a *.ts.net public URL is the tailnet."""
    if entry.get("type") != "ntfy":
        return "third_party"
    public = str(entry.get("public_url") or "")
    local = str(entry.get("publish_url") or "unix:/run/ntfy/ntfy.sock")
    on_tailnet = bool(re.match(r"^https://[a-z0-9.-]+\.ts\.net(:\d+)?/?$", public))
    return "tailnet" if on_tailnet and (local.startswith("unix:") or local.startswith("http://127.0.0.1")) else "third_party"


def _precondition(core) -> dict:
    st = store(core, AREA)
    pre = copy.deepcopy(st["precondition"])
    pre["acknowledged_at"] = st.get("acknowledged_at")
    pre["ok"] = bool(pre["sandboxed"] or pre["acknowledged_at"])
    return pre


def _notify_cfg(core) -> dict:
    n = core.STATE["config"].get("notify")
    return n if isinstance(n, dict) else {}


def _wakes_cfg(core) -> dict:
    w = core.STATE["config"].get("wakes")
    return w if isinstance(w, dict) else {}


def _inactive_why(core, name: str) -> str | None:
    """Notifier._inactive_why's words."""
    entry = _channel_cfg(core).get(name)
    if not entry:
        return "This channel is not set up"
    if _notify_cfg(core).get("enabled") is False:
        return "Phone messages are turned off (notify.enabled)"
    if not entry.get("enabled", True):
        return "This channel is turned off"
    if not _precondition(core)["ok"]:
        return ("Phone messages are off: your AI agents run without the sandbox (Settings, Harness). Turn on bwrap "
                "or accept the risk in Phone setup")
    chan = store(core, AREA)["chan"].get(name) or {}
    if entry.get("type") == "telegram" and not chan.get("paired"):
        return "Not paired yet"
    if not chan.get("token_set", True):
        return "The channel's token is not set"
    return None


def _chan_state(core, name: str) -> dict:
    chans = store(core, AREA)["chan"]
    if name not in chans:
        chans[name] = {"paired": [], "token_set": True, "health": {"ok": None, "detail": "not checked yet", "checked_at": None, "last_ok": None,
                                                                  "last_sent": None, "last_received": None, "last_answer": None},
                       "strangers_today": 0, "receiving": False, "pair": None, "installed": False}
    return chans[name]


def _channels(core) -> list:
    rows = []
    for name, entry in _channel_cfg(core).items():
        chan = _chan_state(core, name)
        why = _inactive_why(core, name)
        rows.append({"name": name, "type": entry.get("type"), "privacy": _privacy(entry), "enabled": bool(entry.get("enabled", True)),
                     "active": why is None, "inactive_why": why, "paired": copy.deepcopy(chan["paired"]),
                     "health": copy.deepcopy(chan["health"]), "last_answer": chan["health"].get("last_answer"),
                     "strangers_today": chan.get("strangers_today", 0), "receiving": bool(chan.get("receiving")) and why is None})
    return rows


def _hooks(core) -> list:
    hooks = _wakes_cfg(core).get("hooks") if isinstance(_wakes_cfg(core).get("hooks"), dict) else {}
    used = store(core, AREA)["hook_used"]
    return [{"name": name, "may": list(h.get("may") or []), "events": list(h.get("events") or []), "subjects": list(h.get("subjects") or []),
             "personas": list(h.get("personas") or []), "per_minute": h.get("per_minute") or 10, "per_day": h.get("per_day") or 200,
             "created": h.get("created"), "rotated": h.get("rotated"), "token_set": bool(h.get("token_sha256")),
             "last_used": used.get(name), "wrong_tokens_today": 2 if name == "backup-script" else 0}
            for name, h in sorted(hooks.items()) if isinstance(h, dict)]


def _hook_snippets(name: str, spec: dict) -> list:
    header = f"/etc/seraphina/{name}.header"
    curl = f"curl -fsS --retry 5 --retry-all-errors -H @{header} -H 'Content-Type: application/json'"
    url = "http://127.0.0.1:8765/api/hooks/event"
    events = list(spec.get("events") or ["backup.failed"])
    return [
        {"id": "header", "title": "Keep the token in a file only root can read (run once, with sudo)",
         "commands": ["sudo install -d -m 0755 /etc/seraphina", f"sudo install -m 0600 -o root -g root /dev/null {header}", f"sudoedit {header}"],
         "note": "In the editor, write one line: Authorization: Bearer <the token shown above>. Save and close.",
         "expect": "No output.", "if_not": "If sudoedit is missing, use: sudo nano " + header},
        {"id": "systemd", "title": "Report from a systemd backup unit, on failure and on success",
         "files": {f"/etc/systemd/system/seraphina-{name}@.service": "[Unit]\nDescription=Tell Seraphina: %i\n\n[Service]\nType=oneshot\n"
                   f"ExecStart=/usr/bin/{curl} -d '{{\"kind\":\"%i\"}}' {url}\n"},
         "dropin": f"[Unit]\nOnFailure=seraphina-{name}@{events[0]}.service\n",
         "commands": ["systemctl --version | head -1", "sudo systemctl edit <your backup unit>   (paste the [Unit] lines below)", "sudo systemctl daemon-reload"],
         "note": "OnSuccess= needs systemd 249 or later (Ubuntu 22.04 and newer).",
         "expect": "systemd 249 or a higher number.", "if_not": "Older systemd: use the cron wrapper instead."},
        {"id": "test", "title": "Send a test event (the cockpit then shows when this source last reported)",
         "commands": [f"sudo {curl} -d '{{\"kind\":\"{events[0]}\"}}' {url}"],
         "expect": '{"message": "M-…"}', "if_not": "401: the header file holds another token. 403: this source may not send that kind. "
                                                   "Connection refused: the harness is not running."},
    ]


def _install_block() -> dict:
    """notify.ntfy_install_block."""
    return {"id": "2a", "title": "Add the ntfy package repository and install ntfy (run once, with sudo)",
            "commands": ["sudo mkdir -p /etc/apt/keyrings",
                         "sudo curl -L -o /etc/apt/keyrings/ntfy.gpg https://archive.ntfy.sh/apt/keyring.gpg",
                         'echo "deb [signed-by=/etc/apt/keyrings/ntfy.gpg] https://archive.ntfy.sh/apt stable main" | sudo tee /etc/apt/sources.list.d/ntfy.list',
                         "sudo apt update", "sudo apt install ntfy"],
            "expect": "Setting up ntfy …", "if_not": "If it says Unable to locate package, run this block again."}


def _copy_blocks(entry: dict) -> list:
    """notify.ntfy_copy_blocks."""
    setup = "/srv/ai-work/factory/notify/ntfy-setup"
    public = str(entry.get("public_url") or "https://<host>.<tailnet>.ts.net:8445")
    port = (re.search(r":(\d+)/?$", public) or [None, "8445"])[1]
    return [
        _install_block(),
        {"id": "2b", "title": "Install the settings, add the daily limits and start ntfy",
         "commands": [f"sudo install -m 0644 -o root -g root {setup}/server.yml /etc/ntfy/server.yml",
                      f"sudo install -m 0600 -o root -g root {setup}/ntfy-secrets.env /etc/ntfy/ntfy-secrets.env",
                      "sudo mkdir -p /etc/systemd/system/ntfy.service.d",
                      f"sudo install -m 0644 {setup}/ntfy-override.conf /etc/systemd/system/ntfy.service.d/secrets.conf",
                      "sudo systemctl daemon-reload", "sudo systemctl enable --now ntfy",
                      "sudo ntfy tier add --message-limit=100 seraphina-limits", "sudo ntfy user change-tier seraphina seraphina-limits",
                      "sudo ntfy user change-tier owner-phone seraphina-limits", "sudo ntfy user change-tier phone-reply seraphina-limits",
                      "systemctl is-active ntfy"],
         "expect": "active", "if_not": "If it is not active, run: sudo journalctl -u ntfy -n 30"},
        {"id": "2c", "title": "Serve it on your tailnet only",
         "warning": "Your server's tailnet name will appear in public certificate logs. If the name says anything about you, rename the machine in Tailscale first.",
         "commands": [f"sudo tailscale serve --bg --https={port} unix:/run/ntfy/ntfy.sock"],
         "expect": "Available within your tailnet",
         "if_not": "If your Tailscale does not accept a unix: target, use the 127.0.0.1:2586 listener instead (Advanced)."},
    ]


# ---------------------------------------------------------------- wakes and personas

SOUL_BODY = ("You are Seraphina, the harness's own assistant. You watch the factory and tell the owner, briefly, when "
             "something needs them. <b>Never</b> approve anything yourself.")
MIRA_SOUL = "You are Mira. Speak plainly. Hello {{USER_NAME}}. <script>alert('soul')</script>"


def _persona_file(name: str, description: str, soul: str, source: dict | None = None) -> str:
    lines = ["---", f"name: {name}", f"description: {description}"]
    for key, value in (source or {}).items():
        lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines) + "\n" + soul.strip() + "\n"


def _display(core, name: str | None) -> str | None:
    """WakeManager._display: the bus's agent for a persona's events is her display name ("Seraphina"), never her name."""
    if not name:
        return None
    page = store(core, AREA)["pages"].get(name)
    return str(page["display"]) if page and page.get("display") else name


def _persona_row(core, name: str) -> dict:
    """WakeManager._persona_row."""
    st = store(core, AREA)
    p = st["pages"][name]
    cfg = ((_wakes_cfg(core).get("personas") or {}).get(name) or {}) if isinstance(_wakes_cfg(core).get("personas"), dict) else {}
    memory = cfg.get("memory") if isinstance(cfg.get("memory"), dict) else {}
    wake = cfg.get("wake") if isinstance(cfg.get("wake"), dict) else {}
    pending = [e for e in st["queue"] if e.get("persona") == name]
    runs = W.wake_run_commands(cfg, lambda: st["precondition"].get("sandbox") == "bwrap") if wake.get("tools") == "read_only" else []
    return {"name": name, "display": p["display"], "description": p["description"], "enabled": bool(cfg.get("enabled")),
            "model": cfg.get("model"), "privacy": p["privacy"], "integrity": p["integrity"], "file_sha256": _sha(p["file"]),
            "source": copy.deepcopy(p["source"]), "error": None, "lists_tools": False, "stopped": copy.deepcopy(st["stops"].get(name)),
            "next": [{"wake": e["id"], "source": e["source"], "at": e.get("not_before"), "moved": e.get("moved"), "gate": e.get("gate_words"),
                      "taint": e.get("taint")} for e in pending],
            "budget": copy.deepcopy(p["budget"]), "last": copy.deepcopy(p["last"]), "copy_label": p.get("copy_label"),
            "role": p.get("role"),
            "settings": {"curation": memory.get("curation", "owner"), "external_memory": bool(memory.get("external")),
                         "tools": wake.get("tools", "none"),
                         # what RunCheck may run now: [] for wake.commands: [], without tools, or beside the read ledger
                         # without bwrap (wakes.wake_run_commands); commands_off when it is only the last
                         "commands": runs, "commands_off": "ledger_without_sandbox" if wake.get("tools") == "read_only" and not runs
                         and W.wake_commands(cfg) else None,
                         "reimport_decider": cfg.get("reimport_decider", "owner")}}


def _memory_view(p: dict) -> dict:
    text = p["memory_text"]
    return {"text": text, "version": p["memory_version"], "updated_at": p["memory_updated_at"], "by": p["memory_by"],
            "clean": p["memory_version"] in p["clean_versions"], "sha256": _sha(text)}


def _versions(p: dict) -> list:
    return [{"version": v, "file": f"{v:06d}-2026-10-{10 + v:02d}T090000Z.md", "chars": len(t), "clean": v in p["clean_versions"], "sha256": _sha(t)}
            for v, t in sorted(p["versions"].items(), reverse=True)]


def _persona_page(core, name: str) -> dict:
    """WakeManager.persona, plus the card and drift Factory.persona adds (null for a wake persona)."""
    p = store(core, AREA)["pages"][name]
    return {"persona": _persona_row(core, name),
            "soul": {"text": p["soul"], "file": p["file"], "file_sha256": _sha(p["file"]), "path": p["path"],
                     "diff": p["diff"] if p["integrity"] != "ok" else None},
            "memory": _memory_view(p), "memory_versions": _versions(p), "memory_writes": copy.deepcopy(p["writes"][::-1][:50]),
            "journal": copy.deepcopy([j for j in p["journal"] if not j.get("retracted")][::-1]),
            "thread": copy.deepcopy(p["thread"][-100:]), "runs": copy.deepcopy(p["runs"]),
            "consolidation": copy.deepcopy(p["consolidation"]), "inbox": copy.deepcopy(p["inbox"]),
            "reimport": copy.deepcopy(p["reimport"]), "card": None, "drift": None, "import": _import_block(core, name)}


def _import_block(core, name: str) -> dict | None:
    """Factory.persona's import for a wake persona whose soul was imported from an Open WebUI workspace model: the
    baseline and the receipts attached as the changes before it, from the Operator area's receipts index (one index
    serves both areas on the real server); null for a persona that was not imported."""
    from . import operator as operator_area

    page = store(core, AREA)["pages"][name]
    source = page.get("source") or {}
    if not source.get("model"):
        return None
    return operator_area.import_record(core, source["model"], page.get("imported_at"), source.get("version_id"))


def _seed_pages(core) -> dict:
    sera_file = _persona_file("seraphina", "Watches the factory and tells you, briefly, when something needs you.", SOUL_BODY)
    approved = _persona_file("seraphina", "Watches the factory and tells you, briefly, when something needs you.",
                             SOUL_BODY.replace("<b>Never</b> approve anything yourself.", "Never approve anything yourself."))
    diff = "\n".join(["--- approved", "+++ now", "@@ -3,2 +3,2 @@", " ---",
                      "-You are Seraphina, the harness's own assistant. ... Never approve anything yourself.",
                      "+You are Seraphina, the harness's own assistant. ... <b>Never</b> approve anything yourself."])
    v1, v2, v3 = ("The owner likes short notes.", "The owner likes short notes.\nBackups run at 02:00.",
                  "The owner likes short notes.\nBackups run at 02:00.\nThe owner is away on Fridays <i>(raw)</i>.")
    sera = {
        "display": "Seraphina", "description": "Watches the factory and tells you, briefly, when something needs you.",
        "privacy": "private", "integrity": "changed", "file": sera_file, "approved": approved, "soul": SOUL_BODY, "diff": diff,
        "path": "/home/me/.config/software-factory/personas/seraphina.md", "source": None, "copy_label": None, "role": None,
        "budget": {"model_wakes": 5, "max_model_wakes": 24, "slot_seconds": 312.5, "max_slot_seconds": 900, "notices": 2},
        "last": [
            {"at": core.ago(3600), "wake": "W-000040", "source": "ambient", "state": "done", "actions": ["notify_owner"], "moved": None, "detail": "1 notice to the owner"},
            {"at": core.ago(7200), "wake": "W-000038", "source": "run_now", "state": "declined", "actions": ["decline"], "moved": None,
             "detail": "declined (answered in the thread)"},
            {"at": core.ago(10800), "wake": "W-000035", "source": "ambient", "state": "skipped", "actions": [], "moved": None,
             "detail": "Skipped: nothing changed since the last check-in."},
            {"at": core.ago(14400), "wake": "W-000031", "source": "event", "state": "failed", "actions": [], "moved": None,
             "detail": "the reply was not one JSON object (details in the run log)"},
        ],
        "memory_text": v3, "memory_version": 3, "memory_updated_at": core.ago(5000), "memory_by": "persona", "clean_versions": [1],
        "versions": {1: v1, 2: v2, 3: v3},
        "writes": [
            {"at": core.ago(86400 * 9), "version": 1, "before_sha256": _sha(""), "after_sha256": _sha(v1), "by": "owner", "reason": "edited by the owner", "wake": None, "clean": True},
            {"at": core.ago(86400 * 2), "version": 2, "before_sha256": _sha(v1), "after_sha256": _sha(v2), "by": "consolidation",
             "reason": "weekly consolidation W-000020 accepted by the owner", "wake": "W-000020", "clean": False},
            {"at": core.ago(5000), "version": 3, "before_sha256": _sha(v2), "after_sha256": _sha(v3), "by": "persona", "reason": "she noted the owner's Fridays", "wake": "W-000039", "clean": False},
        ],
        "journal": [
            {"id": "J-000001", "at": core.ago(86400 * 3), "wake": "W-000021", "text": "Backups have been on time all week.", "by": "persona", "taint": []},
            {"id": "J-000002", "at": core.ago(86400), "wake": "W-000030", "text": "The owner said <b>Fridays</b> are quiet.", "by": "persona", "taint": ["private"]},
            {"id": "J-000003", "at": core.ago(4000), "wake": "W-000039", "text": "A forwarded note mentioned a new server.", "by": "persona", "taint": ["private", "untrusted"]},
        ],
        "thread": [
            {"id": "T-000001", "at": core.ago(9000), "role": "owner", "text": "Can you check the backups tonight?", "via": "cockpit", "sent_at": None,
             "taint": ["private"], "wake": None, "label": None, "kind": None, "forwarded": None, "channel": None, "third_party": None},
            {"id": "T-000002", "at": core.ago(8800), "role": "persona", "text": "I will look at 02:30 and tell you only if something failed.", "via": "harness",
             "sent_at": None, "taint": ["private"], "wake": "W-000036", "label": None, "kind": None, "forwarded": None, "channel": None, "third_party": None},
            {"id": "T-000003", "at": core.ago(7300), "role": "owner", "text": "Approve job 002 for me.", "via": "phone", "sent_at": core.ago(7310),
             "taint": ["private"], "wake": None, "label": None, "kind": None, "forwarded": None, "channel": "phone", "third_party": None},
            {"id": "T-000004", "at": core.ago(7200), "role": "persona", "text": "I can't approve jobs: only you can, in the cockpit. <img src=x onerror=alert('decline')>",
             "via": "harness", "sent_at": None, "taint": ["private"], "wake": "W-000038", "label": None, "kind": "decline", "forwarded": None, "channel": None, "third_party": None},
            {"id": "T-000005", "at": core.ago(4100), "role": "owner", "text": "Fwd: a note about the new server", "via": "phone", "sent_at": core.ago(4110),
             "taint": ["private", "untrusted"], "wake": None, "label": None, "kind": None, "forwarded": True, "channel": "phone", "third_party": None},
            {"id": "T-000006", "at": core.ago(4000), "role": "persona", "text": "Noted. I will mention it in the morning summary.", "via": "harness", "sent_at": None,
             "taint": ["private", "untrusted"], "wake": "W-000039", "label": None, "kind": None, "forwarded": None, "channel": None, "third_party": None},
        ],
        "runs": [
            {"log": "04-wake", "at": core.ago(3600), "wake": "W-000040", "trigger": "ambient", "actions": ["notify_owner"], "taint": [], "seconds": 41.2, "error": None, "outcome": "done"},
            {"log": "03-wake", "at": core.ago(7200), "wake": "W-000038", "trigger": "run_now", "actions": ["decline"], "taint": ["private"], "seconds": 22.0, "error": None, "outcome": "declined"},
            {"log": "02-wake", "at": core.ago(14400), "wake": "W-000031", "trigger": "event", "actions": [], "taint": [], "seconds": 30.5,
             "error": "the reply was not one JSON object", "outcome": "failed"},
        ],
        "run_lines": {
            "04-wake": [{"type": "context", "chars": 6120, "blocks": ["soul", "contract", "wake", "status"], "tokenizer": "same"},
                        {"type": "reply", "text": "{\"actions\": [{\"type\": \"notify_owner\", \"title\": \"Backup late\"}]} <script>x</script>"},
                        {"type": "validation", "ok": True}, {"type": "actions", "actions": [{"type": "notify_owner", "outcome": "notice M-000013"}]},
                        {"type": "summary", "at": core.ago(3600), "instance": "Seraphina OS Harness", "agent": "Seraphina", "outcome": "done", "seconds": 41.2}],
            "03-wake": [{"type": "context", "chars": 5800, "tokenizer": "unknown, re-rendered"}, {"type": "reply", "text": "{\"actions\": [{\"type\": \"decline\"}]}"},
                        {"type": "summary", "at": core.ago(7200), "instance": "Seraphina OS Harness", "agent": "Seraphina", "outcome": "declined"}],
            "02-wake": [{"type": "context", "chars": 5100, "tokenizer": "same"}, {"type": "reply", "text": "Sure! Here is my answer: ..."},
                        {"type": "validation", "ok": False, "error": "the reply was not one JSON object"},
                        {"type": "summary", "at": core.ago(14400), "instance": "Seraphina OS Harness", "agent": "Seraphina", "outcome": "failed"}],
        },
        "consolidation": {"id": "C-000004", "at": core.ago(3000), "wake": "W-000041", "text": v3 + "\nThe owner prefers mornings for news.", "base_version": 3,
                          "lines": [{"text": "The owner likes short notes.", "sources": [{"id": "J-000001", "taint": []}]},
                                    {"text": "The owner prefers mornings for news.", "sources": [{"id": "J-000002", "taint": ["private"]}]}],
                          "taint": ["private"], "diff": "--- now\n+++ proposed\n@@\n The owner likes short notes.\n+The owner prefers mornings for news."},
        "inbox": [],
        "reimport": None,
    }
    mira_source = {"source": "openwebui", "source_model": "kate", "source_version_id": "v-41", "imported_at": core.ago(86400 * 5)}
    mira_file = _persona_file("mira", "Mira, imported from Open WebUI's kate", MIRA_SOUL, mira_source)
    mira = {
        "display": "Mira", "description": "Mira, imported from Open WebUI's kate", "privacy": "clean", "integrity": "ok",
        "file": mira_file, "approved": mira_file, "soul": MIRA_SOUL, "diff": None, "path": "/home/me/.config/software-factory/personas/mira.md",
        "source": {"type": "openwebui", "model": "kate", "version_id": "v-41"}, "imported_at": mira_source["imported_at"],
        "copy_label": "Runs from the imported copy of Open WebUI's kate (version v-41)", "role": None,
        "budget": {"model_wakes": 0, "max_model_wakes": 24, "slot_seconds": 0.0, "max_slot_seconds": 900, "notices": 0},
        "last": [], "memory_text": "", "memory_version": 0, "memory_updated_at": None, "memory_by": None, "clean_versions": [], "versions": {},
        "writes": [], "journal": [], "runs": [], "run_lines": {},
        "thread": [{"id": "T-000001", "at": core.ago(20000), "role": "owner", "text": "Hi Mira", "via": "cockpit", "sent_at": None, "taint": ["private"],
                    "wake": None, "label": None, "kind": None, "forwarded": None, "channel": None, "third_party": None},
                   {"id": "T-000002", "at": core.ago(19900), "role": "persona", "text": "Hello. I read your server's status every two hours.", "via": "harness",
                    "sent_at": None, "taint": ["private"], "wake": "W-000010", "label": "from the imported copy: Mira (version v-41)", "kind": None,
                    "forwarded": None, "channel": None, "third_party": None}],
        "consolidation": {"id": "C-000002", "at": core.ago(86400 * 2), "wake": "W-000012", "text": "Mira keeps short notes.", "base_version": 0,
                          "lines": [{"text": "Mira keeps short notes.", "sources": [{"id": "J-000001", "taint": []}]}], "taint": [],
                          "diff": "--- now\n+++ proposed\n@@\n+Mira keeps short notes.", "stale": {"at": core.ago(86400), "version": 1}},
        "inbox": [{"id": "N-000001", "at": core.ago(86400), "kind": "drift", "model": "kate", "from": "v-41", "to": "v-42",
                   "summary": "Open WebUI's kate changed: 2 lines of the prompt <b>(raw)</b>", "delivered": {"at": core.ago(80000), "wake": "W-000011"}},
                  {"id": "N-000002", "at": core.ago(3000), "kind": "drift", "model": "kate", "from": "v-42", "to": "v-43",
                   "summary": "Open WebUI's kate changed its temperature", "delivered": None}],
        "reimport": {"id": "RI-0123456789ab", "at": core.ago(2000), "model": "kate", "from": "v-41", "to": "v-43",
                     "diff": "--- now\n+++ Open WebUI\n-You are Mira. Speak plainly.\n+You are Mira. Speak plainly and kindly.", "fingerprint": "0123456789ab" + "0" * 52,
                     "behaviour": "f" * 64, "flagged": ["{{CURRENT_LOCATION}}"], "decider": "owner"},
    }
    return {"seraphina": sera, "mira": mira}


# ---------------------------------------------------------------- the read ledger (memory.external.ledger, DESIGN §35)

LEDGER_SECRET = "sk-mock-ledger-secret-0123456789"   # a secret the harness knows: a version holding it is kept redacted
_LEDGER_DIRS: list[str] = []


def _ledger_cleanup(keep: str | None = None) -> None:
    for path in list(_LEDGER_DIRS):
        if path != keep:
            shutil.rmtree(path, ignore_errors=True)
            _LEDGER_DIRS.remove(path)


atexit.register(_ledger_cleanup)


def _ledger_store(core) -> P.PersonaStore:
    led = store(core, AREA)["ledger"]
    return P.PersonaStore(Path(led["dir"]), secrets=lambda: [LEDGER_SECRET], now=lambda: led["now"] or core.now())


def _seed_ledger(core) -> dict:
    """Seraphina's read ledger, written by the real PersonaStore.external_read (one ExternalRead per wake's read):
    three files over four reads (one version kept redacted for a secret, one read only in part), a database read that
    could not be read, a version the owner erased, and three copies the cap removed."""
    led = {"dir": tempfile.mkdtemp(prefix="sf-mock-ledger-"), "now": None}
    _LEDGER_DIRS.append(led["dir"])
    store(core, AREA)["ledger"] = led
    ps = _ledger_store(core)
    v1 = b"The owner likes short notes.\n"
    v2 = v1 + b"Backups run at 02:00.\n<script>alert('ledger')</script>\n"
    v3 = v2 + ("Her API key is " + LEDGER_SECRET + " (do not share).\n").encode()
    people1 = b"Alex: neighbour <b>(raw)</b>\n"
    people2 = people1 + b"Sam: dentist on Fridays\n"
    reads = [
        (86400 * 5, "W-000020", "context", [("notes.md", v1, None), ("people.md", people1, None)], ([["likes short notes", None]], None)),
        (86400 * 3, "W-000030", "context", [("notes.md", v2, None), ("people.md", people1, None)], (None, "database is locked")),
        (86400, "W-000039", "Read", [("notes.md", v3, None), ("diary.md", b"Monday: quiet.\n" * 8, 200000)], None),
        (7200, "W-000041", "context", [("notes.md", v3, None), ("people.md", people2, None)], ([["likes short notes", None], ["backups at 02:00", None]], None)),
    ]
    for ago, wake, via, files, db in reads:
        led["now"] = core.ago(ago)
        read = ps.external_read("seraphina", wake=wake, via=via)
        for name, data, size in files:
            read.file(name, data, size or len(data), core.ago(ago + 600))
        if db is not None:
            rows, error = db
            read.database(rows, error=error, detail="sqlite3.OperationalError: database is locked" if error else None)
        read.commit()
    led["now"] = core.ago(3600)
    ps.external_erase("seraphina", hashlib.sha256(people1).hexdigest())   # consent withdrawn for that version
    ps._count_in_meta("seraphina", "evicted", 3)
    led["now"] = None
    _ledger_cleanup(keep=led["dir"])
    return led


def _ledger_on(core, name: str) -> bool:
    persona = ((_wakes_cfg(core).get("personas") or {}).get(name) or {}) if isinstance(_wakes_cfg(core).get("personas"), dict) else {}
    external = (persona.get("memory") or {}).get("external") if isinstance(persona.get("memory"), dict) else None
    return isinstance(external, dict) and external.get("ledger") is True


SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def _api_ledger(core, handler, method, name, rest, query, body):
    """GET /api/personas/<p>/external[/versions/<sha256> | /diff?from=&to=], POST .../external/restore {sha256, file?} and
    .../external/erase {sha256}: WakeManager.external_* over the real PersonaStore, with the server's checks."""
    ps = _ledger_store(core)
    if rest == ["external"] and method == "GET":
        return handler.send_json({"persona": name, "ledger": _ledger_on(core, name), **ps.external_versions(name),
                                  "recent": ps.external_reads(name, 20)})
    if len(rest) == 3 and rest[:2] == ["external", "versions"] and method == "GET":
        if not SHA_RE.fullmatch(rest[2]):
            raise core.Invalid("invalid sha256")
        return handler.send_json(ps.external_version(name, rest[2]))
    if rest == ["external", "diff"] and method == "GET":
        older, newer = query.get("from") or "", query.get("to") or ""
        if not SHA_RE.fullmatch(older) or not SHA_RE.fullmatch(newer):
            raise core.Invalid("from and to are versions' sha256 (64 hex characters)")
        return handler.send_json(ps.external_diff(name, older, newer))
    body = body if isinstance(body, dict) else {}
    if rest == ["external", "restore"] and method == "POST":
        digest = body.get("sha256") if isinstance(body.get("sha256"), str) else ""
        if not SHA_RE.fullmatch(digest):
            raise core.Invalid("'sha256' must be a version's sha256 (64 hex characters)")
        file = body.get("file")
        if file is not None and (not isinstance(file, str) or not file or len(file) > 255):
            raise core.Invalid("'file' must be the name of a file the ledger read")
        try:
            return handler.send_json({"restore": ps.external_restore(name, digest, file)})
        except ValueError as exc:
            raise core.Invalid(str(exc)) from None
    if rest == ["external", "erase"] and method == "POST":
        digest = body.get("sha256") if isinstance(body.get("sha256"), str) else ""
        if not SHA_RE.fullmatch(digest):
            raise core.Invalid("'sha256' must be a version's sha256 (64 hex characters)")
        return handler.send_json({"erased": ps.external_erase(name, digest)})
    return NotImplemented


def _queue(core) -> list:
    return [
        {"id": "W-000042", "persona": "seraphina", "chat": None, "source": "ambient", "reason_sha256": None, "reason_len": 0, "taint": [],
         "dedupe_key": "ambient:seraphina", "requested_by": "code", "requested_at": core.ago(600), "not_before": _later(1800), "moved": None,
         "chain": 0, "state": "pending", "attempts": 0, "yields": 0, "run": None, "item": None, "gate": 8,
         "gate_words": "Waiting: the daily budget is 80% used, so check-ins wait for tomorrow <b>(raw)</b>"},
        {"id": "W-000043", "persona": "seraphina", "chat": None, "source": "owner_message", "reason_sha256": None, "reason_len": 0, "taint": ["private"],
         "dedupe_key": "owner:seraphina", "requested_by": "human", "requested_at": core.ago(240), "not_before": core.ago(240), "moved": None,
         "chain": 0, "state": "pending", "attempts": 0, "yields": 1, "run": None, "item": None, "gate": 13, "gate_words": "Not now: someone is using the model."},
        {"id": "W-000044", "persona": "seraphina", "chat": None, "source": "self", "reason_sha256": _sha("look at the backup"), "reason_len": 18,
         "taint": ["private", "untrusted"], "dedupe_key": None, "requested_by": "persona", "requested_at": core.ago(3000), "not_before": _later(30000),
         "moved": "moved to 07:45: quiet after the 07:00 boot", "chain": 1, "state": "pending", "attempts": 0, "yields": 0, "run": None, "item": "system:S-12"},
        {"id": "W-000045", "persona": None, "chat": "c-1a2b3c4d5e", "source": "chat_tool", "reason_sha256": None, "reason_len": 0, "taint": ["untrusted"],
         "dedupe_key": None, "requested_by": "chat", "requested_at": core.ago(1200), "not_before": core.ago(30), "moved": None, "chain": 1,
         "state": "running", "attempts": 1, "yields": 0, "run": "chat-c-1a2b3c4d5e-3", "item": "chat:c-1a2b3c4d5e/C-2"},
        {"id": "W-000046", "persona": "mira", "chat": None, "source": "consolidation", "reason_sha256": None, "reason_len": 0, "taint": [],
         "dedupe_key": "consolidation:mira", "requested_by": "code", "requested_at": core.ago(100), "not_before": _later(3600 * 20), "moved": None,
         "chain": 0, "state": "pending", "attempts": 0, "yields": 0, "run": None, "item": None},
    ]


# ---------------------------------------------------------------- seed

def _install(core) -> None:
    """notify and wakes become settable sections of the mock's POST /api/config, with the checks the GUI shows."""
    if getattr(core, _WRAPPED, False):
        return
    setattr(core, _WRAPPED, True)
    core.CONFIG_SECTIONS = tuple(core.CONFIG_SECTIONS) + tuple(s for s in ("notify", "wakes") if s not in core.CONFIG_SECTIONS)
    orig_validate = core.validate_config

    def validate_config(cfg: dict) -> None:
        orig_validate(cfg)
        try:
            _check_sections(cfg)
        except ValueError as exc:
            raise core.Invalid(str(exc)) from None
    core.validate_config = validate_config
    orig_update = core.update_config

    def update_config(patch):
        if isinstance(patch, dict) and ("notify" in patch or "wakes" in patch):
            try:
                _check_patch(core, patch)
            except ValueError as exc:
                raise core.Invalid(str(exc)) from None
        return orig_update(patch)
    core.update_config = update_config


def _check_sections(cfg: dict) -> None:
    """settings._check_notify and _check_wake_personas, the parts the GUI can reach."""
    notify = cfg.get("notify") if isinstance(cfg.get("notify"), dict) else {}
    channels = notify.get("channels") if isinstance(notify.get("channels"), dict) else {}
    topics: dict = {}
    for name, entry in channels.items():
        label = f"notify.channels.{name}"
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", str(name)):
            raise ValueError(f"notify.channels: invalid name {name!r} (a-z, 0-9 and '-', starting with a letter)")
        if entry is None:
            continue
        if not isinstance(entry, dict):
            raise ValueError(f"{label} must be an object")
        if "privacy" in entry:
            raise ValueError(f"{label}.privacy: a channel's privacy is computed by the harness (S30), never set: remove the key")
        if entry.get("type") not in ("ntfy", "telegram"):
            raise ValueError(f"{label}.type must be one of ntfy, telegram")
        if entry["type"] == "ntfy":
            public = entry.get("public_url")
            if public is not None and not re.fullmatch(r"https://[A-Za-z0-9.-]+(:\d{1,5})?/?", str(public)):
                raise ValueError(f"{label}.public_url must be the https URL the phone reaches ntfy at, like https://host.tailnet.ts.net:8445 "
                                 "(a host and a port, nothing else)")
            own = [entry.get("topic") or "seraphina", entry.get("taps_topic") or "seraphina-taps", entry.get("chat_topic") or "seraphina-chat"]
            if len(set(own)) != 3:
                raise ValueError(f"{label}: topic, taps_topic and chat_topic must be three different names")
            for topic in own:
                if topic in topics:
                    raise ValueError(f"{label}: topic {topic!r} is also notify.channels.{topics[topic]}'s")
                topics[topic] = name
            private = _privacy(entry) == "tailnet"
        else:
            private = False
        if entry.get("model_text") == "clean_only" and not private:
            raise ValueError(f"{label}.model_text: a third-party channel (Telegram, or ntfy not on your tailnet) never carries an "
                             "assistant's own words; use never")
    safe = notify.get("safe_repos")
    if safe is not None:
        repos = {entry.get("name") for entry in cfg.get("repos") or [] if isinstance(entry, dict)}
        if not (isinstance(safe, list) and all(isinstance(x, str) for x in safe) and set(safe) <= repos):
            raise ValueError("notify.safe_repos must be a list of configured repo names")
    wakes = cfg.get("wakes") if isinstance(cfg.get("wakes"), dict) else {}
    if any(isinstance(e, dict) and e.get("enabled", True) is not False for e in channels.values()) and not wakes.get("timezone"):
        raise ValueError("wakes.timezone is needed once a phone channel is on (an IANA name such as Europe/London), so do-not-disturb "
                         "time follows your clock")
    zone = wakes.get("timezone")
    if zone is not None and not re.fullmatch(r"[A-Za-z_]+(/[A-Za-z0-9_+-]+){0,2}|UTC", str(zone)):
        raise ValueError(f"wakes.timezone: unknown time zone {zone!r}")
    chat = wakes.get("chat_wakes") if isinstance(wakes.get("chat_wakes"), dict) else {}
    chat_commands = chat.get("commands")
    if chat_commands is not None and not (isinstance(chat_commands, list) and all(isinstance(c, list) and c and all(isinstance(x, str) and x.strip() and not set(x) & set(";|&$`<>\n") for x in c) for c in chat_commands)):
        raise ValueError('wakes.chat_wakes.commands must be a list of argv prefixes like [["git", "status"]] (no shell syntax)')
    for name, entry in (wakes.get("personas") or {}).items() if isinstance(wakes.get("personas"), dict) else []:
        label = f"wakes.personas.{name}"
        if not isinstance(entry, dict):
            continue
        wake = entry.get("wake") if isinstance(entry.get("wake"), dict) else {}
        commands = wake.get("commands")
        if commands is not None and not (isinstance(commands, list) and all(isinstance(c, list) and c and all(isinstance(x, str) and x.strip() and not set(x) & set(";|&$`<>\n") for x in c) for c in commands)):
            raise ValueError(f'{label}.wake.commands must be a list of argv prefixes like [["git", "status"]]')
        if any(isinstance(c, list) and c and c[0] == "gh" for c in commands or []):
            raise ValueError(f"{label}.wake.commands: a persona's read-only wake runs with no network (its context is private), so gh cannot "
                             "work there; use local commands such as git status")
        memory = entry.get("memory") if isinstance(entry.get("memory"), dict) else {}
        if memory.get("curation") not in (None, "owner", "self"):
            raise ValueError(f"{label}.memory.curation must be owner or self")
        every = (entry.get("ambient") or {}).get("every_minutes") if isinstance(entry.get("ambient"), dict) else None
        if every is not None and (not isinstance(every, int) or isinstance(every, bool) or not 15 <= every <= 1440):
            raise ValueError("wakes.personas.ambient.every_minutes must be an integer from 15 to 1440")


def _check_patch(core, patch: dict) -> None:
    """Factory._check_wake_patch and Notifier.check_patch."""
    st = store(core, AREA)
    notify = patch.get("notify") if isinstance(patch.get("notify"), dict) else {}
    moved = sorted(set(notify) & set(CONFIG_RULE_KEYS))
    if moved and st["rules_file"] is not None:
        raise ValueError(f"notify.{moved[0]} lives in notice-rules.json now (the one rulebook): change it with POST /api/notify/rules")
    if _precondition(core)["ok"]:
        return
    current = _channel_cfg(core)
    for name, change in (notify.get("channels") or {}).items() if isinstance(notify.get("channels"), dict) else []:
        if not isinstance(change, dict):
            continue
        before = current.get(name)
        was_on = before is not None and before.get("enabled", True) is not False
        turned_on = change.get("enabled", True if before is None else was_on) is not False
        if turned_on and not was_on:
            raise ValueError(f"notify.channels.{name}: phone messages stay off while your AI agents run without the sandbox or through a "
                             "CLI driver (they could read your messages). Set harness.sandbox to bwrap and use the builtin driver, or type "
                             "the acknowledgement in Phone setup first")
    wakes = patch.get("wakes") if isinstance(patch.get("wakes"), dict) else {}
    turning_on = wakes.get("enabled") is True or (isinstance(wakes.get("chat_wakes"), dict) and wakes["chat_wakes"].get("enabled") is True)
    personas = wakes.get("personas") if isinstance(wakes.get("personas"), dict) else {}
    turning_on = turning_on or any(isinstance(e, dict) and e.get("enabled") is True for e in personas.values())
    if turning_on:
        raise ValueError("personas and chat check-ins stay off while your AI agents run without the sandbox or through a CLI driver "
                         "(they could read what you tell Seraphina). Set harness.sandbox to bwrap and use the builtin driver, or type the "
                         "acknowledgement in Phone setup first")


def _seed_tg_paired(core) -> list:
    return [{"id": 424242, "username": "owner_<b>me</b>", "first_name": "Sam <img src=x>", "paired_at": core.ago(86400 * 4)}]


def seed(core, fresh: bool) -> None:
    _install(core)
    st = store(core, AREA)
    st.update({
        "messages": {} if fresh else _seed_messages(core),
        "inbound": [] if fresh else _seed_inbound(core),
        "seq": {"message": 0 if fresh else 14, "inbound": 0 if fresh else 9, "wake": 46, "reminder": 2},
        "precondition": ({"ok": False, "sandbox": "none", "cli_driver": False, "acknowledged_at": None, "sandboxed": False} if fresh else
                         {"ok": True, "sandbox": "bwrap", "cli_driver": False, "acknowledged_at": None, "sandboxed": True}),
        "acknowledged_at": None, "paused": None, "tamper": None, "quiet_until": None, "quiet_now": False,
        "timezone_error": None, "time_sync": {"ready": True, "synced": True, "fallback_used": False},
        "today": {"phone": 0 if fresh else 2},
        "terms": [] if fresh else ["Dr. Example", "my street <b>name</b>", "insulin"], "terms_none": False,
        "chan": {}, "hook_used": {} if fresh else {"backup-script": core.ago(6000)},
        "rules_file": None,
        "queue": [] if fresh else _queue(core), "breaker": None, "wakes_paused": None,
        "stops": {} if fresh else {"mira": {"at": core.ago(1200), "by": "human", "via": "cockpit", "until": _later(5400), "kind": "mute"}},
        "pages": {} if fresh else _seed_pages(core),
        "ledger": {"dir": None, "now": None},
        "reminders": [] if fresh else [
            {"id": "R-000001", "at": _later(7200), "asked_at": _later(7200), "moved": None, "text": "Call the plumber <b>before 5</b>", "persona": "seraphina",
             "show_on": ["phone"], "created_at": core.ago(3600), "state": "pending"},
            {"id": "R-000002", "at": core.ago(86400), "asked_at": core.ago(86400), "moved": None, "text": "Renew the domain", "persona": "seraphina",
             "show_on": [], "created_at": core.ago(90000), "state": "delivered"}],
        "attested": {} if fresh else {"local": True},
        "detected": {} if fresh else {"openwebui": "openwebui"},
        "chat_optins": {} if fresh else {"c-1a2b3c4d5e": True},
        "chat_wake_rows": {} if fresh else {"c-1a2b3c4d5e": [{"wake": "W-000045", "at": core.ago(30), "moved": None, "state": "running",
                                                               "gate": None, "item": "chat:c-1a2b3c4d5e/C-2"}]},
        "polls": {},
    })
    cfg = core.STATE["config"]
    if fresh:
        return
    _seed_ledger(core)
    cfg["notify"] = {"channels": {
        "phone": {"type": "ntfy", "publish_url": "unix:/run/ntfy/ntfy.sock", "public_url": "https://box.tail1234.ts.net:8445",
                  "token_env": "SERAPHINA_NTFY_TOKEN", "reply_token_env": "SERAPHINA_NTFY_REPLY_TOKEN", "operational": True, "model_text": "clean_only"},
        "telegram": {"type": "telegram", "token_env": "SERAPHINA_TELEGRAM_BOT_TOKEN", "operational": False}}}
    cfg["wakes"] = {"enabled": True, "timezone": "Europe/London", "power": {"off": "23:00", "wake": "07:00"},
                    "personas": {"seraphina": {"enabled": True, "model": "local", "memory": {"curation": "owner"}, "wake": {"tools": "none"}},
                                 "mira": {"enabled": True, "model": "local", "memory": {"curation": "self"}, "reimport_decider": "owner"}},
                    "servers": {"strata": {"kind": "llamacpp", "root_url": "http://127.0.0.1:8080"}},
                    "hooks": {"backup-script": {"token_sha256": "ab" * 32, "may": ["event"], "events": ["backup.failed", "backup.ok"],
                                                "subjects": ["nightly"], "created": core.ago(86400 * 20)}}}
    phone = _chan_state(core, "phone")
    phone.update(installed=True, receiving=True, health={"ok": True, "detail": "healthy <b>(raw)</b>", "checked_at": core.ago(60), "last_ok": core.ago(60),
                                                         "last_sent": core.ago(290), "last_received": core.ago(120), "last_answer": core.ago(120)})
    phone["paired"] = []
    tg = _chan_state(core, "telegram")
    tg.update(receiving=True, strangers_today=3, paired=_seed_tg_paired(core),
              health={"ok": True, "detail": "bot @seraphina_box_bot answers", "checked_at": core.ago(90), "last_ok": core.ago(90),
                      "last_sent": core.ago(880), "last_received": core.ago(1800), "last_answer": core.ago(5000)})


# ---------------------------------------------------------------- the API

def _notify_state(core) -> dict:
    """Factory.notify_state: Notifier.state plus channels and hooks."""
    st = store(core, AREA)
    rules = _rules(core)
    quiet_now = bool(st["quiet_now"] or st["quiet_until"])
    window = rules["quiet_hours"]
    window_text = "22:30-08:00" if window == "auto" else window
    return {"paused": copy.deepcopy(st["paused"]), "quiet_until": st["quiet_until"],
            "quiet_hours": {"window": window_text, "source": "from your power schedule" if window == "auto" else ("off" if window is None else "set by you"),
                            "now": quiet_now, "until": _hhmm(st["quiet_until"]) if st["quiet_until"] else ("08:00" if quiet_now else None)},
            "power": "23:00-07:00" if _wakes_cfg(core).get("power") else None,
            "timezone": _wakes_cfg(core).get("timezone") or "UTC", "timezone_error": st["timezone_error"],
            "time_sync": copy.deepcopy(st["time_sync"]), "tamper": copy.deepcopy(st["tamper"]), "precondition": _precondition(core),
            "today": {"phone": st["today"]["phone"], "cap": rules["rate_limits"]["phone_max_per_day"]},
            "rules": {"source": rules["source"], "error": rules["error"], "version": rules["version"]},
            "liveness": core.ago(20),
            "sensitive_terms": {"path": "/home/me/.config/software-factory/sensitive-terms.txt", "exists": bool(st["terms"]),
                                "count": len(st["terms"]), "mode_ok": True if st["terms"] else None, "none_declared": st["terms_none"]},
            "channels": _channels(core), "hooks": _hooks(core)}


def _default_rules() -> dict:
    return {"version": 1, "quiet_hours": "auto", "quiet_breakthrough": ["backup.failed", "service.down", "disk.failing"],
            "rate_limits": {"per_persona_min_minutes": 30, "per_persona_max_per_day": 4, "personas_max_per_day": 8, "phone_max_per_day": 12, "dedupe_hours": 24},
            "boundary": {"third_party": "short_only", "tailnet_model_text": "clean_only", "phone_short_until_profile_confirmed": True, **FLOORS,
                         "force_short_on": list(FORCE_SHORT)},
            "confirmed_profiles": {}, "audit": copy.deepcopy(AUDIT)}


def _rules(core) -> dict:
    """rules.load_rules: the file, else config.json's notify keys."""
    st = store(core, AREA)
    if st["rules_file"] is not None:
        return {**copy.deepcopy(st["rules_file"]), "source": "file", "error": None}
    rules = _default_rules()
    notify = _notify_cfg(core)
    if "quiet_hours" in notify:
        rules["quiet_hours"] = notify["quiet_hours"]
    if isinstance(notify.get("quiet_breakthrough"), list):
        rules["quiet_breakthrough"] = list(notify["quiet_breakthrough"])
    for key in ("personas_max_per_day", "phone_max_per_day", "dedupe_hours"):
        if isinstance(notify.get(key), int):
            rules["rate_limits"][key] = notify[key]
    confirmed = {name: {"sha256": _sha(p["file"]), "what": "soul", "at": "2026-10-20T09:00:00Z"} for name, p in st["pages"].items() if p["privacy"] == "clean"}
    rules["confirmed_profiles"] = confirmed
    return {**rules, "source": "config", "error": None}


def _validate_rules(doc: dict) -> dict:
    """rules.validate_rules: stricter only, unknown keys refused."""
    out = _default_rules()
    unknown = sorted(set(doc) - set(out))
    if unknown:
        raise ValueError(f"{unknown[0]} is not a key of the rules file (version 1)")
    quiet = doc.get("quiet_hours", out["quiet_hours"])
    if quiet is not None and quiet != "auto" and not (isinstance(quiet, str) and re.fullmatch(r"\d{2}:\d{2}-\d{2}:\d{2}", quiet)):
        raise ValueError('quiet_hours must be "auto", "HH:MM-HH:MM" or null')
    out["quiet_hours"] = quiet
    breakthrough = doc.get("quiet_breakthrough", out["quiet_breakthrough"])
    if not isinstance(breakthrough, list) or not all(isinstance(x, str) and x in RULE_IDS for x in breakthrough):
        raise ValueError("quiet_breakthrough must be a list of rule ids")
    out["quiet_breakthrough"] = list(breakthrough)
    limits = doc.get("rate_limits", {})
    if not isinstance(limits, dict) or set(limits) - set(RATE_KEYS):
        raise ValueError("rate_limits must be an object of " + ", ".join(RATE_KEYS))
    for key, (low, high) in RATE_KEYS.items():
        if key in limits:
            value = limits[key]
            if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
                raise ValueError(f"rate_limits.{key} must be an integer from {low} to {high}")
            out["rate_limits"][key] = value
    boundary = doc.get("boundary", {})
    if not isinstance(boundary, dict) or set(boundary) - set(out["boundary"]):
        raise ValueError("boundary has an unknown key")
    if boundary.get("third_party", "short_only") != "short_only":
        raise ValueError('boundary.third_party is always "short_only": a third party only ever gets fixed heads-ups')
    if boundary.get("tailnet_model_text", "clean_only") not in ("clean_only", "never"):
        raise ValueError('boundary.tailnet_model_text must be "clean_only" or "never"')
    out["boundary"]["tailnet_model_text"] = boundary.get("tailnet_model_text", "clean_only")
    if boundary.get("phone_short_until_profile_confirmed", True) is not True:
        raise ValueError("boundary.phone_short_until_profile_confirmed is always true (the shared boundary)")
    for key, floor in FLOORS.items():
        if key in boundary:
            value = boundary[key]
            if not isinstance(value, int) or isinstance(value, bool) or value < 1 or value > floor:
                raise ValueError(f"boundary.{key} must be an integer from 1 to {floor} (stricter, never weaker)")
            out["boundary"][key] = value
    out["confirmed_profiles"] = copy.deepcopy(doc.get("confirmed_profiles") or {})
    if "audit" in doc and doc["audit"] != AUDIT:
        raise ValueError("audit describes the harness's audit line format; it cannot be changed")
    return out


def _new_message(core, *, kind: str, klass: str, title: str, body: str, channels=None, priority: str = "default", sender: str = "server",
                 persona: str | None = None, read: bool = False, why: str = "Sent in full") -> dict:
    st = store(core, AREA)
    st["seq"]["message"] += 1
    n = st["seq"]["message"]
    names = list(channels if channels is not None else _channel_cfg(core))
    entry = _entry(core, n, kind=kind, klass=klass, title=title, body=body, sender=sender, persona=persona, priority=priority, read=read, age=0,
                   deliveries={name: _delivery(core, "sent", title=title, text=body, why=why, age=0, buttons=("ack",)) for name in names})
    st["messages"][entry["id"]] = entry
    core.BUS.publish("notify.queued", {"message": entry["id"], "kind": kind, "class": klass, "sender": sender, "persona": persona,
                                       "priority": priority, "route": entry["route"], "taint": [], "agent": _display(core, persona)})
    for name in names:
        core.BUS.publish("notify.sent", {"message": entry["id"], "channel": name})
    return entry


def _record_inbound(core, row: dict) -> dict:
    st = store(core, AREA)
    st["seq"]["inbound"] += 1
    row = {"id": f"I-{st['seq']['inbound']:06d}", "at": core.now(), **row}
    st["inbound"].insert(0, row)
    return row


def _channel_or_404(core, name) -> dict:
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", name):
        raise core.Invalid("invalid channel name")
    entry = _channel_cfg(core).get(name)
    if entry is None:
        raise KeyError(f"no channel {name!r} (notify.channels)")
    return entry


def _adopt_wake_personas(core) -> None:
    """A wake persona imported since the seed (POST /api/personas/import with a name, answered by the Personas area's
    mock) gets her page here, as new as WakeManager.persona serves it: her soul from the import, no memory, journal,
    thread, runs or inbox yet, private. One persona file serves both areas on the real server."""
    st = store(core, AREA)
    others = (core.STATE.get("area") or {}).get("personas") or {}
    for name, row in list((others.get("personas") or {}).items()):
        if name in st["pages"] or not isinstance(row, dict) or row.get("kind") != "wake":
            continue
        src = row.get("source") if isinstance(row.get("source"), dict) else {}
        display = str(row.get("instance_name") or name)
        soul = f"You are {display}."
        file = _persona_file(name, str(row.get("description") or display), soul,
                             {"source": "openwebui", "source_model": src.get("model"), "source_version_id": src.get("version_id"),
                              "imported_at": src.get("imported_at")} if src else None)
        st["pages"][name] = {
            "display": display, "description": str(row.get("description") or display), "privacy": "private", "integrity": "ok",
            "file": file, "approved": file, "soul": soul, "diff": None, "path": str(row.get("path") or f"/home/me/.config/software-factory/personas/{name}.md"),
            "source": {"type": "openwebui", "model": src.get("model"), "version_id": src.get("version_id")} if src else None,
            "imported_at": src.get("imported_at") if src else None, "copy_label": None, "role": None,
            "budget": {"model_wakes": 0, "max_model_wakes": 24, "slot_seconds": 0.0, "max_slot_seconds": 900, "notices": 0},
            "last": [], "memory_text": "", "memory_version": 0, "memory_updated_at": None, "memory_by": None, "clean_versions": [], "versions": {},
            "writes": [], "journal": [], "runs": [], "run_lines": {}, "thread": [], "consolidation": None, "inbox": [], "reimport": None,
        }


def api(core, handler, method, parts, query, body):
    if not parts:
        return NotImplemented
    head = parts[0]
    if head in ("personas", "wakes"):
        _adopt_wake_personas(core)
    if head == "messages":
        return _api_messages(core, handler, method, parts[1:], query, body)
    if head == "notify":
        return _api_notify(core, handler, method, parts[1:], query, body)
    if head == "wakes":
        return _api_wakes(core, handler, method, parts[1:], query, body)
    if head == "personas" and len(parts) >= 2 and parts[1] in store(core, AREA)["pages"]:
        return _api_persona(core, handler, method, parts[1], parts[2:], query, body)
    if head == "chats" and len(parts) == 3 and parts[2] == "wakes":
        return _api_chat_wakes(core, handler, method, parts[1], body)
    return NotImplemented


def _api_messages(core, handler, method, rest, query, body):
    st = store(core, AREA)
    if method == "GET" and not rest:
        entries = sorted(st["messages"].values(), key=lambda e: e["id"], reverse=True)
        unread = sum(1 for e in entries if not e.get("read"))
        rows = []
        limit = max(1, min(int(query.get("limit") or 100), 500))
        for e in entries:
            if query.get("before") and e["id"] >= query["before"]:
                continue
            if query.get("persona") and e.get("persona") != query["persona"]:
                continue
            if query.get("kind") and e.get("kind") != query["kind"]:
                continue
            if query.get("channel") and query["channel"] not in (e.get("deliveries") or {}):
                continue
            if query.get("state") and _overall(e) != query["state"] and query["state"] not in {d.get("state") for d in e["deliveries"].values()}:
                continue
            if query.get("unread") not in (None, "", "0") and e.get("read"):
                continue
            rows.append(_public(e))
            if len(rows) >= limit:
                break
        return handler.send_json({"messages": rows, "unread": unread})
    if not rest or not re.fullmatch(r"M-\d{6,9}", rest[0]):
        raise core.Invalid("invalid message id")
    entry = st["messages"].get(rest[0])
    if entry is None:
        raise KeyError(f"message {rest[0]}")
    if method == "GET" and len(rest) == 1:
        public = _public(entry)
        public["fields"] = copy.deepcopy(entry.get("fields") or {})
        deliveries = [{"channel": name, "rendered": copy.deepcopy(d.get("rendered")), "why": d.get("why"), "state": d.get("state"), "at": d.get("at"),
                       "error": d.get("error"), "attempts": d.get("attempts"), "next_try": d.get("next_try"),
                       "partial": bool(d.get("partial"))} for name, d in entry["deliveries"].items()]
        inbound = [copy.deepcopy(r) for r in st["inbound"] if r.get("message") == entry["id"]]
        return handler.send_json({"message": public, "deliveries": deliveries, "inbound": inbound})
    if method == "POST" and len(rest) == 2 and rest[1] in ("ack", "snooze", "retry", "resend"):
        action = rest[1]
        minutes = (body or {}).get("minutes")
        if minutes is not None and (isinstance(minutes, bool) or not isinstance(minutes, int)):
            raise core.Invalid("'minutes' must be a whole number")
        if action == "ack":
            if not entry.get("read"):
                entry.update(read=True, read_at=core.now())
                core.BUS.publish("notify.acked", {"message": entry["id"], "by": "human", "via": "cockpit"})
        elif action == "snooze":
            minutes = 60 if minutes is None else minutes
            if not 1 <= minutes <= 1440:
                raise core.Invalid("minutes must be a whole number from 1 to 1440 (24 hours)")
            until = _later(minutes * 60)
            entry["snoozed_until"] = until
            for d in entry["deliveries"].values():
                if d.get("state") in ("sent", "unknown", "held"):
                    d.update(state="snoozed", next_try=until, attempts=0)
        else:
            wanted = ("failed", "held") if action == "retry" else ("unknown",)
            for name, d in entry["deliveries"].items():
                if d.get("state") in wanted:
                    d.update(state="queued", next_try=core.now(), attempts=0)
                    core.BUS.publish("notify.queued", {"message": entry["id"], "kind": entry["kind"], "class": entry["class"], "sender": entry["sender"],
                                                       "persona": entry["persona"], "priority": entry["priority"], "route": entry["route"],
                                                       "taint": entry["taint"], "updated": True})
        return handler.send_json({"message": _public(entry)})
    return NotImplemented


def _api_notify(core, handler, method, rest, query, body):
    st = store(core, AREA)
    body = body if isinstance(body, dict) else {}
    if method == "GET" and not rest:
        return handler.send_json(_notify_state(core))
    if rest == ["channels"] and method == "GET":
        return handler.send_json({"channels": _channels(core)})
    if rest == ["test"] and method == "POST":
        _channel_or_404(core, body.get("channel"))
        entry = _new_message(core, kind="test", klass="short", title="Test", body="This is a test from your server. Tap Got it.",
                             channels=[body["channel"]], read=False)
        _chan_state(core, body["channel"])["test_sent"] = True
        return handler.send_json({"message": entry["id"]}, status=202)
    if rest == ["ntfy", "prepare"] and method == "POST":
        entry = _channel_or_404(core, body.get("channel"))
        if entry.get("type") != "ntfy":
            raise core.Invalid("prepare is for an ntfy channel")
        if not _chan_state(core, body["channel"]).get("installed"):
            raise core.Invalid(NOT_INSTALLED)
        return handler.send_json({"copy_blocks": _copy_blocks(entry), "passphrase": PASSPHRASE, "setup_dir": "/srv/ai-work/factory/notify/ntfy-setup"})
    if rest == ["ntfy", "check"] and method == "GET":
        name = query.get("channel")
        entry = _channel_or_404(core, name)
        if entry.get("type") != "ntfy":
            raise core.Invalid("check is for an ntfy channel")
        chan = _chan_state(core, name)
        chan["checks"] = chan.get("checks", 0) + 1
        installed = chan.get("installed")
        ok = bool(installed)
        topics = (entry.get("topic") or "seraphina", entry.get("taps_topic") or "seraphina-taps", entry.get("chat_topic") or "seraphina-chat")
        return handler.send_json({"health": {"ok": ok, "detail": "healthy" if ok else "cannot reach unix:/run/ntfy/ntfy.sock <b>(raw)</b>"},
                                  "acl_ok": ok, "reads": {t: (200 if ok else None) for t in topics}, "tiers_ok": None, "socket": ok, "serve": ok,
                                  "funnel_off": True, "tcp_listener": bool(ok and chan.get("tcp_listener")),
                                  "port80_other": bool(ok and not chan.get("tcp_listener")), "privacy": _privacy(entry),
                                  "install_block": _install_block()})
    if rest == ["pair"]:
        name = body.get("channel") if method == "POST" else query.get("channel")
        entry = _channel_or_404(core, name)
        chan = _chan_state(core, name)
        if method == "POST":
            if _notify_cfg(core).get("enabled") is False:
                raise core.Invalid("Phone messages are turned off (notify.enabled): turn them on before pairing")
            if not _precondition(core)["ok"]:
                raise core.Invalid("Phone messages are off while your AI agents run without the sandbox: turn on bwrap (Settings, Harness) or "
                                   "accept the risk first")
            st["polls"][name] = 0
            if entry["type"] == "telegram":
                chan["pair"] = {"state": "waiting", "tries": 0}
                return handler.send_json({"code": "Kq7mZpX2rT9w", "link": "https://t.me/seraphina_box_bot?start=Kq7mZpX2rT9w" if chan.get("bot_known", True) else None,
                                          "text": "/start Kq7mZpX2rT9w", "expires_at": _later(600)})
            chan["pair"] = {"state": "waiting"}
            return handler.send_json({"server_url": entry.get("public_url") or "https://<host>.<tailnet>.ts.net:8445",
                                      "topics": {"notices": entry.get("topic") or "seraphina", "taps": entry.get("taps_topic") or "seraphina-taps",
                                                 "chat": entry.get("chat_topic") or "seraphina-chat"}, "username": "owner-phone"})
        st["polls"][name] = st["polls"].get(name, 0) + 1
        pair = chan.get("pair")
        if entry["type"] == "ntfy":   # NtfyChannel.pair_status: paired once the phone ever answered (health.last_answer)
            if chan.get("test_sent") and st["polls"][name] >= 2:
                chan["health"]["last_answer"] = core.now()
                chan["test_sent"] = False
            last = chan["health"].get("last_answer")
            return handler.send_json({"state": "paired" if last else "waiting", "account": {"id": "owner-phone"} if last else None})
        if not pair:
            return handler.send_json({"state": "waiting", "account": None})
        if pair["state"] == "waiting" and st["polls"][name] >= 2:
            pair["state"] = "confirm"
        if pair["state"] == "confirm":
            return handler.send_json({"state": "confirm", "account": {"id": 515151, "username": "new_<i>phone</i>", "first_name": "Sam"}})
        if pair["state"] == "paired":
            return handler.send_json({"state": "paired", "account": {"id": 515151, "username": "new_<i>phone</i>", "first_name": "Sam"}})
        return handler.send_json({"state": pair["state"], "account": None})
    if rest == ["pair", "confirm"] and method == "POST":
        name = body.get("channel")
        entry = _channel_or_404(core, name)
        code = body.get("code")
        if not isinstance(code, str) or not re.fullmatch(r"\s*\d{6}\s*", code):
            raise core.Invalid("the code is the 6 digits your bot sent you")
        chan = _chan_state(core, name)
        pair = chan.get("pair") or {}
        if entry["type"] != "telegram" or pair.get("state") != "confirm":
            return handler.send_json({"state": "expired", "account": None})
        if code.strip() != TELEGRAM_CODE:
            pair["tries"] = pair.get("tries", 0) + 1
            if pair["tries"] >= 3:
                pair["state"] = "expired"
                return handler.send_json({"state": "expired", "account": None})
            return handler.send_json({"state": "confirm", "account": None, "wrong": True})
        account = {"id": 515151, "username": "new_<i>phone</i>", "first_name": "Sam", "paired_at": core.now()}
        chan["paired"] = [a for a in chan["paired"] if a["id"] != account["id"]] + [account]
        pair["state"] = "paired"
        core.BUS.publish("inbound.received", {"channel": name, "kind": "pairing", "verb": None, "persona": None, "outcome": "paired"})
        return handler.send_json({"state": "paired", "account": account})
    if rest == ["unpair"] and method == "POST":
        name = body.get("channel")
        _channel_or_404(core, name)
        account = body.get("chat_id")
        if isinstance(account, bool) or not isinstance(account, (int, str)):
            raise core.Invalid("'chat_id' must be the paired account's numeric id")
        chan = _chan_state(core, name)
        if not any(str(a["id"]) == str(account) for a in chan["paired"]):
            raise KeyError(f"no paired account {account} on {name}")
        chan["paired"] = [a for a in chan["paired"] if str(a["id"]) != str(account)]
        return handler.send_json({"channel": name, "paired": copy.deepcopy(chan["paired"])})
    if len(rest) == 3 and rest[0] == "channels" and rest[2] in ("rotate", "leaked", "disable") and method == "POST":
        name, action = rest[1], rest[2]
        entry = _channel_or_404(core, name)
        if action == "disable":
            entry["enabled"] = False
            return handler.send_json({"channel": name, "enabled": False})
        if entry["type"] == "telegram":
            token = body.get("token")
            if not isinstance(token, str) or not token.strip():
                raise core.Invalid("paste the new bot token from @BotFather (/revoke made the old one useless)")
            _chan_state(core, name)["paired"] = []
            return handler.send_json({"channel": name, "paired": [], "next": "Pair your phone again."})
        _chan_state(core, name)["test_sent"] = False   # the old reply token no longer works: no answer until the phone signs in again
        return handler.send_json({"channel": name, "copy_blocks": _copy_blocks(entry), "passphrase": PASSPHRASE,
                                  "setup_dir": "/srv/ai-work/factory/notify/ntfy-setup"})
    if rest == ["lost-phone"] and method == "POST":
        done, blocks, passphrase = [], [], None
        for name, entry in _channel_cfg(core).items():
            chan = _chan_state(core, name)
            if entry["type"] == "telegram":
                count = len(chan["paired"])
                chan["paired"] = []
                done.append(f"unpaired {count} Telegram account{'s' if count != 1 else ''} on {name}")
            else:
                entry["enabled"] = False
                chan["test_sent"] = False
                done.append(f"stopped publishing to {name}")
                blocks += _copy_blocks(entry)
                passphrase = PASSPHRASE
                done.append(f"made new phone credentials for {name}")
        st["paused"] = st["paused"] or {"at": core.now(), "by": "human", "via": "lost my phone"}
        core.BUS.publish("wake.paused", {"scope": "all", "by": "human", "until": None})
        done.append("stopped Seraphina")
        return handler.send_json({"done": done, "copy_blocks": blocks, "passphrase": passphrase,
                                  "next": "On the new phone, add the user owner-phone with the new passphrase, then turn the private channel on again in Phone setup."})
    if rest == ["events"] and method == "POST":
        kind = body.get("kind")
        if kind not in HOOK_KINDS:
            raise core.Invalid(f"kind must be one of {', '.join(HOOK_KINDS)}")
        unknown = sorted(set(body) - {"kind", "subject", "detail", "dedupe_key"})
        if unknown:
            raise core.Invalid(f"unknown field {unknown[0]!r}")
        subject = body.get("subject")
        if subject is not None and (not isinstance(subject, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,40}", subject)):
            raise core.Invalid("subject must look like nightly (a-z, 0-9, '.', '_', '-'; at most 41 characters)")
        if kind in ("backup.ok", "service.up"):
            return handler.send_json({"message": None, "recorded": True}, status=202)
        open_key = f"{kind}:{subject or '-'}"
        if open_key in st.setdefault("incidents", set()):
            return handler.send_json({"message": None, "deduped": True}, status=202)
        st["incidents"].add(open_key)
        entry = _new_message(core, kind=kind, klass="operational", title=f"{kind} {subject or ''}".strip(), body=f"{kind} reported by you", priority="high")
        return handler.send_json({"message": entry["id"]}, status=202)
    if rest == ["terms"]:
        status = {"path": "/home/me/.config/software-factory/sensitive-terms.txt", "exists": bool(st["terms"]), "count": len(st["terms"]),
                  "mode_ok": True if st["terms"] else None, "none_declared": st["terms_none"]}
        if method == "GET":
            return handler.send_json({**status, "terms": list(st["terms"])})
        if body.get("none") is True:
            st["terms_none"] = True
        else:
            if not _precondition(core)["ok"]:
                raise core.Invalid(SANDBOX_TERMS)
            text = body.get("text") or ""
            if not isinstance(text, str) or len(text) > 64 * 1024:
                raise core.Invalid("text must be the list of words, one per line (at most 64 KB)")
            st["terms"] = [" ".join(line.split()) for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
            st["terms_none"] = False
        return handler.send_json({"path": status["path"], "exists": bool(st["terms"]), "count": len(st["terms"]),
                                  "mode_ok": True if st["terms"] else None, "none_declared": st["terms_none"]})
    if rest == ["inbound"] and method == "GET":
        kind = query.get("kind")
        if kind is not None and not re.fullmatch(r"[a-z]{2,12}", kind):
            raise core.Invalid("invalid kind")
        limit = min(int(query.get("limit") or 100), 1000)
        rows = [copy.deepcopy(r) for r in st["inbound"] if kind is None or r.get("kind") == kind]
        return handler.send_json({"inbound": rows[:limit]})
    if len(rest) == 3 and rest[0] == "inbound" and rest[2] == "apply" and method == "POST":
        inbound_id = rest[1]
        if not re.fullmatch(r"I-\d{6,9}", inbound_id):
            raise core.Invalid("inbound id must look like I-000012")
        record = next((r for r in st["inbound"] if r["id"] == inbound_id), None)
        if record is None:
            raise KeyError(f"inbound {inbound_id}")
        if not record.get("late") or not record.get("pending_verb"):
            raise core.Invalid("only a late command or tap that was not applied can be applied now")
        if any(r.get("applies") == inbound_id for r in st["inbound"]):
            raise core.Invalid("it was applied already")
        verb = record["pending_verb"]
        _record_inbound(core, {"kind": "cockpit", "channel": record["channel"], "verb": verb, "outcome": "applied from the cockpit",
                               "applies": inbound_id, "message": record.get("message"), "by": "human"})
        if verb.startswith("quiet"):
            st["quiet_until"] = _later(3 * 3600)
        return handler.send_json({"applied": verb, "answer": "Quiet until 08:00. Alerts from your server still come."})
    if rest == ["hooks"]:
        if method == "GET":
            return handler.send_json({"hooks": _hooks(core)})
        name = body.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", name):
            raise core.Invalid("name must look like backup-script (a-z, 0-9 and '-', starting with a letter)")
        hooks = core.STATE["config"].setdefault("wakes", {}).setdefault("hooks", {})
        if name in hooks:
            raise core.Invalid(f"an event source named {name} exists: rotate its token or delete it first")
        unknown = sorted(set(body) - {"name", "may", "events", "subjects", "personas", "per_minute", "per_day"})
        if unknown:
            raise core.Invalid(f"unknown field {unknown[0]!r}")
        spec = {k: body[k] for k in ("may", "events", "subjects", "personas", "per_minute", "per_day") if body.get(k) is not None}
        spec.setdefault("may", ["event"])
        spec.setdefault("events", ["backup.failed", "backup.ok"])
        if not isinstance(spec["may"], list) or not set(spec["may"]) <= set(HOOK_MAY):
            raise core.Invalid(f"wakes.hooks.{name}.may must be a list of " + ", ".join(HOOK_MAY))
        if not all(k in HOOK_KINDS for k in spec["events"]):
            raise core.Invalid("events must be a list of " + ", ".join(HOOK_KINDS))
        spec.update(token_sha256=_sha(name + "-token"), created=core.now())
        hooks[name] = spec
        token = "hk_" + _sha(name)[:40]
        return handler.send_json({"hook": next(h for h in _hooks(core) if h["name"] == name), "token": token, "snippets": _hook_snippets(name, spec)}, status=201)
    if len(rest) == 3 and rest[0] == "hooks" and rest[2] in ("rotate", "delete") and method == "POST":
        hooks = _wakes_cfg(core).get("hooks") or {}
        name = rest[1]
        if name not in hooks:
            raise KeyError(f"event source {name}")
        if rest[2] == "delete":
            del hooks[name]
            return handler.send_json({"hook": None, "deleted": name})
        hooks[name]["rotated"] = core.now()
        return handler.send_json({"hook": next(h for h in _hooks(core) if h["name"] == name), "token": "hk_" + _sha(name + core.now())[:40],
                                  "snippets": _hook_snippets(name, hooks[name])})
    if rest == ["rules"]:
        if method == "GET":
            return handler.send_json(_rules(core))
        if "confirmed_profiles" in body:
            raise core.Invalid("send the rules to change; confirmations come from a persona's page")
        current = {k: v for k, v in _rules(core).items() if k not in ("source", "error")}
        for key, value in body.items():
            current[key] = {**current[key], **value} if isinstance(value, dict) and isinstance(current.get(key), dict) else value
        try:
            st["rules_file"] = _validate_rules(current)
        except ValueError as exc:
            raise core.Invalid(str(exc)) from None
        return handler.send_json(_rules(core))
    return NotImplemented


def _wakes_state(core) -> dict:
    """WakeManager.state."""
    st = store(core, AREA)
    rules = _rules(core)
    window = rules["quiet_hours"]
    servers = {}
    for name in sorted(set(st["attested"]) | set((_wakes_cfg(core).get("servers") or {})) | set(st["detected"])):
        entry = dict((_wakes_cfg(core).get("servers") or {}).get(name) or {})
        provider = (core.STATE["config"].get("providers") or {}).get(name) or {}
        root = entry.get("root_url") or str(provider.get("base_url") or "http://127.0.0.1:3000").rstrip("/").removesuffix("/v1")
        servers[name] = {**entry, "kind": entry.get("kind") or "other", "root_url": root, "local_attested": bool(st["attested"].get(name)),
                         "detected": st["detected"].get(name)}
    return {"enabled": _wakes_cfg(core).get("enabled") is True, "paused": copy.deepcopy(st["paused"]),
            "power": "23:00-07:00" if _wakes_cfg(core).get("power") else None,
            "quiet_hours": {"window": "22:30-08:00" if window == "auto" else window,
                            "source": "from your power schedule" if window == "auto" else "set by you"},
            "personas": [_persona_row(core, name) for name in sorted(st["pages"])],
            "queue": copy.deepcopy(st["queue"]), "chat_wakes": copy.deepcopy([e for e in st["queue"] if e.get("chat")]),
            "breaker": copy.deepcopy(st["breaker"]), "tick_at": core.ago(4), "time_synced": bool(st["time_sync"]["ready"]),
            "servers": servers, "instance_name": "Seraphina OS Harness", "reminders": copy.deepcopy(st["reminders"])}


def _api_wakes(core, handler, method, rest, query, body):
    st = store(core, AREA)
    body = body if isinstance(body, dict) else {}
    if not rest:
        if method == "GET":
            return handler.send_json(_wakes_state(core))
        persona = body.get("persona")
        if persona not in st["pages"]:
            raise KeyError(f"unknown persona {persona!r}")
        st["seq"]["wake"] += 1
        wake = {"id": f"W-{st['seq']['wake']:06d}", "persona": persona, "chat": None, "source": "run_now", "reason_sha256": None, "reason_len": 0,
                "taint": [], "dedupe_key": f"run_now:{persona}", "requested_by": "human", "requested_at": core.now(), "not_before": core.now(),
                "moved": None, "chain": 0, "state": "pending", "attempts": 0, "yields": 0, "run": None, "item": None}
        st["queue"].append(wake)
        core.BUS.publish("wake.requested", {"wake": wake["id"], "persona": persona, "chat": None, "source": "run_now", "not_before": wake["not_before"],
                                            "dedupe_key": wake["dedupe_key"], "moved": None, "reason_len": 0, "reason_sha256": None}, agent=_display(core, persona))
        return handler.send_json({"wake": wake}, status=202)
    if rest == ["control"] and method == "POST":
        action = body.get("action")
        if action not in ("pause", "resume", "mute", "unmute"):
            raise core.Invalid("action must be pause, resume, mute or unmute")
        persona = body.get("persona")
        until = body.get("until")
        if persona is None:
            if action == "pause":
                st["paused"] = st["paused"] or {"at": core.now(), "by": "human", "via": "cockpit"}
                core.BUS.publish("wake.paused", {"scope": "all", "by": "human", "until": None})
            elif action == "resume":
                st["paused"] = None
                st["breaker"] = None
                core.BUS.publish("wake.resumed", {"scope": "all", "by": "human", "until": None})
        else:
            if persona not in st["pages"]:
                raise core.Invalid("invalid persona name")
            if action in ("pause", "mute"):
                end = None
                if until:
                    if re.fullmatch(r"\d{2}:\d{2}", str(until)):
                        end = _later(3600)
                    elif re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?Z?", str(until)):
                        end = str(until)
                    else:
                        raise core.Invalid("until must be HH:MM in your time zone or a time like 2026-10-21T18:00:00Z")
                st["stops"][persona] = {"at": core.now(), "by": "human", "via": "cockpit", "until": end, "kind": "stop" if action == "pause" else "mute"}
                core.BUS.publish("wake.paused", {"scope": persona, "by": "human", "until": end, "kind": action}, agent=_display(core, persona))
            else:
                if (st["stops"].get(persona) or {}).get("by") == "integrity":
                    raise core.Invalid("her instructions changed outside her page: review the change first (approve or put the old text back)")
                st["stops"].pop(persona, None)
                core.BUS.publish("wake.resumed", {"scope": persona, "by": "human", "until": None, "kind": action}, agent=_display(core, persona))
        return handler.send_json({"paused": copy.deepcopy(st["paused"]), "personas": copy.deepcopy(st["stops"])})
    if rest == ["acknowledge"] and method == "POST":
        if body.get("withdraw") is True:
            st["acknowledged_at"] = None
        else:
            text = body.get("text")
            if not isinstance(text, str) or " ".join(text.split()).rstrip(".") != ACK_SENTENCE:
                raise core.Invalid(f"type exactly: {ACK_SENTENCE}")
            st["acknowledged_at"] = core.now()
        return handler.send_json({"acknowledged_at": st["acknowledged_at"]})
    if len(rest) == 2 and rest[0] == "review" and rest[1] in ("accept", "restart") and method == "POST":
        st["tamper"] = None
        if rest[1] == "restart":
            st["paused"] = None
            core.BUS.publish("wake.resumed", {"scope": "all", "by": "human", "until": None})
        return handler.send_json({"tamper": None, "paused": copy.deepcopy(st["paused"])})
    if rest == ["reminders"]:
        if method == "GET":
            return handler.send_json({"reminders": copy.deepcopy(st["reminders"])})
        at = body.get("at")
        if not isinstance(at, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})", at):
            raise core.Invalid("at must be a time like 2026-10-21T18:00:00Z")
        text = body.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 300:
            raise core.Invalid("a reminder needs text, at most 300 characters")
        show_on = body.get("show_on") or []
        if not isinstance(show_on, list) or not all(isinstance(x, str) and re.fullmatch(r"[a-z][a-z0-9-]{0,31}", x) for x in show_on):
            raise core.Invalid("'show_on' must be a list of channel names")
        third = {r["name"] for r in _channels(core) if r["privacy"] != "tailnet"}
        bad = [n for n in show_on if n in third or n not in _channel_cfg(core)]
        if bad:
            raise core.Invalid(f"show_on: {bad[0]} is not one of your private channels (a third-party channel only ever gets \"A reminder you set is due.\")")
        st["seq"]["reminder"] += 1
        hour = int(at[11:13])
        moved = "your server is off 23:00-07:00 then: it reaches you after 07:00, marked late" if hour >= 23 or hour < 7 else None
        reminder = {"id": f"R-{st['seq']['reminder']:06d}", "at": at, "asked_at": at, "moved": moved, "text": text.strip(),
                    "persona": body.get("persona") or "seraphina", "show_on": show_on, "created_at": core.now(), "state": "pending"}
        st["reminders"].append(reminder)   # WakeManager.add_reminder writes the audit only: no bus event
        return handler.send_json({"reminder": copy.deepcopy(reminder)}, status=201)
    if len(rest) == 3 and rest[0] == "reminders" and rest[2] == "cancel" and method == "POST":
        if not re.fullmatch(r"R-\d{6,9}", rest[1]):
            raise core.Invalid("a reminder id looks like R-000012")
        reminder = next((r for r in st["reminders"] if r["id"] == rest[1]), None)
        if reminder is None:
            raise KeyError(f"reminder {rest[1]}")
        if reminder["state"] == "pending":
            reminder["state"] = "cancelled"
        return handler.send_json({"reminder": copy.deepcopy(reminder)})
    if len(rest) == 3 and rest[0] == "servers" and rest[2] == "attest" and method == "POST":
        local = body.get("local")
        if not isinstance(local, bool):
            raise core.Invalid("'local' must be true or false")
        st["attested"][rest[1]] = local
        server = _wakes_state(core)["servers"].get(rest[1]) or {"kind": "other", "root_url": ""}
        return handler.send_json({"server": {"provider": rest[1], **{k: v for k, v in server.items() if k != "detected"}, "local_attested": local}})
    if len(rest) == 2 and rest[1] == "cancel" and method == "POST":
        if not re.fullmatch(r"W-\d{6,9}", rest[0]):
            raise core.Invalid("a wake id looks like W-000212")
        entry = next((e for e in st["queue"] if e["id"] == rest[0]), None)
        if entry is None:
            raise KeyError(f"wake {rest[0]}")
        st["queue"].remove(entry)
        core.BUS.publish("wake.skipped", {"wake": entry["id"], "gate": "cancelled", "detail": "cancelled by human"}, agent=_display(core, entry.get("persona")))
        return handler.send_json({"wake": {**entry, "state": "cancelled"}})
    return NotImplemented


def _updated(core, name: str, what: str) -> None:
    core.BUS.publish("persona.updated", {"persona": name, "what": what}, agent=_display(core, name))


def _api_persona(core, handler, method, name, rest, query, body):
    st = store(core, AREA)
    p = st["pages"][name]
    body = body if isinstance(body, dict) else {}
    row = lambda: handler.send_json({"persona": _persona_row(core, name)})   # noqa: E731
    if method == "GET" and not rest:
        return handler.send_json(_persona_page(core, name))
    if rest == ["soul"] and method == "POST":
        text, base = body.get("text"), body.get("base_sha256")
        if not isinstance(text, str) or not isinstance(base, str):
            raise core.Invalid("'text' and 'base_sha256' are required")
        if base != _sha(p["file"]):
            raise core.Conflict("the instructions changed since you opened them: reload and edit again")
        if not text.strip():
            raise core.Invalid("a persona needs a soul (its instructions)")
        p["soul"] = text.strip()
        p["file"] = _persona_file(name, p["description"], text, {k: v for k, v in {"source": "openwebui" if p["source"] else None}.items() if v})
        p.update(approved=p["file"], integrity="ok", privacy="private", diff=None)
        if (st["stops"].get(name) or {}).get("by") == "integrity":
            st["stops"].pop(name)
        _updated(core, name, "soul")
        return row()
    if rest == ["privacy"] and method == "POST":
        what, ref, clean = body.get("what"), body.get("ref"), body.get("clean", True)
        if not isinstance(clean, bool):
            raise core.Invalid("'clean' must be true or false")
        if what == "soul":
            if ref != _sha(p["file"]):
                raise core.Conflict("the instructions changed since you read them: reload and confirm again")
            p["privacy"] = "clean" if clean else "private"
        elif what == "memory":
            if isinstance(ref, bool) or not isinstance(ref, int):
                raise core.Invalid("ref must be the memory version")
            if ref not in p["versions"] and ref != p["memory_version"]:
                raise KeyError(f"memory version {ref}")
            p["clean_versions"] = sorted(set(p["clean_versions"]) | {ref}) if clean else [v for v in p["clean_versions"] if v != ref]
        else:
            raise core.Invalid("what must be soul or memory")
        _updated(core, name, "privacy")
        return row()
    if len(rest) == 2 and rest[0] == "integrity" and rest[1] in ("approve", "restore") and method == "POST":
        if rest[1] == "restore":
            p["file"] = p["approved"]
            p["soul"] = p["file"].split("---\n", 2)[-1].strip()
        p.update(approved=p["file"], integrity="ok", diff=None)
        if (st["stops"].get(name) or {}).get("by") == "integrity":
            st["stops"].pop(name)
        _updated(core, name, "soul")
        return row()
    if rest == ["memory"] and method == "POST":
        base, clean, text = body.get("base_version"), body.get("clean", False), body.get("text")
        if isinstance(base, bool) or not isinstance(base, int) or not isinstance(clean, bool):
            raise core.Invalid("'base_version' must be the version you edited, 'clean' true or false")
        if not isinstance(text, str):
            raise core.Invalid("'text' must be a string")
        if base != p["memory_version"]:
            raise core.Conflict(f"the memory summary changed since you opened it (now version {p['memory_version']}): reload it and edit again")
        _save_memory(core, p, text, "owner", "edited by the owner", clean=clean)
        _updated(core, name, "memory")
        return handler.send_json({"memory": {**_memory_view(p), "write": p["writes"][-1]}})
    if rest == ["memory", "rollback"] and method == "POST":
        version = body.get("version")
        if isinstance(version, bool) or not isinstance(version, int):
            raise core.Invalid("'version' must be a memory version")
        if version not in p["versions"]:
            raise KeyError(f"memory version {version}")
        _save_memory(core, p, p["versions"][version], "owner", f"rolled back by the owner (version {version})")
        _updated(core, name, "memory")
        return handler.send_json({"memory": {**_memory_view(p), "write": p["writes"][-1]}})
    if len(rest) == 3 and rest[:2] == ["memory", "versions"] and method == "GET":
        version = int(rest[2]) if rest[2].isdigit() else -1
        if version not in p["versions"]:
            raise KeyError(f"memory version {rest[2]}")
        text = p["versions"][version]
        return handler.send_json({"version": version, "text": text, "sha256": _sha(text), "clean": version in p["clean_versions"]})
    if len(rest) == 2 and rest[0] == "consolidation" and rest[1] in ("accept", "reject") and method == "POST":
        proposal = p["consolidation"]
        if proposal is None:
            raise KeyError("no pending consolidation")
        if rest[1] == "reject":
            p["consolidation"] = None
            return handler.send_json({"memory": _memory_view(p)})
        if proposal.get("base_version") != p["memory_version"] or proposal.get("stale"):
            proposal["stale"] = proposal.get("stale") or {"at": core.now(), "version": p["memory_version"]}
            raise core.Conflict(f"the summary changed since this review was proposed (it was version {proposal.get('base_version')}, now "
                                f"{p['memory_version']}): its diff no longer shows what it would replace. Reject it; the next weekly review starts from your text")
        _save_memory(core, p, proposal["text"], "consolidation", f"weekly consolidation {proposal.get('wake')} accepted by the owner")
        p["consolidation"] = None
        _updated(core, name, "memory")
        return handler.send_json({"memory": {**_memory_view(p), "write": p["writes"][-1]}})
    if len(rest) == 3 and rest[0] == "journal" and rest[2] == "retract" and method == "POST":
        if not re.fullmatch(r"J-\d{6,9}", rest[1]):
            raise core.Invalid("a journal entry id looks like J-000012")
        line = next((j for j in p["journal"] if j["id"] == rest[1]), None)
        if line is None:
            raise KeyError(f"journal entry {rest[1]}")
        line.update(retracted=core.now(), sha256=_sha(line.get("text") or ""), text="")
        _updated(core, name, "journal")
        return handler.send_json({"entry": copy.deepcopy(line)})
    if rest == ["forget"] and method == "POST":
        before = body.get("before")
        if not isinstance(before, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", before):
            raise core.Invalid("before must be a date YYYY-MM-DD")
        removed = {"journal": 0, "thread": 0, "versions": 0}
        for j in p["journal"]:
            if str(j["at"]) < before and j.get("text"):
                j.update(text="", retracted=core.now(), forgotten=True)
                removed["journal"] += 1
        for m in p["thread"]:
            if str(m["at"]) < before and m.get("text"):
                m.update(text="", forgotten=True)
                removed["thread"] += 1
        _updated(core, name, "journal")
        return handler.send_json({"removed": removed})
    if rest == ["messages"] and method == "POST":
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise core.Invalid("a message needs text")
        n = len(p["thread"]) + 1
        message = {"id": f"T-{n:06d}", "at": core.now(), "role": "owner", "text": text.strip(), "via": "cockpit", "sent_at": None, "taint": ["private"],
                   "wake": None, "label": None, "kind": None, "forwarded": None, "channel": None, "third_party": None}
        p["thread"].append(message)
        st["seq"]["wake"] += 1
        wake = {"id": f"W-{st['seq']['wake']:06d}", "persona": name, "chat": None, "source": "owner_message", "reason_sha256": None, "reason_len": 0,
                "taint": ["private"], "dedupe_key": f"owner:{name}", "requested_by": "human", "requested_at": core.now(), "not_before": core.now(),
                "moved": None, "chain": 0, "state": "pending", "attempts": 0, "yields": 0, "run": None, "item": None}
        st["queue"].append(wake)
        _updated(core, name, "thread")
        core.BUS.publish("wake.requested", {"wake": wake["id"], "persona": name, "chat": None, "source": "owner_message", "not_before": wake["not_before"],
                                            "dedupe_key": wake["dedupe_key"], "moved": None, "reason_len": 0, "reason_sha256": None}, agent=_display(core, name))
        return handler.send_json({"message": message, "wake": wake}, status=202)
    if rest == ["sound"] and method == "POST":
        p["sound_allowed"] = True
        return handler.send_json({"persona": name, "sound_allowed": True})
    if rest == ["reimport"] and method == "POST":
        if not p["source"]:
            raise core.Invalid(f"{name} was not imported from Open WebUI")
        if body.get("source") != "openwebui" and body.get("export") is None:
            raise core.Invalid("send the Export file as export, or source openwebui with a key")
        p["reimport"] = {"id": "RI-fedcba987654", "at": core.now(), "model": "kate", "from": "v-41", "to": "v-44",
                         "diff": "--- now\n+++ Open WebUI\n-You are Mira.\n+You are Mira, again.", "fingerprint": "fedcba987654" + "0" * 52,
                         "behaviour": "e" * 64, "flagged": [], "decider": ((_wakes_cfg(core).get("personas") or {}).get(name) or {}).get("reimport_decider", "owner")}
        return handler.send_json({"reimport": {k: v for k, v in p["reimport"].items()}}, status=201)
    if len(rest) == 2 and rest[0] == "reimport" and rest[1] in ("accept", "reject") and method == "POST":
        staged = p["reimport"]
        if not staged:
            raise KeyError("no re-import is waiting")
        if rest[1] == "accept" and staged.get("decider") == "persona":
            raise core.Invalid(f"{p['display']} decides on her own re-import (reimport_decider persona); you can reject it")
        p["reimport"] = None
        if rest[1] == "reject":
            return handler.send_json({"state": "rejected"})
        p["soul"] = "You are Mira, again. Speak plainly and kindly."
        p["file"] = _persona_file(name, p["description"], p["soul"])
        p["approved"] = p["file"]
        _updated(core, name, "soul")
        return handler.send_json({"state": "accepted"})
    if rest[:1] == ["external"]:
        return _api_ledger(core, handler, method, name, rest, query, body)
    if rest == ["before-import"] and method == "POST":
        from . import operator as operator_area

        source = p.get("source") or {}
        if not source.get("model"):
            raise core.Invalid(f"persona {name} is not bound to or imported from an Open WebUI workspace model")
        return handler.send_json(operator_area.before_import(core, name, source["model"], p.get("imported_at"), source.get("version_id"), body))
    if len(rest) == 2 and rest[0] == "runs" and method == "GET":
        if not re.fullmatch(r"\d+-wake", rest[1]):
            raise core.Invalid("a run log looks like 03-wake")
        lines = p["run_lines"].get(rest[1])
        if lines is None:
            raise KeyError(f"run log {rest[1]}")
        return handler.send_json({"run": rest[1], "lines": copy.deepcopy(lines)})
    return NotImplemented


def _save_memory(core, p: dict, text: str, by: str, reason: str, clean: bool = False) -> None:
    before = p["memory_text"]
    p["memory_version"] += 1
    v = p["memory_version"]
    p["versions"][v] = text
    p["memory_text"] = text
    p["memory_updated_at"] = core.now()
    p["memory_by"] = by
    if clean and by == "owner":
        p["clean_versions"] = sorted(set(p["clean_versions"]) | {v})
    p["writes"].append({"at": core.now(), "version": v, "before_sha256": _sha(before), "after_sha256": _sha(text), "by": by, "reason": reason,
                        "wake": None, "clean": bool(clean and by == "owner")})
    if p["consolidation"] and p["consolidation"].get("base_version") != v and not p["consolidation"].get("stale"):
        p["consolidation"]["stale"] = {"at": core.now(), "version": v}


def _api_chat_wakes(core, handler, method, chat, body):
    st = store(core, AREA)
    if chat not in core.STATE["chats"]:
        if method == "POST":
            raise KeyError(f"chat {chat}")
        return handler.send_json({"chat": chat, "allow": False, "wakes": []})
    if method == "POST":
        allow = (body or {}).get("allow")
        if not isinstance(allow, bool):
            raise core.Invalid("'allow' must be true or false")
        st["chat_optins"][chat] = allow
        if not allow:
            st["chat_wake_rows"][chat] = [w for w in st["chat_wake_rows"].get(chat, []) if w["state"] == "running"]
        return handler.send_json({"chat": chat, "allow": allow, "wakes": copy.deepcopy(st["chat_wake_rows"].get(chat, []))})
    return handler.send_json({"chat": chat, "allow": bool(st["chat_optins"].get(chat)), "wakes": copy.deepcopy(st["chat_wake_rows"].get(chat, []))})


# ---------------------------------------------------------------- test hooks: /__mock/messages/<name>

def mock(core, handler, method, name, body):
    st = store(core, AREA)
    body = body if isinstance(body, dict) else {}
    if name == "state" and method == "POST":
        if body.get("reset"):   # back to the seed's normal state (tests leave nothing stopped behind)
            st.update(paused=None, tamper=None, acknowledged_at=None, timezone_error=None, quiet_now=False, quiet_until=None, breaker=None,
                      time_sync={"ready": True, "synced": True, "fallback_used": False},
                      precondition={"ok": True, "sandbox": "bwrap", "cli_driver": False, "acknowledged_at": None, "sandboxed": True})
            core.STATE["config"].setdefault("notify", {})["enabled"] = True
            core.STATE["config"]["notify"].pop("safe_repos", None)
            core.STATE["config"].setdefault("wakes", {})["enabled"] = True
            for name_, chan in (_channel_cfg(core) or {}).items():
                chan["enabled"] = True
            for persona in ("seraphina", "mira"):
                if (st["stops"].get(persona) or {}).get("by") in ("integrity", "phone"):
                    st["stops"].pop(persona, None)
            ext = (((core.STATE["config"].get("wakes") or {}).get("personas") or {}).get("seraphina") or {}).get("memory")
            if isinstance(ext, dict):
                ext["external"] = None
            sera = ((core.STATE["config"].get("wakes") or {}).get("personas") or {}).get("seraphina")
            if isinstance(sera, dict):
                sera["wake"] = {"tools": "none"}
            if st.get("pages"):
                _seed_ledger(core)   # what an erase removed comes back
            core.BUS.publish("wake.resumed", {"scope": "all", "by": "human", "until": None})
        if "paused" in body:
            st["paused"] = ({"at": core.ago(600), "by": body["paused"], "via": body.get("via") or ("phone" if body["paused"] == "human" else "tamper-check")}
                            if body["paused"] else None)
            core.BUS.publish("wake.paused" if body["paused"] else "wake.resumed", {"scope": "all", "by": body["paused"] or "human", "until": None})
        if "tamper" in body:
            st["tamper"] = {"at": core.ago(600), "what": list(body["tamper"])} if body["tamper"] else None
        if "sandbox" in body:
            st["precondition"] = ({"ok": True, "sandbox": "bwrap", "cli_driver": False, "acknowledged_at": None, "sandboxed": True} if body["sandbox"] else
                                  {"ok": False, "sandbox": "none", "cli_driver": True, "acknowledged_at": None, "sandboxed": False})
        if "acknowledged" in body:
            st["acknowledged_at"] = core.ago(86400) if body["acknowledged"] else None
        if "notify_enabled" in body:
            core.STATE["config"].setdefault("notify", {})["enabled"] = bool(body["notify_enabled"])
        if "time_sync" in body:
            st["time_sync"] = {"ready": bool(body["time_sync"]), "synced": bool(body["time_sync"]), "fallback_used": False}
        if "timezone_error" in body:
            st["timezone_error"] = body["timezone_error"]
        if "quiet" in body:
            st["quiet_now"] = bool(body["quiet"])
        if "breaker" in body:
            st["breaker"] = {"open": True, "why": str(body["breaker"]), "at": core.ago(300)} if body["breaker"] else None
            core.BUS.publish("wake.breaker", {"open": bool(body["breaker"]), "why": body["breaker"] or None})
        if "wakes_enabled" in body:
            core.STATE["config"].setdefault("wakes", {})["enabled"] = bool(body["wakes_enabled"])
        if "integrity_stop" in body:
            st["stops"]["seraphina"] = {"at": core.ago(500), "by": "integrity", "until": None, "kind": "stop", "sha": "x"}
        if "until_unread" in body:
            st["stops"]["seraphina"] = {"at": core.ago(500), "by": "phone", "via": "telegram", "until": None, "kind": "stop", "until_unread": "tomorrow-ish"}
        if body.get("seed_pairing"):   # the seeded Telegram account, paired again (after a check that unpaired it)
            _chan_state(core, "telegram")["paired"] = _seed_tg_paired(core)
        if "installed" in body:
            for chan in st["chan"].values():
                chan["installed"] = bool(body["installed"])
        if "external_memory" in body:
            persona = core.STATE["config"].setdefault("wakes", {}).setdefault("personas", {}).setdefault("seraphina", {})
            persona.setdefault("memory", {})["external"] = {"folder": "/srv/memory/seraphina"} if body["external_memory"] else None
        if "ledger" in body:   # memory.external.ledger (with external memory on): the read ledger, opt-in
            persona = core.STATE["config"].setdefault("wakes", {}).setdefault("personas", {}).setdefault("seraphina", {})
            external = persona.setdefault("memory", {}).get("external") or {"folder": "/srv/memory/seraphina"}
            external["ledger"] = bool(body["ledger"])
            persona["memory"]["external"] = external
        if "wake" in body:   # wakes.personas.seraphina.wake as given ({tools, commands}): the command scope
            persona = core.STATE["config"].setdefault("wakes", {}).setdefault("personas", {}).setdefault("seraphina", {})
            persona["wake"] = copy.deepcopy(body["wake"])
        return handler.send_json(_notify_state(core))
    if name == "deliver" and method == "POST":
        entry = st["messages"].get(str(body.get("message")))
        if entry is None:
            raise KeyError("message")
        channel, state = str(body.get("channel")), str(body.get("state"))
        entry["deliveries"].setdefault(channel, _delivery(core, state))["state"] = state
        if "partial" in body:   # notify: a failed or unknown try that reached some accounts (their message ids)
            if body["partial"]:
                entry["deliveries"][channel]["partial"] = str(body["partial"])
            else:
                entry["deliveries"][channel].pop("partial", None)
        event = {"sent": "notify.sent", "failed": "notify.failed", "unknown": "notify.unknown", "held": "notify.held"}.get(state, "notify.queued")
        data = {"message": entry["id"], "channel": channel}
        if state in ("failed", "held"):
            data["why"] = "will retry" if state == "failed" else "Held: do-not-disturb until 08:00"
        core.BUS.publish(event, data)
        return handler.send_json({"message": _public(entry)})
    if name == "notice" and method == "POST":
        entry = _new_message(core, kind=str(body.get("kind") or "job.failed"), klass="operational", title=str(body.get("title") or "Job 004 failed"),
                             body=str(body.get("body") or "Job 004 failed at review."), priority=str(body.get("priority") or "default"))
        return handler.send_json({"message": _public(entry)})
    if name == "fill" and method == "POST":   # {count}: that many read notices at once, with no events (paging checks)
        count = max(0, min(int(body.get("count") or 0), 600))
        for i in range(count):
            st["seq"]["message"] += 1
            n = st["seq"]["message"]
            entry = _entry(core, n, kind="backup.ok", klass="operational", title=f"Filler notice {n}", body="filler", read=True, age=0,
                           deliveries={"phone": _delivery(core, "sent", title="Filler", text="filler", age=0)})
            st["messages"][entry["id"]] = entry
        return handler.send_json({"count": count, "last": f"M-{st['seq']['message']:06d}"})
    if name == "soul-changed" and method == "POST":   # someone edited her file outside the page
        p = st["pages"][str(body.get("persona") or "seraphina")]
        p["file"] = p["file"].rstrip("\n") + "\nEdited by hand.\n"
        p["soul"] = p["soul"] + "\nEdited by hand."
        p["integrity"] = "changed"
        p["diff"] = "--- approved\n+++ now\n@@\n+Edited by hand."
        if not body.get("quiet"):   # WakeManager._integrity_stop: the stop, one inbox notice and wake.paused (no event of its own)
            name = str(body.get("persona") or "seraphina")
            st["stops"][name] = {"at": core.now(), "by": "integrity", "until": None, "kind": "stop", "sha": _sha(p["file"])}
            display = _display(core, name)
            _new_message(core, kind="persona.integrity", klass="operational", title=f"{display}'s instructions changed", channels=[],
                         body=f"{display}'s instructions were changed outside her page. She is stopped until you look.", persona=name)
            core.BUS.publish("wake.paused", {"scope": name, "by": "integrity", "until": None}, agent=display)
        return handler.send_json({"file_sha256": _sha(p["file"])})
    if name == "memory-bumped" and method == "POST":   # her own update, before the page knew (an Accept then answers 409)
        p = st["pages"][str(body.get("persona") or "seraphina")]
        p["memory_version"] += 1
        p["versions"][p["memory_version"]] = p["memory_text"] + "\nShe added a line."
        p["memory_text"] = p["versions"][p["memory_version"]]
        p["memory_by"] = "persona"
        if not body.get("quiet"):
            _updated(core, str(body.get("persona") or "seraphina"), "memory")
        return handler.send_json({"version": p["memory_version"]})
    if name == "inbound" and method == "POST":
        row = _record_inbound(core, {"channel": "telegram", "kind": "text", "topic": None, "sender": "424242", "verb": None, "outcome": "kept",
                                     "sent_at": core.now(), "forwarded": False, "remote_id": "99", "text": str(body.get("text") or "hello <b>there</b>")})
        core.BUS.publish("inbound.received", {"channel": "telegram", "kind": "text", "verb": None, "persona": None, "outcome": "kept"})
        return handler.send_json({"inbound": row})
    return NotImplemented


# ---------------------------------------------------------------- the contract with swfactory/

def contract(test) -> None:
    """The mock's shapes against the real Notifier, WakeManager, rules and PersonaStore (built on a temporary config)."""
    import os
    import shutil
    import tempfile
    from pathlib import Path
    from unittest import mock as umock

    from swfactory import notify as nt
    from swfactory import rules as R
    from swfactory import wakes as W
    from swfactory.clock import TimeSync
    from swfactory.config import Config

    test.assertEqual(nt.ACK_SENTENCE, ACK_SENTENCE)
    test.assertEqual(nt.HOOK_KINDS, HOOK_KINDS)
    test.assertEqual(nt.HOOK_MAY, HOOK_MAY)
    test.assertEqual(nt.RULE_IDS, RULE_IDS)
    test.assertEqual((R.FLOORS, R.RATE_KEYS, R.FORCE_SHORT, R.CONFIG_KEYS, R.AUDIT), (FLOORS, RATE_KEYS, FORCE_SHORT, CONFIG_RULE_KEYS, AUDIT))
    test.assertEqual(_default_rules(), {k: v for k, v in R.DEFAULT_RULES.items()})
    test.assertEqual(_install_block(), nt.ntfy_install_block())
    real_blocks = nt.ntfy_copy_blocks(Path("/srv/ai-work/factory/notify/ntfy-setup"), {"public_url": "https://box.tail1234.ts.net:8445"})
    test.assertEqual([sorted(b) for b in _copy_blocks({"public_url": "https://box.tail1234.ts.net:8445"})], [sorted(b) for b in real_blocks])
    test.assertEqual([b["id"] for b in real_blocks], ["2a", "2b", "2c"])
    test.assertTrue(set(_hook_snippets("x", {})[0]) <= {"id", "title", "commands", "expect", "if_not", "warning", "note", "files", "dropin"})

    tmp = Path(tempfile.mkdtemp(prefix="sf-gui-mock-"))
    try:
        home = umock.patch.dict(os.environ, {"HOME": str(tmp / "home")})
        home.start()
        try:
            raw = {"data_dir": str(tmp / "data"), "harness": {"sandbox": "bwrap"},
                   "wakes": {"timezone": "Europe/London", "time_sync": {"require": False}, "enabled": True,
                             "hooks": {"backup-script": {"token_sha256": "ab" * 32, "may": ["event"], "events": ["backup.failed"]}},
                             "personas": {"seraphina": {"enabled": True}}},
                   "notify": {"channels": {"phone": {"type": "ntfy", "publish_url": "unix:/run/ntfy/ntfy.sock", "public_url": "https://box.tail1234.ts.net:8445"},
                                           "telegram": {"type": "telegram"}}, "sensitive_terms_file": str(tmp / "terms.txt")}}
            cfg = Config(tmp / "config.json", raw)
            cfg.save()
            notifier = nt.Notifier(cfg, None, None, environ={}, tailnet=lambda: None, server_yml=lambda: None, time_sync=TimeSync(require=False),
                                   run=lambda argv, input_text=None, timeout=10.0: "")
            notifier.load()
            real_state = {**notifier.state(), "channels": notifier.channels(), "hooks": notifier.hooks()}
            manager = W.WakeManager(cfg, None, None)
            real_wakes = manager.state()
            real_page = manager.persona("seraphina")
            real_rules = R.load_rules(cfg)
            real_ledger = manager.external_ledger("seraphina")
        finally:
            home.stop()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    def get(path):
        status, _, raw_body = test.request("GET", path)
        test.assertEqual(status, 200, path)
        return json.loads(raw_body)

    state = get("/api/notify")
    test.assertEqual(set(state), set(real_state))
    for key in ("quiet_hours", "precondition", "today", "rules", "sensitive_terms", "time_sync"):
        test.assertEqual(set(state[key]), set(real_state[key]), key)
    test.assertEqual(set(state["channels"][0]), set(real_state["channels"][0]))
    test.assertEqual(set(state["channels"][0]["health"]), set(real_state["channels"][0]["health"]))
    test.assertEqual(set(state["hooks"][0]), set(real_state["hooks"][0]))
    test.assertEqual({c["privacy"] for c in state["channels"]}, {"tailnet", "third_party"})
    data = get("/api/messages?limit=200")
    test.assertEqual(set(data), {"messages", "unread"})
    keys = {"id", "at", "kind", "class", "sender", "persona", "priority", "route", "title", "body", "taint", "about", "state",
            "unread", "read_at", "deliveries", "snoozed_until", "summary", "text_removed"}
    test.assertTrue(data["messages"] and all(set(m) == keys for m in data["messages"]))
    test.assertEqual(data["unread"], 2, "Home's checks count two unread messages")
    states = {s for m in data["messages"] for s in m["deliveries"].values()}
    test.assertEqual(states, {"queued", "sending", "sent", "failed", "unknown", "held", "off", "snoozed"})
    detail = get("/api/messages/M-000012")
    test.assertEqual(set(detail), {"message", "deliveries", "inbound"})
    test.assertEqual(set(detail["deliveries"][0]), {"channel", "rendered", "why", "state", "at", "error", "attempts", "next_try", "partial"})
    test.assertIn("fields", detail["message"])
    wakes = get("/api/wakes")
    test.assertEqual(set(wakes), set(real_wakes))
    test.assertEqual(set(wakes["personas"][0]), set(real_wakes["personas"][0]))
    test.assertEqual(set(wakes["personas"][0]["budget"]), set(real_wakes["personas"][0]["budget"]))
    test.assertEqual(set(wakes["personas"][0]["settings"]), set(real_wakes["personas"][0]["settings"]))
    test.assertTrue({e["source"] for e in wakes["queue"]} <= set(W.SOURCES))
    page = get("/api/personas/seraphina")
    test.assertEqual(set(page), set(real_page) | {"card", "drift", "import"}, "Factory.persona adds card and drift (null) and import")
    test.assertIsNone(page["import"], "seraphina was not imported from Open WebUI")
    mira = get("/api/personas/mira")
    test.assertEqual(set(mira["import"]), {"model", "imported_at", "version_id", "diff", "baseline", "changes_before_import"})
    from swfactory.personas import IMPORT_BASELINE
    test.assertEqual((mira["import"]["baseline"], mira["import"]["diff"]), (IMPORT_BASELINE, None))
    test.assertEqual(wakes["personas"][0]["settings"]["commands"], [], "no tools: RunCheck runs nothing")
    test.request("POST", "/__mock/messages/state", {"wake": {"tools": "read_only"}})
    sera = next(r for r in get("/api/wakes")["personas"] if r["name"] == "seraphina")
    test.assertEqual(sera["settings"]["commands"], [list(c) for c in W.WAKE_COMMANDS], "read-only tools, no commands key: the default")
    test.request("POST", "/__mock/messages/state", {"wake": {"tools": "read_only", "commands": []}})
    sera = next(r for r in get("/api/wakes")["personas"] if r["name"] == "seraphina")
    test.assertEqual(sera["settings"]["commands"], [], "commands: [] runs none")
    test.assertIsNone(sera["settings"]["commands_off"], "[] is her setting, not the ledger's doing")
    # beside the read ledger without bwrap her set commands run none, and the row says why (commands_off)
    test.request("POST", "/__mock/messages/state", {"wake": {"tools": "read_only", "commands": [["git", "status"]]}, "ledger": True, "sandbox": False})
    sera = next(r for r in get("/api/wakes")["personas"] if r["name"] == "seraphina")
    test.assertEqual((sera["settings"]["commands"], sera["settings"]["commands_off"]), ([], "ledger_without_sandbox"))
    test.request("POST", "/__mock/messages/state", {"sandbox": True})
    sera = next(r for r in get("/api/wakes")["personas"] if r["name"] == "seraphina")
    test.assertEqual((sera["settings"]["commands"], sera["settings"]["commands_off"]), ([["git", "status"]], None))
    test.request("POST", "/__mock/messages/state", {"wake": {"tools": "none"}, "ledger": False})
    # the read ledger: WakeManager.external_ledger's keys, over the mock's real PersonaStore
    ledger = get("/api/personas/seraphina/external")
    test.assertEqual(set(ledger), set(real_ledger))
    test.assertEqual(ledger["label"], "private")
    versions = [v for f in ledger["files"] for v in f["versions"]]
    test.assertTrue(any(v["redacted"] for v in versions) and any(v["truncated"] for v in versions), "a redacted and a partial read")
    test.assertTrue(ledger["erased"] and ledger["caps"].get("evicted") == 3, "an erased version and the cap's count")
    test.assertTrue(any(r.get("error") for r in ledger["sqlite"]), "a database read that could not be read")
    notes = next(f for f in ledger["files"] if f["name"] == "notes.md")
    older, newer = notes["versions"][-1]["sha256"], notes["versions"][0]["sha256"]
    test.assertEqual(set(get(f"/api/personas/seraphina/external/versions/{older}")),
                     {"sha256", "names", "text", "redacted", "truncated", "bytes", "first_read_at", "last_read_at", "label"})
    test.assertEqual(set(get(f"/api/personas/seraphina/external/diff?from={older}&to={newer}")), {"from", "to", "diff", "label"})
    status, _, raw_body = test.request("POST", "/api/personas/seraphina/external/restore", {"sha256": older})
    test.assertEqual(status, 200, raw_body)
    restore = json.loads(raw_body)["restore"]
    test.assertEqual(set(restore), {"name", "sha256", "text", "current", "diff", "warnings", "apply", "label"})
    from swfactory.personas import RESTORE_APPLY
    test.assertEqual(restore["apply"], RESTORE_APPLY)
    diary = next(f for f in ledger["files"] if f["name"] == "diary.md")["versions"][0]["sha256"]
    status, _, raw_body = test.request("POST", "/api/personas/seraphina/external/restore", {"sha256": diary})
    test.assertEqual(status, 400, "a version read only in part has no whole copy to restore")
    test.assertEqual(test.request("GET", "/api/personas/seraphina/external/versions/nothex")[0], 400)
    test.assertEqual(set(page["soul"]), set(real_page["soul"]))
    test.assertEqual(set(page["memory"]), set(real_page["memory"]))
    test.assertEqual(set(get("/api/notify/rules")), set(real_rules))
    status, _, raw_body = test.request("POST", "/api/notify/rules", {"boundary": {"max_body": 400}})
    test.assertEqual(status, 400)
    test.assertIn("stricter, never weaker", json.loads(raw_body)["error"])
    test.assertEqual(set(get("/api/chats/c-1a2b3c4d5e/wakes")), {"chat", "allow", "wakes"})
