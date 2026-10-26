"""Tests for the factory CLI (swfactory/__main__.py), the bin/factory launcher and install.sh.

Commands run in-process through main() against a temporary HOME and config. The
running server is a small fake HTTP API; the orchestrator, server, doctor and model
probes are replaced by fakes, so nothing touches the network or real API keys.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import io
import json
import os
import runpy
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest import mock

from swfactory import __main__ as cli
from swfactory import __version__
from swfactory.config import Config, init_config
from swfactory.events import EventBus
from swfactory.jobs import JobStore
from tests.helpers import make_repo

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "bin" / "factory"
INSTALL = ROOT / "install.sh"
TOKEN = "0123456789abcdef0123456789abcdef"


def free_port() -> int:
    """A localhost port nothing listens on (bound once, then released)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def job(job_id: str = "001-add-login", repo: str = "app", **fields: Any) -> dict:
    """A job.json-like dict."""
    base = {"id": job_id, "repo": repo, "feature": "Add login", "stage": "queued", "detail": "", "round": 0,
            "resumes": 0, "max_resumes": 2, "reviews": {}, "reason": None, "merged_commit": None,
            "job_dir": f"/data/jobs/{repo}/{job_id}", "usage": {"input_tokens": 1500, "output_tokens": 20}}
    return {**base, **fields}


def event(seq: int, type_: str, data: dict, agent: str | None = None, job_id: str = "001-add-login",
          ts: str = "2026-10-20T22:30:00Z") -> dict:
    return {"seq": seq, "ts": ts, "type": type_, "repo": "app", "job": job_id, "agent": agent, "data": data}


class CliCase(unittest.TestCase):
    """main() with HOME and FACTORY_CONFIG in a temporary directory."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sf-cli-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.config_path = self.home / ".config" / "software-factory" / "config.json"
        self.data_dir = self.home / ".local" / "share" / "software-factory"
        environ = mock.patch.dict(os.environ, {"HOME": str(self.home), "FACTORY_CONFIG": str(self.config_path)})
        environ.start()
        self.addCleanup(environ.stop)

    def cli(self, *argv: str, stdin: str = "") -> tuple[int, str, str]:
        """Run main(argv); returns (exit status, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(sys, "stdin", io.StringIO(stdin)):
            try:
                code = cli.main(list(argv))
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        return code, out.getvalue(), err.getvalue()

    def make_config(self, port: int | None = None, host: str = "127.0.0.1", **raw: Any) -> Config:
        """Write a config with the test token, the given server address and extra sections."""
        server = {"host": host, "port": port or free_port(), "token": TOKEN}
        config = Config(self.config_path, {"server": server, **raw})
        config.save()
        return config


# -- parser -------------------------------------------------------------------------


class ParserTests(CliCase):
    COMMANDS = [
        [], ["init"], ["serve"], ["url"], ["doctor"], ["repo"], ["repo", "add"], ["repo", "list"],
        ["repo", "remove"], ["submit"], ["run"], ["status"], ["approve"], ["retry"], ["cancel"],
        ["rework"], ["reject"], ["backlog"], ["backlog", "app", "add"], ["backlog", "app", "next"], ["logs"],
        ["plan"], ["plans"], ["plans", "run"], ["plans", "cancel"], ["plans", "retry"], ["agents"],
        ["agents", "new"], ["models"], ["models", "test"],
    ]

    def test_every_command_has_help(self) -> None:
        for argv in self.COMMANDS:
            with self.subTest(argv=argv):
                code, out, _ = self.cli(*argv, "--help")
                self.assertEqual(code, 0)
                self.assertIn("usage: factory", out)

    def test_top_level_help_lists_every_command(self) -> None:
        _, out, _ = self.cli("--help")
        for name in ("init", "serve", "url", "doctor", "repo", "submit", "run", "status", "approve", "retry",
                     "cancel", "rework", "reject", "backlog", "logs", "plan", "plans", "agents", "models"):
            self.assertRegex(out, rf"\n    {name}\s")

    def test_no_command_prints_help_and_fails(self) -> None:
        code, out, err = self.cli()
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("usage: factory", err)

    def test_version(self) -> None:
        code, out, _ = self.cli("--version")
        self.assertEqual((code, out.strip()), (0, f"factory {__version__}"))

    def test_arguments_reach_the_commands(self) -> None:
        parser = cli.build_parser()
        cases = [
            (["init", "--force", "--preset", "strata", "--data-dir", "/srv/f"], cli.cmd_init,
             {"force": True, "preset": "strata", "data_dir": "/srv/f"}),
            (["serve", "--host", "0.0.0.0", "--port", "9000"], cli.cmd_serve, {"host": "0.0.0.0", "port": 9000}),
            (["doctor", "--probe", "--json"], cli.cmd_doctor, {"probe": True, "json": True, "look_around": False}),
            (["doctor", "--look-around"], cli.cmd_doctor, {"probe": False, "look_around": True}),
            (["repo", "add", "/x", "--name", "x"], cli.cmd_repo_add, {"path": "/x", "name": "x"}),
            (["submit", "app", "Add X", "--preset", "hybrid"], cli.cmd_submit,
             {"repo": "app", "feature": "Add X", "preset": "hybrid"}),
            (["run", "app", "-"], cli.cmd_run, {"repo": "app", "feature": "-", "preset": None}),
            (["status", "app", "--json"], cli.cmd_status, {"repo": "app", "json": True}),
            (["reject", "app/003-add-login"], None, {"ref": ("app", "003-add-login"), "reason": ""}),
            (["rework", "app/003-add-login", "fix it"], None, {"note": "fix it"}),
            (["backlog", "app"], cli.cmd_backlog, {"repo": "app", "action": None}),
            (["backlog", "app", "add", "item"], cli.cmd_backlog, {"action": "add", "text": "item"}),
            (["logs", "app/003-add-login", "-f"], cli.cmd_logs, {"follow": True}),
            (["plan", "app", "Big thing"], cli.cmd_plan, {"repo": "app", "request": "Big thing"}),
            (["plans", "run", "app/P001-billing"], None, {"ref": ("app", "P001-billing")}),
            (["agents", "--json", "--repo", "app"], cli.cmd_agents, {"json": True, "repo": "app"}),
            (["agents", "new", "reviewer-perf", "--from", "builder"], cli.cmd_agents_new,
             {"name": "reviewer-perf", "source": "builder"}),
            (["agents", "new", "reviewer-perf"], cli.cmd_agents_new, {"source": "reviewer-code"}),
            (["models", "test", "local"], cli.cmd_models_test, {"key": "local"}),
        ]
        for argv, func, expected in cases:
            with self.subTest(argv=argv):
                args = parser.parse_args(argv)
                if func is not None:
                    self.assertIs(args.func, func)
                for key, value in expected.items():
                    self.assertEqual(getattr(args, key), value)

    def test_malformed_arguments_are_usage_errors(self) -> None:
        for argv, message in ((["approve", "app"], "expected <repo>/<job-id>"),
                              (["logs", "App/001-x"], "expected <repo>/<job-id>"),
                              (["cancel", "app/1-x"], "expected <repo>/<job-id>"),
                              (["plans", "run", "app/001-x"], "expected <repo>/<plan-id>"),
                              (["serve", "--port", "70000"], "not a TCP port"),
                              (["backlog", "app", "add"], "required"),
                              (["repo"], "required")):
            with self.subTest(argv=argv):
                code, _, err = self.cli(*argv)
                self.assertEqual(code, 2)
                self.assertIn(message, err)

    def test_python_version_check_runs_first(self) -> None:
        err = io.StringIO()
        with mock.patch.object(sys, "version_info", (3, 9, 18)), contextlib.redirect_stderr(err), \
                self.assertRaises(SystemExit) as raised:
            runpy.run_path(str(ROOT / "swfactory" / "__main__.py"), run_name="version_check")
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("needs Python 3.10 or newer; this is Python 3.9", err.getvalue())


# -- init, env file, url ------------------------------------------------------------


class InitTests(CliCase):
    def test_init_writes_a_private_config_with_a_token(self) -> None:
        code, out, err = self.cli("init")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), str(self.config_path))
        self.assertEqual(stat.S_IMODE(self.config_path.stat().st_mode), 0o600)
        token = Config.load(self.config_path).raw["server"]["token"]
        self.assertRegex(token, r"^[0-9a-f]{32}$")
        self.assertIn("DEEPSEEK_API_KEY", err)  # the default routing's key
        self.assertIn("factory serve", err)

    def test_init_refuses_an_existing_config_unless_forced(self) -> None:
        self.assertEqual(self.cli("init")[0], 0)
        before = Config.load(self.config_path).raw["server"]["token"]
        code, _, err = self.cli("init")
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)
        self.assertEqual(self.cli("init", "--force")[0], 0)
        self.assertNotEqual(Config.load(self.config_path).raw["server"]["token"], before)

    def test_init_applies_the_preset_with_settings(self) -> None:
        calls = []

        def apply_preset(config: Config, name: str) -> Config:
            calls.append((config.raw["server"]["token"], name))
            config.raw["models"]["opus"]["model"] = "preset-model"
            config.save()
            return Config.load(config.path)

        with mock.patch("swfactory.settings.apply_preset", apply_preset):
            code, _, err = self.cli("init", "--preset", "custom", "--data-dir", "rel/data")
        self.assertEqual(code, 0, err)
        self.assertEqual(calls[0][1], "custom")
        raw = Config.load(self.config_path).raw
        self.assertEqual(raw["models"]["opus"]["model"], "preset-model")
        self.assertEqual(raw["server"]["token"], calls[0][0])
        self.assertEqual(raw["data_dir"], str(Path.cwd() / "rel" / "data"))
        self.assertIn("(preset custom)", err)

    def test_a_failed_preset_leaves_no_config_behind(self) -> None:
        with mock.patch("swfactory.settings.apply_preset", side_effect=ValueError("unknown preset 'nope'")):
            code, _, err = self.cli("init", "--preset", "nope")
        self.assertEqual(code, 1)
        self.assertIn("unknown preset 'nope'", err)
        self.assertFalse(self.config_path.exists())

    def test_a_failed_preset_with_force_restores_the_old_config(self) -> None:
        self.assertEqual(self.cli("init")[0], 0)
        before = self.config_path.read_bytes()
        with mock.patch("swfactory.settings.apply_preset", side_effect=ValueError("broken preset")):
            code, _, _ = self.cli("init", "--force", "--preset", "broken")
        self.assertEqual(code, 1)
        self.assertEqual(self.config_path.read_bytes(), before)

    def test_a_malformed_config_is_one_error_line(self) -> None:
        self.config_path.parent.mkdir(parents=True)
        for text, message in (("{bad", "invalid JSON"), ("[]", "must be a JSON object")):
            with self.subTest(text=text):
                self.config_path.write_text(text)
                code, out, err = self.cli("init")
                self.assertEqual((code, out), (1, ""))
                self.assertTrue(err.startswith("factory: "), err)
                self.assertIn(message, err)
                self.assertIn("factory init --force", err)
                self.assertNotIn("Traceback", err)
                self.assertEqual(self.config_path.read_text(), text)
        self.assertEqual(self.cli("init", "--force")[0], 0)
        self.assertRegex(Config.load(self.config_path).raw["server"]["token"], r"^[0-9a-f]{32}$")

    def test_init_adds_a_token_to_a_config_written_by_repo_add(self) -> None:
        repo = make_repo(self.tmp, "early")
        self.assertEqual(self.cli("repo", "add", str(repo))[0], 0)
        code, _, err = self.cli("init")
        self.assertEqual(code, 0, err)
        config = Config.load(self.config_path)
        self.assertRegex(config.raw["server"]["token"], r"^[0-9a-f]{32}$")
        self.assertEqual(config.repo("early").path, repo.resolve())

    def test_init_with_a_shipped_preset(self) -> None:
        if not (ROOT / "examples" / "hybrid.json").is_file():
            self.skipTest("examples/hybrid.json is not there")
        code, _, err = self.cli("init", "--preset", "hybrid")
        self.assertEqual(code, 0, err)
        agents = Config.load(self.config_path).raw["agents"]
        self.assertEqual(agents["approver"]["model"], "claude-opus")
        self.assertEqual(agents["reviewer-code"]["model"], "claude-sonnet")


class EnvFileTests(CliCase):
    def write_env(self, text: str, mode: int = 0o600) -> Path:
        path = self.config_path.parent / "env"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(mode)
        return path

    def test_keys_are_exported_but_set_variables_win(self) -> None:
        self.write_env('# keys\nexport SF_TEST_API_KEY="sk-12345678"\nSF_TEST_PLAIN=1 # note\n'
                       "SF_TEST_SET=from-file\nnot a line\nBAD-NAME=x\n")
        with mock.patch.dict(os.environ, {"SF_TEST_SET": "from-env"}):
            code, _, err = self.cli("repo", "list")
            self.assertEqual(code, 0)
            self.assertEqual(os.environ.get("SF_TEST_API_KEY"), "sk-12345678")
            self.assertEqual(os.environ.get("SF_TEST_PLAIN"), "1")
            self.assertEqual(os.environ["SF_TEST_SET"], "from-env")
            self.assertNotIn("BAD-NAME", os.environ)
        self.assertNotIn("warning", err)

    def test_a_readable_env_file_is_flagged(self) -> None:
        path = self.write_env("SF_TEST_API_KEY=sk-12345678\n", mode=0o644)
        _, _, err = self.cli("repo", "list")
        self.assertIn(f"chmod 600 {path}", err)


