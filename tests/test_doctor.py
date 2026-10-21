from __future__ import annotations

import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from unittest import mock

from swfactory import doctor, gitops
from swfactory.config import Config, deep_merge
from tests.helpers import make_repo
from tests.mock_llm import MockLLM
from tests.test_settings import TOKEN, SettingsTestCase, closed_port

LEVELS = {"error", "warn", "info"}
KEY = "doctor-test-key-0123456789"


class AuthServer:
    """A local /models endpoint: 200 with the right bearer key, else 401 (always 401 if reject_all)."""

    def __init__(self, reject_all: bool = False) -> None:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                ok = not owner.reject_all and self.headers.get("Authorization") == f"Bearer {KEY}"
                body = b'{"data": []}' if ok else b'{"error": "unauthorized"}'
                self.send_response(200 if ok else 401)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.reject_all = reject_all
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def __enter__(self) -> "AuthServer":
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()


class DoctorTestCase(SettingsTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.llm = MockLLM(handler=lambda body, path: {"tool_calls": [{"name": "get_time", "arguments": {}}]}
                           if body.get("tools") else {"text": "OK"})
        self.llm.start()
        self.addCleanup(self.llm.stop)

    def local_config(self, **sections: Any) -> dict:
        """A config whose tiers (and so every agent) run on the mock server: no network beyond 127.0.0.1."""
        model = {"provider": "mocklocal", "model": "mock-model", "max_tokens": 4096, "extra_body": {}}
        data: dict[str, Any] = {
            "server": {"token": TOKEN},
            "data_dir": str(self.tmp / "data"),
            "providers": {"mocklocal": {"type": "openai", "base_url": self.llm.base_url, "api_key_env": None}},
            "models": {"opus": dict(model), "sonnet": dict(model), "haiku": dict(model)},
        }
        return deep_merge(data, sections)

    def run_doctor(self, data: dict | None = None, save: bool = True, **kwargs: Any) -> dict[str, dict]:
        config = Config(self.config_path, data if data is not None else self.local_config())
        if save:
            config.save()
            config = Config.load(self.config_path)
        checks = doctor.run_checks(config, **kwargs)
        self.assertShape(checks)
        return {check["name"]: check for check in checks}

    def assertShape(self, checks: list[dict]) -> None:
        self.assertIsInstance(checks, list)
        for check in checks:
            self.assertEqual(set(check), {"name", "ok", "level", "detail"}, check)
            self.assertIsInstance(check["name"], str)
            self.assertIsInstance(check["ok"], bool)
            self.assertIn(check["level"], LEVELS)
            self.assertIsInstance(check["detail"], str)
            if check["ok"]:
                self.assertEqual(check["level"], "info", check)

    def assertCheck(self, checks: dict[str, dict], name: str, ok: bool, level: str, *fragments: str) -> None:
        self.assertIn(name, checks, sorted(checks))
        check = checks[name]
        self.assertEqual((check["ok"], check["level"]), (ok, level), check)
        for fragment in fragments:
            self.assertIn(fragment, check["detail"])


class DoctorBasicsTest(DoctorTestCase):
    def test_healthy_local_setup(self) -> None:
        checks = self.run_doctor()
        for name in ("python", "git", "config file", "server token", "data dir", "sandbox", "agents", "routing",
                     "provider mocklocal reachable"):
            with self.subTest(check=name):
                self.assertCheck(checks, name, True, "info")
        for name in ("rg", "node", "agent-browser"):
            self.assertEqual(checks[name]["level"], "info")
        self.assertCheck(checks, "repos", False, "warn", "factory repo add")
        self.assertIn("builder → opus (mocklocal, builtin)", checks["routing"]["detail"])
        self.assertNotIn("provider mocklocal key", checks, "keyless providers have no key check")
        self.assertFalse(any(name.startswith("model ") for name in checks), "models are probed only on demand")

    def test_config_file_mode_and_token(self) -> None:
        self.config_path.parent.mkdir(parents=True)
        data = self.local_config(server={"token": None})
        checks = self.run_doctor(data)
        self.assertCheck(checks, "config file", True, "info", "mode 600")
        self.assertCheck(checks, "server token", False, "error", "factory init")
        os.chmod(self.config_path, 0o644)
        checks = {c["name"]: c for c in doctor.run_checks(Config.load(self.config_path))}
        self.assertCheck(checks, "config file", False, "warn", "chmod 600")
        self.config_path.unlink()
        checks = {c["name"]: c for c in doctor.run_checks(Config(self.config_path, self.local_config()))}
        self.assertCheck(checks, "config file", False, "warn", "does not exist")

    def test_data_dir(self) -> None:
        (self.tmp / "data").mkdir()
        self.assertCheck(self.run_doctor(), "data dir", True, "info", "is writable")
        blocker = self.tmp / "a-file"
        blocker.write_text("x")
        checks = self.run_doctor(self.local_config(data_dir=str(blocker / "data")))
        self.assertCheck(checks, "data dir", False, "error", "cannot be created", "a-file is not a directory")
        checks = self.run_doctor(self.local_config(data_dir=str(blocker)))
        self.assertCheck(checks, "data dir", False, "error", "a-file exists but is not a directory")
        dangling = self.tmp / "dangling"
        dangling.symlink_to(self.tmp / "nowhere")
        checks = self.run_doctor(self.local_config(data_dir=str(dangling)))
        self.assertCheck(checks, "data dir", False, "error", "exists but is not a directory")
        checks = self.run_doctor(self.local_config(data_dir=str(self.tmp / "new" / "deeper")))
        self.assertCheck(checks, "data dir", True, "info", "will be created")

    def test_git_versions(self) -> None:
        with mock.patch.object(doctor.gitops, "git_version", return_value=(2, 34, 1)):
            self.assertCheck(self.run_doctor(), "git", False, "warn", "2.34.1", "trial merges")
        with mock.patch.object(doctor.gitops, "git_version", side_effect=gitops.GitError(["git"], "not found")):
            self.assertCheck(self.run_doctor(), "git", False, "error", "not installed")

    def test_sandbox(self) -> None:
        self.assertCheck(self.run_doctor(), "sandbox", True, "info", "not contained")
        data = self.local_config(harness={"sandbox": "bwrap"})
        with mock.patch.object(doctor, "bwrap_available", return_value=False):
            self.assertCheck(self.run_doctor(data), "sandbox", False, "error", "bubblewrap")
        with mock.patch.object(doctor, "bwrap_available", return_value=True):
            self.assertCheck(self.run_doctor(data), "sandbox", True, "info", "sandboxed")

    def test_optional_tools(self) -> None:
        with mock.patch.object(doctor.shutil, "which", return_value=None):
            checks = self.run_doctor()
        for name in ("rg", "node", "agent-browser"):
            self.assertCheck(checks, name, False, "info", "not found")

    def test_a_crashing_check_is_reported_not_raised(self) -> None:
        with mock.patch.object(doctor, "_git", side_effect=RuntimeError("boom")):
            checks = self.run_doctor()
        self.assertCheck(checks, "git", False, "error", "RuntimeError: boom")
        self.assertIn("python", checks)

    def test_garbage_config_never_raises(self) -> None:
        data = self.local_config(models={"opus": {"max_tokens": [1]}, "sonnet": "not-an-object"},
                                 pipeline={"reviewers": "security"}, repos=[{"name": "x"}], providers=[],
                                 drivers={"builtin": None}, harness="none")
        checks = self.run_doctor(data, save=False)
        self.assertCheck(checks, "agents", False, "error", "pipeline.reviewers")
        self.assertCheck(checks, "repos", False, "error", "needs a name and a path")
        self.assertFalse(checks["routing"]["ok"])

    def test_parallel_jobs_time_out(self) -> None:
        started = time.monotonic()
        results = doctor._parallel([("slow", lambda: time.sleep(2) or []),
                                    ("fast", lambda: [doctor.check("fast", True, "info", "done")]),
                                    ("crash", lambda: 1 / 0)], timeout=0.3)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual([(r["name"], r["ok"], r["level"]) for r in results],
                         [("slow", False, "warn"), ("fast", True, "info"), ("crash", False, "error")])


class DoctorReposAndAgentsTest(DoctorTestCase):
    def test_repo_checks(self) -> None:
        good = make_repo(self.tmp, "good", {"AGENTS.md": "# agents\n"})
        bare = make_repo(self.tmp, "bare")
        (bare / "sub").mkdir()
        repos = [{"name": "good", "path": str(good)}, {"name": "bare", "path": str(bare)},
                 {"name": "gone", "path": str(self.tmp / "gone")},
                 {"name": "sub", "path": str(bare / "sub")},
                 {"name": "plain", "path": str(self.tmp)},
                 {"name": "nobase", "path": str(good), "base": "nope"}]
        checks = self.run_doctor(self.local_config(repos=repos))
        self.assertCheck(checks, "repo good", True, "info", "base main at")
        self.assertCheck(checks, "repo good instructions", True, "info", "AGENTS.md on main")
        self.assertCheck(checks, "repo bare", True, "info")
        self.assertCheck(checks, "repo bare instructions", False, "warn", "repo scout")
        self.assertCheck(checks, "repo gone", False, "error", "does not exist")
        self.assertCheck(checks, "repo sub", False, "error", "not the top level")
        self.assertCheck(checks, "repo plain", False, "error", "not a git repository")
        self.assertCheck(checks, "repo nobase", False, "error", "base branch does not resolve")

    def test_repo_agent_overrides(self) -> None:
        good = make_repo(self.tmp, "good")
        broken = make_repo(self.tmp, "broken")
        for repo, text in ((good, "---\nname: reviewer-code\ndescription: d\n---\nBody.\n"), (broken, "oops\n")):
            (repo / ".factory" / "agents").mkdir(parents=True)
            (repo / ".factory" / "agents" / "reviewer-code.md").write_text(text)
        checks = self.run_doctor(self.local_config(repos=[{"name": "good", "path": str(good)},
                                                          {"name": "broken", "path": str(broken)}]))
        self.assertCheck(checks, "repo good agents", True, "info")
        self.assertCheck(checks, "repo broken agents", False, "error", "reviewer-code.md")

    def test_overrides_written_before_the_tracker_are_named(self) -> None:
        self.assertNotIn("agents items", self.run_doctor(), "the shipped agents carry the contract")
        self.agents_dir.mkdir(parents=True)
        (self.agents_dir / "builder.md").write_text("---\nname: builder\ndescription: mine\n---\nBuild it.\n")
        (self.agents_dir / "reviewer-perf.md").write_text(
            "---\nname: reviewer-perf\ndescription: d\n---\nReview.\n\n## Items\nAnswer under ## Items.\n")
        checks = self.run_doctor()
        self.assertCheck(checks, "agents items", False, "warn", "builder (", "no ## Items section",
                         "still gets the contract from the harness")
        self.assertNotIn("reviewer-perf", checks["agents items"]["detail"])
        repo = make_repo(self.tmp, "old")
        (repo / ".factory" / "agents").mkdir(parents=True)
        (repo / ".factory" / "agents" / "reviewer-code.md").write_text(
            "---\nname: reviewer-code\ndescription: d\n---\nBody.\n")
        checks = self.run_doctor(self.local_config(repos=[{"name": "old", "path": str(repo)}]))
        self.assertCheck(checks, "repo old agents items", False, "warn", "reviewer-code (")

    def test_missing_and_broken_agents(self) -> None:
        checks = self.run_doctor(self.local_config(pipeline={"reviewers": ["security", "perf"]}))
        self.assertCheck(checks, "agents", False, "error", "reviewer-perf")
        self.agents_dir.mkdir(parents=True)
        (self.agents_dir / "bad.md").write_text("no frontmatter\n")
        self.assertCheck(self.run_doctor(), "agents", False, "error", "bad.md")

    def test_factory_supplies_the_agents(self) -> None:
        class Factory:
            def __init__(self, agents: dict) -> None:
                self._agents = agents

            def agents(self, repo: str | None = None) -> dict:
                if self._agents is None:
                    raise ValueError("agents/broken.md: missing frontmatter")
                return self._agents

        checks = self.run_doctor(factory=Factory({}))
        self.assertCheck(checks, "agents", False, "error", "spec-writer", "builder", "repo-scout")
        checks = self.run_doctor(factory=Factory(None))
        self.assertCheck(checks, "agents", False, "error", "broken.md")

    def test_only_optional_agents_missing_is_a_warning(self) -> None:
        from swfactory import settings
        config = Config(self.config_path, self.local_config())
        agents = {name: agent for name, agent in settings.resolved_agents(config).items() if name != "lead"}
        self.assertCheck({c["name"]: c for c in doctor._agents(config, agents)}, "agents", False, "warn", "lead")


class DoctorRoutesAndProvidersTest(DoctorTestCase):
    def test_routing_problems(self) -> None:
        data = self.local_config(models={"orphan": {"provider": "nope", "model": "x"}},
                                 agents={"*": {"driver": "builtin"}, "builder": {"model": "orphan"},
                                         "approver": {"model": "some-literal-id"}})
        checks = self.run_doctor(data, save=False)
        self.assertCheck(checks, "routing", False, "error", "builder → orphan", "nope")
        data["agents"].pop("builder")
        checks = self.run_doctor(data, save=False)
        self.assertCheck(checks, "routing", False, "warn", "approver (some-literal-id)")

    def test_unreachable_local_provider_warns(self) -> None:
        data = self.local_config(
            providers={"down": {"type": "openai", "base_url": f"http://127.0.0.1:{closed_port()}/v1",
                                "api_key_env": None}},
            models={"down-model": {"provider": "down", "model": "x", "max_tokens": 100}},
            agents={"*": {"driver": "builtin"}, "builder": {"model": "down-model"}})
        checks = self.run_doctor(data)
        self.assertCheck(checks, "provider down reachable", False, "warn", "start your local server",
                         "used by builder")

    def test_missing_key_is_an_error_and_present_key_passes(self) -> None:
        data = self.local_config(providers={"mocklocal": {"api_key_env": "DOCTOR_TEST_KEY"}})
        checks = self.run_doctor(data)
        self.assertCheck(checks, "provider mocklocal key", False, "error", "DOCTOR_TEST_KEY is not set")
        self.assertNotIn("provider mocklocal auth", checks, "no key: nothing to compare against")
        os.environ["DOCTOR_TEST_KEY"] = KEY
        checks = self.run_doctor(data)
        self.assertCheck(checks, "provider mocklocal key", True, "info", "from the environment")
        self.assertCheck(checks, "provider mocklocal auth", False, "warn", "answers without the key")
        self.assertNotIn(KEY, repr(checks))

    def test_key_from_file(self) -> None:
        key_file = self.tmp / "strata.env"
        key_file.write_text(f"DOCTOR_TEST_KEY={KEY}\n")
        data = self.local_config(providers={"mocklocal": {"api_key_env": "DOCTOR_TEST_KEY",
                                                          "api_key_file": str(key_file)}})
        self.assertCheck(self.run_doctor(data), "provider mocklocal key", True, "info", str(key_file))

    def test_server_enforcing_its_key(self) -> None:
        os.environ["DOCTOR_TEST_KEY"] = KEY
        with AuthServer() as server:
            data = self.local_config(providers={"mocklocal": {"base_url": server.base_url,
                                                              "api_key_env": "DOCTOR_TEST_KEY"}})
            checks = self.run_doctor(data)
        self.assertCheck(checks, "provider mocklocal reachable", True, "info", "HTTP 200")
        self.assertCheck(checks, "provider mocklocal auth", True, "info", "rejects requests without the key")

    def test_rejected_key_is_an_error(self) -> None:
        os.environ["DOCTOR_TEST_KEY"] = KEY
        with AuthServer(reject_all=True) as server:
            data = self.local_config(providers={"mocklocal": {"base_url": server.base_url,
                                                              "api_key_env": "DOCTOR_TEST_KEY"}})
            checks = self.run_doctor(data)
        self.assertCheck(checks, "provider mocklocal reachable", False, "error", "HTTP 401", "DOCTOR_TEST_KEY")

    def test_ollama_context_advice(self) -> None:
        data = self.local_config(
            providers={"ollama": {"type": "openai", "base_url": f"http://127.0.0.1:{closed_port()}/v1",
                                  "api_key_env": None}},
            models={"opus": {"provider": "ollama", "model": "qwen3-coder:30b", "max_tokens": 8192,
                             "context_tokens": 65536}})
        self.assertCheck(self.run_doctor(data), "provider ollama context", True, "info", "OLLAMA_CONTEXT_LENGTH=65536")

    def test_cli_drivers_used_by_routes(self) -> None:
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        good = bin_dir / "fakecli"
        good.write_text("#!/bin/sh\necho 'fakecli 1.2.3'\n")
        good.chmod(0o755)
        failing = bin_dir / "failcli"
        failing.write_text("#!/bin/sh\necho 'bad flag' >&2\nexit 3\n")
        failing.chmod(0o755)
        drivers = {"fake": {"type": "cli", "preset": "qwen", "command": str(good)},
                   "relative": {"type": "cli", "preset": "qwen", "command": "bin/fakecli --quiet"},
                   "failing": {"type": "cli", "preset": "qwen", "command": str(failing)},
                   "ghost": {"type": "cli", "preset": "qwen", "command": "definitely-not-installed-cli"},
                   "unused": {"type": "cli", "preset": "qwen", "command": "also-not-installed"}}
        config = Config(self.tmp / "config.json", self.local_config(
            drivers=drivers, agents={"*": {"driver": "builtin"}, "builder": {"driver": "fake"},
                                     "approver": {"driver": "relative"}, "reviewer-ux": {"driver": "failing"},
                                     "reviewer-ui": {"driver": "ghost"}}))
        checks = {c["name"]: c for c in doctor.run_checks(config)}
        self.assertCheck(checks, "driver fake", True, "info", "fakecli 1.2.3", "used by builder")
        self.assertCheck(checks, "driver relative", True, "info", "fakecli 1.2.3")
        self.assertCheck(checks, "driver failing", False, "error", "exit 3", "bad flag")
        self.assertCheck(checks, "driver ghost", False, "error", "not on PATH", "reviewer-ui")
        self.assertNotIn("driver unused", checks)
        self.assertNotIn("driver builtin", checks)


    def test_driver_check_runs_the_command_as_the_driver_does(self) -> None:
        """An existing path with spaces is one program (not shlex-split), and --version runs with the
        driver's environment: secrets removed, the driver's env applied."""
        tools = self.tmp / "My Tools"
        tools.mkdir()
        spaced = tools / "qwen"
        spaced.write_text('#!/bin/sh\necho "qwen 0.25.3 home=$HOME leak=${AWS_SECRET_ACCESS_KEY:-none}"\n')
        spaced.chmod(0o755)
        drivers = {"qwen": {"type": "cli", "preset": "qwen", "command": str(spaced),
                            "env": {"HOME": "/driver-home"}},
                   "odd": {"type": "plugin", "command": "x"}}
        config = Config(self.tmp / "config.json", self.local_config(
            drivers=drivers, agents={"*": {"driver": "builtin"}, "reviewer-ux": {"driver": "qwen"}}))
        with mock.patch.dict(os.environ, {"AWS_SECRET_ACCESS_KEY": "aws-secret-value-0001"}):
            found = doctor._driver_check(config, "qwen", ["reviewer-ux"])
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0]["ok"], found[0])
        self.assertIn(f"{spaced}: qwen 0.25.3 home=/driver-home leak=none", found[0]["detail"])
        missing = doctor._driver_check(Config(self.tmp / "config.json", self.local_config(
            drivers={"qwen": {"type": "cli", "preset": "qwen", "command": str(tools / "gone")}})), "qwen", ["x"])
        self.assertFalse(missing[0]["ok"])
        self.assertIn("does not exist or is not executable", missing[0]["detail"])
        odd = doctor._driver_check(config, "odd", ["x"])
        self.assertIn("unknown type 'plugin'", odd[0]["detail"])


