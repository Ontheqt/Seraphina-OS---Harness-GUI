"""Tests for the routing engine (DESIGN.md section 23, swfactory/routing.py): prices and cost, cached reachability,
builds, overflow (unreachable and slot wait), loaded-model awareness and swaps, the builder's escalation ladder with
its hand-over prompt, routing profiles, estimates, doctor model-id checks, and the HTTP surface.

The orchestrator runs on the scripted runner of test_orchestrator; probes are faked with a Reachability whose probe
and loaded functions read dicts, so nothing here touches the network (the doctor test uses MockLLM on loopback).
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from typing import Any
from unittest import mock

from swfactory import doctor, routing, settings
from swfactory.chats import ChatManager
from swfactory.config import Config, ModelConfig, ProviderConfig, deep_merge
from swfactory.harness.providers import provider_semaphore
from swfactory.orchestrator import Factory
from swfactory.routing import Build, Reachability, Role
from swfactory.runners import AgentRun, RunContext
from tests.mock_llm import MockLLM
from tests.test_chats import ChatTestCase
from tests.helpers import commit_file, run_git
from tests.test_orchestrator import REPO, TIMEOUT, USAGE, Call, FactoryTestCase, reviews, until, write_build
from tests.test_plans import PLAN_2, PlanTestCase
from tests.test_server import FakeFactory as HttpFactory
from tests.test_server import ServerTestCase

LOCAL = {"type": "openai", "api_key_env": None, "max_concurrency": 1, "concurrency_scope": "run", "timeout": 60}


def provider(name: str = "p", base_url: str = "https://api.example.invalid/v1", **extra: Any) -> ProviderConfig:
    return ProviderConfig(name=name, type=extra.pop("type", "openai"), base_url=base_url, **extra)


def model(key: str = "m", prov: ProviderConfig | None = None, **extra: Any) -> ModelConfig:
    return ModelConfig(key=key, provider=prov or provider(), model=extra.pop("model_id", key), **extra)


class FakeReach(Reachability):
    """A Reachability whose answers come from dicts: up[name] (default True) and loaded[name]."""

    def __init__(self, up: dict[str, bool] | None = None, loaded: dict[str, str | None] | None = None) -> None:
        self.up_map = dict(up or {})
        self.loaded_map = dict(loaded or {})
        self.probed: list[str] = []
        super().__init__(probe=self._probe, loaded=self._loaded, ttl=0.0)

    def _probe(self, cfg: ProviderConfig, timeout: float) -> tuple[bool, str]:
        self.probed.append(cfg.name)
        up = self.up_map.get(cfg.name, True)
        return up, "" if up else f"cannot reach {cfg.base_url} (start your local server)"

    def _loaded(self, cfg: ProviderConfig, timeout: float) -> tuple[str | None, str]:
        found = self.loaded_map.get(cfg.name)
        return found, "" if found else "no model loaded"


# -- the routing module ----------------------------------------------------------------


class PriceTest(unittest.TestCase):
    def test_prices_are_unknown_unless_set_and_free_on_local_servers(self) -> None:
        priced = model(prov=provider(), price_in=4.0, price_out=20.0)
        self.assertEqual(routing.price_table(priced), {"in": 4.0, "out": 20.0, "cached_in": 4.0})
        cached = model(prov=provider(), price_in=4.0, price_out=20.0, price_cached_in=0.4)
        self.assertEqual(routing.price_table(cached)["cached_in"], 0.4)
        self.assertIsNone(routing.price_table(model(prov=provider())), "a missing price is unknown, not free")
        self.assertIsNone(routing.price_table(model(prov=provider(), price_in=1.0)), "both prices or none")
        local = model(prov=provider(base_url="http://127.0.0.1:5000/v1"))
        self.assertEqual(routing.price_table(local), {"in": 0.0, "out": 0.0, "cached_in": 0.0})
        self.assertEqual(routing.price_table(model(prov=provider(base_url="http://localhost:1/v1"), price_in=1.0,
                                                   price_out=1.0))["in"], 1.0, "a local price set is kept")

    def test_run_cost_charges_cached_tokens_at_their_price(self) -> None:
        usage = {"input_tokens": 1_000_000, "cached_tokens": 400_000, "output_tokens": 100_000}
        cost, priced = routing.run_cost(usage, {"in": 4.0, "out": 20.0, "cached_in": 0.4})
        self.assertTrue(priced)
        self.assertAlmostEqual(cost, 2.4 + 0.16 + 2.0)
        self.assertEqual(routing.run_cost(usage, None), (None, False))
        self.assertEqual(routing.run_cost({"input_tokens": 5, "cached_tokens": 9}, {"in": 1e6, "out": 0, "cached_in": 0}),
                         (0.0, True), "cached never exceeds input")

    def test_running_totals(self) -> None:
        total = routing.zero_cost()
        routing.add_cost(total, 0.5, True)
        routing.add_cost(total, 0.25, True)
        self.assertEqual(total, {"usd": 0.75, "priced": True, "unpriced_runs": 0})
        routing.add_cost(total, None, False)
        self.assertEqual(total, {"usd": 0.75, "priced": False, "unpriced_runs": 1})
        self.assertEqual(routing.add_cost(None, 1.0, True), {"usd": 1.0, "priced": True, "unpriced_runs": 0})


class ReachabilityTest(unittest.TestCase):
    def test_answers_are_cached_for_the_ttl_and_forgotten_on_demand(self) -> None:
        now = [100.0]
        calls: list[str] = []

        def probe(cfg: ProviderConfig, timeout: float) -> tuple[bool, str]:
            calls.append(cfg.name)
            self.assertEqual(timeout, routing.REACH_TIMEOUT)
            return True, ""

        reach = Reachability(probe=probe, clock=lambda: now[0])
        tabby = provider("tabby", "http://127.0.0.1:5000/v1")
        for _ in range(3):
            self.assertEqual(reach.up(tabby), (True, ""))
        self.assertEqual(calls, ["tabby"])
        now[0] += routing.REACH_TTL + 1
        reach.up(tabby)
        reach.forget("tabby")
        reach.up(tabby)
        self.assertEqual(calls, ["tabby"] * 3)

    def test_a_broken_probe_counts_as_down(self) -> None:
        def probe(cfg: ProviderConfig, timeout: float) -> tuple[bool, str]:
            raise RuntimeError("boom")

        up, why = Reachability(probe=probe).up(provider())
        self.assertFalse(up)
        self.assertIn("boom", why)

    def test_the_default_probe_reads_the_model_list_and_the_loaded_model(self) -> None:
        with MockLLM(model_ids=["a", "b"], loaded_model="a") as llm:
            tabby = provider("tabby-default-probe", llm.base_url + "/v1", kind="tabby")
            self.assertEqual(routing.probe_up(tabby, 3.0), (True, ""))
            self.assertEqual(routing.loaded_model_url(tabby), llm.base_url + "/v1/model")
            self.assertEqual(routing.probe_loaded(tabby, 3.0), ("a", ""))
            plain = provider("plain", llm.base_url + "/v1")
            self.assertIsNone(routing.loaded_model_url(plain))
            self.assertEqual(settings.list_models(plain, 3.0), (200, ["a", "b"], ""))
        up, why = routing.probe_up(provider("gone", "http://127.0.0.1:9/v1"), 1.0)
        self.assertFalse(up)
        self.assertIn("start your local server", why)
        self.assertEqual(routing.model_id_of({"data": [{"id": "x"}]}), "x")
        self.assertEqual(routing.model_id_of({"model_name": "y"}), "y")


class ChooseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tabby = provider("tabby", "http://127.0.0.1:5000/v1")
        self.strata = provider("strata", "http://127.0.0.1:18080/v1")
        self.api = provider("api")
        self.builds = [Build(self.tabby, "flash-exl3"), Build(self.strata, "strata")]
        self.overflow = [Build(self.api, "mimo")]

    def test_one_build_without_overflow_is_never_probed(self) -> None:
        reach = FakeReach(up={"api": False})
        self.assertEqual(routing.choose([Build(self.api, "x")], None, reach).index, 0)
        self.assertEqual(reach.probed, [])

    def test_builds_fall_through_to_the_first_that_answers(self) -> None:
        reach = FakeReach(up={"tabby": False})
        choice = routing.choose(self.builds, self.overflow, reach, "mimo")
        self.assertEqual((choice.index, choice.overflowed), (1, False))
        self.assertIn("build tabby/flash-exl3 is unavailable", choice.notes[0])
        self.assertEqual(reach.probed, ["tabby", "strata"])

    def test_overflow_when_no_build_answers(self) -> None:
        choice = routing.choose(self.builds, self.overflow, FakeReach(up={"tabby": False, "strata": False}), "mimo")
        self.assertEqual((choice.index, choice.overflowed), (0, True))
        self.assertTrue(choice.why.startswith("no build answered"))
        everything_down = FakeReach(up={"tabby": False, "strata": False, "api": False})
        choice = routing.choose(self.builds, self.overflow, everything_down, "mimo")
        self.assertEqual((choice.index, choice.overflowed), (0, False), "no silent fallback: the first build fails")
        self.assertIn("the overflow mimo is unavailable too", choice.notes)
        self.assertIn("running on tabby/flash-exl3 anyway", choice.notes)

    def test_a_model_aware_server_with_another_model_loaded(self) -> None:
        tabby = provider("tabby", "http://127.0.0.1:5000/v1", kind="tabby")
        builds = [Build(tabby, "flash-exl3"), Build(self.strata, "strata")]
        refused = routing.choose(builds, None, FakeReach(loaded={"tabby": "owners-model"}))
        self.assertEqual(refused.index, 1, "swaps off: the build counts as unavailable")
        self.assertIn("tabby has owners-model loaded and allow_model_swap is off", refused.notes[0])
        same = routing.choose(builds, None, FakeReach(loaded={"tabby": "flash-exl3"}))
        self.assertEqual((same.index, same.swap), (0, None))
        unknown = routing.choose(builds, None, FakeReach(loaded={}))
        self.assertEqual(unknown.index, 1)
        self.assertIn("cannot tell which model tabby has loaded", unknown.notes[0])
        swapping = provider("tabby", "http://127.0.0.1:5000/v1", kind="tabby", allow_model_swap=True)
        swap = routing.choose([Build(swapping, "flash-exl3")], None, FakeReach(loaded={"tabby": "owners-model"}))
        self.assertEqual((swap.index, swap.swap), (0, {"provider": "tabby", "from": "owners-model", "to": "flash-exl3"}))

    def test_escalation_start(self) -> None:
        self.assertIsNone(routing.escalation_start(2, 0))
        self.assertIsNone(routing.escalation_start(0, 3))
        self.assertEqual([routing.escalation_start(2, k) for k in (1, 2, 3, 9)], [0, 1, 1, 1])


class SlotTest(unittest.TestCase):
    def test_take_slot_holds_a_run_slot_and_times_out_when_busy(self) -> None:
        cfg = provider(f"slot-{uuid.uuid4().hex[:8]}", "http://127.0.0.1:1/v1", max_concurrency=1,
                       concurrency_scope="run")
        cancel = threading.Event()
        release = routing.take_slot(cfg, 1.0, cancel)
        self.assertIsNotNone(release)
        waits: list[int] = []
        result: list[Any] = []
        other = threading.Thread(target=lambda: result.append(routing.take_slot(cfg, 0.2, cancel,
                                                                                lambda: waits.append(1))))
        other.start()
        other.join(5)
        self.assertEqual((result, waits), ([None], [1]), "busy past the limit: no slot, one wait notice")
        release()
        release()   # idempotent
        self.assertIsNotNone(routing.take_slot(cfg, 0.2, cancel))

    def test_a_cancelled_waiter_never_keeps_the_slot(self) -> None:
        cfg = provider(f"slot-{uuid.uuid4().hex[:8]}", "http://127.0.0.1:1/v1", max_concurrency=1,
                       concurrency_scope="run")
        slots = provider_semaphore(cfg.name, 1)
        cancelled = threading.Event()
        cancelled.set()
        self.assertIsNone(routing.take_slot(cfg, 5.0, cancelled), "cancelled: a free slot is not taken")
        self.assertTrue(slots.acquire(blocking=False), "and not kept either")
        result: list[Any] = []
        cancel = threading.Event()
        waiter = threading.Thread(target=lambda: result.append(routing.take_slot(cfg, float("inf"), cancel)))
        waiter.start()
        time.sleep(0.1)
        cancel.set()
        slots.release()   # the slot comes free in the same acquire slice as the cancel
        waiter.join(5)
        self.assertEqual(result, [None])
        self.assertTrue(slots.acquire(blocking=False), "the cancelled waiter gave the slot back")
        slots.release()

    def test_call_scope_only_waits(self) -> None:
        cfg = provider(f"slot-{uuid.uuid4().hex[:8]}", "http://127.0.0.1:1/v1", max_concurrency=1)
        cancel = threading.Event()
        routing.take_slot(cfg, 1.0, cancel)()
        slots = provider_semaphore(cfg.name, 1)
        self.assertTrue(slots.acquire(blocking=False), "call scope gave the slot back at once")
        slots.release()
        self.assertIsNotNone(routing.take_slot(provider("unlimited"), 0.0, cancel), "no limit: nothing to wait for")


class FamilyTest(unittest.TestCase):
    """DESIGN §23, the independence guard: what a model family is and what the guard picks."""

    def test_a_family_is_the_entry_s_label_else_read_from_the_model_id(self) -> None:
        cases = {"openai/gpt-6.1-sol": "openai", "gpt-oss-120b": "openai", "o3-mini": "openai",
                 "claude-opus-5-5": "anthropic", "anthropic/claude-sonnet-5-5": "anthropic",
                 "google/gemini-4-argon": "google", "gemma-4-31b-q8_0": "google", "meta/muse-spark-1.3": "meta",
                 "meta-llama/llama-4-scout": "meta", "deepseek/deepseek-v4.1-flash-1017:thinking": "deepseek",
                 "deepseek-v4-pro": "deepseek", "Qwen3.8-Flash-Next-Uncensored-EXL3-2.05bpw": "qwen",
                 "Swift-1.5-Qwen3.8-27B-EXL3-5bpw": "qwen", "qwen3-coder:30b": "qwen", "xiaomi/mimo-v2.6-pro": "xiaomi",
                 "mistralai/devstral-2": "mistral", "strata": "strata", "flash-exl3": "flash", "": "unknown"}
        for model_id, family in cases.items():
            with self.subTest(model_id=model_id):
                self.assertEqual(routing.default_family(model_id), family)
        self.assertEqual(routing.entry_family({"model": "strata", "family": " Qwen "}), "qwen")
        self.assertEqual(routing.entry_family({"model": "strata"}), "strata")
        self.assertEqual(routing.model_family(model("m", model_id="strata", family="qwen")), "qwen")
        self.assertEqual(routing.model_family(model("m", model_id="claude-x")), "anthropic")
        self.assertEqual(routing.route_family({"model": "m", "model_id": "openai/x"}), "openai",
                         "a snapshot or job.runs entry from before families")
        self.assertEqual(routing.route_family({"model": "m", "model_id": "openai/x", "family": "own"}), "own")

    def test_hub_and_quantizer_namespaces_name_no_family(self) -> None:
        """Ollama names a Hugging Face pull hf.co/<user>/<repo>:<quant> and vLLM serves the repo id: the family is
        the model the id names, not the namespace it was pulled from; a quantization suffix is not a family."""
        cases = {"unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF": "qwen", "Qwen/Qwen3-32B": "qwen",
                 "hf.co/bartowski/Qwen2.5-Coder-32B-Instruct-GGUF:Q4_K_M": "qwen",
                 "hf.co/bartowski/Llama-3.3-70B-Instruct-GGUF:Q4_K_M": "meta",
                 "lmstudio-community/gemma-3-27b-it-GGUF": "google",
                 "mlx-community/Llama-3.3-70B-Instruct-4bit": "meta",
                 "openrouter/anthropic/claude-sonnet-4": "anthropic", "TEE/deepseek-v3.2": "deepseek",
                 "nvidia/Llama-3.1-Nemotron-70B": "meta", "anthropic/a-new-name": "anthropic", "openai/o3": "openai",
                 "phi-4-gptq": "microsoft", "granite-3.3-8b-instruct-gptq": "ibm", "gpt4o": "openai",
                 "gemma3n:e4b": "google", "qwen2.5vl:7b": "qwen", "someuser/coder-7b": "coder",
                 "hf.co/someuser/strata-gguf:Q8_0": "strata"}
        for model_id, family in cases.items():
            with self.subTest(model_id=model_id):
                self.assertEqual(routing.default_family(model_id), family)
        builder = {"model": "q-local", "model_id": "unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF"}
        judge = {"model": "qwen-max", "model_id": "qwen/qwen3.8-max",
                 "independent_alternate": {"model": "muse", "model_id": "meta/muse-spark-1.3"}}
        self.assertEqual(routing.judge_choice(judge, {routing.route_family(builder): "q-local"})[1], "alternate",
                         "two Qwen models are one family, whichever namespace served the builder")
        qwen = {"model": "a", "model_id": "hf.co/bartowski/Qwen2.5-Coder-32B-Instruct-GGUF:Q4_K_M"}
        llama = {"model": "b", "model_id": "hf.co/bartowski/Llama-3.3-70B-Instruct-GGUF:Q4_K_M"}
        self.assertEqual(routing.judge_choice(llama, {routing.route_family(qwen): "a"}), (llama, None),
                         "a Qwen and a Llama pulled from one namespace are independent")

    def test_judges_and_the_choice(self) -> None:
        self.assertEqual([routing.is_judge(a) for a in ("approver", "reviewer-code", "builder", "lead", "coder")],
                         [True, True, False, False, False])
        alternate = {"model": "alt", "model_id": "meta/a", "family": "meta"}
        route = {"model": "sol", "model_id": "openai/sol", "family": "openai", "independent_alternate": alternate}
        self.assertEqual(routing.judge_choice(route, {"qwen": "flash"}), (route, None))
        self.assertEqual(routing.judge_choice(route, {"openai": "sol"}), (alternate, "alternate"))
        self.assertEqual(routing.judge_choice(route, {"openai": "sol", "meta": "x"}), (route, "reduced"),
                         "an alternate of a family the builder ran on too does not help")
        bare = {key: value for key, value in route.items() if key != "independent_alternate"}
        self.assertEqual(routing.judge_choice(bare, {"openai": "sol"}), (bare, "reduced"))
        self.assertEqual(routing.shared_model(route, {"openai": "sol-x"}, "openai/sol@build2"),
                         {"model": "sol", "model_id": "openai/sol@build2", "family": "openai",
                          "builder_model": "sol-x"})
        shared = {"model": "sol", "family": "openai", "builder_model": "sol-x"}
        self.assertEqual(routing.placement_reason({"kind": "cost", "text": "x"}, {
            "independence": "alternate", "shared_model": shared}),
            {"kind": "stage-fit", "text": "independent alternate: the builder ran on the openai family (sol-x) this "
                                          "round, like sol"})
        self.assertEqual(routing.placement_reason({"kind": "cost", "text": "x"}, {"independence": "reduced"}),
                         {"kind": "cost", "text": "x"}, "a reduced run keeps its route's reason")

    def test_a_per_run_model_drops_the_profile_s_alternate(self) -> None:
        base = {"reviewer-code": {"model": "sol", "independent_alternate": "alt", "reason": "stage-fit: x"}}
        self.assertEqual(routing.merged_placements(base, {"reviewer-code": {"model": "other"}}),
                         {"reviewer-code": {"model": "other"}})
        self.assertEqual(routing.merged_placements(base, {"reviewer-code": {"model": "other",
                                                                            "independent_alternate": "alt2"}}),
                         {"reviewer-code": {"model": "other", "independent_alternate": "alt2"}})
        self.assertEqual(routing.merged_placements(base, {"reviewer-code": {"effort": "high"}})["reviewer-code"],
                         {**base["reviewer-code"], "effort": "high"})


class PlacementShapeTest(unittest.TestCase):
    def test_reasons_and_efforts(self) -> None:
        self.assertEqual(routing.reason("cost: free locally"), {"kind": "cost", "text": "free locally"})
        self.assertEqual(routing.reason({"kind": "stage-fit", "text": "x"}), {"kind": "stage-fit", "text": "x"})
        self.assertIsNone(routing.reason(None))
        for bad in ("vibes: no", {"kind": "time", "why": "x"}, 3, {"kind": "time", "text": "x" * 300}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                routing.reason(bad)
        self.assertEqual(routing.effort_body("anthropic", "high"), {"output_config": {"effort": "high"}})
        self.assertEqual(routing.effort_body("openai", "xhigh"), {"reasoning_effort": "xhigh"})
        self.assertEqual(routing.effort_body("openai", None), {})
        with self.assertRaises(ValueError):
            routing.effort_body("openai", "turbo")
        self.assertEqual(routing.effort_body("openai", "minimal"), {"reasoning_effort": "minimal"})
        with self.assertRaisesRegex(ValueError, "'minimal' is not taken by Anthropic-format providers"):
            routing.effort_body("anthropic", "minimal")

    def test_the_reason_belongs_to_the_placement(self) -> None:
        own = {"kind": "cost", "text": "free locally"}
        self.assertEqual(routing.placement_reason(own, {}), own)
        self.assertEqual(routing.placement_reason(own, {"build": {"index": 1}}), own, "another build, same model")
        overflow = {"overflow": {"from": "flash", "to": "cheap", "why": "no build answered (build t/f is down)"}}
        self.assertEqual(routing.placement_reason(own, overflow), {"kind": "time",
                                                                   "text": "overflow from flash: no build answered"})
        rung = {"escalation": {"rung": 2, "of": 2, "sendbacks": 3, "from": "flash", "model": "stronger"}}
        self.assertEqual(routing.placement_reason(None, rung),
                         {"kind": "capability", "text": "escalation rung 2 of 2 after 3 send-back(s)"})
        base = {"builder": {"model": "flash", "effort": "low", "reason": {"kind": "cost", "text": "x"}}}
        self.assertEqual(routing.merged_placements(base, {"builder": {"model": "strong"}}),
                         {"builder": {"model": "strong", "effort": "low"}}, "the reason was for flash")
        self.assertEqual(routing.merged_placements(base, {"builder": {"effort": "high"}})["builder"]["reason"],
                         {"kind": "cost", "text": "x"}, "same model: the reason stays")
        self.assertEqual(routing.merged_placements(base, {"builder": {"model": "flash"}})["builder"]["reason"],
                         {"kind": "cost", "text": "x"})

    def test_profiles_are_checked_like_routing(self) -> None:
        models, drivers = {"a", "b"}, {"builtin"}
        good = {"profiles": {"fast": {"description": "x", "agents": {"builder": {"model": "a", "escalate": ["b"],
                                                                                 "effort": "high",
                                                                                 "reason": "time: quick"}}}},
                "default_profile": "fast"}
        routing.check_profiles(good, models, drivers)
        for raw, message in (
                ({"profiles": {"Bad Name": {}}}, "invalid name"),
                ({"profiles": {"p": {"agents": {"builder": {"model": "zzz"}}}}}, "is not a key of models"),
                ({"profiles": {"p": {"agents": {"builder": {"escalate": ["a", "zzz"]}}}}}, "escalate: 'zzz'"),
                ({"profiles": {"p": {"agents": {"builder": {"driver": "nope"}}}}}, "not a configured driver"),
                ({"profiles": {"p": {"agents": {"builder": {"modle": "a"}}}}}, "unknown keys modle"),
                ({"profiles": {"p": {"extra": 1}}}, "must be an object like"),
                ({"profiles": {}, "default_profile": "gone"}, "default_profile 'gone'")):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                routing.check_profiles(raw, models, drivers)

    def test_profile_names(self) -> None:
        raw = {"profiles": {"best": {"agents": {}}}, "default_profile": "best"}
        self.assertEqual(routing.profile_name(raw, None), "best")
        self.assertIsNone(routing.profile_name(raw, ""))
        self.assertIsNone(routing.profile_name({"profiles": {}}, None))
        with self.assertRaisesRegex(ValueError, r"unknown profile 'worst' \(available: best\)"):
            routing.profile_name(raw, "worst")
        merged = routing.merged_placements({"builder": {"model": "a", "effort": "low"}}, {"builder": {"model": "b"}})
        self.assertEqual(merged, {"builder": {"model": "b", "effort": "low"}})


class EstimateTest(unittest.TestCase):
    def roles(self, reviewer_provider: ProviderConfig) -> list[Role]:
        api = provider("api")
        priced = model("opus", api, price_in=4.0, price_out=20.0)
        reviewer = model("rev", reviewer_provider, price_in=None)
        return [Role("spec-writer", "spec", priced, {"model": "opus"}),
                Role("builder", "build", priced, {"model": "opus", "reason": {"kind": "cost", "text": "x"}}),
                Role("reviewer-a", "review", reviewer, {"model": "rev"}),
                Role("reviewer-b", "review", reviewer, {"model": "rev"}),
                Role("approver", "approve", priced, {"model": "opus"})]

    def test_defaults_without_history_and_slots_shape_the_review_stage(self) -> None:
        local = provider("one-slot", "http://127.0.0.1:5000/v1", max_concurrency=1, tokens_per_second=50.0)
        result = routing.estimate(self.roles(local), {})
        self.assertEqual(result["basis"], "defaults")
        rows = {row["agent"]: row for row in result["roles"]}
        self.assertEqual(rows["builder"]["tokens"], routing.DEFAULT_TOKENS["builder"])
        self.assertEqual(rows["reviewer-a"]["tokens"], routing.DEFAULT_TOKENS["reviewer"])
        self.assertEqual(rows["builder"]["reason"], {"kind": "cost", "text": "x"})
        self.assertTrue(rows["reviewer-a"]["priced"] and rows["reviewer-a"]["local"], "local reviewers are free")
        one = rows["reviewer-a"]["seconds"]
        review = next(stage for stage in result["stages"] if stage["stage"] == "review")
        self.assertAlmostEqual(review["seconds"], 2 * one, delta=1, msg="one slot: one reviewer after the other")
        tokens = routing.DEFAULT_TOKENS["reviewer"]
        expected = tokens["output_tokens"] / 50 + (tokens["input_tokens"] - tokens["cached_tokens"]) / (50 * 20)
        self.assertEqual(one, round(expected))
        api_result = routing.estimate(self.roles(provider("api-reviews")), {})
        api_review = next(stage for stage in api_result["stages"] if stage["stage"] == "review")
        api_one = next(row for row in api_result["roles"] if row["agent"] == "reviewer-a")["seconds"]
        self.assertEqual(api_review["seconds"], api_one, "an API runs the reviewers together")
        self.assertFalse(api_result["total"]["priced"], "the API reviewers have no prices")
        self.assertIn("no prices for rev", " ".join(api_result["notes"]))
        self.assertEqual(result["total"]["seconds"], sum(stage["seconds"] for stage in result["stages"]))
        self.assertTrue(result["total"]["priced"])
        builder_cost = routing.run_cost(routing.DEFAULT_TOKENS["builder"], {"in": 4.0, "out": 20.0, "cached_in": 4.0})[0]
        self.assertAlmostEqual(rows["builder"]["cost_usd"], builder_cost)

    def test_history_medians_and_plans(self) -> None:
        roles = self.roles(provider("api"))
        samples = [{"input_tokens": n * 1000, "output_tokens": n * 10, "cached_tokens": n * 100} for n in (1, 5, 9)]
        result = routing.estimate(roles, {"builder": samples, "reviewer-a": samples[:2]})
        rows = {row["agent"]: row for row in result["roles"]}
        self.assertEqual(rows["builder"]["tokens"], {"input_tokens": 5000, "output_tokens": 50, "cached_tokens": 500})
        self.assertEqual(rows["builder"]["basis"], "median of 3 past jobs")
        self.assertEqual(rows["reviewer-a"]["basis"], "default table (2 past jobs; 3 needed)")
        self.assertEqual(result["basis"], "mixed")
        lead = Role("lead", "lead", model("opus", provider("api"), price_in=4.0, price_out=20.0), {"model": "opus"})
        plan = routing.estimate([lead, *roles], {}, "plan", [2, 4, 4])
        self.assertEqual(plan["jobs"], 4, "median of the past plans' job counts")
        job_seconds = sum(stage["seconds"] for stage in plan["stages"] if stage["stage"] != "lead")
        lead_seconds = next(stage["seconds"] for stage in plan["stages"] if stage["stage"] == "lead")
        self.assertEqual(plan["total"]["seconds"], lead_seconds + 4 * job_seconds)
        self.assertEqual(routing.estimate([lead, *roles], {}, "plan", [2])["jobs"], routing.DEFAULT_PLAN_JOBS)


class ConfigValidationTest(unittest.TestCase):
    def make(self, **sections: Any) -> Config:
        return Config(Path("/nonexistent/config.json"), deep_merge({
            "providers": {"tabby": {**LOCAL, "base_url": "http://127.0.0.1:5000/v1", "kind": "tabby"},
                          "strata": {**LOCAL, "base_url": "http://127.0.0.1:18080/v1"}},
            "models": {"flash": {"provider": "tabby", "model": "f", "max_tokens": 100,
                                 "builds": [{"provider": "strata", "model": "s", "context_tokens": 1000}],
                                 "overflow": "sonnet", "price_in": 0, "price_out": 0},
                       "opus": {"price_in": 1, "price_out": 2, "price_cached_in": 0.1}},
            "agents": {"builder": {"model": "flash", "escalate": ["opus"], "reason": "cost: local", "effort": "low"}},
        }, sections))

    def test_a_valid_placement_config(self) -> None:
        config = self.make()
        settings.validate_settings(config)
        self.assertEqual(config.provider("tabby").kind, "tabby")
        self.assertEqual((config.model("opus").price_in, config.model("opus").price_cached_in), (1.0, 0.1))

    def test_bad_placement_settings_name_their_field(self) -> None:
        cases = [
            ({"models": {"flash": {"builds": [{"provider": "nope", "model": "x"}]}}}, "builds\\[0\\].provider 'nope'"),
            ({"models": {"flash": {"builds": [{"provider": "strata"}]}}}, "builds\\[0\\].model must be a model id"),
            ({"models": {"flash": {"builds": [{"provider": "strata", "model": "s", "port": 1}]}}}, "builds\\[0\\] must be"),
            ({"models": {"flash": {"builds": [{"provider": "strata", "model": "s", "context_tokens": 50}]}}},
             "context_tokens must be larger"),
            ({"models": {"flash": {"overflow": "flash"}}}, "cannot be the model itself"),
            ({"models": {"flash": {"overflow": "gone"}}}, "overflow 'gone' is not a key of models"),
            ({"models": {"flash": {"price_in": -1}}}, "price_in must be a non-negative number"),
            ({"models": {"flash": {"price_out": None}}}, "set price_in and price_out together"),
            ({"providers": {"tabby": {"kind": "ollama"}}}, "kind must be one of tabby"),
            ({"providers": {"tabby": {"overflow": "gone"}}}, "providers.tabby.overflow 'gone'"),
            ({"providers": {"tabby": {"allow_model_swap": "yes"}}}, "allow_model_swap must be true or false"),
            ({"providers": {"tabby": {"overflow_after_seconds": 0}}}, "overflow_after_seconds must be a positive"),
            ({"agents": {"builder": {"escalate": ["gone"]}}}, "escalate: 'gone' is not a key of models"),
            ({"agents": {"builder": {"effort": "turbo"}}}, "effort must be one of"),
            ({"profiles": {"p": {"agents": {"approver": {"model": "gone"}}}}}, "profiles.p.agents.approver.model"),
            ({"default_profile": "p"}, "default_profile 'p' is not a key of profiles"),
        ]
        for patch, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                settings.validate_settings(self.make(**patch))

    def test_agents_star_takes_no_placement_keys(self) -> None:
        for key, value in (("effort", "high"), ("escalate", ["opus"]), ("reason", "cost: everything")):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, f"agents.\\*: {key} apply to one agent"):
                settings.validate_settings(self.make(agents={"*": {"driver": "builtin", key: value}}))
        settings.validate_settings(self.make(agents={"*": {"driver": "builtin", "extra_body": {"x": 1}}}))

    def test_an_effort_must_suit_the_models_provider(self) -> None:
        claude = {"providers": {"claude": {"type": "anthropic", "base_url": "https://api.anthropic.com",
                                           "api_key_env": None}},
                  "models": {"claude-x": {"provider": "claude", "model": "claude-opus-5-5"}}}
        cases = [
            ({"agents": {"approver": {"model": "claude-x", "effort": "minimal"}}},
             "agents.approver on model claude-x: effort 'minimal' is not taken by Anthropic-format providers"),
            ({"agents": {"approver": {"model": "claude-x"}},
              "profiles": {"cheap": {"agents": {"approver": {"effort": "minimal"}}}}},
             "profiles.cheap.agents.approver on model claude-x: effort 'minimal'"),
            ({"agents": {"*": {"model": "claude-x"}}, "profiles": {"p": {"agents": {"lead": {"effort": "minimal"}}}}},
             "profiles.p.agents.lead on model claude-x"),
        ]
        for patch, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                settings.validate_settings(self.make(**deep_merge(claude, patch)))
        settings.validate_settings(self.make(**deep_merge(claude, {"agents": {"approver": {
            "model": "claude-x", "effort": "max"}, "reviewer-code": {"model": "opus", "effort": "minimal"}}})))

    def test_independent_alternates_and_family_labels(self) -> None:
        models = {"sol": {"provider": "tabby", "model": "openai/gpt-6.1-sol"},
                  "sol-x": {"provider": "tabby", "model": "openai/gpt-6.1-sol", "family": "openai"},
                  "muse": {"provider": "tabby", "model": "meta/muse-spark-1.3"}}
        good = {"models": models, "agents": {"reviewer-code": {"model": "sol", "independent_alternate": "muse"},
                                             "approver": {"independent_alternate": "sol"}},
                "profiles": {"p": {"agents": {"approver": {"model": "muse", "independent_alternate": "sol-x"}}}}}
        settings.validate_settings(self.make(**good))
        cases = [
            ({"agents": {"builder": {"independent_alternate": "muse"}}},
             "agents.builder: independent_alternate is for the judges"),
            ({"agents": {"*": {"independent_alternate": "muse"}}}, "agents.\\*: independent_alternate apply to one"),
            ({"agents": {"reviewer-code": {"independent_alternate": "gone"}}},
             "agents.reviewer-code.independent_alternate 'gone' is not a key of models"),
            ({"agents": {"reviewer-code": {"independent_alternate": 3}}}, "independent_alternate must be a model key"),
            ({"agents": {"reviewer-code": {"independent_alternate": True}}}, "or false to accept the builder's family"),
            ({"agents": {"builder": {"independent_alternate": False}}},
             "agents.builder: independent_alternate is for the judges"),
            ({"agents": {"*": {"independent_alternate": False}}}, "agents.\\*: independent_alternate apply to one"),
            ({"agents": {"reviewer-code": {"model": "sol", "independent_alternate": "sol-x"}}},
             "agents.reviewer-code.independent_alternate 'sol-x' is of the openai family, like the judge's own model"),
            ({"profiles": {"p": {"agents": {"lead": {"independent_alternate": "muse"}}}}},
             "profiles.p.agents.lead: independent_alternate is for the judges"),
            ({"profiles": {"p": {"agents": {"approver": {"model": "muse", "independent_alternate": "muse"}}}}},
             "is of the meta family"),
            ({"models": {"muse": {"family": "Not a label!"}}}, "models.muse.family must be a family label"),
        ]
        for patch, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                settings.validate_settings(self.make(**deep_merge({"models": models}, patch)))
        view = settings.config_view(self.make(**good))
        self.assertEqual({key: view["model_families"][key] for key in models},
                         {"sol": "openai", "sol-x": "openai", "muse": "meta"})
        code = next(agent for agent in view["agent_list"] if agent["name"] == "reviewer-code")
        self.assertEqual(code["independent_alternate"], "muse")
        accepted = deep_merge(good, {"agents": {"reviewer-security": {"model": "sol", "independent_alternate": False}},
                                     "profiles": {"p": {"agents": {"reviewer-ux": {"independent_alternate": False}}}}})
        settings.validate_settings(self.make(**accepted))   # false: the routing accepts the builder's family
        security = next(agent for agent in settings.config_view(self.make(**accepted))["agent_list"]
                        if agent["name"] == "reviewer-security")
        self.assertIs(security["independent_alternate"], False)

    def test_profiles_are_settings_the_gui_can_post(self) -> None:
        self.assertIn("profiles", settings.UPDATABLE_SECTIONS)
        self.assertIn("default_profile", settings.UPDATABLE_SECTIONS)
        view = settings.config_view(self.make(profiles={"p": {"agents": {}}}, default_profile="p"))
        self.assertEqual((view["profiles"], view["default_profile"]), ({"p": {"agents": {}}}, "p"))
        builder = next(agent for agent in view["agent_list"] if agent["name"] == "builder")
        self.assertEqual((builder["escalate"], builder["effort"], builder["reason"]),
                         (["opus"], "low", {"kind": "cost", "text": "local"}))


# -- the orchestrator ---------------------------------------------------------------------


class PlacementTestCase(FactoryTestCase):
    reviewers = ("code",)

    def setUp(self) -> None:
        super().setUp()
        self.reach = FakeReach()
        self.suffix = uuid.uuid4().hex[:6]   # provider slots are process-wide by name

    def placed_config(self, **sections: Any) -> Config:
        tabby, strata = f"tabby{self.suffix}", f"strata{self.suffix}"
        return self.config(**deep_merge({
            "providers": {
                tabby: {**LOCAL, "base_url": "http://127.0.0.1:59001/v1"},
                strata: {**LOCAL, "base_url": "http://127.0.0.1:59002/v1"},
                "api": {"type": "openai", "base_url": "https://api.example.invalid/v1", "api_key_env": None},
                "api2": {"type": "openai", "base_url": "https://api2.example.invalid/v1", "api_key_env": None},
            },
            "models": {
                "flash": {"provider": tabby, "model": "flash-exl3", "max_tokens": 1000, "context_tokens": 8000,
                          "builds": [{"provider": tabby, "model": "flash-exl3"},
                                     {"provider": strata, "model": "flash-gguf", "max_tokens": 900}],
                          "overflow": "cheap"},
                "cheap": {"provider": "api", "model": "cheap-1", "max_tokens": 1000, "price_in": 1, "price_out": 2},
                "strong": {"provider": "api", "model": "strong-1", "max_tokens": 1000, "price_in": 2, "price_out": 10},
                "stronger": {"provider": "api2", "model": "stronger-1", "max_tokens": 1000},
            },
            "agents": {"builder": {"model": "flash", "escalate": ["strong", "stronger"],
                                   "reason": {"kind": "cost", "text": "free locally"}}},
        }, sections))

    def start_placed(self, config: Config | None = None, **actions: Any) -> Factory:
        self.recorder.actions.update({name.replace("_", "-"): act for name, act in actions.items()})
        factory = Factory(config or self.placed_config(), self.bus, runner_factory=self.recorder.runner_factory,
                          reach=self.reach)
        factory.start()
        self.addCleanup(factory.stop)
        return factory

    def start_event(self, job: dict, agent: str, index: int = 0) -> Any:
        return [e for e in self.events(job, "agent.start") if e.agent == agent][index]

    def logs(self, job: dict) -> str:
        return "\n".join(e.data["message"] for e in self.events(job, "log"))


class BuildsAndOverflowTest(PlacementTestCase):
    def test_the_snapshot_carries_builds_overflow_rungs_and_the_reason(self) -> None:
        factory = self.start_placed()
        route = factory.check_routing(REPO)["builder"]
        self.assertEqual([(b["provider"], b["model_id"]) for b in route["builds"]],
                         [(f"tabby{self.suffix}", "flash-exl3"), (f"strata{self.suffix}", "flash-gguf")])
        self.assertEqual(route["builds"][1]["max_tokens"], 900)
        self.assertEqual(route["builds"][1]["provider_entry"]["base_url"], "http://127.0.0.1:59002/v1")
        self.assertEqual((route["overflow"]["model"], route["overflow"]["model_id"]), ("cheap", "cheap-1"))
        self.assertNotIn("overflow", route["overflow"], "an overflow has no overflow of its own")
        self.assertEqual([rung["model"] for rung in route["escalate"]], ["strong", "stronger"])
        self.assertEqual(route["reason"], {"kind": "cost", "text": "free locally"})
        self.assertEqual(route["overflow"]["model_entry"]["price_in"], 1.0)
        self.assertNotIn("builds", factory.check_routing(REPO)["approver"], "one build: nothing to choose")

    def test_builds_fall_through_and_the_event_says_which_ran(self) -> None:
        self.reach.up_map[f"tabby{self.suffix}"] = False
        factory = self.start_placed()
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        ctx = self.recorder.of("builder")[0].ctx
        self.assertEqual((ctx.model.provider.name, ctx.model.model, ctx.model.max_tokens, ctx.model.key),
                         (f"strata{self.suffix}", "flash-gguf", 900, "flash"))
        start = self.start_event(job, "builder")
        self.assertEqual(start.data["build"], {"index": 1, "of": 2, "provider": f"strata{self.suffix}",
                                               "model_id": "flash-gguf"})
        self.assertEqual((start.data["model_id"], start.data["provider"]), ("flash-gguf", f"strata{self.suffix}"))
        self.assertEqual(start.data["reason"], {"kind": "cost", "text": "free locally"})
        run = next(entry for entry in job["runs"] if entry["agent"] == "builder")
        self.assertEqual((run["model"], run["provider"], run["model_id"], run["build"]["index"]),
                         ("flash", f"strata{self.suffix}", "flash-gguf", 1))
        self.assertIn(f"build tabby{self.suffix}/flash-exl3 is unavailable", self.logs(job))

    def test_overflow_when_no_build_answers(self) -> None:
        self.reach.up_map.update({f"tabby{self.suffix}": False, f"strata{self.suffix}": False})
        factory = self.start_placed()
        job = self.run_job(factory)
        ctx = self.recorder.of("builder")[0].ctx
        self.assertEqual((ctx.model.key, ctx.model.model, ctx.model.provider.name), ("cheap", "cheap-1", "api"))
        start = self.start_event(job, "builder")
        self.assertEqual((start.data["overflow"]["from"], start.data["overflow"]["to"]), ("flash", "cheap"))
        self.assertIn("no build answered", start.data["overflow"]["why"])
        self.assertEqual(job["agents"]["builder"]["model"], "cheap", "the record says what ran")
        run = next(entry for entry in job["runs"] if entry["agent"] == "builder")
        self.assertEqual((run["cost_usd"], run["priced"]), ((95 * 1 + 5 * 1 + 10 * 2) / 1e6, True))
        self.assertIn("overflows from flash to cheap", self.logs(job))

    def test_overflow_after_waiting_too_long_for_a_slot(self) -> None:
        tabby = f"tabby{self.suffix}"
        config = self.placed_config(providers={tabby: {"overflow_after_seconds": 0.3}})
        slots = provider_semaphore(tabby, 1)
        held, done = threading.Event(), threading.Event()

        def hold() -> None:   # another run holds the one slot (any thread but the worker's)
            slots.acquire()
            held.set()
            done.wait(TIMEOUT)
            slots.release()

        holder = threading.Thread(target=hold, daemon=True)
        holder.start()
        self.addCleanup(holder.join, 5)
        self.addCleanup(done.set)
        held.wait(5)
        factory = self.start_placed(config)
        job = self.run_job(factory)
        done.set()
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertEqual(self.recorder.of("builder")[0].ctx.model.key, "cheap")
        start = self.start_event(job, "builder")
        self.assertIn(f"no slot of {tabby} came free within 0.3s", start.data["overflow"]["why"])
        runs = [e for e in self.events(job) if e.agent == "builder" and e.data.get("run") == start.data["run"]]
        self.assertEqual([e.type for e in runs[:2]], ["agent.waiting", "agent.start"], "it waited, then started")
        self.assertEqual(runs[0].data["overflow_after_seconds"], 0.3)

    def test_with_the_overflow_down_too_the_run_keeps_waiting_for_its_slot(self) -> None:
        tabby = f"tabby{self.suffix}"
        self.reach.up_map["api"] = False
        config = self.placed_config(providers={tabby: {"overflow_after_seconds": 0.2}})
        slots = provider_semaphore(tabby, 1)
        held = threading.Event()

        def hold() -> None:
            slots.acquire()
            held.set()
            time.sleep(0.8)
            slots.release()

        holder = threading.Thread(target=hold, daemon=True)
        holder.start()
        self.addCleanup(holder.join, 5)
        held.wait(5)
        job = self.run_job(self.start_placed(config))
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertEqual(self.recorder.of("builder")[0].ctx.model.provider.name, tabby)
        self.assertNotIn("overflow", self.start_event(job, "builder").data)
        self.assertIn(f"the overflow cheap is unavailable too; waiting for {tabby}", self.logs(job))

    def test_cancelling_a_job_that_waits_for_its_slot_leaves_the_slot_free(self) -> None:
        tabby = f"tabby{self.suffix}"
        self.reach.up_map["api"] = False   # the overflow is down too: past the limit the run waits without one
        config = self.placed_config(providers={tabby: {"overflow_after_seconds": 0.2}})
        slots = provider_semaphore(tabby, 1)
        held, free = threading.Event(), threading.Event()

        def hold() -> None:
            slots.acquire()
            held.set()
            free.wait(TIMEOUT)
            slots.release()

        holder = threading.Thread(target=hold, daemon=True)
        holder.start()
        self.addCleanup(free.set)
        held.wait(5)
        factory = self.start_placed(config)
        job = factory.submit(REPO, "x")
        until(lambda: any(e.type == "agent.waiting" for e in self.bus.since(0) if e.job == job["id"]))
        time.sleep(0.6)                      # past overflow_after_seconds: in the wait without a limit
        factory.cancel(REPO, job["id"])
        free.set()                           # the other run ends in the same acquire slice as the cancel
        holder.join(5)
        self.assertEqual(factory.wait(REPO, job["id"], timeout=TIMEOUT)["stage"], "cancelled")
        self.assertEqual(self.recorder.of("builder"), [], "the builder never ran")
        self.assertTrue(slots.acquire(timeout=2), "the cancelled run holds no slot")
        slots.release()

    def test_a_free_slot_is_held_for_the_run_and_given_back(self) -> None:
        tabby = f"tabby{self.suffix}"
        config = self.placed_config(providers={tabby: {"overflow_after_seconds": 5}})
        slots = provider_semaphore(tabby, 1)
        seen: list[bool] = []

        def build(ctx: RunContext, call: Call) -> None:
            seen.append(slots.held())   # the worker thread holds it, so the runner re-enters it
            from tests.test_orchestrator import build as default_build
            default_build(ctx, call)

        job = self.run_job(self.start_placed(config, builder=build))
        self.assertEqual((job["stage"], seen), ("merged", [True]))
        self.assertEqual(self.recorder.of("builder")[0].ctx.model.provider.name, tabby)
        self.assertTrue(slots.acquire(blocking=False), "released after the run")
        slots.release()

    def test_a_cli_route_is_never_probed(self) -> None:
        self.reach.up_map.update({f"tabby{self.suffix}": False, f"strata{self.suffix}": False})
        config = self.placed_config(agents={"builder": {"driver": "qwen"}})
        job = self.run_job(self.start_placed(config))
        self.assertEqual(self.recorder.of("builder")[0].ctx.model.provider.name, f"tabby{self.suffix}")
        self.assertNotIn("overflow", self.start_event(job, "builder").data)
        self.assertNotIn(f"tabby{self.suffix}", self.reach.probed)

    def test_secrets_of_every_provider_a_route_can_reach_are_redacted(self) -> None:
        key = "sk-overflow-key-0123456789"
        config = self.placed_config(providers={"api": {"api_key_env": "ROUTING_TEST_KEY"}})
        factory = self.start_placed(config)
        with mock.patch.dict(os.environ, {"ROUTING_TEST_KEY": key}):
            job = factory.submit(REPO, "x")
            factory.wait(REPO, job["id"], timeout=TIMEOUT)
            factory.reload_config(self.config())   # the live config no longer knows the provider
            self.assertIn(key, factory.secret_values(), "the overflow's provider is only in the snapshot now")


class SwapTest(PlacementTestCase):
    def swap_config(self, **provider_settings: Any) -> Config:
        tabby = f"tabby{self.suffix}"
        return self.placed_config(providers={tabby: {"kind": "tabby", **provider_settings}})

    def test_with_swaps_off_another_loaded_model_makes_the_build_unavailable(self) -> None:
        self.reach.loaded_map[f"tabby{self.suffix}"] = "owners-model"
        job = self.run_job(self.start_placed(self.swap_config()))
        self.assertEqual(self.recorder.of("builder")[0].ctx.model.provider.name, f"strata{self.suffix}")
        self.assertNotIn("swap", self.start_event(job, "builder").data)
        self.assertIn(f"tabby{self.suffix} has owners-model loaded and allow_model_swap is off", self.logs(job))

    def test_with_swaps_on_the_run_goes_ahead_and_swap_back_restores(self) -> None:
        tabby = f"tabby{self.suffix}"
        self.reach.loaded_map[tabby] = "owners-model"
        with MockLLM(handler=lambda body, path: {"text": "OK"}) as llm:
            config = self.swap_config(allow_model_swap=True, swap_back=True, base_url=llm.base_url + "/v1")

            def build(ctx: RunContext, call: Call) -> None:
                self.reach.loaded_map[tabby] = "flash-exl3"   # the request made Tabby load it
                from tests.test_orchestrator import build as default_build
                default_build(ctx, call)

            job = self.run_job(self.start_placed(config, builder=build))
            self.assertEqual(job["stage"], "merged", job.get("reason"))
            start = self.start_event(job, "builder")
            self.assertEqual(start.data["swap"], {"provider": tabby, "from": "owners-model", "to": "flash-exl3"})
            self.assertEqual(self.recorder.of("builder")[0].ctx.model.provider.name, tabby)
            until(lambda: "has owners-model loaded again" in self.logs(job))
            self.assertEqual([request["model"] for request in llm.requests], ["owners-model"])
            self.assertEqual(llm.requests[0]["max_tokens"], 1)


class EscalationTest(PlacementTestCase):
    def test_each_changes_round_moves_the_builder_up_a_rung_with_a_hand_over(self) -> None:
        factory = self.start_placed(reviewer_code=reviews("CHANGES", "CHANGES", "PASS"))
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        calls = self.recorder.of("builder")
        self.assertEqual([call.ctx.model.key for call in calls], ["flash", "strong", "stronger"])
        self.assertEqual([call.kind for call in calls], ["run", "run", "run"], "a new model never resumes a session")
        handover = calls[1].prompt
        self.assertTrue(handover.startswith(f"Job folder: {job['job_dir']}. Worktree:"), "the paths line comes first")
        self.assertTrue(handover.endswith("for round 2.\nPort: 5110."))
        for needle in ("Hand-over: you take over this job's build from", "spec.md", "Review findings from code",
                       "1. Fix the thing.", "The diff so far", "feature-0.txt", "Your task now:\n",
                       "Round 1 reviews are in"):
            self.assertIn(needle, handover)
        self.assertIn("previous builder's notes", handover)
        self.assertEqual(job["sendbacks"], 2)
        starts = [e for e in self.events(job, "agent.start") if e.agent == "builder"]
        self.assertEqual([e.data.get("escalation", {}).get("rung") for e in starts], [None, 1, 2])
        self.assertEqual([bool(e.data.get("handover")) for e in starts], [False, True, True])
        self.assertEqual(starts[2].data["escalation"], {"rung": 2, "of": 2, "sendbacks": 2, "from": "flash",
                                                        "model": "stronger"})

    def test_the_last_rung_holds_beyond_the_list_and_resumes_on_the_same_model(self) -> None:
        config = self.placed_config(pipeline={"max_resumes": 3})
        factory = self.start_placed(config, reviewer_code=reviews("CHANGES", "CHANGES", "CHANGES", "PASS"))
        job = self.run_job(factory)
        calls = self.recorder.of("builder")
        self.assertEqual([call.ctx.model.key for call in calls], ["flash", "strong", "stronger", "stronger"])
        self.assertEqual(calls[3].kind, "resume", "the same rung again continues its session")
        self.assertNotIn("Hand-over", calls[3].prompt)

    def test_a_cli_driver_escalates_too(self) -> None:
        self.reach.up_map["api"] = False   # never probed: a CLI reaches its own endpoint
        config = self.placed_config(agents={"builder": {"driver": "qwen"}},
                                    drivers={"qwen": {"models": {"strong": "qwen3.8-max"}}})
        factory = self.start_placed(config, reviewer_code=reviews("CHANGES", "PASS"))
        job = self.run_job(factory)
        calls = self.recorder.of("builder")
        self.assertEqual([(call.driver, call.ctx.model.key, call.kind) for call in calls],
                         [("qwen", "flash", "run"), ("qwen", "strong", "run")])
        self.assertIn("Hand-over", calls[1].prompt)
        self.assertEqual(job["routing"]["builder"]["escalate"][0]["cli_model"], "qwen3.8-max")
        self.assertNotIn("api", self.reach.probed)

    def test_a_dead_rung_is_skipped(self) -> None:
        self.reach.up_map["api"] = False
        factory = self.start_placed(reviewer_code=reviews("CHANGES", "PASS"))
        job = self.run_job(factory)
        self.assertEqual([call.ctx.model.key for call in self.recorder.of("builder")], ["flash", "stronger"])
        self.assertIn("escalation rung 1 (strong) skipped", self.logs(job))

    def test_rework_counts_as_a_send_back(self) -> None:
        config = self.placed_config(pipeline={"max_resumes": 0})
        factory = self.start_placed(config, reviewer_code=reviews("CHANGES", "PASS"))
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "needs-human")
        factory.rework(REPO, job["id"], "Use the other API.")
        job = factory.wait(REPO, job["id"], timeout=TIMEOUT)
        calls = self.recorder.of("builder")
        self.assertEqual([call.ctx.model.key for call in calls], ["flash", "strong"])
        self.assertIn("The human's rework note", calls[1].prompt)
        self.assertIn("Use the other API.", calls[1].prompt)
        self.assertEqual(job["sendbacks"], 1)

    def test_the_build_check_resumes_the_same_rung(self) -> None:
        def build(ctx: RunContext, call: Call) -> None:
            from tests.test_orchestrator import build as default_build
            dirty = Path(ctx.cwd) / "dirty.txt"
            if call.index == 1:   # the new rung leaves a file uncommitted: the build check sends it back
                dirty.write_text("not committed\n")
                write_build(ctx, "READY")
                return
            dirty.unlink(missing_ok=True)
            default_build(ctx, call)

        factory = self.start_placed(builder=build, reviewer_code=reviews("CHANGES", "PASS"))
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        calls = self.recorder.of("builder")
        self.assertEqual([(call.ctx.model.key, call.kind) for call in calls],
                         [("flash", "run"), ("strong", "run"), ("strong", "resume")])
        self.assertTrue(calls[2].prompt.endswith("Commit or discard your uncommitted changes, then update build.md."
                                                 "\nPort: 5110."))


class PlacementReasonTest(PlacementTestCase):
    def test_overflow_and_escalation_runs_give_their_own_reason(self) -> None:
        self.reach.up_map.update({f"tabby{self.suffix}": False, f"strata{self.suffix}": False})
        job = self.run_job(self.start_placed(reviewer_code=reviews("CHANGES", "PASS")))
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        expected = [{"kind": "time", "text": "overflow from flash: no build answered"},
                    {"kind": "capability", "text": "escalation rung 1 of 2 after 1 send-back(s)"}]
        starts = [e for e in self.events(job, "agent.start") if e.agent == "builder"]
        self.assertEqual([e.data["reason"] for e in starts], expected, "not the route's 'cost: free locally'")
        self.assertEqual([run["reason"] for run in job["runs"] if run["agent"] == "builder"], expected)
        self.assertEqual(job["routing"]["builder"]["reason"], {"kind": "cost", "text": "free locally"},
                         "the snapshot keeps the route's own reason")

    def test_a_per_run_model_drops_the_profiles_reason(self) -> None:
        config = self.placed_config(profiles={"p": {"agents": {"builder": {"model": "flash",
                                                                           "reason": "cost: free on the local GPU"}}}},
                                    default_profile="p")
        factory = self.start_placed(config)
        self.assertNotIn("reason", factory.check_routing(REPO, {"builder": {"model": "strong"}})["builder"])
        same = factory.check_routing(REPO, {"builder": {"escalate": []}})["builder"]
        self.assertEqual(same["reason"], {"kind": "cost", "text": "free on the local GPU"})
        own = factory.check_routing(REPO, {"builder": {"model": "strong", "reason": "capability: a hard one"}})
        self.assertEqual(own["builder"]["reason"], {"kind": "capability", "text": "a hard one"})

    def test_a_claude_route_refuses_effort_minimal(self) -> None:
        config = self.placed_config(
            providers={"claude": {"type": "anthropic", "base_url": "https://api.anthropic.invalid", "api_key_env": None}},
            models={"claude-x": {"provider": "claude", "model": "claude-opus-5-5", "max_tokens": 1000}})
        factory = self.start_placed(config)
        with self.assertRaisesRegex(ValueError, "cannot route approver: effort 'minimal' is not taken by Anthropic"):
            factory.check_routing(REPO, {"approver": {"model": "claude-x", "effort": "minimal"}})
        route = factory.check_routing(REPO, {"approver": {"model": "claude-x", "effort": "low"}})["approver"]
        self.assertEqual(route["extra_body"]["output_config"], {"effort": "low"})


class IndependenceGuardTest(PlacementTestCase):
    """DESIGN §23, the independence guard: a judge whose model is of a family the builder ran on in its round
    runs on its independent alternate; without one the run goes ahead and job.json, agent.start and job.runs
    say its independence is reduced, naming the model it shares."""

    def guard_config(self, **sections: Any) -> Config:
        return self.placed_config(**deep_merge({
            "providers": {"lensp": {"type": "openai", "base_url": "https://lens.example.invalid/v1",
                                    "api_key_env": None}},
            "models": {"flash": {"family": "qwen"}, "cheap": {"family": "xiaomi"},
                       "strong": {"family": "openai"}, "stronger": {"family": "openai"},
                       "alt": {"provider": "api2", "model": "alt-1", "max_tokens": 1000, "family": "meta"},
                       "judge": {"provider": "api", "model": "judge-1", "max_tokens": 1000, "family": "anthropic"},
                       "lens": {"provider": "lensp", "model": "lens-1", "max_tokens": 1000, "family": "google",
                                "overflow": "local-lens"},
                       "local-lens": {"provider": "api2", "model": "local-lens-1", "max_tokens": 1000,
                                      "family": "qwen"}},
            "agents": {"reviewer-code": {"model": "strong", "independent_alternate": "alt"},
                       "approver": {"model": "judge"}},
        }, sections))

    def runs(self, job: dict, agent: str) -> list[dict]:
        return [run for run in job["runs"] if run["agent"] == agent]

    def test_after_an_escalation_the_code_lens_moves_to_its_alternate(self) -> None:
        factory = self.start_placed(self.guard_config(), reviewer_code=reviews("CHANGES", "PASS"))
        snapshot = factory.check_routing(REPO)
        code = snapshot["reviewer-code"]
        self.assertEqual((code["family"], code["independent_alternate"]["model"],
                          code["independent_alternate"]["family"]), ("openai", "alt", "meta"))
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertEqual([call.ctx.model.key for call in self.recorder.of("builder")], ["flash", "strong"])
        self.assertEqual([call.ctx.model.key for call in self.recorder.of("reviewer-code")], ["strong", "alt"],
                         "round 1's builder is local Qwen; round 2's is GPT, like the code lens")
        starts = [e.data for e in self.events(job, "agent.start") if e.agent == "reviewer-code"]
        self.assertNotIn("independence", starts[0])
        shared = {"model": "strong", "model_id": "strong-1", "family": "openai", "builder_model": "strong"}
        self.assertEqual((starts[1]["independence"], starts[1]["shared_model"], starts[1]["family"],
                          starts[1]["model"]), ("alternate", shared, "meta", "alt"))
        self.assertEqual(starts[1]["reason"], {"kind": "stage-fit", "text": "independent alternate: the builder ran "
                                                                            "on the openai family (strong) this round, "
                                                                            "like strong"})
        runs = self.runs(job, "reviewer-code")
        self.assertEqual([(run.get("independence"), run["model"], run["family"]) for run in runs],
                         [(None, "strong", "openai"), ("alternate", "alt", "meta")])
        self.assertEqual(runs[1]["shared_model"], shared, "the run's receipt names the model it left")
        self.assertEqual([run["family"] for run in self.runs(job, "builder")], ["qwen", "openai"])
        self.assertNotIn("independence", job, "no judge ran on the builder's family")
        self.assertIn("reviewer-code runs on api2/alt-1: runs on its independent alternate alt instead of strong: the "
                      "builder ran on the openai family (strong) in round 2", self.logs(job))
        self.assertEqual(self.start_event(job, "approver").data.get("independence"), None)

    def test_without_an_alternate_the_run_goes_ahead_reduced_and_says_so(self) -> None:
        config = self.guard_config(agents={"reviewer-code": {"model": "strong", "independent_alternate": None}})
        factory = self.start_placed(config, reviewer_code=reviews("CHANGES", "PASS"))
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertEqual([call.ctx.model.key for call in self.recorder.of("reviewer-code")], ["strong", "strong"])
        shared = {"model": "strong", "model_id": "strong-1", "family": "openai", "builder_model": "strong"}
        start = [e.data for e in self.events(job, "agent.start") if e.agent == "reviewer-code"][1]
        self.assertEqual((start["independence"], start["shared_model"]), ("reduced", shared))
        self.assertNotIn("reason", start, "a reduced run keeps its route's reason (none here)")
        run = self.runs(job, "reviewer-code")[1]
        self.assertEqual((run["independence"], run["shared_model"]), ("reduced", shared))
        self.assertEqual(job["independence"], "reduced")
        self.assertEqual(job["shared_models"], [{"round": 2, "agent": "reviewer-code", **shared}])
        saved = json.loads((Path(job["job_dir"]) / "job.json").read_text())
        self.assertEqual((saved["independence"], saved["shared_models"]), ("reduced", job["shared_models"]))
        self.assertIn("independence reduced: strong is of the openai family, like the builder's strong in round 2 "
                      "(it has no independent_alternate)", self.logs(job))

    def test_an_alternate_of_the_builder_s_family_too_does_not_help(self) -> None:
        """Round 1's builder runs on two families: flash (qwen), then, for the build check's resume with both
        local builds down, its overflow cheap (xiaomi). The code lens is Qwen and its alternate MiMo-like:
        neither is independent of that round, so the lens runs reduced and says why."""
        tabby, strata = f"tabby{self.suffix}", f"strata{self.suffix}"

        def builder(ctx: RunContext, call: Call) -> None:
            if call.index == 0:   # commits, but leaves a file uncommitted: the build check resumes it
                commit_file(ctx.cwd, "feature.txt", "work\n", "work")
                (ctx.cwd / "loose.txt").write_text("not committed\n")
                write_build(ctx, "READY")
                self.reach.up_map.update({tabby: False, strata: False})
            else:
                commit_file(ctx.cwd, "loose.txt", "committed\n", "commit the rest")

        config = self.guard_config(
            agents={"reviewer-code": {"model": "qwen-judge", "independent_alternate": "mimo-judge"}},
            models={"qwen-judge": {"provider": "api2", "model": "qj-1", "max_tokens": 1000, "family": "qwen"},
                    "mimo-judge": {"provider": "api2", "model": "mj-1", "max_tokens": 1000, "family": "xiaomi"}})
        factory = self.start_placed(config, builder=builder)
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertEqual([(run["round"], run["model"], run["family"]) for run in self.runs(job, "builder")],
                         [(1, "flash", "qwen"), (1, "cheap", "xiaomi")])
        self.assertEqual([call.ctx.model.key for call in self.recorder.of("reviewer-code")], ["qwen-judge"])
        run = self.runs(job, "reviewer-code")[0]
        self.assertEqual((run["independence"], run["shared_model"]["builder_model"]), ("reduced", "flash"))
        self.assertIn("independence reduced: qwen-judge is of the qwen family, like the builder's flash in round 1 "
                      "(its independent alternate mimo-judge is of a family the builder ran on too)", self.logs(job))
        config = self.guard_config(agents={"reviewer-code": {"model": "flash", "independent_alternate": "stronger2"}},
                                   models={"stronger2": {"provider": "api2", "model": "s2", "max_tokens": 1000,
                                                         "family": "qwen"}})
        with self.assertRaisesRegex(ValueError, "cannot route reviewer-code: independent_alternate 'stronger2' is of "
                                                "the qwen family, like 'flash'"):
            Factory(config, self.bus, reach=self.reach).check_routing(REPO)

    def test_the_approver_moves_too_and_a_profile_can_place_it(self) -> None:
        config = self.guard_config(profiles={"guarded": {"agents": {
            "approver": {"model": "cheap", "independent_alternate": "judge", "reason": "cost: a cheap gate"}}}},
            models={"cheap": {"family": "qwen"}})
        factory = self.start_placed(config)
        job = self.run_job(factory, profile="guarded")
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertEqual(self.recorder.of("approver")[0].ctx.model.key, "judge")
        start = self.start_event(job, "approver").data
        self.assertEqual((start["independence"], start["shared_model"]["model"], start["reason"]["kind"]),
                         ("alternate", "cheap", "stage-fit"))
        self.assertEqual(job["routing"]["approver"]["reason"], {"kind": "cost", "text": "a cheap gate"},
                         "the snapshot keeps the route's own reason")

    def test_an_overflow_onto_the_builder_s_family_is_reduced(self) -> None:
        self.reach.up_map["lensp"] = False
        factory = self.start_placed(self.guard_config(agents={"reviewer-code": {"model": "lens",
                                                                                "independent_alternate": None}}))
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertEqual(self.recorder.of("reviewer-code")[0].ctx.model.key, "local-lens")
        start = self.start_event(job, "reviewer-code").data
        self.assertEqual((start["independence"], start["shared_model"], start["reason"]["kind"]),
                         ("reduced", {"model": "local-lens", "model_id": "local-lens-1", "family": "qwen",
                                      "builder_model": "flash"}, "time"))
        self.assertEqual(job["shared_models"][0]["round"], 1)
        self.assertIn("independence reduced: local-lens is of the qwen family, like the builder's flash in round 1 "
                      "(its own model lens overflowed to local-lens, and it has no independent_alternate)",
                      self.logs(job))

    def test_an_overflow_onto_the_builder_s_family_takes_the_alternate(self) -> None:
        """The code lens (google) is down and overflows to a Qwen model, the builder's family: its independent
        alternate (meta) answers, so the lens runs there instead of reduced."""
        self.reach.up_map["lensp"] = False
        factory = self.start_placed(self.guard_config(agents={"reviewer-code": {"model": "lens",
                                                                                "independent_alternate": "alt"}}))
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertEqual([call.ctx.model.key for call in self.recorder.of("reviewer-code")], ["alt"])
        start = self.start_event(job, "reviewer-code").data
        left = {"model": "local-lens", "model_id": "local-lens-1", "family": "qwen", "builder_model": "flash"}
        self.assertEqual((start["independence"], start["shared_model"], start["family"]), ("alternate", left, "meta"))
        self.assertNotIn("overflow", start, "the run is not on the overflow")
        self.assertEqual(start["reason"], {"kind": "stage-fit", "text": "independent alternate: the builder ran on "
                                                                         "the qwen family (flash) this round, like "
                                                                         "local-lens"})
        self.assertNotIn("independence", job, "nothing ran reduced")
        logs = self.logs(job)
        self.assertIn("build lensp/lens-1 is unavailable", logs)
        self.assertIn("its overflow local-lens is of the qwen family the builder ran on; runs on its independent "
                      "alternate alt instead of local-lens: the builder ran on the qwen family (flash) in round 1",
                      logs)

    def test_with_its_alternate_down_the_overflow_runs_reduced_and_says_why(self) -> None:
        self.reach.up_map.update(lensp=False, altp=False)
        config = self.guard_config(
            providers={"altp": {"type": "openai", "base_url": "https://alt.example.invalid/v1", "api_key_env": None}},
            models={"alt": {"provider": "altp"}},
            agents={"reviewer-code": {"model": "lens", "independent_alternate": "alt"}})
        job = self.run_job(self.start_placed(config))
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertEqual([call.ctx.model.key for call in self.recorder.of("reviewer-code")], ["local-lens"])
        self.assertEqual(self.start_event(job, "reviewer-code").data["independence"], "reduced")
        self.assertIn("independence reduced: local-lens is of the qwen family, like the builder's flash in round 1 "
                      "(its own model lens overflowed to local-lens, and its independent alternate alt does not "
                      "answer)", self.logs(job))

    def test_an_alternate_that_does_not_answer_leaves_the_judge_on_its_own_model(self) -> None:
        """After an escalation the code lens shares the builder's family; its alternate is down, so it reviews on
        its own model (reduced, saying so) rather than failing on a server that does not answer."""
        self.reach.up_map["altp"] = False
        config = self.guard_config(
            providers={"altp": {"type": "openai", "base_url": "https://alt.example.invalid/v1", "api_key_env": None}},
            models={"alt": {"provider": "altp"}})
        factory = self.start_placed(config, reviewer_code=reviews("CHANGES", "PASS"))
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertEqual([call.ctx.model.key for call in self.recorder.of("reviewer-code")], ["strong", "strong"])
        self.assertEqual(self.runs(job, "reviewer-code")[1]["independence"], "reduced")
        self.assertIn("build altp/alt-1 is unavailable", self.logs(job))
        self.assertIn("independence reduced: strong is of the openai family, like the builder's strong in round 2 "
                      "(its independent alternate alt does not answer)", self.logs(job))

    def test_a_slot_wait_overflow_onto_the_builder_s_family_takes_the_alternate(self) -> None:
        strata = f"strata{self.suffix}"
        config = self.guard_config(
            providers={strata: {"overflow_after_seconds": 0.2}},
            models={"slow-lens": {"provider": strata, "model": "slow-lens-1", "max_tokens": 1000, "family": "google",
                                  "overflow": "local-lens"}},
            agents={"reviewer-code": {"model": "slow-lens", "independent_alternate": "alt"}})
        slots = provider_semaphore(strata, 1)
        held, done = threading.Event(), threading.Event()

        def hold() -> None:   # another run holds the lens's one local slot
            slots.acquire()
            held.set()
            done.wait(TIMEOUT)
            slots.release()

        holder = threading.Thread(target=hold, daemon=True)
        holder.start()
        self.addCleanup(holder.join, 5)
        self.addCleanup(done.set)
        held.wait(5)
        job = self.run_job(self.start_placed(config))
        done.set()
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertEqual([call.ctx.model.key for call in self.recorder.of("reviewer-code")], ["alt"],
                         "past the wait the overflow is Qwen, like the builder: the alternate takes over")
        start = self.start_event(job, "reviewer-code").data
        self.assertEqual((start["independence"], start["shared_model"]["model"]), ("alternate", "local-lens"))
        waits = [e for e in self.events(job, "agent.waiting") if e.agent == "reviewer-code"]
        self.assertEqual(len(waits), 1, "it waited for the slot first")

    def test_the_approver_is_compared_with_every_round(self) -> None:
        """The approver judges the whole branch once: round 1's code came from Claude, round 2's from GPT, so an
        approver on Claude moves to its alternate though round 2's builder is GPT."""
        config = self.guard_config(agents={"builder": {"model": "judge", "escalate": ["strong"]},
                                           "approver": {"model": "judge", "independent_alternate": "alt"}})
        factory = self.start_placed(config, reviewer_code=reviews("CHANGES", "PASS"))
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertEqual([(run["round"], run["family"]) for run in self.runs(job, "builder")],
                         [(1, "anthropic"), (2, "openai")])
        self.assertEqual([call.ctx.model.key for call in self.recorder.of("approver")], ["alt"])
        start = self.start_event(job, "approver").data
        self.assertEqual((start["independence"], start["shared_model"]["builder_model"]), ("alternate", "judge"))
        self.assertIn("runs on its independent alternate alt instead of judge: the builder ran on the anthropic family "
                      "(judge) in the rounds so far", self.logs(job))
        self.assertEqual(factory._builder_families(job, 2, "approver"), {"anthropic": "judge", "openai": "strong"})
        self.assertEqual(factory._builder_families(job, 2, "reviewer-code"), {"openai": "strong"},
                         "a reviewer is compared with its own round: each round's code met independent lenses")

    def chat_branch(self) -> str:
        branch = "chat/c-0123456789"
        run_git(self.repo, "branch", branch)
        worktree = self.tmp / "chat"
        run_git(self.repo, "worktree", "add", "-q", str(worktree), branch)
        commit_file(worktree, "chat.txt", "built in chat\n", "chat work")
        run_git(self.repo, "worktree", "remove", str(worktree))
        return branch

    def test_a_hand_off_counts_the_chat_s_models_as_round_one(self) -> None:
        config = self.guard_config(agents={"approver": {"model": "judge", "independent_alternate": "alt"}})
        factory = self.start_placed(config)
        chat = [{"model": "judge", "provider": "api", "model_id": "judge-1", "family": "anthropic"}]
        job = self.run_job(factory, "Ship the chat work", from_branch=self.chat_branch(), handoff_models=chat)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertEqual(job["handoff_models"], chat)
        self.assertEqual(self.runs(job, "builder"), [], "the chat wrote the code")
        self.assertEqual([call.ctx.model.key for call in self.recorder.of("approver")], ["alt"])
        self.assertEqual(self.start_event(job, "approver").data["shared_model"],
                         {"model": "judge", "model_id": "judge-1", "family": "anthropic", "builder_model": "judge"})
        self.assertNotIn("independence", self.start_event(job, "reviewer-code").data, "GPT is not Claude")
        with self.assertRaisesRegex(ValueError, "handoff_models must be a list"):
            factory.submit(REPO, "x", from_branch="chat/c-0123456789",
                           handoff_models="judge")   # type: ignore[arg-type]

    def test_a_hand_off_without_its_models_is_unknown(self) -> None:
        factory = self.start_placed(self.guard_config())
        job = self.run_job(factory, "Ship the chat work", from_branch=self.chat_branch())
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertEqual([self.start_event(job, agent).data.get("independence") for agent in ("reviewer-code",
                                                                                              "approver")],
                         ["unknown", "unknown"])
        self.assertEqual([run.get("independence") for run in job["runs"] if run["agent"] in ("reviewer-code",
                                                                                            "approver")],
                         ["unknown", "unknown"])
        self.assertEqual(job["independence"], "unknown")
        self.assertNotIn("shared_models", job, "there is no model to name")
        self.assertIn("independence unknown: the code it judges was written in a chat whose models the job does not "
                      "record", self.logs(job))

    def test_an_accepted_shared_family_is_not_a_reduction(self) -> None:
        """independent_alternate: false says the routing accepts this judge on the builder's family (say a local
        security fine-tune whose findings stay local): its run says "accepted", and so does the job."""
        config = self.guard_config(agents={"reviewer-code": {"model": "flash", "independent_alternate": False}})
        factory = self.start_placed(config)
        snapshot = factory.check_routing(REPO)
        self.assertIs(snapshot["reviewer-code"]["independent_alternate"], False)
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        start = self.start_event(job, "reviewer-code").data
        shared = {"model": "flash", "model_id": "flash-exl3", "family": "qwen", "builder_model": "flash"}
        self.assertEqual((start["independence"], start["shared_model"]), ("accepted", shared))
        self.assertEqual(job["independence"], "accepted")
        self.assertEqual(job["shared_models"], [{"round": 1, "agent": "reviewer-code", **shared, "accepted": True}])
        self.assertIn("independence accepted: flash is of the qwen family, like the builder's flash in round 1 (the "
                      "routing accepts it: independent_alternate is false)", self.logs(job))
        estimate = factory.estimate(REPO)
        self.assertEqual(estimate["independence"][0]["accepted"], {"reviewer-code": "flash"})
        row = next(row for row in estimate["roles"] if row["agent"] == "reviewer-code")
        self.assertEqual((row["model"], row["independence"]), ("flash", "accepted"))
        with self.assertRaisesRegex(ValueError, "routing for builder: independent_alternate is for the judges"):
            factory.check_routing(REPO, {"builder": {"independent_alternate": False}})

    def test_a_reduced_run_outranks_an_accepted_one_on_the_job(self) -> None:
        config = self.guard_config(agents={"reviewer-code": {"model": "flash", "independent_alternate": False},
                                           "reviewer-security": {"model": "flash"}},
                                   pipeline={"reviewers": ["code", "security"]})
        job = self.run_job(self.start_placed(config))
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertEqual(job["independence"], "reduced")
        self.assertEqual(sorted((entry["agent"], entry.get("accepted", False)) for entry in job["shared_models"]),
                         [("reviewer-code", True), ("reviewer-security", False)])

    def test_the_guard_compares_the_round_s_builder_runs_only(self) -> None:
        factory = self.start_placed(self.guard_config())
        job = {"repo": REPO, "id": "001-x", "runs": [
            {"agent": "builder", "round": 1, "model": "flash", "model_id": "Qwen3.8-x"},   # before families
            {"agent": "builder", "round": 2, "model": "strong", "model_id": "strong-1", "family": "openai"},
            {"agent": "builder", "round": 2, "model": "cheap", "model_id": "cheap-1", "family": "xiaomi"},
            {"agent": "reviewer-code", "round": 2, "model": "alt", "model_id": "alt-1", "family": "meta"}]}
        self.assertEqual(factory._builder_families(job, 1), {"qwen": "flash"})
        self.assertEqual(factory._builder_families(job, 2), {"openai": "strong", "xiaomi": "cheap"})
        self.assertEqual(factory._builder_families(job, 3), {}, "a round with no builder run: nothing to compare")
        self.assertEqual(factory._builder_families(job, None), {})

    def test_a_per_run_route_and_the_estimate(self) -> None:
        factory = self.start_placed(self.guard_config())
        with self.assertRaisesRegex(ValueError, "routing for builder: independent_alternate is for the judges"):
            factory.check_routing(REPO, {"builder": {"independent_alternate": "alt"}})
        snapshot = factory.check_routing(REPO, {"reviewer-code": {"model": "judge"}})
        self.assertNotIn("independent_alternate", snapshot["reviewer-code"], "agents.<name>'s alternate went with "
                                                                              "its model")
        own = factory.check_routing(REPO, {"reviewer-code": {"model": "judge", "independent_alternate": "alt"}})
        self.assertEqual(own["reviewer-code"]["independent_alternate"]["model"], "alt")
        estimate = factory.estimate(REPO)
        self.assertEqual([(row["builder"]["via"], row["builder"]["model"], row["alternate"], row["reduced"])
                          for row in estimate["independence"]],
                         [("route", "flash", {}, {}), ("overflow", "cheap", {}, {}),
                          ("rung 1", "strong", {"reviewer-code": "alt"}, {}),
                          ("rung 2", "stronger", {"reviewer-code": "alt"}, {})])
        code = next(row for row in estimate["roles"] if row["agent"] == "reviewer-code")
        self.assertEqual((code["model"], code.get("independence")), ("strong", None),
                         "on the builder's own model (Qwen) the code lens stays on its own")

    def test_the_estimate_prices_a_judge_where_the_guard_puts_it(self) -> None:
        config = self.guard_config(agents={"builder": {"model": "strong"},
                                           "reviewer-code": {"model": "strong", "independent_alternate": "alt"}},
                                   models={"alt": {"price_in": 3, "price_out": 7}})
        estimate = self.start_placed(config).estimate(REPO)
        code = next(row for row in estimate["roles"] if row["agent"] == "reviewer-code")
        self.assertEqual((code["model"], code["provider"], code["model_id"], code["independence"],
                          code["instead_of"]), ("alt", "api2", "alt-1", "alternate", "strong"))
        tokens = code["tokens"]
        self.assertAlmostEqual(code["cost_usd"], (tokens["input_tokens"] * 3 + tokens["output_tokens"] * 7) / 1e6,
                               places=5)
        self.assertEqual(code["reason"], {"kind": "stage-fit", "text": "independent alternate: the builder ran on "
                                                                        "the openai family (strong) this round, like "
                                                                        "strong"})
        self.assertTrue(any("priced on its independent alternate" in note for note in estimate["notes"]))

    def test_the_alternate_s_provider_secret_is_redacted(self) -> None:
        key = "sk-alternate-key-0123456789"
        config = self.guard_config(providers={"api2": {"api_key_env": "ALTERNATE_TEST_KEY"}},
                                   agents={"builder": {"model": "flash", "escalate": ["strong"]}})
        factory = self.start_placed(config)
        with mock.patch.dict(os.environ, {"ALTERNATE_TEST_KEY": key}):
            job = factory.submit(REPO, "x")
            factory.wait(REPO, job["id"], timeout=TIMEOUT)
            factory.reload_config(self.config())
            self.assertIn(key, factory.secret_values(), "the alternate's provider is only in the snapshot now")