class UrlTests(CliCase):
    def test_login_url_and_tunnel(self) -> None:
        self.make_config(port=9123)
        code, out, err = self.cli("url")
        self.assertEqual(code, 0)
        self.assertEqual(out, f"http://127.0.0.1:9123/login?token={TOKEN}\n")
        self.assertIn("ssh -N -L 9123:127.0.0.1:9123 ", err)

    def test_wildcard_and_ipv6_hosts(self) -> None:
        for host, base in (("0.0.0.0", "http://127.0.0.1:8800"), ("::1", "http://[::1]:8800"),
                           ("localhost", "http://localhost:8800")):
            with self.subTest(host=host):
                self.make_config(port=8800, host=host)
                code, out, err = self.cli("url")
                self.assertEqual((code, out), (0, f"{base}/login?token={TOKEN}\n"))
                self.assertIn("ssh -N -L 8800:127.0.0.1:8800", err)

    def test_another_bind_address_needs_no_tunnel(self) -> None:
        self.make_config(port=8800, host="10.1.2.3")
        _, out, err = self.cli("url")
        self.assertEqual(out, f"http://10.1.2.3:8800/login?token={TOKEN}\n")
        self.assertNotIn("ssh", err)

    def test_no_token_means_init_first(self) -> None:
        code, out, err = self.cli("url")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("run factory init first", err)


class NoAnswerTests(CliCase):
    def test_a_change_with_no_answer_may_have_been_done(self) -> None:
        """DESIGN §40: a request the server got but never answered may have been done; a read says only that no
        answer came."""
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        self.addCleanup(listener.close)

        def hang_up() -> None:   # read each request, then close without an answer
            for _ in range(2):
                conn, _ = listener.accept()
                with conn:
                    conn.settimeout(5)
                    data = b""
                    while b"\r\n\r\n" not in data:
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        data += chunk

        threading.Thread(target=hang_up, daemon=True).start()
        client = cli.Client(self.make_config(port=listener.getsockname()[1]))
        with self.assertRaises(cli.CliError) as posted:
            client.post("/api/jobs/app/001/approve", {})
        self.assertIn("no answer from the server", str(posted.exception))
        self.assertIn("it may have been done, so look before you run it again", str(posted.exception))
        with self.assertRaises(cli.CliError) as read:
            client.get("/api/jobs")
        self.assertIn("no answer from the server", str(read.exception))
        self.assertNotIn("may have been done", str(read.exception))


# -- doctor, agents, models -----------------------------------------------------------


class DoctorTests(CliCase):
    def run_doctor(self, checks: list[dict], *argv: str) -> tuple[int, str, str, list[dict]]:
        calls: list[dict] = []

        def run_checks(config: Config, factory: Any = None, probe_models: bool = False,
                       look_around: bool = False, look_skip_ports: Any = frozenset()) -> list[dict]:
            calls.append({"config": config, "factory": factory, "probe_models": probe_models,
                          "look_around": look_around, "look_skip_ports": set(look_skip_ports)})
            return checks

        with mock.patch("swfactory.doctor.run_checks", run_checks):
            code, out, err = self.cli("doctor", *argv)
        return code, out, err, calls

    def test_all_ok(self) -> None:
        code, out, err, calls = self.run_doctor([{"name": "python", "ok": True, "level": "info",
                                                  "detail": "3.12.3"}])
        self.assertEqual(code, 0)
        self.assertEqual(out, "ok    python: 3.12.3\n")
        self.assertIn("0 error(s), 0 warning(s)", err)
        self.assertFalse(calls[0]["probe_models"])
        self.assertFalse(calls[0]["look_around"], "the doctor looks for model servers only when asked")

    def test_warnings_do_not_fail(self) -> None:
        code, out, _, _ = self.run_doctor([{"name": "rg", "ok": False, "level": "warn", "detail": "missing"}])
        self.assertEqual(code, 0)
        self.assertIn("warn  rg: missing", out)

    def test_an_error_level_failure_exits_1(self) -> None:
        checks = [{"name": "git", "ok": True, "level": "info", "detail": ""},
                  {"name": "provider deepseek key", "ok": False, "level": "error", "detail": "not set"},
                  {"name": "rg", "ok": False, "level": "warn", "detail": "missing"}]
        code, out, err, _ = self.run_doctor(checks)
        self.assertEqual(code, 1)
        self.assertIn("FAIL  provider deepseek key: not set", out)
        self.assertIn("1 error(s), 1 warning(s)", err)

    def test_probe_and_json(self) -> None:
        checks = [{"name": "model local", "ok": True, "level": "info", "detail": "tool calls"}]
        code, out, _, calls = self.run_doctor(checks, "--probe", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), checks)
        self.assertTrue(calls[0]["probe_models"])
        self.assertFalse(calls[0]["look_around"])

    def test_look_around_only_when_asked(self) -> None:
        checks = [{"name": "model servers here", "ok": True, "level": "info", "detail": "none answered"}]
        code, out, _, calls = self.run_doctor(checks, "--look-around")
        self.assertEqual(code, 0)
        self.assertIn("ok    model servers here: none answered", out)
        self.assertEqual([(c["look_around"], c["probe_models"]) for c in calls], [(True, False)])
        self.assertEqual(calls[0]["look_skip_ports"], set(), "no `factory serve` runs: only the config's port")

    def test_the_look_around_skips_a_running_server_s_own_port(self) -> None:
        """`factory serve --port` records the port it really took in factory.lock: `factory doctor --look-around`
        skips it as well as the config's, so the harness never asks itself (as POST /api/setup/look-around does)."""
        configured, served = free_port(), free_port()
        self.make_config(port=configured)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "factory.lock").write_text(f"{os.getpid()}\nhttp://127.0.0.1:{served}\n")
        asked: list[set] = []

        def finder(skip_ports: Any = frozenset(), *args: Any, **kwargs: Any) -> list[dict]:
            asked.append(set(skip_ports))
            return []
        with mock.patch("swfactory.listeners.model_servers", finder):
            code, out, err = self.cli("doctor", "--look-around", "--json")
            self.assertEqual(asked, [{configured, served}], err)
            self.assertIn("model servers here", [c["name"] for c in json.loads(out)])
            self.cli("doctor", "--json")
        self.assertEqual(len(asked), 1, "without --look-around the doctor does not look")


class AgentTests(CliCase):
    def test_lists_the_shipped_agents_with_their_routes(self) -> None:
        self.make_config()
        code, out, _ = self.cli("agents")
        self.assertEqual(code, 0)
        names = [line.split()[0] for line in out.splitlines()]
        for name in ("spec-writer", "builder", "approver", "reviewer-security", "lead", "coder"):
            self.assertIn(name, names)
        builder = next(line for line in out.splitlines() if line.startswith("builder "))
        self.assertIn("deepseek/deepseek-v4-pro", builder)
        self.assertIn("builtin", builder)

    def test_json_rows(self) -> None:
        self.make_config(agents={"reviewer-ux": {"model": "local", "driver": "qwen"}})
        code, out, _ = self.cli("agents", "--json")
        self.assertEqual(code, 0)
        rows = {row["name"]: row for row in json.loads(out)}
        self.assertEqual((rows["reviewer-ux"]["model"], rows["reviewer-ux"]["provider"],
                          rows["reviewer-ux"]["driver"], rows["reviewer-ux"]["source"]),
                         ("local", "strata", "qwen", "builtin"))
        self.assertEqual(rows["reviewer-ux"]["tier"], "sonnet")
        self.assertEqual(rows["reviewer-ux"]["cli_model"], "strata")   # qwen: the resolved model id
        self.assertIsNone(rows["builder"]["cli_model"])

    def test_cli_driver_rows_show_the_model_the_cli_is_launched_with(self) -> None:
        """A claude-driven sonnet agent runs `claude --model haiku` on the user's login: the listing says
        claude/haiku (as agent.start does), not the tier entry's deepseek/deepseek-v4-flash."""
        self.make_config(agents={"reviewer-code": {"driver": "claude"}},
                         drivers={"claude": {"type": "cli", "preset": "claude", "models": {"sonnet": "haiku"}}})
        code, out, _ = self.cli("agents")
        self.assertEqual(code, 0)
        line = next(line for line in out.splitlines() if line.startswith("reviewer-code "))
        self.assertTrue(line.endswith("claude/haiku"), line)
        self.assertNotIn("deepseek", line)
        builder = next(line for line in out.splitlines() if line.startswith("builder "))
        self.assertTrue(builder.endswith("deepseek/deepseek-v4-pro"), builder)
        rows = {row["name"]: row for row in json.loads(self.cli("agents", "--json")[1])}
        self.assertEqual((rows["reviewer-code"]["driver"], rows["reviewer-code"]["cli_model"]), ("claude", "haiku"))

    def test_new_agent_is_written_to_the_user_dir(self) -> None:
        self.make_config()
        code, out, err = self.cli("agents", "new", "reviewer-perf")
        self.assertEqual(code, 0, err)
        path = Path(out.strip())
        self.assertEqual(path, self.home / ".config" / "software-factory" / "agents" / "reviewer-perf.md")
        self.assertIn("name: reviewer-perf", path.read_text())
        self.assertIn('add "perf" to pipeline.reviewers', err)
        rows = {row["name"]: row for row in json.loads(self.cli("agents", "--json")[1])}
        self.assertEqual(rows["reviewer-perf"]["source"], "user")

    def test_new_agent_with_a_moved_config_is_seen_by_the_factory(self) -> None:
        from swfactory.orchestrator import Factory

        moved = self.tmp / "alt" / "config.json"
        with mock.patch.dict(os.environ, {"FACTORY_CONFIG": str(moved)}):
            self.assertEqual(self.cli("init")[0], 0)
            self.assertTrue(moved.is_file())
            code, out, err = self.cli("agents", "new", "reviewer-perf")
            self.assertEqual(code, 0, err)
            path = Path(out.strip())
            # Agent overrides do not move with $FACTORY_CONFIG: the factory reads only this dir.
            self.assertEqual(path, self.home / ".config" / "software-factory" / "agents" / "reviewer-perf.md")
            rows = {row["name"]: row for row in json.loads(self.cli("agents", "--json")[1])}
            self.assertEqual(rows["reviewer-perf"]["source"], "user")
            agents = Factory(Config.load(), EventBus()).agents()
        self.assertIn("reviewer-perf", agents)
        self.assertEqual(Path(agents["reviewer-perf"].path), path)

    def test_new_agent_errors(self) -> None:
        self.make_config()
        self.assertEqual(self.cli("agents", "new", "helper", "--from", "builder")[0], 0)
        for argv, message in ((["helper"], "already exists"), (["x2", "--from", "nobody"], "unknown agent"),
                              (["Bad_Name"], "invalid agent name")):
            with self.subTest(argv=argv):
                code, out, err = self.cli("agents", "new", *argv)
                self.assertEqual((code, out), (1, ""))
                self.assertIn(message, err)

    def test_unknown_repo(self) -> None:
        self.make_config()
        code, _, err = self.cli("agents", "--repo", "nope")
        self.assertEqual(code, 1)
        self.assertIn("unknown repo 'nope'", err)


class PersonaOfflineTests(CliCase):
    def test_personas_lists_the_shipped_kate(self) -> None:
        self.make_config()
        code, out, err = self.cli("personas")
        self.assertEqual(code, 0, err)
        self.assertRegex(out, r"kate\s+openwebui\s+Kate through Open WebUI\s+\(models\.kate, private, card no\)")
        code, out, _ = self.cli("personas", "--json")
        self.assertEqual(json.loads(out)[0]["binding"]["model"], "kate")
        code, _, err = self.cli("personas", "card", "ghost")
        self.assertEqual(code, 1)
        self.assertIn("unknown persona 'ghost'", err)
        code, _, err = self.cli("personas", "card", "kate")
        self.assertEqual(code, 0)
        self.assertIn("no profile card for 'kate' yet", err)
        self.assertIn("live: no provider 'openwebui': apply the openwebui preset", err)

    def test_the_card_says_what_runs_where(self) -> None:
        from swfactory import personas

        self.make_config()
        record = {"id": "kate", "name": "Kate", "params": {"system": "You are Kate."},
                  "meta": {"toolIds": ["calendar"], "skillIds": ["kind-replies"]}}
        personas.save_profile(personas.profile_card(personas.parse_openwebui_export([record]), source="export file"))
        code, out, _ = self.cli("personas", "card", "kate")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        own = lines.index("  The persona's own Open WebUI chats")
        self.assertEqual(lines[own + 1], "    has: system prompt; settings; Open WebUI built-in memory; Open WebUI "
                                         "Python tools (calendar); skills (kind-replies)")
        chat = lines.index("  A harness chat with the persona [kate]")
        self.assertIn("Open WebUI Python tools (calendar)", lines[chat + 2], "listed under not")
        self.assertIn("Open WebUI built-in memory", lines[chat + 2], "not on the harness path")
        self.assertNotIn("Python tools", lines[chat + 1])
        self.assertIn("  A browsing chat (not set up)", lines)
        self.assertTrue(any(line.startswith("  code: Code through a factory job") for line in lines))
        self.assertTrue(any(line.startswith("import: At the first import the harness had no earlier copy")
                            for line in lines))
        self.assertIn("  changes before import: none attached (factory personas before-import <name> --attach "
                      "<receipt id>)", lines)
        data = json.loads(self.cli("personas", "card", "kate", "--json")[1])
        self.assertIs(data["capability_map"]["parity"], False)
        self.assertEqual((data["import"]["diff"], data["import"]["changes_before_import"]), (None, []))


