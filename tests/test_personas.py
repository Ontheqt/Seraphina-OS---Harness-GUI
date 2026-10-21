"""Tests for swfactory.personas (DESIGN.md section 33): personas, the Open WebUI binding, the importer and its
profile card, fingerprints and drift, persona chats, the doctor's checks, the routes, and contract tests of
what the harness assumes about Open WebUI, against tests/mock_openwebui.py (no network, no real keys)."""

from __future__ import annotations

import copy
import json
import os
import shutil
import stat
import tempfile
import time
import unittest
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest import mock

from swfactory import doctor, personas, settings
from swfactory import items as tracker
from swfactory.agentdefs import parse_agent_file
from swfactory.chats import ChatManager
from swfactory.config import PROJECT_DIR, Config, agent_dirs
from swfactory.events import EventBus
from swfactory.harness.providers import OpenAIProvider
from tests.helpers import make_repo
from tests.mock_llm import MockLLM
from tests.mock_openwebui import OWNER_KEY, READER_KEY, MockOpenWebUI

KATE_PROMPT = ("You are Kate. KATE-MARKER. Today is {{CURRENT_DATE}} ({{CURRENT_WEEKDAY}}, {{CURRENT_TIMEZONE}}). "
               "Hello {{USER_NAME}}. Answer in {{USER_LANGUAGE}}. Near {{USER_LOCATION}}.")
KATE_PARAMS = {"temperature": 0.6, "top_p": 0.95, "max_tokens": 4096, "stop": ["\\n\\nUser:"],
               "custom_params": {"repetition_penalty": "1.05", "top_k": "20", "chat_template_kwargs": '{"x": 1}'},
               "function_calling": "native", "reasoning_tags": ["<think>", "</think>"]}
KATE_META = {"knowledge": [{"id": "kb-1", "name": "Kate notes", "type": "collection"}],
             "toolIds": ["calendar", "server:mcp:files"], "filterIds": ["memory"], "defaultFilterIds": ["tone"],
             "skillIds": ["kind-replies"], "actionIds": ["copy-out"], "capabilities": {"vision": False},
             "tags": [{"name": "specialist"}], "suggestion_prompts": [{"content": "Hi"}]}
WAIT = 10.0


def wait_for(predicate: Any, timeout: float = WAIT) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("timed out")


def finished_turn(manager: ChatManager, chat: dict, text: str) -> dict:
    """Send text to the chat and return it once the turn is over."""
    manager.chat_send(chat["id"], text)
    return wait_for(lambda: (lambda c: c if c["status"] != "running" else None)(manager.chat(chat["id"])))


class PersonaTestCase(unittest.TestCase):
    """A temp HOME, OPENWEBUI_API_KEY set to the mock's owner key, a fixed time zone, and a mock Open WebUI
    serving the workspace model kate."""

    key = OWNER_KEY

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sf-personas-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home), "OPENWEBUI_API_KEY": self.key,
                                               "TZ": "Europe/London", "LANG": "en_GB.UTF-8"})
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("LC_ALL", "LC_MESSAGES"):
            os.environ.pop(name, None)
        self.owui = MockOpenWebUI()
        self.owui.start()
        self.addCleanup(self.owui.stop)
        self.kate = self.owui.add_workspace_model("kate", system=KATE_PROMPT, params=KATE_PARAMS, meta=KATE_META)
        self.user_agents = self.home / ".config" / "software-factory" / "agents"

    def make_config(self, **raw: Any) -> Config:
        preset = settings.load_preset("openwebui")
        provider = dict(preset["providers"]["openwebui"], base_url=self.owui.base_url + "/api")
        data = {"data_dir": str(self.tmp / "data"), "providers": {"openwebui": provider},
                "models": copy.deepcopy(preset["models"])}
        for key, value in raw.items():
            if isinstance(value, dict) and isinstance(data.get(key), dict):
                data[key] = {**data[key], **value}
            else:
                data[key] = value
        return Config(self.tmp / "config.json", data)

    def user_agent(self, name: str, frontmatter: str, body: str = "Thin note.") -> Path:
        self.user_agents.mkdir(parents=True, exist_ok=True)
        path = self.user_agents / f"{name}.md"
        path.write_text(f"---\nname: {name}\ndescription: {name} test persona\n{frontmatter}---\n{body}\n")
        return path

    def system_items(self) -> list[dict]:
        path = self.tmp / "data" / "housekeeping" / "system.json"
        return json.loads(path.read_text())["items"] if path.is_file() else []


class _Factory:
    """What ChatManager asks of a factory in these tests (no system scope: the drift item is written directly)."""


class DiscoveryTest(PersonaTestCase):
    def test_the_shipped_kate_persona(self) -> None:
        found = personas.discover_personas(self.make_config())
        kate = found["kate"]
        self.assertEqual((kate.kind, kate.privacy, kate.error, kate.binding["model"]), ("openwebui", "private", None, "kate"))
        self.assertEqual(kate.path, PROJECT_DIR / "agents" / "kate.md")
        self.assertEqual((kate.agent.tools, kate.agent.meta["model"]), ([], "kate"))
        self.assertLess(len(kate.soul), 400, "the shipped body is a thin note, not a persona")
        view = kate.to_dict()
        self.assertNotIn("soul", view)
        self.assertEqual(view["soul_sha256"], kate.soul_sha256)
        self.assertEqual(personas.label(kate), "Kate through Open WebUI")

    def test_personas_live_in_the_project_and_user_dirs_only(self) -> None:
        repo = make_repo(self.tmp, "repo")
        repo_agents = repo / ".factory" / "agents"
        repo_agents.mkdir(parents=True)
        (repo_agents / "spy.md").write_text("---\nname: spy\ndescription: x\nopenwebui: kate\n---\nBody\n")
        config = self.make_config(repos=[{"name": "app", "path": str(repo)}])
        self.assertEqual(personas.persona_dirs(config), agent_dirs(config, None))
        self.assertNotIn("spy", personas.discover_personas(config))
        with self.assertRaises(KeyError):
            personas.find_persona(config, "spy")

    def test_kinds_flags_and_refusals(self) -> None:
        self.user_agent("seraphina", "wake: true\nprivacy: clean\n")
        self.user_agent("typo", "wake: perhaps\n")
        self.user_agent("kate-offline", "source: openwebui\nsource_model: kate\nsource_version_id: v1\noffline: true\n"
                                        "model: flash\n")
        self.user_agent("quiet", "openwebui: kate\nknowledge: false\nfilters: no\ntimezone: Mars/Olympus\n")
        self.user_agent("Loud", "openwebui: kate\n")
        self.user_agent("helper", "tools: Read\n")
        self.user_agent("kate", "openwebui: kate-v2\nopenwebui_provider: owui2\nlanguage: de-DE\n")
        found = personas.discover_personas(self.make_config())
        self.assertNotIn("helper", found)
        self.assertEqual((found["seraphina"].kind, found["seraphina"].privacy), ("wake", "clean"))
        self.assertEqual(found["typo"].kind, "wake", "a malformed wake: counts as true")
        snapshot = found["kate-offline"]
        self.assertEqual((snapshot.kind, snapshot.binding["model"], snapshot.source["version_id"]),
                         ("snapshot", "kate", "v1"))
        self.assertEqual(personas.label(snapshot)[:31], "Kate, imported snapshot of an u")
        quiet = found["quiet"]
        self.assertEqual((quiet.binding["knowledge"], quiet.binding["filters"]), (False, False))
        self.assertIn("timezone 'Mars/Olympus'", quiet.error)
        self.assertIn("a persona's name must match", found["Loud"].error)
        kate = found["kate"]   # the user dir overrides the shipped file
        self.assertEqual((kate.binding["model"], kate.binding["provider"], kate.binding["language"]),
                         ("kate-v2", "owui2", "de-DE"))
        self.assertEqual(personas.binding_provider(self.make_config(), kate), "owui2")


