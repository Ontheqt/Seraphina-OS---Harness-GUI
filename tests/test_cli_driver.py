"""Tests for swfactory/drivers/cli.py. tests/fake_cli.py stands in for the agent CLIs."""

from __future__ import annotations

import json
import os
import shlex
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from swfactory.drivers import cli
from swfactory.drivers.cli import PRESETS, CliRunner
from swfactory.gridcard import factory_grid
from swfactory.harness import sandbox
from swfactory.runners import AgentRun, RunContext, make_runner

FAKE_CLI = Path(__file__).resolve().parent / "fake_cli.py"
FAKE_COMMAND = f"{shlex.quote(sys.executable)} {shlex.quote(str(FAKE_CLI))}"
SID = "11111111-2222-4333-8444-555555555555"
NASTY_PROMPT = 'Job folder: /j. Say "hi" \'there\' $(rm -rf /) `id` ; | & > x\nsecond line\n'

# Trimmed output captured on 2026-10-20 from the real CLIs running against local API stubs.
CLAUDE_2189_SAMPLE = """\
{"type":"system","subtype":"init","cwd":"/work","session_id":"6f0e8a2e-6a1c-4c55-9a3f-0e5e0f1d2a11","model":"claude-sonnet-4-6","permissionMode":"bypassPermissions","apiKeySource":"ANTHROPIC_API_KEY","claude_code_version":"2.1.89"}
{"type":"assistant","message":{"id":"msg_1","type":"message","role":"assistant","model":"claude-sonnet-4-6","content":[{"type":"tool_use","id":"toolu_01","name":"Write","input":{"file_path":"/work/out.txt","content":"hello\\n"}}],"stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":11,"output_tokens":1,"cache_read_input_tokens":3,"cache_creation_input_tokens":0},"context_management":null},"parent_tool_use_id":null,"session_id":"6f0e8a2e-6a1c-4c55-9a3f-0e5e0f1d2a11","uuid":"5be8f692-7f2f-4f4a-8758-81ffcee8888b"}
{"type":"user","message":{"role":"user","content":[{"tool_use_id":"toolu_01","type":"tool_result","content":"File created successfully at: /work/out.txt"}]},"parent_tool_use_id":null,"session_id":"6f0e8a2e-6a1c-4c55-9a3f-0e5e0f1d2a11","uuid":"d0c99ff3-6fb5-4532-b5e1-a578620a3013"}
{"type":"assistant","message":{"id":"msg_2","type":"message","role":"assistant","model":"claude-sonnet-4-6","content":[{"type":"text","text":"STUB-DONE all good"}],"stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":11,"output_tokens":1,"cache_read_input_tokens":3,"cache_creation_input_tokens":0},"context_management":null},"parent_tool_use_id":null,"session_id":"6f0e8a2e-6a1c-4c55-9a3f-0e5e0f1d2a11","uuid":"6d15cb90-baa1-48ab-bf2c-ced17276ad5e"}
{"type":"result","subtype":"success","is_error":false,"duration_ms":251,"num_turns":2,"result":"STUB-DONE all good","stop_reason":"end_turn","session_id":"6f0e8a2e-6a1c-4c55-9a3f-0e5e0f1d2a11","total_cost_usd":0.0002778,"usage":{"input_tokens":22,"cache_creation_input_tokens":0,"cache_read_input_tokens":6,"output_tokens":14},"permission_denials":[]}
"""
QWEN_0253_SAMPLE = """\
{"type":"system","subtype":"init","session_id":"7a0e8a2e-6a1c-4c55-9a3f-0e5e0f1d2a22","cwd":"/work","model":"stub-model","permission_mode":"yolo"}
{"type":"stream_event","uuid":"bf193e0c-a55d-430c-86c2-a94fa8a1ea32","session_id":"7a0e8a2e-6a1c-4c55-9a3f-0e5e0f1d2a22","parent_tool_use_id":null,"event":{"type":"goal_state","goal_state":{"v":2,"goal":null,"activity":"idle"}}}
{"type":"assistant","uuid":"4615a88f-ffff-4116-917d-15cdc1262eb7","session_id":"7a0e8a2e-6a1c-4c55-9a3f-0e5e0f1d2a22","parent_tool_use_id":null,"message":{"id":"4615a88f-ffff-4116-917d-15cdc1262eb7","type":"message","role":"assistant","model":"stub-model","content":[{"type":"tool_use","id":"call_1","name":"write_file","input":{"file_path":"/work/out.txt","content":"hello\\n"}}],"stop_reason":"tool_use","usage":{"input_tokens":20,"output_tokens":5,"cache_read_input_tokens":4,"total_tokens":25}}}
{"type":"user","uuid":"37cabf39-d8e9-4ddc-82f2-1f4060de937f","session_id":"7a0e8a2e-6a1c-4c55-9a3f-0e5e0f1d2a22","parent_tool_use_id":null,"message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"call_1","is_error":false,"content":"Successfully created and wrote to new file: /work/out.txt."}]}}
{"type":"assistant","uuid":"12d67f0f-1fe7-4c33-93c2-1eb0fb5b81a0","session_id":"7a0e8a2e-6a1c-4c55-9a3f-0e5e0f1d2a22","parent_tool_use_id":null,"message":{"id":"12d67f0f-1fe7-4c33-93c2-1eb0fb5b81a0","type":"message","role":"assistant","model":"stub-model","content":[{"type":"text","text":"STUB-DONE all good"}],"stop_reason":null,"usage":{"input_tokens":20,"output_tokens":5,"cache_read_input_tokens":4,"total_tokens":25}}}
{"type":"result","subtype":"success","session_id":"7a0e8a2e-6a1c-4c55-9a3f-0e5e0f1d2a22","is_error":false,"duration_ms":796,"num_turns":2,"result":"STUB-DONE all good","usage":{"input_tokens":60,"output_tokens":15,"cache_read_input_tokens":12,"total_tokens":75},"permission_denials":[]}
"""
CODEX_0163_SAMPLE = """\
{"type":"thread.started","thread_id":"01a15b82-3c8a-7912-a3d8-a8813828a194"}
{"type":"item.completed","item":{"id":"item_0","type":"error","message":"Model metadata for `stub-model` not found. Defaulting to fallback metadata; this can degrade performance and cause issues."}}
{"type":"turn.started"}
{"type":"item.started","item":{"id":"item_1","type":"command_execution","command":"/bin/bash -lc 'echo hello > /work/out.txt'","aggregated_output":"","exit_code":null,"status":"in_progress"}}
{"type":"item.completed","item":{"id":"item_1","type":"command_execution","command":"/bin/bash -lc 'echo hello > /work/out.txt'","aggregated_output":"","exit_code":0,"status":"completed"}}
{"type":"item.completed","item":{"id":"item_2","type":"agent_message","text":"STUB-DONE all good"}}
{"type":"turn.completed","usage":{"input_tokens":60,"cached_input_tokens":12,"cache_write_input_tokens":0,"output_tokens":16,"reasoning_output_tokens":4}}
"""
CODEX_UNREACHABLE_SAMPLE = """\
{"type":"thread.started","thread_id":"01a15b89-1986-7773-a2e1-9666fdbac32a"}
{"type":"turn.started"}
{"type":"error","message":"Reconnecting... waiting for network (Connection failed: error sending request)"}
"""


def driver(preset: str | None = "claude", **overrides) -> SimpleNamespace:
    """A DriverConfig look-alike (DESIGN section 11 plus section 20's profile)."""
    fields = dict(name=preset or "custom", type="cli", preset=preset, command=preset or "",
                  run_args=None, resume_args=None, env={}, models={}, timeout=None, profile=None)
    fields.update(overrides)
    return SimpleNamespace(**fields)


class Events:
    """Thread-safe stand-in for RunContext.emit."""

    def __init__(self) -> None:
        self.items: list[tuple[str, dict]] = []
        self._lock = threading.Lock()

    def emit(self, event_type: str, data: dict) -> None:
        with self._lock:
            self.items.append((event_type, data))

    def of(self, event_type: str) -> list[dict]:
        with self._lock:
            return [data for kind, data in self.items if kind == event_type]


def pid_alive(pid: int) -> bool:
    """True if pid exists and is not a zombie."""
    try:
        stat_line = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return False
    except OSError:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True
    return stat_line.rsplit(")", 1)[1].split()[0] not in ("Z", "X")


def wait_dead(pid: int, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.05)
    return not pid_alive(pid)


class CliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.cwd = self.tmp / "worktree"
        self.job_dir = self.tmp / "jobs" / "001-feature"
        self.cwd.mkdir()
        self.job_dir.mkdir(parents=True)
        self.record = self.tmp / "record.json"
        self.events = Events()
        totals = mock.patch.dict(cli._session_totals, {}, clear=True)
        totals.start()
        self.addCleanup(totals.stop)

    def ctx(self, model_key: str = "opus", model: str = "deepseek-v4-pro", wall: float = 30,
            env: dict | None = None, body: str = "You are the builder.", secrets: list | None = None,
            cancel: threading.Event | None = None) -> RunContext:
        return RunContext(
            job={"id": "001-feature"},
            agent=SimpleNamespace(name="builder", model="opus", body=body, tools=["Read", "Write"]),
            cwd=self.cwd, job_dir=self.job_dir, write_paths=[self.cwd], read_roots=[self.cwd],
            log_path=self.job_dir / "logs" / "01-builder.jsonl", emit=self.events.emit,
            cancel=cancel or threading.Event(), model=SimpleNamespace(key=model_key, model=model),
            limits=SimpleNamespace(max_wall_seconds=wall, max_turns=10), env=env or {},
            session_path=self.job_dir / "sessions" / "builder.json", secrets=secrets or [])

    def fake(self, preset: str | None = "claude", env: dict | None = None, **overrides) -> CliRunner:
        """A runner whose command is tests/fake_cli.py, recording into self.record."""
        fake_env = {"FAKE_CLI_RECORD": str(self.record), **(env or {})}
        return CliRunner(None, driver(preset, command=FAKE_COMMAND, env=fake_env, **overrides))

    def recorded(self) -> dict:
        return json.loads(self.record.read_text())

    def replay(self, sample: str) -> str:
        path = self.tmp / "replay.jsonl"
        path.write_text(sample)
        return str(path)


