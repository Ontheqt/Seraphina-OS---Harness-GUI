"""Tests for swfactory.clock: windows, the power schedule, zones across the clock change, detection, time sync."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from swfactory.clock import (LocalClock, PowerSchedule, TimeSync, Window, check_wake_alarm, detect_power, detect_zone,
                             iso, load_zone, next_wake, ntp_synchronized, parse_hhmm, parse_iso, read_wake_alarm)

LONDON = ZoneInfo("Europe/London")


def at(hhmm: str, day: str = "2026-10-21", zone: ZoneInfo = LONDON) -> datetime:
    hour, minute = map(int, hhmm.split(":"))
    return datetime.fromisoformat(day).replace(hour=hour, minute=minute, tzinfo=zone)


class WindowTests(unittest.TestCase):
    def test_parse_and_refusals(self) -> None:
        self.assertEqual(Window.parse("21:30-08:00"), Window(dt_time(21, 30), dt_time(8, 0)))
        self.assertEqual(Window.parse(" 09:00 - 17:00 ").text(), "09:00-17:00")
        for bad in ("21:30", "25:00-08:00", "21:30-08:60", "08:00-08:00", "", None, 5):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                Window.parse(bad)

    def test_contains_across_midnight(self) -> None:
        quiet = Window.parse("21:30-08:00")
        self.assertTrue(quiet.wraps)
        for inside in ("21:30", "23:59", "00:00", "07:59"):
            self.assertTrue(quiet.contains(at(inside)), inside)
        for outside in ("08:00", "12:00", "21:29"):
            self.assertFalse(quiet.contains(at(outside)), outside)
        day = Window.parse("09:00-17:00")
        self.assertTrue(day.contains(at("09:00")))
        self.assertFalse(day.contains(at("17:00")))

    def test_next_end_and_start(self) -> None:
        quiet = Window.parse("21:30-08:00")
        self.assertEqual(quiet.next_end(at("22:00")), at("08:00", "2026-10-22"))
        self.assertEqual(quiet.next_end(at("07:00")), at("08:00"))
        self.assertEqual(quiet.next_end(at("12:00")), at("08:00", "2026-10-22"))
        self.assertEqual(quiet.next_start(at("12:00")), at("21:30"))
        self.assertEqual(quiet.next_start(at("21:30")), at("21:30", "2026-10-22"))
        self.assertEqual(quiet.last_start(at("07:00")), at("21:30", "2026-10-20"))
        self.assertEqual(quiet.last_end(at("09:00")), at("08:00"))


class PowerScheduleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.power = PowerSchedule.from_config({"off": "23:00", "wake": "07:00", "quiet_before_off_minutes": 30,
                                                "quiet_after_boot_minutes": 45})

    def test_states_at_the_edges(self) -> None:
        expected = {"22:29": "on", "22:30": "quiet_before_off", "23:00": "off", "06:59": "off",
                    "07:00": "quiet_after_boot", "07:44": "quiet_after_boot", "07:45": "on"}
        for hhmm, state in expected.items():
            with self.subTest(at=hhmm):
                self.assertEqual(self.power.state(at(hhmm)), state)

    def test_next_allowed(self) -> None:
        self.assertEqual(self.power.next_allowed(at("12:00")), at("12:00"))
        self.assertEqual(self.power.next_allowed(at("22:45")), at("07:45", "2026-10-22"))
        self.assertEqual(self.power.next_allowed(at("02:00")), at("07:45"))
        self.assertEqual(self.power.next_allowed(at("07:10")), at("07:45"))

    def test_quiet_hours_auto(self) -> None:
        self.assertEqual(self.power.quiet_hours_auto(), Window.parse("21:30-08:00"))

    def test_in_power_off_with_slack(self) -> None:
        self.assertTrue(self.power.in_power_off(at("22:59")))
        self.assertTrue(self.power.in_power_off(at("22:46")))
        self.assertFalse(self.power.in_power_off(at("22:44")))
        self.assertTrue(self.power.in_power_off(at("07:14")))
        self.assertFalse(self.power.in_power_off(at("15:42")))

    def test_from_config(self) -> None:
        self.assertIsNone(PowerSchedule.from_config({"off": None}))
        self.assertIsNone(PowerSchedule.from_config(None))
        with self.assertRaises(ValueError):
            PowerSchedule.from_config({"off": "23:00", "wake": "23:00"})
        with self.assertRaises(ValueError):
            PowerSchedule.from_config({"off": "23:00", "wake": "07:00", "quiet_before_off_minutes": -1})


class ClockChangeTests(unittest.TestCase):
    def test_late_october_change_keeps_windows_in_local_time(self) -> None:
        # Europe/London leaves summer time at 02:00 local on Sunday 25 October 2026.
        quiet = Window.parse("21:30-08:00")
        before = datetime(2026, 10, 24, 22, 0, tzinfo=LONDON)
        end = quiet.next_end(before)
        self.assertEqual((end.hour, end.minute), (8, 0))
        self.assertEqual(end.utcoffset(), timedelta(0))   # winter time on the 25th
        self.assertEqual(end.astimezone(timezone.utc) - before.astimezone(timezone.utc), timedelta(hours=11))   # one more hour than the wall clock shows
        clock = LocalClock("Europe/London", now=lambda: datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc).timestamp())
        self.assertEqual(clock.hhmm(), "01:30")   # after the change, local equals UTC
        power = PowerSchedule.from_config({"off": "23:00", "wake": "07:00"})
        self.assertEqual(power.state(clock.local()), "off")

    def test_late_march_change_keeps_windows_in_local_time(self) -> None:
        # Europe/London starts summer time at 01:00 GMT on Sunday 28 March 2027.
        quiet = Window.parse("21:30-08:00")
        before = datetime(2027, 3, 27, 22, 0, tzinfo=LONDON)
        end = quiet.next_end(before)
        self.assertEqual((end.hour, end.minute, end.utcoffset()), (8, 0, timedelta(hours=1)))
        self.assertEqual(end.astimezone(timezone.utc) - before.astimezone(timezone.utc), timedelta(hours=9),
                         "one hour less than the wall clock shows")
        power = PowerSchedule.from_config({"off": "23:00", "wake": "07:00", "quiet_after_boot_minutes": 45})
        allowed = power.next_allowed(datetime(2027, 3, 28, 0, 30, tzinfo=LONDON))
        self.assertEqual((allowed.hour, allowed.minute, allowed.utcoffset()), (7, 45, timedelta(hours=1)))
        self.assertEqual(power.state(datetime(2027, 3, 28, 1, 30, tzinfo=timezone.utc).astimezone(LONDON)), "off",
                         "02:30 summer time, in the power-off window")

    def test_the_next_wake_is_local_time_on_the_day_it_wakes(self) -> None:
        """The first morning of winter time (25 Oct 2026) and of summer time (28 Mar 2027): the expected wake moves by
        the hour in UTC; an alarm armed with the evening's offset is an hour off, and is said so."""
        power = PowerSchedule.from_config({"off": "23:00", "wake": "07:00"})
        autumn = datetime(2026, 10, 24, 22, 50, tzinfo=LONDON)   # BST, the evening before the change
        self.assertEqual(next_wake(power, autumn).astimezone(timezone.utc), datetime(2026, 10, 25, 7, 0, tzinfo=timezone.utc))
        right = check_wake_alarm(power, datetime(2026, 10, 25, 7, 0, tzinfo=timezone.utc), autumn)
        self.assertEqual(right[0], "ok", right)
        state, words = check_wake_alarm(power, datetime(2026, 10, 25, 6, 0, tzinfo=timezone.utc), autumn)
        self.assertEqual(state, "hour")
        self.assertIn("06:00 on Sun 25 Oct (GMT), an hour earlier than wakes.power.wake (07:00 on Sun 25 Oct (GMT))", words)
        spring = datetime(2027, 3, 27, 22, 50, tzinfo=LONDON)    # GMT, the evening before the change
        self.assertEqual(next_wake(power, spring).astimezone(timezone.utc), datetime(2027, 3, 28, 6, 0, tzinfo=timezone.utc))
        self.assertEqual(check_wake_alarm(power, datetime(2027, 3, 28, 5, 59, 59, tzinfo=timezone.utc), spring)[0], "ok",
                         "06:59:59 for a 07:00 wake is right")
        state, words = check_wake_alarm(power, datetime(2027, 3, 28, 7, 0, tzinfo=timezone.utc), spring)
        self.assertEqual(state, "hour")
        self.assertIn("an hour later", words)
        self.assertEqual(check_wake_alarm(power, None, spring)[0], "unset")
        self.assertEqual(check_wake_alarm(power, datetime(2027, 3, 27, 6, 0, tzinfo=timezone.utc), spring)[0], "unset",
                         "an alarm already past is not set")
        self.assertEqual(check_wake_alarm(power, datetime(2027, 3, 30, 6, 0, tzinfo=timezone.utc), spring)[0], "other")

    def test_clock_and_iso(self) -> None:
        clock = LocalClock("Europe/London", now=lambda: datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc).timestamp())
        self.assertEqual(clock.hhmm(), "13:00")
        self.assertEqual(clock.stamp(), "2026-07-01T12:00:00Z")
        self.assertEqual(parse_iso("2026-07-01T12:00:00Z"), datetime(2026, 7, 1, 12, tzinfo=timezone.utc))
        self.assertIsNone(parse_iso("yesterday"))
        self.assertEqual(iso(datetime(2026, 7, 1, 13, 0, tzinfo=LONDON)), "2026-07-01T12:00:00Z")
        self.assertEqual(parse_hhmm("07:45"), dt_time(7, 45))
        with self.assertRaises(ValueError):
            load_zone("Mars/Olympus")
        with self.assertRaises(ValueError):
            load_zone("../etc/passwd")


class DetectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sf-clock-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_detect_zone_from_a_link_or_a_file(self) -> None:
        link = self.tmp / "localtime"
        os.symlink("/usr/share/zoneinfo/Europe/London", link)
        self.assertEqual(detect_zone(link, self.tmp / "none"), "Europe/London")
        plain = self.tmp / "plain"
        plain.write_text("x")
        tzfile = self.tmp / "timezone"
        tzfile.write_text("America/New_York\n")
        self.assertEqual(detect_zone(plain, tzfile), "America/New_York")
        self.assertIsNone(detect_zone(plain, self.tmp / "missing"))

    def test_detect_power_from_timers_and_the_rtc_alarm(self) -> None:
        timers = ("Wed 2026-10-21 23:00:00 BST 8h left Tue 2026-10-20 23:00:00 BST 15h ago nightly-poweroff.timer "
                  "nightly-poweroff.service\n"
                  "Wed 2026-10-21 14:30:00 BST 15min left - - apt-daily.timer apt-daily.service\n")
        alarm = self.tmp / "wakealarm"
        alarm.write_text(str(int(datetime(2026, 10, 22, 6, 0, tzinfo=timezone.utc).timestamp())) + "\n")
        found = detect_power(lambda argv: timers, alarm, zone="Europe/London")
        self.assertEqual(found["off"], "23:00")
        self.assertEqual(found["off_unit"], "nightly-poweroff.timer")
        self.assertEqual(found["wake"], "07:00")
        empty = detect_power(lambda argv: "", self.tmp / "none")
        self.assertEqual((empty["off"], empty["wake"]), (None, None))

    def test_read_the_wake_alarm(self) -> None:
        alarm = self.tmp / "wakealarm"
        alarm.write_text("1792911600\n")
        self.assertEqual(read_wake_alarm(alarm), datetime(2026, 10, 25, 7, 0, tzinfo=timezone.utc))
        for raw in ("", "0\n", "soon"):
            alarm.write_text(raw)
            self.assertIsNone(read_wake_alarm(alarm), raw)
        self.assertIsNone(read_wake_alarm(self.tmp / "missing"))

    def test_ntp_synchronized(self) -> None:
        self.assertTrue(ntp_synchronized(lambda argv: "yes\n"))
        self.assertFalse(ntp_synchronized(lambda argv: "no\n"))
        self.assertIsNone(ntp_synchronized(lambda argv: ""))


class TimeSyncTests(unittest.TestCase):
    def test_waits_until_synchronised(self) -> None:
        answers = [False, True]
        now = [0.0]
        sync = TimeSync(check=lambda: answers.pop(0), uptime=lambda: 30.0, monotonic=lambda: now[0])
        self.assertFalse(sync.ready())
        now[0] = 20.0
        self.assertTrue(sync.ready())
        self.assertFalse(sync.fallback_used)
        self.assertTrue(sync.ready())   # cached

    def test_fallback_after_the_minutes_since_boot(self) -> None:
        now = [0.0]
        up = [60.0]
        sync = TimeSync(fallback_minutes=10, check=lambda: None, uptime=lambda: up[0], monotonic=lambda: now[0])
        self.assertFalse(sync.ready())
        up[0], now[0] = 601.0, 20.0
        self.assertTrue(sync.ready())
        self.assertTrue(sync.state()["fallback_used"])

    def test_not_required(self) -> None:
        self.assertTrue(TimeSync(require=False, check=lambda: False, uptime=lambda: 0.0).ready())


if __name__ == "__main__":
    unittest.main()
