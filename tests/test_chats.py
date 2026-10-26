"""Tests for swfactory.chats: ChatManager with a scripted runner, a fake Factory and real git worktrees.

The fakes here (ScriptedRunners, FakeFactory, ManagerTestCase, wait_for) are shared with test_plans.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import sys
import tempfile
import threading
import time
import types
import unittest
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest import mock

from swfactory.chats import CHAT_ID_RE, INTERRUPTED, ChatManager, Route, agent_route, load_agent, next_log_path
from swfactory.config import Config
from swfactory.events import Event, EventBus
from swfactory.gitops import GitError
from swfactory.runners import AgentRun, RunContext, Runner
from tests.helpers import commit_file, make_repo, run_git

WAIT_SECONDS = 10.0


def wait_for(predicate: Callable[[], Any], timeout: float = WAIT_SECONDS, message: str = "condition") -> Any:
    """Poll predicate until it returns something truthy (returned) or fail after timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {message}")


@dataclass
class Call:
    kind: str                  # "run" or "resume"
    driver: str
    ctx: RunContext
    prompt: str
    session_ref: str | None


class ScriptedRunners:
    """A runner_factory: every run is recorded and answered by handler(call) (default: ok)."""

    def __init__(self, handler: Callable[[Call], AgentRun] | None = None, supports_resume: bool = True):
        self.handler = handler
        self.supports_resume = supports_resume
        self.calls: list[Call] = []
        self.drivers: list[str] = []
        self._lock = threading.Lock()

    def __call__(self, config: Config, driver: str) -> Runner:
        self.drivers.append(driver)
        return _ScriptedRunner(self, driver)

    def answer(self, call: Call) -> AgentRun:
        with self._lock:
            self.calls.append(call)
        if self.handler is not None:
            return self.handler(call)
        return AgentRun(ok=True, final_text="done", turns=1, session_ref=str(call.ctx.session_path),
                        usage={"input_tokens": 10, "output_tokens": 5, "cached_tokens": 2, "calls": 1})


class _ScriptedRunner(Runner):
    def __init__(self, owner: ScriptedRunners, driver: str) -> None:
        self.owner = owner
        self.driver = driver
        self.supports_resume = owner.supports_resume

    def run(self, ctx: RunContext, prompt: str) -> AgentRun:
        return self.owner.answer(Call("run", self.driver, ctx, prompt, None))

    def resume(self, ctx: RunContext, session_ref: str, message: str) -> AgentRun:
        return self.owner.answer(Call("resume", self.driver, ctx, message, session_ref))


class FakeFactory:
    """The Factory surface chats and plans use: submit, job, cancel; job stages driven by the test.

    Every change publishes job.updated (with the full job) on the real bus, like the orchestrator.
    """

    ACTIVE = ("queued", "spec", "build", "review", "approve", "merging")

    def __init__(self, bus: EventBus, action_error: type[Exception], repo_path: Path | None = None):
        self.bus = bus
        self.action_error = action_error
        self.repo_path = repo_path
        self.submitted: list[dict] = []
        self.cancelled: list[str] = []
        self.jobs: dict[tuple[str, str], dict] = {}
        self.fail_submit: Exception | None = None
        self.config: Config | None = None    # what check_routing validates against (the manager's config)
        self.routing_checks: list[tuple[str, Any]] = []
        self._lock = threading.Lock()

    def check_routing(self, repo: str, routing: Any = None, profile: str | None = None) -> dict:
        """The real orchestrator.Factory.check_routing on self.config (nothing is started)."""
        from swfactory.orchestrator import Factory

        self.routing_checks.append((repo, routing))
        assert self.config is not None, "set factory.config to the config the manager uses"
        return Factory(self.config, EventBus()).check_routing(repo, routing, profile)

    def check_profile(self, profile: str | None = None, routing: Any = None) -> str | None:
        """The real orchestrator.Factory.check_profile on self.config."""
        from swfactory.orchestrator import Factory

        assert self.config is not None, "set factory.config to the config the manager uses"
        return Factory(self.config, EventBus()).check_profile(profile, routing)

    def check_delivery(self, delivery: str | None = None) -> str:
        """The real orchestrator.Factory.check_delivery on self.config (or the defaults)."""
        from swfactory.orchestrator import Factory

        return Factory(self.config or Config(Path("/nonexistent/config.json")), EventBus()).check_delivery(delivery)

    def submit(self, repo: str, feature: str, from_branch: str | None = None, routing: Any = None,
               delivery: str | None = None, profile: str | None = None, items: list[dict] | None = None,
               handoff_models: list[dict] | None = None) -> dict:
        if self.fail_submit is not None:
            raise self.fail_submit
        with self._lock:
            job_id = f"{len(self.submitted) + 1:03d}-job"
            job = {"id": job_id, "repo": repo, "feature": feature, "stage": "queued",
                   "from_branch": from_branch, "routing": routing, "delivery": delivery, "merged_commit": None,
                   **({"profile": profile} if profile is not None else {}),
                   **({"handoff_models": handoff_models} if handoff_models is not None else {})}
            self.jobs[(repo, job_id)] = job
            self.submitted.append(dict(job))
            if items is not None:   # handed over with the job (written before it is queued)
                self.imported = getattr(self, "imported", []) + [(repo, job_id, items)]
        self.bus.publish("job.created", {"job": dict(job)}, repo=repo, job=job_id)
        self.bus.publish("job.updated", {"job": dict(job)}, repo=repo, job=job_id)
        return dict(job)

    def job(self, repo: str, job_id: str) -> dict:
        return dict(self.jobs[(repo, job_id)])

    def cancel(self, repo: str, job_id: str) -> dict:
        job = self.jobs[(repo, job_id)]
        if job["stage"] not in self.ACTIVE:
            raise self.action_error(f"{job_id} is {job['stage']}")
        self.cancelled.append(job_id)
        return self.set_stage(job_id, "cancelled", repo=repo)

    def add_followups(self, repo: str, entries: list[dict]) -> list[str]:
        """Items carried to the repo's follow-ups (a deleted chat, a deferred item): recorded, with a ref each."""
        self.followups = getattr(self, "followups", []) + entries
        return [f"repo:{repo}/F-{n}" for n in range(len(self.followups) - len(entries) + 1, len(self.followups) + 1)]

    def defer_to_followups(self, repo: str, ledger: Any, item_id: str, reason: str, due: str | None = None,
                           by: str = "human") -> str:
        from swfactory.orchestrator import Factory

        return Factory.defer_to_followups(self, repo, ledger, item_id, reason, due, by)   # same rules, fake store

    def _housekeeping(self) -> Any:
        from swfactory.config import HousekeepingConfig

        return HousekeepingConfig()

    def set_stage(self, job_id: str, stage: str, repo: str = "app", **fields: Any) -> dict:
        job = self.jobs[(repo, job_id)]
        job.update(stage=stage, **fields)
        self.bus.publish("job.updated", {"job": dict(job)}, repo=repo, job=job_id)
        return dict(job)

    def merge(self, job_id: str, repo: str = "app") -> str:
        """Merge a one-commit branch for the job into main (a real --no-ff merge) and mark it merged."""
        assert self.repo_path is not None
        branch = f"factory/{job_id}"
        run_git(self.repo_path, "checkout", "-q", "-b", branch, "main")
        commit_file(self.repo_path, f"{job_id}.txt", f"{job_id}\n", message=f"{job_id}: work")
        run_git(self.repo_path, "checkout", "-q", "main")
        run_git(self.repo_path, "merge", "-q", "--no-ff", "-m", f"Merge {branch}", branch)
        sha = run_git(self.repo_path, "rev-parse", "HEAD").strip()
        self.set_stage(job_id, "merged", repo=repo, merged_commit=sha)
        return sha


