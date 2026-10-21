"""In-process harness for the live CLI-driver end-to-end tests (tests/e2e/test_e2e_drivers.py).

No HTTP server: a Config, the EventBus and orchestrator.Factory from
``orchestrator.new_factory``, and a fresh copy (``cp -a``) of the fixture repo
for every scenario. Agents on the ``builtin`` driver talk to a
``tests.mock_llm.MockLLM`` scripted by ``FactoryScript``; agents routed to an
external CLI (Qwen Code, Claude Code) run the real binary, through a small
recording wrapper (``write_recording_wrapper``) that logs argv, cwd and the
environment's variable names before it execs the CLI, so a test can check the
exact command line the driver built and what its environment scrubbing let
through.

Paths come from the environment (defaults: this machine's scratchpad):
FACTORY_E2E_SCRATCH, FACTORY_E2E_FIXTURE, FACTORY_E2E_QWEN, FACTORY_E2E_CLAUDE_2189,
FACTORY_E2E_CODEX. FACTORY_E2E_KEEP=1 keeps every scenario's folder (repo, data dir,
logs, CLI homes) for inspection; failed scenarios are always kept.
FACTORY_E2E_RESULTS: a JSONL file each scenario appends its wall time, usage and
outcome to (default <scratch>/drv-e2e/results.jsonl).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from swfactory.config import Config
from swfactory.orchestrator import Factory, new_factory
from tests.mock_llm import MockLLM

DEFAULT_SCRATCH = "/tmp/claude-0/-home-user/03a51815-8655-5582-9e2d-e5c582ed1208/scratchpad"
SCRATCH = Path(os.environ.get("FACTORY_E2E_SCRATCH") or DEFAULT_SCRATCH)
FIXTURE_TEMPLATE = Path(os.environ.get("FACTORY_E2E_FIXTURE") or SCRATCH / "e2e" / "fixture-template")
QWEN_BIN = Path(os.environ.get("FACTORY_E2E_QWEN") or SCRATCH / "qwen" / "node_modules" / ".bin" / "qwen")
CLAUDE_2189_BIN = Path(os.environ.get("FACTORY_E2E_CLAUDE_2189")
                       or SCRATCH / "cc2189" / "node_modules" / ".bin" / "claude")
CODEX_BIN = Path(os.environ.get("FACTORY_E2E_CODEX") or SCRATCH / "codex" / "node_modules" / ".bin" / "codex")
RUNS_DIR = SCRATCH / "drv-e2e" / "runs"
RESULTS = Path(os.environ.get("FACTORY_E2E_RESULTS") or SCRATCH / "drv-e2e" / "results.jsonl")
REPO = "todo"
REVIEWERS = ("security", "ux", "ui", "code")
FEATURE = ("Add a clearDone(items) function to src/todo.js that returns the list without the completed "
           "items, with a unit test in test/todo.test.js")
DEAD_PROXY = "http://127.0.0.1:9"     # discard port: any request a CLI sends anywhere but the mock fails

# The first words of each shipped agent's body: identifies the agent behind a model request, both in the
# builtin harness's system prompt and in a CLI's system prompt (where the body arrives as
# --append-system-prompt).
AGENT_MARKERS = {
    "You write the spec for one factory job": "spec-writer",
    "You build one factory job": "builder",
    "You are the security reviewer for one factory job": "reviewer-security",
    "You are the UX reviewer for one factory job": "reviewer-ux",
    "You are the UI design reviewer for one factory job": "reviewer-ui",
    "You are the code reviewer for one factory job": "reviewer-code",
    "You are the final gate for one factory job": "approver",
}
PATHS_RE = re.compile(r"Job folder: (?P<job_dir>.+?)\. Worktree: (?P<worktree>.+?)\. "
                      r"Branch: (?P<branch>.+?)\. Base: (?P<base>.+?)\.(?:\n|$)")
ROUND_RES = (re.compile(r"Build round (\d+)\."), re.compile(r"Review round (\d+)\."),
             re.compile(r"Final round: (\d+)\."), re.compile(r"for round (\d+)\."))
REMINDER = "<system-reminder>"

CLEAR_DONE_JS = """
export function clearDone(items) {
  return items.filter((item) => !item.done);
}
"""
CLEAR_DONE_TEST = """
test("clearDone removes completed items", () => {
  const items = [{ text: "a", done: true }, { text: "b", done: false }];
  assert.deepEqual(clearDone(items), [{ text: "b", done: false }]);
});
"""
CLEAR_DONE_EMPTY_TEST = """
test("clearDone keeps an empty list empty", () => {
  assert.deepEqual(clearDone([]), []);
});
"""
SPEC_MD = """# clearDone: remove completed todo items

