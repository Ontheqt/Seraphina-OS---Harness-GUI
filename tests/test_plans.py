"""Tests for swfactory.plans: plan.md parsing and PlanManager with a scripted lead, a fake Factory
driven through job.updated events on a real EventBus, and real git for the lead's checkouts."""

from __future__ import annotations

import json
import threading
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any

from swfactory.chats import INTERRUPTED
from swfactory.events import EventBus
from swfactory.orchestrator import Factory
from swfactory.plans import PLAN_ID_RE, PlanManager, parse_findings, parse_plan, plan_status
from swfactory.runners import AgentRun
from tests.helpers import commit_file, run_git
from tests.test_chats import WAIT_SECONDS, Call, ManagerTestCase, ScriptedRunners, wait_for

PLAN_4 = """PLAN: READY

## Jobs
1. Add the users table and model
2. Add the settings page shell
3. Add the profile form to the settings page (after: 1, 2)
4. Email users when their profile changes (after: 3)

## Context
Profiles live in the users table.
"""

PLAN_2 = "PLAN: READY\n\n## Jobs\n1. Add the API endpoint\n2. Add the form (after: 1)\n"

PLAN_3 = "PLAN: READY\n\n## Jobs\n1. Add A\n2. Add B\n3. Add C\n\n## Context\nThree independent jobs.\n"

REVIEW_CHANGES = """VERDICT: CHANGES

## Findings
1. Document the profile API in README.md: nobody documented it
2. **Remove the duplicated email helper in mail.py**: two jobs each added one

## Notes
Otherwise good.
"""


class ParsePlanTest(unittest.TestCase):
    def test_ready_plan_with_dependencies(self) -> None:
        parsed = parse_plan(PLAN_4)
        self.assertEqual(parsed.status, "READY")
        self.assertEqual(parsed.jobs, [
            {"n": 1, "feature": "Add the users table and model", "after": []},
            {"n": 2, "feature": "Add the settings page shell", "after": []},
            {"n": 3, "feature": "Add the profile form to the settings page", "after": [1, 2]},
            {"n": 4, "feature": "Email users when their profile changes", "after": [3]},
        ])

    def test_first_line_variants(self) -> None:
        for first in ("PLAN: READY", "**PLAN: READY**", "# PLAN: READY", "`PLAN: READY`", "﻿PLAN: READY",
                      "\n\n  PLAN: READY  "):
            self.assertEqual(parse_plan(first + "\n\n## Jobs\n1. Do it\n").status, "READY", first)
        for bad in ("", "PLAN: ready", "Plan: READY", "PLAN READY", "Here is the plan\nPLAN: READY"):
            with self.assertRaisesRegex(ValueError, "must start with", msg=bad):
                parse_plan(bad + "\n## Jobs\n1. x\n")
        self.assertIsNone(plan_status("   \n"))

    def test_questions(self) -> None:
        parsed = parse_plan("PLAN: QUESTIONS\n\n## Questions\n- Which DB?\n2. Which auth?\n\n## Notes\n- not one\n")
        self.assertEqual((parsed.status, parsed.jobs), ("QUESTIONS", []))
        self.assertEqual(parsed.questions, ["Which DB?", "Which auth?"])

    def test_after_suffix_forms(self) -> None:
        parsed = parse_plan("PLAN: READY\n## Jobs\n1. A\n2. B (after: 1).\n3. C (After 1, #2)\n4. D (after: )\n"
                            "5. E (after: 4, 4)\n6) F (after:5)\n")
        self.assertEqual([(job["feature"], job["after"]) for job in parsed.jobs],
                         [("A", []), ("B", [1]), ("C", [1, 2]), ("D", []), ("E", [4]), ("F", [5])])

    def test_prose_sub_items_and_subsections(self) -> None:
        text = ("PLAN: READY\n\nIntro.\n\n## Jobs\nThese are the jobs.\n### Backend\n1. A\n   - a detail\n"
                "   1. a nested numbered detail\n### Frontend\n2. B (after: 1)\n\n## Context\n1. not a job\n")
        self.assertEqual([job["feature"] for job in parse_plan(text).jobs], ["A", "B"])

    def test_structural_errors(self) -> None:
        cases = {
            "PLAN: READY\n\n## Context\nx\n": "no '## Jobs' section",
            "PLAN: READY\n## Jobs\nnothing here\n": "lists no numbered jobs",
            "PLAN: READY\n## Jobs\n1. A\n3. B\n": "numbered 1, 2, 3",
            "PLAN: READY\n## Jobs\n1. A\n1. B\n": "numbered 1, 2, 3",
            "PLAN: READY\n## Jobs\n1. A (after: 3)\n2. B\n": "unknown job 3",
            "PLAN: READY\n## Jobs\n1. A (after: 0)\n": "unknown job 0",
            "PLAN: READY\n## Jobs\n1. A (after: one)\n": "invalid reference 'one'",
            "PLAN: READY\n## Jobs\n1. A (after: 1)\n": "cycle: jobs 1 can never start",
            "PLAN: READY\n## Jobs\n1. A (after: 2)\n2. B (after: 1)\n3. C (after: 2)\n4. D\n":
                "cycle: jobs 1, 2, 3 can never start",
            "PLAN: READY\n## Jobs\n1. (after: 2)\n2. B\n": "job 1 has no feature text",
        }
        for text, message in cases.items():
            with self.assertRaisesRegex(ValueError, message, msg=text):
                parse_plan(text)

    def test_findings(self) -> None:
        self.assertEqual(parse_findings(REVIEW_CHANGES), [
            ("Document the profile API in README.md", "nobody documented it"),
            ("Remove the duplicated email helper in mail.py", "two jobs each added one"),
        ])
        self.assertEqual(parse_findings("VERDICT: PASS\n\n## Findings\nNone.\n"), [])
        self.assertEqual(parse_findings("VERDICT: CHANGES\n## Findings\n- Fix http://x/y in a.py\n- None\n"),
                         [("Fix http://x/y in a.py", "")])
        self.assertEqual(parse_findings("VERDICT: CHANGES\n"), [])


