"""Tests for swfactory.notify: the content gate and the data boundary, templates and why lines, routes, quiet hours,
summaries, caps, dedupe, incidents, delivery states, the rules engine, hook events, inbound verbs and capabilities,
forged and foreign messages, rate limits, fingerprints, the unclean-stop notice and retention (Wake & Messages
W1a, W1b, W1c; docs/seraphina/WAKE_AND_MESSAGES.md §2.3, §2.7, §3, §5.9, §5.13)."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from swfactory import channels as ch
from swfactory import notify as nt
from swfactory import settings
from swfactory.channels import Inbound, Rendered, SendResult
from swfactory.clock import LocalClock, TimeSync, iso
from swfactory.config import Config
from swfactory.events import EventBus
from swfactory.notify import (Notice, Notifier, filter_text, operational_text, parse_command, render, short_text,
                              why_line)
from tests.mock_ntfy import HARNESS_TOKEN, PHONE_TOKEN, REPLY_TOKEN, MockNtfy
from tests.mock_telegram import OWNER_ID, STRANGER_ID, TOKEN, MockTelegram

TZ = "Europe/London"
ZONE = ZoneInfo(TZ)
TAILNET = {"suffix": "tail0123.ts.net", "addresses": ["100.101.102.103"]}
ENV = {"SERAPHINA_NTFY_TOKEN": HARNESS_TOKEN, "SERAPHINA_NTFY_REPLY_TOKEN": REPLY_TOKEN,
       "SERAPHINA_TELEGRAM_BOT_TOKEN": TOKEN}
TERMS = ["diabetes", "Dr Rahman", "St Anne's clinic"]
MASTER = "master-token-for-tests-0123456789"


def ntfy_entry(publish_url: str = "unix:/run/ntfy/ntfy.sock", **extra: Any) -> dict:
    return {"type": "ntfy", "enabled": True, "publish_url": publish_url,
            "public_url": "https://host.tail0123.ts.net:8445", "topic": "seraphina", "taps_topic": "seraphina-taps",
            "chat_topic": "seraphina-chat", "token_env": "SERAPHINA_NTFY_TOKEN",
            "reply_token_env": "SERAPHINA_NTFY_REPLY_TOKEN", "operational": True, "operational_names": False,
            "model_text": "clean_only", **extra}


def telegram_entry(**extra: Any) -> dict:
    return {"type": "telegram", "enabled": True, "token_env": "SERAPHINA_TELEGRAM_BOT_TOKEN", "operational": False,
            "operational_names": False, "model_text": "never", **extra}


def local(text: str) -> datetime:
    """An aware datetime from "2026-10-21 21:45" in the owner's zone."""
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=ZONE)


class FakeTime:
    """One fake clock for the notifier's LocalClock, its monotonic clock and the channels' now()."""

    def __init__(self, text: str = "2026-10-21 12:00") -> None:
        self.t = local(text).timestamp()

    def __call__(self) -> float:
        return self.t

    def set(self, text: str) -> None:
        self.t = local(text).timestamp()

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeChannel(ch.Channel):
    """A channel that records what it was given; results queue SendResults for the next sends."""

    def __init__(self, name: str, entry: Any, environ: Any, state_dir: Path, **kwargs: Any) -> None:
        kwargs = {k: v for k, v in kwargs.items() if k in ("privacy", "now")}
        super().__init__(name, entry, environ, Path(state_dir) / f"{name}.json", **kwargs)
        self.type = entry().get("type")
        self.can_receive = True
        self.has_buttons = True
        self.sent: list[Rendered] = []
        self.updates: list[tuple[str, Rendered]] = []
        self.finished: list[tuple[Inbound, str | None]] = []
        self.results: list[SendResult] = []

    def ready(self) -> bool:
        return True

    def buttons_ready(self) -> bool:
        return True

    def can_listen(self) -> bool:
        return False

    def send(self, rendered: Rendered, idempotency_key: str) -> SendResult:
        self.sent.append(rendered)
        if self.results:
            return self.results.pop(0)
        return SendResult("sent", remote_id=f"{self.name}-{len(self.sent)}")

    def update(self, remote_id: str, rendered: Rendered) -> SendResult:
        self.updates.append((remote_id, rendered))
        return SendResult("sent", remote_id=remote_id)

    def finish(self, inbound: Inbound, answer: str | None) -> None:
        self.finished.append((inbound, answer))

    def paired(self) -> list[dict]:
        return [{"id": OWNER_ID, "username": "owner_name"}] if self.type == "telegram" else []


class FakeFactory:
    """What the Notifier asks of the Factory: jobs, plans, items, settings saves and secrets."""

    def __init__(self, config: Config, environ: dict) -> None:
        self.config = config
        self.environ = environ
        self.jobs_list: list[dict] = []
        self.plans_list: list[dict] = []
        self.items_answer: dict | None = None
        self.notifier: Notifier | None = None

    def jobs(self) -> list[dict]:
        return list(self.jobs_list)

    def plans(self) -> list[dict]:
        return list(self.plans_list)

    def items(self, **kwargs: Any) -> dict | None:
        return self.items_answer

    def update_config(self, patch: dict) -> dict:
        def saved(new: Config) -> None:
            self.config = new
            if self.notifier is not None:
                self.notifier.reload(new)
        settings.update_config(self.config, patch, on_saved=saved)
        return {}

    def set_secret(self, name: str, value: str) -> dict:
        self.environ[name] = value
        return {"name": name, "set": True}

    def secret_values(self) -> list[str]:
        return [MASTER]


class NotifyCase(unittest.TestCase):
    """A Notifier on a temporary data dir, a fake clock, fake channels (unless real_channels) and an EventBus."""

    channels: dict[str, dict] = {"phone": ntfy_entry(), "telegram": telegram_entry()}
    real_channels = False

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sf-notify-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.time = FakeTime()
        self.environ = dict(ENV)
        self.terms_file = self.tmp / "terms.txt"
        self.terms_file.write_text("\n".join(TERMS) + "\n")
        os.chmod(self.terms_file, 0o600)
        self.config = self.make_config()
        self.bus = EventBus(secrets_provider=lambda: list(self.environ.values()))
        self.events = self.bus.subscribe()
        self.factory = FakeFactory(self.config, self.environ)
        self.notifier = self.make_notifier()

    def make_config(self, **sections: Any) -> Config:
        raw: dict[str, Any] = {
            "data_dir": str(self.tmp / "data"), "server": {"token": MASTER},
            "harness": {"sandbox": "bwrap"},
            "repos": [],
            "wakes": {"timezone": TZ, "time_sync": {"require": False}},
            "notify": {"channels": json.loads(json.dumps(self.channels)), "sensitive_terms_file": str(self.terms_file)},
        }
        for key, value in sections.items():
            if isinstance(value, dict) and isinstance(raw.get(key), dict):
                raw[key] = {**raw[key], **value}
            else:
                raw[key] = value
        path = self.tmp / "config.json"
        path.write_text(json.dumps(raw, indent=2))
        os.chmod(path, 0o600)
        return Config(path, raw)

    def make_notifier(self, **kwargs: Any) -> Notifier:
        params: dict[str, Any] = dict(
            clock=LocalClock(TZ, now=self.time), environ=self.environ, tailnet=lambda: TAILNET,
            server_yml=lambda: None, time_sync=TimeSync(require=False), monotonic=self.time,
            run=lambda argv, input_text=None, timeout=10.0: "")
        if not self.real_channels:
            params["channel_factory"] = FakeChannel
        params.update(kwargs)
        notifier = Notifier(self.factory.config, self.bus, self.factory, **params)
        self.factory.notifier = notifier
        notifier.load()
        return notifier

    def restart(self) -> Notifier:
        """A new Notifier on the same data dir (a server restart)."""
        self.notifier = self.make_notifier()
        return self.notifier

    def channel(self, name: str) -> FakeChannel:
        return self.notifier.channel(name)   # type: ignore[return-value]

    def drain(self) -> list[Any]:
        found = []
        while not self.events.empty():
            found.append(self.events.get_nowait())
        return found

    def job_notice(self, number: str = "003", stage: str = "needs-human", repo: str = "my-app",
                   slug: str = "rate-limit-signup") -> dict:
        job = {"repo": repo, "id": f"{number}-{slug}", "stage": stage, "failed_step": "review" if stage == "failed"
               else None, "updated_at": iso(self.notifier.clock.utc() - timedelta(minutes=5))}
        self.notifier._job_rule(job, None)
        return self.latest()

    def latest(self, kind: str | None = None) -> dict:
        rows = self.notifier.messages(kind=kind)["messages"]
        self.assertTrue(rows, f"no {kind or 'notice'} in the inbox")
        return rows[0]

    def entry(self, message_id: str) -> dict:
        return self.notifier._entries[message_id]


def entry_for(privacy: str) -> dict:
    return ntfy_entry() if privacy == ch.PRIVACY_TAILNET else telegram_entry()


def gate(notice: Notice, privacy: str, entry: dict | None = None, **kwargs: Any) -> tuple[Rendered | None, str]:
    return render(notice, "phone" if privacy == ch.PRIVACY_TAILNET else "telegram", entry or entry_for(privacy),
                  privacy, terms=kwargs.pop("terms", TERMS), secrets=kwargs.pop("secrets", [HARNESS_TOKEN]),
                  link_base=kwargs.pop("link_base", None), safe_repos=kwargs.pop("safe_repos", set()),
                  clock=kwargs.pop("clock", LocalClock(TZ)), **kwargs)


def job_needs_human(**fields: Any) -> Notice:
    values = {"repo": "my-app", "job": "003-rate-limit-signup", "stage": "needs-human", "why": "waiting",
              "since": "2026-10-21T10:10:00Z", **fields}
    title, body = operational_text(Notice("job.needs_human", "operational", "needs_you", "default", "", "",
                                          fields=values), names=True, safe_repos={"my-app"})
    return Notice("job.needs_human", "operational", "needs_you", "default", title, body, fields=values)


BOTH = (ch.PRIVACY_TAILNET, ch.PRIVACY_THIRD_PARTY)


