"""Tests for swfactory.orchestrator: the factory line, driven by a scripted runner against real git repos.

ScriptedRunner plays every agent: per agent (and round) it writes the files the
agent would (spec.md, commits + build.md, review files, decision.md) and records
each call. Chats and plans are replaced by fakes so these tests exercise only
the orchestrator; one test starts the real managers.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import types
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from unittest import mock

from swfactory import gitops
from swfactory.config import Config, deep_merge
from swfactory.events import EventBus
from swfactory.gridcard import HERE, factory_grid
from swfactory.gridcard import receipt as grid_receipt
from swfactory.orchestrator import (BUILD_MD_FIX, COMMIT_FIX, INTERRUPTED, ITEMS_FIX, MERGE_BUSY, REVIEW_RETRY,
                                    SPEC_FROM_BRANCH, SPEC_RETRY, STOPPED, ActionError, Factory, _changed, _revert,
                                    _scan, new_factory)
from swfactory.runners import AgentRun, RunContext, Runner
from tests.helpers import commit_file, make_repo, run_git

REPO = "app"
TIMEOUT = 20.0
USAGE = {"input_tokens": 100, "output_tokens": 10, "cached_tokens": 5, "calls": 1}

Action = Callable[[RunContext, "Call"], "AgentRun | None"]


@dataclass
class Call:
    kind: str            # "run" or "resume"
    agent: str
    prompt: str
    session: str | None
    driver: str
    ctx: RunContext
    index: int           # this agent's call number, from 0, across every job


class Recorder:
    """Plays each agent from a script of actions and records every call, in order."""

    def __init__(self) -> None:
        self.actions: dict[str, Action] = dict(DEFAULT_ACTIONS)
        self.calls: list[Call] = []
        self.lock = threading.Lock()

    def runner_factory(self, config: Config, driver: str) -> Runner:
        return ScriptedRunner(self, driver)

    def play(self, kind: str, driver: str, ctx: RunContext, prompt: str, session: str | None) -> AgentRun:
        name = ctx.agent.name
        with self.lock:
            call = Call(kind, name, prompt, session, driver, ctx, len(self.of(name)))
            self.calls.append(call)
        ctx.emit("agent.text", {"text": f"{name} working"})
        ctx.session_path.write_text("{}")
        action = self.actions.get(name) or (self.actions["reviewer"] if name.startswith("reviewer-") else None)
        result = action(ctx, call) if action else None
        return result or AgentRun(ok=True, final_text="done", usage=dict(USAGE),
                                  session_ref=str(ctx.session_path), turns=1)

    def of(self, agent: str, job: str | None = None) -> list[Call]:
        return [c for c in self.calls if c.agent == agent and (job is None or c.ctx.job.get("id") == job)]


class ScriptedRunner(Runner):
    supports_resume = True

    def __init__(self, recorder: Recorder, driver: str) -> None:
        self.recorder = recorder
        self.driver = driver

    def run(self, ctx: RunContext, prompt: str) -> AgentRun:
        return self.recorder.play("run", self.driver, ctx, prompt, None)

    def resume(self, ctx: RunContext, session_ref: str, message: str) -> AgentRun:
        return self.recorder.play("resume", self.driver, ctx, message, session_ref)


# -- agent actions ---------------------------------------------------------------


def write_spec(ctx: RunContext, call: Call) -> None:
    (ctx.job_dir / "spec.md").write_text("# Spec\n\nAdd a greeting.\n")


def prompt_ids(prompt: str, label: str) -> list[str]:
    """Item ids the prompt's Items line lists after label ("you own", "waiting for your check")."""
    match = re.search(rf"{label}:? ([^;)]*)", prompt)
    return [part.strip() for part in match.group(1).split(",")] if match else []


def build(ctx: RunContext, call: Call) -> None:
    """A builder that follows the contract: commits, then answers every item it owns under ## Items."""
    sha = commit_file(ctx.cwd, f"feature-{call.index}.txt", f"work {call.index}\n", f"work {call.index}")
    lines = "".join(f"- {item} done: commit {sha[:12]}\n" for item in prompt_ids(call.prompt, "you own"))
    write_build(ctx, "READY", f"\n## Items\n{lines}" if lines else "")


def write_build(ctx: RunContext, status: str, extra: str = "") -> None:
    (ctx.job_dir / "build.md").write_text(f"STATUS: {status}\n\n# Build\n{extra}")


def review_pass(ctx: RunContext, call: Call) -> None:
    ctx.write_paths[0].write_text("VERDICT: PASS\n")


def approve(ctx: RunContext, call: Call) -> None:
    """An approver that follows the contract: verifies every item waiting for its check."""
    lines = "".join(f"- {item} verified: re-ran its check\n"
                    for item in prompt_ids(call.prompt, "waiting for your check"))
    (ctx.job_dir / "decision.md").write_text("APPROVE\n\n## Why\nAll good.\n"
                                             + (f"\n## Items\n{lines}" if lines else ""))


def scout(ctx: RunContext, call: Call) -> None:
    ctx.write_paths[0].write_text("# AGENTS.md\n\n## Overview\nA test repo.\n")


DEFAULT_ACTIONS: dict[str, Action] = {"spec-writer": write_spec, "builder": build, "reviewer": review_pass,
                                      "approver": approve, "repo-scout": scout}


def review_round(ctx: RunContext) -> int:
    return int(ctx.write_paths[0].parent.name.split("-")[1])


def reviews(*verdicts: str | None) -> Action:
    """A reviewer that writes verdicts[round - 1] (the last one for later rounds; None writes nothing)."""
    def act(ctx: RunContext, call: Call) -> None:
        verdict = verdicts[min(review_round(ctx), len(verdicts)) - 1]
        if verdict:
            ctx.write_paths[0].write_text(f"VERDICT: {verdict}\n\n1. Fix the thing.\n")
    return act


def failing(error: str) -> Action:
    return lambda ctx, call: AgentRun(ok=False, error=error, usage=dict(USAGE))


def until(predicate: Callable[[], Any], timeout: float = TIMEOUT) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


class FakeManager:
    """Stands in for ChatManager / PlanManager: records every delegated call."""

    def __init__(self, config: Config, bus: EventBus, factory: Any, runner_factory: Any = None) -> None:
        self.config = config
        self.factory = factory
        self.calls: list[tuple[str, tuple]] = []
        self.started = self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self, timeout: float = 10.0) -> None:
        self.stopped = True

    def extra_read_roots_for_job(self, job: dict) -> list[Path]:
        return []

    def plan_of_job(self, job: dict) -> str | None:
        return None

    def __getattr__(self, name: str) -> Callable[..., dict]:
        def method(*args: Any) -> dict:
            self.calls.append((name, args))
            return {"called": name}
        return method


def fake_module(name: str, **attrs: Any) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    return module


def swap_module(test: unittest.TestCase, module: types.ModuleType) -> None:
    """Put module in sys.modules for this test only, touching no other entry."""
    previous = sys.modules.get(module.__name__)
    sys.modules[module.__name__] = module

    def restore() -> None:
        if previous is None:
            sys.modules.pop(module.__name__, None)
        else:
            sys.modules[module.__name__] = previous
    test.addCleanup(restore)


class FactoryTestCase(unittest.TestCase):
    reviewers = ("security", "code")

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sf-orch-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        home = self.tmp / "home"
        home.mkdir()
        patcher = mock.patch.dict(os.environ, {"HOME": str(home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        swap_module(self, fake_module("swfactory.chats", ChatManager=FakeManager))
        swap_module(self, fake_module("swfactory.plans", PlanManager=FakeManager))
        self.repo = make_repo(self.tmp)
        self.bus = EventBus()
        self.recorder = Recorder()

    def config(self, **sections: Any) -> Config:
        raw = {"data_dir": str(self.tmp / "data"), "repos": [{"name": REPO, "path": str(self.repo)}],
               "pipeline": {"reviewers": list(self.reviewers), "max_parallel_jobs": 1}}
        return Config(self.tmp / "config.json", deep_merge(raw, sections))

    def start(self, config: Config | None = None, **actions: Action) -> Factory:
        self.recorder.actions.update({name.replace("_", "-"): act for name, act in actions.items()})
        factory = Factory(config or self.config(), self.bus, runner_factory=self.recorder.runner_factory)
        factory.start()
        self.addCleanup(factory.stop)
        return factory

    def run_job(self, factory: Factory, feature: str = "Add a greeting", **kwargs: Any) -> dict:
        job = factory.submit(REPO, feature, **kwargs)
        return factory.wait(REPO, job["id"], timeout=TIMEOUT)

    def branch_exists(self, branch: str) -> bool:
        return bool(run_git(self.repo, "branch", "--list", branch).strip())

    def events(self, job: dict, kind: str | None = None) -> list:
        return [e for e in self.bus.since(0) if e.job == job["id"] and (kind is None or e.type == kind)]


# -- the line --------------------------------------------------------------------


class HappyPathTests(FactoryTestCase):
    def test_job_merges_and_cleans_up(self) -> None:
        factory = self.start()
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertEqual(job["decision"], "APPROVE")
        self.assertEqual(job["merged_commit"], run_git(self.repo, "rev-parse", "main").strip())
        parents = run_git(self.repo, "rev-list", "--parents", "-n", "1", "main").split()
        self.assertEqual(len(parents), 3, "a --no-ff merge commit")
        self.assertIn(f"Merge factory/{job['id']}: Add a greeting",
                      run_git(self.repo, "log", "-1", "--format=%s", "main"))
        self.assertTrue((self.repo / "feature-0.txt").is_file())
        self.assertFalse(Path(job["worktree"]).exists())
        self.assertFalse(self.branch_exists(job["branch"]))
        self.assertEqual(job["history"], [{"round": 1, "reviews": {"security": "PASS", "code": "PASS"}}])
        self.assertEqual(job["reviews"], {"security": "PASS", "code": "PASS"})
        self.assertEqual(job["round"], 1)
        self.assertEqual(job["usage"]["calls"], 5)
        self.assertEqual(job["usage"]["input_tokens"], 500)
        self.assertEqual(job["agents"]["builder"],
                         {"driver": "builtin", "model": "opus", "session": "sessions/builder.json", "runs": 1,
                          "grid": grid_receipt(factory_grid(job, "builder"))})
        self.assertEqual(job["base"], "main")
        self.assertEqual(job["base_commit"], run_git(self.repo, "rev-parse", "main^1").strip())

    def test_prompts_contexts_and_logs(self) -> None:
        factory = self.start()
        job = self.run_job(factory)
        job_dir, worktree = Path(job["job_dir"]), Path(job["worktree"])
        paths = f"Job folder: {job_dir}. Worktree: {worktree}. Branch: factory/{job['id']}. Base: main."
        spec, builder, approver = (self.recorder.of(a)[0] for a in ("spec-writer", "builder", "approver"))
        self.assertEqual(spec.prompt, f"{paths}\nFeature: Add a greeting")
        self.assertEqual(builder.prompt, f"{paths}\nBuild round 1.\nPort: 5110.")
        self.assertEqual(self.recorder.of("reviewer-code")[0].prompt, f"{paths}\nReview round 1.")
        self.assertEqual(approver.prompt, f"{paths}\nFinal round: 1.")
        self.assertEqual(spec.ctx.write_paths, [job_dir / "spec.md"])
        self.assertEqual(builder.ctx.write_paths, [worktree, job_dir / "build.md"])
        self.assertEqual(self.recorder.of("reviewer-security")[0].ctx.write_paths,
                         [job_dir / "round-1" / "review-security.md"])
        self.assertEqual(approver.ctx.write_paths, [job_dir / "decision.md"])
        self.assertEqual(builder.ctx.cwd, worktree)
        self.assertEqual(builder.ctx.read_roots[:2], [worktree, job_dir])
        self.assertEqual(builder.ctx.session_path, job_dir / "sessions" / "builder.json")
        self.assertEqual(builder.ctx.repo_path, self.repo)
        self.assertEqual(builder.ctx.model.model, "deepseek-v4-pro")
        self.assertEqual(builder.ctx.job["id"], job["id"])
        log = [json.loads(line) for line in (job_dir / "logs" / "02-builder.jsonl").read_text().splitlines()]
        self.assertEqual([e["type"] for e in log], ["agent.start", "agent.text", "agent.end"])
        self.assertTrue(all(e["data"]["run"] == "02-builder" and e["agent"] == "builder" for e in log))
        self.assertEqual(log[0]["data"]["round"], 1)
        self.assertEqual(log[2]["data"]["usage"], USAGE)
        files = factory.job_files(REPO, job["id"])
        for name in ("feature.md", "spec.md", "build.md", "decision.md", "job.json", "round-1/review-code.md",
                     "logs/01-spec-writer.jsonl", "logs/events.jsonl"):
            self.assertIn(name, files)
        self.assertFalse(any(name.startswith("sessions/") for name in files))

    def test_events_are_published_in_order(self) -> None:
        factory = self.start()
        job = self.run_job(factory)
        events = self.events(job)
        self.assertEqual(events[0].type, "job.created")
        seqs = [e.seq for e in events]
        self.assertEqual(seqs, sorted(seqs))
        stages = []
        for event in events:
            if event.type == "job.updated" and (not stages or stages[-1] != event.data["job"]["stage"]):
                stages.append(event.data["job"]["stage"])
        self.assertEqual(stages, ["spec", "build", "review", "approve", "merging", "merged"])
        runs = [(e.type, e.data["run"]) for e in events if e.type in ("agent.start", "agent.end")]
        starts = [run for kind, run in runs if kind == "agent.start"]
        self.assertEqual(starts[:2], ["01-spec-writer", "02-builder"])
        self.assertEqual(sorted(run[:2] for run in starts[2:4]), ["03", "04"])
        self.assertEqual(sorted(run[3:] for run in starts[2:4]), ["reviewer-code", "reviewer-security"])
        self.assertEqual(starts[4], "05-approver")
        for run in starts:
            self.assertLess(runs.index(("agent.start", run)), runs.index(("agent.end", run)))
        for event in events:
            if event.type.startswith("agent."):
                self.assertEqual((event.repo, event.job), (REPO, job["id"]))
                self.assertTrue(event.agent and event.data["run"].endswith(event.agent))
        verdicts = [e.data["job"]["reviews"] for e in events if e.type == "job.updated"
                    and e.data["job"]["stage"] == "review"]
        self.assertIn({"security": "pending", "code": "pending"}, verdicts)
        self.assertTrue(any(list(v.values()).count("PASS") == 1 for v in verdicts), "verdicts land one by one")

    def test_wait_times_out(self) -> None:
        release = threading.Event()
        factory = self.start(builder=lambda ctx, call: release.wait(TIMEOUT) and None)
        self.addCleanup(release.set)
        job = factory.submit(REPO, "Slow")
        with self.assertRaises(TimeoutError):
            factory.wait(REPO, job["id"], timeout=0.2)


class GridDeliveryTests(FactoryTestCase):
    """Every factory-line run carries the Grid Card of the job's own pipeline; agent.start and job.agents
    record its receipt (section 26)."""

    def test_every_line_agent_gets_its_marked_card_and_a_receipt(self) -> None:
        factory = self.start(self.config(pipeline={"reviewers": ["security", "ux", "code"], "max_resumes": 1}))
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        starts = {e.agent: e.data for e in self.events(job, "agent.start")}
        names = ["spec-writer", "builder", "reviewer-security", "reviewer-ux", "reviewer-code", "approver"]
        self.assertEqual(set(starts), set(names))
        for name in names:
            with self.subTest(agent=name):
                ctx = self.recorder.of(name)[0].ctx
                self.assertEqual(ctx.grid, factory_grid({"reviewers": ["security", "ux", "code"],
                                                         "max_resumes": 1}, name))
                self.assertEqual(ctx.grid.count(HERE), 1)
                self.assertIn(f"You are {name}", ctx.grid)
                self.assertEqual(starts[name]["grid"], grid_receipt(ctx.grid))
                self.assertEqual(job["agents"][name]["grid"], starts[name]["grid"])
        builder = self.recorder.of("builder")[0].ctx.grid
        self.assertIn("reviewer-security, reviewer-ux, reviewer-code", builder)
        self.assertIn("at most 1 resume)", builder)
        log = [json.loads(line) for line in (Path(job["job_dir"]) / "logs" / "02-builder.jsonl").read_text()
               .splitlines()]
        self.assertEqual(log[0]["data"]["grid"], grid_receipt(builder), "the run log keeps the receipt too")

    def test_the_card_follows_the_job_not_a_later_config(self) -> None:
        """A job snapshots its reviewers and max_resumes; its card is built from those, even after the
        config changes under it (the builder's first run swaps in other reviewers and another limit)."""
        holder: dict[str, Factory] = {}

        def build_then_change_the_config(ctx: RunContext, call: Call) -> None:
            build(ctx, call)
            if call.index == 0:
                holder["factory"].reload_config(self.config(pipeline={"reviewers": ["ux"], "max_resumes": 5}))
        factory = holder["factory"] = self.start(builder=build_then_change_the_config,
                                                 reviewer_code=reviews("CHANGES", "PASS"))
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertEqual(factory.config.pipeline().reviewers, ["ux"], "the config did change")
        first, second = self.recorder.of("builder")
        self.assertEqual(first.ctx.grid, second.ctx.grid, "a resumed run gets the same bytes")
        snapshot = {"reviewers": ["security", "code"], "max_resumes": 2}
        self.assertEqual(second.ctx.grid, factory_grid(snapshot, "builder"))
        self.assertNotIn("reviewer-ux", second.ctx.grid)
        self.assertEqual(self.recorder.of("approver")[0].ctx.grid, factory_grid(snapshot, "approver"))
        self.assertEqual(job["agents"]["builder"]["grid"]["bytes"], len(first.ctx.grid.encode("utf-8")))

    def test_the_scout_is_not_on_the_line(self) -> None:
        factory = self.start()
        factory.scout(REPO)
        until(lambda: self.recorder.of("repo-scout") and not factory.scout_status(REPO)["running"])
        self.assertEqual(self.recorder.of("repo-scout")[0].ctx.grid, "")
        start = next(e for e in self.bus.since(0) if e.type == "agent.start" and e.agent == "repo-scout")
        self.assertNotIn("grid", start.data)


PRODUCT_SPEC = ("# Spec\n\n> Feature: Add a greeting\n\n## Assumptions\n"
                "- The greeting says \"Hello\" on the free plan **(product)**.\n- It reuses `Banner`.\n")
FREE = 'The greeting says "Hello" on the free plan (product).'
CONFIRMED_FREE = f"# Confirmed by a human\n{FREE}\n"


def product_spec(ctx: RunContext, call: Call) -> None:
    (ctx.job_dir / "spec.md").write_text(PRODUCT_SPEC)


def escalate(ctx: RunContext, call: Call) -> None:
    (ctx.job_dir / "decision.md").write_text("ESCALATE\n\n## Why\nA product decision.\n")


class ProductAssumptionTests(FactoryTestCase):
    """DESIGN section 7: a person confirms the spec's (product) lines with Factory.confirm, at any time;
    confirmed.md, which the approver reads, is the orchestrator's rendering of job.confirmed."""

    def warnings(self, job: dict) -> list[str]:
        return [e.data["message"] for e in self.events(job, "log") if e.data["level"] == "warn"]

    def test_a_person_confirms_while_the_builder_runs(self) -> None:
        holder: dict[str, Factory] = {}
        seen: dict[str, Any] = {}

        def build_while_a_person_confirms(ctx: RunContext, call: Call) -> None:
            build(ctx, call)
            factory = holder["factory"]
            seen["before"] = factory.assumptions(REPO, ctx.job["id"])
            seen["job"] = factory.confirm(REPO, ctx.job["id"])

        def approver_reads(ctx: RunContext, call: Call) -> None:
            seen["confirmed.md"] = (ctx.job_dir / "confirmed.md").read_text()
            approve(ctx, call)
        factory = holder["factory"] = self.start(spec_writer=product_spec, builder=build_while_a_person_confirms,
                                                 approver=approver_reads)
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertEqual(seen["before"], {"marked": [FREE], "confirmed": [], "unconfirmed": [FREE]})
        self.assertEqual(seen["job"]["confirmed"]["lines"], [FREE])
        self.assertEqual(seen["confirmed.md"], CONFIRMED_FREE, "the approver reads what the person confirmed")
        self.assertEqual(job["confirmed"]["lines"], [FREE])
        self.assertRegex(job["confirmed"]["at"], r"^\d{4}-\d\d-\d\dT")
        self.assertEqual(factory.assumptions(REPO, job["id"]),
                         {"marked": [FREE], "confirmed": [FREE], "unconfirmed": []})
        self.assertEqual(self.warnings(job), [], "the write policy leaves confirmed.md to the orchestrator")
        self.assertIn("a person confirmed 1 product assumption(s)",
                      [e.data["message"] for e in self.events(job, "log")])

    def test_a_confirmed_md_nobody_confirmed_never_reaches_the_approver(self) -> None:
        """An agent's write (or a hand edit) is undone before the approver reads the file."""
        holder: dict[str, Factory] = {}
        seen: list[str | None] = []

        def builder_confirms_for_itself(ctx: RunContext, call: Call) -> None:
            build(ctx, call)
            path = ctx.job_dir / "confirmed.md"
            if call.index == 0:
                path.write_text(CONFIRMED_FREE)
            else:   # round 2: a person confirmed meanwhile; the agent adds a line of its own
                holder["factory"].confirm(REPO, ctx.job["id"], [f"- {FREE}"])
                with path.open("a") as handle:
                    handle.write("The paid plan gets no greeting (product)\n")

        def approver_reads(ctx: RunContext, call: Call) -> None:
            path = ctx.job_dir / "confirmed.md"
            seen.append(path.read_text() if path.exists() else None)
            (escalate if call.index == 0 else approve)(ctx, call)
        factory = holder["factory"] = self.start(spec_writer=product_spec, builder=builder_confirms_for_itself,
                                                 approver=approver_reads)
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "needs-human")
        self.assertIsNone(seen[0], "no one confirmed anything: the file the builder wrote is gone")
        self.assertNotIn("confirmed", job)
        restored = [w for w in self.warnings(job) if w.startswith("confirmed.md did not match")]
        self.assertEqual(len(restored), 1)
        self.assertIn("POST /api/jobs/<repo>/<id>/confirm or factory confirm", restored[0])
        factory.rework(REPO, job["id"], "Try again")
        job = factory.wait(REPO, job["id"], timeout=TIMEOUT)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertEqual(seen[1], CONFIRMED_FREE, "the record, without the agent's line")
        self.assertEqual(len([w for w in self.warnings(job) if w.startswith("confirmed.md did not match")]), 2)

    def test_what_a_person_can_confirm(self) -> None:
        factory = self.start(spec_writer=product_spec, approver=escalate)
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "needs-human")
        with self.assertRaisesRegex(ValueError, "spec.md does not mark this line"):
            factory.confirm(REPO, job["id"], ["It reuses `Banner`."])
        with self.assertRaisesRegex(ValueError, "spec.md does not mark this line"):
            factory.confirm(REPO, job["id"], [42])   # type: ignore[list-item]
        self.assertNotIn("confirmed", factory.job(REPO, job["id"]))
        confirmed = factory.confirm(REPO, job["id"], ['  * [x] The greeting says "Hello" on the free plan (product).'])
        self.assertEqual(confirmed["confirmed"]["lines"], [FREE], "stored as the spec spells it, normalized")
        self.assertEqual((Path(job["job_dir"]) / "confirmed.md").read_text(), CONFIRMED_FREE)
        again = factory.confirm(REPO, job["id"])
        self.assertEqual(again["confirmed"], confirmed["confirmed"], "confirming twice changes nothing")
        with self.assertRaises(KeyError):
            factory.confirm(REPO, "999-nope")
        factory.reject(REPO, job["id"])
        with self.assertRaisesRegex(ActionError, "while it is rejected"):
            factory.confirm(REPO, job["id"])

    def test_a_spec_without_marks_or_no_spec(self) -> None:
        factory = self.start(approver=escalate)
        job = self.run_job(factory)
        with self.assertRaisesRegex(ValueError, "spec.md marks no assumption"):
            factory.confirm(REPO, job["id"])
        self.assertEqual(factory.assumptions(REPO, job["id"]), {"marked": [], "confirmed": [], "unconfirmed": []})
        (Path(job["job_dir"]) / "spec.md").unlink()
        with self.assertRaisesRegex(ValueError, "has no spec.md yet"):
            factory.confirm(REPO, job["id"])


class ReviewTests(FactoryTestCase):
    def test_changes_resume_the_builder_session_then_merge(self) -> None:
        factory = self.start(reviewer_code=reviews("CHANGES", "PASS"))
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        first, second = self.recorder.of("builder")
        self.assertEqual((first.kind, second.kind), ("run", "resume"))
        self.assertEqual(second.session, str(Path(job["job_dir"]) / "sessions" / "builder.json"))
        self.assertIn(f"Round 1 reviews are in {job['job_dir']}/round-1/. Address every review whose first line "
                      f"is VERDICT: CHANGES, commit, and update build.md for round 2.", second.prompt)
        self.assertEqual((job["round"], job["resumes"]), (2, 1))
        self.assertEqual([h["reviews"]["code"] for h in job["history"]], ["CHANGES", "PASS"])
        self.assertEqual(self.recorder.of("approver")[0].prompt.splitlines()[-1], "Final round: 2.")
        self.assertEqual(job["agents"]["builder"]["runs"], 2)

    def test_rounds_exhausted_writes_decision_and_stops(self) -> None:
        factory = self.start(self.config(pipeline={"max_resumes": 1}), reviewer_code=reviews("CHANGES"))
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "needs-human")
        self.assertEqual(job["decision"], "ESCALATE")
        self.assertIn("review rounds exhausted", job["reason"])
        self.assertEqual((Path(job["job_dir"]) / "decision.md").read_text(),
                         "ESCALATE\n\n## Why\nReview rounds exhausted. Outstanding CHANGES in round-2/ from: code.\n")
        self.assertEqual(self.recorder.of("approver"), [])
        self.assertEqual(len(job["history"]), 2)
        self.assertTrue(Path(job["worktree"]).is_dir())

    def test_missing_review_is_rerun_once_then_error(self) -> None:
        factory = self.start(reviewer_code=reviews(None))
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "needs-human")
        self.assertEqual(job["reason"], "reviewer code failed")
        self.assertEqual(job["reviews"], {"security": "PASS", "code": "ERROR"})
        first, second = self.recorder.of("reviewer-code")
        self.assertEqual((first.kind, second.kind), ("run", "run"))
        self.assertTrue(second.prompt.endswith(f"Review round 1.\n{REVIEW_RETRY}"))
        self.assertEqual(job["history"][0]["reviews"]["code"], "ERROR")

    def test_invalid_review_then_valid_rerun_passes(self) -> None:
        present_at_start = []

        def flaky(ctx: RunContext, call: Call) -> None:
            present_at_start.append(ctx.write_paths[0].exists())
            ctx.write_paths[0].write_text("Looks fine to me\n" if call.index == 0 else "**VERDICT: PASS**\n")
        job = self.run_job(self.start(reviewer_code=flaky))
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertEqual(present_at_start, [False, False], "the review file is removed before the re-run too")

    def test_reviewer_run_error_fails_the_job_and_retry_keeps_finished_verdicts(self) -> None:
        def down_once(ctx: RunContext, call: Call) -> AgentRun | None:
            if call.index == 0:
                return AgentRun(ok=False, error="provider down")
            review_pass(ctx, call)
            return None
        factory = self.start(reviewer_code=down_once)
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "failed")
        self.assertEqual(job["failed_step"], "review")
        self.assertIn("reviewer-code failed: provider down", job["reason"])
        factory.retry(REPO, job["id"])
        job = factory.wait(REPO, job["id"], timeout=TIMEOUT)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertEqual(len(self.recorder.of("reviewer-security")), 1, "a PASS of the same code is kept")
        self.assertEqual(len(self.recorder.of("reviewer-code")), 2)
        self.assertEqual(len(job["history"]), 1)

    def test_reviewer_edits_are_stashed(self) -> None:
        def meddle(ctx: RunContext, call: Call) -> None:
            (ctx.cwd / "README.md").write_text("reviewer was here\n")
            (ctx.cwd / "scratch.txt").write_text("x\n")
            review_pass(ctx, call)
        factory = self.start(reviewer_security=meddle)
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertIn("factory: discarded reviewer edits round 1", run_git(self.repo, "stash", "list"))
        self.assertEqual((self.repo / "README.md").read_text(), "# test repo\n")
        warnings = [e.data["message"] for e in self.events(job, "log") if e.data["level"] == "warn"]
        self.assertTrue(any("reviewer edits round 1" in w for w in warnings))

    def test_reviewer_commit_moves_the_branch_and_escalates(self) -> None:
        def sneaky(ctx: RunContext, call: Call) -> None:
            commit_file(ctx.cwd, "sneaky.txt", "x\n", "sneaky")
            review_pass(ctx, call)
        job = self.run_job(self.start(reviewer_code=sneaky))
        self.assertEqual(job["stage"], "needs-human")
        self.assertIn("branch moved during review round 1", job["reason"])

    def test_three_reviewers_with_a_custom_lens(self) -> None:
        agents = self.repo / ".factory" / "agents"
        agents.mkdir(parents=True)
        source = (Path(__file__).resolve().parent.parent / "agents" / "reviewer-code.md").read_text()
        (agents / "reviewer-perf.md").write_text(source.replace("reviewer-code", "reviewer-perf"))
        factory = self.start(self.config(pipeline={"reviewers": ["ux", "code", "perf"]}))
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertEqual(job["reviewers"], ["ux", "code", "perf"])
        self.assertEqual(job["reviews"], {"ux": "PASS", "code": "PASS", "perf": "PASS"})
        self.assertTrue((Path(job["job_dir"]) / "round-1" / "review-perf.md").is_file())
        self.assertEqual(set(job["routing"]), {"spec-writer", "builder", "approver", "reviewer-ux", "reviewer-code",
                                               "reviewer-perf"})
        perf, ux = self.recorder.of("reviewer-perf")[0], self.recorder.of("reviewer-ux")[0]
        self.assertTrue(perf.prompt.endswith("Review round 1."))
        self.assertTrue(ux.prompt.endswith("Review round 1.\nPort: 5111."))
        self.assertEqual(perf.ctx.agent.path, agents / "reviewer-perf.md")

    def test_reviewer_without_agent_definition_is_refused_at_submit(self) -> None:
        factory = self.start(self.config(pipeline={"reviewers": ["code", "nope"]}))
        with self.assertRaisesRegex(ValueError, "reviewer-nope"):
            factory.submit(REPO, "Anything")
        self.assertEqual(factory.jobs(), [])