class ScoutPlacementTest(PlacementTestCase):
    def scout_start(self, factory: Factory) -> Any:
        factory.scout(REPO)
        until(lambda: not factory.scout_status(REPO)["running"])
        return next(e for e in self.bus.since(0) if e.type == "agent.start" and e.agent == "repo-scout")

    def test_the_default_profile_places_the_scout_with_its_reason(self) -> None:
        config = self.placed_config(profiles={"p": {"agents": {"repo-scout": {"model": "strong",
                                                                              "reason": "time: a quick reader"}}}},
                                    default_profile="p")
        start = self.scout_start(self.start_placed(config))
        self.assertEqual((start.data["model"], start.data["provider"]), ("strong", "api"))
        self.assertEqual(start.data["reason"], {"kind": "time", "text": "a quick reader"})

    def test_the_scout_overflows_when_its_slot_stays_busy(self) -> None:
        tabby = f"tabby{self.suffix}"
        config = self.placed_config(providers={tabby: {"overflow_after_seconds": 0.2}},
                                    agents={"repo-scout": {"model": "flash", "reason": "cost: local"}})
        slots = provider_semaphore(tabby, 1)
        held, done = threading.Event(), threading.Event()

        def hold() -> None:
            slots.acquire()
            held.set()
            done.wait(TIMEOUT)
            slots.release()

        holder = threading.Thread(target=hold, daemon=True)
        holder.start()
        self.addCleanup(holder.join, 5)
        self.addCleanup(done.set)
        held.wait(5)
        start = self.scout_start(self.start_placed(config))
        self.assertEqual(start.data["model"], "cheap")
        self.assertIn(f"no slot of {tabby} came free within 0.2s", start.data["overflow"]["why"])
        self.assertEqual(start.data["reason"]["kind"], "time")
        self.assertTrue(any(e.type == "agent.waiting" and e.agent == "repo-scout" for e in self.bus.since(0)))


