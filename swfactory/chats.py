"""Chat: conversations with one agent, on a repo or without one, beside the factory line (DESIGN.md §19, §33).

A chat lives in ``<data_dir>/chats/<chat-id>/``: ``chat.json`` (its state, written
atomically and only by ChatManager), ``session.json`` (the builtin agent session, so
a chat resumes across restarts), ``sessions/NN.json`` (the sessions of models a turn
moved away from: a session never moves between models, DESIGN §23) and
``logs/NN-turn.jsonl`` (each turn's events). It
works on its own branch ``chat/<chat-id>`` in a worktree (mode ``worktree``),
directly in the repo checkout (mode ``repo``), or without a repository in the scratch
folder ``work/`` there (mode ``none``: no branch, no worktree, no hand-off, and the
agent may have no tools), one turn at a time, each in a thread. A persona's chat (an
agent file bound to an Open WebUI workspace model, or an imported snapshot of one;
personas.py, DESIGN §33) is always of the last kind: chat.json ``persona`` keeps its
binding and which version of the preset answered each turn, every turn re-reads the
binding and runs on this machine only, and its system prompt is a thin note.

Its tracked items (DESIGN.md section 32) are ``items.json`` and ``items.log.jsonl``
there, also written only by ChatManager: the agent's Items tool calls (journal
``items.journal.jsonl``) and the sweep of each reply go in after the turn; the person
is the verifier; a suggestion from a reply records that reply's turn number (the
``turn`` of each transcript entry). A hand-off moves the open items to the new job,
and deleting a chat moves them to the repo's follow-ups first.

The helpers above ChatManager run one standalone agent (route, RunContext, events,
logs, atomic JSON) and are shared with plans.py.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from . import gitops
from . import items as tracker
from .agentdefs import AgentDef, discover_agents
from .config import agent_dirs, deep_merge, expand_path
from .events import utc_now
from .routing import (REACHABILITY, Reachability, add_cost, effort_body, merged_placements, place_live, placement_reason,
                      price_table, profile_name, profile_routes, reason, run_cost, wait_live, zero_cost)
from .runners import AgentRun, RunContext, Runner, add_usage, launch_model, make_runner
from .skills import skill_dirs

if TYPE_CHECKING:
    from .config import Config, ModelConfig
    from .events import EventBus

RunnerFactory = Callable[["Config", str], Runner]
Emit = Callable[[str, dict], None]

ACTIVE_JOB_STAGES = frozenset({"queued", "spec", "build", "review", "approve", "merging"})
INTERRUPTED = "interrupted (server restarted)"
CHAT_ID_RE = re.compile(r"^c-[0-9a-f]{10}$")
CHAT_MODES = ("worktree", "repo", "none")
CHAT_FILE = "chat.json"
WORK_DIR = "work"          # <chat>/work: where a chat without a repository works (mode "none")
SESSION_FILE = "session.json"
JOURNAL_FILE = "items.journal.jsonl"
TITLE_CHARS = 60
EARLIER_DIR = "sessions"   # <chat>/sessions/NN.json: the sessions of models the chat moved away from
HANDOVER_CHARS = 12000     # the end of the conversation a hand-over prompt quotes
_LOG_NUMBER_RE = re.compile(r"^(\d+)-.+\.jsonl$")
_EARLIER_FILE_RE = re.compile(r"^sessions/\d{2,}\.json$")


class FactoryApi(Protocol):
    """The part of orchestrator.Factory that chats and plans call."""

    def submit(self, repo: str, feature: str, from_branch: str | None = None,
               routing: Any = None, delivery: str | None = None, profile: str | None = None,
               items: list[dict] | None = None) -> dict: ...

    def job(self, repo: str, job_id: str) -> dict: ...

    def cancel(self, repo: str, job_id: str) -> dict: ...

    def check_routing(self, repo: str, routing: Any = None, profile: str | None = None) -> dict: ...

    def check_profile(self, profile: str | None = None, routing: Any = None) -> str | None: ...

    def check_delivery(self, delivery: str | None = None) -> str: ...

    def add_followups(self, repo: str, entries: list[dict]) -> list[str]: ...

    def defer_to_followups(self, repo: str, ledger: Any, item_id: str, reason: str, due: str | None = None,
                           by: str = "human") -> str: ...


# ---------------------------------------------------------------- running one standalone agent


def action_error(message: str) -> Exception:
    """An orchestrator.ActionError (HTTP 409), imported late because the orchestrator imports this module."""
    from .orchestrator import ActionError

    return ActionError(message)


def zero_usage() -> dict:
    """A fresh usage counter."""
    return {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "calls": 0}


def write_atomic(path: Path, text: str) -> None:
    """Write text to path atomically (temporary file in the same dir, fsync, os.replace)."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def write_json(path: Path, data: dict) -> None:
    """Write data to path as indented JSON, atomically."""
    write_atomic(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def read_json(path: Path) -> dict:
    """The JSON object stored in path (FileNotFoundError if missing, ValueError if malformed)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: not a JSON object")
    return data


def next_log_path(logs: Path, name: str) -> Path:
    """Create and return ``<logs>/NN-<name>.jsonl``, NN one past the highest run number there."""
    logs.mkdir(parents=True, exist_ok=True)
    numbers = [int(m.group(1)) for m in map(_LOG_NUMBER_RE.fullmatch, os.listdir(logs)) if m]
    number = max(numbers, default=0) + 1
    while True:
        path = logs / f"{number:02d}-{name}.jsonl"
        try:
            with open(path, "x", encoding="utf-8"):
                return path
        except FileExistsError:
            number += 1


def load_agent(config: Config, repo_path: Path | None, name: str) -> AgentDef:
    """The agent called name, discovered like the factory's agents (project, user, then repo dirs; without a
    repo_path, the project and user dirs only)."""
    agent = discover_agents(agent_dirs(config, repo_path)).get(name)
    if agent is None:
        raise ValueError(f"no agent named {name!r}")
    return agent


@dataclass
class Route:
    """Where an agent runs: a driver name, a model reference, extra_body merged over the model's, and
    why it runs there ({kind, text}, routing.reason; None for none)."""

    driver: str
    model: str
    extra_body: dict = field(default_factory=dict)
    reason: dict | None = None

    def model_config(self, config: Config) -> ModelConfig:
        """The resolved model, with this route's extra_body deep-merged over the model entry's."""
        model = config.model(self.model)
        model.extra_body = deep_merge(model.extra_body, self.extra_body)
        return model


def agent_route(config: Config, agent: AgentDef, override: dict | None = None) -> Route:
    """The agent's route: override's driver/model/extra_body/effort, else config (agents.<name>, agents.*,
    file tier).

    ``agents.<name>`` extra_body, effort and reason apply only when the model comes from config; the
    override's apply on top (an effort as the extra_body routing.effort_body gives the model's
    provider type). ValueError for an unknown driver, a bad model or an effort the provider does
    not take.
    """
    override = override or {}
    extra = override.get("extra_body") or {}
    if not isinstance(extra, dict):
        raise ValueError(f"routing for {agent.name}: extra_body must be an object")
    try:
        driver = str(override.get("driver") or config.agent_driver(agent.name))
        config.driver(driver)
        if override.get("model"):
            model, configured, own = str(override["model"]), {}, {}
            resolved = config.model(model)
        else:
            resolved = config.agent_model(agent)
            model, configured, own = resolved.key, _configured_extra_body(config, agent.name), _configured(config, agent.name)
        effort = override.get("effort") or own.get("effort")
    except KeyError as exc:
        raise ValueError(exc.args[0] if exc.args else str(exc)) from None
    found = reason(override["reason"] if "reason" in override else own.get("reason"), f"routing for {agent.name}.reason")
    return Route(driver, model, deep_merge(deep_merge(configured, effort_body(resolved.provider.type, effort)), extra),
                 found)


def _configured(config: Config, agent_name: str) -> dict:
    agents = config.raw.get("agents")
    entry = agents.get(agent_name) if isinstance(agents, dict) else None
    return entry if isinstance(entry, dict) else {}


def _configured_extra_body(config: Config, agent_name: str) -> dict:
    extra = _configured(config, agent_name).get("extra_body")
    return dict(extra) if isinstance(extra, dict) else {}


def placed_model(config: Config, route: Route, reach: Reachability | None = None) -> tuple[ModelConfig, dict, list[str]]:
    """(the model a standalone run of route runs on, placement info for agent.start, notes): the route's
    model on the first of its builds that answers, else its overflow (routing.place_live); a CLI
    driver's model as it is. Chats and the lead place on the live config."""
    model = route.model_config(config)
    return place_live(config, model, reach or REACHABILITY, _is_cli(config, route.driver))


