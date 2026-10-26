from __future__ import annotations

import contextlib
import json
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from swfactory import doctor, gitops, personas
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

    def test_sandbox_inside_the_sandbox_is_a_named_limitation(self) -> None:
        """The owner's server refuses a sandbox inside the sandbox (AppArmor's user-namespace restriction): said in
        plain words, a warning only when agents would try a browser, and never a failure of harness.sandbox."""
        data = self.local_config(harness={"sandbox": "bwrap"})
        refusal = "bwrap: No permissions to create a new namespace, likely because the kernel does not allow it"
        with mock.patch.object(doctor, "bwrap_available", return_value=True), \
                mock.patch.object(doctor, "nested_bwrap", return_value=(False, refusal)):
            with mock.patch.object(doctor.shutil, "which", side_effect=lambda name: None):
                checks = self.run_doctor(data)
            self.assertCheck(checks, "sandbox", True, "info", "private /tmp")
            self.assertCheck(checks, "sandbox inside the sandbox", False, "info", "No permissions", "curl",
                             "never puts a CLI agent", "do not loosen")
            with mock.patch.object(doctor.shutil, "which",
                                   side_effect=lambda name: "/usr/local/bin/agent-browser" if name == "agent-browser"
                                   else None):
                self.assertCheck(self.run_doctor(data), "sandbox inside the sandbox", False, "warn", "agent-browser")
        with mock.patch.object(doctor, "bwrap_available", return_value=True), \
                mock.patch.object(doctor, "nested_bwrap", return_value=(True, "")):
            self.assertCheck(self.run_doctor(data), "sandbox inside the sandbox", True, "info", "works")
        self.assertNotIn("sandbox inside the sandbox", self.run_doctor(), "not asked without bwrap")

    def test_an_account_that_can_become_root(self) -> None:
        """DESIGN §17: bwrap does not contain an account in docker, lxd, sudo or disk; an error while agents run
        commands outside it, a warning under it, and nothing to say for an account in none of them."""
        config = Config(self.config_path, self.local_config())
        bwrap = Config(self.config_path, self.local_config(harness={"sandbox": "bwrap"}))
        agents = {"builder": SimpleNamespace(tools=["Read", "Bash"])}
        quiet = {"scout": SimpleNamespace(tools=["Read"])}
        found = doctor._rights(config, agents, (1000, "owner", ["adm", "docker", "lxd", "users"]))
        self.assertEqual(found[0]["level"], "error")
        self.assertIn("owner, a member of docker, lxd", found[0]["detail"])
        self.assertIn("the Docker socket starts root containers", found[0]["detail"])
        self.assertIn("harness.sandbox is none", found[0]["detail"])
        under_bwrap = doctor._rights(bwrap, agents, (1000, "owner", ["docker"]))[0]
        self.assertEqual(under_bwrap["level"], "warn")
        self.assertIn("a socket made during a run (Docker or LXD restarted, or installed) is not",
                      under_bwrap["detail"], "the masks are a second line only")
        self.assertEqual(doctor._rights(config, quiet, (1000, "owner", ["docker"]))[0]["level"], "warn",
                         "no agent runs commands")
        self.assertEqual(doctor._rights(bwrap, agents, (0, "root", ["root"]))[0]["level"], "warn")
        self.assertIn("runs as root", doctor._rights(config, agents, (0, "root", ["root"]))[0]["detail"])
        cli = Config(self.config_path, self.local_config(harness={"sandbox": "bwrap"}, agents={
            "builder": {"driver": "claude"}}))
        self.assertIn("CLI driver", doctor._rights(cli, agents, (1000, "owner", ["sudo"]))[0]["detail"])
        clean = doctor._rights(config, agents, (1001, "seraphina", ["seraphina"]))
        self.assertEqual((clean[0]["ok"], clean[0]["level"]), (True, "info"))
        self.assertIn("account", self.run_doctor())

    def test_sudo_programs_and_no_new_privileges(self) -> None:
        """Ubuntu 25.10 and later: sudo is sudo-rs, classic sudo stays as sudo.ws, and both read /etc/sudoers."""
        paths = {"sudo": "/usr/bin/sudo", "sudo.ws": "/usr/bin/sudo.ws"}
        versions = {"/usr/bin/sudo": "sudo-rs 0.2.13\n", "/usr/bin/sudo.ws": "Sudo version 1.9.17p2\nSudoers policy\n"}
        with mock.patch.object(doctor.os.path, "realpath", side_effect=lambda p: {
                "/usr/bin/sudo": "/usr/lib/cargo/bin/sudo"}.get(p, p)):
            found = doctor.sudo_programs(which=paths.get, run=lambda argv: versions[argv[0]])
        self.assertEqual([(f["name"], f["implementation"], f["version"]) for f in found],
                         [("sudo", "sudo-rs", "0.2.13"), ("sudo.ws", "sudo", "1.9.17p2")])
        config = Config(self.config_path, self.local_config())
        checks = {c["name"]: c for c in doctor._privileges(config, found, nnp=False, setpriv="/usr/bin/setpriv")}
        self.assertIn("sudo is sudo-rs 0.2.13; sudo.ws is classic sudo 1.9.17p2", checks["sudo"]["detail"])
        self.assertIn("sudo.ws -l -U", checks["sudo"]["detail"])
        self.assertIn("start with no-new-privileges (setpriv)", checks["no new privileges"]["detail"])
        self.assertIn("the harness's own git", checks["no new privileges"]["detail"])
        held = {c["name"]: c for c in doctor._privileges(config, found[:1], nnp=True, setpriv="/usr/bin/setpriv")}
        self.assertNotIn("sudo.ws", held["sudo"]["detail"])
        self.assertIn("sudo cannot run from it", held["no new privileges"]["detail"])
        bare = {c["name"]: c for c in doctor._privileges(config, [], nnp=False, setpriv=None)}
        self.assertNotIn("sudo", bare)
        self.assertEqual((bare["no new privileges"]["ok"], bare["no new privileges"]["level"]), (False, "warn"))
        bwrap = Config(self.config_path, self.local_config(harness={"sandbox": "bwrap"}))
        self.assertEqual(doctor._privileges(bwrap, [], nnp=False, setpriv=None)[0]["level"], "info")

    def test_python_versions_old_supported_and_newer_than_tested(self) -> None:
        old, = doctor._python((3, 9), "3.9.18")
        self.assertEqual((old["ok"], old["level"]), (False, "error"))
        self.assertIn("needs Python 3.10 or newer", old["detail"])
        for version, shown in (((3, 10), "3.10.12"), ((3, 14), "3.14.4")):
            found, = doctor._python(version, shown)
            self.assertEqual((found["ok"], found["detail"]), (True, f"Python {shown}"))
        newer, = doctor._python((3, 15), "3.15.0")
        self.assertTrue(newer["ok"], "newer than tested still runs")
        self.assertIn("newer than 3.14, the newest the test suite runs on", newer["detail"])

    def test_the_wake_alarm_against_the_next_local_wake(self) -> None:
        """The first morning of winter time: an alarm armed with the summer offset wakes the server an hour early."""
        from datetime import datetime, timezone

        from swfactory.clock import PowerSchedule

        power = PowerSchedule.from_config({"off": "23:00", "wake": "07:00"})
        alarm = self.tmp / "wakealarm"
        evening = datetime(2026, 10, 24, 21, 50, tzinfo=timezone.utc).timestamp()   # 22:50 BST
        alarm.write_text(str(int(datetime(2026, 10, 25, 6, 0, tzinfo=timezone.utc).timestamp())))
        self.assertCheck({"wake alarm": doctor._wake_alarm(power, "Europe/London", alarm, evening)}, "wake alarm",
                         False, "warn", "an hour earlier", "the other side of the clock change")
        alarm.write_text(str(int(datetime(2026, 10, 25, 7, 0, tzinfo=timezone.utc).timestamp())))
        self.assertCheck({"wake alarm": doctor._wake_alarm(power, "Europe/London", alarm, evening)}, "wake alarm",
                         True, "info", "07:00 on Sun 25 Oct (GMT), as wakes.power.wake says")
        self.assertIsNone(doctor._wake_alarm(power, "Europe/London", self.tmp / "unset", evening, quiet=True),
                          "no alarm and no phone channels: nothing to say")
        self.assertIn("no wake alarm is set now", doctor._wake_alarm(power, "Europe/London", self.tmp / "unset",
                                                                    evening)["detail"])
        with mock.patch.object(doctor, "_wake_alarm", return_value=doctor.check("wake alarm", True, "info", "x")) as seen:
            checks = self.run_doctor(self.local_config(wakes={"timezone": "Europe/London",
                                                              "power": {"off": "23:00", "wake": "07:00"}}))
        self.assertIn("wake alarm", checks, "asked with a power schedule, phone channels or not")
        self.assertEqual(seen.call_args.kwargs, {"quiet": True})
        with mock.patch.object(doctor, "_wake_alarm") as seen:
            self.run_doctor(self.local_config(wakes={"power": {"off": None}}))
        seen.assert_not_called()

    def test_custody_off_names_the_servers_that_load_models(self) -> None:
        """No register on this host: a note for the local servers the harness may load or swap models on."""
        data = self.local_config(providers={"tabby": {"type": "openai", "base_url": "http://127.0.0.1:9/v1",
                                                      "api_key_env": None, "allow_model_swap": True}})
        self.assertCheck(self.run_doctor(data), "custody", True, "info", "off: no GPU custody register on this host",
                         "before tabby loads or swaps a model", "by UUID")
        self.assertCheck(self.run_doctor(), "custody", True, "info", "off: no GPU custody register on this host",
                         "no provider sets lazy_load or allow_model_swap")
        self.assertNotIn("tabby", self.run_doctor()["custody"]["detail"])

    def test_a_missing_key_file_whose_folder_is_not_here(self) -> None:
        from swfactory.config import ProviderConfig
        from swfactory.settings import missing_key_message

        absent = ProviderConfig(name="strata", type="openai", base_url="http://127.0.0.1:9/v1",
                                api_key_env="STRATA_TEST_KEY", api_key_file=str(self.tmp / "no-folder" / "strata.env"))
        self.assertIn(f"there is no {self.tmp / 'no-folder'} on this machine", missing_key_message(absent))
        self.assertIn("put STRATA_TEST_KEY=<key> in the env file", missing_key_message(absent))
        absent.api_key_file = str(self.tmp / "strata.env")
        self.assertTrue(missing_key_message(absent).endswith("strata.env does not exist"))
        (self.tmp / "strata.env").write_text("OTHER=1\n")
        self.assertTrue(missing_key_message(absent).endswith("strata.env does not define it"))

    def test_a_local_server_down_gets_advice_on_how_to_look_never_a_look(self) -> None:
        """The "local servers" line never looks for model servers itself: with a server down it says how to ask
        (the setup page's button, `factory doctor --look-around`); given the owner's look, it names the model
        servers found on other ports than the configured ones."""
        from swfactory.config import ProviderConfig

        tabby = ProviderConfig(name="tabby", type="openai", base_url="http://127.0.0.1:5000/v1", api_key_env=None)
        down = [doctor.check("provider tabby reachable", False, "warn", "cannot reach")]
        asked: list[set] = []

        def find(skip: set) -> list[dict]:
            asked.append(skip)
            return [{"address": "127.0.0.1", "port": 5000, "status": 401, "ids": []},
                    {"address": "127.0.0.1", "port": 5005, "status": 401, "ids": []},
                    {"address": "::1", "port": 7860, "status": 200, "ids": ["a", "b", "c", "d"]}]
        config = Config(self.config_path, self.local_config(server={"token": "t", "port": 8771}))
        [line] = doctor._local_servers([tabby], down, config)
        self.assertEqual((line["ok"], line["level"]), (False, "warn"))
        self.assertIn('down: tabby; to look for a model server on another port, press "Look for model servers" on '
                      "the setup page's Server details step (Setup, then Server details), or run factory doctor "
                      "--look-around", line["detail"])
        looked = doctor.look_for_model_servers(config, find)
        self.assertEqual(asked, [{8771}], "the owner's look skips only the harness's own port")
        [line] = doctor._local_servers([tabby], down, config, looked)
        self.assertIn("answering /v1/models elsewhere on this machine: http://127.0.0.1:5005/v1 (wants a key), "
                      "http://[::1]:7860/v1 (lists a, b, c …)", line["detail"])
        self.assertNotIn(":5000/v1", line["detail"], "the configured server's own port is not offered")
        self.assertIn("the presets' ports are placeholders", line["detail"])
        nothing = doctor.look_for_model_servers(config, lambda skip: [])
        self.assertIn("the look-around found no other model server on this machine",
                      doctor._local_servers([tabby], down, config, nothing)[0]["detail"])
        up = [doctor.check("provider tabby reachable", True, "info", "ok")]
        self.assertEqual(doctor._local_servers([tabby], up, config)[0]["detail"], "up: tabby; down: none")
        self.assertEqual(doctor._local_servers([tabby], up, config, looked)[0]["detail"], "up: tabby; down: none")
        self.assertEqual(len(asked), 1)

    def test_the_doctor_looks_for_model_servers_only_when_asked(self) -> None:
        """run_checks (GET /api/doctor, the GUI's Doctor badge, and `factory doctor`) never calls
        listeners.model_servers, even with a configured local server down; look_around=True (`factory doctor
        --look-around`) calls it exactly once and reports what it found and sent."""
        calls: list[set] = []

        def finder(skip_ports=frozenset(), *args: Any, **kwargs: Any) -> list[dict]:
            calls.append(set(skip_ports))
            return [{"address": "127.0.0.1", "port": 5005, "status": 200, "ids": ["qwen"]}]
        data = self.local_config(providers={"tabby": {"type": "openai", "api_key_env": None,
                                                      "base_url": f"http://127.0.0.1:{closed_port()}/v1"}},
                                 models={"sonnet": {"provider": "tabby", "model": "x"}})
        with mock.patch("swfactory.listeners.model_servers", finder):
            checks = self.run_doctor(data)
            self.assertEqual(calls, [], "the doctor never looks on its own")
            self.assertCheck(checks, "local servers", True, "info", "down: tabby", "Look for model servers",
                             "factory doctor --look-around")
            self.assertNotIn("model servers here", checks)
            checks = self.run_doctor(data, look_around=True)
        self.assertEqual(calls, [{8765}], "asked once, the harness's own port skipped")
        self.assertCheck(checks, "local servers", True, "info", "http://127.0.0.1:5005/v1 (lists qwen)")
        self.assertCheck(checks, "model servers here", True, "info", "http://127.0.0.1:5005/v1 (lists qwen)",
                         "Sent one GET /v1/models, without a key, to each program listening on this machine")
        with mock.patch("swfactory.listeners.model_servers", lambda *args, **kwargs: []):
            self.assertCheck(self.run_doctor(data, look_around=True), "model servers here", True, "info",
                             "none of the programs listening here answered like a model server")

        def broken(*args: Any, **kwargs: Any) -> list[dict]:
            raise OSError("no /proc here")
        with mock.patch("swfactory.listeners.model_servers", broken):
            checks = self.run_doctor(data, look_around=True)
        self.assertCheck(checks, "model servers here", False, "warn", "the look did not work: OSError: no /proc here")
        self.assertCheck(checks, "local servers", True, "info", "the look for other model servers here did not work")

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