def action_error_class(test: unittest.TestCase) -> type[Exception]:
    """orchestrator.ActionError, or (while orchestrator.py cannot be imported) a stub for this test only."""
    try:
        from swfactory.orchestrator import ActionError
    except ImportError:
        stub = types.ModuleType("swfactory.orchestrator")
        stub.ActionError = type("ActionError", (Exception,), {})
        patcher = mock.patch.dict(sys.modules, {"swfactory.orchestrator": stub})
        patcher.start()
        test.addCleanup(patcher.stop)
        return stub.ActionError
    return ActionError


class ManagerTestCase(unittest.TestCase):
    """A temp dir with HOME inside it, a repo "app" on main, a config, a real bus and a fake factory."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sf-manager-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        home = self.tmp / "home"
        home.mkdir()
        patcher = mock.patch.dict(os.environ, {"HOME": str(home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repo = make_repo(self.tmp)
        self.ActionError = action_error_class(self)
        self.bus = EventBus()
        self.events = self.bus.subscribe()
        self.factory = FakeFactory(self.bus, self.ActionError, self.repo)

    def make_config(self, **raw: Any) -> Config:
        data = {"data_dir": str(self.tmp / "data"), "repos": [{"name": "app", "path": str(self.repo)}], **raw}
        return Config(self.tmp / "config.json", data)

    def published(self, event_type: str) -> list[Event]:
        """Events of event_type published since the last call (others seen meanwhile are dropped)."""
        found = []
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                return found
            if event.type == event_type:
                found.append(event)


class ChatTestCase(ManagerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.config = self.make_config()
        self.runners = ScriptedRunners()
        self.manager = ChatManager(self.config, self.bus, self.factory, runner_factory=self.runners)
        self.addCleanup(self.manager.stop)

    def settle(self, chat_id: str) -> dict:
        """Wait for the chat's turn to end and return the chat."""
        def ended() -> dict | None:
            chat = self.manager.chat(chat_id)
            return chat if chat["status"] != "running" else None

        return wait_for(ended, message=f"chat {chat_id} to finish its turn")

    def blocking_handler(self) -> tuple[threading.Event, threading.Event]:
        """Make runs wait for release (or their cancel event); returns (started, release)."""
        started, release = threading.Event(), threading.Event()

        def handler(call: Call) -> AgentRun:
            started.set()
            while not release.is_set():
                if call.ctx.cancel.wait(0.01):
                    return AgentRun(ok=False, error="cancelled", session_ref=str(call.ctx.session_path))
            return AgentRun(ok=True, session_ref=str(call.ctx.session_path), usage={"calls": 1})

        self.runners.handler = handler
        return started, release


class NewChatTest(ChatTestCase):
    def test_worktree_mode_creates_branch_and_worktree_from_base(self) -> None:
        chat = self.manager.new_chat("app")
        self.assertRegex(chat["id"], CHAT_ID_RE)
        worktree = self.tmp / "data" / "worktrees" / "app" / chat["id"]
        self.assertEqual(chat["worktree"], str(worktree))
        self.assertEqual(chat["cwd"], str(worktree))
        self.assertEqual(chat["branch"], f"chat/{chat['id']}")
        self.assertEqual((chat["mode"], chat["base"], chat["status"], chat["agent"]),
                         ("worktree", "main", "idle", "coder"))
        self.assertEqual((chat["model"], chat["driver"], chat["title"], chat["job"]), ("opus", "builtin", "", None))
        self.assertEqual(chat["usage"], {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "calls": 0})
        self.assertEqual(run_git(worktree, "symbolic-ref", "--short", "HEAD").strip(), chat["branch"])
        self.assertEqual(run_git(worktree, "rev-parse", "HEAD"), run_git(self.repo, "rev-parse", "main"))
        self.assertEqual(run_git(self.repo, "symbolic-ref", "--short", "HEAD").strip(), "main")
        self.assertEqual(run_git(self.repo, "status", "--porcelain"), "")
        stored = json.loads((self.tmp / "data" / "chats" / chat["id"] / "chat.json").read_text())
        self.assertEqual(stored, chat)
        self.assertEqual([e.data["chat"]["id"] for e in self.published("chat.updated")], [chat["id"]])

    def test_repo_mode_works_in_the_checkout(self) -> None:
        chat = self.manager.new_chat("app", mode="repo")
        self.assertEqual((chat["cwd"], chat["branch"], chat["worktree"]), (str(self.repo), None, None))
        self.assertEqual(run_git(self.repo, "branch", "--list", "chat/*"), "")
        self.assertFalse((self.tmp / "data" / "worktrees").exists())

    def test_model_override_and_configured_route(self) -> None:
        self.assertEqual(self.manager.new_chat("app", model="sonnet", mode="repo")["model"], "sonnet")
        config = self.make_config(agents={"*": {"driver": "builtin"},
                                          "coder": {"model": "local", "extra_body": {"temperature": 0.2}}})
        manager = ChatManager(config, self.bus, self.factory, runner_factory=self.runners)
        chat = manager.new_chat("app", mode="repo")
        self.assertEqual((chat["model"], chat["extra_body"]), ("local", {"temperature": 0.2}))
        self.assertEqual(manager.new_chat("app", model="haiku", mode="repo")["extra_body"], {})

    def test_invalid_arguments(self) -> None:
        with self.assertRaises(ValueError):
            self.manager.new_chat("app", mode="elsewhere")
        with self.assertRaises(KeyError):
            self.manager.new_chat("nope")
        with self.assertRaisesRegex(ValueError, "no agent named"):
            self.manager.new_chat("app", agent="ghost")
        broken = ChatManager(self.make_config(agents={"coder": {"driver": "missing"}}), self.bus, self.factory)
        with self.assertRaisesRegex(ValueError, "missing"):
            broken.new_chat("app")
        self.assertEqual(self.manager.chats(), [])

    def test_any_agent_with_tools_can_chat(self) -> None:
        user_agents = Path(os.environ["HOME"]) / ".config" / "software-factory" / "agents"
        user_agents.mkdir(parents=True)
        (user_agents / "helper.md").write_text("---\nname: helper\ndescription: Helps.\ntools: Read, Grep\n"
                                               "model: sonnet\n---\nYou help.\n")
        (user_agents / "talker.md").write_text("---\nname: talker\ndescription: Talks.\ntools: WebSearch, Task\n"
                                               "model: sonnet\n---\nYou talk.\n")
        chat = self.manager.new_chat("app", mode="repo", agent="helper")
        self.assertEqual((chat["agent"], chat["model"]), ("helper", "sonnet"))
        self.assertEqual(self.manager.new_chat("app", mode="repo", agent=None)["agent"], "coder")
        with self.assertRaisesRegex(ValueError, "has no tools the model can call"):
            self.manager.new_chat("app", mode="repo", agent="talker")
        self.manager.chat_send(chat["id"], "hello")
        self.settle(chat["id"])
        self.assertEqual(self.runners.calls[-1].ctx.agent.name, "helper")

    def test_failed_worktree_creation_leaves_nothing_behind(self) -> None:
        self.config.raw["repos"][0]["base"] = "does-not-exist"
        with self.assertRaises(GitError):
            self.manager.new_chat("app")
        self.assertEqual(list((self.tmp / "data" / "chats").iterdir()), [])


