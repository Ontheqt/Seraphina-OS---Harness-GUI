"""Disk protection with SnapRAID, the harness side (DESIGN.md section 41; docs/seraphina/BACKUPS_SNAPRAID.md).

Two routes, chosen on every view (route()): the **daemon** route, once the SnapRAID Daemon answers its /metrics on
loopback (swfactory/snapraidd.py: Seraphina reads the daemon, and asks it for the five actions after the owner's
approval, through her link), and the **reader** route, the read-only fallback while no daemon is installed: the
panel reads SnapRAID's state, health and changes through the root helper bin/sf-snapraid and protects nothing. With
the daemon installed the reader runs no SnapRAID command (only its info), so it never meets the daemon's runs.

- HelperClient calls ``sudo -n <helper>`` with one JSON request line on standard input, through an injectable runner
  (like the Blueprint collector's), and reads the one JSON answer. Every P1 verb only reads: an answer that never
  came is retried at the next refresh, never in a loop (DESIGN §40: a read changes nothing outside).
- SnapraidPanel keeps the facts cache (``<data_dir>/snapraid/facts.json``, 0600, folder 0700) with each read's
  observed_at: counts, reason codes, disk numbers and the config's own paths, never a file name, a disk's model or
  its serial (those reach the cockpit live, a page at a time, from the helper). It computes the light of section
  2.2, staleness (grey after stale_minutes; never green on stale facts), the guard report's counts-only summary,
  health findings (one tracker item per disk number, closed only by a fresh readable observation), the run-outside
  finding from the content file's fingerprint, the disk.failing notice, events and its activity journal.
- doctor_checks is the doctor's one SnapRAID line (section 9.7); copy_blocks the install and removal blocks
  (section 3.10), staged in ``<data_dir>/snapraid/setup/``.

The helper's thresholds, expected mounts and allow classes live in its root-owned config; this module shows them
and stages changes as copy blocks, never writes them.
"""

from __future__ import annotations

import contextlib
import getpass
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from . import items as tracker
from .config import PROJECT_DIR

PHASE = "P4"
DIR = "snapraid"
HELPER = "/usr/local/libexec/seraphina/sf-snapraid"
HELPER_CONFIG = "/etc/seraphina/snapraid-helper.json"
SUDOERS = "/etc/sudoers.d/seraphina-snapraid"
STATE = "/var/lib/seraphina-snapraid"
BINARIES = ("/usr/bin/snapraid", "/usr/local/bin/snapraid")   # the helper's own fixed list
CONFS = ("/etc/snapraid.conf", "/usr/local/etc/snapraid.conf")
RELEASE_HELPER = PROJECT_DIR / "bin" / "sf-snapraid"
TIMEOUTS = {"info": 120, "status": 240, "smart": 240, "probe": 240, "disks": 120, "guard": 4 * 3600,
            "list": 2 * 3600, "files": 120, "changes": 120, "job": 60, "log": 60}
POLL_SECONDS = 5.0           # the loop's beat; each read keeps its own interval
STOP_GRACE = 30
TONES = ("err", "warn", "ok", "muted")
WHATS = ("status", "health", "diff")
MAX_OUTSIDE = 50

# ---------------------------------------------------------------- the words on the page (novice; U7 checks them)

SENTENCES = (
    ("Disk protection can bring back a dead data disk, or deleted and damaged files, exactly as they were at its "
    "last run."),
    ("It can do that only until its next run, which makes deletions permanent, and it never had anything added "
    "since."),
    ("It keeps no older versions, sits on this same machine, covers only the disks listed below, and an "
    "administrator can delete it: it works beside your backups, never instead of them."),
)
WATCHING = "Seraphina is only watching: nothing is protected by it yet."
SANDBOX_LINE = "Your AI agents can read the names of your files while their sandbox is off."
NO_VERSIONED = ("Nothing Seraphina knows of keeps older versions of your files, or a copy away from this machine. "
                "Disk protection can do neither.")
NOT_COVERED = ("the system disk", "folders outside the protected disks", "folders the settings leave out",
               "every change made after the last protection run")
# A disk being taken out through its placeholder folder (design section 8.3 item 2) is listed under Not covered too
TAKING_OUT_COVERED = "{disk}, which is being taken out of protection"
# The lab checks of design section 13.2 this machine cannot run (they need a server started by systemd): "short" for
# the card's one sentence, "what" for Details and the doctor
LAB_CHECKS = (
    {"id": "L8", "short": "who can reach the reading program",
     "what": "only Seraphina's own user can reach the reading program, it can start nothing else as the "
             "administrator, and the AI agents' sandbox cannot reach it"},
    {"id": "L13", "short": "starting up with a failed disk", "what": "the server still starts when a disk has failed"},
    {"id": "L19", "short": "the reading program on every kind of server set-up",
     "what": "the reading program works with each kind of sudo this server may have, and from a hardened service"},
    {"id": "L20", "short": "replacing a failed disk",
     "what": "the checklist for a failed disk works end to end on test disks, and the next run takes the new disk in"},
)
LAB_LINE = ("Not checked on this server yet: " + ", ".join(c["short"] for c in LAB_CHECKS[:-1]) + " and " +
            LAB_CHECKS[-1]["short"] + ". Until they are, Seraphina only reads.")
LAB_WORDS = ("Not yet checked on a server started by systemd: " +
             "; ".join(f"{c['id']}, {c['what']}" for c in LAB_CHECKS) +
             ". Until they pass, Seraphina only reads.")
LAB_WHY = "needs a server started by systemd"


def _daemon_lab_words() -> str:
    from .snapraidd import LAB_CHECKS as DAEMON_LABS

    return ("Not yet checked against a real protection service and a server started by systemd: " +
            "; ".join(f"{c['id']}, {c['what']}" for c in DAEMON_LABS) + ".")


DAEMON_LAB_WORDS = _daemon_lab_words()
REASONS = {
    "disk_missing": "{disk} is not mounted where it should be.",
    "protection_missing": "{disk} is not mounted where it should be.",
    "disk_uuid_changed": "{disk} is mounted with a different filesystem than before.",
    "smart_fail": "{disk} says it is failing. Replace it soon.",
    "bad_blocks": "Hidden damage was found in {places}.",
    "content_problem": "The protection's file list has a problem.",
    "not_protected_days": "Nothing new has been protected for {days}.",
    "status_failed": "SnapRAID could not read its own state: {why}.",
    "never_protected": "Not protecting anything yet.",
    "outside": "A protection run was started outside Seraphina.",
    "interrupted": "The last run was stopped before it finished. It will carry on next time.",
    "old": "Protected up to {age}.",
    "check_overdue": "Part of the disks has not been checked for hidden damage for {days}.",
    "health_failed": "The disks' health could not be read at the last check.",
    "health_old": "The disks' health was last read {age}.",
    "health_unknown": "{disk}'s health can't be read.",
    "health_unread": "The disks' health has not been checked yet.",
    "failure_likely": "{disk} is more likely than usual to fail this year ({percent}%).",
    "almost_full": "The protection disk is almost full.",
    "list_room_low": "A disk that holds the protection's file list is almost full.",
    "ok": "Protected up to {when}.",
    "stale": "Last checked {age}.",
    "never_checked": "Not checked yet.",
    "unreachable": "Seraphina cannot reach the disk protection reader: {why}",
    "unsafe": "The disk protection reader refused to run: {why}",
    "not_set_up": "Disk protection is installed but not set up.",
    "absent": "Disk protection is not set up. It is optional: it rebuilds a dead data disk from the others.",
    "no_reader": "To show its health, Seraphina needs a small program that reads the disks for her.",
    "off": "Seraphina is not watching disk protection: it is turned off in the settings.",
    "taking_out": "{disk} is being taken out of protection.",
    "read_with_care": "SnapRAID {version} is read with care: some facts may be missing.",
}
# Why the reader's SnapRAID reads wait (section 8.1): the card keeps its last facts and says so
QUIET_WORDS = {"snapraid_running": "SnapRAID is running now",
               "unit_active": "your backup job is running",
               "unit_soon": "your backup job starts soon",
               "power_quiet": "the server is about to power off or has just started"}
HEALTH_TITLES = {"fail": "{disk} says it is failing", "unknown": "{disk}'s health can't be read"}
# SnapRAID's protection levels by config keyword (the reader's LEVELS): a protection disk is named by its level,
# never by the keyword ("1-parity" and "parity" are both level 1)
PARITY_LEVELS = {"parity": 1, "1-parity": 1, "2-parity": 2, "q-parity": 2, "3-parity": 3, "r-parity": 3, "z-parity": 3,
                 "4-parity": 4, "5-parity": 5, "6-parity": 6}
ROOM_DAYS = 30            # "almost full": below this many days of growth at the recent rate (design section 4.2)
ROOM_SAMPLES = 90         # one sample of the protection disk's free room per half day, kept for the rate
# The internal terms of the words table (design section 12.5): never on a novice screen (U7)
INTERNAL_TERMS = ("array", "parity", "sync", "scrub", "content file", "fix", "stripe", "helper", "held sync", "release")
INTERNAL_RE = re.compile(r"\b(?:" + "|".join(re.escape(term).replace(r"\ ", r"\s+") for term in INTERNAL_TERMS) +
                         r")\w*", re.IGNORECASE)


# ---------------------------------------------------------------- settings


def settings(config: Any) -> dict:
    """The snapraid section merged over DEFAULTS (config.py), with maintenance."""
    from .config import DEFAULTS

    raw = getattr(config, "raw", {}) or {}
    out = json.loads(json.dumps(DEFAULTS["snapraid"]))
    section = raw.get("snapraid") if isinstance(raw.get("snapraid"), dict) else {}
    _merge(out, section)
    maintenance = json.loads(json.dumps(DEFAULTS["maintenance"]))
    _merge(maintenance, raw.get("maintenance") if isinstance(raw.get("maintenance"), dict) else {})
    out["maintenance"] = maintenance
    return out


def _merge(base: dict, patch: dict) -> None:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        elif value is not None:
            base[key] = value


# ---------------------------------------------------------------- the helper client


class HelperError(Exception):
    """A call that got no answer: code (no_sudo, sudo_refused, timeout, unreadable, or the helper's own refusal code)
    and plain words."""

    def __init__(self, code: str, words: str, answer: dict | None = None) -> None:
        super().__init__(words)
        self.code = code
        self.words = words
        self.answer = answer or {}


Runner = Callable[[list[str], bytes, float], tuple[int | None, bytes, bytes]]


def run_command(argv: list[str], data: bytes, timeout: float) -> tuple[int | None, bytes, bytes]:
    """The default runner: argv with data on standard input, no shell, a minimal environment. A call past timeout
    gets SIGINT, then SIGTERM (sudo passes them on to the helper); never SIGKILL. (exit status, stdout, stderr);
    exit None when it did not end."""
    env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C", "LANG": "C"}
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd="/",
                            env=env)
    try:
        out, err = proc.communicate(data, timeout=timeout)
    except subprocess.TimeoutExpired:
        out, err = b"", b""
        for signum in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                proc.send_signal(signum)
            try:
                out, err = proc.communicate(timeout=STOP_GRACE)
                break
            except subprocess.TimeoutExpired:
                continue
        return None, out or b"", (err or b"") + b"\n(timed out)"
    return proc.returncode, out, err


