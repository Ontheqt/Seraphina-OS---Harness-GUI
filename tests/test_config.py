from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from swfactory import config as cfg
from swfactory.config import (DEFAULTS, Config, ModelConfig, ProviderConfig, agent_dirs, deep_merge,
                              default_config_path, init_config, read_dotenv_value, resolve_api_key,
                              sanitize_repo_name)
from tests.helpers import clean_env, make_repo

KEY_ENVS = ("DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY",
            "DASHSCOPE_API_KEY", "NANOGPT_API_KEY", "STRATA_API_KEY", "TEST_KEY")


class ConfigTestCase(unittest.TestCase):
    """Each test gets a temp HOME, no FACTORY_CONFIG and no provider keys in the environment."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.home = self.tmp / "home"
        self.home.mkdir()
        env = {k: v for k, v in clean_env(HOME=str(self.home)).items() if k not in KEY_ENVS}
        env.pop("FACTORY_CONFIG", None)
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = self.tmp / "conf" / "config.json"

    def write_config(self, data: dict) -> Config:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data))
        return Config.load(self.path)


class DefaultsTest(unittest.TestCase):
    def test_section_18_provider_table(self) -> None:
        table = {
            "deepseek": ("openai", "https://api.deepseek.com", "DEEPSEEK_API_KEY"),
            "deepseek-anthropic": ("anthropic", "https://api.deepseek.com/anthropic", "DEEPSEEK_API_KEY"),
            "anthropic": ("anthropic", "https://api.anthropic.com", "ANTHROPIC_API_KEY"),
            "openai": ("openai", "https://api.openai.com/v1", "OPENAI_API_KEY"),
            "openrouter": ("openai", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
            "dashscope": ("openai", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY"),
            "ollama": ("openai", "http://127.0.0.1:11434/v1", None),
            "vllm": ("openai", "http://127.0.0.1:8000/v1", None),
            "llamacpp": ("openai", "http://127.0.0.1:8080/v1", None),
            "lmstudio": ("openai", "http://127.0.0.1:1234/v1", None),
            "tabby": ("openai", "http://127.0.0.1:5000/v1", "TABBY_API_KEY"),
            "nanogpt": ("openai", "https://nano-gpt.com/api/v1", "NANOGPT_API_KEY"),
            "nanogpt-anthropic": ("anthropic", "https://nano-gpt.com/api", "NANOGPT_API_KEY"),
        }
        providers = DEFAULTS["providers"]
        for name, (type_, url, key_env) in table.items():
            with self.subTest(provider=name):
                self.assertEqual(providers[name]["type"], type_)
                self.assertEqual(providers[name]["base_url"], url)
                self.assertEqual(providers[name]["api_key_env"], key_env)
        self.assertEqual(providers["nanogpt-anthropic"]["auth"], "both")
        self.assertEqual(providers["deepseek"]["timeout"], 900)

    def test_tabby_provider(self) -> None:
        """DESIGN §18/§23: TabbyAPI accepts x-api-key (Bearer is harmless), one slot, kept per run."""
        self.assertEqual(DEFAULTS["providers"]["tabby"], {
            "type": "openai", "base_url": "http://127.0.0.1:5000/v1", "api_key_env": "TABBY_API_KEY",
            "auth": "both", "max_concurrency": 1, "timeout": 1800, "concurrency_scope": "run"})
        provider = Config(Path("/nonexistent/config.json")).provider("tabby")
        self.assertEqual((provider.auth, provider.max_concurrency, provider.concurrency_scope), ("both", 1, "run"))

    def test_strata_provider_and_local_model(self) -> None:
        self.assertEqual(DEFAULTS["providers"]["strata"], {
            "type": "openai", "base_url": "http://127.0.0.1:18080/v1", "api_key_env": "STRATA_API_KEY",
            "api_key_file": "/etc/ai-stack/strata.env", "max_concurrency": 1, "timeout": 1800,
            "concurrency_scope": "run"})
        # Section 20: the local servers run a >= 262K KV cache; the old 32K/4K figures are superseded.
        self.assertEqual(DEFAULTS["models"]["local"], {
            "provider": "strata", "model": "strata", "max_tokens": 16384, "context_tokens": 262144,
            "keep_reasoning": False, "stream": True, "extra_body": {}})

    def test_section_21_concurrency_scope_and_anthropic_beta(self) -> None:
        providers = DEFAULTS["providers"]
        for name in ("strata", "ollama", "llamacpp", "vllm", "lmstudio", "tabby"):
            with self.subTest(provider=name):
                self.assertEqual(providers[name]["concurrency_scope"], "run")
        for name in ("deepseek", "deepseek-anthropic", "anthropic", "openai", "openrouter", "dashscope",
                     "nanogpt", "nanogpt-anthropic"):
            with self.subTest(provider=name):
                self.assertNotIn("concurrency_scope", providers[name])
        self.assertEqual(providers["anthropic"]["headers"], {"anthropic-beta": "server-side-fallback-2026-07-01"})
        for name in ("deepseek-anthropic", "nanogpt-anthropic"):
            self.assertNotIn("headers", providers[name])

    def test_section_11_values(self) -> None:
        self.assertEqual(DEFAULTS["data_dir"], "~/.local/share/software-factory")
        self.assertEqual(DEFAULTS["server"], {"host": "127.0.0.1", "port": 8765, "token": None,
                                              "allowed_hosts": []})
        self.assertEqual(DEFAULTS["models"]["opus"]["model"], "deepseek-v4-pro")
        self.assertEqual(DEFAULTS["models"]["sonnet"]["model"], "deepseek-v4-flash")
        self.assertEqual(DEFAULTS["models"]["opus"]["extra_body"],
                         {"thinking": {"type": "enabled"}, "reasoning_effort": "high"})
        self.assertEqual(DEFAULTS["models"]["haiku"]["extra_body"], {"thinking": {"type": "disabled"}})
        self.assertEqual(DEFAULTS["agents"], {"*": {"driver": "builtin"}})
        self.assertEqual(sorted(DEFAULTS["drivers"]), ["builtin", "claude", "codex", "codex-worker", "qwen"])
        self.assertEqual(DEFAULTS["pipeline"]["reviewers"], ["security", "ux", "ui", "code"])
        self.assertEqual(DEFAULTS["limits"]["max_context_chars"], 1500000)
        self.assertIn("**/*.key", DEFAULTS["escalate_paths"])
        self.assertEqual(DEFAULTS["merge"], {"push": False, "remote": "origin", "delivery": "merge",
                                             "output_dir": None})
        json.dumps(DEFAULTS)  # serialisable as-is

    def test_default_tiers_do_not_share_mutable_extra_body(self) -> None:
        self.assertIsNot(DEFAULTS["models"]["opus"]["extra_body"], DEFAULTS["models"]["sonnet"]["extra_body"])


class DeepMergeTest(unittest.TestCase):
    def test_dicts_merge_lists_and_scalars_replace(self) -> None:
        base = {"a": {"x": 1, "y": {"z": 2}}, "l": [1, 2], "s": "old", "keep": True}
        override = {"a": {"y": {"w": 3}}, "l": [9], "s": "new", "extra": {"k": 1}}
        merged = deep_merge(base, override)
        self.assertEqual(merged, {"a": {"x": 1, "y": {"z": 2, "w": 3}}, "l": [9], "s": "new",
                                  "keep": True, "extra": {"k": 1}})

    def test_none_replaces_and_inputs_are_not_mutated(self) -> None:
        base = {"d": {"x": 1}, "n": {"m": [1]}}
        override = {"d": None, "n": {"m": [2]}}
        merged = deep_merge(base, override)
        self.assertIsNone(merged["d"])
        merged["n"]["m"].append(3)
        self.assertEqual(base, {"d": {"x": 1}, "n": {"m": [1]}})
        self.assertEqual(override, {"d": None, "n": {"m": [2]}})

    def test_dict_replaces_scalar(self) -> None:
        self.assertEqual(deep_merge({"a": 1}, {"a": {"b": 2}}), {"a": {"b": 2}})


class PathsTest(ConfigTestCase):
    def test_default_config_path(self) -> None:
        self.assertEqual(default_config_path(), self.home / ".config" / "software-factory" / "config.json")

    def test_factory_config_env_overrides(self) -> None:
        with mock.patch.dict(os.environ, {"FACTORY_CONFIG": "~/elsewhere/c.json"}):
            self.assertEqual(default_config_path(), self.home / "elsewhere" / "c.json")
            self.assertEqual(Config.load().path, self.home / "elsewhere" / "c.json")

    def test_sanitize_repo_name(self) -> None:
        self.assertEqual(sanitize_repo_name("My App"), "my-app")
        self.assertEqual(sanitize_repo_name("__Foo.Bar__"), "foo.bar__")
        self.assertEqual(sanitize_repo_name("a  b!!c-"), "a-b-c")
        self.assertEqual(sanitize_repo_name("..."), "")


class LoadSaveTest(ConfigTestCase):
    def test_missing_file_gives_defaults_without_token(self) -> None:
        config = Config.load(self.path)
        self.assertEqual(config.raw, DEFAULTS)
        self.assertIsNot(config.raw["models"], DEFAULTS["models"])
        self.assertIsNone(config.raw["server"]["token"])
        self.assertEqual(config.path, self.path)
        self.assertEqual(config.data_dir, self.home / ".local" / "share" / "software-factory")
        self.assertFalse(self.path.exists())

    def test_partial_file_is_merged_over_defaults(self) -> None:
        config = self.write_config({"server": {"port": 9000}, "models": {"sonnet": {"model": "other"}},
                                    "pipeline": {"reviewers": ["code"]}, "drivers": {"codex": None}})
        self.assertEqual(config.raw["server"]["port"], 9000)
        self.assertEqual(config.raw["server"]["host"], "127.0.0.1")
        self.assertEqual(config.raw["models"]["sonnet"]["model"], "other")
        self.assertEqual(config.raw["models"]["sonnet"]["provider"], "deepseek")
        self.assertEqual(config.pipeline().reviewers, ["code"])
        with self.assertRaises(KeyError):
            config.driver("codex")

    def test_data_dir_expansion(self) -> None:
        self.assertEqual(self.write_config({"data_dir": "~/factory"}).data_dir, self.home / "factory")
        self.assertEqual(self.write_config({"data_dir": "rel/data"}).data_dir, self.path.parent / "rel" / "data")
        self.assertEqual(self.write_config({"data_dir": "/srv/ai-work/factory"}).data_dir,
                         Path("/srv/ai-work/factory"))

    def test_invalid_json_and_non_object(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{nope")
        with self.assertRaisesRegex(ValueError, "invalid JSON"):
            Config.load(self.path)
        self.path.write_text("[1, 2]")
        with self.assertRaisesRegex(ValueError, "JSON object"):
            Config.load(self.path)

    def test_save_is_atomic_private_and_round_trips(self) -> None:
        config = Config(self.path, {"server": {"token": "t" * 32}, "data_dir": "/data"})
        config.save()
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["config.json"])
        again = Config.load(self.path)
        self.assertEqual(again.raw, config.raw)
        self.assertEqual(again.data_dir, Path("/data"))

    def test_save_replaces_an_existing_file_and_fixes_its_mode(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{}")
        os.chmod(self.path, 0o644)
        config = Config.load(self.path)
        config.raw["server"]["port"] = 1234
        config.save()
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(json.loads(self.path.read_text())["server"]["port"], 1234)

    def test_save_keeps_a_minimal_file_minimal(self) -> None:
        written = {"data_dir": "/srv/ai-work/factory", "models": {"sonnet": {"provider": "strata", "model": "x"}},
                   "drivers": {"codex": None}, "pipeline": {"max_resumes": 2}}
        config = self.write_config(written)
        config.raw["limits"]["max_turns"] = 40
        config.raw["providers"]["gw"] = {"type": "openai", "base_url": "http://gw"}
        config.add_repo(make_repo(self.tmp, "app"))
        data = json.loads(self.path.read_text())
        self.assertEqual(data, {**written, "limits": {"max_turns": 40},
                                "providers": {"gw": {"type": "openai", "base_url": "http://gw"}},
                                "repos": [{"name": "app", "path": str(self.tmp / "app"), "base": None,
                                           "setup": None, "escalate_paths": []}]})
        self.assertEqual(Config.load(self.path).raw, config.raw)

    def test_unset_defaults_follow_later_versions(self) -> None:
        config = self.write_config({"models": {"opus": {"max_tokens": 1000}}})
        config.add_repo(make_repo(self.tmp, "app"))
        self.assertNotIn("local", json.loads(self.path.read_text())["models"])
        newer = deep_merge(DEFAULTS, {"models": {"local": {"max_tokens": 20000}}})
        with mock.patch.object(cfg, "DEFAULTS", newer):
            reloaded = Config.load(self.path)
        self.assertEqual(reloaded.model("local").max_tokens, 20000)
        self.assertEqual(reloaded.model("opus").max_tokens, 1000)
        self.assertEqual([repo.name for repo in reloaded.repos()], ["app"])

    def test_save_records_removed_and_retyped_values(self) -> None:
        config = Config(self.path)
        del config.raw["drivers"]["codex"]
        config.raw["pipeline"]["max_resumes"] = 2.0
        config.raw["models"]["opus"]["stream"] = 1
        config.save()
        data = json.loads(self.path.read_text())
        self.assertEqual(data, {"drivers": {"codex": None}, "pipeline": {"max_resumes": 2.0},
                                "models": {"opus": {"stream": 1}}})
        self.assertNotIn("server", data)

    def test_failed_save_leaves_no_temp_file(self) -> None:
        config = Config(self.path)
        with mock.patch.object(cfg.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                config.save()
        self.assertEqual(list(self.path.parent.iterdir()), [])


class InitConfigTest(ConfigTestCase):
    def test_writes_full_defaults_and_token(self) -> None:
        config = init_config(self.path)
        data = json.loads(self.path.read_text())
        token = data["server"]["token"]
        self.assertRegex(token, r"^[0-9a-f]{32}$")
        self.assertEqual(config.raw["server"]["token"], token)
        expected = json.loads(json.dumps(DEFAULTS))
        expected["server"]["token"] = token
        self.assertEqual(data, expected)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_refuses_to_overwrite_unless_forced(self) -> None:
        first = init_config(self.path).raw["server"]["token"]
        with self.assertRaises(FileExistsError):
            init_config(self.path)
        self.assertEqual(json.loads(self.path.read_text())["server"]["token"], first)
        second = init_config(self.path, force=True).raw["server"]["token"]
        self.assertNotEqual(first, second)
        self.assertEqual(json.loads(self.path.read_text())["server"]["token"], second)

    def test_default_path(self) -> None:
        with mock.patch.dict(os.environ, {"FACTORY_CONFIG": str(self.tmp / "x" / "c.json")}):
            init_config()
        self.assertTrue((self.tmp / "x" / "c.json").is_file())

    def test_repo_add_before_init_then_init_adds_token_and_keeps_repos(self) -> None:
        Config.load(self.path).add_repo(make_repo(self.tmp, "app"))
        self.assertNotIn("server", json.loads(self.path.read_text()))
        config = init_config(self.path)
        data = json.loads(self.path.read_text())
        self.assertRegex(data["server"]["token"], r"^[0-9a-f]{32}$")
        self.assertEqual(config.raw["server"]["token"], data["server"]["token"])
        self.assertEqual([repo["name"] for repo in data["repos"]], ["app"])
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        with self.assertRaises(FileExistsError):
            init_config(self.path)

    def test_init_fills_token_when_server_is_null(self) -> None:
        self.write_config({"server": None, "data_dir": "/d"})
        init_config(self.path)
        data = json.loads(self.path.read_text())
        self.assertEqual(data["data_dir"], "/d")
        self.assertEqual(Config.load(self.path).raw["server"]["port"], 8765)
        self.assertTrue(data["server"]["token"])

    def test_init_config_stays_full_after_a_change(self) -> None:
        config = init_config(self.path)
        config.add_repo(make_repo(self.tmp, "app"))
        data = json.loads(self.path.read_text())
        self.assertEqual(data["models"], DEFAULTS["models"])
        self.assertEqual(data["escalate_paths"], DEFAULTS["escalate_paths"])
        self.assertEqual(len(data["repos"]), 1)


class ReposTest(ConfigTestCase):
    def test_add_repo_derives_sanitised_name_and_persists(self) -> None:
        repo_dir = make_repo(self.tmp, "My Cool_App")
        config = Config.load(self.path)
        repo = config.add_repo(repo_dir)
        self.assertEqual(repo.name, "my-cool_app")
        self.assertEqual(repo.path, repo_dir)
        self.assertIsNone(repo.base)
        self.assertEqual(Config.load(self.path).repo("my-cool_app").path, repo_dir)

    def test_add_repo_explicit_name_and_tilde_path(self) -> None:
        repo_dir = make_repo(self.home, "proj")
        repo = Config.load(self.path).add_repo("~/proj", name="web")
        self.assertEqual((repo.name, repo.path), ("web", repo_dir))

    def test_add_repo_rejects_non_toplevel_non_git_and_missing(self) -> None:
        repo_dir = make_repo(self.tmp, "repo", {"sub/a.txt": "a"})
        plain = self.tmp / "plain"
        plain.mkdir()
        config = Config.load(self.path)
        with self.assertRaisesRegex(ValueError, "top level"):
            config.add_repo(repo_dir / "sub")
        with self.assertRaisesRegex(ValueError, "not a git repository"):
            config.add_repo(plain)
        with self.assertRaisesRegex(ValueError, "not a directory"):
            config.add_repo(self.tmp / "missing")
        self.assertEqual(config.repos(), [])
        self.assertFalse(self.path.exists())

    def test_add_repo_rejects_duplicates_and_bad_names(self) -> None:
        first = make_repo(self.tmp, "one")
        second = make_repo(self.tmp, "two")
        config = Config.load(self.path)
        config.add_repo(first, name="app")
        with self.assertRaisesRegex(ValueError, "already exists"):
            config.add_repo(second, name="app")
        with self.assertRaisesRegex(ValueError, "already configured"):
            config.add_repo(first, name="other")
        for bad in ("Bad", "-x", "a/b", ""):
            with self.subTest(name=bad), self.assertRaisesRegex(ValueError, "invalid repo name"):
                config.add_repo(second, name=bad)
        self.assertEqual([r.name for r in config.repos()], ["app"])

    def test_add_repo_without_derivable_name(self) -> None:
        repo_dir = make_repo(self.tmp, "___")
        with self.assertRaisesRegex(ValueError, "cannot derive"):
            Config.load(self.path).add_repo(repo_dir)

    def test_add_repo_rolls_back_when_save_fails(self) -> None:
        repo_dir = make_repo(self.tmp, "app")
        config = Config.load(self.path)
        with mock.patch.object(Config, "save", side_effect=OSError("read-only")):
            with self.assertRaises(OSError):
                config.add_repo(repo_dir)
        self.assertEqual(config.repos(), [])

    def test_remove_repo(self) -> None:
        config = Config.load(self.path)
        config.add_repo(make_repo(self.tmp, "a"))
        config.add_repo(make_repo(self.tmp, "b"))
        config.remove_repo("a")
        self.assertEqual([r.name for r in Config.load(self.path).repos()], ["b"])
        with self.assertRaises(KeyError):
            config.remove_repo("a")

    def test_repo_lookup_and_path_expansion(self) -> None:
        config = self.write_config({"repos": [
            {"name": "home", "path": "~/code/x", "base": "develop", "setup": "make", "escalate_paths": ["db/**"]},
            {"name": "rel", "path": "projects/y"}]})
        home = config.repo("home")
        self.assertEqual(home.path, self.home / "code" / "x")
        self.assertEqual((home.base, home.setup, home.escalate_paths), ("develop", "make", ["db/**"]))
        rel = config.repo("rel")
        self.assertEqual(rel.path, self.path.parent / "projects" / "y")
        self.assertEqual((rel.base, rel.setup, rel.escalate_paths), (None, None, []))
        with self.assertRaises(KeyError):
            config.repo("nope")

    def test_malformed_repo_entry(self) -> None:
        with self.assertRaisesRegex(ValueError, "needs a name and a path"):
            self.write_config({"repos": [{"name": "x"}]}).repos()

    def test_escalate_paths_global_then_repo_deduplicated(self) -> None:
        config = self.write_config({"escalate_paths": ["a/**", "b"],
                                    "repos": [{"name": "r", "path": "/r", "escalate_paths": ["b", "c", "a/**"]}]})
        self.assertEqual(config.escalate_paths(config.repo("r")), ["a/**", "b", "c"])
        self.assertEqual(Config.load(self.path).escalate_paths(cfg.RepoConfig("x", Path("/x"))),
                         ["a/**", "b"])


class ProviderTest(ConfigTestCase):
    def test_provider_fields_and_auth_defaults(self) -> None:
        config = Config.load(self.path)
        deepseek = config.provider("deepseek")
        self.assertEqual((deepseek.type, deepseek.base_url, deepseek.api_key_env, deepseek.timeout),
                         ("openai", "https://api.deepseek.com", "DEEPSEEK_API_KEY", 900.0))
        self.assertEqual((deepseek.auth, deepseek.headers, deepseek.api_key_file, deepseek.max_concurrency,
                          deepseek.concurrency_scope), ("bearer", {}, None, None, "call"))
        self.assertEqual(config.provider("anthropic").auth, "x-api-key")
        self.assertEqual(config.provider("nanogpt-anthropic").auth, "both")
        strata = config.provider("strata")
        self.assertEqual((strata.max_concurrency, strata.api_key_file, strata.timeout, strata.auth),
                         (1, "/etc/ai-stack/strata.env", 1800.0, "bearer"))
        self.assertIsNone(config.provider("ollama").api_key_env)
        for name in ("strata", "ollama", "llamacpp", "vllm", "lmstudio"):
            self.assertEqual(config.provider(name).concurrency_scope, "run")
        self.assertEqual(config.provider("anthropic").headers, {"anthropic-beta": "server-side-fallback-2026-07-01"})

    def test_every_default_provider_builds(self) -> None:
        config = Config.load(self.path)
        for name in DEFAULTS["providers"]:
            with self.subTest(provider=name):
                self.assertEqual(config.provider(name).name, name)

    def test_explicit_auth_headers_and_concurrency(self) -> None:
        config = self.write_config({"providers": {"gw": {
            "type": "anthropic", "base_url": "http://gw", "auth": "bearer", "headers": {"X-Org": 7},
            "max_concurrency": 3, "api_key_env": "", "concurrency_scope": "run"}}})
        gw = config.provider("gw")
        self.assertEqual((gw.auth, gw.headers, gw.max_concurrency, gw.api_key_env, gw.concurrency_scope),
                         ("bearer", {"X-Org": "7"}, 3, None, "run"))

    def test_invalid_providers(self) -> None:
        cases = {
            "unknown type": ({"type": "grpc", "base_url": "x"}, "type must be one of"),
            "no base_url": ({"type": "openai"}, "no base_url"),
            "bad auth": ({"type": "openai", "base_url": "x", "auth": "basic"}, "auth must be one of"),
            "zero concurrency": ({"type": "openai", "base_url": "x", "max_concurrency": 0}, "max_concurrency"),
            "bool concurrency": ({"type": "openai", "base_url": "x", "max_concurrency": True}, "max_concurrency"),
            "bad scope": ({"type": "openai", "base_url": "x", "concurrency_scope": "job"}, "concurrency_scope"),
        }
        for label, (entry, message) in cases.items():
            with self.subTest(label):
                config = self.write_config({"providers": {"p": entry}})
                with self.assertRaisesRegex(ValueError, message):
                    config.provider("p")
        with self.assertRaises(KeyError):
            Config.load(self.path).provider("missing")

    def test_dataclass_auth_default_and_validation(self) -> None:
        self.assertEqual(ProviderConfig("a", "anthropic", "u").auth, "x-api-key")
        self.assertEqual(ProviderConfig("o", "openai", "u").auth, "bearer")
        self.assertEqual(ProviderConfig("o", "openai", "u", auth="both").auth, "both")
        with self.assertRaises(ValueError):
            ProviderConfig("o", "openai", "u", auth="Bearer")
        self.assertEqual(ProviderConfig("o", "openai", "u").concurrency_scope, "call")
        with self.assertRaisesRegex(ValueError, "concurrency_scope"):
            ProviderConfig("o", "openai", "u", concurrency_scope="RUN")


class ApiKeyTest(ConfigTestCase):
    def key_file(self, text: str) -> Path:
        path = self.tmp / "keys.env"
        path.write_text(text)
        return path

    def test_env_var(self) -> None:
        provider = ProviderConfig("p", "openai", "u", api_key_env="TEST_KEY")
        self.assertIsNone(provider.api_key())
        with mock.patch.dict(os.environ, {"TEST_KEY": "sk-env"}):
            self.assertEqual(provider.api_key(), "sk-env")
        with mock.patch.dict(os.environ, {"TEST_KEY": ""}):
            self.assertIsNone(provider.api_key())

    def test_keyless_provider(self) -> None:
        path = self.key_file("TEST_KEY=abc\n")
        self.assertIsNone(ProviderConfig("p", "openai", "u", api_key_env=None, api_key_file=str(path)).api_key())

    def test_file_fallback_and_env_wins(self) -> None:
        path = self.key_file("# strata\nOTHER=1\nTEST_KEY=from-file\n")
        provider = ProviderConfig("p", "openai", "u", api_key_env="TEST_KEY", api_key_file=str(path))
        self.assertEqual(provider.api_key(), "from-file")
        with mock.patch.dict(os.environ, {"TEST_KEY": "from-env"}):
            self.assertEqual(provider.api_key(), "from-env")
        with mock.patch.dict(os.environ, {"TEST_KEY": ""}):
            self.assertEqual(provider.api_key(), "from-file")

    def test_file_is_read_at_call_time(self) -> None:
        path = self.key_file("TEST_KEY=one\n")
        provider = ProviderConfig("p", "openai", "u", api_key_env="TEST_KEY", api_key_file=str(path))
        self.assertEqual(provider.api_key(), "one")
        path.write_text("TEST_KEY=two\n")
        self.assertEqual(provider.api_key(), "two")

    def test_dotenv_syntax(self) -> None:
        cases = {
            'TEST_KEY="quoted value"\n': "quoted value",
            "TEST_KEY='single'\n": "single",
            "  export TEST_KEY = spaced  \n": "spaced",
            "TEST_KEY=abc # trailing comment\n": "abc",
            'TEST_KEY="a # b" # c\n': "a # b",
            "TEST_KEY=abc#def\n": "abc#def",
            "#TEST_KEY=commented\n": None,
            "TEST_KEY=\n": None,
            "TEST_KEY_2=nope\nXTEST_KEY=nope\n": None,
            "TEST_KEY=first\nTEST_KEY=second\n": "second",
            "garbage line\nTEST_KEY=ok\r\n": "ok",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(read_dotenv_value(self.key_file(text), "TEST_KEY"), expected)

    def test_unreadable_or_missing_file(self) -> None:
        self.assertIsNone(read_dotenv_value(self.tmp / "missing.env", "TEST_KEY"))
        self.assertIsNone(read_dotenv_value(self.tmp, "TEST_KEY"))
        binary = self.tmp / "bin.env"
        binary.write_bytes(b"\xff\xfe\x00TEST_KEY=x")
        self.assertIsNone(read_dotenv_value(binary, "TEST_KEY"))
        path = self.key_file("TEST_KEY=secret\n")
        with mock.patch.object(cfg, "read_small_text", side_effect=PermissionError("denied")):
            self.assertIsNone(read_dotenv_value(path, "TEST_KEY"))

    def test_fifo_key_file_does_not_block(self) -> None:
        fifo = self.tmp / "key.fifo"
        os.mkfifo(fifo)
        config = self.write_config({"providers": {"strata": {"api_key_file": str(fifo)}}})
        results: list = []
        worker = threading.Thread(target=lambda: results.append(config.public_view()), daemon=True)
        worker.start()
        worker.join(5)
        self.assertFalse(worker.is_alive(), "public_view blocked on a FIFO api_key_file")
        self.assertFalse(results[0]["providers"]["strata"]["key_present"])
        self.assertIsNone(read_dotenv_value(fifo, "TEST_KEY"))

    def test_device_and_oversized_key_files(self) -> None:
        self.assertIsNone(read_dotenv_value("/dev/zero", "TEST_KEY"))
        padding = "# " + "x" * (cfg.MAX_KEY_FILE_BYTES - 100) + "\n"
        self.assertEqual(read_dotenv_value(self.key_file(padding + "TEST_KEY=fits\n"), "TEST_KEY"), "fits")
        self.assertIsNone(read_dotenv_value(self.key_file(padding * 2 + "TEST_KEY=too-big\n"), "TEST_KEY"))

    def test_tilde_in_key_file(self) -> None:
        (self.home / "strata.env").write_text("TEST_KEY=home-key\n")
        self.assertEqual(resolve_api_key("TEST_KEY", "~/strata.env"), "home-key")

    def test_config_provider_reads_key_file(self) -> None:
        path = self.key_file("STRATA_API_KEY=strata-secret-key\n")
        config = self.write_config({"providers": {"strata": {"api_key_file": str(path)}}})
        self.assertEqual(config.provider("strata").api_key(), "strata-secret-key")
        self.assertEqual(config.model("local").provider.api_key(), "strata-secret-key")


class ModelTest(ConfigTestCase):
    def test_tier_key(self) -> None:
        model = Config.load(self.path).model("opus")
        self.assertIsInstance(model, ModelConfig)
        self.assertEqual((model.key, model.model, model.max_tokens, model.stream), ("opus", "deepseek-v4-pro", 32768, True))
        self.assertEqual(model.provider.name, "deepseek")
        self.assertEqual(model.extra_body, {"thinking": {"type": "enabled"}, "reasoning_effort": "high"})
        self.assertEqual((model.context_tokens, model.keep_reasoning), (None, True))

    def test_extra_body_is_a_copy(self) -> None:
        config = Config.load(self.path)
        config.model("opus").extra_body["thinking"]["type"] = "changed"
        self.assertEqual(config.raw["models"]["opus"]["extra_body"]["thinking"]["type"], "enabled")

    def test_local_model(self) -> None:
        model = Config.load(self.path).model("local")
        self.assertEqual((model.model, model.max_tokens, model.context_tokens, model.keep_reasoning, model.extra_body),
                         ("strata", 16384, 262144, False, {}))
        self.assertEqual((model.provider.name, model.provider.concurrency_scope), ("strata", "run"))
        self.assertEqual((model.allow_history_edits, model.eager_tools), (True, False))

    def test_allow_history_edits_defaults_from_provider_type(self) -> None:
        config = self.write_config({"models": {
            "claude": {"provider": "anthropic", "model": "claude-opus-5-5", "max_tokens": 64000},
            "claude-edits": {"provider": "anthropic", "model": "claude-opus-5-5", "allow_history_edits": True},
            "ds-anthropic": {"provider": "deepseek-anthropic", "model": "deepseek-v4-pro"},
            "eager": {"provider": "anthropic", "model": "claude-sonnet-5-5", "eager_tools": True},
            "frozen": {"provider": "deepseek", "model": "x", "allow_history_edits": False}}})
        self.assertEqual(config.model("opus").allow_history_edits, True)
        self.assertEqual(config.model("claude").allow_history_edits, False)
        self.assertEqual(config.model("claude-edits").allow_history_edits, True)
        self.assertEqual(config.model("ds-anthropic").allow_history_edits, False)
        self.assertEqual(config.model("frozen").allow_history_edits, False)
        self.assertEqual((config.model("eager").eager_tools, config.model("claude").eager_tools), (True, False))
        self.assertEqual(config.model("claude").provider.headers,
                         {"anthropic-beta": "server-side-fallback-2026-07-01"})

    def test_model_dataclass_derives_allow_history_edits(self) -> None:
        anthropic = ProviderConfig("a", "anthropic", "u")
        openai = ProviderConfig("o", "openai", "u")
        self.assertFalse(ModelConfig("k", anthropic, "m").allow_history_edits)
        self.assertTrue(ModelConfig("k", openai, "m").allow_history_edits)
        self.assertTrue(ModelConfig("k", anthropic, "m", allow_history_edits=True).allow_history_edits)
        self.assertFalse(ModelConfig("k", openai, "m").eager_tools)

    def test_literal_model_id_uses_opus_provider_and_settings(self) -> None:
        config = self.write_config({"models": {"opus": {"provider": "openrouter", "max_tokens": 1000,
                                                        "context_tokens": 64000}}})
        model = config.model("anthropic/claude-opus")
        self.assertEqual((model.key, model.model, model.max_tokens, model.context_tokens),
                         ("anthropic/claude-opus", "anthropic/claude-opus", 1000, 64000))
        self.assertEqual(model.provider.name, "openrouter")
        self.assertEqual(model.extra_body, DEFAULTS["models"]["opus"]["extra_body"])

    def test_custom_entry_like_section_18(self) -> None:
        config = self.write_config({"models": {"local-coder": {
            "provider": "ollama", "model": "qwen3-coder:30b", "max_tokens": 8192, "stream": True, "extra_body": {}}}})
        model = config.model("local-coder")
        self.assertEqual((model.model, model.provider.base_url, model.provider.api_key()),
                         ("qwen3-coder:30b", "http://127.0.0.1:11434/v1", None))

    def test_invalid_models(self) -> None:
        cases = {
            "no model": ({"m": {"provider": "deepseek"}}, "has no model"),
            "no provider": ({"m": {"model": "x"}}, "has no provider"),
            "context too small": ({"m": {"provider": "deepseek", "model": "x", "max_tokens": 4096,
                                         "context_tokens": 4096}}, "context_tokens"),
            "string bool": ({"m": {"provider": "deepseek", "model": "x", "keep_reasoning": "false"}},
                            "keep_reasoning must be true or false"),
            "history edits": ({"m": {"provider": "deepseek", "model": "x", "allow_history_edits": 0}},
                              "allow_history_edits must be true or false"),
            "eager tools": ({"m": {"provider": "deepseek", "model": "x", "eager_tools": "yes"}},
                            "eager_tools must be true or false"),
        }
        for label, (models, message) in cases.items():
            with self.subTest(label), self.assertRaisesRegex(ValueError, message):
                self.write_config({"models": models}).model("m")
        with self.assertRaises(KeyError):
            self.write_config({"models": {"m": {"provider": "nope", "model": "x"}}}).model("m")
        with self.assertRaises(ValueError):
            Config.load(self.path).model("")

    def test_literal_without_opus_entry(self) -> None:
        with self.assertRaises(KeyError):
            self.write_config({"models": {"opus": None}}).model("some-id")


class AgentResolutionTest(ConfigTestCase):
    def agent(self, name: str, model: str = "sonnet") -> SimpleNamespace:
        return SimpleNamespace(name=name, model=model)

    def test_agent_model(self) -> None:
        config = self.write_config({"agents": {"reviewer-ux": {"model": "local"}, "builder": {"driver": "qwen"}}})
        self.assertEqual(config.agent_model(self.agent("reviewer-ux")).key, "local")
        self.assertEqual(config.agent_model(self.agent("builder", "opus")).key, "opus")
        self.assertEqual(config.agent_model(self.agent("reviewer-ui")).key, "sonnet")
        self.assertEqual(config.agent_model(self.agent("x", "")).key, "opus")

    def test_star_model_overrides_file_tier_but_not_exact(self) -> None:
        config = self.write_config({"agents": {"*": {"model": "local"}, "approver": {"model": "opus"}}})
        self.assertEqual(config.agent_model(self.agent("builder", "opus")).key, "local")
        self.assertEqual(config.agent_model(self.agent("approver", "sonnet")).key, "opus")
        self.assertEqual(config.agent_driver("builder"), "builtin")

    def test_agent_model_literal_id(self) -> None:
        config = self.write_config({"agents": {"builder": {"model": "deepseek-reasoner"}}})
        model = config.agent_model(self.agent("builder"))
        self.assertEqual((model.model, model.provider.name), ("deepseek-reasoner", "deepseek"))

    def test_agent_driver(self) -> None:
        config = self.write_config({"agents": {"builder": {"driver": "claude"}, "approver": {"model": "opus"}}})
        self.assertEqual(config.agent_driver("builder"), "claude")
        self.assertEqual(config.agent_driver("approver"), "builtin")
        config = self.write_config({"agents": {"*": {"driver": "qwen"}}})
        self.assertEqual(config.agent_driver("anything"), "qwen")
        config = self.write_config({"agents": {"*": None}})
        self.assertEqual(config.agent_driver("anything"), "builtin")


class DriverTest(ConfigTestCase):
    def test_default_drivers(self) -> None:
        config = Config.load(self.path)
        self.assertEqual(config.driver("builtin").type, "builtin")
        qwen = config.driver("qwen")
        self.assertEqual((qwen.type, qwen.preset, qwen.command, qwen.env, qwen.models, qwen.timeout, qwen.profile),
                         ("cli", "qwen", "qwen", {}, {}, None, None))
        self.assertIsNone(qwen.run_args)

    def test_custom_driver(self) -> None:
        config = self.write_config({"drivers": {"codex": {
            "command": "~/.local/bin/codex-local", "profile": "strata", "timeout": 60,
            "run_args": ["exec", 1], "env": {"A": 1}, "models": {"opus": "local"}}}})
        codex = config.driver("codex")
        self.assertEqual(codex.command, str(self.home / ".local" / "bin" / "codex-local"))
        self.assertEqual((codex.profile, codex.timeout, codex.run_args, codex.env, codex.models),
                         ("strata", 60.0, ["exec", "1"], {"A": "1"}, {"opus": "local"}))

    def test_null_env_value_stays_none(self) -> None:
        """JSON null in a driver's env means "remove the variable" (the CLI driver): never the string "None"."""
        config = self.write_config({"drivers": {"claude": {"env": {
            "OPENAI_BASE_URL": None, "IS_SANDBOX": 1, "ANTHROPIC_AUTH_TOKEN": "${DEEPSEEK_API_KEY}"}}}})
        self.assertEqual(config.driver("claude").env,
                         {"OPENAI_BASE_URL": None, "IS_SANDBOX": "1", "ANTHROPIC_AUTH_TOKEN": "${DEEPSEEK_API_KEY}"})

    def test_command_defaults_to_preset(self) -> None:
        config = self.write_config({"drivers": {"q2": {"type": "cli", "preset": "qwen"}}})
        self.assertEqual(config.driver("q2").command, "qwen")

    def test_unknown_and_invalid(self) -> None:
        config = self.write_config({"drivers": {"bad": {"command": "x"}, "args": {"type": "cli", "run_args": "-p"}}})
        with self.assertRaises(KeyError):
            config.driver("nope")
        with self.assertRaisesRegex(ValueError, "no type"):
            config.driver("bad")
        with self.assertRaisesRegex(ValueError, "list of strings"):
            config.driver("args")