class Lead:
    """A scripted lead: answers each Plan / Final review run with the next text for that task
    (None writes nothing, an AgentRun is returned as is) and records what it saw."""

    def __init__(self) -> None:
        self.plans: list[Any] = []
        self.reviews: list[Any] = []
        self.seen: list[dict] = []
        self.gate: threading.Event | None = None
        self.started = threading.Event()
        self.runners = ScriptedRunners(self.handle)

    def handle(self, call: Call) -> AgentRun:
        cwd = Path(call.ctx.cwd)
        self.seen.append({"task": "plan" if "Task: Plan." in call.prompt else "review", "call": call,
                          "head": run_git(cwd, "rev-parse", "HEAD").strip(),
                          "detached": run_git(cwd, "rev-parse", "--abbrev-ref", "HEAD").strip() == "HEAD"})
        self.started.set()
        if self.gate is not None:
            while not self.gate.is_set():
                if call.ctx.cancel.wait(0.01):
                    return AgentRun(ok=False, error="cancelled")
        answer = (self.plans if self.seen[-1]["task"] == "plan" else self.reviews).pop(0)
        if isinstance(answer, AgentRun):
            return answer
        if answer is not None:
            call.ctx.write_paths[0].write_text(answer)
        return AgentRun(ok=True, turns=2,
                        usage={"input_tokens": 100, "output_tokens": 10, "cached_tokens": 0, "calls": 2})


