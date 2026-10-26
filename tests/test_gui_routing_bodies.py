"""The bodies the GUI's Change routing dialog sends (swfactory/web/app.js routingChooser, views/routing.js
keepPlacements and alternatesField), run on the real Factory: what each one keeps of the placement.

- "Saved settings" sends {routing: null}: every step is placed again from the saved config, agents.<name>'s effort,
  escalation, reason and alternate included (each step's saved model sent explicitly would drop them:
  Factory._resolve_route uses agents.<name>'s placement only when the model comes from config).
- Picking only a profile (Custom changes nothing) sends {routing: null, profile}: the saved settings with it.
- Custom, a step kept on its snapshot model (a new driver, or only a new alternate), without a new profile: the
  route carries the snapshot's reason, escalation rungs and the judge's alternate along with the model.
- Custom moves a judge to another model and its alternate says "Keep": the alternate's key goes with the route.
"""
from __future__ import annotations

import unittest
from typing import Any

from swfactory.config import deep_merge
from tests import test_routing   # the module, not its classes: their tests are not run again from here
from tests.test_orchestrator import REPO, write_build


def keep_placements(route: dict, entry: dict, judge: bool) -> dict:
    """views/routing.js keepPlacements for one step that stays on its snapshot model."""
    out = dict(entry)
    if "reason" not in out and route.get("reason"):
        out["reason"] = {"kind": route["reason"]["kind"], "text": route["reason"].get("text", "")}
    rungs = [rung["model"] for rung in route.get("escalate") or []]
    if "escalate" not in out and rungs:
        out["escalate"] = rungs
    alt = route.get("independent_alternate")
    if judge and "independent_alternate" not in out:
        if alt is False:
            out["independent_alternate"] = False
        elif isinstance(alt, dict):
            out["independent_alternate"] = alt["model"]
    return out


class ChangeRoutingBodies(test_routing.PlacementTestCase):
    profile_config = test_routing.ProfileTest.profile_config

    def stopped(self, config: Any = None, **kwargs: Any) -> tuple[Any, dict]:
        factory = self.start_placed(config or self.profile_config(), builder=lambda ctx, call: write_build(ctx, "BLOCKED"))
        job = self.run_job(factory, **kwargs)
        self.assertEqual(job["stage"], "needs-human")
        return factory, job

    def test_saved_settings_is_null_and_keeps_the_saved_placement(self) -> None:
        factory, job = self.stopped(profile="")
        # the snapshot moves away from the saved placement first (Custom: the builder on another model)
        job = factory.set_routing(REPO, job["id"], {"builder": {"model": "strong", "driver": "builtin"}})
        self.assertNotIn("escalate", job["routing"]["builder"])
        job = factory.set_routing(REPO, job["id"], None)   # what "Saved settings" now sends
        builder = job["routing"]["builder"]
        self.assertEqual(builder["model"], "flash")
        self.assertEqual([rung["model"] for rung in builder["escalate"]], ["strong", "stronger"])
        self.assertEqual(builder["reason"], {"kind": "cost", "text": "free locally"})

    def test_saved_settings_or_only_a_profile_applies_the_profile(self) -> None:
        factory, job = self.stopped(profile="")
        self.assertEqual(job["routing"]["approver"]["model"], "opus")
        job = factory.set_routing(REPO, job["id"], None, profile="thrifty")
        self.assertEqual((job["profile"], job["routing"]["approver"]["model"]), ("thrifty", "cheap"))
        self.assertEqual(job["routing"]["approver"]["reason"], {"kind": "cost", "text": "approvals are short"})

    def test_a_profile_with_custom_steps_places_the_others_again(self) -> None:
        factory, job = self.stopped(profile="")
        job = factory.set_routing(REPO, job["id"], {"spec-writer": {"model": "strong", "driver": "builtin"}}, profile="thrifty")
        self.assertEqual((job["routing"]["spec-writer"]["model"], job["routing"]["approver"]["model"]), ("strong", "cheap"))

    def test_a_driver_only_change_keeps_the_builders_ladder_and_reason(self) -> None:
        factory, job = self.stopped(profile="")
        before = job["routing"]["builder"]
        entry = keep_placements(before, {"model": "flash", "driver": "builtin", "extra_body": before["extra_body"]}, judge=False)
        job = factory.set_routing(REPO, job["id"], {"builder": entry})
        after = job["routing"]["builder"]
        self.assertEqual([rung["model"] for rung in after["escalate"]], ["strong", "stronger"])
        self.assertEqual(after["reason"], before["reason"])
        without = factory.set_routing(REPO, job["id"], {"builder": {"model": "flash", "driver": "builtin"}})["routing"]["builder"]
        self.assertNotIn("escalate", without, "the explicit model alone drops agents.builder's ladder: why the GUI sends it")

    def alt_config(self) -> Any:
        return self.profile_config(**deep_merge({}, {
            "models": {"alt": {"provider": "api2", "model": "alt-1", "max_tokens": 1000, "family": "meta"},
                       "cheap": {"family": "xiaomi"}, "strong": {"family": "openai"}, "strong2": {"provider": "api", "model": "strong-2", "max_tokens": 1000, "family": "openai"}},
            "agents": {"approver": {"model": "cheap", "independent_alternate": "alt",
                                    "reason": {"kind": "capability", "text": "the merge gate"}}},
        }))

    def test_only_a_new_alternate_keeps_the_judges_reason(self) -> None:
        factory, job = self.stopped(self.alt_config(), profile="")
        before = job["routing"]["approver"]
        entry = {"model": "cheap", "driver": before["driver"], "independent_alternate": "strong"}   # alternatesField
        if before.get("extra_body"):
            entry["extra_body"] = before["extra_body"]
        job = factory.set_routing(REPO, job["id"], {"approver": keep_placements(before, entry, judge=True)})
        after = job["routing"]["approver"]
        self.assertEqual((after["model"], after["independent_alternate"]["model"]), ("cheap", "strong"))
        self.assertEqual(after["reason"], {"kind": "capability", "text": "the merge gate"})

    def test_keep_carries_the_alternate_to_the_judges_new_model(self) -> None:
        factory, job = self.stopped(self.alt_config(), profile="")
        self.assertEqual(job["routing"]["approver"]["independent_alternate"]["model"], "alt")
        job = factory.set_routing(REPO, job["id"], {"approver": {"model": "strong", "driver": "builtin", "independent_alternate": "alt"}})
        after = job["routing"]["approver"]
        self.assertEqual((after["model"], after["independent_alternate"]["model"]), ("strong", "alt"))
        dropped = factory.set_routing(REPO, job["id"], {"approver": {"model": "strong2", "driver": "builtin"}})["routing"]["approver"]
        self.assertNotIn("independent_alternate", dropped, "without it the server drops the alternate: why the GUI sends it")
        with self.assertRaisesRegex(ValueError, "name a model of another family"):
            factory.set_routing(REPO, job["id"], {"approver": {"model": "strong", "driver": "builtin", "independent_alternate": "strong2"}})


if __name__ == "__main__":
    unittest.main()