def _is_cli(config: Config, driver: str) -> bool:
    try:
        return config.driver(driver).type == "cli"
    except (KeyError, ValueError):
        return False


def place_run(config: Config, route: Route, reach: Reachability | None, cancel: threading.Event,
              emit: Emit) -> tuple[ModelConfig, dict, list[str], Callable[[], None]] | None:
    """Where a chat turn or lead run of route goes, as a job's run is placed: placed_model, then the slot
    wait (routing.wait_live: agent.waiting {provider, overflow_after_seconds} when the slot is busy,
    the overflow past overflow_after_seconds). Returns (the model, placement info for agent.start with
    the placement's reason, notes, the slot's release: call it when the run ends), or None when cancel
    was set while waiting."""
    reach = reach or REACHABILITY
    cli = _is_cli(config, route.driver)
    base = route.model_config(config)
    model, info, notes = place_live(config, base, reach, cli)
    provider = model.provider
    waited = wait_live(config, base, model, info, cancel, lambda: emit("agent.waiting", {
        "provider": provider.name, "overflow_after_seconds": provider.overflow_after_seconds}), reach, cli)
    if waited is None:
        return None
    model, info, more, release = waited
    found = placement_reason(route.reason, info)
    return model, {**info, **({"reason": found} if found else {})}, notes + more, release


def event_emitter(bus: EventBus, repo: str, agent: str, log_path: Path, **labels: str) -> Emit:
    """emit(type, data) for one run: publishes with job=None, adds labels and data.run (the log stem),
    and appends the event to log_path."""
    tags = {**labels, "run": log_path.stem}

    def emit(event_type: str, data: dict) -> None:
        bus.publish(event_type, {**(data or {}), **tags}, repo=repo, agent=agent, log_path=log_path)

    return emit


def agent_context(config: Config, repo_path: Path, agent: AgentDef, model: ModelConfig, *, job: dict,
                  cwd: Path, job_dir: Path, write_paths: list[Path], read_roots: list[Path],
                  log_path: Path, emit: Emit, cancel: threading.Event, session_path: Path,
                  items: Any = None) -> RunContext:
    """A RunContext with what every run gets, as the orchestrator builds it: limits, MCP servers,
    sandbox, secrets, and the skill dirs and harness.extra_read_roots added to read_roots; items is
    the run's ItemsContext (section 32), if any."""
    harness = config.raw.get("harness")
    harness = harness if isinstance(harness, dict) else {}
    dirs = skill_dirs(config, repo_path, cwd)
    extra_roots = [expand_path(root, config.path.parent) for root in harness.get("extra_read_roots") or []]
    return RunContext(
        job=job, agent=agent, cwd=cwd, job_dir=job_dir, write_paths=write_paths,
        read_roots=[*read_roots, *dirs, *extra_roots], log_path=log_path, emit=emit, cancel=cancel,
        model=model, limits=config.limits(), session_path=session_path, repo_path=repo_path,
        skill_dirs=dirs, mcp_servers=config.mcp_servers(),
        sandbox=str(harness.get("sandbox") or "none"),
        sandbox_network=harness.get("sandbox_network") is not False, secrets=config.secret_values(), items=items,
    )


def run_agent(runner_factory: RunnerFactory, config: Config, route: Route, ctx: RunContext, prompt: str,
              session_ref: str | None = None, labels: dict | None = None) -> AgentRun:
    """One run between agent.start and agent.end events (shaped like the orchestrator's): a resume of
    session_ref when there is one and the runner can resume, else a fresh run. An exception becomes a
    failed AgentRun. labels (placement info) go into agent.start; agent.end carries the run's cost_usd
    and priced (run_cost_of)."""
    started = time.monotonic()
    runner: Runner | None = None
    failure = AgentRun(ok=False)
    try:
        runner = runner_factory(config, route.driver)
    except Exception as exc:  # an unknown driver or a broken CLI preset
        failure.error = f"{type(exc).__name__}: {exc}"
    resume = runner is not None and bool(session_ref) and runner.supports_resume
    model_id, provider = launch_model(runner, ctx, (ctx.model.model, ctx.model.provider.name))
    ctx.emit("agent.start", {"agent": ctx.agent.name, "driver": route.driver, "model": ctx.model.key,
                             "model_id": model_id, "provider": provider, "resume": resume, **(labels or {})})
    result = failure
    if runner is not None:
        try:
            result = runner.resume(ctx, str(session_ref), prompt) if resume else runner.run(ctx, prompt)
        except Exception as exc:  # a runner bug must not kill the thread or leave state "running"
            result = AgentRun(ok=False, error=f"{type(exc).__name__}: {exc}")
    cost, priced = run_cost_of(ctx, result)
    ctx.emit("agent.end", {"ok": result.ok, "error": result.error, "usage": result.usage,
                           "turns": result.turns, "seconds": round(time.monotonic() - started, 3),
                           "cost_usd": cost, "priced": priced})
    return result