class ModelTests(CliCase):
    def test_lists_model_entries(self) -> None:
        self.make_config()
        code, out, _ = self.cli("models")
        self.assertEqual(code, 0)
        lines = {line.split()[0]: line for line in out.splitlines()}
        self.assertIn("api    deepseek/deepseek-v4-pro", lines["opus"])
        self.assertIn("local  strata/strata  ctx 262144", lines["local"])
        rows = {row["key"]: row for row in json.loads(self.cli("models", "--json")[1])}
        self.assertEqual(rows["local"]["where"], "local")

    def probe(self, result: dict) -> tuple[int, str, str]:
        self.make_config()
        with mock.patch("swfactory.settings.test_model", return_value=result) as test_model:
            outcome = self.cli("models", "test", "local")
        self.assertEqual(test_model.call_args.args[1], "local")
        return outcome

    def test_a_model_that_calls_tools_passes(self) -> None:
        code, out, _ = self.probe({"ok": True, "latency_ms": 840, "text": "OK", "error": None,
                                   "tool_call": True, "tokens_per_second": 71.25})
        self.assertEqual(code, 0)
        self.assertIn("local: ok, 840 ms, tool calls: yes, 71.2 tok/s", out)
        self.assertIn("reply: OK", out)

    def test_no_tool_call_fails_with_a_hint(self) -> None:
        code, out, err = self.probe({"ok": True, "latency_ms": 900, "text": "OK", "error": "tool probe: none",
                                     "tool_call": False, "tokens_per_second": None})
        self.assertEqual(code, 1)
        self.assertIn("tool calls: NO", out)
        self.assertIn("--jinja", err)

    def test_an_unreachable_model_fails(self) -> None:
        code, out, err = self.probe({"ok": False, "latency_ms": None, "text": "", "error": "connection refused",
                                     "tool_call": False, "tokens_per_second": None})
        self.assertEqual(code, 1)
        self.assertIn("local: FAILED", out)
        self.assertIn("error: connection refused", out)
        self.assertNotIn("--jinja", err)

    def test_unknown_model(self) -> None:
        self.make_config()
        code, _, err = self.cli("models", "test", "nope")
        self.assertEqual(code, 1)
        self.assertIn("unknown model 'nope'", err)


class EstimateTests(CliCase):
    def test_estimates_offline_from_defaults(self) -> None:
        repo = make_repo(self.tmp, "app")
        self.make_config(repos=[{"name": "app", "path": str(repo)}],
                         models={"opus": {"price_in": 1, "price_out": 2}})
        code, out, err = self.cli("estimate", "app")
        self.assertEqual(code, 0, err)
        builder = next(line for line in out.splitlines() if line.startswith("builder"))
        self.assertIn("opus (deepseek/deepseek-v4-pro)", builder)
        self.assertIn("1,500,000 in / 40,000 out", builder)
        self.assertIn("[default table]", builder)
        self.assertIn("+ steps without prices", out.splitlines()[-1], "the sonnet tier has no prices")
        guard = next(line for line in out.splitlines() if line.startswith("independence:"))
        self.assertTrue(guard.startswith("independence: builder on opus (deepseek, route): approver runs with "
                                         "reduced independence on opus;"), "one family: every judge is reduced")
        approver = next(line for line in out.splitlines() if line.startswith("approver"))
        self.assertTrue(approver.endswith("(independence reduced)"), approver)
        data = json.loads(self.cli("estimate", "app", "--kind", "plan", "--json")[1])
        self.assertEqual(data["independence"][0]["builder"], {"via": "route", "model": "opus", "family": "deepseek"})
        self.make_config(repos=[{"name": "app", "path": str(repo)}],
                         models={"gemini": {"provider": "deepseek", "model": "google/gemini-x"}},
                         agents={"reviewer-code": {"model": "opus", "independent_alternate": "gemini"},
                                 "reviewer-security": {"model": "opus", "independent_alternate": False}})
        out = self.cli("estimate", "app")[1]
        code_row = next(line for line in out.splitlines() if line.startswith("reviewer-code"))
        self.assertIn("gemini (deepseek/google/gemini-x)", code_row)
        self.assertTrue(code_row.endswith("(instead of opus: independent alternate)"), code_row)
        guard = next(line for line in out.splitlines() if line.startswith("independence:"))
        self.assertIn("reviewer-code moves to gemini", guard)
        self.assertIn("reviewer-security shares the builder's family on opus (accepted)", guard)
        self.assertEqual((data["kind"], data["jobs"], data["basis"]), ("plan", 3, "defaults"))
        code, _, err = self.cli("estimate", "app", "--profile", "nope")
        self.assertEqual(code, 1)
        self.assertIn("unknown profile 'nope'", err)


# -- repos ------------------------------------------------------------------------------


class RepoOfflineTests(CliCase):
    def test_add_list_remove_without_a_server(self) -> None:
        self.make_config()  # its port has no server
        repo = make_repo(self.tmp, "my-app")
        code, out, err = self.cli("repo", "add", str(repo))
        self.assertEqual(code, 0, err)
        self.assertEqual(out, f"my-app  {repo.resolve()}\n")
        self.assertEqual(Config.load(self.config_path).repo("my-app").path, repo.resolve())
        self.assertIn("my-app", self.cli("repo", "list")[1])
        self.assertEqual(self.cli("repo", "remove", "my-app")[:2], (0, "my-app\n"))
        self.assertEqual(Config.load(self.config_path).repos(), [])

    def test_add_before_init_works(self) -> None:
        repo = make_repo(self.tmp, "early")
        code, _, err = self.cli("repo", "add", str(repo), "--name", "first")
        self.assertEqual(code, 0, err)
        self.assertEqual(Config.load(self.config_path).repo("first").path, repo.resolve())

    def test_errors(self) -> None:
        self.make_config()
        code, _, err = self.cli("repo", "add", str(self.tmp))
        self.assertEqual(code, 1)
        self.assertIn("not a git repository", err)
        code, _, err = self.cli("repo", "remove", "ghost")
        self.assertEqual(code, 1)
        self.assertIn("unknown repo 'ghost'", err)
        code, _, err = self.cli("repo", "list")
        self.assertEqual(code, 0)
        self.assertIn("no repos yet", err)


# -- the HTTP client -------------------------------------------------------------------


