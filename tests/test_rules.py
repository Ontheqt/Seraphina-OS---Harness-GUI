"""Tests for swfactory.rules: the one notice rulebook (operator decision g, Wake & Messages §2.7, §3.4), and the
shared boundary the harness notifier applies from it."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path

from swfactory import rules
from swfactory.config import Config
from swfactory.notify import Notice
from tests.test_notify import NotifyCase

SHA = hashlib.sha256(b"a soul with nothing personal in it").hexdigest()


class RulebookTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sf-rules-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def config(self, **notify: object) -> Config:
        config = Config(self.tmp / "conf" / "config.json", {"data_dir": str(self.tmp / "data"), "notify": notify})
        config.save()
        return config

    def test_without_the_file_the_config_keys_are_the_rules(self) -> None:
        config = self.config(quiet_hours="22:00-07:00", per_persona={"min_minutes": 60, "max_per_day": 2},
                             phone_max_per_day=5)
        loaded = rules.load_rules(config)
        self.assertEqual((loaded["source"], loaded["error"], loaded["quiet_hours"]), ("config", None, "22:00-07:00"))
        self.assertEqual(loaded["rate_limits"]["per_persona_min_minutes"], 60)
        self.assertEqual(loaded["rate_limits"]["per_persona_max_per_day"], 2)
        self.assertEqual(loaded["rate_limits"]["phone_max_per_day"], 5)
        self.assertIsNone(rules.rules_digest(config))

    def test_the_file_is_written_private_and_versioned(self) -> None:
        config = self.config()
        written = rules.update_rules(config, {"quiet_hours": "23:00-06:30", "rate_limits": {"dedupe_hours": 12}})
        path = rules.rules_path(config)
        self.assertEqual(path, config.path.parent / "notice-rules.json")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual((written["source"], written["quiet_hours"]), ("file", "23:00-06:30"))
        self.assertEqual(written["rate_limits"]["dedupe_hours"], 12)
        self.assertEqual(written["rate_limits"]["phone_max_per_day"], 12, "the rest keep their defaults")
        doc = json.loads(path.read_text())
        self.assertEqual(doc["version"], rules.RULES_VERSION)
        self.assertEqual(doc["audit"], rules.AUDIT)
        self.assertNotIn("source", doc)
        self.assertEqual(rules.rules_digest(config), hashlib.sha256(path.read_bytes()).hexdigest())

    def test_the_boundary_can_be_made_stricter_never_weaker(self) -> None:
        good = {"version": 1, "boundary": {"max_body": 120, "token_run_chars": 12}}
        self.assertEqual(rules.validate_rules(good)["boundary"]["max_body"], 120)
        refused = [
            ({"version": 1, "boundary": {"max_body": 301}}, "stricter, never weaker"),
            ({"version": 1, "boundary": {"third_party": "anything"}}, "short_only"),
            ({"version": 1, "boundary": {"phone_short_until_profile_confirmed": False}}, "always true"),
            ({"version": 1, "boundary": {"tailnet_model_text": "always"}}, "clean_only"),
            ({"version": 1, "boundary": {"force_short_on": ["url"]}}, "every filter"),
            ({"version": 1, "audit": {"format": "csv"}}, "cannot be changed"),
            ({"version": 1, "rate_limits": {"phone_max_per_day": 1000}}, "from 0 to 200"),
            ({"version": 1, "quiet_hours": "late"}, "quiet_hours"),
            ({"version": 1, "quiet_breakthrough": ["persona"]}, "rule ids"),
            ({"version": 1, "extra": True}, "not a key"),
            ({"version": 2}, "version must be 1"),
            ({"version": 1, "confirmed_profiles": {"seraphina": {"sha256": "x", "what": "soul"}}}, "sha256"),
        ]
        for doc, words in refused:
            with self.subTest(doc=doc):
                with self.assertRaises(ValueError) as caught:
                    rules.validate_rules(doc)
                self.assertIn(words, str(caught.exception))

    def test_a_file_that_does_not_validate_fails_closed(self) -> None:
        config = self.config()
        path = rules.rules_path(config)
        path.write_text('{"version": 1, "boundary": {"max_body": 9000}}')
        loaded = rules.load_rules(config)
        self.assertEqual(loaded["boundary"]["tailnet_model_text"], "never", "a persona's words stay off every phone")
        self.assertIn("notice-rules.json", loaded["error"])
        path.write_text("not json")
        os.utime(path, (1, 1))
        self.assertEqual(rules.load_rules(config)["boundary"]["tailnet_model_text"], "never")

    def test_a_confirmation_is_bound_to_one_exact_text(self) -> None:
        config = self.config()
        confirmed = rules.confirm_profile(config, "seraphina", SHA, what="soul", clean=True)
        self.assertTrue(rules.confirmed(confirmed, "seraphina", SHA))
        self.assertFalse(rules.confirmed(confirmed, "seraphina", hashlib.sha256(b"edited").hexdigest()))
        self.assertFalse(rules.confirmed(confirmed, "kate", SHA))
        withdrawn = rules.confirm_profile(config, "seraphina", SHA, what="soul", clean=False)
        self.assertFalse(rules.confirmed(withdrawn, "seraphina", SHA))
        with self.assertRaises(ValueError):
            rules.confirm_profile(config, "seraphina", "abc", what="soul", clean=True)
        with self.assertRaises(ValueError):
            rules.confirm_profile(config, "seraphina", SHA, what="memory", clean=True)

    def test_defaults_are_not_shared_mutable_state(self) -> None:
        before = copy.deepcopy(rules.DEFAULT_RULES)
        loaded = rules.load_rules(self.config())
        loaded["boundary"]["max_body"] = 1
        loaded["confirmed_profiles"]["x"] = {}
        self.assertEqual(rules.DEFAULT_RULES, before)


class SharedBoundaryTest(NotifyCase):
    """Anything that reaches the phone stays a fixed short notice until the owner confirms the persona's profile
    holds no personal or health details; the harness notifier reads that from the rulebook."""

    def persona_notice(self, sha: str) -> dict:
        return self.notifier.notify(Notice("persona", "model", "persona", "default", "Backup done",
                                           "The nightly backup finished and looks fine.", sender="nova",
                                           persona="nova", fields={"why": "wake", "soul_sha256": sha,
                                                                   "display": "Nova", "agent": "Nova"}))

    def phone_texts(self) -> list[str]:
        return [rendered.text for rendered in self.channel("phone").sent]

    def test_a_persona_s_words_reach_the_private_phone_only_once_its_profile_is_confirmed(self) -> None:
        self.persona_notice(SHA)
        self.notifier.tick()
        self.assertTrue(self.phone_texts())
        self.assertNotIn("nightly backup finished", " ".join(self.phone_texts()), "unconfirmed: a heads-up only")
        rules.confirm_profile(self.config, "nova", SHA, what="soul", clean=True)
        self.persona_notice(SHA)
        self.notifier.tick()
        self.assertIn("nightly backup finished", self.phone_texts()[-1])
        third_party = [rendered.text for rendered in self.channel("telegram").sent]
        self.assertTrue(all("nightly backup" not in text for text in third_party), "a third party only gets heads-ups")

    def test_the_shipped_default_soul_counts_as_confirmed(self) -> None:
        from swfactory.personas import SHIPPED_SOUL_SHA256

        self.persona_notice(SHIPPED_SOUL_SHA256)
        self.notifier.tick()
        self.assertIn("nightly backup finished", self.phone_texts()[-1])

    def test_a_stricter_body_limit_in_the_rulebook_is_applied(self) -> None:
        rules.update_rules(self.config, {"boundary": {"max_body": 20}})
        rules.confirm_profile(self.config, "nova", SHA, what="soul", clean=True)
        self.persona_notice(SHA)
        self.notifier.tick()
        self.assertNotIn("nightly backup finished", " ".join(self.phone_texts()), "too long: the short notice")


if __name__ == "__main__":
    unittest.main()