class ProfileTest(PlacementTestCase):
    def profile_config(self, **sections: Any) -> Config:
        return self.placed_config(**deep_merge({"profiles": {
            "thrifty": {"description": "Cheap approvals.", "agents": {
                "approver": {"model": "cheap", "effort": "low", "reason": "cost: approvals are short"},
                "builder": {"model": "flash", "escalate": ["stronger"], "reason": "cost: local"}}},
            "plain": {"agents": {}}}}, sections))

    def test_a_profile_applies_over_the_base_routing_and_is_recorded(self) -> None:
        factory = self.start_placed(self.profile_config())
        job = factory.submit(REPO, "x", profile="thrifty")
        self.assertEqual(job["profile"], "thrifty")
        approver = job["routing"]["approver"]
        self.assertEqual((approver["model"], approver["extra_body"]["reasoning_effort"]), ("cheap", "low"))
        self.assertEqual(approver["reason"], {"kind": "cost", "text": "approvals are short"})
        self.assertEqual([rung["model"] for rung in job["routing"]["builder"]["escalate"]], ["stronger"])
        self.assertEqual(job["routing"]["spec-writer"]["model"], "opus", "agents the profile leaves out keep theirs")
        over = factory.check_routing(REPO, {"approver": {"model": "strong"}}, profile="thrifty")["approver"]
        self.assertEqual((over["model"], over["extra_body"]["reasoning_effort"]), ("strong", "low"),
                         "a per-run route goes over the profile, field by field")
        with self.assertRaisesRegex(ValueError, "unknown profile 'lavish'"):
            factory.submit(REPO, "x", profile="lavish")
        self.assertEqual(factory.check_profile("thrifty"), "thrifty")

    def test_the_default_profile_and_none(self) -> None:
        factory = self.start_placed(self.profile_config(default_profile="thrifty"))
        self.assertEqual(factory.submit(REPO, "x")["profile"], "thrifty")
        plain = factory.submit(REPO, "y", profile="")
        self.assertIsNone(plain["profile"])
        self.assertEqual(plain["routing"]["approver"]["model"], "opus")
        self.assertEqual(factory.check_routing(REPO)["approver"]["model"], "cheap")

    def test_change_routing_keeps_the_job_profile_unless_told(self) -> None:
        factory = self.start_placed(self.profile_config(), builder=lambda ctx, call: write_build(ctx, "BLOCKED"))
        job = self.run_job(factory, profile="thrifty")
        self.assertEqual(job["stage"], "needs-human")
        job = factory.set_routing(REPO, job["id"], None)
        self.assertEqual((job["profile"], job["routing"]["approver"]["model"]), ("thrifty", "cheap"))
        job = factory.set_routing(REPO, job["id"], {"approver": {"model": "strong"}})
        self.assertEqual(job["routing"]["approver"]["extra_body"].get("reasoning_effort"), "low")
        job = factory.set_routing(REPO, job["id"], None, profile="")
        self.assertEqual((job["profile"], job["routing"]["approver"]["model"]), (None, "opus"))

    def test_a_bad_profile_never_breaks_check_routing_silently(self) -> None:
        config = self.profile_config()
        config.raw["profiles"]["broken"] = {"agents": {"approver": {"effort": "turbo"}}}
        factory = self.start_placed(config)
        with self.assertRaisesRegex(ValueError, "profile 'broken'.*effort must be one of"):
            factory.check_routing(REPO, profile="broken")


