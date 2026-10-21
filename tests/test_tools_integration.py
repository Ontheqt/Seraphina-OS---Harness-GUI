"""Real tool integrations: REA over MCP, Matt Pocock's skills and agent-browser (docs/tools.md, docs/skills.md).

The ungated tests check the shipped files offline: examples/rea.json (an add-on preset fragment
with the REA MCP servers) and agents/analyst.md (the reverse-engineering Chat agent), and that the
agent-browser commands the prompts teach are the ones the gated test runs.

The other tests need the real tools and run only with FACTORY_E2E_TOOLS=1. They find them through:

    FACTORY_REA_BIN          the `rea` executable of rea-agents 6.x (default: `rea` on PATH)
    FACTORY_SKILLS_CLONE     a clone of https://github.com/mattpocock/skills
    FACTORY_AGENT_BROWSER    the agent-browser executable (default: `agent-browser` on PATH)
    AGENT_BROWSER_EXECUTABLE_PATH   optional: the Chromium agent-browser should drive

A gated test whose tool is missing is skipped with the reason.
"""

from __future__ import annotations

import dataclasses
import functools
import http.server
import json
import os
import re
import shlex
import shutil
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from swfactory import chats, settings
from swfactory.agentdefs import discover_agents, parse_agent_file, required_agents
from swfactory.config import Config, McpServerConfig, ModelConfig, ProviderConfig, agent_dirs
from swfactory.harness import loop
from swfactory.harness.loop import BuiltinRunner, build_system_prompt, tool_output_cap
from swfactory.harness.mcp import McpClient
from swfactory.harness.sandbox import Sandbox, _live_groups, bwrap_available
from swfactory.harness.tools import truncate_output
from swfactory.runners import RunContext
from swfactory.skills import SkillIndex, skill_dirs
from tests.helpers import make_repo
from tests.mock_llm import MockLLM

PROJECT = Path(__file__).resolve().parents[1]
REA_PRESET = PROJECT / "examples" / "rea.json"
ANALYST = PROJECT / "agents" / "analyst.md"
GATED = os.environ.get("FACTORY_E2E_TOOLS") == "1"
GATE_REASON = "set FACTORY_E2E_TOOLS=1 to run the tests that need the installed tools"

# The owner's plan: the static profile covers session identity, artifact/JavaScript analysis,
# retained views and export (inspect_artifact works on the target open_binary opened, so opening and
# closing are in it too); the native profile adds overview, procedures and functions for Ghidra.
REA_STATIC = ["binary_session", "open_binary", "inspect_artifact", "analyze_javascript_application",
              "inspect_analysis_view", "get_evidence_bundle", "export_evidence_bundle", "close_binary"]
REA_NATIVE_EXTRA = ["binary_overview", "list_procedures", "search_procedures", "analyze_function"]
REA_WRITE_ARGS = {"export_evidence_bundle": ["path"]}   # the only allowlisted tool that writes a file
NOT_TOOLS = {"expected_package_version", "server_identity"}   # REA names the analyst prompt uses that are not tools
COMPACT_SCHEMA_BUDGET = 13 * 1024   # REA's compact input-schema budget per tool

SKILLS = {"setup-matt-pocock-skills": ("engineering", False), "diagnosing-bugs": ("engineering", True),
          "tdd": ("engineering", True), "codebase-design": ("engineering", True),
          "code-review": ("engineering", True), "handoff": ("productivity", False),
          "writing-for-agents": ("productivity", True)}

# The agent-browser usage each prompt teaches (the gated test runs these exact command shapes).
BROWSER_USAGE = {
    "builder": ["agent-browser --session <job-id>-builder open <url>", "`snapshot -i`",
                "`get text <selector>`", "`errors`", "agent-browser --session <job-id>-builder close"],
    "reviewer-ux": ["agent-browser --session <job-id>-ux open <url>", "--session <job-id>-ux` on every command",
                    "`snapshot -i`", "`get text <selector>`", "`set viewport 390 844`", "`errors`", "`console`",
                    "agent-browser --session <job-id>-ux close"],
    "reviewer-ui": ["agent-browser --session <job-id>-ui open", "--session <job-id>-ui` on every command",
                    "`get styles <selector>`", "`get box <selector>`", "`set viewport 390 844`",
                    '`eval "document.documentElement.scrollWidth > innerWidth"`',
                    "agent-browser --session <job-id>-ui close"],
    "coder": ["agent-browser --session <name> open <url>", "`snapshot -i` with the same", "`close`"],
}
STYLE_FILTER = ("^(color|background-color|font-(family|size|weight)|line-height|(padding|margin)-[a-z]+"
                "|border-top-left-radius):")