class SectionsTest(ConfigTestCase):
    def test_pipeline_and_limits_defaults(self) -> None:
        config = Config.load(self.path)
        self.assertEqual(config.pipeline(), cfg.PipelineConfig(["security", "ux", "ui", "code"], 2, 2, True, 1500))
        self.assertEqual(config.limits(), cfg.Limits(150, 5400.0, 600.0, 30000, 1500000))

    def test_overrides_null_fallback_and_clamping(self) -> None:
        config = self.write_config({
            "pipeline": {"reviewers": ["code", "code", "security"], "max_resumes": -1, "max_parallel_jobs": 0,
                         "auto_merge": False, "max_auto_merge_lines": None},
            "limits": {"max_turns": 5, "bash_timeout": None}})
        pipeline = config.pipeline()
        self.assertEqual(pipeline.reviewers, ["code", "security"])
        self.assertEqual((pipeline.max_resumes, pipeline.max_parallel_jobs, pipeline.auto_merge,
                          pipeline.max_auto_merge_lines), (0, 1, False, 1500))
        limits = config.limits()
        self.assertEqual((limits.max_turns, limits.bash_timeout), (5, 600.0))

    def test_invalid_pipeline(self) -> None:
        for pipeline in ({"reviewers": "code"}, {"reviewers": ["ok", "../x"]}, {"auto_merge": "false"}):
            with self.subTest(pipeline=pipeline), self.assertRaises(ValueError):
                self.write_config({"pipeline": pipeline}).pipeline()

    def test_merge_section_and_output_dir(self) -> None:
        config = self.write_config({"data_dir": str(self.tmp / "data")})
        self.assertEqual(config.merge(), cfg.MergeConfig(False, "origin", "merge", None))
        self.assertEqual(config.output_dir(), self.tmp / "data" / "output")
        config = self.write_config({"merge": {"delivery": "folder", "output_dir": "exports", "push": None}})
        self.assertEqual((config.merge().delivery, config.merge().push), ("folder", False))
        self.assertEqual(config.output_dir(), self.path.parent / "exports", "relative: next to the config")
        config = self.write_config({"merge": {"output_dir": "~/out"}})
        self.assertEqual(config.output_dir(), self.home / "out")
        config.validate()

    def test_invalid_merge_section(self) -> None:
        for merge in ({"delivery": "zip"}, {"delivery": 1}, {"remote": "-x"}, {"remote": ""}, {"push": "yes"},
                      {"output_dir": ""}, {"output_dir": 5}):
            with self.subTest(merge=merge):
                config = self.write_config({"merge": merge})
                with self.assertRaises(ValueError):
                    config.merge()
                with self.assertRaises(ValueError):
                    config.validate()

    def test_housekeeping_section(self) -> None:
        config = self.write_config({})
        self.assertEqual(config.housekeeping(), cfg.HousekeepingConfig())
        self.assertEqual((config.housekeeping().reinject_every_tool_turns, config.housekeeping().sweep),
                         (3, "regex"))
        config = self.write_config({"housekeeping": {"enabled": False, "stale_rounds": 4, "sweep": "off",
                                                     "followup_due_days": None}})
        hk = config.housekeeping()
        self.assertEqual((hk.enabled, hk.stale_rounds, hk.sweep, hk.followup_due_days), (False, 4, "off", 14))
        config.validate()
        for section in ({"sweep": "ai"}, {"enabled": "no"}, {"stale_rounds": -1}, {"max_proposals_per_run": True},
                        {"sweeper_model": 3}, {"cli_hooks": 1}):
            with self.subTest(section=section):
                config = self.write_config({"housekeeping": section})
                with self.assertRaises(ValueError):
                    config.housekeeping()
                with self.assertRaises(ValueError):
                    config.validate()

    def test_mcp_servers(self) -> None:
        config = self.write_config({"mcp_servers": {
            "rea": {"command": "~/.local/bin/rea-mcp", "args": ["--stdio"], "env": {"X": 1, "GONE": None},
                    "cwd": "~/w", "tools": ["search", "read"], "startup_timeout": 30, "timeout": 10,
                    "write_args": {"export": ["path", "index_path"]}},
            "plain": {"command": "srv"},
            "off": None}})
        servers = config.mcp_servers()
        self.assertEqual(sorted(servers), ["plain", "rea"])
        rea = servers["rea"]
        self.assertEqual((rea.command, rea.args, rea.env, rea.cwd),
                         (str(self.home / ".local/bin/rea-mcp"), ["--stdio"], {"X": "1", "GONE": None},
                          str(self.home / "w")))   # null stays None (removes the variable), not "None"
        self.assertEqual((rea.tools, rea.startup_timeout, rea.timeout), (["search", "read"], 30.0, 10.0))
        self.assertEqual(rea.write_args, {"export": ["path", "index_path"]})
        plain = servers["plain"]
        self.assertEqual((plain.args, plain.env, plain.cwd, plain.tools, plain.startup_timeout, plain.timeout,
                          plain.write_args), ([], {}, None, None, 60.0, cfg.DEFAULT_MCP_TIMEOUT, {}))
        self.assertEqual(config.mcp_servers()["rea"].tools, ["search", "read"])
        self.assertEqual(self.write_config({"mcp_servers": {"s": {"command": "c", "tools": []}}})
                         .mcp_servers()["s"].tools, [])

    def test_invalid_mcp_servers(self) -> None:
        for entry in ({"args": []}, "srv", {"command": "c", "tools": "search"},
                      {"command": "c", "write_args": ["path"]}, {"command": "c", "write_args": {"export": "path"}}):
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                self.write_config({"mcp_servers": {"s": entry}}).mcp_servers()

    def test_agent_dirs_skips_unreadable(self) -> None:
        repo = self.tmp / "repo"
        (repo / ".factory" / "agents").mkdir(parents=True)
        real_is_dir = Path.is_dir

        def is_dir(path: Path) -> bool:
            if ".factory" in path.parts:
                raise PermissionError(13, "Permission denied", str(path))
            return real_is_dir(path)

        with mock.patch.object(Path, "is_dir", autospec=True, side_effect=is_dir):
            self.assertEqual(agent_dirs(None, repo), [cfg.PROJECT_DIR / "agents"])

    def test_agent_dirs(self) -> None:
        repo = self.tmp / "repo"
        self.assertEqual(agent_dirs(None, repo), [cfg.PROJECT_DIR / "agents"])
        user = self.home / ".config" / "software-factory" / "agents"
        user.mkdir(parents=True)
        (repo / ".factory" / "agents").mkdir(parents=True)
        self.assertEqual(agent_dirs(Config.load(self.path), repo),
                         [cfg.PROJECT_DIR / "agents", user, repo / ".factory" / "agents"])
        self.assertEqual(agent_dirs(None, None), [cfg.PROJECT_DIR / "agents", user])
        self.assertTrue((cfg.PROJECT_DIR / "agents" / "builder.md").is_file())


