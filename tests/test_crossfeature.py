"""Tests for where the v1 features meet: the routing engine (DESIGN.md section 23), the Housekeeping Tracker
(section 32), Grid delivery with the (product) confirmations (sections 7 and 26), Open WebUI personas (section
33) and Wake & Messages (sections 34 and 35).

Each feature has its own suite; these pin the interactions: an escalated builder's fresh session still gets
its items and the Grid Card, a person's confirmation and the tracker's decision item stay one answer, a chat
that moves to another model numbers its turns across sessions, the card and the map state the gate's items
rule only while the tracker holds jobs, an outside agent's token and an event source's token never open each
other's routes, overdue items of the system scope reach the inbox as references only, and an offline
snapshot is decision e's imported copy: enabled by its import, labelled on every reply, stopped by its switch.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from swfactory import docs, personas, settings
from swfactory.chats import ChatManager
from swfactory.config import Config
from swfactory.events import EventBus
from swfactory.gridcard import HERE, factory_grid
from swfactory.gridcard import receipt as grid_receipt
from swfactory.runners import AgentRun, RunContext
from tests.test_chats import ChatTestCase
from tests.test_orchestrator import (CONFIRMED_FREE, FREE, REPO, TIMEOUT, Call, FactoryTestCase, build, escalate,
                                     product_spec, reviews, until, verifying_reviewer)
from tests.mock_llm import MockLLM
from tests.mock_openwebui import MockOpenWebUI
from tests.test_personas import KATE_META, KATE_PARAMS, KATE_PROMPT, PersonaTestCase, _Factory, finished_turn
from tests.test_routing import LOCAL, FakeReach, PlacementTestCase, session_writer
from tests.test_server import RealFactoryCase

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


class TokensStayInTheirLanesTest(RealFactoryCase):
    """An outside agent's token (section 32) and an event source's token (section 34) each open their own routes
    and nothing else: neither is the other, and neither reaches the messages, the wakes or a persona."""

    def setUp(self) -> None:
        super().setUp()
        self.server.hook_fail_delay = 0.01

    def test_neither_token_opens_the_other_s_routes(self) -> None:
        created = self.request("POST", "/api/agent-tokens", {"name": "qwen-code"})
        self.assertEqual(created.status, 201, created.body)
        agent = created.json()["token"]
        added = self.request("POST", "/api/notify/hooks", {"name": "backup-script", "events": ["backup.failed"],
                                                          "subjects": ["nightly"]})
        self.assertEqual(added.status, 201, added.body)
        hook = added.json()["token"]
        event = {"kind": "backup.failed", "subject": "nightly"}
        self.assert_error(self.request("POST", "/api/hooks/event", event, token=agent), 401)
        self.assert_error(self.request("POST", "/api/hooks/wake", {"persona": "seraphina"}, token=agent), 401)
        for method, path, body in (("GET", "/api/items", None), ("GET", "/api/system/items", None),
                                   ("POST", "/api/items/propose", {"scope": "system", "title": "x"}),
                                   ("GET", "/api/messages", None), ("GET", "/api/wakes", None)):
            with self.subTest(token="hook", path=path):
                self.assert_error(self.request(method, path, body, token=hook), 401, "authentication required")
        for method, path in (("GET", "/api/messages"), ("GET", "/api/notify"), ("GET", "/api/notify/hooks"),
                             ("GET", "/api/wakes"), ("GET", "/api/notify/rules"), ("POST", "/api/wakes/control"),
                             ("POST", "/api/personas/seraphina/messages"), ("GET", "/api/personas")):
            with self.subTest(token="agent", path=path):
                self.assert_error(self.request(method, path, {} if method == "POST" else None, token=agent), 403,
                                  "valid only for the item routes")
        self.assertEqual(self.request("POST", "/api/hooks/event", event, token=hook).status, 202)
        self.assertEqual(self.request("GET", "/api/items", token=agent).status, 200)
        dumped = json.dumps([e.to_dict() for e in self.bus.since(0)])
        for token in (agent, hook):
            self.assertNotIn(token, dumped, "no event carries a token")


class OverdueItemsReachTheInboxTest(RealFactoryCase):
    """The Notifier's tracker rule (section 34) reads the real tracker, the system scope included (section 32):
    overdue items become one inbox notice that names their references, never their titles, so a persona's private
    promise (section 35) stays on the machine."""

    def test_overdue_system_and_persona_items_are_listed_by_reference_only(self) -> None:
        past = (date.today() - timedelta(days=3)).isoformat()
        added = self.request("POST", "/api/system/items", {"title": "Renew the TLS certificate", "due": past})
        self.assertEqual(added.status, 201, added.body)
        self.factory.open_persona_item("seraphina", "Ask about the clinic appointment on Friday", due=past,
                                       taint=["private"])
        notifier = self.factory._notify_manager()
        notifier.reconcile()            # the first pass only records
        self.assertEqual(self.request("GET", "/api/messages?kind=items.overdue").json()["messages"], [])
        notifier.rules_tick([SimpleNamespace(type="items.updated", data={"ref": "system"})])
        notices = self.request("GET", "/api/messages?kind=items.overdue").json()["messages"]
        self.assertEqual(len(notices), 1, notices)
        body = notices[0]["body"]
        self.assertIn("2 tracked promises are overdue: system:S-1, system:S-2.", body)
        self.assertEqual(notices[0]["deliveries"], {}, "in the inbox and the next summary only")
        for text in ("TLS", "clinic"):
            self.assertNotIn(text, json.dumps(notices))
        dumped = json.dumps([e.to_dict() for e in self.bus.since(0) if e.type.startswith("notify.")])
        self.assertNotIn("system:S-1", dumped, "notify events carry ids, never a notice's text")
        notifier.rules_tick([SimpleNamespace(type="items.updated", data={"ref": "system"})])
        self.assertEqual(len(self.request("GET", "/api/messages?kind=items.overdue").json()["messages"]), 1,
                         "once a day")


class OfflineCopyChatTest(PersonaTestCase):
    """An offline snapshot (section 33) is the imported copy of operator decision e (section 35): its import turns
    agents.<name>.offline on, every reply it writes is labelled, and turning the switch off stops its chat at
    the next turn, before anything is sent."""

    def test_the_import_enables_the_copy_and_its_switch_stops_it(self) -> None:
        with MockLLM([{"text": "Offline Kate here."}, {"text": "never sent"}]) as llm:
            local = {"local": {"type": "openai", "base_url": llm.base_url, "api_key_env": None}}
            models = {"flash": {"provider": "local", "model": "qwen3.8-flash", "max_tokens": 1024}}
            config = self.make_config(providers=local, models=models)
            config.save()
            config = Config.load(config.path)
            personas.import_profile(config, records=self.owui.export_file("kate"), snapshot={"name": "kate-offline"})
            config = Config.load(config.path)
            self.assertIs(config.raw["agents"]["kate-offline"]["offline"], True)
            rows = {name: personas.persona_view(config, persona)
                    for name, persona in personas.discover_personas(config).items()}
            self.assertEqual((rows["kate-offline"]["kind"], rows["kate-offline"]["offline"]), ("snapshot", True))
            self.assertIsNone(rows["kate"]["offline"], "only a snapshot has the switch")
            manager = ChatManager(config, EventBus(), _Factory())
            self.addCleanup(manager.stop)
            chat = manager.new_chat(None, persona="kate-offline")
            done = finished_turn(manager, chat, "hi")
            self.assertEqual(done["status"], "idle", done["error"])
            asked, reply = done["messages"]
            self.assertEqual((reply["role"], reply["text"]), ("assistant", "Offline Kate here."))
            self.assertEqual(reply["label"], "from the imported copy: " + chat["persona"]["label"])
            self.assertNotIn("label", asked, "only the copy's own words are labelled")
            manager.config = settings.update_config(config, {"agents": {"kate-offline": {"offline": False}}})
            refused = finished_turn(manager, chat, "again")
            self.assertEqual(refused["status"], "error")
            self.assertIn("offline mode is not enabled", refused["error"])
            self.assertEqual(len(llm.requests), 1, "nothing was sent once the copy was turned off")
            with self.assertRaisesRegex(ValueError, "offline mode is not enabled"):
                manager.new_chat(None, persona="kate-offline")
            self.assertIs(personas.persona_view(manager.config, personas.discover_personas(manager.config)[
                "kate-offline"])["offline"], False)

    def test_the_switch_is_a_named_copy_s_boolean_and_a_failed_import_takes_it_back(self) -> None:
        local = {"tabby": {"type": "openai", "base_url": "http://127.0.0.1:5000/v1", "api_key_env": None}}
        models = {"flash": {"provider": "tabby", "model": "qwen3.8-flash", "max_tokens": 1024}}
        config = self.make_config(providers=local, models=models)
        config.save()
        config = Config.load(config.path)
        with self.assertRaisesRegex(ValueError, r"agents\.kate-offline\.offline must be true or false"):
            settings.update_config(config, {"agents": {"kate-offline": {"offline": "yes"}}})
        with self.assertRaisesRegex(ValueError, r"agents\.\*\.offline: .* per imported copy"):
            settings.update_config(config, {"agents": {"*": {"offline": True}}})
        with mock.patch.object(settings, "save_agent", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                personas.import_profile(config, records=self.owui.export_file("kate"),
                                        snapshot={"name": "kate-offline", "model": "flash"})
        saved = json.loads(config.path.read_text())
        self.assertNotIn("kate-offline", saved.get("models", {}))
        self.assertNotIn("kate-offline", saved.get("agents", {}),
                         "no switch is left for a later file of that name to run on")
        self.user_agent("kate-offline", "source: openwebui\nsource_model: kate\noffline: true\nmodel: flash\n")
        written = Config.load(config.path)
        with self.assertRaisesRegex(ValueError, "offline mode is not enabled"):
            personas.check_offline(written, personas.discover_personas(written)["kate-offline"])


class SoulImportThroughTheProfileRouteTest(RealFactoryCase):
    """POST /api/personas/import (section 33) with a name and no snapshot makes a persona that wakes herself from
    an Open WebUI profile (section 35): she is listed as a wake persona, private, never a chat; her page is the
    wake page; a second import of that name and an agent's name are refused."""

    def setUp(self) -> None:
        super().setUp()
        self.owui = MockOpenWebUI()
        self.owui.start()
        self.addCleanup(self.owui.stop)
        self.owui.add_workspace_model("kate", system=KATE_PROMPT, params=KATE_PARAMS, meta=KATE_META)

    def test_a_soul_import_is_a_wake_persona_not_a_chat(self) -> None:
        body = {"name": "kate-wake", "export": self.owui.export_file("kate"), "model_id": "kate"}
        imported = self.request("POST", "/api/personas/import", body)
        self.assertEqual(imported.status, 201, imported.body)
        self.assertEqual((imported.json()["persona"], imported.json()["privacy"]), ("kate-wake", "private"))
        self.assertNotIn("KATE-MARKER", imported.body.decode(), "the prompt never comes back")
        rows = {row["name"]: row for row in self.request("GET", "/api/personas").json()["personas"]}
        row = rows["kate-wake"]
        self.assertEqual((row["kind"], row["model"], row["appended_text"], row["offline"]), ("wake", None, None, None))
        page = self.request("GET", "/api/personas/kate-wake").json()
        self.assertEqual((page["card"], page["drift"]), (None, None))
        self.assertIn("soul", page)
        self.assert_error(self.request("POST", "/api/chats", {"persona": "kate-wake"}), 400, "kate-wake")
        self.assert_error(self.request("POST", "/api/personas/import", body), 409, "re-import her soul from her page")
        self.assert_error(self.request("POST", "/api/personas/import", {**body, "name": "coder"}), 400,
                          "an agent called coder exists")
        events = [e.to_dict() for e in self.bus.since(0) if e.type == "persona.imported"]
        self.assertEqual([(e["data"]["persona"], e["data"]["wake"]) for e in events], [("kate-wake", True)])
        self.assertNotIn("KATE-MARKER", json.dumps(events))