class PrivilegesTest(CliTestCase):
    """DESIGN §17: a CLI agent runs outside bwrap, so the driver starts it with no new privileges (setpriv): no
    setuid program (sudo, sudo-rs or classic) gives it more rights than the harness's user."""

    @unittest.skipUnless(sandbox.setpriv_path(), "setpriv (util-linux) is not installed here")
    def test_the_cli_runs_without_new_privileges(self) -> None:
        if sandbox.no_new_privs_set():
            self.skipTest("this test process has no-new-privileges already")
        run = self.fake().run(self.ctx(), "Build it.")
        self.assertTrue(run.ok, run.error)
        self.assertIs(self.recorded()["no_new_privs"], True)
        self.assertEqual(self.recorded()["argv"][:2], ["-p", "Build it."], "the CLI's own argv is unchanged")

    def test_a_missing_cli_still_reads_as_not_found(self) -> None:
        runner = CliRunner(None, driver("claude", command=str(self.tmp / "no-such-cli")))
        run = runner.run(self.ctx(), "Build it.")
        self.assertFalse(run.ok)
        self.assertIn("command not found", run.error)


class PresetArgvTest(CliTestCase):
    """Golden argv lists for each preset (flags verified against the real CLIs)."""

    def test_claude_run_and_resume(self) -> None:
        runner = CliRunner(None, driver("claude"))
        ctx = self.ctx()
        system = runner.system_text(ctx)
        self.assertEqual(runner.command_line(ctx, "Build round 1.", SID), [
            "claude", "-p", "Build round 1.", "--output-format", "stream-json", "--verbose",
            "--append-system-prompt", system, "--model", "opus",
            "--permission-mode", "bypassPermissions", "--session-id", SID,
            "--add-dir", str(self.job_dir)])
        self.assertEqual(runner.command_line(ctx, "Round 1 reviews are in.", SID, resume=True), [
            "claude", "-p", "Round 1 reviews are in.", "--resume", SID, "--output-format",
            "stream-json", "--verbose", "--append-system-prompt", system, "--model", "opus",
            "--permission-mode", "bypassPermissions", "--add-dir", str(self.job_dir)])
        self.assertTrue(runner.supports_resume)

    def test_qwen_run_and_resume(self) -> None:
        runner = CliRunner(None, driver("qwen"))
        ctx = self.ctx()
        system = runner.system_text(ctx)
        self.assertEqual(runner.command_line(ctx, "Build round 1.", SID), [
            "qwen", "Build round 1.", "--yolo", "-m", "deepseek-v4-pro",
            "--append-system-prompt", system, "-o", "stream-json", "--session-id", SID,
            "--include-directories", str(self.job_dir)])
        self.assertEqual(runner.command_line(ctx, "Again.", SID, resume=True), [
            "qwen", "Again.", "--yolo", "-m", "deepseek-v4-pro", "--append-system-prompt", system,
            "-o", "stream-json", "--resume", SID, "--include-directories", str(self.job_dir)])
        self.assertTrue(runner.supports_resume)

    def test_codex_run_prepends_system_and_resume_does_not(self) -> None:
        runner = CliRunner(None, driver("codex"))
        ctx = self.ctx(model="strata")
        system = runner.system_text(ctx)
        common = ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox",
                  "--skip-git-repo-check", "--json", "-m", "strata", "-C", str(self.cwd),
                  "--add-dir", str(self.job_dir)]
        # the prompt goes on stdin ("-"), from a writer thread (BROWSER_AND_CODEX.md §3.3, both presets)
        self.assertEqual(runner.command_line(ctx, "Build round 1.", SID), [*common, "-"])
        self.assertEqual(runner._stdin_text(ctx, "Build round 1.", resume=False),
                         system + "\n\n---\n\nBuild round 1.")
        self.assertEqual(runner.command_line(ctx, "Again.", SID, resume=True), [*common, "resume", SID, "-"])
        self.assertEqual(runner._stdin_text(ctx, "Again.", resume=True), "Again.")
        self.assertTrue(runner.supports_resume)
        self.assertNotIn("--full-auto", PRESETS["codex"]["run_args"])
        self.assertEqual(runner.interrupt, "SIGINT")

    def test_codex_worker_is_told_to_keep_the_ignored_files_it_did_not_make(self) -> None:
        """verify counts only the ignored files the job's Codex runs made or changed; what the repository's setup
        installed (node_modules, .venv) stays, and without network Codex could never put it back."""
        runner = CliRunner(None, driver("codex-worker"))
        system = runner.system_text(self.ctx())
        self.assertIn(cli.IGNORED_NOTE, system)
        self.assertIn("Leave no file that git ignores that you created or changed", cli.IGNORED_NOTE)
        self.assertIn("Ignored files that were there before your run, such as installed dependencies, stay",
                      cli.IGNORED_NOTE)
        self.assertIn("no `git clean -X`", cli.IGNORED_NOTE)
        self.assertNotIn("Leave no file that git ignores in the worktree", cli.IGNORED_NOTE)

    def test_codex_profile_goes_after_exec_in_run_and_resume(self) -> None:
        runner = CliRunner(None, driver("codex", profile="local"))
        ctx = self.ctx()
        self.assertEqual(runner.command_line(ctx, "p", SID)[:4], ["codex", "exec", "--profile", "local"])
        resume = runner.command_line(ctx, "p", SID, resume=True)
        self.assertEqual(resume[:4], ["codex", "exec", "--profile", "local"])
        self.assertEqual(resume.count("--profile"), 1)
        self.assertLess(resume.index("--profile"), resume.index("resume"))

    def test_profile_not_duplicated_and_ignored_for_other_presets(self) -> None:
        custom = CliRunner(None, driver("codex", profile="local",
                                        run_args=["exec", "-p", "mine", "{prompt}"]))
        self.assertEqual(custom.command_line(self.ctx(), "x", SID)[1:3], ["exec", "-p"])
        claude = CliRunner(None, driver("claude", profile="local"))
        self.assertNotIn("--profile", claude.command_line(self.ctx(), "x", SID))

    def test_make_runner_builds_a_cli_runner(self) -> None:
        config = SimpleNamespace(driver=lambda name: driver("codex", command="codex-local"))
        runner = make_runner(config, "codex")
        self.assertIsInstance(runner, CliRunner)
        self.assertEqual(runner.command, ["codex-local"])
        self.assertIs(runner.config, config)

    def test_every_preset_documents_itself(self) -> None:
        for name, preset in PRESETS.items():
            with self.subTest(preset=name):
                self.assertIn(preset["system_mode"], ("flag", "prepend"))
                self.assertIn(preset["output"], ("stream-json", "jsonl", "text"))
                self.assertTrue(preset["env_hint"])
                if preset.get("prompt_stdin"):   # the Codex presets: the prompt on stdin
                    self.assertEqual(preset["run_args"][-1], "-")
                    self.assertEqual(preset["resume_args"][-1], "-")
                else:
                    self.assertIn("{prompt}", preset["run_args"])
                self.assertIn("{session_id}", preset["resume_args"])
                self.assertIn(preset["usage_style"], ("anthropic", "openai"))
                if name == "codex-worker":   # Codex's own environment is the launcher's (DESIGN §44)
                    self.assertEqual(preset["env_keep"], [])
                    self.assertEqual(preset["commits"], "harness")
                else:
                    self.assertTrue(preset["env_keep"])