class ContentGateTests(unittest.TestCase):
    """S24, S25, S31: the delivery matrix, every class on both privacy classes."""

    def test_short_class_on_both_channels(self) -> None:
        notice = Notice("test", "short", "needs_you", "default", "Test", "This is a test from your server. Tap Got it.")
        for privacy in BOTH:
            rendered, _why = gate(notice, privacy)
            self.assertEqual(rendered.text, "This is a test from your server. Tap Got it.")

    def test_operational_on_the_private_channel_hides_project_names_by_default(self) -> None:
        rendered, why = gate(job_needs_human(), ch.PRIVACY_TAILNET)
        self.assertEqual(rendered.text, "A job (003) is waiting for your decision. See it in the cockpit (Jobs, then 003).")
        self.assertNotIn("my-app", rendered.title + rendered.text + rendered.why)
        self.assertNotIn("rate-limit", rendered.title + rendered.text + rendered.why, "the slug never leaves")
        self.assertEqual(rendered.why, "Job 003 has waited since 11:10")
        self.assertEqual(why, "Sent without project names")

    def test_operational_names_only_for_safe_repositories(self) -> None:
        named = ntfy_entry(operational_names=True)
        rendered, _ = gate(job_needs_human(), ch.PRIVACY_TAILNET, named, safe_repos={"my-app"})
        self.assertTrue(rendered.text.startswith("my-app job 003 is waiting for your decision"))
        rendered, _ = gate(job_needs_human(), ch.PRIVACY_TAILNET, named, safe_repos={"other"})
        self.assertTrue(rendered.text.startswith("A job (003)"), "a repository not marked safe stays hidden")

    def test_operational_on_telegram_is_the_short_notice_unless_allowed(self) -> None:
        rendered, why = gate(job_needs_human(), ch.PRIVACY_THIRD_PARTY)
        self.assertEqual(rendered.text, "A job is waiting for your decision. Open the cockpit.")
        self.assertEqual(rendered.why, "Waiting since 11:10", "the third-party why line names no job")
        self.assertEqual(why, "Heads-up only: Telegram can read it")
        allowed = telegram_entry(operational=True)
        rendered, _ = gate(job_needs_human(), ch.PRIVACY_THIRD_PARTY, allowed)
        self.assertEqual(rendered.text, "A job (003) is waiting for your decision. See it in the cockpit (Jobs, then 003).")

    def test_model_text_reaches_only_the_private_channel_from_a_clean_turn(self) -> None:
        clean = Notice("persona", "model", "persona", "default", "Backup check", "The backup check you asked for "
                       "passed.", sender="seraphina", persona="seraphina", fields={"why": "wake",
                                                                                    "wake_at": "2026-10-21T13:05:00Z"})
        rendered, _ = gate(clean, ch.PRIVACY_TAILNET)
        self.assertEqual(rendered.text, "The backup check you asked for passed.")
        self.assertEqual(rendered.why, "From seraphina's 14:05 check")
        rendered, _ = gate(clean, ch.PRIVACY_THIRD_PARTY)
        self.assertEqual(rendered.text, "You have a message. Open the cockpit to read it.")
        self.assertNotIn("seraphina", (rendered.title + rendered.text + rendered.why).lower(),
                         "a third-party channel never names a persona")
        never = ntfy_entry(model_text="never")
        self.assertEqual(gate(clean, ch.PRIVACY_TAILNET, never)[0].text,
                         "seraphina has a message for you. Open the cockpit to read it.")
        for taint in ("private", "untrusted"):
            tainted = Notice("persona", "model", "persona", "default", "Backup", "Your diabetes check went fine.",
                             sender="seraphina", persona="seraphina", taint=(taint,))
            for privacy in BOTH:
                rendered, why = gate(tainted, privacy)
                self.assertNotIn("diabetes", rendered.text)
                self.assertIn("Open the cockpit", rendered.text)
            self.assertIn(nt.TAINT_WHY[taint], gate(tainted, ch.PRIVACY_TAILNET)[1])

    def test_owner_text_on_a_third_party_channel_is_always_the_short_notice(self) -> None:
        notice = Notice("reminder", "owner_text", "persona", "default", "Reminder", "Take the bins out.",
                        show_on=("phone", "telegram"), fields={"why": "reminder", "set_at": "2026-10-20T09:00:00Z"})
        rendered, why = gate(notice, ch.PRIVACY_THIRD_PARTY)
        self.assertEqual(rendered.text, "A reminder you set is due.")
        self.assertEqual(rendered.why, "Set on 20 Oct")
        rendered, _ = gate(notice, ch.PRIVACY_TAILNET)
        self.assertEqual(rendered.text, "Take the bins out.", "ticked for the private channel")
        unticked = Notice("reminder", "owner_text", "persona", "default", "Reminder", "Take the bins out.")
        self.assertEqual(gate(unticked, ch.PRIVACY_TAILNET)[0].text, "A reminder you set is due.")
        sensitive = Notice("reminder", "owner_text", "persona", "default", "Reminder", "Call Dr Rahman at nine.",
                           show_on=("phone",))
        rendered, why = gate(sensitive, ch.PRIVACY_TAILNET)
        self.assertEqual(rendered.text, "A reminder you set is due.")
        self.assertEqual(why, "Heads-up only: it holds one of your sensitive words")

    def test_unknown_class_never_leaves(self) -> None:
        notice = Notice("test", "short", "needs_you", "default", "x", "y")
        notice.klass = "secret"
        self.assertEqual(gate(notice, ch.PRIVACY_TAILNET), (None, "Never sent: it stays in the cockpit"))

    def test_filters_force_the_short_notice_never_a_trim(self) -> None:
        cases = {"link": "Look at https://x.example/?d=1 now.", "markdown": "This is **bold** text.",
                 "html": "Click <a href=x>here</a>.", "secret": f"Token {HARNESS_TOKEN[:3]}{HARNESS_TOKEN[3:]} leaked.",
                 "token-like": "Key AAAAbbbbCCCCddddEEEEffff here.", "number": "Card 123456789 was used.",
                 "sensitive": "About your DIABETES appointment."}
        for reason, body in cases.items():
            notice = Notice("persona", "model", "persona", "default", "A note", body, sender="seraphina",
                            persona="seraphina")
            rendered, why = gate(notice, ch.PRIVACY_TAILNET)
            self.assertEqual(rendered.text, "seraphina has a message for you. Open the cockpit to read it.", reason)
            self.assertEqual(why, f"Heads-up only: {nt.REASON_WHY[reason]}", reason)
            self.assertNotIn(body, rendered.text)
        long_title = Notice("persona", "model", "persona", "default", "x" * 61, "Fine.", sender="seraphina",
                            persona="seraphina")
        self.assertIn("Open the cockpit", gate(long_title, ch.PRIVACY_TAILNET)[0].text)
        long_body = Notice("persona", "model", "persona", "default", "Fine", "word " * 61, sender="seraphina",
                           persona="seraphina")
        self.assertEqual(gate(long_body, ch.PRIVACY_TAILNET)[1], "Heads-up only: it was too long for a phone notice")

    def test_a_sensitive_term_in_an_operational_field_forces_the_short_notice(self) -> None:
        notice = job_needs_human(repo="diabetes-app")
        named = ntfy_entry(operational_names=True)
        rendered, why = gate(notice, ch.PRIVACY_TAILNET, named, safe_repos={"diabetes-app"})
        self.assertEqual(rendered.text, "A job is waiting for your decision. Open the cockpit.")
        self.assertIn("sensitive", why)

    def test_secrets_are_redacted_from_every_rendered_text(self) -> None:
        notice = Notice("notice", "operational", "needs_you", "default", "Your server", f"x {HARNESS_TOKEN} y")
        rendered, _ = gate(notice, ch.PRIVACY_TAILNET)
        self.assertNotIn(HARNESS_TOKEN, json.dumps([rendered.title, rendered.text, rendered.why]))

    def test_links_come_only_from_the_cockpit_url(self) -> None:
        rendered, _ = gate(job_needs_human(), ch.PRIVACY_TAILNET, link_base="https://cockpit.tail0123.ts.net",
                           message_id="M-000012")
        self.assertEqual(rendered.link, "https://cockpit.tail0123.ts.net/#/messages/m/M-000012")
        self.assertIsNone(gate(job_needs_human(), ch.PRIVACY_TAILNET)[0].link)

    def test_buttons_by_sender(self) -> None:
        server = gate(job_needs_human(), ch.PRIVACY_TAILNET)[0]
        self.assertEqual([b.verb for b in server.buttons], ["ack", "snooze_1h", "quiet_morning"])
        persona = gate(Notice("persona", "model", "persona", "default", "Hi", "Fine.", sender="seraphina",
                              persona="seraphina"), ch.PRIVACY_THIRD_PARTY)[0]
        self.assertEqual([(b.verb, b.label) for b in persona.buttons],
                         [("ack", "Got it"), ("snooze_1h", "Snooze 1 h"), ("stop", "Stop the assistant")])
        summary = gate(Notice("summary", "operational", "digest", "default", "S", "x", fields={"which": "Morning",
                                                                                                 "n": 1, "title": "S",
                                                                                                 "text": "Urgent: 1"}),
                       ch.PRIVACY_TAILNET)[0]
        self.assertEqual([b.verb for b in summary.buttons], ["ack"])


class FilterTests(unittest.TestCase):
    def test_terms_match_case_insensitively_on_word_boundaries(self) -> None:
        self.assertEqual(filter_text("Influenza season", terms=["flu"], secrets=[])[1], [])
        self.assertEqual(filter_text("The FLU is back", terms=["flu"], secrets=[])[1], ["sensitive"])
        self.assertEqual(filter_text("Meet Dr   Rahman", terms=["Dr Rahman"], secrets=[])[1], ["sensitive"])
        self.assertEqual(filter_text("St Anne's clinic at 9", terms=TERMS, secrets=[])[1], ["sensitive"])

    def test_cleaning_removes_format_and_control_characters(self) -> None:
        cleaned, reasons = filter_text("dia​betes\x07 and \U000e0041", terms=["diabetes"], secrets=[])
        self.assertEqual(reasons, ["sensitive"], "a zero-width space does not hide a term")
        self.assertNotIn("\x07", cleaned)

    def test_ordinary_text_passes(self) -> None:
        self.assertEqual(filter_text("Job 003 failed at the review step. Nothing was merged.", terms=TERMS,
                                     secrets=["s3cr3t-value"])[1], [])

    def test_read_terms(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="sf-terms-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        path = tmp / "terms.txt"
        path.write_text("# my words\ndiabetes\n\n  Dr Rahman  \n")
        os.chmod(path, 0o644)
        terms, status = nt.read_terms(path)
        self.assertEqual(terms, ["diabetes", "Dr Rahman"])
        self.assertEqual((status["count"], status["mode_ok"]), (2, False))
        self.assertEqual(nt.read_terms(tmp / "missing")[0], [])


class TemplateTests(unittest.TestCase):
    """Every template and why line passes the terms filter and the length limits (§5.9)."""

    def test_every_short_template_fits_and_passes(self) -> None:
        fields = {"which": "Morning", "n": 6, "time": "14:02", "until": "08:00", "channel_words": "Telegram",
                  "count": 14, "minutes": 15}
        for key in nt.SHORT:
            for third in (False, True):
                title, text = short_text(key, third, persona="Seraphina", fields=fields)
                self.assertLessEqual(len(title), nt.MAX_TITLE, key)
                self.assertLessEqual(len(text), nt.MAX_BODY, key)
                self.assertEqual(filter_text(title + "\n" + text, terms=TERMS, secrets=[HARNESS_TOKEN])[1], [], key)
                self.assertNotIn("{", title + text, key)
                if third:
                    self.assertNotIn("Seraphina", text if key not in ("stopped",) else "", key)

    def test_third_party_forms_never_name_a_persona(self) -> None:
        for key in ("message", "answer", "stopped", "checkin"):
            title, text = short_text(key, True, persona="Seraphina", fields={"time": "14:02"})
            self.assertNotIn("Seraphina", title + text, key)

    def test_operational_texts_fit_and_pass(self) -> None:
        fields = {"repo": "my-app", "job": "003-x", "step": "review", "plan": "P004-x", "stage": "ready",
                  "subject": "nightly", "subject_allowed": True, "channel_words": "Telegram", "others": True,
                  "time": "15:42", "off": "23:00", "what": "forged", "reminder": True}
        for kind in (*nt.RULE_IDS, "inbound.alarm"):
            title, text = operational_text(Notice(kind, "operational", "needs_you", "default", "t", "b",
                                                  fields=fields), names=False, safe_repos=set())
            self.assertLessEqual(len(title), nt.MAX_TITLE, kind)
            self.assertLessEqual(len(text), nt.MAX_BODY, kind)
            self.assertEqual(filter_text(title + "\n" + text, terms=TERMS, secrets=[])[1], [], kind)

    def test_hook_subjects_only_from_the_allowlist(self) -> None:
        fields = {"subject": "nightly", "subject_allowed": False}
        _t, text = operational_text(Notice("backup.failed", "operational", "needs_you", "high", "", "", fields=fields),
                                    names=True, safe_repos=set())
        self.assertEqual(text, "A backup failed. See the cockpit for what to do.")
        fields["subject_allowed"] = True
        _t, text = operational_text(Notice("backup.failed", "operational", "needs_you", "high", "", "", fields=fields),
                                    names=True, safe_repos=set())
        self.assertEqual(text, "The nightly backup failed. See the cockpit for what to do.")

    def test_why_lines(self) -> None:
        clock = LocalClock(TZ, now=lambda: local("2026-10-21 14:30").timestamp())
        def why(kind: str, privacy: str = ch.PRIVACY_TAILNET, **fields: Any) -> str:
            return why_line(Notice(kind, "operational", "needs_you", "default", "", "", sender="seraphina"
                                   if kind == "persona" else "server", fields=fields), privacy, clock)
        self.assertEqual(why("persona", why="wake", wake_at="2026-10-21T13:05:00Z"), "From seraphina's 14:05 check")
        self.assertEqual(why("persona", ch.PRIVACY_THIRD_PARTY, why="wake", wake_at="2026-10-21T13:05:00Z"),
                         "From a scheduled check at 14:05")
        self.assertEqual(why("reply", why="reply", asked_at="2026-10-21T12:35:00Z"), "Because you asked at 13:35")
        self.assertEqual(why("job.needs_human", why="waiting", since="2026-10-21T10:10:00Z", job="003-x"),
                         "Job 003 has waited since 11:10")
        self.assertEqual(why("job.needs_human", ch.PRIVACY_THIRD_PARTY, why="waiting", since="2026-10-21T10:10:00Z",
                             job="003-x"), "Waiting since 11:10")
        self.assertEqual(why("job.failed", snoozed_at="2026-10-21T12:07:00Z"), "Snoozed at 13:07")
        self.assertEqual(why("reminder", why="reminder", set_at="2026-10-20T09:00:00Z"), "Reminder set on 20 Oct")
        self.assertEqual(why("backup.failed", at="2026-10-21T21:10:00Z"), "Reported at 22:10")
        self.assertEqual(why("job.failed", why="failed", since="2026-10-21T12:41:00Z"), "Failed at 13:41")


class GrammarTests(unittest.TestCase):
    """S37: a command only as the whole message."""

    def test_whole_message_commands(self) -> None:
        for text, verb in (("stop", "stop"), ("/stop", "stop"), ("  STOP  ", "stop"), ("Stop.", "stop"),
                           ("snooze 1h", "snooze_1h"), ("/snooze   3h", "snooze_3h"), ("snooze morning",
                                                                                     "snooze_morning"),
                           ("quiet 3h", "quiet_3h"), ("quiet morning", "quiet_morning"), ("mute", "mute"),
                           ("status", "status"), ("/help", "help"), ("/stop@quiet_helper_bot", "stop")):
            self.assertEqual(parse_command(text)[:2], ("command", verb), text)
        self.assertEqual(parse_command("mute 2"), ("command", "mute", "2"))

    def test_free_text_and_near_misses(self) -> None:
        self.assertEqual(parse_command("Stop worrying about the backup, it's fine")[0], "text")
        self.assertEqual(parse_command("yes, go ahead and update Open WebUI")[0], "text")
        self.assertEqual(parse_command("stopp")[:2], ("near", "stop"))
        self.assertEqual(parse_command("stop now")[:2], ("near", "stop"))
        self.assertEqual(parse_command("snooze")[:2], ("near", "snooze"))
        self.assertEqual(parse_command("approve 003")[:2], ("refused", "approve"))
        self.assertEqual(parse_command("resume")[:2], ("refused", "resume"))
        self.assertEqual(parse_command("restart")[:2], ("refused", "restart"))
        self.assertEqual(parse_command("hello there")[0], "text")



class RouteTests(NotifyCase):
    def test_needs_you_goes_everywhere_and_the_inbox_always(self) -> None:
        message = self.job_notice()
        self.assertEqual(sorted(message["deliveries"]), ["phone", "telegram"])
        self.notifier.tick()
        phone, telegram = self.channel("phone"), self.channel("telegram")
        self.assertEqual(phone.sent[0].text, "A job (003) is waiting for your decision. See it in the cockpit (Jobs, "
                                             "then 003).")
        self.assertEqual(phone.sent[0].why, "Job 003 has waited since 11:55")
        self.assertEqual(telegram.sent[0].text, "A job is waiting for your decision. Open the cockpit.")
        detail = self.notifier.message(message["id"])
        self.assertIn("my-app job 003", detail["message"]["body"], "the inbox shows everything")
        self.assertEqual({d["channel"]: d["state"] for d in detail["deliveries"]}, {"phone": "sent", "telegram": "sent"})

    def test_private_first_prefers_the_private_channel(self) -> None:
        result = self.notifier.notify(Notice("summary", "operational", "digest", "default", "S", "x",
                                             fields={"which": "Morning", "n": 1, "title": "S", "text": "Urgent: 1"}))
        self.assertEqual(list(result["deliveries"]), ["phone"])

    def test_default_route_is_the_inbox_only(self) -> None:
        result = self.notifier.notify(Notice("notice", "operational", "default", "default", "Hello", "World"))
        self.assertEqual(result["deliveries"], {})
        self.assertEqual(result["state"], "inbox")

    def test_origin_route_answers_on_the_channel_the_owner_wrote_from(self) -> None:
        result = self.notifier.notify(Notice("reply", "model", "reply", "default", "Answer", "Done.",
                                             sender="seraphina", persona="seraphina", taint=("private",),
                                             origin_channel="telegram"))
        self.assertEqual(list(result["deliveries"]), ["telegram"])
        self.notifier.tick()
        self.assertEqual(self.channel("telegram").sent[0].text,
                         "You have an answer waiting. Read it in the cockpit on your computer, under Messages.")


class TelegramOnlyTests(NotifyCase):
    channels = {"telegram": telegram_entry()}

    def test_private_first_falls_back_to_telegram_as_a_short_notice(self) -> None:
        self.time.set("2026-10-21 08:30")
        self.time.advance(-3600 * 2)                          # 06:30, inside do-not-disturb time
        self.job_notice()
        self.time.set("2026-10-21 08:05")
        self.notifier.tick()
        summary = self.latest("summary")
        self.assertEqual(list(summary["deliveries"]), ["telegram"])
        sent = [r for r in self.channel("telegram").sent if "summary" in r.text]
        self.assertEqual(sent[0].text, "Morning summary: 1 thing. Open the cockpit.")


class QuietHoursTests(NotifyCase):
    def test_a_default_notice_at_21_45_is_held_and_counted_in_the_morning_summary(self) -> None:
        self.time.set("2026-10-21 21:45")
        message = self.job_notice()
        self.notifier.tick()
        entry = self.entry(message["id"])
        self.assertEqual(entry["deliveries"]["phone"]["state"], "held")
        self.assertIn("do-not-disturb until 08:00", entry["deliveries"]["phone"]["why"])
        evening = self.latest("summary")
        self.assertEqual(evening["priority"], "low")
        self.assertTrue(evening["title"].startswith("Evening summary"))
        self.assertIn("sleeps 23:00-07:00", evening["body"])
        self.time.set("2026-10-22 08:05")
        self.notifier.tick()
        morning = self.latest("summary")
        self.assertTrue(morning["title"].startswith("Morning summary: 1 thing"))
        self.assertEqual(morning["priority"], "default")
        self.assertIn("Needs you: 1", morning["body"])
        self.assertIn("Held overnight: 1", morning["body"])
        self.assertNotIn("my-app", morning["body"], "summaries carry counts only")
        self.assertEqual(self.entry(message["id"])["summary"], morning["id"])
        phone_texts = [r.text for r in self.channel("phone").sent]
        self.assertFalse(any("waiting for your decision" in t for t in phone_texts), "held, not sent in a burst")
        self.assertTrue(any(t.startswith("Needs you: 1") for t in phone_texts))

    def test_a_breakthrough_alert_at_22_10_buzzes_and_a_repeat_does_not(self) -> None:
        self.time.set("2026-10-21 22:10")
        first = self.notifier.owner_event({"kind": "backup.failed", "subject": "nightly"})
        self.notifier.tick()
        sent = [r for r in self.channel("phone").sent if r.priority == "high"]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].text, "The nightly backup failed. See the cockpit for what to do.")
        self.assertEqual(self.channel("telegram").sent[-1].text,
                         "Urgent: something on your server needs you now. Open the cockpit.")
        self.time.advance(600)
        again = self.notifier.owner_event({"kind": "backup.failed", "subject": "nightly"})
        self.assertEqual(again, {"message": None, "deduped": True})
        self.notifier.tick()
        self.assertEqual(len([r for r in self.channel("phone").sent if r.priority == "high"]), 1)
        self.assertTrue(first["message"])

    def test_without_breakthrough_a_high_alert_waits_for_the_morning(self) -> None:
        self.factory.update_config({"notify": {"quiet_breakthrough": []}})
        self.time.set("2026-10-21 22:10")
        result = self.notifier.owner_event({"kind": "service.down", "subject": "nas"})
        self.notifier.tick()
        self.assertEqual(self.entry(result["message"])["deliveries"]["phone"]["state"], "held")

    def test_an_owner_quiet_holds_below_high_and_ends_with_one_summary(self) -> None:
        self.time.set("2026-10-21 12:00")
        self.notifier._apply("quiet_3h", None, Inbound(channel="phone", kind="command"), self.notifier.clock.utc(),
                             False)
        confirm = self.latest("confirm")
        self.assertEqual(confirm["priority"], "low")
        self.notifier.tick()
        self.assertTrue(any("Quiet until 15:00" in r.text for r in self.channel("phone").sent),
                        "the quiet is confirmed at once")
        message = self.job_notice()
        alert = self.notifier.owner_event({"kind": "backup.failed"})
        self.notifier.tick()
        self.assertEqual(self.entry(message["id"])["deliveries"]["phone"]["state"], "held")
        self.assertEqual(self.entry(alert["message"])["deliveries"]["phone"]["state"], "sent")
        self.time.set("2026-10-21 15:01")
        self.notifier.tick()
        quiet = self.latest("summary")
        self.assertTrue(quiet["title"].startswith("Quiet time summary: 1 thing"))
        self.assertIn("Needs you: 1", quiet["body"])

    def test_retry_sends_a_held_notice_now(self) -> None:
        self.time.set("2026-10-21 23:30")
        message = self.job_notice()
        self.notifier.tick()
        self.assertEqual(self.entry(message["id"])["deliveries"]["phone"]["state"], "held")
        self.notifier.message_action(message["id"], "retry")
        self.notifier.tick()
        self.assertEqual(self.entry(message["id"])["deliveries"]["phone"]["state"], "sent")