class BuildCheckTests(FactoryTestCase):
    def test_blocked_builder_needs_a_human(self) -> None:
        def blocked(ctx: RunContext, call: Call) -> None:
            write_build(ctx, "BLOCKED", "\n## Blocked\n\nNeeds a product decision.\n")
        job = self.run_job(self.start(builder=blocked))
        self.assertEqual(job["stage"], "needs-human")
        self.assertEqual(job["reason"], "builder blocked: Needs a product decision.")
        self.assertEqual(self.recorder.of("reviewer-code"), [])

    def test_builder_without_commits_needs_a_human(self) -> None:
        job = self.run_job(self.start(builder=lambda ctx, call: write_build(ctx, "READY")))
        self.assertEqual((job["stage"], job["reason"]), ("needs-human", "builder made no commits"))
        self.assertEqual(len(self.recorder.of("builder")), 1)

    def test_dirty_tree_gets_one_resume(self) -> None:
        def messy(ctx: RunContext, call: Call) -> None:
            if call.index == 0:
                (ctx.cwd / "wip.txt").write_text("unfinished\n")
                write_build(ctx, "READY")
            else:
                run_git(ctx.cwd, "add", "wip.txt")
                run_git(ctx.cwd, "commit", "-q", "-m", "finish")
        job = self.run_job(self.start(builder=messy))
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        _, second = self.recorder.of("builder")
        self.assertEqual(second.kind, "resume")
        self.assertTrue(second.prompt.endswith(f"{COMMIT_FIX}\nPort: 5110."))

    def test_still_dirty_after_the_resume_needs_a_human(self) -> None:
        def messy(ctx: RunContext, call: Call) -> None:
            if call.index == 0:
                commit_file(ctx.cwd, "done.txt", "done\n")
            (ctx.cwd / f"wip-{call.index}.txt").write_text("unfinished\n")
            write_build(ctx, "READY")
        job = self.run_job(self.start(builder=messy))
        self.assertEqual((job["stage"], job["reason"]), ("needs-human", "builder left uncommitted changes"))
        self.assertEqual(len(self.recorder.of("builder")), 2)

    def test_invalid_build_md_gets_one_resume(self) -> None:
        def sloppy(ctx: RunContext, call: Call) -> None:
            commit_file(ctx.cwd, f"f{call.index}.txt", "x\n")
            (ctx.job_dir / "build.md").write_text("Done!\n")
        job = self.run_job(self.start(builder=sloppy))
        self.assertEqual((job["stage"], job["reason"]), ("needs-human", "builder wrote no valid build.md"))
        second = self.recorder.of("builder")[1]
        self.assertEqual(second.kind, "resume")
        self.assertIn(BUILD_MD_FIX, second.prompt)

    def test_builder_run_error_fails_and_retry_reruns_it(self) -> None:
        def down_once(ctx: RunContext, call: Call) -> AgentRun | None:
            if call.index == 0:
                return AgentRun(ok=False, error="connection refused")
            build(ctx, call)
            return None
        factory = self.start(builder=down_once)
        job = self.run_job(factory)
        self.assertEqual((job["stage"], job["failed_step"]), ("failed", "build"))
        self.assertEqual(job["reason"], "builder failed: connection refused")
        job = factory.retry(REPO, job["id"])
        self.assertEqual((job["stage"], job["failed_step"], job["reason"]), ("build", None, None))
        job = factory.wait(REPO, job["id"], timeout=TIMEOUT)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertTrue(self.recorder.of("builder")[1].prompt.endswith("Build round 1.\nPort: 5110."))


class SpecTests(FactoryTestCase):
    def test_missing_spec_retries_once_then_fails_and_retry_resumes(self) -> None:
        factory = self.start(spec_writer=lambda ctx, call: None)
        job = self.run_job(factory)
        self.assertEqual((job["stage"], job["failed_step"]), ("failed", "spec"))
        self.assertEqual(job["reason"], "spec-writer wrote no spec.md")
        calls = self.recorder.of("spec-writer")
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[1].prompt.endswith(f"Feature: Add a greeting\n{SPEC_RETRY}"))
        self.recorder.actions["spec-writer"] = write_spec
        factory.retry(REPO, job["id"])
        job = factory.wait(REPO, job["id"], timeout=TIMEOUT)
        self.assertEqual(job["stage"], "merged", job.get("reason"))

    def test_spec_writer_error_fails_without_a_second_run(self) -> None:
        job = self.run_job(self.start(spec_writer=failing("401 unauthorized")))
        self.assertEqual((job["stage"], job["failed_step"]), ("failed", "spec"))
        self.assertIn("spec-writer failed: 401 unauthorized", job["reason"])
        self.assertEqual(len(self.recorder.of("spec-writer")), 1)

    def test_setup_command_failure_and_retry(self) -> None:
        config = self.config(repos=[{"name": REPO, "path": str(self.repo), "setup": "echo setting-up; exit 3"}])
        factory = self.start(config)
        job = self.run_job(factory)
        self.assertEqual((job["stage"], job["failed_step"]), ("failed", "setup"))
        self.assertEqual(job["reason"], "setup command failed (exit 3): setting-up")
        self.assertTrue(any("setting-up" in e.data["message"] for e in self.events(job, "log")))
        factory.reload_config(self.config(repos=[{"name": REPO, "path": str(self.repo), "setup": "true"}]))
        factory.retry(REPO, job["id"])
        job = factory.wait(REPO, job["id"], timeout=TIMEOUT)
        self.assertEqual(job["stage"], "merged", job.get("reason"))


class ApproverAndGateTests(FactoryTestCase):
    def test_approver_escalates(self) -> None:
        def escalate(ctx: RunContext, call: Call) -> None:
            (ctx.job_dir / "decision.md").write_text("ESCALATE\n\n## Why\nTouches billing.\n")
        job = self.run_job(self.start(approver=escalate))
        self.assertEqual((job["stage"], job["decision"]), ("needs-human", "ESCALATE"))
        self.assertEqual(job["reason"], "approver escalated: Touches billing.")

    def test_approver_without_decision_fails_closed(self) -> None:
        job = self.run_job(self.start(approver=lambda ctx, call: None))
        self.assertEqual((job["stage"], job["decision"]), ("needs-human", None))
        self.assertEqual(job["reason"], "approver wrote no valid decision")

    def test_protected_path_escalates_and_human_approve_overrides(self) -> None:
        def ci(ctx: RunContext, call: Call) -> None:
            commit_file(ctx.cwd, ".github/workflows/ci.yml", "on: push\n")
            write_build(ctx, "READY")
        factory = self.start(builder=ci)
        job = self.run_job(factory)
        self.assertEqual((job["stage"], job["reason"]), ("needs-human", "touches protected path .github/workflows/ci.yml"))
        job = factory.approve(REPO, job["id"])
        self.assertEqual(job["stage"], "merging")
        job = factory.wait(REPO, job["id"], timeout=TIMEOUT)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertTrue((self.repo / ".github" / "workflows" / "ci.yml").is_file())
        self.assertEqual(len(self.recorder.of("approver")), 1)

    def test_repo_escalate_paths_and_factory_agents_are_protected(self) -> None:
        for rel, globs in (("secrets/prod.txt", ["secrets/**"]), (".factory/agents/approver.md", [])):
            with self.subTest(rel=rel):
                def touch(ctx: RunContext, call: Call, rel: str = rel) -> None:
                    commit_file(ctx.cwd, rel, "x\n")
                    write_build(ctx, "READY")
                config = self.config(repos=[{"name": REPO, "path": str(self.repo), "escalate_paths": globs}])
                factory = self.start(config, builder=touch)
                job = self.run_job(factory)
                self.assertEqual(job["reason"], f"touches protected path {rel}")
                factory.stop()

    def test_symlink_escalates(self) -> None:
        def link(ctx: RunContext, call: Call) -> None:
            os.symlink("README.md", ctx.cwd / "link")
            run_git(ctx.cwd, "add", "link")
            run_git(ctx.cwd, "commit", "-q", "-m", "link")
            write_build(ctx, "READY")
        job = self.run_job(self.start(builder=link))
        self.assertEqual((job["stage"], job["reason"]), ("needs-human", "touches symlink link"))

    def test_submodule_gitlink_escalates(self) -> None:
        def vendor(ctx: RunContext, call: Call) -> None:
            make_repo(ctx.cwd, "vendor")
            run_git(ctx.cwd, "add", "vendor")
            run_git(ctx.cwd, "commit", "-q", "-m", "vendor a repo")
            write_build(ctx, "READY")
        job = self.run_job(self.start(builder=vendor))
        self.assertEqual((job["stage"], job["reason"]), ("needs-human", "touches submodule vendor"))

    def test_cancel_is_refused_once_the_merge_runs(self) -> None:
        merging, release = threading.Event(), threading.Event()
        real_merge = gitops.merge

        def slow_merge(*args: Any, **kwargs: Any) -> str:
            merging.set()
            release.wait(TIMEOUT)
            return real_merge(*args, **kwargs)
        with mock.patch("swfactory.gitops.merge", side_effect=slow_merge):
            factory = self.start()
            self.addCleanup(release.set)
            job = factory.submit(REPO, "Merge me")
            self.assertTrue(merging.wait(TIMEOUT))
            with self.assertRaisesRegex(ActionError, "merging right now"):
                factory.cancel(REPO, job["id"])
            release.set()
            self.assertEqual(factory.wait(REPO, job["id"], timeout=TIMEOUT)["stage"], "merged")

    def test_stash_failure_needs_a_human(self) -> None:
        refusal = gitops.GitError(["git", "stash"], "changes git could not stash remain")
        with mock.patch("swfactory.gitops.stash_all", side_effect=[False, refusal]):
            job = self.run_job(self.start())
        self.assertEqual(job["stage"], "needs-human")
        self.assertIn("cannot discard reviewer edits round 1: git stash", job["reason"])

    def test_a_worktree_repointed_at_another_repository_is_refused(self) -> None:
        """A builder that points <worktree>/.git at a repository of its own: host-side status refuses it."""
        other = make_repo(self.tmp, "other")
        run_git(other, "worktree", "add", "-q", "-b", "evil", str(self.tmp / "other-wt"), "main")

        def repoint(ctx: RunContext, call: Call) -> None:
            build(ctx, call)
            (ctx.cwd / ".git").write_text(f"gitdir: {other / '.git' / 'worktrees' / 'other-wt'}\n")
        job = self.run_job(self.start(builder=repoint))
        self.assertEqual(job["stage"], "failed")
        self.assertIn("refusing to run git in", job["reason"])
        self.assertEqual(run_git(other, "stash", "list"), "")

    def test_a_reviewer_repointing_the_worktree_cannot_hide_its_edits(self) -> None:
        other = make_repo(self.tmp, "other")
        run_git(other, "worktree", "add", "-q", "-b", "evil", str(self.tmp / "other-wt"), "main")

        def repoint(ctx: RunContext, call: Call) -> None:
            review_pass(ctx, call)
            (ctx.cwd / "README.md").write_text("reviewer edit\n")
            (ctx.cwd / ".git").write_text(f"gitdir: {other / '.git' / 'worktrees' / 'other-wt'}\n")
        job = self.run_job(self.start(reviewer=repoint))
        self.assertEqual(job["stage"], "needs-human")
        self.assertIn("cannot discard reviewer edits round 1", job["reason"])
        self.assertIn("refusing to run git in", job["reason"])

    def test_size_limit_escalates(self) -> None:
        def big(ctx: RunContext, call: Call) -> None:
            commit_file(ctx.cwd, "big.txt", "".join(f"line {i}\n" for i in range(10)))
            write_build(ctx, "READY")
        job = self.run_job(self.start(self.config(pipeline={"max_auto_merge_lines": 5}), builder=big))
        self.assertEqual(job["reason"], "changes 10 lines (max_auto_merge_lines is 5)")

    def test_auto_merge_disabled_then_human_approve(self) -> None:
        factory = self.start(self.config(pipeline={"auto_merge": False}))
        job = self.run_job(factory)
        self.assertEqual((job["stage"], job["reason"]), ("needs-human", "auto-merge disabled"))
        factory.approve(REPO, job["id"])
        self.assertEqual(factory.wait(REPO, job["id"], timeout=TIMEOUT)["stage"], "merged")

    def test_conflict_escalates_even_on_human_approve(self) -> None:
        def clash(ctx: RunContext, call: Call) -> None:
            build(ctx, call)
            commit_file(self.repo, f"feature-{call.index}.txt", "theirs\n", "conflicting change on main")
        factory = self.start(builder=clash)
        job = self.run_job(factory)
        self.assertEqual((job["stage"], job["reason"]), ("needs-human", "merge conflicts with main: feature-0.txt"))
        factory.approve(REPO, job["id"])
        job = factory.wait(REPO, job["id"], timeout=TIMEOUT)
        self.assertEqual((job["stage"], job["reason"]), ("needs-human", "merge conflicts with main: feature-0.txt"))
        self.assertTrue(self.branch_exists(job["branch"]))

    def test_merge_busy_keeps_the_approval(self) -> None:
        def approve_and_dirty_main(ctx: RunContext, call: Call) -> None:
            approve(ctx, call)
            (self.repo / "README.md").write_text("local edit\n")
        factory = self.start(approver=approve_and_dirty_main)
        job = self.run_job(factory)
        self.assertEqual((job["stage"], job["decision"], job["reason"]), ("needs-human", "APPROVE", MERGE_BUSY))
        run_git(self.repo, "checkout", "--", "README.md")
        factory.approve(REPO, job["id"])
        self.assertEqual(factory.wait(REPO, job["id"], timeout=TIMEOUT)["stage"], "merged")

    def test_push_only_when_configured(self) -> None:
        remote = self.tmp / "remote.git"
        run_git(self.tmp, "init", "-q", "--bare", str(remote))
        run_git(self.repo, "remote", "add", "origin", str(remote))
        job = self.run_job(self.start())
        self.assertEqual(job["stage"], "merged")
        self.assertEqual(run_git(remote, "branch", "--list").strip(), "")
        factory = self.start(self.config(merge={"push": True}))
        job = self.run_job(factory, "Second feature")
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertEqual(run_git(remote, "rev-parse", "main").strip(), job["merged_commit"])


