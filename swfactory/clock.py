"""Local time, daily windows, the power schedule and time sync (Wake & Messages S11, S12, S18, S50).

Everything the harness stores stays UTC; only windows are judged in the owner's zone, with the
standard library's ``zoneinfo`` (Ubuntu ships the tz database). A Window is a daily span of local
time that may wrap midnight ("21:30-08:00"). The PowerSchedule uses the v2 keys (off, wake,
quiet_before_off_minutes, quiet_after_boot_minutes): ``wake`` is the machine's power-on time, and
no model wakes while the server should be off, in the minutes before it powers off, or in the
minutes after it boots. ``quiet_hours_auto`` is delivery's do-not-disturb time: 90 minutes before
power-off to 60 minutes after boot.

detect_zone, detect_power and ntp_synchronized read the machine (the /etc/localtime link,
``systemctl list-timers`` and the RTC alarm, ``timedatectl``), each through an injectable runner
so tests feed them fake output. TimeSync answers "may the clock be trusted yet?" after boot: once
NTP says it is synchronised, or once the system has been up for the fallback period (the doctor
then warns that the fallback was used).
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc
TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
WINDOW_RE = re.compile(r"^\s*(\d{2}:\d{2})\s*-\s*(\d{2}:\d{2})\s*$")
ZONE_RE = re.compile(r"^[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+)*$")
AUTO_BEFORE_OFF = 90    # minutes of do-not-disturb time before power-off (quiet_hours "auto")
AUTO_AFTER_BOOT = 60    # ... and after boot
POWER_SLACK_MINUTES = 15
RUN_TIMEOUT = 5.0
SYNC_RECHECK_SECONDS = 15.0
POWER_UNIT_RE = re.compile(r"poweroff|shutdown", re.IGNORECASE)
_CLOCK_RE = re.compile(r"\b([01]\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?\b")


def parse_hhmm(text: object, label: str = "time") -> dt_time:
    """dt_time from "HH:MM" (ValueError naming label otherwise)."""
    match = TIME_RE.fullmatch(text.strip()) if isinstance(text, str) else None
    if not match:
        raise ValueError(f"{label} must be a time like 07:45 (HH:MM, 24-hour)")
    return dt_time(int(match.group(1)), int(match.group(2)))


def load_zone(name: object) -> ZoneInfo:
    """The ZoneInfo for an IANA name (ValueError if it does not load)."""
    if not isinstance(name, str) or not ZONE_RE.fullmatch(name) or ".." in name:
        raise ValueError("timezone must be an IANA name such as Europe/London")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ValueError(f"timezone {name!r} is not in this machine's time-zone database") from None


def iso(at: datetime) -> str:
    """An aware datetime as the harness stores times: UTC, seconds, 'Z'."""
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: object) -> datetime | None:
    """An aware UTC datetime from a stored time (None when it does not parse)."""
    if not isinstance(text, str) or not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


class LocalClock:
    """The current time, and conversion to the owner's zone. zone None: detect_zone(), else UTC."""

    def __init__(self, zone: str | None = None, now: Callable[[], float] = time.time) -> None:
        self.name = zone or detect_zone() or "UTC"
        self.zone = load_zone(self.name)
        self._now = now

    def utc(self) -> datetime:
        """Now, aware, in UTC."""
        return datetime.fromtimestamp(self._now(), UTC)

    def local(self, at: datetime | None = None) -> datetime:
        """at (default now) in the owner's zone."""
        return (at or self.utc()).astimezone(self.zone)

    def at(self, day: date, when: dt_time) -> datetime:
        """The aware local datetime of a wall-clock time on a local day (a time in a DST gap moves forward)."""
        naive = datetime.combine(day, when)
        aware = naive.replace(tzinfo=self.zone)
        return aware.astimezone(UTC).astimezone(self.zone)

    def stamp(self, at: datetime | None = None) -> str:
        """at (default now) as the harness stores it (UTC ISO)."""
        return iso(at or self.utc())

    def hhmm(self, at: datetime | None = None) -> str:
        """at (default now) as local HH:MM."""
        return self.local(at).strftime("%H:%M")


def _local_at(local: datetime, day: date, when: dt_time) -> datetime:
    """when on day, in local's zone (normalised through UTC, so a DST gap moves forward)."""
    aware = datetime.combine(day, when).replace(tzinfo=local.tzinfo)
    return aware.astimezone(UTC).astimezone(local.tzinfo)


