from __future__ import annotations

import copy
import json
import os
import shutil
import socket
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from swfactory import settings
from swfactory.config import DEFAULTS, PROJECT_DIR, Config
from swfactory.harness.providers import provider_semaphore
from tests.helpers import clean_env, make_repo
from tests.mock_llm import MockLLM

TOKEN = "0123456789abcdef0123456789abcdef"
SECRET = "sk-probe-secret-0123456789"
TOOL_CALL = {"tool_calls": [{"name": "get_time", "arguments": {}}]}


def closed_port() -> int:
    """A local TCP port nothing listens on."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class SettingsTestCase(unittest.TestCase):
    """A temp HOME and a $FACTORY_CONFIG outside ~/.config, no provider keys in the environment.

    Agent overrides live in ~/.config/software-factory/agents wherever the config file is
    (DESIGN §7, §22; config.agent_dirs), so agents_dir is under the temp HOME.
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.config_path = self.tmp / "conf" / "config.json"
        env = clean_env(HOME=str(self.home), FACTORY_CONFIG=str(self.config_path))
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.agents_dir = self.home / ".config" / "software-factory" / "agents"

    def make_config(self, data: dict | None = None) -> Config:
        """Save data (plus a server token) as the config file and load it back."""
        data = copy.deepcopy(data or {})
        data.setdefault("server", {})["token"] = TOKEN
        Config(self.config_path, data).save()
        return Config.load(self.config_path)

    def builtin_text(self, name: str) -> str:
        return (PROJECT_DIR / "agents" / f"{name}.md").read_text(encoding="utf-8")


class ConfigViewTest(SettingsTestCase):
    def test_view_adds_presets_templates_agents_and_tiers(self) -> None:
        view = settings.config_view(self.make_config())
        self.assertNotIn("token", view["server"])
        self.assertEqual([entry["name"] for entry in view["presets"]], settings.list_presets())
        self.assertEqual(view["preset_names"], settings.list_presets())
        self.assertTrue(all(set(entry) == {"name", "description", "replaces", "merges", "default_profile"}
                            for entry in view["presets"]))
        self.assertEqual(len(view["model_templates"]), 8)
        for template in view["model_templates"]:
            self.assertTrue({"id", "label", "model_hints", "provider", "entry", "roles", "note"} <= set(template))
            self.assertTrue({"model", "max_tokens", "context_tokens", "extra_body"} <= set(template["entry"]))
        gpt_oss = next(t for t in view["model_templates"] if t["id"] == "gpt-oss-120b")
        self.assertEqual(gpt_oss["entry"]["extra_body"], {"chat_template_kwargs": {"reasoning_effort": "high"}})
        self.assertEqual(gpt_oss["entry"]["context_tokens"], 131072)
        self.assertEqual(set(view["tiers"]), {"opus", "sonnet", "haiku"})
        self.assertEqual(view["tiers"]["opus"], {"model": "deepseek-v4-pro", "provider": "deepseek", "same_as": None})
        self.assertEqual(view["agents"], {"*": {"driver": "builtin"}}, "agents stays the routing dict")
        self.assertNotIn("agents_error", view)
        agents = {agent["name"]: agent for agent in view["agent_list"]}
        self.assertTrue({"spec-writer", "builder", "approver", "lead", "coder", "repo-scout"} <= set(agents))
        self.assertEqual(agents["builder"], {"name": "builder", "description": agents["builder"]["description"],
                                             "model": "opus", "driver": "builtin", "source": "builtin",
                                             "tier": "opus", "color": "green", "extra_body": None})
        self.assertIn("key_present", view["providers"]["deepseek"])

    def test_view_shows_routes_overrides_and_tier_copies(self) -> None:
        config = self.make_config({"agents": {"reviewer-code": {"model": "local", "driver": "qwen",
                                                                "extra_body": {"x": 1}}}})
        settings.save_agent(config, "builder", self.builtin_text("builder") + "\nExtra line.\n")
        config = settings.apply_preset(config, "strata")
        view = settings.config_view(config)
        agents = {agent["name"]: agent for agent in view["agent_list"]}
        self.assertEqual(agents["builder"]["source"], "user")
        self.assertEqual(agents["builder"]["model"], "local")
        self.assertEqual(view["tiers"]["opus"], {"model": "strata", "provider": "strata", "same_as": "local"})
        self.assertEqual(view["agents"]["builder"], {"model": "local"})
        self.assertEqual(view["agents"]["*"], {"driver": "builtin"})

    def test_gui_round_trip_keeps_every_section_as_it_is(self) -> None:
        """The GUI edits view[section] in place and posts it back; unchanged sections must be no-ops."""
        repo = make_repo(self.tmp, "app")
        config = self.make_config({"repos": [{"name": "app", "path": str(repo)}],
                                   "providers": {"gw": {"type": "openai", "base_url": "https://gw.example/v1",
                                                        "headers": {"x-api-key": SECRET}}},
                                   "mcp_servers": {"rea": {"command": "rea", "env": {"REA_TOKEN": SECRET}}}})
        config = settings.apply_preset(config, "hybrid")
        view = settings.config_view(config)
        self.assertEqual(view["agents"], config.public_view()["agents"])
        self.assertIsInstance(view["agents"], dict)
        self.assertIsInstance(view["agent_list"], list)
        posted = json.loads(json.dumps({section: view[section] for section in settings.UPDATABLE_SECTIONS}))
        updated = settings.update_config(config, posted)
        self.assertEqual(updated.raw, config.raw)
        self.assertEqual(settings.config_view(updated)["agents"], view["agents"])

    def test_route_model_lookup_order(self) -> None:
        config = self.make_config({"agents": {"*": {"driver": "builtin", "model": "sonnet"},
                                              "builder": {"model": "local"}}})
        self.assertEqual(settings.route_model(config, "builder", "opus"), "local")
        self.assertEqual(settings.route_model(config, "approver", "opus"), "sonnet")
        bare = self.make_config()
        self.assertEqual(settings.route_model(bare, "approver", "opus"), "opus")
        self.assertEqual(settings.route_model(bare, "approver", ""), "opus")

    def test_broken_agent_file_is_reported_not_raised(self) -> None:
        self.agents_dir.mkdir(parents=True)
        (self.agents_dir / "broken.md").write_text("no frontmatter\n")
        view = settings.config_view(self.make_config())
        self.assertEqual(view["agent_list"], [])
        self.assertEqual(view["agents"], {"*": {"driver": "builtin"}})
        self.assertIn("broken.md", view["agents_error"])