class CostTest(PlacementTestCase):
    def test_every_run_is_priced_and_the_job_keeps_the_total(self) -> None:
        config = self.placed_config(models={"opus": {"price_in": 1, "price_out": 2}},
                                    agents={"builder": {"model": "opus", "escalate": None}})
        job = self.run_job(self.start_placed(config))
        each = (95 * 1 + 5 * 1 + 10 * 2) / 1e6
        ends = {e.agent: e.data for e in self.events(job, "agent.end")}
        self.assertEqual((ends["builder"]["cost_usd"], ends["builder"]["priced"]), (each, True))
        self.assertEqual((ends["reviewer-code"]["cost_usd"], ends["reviewer-code"]["priced"]), (None, False),
                         "the sonnet tier on DeepSeek has no prices: unknown, not free")
        self.assertEqual(job["cost"], {"usd": round(3 * each, 6), "priced": False, "unpriced_runs": 1})
        self.assertEqual([entry["agent"] for entry in job["runs"]],
                         ["spec-writer", "builder", "reviewer-code", "approver"])
        self.assertEqual(job["runs"][1]["usage"], USAGE)
        updated = [e for e in self.events(job, "job.updated")][-1].data["job"]
        self.assertEqual(updated["cost"], job["cost"], "job events carry the total")

    def test_a_fully_priced_job(self) -> None:
        config = self.placed_config(models={"opus": {"price_in": 1, "price_out": 2},
                                            "sonnet": {"price_in": 1, "price_out": 2}},
                                    agents={"builder": {"model": "opus", "escalate": None}})
        job = self.run_job(self.start_placed(config))
        self.assertEqual(job["cost"]["priced"], True)
        self.assertEqual(job["cost"]["unpriced_runs"], 0)