def _utc(at: datetime) -> datetime:
    """at in UTC: aware datetimes that share a tzinfo compare by wall time, which a DST change makes wrong."""
    return at.astimezone(UTC)


def _minutes(when: dt_time) -> int:
    return when.hour * 60 + when.minute


def _shift(when: dt_time, minutes: int) -> dt_time:
    total = (_minutes(when) + minutes) % (24 * 60)
    return dt_time(total // 60, total % 60)


@dataclass(frozen=True)
class Window:
    """A daily span of local time, start inclusive, end exclusive; it wraps midnight when start > end."""

    start: dt_time
    end: dt_time

    @classmethod
    def parse(cls, text: object) -> "Window":
        """Window from "HH:MM-HH:MM" (ValueError otherwise; an empty window, start == end, is refused)."""
        match = WINDOW_RE.fullmatch(text) if isinstance(text, str) else None
        if not match:
            raise ValueError("a window must look like 21:30-08:00 (HH:MM-HH:MM)")
        start, end = parse_hhmm(match.group(1), "window start"), parse_hhmm(match.group(2), "window end")
        if start == end:
            raise ValueError("a window's start and end must differ")
        return cls(start, end)

    def text(self) -> str:
        return f"{self.start:%H:%M}-{self.end:%H:%M}"

    @property
    def wraps(self) -> bool:
        return self.start > self.end

    def contains(self, local: datetime) -> bool:
        now = local.time().replace(second=0, microsecond=0, tzinfo=None)
        if self.wraps:
            return now >= self.start or now < self.end
        return self.start <= now < self.end

    def next_end(self, local: datetime) -> datetime:
        """The first end of this window strictly after local (the current window's end while inside it)."""
        candidate = _local_at(local, local.date(), self.end)
        while _utc(candidate) <= _utc(local):
            candidate = _local_at(local, candidate.date() + timedelta(days=1), self.end)
        return candidate

    def next_start(self, local: datetime) -> datetime:
        """The first start of this window strictly after local."""
        candidate = _local_at(local, local.date(), self.start)
        while _utc(candidate) <= _utc(local):
            candidate = _local_at(local, candidate.date() + timedelta(days=1), self.start)
        return candidate

    def last_start(self, local: datetime) -> datetime:
        """The latest start of this window at or before local."""
        candidate = _local_at(local, local.date(), self.start)
        while _utc(candidate) > _utc(local):
            candidate = _local_at(local, candidate.date() - timedelta(days=1), self.start)
        return candidate

    def last_end(self, local: datetime) -> datetime:
        """The latest end of this window at or before local."""
        candidate = _local_at(local, local.date(), self.end)
        while _utc(candidate) > _utc(local):
            candidate = _local_at(local, candidate.date() - timedelta(days=1), self.end)
        return candidate


@dataclass(frozen=True)
class PowerSchedule:
    """The nightly power-off (v2 §6.6 keys): off, the power-on time wake, and the quiet minutes around them."""

    off: dt_time
    wake: dt_time
    quiet_before_off: int = 30
    quiet_after_boot: int = 45

    @classmethod
    def from_config(cls, section: object) -> "PowerSchedule | None":
        """From wakes.power ({off, wake, quiet_before_off_minutes, quiet_after_boot_minutes}); None when off is
        null (a server that never powers off). ValueError naming the bad key."""
        if not isinstance(section, dict) or section.get("off") is None:
            return None
        off = parse_hhmm(section.get("off"), "wakes.power.off")
        wake = parse_hhmm(section.get("wake"), "wakes.power.wake")
        if off == wake:
            raise ValueError("wakes.power.off and wakes.power.wake must differ")
        numbers = []
        for key, default in (("quiet_before_off_minutes", 30), ("quiet_after_boot_minutes", 45)):
            value = section.get(key, default)
            value = default if value is None else value
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 240:
                raise ValueError(f"wakes.power.{key} must be a whole number of minutes from 0 to 240")
            numbers.append(value)
        return cls(off, wake, numbers[0], numbers[1])

    def _windows(self) -> tuple[Window, Window | None, Window | None]:
        off = Window(self.off, self.wake)
        before = Window(_shift(self.off, -self.quiet_before_off), self.off) if self.quiet_before_off else None
        after = Window(self.wake, _shift(self.wake, self.quiet_after_boot)) if self.quiet_after_boot else None
        return off, before, after

    def state(self, local: datetime) -> str:
        """"off", "quiet_before_off", "quiet_after_boot" or "on" at local."""
        off, before, after = self._windows()
        if off.contains(local):
            return "off"
        if before is not None and before.contains(local):
            return "quiet_before_off"
        if after is not None and after.contains(local):
            return "quiet_after_boot"
        return "on"

    def next_allowed(self, local: datetime) -> datetime:
        """The first moment at or after local when a model wake may run (the end of the quiet after boot)."""
        if self.state(local) == "on":
            return local
        first_on = _shift(self.wake, self.quiet_after_boot)
        candidate = _local_at(local, local.date(), first_on)
        while _utc(candidate) < _utc(local):
            candidate = _local_at(local, candidate.date() + timedelta(days=1), first_on)
        return candidate

    def quiet_hours_auto(self) -> Window:
        """Do-not-disturb time from the schedule: 90 minutes before power-off to 60 minutes after boot."""
        return Window(_shift(self.off, -AUTO_BEFORE_OFF), _shift(self.wake, AUTO_AFTER_BOOT))

    def in_power_off(self, at: datetime, slack_minutes: int = POWER_SLACK_MINUTES) -> bool:
        """True when at (aware, any zone; give it in local time) falls in the power-off window widened by slack
        on both sides (S18: a hard stop at 22:59 is the power-off, one at 15:42 is not)."""
        widened = Window(_shift(self.off, -slack_minutes), _shift(self.wake, slack_minutes))
        return widened.contains(at)

    def text(self) -> str:
        return f"{self.off:%H:%M}-{self.wake:%H:%M}"


# ---------------------------------------------------------------- reading the machine


def _run(argv: list[str], timeout: float = RUN_TIMEOUT) -> str:
    """stdout of argv, or "" when it cannot run, fails or times out."""
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout,
                              env={**os.environ, "LC_ALL": "C", "SYSTEMD_PAGER": "", "SYSTEMD_COLORS": "0"})
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""
    return done.stdout if done.returncode == 0 else ""