class UpdateConfigTest(SettingsTestCase):
    def assertRejected(self, config: Config, patch: dict, field: str) -> None:
        before = self.config_path.read_bytes()
        raw = copy.deepcopy(config.raw)
        with self.assertRaises(ValueError) as caught:
            settings.update_config(config, patch)
        self.assertIn(field, str(caught.exception))
        self.assertEqual(self.config_path.read_bytes(), before, "nothing may be saved on a rejected patch")
        self.assertEqual(config.raw, raw, "the old config must not change")

    def test_rejects_sections_outside_the_allowed_list(self) -> None:
        config = self.make_config()
        for key in ("server", "data_dir", "drivers", "bogus"):
            with self.subTest(key=key):
                self.assertRejected(config, {key: {}}, key)
        self.assertRejected(config, {"server": {"token": "stolen"}, "limits": {}}, "server")
        with self.assertRaisesRegex(ValueError, "JSON object"):
            settings.update_config(config, ["providers"])  # type: ignore[arg-type]

    def test_replace_writes_an_entry_whole_instead_of_merging_into_it(self) -> None:
        config = self.make_config()
        first = settings.update_config(config, {"models": {"copy": {"provider": "deepseek", "model": "m",
                                                                     "extra_body": {"top_k": 20, "min_p": 0.05}}}})
        merged = settings.update_config(first, {"models": {"copy": {"extra_body": {"temperature": 0.6}}}})
        self.assertEqual(merged.raw["models"]["copy"]["extra_body"], {"top_k": 20, "min_p": 0.05, "temperature": 0.6})
        whole = settings.update_config(merged, {"models": {"copy": {"provider": "deepseek", "model": "m",
                                                                     "extra_body": {"temperature": 0.6}}}},
                                       replace=[("models", "copy")])
        self.assertEqual(whole.raw["models"]["copy"], {"provider": "deepseek", "model": "m",
                                                       "extra_body": {"temperature": 0.6}})
        self.assertIn("opus", whole.raw["models"], "only the named entry is replaced")
        gone = settings.update_config(whole, {"models": {"copy": None}}, replace=[("models", "copy")])
        self.assertNotIn("copy", gone.raw["models"])

    def test_merge_section_sets_the_delivery_and_output_folder(self) -> None:
        config = self.make_config()
        updated = settings.update_config(config, {"merge": {"delivery": "folder", "output_dir": "~/exports"}})
        self.assertEqual(updated.merge().delivery, "folder")
        self.assertEqual(updated.output_dir(), Path(os.path.expanduser("~/exports")))
        self.assertEqual(updated.merge().remote, "origin", "other merge settings keep their defaults")
        self.assertEqual(json.loads(self.config_path.read_text())["merge"]["delivery"], "folder")
        reset = settings.update_config(updated, {"merge": {"output_dir": None, "delivery": "branch"}})
        self.assertEqual((reset.merge().delivery, reset.merge().output_dir), ("branch", None))
        for patch, field in (({"merge": {"delivery": "zip"}}, "merge.delivery"),
                             ({"merge": {"push": "yes"}}, "merge.push"),
                             ({"merge": {"remote": "--upload-pack=x"}}, "merge.remote"),
                             ({"merge": {"remote": "my remote"}}, "merge.remote"),
                             ({"merge": {"output_dir": ""}}, "merge.output_dir")):
            with self.subTest(patch=patch):
                self.assertRejected(reset, patch, field)

    def test_housekeeping_section(self) -> None:
        config = self.make_config()
        updated = settings.update_config(config, {"housekeeping": {"enabled": False, "reinject_every_tool_turns": 5}})
        hk = updated.housekeeping()
        self.assertEqual((hk.enabled, hk.reinject_every_tool_turns, hk.stop_guard_attempts), (False, 5, 2))
        self.assertEqual(json.loads(self.config_path.read_text())["housekeeping"],
                         {"enabled": False, "reinject_every_tool_turns": 5})
        self.assertIn("housekeeping", settings.config_view(updated))
        for patch, field in (({"housekeeping": {"sweep": "llm"}}, "housekeeping.sweep"),
                             ({"housekeeping": {"stop_guard_attempts": 50}}, "housekeeping.stop_guard_attempts"),
                             ({"housekeeping": {"reinject_max_chars": 10}}, "housekeeping.reinject_max_chars"),
                             ({"housekeeping": {"enabled": "off"}}, "housekeeping.enabled")):
            with self.subTest(patch=patch):
                self.assertRejected(updated, patch, field)

    def test_valid_patch_saves_atomically_and_returns_reloaded_config(self) -> None:
        config = self.make_config({"data_dir": str(self.tmp / "data")})
        patch = {"providers": {"tabby": {"type": "openai", "base_url": "http://127.0.0.1:5000/v1",
                                         "api_key_env": "TABBY_API_KEY", "auth": "both", "max_concurrency": 1,
                                         "concurrency_scope": "run"}},
                 "models": {"fast": {"provider": "tabby", "model": "flash", "max_tokens": 8192,
                                     "context_tokens": 262144, "extra_body": {}}},
                 "agents": {"builder": {"model": "fast"}}}
        updated = settings.update_config(config, patch)
        self.assertIsNot(updated, config)
        self.assertEqual(stat.S_IMODE(self.config_path.stat().st_mode), 0o600)
        self.assertEqual(updated.raw["server"]["token"], TOKEN)
        self.assertEqual(updated.data_dir, self.tmp / "data")
        self.assertEqual(updated.model("fast").provider.max_concurrency, 1)
        self.assertEqual(updated.raw["agents"]["builder"], {"model": "fast"})
        self.assertEqual(Config.load(self.config_path).raw, updated.raw)
        self.assertEqual([p.name for p in self.config_path.parent.iterdir() if p.name.endswith(".tmp")], [])

    def test_deep_merge_lists_replace_and_null_deletes(self) -> None:
        config = self.make_config()
        updated = settings.update_config(config, {
            "models": {"opus": {"max_tokens": 1000, "extra_body": {"reasoning_effort": None}}, "haiku": None},
            "pipeline": {"reviewers": ["security", "code"]},
            "providers": {"vllm": None},
        })
        opus = updated.model("opus")
        self.assertEqual((opus.model, opus.max_tokens, opus.provider.name), ("deepseek-v4-pro", 1000, "deepseek"))
        self.assertIsNone(updated.raw["models"]["opus"]["extra_body"].get("reasoning_effort"))
        self.assertEqual(opus.extra_body["thinking"], {"type": "enabled"})
        self.assertIsNone(updated.raw["models"].get("haiku"))
        self.assertIsNone(updated.raw["providers"].get("vllm"))
        self.assertEqual(updated.pipeline().reviewers, ["security", "code"])
        restored = settings.update_config(updated, {"escalate_paths": ["only/**"]})
        self.assertEqual(restored.raw["escalate_paths"], ["only/**"])
        restored = settings.update_config(restored, {"escalate_paths": None, "pipeline": {"reviewers": None}})
        self.assertEqual(restored.raw["escalate_paths"], DEFAULTS["escalate_paths"])
        self.assertEqual(restored.pipeline().reviewers, DEFAULTS["pipeline"]["reviewers"])

    def test_provider_validation(self) -> None:
        config = self.make_config()
        good = {"type": "openai", "base_url": "https://api.example.com/v1"}
        bad = [
            ({"type": "grpc"}, "providers.x.type"),
            ({"base_url": "ftp://example.com"}, "providers.x.base_url"),
            ({"base_url": "https://"}, "providers.x.base_url"),
            ({"base_url": "http://exa mple.com"}, "providers.x.base_url"),
            ({"base_url": 42}, "providers.x.base_url"),
            ({"auth": "basic"}, "providers.x.auth"),
            ({"concurrency_scope": "job"}, "providers.x.concurrency_scope"),
            ({"max_concurrency": 0}, "providers.x.max_concurrency"),
            ({"max_concurrency": -1}, "providers.x.max_concurrency"),
            ({"max_concurrency": True}, "providers.x.max_concurrency"),
            ({"max_concurrency": "2"}, "providers.x.max_concurrency"),
            ({"timeout": -5}, "providers.x.timeout"),
            ({"headers": {"x-trace": 1}}, "providers.x.headers"),
            ({"api_key_env": "BAD-NAME"}, "providers.x.api_key_env"),
            ({"api_key_env": 5}, "providers.x.api_key_env"),
        ]
        for change, field in bad:
            with self.subTest(change=change):
                self.assertRejected(config, {"providers": {"x": {**good, **change}}}, field)
        self.assertRejected(config, {"providers": {"x": "https://example.com"}}, "providers.x")
        self.assertRejected(config, {"providers": {"bad name": good}}, "providers")
        self.assertRejected(config, {"providers": []}, "providers")
        updated = settings.update_config(config, {"providers": {"x": {**good, "auth": "x-api-key",
                                                                      "max_concurrency": None, "timeout": 30,
                                                                      "concurrency_scope": "call"}}})
        self.assertEqual(updated.provider("x").auth, "x-api-key")

    def test_model_validation(self) -> None:
        config = self.make_config()
        good = {"provider": "deepseek", "model": "deepseek-v4-flash", "max_tokens": 4096}
        bad = [
            ({"provider": "nope"}, "models.m.provider"),
            ({"provider": None}, "models.m.provider"),
            ({"model": ""}, "models.m.model"),
            ({"max_tokens": 0}, "models.m.max_tokens"),
            ({"max_tokens": True}, "models.m.max_tokens"),
            ({"max_tokens": "4096"}, "models.m.max_tokens"),
            ({"context_tokens": 0}, "models.m.context_tokens"),
            ({"context_tokens": 1000}, "context_tokens must be larger"),
            ({"extra_body": []}, "models.m.extra_body"),
            ({"stream": "yes"}, "models.m.stream"),
            ({"eager_tools": 1}, "models.m.eager_tools"),
        ]
        for change, field in bad:
            with self.subTest(change=change):
                self.assertRejected(config, {"models": {"m": {**good, **change}}}, field)
        self.assertRejected(config, {"providers": {"deepseek": None}}, "deepseek")
        updated = settings.update_config(config, {"models": {"m": {**good, "context_tokens": 65536}}})
        self.assertEqual(updated.model("m").context_tokens, 65536)

    def test_agent_route_validation(self) -> None:
        config = self.make_config()
        self.assertRejected(config, {"agents": {"builder": {"model": "nope"}}}, "agents.builder.model")
        self.assertRejected(config, {"agents": {"builder": {"model": 7}}}, "agents.builder.model")
        self.assertRejected(config, {"agents": {"builder": {"driver": "nope"}}}, "agents.builder.driver")
        self.assertRejected(config, {"agents": {"builder": {"extra_body": "x"}}}, "agents.builder.extra_body")
        self.assertRejected(config, {"agents": {"builder": ["local"]}}, "agents.builder")
        self.assertRejected(config, {"agents": {"bad name!": {"model": "local"}}}, "agents")
        self.assertRejected(config, {"models": {"haiku": None}, "agents": {"approver": {"model": "haiku"}}},
                            "agents.approver.model")
        updated = settings.update_config(config, {"agents": {"builder": {"model": "local", "driver": "qwen",
                                                                         "extra_body": {"a": 1}}}})
        self.assertEqual(updated.agent_driver("builder"), "qwen")
        updated = settings.update_config(updated, {"agents": {"builder": {"model": None}}})
        self.assertEqual(updated.raw["agents"]["builder"], {"driver": "qwen", "extra_body": {"a": 1}})

    def test_pipeline_validation(self) -> None:
        config = self.make_config()
        for reviewers in ([], "security", ["Sec"], ["security", "security"], ["perf"], [1]):
            with self.subTest(reviewers=reviewers):
                self.assertRejected(config, {"pipeline": {"reviewers": reviewers}}, "pipeline.reviewers")
        for key, value in (("max_resumes", 11), ("max_resumes", -1), ("max_resumes", 1.5),
                           ("max_parallel_jobs", 0), ("max_parallel_jobs", 17), ("auto_merge", "no"),
                           ("max_auto_merge_lines", -1)):
            with self.subTest(key=key, value=value):
                self.assertRejected(config, {"pipeline": {key: value}}, f"pipeline.{key}")
        settings.new_agent(config, "reviewer-perf")
        updated = settings.update_config(config, {"pipeline": {"reviewers": ["security", "perf"], "max_resumes": 10,
                                                               "max_parallel_jobs": 16}})
        self.assertEqual(updated.pipeline().reviewers, ["security", "perf"])
        updated = settings.update_config(updated, {"pipeline": {"max_resumes": 0, "max_parallel_jobs": 1}})
        self.assertEqual((updated.pipeline().max_resumes, updated.pipeline().max_parallel_jobs), (0, 1))

    def test_limits_harness_mcp_and_escalate_paths(self) -> None:
        config = self.make_config()
        self.assertRejected(config, {"limits": {"max_turns": 0}}, "limits.max_turns")
        self.assertRejected(config, {"limits": {"bash_timeout": -5}}, "limits.bash_timeout")
        self.assertRejected(config, {"limits": {"tool_output_chars": "big"}}, "limits.tool_output_chars")
        self.assertRejected(config, {"harness": {"sandbox": "docker"}}, "harness.sandbox")
        self.assertRejected(config, {"harness": {"sandbox_network": "yes"}}, "harness.sandbox_network")
        self.assertRejected(config, {"harness": {"skills_dirs": [1]}}, "harness.skills_dirs")
        self.assertRejected(config, {"mcp_servers": {"rea": {"args": []}}}, "mcp_servers.rea.command")
        self.assertRejected(config, {"mcp_servers": {"rea": {"command": "rea", "args": "x"}}}, "mcp_servers.rea.args")
        self.assertRejected(config, {"mcp_servers": {"rea": {"command": "rea", "env": {"A": 1}}}},
                            "mcp_servers.rea.env")
        self.assertRejected(config, {"mcp_servers": {"bad.name": {"command": "rea"}}}, "mcp_servers")
        for write_args in (["path"], {"export": "path"}, {"export": [1]}):
            self.assertRejected(config, {"mcp_servers": {"rea": {"command": "rea", "write_args": write_args}}},
                                "mcp_servers.rea.write_args")
        self.assertRejected(config, {"escalate_paths": "x/**"}, "escalate_paths")
        self.assertRejected(config, {"escalate_paths": [1]}, "escalate_paths")
        updated = settings.update_config(config, {
            "limits": {"max_turns": 10, "bash_timeout": 30.5}, "harness": {"sandbox": "bwrap", "skills_dirs": ["/s"]},
            "mcp_servers": {"rea": {"command": "rea-mcp", "tools": ["search"], "timeout": 60,
                                    "write_args": {"export": ["path"]}}},
            "escalate_paths": ["infra/**"]})
        self.assertEqual(updated.limits().max_turns, 10)
        self.assertEqual(updated.mcp_servers()["rea"].tools, ["search"])
        self.assertEqual(updated.mcp_servers()["rea"].write_args, {"export": ["path"]})
        self.assertEqual(updated.raw["escalate_paths"], ["infra/**"])

    def test_repos_validated_like_add_repo(self) -> None:
        config = self.make_config()
        repo = make_repo(self.tmp, "app")
        (repo / "sub").mkdir()
        (self.tmp / "plain").mkdir()
        bad = [
            ([{"name": "App", "path": str(repo)}], "repos[0].name"),
            ([{"name": "app"}], "repos.app.path"),
            ([{"name": "app", "path": str(self.tmp / "missing")}], "not a directory"),
            ([{"name": "app", "path": str(self.tmp / "plain")}], "not a git repository"),
            ([{"name": "app", "path": str(repo / "sub")}], "not the top level"),
            ([{"name": "app", "path": str(repo), "base": "nope"}], "repos.app.base"),
            ([{"name": "app", "path": str(repo), "base": "-x"}], "repos.app.base"),
            ([{"name": "app", "path": str(repo), "escalate_paths": "x"}], "repos.app.escalate_paths"),
            ([{"name": "app", "path": str(repo)}, {"name": "app", "path": str(repo)}], "listed twice"),
            ([{"name": "app", "path": str(repo)}, {"name": "app2", "path": str(repo) + "/"}], "already configured"),
            ({"name": "app"}, "repos must be a list"),
            (["app"], "repos[0]"),
        ]
        for repos, field in bad:
            with self.subTest(repos=repos):
                self.assertRejected(config, {"repos": repos}, field)
        updated = settings.update_config(config, {"repos": [{"name": "app", "path": str(repo / "sub" / ".."),
                                                             "base": "main", "setup": "make deps"}]})
        self.assertEqual(updated.repo("app").path, repo)
        self.assertEqual(updated.repo("app").setup, "make deps")
        shutil.rmtree(repo)
        still = settings.update_config(updated, {"limits": {"max_turns": 99}})
        self.assertEqual(still.repo("app").path, repo, "an unchanged entry is not re-checked")
        self.assertEqual(settings.update_config(still, {"repos": []}).repos(), [])

    def test_masked_view_values_keep_the_real_secrets(self) -> None:
        config = self.make_config({"providers": {"gw": {"type": "openai", "base_url": "https://gw.example/v1",
                                                        "headers": {"x-api-key": SECRET, "x-org": "acme"}}},
                                   "mcp_servers": {"rea": {"command": "rea", "env": {"REA_TOKEN": SECRET}}}})
        view = settings.config_view(config)
        self.assertEqual(view["providers"]["gw"]["headers"]["x-api-key"], "***")
        posted = {"providers": {"gw": dict(view["providers"]["gw"], headers={"x-api-key": "***", "x-org": "beta"})},
                  "mcp_servers": view["mcp_servers"]}
        updated = settings.update_config(config, posted)
        self.assertEqual(updated.provider("gw").headers, {"x-api-key": SECRET, "x-org": "beta"})
        self.assertEqual(updated.mcp_servers()["rea"].env, {"REA_TOKEN": SECRET})
        self.assertNotIn("key_present", updated.raw["providers"]["gw"])

    def test_concurrent_updates_leave_a_valid_file(self) -> None:
        config = self.make_config()
        errors: list[BaseException] = []

        def update(turns: int) -> None:
            try:
                settings.update_config(config, {"limits": {"max_turns": turns}})
            except BaseException as exc:  # collected for the assertion below
                errors.append(exc)

        threads = [threading.Thread(target=update, args=(n,)) for n in range(10, 18)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(errors, [])
        self.assertIn(Config.load(self.config_path).limits().max_turns, range(10, 18))
        self.assertEqual([p.name for p in self.config_path.parent.iterdir() if p.name.endswith(".tmp")], [])

    def test_updates_from_a_stale_config_keep_earlier_saves(self) -> None:
        """Two requests that both start from the same (now stale) Factory.config must both land."""
        repo = make_repo(self.tmp, "app")
        stale = self.make_config()
        settings.update_config(stale, {"limits": {"max_turns": 77}})
        settings.update_config(stale, {"repos": [{"name": "app", "path": str(repo)}]})
        settings.apply_preset(stale, "strata")
        final = settings.update_config(stale, {"pipeline": {"max_resumes": 5}})
        for config in (final, Config.load(self.config_path)):
            self.assertEqual(config.limits().max_turns, 77)
            self.assertEqual([r.name for r in config.repos()], ["app"])
            self.assertEqual(config.model("local").provider.name, "strata")
            self.assertEqual(config.pipeline().max_resumes, 5)
            self.assertEqual(config.raw["server"]["token"], TOKEN)
        self.assertEqual(stale.limits().max_turns, 150, "the caller's config is never changed in place")

    def test_concurrent_updates_from_one_stale_config_all_land(self) -> None:
        stale = self.make_config()
        changes = [{"limits": {"max_turns": 77}}, {"pipeline": {"max_resumes": 5}},
                   {"harness": {"sandbox_network": False}}, {"escalate_paths": ["infra/**"]}]
        for round_ in range(5):
            barrier = threading.Barrier(len(changes) + 1)
            errors: list[BaseException] = []

            preset = "hybrid" if round_ % 2 else "strata"

            def run(change: dict | None, barrier: threading.Barrier = barrier, preset: str = preset,
                    errors: list = errors) -> None:
                try:
                    barrier.wait(5)
                    if change is None:
                        settings.apply_preset(stale, preset)
                    else:
                        settings.update_config(stale, change)
                except BaseException as exc:  # collected for the assertion below
                    errors.append(exc)

            threads = [threading.Thread(target=run, args=(change,)) for change in [*changes, None]]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(20)
            self.assertEqual(errors, [])
            disk = Config.load(self.config_path)
            with self.subTest(round=round_):
                self.assertEqual(disk.limits().max_turns, 77)
                self.assertEqual(disk.pipeline().max_resumes, 5)
                self.assertFalse(disk.raw["harness"]["sandbox_network"])
                self.assertEqual(disk.raw["escalate_paths"], ["infra/**"])
                self.assertEqual(disk.model("local").provider.name, "tabby" if round_ % 2 else "strata")
            settings.update_config(stale, {"limits": None, "pipeline": None, "harness": None,
                                           "escalate_paths": None})

    def test_on_saved_runs_under_the_save_lock_in_save_order(self) -> None:
        """A caller that swaps the result in from on_saved always ends with the newest config."""
        stale = self.make_config()
        installed: list[Config] = []
        current: dict[str, Config] = {}

        def swap(new: Config) -> None:
            time.sleep(0.01)  # widen the window a racing save would need
            installed.append(new)
            current["config"] = new

        threads = [threading.Thread(target=settings.update_config,
                                    args=(stale, {"limits": {"max_turns": 100 + n}}), kwargs={"on_saved": swap})
                   for n in range(6)]
        threads.append(threading.Thread(target=settings.apply_preset, args=(stale, "strata"),
                                        kwargs={"on_saved": swap}))
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        self.assertEqual(len(installed), 7)
        self.assertEqual(current["config"].raw, Config.load(self.config_path).raw,
                         "the last config installed is the one on disk")
        self.assertEqual(current["config"].model("local").provider.name, "strata")
        before = self.config_path.read_bytes()
        with self.assertRaises(ValueError):
            settings.update_config(stale, {"limits": {"max_turns": 0}}, on_saved=swap)
        self.assertEqual((len(installed), self.config_path.read_bytes()), (7, before), "no swap without a save")

    def test_an_unsaved_config_is_the_base_until_a_file_exists(self) -> None:
        config = Config(self.config_path, {"server": {"token": TOKEN}, "limits": {"max_turns": 9}})
        self.assertFalse(self.config_path.exists())
        updated = settings.update_config(config, {"pipeline": {"max_resumes": 1}})
        self.assertEqual((updated.limits().max_turns, updated.pipeline().max_resumes), (9, 1))
        self.assertEqual(updated.raw["server"]["token"], TOKEN)

    def test_a_broken_config_file_is_reported_and_left_alone(self) -> None:
        config = self.make_config()
        self.config_path.write_text("{ not json")
        with self.assertRaisesRegex(ValueError, "invalid JSON"):
            settings.update_config(config, {"limits": {"max_turns": 9}})
        with self.assertRaisesRegex(ValueError, "invalid JSON"):
            settings.apply_preset(config, "strata")
        self.assertEqual(self.config_path.read_text(), "{ not json")
        self.config_path.write_text("[]")
        with self.assertRaisesRegex(ValueError, "top level must be a JSON object"):
            settings.update_config(config, {"limits": {"max_turns": 9}})
        self.assertEqual(self.config_path.read_text(), "[]")


class TestModelTest(SettingsTestCase):
    def mock_config(self, mock_llm: MockLLM, type_: str = "openai", name: str = "mock", **model: object) -> Config:
        provider = {"type": type_, "base_url": mock_llm.base_url, "api_key_env": None}
        provider.update(model.pop("provider", {}))  # type: ignore[arg-type]
        entry = {"provider": name, "model": "mock-model", "max_tokens": 4096, "stream": True, "extra_body": {}}
        entry.update(model)
        return Config(self.config_path, {"providers": {name: provider}, "models": {"m": entry}})

    def test_plain_and_tool_probe(self) -> None:
        with MockLLM([{"text": "OK"}, TOOL_CALL]) as llm:
            result = settings.test_model(self.mock_config(llm), "m")
        self.assertEqual(set(result), {"ok", "latency_ms", "text", "error", "tool_call", "tokens_per_second"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "OK")
        self.assertTrue(result["tool_call"])
        self.assertIsNone(result["error"])
        self.assertIsInstance(result["latency_ms"], int)
        plain, tool = llm.requests
        self.assertEqual(plain["messages"][-1]["content"], "Reply with OK")
        self.assertEqual(plain["max_tokens"], 16)
        self.assertNotIn("tools", plain)
        self.assertEqual([t["function"]["name"] for t in tool["tools"]], ["get_time"])
        self.assertEqual(tool["tools"][0]["function"]["parameters"], {"type": "object", "properties": {}})
        self.assertIn("get_time", tool["messages"][-1]["content"])

    def test_text_instead_of_a_tool_call(self) -> None:
        with MockLLM([{"text": "OK"}, {"text": "It is noon."}]) as llm:
            result = settings.test_model(self.mock_config(llm), "m")
        self.assertTrue(result["ok"])
        self.assertFalse(result["tool_call"])
        self.assertIsNone(result["error"])

    def test_anthropic_and_reasoning_models_get_room(self) -> None:
        cases = [("anthropic", {"max_tokens": 64000}, 2048), ("anthropic", {"max_tokens": 1500}, 1500),
                 ("openai", {"extra_body": {"reasoning_effort": "high"}}, 2048),
                 ("openai", {"extra_body": {"thinking": {"type": "enabled"}}, "max_tokens": 1024}, 1024),
                 ("openai", {"extra_body": {"chat_template_kwargs": {"reasoning_effort": "high"}}}, 2048)]
        for type_, model, expected in cases:
            with self.subTest(type=type_, model=model):
                with MockLLM([{"text": "OK", "reasoning": "think"}, TOOL_CALL]) as llm:
                    result = settings.test_model(self.mock_config(llm, type_, **model), "m")
                self.assertTrue(result["ok"], result)
                self.assertTrue(result["tool_call"], result)
                self.assertEqual([r["max_tokens"] for r in llm.requests], [expected, expected])
                self.assertGreaterEqual(expected, 1024)

    def test_disabled_thinking_is_not_a_reasoning_model(self) -> None:
        with MockLLM([{"text": "OK"}, TOOL_CALL]) as llm:
            settings.test_model(self.mock_config(llm, extra_body={"thinking": {"type": "disabled"}}), "m")
        self.assertEqual([r["max_tokens"] for r in llm.requests], [16, 256])

    def test_reply_cut_off_while_thinking_is_retried_with_room(self) -> None:
        script = [{"reasoning": "let me think about this", "finish": "length"}, {"text": "OK"},
                  {"reasoning": "hmm", "finish": "length"}, TOOL_CALL]
        with MockLLM(script) as llm:
            result = settings.test_model(self.mock_config(llm), "m")
        self.assertEqual((result["ok"], result["text"], result["tool_call"]), (True, "OK", True))
        self.assertEqual([r["max_tokens"] for r in llm.requests], [16, 2048, 256, 2048])

    def test_plain_failure_skips_the_tool_probe(self) -> None:
        with MockLLM([], error_sequence=[401]) as llm:
            result = settings.test_model(self.mock_config(llm), "m")
        self.assertFalse(result["ok"])
        self.assertIn("401", result["error"])
        self.assertFalse(result["tool_call"])
        self.assertEqual(len(llm.requests), 1)

    def test_tool_probe_failure_is_reported(self) -> None:
        with MockLLM([{"text": "OK"}]) as llm:  # the second request finds the script exhausted: HTTP 400
            result = settings.test_model(self.mock_config(llm), "m")
        self.assertTrue(result["ok"])
        self.assertFalse(result["tool_call"])
        self.assertTrue(result["error"].startswith("tool probe: "), result["error"])
        self.assertIn("400", result["error"])

    def test_unknown_model_and_broken_entries_never_raise(self) -> None:
        with MockLLM([]) as llm:
            config = self.mock_config(llm)
            self.assertEqual(settings.test_model(config, "nope")["error"], "unknown model 'nope'")
            config.raw["models"]["broken"] = {"provider": "missing", "model": "x"}
            result = settings.test_model(config, "broken")
        self.assertFalse(result["ok"])
        self.assertIn("missing", result["error"])
        self.assertEqual(llm.requests, [])

    def test_missing_key_is_reported_without_a_request(self) -> None:
        with MockLLM([{"text": "OK"}]) as llm:
            result = settings.test_model(self.mock_config(llm, provider={"api_key_env": "PROBE_TEST_KEY"}), "m")
        self.assertFalse(result["ok"])
        self.assertIn("PROBE_TEST_KEY is not set", result["error"])
        self.assertEqual(llm.requests, [])

    def test_key_is_sent_and_redacted_from_errors(self) -> None:
        os.environ["PROBE_TEST_KEY"] = SECRET
        reply = {"status": 401, "body": f"invalid key {SECRET}"}
        with MockLLM([], error_sequence=[reply]) as llm:
            result = settings.test_model(self.mock_config(llm, provider={"api_key_env": "PROBE_TEST_KEY"}), "m")
        self.assertEqual(llm.requests[0].headers["Authorization"], f"Bearer {SECRET}")
        self.assertIn("401", result["error"])
        self.assertNotIn(SECRET, json.dumps(result))

    def test_unreachable_server_fails_fast(self) -> None:
        config = Config(self.config_path, {
            "providers": {"down": {"type": "openai", "base_url": f"http://127.0.0.1:{closed_port()}/v1",
                                   "api_key_env": None}},
            "models": {"m": {"provider": "down", "model": "x", "max_tokens": 100}}})
        started = time.monotonic()
        result = settings.test_model(config, "m")
        self.assertLess(time.monotonic() - started, 5)
        self.assertFalse(result["ok"])
        self.assertIn("cannot reach", result["error"])
        self.assertIn("start your local server", result["error"])

    def test_non_http_base_url_is_never_opened(self) -> None:
        config = Config(self.config_path, {
            "providers": {"odd": {"type": "openai", "base_url": f"file://{self.tmp}", "api_key_env": None}},
            "models": {"m": {"provider": "odd", "model": "x", "max_tokens": 100}}})
        (self.tmp / "models").write_text("{}")
        self.assertEqual(settings.reach(config.provider("odd")),
                         (None, f"cannot reach file://{self.tmp}/models: base_url must be an http(s) URL"))
        self.assertIn("must be an http(s) URL", settings.test_model(config, "m")["error"])

    def test_timeout_abandons_a_slow_reply(self) -> None:
        with MockLLM([{"text": "OK", "delay": 3}]) as llm:
            started = time.monotonic()
            result = settings.test_model(self.mock_config(llm), "m", timeout=0.5)
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.9)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "no reply within 0.5s")

    def test_cancel_stops_the_probe(self) -> None:
        cancel = threading.Event()
        timer = threading.Timer(0.3, cancel.set)
        with MockLLM([{"text": "OK", "delay": 3}]) as llm:
            timer.start()
            started = time.monotonic()
            result = settings.test_model(self.mock_config(llm), "m", cancel=cancel)
            elapsed = time.monotonic() - started
        timer.cancel()
        self.assertLess(elapsed, 2.9)
        self.assertEqual(result["error"], "cancelled")

    def test_busy_provider_slot_is_explained(self) -> None:
        name = "busy-slot-provider"
        slots = provider_semaphore(name, 1)
        self.assertIsNotNone(slots)
        holder_ready, release = threading.Event(), threading.Event()

        def hold() -> None:
            with slots:
                holder_ready.set()
                release.wait(10)

        holder = threading.Thread(target=hold)
        holder.start()
        self.addCleanup(holder.join, 10)
        self.addCleanup(release.set)
        holder_ready.wait(5)
        with MockLLM([{"text": "OK"}]) as llm:
            result = settings.test_model(self.mock_config(llm, name=name, provider={"max_concurrency": 1}), "m",
                                         timeout=0.4)
        self.assertFalse(result["ok"])
        self.assertIn("no free slot on provider busy-slot-provider", result["error"])
        self.assertEqual([r.path for r in llm.requests], [])

    def test_waiting_for_a_slot_does_not_use_up_the_reply_time(self) -> None:
        name = "briefly-busy-provider"
        slots = provider_semaphore(name, 1)
        self.assertIsNotNone(slots)
        holding = threading.Event()

        def hold() -> None:
            with slots:
                holding.set()
                time.sleep(0.6)

        holder = threading.Thread(target=hold)
        holder.start()
        self.addCleanup(holder.join, 10)
        holding.wait(5)
        with MockLLM([{"text": "OK", "delay": 0.6}, TOOL_CALL]) as llm:
            result = settings.test_model(self.mock_config(llm, name=name, provider={"max_concurrency": 1}), "m",
                                         timeout=1.0)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["tool_call"], result)
        self.assertGreaterEqual(settings.probe_budget(1.0), 4 * 2 * 1.0)

    def test_tokens_per_second_needs_a_measurable_reply(self) -> None:
        long_reply = {"text": "x" * 120, "chunk_delay": 0.03,
                      "usage": {"prompt_tokens": 10, "completion_tokens": 300, "total_tokens": 310}}
        with MockLLM([long_reply, TOOL_CALL]) as llm:
            result = settings.test_model(self.mock_config(llm), "m")
        self.assertIsInstance(result["tokens_per_second"], float)
        self.assertGreater(result["tokens_per_second"], 0)
        with MockLLM([{"text": "OK"}, TOOL_CALL]) as llm:
            self.assertIsNone(settings.test_model(self.mock_config(llm), "m")["tokens_per_second"])

    def test_non_streaming_model(self) -> None:
        with MockLLM([{"text": "OK"}, TOOL_CALL]) as llm:
            result = settings.test_model(self.mock_config(llm, stream=False), "m")
        self.assertEqual((result["ok"], result["tool_call"]), (True, True))
        self.assertFalse(llm.requests[0]["stream"])