class EstimateFactoryTest(PlacementTestCase):
    def test_defaults_then_this_repos_history(self) -> None:
        factory = self.start_placed(self.placed_config(agents={"builder": {"model": "opus", "escalate": None}}))
        first = factory.estimate(REPO)
        self.assertEqual((first["basis"], first["repo"], first["kind"]), ("defaults", REPO, "feature"))
        self.assertEqual({row["agent"] for row in first["roles"]}, {"spec-writer", "builder", "reviewer-code",
                                                                    "approver"})
        for _ in range(3):
            self.run_job(factory)
        later = factory.estimate(REPO)
        builder = next(row for row in later["roles"] if row["agent"] == "builder")
        self.assertEqual(builder["tokens"], {"input_tokens": 100, "output_tokens": 10, "cached_tokens": 5})
        self.assertEqual((builder["basis"], later["basis"]), ("median of 3 past jobs", "history"))
        plan = factory.estimate(REPO, "plan")
        self.assertEqual(plan["jobs"], routing.DEFAULT_PLAN_JOBS)
        self.assertIn("lead", {row["agent"] for row in plan["roles"]})
        with self.assertRaisesRegex(ValueError, "kind must be feature or plan"):
            factory.estimate(REPO, "epic")

    def test_a_profile_changes_the_estimate(self) -> None:
        config = self.placed_config(profiles={"api": {"agents": {"builder": {"model": "strong"}}}})
        factory = self.start_placed(config)
        local = next(row for row in factory.estimate(REPO)["roles"] if row["agent"] == "builder")
        api = next(row for row in factory.estimate(REPO, profile="api")["roles"] if row["agent"] == "builder")
        self.assertEqual((local["cost_usd"], local["local"]), (0.0, True))
        self.assertEqual((api["model"], api["local"]), ("strong", False))
        self.assertGreater(api["cost_usd"], 0)


