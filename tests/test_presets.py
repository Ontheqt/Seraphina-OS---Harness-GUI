from __future__ import annotations

import json
import stat
import unittest
from typing import Any
from unittest import mock

from swfactory import routing, settings
from swfactory.config import Config
from tests.helpers import make_repo
from tests.test_settings import TOKEN, SettingsTestCase

PRESETS = ["anthropic", "deepseek", "gpt-oss", "hybrid", "local-first", "mixed", "nanogpt", "nanogpt-value", "ollama",
           "strata"]   # routing
ADD_ONS = ["rea", "browser"]   # presets that add sections (MCP servers) and route nothing
PROVIDER_ADD_ONS = ["openwebui"]   # presets that add a provider and its model entries and route nothing (DESIGN §33)
FACTORY_AGENTS = ["spec-writer", "builder", "approver", "reviewer-security", "reviewer-ux", "reviewer-ui",
                  "reviewer-code", "lead", "coder", "repo-scout"]
BETA = "server-side-fallback-2026-07-01"
PRICE_FILE = settings.PRESETS_DIR / "prices" / "nanogpt-2026-10-21.json"
PROFILES = ["balanced", "best", "fastest", "local-first", "thrifty"]
GATES = ("spec-writer", "approver", "lead")
REVIEWERS = ("reviewer-security", "reviewer-code", "reviewer-ux", "reviewer-ui")