class CapTests(NotifyCase):
    def test_the_phone_cap_holds_overflow_but_never_a_high_alert(self) -> None:
        self.factory.update_config({"notify": {"phone_max_per_day": 3, "channels": {"telegram": {"enabled": False}}}})
        ids = []
        for number in range(4):
            ids.append(self.job_notice(number=f"00{number}")["id"])
        self.notifier.tick()
        states = [self.entry(i)["deliveries"]["phone"]["state"] for i in ids]
        self.assertEqual(states, ["sent", "sent", "sent", "held"])
        self.assertIn("in the next summary", self.entry(ids[3])["deliveries"]["phone"]["why"])
        alert = self.notifier.owner_event({"kind": "backup.failed", "subject": "nightly"})
        self.notifier.tick()
        self.assertEqual(self.entry(alert["message"])["deliveries"]["phone"]["state"], "sent")

    def test_summaries_fit_300_characters_with_more_in_the_cockpit(self) -> None:
        self.time.set("2026-10-21 23:00")
        notifier = self.notifier
        parts = [(f"Category number {i} with a long label", 1) for i in range(20)]
        notifier._summary_counts = lambda *a, **k: (parts, [], 20)   # type: ignore[method-assign]
        notifier._summary_once("evening", "2026-10-21", notifier.clock.utc(), since=notifier.clock.utc())
        summary = self.latest("summary")
        self.assertLessEqual(len(summary["body"]), nt.MAX_BODY)
        self.assertRegex(summary["body"], r"\+\d+ more in the cockpit")


class DedupeTests(NotifyCase):
    def test_the_same_key_updates_the_phone_notification(self) -> None:
        notice = Notice("notice", "operational", "needs_you", "default", "Disk", "Disk check one.",
                        dedupe_key="disk:1")
        first = self.notifier.notify(notice)
        self.notifier.tick()
        again = self.notifier.notify(Notice("notice", "operational", "needs_you", "default", "Disk", "Disk check one.",
                                            dedupe_key="disk:1"))
        self.assertTrue(again["deduped"])
        changed = self.notifier.notify(Notice("notice", "operational", "needs_you", "default", "Disk",
                                              "Disk check two.", dedupe_key="disk:1"))
        self.assertEqual(changed["id"], first["id"])
        self.notifier.tick()
        phone = self.channel("phone")
        self.assertEqual(len(phone.sent), 1)
        self.assertEqual(phone.updates[-1][1].text, "Disk check two.")
        self.assertEqual(phone.updates[-1][1].sequence, first["id"], "ntfy replaces it by sequence id")


class IncidentTests(NotifyCase):
    def test_alert_once_remind_after_six_hours_at_most_three_and_close(self) -> None:
        opened = self.notifier.owner_event({"kind": "backup.failed", "subject": "nightly"})
        self.notifier.tick()
        reminders = []
        for _ in range(5):
            self.time.advance(6 * 3600 + 60)
            if not 8 <= self.notifier.clock.local().hour < 21:
                self.time.set("2026-10-23 12:00")
            self.notifier.tick()
            reminders = [m for m in self.notifier.messages(kind="backup.failed")["messages"] if m["id"] != opened["message"]]
        self.assertEqual(len(reminders), 3)
        self.assertTrue(all(m["body"].startswith("Still: ") for m in reminders))
        closed = self.notifier.owner_event({"kind": "backup.ok", "subject": "nightly"})
        self.assertTrue(closed["message"])
        self.assertEqual(self.latest("backup.ok")["body"], "The nightly backup is working again.")
        self.assertIsNotNone(self.notifier._rules["incidents"]["backup.failed:nightly"]["closed"])
        self.assertEqual(self.notifier.owner_event({"kind": "backup.ok", "subject": "nightly"}),
                         {"message": None, "recorded": True})

    def test_got_it_silences_only_that_signature(self) -> None:
        backup = self.notifier.owner_event({"kind": "backup.failed", "subject": "nightly"})
        service = self.notifier.owner_event({"kind": "service.down", "subject": "nas"})
        self.notifier.message_action(backup["message"], "ack")
        self.time.advance(6 * 3600 + 60)
        self.notifier.tick()
        self.assertEqual(len(self.notifier.messages(kind="backup.failed")["messages"]), 1)
        self.assertEqual(len(self.notifier.messages(kind="service.down")["messages"]), 2)
        self.assertTrue(service["message"])

    def test_a_different_signature_alerts_at_once(self) -> None:
        self.notifier.owner_event({"kind": "backup.failed", "subject": "nightly"})
        other = self.notifier.owner_event({"kind": "backup.failed", "subject": "weekly"})
        self.assertTrue(other["message"])


class DeliveryStateTests(NotifyCase):
    def test_unknown_is_never_resent_automatically(self) -> None:
        phone = self.channel("phone")
        phone.results = [SendResult("unknown", error="no answer")]
        message = self.job_notice()
        self.notifier.tick()
        for _ in range(3):
            self.time.advance(3600)
            self.notifier.tick()
        self.assertEqual(len(phone.sent), 1)
        delivery = self.entry(message["id"])["deliveries"]["phone"]
        self.assertEqual(delivery["state"], "unknown")
        self.assertIn("may have arrived", delivery["why"])
        self.notifier.message_action(message["id"], "resend")
        self.notifier.tick()
        self.assertEqual(len(phone.sent), 2)
        self.assertEqual(self.entry(message["id"])["deliveries"]["phone"]["state"], "sent")

    def test_a_channel_that_raises_while_sending_is_unknown_never_resent(self) -> None:
        """DESIGN §40: once a delivery is "sending" the request may have left before the channel raised (a full disk
        after ntfy's 200), so it is unknown: never failed and retried."""
        phone = self.channel("phone")

        def send(rendered: Rendered, key: str, *, already: str | None = None) -> SendResult:
            phone.sent.append(rendered)
            raise OSError(28, "No space left on device")

        phone.send = send   # type: ignore[method-assign]
        message = self.job_notice()
        self.notifier.tick()
        for _ in range(3):
            self.time.advance(3600)
            self.notifier.tick()
        self.assertEqual(len(phone.sent), 1, "sent once, never again by itself")
        delivery = self.entry(message["id"])["deliveries"]["phone"]
        self.assertEqual(delivery["state"], "unknown")
        self.assertIsNone(delivery.get("next_try"))
        self.assertIn("OSError", delivery["error"])

    def test_a_retry_after_a_partial_send_never_reaches_an_account_twice(self) -> None:
        """DESIGN §40: a send that reached one account and was refused for the next is retried for the rest only."""
        phone = self.channel("phone")
        calls: list = []

        def send(rendered: Rendered, key: str, *, already: str | None = None) -> SendResult:
            calls.append(already)
            if len(calls) == 1:
                return SendResult("failed", remote_id="111:7", error="429 for the second account")
            return SendResult("sent", remote_id=f"{already},222:8")

        phone.send = send   # type: ignore[method-assign]
        message = self.job_notice()
        self.notifier.tick()
        self.assertEqual(calls[:1], [None])
        shown = next(d for d in self.notifier.message(message["id"])["deliveries"] if d["channel"] == "phone")
        self.assertEqual((shown["state"], shown["partial"]), ("failed", True), "the page says some accounts have it")
        self.time.advance(70)
        self.notifier.tick()
        self.assertEqual(calls, [None, "111:7"])
        shown = next(d for d in self.notifier.message(message["id"])["deliveries"] if d["channel"] == "phone")
        self.assertFalse(shown["partial"])
        delivery = self.entry(message["id"])["deliveries"]["phone"]
        self.assertEqual((delivery["state"], delivery["remote_id"]), ("sent", "111:7,222:8"))
        self.assertNotIn("partial", delivery)

    def test_failed_retries_with_backoff(self) -> None:
        phone = self.channel("phone")
        phone.results = [SendResult("failed", error="down")] * 3
        message = self.job_notice()
        self.notifier.tick()
        self.assertEqual(len(phone.sent), 1)
        self.time.advance(30)
        self.notifier.tick()
        self.assertEqual(len(phone.sent), 1, "the first retry waits a minute")
        self.time.advance(40)
        self.notifier.tick()
        self.assertEqual(len(phone.sent), 2)
        self.time.advance(90)
        self.notifier.tick()
        self.assertEqual(len(phone.sent), 2, "the second waits two minutes")
        self.time.advance(60)
        self.notifier.tick()
        self.assertEqual(len(phone.sent), 3)
        self.time.advance(300)
        self.notifier.tick()
        self.assertEqual(self.entry(message["id"])["deliveries"]["phone"]["state"], "sent")

    def test_an_hour_of_failures_raises_channel_down_on_the_other_channels(self) -> None:
        phone = self.channel("phone")
        phone.results = [SendResult("failed", error="down")] * 100
        for number in range(5):
            self.job_notice(number=f"10{number}")
        self.notifier.tick()
        self.assertGreaterEqual(len(phone.sent), 5)
        self.assertEqual(self.notifier.messages(kind="channel.down")["messages"], [],
                         "five failures within seconds are not an hour")
        for _ in range(55):
            self.time.advance(60)
            self.notifier.tick()
        self.assertEqual(self.notifier.messages(kind="channel.down")["messages"], [], "55 minutes")
        for _ in range(10):
            self.time.advance(60)
            self.notifier.tick()
        down = self.latest("channel.down")
        self.assertEqual(list(down["deliveries"]), ["telegram"])
        self.assertEqual(down["body"], "Your private channel has not worked for an hour. Messages still reach the "
                                       "cockpit and your other channel.")

    def test_an_hour_of_unknown_outcomes_also_raises_channel_down(self) -> None:
        """DESIGN §40: an answer of 500 or more is unknown and never resent, but for the channel-down alarm a try
        that nothing confirmed counts as not working (a proxy in front of a stopped ntfy answers 502)."""
        phone = self.channel("phone")
        phone.results = [SendResult("unknown", error="HTTP 502 after sending")] * 100
        for number in range(8):
            self.job_notice(number=f"10{number}")
            self.notifier.tick()
            self.time.advance(600)
            self.notifier.tick()
        self.assertEqual(len(phone.sent), 8, "each notice was sent once: unknown is never resent")
        down = self.latest("channel.down")
        self.assertEqual(list(down["deliveries"]), ["telegram"])
        self.assertIn("has not worked for an hour", down["body"])

    def test_a_second_telegram_poller_is_reported_as_what_it_is(self) -> None:
        self.notifier._channel_event("telegram", "conflict", {})
        down = self.latest("channel.down")
        self.assertIn("another program is reading this bot's messages", down["body"])
        self.assertNotIn("an hour", down["body"])

    def test_sending_at_a_restart_becomes_unknown(self) -> None:
        message = self.job_notice()
        entry = self.entry(message["id"])
        entry["deliveries"]["phone"]["state"] = "sending"
        self.notifier._save_entry(entry)
        notifier = self.restart()
        self.assertEqual(notifier._entries[message["id"]]["deliveries"]["phone"]["state"], "unknown")

    def test_message_ids_by_exclusive_create_are_never_reused(self) -> None:
        first = self.notifier.notify(Notice("notice", "operational", "default", "default", "a", "b"))
        (self.notifier.outbox / "M-000002.json").write_text("{}")   # a crash left a file without an index line
        second = self.notifier.notify(Notice("notice", "operational", "default", "default", "c", "d"))
        self.assertEqual((first["id"], second["id"]), ("M-000001", "M-000003"))
        self.assertEqual(self.restart().notify(Notice("notice", "operational", "default", "default", "e", "f"))["id"],
                         "M-000004")


