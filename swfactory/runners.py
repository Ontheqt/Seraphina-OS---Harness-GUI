"""The seam between the orchestrator and whatever runs an agent.

The orchestrator builds a RunContext, picks a Runner for the agent's driver and
calls run() or resume(). Runners never touch job.json and never decide whether
an agent "passed": they report how the run ended, and the orchestrator reads
the files the agent was supposed to write.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from .agentdefs import AgentDef
    from .config import Config, Limits, ModelConfig
    from .items import ItemsContext


@dataclass
class RunContext:
    job: dict
    agent: "AgentDef"
    cwd: Path
    job_dir: Path
    write_paths: list[Path]
    read_roots: list[Path]
    log_path: Path
    emit: Callable[[str, dict], None]
    cancel: threading.Event
    model: "ModelConfig"
    limits: "Limits"
    env: dict[str, str] = field(default_factory=dict)
    session_path: Path | None = None
    repo_path: Path | None = None
    skill_dirs: list[Path] = field(default_factory=list)
    mcp_servers: dict = field(default_factory=dict)
    sandbox: str = "none"
    sandbox_network: bool = True
    secrets: list[str] = field(default_factory=list)
    # The run's view of its scope's tracked items (DESIGN.md section 32): the builtin harness's Items tool,
    # re-injected notes and stop guard use it; CLI drivers get the items in their prompt instead.
    items: "ItemsContext | None" = None
    grid: str = ""   # the Factory Grid Card for a factory-line agent (gridcard.py), else empty
    # A persona chat's run (DESIGN.md section 33, personas.prompt_context): {type: "openwebui" | "snapshot", name,
    # label, variables?, user_name?}. The builtin harness then writes a thin system prompt that does not compete
    # with the persona's own instructions.
    persona: dict | None = None
    # Chat check-ins (Wake & Messages §5.8): the Wake tool's context (None: no Wake tool), the origin of the message
    # this run sends ("wake" for a check-in; recorded as a harness message, never as the user's), and a check-in's
    # smaller run: only Read, Glob, Grep, Wake and RunCheck (checkin), and the provider's slot taken as a background
    # caller that yields when a person needs the model (background).
    wake: "Any" = None
    message_origin: str | None = None
    checkin: bool = False
    background: bool = False
    checkin_commands: list = field(default_factory=list)
    max_output_tokens: int | None = None
    # A browsing run's grant (BROWSER_AND_CODEX.md §2.6, DESIGN §43): the path the harness core wrote for this run;
    # the browser server (sandbox "self") starts as `sf-browser mcp <grant>`. None: the run has no browser.
    browser_grant: Path | None = None
    # A codex-worker run's own out folder (DESIGN §44): Codex's --add-dir and where it writes build.md, outside the
    # job folder; set by the CLI driver when it prepares the run.
    codex_out: Path | None = None

    def can_write(self, path: Path) -> bool:
        """True if path (resolved) is one of write_paths or below a directory in it."""
        p = path.resolve()
        for w in self.write_paths:
            w = w.resolve()
            if p == w or w in p.parents:
                return True
        return False


OUTCOMES = ("completed", "failed", "declined", "cancelled", "timed_out", "unknown")


@dataclass
class AgentRun:
    ok: bool
    error: str | None = None
    final_text: str = ""
    usage: dict = field(default_factory=dict)
    session_ref: str | None = None
    turns: int = 0
    # How the run ended (BROWSER_AND_CODEX.md §3.6, DESIGN §40): completed, failed, declined, cancelled, timed_out
    # or unknown (it may have acted and nothing says how far it got). None: the runner did not say; ok then reads
    # as completed and not ok as failed (outcome_of).
    outcome: str | None = None
    details: dict = field(default_factory=dict)   # what a driver knows for the run's receipt (codex-worker)


def outcome_of(result: AgentRun) -> str:
    """result.outcome, or completed / failed from ok for a runner that does not type its outcome."""
    if result.outcome in OUTCOMES:
        return str(result.outcome)
    return "completed" if result.ok else "failed"


class Runner:
    supports_resume: bool = False

    def run(self, ctx: RunContext, prompt: str) -> AgentRun:
        raise NotImplementedError

    def resume(self, ctx: RunContext, session_ref: str, message: str) -> AgentRun:
        raise NotImplementedError

    def launch_model(self, ctx: RunContext) -> tuple[str, str]:
        """(model id, provider) that really serve a run on ctx, for agent.start: the resolved model
        and its provider here; a CLI driver reports the model it passes its CLI and its own name."""
        return str(ctx.model.model), str(ctx.model.provider.name)


def launch_model(runner: object | None, ctx: RunContext, default: tuple[str, str]) -> tuple[str, str]:
    """runner.launch_model(ctx), or default for a runner without one (or when it fails)."""
    describe = getattr(runner, "launch_model", None)
    if describe is None:
        return default
    try:
        model_id, provider = describe(ctx)
    except Exception:  # noqa: BLE001 - agent.start must never fail over a label
        return default
    return str(model_id), str(provider)


def make_runner(config: "Config", driver_name: str) -> Runner:
    driver = config.driver(driver_name)
    if driver.type == "builtin":
        from .harness.loop import BuiltinRunner

        return BuiltinRunner(config)
    if driver.type == "cli":
        from .drivers.cli import CliRunner

        return CliRunner(config, driver)
    raise ValueError(f"unknown driver type {driver.type!r} for driver {driver_name!r}")


def add_usage(total: dict, more: dict) -> dict:
    """Sum the usage counters the factory tracks into total (in place) and return it."""
    for key in ("input_tokens", "output_tokens", "cached_tokens", "calls"):
        total[key] = int(total.get(key, 0)) + int(more.get(key, 0) or 0)
    return total