def walk(value: Any):
    """Every (key, value) pair in nested dicts."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield key, item
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)


class PresetFilesTest(SettingsTestCase):
    def test_the_shipped_presets(self) -> None:
        self.assertEqual(settings.list_presets(), sorted(PRESETS + ADD_ONS + PROVIDER_ADD_ONS))

    def test_every_preset_describes_itself(self) -> None:
        for name in PRESETS + ADD_ONS + PROVIDER_ADD_ONS:
            with self.subTest(preset=name):
                description = settings.load_preset(name).get("description")
                self.assertIsInstance(description, str)
                self.assertGreater(len(description.strip()), 20)

    def test_add_ons_route_nothing(self) -> None:
        for name in ADD_ONS:
            with self.subTest(preset=name):
                data = settings.load_preset(name)
                self.assertFalse({"providers", "models", "agents"} & set(data), "an add-on routes nothing")
                self.assertTrue(set(data) - {"description"}, "an add-on adds something")

    def test_provider_add_ons_add_a_provider_and_its_models_only(self) -> None:
        for name in PROVIDER_ADD_ONS:
            with self.subTest(preset=name):
                data = settings.load_preset(name)
                self.assertEqual(set(data), {"description", "providers", "models"})
                self.assertFalse(set(data["models"]) & {"opus", "sonnet", "haiku"}, "it changes no tier")
                self.assertLessEqual({entry["provider"] for entry in data["models"].values()}, set(data["providers"]))
                for provider in data["providers"].values():
                    self.assertTrue(settings.is_local_url(provider["base_url"]))
                    self.assertEqual((provider["max_concurrency"], provider["concurrency_scope"]), (1, "run"))

    def test_files_are_plain_json_fragments(self) -> None:
        for name in PRESETS:
            with self.subTest(preset=name):
                data = json.loads((settings.PRESETS_DIR / f"{name}.json").read_text(encoding="utf-8"))
                self.assertLessEqual({"description", "providers", "models", "agents"}, set(data))
                self.assertLessEqual(set(data), {"description", "providers", "models", "agents", "profiles",
                                                 "default_profile"})
                self.assertTrue({"opus", "sonnet", "haiku"} <= set(data["models"]), "every preset sets the tiers")
                self.assertEqual(set(data["agents"]), {"*", *FACTORY_AGENTS}, "every factory agent is routed")
                self.assertFalse([key for key, _ in walk(data) if str(key).startswith(("//", "#", "_comment"))])
                used = {entry["provider"] for entry in data["models"].values()}
                used |= {build["provider"] for entry in data["models"].values() for build in entry.get("builds", [])}
                self.assertLessEqual(used, set(data["providers"]), "each preset defines the providers it uses")
                overflow = {entry["overflow"] for entry in data["models"].values() if entry.get("overflow")}
                self.assertLessEqual(overflow, set(data["models"]), "an overflow is one of the preset's models")

    def test_each_preset_loads_validates_and_routes_every_agent(self) -> None:
        base = self.make_config()
        for name in PRESETS:
            for how, config in (("as a config file", Config(self.tmp / f"{name}.json", settings.load_preset(name))),
                                ("merged into the defaults", settings.preset_config(base, name))):
                with self.subTest(preset=name, how=how):
                    config.validate()
                    settings.validate_settings(config)
                    agents = settings.resolved_agents(config)
                    for agent_name in FACTORY_AGENTS:
                        model = config.agent_model(agents[agent_name])
                        self.assertIsInstance(config.raw["models"].get(model.key), dict,
                                              f"{agent_name} must run on a model entry, not a literal id")
                        provider = config.provider(model.provider.name)
                        self.assertIn(provider.type, ("openai", "anthropic"))
                        self.assertEqual(config.agent_driver(agent_name), "builtin")

    def test_local_providers_queue_per_run(self) -> None:
        for name in PRESETS:
            for provider_name, provider in settings.load_preset(name)["providers"].items():
                if settings.is_local_url(provider["base_url"]):
                    with self.subTest(preset=name, provider=provider_name):
                        self.assertEqual(provider["concurrency_scope"], "run")
                        self.assertEqual(provider["max_concurrency"], 1)
                        self.assertEqual(provider["timeout"], 1800)

    def test_claude_entries_follow_the_current_api(self) -> None:
        for name in PRESETS:
            preset = settings.load_preset(name)
            for key, entry in preset["models"].items():
                provider = preset["providers"][entry["provider"]]
                if provider["type"] != "anthropic":
                    continue
                with self.subTest(preset=name, model=key):
                    self.assertIn(entry["model"], ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5",
                                                   "claude-fable-5-1"))
                    extra = entry["extra_body"]
                    self.assertEqual(extra["thinking"], {"type": "adaptive", "display": "summarized"})
                    self.assertIn(extra["output_config"]["effort"], ("low", "medium", "high", "xhigh", "max"))
                    self.assertEqual(extra["cache_control"], {"type": "ephemeral"})
                    self.assertEqual((entry["max_tokens"], entry["allow_history_edits"], entry["eager_tools"]),
                                     (64000, False, True))
                    self.assertNotIn("tool_choice", extra)
                    self.assertNotIn("budget_tokens", json.dumps(extra))
                    if provider["base_url"] != "https://api.anthropic.com":
                        # DESIGN §21: no server-side fallback through a third-party gateway (it may reject it)
                        self.assertNotIn("fallbacks", extra)
                        self.assertNotIn("anthropic-beta", provider.get("headers") or {})
                    elif entry["model"] == "claude-haiku-5-5":
                        self.assertNotIn("fallbacks", extra)
                    else:
                        self.assertEqual(extra["fallbacks"], "default")
                        self.assertEqual(provider["headers"]["anthropic-beta"], BETA)

    def test_anthropic_preset(self) -> None:
        preset = settings.load_preset("anthropic")
        self.assertEqual(preset["providers"]["anthropic"]["base_url"], "https://api.anthropic.com")
        models = preset["models"]
        self.assertEqual({tier: (models[tier]["model"], models[tier]["extra_body"]["output_config"]["effort"])
                          for tier in ("opus", "sonnet", "haiku")},
                         {"opus": ("claude-opus-5-5", "high"), "sonnet": ("claude-sonnet-5-5", "medium"),
                          "haiku": ("claude-haiku-5-5", "low")})

    def test_hybrid_matches_the_section_23_table(self) -> None:
        preset = settings.load_preset("hybrid")
        routes = {name: entry["model"] for name, entry in preset["agents"].items() if name != "*"}
        self.assertEqual(routes, {
            "spec-writer": "local", "builder": "local", "reviewer-security": "local", "coder": "local",
            "repo-scout": "local", "reviewer-code": "claude-sonnet", "reviewer-ux": "claude-sonnet",
            "reviewer-ui": "claude-sonnet", "approver": "claude-opus", "lead": "claude-fable"})
        models, providers = preset["models"], preset["providers"]
        self.assertEqual(models["local"]["provider"], "tabby")
        self.assertEqual(models["local"]["context_tokens"], 262144)
        self.assertEqual(providers["tabby"], {"type": "openai", "base_url": "http://127.0.0.1:5000/v1",
                                              "api_key_env": "TABBY_API_KEY", "auth": "both", "max_concurrency": 1,
                                              "concurrency_scope": "run", "timeout": 1800})
        effort = {key: models[key]["extra_body"]["output_config"]["effort"]
                  for key in ("claude-sonnet", "claude-opus", "claude-fable")}
        self.assertEqual(effort, {"claude-sonnet": "medium", "claude-opus": "high", "claude-fable": "high"})
        self.assertEqual((models["claude-sonnet"]["model"], models["claude-opus"]["model"],
                          models["claude-fable"]["model"]), ("claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-1"))
        self.assertEqual(models["opus"], models["local"])
        self.assertEqual(models["sonnet"], models["claude-sonnet"])

    def test_all_local_presets(self) -> None:
        for name, provider, model_id in (("strata", "strata", "strata"), ("ollama", "ollama", "qwen3-coder:30b")):
            with self.subTest(preset=name):
                preset = settings.load_preset(name)
                self.assertEqual({entry["model"] for n, entry in preset["agents"].items() if n != "*"}, {"local"})
                local = preset["models"]["local"]
                self.assertEqual((local["provider"], local["model"], local["context_tokens"]),
                                 (provider, model_id, 262144))
                for tier in ("opus", "sonnet", "haiku"):
                    self.assertEqual(preset["models"][tier], local)
        strata = settings.load_preset("strata")["providers"]["strata"]
        self.assertEqual((strata["base_url"], strata["api_key_env"], strata["api_key_file"]),
                         ("http://127.0.0.1:18080/v1", "STRATA_API_KEY", "/etc/ai-stack/strata.env"))
        self.assertEqual(settings.load_preset("ollama")["providers"]["ollama"]["base_url"], "http://127.0.0.1:11434/v1")

    def test_mixed_fleet(self) -> None:
        preset = settings.load_preset("mixed")
        providers, models = preset["providers"], preset["models"]
        self.assertEqual(providers["llamacpp-qwen"]["base_url"], "http://127.0.0.1:8081/v1")
        self.assertEqual(providers["llamacpp-gemma"]["base_url"], "http://127.0.0.1:8082/v1")
        self.assertEqual((models["qwen-27b"]["model"], models["gemma-4"]["model"]), ("qwen3.8-27b", "gemma-4"))

        def provider_type(agent: str) -> str:
            return providers[models[preset["agents"][agent]["model"]]["provider"]]["type"]

        self.assertEqual(provider_type("reviewer-security"), "anthropic")
        self.assertEqual(provider_type("approver"), "anthropic")
        routes = {name: models[entry["model"]]["provider"] for name, entry in preset["agents"].items() if name != "*"}
        self.assertEqual(routes["builder"], "llamacpp-qwen")
        self.assertEqual({routes["reviewer-ux"], routes["reviewer-ui"]}, {"llamacpp-gemma"})
        self.assertNotEqual(routes["reviewer-code"], routes["builder"], "the code lens is another model family")

    def test_gpt_oss(self) -> None:
        preset = settings.load_preset("gpt-oss")
        gpt = preset["models"]["gpt-oss"]
        self.assertEqual(gpt["model"], "gpt-oss-120b")
        self.assertEqual(gpt["context_tokens"], 131072)
        self.assertEqual(gpt["extra_body"], {"chat_template_kwargs": {"reasoning_effort": "high"}})
        self.assertEqual(preset["providers"][gpt["provider"]]["base_url"], "http://127.0.0.1:8080/v1")
        routes = {name: entry["model"] for name, entry in preset["agents"].items() if name != "*"}
        self.assertEqual({routes["spec-writer"], routes["reviewer-security"], routes["approver"]}, {"gpt-oss"})
        builder = preset["models"][routes["builder"]]
        self.assertFalse(settings.is_local_url(preset["providers"][builder["provider"]]["base_url"]),
                         "the builder runs on an API model while gpt-oss holds the GPUs")

    def test_nanogpt_serves_claude_over_openai_format(self) -> None:
        preset = settings.load_preset("nanogpt")
        self.assertEqual(preset["providers"]["nanogpt"]["type"], "openai")
        self.assertEqual(preset["providers"]["nanogpt"]["base_url"], "https://nano-gpt.com/api/v1")
        for tier in ("opus", "sonnet", "haiku"):
            self.assertEqual(preset["models"][tier]["provider"], "nanogpt")
            # the ids NanoGPT's public model list gives on its OpenAI-format API (read 2026-10-25); the dashed
            # Anthropic-API form is not on it
            self.assertEqual(preset["models"][tier]["model"], f"anthropic/claude-{tier}-5.5")

    def test_value_presets_copy_the_dated_price_file(self) -> None:
        prices = json.loads(PRICE_FILE.read_text(encoding="utf-8"))
        self.assertEqual((prices["date"], prices["currency"], prices["unit"]), ("2026-10-21", "USD", "per million tokens"))
        self.assertIn("factory doctor", prices["description"])
        for name in ("nanogpt-value", "local-first"):
            preset = settings.load_preset(name)
            self.assertIn("factory doctor", preset["description"], "unverified ids say how to check them")
            for key, entry in preset["models"].items():
                if not entry["provider"].startswith("nanogpt"):
                    self.assertNotIn("price_in", entry, "a local model is free by default")
                    continue
                with self.subTest(preset=name, model=key):
                    listed = prices["models"][entry["model"]]
                    self.assertEqual((entry["price_in"], entry["price_out"]), (listed["price_in"], listed["price_out"]))
                    wire = preset["providers"][entry["provider"]]["type"]
                    self.assertEqual(listed["format"], wire, "Claude runs on NanoGPT's Anthropic-format endpoint")

    def test_nanogpt_value_follows_the_placement_list(self) -> None:
        preset = settings.load_preset("nanogpt-value")
        self.assertEqual(preset["providers"]["nanogpt-anthropic"]["base_url"], "https://nano-gpt.com/api")
        self.assertEqual(preset["providers"]["nanogpt-anthropic"]["type"], "anthropic")
        models, agents = preset["models"], preset["agents"]
        self.assertEqual(agents["builder"]["escalate"], ["gpt-sol-high", "gpt-sol-xhigh"])
        self.assertEqual([models[k]["extra_body"]["reasoning_effort"] for k in agents["builder"]["escalate"]],
                         ["high", "xhigh"])
        self.assertEqual(models["mimo"]["model"], "xiaomi/mimo-v2.6-pro")
        self.assertFalse(any(settings.is_local_url(p["base_url"]) for p in preset["providers"].values()), "all API")
        for name in GATES:
            self.assertEqual(models[agents[name]["model"]]["model"], "claude-opus-5-5", name)
        for name, entry in agents.items():
            if name != "*":
                self.assertIn(entry["reason"]["kind"], ("capability", "time", "stage-fit", "cost"), name)
                self.assertTrue(entry["reason"]["text"], name)

    def test_local_first_builds_ladder_overflow_and_swaps(self) -> None:
        preset = settings.load_preset("local-first")
        providers, models, agents = preset["providers"], preset["models"], preset["agents"]
        self.assertEqual({name: providers[name]["base_url"] for name in ("tabby", "strata", "llamacpp")},
                         {"tabby": "http://127.0.0.1:5000/v1", "strata": "http://127.0.0.1:18080/v1",
                          "llamacpp": "http://127.0.0.1:8080/v1"})
        self.assertEqual((providers["tabby"]["kind"], providers["tabby"]["allow_model_swap"]), ("tabby", False))
        flash = models["flash-next"]
        self.assertEqual([build["provider"] for build in flash["builds"]], ["tabby", "strata"], "Tabby EXL3 first")
        self.assertEqual(flash["builds"][0]["model"], flash["model"])
        self.assertEqual(flash["overflow"], "mimo")
        self.assertEqual({agents[name]["model"] for name in ("builder", "coder", "repo-scout", "reviewer-security")},
                         {"flash-next"})
        self.assertEqual(agents["builder"]["escalate"], ["gpt-sol-high", "gpt-sol-xhigh"])
        self.assertEqual((agents["reviewer-code"]["model"], agents["reviewer-ux"]["model"],
                          agents["reviewer-ui"]["model"]), ("gpt-sol-high", "claude-sonnet", "gemini-argon"))
        self.assertEqual(preset["default_profile"], "local-first")
        self.assertEqual(sorted(preset["profiles"]), PROFILES)
        base = {name: entry for name, entry in agents.items() if name != "*"}
        self.assertEqual(preset["profiles"]["local-first"]["agents"], base, "the local-first profile is the base")

    def test_no_profile_economises_the_gates(self) -> None:
        """DESIGN §23: the spec, approver and lead stay on Opus 5.5 at an effective effort (the profile's
        own effort over the model entry's) of high or more, and the four review lenses come from four
        model families, the code lens from another family than the builder's own model (on the builder's
        escalation rungs the independence guard takes over: test_the_independence_guard_on_every_preset)."""
        preset = settings.load_preset("local-first")
        models = preset["models"]

        def family(key: str) -> str:
            return routing.entry_family(models[key])

        for name, profile in preset["profiles"].items():
            with self.subTest(profile=name):
                agents = profile["agents"]
                self.assertTrue(profile["description"].endswith("."))
                for gate in GATES:
                    entry = models[agents[gate]["model"]]
                    self.assertEqual(entry["model"], "claude-opus-5-5", gate)
                    effort = agents[gate].get("effort") or entry["extra_body"]["output_config"]["effort"]
                    self.assertIn(effort, ("high", "xhigh", "max"), f"{gate}'s effective effort")
                lenses = [family(agents[r]["model"]) for r in REVIEWERS]
                self.assertEqual(len(set(lenses)), 4, f"four families: {lenses}")
                self.assertNotEqual(family(agents["reviewer-code"]["model"]), family(agents["builder"]["model"]))
                for agent, entry in agents.items():
                    self.assertIn(entry["reason"]["kind"], ("capability", "time", "stage-fit", "cost"), agent)
                    for rung in entry.get("escalate", []):
                        self.assertIn(rung, models)
                    alternate = entry.get("independent_alternate")
                    if alternate:
                        self.assertIn(agent, (*REVIEWERS, "approver"), "only a judge has an alternate")
                        self.assertNotEqual(family(alternate), family(entry["model"]), f"{agent}: another family")
                        if agent == "approver":   # the merge gate's alternate is never economised either
                            self.assertEqual(models[alternate]["extra_body"]["reasoning_effort"], "xhigh")

    def guard_snapshot(self, preset: str, profile: str = "") -> dict:
        from swfactory.events import EventBus
        from swfactory.orchestrator import Factory

        repo = make_repo(self.tmp, f"app-{preset}-{profile or 'base'}")
        config = settings.preset_config(self.make_config({"repos": [{"name": "app", "path": str(repo)}]}), preset)
        return Factory(config, EventBus()).check_routing("app", profile=profile)

    def guard_map(self, preset: str, profile: str = "") -> list[tuple[str, str, dict, dict, dict]]:
        """routing.independence_map of preset's snapshot under profile ("" none): [(via, builder model,
        {judge: alternate}, {judge: shared model, reduced}, {judge: shared model, accepted})]."""
        return [(row["builder"]["via"], row["builder"]["model"], row["alternate"], row["reduced"], row["accepted"])
                for row in routing.independence_map(self.guard_snapshot(preset, profile))]

    def test_the_independence_guard_on_every_preset(self) -> None:
        """DESIGN §23 ("The independence guard"): for every model the builder can run on (its own, its
        overflow, each escalation rung), which judges move to their independent alternate, which run with
        their independence reduced and which share the builder's family by the routing's choice
        (independent_alternate: false). The docs name each one, so a change here must change them too."""
        security = {"reviewer-security": "flash-next"}   # the local security fine-tune: stage fit, accepted
        code_alt = {"reviewer-code": "muse-spark"}
        local_first = [("route", "flash-next", {}, {}, security), ("overflow", "mimo", {}, {}, {}),
                       ("rung 1", "gpt-sol-high", code_alt, {}, {}), ("rung 2", "gpt-sol-xhigh", code_alt, {}, {})]
        approver = {"approver": "gpt-sol-xhigh"}
        expected = {
            ("local-first", ""): local_first,
            ("local-first", "local-first"): local_first,
            ("local-first", "balanced"): [("route", "flash-next", {"reviewer-security": "muse-spark"}, {}, {}),
                                          ("overflow", "mimo", {}, {}, {}),
                                          ("rung 1", "gpt-sol-high", code_alt, {}, {}),
                                          ("rung 2", "gpt-sol-xhigh", code_alt, {}, {})],
            ("local-first", "best"): [("route", "claude-fable", approver, {}, {}),
                                      ("rung 1", "claude-opus", approver, {}, {}),
                                      ("rung 2", "claude-opus-max", approver, {}, {})],
            ("local-first", "fastest"): [("route", "gpt-5.6-sol", {}, {}, {}), ("rung 1", "gpt-sol-high", {}, {}, {}),
                                         ("rung 2", "gpt-sol-xhigh", {}, {}, {})],
            ("local-first", "thrifty"): [("route", "flash-next", {}, {}, security),
                                         ("overflow, rung 1 overflow, rung 2", "mimo", {}, {}, {}),
                                         ("rung 1", "qwen-27b-q8", {}, {}, security),
                                         ("rung 3", "gpt-sol-high", code_alt, {}, {})],
            ("nanogpt-value", ""): [("route", "muse-spark", {}, {}, {}),
                                    ("rung 1", "gpt-sol-high", {"reviewer-code": "mimo"}, {}, {}),
                                    ("rung 2", "gpt-sol-xhigh", {"reviewer-code": "mimo"}, {}, {})],
            ("gpt-oss", ""): [("route", "deepseek-pro", {"reviewer-code": "gpt-oss"}, {},
                               {"reviewer-ui": "deepseek-flash", "reviewer-ux": "deepseek-flash"})],
            ("hybrid", ""): [("route", "local", {}, {}, {"reviewer-security": "local"})],
            ("mixed", ""): [("route", "qwen-27b", {}, {}, {})],
        }
        for (preset, profile), rows in expected.items():
            with self.subTest(preset=preset, profile=profile):
                self.assertEqual(self.guard_map(preset, profile), rows)
        judges = {"approver": "opus", "reviewer-code": "sonnet", "reviewer-security": "opus", "reviewer-ui": "sonnet",
                  "reviewer-ux": "sonnet"}
        for preset in ("anthropic", "deepseek", "nanogpt"):   # one family: nothing to move to
            with self.subTest(preset=preset):
                self.assertEqual(self.guard_map(preset), [("route", "opus", {}, judges, {})])
        for preset in ("ollama", "strata"):
            with self.subTest(preset=preset):
                self.assertEqual(self.guard_map(preset), [("route", "local", {}, dict.fromkeys(judges, "local"), {})])
        covered = {preset for preset, _ in expected} | {"anthropic", "deepseek", "nanogpt", "ollama", "strata"}
        self.assertEqual(covered, set(PRESETS), "every routing preset is in the table")

    def test_four_lenses_stay_four_families_after_the_guard(self) -> None:
        """Where the docs promise four review families (local-first and its profiles, nanogpt-value), every model
        the builder can run on leaves the four lenses on four models of four families once the guard has moved
        them: an alternate is neither the builder's family nor another lens's model."""
        cases = [("local-first", profile) for profile in ("", *PROFILES)] + [("nanogpt-value", "")]
        for preset, profile in cases:
            snapshot = self.guard_snapshot(preset, profile)
            lenses = {agent: route for agent, route in snapshot.items() if agent.startswith("reviewer-")}
            self.assertEqual(len(lenses), 4)
            for via, placed in routing.builder_placements(snapshot["builder"]):
                with self.subTest(preset=preset, profile=profile, builder=via):
                    families = {routing.route_family(placed): str(placed["model"])}
                    chosen = [routing.judge_choice(route, families)[0] for route in lenses.values()]
                    self.assertEqual(len({route["model"] for route in chosen}), 4, [r["model"] for r in chosen])
                    self.assertEqual(len({routing.route_family(route) for route in chosen}), 4)

    def test_every_preset_alternate_resolves_in_its_snapshot(self) -> None:
        """An alternate is a whole route in the snapshot (another family, the judge's driver, its own
        provider entry), so a job runs on the alternate it was submitted with."""
        from swfactory.events import EventBus
        from swfactory.orchestrator import Factory

        repo = make_repo(self.tmp, "app")
        config = settings.preset_config(self.make_config({"repos": [{"name": "app", "path": str(repo)}]}),
                                        "local-first")
        snapshot = Factory(config, EventBus()).check_routing("app", profile="balanced")
        code = snapshot["reviewer-code"]
        alternate = code["independent_alternate"]
        self.assertEqual((alternate["model"], alternate["model_id"], alternate["family"], alternate["driver"]),
                         ("muse-spark", "meta/muse-spark-1.3", "meta", code["driver"]))
        self.assertEqual((code["family"], snapshot["builder"]["family"]), ("openai", "qwen"))
        self.assertEqual(alternate["provider_entry"]["base_url"], "https://nano-gpt.com/api/v1")
        self.assertNotIn("independent_alternate", snapshot["reviewer-ux"])

    def test_local_first_profiles_route_in_a_snapshot(self) -> None:
        from swfactory.events import EventBus
        from swfactory.orchestrator import Factory

        repo = make_repo(self.tmp, "app")
        config = settings.preset_config(self.make_config({"repos": [{"name": "app", "path": str(repo)}]}), "local-first")
        factory = Factory(config, EventBus())
        builder = factory.check_routing("app")["builder"]   # default_profile local-first
        self.assertEqual([build["provider"] for build in builder["builds"]], ["tabby", "strata"])
        self.assertEqual(builder["overflow"]["model_id"], "xiaomi/mimo-v2.6-pro")
        self.assertEqual([rung["model"] for rung in builder["escalate"]], ["gpt-sol-high", "gpt-sol-xhigh"])
        self.assertEqual(builder["reason"]["kind"], "cost")
        for name in PROFILES:
            with self.subTest(profile=name):
                snapshot = factory.check_routing("app", profile=name)
                self.assertEqual(snapshot["approver"]["model_id"], "claude-opus-5-5")
                for gate in ("spec-writer", "approver"):   # what the snapshot sends, the profile's effort included
                    self.assertIn(snapshot[gate]["extra_body"]["output_config"]["effort"], ("high", "xhigh", "max"))
        best = factory.check_routing("app", profile="best")
        self.assertEqual(best["reviewer-code"]["extra_body"]["reasoning_effort"], "xhigh", "a profile's effort")
        self.assertEqual(best["builder"]["model_id"], "claude-fable-5-1")

    def test_readme_explains_every_preset_and_how_to_apply(self) -> None:
        readme = (settings.PRESETS_DIR / "README.md").read_text(encoding="utf-8")
        for name in PRESETS:
            self.assertIn(f"**{name}**", readme)
        for how in ("factory init --preset", "POST /api/config/preset", "Preset"):
            self.assertIn(how, readme)
        self.assertIn("the `opus` tier follows", readme, "the Strata switch says what happens to the tier")
        self.assertIn("make the same change to\n`models.opus`", readme)


class PresetSummaryTest(SettingsTestCase):
    def test_summaries_name_describe_and_list_what_each_preset_replaces(self) -> None:
        summaries = {entry["name"]: entry for entry in settings.preset_summaries()}
        self.assertEqual(sorted(summaries), sorted(PRESETS + ADD_ONS + PROVIDER_ADD_ONS))
        openwebui = summaries["openwebui"]
        self.assertEqual((openwebui["replaces"], openwebui["merges"], openwebui["default_profile"]),
                         ({"providers": ["openwebui"], "models": ["kate"], "agents": []}, {}, None))
        hybrid = summaries["hybrid"]
        self.assertEqual(set(hybrid), {"name", "description", "replaces", "merges", "default_profile"})
        self.assertIsNone(hybrid["default_profile"])
        self.assertEqual(summaries["local-first"]["default_profile"], "local-first")
        self.assertEqual(summaries["local-first"]["replaces"]["profiles"], PROFILES)
        self.assertEqual(summaries["local-first"]["merges"], {}, "profiles and default_profile are not merged")
        self.assertEqual(hybrid["description"], settings.load_preset("hybrid")["description"])
        self.assertEqual(hybrid["replaces"]["providers"], ["anthropic", "tabby"])
        self.assertIn("claude-opus", hybrid["replaces"]["models"])
        self.assertEqual(hybrid["replaces"]["agents"], sorted(["*", *FACTORY_AGENTS]))
        self.assertEqual(hybrid["merges"], {})
        for name in ADD_ONS:
            add_on = summaries[name]
            self.assertEqual(add_on["replaces"], {"providers": [], "models": [], "agents": []})
            self.assertEqual(add_on["merges"], {section: sorted(value) for section, value in
                                                settings.load_preset(name).items() if section != "description"})

    def test_config_view_lists_summaries_and_names(self) -> None:
        view = settings.config_view(self.make_config())
        self.assertEqual(view["presets"], settings.preset_summaries())
        self.assertEqual(view["preset_names"], sorted(PRESETS + ADD_ONS + PROVIDER_ADD_ONS))

    def test_a_broken_preset_is_listed_with_its_error(self) -> None:
        broken = self.tmp / "presets"
        broken.mkdir()
        (broken / "good.json").write_text('{"description": "Good.", "models": {}}')
        (broken / "bad.json").write_text("{not json")
        with mock.patch.object(settings, "PRESETS_DIR", broken):
            summaries = {entry["name"]: entry for entry in settings.preset_summaries()}
        self.assertEqual(summaries["good"]["description"], "Good.")
        self.assertTrue(summaries["bad"]["description"].startswith("(cannot load:"))
        self.assertEqual(summaries["bad"]["merges"], {})


class ApplyPresetTest(SettingsTestCase):
    def test_the_description_never_lands_in_the_config(self) -> None:
        config = self.make_config()
        for name in PRESETS + ADD_ONS + PROVIDER_ADD_ONS:
            with self.subTest(preset=name):
                self.assertNotIn("description", settings.preset_config(config, name).raw)
        saved = settings.apply_preset(config, "hybrid")
        self.assertNotIn("description", saved.raw)
        self.assertNotIn("description", json.loads(self.config_path.read_text()))

    def test_an_add_on_keeps_the_routing(self) -> None:
        config = settings.apply_preset(self.make_config(), "hybrid")
        for name in ADD_ONS:
            with self.subTest(preset=name):
                added = settings.preset_config(config, name)
                self.assertEqual(added.raw["agents"], config.raw["agents"])
                self.assertEqual(added.raw["models"], config.raw["models"])
                for section, value in settings.load_preset(name).items():
                    if isinstance(value, dict):
                        self.assertLessEqual(set(value), set(added.raw[section]))

    def test_a_provider_add_on_keeps_the_routing_and_validates(self) -> None:
        config = settings.apply_preset(self.make_config(), "local-first")
        added = settings.preset_config(config, "openwebui")
        self.assertEqual((added.raw["agents"], added.raw["profiles"], added.raw["default_profile"]),
                         (config.raw["agents"], config.raw["profiles"], "local-first"))
        self.assertEqual({key: added.raw["models"][key] for key in config.raw["models"]}, config.raw["models"])
        settings.validate_settings(added)
        model = added.model("kate")
        self.assertEqual((model.provider.kind, model.provider.api_key_env, model.extra_body),
                         ("openwebui", "OPENWEBUI_API_KEY", {"max_tokens": None}))

    def test_apply_keeps_repos_token_limits_harness_and_pipeline(self) -> None:
        repo = make_repo(self.tmp, "app")
        config = self.make_config({"repos": [{"name": "app", "path": str(repo), "base": None, "setup": None,
                                              "escalate_paths": []}],
                                   "limits": {"max_turns": 42}, "harness": {"sandbox": "bwrap"},
                                   "pipeline": {"max_parallel_jobs": 3}, "data_dir": str(self.tmp / "data")})
        updated = settings.apply_preset(config, "hybrid")
        self.assertEqual(stat.S_IMODE(self.config_path.stat().st_mode), 0o600)
        self.assertEqual(updated.raw["server"]["token"], TOKEN)
        self.assertEqual(updated.repo("app").path, repo)
        self.assertEqual(updated.limits().max_turns, 42)
        self.assertEqual(updated.raw["harness"]["sandbox"], "bwrap")
        self.assertEqual(updated.pipeline().max_parallel_jobs, 3)
        self.assertEqual(updated.data_dir, self.tmp / "data")
        self.assertEqual(updated.agent_model(settings.resolved_agents(updated)["approver"]).model, "claude-opus-5-5")
        self.assertEqual(Config.load(self.config_path).raw, updated.raw)

    def test_entries_are_replaced_whole_and_routes_reset(self) -> None:
        config = self.make_config({"agents": {"reviewer-perf": {"model": "sonnet"}}})
        config = settings.apply_preset(config, "anthropic")
        opus = config.model("opus")
        preset_extra = settings.load_preset("anthropic")["models"]["opus"]["extra_body"]
        self.assertEqual({k: v for k, v in opus.extra_body.items() if v is not None}, preset_extra,
                         "no DeepSeek reasoning_effort may leak into the Claude entry")
        config = settings.apply_preset(settings.apply_preset(config, "hybrid"), "strata")
        agents = settings.resolved_agents(config)
        self.assertEqual(config.agent_model(agents["reviewer-code"]).key, "local")
        self.assertEqual(config.model("local").provider.name, "strata")
        self.assertEqual(config.raw["agents"]["reviewer-perf"], {"model": "sonnet"}, "custom routes survive")
        self.assertEqual(settings.tier_view(config)["opus"]["same_as"], "local")
        self.assertIn("claude-sonnet", config.raw["models"], "entries the preset does not name are kept")

    def test_preset_config_does_not_save(self) -> None:
        config = self.make_config()
        before = self.config_path.read_bytes()
        merged = settings.preset_config(config, "mixed")
        self.assertEqual(merged.model("qwen-27b").provider.name, "llamacpp-qwen")
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertNotIn("qwen-27b", config.raw["models"])

    def test_switching_hybrid_local_to_strata_switches_the_opus_tier(self) -> None:
        """DESIGN §23: switching to Strata is changing local.provider; the opus tier copies local."""
        config = settings.apply_preset(self.make_config(), "hybrid")
        switched = settings.update_config(config, {"models": {"local": {"provider": "strata", "model": "strata"}}})
        for key in ("local", "opus"):
            model = switched.model(key)
            self.assertEqual((model.provider.name, model.model), ("strata", "strata"), key)
        self.assertEqual(settings.tier_view(switched)["opus"]["same_as"], "local")
        self.assertEqual(switched.model("sonnet").model, "claude-sonnet-5-5", "a tier copying another entry stays")
        self.assertEqual(switched.model("haiku").model, "claude-haiku-5-5")
        from swfactory import doctor
        users = {provider.name: labels for provider, labels, _ in
                 doctor._providers(doctor._routes(switched, settings.resolved_agents(switched))[0])}
        self.assertNotIn("tabby", users, "nothing runs on TabbyAPI after the switch")
        self.assertIn("opus tier", users["strata"])

    def test_gui_style_patch_with_every_entry_moves_the_copies_too(self) -> None:
        config = settings.apply_preset(self.make_config(), "strata")
        models = settings.config_view(config)["models"]
        models["local"] = dict(models["local"], provider="ollama", model="qwen3-coder:30b")
        switched = settings.update_config(config, {"models": models})
        for tier in ("opus", "sonnet", "haiku"):
            model = switched.model(tier)
            self.assertEqual((model.provider.name, model.model, model.context_tokens),
                             ("ollama", "qwen3-coder:30b", 262144))
            self.assertEqual(settings.tier_view(switched)[tier]["same_as"], "local")

    def test_a_tier_changed_in_the_same_patch_keeps_its_own_change(self) -> None:
        config = settings.apply_preset(self.make_config(), "strata")
        patched = settings.update_config(config, {"models": {"local": {"model": "other"},
                                                             "opus": {"max_tokens": 8192}}})
        self.assertEqual(patched.model("local").model, "other")
        self.assertEqual((patched.model("opus").model, patched.model("opus").max_tokens), ("strata", 8192))
        self.assertEqual(patched.model("sonnet").model, "other", "untouched copies still follow")
        unlinked = settings.update_config(patched, {"models": {"local": {"model": "third"}}})
        self.assertEqual(unlinked.model("opus").model, "strata", "a tier that no longer copies local stays")
        removed = settings.update_config(unlinked, {"models": {"local": None}, "agents": {
            name: {"model": "sonnet"} for name in FACTORY_AGENTS}})
        self.assertNotIn("local", {k for k, v in removed.raw["models"].items() if isinstance(v, dict)})
        self.assertEqual(removed.model("sonnet").model, "third", "removing the source leaves its copies")

    def test_a_routing_preset_sets_the_default_profile(self) -> None:
        """A default profile left by local-first must not route agents again after another routing preset."""
        config = settings.apply_preset(self.make_config(), "local-first")
        self.assertEqual(config.raw["default_profile"], "local-first")
        self.assertEqual(sorted(config.raw["profiles"]), PROFILES)
        config = settings.apply_preset(config, "hybrid")
        self.assertIsNone(config.raw["default_profile"])
        self.assertIn("best", config.raw["profiles"], "profiles stay until removed; only the default goes")
        config = settings.apply_preset(settings.apply_preset(config, "local-first"), "rea")
        self.assertEqual(config.raw["default_profile"], "local-first", "an add-on routes nothing and keeps it")

    def test_unknown_presets_are_rejected(self) -> None:
        config = self.make_config()
        for name in ("nope", "../deepseek", "", "DEEPSEEK", 5):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, "unknown preset"):
                    settings.apply_preset(config, name)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