def run_cost_of(ctx: RunContext, result: AgentRun) -> tuple[float | None, bool]:
    """(cost in USD, priced) of a run on ctx.model (routing.run_cost; unknown prices: (None, False))."""
    return run_cost(result.usage, price_table(ctx.model))


# ---------------------------------------------------------------- chats


BOOKKEEPING_TOOLS = frozenset({"items", "wake"})   # the harness's own tools: they do no work in the repo


def _has_callable_tools(agent: AgentDef) -> bool:
    """Whether the builtin harness gives the agent any tool that works: a builtin one it lists, or an MCP
    server. The bookkeeping tools (Items, which every run with tracked items gets anyway, and Wake) do not
    count: an agent with only those could never do what a chat asks."""
    from .harness.tools import BUILTIN_TOOLS

    known = {tool.lower() for tool in BUILTIN_TOOLS} - BOOKKEEPING_TOOLS
    return bool(agent.mcp) or any(str(tool).strip().lower() in known for tool in agent.tools)


def _refuse_new_persona(agent: AgentDef, persona_chat: bool = False) -> None:
    """ValueError when a turn would run an agent whose file has since become a persona it may not run as: one
    that wakes itself (wake: true) runs in no chat, and another kind only in a persona chat (on this machine,
    on its own model), so new_chat's refusals hold on every turn too (§19, §33)."""
    from . import personas

    if personas.wakes(agent):
        raise ValueError(f"agent {agent.name!r} is now a persona that wakes itself (wake: true): it talks to you in "
                         f"its own thread, never in a chat; this chat cannot go on with it")
    if not persona_chat and personas.persona_of(agent) is not None:
        raise ValueError(f"agent {agent.name!r} is now a persona: its chats run only as persona chats, on this "
                         f"machine and on its own model; start one (POST /api/chats {{\"persona\": \"{agent.name}\"}})")


def _title(text: str) -> str:
    return " ".join(text.split())[:TITLE_CHARS]