class ModelSelectionTest(CliTestCase):
    def model_arg(self, runner: CliRunner, ctx: RunContext) -> str:
        argv = runner.command_line(ctx, "p", SID)
        flag = "--model" if "--model" in argv else "-m"
        return argv[argv.index(flag) + 1]

    def test_claude_passes_tier_names(self) -> None:
        runner = CliRunner(None, driver("claude"))
        for tier in ("opus", "sonnet", "haiku"):
            with self.subTest(tier=tier):
                self.assertEqual(self.model_arg(runner, self.ctx(model_key=tier, model="x")), tier)

    def test_claude_non_tier_key_uses_model_id(self) -> None:
        runner = CliRunner(None, driver("claude"))
        ctx = self.ctx(model_key="local-coder", model="qwen3-coder:30b")
        self.assertEqual(self.model_arg(runner, ctx), "qwen3-coder:30b")

    def test_driver_models_override_for_every_preset(self) -> None:
        for preset in PRESETS:
            with self.subTest(preset=preset):
                runner = CliRunner(None, driver(preset, models={"opus": "mapped-model"}))
                self.assertEqual(self.model_arg(runner, self.ctx(model_key="opus")), "mapped-model")
                self.assertEqual(self.model_arg(runner, self.ctx(model_key="sonnet", model="flash")),
                                 "sonnet" if preset == "claude" else "flash")

    def test_other_presets_use_resolved_model(self) -> None:
        for preset in ("qwen", "codex"):
            with self.subTest(preset=preset):
                runner = CliRunner(None, driver(preset))
                self.assertEqual(self.model_arg(runner, self.ctx(model_key="opus")), "deepseek-v4-pro")

    def test_launch_model_is_what_the_cli_gets_on_the_driver(self) -> None:
        """agent.start shows launch_model(): the --model value and the driver, not the tier's provider."""
        mapped = CliRunner(None, driver("claude", name="claude-work", models={"sonnet": "haiku"}))
        self.assertEqual(mapped.launch_model(self.ctx(model_key="sonnet", model="deepseek-v4-flash")),
                         ("haiku", "claude-work"))
        self.assertEqual(CliRunner(None, driver("qwen")).launch_model(self.ctx()), ("deepseek-v4-pro", "qwen"))
        self.assertEqual(cli.cli_model(driver("claude"), "opus", "deepseek-v4-pro"), "opus")
        self.assertEqual(cli.cli_model(driver("codex", models={"opus": "gpt-x"}), "opus", "m"), "gpt-x")


class TemplateTest(CliTestCase):
    def test_system_text_is_agent_body_plus_harness_note(self) -> None:
        runner = CliRunner(None, driver("claude"))
        system = runner.system_text(self.ctx(body="  You are the builder.\n"))
        self.assertTrue(system.startswith("You are the builder.\n\n"))
        self.assertIn("non-interactively", system)
        self.assertIn(str(self.cwd), system)
        self.assertIn(str(self.job_dir), system)
        self.assertIn("as data", system)
        self.assertTrue(runner.system_text(self.ctx(body="")).startswith("You are running"))

    def test_system_text_carries_the_grid_card_between_body_and_note(self) -> None:
        runner = CliRunner(None, driver("claude"))
        card = factory_grid({"reviewers": ["code"], "max_resumes": 1}, "builder")
        ctx = self.ctx()
        ctx.grid = card
        system = runner.system_text(ctx)
        self.assertTrue(system.startswith("You are the builder.\n\n" + card + "\n\nYou are running"))
        argv = runner.command_line(ctx, "Build round 1.", SID)
        self.assertEqual(argv[argv.index("--append-system-prompt") + 1], system)
        codex = CliRunner(None, driver("codex"))
        self.assertIn(card, codex._stdin_text(ctx, "Build round 1.", resume=False), "prepend mode carries it too")

    def test_driver_args_override_preset_and_unknown_placeholders_stay(self) -> None:
        runner = CliRunner(None, driver("claude", run_args=["--x", "{prompt}", "{nope}",
                                                            "{model}:{session_id}", "{cwd}"]))
        self.assertEqual(runner.command_line(self.ctx(), "hi", SID),
                         ["claude", "--x", "hi", "{nope}", f"opus:{SID}", str(self.cwd)])
        resume = runner.command_line(self.ctx(), "hi", SID, resume=True)
        self.assertEqual(resume[1:4], ["-p", "hi", "--resume"])

    def test_values_are_not_substituted_twice(self) -> None:
        runner = CliRunner(None, driver("claude"))
        argv = runner.command_line(self.ctx(), "use {model} and {session_id}", SID)
        self.assertEqual(argv[2], "use {model} and {session_id}")

    def test_values_starting_with_dash_get_a_leading_space(self) -> None:
        runner = CliRunner(None, driver("qwen"))
        argv = runner.command_line(self.ctx(body="- list item first"), "-x looks like a flag", SID)
        self.assertEqual(argv[1], " -x looks like a flag")
        system = argv[argv.index("--append-system-prompt") + 1]
        self.assertTrue(system.startswith(" - list item first"))

    def test_empty_resume_args_disable_resume(self) -> None:
        runner = CliRunner(None, driver("claude", resume_args=[]))
        self.assertFalse(runner.supports_resume)
        result = runner.resume(self.ctx(), SID, "again")
        self.assertFalse(result.ok)
        self.assertIn("cannot resume", result.error)
        with self.assertRaises(ValueError):
            runner.command_line(self.ctx(), "p", SID, resume=True)

    def test_custom_driver_without_preset_prepends_and_reads_text(self) -> None:
        runner = CliRunner(None, driver(None, command="mycli", run_args=["run", "{prompt}"]))
        ctx = self.ctx()
        self.assertEqual(runner.command_line(ctx, "do it", SID),
                         ["mycli", "run", runner.system_text(ctx) + "\n\n---\n\ndo it"])
        self.assertFalse(runner.supports_resume)
        self.assertEqual(runner.output, "text")

    def test_invalid_driver_configs(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown CLI preset"):
            CliRunner(None, driver("gemini"))
        with self.assertRaisesRegex(ValueError, "preset or run_args"):
            CliRunner(None, driver(None, command="x"))
        with self.assertRaisesRegex(ValueError, "no command"):
            CliRunner(None, driver(None, command="  ", run_args=["{prompt}"]))


class CommandTest(CliTestCase):
    def test_command_defaults_to_preset_name(self) -> None:
        self.assertEqual(CliRunner(None, driver("codex", command="")).command, ["codex"])

    def test_command_with_spaces_is_split(self) -> None:
        runner = CliRunner(None, driver("codex", command="npx -y '@openai/codex@0.163.0'"))
        self.assertEqual(runner.command, ["npx", "-y", "@openai/codex@0.163.0"])

    def test_tilde_is_expanded(self) -> None:
        with mock.patch.dict(os.environ, {"HOME": str(self.tmp)}):
            runner = CliRunner(None, driver("codex", command="~/.local/bin/codex-local"))
            split = CliRunner(None, driver("codex", command="~/bin/codex --flag"))
        self.assertEqual(runner.command, [str(self.tmp / ".local/bin/codex-local")])
        self.assertEqual(split.command, [str(self.tmp / "bin/codex"), "--flag"])

    def test_existing_path_with_spaces_is_not_split(self) -> None:
        tool = self.tmp / "my tools" / "codex"
        tool.parent.mkdir()
        tool.write_text("#!/bin/sh\n")
        self.assertEqual(CliRunner(None, driver("codex", command=str(tool))).command, [str(tool)])

    def test_wrapper_script_passes_args_through(self) -> None:
        wrapper = self.tmp / "home" / ".local" / "bin" / "codex-local"
        wrapper.parent.mkdir(parents=True)
        wrapper.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} '
                           f'{shlex.quote(str(FAKE_CLI))} "$@"\n')
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
        env = {"FAKE_CLI_RECORD": str(self.record), "FAKE_CLI_FORMAT": "jsonl",
               "FAKE_CLI_THREAD": "0199b000-0000-7000-8000-00000000000a"}
        with mock.patch.dict(os.environ, {"HOME": str(self.tmp / "home")}):
            runner = CliRunner(None, driver("codex", command="~/.local/bin/codex-local",
                                            profile="local", env=env))
        result = runner.run(self.ctx(), NASTY_PROMPT)
        self.assertTrue(result.ok, result.error)
        argv = self.recorded()["argv"]
        self.assertEqual(argv[:3], ["exec", "--profile", "local"])
        self.assertEqual(argv[-1], "-")
        self.assertTrue(self.recorded()["stdin"].endswith("\n\n---\n\n" + NASTY_PROMPT.replace("\0", "")))
        self.assertEqual(result.session_ref, "0199b000-0000-7000-8000-00000000000a")