class PlanTestCase(ManagerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.config = self.make_config()
        self.lead = Lead()
        self.manager = self.start_manager()

    def start_manager(self) -> PlanManager:
        self.factory.config = self.config   # the Factory and its plan manager share one config
        manager = PlanManager(self.config, self.bus, self.factory, runner_factory=self.lead.runners)
        self.addCleanup(manager.stop)
        manager.start()
        return manager

    def plan_now(self, plan: dict) -> dict:
        return self.manager.plan("app", plan["id"])

    def wait_plan(self, plan: dict, predicate: Callable[[dict], bool], message: str) -> dict:
        """Wait until predicate holds for the saved plan.json and return it."""
        def holds() -> dict | None:
            current = self.plan_now(plan)
            return current if predicate(current) else None

        return wait_for(holds, message=f"plan {plan['id']}: {message}")

    def wait_stage(self, plan: dict, stage: str) -> dict:
        return self.wait_plan(plan, lambda p: p["stage"] == stage, f"stage {stage}")

    def wait_entry(self, plan: dict, n: int, stage: str) -> dict:
        return self.wait_plan(plan, lambda p: p["jobs"][n - 1]["stage"] == stage, f"job {n} {stage}")

    def wait_submitted(self, plan: dict, count: int) -> dict:
        """Wait until plan.json records factory jobs for count of the plan's jobs."""
        return self.wait_plan(plan, lambda p: sum(1 for j in p["jobs"] if j["job_id"]) == count,
                              f"{count} submitted jobs")

    def ready_plan(self, text: str = PLAN_4, routing: Any = None) -> dict:
        self.lead.plans.append(text)
        return self.wait_stage(self.manager.create("app", "Build user profiles", routing=routing), "ready")

    def job_id(self, plan: dict, n: int) -> str:
        return self.plan_now(plan)["jobs"][n - 1]["job_id"]

    def submitted_features(self) -> list[str]:
        return [job["feature"].split("\n\n")[0] for job in self.factory.submitted]

    def worktrees(self) -> str:
        return run_git(self.repo, "worktree", "list", "--porcelain")


class PlanStepTest(PlanTestCase):
    def test_create_runs_the_lead_and_parses_its_plan(self) -> None:
        self.lead.plans.append(PLAN_4)
        request = "Build user profiles: a table,\na settings page and emails.\n"
        created = self.manager.create("app", request)
        self.assertEqual(created["stage"], "planning")
        self.assertRegex(created["id"], PLAN_ID_RE)
        self.assertTrue(created["id"].startswith("P001-build-user-profiles"))
        plan = self.wait_stage(created, "ready")
        plan_dir = self.tmp / "data" / "plans" / "app" / plan["id"]
        self.assertEqual((plan["plan_dir"], plan["base"]), (str(plan_dir), "main"))
        self.assertEqual(plan["base_commit"], run_git(self.repo, "rev-parse", "main").strip())
        self.assertEqual((plan_dir / "request.md").read_text(), request)
        self.assertEqual([(job["n"], job["after"], job["job_id"], job["stage"]) for job in plan["jobs"]],
                         [(1, [], None, None), (2, [], None, None), (3, [1, 2], None, None), (4, [3], None, None)])
        self.assertEqual(plan["usage"], {"input_tokens": 100, "output_tokens": 10, "cached_tokens": 0, "calls": 2})
        self.assertEqual(json.loads((plan_dir / "plan.json").read_text()), plan)

        seen = self.lead.seen[0]
        ctx = seen["call"].ctx
        checkout = self.tmp / "data" / "worktrees" / "app" / f"{plan['id']}-lead"
        self.assertEqual((ctx.cwd, ctx.job_dir, ctx.write_paths), (checkout, plan_dir, [plan_dir / "plan.md"]))
        self.assertEqual(ctx.read_roots[:2], [checkout, plan_dir])
        self.assertEqual((ctx.agent.name, ctx.log_path), ("lead", plan_dir / "logs" / "01-lead.jsonl"))
        self.assertTrue(seen["detached"])
        self.assertEqual(seen["head"], plan["base_commit"])
        prompt = seen["call"].prompt
        self.assertIn(f"Plan folder: {plan_dir}.", prompt)
        self.assertIn(f"Checkout: {checkout}", prompt)
        self.assertIn(f"Task: Plan. Read {plan_dir / 'request.md'} and write {plan_dir / 'plan.md'}.", prompt)
        wait_for(lambda: not checkout.exists(), message="the lead checkout to be removed")
        self.assertNotIn(str(checkout), self.worktrees())

        agent_events = [e for e in self.bus.since(0) if e.type.startswith("agent.")]
        self.assertEqual([e.type for e in agent_events], ["agent.start", "agent.end"])
        self.assertEqual({(e.job, e.agent, e.data["plan"], e.data["run"]) for e in agent_events},
                         {(None, "lead", plan["id"], "01-lead")})
        stages = [e.data["plan"]["stage"] for e in self.published("plan.updated")]
        self.assertEqual((stages[0], stages[-1]), ("planning", "ready"))

        self.lead.plans.append(PLAN_2)
        second = self.manager.create("app", "Another one")
        self.assertTrue(second["id"].startswith("P002-"))
        self.wait_stage(second, "ready")
        self.assertEqual([p["id"] for p in self.manager.plans()], [plan["id"], second["id"]])

    def test_questions_go_to_needs_human_and_retry_replans(self) -> None:
        self.lead.plans += ["PLAN: QUESTIONS\n\n## Questions\n- Which database?\n", PLAN_2]
        plan = self.wait_stage(self.manager.create("app", "Do something vague"), "needs-human")
        self.assertIn("questions", plan["reason"])
        self.assertEqual(plan["failed_step"], "plan")
        self.assertEqual(self.manager.retry("app", plan["id"])["stage"], "planning")
        plan = self.wait_stage(plan, "ready")
        self.assertEqual((len(plan["jobs"]), plan["reason"], plan["failed_step"]), (2, None, None))
        archived = Path(plan["plan_dir"], "questions-1.md")
        self.assertEqual(archived.read_text(), "PLAN: QUESTIONS\n\n## Questions\n- Which database?\n")
        self.assertIn(f"You asked questions before; they are in {archived},", self.lead.seen[1]["call"].prompt)
        self.assertNotIn("You asked questions before", self.lead.seen[0]["call"].prompt)

    def test_answers_to_the_questions_reach_the_lead_on_retry(self) -> None:
        questions = "PLAN: QUESTIONS\n\n## Questions\n- Postgres or SQLite?\n"
        self.lead.plans += [questions, AgentRun(ok=False, error="HTTP 503"), questions, PLAN_2]
        plan = self.wait_stage(self.manager.create("app", "Add storage"), "needs-human")
        answered = questions + "\n## Answers\n- SQLite, in data/app.db.\n"
        saved = self.manager.update_plan_text("app", plan["id"], answered)
        self.assertEqual((saved["stage"], saved["failed_step"], saved["jobs"]), ("needs-human", "plan", []))
        self.assertIn("answered", saved["reason"])
        plan_dir = Path(plan["plan_dir"])
        self.assertEqual((plan_dir / "plan.md").read_text(), answered)
        self.manager.retry("app", plan["id"])
        failed = self.wait_stage(plan, "failed")                  # the run fails; the answers are kept
        self.assertEqual((plan_dir / "questions-1.md").read_text(), answered)
        self.assertFalse((plan_dir / "plan.md").exists())
        self.manager.retry("app", failed["id"])
        self.wait_stage(plan, "needs-human")                      # the lead asks once more, unanswered
        self.manager.retry("app", plan["id"])
        self.wait_stage(plan, "ready")
        prompts = [seen["call"].prompt for seen in self.lead.seen]
        self.assertEqual(len(prompts), 4)
        self.assertIn(f"they are in {plan_dir / 'questions-1.md'}, with the human's answers", prompts[1])
        self.assertIn(f"they are in {plan_dir / 'questions-1.md'}, with the human's answers", prompts[2])
        self.assertIn(f"they are in {plan_dir / 'questions-1.md'}, {plan_dir / 'questions-2.md'}, with",
                      prompts[3])
        self.assertEqual((plan_dir / "questions-2.md").read_text(), questions)
        self.assertEqual((plan_dir / "plan.md").read_text(), PLAN_2)

    def test_questions_text_is_only_taken_as_answers(self) -> None:
        self.lead.plans.append("PLAN: READY\n## Jobs\n1. A (after: 2)\n2. B (after: 1)\n")
        invalid = self.wait_stage(self.manager.create("app", "x"), "needs-human")
        self.manager.update_plan_text("app", invalid["id"], "PLAN: QUESTIONS\n\nUse a queue, not a cron job.\n")
        plan = self.ready_plan(PLAN_2)
        with self.assertRaisesRegex(ValueError, "PLAN: READY"):
            self.manager.update_plan_text("app", plan["id"], "PLAN: QUESTIONS\n## Questions\n- why?\n")
        self.assertEqual(Path(plan["plan_dir"], "plan.md").read_text(), PLAN_2)

    def test_missing_plan_is_asked_for_once_more(self) -> None:
        self.lead.plans += [None, PLAN_2]
        self.wait_stage(self.manager.create("app", "Two jobs"), "ready")
        prompts = [seen["call"].prompt for seen in self.lead.seen]
        self.assertEqual(len(prompts), 2)
        self.assertIn("You did not write", prompts[1])
        self.assertNotIn("You did not write", prompts[0])

    def test_invalid_plan_twice_needs_human(self) -> None:
        self.lead.plans += ["Sure! Here is a plan.", "Still not a plan."]
        plan = self.wait_stage(self.manager.create("app", "x"), "needs-human")
        self.assertIn("must start with PLAN: READY", plan["reason"])
        self.assertEqual(len(self.lead.seen), 2)

    def test_cyclic_plan_needs_human_without_a_second_run(self) -> None:
        self.lead.plans.append("PLAN: READY\n## Jobs\n1. A (after: 2)\n2. B (after: 1)\n")
        plan = self.wait_stage(self.manager.create("app", "x"), "needs-human")
        self.assertIn("dependency cycle", plan["reason"])
        self.assertEqual(len(self.lead.seen), 1)

    def test_lead_failure_fails_the_plan_and_retry_reruns_it(self) -> None:
        self.lead.plans += [AgentRun(ok=False, error="model call failed: HTTP 503"), PLAN_2]
        plan = self.wait_stage(self.manager.create("app", "x"), "failed")
        self.assertEqual((plan["failed_step"], plan["reason"]), ("plan", "model call failed: HTTP 503"))
        with self.assertRaises(self.ActionError):
            self.manager.run("app", plan["id"])
        self.manager.retry("app", plan["id"])
        self.wait_stage(plan, "ready")
        with self.assertRaises(self.ActionError):
            self.manager.retry("app", plan["id"])

    def test_create_validation(self) -> None:
        with self.assertRaises(ValueError):
            self.manager.create("app", "  ")
        with self.assertRaises(KeyError):
            self.manager.create("nope", "x")
        with self.assertRaises(ValueError):
            self.manager.create("app", "x", routing=[1])
        with self.assertRaises(ValueError):
            self.manager.create("app", "x", routing={"lead": {"driver": "missing"}})
        self.assertEqual(self.manager.plans(), [])

    def test_job_routing_is_checked_before_the_lead_runs(self) -> None:
        cases: list[tuple[Any, str]] = [
            ("hybird", "unknown preset 'hybird'"),
            ("", "unknown preset ''"),
            ({"builder": {"driver": "no-such-driver"}}, "cannot route builder: unknown driver"),
            ({"reviewer-code": {"extra_body": [1]}}, "routing for reviewer-code: extra_body must be an object"),
            ({"builder": {"model": 3}}, "routing for builder: model must be a string"),
            ({"builder": {"modle": "local"}}, "routing for builder: unknown keys modle"),
            ({"builder": "local"}, "routing must map agent names"),
            ({"lead": {"model": "sonnet", "efort": "high"}}, "routing for lead: unknown keys efort"),
            ({"lead": {"model": "sonnet", "effort": "extreme"}}, "routing for lead.effort must be one of"),
            ({"builder": {"escalate": "opus"}}, "routing for builder.escalate must be a list of model keys"),
            ({"builder": {"reason": "vibes: it feels right"}}, "routing for builder.reason.kind must be one of"),
        ]
        real = Factory(self.config, EventBus())   # never started: submit only validates and writes
        for routing, message in cases:
            with self.assertRaisesRegex(ValueError, message, msg=repr(routing)):
                self.manager.create("app", "x", routing=routing)
            with self.assertRaisesRegex(ValueError, message, msg=repr(routing)):
                real.submit("app", "x", routing=routing)   # the factory says the same (check_routing)
        self.assertEqual((self.manager.plans(), self.lead.seen, real.jobs()), ([], [], []))
        self.assertEqual([routing for _, routing in self.factory.routing_checks], [routing for routing, _ in cases])
        # a reviewer the pipeline runs must have an agent definition, also for a preset
        config = self.make_config(pipeline={"reviewers": ["code", "perf"]})
        self.factory.config = config
        manager = PlanManager(config, self.bus, self.factory, runner_factory=self.lead.runners)
        self.addCleanup(manager.stop)
        for routing in (None, "hybrid"):
            with self.assertRaisesRegex(ValueError, "no agent (definition for )?reviewer-perf", msg=routing):
                manager.create("app", "x", routing=routing)
        self.assertEqual(self.lead.seen, [])

    def test_preset_routing_routes_the_lead_and_the_jobs(self) -> None:
        plan = self.ready_plan(PLAN_2, routing="hybrid")
        ctx = self.lead.seen[0]["call"].ctx
        self.assertEqual((ctx.model.key, ctx.model.model, ctx.model.provider.name),
                         ("claude-fable", "claude-fable-5-1", "anthropic"))
        start = next(e for e in self.bus.since(0) if e.type == "agent.start")
        self.assertEqual((start.data["model"], start.data["model_id"]), ("claude-fable", "claude-fable-5-1"))
        self.assertEqual(plan["routing"], "hybrid")
        self.manager.run("app", plan["id"])
        self.assertEqual(self.factory.submitted[0]["routing"], "hybrid")
        self.lead.reviews.append("VERDICT: PASS\n")
        self.factory.merge(self.job_id(plan, 1))
        self.wait_entry(plan, 2, "queued")
        self.factory.merge(self.job_id(plan, 2))
        self.wait_stage(plan, "done")
        self.assertEqual(self.lead.seen[-1]["call"].ctx.model.model, "claude-fable-5-1")

    def test_lead_routing_override_and_job_routing(self) -> None:
        routing = {"lead": {"model": "sonnet"}, "builder": {"model": "local"}}
        plan = self.ready_plan(PLAN_2, routing=routing)
        self.assertEqual(self.lead.seen[0]["call"].ctx.model.model, "deepseek-v4-flash")
        self.manager.run("app", plan["id"])
        self.assertEqual(self.factory.submitted[0]["routing"], routing)

    def test_cancel_while_planning_stops_the_lead(self) -> None:
        self.lead.gate = threading.Event()
        plan = self.manager.create("app", "x")
        self.assertTrue(self.lead.started.wait(WAIT_SECONDS))
        self.assertEqual(self.manager.cancel("app", plan["id"])["stage"], "cancelled")
        checkout = self.tmp / "data" / "worktrees" / "app" / f"{plan['id']}-lead"
        wait_for(lambda: not checkout.exists(), message="the lead checkout to be removed")
        end = wait_for(lambda: [e for e in self.bus.since(0) if e.type == "agent.end"], message="the lead run")
        wait_for(lambda: [e for e in self.bus.since(end[0].seq) if e.type == "plan.updated"],
                 message="the lead step to record its usage")
        self.assertEqual(self.plan_now(plan)["stage"], "cancelled")
        self.assertEqual(end[0].data["error"], "cancelled")
        with self.assertRaises(self.ActionError):
            self.manager.cancel("app", plan["id"])


class RunTest(PlanTestCase):
    def test_jobs_run_in_dependency_order_then_final_review_passes(self) -> None:
        plan = self.ready_plan()
        self.assertEqual(self.manager.run("app", plan["id"])["stage"], "running")
        self.assertEqual(self.submitted_features(), ["Add the users table and model", "Add the settings page shell"])
        plan_md = f"{plan['plan_dir']}/plan.md"
        self.assertTrue(self.factory.submitted[0]["feature"].endswith(
            f"\n\nPlan context: {plan['id']} — see {plan_md}"))
        self.assertEqual(self.factory.submitted[0]["routing"], None)

        self.factory.merge(self.job_id(plan, 1))
        self.wait_entry(plan, 1, "merged")
        self.assertEqual(len(self.factory.submitted), 2)          # job 3 still waits for job 2
        self.factory.set_stage(self.job_id(plan, 2), "review")
        self.wait_entry(plan, 2, "review")
        self.factory.merge(self.job_id(plan, 2))
        self.wait_entry(plan, 3, "queued")
        self.assertEqual(self.submitted_features()[2], "Add the profile form to the settings page")
        self.factory.merge(self.job_id(plan, 3))
        self.wait_entry(plan, 4, "queued")
        self.lead.reviews.append("VERDICT: PASS\n\n## Findings\nNone.\n")
        self.factory.merge(self.job_id(plan, 4))
        done = self.wait_stage(plan, "done")
        self.assertEqual(done["reviews"], [{"k": 1, "verdict": "PASS"}])
        self.assertEqual(len(self.factory.submitted), 4)
        self.assertTrue(all(job["merged_commit"] for job in done["jobs"]))
        self.assertEqual(done["usage"]["calls"], 4)

    def test_final_review_prompt_lists_the_plans_merges(self) -> None:
        plan = self.ready_plan(PLAN_2)
        self.manager.run("app", plan["id"])
        first = self.factory.merge(self.job_id(plan, 1))
        self.wait_entry(plan, 2, "queued")
        run_git(self.repo, "checkout", "-q", "-b", "outsider", "main")
        commit_file(self.repo, "other.txt", "x\n")
        run_git(self.repo, "checkout", "-q", "main")
        run_git(self.repo, "merge", "-q", "--no-ff", "-m", "Merge outsider work", "outsider")
        self.lead.reviews.append("VERDICT: PASS\n")
        second = self.factory.merge(self.job_id(plan, 2))
        self.wait_stage(plan, "done")
        review = self.lead.seen[-1]
        ctx, prompt = review["call"].ctx, review["call"].prompt
        plan_dir = Path(plan["plan_dir"])
        self.assertEqual(ctx.write_paths, [plan_dir / "review-1.md"])
        self.assertEqual(review["head"], second)
        self.assertTrue(review["detached"])
        self.assertIn(f"Task: Final review 1. Every job of the plan has merged into main. "
                      f"Write {plan_dir / 'review-1.md'}.", prompt)
        self.assertIn(f"`git diff {plan['base_commit']}..main`", prompt)
        ours, others = prompt.split("Other merges in")
        self.assertIn(f"  {first[:7]}", ours)
        self.assertIn(f"  {second[:7]}", ours)
        self.assertIn("Merge outsider work", others)
        self.assertNotIn("outsider", ours)

    def test_blocked_job_pauses_the_plan_until_it_merges(self) -> None:
        plan = self.ready_plan()
        self.manager.run("app", plan["id"])
        self.factory.set_stage(self.job_id(plan, 1), "needs-human", reason="builder blocked")
        blocked = self.wait_stage(plan, "blocked")
        self.assertEqual(blocked["reason"], f"job {self.job_id(plan, 1)} is needs-human")
        self.factory.merge(self.job_id(plan, 2))
        self.wait_entry(plan, 2, "merged")
        self.assertEqual(self.plan_now(plan)["stage"], "blocked")
        self.assertEqual(len(self.factory.submitted), 2)
        self.factory.set_stage(self.job_id(plan, 1), "failed")
        self.wait_entry(plan, 1, "failed")
        self.assertIn("is failed", self.plan_now(plan)["reason"])
        self.factory.merge(self.job_id(plan, 1))
        self.wait_entry(plan, 3, "queued")
        resumed = self.plan_now(plan)
        self.assertEqual((resumed["stage"], resumed["reason"]), ("running", None))

    def test_plan_delivery_goes_to_every_job_and_a_delivered_job_holds_the_plan(self) -> None:
        self.lead.plans.append(PLAN_2)
        plan = self.wait_stage(self.manager.create("app", "Two steps", delivery="branch"), "ready")
        self.assertEqual(plan["delivery"], "branch")
        self.manager.run("app", plan["id"])
        self.wait_submitted(plan, 1)
        self.assertEqual(self.factory.submitted[0]["delivery"], "branch")
        first = self.job_id(plan, 1)
        self.factory.set_stage(first, "delivered")
        held = self.wait_stage(plan, "blocked")
        self.assertEqual(held["reason"], f"job {first} is delivered (not merged): approve it to merge it, "
                                         f"and the plan goes on")
        self.assertEqual(len(self.factory.submitted), 1, "job 2 builds on job 1, which is not on main yet")
        self.factory.merge(first)   # a human approved the delivered job
        self.wait_submitted(plan, 2)
        self.assertEqual(self.factory.submitted[1]["delivery"], "branch")
        self.assertEqual(self.plan_now(plan)["stage"], "running")

    def test_plan_delivery_defaults_to_the_config_and_is_validated(self) -> None:
        self.config.raw["merge"]["delivery"] = "folder"
        self.lead.plans.append(PLAN_2)
        plan = self.wait_stage(self.manager.create("app", "Default delivery"), "ready")
        self.assertEqual(plan["delivery"], "folder", "resolved when the plan is created")
        with self.assertRaisesRegex(ValueError, "delivery must be one of merge, branch, folder"):
            self.manager.create("app", "x", delivery="ftp")
        self.assertEqual(len(self.manager.plans()), 1)

    def test_rejected_job_keeps_the_plan_blocked_until_the_plan_is_edited(self) -> None:
        plan = self.ready_plan()
        self.manager.run("app", plan["id"])
        old_job = self.job_id(plan, 1)
        self.factory.merge(self.job_id(plan, 2))
        self.factory.set_stage(old_job, "rejected")
        self.wait_stage(plan, "blocked")
        with self.assertRaises(self.ActionError):
            self.manager.run("app", plan["id"])
        edited = PLAN_4.replace("Add the users table and model", "Add the users table, model and seed data")
        plan = self.manager.update_plan_text("app", plan["id"], edited)
        self.assertEqual((plan["stage"], plan["reason"]), ("ready", None))
        self.assertEqual([job["job_id"] for job in plan["jobs"]][:2], [None, self.factory.submitted[1]["id"]])
        self.assertEqual(Path(plan["plan_dir"], "plan.md").read_text(), edited)
        self.factory.set_stage(old_job, "rejected", note="late event for a job the plan dropped")
        self.manager.run("app", plan["id"])
        self.assertEqual(self.submitted_features()[2], "Add the users table, model and seed data")
        self.assertEqual(self.plan_now(plan)["stage"], "running")

    def test_edit_may_not_drop_or_reword_a_job_still_in_progress(self) -> None:
        plan = self.ready_plan(PLAN_3)
        self.manager.run("app", plan["id"])
        first, second, third = (self.job_id(plan, n) for n in (1, 2, 3))
        self.factory.set_stage(second, "build")
        self.factory.set_stage(first, "rejected")
        self.wait_stage(plan, "blocked")
        self.wait_entry(plan, 2, "build")
        for edited in (PLAN_3.replace("Add B", "Add B."),               # reworded
                       "PLAN: READY\n\n## Jobs\n1. Add A properly\n2. Add C\n"):  # removed
            with self.assertRaisesRegex(self.ActionError, rf"2\. 'Add B' \({second} is build\)"):
                self.manager.update_plan_text("app", plan["id"], edited)
        self.assertEqual(Path(plan["plan_dir"], "plan.md").read_text(), PLAN_3)
        self.assertEqual(self.plan_now(plan)["stage"], "blocked")

        # the line of the job in progress stays (moved to the end); the rejected one is reworded
        edited = "PLAN: READY\n\n## Jobs\n1. Add A properly\n2. Add C\n3. Add B\n"
        ready = self.manager.update_plan_text("app", plan["id"], edited)
        self.assertEqual([(job["n"], job["job_id"], job["stage"]) for job in ready["jobs"]],
                         [(1, None, None), (2, third, "queued"), (3, second, "build")])
        self.factory.cancel("app", second)                              # now its line may go
        self.wait_entry(plan, 3, "cancelled")
        ready = self.manager.update_plan_text("app", plan["id"], PLAN_3.replace("Add B", "Add B, tested"))
        self.manager.run("app", plan["id"])
        self.assertEqual(self.submitted_features()[3:], ["Add A", "Add B, tested"])
        self.assertEqual(self.plan_now(plan)["stage"], "running")
        self.assertEqual({job["job_id"] for job in self.plan_now(plan)["jobs"]},
                         {third, *(job["id"] for job in self.factory.submitted[3:])})

    def test_edit_gives_rejected_and_cancelled_jobs_a_new_job_even_with_the_same_line(self) -> None:
        plan = self.ready_plan(PLAN_3)
        self.manager.run("app", plan["id"])
        first, second, third = (self.job_id(plan, n) for n in (1, 2, 3))
        self.factory.set_stage(first, "rejected")
        self.factory.cancel("app", second)
        self.factory.set_stage(third, "failed")
        self.wait_entry(plan, 3, "failed")
        edited = PLAN_3 + "Add A must use the X library.\n"               # prose only
        ready = self.manager.update_plan_text("app", plan["id"], edited)
        self.assertEqual([(job["job_id"], job["stage"]) for job in ready["jobs"]],
                         [(None, None), (None, None), (third, "failed")])  # failed can still be retried
        self.factory.set_stage(first, "rejected", note="a late event for a job the plan dropped")
        self.manager.update_plan_text("app", plan["id"], PLAN_3)
        self.factory.merge(third)
        self.wait_entry(plan, 3, "merged")
        running = self.manager.run("app", plan["id"])
        self.assertEqual((running["stage"], running["reason"]), ("running", None))
        self.assertEqual(self.submitted_features()[3:], ["Add A", "Add B"])

    def test_run_catches_up_with_job_changes_it_missed(self) -> None:
        plan = self.ready_plan(PLAN_3)
        self.manager.run("app", plan["id"])
        first, second = self.job_id(plan, 1), self.job_id(plan, 2)
        self.factory.set_stage(first, "rejected")
        self.wait_stage(plan, "blocked")
        self.manager.update_plan_text("app", plan["id"], PLAN_3.replace("Add A", "Add A properly"))
        self.factory.jobs[("app", second)].update(stage="failed")       # no job.updated event
        blocked = self.manager.run("app", plan["id"])
        self.assertEqual((blocked["stage"], blocked["reason"]), ("blocked", f"job {second} is failed"))

    def test_cancel_cancels_the_active_jobs(self) -> None:
        plan = self.ready_plan()
        self.manager.run("app", plan["id"])
        self.factory.merge(self.job_id(plan, 1))
        self.factory.set_stage(self.job_id(plan, 2), "build")
        self.wait_entry(plan, 2, "build")
        cancelled = self.manager.cancel("app", plan["id"])
        self.assertEqual(cancelled["stage"], "cancelled")
        self.assertEqual(self.factory.cancelled, [self.job_id(plan, 2)])
        self.wait_entry(plan, 2, "cancelled")
        self.assertEqual(len(self.factory.submitted), 2)
        self.assertEqual(self.plan_now(plan)["stage"], "cancelled")
        with self.assertRaises(self.ActionError):
            self.manager.retry("app", plan["id"])

    def test_edit_and_run_validation(self) -> None:
        plan = self.ready_plan(PLAN_2)
        with self.assertRaisesRegex(ValueError, "must start with"):
            self.manager.update_plan_text("app", plan["id"], "nonsense")
        with self.assertRaisesRegex(ValueError, "PLAN: READY"):
            self.manager.update_plan_text("app", plan["id"], "PLAN: QUESTIONS\n## Questions\n- why?\n")
        self.assertEqual(Path(plan["plan_dir"], "plan.md").read_text(), PLAN_2)
        plan = self.manager.update_plan_text("app", plan["id"], PLAN_4)
        self.assertEqual(len(plan["jobs"]), 4)
        self.manager.run("app", plan["id"])
        with self.assertRaises(self.ActionError):
            self.manager.update_plan_text("app", plan["id"], PLAN_2)
        with self.assertRaises(KeyError):
            self.manager.run("app", "P999-missing")
        with self.assertRaises(KeyError):
            self.manager.plan("app", "../../etc")

    def test_submit_failure_fails_the_plan_and_retry_resubmits(self) -> None:
        plan = self.ready_plan(PLAN_2)
        self.factory.fail_submit = RuntimeError("queue full")
        failed = self.manager.run("app", plan["id"])
        self.assertEqual((failed["stage"], failed["failed_step"]), ("failed", "run"))
        self.assertIn("queue full", failed["reason"])
        self.factory.fail_submit = None
        self.assertEqual(self.manager.retry("app", plan["id"])["stage"], "running")
        self.assertEqual(self.submitted_features(), ["Add the API endpoint"])

    def test_extra_read_roots_for_job(self) -> None:
        plan = self.ready_plan(PLAN_2)
        self.manager.run("app", plan["id"])
        job = self.factory.job("app", self.job_id(plan, 1))
        self.assertEqual(self.manager.extra_read_roots_for_job(job), [Path(plan["plan_dir"])])
        self.assertEqual(self.manager.extra_read_roots_for_job({"repo": "app", "feature": "Plain job"}), [])
        bogus = {"repo": "app", "feature": "x\n\nPlan context: P042-nope — see /etc/plan.md"}
        self.assertEqual(self.manager.extra_read_roots_for_job(bogus), [])
        self.assertEqual(self.manager.extra_read_roots_for_job({**job, "repo": "../x"}), [])


class FinalReviewTest(PlanTestCase):
    def run_to_review(self, reviews: list[Any]) -> dict:
        plan = self.ready_plan(PLAN_2)
        self.lead.reviews += reviews
        self.manager.run("app", plan["id"])
        self.factory.merge(self.job_id(plan, 1))
        self.wait_entry(plan, 2, "queued")
        self.factory.merge(self.job_id(plan, 2))
        return plan

    def test_changes_become_follow_up_jobs_then_pass(self) -> None:
        routing = {"builder": {"model": "sonnet"}}
        plan = self.ready_plan(PLAN_2, routing=routing)
        self.lead.reviews += [REVIEW_CHANGES, "VERDICT: PASS\n"]
        self.manager.run("app", plan["id"])
        self.factory.merge(self.job_id(plan, 1))
        self.wait_entry(plan, 2, "queued")
        self.factory.merge(self.job_id(plan, 2))
        running = self.wait_submitted(plan, 4)
        self.assertEqual((running["stage"], running["reviews"]), ("running", [{"k": 1, "verdict": "CHANGES"}]))
        followups = running["jobs"][2:]
        self.assertEqual([(job["n"], job["feature"], job["review"], job["after"]) for job in followups],
                         [(3, "Document the profile API in README.md", 1, []),
                          (4, "Remove the duplicated email helper in mail.py", 1, [])])
        submitted = self.factory.submitted[2]
        self.assertEqual(submitted["routing"], routing)
        self.assertIn("From final review 1 of the plan: nobody documented it", submitted["feature"])
        self.assertIn(f"Plan context: {plan['id']} — see ", submitted["feature"])
        self.factory.merge(self.job_id(plan, 3))
        self.wait_entry(plan, 3, "merged")
        self.assertEqual(self.plan_now(plan)["stage"], "running")
        self.factory.merge(self.job_id(plan, 4))
        done = self.wait_stage(plan, "done")
        self.assertEqual(done["reviews"], [{"k": 1, "verdict": "CHANGES"}, {"k": 2, "verdict": "PASS"}])
        self.assertEqual(self.lead.seen[-1]["call"].ctx.write_paths, [Path(plan["plan_dir"]) / "review-2.md"])
        self.assertIn("Task: Final review 2.", self.lead.seen[-1]["call"].prompt)

    def test_second_changes_needs_human(self) -> None:
        plan = self.run_to_review([REVIEW_CHANGES, REVIEW_CHANGES])
        self.wait_submitted(plan, 4)
        self.factory.merge(self.job_id(plan, 3))
        self.factory.merge(self.job_id(plan, 4))
        stopped = self.wait_stage(plan, "needs-human")
        self.assertEqual(stopped["reason"], "final review 2 still asks for changes")
        self.assertEqual(len(self.factory.submitted), 4)
        self.lead.reviews.append("VERDICT: PASS\n")
        self.assertEqual(self.manager.retry("app", plan["id"])["stage"], "final-review")
        self.assertEqual(self.wait_stage(plan, "done")["reviews"][-1], {"k": 3, "verdict": "PASS"})

    def test_edit_drops_a_rejected_follow_up_job(self) -> None:
        plan = self.run_to_review([REVIEW_CHANGES, "VERDICT: PASS\n"])
        self.wait_submitted(plan, 4)
        self.factory.set_stage(self.job_id(plan, 3), "rejected")
        self.wait_stage(plan, "blocked")
        kept = self.job_id(plan, 4)
        ready = self.manager.update_plan_text("app", plan["id"], PLAN_2 + "\n## Context\nKeep mail.py small.\n")
        self.assertEqual([(job["n"], job["feature"], job["job_id"]) for job in ready["jobs"][2:]],
                         [(3, "Remove the duplicated email helper in mail.py", kept)])
        self.manager.run("app", plan["id"])
        self.factory.merge(kept)
        done = self.wait_stage(plan, "done")
        self.assertEqual(done["reviews"], [{"k": 1, "verdict": "CHANGES"}, {"k": 2, "verdict": "PASS"}])
        self.assertEqual(len(self.factory.submitted), 4)

    def test_changes_without_findings_needs_human(self) -> None:
        plan = self.run_to_review(["VERDICT: CHANGES\n\n## Findings\nNone.\n"])
        self.assertIn("lists no findings", self.wait_stage(plan, "needs-human")["reason"])

    def test_invalid_review_is_asked_for_again_then_needs_human(self) -> None:
        plan = self.run_to_review(["Looks fine to me.", None])
        stopped = self.wait_stage(plan, "needs-human")
        self.assertEqual(stopped["reason"], "the lead wrote no valid review-1.md")
        self.assertEqual((stopped["reviews"], stopped["failed_step"]), ([], "final-review"))
        self.assertIn("is missing or its first line is not a verdict", self.lead.seen[-1]["call"].prompt)

    def test_human_edit_after_review_reruns_the_review(self) -> None:
        plan = self.run_to_review([REVIEW_CHANGES, REVIEW_CHANGES])
        self.wait_submitted(plan, 4)
        self.factory.merge(self.job_id(plan, 3))
        self.factory.merge(self.job_id(plan, 4))
        self.wait_stage(plan, "needs-human")
        edited = PLAN_2 + "3. Add a changelog entry\n"
        plan = self.manager.update_plan_text("app", plan["id"], edited)
        self.assertEqual([(job["n"], job["stage"]) for job in plan["jobs"]],
                         [(1, "merged"), (2, "merged"), (3, None), (4, "merged"), (5, "merged")])
        self.manager.run("app", plan["id"])
        self.assertEqual(self.submitted_features()[-1], "Add a changelog entry")
        self.lead.reviews.append("VERDICT: PASS\n")
        self.factory.merge(self.job_id(plan, 3))
        self.assertEqual(self.wait_stage(plan, "done")["reviews"][-1], {"k": 3, "verdict": "PASS"})


class PlanItemsTest(PlanTestCase):
    """DESIGN §32: a plan's tracked items: its jobs' leftovers, the lead's answers, findings that follow their jobs."""

    def items(self, plan: dict) -> dict:
        return {item["id"]: item for item in self.manager.scope_items("app", plan["id"])["items"]}

    def test_plan_job_leftovers_land_in_the_plan_and_the_lead_answers_them(self) -> None:
        from swfactory import items as items_lib

        plan = self.ready_plan(PLAN_2)
        self.lead.reviews.append("VERDICT: PASS\n\n## Findings\nNone.\n\n## Items\n"
                                 "- I-1 verified: the combined diff documents the endpoint\n")
        self.manager.run("app", plan["id"])
        first = self.factory.job("app", self.job_id(plan, 1))
        self.assertEqual(self.manager.plan_of_job(first), plan["id"])
        self.assertIsNone(self.manager.plan_of_job({"repo": "app", "feature": "Unrelated"}))
        origin = items_lib.job_scope("app", first["id"])
        leftover = {"id": "I-4", "title": "Document the endpoint", "status": "open", "kind": "commitment",
                    "owner": "builder", "evidence": [], "source": {"quote": "left the docs for later"}}
        refs = self.manager.carry_in("app", plan["id"], [items_lib.carry_copy(leftover, origin, due=None,
                                                                              reason="job merged")])
        self.assertEqual(refs, [f"plan:app/{plan['id']}/I-1"])
        carried = self.items(plan)["I-1"]
        self.assertEqual((carried["kind"], carried["owner"], carried["links"]["job"], carried["source"]["ref"]),
                         ("followup", "human", first["id"], f"job:app/{first['id']}/I-4"))
        self.factory.merge(first["id"])
        self.wait_entry(plan, 2, "queued")
        self.factory.merge(self.job_id(plan, 2))
        self.wait_stage(plan, "done")
        prompt = self.lead.seen[-1]["call"].prompt
        self.assertIn(f"Items: {plan['plan_dir']}/items.md (1 open; waiting for your check: I-1).", prompt)
        self.assertIn("Waiting for your check (1): I-1 [open] Document the endpoint", prompt,
                      "the lead judges what the plan's ended jobs carried in")
        self.assertTrue((Path(plan["plan_dir"]) / "items.md").is_file())
        answered = self.items(plan)["I-1"]
        self.assertEqual((answered["status"], answered["verified_by"]), ("verified", "lead"))
        view = self.manager.scope_items("app", plan["id"])
        self.assertEqual([job["job_id"] for job in view["jobs"]], [self.job_id(plan, 1), self.job_id(plan, 2)])
        history = self.manager.item_history("app", plan["id"], "I-1")
        self.assertEqual([(e["by"], e["to"]) for e in history["log"]], [("factory", "open"), ("lead", "verified")])
        with self.assertRaises(KeyError):
            self.manager.item_history("app", plan["id"], "I-9")

    def test_a_finding_item_follows_its_follow_up_job(self) -> None:
        plan = self.ready_plan(PLAN_2)
        self.lead.reviews += [REVIEW_CHANGES, "VERDICT: PASS\n"]
        self.manager.run("app", plan["id"])
        self.factory.merge(self.job_id(plan, 1))
        self.wait_entry(plan, 2, "queued")
        self.factory.merge(self.job_id(plan, 2))
        running = self.wait_submitted(plan, 4)
        self.assertEqual([job.get("item") for job in running["jobs"][2:]], ["I-1", "I-2"])
        wait_for(lambda: self.items(plan)["I-2"]["status"] == "doing", message="the finding items to be worked on")
        first = self.items(plan)["I-1"]
        self.assertEqual((first["kind"], first["owner"], first["verifier"], first["links"]["job"]),
                         ("finding", "builder", "lead", self.job_id(plan, 3)))
        self.factory.merge(self.job_id(plan, 3))
        wait_for(lambda: self.items(plan)["I-1"]["status"] == "verified", message="the merged finding to be proven")
        self.assertEqual(self.items(plan)["I-1"]["evidence"][-1]["kind"], "job")
        self.factory.set_stage(self.job_id(plan, 4), "rejected")
        wait_for(lambda: self.items(plan)["I-2"]["status"] == "open", message="the rejected finding to be open again")


class RestartTest(PlanTestCase):
    def test_interrupted_lead_step_fails_and_running_plans_catch_up(self) -> None:
        planning = self.ready_plan(PLAN_2)
        running = self.ready_plan(PLAN_2)
        self.manager.run("app", running["id"])
        self.manager.stop()
        state = Path(planning["plan_dir"], "plan.json")
        state.write_text(json.dumps({**json.loads(state.read_text()), "stage": "planning"}))
        self.factory.jobs[("app", self.job_id(running, 1))].update(stage="merged", merged_commit="abc")
        self.manager = self.start_manager()
        failed = self.plan_now(planning)
        self.assertEqual((failed["stage"], failed["failed_step"], failed["reason"]), ("failed", "plan", INTERRUPTED))
        self.assertEqual(self.plan_now(running)["jobs"][0]["stage"], "merged")
        self.assertEqual(self.submitted_features(), ["Add the API endpoint", "Add the form"])
        self.factory.set_stage(self.job_id(running, 2), "needs-human")
        self.wait_stage(running, "blocked")

    def test_plans_that_are_not_running_catch_up_with_interrupted_jobs(self) -> None:
        ready = self.ready_plan(PLAN_3)
        self.manager.run("app", ready["id"])
        building = self.job_id(ready, 2)
        self.factory.set_stage(building, "build")
        self.factory.set_stage(self.job_id(ready, 1), "rejected")
        self.wait_stage(ready, "blocked")
        self.manager.update_plan_text("app", ready["id"], PLAN_3.replace("Add A", "Add A properly"))
        failed = self.ready_plan(PLAN_2)
        self.manager.run("app", failed["id"])
        queued = self.job_id(failed, 1)
        self.manager.stop()
        state = Path(failed["plan_dir"], "plan.json")
        state.write_text(json.dumps({**json.loads(state.read_text()), "stage": "failed", "failed_step": "run",
                                     "reason": "could not submit a job: queue full"}))
        # the factory marks the jobs it was running failed before the plans subscribe (no event seen)
        for job_id in (building, queued):
            self.factory.jobs[("app", job_id)].update(stage="failed", reason=INTERRUPTED)
        self.manager = self.start_manager()
        self.assertEqual(self.plan_now(ready)["jobs"][1]["stage"], "failed")
        self.assertEqual(self.plan_now(ready)["stage"], "ready")
        self.assertEqual(self.plan_now(failed)["jobs"][0]["stage"], "failed")
        self.assertEqual(self.plan_now(failed)["stage"], "failed")
        self.assertEqual(self.manager.run("app", ready["id"])["reason"], f"job {building} is failed")
        self.assertEqual(self.manager.retry("app", failed["id"])["reason"], f"job {queued} is failed")
        self.factory.merge(building)
        self.wait_stage(ready, "running")


if __name__ == "__main__":
    unittest.main()