class AgentOverrideTest(SettingsTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.config = self.make_config()
        self.shipped = {p.name: p.read_bytes() for p in (PROJECT_DIR / "agents").glob("*.md")}
        self.addCleanup(self.assertShippedAgentsUntouched)

    def assertShippedAgentsUntouched(self) -> None:
        self.assertEqual({p.name: p.read_bytes() for p in (PROJECT_DIR / "agents").glob("*.md")}, self.shipped)

    def test_save_agent_writes_the_user_override(self) -> None:
        text = self.builtin_text("builder").replace("You build one factory job.", "You build carefully.")
        detail = settings.save_agent(self.config, "builder", text)
        path = self.agents_dir / "builder.md"
        self.assertEqual(path.read_text(encoding="utf-8"), text)
        self.assertEqual(detail["source"], "user")
        self.assertEqual(detail["path"], str(path))
        self.assertEqual(detail["text"], text)
        self.assertIn("You build carefully.", detail["body"])
        self.assertEqual(set(detail), {"name", "description", "tools", "model", "color", "skills", "mcp", "body",
                                       "text", "source", "path"})
        self.assertEqual(settings.agent_detail(self.config, "builder")["source"], "user")
        self.assertEqual([p.name for p in self.agents_dir.iterdir()], ["builder.md"])

    def test_save_agent_rejects_invalid_names_and_files(self) -> None:
        good = self.builtin_text("builder")
        for name in ("Builder", "-x", "a" * 42, "../x", "x.y", "", None):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, "invalid agent name"):
                    settings.save_agent(self.config, name, good)  # type: ignore[arg-type]
        bad = [
            ("just a prompt\n", "missing frontmatter"),
            (good.replace("name: builder", "name: other"), "frontmatter name must be 'builder'"),
            (good.replace("name: builder\n", ""), "frontmatter name must be 'builder'"),
            ("---\nname: builder\n---\nBody.\n", "description"),
            ("---\nname: builder\ndescription: d\n---\n\n", "body"),
            ("---\nname: builder\ndescription: d\n", "not closed"),
            ("x" * (2 << 20), "larger than"),
        ]
        for text, message in bad:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    settings.save_agent(self.config, "builder", text)
        with self.assertRaisesRegex(ValueError, "string"):
            settings.save_agent(self.config, "builder", 42)  # type: ignore[arg-type]
        self.assertFalse(self.agents_dir.exists())

    def test_new_agent_copies_a_lens_with_the_name_and_review_file_replaced(self) -> None:
        detail = settings.new_agent(self.config, "reviewer-perf")
        self.assertEqual((detail["name"], detail["source"]), ("reviewer-perf", "user"))
        self.assertIn("review-perf.md", detail["body"])
        self.assertNotIn("review-code.md", detail["body"])
        source = settings.agent_detail(self.config, "reviewer-code")
        self.assertEqual((detail["tools"], detail["model"]), (source["tools"], source["model"]))
        self.assertTrue((self.agents_dir / "reviewer-perf.md").is_file())

    def test_new_agent_from_another_agent_and_errors(self) -> None:
        detail = settings.new_agent(self.config, "pair", from_="coder")
        self.assertEqual(detail["name"], "pair")
        self.assertEqual(detail["body"], settings.agent_detail(self.config, "coder")["body"])
        with self.assertRaisesRegex(ValueError, "already exists"):
            settings.new_agent(self.config, "builder")
        with self.assertRaisesRegex(ValueError, "already exists"):
            settings.new_agent(self.config, "pair")
        with self.assertRaisesRegex(ValueError, "unknown agent"):
            settings.new_agent(self.config, "other", from_="nope")
        with self.assertRaisesRegex(ValueError, "invalid agent name"):
            settings.new_agent(self.config, "Other")

    def test_new_agent_adds_a_missing_name_line(self) -> None:
        self.agents_dir.mkdir(parents=True)
        (self.agents_dir / "nameless.md").write_text("---\ndescription: From the file name.\n---\nBody.\n")
        detail = settings.new_agent(self.config, "named", from_="nameless")
        self.assertEqual(detail["name"], "named")
        self.assertTrue(detail["text"].startswith("---\nname: named\n"))

    def test_reset_agent(self) -> None:
        settings.save_agent(self.config, "builder", self.builtin_text("builder") + "\nMore.\n")
        restored = settings.reset_agent(self.config, "builder")
        self.assertEqual(restored["source"], "builtin")
        self.assertEqual(restored["text"], self.builtin_text("builder"))
        with self.assertRaises(KeyError):
            settings.reset_agent(self.config, "builder")
        settings.new_agent(self.config, "reviewer-perf")
        self.assertIsNone(settings.reset_agent(self.config, "reviewer-perf"))
        with self.assertRaises(KeyError):
            settings.agent_detail(self.config, "reviewer-perf")
        with self.assertRaisesRegex(ValueError, "invalid agent name"):
            settings.reset_agent(self.config, "../builder")

    def test_reset_removes_a_broken_override(self) -> None:
        self.agents_dir.mkdir(parents=True)
        (self.agents_dir / "builder.md").write_text("broken\n")
        with self.assertRaisesRegex(ValueError, "builder.md"):
            settings.agent_detail(self.config, "builder")
        self.assertEqual(settings.reset_agent(self.config, "builder")["source"], "builtin")

    def test_agent_detail_sources_and_unknown(self) -> None:
        repo = make_repo(self.tmp, "app")
        agents = repo / ".factory" / "agents"
        agents.mkdir(parents=True)
        (agents / "reviewer-code.md").write_text("---\nname: reviewer-code\ndescription: Repo lens.\n---\nBody.\n")
        detail = settings.agent_detail(self.config, "reviewer-code", repo_path=repo)
        self.assertEqual((detail["source"], detail["description"]), ("repo", "Repo lens."))
        self.assertEqual(settings.agent_detail(self.config, "reviewer-code")["source"], "builtin")
        with self.assertRaises(KeyError):
            settings.agent_detail(self.config, "nope")

    def test_refuses_to_write_into_the_shipped_agents(self) -> None:
        home = self.tmp / "linked-home"
        (home / ".config").mkdir(parents=True)
        (home / ".config" / "software-factory").symlink_to(PROJECT_DIR, target_is_directory=True)
        os.environ["HOME"] = str(home)  # ~/.config/software-factory/agents is now the shipped agents/
        with self.assertRaisesRegex(ValueError, "shipped"):
            settings.save_agent(self.config, "builder", self.builtin_text("builder"))
        with self.assertRaisesRegex(ValueError, "shipped"):
            settings.reset_agent(self.config, "builder")

    def test_overrides_dir_is_the_one_the_factory_reads(self) -> None:
        """$FACTORY_CONFIG outside ~/.config: settings and the Factory still share one agents dir."""
        from swfactory.config import agent_dirs
        from swfactory.events import EventBus
        from swfactory.orchestrator import Factory

        self.assertNotEqual(self.config_path.parent, self.agents_dir.parent)
        self.assertEqual(settings.user_agents_dir(self.config), self.agents_dir)
        settings.new_agent(self.config, "reviewer-perf")
        self.assertTrue((self.agents_dir / "reviewer-perf.md").is_file())
        self.assertFalse((self.config_path.parent / "agents").exists())
        self.assertEqual(settings.agent_search_dirs(self.config), agent_dirs(self.config, None))
        updated = settings.update_config(self.config, {"pipeline": {"reviewers": ["security", "perf"]}})
        factory = Factory(updated, EventBus())
        self.addCleanup(factory.stop, 5)
        self.assertEqual(set(factory.agents()), set(settings.resolved_agents(updated)))
        self.assertEqual(factory.agents()["reviewer-perf"].path, self.agents_dir / "reviewer-perf.md")
        self.assertEqual(settings.agent_detail(updated, "reviewer-perf")["source"], "user")

    def test_an_override_with_another_file_name_is_edited_in_place(self) -> None:
        self.agents_dir.mkdir(parents=True)
        mine = self.agents_dir / "my-builder.md"
        mine.write_text(self.builtin_text("builder").replace("You build one factory job.", "Mine."))
        detail = settings.agent_detail(self.config, "builder")
        self.assertEqual((detail["source"], detail["path"]), ("user", str(mine)))
        self.assertEqual(settings.user_override_files(self.config, "builder"), [mine])
        text = self.builtin_text("builder").replace("You build one factory job.", "Edited in the GUI.")
        saved = settings.save_agent(self.config, "builder", text)
        self.assertEqual((saved["path"], saved["source"]), (str(mine), "user"))
        self.assertEqual(sorted(p.name for p in self.agents_dir.iterdir()), ["my-builder.md"])
        self.assertIn("Edited in the GUI.", settings.agent_detail(self.config, "builder")["body"])
        self.assertNotIn("agents_error", settings.config_view(self.config))
        restored = settings.reset_agent(self.config, "builder")
        self.assertEqual(restored["source"], "builtin")
        self.assertEqual(list(self.agents_dir.iterdir()), [])
        with self.assertRaisesRegex(KeyError, "no user override"):
            settings.reset_agent(self.config, "builder")

    def test_a_save_that_would_break_the_dir_writes_nothing(self) -> None:
        self.agents_dir.mkdir(parents=True)
        (self.agents_dir / "builder.md").write_text("---\nname: helper\ndescription: Not the builder.\n---\nHelp.\n")
        before = {p.name: p.read_bytes() for p in self.agents_dir.iterdir()}
        with self.assertRaisesRegex(ValueError, "does not define agent 'builder'"):
            settings.save_agent(self.config, "builder", self.builtin_text("builder"))
        self.assertEqual({p.name: p.read_bytes() for p in self.agents_dir.iterdir()}, before)
        self.assertEqual(settings.agent_detail(self.config, "helper")["source"], "user")
        (self.agents_dir / "builder.md").unlink()
        for file_name in ("one.md", "two.md"):
            (self.agents_dir / file_name).write_text(self.builtin_text("builder"))
        before = {p.name: p.read_bytes() for p in self.agents_dir.iterdir()}
        with self.assertRaisesRegex(ValueError, "defined by 2 files"):
            settings.save_agent(self.config, "builder", self.builtin_text("builder") + "\nMore.\n")
        self.assertEqual({p.name: p.read_bytes() for p in self.agents_dir.iterdir()}, before)
        self.assertEqual(settings.reset_agent(self.config, "builder")["source"], "builtin", "reset repairs it")
        self.assertEqual(list(self.agents_dir.iterdir()), [])

    def test_a_broken_neighbour_does_not_fail_a_save_that_happened(self) -> None:
        self.agents_dir.mkdir(parents=True)
        (self.agents_dir / "broken.md").write_text("no frontmatter\n")
        text = self.builtin_text("builder") + "\nMore.\n"
        detail = settings.save_agent(self.config, "builder", text)
        self.assertEqual((detail["source"], detail["text"]), ("user", text))
        self.assertIn("broken.md", settings.config_view(self.config)["agents_error"])