def no_new_privs(path: str = "/proc/self/status") -> bool:
    """True when this process runs with NoNewPrivs set: sudo's setuid bit is then ignored and the helper cannot be
    reached (section 3.1)."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    match = re.search(r"^NoNewPrivs:\s*(\d)", text, re.MULTILINE)
    return bool(match and match.group(1) == "1")


class HelperClient:
    """``sudo -n <helper>`` with one JSON request line; the answer, or HelperError."""

    def __init__(self, helper: str = HELPER, runner: Runner | None = None, sudo: str = "sudo",
                 status_path: str = "/proc/self/status") -> None:
        self.helper = helper
        self.runner = runner or run_command
        self.sudo = sudo
        self.status_path = status_path

    def call(self, verb: str, timeout: float | None = None, **fields: Any) -> dict:
        request = json.dumps({"v": 1, "verb": verb, **fields}, separators=(",", ":")).encode("utf-8") + b"\n"
        argv = [self.sudo, "-n", self.helper]
        try:
            code, out, err = self.runner(argv, request, float(timeout or TIMEOUTS.get(verb, 120)))
        except FileNotFoundError:
            raise HelperError("no_sudo", "sudo is not installed on this machine.") from None
        except OSError as exc:
            raise HelperError("no_sudo", f"sudo could not be started ({type(exc).__name__}).") from None
        text = (out or b"").decode("utf-8", "replace").strip()
        answer = None
        if text:
            with contextlib.suppress(ValueError):
                answer = json.loads(text.splitlines()[-1])
        if not isinstance(answer, dict):
            errors = (err or b"").decode("utf-8", "replace")
            if code is None:
                waited = int(timeout or TIMEOUTS.get(verb, 120))
                raise HelperError("timeout", f"The reader did not answer within {waited} seconds; it is asked "
                                             f"again at the next check.")
            if "sudo:" in errors or "a password is required" in errors or "not allowed" in errors:
                cause = (" This process runs with NoNewPrivs set, which stops sudo. Run the harness from a unit "
                         "without NoNewPrivileges=yes: on a server, the harness's own system unit "
                         "(systemd/factory@.service), under its own user."
                         if no_new_privs(self.status_path) else
                         " Check that its sudoers line is installed (the setup page has the copy block).")
                raise HelperError("sudo_refused", "sudo would not run the reader." + cause)
            raise HelperError("unreadable", "The reader's answer could not be read" +
                              (f" (exit {code})." if code is not None else "."))
        if answer.get("ok") is not True:
            raise HelperError(str(answer.get("error") or "refused"), str(answer.get("words") or "Refused."), answer)
        return answer


# ---------------------------------------------------------------- the harness user's own view (no sudo)


def binary_path(which: Callable[[str], str | None] = shutil.which) -> str | None:
    """SnapRAID where the helper looks for it, else on this user's PATH (shown, never run as root)."""
    for path in BINARIES:
        if os.path.isfile(path):
            return path
    return which("snapraid")


def local_facts(run: Callable[[list[str]], str] | None = None) -> dict:
    """What the harness's user can read itself, as the Blueprint collector does: SnapRAID's version, which config
    files exist and whether /etc/snapraid.conf is readable, and the disk count it lists."""
    binary = binary_path()
    version = None
    if binary:
        output = (run or _run_version)([binary, "--version"])
        match = re.search(r"snapraid v([0-9]+(?:\.[0-9]+)?|none)\b", output or "")
        version = match.group(1) if match else None
    configs = {path: os.path.isfile(path) for path in CONFS}
    disks = None
    with contextlib.suppress(OSError, UnicodeDecodeError):
        text = Path(CONFS[0]).read_bytes().decode("utf-8")
        disks = sum(1 for line in text.split("\n") if re.match(r"^(data|disk)[ \t]+\S", line))
    return {"binary": binary, "version": version, "configs": configs, "readable_disks": disks}


def _run_version(argv: list[str]) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL, check=False,
                              env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def file_sha256(path: str | os.PathLike) -> str | None:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


# ---------------------------------------------------------------- words for times


def _when(stamp: Any) -> float | None:
    if isinstance(stamp, (int, float)):
        return float(stamp)
    if not isinstance(stamp, str) or not stamp:
        return None
    with contextlib.suppress(ValueError):
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    return None


def age_words(seconds: float) -> str:
    """"5 minutes ago", "3 hours ago", "2 days ago"."""
    seconds = max(0.0, seconds)
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{round(seconds / 60)} minutes ago"
    if seconds < 36 * 3600:
        hours = round(seconds / 3600)
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = round(seconds / 86400)
    return f"{days} day{'s' if days != 1 else ''} ago"


def when_words(at: float, now: float, zone: Any = None) -> str:
    """A past moment as the card says it after "Protected up to": "this morning at 08:21", "yesterday at 18:02",
    "Tuesday at 08:42", "12 Oct at 08:42" (in the owner's zone)."""
    local = datetime.fromtimestamp(at, timezone.utc).astimezone(zone) if zone else \
        datetime.fromtimestamp(at, timezone.utc)
    today = (datetime.fromtimestamp(now, timezone.utc).astimezone(zone) if zone else
             datetime.fromtimestamp(now, timezone.utc)).date()
    clock = local.strftime("%H:%M")
    if local.date() == today:
        part = "this morning" if local.hour < 12 else "this afternoon" if local.hour < 18 else "this evening"
        return f"{part} at {clock}"
    if local.date() == today - timedelta(days=1):
        return f"yesterday at {clock}"
    if today - local.date() < timedelta(days=7):
        return f"{local.strftime('%A')} at {clock}"
    return f"{local.day} {local.strftime('%b')} at {clock}"


def plural(count: int, word: str) -> str:
    return f"{count:,} {word}{'' if count == 1 else 's'}"


def size_words(count: float) -> str:
    """Bytes as the card says them: "1.2 TB", "640 GB" (decimal, as disks are sold)."""
    units = ("bytes", "KB", "MB", "GB", "TB", "PB")
    index, value = 0, float(max(0, count))
    while value >= 1000 and index < len(units) - 1:
        value /= 1000
        index += 1
    return f"{value:.1f} {units[index]}" if index and value < 10 else f"{round(value)} {units[index]}"


def disk_label(name: str, numbers: dict, levels: int = 1) -> str:
    """A disk as the card names it: "Disk 2" (data, by its number), "The protection disk" (the only one), or
    "Protection disk 2" (by its level, when there are more); never the config's name."""
    if name in numbers:
        return f"Disk {numbers[name]}"
    level = PARITY_LEVELS.get(name)
    if level is None:
        return "A disk"
    return "The protection disk" if level == 1 and levels <= 1 else f"Protection disk {level}"


def not_covered(facts: dict) -> list[str]:
    """The card's Not covered list: the fixed places, and every disk being taken out (design section 8.3 item 2),
    by its label, before the changes made after the last run."""
    status = facts.get("status") or {}
    numbers = status.get("numbers") or {}
    mounts = status.get("mounts") or {}
    levels = sum(1 for entry in mounts.values() if entry.get("role") == "parity")
    leaving = [TAKING_OUT_COVERED.format(disk=disk_label(name, numbers, levels))
               for name, entry in mounts.items() if entry.get("placeholder")]
    return [*NOT_COVERED[:-1], *leaving, NOT_COVERED[-1]]


def _lower_first(text: str) -> str:
    """A sentence's first letter lowered to go after other words, unless it starts with a name."""
    if text.startswith(("Seraphina", "SnapRAID")):
        return text
    return text[:1].lower() + text[1:]


# ---------------------------------------------------------------- the light (section 2.2)


def light(facts: dict, cfg: dict, now: float, *, outside_open: bool = False, zone: Any = None) -> dict:
    """{tone, code, reason, disk?, note?, last_known?}: the first rule that matches gives the light (design section
    4.2); its reason names the most specific cause. Red and amber found from the last facts stand even when those
    facts are old or the reader cannot be reached, with that said in note (a dying disk that makes every read time
    out must stay red). Grey only where the light would otherwise be green or unknown: the facts are older than
    stale_minutes ("Last checked 5 hours ago.", with last_known "unknown (last known: ...)"), or the reader cannot be
    reached, refuses or is not set up. Never green on stale facts. cfg: the snapraid settings."""
    status = facts.get("status") or {}
    observed = _when(status.get("at"))
    errors = facts.get("errors") or {}
    # A status error is cleared by the next status read, so one that is kept is the latest attempt's. An info error
    # alone counts only while no status was ever read (info fails first with the same cause when the reader refuses).
    error = errors.get("status") or (errors.get("info") if status.get("at") is None else None)
    stale_after = float(cfg.get("stale_minutes") or 180) * 60
    found = _rules(facts, cfg, now, outside_open, zone) if observed is not None else None
    grey = None
    if error:
        code = {"unsafe_install": "unsafe", "unsafe_conf": "unsafe", "not_set_up": "not_set_up"}.get(
            str(error.get("code")), "unreachable")
        grey = _tone("muted", code, why=error.get("words") or "")
    elif observed is not None and now - observed > stale_after:
        grey = _tone("muted", "stale", age=age_words(now - observed))
    if found is not None and found["tone"] in ("err", "warn"):
        return {**found, "note": grey["reason"]} if grey else found
    if grey is not None:
        if found is not None:
            grey["last_known"] = f"unknown (last known: {_lower_first(found['reason']).rstrip('.')})"
        return grey
    return found or _tone("muted", "never_checked")


def _tone(tone: str, code: str, **values: Any) -> dict:
    return {"tone": tone, "code": code, "reason": REASONS[code].format(**values)}


