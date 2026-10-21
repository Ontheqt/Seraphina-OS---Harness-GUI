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
from typing import TYPE_CHECKING, Callable

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

    def can_write(self, path: Path) -> bool:
        """True if path (resolved) is one of write_paths or below a directory in it."""
        p = path.resolve()
        for w in self.write_paths:
            w = w.resolve()
            if p == w or w in p.parents:
                return True
        return False


@dataclass
class AgentRun:
    ok: bool
    error: str | None = None
    final_text: str = ""
    usage: dict = field(default_factory=dict)
    session_ref: str | None = None
    turns: int = 0


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