if __name__ == "__main__":
    unittest.main()


class WakesNotifySettingsTest(SettingsTestCase):
    """settings._check_wakes and _check_notify (Wake & Messages §5.4): shape and bounds only."""

    PHONE = {"type": "ntfy", "publish_url": "unix:/run/ntfy/ntfy.sock",
             "public_url": "https://host.tail0123.ts.net:8445", "token_env": "SERAPHINA_NTFY_TOKEN",
             "reply_token_env": "SERAPHINA_NTFY_REPLY_TOKEN", "model_text": "clean_only"}

    def save(self, patch: dict) -> Config:
        return settings.update_config(self.make_config(), patch)

    def refused(self, patch: dict, fragment: str) -> None:
        with self.assertRaises(ValueError) as caught:
            self.save(patch)
        self.assertIn(fragment, str(caught.exception))

    def test_defaults_validate_and_a_channel_saves(self) -> None:
        saved = self.save({"wakes": {"timezone": "Europe/London"}, "notify": {"channels": {"phone": self.PHONE}}})
        self.assertEqual(saved.raw["notify"]["channels"]["phone"]["model_text"], "clean_only")
        self.assertEqual(saved.raw["notify"]["routes"]["needs_you"], ["*"])
        self.assertEqual(saved.raw["wakes"]["power"]["off"], "23:00")

    def test_bounds_and_shapes(self) -> None:
        self.refused({"notify": {"phone_max_per_day": 500}}, "notify.phone_max_per_day must be an integer from 0 to 200")
        self.refused({"notify": {"inbound": {"stale_minutes": 0}}}, "notify.inbound.stale_minutes")
        self.refused({"notify": {"quiet_hours": "late"}}, "notify.quiet_hours must be auto or a window")
        self.refused({"notify": {"rule_priority": {"job.failed": "loud"}}}, "notify.rule_priority.job.failed")
        self.refused({"notify": {"quiet_breakthrough": ["everything"]}}, "notify.quiet_breakthrough")
        self.refused({"notify": {"digests": {"midday": "noon"}}}, "notify.digests.midday")
        self.refused({"notify": {"safe_repos": ["ghost"]}}, "notify.safe_repos")
        self.refused({"notify": {"cockpit_url": "https://evil.example"}}, "notify.cockpit_url")
        self.refused({"wakes": {"timezone": "Mars/Olympus"}}, "wakes.timezone")
        self.refused({"wakes": {"power": {"off": "25:00"}}}, "wakes.power.off")
        self.refused({"wakes": {"power": {"off": "07:00", "wake": "07:00"}}}, "must differ")
        self.refused({"wakes": {"retention": {"text_days": 0}}}, "wakes.retention.text_days")
        self.save({"notify": {"quiet_hours": "22:00-07:30", "cockpit_url": "https://cockpit.tail0123.ts.net",
                              "digests": {"midday": "12:30"}}})

    def test_channels(self) -> None:
        tz = {"timezone": "Europe/London"}
        self.refused({"notify": {"channels": {"phone": self.PHONE}}}, "wakes.timezone is needed")
        self.refused({"wakes": tz, "notify": {"channels": {"phone": {**self.PHONE, "privacy": "tailnet"}}}},
                     "privacy is computed")
        self.refused({"wakes": tz, "notify": {"channels": {"phone": {**self.PHONE,
                                                                     "public_url": "https://ntfy.sh"}}}},
                     "model_text: a third-party channel")
        self.refused({"wakes": tz, "notify": {"channels": {"tg": {"type": "telegram", "model_text": "clean_only"}}}},
                     "never carries an assistant's own words")
        self.refused({"wakes": tz, "notify": {"channels": {"phone": {**self.PHONE, "taps_topic": "seraphina"}}}},
                     "three different names")
        self.refused({"wakes": tz, "notify": {"channels": {"phone": self.PHONE, "other": {**self.PHONE,
                                                                                          "chat_topic": "x-chat"}}}},
                     "is also notify.channels.phone's")
        self.refused({"wakes": tz, "notify": {"channels": {"phone": {**self.PHONE, "publish_url": "https://ntfy.sh"}}}},
                     "publish_url")
        self.refused({"wakes": tz, "notify": {"channels": {"phone": {**self.PHONE, "token_env": "PATH"}}}},
                     "token_env")
        self.refused({"wakes": tz, "notify": {"channels": {"Bad": self.PHONE}}}, "invalid name")
        self.refused({"wakes": tz, "notify": {"routes": {"needs_you": ["nowhere"]}}}, "notify.routes.needs_you")
        self.refused({"wakes": tz, "notify": {"routes": {"loud": ["*"]}}}, "is not a route")
        self.save({"wakes": tz, "notify": {"channels": {"phone": self.PHONE},
                                           "routes": {"needs_you": ["phone", "private_first"]}}})

    def test_hooks(self) -> None:
        digest = "a" * 64
        hook = {"token_sha256": digest, "may": ["event"], "events": ["backup.failed"], "subjects": ["nightly"]}
        self.save({"wakes": {"hooks": {"backup-script": hook}}})
        self.refused({"wakes": {"hooks": {"a": hook, "b": hook}}}, "each source needs its own token")
        self.refused({"wakes": {"hooks": {"x": {**hook, "token_sha256": "plain-token"}}}}, "token_sha256")
        self.refused({"wakes": {"hooks": {"x": {**hook, "may": ["approve"]}}}}, "a list of event, wake")
        self.save({"wakes": {"hooks": {"backup-script": None, "x": {**hook, "may": ["wake"],
                                                                    "personas": ["seraphina"]}}}})
        self.refused({"wakes": {"hooks": {"x": {**hook, "events": ["rm"]}}}}, "wakes.hooks.x.events")
        self.refused({"wakes": {"hooks": {"x": {**hook, "per_minute": 1000}}}}, "wakes.hooks.per_minute")
        self.refused({"wakes": {"hooks": {"x": {**hook, "token": "abc"}}}}, "is not a setting")

    def test_a_missing_persona_file_never_blocks_an_unrelated_save(self) -> None:
        self.save({"notify": {"default_persona": "nobody-here"}})
        self.save({"limits": {"max_turns": 99}})