class ProcessTest(CliTestCase):
    def test_run_passes_prompt_as_one_item_with_cwd_env_and_devnull_stdin(self) -> None:
        runner = self.fake("claude", env={
            "FOO": "driver", "BOTH": "driver", "SF_TEST_UNSET": None,
            "FAKE_CLI_ENV_KEYS": "FOO,BAR,BOTH,SF_TEST_PASSTHROUGH,SF_TEST_UNSET"})
        env = {"SF_TEST_PASSTHROUGH": "yes", "SF_TEST_UNSET": "inherited"}
        with mock.patch.dict(os.environ, env):
            result = runner.run(self.ctx(env={"BAR": "ctx", "BOTH": "ctx"}), NASTY_PROMPT)
        self.assertTrue(result.ok, result.error)
        record = self.recorded()
        self.assertEqual(record["argv"][:2], ["-p", NASTY_PROMPT])
        self.assertEqual(record["argv"][record["argv"].index("--session-id") + 1], result.session_ref)
        self.assertEqual(Path(record["cwd"]).resolve(), self.cwd.resolve())
        self.assertEqual(record["env"], {"FOO": "driver", "BAR": "ctx", "BOTH": "ctx",
                                         "SF_TEST_PASSTHROUGH": "yes", "SF_TEST_UNSET": None})
        self.assertEqual(record["stdin"], "")

    def test_each_run_uses_a_fresh_uuid4_session(self) -> None:
        runner = self.fake("claude")
        first = runner.run(self.ctx(), "one")
        second = runner.run(self.ctx(), "two")
        self.assertNotEqual(first.session_ref, second.session_ref)
        for ref in (first.session_ref, second.session_ref):
            self.assertRegex(ref, r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")

    def test_stream_json_events_final_text_and_usage(self) -> None:
        result = self.fake("claude", env={"FAKE_CLI_STDERR": "a warning"}).run(self.ctx(), "go")
        self.assertEqual(result, AgentRun(
            ok=True, error=None, final_text="fake done",
            usage={"input_tokens": 130, "output_tokens": 20, "cached_tokens": 30, "calls": 3},
            session_ref=result.session_ref, turns=3, outcome="completed", details=result.details))
        self.assertEqual(result.details["exit"], 0)
        lines = [data["line"] for data in self.events.of("agent.output")]
        self.assertIn("not json {", lines)
        self.assertIn("a warning", lines)
        self.assertEqual(len(lines), 8)
        self.assertEqual(self.events.of("agent.text"), [{"text": "fake done"}])
        self.assertEqual(self.events.of("agent.reasoning"), [{"text": "thinking about it"}])
        call, = self.events.of("agent.tool_call")
        self.assertEqual((call["id"], call["name"], call["input"]["file_path"]),
                         ("toolu_1", "Write", "out.txt"))
        self.assertEqual(len(call["input"]["content"]), 500)
        self.assertEqual(self.events.of("agent.tool_result"),
                         [{"id": "toolu_1", "name": "Write", "ok": True, "output": "ok"}])
        self.assertEqual(self.events.of("agent.usage"),
                         [{"input_tokens": 130, "output_tokens": 20, "cached_tokens": 30}])

    def test_resume_uses_resume_args_with_session(self) -> None:
        runner = self.fake("claude")
        first = runner.run(self.ctx(), "first")
        result = runner.resume(self.ctx(), first.session_ref, "Round 1 reviews are in.")
        self.assertTrue(result.ok, result.error)
        argv = self.recorded()["argv"]
        self.assertEqual(argv[:4], ["-p", "Round 1 reviews are in.", "--resume", first.session_ref])
        self.assertNotIn("--session-id", argv)
        self.assertEqual(result.session_ref, first.session_ref)
        self.assertEqual(result.usage["input_tokens"], 130)

    def test_resume_without_session_ref_fails_fast(self) -> None:
        result = self.fake("claude").resume(self.ctx(), "", "again")
        self.assertFalse(result.ok)
        self.assertIn("no session", result.error)
        self.assertFalse(self.record.exists())

    def test_session_total_usage_is_diffed_on_resume(self) -> None:
        runner = self.fake("qwen", env={"FAKE_CLI_USAGE": json.dumps(
            {"input_tokens": 60, "output_tokens": 15, "cache_read_input_tokens": 12})})
        first = runner.run(self.ctx(), "first")
        self.assertEqual(first.usage["input_tokens"], 60)
        runner.driver.env["FAKE_CLI_USAGE"] = json.dumps(
            {"input_tokens": 100, "output_tokens": 25, "cache_read_input_tokens": 20})
        second = runner.resume(self.ctx(), first.session_ref, "again")
        self.assertEqual(second.usage, {"input_tokens": 40, "output_tokens": 10,
                                        "cached_tokens": 8, "calls": 3})

    def test_codex_jsonl_session_tools_text_and_usage(self) -> None:
        thread = "0199c000-0000-7000-8000-00000000000c"
        runner = self.fake("codex", env={"FAKE_CLI_FORMAT": "jsonl", "FAKE_CLI_THREAD": thread})
        result = runner.run(self.ctx(), "Build round 1.")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.session_ref, thread)
        self.assertEqual(result.final_text, "fake done")
        self.assertEqual(result.usage, {"input_tokens": 100, "output_tokens": 20,
                                        "cached_tokens": 30, "calls": 1})
        self.assertEqual(self.events.of("agent.tool_call"),
                         [{"id": "item_1", "name": "shell", "input": {"command": "bash -lc 'echo hi'"}}])
        self.assertEqual(self.events.of("agent.tool_result"),
                         [{"id": "item_1", "name": "shell", "ok": True, "output": "hi\n"}])
        self.assertEqual(self.events.of("agent.reasoning"), [{"text": "pondering"}])
        self.assertEqual(self.recorded()["argv"][-1], "-")
        self.assertTrue(self.recorded()["stdin"].startswith("You are the builder."))
        self.assertEqual(result.outcome, "completed")

        runner.driver.env["FAKE_CLI_USAGE"] = json.dumps(
            {"input_tokens": 150, "cached_input_tokens": 40, "output_tokens": 26})
        resumed = runner.resume(self.ctx(), thread, "Round 1 reviews are in.")
        self.assertTrue(resumed.ok, resumed.error)
        self.assertEqual(self.recorded()["argv"][-3:], ["resume", thread, "-"])
        self.assertEqual(self.recorded()["stdin"], "Round 1 reviews are in.")
        self.assertEqual(resumed.usage, {"input_tokens": 50, "output_tokens": 6,
                                         "cached_tokens": 10, "calls": 1})

    def test_text_output_collects_stdout_as_final_text(self) -> None:
        runner = CliRunner(None, driver(None, command=FAKE_COMMAND, run_args=["{prompt}"],
                                        env={"FAKE_CLI_RECORD": str(self.record),
                                             "FAKE_CLI_FORMAT": "text", "FAKE_CLI_STDERR": "noise"}))
        result = runner.run(self.ctx(), "do it")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.final_text, "not json {\nfake line 1\nfake done")
        self.assertIsNone(result.session_ref)
        self.assertEqual(result.usage, {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0,
                                        "calls": 0})
        self.assertEqual(self.events.of("agent.usage"), [])
        self.assertTrue(self.recorded()["argv"][0].endswith("\n\n---\n\ndo it"))

    def test_agent_can_write_files_in_cwd(self) -> None:
        result = self.fake("claude", env={"FAKE_CLI_WRITE": "build.md=STATUS: READY\n"}).run(
            self.ctx(), "go")
        self.assertTrue(result.ok, result.error)
        self.assertEqual((self.cwd / "build.md").read_text(), "STATUS: READY\n")

    def test_long_lines_and_long_text_are_split_or_cut(self) -> None:
        text = "t" * 9000
        self.fake("claude", env={"FAKE_CLI_LONG": "5000", "FAKE_CLI_TEXT": text}).run(self.ctx(), "go")
        self.assertTrue(all(len(d["line"]) <= 2000 for d in self.events.of("agent.output")))
        chunks = [d["text"] for d in self.events.of("agent.text")]
        self.assertEqual([len(c) for c in chunks], [4000, 4000, 1000])
        self.assertEqual("".join(chunks), text)

    def test_secrets_in_env_are_never_emitted(self) -> None:
        secret = "tok-abcdef123456"
        runner = self.fake("claude", env={"MY_SERVICE_TOKEN": secret, "FAKE_CLI_ECHO": "MY_SERVICE_TOKEN"})
        runner.run(self.ctx(), "go")
        ctx_secret = "ctx-secret-value-1"
        self.fake("claude", env={"FAKE_CLI_ECHO": "PLAIN_VAR"}).run(
            self.ctx(env={"PLAIN_VAR": ctx_secret}, secrets=[ctx_secret]), "go")
        dumped = json.dumps(self.events.items)
        self.assertNotIn(secret, dumped)
        self.assertNotIn(ctx_secret, dumped)
        self.assertEqual(dumped.count('"***"'), 2)