class ExportParsingTest(PersonaTestCase):
    def test_the_soul_variables_unavailable_parts_and_effective_settings(self) -> None:
        profile = personas.parse_openwebui_export(self.owui.export_file("kate"), "kate")
        self.assertEqual(profile.system.encode(), KATE_PROMPT.encode(), "the soul is byte-equal to params.system")
        self.assertEqual(profile.variables, ["CURRENT_DATE", "CURRENT_TIMEZONE", "CURRENT_WEEKDAY", "USER_LANGUAGE",
                                             "USER_NAME"])
        self.assertEqual(profile.unknown_variables, ["USER_LOCATION"])
        self.assertEqual(profile.unavailable, {"tools": ["calendar", "server:mcp:files"], "knowledge": ["Kate notes"],
                                               "filters": ["memory", "tone"], "skills": ["kind-replies"],
                                               "actions": ["copy-out"], "features": []})
        self.assertEqual((profile.version_id, profile.base_model_id, profile.name),
                         (self.kate["version_id"], "qwen3.8-flash", "Kate"))
        self.assertEqual(profile.effective, {"temperature": 0.6, "top_p": 0.95, "max_tokens": 4096,
                                             "stop": ["\\n\\nUser:"], "repetition_penalty": 1.05, "top_k": 20,
                                             "chat_template_kwargs": {"x": 1}},
                         "stop as stored: Open WebUI 0.12.3 does not unescape it for an API caller")
        self.assertIn("reach the model as written, not as a newline", " ".join(profile.notes))
        self.assertNotIn("system", profile.params)
        self.assertEqual(profile.stream, None)

    def test_the_merge_with_open_webui_s_global_defaults(self) -> None:
        defaults = {"temperature": 0.2, "seed": 7, "custom_params": {"min_p": "0.05", "top_k": "40"}}
        merged = personas.effective_params(KATE_PARAMS, defaults)
        self.assertEqual((merged["temperature"], merged["seed"], merged["min_p"], merged["top_k"]), (0.6, 7, 0.05, 20))
        self.assertEqual(personas.effective_params({"stream_response": False, "system": "x", "model_controls": {}}), {})
        self.assertEqual(personas.effective_params({"stop": "\\nEND"})["stop"], "\\nEND", "kept as stored")
        self.assertEqual(personas.effective_params({"custom_params": {"nested": '{"a": {"b": 1}}', "word": "plain"},
                                                    "nested": {"a": {"c": 2}}}),
                         {"nested": {"a": {"c": 2, "b": 1}}, "word": "plain"})

    def test_fingerprints_ignore_cosmetic_edits_and_key_order(self) -> None:
        record = self.owui.export_file("kate")[0]
        base = personas.parse_openwebui_export([record], "kate")
        reordered = json.loads(json.dumps(record, sort_keys=True))
        reordered["params"] = dict(reversed(list(reordered["params"].items())))
        same = personas.parse_openwebui_export([reordered], "kate")
        self.assertEqual((same.fingerprint, same.behaviour_fingerprint, same.live_fingerprint),
                         (base.fingerprint, base.behaviour_fingerprint, base.live_fingerprint))
        cosmetic = copy.deepcopy(record)
        cosmetic.update(name="Kate II", version_id="other", updated_at=1, background_image_data="data:x")
        cosmetic["meta"].update(description="new words", profile_image_url="data:image/png;base64,BBBB",
                                tags=[{"name": "other"}], suggestion_prompts=[])
        edited = personas.parse_openwebui_export([cosmetic], "kate")
        self.assertEqual((edited.behaviour_fingerprint, edited.live_fingerprint),
                         (base.behaviour_fingerprint, base.live_fingerprint))
        prompt = copy.deepcopy(record)
        prompt["params"]["system"] += " Be brief."
        changed = personas.parse_openwebui_export([prompt], "kate")
        self.assertNotEqual(changed.behaviour_fingerprint, base.behaviour_fingerprint)
        self.assertNotEqual(changed.fingerprint, base.fingerprint)
        self.assertEqual(changed.live_fingerprint, base.live_fingerprint, "GET /api/models never shows the prompt")
        string_temperature = copy.deepcopy(record)
        string_temperature["params"]["custom_params"]["repetition_penalty"] = 1.05   # the same setting, unparsed
        self.assertEqual(personas.parse_openwebui_export([string_temperature], "kate").behaviour_fingerprint,
                         base.behaviour_fingerprint, "the fingerprint is over Open WebUI's merge, not the raw text")
        tool = copy.deepcopy(record)
        tool["meta"]["toolIds"].append("weather")
        added = personas.parse_openwebui_export([tool], "kate")
        self.assertNotEqual(added.live_fingerprint, base.live_fingerprint)
        self.assertNotEqual(added.behaviour_fingerprint, base.behaviour_fingerprint)
        self.assertNotEqual(personas.parse_openwebui_export([record], "kate", defaults={"seed": 1}).behaviour_fingerprint,
                            base.behaviour_fingerprint, "the global defaults count when known")

    def test_refusals_and_notes(self) -> None:
        with self.assertRaisesRegex(ValueError, "JSON array"):
            personas.parse_openwebui_export("nope")
        with self.assertRaisesRegex(ValueError, r"no model 'ghost' \(it has: kate\)"):
            personas.parse_openwebui_export(self.owui.export_file(), "ghost")
        self.owui.add_workspace_model("other")
        with self.assertRaisesRegex(ValueError, "holds 2 models"):
            personas.parse_openwebui_export(self.owui.export_file())
        self.assertEqual(personas.parse_openwebui_export(self.owui.export_file("kate")).model_id, "kate")
        self.assertEqual(personas.parse_openwebui_export(self.owui.export_file("kate")[0]).model_id, "kate")
        old = self.owui.export_file("kate")[0]
        old["params"].update(function_calling="default", model_controls={"mode": {"options": []}})
        notes = " ".join(personas.parse_openwebui_export([old]).notes)
        self.assertIn("runs as native now", notes)
        self.assertIn("model controls", notes)
        self.assertIn("{{USER_LOCATION}}", notes)

    def test_the_profile_card_holds_no_prompt_text_and_no_ids_of_people(self) -> None:
        card = personas.profile_card(personas.parse_openwebui_export(self.owui.export_file("kate")), source="export file")
        text = json.dumps(card)
        for secret in ("KATE-MARKER", "u-owner", "access_grants", "data:image"):
            self.assertNotIn(secret, text)
        self.assertEqual((card["system"]["chars"], card["base_model_id"], card["version_id"]),
                         (len(KATE_PROMPT), "qwen3.8-flash", self.kate["version_id"]))
        self.assertEqual(card["filters"], {"always": ["memory"], "default_on": ["tone"]})
        self.assertEqual([(tool["id"], tool["kind"], tool["through_api"]) for tool in card["tools"]],
                         [("calendar", "tool", False), ("server:mcp:files", "mcp", False)])
        parts = {part["part"]: part for part in card["parts"]}
        self.assertEqual({name: part["through_api"] for name, part in parts.items()},
                         {"system prompt": True, "settings": True, "knowledge": True, "filters": True,
                          "default filters": True, "skills": True, "tools": False, "actions": False})
        self.assertIn("server:mcp:files can be connected to the harness", parts["tools"]["note"])
        self.assertEqual(card["knowledge"], [{"type": "collection", "id": "kb-1", "name": "Kate notes"}])


class ImportTest(PersonaTestCase):
    def test_an_export_file_becomes_a_private_profile_card(self) -> None:
        config = self.make_config()
        result = personas.import_profile(config, records=self.owui.export_file("kate"), model_id="kate")
        path = Path(result["path"])
        self.assertEqual(path, self.home / ".config" / "software-factory" / "profiles" / "openwebui" / "kate.json")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(personas.load_profile("kate"), result["card"])
        self.assertEqual((result["card"]["source"], result["snapshot"], result["unknown_variables"]),
                         ("export file", None, ["USER_LOCATION"]))
        self.assertEqual(self.owui.gets, [], "an uploaded file needs no Open WebUI")
        self.assertIsNone(personas.load_profile("other"))
        self.assertEqual(personas.slug("kate"), "kate")
        self.assertRegex(personas.slug("my model:latest"), r"^my_model_latest-[0-9a-f]{8}$")
        self.assertNotEqual(personas.slug("a/b"), personas.slug("a:b"), "ids that clean up alike stay apart")

    def test_through_the_api_with_the_owner_s_key(self) -> None:
        self.owui.default_params = {"seed": 3}
        result = personas.import_profile(self.make_config(), model_id="kate")
        card = result["card"]
        self.assertEqual((card["source"], card["open_webui_version"], card["defaults_known"], card["settings"]["seed"]),
                         ("api", "0.12.3", True, 3))
        self.assertIn(("/api/v1/models/export", OWNER_KEY), self.owui.gets)
        with mock.patch.dict(os.environ, {"OPENWEBUI_API_KEY": READER_KEY}):
            with self.assertRaisesRegex(personas.OpenWebUIError, "does not let this key export 'kate'.*HTTP 403"):
                personas.import_profile(self.make_config(), model_id="kate")
        with self.assertRaisesRegex(ValueError, "model_id"):
            personas.import_profile(self.make_config())
        exported = personas.fetch_openwebui_export(self.owui.base_url + "/api", OWNER_KEY, "kate")
        self.assertEqual([record["id"] for record in exported], ["kate"])

    def test_the_offline_snapshot_only_on_request(self) -> None:
        local = {"llamacpp": {"type": "openai", "base_url": "http://127.0.0.1:8080/v1", "api_key_env": None}}
        models = {"flash": {"provider": "llamacpp", "model": "qwen3.8-flash", "max_tokens": 16384,
                            "context_tokens": 131072, "extra_body": {"temperature": 0.1, "chat_template_kwargs": {}}},
                  "hosted": {"provider": "deepseek", "model": "qwen3.8-flash", "max_tokens": 8192}}
        config = self.make_config(providers=local, models=models)
        config.save()
        config = Config.load(config.path)
        saved: list[Config] = []
        result = personas.import_profile(config, records=self.owui.export_file("kate"), model_id="kate",
                                         snapshot={"name": "kate-offline", "user_name": "Sam"},
                                         on_saved=saved.append)
        made = result["snapshot"]
        self.assertEqual((made["persona"], made["model"], made["base"], made["candidates"]),
                         ("kate-offline", "kate-offline", "flash", ["flash"]), "the hosted copy is no candidate")
        entry = Config.load(config.path).raw["models"]["kate-offline"]
        self.assertEqual((entry["provider"], entry["model"], entry["max_tokens"]), ("llamacpp", "qwen3.8-flash", 4096))
        self.assertEqual(entry["extra_body"], {"temperature": 0.6, "chat_template_kwargs": {"x": 1}, "top_p": 0.95,
                                               "stop": ["\\n\\nUser:"], "repetition_penalty": 1.05, "top_k": 20})
        self.assertEqual(len(saved), 1)
        path = Path(made["path"])
        self.assertEqual((path, stat.S_IMODE(path.stat().st_mode)), (self.user_agents / "kate-offline.md", 0o600))
        agent = parse_agent_file(path)
        self.assertEqual(agent.body, KATE_PROMPT)
        self.assertEqual((agent.tools, agent.meta["privacy"], agent.meta["source_model"], agent.meta["user_name"]),
                         ([], "private", "kate", "Sam"))
        snapshot = personas.persona_of(agent)
        self.assertEqual((snapshot.kind, snapshot.binding["version_id"], snapshot.error),
                         ("snapshot", self.kate["version_id"], None))
        self.assertEqual(snapshot.binding["behaviour_fingerprint"], result["card"]["behaviour_fingerprint"])

    def test_the_snapshot_asks_when_the_base_model_is_ambiguous(self) -> None:
        local = {"llamacpp": {"type": "openai", "base_url": "http://127.0.0.1:8080/v1", "api_key_env": None},
                 "strata": {"type": "openai", "base_url": "http://127.0.0.1:18080/v1", "api_key_env": None}}
        models = {"flash": {"provider": "llamacpp", "model": "qwen3.8-flash"},
                  "flash-strata": {"provider": "strata", "model": "qwen3.8-flash"},
                  "hosted": {"provider": "deepseek", "model": "deepseek-v4-pro"}}
        config = self.make_config(providers=local, models=models)
        config.save()
        config = Config.load(config.path)
        profile = personas.parse_openwebui_export(self.owui.export_file("kate"), "kate")
        with self.assertRaisesRegex(ValueError, "maps to flash, flash-strata.*snapshot.model"):
            personas.write_snapshot(config, profile, {"name": "kate-offline"})
        with self.assertRaisesRegex(ValueError, "not through Open WebUI or an API"):
            personas.write_snapshot(config, profile, {"name": "kate-offline", "model": "hosted"})
        with self.assertRaisesRegex(ValueError, "already exists"):
            personas.write_snapshot(config, profile, {"name": "coder", "model": "flash"})
        with self.assertRaisesRegex(ValueError, "must match"):
            personas.write_snapshot(config, profile, {"name": "Kate Offline", "model": "flash"})
        made = personas.write_snapshot(config, profile, {"name": "kate-offline", "model": "flash-strata"})
        self.assertEqual(made["base"], "flash-strata")
        again = personas.write_snapshot(Config.load(config.path), profile, {"name": "kate-offline", "model": "flash"})
        self.assertEqual(again["base"], "flash", "re-importing the same model's snapshot replaces it")
        bare = copy.deepcopy(profile)
        bare.system = " "
        with self.assertRaisesRegex(ValueError, "no system prompt"):
            personas.write_persona_file(config, bare, "bare", model="flash")