class LookAroundTest(unittest.TestCase):
    """doctor.LookAround, behind POST /api/setup/look-around: one look per ask, one at a time, at most once in
    LOOK_AROUND_GAP seconds and LOOK_AROUND_PER_HOUR times an hour; the last result kept for GET /api/setup."""

    def setUp(self) -> None:
        self.calls: list[set] = []
        self.looks = doctor.LookAround()
        self.config = Config(Path("/nonexistent/config.json"), {"server": {"token": "t"}})

    def find(self, skip: set) -> list[dict]:
        self.calls.append(set(skip))
        return [{"address": "127.0.0.1", "port": 5005, "status": 200, "ids": [str(len(self.calls))]}]

    def test_each_ask_looks_once_and_a_second_too_soon_is_refused(self) -> None:
        self.assertIsNone(self.looks.last())
        first = self.looks.ask(self.config, 1000.0, self.find)
        self.assertEqual(self.calls, [{8765}])
        self.assertEqual(first["model_servers"][0]["ids"], ["1"])
        for now in (1000.0, 1001.5, 1000.0 + doctor.LOOK_AROUND_GAP - 0.1):
            with self.subTest(now=now), self.assertRaises(doctor.LookAroundRefused) as refused:
                self.looks.ask(self.config, now, self.find)
            self.assertGreater(refused.exception.retry_after, 0)
            self.assertLessEqual(refused.exception.retry_after, doctor.LOOK_AROUND_GAP)
            self.assertIn("You can look again in", str(refused.exception))
        self.assertEqual(len(self.calls), 1, "a double click looks once")
        self.assertEqual(self.looks.last(), first)
        second = self.looks.ask(self.config, 1000.0 + doctor.LOOK_AROUND_GAP, self.find)
        self.assertEqual((len(self.calls), second["model_servers"][0]["ids"]), (2, ["2"]))
        self.assertEqual(self.looks.last(), second)
        kept = self.looks.last()
        kept["model_servers"].clear()
        self.assertEqual(self.looks.last(), second, "last() is a copy")

    def test_a_stuck_script_gets_at_most_the_hourly_budget(self) -> None:
        now = 0.0
        for _ in range(doctor.LOOK_AROUND_PER_HOUR):
            self.looks.ask(self.config, now, self.find)
            now += doctor.LOOK_AROUND_GAP
        self.assertEqual(len(self.calls), doctor.LOOK_AROUND_PER_HOUR)
        for _ in range(50):   # a script that keeps asking, politely spaced
            with self.assertRaises(doctor.LookAroundRefused) as refused:
                self.looks.ask(self.config, now, self.find)
            now += doctor.LOOK_AROUND_GAP
        self.assertIn("times in the last hour", str(refused.exception))
        self.assertEqual(len(self.calls), doctor.LOOK_AROUND_PER_HOUR, "no look past the budget")
        self.assertGreaterEqual(refused.exception.retry_after, 0)
        self.looks.ask(self.config, 3600.0, self.find)
        self.assertEqual(len(self.calls), doctor.LOOK_AROUND_PER_HOUR + 1, "the first look of the hour has aged out")

    def test_one_look_at_a_time(self) -> None:
        started, release = threading.Event(), threading.Event()

        def slow(skip: set) -> list[dict]:
            self.calls.append(set(skip))
            started.set()
            release.wait(5)
            return []
        results: list[dict] = []
        worker = threading.Thread(target=lambda: results.append(self.looks.ask(self.config, 0.0, slow)))
        worker.start()
        self.assertTrue(started.wait(5))
        with self.assertRaises(doctor.LookAroundRefused) as refused:
            self.looks.ask(self.config, 500.0, slow)
        self.assertIn("running now", str(refused.exception))
        release.set()
        worker.join(5)
        self.assertEqual((len(self.calls), results[0]["model_servers"]), (1, []))
        self.looks.ask(self.config, 500.0, self.find)
        self.assertEqual(len(self.calls), 2)

    def test_a_look_that_breaks_still_counts_and_is_kept(self) -> None:
        def broken(skip: set) -> list[dict]:
            self.calls.append(set(skip))
            raise OSError("no /proc here")
        self.assertEqual(self.looks.ask(self.config, 0.0, broken)["error"], "OSError: no /proc here")
        with self.assertRaises(doctor.LookAroundRefused):
            self.looks.ask(self.config, 1.0, broken)
        self.assertEqual((len(self.calls), self.looks.last()["error"]), (1, "OSError: no /proc here"))