class FailureTest(CliTestCase):
    def test_nonzero_exit_reports_code_and_stderr(self) -> None:
        runner = CliRunner(None, driver(None, command=FAKE_COMMAND, run_args=["{prompt}"], env={
            "FAKE_CLI_FORMAT": "text", "FAKE_CLI_EXIT": "3", "FAKE_CLI_STDERR": "boom happened"}))
        result = runner.run(self.ctx(), "go")
        self.assertFalse(result.ok)
        self.assertIn("exited with code 3: boom happened", result.error)

    def test_nonzero_exit_prefers_cli_error_result(self) -> None:
        result = self.fake("claude", env={"FAKE_CLI_EXIT": "1", "FAKE_CLI_TEXT": "API Error: 401"}).run(
            self.ctx(), "go")
        self.assertFalse(result.ok)
        self.assertIn("exited with code 1: API Error: 401", result.error)
        self.assertEqual(result.final_text, "API Error: 401")

    def test_missing_command(self) -> None:
        for command in ("/nonexistent/dir/sf-fake-cli", "sf-definitely-not-installed-cli"):
            with self.subTest(command=command):
                result = CliRunner(None, driver("qwen", command=command)).run(self.ctx(), "go")
                self.assertEqual((result.ok, result.error), (False, f"command not found: {command}"))

    def test_not_executable_command(self) -> None:
        tool = self.tmp / "not-executable"
        tool.write_text("#!/bin/sh\n")
        tool.chmod(0o644)
        result = CliRunner(None, driver("qwen", command=str(tool))).run(self.ctx(), "go")
        self.assertFalse(result.ok)
        self.assertIn("cannot start", result.error)

    def test_missing_working_directory(self) -> None:
        ctx = self.ctx()
        ctx.cwd = self.tmp / "gone"
        result = self.fake("claude").run(ctx, "go")
        self.assertFalse(result.ok)
        self.assertIn("working directory missing", result.error)
        self.assertFalse(self.record.exists())

    def test_timeout_kills_the_process_group(self) -> None:
        child_pid_file = self.tmp / "child.pid"
        runner = self.fake("claude", env={"FAKE_CLI_SLEEP": "30", "FAKE_CLI_CHILD": str(child_pid_file)})
        started = time.monotonic()
        result = runner.run(self.ctx(wall=1), "go")
        self.assertLess(time.monotonic() - started, 10)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "timed out after 1s")
        self.assertEqual(result.final_text, "fake done")
        self.assertTrue(wait_dead(self.recorded()["pid"]))
        self.assertTrue(wait_dead(int(child_pid_file.read_text())))

    def test_driver_timeout_overrides_limits(self) -> None:
        runner = self.fake("claude", env={"FAKE_CLI_SLEEP": "30"}, timeout=0.5)
        result = runner.run(self.ctx(wall=60), "go")
        self.assertEqual(result.error, "timed out after 0.5s")

    def test_timeout_mentions_last_cli_error(self) -> None:
        runner = self.fake("codex", env={"FAKE_CLI_REPLAY": self.replay(CODEX_UNREACHABLE_SAMPLE),
                                         "FAKE_CLI_SLEEP": "30"})
        result = runner.run(self.ctx(wall=1), "go")
        self.assertFalse(result.ok)
        self.assertTrue(result.error.startswith("timed out after 1s (last error: Reconnecting..."))
        self.assertEqual(result.session_ref, "01a15b89-1986-7773-a2e1-9666fdbac32a")

    def test_an_unreachable_api_stops_the_run_early(self) -> None:
        """Codex retries a down server forever ("Reconnecting..." error events); after UNREACHABLE_ERRORS
        of them in a row spanning UNREACHABLE_SECONDS the run stops instead of waiting for the wall clock."""
        errors = CODEX_UNREACHABLE_SAMPLE + "\n".join(
            [CODEX_UNREACHABLE_SAMPLE.strip().splitlines()[-1]] * (cli.UNREACHABLE_ERRORS - 1)) + "\n"
        runner = self.fake("codex", env={"FAKE_CLI_REPLAY": self.replay(errors), "FAKE_CLI_SLEEP": "30"})
        with mock.patch.object(cli, "UNREACHABLE_SECONDS", 0.0):
            started = time.monotonic()
            result = runner.run(self.ctx(wall=60), "go")
        self.assertLess(time.monotonic() - started, 10)
        self.assertFalse(result.ok)
        self.assertTrue(result.error.startswith(f"stopped: the CLI reported {cli.UNREACHABLE_ERRORS} errors in a row"),
                        result.error)
        self.assertIn("looks unreachable (last error: Reconnecting... waiting for network", result.error)
        self.assertEqual(result.session_ref, "01a15b89-1986-7773-a2e1-9666fdbac32a")
        self.assertTrue(wait_dead(self.recorded()["pid"]))

    def test_sigterm_ignored_escalates_to_sigkill(self) -> None:
        runner = self.fake("claude", env={"FAKE_CLI_SLEEP": "30", "FAKE_CLI_IGNORE_TERM": "1"})
        with mock.patch.object(cli, "KILL_GRACE_SECONDS", 0.5):
            started = time.monotonic()
            result = runner.run(self.ctx(wall=1), "go")
        self.assertLess(time.monotonic() - started, 8)
        self.assertEqual(result.error, "timed out after 1s")
        self.assertTrue(wait_dead(self.recorded()["pid"]))

    def test_cancel_during_run(self) -> None:
        cancel = threading.Event()
        timer = threading.Timer(0.7, cancel.set)
        timer.start()
        self.addCleanup(timer.cancel)
        started = time.monotonic()
        result = self.fake("claude", env={"FAKE_CLI_SLEEP": "30"}).run(self.ctx(cancel=cancel), "go")
        self.assertLess(time.monotonic() - started, 8)
        self.assertEqual((result.ok, result.error), (False, "cancelled"))
        self.assertTrue(wait_dead(self.recorded()["pid"]))

    def test_cancel_before_start_spawns_nothing(self) -> None:
        cancel = threading.Event()
        cancel.set()
        result = self.fake("claude").run(self.ctx(cancel=cancel), "go")
        self.assertEqual((result.ok, result.error), (False, "cancelled"))
        self.assertFalse(self.record.exists())

    def test_leftover_background_processes_are_cleaned_up(self) -> None:
        child_pid_file = self.tmp / "child.pid"
        started = time.monotonic()
        result = self.fake("claude", env={"FAKE_CLI_CHILD": str(child_pid_file)}).run(self.ctx(), "go")
        self.assertTrue(result.ok, result.error)
        self.assertLess(time.monotonic() - started, 10)
        self.assertTrue(wait_dead(int(child_pid_file.read_text())))


class CommandPathTest(CliTestCase):
    """A relative command path never resolves against the agent's working directory."""

    def test_relative_path_is_anchored_at_the_config_directory(self) -> None:
        config = SimpleNamespace(path=self.tmp / "conf" / "config.json", raw={})
        anchored = CliRunner(config, driver("codex", command="bin/codex-local"))
        self.assertEqual(anchored.command, [str(self.tmp / "conf" / "bin" / "codex-local")])
        split = CliRunner(config, driver("codex", command="./bin/codex --flag"))
        self.assertEqual(split.command, [str(self.tmp / "conf" / "bin" / "codex"), "--flag"])
        self.assertEqual(CliRunner(config, driver("codex", command="codex-local")).command,
                         ["codex-local"])

    def test_without_a_config_relative_paths_use_the_server_cwd(self) -> None:
        runner = CliRunner(None, driver("codex", command="bin/codex-local"))
        self.assertEqual(runner.command, [os.path.join(os.getcwd(), "bin", "codex-local")])

    def test_binary_planted_in_the_worktree_never_runs(self) -> None:
        marker = self.tmp / "planted-ran"
        planted = self.cwd / "bin" / "codex-local"
        planted.parent.mkdir()
        planted.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\n")
        planted.chmod(0o755)
        config = SimpleNamespace(path=self.tmp / "conf" / "config.json", raw={})
        result = CliRunner(config, driver("codex", command="bin/codex-local")).run(self.ctx(), "go")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, f"command not found: {self.tmp / 'conf' / 'bin' / 'codex-local'}")
        self.assertFalse(marker.exists())


class EnvironmentTest(CliTestCase):
    """The CLI gets the server's environment without the factory's secrets (DESIGN section 17)."""

    SERVER_ENV = {
        "DEEPSEEK_API_KEY": "sk-deepseek-factory-key-0000", "FACTORY_CONFIG": "/x",
        "GITHUB_TOKEN": "ghp-token-value-0000", "MY_LOCAL_KEY": "local-key-value-0000",
        "ANTHROPIC_API_KEY": "sk-ant-value-00000", "CLAUDE_CODE_OAUTH_TOKEN": "oauth-value-0000",
        "OPENAI_API_KEY": "sk-openai-value-0000", "CODEX_HOME": "/codex", "SF_PLAIN": "kept"}
    CONFIG = SimpleNamespace(raw={"providers": {"local": {"api_key_env": "MY_LOCAL_KEY"},
                                                "keyless": {"api_key_env": None}, "bad": None}})

    def child_env(self, preset: str, **overrides) -> dict:
        runner = CliRunner(self.CONFIG, driver(preset, **overrides))
        with mock.patch.dict(os.environ, self.SERVER_ENV):
            env = runner._env(self.ctx())
        return {name: env.get(name) for name in self.SERVER_ENV}

    def test_factory_secrets_are_removed_and_cli_logins_kept(self) -> None:
        dropped = {"DEEPSEEK_API_KEY": None, "FACTORY_CONFIG": None, "GITHUB_TOKEN": None,
                   "MY_LOCAL_KEY": None}
        expected = {
            "claude": {**dropped, "ANTHROPIC_API_KEY": "sk-ant-value-00000",
                       "CLAUDE_CODE_OAUTH_TOKEN": "oauth-value-0000", "OPENAI_API_KEY": None,
                       "CODEX_HOME": "/codex", "SF_PLAIN": "kept"},
            "qwen": {**dropped, "ANTHROPIC_API_KEY": None, "CLAUDE_CODE_OAUTH_TOKEN": None,
                     "OPENAI_API_KEY": "sk-openai-value-0000", "CODEX_HOME": "/codex", "SF_PLAIN": "kept"},
            "codex": {**dropped, "ANTHROPIC_API_KEY": None, "CLAUDE_CODE_OAUTH_TOKEN": None,
                      "OPENAI_API_KEY": "sk-openai-value-0000", "CODEX_HOME": "/codex", "SF_PLAIN": "kept"},
        }
        for preset, want in expected.items():
            with self.subTest(preset=preset):
                self.assertEqual(self.child_env(preset), want)

    def test_common_credential_names_are_removed(self) -> None:
        """Cloud, payment and database credentials never reach any CLI, whatever its login prefixes."""
        credentials = {"AWS_SECRET_ACCESS_KEY": "aws-secret-value-000001", "AWS_ACCESS_KEY_ID": "AKIA0000000001",
                       "AWS_SESSION_TOKEN": "aws-session-000001", "STRIPE_SECRET_KEY": "sk_live_000002",
                       "DATABASE_PASSWORD": "db-password-000003", "PGPASSWORD": "pg-password-000004",
                       "GOOGLE_APPLICATION_CREDENTIALS": "/keys/gcp.json", "SENTRY_AUTH_TOKEN": "sntrys_0005"}
        for preset in (*PRESETS, None):
            runner = CliRunner(self.CONFIG, driver(preset, command="mycli", run_args=["{prompt}"]))
            with self.subTest(preset=preset), mock.patch.dict(os.environ, {**credentials, "SF_PLAIN": "kept"}):
                env = runner._env(self.ctx())
                self.assertEqual(sorted(name for name in credentials if name in env), [])
                self.assertEqual(env["SF_PLAIN"], "kept")

    def test_custom_driver_keeps_no_secrets(self) -> None:
        env = self.child_env(None, command="mycli", run_args=["{prompt}"])
        self.assertEqual({name for name, value in env.items() if value is not None},
                         {"CODEX_HOME", "SF_PLAIN"})

    def test_driver_env_can_pass_a_secret_on_purpose(self) -> None:
        runner = CliRunner(self.CONFIG, driver("claude", env={
            "ANTHROPIC_AUTH_TOKEN": "${DEEPSEEK_API_KEY}", "STRATA_API_KEY": "${SF_MISSING_VAR}",
            "LITERAL": "$DEEPSEEK_API_KEY and ${X}", "GITHUB_TOKEN": "explicit-token"}))
        with mock.patch.dict(os.environ, {**self.SERVER_ENV, "STRATA_API_KEY": "inherited-value"}):
            os.environ.pop("SF_MISSING_VAR", None)
            env = runner._env(self.ctx(env={"FACTORY_JOB": "001"}))
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "sk-deepseek-factory-key-0000")
        self.assertNotIn("STRATA_API_KEY", env)
        self.assertEqual(env["LITERAL"], "$DEEPSEEK_API_KEY and ${X}")
        self.assertEqual(env["GITHUB_TOKEN"], "explicit-token")
        self.assertEqual(env["FACTORY_JOB"], "001")
        self.assertNotIn("DEEPSEEK_API_KEY", env)

    def test_null_in_config_json_removes_the_variable(self) -> None:
        """drivers.<name>.env {"NAME": null} in config.json reaches the driver as None and unsets NAME."""
        from swfactory.config import Config

        path = self.tmp / "conf" / "config.json"
        path.parent.mkdir()
        path.write_text(json.dumps({"drivers": {"claude": {"env": {"SF_PLAIN": None, "SF_EXTRA": "x"}}}}))
        config = Config.load(path)
        runner = CliRunner(config, config.driver("claude"))
        with mock.patch.dict(os.environ, self.SERVER_ENV):
            env = runner._env(self.ctx())
        self.assertNotIn("SF_PLAIN", env)
        self.assertEqual(env["SF_EXTRA"], "x")
        self.assertNotIn("None", env.values())

    def test_child_process_sees_the_scrubbed_environment(self) -> None:
        runner = CliRunner(self.CONFIG, driver("codex", command=FAKE_COMMAND, env={
            "FAKE_CLI_RECORD": str(self.record), "FAKE_CLI_FORMAT": "jsonl",
            "FAKE_CLI_ENV_KEYS": ",".join(self.SERVER_ENV)}))
        with mock.patch.dict(os.environ, self.SERVER_ENV):
            result = runner.run(self.ctx(), "go")
        self.assertTrue(result.ok, result.error)
        seen = {name for name, value in self.recorded()["env"].items() if value is not None}
        self.assertEqual(seen, {"OPENAI_API_KEY", "CODEX_HOME", "SF_PLAIN"})