class SecretsTest(ConfigTestCase):
    def test_secret_values(self) -> None:
        key_file = self.tmp / "strata.env"
        key_file.write_text("STRATA_API_KEY=strata-file-key\n")
        config = self.write_config({"server": {"token": "tok" * 11},
                                    "providers": {"strata": {"api_key_file": str(key_file)},
                                                  "dup": {"type": "openai", "base_url": "u",
                                                          "api_key_env": "DEEPSEEK_API_KEY"},
                                                  "gone": None}})
        env = {"DEEPSEEK_API_KEY": "sk-deepseek-123", "OPENAI_API_KEY": "", "ANTHROPIC_API_KEY": "short"}
        with mock.patch.dict(os.environ, env):
            values = config.secret_values()
        self.assertEqual(values, ["sk-deepseek-123", "strata-file-key", "tok" * 11])

    def test_secret_values_cover_masked_headers_and_env(self) -> None:
        config = self.write_config({
            "providers": {"gw": {"type": "openai", "base_url": "u", "api_key_env": None,
                                 "headers": {"Authorization": "Bearer sk-literal-secret-123456",
                                             "X-Api-Key": "gw-key-abcdefgh", "X-Title": "factory-title",
                                             "X-Auth-Short": "tiny", "X-Token": None}}},
            "drivers": {"claude": {"env": {"ANTHROPIC_API_KEY": "sk-ant-literal-999999",
                                           "ANTHROPIC_BASE_URL": "http://localhost:4000"}}},
            "mcp_servers": {"rea": {"command": "rea", "env": {"REA_TOKEN": "rea-token-1234", "MODE": "fast-mode"}}}})
        values = config.secret_values()
        self.assertEqual(values, ["sk-literal-secret-123456", "gw-key-abcdefgh", "sk-ant-literal-999999",
                                  "rea-token-1234"])
        view = json.dumps(config.public_view())
        self.assertFalse([value for value in values if value in view])

    def test_no_secrets(self) -> None:
        self.assertEqual(Config.load(self.path).secret_values(), [])

    def test_public_view(self) -> None:
        config = self.write_config({
            "server": {"token": "a" * 32},
            "providers": {"gw": {"type": "openai", "base_url": "u", "api_key_env": "TEST_KEY",
                                 "headers": {"Authorization": "Bearer x", "X-Title": "factory"}}},
            "drivers": {"claude": {"env": {"ANTHROPIC_AUTH_TOKEN": "sk", "ANTHROPIC_BASE_URL": "http://b"}}},
            "mcp_servers": {"rea": {"command": "rea", "env": {"REA_API_KEY": "k", "MODE": "fast"}}}})
        with mock.patch.dict(os.environ, {"TEST_KEY": "present"}):
            view = config.public_view()
        self.assertNotIn("token", view["server"])
        self.assertEqual(view["server"]["port"], 8765)
        self.assertTrue(view["providers"]["gw"]["key_present"])
        self.assertFalse(view["providers"]["deepseek"]["key_present"])
        self.assertFalse(view["providers"]["ollama"]["key_present"])
        self.assertEqual(view["providers"]["gw"]["headers"], {"Authorization": "***", "X-Title": "factory"})
        self.assertEqual(view["drivers"]["claude"]["env"], {"ANTHROPIC_AUTH_TOKEN": "***",
                                                            "ANTHROPIC_BASE_URL": "http://b"})
        self.assertEqual(view["mcp_servers"]["rea"]["env"], {"REA_API_KEY": "***", "MODE": "fast"})
        self.assertEqual(config.raw["server"]["token"], "a" * 32)
        self.assertNotIn("key_present", config.raw["providers"]["gw"])
        self.assertEqual(config.raw["drivers"]["claude"]["env"]["ANTHROPIC_AUTH_TOKEN"], "sk")
        self.assertNotIn("a" * 32, json.dumps(view))

    def test_public_view_key_from_file(self) -> None:
        key_file = self.tmp / "strata.env"
        key_file.write_text("STRATA_API_KEY=x\n")
        view = self.write_config({"providers": {"strata": {"api_key_file": str(key_file)}}}).public_view()
        self.assertTrue(view["providers"]["strata"]["key_present"])


