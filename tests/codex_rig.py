"""The Codex worker's test rig (BROWSER_AND_CODEX.md §4.1, §4.6; DESIGN §44).

A factory job on the fixture app (tests/fixtures/fixture-app) whose builder runs on the ``codex-worker`` driver:
the spec-writer, reviewers and approver are scripted stand-ins for stub models (tests/test_orchestrator.py's
Recorder), the builder is the real CliRunner through the real ``bin/sf-codex-launch``. Codex is either

- the real pinned binary (``SF_CODEX_BIN``: the native binary of ``@openai/codex@0.163.0``, installed outside the
  repository; the live tests skip without it), talking to ``tests/mock_responses.py`` (a Responses-API stub scripting the fix), or
- ``tests/fake_codex.py`` (the unit tests: it plays a scenario, it is not Codex and has no sandbox).

The stub is the route's provider: a loopback server marked ``lazy_load`` under ``tests/mock_custody.py``'s register,
whose Responses check passes first (``work.responses_check`` under a custody request lease), so every Codex run takes
a custody request lease as a server run would (§3.3 (c)). Nothing reaches the outside network.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest import mock

from swfactory.codex import work
from swfactory.config import Config, deep_merge
from swfactory.events import EventBus
from swfactory.orchestrator import Factory
from swfactory.runners import make_runner
from tests.helpers import make_repo
from tests.mock_custody import MockRegister
from tests.mock_responses import MockResponses
from tests.test_orchestrator import Recorder, fake_module, swap_module

HERE = Path(__file__).resolve().parent
FIXTURE_APP = HERE / "fixtures" / "fixture-app"
FAKE_CODEX = HERE / "fake_codex.py"
REPO = "fixture-app"
NOBODY_GROUP = "nogroup"
STUB_MODEL = "stub-model"
KEY = "stub-provider-key-0123456789"
FIX = ("python3 - <<'PY'\nimport pathlib\np = pathlib.Path('app.py')\n"
       "p.write_text(p.read_text().replace('return \"Hello, there\"', 'return f\"Hello, {name}\"'))\nPY\n"
       "printf 'STATUS: READY\\n\\n# Build\\nThe greeting shows the name.\\n\\n## Items\\n"
       "- I-1 done: `python3 -m unittest -q` passed\\n' > {out_dir}/build.md")


def real_codex() -> str | None:
    """The pinned Codex binary for the live tests, or None (they skip)."""
    candidate = os.environ.get("SF_CODEX_BIN")
    if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate
    return None


def codex_version(binary: str) -> str | None:
    try:
        done = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=30,
                              env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "CODEX_HOME": "/nonexistent"})
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip().split()[-1] if done.returncode == 0 and done.stdout.strip() else None


def live_reason() -> str | None:
    from swfactory.harness.sandbox import bwrap_available

    binary = real_codex()
    if binary is None:
        return "the pinned Codex binary is not installed (SF_CODEX_BIN; DESIGN §44)"
    if codex_version(binary) != "0.163.0":
        return f"{binary} is not Codex 0.163.0 (the pin)"
    if not bwrap_available():
        return "bubblewrap is not usable here (Codex's own sandbox needs one level of it)"
    return None


def spec(ctx: Any, call: Any) -> None:
    (ctx.job_dir / "spec.md").write_text("# Spec\n\nFix the greeting so it shows the submitted name.\n\n"
                                         "## Acceptance criteria\n1. The greeting shows the submitted name.\n")


def approve(ctx: Any, call: Any) -> None:
    from tests.test_orchestrator import prompt_ids

    lines = "".join(f"- {item} verified: the harness's verify check passed\n"
                    for item in prompt_ids(call.prompt, "waiting for your check"))
    (ctx.job_dir / "decision.md").write_text("APPROVE\n\n## Acceptance criteria\n1. Met: the check passed.\n\n"
                                             + (f"## Items\n{lines}" if lines else ""))


class Rig:
    """One temporary world: the repository, the data folder, the stub model, the custody register, the config."""

    def __init__(self, case: Any, *, binary: str | None = None, scenario: list | None = None,
                 script: list | None = None, repo_extra: dict | None = None, codex: dict | None = None,
                 sections: dict | None = None, check: bool = True, verify: str | None = "python3 -m unittest -q",
                 fake_version: str | None = None, as_nobody: bool = False) -> None:
        self.case = case
        self.tmp = Path(tempfile.mkdtemp(prefix="sf-codex-"))
        if not as_nobody:   # as nobody, block c2 itself lets the group through this 0700 folder
            os.chmod(self.tmp, 0o755)
        case.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        home = self.tmp / "home"
        home.mkdir()
        patcher = mock.patch.dict(os.environ, {"HOME": str(home)})
        patcher.start()
        case.addCleanup(patcher.stop)
        swap_module(case, fake_module("swfactory.chats", ChatManager=_NoChats))
        swap_module(case, fake_module("swfactory.plans", PlanManager=_NoChats))
        files = {path.name: path.read_text() for path in FIXTURE_APP.iterdir() if path.is_file()}
        self.repo = make_repo(self.tmp, REPO, files)
        self.stub = MockResponses(script or [], models=(STUB_MODEL,))
        self.stub.start()
        case.addCleanup(self.stub.stop)
        self.register = MockRegister(ttl=600)
        self.register.start()
        case.addCleanup(self.register.stop)
        self.register.load("strata", STUB_MODEL)
        self.key_file = self.tmp / "strata.env"
        self.key_file.write_text(f"STRATA_API_KEY={KEY}\n")
        os.chmod(self.key_file, 0o600)
        if binary is None:   # the fake, copied so its scenario sits beside it
            folder = self.tmp / "fake-codex"
            folder.mkdir()
            binary = str(folder / "codex")
            shutil.copy(FAKE_CODEX, binary)
            os.chmod(binary, os.stat(binary).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            (folder / "scenario.json").write_text(json.dumps({"launches": scenario or [[{"text": "Done."}]]}))
            if fake_version:
                (folder / "version.txt").write_text(fake_version)
            self.fake_dir: Path | None = folder
        else:
            self.fake_dir = None
        repo_entry = {"name": REPO, "path": str(self.repo), **({"verify": verify} if verify else {}),
                      **(repo_extra or {})}
        if as_nobody:   # a harness running as root starts Codex as nobody (setpriv): the owner's O3, emulated
            binary = self._as_nobody(binary)
            codex = {"user": "nobody", "via": "setpriv", "group": NOBODY_GROUP,
                     "launcher": str(self.tmp / "codex-bin" / "sf-codex-launch"), **(codex or {})}
            # The suite never loads the owner's uid egress rule on this machine, so the run gate that wants it
            # (work.containment_blocker, tested on its own in test_codex_containment) stands aside here.
            gate = mock.patch.object(work, "containment_blocker", return_value=None)
            gate.start()
            case.addCleanup(gate.stop)
        self.binary = binary
        raw = {"data_dir": str(self.tmp / "data"), "repos": [repo_entry],
               "pipeline": {"reviewers": ["code"], "max_parallel_jobs": 1},
               "providers": {"strata": {"type": "openai", "base_url": self.stub.url, "api_key_env": "STRATA_API_KEY",
                                        "api_key_file": str(self.key_file), "lazy_load": True,
                                        "custody": {"url": self.register.url}, "timeout": 120}},
               "models": {"local": {"provider": "strata", "model": STUB_MODEL}},
               "agents": {"*": {"driver": "builtin", "model": "local"},
                          "builder": {"driver": "codex-worker", "model": "local"}},
               "limits": {"max_wall_seconds": 120},
               "codex": {"enabled": True, "binary": binary, **(codex or {})}}
        self.config = Config(self.tmp / "conf" / "config.json", deep_merge(raw, sections or {}))
        (self.tmp / "conf").mkdir(mode=0o700, exist_ok=True)
        self.config.validate()
        if as_nobody:
            self._block_c2()
        if check:
            record = work.responses_check(self.config, self.config.model("local"))
            case.assertTrue(record["ok"], record)
        self.recorder = Recorder()
        self.recorder.actions.update({"spec-writer": spec, "approver": approve})
        self.bus = EventBus()
        self.factory: Factory | None = None

    def _as_nobody(self, binary: str) -> str:
        """For nobody and its group, on this rig's own folders only: the harness runs with umask 077, as `factory
        serve` does under its lock; the key file is readable by the group (as block c4 installs the server's copy),
        and the binary and the launcher are installed as root installs them (blocks c7 and c3). Block c2 itself
        comes after the config (_block_c2). Nothing outside the rig changes."""
        import grp

        gid = grp.getgrnam(NOBODY_GROUP).gr_gid
        self.case.addCleanup(os.umask, os.umask(0o077))
        os.chown(self.key_file, 0, gid)
        os.chmod(self.key_file, 0o640)
        (self.tmp / "conf").mkdir(mode=0o700)   # the harness's config folder, as an install keeps it
        folder = self.tmp / "codex-bin"
        folder.mkdir()
        os.chmod(folder, 0o755)   # explicit: the harness may run with umask 077 (and a test may have left one)
        target = folder / "codex"
        try:
            os.link(binary, target)
        except OSError:
            shutil.copy2(binary, target)
        os.chmod(target, 0o755)
        launcher = folder / "sf-codex-launch"   # installed as root installs it (the setup's block c3)
        shutil.copy(work.L.__file__, launcher)
        os.chmod(launcher, 0o755)
        return str(target)

    def _block_c2(self) -> None:
        """Block c2 exactly as `factory codex setup` prints it (sudo dropped: the harness here is root), after the
        harness's first start made its data folder: the traverse entries (this rig's 0700 folder above the data
        folder among them), the worktrees' default ACLs, the work root (2750) and the Codex user's homes. A line
        naming a path outside the rig stops the test rather than change the machine."""
        import shlex

        from swfactory.codex import setup as codex_setup

        (self.tmp / "data").mkdir(mode=0o700, exist_ok=True)
        [c2] = [b for b in codex_setup.blocks(self.config) if b["id"] == "c2"]
        for line in c2["commands"]:
            argv = shlex.split(line)
            argv = argv[3:] if argv[1] == "-u" else argv[1:]
            outside = [a for a in argv if a.startswith("/") and not (a + "/").startswith(str(self.tmp) + "/")]
            if outside:
                self.case.skipTest(f"block c2 names {outside}, outside the rig")
            subprocess.run(argv, check=True)

    def runner_factory(self, config: Config, driver: str) -> Any:
        if work.is_worker(config, driver):
            return make_runner(config, driver)
        return self.recorder.runner_factory(config, driver)

    def start(self) -> Factory:
        self.factory = Factory(self.config, self.bus, runner_factory=self.runner_factory)
        self.factory.start()
        self.case.addCleanup(self.factory.stop)
        return self.factory

    def run_job(self, feature: str = "Fix the greeting so it shows the submitted name", timeout: float = 240) -> dict:
        factory = self.factory or self.start()
        job = factory.submit(REPO, feature)
        return factory.wait(REPO, job["id"], timeout=timeout)

    def launches(self) -> list[dict]:
        if self.fake_dir is None or not (self.fake_dir / "launches.jsonl").exists():
            return []
        return [json.loads(line) for line in (self.fake_dir / "launches.jsonl").read_text().splitlines() if line]

    def receipts(self, job: dict) -> list[dict]:
        store = work.Receipts(self.config.data_dir)
        return [store.read(REPO, job["id"], name) for name in store.names(REPO, job["id"])]

    def items(self, job: dict) -> list[dict]:
        return json.loads((Path(job["job_dir"]) / "items.json").read_text())["items"]

    def events(self, job: dict, kind: str | None = None) -> list:
        return [e for e in self.bus.since(0) if e.job == job["id"] and (kind is None or e.type == kind)]

    def git(self, *args: str, cwd: Path | None = None) -> str:
        return subprocess.run(["git", "-C", str(cwd or self.repo), *args], capture_output=True, text=True,
                              check=True).stdout


class _NoChats:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.config = args[0] if args else None

    def start(self) -> None:
        pass

    def stop(self, timeout: float = 10.0) -> None:
        pass

    def extra_read_roots_for_job(self, job: dict) -> list[Path]:
        return []

    def plan_of_job(self, job: dict) -> str | None:
        return None

    def __getattr__(self, name: str) -> Any:
        return lambda *args, **kwargs: {}


def python() -> str:
    return os.path.realpath(sys.executable)