# -- chats and plans ------------------------------------------------------------------------


class LivePlacementTest(unittest.TestCase):
    def test_a_chat_or_lead_route_runs_on_the_first_build_that_answers(self) -> None:
        from swfactory.chats import Route, placed_model

        config = Config(Path("/nonexistent/config.json"), {
            "providers": {"t": {**LOCAL, "base_url": "http://127.0.0.1:59003/v1"},
                          "s": {**LOCAL, "base_url": "http://127.0.0.1:59004/v1"}},
            "models": {"flash": {"provider": "t", "model": "f1", "builds": [{"provider": "s", "model": "f2"}],
                                 "overflow": "opus"}}})
        placed, info, notes = placed_model(config, Route("builtin", "flash", {"x": 1}), FakeReach(up={"t": False}))
        self.assertEqual((placed.provider.name, placed.model, placed.extra_body), ("s", "f2", {"x": 1}))
        self.assertEqual(info["build"]["index"], 1)
        placed, info, _ = placed_model(config, Route("builtin", "flash"), FakeReach(up={"t": False, "s": False}))
        self.assertEqual((placed.key, info["overflow"]["to"]), ("opus", "opus"))
        placed, info, _ = placed_model(config, Route("qwen", "flash"), FakeReach(up={"t": False}))
        self.assertEqual((placed.provider.name, info), ("t", {}), "a CLI driver keeps its model")