class FakeApi:
    """A stand-in for the factory server: canned JSON per (method, path), and an SSE stream."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], tuple[int, Any]] = {}
        self.requests: list[dict] = []
        self.sse: list[str] = []
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                api.handle(self)

            def do_POST(self) -> None:
                api.handle(self)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True)
        self.thread.start()

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def stream(self, *events: dict) -> None:
        self.sse = [": ping\n\n"] + [f"id: {ev['seq']}\nevent: {ev['type']}\ndata: {json.dumps(ev)}\n\n"
                                     for ev in events]

    def handle(self, request: BaseHTTPRequestHandler) -> None:
        length = int(request.headers.get("Content-Length") or 0)
        body = json.loads(request.rfile.read(length)) if length else None
        self.requests.append({"method": request.command, "path": request.path, "body": body,
                              "auth": request.headers.get("Authorization"),
                              "type": request.headers.get("Content-Type")})
        if request.path.startswith("/api/events"):
            request.send_response(200)
            request.send_header("Content-Type", "text/event-stream")
            request.end_headers()
            for chunk in self.sse:
                request.wfile.write(chunk.encode())
                request.wfile.flush()
            return
        status, payload = self.routes.get((request.command, request.path), (404, {"error": "no such route"}))
        data = json.dumps(payload).encode()
        request.send_response(status)
        request.send_header("Content-Type", "application/json")
        request.send_header("Content-Length", str(len(data)))
        request.end_headers()
        request.wfile.write(data)


class ClientTests(CliCase):
    def setUp(self) -> None:
        super().setUp()
        self.api = FakeApi()
        self.addCleanup(self.api.close)
        self.make_config(port=self.api.port)

    def route(self, method: str, path: str, payload: Any, status: int = 200) -> None:
        self.api.routes[(method, path)] = (status, payload)

    def last(self) -> dict:
        return self.api.requests[-1]

    def test_status_lists_jobs_with_the_token(self) -> None:
        jobs = [job(stage="review", round=2, resumes=1, detail="round 2: waiting for ui"),
                job("002-dark-mode", stage="merged", merged_commit="3f2c1ab9e8d7c6b5"),
                job("001-fix", repo="other", stage="needs-human", reason="touches protected path package.json")]
        self.route("GET", "/api/state", {"repos": [{"name": "app"}, {"name": "other"}], "jobs": jobs})
        code, out, _ = self.cli("status")
        self.assertEqual(code, 0)
        self.assertEqual(self.last()["auth"], f"Bearer {TOKEN}")
        lines = out.splitlines()
        self.assertEqual(len(lines), 3)
        self.assertRegex(lines[0], r"^app/001-add-login\s+review\s+r2 1/2\s+round 2: waiting for ui$")
        self.assertRegex(lines[1], r"merged\s+r0 0/2\s+3f2c1ab9e8d7$")
        self.assertIn("touches protected path package.json", lines[2])
        _, out, _ = self.cli("status", "other")
        self.assertEqual(len(out.splitlines()), 1)
        _, out, _ = self.cli("status", "app", "--json")
        self.assertEqual([j["id"] for j in json.loads(out)], ["001-add-login", "002-dark-mode"])
        code, _, err = self.cli("status", "ghost")
        self.assertEqual(code, 1)
        self.assertIn("unknown repo 'ghost'", err)

    def test_submit(self) -> None:
        self.route("POST", "/api/jobs", {"job": job("004-add-search")}, status=201)
        code, out, err = self.cli("submit", "app", "Add search")
        self.assertEqual((code, out), (0, "app/004-add-search\n"))
        self.assertEqual(self.last()["body"], {"repo": "app", "feature": "Add search"})
        self.assertEqual(self.last()["type"], "application/json")
        self.assertIn("factory logs app/004-add-search -f", err)
        self.cli("submit", "app", "-", "--preset", "hybrid", stdin="Long feature\nfrom stdin\n")
        self.assertEqual(self.last()["body"], {"repo": "app", "feature": "Long feature\nfrom stdin\n",
                                               "routing": "hybrid"})
        code, _, err = self.cli("submit", "app", "-", stdin="  \n")
        self.assertEqual(code, 1)
        self.assertIn("empty", err)
        self.cli("submit", "app", "Export it", "--delivery", "folder")
        self.assertEqual(self.last()["body"], {"repo": "app", "feature": "Export it", "delivery": "folder"})
        code, _, err = self.cli("submit", "app", "x", "--delivery", "zip")
        self.assertEqual(code, 2)
        self.assertIn("invalid choice: 'zip'", err)

    def test_personas_import_and_check(self) -> None:
        export = self.tmp / "kate-1760000000000.json"
        export.write_text(json.dumps([{"id": "kate", "params": {"system": "x"},
                                       "meta": {"profile_image_url": "data:image/png;base64,AAAA"},
                                       "background_image_data": "data:image/png;base64,BBBB"}]))
        self.route("POST", "/api/personas/import", {"card": {"model_id": "kate"}, "path": "/x/kate.json",
                                                    "unavailable": {"tools": ["calendar"], "skills": []},
                                                    "snapshot": None}, status=201)
        code, out, err = self.cli("personas", "import", str(export))
        self.assertEqual((code, out), (0, "kate: profile card saved (/x/kate.json)\n"), err)
        self.assertEqual(self.last()["body"], {"export": [{"id": "kate", "params": {"system": "x"}, "meta": {}}]},
                         "the images are cosmetic and stay home")
        self.assertIn("not in an imported copy: tools calendar", err)
        self.cli("personas", "import", "--model", "kate", "--snapshot", "kate-offline", "--snapshot-model", "flash")
        self.assertEqual(self.last()["body"], {"source": "openwebui", "model_id": "kate",
                                               "snapshot": {"name": "kate-offline", "model": "flash"}})
        code, _, err = self.cli("personas", "import")
        self.assertEqual(code, 1)
        self.assertIn("give an Export file, or --model", err)
        self.route("POST", "/api/personas/check", {"checked": [
            {"persona": "kate", "model": "kate", "kind": "behaviour", "version_id": "0123456789abcdef", "item": "system:S-1"},
            {"persona": "kate-offline", "model": "kate", "error": "cannot reach"}]})
        code, out, _ = self.cli("personas", "check", "kate")
        self.assertEqual(self.last()["body"], {"persona": "kate"})
        self.assertEqual(out.splitlines(), ["kate  behaviour  version 0123456789ab  → system:S-1",
                                            "kate-offline  error: cannot reach"])

    def test_personas_before_import_and_the_read_ledger(self) -> None:
        row = {"id": "CHG-1", "by": "operator", "attached_at": "2026-10-22T09:00:00Z", "time": "2026-10-21T16:00:00Z",
               "intent": "Rewrite her own prompt", "not_verified": ["the copy not re-imported yet"], "verified": [],
               "label": "reported by the operator"}
        self.route("POST", "/api/personas/kate/before-import",
                   {"import": {"baseline": "the import is the baseline", "changes_before_import": [row]}})
        code, out, err = self.cli("personas", "before-import", "kate", "--attach", "CHG-1", "--by", "operator")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.last()["body"], {"attach": ["CHG-1"], "detach": [], "by": "operator"})
        self.assertIn("    NOT verified: the copy not re-imported yet", out)
        self.assertIn("    verified: nothing", out)
        self.assertNotIn("    outcome:", out, "a row without an outcome prints none")
        unknown = {**row, "verified": ["the unit file was written"], "not_verified": [],
                   "outcome": {"state": "outcome_unknown", "words": "outcome unknown (reported): it may or may not "
                                                                     "have happened"}}
        self.route("POST", "/api/personas/kate/before-import",
                   {"import": {"baseline": "the import is the baseline", "changes_before_import": [unknown]}})
        code, out, err = self.cli("personas", "before-import", "kate", "--attach", "CHG-1")
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertLess(lines.index("    outcome: outcome unknown (reported): it may or may not have happened"),
                        lines.index("    verified (reported): the unit file was written"),
                        "how the change ended comes before what was reported verified")
        code, _, err = self.cli("personas", "before-import", "kate")
        self.assertEqual(code, 1)
        self.assertIn("--attach ID or --detach ID", err)
        old, new = "a" * 64, "b" * 64
        self.route("GET", "/api/personas/kate/external", {
            "ledger": True, "reads": 2, "kept_bytes": 20, "cap_bytes": 100, "sqlite": [],
            "files": [{"name": "pets.md", "versions": [
                {"sha256": new, "bytes": 10, "first_read_at": "2026-10-22T09:00:00Z",
                 "last_read_at": "2026-10-22T09:00:00Z", "reads": 1, "kept": True, "redacted": False},
                {"sha256": old, "bytes": 10, "first_read_at": "2026-10-21T09:00:00Z",
                 "last_read_at": "2026-10-21T09:00:00Z", "reads": 1, "kept": True, "redacted": True}]}]})
        code, out, _ = self.cli("personas", "ledger", "kate")
        self.assertEqual(code, 0)
        self.assertIn("read ledger: on; 2 reads, 20 of 100 bytes kept (private)", out)
        self.assertIn(f"  {old[:12]}  10 bytes  read 2026-10-21T09:00 to 2026-10-21T09:00 (1x)  kept, redacted", out)
        self.route("GET", f"/api/personas/kate/external/diff?from={old}&to={new}", {"diff": "-Pip is 3.\n+Pip is 4."})
        code, out, _ = self.cli("personas", "ledger", "kate", "--diff", old, new)
        self.assertEqual(out, "-Pip is 3.\n+Pip is 4.\n")
        self.route("POST", "/api/personas/kate/external/restore", {"restore": {
            "name": "pets.md", "sha256": old, "text": "Pip is 3.\n", "current": {"sha256": new}, "warnings": [],
            "apply": "The harness never writes the persona's own memory."}})
        code, out, err = self.cli("personas", "ledger", "kate", "--restore", old, "--file", "pets.md")
        self.assertEqual(self.last()["body"], {"sha256": old, "file": "pets.md"})
        self.assertEqual(out, f"restore pets.md to {old[:12]} (the newest read held {new[:12]})\nPip is 3.\n")
        self.assertIn("never writes", err)
        self.route("POST", "/api/personas/kate/external/erase", {"erased": {"sha256": old, "copies": 1, "entries": 2}})
        code, out, err = self.cli("personas", "ledger", "kate", "--erase", old)
        self.assertEqual((code, self.last()["body"]), (0, {"sha256": old}))
        self.assertEqual(out, f"erased {old[:12]}: 1 copy removed, 2 manifest entries keep its sha256 only\n")
        self.assertIn("her own store is not touched", err)
        self.route("GET", "/api/personas/kate/external", {
            "ledger": True, "reads": 3, "kept_bytes": 20, "cap_bytes": 100, "files": [],
            "erased": [{"sha256": old, "erased_at": "2026-10-23T09:00:00Z", "reads": 2}],
            "caps": {"evicted": 4, "evicted_at": "2026-10-23T08:00:00Z"},
            "sqlite": [{"at": "2026-10-23T09:00:00Z", "rows": None, "sha256": None, "error": "the query failed",
                        "detail": "no such column: hidden"}]})
        code, out, err = self.cli("personas", "ledger", "kate")
        self.assertIn(f"erased  {old[:12]}  at 2026-10-23T09:00 (2x read)", out)
        self.assertIn("database read 2026-10-23T09:00: could not be read (the query failed: no such column: hidden)",
                      out)
        self.assertIn("4 older copies removed to stay under the cap", err)

    def test_plan_delivery(self) -> None:
        self.route("POST", "/api/plans", {"plan": {"id": "P001-x", "repo": "app", "stage": "planning"}}, status=201)
        code, out, _ = self.cli("plan", "app", "Big change", "--delivery", "branch")
        self.assertEqual((code, out), (0, "app/P001-x\n"))
        self.assertEqual(self.last()["body"], {"repo": "app", "request": "Big change", "delivery": "branch"})

    def test_status_shows_where_a_job_was_delivered(self) -> None:
        jobs = [job(stage="delivered", delivered={"mode": "folder", "path": "/srv/out/app/001-add-login"}),
                job("002-branch", stage="delivered", delivered={"mode": "branch", "branch": "factory/002-branch"})]
        self.route("GET", "/api/state", {"repos": [{"name": "app"}], "jobs": jobs})
        _, out, _ = self.cli("status")
        lines = out.splitlines()
        self.assertRegex(lines[0], r"delivered\s+r0 0/2\s+/srv/out/app/001-add-login$")
        self.assertRegex(lines[1], r"delivered\s+r0 0/2\s+factory/002-branch$")

    def test_job_actions(self) -> None:
        for action in ("approve", "retry", "cancel", "rework", "reject"):
            self.route("POST", f"/api/jobs/app/001-add-login/{action}", {"job": job(stage="build")})
        cases = [(["approve"], {}), (["retry"], {}), (["cancel"], {}), (["rework", "x", "Use bcrypt"], None),
                 (["reject", "x", "Not needed"], {"reason": "Not needed"}), (["reject", "x"], {"reason": ""})]
        for argv, body in cases:
            with self.subTest(argv=argv):
                command, *rest = argv
                extra = rest[1:] if rest else []
                code, out, _ = self.cli(command, "app/001-add-login", *extra)
                self.assertEqual(code, 0)
                self.assertTrue(out.startswith("app/001-add-login  build"))
                self.assertEqual(self.last()["path"], f"/api/jobs/app/001-add-login/{command}")
                self.assertEqual(self.last()["body"], body if body is not None else {"note": "Use bcrypt"})

    def test_approve_carries_open_must_do_items_only_when_asked(self) -> None:
        self.route("POST", "/api/jobs/app/001-add-login/approve", {"job": job(stage="merging")})
        self.route("GET", "/api/jobs/app/001-add-login/items", {"items": [
            {"id": "I-1", "title": "greet() says hello", "status": "open", "blocking": True, "word": "To do"},
            {"id": "I-2", "title": "A note", "status": "proposed", "blocking": False},
            {"id": "I-3", "title": "Proven", "status": "verified", "blocking": True}]})
        code, out, err = self.cli("approve", "app/001-add-login", "--carry-open-items")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.last()["body"], {"carry_items": ["I-1"]})
        self.assertIn("carrying I-1 [To do] greet() says hello", err)
        self.cli("approve", "app/001-add-login")
        self.assertEqual(self.last()["body"], {}, "without the flag nothing is waived: the server says 409")
        self.route("POST", "/api/jobs/app/001-add-login/approve",
                   {"error": "1 must-do item is not proven: I-1. ... (factory approve --carry-open-items)",
                    "open_blocking": ["I-1"]}, status=409)
        code, _, err = self.cli("approve", "app/001-add-login")
        self.assertEqual(code, 1)
        self.assertIn("--carry-open-items", err)

    def test_confirm(self) -> None:
        free, paid = "Free plan says Hello (product)", "Paid plan gets no greeting (product)"
        self.route("GET", "/api/jobs/app/001-add-login/assumptions",
                   {"marked": [free, paid], "confirmed": [free], "unconfirmed": [paid]})
        code, out, err = self.cli("confirm", "app/001-add-login")
        self.assertEqual((code, out), (0, f"[x] {free}\n[ ] {paid}\n"))
        self.assertIn("factory confirm app/001-add-login --all", err)
        self.route("POST", "/api/jobs/app/001-add-login/confirm",
                   {"job": job(), "assumptions": {"marked": [free, paid], "confirmed": [free, paid], "unconfirmed": []}})
        code, out, err = self.cli("confirm", "app/001-add-login", paid)
        self.assertEqual((code, out, err), (0, f"[x] {free}\n[x] {paid}\n", ""))
        self.assertEqual(self.last()["body"], {"lines": [paid]})
        self.cli("confirm", "app/001-add-login", "--all")
        self.assertEqual(self.last()["body"], {"all": True})
        code, _, err = self.cli("confirm", "app/001-add-login", paid, "--all")
        self.assertEqual(code, 1)
        self.assertIn("not both", err)
        self.route("GET", "/api/jobs/app/001-add-login/assumptions", {"marked": [], "confirmed": [], "unconfirmed": []})
        code, out, err = self.cli("confirm", "app/001-add-login")
        self.assertEqual((code, out), (0, ""))
        self.assertIn("nothing to confirm", err)

    def write_lock(self, text: str) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "factory.lock").write_text(text)

    def test_commands_follow_a_server_started_with_another_port(self) -> None:
        configured = free_port()
        self.make_config(port=configured)  # `factory serve --port <api port>` overrides this one
        self.route("GET", "/api/state", {"repos": [], "jobs": [job()]})
        self.write_lock(f"{os.getpid()}\nhttp://127.0.0.1:{self.api.port}\n")
        code, out, err = self.cli("status")
        self.assertEqual(code, 0, err)
        self.assertIn("app/001-add-login", out)
        code, out, err = self.cli("url")
        self.assertEqual(out, f"http://127.0.0.1:{self.api.port}/login?token={TOKEN}\n")
        self.assertIn(f"ssh -N -L {self.api.port}:127.0.0.1:{self.api.port} ", err)

    def test_a_stale_or_odd_lock_file_falls_back_to_the_config(self) -> None:
        configured = free_port()
        self.make_config(port=configured)
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait(30)
        for text in (f"{gone.pid}\nhttp://127.0.0.1:{self.api.port}\n",   # its server was killed
                     f"{os.getpid()}\n",                                     # `factory run` holds it
                     f"{os.getpid()}\nhttp://evil.example/x\n", f"{'9' * 40}\nhttp://127.0.0.1:1\n",
                     "garbage", ""):
            with self.subTest(lock=text):
                self.write_lock(text)
                self.assertEqual(cli.server_url(Config.load()), f"http://127.0.0.1:{configured}")
                code, _, err = self.cli("status")
                self.assertEqual(code, 1)
                self.assertIn("server not running", err)

    def test_server_errors_are_reported(self) -> None:
        self.route("POST", "/api/jobs/app/001-add-login/approve", {"error": "job is merged"}, status=409)
        code, out, err = self.cli("approve", "app/001-add-login")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("factory: job is merged (HTTP 409)", err)
        self.route("GET", "/api/state", {"error": "unauthorized"}, status=401)
        code, _, err = self.cli("status")
        self.assertEqual(code, 1)
        self.assertIn("refused the token", err)

    def test_backlog(self) -> None:
        items = [{"text": "Add search", "done": True, "job": "004-add-search"}, {"text": "Docs", "done": False}]
        self.route("GET", "/api/repos/app/backlog", {"items": items})
        self.route("POST", "/api/repos/app/backlog", {"items": items})
        self.route("POST", "/api/repos/app/next", {"job": job("005-docs", feature="Docs")})
        code, out, _ = self.cli("backlog", "app")
        self.assertEqual((code, out), (0, "[x] Add search → 004-add-search\n[ ] Docs\n"))
        self.cli("backlog", "app", "add", "Docs")
        self.assertEqual(self.last()["body"], {"text": "Docs"})
        code, out, _ = self.cli("backlog", "app", "next")
        self.assertEqual((code, out), (0, "app/005-docs\n"))
        self.route("POST", "/api/repos/app/next", {"error": "the backlog has no unchecked items"}, status=409)
        code, _, err = self.cli("backlog", "app", "next")
        self.assertEqual(code, 1)
        self.assertIn("no unchecked items", err)

    def test_plans(self) -> None:
        plan = {"id": "P001-billing", "repo": "app", "stage": "planning", "request": "Billing\nwith Stripe",
                "jobs": [{"n": 1, "stage": "merged"}, {"n": 2, "stage": "build"}], "reason": None}
        self.route("POST", "/api/plans", {"plan": plan}, status=201)
        self.route("GET", "/api/plans", {"plans": [plan]})
        self.route("POST", "/api/plans/app/P001-billing/run", {"plan": {**plan, "stage": "running"}})
        code, out, err = self.cli("plan", "app", "Billing with Stripe")
        self.assertEqual((code, out), (0, "app/P001-billing\n"))
        self.assertEqual(self.last()["body"], {"repo": "app", "request": "Billing with Stripe"})
        self.assertIn(str(self.data_dir / "plans" / "app" / "P001-billing" / "plan.md"), err)
        self.assertIn("factory plans run app/P001-billing", err)
        code, out, _ = self.cli("plans")
        self.assertRegex(out, r"^app/P001-billing\s+planning\s+1/2 jobs\s+Billing$")
        self.assertEqual(json.loads(self.cli("plans", "--json")[1]), [plan])
        code, out, _ = self.cli("plans", "run", "app/P001-billing")
        self.assertEqual(code, 0)
        self.assertIn("running", out)

    def test_repo_add_and_remove_go_through_a_running_server(self) -> None:
        repo = make_repo(self.tmp, "web")
        self.route("GET", "/api/state", {"repos": [], "jobs": []})
        self.route("POST", "/api/repos", {"repo": {"name": "web", "path": str(repo)}})
        code, out, _ = self.cli("repo", "add", str(repo), "--name", "web")
        self.assertEqual((code, out), (0, f"web  {repo}\n"))
        self.assertEqual(self.last()["body"], {"path": str(repo.resolve()), "name": "web"})
        self.assertEqual(Config.load(self.config_path).repos(), [])  # the server owns the write

        repos = [{"name": "web", "path": str(repo)}, {"name": "api", "path": "/srv/api"}]
        self.route("GET", "/api/config", {"repos": repos})
        self.route("POST", "/api/config", {"repos": repos[1:]})
        self.assertEqual(self.cli("repo", "remove", "web")[:2], (0, "web\n"))
        self.assertEqual(self.last()["body"], {"repos": [repos[1]]})
        code, _, err = self.cli("repo", "remove", "ghost")
        self.assertEqual(code, 1)
        self.assertIn("unknown repo 'ghost'", err)

    def write_job(self, stage: str, events: list[dict]) -> Path:
        job_dir = self.data_dir / "jobs" / "app" / "001-add-login"
        (job_dir / "logs").mkdir(parents=True)
        (job_dir / "job.json").write_text(json.dumps(job(stage=stage)))
        (job_dir / "logs" / "01-builder.jsonl").write_text("".join(json.dumps(ev) + "\n" for ev in events))
        return job_dir

    def test_logs_follow_skips_what_the_files_had_and_stops_at_merged(self) -> None:
        history = [event(1, "agent.start", {"run": "01-builder", "driver": "builtin", "model": "opus"}, "builder")]
        self.write_job("build", history)
        self.api.stream(
            history[0],
            event(5, "agent.text", {"run": "01-other", "text": "not this job"}, "builder", job_id="002-other"),
            event(6, "agent.text", {"run": "01-builder", "text": "Writing the "}, "builder"),
            event(7, "agent.text", {"run": "01-builder", "text": "code now."}, "builder"),
            event(8, "agent.tool_call", {"run": "01-builder", "name": "Bash", "input": {"command": "make"}},
                  "builder"),
            event(9, "job.updated", {"job": job(stage="merged", merged_commit="abcdef1234567")}),
            event(10, "agent.text", {"run": "01-builder", "text": "after the end"}, "builder"),
        )
        code, out, err = self.cli("logs", "app/001-add-login", "-f")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.count("started (builtin, opus)"), 1)
        self.assertNotIn("not this job", out)
        self.assertIn("builder            Writing the code now.\n", out)
        self.assertIn('> Bash {"command": "make"}', out)
        self.assertRegex(out, r"merged\s+abcdef123456\n$")
        self.assertNotIn("after the end", out)
        self.assertTrue(self.last()["path"].startswith("/api/events?since=0"))

    def test_logs_follow_does_not_replay_the_stage_history(self) -> None:
        t0, t1, t2 = "2026-10-20T22:30:00Z", "2026-10-20T22:30:05Z", "2026-10-20T22:31:00Z"
        history = [event(3, "log", {"level": "info", "message": "stage spec: creating the worktree"}, ts=t0),
                   event(4, "agent.start", {"run": "01-spec-writer", "driver": "builtin", "model": "opus"},
                         "spec-writer", ts=t1),
                   event(6, "agent.end", {"run": "01-spec-writer", "ok": True, "turns": 2}, "spec-writer", ts=t1)]
        self.write_job("spec", history)
        reviewing = job(stage="review", round=1, detail="round 1", reviews={"security": "PASS", "ux": "running"})
        self.api.stream(
            event(1, "job.created", {"job": job()}, ts=t0),
            event(2, "job.updated", {"job": job(stage="spec", detail="creating the worktree")}, ts=t0),
            *history[:2],
            event(5, "job.updated", {"job": job(stage="spec", detail="writing the spec")}, ts=t1),  # before 6
            history[2],
            event(7, "job.updated", {"job": reviewing}, ts=t2),
            event(8, "job.updated", {"job": {**reviewing, "reviews": {"security": "PASS", "ux": "PASS"}}}, ts=t2),
            event(9, "job.updated", {"job": job(stage="merged", round=1, merged_commit="abcdef1234567")}, ts=t2),
        )
        code, out, err = self.cli("logs", "app/001-add-login", "-f")
        self.assertEqual(code, 0, err)
        body = [line[10:].rstrip() for line in out.splitlines()]
        self.assertEqual(body, ["log                info: stage spec: creating the worktree",
                                "spec-writer        started (builtin, opus)",
                                "spec-writer        finished (2 turns)",
                                "review             round 1",
                                "review             security: PASS",
                                "review             ux: PASS",
                                "merged             abcdef123456"])

    def test_logs_follow_reports_a_closed_stream(self) -> None:
        self.write_job("review", [])
        self.api.stream(event(3, "log", {"level": "info", "message": "round 1"}))
        code, out, err = self.cli("logs", "app/001-add-login", "-f")
        self.assertEqual(code, 1)
        self.assertIn("info: round 1", out)
        self.assertIn("closed the event stream", err)


class NoServerTests(CliCase):
    def test_server_commands_say_how_to_start_it(self) -> None:
        self.make_config()
        for argv in (["status"], ["submit", "app", "x"], ["approve", "app/001-x"], ["backlog", "app"],
                     ["plan", "app", "x"], ["plans"], ["logs", "app/001-x", "-f"]):
            with self.subTest(argv=argv):
                if argv[0] == "logs":
                    (self.data_dir / "jobs" / "app" / "001-x").mkdir(parents=True, exist_ok=True)
                code, out, err = self.cli(*argv)
                self.assertEqual(code, 1)
                self.assertIn("factory: server not running — start it with: factory serve", err)

    def test_server_commands_need_a_token(self) -> None:
        code, _, err = self.cli("status")
        self.assertEqual(code, 1)
        self.assertIn("run factory init first", err)


# -- logs from files ---------------------------------------------------------------------


class LogFileTests(CliCase):
    def test_transcript_from_the_log_files(self) -> None:
        self.make_config()
        logs = self.data_dir / "jobs" / "app" / "001-add-login" / "logs"
        logs.mkdir(parents=True)
        (logs.parent / "job.json").write_text(json.dumps(job(stage="merged")))
        builder = [
            event(3, "agent.start", {"run": "02-builder", "driver": "builtin", "model": "opus", "round": 1},
                  "builder", ts="2026-10-20T22:31:00Z"),
            event(4, "agent.reasoning", {"run": "02-builder", "text": "Plan first."}, "builder",
                  ts="2026-10-20T22:31:01Z"),
            event(5, "agent.text", {"run": "02-builder", "text": "Line one\nline "}, "builder",
                  ts="2026-10-20T22:31:02Z"),
            event(6, "agent.text", {"run": "02-builder", "text": "two\n"}, "builder", ts="2026-10-20T22:31:02Z"),
            event(7, "agent.tool_result", {"run": "02-builder", "name": "Bash", "ok": False,
                                           "output": "boom\nmore"}, "builder", ts="2026-10-20T22:31:03Z"),
            event(8, "agent.usage", {"run": "02-builder", "input_tokens": 5}, "builder", ts="2026-10-20T22:31:03Z"),
            event(9, "agent.end", {"run": "02-builder", "ok": True, "turns": 4,
                                   "usage": {"input_tokens": 12345, "output_tokens": 678}}, "builder",
                  ts="2026-10-20T22:31:04Z"),
        ]
        spec = [event(1, "agent.start", {"run": "01-spec-writer", "driver": "cli", "model": "sonnet"},
                      "spec-writer", ts="2026-10-20T22:30:00Z"),
                event(2, "agent.end", {"run": "01-spec-writer", "ok": False, "error": "timeout"}, "spec-writer",
                      ts="2026-10-20T22:30:30Z")]
        (logs / "02-builder.jsonl").write_text("".join(json.dumps(ev) + "\n" for ev in builder) + "{broken\n")
        (logs / "01-spec-writer.jsonl").write_text("".join(json.dumps(ev) + "\n" for ev in spec))
        code, out, _ = self.cli("logs", "app/001-add-login")
        self.assertEqual(code, 0)
        body = [line[10:] for line in out.splitlines()]   # drop the local clock
        indent = " " * (len(cli.INDENT) - 10)
        self.assertEqual(body, [
            "spec-writer        started (cli, sonnet)",
            "spec-writer        failed: timeout",
            "builder            started (builtin, opus, round 1)",
            "builder            (thinking) Plan first.",
            "builder            Line one",
            f"{indent}line two",
            "builder            < Bash error boom",
            "builder            finished (4 turns, 12.3k in / 678 out)",
        ])

    def test_follow_on_a_finished_job_needs_no_server(self) -> None:
        self.make_config()
        (self.data_dir / "jobs" / "app" / "001-add-login" / "logs").mkdir(parents=True)
        (self.data_dir / "jobs" / "app" / "001-add-login" / "job.json").write_text(json.dumps(job(stage="merged")))
        code, out, err = self.cli("logs", "app/001-add-login", "-f")
        self.assertEqual((code, out), (0, ""))
        self.assertIn("is merged; nothing more to follow", err)

    def test_unknown_job(self) -> None:
        self.make_config()
        code, _, err = self.cli("logs", "app/009-missing")
        self.assertEqual(code, 1)
        self.assertIn("no job app/009-missing", err)


class FormattingTests(unittest.TestCase):
    def test_parse_sse(self) -> None:
        stream = [b": ping\n", b"\n", b"id: 4\n", b"event: job.updated\n",
                  b'data: {"seq": 4, "type": "job.updated", "data": {}, "repo": "app"}\n', b"\n",
                  b"id: 5\n", b"event: log\n", b'data: {"level": "info",\n', b'data: "message": "hi"}\n', b"\n",
                  b"data: not json\n", b"\n"]
        events = list(cli.parse_sse(iter(stream)))
        self.assertEqual(events[0], {"seq": 4, "type": "job.updated", "data": {}, "repo": "app"})
        self.assertEqual((events[1]["seq"], events[1]["type"], events[1]["data"]),
                         (5, "log", {"level": "info", "message": "hi"}))
        self.assertEqual(len(events), 2)

    def test_run_mode_prints_only_transitions(self) -> None:
        out = io.StringIO()
        printer = cli.EventPrinter(out, transcript=False)
        for ev in (event(1, "job.created", {"job": job()}),
                   event(2, "job.updated", {"job": job(stage="review", round=1, detail="round 1",
                                                       reviews={"security": "running", "ux": "pending"})}),
                   event(3, "agent.text", {"text": "hidden"}, "reviewer-ux"),
                   event(4, "agent.tool_call", {"name": "Read", "input": {}}, "reviewer-ux"),
                   event(5, "log", {"level": "info", "message": "hidden too"}),
                   event(6, "log", {"level": "warn", "message": "reviewer edits stashed"}),
                   event(7, "job.updated", {"job": job(stage="review", round=1, detail="round 1",
                                                       reviews={"security": "PASS", "ux": "running"})}),
                   event(8, "job.updated", {"job": job(stage="review", round=1, detail="round 1",
                                                       reviews={"security": "PASS", "ux": "CHANGES"})}),
                   event(9, "job.updated", {"job": job(stage="build", round=1, detail="round 2: building")}),
                   event(10, "job.updated", {"job": job(stage="review", round=2, detail="round 2",
                                                        reviews={"security": "PASS", "ux": "PASS"})})):
            printer.handle(ev)
        printer.finish()
        body = [line[10:].rstrip() for line in out.getvalue().splitlines()]
        self.assertEqual(body, ["queued", "review             round 1",
                                "log                warn: reviewer edits stashed",
                                "review             security: PASS", "review             ux: CHANGES",
                                "build              round 2: building", "review             round 2",
                                "review             security: PASS", "review             ux: PASS"])

    def test_streams_of_two_runs_do_not_mix(self) -> None:
        out = io.StringIO()
        printer = cli.EventPrinter(out, transcript=True)
        printer.handle(event(1, "agent.text", {"run": "03-reviewer-ux", "text": "ux says"}, "reviewer-ux"))
        printer.handle(event(2, "agent.text", {"run": "04-reviewer-ui", "text": "ui says"}, "reviewer-ui"))
        printer.handle(event(3, "agent.text", {"run": "04-reviewer-ui", "text": " more"}, "reviewer-ui"))
        printer.finish()
        self.assertEqual([line[10:] for line in out.getvalue().splitlines()],
                         ["reviewer-ux        ux says", "reviewer-ui        ui says more"])

    def test_clock_and_lines(self) -> None:
        self.assertEqual(cli.clock("not a time"), "--:--:--")
        self.assertRegex(cli.clock("2026-10-20T22:30:00Z"), r"^\d\d:\d\d:\d\d$")
        self.assertEqual(cli.job_line(job(stage="failed", reason=None, error="boom")),
                         "app/001-add-login  failed       r0 0/2   boom")
        summary = cli.run_summary(job(stage="needs-human", reason="approver escalated", round=2, resumes=2))
        self.assertEqual(summary[0], "app/001-add-login: needs-human — approver escalated")
        self.assertEqual(summary[1], "  rounds 2, resumes 2/2, tokens 1.5k in / 20 out")
        self.assertIn("approve, rework or reject app/001-add-login", summary[3])
        self.assertIn("retry or reject", cli.run_summary(job(stage="failed"))[3])
        self.assertEqual(len(cli.run_summary(job(stage="merged"))), 3)


# -- serve and run: the lock and the in-process factory ------------------------------------


PER_RUN_SECRET = "per-run-preset-key-0123456789"


class FakeFactory:
    """Plays a scripted job through the bus like the orchestrator would."""

    script: list[dict] = []
    instances: list[FakeFactory] = []

    def __init__(self, config: Config, bus: EventBus) -> None:
        self.config, self.bus = config, bus
        self.started = self.stopped = self.jobs_started = 0
        self.submitted: tuple | None = None
        self.current: dict = {}
        self.polls = 0
        FakeFactory.instances.append(self)

    def start(self) -> None:
        self.started += 1

    def start_jobs(self) -> None:
        self.jobs_started += 1

    def secret_values(self) -> list[str]:
        """Like Factory.secret_values: the config's secrets plus keys only a per-run preset knows."""
        return [*self.config.secret_values(), PER_RUN_SECRET]

    def stop(self) -> None:
        self.stopped += 1

    def submit(self, repo: str, feature: str, **kwargs: Any) -> dict:
        self.submitted = (repo, feature, kwargs)
        self.current = job("001-add-x", repo, feature=feature)
        self.bus.publish("job.created", {"job": self.current}, repo=repo, job="001-add-x")
        threading.Thread(target=self.play, daemon=True).start()
        return dict(self.current)

    def play(self) -> None:
        for step in self.script:
            time.sleep(0.005)
            if "agent" in step:
                self.bus.publish(step["type"], step["data"], repo="app", job="001-add-x", agent=step["agent"])
                continue
            self.current = {**self.current, **step}
            if not step.get("silent"):
                self.bus.publish("job.updated", {"job": self.current}, repo="app", job="001-add-x")

    def job(self, repo: str, job_id: str) -> dict:
        self.polls += 1
        return dict(self.current)

    def cancel(self, repo: str, job_id: str) -> dict:
        self.current = {**self.current, "stage": "cancelled", "reason": "cancelled by a human"}
        self.bus.publish("job.updated", {"job": self.current}, repo=repo, job=job_id)
        return dict(self.current)