class WakePersonaSettingsTest(SettingsTestCase):
    """wakes.personas, wakes.servers and the step W2/W3 groups (Wake & Messages §5.4, decisions a-c, e, f)."""

    save = WakesNotifySettingsTest.save
    refused = WakesNotifySettingsTest.refused

    def persona(self, **entry: object) -> dict:
        return {"wakes": {"timezone": "Europe/London", "personas": {"seraphina": entry}}}

    def test_a_persona_saves_with_no_model_and_no_file(self) -> None:
        saved = self.save(self.persona(enabled=False, model=None))
        self.assertIsNone(saved.raw["wakes"]["personas"]["seraphina"]["model"])
        self.save({"wakes": {"personas": {"nobody": {"model": None}}}})
        self.save({"limits": {"max_turns": 50}})

    def test_persona_keys_and_the_operator_decisions(self) -> None:
        self.save(self.persona(memory={"curation": "self"}, wake={"tools": "read_only",
                                                                 "read_paths": ["/srv/notes"],
                                                                 "commands": [["systemctl", "status"]]},
                               reimport_decider="persona", instance_name="Sera at home"))
        self.save(self.persona(memory={"external": {"folder": "/srv/sera/memory", "outbox": "/srv/sera/outbox.jsonl",
                                                    "sqlite": {"path": "/srv/sera/memory.db",
                                                               "query": "SELECT text, at FROM facts"}}}))
        # a wake that reads and never pokes, and the read ledger of her own memory (both explicit choices)
        saved = self.save(self.persona(wake={"tools": "read_only", "read_paths": ["/srv/notes"], "commands": []},
                                       memory={"external": {"folder": "/srv/sera/memory", "ledger": True}}))
        self.assertEqual(saved.raw["wakes"]["personas"]["seraphina"]["wake"]["commands"], [],
                         "an empty list is kept as written, never read as the default")
        refusals = [
            (dict(approve_runs=True), "is not a persona setting"),
            (dict(memory={"curation": "anyone"}), "owner or self"),
            (dict(memory={"shared": True}), "only curation and external"),
            (dict(memory={"external": {"folder": "relative/path"}}), "an absolute path"),
            (dict(memory={"external": {"sqlite": {"path": "/srv/m.db", "query": "DELETE FROM facts"}}}), "SELECT"),
            (dict(memory={"external": {"folder": "/srv/m", "taint": []}}), "never clean"),
            (dict(memory={"external": {"folder": "/srv/m", "ledger": "yes"}}), "true or false"),
            (dict(memory={"external": {"folder": "/srv/m", "versions": True}}), "outbox, taint, ledger"),
            (dict(wake={"tools": "all"}), "none or read_only"),
            (dict(wake={"tools": "read_only", "write_paths": ["/"]}), "only tools, read_paths and commands"),
            (dict(wake={"commands": [["sh", "-c", "rm -rf ~; echo"]]}), "argv prefixes"),
            (dict(wake={"commands": [["gh", "run", "list"]]}), "runs with no network (its context is private)"),
            (dict(reimport_decider="nobody"), "owner or persona"),
            (dict(instance_name="<script>"), "letters, digits"),
            (dict(ambient={"active": "always"}), "auto"),
            (dict(model="hosted model"), "a models key"),
        ]
        for entry, words in refusals:
            with self.subTest(entry=entry):
                self.refused(self.persona(**entry), words)
        self.refused({"wakes": {"personas": {"Bad Name": {}}}}, "invalid persona name")

    def test_the_read_and_ledger_caps_are_settings_with_bounds(self) -> None:
        """The operator's ruling (26 Oct): the caps of one read and of the read ledger are settings, not constants.
        memory.external.max_files (default 200), read_mib (8), ledger_mib (32) and ledger_copies (2000)."""
        from swfactory import personas

        self.assertEqual(personas.external_caps(None), {"max_files": 200, "read_bytes": 8 << 20,
                                                        "ledger_bytes": 32 << 20, "ledger_copies": 2000,
                                                        "ledger_mib": 32})
        caps = {"max_files": 400, "read_mib": 16, "ledger_mib": 64, "ledger_copies": 5000}
        saved = self.save(self.persona(memory={"external": {"folder": "/srv/sera/memory", "ledger": True, **caps}}))
        external = saved.raw["wakes"]["personas"]["seraphina"]["memory"]["external"]
        self.assertEqual(personas.external_caps(external), {"max_files": 400, "read_bytes": 16 << 20,
                                                            "ledger_bytes": 64 << 20, "ledger_copies": 5000,
                                                            "ledger_mib": 64})
        for key, bad, words in (("max_files", 0, "an integer from 1 to 2000"), ("max_files", 2001, "an integer from 1 to 2000"),
                                ("max_files", True, "an integer from 1"), ("read_mib", 65, "an integer from 1 to 64"),
                                ("ledger_mib", 0, "an integer from 1 to 1024"),
                                ("ledger_copies", 9, "an integer from 10 to 100000"), ("ledger_copies", "lots", "an integer from 10")):
            with self.subTest(key=key, value=bad):
                self.refused(self.persona(memory={"external": {"folder": "/srv/m", key: bad}}),
                             f"wakes.personas.seraphina.memory.external.{key} must be {words}")
        self.assertEqual(personas.external_caps({"max_files": 0, "ledger_mib": "64"})["max_files"], 200,
                         "a hand edit the settings would refuse counts as unset")

    def test_local_attested_is_never_set_in_the_config(self) -> None:
        self.save({"wakes": {"servers": {"local": {"kind": "llamacpp", "router": True,
                                                   "root_url": "http://127.0.0.1:8080"}}}})
        self.refused({"wakes": {"servers": {"local": {"local_attested": True}}}}, "set only by the persona setup")
        self.refused({"wakes": {"servers": {"local": {"root_url": "http://example.com:8080"}}}}, "loopback")
        self.refused({"wakes": {"servers": {"local": {"kind": "vllm-cloud"}}}}, "one of")

    def test_chat_check_in_commands_carry_no_shell(self) -> None:
        self.save({"wakes": {"chat_wakes": {"enabled": False, "commands": [["gh", "run", "view"]]}}})
        self.refused({"wakes": {"chat_wakes": {"commands": [["gh", "run", "view", "$(id)"]]}}}, "no shell syntax")
        # the setup's three choices (DESIGN §35): [] is no commands, null is the default list again
        from swfactory import wakes
        cfg = self.save({"wakes": {"chat_wakes": {"commands": []}}})
        self.assertEqual(wakes.chat_wake_commands(cfg), [])
        cfg = settings.update_config(cfg, {"wakes": {"chat_wakes": {"commands": None}}})
        self.assertEqual(wakes.chat_wake_commands(cfg), [list(c) for c in wakes.CHAT_WAKE_DEFAULTS["commands"]])
        self.refused({"wakes": {"external_memory_roots": ["relative"]}}, "absolute folders")