class ChatPlacementTest(ChatTestCase):
    def test_a_turn_runs_on_the_first_build_that_answers_and_is_priced(self) -> None:
        from swfactory.chats import ChatManager

        self.config.raw["providers"].update(
            t={**LOCAL, "base_url": "http://127.0.0.1:59005/v1"}, s={**LOCAL, "base_url": "http://127.0.0.1:59006/v1"})
        self.config.raw["models"]["flash"] = {"provider": "t", "model": "f1", "builds": [{"provider": "s", "model": "f2"}],
                                              "overflow": "opus"}
        self.config.raw["models"]["opus"].update(price_in=1, price_out=2)
        self.factory.reach = FakeReach(up={"t": False, "s": False})   # ChatManager uses the factory's probes
        manager = ChatManager(self.config, self.bus, self.factory, runner_factory=self.runners)
        self.addCleanup(manager.stop)
        chat = manager.new_chat("app", model="flash")
        manager.chat_send(chat["id"], "hello")
        wait_until = time.monotonic() + TIMEOUT
        while manager.chat(chat["id"])["status"] == "running" and time.monotonic() < wait_until:
            time.sleep(0.02)
        model = self.runners.calls[0].ctx.model
        self.assertEqual((model.key, model.provider.name), ("opus", "deepseek"), "both builds down: the overflow")
        done = manager.chat(chat["id"])
        self.assertEqual(done["cost"], {"usd": round((8 * 1 + 2 * 1 + 5 * 2) / 1e6, 6), "priced": True,
                                        "unpriced_runs": 0})
        start = next(e for e in self.bus.since(0) if e.type == "agent.start" and e.data.get("chat") == chat["id"])
        self.assertEqual((start.data["overflow"]["to"], start.data["model"]), ("opus", "opus"))

    def test_a_handoff_passes_the_profile(self) -> None:
        chat = self.manager.new_chat("app")
        commit_file(Path(chat["worktree"]), "feature.txt", "done\n", message="add feature")
        self.manager.chat_handoff(chat["id"], "Ship", profile="best")
        self.assertEqual(self.factory.submitted[0]["profile"], "best")


def session_writer(call: Any) -> AgentRun:
    """A ScriptedRunners handler that keeps a builtin-style session file (what transcript reads)."""
    path = Path(call.session_ref or call.ctx.session_path)
    data = json.loads(path.read_text(encoding="utf-8")) if call.kind == "resume" else {"messages": []}
    data["messages"] += [{"role": "user", "content": call.prompt},
                         {"role": "assistant", "content": f"answer from {call.ctx.model.provider.name}"}]
    path.write_text(json.dumps(data), encoding="utf-8")
    return AgentRun(ok=True, session_ref=str(path), usage={"input_tokens": 10, "output_tokens": 5, "calls": 1})