class RepolessChatTest(ChatTestCase):
    """DESIGN §19, §33: a chat without a repository works in a scratch folder, and its agent may only talk."""

    def user_agent(self, name: str, text: str) -> None:
        user_agents = Path(os.environ["HOME"]) / ".config" / "software-factory" / "agents"
        user_agents.mkdir(parents=True, exist_ok=True)
        (user_agents / f"{name}.md").write_text(text)

    def test_a_chat_without_a_repository(self) -> None:
        chat = self.manager.new_chat(None)
        work = self.tmp / "data" / "chats" / chat["id"] / "work"
        self.assertEqual((chat["repo"], chat["repo_path"], chat["mode"], chat["cwd"]), (None, None, "none", str(work)))
        self.assertEqual((chat["branch"], chat["worktree"], chat["base"], chat["agent"]), (None, None, None, "coder"))
        self.assertTrue(work.is_dir())
        self.assertEqual(work.stat().st_mode & 0o777, 0o700)
        self.assertFalse((self.tmp / "data" / "worktrees").exists())
        self.assertEqual(run_git(self.repo, "branch", "--list", "chat/*"), "")
        self.assertEqual(self.manager.new_chat(None, mode="none")["mode"], "none")
        with self.assertRaisesRegex(ValueError, "leave out the repo"):
            self.manager.new_chat("app", mode="none")
        self.manager.chat_send(chat["id"], "hello")
        chat = self.settle(chat["id"])
        self.assertEqual(chat["status"], "idle", chat.get("error"))
        ctx = self.runners.calls[-1].ctx
        self.assertEqual((ctx.cwd, ctx.write_paths, ctx.repo_path), (work, [work], None))
        self.assertIn(work, ctx.read_roots)
        with self.assertRaisesRegex(self.ActionError, "no branch to hand off"):
            self.manager.chat_handoff(chat["id"], "Ship it")
        self.manager.chat_delete(chat["id"])
        self.assertFalse(work.exists())

    def test_its_agent_may_have_no_tools_but_never_wakes(self) -> None:
        self.user_agent("talker", "---\nname: talker\ndescription: Talks.\ntools: none\nmodel: sonnet\n---\nYou talk.\n")
        self.user_agent("seraphina", "---\nname: seraphina\ndescription: Wakes.\ntools: none\nwake: true\n---\nCalm.\n")
        self.user_agent("typo", "---\nname: typo\ndescription: Wakes?\nwake: perhaps\n---\nCalm.\n")
        chat = self.manager.new_chat(None, agent="talker")
        self.assertEqual((chat["agent"], chat["model"]), ("talker", "sonnet"))
        self.manager.chat_send(chat["id"], "hi")
        self.settle(chat["id"])
        self.assertEqual(self.runners.calls[-1].ctx.agent.tools, [])
        with self.assertRaisesRegex(ValueError, "has no tools the model can call"):
            self.manager.new_chat("app", mode="repo", agent="talker")
        for name in ("seraphina", "typo"):
            for repo, mode in ((None, "none"), ("app", "repo")):
                with self.subTest(agent=name, mode=mode), self.assertRaisesRegex(ValueError, "wakes itself"):
                    self.manager.new_chat(repo, mode=mode, agent=name)

    def test_a_repository_s_agents_never_reach_a_chat_without_one(self) -> None:
        agents = self.repo / ".factory" / "agents"
        agents.mkdir(parents=True)
        (agents / "repo-only.md").write_text("---\nname: repo-only\ndescription: x\ntools: Read\n---\nBody.\n")
        self.assertEqual(self.manager.new_chat("app", mode="repo", agent="repo-only")["agent"], "repo-only")
        with self.assertRaisesRegex(ValueError, "no agent named"):
            self.manager.new_chat(None, agent="repo-only")

    def test_its_items_stay_put_or_move_to_the_system_scope(self) -> None:
        chat = self.manager.new_chat(None)
        self.manager.add_item(chat["id"], {"title": "Ask about the dosage chart"})
        with self.assertRaisesRegex(ValueError, "no follow-ups"):
            self.manager.item_action(chat["id"], "C-1", "defer", reason="later")
        carried: list[list[dict]] = []
        self.factory.add_system_items = lambda entries: (carried.append(entries),
                                                         [f"system:S-{n}" for n, _ in enumerate(entries, 1)])[1]
        self.manager.chat_delete(chat["id"])
        self.assertEqual([(e["title"], e["kind"], e["links"]["chat"]) for e in carried[0]],
                         [("Ask about the dosage chart", "followup", chat["id"])])
        self.assertFalse(getattr(self.factory, "followups", []), "nothing goes to a repo's follow-ups")

    def test_a_turn_s_hand_over_names_the_folder_not_git(self) -> None:
        chat = self.manager.new_chat(None)
        prompt = self.manager._handover_prompt(chat, [], {"provider": "a", "model_id": "m"},
                                               {"provider": "b", "model_id": "n"}, "next")
        self.assertIn(f"Your working folder is {chat['cwd']}", prompt)
        self.assertNotIn("git log", prompt)