class DoctorProbeTest(DoctorTestCase):
    def test_probe_models_on_demand(self) -> None:
        data = self.local_config(
            providers={"down": {"type": "openai", "base_url": f"http://127.0.0.1:{closed_port()}/v1",
                                "api_key_env": None}},
            models={"down-model": {"provider": "down", "model": "x", "max_tokens": 100}},
            agents={"*": {"driver": "builtin"}, "builder": {"model": "down-model"}})
        checks = self.run_doctor(data, probe_models=True)
        self.assertCheck(checks, "model opus", True, "info", "tool calling works")
        self.assertCheck(checks, "model sonnet", True, "info")
        self.assertCheck(checks, "model down-model", False, "error", "cannot reach")

    def test_probe_without_tool_call_warns(self) -> None:
        with MockLLM(handler=lambda body, path: {"text": "OK"}) as texty:
            data = self.local_config(providers={"mocklocal": {"base_url": texty.base_url}})
            checks = self.run_doctor(data, probe_models=True)
        self.assertCheck(checks, "model opus", False, "warn", "tool-call probe failed", "--jinja")

    @staticmethod
    def slow_handler(body: dict, path: str) -> dict:
        reply = {"tool_calls": [{"name": "get_time", "arguments": {}}]} if body.get("tools") else {"text": "OK"}
        return {**reply, "delay": 0.4}

    def one_slot_config(self, base_url: str, entries: dict[str, dict], slots: int | None = 1) -> dict:
        provider = {"type": "openai", "base_url": base_url, "api_key_env": None, "max_concurrency": slots,
                    "concurrency_scope": "run", "timeout": 60}
        return self.local_config(providers={"onebox": provider}, models=entries,
                                 agents={"*": {"driver": "builtin", "model": "local"}})

    def test_tier_copies_are_probed_once(self) -> None:
        """The strata preset: local plus three tiers copying it, all on one 1-slot server."""
        from swfactory import settings
        with MockLLM(handler=self.slow_handler) as llm:
            config = settings.apply_preset(self.make_config({"data_dir": str(self.tmp / "data")}), "strata")
            config = settings.update_config(config, {"providers": {"strata": {
                "base_url": llm.base_url, "api_key_env": None, "api_key_file": None}}})
            self.assertEqual({tier: view["same_as"] for tier, view in settings.tier_view(config).items()},
                             {"opus": "local", "sonnet": "local", "haiku": "local"})
            ordered = doctor.run_checks(config, probe_models=True)
        self.assertShape(ordered)
        checks = {c["name"]: c for c in ordered}
        self.assertCheck(checks, "model local", True, "info", "tool calling works")
        for key in ("opus", "sonnet", "haiku"):
            self.assertCheck(checks, f"model {key}", True, "info", "same model as local, probed once")
        self.assertEqual(len(llm.requests), 2, "one plain and one tool probe for the one distinct model")
        names = [c["name"] for c in ordered if c["name"].startswith("model ")]
        self.assertEqual(names, ["model haiku", "model local", "model opus", "model sonnet"])

    def test_a_one_slot_provider_gets_one_probe_at_a_time(self) -> None:
        """Each call takes 0.4s and may take 0.7s: probes queued on the one slot would time out waiting."""
        entries = {key: {"provider": "onebox", "model": f"m-{key}", "max_tokens": 4096, "stream": False,
                         "extra_body": {}} for key in ("local", "opus", "sonnet")}
        real_test_model = doctor.settings.test_model
        quick = mock.patch.object(doctor.settings, "test_model",
                                  side_effect=lambda config, key: real_test_model(config, key, timeout=0.7))
        with MockLLM(handler=self.slow_handler) as llm, quick:
            started = time.monotonic()
            checks = self.run_doctor(self.one_slot_config(llm.base_url, entries), probe_models=True)
            elapsed = time.monotonic() - started
        for key in entries:
            self.assertCheck(checks, f"model {key}", True, "info", "tool calling works")
        self.assertEqual(llm.max_concurrent, 1)
        self.assertEqual(len(llm.requests), 6)
        self.assertGreaterEqual(elapsed, 6 * 0.4)

    def test_providers_with_more_slots_probe_in_parallel(self) -> None:
        entries = {key: {"provider": "onebox", "model": f"m-{key}", "max_tokens": 4096, "stream": False,
                         "extra_body": {}} for key in ("local", "opus", "sonnet", "haiku")}
        for slots, expected in ((2, 2), (None, 4)):
            with self.subTest(slots=slots), MockLLM(handler=self.slow_handler) as llm:
                checks = self.run_doctor(self.one_slot_config(llm.base_url, entries, slots), probe_models=True)
                for key in entries:
                    self.assertCheck(checks, f"model {key}", True, "info")
                self.assertEqual(llm.max_concurrent, expected)