# Under bwrap a later Bash call cannot reach the browser an earlier one opened, which keeps running until the run
# ends: the prompts say to open, check and close in one call (the gated bwrap test runs that shape).
ONE_CALL = {"builder": ("run `open`, them and `close` in one Bash call", "<job-id>-builder"),
            "reviewer-ux": ("run `open`, the commands after it and `close` in one Bash call", "<job-id>-ux"),
            "reviewer-ui": ("run `open`, the commands after it and `close` in one Bash call", "<job-id>-ui"),
            "coder": ("run `open`, `snapshot -i` and `close` in one Bash call", None)}

TODO_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Todo</title><style>
:root { --accent: #2f6fde; --radius: 8px; --space: 12px; }
body { margin: 0; font: 16px/1.5 system-ui, sans-serif; }
.card { max-width: 480px; margin: 48px auto; padding: calc(var(--space) * 2); }
form { display: flex; gap: var(--space); }
button { padding: 8px var(--space); border: 0; border-radius: var(--radius); background: var(--accent); color: #fff; }
.visually-hidden { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); }
</style></head><body><main class="card"><h1>Todo</h1>
<form id="add-form"><label for="new-item" class="visually-hidden">New item</label>
<input id="new-item" placeholder="What needs doing?" autocomplete="off"><button type="submit">Add</button></form>
<ul id="list"></ul><p id="empty">Nothing to do yet.</p></main></body></html>
"""
JS_APP = {"mathutil.js": "'use strict';\nconst FIXTURE_NAME = 'factory-fixture';\n"
                         "function mix(x) { return 3 * x + 7; }\n"
                         "function label(x) { return FIXTURE_NAME + ':' + mix(x); }\n"
                         "module.exports = { mix, label };\n",
          "package.json": '{"name": "factory-fixture", "version": "1.0.0", "main": "mathutil.js"}\n'}


def rea_preset() -> dict:
    return json.loads(REA_PRESET.read_text(encoding="utf-8"))


def tool_path(env_name: str, default: str | None) -> str | None:
    """The executable env_name names (or default on PATH), or None. default None: env_name names a directory."""
    configured = os.environ.get(env_name)
    if default is None:
        return configured if configured and Path(configured).is_dir() else None
    if configured:
        return configured if Path(configured).is_file() and os.access(configured, os.X_OK) else None
    return shutil.which(default)


class TempHome(unittest.TestCase):
    """A temporary dir as $HOME, so the user's own agents and skills never leak into a test."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        home = self.tmp / "home"
        home.mkdir()
        patcher = mock.patch.dict(os.environ, {"HOME": str(home)})
        patcher.start()
        self.addCleanup(patcher.stop)


# ------------------------------------------------------------------ ungated: shipped files


class ReaPresetTest(TempHome):
    def test_fragment_shape(self) -> None:
        preset = rea_preset()
        self.assertEqual(set(preset), {"description", "mcp_servers"}, "an add-on: no providers, models or routes")
        self.assertIn("Add-on", preset["description"])
        servers = preset["mcp_servers"]
        self.assertEqual(set(servers), {"rea", "rea-native"})
        for name, entry in servers.items():
            with self.subTest(server=name):
                self.assertEqual(entry["command"], "~/.local/bin/rea-mcp")
                self.assertEqual(entry["args"], [])
                self.assertEqual(entry["timeout"], 450, "the plan's tool_timeout_sec")
                self.assertGreaterEqual(entry["startup_timeout"], 30)
                self.assertLessEqual(entry["startup_timeout"], 60)
                self.assertEqual(entry["env"]["REA_MCP_INPUT_SCHEMA_PROFILE"], "compact")
                self.assertEqual(len(entry["tools"]), len(set(entry["tools"])))
                self.assertEqual(entry["write_args"], REA_WRITE_ARGS, "the export path gets Write's checks")
        self.assertEqual(servers["rea"]["tools"], REA_STATIC)
        self.assertEqual(servers["rea-native"]["tools"], REA_STATIC + REA_NATIVE_EXTRA)
        self.assertEqual(servers["rea-native"]["env"]["REA_ANALYSIS_PROVIDER"], "ghidra")

    def test_listed_and_merged_into_a_config_without_touching_routing(self) -> None:
        self.assertIn("rea", settings.list_presets())
        path = self.tmp / "config.json"
        path.write_text(json.dumps({"mcp_servers": {"other": {"command": "other-mcp"}},
                                    "agents": {"builder": {"model": "sonnet"}}}))
        base = Config.load(path)
        merged = settings.preset_config(base, "rea")
        merged.validate()
        settings.validate_settings(merged)
        for section in ("providers", "models", "agents", "pipeline", "limits", "harness"):
            self.assertEqual(merged.raw[section], base.raw[section], f"{section} is left alone")
        self.assertNotIn("description", merged.raw, "the preset's description is not a config key")
        servers = merged.mcp_servers()
        self.assertEqual(set(servers), {"other", "rea", "rea-native"})
        rea = servers["rea"]
        self.assertEqual(rea.command, str(Path(os.environ["HOME"]) / ".local" / "bin" / "rea-mcp"))
        self.assertEqual((rea.args, rea.tools, rea.timeout, rea.startup_timeout), ([], REA_STATIC, 450.0, 30.0))
        self.assertEqual(rea.write_args, REA_WRITE_ARGS)
        self.assertEqual(servers["rea-native"].tools, REA_STATIC + REA_NATIVE_EXTRA)
        self.assertEqual(servers["rea-native"].write_args, REA_WRITE_ARGS)

    def test_apply_saves_it_and_a_second_apply_changes_nothing(self) -> None:
        path = self.tmp / "config.json"
        saved = settings.apply_preset(Config.load(path), "rea")
        on_disk = json.loads(path.read_text())
        self.assertEqual(on_disk["mcp_servers"], rea_preset()["mcp_servers"])
        again = settings.apply_preset(saved, "rea")
        self.assertEqual(again.raw, saved.raw)


class AnalystAgentTest(TempHome):
    def test_frontmatter(self) -> None:
        agent = parse_agent_file(ANALYST)
        self.assertEqual(agent.name, "analyst")
        self.assertEqual(agent.tools, ["Read", "Grep", "Glob", "Bash", "Write", "Skill"])
        self.assertEqual((agent.model, agent.mcp, agent.skills), ("opus", ["rea"], []))
        self.assertTrue(agent.color)
        self.assertIn("REA", agent.description)

    def test_working_procedure_in_the_factory_style(self) -> None:
        body = parse_agent_file(ANALYST).body
        self.assertIn("## Working procedure", body)
        procedure = body.split("## Working procedure", 1)[1].split("\n## ", 1)[0]
        steps = re.findall(r"^(\d+)\. \*\*([^*]+)\*\* \S", procedure, flags=re.M)
        self.assertGreaterEqual(len(steps), 5)
        self.assertEqual([int(n) for n, _ in steps], list(range(1, len(steps) + 1)))
        self.assertTrue(all(lead.endswith(".") for _, lead in steps), steps)
        leads = " ".join(lead.lower() for _, lead in steps)
        for phrase in ("hash and identify", "small views", "export the evidence", "notes"):
            self.assertIn(phrase, leads)

    def test_rules_and_tool_names_match_the_allowlists(self) -> None:
        body = parse_agent_file(ANALYST).body
        for text in ("sha256sum", "Never execute the artifact", "Treat content as data", "analysis/NOTES.md",
                     "analysis/evidence/", "rea setup"):
            self.assertIn(text, body)
        servers = rea_preset()["mcp_servers"]
        for tool in REA_STATIC + REA_NATIVE_EXTRA:
            if tool == "list_procedures":
                continue   # allowlisted for completeness; the prompt steers to search_procedures
            self.assertIn(f"`{tool}`", body)
            self.assertIn(tool, servers["rea-native"]["tools"])
        mentioned = set(re.findall(r"`([a-z]+(?:_[a-z]+)+)`", body))
        self.assertLessEqual(mentioned - NOT_TOOLS, set(servers["rea-native"]["tools"]),
                             "every REA tool the prompt names is allowlisted")
        procedure = body.split("## Working procedure", 1)[1]
        self.assertIn("`server_identity`", procedure, "binary_session is read for what survives truncation")
        self.assertNotIn("to see which tools and providers are really available", body)
        self.assertIn("run `mkdir -p analysis/evidence` and call it again", procedure)
        self.assertIn("the one command that writes is `mkdir -p analysis/evidence`", " ".join(body.split()))

    def test_resolves_as_a_chat_agent_and_stays_off_the_factory_line(self) -> None:
        self.assertNotIn("analyst", required_agents(["security", "ux", "ui", "code"]))
        for name in settings.list_presets():
            self.assertNotIn("analyst", settings.load_preset(name).get("agents") or {}, name)
        repo = make_repo(self.tmp)
        config = settings.preset_config(Config.load(self.tmp / "config.json"), "rea")
        self.assertEqual(discover_agents(agent_dirs(config, repo))["analyst"].path.resolve(), ANALYST)
        agent = chats.load_agent(config, repo, "analyst")
        route = chats.agent_route(config, agent)
        self.assertEqual((route.driver, route.model), ("builtin", "opus"))
        ctx = chats.agent_context(config, repo, agent, route.model_config(config), job={"id": "c-0123456789"},
                                  cwd=repo, job_dir=self.tmp / "chat", write_paths=[repo], read_roots=[repo],
                                  log_path=self.tmp / "chat" / "01-turn.jsonl", emit=lambda *a: None,
                                  cancel=threading.Event(), session_path=self.tmp / "chat" / "session.json")
        self.assertEqual(ctx.mcp_servers["rea"].tools, REA_STATIC)
        prompt = build_system_prompt(agent, ctx, SkillIndex(ctx.skill_dirs))
        self.assertIn("MCP servers: rea", prompt)
        self.assertIn("## Working procedure", prompt)


class BrowserPromptTest(unittest.TestCase):
    def test_prompts_teach_the_checked_commands_with_one_session(self) -> None:
        for agent, needles in BROWSER_USAGE.items():
            text = " ".join((PROJECT / "agents" / f"{agent}.md").read_text(encoding="utf-8").split())
            with self.subTest(agent=agent):
                for needle in needles:
                    self.assertIn(needle, text)
                self.assertNotIn("`agent-browser snapshot", text, "a bare snapshot reaches no page")
        ui = " ".join((PROJECT / "agents" / "reviewer-ui.md").read_text(encoding="utf-8").split())
        self.assertIn(STYLE_FILTER, ui)

    def test_prompts_close_the_browser_in_the_call_that_opened_it_when_the_shell_does_not_keep_it(self) -> None:
        for agent, (needle, session) in ONE_CALL.items():
            text = " ".join((PROJECT / "agents" / f"{agent}.md").read_text(encoding="utf-8").split())
            with self.subTest(agent=agent):
                self.assertIn(needle, text)
                if session:
                    self.assertIn(f'`ab="agent-browser --session {session}"; $ab open <url> && $ab ', text)
                    self.assertIn("; $ab close`", text)
                    self.assertIn("keeps running until your run ends", text)


# ------------------------------------------------------------------ gated: the real tools


def needs(env_name: str, default: str | None):
    """Skip unless FACTORY_E2E_TOOLS=1 and the tool is installed; pass its path to the test."""
    def wrap(test):
        @functools.wraps(test)
        def run(self, *args):
            if not GATED:
                self.skipTest(GATE_REASON)
            path = tool_path(env_name, default)
            if path is None:
                self.skipTest(f"{default or 'the tool'} not found (set {env_name})")
            return test(self, path, *args)
        return run
    return wrap


def rea_config(command: str, server: str = "rea") -> McpServerConfig:
    """examples/rea.json's entry for server, started with the given rea executable directly."""
    entry = rea_preset()["mcp_servers"][server]
    return McpServerConfig(name=server, command=command, args=["mcp"], env=dict(entry["env"]),
                           timeout=float(entry["timeout"]), tools=list(entry["tools"]),
                           startup_timeout=float(entry["startup_timeout"]), write_args=dict(entry["write_args"]))


def write_js_app(root: Path) -> Path:
    app = root / "input" / "app"
    app.mkdir(parents=True)
    for name, text in JS_APP.items():
        (app / name).write_text(text)
    return app


class ReaMcpTest(TempHome):
    def start(self, command: str, server: str = "rea") -> McpClient:
        client = McpClient(rea_config(command, server), self.tmp, log=lambda line: None)
        client.start()
        self.addCleanup(client.close)
        return client

    @needs("FACTORY_REA_BIN", "rea")
    def test_static_round_trip(self, rea: str) -> None:
        client = self.start(rea)
        offered = {tool["name"]: tool for tool in client.tools()}
        self.assertEqual(sorted(offered), sorted(REA_STATIC), "every allowlisted tool exists, nothing else")
        for name, tool in offered.items():
            self.assertLessEqual(len(json.dumps(tool["inputSchema"])), COMPACT_SCHEMA_BUDGET, name)
        text, is_error = client.call("binary_session", {})
        self.assertFalse(is_error, text[:500])
        identity = json.loads(text)["result"]["server_identity"]
        self.assertEqual(identity["package"]["name"], "rea-agents")
        self.assertTrue(identity["package"]["version"].startswith("6."), identity["package"])
        small = ModelConfig(key="small", provider=None, model="m", max_tokens=8192, context_tokens=32768)
        for cap in (30000, tool_output_cap(small, Config(self.tmp / "c.json", {}).limits())):
            with self.subTest(cap=cap):   # what the analyst's step 2 reads survives the factory's cut
                kept = truncate_output(text, cap)
                self.assertLess(len(kept), len(text))
                for needle in ('"capabilities"', '"server_identity"', identity["package"]["version"]):
                    self.assertIn(needle, kept)

        app = write_js_app(self.tmp)
        text, is_error = client.call("analyze_javascript_application", {"input_path": str(app), "detail": "summary"})
        self.assertFalse(is_error, text[:500])
        summary = json.loads(text)
        self.assertTrue(summary["evidence_id"].startswith("ev_"))
        self.assertEqual(summary["normalized_result"]["parent_operation"], "analyze_javascript_application")

        bundle = self.tmp / "evidence" / "bundle.json"
        text, is_error = client.call("export_evidence_bundle", {"path": str(bundle)})
        self.assertTrue(is_error, "REA itself never creates the directory (the factory's write_args check does)")
        self.assertIn("directory does not exist", text)
        bundle.parent.mkdir()
        text, is_error = client.call("export_evidence_bundle", {"path": str(bundle)})
        self.assertFalse(is_error, text[:500])
        exported = json.loads(bundle.read_text())
        self.assertIn(summary["normalized_result"]["parent_evidence_id"],
                      [record["evidence_id"] for record in exported["records"]], "the retained analysis is exported")
        self.assertEqual(sorted(p.name for p in app.iterdir()), sorted(JS_APP), "the input is untouched")

        text, is_error = client.call("open_binary", {"path": str(app / "mathutil.js")})
        self.assertFalse(is_error, text[:500])
        self.assertEqual(client.call("close_binary", {})[1], False)
        text, is_error = client.call("capture_process_scenario", {})
        self.assertTrue(is_error)
        self.assertIn("not in the tools allowlist", text)

    @needs("FACTORY_REA_BIN", "rea")
    def test_native_profile_lists_its_tools(self, rea: str) -> None:
        offered = [tool["name"] for tool in self.start(rea, "rea-native").tools()]
        self.assertEqual(sorted(offered), sorted(REA_STATIC + REA_NATIVE_EXTRA))

    def run_analyst(self, rea: str, cwd: Path, script: list, server: McpServerConfig, sandbox: str = "none"):
        """The analyst agent file on the builtin runner with a scripted model; returns (run, model requests)."""
        agent = parse_agent_file(ANALYST)
        with MockLLM(script) as llm:
            provider = ProviderConfig(name=f"mock-{uuid.uuid4().hex[:8]}", type="openai",
                                      base_url=llm.base_url, api_key_env=None, timeout=30)
            model = ModelConfig(key="test", provider=provider, model="mock-model", max_tokens=1024, stream=True)
            config = Config(self.tmp / "config.json", {})
            ctx = RunContext(job={"id": "c-0123456789"}, agent=agent, cwd=cwd, job_dir=self.tmp / "chat",
                             write_paths=[cwd], read_roots=[cwd], log_path=self.tmp / "chat" / "01-turn.jsonl",
                             emit=lambda *a: None, cancel=threading.Event(), model=model, limits=config.limits(),
                             session_path=self.tmp / "chat" / f"session-{sandbox}.json", repo_path=cwd,
                             mcp_servers={"rea": server}, sandbox=sandbox, env={"GIT_CONFIG_GLOBAL": "/dev/null"})
            # The test tree and a scratch install of REA sit under /tmp, which bwrap hides behind the sandbox's
            # own /tmp: show them read-only, as a case directory and ~/.local/npm are seen on a server.
            roots = [self.tmp, Path(rea).resolve().parents[2]]
            sandbox_cls = type("Sandbox", (_VisibleSandbox,), {"roots": roots})
            with mock.patch.object(loop, "Sandbox", sandbox_cls):
                run = BuiltinRunner(config).run(ctx, "What does mix() in the case's app return?")
            return run, list(llm.requests)

    @needs("FACTORY_REA_BIN", "rea")
    def test_the_analyst_uses_rea_through_the_builtin_runner(self, rea: str) -> None:
        cwd = make_repo(self.tmp)
        app = write_js_app(self.tmp / "case")
        notes = cwd / "analysis" / "NOTES.md"
        evidence = cwd / "analysis" / "evidence" / "case-1.json"
        victim = self.tmp / "victim.txt"
        victim.write_text("precious")
        (cwd / "escape").symlink_to(self.tmp)
        call = lambda tool, **arguments: {"tool_calls": [{"name": f"mcp__rea__{tool}", "arguments": arguments}]}
        script = [
            call("binary_session"),
            call("analyze_javascript_application", input_path=str(app), detail="summary"),
            call("export_evidence_bundle", path=str(evidence)),   # a fresh checkout: no analysis/evidence/ yet
            call("export_evidence_bundle", path=str(victim), overwrite=True),
            call("export_evidence_bundle", path=str(cwd / "escape" / "victim.txt"), overwrite=True),
            {"tool_calls": [{"name": "Write", "arguments": {"file_path": str(notes),
                                                            "content": "# Notes\n\nmix(x) = 3*x + 7\n"}}]},
            {"text": "mix returns 3*x + 7; evidence in analysis/evidence/case-1.json."},
        ]
        run, requests = self.run_analyst(rea, cwd, script, rea_config(rea))
        self.assertTrue(run.ok, run.error)
        offered = sorted(t["function"]["name"] for t in requests[0]["tools"] if t["function"]["name"].startswith("mcp__"))
        self.assertEqual(offered, sorted(f"mcp__rea__{name}" for name in REA_STATIC))
        result = lambda n: [m["content"] for m in requests[n]["messages"] if m.get("role") == "tool"][-1]
        self.assertIn("server_identity", result(1))
        self.assertIn("evidence_id", result(2))
        self.assertIn(str(evidence), result(3))
        self.assertGreaterEqual(len(json.loads(evidence.read_text())["records"]), 1, "the analysis is exported")
        for n in (4, 5):
            self.assertIn("is not writable by this agent", result(n))
        self.assertEqual(victim.read_text(), "precious")
        self.assertIn("3*x + 7", notes.read_text())

    @needs("FACTORY_REA_BIN", "rea")
    def test_under_bwrap_rea_runs_inside_the_sandbox(self, rea: str) -> None:
        if not bwrap_available():
            self.skipTest("bubblewrap is not usable here")
        cwd = make_repo(self.tmp)
        app = write_js_app(self.tmp / "case")
        evidence = cwd / "analysis" / "evidence" / "case-1.json"
        evidence.parent.mkdir(parents=True)   # without write_args nothing creates it
        victim = self.tmp / "victim.txt"
        victim.write_text("precious")
        call = lambda tool, **arguments: {"tool_calls": [{"name": f"mcp__rea__{tool}", "arguments": arguments}]}
        script = [call("analyze_javascript_application", input_path=str(app), detail="summary"),
                  call("export_evidence_bundle", path=str(evidence)),
                  call("export_evidence_bundle", path=str(victim), overwrite=True),
                  {"text": "done"}]
        unchecked = dataclasses.replace(rea_config(rea), write_args={})   # only the sandbox stands in the way
        run, requests = self.run_analyst(rea, cwd, script, unchecked, sandbox="bwrap")
        self.assertTrue(run.ok, run.error)
        result = lambda n: [m["content"] for m in requests[n]["messages"] if m.get("role") == "tool"][-1]
        self.assertIn("evidence_id", result(1), "REA analyses a case outside the checkout, read-only")
        self.assertIn(str(evidence), result(2))
        self.assertGreaterEqual(len(json.loads(evidence.read_text())["records"]), 1, "it writes the checkout")
        self.assertIn("could not be accessed", result(3))
        self.assertEqual(victim.read_text(), "precious", "REA cannot write outside what the agent's Bash can")


class _VisibleSandbox(Sandbox):
    """The real Sandbox, with extra roots under /tmp (hidden by bwrap's own /tmp) shown read-only."""

    roots: list

    def wrap(self, argv: list[str], cwd: Path | None = None) -> list[str]:
        args = super().wrap(argv, cwd)
        if self.mode == "bwrap":
            at = args.index("--tmpfs") + 2
            for root in self.roots:
                args[at:at] = ["--ro-bind", str(root), str(root)]
        return args


class MattPocockSkillsTest(TempHome):
    @needs("FACTORY_SKILLS_CLONE", None)
    def test_the_seven_skills_load_and_preload(self, clone: str) -> None:
        clone = Path(clone)
        index = SkillIndex([clone])
        self.assertEqual(index.errors, [])
        for name, (group, invocable) in SKILLS.items():
            skill = index.get(name)
            with self.subTest(skill=name):
                self.assertIsNotNone(skill)
                self.assertEqual(skill.dir, (clone / "skills" / group / name).resolve())
                self.assertEqual(skill.model_invocable, invocable)
                self.assertTrue(skill.description)

        repo = make_repo(self.tmp)   # the plan's install location: <repo>/.agents/skills/<name>
        for name, (group, _) in SKILLS.items():
            shutil.copytree(clone / "skills" / group / name, repo / ".agents" / "skills" / name)
        dirs = skill_dirs(None, repo, None)
        self.assertEqual(dirs[0], (repo / ".agents" / "skills").resolve())
        project = SkillIndex(dirs)
        self.assertEqual(sorted(skill.name for skill in project.list()), sorted(SKILLS))
        catalog = project.catalog_text()
        for name, (_, invocable) in SKILLS.items():
            self.assertEqual(f"- {name}:" in catalog, invocable, name)

        config = Config(self.tmp / "config.json", {})
        for agent_name, skill_name in (("builder", "tdd"), ("coder", "diagnosing-bugs")):
            agent = dataclasses.replace(parse_agent_file(PROJECT / "agents" / f"{agent_name}.md"), skills=[skill_name])
            ctx = RunContext(job={}, agent=agent, cwd=repo, job_dir=self.tmp / "job", write_paths=[repo],
                             read_roots=[repo], log_path=self.tmp / "log.jsonl", emit=lambda *a: None,
                             cancel=threading.Event(), model=config.model("opus"), limits=config.limits(),
                             skill_dirs=dirs)
            prompt = build_system_prompt(agent, ctx, project)
            skill = project.get(skill_name)
            with self.subTest(agent=agent_name):
                self.assertIn(f"# Skill: {skill_name}", prompt)
                self.assertIn(str(skill.dir), prompt, "the prompt says where the skill's other files are")
                self.assertIn(skill.body.strip()[:200], prompt)
                self.assertLess(prompt.index(agent.body.strip()[:80]), prompt.index(f"# Skill: {skill_name}"))
                self.assertEqual("# Skills\n" in prompt, agent_name == "coder", "only agents with Skill get a catalog")


def wait_for(predicate, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.2)
    return True


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass


class AgentBrowserTest(TempHome):
    def serve(self) -> str:
        site = self.tmp / "site"
        site.mkdir()
        (site / "index.html").write_text(TODO_PAGE)
        handler = functools.partial(_QuietHandler, directory=str(site))
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return f"http://127.0.0.1:{httpd.server_address[1]}/"

    def sandbox(self, mode: str, binary: str, socket_dir: str, sessions: tuple[str, ...] = ()) -> Sandbox:
        """A Sandbox like an agent's; the sessions are closed afterwards even if the test fails (the
        daemon starts its own process session, so Sandbox.cleanup() cannot reach it)."""
        env = {"AGENT_BROWSER_SOCKET_DIR": socket_dir, "AGENT_BROWSER_IDLE_TIMEOUT_MS": "120000",
               "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
        cwd = self.tmp / "work"
        cwd.mkdir(exist_ok=True)
        writable = [cwd]
        if mode == "bwrap":   # an install under /tmp is hidden by the sandbox's own /tmp: bind it back
            writable.append(Path(binary).resolve().parents[1])
        box = Sandbox(mode, cwd, writable, True, env)
        self.addCleanup(box.cleanup)
        for session in sessions:   # cleanups run last-in first-out: these close before box.cleanup()
            self.addCleanup(box.run, f"{shlex.quote(binary)} --session {session} close", 60)
        return box

    def run_ok(self, box: Sandbox, command: str) -> str:
        code, output, timed_out = box.run(command, 120)
        self.assertFalse(timed_out, command)
        self.assertEqual(code, 0, f"{command}\n{output}")
        return output

    @needs("FACTORY_AGENT_BROWSER", "agent-browser")
    def test_the_prompt_sequences_yield_useful_text(self, binary: str) -> None:
        url = self.serve()
        socket_dir = tempfile.mkdtemp(prefix="ab-", dir="/tmp")   # short: socket paths are capped at 103 bytes
        self.addCleanup(shutil.rmtree, socket_dir, True)
        box = self.sandbox("none", binary, socket_dir, ("t-ux", "t-other", "t-ui"))
        ab = shlex.quote(binary)
        self.assertIn("agent-browser", self.run_ok(box, f"{ab} --version"))
        self.assertIn("snapshot", self.run_ok(box, f"{ab} skills get core"))

        ux = f"{ab} --session t-ux"
        self.run_ok(box, f"{ux} open {url}")
        snapshot = self.run_ok(box, f"{ux} snapshot -i")
        self.assertIn('textbox "New item"', snapshot)
        self.assertIn('button "Add"', snapshot)
        self.assertEqual(self.run_ok(box, f"{ux} get text '#empty'").strip(), "Nothing to do yet.")
        self.run_ok(box, f"{ux} set viewport 390 844")
        self.run_ok(box, f"{ux} errors")
        self.run_ok(box, f"{ux} console")
        self.assertIn("about:blank", self.run_ok(box, f"{ab} --session t-other get url"),
                      "another session (or none) does not see the page: prompts pass --session every time")
        self.run_ok(box, f"{ab} --session t-other close")
        self.run_ok(box, f"{ux} close")

        ui = f"{ab} --session t-ui"
        self.run_ok(box, f"{ui} open {url}")
        styles = self.run_ok(box, f"{ui} get styles button | grep -E {shlex.quote(STYLE_FILTER)}")
        self.assertIn("background-color: rgb(47, 111, 222)", styles)
        self.assertIn("border-top-left-radius: 8px", styles)
        self.assertLess(styles.count("\n"), 40, "the filter keeps the output small")
        self.assertIn("width:", self.run_ok(box, f"{ui} get box .card"))
        self.run_ok(box, f"{ui} set viewport 390 844")
        overflow = self.run_ok(box, f'{ui} eval "document.documentElement.scrollWidth > innerWidth"')
        self.assertEqual(overflow.strip(), "false")
        self.assertIn("closed", self.run_ok(box, f"{ui} close").lower())

    @needs("FACTORY_AGENT_BROWSER", "agent-browser")
    def test_under_bwrap_open_check_and_close_in_one_call(self, binary: str) -> None:
        if not bwrap_available():
            self.skipTest("bubblewrap is not usable here")
        url = self.serve()
        binary = str(Path(binary).resolve())   # a node_modules/.bin link may sit outside what gets bound
        box = self.sandbox("bwrap", binary, "/tmp/agent-browser")   # inside the sandbox's own /tmp
        ab = f'ab={shlex.quote(binary + " --session t-bw")}; '
        live = lambda: _live_groups(list(box._groups))   # a call's PID namespace lives while its browser does

        # The prompts' shape: open, check and close in one call; nothing is left running.
        output = self.run_ok(box, ab + f"$ab open {url} && $ab snapshot -i && $ab get text '#empty'; $ab close")
        self.assertIn('button "Add"', output)
        self.assertIn("Nothing to do yet.", output)
        self.assertTrue(wait_for(lambda: not live(), 30), "the closed browser and its daemon exit")

        # Without close the browser keeps running after the call, and a later call cannot reach it.
        self.run_ok(box, ab + f"$ab open {url}")
        time.sleep(2)
        self.assertTrue(live(), "an unclosed browser outlives its Bash call")
        self.assertIn("about:blank", self.run_ok(box, ab + "$ab get url; $ab close"))
        self.assertTrue(live(), "the first call's browser is still running: only the run's cleanup stops it")
        box.cleanup()
        self.assertFalse(live())


if __name__ == "__main__":
    unittest.main()