> Feature: {feature}

## Context
`src/todo.js` holds the pure list helpers (`addItem`, `toggleItem`, `removeItem`); each returns a new
array and has a test in `test/todo.test.js` (run with `node --test`).

## Requirements
1. `src/todo.js` exports `clearDone(items)`, which returns a new array without the items whose `done`
   is true, keeping the others in order and leaving the input unchanged.
2. `test/todo.test.js` covers it.

## Acceptance criteria
1. `node --test` passes, including a test for `clearDone`.
2. `node --check src/todo.js` passes.

## Out of scope
UI changes in `src/app.js` or `index.html`.

## Assumptions
None.

## Risk
None.
"""


# -- the fixture -------------------------------------------------------------------


def fresh_repo(root: Path, name: str = REPO) -> Path:
    """A private copy of the fixture template (cp -a); the template itself is never touched."""
    if not (FIXTURE_TEMPLATE / ".git").exists():
        raise FileNotFoundError(f"fixture template missing: {FIXTURE_TEMPLATE}")
    target = root / name
    subprocess.run(["cp", "-a", str(FIXTURE_TEMPLATE), str(target)], check=True)
    git(target, "worktree", "prune")
    status = git(target, "status", "--porcelain")
    if status.strip() or git(target, "branch", "--show-current").strip() != "main":
        raise RuntimeError(f"fixture copy is not a clean main checkout: {status!r}")
    return target


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


# -- recording wrapper for a CLI ------------------------------------------------------

WRAPPER = '''#!{python}
"""Recording wrapper written by tests/e2e/drivers_harness.py: log the launch, run the CLI with its
stdout passed through line by line and also kept in a file (the driver's agent.output events cut
lines at 2000 chars), and exit with the CLI's status. Same process group, so the driver's
SIGTERM/SIGKILL of the group reaches the CLI too."""
import json, os, subprocess, sys, time
out_path = {log!r} + ".%d.stdout" % os.getpid()
record = {{"argv": sys.argv[1:], "cwd": os.getcwd(), "time": time.time(), "pid": os.getpid(),
          "env_names": sorted(os.environ), "stdout_file": out_path,
          "env": {{k: os.environ.get(k) for k in {shown!r}}}}}
with open({log!r}, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(record) + "\\n")
proc = subprocess.Popen([{real!r}, *sys.argv[1:]], stdout=subprocess.PIPE)
with open(out_path, "wb") as out:
    for line in iter(proc.stdout.readline, b""):
        sys.stdout.buffer.write(line)
        sys.stdout.buffer.flush()
        out.write(line)
        out.flush()
code = proc.wait()
sys.exit(code if code >= 0 else 128 - code)
'''
SHOWN_ENV = ("HOME", "OPENAI_BASE_URL", "OPENAI_MODEL", "IS_SANDBOX", "HTTPS_PROXY", "NO_PROXY", "CODEX_HOME")


def write_recording_wrapper(path: Path, real: Path, log: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(WRAPPER.format(python=sys.executable, shown=SHOWN_ENV, log=str(log),
                                   real=str(Path(real).resolve())))
    path.chmod(0o755)
    return path


def read_launches(log: Path) -> list[dict]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def launch_events(launch: dict) -> list[dict]:
    """The JSON objects a recorded CLI launch printed on stdout, full length."""
    path = Path(launch.get("stdout_file") or "")
    if not path.is_file():
        return []
    found = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                found.append(value)
    return found


def arg_after(argv: list[str], flag: str) -> str | None:
    return argv[argv.index(flag) + 1] if flag in argv[:-1] else None


# -- reading model requests ------------------------------------------------------------


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and isinstance(b.get("text"), str))
    return ""


def _user_texts(message: dict) -> list[str]:
    """The text parts of a user message that are not a CLI's injected <system-reminder> blocks."""
    content = message.get("content")
    parts = [content] if isinstance(content, str) else [
        b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type", "text") == "text"]
    return [p for p in parts if isinstance(p, str) and p.strip() and not p.lstrip().startswith(REMINDER)]


@dataclass
class Turn:
    """Where a model request stands: which agent, its latest prompt and how many replies it already gave."""

    agent: str | None
    prompt: str
    step: int
    paths: dict
    round: int | None
    system: str
    tool_names: list[str]
    last_tool_output: str


def read_turn(body: dict) -> Turn:
    messages = [m for m in body.get("messages") or [] if isinstance(m, dict)]
    system = _text(body.get("system")) if body.get("system") else ""
    if messages and messages[0].get("role") == "system":
        system = _text(messages[0].get("content"))
    agent = next((name for marker, name in AGENT_MARKERS.items() if marker in system), None)
    last_prompt, prompt = -1, ""
    for index, message in enumerate(messages):
        if message.get("role") == "user":
            texts = _user_texts(message)
            if texts:
                last_prompt, prompt = index, "\n".join(texts)
    step = sum(1 for m in messages[last_prompt + 1:] if m.get("role") == "assistant")
    tool_output = ""
    for message in reversed(messages):
        if message.get("role") == "tool":
            tool_output = _text(message.get("content"))
            break
    match = PATHS_RE.search(prompt)
    paths = match.groupdict() if match else {}
    round_ = None
    for regex in ROUND_RES:
        found = regex.search(prompt)
        if found:
            round_ = int(found.group(1))
            break
    names = [t.get("function", {}).get("name") or t.get("name") for t in body.get("tools") or []]
    return Turn(agent, prompt, step, paths, round_, system, names, tool_output)


# -- the scripted factory agents ---------------------------------------------------------


class FactoryScript:
    """A MockLLM handler that plays every factory agent, deterministically.

    tools: the tool names of the runtime being scripted: "builtin" (Write, Bash) or "qwen"
    (write_file, run_shell_command). verdicts: {(reviewer, round): "PASS" | "CHANGES"}, PASS by
    default. Each request is answered from the agent's latest prompt and how many replies it
    already gave since (so a resumed session continues the script). Every request is summarised
    in .log, every tool list a request offered in .offered_tools.
    """

    TOOLSETS: ClassVar[dict] = {
        "builtin": {"write": ("Write", "file_path", "content"), "shell": ("Bash", "command"),
                    "read": ("Read", "file_path")},
        "qwen": {"write": ("write_file", "file_path", "content"), "shell": ("run_shell_command", "command"),
                 "read": ("read_file", "file_path")},
    }

    def __init__(self, tools: str = "builtin", verdicts: dict | None = None,
                 usage: dict | None = None) -> None:
        self.tools = self.TOOLSETS[tools]
        self.verdicts = dict(verdicts or {})
        self.usage = usage
        self.log: list[dict] = []
        self.offered_tools: list[list[str]] = []
        self._lock = threading.Lock()

    # MockLLM handler
    def __call__(self, body: dict, path: str) -> dict:
        turn = read_turn(body)
        reply = self.reply(turn, body)
        if self.usage is not None and "usage" not in reply:
            reply = dict(reply, usage=dict(self.usage))
        with self._lock:
            self.offered_tools.append(turn.tool_names)
            self.log.append({"time": time.time(), "agent": turn.agent, "step": turn.step, "round": turn.round,
                             "messages": len(body.get("messages") or []), "tools": len(turn.tool_names),
                             "roles": [m.get("role") for m in body.get("messages") or [] if isinstance(m, dict)],
                             "reply": _summary(reply), "prompt_head": turn.prompt[:160],
                             "last_user": _last_user_text(body)[:300]})
        return reply

    def dump(self, path: Path) -> None:
        """Write the request log (for a human reading a kept scenario folder)."""
        with self._lock:
            path.write_text(json.dumps({"log": self.log, "offered_tools": self.offered_tools[:1]}, indent=1))

    def reply(self, turn: Turn, body: dict) -> dict:
        if not turn.tool_names:                      # a side request without tools: plain text
            return {"text": "ok"}
        if turn.agent is None or not turn.paths:
            return {"text": "Nothing to do."}
        script = getattr(self, "_" + turn.agent.replace("-", "_"), None)
        if turn.agent.startswith("reviewer-"):
            script = self._reviewer
        actions = script(turn) if script else []
        if turn.step < len(actions):
            return actions[turn.step]
        return {"text": f"{turn.agent}: done."}

    # -- per-agent scripts: the list of replies, one per step --------------------------

    def _write(self, path: str, content: str) -> dict:
        name, path_key, content_key = self.tools["write"]
        return {"tool_calls": [{"name": name, "arguments": {path_key: path, content_key: content}}]}

    def _read(self, path: str) -> dict:
        """Read a file before writing it (Qwen Code's write_file refuses to overwrite an unread file)."""
        name, path_key = self.tools["read"]
        return {"tool_calls": [{"name": name, "arguments": {path_key: path}}]}

    def _shell(self, command: str) -> dict:
        name, command_key = self.tools["shell"]
        return {"tool_calls": [{"name": name, "arguments": {command_key: command}}]}

    def _spec_writer(self, turn: Turn) -> list[dict]:
        feature = turn.prompt.split("Feature: ", 1)[-1].split("\n", 1)[0]
        return [self._write(f"{turn.paths['job_dir']}/spec.md", SPEC_MD.format(feature=feature))]

    def _builder(self, turn: Turn) -> list[dict]:
        wt, job_dir = turn.paths["worktree"], turn.paths["job_dir"]
        n = turn.round or 1
        if "Build round 1." in turn.prompt:
            source = (Path(wt) / "src" / "todo.js").read_text()
            tests = (Path(wt) / "test" / "todo.test.js").read_text()
            tests = tests.replace("import { addItem, toggleItem, removeItem }",
                                  "import { addItem, toggleItem, removeItem, clearDone }")
            return [self._read(f"{wt}/src/todo.js"), self._read(f"{wt}/test/todo.test.js"),
                    self._write(f"{wt}/src/todo.js", source + CLEAR_DONE_JS),
                    self._write(f"{wt}/test/todo.test.js", tests + CLEAR_DONE_TEST),
                    self._shell(f"cd {wt} && node --test && git add src/todo.js test/todo.test.js && "
                                f"git commit -q -m 'Add clearDone to remove completed items'"),
                    self._write(f"{job_dir}/build.md", _build_md(n, "Added clearDone and its test."))]
        if "reviews are in" in turn.prompt:
            tests = (Path(wt) / "test" / "todo.test.js").read_text()
            return [self._read(f"{wt}/test/todo.test.js"),
                    self._write(f"{wt}/test/todo.test.js", tests + CLEAR_DONE_EMPTY_TEST),
                    self._shell(f"cd {wt} && node --test && git add test/todo.test.js && "
                                f"git commit -q -m 'Test clearDone on an empty list'"),
                    self._write(f"{job_dir}/build.md",
                                _build_md(n, "Round 1 finding (empty list test) addressed: added the test."))]
        return [self._shell(f"cd {wt} && git add -A && git commit -q -m 'Commit leftovers' || true")]

    def _reviewer(self, turn: Turn) -> list[dict]:
        lens = turn.agent[len("reviewer-"):]
        n = turn.round or 1
        verdict = self.verdicts.get((lens, n), "PASS")
        findings = ("1. test/todo.test.js: add a test that clearDone([]) returns [].\n"
                    if verdict == "CHANGES" else "None.\n")
        text = f"VERDICT: {verdict}\n\n## Checks\nnode --test: pass\n\n## Findings\n{findings}"
        return [self._write(f"{turn.paths['job_dir']}/round-{n}/review-{lens}.md", text)]

    def _approver(self, turn: Turn) -> list[dict]:
        text = ("APPROVE\n\n## Why\nThe spec is met and every review passed.\n\n"
                "## Acceptance criteria\n1. node --test passes: met (ran it).\n")
        return [self._write(f"{turn.paths['job_dir']}/decision.md", text)]


def _build_md(n: int, log: str) -> str:
    return (f"STATUS: READY\n\n# Build: clearDone\n\n## Summary\nsrc/todo.js exports clearDone; test in "
            f"test/todo.test.js.\n\n## Acceptance criteria\n1. [x] node --test passes: ran it, all pass.\n"
            f"2. [x] node --check src/todo.js: passes.\n\n## Validation\nnode --test: pass\n\n"
            f"## Log\n### Round {n}\n{log}\n")


def _last_user_text(body: dict) -> str:
    for message in reversed(body.get("messages") or []):
        if isinstance(message, dict) and message.get("role") == "user":
            return _text(message.get("content"))
    return ""


def _summary(reply: dict) -> str:
    calls = reply.get("tool_calls") or []
    if calls:
        return ", ".join(f"{c['name']}({json.dumps(c['arguments'])[:120]})" for c in calls)
    return "text: " + str(reply.get("text", ""))[:80]


# -- running a job -----------------------------------------------------------------------


@dataclass
class JobResult:
    job: dict
    seconds: float
    job_dir: Path
    events: list[dict] = field(default_factory=list)       # every event in the job's logs/*.jsonl, by seq
    runs: dict[str, list[dict]] = field(default_factory=dict)   # run stem (01-spec-writer) -> its events

    def of(self, kind: str, run: str | None = None) -> list[dict]:
        source = self.runs.get(run, []) if run else self.events
        return [e for e in source if e.get("type") == kind]

    def run_names(self, agent: str) -> list[str]:
        return sorted(name for name in self.runs if name.split("-", 1)[1] == agent)

    def warnings(self) -> list[str]:
        return [e["data"].get("message", "") for e in self.of("log") if e["data"].get("level") in ("warn", "error")]

    def agent_usage(self, agent: str) -> dict:
        total = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0}
        for name in self.run_names(agent):
            for event in self.of("agent.end", name):
                for key in total:
                    total[key] += int((event["data"].get("usage") or {}).get(key) or 0)
        return total


