"""Tests for where the v1 features meet: the routing engine (DESIGN.md section 23), the Housekeeping Tracker
(section 32) and Grid delivery with the (product) confirmations (sections 7 and 26).

Each feature has its own suite; these pin the interactions: an escalated builder's fresh session still gets
its items and the Grid Card, a person's confirmation and the tracker's decision item stay one answer, a chat
that moves to another model numbers its turns across sessions, and the card and the map state the gate's
items rule only while the tracker holds jobs.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from swfactory import docs
from swfactory.chats import ChatManager
from swfactory.gridcard import HERE, factory_grid
from swfactory.gridcard import receipt as grid_receipt
from swfactory.runners import AgentRun, RunContext
from tests.test_chats import ChatTestCase
from tests.test_orchestrator import (CONFIRMED_FREE, FREE, REPO, TIMEOUT, Call, FactoryTestCase, build, escalate,
                                     product_spec, reviews, until, verifying_reviewer)
from tests.test_routing import LOCAL, FakeReach, PlacementTestCase, session_writer

CRITERIA_SPEC = "# Spec\n\nAdd a greeting.\n\n## Acceptance criteria\n1. The page says Hello.\n"


def criteria_spec(ctx: RunContext, call: Call) -> None:
    (ctx.job_dir / "spec.md").write_text(CRITERIA_SPEC)


class EscalatedBuilderTest(PlacementTestCase):
    """A builder escalated to another model starts a fresh session with a hand-over prompt (section 23); that
    session still lists its open items (section 32) and carries the job's Grid Card (section 26)."""

    def test_the_hand_over_session_keeps_its_items_and_the_card(self) -> None:
        factory = self.start_placed(spec_writer=criteria_spec, reviewer_code=verifying_reviewer("CHANGES", "PASS"))
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        first, second = self.recorder.of("builder")
        self.assertEqual((first.ctx.model.key, second.ctx.model.key, second.kind), ("flash", "strong", "run"))
        handover = second.prompt
        self.assertIn("Hand-over: you take over this job's build from", handover)
        task = handover.split("Your task now:\n", 1)[1]
        self.assertRegex(task, r"^Items: \S+/items\.md \(2 open; you own I-2\)\.\n",
                         "the items lines open the task, after the hand-over's quotes")
        self.assertIn("greet() returns hi, the spec says hello", task, "the compact list names the finding")
        self.assertIn("Round 1 reviews are in", task)
        self.assertIsNotNone(second.ctx.items, "the fresh session's builtin loop re-shows the items")
        self.assertEqual(second.ctx.grid, first.ctx.grid, "a new model gets the same card")
        self.assertEqual(second.ctx.grid, factory_grid(job, "builder"))
        self.assertEqual(second.ctx.grid.count(HERE), 1)
        starts = [e for e in self.events(job, "agent.start") if e.agent == "builder"]
        self.assertTrue(starts[1].data["handover"])
        self.assertEqual(starts[1].data["escalation"]["rung"], 1)
        self.assertEqual(starts[1].data["grid"], grid_receipt(second.ctx.grid))
        runs = [run for run in job["runs"] if run["agent"] == "builder"]
        self.assertEqual([run["grid"] for run in runs], [grid_receipt(first.ctx.grid)] * 2,
                         "job.runs keeps the receipt of every run")
        self.assertEqual(job["agents"]["builder"]["grid"], runs[-1]["grid"])
        self.assertEqual(job["agents"]["builder"]["model"], "strong")
        self.assertEqual(job["items"]["blocking_open"], 0, "the escalated builder's answers proved the items")

    def test_the_hand_over_quotes_the_rework_note_its_step_names(self) -> None:
        """The items lines come before the step's line and may name an earlier note: the hand-over quotes
        the note the step names."""
        factory = self.start_placed(self.placed_config(pipeline={"max_resumes": 0}),
                                    reviewer_code=reviews("CHANGES", "CHANGES", "PASS"))
        job = self.run_job(factory)
        factory.rework(REPO, job["id"], "Keep the old API.")
        job = factory.wait(REPO, job["id"], timeout=TIMEOUT)
        factory.add_item(f"job:{REPO}/{job['id']}", title="Check that rework-1.md still holds", owner="builder")
        factory.rework(REPO, job["id"], "Second note: rename the flag.")
        job = factory.wait(REPO, job["id"], timeout=TIMEOUT)
        calls = self.recorder.of("builder")
        self.assertEqual([call.ctx.model.key for call in calls], ["flash", "strong", "stronger"])
        task = calls[2].prompt.split("Your task now:\n", 1)[1]
        self.assertLess(task.index("rework-1.md still holds"), task.index("rework-2.md"))
        quoted = calls[2].prompt.split("The human's rework note", 1)[1].split("\n\n", 1)[0]
        self.assertIn("rework-2.md", quoted)
        self.assertIn("Second note: rename the flag.", quoted)


