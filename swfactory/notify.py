"""Messages to the owner's phone without a model (Wake & Messages steps W1a, W1b, W1c;
docs/seraphina/WAKE_AND_MESSAGES.md §2.3, §2.7, §3, §5.9).

A Notice is one message for the owner. Every notice lands in the cockpit's inbox (its outbox file holds
the full text, on this machine only); what each phone channel gets is decided by the content gate,
render(), a pure function of the notice, the channel's class and settings, the sensitive-terms list and
the secrets:

- ``short``: a fixed, content-free template chosen by the notice's kind ("A job is waiting for your
  decision. Open the cockpit."); the third-party form never names a persona;
- ``operational``: built by code from typed fields only (a job's number, never its slug; a repository name
  only when it is marked safe to show and the channel allows names; a step from a fixed list; a hook
  subject only when it is on the hook's allowlist); a third-party channel gets it only with
  ``operational: true``, otherwise the short notice;
- ``model`` (a persona's words, step W2) and ``owner_text`` (a reminder, step W2) reach only the private
  channel, only from a clean turn or when the owner ticked "show this text on my phone";
- every chosen text then passes filter_text: a link, Markdown, HTML, a secret, a 20-character run of
  token-like characters, 8 digits in a row or a sensitive term forces the short notice instead (never a
  trimmed text), and so does a title over 60 or a body over 300 characters.

Each phone notice carries one code-written why line ("Job 003 has waited since 11:10"). Routes map a
notice to channels (needs_you: every channel; persona and digest: the private channel first; reply: the
channel the owner wrote from; default: the inbox only). Delivery holds everything below high priority
during do-not-disturb time (only the rules in quiet_breakthrough break through), counts phone notices
against per-day caps (deterministic high alerts are exempt), and summarises what it held in the morning
summary, in counts only. Deliveries are queued, sending, sent, failed (refused, or never sent: retried with
backoff, skipping the accounts an earlier try reached), unknown (sent and not confirmed: no answer, or a server
error; never resent automatically, DESIGN §40) or held.

The one rules engine turns harness events into notices (a job needs a decision, a job failed, a plan
waits, tracker items overdue), level-triggered with a reconcile every five minutes, its state in
rules.json so a restart repeats nothing. Hook events (backup.failed, service.down, ...) open incidents that
alert once, remind after six hours at most three times, and close with "working again".

Inbound handling (S32-S37, S48): buttons carry single-use capabilities (only their sha256 is kept), typed
commands are recognised only as whole messages (stop, snooze 1h|3h|morning, quiet 3h|morning, mute, status,
help), late commands are not applied (except stop), bursts hold a channel for 15 minutes (stop still
works), anything on the taps topic that is not a verifying capability is forged, and a notice on the
notices topic the harness did not publish is foreign. Free text is kept for the owner in the cockpit (and
goes to a persona's thread once step W2 exists). Nothing an inbound message says can approve, resume,
pair, change a setting or reveal anything.

The audit log (S45) is plain append-only JSON lines in <data_dir>/wakes/audit-YYYY-MM.jsonl; the settings
fingerprints (S47) stop Seraphina and hold every phone send when the wakes or notify settings, a pairing,
the reminders or the sensitive-terms file change outside the cockpit. Phone channels stay off while agents
run without bwrap or through a CLI driver, unless the owner typed the acknowledgement (S46).

Lock rule: the Notifier never sends, publishes an event or calls the factory while holding its lock.
"""

from __future__ import annotations

import contextlib
import copy
import functools
import hashlib
import ipaddress
import json
import os
import queue
import re
import secrets
import shlex
import shutil
import string
import subprocess
import threading
import time
import unicodedata
import urllib.parse
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import envfile
from . import outcomes
from .channels import (BUTTON_LABELS, NTFY_SERVER_YML, PRIORITIES, PRIVACY_TAILNET, PRIVACY_THIRD_PARTY, TAILNET_NET,
                       Button, Channel, ChannelError, Inbound, Rendered, SendResult, channel_privacy, digest,
                       is_loopback_host, make_channel, new_capability, tailnet_status)
from .config import sensitive_terms_path
from .clock import LocalClock, PowerSchedule, TimeSync, Window, iso, parse_iso
from .events import env_secret_values, utc_now

UTC = timezone.utc
NOTICE_CLASSES = ("short", "operational", "model", "owner_text")
ROUTE_KEYS = ("needs_you", "persona", "digest", "reply", "default")
ROUTE_TARGETS = ("*", "private_first", "origin", "tailnet", "third_party")
HOOK_KINDS = ("backup.failed", "backup.ok", "disk.low", "service.down", "service.up")
# Disk protection (DESIGN §41; docs/seraphina/BACKUPS_SNAPRAID.md §7.3): fixed texts with counts only, never a disk's
# or a file's name. The reader sends disk.failing; the daemon route all of them.
PARITY_KINDS = ("parity.held", "parity.failed", "parity.ok", "disk.failing", "parity.released_outside",
                "parity.down", "parity.lapsed")
# Event kinds an event source may send that go to a panel, never to the incident rules: the SnapRAID Daemon's
# report forwarder (BACKUPS_SNAPRAID.md §7.1) posts parity.report, a prompt for the panel to read the daemon.
PANEL_HOOK_KINDS = ("parity.report",)
RECOVERS = {"backup.ok": "backup.failed", "service.up": "service.down", "parity.ok": "parity.failed"}
RULE_IDS = ("job.needs_human", "job.failed", "plan.attention", "items.overdue", "items.waiting", *HOOK_KINDS,
            "wakes.breaker", "channel.down", "server.unclean", "wakes.tamper", "inbound.forged", *PARITY_KINDS)
RULE_ROUTES = {"job.needs_human": "needs_you", "job.failed": "needs_you", "plan.attention": "needs_you",
               "items.overdue": "default", "items.waiting": "default", "backup.failed": "needs_you",
               "backup.ok": "needs_you", "disk.low": "needs_you", "service.down": "needs_you",
               "service.up": "needs_you", "wakes.breaker": "needs_you", "channel.down": "needs_you",
               "server.unclean": "needs_you", "wakes.tamper": "needs_you", "inbound.forged": "needs_you",
               **{kind: "needs_you" for kind in PARITY_KINDS}}
BUTTONS_BY_SENDER = {"persona": ("ack", "snooze_1h", "stop"), "server": ("ack", "snooze_1h", "quiet_morning"),
                     "summary": ("ack",), "test": ("ack",)}
BUTTON_VERBS = ("ack", "snooze_1h", "snooze_3h", "snooze_morning", "quiet_3h", "quiet_morning", "stop")
PASS_THROUGH = ("summary", "confirm", "test", "answer")   # kinds never held by do-not-disturb time or caps
SECURITY_KINDS = ("wakes.tamper", "inbound.forged", "inbound.alarm", "inbound.hold")   # sent at once, low inside quiet
NOT_COUNTED = ("summary", "confirm", "test", "answer")
READ_KINDS = ("answer",)          # an answer to the owner's own command lands in the inbox already read
HOOK_MAY = ("event", "wake")     # report events; wake a persona (/api/hooks/wake, step W3)
# What each of the harness's own writes changes (settings_written): those fingerprints become the new baseline
# without counting as a hand edit; the writer checked them before it wrote.
WRITTEN_BY = {"reminders": ("reminders",), "clean confirmations": ("clean", "rules"), "rules": ("rules", "clean")}
HOOK_TOKEN_BYTES = 32
ACK_SENTENCE = "I understand my AI agents can read my messages and send them anywhere"
MESSAGE_ID_RE = re.compile(r"^M-(\d{6,9})$")
INBOUND_ID_RE = re.compile(r"^I-(\d{6,9})$")
HOOK_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
SUBJECT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,40}$")
DEDUPE_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
IDEMPOTENCY_RE = re.compile(r"^[\x21-\x7e]{1,128}$")
MAX_TITLE = 60
MAX_BODY = 300
MAX_DETAIL = 200
TOKEN_RUN_RE = re.compile(r"[A-Za-z0-9+/=_-]{20,}")
DIGIT_RUN_RE = re.compile(r"\d{8,}")
URL_RE = re.compile(r"(?i)\b(?:[a-z][a-z0-9+.-]*://|www\.)\S+|\b[a-z0-9-]+(?:\.[a-z0-9-]+)+\.[a-z]{2,}/\S*")
HTML_RE = re.compile(r"<[A-Za-z/!][^<>]{0,200}>")
MARKDOWN_RE = re.compile(r"\*\*[^*\n]+\*\*|__[^_\n]+__|`[^`\n]+`|~~[^~\n]+~~|\[[^\]\n]*\]\([^)\n]*\)|"
                         r"^[ \t]{0,3}(?:#{1,6}\s|>\s|[-*+]\s|\d+\.\s)", re.MULTILINE)
RETRY_MINUTES = (1, 2, 4, 8, 16, 30)
GIVE_UP_HOURS = 24
CHANNEL_DOWN_FAILURES = 5
CHANNEL_DOWN_SECONDS = 3600
RECONCILE_SECONDS = 300
TICK_SECONDS = 5.0
LIVENESS_SECONDS = 60
SUMMARY_LABELS = {"evening": "Evening", "morning": "Morning", "midday": "Midday", "quiet": "Quiet time"}
EVENING_WINDOW = timedelta(hours=2)    # an evening summary later than this after quiet hours began is skipped
MORNING_WINDOW = timedelta(hours=4)
RULE_EVENTS = ("job.updated", "plan.updated", "items.updated")
HUMAN_STAGES = ("needs-human", "failed")
PLAN_WAITING = {"ready": "it is ready to read and run", "needs-human": "it needs your decision",
                "failed": "it stopped", "blocked": "one of its jobs needs you"}
STEP_WORDS = {"setup": "setup", "spec": "spec", "build": "build", "review": "review", "approve": "approval",
              "gate": "approval", "merge": "merge", "deliver": "delivery"}
SAFE_WORD_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,40}$")
FAIL_DELAY = 0.25

# ---------------------------------------------------------------- templates (v2 word list)

# SHORT[key] = ((private title, private text), (third-party title, third-party text)). "{persona}" only ever in
# the private form, filled with a name already checked against the sensitive terms.
SHORT: dict[str, tuple[tuple[str, str], tuple[str, str]]] = {
    "urgent": (("Urgent", "Urgent: something on your server needs you now. Open the cockpit."),
               ("Urgent", "Urgent: something on your server needs you now. Open the cockpit.")),
    "decision": (("Waiting for you", "A job is waiting for your decision. Open the cockpit."),
                 ("Waiting for you", "A job is waiting for your decision. Open the cockpit.")),
    "failed": (("A job failed", "A job failed. Nothing was merged. Open the cockpit."),
               ("A job failed", "A job failed. Nothing was merged. Open the cockpit.")),
    "recovered": (("Working again", "Something that went wrong on your server is working again."),
                  ("Working again", "Something that went wrong on your server is working again.")),
    "reminder": (("Reminder", "A reminder you set is due."), ("Reminder", "A reminder you set is due.")),
    "message": (("{persona}", "{persona} has a message for you. Open the cockpit to read it."),
                ("Heads-up", "You have a message. Open the cockpit to read it.")),
    "answer": (("{persona}", "{persona} answered. Read it in the cockpit on your computer: Messages, then {persona}."),
               ("Heads-up", "You have an answer waiting. Read it in the cockpit on your computer, under Messages.")),
    "checkin": (("Check-in done", "A chat checked on something, as it said it would. Read it in the cockpit."),
                ("Check-in done", "A scheduled check-in finished. Read it in the cockpit.")),
    "summary": (("{which} summary", "{which} summary: {things}. Open the cockpit."),
                ("{which} summary", "{which} summary: {things}. Open the cockpit.")),
    "system": (("Your server", "Your server has a notice about its messaging. Open the cockpit."),
               ("Your server", "Your server has a notice about its messaging. Open the cockpit.")),
    "notice": (("Your server", "Your server has a notice for you. Open the cockpit."),
               ("Your server", "Your server has a notice for you. Open the cockpit.")),
    "test": (("Test", "This is a test from your server. Tap Got it."),
             ("Test", "This is a test from your server. Tap Got it.")),
    "stopped": (("Seraphina stopped", "Seraphina is stopped (from your phone at {time}). Alerts from your server, "
                                      "such as failed jobs and backups, still come. Restart her in the cockpit on "
                                      "your computer."),
                ("Assistant stopped", "Your assistant is stopped (from your phone at {time}). Alerts from your "
                                      "server still come. Restart it in the cockpit on your computer.")),
    "muted": (("{persona} muted", "{persona} is muted until {until} (from your phone at {time}). Alerts from your "
                                  "server still come. Unmute in the cockpit on your computer."),
              ("Assistant muted", "Your assistant is muted until {until} (from your phone at {time}). Alerts from "
                                  "your server still come. Unmute in the cockpit on your computer.")),
    "quiet": (("Quiet until {until}", "Quiet until {until}: messages wait until then, except urgent ones."),
              ("Quiet until {until}", "Quiet until {until}: messages wait until then, except urgent ones.")),
    "unpaired": (("Unpaired", "A phone was unpaired in the cockpit at {time}. It can no longer talk to your server."),
                 ("Unpaired", "A phone was unpaired in the cockpit at {time}. It can no longer talk to your server.")),
    "hold": (("Stopped listening", "{channel} sent {count} commands in a minute, so your server stopped listening "
                                   "to it for {minutes} minutes. If that wasn't you: change your Telegram password, "
                                   "check Two-Step Verification, then Unpair in the cockpit."),
             ("Stopped listening", "{channel} sent {count} commands in a minute, so your server stopped listening "
                                   "to it for {minutes} minutes. If that wasn't you, Unpair in the cockpit.")),
}

HELP = ("Commands, as the whole message: stop, snooze 1h, snooze 3h, snooze morning, quiet 3h, quiet morning, "
        "mute, status, help. Read answers in the cockpit on your computer, under Messages. A real message never "
        "asks you to approve anything, type a password or code, or open a link. If one does, it is fake: send "
        "stop and tell an admin.")
DID_YOU_MEAN = {
    "stop": ("Did you mean the command 'stop' (stops Seraphina until you restart her on your computer)? Reply /stop.",
             "Did you mean the command 'stop' (stops your assistant until you restart it on your computer)? "
             "Reply /stop."),
    "snooze": ("Did you mean 'snooze 1h', 'snooze 3h' or 'snooze morning'? Reply with exactly one of them.",) * 2,
    "quiet": ("Did you mean 'quiet 3h' or 'quiet morning'? Reply with exactly one of them.",) * 2,
    "mute": ("Did you mean 'mute'? Reply with exactly that.",) * 2,
    "status": ("Did you mean 'status'? Reply with exactly that.",) * 2,
    "help": ("Did you mean 'help'? Reply with exactly that.",) * 2,
}
REFUSED = {"approve": "Approve in the cockpit.", "merge": "Approve in the cockpit.",
           "reject": "Do this in the cockpit.", "rework": "Do this in the cockpit.",
           "resume": "Do this in the cockpit.", "restart": "Do this in the cockpit.",
           "unmute": "Do this in the cockpit.", "unpause": "Do this in the cockpit.", "pair": "Do this in the cockpit.",
           "unpair": "Do this in the cockpit.", "run": "Do this in the cockpit.", "delete": "Do this in the cockpit.",
           "retry": "Do this in the cockpit.", "cancel": "Do this in the cockpit.",
           "settings": "Do this in the cockpit.", "config": "Do this in the cockpit."}
COMMANDS = {"stop": "stop", "snooze 1h": "snooze_1h", "snooze 3h": "snooze_3h", "snooze morning": "snooze_morning",
            "quiet 3h": "quiet_3h", "quiet morning": "quiet_morning", "mute": "mute", "status": "status",
            "help": "help", "start": "help"}
COMMAND_WORDS = ("stop", "snooze", "quiet", "mute", "status", "help")
KEPT = "Your message is kept for you in the cockpit (Messages). Seraphina cannot answer messages yet."
KEPT_THIRD = "Your message is kept for you in the cockpit (Messages). Nothing here can answer messages yet."
TELEGRAM_WARNING = "Telegram can read what you type here. For anything private, write to Seraphina in the cockpit."
ONLY_TEXT = "Seraphina reads only typed text here."
EXPIRED = "That button had expired; nothing changed."
REFUSED_TAP = "That button no longer works; nothing changed."
LATE_TAP = "That tap arrived late and was not applied. Open the cockpit to apply it."
HOLD_DROPPED = "Too many commands: only stop works for now."
UNDER_REVIEW = "dropped (settings under review)"   # S47: only stop is obeyed while tamper is set


class NotifyError(ValueError):
    """A request the notifier refuses (HTTP 400)."""