class LockAndRunTests(CliCase):
    def setUp(self) -> None:
        super().setUp()
        FakeFactory.instances = []
        FakeFactory.script = []
        self.lock_path = self.data_dir / "factory.lock"

    def config_with_repo(self) -> None:
        self.make_config(repos=[{"name": "app", "path": str(self.tmp / "app")}])

    def run_job(self, script: list[dict], *extra: str) -> tuple[int, str, str]:
        FakeFactory.script = script
        with mock.patch("swfactory.orchestrator.Factory", FakeFactory):
            return self.cli("run", "app", "Add X", *extra)

    def hold_lock_in_another_process(self) -> subprocess.Popen:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        holder = subprocess.Popen(
            [sys.executable, "-c", "import fcntl, os, sys; fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT); "
             "fcntl.flock(fd, fcntl.LOCK_EX); os.write(fd, b'4242\\n'); print('locked', flush=True); "
             "sys.stdin.read()", str(self.lock_path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.wait, 10)
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.stdin.close)
        self.assertEqual(holder.stdout.readline().strip(), "locked")
        return holder

    def assert_lock_free(self) -> None:
        fd = os.open(self.lock_path, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)

    def test_serve_and_run_refuse_while_another_factory_holds_the_lock(self) -> None:
        self.config_with_repo()
        self.hold_lock_in_another_process()
        for argv in (["serve"], ["run", "app", "Add X"]):
            with self.subTest(argv=argv), mock.patch("swfactory.orchestrator.Factory", FakeFactory):
                code, _, err = self.cli(*argv)
                self.assertEqual(code, 1)
                self.assertIn("another factory (pid 4242) is already running", err)
        self.assertEqual(FakeFactory.instances, [])

    def test_serve_holds_the_lock_and_starts_the_factory_and_server(self) -> None:
        self.config_with_repo()
        seen: dict = {}

        def run_server(config: Config, factory: Any, bus: EventBus, host: str | None = None,
                       port: int | None = None) -> None:
            seen.update(factory=factory, bus=bus, host=host, port=port, started=factory.started,
                        lock=self.lock_path.read_text().splitlines(), url=cli.server_url(Config.load()))
            fd = os.open(self.lock_path, os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)

        with mock.patch("swfactory.orchestrator.Factory", FakeFactory), \
                mock.patch("swfactory.server.run_server", run_server):
            code, _, err = self.cli("serve", "--port", "9999")
        self.assertEqual(code, 0, err)
        self.assertEqual((seen["host"], seen["port"], seen["started"]), (None, 9999, 1))
        # The lock records the pid and the --port override, so other CLI commands find the server.
        self.assertEqual(seen["lock"], [str(os.getpid()), "http://127.0.0.1:9999"])
        self.assertEqual(seen["url"], "http://127.0.0.1:9999")
        self.assertGreaterEqual(seen["factory"].stopped, 1)
        self.assertEqual(seen["bus"].publish("log", {"message": f"token {TOKEN}"}).data["message"], "token ***")
        self.assertIs(seen["factory"].bus, seen["bus"])
        self.assertEqual(seen["bus"].publish("log", {"message": PER_RUN_SECRET}).data["message"], "***",
                         "the bus asks the factory, which knows the per-run presets' keys")
        self.assert_lock_free()
        self.assertEqual(self.lock_path.read_text(), "")  # released: nothing stale for clients to read
        self.assertNotEqual(cli.server_url(Config.load()), "http://127.0.0.1:9999")

    def test_serve_reports_a_busy_port(self) -> None:
        self.config_with_repo()
        busy = OSError(errno.EADDRINUSE, "Address already in use")
        with mock.patch("swfactory.orchestrator.Factory", FakeFactory), \
                mock.patch("swfactory.server.run_server", side_effect=busy):
            code, _, err = self.cli("serve", "--port", "9998")
        self.assertEqual(code, 1)
        self.assertIn("port 9998 is already in use", err)
        self.assertEqual(FakeFactory.instances[0].stopped, 1)
        self.assert_lock_free()

    def test_serve_warns_about_a_public_bind_address(self) -> None:
        self.config_with_repo()
        with mock.patch("swfactory.orchestrator.Factory", FakeFactory), \
                mock.patch("swfactory.server.run_server"):
            _, _, err = self.cli("serve", "--host", "0.0.0.0")
        self.assertIn("listening on 0.0.0.0", err)

    def test_serve_needs_a_token(self) -> None:
        code, _, err = self.cli("serve")
        self.assertEqual(code, 1)
        self.assertIn("run factory init first", err)

    def test_run_to_merged(self) -> None:
        self.config_with_repo()
        script = [
            {"stage": "spec", "detail": "writing the spec"},
            {"agent": "spec-writer", "type": "agent.start", "data": {"driver": "builtin", "model": "opus"}},
            {"agent": "spec-writer", "type": "agent.text", "data": {"text": "not shown in run"}},
            {"agent": "spec-writer", "type": "agent.end", "data": {"ok": True, "turns": 3}},
            {"stage": "review", "round": 1, "detail": "round 1", "reviews": {"security": "running"}},
            {"reviews": {"security": "PASS"}},
            {"stage": "merged", "merged_commit": "abc123def4567890"},
        ]
        code, out, err = self.run_job(script)
        self.assertEqual(code, 0, err)
        factory = FakeFactory.instances[0]
        self.assertEqual(factory.submitted, ("app", "Add X", {}))
        # Recovery and workers only: Factory.start() would also resume plans left on disk.
        self.assertEqual((factory.jobs_started, factory.started, factory.stopped), (1, 0, 1))
        body = [line[10:].rstrip() for line in out.splitlines()]
        self.assertEqual(body[:6], ["queued", "spec               writing the spec",
                                    "spec-writer        started (builtin, opus)",
                                    "spec-writer        finished (3 turns)", "review             round 1",
                                    "review             security: PASS"])
        self.assertIn("merged             abc123def456", body)
        self.assertIn("app/001-add-x: merged — abc123def456", out)
        self.assertNotIn("not shown in run", out)
        self.assertEqual(factory.bus.publish("log", {"message": PER_RUN_SECRET}).data["message"], "***")
        self.assert_lock_free()

    def test_run_exit_codes(self) -> None:
        self.config_with_repo()
        code, out, _ = self.run_job([{"stage": "delivered", "delivered": {"mode": "branch",
                                                                         "branch": "factory/001-add-x"}}])
        self.assertEqual(code, 0, "delivered is a success")
        self.assertIn("app/001-add-x: delivered — factory/001-add-x", out)
        self.assertIn("approve (merges it) or reject", out)
        for stage, expected in (("needs-human", 2), ("failed", 1), ("rejected", 1), ("cancelled", 1)):
            with self.subTest(stage=stage):
                code, out, _ = self.run_job([{"stage": stage, "reason": f"{stage} because"}])
                self.assertEqual(code, expected)
                self.assertIn(f"app/001-add-x: {stage} — {stage} because", out)

    def test_run_passes_the_preset_and_reads_stdin(self) -> None:
        self.config_with_repo()
        FakeFactory.script = [{"stage": "merged"}]
        with mock.patch("swfactory.orchestrator.Factory", FakeFactory):
            code, _, _ = self.cli("run", "app", "-", "--preset", "hybrid", stdin="Feature text")
        self.assertEqual(code, 0)
        self.assertEqual(FakeFactory.instances[0].submitted, ("app", "Feature text", {"routing": "hybrid"}))
        with mock.patch("swfactory.orchestrator.Factory", FakeFactory):
            code, out, _ = self.cli("run", "app", "Branch it", "--delivery", "branch")
        self.assertEqual(code, 0)
        self.assertEqual(FakeFactory.instances[-1].submitted, ("app", "Branch it", {"delivery": "branch"}))

    def test_run_reports_an_invalid_preset(self) -> None:
        self.config_with_repo()
        with mock.patch.object(FakeFactory, "submit", side_effect=ValueError("unknown preset 'nope'")):
            code, out, err = self.run_job([], "--preset", "nope")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("factory: unknown preset 'nope'", err)
        self.assertEqual(FakeFactory.instances[0].stopped, 1)
        self.assert_lock_free()

    def test_run_polls_when_events_were_missed(self) -> None:
        self.config_with_repo()
        with mock.patch.object(cli, "POLL_SECONDS", 0.05):
            code, out, _ = self.run_job([{"stage": "merged", "merged_commit": "f00d", "silent": True}])
        self.assertEqual(code, 0)
        self.assertIn("merged             f00d", out)

    def test_ctrl_c_cancels_the_job(self) -> None:
        self.config_with_repo()
        interrupted = []

        def job_then_interrupt(factory: FakeFactory, repo: str, job_id: str) -> dict:
            if not interrupted:
                interrupted.append(True)
                raise KeyboardInterrupt
            return dict(factory.current)

        with mock.patch.object(cli, "POLL_SECONDS", 0.05), \
                mock.patch.object(FakeFactory, "job", job_then_interrupt):
            code, out, err = self.run_job([])
        self.assertEqual(code, 1)
        self.assertIn("cancelling the job", err)
        self.assertIn("app/001-add-x: cancelled", out)

    def test_run_rejects_an_unknown_repo_before_starting(self) -> None:
        self.config_with_repo()
        self.assertEqual(self.run_job([{"stage": "merged"}])[0], 0)
        code, _, err = self.cli("run", "ghost", "x")
        self.assertEqual(code, 1)
        self.assertIn("unknown repo 'ghost' (configured: app)", err)
        self.assertEqual(len(FakeFactory.instances), 1)

    def config_without_keys(self) -> Path:
        """A real repo and a config whose only provider has no key, so every agent run fails at once."""
        repo = make_repo(self.tmp, "app")
        provider = {"type": "openai", "base_url": "http://127.0.0.1:9/v1", "api_key_env": "SF_TEST_NO_SUCH_KEY"}
        model = {"provider": "nokey", "model": "m", "max_tokens": 1024, "stream": False, "extra_body": {}}
        self.make_config(repos=[{"name": "app", "path": str(repo)}], providers={"nokey": provider},
                         models={"opus": model, "sonnet": model}, agents={"*": {"driver": "builtin", "model": "opus"}})
        os.environ.pop("SF_TEST_NO_SUCH_KEY", None)
        return repo

    def test_run_leaves_plans_on_disk_alone(self) -> None:
        repo = self.config_without_keys()
        store = JobStore(self.data_dir)
        first = store.new_job("app", "First plan job", ["security"], 2)
        first.update(stage="merged", merged_commit="0" * 40)
        store.save(first)
        plan_dir = self.data_dir / "plans" / "app" / "P001-two-jobs"
        plan_dir.mkdir(parents=True)
        base_commit = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True,
                                     text=True, check=True).stdout.strip()
        plan = {"id": "P001-two-jobs", "repo": "app", "request": "Two jobs", "stage": "running", "reason": None,
                "failed_step": None, "base": "main", "base_commit": base_commit, "plan_dir": str(plan_dir),
                "jobs": [{"n": 1, "feature": "First plan job", "after": [], "job_id": first["id"], "stage": "merged"},
                         {"n": 2, "feature": "Second plan job", "after": [1], "job_id": None, "stage": None}],
                "reviews": [], "routing": None, "usage": {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0,
                                                          "calls": 0},
                "created_at": "2026-10-20T22:30:00Z", "updated_at": "2026-10-20T22:30:00Z"}
        (plan_dir / "plan.json").write_text(json.dumps(plan))
        before = (plan_dir / "plan.json").read_bytes()
        code, out, err = self.cli("run", "app", "Add a hello page")
        self.assertEqual(code, 1, err)
        self.assertIn("app/002-add-hello-page: failed", out)
        # The one-shot run drove only its own job: the plan's next job is not submitted.
        self.assertEqual([j["id"] for j in store.list("app")], [first["id"], "002-add-hello-page"])
        self.assertEqual((plan_dir / "plan.json").read_bytes(), before)
        self.assert_lock_free()

    def test_run_with_the_real_factory_stops_on_failure(self) -> None:
        self.config_without_keys()
        code, out, err = self.cli("run", "app", "Add a hello page")
        self.assertEqual(code, 1, err)
        self.assertIn("spec-writer        started", out)
        self.assertIn("app/001-add-hello-page: failed — spec-writer failed", out)
        self.assert_lock_free()


