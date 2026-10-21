"""Live end-to-end tests of the external CLI drivers (DESIGN.md section 13) through the whole factory line.

Gated: run only with FACTORY_E2E_DRIVERS=1 (they launch real CLIs and, for Claude, real models):

    FACTORY_E2E_DRIVERS=1 python3 -m unittest tests.e2e.test_e2e_drivers -v

FACTORY_E2E_DRIVERS_ONLY=qwen,claude,claude-line,codex,env picks groups (default: all).
See tests/e2e/drivers_harness.py for the paths it uses and FACTORY_E2E_KEEP.

- Qwen Code (QWEN_BIN, 0.25.3) runs against tests/mock_llm.py's OpenAI endpoint: reviewer-ux on the
  qwen driver in one job, the builder on it in another (a CHANGES round, so the builder resumes its
  Qwen session with --resume).
- Claude Code runs on real models with this machine's login: reviewer-code on the claude driver
  (tier sonnet mapped to haiku) with the claude on PATH and with Claude Code 2.1.89; the builder on
  claude with a CHANGES round (so it resumes); and the whole line on claude.
- Codex CLI 0.163.0 is not run against a model (it needs a Responses API endpoint): the preset's
  argv is checked against `codex exec --help` / `codex exec resume --help`, and codex is launched with
  the preset's exact run and resume argv against a dead local provider to show that it accepts them.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

from swfactory.drivers.cli import HARNESS_NOTE, PRESETS
from tests.e2e import drivers_harness as h
from tests.mock_llm import MockLLM

E2E = os.environ.get("FACTORY_E2E_DRIVERS") == "1"
ONLY = {name.strip() for name in os.environ.get("FACTORY_E2E_DRIVERS_ONLY", "").split(",") if name.strip()}
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
QWEN_KEY = "sk-e2e-qwen-mock-key-0123456789"
DECOYS = {"E2E_DECOY_API_KEY": "decoy-api-key-value-0001", "DEEPSEEK_API_KEY": "decoy-deepseek-key-0002",
          "E2E_DECOY_SECRET": "decoy-secret-value-0003"}
QWEN_USAGE = {"prompt_tokens": 1000, "completion_tokens": 50, "total_tokens": 1050,
              "prompt_tokens_details": {"cached_tokens": 400}}
VERDICT_RE = re.compile(r"^VERDICT: (PASS|CHANGES)$")


def group(name: str) -> bool:
    return E2E and (not ONLY or name in ONLY)


def why_skipped(name: str) -> str:
    if not E2E:
        return "set FACTORY_E2E_DRIVERS=1 to run the live CLI driver tests"
    return f"group {name!r} not in FACTORY_E2E_DRIVERS_ONLY"


def strip_md(line: str) -> str:
    """The first-line normalisation of jobs.read_verdict (DESIGN.md section 5), for messages."""
    return line.strip().lstrip("﻿").strip("*_`").lstrip("#").strip()


class DriverE2ECase(unittest.TestCase):
    maxDiff = None

    def scenario(self, name: str) -> h.Scenario:
        scenario = h.Scenario(name)
        self.addCleanup(scenario.close)
        outcome = self._outcome
        self.addCleanup(lambda: setattr(scenario, "failed", scenario.failed or not _passed(outcome)))
        return scenario

    def assert_clean_policy(self, result: h.JobResult) -> None:
        policy = [w for w in result.warnings() if "write policy" in w]
        self.assertEqual(policy, [], "the orchestrator's write-policy check fired")
        self.assertNotIn("write policy", str(result.job.get("reason")))

    def assert_no_secret_in_logs(self, result: h.JobResult, values: list[str]) -> None:
        for log in (result.job_dir / "logs").glob("*.jsonl"):
            text = log.read_text(encoding="utf-8")
            for value in values:
                self.assertNotIn(value, text, f"{value[:6]}… leaked into {log.name}")
        self.assertNotIn(values[0], (result.job_dir / "job.json").read_text())

    def assert_cli_run_events(self, result: h.JobResult, run: str, driver: str, resume: bool) -> dict:
        """agent.start/output/text/usage/end of one CLI run; returns its agent.end data."""
        start = result.of("agent.start", run)
        self.assertEqual(len(start), 1, run)
        self.assertEqual(start[0]["data"]["driver"], driver)
        self.assertEqual(start[0]["data"]["resume"], resume, run)
        self.assertGreater(len(result.of("agent.output", run)), 2, f"{run}: no CLI output lines")
        self.assertTrue(result.of("agent.text", run), f"{run}: no agent.text")
        self.assertTrue(result.of("agent.usage", run), f"{run}: no agent.usage")
        end = result.of("agent.end", run)
        self.assertEqual(len(end), 1)
        self.assertTrue(end[0]["data"]["ok"], end[0]["data"].get("error"))
        self.assertGreater(end[0]["data"]["usage"]["input_tokens"], 0, run)
        self.assertGreater(end[0]["data"]["usage"]["output_tokens"], 0, run)
        return end[0]["data"]


def _passed(outcome) -> bool:
    """Best effort: did the running test pass so far (keeps a failed scenario's folder)."""
    result = getattr(outcome, "result", None)
    if result is None:
        return getattr(outcome, "success", True)
    return not (result.failures or result.errors)


def cli_results(launch: dict) -> list[dict]:
    """The CLI's own stream-json `result` events of one launch (full lines, from the wrapper's copy)."""
    return [e for e in h.launch_events(launch) if e.get("type") == "result"]


def stream_init(launch: dict) -> dict:
    """The CLI's stream-json system/init event of one launch."""
    return next((e for e in h.launch_events(launch) if e.get("type") == "system" and e.get("subtype") == "init"), {})


def openai_usage(raw: dict) -> dict:
    """The driver's normalisation for usage_style openai (DESIGN.md section 13)."""
    cached = int(raw.get("cache_read_input_tokens") or raw.get("cached_input_tokens") or 0)
    return {"input_tokens": int(raw.get("input_tokens") or 0), "output_tokens": int(raw.get("output_tokens") or 0),
            "cached_tokens": cached}


def anthropic_usage(raw: dict) -> dict:
    cached = int(raw.get("cache_read_input_tokens") or 0)
    return {"input_tokens": int(raw.get("input_tokens") or 0) + cached + int(raw.get("cache_creation_input_tokens") or 0),
            "output_tokens": int(raw.get("output_tokens") or 0), "cached_tokens": cached}


# -- A. Qwen Code against the mock -------------------------------------------------------


@unittest.skipUnless(group("qwen"), why_skipped("qwen"))
class QwenDriverE2E(DriverE2ECase):
    def setUp(self) -> None:
        if not h.QWEN_BIN.exists():
            self.skipTest(f"Qwen Code not installed at {h.QWEN_BIN}")
        self.version = subprocess.run([str(h.QWEN_BIN), "--version"], capture_output=True, text=True,
                                      timeout=60, check=False).stdout.strip()
        env = mock.patch.dict(os.environ, DECOYS)
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(h.kill_strays, str(h.QWEN_BIN.resolve().parent.parent))

    def qwen_setup(self, scenario: h.Scenario) -> tuple[MockLLM, h.FactoryScript, Path, Path, dict]:
        script = h.FactoryScript("qwen", usage=QWEN_USAGE)
        qwen_mock = MockLLM(handler=script)
        qwen_mock.start()
        self.addCleanup(qwen_mock.stop)
        self.addCleanup(script.dump, scenario.root / "qwen-mock-log.json")
        launches = scenario.root / "qwen-launches.jsonl"
        wrapper = h.write_recording_wrapper(scenario.root / "bin" / "qwen-rec", h.QWEN_BIN, launches)
        home = h.qwen_home(scenario.root)
        driver = h.qwen_driver(wrapper, home, qwen_mock.base_url, QWEN_KEY)
        return qwen_mock, script, launches, home, driver

    def expected_argv(self, result: h.JobResult, launch: dict, prompt: str, session: str, resume: bool) -> list[str]:
        """The qwen preset's argv (DESIGN.md section 13 table) for this run's prompt and session."""
        system = launch["argv"][launch["argv"].index("--append-system-prompt") + 1]
        flag = "--resume" if resume else "--session-id"
        return [prompt, "--yolo", "-m", "mock-model", "--append-system-prompt", system, "-o", "stream-json",
                flag, session, "--include-directories", str(result.job_dir)]

    def test_reviewer_ux_on_qwen(self) -> None:
        """reviewer-ux on the qwen driver (everything else builtin): argv, events, usage, review, merged."""
        scenario = self.scenario("qwen-reviewer-ux")
        qwen_mock, script, launches, home, driver = self.qwen_setup(scenario)
        config = scenario.config(drivers={"qwen": driver}, agents={"reviewer-ux": {"driver": "qwen"}})
        result = scenario.run_job(config, timeout=600)
        scenario.extra.update(qwen_version=self.version, qwen_requests=len(qwen_mock.requests),
                              offered_tools=sorted(set(script.offered_tools[0])) if script.offered_tools else [])
        scenario.record("done", result)
        job = result.job

        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertEqual(job["reviews"], {r: "PASS" for r in h.REVIEWERS})
        self.assertEqual(h.first_line(result.job_dir / "round-1" / "review-ux.md"), "VERDICT: PASS")
        self.assert_clean_policy(result)

        # The CLI was launched once, in the worktree, with the preset's argv.
        runs = launches_of(launches)
        self.assertEqual(len(runs), 1, runs)
        launch = runs[0]
        self.assertEqual(launch["cwd"], job["worktree"])
        session = h.arg_after(launch["argv"], "--session-id")
        self.assertRegex(session or "", UUID_RE)
        prompt = launch["argv"][0]
        self.assertTrue(prompt.startswith(f"Job folder: {result.job_dir}. Worktree: {job['worktree']}."), prompt)
        self.assertIn("Review round 1.", prompt)
        self.assertIn(f"Port: {job['port'] + 1}.", prompt)
        self.assertEqual(launch["argv"], self.expected_argv(result, launch, prompt, session, resume=False))
        system = h.arg_after(launch["argv"], "--append-system-prompt")
        self.assertTrue(system.startswith("You are the UX reviewer for one factory job"), system[:80])
        self.assertIn(HARNESS_NOTE.format(cwd=job["worktree"], job_dir=result.job_dir), system)

        # Environment: the CLI's own variables in, other secrets out.
        names = set(launch["env_names"])
        self.assertEqual(launch["env"]["HOME"], str(home))
        self.assertEqual(launch["env"]["OPENAI_BASE_URL"], qwen_mock.base_url + "/v1")
        self.assertIn("OPENAI_API_KEY", names)
        for name in [*DECOYS, "FACTORY_E2E_DRIVERS"]:
            self.assertNotIn(name, names)

        # Qwen's requests reached the mock with the agent body appended to Qwen's system prompt.
        self.assertTrue(qwen_mock.requests)
        first = qwen_mock.requests[0]
        self.assertEqual(first["path"], "/v1/chat/completions")
        self.assertEqual(first["model"], "mock-model")
        self.assertEqual(first["headers"].get("Authorization"), f"Bearer {QWEN_KEY}")
        self.assertIn("You are the UX reviewer for one factory job", first["messages"][0]["content"])
        offered = set(script.offered_tools[0])
        self.assertTrue({"write_file", "read_file", "run_shell_command", "edit"} <= offered, offered)

        # Events and usage of the run, as the GUI shows them.
        run = result.run_names("reviewer-ux")
        self.assertEqual(len(run), 1)
        end = self.assert_cli_run_events(result, run[0], "qwen", resume=False)
        calls = result.of("agent.tool_call", run[0])
        self.assertIn("write_file", [c["data"]["name"] for c in calls])
        self.assertTrue(all(r["data"]["ok"] for r in result.of("agent.tool_result", run[0])))
        reported = cli_results(launch)
        self.assertEqual(len(reported), 1)
        self.assertFalse(reported[0].get("is_error"), reported[0])
        expected = openai_usage(reported[0]["usage"])
        self.assertEqual({k: end["usage"][k] for k in expected}, expected)
        # Every request Qwen sent is in the session's reported usage. The factory turns Qwen's managed
        # auto-memory off (system settings beside the session file), so there is no background request.
        side = [e for e in script.log if e["agent"] is None]
        scenario.extra.update(qwen_side_requests=[e["last_user"][:120] for e in side])
        self.assertEqual(side, [], "Qwen sent background (auto-memory) requests")
        self.assertIn("QWEN_CODE_SYSTEM_SETTINGS_PATH", names)
        settings = json.loads((result.job_dir / "sessions" / "reviewer-ux.qwen-settings.json").read_text())
        self.assertFalse(settings["memory"]["enableManagedAutoMemory"])
        self.assertEqual(expected["input_tokens"], len(script.log) * QWEN_USAGE["prompt_tokens"],
                         "Qwen's reported input tokens are not the sum of its calls")
        self.assertEqual(job["agents"]["reviewer-ux"]["driver"], "qwen")
        self.assertEqual(job["agents"]["reviewer-ux"]["session"], session)
        totals = json.loads((result.job_dir / "sessions" / "reviewer-ux.cli.json").read_text())
        self.assertEqual(totals["session"], session)
        self.assertGreaterEqual(job["usage"]["input_tokens"], expected["input_tokens"])
        self.assert_no_secret_in_logs(result, [QWEN_KEY, *DECOYS.values()])

    def test_builder_on_qwen_resumes_after_changes(self) -> None:
        """The builder on qwen; reviewer-code asks for CHANGES in round 1, so the builder resumes its Qwen
        session (--resume <session>) and the job merges after round 2."""
        scenario = self.scenario("qwen-builder-resume")
        scenario.builtin.verdicts[("code", 1)] = "CHANGES"
        qwen_mock, script, launches, _home, driver = self.qwen_setup(scenario)
        config = scenario.config(drivers={"qwen": driver}, agents={"builder": {"driver": "qwen"}})
        result = scenario.run_job(config, timeout=600)
        scenario.extra.update(qwen_version=self.version, qwen_requests=len(qwen_mock.requests))
        scenario.record("done", result)
        job = result.job

        self.assertEqual(job["stage"], "merged", job.get("reason"))
        self.assertEqual(job["round"], 2)
        self.assertEqual(job["history"][0]["reviews"]["code"], "CHANGES")
        self.assertEqual(job["reviews"], {r: "PASS" for r in h.REVIEWERS})
        self.assert_clean_policy(result)
        log = h.git(scenario.repo, "log", "--format=%s", "main")
        self.assertIn("Add clearDone to remove completed items", log)
        self.assertIn("Test clearDone on an empty list", log)
        self.assertIn("clearDone([])", (scenario.repo / "test" / "todo.test.js").read_text())

        runs = launches_of(launches)
        self.assertEqual(len(runs), 2, [r["argv"][0][:60] for r in runs])
        first, second = runs
        session = h.arg_after(first["argv"], "--session-id")
        self.assertRegex(session or "", UUID_RE)
        self.assertIn("Build round 1.", first["argv"][0])
        self.assertEqual(first["argv"], self.expected_argv(result, first, first["argv"][0], session, resume=False))
        resume_prompt = second["argv"][0]
        self.assertIn(f"Round 1 reviews are in {result.job_dir}/round-1/.", resume_prompt)
        self.assertEqual(second["argv"], self.expected_argv(result, second, resume_prompt, session, resume=True))
        self.assertNotIn("--session-id", second["argv"])
        self.assertEqual(h.arg_after(second["argv"], "--append-system-prompt"),
                         h.arg_after(first["argv"], "--append-system-prompt"))

        # The resumed launch carried the first launch's conversation (the session really resumed).
        resumed = [r for r in qwen_mock.requests if "Round 1 reviews are in" in json.dumps(r["messages"])]
        self.assertTrue(resumed)
        history = json.dumps(resumed[0]["messages"])
        self.assertIn("Build round 1.", history)
        self.assertIn("Add clearDone to remove completed items", history)
        self.assertIn("You build one factory job", resumed[0]["messages"][0]["content"],
                      "the resumed launch lost the agent body (--append-system-prompt)")

        builder_runs = result.run_names("builder")
        self.assertEqual(len(builder_runs), 2)
        end1 = self.assert_cli_run_events(result, builder_runs[0], "qwen", resume=False)
        end2 = self.assert_cli_run_events(result, builder_runs[1], "qwen", resume=True)
        self.assertIn("run_shell_command", [c["data"]["name"] for c in result.of("agent.tool_call", builder_runs[0])])
        # Usage: Qwen reports the session's running total; the driver reports each launch's share.
        total1 = openai_usage(cli_results(first)[-1]["usage"])
        total2 = openai_usage(cli_results(second)[-1]["usage"])
        self.assertEqual({k: end1["usage"][k] for k in total1}, total1)
        launch2 = {k: total2[k] - total1[k] for k in total2}
        self.assertEqual({k: end2["usage"][k] for k in launch2}, launch2)
        calls1 = sum(1 for e in script.log if e["time"] < second["time"])
        calls2 = sum(1 for e in script.log if e["time"] >= second["time"])
        self.assertEqual(end1["usage"]["input_tokens"], calls1 * QWEN_USAGE["prompt_tokens"])
        self.assertEqual(end2["usage"]["input_tokens"], calls2 * QWEN_USAGE["prompt_tokens"])
        record = job["agents"]["builder"]
        self.assertEqual((record["driver"], record["session"], record["runs"]), ("qwen", session, 2))
        scenario.record("usage", result, builder_usage=[end1["usage"], end2["usage"]],
                        qwen_totals=[total1, total2])
        self.assert_no_secret_in_logs(result, [QWEN_KEY, *DECOYS.values()])


def launches_of(log: Path) -> list[dict]:
    return h.read_launches(log)


def launch_agent(launch: dict) -> str | None:
    """The agent a CLI launch ran, from the agent body in its system text (--append-system-prompt or a
    prepended prompt)."""
    text = " ".join(launch["argv"])
    return next((name for marker, name in h.AGENT_MARKERS.items() if marker in text), None)


def pair_launches(result: h.JobResult, launches: list[dict]) -> dict[str, dict]:
    """{run stem: launch}: each agent's runs and launches in order (parallel reviewers start in any order)."""
    paired: dict[str, dict] = {}
    for agent in {launch_agent(launch) for launch in launches}:
        mine = sorted((l for l in launches if launch_agent(l) == agent), key=lambda l: l["time"])
        paired.update(zip(result.run_names(agent or ""), mine))
    return paired


def tool_trace(result: h.JobResult, run: str) -> list[str]:
    """One line per tool call of a run: the tool and the path or command it touched (for the report)."""
    lines = []
    for event in result.of("agent.tool_call", run):
        data = event["data"]
        args = data.get("input") or {}
        target = args.get("file_path") or args.get("path") or args.get("command") or args.get("pattern") or ""
        lines.append(f"{data.get('name')}: {str(target)[:160]}")
    return lines


# -- B. Claude Code on real models ------------------------------------------------------------


CLAUDE_MODELS = {"opus": "sonnet", "sonnet": "haiku"}


class ClaudeCase(DriverE2ECase):
    def claude_setup(self, scenario: h.Scenario, binary: Path | str, models: dict) -> tuple[Path, dict, str]:
        real = Path(binary).resolve()
        version = subprocess.run([str(real), "--version"], capture_output=True, text=True, timeout=60, check=False).stdout.strip()
        launches = scenario.root / "claude-launches.jsonl"
        wrapper = h.write_recording_wrapper(scenario.root / "bin" / "claude-rec", real, launches)
        scenario.extra.update(claude_version=version, claude_bin=str(binary))
        self.addCleanup(h.kill_strays, str(scenario.root))
        return launches, h.claude_driver(wrapper, models), version

    def expected_argv(self, result: h.JobResult, launch: dict, model: str, session: str, resume: bool) -> list[str]:
        argv = launch["argv"]
        prompt, system = h.arg_after(argv, "-p"), h.arg_after(argv, "--append-system-prompt")
        if resume:
            return ["-p", prompt, "--resume", session, "--output-format", "stream-json", "--verbose",
                    "--append-system-prompt", system, "--model", model, "--permission-mode", "bypassPermissions",
                    "--add-dir", str(result.job_dir)]
        return ["-p", prompt, "--output-format", "stream-json", "--verbose", "--append-system-prompt", system,
                "--model", model, "--permission-mode", "bypassPermissions", "--session-id", session,
                "--add-dir", str(result.job_dir)]

    def assert_claude_run(self, result: h.JobResult, run: str, launch: dict, model: str, session: str,
                          resume: bool) -> dict:
        self.assertEqual(launch["argv"], self.expected_argv(result, launch, model, session, resume))
        self.assertEqual(launch["cwd"], result.job["worktree"])
        names = set(launch["env_names"])
        for name in [*DECOYS, "FACTORY_E2E_DRIVERS", "CLAUDE_CODE_SESSION_ID", "CLAUDECODE"]:
            self.assertNotIn(name, names)
        end = self.assert_cli_run_events(result, run, "claude", resume=resume)
        init = stream_init(launch)
        self.assertEqual(init.get("session_id"), session, f"{run}: claude ran another session")
        reported = cli_results(launch)
        self.assertEqual(len(reported), 1, run)
        self.assertFalse(reported[0].get("is_error"), reported[0])
        expected = anthropic_usage(reported[0]["usage"])
        self.assertEqual({k: end["usage"][k] for k in expected}, expected, f"{run}: usage")
        self.assertEqual(reported[0].get("session_id"), session)
        return {"run": run, "model": init.get("model"), "version": init.get("claude_code_version"), "seconds": round(_end_seconds(result, run), 1),
                "usage": end["usage"], "cost_usd": reported[0].get("total_cost_usd"),
                "tools": tool_trace(result, run), "final": (reported[0].get("result") or "")[:300]}

    def assert_review_file(self, result: h.JobResult, lens: str, n: int) -> str:
        path = result.job_dir / f"round-{n}" / f"review-{lens}.md"
        self.assertTrue(path.exists(), f"{path} missing")
        line = h.first_line(path)
        self.assertRegex(strip_md(line), VERDICT_RE, f"first line of {path.name}: {line!r}")
        return line


def _end_seconds(result: h.JobResult, run: str) -> float:
    ends = result.of("agent.end", run)
    return float(ends[0]["data"].get("seconds") or 0) if ends else 0.0


@unittest.skipUnless(group("claude"), why_skipped("claude"))
class ClaudeDriverE2E(ClaudeCase):
    """Real Claude models through the claude driver, one agent at a time; the rest is scripted."""

    def setUp(self) -> None:
        env = mock.patch.dict(os.environ, DECOYS)
        env.start()
        self.addCleanup(env.stop)

    def reviewer_code_on(self, name: str, binary: Path | str | None) -> None:
        if not binary or not Path(binary).exists():
            self.skipTest(f"no claude binary for {name}")
        scenario = self.scenario(name)
        launches, driver, _version = self.claude_setup(scenario, binary, {"sonnet": "haiku"})
        config = scenario.config(drivers={"claude": driver}, agents={"reviewer-code": {"driver": "claude"}},
                                 pipeline={"max_resumes": 1})
        result = scenario.run_job(config, timeout=1800)
        job = result.job
        runs = result.run_names("reviewer-code")
        details = []
        try:
            self.assertTrue(runs, job.get("reason"))
            all_launches = launches_of(launches)
            self.assertEqual(len(all_launches), len(runs), "one claude launch per reviewer-code run")
            for run, launch in zip(runs, all_launches):
                session = h.arg_after(launch["argv"], "--session-id")
                self.assertRegex(session or "", UUID_RE)
                prompt = h.arg_after(launch["argv"], "-p")
                self.assertTrue(prompt.startswith(f"Job folder: {result.job_dir}. "), prompt[:80])
                details.append(self.assert_claude_run(result, run, launch, "haiku", session, resume=False))
            for n in range(1, job["round"] + 1):
                details[n - 1]["first_line"] = self.assert_review_file(result, "code", n)
            self.assert_clean_policy(result)
            self.assertNotIn("reviewer edits", " ".join(result.warnings()), "reviewer-code edited the worktree")
            self.assertEqual(job["agents"]["reviewer-code"]["driver"], "claude")
            # The job went on: merged after PASS, or the reviewer's CHANGES sent it round the line.
            if job["stage"] != "merged":
                self.assertEqual(job["stage"], "needs-human", job.get("reason"))
                self.assertIn("review rounds exhausted", job.get("reason") or "")
            self.assertIn(job["reviews"]["code"], ("PASS", "CHANGES"))
            self.assert_no_secret_in_logs(result, list(DECOYS.values()))
        finally:
            scenario.record("done", result, claude_runs=details)

    def test_reviewer_code_on_claude_path(self) -> None:
        """reviewer-code on the claude on PATH (sonnet tier mapped to haiku), reviewing a real diff."""
        self.reviewer_code_on("claude-path-reviewer-code", h.which("claude"))

    def test_reviewer_code_on_claude_2189(self) -> None:
        """reviewer-code on Claude Code 2.1.89 (closest to the owner's 2.1.88)."""
        self.reviewer_code_on("claude2189-reviewer-code", h.CLAUDE_2189_BIN)

    def test_builder_on_claude_resumes_after_changes(self) -> None:
        """The builder on claude (a real model edits the todo app and commits); reviewer-code asks for
        CHANGES in round 1, so the builder's Claude session is resumed with --resume."""
        binary = os.environ.get("FACTORY_E2E_CLAUDE_BUILDER_BIN") or str(h.CLAUDE_2189_BIN)
        if not Path(binary).exists():
            self.skipTest(f"no claude binary at {binary}")
        scenario = self.scenario("claude-builder-resume")
        scenario.builtin.verdicts[("code", 1)] = "CHANGES"
        launches, driver, _version = self.claude_setup(scenario, binary, CLAUDE_MODELS)
        config = scenario.config(drivers={"claude": driver}, agents={"builder": {"driver": "claude"}})
        result = scenario.run_job(config, timeout=2400)
        job = result.job
        details = []
        try:
            runs = result.run_names("builder")
            all_launches = launches_of(launches)
            self.assertGreaterEqual(len(runs), 2, job.get("reason"))
            self.assertEqual(len(all_launches), len(runs))
            session = h.arg_after(all_launches[0]["argv"], "--session-id")
            self.assertRegex(session or "", UUID_RE)
            details.append(self.assert_claude_run(result, runs[0], all_launches[0], "sonnet", session, resume=False))
            for run, launch in zip(runs[1:], all_launches[1:]):
                self.assertEqual(h.arg_after(launch["argv"], "--resume"), session)
                details.append(self.assert_claude_run(result, run, launch, "sonnet", session, resume=True))
            resume_prompts = [h.arg_after(launch["argv"], "-p") for launch in all_launches[1:]]
            self.assertTrue(any(f"Round 1 reviews are in {result.job_dir}/round-1/." in p for p in resume_prompts),
                            resume_prompts)
            self.assertEqual(job["stage"], "merged", job.get("reason"))
            self.assertEqual(job["round"], 2)
            self.assertEqual(h.first_line(result.job_dir / "build.md"), "STATUS: READY")
            self.assert_clean_policy(result)
            main = h.git(scenario.repo, "log", "--format=%s", f"{job['base_commit']}..main")
            self.assertGreaterEqual(len([s for s in main.splitlines() if not s.startswith("Merge")]), 2, main)
            self.assertIn("clearDone", (scenario.repo / "src" / "todo.js").read_text())
            tests = subprocess.run(["node", "--test"], cwd=scenario.repo, capture_output=True, text=True, timeout=120,
                                   check=False)
            self.assertEqual(tests.returncode, 0, tests.stdout[-2000:])
            self.assertEqual(job["agents"]["builder"]["session"], session)
        finally:
            scenario.record("done", result, claude_runs=details,
                            main_log=h.git(scenario.repo, "log", "--oneline", "-8", "main"))


@unittest.skipUnless(group("claude-line"), why_skipped("claude-line"))
class ClaudeLineE2E(ClaudeCase):
    """Every agent of the line on the claude driver (opus tier -> sonnet, sonnet tier -> haiku)."""

    def test_whole_line_on_claude(self) -> None:
        binary = os.environ.get("FACTORY_E2E_CLAUDE_LINE_BIN") or h.which("claude")
        if not binary or not Path(binary).exists():
            self.skipTest("no claude binary")
        label = "claude-line-" + ("2189" if "cc2189" in str(binary) else "path")
        scenario = self.scenario(label)
        launches, driver, _version = self.claude_setup(scenario, binary, CLAUDE_MODELS)
        config = scenario.config(drivers={"claude": driver}, agents={"*": {"driver": "claude"}},
                                 pipeline={"max_resumes": 1})
        result = scenario.run_job(config, timeout=3600)
        job = result.job
        details = {}
        try:
            all_launches = launches_of(launches)
            names = sorted(result.runs)
            self.assertEqual(len(all_launches), len(names))
            by_run = pair_launches(result, all_launches)
            self.assertEqual(sorted(by_run), names)
            sessions: dict[str, str] = {}
            for run in names:
                agent = run.split("-", 1)[1]
                launch = by_run[run]
                tier = "opus" if agent in ("spec-writer", "builder", "approver", "reviewer-security") else "sonnet"
                model = CLAUDE_MODELS[tier]
                resume = "--resume" in launch["argv"]
                session = h.arg_after(launch["argv"], "--resume" if resume else "--session-id")
                if resume:
                    self.assertEqual(session, sessions.get(agent), f"{run} resumed another session")
                sessions.setdefault(agent, session)
                details[run] = self.assert_claude_run(result, run, launch, model, session, resume=resume)
            self.assertTrue((result.job_dir / "spec.md").read_text().strip())
            self.assertIn(h.first_line(result.job_dir / "build.md"), ("STATUS: READY", "STATUS: BLOCKED"))
            for n in range(1, job["round"] + 1):
                for lens in h.REVIEWERS:
                    details.setdefault("first_lines", {})[f"round-{n}/review-{lens}.md"] = \
                        self.assert_review_file(result, lens, n)
            if (result.job_dir / "decision.md").exists():
                details["decision"] = h.first_line(result.job_dir / "decision.md")
            self.assert_clean_policy(result)
            self.assertNotIn("reviewer edits", " ".join(result.warnings()))
            self.assertNotIn("approver edits", " ".join(result.warnings()))
            self.assertEqual(job["stage"], "merged", job.get("reason"))
        finally:
            details["warnings"] = result.warnings()
            details["job_files"] = h.job_files(result.job_dir)
            scenario.record("done", result, claude_runs=details,
                            main_log=h.git(scenario.repo, "log", "--oneline", "-8", "main"))


# -- Environment scrubbing (DESIGN.md section 13) ------------------------------------------------


@unittest.skipUnless(group("env"), why_skipped("env"))
class DriverEnvE2E(unittest.TestCase):
    """What a CLI agent's environment keeps of the server's secrets, through the real CliRunner._env."""

    CREDENTIALS: ClassVar[dict] = {"AWS_SECRET_ACCESS_KEY": "aws-secret-value-000001", "STRIPE_SECRET_KEY": "sk_live_e2e_000002",
                   "DATABASE_PASSWORD": "db-password-value-000003", "PGPASSWORD": "pg-password-value-000004"}

    def runner_env(self, preset: str) -> dict:
        import threading
        from types import SimpleNamespace

        from swfactory.drivers.cli import CliRunner
        from swfactory.runners import RunContext

        driver = SimpleNamespace(name=preset, type="cli", preset=preset, command=preset, run_args=None,
                                 resume_args=None, env={}, models={}, timeout=None, profile=None)
        ctx = RunContext(job={}, agent=SimpleNamespace(name="builder", body=""), cwd=Path("."), job_dir=Path("."),
                         write_paths=[], read_roots=[], log_path=Path("x.jsonl"), emit=lambda *a: None,
                         cancel=threading.Event(), model=SimpleNamespace(key="opus", model="m"),
                         limits=SimpleNamespace(max_wall_seconds=1))
        with mock.patch.dict(os.environ, {**self.CREDENTIALS, **DECOYS}):
            return CliRunner(SimpleNamespace(path=Path("config.json"), raw={}), driver)._env(ctx)

    def test_documented_secret_names_are_removed(self) -> None:
        for preset in ("qwen", "claude", "codex"):
            env = self.runner_env(preset)
            for name in DECOYS:
                self.assertNotIn(name, env, f"{preset}: {name}")

    def test_common_credential_names_are_removed(self) -> None:
        """Common credential names (AWS_SECRET_ACCESS_KEY, *_SECRET_KEY, *PASSWORD) never reach an agent CLI
        (nor builtin Bash): the scrub rule covers credential-looking names, not only *_API_KEY/_TOKEN/_SECRET."""
        for preset in ("qwen", "claude", "codex"):
            env = self.runner_env(preset)
            leaked = sorted(name for name in self.CREDENTIALS if name in env)
            self.assertEqual(leaked, [], f"{preset} CLI environment keeps {leaked}")


# -- C. Codex: argv only, no model --------------------------------------------------------------


def codex_help(*args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OPENAI_", "CODEX_"))}
    return subprocess.run([str(h.CODEX_BIN), *args, "--help"], capture_output=True, text=True, timeout=60,
                          env=env, check=False).stdout


def help_options(text: str) -> set[str]:
    """Every -x / --long option a clap help text lists."""
    found: set[str] = set()
    for short, long in re.findall(r"(?m)^\s+(?:(-\w), )?(--[\w-]+)", text):
        found.update(flag for flag in (short, long) if flag)
    return found


@unittest.skipUnless(group("codex"), why_skipped("codex"))
class CodexPresetE2E(unittest.TestCase):
    """What can be verified about the codex preset without a Responses-API model."""

    def setUp(self) -> None:
        if not h.CODEX_BIN.exists():
            self.skipTest(f"Codex CLI not installed at {h.CODEX_BIN}")
        self.version = subprocess.run([str(h.CODEX_BIN), "--version"], capture_output=True, text=True,
                                      timeout=60, check=False).stdout.strip()

    def test_version(self) -> None:
        self.assertEqual(self.version, "codex-cli 0.163.0")

    def test_run_args_are_exec_options(self) -> None:
        """Every option in the preset's run_args is a `codex exec` option, the prompt is exec's [PROMPT]."""
        exec_help = codex_help("exec")
        self.assertIn("Usage: codex exec [OPTIONS] [PROMPT]", exec_help)
        options = help_options(exec_help)
        run_args = PRESETS["codex"]["run_args"]
        self.assertEqual(run_args[0], "exec")
        self.assertEqual(run_args[-1], "{prompt}")
        flags = [a for a in run_args if a.startswith("-")]
        self.assertEqual(flags, ["--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check", "--json",
                                 "-m", "-C", "--add-dir"])
        self.assertEqual(sorted(set(flags) - options), [], "run_args flags codex exec does not know")
        self.assertNotIn("--full-auto", options)
        self.assertIn("--profile", options)        # driver profile inserts '--profile <name>' after exec

    def test_resume_args_order(self) -> None:
        """resume_args: exec options, then `resume {session_id} {prompt}`; -C/--add-dir/--profile are exec-only."""
        exec_options = help_options(codex_help("exec"))
        resume_help = codex_help("exec", "resume")
        self.assertIn("Usage: codex exec resume [OPTIONS] [SESSION_ID] [PROMPT]", resume_help)
        resume_options = help_options(resume_help)
        args = PRESETS["codex"]["resume_args"]
        at = args.index("resume")
        self.assertEqual(args[at:], ["resume", "{session_id}", "{prompt}"])
        before = [a for a in args[:at] if a.startswith("-")]
        self.assertEqual(sorted(set(before) - exec_options), [])
        for exec_only in ("-C", "--add-dir", "--profile"):
            self.assertNotIn(exec_only, resume_options)
        for both in ("-m", "--json", "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check"):
            self.assertIn(both, resume_options)

    def test_codex_accepts_the_preset_argv_without_a_model(self) -> None:
        """Launch codex with the driver's exact run and resume argv (profile e2e) against a local endpoint
        that records each request and answers 404: codex parses the argv, starts and then resumes the same
        thread, and sends the prompt (system text prepended) and, on resume, the earlier turn. No model."""
        import tempfile
        import threading
        from types import SimpleNamespace

        from swfactory.drivers.cli import CliRunner
        from swfactory.runners import RunContext

        recorder = MockLLM()
        recorder.start()
        self.addCleanup(recorder.stop)
        h.RUNS_DIR.mkdir(parents=True, exist_ok=True)
        root = Path(tempfile.mkdtemp(prefix="codex-argv-", dir=h.RUNS_DIR))
        if os.environ.get("FACTORY_E2E_KEEP") != "1":
            self.addCleanup(shutil.rmtree, root, True)
        codex_home = root / "codex-home"
        codex_home.mkdir(parents=True)
        provider = ('model_provider = "recorder"\n[model_providers.recorder]\nname = "recorder"\n'
                    f'base_url = "{recorder.base_url}/v1"\nwire_api = "responses"\nenv_key = "REC_API_KEY"\n'
                    'request_max_retries = 0\nstream_max_retries = 0\n')
        (codex_home / "config.toml").write_text('check_for_update_on_startup = false\n[analytics]\nenabled = false\n')
        (codex_home / "e2e.config.toml").write_text(provider)
        worktree = root / "wt"
        worktree.mkdir()
        subprocess.run(["git", "init", "-q", str(worktree)], check=True)
        job_dir = root / "job"
        job_dir.mkdir()
        launches = root / "codex-launches.jsonl"
        wrapper = h.write_recording_wrapper(root / "bin" / "codex-rec", h.CODEX_BIN, launches)
        driver = SimpleNamespace(name="codex", type="cli", preset="codex", command=str(wrapper), run_args=None,
                                 resume_args=None, models={"opus": "recorded-model"}, timeout=30, profile="e2e",
                                 env={"CODEX_HOME": str(codex_home), "HOME": str(root),
                                      "REC_API_KEY": "rec-key-1234567890", "HTTPS_PROXY": h.DEAD_PROXY,
                                      "HTTP_PROXY": h.DEAD_PROXY, "NO_PROXY": "127.0.0.1,localhost"})
        events: list[tuple[str, dict]] = []
        body = "- Build it (body starts with a dash)."
        ctx = RunContext(job={}, agent=SimpleNamespace(name="builder", body=body),
                         cwd=worktree, job_dir=job_dir, write_paths=[worktree], read_roots=[worktree],
                         log_path=job_dir / "log.jsonl", emit=lambda t, d: events.append((t, d)),
                         cancel=threading.Event(), model=SimpleNamespace(key="opus", model="unused"),
                         limits=SimpleNamespace(max_wall_seconds=30), session_path=job_dir / "sessions" / "builder.json")
        runner = CliRunner(SimpleNamespace(path=root / "config.json", raw={}), driver)
        started = time.monotonic()
        first = runner.run(ctx, "Job folder: x. Build round 1.")
        run_seconds = time.monotonic() - started
        outputs = [d["line"] for t, d in events if t == "agent.output"]
        first_requests = list(recorder.requests)
        events.clear()
        second = runner.resume(ctx, first.session_ref or "", "-Round 1 reviews are in x.")
        outputs2 = [d["line"] for t, d in events if t == "agent.output"]
        resume_requests = recorder.requests[len(first_requests):]
        recorded = h.read_launches(launches)
        Path(h.RESULTS).parent.mkdir(parents=True, exist_ok=True)
        with open(h.RESULTS, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"scenario": "codex-argv", "version": self.version, "run_error": first.error,
                                 "resume_error": second.error, "session": first.session_ref,
                                 "resume_session": second.session_ref, "run_seconds": round(run_seconds, 1),
                                 "requests": [(r["path"], r.get("model")) for r in recorder.requests],
                                 "first_lines": outputs[:8], "resume_lines": outputs2[:8],
                                 "argv": [r["argv"][:12] for r in recorded]}) + "\n")

        self.assertFalse(first.ok)                      # the endpoint answers 404: no model ran
        self.assertRegex(first.session_ref or "", r"^[0-9a-f-]{36}$", "no thread.started: codex rejected the argv?")
        for lines in (outputs, outputs2):
            self.assertFalse(any("unexpected argument" in line or line.startswith("Usage:") for line in lines),
                             lines[:5])
        self.assertEqual(len(recorded), 2)
        run_argv, resume_argv = recorded[0]["argv"], recorded[1]["argv"]
        self.assertEqual(run_argv[:3], ["exec", "--profile", "e2e"])
        self.assertEqual(run_argv[3:9], ["--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check",
                                         "--json", "-m", "recorded-model", "-C"])
        self.assertTrue(run_argv[-1].startswith(" " + body), "a prompt starting with '-' gets a leading space")
        self.assertTrue(run_argv[-1].endswith("\n\n---\n\nJob folder: x. Build round 1."))
        self.assertEqual(resume_argv[:3], ["exec", "--profile", "e2e"])
        self.assertEqual(resume_argv[-3:], ["resume", first.session_ref, " -Round 1 reviews are in x."])
        # Codex sent the prompt to the profile's provider, with the agent body prepended (system_mode prepend).
        self.assertTrue(first_requests, "codex sent no request to the profile's provider")
        self.assertEqual(first_requests[0]["path"], "/v1/responses")
        self.assertEqual(first_requests[0].get("model"), "recorded-model")
        sent = json.dumps(first_requests[0].body)
        self.assertIn("Job folder: x. Build round 1.", sent)
        self.assertIn("Build it (body starts with a dash).", sent)
        # The resume continued the same thread and carried the first turn's prompt.
        self.assertEqual(second.session_ref, first.session_ref)
        self.assertTrue(resume_requests, "codex resume sent no request")
        resent = json.dumps(resume_requests[0].body)
        self.assertIn("-Round 1 reviews are in x.", resent)
        self.assertIn("Job folder: x. Build round 1.", resent)