class ProductDecisionTest(FactoryTestCase):
    """A (product) assumption is both a line a person confirms (section 7) and a decision item only a person
    answers (section 32): either answer is the other."""

    def items(self, factory: Any, job: dict) -> list[dict]:
        return factory.scope_items(f"job:{REPO}/{job['id']}")["items"]

    def decision(self, factory: Any, job: dict) -> dict:
        return next(item for item in self.items(factory, job) if item["kind"] == "decision")

    def test_a_confirmation_proves_the_decision_item(self) -> None:
        holder: dict[str, Any] = {}

        def build_while_a_person_confirms(ctx: RunContext, call: Call) -> None:
            build(ctx, call)
            holder["factory"].confirm(REPO, ctx.job["id"], [FREE])

        factory = holder["factory"] = self.start(spec_writer=product_spec, builder=build_while_a_person_confirms)
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        item = self.decision(factory, job)
        self.assertEqual((item["status"], item["verified_by"]), ("verified", "human"))
        self.assertIn(f"a person confirmed: {FREE}", [entry.get("note") for entry in
                                                      factory.item_history(item["ref"])["log"]])

    def test_a_line_confirmed_before_the_spec_is_taken_in(self) -> None:
        """spec.md exists before the spec step ends; a confirmation then finds no item yet, so the ingest
        that makes the item applies it."""
        holder: dict[str, Any] = {}

        def spec_then_confirm(ctx: RunContext, call: Call) -> None:
            product_spec(ctx, call)
            holder["factory"].confirm(REPO, ctx.job["id"])

        factory = holder["factory"] = self.start(spec_writer=spec_then_confirm)
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertEqual(job["confirmed"]["lines"], [FREE])
        self.assertEqual((self.decision(factory, job)["status"], self.decision(factory, job)["verified_by"]),
                         ("verified", "human"))

    def test_proving_the_item_confirms_its_line_and_reopening_withdraws_it(self) -> None:
        factory = self.start(spec_writer=product_spec, approver=escalate)
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "needs-human")
        ref = self.decision(factory, job)["ref"]
        self.assertEqual(factory.assumptions(REPO, job["id"])["unconfirmed"], [FREE])
        factory.item_action(ref, "verify", note="yes, Hello on the free plan")
        job = factory.job(REPO, job["id"])
        self.assertEqual(job["confirmed"]["lines"], [FREE])
        self.assertEqual((Path(job["job_dir"]) / "confirmed.md").read_text(), CONFIRMED_FREE)
        self.assertEqual(factory.assumptions(REPO, job["id"])["unconfirmed"], [])
        factory.item_action(ref, "reopen", reason="ask marketing first")
        job = factory.job(REPO, job["id"])
        self.assertEqual(job["confirmed"]["lines"], [])
        self.assertFalse((Path(job["job_dir"]) / "confirmed.md").exists())
        self.assertEqual(factory.assumptions(REPO, job["id"])["unconfirmed"], [FREE])
        messages = [e.data["message"] for e in self.events(job, "log")]
        self.assertIn(f"a person confirmed 1 product assumption(s) by proving {ref.rsplit('/', 1)[1]}", messages)
        self.assertIn("a person withdrew the confirmation of 1 product assumption(s) by reopening I-1", messages)
        factory.confirm(REPO, job["id"])
        self.assertEqual(self.decision(factory, job)["status"], "verified", "confirming again proves it again")

    def test_a_mention_that_is_not_a_mark_is_no_decision(self) -> None:
        def spec(ctx: RunContext, call: Call) -> None:
            (ctx.job_dir / "spec.md").write_text("# Spec\n\n## Assumptions\n- Plain text only, not (product).\n"
                                                 "- The free plan says Hello. (product)\n")

        factory = self.start(spec_writer=spec, approver=escalate)
        job = self.run_job(factory)
        decisions = [item["title"] for item in self.items(factory, job) if item["kind"] == "decision"]
        self.assertEqual(decisions, ["The free plan says Hello. (product)"], "the kit gate's rule, in both")
        self.assertEqual(factory.assumptions(REPO, job["id"])["marked"], ["The free plan says Hello. (product)"])

    def test_a_multi_line_assumption_is_confirmed_by_its_marked_line(self) -> None:
        def spec(ctx: RunContext, call: Call) -> None:
            (ctx.job_dir / "spec.md").write_text("# Spec\n\n## Assumptions\n- Free users get three projects,\n"
                                                 "  paid users unlimited **(product)**.\n")

        factory = self.start(spec_writer=spec, approver=escalate)
        job = self.run_job(factory)
        self.assertEqual(factory.assumptions(REPO, job["id"])["marked"], ["paid users unlimited (product)."])
        factory.confirm(REPO, job["id"])
        self.assertEqual(self.decision(factory, job)["status"], "verified")
        self.assertIn("Free users get three projects", self.decision(factory, job)["title"])