# -- bin/factory and install.sh ---------------------------------------------------------


class ScriptCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sf-scripts-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.fakebin = self.tmp / "fakebin"
        self.fakebin.mkdir()
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith("FACTORY_") and key not in ("PYTHONPATH",)}
        self.env.update(HOME=str(self.home), PATH=f"{self.fakebin}{os.pathsep}{os.environ.get('PATH', '')}")

    def fake_command(self, name: str, script: str) -> Path:
        path = self.fakebin / name
        path.write_text("#!/bin/sh\n" + script)
        path.chmod(0o755)
        return path

    def run_script(self, argv: list[str], cwd: Path | None = None, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run([str(arg) for arg in argv], cwd=cwd or self.tmp, env={**self.env, **env},
                              capture_output=True, text=True, timeout=60)


class LauncherTests(ScriptCase):
    def test_help_through_the_launcher(self) -> None:
        result = self.run_script([LAUNCHER, "--help"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: factory", result.stdout)

    def test_a_symlink_finds_the_checkout(self) -> None:
        link = self.tmp / "bin" / "factory"
        link.parent.mkdir()
        link.symlink_to(LAUNCHER)
        result = self.run_script([link, "--version"], cwd=self.home)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, f"factory {__version__}"))

    def test_factory_python_picks_the_interpreter(self) -> None:
        for name in ("python3.10", "python3.11"):
            interpreter = shutil.which(name)
            if interpreter is None:
                continue
            with self.subTest(python=name):
                marker = self.tmp / f"{name}.txt"
                wrapper = self.fake_command(f"py-{name}", f'echo used > "{marker}"\nexec {interpreter} "$@"\n')
                result = self.run_script([LAUNCHER, "--version"], FACTORY_PYTHON=str(wrapper))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(marker.exists())

    def test_an_old_python_gets_one_clear_line(self) -> None:
        old = shutil.which("python3.8") or shutil.which("python3.9")
        if old is None:
            self.skipTest("no python3.8 or python3.9 here")
        result = self.run_script([LAUNCHER, "--version"], FACTORY_PYTHON=old)
        self.assertEqual(result.returncode, 1)
        self.assertIn("needs Python 3.10 or newer; this is Python 3.", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_the_launcher_does_not_put_the_checkout_in_the_environment(self) -> None:
        # Agent shells, setup commands and CLI drivers inherit the factory's environment; with the
        # checkout in PYTHONPATH, a user repo's `import tests.helpers` would load the factory's.
        marker = self.tmp / "pythonpath.txt"
        wrapper = self.fake_command("py-env", f'printf "%s" "${{PYTHONPATH-<unset>}}" > "{marker}"\n'
                                              f'exec "{sys.executable}" "$@"\n')
        for given, expected in ((None, "<unset>"), ("/opt/mine", "/opt/mine"), ("", "")):
            with self.subTest(pythonpath=given):
                env = {"FACTORY_PYTHON": str(wrapper)}
                if given is not None:
                    env["PYTHONPATH"] = given
                result = self.run_script([LAUNCHER, "--version"], cwd=self.home, **env)
                self.assertEqual((result.returncode, result.stdout.strip()), (0, f"factory {__version__}"),
                                 result.stderr)
                self.assertEqual(marker.read_text(), expected)

    def test_a_user_repo_keeps_its_own_tests_package(self) -> None:
        repo = self.tmp / "user-repo"
        (repo / "tests").mkdir(parents=True)
        (repo / "tests" / "helpers.py").write_text("WHO = 'user'\n")  # a namespace package, as with pytest
        probe = self.tmp / "probe.py"
        probe.write_text(
            "import os, subprocess, sys\n"
            "from swfactory.__main__ import main  # the launcher's module, as the factory runs it\n"
            "print(subprocess.run([sys.executable, '-c', 'import tests.helpers as h; print(h.WHO)'],\n"
            "                     cwd=sys.argv[1], capture_output=True, text=True).stdout.strip())\n")
        # Run the probe the way the launcher runs the factory (same environment, same sys.path setup).
        entry = "from swfactory.__main__ import main\nsys.exit(main())"
        self.assertIn(entry, LAUNCHER.read_text())
        launcher = LAUNCHER.read_text().replace(entry, 'import runpy\nsys.argv.pop(0)\n'
                                                       'runpy.run_path(sys.argv[0], run_name="__main__")')
        fake = self.tmp / "checkout" / "bin" / "factory"
        fake.parent.mkdir(parents=True)
        fake.write_text(launcher)
        fake.chmod(0o755)
        (self.tmp / "checkout" / "swfactory").symlink_to(ROOT / "swfactory")
        (self.tmp / "checkout" / "tests").symlink_to(ROOT / "tests")
        result = self.run_script([fake, probe, repo], cwd=repo)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "user"), result.stderr)

    def test_files_in_the_working_directory_cannot_shadow_modules(self) -> None:
        workdir = self.tmp / "checkout"
        workdir.mkdir()
        marker = self.tmp / "shadowed"
        for module in ("argparse", "json", "urllib"):
            target = workdir / module / "__init__.py" if module == "urllib" else workdir / f"{module}.py"
            target.parent.mkdir(exist_ok=True)
            target.write_text(f"open({str(marker)!r}, 'w').close()\n")
        result = self.run_script([LAUNCHER, "--version"], cwd=workdir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists())