class RefusedTunnelTest(unittest.TestCase):
    """A provider read through an HTTPS proxy that refuses the CONNECT: the problem in words, and no socket left for
    the garbage collector (Python 3.10's http.client keeps the tunnel's response in a frame of the traceback; the
    harness clears those frames: settings.release_frames)."""

    def test_a_refused_tunnel_leaves_no_socket_behind(self):
        import gc
        import warnings

        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(4)
        port = server.getsockname()[1]

        def refuse():
            conn, _ = server.accept()
            with conn:
                conn.recv(4096)
                conn.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
        thread = threading.Thread(target=refuse, daemon=True)
        thread.start()
        self.addCleanup(server.close)
        config = Config(Path(tempfile.mkdtemp()) / "c.json",
                        {"providers": {"far": {"type": "openai", "base_url": "https://models.example.com/v1"}}})
        self.addCleanup(shutil.rmtree, config.path.parent, True)
        proxy = f"http://127.0.0.1:{port}"
        with mock.patch.dict(os.environ, {"https_proxy": proxy, "HTTPS_PROXY": proxy, "no_proxy": "", "NO_PROXY": ""}), \
                warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            status, listed, problem = settings.list_models(config.provider("far"), 5)
            gc.collect()
        thread.join(5)
        self.assertEqual((status, listed), (None, None))
        self.assertIn("cannot reach https://models.example.com/v1/models", problem)
        self.assertEqual([str(w.message) for w in caught if issubclass(w.category, ResourceWarning)], [])

    def test_release_frames_walks_the_chain(self):
        def inner():
            held = object()   # noqa: F841 - a local the cleared frame must drop
            raise OSError("inner")
        try:
            try:
                inner()
            except OSError as exc:
                raise ValueError("outer") from exc
        except ValueError as err:
            caught = err
        frame = caught.__cause__.__traceback__.tb_next.tb_frame
        self.assertIn("held", frame.f_locals)
        settings.release_frames(caught)
        self.assertNotIn("held", frame.f_locals)