class DeliveryTests(FactoryTestCase):
    """DESIGN §25: merge (default), branch and folder delivery after APPROVE."""

    def main_head(self) -> str:
        return run_git(self.repo, "rev-parse", "main").strip()

    def test_branch_delivery_keeps_the_branch_and_a_human_approve_merges_it(self) -> None:
        factory = self.start()
        before = self.main_head()
        job = self.run_job(factory, delivery="branch")
        self.assertEqual(job["stage"], "delivered", job.get("reason"))
        self.assertEqual(job["delivery"], "branch")
        record = job["delivered"]
        self.assertEqual((record["mode"], record["branch"], record["base"]), ("branch", job["branch"], "main"))
        self.assertEqual(record["commit"], run_git(self.repo, "rev-parse", job["branch"]).strip())
        self.assertEqual((record["by"], job["approved_by"]), ("approver", "approver"))
        self.assertNotIn("path", record)
        self.assertRegex(record["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertIn(f"on branch {job['branch']}", job["detail"])
        self.assertEqual(self.main_head(), before, "nothing is merged")
        self.assertTrue(self.branch_exists(job["branch"]))
        self.assertFalse(Path(job["worktree"]).exists(), "the branch is kept without its worktree")
        self.assertNotIn(job["worktree"], run_git(self.repo, "worktree", "list"))
        stages = []
        for event in self.events(job, "job.updated"):
            if not stages or stages[-1] != event.data["job"]["stage"]:
                stages.append(event.data["job"]["stage"])
        self.assertEqual(stages, ["spec", "build", "review", "approve", "delivered"])
        job = factory.approve(REPO, job["id"])
        self.assertEqual(job["stage"], "merging")
        job = factory.wait(REPO, job["id"], timeout=TIMEOUT)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertTrue((self.repo / "feature-0.txt").is_file())
        self.assertFalse(self.branch_exists(job["branch"]))
        self.assertEqual(job["delivered"], record, "the delivery stays on record")

    def test_folder_delivery_exports_patch_files_and_report(self) -> None:
        def work(ctx: RunContext, call: Call) -> None:
            commit_file(ctx.cwd, "src/app.py", "print('hi')\n", "add app")
            (ctx.cwd / "bin").mkdir()
            (ctx.cwd / "bin" / "run.sh").write_text("#!/bin/sh\necho run\n")
            os.chmod(ctx.cwd / "bin" / "run.sh", 0o755)
            (ctx.cwd / "data.bin").write_bytes(b"\x00\x01\xff")
            os.symlink("README.md", ctx.cwd / "link")
            run_git(ctx.cwd, "rm", "-q", "README.md")
            run_git(ctx.cwd, "add", "bin/run.sh", "data.bin", "link")
            run_git(ctx.cwd, "commit", "-q", "-m", "more")
            write_build(ctx, "READY", "## Summary\nAdds the app.\n")
        out = self.tmp / "out"
        factory = self.start(self.config(merge={"output_dir": str(out)}), builder=work)
        before = self.main_head()
        job = self.run_job(factory, "Ship the app", delivery="folder")
        self.assertEqual(job["stage"], "delivered", job.get("reason"))
        folder = out / REPO / job["id"]
        record = job["delivered"]
        self.assertEqual((record["mode"], record["path"], record["files"]), ("folder", str(folder), 3))
        self.assertEqual(record["skipped"], ["symlink link"])
        self.assertEqual(self.main_head(), before)
        self.assertTrue(self.branch_exists(job["branch"]))
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["REPORT.md", "changes.patch", "files"])
        files = folder / "files"
        self.assertEqual(sorted(str(p.relative_to(files)) for p in files.rglob("*") if p.is_file()),
                         ["bin/run.sh", "data.bin", "src/app.py"])
        self.assertEqual((files / "src" / "app.py").read_text(), "print('hi')\n")
        self.assertEqual((files / "data.bin").read_bytes(), b"\x00\x01\xff")
        self.assertTrue(os.access(files / "bin" / "run.sh", os.X_OK))
        self.assertFalse(os.access(files / "src" / "app.py", os.X_OK))
        # the patch applies with git am on a fresh clone of the base
        clone = self.tmp / "clone"
        run_git(self.tmp, "clone", "-q", "--branch", "main", str(self.repo), str(clone))
        run_git(clone, "config", "user.name", "T")
        run_git(clone, "config", "user.email", "t@example.invalid")
        run_git(clone, "am", "-q", str(folder / "changes.patch"))
        self.assertEqual(run_git(clone, "rev-parse", "HEAD^{tree}"),
                         run_git(self.repo, "rev-parse", f"{job['branch']}^{{tree}}"))
        report = (folder / "REPORT.md").read_text()
        for text in (f"# Delivery report: {REPO}/{job['id']}", "## Feature\n\nShip the app", "## Spec summary",
                     "Add a greeting.", "## Build summary", "Adds the app.", "| round | security | code |",
                     "| 1 | PASS | PASS |", "## Decision\n\n**APPROVE**: All good.", "`link` (symlink)",
                     "Deleted on the branch: `README.md`", "git am changes.patch"):
            self.assertIn(text, report)

    def test_a_human_approve_delivers_a_folder_job_that_stopped_before_its_delivery(self) -> None:
        def escalate(ctx: RunContext, call: Call) -> None:
            (ctx.job_dir / "decision.md").write_text("ESCALATE\n\n## Why\nUnsure about the API.\n")
        out = self.tmp / "out"
        factory = self.start(self.config(merge={"output_dir": str(out)}), approver=escalate)
        before = self.main_head()
        job = self.run_job(factory, delivery="folder")
        self.assertEqual((job["stage"], job["decision"], job.get("delivered")), ("needs-human", "ESCALATE", None))
        job = factory.approve(REPO, job["id"])
        self.assertEqual((job["stage"], job["step"], job["approved_by"]), ("approve", "deliver", "human"))
        self.assertEqual(job["detail"], "approved by a human: delivering as a folder")
        job = factory.wait(REPO, job["id"], timeout=TIMEOUT)
        self.assertEqual(job["stage"], "delivered", job.get("reason"))
        self.assertEqual(self.main_head(), before, "a human approve does not skip the delivery: nothing merged")
        folder = out / REPO / job["id"]
        self.assertEqual((job["delivered"]["path"], job["delivered"]["by"]), (str(folder), "human"))
        report = (folder / "REPORT.md").read_text()
        self.assertIn("## Decision\n\nApproved for delivery by a human in the factory", report)
        self.assertIn("**ESCALATE**: Unsure about the API.", report)
        self.assertEqual(len(self.recorder.of("approver")), 1, "the approver is not asked again")
        job = factory.approve(REPO, job["id"])   # and approving the delivered job merges it
        self.assertEqual((job["stage"], job["step"]), ("merging", "override"))
        job = factory.wait(REPO, job["id"], timeout=TIMEOUT)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertNotEqual(self.main_head(), before)
        self.assertEqual(job["delivered"]["by"], "human", "the delivery stays on record")

    def test_a_human_approve_delivers_a_branch_job_and_skips_only_the_review_rule(self) -> None:
        factory = self.start(self.config(pipeline={"max_resumes": 0}), reviewer_code=reviews("CHANGES"))
        before = self.main_head()
        job = self.run_job(factory, delivery="branch")
        self.assertEqual(job["stage"], "needs-human", job.get("reason"))
        self.assertEqual(job["reviews"]["code"], "CHANGES")
        factory.approve(REPO, job["id"])
        job = factory.wait(REPO, job["id"], timeout=TIMEOUT)
        self.assertEqual(job["stage"], "delivered", job.get("reason"))
        self.assertEqual((job["delivered"]["mode"], job["delivered"]["by"]), ("branch", "human"))
        self.assertNotIn("path", job["delivered"])
        self.assertEqual(self.main_head(), before)
        self.assertTrue(self.branch_exists(job["branch"]))
        self.assertFalse(Path(job["worktree"]).exists())

    def test_a_retried_folder_delivery_replaces_the_export(self) -> None:
        out = self.tmp / "out"
        factory = self.start(self.config(merge={"output_dir": str(out)}))
        from swfactory import delivery as real
        with mock.patch("swfactory.orchestrator.export_folder", side_effect=OSError("disk full")):
            job = self.run_job(factory, delivery="folder")
        self.assertEqual((job["stage"], job["failed_step"]), ("failed", "deliver"))
        self.assertEqual(job["reason"], "folder delivery failed: disk full")
        folder = out / REPO / job["id"]
        folder.mkdir(parents=True)
        (folder / "stale.txt").write_text("old export\n")
        with mock.patch("swfactory.orchestrator.export_folder", wraps=real.export_folder) as export:
            factory.retry(REPO, job["id"])
            job = factory.wait(REPO, job["id"], timeout=TIMEOUT)
        self.assertEqual(job["stage"], "delivered", job.get("reason"))
        export.assert_called_once()
        self.assertFalse((folder / "stale.txt").exists())
        self.assertTrue((folder / "REPORT.md").is_file())
        self.assertEqual(list(folder.parent.glob(".*")), [], "no staging or old folder left behind")

    def test_delivery_skips_the_path_size_and_auto_merge_rules(self) -> None:
        def ci(ctx: RunContext, call: Call) -> None:
            commit_file(ctx.cwd, ".github/workflows/ci.yml", "on: push\n" * 20)
            write_build(ctx, "READY")
        config = self.config(pipeline={"auto_merge": False, "max_auto_merge_lines": 5})
        factory = self.start(config, builder=ci)
        job = self.run_job(factory, delivery="branch")
        self.assertEqual(job["stage"], "delivered", job.get("reason"))
        factory.approve(REPO, job["id"])   # the human override merges it anyway
        self.assertEqual(factory.wait(REPO, job["id"], timeout=TIMEOUT)["stage"], "merged")

    def test_default_delivery_comes_from_the_config_and_is_validated(self) -> None:
        factory = self.start(self.config(merge={"delivery": "branch"}))
        self.assertEqual(factory.check_delivery(), "branch")
        self.assertEqual(factory.check_delivery("folder"), "folder")
        with self.assertRaisesRegex(ValueError, r"delivery must be one of merge, branch, folder \(got 'zip'\)"):
            factory.submit(REPO, "Nope", delivery="zip")
        self.assertEqual(factory.jobs(), [])
        job = self.run_job(factory)
        self.assertEqual((job["delivery"], job["stage"]), ("branch", "delivered"))
        factory.add_backlog(REPO, "From the backlog")
        self.assertEqual(factory.next(REPO)["delivery"], "branch")

    def test_an_old_job_without_delivery_merges(self) -> None:
        factory = self.start()
        job = factory.submit(REPO, "Old style")
        with factory._job_lock((REPO, job["id"])):
            factory._jobs[(REPO, job["id"])].pop("delivery")
        self.assertEqual(factory.wait(REPO, job["id"], timeout=TIMEOUT)["stage"], "merged")

    def test_rework_after_a_delivery_checks_the_branch_out_again(self) -> None:
        factory = self.start()
        job = self.run_job(factory, delivery="branch")
        self.assertEqual(job["stage"], "delivered")
        commit_file(self.repo, "feature-0.txt", "theirs\n", "conflicting change on main")
        factory.approve(REPO, job["id"])
        job = factory.wait(REPO, job["id"], timeout=TIMEOUT)
        self.assertEqual((job["stage"], job["reason"]), ("needs-human", "merge conflicts with main: feature-0.txt"))
        first = job["delivered"]
        self.assertEqual(first["commit"], run_git(self.repo, "rev-parse", job["branch"]).strip())
        job = factory.approve(REPO, job["id"])   # delivered already: the approve is the merge override again
        self.assertEqual((job["stage"], job["step"]), ("merging", "override"))
        job = factory.wait(REPO, job["id"], timeout=TIMEOUT)
        self.assertEqual((job["stage"], job["reason"]), ("needs-human", "merge conflicts with main: feature-0.txt"))
        self.assertEqual(job["delivered"], first)
        job = factory.rework(REPO, job["id"], "Resolve the conflict with main.")
        self.assertEqual((job["delivered"], job["approved_by"]), (None, None), "the branch moves: delivered again")
        job = factory.wait(REPO, job["id"], timeout=TIMEOUT)
        self.assertEqual(job["stage"], "delivered", job.get("reason"))
        self.assertEqual(job["delivered"]["commit"], run_git(self.repo, "rev-parse", job["branch"]).strip())
        self.assertNotEqual(job["delivered"]["commit"], first["commit"])
        self.assertEqual(job["delivered"]["by"], "approver")
        rework = self.recorder.of("builder")[-1]
        self.assertEqual(rework.ctx.cwd, Path(job["worktree"]))
        self.assertIn(f"re-created the worktree {job['worktree']} from {job['branch']}",
                      [e.data["message"] for e in self.events(job, "log")])
        self.assertFalse(Path(job["worktree"]).exists(), "delivered again: the worktree is gone again")

    def test_reject_a_delivered_job_drops_the_branch_and_keeps_the_export(self) -> None:
        out = self.tmp / "out"
        factory = self.start(self.config(merge={"output_dir": str(out), "delivery": "folder"}))
        job = self.run_job(factory)
        job = factory.reject(REPO, job["id"], "not needed")
        self.assertEqual(job["stage"], "rejected")
        self.assertFalse(self.branch_exists(job["branch"]))
        self.assertTrue((out / REPO / job["id"] / "REPORT.md").is_file())
        with self.assertRaises(ActionError):
            factory.approve(REPO, job["id"])


class HumanActionTests(FactoryTestCase):
    def escalating_once(self) -> Action:
        def act(ctx: RunContext, call: Call) -> None:
            if call.index:
                return approve(ctx, call)
            (ctx.job_dir / "decision.md").write_text("ESCALATE\n\n## Why\nNeeds tests.\n")
        return act

    def test_rework_resumes_the_builder_with_the_note(self) -> None:
        factory = self.start(approver=self.escalating_once())
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "needs-human")
        with self.assertRaises(ValueError):
            factory.rework(REPO, job["id"], "  ")
        job = factory.rework(REPO, job["id"], "Please add tests.")
        self.assertEqual((job["stage"], job["resumes"], job["round"], job["decision"]), ("build", 0, 2, None))
        self.assertEqual((Path(job["job_dir"]) / "rework-1.md").read_text(), "Please add tests.\n")
        job = factory.wait(REPO, job["id"], timeout=TIMEOUT)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        rework = self.recorder.of("builder")[1]
        self.assertEqual(rework.kind, "resume")
        self.assertIn(f"Rework: read {job['job_dir']}/rework-1.md and address it, then update build.md for round 2.",
                      rework.prompt)
        self.assertEqual([h["round"] for h in job["history"]], [1, 2])

    def test_reject_removes_worktree_and_branch(self) -> None:
        factory = self.start(approver=self.escalating_once())
        job = self.run_job(factory)
        job = factory.reject(REPO, job["id"], "not wanted")
        self.assertEqual((job["stage"], job["reason"]), ("rejected", "not wanted"))
        self.assertFalse(Path(job["worktree"]).exists())
        self.assertFalse(self.branch_exists(job["branch"]))
        for action in (factory.approve, factory.retry, factory.cancel, factory.reject):
            with self.assertRaises(ActionError):
                action(REPO, job["id"])

    def test_actions_validate_the_stage(self) -> None:
        factory = self.start()
        job = self.run_job(factory)
        with self.assertRaises(ActionError):
            factory.approve(REPO, job["id"])
        with self.assertRaises(ActionError):
            factory.rework(REPO, job["id"], "more")
        with self.assertRaises(ActionError):
            factory.retry(REPO, job["id"])
        with self.assertRaises(ActionError):
            factory.set_routing(REPO, job["id"], {"builder": {"model": "sonnet"}})
        with self.assertRaises(KeyError):
            factory.approve(REPO, "999-nope")

    def test_cancel_a_running_job_then_reject(self) -> None:
        def slow(ctx: RunContext, call: Call) -> AgentRun:
            ctx.cancel.wait(TIMEOUT)
            return AgentRun(ok=False, error="cancelled")
        factory = self.start(builder=slow)
        job = factory.submit(REPO, "Slow feature")
        until(lambda: self.recorder.of("builder"))
        job = factory.cancel(REPO, job["id"])
        self.assertEqual(job["stage"], "cancelled")
        job = factory.wait(REPO, job["id"], timeout=TIMEOUT)
        self.assertEqual(job["stage"], "cancelled")
        self.assertTrue(Path(job["worktree"]).is_dir(), "kept until reject")
        with self.assertRaises(ActionError):
            factory.cancel(REPO, job["id"])
        job = factory.reject(REPO, job["id"])
        self.assertEqual(job["stage"], "rejected")
        self.assertFalse(Path(job["worktree"]).exists())

    def test_cancel_mid_review_keeps_landed_verdicts_and_settles_the_rest(self) -> None:
        def finished(ctx: RunContext, call: Call) -> None:      # writes its verdict; its run ends normally
            review_pass(ctx, call)
            ctx.cancel.wait(TIMEOUT)

        def interrupted(ctx: RunContext, call: Call) -> AgentRun:   # wrote a verdict, but the run was cut short
            review_pass(ctx, call)
            ctx.cancel.wait(TIMEOUT)
            return AgentRun(ok=False, error="cancelled")
        factory = self.start(reviewer_security=finished, reviewer_code=interrupted)
        job = factory.submit(REPO, "Review me")
        until(lambda: self.recorder.of("reviewer-security") and self.recorder.of("reviewer-code"))
        job = factory.cancel(REPO, job["id"])
        self.assertNotIn("running", job["reviews"].values())
        job = factory.wait(REPO, job["id"], timeout=TIMEOUT)
        self.assertEqual((job["stage"], job["detail"]), ("cancelled", "cancelled"))
        self.assertEqual(job["reviews"], {"security": "PASS", "code": "pending"})
        job = factory.reject(REPO, job["id"])
        self.assertEqual((job["stage"], job["reviews"]), ("rejected", {"security": "PASS", "code": "pending"}))

    def test_cancel_a_queued_job(self) -> None:
        release = threading.Event()

        def gated(ctx: RunContext, call: Call) -> None:
            release.wait(TIMEOUT)
            build(ctx, call)
        factory = self.start(builder=gated)
        self.addCleanup(release.set)
        first = factory.submit(REPO, "First")
        until(lambda: self.recorder.of("builder"))
        second = factory.submit(REPO, "Second")
        self.assertEqual(factory.cancel(REPO, second["id"])["stage"], "cancelled")
        release.set()
        self.assertEqual(factory.wait(REPO, first["id"], timeout=TIMEOUT)["stage"], "merged")
        self.assertEqual(factory.wait(REPO, second["id"], timeout=TIMEOUT)["stage"], "cancelled")
        self.assertEqual(self.recorder.of("spec-writer", second["id"]), [])