class QwenSettingsTest(CliTestCase):
    """The qwen preset runs with managed auto-memory, auto-dream and auto-skill off: they would carry one
    job's untrusted repo content into the user's cross-project memory and other jobs' sessions."""

    VAR = "QWEN_CODE_SYSTEM_SETTINGS_PATH"
    OFF = {"enableManagedAutoMemory": False, "enableManagedAutoDream": False, "enableAutoSkill": False}

    def run_qwen(self, server_env: dict | None = None, **overrides) -> tuple[AgentRun, str | None]:
        """Run the fake CLI on the qwen preset; return the run and the settings path it was given."""
        env = {"FAKE_CLI_RECORD": str(self.record), "FAKE_CLI_ENV_KEYS": self.VAR, **overrides.pop("env", {})}
        runner = CliRunner(None, driver("qwen", command=FAKE_COMMAND, env=env, **overrides))
        missing = str(self.tmp / "no-system-settings.json")   # never the host's /etc/qwen-code/settings.json
        with mock.patch.dict(os.environ, {self.VAR: missing, **(server_env or {})}):
            result = runner.run(self.ctx(), "go")
        return result, self.recorded()["env"][self.VAR]

    def test_memory_is_off_through_system_settings_beside_the_session(self) -> None:
        result, path = self.run_qwen()
        self.assertTrue(result.ok, result.error)
        self.assertEqual(path, str(self.job_dir / "sessions" / "builder.qwen-settings.json"))
        self.assertEqual(json.loads(Path(path).read_text()), {"memory": self.OFF})

    def test_the_hosts_system_settings_are_merged_in(self) -> None:
        system = self.tmp / "etc-settings.json"
        system.write_text(json.dumps({"general": {"vimMode": True},
                                      "memory": {"enableTeamMemory": True, "enableManagedAutoMemory": True}}))
        result, path = self.run_qwen(server_env={self.VAR: str(system)})
        self.assertTrue(result.ok, result.error)
        self.assertEqual(json.loads(Path(path).read_text()),
                         {"general": {"vimMode": True}, "memory": {"enableTeamMemory": True, **self.OFF}})

    def test_driver_env_opts_out(self) -> None:
        mine = str(self.tmp / "my-qwen-settings.json")
        result, path = self.run_qwen(env={self.VAR: mine})
        self.assertEqual(path, mine)
        self.assertFalse((self.job_dir / "sessions" / "builder.qwen-settings.json").exists())

    def test_unreadable_system_settings_are_left_alone(self) -> None:
        system = self.tmp / "etc-settings.json"
        system.write_text('{"general": {} // a comment\n}')
        result, path = self.run_qwen(server_env={self.VAR: str(system)})
        self.assertTrue(result.ok, result.error)
        self.assertEqual(path, str(system))
        notes = [e["line"] for e in self.events.of("agent.output") if e["line"].startswith("[factory]")]
        self.assertEqual(len(notes), 1)
        self.assertIn(f"cannot merge {system}", notes[0])

    def test_other_presets_get_no_settings_file(self) -> None:
        for preset in ("claude", "codex"):
            with self.subTest(preset=preset):
                self.assertIsNone(PRESETS[preset].get("system_settings"))
                env = {"FAKE_CLI_FORMAT": "jsonl"} if preset == "codex" else {}
                with mock.patch.dict(os.environ):
                    os.environ.pop(self.VAR, None)
                    result = self.fake(preset, env={**env, "FAKE_CLI_ENV_KEYS": self.VAR}).run(self.ctx(), "go")
                self.assertTrue(result.ok, result.error)
                self.assertIsNone(self.recorded()["env"][self.VAR])


class UsageTest(CliTestCase):
    """Usage follows the builtin providers' convention: input_tokens includes cached tokens."""

    BIG_CLAUDE_USAGE = {"input_tokens": 40, "cache_creation_input_tokens": 24000,
                        "cache_read_input_tokens": 180000, "output_tokens": 3000}

    def test_claude_input_includes_cache_reads_and_writes(self) -> None:
        runner = self.fake("claude", env={"FAKE_CLI_USAGE": json.dumps(self.BIG_CLAUDE_USAGE)})
        result = runner.run(self.ctx(), "go")
        self.assertEqual(result.usage, {"input_tokens": 204040, "output_tokens": 3000,
                                        "cached_tokens": 180000, "calls": 3})
        self.assertEqual(self.events.of("agent.usage"),
                         [{"input_tokens": 204040, "output_tokens": 3000, "cached_tokens": 180000}])

    def test_normalize_by_style(self) -> None:
        self.assertEqual(cli._normalize_usage(self.BIG_CLAUDE_USAGE, "anthropic"),
                         {"input_tokens": 204040, "output_tokens": 3000, "cached_tokens": 180000})
        # Qwen and Codex (OpenAI style) already count cached tokens inside input_tokens.
        self.assertEqual(cli._normalize_usage({"input_tokens": 60, "cache_read_input_tokens": 12,
                                               "output_tokens": 15}),
                         {"input_tokens": 60, "output_tokens": 15, "cached_tokens": 12})
        self.assertEqual(cli._normalize_usage({"input_tokens": 60, "cached_input_tokens": 12,
                                               "output_tokens": 16}, "openai"),
                         {"input_tokens": 60, "output_tokens": 16, "cached_tokens": 12})
        self.assertEqual(cli._normalize_usage({"input_tokens": True, "output_tokens": "9"}, "anthropic"),
                         {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0})

    def qwen(self, usage: dict) -> CliRunner:
        return self.fake("qwen", env={"FAKE_CLI_USAGE": json.dumps(usage)})

    def test_session_totals_survive_a_server_restart(self) -> None:
        first = self.qwen({"input_tokens": 60, "output_tokens": 15}).run(self.ctx(), "first")
        self.assertEqual(first.usage["input_tokens"], 60)
        store = self.job_dir / "sessions" / "builder.cli.json"
        self.assertEqual(json.loads(store.read_text())["session"], first.session_ref)
        cli._session_totals.clear()   # what a restart loses
        second = self.qwen({"input_tokens": 100, "output_tokens": 25}).resume(
            self.ctx(), first.session_ref, "again")
        self.assertEqual((second.usage["input_tokens"], second.usage["output_tokens"]), (40, 10))

    def test_stored_totals_of_another_session_are_ignored(self) -> None:
        store = self.job_dir / "sessions" / "builder.cli.json"
        store.parent.mkdir()
        store.write_text(json.dumps({"session": "older-session", "usage": {"input_tokens": 50}}))
        result = self.qwen({"input_tokens": 70, "output_tokens": 5}).resume(self.ctx(), SID, "again")
        self.assertEqual(result.usage["input_tokens"], 70)
        store.write_text("not json")
        cli._session_totals.clear()
        result = self.qwen({"input_tokens": 90, "output_tokens": 5}).resume(self.ctx(), SID, "again")
        self.assertEqual(result.usage["input_tokens"], 90)

    def test_without_a_session_path_totals_stay_in_memory(self) -> None:
        ctx = self.ctx()
        ctx.session_path = None
        first = self.qwen({"input_tokens": 60, "output_tokens": 15}).run(ctx, "first")
        second = self.qwen({"input_tokens": 100, "output_tokens": 25}).resume(ctx, first.session_ref, "x")
        self.assertEqual(second.usage["input_tokens"], 40)
        self.assertFalse((self.job_dir / "sessions").exists())