class Scenario:
    """One scenario's folder: a fresh fixture repo, a data dir, the builtin MockLLM, results recording."""

    def __init__(self, name: str) -> None:
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        self.name = name
        self.root = Path(tempfile.mkdtemp(prefix=f"{name}-", dir=RUNS_DIR))
        self.repo = fresh_repo(self.root)
        self.builtin = FactoryScript("builtin")
        self.mock = MockLLM(handler=self.builtin)
        self.mock.start()
        self.failed = False
        self.extra: dict = {}

    def close(self) -> None:
        self.mock.stop()
        if not self.failed and os.environ.get("FACTORY_E2E_KEEP") != "1":
            shutil.rmtree(self.root, ignore_errors=True)

    def config(self, *, drivers: dict | None = None, agents: dict | None = None,
               pipeline: dict | None = None, limits: dict | None = None) -> Config:
        """Every tier and the "mock" model on the builtin MockLLM; drivers and routes on top."""
        mock_model = {"provider": "e2e-mock", "model": "mock-model", "max_tokens": 4096, "stream": True,
                      "extra_body": {}}
        raw = {
            "data_dir": str(self.root / "data"),
            "repos": [{"name": REPO, "path": str(self.repo)}],
            "providers": {"e2e-mock": {"type": "openai", "base_url": self.mock.base_url, "api_key_env": None,
                                       "timeout": 60}},
            "models": {key: dict(mock_model) for key in ("opus", "sonnet", "haiku", "mock")},
            "drivers": drivers or {},
            "agents": {"*": {"driver": "builtin"}, **(agents or {})},
            "pipeline": {"reviewers": list(REVIEWERS), "max_parallel_jobs": 1, "max_resumes": 2,
                         **(pipeline or {})},
            "limits": {"max_wall_seconds": 900, **(limits or {})},
        }
        return Config(self.root / "etc" / "config.json", raw)

    def run_job(self, config: Config, feature: str = FEATURE, timeout: float = 900,
                routing: dict | str | None = None) -> JobResult:
        factory, _bus = new_factory(config)
        factory.start_jobs()
        started = time.monotonic()
        try:
            job = factory.submit(REPO, feature, routing=routing)
            job = factory.wait(REPO, job["id"], timeout=timeout)
        finally:
            _stop(factory)
        seconds = time.monotonic() - started
        job_dir = Path(job["job_dir"])
        events, runs = read_job_logs(job_dir)
        return JobResult(job=job, seconds=seconds, job_dir=job_dir, events=events, runs=runs)

    def record(self, outcome: str, result: JobResult | None = None, **extra: Any) -> None:
        """Append this scenario's numbers to RESULTS (best effort)."""
        entry: dict[str, Any] = {"scenario": self.name, "outcome": outcome, "root": str(self.root),
                                 "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        if result is not None:
            entry.update(stage=result.job.get("stage"), reason=result.job.get("reason"),
                         seconds=round(result.seconds, 1), usage=result.job.get("usage"),
                         rounds=result.job.get("round"),
                         history=[h.get("reviews") for h in result.job.get("history") or []],
                         runs=[{"run": name, "seconds": round(_run_seconds(events), 1),
                                "usage": (next(iter(e["data"].get("usage") for e in events
                                                    if e.get("type") == "agent.end"), None)),
                                "driver": next((e["data"].get("driver") for e in events
                                                if e.get("type") == "agent.start"), None)}
                               for name, events in sorted(result.runs.items())])
        entry.update(self.extra, **extra)
        try:
            RESULTS.parent.mkdir(parents=True, exist_ok=True)
            with open(RESULTS, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, default=str) + "\n")
        except OSError:
            pass