class RulesTests(NotifyCase):
    def test_needs_human_once_per_entry_not_again_after_a_restart(self) -> None:
        job = {"repo": "my-app", "id": "003-x", "stage": "needs-human", "updated_at": "2026-10-21T10:00:00Z"}
        self.factory.jobs_list = [job]
        self.notifier.reconcile()                       # the first pass records, so turning messages on is silent
        self.assertEqual(self.notifier.messages()["messages"], [])
        job["stage"] = "build"
        self.notifier.rules_tick([_event("job.updated", {"job": dict(job)})])
        job["stage"] = "needs-human"
        self.notifier.rules_tick([_event("job.updated", {"job": dict(job)})])
        self.notifier.rules_tick([_event("job.updated", {"job": dict(job)})])
        self.assertEqual(len(self.notifier.messages(kind="job.needs_human")["messages"]), 1)
        self.restart().reconcile()
        self.assertEqual(len(self.notifier.messages(kind="job.needs_human")["messages"]), 1)

    def test_the_reconcile_catches_a_dropped_event(self) -> None:
        self.notifier.reconcile()
        self.factory.jobs_list = [{"repo": "my-app", "id": "004-y", "stage": "failed", "failed_step": "review"}]
        self.notifier.reconcile()
        failed = self.latest("job.failed")
        self.assertIn("failed at the review step", failed["body"])

    def test_rules_ignore_the_wake_systems_own_events(self) -> None:
        self.notifier.reconcile()
        for kind in ("notify.queued", "inbound.received", "wake.paused", "channel.health"):
            self.notifier.rules_tick([_event(kind, {"job": {"repo": "a", "id": "001", "stage": "needs-human"}})])
        self.assertEqual(self.notifier.messages()["messages"], [])

    def test_plan_attention_and_overdue_items(self) -> None:
        self.notifier.reconcile()
        self.notifier._plan_rule({"repo": "my-app", "id": "P004-big", "stage": "ready"}, None)
        plan = nt.notice_from_entry(self.entry(self.latest("plan.attention")["id"]))
        self.assertIn("A plan (P004) is waiting for you", gate(plan, ch.PRIVACY_TAILNET)[0].text)
        self.factory.items_answer = {"items": [{"ref": "system:S-12"}], "counts": {"waiting": 7}}
        self.notifier.rules_tick([_event("items.updated", {})])
        overdue = self.latest("items.overdue")
        self.assertEqual(overdue["deliveries"], {}, "in the inbox and the next summary only")
        self.assertEqual(self.latest("items.waiting")["deliveries"], {})

    def test_rules_off_keeps_the_notice_in_the_inbox(self) -> None:
        self.factory.update_config({"notify": {"rules_off": ["job.needs_human"]}})
        message = self.job_notice()
        self.assertEqual(message["deliveries"], {})


def _event(kind: str, data: dict) -> Any:
    from swfactory.events import Event
    return Event(1, "", kind, None, None, None, data)




class InboundCase(NotifyCase):
    def sent_notice(self) -> tuple[dict, dict[str, dict[str, str]]]:
        """A needs-human notice sent to both channels: (its inbox row, {channel: {verb: raw capability}})."""
        message = self.job_notice()
        self.notifier.tick()
        caps = {name: {b.verb: b.cap for b in self.channel(name).sent[-1].buttons} for name in ("phone", "telegram")}
        return message, caps

    def tap(self, channel: str, verb: str, cap: str, **extra: Any) -> dict:
        topic = "seraphina-taps" if channel == "phone" else None
        sender = "phone" if channel == "phone" else str(OWNER_ID)
        now = iso(self.notifier.clock.utc())
        extra.setdefault("sent_at", now if channel == "phone" else None)   # an ntfy tap has a time; Telegram's not
        return self.notifier.handle_inbound(Inbound(channel=channel, kind="button", topic=topic, sender=sender,
                                                    verb=verb, cap=cap, received_at=now,
                                                    remote_id=f"t{time.monotonic_ns()}", **extra))

    def say(self, channel: str, text: str, *, sent_at: str | None = None, **extra: Any) -> dict:
        topic = "seraphina-chat" if channel == "phone" else None
        sender = "phone" if channel == "phone" else str(OWNER_ID)
        stamp = sent_at or iso(self.notifier.clock.utc())
        return self.notifier.handle_inbound(Inbound(channel=channel, kind="text", topic=topic, sender=sender,
                                                    text=text, sent_at=stamp,
                                                    received_at=iso(self.notifier.clock.utc()),
                                                    remote_id=f"m{time.monotonic_ns()}", **extra))


class CapabilityTests(InboundCase):
    def test_a_capability_works_once_for_its_own_notice_and_verb(self) -> None:
        message, caps = self.sent_notice()
        cap = caps["phone"]["ack"]
        self.assertRegex(cap, r"^[A-Z2-7]{26}$")
        stored = json.loads((self.notifier.root / "caps.json").read_text())
        self.assertNotIn(cap, json.dumps(stored), "only the sha256 is kept")
        self.assertIn(ch.digest(cap), stored["caps"])
        self.assertEqual(self.tap("phone", "snooze_1h", cap)["outcome"], "forged", "bound to its verb")
        self.assertEqual(self.tap("telegram", "ack", cap)["outcome"], "refused", "bound to its channel")
        result = self.tap("phone", "ack", cap)
        self.assertEqual(result["outcome"], "applied")
        self.assertTrue(self.entry(message["id"])["read"])
        self.assertEqual(self.entry(message["id"])["read_via"], "phone")
        self.assertEqual(self.tap("phone", "ack", cap)["outcome"], "forged", "a replay does nothing")

    def test_an_expired_capability_answers_that_it_expired(self) -> None:
        _message, caps = self.sent_notice()
        self.time.advance(25 * 3600)
        result = self.tap("telegram", "ack", caps["telegram"]["ack"])
        self.assertEqual(result["outcome"], "expired")
        self.assertEqual(result["answer"], "That button had expired; nothing changed.")

    def test_a_telegram_tap_in_the_first_batch_after_boot_is_late(self) -> None:
        message, caps = self.sent_notice()
        result = self.tap("telegram", "ack", caps["telegram"]["ack"], first_batch=True)
        self.assertEqual(result["outcome"], "late")
        self.assertFalse(self.entry(message["id"])["read"])
        applied = self.notifier.apply_inbound(result["inbound"])
        self.assertEqual(applied["applied"], "ack")
        self.assertTrue(self.entry(message["id"])["read"])
        with self.assertRaises(nt.NotifyError):
            self.notifier.apply_inbound(result["inbound"])

    def test_a_tap_on_a_stale_telegram_notice_is_late_but_stop_always_applies(self) -> None:
        _message, caps = self.sent_notice()
        self.time.advance(31 * 60)
        self.assertEqual(self.tap("telegram", "snooze_1h", caps["telegram"]["snooze_1h"])["outcome"], "late")
        persona = self.notifier.notify(Notice("persona", "model", "needs_you", "default", "Hi", "Fine.",
                                              sender="seraphina", persona="seraphina"))
        self.notifier.tick()
        stop_cap = {b.verb: b.cap for b in self.channel("telegram").sent[-1].buttons}["stop"]
        self.time.advance(40 * 60)
        self.assertEqual(self.tap("telegram", "stop", stop_cap, first_batch=True)["outcome"], "applied")
        self.assertIsNotNone(self.notifier.paused())
        self.assertTrue(persona["id"])


class VerbTests(InboundCase):
    def test_got_it_snooze_and_the_snoozed_update(self) -> None:
        message, caps = self.sent_notice()
        self.time.set("2026-10-21 13:07")
        result = self.tap("phone", "snooze_1h", caps["phone"]["snooze_1h"])
        self.assertEqual(result["answer"], "Snoozed until 14:07")
        self.assertEqual(self.channel("phone").updates[-1][1].text, "Snoozed until 14:07")
        sent_before = len(self.channel("phone").sent)
        self.time.set("2026-10-21 14:08")
        self.notifier.tick()
        again = self.channel("phone").sent[sent_before:]
        self.assertEqual(len([r for r in again if "waiting for your decision" in r.text]), 1)
        self.assertEqual(again[0].why, "Snoozed at 13:07")
        self.assertTrue(message["id"])

    def test_a_snooze_into_do_not_disturb_time_moves_to_its_end(self) -> None:
        self.time.set("2026-10-21 20:50")
        _message, caps = self.sent_notice()
        self.time.set("2026-10-21 21:00")
        result = self.tap("phone", "snooze_1h", caps["phone"]["snooze_1h"])
        self.assertEqual(result["answer"], "Snoozed until 08:00 (do-not-disturb time)")

    def test_stop_creates_paused_and_confirms_at_once_on_every_channel_even_at_22_00(self) -> None:
        self.time.set("2026-10-21 22:00")
        result = self.say("phone", "stop")
        self.assertEqual(result["outcome"], "applied")
        self.assertTrue((self.notifier.wakes_root / "PAUSED").exists())
        self.assertEqual(self.notifier.paused()["by"], "phone")
        self.notifier.tick()
        phone, telegram = self.channel("phone").sent[-1], self.channel("telegram").sent[-1]
        self.assertIn("Seraphina is stopped (from your phone at 22:00)", phone.text)
        self.assertEqual((phone.priority, telegram.priority), ("low", "low"))
        self.assertNotIn("Seraphina", telegram.title + telegram.text, "persona-neutral on Telegram")
        self.assertEqual(self.say("telegram", "restart")["answer"], "Do this in the cockpit.")
        self.assertIsNotNone(self.notifier.paused())
        with self.assertRaises(nt.NotifyError):
            self.notifier.resume(by="phone")
        self.notifier.resume(by="human")
        self.assertIsNone(self.notifier.paused())

    def test_status_help_mute_and_refusals(self) -> None:
        self.notifier.reconcile()
        self.factory.jobs_list = [{"repo": "a", "id": "001-x", "stage": "needs-human"},
                                  {"repo": "a", "id": "002-y", "stage": "build"}]
        status = self.say("telegram", "status")["answer"]
        self.assertIn("1 job waits for you.", status)
        self.assertIn("1 job is running.", status)
        self.assertNotIn("001", status, "counts only")
        self.assertIn("A real message never asks you to approve anything", self.say("phone", "help")["answer"])
        self.assertIn("nothing to mute", self.say("phone", "mute")["answer"])
        self.assertEqual(self.say("phone", "approve 003")["answer"], "Approve in the cockpit.")
        self.assertIn("Did you mean the command 'stop'", self.say("phone", "stopp")["answer"])
        self.assertIsNone(self.notifier.paused())

    def test_stop_worrying_is_free_text_and_stops_nothing(self) -> None:
        result = self.say("telegram", "Stop worrying about the backup, it's fine")
        self.assertEqual(result["outcome"], "kept")
        self.assertIsNone(self.notifier.paused())
        self.assertIn("Telegram can read what you type here", result["answer"])
        second = self.say("telegram", "thanks")
        self.assertIsNone(second["answer"], "the warning and the kept note come once")
        lines = self.notifier.inbound(kind="text")
        self.assertEqual(lines[0]["text"], "thanks")

    def test_late_commands_are_not_applied_but_stop_is(self) -> None:
        self.time.set("2026-10-22 07:00")
        sent = iso(local("2026-10-21 23:41").astimezone(timezone.utc))
        result = self.say("telegram", "snooze 1h", sent_at=sent)
        self.assertEqual(result["outcome"], "late")
        self.assertIn("You sent 'snooze 1h' at 23:41; it arrived at 07:00 and was not applied", result["answer"])
        self.assertEqual(self.say("telegram", "/stop", sent_at=sent)["outcome"], "applied")
        self.assertIsNotNone(self.notifier.paused())

    def test_quiet_morning_ends_with_do_not_disturb_time(self) -> None:
        self.time.set("2026-10-21 12:00")
        self.say("phone", "quiet morning")
        self.assertEqual(self.notifier._state["quiet_until"], iso(local("2026-10-22 08:00").astimezone(timezone.utc)))