def _rules(facts: dict, cfg: dict, now: float, outside_open: bool, zone: Any) -> dict:
    limits = cfg.get("light") or {}
    status = facts.get("status") or {}
    numbers = status.get("numbers") or {}
    mounts = status.get("mounts") or {}
    state = status.get("facts") or {}
    smart = (facts.get("smart") or {}).get("disks") or {}
    content = status.get("content") or {}
    levels = sum(1 for entry in mounts.values() if entry.get("role") == "parity") or \
        sum(1 for name in smart if name in PARITY_LEVELS)

    def label(name: str) -> str:
        return disk_label(name, numbers, levels)
    # red
    for name, entry in mounts.items():
        if not entry.get("mounted") and not entry.get("placeholder"):
            code = "protection_missing" if entry.get("role") == "parity" else "disk_missing"
            return {**_tone("err", code, disk=label(name)), "disk": numbers.get(name)}
    for name, entry in mounts.items():
        if entry.get("mounted") and not entry.get("uuid_ok") and not entry.get("placeholder"):
            return {**_tone("err", "disk_uuid_changed", disk=label(name)), "disk": numbers.get(name)}
    for name, entry in smart.items():
        if entry.get("health") in ("fail", "prefail"):
            return {**_tone("err", "smart_fail", disk=label(name)), "disk": numbers.get(name)}
    bad = (state.get("has_bad") or {}).get("count") or 0
    if bad:
        return _tone("err", "bad_blocks", places=plural(bad, "place"))
    if content.get("problem") and content.get("other"):
        return _tone("err", "content_problem")
    last = state.get("newest_block")
    may_change = _may_have_changed(facts, last, now, float(cfg.get("stale_minutes") or 180) * 60)
    empty = state.get("array_empty")
    if not empty and last and may_change and now - last > float(limits.get("red_after_days", 7)) * 86400:
        return _tone("err", "not_protected_days", days=plural(int((now - last) // 86400), "day"))
    # amber
    failed = status.get("failed")
    if failed:
        why = re.sub(r"^Failed: ", "", str(failed.get("words") or OUTCOME_UNEXPECTED)).rstrip(".")
        return _tone("warn", "status_failed", why=_lower_first(why))
    if empty:
        return _tone("warn", "never_protected")
    if outside_open:
        return _tone("warn", "outside")
    for name, entry in mounts.items():
        if entry.get("placeholder"):
            return {**_tone("warn", "taking_out", disk=label(name)), "disk": numbers.get(name)}
    version = ((facts.get("info") or {}).get("snapraid") or {})
    if version.get("read_only") and version.get("version"):
        return _tone("warn", "read_with_care", version=version["version"])
    if (state.get("has_unsynced") or 0) > 0:
        return _tone("warn", "interrupted")
    if last and may_change and now - last > float(limits.get("amber_after_hours", 36)) * 3600:
        return _tone("warn", "old", age=age_words(now - last))
    target = float(limits.get("scrub_target_days") or ((cfg.get("schedule") or {}).get("scrub") or {})
                   .get("target_days") or 30) * 86400
    oldest = state.get("oldest_scrubbed")
    histogram = state.get("histogram") or []
    first = min((row.get("time") for row in histogram if row.get("time")), default=None)
    unchecked = state.get("unscrubbed_blocks") or 0
    if oldest and now - oldest > target + 15 * 86400:
        return _tone("warn", "check_overdue", days=plural(int((now - oldest) // 86400), "day"))
    if unchecked and first and now - first > target:
        return _tone("warn", "check_overdue", days=plural(int((now - first) // 86400), "day"))
    health = health_state(facts, cfg, now)
    if health == "unread" and numbers:
        return _tone("warn", "health_unread")
    if health == "failed":
        return _tone("warn", "health_failed")
    if health == "old":
        return _tone("warn", "health_old", age=age_words(now - (_when((facts.get("smart") or {}).get("at")) or now)))
    accepted = set(limits.get("smart_unreadable_ok") or [])
    for name, entry in smart.items():
        if entry.get("health") == "unknown" and name not in accepted:
            return {**_tone("warn", "health_unknown", disk=label(name)), "disk": numbers.get(name)}
    threshold = float(limits.get("fp_amber_percent", 20)) / 100
    for name, entry in smart.items():
        if isinstance(entry.get("probability"), (int, float)) and entry["probability"] >= threshold:
            return {**_tone("warn", "failure_likely", disk=label(name), percent=round(entry["probability"] * 100)),
                    "disk": numbers.get(name)}
    free, total = state.get("parity_block_free_min"), _parity_total(state)
    days_left = room_days(facts)
    if free is not None and total and (free / total < 0.05 or (days_left is not None and days_left < ROOM_DAYS)):
        return _tone("warn", "almost_full")
    if content.get("space_low"):
        return _tone("warn", "list_room_low")
    if not last:
        return _tone("warn", "never_protected")
    return _tone("ok", "ok", when=when_words(last, now, zone))


OUTCOME_UNEXPECTED = "it ended in an unexpected way"


def quiet_reason(cfg: dict, config: Any, now: float, proc: str = "/proc",
                 run: Callable[[list[str]], str] | None = None) -> str | None:
    """Section 8.1's waits, as the harness's own user can see them: a snapraid process (any /proc/<pid>/comm), a
    unit of snapraid.quiet_units active or its timer's next start within quiet_margin_minutes (systemctl, no
    root), or the power schedule's quiet periods and power-off. A key of QUIET_WORDS, or None."""
    with contextlib.suppress(OSError):
        for pid in os.listdir(proc):
            if pid.isdigit():
                with contextlib.suppress(OSError):
                    if Path(proc, pid, "comm").read_text(encoding="utf-8", errors="replace").strip() == "snapraid":
                        return "snapraid_running"
    runner = run or _systemctl
    margin = float(cfg.get("quiet_margin_minutes", 30)) * 60
    for unit in cfg.get("quiet_units") or []:
        if runner(["systemctl", "is-active", unit]).strip() in ("active", "activating", "reloading"):
            return "unit_active"
        timer = unit.rsplit(".", 1)[0] + ".timer"
        text = runner(["systemctl", "show", "-p", "NextElapseUSecRealtime", "--value", timer]).strip()
        with contextlib.suppress(ValueError):
            when = datetime.strptime(" ".join(text.split()[1:3]), "%Y-%m-%d %H:%M:%S").timestamp() if text else None
            if when is not None and 0 <= when - now <= margin:
                return "unit_soon"
    from .clock import LocalClock, PowerSchedule

    wakes = (getattr(config, "raw", {}) or {}).get("wakes") or {}
    with contextlib.suppress(ValueError, TypeError, AttributeError):
        power = PowerSchedule.from_config(wakes.get("power"))
        if power is not None:
            local = datetime.fromtimestamp(now, timezone.utc).astimezone(LocalClock(wakes.get("timezone")).zone)
            if power.state(local) != "on":
                return "power_quiet"
    return None


def _systemctl(argv: list[str]) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL,
                              check=False, env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def health_state(facts: dict, cfg: dict, now: float) -> str | None:
    """Whether the disks' health facts can be used: "unread" (never read), "failed" (the latest read got no answer:
    a disk whose smartctl call hangs is exactly when), "old" (read more than twice refresh.health_hours ago), or
    None (fresh). Old or failed health is never "healthy"."""
    smart = facts.get("smart") or {}
    if (facts.get("errors") or {}).get("smart"):
        return "failed"
    at = _when(smart.get("at"))
    if at is None:
        return "unread"
    hours = float((cfg.get("refresh") or {}).get("health_hours") or 24)
    return "old" if now - at > 2 * hours * 3600 else None


def _parity_total(state: dict) -> int | None:
    totals = [entry.get("total") for entry in (state.get("parity") or {}).values() if entry.get("total")]
    return min(totals) if totals else None


def room_days(facts: dict) -> float | None:
    """How many days of growth the protection disk still has room for, at the rate of the last 30 days' samples
    (facts.room: [time, free blocks], one per half day); None while there is no rate to go by (under a day of
    samples, or no growth)."""
    samples = [row for row in facts.get("room") or [] if isinstance(row, list) and len(row) == 2]
    if len(samples) < 2:
        return None
    newest = samples[-1]
    window = [row for row in samples if newest[0] - row[0] <= ROOM_DAYS * 86400]
    oldest = window[0]
    days = (newest[0] - oldest[0]) / 86400
    if days < 1 or oldest[1] <= newest[1]:
        return None
    return newest[1] / ((oldest[1] - newest[1]) / days)


def _may_have_changed(facts: dict, last_sync: float | None, now: float, fresh: float) -> bool:
    """Files changed comes only from a diff (the latest guard report): with no diff since the last run, or one older
    than fresh (stale_minutes: a month-old "nothing changed" says nothing about the files now), it is unknown, and
    unknown counts as "may have changed"."""
    guard = facts.get("guard") or {}
    at = _when(guard.get("at"))
    if at is None or (last_sync and at < last_sync) or now - at > fresh:
        return True
    counts = (guard.get("counts") or {}).get("array") or {}
    return guard.get("verdict") != "nothing_to_do" and bool(counts.get("changed", 1))


# ---------------------------------------------------------------- the panel


class SnapraidPanel:
    """The disk protection panel. config: the harness config (snapraid, maintenance, wakes, harness sections);
    system: the HousekeepingManager (the system scope's writer, for findings); notifier: the Notifier (disk.failing);
    bus: the EventBus; client: a HelperClient (tests give one with a fake runner); local: what the harness's user reads
    itself (local_facts); clock: time.time."""

    def __init__(self, config: Any, system: Any = None, notifier: Any = None, bus: Any = None,
                 client: HelperClient | None = None, local: Callable[[], dict] | None = None,
                 clock: Callable[[], float] = time.time, metrics: Any = None, link: Any = None,
                 quiet: Callable[[dict], str | None] | None = None, proc_root: str = "/") -> None:
        self.config = config
        self.system = system
        self.notifier = notifier
        self.bus = bus
        self._client = client
        self.local = local or local_facts
        self.clock = clock
        self._lock = threading.Lock()
        self._busy = threading.Lock()
        self.refreshing: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._disks: dict | None = None      # the disks verb's answer (models, serials): memory only, never on disk
        self._local: tuple[float, dict] | None = None
        from .snapraidd import DaemonRoute

        self.daemon = DaemonRoute(self, metrics=metrics, link=link, proc_root=proc_root)
        self._quiet = quiet
        from .snapraid_rebuild import Checklist

        self.rebuild = Checklist(self)        # the owner's checklist for a whole failed disk (P4, design section 9.4)

    # -- lifecycle ------------------------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="snapraid", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(max(0.0, timeout))

    def _loop(self) -> None:
        while not self._stop.wait(POLL_SECONDS):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - a refresh's trouble is in the facts' errors, never a dead thread
                continue

    def tick(self) -> list[str]:
        """Run what is due. On the daemon route: the daemon's reads (snapraidd.DaemonRoute.tick), and the reader's
        info (never a SnapRAID command) every refresh.status_minutes. On the reader's route: status (and info) every
        refresh.status_minutes, health every refresh.health_hours, each only when none of section 8.1's waits
        applies. Nothing while the panel is off, or SnapRAID and the reader are absent. Returns what ran."""
        if not self.settings().get("enabled"):
            return []
        cfg = self.settings()
        facts = self.facts()
        now = self.clock()
        if self.route() in ("daemon", "metrics"):
            ran = self.daemon.tick()
            last = _when((facts.get("info_tried"))) or _when((facts.get("info") or {}).get("at"))
            if self.reader_present() and (last is None or now - last >= float(cfg["refresh"]["status_minutes"]) * 60):
                self._refresh_info()
                ran.append("info")
            return ran
        if self.state() != "ready":
            return []
        last_status = _when((facts.get("status") or {}).get("tried")) or _when((facts.get("status") or {}).get("at"))
        last_health = _when((facts.get("smart") or {}).get("tried")) or _when((facts.get("smart") or {}).get("at"))
        due = [what for what, last, every in (("status", last_status, float(cfg["refresh"]["status_minutes"]) * 60),
                                              ("health", last_health, float(cfg["refresh"]["health_hours"]) * 3600))
               if last is None or now - last >= every]
        if not due:
            return []
        why = self.quiet_reason()
        if why is not None:
            facts["waiting"] = {"at": _iso(now), "why": why}
            self._save(facts)
            return []
        if facts.pop("waiting", None) is not None:
            self._save(facts)
        for what in due:
            self.refresh(what)
        return due

    # -- reading ------------------------------------------------------------------------------------------------

    def settings(self) -> dict:
        return settings(self.config)

    def client(self) -> HelperClient:
        return self._client or HelperClient(str(self.settings().get("helper") or HELPER))

    def folder(self) -> Path:
        return Path(self.config.data_dir) / DIR

    def facts(self) -> dict:
        try:
            data = json.loads((self.folder() / "facts.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"version": 1}
        return data if isinstance(data, dict) else {"version": 1}

    def local_view(self) -> dict:
        """local_facts, at most once a minute."""
        now = time.monotonic()
        if self._local is None or now - self._local[0] > 60:
            self._local = (now, self.local())
        return self._local[1]

    def reader_present(self) -> bool:
        return self._client is not None or os.path.isfile(str(self.settings().get("helper") or HELPER))

    def state(self) -> str:
        """absent (no SnapRAID and no daemon), no_reader (SnapRAID without the helper, and no daemon), off
        (snapraid.enabled false) or ready."""
        local = self.local_view()
        daemon = self.daemon.installed()
        if not local.get("binary") and not daemon:
            return "absent"
        if not self.reader_present() and not daemon:
            return "no_reader"
        if not self.settings().get("enabled"):
            return "off"
        return "ready"

    def route(self) -> str:
        """daemon (the daemon answers and Seraphina's link is installed), metrics (the daemon answers, no link yet),
        reader (P1's read-only reader) or none."""
        if self.daemon.installed():
            return "daemon" if self.daemon.link() is not None else "metrics"
        return "reader" if self.reader_present() and self.local_view().get("binary") else "none"

    def quiet_reason(self) -> str | None:
        """Section 8.1's waits for the reader's SnapRAID reads: a snapraid process runs, a unit of
        snapraid.quiet_units is active or its timer starts within quiet_margin_minutes, or the power schedule's
        quiet periods. A key of QUIET_WORDS, or None."""
        if self._quiet is not None:
            return self._quiet(self.settings())
        return quiet_reason(self.settings(), self.config, self.clock())

    def _refresh_info(self) -> None:
        """The reader's info only (the daemon route): its inventory, mounts and the daemon config summary."""
        facts = self.facts()
        facts["info_tried"] = _iso(self.clock())
        info = self._ask(self.client(), "info", facts)
        if info is not None:
            facts["info"] = _info_summary(info)
        self._save(facts)

    # -- refreshing --------------------------------------------------------------------------------------------

    def refresh(self, what: str = "status", wait: bool = True) -> dict:
        """A fresh read now: status (info, status, and the file list after a run outside), health (smart, probe,
        disks) or diff (the guard report). With wait False it runs in the background ({refreshing}); one at a time
        (busy otherwise). Every call only reads (DESIGN §40): a failed one is recorded and tried at the next check."""
        if what not in WHATS:
            raise ValueError(f"what must be one of {', '.join(WHATS)}")
        if self.route() in ("daemon", "metrics") and self.settings().get("enabled"):
            self.daemon.report_event(None, None)             # read the daemon now; the reader runs no SnapRAID
            if self.reader_present():
                self._refresh_info()
            return self.view()
        state = self.state()
        if state != "ready":
            raise ValueError({"absent": "SnapRAID is not installed on this machine",
                              "no_reader": "the reading program is not installed yet (see the setup page)",
                              "off": "disk protection is turned off in the settings (snapraid.enabled)"}[state])
        if not self._busy.acquire(blocking=False):
            return {"refreshing": self.refreshing, "busy": True}
        self.refreshing = what
        if not wait:
            threading.Thread(target=self._refresh_then_release, args=(what,), name=f"snapraid-{what}",
                             daemon=True).start()
            return {"refreshing": what}
        try:
            self._refresh(what)
        finally:
            self.refreshing = None
            self._busy.release()
        return self.view()

    def _refresh_then_release(self, what: str) -> None:
        try:
            self._refresh(what)
        except Exception:  # noqa: BLE001 - recorded in the facts by _refresh where it could be
            pass
        finally:
            self.refreshing = None
            self._busy.release()
            self._publish()

    def _refresh(self, what: str) -> None:
        client = self.client()
        before = self.view_light()
        facts = self.facts()
        now = self.clock()
        if what == "status":
            info = self._ask(client, "info", facts)
            if info is not None:
                facts["info"] = _info_summary(info)
            facts.setdefault("status", {})["tried"] = _iso(now)
            status = self._ask(client, "status", facts)
            if status is not None and status.get("outcome") != "busy":
                facts["status"] = merge_status(facts.get("status") or {}, status, _iso(now))
                self._outside(facts, status.get("fingerprint") or {}, status.get("job"))
                if status.get("outcome") == "ok":
                    _sample_room(facts, status, now)
                    self._index(client, facts, status)
            if status is not None:
                probe = self._ask(client, "probe", facts)       # power states, without waking a disk
                if probe is not None:
                    facts["probe"] = {"at": probe.get("at"), "disks": probe.get("disks") or {}}
        elif what == "health":
            facts.setdefault("smart", {})["tried"] = _iso(now)
            smart = self._ask(client, "smart", facts)
            disks = self._ask(client, "disks", facts)
            if disks is not None:
                self._disks = disks
            if smart is not None:
                facts["smart"] = {**_smart_summary(smart), "tried": _iso(now)}
                self._health(facts, smart)
        else:
            guard = self._ask(client, "guard", facts)
            if guard is not None:
                facts["guard"] = _guard_summary(guard)
                self._journal("guard", f"guard report: {guard.get('verdict')}"
                              + (f" ({', '.join(guard.get('reasons') or [])})" if guard.get("reasons") else "")
                              + f"; {((guard.get('counts') or {}).get('array') or {}).get('lost', 0)} file(s) would "
                                f"be lost", guard=guard.get("guard"), reasons=guard.get("reasons") or [],
                              verdict=guard.get("verdict"))
                if self.bus is not None:
                    self.bus.publish("snapraid.guard", {"guard": guard.get("guard"), "state": "reported",
                                                        "verdict": guard.get("verdict"),
                                                        "reasons": guard.get("reasons") or [],
                                                        "lost": ((guard.get("counts") or {}).get("array") or {})
                                                        .get("lost")})
        self._save(facts)
        if self.view_light() != before:
            self._publish()

    def _ask(self, client: HelperClient, verb: str, facts: dict) -> dict | None:
        """One read; its failure is kept under facts.errors[verb] (cleared by the next success). A read changes
        nothing outside the harness (DESIGN §40), so a failed one is simply asked again at the next check."""
        errors = facts.setdefault("errors", {})
        try:
            answer = client.call(verb)
        except HelperError as exc:
            if exc.code == "waiting":                  # a snapraid process runs: the card keeps its last facts
                facts["waiting"] = {"at": _iso(self.clock()), "why": "snapraid_running"}
                return None
            errors[verb] = {"at": _iso(self.clock()), "code": exc.code, "words": exc.words}
            return None
        errors.pop(verb, None)
        return answer

    def _index(self, client: HelperClient, facts: dict, status: dict) -> None:
        """The file list after a run outside Seraphina (or once at first), while status shows nothing unfinished:
        the helper's root-side index of the last good run, which the guard and the cockpit's browsing use."""
        state = status.get("facts") or {}
        changed = (status.get("fingerprint") or {}).get("changed_now")
        if state.get("array_empty") or state.get("has_unsynced") or not (changed or not facts.get("list")):
            return
        answer = self._ask(client, "list", facts)
        if answer is not None:
            facts["list"] = {"at": answer.get("at"), "job": answer.get("job"), "good": answer.get("good"),
                             "files": sum((answer.get("files") or {}).values())}

    # -- findings, notices, the journal -----------------------------------------------------------------------

    def _health(self, facts: dict, smart: dict) -> None:
        """Health findings (section 4.7): one per disk number, opened by a failing or unreadable disk, closed only
        by a fresh, readable observation of that disk; a smart run that read nothing changes nothing."""
        disks = smart.get("disks") or {}
        readable = smart.get("outcome") == "ok" or any(entry.get("health") in ("fail", "prefail")
                                                       for entry in disks.values())
        if not readable or self.system is None:
            return
        numbers = smart.get("numbers") or {}
        levels = sum(1 for name in disks if name in PARITY_LEVELS)
        accepted = set((self.settings().get("light") or {}).get("smart_unreadable_ok") or [])
        seen = facts.setdefault("health_seen", {})
        opened, closed = [], []
        with self.system.transaction() as ledger:
            for name, entry in disks.items():
                key = _health_key(name, numbers)
                label = disk_label(name, numbers, levels)
                health = entry.get("health")
                dedupe = f"snapraid:health:{key}"
                item = _open_item(ledger, dedupe)
                if health == "ok":
                    if item is not None:
                        ledger.change(tracker.FACTORY, item["id"], "verified",
                                      note=f"a fresh health check reads {_lower_first(label)} healthy",
                                      evidence=[{"kind": "receipt", "ref": f"snapraid:{smart.get('job')}", "check": "ok",
                                                 "detail": "a fresh, readable SMART observation"}])
                        closed.append(ledger.scope.item_ref(item["id"]))
                    if self.notifier is not None:
                        with contextlib.suppress(Exception):
                            self.notifier.parity_clear("disk.failing", f"disk-{key}")
                elif health in ("fail", "prefail"):
                    fields = _health_fields("fail", label, entry, smart.get("at"), key)
                    if item is None:
                        item, _ = tracker.open_once(ledger, fields, dedupe)
                        opened.append(ledger.scope.item_ref(item["id"]))
                    elif not item["title"].endswith("says it is failing"):
                        ledger.retitle(item["id"], fields["title"], detail=fields["detail"], note="it now fails")
                    if seen.get(name) not in ("fail", "prefail") and self.notifier is not None:
                        with contextlib.suppress(Exception):
                            self.notifier.parity_event("disk.failing", {}, f"disk-{key}")
                elif health == "unknown" and item is None and name not in accepted:
                    item, _ = tracker.open_once(ledger, _health_fields("unknown", label, entry, smart.get("at"), key),
                                                dedupe)
                    opened.append(ledger.scope.item_ref(item["id"]))
                seen[name] = health
        if opened or closed:
            self._journal("health", f"health check: {len(opened)} finding(s) opened, {len(closed)} closed",
                          job=smart.get("job"), opened=opened, closed=closed)

    def _outside(self, facts: dict, fingerprint: dict, job: Any) -> None:
        """A run outside Seraphina (section 8.5): the content file changed with no job of the helper's (in P1 it
        runs none). Always a finding (snapraid:outside, kept open and updated while it is open) and the amber line,
        whether or not another scheduler of SnapRAID was found: such a run did not pass Seraphina's checks either
        way. The finding names how many schedulers were found, never which."""
        known = {entry.get("seen_at") for entry in facts.get("outside") or []}
        new = [entry for entry in fingerprint.get("outside") or [] if entry.get("seen_at") not in known]
        if not new:
            return
        facts["outside"] = (list(facts.get("outside") or []) + new)[-MAX_OUTSIDE:]
        schedulers = ((facts.get("info") or {}).get("schedulers") or {}).get("found") or []
        modified = new[-1].get("modified")
        self._journal("outside", f"the protection's file list changed outside Seraphina (modified {modified})",
                      job=job, schedulers=len(schedulers))
        if self.system is None:
            return
        count = len(schedulers)
        who = (f"{plural(count, 'other program')} on this machine {'starts' if count == 1 else 'start'} SnapRAID on "
               f"a timetable (Backups, Who runs it): this run may be {'its' if count == 1 else 'one of theirs'}"
               if count else "no other program on this machine starts SnapRAID on a timetable")
        fields = {"title": "SnapRAID ran outside Seraphina", "kind": "finding",
                  "detail": f"The protection's file list changed (modified {modified}) without a run by Seraphina, "
                            f"and {who}. Such a run did not pass Seraphina's checks. If you ran it yourself, you can "
                            f"drop this item.",
                  "source": {"type": "snapraid", "key": "outside", "observed_at": new[-1].get("seen_at")}}
        with self.system.transaction() as ledger:
            item = _open_item(ledger, "snapraid:outside")
            if item is None:
                tracker.open_once(ledger, fields, "snapraid:outside")
            else:
                ledger.retitle(item["id"], fields["title"], detail=fields["detail"], note="it ran outside again")

    def outside_open(self) -> bool:
        if self.system is None:
            return False
        try:
            ledger = self.system.store().ledger()
        except Exception:  # noqa: BLE001 - an unreadable tracker shows no amber line, never an error page
            return False
        return _open_item(ledger, "snapraid:outside") is not None

    def _journal(self, kind: str, summary: str, **fields: Any) -> None:
        """One line of <data_dir>/snapraid/journal-YYYY-MM.jsonl (0600): counts, codes, ids and item refs, never a
        file name or a disk's SnapRAID name. The operator's activity query lists them (source snapraid)."""
        at = _iso(self.clock())
        line = {"at": at, "kind": kind, "summary": summary,
                **{key: value for key, value in fields.items() if value not in (None, [], {})}}
        folder = self.folder()
        with contextlib.suppress(OSError, TypeError, ValueError):
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(folder / f"journal-{at[:7]}.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n")

    def _save(self, facts: dict) -> None:
        facts["version"] = 1
        _write_private(self.folder(), "facts.json", facts)

    def _publish(self) -> None:
        if self.bus is not None:
            lit = self.view_light()
            self.bus.publish("snapraid.updated", {"tone": lit.get("tone"), "code": lit.get("code"),
                                                  "observed_at": (self.facts().get("status") or {}).get("at"),
                                                  "refreshing": self.refreshing})

    # -- the view (GET /api/snapraid) ----------------------------------------------------------------------------

    def view_light(self) -> dict:
        rebuild = self.rebuild.light() if self.settings().get("enabled") else None
        if rebuild is not None:             # a disk is being replaced: the checklist's light, on either route
            return rebuild
        if self.route() in ("daemon", "metrics") and self.settings().get("enabled"):
            from . import snapraidd

            daemon_view, actions = self.daemon.view()
            unknown = any(a.get("outcome") == "unknown" and a.get("state") == "sent" for a in actions)
            return snapraidd.light(daemon_view, self.settings(), self.clock(), unknown_open=unknown,
                                   zone=self._zone())
        state = self.state()
        if state != "ready":
            code = {"absent": "absent", "no_reader": "no_reader", "off": "off"}[state]
            return _tone("muted", code)
        facts = self.facts()
        errors = facts.get("errors") or {}
        unset = (errors.get("status") or {}).get("code") == "not_set_up" or \
            (facts.get("info") or {}).get("set_up") is False
        if unset and not (errors.get("info") or {}).get("code"):
            return _tone("muted", "not_set_up")
        return light(facts, self.settings(), self.clock(), outside_open=self.outside_open(), zone=self._zone())

    def _zone(self) -> Any:
        from .clock import LocalClock

        wakes = (getattr(self.config, "raw", {}) or {}).get("wakes") or {}
        try:
            return LocalClock(wakes.get("timezone") if isinstance(wakes, dict) else None).zone
        except Exception:  # noqa: BLE001 - an unknown zone: UTC
            return None

    def view(self) -> dict:
        """GET /api/snapraid: {phase, installed, helper, enabled, state, allowed, light, sentences, undo_line,
        facts {status, diff, smart, observed_at, stale}, disks, job, guard, schedule, scrub_plan, not_covered,
        schedulers, outside, warnings, lab, versioned, refreshing}. Counts and codes; the config's own paths."""
        cfg = self.settings()
        state = self.state()
        local = self.local_view()
        facts = self.facts() if state == "ready" else {}
        info = facts.get("info") or {}
        status = facts.get("status") or {}
        lit = self.view_light()
        now = self.clock()
        observed = _when(status.get("at"))
        release = file_sha256(RELEASE_HELPER)
        installed = info.get("helper_sha256")
        route = self.route()
        daemon_view, actions = self.daemon.view() if route in ("daemon", "metrics") else (None, [])
        return {
            "phase": PHASE, "state": state, "installed": bool(local.get("binary")) or route in ("daemon", "metrics"),
            "route": route, "daemon": daemon_view, "actions": actions,
            "undo_line": self._undo_line(route, daemon_view, now),
            "waiting": facts.get("waiting"), "waiting_words": QUIET_WORDS.get((facts.get("waiting") or {}).get("why")),
            "version_note": (info.get("snapraid") or {}).get("note"),
            "snapraid": {"binary": local.get("binary"), "version": (info.get("snapraid") or {}).get("version")
                         or local.get("version"), "configs": local.get("configs"),
                         "readable_disks": local.get("readable_disks")},
            "helper": {"path": str(cfg.get("helper") or HELPER), "installed": state not in ("absent", "no_reader"),
                       "sha256": installed, "release_sha256": release,
                       "matches_release": None if not installed else installed == release,
                       "error": (facts.get("errors") or {}).get("info") or (facts.get("errors") or {}).get("status")},
            "enabled": bool(cfg.get("enabled")), "allowed": list(info.get("allow") or ["read"]),
            "light": lit, "sentences": list(SENTENCES),
            "facts": {"status": status.get("facts"), "observed_at": status.get("at"),
                      "facts_at": status.get("facts_at"), "failed": status.get("failed"),
                      "stale": observed is None or now - observed > float(cfg.get("stale_minutes") or 180) * 60,
                      "diff": (facts.get("guard") or {}).get("counts"), "smart": facts.get("smart"),
                      "last_run": {"at": _iso(status["facts"]["newest_block"]), "inferred": True}
                      if (status.get("facts") or {}).get("newest_block") else None,
                      "list": facts.get("list"), "words": fact_words(facts, cfg, now)},
            "disks": self._disk_rows(facts), "job": None, "guard": facts.get("guard"),
            "schedule": None, "scrub_plan": None, "not_covered": not_covered(facts),
            "set_up": info.get("set_up"), "conf_ok": info.get("conf_ok"), "conf_problems": info.get("conf_problems")
            or [], "schedulers": (info.get("schedulers") or {}).get("found") or [],
            "outside": (facts.get("outside") or [])[-5:], "warnings": self._warnings(cfg),
            "lab": [{**check, "status": "not_run", "why": LAB_WHY} for check in LAB_CHECKS],
            "lab_line": LAB_LINE, "lab_words": LAB_WORDS,
            "versioned": {"set_up": False, "words": NO_VERSIONED},
            "refreshing": self.refreshing, "errors": facts.get("errors") or {},
            "rebuild": self.rebuild.summary() if cfg.get("enabled") else None,
        }

    def _undo_line(self, route: str, daemon_view: dict | None, now: float) -> str:
        """Section 1.1's line under the sentences: when the chance to undo a deletion ends."""
        if route not in ("daemon", "metrics") or not daemon_view:
            return WATCHING
        window = (daemon_view.get("window") or {}).get("words")
        if daemon_view.get("hold_off") and window:
            return f"{window[:1].upper()}{window[1:]}'s run will be skipped. A restart of the protection service " \
                   f"undoes this."
        if window:
            return f"Deleted something by mistake? It can be brought back until the next protection run: {window}."
        return "Deleted something by mistake? It can be brought back until the next protection run, which nothing " \
               "has scheduled."

    def _disk_rows(self, facts: dict) -> list[dict]:
        status = facts.get("status") or {}
        numbers = status.get("numbers") or {}
        mounts = status.get("mounts") or {}
        disks = (status.get("facts") or {}).get("disks") or {}
        smart = (facts.get("smart") or {}).get("disks") or {}
        power = (facts.get("probe") or {}).get("disks") or {}
        block = (status.get("facts") or {}).get("block_size") or 0
        identity = {row.get("name"): row for row in (self._disks or {}).get("disks") or []}
        levels = sum(1 for entry in mounts.values() if entry.get("role") == "parity")
        rows = []
        for name, mount in mounts.items():
            entry = disks.get(name) or {}
            health = smart.get(name) or {}
            ident = identity.get(name) or {}
            rows.append({"name": name, "number": numbers.get(name) if mount.get("role") == "data" else None,
                         "level": PARITY_LEVELS.get(name) if mount.get("role") == "parity" else None,
                         "label": disk_label(name, numbers, levels),
                         "role": mount.get("role"), "mount": mount.get("path"), "mounted": mount.get("mounted"),
                         "uuid_ok": mount.get("uuid_ok"), "files": entry.get("files"),
                         "free_bytes": entry.get("free_blocks") * block if entry.get("free_blocks") is not None
                         and block else None, "health": health.get("health"),
                         "probability": health.get("probability"), "power": power.get(name),
                         "model": ident.get("model"), "serial": ident.get("serial"), "by_id": ident.get("by_id")})
        return rows

    def _warnings(self, cfg: dict) -> list[dict]:
        from .notify import sandbox_mode, uses_cli_driver

        out = []
        if cfg.get("enabled") and (sandbox_mode(self.config) != "bwrap" or uses_cli_driver(self.config)):
            out.append({"code": "agents_unsandboxed", "words": SANDBOX_LINE})
        return out

    # -- the cockpit's pages ------------------------------------------------------------------------------------

    def guard_page(self, guard: str, *, disk: str | None = None, kind: str | None = None, folder: str | None = None,
                   q: str | None = None, offset: int = 0) -> dict:
        """GET /api/snapraid/guard/<guard>: the guard record (counts) and one page of its change list, live from
        the helper (the harness keeps no copy of names)."""
        fields = {key: value for key, value in (("disk", disk), ("kind", kind), ("dir", folder), ("q", q))
                  if value not in (None, "")}
        page = self._call("changes", guard=guard, offset=int(offset or 0), **fields)
        summary = _guard_summary(page.get("report") or {"guard": guard})
        return {"guard": summary, "page": {key: page.get(key) for key in ("total", "offset", "entries", "groups",
                                                                           "list_sha256")}}

    def files_page(self, *, disk: str | None = None, folder: str | None = None, q: str | None = None,
                   offset: int = 0) -> dict:
        fields = {key: value for key, value in (("disk", disk), ("dir", folder), ("q", q)) if value is not None}
        page = self._call("files", offset=int(offset or 0), **fields)
        return {key: page.get(key) for key in ("index", "total", "offset", "entries", "folders")}

    def jobs(self) -> dict:
        return {"jobs": self._call("job").get("jobs") or []}

    def job(self, job: str, *, log: bool = False, offset: int = 0) -> dict:
        record = self._call("job", job=job).get("job")
        out = {"job": record}
        if log:
            text = self._call("log", job=job, text=True, offset=int(offset or 0))
            out["log"] = {key: text.get(key) for key in ("text", "offset", "next_offset", "size", "more")}
        return out

    def _call(self, verb: str, **fields: Any) -> dict:
        if self.state() != "ready":
            raise ValueError("disk protection is not being read (see its card)")
        try:
            return self.client().call(verb, **fields)
        except HelperError as exc:
            if exc.code == "no_record":
                raise KeyError(exc.words) from None
            raise ValueError(exc.words) from None

    # -- setup (section 3.10) ---------------------------------------------------------------------------------------

    def setup(self) -> dict:
        """GET /api/snapraid/setup: the install blocks, the removal block, what is staged and the helper's hash."""
        from . import snapraidd

        folder = self.folder() / "setup"
        staged = (folder / "sf-snapraid").is_file()
        link_staged = (folder / "sf-snapraidd").is_file()
        return {"blocks": copy_blocks(folder if staged else None, release_sha256=file_sha256(RELEASE_HELPER)),
                "removal": removal_blocks(), "staged": staged, "setup_dir": str(folder),
                "release_sha256": file_sha256(RELEASE_HELPER), "wizard": None,
                "link": {"blocks": snapraidd.link_blocks(folder if link_staged else None,
                                                         file_sha256(snapraidd.RELEASE_LINK), self._group()),
                         "removal": snapraidd.link_removal_blocks(), "staged": link_staged,
                         "release_sha256": file_sha256(snapraidd.RELEASE_LINK),
                         "daemon_lines": snapraidd.daemon_lines()}}

    @staticmethod
    def _group() -> str:
        import grp

        with contextlib.suppress(KeyError, OSError):
            return grp.getgrgid(os.getgid()).gr_name
        return "<the harness's group>"

    def stage_link(self, user: str | None = None) -> dict:
        """POST /api/snapraid/setup/link-stage: the link, a draft of its settings (reading only, and one allowing
        actions for later), and its two units, into <data_dir>/snapraid/setup/ for the copy blocks."""
        from . import snapraidd

        folder = self.folder() / "setup"
        user = user or getpass.getuser()
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user):
            raise ValueError("the harness's user name cannot go into the link's settings as it is")
        hook = self._hook_url()
        _write_private(folder, "sf-snapraidd", snapraidd.RELEASE_LINK.read_text(encoding="utf-8"))
        _write_private(folder, "snapraidd.json", json.dumps(snapraidd.draft_link_config(user, hook), indent=1) + "\n")
        _write_private(folder, "snapraidd-act.json",
                       json.dumps(snapraidd.draft_link_config(user, hook, ("read", "act")), indent=1) + "\n")
        for unit in snapraidd.UNITS:
            _write_private(folder, unit, snapraidd.unit_text(unit, self._group()))
        return self.setup()

    def _hook_url(self) -> str:
        server = (getattr(self.config, "raw", {}) or {}).get("server") or {}
        return f"http://127.0.0.1:{int(server.get('port') or 8765)}/api/hooks/event"

    def check_link(self) -> dict:
        """POST /api/snapraid/setup/link-check: the link's info, as the install's last block expects it."""
        link = self.daemon.link()
        if link is None:
            return {"ok": False, "code": "no_link", "words": "Seraphina's link is not installed yet."}
        from .snapraidd import LinkError

        try:
            info = link.call("info")
        except LinkError as exc:
            return {"ok": False, "code": exc.code, "words": exc.words}
        allow = (info.get("link") or {}).get("allow") or ["read"]
        ok = info.get("auth_on") is True and re.match(r"^(1[5-9]|[2-9][0-9])\.", str(info.get("engine_version")) + ".")
        return {"ok": bool(ok), "info": info,
                "words": f"The link answers: the password is {'on' if info.get('auth_on') else 'OFF'}, SnapRAID "
                         f"{info.get('engine_version') or '?'}, allowing " + ", ".join(allow) + "."}

    def stage(self, user: str | None = None) -> dict:
        """POST /api/snapraid/setup/stage: copy the helper, a draft of its config and the sudoers file into
        <data_dir>/snapraid/setup/ (0700, files 0600) for the owner's copy blocks. The harness never runs sudo."""
        folder = self.folder() / "setup"
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(folder, 0o700)
        user = user or getpass.getuser()
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user):
            raise ValueError("the harness's user name cannot go into a sudoers line as it is")
        _write_private(folder, "sf-snapraid", RELEASE_HELPER.read_text(encoding="utf-8"))
        _write_private(folder, "snapraid-helper.json", json.dumps(draft_helper_config(), indent=1) + "\n")
        _write_private(folder, "seraphina-snapraid", sudoers_text(user))
        return self.setup()

    def check(self) -> dict:
        """POST /api/snapraid/setup/check: the helper's info, as the install's last block expects it."""
        try:
            info = self.client().call("info")
        except HelperError as exc:
            return {"ok": False, "code": exc.code, "words": exc.words}
        return {"ok": bool(info.get("set_up") and info.get("conf_ok")), "info": _info_summary(info),
                "words": "The reader answers: SnapRAID " + str((info.get("snapraid") or {}).get("version")) +
                         (", a valid config, reading only." if info.get("conf_ok") else
                          ", but the config needs a look: " + "; ".join(p.get("words", "") for p in
                                                                         info.get("conf_problems") or [])[:300])}


# ---------------------------------------------------------------- summaries (counts only)


def _info_summary(info: dict) -> dict:
    cfg = info.get("helper_config") or {}
    parsed = info.get("parsed") or {}
    return {"at": info.get("at"), "snapraid": info.get("snapraid"),
            "helper_sha256": (info.get("helper") or {}).get("sha256"), "set_up": info.get("set_up"),
            "conf": cfg.get("conf"), "allow": cfg.get("allow") or ["read"], "guard_limits": cfg.get("guard"),
            "conf_ok": info.get("conf_ok"), "conf_problems": info.get("conf_problems") or [],
            "data": parsed.get("data") or [], "parity": [p.get("name") for p in parsed.get("parity") or []],
            "content": parsed.get("content") or [], "exclude": parsed.get("exclude") or [],
            "mounts": info.get("mounts"), "content_report": info.get("content"),
            "schedulers": info.get("schedulers"), "backups_running": info.get("backups_running") or [],
            "holds": len(info.get("holds") or []), "releases": len(info.get("releases") or []),
            "running": info.get("running")}


def _status_summary(status: dict) -> dict:
    return {key: status.get(key) for key in ("at", "job", "outcome", "words", "facts", "mounts", "content",
                                               "numbers")}


def _smart_summary(smart: dict) -> dict:
    """Health per disk without serial, model or device (those come live from the helper's disks verb)."""
    disks = {name: {key: entry.get(key) for key in ("health", "afr", "probability", "errors", "why")}
             for name, entry in (smart.get("disks") or {}).items()}
    return {"at": smart.get("at"), "job": smart.get("job"), "outcome": smart.get("outcome"),
            "unsupported": smart.get("unsupported"), "array_probability": smart.get("array_probability"),
            "disks": disks}


def _guard_summary(guard: dict) -> dict:
    return {key: guard.get(key) for key in ("guard", "at", "verdict", "reasons", "counts", "checks", "first_run",
                                              "list_sha256", "numbers", "report_only", "runs")}


def merge_status(previous: dict, answer: dict, tried: str | None = None) -> dict:
    """The status facts after one status answer. A good one replaces them (facts_at: when SnapRAID's facts were
    read). A failed one (SnapRAID could not report: a damaged file list, a timeout) keeps the last good SnapRAID
    facts and their time, takes the reader's fresh mount check and file list report, and records the failure
    (failed {at, job, outcome, words}): a failure never reads as "never protected". A busy one changes nothing."""
    if answer.get("outcome") == "busy":
        return previous
    summary = _status_summary(answer)
    if answer.get("outcome") == "ok":
        summary.update(facts_at=summary.get("at"), failed=None)
    else:
        summary.update(facts=previous.get("facts"),
                       facts_at=previous.get("facts_at") or (previous.get("at") if previous.get("facts") else None),
                       failed={key: answer.get(key) for key in ("at", "job", "outcome", "words")})
    summary["tried"] = tried
    return summary


def _sample_room(facts: dict, status: dict, now: float) -> None:
    """One sample of the protection disk's free room (parity_block_free_min) per half day, for room_days."""
    free = ((status.get("facts") or {}).get("parity_block_free_min"))
    if not isinstance(free, int) or isinstance(free, bool):
        return
    room = [row for row in facts.get("room") or [] if isinstance(row, list) and len(row) == 2]
    if room and now - room[-1][0] < 12 * 3600:
        room[-1] = [room[-1][0], free] if free < room[-1][1] else room[-1]
    else:
        room.append([now, free])
    facts["room"] = room[-ROOM_SAMPLES:]


def fact_words(facts: dict, cfg: dict, now: float) -> list[str]:
    """The card's plain sentences under the light: how much was checked for hidden damage, the disks' health
    (every disk, protection disks too; never "healthy" on an old reading), the room left for new data, and what the
    last check for changes found."""
    out: list[str] = []
    state = (facts.get("status") or {}).get("facts") or {}
    if state.get("array_empty"):
        return out
    oldest = state.get("oldest_scrubbed")
    if state.get("unscrubbed_blocks"):
        out.append("Part of the disks has never been checked for hidden damage.")
    elif oldest:
        days = max(1, math.ceil((now - oldest) / 86400))
        out.append("Every part of the disks was checked for hidden damage in the last " +
                   ("day." if days == 1 else f"{days} days."))
    smart_facts = facts.get("smart") or {}
    smart = smart_facts.get("disks") or {}
    health = health_state(facts, cfg, now)
    if smart and health in ("old", "failed"):
        at = _when(smart_facts.get("at"))
        out.append(f"The disks' health was last read {age_words(now - at)}." if at else
                   "The disks' health could not be read at the last check.")
    elif smart:
        failing = [name for name, entry in smart.items() if entry.get("health") in ("fail", "prefail")]
        unknown = [name for name, entry in smart.items() if entry.get("health") == "unknown"]
        if failing:
            out.append(f"{plural(len(failing), 'disk')} {'says it is' if len(failing) == 1 else 'say they are'} "
                       f"failing.")
        if unknown:
            out.append(f"The health of {plural(len(unknown), 'disk')} can't be read.")
        if not failing and not unknown:
            out.append("The disk is healthy." if len(smart) == 1 else f"All {len(smart)} disks are healthy.")
    free, block = state.get("parity_block_free_min"), state.get("block_size")
    if isinstance(free, int) and isinstance(block, int) and block:
        out.append(f"The protection disk has room for about {size_words(free * block)} more data.")
    guard = facts.get("guard") or {}
    counts = (guard.get("counts") or {}).get("array")
    at = _when(guard.get("at"))
    if counts and at is not None:
        out.append(f"Since the last run, as checked {age_words(now - at)}: {counts.get('added', 0):,} added, "
                   f"{counts.get('removed', 0):,} removed, {counts.get('updated', 0):,} changed, "
                   f"{counts.get('moved', 0):,} moved.")
    return out


def _health_key(name: str, numbers: dict) -> str:
    """A disk's key in tracker items and notices: its number (data) or p<level> (protection, by its level, so two
    protection disks never share a key and "q-parity" is "2-parity"'s), never its name."""
    if name in numbers:
        return str(numbers[name])
    level = PARITY_LEVELS.get(name)
    return f"p{level}" if level else "other"


def _health_fields(cause: str, label: str, entry: dict, at: Any, key: str) -> dict:
    """A health finding's fields; label is the disk as the card names it ("Disk 2", "The protection disk"), key its
    tracker key (_health_key)."""
    title = HEALTH_TITLES[cause].format(disk=label)
    named = _lower_first(label)
    if cause == "fail":
        detail = (f"The health check (SMART) of {named} reports a failure. Replace it soon, and check that your "
                  f"backups are current. Seen {at}. This item closes on its own once a fresh check reads the disk "
                  f"healthy.")
    else:
        detail = (f"The health check (SMART) of {named} gave no data ({entry.get('why') or 'no data'}). A disk "
                  f"without data is unknown, never healthy. USB enclosures often need a setting in the config; or "
                  f"accept it in the settings (snapraid.light.smart_unreadable_ok). Seen {at}.")
    return {"title": title, "detail": detail, "kind": "finding",
            "source": {"type": "snapraid", "key": f"health:{key}", "observed_at": at}}


def _open_item(ledger: Any, dedupe: str) -> dict | None:
    found = [item for item in ledger.items if (item.get("source") or {}).get("hash") == dedupe
             and item.get("status") not in tracker.CLOSED]
    return found[-1] if found else None


def _iso(when: float | None) -> str | None:
    if when is None:
        return None
    return datetime.fromtimestamp(when, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _write_private(folder: Path, name: str, text: Any) -> None:
    """name in folder (0700), written beside its place (0600), then moved over it."""
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(folder, 0o700)
    data = text if isinstance(text, str) else json.dumps(text, indent=1, ensure_ascii=False)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=f".{name}.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
        os.replace(tmp, folder / name)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# ---------------------------------------------------------------- copy blocks (section 3.10)


def sudoers_text(user: str) -> str:
    """The one sudoers rule: the harness's user may run the helper with no arguments at all ("" in sudoers)."""
    return (f"# Seraphina OS Harness: the disk protection reader, with no arguments (docs/seraphina/"
            f"BACKUPS_SNAPRAID.md §8.7)\nDefaults!{HELPER} !use_pty\n{user} ALL=(root) NOPASSWD: {HELPER} \"\"\n")


def draft_helper_config(conf_text: str | None = None, uuid_of: Callable[[str], str | None] | None = None) -> dict:
    """A draft of the helper's config: the config path, reading only, each disk's filesystem UUID where this user
    can read it (findmnt), else a placeholder the owner fills in (explain refuses it until then)."""
    conf = next((path for path in CONFS if os.path.isfile(path)), CONFS[0])
    if conf_text is None:
        with contextlib.suppress(OSError, UnicodeDecodeError):
            conf_text = Path(conf).read_bytes().decode("utf-8")
    mounts: dict[str, str] = {}
    for line in (conf_text or "").split("\n"):
        match = re.match(r"^\s*(data|disk)\s+(\S+)\s+(.+?)\s*$", line)
        level = re.match(r"^\s*((?:[1-6]|[qrz])?-?parity)\s+(.+?)\s*$", line)
        if match:
            mounts[match.group(2)] = (uuid_of or _findmnt_uuid)(match.group(3)) or \
                f"<filesystem UUID of {match.group(3).rstrip('/')}>"
        elif level:
            folder = os.path.dirname(level.group(2).split(",")[0])
            mounts[level.group(1)] = (uuid_of or _findmnt_uuid)(folder) or f"<filesystem UUID of {folder}>"
    return {"v": 1, "conf": conf, "allow": ["read"], "mounts": mounts, "prehash": True,
            "placeholders": {}}


def _findmnt_uuid(path: str) -> str | None:
    """The UUID of the filesystem mounted exactly at path (findmnt --mountpoint), or None: never the filesystem of a
    folder above it (--target would give the system disk's for a disk that is not mounted at drafting time)."""
    point = path.rstrip("/") or "/"
    try:
        out = subprocess.run(["findmnt", "-n", "-o", "UUID", "--mountpoint", point], capture_output=True, text=True,
                             timeout=10, stdin=subprocess.DEVNULL, check=False).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out if re.fullmatch(r"[0-9A-Fa-f-]{4,64}", out) else None


def copy_blocks(setup: Path | None, release_sha256: str | None = None) -> list[dict]:
    """The install blocks, in order (design section 8.7). Each installs to a root-owned .new path and checks or
    explains that copy (commands); only then, as a step of its own (then), moves it into place, and that move runs
    only when its check passes again (sha256sum -c, explain, visudo -cf, chained with &&), so a block pasted whole
    never moves an unchecked file. On a mismatch if_not removes the .new file."""
    staged = shlex.quote(str(setup)) if setup else "<stage the files first>"
    new_helper = f"{HELPER}.new"
    if release_sha256:
        install = f"echo {shlex.quote(f'{release_sha256}  {new_helper}')} | sha256sum -c && sudo mv {new_helper} {HELPER}"
    else:
        install = f"sudo mv {new_helper} {HELPER}"
    return [
        {"id": "1", "title": "Check SnapRAID",
         "commands": ["snapraid --version"], "expect": "snapraid v13.0 (or v0 for a distribution's 13.0 package)",
         "if_not": "The reader reads 13.x; 12.x and 14.x with care; 15 or newer only through the SnapRAID Daemon "
                   "(set up Seraphina's link instead). Keep exactly one SnapRAID program installed."},
        {"id": "2", "title": "Install the disk protection reader",
         "commands": [f"sudo install -d -m 0755 -o root -g root {os.path.dirname(HELPER)}",
                      f"sudo install -m 0755 -o root -g root {staged}/sf-snapraid {new_helper}",
                      f"sha256sum {new_helper}"],
         "expect": release_sha256 or "the sha256 published in the release notes",
         "then": [install],
         "note": "Compare the number with the release notes opened on another device: that catches a swapped or "
                 "damaged file. Compared only with this page, it catches accidental damage and nothing more, "
                 "because this page is what a check would guard against. The second step moves the file into "
                 "place only when it matches this page's number.",
         "if_not": f"sudo rm {new_helper}"},
        {"id": "3", "title": "Install its settings",
         "commands": ["sudo install -d -m 0755 -o root -g root /etc/seraphina",
                      f"sudo install -m 0644 -o root -g root {staged}/snapraid-helper.json {HELPER_CONFIG}.new",
                      f"sudo {HELPER} explain helper-config"],
         "expect": "It says what the settings allow (reading only) and names each disk's filesystem; run the "
                   "second step only when that is what you want.",
         "then": [f"sudo {HELPER} explain helper-config > /dev/null && sudo mv {HELPER_CONFIG}.new {HELPER_CONFIG}"],
         "note": "Fill in any <filesystem UUID of ...> in the staged file first (findmnt -o TARGET,UUID lists "
                 "them). The file holds no secret. The second step checks it again and moves it only when the "
                 "reader would use it.",
         "if_not": f"sudo rm {HELPER_CONFIG}.new"},
        {"id": "4", "title": "Allow Seraphina to call it, and nothing else",
         "commands": [f"sudo install -m 0440 -o root -g root {staged}/seraphina-snapraid {SUDOERS}.new",
                      f"sudo visudo -cf {SUDOERS}.new"],
         "expect": "parsed OK",
         "then": [f"sudo visudo -cf {SUDOERS}.new && sudo mv {SUDOERS}.new {SUDOERS}"],
         "note": "Until the move, sudo ignores the file (its name has a dot). The rule lets Seraphina's user run the "
                 "reader with no arguments only. The second step checks the file again and moves it only when sudo "
                 "can read it, so a broken file never reaches /etc/sudoers.d.",
         "if_not": f"sudo rm {SUDOERS}.new"},
        {"id": "5", "title": "Check", "commands": [], "action": "check",
         "expect": "The reader answers: SnapRAID 13.0, a valid config, reading only.",
         "if_not": "The answer says what to fix; the doctor says it too."},
    ]


def removal_blocks() -> list[dict]:
    return [
        {"id": "r1", "title": "Remove the reader",
         "commands": [f"sudo rm {SUDOERS} {HELPER_CONFIG} {HELPER}"],
         "expect": "nothing printed",
         "note": "First turn the panel off (snapraid.enabled false). This changes nothing in SnapRAID itself: its "
                 "config and its protection data stay as they are."},
        {"id": "r2", "title": "Also remove what the reader kept (optional)",
         "commands": [f"sudo rm -r {STATE}"],
         "expect": "nothing printed",
         "note": "Its run records, file lists and logs, the only place file names were kept (none went to the "
                 "system journal)."},
    ]


# ---------------------------------------------------------------- the doctor (section 9.7)


def doctor_checks(config: Any, *, client: HelperClient | None = None, local: dict | None = None,
                  fstab: str = "/etc/fstab", status_path: str = "/proc/self/status",
                  check: Callable[..., dict] | None = None, daemon: dict | None = None) -> list[dict]:
    """The doctor's SnapRAID checks, read-only: through the helper's info, and on the daemon route through /metrics
    and Seraphina's link (snapraidd.doctor_checks). daemon: injectables for that part (tests)."""
    from . import snapraid_rebuild, snapraidd
    from .doctor import check as make_check
    from .notify import sandbox_mode, uses_cli_driver

    make = check or make_check
    cfg = settings(config)
    facts = local if local is not None else local_facts()
    out: list[dict] = []
    daemon_kw = dict(daemon or {})
    if "link" not in daemon_kw:
        path = str(snapraidd.settings(cfg)["link"])
        daemon_kw["link"] = snapraidd.LinkClient(path) if os.path.exists(path) else None
    daemon_lines = snapraidd.doctor_checks(config, cfg, make, data_dir=getattr(config, "data_dir", None),
                                           **daemon_kw) if cfg.get("enabled") else []
    daemon_found = any(line.get("name") == "snapraid daemon" and "answers, running" in line.get("detail", "")
                       for line in daemon_lines)
    rebuild_lines = snapraid_rebuild.doctor_lines(config, make) if cfg.get("enabled") else []
    if not facts.get("binary"):
        if daemon_found:
            return daemon_lines + rebuild_lines + [make("snapraid lab checks", False, "warn", DAEMON_LAB_WORDS)]
        return [make("snapraid", True, "info", "SnapRAID is not installed (optional: it rebuilds a dead data disk "
                                               "from the others)")]
    out.append(version_check(facts, make, daemon_found))
    if not cfg.get("enabled"):
        out.append(make("snapraid panel", True, "info", "the disk protection panel is off (snapraid.enabled); "
                                                        "turn it on once its reader is installed"))
        return out
    helper = str(cfg.get("helper") or HELPER)
    if helper != HELPER:
        out.append(make("snapraid reader", False, "warn", f"snapraid.helper is {helper}, but the reader accepts only "
                                                          f"its own place, {HELPER}"))
    if not os.path.isfile(helper) and client is None:
        out.append(make("snapraid reader", False, "warn", "the disk protection reader is not installed: the setup "
                                                          "page has its copy blocks"))
        return out
    if sandbox_mode(config) != "bwrap" or uses_cli_driver(config):
        why = "harness.sandbox is none" if sandbox_mode(config) != "bwrap" else "an agent route uses a CLI driver"
        out.append(make("snapraid sandbox", False, "error", f"disk protection is on but {why}: your AI agents can "
                                                            f"read the names of your files and call the reader "
                                                            f"exactly as the harness does. Set harness.sandbox to "
                                                            f"bwrap and use the builtin driver"))
    caller = client or HelperClient(helper, status_path=status_path)
    try:
        info = caller.call("info")
    except HelperError as exc:
        level = "error"
        detail = exc.words
        if exc.code == "sudo_refused" and no_new_privs(status_path):
            detail = ("sudo cannot reach the reader: this process runs with NoNewPrivs set (a unit with "
                      "NoNewPrivileges=yes), which stops sudo. Run the harness from a unit without NoNewPrivileges: "
                      "on a server, the harness's own system unit (systemd/factory@.service), under its own user")
        out.append(make("snapraid reader", False, level, detail))
        out.append(make("snapraid lab checks", False, "warn", LAB_WORDS))
        return out
    found_binary = info.get("snapraid") or {}
    if not found_binary.get("binary"):
        out.append(make("snapraid binary", False, "error", "the reader finds no SnapRAID 13 at its fixed places "
                                                           f"({', '.join(BINARIES)}), so every read is refused: "
                                                           f"{found_binary.get('warning') or 'none answers'}"))
    elif found_binary.get("warning"):
        out.append(make("snapraid binary", False, "warn", f"{found_binary.get('binary')}: "
                                                          f"{found_binary.get('warning')}"))
    release = file_sha256(RELEASE_HELPER)
    sha = (info.get("helper") or {}).get("sha256")
    if sha and release and sha != release:
        out.append(make("snapraid reader", False, "warn", "the installed reader is not this release's (its sha256 "
                                                          "differs): that catches damage, and a swapped file only if "
                                                          "you also compare the release notes yourself"))
    else:
        out.append(make("snapraid reader", True, "info", "the installed reader is this release's (sha256 matches; "
                                                         "compare the release notes yourself to rule out a swap)"))
    if not info.get("set_up"):
        out.append(make("snapraid config", False, "warn", "the reader's settings are not installed yet (setup page)"))
    elif not info.get("conf_ok"):
        problem = (info.get("conf_problems") or [{"words": "the config cannot be read"}])[0]
        out.append(make("snapraid config", False, "error", f"unsafe_conf: {problem.get('words')}"))
    else:
        out.append(make("snapraid config", True, "info", "the SnapRAID config and the reader's settings pass its "
                                                         "checks"))
    content = info.get("content") or {}
    if content.get("too_few_disks"):
        out.append(make("snapraid file list", False, "error", content.get("problem") or "the copies of the file list "
                                                                                        "sit on too few disks"))
    parsed = info.get("parsed") or {}
    copies = parsed.get("content") or []
    if copies and not copies[0].startswith(("/var/snapraid/", "/var/lib/snapraid/")):
        out.append(make("snapraid file list", True, "info", "the first copy of the file list is not on the system "
                                                            "disk (/var/snapraid/ or /var/lib/snapraid/): every "
                                                            "hourly status reads a data disk"))
    if info.get("releases"):
        out.append(make("snapraid releases", False, "error", f"{len(info['releases'])} release line(s) with no "
                                                             f"release in the cockpit: something called the reader "
                                                             f"directly"))
    out += _fstab_checks(parsed, info.get("mounts") or {}, fstab, make)
    if config is not None and sandbox_mode(config) == "bwrap":
        from .config import sandbox_masks

        with contextlib.suppress(Exception):
            masked = any(Path(path) == Path(config.data_dir) / DIR for path in sandbox_masks(config)["dirs"])
            if not masked:
                out.append(make("snapraid masks", False, "warn", f"{Path(config.data_dir) / DIR} is not hidden from "
                                                                 f"the agents' sandbox"))
    found = (info.get("schedulers") or {}).get("found") or []
    if found:
        places = ", ".join(entry.get("place", "?") for entry in found[:5])
        out.append(make("snapraid schedulers", True, "info", f"{plural(len(found), 'other scheduler')} of SnapRAID: "
                                                             f"{places}"))
    out += daemon_lines + rebuild_lines
    out.append(make("snapraid lab checks", False, "warn", DAEMON_LAB_WORDS if daemon_found else LAB_WORDS))
    return out


def version_check(facts: dict, make: Callable[..., dict], daemon_found: bool = False) -> dict:
    """The version line, by the policy of design section 8.2: 15 and later only through the SnapRAID Daemon, so it
    passes only when one answers (daemon_found)."""
    version = facts.get("version") or ""
    where = facts.get("binary")
    if version.startswith("13."):
        return make("snapraid", True, "info", f"SnapRAID {version} at {where}")
    if version == "0":
        return make("snapraid", True, "info", f"SnapRAID at {where} prints v0 (a distribution build): the reader "
                                              f"reads it as 13.0 only when the package database owns it at 13.0")
    if version.startswith(("12.", "14.")):
        return make("snapraid", False, "warn", f"SnapRAID {version}: read with care (some facts may be missing)")
    if re.match(r"^(1[5-9]|[2-9][0-9])\.", version):
        if daemon_found:
            return make("snapraid", True, "info", f"SnapRAID {version} at {where}: read through the SnapRAID Daemon")
        return make("snapraid", False, "warn", f"SnapRAID {version} at {where} needs the SnapRAID Daemon, which is not "
                                               f"installed or not answering: Seraphina does not read it without one")
    return make("snapraid", False, "warn", f"SnapRAID at {where} answers version {version or 'unknown'}: not a version "
                                           f"Seraphina reads (she reads 13.x, 12.x and 14.x with care, and 15 and "
                                           f"later through the SnapRAID Daemon)")


def _fstab_checks(parsed: dict, mounts: dict, path: str, make: Callable[..., dict]) -> list[dict]:
    """An array disk's fstab line without nofail and x-systemd.device-timeout= (section 6.3 step 3): every data and
    protection disk (its mount point from the reader's mount check, else the folder of its file)."""
    try:
        text = Path(path).read_bytes().decode("utf-8", "replace")
    except OSError:
        return []
    points = {(entry.get("dir") or "").rstrip("/") for entry in parsed.get("data") or []}
    points |= {os.path.dirname(entry["files"][0]) for entry in parsed.get("parity") or [] if entry.get("files")}
    points |= {(entry.get("path") or "").rstrip("/") for entry in mounts.values() if entry.get("role") == "parity"}
    points.discard("")
    out = []
    for line in text.split("\n"):
        fields = line.split()
        if len(fields) < 4 or line.lstrip().startswith("#"):
            continue
        if fields[1].rstrip("/") in points:
            options = fields[3].split(",")
            if "nofail" not in options or not any(o.startswith("x-systemd.device-timeout=") for o in options):
                out.append(make("snapraid fstab", False, "warn", f"{fields[1]}'s fstab line lacks nofail and "
                                                                 f"x-systemd.device-timeout=: a boot without that "
                                                                 f"disk stops in emergency mode, where Tailscale does "
                                                                 f"not run"))
    return out


def novice_strings() -> list[str]:
    """Every novice sentence of this module and of the disk checklist (U7's word check)."""
    from .snapraid_rebuild import novice_strings as rebuild_strings

    return [*rebuild_strings(), *SENTENCES, WATCHING, SANDBOX_LINE, NO_VERSIONED, *NOT_COVERED, TAKING_OUT_COVERED, LAB_LINE, LAB_WORDS, *REASONS.values(),
            *QUIET_WORDS.values(), DAEMON_LAB_WORDS,
            *HEALTH_TITLES.values(), *(check["what"] for check in LAB_CHECKS), LAB_WHY]


def internal_terms(text: str) -> list[str]:
    """The internal terms (design section 12.5's words table) found in text."""
    return [match.group(0) for match in INTERNAL_RE.finditer(text)]


def card_lines(view: dict) -> list[str]:
    """The card in plain text for `factory snapraid`."""
    lines = []
    lit = view.get("light") or {}
    tone = {"err": "RED", "warn": "AMBER", "ok": "GREEN", "muted": "GREY"}.get(lit.get("tone"), "GREY")
    lines.append(f"Disk protection: {tone}  {lit.get('reason', '')}")
    for key in ("note", "last_known"):
        if lit.get(key):
            lines.append(f"  {lit[key]}")
    lines.append(f"  {view.get('undo_line')}")
    rebuild = view.get("rebuild") or {}
    if rebuild.get("state") == "open":
        lines.append(f"  A disk is being replaced: {rebuild.get('label')}, step {rebuild.get('n')} of "
                     f"{rebuild.get('of')} ({rebuild.get('title')}): factory snapraid rebuild")
    daemon = view.get("daemon")
    if daemon:
        lines.append(f"  The protection service: {'answering' if daemon.get('answering') else 'NOT ANSWERING'}, "
                     f"version {daemon.get('version') or '?'}, SnapRAID {daemon.get('engine') or '?'}, password "
                     f"{({True: 'on', False: 'OFF'}).get(daemon.get('auth_on'), 'unknown')}")
        if daemon.get("limits_line"):
            lines.append(f"  {daemon['limits_line']}")
        window = daemon.get("window") or {}
        count = (daemon.get("schedulers") or {}).get("count")
        lines.append(f"  Next run: {window.get('words') or 'none scheduled'}; things that start runs: "
                     f"{'unknown' if count is None else count}")
        last = daemon.get("last_run") or {}
        if last:
            lines.append(f"  Last run: {last.get('high_command')} {last.get('verdict')}")
        if not (daemon.get("link") or {}).get("installed"):
            lines.append("  Seraphina's link is not installed: factory snapraid link install")
        for action in view.get("actions") or []:
            lines.append(f"  {action.get('approval')} {action.get('action')}: {action.get('state')}"
                         + (f", {action.get('outcome')}" if action.get("outcome") else ""))
    for text in ((view.get("facts") or {}).get("words") or []):
        lines.append(f"  {text}")
    for row in view.get("disks") or []:
        label = row.get("label") or (f"Disk {row['number']}" if row.get("number") else "Protection disk")
        lines.append(f"  {label:<19} {row.get('name')}: {'mounted' if row.get('mounted') else 'NOT MOUNTED'} at "
                     f"{row.get('mount')}; files {row.get('files') if row.get('files') is not None else '-'}; health "
                     f"{row.get('health') or '-'}")
    guard = view.get("guard") or {}
    if guard:
        lines.append(f"  last check for changes {guard.get('guard')}: {guard.get('verdict')}"
                     + (f" ({', '.join(guard.get('reasons') or [])})" if guard.get("reasons") else ""))
    for warning in view.get("warnings") or []:
        lines.append(f"  ! {warning.get('words')}")
    lines.append(f"  {view.get('lab_words')}")
    return lines