if __name__ == "__main__":
    unittest.main()


class DoctorToolsTest(DoctorTestCase):
    def write_agent(self, name: str, mcp: str) -> None:
        self.agents_dir.mkdir(parents=True, exist_ok=True)
        (self.agents_dir / f"{name}.md").write_text(f"---\nname: {name}\ndescription: Probe.\ntools: Read\n"
                                                    f"model: opus\nmcp: {mcp}\n---\nYou probe.\n")

    def test_a_check_per_mcp_server_an_agent_uses(self) -> None:
        self.write_agent("probe-a", "srv, ghost")
        self.write_agent("probe-b", "srv")
        self.write_agent("builder", "srv, ghost")   # a factory-line agent: its servers are needed
        script = self.tmp / "mcp.sh"
        script.write_text("#!/bin/sh\n")
        os.chmod(script, 0o755)
        plain = self.tmp / "plain.sh"
        plain.write_text("#!/bin/sh\n")
        servers = {"srv": {"command": "python3", "tools": ["one", "two"]}, "unused": {"command": "nope-x"}}
        checks = self.run_doctor(self.local_config(mcp_servers=servers))
        self.assertCheck(checks, "mcp srv", True, "info", "python3", "tools: one, two",
                         "used by builder, probe-a, probe-b")
        self.assertCheck(checks, "mcp ghost", False, "warn", "no entry 'ghost'", "used by builder, probe-a")
        self.assertNotIn("mcp unused", checks, "only servers an agent uses are checked; nothing is started")
        cases = [({"command": "no-such-mcp-binary"}, False, "error", "not on PATH"),
                 ({"command": str(plain)}, False, "error", "is not executable"),
                 ({"command": str(self.tmp / "missing.sh")}, False, "error", "does not exist"),
                 ({"command": "bin/mcp.sh"}, False, "error", "relative", "absolute path"),
                 ({"command": "./mcp.sh", "cwd": str(self.tmp)}, True, "info", str(script)),
                 ({"command": str(script)}, True, "info", "all of its tools"),
                 ({"command": "mcp.sh", "env": {"PATH": str(self.tmp)}}, True, "info", str(script))]
        for entry, ok, level, *fragments in cases:
            with self.subTest(entry=entry):
                checks = self.run_doctor(self.local_config(mcp_servers={"srv": entry}))
                self.assertCheck(checks, "mcp srv", ok, level, *fragments)

    def test_servers_only_chat_agents_use_are_optional(self) -> None:
        self.write_agent("probe-chat", "chatsrv")
        checks = self.run_doctor()
        self.assertCheck(checks, "mcp chatsrv", True, "info", "not configured (optional: only Chat uses it)",
                         "no entry 'chatsrv'", "used by probe-chat")
        self.assertCheck(checks, "mcp rea", True, "info", "no entry 'rea'", "the rea preset adds it",
                         "used by analyst")   # the shipped analyst on a default install
        checks = self.run_doctor(self.local_config(mcp_servers={"chatsrv": {"command": "no-such-mcp-binary"}}))
        self.assertCheck(checks, "mcp chatsrv", False, "warn", "not on PATH", "(only Chat uses it)")
        self.write_agent("reviewer-code", "chatsrv")
        checks = self.run_doctor(self.local_config(mcp_servers={"chatsrv": {"command": "no-such-mcp-binary"}}))
        self.assertCheck(checks, "mcp chatsrv", False, "error", "not on PATH", "used by probe-chat, reviewer-code")
        repo = make_repo(self.tmp, "app")
        status = doctor.setup_status(Config(self.config_path, self.local_config(repos=[{"name": "app",
                                                                                       "path": str(repo)}])))
        self.assertFalse(status["steps"]["tools"]["ok"], "a line agent's server that does not start needs attention")
        (self.agents_dir / "reviewer-code.md").unlink()
        status = doctor.setup_status(Config(self.config_path, self.local_config(repos=[{"name": "app",
                                                                                       "path": str(repo)}])))
        self.assertTrue(status["steps"]["tools"]["ok"], status["steps"]["tools"])
        self.assertIn("no MCP servers in use; not configured, Chat only: chatsrv, rea",
                      status["steps"]["tools"]["detail"])

    def test_mcp_on_a_cli_driver_is_noted(self) -> None:
        self.write_agent("probe-a", "srv")
        data = self.local_config(mcp_servers={"srv": {"command": "python3"}},
                                 agents={"probe-a": {"driver": "codex"}})
        self.assertCheck(self.run_doctor(data), "mcp srv", True, "info", "probe-a run on a CLI driver")

    def test_skills_dirs(self) -> None:
        empty = self.tmp / "empty"
        empty.mkdir()
        skills = self.tmp / "conf" / "my-skills"
        (skills / "collection" / "tdd").mkdir(parents=True)
        (skills / "collection" / "tdd" / "SKILL.md").write_text("---\nname: tdd\ndescription: x\n---\nbody\n")
        (skills / "review").mkdir()
        (skills / "review" / "SKILL.md").write_text("---\nname: review\ndescription: x\n---\nbody\n")
        data = self.local_config(harness={"skills_dirs": [str(self.tmp / "missing"), str(empty), "my-skills"]})
        checks = self.run_doctor(data)
        self.assertCheck(checks, f"skills dir {self.tmp / 'missing'}", False, "warn", "does not exist")
        self.assertCheck(checks, f"skills dir {empty}", False, "warn", "holds no SKILL.md")
        self.assertCheck(checks, "skills dir my-skills", True, "info", str(skills), "2 skill(s)")