class RecoveryTests(FactoryTestCase):
    def test_restart_marks_active_jobs_failed_and_retry_resumes(self) -> None:
        factory = self.start(self.config(pipeline={"auto_merge": False}))
        job = self.run_job(factory)
        factory.stop()
        job.update(stage="review", step="review")
        factory.store.save(job)
        queued = factory.store.new_job(REPO, "Never started", list(self.reviewers), 2)
        queued["step"] = "setup"
        factory.store.save(queued)
        factory2 = self.start()
        job = factory2.job(REPO, job["id"])
        self.assertEqual((job["stage"], job["failed_step"], job["reason"]), ("failed", "review", INTERRUPTED))
        self.assertEqual(factory2.job(REPO, queued["id"])["failed_step"], "setup")
        reviews_before = len(self.recorder.of("reviewer-code"))
        factory2.retry(REPO, job["id"])
        job = factory2.wait(REPO, job["id"], timeout=TIMEOUT)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertEqual(len(self.recorder.of("reviewer-code")), reviews_before, "finished verdicts are kept")
        self.assertEqual(len(self.recorder.of("approver")), 2)

    def test_stop_interrupts_a_running_job(self) -> None:
        def slow(ctx: RunContext, call: Call) -> AgentRun | None:
            if call.index == 0:
                ctx.cancel.wait(TIMEOUT)
                return AgentRun(ok=False, error="cancelled")
            build(ctx, call)
            return None
        factory = self.start(builder=slow)
        job = factory.submit(REPO, "Interrupted")
        until(lambda: self.recorder.of("builder"))
        factory.stop()
        job = factory.job(REPO, job["id"])
        self.assertEqual((job["stage"], job["failed_step"], job["reason"]), ("failed", "build", STOPPED))
        factory2 = self.start()
        factory2.retry(REPO, job["id"])
        self.assertEqual(factory2.wait(REPO, job["id"], timeout=TIMEOUT)["stage"], "merged")

    def test_stop_mid_review_keeps_finished_verdicts_for_the_retry(self) -> None:
        def finished(ctx: RunContext, call: Call) -> None:
            review_pass(ctx, call)
            ctx.cancel.wait(TIMEOUT)

        def stopped_once(ctx: RunContext, call: Call) -> AgentRun | None:
            if call.index == 0:
                ctx.cancel.wait(TIMEOUT)
                return AgentRun(ok=False, error="stopped")
            review_pass(ctx, call)
            return None
        factory = self.start(reviewer_security=finished, reviewer_code=stopped_once)
        job = factory.submit(REPO, "Interrupted review")
        until(lambda: self.recorder.of("reviewer-security") and self.recorder.of("reviewer-code"))
        factory.stop()
        job = factory.store.load(REPO, job["id"])
        self.assertEqual((job["stage"], job["failed_step"], job["reason"]), ("failed", "review", STOPPED))
        self.assertEqual(job["reviews"], {"security": "PASS", "code": "pending"})
        self.recorder.actions["reviewer-security"] = review_pass
        factory2 = self.start()
        factory2.retry(REPO, job["id"])
        self.assertEqual(factory2.wait(REPO, job["id"], timeout=TIMEOUT)["stage"], "merged")
        self.assertEqual(len(self.recorder.of("reviewer-security")), 1, "its finished PASS was kept")
        self.assertEqual(len(self.recorder.of("reviewer-code")), 2)

    def test_restart_settles_running_reviews(self) -> None:
        store = self.start().store
        job = store.new_job(REPO, "Crashed mid-review", list(self.reviewers), 2)
        job.update(stage="review", step="review", round=1, reviews={"security": "PASS", "code": "running"})
        store.save(job)
        factory = self.start()
        job = factory.job(REPO, job["id"])
        self.assertEqual((job["stage"], job["failed_step"]), ("failed", "review"))
        self.assertEqual(job["reviews"], {"security": "PASS", "code": "pending"})

    def test_an_interrupted_merge_can_be_approved(self) -> None:
        factory = self.start(self.config(pipeline={"auto_merge": False}))
        job = self.run_job(factory)
        factory.stop()
        job.update(stage="merging", step="override")
        factory.store.save(job)
        factory2 = self.start()
        job = factory2.job(REPO, job["id"])
        self.assertEqual((job["stage"], job["failed_step"]), ("failed", "merge"))
        with self.assertRaises(ActionError):
            factory2.rework(REPO, job["id"], "not from failed")
        factory2.approve(REPO, job["id"])
        self.assertEqual(factory2.wait(REPO, job["id"], timeout=TIMEOUT)["stage"], "merged")


class FromBranchTests(FactoryTestCase):
    def chat_branch(self) -> str:
        run_git(self.repo, "branch", "chat/c-0123456789")
        worktree = self.tmp / "chat"
        run_git(self.repo, "worktree", "add", "-q", str(worktree), "chat/c-0123456789")
        commit_file(worktree, "chat.txt", "built in chat\n", "chat work")
        run_git(self.repo, "worktree", "remove", str(worktree))
        return "chat/c-0123456789"

    def test_handoff_skips_builder_round_one(self) -> None:
        factory = self.start()
        job = self.run_job(factory, "Ship the chat work", from_branch=self.chat_branch())
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertEqual(self.recorder.of("builder"), [])
        self.assertIn(SPEC_FROM_BRANCH, self.recorder.of("spec-writer")[0].prompt)
        build_md = (Path(job["job_dir"]) / "build.md").read_text()
        self.assertTrue(build_md.startswith("STATUS: READY\n"))
        self.assertIn("Built interactively in chat c-0123456789", build_md)
        self.assertTrue((self.repo / "chat.txt").is_file())
        self.assertEqual(job["from_branch"], "chat/c-0123456789")

    def test_handoff_rework_rounds_use_a_fresh_builder(self) -> None:
        factory = self.start(reviewer_code=reviews("CHANGES", "PASS"))
        job = self.run_job(factory, "Ship the chat work", from_branch=self.chat_branch())
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        (builder,) = self.recorder.of("builder")
        self.assertEqual(builder.kind, "run")
        self.assertIn("Round 1 reviews are in", builder.prompt)

    def test_unknown_branch_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "does not exist"):
            self.start().submit(REPO, "x", from_branch="chat/nope")


def core(route: dict) -> dict:
    """The route keys DESIGN section 23 names (snapshots also carry model_entry and provider_entry)."""
    return {key: route[key] for key in ("model", "driver", "provider", "model_id", "extra_body")}


class RoutingTests(FactoryTestCase):
    def routed_config(self, **sections: Any) -> Config:
        return self.config(**deep_merge({
            "models": {"big": {"provider": "openai", "model": "big-model", "max_tokens": 1000,
                               "extra_body": {"temperature": 0}}},
            "agents": {"approver": {"model": "big", "extra_body": {"output_config": {"effort": "xhigh"}}}},
        }, sections))

    def test_snapshot_resolves_every_agent_at_submit(self) -> None:
        factory = self.start(self.routed_config())
        job = self.run_job(factory)
        self.assertEqual(core(job["routing"]["approver"]), {
            "model": "big", "driver": "builtin", "provider": "openai", "model_id": "big-model",
            "extra_body": {"temperature": 0, "output_config": {"effort": "xhigh"}}})
        self.assertEqual(job["routing"]["approver"]["model_entry"], {
            "max_tokens": 1000, "stream": True, "context_tokens": None, "keep_reasoning": True,
            "allow_history_edits": True, "eager_tools": False})
        self.assertEqual(job["routing"]["approver"]["provider_entry"], {
            "type": "openai", "base_url": "https://api.openai.com/v1", "api_key_env": "OPENAI_API_KEY",
            "api_key_file": None, "timeout": 900.0, "auth": "bearer", "max_concurrency": None,
            "concurrency_scope": "call", "headers": {}})
        self.assertEqual(core(job["routing"]["reviewer-code"]), {
            "model": "sonnet", "driver": "builtin", "provider": "deepseek", "model_id": "deepseek-v4-flash",
            "extra_body": {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}})
        self.assertEqual(set(job["routing"]), {"spec-writer", "builder", "approver", "reviewer-security",
                                               "reviewer-code"})
        model = self.recorder.of("approver")[0].ctx.model
        self.assertEqual((model.key, model.model, model.provider.name, model.max_tokens),
                         ("big", "big-model", "openai", 1000))
        self.assertEqual(model.extra_body, {"temperature": 0, "output_config": {"effort": "xhigh"}})

    def test_per_run_override(self) -> None:
        factory = self.start(self.routed_config())
        job = self.run_job(factory, routing={
            "builder": {"model": "local", "extra_body": {"temperature": 0.2}},
            "approver": {"model": "opus"},
            "lead": {"model": "big"}})
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertEqual(core(job["routing"]["builder"]), {"model": "local", "driver": "builtin",
                                                           "provider": "strata", "model_id": "strata",
                                                           "extra_body": {"temperature": 0.2}})
        approver = job["routing"]["approver"]
        self.assertEqual((approver["model"], approver["model_id"]), ("opus", "deepseek-v4-pro"))
        self.assertNotIn("output_config", approver["extra_body"], "the configured extra_body belongs to 'big'")
        builder = self.recorder.of("builder")[0].ctx.model
        self.assertEqual((builder.provider.name, builder.model, builder.context_tokens), ("strata", "strata", 262144))

    def test_invalid_routing_is_refused(self) -> None:
        factory = self.start()
        for routing, message in ((5, "routing must be"), ({"builder": {"modle": "x"}}, "unknown keys"),
                                 ({"builder": {"driver": "nope"}}, "cannot route builder"),
                                 ({"builder": {"extra_body": []}}, "extra_body must be an object")):
            with self.subTest(routing=routing), self.assertRaisesRegex(ValueError, message):
                factory.submit(REPO, "x", routing=routing)
        self.assertEqual(factory.jobs(), [])

    def test_check_routing_is_submit_without_the_job(self) -> None:
        factory = self.start(self.routed_config())
        routing = {"builder": {"model": "local", "extra_body": {"temperature": 0.2}}}
        snapshot = factory.check_routing(REPO, routing)
        self.assertEqual(factory.jobs(), [], "nothing is created")
        self.assertEqual(set(snapshot), {"spec-writer", "builder", "reviewer-security", "reviewer-code", "approver"})
        self.assertEqual((snapshot["builder"]["model"], snapshot["builder"]["provider"]), ("local", "strata"))
        self.assertEqual(factory.submit(REPO, "x", routing=routing)["routing"], snapshot)
        self.assertEqual(factory.check_routing(REPO), factory.check_routing(REPO, None))
        for bad, message in ((5, "routing must be"), ({"builder": {"modle": "x"}}, "unknown keys modle"),
                             ({"builder": {"driver": "nope"}}, "cannot route builder: unknown driver"),
                             ("nope", "unknown preset 'nope'")):
            with self.subTest(routing=bad):
                with self.assertRaisesRegex(ValueError, message):
                    factory.check_routing(REPO, bad)
                with self.assertRaisesRegex(ValueError, message):
                    factory.submit(REPO, "x", routing=bad)
        with self.assertRaises(KeyError):
            factory.check_routing("unknown-repo")
        self.assertEqual(len(factory.jobs()), 1)

    def test_cli_driver_steps_show_the_model_the_cli_gets(self) -> None:
        """reviewer-code on the claude driver with drivers.claude.models {"sonnet": "haiku"}: claude is
        launched with --model haiku on its own login, so the snapshot records that (cli_model) and
        agent.start reports haiku on the claude driver, not the tier's deepseek-v4-flash on deepseek."""
        from swfactory.drivers.cli import CliRunner

        recorder = self.recorder

        class Labelled(ScriptedRunner):
            def __init__(self, config: Config, driver: str) -> None:
                super().__init__(recorder, driver)
                cfg = config.driver(driver)
                self.cli = CliRunner(config, cfg) if cfg.type == "cli" else None

            def launch_model(self, ctx: RunContext) -> tuple[str, str]:
                return self.cli.launch_model(ctx) if self.cli else super().launch_model(ctx)

        config = self.config(drivers={"claude": {"models": {"sonnet": "haiku"}}},
                             agents={"reviewer-code": {"driver": "claude"}, "approver": {"driver": "qwen"}})
        factory = Factory(config, self.bus, runner_factory=Labelled)
        factory.start()
        self.addCleanup(factory.stop)
        snapshot = factory.check_routing(REPO)
        route = snapshot["reviewer-code"]
        self.assertEqual((route["model"], route["model_id"], route["provider"], route["cli_model"]),
                         ("sonnet", "deepseek-v4-flash", "deepseek", "haiku"))
        self.assertEqual(snapshot["approver"]["cli_model"], "deepseek-v4-pro")   # qwen: the resolved model id
        self.assertNotIn("cli_model", snapshot["builder"])
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertEqual(job["routing"]["reviewer-code"]["cli_model"], "haiku")
        starts = {e.agent: e.data for e in self.events(job, "agent.start")}
        self.assertEqual({k: starts["reviewer-code"][k] for k in ("driver", "model", "model_id", "provider")},
                         {"driver": "claude", "model": "sonnet", "model_id": "haiku", "provider": "claude"})
        self.assertEqual((starts["approver"]["model_id"], starts["approver"]["provider"]),
                         ("deepseek-v4-pro", "qwen"))
        self.assertEqual((starts["builder"]["model_id"], starts["builder"]["provider"]),
                         ("deepseek-v4-pro", "deepseek"))

    def test_preset_name(self) -> None:
        presets = self.tmp / "presets"
        presets.mkdir()
        (presets / "fast.json").write_text(json.dumps({
            "agents": {"reviewer-code": {"model": "haiku", "driver": "claude"}}}))
        (presets / "foreign.json").write_text(json.dumps({
            "models": {"elsewhere": {"provider": "deepseek", "model": "x-1", "max_tokens": 100}},
            "agents": {"builder": {"model": "elsewhere"}}}))
        with mock.patch("swfactory.settings.PRESETS_DIR", presets):
            factory = self.start()
            job = self.run_job(factory, routing="fast")
            self.assertEqual(job["stage"], "merged", job.get("reason"))
            self.assertEqual(job["routing"]["reviewer-code"]["model"], "haiku")
            self.assertEqual(job["routing"]["reviewer-code"]["driver"], "claude")
            self.assertEqual(job["routing"]["builder"]["model"], "opus")
            self.assertEqual(self.recorder.of("reviewer-code")[0].driver, "claude")
            with self.assertRaisesRegex(ValueError, "unknown preset"):
                factory.submit(REPO, "x", routing="nope")
            foreign = self.run_job(factory, "Foreign feature", routing="foreign")
            self.assertEqual(foreign["stage"], "merged", "a model only the preset defines runs from the snapshot")
            builder = self.recorder.of("builder", foreign["id"])[0].ctx.model
            self.assertEqual((builder.key, builder.model, builder.max_tokens), ("elsewhere", "x-1", 100))
            self.recorder.actions["approver"] = lambda ctx, call: None
            stuck = self.run_job(factory, "Stuck feature")
            self.assertEqual(stuck["routing"]["reviewer-code"]["driver"], "builtin")
            job = factory.set_routing(REPO, stuck["id"], "fast")
            self.assertEqual((job["routing"]["reviewer-code"]["model"], job["routing"]["reviewer-code"]["driver"]),
                             ("haiku", "claude"))

    def test_config_changes_mid_run_do_not_affect_the_job(self) -> None:
        holder: dict[str, Factory] = {}
        changed = self.config(models={"sonnet": {"model": "changed-flash", "max_tokens": 111, "context_tokens": 5000,
                                                 "keep_reasoning": False},
                                      "opus": {"max_tokens": 222}},
                              providers={"deepseek": {"base_url": "https://elsewhere.example/v1", "timeout": 5}},
                              agents={"reviewer-code": {"model": "opus", "driver": "claude"}})

        def reload_then_build(ctx: RunContext, call: Call) -> None:
            holder["factory"].reload_config(changed)
            build(ctx, call)
        factory = holder["factory"] = self.start(builder=reload_then_build)
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        code = self.recorder.of("reviewer-code")[0]
        self.assertEqual((code.ctx.model.key, code.ctx.model.model, code.driver), ("sonnet", "deepseek-v4-flash",
                                                                                   "builtin"))
        model = code.ctx.model
        self.assertEqual((model.max_tokens, model.context_tokens, model.keep_reasoning), (32768, None, True),
                         "the model entry's settings come from the snapshot too")
        self.assertEqual((model.provider.base_url, model.provider.timeout), ("https://api.deepseek.com", 900.0))
        self.assertEqual(self.recorder.of("approver")[0].ctx.model.max_tokens, 32768)
        self.assertEqual(job["routing"]["reviewer-code"]["model_id"], "deepseek-v4-flash")
        self.recorder.actions["builder"] = build
        later = self.run_job(factory, "Later feature")
        self.assertEqual(later["routing"]["reviewer-code"]["model"], "opus")
        self.assertEqual(self.recorder.of("reviewer-code", later["id"])[0].driver, "claude")
        self.assertEqual(self.recorder.of("reviewer-code", later["id"])[0].ctx.model.max_tokens, 222)

    def test_a_vanished_model_still_runs_from_the_snapshot(self) -> None:
        holder: dict[str, Factory] = {}

        def drop_big(ctx: RunContext, call: Call) -> None:
            holder["factory"].reload_config(self.config())
            build(ctx, call)
        factory = holder["factory"] = self.start(self.routed_config(), builder=drop_big)
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        model = self.recorder.of("approver")[0].ctx.model
        self.assertEqual((model.key, model.model, model.provider.name, model.max_tokens),
                         ("big", "big-model", "openai", 1000))

    def test_a_vanished_provider_is_not_replaced(self) -> None:
        holder: dict[str, Factory] = {}

        def drop_openai(ctx: RunContext, call: Call) -> None:
            holder["factory"].reload_config(self.routed_config(providers={"openai": None}))
            build(ctx, call)
        factory = holder["factory"] = self.start(self.routed_config(), builder=drop_openai)
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        provider = self.recorder.of("approver")[0].ctx.model.provider
        self.assertEqual((provider.name, provider.type, provider.base_url, provider.api_key_env),
                         ("openai", "openai", "https://api.openai.com/v1", "OPENAI_API_KEY"))

    def test_a_route_from_an_older_job_uses_the_live_entry_and_fails_when_it_is_gone(self) -> None:
        holder: dict[str, Factory] = {}

        def strip_snapshot_and_drop_big(ctx: RunContext, call: Call) -> None:
            factory = holder["factory"]
            for route in factory._entry((REPO, ctx.job["id"]))["routing"].values():
                route.pop("model_entry")
                route.pop("provider_entry")
            factory.reload_config(self.config())
            build(ctx, call)
        factory = holder["factory"] = self.start(self.routed_config(), builder=strip_snapshot_and_drop_big)
        job = self.run_job(factory)
        self.assertEqual((job["stage"], job["failed_step"]), ("failed", "approve"))
        self.assertIn("model 'big' is no longer in the config", job["reason"])
        self.assertEqual(self.recorder.of("approver"), [])
        self.assertEqual(self.recorder.of("reviewer-code")[0].ctx.model.key, "sonnet", "older routes still run")
        self.recorder.actions["builder"] = build
        job = factory.set_routing(REPO, job["id"], {"approver": {"model": "opus"}})
        self.assertEqual(job["routing"]["approver"]["model"], "opus")
        self.assertIn("model_entry", job["routing"]["approver"])
        self.assertEqual(job["routing"]["builder"]["model"], "opus", "agents the dict does not name are kept")
        self.assertNotIn("model_entry", job["routing"]["builder"])
        factory.retry(REPO, job["id"])
        self.assertEqual(factory.wait(REPO, job["id"], timeout=TIMEOUT)["stage"], "merged")

    def test_hybrid_preset_runs_on_a_default_config(self) -> None:
        factory = self.start()
        job = self.run_job(factory, routing="hybrid")
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertEqual({name: route["model"] for name, route in job["routing"].items()},
                         {"spec-writer": "local", "builder": "local", "reviewer-security": "local",
                          "reviewer-code": "claude-sonnet", "approver": "claude-opus"})
        builder = self.recorder.of("builder")[0].ctx.model
        self.assertEqual((builder.model, builder.max_tokens, builder.context_tokens, builder.keep_reasoning),
                         ("qwen3.8-flash-next", 16384, 262144, False))
        tabby = builder.provider
        self.assertEqual((tabby.name, tabby.base_url, tabby.auth, tabby.max_concurrency, tabby.concurrency_scope,
                          tabby.api_key_env, tabby.timeout),
                         ("tabby", "http://127.0.0.1:5000/v1", "both", 1, "run", "TABBY_API_KEY", 1800.0))
        approver = self.recorder.of("approver")[0].ctx.model
        self.assertEqual((approver.model, approver.max_tokens, approver.context_tokens, approver.allow_history_edits,
                          approver.eager_tools), ("claude-opus-5-5", 64000, 1000000, False, True))
        self.assertEqual(approver.provider.headers, {"anthropic-beta": "server-side-fallback-2026-07-01"})
        self.assertEqual(approver.extra_body["output_config"], {"effort": "high"})
        for name in ("mixed", "gpt-oss"):
            with self.subTest(preset=name):
                queued = factory.submit(REPO, f"With {name}", routing=name)
                self.assertEqual(queued["stage"], "queued")
                factory.cancel(REPO, queued["id"])

    def test_secret_headers_stay_out_of_job_json(self) -> None:
        secret = "sekret-0123456789"
        headers = {"Authorization": f"Bearer {secret}", "X-Gateway-Token": secret, "anthropic-beta": "beta-1"}
        holder: dict[str, Factory] = {}

        def repoint(ctx: RunContext, call: Call) -> None:
            holder["factory"].reload_config(self.routed_config(
                providers={"openai": {"headers": headers, "base_url": "https://moved.example/v1"}}))
            build(ctx, call)
        factory = holder["factory"] = self.start(self.routed_config(providers={"openai": {"headers": headers}}),
                                                 builder=repoint)
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertEqual(job["routing"]["approver"]["provider_entry"]["headers"], {"anthropic-beta": "beta-1"})
        self.assertNotIn(secret, (Path(job["job_dir"]) / "job.json").read_text())
        approver = self.recorder.of("approver")[0].ctx.model.provider
        self.assertEqual(approver.headers, {"anthropic-beta": "beta-1"},
                         "the live secret headers belong to the provider's new base_url")
        self.recorder.actions["builder"] = build
        later = self.run_job(factory, "Later feature")
        call = self.recorder.of("approver", later["id"])[0]
        self.assertEqual(call.ctx.model.provider.headers, headers, "same base_url: secrets are read live")
        self.assertIn(secret, call.ctx.secrets)

    def test_rework_reuses_the_snapshot(self) -> None:
        factory = self.start(self.routed_config(), approver=lambda ctx, call: None)
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "needs-human")
        factory.reload_config(self.config(agents={"builder": {"model": "sonnet"}}))
        self.recorder.actions["approver"] = approve
        factory.rework(REPO, job["id"], "Try again.")
        job = factory.wait(REPO, job["id"], timeout=TIMEOUT)
        self.assertEqual(job["stage"], "merged", "approver's model 'big' left the config; the snapshot kept it")
        rework = self.recorder.of("builder")[1]
        self.assertEqual((rework.kind, rework.ctx.model.key), ("resume", "opus"))
        self.assertEqual(self.recorder.of("approver")[1].ctx.model.model, "big-model")

    def test_a_changed_driver_starts_a_fresh_session(self) -> None:
        factory = self.start(approver=lambda ctx, call: None)
        job = self.run_job(factory)
        factory.set_routing(REPO, job["id"], {"builder": {"driver": "claude"}})
        factory.rework(REPO, job["id"], "Again.")
        factory.wait(REPO, job["id"], timeout=TIMEOUT)
        rework = self.recorder.of("builder")[1]
        self.assertEqual((rework.kind, rework.driver), ("run", "claude"))
        self.assertIn("Rework: read", rework.prompt)