class InputSafetyTest(CliTestCase):
    """Inputs that cannot go through execve end as AgentRun(ok=False), never as an exception."""

    def test_nul_in_prompt_and_system_is_dropped(self) -> None:
        result = self.fake("claude").run(self.ctx(body="You are\0 the builder."), "spec \0 binary")
        self.assertTrue(result.ok, result.error)
        argv = self.recorded()["argv"]
        self.assertEqual(argv[1], "spec  binary")
        self.assertTrue(argv[argv.index("--append-system-prompt") + 1].startswith("You are the builder."))

    def test_nul_in_env_value_fails_cleanly(self) -> None:
        result = self.fake("claude").run(self.ctx(env={"BAD": "a\0b"}), "go")
        self.assertFalse(result.ok)
        self.assertIn("cannot start", result.error)
        self.assertIn("null byte", result.error)
        self.assertFalse(self.record.exists())

    def test_prompt_over_the_argument_limit_gets_a_clear_error(self) -> None:
        for preset, size in (("claude", 200000), ("qwen", cli.MAX_ARG_BYTES + 1)):
            with self.subTest(preset=preset):
                result = self.fake(preset).run(self.ctx(), "x" * size)
                self.assertFalse(result.ok)
                self.assertTrue(result.error.startswith("prompt too large for the command line"),
                                result.error)
                self.assertIn(f"limit {cli.MAX_ARG_BYTES}", result.error)
                self.assertFalse(self.record.exists())

    def test_codex_takes_a_prompt_over_the_argument_limit_on_stdin(self) -> None:
        """210,000 bytes worked with Codex 0.163.0 on stdin (codex-runtime.md probe 5); no argv item carries it."""
        runner = self.fake("codex", env={"FAKE_CLI_FORMAT": "jsonl"})
        result = runner.run(self.ctx(), "z" * 210000)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(self.recorded()["argv"][-1], "-")
        self.assertTrue(self.recorded()["stdin"].endswith("z" * 210000))

    def test_prompt_at_the_argument_limit_still_runs(self) -> None:
        result = self.fake("claude").run(self.ctx(), "y" * cli.MAX_ARG_BYTES)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(len(self.recorded()["argv"][1]), cli.MAX_ARG_BYTES)

    def test_error_and_final_text_are_redacted(self) -> None:
        driver_secret, server_secret, ctx_secret = "tok-driver-secret-1", "sk-server-secret-2", "ctx-secret-3"
        runner = self.fake("claude", env={
            "MY_SERVICE_TOKEN": driver_secret, "FAKE_CLI_EXIT": "1",
            "FAKE_CLI_TEXT": f"Invalid API key {driver_secret} {server_secret} {ctx_secret}"})
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": server_secret}):
            result = runner.run(self.ctx(secrets=[ctx_secret]), "go")
        self.assertFalse(result.ok)
        self.assertEqual(result.final_text, "Invalid API key *** *** ***")
        self.assertTrue(result.error.endswith("exited with code 1: Invalid API key *** *** ***"),
                        result.error)

    def test_a_secret_across_a_cut_point_leaks_no_prefix(self) -> None:
        secret = "tok-boundary-secret-9"
        # One stderr line: a secret across the 300-char error hint cut, another across the
        # 2000-char agent.output cut.
        line = "x" * 295 + secret + "y" * (1995 - 295 - len(secret)) + secret
        runner = CliRunner(None, driver(None, command=FAKE_COMMAND, run_args=["{prompt}"], env={
            "MY_SERVICE_TOKEN": secret, "FAKE_CLI_FORMAT": "text", "FAKE_CLI_EXIT": "3",
            "FAKE_CLI_STDERR": line}))
        result = runner.run(self.ctx(), "go")
        self.assertIn("exited with code 3: " + "x" * 295 + "***", result.error)
        self.assertNotIn("tok-", result.error)
        self.assertNotIn("tok-", json.dumps(self.events.items))


class RealSampleTest(CliTestCase):
    """Replays output captured from the real CLIs to pin the parsers to their formats."""

    def test_claude_code_2_1_89(self) -> None:
        runner = self.fake("claude", env={"FAKE_CLI_REPLAY": self.replay(CLAUDE_2189_SAMPLE)})
        result = runner.run(self.ctx(), "go")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.final_text, "STUB-DONE all good")
        self.assertEqual(result.usage, {"input_tokens": 28, "output_tokens": 14, "cached_tokens": 6,
                                        "calls": 2})
        self.assertEqual(result.turns, 2)
        self.assertEqual(self.events.of("agent.tool_call"), [{
            "id": "toolu_01", "name": "Write",
            "input": {"file_path": "/work/out.txt", "content": "hello\n"}}])
        self.assertEqual(self.events.of("agent.tool_result"), [{
            "id": "toolu_01", "name": "Write", "ok": True,
            "output": "File created successfully at: /work/out.txt"}])
        self.assertEqual(self.events.of("agent.text"), [{"text": "STUB-DONE all good"}])

    def test_qwen_code_0_25_3(self) -> None:
        runner = self.fake("qwen", env={"FAKE_CLI_REPLAY": self.replay(QWEN_0253_SAMPLE)})
        result = runner.run(self.ctx(), "go")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.final_text, "STUB-DONE all good")
        self.assertEqual(result.usage, {"input_tokens": 60, "output_tokens": 15, "cached_tokens": 12,
                                        "calls": 2})
        self.assertEqual([c["name"] for c in self.events.of("agent.tool_call")], ["write_file"])
        self.assertEqual(self.events.of("agent.tool_result")[0]["name"], "write_file")

    def test_codex_0_163_0(self) -> None:
        runner = self.fake("codex", env={"FAKE_CLI_REPLAY": self.replay(CODEX_0163_SAMPLE)})
        result = runner.run(self.ctx(), "go")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.session_ref, "01a15b82-3c8a-7912-a3d8-a8813828a194")
        self.assertEqual(result.final_text, "STUB-DONE all good")
        self.assertEqual(result.usage, {"input_tokens": 60, "output_tokens": 16, "cached_tokens": 12,
                                        "calls": 1})
        call, = self.events.of("agent.tool_call")
        self.assertEqual((call["id"], call["name"]), ("item_1", "shell"))
        result_event, = self.events.of("agent.tool_result")
        self.assertTrue(result_event["ok"])