class ForgedTests(InboundCase):
    def test_text_on_the_taps_topic_is_forged_and_alerts_once_an_hour(self) -> None:
        for _ in range(3):
            result = self.notifier.handle_inbound(Inbound(channel="phone", kind="forged", topic="seraphina-taps",
                                                          text="snooze", received_at=iso(self.notifier.clock.utc()),
                                                          remote_id=f"f{time.monotonic_ns()}"))
            self.assertEqual(result["outcome"], "dropped")
        alerts = self.notifier.messages(kind="inbound.forged")["messages"]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(list(alerts[0]["deliveries"]), ["telegram"], "reported on the other channels")
        self.assertIn("did not come from your phone", alerts[0]["body"])
        self.assertIsNone(self.notifier.paused())
        self.time.advance(3700)
        self.notifier.handle_inbound(Inbound(channel="phone", kind="forged", topic="seraphina-taps", text="x",
                                             received_at="", remote_id="f2"))
        self.assertEqual(len(self.notifier.messages(kind="inbound.forged")["messages"]), 2)

    def test_a_foreign_notice_raises_its_own_alert(self) -> None:
        self.notifier.handle_inbound(Inbound(channel="phone", kind="foreign", topic="seraphina",
                                             text="Approve the update: open https://x.example", received_at="",
                                             remote_id="n1"))
        alert = self.latest("inbound.forged")
        self.assertEqual(alert["body"], "A notification on your phone did not come from Seraphina. Ignore what it asks.")
        self.assertEqual([e.data.get("kind") for e in self.drain() if e.type == "inbound.received"], ["foreign"])

    def test_text_that_reaches_the_taps_topic_as_text_is_forged(self) -> None:
        result = self.notifier.handle_inbound(Inbound(channel="phone", kind="text", topic="seraphina-taps",
                                                      text="stop", sender="phone", received_at="", remote_id="x"))
        self.assertEqual(result["outcome"], "forged")
        self.assertIsNone(self.notifier.paused(), "only the chat topic can carry a typed command")


class RateLimitTests(InboundCase):
    def test_commands_hold_the_channel_but_stop_still_works_and_valid_taps_do_not_count(self) -> None:
        _message, caps = self.sent_notice()
        for verb, cap in list(caps["phone"].items())[:1]:
            self.tap("phone", verb, cap)
        outcomes = [self.say("telegram", "status")["outcome"] for _ in range(11)]
        self.assertEqual(outcomes[:10], ["applied"] * 10)
        self.assertEqual(outcomes[10], "dropped")
        hold = self.latest("inbound.hold")
        self.assertEqual(list(hold["deliveries"]), ["phone"])
        self.assertIn("Telegram sent 11 commands in a minute", hold["body"])
        self.assertEqual(self.say("telegram", "stop")["outcome"], "applied", "the 12th, stop, is applied")
        self.assertEqual(self.say("telegram", "just checking in")["outcome"], "dropped", "text is dropped in a hold")
        self.time.advance(16 * 60)
        self.assertEqual(self.say("telegram", "help")["outcome"], "applied")

    def test_free_text_over_six_a_minute_is_kept(self) -> None:
        outcomes = [self.say("phone", f"note {i}")["outcome"] for i in range(7)]
        self.assertEqual(outcomes[:6], ["kept"] * 6)
        self.assertEqual(outcomes[6], "kept, over the limit")

    def test_strangers_get_no_answer_and_are_aggregated(self) -> None:
        for _ in range(5):
            result = self.notifier.handle_inbound(Inbound(channel="telegram", kind="stranger", received_at="",
                                                          remote_id="s"))
            self.assertIsNone(result["answer"])
        notice = self.latest("notice")
        self.assertIn("Someone who is not you messaged the bot", notice["body"])
        self.assertEqual(notice["deliveries"], {})
        received = [e for e in self.drain() if e.type == "inbound.received"]
        self.assertEqual(len(received), 1, "logged at most once a minute")



class HookTests(NotifyCase):
    def add_hook(self, **body: Any) -> dict:
        return self.notifier.hook_add({"name": "backup-script", "events": ["backup.failed", "backup.ok"],
                                       "subjects": ["nightly"], **body})

    def test_add_keeps_only_the_digest_and_snippets_never_hold_the_token(self) -> None:
        added = self.add_hook()
        token = added["token"]
        self.assertGreaterEqual(len(token), 43)
        saved = Path(self.factory.config.path).read_text()
        self.assertNotIn(token, saved)
        self.assertIn(nt.hook_digest(token), saved)
        self.assertNotIn(token, json.dumps(added["snippets"]))
        self.assertIn("-H @/etc/seraphina/backup-script.header", json.dumps(added["snippets"]))
        self.assertEqual(self.notifier.hook_for_token(token), "backup-script")
        self.assertIsNone(self.notifier.hook_for_token(MASTER))
        self.assertIsNone(self.notifier.hook_for_token("x" * 43))
        rotated = self.notifier.hook_action("backup-script", "rotate")
        self.assertIsNone(self.notifier.hook_for_token(token), "the old token stops working")
        self.assertEqual(self.notifier.hook_for_token(rotated["token"]), "backup-script")
        self.notifier.hook_action("backup-script", "delete")
        self.assertIsNone(self.notifier.hook_for_token(rotated["token"]))
        self.assertTrue(all(token not in line for line in _all_text(self.tmp / "data")))

    def test_kinds_and_the_subject_allowlist(self) -> None:
        self.add_hook()
        result = self.notifier.hook_event("backup-script", {"kind": "backup.failed", "subject": "nightly",
                                                            "detail": "rsync exit 23 on /home"})
        entry = self.entry(result["message"])
        self.assertEqual(entry["priority"], "high")
        self.assertIn("rsync exit 23", entry["body"], "the detail is shown in the cockpit only")
        self.notifier.tick()
        for name in ("phone", "telegram"):
            self.assertNotIn("rsync", json.dumps([r.text for r in self.channel(name).sent]))
        other = self.notifier.hook_event("backup-script", {"kind": "backup.failed", "subject": "secret-project"})
        self.assertEqual(self.entry(other["message"])["body"], "A backup failed. See the cockpit for what to do.")
        with self.assertRaises(nt.HookRefused) as refused:
            self.notifier.hook_event("backup-script", {"kind": "service.down"})
        self.assertEqual(refused.exception.status, 403)
        with self.assertRaises(nt.NotifyError):
            self.notifier.hook_event("backup-script", {"kind": "rm -rf"})
        with self.assertRaises(nt.NotifyError):
            self.notifier.hook_event("backup-script", {"kind": "backup.ok", "extra": 1})
        with self.assertRaises(nt.NotifyError):
            self.notifier.hook_event("backup-script", {"kind": "backup.ok", "detail": "x" * 201})

    def test_idempotency_key_returns_the_first_answer(self) -> None:
        self.add_hook()
        first = self.notifier.hook_event("backup-script", {"kind": "backup.failed"}, "run-42")
        second = self.notifier.hook_event("backup-script", {"kind": "backup.failed"}, "run-42")
        self.assertEqual(first, second)
        self.time.advance(25 * 3600)
        third = self.notifier.hook_event("backup-script", {"kind": "backup.failed"}, "run-42")
        self.assertNotEqual(third, first)

    def test_wrong_tokens_are_counted_into_one_inbox_notice_a_day(self) -> None:
        for _ in range(3):
            self.notifier.hook_auth_failed()
        self.add_hook()
        self.assertEqual(self.notifier.hooks()[0]["wrong_tokens_today"], 3, "every failure is counted")
        notices = self.notifier.messages(kind="notice")["messages"]
        self.assertEqual(len(notices), 1)
        self.assertIn("1 wrong event-source token today", notices[0]["body"], "the notice moves once a minute")
        self.time.advance(61)
        self.notifier.hook_auth_failed()
        notices = self.notifier.messages(kind="notice")["messages"]
        self.assertEqual(len(notices), 1)
        self.assertIn("4 wrong event-source tokens today", notices[0]["body"])
        self.assertEqual(notices[0]["deliveries"], {})


class FingerprintTests(NotifyCase):
    def test_a_hand_edit_of_notify_stops_seraphina_and_sends_only_the_alarm(self) -> None:
        self.notifier.start(threads=False)
        self.addCleanup(self.notifier.stop)
        data = json.loads(Path(self.config.path).read_text())
        data["notify"]["routes"] = {"needs_you": ["telegram"]}
        Path(self.config.path).write_text(json.dumps(data))
        message = self.job_notice()
        self.notifier.tick()
        self.assertIsNotNone(self.notifier.paused())
        self.assertEqual(self.notifier.paused()["by"], "tamper-check")
        alarm = self.latest("wakes.tamper")
        self.assertIn("notify", alarm["body"])
        self.notifier.tick()
        self.assertEqual(self.entry(alarm["id"])["deliveries"]["phone"]["state"], "sent")
        self.assertEqual(self.entry(message["id"])["deliveries"]["phone"]["state"], "queued", "held while tampered")
        self.notifier.review("accept")
        self.time.advance(61)                            # held sends look again every minute
        self.notifier.tick()
        self.assertEqual(self.entry(message["id"])["deliveries"]["phone"]["state"], "sent")
        self.assertIsNotNone(self.notifier.paused(), "accept keeps her stopped; restart is its own choice")
        self.notifier.review("restart")
        self.assertIsNone(self.notifier.paused())

    def test_an_api_save_is_the_new_baseline_and_a_hand_edit_of_the_terms_is_not(self) -> None:
        self.notifier.start(threads=False)
        self.addCleanup(self.notifier.stop)
        self.factory.update_config({"notify": {"phone_max_per_day": 20}})
        self.notifier.tick()
        self.assertIsNone(self.notifier.paused())
        self.terms_file.write_text("diabetes\nnew word\n")
        self.notifier.tick()
        self.assertIsNotNone(self.notifier.paused())
        self.assertEqual(self.notifier._state["tamper"]["what"], ["terms"])

    def test_a_hand_edit_while_stopped_is_caught_at_start(self) -> None:
        self.notifier.start(threads=False)
        self.notifier.stop()
        data = json.loads(Path(self.config.path).read_text())
        data["notify"]["phone_max_per_day"] = 100
        Path(self.config.path).write_text(json.dumps(data))
        notifier = self.restart()
        notifier.start(threads=False)
        self.addCleanup(notifier.stop)
        self.assertIsNotNone(notifier._state.get("tamper"))

    def test_the_settings_audit_names_keys_never_values(self) -> None:
        self.notifier.start(threads=False)
        self.addCleanup(self.notifier.stop)
        self.factory.update_config({"notify": {"channels": {"phone": {"operational_names": True}}}})
        lines = [json.loads(line) for line in self.notifier.audit.path().read_text().splitlines()]
        settings_line = [line for line in lines if line.get("why") == "settings"][-1]
        self.assertEqual(settings_line["keys"], ["notify.channels.phone.operational_names"])


class RulebookFingerprintTests(NotifyCase):
    """S47 for steps W2/W3: the reminders file, the rulebook and the clean confirmations are fingerprinted; the
    harness's own writes are the new baseline, a hand edit stops wakes."""

    def setUp(self) -> None:
        super().setUp()
        self.notifier.start(threads=False)
        self.addCleanup(self.notifier.stop)

    def test_a_hand_edit_of_the_reminders_file_stops_wakes(self) -> None:
        reminders = self.notifier.wakes_root / "reminders.json"
        reminders.parent.mkdir(parents=True, exist_ok=True)
        reminders.write_text('{"reminders": []}')
        self.notifier.settings_written("reminders")
        self.notifier.tick()
        self.assertIsNone(self.notifier.paused(), "the harness's own write is the baseline")
        reminders.write_text('{"reminders": [{"id": "R-000001", "text": "planted"}]}')
        self.notifier.tick()
        self.assertEqual(self.notifier._state["tamper"]["what"], ["reminders"])

    def test_a_hand_edit_of_the_rulebook_stops_wakes_and_a_cockpit_change_does_not(self) -> None:
        from swfactory import rules

        rules.update_rules(self.config, {"quiet_hours": "22:00-07:00"})
        self.notifier.settings_written("rules")
        self.notifier.tick()
        self.assertIsNone(self.notifier.paused())
        path = rules.rules_path(self.config)
        doc = json.loads(path.read_text())
        doc["confirmed_profiles"] = {"nova": {"sha256": "a" * 64, "what": "soul", "at": ""}}
        path.write_text(json.dumps(doc))
        self.notifier.tick()
        self.assertIsNotNone(self.notifier.paused())
        self.assertEqual(sorted(self.notifier._state["tamper"]["what"]), ["clean", "rules"])


class UncleanStopTests(NotifyCase):
    def crash_at(self, text: str) -> None:
        self.notifier.start(threads=False)
        self.notifier.stop()
        state = json.loads((self.notifier.root / "state.json").read_text())
        state.update(clean_stop=False, stopping=False, liveness=iso(local(text).astimezone(timezone.utc)))
        (self.notifier.root / "state.json").write_text(json.dumps(state))
        self.time.set("2026-10-22 07:05")
        notifier = self.restart()
        notifier.start(threads=False)
        self.addCleanup(notifier.stop)

    def test_a_hard_stop_inside_the_power_off_window_is_only_logged(self) -> None:
        self.crash_at("2026-10-21 22:59")
        self.assertEqual(self.notifier.messages(kind="server.unclean")["messages"], [])
        self.assertTrue(self.notifier.audit.last("hard_stop")["inside_power_off"])

    def test_a_hard_stop_at_15_42_is_reported(self) -> None:
        self.crash_at("2026-10-21 15:42")
        unclean = self.latest("server.unclean")
        self.assertEqual(unclean["body"], "Your server stopped unexpectedly at 15:42 (not the 23:00 power-off). "
                                          "Nothing to do unless it happens again.")

    def test_a_clean_stop_reports_nothing(self) -> None:
        self.notifier.start(threads=False)
        self.notifier.mark_stopping()
        self.notifier.stop()
        notifier = self.restart()
        notifier.start(threads=False)
        self.addCleanup(notifier.stop)
        self.assertEqual(notifier.messages(kind="server.unclean")["messages"], [])