class ParallelTests(FactoryTestCase):
    def test_two_jobs_build_at_once(self) -> None:
        barrier = threading.Barrier(2, timeout=TIMEOUT / 2)   # breaks (job BLOCKED, reason shown) before wait times out

        def together(ctx: RunContext, call: Call) -> None:
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                write_build(ctx, "BLOCKED", "\n## Blocked\n\nnot parallel\n")
                return
            build(ctx, call)
        factory = self.start(self.config(pipeline={"max_parallel_jobs": 2}), builder=together)
        jobs = [factory.submit(REPO, f"Feature {n}") for n in (1, 2)]
        done = [factory.wait(REPO, job["id"], timeout=TIMEOUT) for job in jobs]
        self.assertEqual([job["stage"] for job in done], ["merged", "merged"], [job.get("reason") for job in done])
        self.assertEqual(len(run_git(self.repo, "rev-list", "--merges", "main").split()), 2)

    def test_reviewers_of_a_round_run_in_parallel(self) -> None:
        barrier = threading.Barrier(2, timeout=TIMEOUT)

        def together(ctx: RunContext, call: Call) -> None:
            barrier.wait()
            review_pass(ctx, call)
        job = self.run_job(self.start(reviewer=together))
        self.assertEqual(job["stage"], "merged", job.get("reason"))


class RepoLevelTests(FactoryTestCase):
    def test_backlog_and_next(self) -> None:
        factory = self.start()
        factory.add_backlog(REPO, "First item")
        items = factory.add_backlog(REPO, "Second item")
        self.assertEqual([i["text"] for i in items], ["First item", "Second item"])
        job = factory.next(REPO)
        self.assertEqual(job["feature"], "First item")
        self.assertEqual(factory.backlog(REPO)[0], {"text": "First item", "done": True, "job": job["id"]})
        factory.wait(REPO, job["id"], timeout=TIMEOUT)
        factory.wait(REPO, factory.next(REPO)["id"], timeout=TIMEOUT)
        with self.assertRaises(ActionError):
            factory.next(REPO)
        self.assertTrue(any(e.type == "backlog.updated" for e in self.bus.since(0)))
        with self.assertRaises(KeyError):
            factory.backlog("unknown")

    def test_next_never_strands_an_item_as_claimed(self) -> None:
        factory = self.start(self.config(pipeline={"reviewers": ["code", "perf"]}))
        factory.add_backlog(REPO, "First item")
        unclaimed = [{"text": "First item", "done": False, "job": None}]
        with self.assertRaisesRegex(ValueError, "reviewer-perf"):
            factory.next(REPO)
        self.assertEqual(factory.backlog(REPO), unclaimed, "refused before the item was claimed")
        factory.reload_config(self.config())
        with mock.patch.object(factory.store, "new_job", side_effect=OSError("disk full")), \
                self.assertRaisesRegex(OSError, "disk full"):
            factory.next(REPO)
        self.assertEqual(factory.backlog(REPO), unclaimed, "put back after the job could not be created")
        job = factory.next(REPO)
        self.assertEqual(factory.backlog(REPO), [{"text": "First item", "done": True, "job": job["id"]}])
        self.assertEqual(factory.wait(REPO, job["id"], timeout=TIMEOUT)["stage"], "merged")

    def test_next_unclaims_through_the_store_and_logs_when_it_cannot(self) -> None:
        factory = self.start()
        factory.add_backlog(REPO, "First item")
        with mock.patch.object(factory.store, "new_job", side_effect=OSError("disk full")), \
                mock.patch.object(factory.store, "unclaim", wraps=factory.store.unclaim) as unclaim, \
                self.assertRaises(OSError):
            factory.next(REPO)
        unclaim.assert_called_once_with(REPO, "First item")
        with mock.patch.object(factory.store, "new_job", side_effect=OSError("disk full")), \
                mock.patch.object(factory.store, "unclaim", side_effect=OSError("read-only")), \
                self.assertRaises(OSError):
            factory.next(REPO)
        warnings = [e.data["message"] for e in self.bus.since(0) if e.type == "log" and e.data["level"] == "warn"]
        self.assertEqual(warnings, ["backlog item 'First item' of app stays claimed: read-only"])
        self.assertEqual(factory.backlog(REPO), [{"text": "First item", "done": True, "job": "claimed"}])

    def test_next_logs_when_the_claimed_line_is_gone(self) -> None:
        factory = self.start()
        factory.add_backlog(REPO, "First item")
        real_claim = factory.store.claim_next

        def claim_then_edit(repo: str) -> str | None:   # a human rewrites backlog.md right after the claim
            text = real_claim(repo)
            path = factory.store._backlog_path(repo)
            path.write_text(path.read_text().replace("First item", "First item, reworded"))
            return text

        with mock.patch.object(factory.store, "claim_next", side_effect=claim_then_edit), \
                mock.patch.object(factory.store, "new_job", side_effect=OSError("disk full")), \
                self.assertRaises(OSError):
            factory.next(REPO)
        warnings = [e.data["message"] for e in self.bus.since(0) if e.type == "log" and e.data["level"] == "warn"]
        self.assertEqual(warnings, ["backlog item 'First item' of app was not put back: its '→ claimed' line is "
                                    "no longer in backlog.md"])

    def test_agent_overrides_saved_from_the_gui_reach_the_jobs(self) -> None:
        etc = self.tmp / "etc"   # the config file lives elsewhere ($FACTORY_CONFIG)
        etc.mkdir()
        config = Config(etc / "config.json", {"data_dir": str(self.tmp / "data"),
                                              "repos": [{"name": REPO, "path": str(self.repo)}],
                                              "pipeline": {"reviewers": list(self.reviewers), "max_parallel_jobs": 1}})
        factory = self.start(config)
        text = factory.agent_detail("reviewer-code")["text"].rstrip("\n") + "\n\nOverride from the GUI.\n"
        saved = factory.save_agent("reviewer-code", text)
        self.assertEqual(saved["source"], "user")
        agent = factory.agents(REPO)["reviewer-code"]
        self.assertEqual(str(agent.path), saved["path"])
        self.assertIn("Override from the GUI.", agent.body)
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertIn("Override from the GUI.", self.recorder.of("reviewer-code")[0].ctx.agent.body)
        self.assertEqual(factory.reset_agent("reviewer-code")["source"], "builtin")
        self.assertNotIn("Override from the GUI.", factory.agents(REPO)["reviewer-code"].body)
        perf = factory.new_agent("reviewer-perf")
        self.assertIn("reviewer-perf", factory.agents(REPO))
        self.assertIsNone(factory.reset_agent("reviewer-perf"), "it existed only as the override")
        self.assertFalse(Path(perf["path"]).exists())
        with self.assertRaises(KeyError):
            factory.reset_agent("reviewer-perf")

    def test_scout_writes_the_draft(self) -> None:
        release = threading.Event()

        def slow_scout(ctx: RunContext, call: Call) -> None:
            release.wait(TIMEOUT)
            scout(ctx, call)
        factory = self.start(repo_scout=slow_scout)
        self.addCleanup(release.set)
        self.assertEqual(factory.scout_status(REPO), {"draft": None, "running": False})
        factory.scout(REPO)
        self.assertTrue(factory.scout_status(REPO)["running"])
        with self.assertRaises(ActionError):
            factory.scout(REPO)
        release.set()
        status = until(lambda: (s := factory.scout_status(REPO)) and not s["running"] and s)
        self.assertIn("A test repo.", status["draft"])
        (call,) = self.recorder.of("repo-scout")
        draft = self.tmp / "data" / "jobs" / REPO / "AGENTS.draft.md"
        self.assertEqual(call.ctx.cwd, self.repo)
        self.assertEqual(call.ctx.write_paths, [draft])
        self.assertIn(str(self.repo), call.prompt)
        self.assertIn(str(draft), call.prompt)
        events = [e for e in self.bus.since(0) if e.agent == "repo-scout"]
        self.assertEqual(events[0].type, "agent.start")
        self.assertTrue(all(e.job is None and e.data["run"] == "01-repo-scout" for e in events))

    def test_a_failing_scout_logs_the_error(self) -> None:
        factory = self.start(repo_scout=failing("model offline"))
        factory.scout(REPO)
        until(lambda: not factory.scout_status(REPO)["running"])
        errors = [e.data["message"] for e in self.bus.since(0) if e.type == "log" and e.data["level"] == "error"]
        self.assertEqual(errors, ["repo-scout on app: repo-scout failed: model offline"])
        self.assertIsNone(factory.scout_status(REPO)["draft"])

    def test_reload_config_applies_to_new_jobs(self) -> None:
        factory = self.start()
        factory.reload_config(self.config(pipeline={"reviewers": ["code"], "max_resumes": 0}))
        job = self.run_job(factory)
        self.assertEqual((job["reviewers"], job["max_resumes"]), (["code"], 0))
        self.assertEqual(job["stage"], "merged")

    def test_agents_include_repo_overrides(self) -> None:
        factory = self.start()
        self.assertIn("builder", factory.agents())
        agents = self.repo / ".factory" / "agents"
        agents.mkdir(parents=True)
        (agents / "builder.md").write_text("---\nname: builder\ndescription: Repo builder.\n---\nBuild.\n")
        self.assertEqual(factory.agents(REPO)["builder"].description, "Repo builder.")


class DelegationTests(FactoryTestCase):
    def test_chats_and_plans_are_delegated(self) -> None:
        factory = self.start()
        chats, plans = factory._chats, factory._plans
        self.assertTrue(chats.started and plans.started)
        self.assertIs(chats.factory, factory)
        factory.new_chat(REPO, "local")
        factory.chat_send("c-0123456789", "hi")
        factory.chat_handoff("c-0123456789", "Ship it")
        factory.create_plan(REPO, "Big change", "hybrid")
        factory.run_plan(REPO, "P001-big")
        factory.cancel_plan(REPO, "P001-big")
        factory.retry_plan(REPO, "P001-big")
        factory.update_plan_text(REPO, "P001-big", "PLAN: READY")
        self.assertEqual(chats.calls, [("new_chat", (REPO, "local", "worktree", "coder")),
                                       ("chat_send", ("c-0123456789", "hi")),
                                       ("chat_handoff", ("c-0123456789", "Ship it", None))])
        self.assertEqual([name for name, _ in plans.calls], ["create", "run", "cancel", "retry", "update_plan_text"])
        new_config = self.config()
        factory.reload_config(new_config)
        self.assertIs(plans.config, new_config)
        factory.stop()
        self.assertTrue(chats.stopped and plans.stopped)

    def test_doctor_and_settings_are_delegated(self) -> None:
        factory = self.start()
        seen = {}

        def run_checks(config: Config, factory: Any = None) -> list[dict]:
            seen.update(config=config, factory=factory)
            return [{"name": "x", "ok": True, "detail": "", "level": "info"}]
        swap_module(self, fake_module("swfactory.doctor", run_checks=run_checks))
        self.assertEqual(factory.doctor()[0]["name"], "x")
        self.assertIs(seen["factory"], factory)
        new_config = self.config(pipeline={"max_resumes": 5})

        def saved(config: Config, patch: dict, on_saved: Any = None) -> Config:
            on_saved(new_config)   # settings swaps the config in from here, under its save lock
            return new_config

        with mock.patch("swfactory.settings.update_config", side_effect=saved) as update, \
                mock.patch("swfactory.settings.config_view", return_value={"view": True}):
            self.assertEqual(factory.update_config({"pipeline": {"max_resumes": 5}}), {"view": True})
        update.assert_called_once()
        self.assertEqual(update.call_args.kwargs, {"on_saved": factory.reload_config})
        self.assertIs(factory.config, new_config)
        with mock.patch("swfactory.settings.reset_agent", return_value=None):
            self.assertIsNone(factory.reset_agent("reviewer-perf"), "nothing of that name remains")
        with mock.patch("swfactory.settings.reset_agent", side_effect=KeyError("no user override")), \
                self.assertRaises(KeyError):
            factory.reset_agent("reviewer-perf")

    def test_agent_changes_publish_agent_updated(self) -> None:
        factory = self.start()
        detail = {"name": "reviewer-perf", "source": "user"}
        with mock.patch("swfactory.settings.new_agent", return_value=detail):
            self.assertEqual(factory.new_agent("reviewer-perf"), detail)
        with mock.patch("swfactory.settings.save_agent", return_value=dict(detail, body="new")):
            factory.save_agent("reviewer-perf", "---\nname: reviewer-perf\n---\nx")
        with mock.patch("swfactory.settings.reset_agent", return_value=None):
            factory.reset_agent("reviewer-perf")
        with mock.patch("swfactory.settings.reset_agent", side_effect=KeyError("no user override")), \
                self.assertRaises(KeyError):
            factory.reset_agent("reviewer-perf")
        events = [e for e in self.bus.since(0) if e.type == "agent.updated"]
        self.assertEqual([(e.agent, e.job, e.data) for e in events], [
            ("reviewer-perf", None, {"name": "reviewer-perf", "agent": detail}),
            ("reviewer-perf", None, {"name": "reviewer-perf", "agent": dict(detail, body="new")}),
            ("reviewer-perf", None, {"name": "reviewer-perf", "agent": None})])

    def test_setup_secrets_and_docs_are_delegated(self) -> None:
        factory = self.start()
        status = factory.setup_status()
        self.assertEqual(set(status["steps"]), {"providers", "models", "routing", "repos", "tools", "output"})
        self.assertTrue(status["steps"]["repos"]["ok"], status["steps"]["repos"])
        self.assertEqual(factory.secret_status()["env_file"], str(self.tmp / "env"))
        factory.secret_values()   # cached now
        with mock.patch.dict(os.environ, {}):
            entry = factory.set_secret("DEEPSEEK_API_KEY", "sk-delegated-0123456789")
            self.assertEqual(entry["name"], "DEEPSEEK_API_KEY")
            self.assertTrue(entry["set"])
            self.assertIn("sk-delegated-0123456789", factory.secret_values(), "the redaction list is rebuilt")
        data = factory.procedures()
        self.assertEqual(data["agents"][0]["name"], "spec-writer")
        self.assertIn("<svg", factory.procedures_html())


class ConfigUpdateTests(FactoryTestCase):
    """update_config / apply_preset swap the saved config in under settings' save lock."""

    def test_concurrent_updates_end_with_the_config_on_disk(self) -> None:
        config = self.config()
        config.save()
        factory = self.start(config)
        swap = factory.reload_config
        order = iter(range(9, 0, -1))
        order_lock = threading.Lock()

        def slow_swap(new: Config) -> None:
            # Earlier saves swap more slowly: a swap outside the save lock would land after a newer one.
            with order_lock:
                delay = next(order, 0) * 0.01
            time.sleep(delay)
            swap(new)

        factory.reload_config = slow_swap  # type: ignore[method-assign]
        errors: list[BaseException] = []

        def run(call: Callable[[], Any]) -> None:
            try:
                call()
            except BaseException as exc:  # reported below
                errors.append(exc)

        calls = [lambda n=n: factory.update_config({"limits": {"max_turns": 100 + n},
                                                    "agents": {f"extra-{n}": {"model": "sonnet"}}})
                 for n in range(8)]
        calls.append(lambda: factory.apply_preset("strata"))
        threads = [threading.Thread(target=run, args=(call,)) for call in calls]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(errors, [])
        on_disk = Config.load(config.path)
        self.assertEqual(factory.config.raw, on_disk.raw, "the config installed last is the one saved last")
        self.assertEqual({f"extra-{n}" for n in range(8)} - set(on_disk.raw["agents"]), set(), "every update landed")
        self.assertEqual(factory.config.model("local").provider.name, "strata")

    def test_an_invalid_update_swaps_nothing(self) -> None:
        config = self.config()
        config.save()
        factory = self.start(config)
        before = factory.config
        with self.assertRaises(ValueError):
            factory.update_config({"limits": {"max_turns": 0}})
        self.assertIs(factory.config, before)


VAULT_KEY = "vault-secret-value-0123456789"