class ChatTurnTest(ChatTestCase):
    def test_first_message_runs_and_later_ones_resume(self) -> None:
        chat = self.manager.new_chat("app")
        chat_dir = self.tmp / "data" / "chats" / chat["id"]
        text = "Please add a --verbose flag to the CLI and print every step it takes while it runs."
        running = self.manager.chat_send(chat["id"], text)
        self.assertEqual(running["status"], "running")
        self.assertEqual(running["title"], text[:60])
        done = self.settle(chat["id"])
        self.assertEqual((done["status"], done["error"], done["turns"]), ("idle", None, 1))
        self.assertEqual(done["session"], str(chat_dir / "session.json"))
        self.assertEqual(done["usage"], {"input_tokens": 10, "output_tokens": 5, "cached_tokens": 2, "calls": 1})

        first = self.runners.calls[0]
        cwd = Path(chat["cwd"])
        self.assertEqual((first.kind, first.driver, first.prompt), ("run", "builtin", text))
        self.assertEqual((first.ctx.cwd, first.ctx.job_dir, first.ctx.write_paths), (cwd, chat_dir, [cwd]))
        self.assertEqual(first.ctx.read_roots[0], cwd)
        self.assertEqual(first.ctx.session_path, chat_dir / "session.json")
        self.assertEqual(first.ctx.log_path, chat_dir / "logs" / "01-turn.jsonl")
        self.assertEqual((first.ctx.agent.name, first.ctx.model.model), ("coder", "deepseek-v4-pro"))
        self.assertEqual((first.ctx.job["id"], first.ctx.repo_path), (chat["id"], self.repo))

        self.manager.chat_send(chat["id"], "Now add a test for it.")
        done = self.settle(chat["id"])
        second = self.runners.calls[1]
        self.assertEqual((second.kind, second.session_ref, second.prompt),
                         ("resume", str(chat_dir / "session.json"), "Now add a test for it."))
        self.assertEqual(second.ctx.log_path, chat_dir / "logs" / "02-turn.jsonl")
        self.assertEqual((done["title"], done["turns"]), (text[:60], 2))
        self.assertEqual(done["usage"], {"input_tokens": 20, "output_tokens": 10, "cached_tokens": 4, "calls": 2})

    def test_title_collapses_whitespace(self) -> None:
        chat = self.manager.new_chat("app", mode="repo")
        self.assertEqual(self.manager.chat_send(chat["id"], "  fix\n\nthe   bug  ")["title"], "fix the bug")
        self.settle(chat["id"])

    def test_events_are_tagged_with_the_chat_and_logged(self) -> None:
        def handler(call: Call) -> AgentRun:
            call.ctx.emit("agent.text", {"text": "hello"})
            return AgentRun(ok=True, session_ref="s", usage={"calls": 1}, turns=1)

        self.runners.handler = handler
        chat = self.manager.new_chat("app", mode="repo")
        self.published("chat.updated")
        self.manager.chat_send(chat["id"], "hi")
        self.settle(chat["id"])
        events = [e for e in self.bus.since(0) if e.type.startswith("agent.")]
        self.assertEqual([e.type for e in events], ["agent.start", "agent.text", "agent.end"])
        for event in events:
            self.assertEqual((event.repo, event.job, event.agent), ("app", None, "coder"))
            self.assertEqual((event.data["chat"], event.data["run"]), (chat["id"], "01-turn"))
        self.assertEqual(events[0].data["driver"], "builtin")
        self.assertEqual((events[2].data["ok"], events[2].data["turns"]), (True, 1))
        log = self.tmp / "data" / "chats" / chat["id"] / "logs" / "01-turn.jsonl"
        self.assertEqual([json.loads(line)["type"] for line in log.read_text().splitlines()],
                         ["agent.start", "agent.text", "agent.end"])
        statuses: list = []

        def idle_published() -> bool:   # the chat is saved idle just before the event is published
            statuses.extend(e.data["chat"]["status"] for e in self.published("chat.updated"))
            return "idle" in statuses
        wait_for(idle_published, message="the chat.updated event of the idle chat")
        self.assertEqual(statuses, ["running", "idle"])

    def test_send_while_running_is_refused(self) -> None:
        started, release = self.blocking_handler()
        chat = self.manager.new_chat("app")
        self.manager.chat_send(chat["id"], "one")
        self.assertTrue(started.wait(WAIT_SECONDS))
        with self.assertRaises(self.ActionError):
            self.manager.chat_send(chat["id"], "two")
        with self.assertRaises(self.ActionError):
            self.manager.chat_delete(chat["id"])
        with self.assertRaises(self.ActionError):
            self.manager.chat_handoff(chat["id"], "Ship it")
        self.assertEqual(self.manager.chat(chat["id"])["status"], "running")
        release.set()
        self.assertEqual(self.settle(chat["id"])["status"], "idle")
        self.manager.chat_send(chat["id"], "three")
        self.settle(chat["id"])
        self.assertEqual([call.prompt for call in self.runners.calls], ["one", "three"])

    def test_concurrent_sends_start_exactly_one_turn(self) -> None:
        _, release = self.blocking_handler()
        chat = self.manager.new_chat("app", mode="repo")
        outcomes: list[str] = []
        barrier = threading.Barrier(8)

        def send() -> None:
            barrier.wait()
            try:
                self.manager.chat_send(chat["id"], "go")
                outcomes.append("ok")
            except self.ActionError:
                outcomes.append("409")

        threads = [threading.Thread(target=send) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(WAIT_SECONDS)
        self.assertEqual(sorted(outcomes), ["409"] * 7 + ["ok"])
        release.set()
        self.settle(chat["id"])
        self.assertEqual(len(self.runners.calls), 1)

    def test_cancel_stops_the_turn(self) -> None:
        started, _ = self.blocking_handler()
        chat = self.manager.new_chat("app", mode="repo")
        self.assertEqual(self.manager.chat_cancel(chat["id"])["status"], "idle")   # nothing to cancel
        self.manager.chat_send(chat["id"], "long task")
        self.assertTrue(started.wait(WAIT_SECONDS))
        self.manager.chat_cancel(chat["id"])
        done = self.settle(chat["id"])
        self.assertEqual((done["status"], done["error"]), ("idle", "cancelled"))

    def test_failed_turn_sets_error_and_the_next_message_resumes(self) -> None:
        results = [AgentRun(ok=False, error="model call failed: HTTP 500", session_ref="sess"),
                   AgentRun(ok=True, session_ref="sess")]
        self.runners.handler = lambda call: results.pop(0)
        chat = self.manager.new_chat("app", mode="repo")
        self.manager.chat_send(chat["id"], "first")
        failed = self.settle(chat["id"])
        self.assertEqual((failed["status"], failed["error"]), ("error", "model call failed: HTTP 500"))
        self.manager.chat_send(chat["id"], "again")
        self.assertEqual(self.settle(chat["id"])["status"], "idle")
        self.assertEqual([(c.kind, c.session_ref) for c in self.runners.calls], [("run", None), ("resume", "sess")])

    def test_runner_exception_becomes_an_error(self) -> None:
        def handler(call: Call) -> AgentRun:
            raise RuntimeError("boom")

        self.runners.handler = handler
        chat = self.manager.new_chat("app", mode="repo")
        self.manager.chat_send(chat["id"], "x")
        done = self.settle(chat["id"])
        self.assertEqual((done["status"], done["error"]), ("error", "RuntimeError: boom"))
        events = [e for e in self.bus.since(0) if e.type == "agent.end"]
        self.assertEqual(events[-1].data["ok"], False)

    def test_turn_setup_failure_becomes_an_error(self) -> None:
        chat = self.manager.new_chat("app", mode="repo")
        self.config.raw["repos"] = []
        self.manager.chat_send(chat["id"], "x")
        done = self.settle(chat["id"])
        self.assertEqual(done["status"], "error")
        self.assertIn("unknown repo", done["error"])
        self.assertEqual(self.runners.calls, [])

    def test_cli_driver_without_resume_runs_fresh_each_turn(self) -> None:
        config = self.make_config(agents={"*": {"driver": "builtin"}, "coder": {"driver": "qwen"}})
        runners = ScriptedRunners(supports_resume=False)
        manager = ChatManager(config, self.bus, self.factory, runner_factory=runners)
        chat = manager.new_chat("app", mode="repo")
        self.assertEqual(chat["driver"], "qwen")
        for text in ("a", "b"):
            manager.chat_send(chat["id"], text)
            wait_for(lambda: manager.chat(chat["id"])["status"] != "running")
        self.assertEqual([(c.kind, c.driver) for c in runners.calls], [("run", "qwen"), ("run", "qwen")])

    def test_cli_driver_turn_reports_the_model_its_cli_gets(self) -> None:
        """agent.start of a chat turn on a CLI driver names the model the CLI is launched with and the
        driver as provider (runner.launch_model), like a job's runs."""
        from swfactory.drivers.cli import CliRunner

        config = self.make_config(agents={"*": {"driver": "builtin"}, "coder": {"driver": "claude"}},
                                  drivers={"claude": {"models": {"opus": "claude-opus-5-5"}}})
        runners = ScriptedRunners()

        class Labelled(_ScriptedRunner):
            def launch_model(self, ctx: RunContext) -> tuple[str, str]:
                return CliRunner(config, config.driver("claude")).launch_model(ctx)

        manager = ChatManager(config, self.bus, self.factory,
                              runner_factory=lambda cfg, driver: Labelled(runners, driver))
        chat = manager.new_chat("app", mode="repo")
        manager.chat_send(chat["id"], "hi")
        wait_for(lambda: manager.chat(chat["id"])["status"] != "running")
        start = next(e for e in self.bus.since(0) if e.type == "agent.start")
        self.assertEqual((start.data["driver"], start.data["model_id"], start.data["provider"]),
                         ("claude", "claude-opus-5-5", "claude"))

    def test_empty_message_and_unknown_chat(self) -> None:
        chat = self.manager.new_chat("app", mode="repo")
        with self.assertRaises(ValueError):
            self.manager.chat_send(chat["id"], "  ")
        for bad in ("c-0000000000", "../etc", "c-zz"):
            with self.assertRaises(KeyError):
                self.manager.chat_send(bad, "hi")
            with self.assertRaises(KeyError):
                self.manager.chat(bad)

    def test_stop_cancels_running_turns(self) -> None:
        started, _ = self.blocking_handler()
        chat = self.manager.new_chat("app", mode="repo")
        self.manager.chat_send(chat["id"], "x")
        self.assertTrue(started.wait(WAIT_SECONDS))
        self.manager.stop()
        self.assertEqual(self.manager.chat(chat["id"])["error"], "cancelled")


class ChatListTest(ChatTestCase):
    def test_chats_are_listed_most_recent_first(self) -> None:
        first = self.manager.new_chat("app", mode="repo")
        second = self.manager.new_chat("app", mode="repo")
        with mock.patch("swfactory.chats.utc_now", return_value="2999-01-01T00:00:00Z"):
            self.manager.chat_send(first["id"], "bump")
            self.settle(first["id"])
        ids = [chat["id"] for chat in self.manager.chats()]
        self.assertEqual(ids[0], first["id"])
        self.assertEqual(set(ids), {first["id"], second["id"]})
        (self.tmp / "data" / "chats" / second["id"] / "chat.json").write_text("{broken")
        self.assertEqual([chat["id"] for chat in self.manager.chats()], [first["id"]])

    def test_messages_come_from_the_session_transcript(self) -> None:
        chat = self.manager.new_chat("app", mode="repo")
        self.assertEqual(self.manager.chat(chat["id"])["messages"], [])
        session = {"version": 1, "messages": [
            {"role": "user", "content": "add a flag"},
            {"role": "assistant", "content": "Looking.", "tool_calls": [
                {"id": "1", "type": "function", "function": {"name": "Read", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "1", "content": "file text"},
            {"role": "assistant", "content": "Done."}]}
        (self.tmp / "data" / "chats" / chat["id"] / "session.json").write_text(json.dumps(session))
        messages = self.manager.chat(chat["id"])["messages"]
        self.assertEqual([(m["role"], m["text"]) for m in messages],
                         [("user", "add a flag"), ("assistant", "Looking.\n\nDone.")])
        with mock.patch.dict(sys.modules, {"swfactory.harness.loop": None}):
            self.assertEqual(self.manager.chat(chat["id"])["messages"], [])
        (self.tmp / "data" / "chats" / chat["id"] / "session.json").write_text("[1, 2]")
        self.assertEqual(self.manager.chat(chat["id"])["messages"], [])

    def test_restart_marks_running_chats_interrupted(self) -> None:
        chat = self.manager.new_chat("app", mode="repo")
        idle = self.manager.new_chat("app", mode="repo")
        path = self.tmp / "data" / "chats" / chat["id"] / "chat.json"
        path.write_text(json.dumps({**chat, "status": "running"}))
        self.published("chat.updated")
        restarted = ChatManager(self.config, self.bus, self.factory, runner_factory=self.runners)
        restarted.start()
        recovered = restarted.chat(chat["id"])
        self.assertEqual((recovered["status"], recovered["error"]), ("error", INTERRUPTED))
        self.assertEqual(restarted.chat(idle["id"])["status"], "idle")
        self.assertEqual([e.data["chat"]["id"] for e in self.published("chat.updated")], [chat["id"]])
        restarted.chat_send(chat["id"], "carry on")
        wait_for(lambda: restarted.chat(chat["id"])["status"] == "idle", message="the resumed turn")


class ChatDeleteTest(ChatTestCase):
    def test_delete_worktree_chat_removes_worktree_and_branch(self) -> None:
        chat = self.manager.new_chat("app")
        commit_file(Path(chat["worktree"]), "work.txt", "w\n")
        self.manager.chat_delete(chat["id"])
        self.assertFalse(Path(chat["worktree"]).exists())
        self.assertEqual(run_git(self.repo, "branch", "--list", chat["branch"]), "")
        self.assertNotIn(chat["worktree"], run_git(self.repo, "worktree", "list"))
        self.assertFalse((self.tmp / "data" / "chats" / chat["id"]).exists())
        self.assertEqual(self.manager.chats(), [])
        with self.assertRaises(KeyError):
            self.manager.chat(chat["id"])
        self.assertEqual([e.data for e in self.published("chat.removed")], [{"chat": chat["id"]}])

    def test_delete_repo_chat_leaves_the_repo_alone(self) -> None:
        chat = self.manager.new_chat("app", mode="repo")
        head = run_git(self.repo, "rev-parse", "HEAD")
        self.manager.chat_delete(chat["id"])
        self.assertTrue(self.repo.is_dir())
        self.assertEqual(run_git(self.repo, "rev-parse", "HEAD"), head)
        self.assertFalse((self.tmp / "data" / "chats" / chat["id"]).exists())
        with self.assertRaises(KeyError):
            self.manager.chat_delete(chat["id"])

    def test_delete_with_worktree_already_gone(self) -> None:
        chat = self.manager.new_chat("app")
        shutil.rmtree(chat["worktree"])
        self.manager.chat_delete(chat["id"])
        self.assertEqual(run_git(self.repo, "branch", "--list", chat["branch"]), "")


class ChatHandoffTest(ChatTestCase):
    def test_handoff_submits_the_branch_and_records_the_job(self) -> None:
        chat = self.manager.new_chat("app")
        commit_file(Path(chat["worktree"]), "feature.txt", "done\n", message="add feature")
        job = self.manager.chat_handoff(chat["id"], "Add the feature file")
        self.assertEqual(self.factory.submitted[0]["feature"], "Add the feature file")
        self.assertEqual(self.factory.submitted[0]["from_branch"], chat["branch"])
        self.assertEqual(self.manager.chat(chat["id"])["job"], job["id"])
        self.assertEqual(self.published("chat.updated")[-1].data["chat"]["job"], job["id"])

    def test_handoff_names_the_models_the_chat_ran_on(self) -> None:
        """DESIGN §23: the chat's model wrote the branch, so the job's judges must be independent of it; chat.models
        lists every model a turn ran on, and the hand-off passes them as handoff_models."""
        from swfactory.routing import default_family

        chat = self.manager.new_chat("app")
        self.assertEqual(chat["models"], [])
        self.manager.chat_send(chat["id"], "build it")
        done = self.settle(chat["id"])
        ran = self.runners.calls[-1].ctx.model
        entry = {"model": ran.key, "provider": ran.provider.name, "model_id": ran.model,
                 "family": default_family(ran.model)}
        self.assertEqual(done["models"], [entry])
        self.manager.chat_send(chat["id"], "again")
        self.assertEqual(self.settle(chat["id"])["models"], [entry], "each model once")
        commit_file(Path(chat["worktree"]), "feature.txt", "done\n", message="add feature")
        self.manager.chat_handoff(chat["id"], "Ship")
        self.assertEqual(self.factory.submitted[0]["handoff_models"], [entry])

    def test_handoff_of_a_chat_from_before_chat_models(self) -> None:
        chat = self.manager.new_chat("app")
        path = self.tmp / "data" / "chats" / chat["id"] / "chat.json"
        saved = json.loads(path.read_text())
        del saved["models"]
        saved.update(turns=2,
                     session_model={"driver": "builtin", "model": "m1", "provider": "p", "model_id": "qwen3-x"},
                     earlier_sessions=[{"driver": "builtin", "model": "m0", "provider": "p",
                                        "model_id": "claude-x", "file": None}])
        path.write_text(json.dumps(saved))
        commit_file(Path(chat["worktree"]), "feature.txt", "done\n", message="add feature")
        self.manager.chat_handoff(chat["id"], "Ship")
        self.assertEqual(self.factory.submitted[0]["handoff_models"],
                         [{"model": "m0", "provider": "p", "model_id": "claude-x", "family": "anthropic"},
                          {"model": "m1", "provider": "p", "model_id": "qwen3-x", "family": "qwen"}])
        other = self.manager.new_chat("app")
        path = self.tmp / "data" / "chats" / other["id"] / "chat.json"
        saved = json.loads(path.read_text())
        del saved["models"]
        saved["turns"] = 3
        path.write_text(json.dumps(saved))
        commit_file(Path(other["worktree"]), "feature.txt", "done\n", message="add feature")
        self.manager.chat_handoff(other["id"], "Ship")
        self.assertNotIn("handoff_models", self.factory.submitted[1], "turns, but no model recorded: unknown")

    def test_handoff_validation(self) -> None:
        chat = self.manager.new_chat("app")
        with self.assertRaisesRegex(self.ActionError, "no commits ahead"):
            self.manager.chat_handoff(chat["id"], "Ship")
        worktree = Path(chat["worktree"])
        commit_file(worktree, "a.txt", "a\n")
        (worktree / "dirty.txt").write_text("uncommitted\n")
        with self.assertRaisesRegex(self.ActionError, "uncommitted"):
            self.manager.chat_handoff(chat["id"], "Ship")
        (worktree / "dirty.txt").unlink()
        with self.assertRaises(ValueError):
            self.manager.chat_handoff(chat["id"], " ")
        repo_chat = self.manager.new_chat("app", mode="repo")
        with self.assertRaisesRegex(self.ActionError, "worktree-mode"):
            self.manager.chat_handoff(repo_chat["id"], "Ship")
        self.assertEqual(self.factory.submitted, [])

    def test_handoff_delivery_reaches_the_job(self) -> None:
        chat = self.manager.new_chat("app")
        commit_file(Path(chat["worktree"]), "feature.txt", "done\n", message="add feature")
        self.factory.config = self.config
        with self.assertRaisesRegex(ValueError, "delivery must be one of merge, branch, folder"):
            self.manager.chat_handoff(chat["id"], "Ship", delivery="zip")
        self.assertEqual(self.factory.submitted, [])
        self.manager.chat_handoff(chat["id"], "Ship", delivery="folder")
        self.assertEqual(self.factory.submitted[0]["delivery"], "folder")
        self.factory.set_stage(self.factory.submitted[0]["id"], "delivered")
        self.manager.chat_handoff(chat["id"], "Again")   # a delivered job no longer blocks a new handoff
        self.assertEqual(self.factory.submitted[1]["delivery"], "merge", "the config's merge.delivery")

    def test_second_handoff_waits_for_the_first_job(self) -> None:
        chat = self.manager.new_chat("app")
        commit_file(Path(chat["worktree"]), "a.txt", "a\n")
        job = self.manager.chat_handoff(chat["id"], "First")
        with self.assertRaisesRegex(self.ActionError, "already handed off"):
            self.manager.chat_handoff(chat["id"], "Again")
        self.factory.set_stage(job["id"], "rejected")
        second = self.manager.chat_handoff(chat["id"], "Again")
        self.assertEqual(self.manager.chat(chat["id"])["job"], second["id"])
        del self.factory.jobs[("app", second["id"])]
        self.manager.chat_handoff(chat["id"], "Third")
        self.assertEqual(len(self.factory.submitted), 3)


class ChatItemsTest(ChatTestCase):
    """DESIGN §32: a chat's tracked items: the coder's Items calls, the reply sweep, hand-off and delete."""

    def turn(self, chat: dict, text: str, handler: Callable[[Call], AgentRun]) -> dict:
        self.runners.handler = handler
        self.manager.chat_send(chat["id"], text)
        return self.settle(chat["id"])

    def ledger(self, chat: dict) -> dict:
        return {item["id"]: item for item in self.manager.scope_items(chat["id"])["items"]}

    def test_coder_items_calls_persist_and_the_reply_is_swept(self) -> None:
        chat = self.manager.new_chat("app")

        def coder(call: Call) -> AgentRun:
            self.assertIsNotNone(call.ctx.items)
            self.assertFalse(call.ctx.items.stop_guard, "no stop guard in a chat: a person is present")
            call.ctx.items.call({"action": "add", "title": "Check the other callers of parse_rate()"})
            return AgentRun(ok=True, final_text="Fixed parse_rate(). I'll update the docs once the API settles.",
                            turns=1, session_ref=str(call.ctx.session_path))

        chat = self.turn(chat, "Fix parse_rate", coder)
        items = self.ledger(chat)
        self.assertEqual([(i["id"], i["status"], i["owner"], i["verifier"]) for i in items.values()],
                         [("C-1", "open", "coder", "human"), ("C-2", "proposed", "coder", "human")])
        self.assertEqual(items["C-2"]["source"]["type"], "sweep")
        self.assertEqual(items["C-2"]["waiting"], "suggested")
        self.assertEqual(chat["items"]["total"], 2)
        updated = [e for e in self.published("items.updated") if e.data.get("chat") == chat["id"]]
        self.assertTrue(updated and updated[-1].data["ref"] == f"chat:{chat['id']}")
        self.assertEqual([row["ref"] for row in self.manager.item_rows("app", "2026-10-21T00:00:00Z")],
                         [f"chat:{chat['id']}/C-1", f"chat:{chat['id']}/C-2"])
        before = self.ledger(chat)
        self.turn(chat, "thanks", lambda call: AgentRun(ok=True, final_text="Sure.", turns=1,
                                                       session_ref=str(call.ctx.session_path)))
        self.assertEqual(self.ledger(chat).keys(), before.keys(), "the journal is applied once")

    def test_a_reply_s_suggestions_name_its_turn(self) -> None:
        chat = self.manager.new_chat("app")
        session = self.tmp / "data" / "chats" / chat["id"] / "session.json"

        def coder(call: Call) -> AgentRun:
            messages = json.loads(session.read_text())["messages"] if session.exists() else []
            messages += [{"role": "user", "content": call.prompt},
                         {"role": "assistant", "content": f"Reply {len(messages) // 2 + 1}."}]
            session.write_text(json.dumps({"version": 1, "messages": messages}))
            return AgentRun(ok=True, final_text=f"Reply {len(messages) // 2}. The docs are left for later.",
                            turns=1, session_ref=str(session))

        self.turn(chat, "Fix parse_rate", lambda call: AgentRun(ok=True, final_text="Looking.", turns=1))
        self.turn(chat, "first", coder)
        self.turn(chat, "second", lambda call: (coder(call), AgentRun(
            ok=True, final_text="Also the tests are not run yet.", turns=1, session_ref=str(session)))[1])
        sources = [(i["source"]["turn"], i["source"]["run"]) for i in self.ledger(chat).values()]
        self.assertEqual(sources, [(1, "02-turn"), (2, "03-turn")], "the transcript's turn, the turn log's stem")
        messages = self.manager.chat(chat["id"])["messages"]
        self.assertEqual([(m["role"], m["turn"]) for m in messages],
                         [("user", 1), ("assistant", 1), ("user", 2), ("assistant", 2)])
        history = self.manager.item_history(chat["id"], "C-1")
        self.assertEqual((history["item"]["ref"], [e["to"] for e in history["log"]]),
                         (f"chat:{chat['id']}/C-1", ["proposed"]))

    def test_a_person_adds_verifies_and_dismisses(self) -> None:
        chat = self.manager.new_chat("app")
        added = self.manager.add_item(chat["id"], {"title": "Remember the README"})
        self.assertEqual((added["id"], added["owner"], added["verifier"]), ("C-1", "coder", "human"))
        self.assertEqual(self.manager.item_action(chat["id"], "C-1", "verify", note="", reason="")["item"]["status"],
                         "verified")
        self.assertEqual(self.manager.chat(chat["id"])["items"]["verified"], 1)
        with self.assertRaises(KeyError):
            self.manager.item_action(chat["id"], "C-9", "verify")
        with self.assertRaises(ValueError):
            self.manager.item_action(chat["id"], "C-1", "make-job")

    def test_hand_off_moves_open_items_to_the_job(self) -> None:
        chat = self.manager.new_chat("app")
        self.manager.add_item(chat["id"], {"title": "Handle the empty list", "blocking": True})
        self.manager.add_item(chat["id"], {"title": "Done already"})
        self.manager.item_action(chat["id"], "C-2", "verify")
        commit_file(Path(chat["worktree"]), "feature.txt", "done\n", message="add feature")
        job = self.manager.chat_handoff(chat["id"], "Add the feature file")
        self.assertEqual(self.factory.submitted[0]["feature"],
                         f"Add the feature file\n\nOpen items from chat {chat['id']}: see items.md")
        repo, job_id, entries = self.factory.imported[0]
        self.assertEqual((repo, job_id), ("app", job["id"]))
        self.assertEqual([(e["title"], e["owner"], e["verifier"], e["blocking"], e["links"]) for e in entries],
                         [("Handle the empty list", "builder", "approver", True, {"chat": chat["id"]})])
        self.assertEqual(len(self.factory.imported), 1, "the items go with Factory.submit, before the job is queued")
        moved = self.ledger(chat)["C-1"]
        self.assertEqual((moved["status"], moved["links"]["job"], moved["reason"]),
                         ("deferred", job["id"], f"handed off to job {job['id']}"))
        self.assertEqual(self.ledger(chat)["C-2"]["status"], "verified", "closed items stay")

    def test_delete_carries_open_items_to_follow_ups_first(self) -> None:
        chat = self.manager.new_chat("app")
        self.manager.add_item(chat["id"], {"title": "Re-check the import command"})
        self.manager.chat_delete(chat["id"])
        self.assertEqual([(e["title"], e["kind"], e["owner"], e["links"]["chat"]) for e in self.factory.followups],
                         [("Re-check the import command", "followup", "human", chat["id"])])
        self.assertEqual(self.factory.followups[0]["source"]["reason"], "chat deleted")
        self.assertRegex(self.factory.followups[0]["due"], r"^\d{4}-\d\d-\d\d$", "an open item keeps a due date")

    def test_defer_moves_one_item_to_follow_ups(self) -> None:
        chat = self.manager.new_chat("app")
        self.manager.add_item(chat["id"], {"title": "Later thing"})
        result = self.manager.item_action(chat["id"], "C-1", "defer", reason="next week", due="2026-11-30")
        self.assertEqual((result["item"]["status"], result["item"]["links"]["followup"]), ("deferred", "repo:app/F-1"))
        self.assertEqual(self.factory.followups[0]["due"], "2026-11-30")

    def test_hand_off_leaves_a_pending_wake_s_item_in_the_chat(self) -> None:
        from swfactory import items as tracker
        from swfactory.chats import JOURNAL_FILE

        chat = self.manager.new_chat("app")
        self.manager.add_item(chat["id"], {"title": "Handle the empty list"})
        ctx = tracker.ItemsContext(self.manager._items_store(chat), "coder",
                                   Path(chat["cwd"]).parent / JOURNAL_FILE, stop_guard=False)
        wake = ctx.open_wake("Check CI on the branch", "2026-10-21T14:05:00Z", "W-000231")
        commit_file(Path(chat["worktree"]), "feature.txt", "done\n", message="add feature")
        self.manager.chat_handoff(chat["id"], "Add the feature file")
        _, _, entries = self.factory.imported[0]
        self.assertEqual([e["title"] for e in entries], ["Handle the empty list"],
                         "a wake fires into the chat (Wake & Messages §5.8): its item is not the job's")
        self.assertEqual(self.ledger(chat)[wake]["status"], "open")
        ctx.close_wake(wake, "05-turn")
        closed = self.ledger(chat)[wake]
        self.assertEqual((closed["status"], closed["verified_by"], closed["evidence"][-1]["ref"]),
                         ("verified", "code", "05-turn"))

    def test_the_agent_s_own_defer_and_a_person_s_update_reach_the_follow_ups(self) -> None:
        chat = self.manager.new_chat("app")

        def coder(call: Call) -> AgentRun:
            call.ctx.items.call({"action": "add", "title": "Ask about the copy"})
            call.ctx.items.call({"action": "update", "id": "C-1", "to": "defer", "reason": "the owner decides"})
            return AgentRun(ok=True, final_text="Done.", turns=1, session_ref=str(call.ctx.session_path))

        chat = self.turn(chat, "Fix the footer", coder)
        moved = self.ledger(chat)["C-1"]
        self.assertEqual((moved["status"], moved["links"]["followup"]), ("deferred", "repo:app/F-1"))
        self.assertEqual([(e["title"], e["status"], e["links"]["chat"], e["source"]["reason"])
                          for e in self.factory.followups],
                         [("Ask about the copy", "open", chat["id"], "the owner decides")])
        self.manager.add_item(chat["id"], {"title": "Check the print stylesheet"})
        updated = self.manager.item_action(chat["id"], "C-2", "update", to="defer", reason="next week")["item"]
        self.assertEqual((updated["status"], updated["links"]["followup"]), ("deferred", "repo:app/F-2"))
        with self.manager._items_store(chat).transaction() as ledger:   # Later in place, as an older chat left it
            old = ledger.add("human", "An old Later item", owner="coder", verifier="human")
            ledger.change("coder", old["id"], "deferred", reason="some day")
        self.manager.chat_delete(chat["id"])
        self.assertEqual([e["title"] for e in self.factory.followups],
                         ["Ask about the copy", "Check the print stylesheet", "An old Later item"],
                         "each moved once; delete carries only what never moved on")

    def test_a_chat_without_a_repository_keeps_the_agent_s_items_open(self) -> None:
        """§15 and §32 together: Later means moved to the repo's follow-ups, so where there is no repository
        the agent's own defer is refused as a person's is; the item stays open until the chat is deleted."""
        chat = self.manager.new_chat(None)
        heard: list[str] = []

        def coder(call: Call) -> AgentRun:
            call.ctx.items.call({"action": "add", "title": "Ask about the copy"})
            try:
                call.ctx.items.call({"action": "update", "id": "C-1", "to": "defer", "reason": "the owner decides"})
            except ValueError as exc:
                heard.append(str(exc))
            return AgentRun(ok=True, final_text="Done.", turns=1, session_ref=str(call.ctx.session_path))

        chat = self.turn(chat, "Draft the note", coder)
        self.assertEqual(len(heard), 1)
        self.assertIn("has no follow-ups", heard[0])
        item = self.ledger(chat)["C-1"]
        self.assertEqual((item["status"], item["links"].get("followup")), ("open", None))
        self.assertFalse(getattr(self.factory, "followups", []), "nothing goes to a repo's follow-ups")
        with self.assertRaisesRegex(ValueError, "has no follow-ups"):
            self.manager.item_action(chat["id"], "C-1", "update", to="defer", reason="next week")
        carried: list[list[dict]] = []
        self.factory.add_system_items = lambda entries: (carried.append(entries),
                                                         [f"system:S-{n}" for n, _ in enumerate(entries, 1)])[1]
        self.manager.chat_delete(chat["id"])
        self.assertEqual([e["title"] for e in carried[0]], ["Ask about the copy"])


class HelperTest(ManagerTestCase):
    def test_next_log_path_numbers_runs(self) -> None:
        logs = self.tmp / "logs"
        self.assertEqual(next_log_path(logs, "turn").name, "01-turn.jsonl")
        (logs / "07-lead.jsonl").write_text("")
        self.assertEqual(next_log_path(logs, "turn").name, "08-turn.jsonl")

    def test_agent_route_override_and_extra_body(self) -> None:
        config = self.make_config(agents={"*": {"driver": "builtin"},
                                          "lead": {"model": "sonnet", "extra_body": {"a": {"b": 1}}}})
        lead = load_agent(config, self.repo, "lead")
        self.assertEqual(agent_route(config, lead), Route("builtin", "sonnet", {"a": {"b": 1}}))
        override = agent_route(config, lead, {"model": "opus", "driver": "claude", "extra_body": {"c": 2}})
        self.assertEqual(override, Route("claude", "opus", {"c": 2}))
        model = agent_route(config, lead, {"extra_body": {"a": {"d": 3}}}).model_config(config)
        self.assertEqual((model.model, model.extra_body["a"]), ("deepseek-v4-flash", {"b": 1, "d": 3}))
        self.assertIn("thinking", model.extra_body)
        with self.assertRaises(ValueError):
            agent_route(config, lead, {"driver": "nope"})
        with self.assertRaises(ValueError):
            agent_route(config, lead, {"extra_body": [1]})


if __name__ == "__main__":
    unittest.main()


class ChatWakeTurnTest(ChatTestCase):
    """Chat check-ins (Wake & Messages §5.8): the WakeManager fires chat_send(origin="wake"), the turn is a smaller
    read-only run, wake_chain counts the links, and the turn's end closes its item and tells the WakeManager."""

    def setUp(self) -> None:
        super().setUp()
        self.contexts: list[tuple[str, int, bool]] = []
        self.done: list[tuple] = []

        def wake_chat_context(chat_id: str, chain: int, *, checkin: bool) -> Any:
            self.contexts.append((chat_id, chain, checkin))
            return types.SimpleNamespace(chat_id=chat_id, chain=chain, checkin=checkin)

        self.factory.wake_chat_context = wake_chat_context
        self.factory.wake_chat_done = lambda *args: self.done.append(args)

    def test_a_wake_turn_is_a_read_only_checkin_and_the_chain_counts(self) -> None:
        chat = self.manager.new_chat("app")
        self.manager.chat_send(chat["id"], "Ship the fix and watch CI.")
        first = self.settle(chat["id"])
        self.assertEqual(first.get("wake_chain"), 0)
        user_call = self.runners.calls[0]
        self.assertFalse(user_call.ctx.checkin)
        self.assertEqual(self.contexts[-1], (chat["id"], 0, False), "an ordinary turn gets the Wake tool's context")
        self.assertIsNotNone(user_call.ctx.wake)
        with self.assertRaises(ValueError):
            self.manager.chat_send(chat["id"], "x", origin="cron")
        for link in (1, 2):
            self.manager.chat_send(chat["id"], "[Scheduled check-in] look at CI", origin="wake", wake_id=f"W-00000{link}")
            done = self.settle(chat["id"])
            self.assertEqual(done["wake_chain"], link)
            self.assertEqual(done["title"], first["title"], "a check-in never retitles the chat")
        call = self.runners.calls[-1]
        cwd = Path(chat["cwd"])
        self.assertEqual((call.ctx.checkin, call.ctx.background, call.ctx.message_origin), (True, True, "wake"))
        self.assertEqual(call.ctx.write_paths, [], "a check-in writes nothing")
        self.assertEqual(call.ctx.read_roots[0], cwd)
        self.assertIn(["gh", "run", "view"], call.ctx.checkin_commands)
        self.assertEqual((call.ctx.limits.max_turns, call.ctx.max_output_tokens), (30, 20000))
        self.assertEqual(self.contexts[-1], (chat["id"], 2, True))
        self.assertEqual([d[:2] + d[3:] for d in self.done], [(chat["id"], "W-000001", True),
                                                             (chat["id"], "W-000002", True)])
        self.manager.chat_send(chat["id"], "Thanks, now tidy up.")
        self.assertEqual(self.settle(chat["id"])["wake_chain"], 0, "a person's message resets the chain")

    def test_the_end_of_a_checkin_closes_its_item_through_the_chats_items(self) -> None:
        chat = self.manager.new_chat("app")
        opened: list[str] = []

        def coder(call: Call) -> AgentRun:
            if not call.ctx.checkin:
                opened.append(call.ctx.items.open_wake("Scheduled check-in W-000007", "2026-10-21T15:00:00+01:00",
                                                       "W-000007", taint=("untrusted",)))
            return AgentRun(ok=True, final_text="ok", turns=1, session_ref=str(call.ctx.session_path))

        self.runners.handler = coder
        self.manager.chat_send(chat["id"], "Watch CI for me.")
        self.settle(chat["id"])
        item = opened[0]
        self.manager.chat_send(chat["id"], "[Scheduled check-in] CI", origin="wake", wake_id="W-000007", wake_item=item)
        self.settle(chat["id"])
        row = next(i for i in self.manager.scope_items(chat["id"])["items"] if i["id"] == item)
        self.assertEqual(row["status"], "verified")
        self.assertEqual(self.done[-1][1], "W-000007")

    def test_a_failed_checkin_leaves_its_item_open_and_says_so(self) -> None:
        chat = self.manager.new_chat("app")
        opened: list[str] = []

        def coder(call: Call) -> AgentRun:
            if not call.ctx.checkin:
                opened.append(call.ctx.items.open_wake("Scheduled check-in W-000008", "2026-10-21T15:00:00+01:00",
                                                       "W-000008"))
                return AgentRun(ok=True, final_text="ok", turns=1, session_ref=str(call.ctx.session_path))
            return AgentRun(ok=False, error="the model stayed busy: a check-in waits for people")

        self.runners.handler = coder
        self.manager.chat_send(chat["id"], "Watch CI for me.")
        self.settle(chat["id"])
        self.manager.chat_send(chat["id"], "[Scheduled check-in] CI", origin="wake", wake_id="W-000008",
                               wake_item=opened[0])
        self.settle(chat["id"])
        row = next(i for i in self.manager.scope_items(chat["id"])["items"] if i["id"] == opened[0])
        self.assertEqual(row["status"], "open", "a promised check-in that did not happen stays in sight")
        self.assertEqual(self.done[-1][1:2] + self.done[-1][3:], ("W-000008", False))

    def test_a_checkin_s_commands_follow_the_setting_and_an_empty_list_gives_none(self) -> None:
        """wakes.chat_wakes.commands: [] gives the check-in no RunCheck (an empty list); missing or null keeps the
        default list, the same rule as a persona's wake.commands."""
        from swfactory.wakes import CHAT_WAKE_DEFAULTS

        chat = self.manager.new_chat("app")
        self.manager.chat_send(chat["id"], "Watch CI.")
        self.settle(chat["id"])
        wakes = self.manager.config.raw.setdefault("wakes", {})
        cases = (([], []), (None, CHAT_WAKE_DEFAULTS["commands"]), ([["git", "status"]], [["git", "status"]]))
        for number, (value, expected) in enumerate(cases, start=1):
            wakes["chat_wakes"] = {"enabled": True, "commands": value}
            self.manager.chat_send(chat["id"], "[Scheduled check-in] CI", origin="wake", wake_id=f"W-00002{number}")
            self.settle(chat["id"])
            self.assertEqual(self.runners.calls[-1].ctx.checkin_commands, expected, value)

    def test_without_the_factory_context_there_is_no_wake_tool(self) -> None:
        del self.factory.wake_chat_context
        chat = self.manager.new_chat("app")
        self.manager.chat_send(chat["id"], "hi")
        self.settle(chat["id"])
        self.assertIsNone(self.runners.calls[0].ctx.wake)

    def test_a_checkin_whose_context_is_refused_does_not_run(self) -> None:
        """S46: the switches no longer hold when the check-in turn starts (bwrap turned off meanwhile): no run at
        all, so RunCheck never runs unsandboxed; the WakeManager hears it failed."""
        self.factory.wake_chat_context = lambda chat_id, chain, *, checkin: None
        chat = self.manager.new_chat("app")
        self.manager.chat_send(chat["id"], "[Scheduled check-in] CI", origin="wake", wake_id="W-000009")
        ended = self.settle(chat["id"])
        self.assertEqual(self.runners.calls, [], "the check-in never ran")
        self.assertIn("check-ins are off for this chat now", ended["error"])
        self.assertEqual(self.done[-1][1:2] + self.done[-1][3:], ("W-000009", False))