class InstallTests(ScriptCase):
    def setUp(self) -> None:
        super().setUp()
        self.config = self.home / ".config" / "software-factory" / "config.json"
        self.link = self.home / ".local" / "bin" / "factory"
        self.unit = self.home / ".config" / "systemd" / "user" / "factory.service"
        self.systemctl_log = self.tmp / "systemctl.log"
        self.fake_command("systemctl", f'echo "$*" >> "{self.systemctl_log}"\n')

    def install(self, *argv: str, **env: str) -> subprocess.CompletedProcess:
        return self.run_script(["bash", INSTALL, *argv], **env)

    def existing_config(self) -> bytes:
        init_config(self.config)
        return self.config.read_bytes()

    def test_help_and_bad_options(self) -> None:
        result = self.install("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("Usage: ./install.sh", result.stdout)
        self.assertIn("--systemd-user", result.stdout)
        self.assertEqual(self.install("--bogus").returncode, 2)
        result = self.install("--start")
        self.assertEqual(result.returncode, 1)
        self.assertIn("--start needs --systemd-user", result.stderr)

    def test_install_into_a_home_is_idempotent(self) -> None:
        before = self.existing_config()
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                result = self.install()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(Path(os.readlink(self.link)), LAUNCHER)
                self.assertEqual(self.config.read_bytes(), before)
                env_file = self.config.parent / "env"
                self.assertEqual(stat.S_IMODE(env_file.stat().st_mode), 0o600)
                self.assertIn("is not on your PATH", result.stderr)
                self.assertIn("factory url", result.stdout)
                self.assertIn("ssh -N -L 8765:127.0.0.1:8765 ", result.stdout)
                env_file.write_text("DEEPSEEK_API_KEY=kept\n")
        self.assertEqual((self.config.parent / "env").read_text(), "DEEPSEEK_API_KEY=kept\n")
        self.assertFalse(self.unit.exists())
        self.assertFalse(self.systemctl_log.exists())

    def test_the_link_runs_the_factory(self) -> None:
        self.existing_config()
        self.assertEqual(self.install().returncode, 0)
        result = self.run_script([self.link, "url"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r"^http://127\.0\.0\.1:8765/login\?token=[0-9a-f]{32}\n$")

    def test_fresh_install_runs_factory_init_with_the_preset(self) -> None:
        if not (ROOT / "examples" / "deepseek.json").is_file():
            self.skipTest("examples/deepseek.json is not there")
        data = self.tmp / "factory-data"
        result = self.install("--preset", "deepseek", "--data-dir", str(data))
        self.assertEqual(result.returncode, 0, result.stderr)
        raw = Config.load(self.config).raw
        self.assertRegex(raw["server"]["token"], r"^[0-9a-f]{32}$")
        self.assertEqual(raw["data_dir"], str(data))
        result = self.install("--data-dir", str(self.tmp / "other"))
        self.assertIn("--data-dir ignored", result.stderr)
        self.assertEqual(Config.load(self.config).raw["data_dir"], str(data))

    def test_a_config_without_a_token_gets_one(self) -> None:
        # `factory repo add` before installing writes a config with repos and no token.
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps({"repos": [{"name": "early", "path": "/srv/early"}],
                                           "models": {"opus": {"model": "my-own-model"}}}))
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("adding the server token", result.stdout)
        raw = Config.load(self.config).raw
        self.assertRegex(raw["server"]["token"], r"^[0-9a-f]{32}$")
        self.assertEqual(raw["repos"], [{"name": "early", "path": "/srv/early"}])
        self.assertEqual(raw["models"]["opus"]["model"], "my-own-model")  # no preset unless asked
        before = self.config.read_bytes()
        result = self.install("--preset", "hybrid")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--preset ignored", result.stderr)
        self.assertEqual(self.config.read_bytes(), before)

    def test_an_invalid_config_is_kept_with_a_warning(self) -> None:
        self.config.parent.mkdir(parents=True)
        self.config.write_text("{bad")
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("is not a valid config", result.stderr)
        self.assertIn("factory init --force", result.stderr)
        self.assertEqual(self.config.read_text(), "{bad")

    def test_the_unit_survives_odd_characters_in_the_checkout_path(self) -> None:
        checkout = self.tmp / "a&b%c$d" / "sf"
        for name in ("install.sh", "bin/factory", "systemd/factory.service"):
            (checkout / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, checkout / name)
        self.existing_config()
        result = self.run_script(["bash", checkout / "install.sh", "--systemd-user"])
        self.assertEqual(result.returncode, 0, result.stderr)
        escaped = str(checkout / "bin" / "factory").replace("%", "%%").replace("$", "$$")
        self.assertIn(f'ExecStart="{escaped}" serve\n', self.unit.read_text())

    def test_systemd_user_unit_and_uninstall(self) -> None:
        self.existing_config()
        result = self.install("--systemd-user", "--start", FACTORY_PYTHON=sys.executable)
        self.assertEqual(result.returncode, 0, result.stderr)
        unit = self.unit.read_text()
        self.assertIn(f'ExecStart="{LAUNCHER}" serve\n', unit)
        self.assertIn("Environment=PYTHONUNBUFFERED=1\n", unit)
        self.assertIn("EnvironmentFile=-%h/.config/software-factory/env\n", unit)
        self.assertIn(f'Environment="PATH={self.env["PATH"]}"\n', unit)
        self.assertIn(f'Environment="FACTORY_PYTHON={sys.executable}"\n', unit)
        self.assertNotIn("@", unit.replace("@FACTORY", "").split("[Unit]")[1])
        self.assertIn("WantedBy=default.target", unit)
        self.assertEqual(self.systemctl_log.read_text().splitlines(),
                         ["--user daemon-reload", "--user enable --now factory.service"])
        self.assertIn("sudo loginctl enable-linger", result.stdout)

        result = self.install("--uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.unit.exists())
        self.assertFalse(self.link.exists() or self.link.is_symlink())
        self.assertTrue(self.config.exists())
        self.assertIn("--user disable --now factory.service", self.systemctl_log.read_text())

    def test_credentials_become_load_credential_lines(self) -> None:
        self.existing_config()
        result = self.install("--systemd-user", "--credential", "register-token:/etc/credstore/register.token",
                              "--credential=checker-token:/etc/credstore/checker.token")
        self.assertEqual(result.returncode, 0, result.stderr)
        unit = self.unit.read_text()
        self.assertIn("\nLoadCredential=register-token:/etc/credstore/register.token\n"
                      "LoadCredential=checker-token:/etc/credstore/checker.token\n", unit)
        self.assertLess(unit.index("\nLoadCredential="), unit.index("[Install]"), "in [Service]")
        self.assertGreater(unit.index("\nLoadCredential="), unit.index("[Service]"))
        self.assertNotIn("@CREDENTIALS@", unit)
        for bad, words in [("register-token", "NAME must be"), ("../x:/etc/x", "NAME must be"),
                           ("a/b:/etc/x", "NAME must be"), ("token:relative/path", "absolute path"),
                           ("token:/etc/with space", "must not hold"), ("token:/etc/50%", "must not hold"),
                           ('token:/etc/"q"', "must not hold")]:
            with self.subTest(bad=bad):
                result = self.install("--systemd-user", "--credential", bad)
                self.assertEqual(result.returncode, 1)
                self.assertIn(words, result.stderr)
        result = self.install("--credential", "register-token:/etc/credstore/register.token")
        self.assertEqual(result.returncode, 1)
        self.assertIn("--credential needs --systemd-user", result.stderr)
        result = self.install("--drop-credential", "register-token")
        self.assertEqual(result.returncode, 1)
        self.assertIn("--drop-credential needs --systemd-user", result.stderr)

    def credential_lines(self) -> list[str]:
        return [line for line in self.unit.read_text().splitlines() if line.startswith("LoadCredential")]

    def test_running_it_again_keeps_the_credential_lines(self) -> None:
        """An update, or the doctor's advice for a second credential, never drops a credential line the unit
        has: a NAME given again gets its new PATH, and --drop-credential NAME removes one."""
        self.existing_config()
        result = self.install("--systemd-user", "--credential", "register-token:/etc/credstore/register.token")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("with LoadCredential=register-token:/etc/credstore/register.token", result.stdout)
        self.assertEqual(self.install("--systemd-user").returncode, 0)
        self.assertEqual(self.credential_lines(), ["LoadCredential=register-token:/etc/credstore/register.token"],
                         "a plain re-run keeps it")
        self.assertEqual(self.install("--systemd-user", "--credential",
                                      "checker-token:/etc/credstore/checker.token").returncode, 0)
        self.assertEqual(self.credential_lines(), ["LoadCredential=register-token:/etc/credstore/register.token",
                                                   "LoadCredential=checker-token:/etc/credstore/checker.token"])
        self.assertEqual(self.install("--systemd-user", "--credential",
                                      "register-token:/etc/credstore/register-2.token").returncode, 0)
        self.assertEqual(self.credential_lines(), ["LoadCredential=checker-token:/etc/credstore/checker.token",
                                                   "LoadCredential=register-token:/etc/credstore/register-2.token"])
        with self.unit.open("a") as handle:   # a line the owner added by hand is kept as it is
            handle.write("LoadCredentialEncrypted=sealed.token:/etc/credstore.encrypted/sealed.token\n")
        result = self.install("--systemd-user", "--drop-credential", "checker-token", "--drop-credential=absent")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--drop-credential absent: the unit has no credential line absent", result.stderr)
        unit = self.unit.read_text()
        self.assertEqual(self.credential_lines(), [
            "LoadCredential=register-token:/etc/credstore/register-2.token",
            "LoadCredentialEncrypted=sealed.token:/etc/credstore.encrypted/sealed.token"])
        self.assertLess(unit.index("\nLoadCredential="), unit.index("[Install]"), "in [Service]")
        self.assertGreater(unit.index("\nLoadCredential="), unit.index("[Service]"))
        self.assertEqual(unit.count("\nLoadCredentialEncrypted="), 1, "written once, in place of @CREDENTIALS@")

    def test_a_unit_without_start_is_only_loaded(self) -> None:
        self.existing_config()
        result = self.install("--systemd-user")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.systemctl_log.read_text().splitlines(), ["--user daemon-reload"])
        self.assertIn("systemctl --user enable --now factory.service", result.stdout)

    def test_foreign_files_are_left_alone(self) -> None:
        self.existing_config()
        self.link.parent.mkdir(parents=True)
        self.link.write_text("mine")
        result = self.install()
        self.assertEqual(result.returncode, 1)
        self.assertIn("is not a symlink", result.stderr)
        self.link.unlink()
        self.link.symlink_to("/bin/true")
        result = self.install("--uninstall")
        self.assertEqual(result.returncode, 0)
        self.assertIn("left alone", result.stderr)
        self.assertTrue(self.link.is_symlink())

    def test_requirements(self) -> None:
        self.existing_config()
        old_python = self.fake_command("old-python", 'if [ "$1" = "-V" ]; then echo "Python 3.8.10"; fi\nexit 1\n')
        result = self.install(FACTORY_PYTHON=str(old_python))
        self.assertEqual(result.returncode, 1)
        self.assertIn("Python 3.10 or newer is required", result.stderr)
        self.assertIn("Python 3.8.10", result.stderr)
        self.fake_command("git", 'echo "git version 2.34.1"\n')
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("git 2.34 is older than 2.38", result.stderr)

    @unittest.skipUnless(shutil.which("shellcheck"), "shellcheck is not installed")
    def test_shellcheck(self) -> None:
        for argv in (["shellcheck", INSTALL], ["shellcheck", "-s", "sh", LAUNCHER]):
            with self.subTest(script=argv[-1].name):
                result = subprocess.run([str(arg) for arg in argv], capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout)


if __name__ == "__main__":
    unittest.main()


class MessagesCliTests(CliCase):
    """factory notify, factory wake and factory secret set (DESIGN §34)."""

    def test_secret_set_reads_stdin_and_never_echoes(self) -> None:
        self.make_config()
        code, out, err = self.cli("secret", "set", "SERAPHINA_TELEGRAM_BOT_TOKEN", "--stdin",
                                  stdin="7123:AAH-secret-value-0123456789\n")
        self.assertEqual(code, 0, err)
        self.assertNotIn("secret-value", out + err)
        env = (self.config_path.parent / "env").read_text()
        self.assertIn("SERAPHINA_TELEGRAM_BOT_TOKEN=7123:AAH-secret-value-0123456789", env)
        code, _, err = self.cli("secret", "set", "SERAPHINA_TELEGRAM_BOT_TOKEN", stdin="x\n")
        self.assertEqual(code, 1)
        self.assertIn("--stdin", err)
        code, _, err = self.cli("secret", "set", "PATH", "--stdin", stdin="/bin\n")
        self.assertEqual(code, 1)
        self.assertNotIn("/bin", err)
        code, _, err = self.cli("secret", "set", "SERAPHINA_NTFY_TOKEN", "--stdin", stdin="")
        self.assertEqual(code, 1)
        self.assertIn("no value", err)

    def test_wake_pause_and_resume_work_without_the_server(self) -> None:
        self.make_config()
        code, out, _ = self.cli("wake", "pause")
        self.assertEqual(code, 0)
        self.assertIn("Seraphina is stopped", out)
        paused = self.data_dir / "wakes" / "PAUSED"
        self.assertEqual(json.loads(paused.read_text())["by"], "cli")
        self.assertEqual(self.cli("wake", "status", "--json")[1].strip()[:1], "{")
        self.assertTrue(json.loads(self.cli("wake", "status", "--json")[1])["paused"])
        code, out, _ = self.cli("wake", "resume")
        self.assertEqual((code, paused.exists()), (0, False))
        self.assertEqual(self.cli("wake", "status")[1].strip(), "running")
        audit = "".join(p.read_text() for p in (self.data_dir / "wakes").glob("audit-*.jsonl"))
        self.assertIn('"kind": "stop"', audit)
        self.assertIn('"kind": "restart"', audit)
        # step W2: one persona stops (offline: in the wake state the server reads), and resumes
        code, out, _ = self.cli("wake", "pause", "--persona", "seraphina", "--until", "08:00")
        self.assertEqual(code, 0, out)
        state = json.loads((self.data_dir / "wakes" / "state.json").read_text())
        # stored as the server stores it (an ISO UTC time of the next 08:00 in wakes.timezone), so the next start
        # reads the stop as still on instead of as already over (S43)
        from datetime import timedelta

        from swfactory import wakes

        until = state["stops"]["seraphina"]["until"]
        clock = wakes.wake_clock(Config.load(self.config_path))
        end = wakes.parse_iso(until)
        self.assertIsNotNone(end, until)
        self.assertEqual((state["stops"]["seraphina"]["by"], clock.hhmm(end)), ("cli", "08:00"))
        self.assertTrue(timedelta(0) < end - clock.utc() <= timedelta(days=1))
        self.assertEqual(self.cli("wake", "pause", "--persona", "seraphina", "--until", "8 o'clock")[0], 1)
        self.assertEqual(self.cli("wake", "resume", "--persona", "seraphina")[0], 0)
        state = json.loads((self.data_dir / "wakes" / "state.json").read_text())
        self.assertNotIn("seraphina", state["stops"])
        self.assertEqual(self.cli("wake", "pause", "--until", "08:00")[0], 1, "--until goes with --persona")

    def test_wake_poke_writes_the_spool_without_the_server(self) -> None:
        """W3-1: factory wake poke works without a running server; the reason rides in the file."""
        self.make_config()
        code, out, _ = self.cli("wake", "poke", "seraphina", "--reason", "test")
        self.assertEqual(code, 0, out)
        files = list((self.data_dir / "wakes" / "spool" / "new").iterdir())
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].name.startswith("seraphina."))
        self.assertEqual(json.loads(files[0].read_text()), {"reason": "test"})
        self.assertEqual(oct(files[0].stat().st_mode & 0o777), "0o600")
        self.assertEqual(list((self.data_dir / "wakes" / "spool" / "tmp").iterdir()), [])
        self.assertEqual(self.cli("wake", "poke", "Bad Name")[0], 1)
        self.assertEqual(self.cli("wake", "poke", "seraphina", "--reason", "x" * 201)[0], 1)


