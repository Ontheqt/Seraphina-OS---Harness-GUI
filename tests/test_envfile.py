"""Tests for swfactory.envfile: the env file's one format, set_secret and secret_status."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from swfactory import envfile
from swfactory.__main__ import load_env_file
from swfactory.config import Config, read_dotenv_value
from tests.helpers import clean_env

INSTALL_SH_FILE = """# API keys for Software Factory: one NAME=value per line. `factory` reads this
# file at startup (variables already set win), and so does the systemd unit.
# Keep it mode 600. Uncomment what your presets use:
# DEEPSEEK_API_KEY=
# ANTHROPIC_API_KEY=
# TABBY_API_KEY=
"""


class EnvFileTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sf-envfile-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        patcher = mock.patch.dict(os.environ, clean_env(HOME=str(self.tmp / "home")), clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config = Config(self.tmp / "conf" / "config.json", {
            "providers": {"gpu0": {"type": "openai", "base_url": "http://127.0.0.1:8081/v1",
                                   "api_key_env": "GPU0_SERVER", "timeout": 60},
                          "keyfile": {"type": "openai", "base_url": "http://127.0.0.1:8082/v1",
                                      "api_key_env": "FILE_ONLY_API_KEY", "api_key_file": str(self.tmp / "k.env")}}})
        self.path = self.tmp / "conf" / "env"

    def read(self) -> str:
        return self.path.read_text(encoding="utf-8")


class FormatTest(EnvFileTestCase):
    def test_parse_lines(self) -> None:
        text = ("# comment\n\nA_KEY=plain\nexport B_TOKEN = 'single quoted' \nC=\"double # not a comment\"\n"
                "D=value # comment\nnot an assignment\n1BAD=x\n#E=commented\n")
        self.assertEqual(envfile.parse_env_text(text), [("A_KEY", "plain"), ("B_TOKEN", "single quoted"),
                                                        ("C", "double # not a comment"), ("D", "value")])

    def test_every_reader_gets_the_value_back(self) -> None:
        values = {"PLAIN_API_KEY": "sk-abc_DEF-123.456/789+=:@%,~^",
                  "SPACED_API_KEY": "two words # and a hash",
                  "SHELL_API_KEY": 'a"b$c\\d`e!f;g&h|i<j>k(l)m*n?o[p]q{r}s',
                  "TRAILING_HASH_API_KEY": "abc#def"}
        for name, value in values.items():
            envfile.set_secret(self.config, name, value)
        for name, value in values.items():
            with self.subTest(name=name):
                self.assertEqual(read_dotenv_value(self.path, name), value, "provider api_key_file reader")
                self.assertEqual(dict(envfile.parse_env_text(self.read()))[name], value)
        with mock.patch.dict(os.environ, {}, clear=True):
            load_env_file(self.path)   # what `factory serve` does at startup
            for name, value in values.items():
                self.assertEqual(os.environ[name], value, name)
        self.assertIn("PLAIN_API_KEY=sk-abc_DEF-123.456/789+=:@%,~^\n", self.read(), "safe values stay unquoted")
        self.assertIn("SPACED_API_KEY='two words # and a hash'\n", self.read(), "single quotes: literal for systemd")


class SetSecretTest(EnvFileTestCase):
    def test_creates_the_file_private_and_sets_the_environment(self) -> None:
        entry = envfile.set_secret(self.config, "DEEPSEEK_API_KEY", "sk-one")
        self.assertEqual(entry, {"name": "DEEPSEEK_API_KEY", "set": True, "in_env_file": True,
                                 "providers": ["deepseek", "deepseek-anthropic"]})
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode) & 0o077, 0)
        self.assertTrue(self.read().startswith("# API keys for Software Factory"))
        self.assertEqual(os.environ["DEEPSEEK_API_KEY"], "sk-one")
        self.assertEqual([p.name for p in self.path.parent.iterdir() if p.name.endswith(".tmp")], [])

    def test_replaces_in_place_keeps_other_lines_and_fills_a_placeholder(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text(INSTALL_SH_FILE + "OTHER_TOKEN=keep # mine\nexport ANTHROPIC_API_KEY=old\n"
                             "ANTHROPIC_API_KEY=older\n")
        os.chmod(self.path, 0o644)
        envfile.set_secret(self.config, "ANTHROPIC_API_KEY", "sk-new")
        envfile.set_secret(self.config, "TABBY_API_KEY", "tabby-key")
        text = self.read()
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600, "written 0600 whatever it was")
        self.assertEqual(text.count("ANTHROPIC_API_KEY="), 2, "the placeholder stays a comment, one assignment")
        self.assertIn("\nANTHROPIC_API_KEY=sk-new\n", text)
        self.assertNotIn("older", text)
        self.assertIn("# TABBY_API_KEY=\n", INSTALL_SH_FILE)
        self.assertIn("\nTABBY_API_KEY=tabby-key\n", text)
        self.assertNotIn("# TABBY_API_KEY=", text, "the commented placeholder became the assignment")
        self.assertIn("OTHER_TOKEN=keep # mine\n", text)
        self.assertIn("# DEEPSEEK_API_KEY=\n", text)

    def test_empty_value_removes_the_name(self) -> None:
        envfile.set_secret(self.config, "OPENAI_API_KEY", "sk-x")
        entry = envfile.set_secret(self.config, "OPENAI_API_KEY", "")
        self.assertEqual((entry["set"], entry["in_env_file"]), (False, False))
        self.assertNotIn("OPENAI_API_KEY", os.environ)
        self.assertNotIn("OPENAI_API_KEY", self.read())

    def test_validation_never_names_the_value(self) -> None:
        secret = "sk-very-secret-value"
        cases = [("lower_case", "x", "name must match"), ("A", "x", "name must match"),
                 ("X" * 65, "x", "name must match"), ("HOME", "x", "does not look like a credential"),
                 ("OPENAI_BASE_URL", "x", "does not look like a credential"),
                 ("OPENAI_API_KEY", secret + "\n", "single line"), ("OPENAI_API_KEY", secret + "\x00", "single line"),
                 ("OPENAI_API_KEY", " " + secret, "spaces"), ("OPENAI_API_KEY", secret + "'", "single quote"),
                 ("OPENAI_API_KEY", secret + " ", "single line"),
                 ("OPENAI_API_KEY", "x" * 4097, "longer than 4096"), ("OPENAI_API_KEY", 5, "must be a string")]
        for name, value, message in cases:
            with self.subTest(name=name, message=message):
                with self.assertRaisesRegex(ValueError, message) as caught:
                    envfile.set_secret(self.config, name, value)
                self.assertNotIn(secret, str(caught.exception))
        self.assertFalse(self.path.exists())

    def test_a_value_cannot_break_the_line_for_any_reader(self) -> None:
        # str.splitlines() also breaks at these: a value holding one must not plant a second variable
        breaks = {"line separator": " ", "paragraph separator": " ", "next line": "\x85",
                  "vertical tab": "\x0b", "form feed": "\x0c", "file separator": "\x1c", "carriage return": "\r",
                  "line feed": "\n"}
        others = {"zero width space": "​", "byte order mark": "﻿", "bidi override": "‮",
                  "no-break space": " ", "tab": "\t", "delete": "\x7f", "c1 control": "\x9b"}
        envfile.set_secret(self.config, "DEEPSEEK_API_KEY", "sk-good")
        before = self.read()
        for label, char in {**breaks, **others}.items():
            with self.subTest(char=label):
                with self.assertRaisesRegex(ValueError, "single line of printable characters"):
                    envfile.set_secret(self.config, "DEEPSEEK_API_KEY", f"abc{char}LD_PRELOAD=/tmp/evil.so")
        self.assertEqual(self.read(), before, "nothing written")
        self.assertEqual(os.environ["DEEPSEEK_API_KEY"], "sk-good")
        self.assertNotIn("LD_PRELOAD", os.environ)
        entry = envfile.set_secret(self.config, "DEEPSEEK_API_KEY", "clé-ünïcode €")   # printable is fine
        self.assertTrue(entry["set"])
        self.assertEqual(dict(envfile.parse_env_text(self.read()))["DEEPSEEK_API_KEY"], "clé-ünïcode €")

    def test_readers_split_lines_as_systemd_does(self) -> None:
        # a hand-edited file: only \n and \r end a line (systemd's EnvironmentFile=), so the factory, the
        # api_key_file reader and systemd all see one assignment here, never LD_PRELOAD
        text = ("DEEPSEEK_API_KEY='abc LD_PRELOAD=/tmp/a.so'\nOTHER_TOKEN=x\x85LD_PRELOAD=/tmp/b.so\r"
                "CR_TOKEN=cr\r\nLAST_TOKEN=last")
        self.assertEqual(envfile.parse_env_text(text), [("DEEPSEEK_API_KEY", "abc LD_PRELOAD=/tmp/a.so"),
                                                        ("OTHER_TOKEN", "x\x85LD_PRELOAD=/tmp/b.so"),
                                                        ("CR_TOKEN", "cr"), ("LAST_TOKEN", "last")])
        self.path.parent.mkdir(parents=True)
        self.path.write_text(text, encoding="utf-8", newline="")
        os.chmod(self.path, 0o600)
        self.assertIsNone(read_dotenv_value(self.path, "LD_PRELOAD"))
        self.assertEqual(read_dotenv_value(self.path, "CR_TOKEN"), "cr")
        with mock.patch.dict(os.environ, {}, clear=True):
            load_env_file(self.path)
            self.assertNotIn("LD_PRELOAD", os.environ)
            self.assertEqual(os.environ["LAST_TOKEN"], "last")
        envfile.set_secret(self.config, "CR_TOKEN", "new")   # a rewrite keeps the other lines whole
        rewritten = self.path.read_bytes().decode("utf-8")   # no newline translation
        self.assertIn("DEEPSEEK_API_KEY='abc LD_PRELOAD=/tmp/a.so'\n", rewritten)
        self.assertIn("\nCR_TOKEN=new\n", rewritten)
        self.assertNotIn("\r", rewritten)
        self.assertEqual([name for name, _ in envfile.parse_env_text(rewritten)],
                         ["DEEPSEEK_API_KEY", "OTHER_TOKEN", "CR_TOKEN", "LAST_TOKEN"])

    def test_a_provider_key_name_need_not_look_like_a_credential(self) -> None:
        entry = envfile.set_secret(self.config, "GPU0_SERVER", "local-secret")
        self.assertEqual(entry["providers"], ["gpu0"])
        self.assertEqual(os.environ["GPU0_SERVER"], "local-secret")

    def test_concurrent_writes_keep_every_name(self) -> None:
        names = [f"KEY{n}_API_KEY" for n in range(12)]
        threads = [threading.Thread(target=envfile.set_secret, args=(self.config, name, f"value-{name}"))
                   for name in names]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(dict(envfile.parse_env_text(self.read()))), sorted(names))


class StatusTest(EnvFileTestCase):
    def test_lists_provider_keys_and_credentials_in_the_file_by_name(self) -> None:
        (self.tmp / "k.env").write_text("FILE_ONLY_API_KEY=from-file\n")
        self.path.parent.mkdir(parents=True)
        self.path.write_text("EXTRA_TOKEN=abc\nPATH=/usr/bin\n")
        os.environ["ANTHROPIC_API_KEY"] = "sk-env"
        status = envfile.secret_status(self.config)
        self.assertEqual(status["env_file"], str(self.path))
        entries = {entry["name"]: entry for entry in status["secrets"]}
        self.assertNotIn("PATH", entries, "only credentials and provider keys are listed")
        self.assertEqual(entries["EXTRA_TOKEN"], {"name": "EXTRA_TOKEN", "set": False, "in_env_file": True,
                                                  "providers": []})
        self.assertTrue(entries["ANTHROPIC_API_KEY"]["set"])
        self.assertFalse(entries["ANTHROPIC_API_KEY"]["in_env_file"])
        self.assertTrue(entries["FILE_ONLY_API_KEY"]["set"], "resolved from the provider's api_key_file")
        self.assertFalse(entries["DEEPSEEK_API_KEY"]["set"])
        self.assertIn("TABBY_API_KEY", entries)
        self.assertEqual([entry["name"] for entry in status["secrets"]], sorted(entries))
        self.assertNotIn("sk-env", repr(status))
        self.assertNotIn("from-file", repr(status))

    def test_unreadable_file_still_lists_the_provider_keys(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b"\xff\xfe broken")
        status = envfile.secret_status(self.config)
        self.assertIn("DEEPSEEK_API_KEY", [entry["name"] for entry in status["secrets"]])


if __name__ == "__main__":
    unittest.main()


class ChannelSecretStatusTest(EnvFileTestCase):
    def test_configured_channel_tokens_are_listed_before_they_are_set(self) -> None:
        self.config.raw["notify"] = {"channels": {"phone": {"type": "ntfy", "token_env": "SERAPHINA_NTFY_TOKEN",
                                                            "reply_token_env": "SERAPHINA_NTFY_REPLY_TOKEN"},
                                                  "telegram": {"type": "telegram",
                                                               "token_env": "SERAPHINA_TELEGRAM_BOT_TOKEN"}}}
        rows = {row["name"]: row for row in envfile.secret_status(self.config)["secrets"]}
        self.assertEqual(rows["SERAPHINA_NTFY_TOKEN"]["channels"], ["phone"])
        self.assertFalse(rows["SERAPHINA_TELEGRAM_BOT_TOKEN"]["set"])
        envfile.set_secret(self.config, "SERAPHINA_TELEGRAM_BOT_TOKEN", "7123:AAH-test-0123456789")
        rows = {row["name"]: row for row in envfile.secret_status(self.config)["secrets"]}
        self.assertTrue(rows["SERAPHINA_TELEGRAM_BOT_TOKEN"]["set"])
        self.assertNotIn("7123:AAH", str(rows))

    def test_read_secret_from_stdin(self) -> None:
        import io
        self.assertEqual(envfile.read_secret_from_stdin(io.StringIO("value\r\nmore\n")), "value")
        with self.assertRaises(ValueError):
            envfile.read_secret_from_stdin(io.StringIO(""))