class ValidateTest(ConfigTestCase):
    def test_defaults_are_valid(self) -> None:
        Config.load(self.path).validate()

    def test_invalid_settings(self) -> None:
        cases = {
            "agent driver": {"agents": {"builder": {"driver": "nope"}}},
            "agent entry": {"agents": {"builder": "qwen"}},
            "model provider": {"models": {"x": {"provider": "nope", "model": "m"}}},
            "provider": {"providers": {"p": {"type": "x"}}},
            "concurrency scope": {"providers": {"p": {"type": "openai", "base_url": "u",
                                                      "concurrency_scope": "always"}}},
            "model bool": {"models": {"m": {"provider": "deepseek", "model": "x", "eager_tools": 1}}},
            "repo name": {"repos": [{"name": "Bad Name", "path": "/x"}]},
            "repo dup": {"repos": [{"name": "a", "path": "/x"}, {"name": "a", "path": "/y"}]},
            "limits": {"limits": {"max_turns": "many"}},
            "limits type": {"limits": {"max_turns": [1]}},
            "escalate": {"escalate_paths": ["ok", 3]},
            "mcp": {"mcp_servers": {"s": {}}},
        }
        for label, data in cases.items():
            with self.subTest(label), self.assertRaises(ValueError):
                self.write_config(data).validate()


if __name__ == "__main__":
    unittest.main()