class MessagesServerCliTests(CliCase):
    def setUp(self) -> None:
        super().setUp()
        self.api = FakeApi()
        self.addCleanup(self.api.close)
        self.make_config(port=self.api.port)

    def test_notify_event_status_and_test(self) -> None:
        self.api.routes[("POST", "/api/notify/events")] = (202, {"message": "M-000004"})
        code, out, _ = self.cli("notify", "event", "backup.failed", "--subject", "nightly", "--detail", "exit 23")
        self.assertEqual((code, out.strip()), (0, "M-000004"))
        self.assertEqual(self.api.requests[-1]["body"], {"kind": "backup.failed", "subject": "nightly",
                                                         "detail": "exit 23"})
        self.api.routes[("GET", "/api/notify")] = (200, {
            "channels": [{"name": "phone", "type": "ntfy", "privacy": "tailnet", "active": True,
                          "health": {"ok": True, "detail": "listening"}, "last_answer": "2026-10-21T10:00:00Z"}],
            "quiet_hours": {"window": "21:30-08:00", "source": "from your power schedule"},
            "paused": None, "today": {"phone": 2, "cap": 12}})
        code, out, err = self.cli("notify", "status")
        self.assertEqual(code, 0)
        self.assertIn("phone  ntfy  tailnet  ok", out)
        self.assertIn("21:30-08:00", err)
        self.api.routes[("GET", "/api/notify/channels")] = (200, {"channels": [{"name": "phone", "enabled": True}]})
        self.api.routes[("POST", "/api/notify/test")] = (202, {"message": {"id": "M-000005",
                                                                           "deliveries": {"phone": "queued"}}})
        code, out, _ = self.cli("notify", "test")
        self.assertEqual((code, out.strip()), (0, "phone  M-000005  queued"))
        self.assertEqual(self.api.requests[-1]["body"], {"channel": "phone"})

    def test_wake_pause_tells_a_running_server_and_resume_goes_through_it(self) -> None:
        self.api.routes[("GET", "/api/state")] = (200, {"jobs": []})
        self.api.routes[("POST", "/api/wakes/control")] = (200, {"paused": None})
        self.cli("wake", "pause")
        self.assertEqual(self.api.requests[-1]["body"], {"action": "pause"})
        self.assertTrue((self.data_dir / "wakes" / "PAUSED").exists(), "stopped at once, server or not")
        self.cli("wake", "resume")
        self.assertEqual(self.api.requests[-1]["body"], {"action": "resume"})

    def test_secret_set_through_a_running_server(self) -> None:
        self.api.routes[("GET", "/api/state")] = (200, {"jobs": []})
        self.api.routes[("POST", "/api/secrets")] = (200, {"secret": {"name": "SERAPHINA_NTFY_TOKEN", "set": True}})
        code, out, err = self.cli("secret", "set", "SERAPHINA_NTFY_TOKEN", "--stdin", stdin="tk_value\n")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.api.requests[-1]["body"], {"name": "SERAPHINA_NTFY_TOKEN", "value": "tk_value"})
        self.assertNotIn("tk_value", out + err)