class SecretRedactionTests(FactoryTestCase):
    """The bus redacts keys that only a per-run preset's provider knows (Factory.secret_values)."""

    def setUp(self) -> None:
        super().setUp()
        key_file = self.tmp / "vault.env"
        key_file.write_text(f"VAULT_KEY={VAULT_KEY}\n")
        os.environ.pop("VAULT_KEY", None)   # the key comes from the file; its name is no *_API_KEY
        presets = self.tmp / "presets"
        presets.mkdir()
        (presets / "vaulted.json").write_text(json.dumps({
            "providers": {"vault": {"type": "openai", "base_url": "http://127.0.0.1:9/v1", "api_key_env": "VAULT_KEY",
                                    "api_key_file": str(key_file)}},
            "models": {"vaulted": {"provider": "vault", "model": "v-1", "max_tokens": 100}},
            "agents": {"builder": {"model": "vaulted"}}}))
        patcher = mock.patch("swfactory.settings.PRESETS_DIR", presets)
        patcher.start()
        self.addCleanup(patcher.stop)

    def new(self, **actions: Action) -> tuple[Factory, EventBus]:
        self.recorder.actions.update(actions)
        factory, bus = new_factory(self.config(), runner_factory=self.recorder.runner_factory)
        factory.start()
        self.addCleanup(factory.stop)
        return factory, bus

    def test_a_key_only_a_per_run_preset_knows_is_redacted(self) -> None:
        def leaky_builder(ctx: RunContext, call: Call) -> None:
            ctx.emit("agent.text", {"text": f"the key is {VAULT_KEY}"})
            build(ctx, call)
        factory, bus = self.new(builder=leaky_builder)
        self.assertNotIn(VAULT_KEY, factory.config.secret_values(), "the live config has no such provider")
        self.assertEqual(bus.publish("log", {"message": VAULT_KEY}).data["message"], VAULT_KEY)
        job = factory.submit(REPO, "Add a greeting", routing="vaulted")
        self.assertEqual(job["routing"]["builder"]["provider"], "vault")
        self.assertEqual(bus.publish("log", {"message": f"leak {VAULT_KEY}"}).data["message"], "leak ***",
                         "redacted as soon as the job exists")
        job = factory.wait(REPO, job["id"], timeout=TIMEOUT)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        texts = [e.data["text"] for e in bus.since(0) if e.type == "agent.text" and e.agent == "builder"]
        self.assertIn("the key is ***", texts)
        log = next((Path(job["job_dir"]) / "logs").glob("*-builder.jsonl")).read_text()
        self.assertNotIn(VAULT_KEY, log)
        self.assertIn("the key is ***", log)

    def test_set_routing_to_the_preset_redacts_its_key(self) -> None:
        factory, bus = self.new(approver=lambda ctx, call: None)
        stuck = factory.wait(REPO, factory.submit(REPO, "Stuck")["id"], timeout=TIMEOUT)
        self.assertEqual(stuck["stage"], "needs-human")
        self.assertNotIn(VAULT_KEY, factory.secret_values())
        factory.set_routing(REPO, stuck["id"], "vaulted")
        self.assertIn(VAULT_KEY, factory.secret_values())
        self.assertEqual(bus.publish("log", {"message": VAULT_KEY}).data["message"], "***")

    def test_a_plan_routed_by_the_preset_redacts_its_key(self) -> None:
        factory, bus = self.new()
        plans = [{"id": "P001-x", "repo": REPO, "stage": "planning", "routing": "vaulted"},
                 {"id": "P002-y", "repo": REPO, "stage": "done", "routing": "missing-preset"}]
        factory._plans = types.SimpleNamespace(plans=lambda: plans, config=factory.config, stop=lambda timeout: None)
        factory._forget_secrets()
        self.assertIn(VAULT_KEY, factory.secret_values())
        plans[0]["stage"] = "done"
        factory._forget_secrets()
        self.assertNotIn(VAULT_KEY, factory.secret_values(), "a finished plan runs no lead")

    def test_the_answer_is_cached_until_something_changes(self) -> None:
        # Not started: no worker drives the submitted job, so only this thread asks for secrets.
        factory, _ = new_factory(self.config(), runner_factory=self.recorder.runner_factory)
        self.addCleanup(factory.stop)
        with mock.patch.object(Config, "secret_values", autospec=True, return_value=["config-secret-0001"]) as live:
            self.assertEqual(factory.secret_values(), ["config-secret-0001"])
            self.assertEqual(factory.secret_values(), ["config-secret-0001"])
            self.assertEqual(live.call_count, 1)
            factory.reload_config(self.config())
            factory.secret_values()
            self.assertEqual(live.call_count, 2)
            factory.submit(REPO, "Add a greeting")
            factory.secret_values()
            self.assertEqual(live.call_count, 3)
            with mock.patch("swfactory.orchestrator.SECRETS_TTL", 0.0):
                factory.secret_values()
            self.assertEqual(live.call_count, 4)

    def test_the_bus_falls_back_to_the_config_for_a_factory_without_secret_values(self) -> None:
        class Bare:
            def __init__(self, config: Config, bus: EventBus) -> None:
                self.config = config

        config = self.config(server={"token": "server-token-0123456789"})
        with mock.patch("swfactory.orchestrator.Factory", Bare):
            factory, bus = new_factory(config)
        self.assertIsInstance(factory, Bare)
        self.assertEqual(bus.publish("log", {"message": "server-token-0123456789"}).data["message"], "***")


# -- the Housekeeping Tracker (DESIGN §32) ------------------------------------------------------


CRITERIA_SPEC = ("# Spec\n\nAdd a greeting.\n\n## Acceptance criteria\n1. greet() says hello.\n"
                 "2. A test covers greet().\n")


def spec_with_criteria(ctx: RunContext, call: Call) -> None:
    (ctx.job_dir / "spec.md").write_text(CRITERIA_SPEC)


def silent_build(ctx: RunContext, call: Call) -> None:
    """A builder that commits and says READY but answers no item."""
    commit_file(ctx.cwd, f"feature-{call.index}.txt", f"work {call.index}\n", f"work {call.index}")
    write_build(ctx, "READY")


def plain_approve(ctx: RunContext, call: Call) -> None:
    (ctx.job_dir / "decision.md").write_text("APPROVE\n\n## Why\nAll good.\n")


def verifying_reviewer(*verdicts: str) -> Action:
    """A reviewer that writes verdicts[round - 1]: a CHANGES review lists one finding; later rounds verify the
    items its prompt says wait for its check."""
    def act(ctx: RunContext, call: Call) -> None:
        verdict = verdicts[min(review_round(ctx), len(verdicts)) - 1]
        if verdict == "CHANGES":
            ctx.write_paths[0].write_text("VERDICT: CHANGES\n\n## Findings\n1. greet() returns hi, the spec says "
                                          "hello\n\n## Notes\nNone.\n")
            return
        lines = "".join(f"- {item} verified: re-ran the test\n"
                        for item in prompt_ids(call.prompt, "waiting for your check"))
        ctx.write_paths[0].write_text(f"VERDICT: {verdict}\n\n## Findings\nNone.\n" +
                                      (f"\n## Items\n{lines}" if lines else ""))
    return act


class ItemsTests(FactoryTestCase):
    """DESIGN §32: the job's tracked items through the line, the gate and carry-forward."""

    def items(self, job: dict) -> dict:
        return {item["id"]: item for item in json.loads((Path(job["job_dir"]) / "items.json").read_text())["items"]}

    def followups(self) -> dict:
        path = self.tmp / "data" / "jobs" / REPO / "followups.json"
        return {item["id"]: item for item in json.loads(path.read_text())["items"]}

    def test_spec_criteria_become_items_the_builder_proves_and_the_approver_verifies(self) -> None:
        factory = self.start(spec_writer=spec_with_criteria)
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        items = self.items(job)
        self.assertEqual([(i["kind"], i["status"], i["owner"], i["verifier"]) for i in items.values()],
                         [("criterion", "verified", "builder", "approver")] * 2)
        commit = items["I-1"]["evidence"][0]
        self.assertEqual((commit["kind"], commit["check"]), ("commit", "ok"))
        self.assertEqual(items["I-1"]["verified_by"], "approver")
        spec, builder = self.recorder.of("spec-writer")[0], self.recorder.of("builder")[0]
        self.assertNotIn("Items:", spec.prompt, "no line while there are no items")
        job_dir = Path(job["job_dir"])
        self.assertIn(f"Items: {job_dir}/items.md (2 open; you own I-1, I-2).", builder.prompt)
        self.assertIn("You own (2): I-1 [open, blocking] greet() says hello. (spec criterion 1)", builder.prompt)
        self.assertLess(builder.prompt.index("Items:"), builder.prompt.index("Build round 1."))
        self.assertIsNotNone(builder.ctx.items, "a builtin run gets the job's items")
        self.assertIn("waiting for your check: I-1, I-2", self.recorder.of("approver")[0].prompt)
        self.assertTrue((job_dir / "items.md").is_file())
        self.assertEqual(job["items"]["verified"], 2)
        self.assertEqual(job["items"]["blocking_open"], 0)
        log = [json.loads(line) for line in (job_dir / "items.log.jsonl").read_text().splitlines()]
        self.assertEqual([(e["ref"].rsplit("/", 1)[1], e["by"], e["to"]) for e in log if e["ref"].endswith("I-1")],
                         [("I-1", "factory", "open"), ("I-1", "builder", "done"), ("I-1", "approver", "verified")])
        updated = [e for e in self.events(job, "items.updated")]
        self.assertTrue(updated and updated[-1].data["ref"] == f"job:{REPO}/{job['id']}")

    def test_an_untouched_blocking_item_resumes_the_builder_once_then_needs_a_human(self) -> None:
        factory = self.start(spec_writer=spec_with_criteria, builder=silent_build)
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "needs-human")
        self.assertEqual(job["reason"], "builder left blocking items open: I-1, I-2")
        builder = self.recorder.of("builder")
        self.assertEqual([c.kind for c in builder], ["run", "resume"])
        self.assertIn(ITEMS_FIX.format(ids="I-1, I-2"), builder[1].prompt)
        self.assertEqual(self.recorder.of("reviewer-code"), [])

    def test_changes_findings_become_builder_items_proven_by_commit_and_verified_by_the_reviewer(self) -> None:
        factory = self.start(reviewer_code=verifying_reviewer("CHANGES", "PASS"))
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        finding = self.items(job)["I-1"]
        self.assertEqual((finding["kind"], finding["owner"], finding["verifier"], finding["blocking"]),
                         ("finding", "builder", "reviewer-code", True))
        self.assertEqual((finding["status"], finding["verified_by"]), ("verified", "reviewer-code"))
        self.assertEqual(finding["evidence"][0]["check"], "ok", "the round-2 commit is on the branch")
        resumed = self.recorder.of("builder")[1]
        self.assertIn("you own I-1", resumed.prompt)
        self.assertIn("Address every review whose first line is VERDICT: CHANGES", resumed.prompt)
        self.assertIn("waiting for your check: I-1", self.recorder.of("reviewer-code")[1].prompt)

    def test_a_done_but_unverified_item_holds_the_gate_until_a_person_verifies(self) -> None:
        factory = self.start(spec_writer=spec_with_criteria, approver=plain_approve)
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "needs-human")
        self.assertEqual(job["reason"], "2 blocking items not proven: I-1 (Waiting for a check, builder), "
                                        "I-2 (Waiting for a check, builder)")
        self.assertTrue(self.branch_exists(job["branch"]), "nothing merged")
        for item in ("I-1", "I-2"):
            result = factory.item_action(f"job:{REPO}/{job['id']}/{item}", "verify", note="I ran it")
            self.assertEqual((result["item"]["status"], result["item"]["word"]), ("verified", "Proven"))
        self.assertEqual(factory.job(REPO, job["id"])["items"]["blocking_open"], 0)
        factory.approve(REPO, job["id"])
        job = factory.wait(REPO, job["id"], timeout=TIMEOUT)
        self.assertEqual(job["stage"], "merged", job.get("reason"))

    def test_a_product_decision_is_a_person_s_blocking_item(self) -> None:
        def spec(ctx: RunContext, call: Call) -> None:
            (ctx.job_dir / "spec.md").write_text("# Spec\n\n## Assumptions\n- Hello is the right word. "
                                                 "**(product)**\n- Plain text only.\n")
        factory = self.start(spec_writer=spec)
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "needs-human")
        self.assertEqual(job["reason"], "1 blocking item not proven: I-1 (To do, human)")
        decision = self.items(job)["I-1"]
        self.assertEqual((decision["kind"], decision["owner"], decision["verifier"]), ("decision", "human", "human"))
        listed = factory.items(repo=REPO, waiting=True)
        self.assertEqual([row["ref"] for row in listed["items"]], [f"job:{REPO}/{job['id']}/I-1"])
        self.assertEqual(listed["items"][0]["waiting"], "blocking")
        with self.assertRaisesRegex(ValueError, "dropping an item needs a reason"):
            factory.item_action(f"job:{REPO}/{job['id']}/I-1", "drop")
        factory.item_action(f"job:{REPO}/{job['id']}/I-1", "verify", note="product says yes")
        factory.approve(REPO, job["id"])
        self.assertEqual(factory.wait(REPO, job["id"], timeout=TIMEOUT)["stage"], "merged")

    def test_pass_review_notes_are_suggestions_carried_to_follow_ups_on_merge(self) -> None:
        def noting(ctx: RunContext, call: Call) -> None:
            ctx.write_paths[0].write_text("VERDICT: PASS\n\n## Findings\nNone.\n\n## Notes\n"
                                          "- The helper name is long.\n")
        factory = self.start(reviewer_code=noting)
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        note = self.items(job)["I-1"]
        self.assertEqual((note["kind"], note["status"], note["links"]["followup"]),
                         ("note", "deferred", f"repo:{REPO}/F-1"))
        follow = self.followups()["F-1"]
        self.assertEqual((follow["kind"], follow["status"], follow["owner"], follow["links"]["job"]),
                         ("followup", "proposed", "human", job["id"]))
        self.assertEqual(follow["source"]["ref"], f"job:{REPO}/{job['id']}/I-1")
        self.assertIsNone(follow["due"], "a suggestion carries forward undated: never overdue")
        carried = self.events(job, "items.carried")
        self.assertEqual((carried[-1].data["count"], carried[-1].data["to"], carried[-1].data["reason"]),
                         (1, "follow-ups", "job merged"))
        rows = factory.items(repo=REPO, scope="followups")["items"]
        self.assertEqual([(row["ref"], row["waiting"]) for row in rows], [(f"repo:{REPO}/F-1", "suggested")])

    def test_a_branch_delivery_is_held_the_same_way(self) -> None:
        factory = self.start(spec_writer=spec_with_criteria, approver=plain_approve)
        job = self.run_job(factory, delivery="branch")
        self.assertEqual(job["stage"], "needs-human")
        self.assertIn("2 blocking items not proven", job["reason"])
        self.assertIsNone(job.get("delivered"))

    def test_a_human_override_merge_carries_open_items_forward_and_logs_it(self) -> None:
        factory = self.start(spec_writer=spec_with_criteria, builder=silent_build)
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "needs-human")
        with self.assertRaises(ActionError) as refused:
            factory.approve(REPO, job["id"])
        self.assertEqual(refused.exception.payload, {"open_blocking": ["I-1", "I-2"]})
        self.assertIn("name every one in carry_items", str(refused.exception))
        with self.assertRaises(ActionError):
            factory.approve(REPO, job["id"], carry_items=["I-1"])
        self.assertEqual(factory.job(REPO, job["id"])["stage"], "needs-human", "nothing moved")
        factory.approve(REPO, job["id"], carry_items=["I-1", "I-2"])
        job = factory.wait(REPO, job["id"], timeout=TIMEOUT)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        items = self.items(job)
        self.assertEqual({i["status"] for i in items.values()}, {"deferred"})
        self.assertEqual(items["I-1"]["reason"], "merged by human override while open")
        follow = self.followups()
        self.assertEqual([(f["title"], f["status"]) for f in follow.values()],
                         [("greet() says hello.", "open"), ("A test covers greet().", "open")])
        messages = [e.data["message"] for e in self.events(job, "log")]
        self.assertIn("2 open items moved to follow-ups (merged by human override while open)", messages)
        self.assertIn("a person approved with 2 open must-do items to carry to follow-ups: I-1, I-2", messages)
        self.assertEqual(job["carried_items"], ["I-1", "I-2"])
        log = (Path(job["job_dir"]) / "items.log.jsonl").read_text()
        self.assertIn("merged by human override while open", log)

    def test_rejected_and_cancelled_jobs_carry_forward_too(self) -> None:
        factory = self.start(spec_writer=spec_with_criteria, builder=silent_build)
        job = self.run_job(factory)
        factory.reject(REPO, job["id"], "not now")
        self.assertEqual({i["status"] for i in self.items(job).values()}, {"deferred"})
        self.assertEqual(self.items(job)["I-1"]["reason"], "job rejected")
        release = threading.Event()
        self.addCleanup(release.set)
        self.recorder.actions["builder"] = lambda ctx, call: release.wait(TIMEOUT) and None
        second = factory.submit(REPO, "Another")
        until(lambda: factory.job(REPO, second["id"])["stage"] == "build")
        factory.cancel(REPO, second["id"])
        release.set()
        second = factory.wait(REPO, second["id"], timeout=TIMEOUT)
        self.assertEqual({i["status"] for i in self.items(second).values()}, {"deferred"})
        self.assertEqual(self.items(second)["I-1"]["reason"], "job cancelled")
        self.assertEqual(len(self.followups()), 4)

    def test_a_follow_up_made_into_a_job_is_proven_when_it_merges(self) -> None:
        factory = self.start(spec_writer=spec_with_criteria, builder=silent_build)
        job = self.run_job(factory)
        factory.reject(REPO, job["id"])
        self.recorder.actions.update({"spec-writer": write_spec, "builder": build})
        made = factory.item_action(f"repo:{REPO}/F-1", "make-job")
        self.assertEqual(made["item"]["status"], "doing")
        self.assertEqual(made["item"]["links"]["job"], made["job"]["id"])
        self.assertTrue(made["job"]["feature"].startswith(f"Follow-up F-1 from job {job['id']}: greet() says hello."))
        follow_job = factory.wait(REPO, made["job"]["id"], timeout=TIMEOUT)
        self.assertEqual(follow_job["stage"], "merged", follow_job.get("reason"))
        proven = self.followups()["F-1"]
        self.assertEqual((proven["status"], proven["evidence"][-1]["kind"], proven["evidence"][-1]["ref"]),
                         ("verified", "job", follow_job["id"]))

    def test_a_forged_reviewer_journal_is_an_error(self) -> None:
        holder: dict[str, Factory] = {}

        def forger(ctx: RunContext, call: Call) -> None:   # reviewer-code
            until(lambda: holder["factory"].job(REPO, ctx.job["id"])["reviews"]["security"] == "PASS")
            journal = ctx.job_dir / "sessions" / "reviewer-security.items.jsonl"
            entry = {"seq": 1, "by": "reviewer-security", "op": "add", "title": "planted"}
            journal.write_text(json.dumps(entry) + "\n")
            review_pass(ctx, call)
        factory = holder["factory"] = self.start(reviewer_code=forger)
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "needs-human")
        self.assertEqual(job["reason"], "write policy, review round 1: sessions/reviewer-security.items.jsonl changed "
                                        "after reviewer-security finished")
        self.assertEqual(job["reviews"]["security"], "ERROR")
        self.assertFalse((Path(job["job_dir"]) / "items.json").exists() and self.items(job),
                         "the forged journal is never applied")

    def test_an_items_json_written_by_an_agent_is_put_back_and_reported(self) -> None:
        def rewrite(ctx: RunContext, call: Call) -> None:
            approve(ctx, call)
            (ctx.job_dir / "items.json").write_text(json.dumps({"items": [], "v": 99}))
        factory = self.start(spec_writer=spec_with_criteria, approver=rewrite)
        job = self.run_job(factory)
        self.assertEqual((job["stage"], job["failed_step"]), ("failed", "approve"))
        self.assertEqual(job["reason"], "write policy: approver changed items.json in the job folder, outside its "
                                        "write paths (reverted)")
        self.assertEqual(len(self.items(job)), 2, "the ledger is the orchestrator's again")

    def test_a_persons_action_during_a_run_is_not_a_write_policy_violation(self) -> None:
        holder: dict[str, Factory] = {}

        def building(ctx: RunContext, call: Call) -> None:
            holder["factory"].add_item(f"job:{REPO}/{ctx.job['id']}", title="Remember the docs", blocking=False)
            build(ctx, call)
        factory = holder["factory"] = self.start(builder=building)
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertEqual(self.items(job)["I-1"]["title"], "Remember the docs")
        self.assertEqual(self.items(job)["I-1"]["status"], "deferred", "carried to follow-ups at the merge")

    def test_journal_and_items_lines_retry_idempotently_after_a_restart(self) -> None:
        def journaling(ctx: RunContext, call: Call) -> AgentRun | None:
            journal = ctx.job_dir / "sessions" / "builder.items.jsonl"
            if call.index == 0:
                journal.write_text(json.dumps({"seq": 1, "by": "builder", "op": "add", "id": "I-1",
                                               "title": "Check the other callers"}) + "\n")
                write_build(ctx, "READY", "\n## Items\n- new: Update the docs later\n")
                return AgentRun(ok=False, error="provider down", usage=dict(USAGE))
            commit_file(ctx.cwd, f"feature-{call.index}.txt", "work\n", "work")
            write_build(ctx, "READY", "\n## Items\n- new: Update the docs later\n- I-1 doing: looked at two\n")
            return None
        factory = self.start(builder=journaling)
        job = self.run_job(factory)
        self.assertEqual((job["stage"], job["failed_step"]), ("failed", "build"))
        self.assertEqual([i["title"] for i in self.items(job).values()], ["Check the other callers"],
                         "a failed run gives its Items calls, not its files (they are not its final word)")
        factory.stop()
        restarted = Factory(self.config(), self.bus, runner_factory=self.recorder.runner_factory)
        restarted.start()
        self.addCleanup(restarted.stop)
        restarted.retry(REPO, job["id"])
        job = restarted.wait(REPO, job["id"], timeout=TIMEOUT)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertEqual([i["title"] for i in self.items(job).values()],
                         ["Check the other callers", "Update the docs later"], "nothing was applied twice")

    def test_the_item_cap_stops_the_job_for_a_person(self) -> None:
        def flood(ctx: RunContext, call: Call) -> None:
            commit_file(ctx.cwd, "f.txt", "f\n", "f")
            lines = "".join(f"- new: thing {n}\n" for n in range(305))
            write_build(ctx, "READY", f"\n## Items\n{lines}")
        job = self.run_job(self.start(builder=flood))
        self.assertEqual(job["stage"], "needs-human")
        self.assertIn("already holds 300 open items", job["reason"])
        self.assertIn("verify, drop or move some to later", job["reason"])
        self.assertEqual(len(self.items(job)), 300)

    def test_unparsed_lines_and_refusals_reach_the_event_stream_and_the_next_prompt(self) -> None:
        def sloppy(ctx: RunContext, call: Call) -> None:
            sha = commit_file(ctx.cwd, f"f{call.index}.txt", "f\n", "f")
            if call.index == 0:
                write_build(ctx, "READY", "\n## Items\n- I-1 done: ran it\n- I-2 finished\n")
            else:
                write_build(ctx, "READY", f"\n## Items\n- I-1 done: commit {sha}\n- I-2 done: commit {sha}\n")
        factory = self.start(spec_writer=spec_with_criteria, builder=sloppy)
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        unparsed = self.events(job, "items.unparsed")
        self.assertEqual([(e.data["agent"], e.data["line"], e.data["text"]) for e in unparsed],
                         [("builder", 7, "- I-2 finished")])
        resumed = self.recorder.of("builder")[1].prompt
        self.assertIn("Your last answer left these unresolved:", resumed)
        self.assertIn("done needs evidence", resumed)
        self.assertIn("was not understood: - I-2 finished", resumed)

    def test_the_sweep_proposes_what_the_builder_left_for_later(self) -> None:
        def admits(ctx: RunContext, call: Call) -> AgentRun:
            build(ctx, call)
            with (ctx.job_dir / "build.md").open("a") as handle:
                handle.write("\n## Log\nI could not run the UI check.\n")
            return AgentRun(ok=True, final_text="Done; the docs are left for later.", usage=dict(USAGE),
                            session_ref=str(ctx.session_path), turns=1)
        job = self.run_job(self.start(builder=admits))
        proposals = [i for i in self.items(job).values() if i["source"].get("type") == "sweep"]
        self.assertEqual([(i["kind"], i["source"]["file"]) for i in proposals], [("gap", "build.md"),
                                                                                  ("commitment", "reply")])
        self.assertTrue(all(i["status"] == "deferred" for i in proposals), "carried forward at the merge")

    def test_housekeeping_off_records_items_but_never_holds_the_job(self) -> None:
        config = self.config(housekeeping={"enabled": False})
        factory = self.start(config, spec_writer=spec_with_criteria, builder=silent_build, approver=plain_approve)
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertEqual(len(self.recorder.of("builder")), 1, "no ITEMS_FIX")
        self.assertEqual({i["status"] for i in self.items(job).values()}, {"deferred"})

    def test_the_api_views(self) -> None:
        factory = self.start(spec_writer=spec_with_criteria, approver=plain_approve)
        job = self.run_job(factory)
        view = factory.scope_items(f"job:{REPO}/{job['id']}")
        self.assertEqual(sorted(view), ["items", "log", "notices", "summary", "unparsed"])
        self.assertEqual([row["ref"] for row in view["items"]], [f"job:{REPO}/{job['id']}/I-1",
                                                                f"job:{REPO}/{job['id']}/I-2"])
        self.assertEqual(view["items"][0]["scope"]["stage"], "needs-human")
        added = factory.add_item(f"job:{REPO}/{job['id']}", title="Ask about the copy", owner="human")
        self.assertEqual((added["id"], added["owner"], added["waiting"]), ("I-3", "human", "yours"))
        listing = factory.items(repo=REPO)
        self.assertEqual(listing["counts"]["blocking"], 2)
        self.assertEqual(listing["counts"]["waiting"], 3)
        self.assertEqual(listing["items"][0]["waiting"], "blocking")
        follow = factory.add_item(f"repo:{REPO}", title="Re-run the restore drill")
        self.assertEqual((follow["id"], follow["kind"], follow["owner"]), ("F-1", "followup", "human"))
        deferred = factory.item_action(f"job:{REPO}/{job['id']}/I-3", "defer", reason="next month")
        self.assertEqual((deferred["item"]["status"], deferred["item"]["links"]["followup"]),
                         ("deferred", f"repo:{REPO}/F-2"))
        with self.assertRaisesRegex(ValueError, "waiting for its proof check"):
            factory.item_action(f"job:{REPO}/{job['id']}/I-1", "defer", reason="later")
        with self.assertRaises(KeyError):
            factory.item_action(f"job:{REPO}/{job['id']}/I-9", "verify")
        with self.assertRaises(ValueError):
            factory.item_action("job:nope", "verify")
        self.assertEqual(factory.scope_items(f"repo:{REPO}")["summary"]["total"], 2)

    def test_the_system_scope(self) -> None:
        """What belongs to no repo (§32, §33): the factory opens items there, deduped; a person works them."""
        factory = self.start()
        opened = factory.system_item({"title": "Kate changed in Open WebUI", "detail": "version a to b",
                                      "source": {"type": "drift", "model": "kate"}}, dedupe="kate:b")
        self.assertEqual((opened["ref"], opened["added"], opened["item"]["owner"]), ("system:S-1", True, "human"))
        again = factory.system_item({"title": "Kate changed in Open WebUI"}, dedupe="kate:b")
        self.assertEqual((again["ref"], again["added"]), ("system:S-1", False))
        self.assertEqual(json.loads((self.tmp / "data" / "housekeeping" / "system.json").read_text())["items"][0]
                         ["source"], {"type": "drift", "model": "kate", "hash": "kate:b"})
        updated = [e for e in self.bus.since(0) if e.type == "items.updated"]
        self.assertEqual(updated[-1].data["ref"], "system")
        added = factory.add_item("system", title="Renew the TLS certificate", due="2026-12-01")
        self.assertEqual((added["ref"], added["owner"], added["waiting"]), ("system:S-2", "human", "yours"))
        listing = factory.items(scope="system")
        self.assertEqual(sorted(row["ref"] for row in listing["items"]), ["system:S-1", "system:S-2"])
        self.assertEqual(factory.items(repo=REPO, scope="system")["items"], [], "system items belong to no repo")
        self.assertIn("system:S-1", [row["ref"] for row in factory.items()["items"]])
        self.assertEqual(sorted(factory.scope_items("system")), ["items", "log", "summary"])
        done = factory.item_action("system:S-1", "verify", note="re-read the profile card")
        self.assertEqual(done["item"]["status"], "verified")
        self.assertEqual(factory.system_item({"title": "Kate changed again"}, dedupe="kate:b")["ref"], "system:S-3",
                         "a closed item does not swallow the next change")
        later = factory.item_action("system:S-2", "defer", reason="after the upgrade", due="2027-01-10")
        self.assertEqual((later["item"]["status"], later["item"]["due"]), ("deferred", "2027-01-10"))
        with self.assertRaisesRegex(ValueError, "no repo"):
            factory.item_action("system:S-2", "make-job")
        with self.assertRaises(KeyError):
            factory.item_action("system:S-9", "verify")
        self.assertEqual([entry["to"] for entry in factory.item_history("system:S-1")["log"]], ["open", "verified"])
        refs = factory.add_system_items([{"title": "From a deleted chat", "kind": "followup", "owner": "human",
                                          "verifier": "human", "status": "open"}])
        self.assertEqual(refs, ["system:S-4"])

    # -- review fixes: the gate, the cap, evidence, retries, ended jobs ----------------------------------

    def test_the_approver_cannot_answer_a_product_decision(self) -> None:
        def spec(ctx: RunContext, call: Call) -> None:
            (ctx.job_dir / "spec.md").write_text("# Spec\n\n## Assumptions\n- Charge 5 EUR per seat. **(product)**\n")

        def deciding(ctx: RunContext, call: Call) -> None:
            (ctx.job_dir / "decision.md").write_text("APPROVE\n\n## Why\nAll good.\n\n## Items\n"
                                                     "- I-1 verified: the price looks reasonable to me\n"
                                                     "- I-1 dismiss: not needed\n")
        factory = self.start(spec_writer=spec, approver=deciding)
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "needs-human")
        self.assertEqual(job["reason"], "1 blocking item not proven: I-1 (To do, human)")
        decision = self.items(job)["I-1"]
        self.assertEqual((decision["status"], decision["verified_by"]), ("open", None))
        notices = json.loads((Path(job["job_dir"]) / "items.json").read_text())["notices"]["approver"]
        self.assertTrue(all("only a person can answer it" in notice for notice in notices), notices)
        factory.item_action(f"job:{REPO}/{job['id']}/I-1", "verify", note="product says yes")
        factory.approve(REPO, job["id"])
        self.assertEqual(factory.wait(REPO, job["id"], timeout=TIMEOUT)["stage"], "merged")

    def test_a_full_ledger_stops_the_job_after_the_review_rounds_own_checks(self) -> None:
        def sneaky(ctx: RunContext, call: Call) -> None:   # must-do suggestions count: they hold the gate
            commit_file(ctx.cwd, "backdoor.txt", "reviewer was here\n", "reviewer commit")
            lines = "".join(f"- new blocking: thing {n}\n" for n in range(305))
            ctx.write_paths[0].write_text(f"VERDICT: CHANGES\n\n## Findings\nNone.\n\n## Items\n{lines}")
        job = self.run_job(self.start(reviewer_code=sneaky))
        self.assertEqual(job["stage"], "needs-human")
        self.assertTrue(job["reason"].startswith("the branch moved during review round 1 (a reviewer committed?); "),
                        job["reason"])
        self.assertIn("already holds 300 open items", job["reason"])
        self.assertEqual(len(self.items(job)), 300)
        self.assertEqual(self.recorder.of("approver"), [])

    def test_a_full_ledger_after_an_approve_stops_before_the_gate(self) -> None:
        def flooding(ctx: RunContext, call: Call) -> None:
            approve(ctx, call)
            lines = "".join(f"- new blocking: thing {n}\n" for n in range(305))
            with (ctx.job_dir / "decision.md").open("a") as handle:
                handle.write(f"\n## Items\n{lines}")
        job = self.run_job(self.start(approver=flooding))
        self.assertEqual(job["stage"], "needs-human")
        self.assertIn("already holds 300 open items", job["reason"])
        self.assertTrue(self.branch_exists(job["branch"]), "nothing merged")

    def test_rework_and_room_after_the_cap(self) -> None:
        def flood(ctx: RunContext, call: Call) -> None:
            commit_file(ctx.cwd, f"f{call.index}.txt", "f\n", "f")
            lines = "".join(f"- new: thing {n}\n" for n in range(305)) if call.index == 0 else ""
            write_build(ctx, "READY", f"\n## Items\n{lines}")
        factory = self.start(builder=flood)
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "needs-human")
        ref = f"job:{REPO}/{job['id']}"
        with self.assertRaisesRegex(ValueError, "verify, drop or move some to later first"):
            factory.add_item(ref, title="one more")
        factory.item_action(f"{ref}/I-1", "drop", reason="noise")
        self.assertEqual(factory.add_item(ref, title="one more")["id"], "I-301", "closing frees room")
        self.recorder.actions["builder"] = build
        factory.rework(REPO, job["id"], "Rename greet to hello.")   # a person's note is never refused
        rework = self.items(job)["I-302"]
        self.assertEqual((rework["kind"], rework["blocking"]), ("rework", True))

    def test_carry_forward_is_never_refused_for_the_follow_ups_cap(self) -> None:
        def noting(ctx: RunContext, call: Call) -> None:
            ctx.write_paths[0].write_text("VERDICT: PASS\n\n## Findings\nNone.\n\n## Notes\n- The helper name is long.\n")
        factory = self.start(reviewer_code=noting)
        for n in range(300):
            factory.add_item(f"repo:{REPO}", title=f"old follow-up {n}")
        with self.assertRaisesRegex(ValueError, "300 follow-ups that are not proven or dropped"):
            factory.add_item(f"repo:{REPO}", title="one more")
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertEqual(self.items(job)["I-1"]["links"]["followup"], f"repo:{REPO}/F-301")
        self.assertEqual(self.events(job, "items.carried")[-1].data["count"], 1)

    def test_a_done_line_whose_commit_fails_its_check_is_asked_about_again(self) -> None:
        def faking(ctx: RunContext, call: Call) -> None:
            sha = commit_file(ctx.cwd, f"f{call.index}.txt", "f\n", "f")
            proof = "commit 1234567" if call.index == 0 else f"commit {sha[:12]}"
            write_build(ctx, "READY", f"\n## Items\n- I-1 done: {proof}\n- I-2 done: {proof}\n")
        factory = self.start(spec_writer=spec_with_criteria, builder=faking)
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        builder = self.recorder.of("builder")
        self.assertEqual([c.kind for c in builder], ["run", "resume"])
        self.assertIn(ITEMS_FIX.format(ids="I-1, I-2"), builder[1].prompt)
        self.assertIn("I-1 is To do again: evidence check failed: commit 1234567 is not in the repository",
                      builder[1].prompt)

    def test_a_plain_number_next_to_a_real_commit_does_not_reopen(self) -> None:
        def timing(ctx: RunContext, call: Call) -> None:
            sha = commit_file(ctx.cwd, "f.txt", "f\n", "f")
            proof = f"commit {sha[:12]}; `pytest -q` passed, 12 tests in 2400123 microseconds"
            write_build(ctx, "READY", f"\n## Items\n- I-1 done: {proof}\n- I-2 done: {proof}\n")
        job = self.run_job(self.start(spec_writer=spec_with_criteria, builder=timing))
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertEqual([e["kind"] for e in self.items(job)["I-1"]["evidence"][:2]], ["command", "commit"])
        self.assertEqual(len(self.recorder.of("builder")), 1)

    def test_a_failed_spec_run_is_not_taken_in_and_a_retried_spec_is_followed(self) -> None:
        draft = ("# Spec\n\n## Acceptance criteria\n1. Return 400 on a bad email.\n2. Log every signup.\n"
                 "3. Send a welcome email.\n")
        final = "# Spec\n\n## Acceptance criteria\n1. Return 422 on a bad email.\n2. A test covers the 422 path.\n"

        def spec(ctx: RunContext, call: Call) -> AgentRun | None:
            if call.index == 0:
                (ctx.job_dir / "spec.md").write_text(draft)
                return AgentRun(ok=False, error="provider down", usage=dict(USAGE))
            (ctx.job_dir / "spec.md").write_text(final)
            return None
        factory = self.start(spec_writer=spec)
        job = self.run_job(factory)
        self.assertEqual((job["stage"], job["failed_step"]), ("failed", "spec"))
        self.assertFalse((Path(job["job_dir"]) / "items.json").exists() and self.items(job))
        factory.retry(REPO, job["id"])
        job = factory.wait(REPO, job["id"], timeout=TIMEOUT)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertEqual([i["title"] for i in self.items(job).values()],
                         ["Return 422 on a bad email.", "A test covers the 422 path."])

    def test_an_ended_job_takes_no_new_or_reopened_items(self) -> None:
        factory = self.start(spec_writer=spec_with_criteria)
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        ref = f"job:{REPO}/{job['id']}"
        with self.assertRaisesRegex(ValueError, f"has ended \\(merged\\): add a follow-up instead \\(repo:{REPO}\\)"):
            factory.add_item(ref, title="Check the docs", blocking=True)
        with self.assertRaisesRegex(ValueError, "has ended"):
            factory.item_action(f"{ref}/I-1", "reopen", reason="it broke")
        self.assertEqual(factory.job(REPO, job["id"])["items"]["blocking_open"], 0)

    def test_make_job_twice_makes_one_job(self) -> None:
        factory = self.start(spec_writer=spec_with_criteria, builder=silent_build)
        job = self.run_job(factory)
        factory.reject(REPO, job["id"])
        release = threading.Event()
        self.addCleanup(release.set)
        self.recorder.actions["spec-writer"] = lambda ctx, call: release.wait(TIMEOUT) and None
        first = factory.item_action(f"repo:{REPO}/F-1", "make-job")
        with self.assertRaisesRegex(ValueError, f"already being worked on by job {first['job']['id']}"):
            factory.item_action(f"repo:{REPO}/F-1", "make-job")
        self.assertEqual(len([j for j in factory.jobs() if j["id"] != job["id"]]), 1)
        factory.cancel(REPO, first["job"]["id"])
        release.set()
        factory.wait(REPO, first["job"]["id"], timeout=TIMEOUT)
        self.assertEqual(self.followups()["F-1"]["status"], "open", "a cancelled job's follow-up is To do again")
        self.recorder.actions["spec-writer"] = write_spec
        second = factory.item_action(f"repo:{REPO}/F-1", "make-job")
        self.assertEqual(second["item"]["links"]["job"], second["job"]["id"])
        self.assertTrue(second["job"]["feature"].startswith(f"Follow-up F-1 from job {job['id']}: "),
                        "the job it came from, not the job made for it before")

    def test_an_item_s_whole_history(self) -> None:
        factory = self.start()
        for n in range(120):
            factory.add_item(f"repo:{REPO}", title=f"follow-up {n}")
        factory.item_action(f"repo:{REPO}/F-1", "verify", note="done by hand")
        self.assertEqual(len(factory.scope_items(f"repo:{REPO}")["log"]), 100)
        history = factory.item_history(f"repo:{REPO}/F-1")
        self.assertEqual(history["item"]["ref"], f"repo:{REPO}/F-1")
        self.assertEqual([(e["from"], e["to"]) for e in history["log"]], [(None, "open"), ("open", "verified")])
        with self.assertRaises(KeyError):
            factory.item_history(f"repo:{REPO}/F-999")
        with self.assertRaises(ValueError):
            factory.item_history(f"repo:{REPO}")