class PreconditionTests(NotifyCase):
    def test_without_bwrap_phone_channels_are_off_until_acknowledged(self) -> None:
        self.factory.update_config({"harness": {"sandbox": "none"}})
        message = self.job_notice()
        self.assertEqual(self.entry(message["id"])["deliveries"]["phone"]["state"], "off")
        self.assertIn("without the sandbox", self.entry(message["id"])["deliveries"]["phone"]["why"])
        with self.assertRaises(nt.NotifyError):
            self.notifier.pair("telegram")
        with self.assertRaises(nt.NotifyError):
            self.notifier.set_terms("diabetes")
        with self.assertRaises(nt.NotifyError):
            self.notifier.acknowledge("I understand")
        self.notifier.acknowledge(nt.ACK_SENTENCE + ".")
        second = self.job_notice(number="004")
        self.assertEqual(self.entry(second["id"])["deliveries"]["phone"]["state"], "queued")
        self.assertEqual(self.notifier.audit.last("acknowledgement")["withdrawn"], False)
        self.notifier.acknowledge(withdraw=True)
        self.assertFalse(self.notifier.precondition()["ok"])

    def test_a_cli_driver_route_counts_as_unsandboxed(self) -> None:
        self.factory.update_config({"agents": {"builder": {"driver": "claude"}}})
        self.assertTrue(self.notifier.precondition()["cli_driver"])
        self.assertFalse(self.notifier.precondition()["ok"])

    def test_check_patch_refuses_turning_a_channel_on_without_the_sandbox(self) -> None:
        self.factory.update_config({"harness": {"sandbox": "none"},
                                    "notify": {"channels": {"telegram": {"enabled": False}}}})
        with self.assertRaises(nt.NotifyError):
            self.notifier.check_patch({"notify": {"channels": {"telegram": {"enabled": True}}}})
        self.notifier.check_patch({"harness": {"sandbox": "bwrap"},
                                   "notify": {"channels": {"telegram": {"enabled": True}}}})
        self.notifier.check_patch({"notify": {"channels": {"phone": {"operational": False}}}})