def _stop(factory: Factory) -> None:
    factory.stop(timeout=30)


def _run_seconds(events: list[dict]) -> float:
    for event in events:
        if event.get("type") == "agent.end":
            return float(event["data"].get("seconds") or 0)
    return 0.0


def read_job_logs(job_dir: Path) -> tuple[list[dict], dict[str, list[dict]]]:
    events: list[dict] = []
    runs: dict[str, list[dict]] = {}
    for log in sorted((job_dir / "logs").glob("*.jsonl")):
        items = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
        events += items
        if log.stem != "events":
            runs[log.stem] = items
    events.sort(key=lambda e: e.get("seq", 0))
    return events, runs


# -- CLI homes and driver entries ----------------------------------------------------------


def qwen_home(root: Path) -> Path:
    """A HOME for Qwen Code: OpenAI auth, no telemetry, no usage statistics, no update checks."""
    home = root / "qwen-home"
    (home / ".qwen").mkdir(parents=True, exist_ok=True)
    (home / ".qwen" / "settings.json").write_text(json.dumps({
        "security": {"auth": {"selectedType": "openai"}},
        "privacy": {"usageStatisticsEnabled": False},
        "telemetry": {"enabled": False},
        "general": {"disableAutoUpdate": True, "disableUpdateNag": True, "enableAutoUpdate": False},
    }, indent=2))
    return home