class ItemsRefinementTests(FactoryTestCase):
    """The v2 design's §10.1 refinements: PASS reviews require nothing, hand-offs, met without evidence, the
    approve carry rule in both branches, carry-forward after a live run, the proposal limit."""

    items = ItemsTests.items
    followups = ItemsTests.followups
    chat_branch = FromBranchTests.chat_branch

    def test_a_pass_review_s_findings_and_must_do_lines_become_suggestions(self) -> None:
        def passing(ctx: RunContext, call: Call) -> None:
            ctx.write_paths[0].write_text("VERDICT: PASS\n\n## Findings\n1. The helper name is long.\n\n## Items\n"
                                          "- new blocking: Rename the helper before merge\n")
        factory = self.start(reviewer_code=passing)
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        items = self.items(job)
        self.assertEqual([(i["kind"], i["blocking"], i["source"].get("type")) for i in items.values()],
                         [("note", False, "review"), ("commitment", False, "agent")],
                         "a PASS never blocks: its findings and its must-do lines are suggestions")
        notices = json.loads((Path(job["job_dir"]) / "items.json").read_text())["notices"]["reviewer-code"]
        self.assertTrue(any("a review that passes cannot require anything" in n for n in notices), notices)

    def test_a_chat_hand_off_with_a_two_criterion_spec_merges(self) -> None:
        factory = self.start(spec_writer=spec_with_criteria)
        job = self.run_job(factory, "Ship the chat work", from_branch=self.chat_branch())
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        items = self.items(job)
        self.assertEqual([(i["owner"], i["verifier"], i["status"], i["verified_by"]) for i in items.values()],
                         [("human", "approver", "verified", "approver")] * 2,
                         "the person's criteria, proven by the approver though nobody marked them done")
        self.assertEqual(self.recorder.of("builder"), [], "no ITEMS_FIX: no builder runs in a hand-off's round 1")
        self.assertIn("waiting for your check: I-1, I-2", self.recorder.of("approver")[0].prompt)

    def test_an_approver_s_met_on_a_criterion_nobody_proved_leaves_it_open_and_names_it(self) -> None:
        def half(ctx: RunContext, call: Call) -> None:
            commit_file(ctx.cwd, f"feature-{call.index}.txt", "half\n", "half")
            write_build(ctx, "READY", "\n## Items\n- I-1 doing: half way\n- I-2 doing: half way\n")

        def claims(ctx: RunContext, call: Call) -> None:
            (ctx.job_dir / "decision.md").write_text("APPROVE\n\n## Why\nFine.\n\n## Acceptance criteria\n"
                                                     "1. greet() says hello.: met, I ran it\n"
                                                     "2. A test covers greet().: met\n")
        factory = self.start(spec_writer=spec_with_criteria, builder=half, approver=claims)
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "needs-human")
        claim = "the approver said met, but the builder reported no evidence"
        self.assertEqual(job["reason"], f"2 blocking items not proven: I-1 (Working on it, builder: {claim}), "
                                        f"I-2 (Working on it, builder: {claim})")
        self.assertEqual({i["status"] for i in self.items(job).values()}, {"doing"})
        log = (Path(job["job_dir"]) / "items.log.jsonl").read_text()
        self.assertIn(claim, log)

    def test_an_approve_over_a_not_met_criterion_stops_for_a_person(self) -> None:
        def disagree(ctx: RunContext, call: Call) -> None:   # v2 §10.1.15 acceptance check 2
            (ctx.job_dir / "decision.md").write_text("APPROVE\n\n## Why\nFine.\n\n## Acceptance criteria\n"
                                                     "1. greet() says hello.: met, ran it\n"
                                                     "2. A test covers greet().: not met, there is no test\n")
        factory = self.start(spec_writer=spec_with_criteria, approver=disagree)
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "needs-human")
        self.assertEqual(job["reason"], "1 blocking item not proven: I-2 (To do, builder)")
        self.assertIn("not met, there is no test", self.items(job)["I-2"]["reason"])

    def test_branch_delivery_approve_needs_the_open_items_named_too(self) -> None:
        factory = self.start(spec_writer=spec_with_criteria, builder=silent_build)
        job = self.run_job(factory, delivery="branch")
        self.assertEqual(job["stage"], "needs-human")
        with self.assertRaises(ActionError) as refused:
            factory.approve(REPO, job["id"])
        self.assertEqual(refused.exception.payload["open_blocking"], ["I-1", "I-2"])
        factory.approve(REPO, job["id"], carry_items=["I-2", "I-1"])
        job = factory.wait(REPO, job["id"], timeout=TIMEOUT)
        self.assertEqual(job["stage"], "delivered", job.get("reason"))
        self.assertEqual({i["reason"] for i in self.items(job).values()}, {"delivered by human override while open"})
        self.assertEqual(len(self.followups()), 2)

    def test_housekeeping_off_needs_no_carry_items(self) -> None:
        config = self.config(housekeeping={"enabled": False})
        factory = self.start(config, spec_writer=spec_with_criteria, builder=silent_build, approver=plain_approve)
        job = self.run_job(factory, delivery="branch")
        self.assertEqual(job["stage"], "delivered", job.get("reason"))

    def test_cancel_during_a_run_with_a_tool_added_item_leaves_a_follow_up(self) -> None:
        release = threading.Event()
        self.addCleanup(release.set)
        started = threading.Event()

        def building(ctx: RunContext, call: Call) -> AgentRun:
            added = ctx.items.call({"action": "add", "title": "Check the other callers of greet()"})
            self.assertTrue(added.startswith("Added I-1"), added)
            started.set()
            release.wait(TIMEOUT)
            return AgentRun(ok=False, error="cancelled", usage=dict(USAGE))
        factory = self.start(builder=building)
        job = factory.submit(REPO, "Add a greeting")
        self.assertTrue(started.wait(TIMEOUT))
        factory.cancel(REPO, job["id"])
        self.assertFalse((self.tmp / "data" / "jobs" / REPO / "followups.json").exists(),
                         "never carried while the run is live")
        release.set()
        job = factory.wait(REPO, job["id"], timeout=TIMEOUT)
        self.assertEqual(job["stage"], "cancelled")
        follow = self.followups()["F-1"]
        self.assertEqual((follow["title"], follow["links"]["job"], follow["source"]["reason"]),
                         ("Check the other callers of greet()", job["id"], "job cancelled"))
        self.assertEqual(self.items(job)["I-1"]["links"]["followup"], f"repo:{REPO}/F-1")

    def test_reject_after_a_failed_run_carries_its_journal_forward(self) -> None:
        def failing_build(ctx: RunContext, call: Call) -> AgentRun:
            ctx.items.call({"action": "add", "title": "Retry the flaky test"})
            return AgentRun(ok=False, error="provider down", usage=dict(USAGE))
        factory = self.start(builder=failing_build)
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "failed")
        factory.reject(REPO, job["id"], "not now")
        self.assertEqual([f["title"] for f in self.followups().values()], ["Retry the flaky test"])

    def test_a_run_that_breaks_the_write_policy_still_gives_its_items_calls(self) -> None:
        def trespass(ctx: RunContext, call: Call) -> None:
            ctx.items.call({"action": "add", "title": "Explain the extra file"})
            (ctx.job_dir / "spec.md").write_text("# Spec\n\nrewritten by the builder\n")
            build(ctx, call)
        factory = self.start(builder=trespass)
        job = self.run_job(factory)
        self.assertEqual((job["stage"], job["failed_step"]), ("failed", "build"))
        self.assertIn("write policy", job["reason"])
        self.assertEqual([i["title"] for i in self.items(job).values()], ["Explain the extra file"])

    def test_submit_writes_handed_over_items_before_the_job_is_queued(self) -> None:
        from swfactory import items as items_lib

        factory = self.start()
        tracker_scope = items_lib.chat_scope("c-0123456789", REPO)
        origin = {"id": "C-3", "title": "Handle the empty list", "status": "open", "kind": "commitment",
                  "blocking": True, "evidence": [], "source": {}}
        fields = items_lib.transfer_copy(origin, tracker_scope, owner="builder", verifier="approver",
                                         reason="handed off from the chat")
        job = factory.submit(REPO, "Ship the chat work", from_branch=self.chat_branch(), items=[fields])
        handed = json.loads((Path(job["job_dir"]) / "items.json").read_text())["items"]
        self.assertEqual([(i["id"], i["owner"], i["blocking"], i["links"]["chat"]) for i in handed],
                         [("I-1", "builder", True, "c-0123456789")])
        job = factory.wait(REPO, job["id"], timeout=TIMEOUT)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertEqual(self.items(job)["I-1"]["verified_by"], "approver", "a hand-off's approver proves it")
        with self.assertRaisesRegex(ValueError, "items must be a list"):
            factory.submit(REPO, "x", items={"title": "no"})   # type: ignore[arg-type]

    def test_suggestions_past_the_scope_s_limit_fold_into_one_item_and_never_stop_the_job(self) -> None:
        def chatty(ctx: RunContext, call: Call) -> None:
            lines = "".join(f"- new: idea {n}\n" for n in range(25))
            ctx.write_paths[0].write_text(f"VERDICT: PASS\n\n## Findings\nNone.\n\n## Items\n{lines}")
        config = self.config(housekeeping={"max_proposals_per_scope": 10})
        factory = self.start(config, reviewer_code=chatty)
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        items = list(self.items(job).values())
        self.assertEqual(len(items), 11, "ten one by one, the rest in one item")
        overflow = items[-1]
        self.assertEqual((overflow["source"]["type"], overflow["source"]["count"]), ("overflow", 15))
        self.assertTrue(overflow["title"].startswith("15 more suggestions"), overflow["title"])
        self.assertIn("- idea 24 (reviewer-code)", overflow["detail"])
        self.assertIsNone(self.followups()[f"F-{len(items)}"]["due"])

    def test_an_agent_s_own_defer_reaches_the_follow_ups_at_once(self) -> None:
        def deferring(ctx: RunContext, call: Call) -> None:
            added = ctx.items.call({"action": "add", "title": "Update the footer copy"})
            self.assertTrue(added.startswith("Added I-1"), added)
            ctx.items.call({"action": "update", "id": "I-1", "to": "defer",
                            "reason": "needs a product decision on the copy"})
            build(ctx, call)
        factory = self.start(builder=deferring)
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        item = self.items(job)["I-1"]
        self.assertEqual((item["status"], item["links"]["followup"]), ("deferred", f"repo:{REPO}/F-1"))
        follow = self.followups()
        self.assertEqual([(f["title"], f["status"], f["links"]["job"], f["source"]["reason"]) for f in follow.values()],
                         [("Update the footer copy", "open", job["id"], "needs a product decision on the copy")],
                         "moved when the run was taken in, and not carried again at the job's end")
        notices = json.loads((Path(job["job_dir"]) / "items.json").read_text())["notices"]["builder"]
        self.assertIn(f"I-1 is Later: it moved to the repo's follow-ups as repo:{REPO}/F-1", notices)

    def test_a_later_item_that_never_moved_goes_with_the_job_s_end(self) -> None:
        factory = Factory(self.config(), self.bus, runner_factory=self.recorder.runner_factory)   # no worker
        self.addCleanup(factory.stop)
        job = factory.submit(REPO, "Add a greeting")
        live = factory._entry((REPO, job["id"]))
        with factory._job_items(live) as ledger:   # as an older ledger left it: Later inside the job
            item = ledger.add("factory", "Check the print stylesheet", owner="builder", verifier="approver")
            ledger.change("builder", item["id"], "deferred", reason="next sprint")
        job = factory.cancel(REPO, job["id"])   # no worker drives it: cancel carries forward itself
        self.assertEqual(job["stage"], "cancelled")
        self.assertEqual(self.items(job)["I-1"]["links"]["followup"], f"repo:{REPO}/F-1")
        self.assertEqual([(f["title"], f["status"]) for f in self.followups().values()],
                         [("Check the print stylesheet", "open")])

    def test_a_hand_over_is_announced_before_its_items_and_a_bad_one_leaves_no_job(self) -> None:
        from swfactory import items as items_lib

        factory = Factory(self.config(), self.bus, runner_factory=self.recorder.runner_factory)   # no worker
        self.addCleanup(factory.stop)
        handed = {"title": "Handle the empty list", "kind": "commitment", "owner": "builder", "verifier": "approver",
                  "blocking": True, "status": "open", "links": {"chat": "c-0123456789"}}
        job = factory.submit(REPO, "Add the feature", items=[handed])
        kinds = [e.type for e in self.events(job)]
        self.assertEqual(kinds[:2], ["job.created", "items.updated"], kinds)
        self.assertEqual(self.events(job, "job.created")[0].data["job"]["items"]["blocking_open"], 1)
        with self.assertRaisesRegex(ValueError, "an item needs a title"):
            factory.submit(REPO, "Add another", items=[{"title": "   "}])
        with mock.patch.object(items_lib.ItemStore, "_save", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                factory.submit(REPO, "Add a third", items=[handed])
        jobs = sorted(path.parent.name for path in (self.tmp / "data" / "jobs" / REPO).glob("*/job.json"))
        self.assertEqual(jobs, [job["id"]], "neither refused job was left half made")
        self.assertEqual([e.type for e in self.bus.since(0) if e.job not in (None, job["id"])], [])
        again = factory.submit(REPO, "Add a fourth", items=[{**handed, "title": "Its own item"}])
        titles = [i["title"] for i in json.loads((Path(again["job_dir"]) / "items.json").read_text())["items"]]
        self.assertEqual(titles, ["Its own item"], "a removed job's items are never put back into the next one")


SPEC_TEXT = "# Spec\n\nAdd a greeting.\n"


class WritePolicyTests(FactoryTestCase):
    """DESIGN section 7: what each agent may write is checked after every run, whatever the driver."""

    def warnings(self, job: dict) -> list[str]:
        return [e.data["message"] for e in self.events(job, "log") if e.data["level"] == "warn"]

    def test_a_reviewer_cannot_stand_in_for_another(self) -> None:
        code_done = threading.Event()

        def forger(ctx: RunContext, call: Call) -> None:      # reviewer-code
            review_pass(ctx, call)
            (ctx.write_paths[0].parent / "review-security.md").write_text("VERDICT: PASS\n")
            (ctx.job_dir / "spec.md").write_text("forged spec\n")
            code_done.set()

        def silent(ctx: RunContext, call: Call) -> None:      # reviewer-security writes nothing
            code_done.wait(TIMEOUT)
        job = self.run_job(self.start(reviewer_code=forger, reviewer_security=silent))
        self.assertEqual(job["stage"], "needs-human")
        self.assertEqual(job["reason"], "write policy, review round 1: spec.md changed outside the review files "
                                        "(reverted)")
        self.assertEqual((Path(job["job_dir"]) / "spec.md").read_text(), SPEC_TEXT)
        self.assertEqual(self.recorder.of("approver"), [])
        self.assertIn(job["reason"], self.warnings(job))

    def test_a_review_file_changed_after_its_reviewer_finished_is_an_error(self) -> None:
        holder: dict[str, Factory] = {}

        def late_forger(ctx: RunContext, call: Call) -> None:   # reviewer-code
            until(lambda: holder["factory"].job(REPO, ctx.job["id"])["reviews"]["security"] == "CHANGES")
            (ctx.write_paths[0].parent / "review-security.md").write_text("VERDICT: PASS\n")
            review_pass(ctx, call)
        factory = holder["factory"] = self.start(reviewer_code=late_forger, reviewer_security=reviews("CHANGES"))
        job = self.run_job(factory)
        self.assertEqual(job["stage"], "needs-human")
        self.assertEqual(job["reason"], "write policy, review round 1: round-1/review-security.md changed after "
                                        "reviewer-security finished")
        self.assertEqual(job["reviews"], {"security": "ERROR", "code": "PASS"})
        self.assertEqual(job["history"], [{"round": 1, "reviews": {"security": "ERROR", "code": "PASS"}}])
        self.assertEqual(len(self.recorder.of("builder")), 1, "a forged round never resumes the builder")

    def test_a_builder_writing_outside_its_paths_fails_the_step_and_is_reverted(self) -> None:
        def overreach(ctx: RunContext, call: Call) -> None:
            build(ctx, call)
            (ctx.job_dir / "spec.md").write_text("the builder's own spec\n")
            (ctx.job_dir / "notes.md").write_text("x\n")
            (ctx.job_dir / "sessions" / "approver.json").write_text("{}")
            (ctx.job_dir / "sessions" / "builder.cli.json").write_text("{}")   # its own session files are fine
        factory = self.start(builder=overreach)
        job = self.run_job(factory)
        self.assertEqual((job["stage"], job["failed_step"]), ("failed", "build"))
        self.assertEqual(job["reason"], "write policy: builder changed notes.md, sessions/approver.json, spec.md in "
                                        "the job folder, outside its write paths (reverted)")
        job_dir = Path(job["job_dir"])
        self.assertEqual((job_dir / "spec.md").read_text(), SPEC_TEXT)
        self.assertFalse((job_dir / "notes.md").exists())
        self.assertFalse((job_dir / "sessions" / "approver.json").exists())
        self.assertTrue((job_dir / "sessions" / "builder.cli.json").exists())
        self.recorder.actions["builder"] = build
        factory.retry(REPO, job["id"])
        self.assertEqual(factory.wait(REPO, job["id"], timeout=TIMEOUT)["stage"], "merged")

    def test_a_symlinked_spec_is_restored_as_a_file(self) -> None:
        def swap(ctx: RunContext, call: Call) -> None:
            build(ctx, call)
            (ctx.job_dir / "spec.md").unlink()
            os.symlink("/etc/hostname", ctx.job_dir / "spec.md")
        job = self.run_job(self.start(builder=swap))
        self.assertEqual((job["stage"], job["failed_step"]), ("failed", "build"))
        spec = Path(job["job_dir"]) / "spec.md"
        self.assertFalse(spec.is_symlink())
        self.assertEqual(spec.read_text(), SPEC_TEXT)

    def test_the_spec_writer_may_write_only_spec_md(self) -> None:
        def overreach(ctx: RunContext, call: Call) -> None:
            write_spec(ctx, call)
            (ctx.job_dir / "round-1").mkdir()
            (ctx.job_dir / "round-1" / "review-code.md").write_text("VERDICT: PASS\n")
            (ctx.job_dir / "decision.md").write_text("APPROVE\n")
        job = self.run_job(self.start(spec_writer=overreach))
        self.assertEqual((job["stage"], job["failed_step"]), ("failed", "spec"))
        self.assertEqual(job["reason"], "write policy: spec-writer changed decision.md, round-1/review-code.md in the "
                                        "job folder, outside its write paths (reverted)")
        self.assertFalse((Path(job["job_dir"]) / "decision.md").exists())
        self.assertFalse((Path(job["job_dir"]) / "round-1" / "review-code.md").exists())
        self.assertEqual(len(self.recorder.of("spec-writer")), 1)

    def test_a_spec_writer_commit_needs_a_human(self) -> None:
        def commits(ctx: RunContext, call: Call) -> None:
            write_spec(ctx, call)
            commit_file(ctx.cwd, "sneaky.txt", "x\n", "sneaky")
        job = self.run_job(self.start(spec_writer=commits))
        self.assertEqual(job["stage"], "needs-human")
        self.assertIn("the branch moved while the spec-writer ran", job["reason"])
        self.assertEqual(self.recorder.of("builder"), [])

    def test_job_json_rewritten_by_an_agent_is_reported_and_rewritten(self) -> None:
        def rewrite(ctx: RunContext, call: Call) -> None:
            approve(ctx, call)
            (ctx.job_dir / "job.json").write_text(json.dumps({**ctx.job, "stage": "merged"}))
        job = self.run_job(self.start(approver=rewrite))
        self.assertEqual((job["stage"], job["failed_step"]), ("failed", "approve"))
        self.assertEqual(job["reason"], "write policy: approver changed job.json in the job folder, outside its write "
                                        "paths (reverted)")
        self.assertEqual(json.loads((Path(job["job_dir"]) / "job.json").read_text())["stage"], "failed")
        self.assertTrue(self.branch_exists(job["branch"]), "nothing merged")

    def test_job_json_rewritten_during_a_review_round_is_reported(self) -> None:
        holder: dict[str, Factory] = {}

        def rewrite(ctx: RunContext, call: Call) -> None:
            # After the sibling's last save: a save already under way when the agent writes would
            # overwrite (and so undo) the change before it could be seen.
            until(lambda: holder["factory"].job(REPO, ctx.job["id"])["reviews"]["security"] == "PASS")
            review_pass(ctx, call)
            (ctx.job_dir / "job.json").write_text("{}")
        job = self.run_job(holder.setdefault("factory", self.start(reviewer_code=rewrite)))
        self.assertEqual(job["stage"], "needs-human")
        self.assertEqual(job["reason"], "write policy, review round 1: job.json changed outside the review files "
                                        "(reverted)")
        self.assertEqual(json.loads((Path(job["job_dir"]) / "job.json").read_text())["stage"], "needs-human")

    def test_kept_verdicts_are_protected_in_a_retried_round(self) -> None:
        def down_once(ctx: RunContext, call: Call) -> AgentRun | None:   # reviewer-code
            if call.index == 0:
                return AgentRun(ok=False, error="provider down")
            (ctx.write_paths[0].parent / "review-security.md").write_text("VERDICT: CHANGES\n")
            review_pass(ctx, call)
            return None
        factory = self.start(reviewer_code=down_once)
        job = self.run_job(factory)
        self.assertEqual((job["stage"], job["failed_step"]), ("failed", "review"))
        factory.retry(REPO, job["id"])
        job = factory.wait(REPO, job["id"], timeout=TIMEOUT)
        self.assertEqual(job["stage"], "needs-human")
        self.assertEqual(job["reason"], "write policy, review round 1: round-1/review-security.md changed outside "
                                        "the review files (reverted)")
        self.assertEqual((Path(job["job_dir"]) / "round-1" / "review-security.md").read_text(), "VERDICT: PASS\n")
        self.assertEqual(len(self.recorder.of("reviewer-security")), 1)

    def test_approver_edits_in_the_worktree_are_discarded(self) -> None:
        def meddle(ctx: RunContext, call: Call) -> None:
            (ctx.cwd / "approver-notes.txt").write_text("x\n")
            approve(ctx, call)
        job = self.run_job(self.start(approver=meddle))
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertIn("factory: discarded approver edits", run_git(self.repo, "stash", "list"))
        self.assertFalse((self.repo / "approver-notes.txt").exists())


class PolicyHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="sf-policy-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_scan_skips_what_the_orchestrator_writes_and_never_follows_links(self) -> None:
        (self.root / "logs").mkdir()
        (self.root / "logs" / "01-builder.jsonl").write_text("{}\n")
        (self.root / "job.json").write_text("{}")
        (self.root / ".sf-job.json.abc.tmp").write_text("partial")
        (self.root / "sessions").mkdir()
        (self.root / "sessions" / "builder.json").write_text("{}")
        (self.root / "round-1").mkdir()
        (self.root / "round-1" / "review-code.md").write_text("VERDICT: PASS\n")
        os.symlink("/etc", self.root / "etc-link")
        os.mkfifo(self.root / "pipe")
        states = _scan(self.root, keep=True)
        self.assertEqual(sorted(states), ["etc-link", "pipe", "round-1/review-code.md", "sessions/builder.json"])
        self.assertEqual((states["etc-link"].kind, states["etc-link"].data), ("link", b"/etc"))
        self.assertEqual(states["pipe"].kind, "other")
        self.assertEqual(states["round-1/review-code.md"].data, b"VERDICT: PASS\n")
        self.assertIsNone(_scan(self.root)["round-1/review-code.md"].data, "contents are kept only when asked")

    def test_revert_restores_changed_removed_and_replaced_files(self) -> None:
        (self.root / "spec.md").write_text("spec\n")
        (self.root / "build.md").write_text("build\n")
        before = _scan(self.root, keep=True)
        (self.root / "spec.md").unlink()
        (self.root / "spec.md").mkdir()
        (self.root / "spec.md" / "inner.txt").write_text("x")
        (self.root / "build.md").unlink()
        (self.root / "new.md").write_text("new\n")
        changed = _changed(before, _scan(self.root))
        self.assertEqual(changed, ["build.md", "new.md", "spec.md", "spec.md/inner.txt"])
        self.assertTrue(all(_revert(self.root, rel, before.get(rel)) for rel in changed))
        self.assertEqual(_changed(before, _scan(self.root)), [])
        self.assertEqual((self.root / "spec.md").read_text(), "spec\n")
        self.assertFalse((self.root / "new.md").exists())

    def test_files_too_big_to_keep_are_reported_not_reverted(self) -> None:
        (self.root / "big.md").write_bytes(b"x" * 10)
        with mock.patch("swfactory.orchestrator.POLICY_KEEP_BYTES", 4):
            before = _scan(self.root, keep=True)
        self.assertIsNotNone(before["big.md"].digest)
        (self.root / "big.md").write_bytes(b"y" * 10)
        self.assertEqual(_changed(before, _scan(self.root)), ["big.md"])
        self.assertFalse(_revert(self.root, "big.md", before["big.md"]))


class RealManagerTests(unittest.TestCase):
    def test_start_and_stop_with_the_real_chat_and_plan_managers(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="sf-orch-real-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with mock.patch.dict(os.environ, {"HOME": str(tmp)}):
            repo = make_repo(tmp)
            config = Config(tmp / "config.json", {"data_dir": str(tmp / "data"),
                                                  "repos": [{"name": REPO, "path": str(repo)}]})
            factory = Factory(config, EventBus(), runner_factory=Recorder().runner_factory)
            factory.start()
            try:
                self.assertEqual(factory.chats(), [])
                self.assertEqual(factory.plans(), [])
            finally:
                factory.stop()


if __name__ == "__main__":
    unittest.main()