class ChatManager:
    """Chats under ``<data_dir>/chats/``; Factory delegates its chat methods here.

    Call start() once the server starts (restart recovery) and stop() when it stops.
    """

    def __init__(self, config: Config, bus: EventBus, factory: FactoryApi,
                 runner_factory: RunnerFactory = make_runner) -> None:
        self.config = config
        self.bus = bus
        self.factory = factory
        self.runner_factory = runner_factory
        self.root = Path(config.data_dir) / "chats"
        self._lock = threading.Lock()
        self._turns: dict[str, tuple[threading.Thread, threading.Event]] = {}
        self.reach: Reachability = getattr(factory, "reach", None) or REACHABILITY

    def start(self) -> None:
        """Restart recovery: a chat a previous server left "running" becomes "error" (interrupted)."""
        with self._lock:
            for chat in self.chats():
                if chat.get("status") == "running" and chat["id"] not in self._turns:
                    chat.update(status="error", error=INTERRUPTED)
                    self._save(chat)

    def stop(self, timeout: float = 10.0) -> None:
        """Cancel every running turn and wait up to timeout seconds for each to end."""
        with self._lock:
            turns = list(self._turns.values())
        for _, cancel in turns:
            cancel.set()
        for thread, _ in turns:
            thread.join(timeout)

    # -- reading ----------------------------------------------------------------

    def chats(self) -> list[dict]:
        """Every chat.json, most recently updated first (unreadable ones are skipped)."""
        found = []
        if self.root.is_dir():
            for entry in self.root.iterdir():
                if CHAT_ID_RE.fullmatch(entry.name):
                    with contextlib.suppress(OSError, ValueError):
                        found.append(read_json(entry / CHAT_FILE))
        return sorted(found, key=lambda chat: (chat.get("updated_at") or "", chat.get("id") or ""),
                      reverse=True)

    def chat(self, chat_id: str) -> dict:
        """chat.json plus "messages": the conversation's [{role, text, tools, turn}] (KeyError if unknown);
        turn numbers the person's messages from 1, and a reply carries the turn it answers."""
        chat = self._load(chat_id)
        chat["messages"] = self._messages(chat)
        return chat

    # -- actions ----------------------------------------------------------------

    def new_chat(self, repo: str | None, model: str | None = None, mode: str = "worktree",
                 agent: str | None = "coder", profile: str | None = None, persona: str | None = None) -> dict:
        """Create an idle chat with the agent on repo: a new chat/<id> branch and worktree from the
        base (mode "worktree") or the repo checkout itself (mode "repo"); or, with repo None (mode
        "none"), a chat without a repository: it works in the scratch folder <data_dir>/chats/<id>/work
        (0700), has no branch, worktree or hand-off, and its agent comes from the project and user agent
        dirs only. model overrides the route's.

        agent: on a repo, any agent that has tools the model can call (default coder, also e.g. analyst):
        one of the builtin harness's tools or an MCP server; without a repository it may have none (tools:
        none: a plain conversation). An agent marked wake: true (a persona that wakes itself) is refused.
        ValueError if no agent of that name exists or it is refused. profile: the routing profile whose
        placement for the agent applies (None: the config's default_profile; "": none), under model; its
        name and the route's reason are kept in chat.json (ValueError for an unknown profile).

        persona (or an agent that is one): a persona's chat (DESIGN §33), always without a repository. It runs
        on the persona's models entry (its model:, else the one entry for its workspace model) unless model
        or a profile names another, only on this machine, and for an Open WebUI binding only on its workspace
        model; the live binding is read now (GET /api/models) and kept in chat.json["persona"]. ValueError
        when Open WebUI does not answer, refuses the key or does not list the model.
        """
        if persona is not None:
            if agent not in (None, "coder", persona):
                raise ValueError("give either agent or persona, not both")
            if repo is not None:
                raise ValueError("a persona's chat has no repository: leave out the repo")
            agent, mode = persona, "none"
        if repo is None or mode == "none":
            if repo is not None:
                raise ValueError("mode 'none' is a chat without a repository: leave out the repo")
            mode = "none"
        if mode not in CHAT_MODES:
            raise ValueError(f"mode must be one of {', '.join(CHAT_MODES)}")
        agent = agent or "coder"
        repo_cfg = self.config.repo(repo) if repo is not None else None
        agent_def = load_agent(self.config, repo_cfg.path if repo_cfg is not None else None, agent)
        from . import personas

        bound = personas.persona_of(agent_def) if repo_cfg is None else None
        if repo_cfg is not None and personas.persona_of(agent_def) is not None and not personas.wakes(agent_def):
            raise ValueError(f"{agent!r} is a persona: its chats have no repository (POST /api/chats "
                             f"{{\"persona\": \"{agent}\"}})")
        if persona is not None and bound is None:
            raise ValueError(f"agent {agent!r} is not a persona (its file has no openwebui: binding)")
        if personas.wakes(agent_def):
            raise ValueError(f"agent {agent!r} is a persona that wakes itself (wake: true): it talks to you in its "
                             f"own thread, never in a chat")
        if mode != "none" and not _has_callable_tools(agent_def):
            raise ValueError(f"agent {agent!r} has no tools the model can call (it lists "
                             f"{', '.join(agent_def.tools) or 'none'}); a chat on a repo needs an agent with tools "
                             f"such as Read, Edit or Bash, or an MCP server (coder, for example); a chat without "
                             f"a repository (mode none) takes it")
        name = profile_name(self.config.raw, profile)
        placement = merged_placements({agent: profile_routes(self.config.raw, name).get(agent) or {}},
                                      {agent: {"model": model}} if model else {})[agent]
        if bound is not None and not (placement or {}).get("model"):
            key = personas.chat_model_key(self.config, bound)
            if key:
                placement = {**(placement or {}), "model": key}
        route = agent_route(self.config, agent_def, placement or None)
        record = None
        if bound is not None:
            personas.check_driver(self.config, bound, route.driver)
            record = personas.start_binding(self.config, bound, route.model_config(self.config),
                                            open_item=getattr(self.factory, "system_item", None))
        chat_id, chat_dir = self._allocate()
        base = branch = worktree = None
        cwd = repo_cfg.path if repo_cfg is not None else chat_dir / WORK_DIR
        try:
            if mode == "worktree":
                base = gitops.detect_base(repo_cfg.path, repo_cfg.base)
                branch = f"chat/{chat_id}"
                cwd = Path(self.config.data_dir) / "worktrees" / repo / chat_id
                gitops.add_worktree(repo_cfg.path, cwd, branch, base)
                worktree = str(cwd)
            elif mode == "none":
                cwd.mkdir(mode=0o700)
            now = utc_now()
            chat = {
                "id": chat_id, "title": "", "repo": repo,
                "repo_path": str(repo_cfg.path) if repo_cfg is not None else None,
                "agent": agent, "model": route.model, "driver": route.driver,
                "extra_body": route.extra_body, "reason": route.reason, "profile": name, "mode": mode,
                "base": base, "branch": branch, "worktree": worktree, "cwd": str(cwd), "status": "idle",
                "error": None, "session": None, "session_model": None, "turns": 0, "usage": zero_usage(),
                "cost": zero_cost(), "job": None, "created_at": now, "updated_at": now,
            }
            if record is not None:
                chat["persona"] = record
            with self._lock:
                return self._save(chat)
        except BaseException:
            if worktree is not None and repo_cfg is not None:
                with contextlib.suppress(gitops.GitError, OSError):
                    self._remove_checkout(repo_cfg.path, cwd, branch)
            shutil.rmtree(chat_dir, ignore_errors=True)
            raise

    def chat_send(self, chat_id: str, text: str) -> dict:
        """Start a turn with text in a thread and return the chat ("running"); ActionError if one runs."""
        if not isinstance(text, str) or not text.strip():
            raise ValueError("message text must be a non-empty string")
        with self._lock:
            chat = self._load(chat_id)
            if chat_id in self._turns:
                raise action_error(f"chat {chat_id} is already running a turn")
            chat["title"] = chat.get("title") or _title(text)
            chat.update(status="running", error=None)
            cancel = threading.Event()
            thread = threading.Thread(target=self._turn, args=(chat_id, text, cancel),
                                      name=f"chat-{chat_id}", daemon=True)
            self._save(chat)
            self._turns[chat_id] = (thread, cancel)
            thread.start()
            return chat

    def chat_cancel(self, chat_id: str) -> dict:
        """Ask the running turn, if any, to stop; returns the chat."""
        with self._lock:
            chat = self._load(chat_id)
            turn = self._turns.get(chat_id)
            if turn is not None:
                turn[1].set()
            return chat

    def chat_delete(self, chat_id: str) -> None:
        """Delete the chat folder (with a repo-less chat's work folder) and, in worktree mode, its worktree and
        branch (ActionError while running). Open items move to the repo's follow-ups, or for a chat without a
        repository to the system scope."""
        with self._lock:
            chat = self._load(chat_id)
            if chat_id in self._turns:
                raise action_error(f"chat {chat_id} is running; cancel it first")
            self._carry_forward(chat)
            repo_path = Path(chat.get("repo_path") or "")
            if chat.get("mode") == "worktree" and chat.get("worktree") and repo_path.is_dir():
                self._remove_checkout(repo_path, Path(chat["worktree"]), chat.get("branch"))
            shutil.rmtree(self.root / chat_id)
            self.bus.publish("chat.removed", {"chat": chat_id}, repo=chat.get("repo"))

    def chat_handoff(self, chat_id: str, feature: str, delivery: str | None = None,
                     profile: str | None = None) -> dict:
        """Submit the chat's branch to the factory as a job (from_branch=chat/<id>) and return the job.

        Needs a worktree-mode chat that is not running, with commits ahead of base, a clean
        tree and no earlier handoff job still in progress (else ActionError). delivery (merge,
        branch or folder; None: the config's merge.delivery) is the job's delivery mode, profile
        its routing profile (None: the default profile; "": none).
        """
        if not isinstance(feature, str) or not feature.strip():
            raise ValueError("feature must be a non-empty string")
        delivery = self.factory.check_delivery(delivery)
        extra = {"profile": profile} if profile is not None else {}
        with self._lock:
            chat = self._load(chat_id)
            if chat_id in self._turns:
                raise action_error(f"chat {chat_id} is running; wait for the turn to end")
            if not chat.get("repo"):
                raise action_error("a chat without a repository has no branch to hand off to the factory")
            if chat.get("mode") != "worktree":
                raise action_error("only a worktree-mode chat can be handed off to the factory")
            self._check_previous_job(chat)
            repo_cfg = self.config.repo(chat["repo"])
            base = gitops.detect_base(repo_cfg.path, repo_cfg.base)
            branch = chat["branch"]
            if gitops.commits_ahead(repo_cfg.path, base, branch) == 0:
                raise action_error(f"{branch} has no commits ahead of {base}")
            if not gitops.is_clean(Path(chat["worktree"]), repo_cfg.path):
                raise action_error(f"{chat['worktree']} has uncommitted changes; commit or discard them")
            store = self._items_store(chat)
            # a pending wake's item stays: the wake fires into this chat, and code closes the item there
            waiting = [item for item in store.ledger().items if item.get("status") not in tracker.CLOSED
                       and not tracker.is_wake_item(item)]
            if waiting:
                feature = f"{feature.rstrip()}\n\nOpen items from chat {chat_id}: see items.md"
                # the job's items.json gets them before the job is queued (Factory.submit): ChatManager never
                # writes a job's file, and nothing races the worker
                extra["items"] = [tracker.transfer_copy(item, store.scope, owner="builder", verifier="approver",
                                                        reason="handed off from the chat") for item in waiting]
            job = self.factory.submit(chat["repo"], feature, from_branch=branch, delivery=delivery, **extra)
            chat["job"] = job["id"]
            if waiting:
                self._hand_over(chat, job["id"], [item["id"] for item in waiting])
            self._save(chat)
            return job

    # -- internals ----------------------------------------------------------------

    def _turn(self, chat_id: str, text: str, cancel: threading.Event) -> None:
        cost: tuple[float | None, bool] | None = None
        switch: dict = {}
        log_path: Path | None = None
        try:
            log_path = next_log_path(self.root / chat_id / "logs", "turn")
            result, cost, switch = self._run_turn(self._load(chat_id), text, cancel, log_path)
        except Exception as exc:  # the route, agent or repo no longer resolves
            result = AgentRun(ok=False, error=str(exc))
        # the ingest numbers the reply's turn from the sessions as _turn is about to record them
        summary = self._ingest_turn(self._switched(self._load(chat_id), result, switch), result,
                                    log_path.stem if log_path is not None else None)
        with self._lock:
            self._turns.pop(chat_id, None)
            chat = self._switched(self._load(chat_id), result, switch)
            add_usage(chat.setdefault("usage", zero_usage()), result.usage)
            if cost is not None:
                add_cost(chat.setdefault("cost", zero_cost()), *cost)
            chat["turns"] = int(chat.get("turns") or 0) + 1
            if summary is not None or chat.get("items") is not None:
                with contextlib.suppress(OSError, ValueError):   # read now: a person may have acted meanwhile
                    chat["items"] = self._items_store(chat).ledger().summary()
            if result.ok:
                chat.update(status="idle", error=None)
            elif cancel.is_set():
                chat.update(status="idle", error="cancelled")
            else:
                chat.update(status="error", error=result.error or "the agent run failed")
            self._save(chat)

    @staticmethod
    def _switched(chat: dict, result: AgentRun, switch: dict) -> dict:
        """chat with a turn's session switch (see _run_turn) recorded; returns chat."""
        if switch.get("earlier") is not None:   # the old model's session was archived: never resume it
            chat.setdefault("earlier_sessions", []).append(switch["earlier"])
            chat.update(session=None, handover_text=None)
        if "handover_text" in switch:
            chat["handover_text"] = switch["handover_text"]
        if isinstance(switch.get("persona"), dict) and isinstance(chat.get("persona"), dict):
            chat["persona"] = {**chat["persona"], **switch["persona"]}   # this turn's binding and version
        chat["session"] = result.session_ref or chat.get("session")
        if result.session_ref and switch.get("model"):
            chat["session_model"] = switch["model"]
        return chat

    def _run_turn(self, chat: dict, text: str, cancel: threading.Event, log_path: Path | None = None
                  ) -> tuple[AgentRun, tuple[float | None, bool] | None, dict]:
        """One turn on the chat's route, placed like a job's run (place_run: the first build of its model
        that answers, else its overflow, and the slot wait). (the run, its cost or None when it never
        started, the session switch for _turn: {model: what it ran on, earlier?: the archived session,
        handover_text?}). A session never moves between models (DESIGN §23): when the turn runs on
        another driver, model, provider or model id than the one that made chat.session, that session
        is archived and the turn starts a fresh one with a hand-over prompt. The chat's tracked items
        (DESIGN §32) go into the run, without the stop guard."""
        from . import personas

        repo_path = self.config.repo(chat["repo"]).path if chat.get("repo") else None
        record = chat.get("persona") if isinstance(chat.get("persona"), dict) else None
        found = None
        if record is not None:
            found = personas.find_persona(self.config, chat["agent"])
            agent = found.agent
            _refuse_new_persona(agent, persona_chat=True)
        else:
            agent = load_agent(self.config, repo_path, chat["agent"])
            _refuse_new_persona(agent)
        route = Route(chat["driver"], chat["model"], chat.get("extra_body") or {}, chat.get("reason"))
        chat_dir = self.root / chat["id"]
        cwd = Path(chat["cwd"])
        log_path = log_path or next_log_path(chat_dir / "logs", "turn")
        emit = event_emitter(self.bus, chat["repo"], agent.name, log_path, chat=chat["id"])
        update: dict | None = None
        items = None
        if found is not None:
            # the live binding, read again: which version answers this turn, its knowledge and filters (§33)
            body, update, persona_notes, event = personas.persona_turn(
                self.config, found, record, turn=int(chat.get("turns") or 0) + 1,
                open_item=getattr(self.factory, "system_item", None))
            route.extra_body = deep_merge(route.extra_body, body)
            for level, message in persona_notes:
                emit("log", {"level": level, "message": message})
            emit("persona.turn", event)
        else:
            def changed(ledger: Any) -> None:
                self.bus.publish("items.updated", {"ref": ledger.scope.ref, "summary": ledger.summary(),
                                                   "changed": list(ledger.changed), "chat": chat["id"]},
                                 repo=chat.get("repo"))

            try:
                items = tracker.ItemsContext(self._items_store(chat), agent.name, chat_dir / JOURNAL_FILE,
                                             settings=self._housekeeping(), stop_guard=False,
                                             secrets=self._item_secrets(), on_change=changed)
            except (OSError, ValueError):
                items = None   # the ledger cannot be read: the turn runs without it; the ingest after it says why
        kept = {"persona": update} if update else {}
        placed = place_run(self.config, route, self.reach, cancel, emit)
        if placed is None:
            return AgentRun(ok=False, error="cancelled while waiting for a slot"), None, kept
        model, info, notes, release = placed
        try:
            if found is not None:   # neither a placement nor an edited route takes a persona elsewhere
                personas.check_driver(self.config, found, route.driver)
                personas.check_model(self.config, found, model)
            ctx = agent_context(
                self.config, repo_path, agent, model, job=chat, cwd=cwd,
                job_dir=chat_dir, write_paths=[cwd], read_roots=[cwd], log_path=log_path,
                emit=emit, cancel=cancel, session_path=chat_dir / SESSION_FILE, items=items)
            if found is not None:
                ctx.persona = personas.prompt_context(found, {**record, **(update or {})})
            if notes:
                emit("log", {"level": "warn" if "overflow" in info else "info",
                             "message": f"{agent.name} runs on {model.provider.name}/{model.model}: " + "; ".join(notes)})
            now = {"driver": route.driver, "model": model.key, "provider": model.provider.name, "model_id": model.model}
            session, switch, prompt = self._session_for(chat, now, text)
            if switch.get("handover"):
                info = {**info, "handover": True}
            result = run_agent(self.runner_factory, self.config, route, ctx, prompt, session, info)
        finally:
            release()
        switch.pop("handover", None)
        return result, run_cost_of(ctx, result), {**switch, "model": now, **kept}

    def _session_for(self, chat: dict, now: dict, text: str) -> tuple[str | None, dict, str]:
        """(the session to resume or None, the switch for _turn, the prompt) for a turn running on now.

        The chat's session resumes while now is the placement that made it (a chat.json from before
        session_model resumes as before). On another placement the builtin session file is archived
        under sessions/NN.json and a fresh session starts with a hand-over prompt; so does a turn
        after a hand-over whose run saved no session."""
        chat_dir = self.root / chat["id"]
        before = chat.get("session_model")
        if chat.get("session") and (before is None or before == now):
            return str(chat["session"]), {}, text
        earlier = list(chat.get("earlier_sessions") or [])
        if not chat.get("session"):
            if not earlier:
                return None, {}, text   # a chat's first session
            prompt = self._handover_prompt(chat, earlier, earlier[-1], now, text)
            return None, {"handover": True, "handover_text": text}, prompt
        current = chat_dir / SESSION_FILE
        handover_text = chat.get("handover_text")
        prompt = self._handover_prompt(chat, [*earlier, {"file": SESSION_FILE, "handover_text": handover_text}],
                                       before, now, text)
        entry = {**before, "file": None, "handover_text": handover_text}
        if current.is_file():   # moved last, so the chat.json _turn writes is what lists it
            entry["file"] = f"{EARLIER_DIR}/{len(earlier) + 1:02d}.json"
            (chat_dir / EARLIER_DIR).mkdir(exist_ok=True)
            os.replace(current, chat_dir / entry["file"])
        return None, {"earlier": entry, "handover": True, "handover_text": text}, prompt

    def _handover_prompt(self, chat: dict, earlier: list[dict], before: dict | None, now: dict, text: str) -> str:
        """The first prompt of a fresh session on another model: where the work is, the conversation so
        far (its last HANDOVER_CHARS characters) and the user's new message."""
        lines = []
        for entry in self._session_messages(chat, earlier):
            tools = f" [tools: {', '.join(entry['tools'])}]" if entry.get("tools") else ""
            lines.append(f"{'User' if entry['role'] == 'user' else 'Assistant'}: {entry['text']}{tools}")
        conversation = "\n\n".join(lines)
        cut = len(conversation) > HANDOVER_CHARS
        where = f"{before.get('provider')}/{before.get('model_id')}" if before and before.get("model_id") else "another model"
        branch = f" on the branch {chat['branch']}" if chat.get("branch") else ""
        work = (f"You work in {chat['cwd']}{branch}; everything done so far is there (git log and git diff show it)."
                if chat.get("repo") else f"Your working folder is {chat['cwd']}; anything written so far is there.")
        parts = [f"Hand-over: this chat ran on {where} until now. Its session cannot continue on "
                 f"{now['provider']}/{now['model_id']} (providers pass reasoning back differently), so you start "
                 f"a fresh session.", work]
        if conversation:
            parts.append("The conversation so far" + (" (its start is cut off)" if cut else "") + ":\n"
                         + conversation[-HANDOVER_CHARS:])
        parts.append(f"The user's new message:\n{text}")
        return "\n\n".join(parts)

    # -- tracked items (section 32) -------------------------------------------------

    def _housekeeping(self) -> Any:
        from .config import HousekeepingConfig

        try:
            return self.config.housekeeping()
        except (ValueError, AttributeError):
            return HousekeepingConfig()

    def _items_store(self, chat: dict) -> tracker.ItemStore:
        scope = tracker.chat_scope(chat["id"], chat.get("repo"), chat.get("agent") or "coder")
        return tracker.ItemStore(self.root / chat["id"], scope, md=False,
                                 max_proposals=self._housekeeping().max_proposals_per_scope)

    def _item_secrets(self) -> list[str]:
        values = getattr(self.factory, "secret_values", None)
        found = list(values()) if callable(values) else list(self.config.secret_values())
        from .events import env_secret_values

        return found + env_secret_values()

    @contextlib.contextmanager
    def _chat_items(self, chat: dict, timeout: float | None = None) -> Any:
        """A transaction on the chat's ledger; items.updated (with data.chat) after a change."""
        store = self._items_store(chat)
        with store.transaction(timeout=timeout, secrets=self._item_secrets()) as ledger:
            yield ledger
        if ledger.dirty:
            self.bus.publish("items.updated", {"ref": store.scope.ref, "summary": ledger.summary(),
                                               "changed": list(ledger.changed), "chat": chat["id"]},
                             repo=chat.get("repo"))

    def _ingest_turn(self, chat: dict, result: AgentRun, run: str | None = None) -> dict | None:
        """After a turn: the agent's Items calls, the sweep of its reply (each suggestion records the turn's
        log stem as run and its transcript turn number as turn, so it shows under that reply), the commit
        checks against the chat branch. Returns the new summary, or None when nothing changed (or the
        ledger cannot be read)."""
        agent = chat.get("agent") or "coder"
        hk = self._housekeeping()
        try:
            with self._chat_items(chat) as ledger:
                try:
                    notices = tracker.apply_journal(ledger, self.root / chat["id"] / JOURNAL_FILE, agent)
                    if hk.sweep != "off" and result.final_text:
                        suppress = self.factory.store.sweep_suppressed(chat["repo"]) if chat.get("repo") and hasattr(
                            self.factory, "store") else set()
                        turns = [entry["turn"] for entry in self._messages(chat) if entry.get("turn")]
                        tracker.apply_sweep(ledger, [("reply", result.final_text)], agent,
                                            limit=hk.max_proposals_per_run, suppress=suppress, run=run,
                                            turn=turns[-1] if turns else None, **self._persona_sweep(chat))
                except tracker.ItemsFull as exc:
                    notices = [str(exc)]
                notices += self._move_later(chat, ledger)
                tracker.check_evidence(ledger, self._evidence_checker(chat))
                ledger.set_notices(agent, notices)
                return ledger.summary() if ledger.dirty else None
        except (OSError, ValueError) as exc:
            self.bus.publish("log", {"level": "warn", "message": f"chat {chat['id']}: tracked items: {exc}",
                                     "chat": chat["id"]}, repo=chat.get("repo"))
            return None

    def _persona_sweep(self, chat: dict) -> dict:
        """For a persona chat's reply (§33): {writer: persona:<name>, taint}. The suggestions are a persona's
        words (items.persona_written, kept on a carried copy), so no outside agent's token ever reads them nor
        can a person assign them to one (§32); a private persona (the default, and whenever its file cannot be
        read now) also taints them private. {} for any other chat."""
        if not isinstance(chat.get("persona"), dict):
            return {}
        from . import personas

        name = str(chat.get("agent") or chat["persona"].get("persona") or "unknown")
        try:
            private = personas.find_persona(self.config, name).privacy != "clean"
        except (KeyError, ValueError, OSError):
            private = True   # fail closed: the persona's file changed or went away since the turn
        return {"writer": f"persona:{name}", "taint": ["private"] if private else []}

    def _evidence_checker(self, chat: dict) -> Callable[[dict], tuple[str, str]]:
        if not chat.get("repo"):
            return lambda entry: ("unchecked", "a chat without a repository has no git history to check it against")
        try:
            repo_path = self.config.repo(chat["repo"]).path
        except KeyError:
            return lambda entry: ("unchecked", "the repo is no longer configured")
        branch = chat.get("branch") if chat.get("mode") == "worktree" else None
        return tracker.git_checker(repo_path, chat.get("base"), branch, Path(chat["cwd"]))

    def _move_later(self, chat: dict, ledger: Any) -> list[str]:
        """Items the chat's agent made Later in place move to the repo's follow-ups, as a person's defer does
        (inside the chat's transaction); what the agent should hear. Best effort: the item stays Later here
        when the follow-ups cannot be written, and goes with the chat's items when it is deleted."""
        if not chat.get("repo"):
            return []
        due = tracker.add_days(tracker.today(), self._housekeeping().followup_due_days)
        try:
            return tracker.move_later(ledger, lambda copies: self.factory.add_followups(chat["repo"], copies), due)
        except (OSError, ValueError) as exc:
            self.bus.publish("log", {"level": "warn", "message": f"chat {chat['id']}: could not move Later items "
                                     f"to the follow-ups yet: {exc}", "chat": chat["id"]}, repo=chat.get("repo"))
            return []

    def _hand_over(self, chat: dict, job_id: str, handed: list[str]) -> None:
        """Make the chat's items that went with the job (Factory.submit(items=...): owner builder, must-do as
        marked, links.chat) Later with links.job (caller holds the manager's lock)."""
        with self._chat_items(chat) as ledger:
            for item_id in handed:
                item = ledger.find(item_id)
                if item is not None and item.get("status") not in tracker.CLOSED:
                    ledger.change(tracker.FACTORY, item_id, "deferred", reason=f"handed off to job {job_id}",
                                  links={"job": job_id})
            chat["items"] = ledger.summary()

    def _carry_forward(self, chat: dict) -> None:
        """Before a chat is deleted: its open items move to the repo's follow-ups, or for a chat without a
        repository to the system scope (deleting a chat must not delete its promises). The ledger goes with
        the chat folder; the copies keep links.chat."""
        store = self._items_store(chat)
        if not store.path.is_file():
            return
        due = tracker.add_days(tracker.today(), self._housekeeping().followup_due_days)
        add_system = getattr(self.factory, "add_system_items", None)
        if not chat.get("repo") and add_system is None:
            return   # a factory without the system scope (tests' fakes): nowhere to carry them
        with self._chat_items(chat) as ledger:
            later = {item["id"] for item in tracker.unmoved_later(ledger)}   # made Later in place: they go too
            waiting = [item for item in ledger.items if item.get("status") not in tracker.CLOSED
                       or item["id"] in later]
            if not waiting:
                return
            copies = [tracker.carry_copy(item, ledger.scope, due=(item.get("due") or due) if item["id"] in later
                                         else due, reason="chat deleted") for item in waiting]
            refs = self.factory.add_followups(chat["repo"], copies) if chat.get("repo") else add_system(copies)
            for item, ref in zip(waiting, refs):
                if item["id"] in later:
                    ledger.set_links(item["id"], followup=ref)
                else:
                    ledger.change(tracker.FACTORY, item["id"], "deferred", reason="chat deleted",
                                  links={"followup": ref})

    def scope_items(self, chat_id: str) -> dict:
        """{items, summary, log} of one chat (KeyError if unknown)."""
        chat = self._load(chat_id)
        store = self._items_store(chat)
        ledger = store.ledger()
        now = utc_now()
        return {"items": [tracker.decorate(item, store.scope, now=now, scope_stage=chat.get("status"))
                          for item in ledger.items],
                "summary": ledger.summary(), "log": store.log_tail()}

    def item_history(self, chat_id: str, item_id: str) -> dict:
        """{item, log}: one chat item and every transition it went through (KeyError if unknown)."""
        chat = self._load(chat_id)
        store = self._items_store(chat)
        item = store.ledger().get(item_id)
        return {"item": tracker.decorate(item, store.scope, now=utc_now(), scope_stage=chat.get("status")),
                "log": store.history(item_id)}

    def item_rows(self, repo: str | None, now: str) -> list[dict]:
        """Every chat's items as the tracker lists them (repo: only that repo's chats)."""
        rows: list[dict] = []
        for chat in self.chats():
            if (repo is not None and chat.get("repo") != repo) or not (self.root / chat["id"] / "items.json").is_file():
                continue
            with contextlib.suppress(OSError, ValueError):
                store = self._items_store(chat)
                rows += [tracker.decorate(item, store.scope, now=now, scope_stage=chat.get("status"))
                         for item in store.ledger().items]
        return rows

    def add_item(self, chat_id: str, fields: dict) -> dict:
        """A person's "+ Item" in the chat: owned by the chat's agent, verified by the person."""
        chat = self._load(chat_id)
        store = self._items_store(chat)
        with self._busy():
            with self._chat_items(chat, tracker.BUSY_SECONDS) as ledger:
                item = tracker.human_add(ledger, {key: fields.get(key) for key in ("title", "detail", "blocking")})
        self._store_summary(chat_id)
        return tracker.decorate(item, store.scope, now=utc_now(), scope_stage=chat.get("status"))

    def item_action(self, chat_id: str, item_id: str, action: str, **kwargs: Any) -> dict:
        """A person's action on one chat item; defer (and update to defer) moves it to the repo's follow-ups."""
        action = tracker.person_action(action, kwargs.get("to"))
        if action != "update":
            kwargs.pop("to", None)
        if action == "make-job":
            raise ValueError("hand the chat off to the factory instead; make-job works on follow-ups")
        chat = self._load(chat_id)
        store = self._items_store(chat)
        if not store.path.is_file():
            raise KeyError(f"unknown item {store.scope.item_ref(item_id)}")
        if action == "defer" and not chat.get("repo"):
            raise ValueError(tracker.NO_FOLLOWUPS)
        with self._busy():
            with self._chat_items(chat, tracker.BUSY_SECONDS) as ledger:
                if action == "defer":
                    self.factory.defer_to_followups(chat["repo"], ledger, item_id,
                                                    kwargs.get("reason") or kwargs.get("note") or "", kwargs.get("due"))
                    item = ledger.get(item_id)
                else:
                    item = tracker.human_action(ledger, item_id, action, **kwargs)
                    self._move_later(chat, ledger)
                    item = ledger.get(item_id)
        source = item.get("source") or {}
        if action == "dismiss" and source.get("type") == "sweep" and source.get("hash") and chat.get("repo") \
                and hasattr(self.factory, "store"):
            with contextlib.suppress(OSError, ValueError):
                self.factory.store.suppress_sweep(chat["repo"], str(source["hash"]))
        self._store_summary(chat_id)
        return {"item": tracker.decorate(item, store.scope, now=utc_now(), scope_stage=chat.get("status"))}

    @contextlib.contextmanager
    def _busy(self) -> Any:
        try:
            yield
        except tracker.ItemsBusy as exc:
            raise action_error(str(exc)) from None

    def _store_summary(self, chat_id: str) -> None:
        """Copy the ledger's summary into chat.json (chat.updated), unless a turn is about to write it."""
        with self._lock:
            chat = self._load(chat_id)
            summary = self._items_store(chat).ledger().summary()
            if chat.get("items") != summary:
                chat["items"] = summary
                self._save(chat)

    def _check_previous_job(self, chat: dict) -> None:
        if not chat.get("job"):
            return
        try:
            stage = self.factory.job(chat["repo"], chat["job"]).get("stage")
        except KeyError:
            return
        if stage not in ("merged", "delivered", "rejected", "cancelled"):
            raise action_error(f"chat {chat['id']} was already handed off as job {chat['job']} ({stage})")

    def _messages(self, chat: dict) -> list[dict]:
        """The conversation: every earlier session (one per model the chat moved away from), then the
        current one, as [{role, text, tools, turn}]: the person's k-th message opens turn k and the reply
        carries it, counted across sessions."""
        entries = self._session_messages(chat, [*(chat.get("earlier_sessions") or []),
                                                {"file": SESSION_FILE, "handover_text": chat.get("handover_text")}])
        turn = 0
        for entry in entries:
            turn += entry.get("role") == "user"
            entry["turn"] = turn or None
        return entries

    def _session_messages(self, chat: dict, sessions: list[dict]) -> list[dict]:
        """The [{role, text, tools}] of sessions ({file, handover_text?} each, oldest first; a missing or
        unreadable file has none). A session that began with a hand-over shows the user's own message
        there, not the hand-over prompt."""
        try:
            from .harness.loop import transcript
        except ImportError:
            return []
        found: list[dict] = []
        for entry in sessions:
            name = entry.get("file") if isinstance(entry, dict) else None
            if not isinstance(name, str) or not (name == SESSION_FILE or _EARLIER_FILE_RE.fullmatch(name)):
                continue
            try:
                messages = transcript(read_json(self.root / chat["id"] / name))
            except (OSError, ValueError):
                continue
            if entry.get("handover_text") and messages and messages[0]["role"] == "user":
                messages[0] = {**messages[0], "text": entry["handover_text"]}
            found += messages
        return found

    def _allocate(self) -> tuple[str, Path]:
        self.root.mkdir(parents=True, exist_ok=True)
        while True:
            chat_id = "c-" + secrets.token_hex(5)
            path = self.root / chat_id
            try:
                os.mkdir(path)
            except FileExistsError:
                continue
            return chat_id, path

    def _load(self, chat_id: str) -> dict:
        if not isinstance(chat_id, str) or not CHAT_ID_RE.fullmatch(chat_id):
            raise KeyError(f"unknown chat {chat_id!r}")
        try:
            return read_json(self.root / chat_id / CHAT_FILE)
        except FileNotFoundError:
            raise KeyError(f"unknown chat {chat_id!r}") from None

    def _save(self, chat: dict) -> dict:
        chat["updated_at"] = utc_now()
        write_json(self.root / chat["id"] / CHAT_FILE, chat)
        self.bus.publish("chat.updated", {"chat": chat}, repo=chat.get("repo"))
        return chat

    @staticmethod
    def _remove_checkout(repo_path: Path, worktree: Path, branch: str | None) -> None:
        gitops.remove_worktree(repo_path, worktree)
        if branch:
            gitops.delete_branch(repo_path, branch, force=True)