class HookRefused(Exception):
    """A hook request outside the hook's scope or limits: status (403, 413, 429, 503) and a safe message."""

    def __init__(self, status: int, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


# ---------------------------------------------------------------- the notice


@dataclass
class Notice:
    kind: str                       # rule id ("job.needs_human"), "persona", "reply", "reminder", "summary", "test",
    #                                 "confirm", "checkin", "inbound.alarm", "inbound.hold", "notice"
    klass: str                      # NOTICE_CLASSES; set by the code that builds the notice
    route: str                      # ROUTE_KEYS
    priority: str                   # PRIORITIES
    title: str                      # full text; the inbox always shows it
    body: str
    sender: str = "server"          # "server" (shown as "Your server") or a persona name
    fields: dict = field(default_factory=dict)     # typed values the templates and why lines use
    persona: str | None = None
    wake: str | None = None
    taint: tuple[str, ...] = ()
    about: str | None = None        # a ref for the cockpit link
    origin_channel: str | None = None   # route "reply": the channel the owner wrote from
    dedupe_key: str | None = None
    signature: str | None = None
    buttons: tuple[str, ...] | None = None   # None: by sender (BUTTONS_BY_SENDER)
    show_on: tuple[str, ...] = ()   # owner_text: the private channels the owner allowed

    def check(self) -> None:
        if self.klass not in NOTICE_CLASSES:
            raise NotifyError(f"notice class must be one of {', '.join(NOTICE_CLASSES)}")
        if self.route not in ROUTE_KEYS:
            raise NotifyError(f"notice route must be one of {', '.join(ROUTE_KEYS)}")
        if self.priority not in PRIORITIES:
            raise NotifyError(f"notice priority must be one of {', '.join(PRIORITIES)}")
        if self.buttons is not None and any(b not in BUTTON_VERBS for b in self.buttons):
            raise NotifyError("unknown button verb")


def _button_verbs(notice: Notice) -> tuple[str, ...]:
    if notice.buttons is not None:
        return notice.buttons
    if notice.kind in ("summary", "test"):
        return BUTTONS_BY_SENDER[notice.kind]
    if notice.kind in ("confirm", "answer"):
        return ()
    return BUTTONS_BY_SENDER["persona" if notice.sender != "server" else "server"]


def notice_from_entry(entry: dict) -> Notice:
    """The Notice an outbox entry holds."""
    buttons = entry.get("buttons")
    return Notice(kind=entry["kind"], klass=entry["class"], route=entry["route"], priority=entry["priority"],
                  title=entry.get("title") or "", body=entry.get("body") or "", sender=entry.get("sender") or "server",
                  fields=dict(entry.get("fields") or {}), persona=entry.get("persona"), wake=entry.get("wake"),
                  taint=tuple(entry.get("taint") or ()), about=entry.get("about"),
                  origin_channel=entry.get("origin_channel"), dedupe_key=entry.get("dedupe_key"),
                  signature=entry.get("signature"), buttons=tuple(buttons) if isinstance(buttons, list) else None,
                  show_on=tuple(entry.get("show_on") or ()))


# ---------------------------------------------------------------- the content gate (pure)


def job_number(job_id: Any) -> str:
    """"003" from "003-rate-limit-signup" (the slug comes from the feature text and stays off the phone)."""
    match = re.match(r"^(\d{1,6})", str(job_id or ""))
    return match.group(1) if match else "?"


def plan_number(plan_id: Any) -> str:
    match = re.match(r"^(P\d{1,6})", str(plan_id or ""))
    return match.group(1) if match else "?"


def _strip_invisible(text: str) -> str:
    """text without control characters (newline kept) and Unicode format characters (category Cf)."""
    return "".join(ch for ch in text if ch == "\n" or (unicodedata.category(ch) not in ("Cc", "Cf")
                                                       and not 0xE0000 <= ord(ch) <= 0xE007F))


def _term_pattern(term: str) -> re.Pattern[str] | None:
    words = [re.escape(word) for word in term.split()]
    if not words:
        return None
    return re.compile(r"(?<!\w)" + r"\s+".join(words) + r"(?!\w)", re.IGNORECASE)


def filter_text(text: str, *, terms: Iterable[str], secrets: Iterable[str], token_run: int = 20,
                digit_run: int = 8) -> tuple[str, list[str]]:
    """(the cleaned text, the reasons that force the short notice), S25. Cleaning removes control and format
    characters and redacts secrets; a link, HTML, Markdown, a secret, a run of 20 token-like characters, 8
    digits in a row or a sensitive term is a reason (the caller then sends the short notice, never a trim).
    token_run and digit_run come from the rules file's boundary (stricter values only, rules.FLOORS)."""
    cleaned = _strip_invisible(str(text or ""))
    reasons: list[str] = []
    if URL_RE.search(cleaned):
        reasons.append("link")
    if HTML_RE.search(cleaned):
        reasons.append("html")
    if MARKDOWN_RE.search(cleaned):
        reasons.append("markdown")
    for secret in sorted({s for s in secrets if isinstance(s, str) and len(s) >= 8}, key=len, reverse=True):
        if secret in cleaned:
            cleaned = cleaned.replace(secret, "***")
            if "secret" not in reasons:
                reasons.append("secret")
    token_re = TOKEN_RUN_RE if token_run >= 20 else re.compile(r"[A-Za-z0-9+/=_-]{%d,}" % max(1, token_run))
    digit_re = DIGIT_RUN_RE if digit_run >= 8 else re.compile(r"\d{%d,}" % max(1, digit_run))
    if token_re.search(cleaned):
        reasons.append("token-like")
    if digit_re.search(cleaned):
        reasons.append("number")
    for term in terms:
        pattern = _term_pattern(term) if isinstance(term, str) else None
        if pattern is not None and pattern.search(cleaned):
            reasons.append("sensitive")
            break
    return cleaned, reasons


def read_terms(path: Path) -> tuple[list[str], dict]:
    """(terms, status) from the sensitive-terms file: one term or phrase per line, # comments. status:
    {path, exists, count, mode_ok}. A file that cannot be read gives no terms (the doctor reports it)."""
    status = {"path": str(path), "exists": False, "count": 0, "mode_ok": None}
    try:
        info = path.stat()
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return [], status
    terms = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
    status.update(exists=True, count=len(terms), mode_ok=not (info.st_mode & 0o077))
    return terms, status


def short_key(notice: Notice) -> str:
    """Which short-notice template a notice gets, by kind and priority."""
    kind = notice.kind
    if kind == "confirm":
        return str(notice.fields.get("short") or "system")
    if kind in ("summary", "test", "reminder", "checkin"):
        return kind
    if kind == "persona":
        return "message"
    if kind == "reply":
        return "answer"
    if kind in ("inbound.hold",):
        return "hold"
    if notice.priority in ("high", "max") and notice.sender == "server":
        return "urgent"
    if kind in ("job.needs_human", "plan.attention"):
        return "decision"
    if kind == "job.failed":
        return "failed"
    if kind in RECOVERS:
        return "recovered"
    if kind in ("wakes.breaker", "channel.down", "server.unclean", "wakes.tamper", "inbound.forged", "inbound.alarm",
                "wakes.watch"):
        return "system"
    return "notice"


def short_text(key: str, third: bool, *, persona: str | None, fields: Mapping[str, Any]) -> tuple[str, str]:
    """The (title, text) of a short notice; the third-party form never names a persona."""
    private, public = SHORT.get(key, SHORT["notice"])
    title, text = public if third else private
    count = fields.get("n") if isinstance(fields.get("n"), int) else 0
    values = {"persona": persona or "Seraphina", "which": str(fields.get("which") or "Your").capitalize(),
              "things": f"{count} thing{'s' if count != 1 else ''}", "time": str(fields.get("time") or ""),
              "until": str(fields.get("until") or ""), "channel": str(fields.get("channel_words") or "A phone"),
              "count": str(fields.get("count") or ""), "minutes": str(fields.get("minutes") or "")}
    return _fill(title, values), _fill(text, values)


def _fill(template: str, values: Mapping[str, str]) -> str:
    for key, value in values.items():
        template = template.replace("{" + key + "}", value)
    return template


def _repo_label(fields: Mapping[str, Any], names: bool, safe_repos: set[str], what: str, number: str) -> str:
    repo = str(fields.get("repo") or "")
    if names and repo and repo in safe_repos and SAFE_WORD_RE.fullmatch(repo):
        return f"{repo} {what} {number}"
    return f"A {what} ({number})"


def _subject(fields: Mapping[str, Any]) -> str | None:
    subject = fields.get("subject")
    return subject if fields.get("subject_allowed") and isinstance(subject, str) and SUBJECT_RE.fullmatch(subject) \
        else None


def operational_text(notice: Notice, *, names: bool, safe_repos: set[str]) -> tuple[str, str]:
    """(title, body) built from the notice's typed fields only (S24). names: repository names may appear
    (only those in safe_repos)."""
    f = notice.fields
    kind = notice.kind
    if kind == "job.needs_human":
        number = job_number(f.get("job"))
        label = _repo_label(f, names, safe_repos, "job", number)
        return "Waiting for your decision", f"{label} is waiting for your decision. See it in the cockpit (Jobs, then {number})."
    if kind == "job.failed":
        number = job_number(f.get("job"))
        label = _repo_label(f, names, safe_repos, "job", number)
        step = STEP_WORDS.get(str(f.get("step") or ""), "a")
        step_words = "at a step" if step == "a" else f"at the {step} step"
        return "A job failed", (f"{label} failed {step_words}. Nothing was merged. "
                                f"See why in the cockpit (Jobs, then {number}).")
    if kind == "plan.attention":
        number = plan_number(f.get("plan"))
        label = _repo_label(f, names, safe_repos, "plan", number)
        return "A plan is waiting", f"{label} is waiting for you: {PLAN_WAITING.get(str(f.get('stage')), 'it needs you')}. See it in the cockpit (Plans)."
    if kind in HOOK_KINDS:
        subject = _subject(f)
        reminder = "Still: " if f.get("reminder") else ""
        texts = {
            "backup.failed": ("A backup failed", f"The {subject} backup failed." if subject else "A backup failed.",
                              " See the cockpit for what to do."),
            "backup.ok": ("Working again", f"The {subject} backup is working again." if subject
                          else "A backup is working again.", ""),
            "service.down": ("A service is down", f"The {subject} service is down." if subject
                             else "A service is down.", " See the cockpit for what to do."),
            "service.up": ("Working again", f"The {subject} service is working again." if subject
                           else "A service is working again.", ""),
            "disk.low": ("Disk almost full", f"The {subject} disk is almost full." if subject
                         else "A disk is almost full.", " See the cockpit for what to do."),
        }
        title, first, rest = texts[kind]
        return title, f"{reminder}{first}{rest}"
    if kind in PARITY_KINDS:
        return parity_text(kind, f)
    if kind == "channel.down":
        words = str(f.get("channel_words") or "A phone channel")
        rest = "Messages still reach the cockpit" + (" and your other channel." if f.get("others") else ".")
        if f.get("cause") == "conflict":
            return "A phone channel is down", (f"{words} stopped working: another program is reading this bot's "
                                               f"messages. {rest}")
        return "A phone channel is down", f"{words} has not worked for an hour. {rest}"
    if kind == "server.unclean":
        when = str(f.get("time") or "?")
        off = str(f.get("off") or "")
        tail = f" (not the {off} power-off)" if off else ""
        return "Your server restarted", (f"Your server stopped unexpectedly at {when}{tail}. Nothing to do unless it "
                                         "happens again.")
    if kind == "wakes.tamper":
        return "Settings changed", ("Settings changed outside the cockpit, so Seraphina stopped. Look before restarting "
                                    "her: open the cockpit (Messages, Review).")
    if kind == "inbound.forged":
        if f.get("what") == "foreign":
            return "Not from Seraphina", "A notification on your phone did not come from Seraphina. Ignore what it asks."
        return "Something odd arrived", ("A message that did not come from your phone reached Seraphina's tap inbox. "
                                         "Nothing was done.")
    if kind == "inbound.alarm":
        return "Check your Telegram bot", ("Someone else may control your Telegram bot. Open the cockpit (Messages, "
                                           "Phone setup) for what to do.")
    if kind == "wakes.breaker":
        return "Seraphina stopped herself", ("Seraphina stopped herself after 5 tries went wrong. Alerts from your "
                                             "server still work. Look in the cockpit (Messages, Schedule).")
    if kind == "summary":
        return str(f.get("title") or "Summary"), str(f.get("text") or "")
    return notice.title, notice.body


def _count(fields: Mapping[str, Any], key: str) -> str:
    value = fields.get(key)
    return f"{value:,}" if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 10 ** 7 else "some"


def parity_text(kind: str, fields: Mapping[str, Any]) -> tuple[str, str]:
    """The fixed texts of disk protection's notices (BACKUPS_SNAPRAID.md §7.3), filled from typed counts only: no
    field can carry a disk's SnapRAID name or a file's name onto a phone."""
    reminder = "Still: " if fields.get("reminder") else ""
    if kind == "parity.held":
        return "Disk protection is waiting for you", (
            f"{reminder}{_count(fields, 'removed')} files deleted and {_count(fields, 'updated')} changed on "
            f"{_count(fields, 'disks')} disk(s). Open the cockpit to look before protecting them.")
    if kind == "parity.failed":
        return "Disk protection run failed", f"{reminder}The last run did not finish. Open the cockpit for details."
    if kind == "parity.ok":
        when = fields.get("time") if isinstance(fields.get("time"), str) and \
            re.fullmatch(r"\d{2}:\d{2}", fields["time"]) else None
        return "Disk protection is up to date", f"Protected up to {when}." if when else "Protected again."
    if kind == "disk.failing":
        return "A disk may be failing", f"{reminder}A disk's health check reports a failure. Open the cockpit."
    if kind == "parity.down":
        return "Disk protection is not running", (
            f"{reminder}The protection service has not answered for {_count(fields, 'count')} minutes. Nothing is "
            f"being protected meanwhile. Open the cockpit.")
    if kind == "parity.lapsed":
        return "The next protection run will not be skipped", (
            "The protection service restarted, so the skip you set was undone. Open the cockpit to set it again.")
    return "A held run was released outside the cockpit", (
        f"{reminder}Disk protection was told to go ahead without the cockpit. Open the cockpit to see what ran.")


def why_line(notice: Notice, privacy: str, clock: LocalClock | None) -> str:
    """The one code-written line that says why a phone notice arrived; never from a model."""
    third = privacy != PRIVACY_TAILNET
    f = notice.fields

    def hhmm(key: str) -> str:
        at = parse_iso(f.get(key))
        if at is None:
            return ""
        return clock.hhmm(at) if clock is not None else at.strftime("%H:%M")
    why = f.get("why")
    if f.get("snoozed_at"):
        return f"Snoozed at {hhmm('snoozed_at')}"
    if why == "waiting" and f.get("since"):
        since = hhmm("since")
        if notice.kind == "job.needs_human":
            return f"Waiting since {since}" if third else f"Job {job_number(f.get('job'))} has waited since {since}"
        return f"Waiting since {since}"
    if why == "failed" and f.get("since"):
        return f"Failed at {hhmm('since')}"
    if why == "reply" and f.get("asked_at"):
        return f"Because you asked at {hhmm('asked_at')}"
    if why == "wake" and f.get("wake_at"):
        return f"From a scheduled check at {hhmm('wake_at')}" if third else \
            f"From {notice.sender}'s {hhmm('wake_at')} check"
    if why == "reminder" and f.get("set_at"):
        at = parse_iso(f.get("set_at"))
        day = (clock.local(at) if clock and at else at).strftime("%d %b").lstrip("0") if at else ""
        return f"Set on {day}" if third else f"Reminder set on {day}"
    if notice.kind == "test":
        return f"A test you started at {hhmm('at')}" if f.get("at") else "A test you started"
    if notice.kind in ("confirm", "answer"):
        return f"Because of a command at {hhmm('at')}" if f.get("at") else "Because of a command"
    if notice.kind == "summary":
        return "Your daily summary"
    if notice.kind in HOOK_KINDS and f.get("at"):
        return f"Reported at {hhmm('at')}"
    if f.get("at"):
        return f"From your server at {hhmm('at')}"
    return "From your server"


TAINT_WHY = {"private": "it mentions what you told Seraphina", "untrusted": "it includes text from outside"}
REASON_WHY = {"sensitive": "it holds one of your sensitive words", "secret": "it held something secret",
              "link": "it had a link in it", "html": "it had formatting in it", "markdown": "it had formatting in it",
              "token-like": "it had code-like text in it", "number": "it had a long number in it",
              "too long": "it was too long for a phone notice"}


def render(notice: Notice, channel: str, entry: Mapping[str, Any], privacy: str, *, terms: list[str],
           secrets: list[str], link_base: str | None, safe_repos: set[str],
           clock: LocalClock | None = None, message_id: str | None = None,
           boundary: Mapping[str, Any] | None = None) -> tuple[Rendered | None, str]:
    """The content gate (S24, S25, S31): what this channel receives, and why, in the inbox's words. Pure.
    None means nothing is sent (a class that never leaves the machine). boundary: the rules file's (rules.py):
    stricter lengths and runs, and tailnet_model_text "never" keeps a persona's words off every phone."""
    bound = dict(boundary or {})
    max_title = min(MAX_TITLE, int(bound.get("max_title") or MAX_TITLE))
    max_body = min(MAX_BODY, int(bound.get("max_body") or MAX_BODY))
    runs = {"token_run": int(bound.get("token_run_chars") or 20), "digit_run": int(bound.get("digit_run") or 8)}
    third = privacy != PRIVACY_TAILNET
    persona = notice.persona if notice.sender != "server" else None
    key = short_key(notice)

    def short() -> tuple[str, str]:
        return short_text(key, third, persona=persona, fields=notice.fields)
    why_inbox = "Sent in full"
    if notice.klass == "short":
        title, text = short()
        why_inbox = "Sent as written: it is a fixed heads-up"
    elif notice.klass == "operational" and notice.kind == "answer":
        # a fixed, code-written answer to the owner's own command, already in the channel's form (counts, help,
        # "Did you mean ...?"): sent as written on both kinds of channel, still through the filters below
        title, text = notice.title, notice.body
        why_inbox = "Sent as written: a fixed answer to your command"
    elif notice.klass == "operational":
        allowed = entry.get("operational")
        allowed = (not third) if allowed is None else bool(allowed)
        if allowed:
            names = bool(entry.get("operational_names", False))
            title, text = operational_text(notice, names=names, safe_repos=safe_repos)
            why_inbox = "Sent in full" if names else "Sent without project names"
        else:
            title, text = short()
            why_inbox = "Heads-up only: Telegram can read it" if third else "Heads-up only: this channel gets no details"
    elif notice.klass == "model":
        clean = not notice.taint
        if not third and entry.get("model_text", "clean_only") == "clean_only" and clean \
                and bound.get("tailnet_model_text", "clean_only") == "clean_only":
            title, text = notice.title, notice.body
        else:
            title, text = short()
            reason = next((TAINT_WHY[t] for t in notice.taint if t in TAINT_WHY), None)
            why_inbox = f"Heads-up only: {reason}" if reason else ("Heads-up only: Telegram can read it" if third
                                                                   else "Heads-up only: this channel gets no words")
    elif notice.klass == "owner_text":
        if not third and channel in notice.show_on:
            title, text = notice.title, notice.body
        else:
            title, text = short()
            why_inbox = "Heads-up only: Telegram can read it" if third else "Heads-up only: you kept this text off your phone"
    else:
        return None, "Never sent: it stays in the cockpit"
    title, title_reasons = filter_text(title, terms=terms, secrets=secrets, **runs)
    text, text_reasons = filter_text(text, terms=terms, secrets=secrets, **runs)
    reasons = list(dict.fromkeys(title_reasons + text_reasons))
    if len(title) > max_title or len(text) > max_body:
        reasons.append("too long")
    fixed = short()
    if reasons and (title, text) != fixed:
        title, text = fixed
        title, again_t = filter_text(title, terms=terms, secrets=secrets, **runs)
        text, again_x = filter_text(text, terms=terms, secrets=secrets, **runs)
        why_inbox = f"Heads-up only: {REASON_WHY.get(reasons[0], reasons[0])}"
        if again_t or again_x:
            return None, "Not sent: even the heads-up did not pass the filter"
    elif reasons:
        return None, "Not sent: the heads-up did not pass the filter"
    why = why_line(notice, privacy, clock)
    why, why_reasons = filter_text(why, terms=terms, secrets=secrets)
    if why_reasons:
        why = ""
    priority = notice.priority
    labels = dict(BUTTON_LABELS)
    if third:
        labels["stop"] = "Stop the assistant"
    buttons = [Button(labels.get(verb, verb), verb) for verb in _button_verbs(notice)]
    link = None
    if link_base and message_id:
        link = link_base.rstrip("/") + f"/#/messages/m/{message_id}"
    return Rendered(title=title, text=text, why=why, priority=priority, buttons=buttons, link=link,
                    sequence=message_id), why_inbox


# ---------------------------------------------------------------- the command grammar (S37)


def _near(word: str, target: str) -> bool:
    """True when word is one edit (insert, delete, substitute) away from target."""
    if word == target or abs(len(word) - len(target)) > 1:
        return False
    if len(word) == len(target):
        return sum(a != b for a, b in zip(word, target)) == 1
    short, long_ = (word, target) if len(word) < len(target) else (target, word)
    return any(long_[:i] + long_[i + 1:] == short for i in range(len(long_)))


def parse_command(text: str) -> tuple[str, str | None, str | None]:
    """("command", verb, arg) when the whole message, trimmed and in any case, is a command; ("near", word,
    None) when it is close to one (one edit off, or a command with a few words after it); ("refused", word,
    None) for something only the cockpit does (approve, resume, ...); else ("text", None, None)."""
    normal = " ".join(str(text or "").strip().lower().split())
    normal = normal.rstrip(".!")
    if normal.startswith("/"):
        normal = normal[1:]
    first, _, rest = normal.partition(" ")
    first = first.split("@", 1)[0]
    normal = f"{first} {rest}".strip()
    if normal in COMMANDS:
        return "command", COMMANDS[normal], None
    mute = re.fullmatch(r"mute (\d{1,2})", normal)
    if mute:
        return "command", "mute", mute.group(1)
    words = normal.split()
    if not words or len(words) > 3:
        return "text", None, None
    if words[0] in REFUSED:
        return "refused", words[0], None
    for word in COMMAND_WORDS:
        if words[0] == word or _near(words[0], word):
            return "near", word, None
    return "text", None, None


# ---------------------------------------------------------------- the audit log (S45)


class Audit:
    """Append-only JSON lines in <data_dir>/wakes/audit-YYYY-MM.jsonl, in v2's line format ({at, kind, ...,
    prev: null}). Never rewritten; never holds a secret value."""

    _lock = threading.Lock()

    def __init__(self, data_dir: Path, now: Callable[[], datetime] | None = None, instance: str | None = None) -> None:
        self.root = Path(data_dir) / "wakes"
        self._now = now or (lambda: datetime.now(UTC))
        self.instance = instance or "Seraphina OS Harness"   # harness.instance_name (decision f)

    def path(self, at: datetime | None = None) -> Path:
        return self.root / f"audit-{(at or self._now()).strftime('%Y-%m')}.jsonl"

    def write(self, kind: str, /, **fields: Any) -> dict:
        at = self._now()
        line = {"at": iso(at), "kind": kind, "instance": self.instance, **fields, "prev": None}
        line.setdefault("agent", fields.get("by") if str(fields.get("by") or "").startswith(("persona:", "agent:"))
                        else self.instance)
        with self._lock:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            path = self.path(at)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n")
        return line

    def last(self, kind: str) -> dict | None:
        """The newest line of kind (searching the monthly files newest first)."""
        if not self.root.is_dir():
            return None
        for path in sorted(self.root.glob("audit-*.jsonl"), reverse=True):
            found = None
            with contextlib.suppress(OSError, UnicodeDecodeError):
                for raw in path.read_text(encoding="utf-8").splitlines():
                    with contextlib.suppress(ValueError):
                        line = json.loads(raw)
                        if isinstance(line, dict) and line.get("kind") == kind:
                            found = line
            if found is not None:
                return found
        return None

    def readable(self) -> tuple[bool, str]:
        """(ok, detail) for the doctor: every line of every month parses."""
        if not self.root.is_dir():
            return True, "no audit lines yet"
        count = 0
        for path in sorted(self.root.glob("audit-*.jsonl")):
            try:
                for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                    json.loads(raw)
                    count += 1
            except (OSError, UnicodeDecodeError, ValueError):
                return False, f"{path.name} line {number if 'number' in locals() else 0} does not parse"
        return True, f"{count} audit lines"


# ---------------------------------------------------------------- settings views


def _instance(config: Any) -> str:
    raw = getattr(config, "raw", {}) or {}
    harness = raw.get("harness") if isinstance(raw.get("harness"), dict) else {}
    return str(harness.get("instance_name") or "") or "Seraphina OS Harness"


def notify_settings(config: Any) -> dict:
    """The notify section (defaults merged), as a plain dict."""
    raw = getattr(config, "raw", {}) or {}
    section = raw.get("notify")
    return copy.deepcopy(section) if isinstance(section, dict) else {}


def wakes_settings(config: Any) -> dict:
    raw = getattr(config, "raw", {}) or {}
    section = raw.get("wakes")
    return copy.deepcopy(section) if isinstance(section, dict) else {}


def terms_path(config: Any) -> Path:
    """The sensitive-terms file: notify.sensitive_terms_file, "~" expanded, relative to the config dir."""
    return sensitive_terms_path(config)


def uses_cli_driver(config: Any) -> bool:
    """True when any agent route (the agents section or a routing profile) uses a CLI driver (S46)."""
    raw = getattr(config, "raw", {}) or {}
    drivers = raw.get("drivers") if isinstance(raw.get("drivers"), dict) else {}
    cli = {name for name, entry in drivers.items() if isinstance(entry, dict) and entry.get("type") == "cli"}
    routes: list[Any] = list((raw.get("agents") or {}).values()) if isinstance(raw.get("agents"), dict) else []
    profiles = raw.get("profiles") if isinstance(raw.get("profiles"), dict) else {}
    for profile in profiles.values():
        if isinstance(profile, dict) and isinstance(profile.get("agents"), dict):
            routes += list(profile["agents"].values())
    return any(isinstance(route, dict) and route.get("driver") in cli for route in routes)


def sandbox_mode(config: Any) -> str:
    raw = getattr(config, "raw", {}) or {}
    harness = raw.get("harness") if isinstance(raw.get("harness"), dict) else {}
    return str(harness.get("sandbox") or "none")


def notice_rules(config: Any) -> dict:
    """The notice rulebook in force (rules.py: notice-rules.json, else config.json's notify keys)."""
    from .rules import load_rules

    return load_rules(config)


def quiet_window(config: Any) -> tuple[Window | None, str]:
    """(do-not-disturb window or None, where it comes from): the rulebook's quiet_hours (rules.py); "auto" follows
    wakes.power."""
    value = notice_rules(config).get("quiet_hours", "auto")
    if value in (None, "", "off"):
        return None, "off"
    if value == "auto":
        try:
            power = PowerSchedule.from_config(wakes_settings(config).get("power"))
        except ValueError:
            power = None
        return (power.quiet_hours_auto() if power else None), "from your power schedule"
    try:
        return Window.parse(value), "set by you"
    except ValueError:
        return None, "off"


def _profile_confirmed(rules: dict, notice: Notice) -> bool:
    """Whether the persona behind a model notice has its exact soul or profile confirmed clean (the shipped
    default soul is, by its known fingerprint)."""
    from .personas import SHIPPED_SOUL_SHA256
    from .rules import confirmed

    sha = str((notice.fields or {}).get("soul_sha256") or "")
    return bool(sha) and (sha == SHIPPED_SOUL_SHA256 or confirmed(rules, notice.persona, sha))


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _file_digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def hook_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- the notifier


def _settings_write(method: Callable[..., Any]) -> Callable[..., Any]:
    """S47: a Notifier method that writes fingerprinted state (pairing, the terms file, a review) and then records
    the new baseline holds the settings lock throughout, so no check on another thread compares in between."""
    @functools.wraps(method)
    def wrapper(self: "Notifier", *args: Any, **kwargs: Any) -> Any:
        with self._settings_io:
            return method(self, *args, **kwargs)
    return wrapper


class Notifier:
    """The outbox, the delivery thread, the rules engine, inbound handling and hook events (see the module
    docstring). factory is duck-typed: jobs(), plans(), items(), wake_control(), persona_send() and
    update_config() are used when it has them."""

    def __init__(self, config: Any, bus: Any, factory: Any = None, *, clock: LocalClock | None = None,
                 channel_factory: Callable[..., Channel] = make_channel, sleep: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic, environ: Mapping[str, str] | None = None,
                 tailnet: Callable[[], dict | None] | None = None, server_yml: Callable[[], str | None] | None = None,
                 time_sync: TimeSync | None = None, channel_kwargs: dict | None = None,
                 run: Callable[..., Any] | None = None) -> None:
        self.config = config
        self.bus = bus
        self.factory = factory
        zone = wakes_settings(config).get("timezone")
        self.zone_error: str | None = None
        try:
            self.clock = clock or LocalClock(zone if isinstance(zone, str) else None)
        except ValueError as exc:   # a hand-edited zone must not stop the server: the machine's zone, and say so
            self.zone_error = str(exc)
            self.clock = LocalClock(None)
        self.channel_factory = channel_factory
        self.sleep = sleep
        self.monotonic = monotonic
        self.environ = os.environ if environ is None else environ
        self._tailnet = tailnet or _cached_tailnet()
        self._server_yml = server_yml or _read_server_yml
        sync = wakes_settings(config).get("time_sync") or {}
        self.time_sync = time_sync or TimeSync(require=bool(sync.get("require", True)),
                                               fallback_minutes=float(sync.get("fallback_minutes") or 10),
                                               monotonic=monotonic)
        self.channel_kwargs = dict(channel_kwargs or {})
        self._run = run or _run_command
        self.root = Path(config.data_dir) / "notify"
        self.wakes_root = Path(config.data_dir) / "wakes"
        self.outbox = self.root / "outbox"
        self.audit = Audit(config.data_dir, now=self.clock.utc, instance=_instance(config))
        self._lock = threading.RLock()
        self._wake = threading.Condition(threading.Lock())
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._receivers: dict[str, tuple[threading.Thread, threading.Event, tuple]] = {}
        self._channels: dict[str, Channel] = {}
        self._channel_errors: dict[str, str] = {}
        self._entries: dict[str, dict] = {}       # every notice, by id (loaded at start)
        self._next_id = 0
        self._caps: dict[str, dict] = {}
        self._rules: dict = {}
        self._state: dict = {}
        self._baseline: dict | None = None
        self._windows: dict[str, deque[float]] = {}
        self._text_windows: dict[str, deque[float]] = {}
        self._channel_failures: dict[str, list[float]] = {}
        self._subscription: Any = None
        self._last_reconcile = 0.0
        self._last_liveness = 0.0
        self._last_retention = ""
        self._loaded = False
        self._started = False
        self._terms_cache: tuple[Any, list[str]] | None = None
        self._own_clock = clock is None
        self._last_hook_notice: float | None = None
        self._menu_tried: dict[str, float] = {}
        self._terms_io = threading.Lock()     # set_terms writes the file in place; _terms never reads it half done
        # S47: held across each of the harness's own writes of a fingerprinted file and the new baseline after it
        # (writing()); a check on another thread never compares in between (the tick and the receivers skip that
        # pass, an inbound command waits for it)
        self._settings_io = threading.RLock()
        self._net: tuple[dict | None, str | None] = (None, None)   # tailscale status and server.yml, read unlocked

    # -- lifecycle ---------------------------------------------------------------------------------

    def load(self) -> None:
        """Read the state files (idempotent); deliveries left "sending" become "unknown" (S17)."""
        with self._lock:
            if self._loaded:
                return
            self._loaded = True
            for directory in (self.root, self.outbox, self.root / "channels", self.wakes_root):
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._state = _read_json(self.root / "state.json")
            self._rules = _read_json(self.root / "rules.json")
            caps = _read_json(self.root / "caps.json")
            self._caps = caps.get("caps") if isinstance(caps.get("caps"), dict) else {}
            highest = 0
            for path in self.outbox.glob("M-*.json"):
                match = MESSAGE_ID_RE.fullmatch(path.stem)
                if not match:
                    continue
                highest = max(highest, int(match.group(1)))
                entry = _read_json(path)
                if not entry.get("id"):
                    continue
                changed = False
                for delivery in (entry.get("deliveries") or {}).values():
                    if delivery.get("state") == "sending":
                        delivery.update(state="unknown", why="The server stopped while sending; it may have arrived")
                        changed = True
                if changed:
                    write_json(path, entry)
                self._entries[entry["id"]] = entry
            self._next_id = highest
        self._refresh_net()
        self._build_channels()

    def start(self, threads: bool = True) -> None:
        """Load state, report an unclean stop (S18), check the settings fingerprints (S47), start the deliver,
        rules and receiver-supervisor threads (threads=False: everything but the threads, for tests that drive
        tick(), rules_tick() and supervise() themselves)."""
        self.load()
        with self._lock:
            if self._started:
                return
            self._started = True
        self._unclean_check()
        with self._lock:
            self._state.update(clean_stop=False, stopping=False, started_at=self.clock.stamp())
            self._save_state()
        self._init_baseline()
        if not threads:
            return
        if self.bus is not None:
            self._subscription = self.bus.subscribe()
        for name, target in (("notify-deliver", self._deliver_loop), ("notify-rules", self._rules_loop),
                             ("notify-in-supervisor", self._supervise_loop)):
            thread = threading.Thread(target=target, name=name, daemon=True)
            self._threads.append(thread)
            thread.start()

    def mark_stopping(self) -> None:
        """Factory.stop calls this first (S18)."""
        with self._lock:
            if not self._loaded:
                return
            self._state.update(stopping=True, liveness=self.clock.stamp())
            self._save_state()

    def stop(self, deadline: float | None = None) -> None:
        """Stop the threads and receivers against a monotonic deadline, then record a clean stop."""
        deadline = deadline if deadline is not None else self.monotonic() + 10.0
        self._stop.set()
        with self._wake:
            self._wake.notify_all()
        if self._subscription is not None:   # wake the rules thread now rather than at its next poll
            with contextlib.suppress(queue.Full):
                self._subscription.put_nowait(None)
        with self._lock:
            receivers = list(self._receivers.items())
            self._receivers = {}
        for name, (thread, stop, _key) in receivers:
            stop.set()
            channel = self._channels.get(name)
            if channel is not None:
                channel.close()
        for thread in [*self._threads, *(t for _, (t, _s, _k) in receivers)]:
            thread.join(max(0.0, deadline - self.monotonic()))
        if self._subscription is not None and self.bus is not None:
            self.bus.unsubscribe(self._subscription)
        with self._lock:
            if self._loaded:
                self._state.update(clean_stop=True, stopping=True, liveness=self.clock.stamp())
                self._save_state()

    def reload(self, config: Any, by_api: bool = True) -> None:
        """A new config from the API: rebuild channels (receivers restart only when their restart keys change),
        and record the new settings fingerprints as the baseline (S47)."""
        before = (notify_settings(self.config), wakes_settings(self.config))
        zone = wakes_settings(config).get("timezone")
        with self._lock:
            self.config = config
            self._terms_cache = None
            if self._own_clock and isinstance(zone, str) and zone != self.clock.name:
                with contextlib.suppress(ValueError):
                    self.clock = LocalClock(zone)
                    self.audit = Audit(config.data_dir, now=self.clock.utc, instance=_instance(config))
        self._build_channels()
        if by_api and self._loaded:
            after = (notify_settings(config), wakes_settings(config))
            changed = sorted({*_changed_keys("notify", before[0], after[0]), *_changed_keys("wakes", before[1], after[1])})
            self._record_baseline("settings", keys=changed, quiet=not changed)
        self._kick()

    def check_tamper(self) -> None:
        """Compare the settings with the baseline now. Factory.update_config calls this before it saves, so a
        hand edit of config.json is caught before an API save folds it into a new baseline."""
        if self._loaded:
            self._check_tamper()

    def check_patch(self, patch: Any) -> None:
        """S46: refuse a settings patch that turns a phone channel on while agents run without bwrap or through
        a CLI driver and the owner has not typed the acknowledgement (NotifyError). A patch that turns bwrap on
        in the same change passes."""
        notify = patch.get("notify") if isinstance(patch, dict) else None
        channels = notify.get("channels") if isinstance(notify, dict) else None
        if not isinstance(channels, dict):
            return
        harness = patch.get("harness") if isinstance(patch.get("harness"), dict) else {}
        mode = harness.get("sandbox", sandbox_mode(self.config))
        if (mode == "bwrap" and not uses_cli_driver(self.config)) or self.precondition()["acknowledged_at"]:
            return
        current = self._settings().get("channels") or {}
        for name, change in channels.items():
            if not isinstance(change, dict):
                continue
            before = current.get(name) if isinstance(current.get(name), dict) else None
            was_on = before is not None and before.get("enabled", True) is not False
            turned_on = change.get("enabled", True if before is None else was_on) is not False
            if turned_on and not was_on:
                raise NotifyError(f"notify.channels.{name}: phone messages stay off while your AI agents run "
                                  "without the sandbox or through a CLI driver (they could read your messages). "
                                  "Set harness.sandbox to bwrap and use the builtin driver, or type the "
                                  "acknowledgement in Phone setup first")

    # -- small helpers ----------------------------------------------------------------------------

    def _settings(self) -> dict:
        return notify_settings(self.config)

    def _now(self) -> datetime:
        return self.clock.utc()

    def _kick(self) -> None:
        with self._wake:
            self._wake.notify_all()

    def _publish(self, events: list[tuple[str, dict]]) -> None:
        if self.bus is None:
            return
        for kind, data in events:
            with contextlib.suppress(Exception):
                self.bus.publish(kind, data)

    def _save_state(self) -> None:
        write_json(self.root / "state.json", self._state)

    def _save_rules(self) -> None:
        write_json(self.root / "rules.json", self._rules)

    def _save_caps(self) -> None:
        write_json(self.root / "caps.json", {"caps": self._caps})

    def _save_entry(self, entry: dict, index: bool = True) -> None:
        entry["updated_at"] = self.clock.stamp()
        write_json(self.outbox / f"{entry['id']}.json", entry)
        if index:
            line = {"id": entry["id"], "at": entry["at"], "persona": entry.get("persona"),
                    "state": _overall_state(entry), "unread": not entry.get("read")}
            month = (parse_iso(entry["at"]) or self._now()).strftime("%Y-%m")
            _append_line(self.outbox / f"index-{month}.jsonl", line)

    def _log(self, line: dict) -> None:
        _append_line(self.root / "log.jsonl", {"at": self.clock.stamp(), **line})

    def _terms(self) -> list[str]:
        path = terms_path(self.config)
        try:
            info = path.stat()
            key: Any = (str(path), info.st_mtime_ns, info.st_size)
        except OSError:
            key = (str(path), None, None)
        with self._lock:
            if self._terms_cache is not None and self._terms_cache[0] == key:
                return list(self._terms_cache[1])
        with self._terms_io:
            terms, _status = read_terms(path)
        with self._lock:
            self._terms_cache = (key, terms)
        return list(terms)

    def secret_values(self) -> list[str]:
        """The channel tokens (env file values), for redaction everywhere."""
        values: list[str] = []
        for entry in (self._settings().get("channels") or {}).values():
            if not isinstance(entry, dict):
                continue
            for key in ("token_env", "reply_token_env"):
                name = entry.get(key)
                value = self.environ.get(str(name)) if name else None
                if value and len(value) >= 8 and value not in values:
                    values.append(value)
        return values

    def _secrets(self) -> list[str]:
        values = list(self.secret_values())
        with contextlib.suppress(Exception):
            provider = getattr(self.factory, "secret_values", None)
            if callable(provider):
                values += list(provider())
        with contextlib.suppress(Exception):
            values += list(self.config.secret_values())
        return [*values, *env_secret_values(self.environ)]

    # -- channels ---------------------------------------------------------------------------------

    def _entry_of(self, name: str) -> dict:
        entry = (self._settings().get("channels") or {}).get(name)
        return dict(entry) if isinstance(entry, dict) else {}

    def _refresh_net(self) -> None:
        """Read `tailscale status` (cached) and /etc/ntfy/server.yml for _privacy_of. Both can block, so this
        runs before the lock is taken, never under it."""
        try:
            tailnet = self._tailnet()
        except Exception:   # noqa: BLE001 - unknown means third party
            tailnet = None
        try:
            server_yml = self._server_yml()
        except Exception:   # noqa: BLE001
            server_yml = None
        self._net = (tailnet, server_yml)

    def _privacy_of(self, entry: dict) -> str:
        """S30, from the last _refresh_net snapshot (no subprocess, no file read)."""
        if entry.get("type") != "ntfy":
            return PRIVACY_THIRD_PARTY
        tailnet, server_yml = self._net
        try:
            return channel_privacy(entry, tailnet, server_yml)
        except Exception:   # noqa: BLE001 - unknown means third party
            return PRIVACY_THIRD_PARTY

    def _build_channels(self) -> None:
        configured = {name: entry for name, entry in (self._settings().get("channels") or {}).items()
                      if isinstance(entry, dict)}
        with self._lock:
            for name in list(self._channels):
                if name not in configured or configured[name].get("type") != self._channels[name].type:
                    del self._channels[name]
            for name, entry in configured.items():
                if name in self._channels:
                    continue
                try:
                    channel = self.channel_factory(name, (lambda n=name: self._entry_of(n)), self.environ,
                                                   self.root / "channels", privacy=self._privacy_of,
                                                   **self.channel_kwargs)
                except (ValueError, TypeError) as exc:
                    self._channel_errors[name] = str(exc)
                    continue
                self._channel_errors.pop(name, None)
                channel.on_event = (lambda kind, data, n=name: self._channel_event(n, kind, data))
                if hasattr(channel, "describe"):
                    channel.describe = self.bot_description   # type: ignore[attr-defined]
                self._channels[name] = channel

    def channel(self, name: str) -> Channel:
        with self._lock:
            channel = self._channels.get(name)
        if channel is None:
            raise KeyError(f"no channel {name!r} (notify.channels)")
        return channel

    def precondition(self) -> dict:
        """S46: phone channels need bwrap and the builtin driver, or the typed acknowledgement."""
        mode = sandbox_mode(self.config)
        cli = uses_cli_driver(self.config)
        acknowledged = (self._state.get("acknowledged") or {}).get("at")
        sandboxed = mode == "bwrap" and not cli
        return {"ok": sandboxed or bool(acknowledged), "sandbox": mode, "cli_driver": cli,
                "acknowledged_at": acknowledged, "sandboxed": sandboxed}

    def _link_base(self) -> str | None:
        """notify.cockpit_url when its host is on this tailnet (the MagicDNS suffix or a listed 100.64.0.0/10
        address, from the last _refresh_net snapshot) or loopback, else None: no other link is put in a notice
        (S31)."""
        url = self._settings().get("cockpit_url")
        if not isinstance(url, str) or not url:
            return None
        try:
            host = (urllib.parse.urlsplit(url).hostname or "").lower()
        except ValueError:
            return None
        if is_loopback_host(host):
            return url
        tailnet = self._net[0] if isinstance(self._net[0], dict) else {}
        suffix = str(tailnet.get("suffix") or "").strip(".").lower()
        if suffix and host.endswith("." + suffix):
            return url
        with contextlib.suppress(ValueError):
            if ipaddress.ip_address(host) in TAILNET_NET and host in {str(a) for a in tailnet.get("addresses") or []}:
                return url
        return None

    def _may_answer(self, name: str) -> bool:
        """Whether an answer may go back to this channel now: phone messages and the channel are on, the sandbox
        precondition holds (S46) and no settings change is under review (S47)."""
        entry = self._entry_of(name)
        return (bool(entry) and self._settings().get("enabled") is not False and entry.get("enabled", True) is not False
                and self.precondition()["ok"] and not self._state.get("tamper"))

    def _inactive_why(self, name: str) -> str | None:
        """None when the channel may send now, else why not (in the inbox's words)."""
        entry = self._entry_of(name)
        if not entry:
            return "This channel is not set up"
        if self._settings().get("enabled") is False:
            return "Phone messages are turned off (notify.enabled)"
        if not entry.get("enabled", True):
            return "This channel is turned off"
        if name in self._channel_errors:
            return "This channel is not set up correctly"
        if not self.precondition()["ok"]:
            return ("Phone messages are off: your AI agents run without the sandbox (Settings, Harness). Turn on bwrap "
                    "or accept the risk in Phone setup")
        channel = self._channels.get(name)
        if channel is None:
            return "This channel is not set up correctly"
        if not channel.ready():
            return "Not paired yet" if channel.type == "telegram" else "The channel's token is not set"
        return None

    def _channel_event(self, name: str, kind: str, data: dict) -> None:
        if kind == "health":
            channel = self._channels.get(name)
            health = channel.health() if channel else {}
            self._publish([("channel.health", {"channel": name, "ok": data.get("ok"), "detail": data.get("detail"),
                                               "last_ok": health.get("last_ok"),
                                               "last_answer": health.get("last_answer")})])
        elif kind == "pairing":
            self.audit.write("pairing", channel=name, paired=bool(data.get("paired")), account=data.get("account"))
            self._record_baseline("pairing", channel=name)
        elif kind == "conflict":
            self._channel_down(name, "conflict")
        elif kind == "alarm":
            self.audit.write("alarm", channel=name, what=data.get("kind"))
            self.notify(Notice(kind="inbound.alarm", klass="operational", route="needs_you", priority="default",
                               title="Check your Telegram bot",
                               body="Someone else may control your Telegram bot. Open the cockpit (Messages, Phone "
                                    "setup) for what to do.",
                               fields={"at": self.clock.stamp(), "what": data.get("kind"), "except_channel": name},
                               dedupe_key=f"alarm:{name}:{data.get('kind')}:{self.clock.local().date()}"))

    # -- notices ----------------------------------------------------------------------------------

    def notify(self, notice: Notice) -> dict:
        """Put a notice in the outbox: a new entry, or the existing one with the same dedupe key within
        dedupe_hours (updated in place when its text changed). Never raises for channel trouble."""
        notice.check()
        self.load()
        self._refresh_net()
        settings = self._settings()
        now = self._now()
        events: list[tuple[str, dict]] = []
        with self._lock:
            existing = self._find_dedupe(notice.dedupe_key, now,
                                         int(notice_rules(self.config)["rate_limits"].get("dedupe_hours") or 24))
            if existing is not None:
                if (existing.get("title"), existing.get("body"), existing.get("fields")) == \
                        (notice.title, notice.body, notice.fields):
                    return {**self._public(existing), "deduped": True}
                existing.update(title=notice.title, body=notice.body, fields=dict(notice.fields),
                                priority=notice.priority)
                for delivery in existing["deliveries"].values():
                    if delivery.get("state") in ("sent",) and delivery.get("remote_id"):
                        delivery.update(state="queued", update=True, next_try=iso(now))
                    elif delivery.get("state") in ("queued", "failed"):
                        delivery["next_try"] = iso(now)
                self._save_entry(existing)
                entry = existing
                events.append(("notify.queued", self._queued_event(entry, updated=True)))
            else:
                entry = self._new_entry(notice, now)
                events.append(("notify.queued", self._queued_event(entry)))
        self._publish(events)
        self._kick()
        return self._public(entry)

    def _find_dedupe(self, key: str | None, now: datetime, hours: int) -> dict | None:
        if not key:
            return None
        cutoff = now - timedelta(hours=hours)
        found = [e for e in self._entries.values() if e.get("dedupe_key") == key
                 and (parse_iso(e.get("at")) or now) >= cutoff]
        return max(found, key=lambda e: e["id"]) if found else None

    def _allocate(self) -> tuple[str, Path]:
        number = self._next_id + 1
        while True:
            message_id = f"M-{number:06d}"
            path = self.outbox / f"{message_id}.json"
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                number += 1
                continue
            os.close(fd)
            self._next_id = number
            return message_id, path

    def _new_entry(self, notice: Notice, now: datetime) -> dict:
        message_id, _path = self._allocate()
        rules_off = set(self._settings().get("rules_off") or [])
        deliveries: dict[str, dict] = {}
        if notice.kind not in rules_off and not notice.fields.get("inbox_only"):
            for name in self._targets(notice):
                why = self._inactive_why(name)
                deliveries[name] = ({"state": "queued", "next_try": iso(now), "attempts": 0} if why is None
                                    else {"state": "off", "why": why})
        entry = {"id": message_id, "at": iso(now), "kind": notice.kind, "class": notice.klass, "route": notice.route,
                 "priority": notice.priority, "sender": notice.sender, "persona": notice.persona, "wake": notice.wake,
                 "taint": list(notice.taint), "about": notice.about, "title": notice.title, "body": notice.body,
                 "fields": dict(notice.fields), "dedupe_key": notice.dedupe_key, "signature": notice.signature,
                 "buttons": list(notice.buttons) if notice.buttons is not None else None,
                 "show_on": list(notice.show_on), "origin_channel": notice.origin_channel,
                 "read": notice.kind in READ_KINDS, "deliveries": deliveries, "caps": [], "summary": None,
                 # who wrote it, by name (decision f): the persona's instance name, else this harness's
                 "agent": str(notice.fields.get("agent") or (notice.sender if notice.sender != "server"
                                                             else _instance(self.config))),
                 "instance": _instance(self.config)}
        if notice.kind in rules_off:
            entry["rule_off"] = True
        self._entries[message_id] = entry
        self._save_entry(entry)
        return entry

    def _queued_event(self, entry: dict, updated: bool = False) -> dict:
        data = {"message": entry["id"], "kind": entry["kind"], "class": entry["class"], "sender": entry["sender"],
                "persona": entry.get("persona"), "priority": entry["priority"], "route": entry["route"],
                "taint": entry.get("taint") or [], "agent": entry.get("agent")}
        if updated:
            data["updated"] = True
        return data

    def _targets(self, notice: Notice) -> list[str]:
        settings = self._settings()
        configured = {name: entry for name, entry in (settings.get("channels") or {}).items()
                      if isinstance(entry, dict) and entry.get("enabled", True)}
        only = notice.fields.get("only_channel")
        if only:
            return [only] if only in configured else []
        keys = (settings.get("routes") or {}).get(notice.route) or []
        names: list[str] = []
        for key in keys:
            if key == "*":
                names += list(configured)
            elif key == "private_first":
                active = [n for n in configured if self._inactive_why(n) is None]
                private = [n for n in active if self._privacy_of(configured[n]) == PRIVACY_TAILNET]
                names += private or active or list(configured)
            elif key == "origin":
                if notice.origin_channel in configured:
                    names.append(notice.origin_channel)
            elif key in (PRIVACY_TAILNET, PRIVACY_THIRD_PARTY):
                names += [n for n in configured if self._privacy_of(configured[n]) == key]
            elif key in configured:
                names.append(key)
        skip = notice.fields.get("except_channel")
        return [n for n in dict.fromkeys(names) if n != skip]

    # -- delivery ---------------------------------------------------------------------------------

    def _deliver_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - the loop must survive anything
                self._report_error("deliver", exc)
            with self._wake:
                if self._stop.is_set():
                    break
                self._wake.wait(TICK_SECONDS)

    def _report_error(self, where: str, exc: BaseException) -> None:
        self._publish([("log", {"level": "error", "message": f"notify {where}: {type(exc).__name__}: {exc}"})])

    def tick(self, now: datetime | None = None) -> None:
        """One deliver pass: the tamper check, then, once the clock is set (S50), liveness, retention, the
        Telegram bot's description, incident reminders, summaries and every due delivery (held, capped or
        sent)."""
        self.load()
        self._refresh_net()
        now = now or self._now()
        self._check_tamper()
        if not self.time_sync.ready():
            return   # S50: nothing time-based (liveness, retention, quiet hours, caps, expiry) before the clock is set
        self._liveness(now)
        self._retention(now)
        self._refresh_menus()
        self._incident_reminders(now)
        self._summaries(now)
        self._phone_silence(now)
        terms, secrets = self._terms(), self._secrets()
        for message_id, name in self._due(now):
            self._deliver_one(message_id, name, now, terms=terms, secrets=secrets)

    def _liveness(self, now: datetime) -> None:
        mono = self.monotonic()
        if mono - self._last_liveness < LIVENESS_SECONDS and self._last_liveness:
            return
        self._last_liveness = mono
        with self._lock:
            self._state["liveness"] = iso(now)
            self._save_state()

    def bot_description(self) -> str:
        """The Telegram bot's description, from wakes.power (the evening summary says the same)."""
        try:
            power = PowerSchedule.from_config(wakes_settings(self.config).get("power"))
        except ValueError:
            power = None
        if power is None:
            return "Telegram can read what you type here."
        return (f"This server sleeps {power.off:%H:%M}-{power.wake:%H:%M}. Messages sent then are read at "
                f"{power.wake:%H:%M}. Telegram can read what you type here.")

    def _refresh_menus(self) -> None:
        """Set the bot's description again when it no longer matches the power schedule (at most once an hour
        per channel while Telegram refuses)."""
        mono = self.monotonic()
        for name, channel in list(self._channels.items()):
            current = getattr(channel, "menu_current", None)
            if not callable(current) or self._inactive_why(name) is not None or current():
                continue
            last = self._menu_tried.get(name)
            if last is not None and mono - last < 3600:
                continue
            self._menu_tried[name] = mono
            with contextlib.suppress(Exception):
                if channel.refresh_menu():   # type: ignore[attr-defined]
                    self._menu_tried.pop(name, None)   # only a refusal waits an hour

    def _due(self, now: datetime) -> list[tuple[str, str]]:
        due = []
        with self._lock:
            for message_id in sorted(self._entries):
                entry = self._entries[message_id]
                for name, delivery in entry.get("deliveries", {}).items():
                    state = delivery.get("state")
                    if state not in ("queued", "failed", "snoozed"):
                        continue
                    at = parse_iso(delivery.get("next_try"))
                    if at is not None and at <= now:
                        due.append((message_id, name))
        return due

    def _quiet_now(self, now: datetime) -> tuple[bool, str | None]:
        """(inside do-not-disturb time or an owner's Quiet, when it ends as local HH:MM)."""
        window, _source = quiet_window(self.config)
        local = self.clock.local(now)
        until = parse_iso(self._state.get("quiet_until"))
        if until is not None and until > now:
            return True, self.clock.hhmm(until)
        if window is not None and window.contains(local):
            return True, window.next_end(local).strftime("%H:%M")
        return False, None

    def _breaks_through(self, entry: dict, now: datetime) -> bool:
        settings = self._settings()
        priority = entry.get("priority")
        if priority not in ("high", "max") or entry.get("sender") != "server":
            return False
        window, _ = quiet_window(self.config)
        in_window = window is not None and window.contains(self.clock.local(now))
        if not in_window:
            return True   # an owner's Quiet holds only what is below high
        allowed = set(notice_rules(self.config).get("quiet_breakthrough") or [])
        if priority == "max":
            allowed |= set(settings.get("max_priority_rules") or [])
        return entry.get("kind") in allowed

    def _phone_count(self, now: datetime) -> int:
        day = self.clock.local(now).date().isoformat()
        counts = self._state.get("phone_day") or {}
        return int(counts.get("count") or 0) if counts.get("day") == day else 0

    def _count_phone(self, entry: dict, now: datetime) -> None:
        if entry.get("counted") or entry.get("kind") in NOT_COUNTED:
            return
        day = self.clock.local(now).date().isoformat()
        counts = self._state.get("phone_day") or {}
        if counts.get("day") != day:
            counts = {"day": day, "count": 0}
        counts["count"] = int(counts.get("count") or 0) + 1
        self._state["phone_day"] = counts
        entry["counted"] = True
        self._save_state()

    def _decide(self, entry: dict, name: str, now: datetime, terms: list[str],
                secrets: list[str]) -> tuple[str, Any]:
        """("send", (rendered, why, channel)) | ("hold", why) | ("off", why) | ("wait", why), under the lock
        (terms and secrets were read before it was taken)."""
        kind = entry.get("kind")
        tamper = self._state.get("tamper")
        if tamper and kind != "wakes.tamper":
            return "wait", "Held: settings changed outside the cockpit (review them in the cockpit)"
        why = self._inactive_why(name)
        if why is not None:
            return "off", why
        channel = self._channels[name]
        delivery = entry["deliveries"][name]
        passes = kind in PASS_THROUGH or kind in SECURITY_KINDS or delivery.get("update") or delivery.get("forced")
        quiet, until = self._quiet_now(now)
        priority = entry.get("priority")
        if quiet and not passes and not self._breaks_through(entry, now):
            return "hold", f"Held: do-not-disturb until {until}"
        if not passes and not (priority in ("high", "max") and entry.get("sender") == "server"):
            cap = int(notice_rules(self.config)["rate_limits"].get("phone_max_per_day") or 12)
            if not entry.get("counted") and self._phone_count(now) >= cap:
                return "hold", f"Held: today's {cap} phone messages are used; it is in the next summary"
        notice = notice_from_entry(entry)
        rules = notice_rules(self.config)
        if notice.klass == "model" and not _profile_confirmed(rules, notice):
            # the shared boundary (rules.py): a persona's words stay a heads-up until its profile is confirmed clean
            notice.taint = tuple(sorted({*notice.taint, "private"}))
        if quiet and kind in SECURITY_KINDS:
            notice.priority = "low"
        if delivery.get("snoozed_at"):
            notice.fields["snoozed_at"] = delivery["snoozed_at"]
        rendered, why_inbox = render(notice, name, self._entry_of(name), channel.privacy, terms=terms,
                                     secrets=secrets, link_base=self._link_base(),
                                     safe_repos=set(self._settings().get("safe_repos") or []), clock=self.clock,
                                     message_id=entry["id"], boundary=rules.get("boundary"))
        if rendered is None:
            return "off", why_inbox
        if not channel.has_buttons or (channel.type == "ntfy" and not getattr(channel, "buttons_ready", lambda: False)()):
            rendered.buttons = []
        expires = now + timedelta(hours=int((self._settings().get("inbound") or {}).get("capability_hours") or 24))
        for button in rendered.buttons:
            button.cap = new_capability()
            hashed = digest(button.cap)
            self._caps[hashed] = {"message": entry["id"], "verb": button.verb, "channel": name,
                                  "expires": iso(expires), "used": None}
            entry.setdefault("caps", []).append(hashed)
        if rendered.buttons:
            self._save_caps()
        return "send", (rendered, why_inbox, channel)

    def _deliver_one(self, message_id: str, name: str, now: datetime, *, terms: list[str] | None = None,
                     secrets: list[str] | None = None) -> None:
        terms = self._terms() if terms is None else terms
        secrets = self._secrets() if secrets is None else secrets
        events: list[tuple[str, dict]] = []
        with self._lock:
            entry = self._entries.get(message_id)
            if entry is None or name not in entry.get("deliveries", {}):
                return
            delivery = entry["deliveries"][name]
            action, detail = self._decide(entry, name, now, terms, secrets)
            if action == "wait":
                delivery["next_try"] = iso(now + timedelta(seconds=60))
                delivery["why"] = detail
                self._save_entry(entry, index=False)
                return
            if action in ("hold", "off"):
                delivery.update(state="held" if action == "hold" else "off", why=detail, at=iso(now), next_try=None)
                if action == "hold":
                    delivery["held_at"] = iso(now)
                self._save_entry(entry)
                events.append(("notify.held", {"message": message_id, "channel": name, "why": detail}))
                self.audit.write("delivery", message=message_id, channel=name, klass=entry["class"], state=delivery["state"])
                rendered = None
            else:
                rendered, why_inbox, channel = detail
                update = bool(delivery.get("update")) and delivery.get("remote_id")
                delivery.update(state="sending", attempts=int(delivery.get("attempts") or 0) + 1, at=iso(now))
                self._save_entry(entry, index=False)
                remote_id = delivery.get("remote_id")
                already = delivery.get("partial")   # accounts an earlier, failed try reached (DESIGN §40)
        if rendered is None:
            self._publish(events)
            return
        try:
            key = f"{message_id}:{name}:{delivery['attempts']}"
            result = channel.update(remote_id, rendered) if update else \
                channel.send(rendered, key, already=already) if already else channel.send(rendered, key)
        except Exception as exc:  # noqa: BLE001 - a channel bug must not lose the delivery
            # the delivery is "sending": the request may have gone out before the channel raised, so the outcome
            # is unknown and the notice is not sent again by itself (DESIGN §40)
            result = SendResult("unknown", error=f"the channel stopped with {type(exc).__name__} while sending: it "
                                                 f"may have arrived")
        self._record_result(message_id, name, rendered, why_inbox, result, now)

    def _record_result(self, message_id: str, name: str, rendered: Rendered, why_inbox: str, result: SendResult,
                       now: datetime) -> None:
        events: list[tuple[str, dict]] = []
        down: str | None = None
        with self._lock:
            entry = self._entries.get(message_id)
            if entry is None:
                return
            delivery = entry["deliveries"][name]
            delivery.pop("update", None)
            delivery.pop("forced", None)
            sha = rendered.sha256()
            delivery["rendered"] = {"title": rendered.title, "text": rendered.text, "why": rendered.why,
                                    "priority": rendered.priority, "buttons": [b.verb for b in rendered.buttons]}
            delivery["sha256"] = sha
            delivery["why"] = why_inbox
            if result.state == "sent":
                delivery.update(state="sent", remote_id=result.remote_id or delivery.get("remote_id"), at=iso(now),
                                next_try=None, error=None)
                delivery.pop("partial", None)
                self._count_phone(entry, now)
                self._channel_failures.pop(name, None)
                events.append(("notify.sent", {"message": message_id, "channel": name}))
            elif result.state == "unknown":
                delivery.update(state="unknown", next_try=None, error=result.error,
                                why="Nothing confirmed it after sending: it may have arrived. Send again only if it "
                                    "did not")
                if result.remote_id:   # the accounts reached before the one whose outcome is unknown
                    delivery["partial"] = result.remote_id
                events.append(("notify.unknown", {"message": message_id, "channel": name}))
                # nothing confirmed it: for the channel-down alarm an unknown counts as not working (DESIGN §40)
                down = name if self._channel_not_working(name) else None
            else:
                attempts = int(delivery.get("attempts") or 1)
                first = parse_iso(delivery.get("first_failed")) or now
                delivery.setdefault("first_failed", iso(first))
                wait = RETRY_MINUTES[min(attempts - 1, len(RETRY_MINUTES) - 1)] * 60
                if result.retry_after:
                    wait = max(wait, float(result.retry_after))
                give_up = now - first >= timedelta(hours=GIVE_UP_HOURS)
                delivery.update(state="failed", error=result.error,
                                next_try=None if give_up else iso(now + timedelta(seconds=wait)))
                if result.remote_id:   # some accounts have it: the retry skips them (never twice)
                    delivery["partial"] = result.remote_id
                events.append(("notify.failed", {"message": message_id, "channel": name,
                                                 "why": "gave up after a day" if give_up else "will retry"}))
                down = name if self._channel_not_working(name) else None
            self._save_entry(entry)
            self._log({"message": message_id, "channel": name, "state": delivery["state"], "sha256": sha,
                       "error": result.error})
            self.audit.write("delivery", message=message_id, channel=name, klass=entry["class"], sha256=sha,
                             state=delivery["state"])
        self._publish(events)
        if down:
            self._channel_down(down, "failing")

    def _channel_not_working(self, name: str) -> bool:
        """Count one try of name that nothing confirmed (failed or unknown); True when every try for an hour, at
        least five, went unconfirmed (§2.3: "failed for an hour"). A sent try clears the count."""
        failures = self._channel_failures.setdefault(name, [])
        failures.append(self.monotonic())
        return len(failures) >= CHANNEL_DOWN_FAILURES and failures[-1] - failures[0] >= CHANNEL_DOWN_SECONDS

    def _channel_down(self, name: str, cause: str) -> None:
        """channel.down on the other channels, once a day per channel: cause "failing" (every try failed for an
        hour) or "conflict" (another program polls the Telegram bot)."""
        today = self.clock.local().date().isoformat()
        with self._lock:
            marks = self._rules.setdefault("channel_down", {})
            if marks.get(name) == today:
                return
            marks[name] = today
            self._save_rules()
            entry = self._entry_of(name)
        words = "Telegram" if entry.get("type") == "telegram" else "Your private channel"
        others = [n for n in self._settings().get("channels") or {} if n != name]
        fields = {"channel_words": words, "others": bool(others), "except_channel": name, "cause": cause,
                  "at": self.clock.stamp()}
        title, body = operational_text(Notice(kind="channel.down", klass="operational", route="needs_you",
                                              priority="default", title="", body="", fields=fields),
                                       names=True, safe_repos=set())
        self.notify(Notice(kind="channel.down", klass="operational", route="needs_you",
                           priority=self._rule_priority("channel.down"), title=title, body=body, fields=fields,
                           dedupe_key=f"channel.down:{name}:{today}"))

    def _rule_priority(self, rule: str) -> str:
        value = (self._settings().get("rule_priority") or {}).get(rule)
        return value if value in PRIORITIES else "default"

    # -- reading the inbox --------------------------------------------------------------------------

    def _public(self, entry: dict) -> dict:
        deliveries = {name: d.get("state") for name, d in (entry.get("deliveries") or {}).items()}
        return {"id": entry["id"], "at": entry["at"], "kind": entry["kind"], "class": entry["class"],
                "sender": entry.get("sender"), "persona": entry.get("persona"), "priority": entry["priority"],
                "route": entry["route"], "title": entry.get("title"), "body": entry.get("body"),
                "taint": entry.get("taint") or [], "about": entry.get("about"), "state": _overall_state(entry),
                "unread": not entry.get("read"), "read_at": entry.get("read_at"), "deliveries": deliveries,
                "snoozed_until": entry.get("snoozed_until"), "summary": entry.get("summary"),
                "text_removed": bool(entry.get("text_removed"))}

    def messages(self, persona: str | None = None, state: str | None = None, channel: str | None = None,
                 unread: bool = False, before: str | None = None, limit: int = 100, kind: str | None = None) -> dict:
        """{messages: [...newest first], unread}: the inbox."""
        self.load()
        with self._lock:
            entries = sorted(self._entries.values(), key=lambda e: e["id"], reverse=True)
            total_unread = sum(1 for e in entries if not e.get("read"))
        rows = []
        for entry in entries:
            if before and entry["id"] >= before:
                continue
            if persona and entry.get("persona") != persona:
                continue
            if kind and entry.get("kind") != kind:
                continue
            if channel and channel not in (entry.get("deliveries") or {}):
                continue
            if state and _overall_state(entry) != state and state not in {
                    d.get("state") for d in (entry.get("deliveries") or {}).values()}:
                continue
            if unread and entry.get("read"):
                continue
            rows.append(self._public(entry))
            if len(rows) >= max(1, min(int(limit), 500)):
                break
        return {"messages": rows, "unread": total_unread}

    def message(self, message_id: str) -> dict:
        """{message, deliveries: [{channel, rendered, why, state, at, error, attempts, next_try, partial}], inbound:
        [...]}. partial: a failed or unknown try reached some of the channel's accounts (they are skipped next time)."""
        self.load()
        with self._lock:
            entry = self._entries.get(message_id)
            if entry is None:
                raise KeyError(f"message {message_id}")
            entry = copy.deepcopy(entry)
        deliveries = [{"channel": name, "rendered": d.get("rendered"), "why": d.get("why"), "state": d.get("state"),
                       "at": d.get("at"), "error": d.get("error"), "attempts": d.get("attempts"),
                       "next_try": d.get("next_try"), "partial": bool(d.get("partial"))}
                      for name, d in entry.get("deliveries", {}).items()]
        inbound = [line for line in self._inbound_lines(limit=2000) if line.get("message") == message_id]
        public = self._public(entry)
        public["fields"] = {k: v for k, v in (entry.get("fields") or {}).items() if k not in ("except_channel",)}
        return {"message": public, "deliveries": deliveries, "inbound": inbound}

    def message_action(self, message_id: str, action: str, *, minutes: int | None = None, by: str = "human",
                       via: str = "cockpit") -> dict:
        """ack, snooze (minutes, default 60, at most 24 hours), retry (failed deliveries, and held ones: the owner
        sends them now, past do-not-disturb time and the daily cap) or resend (unknown ones: the owner decides
        it did not arrive)."""
        self.load()
        if action not in ("ack", "snooze", "retry", "resend"):
            raise NotifyError("action must be ack, snooze, retry or resend")
        now = self._now()
        events: list[tuple[str, dict]] = []
        updates: list[tuple[Channel, str, Rendered]] = []
        with self._lock:
            entry = self._entries.get(message_id)
            if entry is None:
                raise KeyError(f"message {message_id}")
            if action == "ack":
                self._ack(entry, by, via, now, events)
            elif action == "snooze":
                minutes = 60 if minutes is None else minutes
                if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= 1440:
                    raise NotifyError("minutes must be a whole number from 1 to 1440 (24 hours)")
                self._snooze(entry, now + timedelta(minutes=minutes), now, updates)
            else:
                wanted = ("failed", "held") if action == "retry" else ("unknown",)
                for delivery in entry["deliveries"].values():
                    if delivery.get("state") in wanted:
                        delivery.update(state="queued", next_try=iso(now), attempts=0, first_failed=None,
                                        forced=delivery.get("state") == "held")
                self.audit.write("message.action", message=message_id, action=action, by=by)
            self._save_entry(entry)
            public = self._public(entry)
        self._publish(events)
        for channel, remote, rendered in updates:
            with contextlib.suppress(Exception):
                channel.update(remote, rendered)
        self._kick()
        return public

    def _ack(self, entry: dict, by: str, via: str, now: datetime, events: list) -> None:
        if not entry.get("read"):
            entry.update(read=True, read_at=iso(now), read_by=by, read_via=via)
            events.append(("notify.acked", {"message": entry["id"], "by": by, "via": via}))
        signature = entry.get("signature")
        if signature:
            incident = (self._rules.get("incidents") or {}).get(signature)
            if incident is not None and not incident.get("closed"):
                incident["silenced"] = True
                self._save_rules()

    def _snooze(self, entry: dict, until: datetime, now: datetime, updates: list) -> str:
        """Re-send entry at until (moved out of do-not-disturb time); returns the local time it got."""
        window, _ = quiet_window(self.config)
        moved = False
        if window is not None and window.contains(self.clock.local(until)):
            until = window.next_end(self.clock.local(until)).astimezone(UTC)
            moved = True
        until = min(until, now + timedelta(hours=24))
        entry["snoozed_until"] = iso(until)
        for name, delivery in entry["deliveries"].items():
            if delivery.get("state") in ("sent", "unknown", "held"):
                if delivery.get("state") == "sent" and delivery.get("remote_id") and name in self._channels:
                    text = f"Snoozed until {self.clock.hhmm(until)}"
                    updates.append((self._channels[name], delivery["remote_id"],
                                    Rendered(title=delivery.get("rendered", {}).get("title") or "Snoozed", text=text,
                                             why="", priority="min", sequence=entry["id"])))
                delivery.update(state="snoozed", next_try=iso(until), snoozed_at=iso(now), attempts=0)
                delivery.pop("partial", None)   # a snooze sends it again to everyone, on purpose
        signature = entry.get("signature")
        incident = (self._rules.get("incidents") or {}).get(signature) if signature else None
        if incident is not None:
            incident["last_alert"] = iso(until)
            self._save_rules()
        hhmm = self.clock.hhmm(until)
        return f"{hhmm} (do-not-disturb time)" if moved else hhmm

    # -- channels for the cockpit --------------------------------------------------------------------

    def channels(self) -> list[dict]:
        """GET /api/notify/channels: each channel with its computed privacy, health and paired accounts."""
        self.load()
        self._refresh_net()
        rows = []
        for name, entry in (self._settings().get("channels") or {}).items():
            if not isinstance(entry, dict):
                continue
            channel = self._channels.get(name)
            health = channel.health() if channel else {"ok": False, "detail": self._channel_errors.get(name)}
            paired = [{"id": a.get("id"), "username": a.get("username"), "first_name": a.get("first_name"),
                       "paired_at": a.get("paired_at")} for a in (channel.paired() if channel else [])]
            strangers = (self._rules.get("strangers") or {}).get(name) or {}
            today = self.clock.local().date().isoformat()
            rows.append({"name": name, "type": entry.get("type"), "privacy": self._privacy_of(entry),
                         "enabled": bool(entry.get("enabled", True)), "active": self._inactive_why(name) is None,
                         "inactive_why": self._inactive_why(name), "paired": paired, "health": health,
                         "last_answer": health.get("last_answer"),
                         "strangers_today": strangers.get("count", 0) if strangers.get("day") == today else 0,
                         "receiving": name in self._receivers})
        return rows

    def state(self) -> dict:
        """What the Messages settings card shows: stop, quiet, do-not-disturb, time sync, tamper, the sandbox
        precondition and today's count."""
        self.load()
        now = self._now()
        window, source = quiet_window(self.config)
        quiet, until = self._quiet_now(now)
        try:
            power = PowerSchedule.from_config(wakes_settings(self.config).get("power"))
        except ValueError:
            power = None
        paused = self.paused()
        _terms, terms_status = read_terms(terms_path(self.config))
        return {"paused": paused, "quiet_until": self._state.get("quiet_until"),
                "quiet_hours": {"window": window.text() if window else None, "source": source, "now": quiet,
                                "until": until},
                "power": power.text() if power else None, "timezone": self.clock.name,
                "timezone_error": self.zone_error,
                "time_sync": self.time_sync.state(), "tamper": self._state.get("tamper"),
                "precondition": self.precondition(),
                "today": {"phone": self._phone_count(now),
                          "cap": int(notice_rules(self.config)["rate_limits"].get("phone_max_per_day") or 12)},
                "rules": {key: value for key, value in notice_rules(self.config).items()
                          if key in ("source", "error", "version")},
                "liveness": self._state.get("liveness"),
                "sensitive_terms": {**terms_status, "none_declared": bool(self._state.get("terms_none"))}}

    # -- stop, quiet, review ----------------------------------------------------------------------------

    def paused(self) -> dict | None:
        """The PAUSED file's record ({at, by, via}) when Seraphina is stopped."""
        path = self.wakes_root / "PAUSED"
        if not path.exists():
            return None
        record = _read_json(path)
        return {"at": record.get("at"), "by": record.get("by") or "unknown", "via": record.get("via")}

    def pause(self, by: str = "human", via: str = "cockpit") -> dict:
        """Create <data_dir>/wakes/PAUSED (S43). Notices from your server keep coming."""
        self.wakes_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.wakes_root / "PAUSED"
        if not path.exists():
            write_json(path, {"at": self.clock.stamp(), "by": by, "via": via})
            self.audit.write("stop", by=by, via=via)
            self._publish([("wake.paused", {"scope": "all", "by": by, "until": None})])
        return self.paused() or {}

    def resume(self, by: str = "human") -> dict | None:
        """Remove PAUSED: only from the cockpit or the CLI, never from a phone (S37)."""
        if by not in ("human", "cli"):
            raise NotifyError("Seraphina is restarted only in the cockpit or the CLI")
        path = self.wakes_root / "PAUSED"
        if path.exists():
            path.unlink()
            self.audit.write("restart", by=by)
            self._publish([("wake.resumed", {"scope": "all", "by": by, "until": None})])
        return self.paused()

    def acknowledge(self, text: str | None = None, withdraw: bool = False) -> dict:
        """S46: the typed acknowledgement that lets phone channels run without bwrap (audited)."""
        self.load()
        with self._lock:
            if withdraw:
                self._state.pop("acknowledged", None)
                self.audit.write("acknowledgement", withdrawn=True)
            else:
                if " ".join(str(text or "").split()).rstrip(".") != ACK_SENTENCE:
                    raise NotifyError(f"type exactly: {ACK_SENTENCE}")
                self._state["acknowledged"] = {"at": self.clock.stamp()}
                self.audit.write("acknowledgement", withdrawn=False)
            self._save_state()
        self._kick()
        return {"acknowledged_at": (self._state.get("acknowledged") or {}).get("at")}

    @_settings_write
    def review(self, action: str) -> dict:
        """After S47: accept the current settings as the new baseline (and with "restart", restart Seraphina)."""
        if action not in ("accept", "restart"):
            raise NotifyError("action must be accept or restart")
        self.load()
        with self._lock:
            tamper = self._state.pop("tamper", None)
            self._save_state()
        self._record_baseline("review", accepted=bool(tamper))
        if action == "restart":
            self.resume(by="human")
        self._kick()
        return {"tamper": None, "paused": self.paused()}

    # -- the settings fingerprints (S47) -------------------------------------------------------------

    def fingerprints(self) -> dict:
        """sha256 of the wakes and notify sections as config.json holds them, each channel's pairing state, the
        reminders file, the clean confirmations and the sensitive-terms file."""
        try:
            data = json.loads(Path(self.config.path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None   # read while someone was writing it: compared again at the next check
        if not isinstance(data, dict):
            return {"unreadable": True}
        from .rules import rules_digest

        pairing = {name: channel.pairing_fingerprint() for name, channel in sorted(self._channels.items())}
        return {"wakes": digest(_canonical(data.get("wakes"))), "notify": digest(_canonical(data.get("notify"))),
                "pairing": pairing, "reminders": _file_digest(self.wakes_root / "reminders.json"),
                "clean": digest(_canonical(notice_rules(self.config).get("confirmed_profiles") or None)),
                "rules": rules_digest(self.config), "terms": _file_digest(terms_path(self.config))}

    def tamper(self) -> dict | None:
        """The S47 alarm while it stands ({at, what}), else None."""
        self.load()
        with self._lock:
            return copy.deepcopy(self._state.get("tamper"))

    def settings_written(self, why: str) -> None:
        """The harness itself wrote a fingerprinted file (reminders, a clean confirmation, the rulebook): the new
        baseline, so its own write never looks like a hand edit (S47). Every other fingerprint is compared first
        (the writer compared its own files before writing: check_tamper). A writer on another thread should use
        writing(), which also keeps every other check from comparing between the write and this baseline."""
        if self._loaded:
            with self._settings_io:
                alarm = self._compare(WRITTEN_BY.get(why, ()))
                self._record_baseline(why)
            self._alarm(alarm)

    @contextlib.contextmanager
    def writing(self, why: str | None) -> Iterator[None]:
        """The harness's own write of a fingerprinted file (S47), as one step: a hand edit is caught first (the
        check runs before the write folds it in); then, with the settings lock held, the write (the with block)
        and the new baseline (why: settings_written's; None when the block records its own, as a config save's
        reload does). Checks on other threads skip or wait meanwhile, so no check ever compares a file the
        harness has written against the baseline from before it. Never entered while holding a WakeManager or
        Factory lock (they are taken inside it)."""
        alarm = None
        with self._settings_io:
            try:
                if self._loaded:
                    alarm = self._compare()
                yield
            finally:
                if why is not None and self._loaded:
                    alarm = self._compare(WRITTEN_BY.get(why, ())) or alarm
                    self._record_baseline(why)
        self._alarm(alarm)

    def _init_baseline(self) -> None:
        last = self.audit.last("fingerprints")
        if last is None:
            self._record_baseline("first start")
            return
        with self._lock:
            self._baseline = last.get("fingerprints")
        self._check_tamper()

    def _record_baseline(self, why: str, quiet: bool = False, **fields: Any) -> None:
        """The current fingerprints become the baseline, and an audit line says so (quiet: only when they
        changed, as after a config save that touched neither section)."""
        prints = self.fingerprints()
        if prints.get("unreadable"):
            return
        with self._lock:
            unchanged = self._baseline == prints
            self._baseline = prints
        if not (quiet and unchanged):
            self.audit.write("fingerprints", why=why, fingerprints=prints, **fields)

    def _check_tamper(self, skip: Iterable[str] = (), wait: float = 0.0) -> None:
        """S47's comparison. While the harness writes a fingerprinted file (writing(), on another thread) this pass
        is skipped (wait 0: the tick, which compares again in 5 seconds) or waits up to wait seconds for the write
        and its baseline (an inbound command), then compares; skipped when the write takes longer."""
        acquired = self._settings_io.acquire(timeout=wait) if wait > 0 else self._settings_io.acquire(blocking=False)
        if not acquired:
            return
        try:
            alarm = self._compare(skip)
        finally:
            self._settings_io.release()
        self._alarm(alarm)

    def _compare(self, skip: Iterable[str] = ()) -> list[str] | None:
        """The fingerprints against the baseline (under the settings lock): what changed, recorded as the alarm,
        or None. The caller raises the alarm (_alarm) after releasing the lock."""
        with self._lock:
            baseline = self._baseline
            if baseline is None or self._state.get("tamper"):
                return None
        current = self.fingerprints()
        if current.get("unreadable"):
            return None
        skip = set(skip)
        changed = [key for key in ("wakes", "notify", "pairing", "reminders", "clean", "rules", "terms")
                   if key not in skip and current.get(key) != baseline.get(key)
                   and (key in baseline or current.get(key) is not None)]
        if not changed:
            return None
        with self._lock:
            if self._state.get("tamper"):   # another thread (the receiver, the deliver pass) caught it first
                return None
            self._state["tamper"] = {"at": self.clock.stamp(), "what": changed}
            self._save_state()
        self.audit.write("tamper", what=changed, fingerprints=current)
        return changed

    def _alarm(self, changed: list[str] | None) -> None:
        """S47's alarm for what _compare found: Seraphina stops and every channel hears it."""
        if not changed:
            return
        self.pause(by="tamper-check", via="settings check")
        self.notify(Notice(kind="wakes.tamper", klass="operational", route="needs_you",
                           priority=self._rule_priority("wakes.tamper"), title="Settings changed",
                           body="Settings changed outside the cockpit (" + ", ".join(changed) + "), so Seraphina "
                                "stopped. Look before restarting her: open the cockpit (Messages, Review).",
                           fields={"what": changed, "at": self.clock.stamp()},
                           dedupe_key=f"tamper:{self.clock.stamp()}"))

    # -- the sensitive-terms file -----------------------------------------------------------------------

    def terms(self) -> dict:
        terms, status = read_terms(terms_path(self.config))
        return {**status, "terms": terms, "none_declared": bool(self._state.get("terms_none"))}

    @_settings_write
    def set_terms(self, text: str | None = None, none: bool = False) -> dict:
        """Write the sensitive-terms file (0600) from the cockpit, or record "I have none"; a new baseline."""
        self.load()
        path = terms_path(self.config)
        if not none and not self.precondition()["ok"]:
            raise NotifyError("the list of sensitive words would be readable by your AI agents while they run "
                              "without the sandbox: turn on bwrap (Settings, Harness) or type the acknowledgement "
                              "in Phone setup first")
        if none:
            with self._lock:
                self._state["terms_none"] = True
                self._save_state()
        else:
            if not isinstance(text, str) or len(text) > 64 * 1024:
                raise NotifyError("text must be the list of words, one per line (at most 64 KB)")
            lines = [" ".join(line.split()) for line in text.splitlines()]
            body = "\n".join(line for line in lines if line) + "\n"
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with self._terms_io:
                if path.is_file() and not path.is_symlink():
                    # In place, never by rename: a file kept outside the masked directories is hidden from agents
                    # by a bind over it (H1), and a rename onto it would detach that bind in a running sandbox.
                    fd = os.open(path, os.O_WRONLY | os.O_TRUNC)
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        handle.write(body)
                        handle.flush()
                        os.fsync(handle.fileno())
                else:
                    write_atomic(path, body)
            os.chmod(path, 0o600)
            with self._lock:
                self._state["terms_none"] = False
                self._terms_cache = None
                self._save_state()
        self.audit.write("sensitive_terms", none=bool(none))
        self._record_baseline("sensitive terms")
        terms, status = read_terms(path)
        return {**status, "none_declared": bool(self._state.get("terms_none"))}

    # -- incidents and hook events (S20) ------------------------------------------------------------------

    def hooks(self) -> list[dict]:
        hooks = wakes_settings(self.config).get("hooks") or {}
        failures = self._rules.get("hook_failures") or {}
        today = self.clock.local().date().isoformat()
        return [{"name": name, "may": list(h.get("may") or []), "events": list(h.get("events") or []),
                 "subjects": list(h.get("subjects") or []), "personas": list(h.get("personas") or []),
                 "per_minute": h.get("per_minute") or 10, "per_day": h.get("per_day") or 200,
                 "created": h.get("created"), "rotated": h.get("rotated"), "token_set": bool(h.get("token_sha256")),
                 "last_used": (self._state.get("hook_used") or {}).get(name),
                 "wrong_tokens_today": failures.get("count", 0) if failures.get("day") == today else 0}
                for name, h in sorted(hooks.items()) if isinstance(h, dict)]

    def hook_for_token(self, token: str) -> str | None:
        """The hook whose stored digest matches token (compared with hmac.compare_digest), or None."""
        import hmac

        if not isinstance(token, str) or not token:
            return None
        presented = hook_digest(token)
        found = None
        for name, hook in (wakes_settings(self.config).get("hooks") or {}).items():
            stored = hook.get("token_sha256") if isinstance(hook, dict) else None
            if isinstance(stored, str) and len(stored) == 64 and hmac.compare_digest(stored, presented):
                found = name
        return found

    def hook_auth_failed(self) -> None:
        """A request with a wrong hook token: counted, and one inbox notice a day while it goes on (S10). The
        count is kept in memory and the notice and rules.json are updated at most once a minute, so a flood of
        wrong tokens cannot flood the disk or the event stream."""
        self.load()
        today = self.clock.local().date().isoformat()
        mono = self.monotonic()
        with self._lock:
            record = self._rules.get("hook_failures") or {}
            if record.get("day") != today:
                record = {"day": today, "count": 0}
                self._last_hook_notice = None
            record["count"] = int(record.get("count") or 0) + 1
            self._rules["hook_failures"] = record
            last = getattr(self, "_last_hook_notice", None)
            if last is not None and mono - last < 60.0:
                return
            self._last_hook_notice = mono
            self._save_rules()
            count = record["count"]
        self.notify(Notice(kind="notice", klass="operational", route="default", priority="low",
                           title="Wrong event-source tokens",
                           body=f"Something sent {count} wrong event-source token{'s' if count != 1 else ''} today. "
                                "Nothing was accepted.",
                           fields={"count": count, "inbox_only": True},
                           dedupe_key=f"hook-failures:{today}"))

    def hook_event(self, hook: str, body: dict, idempotency_key: str | None = None) -> dict:
        """POST /api/hooks/event from the hook named hook (already authenticated): {message} or {message: None}
        when deduped. HookRefused for a kind outside its scope; NotifyError for a bad body."""
        hooks = wakes_settings(self.config).get("hooks") or {}
        spec = hooks.get(hook)
        if not isinstance(spec, dict):
            raise HookRefused(401, "unknown event source")
        if "event" not in (spec.get("may") or []):
            raise HookRefused(403, "this event source may not report events")
        if idempotency_key is not None and not IDEMPOTENCY_RE.fullmatch(idempotency_key):
            raise NotifyError("Idempotency-Key must be 1-128 visible ASCII characters")
        kind = body.get("kind") if isinstance(body, dict) else None
        if kind not in HOOK_KINDS and kind not in PANEL_HOOK_KINDS:
            raise NotifyError(f"kind must be one of {', '.join(HOOK_KINDS + PANEL_HOOK_KINDS)}")
        if kind not in (spec.get("events") or []):
            raise HookRefused(403, f"this event source may not report {kind}")
        if kind in PANEL_HOOK_KINDS:
            return self._panel_event(body, hook, f"{hook}:{idempotency_key}" if idempotency_key else None)
        return self._event(body, source=f"hook:{hook}",
                           idempotency=f"{hook}:{idempotency_key}" if idempotency_key else None)

    def _panel_event(self, body: dict, hook: str, idempotency: str | None) -> dict:
        """A panel's event (PANEL_HOOK_KINDS): no notice and no incident; the panel is told the typed subject and
        detail (parity.report: the report's kind, level and code) and reads its source itself. A repeat of the same
        Idempotency-Key within 24 hours changes nothing. {message: None, panel: True}."""
        self.load()
        unknown = sorted(set(body) - {"kind", "subject", "detail"})
        if unknown:
            raise NotifyError(f"unknown field {unknown[0]!r}")
        subject, detail = body.get("subject"), body.get("detail")
        if subject is not None and (not isinstance(subject, str) or not SUBJECT_RE.fullmatch(subject)):
            raise NotifyError("subject must look like maintenance (a-z, 0-9, '.', '_', '-')")
        if detail is not None and (not isinstance(detail, str) or len(detail) > MAX_DETAIL):
            raise NotifyError(f"detail must be text of at most {MAX_DETAIL} characters")
        now = self._now()
        with self._lock:
            keys = self._state.setdefault("idempotency", {})
            cutoff = iso(now - timedelta(hours=24))
            for key in [k for k, v in keys.items() if str(v.get("at")) < cutoff]:
                del keys[key]
            if idempotency and idempotency in keys:
                return dict(keys[idempotency]["answer"])
            answer = {"message": None, "panel": True}
            if idempotency:
                keys[idempotency] = {"at": iso(now), "answer": answer}
            self._state.setdefault("hook_used", {})[hook] = iso(now)
            self._save_state()
        self.audit.write("event", source=f"hook:{hook}", event=body["kind"], subject=subject, message=None)
        _call(self.factory, "snapraid_report", subject, detail)
        return dict(answer)

    def owner_event(self, body: dict) -> dict:
        """POST /api/notify/events (the CLI's `factory notify event`): the owner reports an event."""
        if not isinstance(body, dict) or body.get("kind") not in HOOK_KINDS:
            raise NotifyError(f"kind must be one of {', '.join(HOOK_KINDS)}")
        return self._event(body, source="owner", idempotency=None)

    def hook_add(self, body: dict) -> dict:
        """POST /api/notify/hooks {name, may, events?, subjects?, personas?, per_minute?, per_day?}: a new event
        source. Its token (256 bits) is in this answer only; config keeps its sha256 (S10). Returns {hook,
        token, snippets}: the snippets read the token from a root-only header file, never from a command line."""
        if not isinstance(body, dict):
            raise NotifyError("the body must be a JSON object")
        name = body.get("name")
        if not isinstance(name, str) or not HOOK_NAME_RE.fullmatch(name):
            raise NotifyError("name must look like backup-script (a-z, 0-9 and '-', starting with a letter)")
        if name in (wakes_settings(self.config).get("hooks") or {}):
            raise NotifyError(f"an event source named {name} exists: rotate its token or delete it first")
        unknown = sorted(set(body) - {"name", "may", "events", "subjects", "personas", "per_minute", "per_day"})
        if unknown:
            raise NotifyError(f"unknown field {unknown[0]!r}")
        spec = {key: body[key] for key in ("may", "events", "subjects", "personas", "per_minute", "per_day")
                if body.get(key) is not None}
        spec.setdefault("may", ["event"])
        spec.setdefault("events", ["backup.failed", "backup.ok"])
        token = secrets.token_urlsafe(HOOK_TOKEN_BYTES)
        spec.update(token_sha256=hook_digest(token), created=self.clock.stamp())
        self._patch({"wakes": {"hooks": {name: spec}}})
        self.audit.write("hook.added", hook=name, may=spec.get("may"), events=spec.get("events"))
        return {"hook": self._hook_view(name), "token": token, "snippets": hook_snippets(name, spec, self._hook_url())}

    def hook_action(self, name: str, action: str) -> dict:
        """rotate (a new token, shown once; the old one stops working) or delete an event source."""
        hooks = wakes_settings(self.config).get("hooks") or {}
        if not isinstance(hooks.get(name), dict):
            raise KeyError(f"event source {name}")
        if action == "delete":
            self._patch({"wakes": {"hooks": {name: None}}})
            self.audit.write("hook.deleted", hook=name)
            return {"hook": None, "deleted": name}
        if action != "rotate":
            raise NotifyError("action must be rotate or delete")
        token = secrets.token_urlsafe(HOOK_TOKEN_BYTES)
        self._patch({"wakes": {"hooks": {name: {"token_sha256": hook_digest(token), "rotated": self.clock.stamp()}}}})
        self.audit.write("hook.rotated", hook=name)
        return {"hook": self._hook_view(name), "token": token,
                "snippets": hook_snippets(name, wakes_settings(self.config)["hooks"][name], self._hook_url())}

    def _hook_view(self, name: str) -> dict | None:
        return next((hook for hook in self.hooks() if hook["name"] == name), None)

    def _hook_url(self) -> str:
        server = (getattr(self.config, "raw", {}) or {}).get("server") or {}
        host = str(server.get("host") or "127.0.0.1")
        if host in ("", "0.0.0.0", "::", "*", "localhost") or not re.fullmatch(r"[A-Za-z0-9.:-]{1,253}", host):
            host = "127.0.0.1"   # the snippets go into root's files: nothing but a host name or address
        host = f"[{host}]" if ":" in host else host
        return f"http://{host}:{int(server.get('port') or 8765)}/api/hooks/event"

    def _patch(self, change: dict) -> None:
        update = getattr(self.factory, "update_config", None)
        if not callable(update):
            raise NotifyError("this factory cannot change settings")
        update(change)

    def _event(self, body: dict, *, source: str, idempotency: str | None) -> dict:
        self.load()
        unknown = sorted(set(body) - {"kind", "subject", "detail", "dedupe_key"})
        if unknown:
            raise NotifyError(f"unknown field {unknown[0]!r}")
        kind = str(body["kind"])
        subject = body.get("subject")
        if subject is not None and (not isinstance(subject, str) or not SUBJECT_RE.fullmatch(subject)):
            raise NotifyError("subject must look like nightly (a-z, 0-9, '.', '_', '-'; at most 41 characters)")
        detail = body.get("detail")
        if detail is not None and (not isinstance(detail, str) or len(detail) > MAX_DETAIL):
            raise NotifyError(f"detail must be text of at most {MAX_DETAIL} characters")
        dedupe = body.get("dedupe_key")
        if dedupe is not None and (not isinstance(dedupe, str) or not DEDUPE_RE.fullmatch(dedupe)):
            raise NotifyError("dedupe_key must be 1-64 characters of A-Z, a-z, 0-9, '.', '_', ':' and '-'")
        now = self._now()
        with self._lock:
            keys = self._state.setdefault("idempotency", {})
            cutoff = iso(now - timedelta(hours=24))
            for key in [k for k, v in keys.items() if str(v.get("at")) < cutoff]:
                del keys[key]
            if idempotency and idempotency in keys:
                return dict(keys[idempotency]["answer"])
        result = self._incident(kind, subject, detail, source, now, dedupe)
        with self._lock:
            if idempotency:
                self._state.setdefault("idempotency", {})[idempotency] = {"at": iso(now), "answer": result}
            if source.startswith("hook:"):
                self._state.setdefault("hook_used", {})[source[5:]] = iso(now)
            self._save_state()
        self.audit.write("event", source=source, event=kind, subject=subject, message=result.get("message"))
        return result

    def _incident(self, kind: str, subject: str | None, detail: str | None, source: str, now: datetime,
                  dedupe: str | None) -> dict:
        allowed_subject = False
        if source == "owner":
            allowed_subject = subject is not None
        elif source.startswith("hook:"):
            hook = (wakes_settings(self.config).get("hooks") or {}).get(source[5:]) or {}
            allowed_subject = subject is not None and subject in (hook.get("subjects") or [])
        base = RECOVERS.get(kind, kind)
        signature = f"{base}:{subject or '-'}"
        fields = {"subject": subject, "subject_allowed": allowed_subject, "at": iso(now), "source": source}
        if detail:
            fields["detail"] = detail     # untrusted: kept for the cockpit, never in a phone template
        with self._lock:
            incidents = self._rules.setdefault("incidents", {})
            open_one = incidents.get(signature)
            if kind in RECOVERS:
                if not open_one or open_one.get("closed"):
                    return {"message": None, "recorded": True}
                open_one.update(closed=iso(now))
                self._save_rules()
            elif open_one and not open_one.get("closed"):
                open_one["repeats"] = int(open_one.get("repeats") or 0) + 1
                self._save_rules()
                return {"message": None, "deduped": True}
        title, body = operational_text(Notice(kind=kind, klass="operational", route="needs_you", priority="default",
                                              title="", body="", fields=fields), names=True,
                                       safe_repos=set())
        if detail:
            body = f"{body} Detail from the sender (shown only here): {detail}"
        notice = Notice(kind=kind, klass="operational", route=RULE_ROUTES[kind], priority=self._rule_priority(kind),
                        title=title, body=body, fields=fields, signature=signature,
                        dedupe_key=dedupe and f"{source}:{dedupe}")
        result = self.notify(notice)
        if result.get("deduped"):
            return {"message": None, "deduped": True}
        if kind not in RECOVERS:
            with self._lock:
                self._rules.setdefault("incidents", {})[signature] = {
                    "kind": kind, "subject": subject, "opened": iso(now), "last_alert": iso(now), "reminders": 0,
                    "message": result["id"], "silenced": False, "closed": None}
                self._save_rules()
        # an event's wake sees the kind and an allowlisted subject only; the sender's detail never (§2.3)
        _call(self.factory, "wake_event", kind, {"kind": kind, "subject": subject if allowed_subject else None},
              message=result["id"])
        return {"message": result["id"]}

    def parity_event(self, kind: str, fields: Mapping[str, Any], key: str = "-") -> dict:
        """A disk protection notice (DESIGN §41, BACKUPS_SNAPRAID.md §7.3): one incident per kind and key (a disk
        number, never its name), alerting once and reminding as hook incidents do; parity.ok closes an open
        parity.failed. fields: the counts the fixed text takes (removed, updated, disks, count, time), nothing else.
        {message} or {message: None} when deduped."""
        if kind not in PARITY_KINDS:
            raise NotifyError(f"kind must be one of {', '.join(PARITY_KINDS)}")
        if not isinstance(key, str) or not SUBJECT_RE.fullmatch(key) and key != "-":
            raise NotifyError("key must be a short name such as disk-2")
        self.load()
        now = self._now()
        typed = {name: fields[name] for name in ("removed", "updated", "disks", "count", "time") if name in fields}
        typed.update(at=iso(now), source="snapraid")
        base = RECOVERS.get(kind, kind)
        signature = f"{base}:{key}"
        with self._lock:
            incidents = self._rules.setdefault("incidents", {})
            open_one = incidents.get(signature)
            if kind in RECOVERS:
                if not open_one or open_one.get("closed"):
                    return {"message": None, "recorded": True}
                open_one.update(closed=iso(now))
                self._save_rules()
            elif open_one and not open_one.get("closed"):
                open_one["repeats"] = int(open_one.get("repeats") or 0) + 1
                self._save_rules()
                return {"message": None, "deduped": True}
        title, body = parity_text(kind, typed)
        result = self.notify(Notice(kind=kind, klass="operational", route=RULE_ROUTES[kind],
                                    priority=self._rule_priority(kind), title=title, body=body, fields=typed,
                                    signature=signature))
        if kind not in RECOVERS:
            with self._lock:
                self._rules.setdefault("incidents", {})[signature] = {
                    "kind": kind, "subject": None, "opened": iso(now), "last_alert": iso(now), "reminders": 0,
                    "message": result["id"], "silenced": False, "closed": None}
                self._save_rules()
        _call(self.factory, "wake_event", kind, {"kind": kind, "subject": None}, message=result["id"])
        return {"message": result["id"]}

    def parity_clear(self, kind: str, key: str = "-") -> bool:
        """Close an open disk protection incident without a notice (a disk that reads healthy again): True when
        one was open."""
        signature = f"{kind}:{key}"
        with self._lock:
            open_one = (self._rules.get("incidents") or {}).get(signature)
            if not open_one or open_one.get("closed"):
                return False
            open_one.update(closed=iso(self._now()))
            self._save_rules()
        return True

    def _incident_reminders(self, now: datetime) -> None:
        settings = (self._settings().get("incident") or {})
        hours = float(settings.get("repeat_hours") or 6)
        most = int(settings.get("max_reminders") or 3)
        due: list[tuple[str, dict]] = []
        with self._lock:
            for signature, incident in (self._rules.get("incidents") or {}).items():
                if incident.get("closed") or incident.get("silenced") or int(incident.get("reminders") or 0) >= most:
                    continue
                last = parse_iso(incident.get("last_alert"))
                if last is not None and now - last >= timedelta(hours=hours):
                    incident["reminders"] = int(incident.get("reminders") or 0) + 1
                    incident["last_alert"] = iso(now)
                    due.append((signature, dict(incident)))
            if due:
                self._save_rules()
        for signature, incident in due:
            kind = incident["kind"]
            original = self._entries.get(incident.get("message") or "")
            fields = dict((original or {}).get("fields") or {})
            fields.update(reminder=True, at=iso(now))
            title, body = operational_text(Notice(kind=kind, klass="operational", route="needs_you",
                                                  priority="default", title="", body="", fields=fields),
                                           names=True, safe_repos=set())
            result = self.notify(Notice(kind=kind, klass="operational", route=RULE_ROUTES.get(kind, "needs_you"),
                                        priority=self._rule_priority(kind), title=title, body=body, fields=fields,
                                        signature=signature, dedupe_key=f"{signature}:reminder:{incident['reminders']}"))
            with self._lock:
                record = (self._rules.get("incidents") or {}).get(signature)
                if record is not None:
                    record["message"] = result["id"]
                    self._save_rules()

    # -- the rules engine ---------------------------------------------------------------------------------

    def _rules_loop(self) -> None:
        first = True
        while not self._stop.is_set():
            events = []
            try:
                if self._subscription is not None:
                    try:
                        events.append(self._subscription.get(timeout=1.0))
                    except queue.Empty:
                        pass
                    while len(events) < 500:
                        try:
                            events.append(self._subscription.get_nowait())
                        except queue.Empty:
                            break
                else:
                    self._stop.wait(1.0)
                wanted = [e for e in events if getattr(e, "type", None) in RULE_EVENTS]
                if first or self.monotonic() - self._last_reconcile >= RECONCILE_SECONDS:
                    first = False
                    self.reconcile()
                elif wanted:
                    self.rules_tick(wanted)
            except Exception as exc:  # noqa: BLE001
                self._report_error("rules", exc)
                self._stop.wait(1.0)

    def rules_tick(self, events: list[Any], now: datetime | None = None) -> None:
        """One rules pass over bus events (only job.updated, plan.updated and items.updated count)."""
        self.load()
        items = False
        for event in events:
            kind = getattr(event, "type", None)
            data = getattr(event, "data", None) or {}
            if kind == "job.updated" and isinstance(data.get("job"), dict):
                self._job_rule(data["job"], now)
            elif kind == "plan.updated" and isinstance(data.get("plan"), dict):
                self._plan_rule(data["plan"], now)
            elif kind == "items.updated":
                items = True
        if items:
            self._items_rules(now)

    def reconcile(self, now: datetime | None = None) -> None:
        """Re-derive the rules' state from the job store, the plans and the tracker (the bus drops events
        under load). The very first pass only records what it finds, so enabling messages sends no burst."""
        self.load()
        self._last_reconcile = self.monotonic()
        with self._lock:
            baseline = not self._rules.get("baseline_done")
        jobs = _call(self.factory, "jobs", default=[]) or []
        for job in jobs if isinstance(jobs, list) else []:
            if isinstance(job, dict):
                self._job_rule(job, now, record_only=baseline)
        plans = _call(self.factory, "plans", default=[]) or []
        for plan in plans if isinstance(plans, list) else []:
            if isinstance(plan, dict):
                self._plan_rule(plan, now, record_only=baseline)
        if not baseline:
            self._items_rules(now)
        with self._lock:
            if baseline:
                self._rules["baseline_done"] = True
                self._save_rules()

    def _job_rule(self, job: dict, now: datetime | None, record_only: bool = False) -> None:
        repo, job_id, stage = job.get("repo"), job.get("id"), job.get("stage")
        if not isinstance(repo, str) or not isinstance(job_id, str) or not isinstance(stage, str):
            return
        key = f"{repo}/{job_id}"
        current = {"stage": stage, "step": job.get("failed_step") if stage == "failed" else None}
        with self._lock:
            jobs = self._rules.setdefault("jobs", {})
            previous = jobs.get(key)
            if previous == current:
                return
            jobs[key] = current
            self._save_rules()
        if record_only or stage not in HUMAN_STAGES:
            return
        now = now or self._now()
        number = job_number(job_id)
        since = job.get("updated_at") if isinstance(job.get("updated_at"), str) else iso(now)
        fields = {"repo": repo, "job": job_id, "stage": stage, "step": current["step"], "since": since,
                  "why": "waiting" if stage == "needs-human" else "failed", "at": iso(now)}
        rule = "job.needs_human" if stage == "needs-human" else "job.failed"
        title, body = operational_text(Notice(kind=rule, klass="operational", route="needs_you", priority="default",
                                              title="", body="", fields=fields), names=True, safe_repos={repo})
        result = self.notify(Notice(kind=rule, klass="operational", route=RULE_ROUTES[rule],
                                    priority=self._rule_priority(rule), title=title, body=body, fields=fields,
                                    about=f"job:{repo}/{job_id}", dedupe_key=f"{rule}:{key}:{since}"))
        # one engine: after its notice, the rule asks for the persona wakes its on_events lists (§2.3), with the
        # event's typed fields as data, so a notice and its wake never disagree
        _call(self.factory, "wake_event", rule, {"repo": repo, "job": job_id, "stage": stage, "step": current["step"]},
              message=result.get("id"))

    def _plan_rule(self, plan: dict, now: datetime | None, record_only: bool = False) -> None:
        repo, plan_id, stage = plan.get("repo"), plan.get("id"), plan.get("stage")
        if not isinstance(repo, str) or not isinstance(plan_id, str) or not isinstance(stage, str):
            return
        key = f"{repo}/{plan_id}"
        with self._lock:
            plans = self._rules.setdefault("plans", {})
            previous = plans.get(key)
            if previous == stage:
                return
            plans[key] = stage
            self._save_rules()
        if record_only or stage not in PLAN_WAITING:
            return
        now = now or self._now()
        fields = {"repo": repo, "plan": plan_id, "stage": stage, "at": iso(now)}
        title, body = operational_text(Notice(kind="plan.attention", klass="operational", route="needs_you",
                                              priority="default", title="", body="", fields=fields),
                                       names=True, safe_repos={repo})
        result = self.notify(Notice(kind="plan.attention", klass="operational", route=RULE_ROUTES["plan.attention"],
                                    priority=self._rule_priority("plan.attention"), title=title, body=body,
                                    fields=fields, about=f"plan:{repo}/{plan_id}",
                                    dedupe_key=f"plan.attention:{key}:{stage}:{iso(now)}"))
        _call(self.factory, "wake_event", "plan.attention", {"stage": stage}, message=result.get("id"))

    def _items_rules(self, now: datetime | None) -> None:
        """items.overdue and items.waiting: one inbox notice a day each, counted in the next summary."""
        now = now or self._now()
        today = self.clock.local(now).date().isoformat()
        found = _call(self.factory, "items", default=None, overdue=True, limit=1000)
        rows = found.get("items") if isinstance(found, dict) else None
        if isinstance(rows, list):
            refs = sorted(str(row.get("ref")) for row in rows if isinstance(row, dict) and row.get("ref"))
            with self._lock:
                seen = self._rules.setdefault("items", {})
                fresh = [ref for ref in refs if seen.get(ref) != today]
                for ref in fresh:
                    seen[ref] = today
                if fresh:
                    self._save_rules()
            if fresh:
                listed = ", ".join(refs[:10]) + (f" and {len(refs) - 10} more" if len(refs) > 10 else "")
                self.notify(Notice(kind="items.overdue", klass="operational", route=RULE_ROUTES["items.overdue"],
                                   priority="low", title="Overdue promises",
                                   body=f"{len(refs)} tracked promise{'s are' if len(refs) != 1 else ' is'} overdue: "
                                        f"{listed}.",
                                   fields={"count": len(refs), "inbox_only": True, "at": iso(now)},
                                   dedupe_key=f"items.overdue:{today}"))
        waiting = found.get("counts", {}).get("waiting") if isinstance(found, dict) else None
        if not isinstance(waiting, int):
            counted = _call(self.factory, "items", default=None, waiting=True, limit=1)
            waiting = (counted.get("counts") or {}).get("waiting") if isinstance(counted, dict) else None
        if isinstance(waiting, int) and waiting > 5:
            with self._lock:
                if self._rules.get("waiting_day") == today:
                    return
                self._rules["waiting_day"] = today
                self._save_rules()
            self.notify(Notice(kind="items.waiting", klass="operational", route=RULE_ROUTES["items.waiting"],
                               priority="low", title="Waiting for you",
                               body=f"{waiting} things are waiting for you in the tracker.",
                               fields={"count": waiting, "inbox_only": True, "at": iso(now)},
                               dedupe_key=f"items.waiting:{today}"))

    def _unclean_check(self) -> None:
        """S18: at start, no clean-stop record means the server stopped hard; tell the owner only when the last
        liveness time is not inside the scheduled power-off window (± 15 minutes). A stop that was asked for
        (the stopping record) but ran out of time is logged, not announced."""
        with self._lock:
            state = dict(self._state)
        if not state or state.get("clean_stop") is not False or not state.get("liveness"):
            return
        last = parse_iso(state.get("liveness"))
        if last is None:
            return
        if state.get("stopping"):
            self.audit.write("hard_stop", liveness=state.get("liveness"), during_stop=True)
            return
        try:
            power = PowerSchedule.from_config(wakes_settings(self.config).get("power"))
        except ValueError:
            power = None
        local = self.clock.local(last)
        if power is not None and power.in_power_off(local):
            self.audit.write("hard_stop", liveness=state.get("liveness"), inside_power_off=True)
            return
        self.audit.write("hard_stop", liveness=state.get("liveness"), inside_power_off=False)
        fields = {"time": local.strftime("%H:%M"), "off": power.off.strftime("%H:%M") if power else None,
                  "at": self.clock.stamp()}
        title, body = operational_text(Notice(kind="server.unclean", klass="operational", route="needs_you",
                                              priority="default", title="", body="", fields=fields),
                                       names=True, safe_repos=set())
        self.notify(Notice(kind="server.unclean", klass="operational", route=RULE_ROUTES["server.unclean"],
                           priority=self._rule_priority("server.unclean"), title=title, body=body, fields=fields,
                           dedupe_key=f"server.unclean:{state.get('liveness')}"))

    # -- summaries (S15) ---------------------------------------------------------------------------------

    def _summaries(self, now: datetime) -> None:
        window, _source = quiet_window(self.config)
        digests = self._settings().get("digests") or {}
        local = self.clock.local(now)
        if window is not None:
            start = window.last_start(local)
            end = window.last_end(local)
            if digests.get("evening", True) and window.contains(local) and local - start < EVENING_WINDOW:
                self._summary_once("evening", start.date().isoformat(), now, since=now - timedelta(hours=24))
            if digests.get("morning", True) and not window.contains(local) and local - end < MORNING_WINDOW:
                quiet_start = window.last_start(end - timedelta(minutes=1))
                self._summary_once("morning", end.date().isoformat(), now, since=quiet_start.astimezone(UTC),
                                   until=end.astimezone(UTC))
        midday = digests.get("midday")
        if isinstance(midday, str):
            try:
                at = Window.parse(f"{midday}-{_plus_minutes(midday, 60)}")
            except ValueError:
                at = None
            if at is not None and at.contains(local):
                self._summary_once("midday", local.date().isoformat(), now, since=now - timedelta(hours=6),
                                   only="cap")
        quiet_until = parse_iso(self._state.get("quiet_until"))
        if quiet_until is not None and quiet_until <= now:
            # the owner's Quiet ended: what it held comes as one summary, never as a burst
            quiet_from = parse_iso(self._state.get("quiet_from")) or quiet_until - timedelta(hours=3)
            with self._lock:
                self._state.pop("quiet_until", None)
                self._state.pop("quiet_from", None)
                self._save_state()
            if window is None or not window.contains(local):
                self._summary_once("quiet", iso(quiet_until), now, since=quiet_from, until=quiet_until, only="held")

    def _summary_once(self, which: str, day: str, now: datetime, *, since: datetime, until: datetime | None = None,
                      only: str | None = None) -> None:
        with self._lock:
            done = self._rules.setdefault("summaries", {})
            if done.get(which) == day:
                return
            done[which] = day
            self._save_rules()
            counts, held_ids, total = self._summary_counts(which, since, until or now, only)
        if total == 0:
            return
        parts = [f"{label}: {count}" for label, count in counts if count]
        sentence = self._overnight_sentence() if which == "evening" else ""
        budget = MAX_BODY - (len(sentence) + 1 if sentence else 0)
        body = ""
        for index, part in enumerate(parts):
            candidate = part if not body else f"{body} · {part}"
            rest = len(parts) - index - 1
            tail = f" · +{rest} more in the cockpit" if rest else ""
            if len(candidate + tail) > budget:
                body = f"{body} · +{len(parts) - index} more in the cockpit" if body else f"+{len(parts)} in the cockpit"
                break
            body = candidate
        if sentence:
            body = f"{body}\n{sentence}" if body else sentence
        label = SUMMARY_LABELS.get(which, which.capitalize())
        title = f"{label} summary: {total} thing{'s' if total != 1 else ''}"
        result = self.notify(Notice(kind="summary", klass="operational", route="digest",
                                    priority="low" if which == "evening" else "default", title=title, body=body,
                                    fields={"which": label, "n": total, "title": title, "text": body,
                                            "at": iso(now)},
                                    dedupe_key=f"summary:{which}:{day}"))
        with self._lock:
            for message_id in held_ids:
                entry = self._entries.get(message_id)
                if entry is not None:
                    entry["summary"] = result["id"]   # the inbox says which summary carried it
                    self._save_entry(entry, index=False)

    def _summary_counts(self, which: str, since: datetime, until: datetime,
                        only: str | None) -> tuple[list[tuple[str, int]], list[str], int]:
        categories = {"Urgent": 0, "Needs you": 0, "Failed": 0, "Reminders": 0, "Messages from Seraphina": 0,
                      "Overdue promises": 0, "Notices from your server": 0}
        held: list[str] = []
        counted: set[str] = set()
        for entry in self._entries.values():
            if entry.get("kind") in ("summary", "confirm", "test"):
                continue
            at = parse_iso(entry.get("at"))
            if at is None or at < since or at > until:
                continue
            states = [d.get("state") for d in (entry.get("deliveries") or {}).values()]
            was_held = "held" in states and not (only and entry.get("summary"))
            if only == "cap":
                was_held = was_held and any("phone messages are used" in str(d.get("why") or "")
                                            for d in entry["deliveries"].values())
            if only and not was_held:
                continue
            if entry.get("read") and not was_held:
                continue
            if was_held:
                held.append(entry["id"])
            counted.add(entry["id"])
            kind = entry.get("kind")
            if entry.get("priority") in ("high", "max") and entry.get("sender") == "server":
                categories["Urgent"] += 1
            elif kind in ("job.needs_human", "plan.attention"):
                categories["Needs you"] += 1
            elif kind == "job.failed":
                categories["Failed"] += 1
            elif kind == "reminder":
                categories["Reminders"] += 1
            elif kind in ("persona", "reply"):
                categories["Messages from Seraphina"] += 1
            elif kind == "items.overdue":
                categories["Overdue promises"] += int((entry.get("fields") or {}).get("count") or 1)
            else:
                categories["Notices from your server"] += 1
        rows = list(categories.items())
        if which == "morning":
            night = 0
            for line in self._inbound_lines(limit=500):
                at = parse_iso(line.get("at"))
                if line.get("kind") == "text" and at is not None and since <= at <= until:
                    night += 1
            rows.append(("Messages you sent overnight", night))
            counted |= {f"in-{i}" for i in range(night)}
        if held:
            rows.append(("Held overnight" if which == "morning" else "Held", len(held)))
        return rows, held, len(counted)

    def _overnight_sentence(self) -> str:
        try:
            power = PowerSchedule.from_config(wakes_settings(self.config).get("power"))
        except ValueError:
            power = None
        if power is None:
            return ""
        types = {entry.get("type") for name, entry in (self._settings().get("channels") or {}).items()
                 if isinstance(entry, dict) and entry.get("enabled", True)}
        parts = [f"Your server sleeps {power.off:%H:%M}-{power.wake:%H:%M}."]
        if "ntfy" in types:
            parts.append("Buttons on your private channel don't work then, and taps are lost.")
        if "telegram" in types:
            parts.append(f"Telegram messages are read at {power.wake:%H:%M}: stop still applies, snooze and mute don't.")
        return " ".join(parts)

    def _phone_silence(self, now: datetime) -> None:
        days = int((self._settings().get("inbound") or {}).get("phone_silent_days") or 7)
        week = self.clock.local(now).strftime("%G-W%V")
        for name, channel in list(self._channels.items()):
            if self._inactive_why(name) is not None or not channel.can_receive:
                continue
            health = channel.health()
            last_sent = parse_iso(health.get("last_sent"))
            if last_sent is None or now - last_sent > timedelta(days=days):
                continue
            last_answer = parse_iso(health.get("last_answer"))
            first_sent = parse_iso(channel.state().get("first_sent"))
            if first_sent is None:
                channel.update_state(lambda s: s.setdefault("first_sent", iso(now)))
                continue
            reference = last_answer or first_sent
            if now - reference < timedelta(days=days):
                continue
            with self._lock:
                marks = self._rules.setdefault("silent", {})
                if marks.get(name) == week:
                    continue
                marks[name] = week
                self._save_rules()
            self.notify(Notice(kind="notice", klass="operational", route="default", priority="low",
                               title="Your phone has not answered",
                               body=f"Notices go out on {name}, but your phone has not answered for {days} days. "
                                    "Send a test from Phone setup and tap Got it.",
                               fields={"channel": name, "inbox_only": True}, dedupe_key=f"silent:{name}:{week}"))

    # -- retention (S49) -------------------------------------------------------------------------------

    def _retention(self, now: datetime) -> None:
        day = now.date().isoformat()
        if self._last_retention == day:
            return
        self._last_retention = day
        days = int((wakes_settings(self.config).get("retention") or {}).get("text_days") or 90)
        cutoff = now - timedelta(days=days)
        with self._lock:
            for entry in self._entries.values():
                at = parse_iso(entry.get("at"))
                if at is None or at >= cutoff or entry.get("text_removed"):
                    continue
                _forget_text(entry)
                self._save_entry(entry, index=False)
        for path in sorted(self.root.glob("inbound-*.jsonl")):
            month = path.stem.split("-", 1)[1]
            try:
                start = datetime.strptime(month + "-01", "%Y-%m-%d").replace(tzinfo=UTC)
            except ValueError:
                continue
            if start < cutoff:   # the month may hold lines older than the cutoff: rewritten line by line
                _rewrite_lines(path, cutoff)

    # -- inbound (S32-S37, S48) -------------------------------------------------------------------------

    def _inbound_lines(self, limit: int = 200) -> list[dict]:
        lines: list[dict] = []
        for path in sorted(self.root.glob("inbound-*.jsonl"), reverse=True):
            try:
                raw = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for text in reversed(raw):
                with contextlib.suppress(ValueError):
                    lines.append(json.loads(text))
                if len(lines) >= limit:
                    return lines
        return lines

    def inbound(self, limit: int = 100, kind: str | None = None) -> list[dict]:
        """The newest inbound records (replies, commands, strangers, forged): the owner's own text included,
        for the cockpit."""
        rows = self._inbound_lines(limit=max(1, min(limit, 1000)) * (4 if kind else 1))
        return [row for row in rows if kind is None or row.get("kind") == kind][:limit]

    def _record_inbound(self, inbound: Inbound, outcome: str, **extra: Any) -> dict:
        now = self._now()
        with self._lock:
            number = int(self._state.get("inbound_seq") or 0) + 1
            self._state["inbound_seq"] = number
            self._save_state()
        record = {"id": f"I-{number:06d}", "at": iso(now), "channel": inbound.channel, "kind": inbound.kind,
                  "topic": inbound.topic, "sender": inbound.sender or "stranger", "verb": inbound.verb,
                  "outcome": outcome, "sent_at": inbound.sent_at, "forwarded": inbound.forwarded,
                  "remote_id": inbound.remote_id, **extra}
        if inbound.text and inbound.kind in ("text", "command", "forged", "foreign"):
            record["text"] = self._redact(inbound.text)   # a token typed or forged in is never kept
        _append_line(self.root / f"inbound-{now.strftime('%Y-%m')}.jsonl", record)
        self.audit.write("inbound", channel=inbound.channel, inbound_kind=inbound.kind, verb=inbound.verb, outcome=outcome,
                         sender=inbound.sender or "stranger", inbound=record["id"])
        self._publish([("inbound.received", {"channel": inbound.channel, "kind": inbound.kind, "verb": inbound.verb,
                                             "persona": extra.get("persona"), "outcome": outcome})])
        return record

    def _redact(self, text: str) -> str:
        """text with every secret the harness knows (the channel tokens, the master token, provider keys, the
        env file's values) replaced by ***."""
        for secret in sorted({v for v in self._secrets() if isinstance(v, str) and len(v) >= 8}, key=len, reverse=True):
            text = text.replace(secret, "***")
        return text

    def _late(self, inbound: Inbound, entry: dict | None, now: datetime) -> bool:
        stale = timedelta(minutes=int((self._settings().get("inbound") or {}).get("stale_minutes") or 30))
        sent = parse_iso(inbound.sent_at)
        if sent is not None:
            return now - sent > stale
        if inbound.first_batch:
            return True
        if entry is not None:   # a Telegram tap has no date: the notice it answers was sent too long ago
            delivery = (entry.get("deliveries") or {}).get(inbound.channel) or {}
            at = parse_iso(delivery.get("at")) or parse_iso(entry.get("at"))
            return at is not None and now - at > stale
        return False

    def _rate(self, name: str, now_mono: float) -> tuple[bool, int]:
        """Count one typed command or failed capability on channel name; (now held, count in the minute)."""
        settings = self._settings().get("inbound") or {}
        most = int(settings.get("max_commands_per_minute") or 10)
        window = self._windows.setdefault(name, deque())
        window.append(now_mono)
        while window and now_mono - window[0] > 60:
            window.popleft()
        return len(window) > most, len(window)

    def _held(self, name: str) -> bool:
        holds = self._state.get("holds") or {}
        until = parse_iso(holds.get(name))
        return until is not None and until > self._now()

    def _hold(self, name: str, count: int) -> None:
        minutes = int((self._settings().get("inbound") or {}).get("hold_minutes") or 15)
        until = self._now() + timedelta(minutes=minutes)
        with self._lock:
            self._state.setdefault("holds", {})[name] = iso(until)
            self._save_state()
        self.audit.write("inbound.hold", channel=name, count=count, until=iso(until))
        words = "Telegram" if self._entry_of(name).get("type") == "telegram" else "Your phone"
        self.notify(Notice(kind="inbound.hold", klass="short", route="needs_you", priority="default",
                           title="Stopped listening",
                           body=f"{words} sent {count} commands in a minute, so your server stopped listening to it "
                                f"for {minutes} minutes. If that wasn't you: change your Telegram password, check "
                                "Two-Step Verification, then Unpair in the cockpit (Messages, Phone setup).",
                           fields={"channel_words": words, "count": count, "minutes": minutes,
                                   "except_channel": name, "at": self.clock.stamp()},
                           dedupe_key=f"hold:{name}:{iso(until)}"))

    def _alert_forged(self, name: str, what: str) -> None:
        minutes = int((self._settings().get("inbound") or {}).get("forged_alert_minutes") or 60)
        now = self._now()
        with self._lock:
            marks = self._rules.setdefault("forged_alert", {})
            last = parse_iso(marks.get(f"{name}:{what}"))
            if last is not None and now - last < timedelta(minutes=minutes):
                return
            marks[f"{name}:{what}"] = iso(now)
            self._save_rules()
        fields = {"what": what, "except_channel": name, "at": iso(now)}
        title, body = operational_text(Notice(kind="inbound.forged", klass="operational", route="needs_you",
                                              priority="default", title="", body="", fields=fields),
                                       names=True, safe_repos=set())
        self.notify(Notice(kind="inbound.forged", klass="operational", route=RULE_ROUTES["inbound.forged"],
                           priority=self._rule_priority("inbound.forged"), title=title, body=body, fields=fields,
                           dedupe_key=f"forged:{name}:{what}:{iso(now)}"))

    def handle_inbound(self, inbound: Inbound) -> dict:
        """Decide what one inbound message does (S32-S37, S48): {outcome, answer, inbound}. The answer is fixed
        text the channel sends back (Telegram: the callback answer or a chat message; ntfy: a low notice)."""
        self.load()
        self._refresh_net()
        now = self._now()
        name = inbound.channel
        third = self._privacy_of(self._entry_of(name)) != PRIVACY_TAILNET
        kind = inbound.kind
        if kind == "stranger":
            return self._stranger(inbound, now)
        if kind == "error":
            return {"outcome": "error", "answer": None, "inbound": self._record_inbound(inbound, "error")["id"]}
        if kind == "pairing":
            return {"outcome": "confirm code sent", "answer": None,
                    "inbound": self._record_inbound(inbound, "confirm code sent")["id"]}
        if kind == "foreign":
            record = self._record_inbound(inbound, "alerted")
            self._alert_forged(name, "foreign")
            return {"outcome": "alerted", "answer": None, "inbound": record["id"]}
        if kind == "forged":
            held, count = self._rate(name, self.monotonic())
            record = self._record_inbound(inbound, "dropped")
            self._alert_forged(name, "forged")
            if held and not self._held(name):
                self._hold(name, count)
            return {"outcome": "dropped", "answer": None, "inbound": record["id"]}
        if kind == "ignored":
            answer = self._once_a_day(name, "only_text", ONLY_TEXT if not third else
                                      "Only typed text is read here.")
            return {"outcome": "ignored", "answer": answer,
                    "inbound": self._record_inbound(inbound, "ignored")["id"]}
        self._check_tamper(wait=10.0)   # S47: a hand-added account or setting is caught before anything it says is obeyed
        channel = self._channels.get(name)
        if channel is not None:
            channel.note("last_answer")
        if kind == "button":
            return self._button(inbound, now, third)
        if kind == "text":
            return self._text(inbound, now, third)
        return {"outcome": "ignored", "answer": None, "inbound": self._record_inbound(inbound, "ignored")["id"]}

    def _once_a_day(self, name: str, what: str, text: str) -> str | None:
        today = self.clock.local().date().isoformat()
        with self._lock:
            marks = self._rules.setdefault("once", {})
            key = f"{name}:{what}"
            if marks.get(key) == today:
                return None
            marks[key] = today
            self._save_rules()
        return text

    def _stranger(self, inbound: Inbound, now: datetime) -> dict:
        today = self.clock.local(now).date().isoformat()
        name = inbound.channel
        with self._lock:
            strangers = self._rules.setdefault("strangers", {})
            record = strangers.get(name) or {}
            if record.get("day") != today:
                record = {"day": today, "count": 0, "logged": None}
            record["count"] = int(record.get("count") or 0) + 1
            last = parse_iso(record.get("logged"))
            log_now = last is None or now - last >= timedelta(minutes=1)
            if log_now:
                record["logged"] = iso(now)
            strangers[name] = record
            self._save_rules()
            count = record["count"]
        result = {"outcome": "ignored", "answer": None, "inbound": None}
        if log_now:
            result["inbound"] = self._record_inbound(inbound, "ignored", count_today=count)["id"]
            self.notify(Notice(kind="notice", klass="operational", route="default", priority="low",
                               title="Someone else messaged the bot",
                               body=f"Someone who is not you messaged the bot {count} time{'s' if count != 1 else ''} "
                                    "today. They can't do anything. If it keeps happening, make a new bot in Phone setup.",
                               fields={"channel": name, "count": count, "inbox_only": True},
                               dedupe_key=f"stranger:{name}:{today}"))
        return result

    def _cap(self, cap: str | None) -> tuple[dict | None, str | None]:
        if not cap:
            return None, None
        return self._caps.get(digest(cap)), digest(cap)

    def _button(self, inbound: Inbound, now: datetime, third: bool) -> dict:
        name = inbound.channel
        with self._lock:
            record, hashed = self._cap(inbound.cap)
            valid = (record is not None and record.get("verb") == inbound.verb and record.get("channel") == name
                     and not record.get("used"))
            expired = valid and (parse_iso(record.get("expires")) or now) <= now
            entry = self._entries.get(record["message"]) if record else None
            if valid and not expired:
                record["used"] = iso(now)
                self._save_caps()
        if not valid or expired:
            if not expired:
                held, count = self._rate(name, self.monotonic())
                if held and not self._held(name):
                    self._hold(name, count)
            if self._entry_of(name).get("type") == "ntfy" and not expired:
                rec = self._record_inbound(Inbound(**{**asdict(inbound), "kind": "forged"}), "dropped")
                self._alert_forged(name, "forged")
                return {"outcome": "forged", "answer": None, "inbound": rec["id"]}
            outcome = "expired" if expired else "refused"
            rec = self._record_inbound(inbound, outcome, message=record.get("message") if record else None)
            return {"outcome": outcome, "answer": EXPIRED if expired else REFUSED_TAP, "inbound": rec["id"]}
        verb = inbound.verb or ""
        if self._state.get("tamper") and verb != "stop":
            rec = self._record_inbound(inbound, UNDER_REVIEW, message=entry["id"] if entry else None)
            return {"outcome": "dropped", "answer": None, "inbound": rec["id"]}
        if self._held(name) and verb != "stop":
            rec = self._record_inbound(inbound, "dropped (hold)", message=entry["id"] if entry else None)
            return {"outcome": "dropped", "answer": HOLD_DROPPED, "inbound": rec["id"]}
        if verb != "stop" and self._late(inbound, entry, now):
            rec = self._record_inbound(inbound, "late, not applied", message=entry["id"] if entry else None,
                                       late=True, pending_verb=verb)
            return {"outcome": "late", "answer": LATE_TAP, "inbound": rec["id"]}
        answer = self._apply(verb, entry, inbound, now, third)
        if self._held(name):
            answer = None   # a stop in a hold: the hold notice said only stop works; no answer per tap
        rec = self._record_inbound(inbound, "applied", message=entry["id"] if entry else None)
        return {"outcome": "applied", "answer": answer, "inbound": rec["id"]}

    def _latest_for(self, name: str, reply_to: str | None) -> dict | None:
        with self._lock:
            entries = sorted(self._entries.values(), key=lambda e: e["id"], reverse=True)
        if reply_to:
            for entry in entries:
                delivery = (entry.get("deliveries") or {}).get(name) or {}
                if reply_to in str(delivery.get("remote_id") or "").split(","):
                    return entry
        for entry in entries:
            delivery = (entry.get("deliveries") or {}).get(name) or {}
            if delivery.get("state") == "sent" and entry.get("kind") not in ("confirm",):
                return entry
        return None

    def _text(self, inbound: Inbound, now: datetime, third: bool) -> dict:
        name = inbound.channel
        if self._entry_of(name).get("type") == "ntfy" and inbound.topic and inbound.topic != \
                self._entry_of(name).get("chat_topic", "seraphina-chat"):
            rec = self._record_inbound(Inbound(**{**asdict(inbound), "kind": "forged"}), "dropped")
            self._alert_forged(name, "forged")
            return {"outcome": "forged", "answer": None, "inbound": rec["id"]}
        form, verb, arg = parse_command(inbound.text)
        entry = self._latest_for(name, inbound.reply_to)
        if self._state.get("tamper") and not (form == "command" and verb == "stop"):
            # S47: while a settings change is under review a phone can only stop (an account added by hand
            # to the pairing file is obeyed in nothing else)
            record = Inbound(**{**asdict(inbound), "kind": "command", "verb": verb}) if form != "text" else inbound
            rec = self._record_inbound(record, UNDER_REVIEW)
            return {"outcome": "dropped", "answer": None, "inbound": rec["id"]}
        if form in ("command", "near", "refused"):
            held, count = self._rate(name, self.monotonic())
            if held and not self._held(name):
                self._hold(name, count)
            command = Inbound(**{**asdict(inbound), "kind": "command", "verb": verb})
            if self._held(name) and verb != "stop":
                rec = self._record_inbound(command, "dropped (hold)")
                return {"outcome": "dropped", "answer": HOLD_DROPPED, "inbound": rec["id"]}
            if form == "near":
                rec = self._record_inbound(command, "asked")
                return {"outcome": "asked", "answer": DID_YOU_MEAN[verb][1 if third else 0], "inbound": rec["id"]}
            if form == "refused":
                rec = self._record_inbound(command, "refused")
                return {"outcome": "refused", "answer": REFUSED[verb], "inbound": rec["id"]}
            if verb != "stop" and self._late(inbound, None, now):
                sent = parse_iso(inbound.sent_at)
                rec = self._record_inbound(command, "late, not applied", late=True, pending_verb=verb,
                                           message=entry["id"] if entry else None)
                when = self.clock.hhmm(sent) if sent else "earlier"
                said = next((words for words, known in COMMANDS.items() if known == verb), verb) + \
                    (f" {arg}" if arg else "")   # the command as the grammar knows it, never the typed text
                return {"outcome": "late", "inbound": rec["id"],
                        "answer": f"You sent '{said}' at {when}; it arrived at "
                                  f"{self.clock.hhmm(now)} and was not applied. Open the cockpit to apply it."}
            answer = self._apply(verb, entry, command, now, third, arg=arg)
            if self._held(name):
                answer = None   # a stop in a hold: the hold notice said only stop works; no answer per command
            rec = self._record_inbound(command, "applied", message=entry["id"] if entry else None)
            return {"outcome": "applied", "answer": answer, "inbound": rec["id"]}
        if self._held(name):   # S35: during a hold only stop works; text is logged and dropped
            rec = self._record_inbound(inbound, "dropped (hold)")
            return {"outcome": "dropped", "answer": None, "inbound": rec["id"]}
        return self._free_text(inbound, now, third, entry)

    def _free_text(self, inbound: Inbound, now: datetime, third: bool, entry: dict | None) -> dict:
        name = inbound.channel
        settings = self._settings().get("inbound") or {}
        per_minute = int(settings.get("text_per_minute") or 6)
        per_day = int(settings.get("text_per_day") or 40)
        window = self._text_windows.setdefault(name, deque())
        mono = self.monotonic()
        window.append(mono)
        while window and mono - window[0] > 60:
            window.popleft()
        today = self.clock.local(now).date().isoformat()
        with self._lock:
            counts = self._rules.setdefault("text_day", {})
            record = counts.get(name) or {}
            if record.get("day") != today:
                record = {"day": today, "count": 0}
            record["count"] = int(record.get("count") or 0) + 1
            counts[name] = record
            self._save_rules()
        over = len(window) > per_minute or record["count"] > per_day
        persona = str((entry or {}).get("persona") or self._settings().get("default_persona") or "seraphina")
        sent_to_persona = False
        send = getattr(self.factory, "persona_send", None)
        if callable(send) and getattr(self.factory, "wakes_ready", lambda: False)() and not over:
            with contextlib.suppress(Exception):
                send(persona, self._redact(inbound.text), via=self._entry_of(name).get("type") or name, channel=name,
                     sent_at=inbound.sent_at, forwarded=inbound.forwarded)
                sent_to_persona = True
        outcome = "sent to the thread" if sent_to_persona else ("kept, over the limit" if over else "kept")
        rec = self._record_inbound(inbound, outcome, persona=persona if sent_to_persona else None,
                                   message=entry["id"] if entry else None, untrusted=inbound.forwarded or None)
        answer = None
        if not sent_to_persona:
            answer = self._once_a_day(name, "kept", KEPT_THIRD if third else KEPT)
        if third and self._entry_of(name).get("type") == "telegram":
            warned = self._channels.get(name).state().get("warned") if name in self._channels else True
            if not warned:
                self._channels[name].update_state(lambda s: s.__setitem__("warned", iso(now)))
                answer = f"{TELEGRAM_WARNING} {answer or ''}".strip()
        return {"outcome": outcome, "answer": answer, "inbound": rec["id"]}

    def _apply(self, verb: str, entry: dict | None, inbound: Inbound, now: datetime, third: bool,
               arg: str | None = None) -> str | None:
        """Apply one verb from the phone; the answer to send back (None when the confirmation goes out as a
        notice on every channel)."""
        via = inbound.channel
        if verb == "ack":
            if entry is None:
                return "Nothing to mark as read."
            events: list = []
            with self._lock:
                self._ack(entry, "phone", via, now, events)
                self._save_entry(entry)
            self._publish(events)
            return "Marked as read."
        if verb.startswith("snooze_"):
            if entry is None:
                return "Nothing to snooze."
            updates: list = []
            if verb == "snooze_morning":
                until = self._morning(now)
            else:
                until = now + timedelta(hours=1 if verb == "snooze_1h" else 3)
            with self._lock:
                said = self._snooze(entry, until, now, updates)
                self._save_entry(entry)
            for channel, remote, rendered in updates:
                with contextlib.suppress(Exception):
                    channel.update(remote, rendered)
            self._kick()
            return f"Snoozed until {said}"
        if verb.startswith("quiet_"):
            until = self._morning(now) if verb == "quiet_morning" else now + timedelta(hours=3)
            with self._lock:
                self._state["quiet_until"] = iso(until)
                self._state.setdefault("quiet_from", iso(now))
                self._save_state()
            self.audit.write("quiet", until=iso(until), via=via)
            self._confirm("quiet", until=self.clock.hhmm(until))
            return None
        if verb == "stop":
            if self.paused() is not None:   # already stopped: one answer to this channel, no new confirmations
                return ("Your assistant is already stopped. Restart it in the cockpit on your computer." if third
                        else "Seraphina is already stopped. Restart her in the cockpit on your computer.")
            control = getattr(self.factory, "wake_control", None)
            done = False
            if callable(control):
                with contextlib.suppress(Exception):
                    control("pause", by="phone", via=via)
                    done = True
            if not done:
                self.pause(by="phone", via=via)
            self._confirm("stopped", time=self.clock.hhmm(now))
            return None
        if verb == "mute":
            ready = getattr(self.factory, "wakes_ready", lambda: False)()
            control = getattr(self.factory, "wake_control", None)
            if not ready or not callable(control):
                return ("Seraphina is not set up yet, so there is nothing to mute. To hold messages, send quiet 3h."
                        if not third else "There is nothing to mute yet. To hold messages, send quiet 3h.")
            index = int(arg) if isinstance(arg, str) and arg.isdigit() else None
            try:
                target = self.factory.wake_mute_target((entry or {}).get("persona"), index)
                result = control("mute", by="phone", via=via, persona=target)
            except Exception:  # noqa: BLE001 - an unknown number: say so, change nothing
                return "There is no assistant with that number. Send status to see the list."
            until = parse_iso(((result or {}).get("personas") or {}).get(target, {}).get("until"))
            display = target[:1].upper() + target[1:]
            self._confirm("muted", time=self.clock.hhmm(now), until=self.clock.hhmm(until) if until else "morning",
                          persona=display)
            return None
        if verb == "status":
            return self._status_text(third)
        if verb == "help":
            return HELP if not third else HELP.replace("Seraphina", "your assistant")
        return None

    def answer(self, channel: str, text: str) -> dict:
        """Send fixed, code-written text back to one channel (the answer to a command or a tap), at low priority,
        through the outbox (held while settings are under review, S47); it lands in the inbox already read. The
        gate sends an answer as written on both channel kinds: _apply wrote it for that channel's class."""
        return self.notify(Notice(kind="answer", klass="operational", route="needs_you", priority="low",
                                  title="Your server", body=text,
                                  fields={"only_channel": channel, "at": self.clock.stamp()}))

    def _morning(self, now: datetime) -> datetime:
        window, _ = quiet_window(self.config)
        local = self.clock.local(now)
        if window is not None:
            return window.next_end(local).astimezone(UTC)
        return now + timedelta(hours=12)

    def _confirm(self, short: str, **values: str) -> None:
        """Confirm a stop, quiet, mute, unpair or hold at once on every channel, silently, even in quiet hours."""
        fields = {"short": short, "at": self.clock.stamp(), **values}
        title, body = short_text(short, False, persona=values.get("persona") or "Seraphina", fields=fields)
        self.notify(Notice(kind="confirm", klass="short", route="needs_you", priority="low", title=title, body=body,
                           fields=fields, dedupe_key=f"confirm:{short}:{self.clock.local():%Y-%m-%dT%H:%M}"))

    def _status_text(self, third: bool) -> str:
        jobs = _call(self.factory, "jobs", default=[]) or []
        waiting = sum(1 for j in jobs if isinstance(j, dict) and j.get("stage") in HUMAN_STAGES)
        running = sum(1 for j in jobs if isinstance(j, dict)
                      and j.get("stage") in ("queued", "spec", "build", "review", "approve", "merging"))
        with self._lock:
            unread = sum(1 for e in self._entries.values() if not e.get("read")
                         and e.get("kind") not in ("summary", "confirm", "test"))
        parts = [f"{waiting} job{'s wait' if waiting != 1 else ' waits'} for you.",
                 f"{running} job{'s are' if running != 1 else ' is'} running.",
                 f"{unread} unread message{'s' if unread != 1 else ''} in the cockpit."]
        if self.paused():
            parts.append("Your assistant is stopped." if third else "Seraphina is stopped.")
        if getattr(self.factory, "wakes_ready", lambda: False)():
            names = sorted(_call(self.factory, "wake_persona_names", default=[]) or [])
            if names:
                listed = [f"{n} {'assistant ' + str(n) if third else name[:1].upper() + name[1:]}"
                          for n, name in enumerate(names, 1)]
                parts.append("Assistants: " + ", ".join(listed) + " (mute <n> mutes one).")
        return " ".join(parts)

    def apply_inbound(self, inbound_id: str, by: str = "human") -> dict:
        """[Apply now] in the cockpit for a late command or tap: the owner applies it from the cockpit."""
        if not INBOUND_ID_RE.fullmatch(str(inbound_id)):
            raise NotifyError("inbound id must look like I-000012")
        record = next((line for line in self._inbound_lines(limit=5000) if line.get("id") == inbound_id), None)
        if record is None:
            raise KeyError(f"inbound {inbound_id}")
        verb = record.get("pending_verb")
        if not record.get("late") or not verb:
            raise NotifyError("only a late command or tap that was not applied can be applied now")
        applied = {line.get("applies") for line in self._inbound_lines(limit=5000) if line.get("applies")}
        if inbound_id in applied:
            raise NotifyError("it was applied already")
        entry = self._entries.get(str(record.get("message") or ""))
        now = self._now()
        third = self._privacy_of(self._entry_of(str(record.get("channel")))) != PRIVACY_TAILNET
        answer = self._apply(verb, entry, Inbound(channel=str(record.get("channel")), kind="command", verb=verb),
                             now, third)
        line = {"id": f"I-{int(self._state.get('inbound_seq') or 0) + 1:06d}", "at": iso(now), "kind": "cockpit",
                "channel": record.get("channel"), "verb": verb, "outcome": "applied from the cockpit",
                "applies": inbound_id, "message": record.get("message"), "by": by}
        with self._lock:
            self._state["inbound_seq"] = int(self._state.get("inbound_seq") or 0) + 1
            self._save_state()
        _append_line(self.root / f"inbound-{now.strftime('%Y-%m')}.jsonl", line)
        self.audit.write("inbound.applied", inbound=inbound_id, verb=verb, by=by)
        return {"applied": verb, "answer": answer}

    # -- receivers -----------------------------------------------------------------------------------

    def _supervise_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.supervise()
            except Exception as exc:  # noqa: BLE001
                self._report_error("receivers", exc)
            self._stop.wait(1.0)

    def supervise(self) -> None:
        """Start, stop and restart receivers: one per enabled, ready channel, only while phone messages are on
        (notify.enabled), once the clock is set (S50) and while the sandbox precondition holds; a restart only
        when the channel's restart keys changed, the old receiver joined first so two pollers never overlap."""
        ready = self.time_sync.ready()
        enabled = self._settings().get("enabled") is not False
        wanted: dict[str, tuple] = {}
        with self._lock:
            channels = dict(self._channels)
        for name, channel in channels.items():
            entry = self._entry_of(name)
            if (ready and enabled and channel.can_receive and entry.get("enabled", True) and channel.can_listen()
                    and self.precondition()["ok"] and not self._stop.is_set()):
                wanted[name] = channel.restart_keys()
        with self._lock:
            running = dict(self._receivers)
        for name, (thread, stop, key) in running.items():
            if name not in wanted or wanted[name] != key or not thread.is_alive():
                stop.set()
                channel = channels.get(name)
                if channel is not None:
                    channel.close()
                timeout = getattr(channel, "http_timeout", None) or getattr(channel, "stream_timeout", 10.0)
                thread.join(float(timeout) + 5.0)
                with self._lock:
                    self._receivers.pop(name, None)
        for name, key in wanted.items():
            with self._lock:
                if name in self._receivers:
                    continue
                stop = threading.Event()
                thread = threading.Thread(target=self._receive, args=(name, stop), name=f"notify-in-{name}",
                                          daemon=True)
                self._receivers[name] = (thread, stop, key)
            thread.start()

    def _receive(self, name: str, stop: threading.Event) -> None:
        channel = self._channels.get(name)
        if channel is None:
            return
        try:
            for inbound in channel.receive(stop):
                if stop.is_set():
                    break
                try:
                    result = self.handle_inbound(inbound)
                except Exception as exc:  # noqa: BLE001 - one bad message never stops the channel
                    self._report_error(f"inbound {name}", exc)
                    result = {"answer": None}
                answer = result.get("answer")
                if answer:
                    answer, reasons = filter_text(answer, terms=self._terms(), secrets=self._secrets())
                    if reasons:
                        answer = None
                if answer and not self._may_answer(name):
                    answer = None   # off, without the sandbox, or under review: nothing goes back
                toast = None
                if answer and inbound.kind == "button" and channel.type == "telegram":
                    toast = answer   # the callback answer: a toast on the tap itself, not a message
                elif answer and not (inbound.kind == "button" and result.get("outcome") == "applied"):
                    # Every other answer is a notice through the outbox (held like any phone send, and in the
                    # inbox). A tap that worked needs none on ntfy: Got it clears the notification, Snooze
                    # replaces it with "Snoozed until ...".
                    self.answer(name, answer)
                with contextlib.suppress(Exception):
                    channel.finish(inbound, toast)   # Telegram: answers the tap, removes its buttons, deletes text
        except Exception as exc:  # noqa: BLE001
            if not stop.is_set():
                self._report_error(f"receiver {name}", exc)

    # -- the cockpit's channel actions ----------------------------------------------------------------

    def test(self, channel: str) -> dict:
        """A test notice with a Got it button, to one channel, at once (even in do-not-disturb time)."""
        self.channel(channel)
        return self.notify(Notice(kind="test", klass="short", route="needs_you", priority="default",
                                  title="Test", body="This is a test from your server. Tap Got it.",
                                  fields={"only_channel": channel, "at": self.clock.stamp()}))

    def pair(self, channel: str) -> dict:
        target = self.channel(channel)
        if self._settings().get("enabled") is False:
            raise NotifyError("Phone messages are turned off (notify.enabled): turn them on before pairing")
        if not self.precondition()["ok"]:
            raise NotifyError("Phone messages are off while your AI agents run without the sandbox: turn on bwrap "
                              "(Settings, Harness) or accept the risk first")
        if target.type == "telegram":
            minutes = float((self._settings().get("inbound") or {}).get("pairing_minutes") or 10)
            try:
                result = target.start_pairing(minutes=minutes)   # type: ignore[call-arg]
            except ChannelError as exc:
                raise NotifyError(str(exc)) from None
            self.audit.write("pairing.started", channel=channel)
            self._kick_supervisor()
            return result
        return target.start_pairing() or {}

    def _kick_supervisor(self) -> None:
        if self._started:
            with contextlib.suppress(Exception):
                self.supervise()

    def pair_status(self, channel: str) -> dict:
        return self.channel(channel).pair_status()

    @_settings_write
    def pair_confirm(self, channel: str, code: str) -> dict:
        target = self.channel(channel)
        if not isinstance(code, str) or not re.fullmatch(r"\s*\d{6}\s*", code):
            raise NotifyError("the code is the 6 digits your bot sent you")
        result = target.confirm_pairing(code) or {"state": "waiting", "account": None}
        self.audit.write("pairing.confirm", channel=channel, state=result.get("state"))
        return result

    @_settings_write
    def unpair(self, channel: str, chat_id: Any) -> dict:
        target = self.channel(channel)
        if not target.unpair(chat_id):
            raise KeyError(f"no paired account {chat_id} on {channel}")
        self.audit.write("unpair", channel=channel, account=chat_id)
        self._confirm("unpaired", time=self.clock.hhmm())
        return {"channel": channel, "paired": target.paired()}

    @_settings_write
    def channel_action(self, channel: str, action: str, token: str | None = None) -> dict:
        """rotate | leaked | disable (S51). Telegram leaked/rotate needs the new bot token from @BotFather;
        ntfy rotate makes a new reply token and phone passphrase, leaked also a new harness token."""
        target = self.channel(channel)
        if action not in ("rotate", "leaked", "disable"):
            raise NotifyError("action must be rotate, leaked or disable")
        if action == "disable":
            self._patch_channel(channel, {"enabled": False})
            self.audit.write("channel.disabled", channel=channel)
            return {"channel": channel, "enabled": False}
        entry = self._entry_of(channel)
        if target.type == "telegram":
            if not isinstance(token, str) or not token.strip():
                raise NotifyError("paste the new bot token from @BotFather (/revoke made the old one useless)")
            self._set_secret(str(entry.get("token_env") or "SERAPHINA_TELEGRAM_BOT_TOKEN"), token.strip())
            target.unpair_all()
            self.audit.write("channel.token", channel=channel, action=action)
            return {"channel": channel, "paired": [], "next": "Pair your phone again."}
        result = self.prepare_ntfy(channel, rotate_harness=(action == "leaked"))
        self.audit.write("channel.token", channel=channel, action=action)
        return {"channel": channel, **result}

    @_settings_write
    def lost_phone(self) -> dict:
        """[Lost my phone]: unpair Telegram, stop publishing to ntfy, stop Seraphina, rotate the ntfy phone
        credentials and say how to sign in again (S51)."""
        done: list[str] = []
        blocks: list[dict] = []
        passphrase = None
        for name, entry in (self._settings().get("channels") or {}).items():
            if not isinstance(entry, dict) or name not in self._channels:
                continue
            channel = self._channels[name]
            if channel.type == "telegram":
                count = channel.unpair_all()
                done.append(f"unpaired {count} Telegram account{'s' if count != 1 else ''} on {name}")
            elif channel.type == "ntfy":
                self._patch_channel(name, {"enabled": False})
                done.append(f"stopped publishing to {name}")
                with contextlib.suppress(NotifyError, OSError):
                    prepared = self.prepare_ntfy(name, rotate_harness=False)
                    blocks += prepared.get("copy_blocks") or []
                    passphrase = prepared.get("passphrase")
                    done.append(f"made new phone credentials for {name}")
        self.pause(by="human", via="lost my phone")
        done.append("stopped Seraphina")
        self.audit.write("lost_phone", done=done)
        return {"done": done, "copy_blocks": blocks, "passphrase": passphrase,
                "next": "On the new phone, add the user owner-phone with the new passphrase, then turn the private "
                        "channel on again in Phone setup."}

    def _patch_channel(self, name: str, change: dict) -> None:
        self._patch({"notify": {"channels": {name: change}}})

    def _set_secret(self, name: str, value: str) -> None:
        setter = getattr(self.factory, "set_secret", None)
        if callable(setter):
            setter(name, value)
        else:
            envfile.set_secret(self.config, name, value)

    # -- ntfy setup (§2.7, §4.2) -------------------------------------------------------------------------

    @_settings_write
    def prepare_ntfy(self, channel: str, rotate_harness: bool = True) -> dict:
        """[Prepare the settings]: generate the tokens (into the env file) and the owner-phone passphrase, hash
        the three passwords with `ntfy user hash`, and stage server.yml and ntfy-secrets.env in
        <data_dir>/notify/ntfy-setup/ for the owner to install with sudo. Returns {copy_blocks, passphrase};
        the passphrase is in this answer only. No token is ever printed."""
        target = self.channel(channel)
        if target.type != "ntfy":
            raise NotifyError("prepare is for an ntfy channel")
        entry = self._entry_of(channel)
        token_env = str(entry.get("token_env") or "SERAPHINA_NTFY_TOKEN")
        reply_env = str(entry.get("reply_token_env") or "SERAPHINA_NTFY_REPLY_TOKEN")
        harness = self.environ.get(token_env) if not rotate_harness else None
        harness = harness or ntfy_token()
        reply = ntfy_token()
        passphrase = phone_passphrase()
        hashes = {}
        for user, password in (("seraphina", secrets.token_urlsafe(24)), ("owner-phone", passphrase),
                               ("phone-reply", secrets.token_urlsafe(24))):
            hashed = self._ntfy_hash(password)
            if hashed is None:
                raise NotifyError("ntfy is not installed yet: run copy block 2a, then Prepare the settings again")
            hashes[user] = hashed
        self._set_secret(token_env, harness)
        self._set_secret(reply_env, reply)
        topics = (str(entry.get("topic") or "seraphina"), str(entry.get("taps_topic") or "seraphina-taps"),
                  str(entry.get("chat_topic") or "seraphina-chat"))
        setup = self.root / "ntfy-setup"
        setup.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_atomic(setup / "server.yml", ntfy_server_yml(entry, topics))
        write_atomic(setup / "ntfy-secrets.env", ntfy_secrets_env(hashes, harness, reply))
        write_atomic(setup / "ntfy-override.conf", "[Service]\nEnvironmentFile=/etc/ntfy/ntfy-secrets.env\n")
        for path in setup.iterdir():
            os.chmod(path, 0o600)
        self.audit.write("ntfy.prepared", channel=channel, rotated_harness=rotate_harness)
        self._record_baseline("ntfy prepared")
        return {"copy_blocks": ntfy_copy_blocks(setup, entry), "passphrase": passphrase,
                "setup_dir": str(setup)}

    def _ntfy_hash(self, password: str) -> str | None:
        output = self._run(["ntfy", "user", "hash"], input_text=f"{password}\n{password}\n")
        match = re.search(r"\$2[aby]\$\d{2}\$[./A-Za-z0-9]{53}", output or "")
        return match.group(0) if match else None

    def check_ntfy(self, channel: str) -> dict:
        """[Check again]: the socket or listener, /v1/health, each topic's access, the serve config and Funnel.
        Deletes the staged setup folder once the access rules work."""
        target = self.channel(channel)
        if target.type != "ntfy":
            raise NotifyError("check is for an ntfy channel")
        self._refresh_net()
        entry = self._entry_of(channel)
        publish = str(entry.get("publish_url") or "")
        socket_ok = os.path.exists(publish[5:]) if publish.startswith("unix:") else None
        healthy, detail = target.check_health()   # type: ignore[attr-defined]
        topics = target.topics()                   # type: ignore[attr-defined]
        reply = self.environ.get(str(entry.get("reply_token_env") or ""))
        reads = {topic: target.poll(topic) for topic in topics}   # type: ignore[attr-defined]
        acl_ok = healthy and all(status == 200 for status in reads.values())
        if acl_ok and reply:
            acl_ok = target.poll(topics[1], token=reply) == 403   # type: ignore[attr-defined]
        status = self._tailnet() or {}
        serve = self._run(["tailscale", "serve", "status", "--json"]) or ""
        funnel_off = '"AllowFunnel"' not in serve and not status.get("funnel")
        setup = self.root / "ntfy-setup"
        if acl_ok and setup.is_dir():
            shutil.rmtree(setup, ignore_errors=True)
        return {"health": {"ok": healthy, "detail": detail}, "acl_ok": acl_ok, "reads": reads,
                "tiers_ok": None, "socket": socket_ok, "serve": bool(serve.strip()), "funnel_off": funnel_off,
                **ntfy_port_80(publish.startswith("unix:") and self._ntfy_on_port(80), self._ports_in_use()),
                "privacy": self._privacy_of(entry), "install_block": ntfy_install_block()}

    def _ports_in_use(self) -> set[int]:
        from . import listeners

        return listeners.ports_in_use()

    def _ntfy_on_port(self, port: int) -> bool:
        """True when an ntfy server answers on 127.0.0.1:port: with the Unix-socket setup nothing should
        (whether `listen-http: "-"` turns ntfy's default :80 listener off is UNVERIFIED, so it is checked)."""
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2.0)
        try:
            conn.request("GET", "/v1/health")
            response = conn.getresponse()
            return '"healthy"' in response.read(4096).decode("utf-8", "replace")
        except (OSError, http.client.HTTPException):
            return False
        finally:
            conn.close()


def ntfy_port_80(ntfy_answers: bool, in_use: set[int]) -> dict:
    """{tcp_listener, port80_other} for the ntfy check: whether ntfy itself answers on 127.0.0.1:80 (with the Unix
    socket setup it should not), and whether another program listens on port 80 (a web server or login gateway,
    which is fine: only root can tell which program, so it is not named)."""
    return {"tcp_listener": bool(ntfy_answers), "port80_other": not ntfy_answers and 80 in in_use}


# ---------------------------------------------------------------- ntfy setup text


def ntfy_token() -> str:
    """tk_ plus 29 random lowercase letters and digits (ntfy requires tk_ and 32 characters)."""
    alphabet = string.ascii_lowercase + string.digits
    return "tk_" + "".join(secrets.choice(alphabet) for _ in range(29))


def phone_passphrase() -> str:
    """A long passphrase for the owner-phone user, typed once into the ntfy app."""
    words = ("amber", "birch", "cedar", "delta", "ember", "fjord", "grove", "harbor", "indigo", "juniper", "kestrel",
             "lantern", "meadow", "nectar", "orchid", "pebble", "quartz", "river", "saffron", "thistle", "umber",
             "violet", "willow", "yarrow", "zephyr", "basil", "coral", "dune", "fern", "heron", "iris", "lark")
    return "-".join(secrets.choice(words) for _ in range(5)) + "-" + str(secrets.randbelow(90) + 10)


def _ntfy_urls(entry: Mapping[str, Any]) -> tuple[str, str]:
    """(public URL, publish URL) for the staged files and copy blocks: only URLs that pass the settings checks
    (channels.public_url_ok, channels.local_publish_url), so nothing else can reach server.yml or a root command
    (NotifyError otherwise)."""
    from .channels import local_publish_url, public_url_ok

    public = entry.get("public_url") or "https://<host>.<tailnet>.ts.net:8445"
    publish = entry.get("publish_url") or "unix:/run/ntfy/ntfy.sock"
    if entry.get("public_url") is not None and not public_url_ok(public):
        raise NotifyError("notify.channels: public_url must be a plain https URL (a host and a port)")
    if not local_publish_url(publish):
        raise NotifyError("notify.channels: publish_url must be unix:/path or http://127.0.0.1:<port>")
    return str(public), str(publish)


def _yaml(value: str) -> str:
    """value as a YAML double-quoted scalar (JSON's quoting is valid YAML)."""
    return json.dumps(value)


def ntfy_server_yml(entry: Mapping[str, Any], topics: tuple[str, str, str]) -> str:
    notices, taps, chat = topics
    base, unix = _ntfy_urls(entry)
    listen = (f"listen-unix: {_yaml(unix[5:])}\nlisten-unix-mode: 0666\nlisten-http: \"-\"\n"
              if unix.startswith("unix:") else 'listen-http: "127.0.0.1:2586"\n')
    access = [f"seraphina:{notices}:rw", f"seraphina:{taps}:ro", f"seraphina:{chat}:ro", f"owner-phone:{notices}:ro",
              f"owner-phone:{chat}:rw", f"phone-reply:{taps}:wo"]
    return (
        "# Written by the harness for the private phone channel (Wake & Messages §2.7). No secret is in here:\n"
        "# the users' password hashes and the tokens are in ntfy-secrets.env.\n"
        f"base-url: {_yaml(base)}\n"
        f"{listen}"
        "# listen-http \"-\" turns off ntfy's default :80 listener (cmd/serve.go), so no TCP port is open\n"
        'auth-file: "/var/lib/ntfy/user.db"\n'
        'auth-default-access: "deny-all"\n'
        "auth-access:\n"
        + "".join(f"  - {_yaml(line)}\n" for line in access) +
        'cache-file: "/var/cache/ntfy/cache.db"\n'
        'cache-duration: "24h"\n'
        "web-root: disable\n"
        "visitor-message-daily-limit: 50\n"
        "# Nothing that sends text off the machine: no firebase-key-file, smtp-sender-addr, twilio-account,\n"
        "# web push or upstream-base-url (the iPhone relay is added only with your consent).\n"
    )


def ntfy_secrets_env(hashes: Mapping[str, str], harness: str, reply: str) -> str:
    users = ",".join(f"{user}:{hashes[user]}:user" for user in ("seraphina", "owner-phone", "phone-reply"))
    return ("# Installed 0600 root:root as /etc/ntfy/ntfy-secrets.env and loaded by the ntfy unit's drop-in.\n"
            f"NTFY_AUTH_USERS='{users}'\n"
            f"NTFY_AUTH_TOKENS='seraphina:{harness}:harness,phone-reply:{reply}:buttons'\n")


def ntfy_install_block() -> dict:
    return {"id": "2a", "title": "Add the ntfy package repository and install ntfy (run once, with sudo)",
            "commands": ["sudo mkdir -p /etc/apt/keyrings",
                         "sudo curl -L -o /etc/apt/keyrings/ntfy.gpg https://archive.ntfy.sh/apt/keyring.gpg",
                         'echo "deb [signed-by=/etc/apt/keyrings/ntfy.gpg] https://archive.ntfy.sh/apt stable main" '
                         "| sudo tee /etc/apt/sources.list.d/ntfy.list",
                         "sudo apt update", "sudo apt install ntfy"],
            "expect": "Setting up ntfy …", "if_not": "If it says Unable to locate package, run this block again."}


def ntfy_copy_blocks(setup: Path, entry: Mapping[str, Any]) -> list[dict]:
    public, unix = _ntfy_urls(entry)
    port = str(urllib.parse.urlsplit(public).port or 8445) if not public.startswith("https://<") else "8445"
    target = shlex.quote(unix if unix.startswith("unix:") else "http://127.0.0.1:2586")

    def staged(name: str) -> str:
        return shlex.quote(str(setup / name))
    return [
        ntfy_install_block(),
        {"id": "2b", "title": "Install the settings, add the daily limits and start ntfy",
         "commands": [f"sudo install -m 0644 -o root -g root {staged('server.yml')} /etc/ntfy/server.yml",
                      f"sudo install -m 0600 -o root -g root {staged('ntfy-secrets.env')} /etc/ntfy/ntfy-secrets.env",
                      "sudo mkdir -p /etc/systemd/system/ntfy.service.d",
                      f"sudo install -m 0644 {staged('ntfy-override.conf')} "
                      "/etc/systemd/system/ntfy.service.d/secrets.conf",
                      "sudo systemctl daemon-reload", "sudo systemctl enable --now ntfy",
                      "sudo ntfy tier add --message-limit=100 seraphina-limits",
                      "sudo ntfy user change-tier seraphina seraphina-limits",
                      "sudo ntfy user change-tier owner-phone seraphina-limits",
                      "sudo ntfy user change-tier phone-reply seraphina-limits",
                      "systemctl is-active ntfy"],
         "expect": "active", "if_not": "If it is not active, run: sudo journalctl -u ntfy -n 30"},
        {"id": "2c", "title": "Serve it on your tailnet only",
         "warning": "Your server's tailnet name will appear in public certificate logs. If the name says anything "
                    "about you, rename the machine in Tailscale first.",
         "commands": [f"sudo tailscale serve --bg --https={port} {target}"],
         "expect": "Available within your tailnet",
         "if_not": "If your Tailscale does not accept a unix: target, use the 127.0.0.1:2586 listener instead "
                   "(Advanced)."},
    ]


def hook_snippets(name: str, spec: Mapping[str, Any], url: str) -> list[dict]:
    """Copy blocks for an event source (§4.2 Add a source). The token never appears in a command line (argv is
    readable in /proc by every local user): the owner pastes it once into a root-only header file, and every
    snippet reads it with curl -H @file (curl 7.55; --retry-all-errors needs 7.71)."""
    header = f"/etc/seraphina/{name}.header"
    subject = next(iter(spec.get("subjects") or []), None)
    events = list(spec.get("events") or [])
    failed = next((kind for kind in events if kind not in RECOVERS), "backup.failed")
    ok = next((kind for kind in events if kind in RECOVERS), None)

    def body(kind: str) -> str:
        return json.dumps({"kind": kind, **({"subject": subject} if subject else {})}, separators=(",", ":"))
    curl = f"curl -fsS --retry 5 --retry-all-errors -H @{header} -H 'Content-Type: application/json'"
    outcomes.retried("event-source snippet")   # curl posts again by itself: an open incident absorbs a repeat
    unit = f"seraphina-{name}@.service"
    blocks = [
        {"id": "header", "title": "Keep the token in a file only root can read (run once, with sudo)",
         "commands": ["sudo install -d -m 0755 /etc/seraphina",
                      f"sudo install -m 0600 -o root -g root /dev/null {header}",
                      f"sudoedit {header}"],
         "note": "In the editor, write one line: Authorization: Bearer <the token shown above>. Save and close.",
         "expect": "No output.", "if_not": "If sudoedit is missing, use: sudo nano " + header},
        {"id": "systemd", "title": "Report from a systemd backup unit, on failure and on success",
         "files": {f"/etc/systemd/system/{unit}": (
             "[Unit]\nDescription=Tell Seraphina: %i\n\n[Service]\nType=oneshot\n"
             f"ExecStart=/usr/bin/{curl} -d '{{\"kind\":\"%i\"" + (f',\"subject\":\"{subject}\"' if subject else "")
             + f"}}' {url}\n")},
         "dropin": ("[Unit]\n" + f"OnFailure=seraphina-{name}@{failed}.service\n"
                    + (f"OnSuccess=seraphina-{name}@{ok}.service\n" if ok else "")),
         "commands": ["systemctl --version | head -1",
                      "sudo systemctl edit <your backup unit>   (paste the [Unit] lines below)",
                      "sudo systemctl daemon-reload"],
         "note": "OnSuccess= needs systemd 249 or later (Ubuntu 22.04 and newer).",
         "expect": "systemd 249 or a higher number.", "if_not": "Older systemd: use the cron wrapper instead."},
        {"id": "cron", "title": "Or wrap a backup command run from root's crontab",
         "files": {f"/usr/local/bin/seraphina-{name}": (
             "#!/bin/sh\n# Runs the command given to it and tells Seraphina how it went.\n"
             f"if \"$@\"; then kind={ok or failed}; else kind={failed}; fi\n"
             f"{curl} -d \"{{\\\"kind\\\":\\\"$kind\\\"" + (f',\\\"subject\\\":\\\"{subject}\\\"' if subject else "")
             + f"}}\" {url} >/dev/null\n")},
         "commands": [f"sudo chmod 0755 /usr/local/bin/seraphina-{name}",
                      f"sudo crontab -e   (use: /usr/local/bin/seraphina-{name} <your backup command>)"],
         "expect": "No output.", "if_not": "The header file is root-only, so the crontab must be root's."},
        {"id": "find", "title": "Not sure which unit is your backup? These list the candidates",
         "commands": ["systemctl list-units --all '*backup*'", "systemctl list-timers --all"],
         "expect": "A unit or timer whose name says backup.", "if_not": "None: use the cron wrapper."},
        {"id": "test", "title": "Send a test event (the cockpit then shows when this source last reported)",
         "commands": [f"sudo {curl} -d '{body(ok or failed)}' {url}"],
         "expect": '{"message": null, "recorded": true}' if ok else '{"message": "M-…"}',
         "if_not": "401: the header file holds another token. 403: this source may not send that kind. "
                   "Connection refused: the harness is not running."},
    ]
    return blocks


# ---------------------------------------------------------------- module helpers


def _cached_tailnet(seconds: float = 600.0) -> Callable[[], dict | None]:
    lock = threading.Lock()
    cache: list[Any] = [0.0, None]

    def get() -> dict | None:
        with lock:
            if cache[0] and time.monotonic() - cache[0] < seconds:
                return cache[1]
        status = tailnet_status()
        with lock:
            cache[0], cache[1] = time.monotonic(), status
        return status
    return get


def _read_server_yml() -> str | None:
    try:
        return NTFY_SERVER_YML.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _run_command(argv: list[str], input_text: str | None = None, timeout: float = 10.0) -> str:
    try:
        done = subprocess.run(argv, input=input_text, capture_output=True, text=True, timeout=timeout,
                              stdin=None if input_text is not None else subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""
    return (done.stdout or "") + (done.stderr or "") if done.returncode == 0 else ""


def _call(target: Any, name: str, *args: Any, default: Any = None, **kwargs: Any) -> Any:
    method = getattr(target, name, None)
    if not callable(method):
        return default
    try:
        return method(*args, **kwargs)
    except Exception:  # noqa: BLE001 - a rule's data source failing is retried at the next reconcile
        return default


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_atomic(path: Path, text: str) -> None:
    """Write text to path atomically, mode 0600 (a temporary file in the same directory, fsync, os.replace)."""
    import tempfile

    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def write_json(path: Path, data: dict) -> None:
    write_atomic(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


_append_lock = threading.Lock()


def _append_line(path: Path, line: dict) -> None:
    with _append_lock:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")


def _overall_state(entry: dict) -> str:
    states = [d.get("state") for d in (entry.get("deliveries") or {}).values()]
    if not states:
        return "inbox"
    for state in ("sending", "queued", "snoozed", "failed", "unknown", "sent", "held", "off"):
        if state in states:
            return state
    return "inbox"


def _changed_keys(section: str, before: Any, after: Any, prefix: str = "") -> list[str]:
    label = f"{section}{prefix}"
    if isinstance(before, dict) and isinstance(after, dict):
        keys: list[str] = []
        for key in sorted(set(before) | set(after)):
            keys += _changed_keys(section, before.get(key), after.get(key), f"{prefix}.{key}")
        return keys
    return [] if before == after else [label]


def _forget_text(entry: dict) -> None:
    """S49: replace the notice's text by its sha256."""
    for key in ("title", "body"):
        value = entry.get(key)
        if isinstance(value, str):
            entry[key] = f"sha256:{digest(value)}"
    fields = entry.get("fields") or {}
    if isinstance(fields.get("detail"), str):
        fields["detail"] = f"sha256:{digest(fields['detail'])}"
    for delivery in (entry.get("deliveries") or {}).values():
        rendered = delivery.get("rendered")
        if isinstance(rendered, dict):
            for key in ("title", "text", "why"):
                if isinstance(rendered.get(key), str):
                    rendered[key] = f"sha256:{digest(rendered[key])}"
    entry["text_removed"] = True


def _rewrite_lines(path: Path, cutoff: datetime) -> None:
    """S49 for a monthly inbound file: text older than cutoff becomes its sha256 (the file is rewritten)."""
    try:
        raw = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return
    out = []
    changed = False
    for text in raw:
        try:
            line = json.loads(text)
        except ValueError:
            out.append(text)
            continue
        at = parse_iso(line.get("at"))
        if isinstance(line.get("text"), str) and at is not None and at < cutoff:
            line["text"] = f"sha256:{digest(line['text'])}"
            changed = True
        out.append(json.dumps(line, ensure_ascii=False))
    if changed:
        write_atomic(path, "\n".join(out) + "\n")


def _plus_minutes(hhmm: str, minutes: int) -> str:
    hour, minute = map(int, hhmm.split(":"))
    total = (hour * 60 + minute + minutes) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"
