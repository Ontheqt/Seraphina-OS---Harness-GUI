"""Disk protection through the SnapRAID Daemon, the harness side (DESIGN.md section 41;
docs/seraphina/BACKUPS_SNAPRAID.md sections 2 to 7 and 12).

Once SnapRAID 15 and the SnapRAID Daemon are in, Seraphina reads the daemon, never the disks, and asks it for the five
actions after the owner's own approval. This module holds that route; swfactory/snapraid.py (the P1 reader's panel)
owns one DaemonRoute and chooses the route.

- MetricsReader reads the daemon's ``/metrics`` (it needs no login) over plain HTTP on loopback and parses only the
  metric names it knows into typed facts (parse_metrics); a malformed line fails that read only. A read changes
  nothing, so a failed one is asked again at the next read (DESIGN §40, outcomes.RETRIED "SnapRAID daemon reads").
- LinkClient talks to the link (bin/sf-snapraidd serve, socket-activated), one JSON line each way over its Unix
  socket, through an injectable connector. The link holds the daemon's login; the harness never does (D7).
- DaemonRoute polls: /metrics every metrics_seconds (active_seconds while a run is active, never under 5 s); a link
  read only for the change counters that moved, at most every reads_min_seconds for the task list, and everything
  every full_read_minutes. It detects restarts from the uptime (confirmed by a start-up probe or the config counter
  going back), classifies runs and reports by the daemon's typed fields (never its texts), computes the light, and
  opens notices and tracker items with counts only.
- Approvals: one record A-<n> per press (<data_dir>/snapraid/actions/, 0600), bound to what the cockpit showed and
  checked again by the link just before sending; one send each; the intent is written before the call, the answer
  after it (outcomes.response); an unknown outcome is looked at again read-only and never sent again by itself.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import os
import re
import socket
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from . import items as tracker
from . import outcomes

METRICS_URL = "http://127.0.0.1:7627/metrics"
LINK_SOCKET = "/run/seraphina-snapraidd/link.sock"
LINK_PROGRAM = "/usr/local/libexec/seraphina/sf-snapraidd"
LINK_CONFIG = "/etc/seraphina/snapraidd.json"
LINK_CREDENTIAL = "/etc/seraphina/snapraidd-basic.cred"
HOOK_HEADER = "/etc/seraphina/snapraidd-hook.header"
DAEMON_CONF = "/etc/snapraidd.conf"
DEFAULTS = {"metrics_url": METRICS_URL, "link": LINK_SOCKET, "metrics_seconds": 60, "active_seconds": 10,
            "reads_min_seconds": 60, "full_read_minutes": 30, "down_minutes": 30, "approval_minutes": 15}
MIN_METRICS_SECONDS = 5
DISKS_MIN_SECONDS = 60
RESTART_SLACK = 10.0
MATCH_SECONDS = 30            # a run's scheduled_at within this many seconds of the POST (daemon clock)
OWN_ACTIVE_SECONDS = 86400    # a taken run request with no run matched stops counting as active after a day
ACT_TIMEOUT = 75.0            # the wait for the link's act: longer than its unit's RuntimeMaxSec (60) with a margin,
                              # so a look after no answer comes when the link can no longer send
# The link's refusals of an act: every one comes before its POST, or after a 429 (nothing queued), so each is a
# failed (refused) outcome. Any other code ("failed", the link's own crash) may come after the POST: unknown.
LINK_REFUSALS = frozenset((
    "bad_request", "unknown_op", "unknown_field", "bad_field", "bad_path", "bad_config", "not_allowed", "auth_off",
    "engine_too_old", "changed", "health", "not_running", "login_refused", "busy", "daemon_error", "no_credential",
    "unsafe_install", "wrong_caller", "used", "no_binding", "too_slow", "one_disk"))
REPORT_WINDOW = 600           # a report task this recent answers a parity.report event
HANDLED_KEEP = 200
TYPED_FACTOR = 10             # an accept of more than ten times a limit asks for the typed count
ACTIONS = ("stop", "hold_off", "maintenance", "accept", "undelete", "heal")
STARTS_RUNS = ("maintenance", "accept", "undelete", "heal")
BAD_HEALTH = ("degraded", "prefail", "failing")
HEALTH_BY_NUMBER = {0: "pending", 1: "passed", 2: "corrupt", 3: "degraded", 4: "prefail", 5: "failing"}
CHANGES = ("equal", "added", "removed", "updated", "moved", "copied", "relocated", "restored")
RUNNING = ("starting", "processing", "finalizing", "stopping")
ENDED = ("terminated", "signaled", "canceled")
DAEMON_UNITS = ("snapraidd.service", "snapraid-daemon.service")
LAB_CHECKS = (
    {"id": "L14", "short": "who can reach Seraphina's link",
     "what": "only Seraphina's own user can reach her link to the protection service, the link alone can read its "
             "password, and the AI agents' sandbox cannot reach it"},
    {"id": "L15", "short": "the actions against the real protection service",
     "what": "every action works as tested here against the real protection service on test disks; until it does, "
             "keep the link's settings at reading only"},
    {"id": "L16", "short": "the reports from the protection service",
     "what": "the protection service's reports reach Seraphina, and a report missed while she was off is read later"},
    {"id": "L17", "short": "a power-off during a run",
     "what": "a power-off during a run stops it safely first, so the next run carries on"},
    {"id": "L13", "short": "starting up with a failed disk", "what": "the server still starts when a disk has failed"},
    {"id": "L20", "short": "replacing a failed disk",
     "what": "the checklist for a failed disk works end to end on test disks, and the next run takes the new disk in"},
)
METRIC_NAMES = frozenset((
    "snapraid_array_health", "snapraid_build_info", "snapraid_array_block_size_bytes",
    "snapraid_array_data_disks_count", "snapraid_array_parity_disks_count", "snapraid_array_files",
    "snapraid_array_blocks", "snapraid_blocks_bad", "snapraid_blocks_unsynced", "snapraid_blocks_unscrubbed",
    "snapraid_last_command_timestamp_seconds", "snapraid_diff_files", "snapraid_config_sync_threshold_deletes",
    "snapraid_config_sync_threshold_updates", "snapraid_config_scrub_percentage",
    "snapraid_config_scrub_older_than_days", "snapraid_config_hold_off", "snapraid_disk_health",
    "snapraid_task_queue_depth", "snapraid_task_active", "snapraid_task_active_progress_percent",
    "snapraid_task_active_eta_seconds", "snapraid_task_active_elapsed_seconds", "snapraid_task_last_exit_code",
    "snapraid_exporter_last_pulse", "snapraid_daemon_uptime_seconds"))
METRIC_RE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{([^{}]*)\})?\s+(-?[0-9.]+(?:[eE][+-]?[0-9]+)?|NaN|[+-]Inf)"
                       r"(?:\s+-?[0-9]+)?$")
LABEL_RE = re.compile(r'\s*([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"\s*(?:,|$)')

# ---------------------------------------------------------------- the words on the page (novice; U7 checks them)

REASONS = {
    "auth_off": "The protection service accepts commands from any program on this server without a password.",
    "open_beyond": "The protection service can be reached from other computers without a password.",
    "failing": "{disk} says it is failing. Replace it soon.",
    "degraded": "{disk} is missing or not answering. The protection service will not run until it is back.",
    "bad_blocks": "Hidden damage was found in {places}.",
    "engine_old": "The protection service cannot run this version of SnapRAID.",
    "frozen_long": "Waiting for you for {days}.",
    "not_protected_days": "Nothing new has been protected for {days}.",
    "frozen": "Waiting for you before protecting the changes.",
    "partial": "The last run was stopped before it finished. It carries on at the next run.",
    "unknown_outcome": ("Seraphina could not tell whether your request reached the protection service. Check before "
                        "trying again."),
    "lapsed": "The protection service restarted, so the next run will not be skipped after all.",
    "two_schedulers": "Two things start protection runs. Only one should.",
    "no_scheduler": "Nothing starts protection runs.",
    "beyond_loopback": "The protection service can be reached from other computers. Only you should hold its password.",
    "down": "The protection service is not answering. Nothing is being protected meanwhile.",
    "report_failed": "The protection service could not tell Seraphina about its last run.",
    "outside": "A run with the limits off was started outside Seraphina.",
    "health_unknown": "{disk}'s health can't be read.",
    "old": "Protected up to {age}.",
    "check_overdue": "Part of the disks has not been checked for hidden damage for {days}.",
    "never_protected": "Not protecting anything yet.",
    "ok": "Protected up to {when}.",
    "stale": "Last checked {age}.",
    "never_checked": "Not checked yet.",
}
NO_LINK = "To see more and to ask for runs, set up Seraphina's link."
NO_READER_SCHEDULERS = "Seraphina cannot see what else might start protection runs."
LIMITS_LINE = ("The protection service stops a run when {deletes} or more files were deleted, or {updates} or more "
               "changed, across all disks.")
LIMIT_LINES = {"deletes": "The protection service stops a run when {n} or more files were deleted, across all disks.",
               "updates": "The protection service stops a run when {n} or more files changed, across all disks.",
               "no_deletes": "It does not stop for deleted files (no limit is set).",
               "no_updates": "It does not stop for changed files (no limit is set).",
               "none": "The protection service does not stop for deleted or changed files (no limit is set)."}
ACCEPT_LINE = ("This protects whatever is on the disks when it runs, which may be more than you looked at. After it, "
               "the record of what was accepted is only Seraphina's counts and the protection service's own log.")
UNDELETE_LINES = ("Missing files only: nothing that exists is replaced.",
                  "They come back readable only by an administrator.",
                  "While it runs, the names you chose are visible to other programs on this server.")
STOP_LINE = "Stopping safely: the protection service saves what was done. Don't turn the server off for a minute."
SKIP_LINES = ("Only that one run is skipped.", "A restart of the protection service undoes this.")
REFUSALS = {
    "engine_too_old": "The protection service runs SnapRAID {version}, which it cannot drive. Nothing was sent.",
    "auth_off": "The protection service accepts commands without a password. Seraphina will not use it until it "
                "has one.",
    "two_schedulers": "Two things start protection runs. Turn one off first.",
    "schedulers_unknown": NO_READER_SCHEDULERS,
    "not_allowed": "Seraphina may only read the protection service. Allow more in the link's settings (setup page).",
    "changed": "Something changed since you looked. Look again.",
    "health": "See to the disk first: the protection service would cancel this run.",
    "no_link": NO_LINK,
    "nothing_running": "Nothing is running now.",
    "not_frozen": "The protection service is not waiting for you.",
    "no_review": "Open the list of changes first.",
    "no_damage": "No hidden damage is recorded.",
    "no_window": "No protection run is scheduled, so there is nothing to skip.",
    "mount_unknown": "Seraphina cannot confirm that disk is mounted: the disk protection reader must check it first.",
    "expired": "This go-ahead is too old. Look again.",
    "used": "This go-ahead was already used. Look again.",
    "mismatch": "What you approved is not what Seraphina showed. Look again.",
    "one_disk": "Choose files from one disk at a time: the protection service would bring back every chosen name on "
                "every chosen disk.",
    "typed": "Type the number of changes to confirm.",
    "rebuild": "A disk is being replaced: finish its checklist first (Backups, Replace a failed disk).",
}
WORDS_OF = {"sync": "protecting the latest changes", "scrub": "checking for hidden damage", "fix": "bringing files back",
            "up": "starting the disks", "report": "writing its report", "probe": "checking the disks' power",
            "diff": "looking for changes", "smart": "reading the disks' health", "read": "reading its file list",
            "start": "starting a run", "down": "letting the disks sleep"}
HIGH_WORDS = {"maintenance": "the protection run", "heal": "the repair of hidden damage",
              "undelete": "bringing files back", "startup": "its start-up check", "suspend_idle": "its idle check",
              "refresh": "a check of its state"}
# The tracker items of brief sections 2 and 13 (titles and details: novice words, U7)
SCHEDULERS_FINDING = ("Two things start protection runs",
                      "Seraphina found {count} things that start protection runs. Only one should: two at once can "
                      "fail each other or damage the protection. Turn off all but one (see Backups, What starts "
                      "protection runs), or mark a place that does not start runs.")
NO_SCHEDULER_FINDING = ("Nothing starts protection runs",
                        "Seraphina found nothing that starts protection runs, so nothing new is being protected. Set "
                        "the protection service's nightly time, or keep the one job that runs it.")
LOGIN_FINDING = ("The protection service refused Seraphina's link",
                 "The protection service turned down the password Seraphina's link holds, so Seraphina sees only the "
                 "counts it shows to anyone. Its password was probably changed: give the link the new one (setup "
                 "page). Seraphina tries again every {minutes} minutes meanwhile.")
VERDICT_WORDS = {"done": "Done.", "warning": "Done, with a warning.", "frozen": "Stopped for you to look at the changes.",
                 "failed": "It did not finish.", "stopped": "Stopped safely.", "did_nothing":
                 "It was cancelled because a disk is not healthy. It did nothing.", "unknown":
                 "Seraphina could not tell how it ended.", "running": "Running.", "partial":
                 "Stopped part way: some changes are not protected yet."}


class MetricsError(Exception):
    """A /metrics read with no usable answer: kind "down" (no connection, no answer, not 200) or "malformed"."""

    def __init__(self, kind: str, words: str) -> None:
        super().__init__(words)
        self.kind = kind
        self.words = words


class LinkError(Exception):
    """A link call with no answer, or the link's own refusal: code, words, sent (False when the request never
    reached the link), answer (the refusal's fields)."""

    def __init__(self, code: str, words: str, *, sent: bool = True, answer: dict | None = None) -> None:
        super().__init__(words)
        self.code = code
        self.words = words
        self.sent = sent
        self.answer = answer or {}


class Changed(Exception):
    """An approval refused because a bound fact no longer holds (409 {error: changed})."""


# ---------------------------------------------------------------- settings


def settings(cfg: dict) -> dict:
    """snapraid.daemon merged over DEFAULTS."""
    out = dict(DEFAULTS)
    section = cfg.get("daemon") if isinstance(cfg.get("daemon"), dict) else {}
    out.update({key: value for key, value in section.items() if value is not None})
    return out


def limits_line(limits: dict | None) -> str | None:
    """The limits in words: a limit of 0 (or none) is off in the daemon (its threshold option is passed only when
    not 0, scheduler.c:96-104), so it reads as no limit, never as "any"."""
    if not isinstance(limits, dict):
        return None
    deletes, updates = int(limits.get("deletes") or 0), int(limits.get("updates") or 0)
    if deletes and updates:
        return LIMITS_LINE.format(deletes=f"{deletes:,}", updates=f"{updates:,}")
    if deletes:
        return LIMIT_LINES["deletes"].format(n=f"{deletes:,}") + " " + LIMIT_LINES["no_updates"]
    if updates:
        return LIMIT_LINES["updates"].format(n=f"{updates:,}") + " " + LIMIT_LINES["no_deletes"]
    return LIMIT_LINES["none"]


# ---------------------------------------------------------------- /metrics


def parse_metrics(text: str) -> dict:
    """The Prometheus text of /metrics as typed facts. Only the names in METRIC_NAMES are read; every other one is
    ignored. A line that is not a comment, not blank and not a sample is MetricsError("malformed")."""
    out: dict[str, Any] = {"pulse": {}, "diff": {}, "disks": {}, "last": {}, "last_exit": {}, "active": None,
                           "build": {}, "thresholds": {}}
    active: dict[str, Any] = {}
    for number, raw in enumerate(text.split("\n"), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = METRIC_RE.fullmatch(line)
        if not match:
            raise MetricsError("malformed", f"/metrics line {number} could not be read")
        name, labels_text, value_text = match.groups()
        if name not in METRIC_NAMES:
            continue
        labels = {}
        rest = labels_text or ""
        while rest.strip():
            label = LABEL_RE.match(rest)
            if not label:
                raise MetricsError("malformed", f"/metrics line {number} has labels that could not be read")
            labels[label.group(1)] = label.group(2)
            rest = rest[label.end():]
        try:
            value = float(value_text)
        except ValueError:
            continue
        whole = int(value) if value == value and abs(value) != float("inf") else None
        if name == "snapraid_array_health":
            out["health"] = HEALTH_BY_NUMBER.get(whole, "pending")
        elif name == "snapraid_build_info":
            out["build"] = {"daemon": labels.get("daemon_version", ""), "engine": labels.get("engine_version", "")}
        elif name == "snapraid_disk_health":
            out["disks"][labels.get("disk", "?")] = {"role": labels.get("role"),
                                                     "health": HEALTH_BY_NUMBER.get(whole, "pending")}
        elif name == "snapraid_last_command_timestamp_seconds":
            out["last"][labels.get("command", "?")] = value
        elif name == "snapraid_diff_files":
            out["diff"][labels.get("change", "?")] = whole
        elif name == "snapraid_task_last_exit_code":
            out["last_exit"][labels.get("command", "?")] = whole
        elif name == "snapraid_exporter_last_pulse":
            out["pulse"][labels.get("subsystem", "?")] = whole
        elif name == "snapraid_task_active":
            active.update(command=labels.get("command"), high_command=labels.get("high_command"))
        elif name.startswith("snapraid_task_active_"):
            active[name[len("snapraid_task_active_"):]] = whole
        elif name == "snapraid_config_sync_threshold_deletes":
            out["thresholds"]["deletes"] = whole
        elif name == "snapraid_config_sync_threshold_updates":
            out["thresholds"]["updates"] = whole
        elif name == "snapraid_config_hold_off":
            out["hold_off"] = whole == 1
        elif name == "snapraid_daemon_uptime_seconds":
            out["uptime"] = value
        else:
            key = {"snapraid_array_block_size_bytes": "block_size", "snapraid_array_data_disks_count": "data_disks",
                   "snapraid_array_parity_disks_count": "parity_disks", "snapraid_array_files": "files",
                   "snapraid_array_blocks": "blocks", "snapraid_blocks_bad": "blocks_bad",
                   "snapraid_blocks_unsynced": "blocks_unsynced", "snapraid_blocks_unscrubbed": "blocks_unscrubbed",
                   "snapraid_config_scrub_percentage": "scrub_percentage",
                   "snapraid_config_scrub_older_than_days": "scrub_older_than",
                   "snapraid_task_queue_depth": "queue_depth"}[name]
            out[key] = whole
    if active.get("command"):
        out["active"] = {"command": active["command"], "high_command": active.get("high_command"),
                         "progress": active.get("progress_percent"), "eta": active.get("eta_seconds"),
                         "elapsed": active.get("elapsed_seconds")}
    return out


def fetch(url: str, timeout: float = 15.0) -> tuple[int, bytes]:
    """GET url (plain HTTP on loopback): (status, body). OSError or HTTPException with no answer."""
    match = re.fullmatch(r"http://(127\.0\.0\.1|\[::1\]):([0-9]{1,5})(/metrics)", url)
    if not match:
        raise ValueError("the metrics address must be http://127.0.0.1:<port>/metrics or http://[::1]:<port>/metrics")
    host = "::1" if match.group(1).startswith("[") else "127.0.0.1"
    conn = http.client.HTTPConnection(host, int(match.group(2)), timeout=2.0)
    try:
        conn.connect()
        conn.sock.settimeout(timeout)
        conn.request("GET", match.group(3), headers={"Accept": "text/plain"})
        response = conn.getresponse()
        return response.status, response.read(1 << 20)
    finally:
        conn.close()


class MetricsReader:
    """/metrics, through an injectable fetch(url) -> (status, body)."""

    def __init__(self, url: str = METRICS_URL, fetcher: Callable[[str], tuple[int, bytes]] | None = None) -> None:
        self.url = url
        self.fetcher = fetcher or fetch

    def read(self) -> dict:
        outcomes.retried("SnapRAID daemon reads")
        try:
            status, body = self.fetcher(self.url)
        except (OSError, http.client.HTTPException) as exc:
            raise MetricsError("down", f"the protection service did not answer ({type(exc).__name__})") from None
        if status != 200:
            raise MetricsError("down", f"the protection service answered {status}")
        return parse_metrics(body.decode("utf-8", "replace"))


# ---------------------------------------------------------------- the link


def unix_connector(path: str) -> Callable[[bytes, float], bytes]:
    """One request line over the link's Unix socket, the answer line back. LinkError(no_link, sent False) when the
    socket cannot be reached; LinkError(no_answer) when the request was written and no answer line came."""
    def call(line: bytes, timeout: float) -> bytes:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            try:
                sock.connect(path)
            except OSError as exc:
                raise LinkError("no_link", f"Seraphina's link cannot be reached ({type(exc).__name__}).",
                                sent=False) from None
            try:
                sock.sendall(line)
                sock.shutdown(socket.SHUT_WR)
                chunks, size = [], 0
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > 64 << 20:
                        break
                return b"".join(chunks)
            except OSError as exc:
                raise LinkError("no_answer", f"Seraphina's link did not answer ({type(exc).__name__}).") from None
        finally:
            sock.close()
    return call


class LinkClient:
    """The link's protocol: {"v": 1, "op": ..., fields} in, {"ok": true, ...} out; the link's refusal is
    LinkError(code, words, answer=...)."""

    def __init__(self, path: str = LINK_SOCKET, connector: Callable[[bytes, float], bytes] | None = None) -> None:
        self.path = path
        self.connector = connector or unix_connector(path)

    def call(self, op: str, timeout: float = 60.0, **fields: Any) -> dict:
        line = json.dumps({"v": 1, "op": op, **fields}, separators=(",", ":")).encode("utf-8") + b"\n"
        out = self.connector(line, timeout)
        text = (out or b"").decode("utf-8", "replace").strip()
        try:
            answer = json.loads(text.splitlines()[-1]) if text else None
        except ValueError:
            answer = None
        if not isinstance(answer, dict):
            raise LinkError("no_answer", "Seraphina's link gave no answer that could be read.")
        if answer.get("ok") is not True:
            raise LinkError(str(answer.get("error") or "refused"), str(answer.get("words") or "Refused."),
                            answer=answer)
        return answer


# ---------------------------------------------------------------- times


def daemon_time(stamp: Any) -> float | None:
    """A daemon time ("2026-10-25T15:00:57", its own wall clock, no zone) as seconds on that same clock; compare
    only with other daemon times."""
    if not isinstance(stamp, str):
        return None
    with contextlib.suppress(ValueError):
        return datetime.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S").replace().timestamp()
    return None


def next_window(schedule: Any, now: float) -> float | None:
    """The next start of the daemon's maintenance_schedule ("02:00", "Mon 03:00, Thu 04:00"), in this machine's local
    time, after now; None when it is empty or cannot be read."""
    if not isinstance(schedule, str) or not schedule.strip():
        return None
    days = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
    entries = []
    for part in schedule.split(","):
        match = re.fullmatch(r"\s*(?:([A-Za-z]{3})\s+)?([0-9]{1,2}):([0-9]{2})\s*", part)
        if not match or (match.group(1) and match.group(1).lower() not in days):
            return None
        entries.append((days.get((match.group(1) or "").lower()), int(match.group(2)), int(match.group(3))))
    local = datetime.fromtimestamp(now)
    best = None
    for offset in range(0, 8):
        day = local + timedelta(days=offset)
        for weekday, hour, minute in entries:
            if weekday is not None and day.weekday() != weekday:
                continue
            when = day.replace(hour=hour, minute=minute, second=0, microsecond=0).timestamp()
            if when > now and (best is None or when < best):
                best = when
    return best


def window_words(at: float | None, now: float) -> str | None:
    if at is None:
        return None
    local, today = datetime.fromtimestamp(at), datetime.fromtimestamp(now).date()
    clock = local.strftime("%H:%M")
    if local.date() == today:
        return f"tonight at {clock}" if local.hour >= 17 else f"today at {clock}"
    if local.date() == today + timedelta(days=1):
        return f"tomorrow at {clock}" if local.hour >= 5 else f"tonight at {clock}"
    return f"{local.strftime('%A')} at {clock}"


# ---------------------------------------------------------------- runs and their verdicts (sections 3.5, 6.5)


def runs_of(tasks: dict) -> list[dict]:
    """The daemon's tasks grouped into runs: one run per (high_command, scheduled_at). Newest first (by the highest
    number in each run). Each {high_command, scheduled_at, steps (by number), numbers}."""
    groups: dict[tuple, list[dict]] = {}
    for key in ("pending", "active", "history"):
        for task in tasks.get(key) or []:
            if isinstance(task, dict) and isinstance(task.get("number"), int):
                groups.setdefault((task.get("high_command"), task.get("scheduled_at")), []).append(task)
    out = []
    for (high, scheduled), steps in groups.items():
        current: list[dict] = []
        for task in sorted(steps, key=lambda t: t["number"]):
            current.append(task)
            if task.get("command") == "report":      # a run ends with its one report: two runs queued in the
                out.append(current)                  # same second never merge
                current = []
        if current:
            out.append(current)
    runs = [{"high_command": run[0].get("high_command"), "scheduled_at": run[0].get("scheduled_at"), "steps": run,
             "numbers": [t["number"] for t in run]} for run in out]
    return sorted(runs, key=lambda run: -max(run["numbers"]))


def run_ended(run: dict) -> bool:
    """A run has ended when its report is terminated, or every step has ended (section 6.4 rule 3)."""
    steps = run["steps"]
    report = [t for t in steps if t.get("command") == "report"]
    if report and report[-1].get("status") == "terminated":
        return True
    return bool(steps) and all(t.get("status") in ENDED for t in steps)


def verdict(run: dict, stopped: Any = ()) -> dict:
    """{verdict, step?, message?}: the worst step decides (section 6.5). running; unknown (a step signaled 9: the
    daemon restarted under it); frozen (sync exit 2); did_nothing (the health gate cancelled it); stopped (a
    person's stop: a step Seraphina's own stop reached, whose (number, scheduled_at) is in stopped, ending
    terminated 1 or signaled 15; or a later step's "... was canceled"); failed; warning (exit 3); done. Decided
    from statuses, exit codes and the daemon's fixed cancel messages only, never from engine text (section 3.5
    rule 11: a sync exit 1 alone is ambiguous)."""
    marked = {tuple(key) for key in stopped or ()}
    steps = [{**t, "stopped": True} if (t.get("number"), t.get("scheduled_at")) in marked else t
             for t in run["steps"] if t.get("command") != "report"]
    if not run_ended(run) and any(t.get("status") in RUNNING or t.get("status") == "queued" for t in steps):
        return {"verdict": "running"}
    for task in steps:
        if task.get("status") == "signaled" and task.get("exit_sig") == 9:
            return {"verdict": "unknown", "step": task.get("command")}
    for task in steps:
        if task.get("command") == "sync" and task.get("status") == "terminated" and task.get("exit_code") == 2:
            return {"verdict": "frozen", "step": "sync"}
    for task in steps:
        message = str(task.get("exit_msg") or "")
        if task.get("status") == "canceled" and message.startswith("Array is "):
            return {"verdict": "did_nothing", "step": task.get("command"), "message": message}
    canceled = any(str(t.get("exit_msg") or "").endswith("was canceled") for t in steps)
    for task in steps:
        status, code = task.get("status"), task.get("exit_code")
        if status == "signaled" or (status == "terminated" and code not in (0, 3)):
            reached = task.get("stopped") and (code == 1 or task.get("exit_sig") == 15)
            if reached or canceled or (status == "signaled" and task.get("exit_sig") == 15):
                return {"verdict": "stopped", "step": task.get("command")}
            return {"verdict": "failed", "step": task.get("command"), "code": code}
        if status == "canceled" and not str(task.get("exit_msg") or "").startswith("The preceding"):
            return {"verdict": "failed", "step": task.get("command"), "message": task.get("exit_msg")}
    if canceled:
        return {"verdict": "stopped"}
    if any(t.get("exit_code") == 3 for t in steps):
        return {"verdict": "warning"}
    return {"verdict": "done"}


def match_run(tasks: dict, action: str, before: dict) -> dict | None:
    """The run an action's 2xx queued (section 6.4 rule 2): high_command equal to the action (accept is a
    maintenance), every number above the highest recorded just before the POST, one shared scheduled_at within
    MATCH_SECONDS of the daemon's clock then. None when no such run is listed."""
    high = "maintenance" if action == "accept" else action
    highest = int(before.get("highest") or 0)
    sent_at = daemon_time(before.get("daemon_now"))
    for run in runs_of(tasks):
        if run["high_command"] != high or min(run["numbers"]) <= highest:
            continue
        scheduled = daemon_time(run["scheduled_at"])
        if sent_at is not None and scheduled is not None and abs(scheduled - sent_at) > MATCH_SECONDS:
            continue
        return run
    return None


def schedulers(info: dict | None, config: dict | None, not_schedulers: list) -> dict:
    """The count of section 1.4: every place the reader's inventory found that mentions SnapRAID (the daemon's own
    unit never counts; a place the owner marked "does not start runs" counts again once its contents' sha256
    changes), plus the daemon's window when maintenance_schedule is set. {count (None without the reader),
    places, window}."""
    window = (config or {}).get("maintenance_schedule") or None
    if info is None:
        return {"count": None, "places": [], "window": window}
    marked = {(entry.get("place"), entry.get("sha256")) for entry in not_schedulers or [] if isinstance(entry, dict)}
    places = []
    for entry in ((info.get("schedulers") or {}).get("found") or []):
        place = str(entry.get("place") or "")
        if os.path.basename(place) in DAEMON_UNITS or "seraphina-snapraid" in place:
            continue
        counted = (place, entry.get("sha256")) not in marked
        places.append({"place": place, "kind": entry.get("kind"), "schedule": entry.get("schedule") or [],
                       "sha256": entry.get("sha256"), "counted": counted})
    count = sum(1 for p in places if p["counted"]) + (1 if window else 0)
    return {"count": count, "places": places, "window": window}


def listening_beyond_loopback(port: int, root: str = "/") -> bool | None:
    """Whether something listens on port on an address other than loopback, from /proc/net/tcp and tcp6 (readable by
    any user). None when neither can be read."""
    found = None
    for name, width in (("tcp", 8), ("tcp6", 32)):
        try:
            text = Path(root, "proc/net", name).read_text(encoding="ascii", errors="replace")
        except OSError:
            continue
        found = bool(found)
        for line in text.split("\n")[1:]:
            fields = line.split()
            if len(fields) < 4 or fields[3] != "0A":      # LISTEN
                continue
            address, _, hexport = fields[1].partition(":")
            if int(hexport or "0", 16) != port:
                continue
            if width == 8:
                loopback = address.upper().endswith("7F")      # 127.x.x.x in little-endian hex
            else:
                loopback = address.upper() == "00000000000000000000000001000000" or \
                    address.upper().endswith("0100007F") and address.upper().startswith("0000000000000000FFFF0000")
            if not loopback:
                found = True
    return found


# ---------------------------------------------------------------- the route


class DaemonRoute:
    """The daemon route of one SnapraidPanel (panel: settings, facts, saving, the journal, the tracker, notices,
    events and the clock). metrics: a MetricsReader; link: a LinkClient (tests give ones with fakes)."""

    def __init__(self, panel: Any, metrics: MetricsReader | None = None, link: LinkClient | None = None,
                 proc_root: str = "/") -> None:
        self.panel = panel
        self._metrics = metrics
        self._link = link
        self.proc_root = proc_root
        self._lock = threading.RLock()
        self._probe: tuple[float, dict] | None = None
        self._paths: dict[str, list[dict]] = {}      # an undelete approval's chosen paths: memory only (§11.3)
        self._reviewed: dict | None = None            # the scan whose list the owner opened, and when

    # -- plumbing ---------------------------------------------------------------------------------------------

    def cfg(self) -> dict:
        return settings(self.panel.settings())

    def metrics_reader(self) -> MetricsReader:
        return self._metrics or MetricsReader(str(self.cfg()["metrics_url"]))

    def link(self) -> LinkClient | None:
        if self._link is not None:
            return self._link
        path = str(self.cfg()["link"])
        return LinkClient(path) if os.path.exists(path) else None

    def facts(self) -> dict:
        return self.panel.facts().get("daemon") or {}

    def save(self, daemon: dict) -> None:
        facts = self.panel.facts()
        facts["daemon"] = daemon
        self.panel._save(facts)

    def now(self) -> float:
        return self.panel.clock()

    # -- detection (section 10.1) --------------------------------------------------------------------------

    def probe(self) -> dict:
        """Whether a daemon answers /metrics, at most once a minute; nothing is stored while the panel is off."""
        mono = time.monotonic()
        if self._probe is not None and mono - self._probe[0] < 60:
            return self._probe[1]
        try:
            found = self.metrics_reader().read()
            result = {"answering": True, "engine": (found.get("build") or {}).get("engine"),
                      "daemon": (found.get("build") or {}).get("daemon")}
        except (MetricsError, ValueError):
            result = {"answering": False}
        self._probe = (mono, result)
        return result

    def installed(self) -> bool:
        """The daemon route applies: a daemon answered /metrics once (it is recorded), or answers now."""
        return bool(self.facts().get("seen")) or self.probe().get("answering", False)

    # -- the poll (sections 3.3, 3.4) ------------------------------------------------------------------------

    def tick(self) -> list[str]:
        """What is due now: /metrics, then the link reads its counters call for. Returns what ran."""
        with self._lock:
            cfg = self.cfg()
            d = self.facts()
            now = self.now()
            interval = float(cfg["active_seconds"] if (d.get("metrics") or {}).get("active") or self._own_active(d)
                             else cfg["metrics_seconds"])
            last = d.get("metrics_tried")
            if last is not None and now - float(last) < max(MIN_METRICS_SECONDS, interval):
                return []
            ran = ["metrics"]
            d["metrics_tried"] = now
            try:
                metrics = self.metrics_reader().read()
            except (MetricsError, ValueError) as exc:
                self._down(d, getattr(exc, "words", str(exc)), now)
                self.save(d)
                return ran
            self._answering(d, metrics, now)
            ran += self._link_reads(d, metrics, now, cfg)
            if "restart_candidate" in d and not d.get("tasks_read_at_restart"):
                self._confirm_restart(d, metrics, now)      # no task list to confirm by: the config counter decides
            d.pop("tasks_read_at_restart", None)
            self._skips_used(now)
            self._classify(d, now)
            self._schedulers_item(d)
            self.save(d)
            self._due_looks(now)
            return ran

    def _schedulers_item(self, d: dict) -> None:
        """Brief section 2 and design sections 1.4 and 7.4: two or more schedulers, or none, open the finding
        snapraid:schedulers (counts only, never which places), updated when the count changes; exactly one closes
        it. An unknown count (no reader) changes nothing."""
        count = self.scheduler_count(d.get("config")).get("count")
        if count is None or count == d.get("schedulers_seen"):
            return
        d["schedulers_seen"] = count
        if count == 1:
            self._close("snapraid:schedulers", "one thing now starts protection runs")
            return
        title, detail = SCHEDULERS_FINDING if count > 1 else NO_SCHEDULER_FINDING
        self._finding("snapraid:schedulers", title, detail.format(count=count))
        self.panel._journal("daemon", "schedulers of protection runs: not one", count=count)

    def _skips_used(self, now: float) -> None:
        """An approved skip whose window has come is used up: a later restart no longer undoes it."""
        for record in self.approvals():
            if record["action"] == "hold_off" and record.get("outcome") == outcomes.SUCCEEDED and \
                    record.get("window_at_preview") and now >= float(record["window_at_preview"]) and \
                    not record.get("window_passed"):
                record["window_passed"] = now
                self._write(record)

    def _own_active(self, d: dict) -> bool:
        """A run Seraphina asked for may be queued or running: a run request the daemon took (2xx, or seen
        afterwards), sent within the last OWN_ACTIVE_SECONDS, whose run has not ended. Stops, skips, refusals and
        unknown outcomes never count, so they never hold the poller at the active rate (section 3.3)."""
        now = self.now()
        return any(record.get("state") == "sent" and record.get("action") in STARTS_RUNS and
                   record.get("outcome") == outcomes.SUCCEEDED and not (record.get("run") or {}).get("ended") and
                   now - float(record.get("sent_at") or 0) <= OWN_ACTIVE_SECONDS for record in self.approvals())

    def _own_run(self, run: dict, records: list[dict] | None = None) -> dict | None:
        """The approval whose run this is: the same scheduled_at and a shared task number. The daemon gives numbers
        out again after a restart (section 3.5), so a number alone never matches."""
        for record in records if records is not None else self.approvals():
            mine = record.get("run") or {}
            if mine.get("numbers") and mine.get("scheduled_at") == run.get("scheduled_at") and \
                    set(mine["numbers"]) & set(run["numbers"]):
                return record
        return None

    def _stopped(self) -> list[tuple]:
        """(number, scheduled_at) of every step a stop Seraphina sent reached: the answer named the very step the
        owner was shown. verdict() reads such a step's exit 1 (or signal 15) as stopped, not failed."""
        out = []
        for record in self.approvals():
            target = record.get("target") or {}
            if record.get("action") == "stop" and record.get("state") == "sent" and \
                    record.get("outcome") == outcomes.SUCCEEDED and target.get("number") is not None and \
                    record.get("stopped_number", target["number"]) == target["number"]:
                out.append((target["number"], target.get("scheduled_at")))
        return out

    def _down(self, d: dict, words: str, now: float) -> None:
        d["error"] = {"at": now, "words": words}
        d.setdefault("down_since", now)
        rebuild = getattr(self.panel, "rebuild", None)
        if rebuild is not None and rebuild.expects_down():
            return                     # the owner turned it off for a disk rebuild (design section 9.4): no alarm
        if d.get("seen") and now - float(d["down_since"]) >= float(self.cfg()["down_minutes"]) * 60 \
                and not d.get("down_notified"):
            d["down_notified"] = True
            self._notice("parity.down", {"count": int((now - float(d["down_since"])) // 60)})
            self._finding("snapraid:service", "The protection service is not answering",
                          "Seraphina's reads of the protection service get no answer. Nothing is being protected "
                          "meanwhile. Check that its service runs.")
            self.panel._journal("daemon", "the protection service is not answering")
            self._publish_daemon(answering=False)

    def _answering(self, d: dict, metrics: dict, now: float) -> None:
        was_down = "down_since" in d
        if was_down:
            d.pop("down_since", None)
            d.pop("error", None)
            if d.pop("down_notified", None):
                self._clear("parity.down")
                self._close("snapraid:service", "the protection service answers again")
        d["seen"] = True
        start = now - float(metrics.get("uptime") or 0)
        previous = d.get("start")
        if previous is not None and abs(start - float(previous)) > RESTART_SLACK:
            config_back = (metrics.get("pulse") or {}).get("config", 0) < \
                ((d.get("metrics") or {}).get("pulse") or {}).get("config", 0)
            d["restart_candidate"] = {"start": start, "seen_at": now, "config_back": config_back}
        d["start"] = start if previous is None or "restart_candidate" not in d else previous
        d["metrics"] = {**metrics, "at": now}
        self._protected(d, metrics, now)
        if was_down:
            self._publish_daemon(answering=True)

    def _protected(self, d: dict, metrics: dict, now: float) -> None:
        """"Protected up to" moves only with blocks_unsynced 0 after a sync that did not freeze (section 3.5 rules 4
        and 5); a freeze is the last sync's exit 2, its counts and the limits."""
        last_sync = (metrics.get("last") or {}).get("sync")
        exit_sync = (metrics.get("last_exit") or {}).get("sync")
        if last_sync and exit_sync in (0, 3, None) and metrics.get("blocks_unsynced") == 0:
            d["protected_up_to"] = max(float(last_sync), float(d.get("protected_up_to") or 0))
        if exit_sync == 2:
            diff = metrics.get("diff") or {}
            limits = metrics.get("thresholds") or {}
            freeze = d.get("freeze") or {"since": now}
            freeze.update(removed=int(diff.get("removed") or 0), updated=int(diff.get("updated") or 0),
                          added=int(diff.get("added") or 0), limits=dict(limits),
                          which=[name for name, change in (("deletes", "removed"), ("updates", "updated"))
                                 if limits.get(name) and int(diff.get(change) or 0) >= int(limits[name])])
            d["freeze"] = freeze
        elif exit_sync is not None and d.get("freeze") and exit_sync != 2:
            d["freeze_ended"] = {"at": now, **d.pop("freeze")}

    def _link_reads(self, d: dict, metrics: dict, now: float, cfg: dict) -> list[str]:
        """The link reads the moved counters call for (section 3.3), the full read every full_read_minutes, and the
        link's info with it."""
        link = self.link()
        if link is None:
            d["link"] = {"installed": False}
            return []
        pulse = metrics.get("pulse") or {}
        read = d.setdefault("read_pulse", {})
        stamps = d.setdefault("read_at", {})
        full = now - float(d.get("full_at") or 0) >= float(cfg["full_read_minutes"]) * 60
        own = self._own_active(d)
        what = []
        if full or "restart_candidate" in d:
            what = ["state", "tasks", "activity", "array", "disks", "config"]
        else:
            if pulse.get("tasks") != read.get("tasks"):
                gap = float(cfg["active_seconds"] if own else cfg["reads_min_seconds"])
                if now - float(stamps.get("tasks") or 0) >= gap:
                    what.append("tasks")
            if pulse.get("activity") != read.get("activity") and own:
                what.append("activity")
            if pulse.get("array") != read.get("array"):
                what.append("array")
            if pulse.get("disks") != read.get("disks") and now - float(stamps.get("disks") or 0) >= DISKS_MIN_SECONDS:
                what.append("disks")
            if pulse.get("config") != read.get("config"):
                what.append("config")
        refused = d.get("login_refused")
        if refused and now - float(refused.get("at") or 0) < float(cfg["full_read_minutes"]) * 60:
            return []                     # a refused password: no calls until the next full read (brief section 13)
        ran = []
        if full or not d.get("info"):
            try:
                d["info"] = self._info(link.call("info"), now)
                ran.append("info")
            except LinkError as exc:
                d["info_error"] = {"at": now, "code": exc.code, "words": exc.words}
                d["link"] = {"installed": True, "error": exc.words}
                return ran
        if not what:
            return ran
        try:
            answer = link.call("read", what=what)
        except LinkError as exc:
            d["link"] = {"installed": True, "error": exc.words, "code": exc.code}
            if exc.code == "login_refused":
                self._login_refused(d, now, cfg)
            return ran
        d["link"] = {"installed": True}
        if d.pop("login_refused", None):
            self._close("snapraid:service", "the protection service accepts the link's password again",
                        title=LOGIN_FINDING[0])
        d.pop("info_error", None)
        for key in what:
            if key in answer:
                d[key] = answer[key]
                stamps[key] = now
        d["state"] = answer.get("state")
        d["daemon_now"] = (answer.get("pulse") or {}).get("current_at")
        subsystem = {"tasks": "tasks", "activity": "activity", "array": "array", "disks": "disks",
                     "config": "config"}
        for key in what:
            if key in subsystem:
                read[subsystem[key]] = pulse.get(subsystem[key])
        if full:
            d["full_at"] = now
        if "tasks" in what:
            d["tasks_read_at_restart"] = "restart_candidate" in d
            self._confirm_restart(d, metrics, now)
        ran.append("read:" + ",".join(what))
        return ran

    def _login_refused(self, d: dict, now: float, cfg: dict) -> None:
        """The daemon answered 401 to the link's password: the finding snapraid:service once, and the link's calls
        wait for the next full read, so a password it refuses is not sent again every minute."""
        first = "login_refused" not in d
        d["login_refused"] = {"at": now, "since": (d.get("login_refused") or {}).get("since", now)}
        if first:
            self._finding("snapraid:service", LOGIN_FINDING[0],
                          LOGIN_FINDING[1].format(minutes=int(float(cfg["full_read_minutes"]))))
            self.panel._journal("daemon", "the protection service refused the link's password")

    def _info(self, info: dict, now: float) -> dict:
        return {"at": now, "auth_on": info.get("auth_on"), "engine": info.get("engine_version"),
                "daemon": info.get("daemon_version"), "allow": (info.get("link") or {}).get("allow") or ["read"],
                "link_sha256": (info.get("link") or {}).get("sha256"), "hook": (info.get("link") or {}).get("hook")}

    def _confirm_restart(self, d: dict, metrics: dict, now: float) -> None:
        """A restart candidate is a restart when the config counter went back or the task list shows a start-up
        check scheduled at the new start; a clock step alone only re-bases the start (section 3.4)."""
        candidate = d.pop("restart_candidate", None)
        if candidate is None:
            return
        daemon_now = daemon_time(d.get("daemon_now"))
        started = daemon_now - float(metrics.get("uptime") or 0) if daemon_now is not None else None
        startup = any(task.get("high_command") == "startup" and started is not None and
                      (daemon_time(task.get("scheduled_at")) or 0) >= started - RESTART_SLACK
                      for key in ("pending", "active", "history") for task in (d.get("tasks") or {}).get(key) or [])
        d["start"] = candidate["start"]
        if not (startup or candidate.get("config_back")):
            return
        d["restarted_at"] = now
        self._after_restart(d, now, float(candidate["start"]))

    def _after_restart(self, d: dict, now: float, start: float) -> None:
        """Section 3.4: a run Seraphina asked for that the daemon took (or whose answer never came) and that had not
        ended becomes unknown; a skip the owner approved, when it is the latest skip answer and still unused, has
        lapsed (asked again, never set again by itself); unsent approvals are void. A recorded refusal, a send that
        did not take effect and an outcome already decided stay as they are (DESIGN §40), and nothing sent after
        the new start (start: the daemon's new start time, on the harness's clock) is touched."""
        self.panel._journal("daemon", "the protection service restarted")
        self._publish_daemon(answering=True, restarted=True)
        records = self.approvals()
        before = [r for r in records if r.get("state") == "sent" and
                  float(r.get("sent_at") or 0) <= start + RESTART_SLACK]
        for record in records:
            if record.get("state") == "preview":
                record["state"] = "void"
                self._write(record)
        skips = [r for r in before if r["action"] == "hold_off" and r.get("outcome") == outcomes.SUCCEEDED]
        latest = skips[-1] if skips else None
        if latest is not None and latest["sends"]["body"].get("enabled") and not latest.get("window_passed") and \
                not latest.get("lapsed"):
            latest["lapsed"] = now
            self._write(latest)
            d["lapsed"] = {"at": now, "approval": latest["id"]}
            self._notice("parity.lapsed", {})
            self._decision("snapraid:lapsed", "The skip of tonight's run was undone",
                           "The protection service restarted, so the skip you approved no longer holds. Set it "
                           "again in Backups if you still want the next run skipped.")
        for record in before:
            if record["action"] in STARTS_RUNS and record.get("outcome") in (outcomes.SUCCEEDED, None) and \
                    not (record.get("run") or {}).get("ended"):
                record["outcome"] = outcomes.UNKNOWN
                record["words"] = "The protection service restarted before the run ended: it may not have run."
                self._write(record)
                self._unknown_item(record)

    # -- classifying runs and reports (sections 6.5, 7.2) --------------------------------------------------------

    def _classify(self, d: dict, now: float) -> None:
        tasks = d.get("tasks")
        if not isinstance(tasks, dict):
            self._health(d)
            return
        self._follow(d, tasks)
        handled = d.setdefault("handled", [])
        reports = [t for t in tasks.get("history") or [] if t.get("command") == "report"]
        if not d.get("reports_baseline"):
            handled.extend(f"{t['number']}@{t.get('finished_at')}" for t in reports)
            d["reports_baseline"] = now
        for task in sorted(reports, key=lambda t: t["number"]):
            key = f"{task['number']}@{task.get('finished_at')}"
            if key in handled:
                continue
            handled.append(key)
            self._report(d, task, tasks, now)
        d["handled"] = handled[-HANDLED_KEEP:]
        self._health(d)

    def _report(self, d: dict, report: dict, tasks: dict, now: float) -> None:
        # a report that could not be delivered: terminated with -1 (the only way a report gets it, runner.c:577-587),
        # or the daemon's fixed fatal message, which the link passes on as notify_failed
        if report.get("notify_failed") or (report.get("status") == "terminated" and report.get("exit_code") == -1):
            d["report_failed"] = {"at": now, "number": report["number"]}
            self._finding("snapraid:service", "The protection service could not tell Seraphina about a run",
                          "Its report could not be delivered to Seraphina (the forwarder failed). Seraphina read the "
                          "run from the task list instead. Check the hook in Messages.")
        else:
            d.pop("report_failed", None)
        run = next((r for r in runs_of(tasks) if report["number"] in r["numbers"]), None)
        if run is None or run["high_command"] in ("report", "startup", "suspend_idle", "refresh"):
            return
        result = verdict(run, self._stopped())
        own = self._own_run(run)
        self.panel._journal("run", f"{HIGH_WORDS.get(run['high_command'], 'a run')}: {result['verdict']}",
                            high_command=run["high_command"], verdict=result["verdict"], numbers=run["numbers"],
                            approval=own["id"] if own else None)
        if self.panel.bus is not None:
            self.panel.bus.publish("snapraid.run", {"approval": own["id"] if own else None,
                                                    "high_command": run["high_command"], "verdict": result["verdict"]})
        metrics = d.get("metrics") or {}
        if result["verdict"] == "frozen":
            freeze = d.get("freeze") or {}
            per_disk = self._per_disk_counts()
            self._notice("parity.held", {"removed": freeze.get("removed", 0), "updated": freeze.get("updated", 0),
                                         "disks": len(per_disk) or 1})
            self._decision("snapraid:frozen", "Review the changes the protection service stopped for",
                           f"The protection service stopped its run before changing anything: "
                           f"{freeze.get('removed', 0):,} files deleted and {freeze.get('updated', 0):,} changed since "
                           f"the last run. See what changed in Backups, then choose a way out.", update=True)
        elif result["verdict"] in ("done", "warning") and run["high_command"] == "maintenance":
            if metrics.get("blocks_unsynced") == 0:
                was_frozen = d.get("freeze_ended")
                if was_frozen and not (own and own.get("action") == "accept") and \
                        was_frozen.get("limits") == (metrics.get("thresholds") or {}) and was_frozen.get("which"):
                    self._notice("parity.released_outside", {})
                    self._finding("snapraid:outside", "A run with the limits off was started outside Seraphina",
                                  "The protection service protected changes over its limits that you did not approve "
                                  "in the cockpit. Check who used its web page or its password.")
                    d["outside"] = {"at": now}
                d.pop("freeze_ended", None)
                self._notice("parity.ok", {"time": datetime.fromtimestamp(now).strftime("%H:%M")})
                self._close("snapraid:frozen", "a complete protection run")
                self._close("snapraid:failed", "a complete protection run")
        elif result["verdict"] in ("failed", "unknown"):
            self._notice("parity.failed", {})
            self._finding("snapraid:failed", "A protection run did not finish",
                          f"{HIGH_WORDS.get(run['high_command'], 'A run').capitalize()} did not finish "
                          f"(step: {WORDS_OF.get(result.get('step') or '', 'a step')}"
                          + (f"; {result['message']}" if result.get("message") else "") + ").")
        elif result["verdict"] in ("done", "warning") and run["high_command"] in ("heal", "undelete"):
            self._close("snapraid:failed", f"a complete {run['high_command']}")

    def _per_disk_counts(self) -> dict:
        """The frozen scan's counts per disk, from the link's review (names are read and dropped here)."""
        link = self.link()
        if link is None:
            return {}
        with contextlib.suppress(LinkError):
            return link.call("review", offset=0).get("per_disk") or {}
        return {}

    def _health(self, d: dict) -> None:
        """disk.failing per disk key and a snapraid:health:<key> finding for prefail or failing; cleared by a
        fresh healthy reading (a disk with no resolved device reads unknown, which clears nothing)."""
        metrics = d.get("metrics") or {}
        seen = d.setdefault("health_seen", {})
        devices = _devices(d)
        for name, key, label in disk_keys(metrics.get("disks") or {}):
            health = disk_health((metrics["disks"][name] or {}).get("health"), devices.get(name))
            dedupe = f"snapraid:health:{key}"
            if health in ("prefail", "failing"):
                if seen.get(name) not in ("prefail", "failing"):
                    self._notice("disk.failing", {}, f"disk-{key}")
                self._finding(dedupe, f"{label} says it is failing",
                              f"The protection service reports {label[:1].lower() + label[1:]} as {health}. Replace "
                              f"it soon, and check that your backups are current.")
            elif health == "passed" and seen.get(name) in ("prefail", "failing", "degraded"):
                self._clear("disk.failing", f"disk-{key}")
                self._close(dedupe, "a fresh healthy reading")
            seen[name] = health

    # -- approvals (section 6) ------------------------------------------------------------------------------

    def folder(self) -> Path:
        return self.panel.folder() / "actions"

    def approvals(self) -> list[dict]:
        folder = self.folder()
        out = []
        with contextlib.suppress(OSError):
            for path in folder.glob("A-*.json"):
                with contextlib.suppress(OSError, ValueError):
                    record = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(record, dict):
                        out.append(record)
        return sorted(out, key=lambda r: int(str(r.get("id", "A-0"))[2:] or 0))

    def approval(self, approval: str) -> dict:
        if not re.fullmatch(r"A-[0-9]{1,9}", approval or ""):
            raise KeyError(approval)
        path = self.folder() / f"{approval}.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise KeyError(f"no approval {approval}") from None

    def _write(self, record: dict) -> None:
        from .snapraid import _write_private

        _write_private(self.folder(), f"{record['id']}.json", json.dumps(record, indent=1, sort_keys=True))

    def _new_id(self, floor: int = 0) -> str:
        """The next approval id: above every record here and above floor (the highest id in the link's journal, which
        refuses an id it has sent before, so a fresh data folder never reuses one)."""
        numbers = [int(str(r.get("id"))[2:]) for r in self.approvals() if str(r.get("id", "")).startswith("A-")]
        return f"A-{max(numbers + [floor]) + 1}"

    def _link_floor(self) -> int:
        link = self.link()
        if link is None:
            return 0
        with contextlib.suppress(LinkError, ValueError, TypeError):
            lines = link.call("journal", limit=50).get("lines") or []
            return max([int(str(line.get("approval"))[2:]) for line in lines
                        if re.fullmatch(r"A-[0-9]{1,9}", str(line.get("approval")))] + [0])
        return 0

    def fresh(self) -> tuple[dict, dict]:
        """Fresh facts for a preview or a send: /metrics and every link read. (metrics, link answer)."""
        link = self.link()
        if link is None:
            raise ValueError(REFUSALS["no_link"])
        try:
            metrics = self.metrics_reader().read()
        except MetricsError as exc:
            raise ValueError("The protection service is not answering: " + exc.words + ".") from None
        try:
            answer = link.call("read", what=["state", "tasks", "array", "disks", "config"])
            info = link.call("info")
        except LinkError as exc:
            raise ValueError(exc.words) from None
        with self._lock:
            d = self.facts()
            now = self.now()
            d["info"] = self._info(info, now)
            for key in ("tasks", "array", "disks", "config"):
                d[key] = answer.get(key)
            d["state"] = answer.get("state")
            self.save(d)
        answer["metrics"] = metrics
        answer["info"] = d["info"]
        return metrics, answer

    def checks(self, action: str, facts: dict) -> str | None:
        """Section 2.4, in the harness: the refusal code, or None."""
        info = facts["info"]
        engine = info.get("engine") or (facts.get("array") or {}).get("engine_version") or ""
        if not re.match(r"^(1[5-9]|[2-9][0-9])\.", str(engine) + "."):
            return "engine_too_old"
        if info.get("auth_on") is not True:
            return "auth_off"
        allow = info.get("allow") or ["read"]
        if "act" not in allow or (action == "accept" and "accept" not in allow):
            return "not_allowed"
        if action in STARTS_RUNS and self._rebuilding():
            return "rebuild"            # design section 9.1: no run is started while a restore is open
        if action in STARTS_RUNS:
            count = self.scheduler_count(facts.get("config"))["count"]
            if count is None:
                return "schedulers_unknown"
            if count > 1:
                return "two_schedulers"
            if (facts.get("state") or {}).get("health") in BAD_HEALTH:
                return "health"
        return None

    def _rebuilding(self) -> bool:
        """The owner's checklist for a failed disk is open (design section 9.4)."""
        rebuild = getattr(self.panel, "rebuild", None)
        return rebuild is not None and rebuild.open_one() is not None

    def scheduler_count(self, config: dict | None = None) -> dict:
        info = (self.panel.facts().get("info") or None) if self.panel.reader_present() else None
        return schedulers(info, config if config is not None else self.facts().get("config"),
                          self.panel.settings().get("not_schedulers") or [])

    def preview(self, action: str, *, paths: list | None = None, enabled: bool | None = None) -> dict:
        """POST /api/snapraid/actions: a preview, recorded as A-<n> (state preview): {approval, shows, sends,
        binding, shown_sha256, expires_at, typed?}. ValueError with the refusal's words when an action may not be
        offered now."""
        if action not in ACTIONS:
            raise ValueError(f"action must be one of {', '.join(ACTIONS)}")
        metrics, facts = self.fresh()
        refusal = self.checks(action, facts)
        if refusal:
            engine = facts["info"].get("engine") or "of an unknown version"
            raise ValueError(REFUSALS[refusal].format(version=engine))
        now = self.now()
        d = self.facts()
        start = round(float(d.get("start") or now - float(metrics.get("uptime") or 0)))
        tasks, array, config = facts.get("tasks") or {}, facts.get("array") or {}, facts.get("config") or {}
        state = facts.get("state") or {}
        limits = {"deletes": config.get("sync_threshold_deletes"), "updates": config.get("sync_threshold_updates")}
        shows: dict[str, Any] = {}
        body: dict[str, Any] = {}
        binding: dict[str, Any] = {"started_at": start}
        typed = None
        target = None
        if action == "stop":
            active = (tasks.get("active") or [None])[0]
            if not active:
                raise ValueError(REFUSALS["nothing_running"])
            shows = {"what": WORDS_OF.get(active.get("command"), "a step"), "of": HIGH_WORDS.get(
                active.get("high_command"), "a run"), "number": active["number"], "progress": active.get("progress"),
                "since": active.get("started_at"), "line": STOP_LINE}
            binding["active_number"] = active["number"]
            target = {"number": active["number"], "scheduled_at": active.get("scheduled_at"),
                      "high_command": active.get("high_command")}
        elif action == "hold_off":
            enabled = True if enabled is None else bool(enabled)
            window = next_window(config.get("maintenance_schedule"), now)
            if enabled and window is None:
                raise ValueError(REFUSALS["no_window"])
            shows = {"enabled": enabled, "window": window, "window_words": window_words(window, now),
                     "lines": list(SKIP_LINES) if enabled else ["The next run will happen as scheduled."]}
            body = {"enabled": enabled}
            binding.update(schedule=config.get("maintenance_schedule"), hold_off=bool(metrics.get("hold_off")))
        elif action in ("maintenance", "accept"):
            counts = {change: int(array.get(f"diff_{change}") or 0) for change in CHANGES}
            shows = {"counts": counts, "limits": limits, "limits_line": limits_line(limits),
                     "health": state.get("health"), "scan_at": array.get("last_command_at"),
                     "check_after": bool(config.get("scrub_percentage"))}
            binding.update(thresholds=limits, prevent_truncations=config.get("sync_prevent_truncations"),
                           nothing_queued=True)
            if action == "accept":
                if not (d.get("freeze") or (metrics.get("last_exit") or {}).get("sync") == 2):
                    raise ValueError(REFUSALS["not_frozen"])
                scan = {"last_command": array.get("last_command"), "last_command_at": array.get("last_command_at"),
                        "last_sync_at": array.get("last_sync_at"),
                        "counts": {c: counts[c] for c in CHANGES if c != "equal"}}
                reviewed = self._reviewed or {}
                if reviewed.get("scan") != scan:
                    raise ValueError(REFUSALS["no_review"])
                shows.update(per_disk=reviewed.get("per_disk") or {}, accept_line=ACCEPT_LINE)
                binding["scan"] = scan
                biggest = max((counts["removed"] / limits["deletes"]) if limits.get("deletes") else 0,
                              (counts["updated"] / limits["updates"]) if limits.get("updates") else 0)
                if biggest > TYPED_FACTOR:
                    typed = counts["removed"] + counts["updated"]
                body = {"ignore_thresholds": True}
            else:
                binding["health"] = state.get("health")
        elif action == "undelete":
            chosen = self._check_paths(paths, facts)
            by_disk: dict[str, int] = {}
            for entry in chosen:
                by_disk[entry["disk"]] = by_disk.get(entry["disk"], 0) + 1
            shows = {"count": len(chosen), "by_disk": by_disk, "lines": list(UNDELETE_LINES),
                     "as_of": array.get("last_sync_at")}
            binding.update(scan={"last_command": array.get("last_command"),
                                 "last_command_at": array.get("last_command_at"),
                                 "last_sync_at": array.get("last_sync_at"),
                                 "counts": {c: int(array.get(f"diff_{c}") or 0) for c in CHANGES if c != "equal"}},
                           health=state.get("health"))
            body = {"paths_count": len(chosen), "paths_sha256": hashlib.sha256(
                json.dumps(chosen, sort_keys=True).encode()).hexdigest()}
        else:   # heal
            bad = int(array.get("blocks_bad") or 0)
            if not bad and state.get("health") != "corrupt":
                raise ValueError(REFUSALS["no_damage"])
            shows = {"places": bad, "health": state.get("health"),
                     "line": "It repairs the damaged places from the protection data, then checks them again."}
            binding.update(blocks_bad=array.get("blocks_bad"), health=state.get("health"))
        floor = self._link_floor()
        with self._lock:
            approval = self._new_id(floor)
            expires = now + float(self.cfg()["approval_minutes"]) * 60
            record = {"id": approval, "action": action, "created_at": now, "expires_at": expires, "shows": shows,
                      "sends": {"action": "maintenance" if action == "accept" else action, "body": body},
                      "binding": binding, "typed": typed, "state": "preview", "outcome": None,
                      "fix_before": {"recovered": array.get("fix_recovered"),
                                     "unrecoverable": array.get("fix_unrecoverable")},
                      "window_at_preview": shows.get("window"), "target": target}
            record["shown_sha256"] = shown_hash(record)
            self._write(record)
            if action == "undelete":
                self._paths[approval] = chosen
        self.panel._journal("action", f"{approval}: a preview of {action}", approval=approval, action=action,
                            state="preview")
        self._publish_action(record, "preview")
        return self.public(record)

    def _check_paths(self, paths: Any, facts: dict) -> list[dict]:
        if not isinstance(paths, list) or not paths or len(paths) > 1000:
            raise ValueError("choose from 1 to 1,000 files or folders to bring back")
        chosen = []
        for entry in paths:
            if not isinstance(entry, dict) or set(entry) - {"disk", "path", "folder"} or \
                    not isinstance(entry.get("disk"), str) or not isinstance(entry.get("path"), str):
                raise ValueError("each chosen path is {disk, path, folder?}")
            parts = entry["path"].strip("/").split("/")
            if not entry["path"].strip("/") or any(p in ("", ".", "..") for p in parts) or \
                    any(c in entry["path"] for c in "\0\n\r"):
                raise ValueError("a chosen path has an empty, '.' or '..' part, or a control character")
            chosen.append({"disk": entry["disk"], "path": entry["path"].strip("/"),
                           "folder": bool(entry.get("folder"))})
        if len({entry["disk"] for entry in chosen}) > 1:
            raise ValueError(REFUSALS["one_disk"])
        disks = {disk.get("name"): disk for disk in facts.get("disks") or []}
        mounts = ((self.panel.facts().get("info") or {}).get("mounts") or {}) if self.panel.reader_present() else {}
        for disk in {entry["disk"] for entry in chosen}:
            found = disks.get(disk)
            if found is None or not found.get("devices") or found.get("health") == "degraded":
                raise ValueError(REFUSALS["health"])
            mount = mounts.get(disk)
            if not mount or not mount.get("mounted") or not mount.get("uuid_ok"):
                raise ValueError(REFUSALS["mount_unknown"])
        return chosen

    def public(self, record: dict) -> dict:
        """An approval as the cockpit sees it."""
        return {"approval": record["id"], "action": record["action"], "shows": record["shows"],
                "sends": record["sends"], "binding": record["binding"], "shown_sha256": record["shown_sha256"],
                "expires_at": record["expires_at"], "typed": bool(record.get("typed")), "state": record["state"],
                "outcome": record.get("outcome"), "words": record.get("words"), "run": record.get("run"),
                "outcome_words": outcomes.WORDS.get(record.get("outcome") or "", None)}

    def cancel(self, approval: str) -> dict:
        record = self.approval(approval)
        if record["state"] != "preview":
            raise ValueError(REFUSALS["used"])
        record["state"] = "cancelled"
        self._write(record)
        self._paths.pop(approval, None)
        return self.public(record)

    def approve(self, approval: str, shown_sha256: str, typed: Any = None) -> dict:
        """POST /api/snapraid/actions/<A-n>/approve: one send through the link. Changed when the binding no longer
        holds (nothing sent); ValueError for a refusal before sending."""
        with self._lock:
            record = self.approval(approval)
            now = self.now()
            if record["state"] != "preview":
                raise ValueError(REFUSALS["used"])
            if now > float(record["expires_at"]):
                record["state"] = "expired"
                self._write(record)
                raise ValueError(REFUSALS["expired"])
            if shown_sha256 != record["shown_sha256"] or shown_hash(record) != record["shown_sha256"]:
                raise ValueError(REFUSALS["mismatch"])
            if record.get("typed") and typed != record["typed"]:
                raise ValueError(REFUSALS["typed"])
            if record["action"] == "hold_off" and record["sends"]["body"].get("enabled"):
                window = record.get("window_at_preview")
                if window is not None and now >= float(window):
                    raise Changed("the run you meant to skip has already started")
            if record["action"] == "undelete" and approval not in self._paths:
                record["state"] = "void"
                self._write(record)
                raise ValueError("Seraphina restarted since you chose the files. Choose them again.")
            facts = {"info": self.facts().get("info") or {}, "config": self.facts().get("config"),
                     "state": self.facts().get("state"), "array": self.facts().get("array")}
            refusal = self.checks(record["action"], facts)
            if refusal:
                raise ValueError(REFUSALS[refusal].format(version=facts["info"].get("engine") or "?"))
            link = self.link()
            if link is None:
                raise ValueError(REFUSALS["no_link"])
            body = dict(record["sends"]["body"])
            if record["action"] == "undelete":
                body = {"paths": self._paths[approval]}
            # the intent first (DESIGN §40): written before the call, so a crash leaves an unknown outcome
            record.update(state="sent", sent_at=now, outcome=None)
            self._write(record)
            self.panel._journal("action", f"{approval}: {record['action']} sent", approval=approval,
                                action=record["action"], state="sent")
            try:
                answer = link.call("act", timeout=ACT_TIMEOUT, approval=approval,
                                   action=record["sends"]["action"], body=body, expect=record["binding"])
            except LinkError as exc:
                return self._link_refused(record, exc)
            self._paths.pop(approval, None)
            record.update(outcome=answer.get("outcome"), status=answer.get("status"), words=answer.get("words"),
                          message=answer.get("message"), before=answer.get("before"),
                          response=outcomes.response(answer.get("status"), [answer.get("number")],
                                                     None) | {"body_sha256": answer.get("body_sha256")})
            if record["action"] == "stop" and answer.get("number") is not None:
                record["stopped_number"] = answer["number"]
            if record["action"] == "stop" and answer.get("number") is not None and \
                    answer["number"] != record["binding"].get("active_number"):
                record["wrong_task"] = answer["number"]
                self._finding(f"snapraid:wrongstop:{approval}", "A stop reached a different step",
                              f"The protection service stopped step {answer['number']}, not the step you approved "
                              f"({record['binding'].get('active_number')}). Nothing was sent again. Look at its runs "
                              f"in Backups.")
                self._notice("parity.failed", {})
            if record["action"] == "stop" and answer.get("status") == 409:
                record["words"] = "Nothing was running to stop."
            self._write(record)
            if record["outcome"] == outcomes.UNKNOWN:
                self.look_again(record)
            elif record["outcome"] == outcomes.SUCCEEDED and record["action"] in STARTS_RUNS:
                self._after_send(record)
            self.panel._journal("action", f"{approval}: {record['action']} {record['outcome']}", approval=approval,
                                action=record["action"], state="sent", outcome=record["outcome"],
                                status=record.get("status"))
            self._publish_action(record, "sent")
            self._probe = None
            return self.public(self.approval(approval))

    def _link_refused(self, record: dict, exc: LinkError) -> dict:
        """The link refused before sending (one of LINK_REFUSALS: the binding, section 2.4), or never got the
        request: failed. It got the request and no answer came, or it answered with an error that may come after
        its POST (its own failure): unknown, looked at again read-only (DESIGN §40)."""
        if exc.code == "changed":
            record.update(state="refused", outcome=outcomes.FAILED, words=exc.words, refused="changed")
            self._write(record)
            self._paths.pop(record["id"], None)
            raise Changed(exc.words)
        if not exc.sent or (exc.answer and exc.code in LINK_REFUSALS):
            record.update(outcome=outcomes.FAILED, words=exc.words, refused=exc.code,
                          state="refused" if exc.answer else "sent")
            self._write(record)
            self._paths.pop(record["id"], None)
            self._publish_action(record, "refused")
            return self.public(record)
        # The look waits until the link can no longer send. A link that answered has ended; one that did not may
        # still be working (systemd ends it at RuntimeMaxSec, which ACT_TIMEOUT outlasts), and a look before then
        # could call a skip that is still on its way "did not take effect".
        look_after = self.now() if exc.answer else float(record.get("sent_at") or self.now()) + ACT_TIMEOUT
        record.update(outcome=outcomes.UNKNOWN, link_error=exc.code if exc.answer else None, look_after=look_after,
                      words="Seraphina's link did not answer, or failed: the request may or may not have reached the "
                            "protection service. She looks again, read-only, once the link can no longer send it.")
        self._write(record)
        self._paths.pop(record["id"], None)
        if self.now() >= look_after:
            self.look_again(record)
        return self.public(self.approval(record["id"]))

    def _due_looks(self, now: float) -> None:
        """The read-only looks _link_refused put off: each once, when its time has come."""
        for record in self.approvals():
            if record.get("state") == "sent" and record.get("outcome") == outcomes.UNKNOWN and \
                    record.get("look_after") and not record.get("looked_at") and now >= float(record["look_after"]):
                self.look_again(record)

    def _after_send(self, record: dict) -> None:
        """Right after a 2xx: read the task list once and match the run (section 6.4)."""
        with contextlib.suppress(LinkError, ValueError):
            link = self.link()
            tasks = link.call("read", what=["tasks"]).get("tasks") or {}
            run = match_run(tasks, record["action"], record.get("before") or {})
            if run is not None:
                record["run"] = {"numbers": run["numbers"], "scheduled_at": run["scheduled_at"],
                                 **verdict(run, self._stopped()), "ended": run_ended(run)}
                self._write(record)

    def look_again(self, record: dict) -> None:
        """After an unknown outcome: the same search, read-only, and never a second send (section 6.4 rule 4):
        tasks found, "succeeded (queued), seen afterwards"; none and no restart, "did not take effect"; a restart in
        between, unknown with the tracker item."""
        link = self.link()
        try:
            metrics = self.metrics_reader().read()
            tasks = link.call("read", what=["tasks"]).get("tasks") or {} if link else None
        except (MetricsError, LinkError, ValueError):
            self._unknown_item(record)
            return
        before = record.get("before") or {}
        start = self.now() - float(metrics.get("uptime") or 0)
        started_at = before.get("started_at") or record["binding"].get("started_at")
        restarted = started_at is not None and abs(start - float(started_at)) > RESTART_SLACK
        action = record["action"]
        seen = None
        if action == "hold_off":
            on = bool(metrics.get("hold_off"))
            wanted = bool(record["sends"]["body"].get("enabled"))
            window = record.get("window_at_preview")
            passed = window is not None and self.now() >= float(window)
            if on == wanted:
                seen = True
            elif not passed and not restarted:
                seen = False
        elif action == "stop" and link is not None and not restarted:
            seen = self._stop_seen(link, record)
        elif tasks is not None and before:
            run = match_run(tasks, action, before)
            if run is not None:
                seen = True
                record["run"] = {"numbers": run["numbers"], "scheduled_at": run["scheduled_at"],
                                 **verdict(run, self._stopped()), "ended": run_ended(run)}
            elif not restarted:
                seen = False
        record["looked_at"] = self.now()
        if seen is True:
            record.update(outcome=outcomes.SUCCEEDED, seen_afterwards=True,
                          words="It was taken: seen afterwards in the protection service's list.")
        elif seen is False:
            record.update(outcome=outcomes.FAILED, did_not_take_effect=True,
                          words="It did not take effect: nothing was sent again. Press again for a fresh go-ahead.")
        else:
            self._unknown_item(record)
        self._write(record)

    def _stop_seen(self, link: LinkClient, record: dict) -> bool | None:
        """Section 6.7 rule 3, read-only: only the stopped task is read (the link's run op). The daemon's own "Stop
        requested" among its messages: taken (runner.c:2219 adds it at once). The task ended with no such message
        while it is still the daemon's latest, so no later task can have been reached: did not take effect.
        Anything else (the task gone, another one since, its messages not all readable): None, unknown. Its status
        alone decides nothing: 15.1 stays "processing" for seconds after a stop (lab C 05)."""
        target = record.get("target") or {}
        number = record["binding"].get("active_number")
        if number is None:
            return None
        try:
            answer = link.call("run", number=int(number))
        except (LinkError, ValueError, TypeError):
            return None
        task = answer.get("task") or {}
        if target.get("scheduled_at") is not None and task.get("scheduled_at") != target["scheduled_at"]:
            return None
        if answer.get("stop_requested") is True:
            return True
        if answer.get("stop_requested") is False and task.get("status") in ENDED:
            return False
        return None

    def _unknown_item(self, record: dict) -> None:
        what = {"stop": "a stop", "hold_off": "a skip of the next run"}.get(record["action"], "a protection run")
        self._finding(f"snapraid:unknown:{record['id']}", f"{outcomes.UNKNOWN_TITLE} ({what})",
                      f"Seraphina asked the protection service for {what} ({record['id']}) and could not tell whether "
                      f"it happened. Look at its runs in Backups before asking again; Seraphina sends nothing again by "
                      f"herself.", kind="commitment")

    def _follow(self, d: dict, tasks: dict) -> None:
        """Each sent approval's run: matched, then followed to its verdict (section 6.5)."""
        for record in self.approvals():
            if record.get("state") != "sent" or record.get("outcome") != outcomes.SUCCEEDED or \
                    record["action"] not in STARTS_RUNS or (record.get("run") or {}).get("ended"):
                continue
            run = None
            numbers = (record.get("run") or {}).get("numbers")
            if numbers:
                run = next((r for r in runs_of(tasks) if set(r["numbers"]) & set(numbers) and
                            r["scheduled_at"] == record["run"].get("scheduled_at")), None)
            if run is None:
                run = match_run(tasks, record["action"], record.get("before") or {})
            if run is None:
                continue
            result = verdict(run, self._stopped())
            array = d.get("array") or {}
            if run_ended(run):
                if record["action"] in ("maintenance", "accept") and result["verdict"] in ("done", "warning") and \
                        (d.get("metrics") or {}).get("blocks_unsynced", 0) != 0:
                    result = {"verdict": "partial"}
                if record["action"] == "undelete":
                    before = record.get("fix_before") or {}
                    result["recovered"] = int(array.get("fix_recovered") or 0) - int(before.get("recovered") or 0)
                    result["unrecoverable"] = int(array.get("fix_unrecoverable") or 0) - \
                        int(before.get("unrecoverable") or 0)
                    if result["verdict"] == "failed" and result["recovered"] > 0 and not result["unrecoverable"]:
                        result["verdict"] = "done"      # the engine's exit 1 on an unrelated file (section 6.5)
            record["run"] = {"numbers": run["numbers"], "scheduled_at": run["scheduled_at"], **result,
                             "ended": run_ended(run)}
            self._write(record)

    # -- the review page (section 5.3) ---------------------------------------------------------------------------

    def review(self, *, disk: str | None = None, change: str | None = None, q: str | None = None,
               offset: int = 0) -> dict:
        """GET /api/snapraid/review: one page of the last scan's change list, live from the link; nothing is kept
        but the scan's identity and its per-disk counts (for an accept's binding)."""
        link = self.link()
        if link is None:
            raise ValueError(REFUSALS["no_link"])
        fields = {key: value for key, value in (("disk", disk), ("change", change), ("q", q)) if value not in (None, "")}
        try:
            page = link.call("review", offset=int(offset or 0), **fields)
        except LinkError as exc:
            raise ValueError(exc.words) from None
        self._reviewed = {"scan": page.get("scan"), "per_disk": page.get("per_disk"), "at": self.now()}
        return {key: page.get(key) for key in ("scan", "per_disk", "total", "offset", "cut", "entries")}

    def fixes(self, offset: int = 0) -> dict:
        link = self.link()
        if link is None:
            raise ValueError(REFUSALS["no_link"])
        try:
            page = link.call("fixes", offset=int(offset or 0))
        except LinkError as exc:
            raise ValueError(exc.words) from None
        return {key: page.get(key) for key in ("fix_recovered", "fix_unrecoverable", "offset", "entries")}

    def runs(self) -> dict:
        """GET /api/snapraid/runs: the daemon's recent runs (typed fields), with Seraphina's approvals marked."""
        tasks = self.facts().get("tasks") or {}
        records = self.approvals()
        stopped = self._stopped()
        out = []
        for run in runs_of(tasks)[:20]:
            mine = self._own_run(run, records)
            approval = mine["id"] if mine else None
            out.append({"high_command": run["high_command"], "words": HIGH_WORDS.get(run["high_command"], "a run"),
                        "scheduled_at": run["scheduled_at"], "numbers": run["numbers"], **verdict(run, stopped),
                        "approval": approval, "started_by": "you, in the cockpit" if approval else
                        ("the protection service" if run["high_command"] in ("startup", "suspend_idle") else
                         "the nightly protection run or outside Seraphina"),
                        "steps": [{key: t.get(key) for key in ("number", "command", "status", "exit_code",
                                                               "exit_sig", "exit_msg", "started_at", "finished_at")}
                                  for t in run["steps"]]})
        return {"runs": out}

    def run(self, number: int) -> dict:
        link = self.link()
        if link is None:
            raise ValueError(REFUSALS["no_link"])
        try:
            answer = link.call("run", number=int(number))
        except LinkError as exc:
            if exc.code == "no_record":
                raise KeyError(exc.words) from None
            raise ValueError(exc.words) from None
        return {"task": answer.get("task"), "messages": answer.get("messages") or []}

    def report_event(self, subject: Any, detail: Any) -> None:
        """A parity.report from the forwarder: a prompt to read the daemon now (section 7.2); its text is never
        used for a decision."""
        with self._lock:
            d = self.facts()
            d.pop("metrics_tried", None)
            d["full_at"] = 0
            self.save(d)
        with contextlib.suppress(Exception):
            self.tick()

    # -- the view ------------------------------------------------------------------------------------------------

    def view(self) -> tuple[dict, list[dict]]:
        """The daemon part of GET /api/snapraid, and the latest approvals."""
        d = self.facts()
        now = self.now()
        metrics = d.get("metrics") or {}
        config = d.get("config") or {}
        info = d.get("info") or {}
        port = _port(str(self.cfg()["metrics_url"]))
        beyond = listening_beyond_loopback(port, self.proc_root) if port else None
        window = next_window(config.get("maintenance_schedule"), now)
        count = self.scheduler_count()
        records = self.approvals()
        frozen = d.get("freeze")
        devices = _devices(d)
        return {
            "answering": bool(metrics) and "down_since" not in d, "seen": bool(d.get("seen")),
            "down_since": d.get("down_since"), "error": (d.get("error") or {}).get("words"),
            "auth_on": info.get("auth_on"), "listen": {"beyond_loopback": beyond},
            "engine": (metrics.get("build") or {}).get("engine") or info.get("engine"),
            "version": (metrics.get("build") or {}).get("daemon") or info.get("daemon"),
            "started_at": d.get("start"), "restarted_at": d.get("restarted_at"),
            "window": {"next": window, "words": window_words(window, now), "source": "the protection service"
                       if config.get("maintenance_schedule") else None, "schedule": config.get("maintenance_schedule")},
            "hold_off": metrics.get("hold_off"), "lapsed": d.get("lapsed"),
            "frozen": {"counts": {k: frozen.get(k) for k in ("removed", "updated", "added")},
                       "limits": frozen.get("limits"), "which": frozen.get("which"), "since": frozen.get("since")}
            if frozen else None,
            "active": metrics.get("active"), "queue": metrics.get("queue_depth"),
            "protected_up_to": d.get("protected_up_to"), "partial": (metrics.get("blocks_unsynced") or 0) > 0,
            "last_run": self._last_run(d), "schedulers": count,
            "link": {**(d.get("link") or {"installed": self.link() is not None}), "allow": info.get("allow"),
                     "words": None if self.link() is not None else NO_LINK},
            "report_failed": bool(d.get("report_failed")), "outside": d.get("outside"),
            "health": metrics.get("health"), "blocks_bad": metrics.get("blocks_bad"),
            "disks": [{"name": name, "key": key, "label": label, "role": (metrics["disks"][name] or {}).get("role"),
                       "health": disk_health((metrics["disks"][name] or {}).get("health"), devices.get(name))}
                      for name, key, label in disk_keys(metrics.get("disks") or {})],
            "limits": metrics.get("thresholds"), "limits_line": limits_line(metrics.get("thresholds")),
            "lab": [{**check, "status": "not_run"} for check in LAB_CHECKS],
            "offers": self.offers(d, count),
        }, [self.public(r) for r in records[-10:]]

    def _last_run(self, d: dict) -> dict | None:
        for run in runs_of(d.get("tasks") or {}):
            if run["high_command"] in ("maintenance", "heal", "undelete") and run_ended(run):
                last = max((daemon_time(t.get("finished_at")) or 0) for t in run["steps"])
                return {**verdict(run, self._stopped()), "high_command": run["high_command"],
                        "finished_at": last or None,
                        "numbers": run["numbers"]}
        return None

    def offers(self, d: dict, count: dict) -> dict:
        """Which buttons the card shows (absent when the link does not allow them or the checks would refuse)."""
        info = d.get("info") or {}
        allow = info.get("allow") or []
        metrics = d.get("metrics") or {}
        ok = self.link() is not None and info.get("auth_on") is True and "act" in allow and \
            re.match(r"^(1[5-9]|[2-9][0-9])\.", str(info.get("engine") or "") + ".") is not None
        health_ok = metrics.get("health") not in BAD_HEALTH
        one = count.get("count") == 1 and not self._rebuilding()      # no run while a disk is being replaced
        return {"stop": ok and bool(metrics.get("active")),
                "hold_off": ok and bool((d.get("config") or {}).get("maintenance_schedule")),
                "maintenance": ok and one and health_ok and not metrics.get("active") and not metrics.get("queue_depth"),
                "accept": ok and one and health_ok and "accept" in allow and bool(d.get("freeze")),
                "undelete": ok and one and health_ok,
                "heal": ok and one and health_ok and bool(metrics.get("blocks_bad") or metrics.get("health") ==
                                                          "corrupt")}

    # -- notices, tracker items, events ------------------------------------------------------------------------

    def _notice(self, kind: str, fields: dict, key: str = "-") -> None:
        if self.panel.notifier is not None:
            with contextlib.suppress(Exception):
                self.panel.notifier.parity_event(kind, fields, key)

    def _clear(self, kind: str, key: str = "-") -> None:
        if self.panel.notifier is not None:
            with contextlib.suppress(Exception):
                self.panel.notifier.parity_clear(kind, key)

    def _finding(self, dedupe: str, title: str, detail: str, kind: str = "finding") -> None:
        self._open(dedupe, title, detail, kind, update=True)

    def _decision(self, dedupe: str, title: str, detail: str, update: bool = False) -> None:
        self._open(dedupe, title, detail, "decision", update=update)

    def _open(self, dedupe: str, title: str, detail: str, kind: str, update: bool) -> None:
        from .snapraid import _open_item

        system = self.panel.system
        if system is None:
            return
        with contextlib.suppress(Exception):
            with system.transaction() as ledger:
                item = _open_item(ledger, dedupe)
                fields = {"title": title, "detail": detail, "kind": kind,
                          "source": {"type": "snapraid", "key": dedupe.split(":", 1)[1]}}
                if item is None:
                    tracker.open_once(ledger, fields, dedupe)
                elif update and (item.get("title") != title or item.get("detail") != detail):
                    ledger.retitle(item["id"], title, detail=detail, note="seen again")

    def _close(self, dedupe: str, why: str, title: str | None = None) -> None:
        """Close the open item of dedupe (only when it still has that title, if given: snapraid:service is shared
        by the service's causes, and one cause's end must not close another's)."""
        from .snapraid import _open_item

        system = self.panel.system
        if system is None:
            return
        with contextlib.suppress(Exception):
            with system.transaction() as ledger:
                item = _open_item(ledger, dedupe)
                if item is not None and (title is None or item.get("title") == title):
                    ledger.change(tracker.FACTORY, item["id"], "verified", note=why,
                                  evidence=[{"kind": "receipt", "ref": "snapraidd:tasks", "check": "ok", "detail": why}])

    def _publish_action(self, record: dict, state: str) -> None:
        if self.panel.bus is not None:
            self.panel.bus.publish("snapraid.action", {"approval": record["id"], "action": record["action"],
                                                       "state": state, "outcome": record.get("outcome")})

    def _publish_daemon(self, **fields: Any) -> None:
        if self.panel.bus is not None:
            self.panel.bus.publish("snapraid.daemon", fields)


def shown_hash(record: dict) -> str:
    """The sha256 of what the cockpit showed and what will be sent: the cockpit sends it back with the press."""
    part = {key: record.get(key) for key in ("id", "action", "shows", "sends", "binding", "typed", "expires_at")}
    return hashlib.sha256(json.dumps(part, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def disk_health(health: Any, devices: Any) -> Any:
    """A disk's health as the card and the light take it: /metrics' value, except that a disk whose devices the
    daemon could not resolve (the link's disks read gives devices []) is "unknown", never "passed": its SMART was
    never read (brief section 13, design T49). A failing reading still stands."""
    if devices == [] and health not in BAD_HEALTH:
        return "unknown"
    return health


def disk_keys(disks: dict) -> list[tuple[str, str, str]]:
    """(SnapRAID name, key, label) per disk, in the daemon's order: data disks "1", "2" ("Disk 1"), protection disks
    "p1" ("The protection disk", or "Protection disk 2")."""
    data = [name for name, entry in disks.items() if (entry or {}).get("role") == "data"]
    parity = [name for name, entry in disks.items() if (entry or {}).get("role") == "parity"]
    out = [(name, str(index), f"Disk {index}") for index, name in enumerate(data, 1)]
    out += [(name, f"p{index}", "The protection disk" if len(parity) == 1 else f"Protection disk {index}")
            for index, name in enumerate(parity, 1)]
    return out


def _devices(d: dict) -> dict:
    """Each disk's devices from the link's last disks read (absent: not read, so /metrics alone decides)."""
    return {disk.get("name"): disk.get("devices") for disk in d.get("disks") or []
            if isinstance(disk, dict) and isinstance(disk.get("devices"), list)}


def _port(url: str) -> int | None:
    match = re.search(r":([0-9]{1,5})/", url)
    return int(match.group(1)) if match else None


# ---------------------------------------------------------------- the light (section 4.2)


def light(view: dict, cfg: dict, now: float, *, unknown_open: bool = False, zone: Any = None) -> dict:
    """{tone, code, reason, disk?, note?}: the first rule of section 4.2's daemon table that matches. view: the
    daemon part of the card (DaemonRoute.view); cfg: the snapraid settings."""
    from .snapraid import age_words, plural, when_words

    limits = cfg.get("light") or {}
    stale_after = float(cfg.get("stale_minutes") or 180) * 60
    red_days = float(limits.get("red_after_days", 7))

    def tone(t: str, code: str, **values: Any) -> dict:
        return {"tone": t, "code": code, "reason": REASONS[code].format(**values)}
    beyond = (view.get("listen") or {}).get("beyond_loopback")
    if view.get("auth_on") is False:
        return tone("err", "open_beyond" if beyond else "auth_off")
    disks = view.get("disks") or []
    for disk in disks:
        if disk.get("health") in ("failing", "prefail"):
            return {**tone("err", "failing", disk=disk["label"]), "disk": disk["key"]}
    for disk in disks:
        if disk.get("health") == "degraded":
            return {**tone("err", "degraded", disk=disk["label"]), "disk": disk["key"]}
    if view.get("health") == "degraded":
        return tone("err", "degraded", disk="A disk")
    if (view.get("blocks_bad") or 0) > 0 or view.get("health") == "corrupt":
        return tone("err", "bad_blocks", places=plural(int(view.get("blocks_bad") or 0) or 1, "place"))
    engine = str(view.get("engine") or "")
    if engine and not re.match(r"^(1[5-9]|[2-9][0-9])\.", engine + "."):
        return tone("err", "engine_old")
    frozen = view.get("frozen")
    protected = view.get("protected_up_to")
    if frozen and frozen.get("since") and now - float(frozen["since"]) > red_days * 86400:
        return tone("err", "frozen_long", days=plural(int((now - float(frozen["since"])) // 86400), "day"))
    if protected and now - float(protected) > red_days * 86400:
        return tone("err", "not_protected_days", days=plural(int((now - float(protected)) // 86400), "day"))
    if frozen:
        return tone("warn", "frozen")
    if view.get("partial"):
        return tone("warn", "partial")
    if unknown_open:
        return tone("warn", "unknown_outcome")
    if view.get("lapsed"):
        return tone("warn", "lapsed")
    count = (view.get("schedulers") or {}).get("count")
    if count is not None and count > 1:
        return tone("warn", "two_schedulers")
    if count == 0:
        return tone("warn", "no_scheduler")
    if beyond:
        return tone("warn", "beyond_loopback")
    if view.get("down_since") and now - float(view["down_since"]) >= float(
            ((cfg.get("daemon") or {}).get("down_minutes")) or 30) * 60:
        return tone("warn", "down")
    if view.get("report_failed"):
        return tone("warn", "report_failed")
    if view.get("outside"):
        return tone("warn", "outside")
    for disk in disks:
        if disk.get("health") in ("pending", "unknown") and disk["name"] not in (limits.get("smart_unreadable_ok") or []):
            return {**tone("warn", "health_unknown", disk=disk["label"]), "disk": disk["key"]}
    if protected and now - float(protected) > float(limits.get("amber_after_hours", 36)) * 3600:
        return tone("warn", "old", age=age_words(now - float(protected)))
    if not view.get("answering"):
        at = view.get("down_since")
        return tone("muted", "stale", age=age_words(now - float(at))) if at else tone("muted", "never_checked")
    if not protected:
        return tone("warn", "never_protected")
    return tone("ok", "ok", when=when_words(float(protected), now, zone))


def novice_strings() -> list[str]:
    """Every novice sentence of the daemon route (U7's word check)."""
    return [*REASONS.values(), NO_LINK, NO_READER_SCHEDULERS, LIMITS_LINE, *LIMIT_LINES.values(), ACCEPT_LINE,
            *UNDELETE_LINES, STOP_LINE,
            *SKIP_LINES, *REFUSALS.values(), *WORDS_OF.values(), *HIGH_WORDS.values(), *VERDICT_WORDS.values(),
            *(check["what"] for check in LAB_CHECKS), *(check["short"] for check in LAB_CHECKS),
            *SCHEDULERS_FINDING, *NO_SCHEDULER_FINDING, *LOGIN_FINDING]


# ---------------------------------------------------------------- the doctor (section 12.7)


def state_without_login(url: str, fetcher: Callable[[str], tuple[int, bytes]] | None = None) -> int | None:
    """The status of an unauthenticated GET /snapraid/v1/state (401: the login is on; 200: every route is open),
    None with no answer. The harness sends no credential; it has none."""
    target = re.sub(r"/metrics$", "/snapraid/v1/state", url)
    if fetcher is not None:
        try:
            return fetcher(target)[0]
        except (OSError, http.client.HTTPException):
            return None
    match = re.fullmatch(r"http://(127\.0\.0\.1|\[::1\]):([0-9]{1,5})(/snapraid/v1/state)", target)
    if not match:
        return None
    conn = http.client.HTTPConnection("::1" if match.group(1).startswith("[") else "127.0.0.1", int(match.group(2)),
                                      timeout=2.0)
    try:
        conn.request("GET", match.group(3))
        response = conn.getresponse()
        response.read(4096)
        return response.status
    except (OSError, http.client.HTTPException):
        return None
    finally:
        conn.close()


def doctor_checks(config: Any, cfg: dict, make: Callable[..., dict], *, metrics: MetricsReader | None = None,
                  link: LinkClient | None = None, state_status: Callable[[str], int | None] | None = None,
                  reader_info: dict | None = None, data_dir: Path | None = None, proc_root: str = "/",
                  groups: list[str] | None = None, daemon_conf: str = DAEMON_CONF) -> list[dict]:
    """The daemon route's doctor lines, read-only: /metrics, the login, the listener, the engine, the link, the
    daemon's settings (section 10.3), the scheduler count, reports and the link's journal, the harness's user."""
    daemon = settings(cfg)
    reader = metrics or MetricsReader(str(daemon["metrics_url"]))
    out: list[dict] = []
    try:
        facts = reader.read()
    except (MetricsError, ValueError) as exc:
        installed = os.path.exists(daemon_conf)
        out.append(make("snapraid daemon", not installed, "warn" if installed else "info",
                        f"the SnapRAID Daemon does not answer its /metrics ({getattr(exc, 'words', exc)})" if installed
                        else "no SnapRAID Daemon answers on this machine (optional: it runs SnapRAID on a schedule)"))
        return out
    build = facts.get("build") or {}
    out.append(make("snapraid daemon", True, "info", f"the SnapRAID Daemon {build.get('daemon') or '?'} answers, "
                                                     f"running SnapRAID {build.get('engine') or '?'}"))
    status = (state_status or state_without_login)(str(daemon["metrics_url"]))
    auth_on = status == 401
    if status == 200:
        out.append(make("snapraid daemon login", False, "error", "the SnapRAID Daemon answers without a password: any "
                                                                 "program on this server can run it. Set "
                                                                 "net_auth_credential in /etc/snapraidd.conf"))
    elif status == 401:
        out.append(make("snapraid daemon login", True, "info", "the SnapRAID Daemon asks for a password"))
    port = _port(str(daemon["metrics_url"]))
    beyond = listening_beyond_loopback(port, proc_root) if port else None
    if beyond:
        out.append(make("snapraid daemon listener", False, "warn" if auth_on else "error",
                        "the SnapRAID Daemon listens beyond this machine" + ("" if auth_on else " without a password")
                        + ": set net_port to 127.0.0.1:" + str(port)))
    engine = build.get("engine") or ""
    if not re.match(r"^(1[5-9]|[2-9][0-9])\.", engine + "."):
        out.append(make("snapraid daemon engine", False, "error", f"the SnapRAID Daemon runs SnapRAID "
                                                                  f"{engine or 'of an unknown version'}, which it "
                                                                  f"cannot drive: it needs 15 or newer"))
    if link is None:
        out.append(make("snapraid link", False, "warn", "Seraphina's link to the SnapRAID Daemon is not installed: "
                                                        "she reads only its /metrics (the setup page has its copy "
                                                        "blocks)"))
        config_view = None
    else:
        try:
            info = link.call("info")
            config_view = link.call("read", what=["config"]).get("config") or {}
            allow = (info.get("link") or {}).get("allow") or ["read"]
            out.append(make("snapraid link", True, "info", "Seraphina's link answers; it allows " + ", ".join(allow)))
            if "act" in allow and config is not None and _sandbox(config) == "none":
                out.append(make("snapraid link sandbox", False, "error",
                                "the link allows asking for runs, but the agents run without the sandbox "
                                "(harness.sandbox is none): any agent, running as Seraphina's user, can reach the "
                                "link. Keep the link at reading only, or turn the sandbox on"))
        except LinkError as exc:
            out.append(make("snapraid link", False, "warn", f"Seraphina's link does not answer: {exc.words}"))
            config_view = None
    if config is not None:
        from .config import sandbox_masks

        with contextlib.suppress(Exception):
            if Path(daemon["link"]) not in [Path(p) for p in sandbox_masks(config)["files"]]:
                out.append(make("snapraid link masks", False, "warn", "the link's socket is not hidden from the agents' "
                                                                      "sandbox"))
    if config_view:
        out += _daemon_setting_checks(config_view, make, config)
    count = schedulers(reader_info, config_view, cfg.get("not_schedulers") or [])
    if count["count"] is None:
        out.append(make("snapraid schedulers", False, "warn", "the reader is not installed, so Seraphina cannot count "
                                                              "what starts protection runs: actions that start runs "
                                                              "are refused"))
    elif count["count"] != 1:
        out.append(make("snapraid schedulers", False, "warn",
                        f"{count['count']} things start protection runs (" + ", ".join(
                            [p["place"] for p in count["places"] if p["counted"]] +
                            (["the SnapRAID Daemon's window"] if count["window"] else [])) + "): exactly one should"))
    if link is not None and data_dir is not None:
        with contextlib.suppress(LinkError):
            lines = link.call("journal", limit=50).get("lines") or []
            known = {p.stem for p in (Path(data_dir) / "snapraid" / "actions").glob("A-*.json")}
            stray = sorted({str(line.get("approval")) for line in lines if line.get("approval") not in known})
            if stray:
                out.append(make("snapraid link journal", False, "error", f"the link sent {len(stray)} request(s) with "
                                                                         f"no approval in the cockpit: something used "
                                                                         f"its socket directly"))
    member = [group for group in (groups if groups is not None else _groups()) if group in ("docker", "lxd", "sudo")]
    if member:
        out.append(make("snapraid harness user", False, "error", f"the harness's user is in {', '.join(member)}: every "
                                                                 f"agent running as it holds root whatever the sandbox "
                                                                 f"does. Run the harness as a dedicated user"))
    return out


def _daemon_setting_checks(view: dict, make: Callable[..., dict], config: Any) -> list[dict]:
    out = []
    for key, words in (("sync_threshold_deletes", "deletions"), ("sync_threshold_updates", "changes")):
        if not view.get(key):
            out.append(make("snapraid daemon limits", False, "error", f"{key} is 0: the protection service would "
                                                                      f"protect any number of {words}"))
    if not view.get("sync_prevent_truncations"):
        out.append(make("snapraid daemon limits", False, "warn", "sync_prevent_truncations is off: a file cut to zero "
                                                                 "bytes would be protected as empty"))
    if not view.get("notify_result_is_forwarder"):
        out.append(make("snapraid daemon reports", False, "warn", "notify_result does not name Seraphina's forwarder: "
                                                                  "she learns of runs only at her next read"))
    elif view.get("notify_result_level") != "info":
        out.append(make("snapraid daemon reports", False, "warn", "notify_result_level is not info: clean runs are not "
                                                                  "reported"))
    if view.get("config_full_access"):
        out.append(make("snapraid daemon settings", False, "warn", "net_config_full_access is on: whoever holds the "
                                                                   "password can change its notify and hook commands"))
    if view.get("notify_syslog_level") in ("info", "warning"):
        out.append(make("snapraid daemon settings", True, "info", "notify_syslog_level is "
                                                                  f"{view['notify_syslog_level']}: file names may reach "
                                                                  "the system journal (error is recommended)"))
    if view.get("check_updates"):
        out.append(make("snapraid daemon settings", True, "info", "check_updates is on: the protection service "
                                                                  "reaches the internet"))
    from .clock import LocalClock, PowerSchedule

    wakes = (getattr(config, "raw", {}) or {}).get("wakes") or {}
    with contextlib.suppress(ValueError, TypeError, AttributeError):
        power = PowerSchedule.from_config(wakes.get("power"))
        window = next_window(view.get("maintenance_schedule"), time.time())
        if power is not None and window is not None:
            local = datetime.fromtimestamp(window).astimezone(LocalClock(wakes.get("timezone")).zone)
            if power.in_power_off(local):
                out.append(make("snapraid daemon window", False, "warn", "the protection service's window falls inside "
                                                                         "the power-off: the server is off then"))
    return out


def _sandbox(config: Any) -> str:
    from .notify import sandbox_mode

    return sandbox_mode(config)


def _groups() -> list[str]:
    import grp
    import pwd

    with contextlib.suppress(KeyError, OSError):
        user = pwd.getpwuid(os.getuid()).pw_name
        return [g.gr_name for g in grp.getgrall() if user in g.gr_mem] + [grp.getgrgid(os.getgid()).gr_name]
    return []


# ---------------------------------------------------------------- setup: the link's copy blocks (section 10.4)

RELEASE_LINK = Path(__file__).resolve().parent.parent / "bin" / "sf-snapraidd"
UNITS = ("seraphina-snapraidd.socket", "seraphina-snapraidd@.service")
FORWARDER_LINE = f'notify_result = {LINK_PROGRAM} notify result --narrow %l %h %c "%s"'


def draft_link_config(user: str, hook_url: str | None, allow: tuple[str, ...] = ("read",)) -> dict:
    return {"v": 1, "caller": user, "allow": list(allow), "daemon": "http://127.0.0.1:7627",
            **({"hook": {"url": hook_url, "header_file": HOOK_HEADER}} if hook_url else {})}


def daemon_lines() -> list[str]:
    """The daemon's settings Seraphina expects (section 10.3), as lines for /etc/snapraidd.conf; the owner writes
    them himself."""
    return ["net_port = 127.0.0.1:7627", "net_config_full_access = 0", "sync_threshold_deletes = 100",
            "sync_threshold_updates = 100", "sync_prevent_truncations = 1", "sys_shutdown_on =", FORWARDER_LINE,
            "notify_result_level = info", "notify_syslog_level = error", "check_updates = 0"]


def link_blocks(setup: Path | None, release_sha256: str | None = None, group: str = "<the harness's group>") -> list[dict]:
    """The link's install blocks, in order (section 10.4): each installs to a root-owned .new path, checks that copy,
    and moves it into place only when the check passes again. The harness never runs sudo."""
    import shlex

    staged = shlex.quote(str(setup)) if setup else "<stage the files first>"
    new = f"{LINK_PROGRAM}.new"
    check = f"echo {shlex.quote(f'{release_sha256}  {new}')} | sha256sum -c && " if release_sha256 else ""
    return [
        {"id": "L1", "title": "Install Seraphina's link",
         "commands": [f"sudo install -d -m 0755 -o root -g root {os.path.dirname(LINK_PROGRAM)}",
                      f"sudo install -m 0755 -o root -g root {staged}/sf-snapraidd {new}", f"sha256sum {new}"],
         "expect": release_sha256 or "the sha256 published in the release notes",
         "then": [f"{check}sudo mv {new} {LINK_PROGRAM}"],
         "note": "Compare the number with the release notes opened on another device.", "if_not": f"sudo rm {new}"},
        {"id": "L2", "title": "Install its settings",
         "commands": ["sudo install -d -m 0755 -o root -g root /etc/seraphina",
                      f"sudo install -m 0644 -o root -g root {staged}/snapraidd.json {LINK_CONFIG}.new",
                      f"sudo {LINK_PROGRAM} explain"],
         "expect": "It says what the link allows (reading only) and every change; run the second step only when that "
                   "is what you want.",
         "then": [f"sudo {LINK_PROGRAM} explain > /dev/null && sudo mv {LINK_CONFIG}.new {LINK_CONFIG}"],
         "note": "The file holds no secret.", "if_not": f"sudo rm {LINK_CONFIG}.new"},
        {"id": "L3", "title": "Give the link the protection service's password",
         "commands": ["sudo -v", "systemd-ask-password 'The protection service login (user:password):' | "
                                 f"sudo install -m 0600 -o root -g root /dev/stdin {LINK_CREDENTIAL}"],
         "expect": "The first line asks for your own password. The second asks for user:password (the protection "
                   "service's login): type it and press Enter. Each character shows as a star; the password itself "
                   "is never shown and is not kept in the terminal's history or scrollback.",
         "note": "Type it here only: never on a command line, never in the cockpit. Seraphina never sees it."},
        {"id": "L4", "title": "Install the link's units",
         "commands": [f"sudo install -m 0644 -o root -g root {staged}/{unit} /etc/systemd/system/{unit}"
                      for unit in UNITS] + ["sudo systemctl daemon-reload",
                                            "sudo systemctl enable --now seraphina-snapraidd.socket"],
         "expect": "nothing printed", "note": f"The socket lets only root and the group {group} connect."},
        {"id": "L5", "title": "Let the protection service report to Seraphina",
         "commands": [], "action": "hook",
         "expect": "Messages, Event sources: add one named snapraidd that may send parity.report only. Its token is "
                   "shown once: write it into the header file with the copy block shown there, as "
                   f"\"Authorization: Bearer <token>\" in {HOOK_HEADER} (root, 0600)."},
        {"id": "L6", "title": "The protection service's settings",
         "commands": ["sudoedit /etc/snapraidd.conf", "sudo systemctl reload snapraidd"],
         "expect": "These lines in /etc/snapraidd.conf: " + "; ".join(daemon_lines()),
         "note": "Seraphina never changes the protection service's settings. The limits are your choice (100 each is "
                 "recommended)."},
        {"id": "L7", "title": "Check", "commands": [f"sudo {LINK_PROGRAM} notify test < /dev/null"],
         "action": "link-check",
         "expect": "The link answers: the password is on, SnapRAID 15 or newer, reading only; and the test report "
                   "arrives in Messages."},
        {"id": "L8", "title": "Later: allow Seraphina to ask for runs (after the real-service lab, L15)",
         "commands": [f"sudo install -m 0644 -o root -g root {staged}/snapraidd-act.json {LINK_CONFIG}.new",
                      f"sudo {LINK_PROGRAM} explain"],
         "then": [f"sudo {LINK_PROGRAM} explain > /dev/null && sudo mv {LINK_CONFIG}.new {LINK_CONFIG}"],
         "expect": "It says: reading → reading, asking for runs, stops and skips after your go-ahead.",
         "if_not": f"sudo rm {LINK_CONFIG}.new"},
    ]


def link_removal_blocks() -> list[dict]:
    return [{"id": "Lr1", "title": "Remove Seraphina's link",
             "commands": ["sudo systemctl disable --now seraphina-snapraidd.socket",
                          *[f"sudo rm /etc/systemd/system/{unit}" for unit in UNITS], "sudo systemctl daemon-reload",
                          f"sudo rm {LINK_PROGRAM} {LINK_CONFIG} {LINK_CREDENTIAL} {HOOK_HEADER}"],
             "expect": "nothing printed",
             "note": "Then delete the snapraidd event source in Messages and take the notify_result line out of "
                     "/etc/snapraidd.conf. The protection service and SnapRAID are untouched."}]


def unit_text(name: str, group: str) -> str:
    text = (Path(__file__).resolve().parent.parent / "systemd" / name).read_text(encoding="utf-8")
    return text.replace("@GROUP@", group)