class SnapshotSafetyTest(PersonaTestCase):
    """An offline snapshot never takes over a models key, never leaves half of itself behind, and a re-import
    replaces what the last one wrote."""

    def local_config(self) -> Config:
        local = {"tabby": {"type": "openai", "base_url": "http://127.0.0.1:5000/v1", "api_key_env": None}}
        models = {"flash": {"provider": "tabby", "model": "qwen3.8-flash", "max_tokens": 4096, "context_tokens": 32768}}
        config = self.make_config(providers=local, models=models, agents={"coder": {"model": "flash"}})
        config.save()
        return Config.load(config.path)

    def saved_models(self, config: Config) -> dict:
        return json.loads(config.path.read_text())["models"]

    def test_a_snapshot_never_takes_an_existing_models_key(self) -> None:
        config = self.local_config()
        before = self.saved_models(config)
        for name in ("flash", "opus", "haiku"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, f"models.{name} already exists.*choose another name"):
                    personas.import_profile(config, records=self.owui.export_file("kate"),
                                            snapshot={"name": name, "model": "flash"})
        self.assertEqual(self.saved_models(config), before, "no models entry changed")
        self.assertEqual(json.loads(config.path.read_text())["agents"]["coder"], {"model": "flash"})
        self.assertFalse((self.user_agents / "flash.md").exists())

    def test_a_snapshot_that_cannot_be_written_leaves_nothing_behind(self) -> None:
        config = self.local_config()
        self.owui.add_workspace_model("blank", params={"temperature": 1.3})
        with self.assertRaisesRegex(ValueError, "'blank' has no system prompt to copy"):
            personas.import_profile(config, records=self.owui.export_file("blank"),
                                    snapshot={"name": "blank-offline", "model": "flash"})
        self.assertNotIn("blank-offline", self.saved_models(config))
        self.assertFalse((self.user_agents / "blank-offline.md").exists())
        self.owui.add_workspace_model("huge", system="x" * (1 << 20))
        with self.assertRaisesRegex(ValueError, "larger than"):
            personas.import_profile(config, records=self.owui.export_file("huge"),
                                    snapshot={"name": "huge-offline", "model": "flash"})
        self.assertNotIn("huge-offline", self.saved_models(config))
        with mock.patch.object(settings, "save_agent", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                personas.import_profile(config, records=self.owui.export_file("kate"),
                                        snapshot={"name": "kate-offline", "model": "flash"})
        self.assertNotIn("kate-offline", self.saved_models(config), "the models entry is taken back")

    def test_a_re_import_replaces_the_snapshot_s_settings(self) -> None:
        self.owui.add_workspace_model("kate", system="You are Kate.",
                                      params={"temperature": 0.6, "custom_params": {"top_k": "20", "min_p": "0.05"}})
        config = self.local_config()
        personas.import_profile(config, records=self.owui.export_file("kate"),
                                snapshot={"name": "kate-offline", "model": "flash"})
        self.assertEqual(self.saved_models(config)["kate-offline"]["extra_body"],
                         {"temperature": 0.6, "top_k": 20, "min_p": 0.05})
        version = self.owui.edit("kate", params={"custom_params": {}})
        config = Config.load(config.path)
        personas.import_profile(config, records=self.owui.export_file("kate"),
                                snapshot={"name": "kate-offline", "model": "flash"})
        self.assertEqual(self.saved_models(config)["kate-offline"]["extra_body"], {"temperature": 0.6},
                         "settings the preset dropped are gone, not merged back")
        agent = parse_agent_file(self.user_agents / "kate-offline.md")
        self.assertEqual((agent.meta["source_version_id"], agent.meta["model"]), (version, "kate-offline"))
        with mock.patch.object(settings, "save_agent", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                personas.import_profile(Config.load(config.path), records=self.owui.export_file("kate"),
                                        snapshot={"name": "kate-offline", "model": "flash"})
        self.assertEqual(self.saved_models(config)["kate-offline"]["extra_body"], {"temperature": 0.6},
                         "a failed re-import puts the earlier entry back")
        self.user_agent("other", "source: openwebui\nsource_model: kate\noffline: true\nmodel: flash\n")
        with self.assertRaisesRegex(ValueError, "models.flash already exists"):
            personas.import_profile(Config.load(config.path), records=self.owui.export_file("kate"),
                                    snapshot={"name": "flash", "model": "flash"})


class BindingTest(PersonaTestCase):
    def persona(self) -> personas.Persona:
        return personas.find_persona(self.make_config(), "kate")

    def test_the_binding_read_at_chat_start(self) -> None:
        config = self.make_config()
        record = personas.start_binding(config, self.persona(), config.model("kate"))
        self.assertEqual((record["type"], record["model"], record["provider"], record["version_id"], record["name"]),
                         ("openwebui", "kate", "openwebui", self.kate_version(), "Kate"))
        self.assertEqual((record["knowledge"], record["filter_ids"]), ([], ["tone"]), "knowledge is opt-in")
        self.assertEqual(record["not_through_api"], ["knowledge", "tools", "actions"])
        self.assertEqual((record["open_webui_version"], record["drift"]["kind"], record["turns"]), ("0.12.3", "new", []))
        self.assertEqual((record["base_model"], record["warnings"]),
                         ({"id": "qwen3.8-flash", "listed": True, "owned_by": "openai", "connection_type": "local"}, []))
        self.assertNotIn("stream", record, "the harness never forces Open WebUI's stream setting")
        with mock.patch.dict(os.environ, {"OPENWEBUI_API_KEY": READER_KEY}):
            self.assertEqual(personas.start_binding(config, self.persona(), config.model("kate"))["version_id"],
                             self.kate_version(), "read access is enough to chat")

    def kate_version(self) -> str:
        return self.owui.records["kate"]["version_id"]

    def test_why_a_binding_cannot_start(self) -> None:
        config = self.make_config()
        kate = self.persona()
        with mock.patch.dict(os.environ, {"OPENWEBUI_API_KEY": ""}):
            with self.assertRaisesRegex(ValueError, "OPENWEBUI_API_KEY is not set.*API keys"):
                personas.start_binding(config, kate, config.model("kate"))
        with mock.patch.dict(os.environ, {"OPENWEBUI_API_KEY": "sk-wrong-0123456789"}):
            with self.assertRaisesRegex(ValueError, "refused OPENWEBUI_API_KEY.*HTTP 401"):
                personas.start_binding(config, kate, config.model("kate"))
        del self.owui.records["kate"]
        with self.assertRaisesRegex(ValueError, "does not list 'kate'"):
            personas.start_binding(config, kate, config.model("kate"))
        remote = self.make_config(models={"kate": {"provider": "deepseek", "model": "kate"}})
        with self.assertRaisesRegex(ValueError, "not on this machine"):
            personas.start_binding(remote, kate, remote.model("kate"))
        other = self.make_config(models={"kate": {"provider": "openwebui", "model": "kate-v2"}})
        with self.assertRaisesRegex(ValueError, "bound to Open WebUI's 'kate'.*'kate-v2'"):
            personas.start_binding(other, kate, other.model("kate"))
        with self.assertRaisesRegex(ValueError, "models.opus, which is not configured"):
            personas.check_model(Config(self.tmp / "c.json", {"models": {"opus": None}}), kate,
                                 config.model("kate").__class__(key="opus", provider=config.provider("openwebui"),
                                                                model="kate"))
        far = self.make_config(providers={"openwebui": {"type": "openai", "kind": "openwebui", "base_url":
                                                        "https://webui.example.com/api", "api_key_env": "OPENWEBUI_API_KEY"}})
        with self.assertRaisesRegex(personas.OpenWebUIError, "not on this machine"):
            personas.client(far, kate)

    def test_each_turn_s_body(self) -> None:
        config = self.make_config()
        kate = self.persona()
        record = personas.start_binding(config, kate, config.model("kate"))
        now = datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc)   # the clocks went back an hour later
        body = personas.turn_body(kate, record, now)
        self.assertEqual(body["variables"], {"{{CURRENT_DATE}}": "2026-10-25", "{{CURRENT_TIME}}": "01:30:00",
                                             "{{CURRENT_DATETIME}}": "2026-10-25 01:30:00",
                                             "{{CURRENT_WEEKDAY}}": "Sunday", "{{CURRENT_TIMEZONE}}": "Europe/London",
                                             "{{USER_LANGUAGE}}": "en-GB"})
        self.assertEqual((body.get("files"), body["filter_ids"]), (None, ["tone"]), "no knowledge unless turned on")
        self.assertEqual({key: body[key] for key in ("tool_ids", "chat_id", "id", "session_id", "max_tokens")},
                         dict.fromkeys(("tool_ids", "chat_id", "id", "session_id", "max_tokens")))
        personas.import_profile(config, records=self.owui.export_file("kate"))
        card = json.loads(personas.profile_path("kate").read_text())
        card["stream"] = False
        personas.save_profile(card)
        record = personas.start_binding(config, kate, config.model("kate"))
        self.assertNotIn("stream", personas.turn_body(kate, record), "a card's stream setting is never forced")
        self.user_agent("kate", "openwebui: kate\nmodel: kate\ntools: none\nknowledge: true\n")
        knowing = personas.find_persona(config, "kate")
        record = personas.start_binding(config, knowing, config.model("kate"))
        self.assertEqual(personas.turn_body(knowing, record)["files"], [{"type": "collection", "id": "kb-1"}])
        self.assertNotIn("knowledge", record["not_through_api"])
        self.user_agent("kate", "openwebui: kate\nmodel: kate\ntools: none\nknowledge: false\nfilters: false\n")
        quiet = personas.find_persona(config, "kate")
        record = personas.start_binding(config, quiet, config.model("kate"))
        self.assertEqual((record["knowledge"], record["filter_ids"]), ([], []))
        self.assertNotIn("files", personas.turn_body(quiet, record))
        self.assertIn("knowledge", record["not_through_api"])

    def test_fill_variables_for_an_offline_copy(self) -> None:
        text = personas.fill_variables(KATE_PROMPT, personas.prompt_variables("UTC", "en-US",
                                                                              datetime(2026, 1, 2, tzinfo=timezone.utc)),
                                       "Sam")
        self.assertIn("Today is 2026-01-02 (Friday, UTC). Hello Sam. Answer in en-US. Near {{USER_LOCATION}}.", text)


class PersonaChatTest(PersonaTestCase):
    """A persona's chat on the real builtin harness, through the mock Open WebUI."""

    def setUp(self) -> None:
        super().setUp()
        self.config = self.make_config()
        self.bus = EventBus()
        self.events = self.bus.subscribe()
        self.manager = ChatManager(self.config, self.bus, _Factory())
        self.addCleanup(self.manager.stop)

    def turn(self, chat: dict, text: str) -> dict:
        self.manager.chat_send(chat["id"], text)
        return wait_for(lambda: (lambda c: c if c["status"] != "running" else None)(self.manager.chat(chat["id"])))

    def test_a_chat_with_kate_through_open_webui(self) -> None:
        chat = self.manager.new_chat(None, persona="kate")
        self.assertEqual((chat["agent"], chat["mode"], chat["repo"], chat["model"]), ("kate", "none", None, "kate"))
        self.assertEqual(chat["persona"]["version_id"], self.owui.records["kate"]["version_id"])
        done = self.turn(chat, "hello")
        self.assertEqual((done["status"], done["error"]), ("idle", None))
        self.assertEqual(done["messages"][-1]["text"], "ECHO hello")
        sent = self.owui.requests[0]
        self.assertEqual(sent["model"], "kate")
        self.assertNotIn("max_tokens", sent, "the preset's max_tokens applies")
        self.assertNotIn("tools", sent, "Kate has no harness tools")
        self.assertEqual(self.owui.forbidden, [], "never tool_ids, chat_id, id or session_id")
        self.assertEqual((sent.get("files"), sent["filter_ids"], sent["variables"]["{{CURRENT_TIMEZONE}}"]),
                         (None, ["tone"], "Europe/London"), "the shipped kate.md leaves knowledge off")
        system = self.owui.upstream[0]["messages"][0]
        self.assertEqual(system["role"], "system")
        self.assertTrue(system["content"].startswith("You are Kate. KATE-MARKER. Today is "), system["content"])
        self.assertIn("Hello Owner.", system["content"])
        self.assertIn("The owner is talking to you from the harness's chat page", system["content"])
        for coding in ("Software Factory", "coding agent", "Working directory", "build.md"):
            self.assertNotIn(coding, sent["messages"][0]["content"])
        self.assertEqual(self.owui.upstream[0]["max_tokens"], 4096)
        self.assertEqual([m["role"] for m in self.owui.upstream[0]["messages"]], ["system", "user"])
        self.assertEqual([t["version_id"] for t in done["persona"]["turns"]], [self.owui.records["kate"]["version_id"]])
        log = (self.tmp / "data" / "chats" / chat["id"] / "logs" / "01-turn.jsonl").read_text()
        self.assertIn('"persona.turn"', log)
        self.assertNotIn("KATE-MARKER", log, "the preset's prompt never reaches a log")

    def test_each_turn_records_which_kate_answered_and_drift_opens_an_item(self) -> None:
        chat = self.manager.new_chat(None, persona="kate")
        first = self.owui.records["kate"]["version_id"]
        self.turn(chat, "one")
        second = self.owui.edit("kate", system=KATE_PROMPT + " Be brief.")
        done = self.turn(chat, "two")
        self.assertEqual([t["version_id"] for t in done["persona"]["turns"]], [first, second])
        self.assertEqual((done["persona"]["version_id"], done["persona"]["drift"]["kind"]), (second, "behaviour"))
        items = self.system_items()
        self.assertEqual([(i["id"], i["title"], i["owner"], i["source"]["change"]) for i in items],
                         [("S-1", "Kate changed in Open WebUI", "human", "behaviour")])
        self.assertIn(f"version {first[:12]} → {second[:12]}", items[0]["detail"])
        self.owui.edit("kate", system=KATE_PROMPT + " Be very brief.")
        self.turn(chat, "three")
        self.assertEqual(len(self.system_items()), 1, "one open item per model")
        self.owui.edit("kate", meta={"description": "renamed"})
        third = self.turn(chat, "four")
        self.assertEqual(third["persona"]["drift"]["kind"], "cosmetic")

    def test_a_persona_s_swept_words_never_reach_an_outside_agent(self) -> None:
        """§33 meets §32: a sentence of Kate's reply that the sweep suggests is a persona's words (private by
        default), so no agent token reads it and no person can give it to one, also once a deleted chat has
        carried it to the system scope."""
        chat = self.manager.new_chat(None, persona="kate")
        done = self.turn(chat, "I will update the dosage notes later.")
        self.assertEqual(done["status"], "idle", done.get("error"))
        swept = self.manager.scope_items(chat["id"])["items"]
        self.assertEqual([(i["status"], i["owner"], i["source"]["agent"], i["taint"]) for i in swept],
                         [("proposed", "kate", "persona:kate", ["private"])])
        self.assertTrue(tracker.persona_written(swept[0]))
        self.assertFalse(tracker.agent_visible({**swept[0], "owner": "agent:qwen-code"}, "qwen-code"))
        carried: list[dict] = []
        self.manager.factory.add_system_items = lambda entries: (carried.extend(entries),
                                                                 [f"system:S-{n}" for n, _ in enumerate(entries, 1)])[1]
        self.manager.chat_delete(chat["id"])
        self.assertEqual([(e["source"]["type"], e["source"]["agent"], e["taint"]) for e in carried],
                         [("carry", "persona:kate", ["private"])])
        ledger = tracker.Ledger(tracker.empty_doc(tracker.system_scope()), tracker.system_scope())
        copy_ = tracker.add_carried(ledger, dict(carried[0]))
        self.assertTrue(tracker.persona_written(copy_))
        with self.assertRaisesRegex(tracker.ItemError, "never given to an outside agent"):
            tracker.human_action(ledger, copy_["id"], "assign", owner="agent:qwen-code")

        self.user_agent("kate", "openwebui: kate\nmodel: kate\ntools: none\nprivacy: clean\n")
        clean = self.manager.new_chat(None, persona="kate")
        self.turn(clean, "We still need to test the login form.")
        item = self.manager.scope_items(clean["id"])["items"][0]
        self.assertEqual((item["source"]["agent"], item["taint"]), ("persona:kate", []),
                         "a clean persona's words are not private, but they are still a persona's")

    def test_a_preset_that_does_not_stream_answers_in_one_piece(self) -> None:
        self.owui.edit("kate", params={"stream_response": False})
        chat = self.manager.new_chat(None, persona="kate")
        done = self.turn(chat, "hello")
        self.assertEqual((done["status"], done["messages"][-1]["text"]), ("idle", "ECHO hello"))
        self.assertEqual(len(self.owui.requests), 1, "no retries of an answer that was whole")
        self.assertIs(self.owui.requests[0]["stream"], True)

    def test_a_card_that_says_no_stream_never_forces_it(self) -> None:
        self.owui.edit("kate", params={"stream_response": False})
        personas.import_profile(self.config, model_id="kate")
        self.assertIs(personas.load_profile("kate")["stream"], False)
        self.owui.edit("kate", params={"stream_response": True})   # the owner turns streaming on later
        chat = self.manager.new_chat(None, persona="kate")
        done = self.turn(chat, "hello")
        self.assertEqual((done["status"], done["error"], done["messages"][-1]["text"]), ("idle", None, "ECHO hello"))
        self.assertIs(self.owui.requests[-1]["stream"], True, "the models entry decides, not the card")
        preset = settings.load_preset("openwebui")["models"]["kate"]
        self.manager.config = self.make_config(models={"kate": {**preset, "stream": False}})
        done = self.turn(chat, "again")
        self.assertEqual((done["status"], done["messages"][-1]["text"]), ("idle", "ECHO again"), done["error"])
        self.assertIs(self.owui.requests[-1]["stream"], False)
        self.assertEqual(len(self.owui.requests), 2, "a stream answering a request without one is read once")

    def test_a_chat_stops_when_its_persona_now_wakes_itself(self) -> None:
        chat = self.manager.new_chat(None, persona="kate")
        self.user_agent("kate", "openwebui: kate\nmodel: kate\ntools: none\nwake: true\n", body="SOUL text.")
        done = self.turn(chat, "hello")
        self.assertEqual(done["status"], "error")
        self.assertIn("now a persona that wakes itself", done["error"])
        self.assertEqual(self.owui.requests, [], "nothing was sent")

    def test_a_base_model_open_webui_does_not_label_local_is_a_warning(self) -> None:
        self.owui.base_models.append({"id": "gpt-5.1", "name": "gpt-5.1", "object": "model", "owned_by": "openai",
                                      "connection_type": "external"})
        self.owui.add_workspace_model("kate", system=KATE_PROMPT, base_model_id="gpt-5.1")
        chat = self.manager.new_chat(None, persona="kate")
        self.assertEqual(chat["persona"]["base_model"],
                         {"id": "gpt-5.1", "listed": True, "owned_by": "openai", "connection_type": "external"})
        [warning] = chat["persona"]["warnings"]
        self.assertIn("labels the connection of Kate's base model 'gpt-5.1' external, owned by openai", warning)
        self.assertIn("set its connection type to Local", warning)
        done = self.turn(chat, "hello")
        self.assertEqual(done["status"], "idle", "a warning, not a refusal")
        log = (self.tmp / "data" / "chats" / chat["id"] / "logs" / "01-turn.jsonl").read_text()
        self.assertIn("base model 'gpt-5.1' external", log)
        check = run_doctor(self.make_config())["persona kate"]
        self.assertEqual((check["ok"], check["level"]), (False, "warn"), check)
        self.assertIn("whose connection Open WebUI labels external", check["detail"])
        self.owui.base_models = [model for model in self.owui.base_models if model["id"] != "gpt-5.1"]
        check = run_doctor(self.make_config())["persona kate"]
        self.assertEqual(check["level"], "warn")
        self.assertIn("does not list Kate's base model 'gpt-5.1'", check["detail"])
        self.assertIn("does not list Kate's base model", self.manager.new_chat(None, persona="kate")["persona"]["warnings"][0])

    def test_knowledge_only_when_the_persona_file_turns_it_on(self) -> None:
        self.user_agent("kate", "openwebui: kate\nmodel: kate\ntools: none\nknowledge: true\n")
        chat = self.manager.new_chat(None, persona="kate")
        self.assertEqual(self.turn(chat, "hello")["status"], "idle")
        self.assertEqual(self.owui.requests[-1]["files"], [{"type": "collection", "id": "kb-1"}])

    def test_when_open_webui_cannot_be_read_for_a_turn(self) -> None:
        chat = self.manager.new_chat(None, persona="kate")
        with mock.patch.object(personas.OpenWebUI, "models", side_effect=personas.OpenWebUIError("down")):
            done = self.turn(chat, "hello")
        self.assertEqual(done["status"], "idle")
        self.assertEqual(done["persona"]["turns"][-1]["version_id"], None)
        log = (self.tmp / "data" / "chats" / chat["id"] / "logs" / "01-turn.jsonl").read_text()
        self.assertIn("could not read the live binding (down)", log)

    def test_refusals(self) -> None:
        repo = make_repo(self.tmp, "repo")
        config = self.make_config(repos=[{"name": "app", "path": str(repo)}])
        manager = ChatManager(config, self.bus, _Factory())
        with self.assertRaisesRegex(ValueError, "no repository"):
            manager.new_chat("app", persona="kate")
        with self.assertRaisesRegex(ValueError, "is a persona: its chats have no repository"):
            manager.new_chat("app", mode="repo", agent="kate")
        with self.assertRaisesRegex(ValueError, "not a persona"):
            manager.new_chat(None, persona="coder")
        with self.assertRaisesRegex(ValueError, "either agent or persona"):
            manager.new_chat(None, agent="analyst", persona="kate")
        self.user_agent("seraphina", "wake: true\n")
        with self.assertRaisesRegex(ValueError, "wakes itself"):
            manager.new_chat(None, persona="seraphina")
        with self.assertRaisesRegex(ValueError, "not on this machine"):
            manager.new_chat(None, persona="kate", model="opus")
        cli = ChatManager(self.make_config(agents={"*": {"driver": "claude"}}), self.bus, _Factory())
        with self.assertRaisesRegex(ValueError, "builtin harness only"):
            cli.new_chat(None, persona="kate")
        bare = ChatManager(Config(self.tmp / "bare.json", {"data_dir": str(self.tmp / "bare")}), self.bus, _Factory())
        with self.assertRaisesRegex(ValueError, "models.kate, which is not configured.*openwebui preset"):
            bare.new_chat(None, persona="kate")
        self.assertEqual(manager.chats(), [])
        self.assertEqual(self.owui.requests, [])

    def test_a_turn_never_runs_once_the_route_leaves_the_machine(self) -> None:
        chat = self.manager.new_chat(None, persona="kate")
        self.manager.config = self.make_config(models={"kate": {"provider": "deepseek", "model": "kate"}})
        done = self.turn(chat, "hello")
        self.assertEqual(done["status"], "error")
        self.assertIn("not on this machine", done["error"])
        self.assertEqual(self.owui.requests, [], "nothing was sent")

    def test_an_agent_that_is_a_persona_becomes_a_persona_chat(self) -> None:
        chat = self.manager.new_chat(None, agent="kate")
        self.assertEqual(chat["persona"]["type"], "openwebui")
        self.user_agent("kate", "openwebui: kate\ntools: none\n")   # no model: the one entry for kate is found
        self.assertEqual(self.manager.new_chat(None, persona="kate")["model"], "kate")

    def test_an_offline_snapshot_runs_on_the_harness_s_own_model(self) -> None:
        with MockLLM([{"text": "Offline Kate here."}]) as llm:
            local = {"local": {"type": "openai", "base_url": llm.base_url, "api_key_env": None}}
            models = {"flash": {"provider": "local", "model": "qwen3.8-flash", "max_tokens": 1024}}
            config = self.make_config(providers=local, models=models)
            config.save()
            config = Config.load(config.path)
            personas.import_profile(config, records=self.owui.export_file("kate"),
                                    snapshot={"name": "kate-offline", "user_name": "Sam"})
            config = Config.load(config.path)
            manager = ChatManager(config, self.bus, _Factory())
            self.addCleanup(manager.stop)
            chat = manager.new_chat(None, persona="kate-offline")
            self.assertEqual((chat["persona"]["type"], chat["persona"]["stale"]), ("snapshot", None))
            self.assertTrue(chat["persona"]["label"].startswith("Kate, imported snapshot of "))
            manager.chat_send(chat["id"], "hi")
            done = wait_for(lambda: (lambda c: c if c["status"] != "running" else None)(manager.chat(chat["id"])))
            self.assertEqual((done["status"], done["messages"][-1]["text"]), ("idle", "Offline Kate here."), done["error"])
            sent = llm.requests[0]
            system = sent["messages"][0]["content"]
            self.assertTrue(system.startswith("You are Kate. KATE-MARKER. Today is 20"), system)
            self.assertIn("Europe/London). Hello Sam. Answer in en-GB. Near {{USER_LOCATION}}.", system)
            self.assertNotIn("Software Factory", system)
            self.assertEqual((sent["temperature"], sent["top_k"], sent["max_tokens"]), (0.6, 20, 4096),
                             "the preset's settings, with its own max_tokens")
            for key in ("variables", "files", "filter_ids"):
                self.assertNotIn(key, sent)
            self.assertEqual(self.owui.requests, [], "an offline snapshot never calls Open WebUI's chat")
            self.owui.edit("kate", system="New Kate.")
            llm.script.append({"text": "Still offline."})
            manager.chat_send(chat["id"], "again")
            done = wait_for(lambda: (lambda c: c if c["status"] != "running" else None)(manager.chat(chat["id"])))
            self.assertIn("version", done["persona"]["stale"])


class TurnRefusalTest(PersonaTestCase):
    """A chat refuses, on every turn and not only at its start, an agent whose file has since become a persona."""

    def test_an_agent_that_became_a_persona_is_refused_on_its_next_turn(self) -> None:
        self.user_agent("helper", "tools: Read\n", body="You help.")
        with MockLLM([{"text": "first"}, {"text": "never"}]) as llm:
            config = self.make_config(providers={"elsewhere": {"type": "openai", "base_url": llm.base_url,
                                                               "api_key_env": None}},
                                      models={"m": {"provider": "elsewhere", "model": "mock-model"}})
            manager = ChatManager(config, EventBus(), _Factory())
            self.addCleanup(manager.stop)
            chat = manager.new_chat(None, model="m", mode="none", agent="helper")
            self.assertEqual(finished_turn(manager, chat, "one")["status"], "idle")
            self.user_agent("helper", "wake: true\nprivacy: private\ntools: Read\n", body="SOUL: private notes.")
            with self.assertRaisesRegex(ValueError, "wakes itself"):
                manager.new_chat(None, model="m", mode="none", agent="helper")
            done = finished_turn(manager, chat, "two")
            self.assertEqual(done["status"], "error")
            self.assertIn("agent 'helper' is now a persona that wakes itself", done["error"])
            self.user_agent("helper", "openwebui: kate\ntools: Read\n", body="You help.")
            done = finished_turn(manager, chat, "three")
            self.assertIn("agent 'helper' is now a persona: its chats run only as persona chats", done["error"])
            self.assertEqual(len(llm.requests), 1, "nothing reached the model after the change")
            self.assertNotIn("SOUL", json.dumps([request.body for request in llm.requests]))


class DriftTest(PersonaTestCase):
    def info(self) -> dict:
        return personas.listed(self.owui.listing(), "kate")["info"]

    def api(self) -> personas.OpenWebUI:
        return personas.client(self.make_config(), provider="openwebui")

    def check(self, **kwargs: Any) -> dict:
        return personas.check_drift(self.make_config(), "kate", self.info(), name="Kate", provider="openwebui",
                                    api=self.api(), **kwargs)

    def test_new_none_cosmetic_and_behaviour(self) -> None:
        self.assertEqual(self.check()["kind"], "new")
        self.assertEqual(self.check()["kind"], "none")
        self.assertEqual(self.system_items(), [])
        self.owui.edit("kate", meta={"description": "Kate, kinder", "tags": []}, name="Kate!")
        result = self.check()
        self.assertEqual((result["kind"], result["changed"], result["item"]), ("cosmetic", True, None))
        self.owui.edit("kate", params={"temperature": 0.9})
        result = self.check()
        self.assertEqual((result["kind"], result["item"]), ("behaviour", "system:S-1"))
        self.owui.edit("kate", system="Different.")
        self.assertEqual(self.check()["item"], "system:S-1", "still open: the same item")
        state = personas.drift_state(self.make_config(), "kate")
        self.assertEqual([entry["kind"] for entry in state["history"]], ["cosmetic", "behaviour", "behaviour"])
        path = self.tmp / "data" / "personas" / "openwebui" / "kate.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        store = tracker.system_store(self.tmp / "data")
        with store.transaction() as ledger:
            ledger.change(tracker.HUMAN, "S-1", "verified", note="re-imported")
        self.owui.edit("kate", params={"temperature": 1.0})
        self.assertEqual(self.check()["item"], "system:S-2", "after the owner looked, a new change is a new item")

    def test_a_read_only_key_cannot_tell_cosmetic_from_behaviour(self) -> None:
        with mock.patch.dict(os.environ, {"OPENWEBUI_API_KEY": READER_KEY}):
            self.check()
            self.owui.edit("kate", meta={"description": "only words"})
            result = self.check()
            self.assertEqual(result["kind"], "unknown")
            self.assertIn("may be only a cosmetic edit", self.system_items()[0]["detail"])
            self.owui.edit("kate", meta={"toolIds": ["calendar"]})
            self.assertEqual(self.check()["kind"], "behaviour", "the live fingerprint sees tools change")

    def test_the_baseline_comes_from_the_profile_card_and_the_doctor_writes_nothing(self) -> None:
        personas.import_profile(self.make_config(), records=self.owui.export_file("kate"))
        self.assertEqual(personas.check_drift(self.make_config(), "kate", self.info(), write=False)["kind"], "none")
        self.owui.edit("kate", system="Changed.")
        peek = personas.check_drift(self.make_config(), "kate", self.info(), write=False)
        self.assertEqual(peek["kind"], "unknown")
        self.assertIsNone(personas.drift_state(self.make_config(), "kate"))
        self.assertEqual(self.system_items(), [])
        self.assertEqual(self.check()["kind"], "behaviour", "the owner's key exports it to compare")


class CardBaselineTest(PersonaTestCase):
    """Import a card, then look at the live preset with nothing edited: no drift, though the listing shows
    model controls reduced and the admin's Model Defaults metadata merged in, which the export never has."""

    CONTROLS = {"reasoning": {"label": "Reasoning", "default": "off", "options": {
        "on": {"label": "On", "params": {"chat_template_kwargs": {"enable_thinking": True}}},
        "off": {"label": "Off", "params": {"chat_template_kwargs": {"enable_thinking": False}}}}}}

    def setUp(self) -> None:
        super().setUp()
        self.owui.default_metadata = {"capabilities": {"vision": True, "file_context": True, "usage": True}}
        params = {**KATE_PARAMS, "custom_params": {"top_k": "20", "chat_template_kwargs": {"x": 1}},
                  "model_controls": self.CONTROLS}
        self.kate = self.owui.add_workspace_model("kate", system=KATE_PROMPT, params=params, meta=KATE_META)

    def info(self) -> dict:
        return personas.listed(personas.client(self.make_config(), provider="openwebui").models(), "kate")["info"]

    def test_the_listing_is_not_the_export(self) -> None:
        info = self.info()
        self.assertEqual(info["params"]["model_controls"]["reasoning"]["options"]["on"], {"label": "On"})
        self.assertEqual(info["meta"]["capabilities"], {"vision": False, "file_context": True, "usage": True})
        record = self.owui.export_file("kate")[0]
        self.assertNotEqual(personas.live_fingerprint(record), personas.live_fingerprint(info),
                            "the export lacks the Model Defaults metadata")
        self.owui.default_metadata = {}
        self.assertEqual(personas.live_fingerprint(record), personas.live_fingerprint(self.info()),
                         "model controls compare as the listing shows them")

    def test_an_unedited_preset_after_an_import_is_no_drift(self) -> None:
        for source in ("api", "export file"):
            with self.subTest(source=source):
                if source == "api":
                    personas.import_profile(self.make_config(), model_id="kate")
                else:
                    personas.import_profile(self.make_config(), records=self.owui.export_file("kate"))
                self.assertEqual(personas.check_drift(self.make_config(), "kate", self.info(), write=False)["kind"],
                                 "none")
        doctor_check = run_doctor(self.make_config())["persona kate"]
        self.assertEqual((doctor_check["ok"], doctor_check["level"]), (True, "info"), doctor_check)
        manager = ChatManager(self.make_config(), EventBus(), _Factory())
        self.addCleanup(manager.stop)
        chat = manager.new_chat(None, persona="kate")
        self.assertEqual((chat["persona"]["drift"]["kind"], chat["persona"]["drift"]["item"]), ("none", None))
        self.assertEqual(self.system_items(), [])
        state = personas.drift_state(self.make_config(), "kate")
        self.assertEqual(state["live_fingerprint"], personas.live_fingerprint(self.info()), "the listing is the baseline")
        result = personas.check_drift(self.make_config(), "kate", self.info())
        self.assertEqual((result["kind"], result["item"]), ("none", None))

    def test_a_changed_version_against_the_card_is_still_drift(self) -> None:
        personas.import_profile(self.make_config(), model_id="kate")
        self.owui.edit("kate", meta={"description": "kinder words"})
        api = personas.client(self.make_config(), provider="openwebui")
        self.assertEqual(personas.check_drift(self.make_config(), "kate", self.info(), api=api)["kind"], "cosmetic")
        self.owui.edit("kate", params={"temperature": 0.9})
        self.assertEqual(personas.check_drift(self.make_config(), "kate", self.info(), api=api)["kind"], "behaviour")

    def test_model_defaults_that_change_the_listing_without_a_new_version(self) -> None:
        personas.check_drift(self.make_config(), "kate", self.info())
        self.owui.default_metadata = {"capabilities": {"file_context": False}}
        result = personas.check_drift(self.make_config(), "kate", self.info())
        self.assertEqual((result["kind"], result["version_id"]), ("behaviour", self.kate["version_id"]))
        self.assertIn(f"version {self.kate['version_id'][:12]} is unchanged, but Open WebUI now lists",
                      self.system_items()[0]["detail"])

    def test_model_control_defaults_are_in_the_settings_and_reach_the_model(self) -> None:
        card = personas.import_profile(self.make_config(), model_id="kate")["card"]
        self.assertEqual(card["settings"]["chat_template_kwargs"], {"x": 1, "enable_thinking": False},
                         "the default option deep-merged over the preset's custom_params")
        self.assertIn("model controls (reasoning): the settings include each control's default option",
                      " ".join(card["notes"]))
        model = self.make_config().model("kate")
        OpenAIProvider(self.make_config().provider("openwebui")).complete(model, "s", [{"role": "user", "content": "hi"}], [])
        upstream = self.owui.upstream[-1]
        self.assertEqual((upstream["chat_template_kwargs"], upstream["stop"]),
                         ({"x": 1, "enable_thinking": False}, ["\\n\\nUser:"]))
        self.assertEqual({key: upstream[key] for key in card["settings"]}, card["settings"],
                         "the card's settings are what Open WebUI sends upstream")


class OpenWebUIContractTest(PersonaTestCase):
    """What the harness assumes about Open WebUI 0.12.3 (research §11), pinned against the mock that mimics it.
    Re-run these assumptions against the owner's Open WebUI after every upgrade (DESIGN §33)."""

    def provider(self, key: str = OWNER_KEY) -> OpenAIProvider:
        cfg = self.make_config().provider("openwebui")
        return OpenAIProvider(cfg)

    def model(self, **extra: Any) -> Any:
        model = self.make_config().model("kate")
        model.extra_body = {**model.extra_body, **extra}
        return model

    def test_1_the_listing_needs_the_key_and_shows_no_prompt(self) -> None:
        api = self.api = personas.client(self.make_config(), provider="openwebui")
        entry = personas.listed(api.models(), "kate")
        self.assertTrue(entry["preset"])
        self.assertEqual(entry["info"]["version_id"], self.owui.records["kate"]["version_id"])
        self.assertNotIn("params", entry["info"])
        self.assertNotIn("KATE-MARKER", json.dumps(entry))
        self.assertEqual(api.get("/models", authenticated=False)[0], 401)
        self.assertEqual(settings.list_models(self.make_config().provider("openwebui"))[1][-1], "kate")

    def test_2_the_preset_s_prompt_comes_first_and_the_caller_s_is_kept(self) -> None:
        self.owui.reply = lambda body: {"text": body["messages"][0]["content"]}
        result = self.provider().complete(self.model(), "HARNESS-NOTE", [{"role": "user", "content": "hi"}], [])
        self.assertTrue(result.text.startswith("You are Kate. KATE-MARKER."), result.text)
        self.assertIn("Hello Owner.", result.text, "{{USER_NAME}} is the key's user")
        self.assertTrue(result.text.endswith("\nHARNESS-NOTE"))
        self.assertEqual(sum(1 for m in self.owui.upstream[0]["messages"] if m["role"] == "system"), 1)

    def test_3_the_caller_s_tools_come_back_unexecuted(self) -> None:
        self.owui.reply = lambda body: {"tool_calls": [{"name": "Read", "arguments": {"file_path": "a"}}]}
        tools = [{"name": "Read", "description": "Read.", "input_schema": {"type": "object", "properties": {}}}]
        result = self.provider().complete(self.model(), "s", [{"role": "user", "content": "read a"}], tools)
        self.assertEqual(([c.name for c in result.tool_calls], result.stop), (["Read"], "tool_use"))
        self.assertEqual(self.owui.upstream[0]["tools"][0]["function"]["name"], "Read")

    def raw(self, **body: Any) -> tuple[str, str]:
        """(content type, body text) of one POST /api/chat/completions, read as it comes (no proxy)."""
        payload = {"model": "kate", "messages": [{"role": "user", "content": "hi"}], **body}
        request = urllib.request.Request(self.owui.base_url + "/api/chat/completions", method="POST",
                                         data=json.dumps(payload).encode(),
                                         headers={"Authorization": f"Bearer {OWNER_KEY}",
                                                  "Content-Type": "application/json"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=WAIT) as response:
            return response.headers.get_content_type(), response.read().decode()

    @staticmethod
    def frames(text: str) -> list[Any]:
        return [json.loads(line[5:]) if line[5:].strip() != "[DONE]" else "[DONE]"
                for line in text.splitlines() if line.startswith("data:")]

    def test_4_knowledge_files_begin_the_stream_with_a_sources_frame(self) -> None:
        files = [{"type": "collection", "id": "kb-1"}]
        result = self.provider().complete(self.model(files=files), "s", [{"role": "user", "content": "hi"}], [])
        self.assertEqual(result.text, "ECHO hi", "the sources frame is skipped")
        self.assertEqual(self.owui.requests[-1]["files"], files, "the request carried the files")
        kind, text = self.raw(stream=True, files=files)
        frames = self.frames(text)
        self.assertEqual((kind, list(frames[0])), ("text/event-stream", ["sources"]))
        self.assertEqual(frames[0]["sources"][0]["source"], {"id": "kb-1", "type": "collection"})
        self.assertIn("choices", frames[1])
        self.assertNotIn("sources", self.frames(self.raw(stream=True)[1])[0], "no files, no sources frame")

    def test_5_stream_unless_the_preset_forces_otherwise(self) -> None:
        self.assertEqual(self.raw(stream=True)[0], "text/event-stream")
        self.assertEqual(self.provider().complete(self.model(), "s", [{"role": "user", "content": "a"}], []).text,
                         "ECHO a")
        self.owui.edit("kate", params={"stream_response": False})
        kind, text = self.raw(stream=True)
        self.assertEqual((kind, json.loads(text)["choices"][0]["message"]["content"]), ("application/json", "ECHO hi"),
                         "Stream Chat Response off: one whole JSON answer to a stream request")
        self.assertEqual(self.provider().complete(self.model(), "s", [{"role": "user", "content": "b"}], []).text,
                         "ECHO b")
        self.assertIs(self.owui.upstream[-1]["stream"], False)
        self.owui.edit("kate", params={"stream_response": True})
        self.assertEqual(self.raw(stream=False)[0], "text/event-stream", "on: a stream to a request without one")
        self.assertEqual(self.provider().complete(self.model(stream=False), "s",
                                                  [{"role": "user", "content": "c"}], []).text, "ECHO c")

    def test_6_the_export_needs_write_access(self) -> None:
        api = personas.client(self.make_config(), provider="openwebui")
        records = api.export("kate")
        self.assertEqual((len(records), records[0]["params"]["system"]), (1, KATE_PROMPT))
        with mock.patch.dict(os.environ, {"OPENWEBUI_API_KEY": READER_KEY}):
            with self.assertRaises(personas.OpenWebUIError) as caught:
                api.export("kate")
        self.assertEqual(caught.exception.status, 403)
        self.assertIsNotNone(api.default_params())
        with mock.patch.dict(os.environ, {"OPENWEBUI_API_KEY": READER_KEY}):
            self.assertIsNone(api.default_params(), "the global defaults are an admin's")

    def test_the_api_root_from_any_base_url(self) -> None:
        for url in ("http://127.0.0.1:8083/api", "http://127.0.0.1:8083/api/", "http://127.0.0.1:8083/api/v1",
                    "http://127.0.0.1:8083"):
            with self.subTest(url=url):
                self.assertEqual(personas.api_root(url), "http://127.0.0.1:8083/api")


def run_doctor(config: Config) -> dict[str, dict]:
    """The doctor's checks by name (a token set, so the server check passes)."""
    config.raw.setdefault("server", {})["token"] = "t" * 32
    return {c["name"]: c for c in doctor.run_checks(config)}


class DoctorTest(PersonaTestCase):
    def run_doctor(self, config: Config) -> dict[str, dict]:
        return run_doctor(config)

    def test_open_webui_and_kate(self) -> None:
        checks = self.run_doctor(self.make_config())
        self.assertEqual(checks["openwebui openwebui reachable"]["ok"], True)
        self.assertIn("Open WebUI 0.12.3", checks["openwebui openwebui reachable"]["detail"])
        self.assertIn("OPENWEBUI_API_KEY accepted; without a key Open WebUI answers HTTP 401",
                      checks["openwebui openwebui key"]["detail"])
        self.assertIn("kate (Kate, version", checks["openwebui openwebui models"]["detail"])
        kate = checks["persona kate"]
        self.assertEqual((kate["ok"], kate["level"]), (True, "info"), kate)
        self.assertIn("not through the API: knowledge, tools, actions", kate["detail"])
        self.assertIn("connection Open WebUI labels local", kate["detail"])
        self.assertIsNone(personas.drift_state(self.make_config(), "kate"), "the doctor writes no drift state")

    def test_what_goes_wrong(self) -> None:
        with mock.patch.dict(os.environ, {"OPENWEBUI_API_KEY": "sk-wrong-0123456789"}):
            checks = self.run_doctor(self.make_config())
        self.assertEqual((checks["openwebui openwebui key"]["ok"], checks["openwebui openwebui key"]["level"]),
                         (False, "error"))
        del self.owui.records["kate"]
        checks = self.run_doctor(self.make_config())
        self.assertEqual((checks["persona kate"]["ok"], checks["persona kate"]["level"]), (False, "error"))
        self.assertIn("does not list 'kate'", checks["persona kate"]["detail"])
        config = self.make_config()
        self.owui.stop()
        checks = self.run_doctor(config)
        self.assertEqual(checks["openwebui openwebui reachable"]["level"], "warn")
        bare = Config(self.tmp / "bare.json", {"data_dir": str(self.tmp / "bare")})
        checks = self.run_doctor(bare)
        self.assertEqual((checks["persona kate"]["ok"], checks["persona kate"]["level"]), (True, "info"))
        self.assertIn("not set up (optional): apply the openwebui preset", checks["persona kate"]["detail"])

    def test_a_snapshot_without_open_webui_is_only_checked_for_staying_local(self) -> None:
        self.user_agent("kate-offline", "source: openwebui\nsource_model: kate\noffline: true\nmodel: flash\n"
                                        "tools: none\n")
        local = {"llamacpp": {"type": "openai", "base_url": "http://127.0.0.1:9/v1", "api_key_env": None}}
        bare = Config(self.tmp / "bare.json", {"data_dir": str(self.tmp / "bare"), "providers": local,
                                               "models": {"flash": {"provider": "llamacpp", "model": "q"}}})
        check = self.run_doctor(bare)["persona kate-offline"]
        self.assertEqual((check["ok"], check["level"]), (True, "info"), check)
        self.assertIn("no Open WebUI provider is configured", check["detail"])
        hosted = Config(self.tmp / "hosted.json", {"data_dir": str(self.tmp / "bare"),
                                                   "models": {"flash": {"provider": "deepseek", "model": "q"}}})
        check = self.run_doctor(hosted)["persona kate-offline"]
        self.assertEqual((check["ok"], check["level"]), (False, "error"))
        self.assertIn("not on this machine", check["detail"])

    def test_a_changed_preset_is_a_warning(self) -> None:
        personas.check_drift(self.make_config(), "kate", personas.listed(self.owui.listing(), "kate")["info"])
        self.owui.edit("kate", meta={"toolIds": []})
        kate = self.run_doctor(self.make_config())["persona kate"]
        self.assertEqual((kate["ok"], kate["level"]), (False, "warn"))
        self.assertIn("changed in Open WebUI since the last check (behaviour)", kate["detail"])


class PresetTest(unittest.TestCase):
    def test_the_openwebui_preset(self) -> None:
        preset = settings.load_preset("openwebui")
        provider = preset["providers"]["openwebui"]
        self.assertEqual((provider["base_url"], provider["api_key_env"], provider["max_concurrency"], provider["kind"]),
                         ("http://127.0.0.1:8083/api", personas.OPENWEBUI_KEY_ENV, 1, "openwebui"))
        model = preset["models"]["kate"]
        self.assertEqual((model["provider"], model["model"], model["extra_body"]), ("openwebui", "kate", {"max_tokens": None}))
        self.assertTrue(settings.is_local_url(provider["base_url"]))


from tests.test_server import RealFactoryCase  # noqa: E402  (the server's own fixtures)


class RoutesTest(RealFactoryCase):
    """The GUI's routes (DESIGN §15 interface notes, §33) in front of a real, not started Factory."""

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"OPENWEBUI_API_KEY": OWNER_KEY, "TZ": "UTC"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.owui = MockOpenWebUI()
        self.owui.start()
        self.addCleanup(self.owui.stop)
        self.owui.add_workspace_model("kate", system=KATE_PROMPT, params=KATE_PARAMS, meta=KATE_META)
        preset = settings.load_preset("openwebui")
        provider = dict(preset["providers"]["openwebui"], base_url=self.owui.base_url + "/api")
        response = self.request("POST", "/api/config", {"providers": {"openwebui": provider}, "models": preset["models"]})
        self.assertEqual(response.status, 200, response.body)

    def test_personas_cards_import_check_and_a_chat(self) -> None:
        listing = self.request("GET", "/api/personas").json()["personas"]
        kate = next(row for row in listing if row["name"] == "kate")
        self.assertEqual((kate["kind"], kate["model"], kate["card"], kate["provider"], kate["drift"]),
                         ("openwebui", "kate", False, "openwebui", None))
        self.assertNotIn("KATE-MARKER", json.dumps(listing))
        detail = self.request("GET", "/api/personas/kate").json()
        self.assertEqual((detail["persona"]["name"], detail["card"], detail["drift"]), ("kate", None, None))
        page = self.request("GET", "/api/personas/kate/card").json()
        self.assertEqual((page["card"], page["live"]["listed"], page["live"]["version_id"]),
                         (None, True, self.owui.records["kate"]["version_id"]))
        records = self.owui.export_file("kate")
        imported = self.request("POST", "/api/personas/import", {"export": records, "model_id": "kate"})
        self.assertEqual(imported.status, 201, imported.body)
        self.assertEqual(imported.json()["card"]["model_id"], "kate")
        self.assertNotIn("KATE-MARKER", imported.body.decode())
        self.assertEqual(self.request("GET", "/api/personas/kate/card").json()["card"]["version_id"],
                         self.owui.records["kate"]["version_id"])
        from_api = self.request("POST", "/api/personas/import", {"source": "openwebui", "model_id": "kate"})
        self.assertEqual((from_api.status, from_api.json()["card"]["source"]), (201, "api"))
        models = self.request("GET", "/api/openwebui/models").json()
        self.assertEqual((models["provider"], models["models"][0]["id"], models["models"][0]["personas"],
                          models["models"][0]["card"]), ("openwebui", "kate", ["kate"], True))
        check = self.request("POST", "/api/personas/check", {}).json()["checked"]
        self.assertEqual([(row["persona"], row["kind"]) for row in check], [("kate", "none")])
        self.owui.edit("kate", system="Changed.")
        check = self.request("POST", "/api/personas/check", {"persona": "kate"}).json()["checked"]
        self.assertEqual((check[0]["kind"], check[0]["item"]), ("behaviour", "system:S-1"))
        system = self.request("GET", "/api/items?scope=system").json()["items"]
        self.assertEqual([row["title"] for row in system], ["Kate changed in Open WebUI"])
        chat = self.request("POST", "/api/chats", {"persona": "kate"})
        self.assertEqual(chat.status, 201, chat.body)
        body = chat.json()["chat"]
        self.assertEqual((body["agent"], body["mode"], body["repo"], body["persona"]["type"]),
                         ("kate", "none", None, "openwebui"))
        self.assertEqual(self.runner_requests, [])

    def test_refusals(self) -> None:
        self.assert_error(self.request("GET", "/api/personas/nobody"), 404, "unknown persona")
        self.assert_error(self.request("GET", "/api/personas/Bad_Name"), 400, "persona name")
        self.assert_error(self.request("GET", "/api/personas/coder/card"), 404)
        self.assert_error(self.request("POST", "/api/personas/import", {}), 400, "either export")
        self.assert_error(self.request("POST", "/api/personas/import", {"export": [], "source": "openwebui"}), 400)
        self.assert_error(self.request("POST", "/api/personas/import", {"export": "x"}), 400, "JSON array")
        self.assert_error(self.request("POST", "/api/personas/import", {"export": [], "colour": 1}), 400, "unknown field")
        self.assert_error(self.request("POST", "/api/personas/import", {"export": [], "snapshot": True}), 400,
                          "snapshot must be an object")
        self.assert_error(self.request("POST", "/api/chats", {"persona": "kate", "repo": "repo"}), 400, "no repository")
        self.assert_error(self.request("POST", "/api/chats", {"persona": "kate", "agent": "coder"}), 400, "not both")
        self.assert_error(self.request("POST", "/api/chats", {"persona": "kate", "mode": "repo"}), 400, "mode 'none'")
        with mock.patch.dict(os.environ, {"OPENWEBUI_API_KEY": READER_KEY}):
            self.assert_error(self.request("POST", "/api/personas/import", {"source": "openwebui", "model_id": "kate"}),
                              502, "does not let this key export")
        self.owui.stop()
        self.assert_error(self.request("GET", "/api/openwebui/models"), 502, "cannot reach")
        self.assert_error(self.request("GET", "/api/openwebui/models?provider=bad/name"), 400, "provider")
        self.assert_error(self.request("POST", "/api/chats", {"persona": "kate"}), 400, "cannot reach")

    def test_a_missing_key_is_the_harness_s_setup_not_open_webui_s_answer(self) -> None:
        from swfactory.server import error_status

        with mock.patch.dict(os.environ, {"OPENWEBUI_API_KEY": ""}):
            self.assert_error(self.request("GET", "/api/openwebui/models"), 400, "OPENWEBUI_API_KEY is not set")
            self.assert_error(self.request("POST", "/api/personas/import", {"source": "openwebui", "model_id": "kate"}),
                              400, "OPENWEBUI_API_KEY is not set")
        self.assertEqual(self.owui.gets, [], "Open WebUI was never asked")
        self.assertEqual(error_status(personas.OpenWebUISetupError("x")), 400)
        self.assertEqual(error_status(personas.OpenWebUIError("x", 403)), 502)


if __name__ == "__main__":
    unittest.main()