class ChatTurnsAcrossModelsTest(ChatTestCase):
    """A chat turn on another model archives the old session (section 23); the sweep's suggestion from that
    turn's reply still names its turn among all the chat's sessions (section 32)."""

    def setUp(self) -> None:
        super().setUp()
        self.local = f"t{uuid.uuid4().hex[:6]}"   # provider slots are process-wide by name
        self.config.raw["providers"][self.local] = {**LOCAL, "base_url": "http://127.0.0.1:59107/v1"}
        self.config.raw["models"]["flash"] = {"provider": self.local, "model": "f1", "overflow": "opus"}
        self.config.raw.setdefault("agents", {})["coder"] = {"model": "flash"}
        self.reach = FakeReach()
        self.factory.reach = self.reach
        self.chats = ChatManager(self.config, self.bus, self.factory, runner_factory=self.runners)
        self.addCleanup(self.chats.stop)

    def turn(self, chat_id: str, text: str, reply: str) -> dict:
        def answer(call: Any) -> AgentRun:
            result = session_writer(call)
            result.final_text = reply
            return result

        self.runners.handler = answer
        self.chats.chat_send(chat_id, text)
        until(lambda: self.chats.chat(chat_id)["status"] != "running")
        return self.chats.chat(chat_id)

    def test_a_suggestion_from_a_hand_over_turn_names_its_turn(self) -> None:
        chat = self.chats.new_chat("app")
        self.turn(chat["id"], "turn one", "Done. The docs are left for later.")
        self.reach.up_map[self.local] = False   # the overflow takes turn two, in a fresh session
        after = self.turn(chat["id"], "turn two", "Done. Also the tests are not run yet.")
        self.assertEqual(after["earlier_sessions"][0]["file"], "sessions/01.json")
        items = self.chats.scope_items(chat["id"])["items"]
        self.assertEqual([(item["source"]["turn"], item["source"]["run"]) for item in items],
                         [(1, "01-turn"), (2, "02-turn")], "turn 2 counts the archived session's turn")
        messages = self.chats.chat(chat["id"])["messages"]
        self.assertEqual([(m["role"], m["turn"]) for m in messages],
                         [("user", 1), ("assistant", 1), ("user", 2), ("assistant", 2)])
        self.assertEqual(messages[2]["text"], "turn two", "the hand-over turn shows the person's own words")
        session = json.loads((self.chats.root / chat["id"] / "session.json").read_text())
        self.assertTrue(session["messages"][0]["content"].startswith("Hand-over:"))


class GateRuleTextTest(FactoryTestCase):
    """The Grid Card and the factory-line map state the tracker's gate rule only while it holds jobs."""

    def test_the_card_and_the_map_follow_housekeeping_enabled(self) -> None:
        on = factory_grid({"reviewers": ["code"], "max_resumes": 1}, "approver")
        off = factory_grid({"reviewers": ["code"], "max_resumes": 1}, "approver", items=False)
        self.assertIn("every must-do item in items.md proven, a product decision by a person", on)
        self.assertIn("answering every must-do item it owns → [4]", on)
        self.assertNotIn("must-do", off)
        factory = self.start(self.config(housekeeping={"enabled": False}))
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertNotIn("must-do", self.recorder.of("builder")[0].ctx.grid)
        gates = [next(node["label"] for node in docs.maps(config)[0]["nodes"] if node["id"] == "gate")
                 for config in (self.config(), self.config(housekeeping={"enabled": False}))]
        self.assertEqual(gates, ["Gate (code)\nreviews · items · conflicts · paths · size",
                                 "Gate (code)\nreviews · conflicts · paths · size"])