def qwen_driver(command: Path, home: Path, base_url: str, api_key: str, timeout: float = 300) -> dict:
    """drivers.qwen for config.json: the qwen preset on command, its env pointing at base_url."""
    return {"type": "cli", "preset": "qwen", "command": str(command), "timeout": timeout, "models": {},
            "env": {"HOME": str(home), "OPENAI_BASE_URL": base_url.rstrip("/") + "/v1", "OPENAI_API_KEY": api_key,
                    "QWEN_CODE_SUPPRESS_YOLO_WARNING": "1", "QWEN_CODE_DISABLE_PRECONNECT": "1",
                    "HTTPS_PROXY": DEAD_PROXY, "HTTP_PROXY": DEAD_PROXY, "https_proxy": DEAD_PROXY,
                    "http_proxy": DEAD_PROXY, "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}}


def claude_driver(command: Path | str, models: dict, timeout: float = 900, env: dict | None = None) -> dict:
    """drivers.claude for config.json. The parent Claude Code session's own ids are removed, so a nested
    claude never writes into (or reports as) this session."""
    scrub = {"CLAUDE_CODE_SESSION_ID": None, "CLAUDECODE": None, "CLAUDE_CODE_CHILD_SESSION": None,
             "DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
    return {"type": "cli", "preset": "claude", "command": str(command), "timeout": timeout, "models": models,
            "env": {**scrub, **(env or {})}}


def which(name: str) -> str | None:
    return shutil.which(name)


def kill_strays(pattern: str) -> None:
    """Best effort: kill processes whose command line contains pattern (left over by a failed run)."""
    try:
        out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, check=False).stdout
    except OSError:
        return
    for pid in out.split():
        if int(pid) != os.getpid():
            try:
                os.kill(int(pid), 9)
            except OSError:
                pass


def first_line(path: Path) -> str:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def job_files(job_dir: Path) -> list[str]:
    return sorted(p.relative_to(job_dir).as_posix() for p in job_dir.rglob("*")
                  if p.is_file() and p.relative_to(job_dir).parts[0] not in ("logs",))


def wait_for(predicate: Callable[[], bool], seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return predicate()