class ChatModelSwitchTest(ChatTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.suffix = uuid.uuid4().hex[:6]   # provider slots are process-wide by name
        self.t, self.s = f"t{self.suffix}", f"s{self.suffix}"
        self.config.raw["providers"].update({
            self.t: {**LOCAL, "base_url": "http://127.0.0.1:59105/v1"},
            self.s: {**LOCAL, "base_url": "http://127.0.0.1:59106/v1"}})
        self.config.raw["models"]["flash"] = {"provider": self.t, "model": "f1", "overflow": "opus",
                                              "builds": [{"provider": self.s, "model": "f2"}]}
        self.config.raw.setdefault("agents", {})["coder"] = {"model": "flash", "reason": "cost: interactive, local"}
        self.reach = FakeReach()
        self.factory.reach = self.reach
        self.runners.handler = session_writer
        self.chats = ChatManager(self.config, self.bus, self.factory, runner_factory=self.runners)
        self.addCleanup(self.chats.stop)

    def turn(self, chat_id: str, text: str) -> dict:
        self.chats.chat_send(chat_id, text)
        until(lambda: self.chats.chat(chat_id)["status"] != "running")
        return self.chats.chat(chat_id)

    def starts(self, chat_id: str) -> list[Any]:
        return [e for e in self.bus.since(0) if e.type == "agent.start" and e.data.get("chat") == chat_id]

    def test_a_turn_on_another_model_starts_a_fresh_session_with_a_hand_over(self) -> None:
        chat = self.chats.new_chat("app")
        self.turn(chat["id"], "turn one")
        self.reach.up_map.update({self.t: False, self.s: False})   # the local servers go down: the overflow
        after = self.turn(chat["id"], "turn two")
        self.turn(chat["id"], "turn three")                        # still on the overflow: it resumes
        calls = self.runners.calls
        self.assertEqual([(c.kind, c.ctx.model.key, c.ctx.model.provider.name) for c in calls],
                         [("run", "flash", self.t), ("run", "opus", "deepseek"), ("resume", "opus", "deepseek")])
        handover = calls[1].prompt
        for needle in (f"Hand-over: this chat ran on {self.t}/f1 until now", "deepseek/deepseek-v4-pro",
                       f"on the branch chat/{chat['id']}", "User: turn one", f"Assistant: answer from {self.t}",
                       "The user's new message:\nturn two"):
            self.assertIn(needle, handover)
        self.assertEqual(calls[2].prompt, "turn three")
        self.assertEqual(after["earlier_sessions"][0]["file"], "sessions/01.json")
        self.assertEqual((after["earlier_sessions"][0]["provider"], after["session_model"]["provider"]),
                         (self.t, "deepseek"))
        self.assertTrue((self.chats.root / chat["id"] / "sessions" / "01.json").is_file())
        messages = [(m["role"], m["text"]) for m in self.chats.chat(chat["id"])["messages"]]
        self.assertEqual([text for role, text in messages if role == "user"], ["turn one", "turn two", "turn three"],
                         "both sessions show, the hand-over as the user's own words")
        starts = self.starts(chat["id"])
        self.assertEqual([bool(e.data.get("handover")) for e in starts], [False, True, False])
        self.assertEqual(starts[0].data["reason"], {"kind": "cost", "text": "interactive, local"})
        self.assertEqual(starts[1].data["reason"], {"kind": "time", "text": "overflow from flash: no build answered"})

    def test_another_build_of_the_model_is_another_model_too(self) -> None:
        chat = self.chats.new_chat("app")
        self.turn(chat["id"], "one")
        self.reach.up_map[self.t] = False
        self.turn(chat["id"], "two")
        self.assertEqual([(c.kind, c.ctx.model.model) for c in self.runners.calls], [("run", "f1"), ("run", "f2")])
        self.assertIn(f"Hand-over: this chat ran on {self.t}/f1", self.runners.calls[1].prompt)

    def test_an_older_chat_without_a_session_model_resumes(self) -> None:
        chat = self.chats.new_chat("app")
        self.turn(chat["id"], "one")
        with self.chats._lock:
            stored = self.chats._load(chat["id"])
            stored.pop("session_model")
            self.chats._save(stored)
        self.reach.up_map[self.t] = False
        self.turn(chat["id"], "two")
        self.assertEqual([c.kind for c in self.runners.calls], ["run", "resume"])

    def hold_slot(self) -> threading.Event:
        slots = provider_semaphore(self.t, 1)
        held, done = threading.Event(), threading.Event()

        def hold() -> None:
            slots.acquire()
            held.set()
            done.wait(TIMEOUT)
            slots.release()

        holder = threading.Thread(target=hold, daemon=True)
        holder.start()
        self.addCleanup(holder.join, 5)
        self.addCleanup(done.set)
        held.wait(5)
        return done

    def test_a_turn_overflows_when_the_slot_stays_busy(self) -> None:
        self.config.raw["providers"][self.t]["overflow_after_seconds"] = 0.2
        self.hold_slot()
        chat = self.chats.new_chat("app")
        self.turn(chat["id"], "hello")
        self.assertEqual(self.runners.calls[0].ctx.model.key, "opus")
        start = self.starts(chat["id"])[0]
        self.assertIn(f"no slot of {self.t} came free within 0.2s", start.data["overflow"]["why"])
        turn = [e.type for e in self.bus.since(0) if e.data.get("run") == start.data["run"]]
        self.assertEqual(turn[:2], ["agent.waiting", "agent.start"])

    def test_a_turn_cancelled_while_waiting_never_runs_and_holds_no_slot(self) -> None:
        self.config.raw["providers"][self.t]["overflow_after_seconds"] = 0.1
        self.reach.up_map["deepseek"] = False   # the overflow is down too: the turn waits for the slot
        done = self.hold_slot()
        chat = self.chats.new_chat("app")
        self.chats.chat_send(chat["id"], "hello")
        until(lambda: any(e.type == "agent.waiting" and e.data.get("chat") == chat["id"] for e in self.bus.since(0)))
        time.sleep(0.3)
        self.chats.chat_cancel(chat["id"])
        done.set()
        until(lambda: self.chats.chat(chat["id"])["status"] != "running")
        self.assertEqual((self.chats.chat(chat["id"])["error"], self.runners.calls), ("cancelled", []))
        slots = provider_semaphore(self.t, 1)
        self.assertTrue(slots.acquire(timeout=2))
        slots.release()


class ChatProfileTest(ChatTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.config.raw["profiles"] = {"quick": {"agents": {"coder": {"model": "sonnet", "effort": "low",
                                                                      "reason": "time: answers fast"}}},
                                       "plain": {"agents": {}}}
        self.config.raw["default_profile"] = "quick"

    def test_the_default_profile_places_a_new_chat(self) -> None:
        chat = self.manager.new_chat("app", mode="repo")
        self.assertEqual((chat["model"], chat["profile"], chat["reason"]),
                         ("sonnet", "quick", {"kind": "time", "text": "answers fast"}))
        self.assertEqual(chat["extra_body"]["reasoning_effort"], "low")
        self.manager.chat_send(chat["id"], "hi")
        self.settle(chat["id"])
        start = next(e for e in self.bus.since(0) if e.type == "agent.start" and e.data.get("chat") == chat["id"])
        self.assertEqual(start.data["reason"], {"kind": "time", "text": "answers fast"})

    def test_a_named_profile_none_and_an_explicit_model(self) -> None:
        self.assertEqual(self.manager.new_chat("app", mode="repo", profile="plain")["profile"], "plain")
        none = self.manager.new_chat("app", mode="repo", profile="")
        self.assertEqual((none["profile"], none["model"], none["reason"]), (None, "opus", None))
        mine = self.manager.new_chat("app", model="haiku", mode="repo")
        self.assertEqual((mine["model"], mine["reason"]), ("haiku", None), "the profile's reason was for sonnet")
        with self.assertRaisesRegex(ValueError, "unknown profile 'gone'"):
            self.manager.new_chat("app", mode="repo", profile="gone")


class PlanProfileTest(PlanTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.config.raw["profiles"] = {"short": {"agents": {"lead": {"model": "sonnet", "reason": "cost: short plans"}}}}
        self.config.raw["default_profile"] = "short"
        self.config.raw["models"]["opus"].update(price_in=1, price_out=2)

    def test_the_plan_keeps_its_profile_for_its_jobs_and_its_lead(self) -> None:
        plan = self.ready_plan(PLAN_2)
        self.assertEqual(plan["profile"], "short", "the default, resolved when the plan is created")
        self.assertEqual(self.lead.seen[0]["call"].ctx.model.key, "sonnet", "the profile's lead placement")
        start = next(e for e in self.bus.since(0) if e.type == "agent.start" and e.agent == "lead")
        self.assertEqual(start.data["reason"], {"kind": "cost", "text": "short plans"})
        self.assertEqual(plan["cost"], {"usd": 0.0, "priced": False, "unpriced_runs": 1})
        self.manager.run("app", plan["id"])
        self.wait_submitted(plan, 1)
        self.assertEqual(self.factory.submitted[0]["profile"], "short")

    def test_no_profile_and_a_priced_lead(self) -> None:
        self.lead.plans.append(PLAN_2)
        plan = self.wait_stage(self.manager.create("app", "Two steps", profile=""), "ready")
        self.assertIsNone(plan["profile"])
        self.assertEqual(self.lead.seen[0]["call"].ctx.model.key, "opus")
        self.assertEqual(plan["cost"], {"usd": round((100 + 10 * 2) / 1e6, 6), "priced": True, "unpriced_runs": 0})
        self.manager.run("app", plan["id"])
        self.wait_submitted(plan, 1)
        self.assertEqual(self.factory.submitted[0]["profile"], "", "its jobs run without a profile too")
        with self.assertRaisesRegex(ValueError, "unknown profile 'gone'"):
            self.manager.create("app", "x", profile="gone")


# -- doctor ------------------------------------------------------------------------------------


class DoctorTest(unittest.TestCase):
    def setUp(self) -> None:
        home = Path(tempfile.mkdtemp(prefix="sf-routing-doctor-"))
        self.addCleanup(shutil.rmtree, home, True)
        patcher = mock.patch.dict(os.environ, {"HOME": str(home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def tiers(provider_name: str, model_id: str) -> dict:
        return {tier: {"provider": provider_name, "model": model_id, "extra_body": None}
                for tier in ("opus", "sonnet", "haiku")}

    def test_model_ids_the_server_does_not_list_and_the_loaded_model(self) -> None:
        with MockLLM(model_ids=["mock-model", "other"], loaded_model="mock-model") as llm:
            config = Config(Path("/nonexistent/config.json"), {
                "providers": {"tabbymock": {**LOCAL, "base_url": llm.base_url + "/v1", "kind": "tabby"},
                              "down": {**LOCAL, "base_url": "http://127.0.0.1:9/v1"}},
                "models": {"good": {"provider": "tabbymock", "model": "mock-model",
                                    "builds": [{"provider": "down", "model": "mock-gguf"}]},
                           "typo": {"provider": "tabbymock", "model": "mock-modle"},
                           **self.tiers("tabbymock", "mock-model")},
                "agents": {"*": {"driver": "builtin", "model": "good"}, "approver": {"model": "typo"}}})
            checks = {c["name"]: c for c in doctor.run_checks(config)}
        models = checks["provider tabbymock models"]
        self.assertEqual((models["ok"], models["level"]), (False, "warn"))
        self.assertIn("does not list mock-modle", models["detail"])
        loaded = checks["provider tabbymock loaded model"]
        self.assertEqual(loaded["level"], "warn")
        self.assertIn("mock-model loaded; with allow_model_swap off, runs on mock-modle", loaded["detail"])
        self.assertIn("used by good build 2", checks["provider down reachable"]["detail"],
                      "a build's provider is checked like a routed one")
        servers = checks["local servers"]
        self.assertEqual(servers["detail"], "up: tabbymock (mock-model loaded); down: down; "
                         + doctor.LOOK_AROUND_ADVICE, "with a server down it says how to look, and never looks itself")
        self.assertIn("1 extra builds", checks["placement"]["detail"])

    def test_bad_references_are_an_error(self) -> None:
        config = Config(Path("/nonexistent/config.json"), {
            "providers": {"dead": {**LOCAL, "base_url": "http://127.0.0.1:9/v1"}},
            "models": self.tiers("dead", "x"),
            "profiles": {"p": {"agents": {"approver": {"model": "missing"}}}}})
        checks = {c["name"]: c for c in doctor.run_checks(config)}
        self.assertEqual(checks["placement"]["level"], "error")
        self.assertIn("profiles.p.agents.approver.model 'missing'", checks["placement"]["detail"])


# -- HTTP --------------------------------------------------------------------------------------


class ProfileHttpFactory(HttpFactory):
    """The server's FakeFactory with the profile arguments and estimate."""

    def submit(self, repo: str, feature: str, from_branch: str | None = None, routing: Any = None,
               delivery: str | None = None, profile: str | None = None) -> dict:
        self._record("submit", repo, feature, profile=profile)
        return {"id": "002-new-job", "repo": repo, "profile": profile}

    def create_plan(self, repo: str, request: str, routing: Any = None, delivery: str | None = None,
                    profile: str | None = None) -> dict:
        self._record("create_plan", repo, request, profile=profile)
        return {"id": "P002-x", "repo": repo}

    def chat_handoff(self, chat_id: str, feature: str, delivery: str | None = None, profile: str | None = None) -> dict:
        self._record("chat_handoff", chat_id, feature, profile=profile)
        return {"id": "003-x"}

    def set_routing(self, repo: str, job_id: str, routing: Any, profile: str | None = None) -> dict:
        self._record("set_routing", repo, job_id, routing, profile=profile)
        return {"id": job_id}

    def new_chat(self, repo: str, model: str | None, mode: str = "worktree", agent: str = "coder",
                 profile: str | None = None) -> dict:
        self._record("new_chat", repo, model, mode, profile=profile)
        return {"id": "c-0123456789", "repo": repo, "mode": mode}

    def estimate(self, repo: str, kind: str = "feature", profile: str | None = None, routing: Any = None) -> dict:
        self._record("estimate", repo, kind, profile, routing)
        return {"repo": repo, "kind": kind, "profile": profile}


class HttpTest(ServerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.factory = ProfileHttpFactory(self.config, self.bus)
        self.port = self.start_server(self.config, self.factory, self.bus).server_port

    def kwargs(self, name: str) -> list[dict]:
        return [kwargs for called, _, kwargs in self.factory.calls if called == name]

    def test_profile_is_passed_on_and_estimates_are_served(self) -> None:
        self.assertEqual(self.request("POST", "/api/jobs", {"repo": "demo", "feature": "x", "profile": "best"}).status,
                         201)
        self.request("POST", "/api/jobs", {"repo": "demo", "feature": "x", "profile": ""})
        self.request("POST", "/api/jobs", {"repo": "demo", "feature": "x"})
        self.assertEqual([k.get("profile") for k in self.kwargs("submit")], ["best", "", None])
        self.request("POST", "/api/plans", {"repo": "demo", "request": "r", "profile": "thrifty"})
        self.request("POST", "/api/chats/c-0123456789/handoff", {"feature": "f", "profile": "fastest"})
        self.request("POST", "/api/jobs/demo/001-first-job/routing", {"routing": None, "profile": "best"})
        self.assertEqual(self.request("POST", "/api/chats", {"repo": "demo", "profile": "quick"}).status, 201)
        self.request("POST", "/api/chats", {"repo": "demo"})
        self.assertEqual([self.kwargs(name)[0]["profile"] for name in ("create_plan", "chat_handoff", "set_routing",
                                                                        "new_chat")],
                         ["thrifty", "fastest", "best", "quick"])
        self.assertIsNone(self.kwargs("new_chat")[1]["profile"], "no profile given: the default")
        self.assert_error(self.request("POST", "/api/jobs", {"repo": "demo", "feature": "x", "profile": 3}), 400,
                          "'profile' must be")
        response = self.request("GET", "/api/estimate?repo=demo&kind=plan&profile=best&routing=hybrid")
        self.assertEqual(response.json(), {"repo": "demo", "kind": "plan", "profile": "best"})
        self.assertEqual(self.factory.called("estimate")[-1], ("demo", "plan", "best", "hybrid"))
        self.assertEqual(self.request("GET", "/api/estimate?repo=demo").status, 200)
        self.assertEqual(self.factory.called("estimate")[-1], ("demo", "feature", None, None))
        self.assert_error(self.request("GET", "/api/estimate"), 400, "repo")
        self.assert_error(self.request("GET", "/api/estimate?repo=demo&routing=../x"), 400, "preset name")


if __name__ == "__main__":
    unittest.main()