class SetupStatusTest(DoctorTestCase):
    def status(self, data: dict) -> dict:
        result = doctor.setup_status(Config(self.config_path, data))
        self.assertEqual(set(result), {"configured", "steps"})
        self.assertEqual(set(result["steps"]), {"providers", "models", "routing", "repos", "tools", "output"})
        for step in result["steps"].values():
            self.assertEqual(set(step), {"ok", "detail"})
            self.assertIsInstance(step["detail"], str)
        return result

    def test_a_fresh_config_needs_keys_and_a_repo(self) -> None:
        result = self.status({"server": {"token": TOKEN}})
        self.assertFalse(result["configured"])
        steps = result["steps"]
        self.assertFalse(steps["providers"]["ok"])
        self.assertIn("DEEPSEEK_API_KEY (deepseek", steps["providers"]["detail"])
        self.assertTrue(steps["models"]["ok"])
        self.assertIn("opus → deepseek-v4-pro (deepseek)", steps["models"]["detail"])
        self.assertTrue(steps["routing"]["ok"])
        self.assertFalse(steps["repos"]["ok"])
        self.assertEqual(steps["output"], {"ok": True, "detail": "merge: approved jobs merge into the base branch"})

    def test_configured_once_keys_and_a_repo_are_there(self) -> None:
        repo = make_repo(self.tmp, "app")
        data = self.local_config(repos=[{"name": "app", "path": str(repo)}],
                                 merge={"delivery": "folder", "output_dir": str(self.tmp / "out")})
        result = self.status(data)
        self.assertTrue(result["configured"], result)
        steps = result["steps"]
        self.assertIn("mocklocal (no key needed)", steps["providers"]["detail"])
        self.assertIn("on local models", steps["routing"]["detail"])
        self.assertIn(f"app ({repo})", steps["repos"]["detail"])
        self.assertEqual(steps["output"]["detail"],
                         f"folder: approved jobs are exported to {self.tmp / 'out'}/<repo>/<job-id>/")
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": KEY}):
            self.assertTrue(self.status(dict(data, models={}))["steps"]["providers"]["ok"])

    def test_tools_and_output_problems_do_not_block_configured(self) -> None:
        repo = make_repo(self.tmp, "app")
        blocker = self.tmp / "file"
        blocker.write_text("x")
        data = self.local_config(repos=[{"name": "app", "path": str(repo)}],
                                 harness={"skills_dirs": [str(self.tmp / "missing")]},
                                 merge={"delivery": "folder", "output_dir": str(blocker / "out")})
        result = self.status(data)
        self.assertTrue(result["configured"])
        self.assertFalse(result["steps"]["tools"]["ok"])
        self.assertIn("does not exist", result["steps"]["tools"]["detail"])
        self.assertFalse(result["steps"]["output"]["ok"])
        self.assertIn("cannot be written", result["steps"]["output"]["detail"])

    def test_broken_settings_are_steps_that_need_attention(self) -> None:
        result = self.status(self.local_config(models={"opus": {"provider": "nowhere"}},
                                               merge={"delivery": "zip"}, pipeline={"reviewers": ["perf"]}))
        self.assertFalse(result["configured"])
        self.assertFalse(result["steps"]["models"]["ok"])
        self.assertIn("nowhere", result["steps"]["models"]["detail"])
        self.assertFalse(result["steps"]["routing"]["ok"])
        self.assertIn("reviewer-perf", result["steps"]["routing"]["detail"])
        self.assertFalse(result["steps"]["output"]["ok"])
        self.assertIn("merge.delivery", result["steps"]["output"]["detail"])