class ZoneFallbackTests(unittest.TestCase):
    def test_a_bad_hand_edited_zone_does_not_stop_the_notifier(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="sf-zone-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        config = Config(tmp / "config.json", {"data_dir": str(tmp / "data"), "wakes": {"timezone": "Mars/Base"}})
        notifier = Notifier(config, None, None, environ={}, tailnet=lambda: None, server_yml=lambda: None,
                            time_sync=TimeSync(require=False))
        self.assertIn("Mars/Base", notifier.state()["timezone_error"])


FAKE_HASH = "$2a$10$" + "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZa"   # bcrypt shape, 53 chars after the cost


class NtfySetupTests(NotifyCase):
    def make_notifier(self, **kwargs: Any) -> Notifier:
        self.hashed: list[str] = []

        def run(argv: list[str], input_text: str | None = None, timeout: float = 10.0) -> str:
            if argv[:3] == ["ntfy", "user", "hash"]:
                self.hashed.append(input_text or "")
                return FAKE_HASH + "\n"
            return ""
        kwargs.setdefault("run", run)
        return super().make_notifier(**kwargs)

    def test_prepare_generates_tokens_stages_files_and_prints_no_secret(self) -> None:
        result = self.notifier.prepare_ntfy("phone")
        harness, reply = self.environ["SERAPHINA_NTFY_TOKEN"], self.environ["SERAPHINA_NTFY_REPLY_TOKEN"]
        self.assertRegex(harness, r"^tk_[a-z0-9]{29}$")
        self.assertRegex(reply, r"^tk_[a-z0-9]{29}$")
        self.assertNotEqual(harness, HARNESS_TOKEN, "a new harness token")
        self.assertEqual(len(self.hashed), 3)
        self.assertIn(result["passphrase"] + "\n" + result["passphrase"] + "\n", self.hashed,
                      "the phone's passphrase is hashed by piping it twice")
        blocks = json.dumps(result["copy_blocks"])
        for secret in (harness, reply, result["passphrase"]):
            self.assertNotIn(secret, blocks, "no secret in a command line")
        setup = Path(result["setup_dir"])
        yml = (setup / "server.yml").read_text()
        self.assertIn('auth-default-access: "deny-all"', yml)
        self.assertIn('"phone-reply:seraphina-taps:wo"', yml)
        self.assertIn('"owner-phone:seraphina-chat:rw"', yml)
        self.assertEqual(ch.ntfy_forwarders(yml), [], "nothing that forwards text off the machine")
        secrets_env = (setup / "ntfy-secrets.env").read_text()
        self.assertIn(harness, secrets_env)
        self.assertNotIn(result["passphrase"], secrets_env, "only the hash of the passphrase")
        for path in setup.iterdir():
            self.assertEqual(path.stat().st_mode & 0o077, 0, path)
        self.assertEqual([b["id"] for b in result["copy_blocks"]], ["2a", "2b", "2c"])

    def test_prepare_without_ntfy_installed_says_what_to_do(self) -> None:
        self.notifier._run = lambda argv, input_text=None, timeout=10.0: ""
        with self.assertRaises(nt.NotifyError) as caught:
            self.notifier.prepare_ntfy("phone")
        self.assertIn("run copy block 2a", str(caught.exception))
        self.assertEqual(self.environ["SERAPHINA_NTFY_TOKEN"], HARNESS_TOKEN, "nothing changed")

    def test_rotate_keeps_the_harness_token_leaked_replaces_it(self) -> None:
        self.notifier.channel_action("phone", "rotate")
        rotated = self.environ["SERAPHINA_NTFY_TOKEN"]
        self.assertEqual(rotated, HARNESS_TOKEN)
        self.assertNotEqual(self.environ["SERAPHINA_NTFY_REPLY_TOKEN"], REPLY_TOKEN)
        self.notifier.channel_action("phone", "leaked")
        self.assertNotEqual(self.environ["SERAPHINA_NTFY_TOKEN"], HARNESS_TOKEN)
        with self.assertRaises(nt.NotifyError):
            self.notifier.channel_action("telegram", "leaked")
        self.notifier.channel_action("telegram", "leaked", token="8123:AAH-new-token-0123456789")
        self.assertEqual(self.environ["SERAPHINA_TELEGRAM_BOT_TOKEN"], "8123:AAH-new-token-0123456789")

    def test_lost_my_phone(self) -> None:
        result = self.notifier.lost_phone()
        self.assertIn("stopped Seraphina", result["done"])
        self.assertIsNotNone(self.notifier.paused())
        self.assertFalse(self.factory.config.raw["notify"]["channels"]["phone"]["enabled"])
        self.assertTrue(result["passphrase"])
        self.assertTrue(any("unpaired" in line for line in result["done"]))


class RetentionTests(NotifyCase):
    def test_text_older_than_ninety_days_becomes_its_sha256(self) -> None:
        message = self.notifier.notify(Notice("notice", "operational", "needs_you", "default", "Old", "Old body."))
        self.notifier.tick()
        self.notifier.handle_inbound(Inbound(channel="phone", kind="text", topic="seraphina-chat", sender="phone",
                                             text="my words", sent_at=iso(self.notifier.clock.utc()), received_at="",
                                             remote_id="r1"))
        self.time.advance(95 * 86400)
        self.notifier.tick()
        entry = self.entry(message["id"])
        self.assertEqual(entry["body"], "sha256:" + ch.digest("Old body."))
        self.assertTrue(entry["text_removed"])
        self.assertTrue(all("Old body." not in line for line in _all_text(self.notifier.outbox)))
        self.assertTrue(all("my words" not in line for path in self.notifier.root.glob("inbound-*.jsonl")
                            for line in path.read_text().splitlines()))


class DataBoundaryTests(NotifyCase):
    """The owner's words, sensitive terms and tokens never reach an event, and tokens never a file."""

    def test_events_carry_ids_never_text(self) -> None:
        self.notifier.notify(Notice("reminder", "owner_text", "persona", "default", "Reminder",
                                    "Collect the diabetes prescription.", fields={"why": "reminder"}))
        self.notifier.handle_inbound(Inbound(channel="telegram", kind="text", sender=str(OWNER_ID),
                                             text="my blood sugar was 9.1 today", sent_at=iso(self.notifier.clock.utc()),
                                             received_at="", remote_id="1"))
        self.notifier.tick()
        dumped = json.dumps([e.to_dict() for e in self.drain()])
        self.assertNotIn("diabetes", dumped)
        self.assertNotIn("blood sugar", dumped)
        for name in ("phone", "telegram"):
            self.assertNotIn("diabetes", json.dumps([r.text + r.title for r in self.channel(name).sent]))

    def test_no_token_in_any_file_or_event(self) -> None:
        self.job_notice()
        self.notifier.tick()
        self.notifier.handle_inbound(Inbound(channel="phone", kind="forged", topic="seraphina-taps",
                                             text=f"steal {HARNESS_TOKEN}", received_at="", remote_id="x"))
        files = _all_text(self.tmp / "data")
        for token in (TOKEN, HARNESS_TOKEN, REPLY_TOKEN, MASTER):
            self.assertTrue(all(token not in line for line in files), token)
        dumped = json.dumps([e.to_dict() for e in self.drain()])
        for token in (TOKEN, HARNESS_TOKEN, REPLY_TOKEN):
            self.assertNotIn(token, dumped)

    def test_data_files_are_private(self) -> None:
        self.job_notice()
        self.notifier.tick()
        for path in (self.tmp / "data").rglob("*"):
            mode = path.stat().st_mode & 0o777
            self.assertEqual(mode & 0o077, 0, f"{path} is {mode:o}")


def _all_text(root: Path) -> list[str]:
    lines: list[str] = []
    for path in Path(root).rglob("*"):
        if path.is_file():
            lines += path.read_text(encoding="utf-8", errors="replace").splitlines()
    return lines



def wait_until(predicate: Any, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


class EndToEndTests(NotifyCase):
    """W1a-W1c through the real channels: the mock ntfy server on a Unix socket and the mock Telegram Bot API."""

    real_channels = True
    paired = True

    def setUp(self) -> None:
        self.ntfy = MockNtfy(unix=True).start()
        self.addCleanup(self.ntfy.stop)
        self.tg = MockTelegram().start()
        self.addCleanup(self.tg.stop)
        self.channels = {"phone": ntfy_entry(self.ntfy.publish_url), "telegram": telegram_entry()}
        super().setUp()

    def make_config(self, **sections: Any) -> Config:
        hour = datetime.now(ZONE).hour     # real time: keep do-not-disturb time far from now
        quiet = f"{(hour + 6) % 24:02d}:00-{(hour + 7) % 24:02d}:00"
        notify = dict(sections.pop("notify", {}))
        notify.setdefault("quiet_hours", quiet)
        config = super().make_config(**sections)
        config.raw["notify"]["quiet_hours"] = notify["quiet_hours"]
        data = json.loads(Path(config.path).read_text())
        data["notify"]["quiet_hours"] = notify["quiet_hours"]
        Path(config.path).write_text(json.dumps(data))
        if self.paired:
            state = self.tmp / "data" / "notify" / "channels" / "telegram.json"
            state.parent.mkdir(parents=True, exist_ok=True)
            state.write_text(json.dumps({"paired": [{"id": OWNER_ID, "username": "owner_name"}]}))
        return Config(config.path, data)

    def make_notifier(self, **kwargs: Any) -> Notifier:
        kwargs.setdefault("clock", LocalClock(TZ))
        kwargs.setdefault("monotonic", time.monotonic)
        kwargs.setdefault("channel_kwargs", {"api_base": self.tg.api_base, "backoff": (0.05, 0.2),
                                             "stream_timeout": 5.0, "http_timeout": 5.0})
        return super().make_notifier(**kwargs)

    def running(self) -> Notifier:
        notifier = self.notifier
        notifier.start(threads=False)
        notifier.supervise()
        self.addCleanup(notifier.stop)
        self.assertTrue(wait_until(lambda: self.ntfy.subscriptions and self.tg.offsets), "receivers started")
        return notifier

    def test_needs_human_on_both_channels_then_got_it_from_the_phone(self) -> None:
        notifier = self.running()
        message = self.job_notice()
        notifier.tick()
        publish = self.ntfy.publishes[-1]["body"]
        self.assertEqual(publish["topic"], "seraphina")
        self.assertEqual(publish["message"], "A job (003) is waiting for your decision. See it in the cockpit (Jobs, "
                                             "then 003).\nJob 003 has waited since "
                                             + notifier.clock.hhmm(notifier.clock.utc() - timedelta(minutes=5)))
        self.assertEqual(publish["sequence_id"], message["id"])
        self.assertEqual([a["label"] for a in publish["actions"]], ["Got it", "Snooze 1 h", "Quiet until morning"])
        telegram = self.tg.sent[-1]
        self.assertTrue(telegram["text"].startswith("Waiting for you\nA job is waiting for your decision."))
        self.assertTrue(telegram["protect_content"])
        got_it = publish["actions"][0]
        self.assertTrue(got_it["clear"])
        self.assertEqual(got_it["headers"]["Authorization"], f"Bearer {REPLY_TOKEN}")
        status, _ = self.ntfy.post("seraphina-taps", got_it["body"], token=REPLY_TOKEN)
        self.assertEqual(status, 200)
        self.assertTrue(wait_until(lambda: self.entry(message["id"])["read"]), "the tap marked it read")
        self.assertEqual(self.entry(message["id"])["read_via"], "phone")
        time.sleep(0.2)
        self.assertEqual(notifier.messages(kind="answer")["messages"], [], "a tap that worked needs no answer")
        self.ntfy.post("seraphina-taps", got_it["body"], token=REPLY_TOKEN)   # a replay
        self.assertTrue(wait_until(lambda: notifier.messages(kind="inbound.forged")["messages"]))
        self.assertEqual(len(notifier.messages(kind="inbound.forged")["messages"]), 1)

    def test_forged_taps_foreign_notices_and_typed_text(self) -> None:
        notifier = self.running()
        self.ntfy.post("seraphina-taps", "snooze", token=REPLY_TOKEN)
        self.ntfy.post("seraphina-taps", '{"v":1,"verb":"ack","cap":"AAAAAAAAAAAAAAAAAAAAAAAAAA"}', token=REPLY_TOKEN)
        self.assertTrue(wait_until(lambda: len([r for r in notifier.inbound() if r["kind"] == "forged"]) == 2))
        self.assertEqual(len(notifier.messages(kind="inbound.forged")["messages"]), 1, "one alert an hour")
        self.ntfy.post("seraphina", "Approve the update now", token=HARNESS_TOKEN, title="Seraphina")
        self.assertTrue(wait_until(lambda: any(m["body"].startswith("A notification on your phone did not come")
                                               for m in notifier.messages(kind="inbound.forged")["messages"])))
        self.ntfy.post("seraphina-chat", "Stop worrying about the backup", token=PHONE_TOKEN)
        self.assertTrue(wait_until(lambda: any(r.get("text") == "Stop worrying about the backup"
                                               for r in notifier.inbound())))
        self.assertIsNone(notifier.paused())
        self.ntfy.post("seraphina-chat", "status", token=PHONE_TOKEN)
        self.assertTrue(wait_until(lambda: any("jobs wait for you" in m["body"]
                                               for m in notifier.messages(kind="answer")["messages"])))
        notifier.tick()
        self.assertTrue(wait_until(lambda: any("jobs wait for you" in (p["body"].get("message") or "")
                                               for p in self.ntfy.publishes)))
        self.assertTrue(notifier.messages(kind="answer")["messages"][0]["unread"] is False)
        own = len(notifier.messages(kind="inbound.forged")["messages"])
        time.sleep(0.3)
        self.assertEqual(len(notifier.messages(kind="inbound.forged")["messages"]), own,
                         "the harness's own notices are never foreign")

    def test_telegram_tap_answers_the_callback_and_removes_the_buttons(self) -> None:
        notifier = self.running()
        self.assertTrue(wait_until(lambda: len(self.tg.offsets) >= 2), "past the first batch after boot")
        message = self.job_notice()
        notifier.tick()
        sent = self.tg.sent[-1]
        data = sent["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        self.assertRegex(data, r"^v1:ack:[A-Z2-7]{26}$")
        self.assertLessEqual(len(data.encode()), 64)
        self.tg.add_callback(OWNER_ID, data, sent["message_id"])
        self.assertTrue(wait_until(lambda: self.entry(message["id"])["read"]))
        self.assertTrue(wait_until(lambda: self.tg.called("editMessageReplyMarkup")))
        self.assertEqual(self.tg.called("answerCallbackQuery")[-1]["text"], "Marked as read.")

    def test_telegram_strangers_and_typed_text(self) -> None:
        notifier = self.running()
        self.tg.add_message(STRANGER_ID, "status")
        self.tg.add_message(OWNER_ID, "Stop worrying about the backup, it's fine")
        self.assertTrue(wait_until(lambda: any(r.get("text", "").startswith("Stop worrying")
                                               for r in notifier.inbound())))
        self.assertTrue(wait_until(lambda: self.tg.called("deleteMessage")), "typed text leaves the chat")
        answers = [m for m in self.tg.sent if m.get("chat_id") == STRANGER_ID]
        self.assertEqual(answers, [], "a stranger gets no answer")
        self.assertIsNone(notifier.paused())
        self.tg.add_message(OWNER_ID, "/stop")
        self.assertTrue(wait_until(lambda: notifier.paused() is not None))
        notifier.tick()
        self.assertTrue(wait_until(lambda: any("Your assistant is stopped" in m["text"] for m in self.tg.sent)))
        self.assertTrue(any("Seraphina is stopped" in (p["body"].get("message") or "") for p in self.ntfy.publishes))

    def test_no_token_reaches_a_file_or_an_event(self) -> None:
        notifier = self.running()
        self.job_notice()
        notifier.tick()
        self.tg.add_message(OWNER_ID, "help")
        self.assertTrue(wait_until(lambda: any(r.get("verb") == "help" for r in notifier.inbound())))
        files = _all_text(self.tmp / "data")
        for token in (TOKEN, HARNESS_TOKEN, REPLY_TOKEN):
            self.assertTrue(all(token not in line for line in files), token)
        dumped = json.dumps([event.to_dict() for event in self.bus.since(0)])
        for token in (TOKEN, HARNESS_TOKEN, REPLY_TOKEN):
            self.assertNotIn(token, dumped)


class TelegramPairingTests(EndToEndTests):
    paired = False

    def test_two_step_pairing_through_the_notifier(self) -> None:
        notifier = self.running()
        started = notifier.pair("telegram")
        self.assertRegex(started["text"], r"^/start [A-Za-z0-9]{12}$")
        self.assertEqual(started["link"], f"https://t.me/quiet_helper_bot?start={started['code']}")
        self.tg.add_message(STRANGER_ID, started["text"], username="someone")
        self.assertTrue(wait_until(lambda: notifier.pair_status("telegram")["state"] == "confirm"))
        self.assertEqual(notifier.pair_status("telegram")["account"]["id"], STRANGER_ID)
        self.assertEqual(notifier.channel("telegram").paired(), [], "nothing is paired by the /start message")
        with self.assertRaises(nt.NotifyError):
            notifier.pair_confirm("telegram", "12345")
        wrong = notifier.pair_confirm("telegram", "000000")
        self.assertEqual(wrong["state"], "confirm")
        self.assertEqual(notifier.channel("telegram").paired(), [])
        code = notifier.pair("telegram")
        self.tg.add_message(OWNER_ID, code["text"])
        self.assertTrue(wait_until(lambda: any("Your code is" in m["text"] for m in self.tg.sent
                                               if m.get("chat_id") == OWNER_ID)))
        six = next(m["text"] for m in self.tg.sent if m.get("chat_id") == OWNER_ID).split()[3].rstrip(".")
        result = notifier.pair_confirm("telegram", six)
        self.assertEqual(result["state"], "paired")
        self.assertEqual([a["id"] for a in notifier.channel("telegram").paired()], [OWNER_ID])
        self.assertTrue(self.tg.called("setMyCommands"))
        self.assertIsNone(notifier._state.get("tamper"), "pairing in the cockpit is a new baseline")
        self.assertEqual(notifier.audit.last("pairing")["account"], OWNER_ID)

    test_needs_human_on_both_channels_then_got_it_from_the_phone = None   # type: ignore[assignment]
    test_telegram_tap_answers_the_callback_and_removes_the_buttons = None   # type: ignore[assignment]
    test_telegram_strangers_and_typed_text = None   # type: ignore[assignment]
    test_forged_taps_foreign_notices_and_typed_text = None   # type: ignore[assignment]
    test_no_token_reaches_a_file_or_an_event = None   # type: ignore[assignment]



class ReviewFixTests(InboundCase):
    """Fixes from the W1 review: stop confirmations, answers through the outbox, settings under review, the
    clock gate before retention, the link only on this tailnet, inbound text redacted, ntfy setup text quoted."""

    def test_a_repeated_stop_confirms_once_and_answers_that_it_is_stopped(self) -> None:
        self.time.set("2026-10-21 22:15")   # do-not-disturb time: confirmations still pass
        outcomes = [self.say("phone", "stop") for _ in range(8)]
        self.assertEqual({o["outcome"] for o in outcomes}, {"applied"})
        self.assertEqual(len(self.notifier.messages(kind="confirm")["messages"]), 1, "one confirmation")
        self.assertEqual(outcomes[1]["answer"], "Seraphina is already stopped. Restart her in the cockpit on your "
                                                "computer.")
        self.assertEqual(self.say("telegram", "stop")["answer"], "Your assistant is already stopped. Restart it in the "
                                                                 "cockpit on your computer.")

    def test_a_stop_in_a_hold_is_applied_without_an_answer(self) -> None:
        for _ in range(11):
            self.say("telegram", "status")
        self.assertTrue(self.notifier._held("telegram"))
        self.notifier.pause(by="human")
        result = self.say("telegram", "stop")
        self.assertEqual((result["outcome"], result["answer"]), ("applied", None))

    def test_answers_go_through_the_outbox_on_telegram_too(self) -> None:
        channel = self.channel("telegram")
        inbound = Inbound(channel="telegram", kind="text", sender=str(OWNER_ID), text="status",
                          sent_at=iso(self.notifier.clock.utc()), received_at="", remote_id="77")
        channel.receive = lambda stop: iter([inbound])   # type: ignore[method-assign]
        self.notifier._receive("telegram", threading.Event())
        self.assertEqual(channel.finished, [(inbound, None)], "Telegram only deletes the typed text itself")
        answer = self.latest("answer")
        self.assertEqual(list(answer["deliveries"]), ["telegram"])
        self.assertFalse(answer["unread"])
        self.notifier.tick()
        sent = channel.sent[-1]
        self.assertIn("0 jobs wait for you.", sent.text, "sent as written, not as a heads-up")
        self.assertEqual((sent.priority, sent.buttons), ("low", []))

    def test_no_answer_while_phone_messages_are_off(self) -> None:
        self.factory.update_config({"notify": {"enabled": False}})
        channel = self.channel("telegram")
        inbound = Inbound(channel="telegram", kind="text", sender=str(OWNER_ID), text="help",
                          sent_at=iso(self.notifier.clock.utc()), received_at="", remote_id="78")
        channel.receive = lambda stop: iter([inbound])   # type: ignore[method-assign]
        self.notifier._receive("telegram", threading.Event())
        self.assertEqual(self.notifier.messages(kind="answer")["messages"], [])
        self.assertEqual(channel.finished, [(inbound, None)])
        with self.assertRaises(nt.NotifyError):
            self.notifier.pair("telegram")

    def test_while_settings_are_under_review_a_phone_can_only_stop(self) -> None:
        self.notifier.start(threads=False)   # records the settings baseline
        message, caps = self.sent_notice()
        data = json.loads(self.config.path.read_text())
        data["notify"]["phone_max_per_day"] = 200   # a hand edit
        self.config.path.write_text(json.dumps(data))
        result = self.say("telegram", "quiet morning")
        self.assertEqual(result["outcome"], "dropped", "caught at once, before the next deliver pass")
        self.assertEqual(self.notifier._state["tamper"]["what"], ["notify"])
        self.assertIsNone(self.notifier._state.get("quiet_until"))
        self.assertEqual(self.notifier.inbound(limit=1)[0]["outcome"], "dropped (settings under review)")
        self.assertEqual(self.tap("phone", "ack", caps["phone"]["ack"])["outcome"], "dropped")
        self.assertFalse(self.entry(message["id"])["read"])
        self.assertEqual(self.say("phone", "just saying")["outcome"], "dropped")
        self.assertEqual(self.say("telegram", "stop")["outcome"], "applied", "stop always works")
        self.assertEqual(self.notifier.paused()["by"], "tamper-check")

    def test_nothing_time_based_runs_before_the_clock_is_set(self) -> None:
        message = self.job_notice()
        self.time.advance(100 * 86400)   # a real-time clock far ahead, NTP not synchronised yet
        sync = TimeSync(require=True, fallback_minutes=10, check=lambda: False, uptime=lambda: 5.0,
                        monotonic=self.time)
        notifier = self.make_notifier(time_sync=sync)
        notifier.tick()
        entry = notifier._entries[message["id"]]
        self.assertFalse(entry.get("text_removed"), "retention waits for the clock")
        self.assertIn("waiting for your decision", entry["body"])
        self.assertIsNone(notifier._state.get("liveness"), "so does the liveness time")

    def test_the_cockpit_link_is_added_only_on_this_tailnet(self) -> None:
        for url, linked in (("https://cockpit.tail0123.ts.net", True), ("https://cockpit.other-tailnet.ts.net", False),
                            ("http://127.0.0.1:8765", True)):
            with self.subTest(url):
                self.factory.update_config({"notify": {"cockpit_url": url}})
                self.notifier.review("accept")
                self.notifier.test("phone")
                self.notifier.tick()
                link = self.channel("phone").sent[-1].link
                self.assertEqual(bool(link), linked, link)
                if linked:
                    self.assertTrue(link.startswith(url + "/#/messages/m/M-"))

    def test_inbound_text_is_kept_without_secrets(self) -> None:
        self.say("telegram", f"my key is {MASTER} and {HARNESS_TOKEN}")
        self.notifier.handle_inbound(Inbound(channel="phone", kind="forged", topic="seraphina-taps",
                                             text=f"steal {REPLY_TOKEN}", received_at="", remote_id="f9"))
        texts = [row.get("text") for row in self.notifier.inbound()]
        self.assertIn("my key is *** and ***", texts)
        self.assertIn("steal ***", texts)
        for token in (MASTER, HARNESS_TOKEN, REPLY_TOKEN):
            self.assertTrue(all(token not in line for line in _all_text(self.tmp / "data")), token)

    def test_the_cockpit_url_is_a_plain_url(self) -> None:
        for url in ("https://cockpit.tail0123.ts.net/?next=https://x.example", "https://c.ts.net/#x",
                    "https://u:p@c.ts.net", "https://c.ts.net/a b", "javascript://c.ts.net"):
            with self.subTest(url), self.assertRaises(ValueError):
                settings.update_config(self.config, {"notify": {"cockpit_url": url}})
        settings.update_config(self.config, {"notify": {"cockpit_url": "https://cockpit.tail0123.ts.net/sf/"}})

    def test_ntfy_setup_text_quotes_every_value(self) -> None:
        entry = ntfy_entry()
        yml = nt.ntfy_server_yml(entry, ("seraphina", "seraphina-taps", "seraphina-chat"))
        self.assertIn('base-url: "https://host.tail0123.ts.net:8445"', yml)
        self.assertIn('listen-unix: "/run/ntfy/ntfy.sock"', yml)
        self.assertEqual(ch.ntfy_forwarders(yml), [])
        bad = ntfy_entry(public_url='https://host.tail0123.ts.net:8445"\nfirebase-key-file: "/var/lib/x.json')
        with self.assertRaises(nt.NotifyError):
            nt.ntfy_server_yml(bad, ("seraphina", "seraphina-taps", "seraphina-chat"))
        with self.assertRaises(ValueError):
            settings.validate_settings(Config(self.config.path, {**self.config.raw, "notify": {
                **self.config.raw["notify"], "channels": {"phone": bad}}}))
        blocks = nt.ntfy_copy_blocks(Path("/data dir/notify/ntfy-setup"), entry)
        commands = [command for block in blocks for command in block["commands"]]
        self.assertIn("sudo install -m 0644 -o root -g root '/data dir/notify/ntfy-setup/server.yml' "
                      "/etc/ntfy/server.yml", commands)
        self.assertIn("sudo tailscale serve --bg --https=8445 unix:/run/ntfy/ntfy.sock", commands)


class BotDescriptionTests(EndToEndTests):
    """The Telegram bot says when the server sleeps, from wakes.power, and again after it changes."""

    def test_the_description_follows_the_power_schedule(self) -> None:
        notifier = self.notifier
        notifier.start(threads=False)
        self.addCleanup(notifier.stop)
        notifier.tick()
        self.assertTrue(wait_until(lambda: self.tg.called("setMyDescription")))
        self.assertEqual(self.tg.called("setMyDescription")[-1]["description"],
                         "This server sleeps 23:00-07:00. Messages sent then are read at 07:00. Telegram can read "
                         "what you type here.")
        self.factory.update_config({"wakes": {"power": {"off": "01:00", "wake": "06:00"}}})
        notifier.tick()
        self.assertEqual(self.tg.called("setMyDescription")[-1]["description"],
                         "This server sleeps 01:00-06:00. Messages sent then are read at 06:00. Telegram can read "
                         "what you type here.")
        count = len(self.tg.called("setMyDescription"))
        notifier.tick()
        self.assertEqual(len(self.tg.called("setMyDescription")), count, "only when it changed")

    test_needs_human_on_both_channels_then_got_it_from_the_phone = None   # type: ignore[assignment]
    test_telegram_tap_answers_the_callback_and_removes_the_buttons = None   # type: ignore[assignment]
    test_telegram_strangers_and_typed_text = None   # type: ignore[assignment]
    test_forged_taps_foreign_notices_and_typed_text = None   # type: ignore[assignment]
    test_no_token_reaches_a_file_or_an_event = None   # type: ignore[assignment]


class ReceiverReviewTests(EndToEndTests):
    """Receivers stop when phone messages are turned off; a tap during the first long poll is fresh (S34);
    an account added to the pairing file by hand is obeyed in nothing but stop (S47)."""

    def test_receivers_stop_when_phone_messages_are_turned_off(self) -> None:
        notifier = self.running()
        self.assertEqual(sorted(notifier._receivers), ["phone", "telegram"])
        self.factory.update_config({"notify": {"enabled": False}})
        notifier.supervise()
        self.assertEqual(notifier._receivers, {})
        calls = len(self.tg.offsets)
        time.sleep(0.6)
        self.assertEqual(len(self.tg.offsets), calls, "Telegram is no longer polled")

    def test_a_tap_during_the_first_long_poll_after_a_restart_is_applied(self) -> None:
        self.tg.max_wait = 2.0   # a real long poll waits up to 50 s
        notifier = self.running()
        message = self.job_notice()
        notifier.tick()
        sent = self.tg.sent[-1]
        data = sent["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        notifier.stop(time.monotonic() + 3.0)
        again = self.restart()
        again.start(threads=False)
        again.supervise()
        self.addCleanup(again.stop)
        before = len(self.tg.called("getUpdates"))
        self.assertTrue(wait_until(lambda: any(p["timeout"] > 0 for p in self.tg.called("getUpdates")[before:])),
                        "the new receiver drained the backlog and long-polls")
        time.sleep(0.5)
        self.tg.add_callback(OWNER_ID, data, sent["message_id"])
        self.assertTrue(wait_until(lambda: again._entries[message["id"]]["read"]), "applied, not late")
        self.assertTrue(wait_until(lambda: self.tg.called("answerCallbackQuery")))
        self.assertEqual(self.tg.called("answerCallbackQuery")[-1]["text"], "Marked as read.")

    def test_a_tap_that_waited_while_nothing_listened_is_late(self) -> None:
        notifier = self.running()
        message = self.job_notice()
        notifier.tick()
        sent = self.tg.sent[-1]
        data = sent["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        notifier.stop(time.monotonic() + 3.0)
        self.tg.add_callback(OWNER_ID, data, sent["message_id"])   # the server is off
        again = self.restart()
        again.start(threads=False)
        again.supervise()
        self.addCleanup(again.stop)
        self.assertTrue(wait_until(lambda: self.tg.called("answerCallbackQuery")))
        record = [r for r in again.inbound() if r["kind"] == "button"][0]
        self.assertEqual(record["outcome"], "late, not applied")
        self.assertFalse(again._entries[message["id"]]["read"])

    def test_an_account_added_by_hand_is_caught_and_obeyed_only_in_stop(self) -> None:
        notifier = self.running()
        # a hand edit of channels/telegram.json (under the channel's own lock, so its offset writes cannot race it)
        notifier.channel("telegram").update_state(lambda state: state["paired"].append({"id": STRANGER_ID,
                                                                                        "username": "added"}))
        self.tg.add_message(STRANGER_ID, "quiet morning")
        self.tg.add_message(STRANGER_ID, "status")
        self.assertTrue(wait_until(lambda: len([r for r in notifier.inbound()
                                                if r["outcome"] == "dropped (settings under review)"]) == 2))
        self.assertEqual(notifier._state["tamper"]["what"], ["pairing"])
        self.assertIsNone(notifier._state.get("quiet_until"))
        notifier.tick()
        time.sleep(0.2)
        reached = [m["text"].splitlines()[1] for m in self.tg.sent if m.get("chat_id") == STRANGER_ID]
        self.assertEqual(reached, ["Your server has a notice about its messaging. Open the cockpit."],
                         "no answer reaches it: only the content-free alarm, which goes to every paired account")
        self.assertEqual(notifier.messages(kind="answer")["messages"], [])

    test_needs_human_on_both_channels_then_got_it_from_the_phone = None   # type: ignore[assignment]
    test_telegram_tap_answers_the_callback_and_removes_the_buttons = None   # type: ignore[assignment]
    test_telegram_strangers_and_typed_text = None   # type: ignore[assignment]
    test_forged_taps_foreign_notices_and_typed_text = None   # type: ignore[assignment]
    test_no_token_reaches_a_file_or_an_event = None   # type: ignore[assignment]


class FactoryWiringTests(unittest.TestCase):
    """The Notifier inside a real Factory (DESIGN §34): started last, stopped last with a clean-stop record, its
    tokens redacted, a hand edit caught before an API save, harness events turned into notices."""

    def setUp(self) -> None:
        from unittest import mock

        from swfactory.orchestrator import Factory

        self.tmp = Path(tempfile.mkdtemp(prefix="sf-notify-factory-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patch = mock.patch.dict(os.environ, {"HOME": str(self.tmp / "home"), **ENV})
        patch.start()
        self.addCleanup(patch.stop)
        raw = {"server": {"token": MASTER}, "data_dir": str(self.tmp / "data"), "harness": {"sandbox": "bwrap"},
               "wakes": {"timezone": TZ, "time_sync": {"require": False}},
               "notify": {"channels": {"phone": ntfy_entry("unix:/nonexistent/ntfy.sock")},
                          "sensitive_terms_file": str(self.tmp / "terms.txt")}}
        self.config = Config(self.tmp / "conf" / "config.json", raw)
        self.config.save()
        self.config = Config.load(self.config.path)
        self.bus = EventBus()
        self.factory = Factory(self.config, self.bus, runner_factory=self.no_runner)

    @staticmethod
    def no_runner(config: Config, driver: str) -> Any:
        raise RuntimeError("no agent runs in these tests")

    def test_start_and_stop_record_a_clean_stop(self) -> None:
        self.factory.start()
        notifier = self.factory._notifier
        self.assertTrue(notifier._started)
        self.factory.stop(5)
        state = json.loads((self.tmp / "data" / "notify" / "state.json").read_text())
        self.assertEqual((state["stopping"], state["clean_stop"]), (True, True))

    def test_channel_tokens_are_redacted_by_the_factory(self) -> None:
        self.factory._notify_manager()
        self.factory._forget_secrets()
        values = self.factory.secret_values()
        self.assertIn(HARNESS_TOKEN, values)
        self.assertIn(REPLY_TOKEN, values)

    def test_a_hand_edit_is_caught_before_an_api_save_folds_it_in(self) -> None:
        self.factory.start()
        self.addCleanup(self.factory.stop, 5)
        data = json.loads(self.config.path.read_text())
        data["notify"]["routes"] = {"needs_you": ["phone"]}
        self.config.path.write_text(json.dumps(data))
        self.factory.update_config({"limits": {"max_turns": 77}})
        notifier = self.factory._notifier
        self.assertEqual(notifier._state["tamper"]["what"], ["notify"])
        self.assertIsNotNone(notifier.paused())
        self.assertIs(notifier.config, self.factory.config, "the new config reached the Notifier")

    def test_a_hand_edit_is_caught_before_a_persona_snapshot_import_saves(self) -> None:
        from unittest import mock

        from swfactory import personas

        self.factory.start()
        self.addCleanup(self.factory.stop, 5)
        data = json.loads(self.config.path.read_text())
        data["notify"]["phone_max_per_day"] = 200
        self.config.path.write_text(json.dumps(data))

        def import_profile(config: Config, **kwargs: Any) -> dict:   # the offline snapshot saves config.json
            with kwargs["writing"]():                                  # inside the settings write, as write_snapshot
                settings.update_config(config, {"limits": {"max_turns": 77}}, on_saved=kwargs["on_saved"])
            return {"card": {"model_id": "kate", "version_id": "v1", "fingerprint": "f", "source": "export"},
                    "snapshot": None}
        with mock.patch.object(personas, "import_profile", import_profile):
            self.factory.persona_import({"export": [], "model_id": "kate", "snapshot": {"name": "kate-offline"}})
        notifier = self.factory._notifier
        self.assertEqual(notifier._state["tamper"]["what"], ["notify"])
        self.assertEqual(notifier.paused()["by"], "tamper-check")

    def while_a_check_holds_the_lock(self, save: Callable[[], Any]) -> Any:
        """Run save on a thread while another thread (the tick, a receiver) holds the Notifier's settings lock
        after a hand edit of config.json; return the Notifier once both are done. The save must wait for the
        lock, never skip the S47 check and fold the hand edit into the new baseline."""
        notifier = self.factory._notify_manager()
        notifier.start(threads=False)
        self.addCleanup(notifier.stop, 5)
        data = json.loads(self.config.path.read_text())
        data["notify"]["phone_max_per_day"] = 200
        self.config.path.write_text(json.dumps(data))
        held, release, done = threading.Event(), threading.Event(), threading.Event()

        def a_check_on_another_thread() -> None:
            with notifier._settings_io:
                held.set()
                release.wait(5)
        checker = threading.Thread(target=a_check_on_another_thread)
        checker.start()
        self.assertTrue(held.wait(5))

        def run() -> None:
            save()
            done.set()
        saver = threading.Thread(target=run)
        saver.start()
        self.assertFalse(done.wait(0.3), "the save waits for the settings lock")
        release.set()
        saver.join(5)
        checker.join(5)
        self.assertTrue(done.is_set())
        self.assertEqual(notifier._state["tamper"]["what"], ["notify"], "the hand edit is caught, not folded in")
        return notifier

    def test_a_snapshot_import_waits_for_a_check_running_on_another_thread(self) -> None:
        """The offline snapshot's save (§33) is one step with the S47 check and the new baseline, as
        update_config's is (Factory._config_write)."""
        from unittest import mock

        from swfactory import personas

        def import_profile(config: Config, **kwargs: Any) -> dict:
            profile = mock.Mock(model_id="kate", base_model_id=None)
            with mock.patch.object(personas, "map_base_model", return_value=["opus"]), \
                    mock.patch.object(personas, "snapshot_model_entry", return_value={"provider": "deepseek",
                                                                                      "model": "deepseek-v4-pro"}), \
                    mock.patch.object(personas, "persona_file_text", return_value="---\nname: kate-offline\n---\n"), \
                    mock.patch.object(personas, "_save_persona_text", return_value=self.tmp / "kate-offline.md"):
                personas.write_snapshot(config, profile, {"name": "kate-offline"}, on_saved=kwargs["on_saved"],
                                        writing=kwargs["writing"])   # the real save; its persona file is mocked
            return {"card": {"model_id": "kate", "version_id": "v1", "fingerprint": "f", "source": "export"},
                    "snapshot": None}

        def save() -> None:
            with mock.patch.object(personas, "import_profile", import_profile):
                self.factory.persona_import({"export": [], "model_id": "kate", "snapshot": {"name": "kate-offline"}})
        self.while_a_check_holds_the_lock(save)
        self.assertIs(json.loads(self.config.path.read_text())["agents"]["kate-offline"]["offline"], True)

    def test_a_preset_waits_for_a_check_running_on_another_thread(self) -> None:
        self.while_a_check_holds_the_lock(lambda: self.factory.apply_preset("openwebui"))
        self.assertIn("openwebui", json.loads(self.config.path.read_text())["providers"])

    def test_a_job_event_on_the_bus_becomes_one_notice(self) -> None:
        self.factory.start()
        self.addCleanup(self.factory.stop, 5)
        notifier = self.factory._notifier
        self.assertTrue(wait_until(lambda: notifier._rules.get("baseline_done")), "the first reconcile ran")
        job = {"repo": "app", "id": "005-z", "stage": "needs-human", "updated_at": "2026-10-21T10:00:00Z"}
        self.bus.publish("job.updated", {"job": job}, repo="app", job="005-z")
        self.bus.publish("job.updated", {"job": job}, repo="app", job="005-z")
        self.assertTrue(wait_until(lambda: self.factory.messages(kind="job.needs_human")["messages"]))
        time.sleep(0.2)
        self.assertEqual(len(self.factory.messages(kind="job.needs_human")["messages"]), 1)

    def test_stop_and_restart_through_the_factory(self) -> None:
        self.assertIsNotNone(self.factory.wake_control("pause", by="phone", via="telegram")["paused"])
        with self.assertRaises(ValueError):   # a phone can never restart her (S37)
            self.factory.wake_control("resume", by="phone")
        self.assertIsNone(self.factory.wake_control("resume")["paused"])
        # step W2: one persona can be stopped, and restarted only from the cockpit or the CLI
        stopped = self.factory.wake_control("pause", persona="seraphina", by="phone", via="telegram")
        self.assertEqual(stopped["personas"]["seraphina"]["by"], "phone")
        with self.assertRaises(ValueError):
            self.factory.wake_control("resume", persona="seraphina", by="phone")
        self.assertNotIn("seraphina", self.factory.wake_control("resume", persona="seraphina")["personas"])


class NtfyPort80Test(unittest.TestCase):
    """The ntfy check's port-80 line: ntfy answering there (it should not, with the Unix socket setup) is a warning;
    another program on port 80 (a web server or a login gateway) is fine, and said so."""

    def test_port_80(self) -> None:
        self.assertEqual(nt.ntfy_port_80(True, {80}), {"tcp_listener": True, "port80_other": False})
        self.assertEqual(nt.ntfy_port_80(False, {80, 443}), {"tcp_listener": False, "port80_other": True})
        self.assertEqual(nt.ntfy_port_80(False, set()), {"tcp_listener": False, "port80_other": False})


if __name__ == "__main__":
    unittest.main()