def detect_zone(localtime: Path = Path("/etc/localtime"), timezone_file: Path = Path("/etc/timezone")) -> str | None:
    """The machine's IANA zone: from the /etc/localtime symlink, else /etc/timezone; None when unknown."""
    try:
        target = os.readlink(localtime)
    except OSError:
        target = ""
    if "zoneinfo/" in target:
        name = target.split("zoneinfo/", 1)[1].strip("/")
        if ZONE_RE.fullmatch(name):
            return name
    try:
        name = timezone_file.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return name if ZONE_RE.fullmatch(name or "") else None


def detect_power(run: Callable[..., str] = _run, wakealarm: Path = Path("/sys/class/rtc/rtc0/wakealarm"),
                 zone: str | None = None) -> dict:
    """What the machine says about its power schedule: {off, wake, off_unit, wake_source} (HH:MM or None).

    off: the local time of the next run of a timer whose unit names mention poweroff or shutdown
    (``systemctl list-timers --all``); wake: the RTC alarm (``/sys/class/rtc/rtc0/wakealarm``, seconds since
    the epoch) where readable."""
    found: dict = {"off": None, "wake": None, "off_unit": None, "wake_source": None}
    output = run(["systemctl", "list-timers", "--all", "--no-legend", "--no-pager"])
    for line in output.splitlines():
        if not POWER_UNIT_RE.search(line):
            continue
        match = _CLOCK_RE.search(line)
        units = [word for word in line.split() if word.endswith((".timer", ".service"))]
        if match:
            found["off"] = f"{match.group(1)}:{match.group(2)}"
            found["off_unit"] = units[0] if units else None
            break
    try:
        raw = wakealarm.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        raw = ""
    if raw.isdigit():
        try:
            tz = load_zone(zone) if zone else load_zone(detect_zone() or "UTC")
        except ValueError:
            tz = ZoneInfo("UTC")
        found["wake"] = datetime.fromtimestamp(int(raw), UTC).astimezone(tz).strftime("%H:%M")
        found["wake_source"] = str(wakealarm)
    return found


WAKE_ALARM = Path("/sys/class/rtc/rtc0/wakealarm")   # readable without root; seconds since the epoch, or empty
WAKE_TOLERANCE = timedelta(minutes=2)               # 06:59:59 for a 07:00 wake is right