class SetupStatusTest(DoctorTestCase):
    def status(self, data: dict) -> dict:
        result = doctor.setup_status(Config(self.config_path, data))
        self.assertEqual(set(result), {"configured", "steps", "host"})
        self.assertEqual(set(result["host"]), {"key_dirs", "listening"}, "no look-around unless the owner asked")
        self.assertEqual(set(result["steps"]), {"providers", "models", "routing", "repos", "tools", "output"})
        for step in result["steps"].values():
            self.assertEqual(set(step), {"ok", "detail"})
            self.assertIsInstance(step["detail"], str)
        return result

    def test_the_host_the_wizard_may_suggest_from(self) -> None:
        """setup_host: the default providers' key folders (a preset's placeholder need not exist here) and the
        ports from 1024 up that listen on loopback or every address (not one bound to another address only)."""
        net = self.tmp / "net"
        net.mkdir()
        head = "  sl  local_address rem_address   st\n"
        (net / "tcp").write_text(head + "   0: 0100007F:138D 00000000:0000 0A\n"     # 127.0.0.1:5005
                                 "   1: 00000000:0016 00000000:0000 0A\n"            # 0.0.0.0:22
                                 "   2: 0500000A:1EB4 00000000:0000 0A\n"            # 10.0.0.5:7860
                                 "   3: 00000000:46A2 00000000:0000 0A\n")           # 0.0.0.0:18082
        host = doctor.setup_host(net)
        self.assertEqual(host["listening"], [5005, 18082])
        self.assertIn("/etc/ai-stack", host["key_dirs"], "the strata default's key folder")
        self.assertEqual(host["key_dirs"]["/etc/ai-stack"], os.path.isdir("/etc/ai-stack"))

    def test_the_setup_status_never_looks_for_model_servers(self) -> None:
        """GET /api/setup (setup_status), which the GUI reads on load and every 15 s, never calls
        listeners.model_servers: host carries the last look the owner asked for, marked with when it was taken,
        or no model_servers at all."""
        calls: list[set] = []

        def finder(skip_ports=frozenset(), *args, **kwargs):
            calls.append(set(skip_ports))
            return [{"address": "127.0.0.1", "port": 5005, "status": 200, "ids": ["m"]}]
        config = Config(self.config_path, self.local_config(server={"token": TOKEN, "port": 8771}))
        with mock.patch("swfactory.listeners.model_servers", finder):
            for _ in range(3):
                host = doctor.setup_status(config)["host"]
                self.assertNotIn("model_servers", host)
                self.assertNotIn("model_servers_at", host)
            self.assertEqual(calls, [])
            looked = doctor.look_for_model_servers(config)
            self.assertEqual(calls, [{8771}])
            host = doctor.setup_status(config, looked=looked)["host"]
        self.assertEqual(len(calls), 1, "showing the last look does not look again")
        self.assertEqual(host["model_servers"], [{"url": "http://127.0.0.1:5005/v1", "port": 5005, "status": 200,
                                                  "ids": ["m"]}])
        self.assertEqual(host["model_servers_at"], looked["at"])
        self.assertRegex(host["model_servers_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_a_look_that_broke_is_not_shown_as_one_that_found_nothing(self) -> None:
        """A look whose finder raised found nothing, so the setup status carries its error (model_servers_error)
        and when it was taken, and no model_servers: the wizard must not say "nothing answered"."""
        def broken(skip):
            raise PermissionError(13, "Permission denied", "/proc/net/tcp")
        looked = doctor.look_for_model_servers(None, broken)
        host = doctor.setup_status(Config(self.config_path, self.local_config()), looked=looked)["host"]
        self.assertNotIn("model_servers", host)
        self.assertEqual(host["model_servers_error"], "PermissionError: [Errno 13] Permission denied: '/proc/net/tcp'")
        self.assertEqual(host["model_servers_at"], looked["at"])

    def test_the_owner_s_look_offers_the_model_servers_that_answer(self) -> None:
        """look_for_model_servers: each listener that answers /v1/models like a model server, as an address the
        wizard can offer (the presets' ports are placeholders: B.5.4), the harness's own port skipped, at most six
        with five ids each, with what was sent; a look that breaks says so."""
        asked = []

        def find(skip):
            asked.append(set(skip))
            return [{"address": "127.0.0.1", "port": 5005, "status": 200, "ids": [f"m{i}" for i in range(9)]},
                    {"address": "::1", "port": 18082, "status": 401}] + [
                {"address": "127.0.0.1", "port": 6000 + i, "status": 401} for i in range(6)]
        looked = doctor.look_for_model_servers(None, find)
        self.assertEqual(asked, [{8765}])
        self.assertEqual(set(looked), {"at", "model_servers", "note"})
        self.assertEqual(looked["model_servers"][:2], [
            {"url": "http://127.0.0.1:5005/v1", "port": 5005, "status": 200, "ids": ["m0", "m1", "m2", "m3", "m4"]},
            {"url": "http://[::1]:18082/v1", "port": 18082, "status": 401, "ids": []}])
        self.assertEqual(len(looked["model_servers"]), 6)
        self.assertIn("one GET /v1/models, without a key, to each program listening on this machine", looked["note"])

        def broken(skip):
            raise OSError("no /proc here")
        failed = doctor.look_for_model_servers(None, broken)
        self.assertEqual(set(failed), {"at", "error"}, "a look that broke has no answer: no model_servers, no note")
        self.assertEqual(failed["error"], "OSError: no /proc here")

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


class MessagesDoctorTest(DoctorTestCase):
    """The Wake & Messages checks (DESIGN §34)."""

    def setUp(self) -> None:
        super().setUp()
        run = mock.patch.object(doctor, "_notify_run", return_value="")
        run.start()
        self.addCleanup(run.stop)

    def phone(self, **sections: Any) -> dict:
        channel = {"type": "telegram", "token_env": "SERAPHINA_TELEGRAM_BOT_TOKEN", "model_text": "never"}
        wakes = {"timezone": "Europe/London", **sections.pop("wakes", {})}
        return self.local_config(wakes=wakes, notify={"channels": {"tg": channel},
                                                      "sensitive_terms_file": str(self.tmp / "terms.txt")},
                                 **sections)

    def test_phone_channels_without_bwrap_are_an_error_even_when_acknowledged(self) -> None:
        checks = self.run_doctor(self.phone())
        self.assertCheck(checks, "messages sandbox", False, "error", "harness.sandbox is none")
        state = self.tmp / "data" / "notify"
        state.mkdir(parents=True)
        (state / "state.json").write_text(json.dumps({"acknowledged": {"at": "2026-10-21T10:00:00Z"}}))
        checks = self.run_doctor(self.phone())
        self.assertCheck(checks, "messages sandbox", False, "error", "you accepted this risk on 2026-10-21")
        with mock.patch.object(doctor, "bwrap_available", return_value=True):
            checks = self.run_doctor(self.phone(harness={"sandbox": "bwrap"}))
        self.assertCheck(checks, "messages sandbox", True, "info")
        self.assertCheck(checks, "messages network", True, "info", "sandbox_network is true")

    def test_no_message_checks_without_a_channel(self) -> None:
        checks = self.run_doctor()
        self.assertNotIn("messages sandbox", checks)
        self.assertNotIn("time zone", checks)

    def test_terms_file_mode_and_absence(self) -> None:
        checks = self.run_doctor(self.phone())
        self.assertCheck(checks, "sensitive words", False, "warn", "tell it you have none")
        terms = self.tmp / "terms.txt"
        terms.write_text("diabetes\n")
        os.chmod(terms, 0o644)
        self.assertCheck(self.run_doctor(self.phone()), "sensitive words", False, "error", "chmod 600")
        os.chmod(terms, 0o600)
        self.assertCheck(self.run_doctor(self.phone()), "sensitive words", True, "info", "1 words")

    def test_hooks_paused_and_tamper(self) -> None:
        data = self.phone(wakes={"hooks": {"backup-script": {"token_sha256": "b" * 64, "events": ["backup.failed"]}}})
        wakes = self.tmp / "data" / "wakes"
        wakes.mkdir(parents=True)
        (wakes / "PAUSED").write_text(json.dumps({"by": "phone", "at": "2026-10-21T22:00:00Z"}))
        notify = self.tmp / "data" / "notify"
        notify.mkdir(parents=True)
        (notify / "state.json").write_text(json.dumps({"tamper": {"what": ["notify"]}, "liveness": "2026-10-21T22:00:00Z"}))
        checks = self.run_doctor(data)
        self.assertCheck(checks, "event source backup-script", True, "info")
        self.assertCheck(checks, "seraphina", True, "info", "stopped by phone")
        self.assertCheck(checks, "messages settings", False, "error", "notify")
        self.assertCheck(checks, "notifier", True, "info", "last tick")

    def test_telegram_channel_checks(self) -> None:
        from tests.mock_telegram import MockTelegram, TOKEN as TG_TOKEN

        with MockTelegram() as tg, mock.patch.dict(os.environ, {"SERAPHINA_TELEGRAM_BOT_TOKEN": TG_TOKEN}), \
                mock.patch.object(doctor, "CHANNEL_KWARGS", {"api_base": tg.api_base}):
            tg.webhook_url = "https://someone.example/hook"
            checks = self.run_doctor(self.phone())
        self.assertCheck(checks, "channel tg bot", True, "info", "@quiet_helper_bot")
        self.assertCheck(checks, "channel tg webhook", False, "error", "another program")
        self.assertCheck(checks, "channel tg paired", False, "warn", "pair your phone")

    def test_ntfy_channel_checks(self) -> None:
        from tests.mock_ntfy import HARNESS_TOKEN, REPLY_TOKEN, MockNtfy

        with MockNtfy(unix=True) as ntfy, mock.patch.dict(os.environ, {"SERAPHINA_NTFY_TOKEN": HARNESS_TOKEN,
                                                                       "SERAPHINA_NTFY_REPLY_TOKEN": REPLY_TOKEN}):
            channel = {"type": "ntfy", "publish_url": ntfy.publish_url, "public_url": "https://h.tail0.ts.net:8445",
                       "token_env": "SERAPHINA_NTFY_TOKEN", "reply_token_env": "SERAPHINA_NTFY_REPLY_TOKEN"}
            data = self.local_config(wakes={"timezone": "Europe/London"}, notify={"channels": {"phone": channel}})
            checks = self.run_doctor(data)
        self.assertCheck(checks, "channel phone health", True, "info")
        self.assertCheck(checks, "channel phone access", True, "info", "three topics")
        self.assertCheck(checks, "channel phone funnel", True, "info")
        self.assertCheck(checks, "channel phone privacy", False, "warn", "third-party")


class WakeDoctorTest(DoctorTestCase):
    """The persona checks of Wake & Messages W2/W3 (doctor._wake_checks): read only, never waking a model."""

    def wake_config(self, persona: dict | None = None, **sections: Any) -> Config:
        cfg = {"enabled": True, "model": "opus", **(persona or {})}
        data = self.local_config(wakes={"enabled": True, "timezone": "Europe/London", "personas": {"seraphina": cfg},
                                        **sections.pop("wakes", {})}, **sections)
        config = Config(self.config_path, data)
        config.save()
        return Config.load(self.config_path)

    def state(self, **values: Any) -> None:
        wakes = self.tmp / "data" / "wakes"
        wakes.mkdir(parents=True, exist_ok=True)
        (wakes / "state.json").write_text(json.dumps(values))

    def checks(self, config: Config, get: Any = None, bwrap: bool = True) -> dict[str, dict]:
        calls: list[str] = []

        def fake_get(url: str, timeout: float) -> tuple[Any, Any]:
            calls.append(url)
            return (get or (lambda u: (404, None)))(url)

        found = doctor._wake_checks(config, get=fake_get, bwrap=lambda: bwrap)
        self.assertShape(found)
        self.assertTrue(all("/completions" not in url for url in calls), "the doctor never calls a model")
        return {check["name"]: check for check in found}

    def test_the_sandbox_precondition_is_an_error_and_bubblewrap_is_said_plainly(self) -> None:
        checks = self.checks(self.wake_config(), bwrap=False)
        self.assertCheck(checks, "personas sandbox", False, "error", "harness.sandbox is none")
        self.assertCheck(checks, "bubblewrap", True, "info", "not installed or not usable",
                         "sudo apt install bubblewrap")
        read_only = self.wake_config({"wake": {"tools": "read_only"}}, harness={"sandbox": "bwrap"})
        checks = self.checks(read_only, bwrap=False)
        self.assertCheck(checks, "bubblewrap", False, "error", "read-only wake tools are refused")
        self.assertNotIn("personas sandbox", self.checks(self.wake_config(harness={"sandbox": "bwrap"})))

    def test_the_model_must_be_attested_local_and_a_model_server(self) -> None:
        config = self.wake_config(harness={"sandbox": "bwrap"})
        self.assertCheck(self.checks(config), "persona seraphina model", False, "error", "not confirmed to run on "
                         "this machine")
        self.state(attested={"mocklocal": {"local": True}}, tick_at="2026-10-21T11:59:00Z")
        checks = self.checks(config)
        self.assertCheck(checks, "persona seraphina model", True, "info", "attested local")
        self.assertCheck(checks, "wake scheduler", True, "info", "last tick")
        owui = self.checks(config, get=lambda url: (200, {"name": "Open WebUI"}) if url.endswith("/api/config")
                           else (404, None))
        self.assertCheck(owui, "persona seraphina model", False, "error", "is Open WebUI")
        proxy = self.checks(config, get=lambda url: (200, {"litellm_version": "1.74.0"})
                            if url.endswith("/health/readiness") else (404, None))
        self.assertCheck(proxy, "persona seraphina model", False, "error", "is a proxy (LiteLLM)", "S7")
        asleep = self.wake_config(harness={"sandbox": "bwrap"},
                                  wakes={"servers": {"mocklocal": {"kind": "llamacpp"}}})
        checks = self.checks(asleep, get=lambda url: (200, {"is_sleeping": True}) if url.endswith("/props")
                             else (404, None))
        self.assertCheck(checks, "persona seraphina model", True, "info", "asleep", "never wake it")
        self.assertCheck(self.checks(self.wake_config({"model": None}, harness={"sandbox": "bwrap"})),
                         "persona seraphina model", False, "warn", "no model yet")

    def test_a_hand_edited_soul_a_missing_file_and_an_open_breaker(self) -> None:
        own = self.home / ".config" / "software-factory" / "personas"
        own.mkdir(parents=True)
        (own / "seraphina.md").write_text("---\nname: seraphina\ndescription: s\ntools: Bash\n---\nEdited soul.\n")
        self.state(breaker={"open": True, "why": "5 failed turns in a row"})
        checks = self.checks(self.wake_config(harness={"sandbox": "bwrap"}))
        self.assertCheck(checks, "persona seraphina instructions", False, "warn", "changed outside her page")
        self.assertCheck(checks, "persona seraphina tools", False, "warn", "wake.tools in the settings decides")
        self.assertCheck(checks, "wake breaker", False, "warn", "5 failed turns")
        missing = self.local_config(harness={"sandbox": "bwrap"},
                                    wakes={"enabled": True, "personas": {"nobody": {"enabled": True}}})
        Config(self.config_path, missing).save()
        self.assertCheck(self.checks(Config.load(self.config_path)), "persona nobody", False, "error",
                         "there is no personas/nobody.md")

    def test_her_query_is_run_once_read_only_and_the_ledger_s_caps_are_said(self) -> None:
        """The query is the persona's own and must match her schema: the doctor runs it once, read-only, and says
        whether it works (never what it returns). With the read ledger on, it says when copies or manifests went for
        the cap."""
        import sqlite3

        root = self.tmp / "persona-memory"
        root.mkdir()
        db = root / "memory.db"
        conn = sqlite3.connect(db)
        with conn:
            conn.execute("CREATE TABLE facts (text TEXT, at TEXT)")
            conn.executemany("INSERT INTO facts VALUES (?, ?)", [("one fact", None), ("another", None)])
        conn.close()
        before = db.read_bytes()
        external = {"sqlite": {"path": str(db), "query": "SELECT text, at FROM facts WHERE hidden = 0"}, "ledger": True}
        config = self.wake_config({"memory": {"external": external}}, harness={"sandbox": "bwrap"},
                                  wakes={"external_memory_roots": [str(root)]})
        checks = self.checks(config)
        self.assertCheck(checks, "persona seraphina database", False, "warn",
                         "cannot be read: the query failed (no such column: hidden)", "must match her own schema")
        self.assertNotIn("ACL", checks["persona seraphina database"]["detail"], "her schema's business, not access")
        self.assertCheck(checks, "persona seraphina read ledger", True, "info", "each read of her own memory")
        personas.PersonaStore(config.data_dir)._count_in_meta("seraphina", "evicted", 3)
        external["sqlite"]["query"] = "SELECT text, at FROM facts"
        config = self.wake_config({"memory": {"external": external}}, harness={"sandbox": "bwrap"},
                                  wakes={"external_memory_roots": [str(root)]})
        checks = self.checks(config)
        self.assertCheck(checks, "persona seraphina database", True, "info", "her query reads 2 rows")
        self.assertNotIn("one fact", json.dumps(checks), "never what it returns")
        self.assertCheck(checks, "persona seraphina read ledger", False, "warn", "3 older copies removed",
                         "the newest version of each file kept", "The oldest rollbacks are gone")
        self.assertEqual(db.read_bytes(), before, "read-only")

    def test_a_database_the_harness_cannot_open_points_at_a_snapshot_never_at_an_acl(self) -> None:
        """Granting the harness read access on a persona's live SQLite store (an ACL, a looser mode) can break the
        store: when her database cannot be opened, the doctor says to name a read-only snapshot her own tools
        publish, or to leave sqlite out, and never to open up her store. A query that fails is her schema's
        business and gets no such advice."""
        root = self.tmp / "persona-memory"
        root.mkdir()
        cases = (("no file", root / "memory.db", None),
                 ("refused", root / "store.db", (None, "the database could not be opened", "unable to open database")))
        for label, db, answer in cases:
            with self.subTest(label), contextlib.ExitStack() as stack:
                if answer:
                    db.write_bytes(b"")
                    stack.enter_context(mock.patch.object(personas, "sqlite_rows", return_value=answer))
                external = {"sqlite": {"path": str(db), "query": "SELECT text FROM facts"}}
                config = self.wake_config({"memory": {"external": external}}, harness={"sandbox": "bwrap"},
                                          wakes={"external_memory_roots": [str(root)]})
                checks = self.checks(config)
                self.assertCheck(checks, "persona seraphina database", False, "warn", "cannot be read: ",
                                 "do not add an ACL to her live store or its folder, or loosen their mode",
                                 "can break the store", "a read-only snapshot her own tools publish",
                                 "or leave sqlite out")
                self.assertNotIn("must match her own schema", checks["persona seraphina database"]["detail"])
        self.assertFalse((root / "memory.db").exists(), "the doctor creates nothing there")

    def test_external_memory_outside_the_allowed_roots_and_a_broken_rulebook(self) -> None:
        outside = self.tmp / "elsewhere"
        config = self.wake_config({"memory": {"external": {"folder": str(outside)}}}, harness={"sandbox": "bwrap"},
                                  wakes={"external_memory_roots": [str(self.tmp / "persona-memory")]})
        self.assertCheck(self.checks(config), "persona seraphina memory", False, "error",
                         "not under wakes.external_memory_roots")
        (self.config_path.parent / "notice-rules.json").write_text('{"version": 7}')
        self.assertCheck(self.checks(config), "notice rules", False, "error", "stay off the phone")