class ItemsHooksTest(CliTestCase):
    """DESIGN §32: the items block of the harness note, and the Claude Code hooks behind housekeeping.cli_hooks."""

    SPEC = "## Acceptance criteria\n1. greet() says hello.\n2. A test covers greet().\n"

    def items(self, **settings) -> Any:
        from swfactory import items as items_lib
        from swfactory.config import HousekeepingConfig

        store = items_lib.ItemStore(self.job_dir, items_lib.job_scope("app", "001-feature"))
        if not store.path.exists():
            with store.transaction() as ledger:
                items_lib.ingest_spec(ledger, self.SPEC)
        build = self.job_dir / "build.md"
        return items_lib.ItemsContext(store, "builder", self.job_dir / "sessions" / "builder.items.jsonl",
                                      settings=HousekeepingConfig(**settings), answer_file=str(build), builder=True,
                                      contract="build", output_files=[build])

    def hooked(self, cli_hooks: bool, **env) -> CliRunner:
        from swfactory.config import HousekeepingConfig

        config = SimpleNamespace(path=None, raw={}, housekeeping=lambda: HousekeepingConfig(cli_hooks=cli_hooks))
        return CliRunner(config, driver("claude", command=FAKE_COMMAND,
                                        env={"FAKE_CLI_RECORD": str(self.record), **env}))

    def test_the_harness_note_carries_the_items_contract(self) -> None:
        ctx = self.ctx()
        runner = self.fake("claude")
        self.assertNotIn(cli.HARNESS_ITEMS_NOTE, runner.system_text(ctx))
        ctx.items = self.items()
        self.assertIn(cli.HARNESS_ITEMS_NOTE, runner.system_text(ctx))
        self.assertIn("Answer each item you own or must check under ## Items", cli.HARNESS_ITEMS_NOTE)

    def test_hooks_are_off_by_default(self) -> None:
        ctx = self.ctx()
        ctx.items = self.items()
        self.assertTrue(self.hooked(False).run(ctx, "go").ok)
        self.assertNotIn("--settings", self.recorded()["argv"])
        self.assertFalse(self.fake("claude").hooks_enabled(ctx), "no config: off")
        self.assertFalse(CliRunner(None, driver("qwen", command=FAKE_COMMAND)).hooks_enabled(ctx),
                         "only the claude preset has the hooks (Qwen Code's names are unverified)")

    def test_with_cli_hooks_the_stop_hook_blocks_once_and_session_start_returns_the_open_list(self) -> None:
        build = self.job_dir / "build.md"
        hooks_record = self.tmp / "hooks.json"
        answered = "STATUS: READY\n\n## Items\n- I-1 done: commit 4f1c2a9\n- I-2 doing: the test is half written\n"
        ctx = self.ctx()
        ctx.items = self.items(cli_hooks=True)
        runner = self.hooked(True, FAKE_CLI_HOOKS=str(hooks_record), FAKE_CLI_ON_BLOCK=f"{build}={answered}")
        result = runner.run(ctx, "Build round 1.")
        self.assertTrue(result.ok, result.error)
        argv = self.recorded()["argv"]
        settings_path = Path(argv[argv.index("--settings") + 1])
        settings = json.loads(settings_path.read_text())
        self.assertEqual(sorted(settings["hooks"]), ["SessionStart", "Stop"])
        self.assertEqual(settings["hooks"]["SessionStart"][0]["matcher"], "compact")
        played = json.loads(hooks_record.read_text())
        context = json.loads(played["session_start"][0])["hookSpecificOutput"]
        self.assertEqual(context["hookEventName"], "SessionStart")
        self.assertIn('<factory-items job="001-feature"', context["additionalContext"])
        self.assertIn("You own (2): I-1 [open, blocking]", context["additionalContext"])
        first, second = played["stop"]
        self.assertEqual(json.loads(first)["decision"], "block")
        self.assertTrue(json.loads(first)["reason"].startswith("Before you finish: you still own I-1 (open, blocking)"))
        self.assertEqual(second, "", "stop_hook_active: the hook lets the run end")
        self.assertIn("continuing after the stop hook", result.final_text)
        from swfactory import items_hook

        state = settings_path.with_name("builder.items-hook.json")
        self.assertIsNone(items_hook.stop(str(state), {"stop_hook_active": False}), "build.md now answers both")
        self.assertEqual(ctx.items.store.read()["items"][0]["status"], "open", "the hook never writes a ledger")

    def test_a_chat_run_gets_no_stop_hook(self) -> None:
        ctx = self.ctx()
        ctx.items = self.items(cli_hooks=True)
        ctx.items.stop_guard = False
        self.assertTrue(self.hooked(True).run(ctx, "go").ok)
        argv = self.recorded()["argv"]
        settings = json.loads(Path(argv[argv.index("--settings") + 1]).read_text())
        self.assertEqual(list(settings["hooks"]), ["SessionStart"])


class ParserTest(unittest.TestCase):
    """Edge cases of the JSON event parser, fed line by line."""

    def setUp(self) -> None:
        self.events = Events()
        self.parser = cli._OutputParser(self.events.emit, "jsonl")

    def feed(self, *events: dict) -> None:
        for event in events:
            self.parser.stdout_line(json.dumps(event))

    def test_codex_file_change_mcp_and_failures(self) -> None:
        self.feed(
            {"type": "item.completed", "item": {"id": "f1", "type": "file_change", "status": "completed",
                                                "changes": [{"path": "a.py", "kind": "update"}]}},
            {"type": "item.started", "item": {"id": "m1", "type": "mcp_tool_call", "server": "rea",
                                              "tool": "search", "arguments": {"q": "x"},
                                              "status": "in_progress"}},
            {"type": "item.completed", "item": {"id": "m1", "type": "mcp_tool_call", "server": "rea",
                                                "tool": "search", "arguments": {"q": "x"},
                                                "status": "failed", "error": {"message": "nope"}}},
            {"type": "item.completed", "item": {"id": "c1", "type": "command_execution",
                                                "command": "false", "aggregated_output": "",
                                                "exit_code": 1, "status": "failed"}},
            {"type": "item.completed", "item": {"id": "t1", "type": "todo_list", "items": []}},
            {"type": "turn.failed", "error": {"message": "stream disconnected"}},
        )
        calls = self.events.of("agent.tool_call")
        self.assertEqual([c["name"] for c in calls], ["apply_patch", "mcp__rea__search", "shell"])
        results = {r["id"]: r for r in self.events.of("agent.tool_result")}
        self.assertEqual(results["f1"]["output"], "update a.py")
        self.assertEqual((results["m1"]["ok"], results["m1"]["output"]), (False, "nope"))
        self.assertFalse(results["c1"]["ok"])
        self.assertEqual(self.parser.error_hint, "stream disconnected")

    def test_claude_tool_errors_lists_and_error_results(self) -> None:
        self.feed(
            {"type": "assistant", "message": {"id": "a", "content": [
                {"type": "tool_use", "id": "t9", "name": "Bash", "input": {"command": "ls"}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t9", "is_error": True,
                 "content": [{"type": "text", "text": "denied"}, {"type": "image"}]}]}},
            {"type": "result", "subtype": "error_max_turns", "is_error": True, "num_turns": 7},
        )
        self.assertEqual(self.events.of("agent.tool_result"),
                         [{"id": "t9", "name": "Bash", "ok": False, "output": "denied\n[image]"}])
        self.assertEqual(self.parser.error_hint, "error_max_turns")
        self.assertEqual(self.parser.turn_count(), 7)
        self.assertIsNone(self.parser.usage)

    def test_qwen_error_result_message_wins(self) -> None:
        # Captured from Qwen Code 0.25.3 run headless without an auth type.
        self.parser.stdout_line(
            '{"type":"result","subtype":"error_during_execution","is_error":true,"duration_ms":0,'
            '"num_turns":0,"usage":{"input_tokens":0,"output_tokens":0},"permission_denials":[],'
            '"error":{"message":"No auth type is selected. Please configure an auth type."}}')
        self.assertEqual(self.parser.error_hint, "No auth type is selected. Please configure an auth type.")
        self.assertEqual(self.parser.usage, {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0})

    def test_claude_api_retry_becomes_the_error_hint(self) -> None:
        # Captured from Claude Code 2.1.89 while its API endpoint was failing.
        self.parser.stdout_line(
            '{"type":"system","subtype":"api_retry","attempt":3,"max_retries":10,'
            '"retry_delay_ms":2025.18,"error_status":null,"error":"unknown",'
            '"session_id":"6f0e8a2e-6a1c-4c55-9a3f-0e5e0f1d2a10","uuid":"e99f76eb"}')
        self.assertEqual(self.parser.error_hint, "API retry 3/10: unknown")
        self.assertEqual(self.parser.session_id, "6f0e8a2e-6a1c-4c55-9a3f-0e5e0f1d2a10")

    def test_error_streak_needs_count_and_time_and_resets_on_progress(self) -> None:
        now = [0.0]
        self.parser.clock = lambda: now[0]
        error = {"type": "error", "message": "Reconnecting... waiting for network"}

        def errors_at(*times: float) -> None:
            for at in times:
                now[0] = at
                self.feed(error)

        errors_at(0, 1, 2, 3, 4, 5)          # a quick retry burst the CLI may still recover from
        self.assertIsNone(self.parser.unreachable())
        now[0] = 100.0                        # long silence after the burst (a reconnect, long reasoning)
        self.assertIsNone(self.parser.unreachable(), "only an arriving error can give up")
        self.feed({"type": "item.started", "item": {"id": "i", "type": "command_execution", "command": "ls"}})
        errors_at(101, 120, 150)               # progress reset the streak: 3 errors are not enough
        self.assertIsNone(self.parser.unreachable())
        errors_at(170)                        # the 4th error in a row, 69s after the first of them
        self.assertIn("4 errors in a row over 69s", self.parser.unreachable() or "")
        self.assertEqual(self.parser.error_hint, "Reconnecting... waiting for network")

    def test_garbage_lines_are_ignored(self) -> None:
        for line in ("", "plain text", "{broken", "[1, 2]", '"string"', '{"type": "assistant"}',
                     '{"type": "assistant", "message": {"content": "plain"}}', '{"type": 5}'):
            self.parser.stdout_line(line)
        self.assertEqual(self.events.of("agent.text"), [{"text": "plain"}])
        self.assertEqual(len(self.events.of("agent.output")), 8)

    def test_reasoning_and_tool_input_are_truncated(self) -> None:
        self.feed({"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "r" * 3000},
            {"type": "tool_use", "id": "x", "name": "Write",
             "input": {"nested": {"deep": ["s" * 900]}, "n": 3}}]}})
        reasoning, = self.events.of("agent.reasoning")
        self.assertEqual(len(reasoning["text"]), 2000)
        self.assertTrue(reasoning["text"].endswith("…"))
        call, = self.events.of("agent.tool_call")
        self.assertEqual(len(call["input"]["nested"]["deep"][0]), 500)
        self.assertEqual(call["input"]["n"], 3)

    def test_tool_result_output_is_head_and_tail(self) -> None:
        self.feed({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "z", "content": "h" * 3000 + "TAIL"}]}})
        output = self.events.of("agent.tool_result")[0]["output"]
        self.assertLessEqual(len(output), 2000)
        self.assertTrue(output.startswith("hhh"))
        self.assertTrue(output.endswith("TAIL"))
        self.assertIn("chars truncated", output)


if __name__ == "__main__":
    unittest.main()