def next_wake(power: PowerSchedule, local: datetime) -> datetime:
    """The power-on the schedule expects next after local: power.wake on the first local date still ahead, with
    that date's own UTC offset (so the night the clocks change, the expected instant moves by the hour)."""
    candidate = _local_at(local, local.date(), power.wake)
    while _utc(candidate) <= _utc(local):
        candidate = _local_at(local, candidate.date() + timedelta(days=1), power.wake)
    return candidate


def read_wake_alarm(path: Path = WAKE_ALARM) -> datetime | None:
    """The RTC wake alarm as an aware UTC time, or None when it is not set (empty, 0) or cannot be read."""
    try:
        raw = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None
    if not raw.isdigit() or int(raw) == 0:
        return None
    return datetime.fromtimestamp(int(raw), UTC)


def check_wake_alarm(power: PowerSchedule, alarm: datetime | None, local: datetime) -> tuple[str, str]:
    """(state, words) of the RTC wake alarm against the next wake the schedule expects: "ok", "unset" (nothing set
    now, or an alarm already past: whatever powers the machine off usually sets it first), "hour" (exactly an hour
    off: whatever sets it used the other side of a clock change, as on a first morning of winter time) or "other"."""
    expected = next_wake(power, local)
    when = f"{expected:%H:%M} on {expected:%a %d %b} ({expected.tzname()})"
    if alarm is None or _utc(alarm) <= _utc(local):
        return "unset", (f"no wake alarm is set now; the next power-on the schedule expects is {when}. Whatever "
                         f"powers the machine off usually sets the alarm first")
    shown = alarm.astimezone(local.tzinfo)
    at = f"{shown:%H:%M} on {shown:%a %d %b} ({shown.tzname()})"
    gap = _utc(alarm) - _utc(expected)
    if abs(gap) <= WAKE_TOLERANCE:
        return "ok", f"the wake alarm is set for {at}, as wakes.power.wake says"
    if abs(abs(gap) - timedelta(hours=1)) <= WAKE_TOLERANCE:
        side = "earlier" if gap < timedelta(0) else "later"
        return "hour", (f"the wake alarm is set for {at}, an hour {side} than wakes.power.wake ({when}): whatever "
                        f"sets it seems to use the other side of the clock change (summer and winter time). Have it "
                        f"set the alarm from local time on the day it wakes, not from today's offset")
    return "other", f"the wake alarm is set for {at}, not for the next wake the schedule expects ({when})"


def ntp_synchronized(run: Callable[..., str] = _run) -> bool | None:
    """timedatectl's NTPSynchronized: True, False, or None when it cannot be asked."""
    value = run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"]).strip().lower()
    if value in ("yes", "true", "1"):
        return True
    if value in ("no", "false", "0"):
        return False
    return None


def system_uptime(path: Path = Path("/proc/uptime")) -> float | None:
    """Seconds since boot (None when unknown)."""
    try:
        return float(path.read_text(encoding="ascii").split()[0])
    except (OSError, ValueError, IndexError, UnicodeDecodeError):
        return None


class TimeSync:
    """S50: after boot, wait for the clock before judging staleness, expiry, quiet hours or budgets.

    ready() is true once timedatectl reports NTP synchronised, or once the system has been up for
    fallback_minutes (then fallback_used is set, and the doctor warns), or always when require is false.
    The answer is cached once true; the check runs at most every SYNC_RECHECK_SECONDS."""

    def __init__(self, require: bool = True, fallback_minutes: float = 10,
                 check: Callable[[], bool | None] = ntp_synchronized,
                 uptime: Callable[[], float | None] = system_uptime,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self.require = require
        self.fallback_seconds = max(0.0, float(fallback_minutes) * 60)
        self._check = check
        self._uptime = uptime
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._ready = not require
        self._synced: bool | None = None
        self.fallback_used = False
        self._checked_at: float | None = None
        self._started = monotonic()

    def ready(self) -> bool:
        with self._lock:
            if self._ready:
                return True
            now = self._monotonic()
            if self._checked_at is not None and now - self._checked_at < SYNC_RECHECK_SECONDS:
                return False
            self._checked_at = now
        synced = self._check()
        up = self._uptime()
        waited = now - self._started
        with self._lock:
            self._synced = synced
            if synced:
                self._ready = True
            elif (up is not None and up >= self.fallback_seconds) or waited >= self.fallback_seconds:
                self._ready = True
                self.fallback_used = True
            return self._ready

    def state(self) -> dict:
        """{ready, synced, fallback_used}."""
        ready = self.ready()
        with self._lock:
            return {"ready": ready, "synced": self._synced, "fallback_used": self.fallback_used}
